"""The volume, as a number, and the menu's transparency.

The media player has a volume control too, on the 0-1 scale Home Assistant uses for
media players. This one is the box's own 0-100 scale, which is what a person reads off
the television and what every other tool that talks to enigma2 reports - including
OpenWebif. Having both means an automation can be written in whichever of the two the
person writing it is thinking in.

"Menu transparency" is the receiver's own setting of how see-through the menu's
background is, 0-80 % in tens, written with a partial `cmd/config` (ADR-0013). It exists
for a receiver that reports the setting.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import (
    ATTR_VOLUME_PASSTHROUGH,
    CAPABILITY_VOLUME_PASSTHROUGH,
    DOMAIN,
    MENU_TRANSPARENCY_MAX,
    MENU_TRANSPARENCY_MIN,
    MENU_TRANSPARENCY_STEP,
    SETTING_MENU_TRANSPARENCY,
    TOPIC_INFO,
    TOPIC_VOLUME,
)
from .entity import (
    Enigma2Entity,
    OptionalEntities,
    PluginSettingEntity,
    menu_settings_reported,
)
from .stream_facts import volume_passthrough

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the volume number, and the menu's transparency where it is reported."""
    box = entry.runtime_data
    async_add_entities([Enigma2Volume(box)])
    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            Platform.NUMBER,
            (SETTING_MENU_TRANSPARENCY,),
            lambda _key: Enigma2MenuTransparency(box),
            lambda: menu_settings_reported(box),
            # Never removed: a receiver that stops reporting the setting is an older plugin
            # or one that has not answered yet, and the entity is unavailable meanwhile.
            lambda: False,
            async_add_entities,
        ).start()
    )



class Enigma2Volume(Enigma2Entity, NumberEntity):
    """The receiver's volume, on its own scale."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the volume."""
        # `info` as well: it carries the capability the attribute below follows, and it
        # can arrive after the volume. The volume topic stays the one that has to have
        # arrived for the entity to be available.
        super().__init__(box, "volume", topics=(TOPIC_VOLUME, TOPIC_INFO))

    @callback
    def _async_read_state(self) -> None:
        """Read the level off the volume topic, and whether it acts on the audio.

        The entity stays available and settable while the receiver passes the audio on
        undecoded: the setting can change with the next track, and the level set now is
        the one that applies then. The attribute is there only for a receiver that
        claims the capability, and is None when the receiver cannot tell.
        """
        level = (self.box.state.volume or {}).get("level")
        self._attr_native_value = (
            float(level) if isinstance(level, (int, float)) else None
        )
        self._attr_extra_state_attributes = (
            {ATTR_VOLUME_PASSTHROUGH: volume_passthrough(self.box.state.volume)}
            if CAPABILITY_VOLUME_PASSTHROUGH in self.box.capabilities
            else {}
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume, and show it before the box confirms it."""
        await self.box.async_publish_cmd("volume", str(int(value)))
        self._attr_native_value = value
        self.async_write_ha_state()


def transparency(value: Any) -> int | None:
    """A reported transparency: a whole percent from 0 to 80 in tens, else None."""
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MENU_TRANSPARENCY_MIN <= value <= MENU_TRANSPARENCY_MAX
        and value % MENU_TRANSPARENCY_STEP == 0
    ):
        return value
    return None


class Enigma2MenuTransparency(PluginSettingEntity, NumberEntity):
    """How see-through the receiver menu's background is, as the receiver reports it.

    Only the background takes it; text and icons stay solid, and the menu shows it the
    next time it is opened. 0 % is opaque.
    """

    _attr_native_min_value = MENU_TRANSPARENCY_MIN
    _attr_native_max_value = MENU_TRANSPARENCY_MAX
    _attr_native_step = MENU_TRANSPARENCY_STEP
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, box: Enigma2Box) -> None:
        """Follow `info.settings.menu_transparency`."""
        super().__init__(box, SETTING_MENU_TRANSPARENCY, SETTING_MENU_TRANSPARENCY)

    @callback
    def _async_read_state(self) -> None:
        self._attr_native_value = transparency(self.reported())

    async def async_set_native_value(self, value: float) -> None:
        """Write the transparency and wait until the receiver reports it.

        The receiver refuses a value that is not a multiple of ten rather than rounding it,
        and so it is refused here, before anything is sent.
        """
        if (
            not float(value).is_integer()
            or (percent := int(value)) % MENU_TRANSPARENCY_STEP
            or not MENU_TRANSPARENCY_MIN <= percent <= MENU_TRANSPARENCY_MAX
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="menu_transparency_step",
                translation_placeholders={"value": f"{value:g}"},
            )
        await self.async_write_setting(percent)

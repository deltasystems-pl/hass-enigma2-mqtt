"""The volume, as a number.

The media player has a volume control too, on the 0-1 scale Home Assistant uses for
media players. This one is the box's own 0-100 scale, which is what a person reads off
the television and what every other tool that talks to enigma2 reports - including
OpenWebif. Having both means an automation can be written in whichever of the two the
person writing it is thinking in.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import (
    ATTR_VOLUME_PASSTHROUGH,
    CAPABILITY_VOLUME_PASSTHROUGH,
    TOPIC_INFO,
    TOPIC_VOLUME,
)
from .entity import Enigma2Entity
from .stream_facts import volume_passthrough

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the volume number."""
    async_add_entities([Enigma2Volume(entry.runtime_data)])


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

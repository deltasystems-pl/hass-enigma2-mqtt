"""The volume, as a number.

The media player has a volume control too, on the 0-1 scale Home Assistant uses for
media players. This one is the box's own 0-100 scale, which is what a person reads off
the television and what every other tool that talks to enigma2 reports — including
OpenWebif. Having both means an automation can be written in whichever of the two the
person writing it is thinking in.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import TOPIC_VOLUME
from .entity import Enigma2Entity

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
        super().__init__(box, "volume", topics=(TOPIC_VOLUME,))

    @callback
    def _async_read_state(self) -> None:
        """Read the level off the volume topic."""
        level = (self.box.state.volume or {}).get("level")
        self._attr_native_value = (
            float(level) if isinstance(level, (int, float)) else None
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume, and show it before the box confirms it."""
        await self.box.async_publish_cmd("volume", str(int(value)))
        self._attr_native_value = value
        self.async_write_ha_state()

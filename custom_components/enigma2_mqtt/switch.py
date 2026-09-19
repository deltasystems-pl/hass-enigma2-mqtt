"""Standby and mute, as switches.

Both are optimistic: the switch moves the moment it is pressed and the state topic
confirms it a moment later. That is not `assumed_state` — the box does report back, and
a switch that never heard the answer would keep showing the wrong thing, which
`assumed_state` is for. It is the opposite: the answer is coming, and the dashboard
should not sit still until it does.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import POWER_ON, TOPIC_POWER, TOPIC_VOLUME
from .entity import Enigma2Entity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches of one box."""
    box = entry.runtime_data
    async_add_entities([Enigma2PowerSwitch(box), Enigma2MuteSwitch(box)])


class Enigma2Switch(Enigma2Entity, SwitchEntity):
    """A switch that publishes a command and expects a topic to confirm it."""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._async_send(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._async_send(False)

    async def _async_send(self, on: bool) -> None:
        """Send the command, and show the result before it is confirmed."""
        raise NotImplementedError

    @callback
    def _async_assume(self, on: bool) -> None:
        """Move the switch now; the state topic will overwrite this in a moment."""
        self._attr_is_on = on
        self.async_write_ha_state()


class Enigma2PowerSwitch(Enigma2Switch):
    """Standby, as a switch."""

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the power switch."""
        super().__init__(box, "power", topics=(TOPIC_POWER,))

    @callback
    def _async_read_state(self) -> None:
        """Read standby off the power topic."""
        self._attr_is_on = self.box.state.power == POWER_ON

    async def _async_send(self, on: bool) -> None:
        """Leave or enter standby."""
        await self.box.async_publish_cmd("power", "on" if on else "standby")
        self._async_assume(on)


class Enigma2MuteSwitch(Enigma2Switch):
    """Mute, as a switch."""

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the mute switch."""
        super().__init__(box, "mute", topics=(TOPIC_VOLUME,))

    @callback
    def _async_read_state(self) -> None:
        """Read mute off the volume topic, which carries both level and mute."""
        self._attr_is_on = bool((self.box.state.volume or {}).get("muted"))

    async def _async_send(self, on: bool) -> None:
        """Mute or unmute."""
        await self.box.async_publish_cmd("mute", "ON" if on else "OFF")
        self._async_assume(on)

"""The receiver's remote control.

`remote.send_command` is the escape hatch: everything enigma2 can be told to do from
the sofa can be told to it from an automation, including the things this integration
has no entity for. The key names are the Linux input names the box publishes on `key`,
and they are accepted in either spelling — `KEY_RED` because that is what the topic
says, and `red` because that is what a person writing an automation types.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import json
from typing import Any

from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    ATTR_HOLD_SECS,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    DEFAULT_NUM_REPEATS,
    RemoteEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import (
    Enigma2Box,
    Enigma2MqttConfigEntry,
    async_send_magic_packet,
    normalise_key,
)
from .const import TOPIC_POWER
from .entity import Enigma2Entity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the remote."""
    async_add_entities([Enigma2Remote(entry.runtime_data)])


class Enigma2Remote(Enigma2Entity, RemoteEntity):
    """Send remote keys to one Enigma2 receiver."""

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the remote on the power topic, which is all it reports."""
        super().__init__(box, "remote", topics=(TOPIC_POWER,))

    @property
    def is_on(self) -> bool:
        """Return whether the box is out of standby."""
        return self.box.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Wake the box: over MQTT if it is listening, with a magic packet if not."""
        if self.box.available:
            await self.box.async_publish_cmd("power", "on")
            return
        await async_send_magic_packet(self.box)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Put the box into standby."""
        await self.box.async_publish_cmd("power", "standby")

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Send one or more keys, with a hold turning them into long presses.

        The delay between keys is the remote platform's own `delay_secs`, because a
        receiver walking a menu needs time to redraw between presses and the caller is
        the only one who knows how much.
        """
        num_repeats: int = kwargs.get(ATTR_NUM_REPEATS, DEFAULT_NUM_REPEATS)
        delay: float = kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
        hold: float = kwargs.get(ATTR_HOLD_SECS, 0)
        keys = [normalise_key(single) for single in command]

        first = True
        for _ in range(num_repeats):
            for key in keys:
                if not first:
                    await asyncio.sleep(delay)
                first = False
                await self.box.async_publish_cmd(
                    "key", json.dumps({"key": key, "long": hold > 0})
                )

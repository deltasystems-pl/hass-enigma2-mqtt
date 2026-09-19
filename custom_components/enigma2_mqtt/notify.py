"""The on-screen display, as a notification target.

`notify.send_message` puts a popup on the television. The plugin caps the text at 500
characters, and so does this — truncating here means the message a user sees is the
message they can read the start of, rather than one the box silently cut somewhere
else.
"""

from __future__ import annotations

import json

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import MESSAGE_DEFAULT_TIMEOUT, MESSAGE_MAX_LENGTH
from .entity import Enigma2Entity

PARALLEL_UPDATES = 0


def message_payload(
    text: str, message_type: str = "info", timeout: int = MESSAGE_DEFAULT_TIMEOUT
) -> str:
    """Return a `cmd/message` payload with the text capped the way the plugin caps it."""
    return json.dumps(
        {
            "text": text[:MESSAGE_MAX_LENGTH],
            "type": message_type,
            "timeout": timeout,
        }
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the on-screen display."""
    async_add_entities([Enigma2Notify(entry.runtime_data)])


class Enigma2Notify(Enigma2Entity, NotifyEntity):
    """Show a popup on the television."""

    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the display. It sends and never reads, so it has no topic."""
        super().__init__(box, "osd", requires=None)

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Put a message on the screen.

        The popup has one text field, so a title becomes its first line rather than
        being dropped: a notification whose subject vanished is worse than one with a
        colon in it.
        """
        text = f"{title}: {message}" if title else message
        await self.box.async_publish_cmd("message", message_payload(text))

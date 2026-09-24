"""The last screenshot of the television, as an image entity.

`screen` is a retained JPEG, so this entity has a picture as soon as Home Assistant
subscribes - including a picture of what was on the television the last time the plugin
captured one, which may be long before the box went into standby. It is a snapshot, not
a live view; the state carries the moment it was taken.
"""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import TOPIC_SCREEN
from .entity import Enigma2Entity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the screenshot image."""
    async_add_entities([Enigma2Screen(hass, entry.runtime_data)])


class Enigma2Screen(Enigma2Entity, ImageEntity):
    """What is on the television, as the plugin last grabbed it."""

    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, box: Enigma2Box) -> None:
        """Set up the image entity.

        `ImageEntity` takes `hass` in its constructor because it can fetch a picture
        over HTTP; this one never does - the bytes arrive on a topic - but the base
        class builds its access tokens there, so both constructors have to run.
        """
        Enigma2Entity.__init__(self, box, "screen", topics=(TOPIC_SCREEN,))
        ImageEntity.__init__(self, hass)

    @callback
    def _async_read_state(self) -> None:
        """Take the timestamp of the frame, which is this entity's whole state."""
        self._attr_image_last_updated = self.box.state.screen_updated

    async def async_image(self) -> bytes | None:
        """Return the last frame."""
        return self.box.state.screen

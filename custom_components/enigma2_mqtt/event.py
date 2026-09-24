"""Remote key presses, as an event entity.

The plugin watches the receiver's key handler and never consumes a press, so this
entity fires for every button a person pushes on the sofa - which is what makes „press
the yellow key to arm the alarm" possible without teaching enigma2 anything.

An event entity has to declare its event types up front, and the list of key names an
Enigma2 image can emit is longer than the list of keys any one remote has. A key
outside `KEY_NAMES` is therefore logged and dropped here rather than raised: the box's
key handler is not a place where an unexpected value should turn into a stack trace
every time a button is pushed. The bus event `enigma2_mqtt_key` still carries it, and
that is what the device triggers listen to.
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import ATTR_PRESS, KEY_NAMES, TOPIC_KEY
from .entity import Enigma2Entity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the key event entity."""
    async_add_entities([Enigma2KeyEvent(entry.runtime_data)])


class Enigma2KeyEvent(Enigma2Entity, EventEntity):
    """Fire whenever a key is pressed on the receiver's remote."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = list(KEY_NAMES)

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the event entity.

        It reads the `key` topic, which is not retained: there is nothing to have
        arrived before the first press, so its availability follows the box alone.
        """
        super().__init__(box, "key", topics=(TOPIC_KEY,), requires=None)

    async def async_added_to_hass(self) -> None:
        """Listen for key presses as well as for the box going up and down."""
        await super().async_added_to_hass()
        self.async_on_remove(self.box.async_add_key_listener(self._async_key_pressed))

    @callback
    def _async_key_pressed(self, key: str, press: str) -> None:
        """Fire one key press."""
        if key not in self.event_types:
            _LOGGER.debug(
                "Ignoring key %s, which is not one this entity declares", key
            )
            return
        self._trigger_event(key, {ATTR_PRESS: press})
        self.async_write_ha_state()

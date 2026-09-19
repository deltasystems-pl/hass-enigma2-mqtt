"""The base every Enigma2 MQTT entity is built on.

One box is one device and every entity hangs off it, so the identity, the naming and
the availability rule are written once here rather than twenty-six times.

Two decisions live in this file.

**The unique id is `<node_id>_<key>`** and the key is the entity's translation key. That
makes the English translation the definition of the entity id — Home Assistant derives
an object id from the entity's name in the default language, not in the user's — so the
Polish and German names can be anything a household would recognise without a single
automation or template breaking.

**An entity is unavailable when the box is offline, and also when the topic it reads
has never arrived.** The plugin only publishes the feature areas it managed to hook on
a given image, and `info.capabilities` names them; rather than reading that list and
guessing which entity it maps to, an entity that has never had a payload simply says so.
It is the same answer with no lookup table to keep in step, and it heals itself the
moment the topic appears.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .box import Enigma2Box
from .const import DOMAIN


class Enigma2Entity(Entity):
    """One entity of one Enigma2 receiver."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        box: Enigma2Box,
        key: str,
        *,
        topics: Iterable[str] = (),
        requires: str | None | bool = True,
    ) -> None:
        """Set up identity, naming and which topics move this entity.

        `topics` are the state topics the entity reads; it is woken for those and for
        availability, and nothing else. `requires` is the topic that has to have
        arrived for the entity to be available: by default the first one it reads, and
        `None` for an entity that only sends commands and therefore has no state of its
        own to wait for.
        """
        self.box = box
        self._topics = tuple(topics)
        self._requires = (
            (self._topics[0] if self._topics else None)
            if requires is True
            else (None if requires is False else requires)
        )
        self._attr_unique_id = f"{box.node_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, box.node_id)})

    @property
    def available(self) -> bool:
        """Return whether this entity has something true to say."""
        if not self.box.available:
            return False
        return self._requires is None or self._requires in self.box.seen

    async def async_added_to_hass(self) -> None:
        """Start listening to the topics this entity reads."""
        self.async_on_remove(
            self.box.async_add_listener(self._handle_box_update, self._topics or None)
        )
        self._async_read_state()

    @callback
    def _handle_box_update(self) -> None:
        """Re-read the box and publish the new state."""
        self._async_read_state()
        self.async_write_ha_state()

    @callback
    def _async_read_state(self) -> None:
        """Copy what this entity shows out of the box's state.

        Entities that are pure properties of the box override nothing; those that cache
        derived values — a parsed timestamp, a media image hash — do the work here, once
        per message, rather than on every read of every property.
        """

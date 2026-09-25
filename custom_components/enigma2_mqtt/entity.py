"""The base every Enigma2 MQTT entity is built on.

One box is one device and every entity hangs off it, so the identity, the naming and
the availability rule are written once here rather than twenty-six times.

Two decisions live in this file.

**The unique id is `<node_id>_<key>`** and the key is the entity's translation key, so it
is the same in every language. The entity id is not: Home Assistant derives the object id
from the entity's name in the installation's language when the entity is first
registered, so every translation's name is load-bearing and an existing entity's name is
never renamed in any of them.

**An entity is unavailable when the box is offline, and also when the topic it reads
has never arrived.** The plugin only publishes the feature areas it managed to hook on
a given image, and `info.capabilities` names them; rather than reading that list and
guessing which entity it maps to, an entity that has never had a payload simply says so.
It is the same answer with no lookup table to keep in step, and it heals itself the
moment the topic appears.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .box import Enigma2Box
from .const import DOMAIN, TOPIC_INFO


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
        derived values - a parsed timestamp, a media image hash - do the work here, once
        per message, rather than on every read of every property.
        """


class OptionalEntities:
    """Entities that exist only while the receiver option behind them is on.

    A box can advertise that it *can* report conditional-access or OSCam health and
    still have that reporting switched off, which is the default. Creating the entities
    anyway leaves a device page full of diagnostics that read `unknown` for ever and
    cannot be made to say anything - a fault indication where there is no fault. So
    they follow the option instead of the capability: created when it is turned on, and
    removed from the registry when it is turned off, which is what the dynamic OSCam
    source entities already do.

    The option arrives on `info`, which may land after setup, so this listens rather
    than reading once.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        box: Enigma2Box,
        platform: str,
        keys: Sequence[str],
        factory: Callable[[str], Entity],
        enabled: Callable[[], bool],
        declared: Callable[[], bool],
        add_entities: Callable[[list[Entity]], None],
    ) -> None:
        """Remember what to create, when, and how to take it away again."""
        self.hass = hass
        self.box = box
        self.platform = platform
        self.keys = tuple(keys)
        self.factory = factory
        self.enabled = enabled
        self.declared = declared
        self.add_entities = add_entities
        self.live = False

    def start(self):
        """Reconcile once, then on every `info`, and return the unsubscribe."""
        unsubscribe = self.box.async_add_listener(self._update, (TOPIC_INFO,))
        self._update()
        return unsubscribe

    @callback
    def _update(self) -> None:
        """Create or retire the whole set to match the option.

        Removing is only done on a stated "off". An `info` payload that simply does not
        mention the setting - an older plugin, or one that answered before it had read
        its own configuration - is not an answer, and treating it as one would delete
        entities the household had renamed, hidden or put on a dashboard, together with
        their history. Silence leaves everything exactly as it is.
        """
        if self.enabled():
            if not self.live:
                self.live = True
                self.add_entities([self.factory(key) for key in self.keys])
            return
        if not self.declared():
            return
        if self.live:
            # The entities were added in this session; Home Assistant removes them when
            # the entry reloads, and turning the option off reloads it.
            self.live = False
        registry = er.async_get(self.hass)
        for key in self.keys:
            unique_id = f"{self.box.node_id}_{key}"
            if entity_id := registry.async_get_entity_id(self.platform, DOMAIN, unique_id):
                registry.async_remove(entity_id)

"""The two lists a household picks from: which bouquet, and which channel in it.

In *discovery* mode the plugin publishes a channel `select` of its own. In *integration*
mode it retracts every discovery payload and this integration builds the entities — and
it had no `select` platform, so a box run the way this integration wants it run listed
neither bouquets nor channels anywhere. The topics (`channels`, `bouquet`) and the
commands (`cmd/bouquet`, `cmd/zap`) were already there; only the two controls in front
of them were missing.

**„Kanał" offers one bouquet at a time.** The receiver this was written against
publishes about a thousand channels, and a select with that many rows is not a control. It follows
the receiver's own channel-up/down context instead, which is the list the household is
already thinking in, and switching „Bukiet" reshapes it at once.

**Both select by service reference, never by name.** The plugin refuses an ambiguous
name and is right to; handing it one when the reference is in hand would be inventing
the problem. The name is what a person reads, the reference is what gets sent.

**Both wait for the receiver.** Selecting publishes and then waits for the topic that
proves it happened — `bouquet` for one, `service` for the other — so a refusal arrives
as an error in the interface rather than as a control that quietly springs back.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry, same_service, service_identity
from .const import DOMAIN, TOPIC_BOUQUET, TOPIC_CHANNELS, TOPIC_SERVICE
from .entity import Enigma2Entity, OptionalEntities
from .services import async_select_bouquet

PARALLEL_UPDATES = 0

KEY_BOUQUET = "bouquet"
KEY_CHANNEL = "channel"

# What a box has to name before either select is worth creating: the channel list to
# build the options from, and the channel-list context that says which of them is live
# and can be switched. A box that publishes one without the other can do neither job.
REQUIRED_CAPABILITIES = frozenset({"channels", "bouquet_context"})


def _unique_options(entries: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return (label, sref) pairs whose labels are unique, numbering the repeats.

    A select cannot offer the same option twice, and dropping the second „TVN HD" would
    make that channel unreachable from Home Assistant. So a repeat is numbered —
    „TVN HD", „TVN HD (2)" — while the list itself stays in the receiver's order.

    🔴 **The number follows the service reference, not the position.** Numbering in
    bouquet order would mean that moving one „TVN HD" above the other in the receiver's
    own bouquet editor silently swaps which channel „TVN HD (2)" tunes, and an
    automation that names it would keep working and do something else. Sorting the
    repeats of one name by their identity makes the label stable under a reorder. It
    cannot be stable under a duplicate being added or removed — the numbers there are
    positions in a set that changed — which is why an automation belongs on the `zap`
    action with a reference rather than on a label.

    Counting up until the label is free, rather than straight to the occurrence number,
    also covers a bouquet that really does hold a channel called „TVN HD (2)".
    """
    pairs = list(entries)
    # Names in the order the receiver first lists them, so that the rare collision
    # below is resolved the same way on every run rather than by a set's iteration
    # order, which Python randomises between processes.
    names = list(dict.fromkeys(name for name, _sref in pairs))

    labels: dict[int, str] = {}
    used: set[str] = set()
    for name in names:
        positions = [index for index, (other, _sref) in enumerate(pairs) if other == name]
        # Ties are two entries with the same reference as well as the same name, which
        # is a bouquet listing one channel twice; falling back to the position keeps
        # that deterministic.
        positions.sort(key=lambda index: (service_identity(pairs[index][1]), index))
        for rank, index in enumerate(positions):
            suffix = rank + 1
            label = name if suffix == 1 else f"{name} ({suffix})"
            while label in used:
                suffix += 1
                label = f"{name} ({suffix})"
            used.add(label)
            labels[index] = label
    return [(labels[index], sref) for index, (_name, sref) in enumerate(pairs)]


def _named_entries(items: Iterable[Any]) -> list[tuple[str, str]]:
    """Return the (name, sref) of everything in a payload that carries both."""
    entries: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        sref = item.get("sref")
        if isinstance(name, str) and name and isinstance(sref, str) and sref:
            entries.append((name, sref))
    return entries


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selects, once the box says it can answer them.

    Capabilities arrive on `info`, which can land well after setup, so this reconciles
    on every `info` rather than reading the list once. A box that has never said what it
    can do keeps whatever entities it has: only a stated capability list without
    `bouquet_context` in it takes them away.
    """
    box = entry.runtime_data

    def _create(key: str) -> Entity:
        return (
            Enigma2BouquetSelect(box)
            if key == KEY_BOUQUET
            else Enigma2ChannelSelect(box)
        )

    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            Platform.SELECT,
            (KEY_BOUQUET, KEY_CHANNEL),
            _create,
            lambda: REQUIRED_CAPABILITIES.issubset(box.capabilities),
            lambda: box.capabilities_declared,
            async_add_entities,
        ).start()
    )


class Enigma2Select(Enigma2Entity, SelectEntity):
    """A list built from the box's topics, with the reference behind every row."""

    def __init__(
        self, box: Enigma2Box, key: str, *, topics: Iterable[str], requires: str
    ) -> None:
        """Set up the list, empty until the first payload fills it."""
        super().__init__(box, key, topics=topics, requires=requires)
        self._attr_options: list[str] = []
        self._attr_current_option: str | None = None
        # Label to service reference. The label is what the household reads and what
        # Home Assistant validates against; the reference is what goes on the wire.
        self._srefs: dict[str, str] = {}

    @callback
    def _set_options(
        self, entries: Iterable[tuple[str, str]], current_sref: Any
    ) -> None:
        """Rebuild the list, and point it at whatever the box says is current.

        The current reference is matched by service identity rather than by string: the
        `service` topic and the `channels` topic do not have to spell one channel the
        same way, and a raw comparison leaves the select showing nothing for ever on a
        receiver whose reference carries a stream URL or a different case.
        """
        options = _unique_options(entries)
        self._srefs = dict(options)
        self._attr_options = [label for label, _ in options]
        self._attr_current_option = next(
            (label for label, sref in options if same_service(sref, current_sref)), None
        )

    def _sref_for(self, option: str) -> str:
        """Return the reference behind an option, or say it is no longer offered.

        Home Assistant checks an option against the list before this is reached, so the
        only way here is a list that changed between the two — the receiver republished
        its channels while somebody was choosing. Sending the stale label as a name
        anyway is how the wrong channel gets tuned.
        """
        if (sref := self._srefs.get(option)) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="option_no_longer_offered",
                translation_placeholders={"option": option},
            )
        return sref


class Enigma2BouquetSelect(Enigma2Select):
    """The receiver's channel-list context, as a list to pick from."""

    def __init__(self, box: Enigma2Box) -> None:
        """Follow the channel list and the context it names."""
        super().__init__(
            box,
            KEY_BOUQUET,
            topics=(TOPIC_CHANNELS, TOPIC_BOUQUET),
            requires=TOPIC_CHANNELS,
        )

    @callback
    def _async_read_state(self) -> None:
        """List the bouquets on offer, and mark the one the receiver is on."""
        active = self.box.active_bouquet
        self._set_options(
            _named_entries(self.box.bouquets),
            (active or {}).get("sref"),
        )

    async def async_select_option(self, option: str) -> None:
        """Make this bouquet the receiver's channel-up/down context."""
        await async_select_bouquet(self.box, self._sref_for(option))


class Enigma2ChannelSelect(Enigma2Select):
    """The active bouquet's channels, as a list to pick from."""

    def __init__(self, box: Enigma2Box) -> None:
        """Follow the channel list, the active bouquet and what is playing."""
        super().__init__(
            box,
            KEY_CHANNEL,
            topics=(TOPIC_CHANNELS, TOPIC_BOUQUET, TOPIC_SERVICE),
            requires=TOPIC_CHANNELS,
        )

    @callback
    def _async_read_state(self) -> None:
        """Reshape to the active bouquet, and mark what is playing inside it.

        Nothing is marked when the playing service is not one of these channels — the
        receiver is on the radio list, or on a bouquet nobody chose to publish. An
        invented option would be worse than an empty one: it would claim the select can
        go back to it.
        """
        active = self.box.active_bouquet
        self._set_options(
            _named_entries((active or {}).get("channels") or []),
            (self.box.state.service or {}).get("sref"),
        )

    async def async_select_option(self, option: str) -> None:
        """Tune this channel, by its service reference.

        What proves it worked is the `service` topic naming the same service — by
        identity, not by string. The receiver answers with its own spelling of the
        reference, and a raw comparison would report a zap that plainly happened as a
        timeout.
        """
        box = self.box
        sref = self._sref_for(option)
        await box.async_command(
            "zap",
            sref,
            effect=lambda: same_service((box.state.service or {}).get("sref"), sref),
        )

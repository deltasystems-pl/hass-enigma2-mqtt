"""The channel list from one topic per bouquet, and from `channels`, through one model.

A plugin after 0.4.0 publishes the receiver's channel list twice: on `channels`, every
bouquet in one payload, and on `bouquets` plus one `channels/<slug>` per bouquet. On a
receiver with a very large list the first is too big for one MQTT packet and is not
published at all, so the second is the only one there is.

What these tests hold the integration to:

- every reader - the selects, the media player's sources, the browser, the actions' name
  resolution, the options form, the zap-history filter - gives the same answer from
  either source;
- retained topics arrive one by one in no order, and neither a half-built list nor a
  rewrite per message ever reaches an entity;
- the zap-history filter fails toward hiding in every state in between;
- a receiver that does not claim `channel_topics` is read exactly as before.

Every test that turns on arrival order uses `async_setup_box_then_retained`: the entities
exist first and the topics land on them, which is the order a broker produces.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
import gc
import itertools
import json
import logging
from typing import Any
from unittest.mock import patch

from homeassistant.components import mqtt
from homeassistant.components.media_player import DATA_COMPONENT
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.enigma2_mqtt import box as box_module
from custom_components.enigma2_mqtt.box import Enigma2Box
from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    AVAILABILITY_TOPIC,
    BASE_TOPIC,
    BOUQUET,
    BOUQUET_TOPIC,
    CHANNELS,
    CHANNELS_TOPIC,
    EPG_GRID,
    EPG_GRID_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    POWER_TOPIC,
    SERVICE,
    SERVICE_TOPIC,
    SLUG,
    SREF,
    SREF_TWO,
    async_setup_box_then_retained,
)

# Spelled out rather than imported, so this module collects against a tree without the
# feature and every test can be shown red.
CAPABILITY = "channel_topics"
BOUQUETS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/bouquets"
ZAP_HISTORY_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/zap_history"
HIDDEN_OPTION = "history_hidden_bouquets"
SETTLE = 5.0

BOUQUET_SELECT = f"select.{SLUG}_bouquet"
CHANNEL_SELECT = f"select.{SLUG}_channel"
HISTORY_SELECT = f"select.{SLUG}_recently_watched"
PLAYER = f"media_player.{SLUG}"
EPG_SENSOR = f"sensor.{SLUG}_epg_active_bouquet"
ENTITIES = (BOUQUET_SELECT, CHANNEL_SELECT, HISTORY_SELECT, PLAYER)

ULUBIONE = CHANNELS["bouquets"][0]
SPORT = CHANNELS["bouquets"][1]
EUROSPORT = SPORT["channels"][0]["sref"]
TVN_IN_SPORT = SPORT["channels"][1]["sref"]
SLUGS = {"Ulubione TV": "ulubione_tv", "Sport": "sport"}


def list_topic(slug: str) -> str:
    return f"{BASE_TOPIC}/{NODE_ID}/channels/{slug}"


def index_of(channels: dict[str, Any], slugs: dict[str, str] | None = None) -> str:
    """Return the `bouquets` index a plugin publishes beside this `channels` payload."""
    slugs = SLUGS if slugs is None else slugs
    return json.dumps(
        {
            "generated": channels["generated"],
            "bouquets": [
                {
                    "name": bouquet["name"],
                    "sref": bouquet["sref"],
                    "slug": slugs[bouquet["name"]],
                    "count": len(bouquet["channels"]),
                }
                for bouquet in channels["bouquets"]
            ],
        }
    )


def list_of(bouquet: dict[str, Any], generated: int = 1789459200) -> str:
    """Return one bouquet's `channels/<slug>` payload."""
    return json.dumps(
        {
            "bouquet": bouquet["name"],
            "sref": bouquet["sref"],
            "generated": generated,
            "channels": bouquet["channels"],
        }
    )


def info(*, claims: bool, not_published: Any = ...) -> str:
    """Return an `info` of a receiver with the selects and the zap history."""
    capabilities = [*INFO["capabilities"], "bouquet_context", "zap_history"]
    if claims:
        capabilities.append(CAPABILITY)
    payload: dict[str, Any] = {**INFO, "capabilities": capabilities}
    if not_published is not ...:
        payload["not_published"] = not_published
    return json.dumps(payload)


def _history_entry(sref: str, name: str, bouquet: dict[str, Any]) -> dict[str, Any]:
    return {"sref": sref, "name": name, "bouquet": bouquet["sref"], "bouquet_name": bouquet["name"]}


# Newest first: a channel of Ulubione, a channel of Sport, and a channel of Sport that was
# reached through Ulubione's path - which is still a member of Sport.
HISTORY = json.dumps(
    {
        "entries": [
            _history_entry(SREF, "TVP 1 HD", ULUBIONE),
            _history_entry(EUROSPORT, "Eurosport 1", SPORT),
            _history_entry(TVN_IN_SPORT, "TVN HD (Sport)", ULUBIONE),
        ],
        "current": 0,
        "limit": 20,
        "panic_button": True,
    }
)
EVERY_HISTORY_LABEL = ["TVP 1 HD", "Eurosport 1", "TVN HD (Sport)"]


def base(store: dict[str, str | bytes]) -> dict[str, str | bytes]:
    """Reduce the store to a receiver that is on and has said nothing about its channels.

    The announcement carries no capability list, so `info` alone says what the receiver
    can do - and a test decides when it arrives.
    """
    store.clear()
    store.update(
        {
            ANNOUNCEMENT_TOPIC: json.dumps(
                {key: value for key, value in ANNOUNCEMENT.items() if key != "capabilities"}
            ),
            AVAILABILITY_TOPIC: "online",
            POWER_TOPIC: "on",
            SERVICE_TOPIC: json.dumps(SERVICE),
            BOUQUET_TOPIC: json.dumps(BOUQUET),
            ZAP_HISTORY_TOPIC: HISTORY,
        }
    )
    return store


def per_bouquet(channels: dict[str, Any] = CHANNELS) -> dict[str, str]:
    """Return the index and every bouquet's own topic for a channel list."""
    topics = {BOUQUETS_TOPIC: index_of(channels)}
    for bouquet in channels["bouquets"]:
        topics[list_topic(SLUGS[bouquet["name"]])] = list_of(bouquet)
    return topics


def hiding(config_entry: MockConfigEntry, *names: str) -> MockConfigEntry:
    """Return the example entry with bouquets hidden from the zap history."""
    return MockConfigEntry(
        domain=config_entry.domain,
        title=config_entry.title,
        unique_id=config_entry.unique_id,
        data=dict(config_entry.data),
        options={HIDDEN_OPTION: list(names)},
    )


async def publish(hass: HomeAssistant, topic: str, payload: str | bytes) -> None:
    """Publish as a connected receiver does: a broker delivers that unretained."""
    async_fire_mqtt_message(hass, topic, payload)
    await hass.async_block_till_done()


async def deliver(hass: HomeAssistant, topic: str, payload: str | bytes) -> None:
    """Deliver one retained message, as the broker does after a subscribe."""
    async_fire_mqtt_message(hass, topic, payload, retain=True)
    await hass.async_block_till_done()


async def settle(hass: HomeAssistant) -> None:
    """Let the burst be over: no per-bouquet message for longer than the settle time."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SETTLE + 1))
    await hass.async_block_till_done()


def options(hass: HomeAssistant, entity_id: str) -> list[str] | None:
    state = hass.states.get(entity_id)
    return None if state is None else state.attributes.get("options")


def watch(hass: HomeAssistant) -> list[tuple[str, str, Any]]:
    """Record every state change of the entities that read the channel list.

    Each as (entity id, state, the list it shows): the select's options or the media
    player's sources.
    """
    seen: list[tuple[str, str, Any]] = []

    @callback
    def _changed(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None or new.entity_id not in ENTITIES:
            return
        shown = new.attributes.get("source_list" if new.entity_id == PLAYER else "options")
        seen.append((new.entity_id, new.state, None if shown is None else list(shown)))

    hass.bus.async_listen(EVENT_STATE_CHANGED, _changed)
    return seen


async def readers(hass: HomeAssistant, box: Enigma2Box) -> dict[str, Any]:
    """Return what every reader of the channel list says, in one comparable value."""
    player = hass.data[DATA_COMPONENT].get_entity(PLAYER)
    root = await player.async_browse_media()
    sport = await player.async_browse_media("bouquet", f"bouquet:{SPORT['sref']}")
    return {
        "bouquet_select": (options(hass, BOUQUET_SELECT), hass.states.get(BOUQUET_SELECT).state),
        "channel_select": (options(hass, CHANNEL_SELECT), hass.states.get(CHANNEL_SELECT).state),
        "history_select": (options(hass, HISTORY_SELECT), hass.states.get(HISTORY_SELECT).state),
        "sources": hass.states.get(PLAYER).attributes.get("source_list"),
        "browse_root": [(child.title, child.media_content_id) for child in root.children],
        "browse_sport": [(child.title, child.media_content_id) for child in sport.children],
        "bouquet_names": box.bouquet_names,
        "published": [
            (bouquet["name"], bouquet["sref"], bouquet["channels"])
            for bouquet in box.published_bouquets
        ],
        "offered": [bouquet["name"] for bouquet in box.bouquets],
        "channel_names": box.channel_names,
        "named_tvn": [channel["sref"] for channel in box.channels_named("TVN HD")],
        "by_sref": (box.bouquet_by_sref(SPORT["sref"]) or {}).get("name"),
        "name_of": box.published_channel_name(EUROSPORT.lower()),
        "active": (box.active_bouquet or {}).get("name"),
        "history_filtered": [
            entry["name"] for entry in box.zap_history_entries(filtered=True)
        ],
        "history_all": [entry["name"] for entry in box.zap_history_entries(filtered=False)],
    }


# What every reader says about the example receiver, whichever topics carried its list.
EXPECTED: dict[str, Any] = {
    "bouquet_select": (["Ulubione TV", "Sport"], "Ulubione TV"),
    "channel_select": (["TVP 1 HD", "TVN HD"], "TVP 1 HD"),
    "history_select": (["TVP 1 HD"], "TVP 1 HD"),
    "sources": ["TVP 1 HD", "TVN HD", "Eurosport 1"],
    "browse_root": [
        ("Ulubione TV", f"bouquet:{ULUBIONE['sref']}"),
        ("Sport", f"bouquet:{SPORT['sref']}"),
    ],
    "browse_sport": [("Eurosport 1", EUROSPORT), ("TVN HD", TVN_IN_SPORT)],
    "bouquet_names": ["Ulubione TV", "Sport"],
    "published": [
        ("Ulubione TV", ULUBIONE["sref"], ULUBIONE["channels"]),
        ("Sport", SPORT["sref"], SPORT["channels"]),
    ],
    "offered": ["Ulubione TV", "Sport"],
    "channel_names": ["TVP 1 HD", "TVN HD", "Eurosport 1"],
    "named_tvn": [SREF_TWO, TVN_IN_SPORT],
    "by_sref": "Sport",
    "name_of": "Eurosport 1",
    "active": "Ulubione TV",
    "history_filtered": ["TVP 1 HD"],
    "history_all": EVERY_HISTORY_LABEL,
}
FULL = {
    BOUQUET_SELECT: EXPECTED["bouquet_select"][0],
    CHANNEL_SELECT: EXPECTED["channel_select"][0],
    HISTORY_SELECT: EXPECTED["history_select"][0],
    PLAYER: EXPECTED["sources"],
}


# ------------------------------------------------- one model, whichever the source

SOURCES = {
    # A plugin up to 0.4.0: `channels` and nothing else.
    "channels": (False, True, False, "channels"),
    # A receiver whose `channels` is too big to send: only the per-bouquet topics.
    "bouquet_topics": (True, False, True, "bouquet_topics"),
    # A receiver whose `channels` fits: both are published, and the per-bouquet ones win.
    "both": (True, True, True, "bouquet_topics"),
    # The capability is claimed and the index never arrives.
    "claimed_without_index": (True, True, False, "channels"),
}


@pytest.mark.parametrize("source", list(SOURCES))
async def test_every_reader_says_the_same_from_either_source(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    source: str,
) -> None:
    claims, combined, topics, read_from = SOURCES[source]
    store = base(retained)
    store[INFO_TOPIC] = info(
        claims=claims,
        **({} if combined or not claims else {"not_published": [{"topic": "channels"}]}),
    )
    if combined:
        store[CHANNELS_TOPIC] = json.dumps(CHANNELS)
    if topics:
        store.update(per_bouquet())
    entry = hiding(config_entry, "Sport")
    await async_setup_box_then_retained(hass, entry, store)
    box: Enigma2Box = entry.runtime_data

    assert await readers(hass, box) == EXPECTED
    assert box.channel_list.source == read_from
    assert box.channel_list.complete is True


async def test_where_the_two_disagree_the_per_bouquet_topics_win_while_claimed(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """And `channels` wins on a receiver that does not claim them, whatever lingers."""
    other = {
        "generated": 1,
        "bouquets": [{**ULUBIONE, "channels": [{"sref": EUROSPORT, "name": "Only in channels"}]}],
    }
    store = base(retained)
    store[INFO_TOPIC] = info(claims=True)
    store[CHANNELS_TOPIC] = json.dumps(other)
    store.update(per_bouquet())
    await async_setup_box_then_retained(hass, config_entry, store)
    box: Enigma2Box = config_entry.runtime_data

    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]
    assert options(hass, CHANNEL_SELECT) == ["TVP 1 HD", "TVN HD"]

    await publish(hass, CHANNELS_TOPIC, json.dumps(other))

    assert options(hass, CHANNEL_SELECT) == ["TVP 1 HD", "TVN HD"]
    assert box.channels_named("Only in channels") == []


async def test_a_receiver_that_does_not_claim_them_never_reads_the_new_topics(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Left behind by a newer plugin, or published by anybody: not this receiver's word."""
    lingering = {
        "generated": 5,
        "bouquets": [
            {**ULUBIONE, "channels": [{"sref": EUROSPORT, "name": "Lingering"}]},
            {"name": "Gone", "sref": "1:7:1:0:0:0:0:0:0:0:gone", "channels": []},
        ],
    }
    store = base(retained)
    store[INFO_TOPIC] = info(claims=False)
    store[CHANNELS_TOPIC] = json.dumps(CHANNELS)
    await async_setup_box_then_retained(hass, hiding(config_entry, "Sport"), store)
    box: Enigma2Box = hass.config_entries.async_entries("enigma2_mqtt")[0].runtime_data
    before = await readers(hass, box)
    changes = watch(hass)
    held = box.state.channels

    await deliver(hass, BOUQUETS_TOPIC, index_of(lingering, {"Ulubione TV": "u", "Gone": "g"}))
    await deliver(hass, list_topic("u"), list_of(lingering["bouquets"][0]))
    await deliver(hass, list_topic("g"), list_of(lingering["bouquets"][1]))
    await settle(hass)

    assert before == EXPECTED
    assert await readers(hass, box) == EXPECTED
    assert changes == []
    assert box.channel_list.source == "channels"
    assert box.state.channels is held, "the payload itself, as it always was"
    assert box.updates["channels"] == 1


# ------------------------------------------------------------------ arrival order

MESSAGES: dict[str, tuple[str, str]] = {
    "info": (INFO_TOPIC, info(claims=True)),
    "index": (BOUQUETS_TOPIC, index_of(CHANNELS)),
    "ulubione": (list_topic("ulubione_tv"), list_of(ULUBIONE)),
    "sport": (list_topic("sport"), list_of(SPORT)),
    "channels": (CHANNELS_TOPIC, json.dumps(CHANNELS)),
}
WITHOUT_CHANNELS = ("info", "index", "ulubione", "sport")


def _never_partial(changes: list[tuple[str, str, Any]]) -> None:
    """Every list an entity ever showed was empty or whole, and it was written whole once."""
    for entity_id in ENTITIES:
        shown = [lists for changed, _state, lists in changes if changed == entity_id]
        assert shown, f"{entity_id} never got a state"
        assert all(lists in (None, [], FULL[entity_id]) for lists in shown), (entity_id, shown)
        assert shown.count(FULL[entity_id]) == 1, (entity_id, shown)
        assert shown[-1] == FULL[entity_id]


@pytest.mark.parametrize("announced", [False, True], ids=["info_declares", "announced"])
@pytest.mark.parametrize(
    "order", list(itertools.permutations(WITHOUT_CHANNELS)), ids="-".join
)
async def test_every_arrival_order_ends_whole_and_is_never_shown_in_part(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    order: tuple[str, ...],
    announced: bool,
) -> None:
    """`channels` is withheld; the index, the two lists and `info` land in every order.

    With `announced`, the announcement already names the capabilities, as a real one
    does, so the per-bouquet topics count from the first message.
    """
    store = base(retained)
    if announced:
        store[ANNOUNCEMENT_TOPIC] = json.dumps(
            {**ANNOUNCEMENT, "capabilities": json.loads(info(claims=True))["capabilities"]}
        )
    entry = hiding(config_entry, "Sport")
    changes = watch(hass)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt")
    await async_setup_box_then_retained(hass, entry, store)
    box: Enigma2Box = entry.runtime_data
    caplog.clear()

    for name in order:
        await deliver(hass, *MESSAGES[name])
        # The filter never lets a channel of the hidden bouquet through, whatever is in.
        assert set(options(hass, HISTORY_SELECT) or []) <= {"TVP 1 HD"}, (name, order)

    assert await readers(hass, box) == EXPECTED
    assert box.channel_list.source == "bouquet_topics"
    _never_partial(changes)
    assert box.updates["channels"] == 1, "one publication of the list, not one per topic"
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.enigma2_mqtt")
    ] == []


@pytest.mark.parametrize("order", list(itertools.permutations(MESSAGES)), ids="-".join)
async def test_every_arrival_order_with_channels_published_too(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    order: tuple[str, ...],
) -> None:
    """Both ways of publishing, in every order: whole at the end, never in part, held once."""
    store = base(retained)
    entry = hiding(config_entry, "Sport")
    changes = watch(hass)
    await async_setup_box_then_retained(hass, entry, store)
    box: Enigma2Box = entry.runtime_data

    for name in order:
        await deliver(hass, *MESSAGES[name])
        assert set(options(hass, HISTORY_SELECT) or []) <= {"TVP 1 HD"}, (name, order)

    assert await readers(hass, box) == EXPECTED
    assert box.channel_list.source == "bouquet_topics"
    _never_partial(changes)
    # The combined payload is not kept beside the per-bouquet ones.
    assert box.state.channels is None
    assert box.state.channels_released is True


# ----------------------------------------------------- the states in between


async def _trickle(
    hass: HomeAssistant,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    *names: str,
    hidden: str = "Sport",
    not_published: Any = ...,
) -> tuple[Enigma2Box, list[tuple[str, str, Any]]]:
    """Set up a receiver that claims the topics, and deliver only the named messages."""
    store = base(retained)
    store[INFO_TOPIC] = info(claims=True, not_published=not_published)
    entry = hiding(config_entry, hidden)
    await async_setup_box_then_retained(hass, entry, store)
    changes = watch(hass)
    for name in names:
        await deliver(hass, *MESSAGES[name])
    return entry.runtime_data, changes


async def test_before_the_index_nothing_is_offered_and_everything_is_hidden(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The capability is claimed and nothing has arrived: the state before `channels` did."""
    box, _changes = await _trickle(hass, retained, config_entry)
    await settle(hass)

    assert box.channel_list.source == "none"
    assert hass.states.get(BOUQUET_SELECT).state == STATE_UNAVAILABLE
    assert hass.states.get(CHANNEL_SELECT).state == STATE_UNAVAILABLE
    assert hass.states.get(PLAYER).attributes.get("source_list") is None
    assert options(hass, HISTORY_SELECT) == []
    assert box.bouquet_names == []


@pytest.mark.parametrize(
    ("arrived", "filtered_after"),
    [
        # The index alone: both lists are missing, the hidden bouquet's among them.
        (("index",), []),
        # The hidden bouquet's list is missing, so which channels to hide is not known.
        (("index", "ulubione"), []),
        # The hidden bouquet's list is in; the other bouquet's is not, and is not needed
        # to tell that a channel is none of the hidden ones.
        (("index", "sport"), ["TVP 1 HD"]),
    ],
)
async def test_an_incomplete_list_is_held_back_and_then_shown_as_far_as_it_goes(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    arrived: tuple[str, ...],
    filtered_after: list[str],
) -> None:
    """Nothing is shown while the burst may still be running; then what there is.

    A bouquet whose list never came stays a bouquet, with no channels. And the filter
    hides everything for as long as a hidden bouquet's list is not here.
    """
    box, changes = await _trickle(hass, retained, config_entry, *arrived)

    assert box.channel_list.source == "none"
    assert changes == []
    assert options(hass, HISTORY_SELECT) == []
    assert hass.states.get(BOUQUET_SELECT).state == STATE_UNAVAILABLE

    await settle(hass)

    listing = box.channel_list
    assert listing.source == "bouquet_topics"
    assert listing.complete is False
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]
    assert box.bouquet_names == ["Ulubione TV", "Sport"]
    assert options(hass, CHANNEL_SELECT) == (
        ["TVP 1 HD", "TVN HD"] if "ulubione" in arrived else []
    )
    assert [bouquet["listed"] for bouquet in listing.bouquets] == [
        "ulubione" in arrived,
        "sport" in arrived,
    ]
    assert options(hass, HISTORY_SELECT) == filtered_after
    assert [entry["name"] for entry in box.zap_history_entries(filtered=False)] == (
        EVERY_HISTORY_LABEL
    )

    # The rest arrives: whole, at once.
    for name in ("ulubione", "sport"):
        if name not in arrived:
            await deliver(hass, *MESSAGES[name])

    assert box.channel_list.complete is True
    assert await readers(hass, box) == EXPECTED


async def test_channels_is_what_is_shown_while_the_lists_are_still_arriving(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Published too, and here before the last bouquet's topic: it is not thrown away.

    Nobody waits out the settle time for a list that is in hand; and when the last
    topic lands, the per-bouquet lists take over without anything an entity shows moving.
    """
    box, changes = await _trickle(hass, retained, config_entry, "index", "ulubione")
    assert box.channel_list.source == "none"

    await deliver(hass, *MESSAGES["channels"])

    assert box.channel_list.source == "channels"
    assert box.state.channels == CHANNELS
    assert await readers(hass, box) == EXPECTED

    del changes[:]
    await deliver(hass, *MESSAGES["sport"])

    assert box.channel_list.source == "bouquet_topics"
    assert box.state.channels is None
    assert await readers(hass, box) == EXPECTED
    assert changes == []


async def test_a_withheld_hidden_bouquet_stays_listed_and_hides_everything(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The receiver says the hidden bouquet's list will not come: nothing waits for it.

    The bouquet is still on the list of bouquets, by its name, with no channels. Its
    channels cannot be told from anybody else's, so the filtered history shows none.
    """
    box, changes = await _trickle(
        hass,
        retained,
        config_entry,
        "index",
        "ulubione",
        not_published=[{"topic": "channels/sport", "bytes": 1000001, "limit": 1000000}],
    )

    # Complete as it stands - no settle time is waited out for a topic that is withheld.
    assert box.channel_list.source == "bouquet_topics"
    assert box.channel_list.complete is True
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]
    assert options(hass, CHANNEL_SELECT) == ["TVP 1 HD", "TVN HD"]
    assert hass.states.get(PLAYER).attributes["source_list"] == ["TVP 1 HD", "TVN HD"]
    assert box.bouquet_by_sref(SPORT["sref"])["channels"] == []
    assert box.bouquet_by_sref(SPORT["sref"])["count"] == 2
    assert options(hass, HISTORY_SELECT) == []
    assert hass.states.get(HISTORY_SELECT).state == STATE_UNKNOWN
    assert [lists for entity_id, _state, lists in changes if entity_id == HISTORY_SELECT] in (
        [],
        [[]],
    )


async def test_a_withheld_bouquet_that_is_not_hidden_costs_only_its_own_channels(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box, _changes = await _trickle(
        hass,
        retained,
        config_entry,
        "index",
        "sport",
        not_published=[{"topic": "channels/ulubione_tv", "bytes": 1000001, "limit": 1000000}],
    )

    assert box.channel_list.complete is True
    assert options(hass, CHANNEL_SELECT) == []
    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD"]


async def test_the_withheld_list_named_after_the_topics_completes_the_list_at_once(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`info` is published again when a topic joins the list, after the rest arrived."""
    box, changes = await _trickle(hass, retained, config_entry, "index", "ulubione")
    assert box.channel_list.source == "none"

    await publish(
        hass,
        INFO_TOPIC,
        info(claims=True, not_published=[{"topic": "channels/sport", "bytes": 1, "limit": 1}]),
    )

    assert box.channel_list.complete is True
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]
    assert options(hass, HISTORY_SELECT) == []

    # And when the bouquet fits again: the topic arrives and the entry goes, in any order.
    await publish(hass, *MESSAGES["sport"])
    listing = box.channel_list
    counted = box.updates["channels"]
    await publish(hass, INFO_TOPIC, info(claims=True, not_published=[]))

    assert await readers(hass, box) == EXPECTED
    # The entry going changed nothing about the list, so nobody was woken for it.
    assert box.channel_list is listing
    assert box.updates["channels"] == counted
    assert [lists for entity_id, _s, lists in changes if entity_id == PLAYER] == [
        ["TVP 1 HD", "TVN HD"],
        EXPECTED["sources"],
    ]


async def test_a_list_from_the_wrong_bouquet_is_not_that_bouquets_list(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Two bouquets that slug alike swap topics when the receiver's order changes.

    Between the new index and the two payloads, the topic the index gives the hidden
    bouquet still holds the other one's list. Read as the hidden bouquet's, it would
    show the hidden bouquet's real channels in the filtered history.
    """
    box, _changes = await _trickle(hass, retained, config_entry, "index", "ulubione")
    # `channels/sport` carries Ulubione's list: it says so in `sref`.
    await deliver(hass, list_topic("sport"), list_of(ULUBIONE))
    await settle(hass)

    assert [bouquet["listed"] for bouquet in box.channel_list.bouquets] == [True, False]
    assert box.bouquet_by_sref(SPORT["sref"])["channels"] == []
    assert options(hass, HISTORY_SELECT) == []

    await publish(hass, list_topic("sport"), list_of(SPORT))

    assert await readers(hass, box) == EXPECTED


async def test_a_hidden_name_that_is_on_no_list_is_said_once_the_list_is_here(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """As with `channels`: nothing before a list has arrived, one line once it has."""
    caplog.set_level(logging.WARNING)
    box, _changes = await _trickle(hass, retained, config_entry, hidden="Renamed")
    await settle(hass)
    assert "is hidden from the zap history" not in caplog.text

    for name in ("index", "ulubione", "sport"):
        await deliver(hass, *MESSAGES[name])
    await publish(hass, SERVICE_TOPIC, json.dumps({**SERVICE, "name": "moved"}))

    assert caplog.text.count("bouquet Renamed is hidden from the zap history") == 1
    assert box.channel_list.source == "bouquet_topics"


# ---------------------------------------------------------------- no churn

MANY = {
    "generated": 1789459200,
    "bouquets": [
        {
            "name": f"Bukiet {number:02}",
            "sref": f'1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.b{number:02}.tv"',
            "channels": [
                {
                    "sref": f"1:0:19:{number:X}{channel:X}:3F3:1:C00000:0:0:0:",
                    "name": f"K {number}.{channel}",
                }
                for channel in range(1, 4)
            ],
        }
        for number in range(12)
    ],
}
MANY_SLUGS = {
    bouquet["name"]: f"bukiet_{number:02}" for number, bouquet in enumerate(MANY["bouquets"])
}


async def test_twelve_bouquets_trickling_in_move_each_entity_once(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Thirteen messages, one change of each entity; a replay of all of them, none.

    Counted on the state machine, not read off the end state: a list rebuilt per message
    ends exactly where one rebuilt once does.
    """
    store = base(retained)
    store[INFO_TOPIC] = info(claims=True)
    store[BOUQUET_TOPIC] = json.dumps(
        {"name": MANY["bouquets"][0]["name"], "sref": MANY["bouquets"][0]["sref"]}
    )
    await async_setup_box_then_retained(hass, config_entry, store)
    box: Enigma2Box = config_entry.runtime_data
    changes = watch(hass)
    caplog.set_level(logging.DEBUG, logger="custom_components.enigma2_mqtt")
    caplog.clear()
    burst = [(BOUQUETS_TOPIC, index_of(MANY, MANY_SLUGS))] + [
        (list_topic(MANY_SLUGS[bouquet["name"]]), list_of(bouquet)) for bouquet in MANY["bouquets"]
    ]

    for topic, payload in burst[:-1]:
        await deliver(hass, topic, payload)

    assert changes == []
    assert "channels" not in box.updates

    await deliver(hass, *burst[-1])

    every_channel = [
        channel["name"] for bouquet in MANY["bouquets"] for channel in bouquet["channels"]
    ]
    assert [entry for entry in changes if entry[0] == BOUQUET_SELECT] == [
        (BOUQUET_SELECT, "Bukiet 00", [bouquet["name"] for bouquet in MANY["bouquets"]])
    ]
    assert [entry for entry in changes if entry[0] == CHANNEL_SELECT] == [
        (CHANNEL_SELECT, STATE_UNKNOWN, ["K 0.1", "K 0.2", "K 0.3"])
    ]
    assert [lists for entity_id, _state, lists in changes if entity_id == PLAYER] == [every_channel]
    assert box.updates["channels"] == 1
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.enigma2_mqtt.box"
    ] == []

    # A reconnect: the receiver sends every topic again, as it was.
    del changes[:]
    counted = dict(box.updates)
    for topic, payload in burst:
        await publish(hass, topic, payload)
    await settle(hass)

    assert changes == []
    assert box.updates == counted

    # One bouquet is edited on the receiver: its list, and only what shows it, moves.
    edited = {**MANY["bouquets"][0], "channels": MANY["bouquets"][0]["channels"][:2]}
    await publish(hass, list_topic("bukiet_00"), list_of(edited, generated=1789459999))

    assert [entry for entry in changes if entry[0] == CHANNEL_SELECT] == [
        (CHANNEL_SELECT, STATE_UNKNOWN, ["K 0.1", "K 0.2"])
    ]
    assert [entry for entry in changes if entry[0] == BOUQUET_SELECT] == []
    assert box.updates["channels"] == 2


async def test_an_info_that_changes_nothing_about_the_topics_rebuilds_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`info` is published again and again; the list is rebuilt when its inputs move."""
    store = base(retained)
    store[INFO_TOPIC] = info(claims=True, not_published=[])
    store.update(per_bouquet())
    await async_setup_box_then_retained(hass, config_entry, store)
    box: Enigma2Box = config_entry.runtime_data
    listing = box.channel_list
    counted = box.updates["channels"]
    built: list[int] = []
    build = Enigma2Box._async_show_topic_list

    def _counting(self: Enigma2Box, **kwargs: Any) -> None:
        built.append(1)
        build(self, **kwargs)

    monkeypatch.setattr(Enigma2Box, "_async_show_topic_list", _counting)

    for uptime in (1, 2, 3):
        payload = {**json.loads(info(claims=True, not_published=[])), "uptime": uptime}
        await publish(hass, INFO_TOPIC, json.dumps(payload))
    # A grid withheld is news for the repair and none for the channel list.
    await publish(
        hass,
        INFO_TOPIC,
        info(claims=True, not_published=[{"topic": "epg_grid/sport", "bytes": 1, "limit": 1}]),
    )

    assert box.channel_list is listing
    assert box.updates["channels"] == counted
    assert built == [], "not even built to be compared and thrown away"

    # What does move the list is still acted on.
    await publish(
        hass,
        INFO_TOPIC,
        info(claims=True, not_published=[{"topic": "channels/sport", "bytes": 1, "limit": 1}]),
    )

    assert built == [1]


async def test_the_members_of_a_hidden_bouquet_are_worked_out_once_per_list(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter runs on every payload of three topics; a hidden bouquet can be thousands."""
    store = base(retained)
    store[INFO_TOPIC] = info(claims=True)
    store.update(per_bouquet())
    entry = hiding(config_entry, "Sport")
    await async_setup_box_then_retained(hass, entry, store)
    box: Enigma2Box = entry.runtime_data
    real = box_module.service_identity
    asked: list[Any] = []

    def _counting(sref: Any) -> str:
        asked.append(sref)
        return real(sref)

    monkeypatch.setattr(box_module, "service_identity", _counting)
    # A member of the hidden bouquet that is in nobody's history: only building the set
    # of hidden members ever asks for its identity.
    quiet = {"sref": "1:0:19:9999:3F3:1:C00000:0:0:0:", "name": "Quiet"}
    await publish(
        hass, list_topic("sport"), list_of({**SPORT, "channels": [*SPORT["channels"], quiet]})
    )
    assert asked.count(quiet["sref"]) == 1

    for _ in range(3):
        await publish(hass, SERVICE_TOPIC, json.dumps({**SERVICE, "name": "moved"}))
        await publish(hass, SERVICE_TOPIC, json.dumps(SERVICE))
        assert box.zap_history_entries(filtered=True)

    assert asked.count(quiet["sref"]) == 1

    await publish(hass, list_topic("sport"), list_of(SPORT))

    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD"]


async def test_the_lists_are_held_once(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The model reads the payloads' own lists, and lets the combined payload go."""
    store = base(retained)
    store[INFO_TOPIC] = info(claims=True)
    store[CHANNELS_TOPIC] = json.dumps(CHANNELS)
    store.update(per_bouquet())
    await async_setup_box_then_retained(hass, config_entry, store)
    box: Enigma2Box = config_entry.runtime_data

    assert box.state.channels is None
    for bouquet in box.published_bouquets:
        assert bouquet["channels"] is box.state.bouquet_channels[bouquet["slug"]]["channels"]

    # Published again by the receiver: still not kept.
    await publish(hass, CHANNELS_TOPIC, json.dumps(CHANNELS))

    assert box.state.channels is None
    assert box.state.channels_released is True


async def test_nothing_keeps_the_combined_payload_alive(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Counted in the interpreter, not read off an attribute: one copy of a channel.

    Both ways of publishing carry a channel with a name nothing else has. Whatever order
    they arrive in, one parsed copy of it is alive once the per-bouquet list is on show.
    """
    marker = {"sref": "1:0:19:ABCD:3F3:1:C00000:0:0:0:", "name": "Only one of me, please"}
    sport = {**SPORT, "channels": [*SPORT["channels"], marker]}
    channels = {**CHANNELS, "bouquets": [ULUBIONE, sport]}

    def alive() -> int:
        gc.collect()
        return sum(
            1 for found in gc.get_objects() if isinstance(found, dict) and found == marker
        ) - 1  # `marker` itself

    store = base(retained)
    store[INFO_TOPIC] = info(claims=True)
    await async_setup_box_then_retained(hass, config_entry, store)
    box: Enigma2Box = config_entry.runtime_data

    # `channels` first: it is the list on show, and its payload is the one copy.
    await deliver(hass, CHANNELS_TOPIC, json.dumps(channels))
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]
    assert alive() == 1

    await deliver(hass, BOUQUETS_TOPIC, index_of(channels))
    await deliver(hass, list_topic("ulubione_tv"), list_of(ULUBIONE))
    await deliver(hass, list_topic("sport"), list_of(sport))

    assert box.channel_list.source == "bouquet_topics"
    assert box.channels_named(marker["name"]) == [marker]
    assert alive() == 1

    await publish(hass, CHANNELS_TOPIC, json.dumps(channels))

    assert alive() == 1


# ------------------------------------------------- downgrade and retraction


async def _complete(
    hass: HomeAssistant,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    *,
    combined: bool,
) -> tuple[Enigma2Box, list[tuple[str, str, Any]]]:
    store = base(retained)
    store[INFO_TOPIC] = info(claims=True)
    if combined:
        store[CHANNELS_TOPIC] = json.dumps(CHANNELS)
    store.update(per_bouquet())
    entry = hiding(config_entry, "Sport")
    await async_setup_box_then_retained(hass, entry, store)
    box: Enigma2Box = entry.runtime_data
    assert await readers(hass, box) == EXPECTED
    return box, watch(hass)


async def test_a_plugin_put_back_to_an_older_one_reads_channels_again_without_a_flap(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The capability goes; the per-bouquet topics linger on the broker, as 0.4.0 leaves them.

    `channels` was let go while they were the source, so the broker is asked for it
    again. Nothing an entity shows moves at any point.
    """
    box, changes = await _complete(hass, retained, config_entry, combined=True)
    # What the broker holds: `channels`, as the older plugin publishes it.
    retained[CHANNELS_TOPIC] = json.dumps(CHANNELS)

    await publish(hass, INFO_TOPIC, info(claims=False))

    assert box.channel_list.source == "channels"
    assert box.state.channels == CHANNELS
    assert box.state.channels_released is False
    assert await readers(hass, box) == EXPECTED
    assert changes == []

    # The lingering topics are nobody's word now.
    await publish(hass, list_topic("sport"), list_of({**SPORT, "channels": []}))
    await publish(hass, BOUQUETS_TOPIC, "")
    await settle(hass)

    assert await readers(hass, box) == EXPECTED
    assert changes == []


async def test_the_list_on_show_stays_until_channels_answers(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Between the capability going and `channels` arriving, nobody's select is emptied."""
    box, changes = await _complete(hass, retained, config_entry, combined=True)
    # The broker answers nothing yet: the fake store is empty.

    await publish(hass, INFO_TOPIC, info(claims=False))

    assert box.channel_list.source == "bouquet_topics"
    assert await readers(hass, box) == EXPECTED
    assert changes == []

    shorter = {**CHANNELS, "bouquets": [ULUBIONE]}
    await publish(hass, CHANNELS_TOPIC, json.dumps(shorter))

    assert box.channel_list.source == "channels"
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV"]


async def test_capability_loss_without_a_channels_payload_on_the_broker_is_no_list(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`channels` was withheld, so there is nothing to go back to - and nothing invented."""
    box, _changes = await _complete(hass, retained, config_entry, combined=False)

    await publish(hass, INFO_TOPIC, info(claims=False))

    assert box.channel_list.source == "none"
    assert options(hass, BOUQUET_SELECT) == []
    assert options(hass, HISTORY_SELECT) == []
    assert hass.states.get(PLAYER).attributes.get("source_list") is None

    # The capability comes back - the newer plugin again - and so does the list.
    await publish(hass, INFO_TOPIC, info(claims=True))

    assert await readers(hass, box) == EXPECTED


async def test_a_channels_retracted_after_it_was_let_go_is_not_waited_for(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The list grew and `channels` was withheld; later the capability goes.

    There is no `channels` on the broker to go back to, so the list is not kept on show
    as though one were on its way.
    """
    box, _changes = await _complete(hass, retained, config_entry, combined=True)
    assert box.state.channels_released is True

    await publish(hass, CHANNELS_TOPIC, "")

    assert box.state.channels_released is False
    assert await readers(hass, box) == EXPECTED

    await publish(hass, INFO_TOPIC, info(claims=False))

    assert box.channel_list.source == "none"
    assert options(hass, BOUQUET_SELECT) == []


async def test_a_retracted_index_goes_back_to_channels(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box, changes = await _complete(hass, retained, config_entry, combined=True)
    retained[CHANNELS_TOPIC] = json.dumps(CHANNELS)

    await publish(hass, BOUQUETS_TOPIC, "")

    assert box.state.bouquet_index is None
    assert box.channel_list.source == "channels"
    assert await readers(hass, box) == EXPECTED
    assert changes == []


async def test_a_retracted_index_without_channels_is_no_list(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box, _changes = await _complete(hass, retained, config_entry, combined=False)

    await publish(hass, BOUQUETS_TOPIC, "")

    assert box.channel_list.source == "none"
    assert options(hass, BOUQUET_SELECT) == []
    assert options(hass, HISTORY_SELECT) == []


async def test_a_retracted_bouquet_topic_leaves_the_bouquet_without_channels(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Held back for the settle time - the index or `info` usually follows - then shown.

    Until then the list on show still knows the hidden bouquet's channels, so the filter
    keeps working; afterwards it does not know them, and hides everything.
    """
    box, changes = await _complete(hass, retained, config_entry, combined=False)

    await publish(hass, list_topic("sport"), "")

    assert "sport" not in box.state.bouquet_channels
    assert changes == []
    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD"]

    await settle(hass)

    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]
    assert hass.states.get(PLAYER).attributes["source_list"] == ["TVP 1 HD", "TVN HD"]
    assert options(hass, HISTORY_SELECT) == []
    assert box.channel_list.complete is False

    # Retracted because the bouquet was dropped on the receiver: the index says so.
    del changes[:]
    await publish(hass, BOUQUETS_TOPIC, index_of({**CHANNELS, "bouquets": [ULUBIONE]}))

    assert box.channel_list.complete is True
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV"]
    # Sport is not a bouquet of this receiver any more. Its own entry has no published
    # bouquet and stays out; its channel reached through Ulubione is no longer known as
    # hidden - what a hidden name missing from the list has always meant, and is logged.
    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD", "TVN HD (Sport)"]


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", '{"channels": "no"}', b"\xff\xfe"])
async def test_a_payload_that_cannot_be_read_keeps_the_last_good_one(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    payload: str | bytes,
) -> None:
    box, changes = await _complete(hass, retained, config_entry, combined=False)

    await publish(hass, list_topic("sport"), payload)
    if payload != '{"channels": "no"}':
        await publish(hass, BOUQUETS_TOPIC, payload)
    await settle(hass)

    assert await readers(hass, box) == EXPECTED
    assert changes == []


async def test_an_index_that_is_not_what_the_contract_says_is_read_as_far_as_it_goes(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Entries without a name are dropped; a slug that cannot be a topic level is none."""
    store = base(retained)
    # `channels` is withheld, so the bouquets with no topic of their own - whose lists
    # are only ever there - are not waited for.
    store[INFO_TOPIC] = info(claims=True, not_published=[{"topic": "channels"}])
    store[BOUQUETS_TOPIC] = json.dumps(
        {
            "generated": "soon",
            "bouquets": [
                "Ulubione TV",
                {"sref": "1:7:1:0:0:0:0:0:0:0:x", "slug": "nameless", "count": 1},
                {**json.loads(index_of(CHANNELS))["bouquets"][0], "later": "ignored"},
                {"name": "Slash", "sref": "s1", "slug": "a/b", "count": True},
                {"name": "Plus", "sref": "s2", "slug": "+", "count": -1},
                {"name": "Twin", "sref": "s3", "slug": "ulubione_tv", "count": 4},
                {"name": "No slug", "sref": "s4", "count": "many"},
                {"name": "!!!", "sref": "s5", "slug": "", "count": 3},
            ],
        }
    )
    store[list_topic("ulubione_tv")] = list_of(ULUBIONE)
    await async_setup_box_then_retained(hass, config_entry, store)
    box: Enigma2Box = config_entry.runtime_data

    listing = box.channel_list
    assert listing.complete is True
    assert [
        (bouquet["name"], bouquet["slug"], bouquet["count"], bouquet["listed"])
        for bouquet in listing.bouquets
    ] == [
        ("Ulubione TV", "ulubione_tv", 2, True),
        ("Slash", "", None, False),
        ("Plus", "", None, False),
        ("Twin", "", 4, False),
        ("No slug", "", None, False),
        ("!!!", "", 3, False),
    ]
    assert options(hass, CHANNEL_SELECT) == ["TVP 1 HD", "TVN HD"]


async def test_bouquets_is_the_index_and_bouquet_is_the_one_in_use(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Two topics one letter apart: neither is ever stored as the other."""
    box, _changes = await _complete(hass, retained, config_entry, combined=False)
    context = dict(box.state.bouquet)
    index = box.state.bouquet_index

    await publish(hass, BOUQUETS_TOPIC, index_of({**CHANNELS, "generated": 7}))

    assert box.state.bouquet == context == BOUQUET
    assert box.state.bouquet_index["generated"] == 7

    await publish(hass, BOUQUET_TOPIC, json.dumps({"name": "Sport", "sref": SPORT["sref"]}))

    assert box.state.bouquet == {"name": "Sport", "sref": SPORT["sref"]}
    assert box.state.bouquet_index["bouquets"] == index["bouquets"]
    assert hass.states.get(BOUQUET_SELECT).state == "Sport"
    assert options(hass, CHANNEL_SELECT) == ["Eurosport 1", "TVN HD"]
    # A level below `bouquets` is nothing this reads.
    await publish(hass, f"{BOUQUETS_TOPIC}/sport", list_of(ULUBIONE))
    await publish(hass, f"{list_topic('sport')}/deeper", list_of(ULUBIONE))
    await settle(hass)

    assert options(hass, CHANNEL_SELECT) == ["Eurosport 1", "TVN HD"]
    assert sorted(box.state.bouquet_channels) == ["sport", "ulubione_tv"]


async def test_unloading_leaves_no_timer_and_no_subscription_behind(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An entry unloaded while the list was still arriving does not show it afterwards."""
    box, _changes = await _trickle(hass, retained, config_entry, "index", "ulubione")
    entry = hass.config_entries.async_entries("enigma2_mqtt")[0]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await settle(hass)

    assert box.channel_list.source == "none"
    assert "channels" not in box.updates


# ------------------------------------------- a bouquet with no topic of its own
#
# A bouquet whose name has no ASCII letter or digit has no slug: the plugin lists it in
# the index with `slug` empty and publishes no `channels/<slug>` for it. Its channels are
# in `channels` and nowhere else.

NO_SLUG_NAME = "".join(map(chr, (0x420, 0x443, 0x441, 0x441, 0x43A, 0x438, 0x435)))
NO_SLUG = {
    "name": NO_SLUG_NAME,
    "sref": '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.ru.tv"',
    "channels": [
        {"sref": "1:0:19:AAAA:3F3:1:C00000:0:0:0:", "name": "Pervyi kanal"},
        {"sref": "1:0:19:BBBB:3F3:1:C00000:0:0:0:", "name": "Rossiya 1"},
    ],
}
THREE = {"generated": 1789459200, "bouquets": [ULUBIONE, NO_SLUG, SPORT]}
THREE_SLUGS = {**SLUGS, NO_SLUG_NAME: ""}
THREE_HISTORY = json.dumps(
    {
        "entries": [
            _history_entry(SREF, "TVP 1 HD", ULUBIONE),
            _history_entry(NO_SLUG["channels"][0]["sref"], "Pervyi kanal", NO_SLUG),
            _history_entry(EUROSPORT, "Eurosport 1", SPORT),
        ],
        "current": 0,
        "limit": 20,
        "panic_button": True,
    }
)
THREE_MESSAGES: dict[str, tuple[str, str]] = {
    "info": (INFO_TOPIC, info(claims=True)),
    "index": (BOUQUETS_TOPIC, index_of(THREE, THREE_SLUGS)),
    "ulubione": (list_topic("ulubione_tv"), list_of(ULUBIONE)),
    "sport": (list_topic("sport"), list_of(SPORT)),
    "channels": (CHANNELS_TOPIC, json.dumps(THREE)),
}
# What the filtered history may show, by which bouquet is the hidden one.
THREE_FILTERED = {
    NO_SLUG_NAME: ["TVP 1 HD", "Eurosport 1"],
    "Sport": ["TVP 1 HD", "Pervyi kanal"],
}


def three_expected(hidden: str) -> dict[str, Any]:
    """Return what every reader says about the receiver with the slug-less bouquet."""
    names = ["Ulubione TV", NO_SLUG_NAME, "Sport"]
    return {
        "bouquet_select": (names, "Ulubione TV"),
        "channel_select": (["TVP 1 HD", "TVN HD"], "TVP 1 HD"),
        "history_select": (THREE_FILTERED[hidden], "TVP 1 HD"),
        "sources": ["TVP 1 HD", "TVN HD", "Pervyi kanal", "Rossiya 1", "Eurosport 1"],
        "bouquet_names": names,
        "published": [
            (bouquet["name"], bouquet["sref"], bouquet["channels"])
            for bouquet in THREE["bouquets"]
        ],
        "named": [NO_SLUG["channels"][0]["sref"]],
        "by_sref": NO_SLUG["channels"],
        "name_of": "Rossiya 1",
    }


def three_readers(hass: HomeAssistant, box: Enigma2Box) -> dict[str, Any]:
    return {
        "bouquet_select": (options(hass, BOUQUET_SELECT), hass.states.get(BOUQUET_SELECT).state),
        "channel_select": (options(hass, CHANNEL_SELECT), hass.states.get(CHANNEL_SELECT).state),
        "history_select": (options(hass, HISTORY_SELECT), hass.states.get(HISTORY_SELECT).state),
        "sources": hass.states.get(PLAYER).attributes.get("source_list"),
        "bouquet_names": box.bouquet_names,
        "published": [
            (bouquet["name"], bouquet["sref"], bouquet["channels"])
            for bouquet in box.published_bouquets
        ],
        "named": [channel["sref"] for channel in box.channels_named("Pervyi kanal")],
        "by_sref": (box.bouquet_by_sref(NO_SLUG["sref"]) or {}).get("channels"),
        "name_of": box.published_channel_name(NO_SLUG["channels"][1]["sref"]),
    }


def three_base(store: dict[str, str | bytes]) -> dict[str, str | bytes]:
    base(store)
    store[ZAP_HISTORY_TOPIC] = THREE_HISTORY
    return store


@pytest.mark.parametrize("hidden", list(THREE_FILTERED))
@pytest.mark.parametrize("source", ["channels", "both"])
async def test_a_bouquet_with_no_topic_of_its_own_reads_the_same_from_either_source(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    source: str,
    hidden: str,
) -> None:
    """`channels` alone - the older plugin, and `origin/main` - and both ways together.

    With both, the per-bouquet topics are the source and `channels` is kept for the one
    bouquet whose list is nowhere else.
    """
    store = three_base(retained)
    store[INFO_TOPIC] = info(claims=source == "both")
    store[CHANNELS_TOPIC] = json.dumps(THREE)
    if source == "both":
        store[BOUQUETS_TOPIC] = index_of(THREE, THREE_SLUGS)
        store[list_topic("ulubione_tv")] = list_of(ULUBIONE)
        store[list_topic("sport")] = list_of(SPORT)
    entry = hiding(config_entry, hidden)
    await async_setup_box_then_retained(hass, entry, store)
    box: Enigma2Box = entry.runtime_data

    assert three_readers(hass, box) == three_expected(hidden)
    assert box.channel_list.complete is True
    if source == "both":
        assert box.channel_list.source == "bouquet_topics"
        assert [bouquet["listed"] for bouquet in box.channel_list.bouquets] == [True] * 3
        assert box.state.channels == THREE, "kept: nothing else has that bouquet's list"
        assert box.state.channels_released is False
        # The list is the payload's own, not a copy of it.
        kept = box.state.channels["bouquets"][1]["channels"]
        assert box.published_bouquets[1]["channels"] is kept


@pytest.mark.parametrize("hidden", list(THREE_FILTERED))
@pytest.mark.parametrize("order", list(itertools.permutations(THREE_MESSAGES)), ids="-".join)
async def test_every_arrival_order_with_a_bouquet_that_has_no_topic_of_its_own(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    order: tuple[str, ...],
    hidden: str,
) -> None:
    """The index, `channels`, the two bouquet topics and `info`, in every order.

    Whole at the end, never shown in part, and at no step does the filtered history
    hold a channel of the hidden bouquet - whether that is the bouquet without a topic
    or another one.
    """
    store = three_base(retained)
    entry = hiding(config_entry, hidden)
    changes = watch(hass)
    await async_setup_box_then_retained(hass, entry, store)
    box: Enigma2Box = entry.runtime_data
    expected = three_expected(hidden)
    whole = {
        BOUQUET_SELECT: expected["bouquet_select"][0],
        CHANNEL_SELECT: expected["channel_select"][0],
        HISTORY_SELECT: expected["history_select"][0],
        PLAYER: expected["sources"],
    }

    for name in order:
        await deliver(hass, *THREE_MESSAGES[name])
        assert set(options(hass, HISTORY_SELECT) or []) <= set(THREE_FILTERED[hidden]), name

    assert three_readers(hass, box) == expected
    assert box.channel_list.source == "bouquet_topics"
    assert box.channel_list.complete is True
    assert box.state.channels == THREE
    for entity_id in ENTITIES:
        shown = [lists for changed, _state, lists in changes if changed == entity_id]
        assert all(lists in (None, [], whole[entity_id]) for lists in shown), (entity_id, shown)
        assert shown.count(whole[entity_id]) == 1, (entity_id, shown)


async def _three(
    hass: HomeAssistant,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    *names: str,
    hidden: str = NO_SLUG_NAME,
    not_published: Any = ...,
) -> tuple[Enigma2Box, list[tuple[str, str, Any]]]:
    store = three_base(retained)
    store[INFO_TOPIC] = info(claims=True, not_published=not_published)
    entry = hiding(config_entry, hidden)
    await async_setup_box_then_retained(hass, entry, store)
    changes = watch(hass)
    for name in names:
        await deliver(hass, *THREE_MESSAGES[name])
    return entry.runtime_data, changes


@pytest.mark.parametrize(
    ("hidden", "filtered"),
    [
        # The hidden bouquet is the one without its list: nothing can be told apart.
        (NO_SLUG_NAME, []),
        # Another bouquet is hidden, and its list is here: only the entry whose own
        # bouquet has no list is still shown or hidden by the usual rules.
        ("Sport", ["TVP 1 HD", "Pervyi kanal"]),
    ],
)
async def test_with_channels_withheld_such_a_bouquet_has_no_list_and_nothing_waits_for_one(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    hidden: str,
    filtered: list[str],
) -> None:
    """The receiver says `channels` will not come, so the list is complete as it stands.

    The bouquet stays a bouquet, by its name and its count, with no channels; and while
    it is the hidden one, the filtered history fails toward hiding.
    """
    box, _changes = await _three(
        hass,
        retained,
        config_entry,
        "index",
        "ulubione",
        "sport",
        hidden=hidden,
        not_published=[{"topic": "channels", "bytes": 2315478, "limit": 1000000}],
    )

    listing = box.channel_list
    assert listing.source == "bouquet_topics"
    assert listing.complete is True
    assert [bouquet["listed"] for bouquet in listing.bouquets] == [True, False, True]
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", NO_SLUG_NAME, "Sport"]
    assert box.bouquet_by_sref(NO_SLUG["sref"])["channels"] == []
    assert box.bouquet_by_sref(NO_SLUG["sref"])["count"] == 2
    assert hass.states.get(PLAYER).attributes["source_list"] == [
        "TVP 1 HD",
        "TVN HD",
        "Eurosport 1",
    ]
    assert options(hass, HISTORY_SELECT) == filtered


async def test_without_channels_such_a_bouquet_is_waited_for_and_then_shown_without_its_list(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Not arrived and not reported withheld: held back, then shown as far as it goes."""
    box, changes = await _three(hass, retained, config_entry, "index", "ulubione", "sport")

    assert box.channel_list.source == "none"
    assert changes == []

    await settle(hass)

    assert box.channel_list.complete is False
    assert [bouquet["listed"] for bouquet in box.channel_list.bouquets] == [True, False, True]
    assert options(hass, HISTORY_SELECT) == []

    counted = box.updates["channels"]
    await deliver(hass, *THREE_MESSAGES["channels"])

    assert box.channel_list.complete is True
    assert three_readers(hass, box) == three_expected(NO_SLUG_NAME)
    assert box.state.channels == THREE
    assert box.updates["channels"] == counted + 1, "woken once, by the list it completed"


async def test_channels_retracted_takes_that_bouquets_list_and_nobody_elses(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The list outgrew one packet: `channels` is retracted, then named in `info`."""
    box, changes = await _three(
        hass, retained, config_entry, "index", "ulubione", "sport", "channels", hidden="Sport"
    )
    assert three_readers(hass, box) == three_expected("Sport")

    await publish(hass, CHANNELS_TOPIC, "")

    # Held back: `info` usually follows, and until then the list on show is still right.
    assert box.state.channels is None
    assert three_readers(hass, box) == three_expected("Sport")

    await publish(hass, INFO_TOPIC, info(claims=True, not_published=[{"topic": "channels"}]))

    assert box.channel_list.complete is True
    assert [bouquet["listed"] for bouquet in box.channel_list.bouquets] == [True, False, True]
    assert box.channels_named("Pervyi kanal") == []
    assert options(hass, CHANNEL_SELECT) == ["TVP 1 HD", "TVN HD"]


async def test_a_republished_channels_is_not_a_second_copy_and_a_changed_one_is_read(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box, changes = await _three(
        hass, retained, config_entry, "index", "ulubione", "sport", "channels"
    )
    kept = box.state.channels
    counted = box.updates["channels"]
    del changes[:]

    await publish(hass, *THREE_MESSAGES["channels"])

    assert box.state.channels is kept
    assert box.updates["channels"] == counted
    assert changes == []

    # Another bouquet's list moves in `channels` - which is not where it is read from -
    # and this one's does not: nothing an entity shows moves, and the list on show is
    # the new payload's, so that the old payload is not kept alive by it.
    elsewhere = {**THREE, "bouquets": [{**ULUBIONE, "channels": []}, NO_SLUG, SPORT]}
    await publish(hass, CHANNELS_TOPIC, json.dumps(elsewhere))

    assert changes == []
    assert box.updates["channels"] == counted
    assert box.state.channels is not kept
    assert box.published_bouquets[1]["channels"] is box.state.channels["bouquets"][1]["channels"]

    # The bouquet without a topic is edited on the receiver; the others' lists in
    # `channels` say something else than their own topics, and are not read.
    edited = {
        **THREE,
        "bouquets": [
            {**ULUBIONE, "channels": []},
            {**NO_SLUG, "channels": NO_SLUG["channels"][:1]},
            SPORT,
        ],
    }
    await publish(hass, CHANNELS_TOPIC, json.dumps(edited))

    assert box.state.channels == edited
    assert [channel["name"] for channel in box.bouquet_by_sref(NO_SLUG["sref"])["channels"]] == [
        "Pervyi kanal"
    ]
    assert options(hass, CHANNEL_SELECT) == ["TVP 1 HD", "TVN HD"]
    assert box.published_bouquets[1]["channels"] is box.state.channels["bouquets"][1]["channels"]


async def test_channels_is_kept_only_while_a_bouquet_needs_it(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The bouquet is renamed to something with a slug: `channels` is let go again.

    And when a bouquet without a topic appears on a receiver whose `channels` was let
    go, the broker is asked for it.
    """
    box, _changes = await _three(
        hass, retained, config_entry, "index", "ulubione", "sport", "channels"
    )
    assert box.state.channels is not None

    await publish(hass, BOUQUETS_TOPIC, index_of(CHANNELS))

    assert box.state.channels is None
    assert box.state.channels_released is True
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]

    retained[CHANNELS_TOPIC] = json.dumps(THREE)
    await publish(hass, BOUQUETS_TOPIC, index_of(THREE, THREE_SLUGS))

    assert box.state.channels == THREE
    assert box.channel_list.complete is True
    assert three_readers(hass, box) == three_expected(NO_SLUG_NAME)


async def test_a_bouquet_without_a_topic_appearing_when_the_broker_has_no_channels(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Asked for, not answered: after the bound the bouquet is one without its list."""
    box, _changes = await _three(
        hass, retained, config_entry, "index", "ulubione", "sport", "channels"
    )
    await publish(hass, BOUQUETS_TOPIC, index_of(CHANNELS))
    assert box.state.channels_released is True

    await publish(hass, BOUQUETS_TOPIC, index_of(THREE, THREE_SLUGS))

    # Still the list that was on show.
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]

    await elapse(hass, REFETCH_BOUND + 1)
    await settle(hass)

    assert box.state.channels_released is False
    assert box.channel_list.complete is False
    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", NO_SLUG_NAME, "Sport"]
    assert box.bouquet_by_sref(NO_SLUG["sref"])["channels"] == []
    assert options(hass, HISTORY_SELECT) == []


async def test_capability_loss_with_channels_kept_needs_no_question(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box, changes = await _three(
        hass, retained, config_entry, "index", "ulubione", "sport", "channels"
    )
    del changes[:]
    with spying(CHANNELS_TOPIC) as subscriptions:
        await publish(hass, INFO_TOPIC, info(claims=False))

    assert subscriptions == []
    assert box.channel_list.source == "channels"
    assert three_readers(hass, box) == three_expected(NO_SLUG_NAME)
    assert changes == []


# ------------------------------------------------ asking for `channels` again
#
# The broker's answer to the request arrives inside `mqtt.async_subscribe` in the
# `retained` fixture, before the unsubscribe handle exists. A broker answers later, so
# these deliver the retained answer by hand, to a subscription that is live.

SUBSCRIBE_TIMEOUT = 5
REFETCH_GRACE = 5.0
REFETCH_BOUND = SUBSCRIBE_TIMEOUT + REFETCH_GRACE


async def elapse(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


@contextmanager
def spying(topic: str) -> Iterator[list[dict[str, bool]]]:
    """Record every subscription made to one topic, and whether it is still live."""
    made: list[dict[str, bool]] = []
    real_subscribe = mqtt.async_subscribe

    async def _subscribe(hass_: HomeAssistant, wanted: str, *args: Any, **kwargs: Any) -> Any:
        unsubscribe = await real_subscribe(hass_, wanted, *args, **kwargs)
        if wanted != topic:
            return unsubscribe
        record = {"live": True}
        made.append(record)

        def _unsubscribe() -> None:
            record["live"] = False
            unsubscribe()

        return _unsubscribe

    with patch("homeassistant.components.mqtt.async_subscribe", _subscribe):
        yield made


async def test_the_answer_ends_the_request_and_is_read_once(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box, changes = await _complete(hass, retained, config_entry, combined=True)
    with spying(CHANNELS_TOPIC) as subscriptions:
        await publish(hass, INFO_TOPIC, info(claims=False))
        assert subscriptions == [{"live": True}]
        assert box.channel_list.source == "bouquet_topics"
        counted = box.updates["channels"]

        # The broker's retained answer, to the subscription that asked.
        await deliver(hass, CHANNELS_TOPIC, json.dumps(CHANNELS))

        assert subscriptions == [{"live": False}]
        assert box.channel_list.source == "channels"
        assert box.updates["channels"] == counted + 1
        assert changes == []

        await publish(hass, CHANNELS_TOPIC, json.dumps(CHANNELS))

        assert box.updates["channels"] == counted + 2
        assert subscriptions == [{"live": False}]


async def test_a_channels_published_meanwhile_ends_the_request_too(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Not retained, so it is the wildcard's to read - once, not once per subscription."""
    box, _changes = await _complete(hass, retained, config_entry, combined=True)
    with spying(CHANNELS_TOPIC) as subscriptions:
        await publish(hass, INFO_TOPIC, info(claims=False))
        counted = box.updates["channels"]

        await publish(hass, CHANNELS_TOPIC, json.dumps(CHANNELS))

        assert subscriptions == [{"live": False}]
        assert box.updates["channels"] == counted + 1
        assert box.channel_list.source == "channels"


async def test_unloading_ends_the_request_and_a_late_answer_reaches_nobody(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box, _changes = await _complete(hass, retained, config_entry, combined=True)
    entry = hass.config_entries.async_entries("enigma2_mqtt")[0]
    with spying(CHANNELS_TOPIC) as subscriptions:
        await publish(hass, INFO_TOPIC, info(claims=False))
        assert subscriptions == [{"live": True}]
        counted = dict(box.updates)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert subscriptions == [{"live": False}]

        await deliver(hass, CHANNELS_TOPIC, json.dumps(CHANNELS))
        await elapse(hass, REFETCH_BOUND + 1)

        assert box.state.channels is None
        assert box.updates == counted
        assert box.channel_list.source == "bouquet_topics"


async def test_a_second_request_does_not_leave_the_first_behind(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The capability comes back before the broker answered, and goes again."""
    box, changes = await _complete(hass, retained, config_entry, combined=True)
    with spying(CHANNELS_TOPIC) as subscriptions:
        await publish(hass, INFO_TOPIC, info(claims=False))
        assert subscriptions == [{"live": True}]

        await publish(hass, INFO_TOPIC, info(claims=True))

        assert subscriptions == [{"live": False}]
        assert box.channel_list.source == "bouquet_topics"

        await publish(hass, INFO_TOPIC, info(claims=False))

        assert subscriptions == [{"live": False}, {"live": True}]

        await deliver(hass, CHANNELS_TOPIC, json.dumps(CHANNELS))

        assert subscriptions == [{"live": False}, {"live": False}]
        assert box.channel_list.source == "channels"
        assert changes == []
        # Nothing of the first request is left to fire.
        await elapse(hass, REFETCH_BOUND + 1)
        assert box.channel_list.source == "channels"
        assert await readers(hass, box) == EXPECTED


async def test_a_broker_that_never_answers_ends_the_wait_with_no_list(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`channels` was retracted while Home Assistant was away: nothing is retained.

    The list is kept on show for the wait and no longer; a stale snapshot is not a
    channel list.
    """
    box, changes = await _complete(hass, retained, config_entry, combined=True)
    with spying(CHANNELS_TOPIC) as subscriptions:
        await publish(hass, INFO_TOPIC, info(claims=False))

        await elapse(hass, 2)

        assert box.channel_list.source == "bouquet_topics"
        assert changes == []

        await elapse(hass, REFETCH_BOUND + 1)

        assert subscriptions == [{"live": False}]
        assert box.channel_list.source == "none"
        assert box.state.channels_released is False
        assert options(hass, BOUQUET_SELECT) == []
        assert hass.states.get(PLAYER).attributes.get("source_list") is None

        # And a `channels` that does come, later, is read like any other.
        await publish(hass, CHANNELS_TOPIC, json.dumps(CHANNELS))

        assert await readers(hass, box) == EXPECTED


@pytest.mark.parametrize(
    "listed_as",
    [
        # Not in `channels` at all: it has nothing more to say about the bouquet.
        None,
        # The same name on another bouquet, and the same reference under another name.
        {"sref": "1:7:1:0:0:0:0:0:0:0:another"},
        {"name": "Renamed since"},
        # In it, without a list.
        {"channels": "none"},
    ],
)
async def test_a_channels_that_does_not_list_such_a_bouquet_is_not_waited_on_or_borrowed_from(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    listed_as: dict[str, Any] | None,
) -> None:
    """`channels` is here and the bouquet's own entry is not: complete, without its list.

    A list is taken only from the entry with this bouquet's reference and its name. One
    of them alone is another bouquet, and another bouquet's channels are not this one's -
    which matters most when this is the hidden one.
    """
    bouquets = [ULUBIONE, SPORT]
    if listed_as is not None:
        bouquets.insert(1, {**NO_SLUG, **listed_as})
    box, _changes = await _three(hass, retained, config_entry, "index", "ulubione", "sport")
    assert box.channel_list.source == "none"

    await deliver(hass, CHANNELS_TOPIC, json.dumps({**THREE, "bouquets": bouquets}))

    listing = box.channel_list
    assert listing.source == "bouquet_topics"
    assert listing.complete is True, "nothing more can arrive for it"
    assert [bouquet["listed"] for bouquet in listing.bouquets] == [True, False, True]
    assert box.bouquet_by_sref(NO_SLUG["sref"])["channels"] == []
    assert options(hass, HISTORY_SELECT) == []


async def test_the_same_channels_under_another_reference_is_another_list(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Only a new stamp is skipped. Whose list it is, is not a stamp."""
    box, _changes = await _complete(hass, retained, config_entry, combined=False)

    await publish(
        hass, list_topic("sport"), list_of({**SPORT, "sref": "1:7:1:0:0:0:0:0:0:0:other"})
    )
    await publish(hass, list_topic("ulubione_tv"), list_of({**ULUBIONE, "name": "Other"}))
    await settle(hass)

    assert box.state.bouquet_channels["sport"]["sref"] == "1:7:1:0:0:0:0:0:0:0:other"
    assert box.state.bouquet_channels["ulubione_tv"]["bouquet"] == "Other"
    assert [bouquet["listed"] for bouquet in box.channel_list.bouquets] == [True, False]


async def test_the_wait_is_five_seconds_from_the_brokers_confirmation(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Confirmed, and then silence: the shorter bound, not the one for no confirmation."""
    box, _changes = await _complete(hass, retained, config_entry, combined=True)
    confirm: list[Any] = []
    real_tracker = mqtt.async_on_subscribe_done

    def _tracker(hass_: HomeAssistant, topic: str, qos: int, done: Any) -> Any:
        if topic != CHANNELS_TOPIC:
            return real_tracker(hass_, topic, qos, done)
        confirm.append(done)
        return lambda: confirm.remove(done) if done in confirm else None

    with patch("homeassistant.components.mqtt.async_on_subscribe_done", _tracker):
        await publish(hass, INFO_TOPIC, info(claims=False))
        assert len(confirm) == 1

        # Never confirmed so far: the longer bound has not run out at six seconds.
        await elapse(hass, REFETCH_GRACE + 1)
        assert box.channel_list.source == "bouquet_topics"

        confirm[0]()
        await elapse(hass, REFETCH_GRACE - 1)
        assert box.channel_list.source == "bouquet_topics"
        await elapse(hass, REFETCH_GRACE + 1)

        assert box.channel_list.source == "none"
        assert confirm == [], "the tracker went with the request"


async def test_without_a_confirmation_the_wait_still_ends(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """No broker to confirm anything: degrades rather than hangs, as the commands do."""
    box, _changes = await _complete(hass, retained, config_entry, combined=True)
    with patch(
        "homeassistant.components.mqtt.async_on_subscribe_done",
        lambda *args: lambda: None,
    ):
        await publish(hass, INFO_TOPIC, info(claims=False))
        await elapse(hass, REFETCH_BOUND - 1)
        assert box.channel_list.source == "bouquet_topics"

        await elapse(hass, REFETCH_BOUND + 1)

        assert box.channel_list.source == "none"


# ------------------------------------------------------------ smaller things


async def test_a_new_stamp_on_the_same_list_rebuilds_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart of the plugin stamps every bouquet's `generated` anew."""
    box, changes = await _complete(hass, retained, config_entry, combined=False)
    held = box.state.bouquet_channels["sport"]["channels"]
    counted = dict(box.updates)
    built: list[int] = []
    build = Enigma2Box._async_show_topic_list

    def _counting(self: Enigma2Box, **kwargs: Any) -> None:
        built.append(1)
        build(self, **kwargs)

    monkeypatch.setattr(Enigma2Box, "_async_show_topic_list", _counting)

    await publish(hass, list_topic("sport"), list_of(SPORT, generated=1789999999))

    assert built == []
    assert changes == []
    assert box.updates == counted
    assert box.state.bouquet_channels["sport"]["channels"] is held
    assert box.state.bouquet_channels["sport"]["generated"] == 1789999999

    # A list that did change is still taken.
    await publish(
        hass, list_topic("sport"), list_of({**SPORT, "channels": SPORT["channels"][:1]})
    )

    assert built == [1]
    assert hass.states.get(PLAYER).attributes["source_list"] == [
        "TVP 1 HD",
        "TVN HD",
        "Eurosport 1",
    ]


async def test_the_capability_is_taken_from_an_announcement_that_arrives_last(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """No `info` yet, and the announcement lands after the index and every list."""
    store = base(retained)
    announcement = store.pop(ANNOUNCEMENT_TOPIC)
    await async_setup_box_then_retained(hass, config_entry, store)
    box: Enigma2Box = config_entry.runtime_data
    for name in ("index", "ulubione", "sport"):
        await deliver(hass, *MESSAGES[name])
    await settle(hass)
    assert box.channel_list.source == "none"

    await deliver(
        hass,
        ANNOUNCEMENT_TOPIC,
        json.dumps(
            {
                **json.loads(announcement),
                "capabilities": json.loads(info(claims=True))["capabilities"],
            }
        ),
    )

    assert box.channel_list.source == "bouquet_topics"
    assert hass.states.get(PLAYER).attributes["source_list"] == EXPECTED["sources"]


# ------------------------------------------------------------------- diagnostics


async def test_diagnostics_show_the_shape_of_the_list_and_none_of_its_channels(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Which source, which bouquets, how many channels - and no name or reference of one."""
    store = base(retained)
    store[INFO_TOPIC] = info(
        claims=True,
        not_published=[
            {"topic": "channels", "bytes": 2315478, "limit": 1000000},
            {"topic": "channels/sport", "bytes": 1000001, "limit": 1000000},
        ],
    )
    store[BOUQUETS_TOPIC] = index_of(CHANNELS)
    store[list_topic("ulubione_tv")] = list_of(ULUBIONE)
    store[EPG_GRID_TOPIC] = json.dumps({**EPG_GRID, "events_per_channel": 2})
    await async_setup_box_then_retained(hass, config_entry, store)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["box"]["channel_list"] == {
        "source": "bouquet_topics",
        "complete": True,
        "generated": 1789459200,
        "channels_topic_released": False,
        "bouquets": [
            {"name": "Ulubione TV", "listed": 2},
            {"name": "Sport", "listed": None},
        ],
    }
    assert diagnostics["box"]["not_published"] == [
        {"topic": "channels", "bytes": 2315478, "limit": 1000000},
        {"topic": "channels/sport", "bytes": 1000001, "limit": 1000000},
    ]
    assert diagnostics["topics"]["channels"] is None
    assert diagnostics["topics"]["bouquets"] == {
        "generated": 1789459200,
        "bouquets": [
            {"name": "Ulubione TV", "slug": "ulubione_tv", "count": 2},
            {"name": "Sport", "slug": "sport", "count": 2},
        ],
    }
    assert diagnostics["topics"]["bouquet_channels"] == {
        "ulubione_tv": {"bouquet": "Ulubione TV", "generated": 1789459200, "channels": 2}
    }
    assert diagnostics["topics"]["epg_grid"]["ulubione_tv"]["events_per_channel"] == 2
    assert "channels/+" in diagnostics["box"]["topics_seen"]
    assert "bouquets" in diagnostics["box"]["topics_seen"]

    # The service topic, the zap history and the grid name channels of their own; what is
    # checked here is that the channel list's topics add none.
    del diagnostics["topics"]["service"]
    dumped = json.dumps(diagnostics, ensure_ascii=False)
    for channel in ULUBIONE["channels"]:
        assert channel["name"] not in dumped
        assert channel["sref"] not in dumped
    # The active bouquet's reference is on the `bouquet` topic; the index adds none.
    assert SPORT["sref"] not in dumped


async def test_diagnostics_of_an_older_plugin_say_so(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["box"]["channel_list"]["source"] == "channels"
    assert diagnostics["box"]["channel_list"]["channels_topic_released"] is False
    assert diagnostics["box"]["not_published"] is None
    assert diagnostics["topics"]["bouquets"] is None
    assert diagnostics["topics"]["bouquet_channels"] == {}
    assert diagnostics["topics"]["epg_grid"] == {}
    assert "channels/+" not in diagnostics["box"]["topics_seen"]
    assert "bouquets" not in diagnostics["box"]["topics_seen"]


# -------------------------------------------------------- the grid's own member


@pytest.mark.parametrize(
    ("member", "shown"),
    [
        ({"events_per_channel": 2}, 2),
        ({"events_per_channel": 1}, 1),
        # An older plugin: the attribute is not there at all, as it never was.
        ({}, ...),
        ({"events_per_channel": 0}, ...),
        ({"events_per_channel": True}, ...),
        ({"events_per_channel": "2"}, ...),
        ({"events_per_channel": None}, ...),
    ],
)
async def test_the_active_bouquets_sensor_says_how_far_its_grid_was_cut(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    member: dict[str, Any],
    shown: Any,
) -> None:
    store = base(retained)
    store[INFO_TOPIC] = info(claims=False)
    store[EPG_GRID_TOPIC] = json.dumps({**EPG_GRID, **member})
    registry = er.async_get(hass)
    await async_setup_box_then_retained(hass, config_entry, store)
    assert registry.async_get(EPG_SENSOR) is not None

    attributes = dict(hass.states.get(EPG_SENSOR).attributes)

    assert [channel["name"] for channel in attributes["channels"]] == ["TVP 1 HD"]
    if shown is ...:
        assert "events_per_channel" not in attributes
    else:
        assert attributes["events_per_channel"] == shown

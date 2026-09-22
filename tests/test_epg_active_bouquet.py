"""„EPG – aktywny bukiet": what is on across the bouquet the receiver is walking.

Three things make this sensor worth its own file. It joins two topics — the channel-list
context and one of several per-bouquet grids — and has to read the right grid and only
that one. It moves with the clock as well as with the topics, because the plugin
republishes a grid only when its content changes and a programme can end in between.
And its one big attribute must stay out of the recorder, which this integration has to
arrange itself; the recorder test below goes through the recorder's own function rather
than reading the declaration back, because a declaration that is read back only proves
it was written.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_bytes
import homeassistant.util.dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.enigma2_mqtt.const import (
    CAPABILITY_BOUQUET_CONTEXT,
    DOMAIN,
    EPG_TITLE_MAX,
)

from .conftest import (
    BASE_TOPIC,
    BOUQUET,
    BOUQUET_TOPIC,
    CAPABILITIES,
    EPG_GRID,
    EPG_GRID_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    SLUG,
    SREF,
    SREF_TWO,
    async_setup_box_then_retained,
)

SENSOR = f"sensor.{SLUG}_epg_active_bouquet"
UNIQUE_ID = f"{NODE_ID}_epg_active_bouquet"

SPORT_GRID_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/epg_grid/sport"
SPORT_CONTEXT = {
    "name": "Sport",
    "sref": '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.sport.tv"',
}
NO_CONTEXT = {"name": None, "sref": None}

WITH_CONTEXT = {**INFO, "capabilities": [*CAPABILITIES, CAPABILITY_BOUQUET_CONTEXT]}

# The fixture's „Wiadomości" runs 1789459200–1789460700 and „Pogoda" follows it until
# 1789461000. Ten minutes into the news is the moment every test below starts from.
NEWS_BEGIN = 1789459200
NEWS_END = 1789460700
WEATHER_END = 1789461000
TEN_MINUTES_IN = dt_util.utc_from_timestamp(NEWS_BEGIN + 600)


def _event(title: str, begin: int, end: int, event_id: int = 1) -> dict[str, Any]:
    return {"title": title, "begin": begin, "end": end, "event_id": event_id}


FAVOURITES_GRID: dict[str, Any] = {
    **EPG_GRID,
    "channels": [
        {
            "sref": SREF,
            "name": "TVP 1 HD",
            "events": [
                _event("Wiadomości", NEWS_BEGIN, NEWS_END, 27431),
                _event("Pogoda", NEWS_END, WEATHER_END, 27432),
            ],
        },
        # A channel the EPG cache has nothing for. It is in the list, because it is in
        # the bouquet, and it is not counted, because it has nothing to show.
        {"sref": SREF_TWO, "name": "TVN HD", "events": []},
    ],
}

SPORT_GRID: dict[str, Any] = {
    "bouquet": "Sport",
    "generated": NEWS_BEGIN,
    "channels": [
        {
            "sref": "1:0:19:1234:3F3:1:C00000:0:0:0:",
            "name": "Eurosport 1",
            "events": [_event("Snooker", NEWS_BEGIN - 3600, NEWS_BEGIN + 7200, 5)],
        },
        {
            "sref": "1:0:19:5678:3F3:1:C00000:0:0:0:",
            "name": "TVN HD",
            "events": [_event("Magazyn sportowy", NEWS_BEGIN, NEWS_END, 6)],
        },
        {
            "sref": "1:0:19:9ABC:3F3:1:C00000:0:0:0:",
            "name": "Polsat Sport",
            "events": [_event("Siatkówka", NEWS_BEGIN, NEWS_END, 7)],
        },
    ],
}


def _capable_box(retained: dict[str, str | bytes], **topics: Any) -> None:
    """Put a receiver on the broker that has grids, a context, and is in „Ulubione TV"."""
    retained[INFO_TOPIC] = json.dumps(WITH_CONTEXT)
    retained[BOUQUET_TOPIC] = json.dumps(topics.get("bouquet", BOUQUET))
    retained[EPG_GRID_TOPIC] = json.dumps(topics.get("favourites", FAVOURITES_GRID))
    retained[SPORT_GRID_TOPIC] = json.dumps(topics.get("sport", SPORT_GRID))


async def _publish(hass: HomeAssistant, topic: str, payload: Any) -> None:
    async_fire_mqtt_message(
        hass, topic, payload if isinstance(payload, str) else json.dumps(payload)
    )
    await hass.async_block_till_done()


def _channels(hass: HomeAssistant) -> list[dict[str, Any]]:
    return hass.states.get(SENSOR).attributes["channels"]


async def test_it_needs_both_the_grids_and_the_context(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Grids without a context have no „active" bouquet to show."""
    box_on_the_broker[EPG_GRID_TOPIC] = json.dumps(FAVOURITES_GRID)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert "epg_grid" in INFO["capabilities"]
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, UNIQUE_ID) is None
    )


async def test_it_is_created_once_in_the_order_a_broker_delivers(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Capabilities arrive after setup; the sensor has to appear then, and only once."""
    freezer.move_to(TEN_MINUTES_IN)
    _capable_box(box_on_the_broker)
    changes: list[tuple[str, str]] = []

    @callback
    def _registry_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["entity_id"] == SENSOR:
            changes.append((event.data["action"], event.data["entity_id"]))

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_changed)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert changes == [("create", SENSOR)]
    assert hass.states.get(SENSOR).state == "1"


async def test_a_capability_that_goes_quiet_does_not_remove_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An older plugin or a box that lost a hook is silence, not a decision."""
    _capable_box(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, UNIQUE_ID) == SENSOR

    await _publish(hass, INFO_TOPIC, {**INFO, "capabilities": ["power"]})

    assert registry.async_get_entity_id("sensor", DOMAIN, UNIQUE_ID) == SENSOR


async def test_the_state_and_the_list_are_what_the_contract_says(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every channel of the bouquet, the count of those with something to show."""
    freezer.move_to(TEN_MINUTES_IN)
    _capable_box(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state.state == "1"
    assert state.attributes["channels"] == [
        {
            "name": "TVP 1 HD",
            "sref": SREF,
            "now": {"title": "Wiadomości", "begin": NEWS_BEGIN, "end": NEWS_END},
            "next": {"title": "Pogoda", "begin": NEWS_END, "end": WEATHER_END},
        },
        {"name": "TVN HD", "sref": SREF_TWO, "now": None, "next": None},
    ]
    # A completeness indicator, not a measurement: nothing for long-term statistics.
    assert ATTR_UNIT_OF_MEASUREMENT not in state.attributes
    assert ATTR_STATE_CLASS not in state.attributes


async def test_titles_are_capped(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An episode blurb in the title field is cut, not carried."""
    freezer.move_to(TEN_MINUTES_IN)
    long_title = "Serial obyczajowy, odcinek 1234: " + "bardzo długi opis " * 10
    assert len(long_title) > EPG_TITLE_MAX
    grid = {
        **FAVOURITES_GRID,
        "channels": [
            {
                "sref": SREF,
                "name": "TVP 1 HD",
                "events": [_event(long_title, NEWS_BEGIN, NEWS_END)],
            }
        ],
    }
    _capable_box(box_on_the_broker, favourites=grid)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    title = _channels(hass)[0]["now"]["title"]
    assert len(title) == EPG_TITLE_MAX
    assert title == long_title[:EPG_TITLE_MAX]


async def test_it_follows_the_bouquet_the_receiver_is_on(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Switching the context switches the grid it reads, both ways."""
    freezer.move_to(TEN_MINUTES_IN)
    _capable_box(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert [channel["name"] for channel in _channels(hass)] == ["TVP 1 HD", "TVN HD"]

    await _publish(hass, BOUQUET_TOPIC, SPORT_CONTEXT)

    assert hass.states.get(SENSOR).state == "3"
    assert [channel["name"] for channel in _channels(hass)] == [
        "Eurosport 1",
        "TVN HD",
        "Polsat Sport",
    ]

    await _publish(hass, BOUQUET_TOPIC, BOUQUET)

    assert hass.states.get(SENSOR).state == "1"
    assert [channel["name"] for channel in _channels(hass)] == ["TVP 1 HD", "TVN HD"]


async def test_only_the_active_bouquets_grid_moves_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Another bouquet's grid is not news here; this bouquet's grid is."""
    freezer.move_to(TEN_MINUTES_IN)
    _capable_box(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    written: list[Event[EventStateChangedData]] = []

    @callback
    def _state_changed(event: Event[EventStateChangedData]) -> None:
        if event.data["entity_id"] == SENSOR:
            written.append(event)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _state_changed)

    moved_sport = {
        **SPORT_GRID,
        "channels": [
            {**channel, "events": [_event("Inny program", NEWS_BEGIN, NEWS_END, 99)]}
            for channel in SPORT_GRID["channels"]
        ],
    }
    await _publish(hass, SPORT_GRID_TOPIC, moved_sport)
    assert written == []
    assert _channels(hass)[0]["now"]["title"] == "Wiadomości"

    moved_favourites = {
        **FAVOURITES_GRID,
        "channels": [
            {
                "sref": SREF,
                "name": "TVP 1 HD",
                "events": [_event("Wiadomości wydanie specjalne", NEWS_BEGIN, NEWS_END)],
            }
        ],
    }
    await _publish(hass, EPG_GRID_TOPIC, moved_favourites)
    assert len(written) == 1
    assert _channels(hass)[0]["now"]["title"] == "Wiadomości wydanie specjalne"


async def test_no_active_bouquet_is_zero_and_an_empty_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The radio list or the movie list: the receiver is in no bouquet to count."""
    _capable_box(box_on_the_broker, bouquet=NO_CONTEXT)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(SENSOR).state == "0"
    assert _channels(hass) == []


async def test_an_active_bouquet_without_a_grid_is_unknown_not_zero(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """„We have no grid" and „the grid says nothing" are different answers.

    A bouquet whose grid has not arrived, one whose grid the box retracted, and one the
    box does not build a grid for at all all read `unknown`. Zero is kept for a receiver
    that is in no bouquet, and for a grid that really does carry nothing.
    """
    freezer.move_to(TEN_MINUTES_IN)
    _capable_box(box_on_the_broker)
    del box_on_the_broker[SPORT_GRID_TOPIC]
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert hass.states.get(SENSOR).state == "1"

    # A bouquet the box builds no grid for.
    await _publish(hass, BOUQUET_TOPIC, SPORT_CONTEXT)
    assert hass.states.get(SENSOR).state == STATE_UNKNOWN
    assert _channels(hass) == []

    # The active bouquet's grid, retracted.
    await _publish(hass, BOUQUET_TOPIC, BOUQUET)
    assert hass.states.get(SENSOR).state == "1"
    await _publish(hass, EPG_GRID_TOPIC, "")
    assert hass.states.get(SENSOR).state == STATE_UNKNOWN
    assert _channels(hass) == []

    # A grid that is here and has nothing in it is a real zero.
    await _publish(
        hass,
        EPG_GRID_TOPIC,
        {**FAVOURITES_GRID, "channels": [{"sref": SREF, "name": "TVP 1 HD", "events": []}]},
    )
    assert hass.states.get(SENSOR).state == "0"
    assert len(_channels(hass)) == 1


async def test_now_moves_to_next_when_the_programme_ends(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With no new grid at all: the plugin only republishes one that changed."""
    freezer.move_to(TEN_MINUTES_IN)
    _capable_box(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert _channels(hass)[0]["now"]["title"] == "Wiadomości"

    await _move_clock(hass, freezer, dt_util.utc_from_timestamp(NEWS_END))

    first = _channels(hass)[0]
    assert first["now"] == {"title": "Pogoda", "begin": NEWS_END, "end": WEATHER_END}
    assert first["next"] is None
    assert hass.states.get(SENSOR).state == "1"

    await _move_clock(hass, freezer, dt_util.utc_from_timestamp(WEATHER_END))

    assert _channels(hass)[0]["now"] is None
    assert hass.states.get(SENSOR).state == "0"


async def test_a_channel_between_programmes_shows_only_next_until_it_starts(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A gap in the schedule is a channel with a `next` and no `now`."""
    freezer.move_to(TEN_MINUTES_IN)
    starts = NEWS_BEGIN + 1200
    grid = {
        **FAVOURITES_GRID,
        "channels": [
            {
                "sref": SREF,
                "name": "TVP 1 HD",
                "events": [_event("Film", starts, starts + 5400)],
            }
        ],
    }
    _capable_box(box_on_the_broker, favourites=grid)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert _channels(hass)[0]["now"] is None
    assert _channels(hass)[0]["next"]["title"] == "Film"
    assert hass.states.get(SENSOR).state == "1"

    await _move_clock(hass, freezer, dt_util.utc_from_timestamp(starts))

    assert _channels(hass)[0]["now"]["title"] == "Film"
    assert _channels(hass)[0]["next"] is None


async def test_the_channel_list_never_reaches_the_recorder(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Asserted through the function the recorder stores attributes with.

    Home Assistant excludes `source_list` and a select's `options` on its own; `channels`
    is an attribute of ours, so it is recorded unless this integration says otherwise.
    Reading `_unrecorded_attributes` back would only prove the set was written. This
    takes the real state, carries it in the event shape the recorder reads, and asks the
    recorder's own encoder what it would store.
    """
    freezer.move_to(TEN_MINUTES_IN)
    _capable_box(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    state = hass.states.get(SENSOR)
    assert state.attributes["channels"]

    event: Event[EventStateChangedData] = Event(
        EVENT_STATE_CHANGED,
        {"entity_id": SENSOR, "old_state": None, "new_state": state},
    )
    stored = StateAttributes.shared_attrs_bytes_from_event(event, None)

    # 🔴 An over-limit state comes back as `b"{}"`, so an absent list alone is not
    # good news: the rest of the attributes have to still be there.
    assert b"friendly_name" in stored
    assert b"channels" not in stored
    assert b"Wiadomo" not in stored
    # And the list really is on the state; it is the recorder that leaves it out.
    assert b"Wiadomo" in json_bytes(dict(state.attributes))


async def test_the_sensor_is_unavailable_until_the_context_arrives(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """No context yet is not „no bouquet": nothing has been said."""
    _capable_box(box_on_the_broker)
    del box_on_the_broker[BOUQUET_TOPIC]
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(SENSOR).state == STATE_UNAVAILABLE


async def _move_clock(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    """Move the frozen clock and let every timer due by then fire."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()

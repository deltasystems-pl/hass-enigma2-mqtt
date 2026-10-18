"""The receiver's recordings: the running ones, timeshift, the library, play and delete.

A plugin after 0.4.0 publishes each running recording in detail, the timeshift state, an
index of its recording library and one topic per folder, and takes `cmd/play_recording`,
`cmd/delete_recording` and `cmd/recordings_refresh`. The receiver lists every recording;
what the hidden-bouquets option keeps out is kept out here - from the media browser, the
attributes and the counts - and a hidden recording can be neither played nor deleted.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components import media_source, mqtt
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DATA_COMPONENT,
    BrowseError,
)
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt import (
    button as button_module,
    recordings,
    services as services_module,
)
from custom_components.enigma2_mqtt.box import Enigma2CommandError
from custom_components.enigma2_mqtt.const import DOMAIN
from custom_components.enigma2_mqtt.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    AVAILABILITY_TOPIC,
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    RECORDING_TOPIC,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    async_setup_box_then_retained,
    command_topic,
    opened_command,
    published_payloads,
)

INDEX_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/recordings"
TIMESHIFT_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/timeshift"
PLAYER = "media_player.dekoder_salon"
TIMESHIFT = "binary_sensor.dekoder_salon_timeshift"
FREE_SPACE = "sensor.dekoder_salon_free_recording_space"
LIBRARY = "sensor.dekoder_salon_recordings_in_library"
ACTIVE = "sensor.dekoder_salon_active_recordings"
REFRESH = "button.dekoder_salon_refresh_recordings"
RECEIVER = f"media-source://{DOMAIN}/{NODE_ID}"
EUROSPORT = "1:0:19:1234:3F3:1:C00000:0:0:0:"
LOCATION = "/media/hdd/movie"
USB = "/media/usb/movie"

NEWS: dict[str, Any] = {
    "id": "3f9c2e4a1b7d5c60",
    "file": "20261017 2015 - TVP 1 HD - Wiadomosci.ts",
    "name": "Wiadomosci",
    "description": "Short text",
    "service_name": "TVP 1 HD",
    "sref": SREF,
    "bouquet": "Ulubione TV",
    "begin": 1792050000,
    "duration": 3600,
    "size_mb": 2210,
}
MATCH: dict[str, Any] = {
    "id": "a1b2c3d4e5f60718",
    "file": "20261016 2000 - Eurosport 1 - Mecz.ts",
    "name": "Mecz",
    "description": None,
    "service_name": "Eurosport 1",
    "sref": EUROSPORT,
    "bouquet": "Sport",
    "begin": 1791964800,
    "duration": 7200,
    "size_mb": 5000,
}
# A recording whose service the receiver found in no published bouquet.
NO_BOUQUET: dict[str, Any] = {
    "id": "0123456789abcdef",
    "file": "20261015 1800 - Kanal - Film.ts",
    "name": "Film",
    "description": None,
    "service_name": "Kanal",
    "sref": SREF_TWO,
    "bouquet": None,
    "begin": 1791878400,
    "duration": 5400,
    "size_mb": 3000,
}
EPISODE: dict[str, Any] = {
    "id": "fedcba9876543210",
    "file": "20261014 2100 - TVN HD - Serial.ts",
    "name": "Serial",
    "description": None,
    "service_name": "TVN HD",
    "sref": SREF_TWO,
    "bouquet": "Ulubione TV",
    "begin": 1791795600,
    "duration": 2700,
    "size_mb": 1200,
}

INDEX: dict[str, Any] = {
    "read": 1792050000,
    "reason": "start",
    "locations": [
        {
            "path": LOCATION,
            "mounted": True,
            "free_mb": 412330,
            "folders": [
                {"slug": "movie", "path": "", "count": 3, "size_mb": 10210, "cut": 0},
                {"slug": "movie_series", "path": "Series", "count": 1, "size_mb": 1200, "cut": 4},
            ],
        }
    ],
}
USB_LOCATION: dict[str, Any] = {
    "path": USB,
    "mounted": False,
    "free_mb": None,
    "folders": [{"slug": "usb_movie", "path": "", "count": 0, "size_mb": 0, "cut": 0}],
}
FOLDER_MOVIE = {"recordings": [NEWS, MATCH, NO_BOUQUET]}
FOLDER_SERIES = {"recordings": [EPISODE]}

LIBRARY_CAPABILITIES = ("recordings", "recording_play", "recording_details", "timeshift")


def folder_topic(slug: str) -> str:
    return f"{BASE_TOPIC}/{NODE_ID}/recordings/{slug}"


def info_with(*capabilities: str, **extra: Any) -> str:
    return json.dumps(
        {**INFO, "capabilities": [*INFO["capabilities"], *capabilities], **extra}
    )


def hiding(entry: MockConfigEntry, *bouquets: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        options={"history_hidden_bouquets": list(bouquets)},
    )


def library(
    store: dict[str, str | bytes],
    *capabilities: str,
    index: dict[str, Any] = INDEX,
    **info: Any,
) -> None:
    store[INFO_TOPIC] = info_with(*(capabilities or LIBRARY_CAPABILITIES), **info)
    store[INDEX_TOPIC] = json.dumps(index)
    store[folder_topic("movie")] = json.dumps(FOLDER_MOVIE)
    store[folder_topic("movie_series")] = json.dumps(FOLDER_SERIES)


def deletable(store: dict[str, str | bytes], **info: Any) -> None:
    library(
        store,
        *LIBRARY_CAPABILITIES,
        "recording_delete",
        settings={"recording_delete_allowed": True},
        recording_trash=True,
        **info,
    )


def _stored_attributes(state) -> bytes:
    """Return what the recorder would store as this state's attributes."""
    event: Event[EventStateChangedData] = Event(
        EVENT_STATE_CHANGED,
        {"entity_id": state.entity_id, "old_state": None, "new_state": state},
    )
    return StateAttributes.shared_attrs_bytes_from_event(event, None)


def _player(hass: HomeAssistant):
    return hass.data[DATA_COMPONENT].get_entity(PLAYER)


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, store: dict[str, str | bytes]
) -> None:
    assert await async_setup_component(hass, "media_source", {})
    await async_setup_box_then_retained(hass, entry, store)


async def _play(hass: HomeAssistant, content_type: str, content_id: str) -> None:
    await hass.services.async_call(
        "media_player",
        "play_media",
        {
            ATTR_ENTITY_ID: PLAYER,
            ATTR_MEDIA_CONTENT_TYPE: content_type,
            ATTR_MEDIA_CONTENT_ID: content_id,
        },
        blocking=True,
    )


async def _delete(hass: HomeAssistant, rec_id: str) -> None:
    await hass.services.async_call(
        DOMAIN, "delete_recording", {ATTR_ENTITY_ID: PLAYER, "id": rec_id}, blocking=True
    )


async def _arm(hass: HomeAssistant, command: str, answer) -> list[tuple[str | None, str]]:
    """Make the fake receiver answer one command; return what it was sent."""
    received: list[tuple[str | None, str]] = []

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        command_id, payload = opened_command(msg.payload)
        received.append((command_id, payload))
        answer(command_id, payload)

    await mqtt.async_subscribe(hass, command_topic(command), _command_received)
    return received


def _refuse(hass: HomeAssistant, command: str, reason: str | None):
    def answer(command_id: str | None, _payload: str) -> None:
        refusal: dict[str, Any] = {"cmd": command, "error": "refused", "ts": 1792050100}
        if reason is not None:
            refusal["reason"] = reason
        if command_id is not None:
            refusal["id"] = command_id
        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(refusal))

    return answer


# ---------------------------------------------------------------------- parsing


def test_a_recording_id_is_sixteen_hex_digits() -> None:
    assert recordings.parse_recording_id("3f9c2e4a1b7d5c60") == "3f9c2e4a1b7d5c60"
    assert recordings.parse_recording_id(" 3F9C2E4A1B7D5C60 ") == "3f9c2e4a1b7d5c60"
    for wrong in ("3f9c2e4a1b7d5c6", "3f9c2e4a1b7d5c600", "3f9c2e4a1b7d5c6g", "", None, 16):
        assert recordings.parse_recording_id(wrong) is None
    assert recordings.parse_recording_id("/media/hdd/movie/x.ts") is None


def test_the_index_keeps_what_the_contract_names() -> None:
    index = recordings.normalise_index(
        {
            "read": True,
            "reason": "x" * 33,
            "extra": "dropped",
            "locations": [
                "not a location",
                {"mounted": True},
                {"path": "", "mounted": True},
                {"path": 5, "mounted": True},
                {
                    "path": LOCATION,
                    "mounted": "yes",
                    "free_mb": -1,
                    "secret": 1,
                    "folders": [
                        {"slug": "movie", "path": "", "count": True, "size_mb": 1, "cut": 2},
                        {"slug": "movie", "path": "again", "count": 1},
                        {"slug": "a/b", "path": "x"},
                        {"slug": "a+", "path": "x"},
                        {"slug": " pad", "path": "x"},
                        {"slug": "series", "path": 5, "count": 7},
                    ],
                },
            ],
        }
    )
    assert index == {
        "read": None,
        "reason": None,
        "locations": [
            {
                "path": LOCATION,
                "mounted": None,
                "free_mb": None,
                "folders": [
                    {"slug": "movie", "path": "", "count": None, "size_mb": 1, "cut": 2},
                    {"slug": "series", "path": "", "count": 7, "size_mb": None, "cut": None},
                ],
            }
        ],
    }
    assert recordings.normalise_index(INDEX)["reason"] == "start"
    assert recordings.normalise_index({"read": 1792050000, "reason": "later"})["reason"] == (
        "later"
    )


def test_a_folder_keeps_recordings_with_an_id_and_a_file_and_drops_the_rest() -> None:
    raw = {
        "recordings": [
            {**NEWS, "description": "d" * 300, "secret": "dropped"},
            {**NEWS, "file": "duplicate id.ts"},
            {**MATCH, "id": "not an id"},
            {**MATCH, "file": ""},
            {**MATCH, "begin": True, "duration": 10**9, "size_mb": -5, "sref": 3},
            "nonsense",
        ]
    }
    folder = recordings.normalise_folder(raw)
    assert [recording["id"] for recording in folder] == [NEWS["id"], MATCH["id"]]
    assert folder[0]["description"] == "d" * 200
    assert "secret" not in folder[0]
    assert folder[1]["begin"] is None
    assert folder[1]["duration"] is None
    assert folder[1]["size_mb"] is None
    assert folder[1]["sref"] is None
    assert folder[1]["bouquet"] == "Sport"
    assert recordings.normalise_folder({"recordings": "x"}) == []


def test_timeshift_without_a_buffer_says_nothing_else() -> None:
    assert recordings.normalise_timeshift(
        {"active": False, "paused": True, "behind": True, "since": 1792050000}
    ) == {"active": False, "paused": None, "behind": None, "since": None}
    assert recordings.normalise_timeshift(
        {"active": True, "paused": False, "behind": True, "since": 1792050000}
    ) == {"active": True, "paused": False, "behind": True, "since": 1792050000}
    assert recordings.normalise_timeshift({"active": 1, "paused": "no"}) == {
        "active": None, "paused": None, "behind": None, "since": None,
    }
    assert recordings.normalise_timeshift({"active": True, "since": 0}) == {
        "active": True, "paused": None, "behind": None, "since": None,
    }


def test_a_recording_path_reads_a_bare_name_against_its_folder() -> None:
    location = {"path": "/media/hdd/movie/"}
    top = {"path": "", "slug": "movie"}
    series = {"path": "Series", "slug": "movie_series"}
    assert recordings.recording_path(location, top, {"file": "a.ts"}) == "/media/hdd/movie/a.ts"
    assert (
        recordings.recording_path(location, series, {"file": "a.ts"})
        == "/media/hdd/movie/Series/a.ts"
    )
    assert (
        recordings.recording_path(location, series, {"file": "Series/a.ts"})
        == "/media/hdd/movie/Series/a.ts"
    )
    playing = {"sref": "1:0:0:0:0:0:0:0:0:0:/media/hdd/movie/Series/a.ts"}
    assert recordings.plays(playing, location, series, {"file": "a.ts"}) is True
    assert recordings.plays(playing, location, top, {"file": "a.ts"}) is False
    assert recordings.plays(None, location, top, {"file": "a.ts"}) is False


async def test_a_recording_is_titled_with_its_channel_date_and_length(
    hass: HomeAssistant,
) -> None:
    begin = dt_util.as_local(dt_util.utc_from_timestamp(NEWS["begin"])).strftime(
        "%Y-%m-%d %H:%M"
    )
    normalised = recordings.normalise_folder({"recordings": [NEWS]})[0]
    assert recordings.recording_title(normalised) == (
        f"Wiadomosci - TVP 1 HD - {begin} (60 min)"
    )
    bare = recordings.normalise_folder({"recordings": [{"id": NEWS["id"], "file": "x/y.ts"}]})
    assert recordings.recording_title(bare[0]) == "y.ts"


# ---------------------------------------------------------------- the entities


async def test_without_the_capabilities_there_are_no_new_entities(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box_on_the_broker[INDEX_TOPIC] = json.dumps(INDEX)
    box_on_the_broker[TIMESHIFT_TOPIC] = json.dumps({"active": True})
    await _setup(hass, config_entry, box_on_the_broker)

    registry = er.async_get(hass)
    for platform, key in (
        ("binary_sensor", "timeshift"),
        ("sensor", "recording_free_space"),
        ("sensor", "recording_library"),
        ("button", "recordings_refresh"),
    ):
        assert registry.async_get_entity_id(platform, DOMAIN, f"{NODE_ID}_{key}") is None
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(hass, f"{RECEIVER}/r")


async def test_timeshift_is_on_with_a_buffer_and_says_how(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    box_on_the_broker[TIMESHIFT_TOPIC] = json.dumps(
        {"active": True, "paused": True, "behind": True, "since": 1792050000}
    )
    await _setup(hass, config_entry, box_on_the_broker)

    state = hass.states.get(TIMESHIFT)
    assert state.state == "on"
    assert state.attributes["paused"] is True
    assert state.attributes["behind"] is True
    assert state.attributes["since"] == dt_util.utc_from_timestamp(1792050000).isoformat()

    async_fire_mqtt_message(hass, TIMESHIFT_TOPIC, json.dumps({"active": False}))
    await hass.async_block_till_done()
    state = hass.states.get(TIMESHIFT)
    assert state.state == "off"
    assert state.attributes["paused"] is None
    assert state.attributes["since"] is None

    async_fire_mqtt_message(hass, TIMESHIFT_TOPIC, "")
    await hass.async_block_till_done()
    assert hass.states.get(TIMESHIFT).state == "unknown"


async def test_the_free_space_is_the_first_location_and_every_location_is_listed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    index = {**INDEX, "locations": [*INDEX["locations"], USB_LOCATION]}
    library(box_on_the_broker, index=index)
    await _setup(hass, config_entry, box_on_the_broker)

    state = hass.states.get(FREE_SPACE)
    # Shown in GiB, kept in MiB.
    assert float(state.state) == pytest.approx(412330 / 1024, abs=0.01)
    assert state.attributes["locations"] == [
        {"path": LOCATION, "mounted": True, "free_mb": 412330},
        {"path": USB, "mounted": False, "free_mb": None},
    ]
    assert b"locations" not in _stored_attributes(state)


async def test_the_library_counts_what_it_shows(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    state = hass.states.get(LIBRARY)
    assert state.state == "4"
    assert state.attributes["left_out"] == 4
    assert state.attributes["folder_count"] == 2
    assert state.attributes["location_count"] == 1
    assert state.attributes["reason"] == "start"
    assert state.attributes["read"] == dt_util.utc_from_timestamp(1792050000).isoformat()

    # A folder that goes is gone from the count: its topic retracted, then the index.
    async_fire_mqtt_message(hass, folder_topic("movie_series"), "")
    await hass.async_block_till_done()
    assert hass.states.get(LIBRARY).state == "3"


async def test_a_hidden_bouquet_leaves_the_count_the_browser_and_the_actions(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The option hides by bouquet name, and a recording with no bouquet fails toward hiding."""
    deletable(box_on_the_broker)
    await _setup(hass, hiding(config_entry, "Sport"), box_on_the_broker)

    # NEWS and EPISODE are shown; MATCH is in the hidden bouquet and NO_BOUQUET cannot be
    # checked against the option.
    assert hass.states.get(LIBRARY).state == "2"
    folder = await media_source.async_browse_media(hass, f"{RECEIVER}/f/movie")
    assert [child.identifier for child in folder.children] == [f"{NODE_ID}/p/{NEWS['id']}"]

    for hidden in (MATCH["id"], NO_BOUQUET["id"]):
        with pytest.raises(ServiceValidationError) as raised:
            await _play(hass, "recording", hidden)
        assert raised.value.translation_key == "recording_unknown"
        with pytest.raises(ServiceValidationError) as raised:
            await _delete(hass, hidden)
        assert raised.value.translation_key == "recording_unknown"
    assert published_payloads(mqtt_mock, command_topic("play_recording")) == []
    assert published_payloads(mqtt_mock, command_topic("delete_recording")) == []


async def test_a_channel_of_a_hidden_bouquet_stays_hidden_under_another_bouquet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    box_on_the_broker[folder_topic("movie")] = json.dumps(
        {"recordings": [{**MATCH, "bouquet": "Ulubione TV"}, NEWS]}
    )
    await _setup(hass, hiding(config_entry, "Sport"), box_on_the_broker)

    folder = await media_source.async_browse_media(hass, f"{RECEIVER}/f/movie")
    assert [child.identifier for child in folder.children] == [f"{NODE_ID}/p/{NEWS['id']}"]


async def test_without_the_option_a_recording_with_no_bouquet_is_shown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    folder = await media_source.async_browse_media(hass, f"{RECEIVER}/f/movie")
    assert len(folder.children) == 3


async def test_running_recordings_carry_their_details(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    entry = {
        "name": "Wiadomosci",
        "sref": SREF,
        "begin": 1792050000,
        "end": 1792053600,
        "bouquet": "Ulubione TV",
        "service_name": "TVP 1 HD",
        "description": "x" * 250,
        "location": LOCATION,
        "file": NEWS["file"],
        "id": NEWS["id"],
        "instant": True,
        "secret": "dropped",
    }
    box_on_the_broker[RECORDING_TOPIC] = json.dumps({"active": [entry], "next": None})
    await _setup(hass, config_entry, box_on_the_broker)

    state = hass.states.get(ACTIVE)
    assert state.state == "1"
    (row,) = state.attributes["recordings"]
    assert row == {
        **{key: value for key, value in entry.items() if key != "secret"},
        "description": "x" * 200,
        "hidden": False,
    }
    assert b"Wiadomosci" not in _stored_attributes(state)


async def test_without_details_the_running_recordings_keep_their_shape(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    entry = {"name": "Wiadomosci", "sref": SREF, "begin": 1, "end": 2, "file": "x.ts"}
    box_on_the_broker[RECORDING_TOPIC] = json.dumps({"active": [entry], "next": None})
    await _setup(hass, config_entry, box_on_the_broker)

    assert hass.states.get(ACTIVE).attributes["recordings"] == [
        {"name": "Wiadomosci", "sref": SREF, "begin": 1, "end": 2, "hidden": False}
    ]


async def test_a_hidden_running_recording_is_counted_and_not_named(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    hidden = {
        "name": "Mecz", "sref": EUROSPORT, "begin": 1, "end": 2, "bouquet": "Sport",
        "service_name": "Eurosport 1", "description": "d", "location": LOCATION,
        "file": MATCH["file"], "id": MATCH["id"], "instant": False,
    }
    box_on_the_broker[RECORDING_TOPIC] = json.dumps({"active": [hidden], "next": None})
    await _setup(hass, hiding(config_entry, "Sport"), box_on_the_broker)

    state = hass.states.get(ACTIVE)
    assert state.state == "1"
    assert state.attributes["recordings"] == [
        {
            "name": None, "sref": None, "begin": 1, "end": 2, "bouquet": None,
            "service_name": None, "description": None, "location": None, "file": None,
            "id": None, "instant": False, "hidden": True,
        }
    ]
    assert hass.states.get("binary_sensor.dekoder_salon_recording").state == "on"


# ------------------------------------------------------------------- refresh


async def test_refresh_asks_and_waits_for_a_new_read(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    entry = er.async_get(hass).async_get(REFRESH)
    assert entry.entity_category == "config"

    sent = await _arm(
        hass,
        "recordings_refresh",
        lambda _id, _payload: async_fire_mqtt_message(
            hass, INDEX_TOPIC, json.dumps({**INDEX, "read": 1792060000, "reason": "refresh"})
        ),
    )
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: REFRESH}, blocking=True
    )
    assert [payload for _id, payload in sent] == ["PRESS"]
    assert hass.states.get(LIBRARY).attributes["reason"] == "refresh"


async def test_a_refresh_that_finds_the_same_library_still_answers(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receiver that publishes the index it already had is still answering the press."""
    monkeypatch.setattr(button_module, "RECORDINGS_READ_TIMEOUT", 1)
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    await _arm(
        hass,
        "recordings_refresh",
        lambda _id, _payload: async_fire_mqtt_message(hass, INDEX_TOPIC, json.dumps(INDEX)),
    )
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: REFRESH}, blocking=True
    )


async def test_a_replayed_index_is_not_the_answer_to_a_refresh(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(button_module, "RECORDINGS_READ_TIMEOUT", 0.1)
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    box = config_entry.runtime_data

    # What a resubscription after a reconnect hands over: the index the broker holds, with
    # the retain flag set. Given to the box directly, because the test broker hands each
    # subscription a retained topic once.
    def replay(_id: str | None, _payload: str) -> None:
        box._message_received(
            ReceiveMessage(
                INDEX_TOPIC,
                json.dumps({**INDEX, "read": 1792060000}).encode(),
                0,
                True,
                f"{BASE_TOPIC}/{NODE_ID}/#",
                0.0,
            )
        )

    await _arm(hass, "recordings_refresh", replay)
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: REFRESH}, blocking=True
        )
    assert raised.value.translation_key == "command_timeout"
    # The replay was read - the sensor shows it - and did not answer the press.
    assert box.state.recordings_index["read"] == 1792060000


# ---------------------------------------------------------------- the browser


async def test_the_player_offers_the_library_beside_its_bouquets(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    root = await _player(hass).async_browse_media()
    assert [child.title for child in root.children] == ["Recordings", "Ulubione TV", "Sport"]
    recordings_dir = root.children[0]
    assert recordings_dir.media_content_id == f"{RECEIVER}/r"

    # One location: its folders straight away.
    top = await _player(hass).async_browse_media(
        media_content_id=recordings_dir.media_content_id
    )
    assert [child.title for child in top.children] == ["movie", "Series"]
    folder = await _player(hass).async_browse_media(
        media_content_id=top.children[1].media_content_id
    )
    assert folder.not_shown == 4
    (episode,) = folder.children
    assert episode.media_content_id == f"{RECEIVER}/p/{EPISODE['id']}"
    assert episode.media_content_type == "recording"
    assert episode.can_play is True
    assert episode.title.startswith("Serial - TVN HD - ")


async def test_the_library_is_not_offered_to_other_players(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker, *LIBRARY_CAPABILITIES, "stream")
    await _setup(hass, config_entry, box_on_the_broker)

    receiver = await media_source.async_browse_media(hass, RECEIVER)
    assert [child.title for child in receiver.children] == ["Ulubione TV", "Sport"]
    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(
            hass, f"{RECEIVER}/p/{NEWS['id']}", "media_player.kodi"
        )
    assert raised.value.translation_key == "recording_plays_on_receiver"


async def test_several_locations_are_a_level_of_their_own(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker, index={**INDEX, "locations": [*INDEX["locations"], USB_LOCATION]})
    await _setup(hass, config_entry, box_on_the_broker)

    top = await media_source.async_browse_media(hass, f"{RECEIVER}/r")
    assert [child.title for child in top.children] == [LOCATION, USB]
    usb = await media_source.async_browse_media(hass, top.children[1].media_content_id)
    assert [child.title for child in usb.children] == ["movie"]
    assert usb.children[0].identifier == f"{NODE_ID}/f/usb_movie"
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(hass, f"{RECEIVER}/l/%2Fmedia%2Fgone")


async def test_a_folder_the_index_does_not_name_is_not_browsed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    box_on_the_broker[folder_topic("stale")] = json.dumps(FOLDER_SERIES)
    await _setup(hass, config_entry, box_on_the_broker)

    with pytest.raises(BrowseError):
        await media_source.async_browse_media(hass, f"{RECEIVER}/f/stale")
    # Its topic is kept - the index may name it next - and read for nothing until then.
    box = config_entry.runtime_data
    assert box.state.recording_folders["stale"]
    assert box.recordings_in("stale") == []

    # A folder whose topic was retracted lists nothing until it is published again.
    async_fire_mqtt_message(hass, folder_topic("movie"), "")
    await hass.async_block_till_done()
    folder = await media_source.async_browse_media(hass, f"{RECEIVER}/f/movie")
    assert folder.children == []


async def test_without_play_the_recordings_are_shown_and_not_playable(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker, "recordings")
    await _setup(hass, config_entry, box_on_the_broker)

    folder = await media_source.async_browse_media(hass, f"{RECEIVER}/f/movie")
    assert folder.children
    assert not any(child.can_play for child in folder.children)
    with pytest.raises(ServiceValidationError) as raised:
        await _play(hass, "recording", NEWS["id"])
    assert raised.value.translation_key == "recording_play_unsupported"


async def test_a_receiver_without_the_library_is_not_browsed_by_its_id(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box_on_the_broker[INDEX_TOPIC] = json.dumps(INDEX)
    await _setup(hass, config_entry, box_on_the_broker)

    root = await _player(hass).async_browse_media()
    assert [child.title for child in root.children] == ["Ulubione TV", "Sport"]
    with pytest.raises(BrowseError):
        await _player(hass).async_browse_media(media_content_id=f"{RECEIVER}/r")


async def test_another_receivers_browse_ids_are_not_this_players(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    with pytest.raises(BrowseError):
        await _player(hass).async_browse_media(
            media_content_id=f"media-source://{DOMAIN}/vuuno4kse_005302/r"
        )
    with pytest.raises(ServiceValidationError) as raised:
        await _play(hass, "video", f"media-source://{DOMAIN}/vuuno4kse_005302/p/{NEWS['id']}")
    assert raised.value.translation_key == "recording_other_receiver"


# ----------------------------------------------------------------------- play


def _plays(hass: HomeAssistant, path: str):
    def answer(_command_id: str | None, _payload: str) -> None:
        async_fire_mqtt_message(
            hass,
            SERVICE_TOPIC,
            json.dumps({**SERVICE, "sref": f"1:0:0:0:0:0:0:0:0:0:{path}", "name": "x"}),
        )

    return answer


async def test_play_from_the_browser_sends_the_id_and_waits_for_the_service(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    sent = await _arm(hass, "play_recording", _plays(hass, f"{LOCATION}/Series/{EPISODE['file']}"))
    await _play(hass, "recording", f"{RECEIVER}/p/{EPISODE['id']}")
    assert [json.loads(payload) for _id, payload in sent] == [{"id": EPISODE["id"]}]


async def test_play_by_the_bare_id_from_an_automation(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    sent = await _arm(hass, "play_recording", _plays(hass, f"{LOCATION}/{NEWS['file']}"))
    await _play(hass, "recording", NEWS["id"].upper())
    assert [json.loads(payload) for _id, payload in sent] == [{"id": NEWS["id"]}]


async def test_a_recording_already_playing_is_played_again_and_proved_at_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(
        {**SERVICE, "sref": f"1:0:0:0:0:0:0:0:0:0:{LOCATION}/{NEWS['file']}"}
    )
    await _setup(hass, config_entry, box_on_the_broker)

    await _play(hass, "recording", NEWS["id"])
    assert [
        json.loads(opened_command(payload)[1])
        for payload in published_payloads(mqtt_mock, command_topic("play_recording"))
    ] == [{"id": NEWS["id"]}]


async def test_play_that_never_shows_on_the_service_times_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.enigma2_mqtt import box as box_module

    monkeypatch.setattr(box_module.Enigma2Box.async_command, "__kwdefaults__", {
        **box_module.Enigma2Box.async_command.__kwdefaults__, "timeout": 0.1,
    })
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    # Another file starts playing: not the recording asked for.
    await _arm(hass, "play_recording", _plays(hass, f"{LOCATION}/other.ts"))
    with pytest.raises(Enigma2CommandError) as raised:
        await _play(hass, "recording", NEWS["id"])
    assert raised.value.translation_key == "command_timeout"


@pytest.mark.parametrize(
    ("reason", "key"),
    [
        ("file_gone", "play_recording_file_gone"),
        ("not_in_library", "play_recording_not_in_library"),
        (None, "command_refused"),
        ("from_a_later_plugin", "command_refused"),
    ],
)
async def test_a_refused_play_is_said_in_the_households_language(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reason: str | None,
    key: str,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    await _arm(hass, "play_recording", _refuse(hass, "play_recording", reason))
    with pytest.raises(HomeAssistantError) as raised:
        await _play(hass, "recording", NEWS["id"])
    assert raised.value.translation_key == key


@pytest.mark.parametrize(
    ("rec_id", "key"),
    [("nonsense", "recording_id_invalid"), ("00000000000000aa", "recording_unknown")],
)
async def test_play_of_what_the_library_does_not_list_sends_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    rec_id: str,
    key: str,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    with pytest.raises(ServiceValidationError) as raised:
        await _play(hass, "recording", rec_id)
    assert raised.value.translation_key == key
    assert published_payloads(mqtt_mock, command_topic("play_recording")) == []


async def test_nothing_is_sent_to_a_receiver_that_is_offline(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    deletable(box_on_the_broker)
    box_on_the_broker[AVAILABILITY_TOPIC] = "offline"
    await _setup(hass, config_entry, box_on_the_broker)

    for call in (_play(hass, "recording", NEWS["id"]), _delete(hass, NEWS["id"])):
        with pytest.raises(ServiceValidationError) as raised:
            await call
        assert raised.value.translation_key == "recording_receiver_offline"
    assert published_payloads(mqtt_mock, command_topic("play_recording")) == []
    assert published_payloads(mqtt_mock, command_topic("delete_recording")) == []


# --------------------------------------------------------------------- delete


@pytest.mark.parametrize(
    ("info", "key"),
    [
        ({}, "recording_delete_unsupported"),
        ({"settings": {"recording_delete_allowed": False}}, "recording_delete_not_permitted"),
        (
            {"settings": {"recording_delete_allowed": False}, "recording_trash": False},
            "recording_delete_not_permitted",
        ),
        (
            {"settings": {"recording_delete_allowed": True}, "recording_trash": False},
            "recording_delete_trash_off",
        ),
        (
            {"settings": {"recording_delete_allowed": True}, "recording_trash": None},
            "recording_delete_unsupported",
        ),
    ],
)
async def test_delete_is_refused_unless_the_receiver_claims_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    info: dict[str, Any],
    key: str,
) -> None:
    library(box_on_the_broker, *LIBRARY_CAPABILITIES, **info)
    await _setup(hass, config_entry, box_on_the_broker)

    with pytest.raises(ServiceValidationError) as raised:
        await _delete(hass, NEWS["id"])
    assert raised.value.translation_key == key
    assert published_payloads(mqtt_mock, command_topic("delete_recording")) == []


async def test_a_retracted_info_claims_no_delete(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    deletable(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    async_fire_mqtt_message(hass, INFO_TOPIC, "")
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as raised:
        await _delete(hass, NEWS["id"])
    # The last good `info` still says the permission and the trash were on.
    assert raised.value.translation_key == "recording_delete_unsupported"


async def test_delete_sends_the_id_and_waits_for_the_library_without_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    deletable(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    def answer(_command_id: str | None, _payload: str) -> None:
        async_fire_mqtt_message(hass, INDEX_TOPIC, json.dumps({**INDEX, "reason": "delete"}))
        async_fire_mqtt_message(
            hass, folder_topic("movie"), json.dumps({"recordings": [MATCH, NO_BOUQUET]})
        )

    sent = await _arm(hass, "delete_recording", answer)
    await _delete(hass, NEWS["id"])
    assert [json.loads(payload) for _id, payload in sent] == [{"id": NEWS["id"]}]
    assert hass.states.get(LIBRARY).state == "3"


async def test_a_delete_the_library_never_shows_times_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence is not a delete: the library has to be read again without the recording."""
    monkeypatch.setattr(services_module, "RECORDINGS_READ_TIMEOUT", 0.1)
    deletable(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    sent = await _arm(hass, "delete_recording", lambda _id, _payload: None)
    with pytest.raises(Enigma2CommandError) as raised:
        await _delete(hass, NEWS["id"].upper())
    assert raised.value.translation_key == "command_timeout"
    # Sent as the receiver writes it, whatever was typed.
    assert [json.loads(payload) for _id, payload in sent] == [{"id": NEWS["id"]}]


async def test_a_recording_being_recorded_is_not_deleted(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    deletable(box_on_the_broker)
    box_on_the_broker[RECORDING_TOPIC] = json.dumps(
        {"active": [{"name": "x", "sref": SREF, "id": NEWS["id"]}], "next": None}
    )
    await _setup(hass, config_entry, box_on_the_broker)

    with pytest.raises(ServiceValidationError) as raised:
        await _delete(hass, NEWS["id"])
    assert raised.value.translation_key == "delete_recording_in_use"
    assert published_payloads(mqtt_mock, command_topic("delete_recording")) == []


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("in_use", ServiceValidationError),
        ("trash_off", ServiceValidationError),
        ("not_permitted", ServiceValidationError),
        ("file_gone", ServiceValidationError),
    ],
)
async def test_a_refused_delete_is_said_in_the_households_language(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reason: str,
    kind: type[Exception],
) -> None:
    deletable(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    await _arm(hass, "delete_recording", _refuse(hass, "delete_recording", reason))
    with pytest.raises(kind) as raised:
        await _delete(hass, NEWS["id"])
    assert raised.value.translation_key == f"delete_recording_{reason}"


# ---------------------------------------------------------------- diagnostics


async def test_the_download_has_the_librarys_shape_and_no_title_or_path(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    box_on_the_broker[TIMESHIFT_TOPIC] = json.dumps({"active": True, "paused": False})
    await _setup(hass, config_entry, box_on_the_broker)

    data = await async_get_config_entry_diagnostics(hass, config_entry)
    topics = data["topics"]
    assert topics["timeshift"] == {"active": True, "paused": False, "behind": None, "since": None}
    assert topics["recording_folders"] == {"movie": 3, "movie_series": 1}
    assert topics["recordings"]["locations"][0]["free_mb"] == 412330
    dumped = json.dumps(topics["recordings"]) + json.dumps(topics["recording_folders"])
    for leaked in (LOCATION, "Series", NEWS["name"], NEWS["file"], NEWS["id"]):
        assert leaked not in dumped

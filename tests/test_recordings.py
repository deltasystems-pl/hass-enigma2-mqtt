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
    box as box_module,
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
    CHANNELS,
    CHANNELS_TOPIC,
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
    # Relative to the movie location, in a subfolder as well.
    "file": "Series/20261014 2100 - TVN HD - Serial.ts",
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
    "mounted": True,
    "free_mb": 1000,
    "folders": [{"slug": "usb_movie", "path": "", "count": 1, "size_mb": 900, "cut": 0}],
}
USB_FILM: dict[str, Any] = {**NEWS, "id": "1111222233334444", "file": "usb.ts", "name": "Usb"}
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


def test_a_recording_path_is_its_file_under_its_location() -> None:
    location = {"path": "/media/hdd/movie/"}
    assert recordings.recording_path(location, {"file": "a.ts"}) == "/media/hdd/movie/a.ts"
    assert (
        recordings.recording_path(location, {"file": "Series/a.ts"})
        == "/media/hdd/movie/Series/a.ts"
    )
    playing = {"sref": "1:0:0:0:0:0:0:0:0:0:/media/hdd/movie/Series/a.ts"}
    assert recordings.plays(playing, location, {"file": "Series/a.ts"}) is True
    # A bare name is at the location's top, not in whatever folder lists it.
    assert recordings.plays(playing, location, {"file": "a.ts"}) is False
    assert recordings.plays(None, location, {"file": "a.ts"}) is False


def test_every_list_has_its_bound() -> None:
    folders = [{"slug": f"s{n}", "path": ""} for n in range(recordings.FOLDERS_MAX + 5)]
    index = recordings.normalise_index(
        {
            "locations": [
                {"path": f"/media/l{n}", "folders": folders if n == 0 else []}
                for n in range(recordings.LOCATIONS_MAX + 3)
            ]
        }
    )
    assert len(index["locations"]) == recordings.LOCATIONS_MAX
    assert sum(len(location["folders"]) for location in index["locations"]) == (
        recordings.FOLDERS_MAX
    )
    many = [
        {"id": f"{n:016x}", "file": f"{n}.ts"} for n in range(recordings.RECORDINGS_MAX + 2)
    ]
    assert len(recordings.normalise_folder({"recordings": many})) == recordings.RECORDINGS_MAX


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


async def test_the_free_space_is_the_default_location_s_and_the_shown_ones_are_listed(
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
    # The USB location has nothing to show: its folder topic has not arrived.
    assert state.attributes["locations"] == [
        {"path": LOCATION, "mounted": True, "free_mb": 412330},
    ]
    assert b"locations" not in _stored_attributes(state)

    async_fire_mqtt_message(hass, folder_topic("usb_movie"), json.dumps({"recordings": [USB_FILM]}))
    await hass.async_block_till_done()
    assert hass.states.get(FREE_SPACE).attributes["locations"] == [
        {"path": LOCATION, "mounted": True, "free_mb": 412330},
        {"path": USB, "mounted": True, "free_mb": 1000},
    ]


async def test_a_location_with_only_hidden_recordings_is_not_named_but_the_default_is(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    sport_only = {
        "path": "/media/hdd/movie/Sport only", "mounted": True, "free_mb": 5,
        "folders": [{"slug": "sport", "path": "", "count": 1, "size_mb": 1, "cut": 0}],
    }
    library(box_on_the_broker, index={**INDEX, "locations": [*INDEX["locations"], sport_only]})
    box_on_the_broker[folder_topic("sport")] = json.dumps({"recordings": [MATCH]})
    # Everything in the default location is hidden too: it is still listed, as the state.
    entry = hiding(config_entry, "Sport", "Ulubione TV")
    await _setup(hass, entry, box_on_the_broker)

    state = hass.states.get(FREE_SPACE)
    assert state.state != "unknown"
    assert [location["path"] for location in state.attributes["locations"]] == [LOCATION]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert '"sport"' not in json.dumps(diagnostics["topics"]["recordings"])
    assert "sport" not in diagnostics["topics"]["recording_folders"]


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
    """The option hides by bouquet name; a recording with no bouquet is shown."""
    deletable(box_on_the_broker)
    await _setup(hass, hiding(config_entry, "Sport"), box_on_the_broker)

    # NEWS, NO_BOUQUET and EPISODE are shown; MATCH is in the hidden bouquet.
    assert hass.states.get(LIBRARY).state == "3"
    folder = await media_source.async_browse_media(hass, f"{RECEIVER}/f/movie")
    assert [child.identifier for child in folder.children] == [
        f"{NODE_ID}/p/{NEWS['id']}",
        f"{NODE_ID}/p/{NO_BOUQUET['id']}",
    ]

    with pytest.raises(ServiceValidationError) as raised:
        await _play(hass, "recording", MATCH["id"])
    assert raised.value.translation_key == "recording_unknown"
    with pytest.raises(ServiceValidationError) as raised:
        await _delete(hass, MATCH["id"])
    assert raised.value.translation_key == "recording_unknown"
    assert published_payloads(mqtt_mock, command_topic("play_recording")) == []
    assert published_payloads(mqtt_mock, command_topic("delete_recording")) == []

    box = hass.config_entries.async_entries("enigma2_mqtt")[0].runtime_data
    assert box.find_recording(NO_BOUQUET["id"]) is not None


async def test_a_recording_is_judged_by_the_bouquet_the_receiver_names_with_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Not by its channel: a channel of a hidden bouquet named under another one is shown."""
    library(box_on_the_broker)
    box_on_the_broker[folder_topic("movie")] = json.dumps(
        {"recordings": [{**MATCH, "bouquet": "Ulubione TV"}, NEWS]}
    )
    await _setup(hass, hiding(config_entry, "Sport"), box_on_the_broker)

    folder = await media_source.async_browse_media(hass, f"{RECEIVER}/f/movie")
    assert [child.identifier for child in folder.children] == [
        f"{NODE_ID}/p/{MATCH['id']}",
        f"{NODE_ID}/p/{NEWS['id']}",
    ]


@pytest.mark.parametrize(
    "bouquet", [None, "", "Not on the channel list", 7, ["Sport"], "sport", "Sport "]
)
async def test_only_a_recording_of_a_hidden_bouquet_by_name_is_hidden(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    bouquet: Any,
) -> None:
    """No bouquet, an unpublished one or a name that is not exactly a hidden one: shown."""
    library(box_on_the_broker)
    box_on_the_broker[folder_topic("movie")] = json.dumps(
        {"recordings": [{**MATCH, "bouquet": bouquet}]}
    )
    entry = hiding(config_entry, "Sport")
    await _setup(hass, entry, box_on_the_broker)

    assert entry.runtime_data.find_recording(MATCH["id"]) is not None


async def test_a_hidden_bouquet_without_its_channels_hides_only_its_own_recordings(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Which channels the hidden bouquet holds is not asked: the library ignores it."""
    library(box_on_the_broker)
    entry = hiding(config_entry, "Sport")
    await _setup(hass, entry, box_on_the_broker)
    assert hass.states.get(LIBRARY).state == "3"

    channels = json.loads(json.dumps(CHANNELS))
    for bouquet in channels["bouquets"]:
        if bouquet["name"] == "Sport":
            bouquet["channels"] = []
    async_fire_mqtt_message(hass, CHANNELS_TOPIC, json.dumps(channels))
    await hass.async_block_till_done()
    assert hass.states.get(LIBRARY).state == "3"
    assert entry.runtime_data.find_recording(MATCH["id"]) is None
    assert entry.runtime_data.find_recording(NO_BOUQUET["id"]) is not None


async def test_what_is_hidden_follows_the_option_without_a_reload(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Options saved without a reload are read at once: the option is part of the cache."""
    library(box_on_the_broker)
    entry = hiding(config_entry, "Sport")
    await _setup(hass, entry, box_on_the_broker)
    box = entry.runtime_data
    assert box.find_recording(MATCH["id"]) is None

    hass.config_entries.async_update_entry(entry, options={"history_hidden_bouquets": []})
    await hass.async_block_till_done()
    assert entry.runtime_data is box
    assert box.find_recording(MATCH["id"]) is not None
    hass.config_entries.async_update_entry(
        entry, options={"history_hidden_bouquets": ["Ulubione TV"]}
    )
    await hass.async_block_till_done()
    assert box.find_recording(NEWS["id"]) is None


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


async def test_an_older_plugins_running_recording_keeps_its_attributes(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Without `bouquet` there is nothing to judge: the option changes nothing here."""
    entry = {"name": "News", "sref": EUROSPORT, "begin": 1, "end": 2}
    box_on_the_broker[RECORDING_TOPIC] = json.dumps({"active": [entry], "next": None})
    await _setup(hass, hiding(config_entry, "Sport"), box_on_the_broker)

    assert hass.states.get(ACTIVE).attributes["recordings"] == [{**entry, "hidden": False}]


@pytest.mark.parametrize("bouquet", [None, "Not on the channel list", "Ulubione TV"])
async def test_a_running_recording_outside_the_hidden_bouquets_is_named(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    bouquet: Any,
) -> None:
    """The library's rule: no bouquet, or one that is not hidden, hides nothing."""
    library(box_on_the_broker)
    running = {
        "name": "Mecz", "sref": EUROSPORT, "begin": 1, "end": 2, "bouquet": bouquet,
        "service_name": "Eurosport 1", "description": "d", "location": LOCATION,
        "file": MATCH["file"], "id": MATCH["id"], "instant": False,
    }
    box_on_the_broker[RECORDING_TOPIC] = json.dumps({"active": [running], "next": None})
    await _setup(hass, hiding(config_entry, "Sport"), box_on_the_broker)

    (row,) = hass.states.get(ACTIVE).attributes["recordings"]
    assert row["hidden"] is False
    assert (row["name"], row["sref"], row["bouquet"]) == ("Mecz", EUROSPORT, bouquet)


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


async def test_a_retracted_index_empties_the_library(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, INDEX_TOPIC, "")
    await hass.async_block_till_done()
    assert hass.states.get(LIBRARY).state == "unknown"
    assert hass.states.get(FREE_SPACE).state == "unknown"
    assert await _get_recordings(hass) == {"read": None, "reason": None, "locations": []}
    with pytest.raises(ServiceValidationError) as raised:
        await _play(hass, "recording", NEWS["id"])
    assert raised.value.translation_key == "recording_unknown"


@pytest.mark.parametrize("retain", [True, False], ids=["replayed", "published again"])
async def test_the_same_index_again_wakes_nobody(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    retain: bool,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    box = config_entry.runtime_data
    before = box.updates.get("recordings", 0)

    box._message_received(
        ReceiveMessage(
            INDEX_TOPIC, json.dumps(INDEX).encode(), 0, retain, f"{BASE_TOPIC}/{NODE_ID}/#", 0.0
        )
    )
    assert box.updates.get("recordings", 0) == before


async def test_folder_topics_no_index_names_are_held_up_to_a_bound(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    box = config_entry.runtime_data
    monkeypatch.setattr(box_module, "FOLDERS_MAX", len(box.state.recording_folders) + 1)

    for slug in ("stale1", "stale2"):
        async_fire_mqtt_message(hass, folder_topic(slug), json.dumps(FOLDER_SERIES))
    await hass.async_block_till_done()
    assert "stale1" in box.state.recording_folders
    assert "stale2" not in box.state.recording_folders
    # A folder already held is still updated, and displaces nothing.
    async_fire_mqtt_message(hass, folder_topic("movie"), json.dumps({"recordings": [NEWS]}))
    await hass.async_block_till_done()
    assert box.state.recording_folders["movie"][0]["id"] == NEWS["id"]
    assert "stale1" in box.state.recording_folders
    # And a retraction is still taken.
    async_fire_mqtt_message(hass, folder_topic("stale1"), "")
    await hass.async_block_till_done()
    assert "stale1" not in box.state.recording_folders


async def test_when_full_a_named_folder_displaces_a_topic_no_index_names(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    box = config_entry.runtime_data
    monkeypatch.setattr(box_module, "FOLDERS_MAX", len(box.state.recording_folders) + 2)
    for slug in ("junk1", "junk2"):
        async_fire_mqtt_message(hass, folder_topic(slug), json.dumps(FOLDER_SERIES))
    await hass.async_block_till_done()
    held = set(box.state.recording_folders)

    # A retraction of a folder that is not held displaces nothing, also when it is named.
    named = json.loads(json.dumps(INDEX))
    named["locations"][0]["folders"].append(
        {"slug": "newfolder", "path": "New", "count": 1, "size_mb": 1, "cut": 0}
    )
    async_fire_mqtt_message(hass, INDEX_TOPIC, json.dumps({**named, "read": INDEX["read"] + 1}))
    async_fire_mqtt_message(hass, folder_topic("newfolder"), "")
    await hass.async_block_till_done()
    assert set(box.state.recording_folders) == held

    # A folder the index names takes the place of one it does not.
    fresh = {**NEWS, "id": "bbbb000011112222", "file": "New/x.ts"}
    async_fire_mqtt_message(hass, folder_topic("newfolder"), json.dumps({"recordings": [fresh]}))
    await hass.async_block_till_done()
    assert "newfolder" in box.state.recording_folders
    assert len(box.state.recording_folders) == len(held)
    assert {"junk1", "junk2"} - set(box.state.recording_folders)
    assert box.find_recording(fresh["id"]) is not None

    # A topic no index names takes nobody's place, and one held named folder is never pushed out.
    async_fire_mqtt_message(hass, folder_topic("junk3"), json.dumps(FOLDER_SERIES))
    await hass.async_block_till_done()
    assert "junk3" not in box.state.recording_folders
    assert {"movie", "movie_series", "newfolder"} <= set(box.state.recording_folders)


async def test_when_full_of_named_folders_another_named_one_is_not_taken(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing the index names is pushed out, and nothing fails for want of room."""
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    box = config_entry.runtime_data
    monkeypatch.setattr(box_module, "FOLDERS_MAX", len(box.state.recording_folders))
    held = dict(box.state.recording_folders)
    named = json.loads(json.dumps(INDEX))
    named["locations"][0]["folders"].append(
        {"slug": "newfolder", "path": "New", "count": 1, "size_mb": 1, "cut": 0}
    )
    async_fire_mqtt_message(hass, INDEX_TOPIC, json.dumps({**named, "read": INDEX["read"] + 1}))
    caplog.clear()

    async_fire_mqtt_message(hass, folder_topic("newfolder"), json.dumps(FOLDER_SERIES))
    await hass.async_block_till_done()
    assert box.state.recording_folders == held
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


async def test_when_full_a_held_folder_is_still_retracted(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    box = config_entry.runtime_data
    monkeypatch.setattr(box_module, "FOLDERS_MAX", len(box.state.recording_folders))

    async_fire_mqtt_message(hass, folder_topic("movie_series"), "")
    await hass.async_block_till_done()
    assert "movie_series" not in box.state.recording_folders
    assert hass.states.get(LIBRARY).state == "3"


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
    """The same library, read again: only `read` differs, and that is the answer."""
    monkeypatch.setattr(button_module, "RECORDINGS_READ_TIMEOUT", 1)
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    await _arm(
        hass,
        "recordings_refresh",
        lambda _id, _payload: async_fire_mqtt_message(
            hass, INDEX_TOPIC, json.dumps({**INDEX, "read": INDEX["read"] + 1})
        ),
    )
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: REFRESH}, blocking=True
    )


async def test_an_index_that_says_no_read_is_no_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(button_module, "RECORDINGS_READ_TIMEOUT", 0.2)
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    await _arm(
        hass,
        "recordings_refresh",
        lambda _id, _payload: async_fire_mqtt_message(
            hass, INDEX_TOPIC, json.dumps({**INDEX, "read": None})
        ),
    )
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: REFRESH}, blocking=True
        )
    assert raised.value.translation_key == "command_timeout"


@pytest.mark.parametrize(
    "moved",
    [
        pytest.param({"free_mb": 412000}, id="free space"),
        pytest.param({"mounted": False}, id="unmounted"),
    ],
)
async def test_an_index_republished_without_a_new_read_is_no_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    moved: dict[str, Any],
) -> None:
    """The minute mount check republishes the index with `read` and `reason` as they were."""
    monkeypatch.setattr(button_module, "RECORDINGS_READ_TIMEOUT", 0.2)
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    republished = json.loads(json.dumps(INDEX))
    republished["locations"][0].update(moved)

    await _arm(
        hass,
        "recordings_refresh",
        lambda _id, _payload: async_fire_mqtt_message(hass, INDEX_TOPIC, json.dumps(republished)),
    )
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: REFRESH}, blocking=True
        )
    assert raised.value.translation_key == "command_timeout"
    # It was read all the same: the free space moved.
    assert hass.states.get(FREE_SPACE).attributes["locations"][0] == {
        "path": LOCATION, **{key: INDEX["locations"][0][key] for key in ("mounted", "free_mb")},
        **moved,
    }


async def test_two_reads_in_quick_succession_each_answer_a_press(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receiver's `read` grows with every read, also two in one second."""
    monkeypatch.setattr(button_module, "RECORDINGS_READ_TIMEOUT", 1)
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    reads = iter((INDEX["read"] + 1, INDEX["read"] + 2))

    await _arm(
        hass,
        "recordings_refresh",
        lambda _id, _payload: async_fire_mqtt_message(
            hass, INDEX_TOPIC, json.dumps({**INDEX, "read": next(reads), "reason": "refresh"})
        ),
    )
    for _ in range(2):
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: REFRESH}, blocking=True
        )
    assert config_entry.runtime_data.state.recordings_index["read"] == INDEX["read"] + 2


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


async def test_a_refresh_during_an_update_is_refused_in_the_households_language(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)

    await _arm(hass, "recordings_refresh", _refuse(hass, "recordings_refresh", "busy"))
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: REFRESH}, blocking=True
        )
    assert raised.value.translation_key == "recordings_refresh_busy"


# ---------------------------------------------------------------- the browser


async def test_a_folder_and_a_location_with_nothing_to_show_are_left_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A folder holding only a hidden bouquet's recordings is not named anywhere."""
    library(box_on_the_broker, index={**INDEX, "locations": [*INDEX["locations"], USB_LOCATION]})
    box_on_the_broker[folder_topic("usb_movie")] = json.dumps(
        {"recordings": [{**USB_FILM, "bouquet": "Ulubione TV"}]}
    )
    await _setup(hass, hiding(config_entry, "Ulubione TV"), box_on_the_broker)

    # Shown: MATCH and NO_BOUQUET. NEWS, EPISODE and the USB film are in the hidden bouquet,
    # so "Series" and the USB location have nothing to show, and the one location left is
    # opened straight away.
    top = await media_source.async_browse_media(hass, f"{RECEIVER}/r")
    assert [child.title for child in top.children] == ["movie"]
    for gone in (
        f"{RECEIVER}/f/movie_series",
        f"{RECEIVER}/f/usb_movie",
        f"{RECEIVER}/l/%2Fmedia%2Fusb%2Fmovie",
    ):
        with pytest.raises(BrowseError):
            await media_source.async_browse_media(hass, gone)

    state = hass.states.get(LIBRARY)
    assert state.state == "2"
    assert state.attributes["folder_count"] == 1
    assert state.attributes["location_count"] == 1
    # The four the receiver cut from "Series" are not counted: that folder is not shown.
    assert state.attributes["left_out"] == 0

    response = await _get_recordings(hass)
    assert [location["path"] for location in response["locations"]] == [LOCATION]
    assert [folder["slug"] for folder in response["locations"][0]["folders"]] == ["movie"]




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
    box_on_the_broker[folder_topic("usb_movie")] = json.dumps({"recordings": [USB_FILM]})
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
    box_on_the_broker[folder_topic("stale")] = json.dumps({"recordings": [USB_FILM]})
    await _setup(hass, config_entry, box_on_the_broker)

    with pytest.raises(BrowseError):
        await media_source.async_browse_media(hass, f"{RECEIVER}/f/stale")
    # Its topic is kept - the index may name it next - and read for nothing until then.
    box = config_entry.runtime_data
    assert box.state.recording_folders["stale"]
    assert box.find_recording(USB_FILM["id"]) is None

    # A folder whose topic was retracted has nothing to show, and is not listed until it
    # is published again.
    async_fire_mqtt_message(hass, folder_topic("movie"), "")
    await hass.async_block_till_done()
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(hass, f"{RECEIVER}/f/movie")
    top = await media_source.async_browse_media(hass, f"{RECEIVER}/r")
    assert [child.title for child in top.children] == ["Series"]


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

    sent = await _arm(hass, "play_recording", _plays(hass, f"{LOCATION}/{EPISODE['file']}"))
    await _play(hass, "recording", f"{RECEIVER}/p/{EPISODE['id']}")
    assert [json.loads(payload) for _id, payload in sent] == [{"id": EPISODE["id"]}]


@pytest.mark.parametrize(
    ("file", "written"),
    [
        pytest.param("100% Wolf.ts", "100%25 Wolf.ts", id="percent"),
        pytest.param("a:b.ts", "a%3ab.ts", id="colon"),
        pytest.param("Series/A b.ts", "Series/A b.ts", id="space"),
        pytest.param("Series/A b.ts", "Series/A%20b.ts", id="space encoded"),
        pytest.param("tab\tname.ts", "tab%09name.ts", id="control"),
    ],
)
async def test_play_reads_the_path_the_way_the_image_writes_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    file: str,
    written: str,
) -> None:
    """The image percent-encodes `:`, `%` and control characters in a reference's path."""
    film = {**NEWS, "id": "9999888877776666", "file": file, "name": "Wolf"}
    library(box_on_the_broker)
    box_on_the_broker[folder_topic("movie")] = json.dumps({"recordings": [film]})
    await _setup(hass, config_entry, box_on_the_broker)

    # A name the image writes after the path is a field of its own.
    await _arm(hass, "play_recording", _plays(hass, f"{LOCATION}/{written}:Wolf"))
    await _play(hass, "recording", film["id"])


def test_a_path_that_only_ends_the_same_is_not_the_recording() -> None:
    location = {"path": "/media/hdd/movie"}
    other = {"sref": "1:0:0:0:0:0:0:0:0:0:/media/usb/media/hdd/movie/a.ts"}
    assert recordings.plays(other, location, {"file": "a.ts"}) is False
    short = {"sref": "1:0:0:0:0"}
    assert recordings.plays(short, location, {"file": "a.ts"}) is False


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


@pytest.mark.parametrize(
    ("timeshift", "timeout"),
    [
        pytest.param({"active": True, "paused": False}, 60, id="buffer"),
        pytest.param({"active": False}, None, id="no buffer"),
        pytest.param(None, None, id="never said"),
    ],
)
async def test_only_a_running_buffer_makes_the_play_wait_a_minute(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    timeshift: dict[str, Any] | None,
    timeout: int | None,
) -> None:
    library(box_on_the_broker)
    if timeshift is not None:
        box_on_the_broker[TIMESHIFT_TOPIC] = json.dumps(timeshift)
    await _setup(hass, config_entry, box_on_the_broker)
    asked: list[tuple[str, dict[str, Any]]] = []

    async def _command(self, name: str, payload: str, **kwargs: Any) -> None:
        asked.append((name, kwargs))

    monkeypatch.setattr(box_module.Enigma2Box, "async_command", _command)
    await _play(hass, "recording", NEWS["id"])
    ((name, kwargs),) = asked
    assert name == "play_recording"
    # Without a buffer the command's own wait applies: the ten seconds of a zap.
    assert kwargs.get("timeout") == timeout


async def test_a_delete_waits_ninety_seconds(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Longer than the receiver's own minute for the move, so its sentence comes first."""
    deletable(box_on_the_broker)
    await _setup(hass, config_entry, box_on_the_broker)
    asked: list[tuple[str, dict[str, Any]]] = []

    async def _command(self, name: str, payload: str, **kwargs: Any) -> None:
        asked.append((name, kwargs))

    monkeypatch.setattr(box_module.Enigma2Box, "async_command", _command)
    await _delete(hass, NEWS["id"])
    ((name, kwargs),) = asked
    assert name == "delete_recording"
    assert kwargs["timeout"] == 90


async def test_with_timeshift_running_play_waits_for_the_viewer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receiver asks first, so the wait is the timeshift one, not a zap's."""
    monkeypatch.setattr(services_module, "PLAY_RECORDING_TIMESHIFT_TIMEOUT", 0.1)
    library(box_on_the_broker)
    box_on_the_broker[TIMESHIFT_TOPIC] = json.dumps({"active": True, "paused": True})
    await _setup(hass, config_entry, box_on_the_broker)

    await _arm(hass, "play_recording", lambda _id, _payload: None)
    started = dt_util.utcnow()
    with pytest.raises(Enigma2CommandError) as raised:
        await _play(hass, "recording", NEWS["id"])
    assert raised.value.translation_key == "command_timeout"
    # Not the ten seconds of a play without a buffer.
    assert (dt_util.utcnow() - started).total_seconds() < 5


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
        ("busy", "play_recording_busy"),
        ("timeshift_kept", "play_recording_timeshift_kept"),
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
        (
            {"settings": {"recording_delete_allowed": True}, "recording_trash": True},
            "recording_delete_not_ready",
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
    monkeypatch.setattr(services_module, "RECORDING_DELETE_TIMEOUT", 0.1)
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
        ("in_progress", ServiceValidationError),
        ("busy", ServiceValidationError),
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


# ------------------------------------------------------------- get_recordings


async def _get_recordings(hass: HomeAssistant) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        "get_recordings",
        {ATTR_ENTITY_ID: PLAYER},
        blocking=True,
        return_response=True,
    )
    return response[PLAYER]


async def test_get_recordings_returns_what_the_library_shows(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    library(box_on_the_broker)
    await _setup(hass, hiding(config_entry, "Sport"), box_on_the_broker)

    fields = ("id", "file", "name", "description", "service_name", "begin", "duration", "size_mb")
    assert await _get_recordings(hass) == {
        "read": 1792050000,
        "reason": "start",
        "locations": [
            {
                "path": LOCATION,
                "mounted": True,
                "free_mb": 412330,
                "folders": [
                    {
                        "slug": "movie",
                        "path": "",
                        "cut": 0,
                        "recordings": [
                            {key: NEWS[key] for key in fields},
                            {key: NO_BOUQUET[key] for key in fields},
                        ],
                    },
                    {
                        "slug": "movie_series",
                        "path": "Series",
                        "cut": 4,
                        "recordings": [{key: EPISODE[key] for key in fields}],
                    },
                ],
            }
        ],
    }
    # Nothing was asked of the receiver.
    assert published_payloads(mqtt_mock, command_topic("recordings_refresh")) == []


async def test_get_recordings_before_the_library_has_arrived(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box_on_the_broker[INFO_TOPIC] = info_with(*LIBRARY_CAPABILITIES)
    await _setup(hass, config_entry, box_on_the_broker)

    assert await _get_recordings(hass) == {"read": None, "reason": None, "locations": []}


async def test_get_recordings_without_the_library_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box_on_the_broker[INDEX_TOPIC] = json.dumps(INDEX)
    await _setup(hass, config_entry, box_on_the_broker)

    with pytest.raises(ServiceValidationError) as raised:
        await _get_recordings(hass)
    assert raised.value.translation_key == "recordings_unsupported"


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

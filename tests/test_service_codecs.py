"""What is on the decoder: the codec members of `service`, as diagnostics.

A plugin after 0.4.0 adds `video_codec`, `interlaced`, `frame_rate` and `audio_tracks` to
the `service` topic and claims `service_codecs` where the image can fill them in. The
entities exist only for a receiver that claims the capability, they are disabled until
somebody wants them, and every member may be missing or `null`.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt import stream

from .conftest import (
    INFO,
    INFO_TOPIC,
    NODE_ID,
    SERVICE,
    SERVICE_TOPIC,
    async_setup_box_then_retained,
)

VIDEO_CODEC = "sensor.dekoder_salon_video_codec"
FRAME_RATE = "sensor.dekoder_salon_frame_rate"
AUDIO_CODEC = "sensor.dekoder_salon_audio_codec"
AUDIO_LANGUAGE = "sensor.dekoder_salon_audio_language"
INTERLACED = "binary_sensor.dekoder_salon_interlaced_picture"
ENTITIES = (VIDEO_CODEC, FRAME_RATE, AUDIO_CODEC, AUDIO_LANGUAGE, INTERLACED)
KEYS = (
    ("sensor", "video_codec"),
    ("sensor", "frame_rate"),
    ("sensor", "audio_codec"),
    ("sensor", "audio_language"),
    ("binary_sensor", "interlaced"),
)

TRACKS: list[dict[str, Any]] = [
    {"index": 0, "description": "MPEG", "language": "pol", "pid": 5001,
     "codec": "mp2", "active": False},
    {"index": 1, "description": "Dolby Digital", "language": "pol", "pid": 5002,
     "codec": "ac3", "active": True},
]
SERVICE_WITH_CODECS: dict[str, Any] = {
    **SERVICE,
    "video_codec": "h264",
    "interlaced": True,
    "frame_rate": 25.0,
    "audio_tracks": TRACKS,
}


def info_with(*capabilities: str) -> str:
    return json.dumps({**INFO, "capabilities": [*INFO["capabilities"], *capabilities]})


def registered(hass: HomeAssistant, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, "enigma2_mqtt", f"{NODE_ID}_{key}")


async def test_a_receiver_that_does_not_claim_the_capability_gets_no_entities(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Plugin 0.4.0 has no such members, and five unknowns would say nothing."""
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(SERVICE_WITH_CODECS)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    for platform, key in KEYS:
        assert registered(hass, platform, key) is None


async def test_the_entities_are_diagnostics_and_disabled_until_wanted(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Like the tuner's numbers: looked up when something is wrong, noise otherwise."""
    box_on_the_broker[INFO_TOPIC] = info_with("service_codecs")
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    registry = er.async_get(hass)
    for platform, key in KEYS:
        entity_id = registered(hass, platform, key)
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry.entity_category is er.EntityCategory.DIAGNOSTIC
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_codec_members_become_states(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The retained burst arrives after setup, as from a broker, and fills them in."""
    box_on_the_broker[INFO_TOPIC] = info_with("service_codecs")
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(SERVICE_WITH_CODECS)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(VIDEO_CODEC).state == "h264"
    assert hass.states.get(FRAME_RATE).state == "25.0"
    assert hass.states.get(INTERLACED).state == "on"
    audio = hass.states.get(AUDIO_CODEC)
    assert audio.state == "ac3"
    assert audio.attributes["description"] == "Dolby Digital"
    assert audio.attributes["audio_tracks"] == TRACKS
    assert hass.states.get(AUDIO_LANGUAGE).state == "pol"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_track_list_is_not_recorded(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Up to 32 tracks, rewritten when somebody picks another one on the remote."""
    box_on_the_broker[INFO_TOPIC] = info_with("service_codecs")
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(SERVICE_WITH_CODECS)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(AUDIO_CODEC)
    assert state.attributes["audio_tracks"] == TRACKS
    event: Event[EventStateChangedData] = Event(
        EVENT_STATE_CHANGED,
        {"entity_id": state.entity_id, "old_state": None, "new_state": state},
    )
    recorded = json.loads(StateAttributes.shared_attrs_bytes_from_event(event, None))
    assert "audio_tracks" not in recorded
    assert recorded["description"] == "Dolby Digital"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    "service",
    [
        # An image that cannot say: the members are there and null.
        {**SERVICE, "video_codec": None, "interlaced": None, "frame_rate": None,
         "audio_tracks": None},
        # A payload without the members at all.
        dict(SERVICE),
        # Members of another type than the contract's.
        {**SERVICE, "video_codec": 27, "interlaced": "yes", "frame_rate": "25",
         "audio_tracks": {"index": 0}},
        # A frame rate that is no rate, and tracks that are no tracks.
        {**SERVICE, "video_codec": "", "interlaced": 1, "frame_rate": True,
         "audio_tracks": ["MPEG", None, 5]},
    ],
)
async def test_what_is_missing_or_malformed_is_unknown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    service: dict[str, Any],
) -> None:
    """Unknown, never a guess - and never an exception on the callback that feeds it."""
    box_on_the_broker[INFO_TOPIC] = info_with("service_codecs")
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(service)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    for entity_id in ENTITIES:
        assert hass.states.get(entity_id).state == "unknown", entity_id


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_no_active_track_leaves_the_audio_sensors_unknown_and_keeps_the_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The image does not always say which track plays; the tracks are still there."""
    tracks = [{**track, "active": False} for track in TRACKS]
    box_on_the_broker[INFO_TOPIC] = info_with("service_codecs")
    box_on_the_broker[SERVICE_TOPIC] = json.dumps({**SERVICE_WITH_CODECS, "audio_tracks": tracks})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    audio = hass.states.get(AUDIO_CODEC)
    assert audio.state == "unknown"
    assert audio.attributes["audio_tracks"] == tracks
    assert hass.states.get(AUDIO_LANGUAGE).state == "unknown"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_unknown_members_are_ignored_and_a_new_codec_is_shown_as_it_is(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The enumerations are open, and a later plugin may say more about a track."""
    tracks = [{**TRACKS[1], "codec": "mpegh", "channels": 6}]
    box_on_the_broker[INFO_TOPIC] = info_with("service_codecs")
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(
        {**SERVICE_WITH_CODECS, "video_codec": "vvc", "hdr": "hlg", "audio_tracks": tracks}
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(VIDEO_CODEC).state == "vvc"
    audio = hass.states.get(AUDIO_CODEC)
    assert audio.state == "mpegh"
    assert audio.attributes["audio_tracks"] == [
        {key: value for key, value in tracks[0].items() if key != "channels"}
    ]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_capability_that_goes_quiet_takes_nothing_away(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A downgraded plugin is not a decision; the entities stay and say unknown."""
    box_on_the_broker[INFO_TOPIC] = info_with("service_codecs")
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(SERVICE_WITH_CODECS)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(SERVICE))
    await hass.async_block_till_done()

    for platform, key in KEYS:
        assert registered(hass, platform, key) is not None
    assert hass.states.get(VIDEO_CODEC).state == "unknown"


def test_a_frame_rate_is_a_positive_number_to_three_places() -> None:
    assert stream.frame_rate({"frame_rate": 23.976}) == 23.976
    assert stream.frame_rate({"frame_rate": 50}) == 50.0
    assert stream.frame_rate({"frame_rate": 29.97002997}) == 29.97
    for value in (0, -25, True, "25", None, float("inf"), float("nan")):
        assert stream.frame_rate({"frame_rate": value}) is None
    assert stream.frame_rate(None) is None


def test_the_track_list_is_bounded_and_its_text_is_cut() -> None:
    """At most 32 tracks and 64 characters, as the plugin publishes them."""
    tracks = [
        {"index": index, "description": "x" * 200, "language": "pol", "pid": index,
         "codec": "mp2", "active": index == 40}
        for index in range(50)
    ]

    read = stream.audio_tracks({"audio_tracks": tracks})

    assert len(read) == 32
    assert read[0]["description"] == "x" * 64
    assert stream.active_audio_track({"audio_tracks": tracks}) is None
    assert stream.audio_tracks({"audio_tracks": []}) == []
    assert stream.audio_tracks({}) is None

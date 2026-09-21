"""The sensors, and the attributes that explain them."""

from __future__ import annotations

import json

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    mock_restore_cache,
)

from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    AVAILABILITY_TOPIC,
    EPG,
    EPG_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    RECORDING_ACTIVE,
    RECORDING_TOPIC,
    SREF,
    TUNER_TOPIC,
    async_setup_box,
    async_setup_box_then_retained,
)

CAM_TOPIC = "enigma2/vuuno4kse_005301/cam"


async def test_the_channel_sensor_carries_the_service(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The name is the state; the reference and the rest are attributes."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get("sensor.dekoder_salon_channel")
    assert state.state == "TVP 1 HD"
    assert state.attributes["sref"] == SREF
    assert state.attributes["bouquet"] == "Ulubione TV"
    assert state.attributes["provider"] == "Cyfrowy Polsat"
    assert state.attributes["width"] == 1920
    assert state.attributes["height"] == 1080


async def test_the_programme_sensors_turn_epoch_into_iso(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Attributes are read by templates, and a template cannot read epoch seconds."""
    await async_setup_box(hass, config_entry)

    now = hass.states.get("sensor.dekoder_salon_program")
    assert now.state == "Wiadomości"
    assert now.attributes["begin"] == "2026-09-15T08:00:00+00:00"
    assert now.attributes["end"] == "2026-09-15T08:25:00+00:00"
    assert now.attributes["event_id"] == 27431
    assert now.attributes["short"] == "Serwis informacyjny"

    following = hass.states.get("sensor.dekoder_salon_next_program")
    assert following.state == "Pogoda"
    assert following.attributes["event_id"] == 27432
    # The plugin publishes an empty string for a description it does not have.
    assert following.attributes["short"] is None


async def test_an_epg_without_a_next_event(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`next` is null at the end of a broadcast day, and that is not an error."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, EPG_TOPIC, json.dumps({**EPG, "next": None}))
    await hass.async_block_till_done()

    assert hass.states.get("sensor.dekoder_salon_next_program").state == "unknown"


async def test_the_recording_sensors(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The count is the state, and the list of what is running is an attribute."""
    await async_setup_box(hass, config_entry)

    assert hass.states.get("sensor.dekoder_salon_active_recordings").state == "0"
    assert (
        hass.states.get("sensor.dekoder_salon_next_timer").state
        == "2026-09-15T08:25:00+00:00"
    )

    async_fire_mqtt_message(hass, RECORDING_TOPIC, json.dumps(RECORDING_ACTIVE))
    await hass.async_block_till_done()

    active = hass.states.get("sensor.dekoder_salon_active_recordings")
    assert active.state == "1"
    assert active.attributes["recordings"][0]["name"] == "Wiadomości"
    # `next` is null while that recording runs, and a timestamp sensor says so.
    assert hass.states.get("sensor.dekoder_salon_next_timer").state == "unknown"


@pytest.mark.parametrize(
    ("entity_id", "value"),
    [
        ("sensor.dekoder_salon_snr", "78"),
        ("sensor.dekoder_salon_agc", "62"),
        ("sensor.dekoder_salon_ber", "0"),
        ("sensor.dekoder_salon_uptime", "384210"),
    ],
)
async def test_the_diagnostic_sensors_are_disabled_until_asked_for(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    entity_id: str,
    value: str,
) -> None:
    """Signal quality and uptime are for a fault, not for a dashboard."""
    await async_setup_box(hass, config_entry)

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.entity_category == "diagnostic"
    assert hass.states.get(entity_id) is None

    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == value


async def test_a_topic_that_never_arrived_is_unavailable(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An image with no tuner hook publishes no tuner topic, and says nothing."""
    retained[AVAILABILITY_TOPIC] = "online"
    await async_setup_box(hass, config_entry)

    assert hass.states.get("sensor.dekoder_salon_channel").state == STATE_UNAVAILABLE

    async_fire_mqtt_message(hass, TUNER_TOPIC, json.dumps({"snr": 70}))
    await hass.async_block_till_done()

    # The channel sensor reads `service`, which still has not arrived.
    assert hass.states.get("sensor.dekoder_salon_channel").state == STATE_UNAVAILABLE


async def test_everything_goes_unavailable_when_the_box_does(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A retained payload is a snapshot; a box that is gone must not look live."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("sensor.dekoder_salon_channel").state == "TVP 1 HD"

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    assert hass.states.get("sensor.dekoder_salon_channel").state == STATE_UNAVAILABLE


async def test_cam_telemetry_is_opt_in_bounded_and_removed_when_disabled(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Only the public CAM schema survives, and disabling removes its entities."""
    enabled_info = {
        **INFO,
        "capabilities": [*INFO["capabilities"], "cam"],
        "settings": {"cam_telemetry": True},
    }
    box_on_the_broker[INFO_TOPIC] = json.dumps(enabled_info)
    box_on_the_broker[CAM_TOPIC] = json.dumps(
        {
            "system": "Irdeto",
            "active": True,
            "encrypted": True,
            "ecm_ms": 184,
            "server": "private.example",
            "reader": "household-card",
            "password": "must-not-survive",
        }
    )
    await async_setup_box(hass, config_entry)

    assert (
        hass.states.get("sensor.dekoder_salon_conditional_access_system").state
        == "Irdeto"
    )
    assert hass.states.get("sensor.dekoder_salon_ecm_time").state == "184"
    assert hass.states.get("binary_sensor.dekoder_salon_cam_active").state == "on"
    assert (
        hass.states.get("binary_sensor.dekoder_salon_service_encrypted").state
        == "on"
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["topics"]["cam"] == {
        "system": "Irdeto",
        "active": True,
        "encrypted": True,
        "ecm_ms": 184,
    }

    async_fire_mqtt_message(hass, CAM_TOPIC, "")
    await hass.async_block_till_done()
    assert config_entry.runtime_data.state.cam is None
    assert CAM_TOPIC.rsplit("/", 1)[-1] not in config_entry.runtime_data.seen

    async_fire_mqtt_message(hass, CAM_TOPIC, box_on_the_broker[CAM_TOPIC])
    await hass.async_block_till_done()
    assert config_entry.runtime_data.state.cam is not None

    disabled_info = {
        **INFO,
        "settings": {"cam_telemetry": False},
    }
    box_on_the_broker[INFO_TOPIC] = json.dumps(disabled_info)
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(disabled_info))
    await hass.async_block_till_done()
    assert config_entry.runtime_data.state.cam is None
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for entity_id in (
        "sensor.dekoder_salon_conditional_access_system",
        "sensor.dekoder_salon_ecm_time",
        "binary_sensor.dekoder_salon_cam_active",
        "binary_sensor.dekoder_salon_service_encrypted",
    ):
        assert hass.states.get(entity_id) is None
        assert registry.async_get(entity_id) is None


async def test_stray_cam_topic_is_ignored_without_opt_in(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An old retained CAM payload cannot populate disabled telemetry."""
    box_on_the_broker[CAM_TOPIC] = json.dumps(
        {"system": "Irdeto", "active": True, "encrypted": True, "ecm_ms": 100}
    )
    await async_setup_box(hass, config_entry)

    assert config_entry.runtime_data.state.cam is None
    assert CAM_TOPIC.rsplit("/", 1)[-1] not in config_entry.runtime_data.seen
    assert hass.states.get("sensor.dekoder_salon_conditional_access_system") is None


async def test_retained_cam_before_info_is_applied_only_after_confirmed_opt_in(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Wildcard retained order cannot lose safe telemetry or bypass opt-in."""
    box_on_the_broker.pop(INFO_TOPIC)
    box_on_the_broker[CAM_TOPIC] = json.dumps(
        {
            "system": "Viaccess",
            "active": True,
            "encrypted": True,
            "ecm_ms": 91,
            "password": "discard-before-pending",
        }
    )
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "cam"],
            "settings": {"cam_telemetry": True},
        }
    )

    await async_setup_box(hass, config_entry)

    assert config_entry.runtime_data.state.cam == {
        "system": "Viaccess",
        "active": True,
        "encrypted": True,
        "ecm_ms": 91,
    }
    assert (
        hass.states.get("sensor.dekoder_salon_conditional_access_system").state
        == "Viaccess"
    )


@pytest.mark.parametrize("cam_payload", ["", json.dumps({"system": "Irdeto"})])
async def test_retained_cam_before_info_is_discarded_on_tombstone_or_opt_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    cam_payload: str,
) -> None:
    """A pending tombstone stays empty and explicit opt-out never creates state."""
    box_on_the_broker.pop(INFO_TOPIC)
    box_on_the_broker[CAM_TOPIC] = cam_payload
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "settings": {"cam_telemetry": False}}
    )

    await async_setup_box(hass, config_entry)

    assert config_entry.runtime_data.state.cam is None
    assert CAM_TOPIC.rsplit("/", 1)[-1] not in config_entry.runtime_data.seen
    assert hass.states.get("sensor.dekoder_salon_conditional_access_system") is None


async def test_the_last_error_sensor_takes_the_boxs_complaint(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The command is the state, the box's own sentence is the attribute."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("sensor.dekoder_salon_last_error").state == STATE_UNKNOWN

    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps(
            {
                "cmd": "deep_standby",
                "error": "deep standby is not allowed on this box",
                "ts": 1789459213,
            }
        ),
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "deep_standby"
    assert state.attributes["error"] == "deep standby is not allowed on this box"
    # The receiver's own timestamp, which is what makes a replay of this payload
    # change nothing.
    assert state.attributes["time"] == "2026-09-15T08:00:13+00:00"


async def test_a_complaint_without_a_timestamp_is_stamped_on_arrival(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An older plugin, or a truncated write, still gets a time worth showing."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "zap", "error": "no such service"}),
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert dt_util.parse_datetime(state.attributes["time"]) is not None


async def test_clearing_the_topic_does_not_clear_the_sensor(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """This is the entire point of the sensor.

    The plugin clears `last_error` on the next command that succeeds, and the person
    asking why „Restart" did nothing looks afterwards — usually after the next volume
    step has already wiped it.
    """
    await async_setup_box(hass, config_entry)
    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "reboot", "error": "a recording is running", "ts": 1}),
    )
    await hass.async_block_till_done()

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await hass.async_block_till_done()

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "reboot"
    assert state.attributes["error"] == "a recording is running"


async def test_a_long_complaint_is_cut_to_what_a_state_can_hold(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A state over 255 characters is dropped entirely, so the text is bounded too."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "zap", "error": "no signal. " * 60, "ts": 1}),
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "zap"
    assert len(state.attributes["error"]) == 255


async def test_the_last_error_survives_a_restart(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Including the time, which the replayed retained payload must not move.

    In the order a real broker produces: the entity is built first and the retained
    complaint arrives afterwards, which is exactly when a sensor that trusted the
    clock would overwrite the restored time with the moment of the replay.
    """
    seen_at = "2026-09-20T21:14:03+00:00"
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.dekoder_salon_last_error",
                "deep_standby",
                {"error": "deep standby is not allowed on this box", "time": seen_at},
            )
        ],
    )
    box_on_the_broker[LAST_ERROR_TOPIC] = json.dumps(
        {"cmd": "deep_standby", "error": "deep standby is not allowed on this box"}
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "deep_standby"
    assert state.attributes["error"] == "deep standby is not allowed on this box"
    assert state.attributes["time"] == seen_at


async def test_a_reconnect_does_not_move_the_time_of_a_complaint(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The broker replays the retained complaint on every reconnect.

    A time read off the clock would walk forward each time, so the one number that
    says when the receiver refused would end up saying "just now" for ever.
    """
    await async_setup_box(hass, config_entry)
    complaint = json.dumps(
        {"cmd": "reboot", "error": "a recording is running", "ts": 1789459213}
    )
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, complaint)
    await hass.async_block_till_done()
    first = hass.states.get("sensor.dekoder_salon_last_error").attributes["time"]

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, complaint, retain=True)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "reboot"
    assert state.attributes["time"] == first


async def test_a_retained_complaint_is_shown_when_there_is_nothing_to_restore(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A fresh install, or a refusal that happened while Home Assistant was down.

    The complaint is still the truth about the receiver, so it is shown — with the
    receiver's own timestamp, not the moment the broker handed it over.
    """
    box_on_the_broker[LAST_ERROR_TOPIC] = json.dumps(
        {"cmd": "deep_standby", "error": "deep standby is not allowed", "ts": 1789459213}
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "deep_standby"
    assert state.attributes["error"] == "deep standby is not allowed"
    assert state.attributes["time"] == "2026-09-15T08:00:13+00:00"


async def test_the_complaint_outlives_the_receiver(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Deep standby is a box that has left the network, and it is the headline case.

    An entity that went unavailable with the receiver would lose the explanation at
    the exact moment somebody went looking for it.
    """
    await async_setup_box(hass, config_entry)
    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "deep_standby", "error": "not allowed", "ts": 1789459213}),
    )
    await hass.async_block_till_done()

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "deep_standby"
    assert state.attributes["error"] == "not allowed"
    # Every other entity of this box is unavailable by now.
    assert hass.states.get("sensor.dekoder_salon_channel").state == STATE_UNAVAILABLE


async def test_a_restart_while_the_receiver_is_away_keeps_the_complaint(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Which is the restart that happens after somebody pressed deep standby."""
    seen_at = "2026-09-20T21:14:03+00:00"
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.dekoder_salon_last_error",
                "deep_standby",
                {"error": "deep standby is not allowed", "time": seen_at},
            )
        ],
    )
    retained[AVAILABILITY_TOPIC] = "offline"

    await async_setup_box_then_retained(hass, config_entry, retained)

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "deep_standby"
    assert state.attributes["time"] == seen_at


async def test_an_empty_retained_complaint_does_not_clear_a_restored_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The usual state of the topic after a restart is empty, and it means nothing."""
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.dekoder_salon_last_error",
                "epg_grid",
                {"error": "the EPG grid is switched off", "time": "2026-09-20T21:14:03+00:00"},
            )
        ],
    )
    box_on_the_broker[LAST_ERROR_TOPIC] = ""

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get("sensor.dekoder_salon_last_error").state == "epg_grid"


async def test_a_new_complaint_replaces_a_restored_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """There is no explicit clear, so the next error has to be able to take over."""
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.dekoder_salon_last_error",
                "epg_grid",
                {"error": "the EPG grid is switched off", "time": "2026-09-20T21:14:03+00:00"},
            )
        ],
    )
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "record", "error": "no tuner is free", "ts": 2}),
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.dekoder_salon_last_error")
    assert state.state == "record"
    assert state.attributes["error"] == "no tuner is free"


async def test_retained_cam_tombstone_before_enabled_info_remains_empty(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An early retained retraction must not turn into CAM state."""
    box_on_the_broker.pop(INFO_TOPIC)
    box_on_the_broker[CAM_TOPIC] = ""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "cam"],
            "settings": {"cam_telemetry": True},
        }
    )

    await async_setup_box(hass, config_entry)

    assert config_entry.runtime_data.state.cam is None
    assert CAM_TOPIC.rsplit("/", 1)[-1] not in config_entry.runtime_data.seen
    assert (
        hass.states.get("sensor.dekoder_salon_conditional_access_system").state
        == STATE_UNAVAILABLE
    )

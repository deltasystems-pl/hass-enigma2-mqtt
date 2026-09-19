"""The sensors, and the attributes that explain them."""

from __future__ import annotations

import json

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
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
    RECORDING_ACTIVE,
    RECORDING_TOPIC,
    SREF,
    TUNER_TOPIC,
    async_setup_box,
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

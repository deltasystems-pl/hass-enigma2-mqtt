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

from .conftest import (
    AVAILABILITY_TOPIC,
    EPG,
    EPG_TOPIC,
    RECORDING_ACTIVE,
    RECORDING_TOPIC,
    SREF,
    TUNER_TOPIC,
    async_setup_box,
)


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

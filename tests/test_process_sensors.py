"""What the enigma2 process itself is doing, once the box says it can measure it.

The topic is five integers and the contract says every one of them may be null. That
makes the interesting tests the ones where the payload is not what it should be — a
float, a string, `true`, a negative, a number from a different unit — because each of
those, taken at face value, is a point on a graph that reads as a real measurement.

The other half is *when* the entities appear. A receiver answers after Home Assistant
has finished setting the integration up, so reading the capability once during platform
setup creates nothing at all on a real box. That was PR #6's bug in the OSCam entities
and this group is built on the manager that fixed it.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorStateClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.const import CAPABILITY_PROCESS

from .conftest import (
    BASE_TOPIC,
    CAPABILITIES,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    SLUG,
    async_setup_box,
)

PROCESS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/process"

MEMORY = f"sensor.{SLUG}_enigma2_memory"
PEAK = f"sensor.{SLUG}_enigma2_memory_peak"
THREADS = f"sensor.{SLUG}_enigma2_threads"
OPEN_FILES = f"sensor.{SLUG}_enigma2_open_files"
STARTED = f"sensor.{SLUG}_enigma2_started"
ALL_PROCESS = (MEMORY, PEAK, THREADS, OPEN_FILES, STARTED)

# A receiver two days into an uptime, using about a third of its RAM.
SAMPLE: dict[str, Any] = {
    "rss_kb": 183_296,
    "hwm_kb": 211_968,
    "threads": 37,
    "fds": 214,
    "started": 1_789_286_400,
}

WITH_PROCESS = {**INFO, "capabilities": [*CAPABILITIES, CAPABILITY_PROCESS]}


async def _enable(hass: HomeAssistant, entity_ids: tuple[str, ...]) -> None:
    """Turn on the entities that ship disabled, and reload so they exist."""
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        registry.async_update_entity(entity_id, disabled_by=None)
    entry = hass.config_entries.async_entries("enigma2_mqtt")[0]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def _announce_capability(
    hass: HomeAssistant, retained: dict[str, str | bytes]
) -> None:
    """Let the box say it can measure its own process, the way a real one does.

    The retained store is updated as well as the message fired, because `info` is a
    retained topic: a reload re-reads it from the broker, and a box that announced the
    capability once announces it again.
    """
    retained[INFO_TOPIC] = json.dumps(WITH_PROCESS)
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(WITH_PROCESS))
    await hass.async_block_till_done()


async def _publish(hass: HomeAssistant, payload: Any) -> None:
    async_fire_mqtt_message(
        hass,
        PROCESS_TOPIC,
        payload if isinstance(payload, (str, bytes)) else json.dumps(payload),
    )
    await hass.async_block_till_done()


async def test_a_box_that_cannot_measure_itself_gets_no_entities(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An older plugin, or an image that would not let it hook the measurement."""
    await async_setup_box(hass, config_entry)

    registry = er.async_get(hass)
    unique_id = f"{NODE_ID}_process_memory"
    assert registry.async_get_entity_id("sensor", "enigma2_mqtt", unique_id) is None


async def test_a_capability_that_arrives_after_setup_still_builds_them(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """This is the case a real receiver is always in.

    `async_setup_entry` subscribes and returns; the retained burst that carries `info`,
    and with it the capability list, arrives afterwards. Reading the capability once
    during setup therefore built nothing at all on a real box — which is exactly what
    happened to the OSCam entities before PR #6.
    """
    await async_setup_box(hass, config_entry)
    assert hass.states.get(MEMORY) is None

    await _announce_capability(hass, box_on_the_broker)

    # Created, and honest about having no reading yet: the repository's rule is that an
    # entity whose topic has never arrived says unavailable rather than inventing a zero.
    assert hass.states.get(MEMORY) is not None
    assert hass.states.get(MEMORY).state == STATE_UNAVAILABLE


async def test_the_numbers_arrive_as_numbers_somebody_would_graph(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Kilobytes on the wire, mebibytes on the card, one decimal either way."""
    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)
    await _enable(hass, (PEAK, THREADS, OPEN_FILES, STARTED))
    await _publish(hass, SAMPLE)

    memory = hass.states.get(MEMORY)
    assert memory.state == "179.0"
    assert memory.attributes[ATTR_UNIT_OF_MEASUREMENT] == "MiB"
    assert memory.attributes[ATTR_DEVICE_CLASS] == "data_size"

    assert hass.states.get(PEAK).state == "207.0"
    assert hass.states.get(THREADS).state == "37"
    assert hass.states.get(OPEN_FILES).state == "214"
    # A moment, not a number: the card says "2 days ago" and a template can subtract it.
    assert hass.states.get(STARTED).state == "2026-09-13T08:00:00+00:00"
    assert hass.states.get(STARTED).attributes[ATTR_DEVICE_CLASS] == "timestamp"


async def test_the_memory_curve_is_the_one_that_is_recorded(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A resident set size over weeks cannot be collected after the question is asked.

    Everything else here is looked up when something is already wrong, so it ships
    switched off. The memory is the one that has to have been running all along.
    """
    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)

    registry = er.async_get(hass)
    assert registry.async_get(MEMORY).disabled_by is None
    for entity_id in (PEAK, THREADS, OPEN_FILES, STARTED):
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_every_number_carries_a_state_class_the_recorder_can_use(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Without it there are no statistics, and a curve over weeks is the whole point."""
    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)
    await _enable(hass, (PEAK, THREADS, OPEN_FILES, STARTED))
    await _publish(hass, SAMPLE)

    for entity_id in (MEMORY, PEAK, THREADS, OPEN_FILES):
        state = hass.states.get(entity_id)
        assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT
    # A start time is a moment. Averaging it would mean nothing.
    assert ATTR_STATE_CLASS not in hass.states.get(STARTED).attributes


async def test_a_null_is_unknown_rather_than_zero(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The contract allows null for every field, and a receiver uses it.

    Zero threads and a process that started at the epoch are both readings. Neither is
    what "the plugin could not measure this" means.
    """
    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)
    await _enable(hass, (PEAK, THREADS, OPEN_FILES, STARTED))
    await _publish(hass, dict.fromkeys(SAMPLE))

    for entity_id in ALL_PROCESS:
        assert hass.states.get(entity_id).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        # Every one of these would be a plausible-looking point on a graph if it were
        # taken at face value.
        {"rss_kb": "183296", "threads": 37.5, "fds": -1, "hwm_kb": True, "started": 0.5},
        # A different unit: bytes where kilobytes belong, milliseconds where seconds do.
        {"rss_kb": 187_695_104, "started": 1_789_286_400_000},
        # A field the contract does not have, and nothing it does.
        {"cpu_percent": 12},
        # The right shape, entirely wrong types.
        {"rss_kb": [1], "hwm_kb": {}, "threads": None, "fds": "many", "started": "now"},
    ],
)
async def test_a_value_that_is_not_a_measurement_is_not_shown_as_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    payload: dict[str, Any],
) -> None:
    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)
    await _enable(hass, (PEAK, THREADS, OPEN_FILES, STARTED))
    await _publish(hass, payload)

    for entity_id in ALL_PROCESS:
        assert hass.states.get(entity_id).state == STATE_UNKNOWN


@pytest.mark.parametrize("payload", ["not json at all", "[1, 2, 3]", "42", b"\xff\xfe"])
async def test_a_payload_that_is_not_the_contract_keeps_the_last_good_sample(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    payload: str | bytes,
) -> None:
    """One bad publish is not a reason to throw away a measurement that parsed.

    And, above all, not a reason to raise: this runs on the MQTT callback, where an
    exception takes the whole box's message handling with it.
    """
    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)
    await _publish(hass, SAMPLE)
    assert hass.states.get(MEMORY).state == "179.0"

    await _publish(hass, payload)

    assert hass.states.get(MEMORY).state == "179.0"


async def test_an_empty_payload_is_a_retraction_and_says_so(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A retained topic cleared by hand, or a plugin that stopped publishing.

    The numbers are gone and the entity says unknown. It does not keep showing a
    measurement from an hour ago as though it were current.
    """
    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)
    await _publish(hass, SAMPLE)

    await _publish(hass, "")

    assert hass.states.get(MEMORY).state == STATE_UNKNOWN


async def test_a_capability_that_stops_being_named_takes_nothing_away(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A downgraded plugin, or a box that answered before it had read its own state.

    Silence is not a decision. Removing these would delete the renames, the areas, the
    dashboards and the history a household had built on them, over a payload that simply
    did not mention something.
    """
    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)
    assert hass.states.get(MEMORY) is not None

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "sensor", "enigma2_mqtt", f"{NODE_ID}_process_memory"
    ) is not None


async def test_the_diagnostics_download_carries_the_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Five integers about a process describe a receiver, not a household."""
    from custom_components.enigma2_mqtt.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await async_setup_box(hass, config_entry)
    await _announce_capability(hass, box_on_the_broker)
    await _publish(hass, SAMPLE)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["topics"]["process"] == SAMPLE

"""Plugin 0.5.0 clears an earlier run's `last_error` when a new plugin process starts.

Until 0.5.0 a retained `last_error` stayed until the next command that succeeded, however
many restarts later. From 0.5.0 the first connect of a new plugin process - after an
interface restart, a reboot, a reinstall or an update - sends the empty retained payload
before `availability` says `online`, unless the starting process has an error of its own
to report (how an update ended, published instead, with no empty payload in between) or
the last update's record ends in a restore that did not complete (then nothing is sent
and the sentence stays). The plugin's TOPICS.md names it `last-error-cleared-at-start`.

Nothing here changes for it, and these tests hold that: every reader of `last_error` is
played the sequence a 0.5.0 receiver sends, live, in its order. The update card and the
followed update over MQTT have theirs in `test_mqtt_update.py`, beside their harness.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.uninstall import _async_watch_uninstall

from .conftest import (
    AVAILABILITY_TOPIC,
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    SCREEN,
    SCREEN_TOPIC,
    async_setup_box_then_retained,
    command_topic,
    published_payloads,
)

SENSOR = "sensor.dekoder_salon_last_error"
COMPLAINT = {"cmd": "deep_standby", "error": "deep standby is not allowed", "ts": 1789459213}


def new_plugin_process(hass: HomeAssistant, *, reports: dict[str, Any] | None = None) -> None:
    """Send what a 0.5.0 plugin's new process sends at its first connect, in its order.

    The old process's `offline`, then the word on `last_error` - the empty payload, or the
    starting process's own report in its place - and only then `online` and the snapshot.
    All of it live: Home Assistant is connected when it happens.
    """
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(reports) if reports else "")
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "online")
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "plugin": "0.5.0"}))


async def test_the_sensor_keeps_its_record_when_a_new_plugin_process_clears_the_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The sensor's memory is Home Assistant's: a restart of the receiver's interface does
    not answer "why did that button do nothing", so it does not take the answer away."""
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(COMPLAINT))
    await hass.async_block_till_done()
    before = hass.states.get(SENSOR)
    assert before.state == "deep_standby"

    new_plugin_process(hass)
    await hass.async_block_till_done()

    after = hass.states.get(SENSOR)
    assert after.state == "deep_standby"
    assert after.attributes["error"] == COMPLAINT["error"]
    assert after.attributes["time"] == before.attributes["time"]
    assert after.attributes["source"] == "receiver"
    # The box itself holds nothing any more, which is what the receiver said.
    assert config_entry.runtime_data.state.last_error is None


async def test_a_report_of_the_starting_process_replaces_the_record(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """How an update ended is published at that connect instead of the empty payload, and
    is news like any complaint the receiver makes while Home Assistant listens."""
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(COMPLAINT))
    await hass.async_block_till_done()

    new_plugin_process(
        hass,
        reports={
            "cmd": "update",
            "error": "the new version did not start; the previous version is back",
            "reason": "not_started",
            "ts": 1789459999,
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get(SENSOR)
    assert state.state == "update"
    assert state.attributes["error"].startswith("the new version did not start")


async def test_home_assistant_s_own_record_is_not_touched_either(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A forced reinstall that failed is recorded here. The plugin that starts afterwards
    clears its topic, not this."""
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    config_entry.runtime_data.async_record_local_error(
        "force_reinstall", "the receiver could not be reached over SSH"
    )
    await hass.async_block_till_done()

    new_plugin_process(hass)
    await hass.async_block_till_done()

    state = hass.states.get(SENSOR)
    assert state.state == "force_reinstall"
    assert state.attributes["source"] == "home_assistant"


async def test_the_empty_payload_neither_answers_nor_refuses_a_command_that_waits(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A press that waits for its effect while the interface restarts under it: the empty
    `last_error` carries no command, so it completes none and fails none."""
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    press = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.dekoder_salon_screenshot"},
            blocking=True,
        )
    )
    for _ in range(200):
        await asyncio.sleep(0.005)
        if published_payloads(mqtt_mock, command_topic("screenshot")):
            break

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    for _ in range(20):
        await asyncio.sleep(0)
    assert not press.done()

    async_fire_mqtt_message(hass, SCREEN_TOPIC, SCREEN)
    await press


async def test_the_uninstall_watch_reads_the_empty_payload_as_nothing(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """Not a refusal, not a rollback, and not one of the retractions that make up the
    removal's shape - also when it arrives after the command went out."""
    watch = await _async_watch_uninstall(hass, BASE_TOPIC, NODE_ID)
    try:
        watch.arm()

        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
        await hass.async_block_till_done()

        assert not watch.refused.done()
        assert not watch.rolled_back.done()
        assert not watch.shape.done()
        assert watch.seen["retracted"] is False

        # The receiver's real answer still gets through, as the refusal it is.
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps({"cmd": "uninstall", "error": "a recording is running", "ts": 2}),
        )
        await hass.async_block_till_done()

        assert watch.refused.result() == "a recording is running"
        assert not watch.rolled_back.done()
    finally:
        watch.cancel()


@pytest.mark.parametrize("cleared_first", [True, False], ids=["cleared", "not cleared"])
async def test_a_rollback_of_an_uninstall_is_read_whatever_the_new_process_cleared(
    hass: HomeAssistant, mqtt_mock, cleared_first: bool
) -> None:
    """The failure path restarts the plugin's session, and on 0.5.0 a process that starts
    may clear the topic first. The sentence that follows is still the rollback."""
    watch = await _async_watch_uninstall(hass, BASE_TOPIC, NODE_ID)
    try:
        watch.arm()
        async_fire_mqtt_message(hass, INFO_TOPIC, "")
        async_fire_mqtt_message(hass, f"enigma2mqtt/discovery/{NODE_ID}/config", "")
        async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
        await hass.async_block_till_done()
        assert watch.shape.done()

        if cleared_first:
            async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
        async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "online")
        async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps({"cmd": "uninstall", "error": "opkg exited with status 255", "ts": 3}),
        )
        await hass.async_block_till_done()

        assert watch.rolled_back.result() == "opkg exited with status 255"
        assert not watch.refused.done()
        assert watch.came_back is True
    finally:
        watch.cancel()

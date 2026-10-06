"""The "Wyczyść ostatni błąd" button: `cmd/clear_error`, and the record "Ostatni błąd" keeps.

The command came with plugin 0.5.0 and the plugin announces no capability for it, so the
gate is the release `info.plugin` states. An older plugin answers the command "unknown
command", and a button that can only ever raise that is worse than no button.

Every set-up here lets the retained burst arrive after the entities exist, which is the
order a broker produces, and creation and removal are read from the registry's events:
an end state cannot tell "never created" from "created and then removed".
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.box import Enigma2Box

from .conftest import (
    AVAILABILITY_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    assert_published,
    async_arm_box_ack,
    async_arm_box_error,
    async_setup_box_then_retained,
    command_topic,
    published_payloads,
)

BUTTON = "button.dekoder_salon_clear_last_error"
SENSOR = "sensor.dekoder_salon_last_error"
UNIQUE_ID = f"{NODE_ID}_clear_error"
COMPLAINT = {"cmd": "deep_standby", "error": "deep standby is not allowed", "ts": 1789459213}


def _info(plugin: str, **extra: Any) -> str:
    return json.dumps({**INFO, "plugin": plugin, **extra})


def _registry_events(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Every creation and removal of the button, as the registry announces them."""
    seen: list[tuple[str, str]] = []

    @callback
    def _changed(event: Event) -> None:
        if event.data["entity_id"] == BUTTON:
            seen.append((event.data["action"], event.data["entity_id"]))

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _changed)
    return seen


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    store: dict[str, str | bytes],
    plugin: str,
    **extra: Any,
) -> None:
    store[INFO_TOPIC] = _info(plugin, **extra)
    await async_setup_box_then_retained(hass, entry, store)


async def _press(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
    )


async def _complain(hass: HomeAssistant) -> None:
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(COMPLAINT))
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "deep_standby"


async def _command_sent(mqtt_mock) -> None:
    """Wait for the press to publish its command, without waiting for the press."""
    for _ in range(200):
        await asyncio.sleep(0.005)
        if published_payloads(mqtt_mock, command_topic("clear_error")):
            for _ in range(5):
                await asyncio.sleep(0)
            return
    raise AssertionError("cmd/clear_error was never published")


@pytest.mark.parametrize(
    "info",
    [
        {"plugin": "0.4.0"},
        {"plugin": "0.4.1"},
        # A member that came with the same release is not the command: the number decides.
        {"plugin": "0.4.0", "not_published": []},
        # A development build says the release it was cut after, and that one has no command.
        {"plugin": "0.4.0", "build": {"commit": "a" * 40, "time": 1, "dirty": False,
                                      "flavour": "dev"}},
        {"plugin": "not a version"},
        {"plugin": None},
    ],
)
async def test_an_older_plugin_gets_no_button(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    info: dict[str, Any],
) -> None:
    """Nothing is created for a plugin that would answer "unknown command"."""
    events = _registry_events(hass)
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, **info})

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert events == []
    assert hass.states.get(BUTTON) is None
    assert er.async_get(hass).async_get_entity_id("button", "enigma2_mqtt", UNIQUE_ID) is None


@pytest.mark.parametrize("plugin", ["0.5.0", "0.5.1", "0.10.0", "1.0.0", "0.5.0+g1a2b3c4"])
async def test_plugin_0_5_0_and_later_gets_the_button(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    plugin: str,
) -> None:
    """Created once, as a diagnostic entity with a unique id in the scheme of the others."""
    events = _registry_events(hass)

    await _setup(hass, config_entry, box_on_the_broker, plugin)

    assert events == [("create", BUTTON)]
    entry = er.async_get(hass).async_get(BUTTON)
    assert entry.unique_id == UNIQUE_ID
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    assert entry.translation_key == "clear_error"
    assert hass.states.get(BUTTON).state == STATE_UNKNOWN


async def test_nothing_is_created_before_info_arrives(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver that has not said which plugin it runs has not said 0.5.0."""
    events = _registry_events(hass)
    retained[AVAILABILITY_TOPIC] = "online"

    await async_setup_box_then_retained(hass, config_entry, retained)
    assert events == []

    async_fire_mqtt_message(hass, INFO_TOPIC, _info("0.5.0"))
    await hass.async_block_till_done()
    assert events == [("create", BUTTON)]


async def test_a_press_clears_the_receiver_and_then_the_record(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The command goes out, the empty `last_error` is the proof, and the sensor empties."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    await _complain(hass)

    press = hass.async_create_task(_press(hass))
    await _command_sent(mqtt_mock)
    assert_published(mqtt_mock, command_topic("clear_error"), "PRESS")
    # Nothing is forgotten on this side before the receiver has answered.
    assert not press.done()
    assert hass.states.get(SENSOR).state == "deep_standby"

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await press
    await hass.async_block_till_done()

    state = hass.states.get(SENSOR)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["error"] is None
    assert state.attributes["time"] is None
    assert state.attributes["source"] is None


async def test_a_press_clears_home_assistants_own_record_too(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A failed forced reinstall is recorded here, and "clear" means that record as well."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    box: Enigma2Box = config_entry.runtime_data
    box.async_record_local_error("force_reinstall", "the receiver could not be reached")
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).attributes["source"] == "home_assistant"
    await async_arm_box_ack(hass, "clear_error")

    await _press(hass)
    await hass.async_block_till_done()

    state = hass.states.get(SENSOR)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["source"] is None
    assert box.state.local_error is None


async def test_a_complaint_after_the_clear_is_shown_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The clear is one event, not a mode: the next refusal is recorded as before."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    await _complain(hass)
    await async_arm_box_ack(hass, "clear_error")
    await _press(hass)
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == STATE_UNKNOWN

    async_fire_mqtt_message(
        hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "zap", "error": "no such service", "ts": 5})
    )
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "zap"

    # And the clearing after a command that succeeded still leaves the record alone.
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "zap"


async def test_a_complaint_right_behind_the_clearing_stays(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The receiver's empty payload and a new refusal arrive in one read. The press is
    answered, and the refusal is newer than the clear: the sensor shows it."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    await _complain(hass)

    press = hass.async_create_task(_press(hass))
    await _command_sent(mqtt_mock)
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    async_fire_mqtt_message(
        hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "zap", "error": "no such service", "ts": 5})
    )
    await press
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR).state == "zap"
    assert hass.states.get(SENSOR).attributes["error"] == "no such service"


@pytest.mark.parametrize("plugin", ["not a version", None])
async def test_a_version_that_cannot_be_read_takes_nothing_away(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    plugin: str | None,
) -> None:
    """Silence is not a "no": the button of a receiver whose `info` stops stating a
    release stays available, and only a stated older release makes it unavailable."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    assert hass.states.get(BUTTON).state == STATE_UNKNOWN

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "plugin": plugin}))
    await hass.async_block_till_done()

    assert hass.states.get(BUTTON).state == STATE_UNKNOWN


async def test_an_empty_payload_from_before_the_press_is_not_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`last_error` being empty already proves nothing about this press."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await hass.async_block_till_done()
    box: Enigma2Box = config_entry.runtime_data
    box.async_record_local_error("force_reinstall", "the receiver could not be reached")
    await hass.async_block_till_done()

    press = hass.async_create_task(_press(hass))
    await _command_sent(mqtt_mock)

    assert not press.done()
    assert hass.states.get(SENSOR).state == "force_reinstall"
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await press


async def test_a_complaint_about_another_command_is_not_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`last_error` moving is not `last_error` emptying: one topic, two conversations."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    await _complain(hass)

    press = hass.async_create_task(_press(hass))
    await _command_sent(mqtt_mock)
    async_fire_mqtt_message(
        hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "zap", "error": "no such service", "ts": 9})
    )
    # Not `async_block_till_done`: that would wait for the press, which is the point.
    for _ in range(20):
        await asyncio.sleep(0)

    assert not press.done()
    assert hass.states.get(SENSOR).state == "zap"

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await press
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == STATE_UNKNOWN


async def test_a_receiver_that_does_not_answer_clears_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No empty `last_error`, no clear: the press raises and the record stays."""
    monkeypatch.setitem(Enigma2Box.async_command.__kwdefaults__, "timeout", 0.3)
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    await _complain(hass)

    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass)

    assert raised.value.translation_key == "command_timeout"
    assert raised.value.translation_placeholders == {"command": "clear_error"}
    state = hass.states.get(SENSOR)
    assert state.state == "deep_standby"
    assert state.attributes["error"] == COMPLAINT["error"]


async def test_a_refusal_is_raised_in_the_receivers_words_and_clears_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """While an update is being applied the plugin refuses every command, this one too."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    await _complain(hass)
    await async_arm_box_error(hass, "clear_error", "an update is being applied")

    with pytest.raises(HomeAssistantError, match="an update is being applied"):
        await _press(hass)
    await hass.async_block_till_done()

    # The refusal is itself the newest complaint; nothing was wiped.
    state = hass.states.get(SENSOR)
    assert state.state == "clear_error"
    assert state.attributes["error"] == "an update is being applied"


async def test_the_button_is_unavailable_while_the_receiver_is_offline(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Offline, nothing is sent and the sensor keeps what it shows."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    await _complain(hass)

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()
    assert hass.states.get(BUTTON).state == STATE_UNAVAILABLE

    await _press(hass)
    await hass.async_block_till_done()

    assert published_payloads(mqtt_mock, command_topic("clear_error")) == []
    assert hass.states.get(SENSOR).state == "deep_standby"


async def test_a_downgraded_plugin_keeps_the_entity_and_makes_it_unavailable(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The entity is never removed; on a plugin without the command it cannot be pressed."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    events = _registry_events(hass)
    assert hass.states.get(BUTTON).state == STATE_UNKNOWN

    async_fire_mqtt_message(hass, INFO_TOPIC, _info("0.4.0"))
    await hass.async_block_till_done()

    assert events == []
    assert er.async_get(hass).async_get(BUTTON) is not None
    assert hass.states.get(BUTTON).state == STATE_UNAVAILABLE
    await _press(hass)
    await hass.async_block_till_done()
    assert published_payloads(mqtt_mock, command_topic("clear_error")) == []

    async_fire_mqtt_message(hass, INFO_TOPIC, _info("0.5.0"))
    await hass.async_block_till_done()
    assert hass.states.get(BUTTON).state == STATE_UNKNOWN


async def test_a_sensor_added_after_a_clear_keeps_what_it_restores(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A clear is an event for the entities that were there, not a flag for later ones."""
    await _setup(hass, config_entry, box_on_the_broker, "0.5.0")
    box: Enigma2Box = config_entry.runtime_data
    box.async_clear_error_record()
    await hass.async_block_till_done()

    from custom_components.enigma2_mqtt.sensor import Enigma2LastErrorSensor

    late = Enigma2LastErrorSensor(box)
    late.hass = hass

    async def _no_last_state() -> None:
        return None

    late.async_get_last_state = _no_last_state  # type: ignore[method-assign]
    late._attr_native_value = "zap"
    await late.async_added_to_hass()

    assert late.native_value == "zap"

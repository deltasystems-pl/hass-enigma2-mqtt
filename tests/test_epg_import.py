"""„Pobierz EPG" and „Import EPG": the receiver's own EPG import, on demand.

The import is the image's. The plugin only finds the importer the image already loaded,
starts it the way the importer's own „Manual" button does, and reports on a retained
`epg_import` topic what it is doing — whoever started it. What this integration adds is
a button behind two gates that both belong to the receiver, and a diagnostic that says
`idle`, `running`, `done` or `failed`.

The tests are shaped by three things that have gone wrong here before. A permission read
before `info` arrives reads „not said" on every start-up, so the gates are watched on
the entity registry rather than on the end state. A capability that goes quiet is not a
decision, so neither entity is removed for it. And a press must not report success on a
state it did not cause: an import the image's schedule started is already `running`,
and the receiver refuses a second one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.box import Enigma2Box
from custom_components.enigma2_mqtt.const import DOMAIN
from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    assert_published,
    async_arm_box_error,
    async_arm_box_reply,
    async_setup_box_then_retained,
    command_topic,
)

# The capability and the topic, spelled out rather than imported, so this module
# still collects against a tree without the feature and every test can be shown red.
CAPABILITY_EPG_IMPORT = "epg_import"
EPG_IMPORT_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/epg_import"
BUTTON = "button.dekoder_salon_import_epg"
SENSOR = "sensor.dekoder_salon_epg_import"

# The last scheduled run, as the plugin seeds the topic from the importer's own record
# after a start: nothing running, and the previous import's finish and count.
IDLE: dict[str, Any] = {
    "state": "idle",
    "started": None,
    "finished": 1789459692,
    "events": 120074,
    "error": None,
}
RUNNING: dict[str, Any] = {
    "state": "running",
    "started": 1789500000,
    "finished": None,
    "events": None,
    "error": None,
}
DONE: dict[str, Any] = {
    "state": "done",
    "started": 1789500000,
    "finished": 1789500090,
    "events": 120074,
    "error": None,
}
FAILED: dict[str, Any] = {
    "state": "failed",
    "started": 1789500000,
    "finished": 1789500090,
    "events": 0,
    "error": (
        "EPG-Importer finished without importing any events; "
        "its sources may be unreachable"
    ),
}


def info(*, capabilities: list[str] | None = None, **settings: Any) -> str:
    """Return an `info` payload with the capabilities and settings asked for."""
    payload = {**INFO}
    if capabilities is not None:
        payload["capabilities"] = capabilities
    if settings:
        payload["settings"] = settings
    return json.dumps(payload)


def with_import(**settings: Any) -> str:
    """Return an `info` payload from a box that found the image's importer."""
    return info(capabilities=[*INFO["capabilities"], CAPABILITY_EPG_IMPORT], **settings)


def registered(hass: HomeAssistant, platform: str, key: str) -> str | None:
    """Return the entity id one of this box's unique ids resolves to, if any."""
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{NODE_ID}_{key}")


def watch_registry(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Record every registry change as (action, entity id)."""
    seen: list[tuple[str, str]] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: seen.append((event.data["action"], event.data["entity_id"])),
    )
    return seen


async def _command_sent(mqtt_mock, topic: str) -> None:
    """Wait for a press to get its command out, but not for the press to finish."""
    for _ in range(200):
        await asyncio.sleep(0.005)
        if any(call.args[0] == topic for call in mqtt_mock.async_publish.call_args_list):
            for _ in range(5):
                await asyncio.sleep(0)
            return
    raise AssertionError(f"{topic} was never published")


def _press(hass: HomeAssistant):
    return hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
        )
    )


# --------------------------------------------------------------------- the button


async def test_a_stated_no_creates_no_button_even_for_an_instant(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Refused without the permission: the button is never offered.

    Watched on the registry, because an end state cannot tell „never created" from
    „created at setup and deleted when `info` arrived".
    """
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=False)
    config_entry.add_to_hass(hass)
    touched = watch_registry(hass)

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert registered(hass, "button", "epg_import") is None
    assert all(entity_id != BUTTON for _, entity_id in touched)
    # The capability really was there: the sensor exists, so the absence is the
    # permission talking.
    assert registered(hass, "sensor", "epg_import") is not None


async def test_an_older_plugin_gets_no_button(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Silence is not permission, and no installation has a button to keep."""
    box_on_the_broker[INFO_TOPIC] = with_import()

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert registered(hass, "button", "epg_import") is None


async def test_the_permission_without_the_capability_is_no_button(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver whose importer was not found claims no capability and refuses.

    The permission is a checkbox every installation has; the capability is claimed
    only where the importer is loaded and the EPG cache can import in place. A button
    offered on the first alone would be refused every time.
    """
    box_on_the_broker[INFO_TOPIC] = info(epg_import_allowed=True)

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert config_entry.runtime_data.epg_import_permission is True
    assert registered(hass, "button", "epg_import") is None
    assert registered(hass, "sensor", "epg_import") is None


async def test_the_capability_arriving_late_creates_the_permitted_button(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The gates listen rather than read once."""
    box_on_the_broker[INFO_TOPIC] = info(epg_import_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert hass.states.get(BUTTON) is None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_import(epg_import_allowed=True))
    await hass.async_block_till_done()

    assert hass.states.get(BUTTON) is not None
    assert hass.states.get(SENSOR) is not None


async def test_a_press_sends_the_command_and_waits_for_the_import_to_run(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """QoS 1, never retained, `PRESS` — and the press stays open until `running`."""
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    press = _press(hass)
    await _command_sent(mqtt_mock, command_topic("epg_import"))
    assert_published(mqtt_mock, command_topic("epg_import"), "PRESS")
    assert not press.done()

    async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, json.dumps(RUNNING))
    await press
    assert hass.states.get(SENSOR).state == "running"


async def test_a_press_answered_by_the_box_resolves_at_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The path a real receiver takes: the command, then the topic moving."""
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_reply(hass, "epg_import", EPG_IMPORT_TOPIC, json.dumps(RUNNING))

    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
    )

    assert hass.states.get(SENSOR).state == "running"


@pytest.mark.parametrize(
    "sentence",
    [
        "EPG import is not allowed on this box",
        "an EPG import is already running",
        "a recording is running",
        "a recording starts in under ten minutes",
        "EPG-Importer runs on its own schedule within ten minutes",
        "EPG-Importer has no sources selected on the receiver",
    ],
)
async def test_every_refusal_raises_the_boxs_own_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    sentence: str,
) -> None:
    """The guards live on the receiver; Home Assistant can only repeat them.

    Without the permission, while recording or with a timer due, while an import runs
    whoever started it, before the image's own scheduled run, and with no sources.
    """
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_error(hass, "epg_import", sentence)

    with pytest.raises(HomeAssistantError, match=sentence):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
        )


async def test_an_import_already_running_is_not_mistaken_for_the_press(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 The image's schedule started one; the receiver refuses a second.

    The topic already says `running`. A proof that read the state held rather than a
    new payload would return at once and report the refusal as a success.
    """
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(RUNNING)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_error(hass, "epg_import", "an EPG import is already running")

    with pytest.raises(HomeAssistantError, match="already running"):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
        )


async def test_a_new_failed_payload_raises_its_error_instead_of_timing_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Starting the importer threw: the topic says `failed` with the reason.

    That is an answer, and it is raised in the receiver's words at once rather than
    after the command timeout as „the receiver did not carry this out".
    """
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    thrown = {**FAILED, "error": "EPG-Importer could not be started: boom"}
    await async_arm_box_reply(hass, "epg_import", EPG_IMPORT_TOPIC, json.dumps(thrown))

    async with asyncio.timeout(2):
        with pytest.raises(HomeAssistantError, match="could not be started: boom"):
            await hass.services.async_call(
                BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
            )


async def test_a_new_payload_that_is_not_running_is_not_the_proof(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `idle` republished in the window proves nothing, and silence times out."""
    monkeypatch.setitem(Enigma2Box.async_command.__kwdefaults__, "timeout", 0.3)
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_reply(hass, "epg_import", EPG_IMPORT_TOPIC, json.dumps(IDLE))

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
        )
    assert raised.value.translation_key == "command_timeout"


async def test_the_payload_of_the_press_carries_nothing_else(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The plugin ignores the payload; the integration sends only the conventional one."""
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_reply(hass, "epg_import", EPG_IMPORT_TOPIC, json.dumps(RUNNING))

    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
    )

    sent = [
        call.args[1]
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == command_topic("epg_import")
    ]
    assert sent == ["PRESS"]


async def test_withdrawing_the_permission_takes_the_button_away(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A stated „no" at the television is a decision, and removes the registration."""
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert registered(hass, "button", "epg_import") is not None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_import(epg_import_allowed=False))
    await hass.async_block_till_done()

    assert registered(hass, "button", "epg_import") is None


@pytest.mark.parametrize(
    "later",
    [
        # The capability stops being named: a downgrade, a hook that did not attach.
        info(epg_import_allowed=True),
        # The permission stops being mentioned.
        with_import(),
    ],
    ids=["capability_quiet", "permission_unsaid"],
)
async def test_silence_removes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    later: str,
) -> None:
    """Neither entity is removed for something nobody decided."""
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    touched = watch_registry(hass)

    async_fire_mqtt_message(hass, INFO_TOPIC, later)
    await hass.async_block_till_done()

    assert registered(hass, "button", "epg_import") == BUTTON
    assert registered(hass, "sensor", "epg_import") == SENSOR
    assert ("remove", BUTTON) not in touched
    assert ("remove", SENSOR) not in touched


# --------------------------------------------------------------------- the sensor


async def test_the_sensor_needs_the_capability_and_not_the_permission(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The topic follows every import, the image's own included, so it is worth
    reading on a receiver that does not permit starting one from here."""
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert registered(hass, "sensor", "epg_import") is None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_import())
    async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, json.dumps(IDLE))
    await hass.async_block_till_done()

    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == "idle"


async def test_the_sensor_is_a_diagnostic_enumeration(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Four states and no others, in the diagnostic category."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    entry = er.async_get(hass).async_get(SENSOR)
    assert entry.entity_category == er.EntityCategory.DIAGNOSTIC
    assert hass.states.get(SENSOR).attributes["options"] == [
        "idle",
        "running",
        "done",
        "failed",
    ]


@pytest.mark.parametrize("payload", [IDLE, RUNNING, DONE, FAILED], ids=lambda p: p["state"])
async def test_the_sensor_follows_the_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    payload: dict[str, Any],
) -> None:
    """State is `state`; the times become ISO strings; events and error pass through."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(payload)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state.state == payload["state"]

    def iso(value: int | None) -> str | None:
        return None if value is None else dt_util.utc_from_timestamp(value).isoformat()

    assert state.attributes["started"] == iso(payload["started"])
    assert state.attributes["finished"] == iso(payload["finished"])
    assert state.attributes["events"] == payload["events"]
    assert state.attributes["error"] == payload["error"]


async def test_idle_to_running_to_done_with_timestamps(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The transitions a run produces, read in order off one entity."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    seen = [hass.states.get(SENSOR).state]
    for payload in (RUNNING, DONE):
        async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, json.dumps(payload))
        await hass.async_block_till_done()
        seen.append(hass.states.get(SENSOR).state)

    assert seen == ["idle", "running", "done"]
    attributes = hass.states.get(SENSOR).attributes
    assert attributes["started"] < attributes["finished"]


@pytest.mark.parametrize("state", ["", "RUNNING", "cancelled", None, True, 1])
async def test_a_state_outside_the_contract_is_unknown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    state: Any,
) -> None:
    """An automation branching on the state sees one of four words, or nothing."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps({**IDLE, "state": state})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(SENSOR).state == STATE_UNKNOWN


@pytest.mark.parametrize("reported", [0, -1, True, "1789500000", 1.5, 4_102_444_801])
async def test_a_time_that_is_not_a_clock_is_not_shown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """Zero is a field nobody filled in, and `True` is not a second of 1970."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(
        {**DONE, "started": reported, "finished": reported}
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    attributes = hass.states.get(SENSOR).attributes
    assert attributes["started"] is None
    assert attributes["finished"] is None


async def test_zero_events_is_a_reading(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An import that finished with nothing is exactly the failure worth seeing."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(FAILED)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(SENSOR).attributes["events"] == 0


@pytest.mark.parametrize("reported", [True, -1, "120074", 1.5, 100_000_001])
async def test_an_event_count_that_is_not_a_count_is_none(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """`True` would graph as one event; a negative number is not a count."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps({**DONE, "events": reported})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(SENSOR).attributes["events"] is None


@pytest.mark.parametrize("reported", ["", 5, True, ["x"], {"a": 1}])
async def test_an_error_that_is_not_a_sentence_is_none(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """The error is for a person; anything that is not text is not shown."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps({**FAILED, "error": reported})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(SENSOR).attributes["error"] is None


async def test_a_long_error_is_cut_not_dropped(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Cut at 255, like „Ostatni błąd" cuts the receiver's sentence."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps({**FAILED, "error": "x" * 400})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(SENSOR).attributes["error"] == "x" * 255


async def test_a_payload_that_is_not_json_leaves_the_last_reading_alone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """One malformed message is a bug at the other end, not news about the import."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(DONE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, "not json at all")
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR).state == "done"


async def test_an_empty_payload_retracts_the_reading(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A retracted retained topic is a receiver that has stopped answering."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(DONE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, "")
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR).state == STATE_UNAVAILABLE
    assert config_entry.runtime_data.state.epg_import is None


async def test_nothing_outside_the_contract_reaches_home_assistant(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A source URL the importer never reported must not appear because a payload grew one."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(
        {**FAILED, "source": "MARKER-SOURCE-8f2a", "url": "MARKER-URL-8f2a"}
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    attributes = json.dumps(dict(hass.states.get(SENSOR).attributes))
    download = json.dumps(await async_get_config_entry_diagnostics(hass, config_entry))
    for marker in ("MARKER-SOURCE-8f2a", "MARKER-URL-8f2a"):
        assert marker not in attributes
        assert marker not in download


async def test_the_download_carries_the_import_snapshot(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A report about a guide that stays thin is unanswerable without it."""
    box_on_the_broker[INFO_TOPIC] = with_import()
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(FAILED)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["topics"]["epg_import"] == FAILED


# ---------------------------------------------------------- what the household sees


async def test_the_polish_names_become_these_entity_ids(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 On a Polish installation the display name is the entity id.

    „Pobierz EPG" and „Import EPG" are load-bearing: renaming either later renames the
    entity on every Polish installation, and the automations with it.
    """
    hass.config.language = "pl"
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert registered(hass, "button", "epg_import") == "button.dekoder_salon_pobierz_epg"
    assert registered(hass, "sensor", "epg_import") == "sensor.dekoder_salon_import_epg"


# ------------------------------------------------------------- what is an answer


async def test_a_retained_replay_of_an_old_failure_is_not_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 A reconnect during the wait replays the previous run's retained `failed`.

    Home Assistant forgets which retained topics it has delivered when it reconnects
    to the broker, resubscribes, and the broker replays what it holds — here the
    `failed` of the last run. That is not the receiver answering this press; the live
    `running` arriving a moment later is. Simulated by clearing the client's record of
    delivered retained topics, which is what a reconnect does before it resubscribes.
    """
    from homeassistant.components import mqtt  # noqa: PLC0415
    from homeassistant.components.mqtt.models import DATA_MQTT  # noqa: PLC0415

    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(FAILED)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    client = hass.data[DATA_MQTT].client

    @callback
    def _reconnect_then_answer(_msg) -> None:
        client._retained_topics.clear()
        async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, json.dumps(FAILED), retain=True)
        hass.loop.call_later(
            0.05,
            lambda: async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, json.dumps(RUNNING)),
        )

    await mqtt.async_subscribe(hass, command_topic("epg_import"), _reconnect_then_answer)

    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
    )
    assert hass.states.get(SENSOR).state == "running"


async def test_the_first_answer_stands(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A `failed` and a `running` inside one tick still raise the failure.

    The waiter resumes only after the loop turns, and a check that kept taking the
    latest payload would read the second one and call the failed start a success.
    """
    from homeassistant.components import mqtt  # noqa: PLC0415

    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=True)
    box_on_the_broker[EPG_IMPORT_TOPIC] = json.dumps(IDLE)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    thrown = {**FAILED, "error": "EPG-Importer could not be started: boom"}

    @callback
    def _both(_msg) -> None:
        async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, json.dumps(thrown))
        async_fire_mqtt_message(hass, EPG_IMPORT_TOPIC, json.dumps(RUNNING))

    await mqtt.async_subscribe(hass, command_topic("epg_import"), _both)

    with pytest.raises(HomeAssistantError, match="could not be started: boom"):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
        )


@pytest.mark.parametrize("reported", ["yes", 1, "true", None])
async def test_a_permission_that_is_not_a_boolean_is_not_said(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """Only `true` opens the gate; anything else is a box that has not answered."""
    box_on_the_broker[INFO_TOPIC] = with_import(epg_import_allowed=reported)

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert config_entry.runtime_data.epg_import_permission is None
    assert registered(hass, "button", "epg_import") is None

"""A toggle that says what its row showed: the viewer's intent, repeated and never inverted.

A late or repeated OK carries the value the row showed when it was pressed (`seen`). Home
Assistant turns it into the explicit service for the other value, or calls nothing when the
entity already is there - and then publishes the menu again, so the receiver redraws the row.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.enigma2_mqtt.const import DOMAIN, EVENT_MENU_ACTION
from custom_components.enigma2_mqtt.menu import Refused, parse_action

from .conftest import NODE_ID, async_setup_box
from .menu_common import (
    MENU_ACTION_TOPIC,
    claim_menu,
    fire_action,
    items_by_id,
    last_menu,
    menu_entry,
    menu_publishes,
)
from .test_menu_actions import MENU, _states

MENU_LOGGER = "custom_components.enigma2_mqtt.menu"

# Every service a toggle row could reach for, so that a wrong one is recorded and seen.
SERVICES = [
    *((domain, service) for domain in ("light", "switch", "input_boolean", "fan")
      for service in ("turn_on", "turn_off", "toggle")),
    ("cover", "open_cover"),
    ("cover", "close_cover"),
    ("cover", "toggle"),
    ("lock", "lock"),
    ("lock", "unlock"),
    ("lock", "open"),
]

# The toggle rows of the shared menu: row id -> entity id.
TOGGLES = {
    "r_lamp": "light.salon",
    "r_switch": "switch.kettle",
    "r_bool": "input_boolean.guests",
    "r_fan": "fan.salon",
    "r_blind_toggle": "cover.salon",
    "r_lock": "lock.front",
}


@pytest.fixture
async def menu_box(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> Any:
    """The example receiver, claiming `menu`, with the shared menu set up."""
    _states(hass)
    claim_menu(box_on_the_broker)
    entry = menu_entry(MENU)
    await async_setup_box(hass, entry)
    return entry


@pytest.fixture
def calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Every service a toggle row may call, recorded instead of performed."""
    recorded: list[ServiceCall] = []

    async def _record(call: ServiceCall) -> None:
        recorded.append(call)

    for domain, service in SERVICES:
        hass.services.async_register(domain, service, _record)
    return recorded


def _performed(calls: list[ServiceCall]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(call.domain, call.service, dict(call.data)) for call in calls]


async def _quiet(hass: HomeAssistant) -> None:
    """Let the debouncer's cooldown pass, so the next publish goes out at once."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()


def _set(hass: HomeAssistant, entity_id: str, state: str) -> None:
    attributes = {"supported_features": 15} if entity_id.startswith("cover.") else {}
    hass.states.async_set(entity_id, state, attributes)


# Every cell of the table: the row, the entity's state when the action arrives, what the row
# showed, and the service called - None for nothing.
CELLS = [
    # light, switch, input_boolean, fan: on/off.
    ("r_lamp", "off", False, "turn_on"),
    ("r_lamp", "on", False, None),
    ("r_lamp", "on", True, "turn_off"),
    ("r_lamp", "off", True, None),
    ("r_switch", "off", False, "turn_on"),
    ("r_switch", "on", False, None),
    ("r_switch", "on", True, "turn_off"),
    ("r_switch", "off", True, None),
    ("r_bool", "off", False, "turn_on"),
    ("r_bool", "on", False, None),
    ("r_bool", "on", True, "turn_off"),
    ("r_bool", "off", True, None),
    ("r_fan", "off", False, "turn_on"),
    ("r_fan", "on", False, None),
    ("r_fan", "on", True, "turn_off"),
    ("r_fan", "off", True, None),
    # cover: open exactly when the row would show true - opening included.
    ("r_blind_toggle", "closed", False, "open_cover"),
    ("r_blind_toggle", "closing", False, "open_cover"),
    ("r_blind_toggle", "open", False, None),
    ("r_blind_toggle", "opening", False, None),
    ("r_blind_toggle", "open", True, "close_cover"),
    ("r_blind_toggle", "opening", True, "close_cover"),
    ("r_blind_toggle", "closed", True, None),
    ("r_blind_toggle", "closing", True, None),
    # lock: locked is true; unlocked and open are false.
    ("r_lock", "unlocked", False, "lock"),
    ("r_lock", "open", False, "lock"),
    ("r_lock", "locked", False, None),
    ("r_lock", "locked", True, "unlock"),
    ("r_lock", "unlocked", True, None),
    ("r_lock", "open", True, None),
    # A state that is neither is not "already there": the explicit service is called.
    ("r_lamp", "unknown", False, "turn_on"),
    ("r_lamp", "unknown", True, "turn_off"),
    ("r_blind_toggle", "unknown", False, "open_cover"),
    ("r_blind_toggle", "unknown", True, "close_cover"),
    ("r_lock", "jammed", False, "lock"),
    ("r_lock", "jammed", True, "unlock"),
]


@pytest.mark.parametrize(("row_id", "state", "seen", "service"), CELLS)
async def test_a_toggle_with_seen_calls_the_explicit_service_or_nothing(
    hass: HomeAssistant,
    menu_box: Any,
    calls: list[ServiceCall],
    row_id: str,
    state: str,
    seen: bool,
    service: str | None,
) -> None:
    """The service for the other value than the one the row showed, unless already there."""
    entity_id = TOGGLES[row_id]
    _set(hass, entity_id, state)
    await hass.async_block_till_done()

    fire_action(hass, row_id, "toggle", seen=seen)
    await hass.async_block_till_done()

    expected = [] if service is None else [
        (entity_id.split(".")[0], service, {"entity_id": entity_id})
    ]
    assert _performed(calls) == expected


# `seen` null, as a row with no state sends it: exactly what an action without `seen` does.
NULL_CELLS = [
    ("r_lamp", "on", "toggle"),
    ("r_lamp", "off", "toggle"),
    ("r_switch", "off", "toggle"),
    ("r_bool", "on", "toggle"),
    ("r_fan", "off", "toggle"),
    ("r_blind_toggle", "open", "close_cover"),
    ("r_blind_toggle", "closed", "open_cover"),
    ("r_lock", "locked", "unlock"),
    ("r_lock", "unlocked", "lock"),
]


@pytest.mark.parametrize(("row_id", "state", "service"), NULL_CELLS)
@pytest.mark.parametrize("with_seen", [True, False], ids=["null", "absent"])
async def test_a_toggle_with_seen_null_or_without_it_acts_as_before(
    hass: HomeAssistant,
    menu_box: Any,
    calls: list[ServiceCall],
    row_id: str,
    state: str,
    service: str,
    with_seen: bool,
) -> None:
    """The domain's toggle, and a cover or lock by its state when the action arrives."""
    entity_id = TOGGLES[row_id]
    _set(hass, entity_id, state)
    await hass.async_block_till_done()

    extra = {"seen": None} if with_seen else {}
    fire_action(hass, row_id, "toggle", **extra)
    await hass.async_block_till_done()

    assert _performed(calls) == [(entity_id.split(".")[0], service, {"entity_id": entity_id})]


@pytest.mark.parametrize(("row_id", "state"), [("r_blind_toggle", "unknown"), ("r_lock", "jammed")])
async def test_a_toggle_with_seen_null_on_a_state_that_is_neither_is_refused(
    hass: HomeAssistant,
    menu_box: Any,
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
    row_id: str,
    state: str,
) -> None:
    """As without `seen`: a cover or lock with no state to go by has no other one."""
    _set(hass, TOGGLES[row_id], state)
    await hass.async_block_till_done()

    fire_action(hass, row_id, "toggle", seen=None)
    await hass.async_block_till_done()

    assert calls == []
    assert "(state_unknown" in caplog.text


async def test_a_met_intent_calls_nothing_and_publishes_the_menu_again(
    hass: HomeAssistant,
    menu_box: Any,
    mqtt_mock,
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No service, no event, one debug line, and the menu out again with a new `rev`."""
    caplog.set_level(logging.DEBUG, logger=MENU_LOGGER)
    events = async_capture_events(hass, EVENT_MENU_ACTION)
    await _quiet(hass)
    before = menu_publishes(mqtt_mock)
    first = last_menu(mqtt_mock)
    assert first is not None
    assert items_by_id(first)["r_lamp"]["value"] is True
    caplog.clear()

    # The row showed "off"; the lamp is on already.
    fire_action(hass, "r_lamp", "toggle", seen=False)
    await hass.async_block_till_done()

    assert calls == []
    assert events == []
    assert len(menu_publishes(mqtt_mock)) == len(before) + 1
    again = last_menu(mqtt_mock)
    assert again is not None
    assert again["rev"] > first["rev"]
    assert again["items"] == first["items"]
    ours = [record for record in caplog.records if record.name.startswith(MENU_LOGGER)]
    assert [record.levelno for record in ours] == [logging.DEBUG]
    assert "called nothing for row r_lamp" in ours[0].getMessage()
    assert not [
        record for record in caplog.records
        if record.levelno > logging.DEBUG and record.name.startswith("custom_components")
    ]


async def test_met_intents_in_a_burst_publish_debounced(
    hass: HomeAssistant, menu_box: Any, mqtt_mock, calls: list[ServiceCall]
) -> None:
    """Anyone on the broker can send these; the menu still goes out at most every 0.5 s."""
    await _quiet(hass)
    before = len(menu_publishes(mqtt_mock))
    for n in range(10):
        fire_action(hass, "r_lamp", "toggle", seen=False, action_id=f"{n:012x}")
    await hass.async_block_till_done()
    assert len(menu_publishes(mqtt_mock)) == before + 1

    await _quiet(hass)
    assert len(menu_publishes(mqtt_mock)) == before + 2
    assert calls == []


async def test_the_late_ok_repeats_the_intent_and_does_not_switch_the_lamp_off(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """Two OKs pressed on a row showing "off": the lamp is switched on once and stays on.

    The second arrives after the first was performed, still saying the row showed "off" -
    a plain toggle would have switched the lamp off again.
    """
    _set(hass, "light.salon", "off")
    await hass.async_block_till_done()

    fire_action(hass, "r_lamp", "toggle", seen=False, action_id="000000000001", rev=3)
    await hass.async_block_till_done(wait_background_tasks=True)
    _set(hass, "light.salon", "on")
    await hass.async_block_till_done()
    fire_action(hass, "r_lamp", "toggle", seen=False, action_id="000000000002", rev=3)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _performed(calls) == [("light", "turn_on", {"entity_id": "light.salon"})]


async def test_an_explicit_service_fires_the_event_as_a_toggle(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """The event says what the viewer did - a toggle - whichever service carried it out."""
    events = async_capture_events(hass, EVENT_MENU_ACTION)
    fire_action(hass, "r_lamp", "toggle", seen=True)
    await hass.async_block_till_done()

    assert _performed(calls) == [("light", "turn_off", {"entity_id": "light.salon"})]
    assert [(event.data["row_id"], event.data["action"], event.data["value"])
            for event in events] == [("r_lamp", "toggle", None)]


async def test_a_met_intent_costs_no_flood_budget_and_is_never_busy(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """It calls nothing, so it is neither counted nor held as the row's action."""
    with patch("custom_components.enigma2_mqtt.menu._monotonic", return_value=1000.0):
        for n in range(6):
            fire_action(hass, "r_lamp", "toggle", seen=False, action_id=f"{n:012x}")
        await hass.async_block_till_done(wait_background_tasks=True)
        fire_action(hass, "r_switch", "toggle", seen=False, action_id="0000000000ff")
        await hass.async_block_till_done(wait_background_tasks=True)

    assert "(flood" not in caplog.text
    assert "(busy" not in caplog.text
    assert _performed(calls) == [("switch", "turn_on", {"entity_id": "switch.kettle"})]


async def test_a_met_intent_on_a_row_whose_call_is_in_flight_is_not_refused(
    hass: HomeAssistant, menu_box: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The entity got there before the call returned: nothing more to do, and no refusal."""
    started: list[ServiceCall] = []
    release = asyncio.Event()

    async def _slow(call: ServiceCall) -> None:
        started.append(call)
        await release.wait()

    hass.services.async_register("switch", "turn_on", _slow)
    fire_action(hass, "r_switch", "toggle", seen=False, action_id="000000000001")
    await hass.async_block_till_done()
    assert len(started) == 1
    _set(hass, "switch.kettle", "on")
    await hass.async_block_till_done()

    fire_action(hass, "r_switch", "toggle", seen=False, action_id="000000000002")
    await hass.async_block_till_done()

    assert len(started) == 1
    assert "(busy" not in caplog.text
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_met_intent_leaves_its_row_free_for_the_next_press(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """Never held as the row's action in progress: a real toggle right after it is taken."""
    fire_action(hass, "r_lamp", "toggle", seen=False, action_id="000000000001")
    await hass.async_block_till_done(wait_background_tasks=True)
    fire_action(hass, "r_lamp", "toggle", seen=True, action_id="000000000002")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert "(busy" not in caplog.text
    assert _performed(calls) == [("light", "turn_off", {"entity_id": "light.salon"})]


async def test_a_met_intent_delivered_twice_publishes_the_menu_once(
    hass: HomeAssistant, menu_box: Any, mqtt_mock, calls: list[ServiceCall], caplog
) -> None:
    """QoS 1 may deliver it again; the id says it is the same press, and it is refused."""
    await _quiet(hass)
    before = len(menu_publishes(mqtt_mock))
    fire_action(hass, "r_lamp", "toggle", seen=False, action_id="000000000001")
    await hass.async_block_till_done()
    await _quiet(hass)
    assert len(menu_publishes(mqtt_mock)) == before + 1

    fire_action(hass, "r_lamp", "toggle", seen=False, action_id="000000000001")
    await hass.async_block_till_done()
    await _quiet(hass)

    assert "(duplicate: 000000000001)" in caplog.text
    assert len(menu_publishes(mqtt_mock)) == before + 1
    assert calls == []


async def test_a_met_intent_stamped_now_clears_the_clock_repair(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """Accepted like any action inside the age limit: the receiver's clock is right."""
    issue_id = f"menu_clock_behind_{menu_box.entry_id}"
    stale = int(dt_util.utcnow().timestamp()) - 3600
    fire_action(hass, "r_lamp", "toggle", seen=False, action_id="000000000001", ts=stale)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    fire_action(hass, "r_lamp", "toggle", seen=False, action_id="000000000002")
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert calls == []


@pytest.mark.parametrize(
    ("action", "seen"),
    [
        ("toggle", '"false"'),
        ("toggle", "0"),
        ("toggle", "1"),
        ("toggle", "[]"),
        ("toggle", "[false]"),
        ("toggle", "{}"),
        ("press", "false"),
        ("press", "null"),
        ("set", "true"),
    ],
)
def test_seen_that_is_not_a_row_value_or_not_on_a_toggle_is_malformed(
    action: str, seen: str
) -> None:
    payload = (
        '{"id":"3f9c2e4a1b7d","rev":1,"row":"r_a","action":"' + action + '","value":1,'
        '"seen":' + seen + ',"ts":1000}'
    ).encode()
    with pytest.raises(Refused) as caught:
        parse_action(payload, now=1000)
    assert (caught.value.cause, caught.value.detail) == ("malformed", "seen")


@pytest.mark.parametrize(
    ("member", "seen"),
    [(',"seen":true', True), (',"seen":false', False), (',"seen":null', None), ("", None)],
)
def test_seen_is_read_on_a_toggle(member: str, seen: bool | None) -> None:
    payload = (
        '{"id":"3f9c2e4a1b7d","rev":42,"row":"r_lamp","action":"toggle"' + member
        + ',"ts":1000}'
    ).encode()
    assert parse_action(payload, now=1000).seen is seen


@pytest.mark.parametrize(
    ("row_id", "action", "seen"),
    [
        ("r_lamp", "toggle", "false"),
        ("r_lamp", "toggle", 0),
        ("r_lamp", "toggle", [False]),
        ("r_lamp", "toggle", {"value": False}),
        ("r_movie", "press", False),
        ("r_dim", "set", True),
    ],
)
async def test_a_malformed_seen_is_refused_with_one_log_line(
    hass: HomeAssistant,
    menu_box: Any,
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
    row_id: str,
    action: str,
    seen: Any,
) -> None:
    """Nothing is called, nothing is fired, and the log says what was wrong."""
    events = async_capture_events(hass, EVENT_MENU_ACTION)
    payload = {
        "id": "3f9c2e4a1b7d",
        "rev": 1,
        "row": row_id,
        "action": action,
        "seen": seen,
        "ts": int(dt_util.utcnow().timestamp()),
    }
    if action == "set":
        payload["value"] = 50
    async_fire_mqtt_message(hass, MENU_ACTION_TOPIC, json.dumps(payload))
    await hass.async_block_till_done()

    assert calls == []
    assert events == []
    refusals = [
        record for record in caplog.records if "Refused a menu action" in record.getMessage()
    ]
    assert len(refusals) == 1
    assert f"Refused a menu action from {NODE_ID} (malformed: seen)" in caplog.text

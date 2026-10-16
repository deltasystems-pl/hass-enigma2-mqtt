"""A viewer's OK on the receiver menu: what Home Assistant performs, and what it refuses."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_mqtt_message,
    async_mock_service,
)

from custom_components.enigma2_mqtt.const import DOMAIN, EVENT_MENU_ACTION
from custom_components.enigma2_mqtt.menu import Refused, parse_action

from .conftest import NODE_ID, async_setup_box
from .menu_common import (
    MENU_ACTION_TOPIC,
    claim_menu,
    fire_action,
    group,
    items_by_id,
    last_menu,
    menu_entry,
    row,
)

MENU = [
    group(
        "g_salon",
        "Salon",
        [
            row("r_lamp", "light.salon", "toggle"),
            row("r_dim", "light.salon", "slider"),
            row("r_blind", "cover.salon", "slider"),
            row("r_blind_toggle", "cover.salon", "toggle"),
            row("r_mode", "input_select.mode", "selector"),
            row("r_heat", "climate.salon", "selector"),
            row("r_volume", "media_player.salon", "slider"),
            row("r_fan", "fan.salon", "toggle"),
        ],
    ),
    row("r_movie", "script.movie", "button"),
    row("r_scene", "scene.evening", "button"),
    row("r_button", "button.doorbell_test", "button"),
    row("r_input_button", "input_button.ring", "button"),
    row("r_automation", "automation.goodnight", "button"),
    row("r_door", "binary_sensor.front_door", "indicator"),
    row("r_lock", "lock.front", "toggle"),
    row("r_switch", "switch.kettle", "toggle"),
    row("r_bool", "input_boolean.guests", "toggle"),
    row("r_select", "select.speed", "selector"),
    row("r_target", "input_number.target", "slider"),
    row("r_number", "number.offset", "slider"),
    row("r_note", "input_text.note", "input"),
    row("r_text", "text.message", "input"),
    row("r_off", "switch.offline", "toggle"),
]


def _states(hass: HomeAssistant) -> None:
    hass.states.async_set("light.salon", "on", {"brightness": 128,
                                                "supported_color_modes": ["brightness"]})
    hass.states.async_set("cover.salon", "open", {"current_position": 40,
                                                  "supported_features": 15})
    hass.states.async_set("input_select.mode", "Evening",
                          {"options": ["Morning", "Evening", "Night"]})
    hass.states.async_set("climate.salon", "heat", {"hvac_modes": ["off", "heat", "auto"]})
    hass.states.async_set("media_player.salon", "playing", {"volume_level": 0.35,
                                                            "supported_features": 4})
    hass.states.async_set("fan.salon", "off")
    hass.states.async_set("script.movie", "off")
    hass.states.async_set("scene.evening", "unknown")
    hass.states.async_set("button.doorbell_test", "unknown")
    hass.states.async_set("input_button.ring", "unknown")
    hass.states.async_set("automation.goodnight", "on")
    hass.states.async_set("binary_sensor.front_door", "off", {"device_class": "door"})
    hass.states.async_set("lock.front", "locked")
    hass.states.async_set("switch.kettle", "off")
    hass.states.async_set("input_boolean.guests", "off")
    hass.states.async_set("select.speed", "low", {"options": ["low", "high"]})
    hass.states.async_set("input_number.target", "250", {"min": 0, "max": 1000, "step": 1})
    hass.states.async_set("number.offset", "0.5", {"min": -2, "max": 2, "step": 0.5})
    hass.states.async_set("input_text.note", "Buy milk", {"max": 10})
    hass.states.async_set("text.message", "", {"max": 300})
    hass.states.async_set("switch.offline", "unavailable")


@pytest.fixture
async def menu_box(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> Any:
    """The example receiver, claiming `menu`, with the menu above set up."""
    _states(hass)
    claim_menu(box_on_the_broker)
    entry = menu_entry(MENU)
    await async_setup_box(hass, entry)
    return entry


SERVICES = [
    ("script", "turn_on"),
    ("scene", "turn_on"),
    ("button", "press"),
    ("input_button", "press"),
    ("automation", "trigger"),
    ("light", "toggle"),
    ("light", "turn_on"),
    ("fan", "toggle"),
    ("switch", "toggle"),
    ("input_boolean", "toggle"),
    ("cover", "open_cover"),
    ("cover", "close_cover"),
    ("cover", "set_cover_position"),
    ("lock", "lock"),
    ("lock", "unlock"),
    ("select", "select_option"),
    ("input_select", "select_option"),
    ("climate", "set_hvac_mode"),
    ("media_player", "volume_set"),
    ("input_number", "set_value"),
    ("number", "set_value"),
    ("input_text", "set_value"),
    ("text", "set_value"),
]


@pytest.fixture
def calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Every service a menu row may call, recorded instead of performed."""
    recorded: list[ServiceCall] = []

    async def _record(call: ServiceCall) -> None:
        recorded.append(call)

    for domain, service in SERVICES:
        hass.services.async_register(domain, service, _record)
    return recorded


def _performed(calls: list[ServiceCall]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(call.domain, call.service, dict(call.data)) for call in calls]


@pytest.mark.parametrize(
    ("row_id", "action", "value", "expected"),
    [
        ("r_movie", "press", None, ("script", "turn_on", {"entity_id": "script.movie"})),
        ("r_scene", "press", None, ("scene", "turn_on", {"entity_id": "scene.evening"})),
        ("r_button", "press", None,
         ("button", "press", {"entity_id": "button.doorbell_test"})),
        ("r_input_button", "press", None,
         ("input_button", "press", {"entity_id": "input_button.ring"})),
        ("r_automation", "press", None,
         ("automation", "trigger", {"entity_id": "automation.goodnight"})),
        ("r_lamp", "toggle", None, ("light", "toggle", {"entity_id": "light.salon"})),
        ("r_fan", "toggle", None, ("fan", "toggle", {"entity_id": "fan.salon"})),
        ("r_switch", "toggle", None, ("switch", "toggle", {"entity_id": "switch.kettle"})),
        ("r_bool", "toggle", None,
         ("input_boolean", "toggle", {"entity_id": "input_boolean.guests"})),
        # An open cover is closed, a locked lock unlocked: the pair, by the state shown.
        ("r_blind_toggle", "toggle", None,
         ("cover", "close_cover", {"entity_id": "cover.salon"})),
        ("r_lock", "toggle", None, ("lock", "unlock", {"entity_id": "lock.front"})),
        ("r_dim", "set", 57,
         ("light", "turn_on", {"entity_id": "light.salon", "brightness_pct": 60})),
        ("r_dim", "set", 0,
         ("light", "turn_on", {"entity_id": "light.salon", "brightness_pct": 0})),
        ("r_blind", "set", 70,
         ("cover", "set_cover_position", {"entity_id": "cover.salon", "position": 70})),
        ("r_volume", "set", 30,
         ("media_player", "volume_set",
          {"entity_id": "media_player.salon", "volume_level": 0.3})),
        ("r_target", "set", 252,
         ("input_number", "set_value", {"entity_id": "input_number.target", "value": 250})),
        ("r_number", "set", -1.4,
         ("number", "set_value", {"entity_id": "number.offset", "value": -1.5})),
        ("r_mode", "set", "Night",
         ("input_select", "select_option",
          {"entity_id": "input_select.mode", "option": "Night"})),
        ("r_select", "set", "high",
         ("select", "select_option", {"entity_id": "select.speed", "option": "high"})),
        # The receiver shows the household's word; the entity is given its own.
        ("r_heat", "set", "Auto",
         ("climate", "set_hvac_mode", {"entity_id": "climate.salon", "hvac_mode": "auto"})),
        ("r_note", "set", "Eggs",
         ("input_text", "set_value", {"entity_id": "input_text.note", "value": "Eggs"})),
        ("r_text", "set", "x" * 255,
         ("text", "set_value", {"entity_id": "text.message", "value": "x" * 255})),
    ],
)
async def test_every_binding_performs_its_service(
    hass: HomeAssistant,
    menu_box: Any,
    calls: list[ServiceCall],
    row_id: str,
    action: str,
    value: Any,
    expected: tuple[str, str, dict[str, Any]],
) -> None:
    """The row id and the value come from the receiver; the service comes from the options."""
    fire_action(hass, row_id, action, value)
    await hass.async_block_till_done()

    assert _performed(calls) == [expected]


async def test_a_closed_cover_opens_and_an_unlocked_lock_locks(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    hass.states.async_set("cover.salon", "closed", {"current_position": 0,
                                                    "supported_features": 15})
    hass.states.async_set("lock.front", "unlocked")

    fire_action(hass, "r_blind_toggle", "toggle", action_id="000000000001")
    fire_action(hass, "r_lock", "toggle", action_id="000000000002")
    await hass.async_block_till_done()

    assert sorted(_performed(calls)) == [
        ("cover", "open_cover", {"entity_id": "cover.salon"}),
        ("lock", "lock", {"entity_id": "lock.front"}),
    ]


async def test_a_performed_action_fires_an_event(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """Automations can follow what the viewer did, with the value that was applied."""
    events = async_capture_events(hass, EVENT_MENU_ACTION)
    box = menu_box.runtime_data

    fire_action(hass, "r_dim", "set", 57)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        "device_id": box.device_id,
        "node_id": NODE_ID,
        "row_id": "r_dim",
        "entity_id": "light.salon",
        "action": "set",
        "value": 60,
    }


async def test_a_failing_service_is_logged_and_fires_no_event(
    hass: HomeAssistant, menu_box: Any, caplog: pytest.LogCaptureFixture
) -> None:
    events = async_capture_events(hass, EVENT_MENU_ACTION)

    async def _fail(call: ServiceCall) -> None:
        raise HomeAssistantError("the kettle is empty")

    hass.services.async_register("switch", "toggle", _fail)
    fire_action(hass, "r_switch", "toggle")
    await hass.async_block_till_done()

    assert events == []
    assert "could not call switch.toggle for switch.kettle" in caplog.text


@pytest.mark.parametrize(
    ("row_id", "action", "value", "cause"),
    [
        ("r_nothing", "press", None, "unknown_row"),
        ("g_salon", "press", None, "unknown_row"),
        ("r_door", "toggle", None, "not_actionable"),
        ("r_door", "set", True, "not_actionable"),
        ("r_lamp", "press", None, "wrong_action"),
        ("r_lamp", "set", True, "wrong_action"),
        ("r_movie", "toggle", None, "wrong_action"),
        ("r_dim", "toggle", None, "wrong_action"),
        ("r_off", "toggle", None, "unavailable"),
        ("r_dim", "set", 101, "invalid_value"),
        ("r_dim", "set", -1, "invalid_value"),
        ("r_dim", "set", "50", "invalid_value"),
        ("r_dim", "set", True, "invalid_value"),
        ("r_dim", "set", None, "invalid_value"),
        ("r_mode", "set", "Afternoon", "invalid_value"),
        ("r_mode", "set", 1, "invalid_value"),
        # The entity's own word is not what the menu offered.
        ("r_heat", "set", "auto", "invalid_value"),
        ("r_note", "set", "x" * 11, "invalid_value"),
        ("r_note", "set", 5, "invalid_value"),
    ],
)
async def test_an_action_the_row_does_not_take_is_refused(
    hass: HomeAssistant,
    menu_box: Any,
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
    row_id: str,
    action: str,
    value: Any,
    cause: str,
) -> None:
    """Nothing is called, and the log says why."""
    events = async_capture_events(hass, EVENT_MENU_ACTION)
    fire_action(hass, row_id, action, value)
    await hass.async_block_till_done()

    assert calls == []
    assert events == []
    assert f"({cause}" in caplog.text


async def test_a_toggle_whose_state_is_unknown_is_refused(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """A lock that is jamming has no "other" state to be put in."""
    hass.states.async_set("lock.front", "jammed")
    fire_action(hass, "r_lock", "toggle")
    await hass.async_block_till_done()

    assert calls == []
    assert "(state_unknown" in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"[1, 2]",
        b'{"id": "XYZ", "rev": 1, "row": "r_lamp", "action": "toggle"}',
        b'{"id": "3f9c2e4a1b7", "rev": 1, "row": "r_lamp", "action": "toggle"}',
        b'{"id": "3f9c2e4a1b7d", "rev": -1, "row": "r_lamp", "action": "toggle"}',
        b'{"id": "3f9c2e4a1b7d", "rev": true, "row": "r_lamp", "action": "toggle"}',
        b'{"id": "3f9c2e4a1b7d", "rev": "1", "row": "r_lamp", "action": "toggle"}',
        b'{"id": "3f9c2e4a1b7d", "rev": 1, "row": "r lamp", "action": "toggle"}',
        b'{"id": "3f9c2e4a1b7d", "rev": 1, "row": "r_lamp", "action": "flip"}',
        b'{"id": "3f9c2e4a1b7d", "rev": 1, "row": "r_dim", "action": "set"}',
        b'{"id": "3f9c2e4a1b7d", "rev": 1, "row": "r_lamp", "action": "toggle", "ts": "x"}',
        b"\xff\xfe",
        b'{"id": "3f9c2e4a1b7d", "rev": 1, "row": "r_lamp", "action": "toggle", "pad": "'
        + b"x" * 5000
        + b'"}',
    ],
)
async def test_a_message_that_is_not_an_action_is_refused(
    hass: HomeAssistant,
    menu_box: Any,
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
    payload: bytes,
) -> None:
    async_fire_mqtt_message(hass, MENU_ACTION_TOPIC, payload)
    await hass.async_block_till_done()

    assert calls == []
    assert "Refused a menu action" in caplog.text


async def test_an_older_rev_is_accepted(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """A state change between drawing and pressing must not make the remote dead."""
    fire_action(hass, "r_switch", "toggle", rev=0)
    await hass.async_block_till_done()

    assert _performed(calls) == [("switch", "toggle", {"entity_id": "switch.kettle"})]


async def test_an_action_without_ts_is_accepted(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """`ts` is for the logs; only a `ts` that is not a number is refused."""
    async_fire_mqtt_message(
        hass,
        MENU_ACTION_TOPIC,
        b'{"id":"3f9c2e4a1b7d","rev":7,"row":"r_switch","action":"toggle"}',
    )
    await hass.async_block_till_done()

    assert len(calls) == 1


async def test_a_redelivered_action_is_performed_once(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """QoS 1 may deliver a message twice; the id says it is the same press."""
    fire_action(hass, "r_switch", "toggle")
    fire_action(hass, "r_switch", "toggle")
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert "(duplicate" in caplog.text


async def test_a_retained_action_is_refused(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """A retained action is somebody's leftover; it would fire at every reconnect."""
    fire_action(hass, "r_switch", "toggle", retain=True)
    await hass.async_block_till_done()

    assert calls == []
    assert "(retained" in caplog.text


async def test_nothing_is_performed_for_a_receiver_that_does_not_claim_menu(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plugin without the menu sends no actions; a message claiming to be one is nobody's."""
    _states(hass)
    await async_setup_box(hass, menu_entry(MENU))

    fire_action(hass, "r_switch", "toggle")
    await hass.async_block_till_done()

    assert calls == []
    assert "(not_claimed" in caplog.text


async def test_a_refusal_is_a_warning_once_per_cause(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """Anyone on the broker can send these; the log must not become theirs to fill."""
    caplog.set_level(logging.DEBUG, logger="custom_components.enigma2_mqtt.menu")
    for n in range(5):
        fire_action(hass, "r_nothing", "press", action_id=f"00000000000{n}")
        await hass.async_block_till_done()

    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING and "unknown_row" in record.getMessage()
    ]
    debug = [
        record for record in caplog.records
        if record.levelno == logging.DEBUG and "unknown_row" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert len(debug) == 4


async def test_an_action_for_a_row_removed_since_is_refused(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """After the options change, only the rows the menu holds now can be acted on."""
    hass.config_entries.async_update_entry(
        menu_box, options={"menu": [row("r_other", "switch.kettle", "toggle")]}
    )
    assert await hass.config_entries.async_reload(menu_box.entry_id)
    await hass.async_block_till_done()

    fire_action(hass, "r_switch", "toggle")
    await hass.async_block_till_done()

    assert calls == []
    assert "(unknown_row" in caplog.text


def test_parse_action_reads_every_field() -> None:
    # `now` given: a fixed `ts` must not age into a refusal as the test suite gets older.
    action = parse_action(
        b'{"id":"3f9c2e4a1b7d","rev":42,"row":"r_blind","action":"set","value":60,'
        b'"ts":1792050000,"later":"ignored"}',
        now=1792050030,
    )
    assert (action.action_id, action.rev, action.row, action.action, action.value) == (
        "3f9c2e4a1b7d", 42, "r_blind", "set", 60,
    )
    with pytest.raises(Refused):
        parse_action(None)



@pytest.mark.parametrize(
    ("age", "cause"),
    [(0, None), (100, None), (-30, None), (-100, None),
     (125, "stale"), (3600, "stale"), (-125, "future"), (-3600, "future")],
)
async def test_an_action_more_than_two_minutes_off_is_refused(
    hass: HomeAssistant,
    menu_box: Any,
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
    age: int,
    cause: str | None,
) -> None:
    """Pressed for a screen that is long gone, or stamped by a clock far ahead of ours."""
    fire_action(hass, "r_switch", "toggle", ts=int(dt_util.utcnow().timestamp()) - age)
    await hass.async_block_till_done()

    assert (len(calls) == 1) is (cause is None)
    assert ("(stale" in caplog.text) is (cause == "stale")
    assert ("(future" in caplog.text) is (cause == "future")


@pytest.mark.parametrize(
    ("now", "cause"),
    [(1120.0, None), (1120.001, "stale"), (1000.0, None), (900.0, None),
     (880.0, None), (879.999, "future")],
)
def test_the_age_limit_is_120_seconds_either_way(now: float, cause: str | None) -> None:
    """At exactly 120 s an action still counts; a moment later, or earlier, it does not."""
    payload = b'{"id":"3f9c2e4a1b7d","rev":1,"row":"r_a","action":"toggle","ts":1000}'
    if cause is not None:
        with pytest.raises(Refused) as caught:
            parse_action(payload, now=now)
        assert caught.value.cause == cause
    else:
        assert parse_action(payload, now=now).row == "r_a"


async def test_a_row_whose_registry_id_names_nothing_cannot_be_acted_on(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entity was deleted: the row is greyed and an action on it is refused."""
    claim_menu(box_on_the_broker)
    entry = menu_entry([row("r_gone", "0123456789abcdef0123456789abcdef", "toggle")])
    await async_setup_box(hass, entry)

    fire_action(hass, "r_gone", "toggle")
    await hass.async_block_till_done()

    assert calls == []
    assert "(unavailable" in caplog.text


@pytest.mark.parametrize(
    ("domain", "state", "control", "action", "value"),
    [
        ("switch", "off", "slider", "set", 50),
        # On and off like a switch, but with no service to toggle it: without the check
        # this would reach for the lock pair and call `binary_sensor.unlock`.
        ("binary_sensor", "on", "toggle", "toggle", None),
    ],
)
async def test_a_registry_id_now_naming_another_domain_is_not_acted_on(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    calls: list[ServiceCall],
    caplog: pytest.LogCaptureFixture,
    domain: str,
    state: str,
    control: str,
    action: str,
    value: Any,
) -> None:
    """A control bound by registry id to an entity that cannot be one is shown greyed."""
    entity = er.async_get(hass).async_get_or_create(domain, "demo", "k", suggested_object_id="k")
    hass.states.async_set(entity.entity_id, state)
    unlocks = async_mock_service(hass, "binary_sensor", "unlock")
    claim_menu(box_on_the_broker)
    entry = menu_entry([row("r_odd", entity.id, control)])
    await async_setup_box(hass, entry)

    assert items_by_id(last_menu(mqtt_mock))["r_odd"]["available"] is False
    fire_action(hass, "r_odd", action, value)
    await hass.async_block_till_done()

    assert calls == []
    assert unlocks == []
    assert "(unavailable" in caplog.text


@pytest.mark.parametrize("ts", ['"x"', "null", "true", '"1792050000"'])
def test_a_ts_that_is_not_a_number_is_malformed(ts: str) -> None:
    """Refused as what it is, not as an action from 1970."""
    payload = (
        '{"id":"3f9c2e4a1b7d","rev":1,"row":"r_a","action":"toggle","ts":' + ts + "}"
    ).encode()
    with pytest.raises(Refused) as caught:
        parse_action(payload, now=1792050000)
    assert (caught.value.cause, caught.value.detail) == ("malformed", "ts")


def test_a_ts_with_a_fraction_is_a_number() -> None:
    """Epoch seconds may carry a fraction; it is aged like any other number."""
    payload = b'{"id":"3f9c2e4a1b7d","rev":1,"row":"r_a","action":"toggle","ts":1000.25}'

    assert parse_action(payload, now=1060.0).row == "r_a"
    with pytest.raises(Refused) as caught:
        parse_action(payload, now=1120.5)
    assert caught.value.cause == "stale"


def _clock_issue(hass: HomeAssistant, entry: Any) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"menu_clock_behind_{entry.entry_id}")


def _stale_ts() -> int:
    return int(dt_util.utcnow().timestamp()) - 3600


async def test_a_stale_action_raises_a_repair_about_the_receivers_clock(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """Every OK is refused while the clock is behind; the household is told where it looks."""
    fire_action(hass, "r_switch", "toggle", action_id="000000000001", ts=_stale_ts())
    await hass.async_block_till_done()

    issue = _clock_issue(hass, menu_box)
    assert calls == []
    assert issue is not None
    assert issue.translation_key == "menu_clock_behind"
    assert issue.is_fixable is False
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {"name": menu_box.runtime_data.name}


async def test_the_clock_repair_is_raised_once_and_kept_while_ignored(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """The next stale actions neither raise it again nor undo "Ignore"."""
    issue_id = f"menu_clock_behind_{menu_box.entry_id}"
    fire_action(hass, "r_switch", "toggle", action_id="000000000001", ts=_stale_ts())
    await hass.async_block_till_done()
    ir.async_get(hass).async_ignore(DOMAIN, issue_id, True)
    with patch(
        "custom_components.enigma2_mqtt.menu.ir.async_create_issue"
    ) as create:
        for n in range(2, 5):
            fire_action(hass, "r_switch", "toggle", action_id=f"00000000000{n}", ts=_stale_ts())
        await hass.async_block_till_done()

    assert create.call_count == 0
    issue = _clock_issue(hass, menu_box)
    assert issue is not None and issue.dismissed_version is not None


async def test_the_next_accepted_action_deletes_the_clock_repair(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    fire_action(hass, "r_switch", "toggle", action_id="000000000001", ts=_stale_ts())
    await hass.async_block_till_done()
    assert _clock_issue(hass, menu_box) is not None

    fire_action(hass, "r_switch", "toggle", action_id="000000000002")
    await hass.async_block_till_done()

    assert _clock_issue(hass, menu_box) is None
    assert len(calls) == 1
    # Raised again by the next stale one: the clock went wrong again.
    fire_action(hass, "r_switch", "toggle", action_id="000000000003", ts=_stale_ts())
    await hass.async_block_till_done()
    assert _clock_issue(hass, menu_box) is not None


async def test_an_action_without_a_time_says_nothing_about_the_clock(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    """Performed, and the repair stays: nothing in it shows the clock is right."""
    fire_action(hass, "r_switch", "toggle", action_id="000000000001", ts=_stale_ts())
    await hass.async_block_till_done()
    async_fire_mqtt_message(
        hass,
        MENU_ACTION_TOPIC,
        b'{"id":"000000000002","rev":7,"row":"r_switch","action":"toggle"}',
    )
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert _clock_issue(hass, menu_box) is not None


async def test_a_clock_ahead_raises_no_repair(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """Refused as from the future, with a log line; the repair is about a clock behind."""
    fire_action(
        hass, "r_switch", "toggle", ts=int(dt_util.utcnow().timestamp()) + 3600
    )
    await hass.async_block_till_done()

    assert calls == []
    assert "(future" in caplog.text
    assert _clock_issue(hass, menu_box) is None


async def test_unloading_keeps_and_removing_deletes_the_clock_repair(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall]
) -> None:
    fire_action(hass, "r_switch", "toggle", ts=_stale_ts())
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(menu_box.entry_id)
    await hass.async_block_till_done()
    assert _clock_issue(hass, menu_box) is not None

    assert await hass.config_entries.async_remove(menu_box.entry_id)
    await hass.async_block_till_done()
    assert _clock_issue(hass, menu_box) is None


async def test_five_hundred_rapid_toggles_call_a_bounded_number_of_services(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """A remote held down, or anyone on the broker, is not a stream of service calls."""
    rows = ["r_switch", "r_bool", "r_fan", "r_lamp"]
    with patch("custom_components.enigma2_mqtt.menu._monotonic", return_value=1000.0):
        for n in range(500):
            fire_action(hass, rows[n % 4], "toggle", action_id=f"{n:012x}")
        await hass.async_block_till_done(wait_background_tasks=True)

    # Inside one second: five, whatever the rows; the other 495 are refused.
    assert len(calls) == 5
    assert "(flood" in caplog.text


async def test_a_row_takes_one_action_at_a_time(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """Refused while its last one is still being performed; another row is not held up."""
    release = asyncio.Event()
    toggled: list[ServiceCall] = []

    async def _slow(call: ServiceCall) -> None:
        toggled.append(call)
        await release.wait()

    hass.services.async_register("switch", "toggle", _slow)
    fire_action(hass, "r_switch", "toggle", action_id="000000000001")
    await hass.async_block_till_done()
    assert len(toggled) == 1

    fire_action(hass, "r_switch", "toggle", action_id="000000000002")
    fire_action(hass, "r_bool", "toggle", action_id="000000000003")
    await hass.async_block_till_done()
    assert len(toggled) == 1
    assert "(busy: row r_switch)" in caplog.text
    assert [call.domain for call in calls] == ["input_boolean"]

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    fire_action(hass, "r_switch", "toggle", action_id="000000000004")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(toggled) == 2


async def test_at_most_five_actions_a_second_and_the_window_recovers(
    hass: HomeAssistant, menu_box: Any, calls: list[ServiceCall], caplog
) -> None:
    """Counted over the actions taken on; a second later there is room again."""
    clock = [1000.0]
    with patch(
        "custom_components.enigma2_mqtt.menu._monotonic", side_effect=lambda: clock[0]
    ):
        for n in range(8):
            fire_action(hass, "r_switch", "toggle", action_id=f"{n:012x}")
            await hass.async_block_till_done(wait_background_tasks=True)
            clock[0] += 0.1
        assert len(calls) == 5
        assert "(flood" in caplog.text

        # 1.0 s after the first: it has left the window, and one more is taken.
        clock[0] = 1001.0
        fire_action(hass, "r_switch", "toggle", action_id="0000000000a0")
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(calls) == 6
        fire_action(hass, "r_switch", "toggle", action_id="0000000000a1")
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(calls) == 6

        clock[0] = 1003.0
        for n in range(5):
            fire_action(hass, "r_switch", "toggle", action_id=f"0000000000b{n}")
            await hass.async_block_till_done(wait_background_tasks=True)
        assert len(calls) == 11

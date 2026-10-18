"""A list of choices on the television: the action, its answer, the event and the triggers.

`enigma2_mqtt.show_list` publishes `cmd/list` in an envelope whose id is the list's, and the
receiver answers once on `list_answer`: which row was picked, or how the list ended
otherwise. Held here:

- **the request** - every rule of the contract, checked before anything is published, with
  a translated error that names the field and the row;
- **who is sent one** - a receiver that claims `list` and opens the envelope, is on the
  broker, and whose subscription the broker has confirmed;
- **the wait** - every ending through the response, `no_answer` fifteen seconds after the
  list's timeout, a refusal matched by the list's id and nothing else, a list replaced by
  the next one, and the unload;
- **the answer as untrusted input** - malformed, retained, duplicate and foreign answers
  are dropped with one log line and fire nothing;
- **the event and the device triggers** - for every readable answer, also to a list nobody
  here sent.

Every message is fired after the entry is set up, the order a broker produces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import json
import logging
import re
from typing import Any
from unittest.mock import patch

from freezegun import freeze_time
from homeassistant.components import automation, mqtt
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
    async_get_device_automation_capabilities,
    async_get_device_automations,
    async_mock_service,
)
import voluptuous as vol

from custom_components.enigma2_mqtt import choice_list
from custom_components.enigma2_mqtt.box import Enigma2Box, Enigma2CommandRefused
from custom_components.enigma2_mqtt.choice_list import (
    ListAnswer,
    ListRequest,
    ListRow,
    build_list,
    parse_answer,
)
from custom_components.enigma2_mqtt.const import (
    CAPABILITY_COMMAND_ID,
    CAPABILITY_LIST,
    COMMAND_MAX_BYTES,
    CONF_BASE_TOPIC,
    CONF_NAME,
    CONF_NODE_ID,
    DOMAIN,
    EVENT_LIST_ANSWER,
    LIST_ANSWER_GRACE,
)
from custom_components.enigma2_mqtt.device_trigger import (
    LIST_TRIGGER_TYPES,
    TRIGGER_TYPES,
    async_validate_trigger_config,
)

from .conftest import (
    AVAILABILITY_TOPIC,
    BASE_TOPIC,
    CAPABILITIES,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    async_setup_box_then_retained,
    command_topic,
    opened_command,
)

PLAYER = "media_player.dekoder_salon"
LIST_TOPIC = command_topic("list")
ANSWER_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/list_answer"
AN_ID = re.compile(r"[0-9a-f]{16}")
SOMEBODY_ELSES = "7f9c2e4a-3b1d-4c8e-9f60-2d5a1b7c8e90"

CLAIMS = {**INFO, "capabilities": [*CAPABILITIES, CAPABILITY_COMMAND_ID, CAPABILITY_LIST]}

DOORBELL: dict[str, Any] = {
    "title": "Doorbell",
    "text": "Someone is at the gate.",
    "timeout": 60,
    "default": "ignore",
    "rows": [
        {"id": "open", "label": "Open the gate", "key": "green", "icon": "gate"},
        {"id": "ignore", "label": "Ignore", "key": "red"},
    ],
}


def _rows(count: int) -> list[dict[str, Any]]:
    return [{"id": f"r{number}", "label": f"Row {number}"} for number in range(count)]


# ------------------------------------------------------------------ the request --


def _refused(data: dict[str, Any]) -> ServiceValidationError:
    with pytest.raises(ServiceValidationError) as raised:
        build_list(data)
    return raised.value


def test_the_doorbell_is_published_as_the_contract_writes_it() -> None:
    request = build_list(DOORBELL)

    assert json.loads(request.payload()) == {
        "title": "Doorbell",
        "text": "Someone is at the gate.",
        "rows": [
            {"id": "open", "label": "Open the gate", "key": "green", "icon": "gate"},
            {"id": "ignore", "label": "Ignore", "key": "red"},
        ],
        "timeout": 60,
        "default": "ignore",
    }
    # No spaces: every byte counts towards the receiver's 4096.
    assert ", " not in request.payload() and ": " not in request.payload()


def test_a_list_without_a_timeout_has_sixty_seconds_written_out() -> None:
    """There is no list without a timeout, and the one waited for is the one sent."""
    request = build_list({"title": "T", "rows": _rows(1)})

    assert request.timeout == 60
    assert json.loads(request.payload()) == {
        "title": "T",
        "rows": [{"id": "r0", "label": "Row 0"}],
        "timeout": 60,
    }


@pytest.mark.parametrize("title", [None, "", "   ", "\\\\", " \\ \\ "])
def test_a_list_needs_a_title_that_is_more_than_backslashes_and_blanks(title: Any) -> None:
    data = {"rows": _rows(1)} if title is None else {"title": title, "rows": _rows(1)}

    assert _refused(data).translation_key == "list_title_blank"


def test_the_title_loses_its_backslashes_before_it_is_cut_at_60() -> None:
    request = build_list({"title": "\\" * 30 + "x" * 70, "rows": _rows(1)})

    assert request.title == "x" * 60


def test_a_title_cut_to_blanks_is_refused() -> None:
    """Cut first, then judged: sixty spaces in front of the words leave nothing to show."""
    assert _refused({"title": " " * 60 + "words", "rows": _rows(1)}).translation_key == (
        "list_title_blank"
    )


@pytest.mark.parametrize(("title", "expected"), [(2026, "2026"), (1.5, "1.5")])
def test_a_number_is_a_text(title: Any, expected: str) -> None:
    """A template renders `{{ 2026 }}` as a number; it still means the text."""
    assert build_list({"title": title, "rows": _rows(1)}).title == expected


@pytest.mark.parametrize("title", [True, ["Doorbell"], {"text": "Doorbell"}])
def test_a_title_that_is_not_a_text_is_refused(title: Any) -> None:
    refused = _refused({"title": title, "rows": _rows(1)})

    assert refused.translation_key == "list_member_not_text"
    assert refused.translation_placeholders == {"member": "title"}


@pytest.mark.parametrize("text", [None, "", "  ", "\\\\"])
def test_no_text_is_no_text(text: Any) -> None:
    request = build_list({"title": "T", "text": text, "rows": _rows(1)})

    assert request.text is None
    assert "text" not in json.loads(request.payload())


def test_the_text_loses_its_backslashes_and_is_cut_at_200() -> None:
    request = build_list({"title": "T", "text": "a\\nb" + "y" * 300, "rows": _rows(1)})

    assert request.text == ("anb" + "y" * 300)[:200]


def test_a_text_that_is_not_a_text_is_refused() -> None:
    refused = _refused({"title": "T", "text": ["x"], "rows": _rows(1)})

    assert refused.translation_placeholders == {"member": "text"}


@pytest.mark.parametrize("rows", [None, "open", {"id": "open", "label": "Open"}, 3])
def test_the_rows_are_a_list(rows: Any) -> None:
    data = {"title": "T"} if rows is None else {"title": "T", "rows": rows}

    assert _refused(data).translation_key == "list_rows_not_list"


@pytest.mark.parametrize("count", [0, 25])
def test_a_list_has_1_to_24_rows(count: int) -> None:
    refused = _refused({"title": "T", "rows": _rows(count)})

    assert refused.translation_key == "list_rows_count"
    assert refused.translation_placeholders == {"count": str(count), "min": "1", "max": "24"}


@pytest.mark.parametrize("count", [1, 24])
def test_one_row_and_twenty_four_rows_are_lists(count: int) -> None:
    assert len(build_list({"title": "T", "rows": _rows(count)}).rows) == count


def test_a_row_is_a_mapping_and_its_number_counts_from_one() -> None:
    refused = _refused({"title": "T", "rows": [*_rows(2), "open"]})

    assert refused.translation_key == "list_row_not_mapping"
    assert refused.translation_placeholders == {"row": "3"}


def test_a_row_member_nobody_knows_is_refused_by_name() -> None:
    """A misspelt `key` would otherwise vanish and leave the row without its shortcut."""
    refused = _refused({"title": "T", "rows": [{"id": "a", "label": "A", "kee": "red"}]})

    assert refused.translation_key == "list_row_member_unknown"
    assert refused.translation_placeholders == {"row": "1", "member": "kee"}


@pytest.mark.parametrize(
    "row_id",
    [
        None, "", "x" * 33, "open gate", "open/gate", "za\u017c\u00f3\u0142\u0107", "a\nb",
        True, ["a"],
    ],
)
def test_a_row_id_is_1_to_32_characters_of_the_contract_s_set(row_id: Any) -> None:
    row: dict[str, Any] = {"label": "A"}
    if row_id is not None:
        row["id"] = row_id
    refused = _refused({"title": "T", "rows": [row]})

    assert refused.translation_key == "list_row_id_invalid"
    assert refused.translation_placeholders == {"row": "1", "max": "32"}


@pytest.mark.parametrize("row_id", ["x" * 32, "a", "A-z.0_9:-", 7])
def test_these_are_row_ids(row_id: Any) -> None:
    request = build_list({"title": "T", "rows": [{"id": row_id, "label": "A"}]})

    assert request.rows[0].row_id == str(row_id)


def test_two_rows_with_one_id_are_refused_and_case_matters() -> None:
    assert build_list(
        {"title": "T", "rows": [{"id": "Open", "label": "A"}, {"id": "open", "label": "B"}]}
    )
    refused = _refused(
        {"title": "T", "rows": [{"id": "open", "label": "A"}, {"id": "open", "label": "B"}]}
    )

    assert refused.translation_key == "list_row_id_duplicate"
    assert refused.translation_placeholders == {"row": "2", "id": "open"}


@pytest.mark.parametrize("label", [None, "", "   ", "\\\\\\"])
def test_a_row_needs_a_label(label: Any) -> None:
    row: dict[str, Any] = {"id": "a"}
    if label is not None:
        row["label"] = label

    assert _refused({"title": "T", "rows": [row]}).translation_key == "list_row_label_blank"


def test_a_label_loses_its_backslashes_and_is_cut_at_60() -> None:
    request = build_list(
        {"title": "T", "rows": [{"id": "a", "label": "\\c" * 5 + "z" * 80}]}
    )

    assert request.rows[0].label == ("c" * 5 + "z" * 80)[:60]


def test_a_label_that_is_not_a_text_is_refused() -> None:
    refused = _refused({"title": "T", "rows": [{"id": "a", "label": {"pl": "A"}}]})

    assert refused.translation_key == "list_row_member_not_text"
    assert refused.translation_placeholders == {"row": "1", "member": "label"}


@pytest.mark.parametrize(
    "key",
    ["red", "green", "yellow", "blue", "1", "9", 1, 9],
)
def test_these_are_keys(key: Any) -> None:
    request = build_list({"title": "T", "rows": [{"id": "a", "label": "A", "key": key}]})

    assert request.rows[0].key == str(key)


@pytest.mark.parametrize("key", ["RED", "0", "10", "", "ok", 0, 1.5, True, ["red"]])
def test_anything_else_is_not_a_key(key: Any) -> None:
    refused = _refused({"title": "T", "rows": [{"id": "a", "label": "A", "key": key}]})

    assert refused.translation_key == "list_row_key_invalid"
    assert refused.translation_placeholders == {"row": "1"}


def test_two_rows_with_one_key_are_refused() -> None:
    refused = _refused(
        {
            "title": "T",
            "rows": [
                {"id": "a", "label": "A", "key": "1"},
                {"id": "b", "label": "B"},
                {"id": "c", "label": "C", "key": 1},
            ],
        }
    )

    assert refused.translation_key == "list_row_key_duplicate"
    assert refused.translation_placeholders == {"row": "3", "key": "1"}


def test_an_icon_the_plugin_may_not_know_is_sent_and_an_empty_one_is_none() -> None:
    """A newer plugin can know more icons; an unknown name is drawn as the neutral one."""
    request = build_list(
        {
            "title": "T",
            "rows": [
                {"id": "a", "label": "A", "icon": "spaceship"},
                {"id": "b", "label": "B", "icon": ""},
                {"id": "c", "label": "C", "icon": None, "key": None},
            ],
        }
    )

    assert [row.wire() for row in request.rows] == [
        {"id": "a", "label": "A", "icon": "spaceship"},
        {"id": "b", "label": "B"},
        {"id": "c", "label": "C"},
    ]


def test_an_icon_that_is_not_a_text_is_refused() -> None:
    refused = _refused({"title": "T", "rows": [{"id": "a", "label": "A", "icon": 3}]})

    assert refused.translation_placeholders == {"row": "1", "member": "icon"}


@pytest.mark.parametrize(("timeout", "expected"), [(5, 5), (600, 600), (60.0, 60), (None, 60)])
def test_these_are_timeouts(timeout: Any, expected: int) -> None:
    assert build_list({"title": "T", "rows": _rows(1), "timeout": timeout}).timeout == expected


@pytest.mark.parametrize("timeout", [4, 601, 0, -5, 60.5, True, False, "60", [60]])
def test_a_timeout_is_whole_seconds_from_5_to_600(timeout: Any) -> None:
    refused = _refused({"title": "T", "rows": _rows(1), "timeout": timeout})

    assert refused.translation_key == "list_timeout_invalid"
    assert refused.translation_placeholders == {"min": "5", "max": "600"}


def test_the_default_is_one_of_the_rows() -> None:
    assert build_list({**DOORBELL, "default": "open"}).default == "open"
    assert build_list({**DOORBELL, "default": None}).default is None

    for default in ("Open", "gate", True, ["open"]):
        refused = _refused({**DOORBELL, "default": default})
        assert refused.translation_key == "list_default_unknown"
        # The caller's text is not repeated back.
        assert refused.translation_placeholders is None


def test_the_label_of_a_row() -> None:
    request = ListRequest(title="T", rows=(ListRow("a", "Alpha"), ListRow("b", "Beta")))

    assert request.label_of("b") == "Beta"
    assert request.label_of("c") is None
    assert request.label_of(None) is None


# ------------------------------------------------------------------- the answer --


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"id": "a1", "ending": "picked", "row": "open", "ts": 1},
            ListAnswer("a1", "picked", "open"),
        ),
        ({"id": "a1", "ending": "timeout", "row": "ignore"}, ListAnswer("a1", "timeout", "ignore")),
        ({"id": "a1", "ending": "timeout", "row": None}, ListAnswer("a1", "timeout", None)),
        ({"id": "a1", "ending": "timeout"}, ListAnswer("a1", "timeout", None)),
        ({"id": "a1", "ending": "dismissed", "row": None}, ListAnswer("a1", "dismissed", None)),
        ({"ending": "replaced", "row": None}, ListAnswer(None, "replaced", None)),
        ({"ending": "closed", "later": {"x": 1}}, ListAnswer(None, "closed", None)),
        ({"id": "x" * 64, "ending": "closed"}, ListAnswer("x" * 64, "closed", None)),
        ({"id": SOMEBODY_ELSES, "ending": "closed"}, ListAnswer(SOMEBODY_ELSES, "closed", None)),
    ],
)
def test_an_answer_of_the_contract_s_shape_is_read(
    payload: dict[str, Any], expected: ListAnswer
) -> None:
    assert parse_answer(json.dumps(payload).encode()) == expected


MALFORMED: list[tuple[Any, str]] = [
    (b"", "unreadable"),
    (b"\xff\xfe", "unreadable"),
    (b"not json", "unreadable"),
    (b"[1]", "unreadable"),
    (b"[" * 5000, "unreadable"),
    (json.dumps({"ending": "picked", "row": "a", "pad": "x" * 2000}).encode(), "unreadable"),
    (json.dumps({"id": None, "ending": "closed"}).encode(), "malformed"),
    (json.dumps({"id": "", "ending": "closed"}).encode(), "malformed"),
    (json.dumps({"id": "x" * 65, "ending": "closed"}).encode(), "malformed"),
    (json.dumps({"id": "a b", "ending": "closed"}).encode(), "malformed"),
    (json.dumps({"id": 17, "ending": "closed"}).encode(), "malformed"),
    (json.dumps({"id": "a1"}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "no_answer"}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "PICKED", "row": "a"}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": ["picked"], "row": "a"}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "picked"}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "picked", "row": None}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "picked", "row": "x" * 33}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "picked", "row": 3}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "timeout", "row": "a/b"}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "dismissed", "row": "open"}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "replaced", "row": "open"}).encode(), "malformed"),
    (json.dumps({"id": "a1", "ending": "closed", "row": ""}).encode(), "malformed"),
]


@pytest.mark.parametrize(("payload", "cause"), MALFORMED)
def test_anything_else_is_dropped_with_its_cause(payload: Any, cause: str) -> None:
    with pytest.raises(choice_list.Dropped) as dropped:
        parse_answer(payload)

    assert dropped.value.cause == cause


# --------------------------------------------------------------- the receiver --


class Receiver:
    """A receiver that hears every `cmd/list` and answers it the way it is told to."""

    def __init__(
        self, hass: HomeAssistant, answer: Callable[[str | None, dict], None] | None = None
    ) -> None:
        self.hass = hass
        self.answer = answer
        # Every list heard: what was published, its id and its own payload, read.
        self.heard: list[tuple[Any, str | None, dict[str, Any]]] = []

    async def arm(self) -> Receiver:
        await mqtt.async_subscribe(self.hass, LIST_TOPIC, self._received)
        return self

    @callback
    def _received(self, msg: ReceiveMessage) -> None:
        list_id, inner = opened_command(msg.payload)
        self.heard.append((msg.payload, list_id, json.loads(inner)))
        if self.answer is not None:
            self.answer(list_id, json.loads(inner))

    def ids(self) -> list[str | None]:
        return [list_id for _, list_id, _ in self.heard]


def _answer(hass: HomeAssistant, payload: dict[str, Any] | str, *, retain: bool = False) -> None:
    async_fire_mqtt_message(
        hass,
        ANSWER_TOPIC,
        payload if isinstance(payload, str) else json.dumps(payload),
        retain=retain,
    )


def _refuse(hass: HomeAssistant, list_id: str | None, error: str, **more: Any) -> None:
    payload: dict[str, Any] = {"cmd": "list", "error": error, "ts": 1789459213, **more}
    if list_id is not None:
        payload["id"] = list_id
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(payload))


async def _box(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    retained: dict[str, str | bytes],
    info: dict[str, Any] | None = CLAIMS,
) -> Enigma2Box:
    """The example box, set up before its retained topics arrive, past the debounce."""
    retained[INFO_TOPIC] = json.dumps(info)
    await async_setup_box_then_retained(hass, entry, retained)
    box: Enigma2Box = entry.runtime_data
    await box.async_wait_subscribed()
    return box


def _show(
    hass: HomeAssistant, data: dict[str, Any] | None = None, *, response: bool = True
) -> asyncio.Task[Any]:
    """The action, running - not tracked by Home Assistant, so settling never waits for it."""
    return asyncio.get_running_loop().create_task(
        hass.services.async_call(
            DOMAIN,
            "show_list",
            {"entity_id": PLAYER, **(DOORBELL if data is None else data)},
            blocking=True,
            return_response=response,
        )
    )


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _heard(receiver: Receiver, count: int = 1) -> str:
    for _ in range(200):
        if len(receiver.heard) >= count:
            list_id = receiver.ids()[count - 1]
            assert list_id is not None
            return list_id
        await asyncio.sleep(0)
    raise AssertionError("cmd/list was never published")


def _events(hass: HomeAssistant) -> list[dict[str, Any]]:
    fired: list[dict[str, Any]] = []

    @callback
    def _fired(event: Event) -> None:
        fired.append(dict(event.data))

    hass.bus.async_listen(EVENT_LIST_ANSWER, _fired)
    return fired


def _device_id(hass: HomeAssistant) -> str:
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_ID), entry.entry_id
    )
    assert device is not None
    return device.id


# ------------------------------------------------------------- who is sent one --


@pytest.mark.parametrize(
    "capabilities",
    [
        [*CAPABILITIES, CAPABILITY_COMMAND_ID],
        # `list` implies the envelope; a plugin claiming one without the other is not sent
        # a list nobody could match the answer of.
        [*CAPABILITIES, CAPABILITY_LIST],
    ],
    ids=["no list", "no envelope"],
)
async def test_a_receiver_that_cannot_show_one_is_refused_before_anything_is_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    capabilities: list[str],
) -> None:
    await _box(hass, config_entry, box_on_the_broker, {**INFO, "capabilities": capabilities})
    receiver = await Receiver(hass).arm()

    with pytest.raises(ServiceValidationError) as raised:
        await _show(hass)

    assert raised.value.translation_key == "list_unsupported"
    assert receiver.heard == []


async def test_the_capability_is_asked_before_the_fields_are_read(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Saying the third row is wrong to somebody whose receiver shows no list helps nobody."""
    await _box(hass, config_entry, box_on_the_broker, INFO)

    with pytest.raises(ServiceValidationError) as raised:
        await _show(hass, {"title": ""})

    assert raised.value.translation_key == "list_unsupported"


async def test_a_receiver_off_the_broker_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as raised:
        await _show(hass)

    assert raised.value.translation_key == "list_offline"
    assert receiver.heard == []


async def test_an_invalid_list_is_refused_and_nothing_is_published(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()

    with pytest.raises(ServiceValidationError) as raised:
        await _show(hass, {**DOORBELL, "rows": [*DOORBELL["rows"], {"id": "open", "label": "x"}]})

    assert raised.value.translation_key == "list_row_id_duplicate"
    assert raised.value.translation_placeholders == {"row": "3", "id": "open"}
    assert receiver.heard == []


async def test_a_list_over_the_receiver_s_limit_in_its_envelope_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """24 rows of 60 three-byte characters do not fit, and sending them without the id that
    makes the answer readable is no way out."""
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    rows = [{"id": f"row{n}", "label": "\u20ac" * 60} for n in range(24)]

    with pytest.raises(ServiceValidationError) as raised:
        await _show(hass, {"title": "T", "rows": rows})

    assert raised.value.translation_key == "list_too_long"
    assert int(raised.value.translation_placeholders["size"]) > COMMAND_MAX_BYTES
    assert receiver.heard == []


async def test_twenty_four_rows_of_sixty_characters_fit(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The contract's own sum: the largest plain list goes out, in its envelope."""
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    rows = [{"id": f"row{n:02}", "label": "x" * 60, "key": None} for n in range(24)]

    assert await _show(hass, {"title": "t" * 60, "rows": rows}, response=False) is None

    (wire, list_id, payload) = receiver.heard[0]
    assert len(wire.encode() if isinstance(wire, str) else wire) <= COMMAND_MAX_BYTES
    assert list_id is not None and len(payload["rows"]) == 24


# ------------------------------------------------------------------ what goes out --


async def test_a_list_goes_out_in_an_envelope_with_a_fresh_id_at_qos_1_not_retained(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()

    await _show(hass, response=False)
    await _show(hass, response=False)

    first, second = receiver.ids()
    assert AN_ID.fullmatch(first) and AN_ID.fullmatch(second) and first != second
    wire, _, payload = receiver.heard[0]
    envelope = json.loads(wire)
    assert list(envelope) == ["id", "payload"]
    # A string, as every command's payload: the plugin reads exactly what it would bare.
    assert envelope["payload"] == build_list(DOORBELL).payload()
    assert payload["rows"][0]["id"] == "open"
    mqtt_mock.async_publish.assert_any_call(
        LIST_TOPIC, wire, 1, False, message_expiry_interval=None
    )


async def test_without_a_response_the_action_returns_once_the_list_is_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    fired = _events(hass)

    assert await _show(hass, response=False) is None
    assert box.lists is not None and box.lists.waiting == 1

    # The answer still comes, and still carries the row's label.
    _answer(hass, {"id": receiver.ids()[0], "ending": "picked", "row": "open", "ts": 1})
    await hass.async_block_till_done()

    assert fired[0]["label"] == "Open the gate"
    assert box.lists.waiting == 0


async def test_the_list_waits_for_the_broker_to_confirm_the_subscription(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Home Assistant debounces its SUBSCRIBE, and a quick viewer answers in a fraction of a
    second: the list goes out only once the broker sends this box's topics to us."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    confirmed = asyncio.Event()

    with patch.object(box, "async_wait_subscribed", confirmed.wait):
        task = _show(hass)
        await _settle()
        assert receiver.heard == []
        assert not task.done()

        confirmed.set()
        list_id = await _heard(receiver)
        _answer(hass, {"id": list_id, "ending": "picked", "row": "ignore"})

        response = await task

    assert response[PLAYER] == {"ending": "picked", "row": "ignore", "label": "Ignore"}


async def test_a_subscription_the_broker_never_confirmed_sends_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The wait for the confirmation runs out after five seconds; a list sent then could be
    answered into nothing, so it is not sent."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    box._subscribed.clear()

    async def _ran_out(*_args: Any, **_kwargs: Any) -> None:
        return None

    with (
        patch.object(box, "async_wait_subscribed", _ran_out),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _show(hass)

    assert raised.value.translation_key == "list_not_subscribed"
    assert not isinstance(raised.value, ServiceValidationError)
    assert receiver.heard == []
    assert box.lists is not None and box.lists.waiting == 0


# ------------------------------------------------------------------- the endings --


@pytest.mark.parametrize(
    ("answer", "data", "expected"),
    [
        (
            {"ending": "picked", "row": "open"},
            DOORBELL,
            {"ending": "picked", "row": "open", "label": "Open the gate"},
        ),
        (
            {"ending": "timeout", "row": "ignore"},
            DOORBELL,
            {"ending": "timeout", "row": "ignore", "label": "Ignore"},
        ),
        (
            {"ending": "timeout", "row": None},
            {**DOORBELL, "default": None},
            {"ending": "timeout", "row": None, "label": None},
        ),
        (
            {"ending": "dismissed", "row": None},
            DOORBELL,
            {"ending": "dismissed", "row": None, "label": None},
        ),
        (
            {"ending": "replaced", "row": None},
            DOORBELL,
            {"ending": "replaced", "row": None, "label": None},
        ),
        (
            {"ending": "closed", "row": None},
            DOORBELL,
            {"ending": "closed", "row": None, "label": None},
        ),
    ],
    ids=["picked", "timeout with a default", "timeout without", "dismissed", "replaced", "closed"],
)
async def test_every_ending_is_the_response(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    answer: dict[str, Any],
    data: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(
        hass,
        lambda list_id, _payload: _answer(hass, {"id": list_id, **answer, "ts": 1792050000}),
    ).arm()

    response = await _show(hass, data)

    assert response == {PLAYER: expected}
    assert len(receiver.heard) == 1


async def test_no_answer_ends_the_wait_fifteen_seconds_after_the_list_s_timeout(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A choice made while the broker was away is never queued: the wait says so.

    The clock is frozen from the moment the list is sent - the wall clock and the event
    loop's alike - so nothing ends the wait but the time this test moves it by.
    """
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    fired = _events(hass)

    with freeze_time(dt_util.utcnow()) as frozen:
        task = _show(hass, {**DOORBELL, "timeout": 5})
        await _heard(receiver)

        frozen.tick(timedelta(seconds=5 + LIST_ANSWER_GRACE - 1))
        async_fire_time_changed(hass)
        await _settle()
        assert not task.done()

        frozen.tick(timedelta(seconds=1))
        async_fire_time_changed(hass)

        assert await task == {PLAYER: {"ending": "no_answer", "row": None, "label": None}}
    assert box.lists is not None and box.lists.waiting == 0
    # An answer that comes after all is somebody's list, as far as this side knows.
    _answer(hass, {"id": receiver.ids()[0], "ending": "picked", "row": "open"})
    await hass.async_block_till_done()
    assert fired[0]["label"] is None and fired[0]["ending"] == "picked"


async def test_a_list_sent_without_a_response_is_forgotten_after_its_wait_too(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box = await _box(hass, config_entry, box_on_the_broker)
    await Receiver(hass).arm()

    with freeze_time(dt_util.utcnow()) as frozen:
        await _show(hass, {**DOORBELL, "timeout": 600}, response=False)
        assert box.lists is not None and box.lists.waiting == 1

        frozen.tick(timedelta(seconds=600 + LIST_ANSWER_GRACE - 1))
        async_fire_time_changed(hass)
        await _settle()
        assert box.lists.waiting == 1

        frozen.tick(timedelta(seconds=1))
        async_fire_time_changed(hass)
        await _settle()

        assert box.lists.waiting == 0


# ---------------------------------------------------------------- the refusals --


@pytest.mark.parametrize(
    ("reason", "refusal"),
    [("standby", True), ("busy", True), ("invalid", True), (None, False), ("later", False)],
)
async def test_a_refusal_that_says_the_list_s_id_ends_the_wait_with_the_receiver_s_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reason: str | None,
    refusal: bool,
) -> None:
    box = await _box(hass, config_entry, box_on_the_broker)
    more = {} if reason is None else {"reason": reason}
    await Receiver(
        hass,
        lambda list_id, _payload: _refuse(hass, list_id, "the receiver is in standby", **more),
    ).arm()
    fired = _events(hass)

    with pytest.raises(HomeAssistantError) as raised:
        await _show(hass)

    assert raised.value.translation_key == "list_refused"
    assert raised.value.translation_placeholders == {"reason": "the receiver is in standby"}
    # A code that says the moment was wrong is a refusal, like every other command's.
    assert isinstance(raised.value, Enigma2CommandRefused) is refusal
    assert isinstance(raised.value, ServiceValidationError) is refusal
    assert fired == []
    assert box.lists is not None and box.lists.waiting == 0


@pytest.mark.parametrize(
    "refusal",
    [
        {"id": SOMEBODY_ELSES, "cmd": "list", "error": "another client's list"},
        {"cmd": "list", "error": "a list sent without an id"},
        {"cmd": "zap", "error": "unrelated"},
    ],
    ids=["another id", "no id", "another command"],
)
async def test_a_refusal_about_anything_else_leaves_the_wait_alone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    refusal: dict[str, Any],
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    task = _show(hass)
    list_id = await _heard(receiver)

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps({**refusal, "ts": 1}))
    await _settle()
    assert not task.done()

    _answer(hass, {"id": list_id, "ending": "dismissed", "row": None})
    assert (await task)[PLAYER]["ending"] == "dismissed"


async def test_a_retained_refusal_with_the_id_is_no_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A retained complaint is what the broker held before; it cannot be about this list."""
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    task = _show(hass)
    list_id = await _heard(receiver)

    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"id": list_id, "cmd": "list", "error": "x", "reason": "standby"}),
        retain=True,
    )
    await _settle()
    assert not task.done()

    _answer(hass, {"id": list_id, "ending": "closed"})
    assert (await task)[PLAYER]["ending"] == "closed"


# ----------------------------------------------------------- one list, the next --


async def test_a_list_replaced_by_the_next_one_from_here_ends_replaced(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The receiver answers the open list `replaced` before it shows the new one, so the
    first caller is not left waiting; the second gets its own answer."""
    await _box(hass, config_entry, box_on_the_broker)
    open_list: list[str] = []

    def _receiver(list_id: str | None, _payload: dict) -> None:
        if open_list:
            _answer(hass, {"id": open_list.pop(), "ending": "replaced", "row": None})
        assert list_id is not None
        open_list.append(list_id)

    receiver = await Receiver(hass, _receiver).arm()

    first = _show(hass)
    await _heard(receiver)
    bedtime = {
        "title": "TV off in 1 minute",
        "rows": [{"id": "later", "label": "+15 minutes"}, {"id": "off", "label": "Cancel"}],
    }
    second = _show(hass, bedtime)
    second_id = await _heard(receiver, 2)

    assert await first == {PLAYER: {"ending": "replaced", "row": None, "label": None}}
    assert not second.done()
    _answer(hass, {"id": second_id, "ending": "picked", "row": "later"})
    assert await second == {PLAYER: {"ending": "picked", "row": "later", "label": "+15 minutes"}}


async def test_unloading_the_receiver_ends_every_wait(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    task = _show(hass)
    await _heard(receiver)

    assert await hass.config_entries.async_unload(config_entry.entry_id)

    with pytest.raises(HomeAssistantError) as raised:
        await task
    assert raised.value.translation_key == "list_unloaded"
    assert box.lists is not None and box.lists.waiting == 0


async def test_a_caller_that_stops_waiting_leaves_the_label_to_the_event(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An automation stopped mid-wait: the viewer's choice still fires the triggers."""
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    fired = _events(hass)
    task = _show(hass)
    list_id = await _heard(receiver)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _answer(hass, {"id": list_id, "ending": "picked", "row": "open"})
    await hass.async_block_till_done()

    assert fired[0]["label"] == "Open the gate"


# ------------------------------------------------------------ untrusted answers --


async def _waiting_list(
    hass: HomeAssistant, entry: MockConfigEntry, retained: dict[str, str | bytes]
) -> tuple[asyncio.Task[Any], str]:
    await _box(hass, entry, retained)
    receiver = await Receiver(hass).arm()
    task = _show(hass)
    return task, await _heard(receiver)


@pytest.mark.parametrize(("payload", "cause"), MALFORMED)
async def test_a_malformed_answer_fires_nothing_and_ends_no_wait(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    payload: Any,
    cause: str,
) -> None:
    task, list_id = await _waiting_list(hass, config_entry, box_on_the_broker)
    fired = _events(hass)

    async_fire_mqtt_message(hass, ANSWER_TOPIC, payload)
    await _settle()

    assert fired == []
    assert not task.done()
    _answer(hass, {"id": list_id, "ending": "closed"})
    assert (await task)[PLAYER]["ending"] == "closed"


@pytest.mark.parametrize(
    "answer",
    [
        {"ending": "picked", "row": "gate"},
        {"ending": "picked", "row": "Open"},
        {"ending": "timeout", "row": "open"},
        {"ending": "timeout", "row": None},
    ],
    ids=["a row it did not have", "case matters", "another default", "no default"],
)
async def test_an_answer_to_a_list_from_here_must_name_what_that_list_offered(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    answer: dict[str, Any],
) -> None:
    task, list_id = await _waiting_list(hass, config_entry, box_on_the_broker)
    fired = _events(hass)

    _answer(hass, {"id": list_id, **answer})
    await _settle()

    assert fired == []
    assert not task.done()
    _answer(hass, {"id": list_id, "ending": "picked", "row": "open"})
    assert (await task)[PLAYER]["row"] == "open"


async def test_a_retained_answer_is_somebody_s_leftover(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    task, list_id = await _waiting_list(hass, config_entry, box_on_the_broker)
    fired = _events(hass)

    _answer(hass, {"id": list_id, "ending": "picked", "row": "open"}, retain=True)
    await _settle()

    assert fired == []
    assert not task.done()
    _answer(hass, {"id": list_id, "ending": "dismissed"})
    await task


async def test_a_list_is_answered_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """QoS 1 may deliver an answer twice, and anybody may publish a copy: one event."""
    task, list_id = await _waiting_list(hass, config_entry, box_on_the_broker)
    fired = _events(hass)

    _answer(hass, {"id": list_id, "ending": "picked", "row": "open"})
    _answer(hass, {"id": list_id, "ending": "picked", "row": "open"})
    _answer(hass, {"id": list_id, "ending": "dismissed"})
    await hass.async_block_till_done()

    assert (await task)[PLAYER]["row"] == "open"
    assert len(fired) == 1


async def test_a_dropped_answer_is_one_warning_per_cause_then_quiet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log names the cause and the member, never what the sender wrote."""
    await _box(hass, config_entry, box_on_the_broker)
    caplog.set_level(logging.DEBUG, logger=choice_list.__name__)

    for _ in range(3):
        _answer(hass, {"id": "secret-looking-id", "ending": "picked", "row": "a b"})
    _answer(hass, "not json at all, says the sender")
    await hass.async_block_till_done()

    records = [r for r in caplog.records if r.name == choice_list.__name__]
    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "malformed: row" in warnings[0]
    assert all("secret" not in r.getMessage() and "sender" not in r.getMessage() for r in records)
    assert len([r for r in records if r.levelno == logging.DEBUG]) == 2


# ------------------------------------------------------------------- the event --


async def test_every_readable_answer_fires_the_event_also_for_a_list_nobody_here_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    fired = _events(hass)
    device_id = _device_id(hass)

    await _show(hass, response=False)
    ours = receiver.ids()[0]
    _answer(hass, {"id": ours, "ending": "timeout", "row": "ignore", "ts": 1})
    _answer(hass, {"id": SOMEBODY_ELSES, "ending": "picked", "row": "open"})
    _answer(hass, {"ending": "dismissed", "row": None})
    await hass.async_block_till_done()

    assert fired == [
        {
            "device_id": device_id,
            "node_id": NODE_ID,
            "list_id": ours,
            "ending": "timeout",
            "row": "ignore",
            "label": "Ignore",
        },
        {
            "device_id": device_id,
            "node_id": NODE_ID,
            "list_id": SOMEBODY_ELSES,
            "ending": "picked",
            "row": "open",
            "label": None,
        },
        {
            "device_id": device_id,
            "node_id": NODE_ID,
            "list_id": None,
            "ending": "dismissed",
            "row": None,
            "label": None,
        },
    ]


# ------------------------------------------------------------- device triggers --


async def test_the_receiver_offers_three_list_triggers_beside_the_colour_keys(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    device_id = _device_id(hass)

    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, device_id)
    ours = sorted(trigger["type"] for trigger in triggers if trigger["domain"] == DOMAIN)

    assert ours == sorted([*TRIGGER_TYPES, *LIST_TRIGGER_TYPES])
    assert sorted(LIST_TRIGGER_TYPES) == ["list_dismissed", "list_picked", "list_timeout"]


async def test_only_the_picked_trigger_has_a_row_field(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    device_id = _device_id(hass)

    for trigger_type in (*TRIGGER_TYPES, *LIST_TRIGGER_TYPES):
        capabilities = await async_get_device_automation_capabilities(
            hass,
            DeviceAutomationType.TRIGGER,
            {"platform": "device", "domain": DOMAIN, "device_id": device_id, "type": trigger_type},
        )
        fields = [field["name"] for field in capabilities.get("extra_fields", [])]
        assert fields == (["row"] if trigger_type == "list_picked" else []), trigger_type


async def test_a_stored_trigger_is_checked(hass: HomeAssistant) -> None:
    base = {"platform": "device", "domain": DOMAIN, "device_id": "abc"}

    assert (await async_validate_trigger_config(hass, {**base, "type": "list_picked"}))[
        "type"
    ] == "list_picked"
    assert (
        await async_validate_trigger_config(hass, {**base, "type": "list_picked", "row": "open"})
    )["row"] == "open"
    # The editor's field left empty is any row.
    assert "row" not in await async_validate_trigger_config(
        hass, {**base, "type": "list_picked", "row": ""}
    )
    for wrong in (
        {"type": "list_picked", "row": "open gate"},
        {"type": "list_picked", "row": "x" * 33},
        {"type": "list_timeout", "row": "open"},
        {"type": "list_dismissed", "row": "open"},
        {"type": "list_replaced"},
    ):
        with pytest.raises(vol.Invalid):
            await async_validate_trigger_config(hass, {**base, **wrong})


async def _automation(
    hass: HomeAssistant, device_id: str, trigger: dict[str, Any]
) -> list[ServiceCall]:
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "trigger": {
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": device_id,
                    **trigger,
                },
                "action": {
                    "service": "test.automation",
                    "data": {
                        "row": "{{ trigger.event.data.row }}",
                        "label": "{{ trigger.event.data.label }}",
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()
    return calls


ENDINGS: list[dict[str, Any]] = [
    {"ending": "picked", "row": "open"},
    {"ending": "picked", "row": "ignore"},
    {"ending": "timeout", "row": "ignore"},
    {"ending": "timeout", "row": None},
    {"ending": "dismissed", "row": None},
    {"ending": "replaced", "row": None},
    {"ending": "closed", "row": None},
]


@pytest.mark.parametrize(
    ("trigger", "fires_on"),
    [
        ({"type": "list_picked"}, [0, 1]),
        ({"type": "list_picked", "row": "ignore"}, [1]),
        ({"type": "list_timeout"}, [2, 3]),
        ({"type": "list_dismissed"}, [4]),
    ],
    ids=["any row", "one row", "timeout", "dismissed"],
)
async def test_each_list_trigger_fires_on_its_ending_only(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    trigger: dict[str, Any],
    fires_on: list[int],
) -> None:
    """For any list - these were sent by nobody here - and never for a replaced or closed one."""
    await _box(hass, config_entry, box_on_the_broker)
    calls = await _automation(hass, _device_id(hass), trigger)

    for number, answer in enumerate(ENDINGS):
        _answer(hass, {"id": f"other-{number}", **answer})
    await hass.async_block_till_done()

    assert [call.data["row"] for call in calls] == [
        ENDINGS[number]["row"] for number in fires_on
    ]


async def test_a_trigger_is_for_its_own_receiver(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Another receiver's answer - the same event, another device id - starts nothing here."""
    await _box(hass, config_entry, box_on_the_broker)
    device_id = _device_id(hass)
    calls = await _automation(hass, device_id, {"type": "list_picked"})

    hass.bus.async_fire(
        EVENT_LIST_ANSWER,
        {"device_id": "another-device", "ending": "picked", "row": "open", "label": None},
    )
    await hass.async_block_till_done()
    assert calls == []

    _answer(hass, {"ending": "picked", "row": "open"})
    await hass.async_block_till_done()
    assert [call.data["row"] for call in calls] == ["open"]


async def test_a_trigger_on_a_list_from_here_carries_the_label(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(
        hass, lambda list_id, _p: _answer(hass, {"id": list_id, "ending": "picked", "row": "open"})
    ).arm()
    calls = await _automation(hass, _device_id(hass), {"type": "list_picked", "row": "open"})

    await _show(hass, response=False)
    await hass.async_block_till_done()

    assert len(receiver.heard) == 1
    assert [dict(call.data) for call in calls] == [{"row": "open", "label": "Open the gate"}]


# --------------------------------------------------------------- the text rule --

# What the receiver shows for each input: its own `clean_line` (a title or a label, 60) and
# `clean_lines` (the text, 200), run on these strings and written down here - not computed
# from the plugin's code, which this repository does not carry. Both repositories test the
# same vectors.
SCREEN_TEXT: list[tuple[str, str, str]] = [
    ("\u200b", "", ""),
    ("\u202e\u2066", "", ""),
    ("\x01\x02", "", ""),
    ("\u00ad", "", ""),
    ("\ufeff", "", ""),
    ("  Open  ", "Open", "Open"),
    ("a\x00b", "a b", "a b"),
    ("\u200b" * 10 + "x" * 60, "x" * 60, "x" * 60),
    ("Line1\nLine2", "Line1 Line2", "Line1\nLine2"),
    ("a\r\nb\rc", "a  b c", "a\nb\nc"),
    ("a\\nb", "anb", "anb"),
    ("x\u2028y\u2029z", "x y z", "x y z"),
    ("\t tab \t", "tab", "tab"),
    ("\u200d" * 70 + "end", "end", "end"),
    ("e\u0301", "e\u0301", "e\u0301"),
    ("\u00a0nbsp\u00a0", "nbsp", "nbsp"),
    ("a\x7fb", "a b", "a b"),
    (" " * 60 + "w", "", "w"),
    ("\n\nLine\n\n", "Line", "Line"),
    # Half a surrogate pair is dropped like a format character.
    ("x\ud800y", "xy", "xy"),
    ("\udfff", "", ""),
]


@pytest.mark.parametrize(("raw", "line", "lines"), SCREEN_TEXT)
def test_the_text_rule_is_the_receiver_s(raw: str, line: str, lines: str) -> None:
    assert choice_list.clean_line(raw, 60) == line
    assert choice_list.clean_lines(raw, 200) == lines


@pytest.mark.parametrize(("raw", "line", "lines"), SCREEN_TEXT)
def test_a_title_and_a_label_are_what_the_receiver_shows_or_are_refused(
    raw: str, line: str, lines: str
) -> None:
    """Blank is judged on the cleaned text, so this side refuses what the receiver would."""
    as_title = {"title": raw, "rows": [{"id": "a", "label": "A"}]}
    as_label = {"title": "T", "rows": [{"id": "a", "label": raw}]}

    if line:
        assert build_list(as_title).title == line
        assert build_list(as_label).rows[0].label == line
    else:
        assert _refused(as_title).translation_key == "list_title_blank"
        assert _refused(as_label).translation_key == "list_row_label_blank"


@pytest.mark.parametrize(("raw", "line", "lines"), SCREEN_TEXT)
def test_the_text_is_what_the_receiver_shows_or_no_text(raw: str, line: str, lines: str) -> None:
    request = build_list({"title": "T", "text": raw, "rows": _rows(1)})

    assert request.text == (lines or None)
    assert ("text" in json.loads(request.payload())) is bool(lines)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**400])
def test_a_number_that_is_not_finite_is_not_a_text(value: Any) -> None:
    """`nan` and `inf` are a template's mistake, not a title anybody wrote."""
    assert _refused({"title": value, "rows": _rows(1)}).translation_key == "list_member_not_text"
    assert _refused({"title": "T", "rows": [{"id": "a", "label": value}]}).translation_key == (
        "list_row_member_not_text"
    )
    assert _refused({"title": "T", "rows": [{"id": value, "label": "A"}]}).translation_key == (
        "list_row_id_invalid"
    )
    assert _refused({**DOORBELL, "default": value}).translation_key == "list_default_unknown"


def test_a_large_finite_number_is_still_a_text() -> None:
    assert build_list({"title": 1e300, "rows": _rows(1)}).title == "1e+300"


async def test_the_label_reported_back_is_the_one_on_the_screen(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(
        hass,
        lambda list_id, _payload: _answer(hass, {"id": list_id, "ending": "picked", "row": "open"}),
    ).arm()

    response = await _show(
        hass,
        {
            "title": "\u202eDoorbell\u200b ",
            "text": "Someone\r\nat the gate\ufeff",
            "rows": [{"id": "open", "label": "  Open\u200b the\ngate  "}],
        },
    )

    assert response == {PLAYER: {"ending": "picked", "row": "open", "label": "Open the gate"}}
    (_, _, payload) = receiver.heard[0]
    assert payload["title"] == "Doorbell"
    assert payload["text"] == "Someone\nat the gate"
    assert payload["rows"] == [{"id": "open", "label": "Open the gate"}]


# ------------------------------------------------------- the waits that never start --


async def test_a_list_waiting_for_the_subscription_is_not_sent_after_an_unload(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Unloaded while it waited for the broker: nothing reads its answer, so it is not sent."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    confirmed = asyncio.Event()

    with patch.object(box, "async_wait_subscribed", confirmed.wait):
        task = _show(hass)
        await _settle()
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        confirmed.set()

        with pytest.raises(HomeAssistantError) as raised:
            await asyncio.wait_for(task, 5)

    assert raised.value.translation_key == "list_unloaded"
    assert receiver.heard == []
    assert box.lists is not None and box.lists.waiting == 0


async def test_a_list_that_could_not_be_published_is_not_remembered(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box = await _box(hass, config_entry, box_on_the_broker)

    with (
        patch.object(
            box, "async_publish_enveloped", side_effect=HomeAssistantError("broker gone")
        ),
        pytest.raises(HomeAssistantError),
    ):
        await _show(hass, response=False)

    assert box.lists is not None and box.lists.waiting == 0


# ------------------------------------------------------------ several receivers --

NODE_TWO = "vuuno4kse_005302"
PLAYER_TWO = "media_player.dekoder_kuchnia"
LIST_TOPIC_TWO = f"{BASE_TOPIC}/{NODE_TWO}/cmd/list"


async def _second_box(
    hass: HomeAssistant, retained: dict[str, str | bytes], info: dict[str, Any]
) -> Enigma2Box:
    """A second receiver on the same broker, set up after the first."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dekoder kuchnia",
        unique_id=NODE_TWO,
        data={CONF_NODE_ID: NODE_TWO, CONF_BASE_TOPIC: BASE_TOPIC, CONF_NAME: "Dekoder kuchnia"},
    )
    retained.clear()
    retained.update(
        {
            f"{BASE_TOPIC}/{NODE_TWO}/info": json.dumps(info),
            f"{BASE_TOPIC}/{NODE_TWO}/availability": "online",
            f"{BASE_TOPIC}/{NODE_TWO}/power": "on",
        }
    )
    await async_setup_box_then_retained(hass, entry, retained)
    box: Enigma2Box = entry.runtime_data
    await box.async_wait_subscribed()
    assert hass.states.get(PLAYER_TWO) is not None
    return box


async def _answer_on(hass: HomeAssistant, topic: str, answer: Callable[[str], None]) -> None:
    @callback
    def _received(msg: ReceiveMessage) -> None:
        list_id, _inner = opened_command(msg.payload)
        assert list_id is not None
        answer(list_id)

    await mqtt.async_subscribe(hass, topic, _received)


def _show_both(hass: HomeAssistant, *, response: bool = True) -> asyncio.Task[Any]:
    return asyncio.get_running_loop().create_task(
        hass.services.async_call(
            DOMAIN,
            "show_list",
            {"entity_id": [PLAYER, PLAYER_TWO], **DOORBELL},
            blocking=True,
            return_response=response,
        )
    )


async def test_two_receivers_answer_one_each(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _box(hass, config_entry, box_on_the_broker)
    await _second_box(hass, box_on_the_broker, CLAIMS)
    fired = _events(hass)
    await _answer_on(
        hass, LIST_TOPIC, lambda list_id: _answer(hass, {"id": list_id, "ending": "dismissed"})
    )
    await _answer_on(
        hass,
        LIST_TOPIC_TWO,
        lambda list_id: async_fire_mqtt_message(
            hass,
            f"{BASE_TOPIC}/{NODE_TWO}/list_answer",
            json.dumps({"id": list_id, "ending": "picked", "row": "open"}),
        ),
    )

    response = await _show_both(hass)

    assert response == {
        PLAYER: {"ending": "dismissed", "row": None, "label": None},
        PLAYER_TWO: {"ending": "picked", "row": "open", "label": "Open the gate"},
    }
    assert sorted((event["node_id"], event["ending"]) for event in fired) == [
        (NODE_ID, "dismissed"),
        (NODE_TWO, "picked"),
    ]


@pytest.mark.parametrize(
    ("second_info", "refuse", "error"),
    [
        (
            INFO,
            False,
            "This receiver cannot show a list of choices. It needs a plugin release that "
            "offers one.",
        ),
        (CLAIMS, True, "The receiver did not show the list: the receiver is in standby"),
    ],
    ids=["cannot show one", "refuses it"],
)
async def test_with_two_receivers_a_refusal_is_its_own_entry(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    second_info: dict[str, Any],
    refuse: bool,
    error: str,
) -> None:
    """Home Assistant raises the first exception of an entity action and drops the other
    answers with it, so with several targets a refusal is an entry, not an exception."""
    await _box(hass, config_entry, box_on_the_broker)
    await _second_box(hass, box_on_the_broker, second_info)
    await _answer_on(
        hass,
        LIST_TOPIC,
        lambda list_id: _answer(hass, {"id": list_id, "ending": "picked", "row": "ignore"}),
    )
    if refuse:
        await _answer_on(
            hass,
            LIST_TOPIC_TWO,
            lambda list_id: async_fire_mqtt_message(
                hass,
                f"{BASE_TOPIC}/{NODE_TWO}/last_error",
                json.dumps(
                    {
                        "id": list_id,
                        "cmd": "list",
                        "error": "the receiver is in standby",
                        "reason": "standby",
                    }
                ),
            ),
        )

    response = await _show_both(hass)

    assert response == {
        PLAYER: {"ending": "picked", "row": "ignore", "label": "Ignore"},
        PLAYER_TWO: {"ending": "refused", "error": error, "row": None, "label": None},
    }


async def test_with_two_receivers_a_mistake_in_the_fields_is_still_raised(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The same mistake for every receiver: an error, not two entries saying it twice."""
    await _box(hass, config_entry, box_on_the_broker)
    await _second_box(hass, box_on_the_broker, CLAIMS)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "show_list",
            {"entity_id": [PLAYER, PLAYER_TWO], **DOORBELL, "timeout": 4},
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == "list_timeout_invalid"


async def test_with_two_receivers_and_no_response_a_refusal_is_raised_after_both_were_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Without a response there is no entry to put it in; the receiver that can show the
    list has been sent it all the same."""
    await _box(hass, config_entry, box_on_the_broker)
    await _second_box(hass, box_on_the_broker, INFO)
    receiver = await Receiver(hass).arm()

    with pytest.raises(ServiceValidationError) as raised:
        await _show_both(hass, response=False)

    assert raised.value.translation_key == "list_unsupported"
    assert len(receiver.heard) == 1


def test_an_icon_no_utf8_holds_is_refused() -> None:
    """The text rule drops half a surrogate pair from texts; an icon's name it does not touch."""
    refused = _refused({"title": "T", "rows": [{"id": "a", "label": "A", "icon": "x\ud800"}]})

    assert refused.translation_key == "list_not_utf8"


async def test_twenty_four_polish_rows_fit_because_the_envelope_is_utf8(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The limit counts what the broker carries: a Polish letter is two bytes in UTF-8, six
    as an escape - and 24 rows of 60 such characters fit only the first way."""
    await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    polish = ("za\u017c\u00f3\u0142\u0107 g\u0119\u015bl\u0105 ja\u017a\u0144 " * 4)[:60]
    assert len(polish) == 60
    rows = [{"id": f"row{n:02}", "label": polish} for n in range(24)]

    request = build_list({"title": polish, "rows": rows})
    await _show(hass, {"title": polish, "rows": rows}, response=False)

    (wire, list_id, payload) = receiver.heard[0]
    text = wire.decode() if isinstance(wire, (bytes, bytearray)) else wire
    assert "\\u" not in text
    assert polish in text
    size = len(text.encode("utf-8"))
    assert size == len(choice_list.command_envelope(list_id, request.payload()).encode("utf-8"))
    assert size <= COMMAND_MAX_BYTES
    # Escaped, the same list would not have fitted.
    escaped = json.dumps({"id": list_id, "payload": request.payload()}, separators=(",", ":"))
    assert len(escaped) > COMMAND_MAX_BYTES
    assert [row["label"] for row in payload["rows"]] == [polish] * 24

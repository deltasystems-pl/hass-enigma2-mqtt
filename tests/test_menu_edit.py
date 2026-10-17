"""The menu creator's edits: untrusted input, read with the options flow's rules, stored
without a reload, and answered."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import re
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.enigma2_mqtt import menu_creator
from custom_components.enigma2_mqtt.const import CONF_DANGEROUS_BUTTONS, CONF_MENU
from custom_components.enigma2_mqtt.menu import menu_revision, stored_menu
from custom_components.enigma2_mqtt.menu_creator import read_edited_menu

from .conftest import BASE_TOPIC, INFO, INFO_TOPIC, NODE_ID, async_setup_box
from .menu_common import (
    MENU_CAPABILITIES,
    MISSING_ID,
    claim_editor,
    editor_entry,
    group,
    items_by_id,
    last_catalogue,
    last_menu,
    row,
)

EDIT_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/menu_edit"
RESULT_TOPIC = f"enigma2mqtt/menu_edit_result/{NODE_ID}"
EDIT_ID = "8c1d2e3f4a5b"
ROW_ID = re.compile(r"^r_[0-9a-f]{10}$")
GROUP_ID = re.compile(r"^g_[0-9a-f]{10}$")


class Clock:
    """The save interval's clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Any:
    tick = Clock()
    with patch.object(menu_creator, "_monotonic", tick):
        yield tick


def _states(hass: HomeAssistant) -> dict[str, str]:
    """The household, and the registry ids of what is registered."""
    registry = er.async_get(hass)
    lamp = registry.async_get_or_create("light", "demo", "l1", suggested_object_id="salon")
    blind = registry.async_get_or_create("cover", "demo", "c1", suggested_object_id="salon")
    hass.states.async_set(
        "light.salon",
        "on",
        {"friendly_name": "Salon lamp", "brightness": 128, "supported_color_modes": ["brightness"]},
    )
    hass.states.async_set(
        "cover.salon", "open",
        {"friendly_name": "Salon blind", "current_position": 40, "supported_features": 15},
    )
    hass.states.async_set("script.movie", "off", {"friendly_name": "Movie night"})
    hass.states.async_set("switch.kettle", "off", {"friendly_name": "Kettle"})
    return {"lamp": lamp.id, "blind": blind.id}


def stored(ids: dict[str, str]) -> list[dict[str, Any]]:
    return [
        group("g_salon", "Salon", [row("r_lamp", ids["lamp"], "toggle")]),
        row("r_movie", "script.movie", "button", label="Movie"),
        row("r_gone", MISSING_ID, "toggle"),
    ]


def sent_row(row_id: str | None, entity: str, control: str, **extra: Any) -> dict[str, Any]:
    return {"type": "row", "id": row_id, "entity": entity, "control": control,
            "label": None, "icon": None, **extra}


def sent_group(group_id: str | None, label: str, rows: list, icon: str = "sofa") -> dict:
    return {"type": "group", "id": group_id, "label": label, "icon": icon, "rows": rows}


def as_catalogue() -> list[dict[str, Any]]:
    """The stored menu as the catalogue gives it, which is what an unchanged edit sends."""
    return [
        sent_group("g_salon", "Salon", [sent_row("r_lamp", "light.salon", "toggle")]),
        sent_row("r_movie", "script.movie", "button", label="Movie"),
        sent_row("r_gone", MISSING_ID, "toggle", missing=True),
    ]


async def _setup(
    hass: HomeAssistant,
    retained: dict[str, str | bytes],
    *,
    claim: bool = True,
    remote_edit: bool = True,
) -> tuple[MockConfigEntry, dict[str, str]]:
    ids = _states(hass)
    if claim:
        claim_editor(retained)
    entry = editor_entry(stored(ids), remote_edit=remote_edit)
    await async_setup_box(hass, entry)
    return entry, ids


def revision(entry: MockConfigEntry) -> str:
    return menu_revision(stored_menu(entry.options))


def fire_edit(
    hass: HomeAssistant,
    menu: Any,
    base: str,
    *,
    edit_id: str = EDIT_ID,
    retain: bool = False,
    **overrides: Any,
) -> None:
    payload: dict[str, Any] = {"id": edit_id, "base": base, "ts": 1792050000, "menu": menu}
    payload.update(overrides)
    async_fire_mqtt_message(hass, EDIT_TOPIC, json.dumps(payload), retain=retain)


def results(mqtt_mock: Any) -> list[dict[str, Any]]:
    """Every answer published, decoded; each not retained, QoS 1."""
    answers = []
    for call in mqtt_mock.async_publish.call_args_list:
        if call.args[0] == RESULT_TOPIC:
            assert (call.args[2], call.args[3]) == (1, False)
            answers.append(json.loads(call.args[1]))
    return answers


# --------------------------------------------------------------------------- saved


async def test_an_accepted_edit_is_stored_without_a_reload_and_republished(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker)
    box = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_DANGEROUS_BUTTONS: True}
    )
    base = revision(entry)
    edited = [
        sent_group("g_salon", "  Living   room ", [
            sent_row("r_lamp", "light.salon", "toggle"),
            sent_row(None, "cover.salon", "slider", label="Blind", icon="blinds"),
        ], icon="not-an-icon"),
        sent_row("r_movie", "script.movie", "button", label="Movie"),
        sent_row("r_gone", MISSING_ID, "toggle", missing=True),
        sent_group(None, "Garden", []),
    ]

    with patch.object(hass.config_entries, "async_reload") as reload:
        fire_edit(hass, edited, base)
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
        await hass.async_block_till_done()

    reload.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is box
    menu = entry.options[CONF_MENU]
    assert entry.options[CONF_DANGEROUS_BUTTONS] is True
    salon, movie, gone, garden = menu
    assert salon == {
        "type": "group", "id": "g_salon", "label": "Living room", "icon": "neutral",
        "rows": [
            {"type": "row", "id": "r_lamp", "entity_id": ids["lamp"], "control": "toggle"},
            {"type": "row", "id": salon["rows"][1]["id"], "entity_id": ids["blind"],
             "label": "Blind", "icon": "blinds", "control": "slider"},
        ],
    }
    assert ROW_ID.match(salon["rows"][1]["id"])
    assert movie == row("r_movie", "script.movie", "button", label="Movie")
    assert gone == row("r_gone", MISSING_ID, "toggle")
    assert GROUP_ID.match(garden["id"]) and garden["rows"] == [] and garden["label"] == "Garden"

    new_revision = revision(entry)
    assert new_revision != base
    assert results(mqtt_mock) == [
        {"id": EDIT_ID, "result": "saved", "revision": new_revision, "path": None,
         "detail": None}
    ]
    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None and catalogue["revision"] == new_revision
    published = last_menu(mqtt_mock)
    assert published is not None
    assert items_by_id(published)[salon["rows"][1]["id"]]["label"] == "Blind"
    assert items_by_id(published)["g_salon"]["label"] == "Living room"


async def test_the_catalogue_and_the_answer_go_out_before_any_time_passes(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    """The page waits for the catalogue with the new revision after `saved`."""
    entry, _ids = await _setup(hass, box_on_the_broker)
    edited = as_catalogue()
    edited[1]["label"] = "Kino"

    fire_edit(hass, edited, revision(entry))
    await hass.async_block_till_done()

    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert catalogue["revision"] == revision(entry) == results(mqtt_mock)[-1]["revision"]
    assert catalogue["menu"][1]["label"] == "Kino"


async def test_an_edit_that_changes_nothing_is_saved_with_the_same_revision(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker)
    base = revision(entry)

    fire_edit(hass, as_catalogue(), base)
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    assert results(mqtt_mock)[-1]["result"] == "saved"
    assert results(mqtt_mock)[-1]["revision"] == base


async def test_an_emptied_menu_removes_the_option_and_publishes_no_menu(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)

    fire_edit(hass, [], revision(entry))
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert CONF_MENU not in entry.options
    assert results(mqtt_mock)[-1]["revision"] == menu_revision([])
    assert last_menu(mqtt_mock) is None


async def test_a_row_keeps_its_id_only_while_entity_and_control_stay(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker)
    old_ids = {"g_salon", "r_lamp", "r_movie", "r_gone"}
    edited = [
        # Moved out of its group, renamed, given an icon: the same binding.
        sent_row("r_lamp", "light.salon", "toggle", label="Lamp", icon="lamp"),
        sent_group("g_salon", "Salon", [
            # Another control: a new row as far as the receiver can tell.
            sent_row("r_movie", "light.salon", "slider"),
        ]),
        # Another entity, same control.
        sent_row("r_gone", "switch.kettle", "toggle"),
    ]

    fire_edit(hass, edited, revision(entry))
    await hass.async_block_till_done()

    lamp, salon, kettle = entry.options[CONF_MENU]
    assert lamp["id"] == "r_lamp" and lamp["label"] == "Lamp" and lamp["icon"] == "lamp"
    assert salon["id"] == "g_salon"
    slider = salon["rows"][0]
    assert ROW_ID.match(slider["id"]) and slider["id"] not in old_ids
    assert slider["entity_id"] == ids["lamp"]
    assert ROW_ID.match(kettle["id"]) and kettle["id"] not in old_ids
    assert kettle["id"] != slider["id"]
    assert kettle["entity_id"] == "switch.kettle"


async def test_a_row_stored_by_entity_id_keeps_its_id_once_the_entity_is_registered(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    # Registered after the row was stored by its entity id.
    script = er.async_get(hass).async_get_or_create(
        "script", "demo", "movie", suggested_object_id="movie"
    )
    assert script.entity_id == "script.movie"
    entry, _ids = await _setup(hass, box_on_the_broker)
    assert entry.options[CONF_MENU][1]["entity_id"] == "script.movie"

    fire_edit(hass, as_catalogue(), revision(entry))
    await hass.async_block_till_done()

    movie = entry.options[CONF_MENU][1]
    assert movie["id"] == "r_movie"
    assert movie["entity_id"] == script.id


async def test_labels_and_icons_follow_the_options_flows_rules(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)
    edited = as_catalogue()
    edited[0]["label"] = "G" * 70
    edited[0]["icon"] = None
    edited[1]["label"] = "   "
    edited[1]["icon"] = "nonexistent"
    edited[2]["label"] = " two\n words "
    edited[2]["icon"] = 5

    fire_edit(hass, edited, revision(entry))
    await hass.async_block_till_done()

    salon, movie, gone = entry.options[CONF_MENU]
    assert salon["label"] == "G" * 60 and salon["icon"] == "neutral"
    assert "label" not in movie and "icon" not in movie
    assert gone["label"] == "two words" and "icon" not in gone


async def test_a_missing_row_survives_unchanged_but_its_control_cannot_change(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)
    edited = as_catalogue()
    edited[2]["control"] = "indicator"

    fire_edit(hass, edited, revision(entry))
    await hass.async_block_till_done()

    answer = results(mqtt_mock)[-1]
    assert (answer["result"], answer["path"]) == ("invalid", "menu[2].control")
    assert entry.options[CONF_MENU][2] == row("r_gone", MISSING_ID, "toggle")


# ------------------------------------------------------------------------ refusals


async def test_a_retained_edit_is_ignored(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker)

    fire_edit(hass, [], revision(entry), retain=True)
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    assert results(mqtt_mock) == []


async def test_a_receiver_that_does_not_claim_the_editor_is_answered_not_claimed(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker, claim=False)
    async_fire_mqtt_message(
        hass, INFO_TOPIC, json.dumps({**INFO, "capabilities": MENU_CAPABILITIES})
    )
    await hass.async_block_till_done()

    fire_edit(hass, [], revision(entry))
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    assert results(mqtt_mock) == [
        {"id": EDIT_ID, "result": "not_claimed", "revision": None, "path": None,
         "detail": None}
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe not utf-8",
        b"not json",
        b'["a list"]',
        b'"a text"',
        b'{"id": "XYZ", "base": "0000000000000000", "menu": []}',
        b'{"id": 5, "base": "0000000000000000", "menu": []}',
        b'{"base": "0000000000000000", "menu": []}',
        b"[" * 60000 + b"]" * 60,
        json.dumps({"id": EDIT_ID, "base": "0" * 16, "menu": [], "pad": "x" * 65536}).encode(),
        b"",
    ],
)
async def test_an_unreadable_edit_gets_no_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    clock,
    payload: bytes,
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker)

    async_fire_mqtt_message(hass, EDIT_TOPIC, payload)
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    assert results(mqtt_mock) == []


async def test_an_edit_of_up_to_64_kib_is_read(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)
    payload = {"id": EDIT_ID, "base": revision(entry), "menu": as_catalogue(), "pad": ""}
    size = len(json.dumps(payload).encode())
    payload["pad"] = "x" * (65536 - size)
    assert len(json.dumps(payload).encode()) == 65536

    async_fire_mqtt_message(hass, EDIT_TOPIC, json.dumps(payload))
    await hass.async_block_till_done()

    assert results(mqtt_mock)[-1]["result"] == "saved"


@pytest.mark.parametrize("base", ["not-a-revision", 5, None, "0123456789ABCDEF"])
async def test_an_edit_with_an_id_but_no_usable_base_is_invalid(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    clock,
    base: Any,
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker)

    fire_edit(hass, [], base)
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    answer = results(mqtt_mock)[-1]
    assert (answer["result"], answer["path"]) == ("invalid", "base")
    assert answer["detail"]


async def test_an_edit_from_another_revision_is_stale(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    """Checked before the menu is read: a stale edit with a broken menu is stale."""
    entry, ids = await _setup(hass, box_on_the_broker)

    fire_edit(hass, "not even a list", "0123456789abcdef")
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    assert results(mqtt_mock) == [
        {"id": EDIT_ID, "result": "stale", "revision": revision(entry), "path": None,
         "detail": None}
    ]


async def test_at_most_one_accepted_save_every_two_seconds(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)
    fire_edit(hass, [], revision(entry), edit_id="000000000001")
    await hass.async_block_till_done()
    emptied = revision(entry)

    clock.now += 1.9
    fire_edit(hass, as_catalogue()[1:2], emptied, edit_id="000000000002")
    await hass.async_block_till_done()
    assert CONF_MENU not in entry.options

    clock.now += 0.2
    fire_edit(hass, [sent_row(None, "script.movie", "button")], emptied, edit_id="000000000003")
    await hass.async_block_till_done()

    assert [(answer["id"], answer["result"]) for answer in results(mqtt_mock)] == [
        ("000000000001", "saved"),
        ("000000000002", "busy"),
        ("000000000003", "saved"),
    ]
    assert results(mqtt_mock)[1]["revision"] == emptied
    assert len(entry.options[CONF_MENU]) == 1


async def test_a_refused_edit_does_not_count_towards_the_interval(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)
    fire_edit(hass, [], "0123456789abcdef", edit_id="000000000001")
    await hass.async_block_till_done()

    fire_edit(hass, [], revision(entry), edit_id="000000000002")
    await hass.async_block_till_done()

    assert [answer["result"] for answer in results(mqtt_mock)] == ["stale", "saved"]


async def test_one_save_at_a_time(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    """A save whose catalogue is still going out keeps the next one busy, whatever the clock."""
    entry, _ids = await _setup(hass, box_on_the_broker)
    release = asyncio.Event()
    publish = menu_creator.MenuCreator._async_publish

    async def _slow(self: Any, *, force: bool) -> None:
        await release.wait()
        await publish(self, force=force)

    with patch.object(menu_creator.MenuCreator, "_async_publish", _slow):
        fire_edit(hass, [], revision(entry), edit_id="000000000001")
        for _ in range(20):
            await asyncio.sleep(0)
        clock.now += 10
        fire_edit(hass, [], revision(entry), edit_id="000000000002")
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        await hass.async_block_till_done()

    assert [(answer["id"], answer["result"]) for answer in results(mqtt_mock)] == [
        ("000000000002", "busy"),
        ("000000000001", "saved"),
    ]


INVALID_CASES: list[tuple[str, Any, str]] = [
    ("not a list", {"a": 1}, "menu"),
    ("25 at the top", [sent_row(None, "script.movie", "button")] * 25, "menu"),
    ("nine groups", [sent_group(None, f"G{i}", []) for i in range(9)], "menu[8]"),
    ("not an object", ["row"], "menu[0]"),
    ("unknown type", [{"type": "folder", "id": None}], "menu[0].type"),
    ("empty group name", [sent_group(None, " \t ", [])], "menu[0].label"),
    ("group name not text", [sent_group(None, 5, [])], "menu[0].label"),  # type: ignore[arg-type]
    ("rows not a list", [{**sent_group(None, "G", []), "rows": None}], "menu[0].rows"),
    ("25 rows", [sent_group(None, "G", [sent_row(None, "script.movie", "button")] * 25)],
     "menu[0].rows"),
    ("a group in a group", [sent_group(None, "G", [sent_group(None, "H", [])])],
     "menu[0].rows[0].type"),
    ("a row not an object", [sent_group(None, "G", ["x"])], "menu[0].rows[0]"),
    ("an id that is not stored", [sent_row("r_nothere", "script.movie", "button")],
     "menu[0].id"),
    ("an id twice", [sent_row("r_movie", "script.movie", "button"),
                     sent_row("r_movie", "script.movie", "button")], "menu[1].id"),
    ("a group's id on a row", [sent_row("g_salon", "script.movie", "button")], "menu[0].id"),
    ("a row's id on a group", [sent_group("r_movie", "G", [])], "menu[0].id"),
    ("an id that is not text", [sent_row(7, "script.movie", "button")], "menu[0].id"),  # type: ignore[arg-type]
    ("an entity nobody has", [sent_row(None, "light.nowhere", "toggle")], "menu[0].entity"),
    ("an entity that is not text", [sent_row(None, 5, "toggle")], "menu[0].entity"),  # type: ignore[arg-type]
    ("a registry id on a new row", [sent_row(None, MISSING_ID, "toggle")], "menu[0].entity"),
    ("another row's binding", [sent_row("r_movie", MISSING_ID, "toggle")], "menu[0].entity"),
    ("a control the entity does not take", [sent_row(None, "script.movie", "toggle")],
     "menu[0].control"),
    ("a control that is not text", [sent_row(None, "script.movie", None)], "menu[0].control"),  # type: ignore[arg-type]
    ("an unknown control", [sent_row(None, "light.salon", "dial")], "menu[0].control"),
    ("a row label not text", [sent_row(None, "script.movie", "button", label=3)],
     "menu[0].label"),
    ("a nested path", [sent_group("g_salon", "Salon", [
        sent_row("r_lamp", "light.salon", "toggle"),
        sent_row(None, "switch.kettle", "slider"),
    ])], "menu[0].rows[1].control"),
]


@pytest.mark.parametrize(("menu", "path"), [case[1:] for case in INVALID_CASES],
                         ids=[case[0] for case in INVALID_CASES])
async def test_an_invalid_menu_is_refused_with_where_and_why(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    clock,
    menu: Any,
    path: str,
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker)
    base = revision(entry)

    fire_edit(hass, menu, base)
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    [answer] = results(mqtt_mock)
    assert answer["id"] == EDIT_ID
    assert answer["result"] == "invalid"
    assert answer["revision"] == base
    assert answer["path"] == path
    assert isinstance(answer["detail"], str) and 0 < len(answer["detail"]) <= 200
    assert answer["detail"].isascii()


async def test_a_detail_is_cut_at_200_characters(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)

    fire_edit(hass, [sent_row(None, "light." + "x" * 240, "toggle")], revision(entry))
    await hass.async_block_till_done()

    detail = results(mqtt_mock)[-1]["detail"]
    # What the edit sent is quoted cut at 60 characters, and the sentence stays within 200.
    assert len(detail) <= 200
    assert "'light." + "x" * 54 + "'" in detail
    assert "x" * 55 not in detail


async def test_each_cause_is_a_warning_once_then_quiet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)

    with caplog.at_level(logging.DEBUG, logger="custom_components.enigma2_mqtt.menu_creator"):
        for number in range(3):
            fire_edit(hass, [], "0123456789abcdef", edit_id=f"00000000000{number}")
            await hass.async_block_till_done()
        fire_edit(hass, "x", revision(entry), edit_id="000000000009")
        await hass.async_block_till_done()

    refusals = [r for r in caplog.records if "Refused a menu edit" in r.getMessage()]
    assert [r.levelno for r in refusals] == [
        logging.WARNING, logging.DEBUG, logging.DEBUG, logging.WARNING
    ]


def test_the_reader_never_reuses_a_stored_id_for_a_new_entry(hass: HomeAssistant) -> None:
    hass.states.async_set("script.movie", "off")
    stored_menu_ = [row("r_movie", "script.movie", "button")]
    ids = iter(["r_movie", "r_movie", "r_fresh0001"])

    with patch("custom_components.enigma2_mqtt.menu.secrets.token_hex",
               side_effect=lambda _n: next(ids)[2:]):
        menu = read_edited_menu(
            hass, stored_menu_, [sent_row(None, "script.movie", "button")]
        )

    assert menu[0]["id"] == "r_fresh0001"


def test_a_nesting_too_deep_for_the_parser_is_unreadable_not_an_exception() -> None:
    deep = b"[" * 60000 + b"]" * 60000
    nested_id = b'{"id": "8c1d2e3f4a5b", "base": "' + b"0" * 16 + b'", "menu": ' + b"[" * 30000

    assert menu_creator.decode_edit(deep) is None
    assert menu_creator.edit_id_of(menu_creator.decode_edit(nested_id)) is None
    with pytest.raises(menu_creator.EditRefused) as refused:
        menu_creator.parse_edit(menu_creator.decode_edit(b"[" * 40000 + b"]" * 20000))
    assert refused.value.result is None
    # An older Python's parser gives up on such a nesting with RecursionError.
    with patch.object(menu_creator.json, "loads", side_effect=RecursionError):
        assert menu_creator.decode_edit(b'{"id": "8c1d2e3f4a5b"}') is None


# ----------------------------------------------------------- opt-in and check order


async def test_with_editing_off_every_edit_is_disabled(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, ids = await _setup(hass, box_on_the_broker, remote_edit=False)

    fire_edit(hass, as_catalogue(), revision(entry))
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    assert results(mqtt_mock) == [
        {"id": EDIT_ID, "result": "disabled", "revision": None, "path": None,
         "detail": None}
    ]
    # A menu changed in the options meanwhile: the refusal still says nothing about it.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_MENU: [row("r_new", "script.movie", "button")]}
    )
    fire_edit(hass, as_catalogue(), revision(entry), edit_id="000000000009")
    await hass.async_block_till_done()
    assert results(mqtt_mock)[-1]["revision"] is None


async def test_disabled_comes_after_not_claimed_and_unreadable_and_before_busy(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    from custom_components.enigma2_mqtt.const import CONF_MENU_REMOTE_EDIT

    entry, _ids = await _setup(hass, box_on_the_broker)
    fire_edit(hass, [], revision(entry), edit_id="000000000001")
    await hass.async_block_till_done()
    # Turned off at once, without a reload: the next edit is inside the two seconds.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_MENU_REMOTE_EDIT: False}
    )
    fire_edit(hass, [], revision(entry), edit_id="000000000002")
    await hass.async_block_till_done()
    fire_edit(hass, [], "bad", edit_id="000000000003")
    await hass.async_block_till_done()

    assert [(a["id"], a["result"], a["path"]) for a in results(mqtt_mock)] == [
        ("000000000001", "saved", None),
        ("000000000002", "disabled", None),
        ("000000000003", "invalid", "base"),
    ]


async def test_not_claimed_comes_before_a_bad_base_and_before_disabled(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker, claim=False, remote_edit=False)

    fire_edit(hass, [], "bad", edit_id="000000000001")
    fire_edit(hass, [], revision(entry), edit_id="000000000002")
    await hass.async_block_till_done()

    assert [a["result"] for a in results(mqtt_mock)] == ["not_claimed", "not_claimed"]


async def test_busy_comes_before_stale(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)
    fire_edit(hass, [], revision(entry), edit_id="000000000001")
    await hass.async_block_till_done()

    fire_edit(hass, [], "0123456789abcdef", edit_id="000000000002")
    await hass.async_block_till_done()

    assert [a["result"] for a in results(mqtt_mock)] == ["saved", "busy"]


# ------------------------------------------------------------- text that cannot be held


@pytest.mark.parametrize(
    "raw",
    [
        # A lone surrogate, escaped: valid JSON in valid UTF-8 bytes.
        b'{"id": "8c1d2e3f4a5b", "base": "%s", "menu": [{"type": "group", "id": null, '
        b'"label": "x\\ud800", "icon": "sofa", "rows": []}]}',
        b'{"id": "8c1d2e3f4a5b", "base": "%s", "menu": [], "\\udfff": 1}',
        b'{"id": "8c1d2e3f4a5b", "base": "%s", "menu": [{"type": "row", "id": null, '
        b'"entity": "script.movie", "control": "button", "label": "a\\udc80b", "icon": null}]}',
        b'{"id": "8c1d2e3f4a5b", "base": "%s", "menu": [], "ts": "\\ud83d"}',
    ],
)
async def test_a_text_utf8_cannot_hold_is_unreadable_and_nothing_breaks(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    clock,
    raw: bytes,
) -> None:
    from homeassistant.helpers.json import json_bytes

    entry, ids = await _setup(hass, box_on_the_broker)
    payload = raw.replace(b"%s", revision(entry).encode())

    async_fire_mqtt_message(hass, EDIT_TOPIC, payload)
    await hass.async_block_till_done()

    assert entry.options[CONF_MENU] == stored(ids)
    assert results(mqtt_mock) == []
    # Every config entry can still be written, and the next edit is answered.
    json_bytes(entry.as_storage_fragment)
    json_bytes(hass.config_entries._data_to_save())
    fire_edit(hass, as_catalogue(), revision(entry))
    await hass.async_block_till_done()
    assert results(mqtt_mock)[-1]["result"] == "saved"


async def test_nothing_from_an_edit_reaches_the_answer_or_the_log_as_it_came(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A line break in an id, an entity or a control cannot forge a second log line."""
    entry, _ids = await _setup(hass, box_on_the_broker)
    forged = "x\n2026-10-17 12:00:00 ERROR (MainThread) [homeassistant] forged\x1b[31m\u00e9"

    with caplog.at_level(logging.DEBUG, logger="custom_components.enigma2_mqtt.menu_creator"):
        for number, menu in enumerate(
            [
                [sent_row(forged, "script.movie", "button")],
                [sent_row(None, forged, "button")],
                [sent_row(None, "script.movie", forged)],
            ]
        ):
            clock.now += 3
            fire_edit(hass, menu, revision(entry), edit_id=f"00000000000{number}")
            await hass.async_block_till_done()

    answers = results(mqtt_mock)
    assert [a["result"] for a in answers] == ["invalid"] * 3
    for answer in answers:
        assert answer["detail"].isascii()
        assert "\n" not in answer["detail"] and "\x1b" not in answer["detail"]
        assert all(ord(c) >= 32 for c in answer["detail"])
    for record in caplog.records:
        message = record.getMessage()
        assert "\n" not in message and "\x1b" not in message


@pytest.mark.parametrize("ts", ["9" * 400, "1e999", "NaN", "Infinity", '"soon"', "true"])
async def test_a_ts_that_is_no_usable_number_is_only_dropped(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    clock,
    ts: str,
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)
    body = json.dumps({"id": EDIT_ID, "base": revision(entry), "menu": as_catalogue()})
    payload = body[:-1] + f', "ts": {ts}}}'

    async_fire_mqtt_message(hass, EDIT_TOPIC, payload)
    await hass.async_block_till_done()

    assert results(mqtt_mock)[-1]["result"] == "saved"


def test_a_huge_integer_is_read_as_nothing_and_cheaply() -> None:
    data = menu_creator.decode_edit(
        b'{"id": "8c1d2e3f4a5b", "base": "0000000000000000", "ts": ' + b"7" * 4000 + b"}"
    )

    assert data is not None and data["ts"] is None
    assert menu_creator.parse_edit(data).ts is None


# ------------------------------------------------------------ unchanged and changed rows


async def test_an_unchanged_row_is_kept_although_its_entity_no_longer_offers_its_control(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    """As the options flow keeps a row nobody changed; a changed row is checked."""
    entry, ids = await _setup(hass, box_on_the_broker)
    menu = [*stored(ids), row("r_dim", ids["lamp"], "slider")]
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_MENU: menu})
    # The lamp loses its brightness: no slider any more.
    hass.states.async_set(
        "light.salon", "on", {"friendly_name": "Salon lamp", "supported_color_modes": ["onoff"]}
    )
    edited = [*as_catalogue(), sent_row("r_dim", "light.salon", "slider")]
    edited[1]["label"] = "Kino"

    fire_edit(hass, edited, revision(entry), edit_id="000000000001")
    await hass.async_block_till_done()
    assert results(mqtt_mock)[-1]["result"] == "saved"
    assert entry.options[CONF_MENU][3] == row("r_dim", ids["lamp"], "slider")

    clock.now += 3
    edited = [*as_catalogue(), sent_row(None, "light.salon", "slider")]
    fire_edit(hass, edited, revision(entry), edit_id="000000000002")
    await hass.async_block_till_done()
    answer = results(mqtt_mock)[-1]
    assert (answer["result"], answer["path"]) == ("invalid", "menu[3].control")


async def test_a_stored_registry_binding_is_checked_against_the_entity_it_names(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    """Sent back as the registry id it is stored by, a changed control is checked."""
    entry, ids = await _setup(hass, box_on_the_broker)
    edited = as_catalogue()
    edited[0]["rows"][0] = sent_row("r_lamp", ids["lamp"], "button")

    fire_edit(hass, edited, revision(entry), edit_id="000000000001")
    await hass.async_block_till_done()
    answer = results(mqtt_mock)[-1]
    assert (answer["result"], answer["path"]) == ("invalid", "menu[0].rows[0].control")
    assert "light.salon" in answer["detail"]

    clock.now += 3
    edited[0]["rows"][0] = sent_row("r_lamp", ids["lamp"], "slider")
    fire_edit(hass, edited, revision(entry), edit_id="000000000002")
    await hass.async_block_till_done()
    assert results(mqtt_mock)[-1]["result"] == "saved"
    slider = entry.options[CONF_MENU][0]["rows"][0]
    assert slider["entity_id"] == ids["lamp"] and slider["control"] == "slider"
    assert slider["id"] != "r_lamp"


# ---------------------------------------------------- other saves keep the creator's menu


def test_another_form_carries_the_menu_over_as_the_entry_holds_it_now() -> None:
    from custom_components.enigma2_mqtt.const import CONF_MENU_REMOTE_EDIT
    from custom_components.enigma2_mqtt.menu_flow import with_current_menu

    pending = {"dangerous_buttons": True, CONF_MENU: ["old"], CONF_MENU_REMOTE_EDIT: True}

    assert with_current_menu(pending, {CONF_MENU: ["new"]}) == {
        "dangerous_buttons": True, CONF_MENU: ["new"]
    }
    assert with_current_menu({"x": 1}, {CONF_MENU_REMOTE_EDIT: True}) == {
        "x": 1, CONF_MENU_REMOTE_EDIT: True
    }


async def test_the_settings_form_keeps_a_menu_stored_from_the_receiver_meanwhile(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.enigma2_mqtt.const import CONF_MENU_REMOTE_EDIT

    entry, _ids = await _setup(hass, box_on_the_broker)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.FORM
    # Saved from the receiver while the form is open.
    fire_edit(hass, [sent_row(None, "script.movie", "button", label="Kino")], revision(entry))
    await hass.async_block_till_done()
    assert results(mqtt_mock)[-1]["result"] == "saved"
    kept = entry.options[CONF_MENU]

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MENU] == kept
    assert entry.options[CONF_MENU_REMOTE_EDIT] is True


async def test_the_ssh_steps_keep_a_menu_stored_from_the_receiver_meanwhile(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    from unittest.mock import AsyncMock

    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.enigma2_mqtt.const import (
        CONF_SSH_HOST,
        CONF_SSH_PASSWORD,
        CONF_SSH_PORT,
        CONF_SSH_USERNAME,
    )

    from .test_installer_flow import HOST_KEY, PREFLIGHT

    entry, _ids = await _setup(hass, box_on_the_broker)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"dangerous_buttons": False, "wol_mac": "", "bouquets": [], "configure_ssh": True},
    )
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_probe_host_key",
        AsyncMock(return_value=HOST_KEY),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SSH_HOST: "receiver.local", CONF_SSH_PORT: 22}
        )
    # Saved from the receiver while the SSH steps are open.
    fire_edit(hass, [sent_row(None, "script.movie", "button", label="Kino")], revision(entry))
    await hass.async_block_till_done()
    assert results(mqtt_mock)[-1]["result"] == "saved"
    kept = entry.options[CONF_MENU]
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_preflight",
        AsyncMock(return_value=PREFLIGHT),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SSH_USERNAME: "root", CONF_SSH_PASSWORD: "secret"}
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MENU] == kept


def test_only_a_stated_true_allows_editing_from_the_receiver() -> None:
    from custom_components.enigma2_mqtt.const import CONF_MENU_REMOTE_EDIT

    assert menu_creator.editing_allowed({CONF_MENU_REMOTE_EDIT: True}) is True
    for value in (False, None, 1, "true", "yes", [True]):
        assert menu_creator.editing_allowed({CONF_MENU_REMOTE_EDIT: value}) is False
    assert menu_creator.editing_allowed({}) is False


def test_the_revision_of_a_text_utf8_cannot_hold_does_not_raise() -> None:
    menu = [{"type": "group", "id": "g_a", "label": "x\ud800", "icon": "sofa", "rows": []}]

    assert len(menu_revision(menu)) == 16


async def test_only_disabled_and_not_claimed_leave_the_revision_out(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], clock
) -> None:
    entry, _ids = await _setup(hass, box_on_the_broker)
    fire_edit(hass, [], "0123456789abcdef", edit_id="000000000001")
    await hass.async_block_till_done()
    fire_edit(hass, "x", revision(entry), edit_id="000000000002")
    await hass.async_block_till_done()
    fire_edit(hass, [], revision(entry), edit_id="000000000003")
    await hass.async_block_till_done()
    clock.now += 1
    fire_edit(hass, [], revision(entry), edit_id="000000000004")
    await hass.async_block_till_done()

    answers = {a["id"]: (a["result"], a["revision"]) for a in results(mqtt_mock)}
    emptied = menu_revision([])
    assert answers["000000000001"][0] == "stale" and answers["000000000001"][1]
    assert answers["000000000002"][0] == "invalid" and answers["000000000002"][1]
    assert answers["000000000003"] == ("saved", emptied)
    assert answers["000000000004"] == ("busy", emptied)

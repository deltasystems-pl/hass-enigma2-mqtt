"""The receiver menu as Home Assistant publishes it: shape, bounds, ids and when it goes out."""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
import json
import logging
from typing import Any
from unittest.mock import patch

from homeassistant.components.mqtt.const import MQTT_CONNECTION_STATE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.enigma2_mqtt.menu import (
    MAX_PAYLOAD_BYTES,
    async_menu_texts,
    build_menu,
    coarsen,
    encode_menu,
    fit_menu,
    snap,
    stored_menu,
)

from .conftest import AVAILABILITY_TOPIC, NODE_ID, async_setup_box
from .menu_common import (
    MENU_TOPIC,
    claim_menu,
    fire_action,
    group,
    items_by_id,
    last_menu,
    menu_entry,
    menu_publishes,
    row,
)

TEXTS = {"unavailable": "Unavailable", "unknown": "Unknown"}


def _states(hass: HomeAssistant) -> None:
    """The household the menus below are bound to."""
    hass.states.async_set(
        "light.salon",
        "on",
        {"friendly_name": "Salon lamp", "brightness": 128, "supported_color_modes": ["brightness"]},
    )
    hass.states.async_set(
        "cover.salon", "open", {"friendly_name": "Salon blind", "current_position": 40,
                                "supported_features": 15}
    )
    hass.states.async_set("lock.front", "locked", {"friendly_name": "Front lock"})
    hass.states.async_set(
        "binary_sensor.front_door", "off", {"friendly_name": "Front door", "device_class": "door"}
    )
    hass.states.async_set(
        "binary_sensor.hall_motion",
        "on",
        {"friendly_name": "Hall motion", "device_class": "motion"},
    )
    hass.states.async_set(
        "sensor.salon_temperature",
        "21.5",
        {"friendly_name": "Salon temperature", "unit_of_measurement": "\u00b0C",
         "device_class": "temperature"},
    )
    hass.states.async_set(
        "input_select.mode", "Evening", {"friendly_name": "Mode",
                                         "options": ["Morning", "Evening", "Night"]}
    )
    hass.states.async_set(
        "climate.salon", "heat", {"friendly_name": "Heating", "hvac_modes": ["off", "heat", "auto"]}
    )
    hass.states.async_set(
        "input_number.target",
        "250",
        {"friendly_name": "Target", "min": 0, "max": 1000, "step": 1, "unit_of_measurement": "W"},
    )
    hass.states.async_set(
        "media_player.salon", "playing", {"friendly_name": "Salon speaker", "volume_level": 0.35,
                                          "supported_features": 4}
    )
    hass.states.async_set(
        "input_text.note", "Buy milk", {"friendly_name": "Note", "max": 30, "mode": "text"}
    )
    hass.states.async_set(
        "input_text.code", "1234", {"friendly_name": "Code", "max": 10, "mode": "password"}
    )
    hass.states.async_set("script.movie", "off", {"friendly_name": "Movie night"})
    hass.states.async_set("switch.kettle", "unavailable", {"friendly_name": "Kettle"})


FULL_MENU = [
    group(
        "g_salon",
        "Salon",
        [
            row("r_lamp", "light.salon", "toggle"),
            row("r_dim", "light.salon", "slider"),
            row("r_blind", "cover.salon", "slider", icon="blinds"),
            row("r_blind_toggle", "cover.salon", "toggle"),
            row("r_mode", "input_select.mode", "selector"),
            row("r_heat", "climate.salon", "selector"),
            row("r_volume", "media_player.salon", "slider"),
            row("r_temp", "sensor.salon_temperature", "indicator"),
        ],
    ),
    row("r_movie", "script.movie", "button", label="Movie night!"),
    row("r_door", "binary_sensor.front_door", "indicator"),
    row("r_motion", "binary_sensor.hall_motion", "indicator"),
    row("r_lock", "lock.front", "toggle"),
    row("r_target", "input_number.target", "slider"),
    row("r_note", "input_text.note", "input"),
    row("r_code", "input_text.code", "input"),
    row("r_kettle", "switch.kettle", "toggle"),
    row("r_gone", "light.removed", "toggle"),
]


async def _published(
    hass: HomeAssistant,
    mqtt_mock: Any,
    retained: dict[str, str | bytes],
    menu: list[dict[str, Any]] | None,
) -> tuple[MockConfigEntry, dict[str, Any] | None]:
    _states(hass)
    claim_menu(retained)
    entry = menu_entry(menu)
    await async_setup_box(hass, entry)
    return entry, last_menu(mqtt_mock)


async def test_the_menu_is_published_retained_with_every_binding(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """One retained QoS 1 payload in the contract's shape, every row read from its entity."""
    _entry, menu = await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)

    assert menu is not None
    assert menu["v"] == 1
    assert isinstance(menu["rev"], int) and menu["rev"] >= 1
    assert menu["title"] == "Home Assistant"
    salon = menu["items"][0]
    assert {k: salon[k] for k in ("type", "id", "label", "icon")} == {
        "type": "group", "id": "g_salon", "label": "Salon", "icon": "sofa",
    }
    rows = items_by_id(menu)

    assert rows["r_lamp"] == {
        "type": "toggle", "id": "r_lamp", "label": "Salon lamp", "icon": "lamp",
        "value": True, "state": "On", "color": "green", "available": True,
    }
    assert rows["r_dim"] == {
        "type": "slider", "id": "r_dim", "label": "Salon lamp", "icon": "lamp",
        "value": 50, "min": 0, "max": 100, "step": 10, "unit": "%", "state": "50 %",
        "available": True,
    }
    assert rows["r_blind"]["value"] == 40 and rows["r_blind"]["icon"] == "blinds"
    assert rows["r_blind_toggle"]["value"] is True
    assert rows["r_blind_toggle"]["color"] == "green"
    assert rows["r_mode"] == {
        "type": "selector", "id": "r_mode", "label": "Mode", "icon": "neutral",
        "value": "Evening", "options": ["Morning", "Evening", "Night"], "state": "Evening",
        "available": True,
    }
    # The household's words for the climate's modes, not the entity's own.
    assert rows["r_heat"]["options"] == ["Off", "Heat", "Auto"]
    assert rows["r_heat"]["value"] == "Heat"
    assert rows["r_heat"]["icon"] == "radiator"
    assert rows["r_volume"]["value"] == 35
    assert rows["r_temp"]["state"] == "21.5 \u00b0C"
    assert rows["r_temp"]["value"] is None
    assert rows["r_temp"]["color"] is None
    assert rows["r_temp"]["icon"] == "thermometer"
    assert rows["r_movie"] == {
        "type": "button", "id": "r_movie", "label": "Movie night!", "icon": "script",
        "state": None, "available": True,
    }
    # A closed door is the good state of a door, as the contract's example shows.
    assert rows["r_door"]["value"] is False
    assert rows["r_door"]["color"] == "green"
    assert rows["r_door"]["state"] == "Closed"
    assert rows["r_motion"]["value"] is True
    assert rows["r_motion"]["color"] == "green"
    assert rows["r_lock"]["value"] is True
    assert rows["r_lock"]["color"] == "green"
    assert rows["r_lock"]["state"] == "Locked"
    # 1000 steps of 1 coarsened to 200 steps of 5.
    assert {k: rows["r_target"][k] for k in ("value", "min", "max", "step", "unit")} == {
        "value": 250, "min": 0, "max": 1000, "step": 5, "unit": "W",
    }
    assert {k: rows["r_note"][k] for k in ("value", "max_length", "state")} == {
        "value": "Buy milk", "max_length": 30, "state": "Buy milk",
    }
    # A password field's text never goes on the broker.
    assert rows["r_code"]["value"] is None
    assert rows["r_code"]["state"] is None
    assert "1234" not in json.dumps(menu)
    assert rows["r_kettle"]["available"] is False
    assert rows["r_kettle"]["value"] is None
    assert rows["r_kettle"]["state"] == "Unavailable"
    assert rows["r_gone"]["available"] is False
    assert rows["r_gone"]["label"] == "light.removed"


@pytest.mark.parametrize(
    ("entity_id", "control"),
    [
        ("input_text.pin", "indicator"),
        ("input_text.pin", "input"),
        ("text.pin", "indicator"),
        ("text.pin", "input"),
    ],
)
async def test_a_password_field_never_has_its_text_published(
    hass: HomeAssistant, entity_id: str, control: str
) -> None:
    """Shown as an indicator it is read like any other state; its text still stays home."""
    hass.states.async_set(entity_id, "4711", {"friendly_name": "PIN", "mode": "password"})
    menu = stored_menu({"menu": [row("r_pin", entity_id, control)]})

    items, _views, _notes = build_menu(hass, menu, TEXTS)

    assert items[0]["available"] is True
    assert items[0]["state"] is None
    assert items[0]["value"] is None
    assert "4711" not in json.dumps(items)


async def test_an_empty_menu_is_the_empty_payload(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """No menu defined: the topic is emptied, which the receiver reads as "no menu"."""
    _entry, menu = await _published(hass, mqtt_mock, box_on_the_broker, None)

    assert menu is None
    assert menu_publishes(mqtt_mock)[-1] == ("", 1, True)


async def test_labels_states_options_and_units_are_cut(hass: HomeAssistant) -> None:
    """Every text is cut by the sender at the contract's length."""
    hass.states.async_set(
        "input_select.long",
        "x" * 80,
        {"friendly_name": "L" * 90, "options": [f"{n:02d}" + "o" * 70 for n in range(30)]
         + ["x" * 80]},
    )
    hass.states.async_set(
        "input_number.long",
        "3",
        {"friendly_name": "n", "min": 0, "max": 10, "step": 1,
         "unit_of_measurement": "kilowatthours"},
    )
    hass.states.async_set("sensor.long", "s" * 40, {"friendly_name": "s"})
    menu = stored_menu(
        {
            "menu": [
                row("r_sel", "input_select.long", "selector"),
                row("r_num", "input_number.long", "slider"),
                row("r_sen", "sensor.long", "indicator"),
            ]
        }
    )

    items, views, notes = build_menu(hass, menu, TEXTS)

    selector = items[0]
    assert len(selector["label"]) == 60
    assert len(selector["options"]) == 24
    assert all(len(option) == 60 for option in selector["options"])
    assert len(selector["state"]) == 24
    # The current option is past the 24th, so there is no value to show.
    assert selector["value"] is None
    assert any("option" in note for note in notes)
    assert items[1]["unit"] == "kilowatt"
    assert len(items[2]["state"]) == 24
    assert views["r_sel"].options[selector["options"][0]] == "00" + "o" * 70


async def test_options_that_read_the_same_once_cut_keep_the_entitys_words(
    hass: HomeAssistant,
) -> None:
    """Two options cut to the same text: the second is dropped, never sent twice."""
    hass.states.async_set(
        "input_select.twins", "a", {"options": ["p" * 61 + "a", "p" * 61 + "b", "a"]}
    )
    menu = stored_menu({"menu": [row("r", "input_select.twins", "selector")]})

    items, views, notes = build_menu(hass, menu, TEXTS)

    assert items[0]["options"] == ["p" * 60, "a"]
    assert items[0]["value"] == "a"
    assert notes


async def test_at_most_eight_groups_and_twenty_four_entries(hass: HomeAssistant) -> None:
    """What does not fit the contract's bounds is left out, from the end, with a log line."""
    menu = stored_menu(
        {
            "menu": [
                group(f"g_{n}", f"G{n}", [row(f"r_{n}_{m}", "script.x", "button")
                                          for m in range(30)])
                for n in range(10)
            ]
            + [row(f"r_top_{n}", "script.x", "button") for n in range(20)]
        }
    )

    items, _views, notes = build_menu(hass, menu, TEXTS)

    groups = [item for item in items if item["type"] == "group"]
    assert len(groups) == 8
    assert all(len(item["rows"]) == 24 for item in groups)
    assert len(items) == 24
    assert [item["id"] for item in items[8:]] == [f"r_top_{n}" for n in range(16)]
    assert len(notes) >= 3


def test_the_payload_stays_under_64_kib() -> None:
    """The longest rows, too many of them: dropped from the end until the payload fits."""
    long_row = {
        "type": "selector", "id": "r", "label": "\u0142" * 60, "icon": "neutral",
        "value": None, "options": ["\u0142" * 60] * 24, "state": "", "available": True,
    }
    payload = {
        "v": 1, "rev": 1, "title": "Home Assistant",
        "items": [
            {"type": "group", "id": f"g{n}", "label": "x", "icon": "home",
             "rows": [dict(long_row, id=f"r{n}_{m}") for m in range(24)]}
            for n in range(8)
        ],
    }

    encoded, dropped = fit_menu(payload)

    assert len(encoded) < MAX_PAYLOAD_BYTES
    assert dropped > 0
    assert encoded == encode_menu(payload)
    # What was put first survives.
    assert payload["items"][0]["rows"][0]["id"] == "r0_0"


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ((0, 100, 1), (0, 100, 1)),
        ((0, 1000, 1), (0, 1000, 5)),
        ((0, 201, 1), (0, 201, 2)),
        ((10, 30, 0.5), (10, 30, 0.5)),
        ((0, 100, 0), None),
        ((5, 5, 1), None),
        (("a", 5, 1), None),
        ((0, float("inf"), 1), None),
    ],
)
def test_a_slider_is_coarsened_to_at_most_200_steps(bounds: tuple, expected: Any) -> None:
    """The step grows by a whole multiple, so every value is still one the entity takes."""
    assert coarsen(*bounds) == expected


def test_a_value_snaps_to_the_grid() -> None:
    assert snap(57, (0, 100, 10)) == 60
    assert snap(54, (0, 100, 10)) == 50
    assert snap(10.3, (10, 30, 0.5)) == 10.5
    assert snap(100, (0, 100, 10)) == 100


def test_the_stored_menu_drops_what_it_cannot_read() -> None:
    """A malformed or duplicated item is left out, never allowed to answer for another."""
    menu = stored_menu(
        {
            "menu": [
                row("r_ok", "light.a", "toggle"),
                row("r_ok", "light.b", "toggle"),
                row("bad id!", "light.c", "toggle"),
                row("r_kind", "light.d", "input"),
                row("r_nodomain", "nothing", "toggle"),
                group("g_empty_label", "  ", []),
                group("g_ok", "Ok", [row("r_ok", "light.e", "toggle"),
                                     row("r_in", "light.f", "toggle", icon="nope")], icon="x"),
                "not a dict",
            ]
        }
    )

    assert [item["id"] for item in menu] == ["r_ok", "g_ok"]
    assert [r["id"] for r in menu[1]["rows"]] == ["r_in"]
    assert menu[1]["icon"] == "neutral"
    assert "icon" not in menu[1]["rows"][0]
    assert stored_menu({"menu": "nope"}) == []


async def test_ids_are_stable_across_a_reload_and_rev_grows(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """The receiver may keep a row selected by its id; a reload must not renumber it."""
    entry, first = await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    second = last_menu(mqtt_mock)

    assert first is not None and second is not None
    assert list(items_by_id(first)) == list(items_by_id(second))
    assert second["rev"] > first["rev"]


async def test_a_state_change_republishes_debounced(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """The first change goes out at once, a burst after it as one publish half a second on."""
    await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)
    now = dt_util.utcnow()
    async_fire_time_changed(hass, now + timedelta(seconds=2))
    await hass.async_block_till_done()
    before = len(menu_publishes(mqtt_mock))

    hass.states.async_set("light.salon", "off", {"friendly_name": "Salon lamp"})
    await hass.async_block_till_done()
    assert len(menu_publishes(mqtt_mock)) == before + 1
    assert items_by_id(last_menu(mqtt_mock))["r_lamp"]["value"] is False

    hass.states.async_set("light.salon", "on", {"friendly_name": "Salon lamp", "brightness": 255})
    await hass.async_block_till_done()
    hass.states.async_set("lock.front", "unlocked", {"friendly_name": "Front lock"})
    await hass.async_block_till_done()
    # Inside the cooldown: held back.
    assert len(menu_publishes(mqtt_mock)) == before + 1

    async_fire_time_changed(hass, now + timedelta(seconds=2.6))
    await hass.async_block_till_done()
    assert len(menu_publishes(mqtt_mock)) == before + 2
    rows = items_by_id(last_menu(mqtt_mock))
    assert rows["r_lamp"]["value"] is True
    assert rows["r_lock"]["color"] == "red"


async def test_an_attribute_nobody_shows_does_not_republish(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """A player's position moves every second; the menu does not."""
    await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    before = len(menu_publishes(mqtt_mock))

    hass.states.async_set(
        "media_player.salon",
        "playing",
        {"friendly_name": "Salon speaker", "volume_level": 0.35, "supported_features": 4,
         "media_position": 12},
    )
    await hass.async_block_till_done()

    assert len(menu_publishes(mqtt_mock)) == before


async def test_the_receiver_coming_back_gets_the_menu_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """Published again when the receiver returns, even though nothing in it changed."""
    await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)
    now = dt_util.utcnow()
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    async_fire_time_changed(hass, now + timedelta(seconds=2))
    await hass.async_block_till_done()
    before = len(menu_publishes(mqtt_mock))

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "online")
    await hass.async_block_till_done()

    assert len(menu_publishes(mqtt_mock)) == before + 1


async def test_unloading_keeps_and_removing_retracts_the_menu(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """A reload must not blank the television; a removed receiver's menu goes with it."""
    entry, _menu = await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)
    before = len(menu_publishes(mqtt_mock))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(menu_publishes(mqtt_mock)) == before
    # Nothing is followed any more.
    hass.states.async_set("light.salon", "off")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert len(menu_publishes(mqtt_mock)) == before

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert menu_publishes(mqtt_mock)[-1] == ("", 1, True)


async def test_a_menu_that_cannot_be_published_is_tried_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed publish is not remembered as published: the next change sends it."""
    from homeassistant.exceptions import HomeAssistantError

    entry, _menu = await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)
    menu = entry.runtime_data.menu_action_handler.__self__
    caplog.set_level(logging.DEBUG, logger="custom_components.enigma2_mqtt.menu")
    mqtt_mock.async_publish.side_effect = HomeAssistantError("broker gone")
    hass.states.async_set("lock.front", "unlocked", {"friendly_name": "Front lock"})
    await menu._async_publish()
    mqtt_mock.async_publish.side_effect = None
    before = len(menu_publishes(mqtt_mock))

    # The same menu again: it never arrived, so it is not "already published".
    await menu._async_publish()

    assert "Could not publish the menu" in caplog.text
    assert len(menu_publishes(mqtt_mock)) == before + 1


async def test_a_menu_that_had_to_leave_something_out_says_so_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One warning per thing left out, not one per publish."""
    long_options = [f"{n:03d}" + "\u0142" * 57 for n in range(24)]
    for n in range(200):
        hass.states.async_set(f"input_select.s{n}", long_options[0], {"options": long_options})
    menu = [
        group(f"g_{n}", f"G{n}", [row(f"r_{n}_{m}", f"input_select.s{n * 24 + m}", "selector")
                                  for m in range(24) if n * 24 + m < 200])
        for n in range(8)
    ]
    claim_menu(box_on_the_broker)
    await async_setup_box(hass, menu_entry(menu))
    published = last_menu(mqtt_mock)

    assert published is not None
    payload, _qos, _retain = menu_publishes(mqtt_mock)[-1]
    assert len(payload.encode("utf-8")) < MAX_PAYLOAD_BYTES
    assert caplog.text.count("to stay under 64 KiB") == 1
    assert MENU_TOPIC



def test_the_stored_menu_reads_both_forms_of_a_binding() -> None:
    """A registry id or an entity id; a registry id cannot say its domain, so any control."""
    menu = stored_menu(
        {
            "menu": [
                row("r_reg", "0123456789abcdef0123456789abcdef", "slider"),
                row("r_ent", "person.anna", "indicator"),
                row("r_bad_reg", "0123456789ABCDEF0123456789abcdef", "toggle"),
                row("r_bad_ctl", "0123456789abcdef0123456789abcdef", "dial"),
                row("r_person_toggle", "person.anna", "toggle"),
                row("r_script_indicator", "script.movie", "indicator"),
            ]
        }
    )

    assert [item["id"] for item in menu] == ["r_reg", "r_ent"]


async def test_any_entity_is_shown_as_an_indicator_with_its_state(hass: HomeAssistant) -> None:
    """A domain the menu cannot act on: its state in the household's words, no colour."""
    hass.states.async_set("person.anna", "not_home", {"friendly_name": "Anna"})
    hass.states.async_set("alarm_control_panel.house", "armed_away", {"friendly_name": "Alarm"})
    menu = stored_menu(
        {
            "menu": [
                row("r_anna", "person.anna", "indicator"),
                row("r_alarm", "alarm_control_panel.house", "indicator"),
            ]
        }
    )
    texts = await async_menu_texts(hass, menu)

    items, _views, _notes = build_menu(hass, menu, texts)

    assert items[0]["state"] == "Away"
    assert items[0]["icon"] == "home"
    assert items[0]["value"] is None and items[0]["color"] is None
    assert items[1]["state"] == "Armed away"
    assert items[1]["icon"] == "alarm"


async def test_a_rename_republishes_and_is_followed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """Without anybody opening the options, the row follows the entity to its new id."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entity = registry.async_get_or_create("light", "demo", "l1", suggested_object_id="desk")
    hass.states.async_set("light.desk", "on", {"friendly_name": "Desk"})
    claim_menu(box_on_the_broker)
    await async_setup_box(hass, menu_entry([row("r_desk", entity.id, "toggle")]))
    assert items_by_id(last_menu(mqtt_mock))["r_desk"]["label"] == "Desk"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    registry.async_update_entity("light.desk", new_entity_id="light.study")
    hass.states.async_set("light.study", "off", {"friendly_name": "Study"})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()

    shown = items_by_id(last_menu(mqtt_mock))["r_desk"]
    assert shown["label"] == "Study"
    assert shown["value"] is False


# Binary sensor classes whose "on" needs looking at - open, wet, burning, low.
ATTENTION = [
    "battery",
    "carbon_monoxide",
    "cold",
    "door",
    "garage_door",
    "gas",
    "heat",
    "lock",
    "moisture",
    "opening",
    "problem",
    "safety",
    "smoke",
    "sound",
    "tamper",
    "update",
    "vibration",
    "window",
]


@pytest.mark.parametrize(
    ("device_class", "state", "color"),
    [
        *[(device_class, "off", "green") for device_class in ATTENTION],
        *[(device_class, "on", "red") for device_class in ATTENTION],
        ("motion", "on", "green"),
        ("motion", "off", "red"),
        ("connectivity", "on", "green"),
        ("running", "off", "red"),
        (None, "on", "green"),
    ],
)
async def test_an_indicator_is_green_while_all_is_good(
    hass: HomeAssistant, device_class: str | None, state: str, color: str
) -> None:
    """A door is good closed and a smoke detector quiet; a link or a motion is good on."""
    attributes = {"device_class": device_class} if device_class else {}
    hass.states.async_set("binary_sensor.probe", state, attributes)
    menu = stored_menu({"menu": [row("r_probe", "binary_sensor.probe", "indicator")]})

    items, _views, _notes = build_menu(hass, menu, TEXTS)

    assert items[0]["color"] == color


async def test_only_a_binary_sensor_reads_its_class_as_needing_attention(
    hass: HomeAssistant,
) -> None:
    """A cover that is a door is opened on purpose: open is its "on", and green."""
    hass.states.async_set(
        "cover.garage", "open", {"device_class": "door", "current_position": 100,
                                 "supported_features": 15}
    )
    menu = stored_menu(
        {
            "menu": [
                row("r_toggle", "cover.garage", "toggle"),
                row("r_indicator", "cover.garage", "indicator"),
            ]
        }
    )

    items, _views, _notes = build_menu(hass, menu, TEXTS)

    assert [(item["value"], item["color"]) for item in items] == [
        (True, "green"), (True, "green"),
    ]


@pytest.mark.parametrize("domain", ["script", "scene", "button", "input_button", "automation"])
def test_what_the_menu_runs_is_only_ever_a_button(domain: str) -> None:
    """Its state is whether it runs or when it last ran - nothing to show as an indicator."""
    menu = stored_menu(
        {
            "menu": [
                row("r_button", f"{domain}.x", "button"),
                row("r_indicator", f"{domain}.x", "indicator"),
            ]
        }
    )

    assert [item["id"] for item in menu] == ["r_button"]


async def test_a_registered_entity_without_a_state_is_named_by_its_entity_id(
    hass: HomeAssistant,
) -> None:
    """Never by its registry id, which means nothing to anybody at the television."""
    entity = er.async_get(hass).async_get_or_create(
        "switch", "demo", "k1", suggested_object_id="kettle"
    )
    menu = stored_menu({"menu": [row("r_kettle", entity.id, "toggle")]})

    items, _views, _notes = build_menu(hass, menu, TEXTS)

    assert items[0]["label"] == "switch.kettle"
    assert items[0]["available"] is False


async def test_a_row_bound_by_registry_id_shows_its_state_in_the_households_words(
    hass: HomeAssistant,
) -> None:
    """The translations are loaded for the entity the registry id names."""
    entity = er.async_get(hass).async_get_or_create(
        "binary_sensor", "demo", "d1", suggested_object_id="front_door"
    )
    hass.states.async_set(entity.entity_id, "off", {"device_class": "door"})
    menu = stored_menu({"menu": [row("r_door", entity.id, "indicator")]})
    texts = await async_menu_texts(hass, menu)

    items, _views, _notes = build_menu(hass, menu, texts)

    assert items[0]["state"] == "Closed"


async def test_a_rename_alone_republishes(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """No state change needed: the row names the entity's new id as soon as it has one."""
    registry = er.async_get(hass)
    entity = registry.async_get_or_create("light", "demo", "l1", suggested_object_id="desk")
    hass.states.async_set("light.desk", "on", {"friendly_name": "Desk"})
    claim_menu(box_on_the_broker)
    await async_setup_box(hass, menu_entry([row("r_desk", entity.id, "toggle")]))
    assert items_by_id(last_menu(mqtt_mock))["r_desk"]["label"] == "Desk"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    registry.async_update_entity("light.desk", new_entity_id="light.study")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()

    shown = items_by_id(last_menu(mqtt_mock))["r_desk"]
    assert shown["label"] == "light.study"
    assert shown["available"] is False


def _slowed(menu: Any) -> asyncio.Event:
    """Hold the menu's next translation load until the event is set."""
    release = asyncio.Event()
    load = menu._async_texts

    async def _slow(rows: Any) -> dict[str, str]:
        await release.wait()
        return await load(rows)

    menu._async_texts = _slow
    return release


@pytest.mark.parametrize("then", ["remove", "reload"])
async def test_a_publish_under_way_when_the_entry_stops_publishes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    then: str,
) -> None:
    """Never the full menu after the retraction, nor an older run's after the next one's."""
    entry, _menu = await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)
    menu = entry.runtime_data.menu_action_handler.__self__
    release = _slowed(menu)
    menu._force = True
    under_way = asyncio.ensure_future(menu._async_publish())
    await asyncio.sleep(0)

    if then == "remove":
        assert await hass.config_entries.async_remove(entry.entry_id)
    else:
        assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    before = menu_publishes(mqtt_mock)

    release.set()
    await under_way
    await hass.async_block_till_done()

    assert menu_publishes(mqtt_mock) == before
    if then == "remove":
        assert before[-1] == ("", 1, True)


def _button(row_id: str, label: str = "") -> dict[str, Any]:
    return {"type": "button", "id": row_id, "label": label, "icon": "neutral",
            "state": None, "available": True}


def _sized(size: int, rev: int) -> dict[str, Any]:
    """A payload of exactly `size` bytes at this rev: a long first row and a short last one."""
    payload = {"v": 1, "rev": rev, "title": "Home Assistant",
               "items": [_button("r_long"), _button("r_last")]}
    pad = size - len(encode_menu(payload))
    payload["items"][0]["label"] = "x" * pad
    assert len(encode_menu(payload)) == size
    return payload


@pytest.mark.parametrize(("size", "dropped"), [(MAX_PAYLOAD_BYTES - 1, 0), (MAX_PAYLOAD_BYTES, 1)])
def test_the_payload_bound_is_exact(size: int, dropped: int) -> None:
    """65535 bytes is under 64 KiB and goes as it is; 65536 is not."""
    payload = _sized(size, 123456)

    encoded, count = fit_menu(payload)

    assert count == dropped
    assert len(encoded) == (size if not dropped else len(encode_menu(payload)))
    assert len(encoded) < MAX_PAYLOAD_BYTES


async def test_the_menu_is_fitted_with_the_rev_it_goes_out_with(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """Under 64 KiB at rev 0 is not under 64 KiB at rev 123456; a row left out is not acted on."""
    hass.states.async_set("script.long", "off")
    hass.states.async_set("script.last", "off")
    pressed = async_mock_service(hass, "script", "turn_on")
    hass.data["enigma2_mqtt_menu_rev"] = {NODE_ID: 123455}
    # 65535 bytes at rev 0, five more at rev 123456.
    items = _sized(MAX_PAYLOAD_BYTES - 1, 0)["items"]
    claim_menu(box_on_the_broker)
    entry = menu_entry([row("r_long", "script.long", "button"),
                        row("r_last", "script.last", "button")])
    with patch(
        "custom_components.enigma2_mqtt.menu.build_menu",
        side_effect=lambda *_args: (copy.deepcopy(items), {}, []),
    ):
        await async_setup_box(hass, entry)
        payload, _qos, _retain = menu_publishes(mqtt_mock)[-1]

    assert len(payload.encode("utf-8")) < MAX_PAYLOAD_BYTES
    published = json.loads(payload)
    assert published["rev"] == 123456
    assert list(items_by_id(published)) == ["r_long"]

    fire_action(hass, "r_last", "press", action_id="000000000001")
    await hass.async_block_till_done()
    assert pressed == []
    fire_action(hass, "r_long", "press", action_id="000000000002")
    await hass.async_block_till_done()
    assert len(pressed) == 1


async def test_a_row_past_the_contracts_bounds_is_not_acted_on(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 25th entry of the top list is not on the receiver; an action naming it is refused."""
    hass.states.async_set("script.x", "off")
    pressed = async_mock_service(hass, "script", "turn_on")
    claim_menu(box_on_the_broker)
    await async_setup_box(
        hass, menu_entry([row(f"r_{n}", "script.x", "button") for n in range(25)])
    )
    assert "r_24" not in items_by_id(last_menu(mqtt_mock))

    fire_action(hass, "r_24", "press", action_id="000000000001")
    await hass.async_block_till_done()
    assert pressed == []
    assert "(unknown_row: r_24 (left out of the menu))" in caplog.text

    fire_action(hass, "r_23", "press", action_id="000000000002")
    await hass.async_block_till_done()
    assert len(pressed) == 1


async def test_the_broker_connection_coming_back_gets_the_menu_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """A broker restarted without persistence lost the retained menu; nothing else changed."""
    await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    before = menu_publishes(mqtt_mock)

    async_dispatcher_send(hass, MQTT_CONNECTION_STATE, False)
    await hass.async_block_till_done()
    assert menu_publishes(mqtt_mock) == before

    async_dispatcher_send(hass, MQTT_CONNECTION_STATE, True)
    await hass.async_block_till_done()
    after = menu_publishes(mqtt_mock)
    assert len(after) == len(before) + 1
    assert json.loads(after[-1][0])["items"] == json.loads(before[-1][0])["items"]


async def test_removing_retracts_the_menu_even_when_the_first_messages_fail(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The retraction does not hang on the integration topic or `ha_mode` going out."""
    from homeassistant.exceptions import HomeAssistantError

    entry, _menu = await _published(hass, mqtt_mock, box_on_the_broker, FULL_MENU)

    def _fail_but_the_menu(topic: str, *_args: Any, **_kwargs: Any) -> None:
        if topic != MENU_TOPIC:
            raise HomeAssistantError("broker said no")

    mqtt_mock.async_publish.side_effect = _fail_but_the_menu
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    topics = [call.args[0] for call in mqtt_mock.async_publish.call_args_list]
    assert f"enigma2mqtt/integration/{NODE_ID}" in topics
    assert menu_publishes(mqtt_mock)[-1] == ("", 1, True)
    assert "Could not tell" in caplog.text
    assert "Could not retract" not in caplog.text

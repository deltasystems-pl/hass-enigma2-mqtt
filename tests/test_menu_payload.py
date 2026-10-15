"""The receiver menu as Home Assistant publishes it: shape, bounds, ids and when it goes out."""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.enigma2_mqtt.menu import (
    MAX_PAYLOAD_BYTES,
    build_menu,
    coarsen,
    encode_menu,
    fit_menu,
    snap,
    stored_menu,
)

from .conftest import AVAILABILITY_TOPIC, async_setup_box
from .menu_common import (
    MENU_TOPIC,
    claim_menu,
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

"""The options flow's receiver menu: building, changing and saving the menu."""

from __future__ import annotations

from datetime import timedelta
import json
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_BOUQUETS,
    CONF_DANGEROUS_BUTTONS,
    CONF_MENU,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_WOL_MAC,
    MENU_ICONS,
)

from .conftest import async_arm_ha_mode_ack, async_setup_box
from .menu_common import (
    MENU_INFO,
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

ROW_ID = re.compile(r"^r_[0-9a-f]{10}$")
GROUP_ID = re.compile(r"^g_[0-9a-f]{10}$")


@pytest.fixture
async def entry(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> MockConfigEntry:
    """A receiver that claims `menu`, with no menu yet and a few entities to bind."""
    hass.states.async_set(
        "light.salon",
        "on",
        {"friendly_name": "Salon lamp", "brightness": 128, "supported_color_modes": ["brightness"]},
    )
    hass.states.async_set(
        "light.hall", "off", {"friendly_name": "Hall", "supported_color_modes": ["onoff"]}
    )
    hass.states.async_set("script.movie", "off", {"friendly_name": "Movie night"})
    hass.states.async_set("switch.kettle", "off", {"friendly_name": "Kettle"})
    claim_menu(box_on_the_broker)
    config_entry = menu_entry(None)
    await async_setup_box(hass, config_entry)
    return config_entry


async def _open_menu(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "receiver_menu" in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "receiver_menu"}
    )


async def _step(hass: HomeAssistant, result: dict[str, Any], step: str) -> dict[str, Any]:
    assert result["type"] is FlowResultType.MENU, result
    assert step in result["menu_options"], result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def _form(hass: HomeAssistant, result: dict[str, Any], data: dict[str, Any]) -> Any:
    assert result["type"] is FlowResultType.FORM, result
    return await hass.config_entries.options.async_configure(result["flow_id"], data)


async def _save(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    result = await _step(hass, result, "menu_save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result


async def test_the_menu_is_offered_only_where_it_can_be_shown(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """A receiver without the capability opens straight on the options, as before."""
    config_entry = menu_entry(None)
    await async_setup_box(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"


async def test_a_stored_menu_is_offered_even_without_the_capability(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """So that a menu can be emptied after the plugin was downgraded."""
    config_entry = menu_entry([row("r_a", "script.movie", "button")])
    await async_setup_box(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "receiver_menu"]


async def test_building_a_menu_from_nothing(
    hass: HomeAssistant, mqtt_mock, entry: MockConfigEntry
) -> None:
    """A group, a row in it with a chosen control, a row at the top; saved and published."""
    result = await _open_menu(hass, entry)
    assert result["step_id"] == "receiver_menu"
    assert result["menu_options"] == [
        "menu_add_group", "menu_add_row", "menu_remote_edit", "menu_save"
    ]
    assert result["description_placeholders"]["menu"] == "The menu is empty."

    result = await _step(hass, result, "menu_add_group")
    assert result["step_id"] == "menu_group"
    icons = result["data_schema"].schema["icon"].config["options"]
    assert icons == list(MENU_ICONS)
    result = await _form(hass, result, {"label": "  Salon  ", "icon": "sofa"})
    assert result["step_id"] == "receiver_menu"

    result = await _step(hass, result, "menu_add_row")
    assert result["step_id"] == "menu_row"
    places = result["data_schema"].schema["place"].config["options"]
    assert places[0] == {"value": "top", "label": "Top list"}
    assert places[1]["label"] == "Salon"
    group_id = places[1]["value"]
    assert GROUP_ID.match(group_id)
    result = await _form(hass, result, {"entity_id": "light.salon", "place": group_id})
    # A light can be a switch, a brightness slider or an indicator.
    assert result["step_id"] == "menu_row_control"
    assert result["data_schema"].schema["control"].config["options"] == [
        "toggle", "slider", "indicator",
    ]
    result = await _form(hass, result, {"control": "slider"})

    result = await _step(hass, result, "menu_add_row")
    # A script is only ever a button: no second question.
    result = await _form(
        hass,
        result,
        {"entity_id": "script.movie", "place": "top", "label": "Film", "icon": "movie"},
    )
    assert result["step_id"] == "receiver_menu"
    assert "Salon lamp - light.salon (Slider)" in result["description_placeholders"]["menu"]

    await _save(hass, result)

    stored = entry.options[CONF_MENU]
    assert GROUP_ID.match(stored[0]["id"])
    assert stored[0]["label"] == "Salon"
    assert stored[0]["icon"] == "sofa"
    assert ROW_ID.match(stored[0]["rows"][0]["id"])
    assert stored[0]["rows"][0] == {
        "type": "row", "id": stored[0]["rows"][0]["id"], "entity_id": "light.salon",
        "control": "slider",
    }
    assert stored[1] == {
        "type": "row", "id": stored[1]["id"], "entity_id": "script.movie",
        "control": "button", "label": "Film", "icon": "movie",
    }
    # Saving reloads the entry, and the reload publishes the menu.
    published = last_menu(mqtt_mock)
    assert published is not None
    rows = items_by_id(published)
    assert rows[stored[0]["rows"][0]["id"]]["type"] == "slider"
    assert rows[stored[1]["id"]]["label"] == "Film"


async def test_a_light_that_is_only_on_or_off_has_no_slider(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result = await _open_menu(hass, entry)
    result = await _step(hass, result, "menu_add_row")
    result = await _form(hass, result, {"entity_id": "light.hall", "place": "top"})

    assert result["data_schema"].schema["control"].config["options"] == ["toggle", "indicator"]


async def test_a_group_needs_a_name(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await _open_menu(hass, entry)
    result = await _step(hass, result, "menu_add_group")
    result = await _form(hass, result, {"label": "   ", "icon": "home"})

    assert result["errors"] == {"label": "menu_label_required"}


async def test_an_entity_that_cannot_be_found_is_refused(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The picker may answer with a registry id; one that names nothing is an error."""
    result = await _open_menu(hass, entry)
    result = await _step(hass, result, "menu_add_row")
    result = await _form(
        hass, result, {"entity_id": "0123456789abcdef0123456789abcdef", "place": "top"}
    )

    assert result["errors"] == {"entity_id": "menu_entity_not_supported"}


async def test_eight_groups_and_twenty_four_entries_at_most(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """Adding is not offered where nothing more fits, and a full group refuses a row."""
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    full_group = group("g_full", "Full", [row(f"r_{n}", "script.movie", "button")
                                           for n in range(24)])
    menu = [full_group] + [group(f"g_{n}", f"G{n}", []) for n in range(7)]
    config_entry = menu_entry(menu)
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)
    assert "menu_add_group" not in result["menu_options"]
    assert "menu_add_row" in result["menu_options"]

    result = await _step(hass, result, "menu_add_row")
    result = await _form(hass, result, {"entity_id": "script.movie", "place": "g_full"})
    assert result["errors"] == {"place": "menu_place_full"}


async def test_a_full_top_list_offers_no_new_row_unless_a_group_has_room(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    claim_menu(box_on_the_broker)
    config_entry = menu_entry([row(f"r_{n}", "script.movie", "button") for n in range(24)])
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)

    assert result["menu_options"] == [
        "menu_edit_row", "menu_move", "menu_remove", "menu_remote_edit", "menu_save",
    ]


async def test_changing_a_label_keeps_the_id_and_a_new_binding_gets_a_new_one(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """The receiver may hold an action for the old binding; it must not reach the new one."""
    hass.states.async_set("light.salon", "on", {"supported_color_modes": ["brightness"]})
    hass.states.async_set("switch.kettle", "off")
    claim_menu(box_on_the_broker)
    config_entry = menu_entry(
        [row("r_lamp", "light.salon", "toggle"), row("r_kettle", "switch.kettle", "toggle")]
    )
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)
    result = await _step(hass, result, "menu_edit_row")
    result = await _form(hass, result, {"row": "r_lamp"})
    assert result["step_id"] == "menu_row"
    # The form opens on what the row is now.
    suggested = {
        str(key): key.description.get("suggested_value")
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested["entity_id"] == "light.salon"
    assert suggested["place"] == "top"
    result = await _form(hass, result, {"entity_id": "light.salon", "place": "top",
                                        "label": "Lamp"})
    control_key = next(key for key in result["data_schema"].schema if key == "control")
    assert control_key.default() == "toggle"
    result = await _form(hass, result, {"control": "toggle"})

    result = await _step(hass, result, "menu_edit_row")
    result = await _form(hass, result, {"row": "r_kettle"})
    result = await _form(hass, result, {"entity_id": "switch.kettle", "place": "top"})
    result = await _form(hass, result, {"control": "indicator"})
    await _save(hass, result)

    stored = config_entry.options[CONF_MENU]
    assert stored[0] == {
        "type": "row", "id": "r_lamp", "entity_id": "light.salon", "control": "toggle",
        "label": "Lamp",
    }
    # Same place in the list, new id.
    assert stored[1]["entity_id"] == "switch.kettle"
    assert stored[1]["control"] == "indicator"
    assert ROW_ID.match(stored[1]["id"])


async def test_a_row_moves_between_the_top_list_and_a_group(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    config_entry = menu_entry(
        [group("g_salon", "Salon", [row("r_in", "script.movie", "button")]),
         row("r_top", "script.movie", "button")]
    )
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)
    result = await _step(hass, result, "menu_edit_row")
    result = await _form(hass, result, {"row": "r_top"})
    result = await _form(hass, result, {"entity_id": "script.movie", "place": "g_salon"})
    result = await _step(hass, result, "menu_edit_row")
    result = await _form(hass, result, {"row": "r_in"})
    result = await _form(hass, result, {"entity_id": "script.movie", "place": "top"})
    await _save(hass, result)

    stored = config_entry.options[CONF_MENU]
    assert [item["id"] for item in stored] == ["g_salon", "r_in"]
    assert [r["id"] for r in stored[0]["rows"]] == ["r_top"]


async def test_editing_a_group_and_removing_one_with_its_rows(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    config_entry = menu_entry(
        [group("g_salon", "Salon", [row("r_in", "script.movie", "button")]),
         group("g_hall", "Hall", [row("r_hall", "script.movie", "button")]),
         row("r_top", "script.movie", "button")]
    )
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)
    result = await _step(hass, result, "menu_edit_group")
    options = result["data_schema"].schema["group"].config["options"]
    assert [option["value"] for option in options] == ["g_salon", "g_hall"]
    result = await _form(hass, result, {"group": "g_hall"})
    result = await _form(hass, result, {"label": "Korytarz", "icon": "home"})

    result = await _step(hass, result, "menu_remove")
    choices = result["data_schema"].schema["remove"].config["options"]
    assert [choice["value"] for choice in choices] == [
        "g_salon", "r_in", "g_hall", "r_hall", "r_top",
    ]
    assert choices[1]["label"] == "Salon / movie"
    result = await _form(hass, result, {"remove": ["g_salon", "r_top"]})
    await _save(hass, result)

    assert config_entry.options[CONF_MENU] == [
        group("g_hall", "Korytarz", [row("r_hall", "script.movie", "button")], icon="home"),
    ]


async def test_emptying_the_menu_removes_it_from_the_options(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """And the reload publishes the empty payload: the receiver shows no menu."""
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    config_entry = menu_entry([row("r_top", "script.movie", "button")])
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)
    result = await _step(hass, result, "menu_remove")
    result = await _form(hass, result, {"remove": ["r_top"]})
    assert result["menu_options"] == [
        "menu_add_group", "menu_add_row", "menu_remote_edit", "menu_save"
    ]
    await _save(hass, result)

    assert CONF_MENU not in config_entry.options
    assert menu_publishes(mqtt_mock)[-1] == ("", 1, True)


async def test_closing_the_dialog_changes_nothing(
    hass: HomeAssistant, mqtt_mock, entry: MockConfigEntry
) -> None:
    result = await _open_menu(hass, entry)
    result = await _step(hass, result, "menu_add_group")
    result = await _form(hass, result, {"label": "Salon", "icon": "sofa"})
    hass.config_entries.options.async_abort(result["flow_id"])

    assert CONF_MENU not in entry.options


async def test_saving_the_other_options_keeps_the_menu(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """The settings form does not carry the menu, so it must not drop it either."""
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    menu = [row("r_top", "script.movie", "button")]
    config_entry = menu_entry(menu)
    await async_setup_box(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DANGEROUS_BUTTONS: True, CONF_WOL_MAC: "", CONF_BOUQUETS: []},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_MENU] == menu
    assert config_entry.options[CONF_DANGEROUS_BUTTONS] is True
    assert json.loads(menu_publishes(mqtt_mock)[-1][0])["items"][0]["id"] == "r_top"
    assert MENU_TOPIC


async def test_reconfiguring_to_a_new_node_id_retracts_the_old_nodes_menu(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
) -> None:
    """The old node's retained menu would stay on the broker for nobody."""
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    config_entry = menu_entry([row("r_top", "script.movie", "button")])
    await async_setup_box(hass, config_entry)
    box_on_the_broker["enigma2/vuuno4kse_0053ff/info"] = json.dumps(MENU_INFO)
    await async_arm_ha_mode_ack(hass, MENU_INFO, node_id="vuuno4kse_0053ff")

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_TOPIC: "enigma2",
            CONF_NODE_ID: "vuuno4kse_0053ff",
            CONF_NAME: "Dekoder salon",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert menu_publishes(mqtt_mock)[-1] == ("", 1, True)
    new_menu = [
        call.args[1]
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == "enigma2mqtt/menu/vuuno4kse_0053ff"
    ]
    assert json.loads(new_menu[-1])["items"][0]["id"] == "r_top"


async def test_moving_groups_and_controls_within_their_own_list(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """One place up or down; a group's rows move inside the group, an end stays an end."""
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    config_entry = menu_entry(
        [
            group("g_a", "A", [row("r_1", "script.movie", "button"),
                               row("r_2", "script.movie", "button")]),
            group("g_b", "B", []),
            row("r_top", "script.movie", "button"),
        ]
    )
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)
    for item, direction in (
        ("r_2", "up"),
        ("g_b", "up"),
        ("r_top", "down"),
        ("r_1", "down"),
        ("g_b", "up"),
    ):
        result = await _step(hass, result, "menu_move")
        assert result["step_id"] == "menu_move"
        choices = result["data_schema"].schema["item"].config["options"]
        assert {choice["value"] for choice in choices} == {"g_a", "r_1", "r_2", "g_b", "r_top"}
        result = await _form(hass, result, {"item": item, "direction": direction})
        assert result["step_id"] == "receiver_menu"
    await _save(hass, result)

    stored = config_entry.options[CONF_MENU]
    assert [item["id"] for item in stored] == ["g_b", "g_a", "r_top"]
    assert [r["id"] for r in stored[1]["rows"]] == ["r_2", "r_1"]
    published = last_menu(mqtt_mock)
    assert [item["id"] for item in published["items"]] == ["g_b", "g_a", "r_top"]


async def test_moving_is_offered_only_where_a_list_has_two_entries(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    config_entry = menu_entry([group("g_a", "A", [row("r_1", "script.movie", "button")])])
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)

    assert "menu_move" not in result["menu_options"]


async def test_a_row_stores_the_registry_id_and_follows_a_rename(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """Renaming the entity's id keeps the row bound, shown and acting on the new id."""
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "switch", "demo", "kettle-1", suggested_object_id="kettle"
    )
    assert entity.entity_id == "switch.kettle"
    hass.states.async_set("switch.kettle", "off", {"friendly_name": "Kettle"})
    claim_menu(box_on_the_broker)
    config_entry = menu_entry(None)
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)
    result = await _step(hass, result, "menu_add_row")
    result = await _form(hass, result, {"entity_id": "switch.kettle", "place": "top"})
    result = await _form(hass, result, {"control": "toggle"})
    await _save(hass, result)
    stored = config_entry.options[CONF_MENU][0]
    assert stored["entity_id"] == entity.id
    row_id = stored["id"]

    registry.async_update_entity("switch.kettle", new_entity_id="switch.water_kettle")
    hass.states.async_remove("switch.kettle")
    hass.states.async_set("switch.water_kettle", "on", {"friendly_name": "Water kettle"})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()

    published = items_by_id(last_menu(mqtt_mock))[row_id]
    assert published["label"] == "Water kettle"
    assert published["value"] is True
    assert published["available"] is True

    # Its state is followed under the new id.
    hass.states.async_set("switch.water_kettle", "off", {"friendly_name": "Water kettle"})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    assert items_by_id(last_menu(mqtt_mock))[row_id]["value"] is False

    # And an action reaches it there.
    calls = async_mock_service(hass, "switch", "toggle")
    fire_action(hass, row_id, "toggle")
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in calls] == ["switch.water_kettle"]

    # Editing the row shows the entity under its new id, and keeps the row's id.
    result = await _open_menu(hass, config_entry)
    result = await _step(hass, result, "menu_edit_row")
    result = await _form(hass, result, {"row": row_id})
    suggested = {
        str(key): key.description.get("suggested_value")
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested["entity_id"] == "switch.water_kettle"
    result = await _form(hass, result, {"entity_id": "switch.water_kettle", "place": "top"})
    result = await _form(hass, result, {"control": "toggle"})
    await _save(hass, result)
    assert config_entry.options[CONF_MENU][0]["id"] == row_id


async def test_a_row_stored_by_entity_id_keeps_its_id_once_the_entity_is_registered(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """Both forms name the same binding: re-saving does not turn the row into a new one."""
    claim_menu(box_on_the_broker)
    config_entry = menu_entry([row("r_kettle", "switch.kettle", "toggle")])
    entity = er.async_get(hass).async_get_or_create(
        "switch", "demo", "kettle-1", suggested_object_id="kettle"
    )
    hass.states.async_set("switch.kettle", "off")
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)
    result = await _step(hass, result, "menu_edit_row")
    result = await _form(hass, result, {"row": "r_kettle"})
    result = await _form(hass, result, {"entity_id": "switch.kettle", "place": "top"})
    result = await _form(hass, result, {"control": "toggle"})
    await _save(hass, result)

    assert config_entry.options[CONF_MENU] == [row("r_kettle", entity.id, "toggle")]


async def test_any_entity_can_be_an_indicator(
    hass: HomeAssistant, mqtt_mock, entry: MockConfigEntry
) -> None:
    """A domain the menu cannot act on is shown with its state - and asks nothing more."""
    hass.states.async_set("person.anna", "home", {"friendly_name": "Anna"})

    result = await _open_menu(hass, entry)
    result = await _step(hass, result, "menu_add_row")
    result = await _form(hass, result, {"entity_id": "person.anna", "place": "top"})
    assert result["step_id"] == "receiver_menu"
    await _save(hass, result)

    stored = entry.options[CONF_MENU][0]
    assert stored["control"] == "indicator"
    shown = items_by_id(last_menu(mqtt_mock))[stored["id"]]
    assert shown["state"] == "Home"
    assert shown["icon"] == "home"
    assert shown["value"] is None
    assert shown["color"] is None


async def test_moving_is_offered_where_a_group_has_two_rows(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """The top list holds one group; its two rows are what can be moved."""
    hass.states.async_set("script.movie", "off")
    claim_menu(box_on_the_broker)
    config_entry = menu_entry(
        [group("g_a", "A", [row("r_1", "script.movie", "button"),
                            row("r_2", "script.movie", "button")])]
    )
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)

    assert "menu_move" in result["menu_options"]


async def test_the_outline_names_a_row_by_its_entity_id_not_its_registry_id(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    entity = er.async_get(hass).async_get_or_create(
        "switch", "demo", "kettle-1", suggested_object_id="kettle"
    )
    hass.states.async_set("switch.kettle", "off", {"friendly_name": "Kettle"})
    claim_menu(box_on_the_broker)
    config_entry = menu_entry([row("r_kettle", entity.id, "toggle")])
    await async_setup_box(hass, config_entry)

    result = await _open_menu(hass, config_entry)

    outline = result["description_placeholders"]["menu"]
    assert "Kettle - switch.kettle (" in outline
    assert entity.id not in outline

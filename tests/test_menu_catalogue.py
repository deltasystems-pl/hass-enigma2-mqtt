"""The menu creator's catalogue: what it lists, its bound, and when it is published."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import logging
from typing import Any

from homeassistant.components.mqtt.const import MQTT_CONNECTION_STATE
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EntityCategory
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.enigma2_mqtt.const import CONF_MENU
from custom_components.enigma2_mqtt.menu import encode_menu, menu_revision, stored_menu
from custom_components.enigma2_mqtt.menu_creator import MAX_CATALOGUE_BYTES, fit_catalogue

from .conftest import (
    AVAILABILITY_TOPIC,
    INFO,
    INFO_TOPIC,
    async_arm_ha_mode_ack,
    async_setup_box,
)
from .menu_common import (
    EDITOR_INFO,
    MENU_CAPABILITIES,
    MISSING_ID,
    catalogue_publishes,
    claim_editor,
    group,
    last_catalogue,
    menu_entry,
    row,
)


def by_entity(catalogue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entity["e"]: entity for entity in catalogue["entities"]}


def _states(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "light.salon",
        "on",
        {"friendly_name": "Salon lamp", "brightness": 128, "supported_color_modes": ["brightness"]},
    )
    hass.states.async_set(
        "light.hall", "off", {"friendly_name": "Hall", "supported_color_modes": ["onoff"]}
    )
    hass.states.async_set("script.movie", "off", {"friendly_name": "Movie night"})


async def _setup(
    hass: HomeAssistant,
    retained: dict[str, str | bytes],
    menu: list[dict[str, Any]] | None = None,
    *,
    claim: bool = True,
) -> MockConfigEntry:
    _states(hass)
    if claim:
        claim_editor(retained)
    entry = menu_entry(menu)
    await async_setup_box(hass, entry)
    return entry


# ----------------------------------------------------------------------- revision


def test_the_revision_is_a_hash_of_the_canonical_stored_menu() -> None:
    menu = [row("r_a", "script.movie", "button", label="Kino \u0142\u00f3d\u017a")]
    canonical = json.dumps(menu, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert menu_revision(menu) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    assert menu_revision([]) == hashlib.sha256(b"[]").hexdigest()[:16]


def test_the_revision_is_stable_and_follows_order_but_not_key_order() -> None:
    first = row("r_a", "script.movie", "button")
    second = row("r_b", "light.salon", "toggle")
    shuffled = dict(reversed(list(first.items())))

    assert menu_revision([first, second]) == menu_revision([shuffled, second])
    assert menu_revision([first, second]) != menu_revision([second, first])
    assert menu_revision([first]) != menu_revision([{**first, "label": "x"}])
    assert len(menu_revision([first])) == 16


# ---------------------------------------------------------------------- contents


async def test_the_catalogue_lists_every_usable_entity_and_the_stored_menu(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """Names, areas (own, else the device's), controls, and what is hidden or a category."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    living = areas.async_create("Living room " + "x" * 80)
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    device = devices.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("demo", "d1")}
    )
    devices.async_update_device(device.id, area_id=kitchen.id)
    on_device = entities.async_get_or_create(
        "sensor", "demo", "t1", suggested_object_id="fridge", device_id=device.id
    )
    own_area = entities.async_get_or_create(
        "switch", "demo", "s1", suggested_object_id="kettle", device_id=device.id
    )
    entities.async_update_entity(own_area.entity_id, area_id=living.id)
    hidden = entities.async_get_or_create(
        "sensor", "demo", "t2", suggested_object_id="hidden",
        hidden_by=er.RegistryEntryHider.USER,
    )
    diagnostic = entities.async_get_or_create(
        "sensor", "demo", "t3", suggested_object_id="signal",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    config = entities.async_get_or_create(
        "select", "demo", "t4", suggested_object_id="mode",
        entity_category=EntityCategory.CONFIG,
    )
    disabled = entities.async_get_or_create(
        "light", "demo", "l9", suggested_object_id="disabled",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    hass.states.async_set(on_device.entity_id, "4", {"friendly_name": "Fridge  temperature"})
    hass.states.async_set(own_area.entity_id, "off", {"friendly_name": "Kettle"})
    hass.states.async_set(hidden.entity_id, "1", {"friendly_name": "Hidden"})
    hass.states.async_set(diagnostic.entity_id, "-60", {"friendly_name": "Signal"})
    hass.states.async_set(config.entity_id, "a", {"friendly_name": "Mode", "options": ["a"]})
    # A state for a disabled entity cannot happen in a running Home Assistant; it is left
    # out by its registry entry all the same.
    hass.states.async_set(disabled.entity_id, "on", {"friendly_name": "Disabled"})
    hass.states.async_set("light.no_registry", "on", {"friendly_name": "Z" + "y" * 80})
    lamp = entities.async_get_or_create("light", "demo", "l1", suggested_object_id="salon")
    menu = [
        group("g_salon", "Salon", [row("r_lamp", lamp.id, "slider")]),
        row("r_movie", "script.movie", "button", label="Kino", icon="movie"),
        row("r_gone", MISSING_ID, "toggle"),
    ]

    entry = await _setup(hass, box_on_the_broker, menu)

    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert catalogue["v"] == 1
    assert catalogue["left_out"] == 0
    assert catalogue["revision"] == menu_revision(stored_menu(entry.options))
    assert catalogue["areas"] == ["Kitchen", ("Living room " + "x" * 80)[:60]]
    assert catalogue["menu"] == [
        {
            "type": "group",
            "id": "g_salon",
            "label": "Salon",
            "icon": "sofa",
            "rows": [
                {"type": "row", "id": "r_lamp", "entity": "light.salon", "control": "slider",
                 "label": None, "icon": None}
            ],
        },
        {"type": "row", "id": "r_movie", "entity": "script.movie", "control": "button",
         "label": "Kino", "icon": "movie"},
        {"type": "row", "id": "r_gone", "entity": MISSING_ID, "control": "toggle",
         "label": None, "icon": None, "missing": True},
    ]
    found = by_entity(catalogue)
    assert found["sensor.fridge"] == {
        "e": "sensor.fridge", "n": "Fridge temperature", "a": 0, "c": ["indicator"]
    }
    assert found["switch.kettle"]["a"] == 1
    assert found["switch.kettle"]["c"] == ["toggle", "indicator"]
    assert found["sensor.hidden"]["h"] is True
    assert "k" not in found["sensor.hidden"]
    assert found["sensor.signal"]["k"] == "diagnostic"
    assert found["select.mode"]["k"] == "config"
    assert found["select.mode"]["c"] == ["selector", "indicator"]
    assert "h" not in found["select.mode"]
    assert found["light.salon"]["c"] == ["toggle", "slider", "indicator"]
    assert found["light.hall"]["c"] == ["toggle", "indicator"]
    assert found["light.hall"]["a"] is None
    assert found["script.movie"]["c"] == ["button"]
    assert found["light.no_registry"]["n"] == ("Z" + "y" * 80)[:60]
    assert "light.disabled" not in found
    names = [(entity["n"], entity["e"]) for entity in catalogue["entities"]]
    assert names == sorted(names)


async def test_a_row_whose_entity_has_no_state_is_missing_with_its_binding(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """A registered entity without a state, or an entity id nobody has: both missing."""
    entities = er.async_get(hass)
    dormant = entities.async_get_or_create("light", "demo", "l2", suggested_object_id="dormant")
    menu = [row("r_dormant", dormant.id, "toggle"), row("r_old", "light.old", "toggle")]

    await _setup(hass, box_on_the_broker, menu)

    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert [(item["entity"], item.get("missing")) for item in catalogue["menu"]] == [
        (dormant.id, True),
        ("light.old", True),
    ]
    assert "light.dormant" not in by_entity(catalogue)


# --------------------------------------------------------------------------- bound


def _entity(index: int, **extra: Any) -> dict[str, Any]:
    return {"e": f"sensor.s{index:05d}", "n": f"Sensor {index:05d} " + "n" * 40, "a": None,
            "c": ["indicator"], **extra}


def test_over_1_mib_entities_are_left_out_hidden_first_then_by_category_then_by_name() -> None:
    hidden = [_entity(i, h=True) for i in range(0, 2000)]
    diagnostic = [_entity(i, k="diagnostic") for i in range(2000, 5000)]
    config = [_entity(i, k="config") for i in range(5000, 6000)]
    plain = [_entity(i) for i in range(6000, 14000)]
    entities = sorted([*hidden, *diagnostic, *config, *plain], key=lambda e: (e["n"], e["e"]))
    payload = {"v": 1, "revision": "0" * 16, "areas": [], "left_out": 0,
               "menu": [{"type": "row", "id": "r_a", "entity": "script.x", "control": "button",
                         "label": None, "icon": None}],
               "entities": entities}
    assert len(encode_menu(payload)) > MAX_CATALOGUE_BYTES

    encoded, left_out = fit_catalogue(payload)

    assert len(encoded) < MAX_CATALOGUE_BYTES
    decoded = json.loads(encoded)
    assert decoded["left_out"] == left_out == len(entities) - len(decoded["entities"])
    assert decoded["menu"] == payload["menu"]
    kept = {entity["e"] for entity in decoded["entities"]}
    assert not kept & {entity["e"] for entity in hidden}
    assert {entity["e"] for entity in config} <= kept
    assert {entity["e"] for entity in plain} <= kept
    # The diagnostic ones go from the end of the name order.
    kept_diagnostic = [entity["e"] for entity in diagnostic if entity["e"] in kept]
    assert kept_diagnostic == [entity["e"] for entity in diagnostic][: len(kept_diagnostic)]
    assert 0 < len(kept_diagnostic) < len(diagnostic)
    # Only as many as it takes: one more would have fitted the bound.
    assert len(encoded) > MAX_CATALOGUE_BYTES - 200


def test_the_rest_go_from_the_end_of_the_name_order() -> None:
    entities = [_entity(i) for i in range(20000)]
    payload = {"v": 1, "revision": "0" * 16, "areas": [], "left_out": 0, "menu": [],
               "entities": entities}

    encoded, left_out = fit_catalogue(payload)

    decoded = json.loads(encoded)
    assert len(encoded) < MAX_CATALOGUE_BYTES
    assert decoded["entities"] == entities[: len(entities) - left_out]


def test_a_catalogue_under_the_bound_is_not_touched() -> None:
    payload = {"v": 1, "revision": "0" * 16, "areas": [], "left_out": 0, "menu": [],
               "entities": [_entity(1, h=True)]}

    encoded, left_out = fit_catalogue(payload)

    assert left_out == 0
    assert json.loads(encoded)["entities"] == [_entity(1, h=True)]


async def test_a_catalogue_that_leaves_entities_out_says_so_in_the_log(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    for index in range(12000):
        hass.states.async_set(f"sensor.s{index:05d}", "1", {"friendly_name": "n" * 60})

    with caplog.at_level(logging.WARNING):
        await _setup(hass, box_on_the_broker)

    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert catalogue["left_out"] > 0
    lines = [r for r in caplog.records if "leaves out" in r.getMessage()]
    assert len(lines) == 1
    assert str(catalogue["left_out"]) in lines[0].getMessage()


# ------------------------------------------------------------------------ triggers


async def test_published_retained_at_setup_only_when_claimed(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker, [row("r_movie", "script.movie", "button")])

    publishes = catalogue_publishes(mqtt_mock)
    assert len(publishes) == 1
    assert publishes[0][1:] == (1, True)
    assert json.loads(publishes[0][0])["menu"][0]["id"] == "r_movie"


async def test_a_receiver_that_does_not_claim_it_gets_the_empty_payload_once(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """A catalogue an earlier run left on the broker does not stay there.

    Not urgent, so it waits like a registry change: within five seconds.
    """
    await _setup(hass, box_on_the_broker, claim=False)
    assert catalogue_publishes(mqtt_mock) == []
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert catalogue_publishes(mqtt_mock) == [("", 1, True)]

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "uptime": 5}))
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=12))
    await hass.async_block_till_done()

    assert catalogue_publishes(mqtt_mock) == [("", 1, True)]


async def test_a_receiver_that_said_nothing_yet_is_left_alone(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str | bytes]
) -> None:
    retained[AVAILABILITY_TOPIC] = "online"
    _states(hass)
    await async_setup_box(hass, menu_entry(None))

    assert catalogue_publishes(mqtt_mock) == []


async def test_the_claim_appearing_publishes_and_disappearing_retracts(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker, claim=False)
    listeners = hass.bus.async_listeners().get(er.EVENT_ENTITY_REGISTRY_UPDATED, 0)

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(EDITOR_INFO))
    await hass.async_block_till_done()
    assert last_catalogue(mqtt_mock) is not None
    assert hass.bus.async_listeners()[er.EVENT_ENTITY_REGISTRY_UPDATED] == listeners + 1
    count = len(catalogue_publishes(mqtt_mock))

    async_fire_mqtt_message(
        hass, INFO_TOPIC, json.dumps({**INFO, "capabilities": MENU_CAPABILITIES})
    )
    await hass.async_block_till_done()
    assert len(catalogue_publishes(mqtt_mock)) == count
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    assert catalogue_publishes(mqtt_mock)[count:] == [("", 1, True)]
    assert hass.bus.async_listeners().get(er.EVENT_ENTITY_REGISTRY_UPDATED, 0) == listeners

    # The registries are not followed any more.
    er.async_get(hass).async_get_or_create("light", "demo", "l5", suggested_object_id="new")
    hass.states.async_set("light.new", "on")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=12))
    await hass.async_block_till_done()
    assert len(catalogue_publishes(mqtt_mock)) == count + 1


async def test_an_unchanged_catalogue_is_not_published_again(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker)
    entities = er.async_get(hass)
    entry = entities.async_get_or_create("light", "demo", "l6", suggested_object_id="other")
    hass.states.async_set(entry.entity_id, "on", {"friendly_name": "Other"})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    count = len(catalogue_publishes(mqtt_mock))

    # An icon is not in the catalogue.
    entities.async_update_entity(entry.entity_id, icon="mdi:lamp")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=12))
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**EDITOR_INFO, "uptime": 7}))
    await hass.async_block_till_done()

    assert len(catalogue_publishes(mqtt_mock)) == count


async def test_registry_changes_are_debounced_to_one_publish_in_five_seconds(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker)
    count = len(catalogue_publishes(mqtt_mock))
    entities = er.async_get(hass)
    start = dt_util.utcnow()

    first = entities.async_get_or_create("light", "demo", "l7", suggested_object_id="desk")
    hass.states.async_set(first.entity_id, "on", {"friendly_name": "Desk"})
    async_fire_time_changed(hass, start + timedelta(seconds=1))
    await hass.async_block_till_done()
    entities.async_update_entity(first.entity_id, name="Desk lamp")
    hass.states.async_set(first.entity_id, "on", {"friendly_name": "Desk lamp"})
    async_fire_time_changed(hass, start + timedelta(seconds=3))
    await hass.async_block_till_done()
    assert len(catalogue_publishes(mqtt_mock)) == count

    async_fire_time_changed(hass, start + timedelta(seconds=6))
    await hass.async_block_till_done()

    assert len(catalogue_publishes(mqtt_mock)) == count + 1
    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert by_entity(catalogue)["light.desk"]["n"] == "Desk lamp"


async def test_area_and_device_registry_changes_republish(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    device = devices.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("demo", "d2")}
    )
    lamp = entities.async_get_or_create(
        "light", "demo", "l8", suggested_object_id="garden", device_id=device.id
    )
    hass.states.async_set(lamp.entity_id, "on", {"friendly_name": "Garden"})
    await _setup(hass, box_on_the_broker)
    garden = areas.async_create("Garden")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    count = len(catalogue_publishes(mqtt_mock))

    devices.async_update_device(device.id, area_id=garden.id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=12))
    await hass.async_block_till_done()
    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert len(catalogue_publishes(mqtt_mock)) == count + 1
    assert catalogue["areas"][by_entity(catalogue)["light.garden"]["a"]] == "Garden"

    areas.async_update(garden.id, name="Back garden")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=18))
    await hass.async_block_till_done()
    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert catalogue["areas"] == ["Back garden"]


async def test_a_state_change_publishes_nothing(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker)
    count = len(catalogue_publishes(mqtt_mock))

    hass.states.async_set(
        "light.hall", "on", {"friendly_name": "Hall", "supported_color_modes": ["brightness"]}
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()

    assert len(catalogue_publishes(mqtt_mock)) == count


async def test_the_receiver_or_the_broker_coming_back_republishes_it(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker)
    count = len(catalogue_publishes(mqtt_mock))

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "online")
    await hass.async_block_till_done()
    assert len(catalogue_publishes(mqtt_mock)) == count + 1

    async_dispatcher_send(hass, MQTT_CONNECTION_STATE, False)
    await hass.async_block_till_done()
    async_dispatcher_send(hass, MQTT_CONNECTION_STATE, True)
    await hass.async_block_till_done()
    publishes = catalogue_publishes(mqtt_mock)
    assert len(publishes) == count + 2
    assert publishes[-1] == publishes[-2]


async def test_a_catalogue_built_while_home_assistant_starts_is_built_again(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    hass.set_state(CoreState.not_running)
    await _setup(hass, box_on_the_broker)
    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert "light.late" not in by_entity(catalogue)

    hass.states.async_set("light.late", "on", {"friendly_name": "Late"})
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    catalogue = last_catalogue(mqtt_mock)
    assert catalogue is not None
    assert "light.late" in by_entity(catalogue)


async def test_unloading_keeps_and_removing_retracts_the_catalogue(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    entry = await _setup(hass, box_on_the_broker)
    count = len(catalogue_publishes(mqtt_mock))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(catalogue_publishes(mqtt_mock)) == count

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert catalogue_publishes(mqtt_mock)[-1] == ("", 1, True)


async def test_reconfiguring_to_a_new_node_id_retracts_the_old_nodes_catalogue(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    from custom_components.enigma2_mqtt.const import CONF_BASE_TOPIC, CONF_NAME, CONF_NODE_ID

    entry = await _setup(hass, box_on_the_broker)
    box_on_the_broker["enigma2/vuuno4kse_0053ff/info"] = json.dumps(EDITOR_INFO)
    await async_arm_ha_mode_ack(hass, EDITOR_INFO, node_id="vuuno4kse_0053ff")

    result = await entry.start_reconfigure_flow(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_TOPIC: "enigma2", CONF_NODE_ID: "vuuno4kse_0053ff", CONF_NAME: "Dekoder"},
    )
    await hass.async_block_till_done()

    assert catalogue_publishes(mqtt_mock)[-1] == ("", 1, True)
    new = [
        call.args[1]
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == "enigma2mqtt/menu_catalogue/vuuno4kse_0053ff"
    ]
    assert new and json.loads(new[-1])["v"] == 1
    assert CONF_MENU not in entry.options

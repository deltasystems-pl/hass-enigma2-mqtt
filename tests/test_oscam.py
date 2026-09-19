"""Privacy and lifecycle of opt-in OSCam diagnostics."""

import json

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from .conftest import AVAILABILITY_TOPIC, INFO, INFO_TOPIC, async_setup_box

OSCAM_TOPIC = "enigma2/vuuno4kse_005301/oscam"


def source_entity(hass, source_id, metric):
    return er.async_get(hass).async_get_entity_id(
        "sensor",
        "enigma2_mqtt",
        f"vuuno4kse_005301_oscam_{source_id}_{metric}",
    )


def payload(*readers, reachable=True, access="granted"):
    return {
        "software": "OSCam",
        "version": "1.20 build r12345",
        "software_running": True,
        "api_reachable": reachable,
        "api_access": access,
        "readonly": True,
        "uptime_s": 120,
        "readers_configured": len(readers),
        "readers_enabled": len(readers),
        "readers_healthy": len(readers),
        "cards_ready": sum(reader["status"] == "ready" for reader in readers),
        "servers_connected": sum(reader["status"] == "connected" for reader in readers),
        "shared_cards": 4,
        "readers": list(readers),
        "password": "must-not-survive",
    }


READER = {
    "id": "reader_0123456789ab",
    "kind": "reader",
    "enabled": True,
    "status": "ready",
    "protocol": "internal",
    "shared_cards": None,
    "label": "private-reader-name",
}
READER_TWO = {
    **READER,
    "id": "reader_abcdef6789ab",
    "status": "no_card",
}
SERVER = {
    "id": "server_abcdef012345",
    "kind": "server",
    "enabled": True,
    "status": "connected",
    "protocol": "cccam",
    "shared_cards": 4,
    "address": "private.example",
}


async def test_retained_oscam_before_info_is_normalized_and_applied(
    hass, mqtt_mock, retained, config_entry
):
    retained[OSCAM_TOPIC] = json.dumps(payload(READER, READER_TWO, SERVER))
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    await async_setup_box(hass, config_entry)

    state = config_entry.runtime_data.state.oscam
    assert state is not None
    assert set(state) == {
        "software",
        "version",
        "software_running",
        "api_reachable",
        "api_access",
        "readonly",
        "uptime_s",
        "readers_configured",
        "readers_enabled",
        "readers_healthy",
        "cards_ready",
        "servers_connected",
        "shared_cards",
        "readers",
    }
    assert set(state["readers"][0]) == {
        "id",
        "kind",
        "enabled",
        "status",
        "protocol",
        "shared_cards",
    }
    assert hass.states.get(source_entity(hass, READER["id"], "status")).state == "ready"
    assert hass.states.get(source_entity(hass, SERVER["id"], "shared_cards")).state == "4"
    first = hass.states.get(source_entity(hass, READER["id"], "status"))
    second = hass.states.get(source_entity(hass, READER_TWO["id"], "status"))
    assert first.name != second.name
    assert READER["id"].split("_", 1)[1] in first.name
    assert READER_TWO["id"].split("_", 1)[1] in second.name


async def test_opt_out_discards_retained_payload(hass, mqtt_mock, retained, config_entry):
    retained[OSCAM_TOPIC] = json.dumps(payload(READER))
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps({**INFO, "settings": {"oscam_telemetry": False}})
    await async_setup_box(hass, config_entry)
    assert config_entry.runtime_data.state.oscam is None


async def test_opt_out_removes_stale_binary_registry_after_restart(
    hass, mqtt_mock, retained, config_entry
):
    registry = er.async_get(hass)
    config_entry.add_to_hass(hass)
    entries = [
        registry.async_get_or_create(
            "binary_sensor",
            "enigma2_mqtt",
            f"vuuno4kse_005301_oscam_{key}",
            config_entry=config_entry,
            suggested_object_id=f"stale_oscam_{key}",
        )
        for key in ("software_running", "api_reachable", "readonly", "api_access")
    ]
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps({**INFO, "settings": {"oscam_telemetry": False}})

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert all(registry.async_get(entry.entity_id) is None for entry in entries)


async def test_api_failure_preserves_entities_but_authoritative_removal_reconciles(
    hass, mqtt_mock, retained, config_entry
):
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    retained[AVAILABILITY_TOPIC] = "online"
    retained[OSCAM_TOPIC] = json.dumps(payload(READER))
    await async_setup_box(hass, config_entry)
    entity_id = source_entity(hass, READER["id"], "status")
    assert hass.states.get(entity_id) is not None

    async_fire_mqtt_message(hass, OSCAM_TOPIC, json.dumps(payload(reachable=False, access=None)))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is not None
    assert hass.states.get(entity_id).state == "unavailable"

    async_fire_mqtt_message(hass, OSCAM_TOPIC, json.dumps(payload()))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None


async def test_tombstone_clears_state_and_dynamic_entities(hass, mqtt_mock, retained, config_entry):
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    retained[AVAILABILITY_TOPIC] = "online"
    retained[OSCAM_TOPIC] = json.dumps(payload(READER))
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, OSCAM_TOPIC, "")
    await hass.async_block_till_done()
    assert config_entry.runtime_data.state.oscam is None
    assert hass.states.get(source_entity(hass, READER["id"], "status")).state == "unavailable"
    async_fire_mqtt_message(
        hass,
        INFO_TOPIC,
        json.dumps({**INFO, "settings": {"oscam_telemetry": False}}),
    )
    await hass.async_block_till_done()
    assert source_entity(hass, READER["id"], "status") is None


async def test_authoritative_empty_snapshot_removes_stale_registry_after_restart(
    hass, mqtt_mock, retained, config_entry
):
    stale = "reader_deadbeef0123"
    registry = er.async_get(hass)
    config_entry.add_to_hass(hass)
    entry = registry.async_get_or_create(
        "sensor",
        "enigma2_mqtt",
        f"vuuno4kse_005301_oscam_{stale}_status",
        config_entry=config_entry,
        suggested_object_id="stale_oscam_source",
    )
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    retained[OSCAM_TOPIC] = json.dumps(payload())

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(entry.entity_id) is None


async def test_private_version_and_extra_reader_fields_never_enter_state(
    hass, mqtt_mock, retained, config_entry
):
    document = payload(READER)
    document["version"] = "private operator build"
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    retained[OSCAM_TOPIC] = json.dumps(document)

    await async_setup_box(hass, config_entry)

    state = config_entry.runtime_data.state.oscam
    assert state["version"] is None
    assert "label" not in state["readers"][0]
    assert "password" not in state

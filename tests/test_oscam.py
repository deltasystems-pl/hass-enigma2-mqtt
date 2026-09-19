"""Privacy and lifecycle of opt-in OSCam diagnostics."""

import json

from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.enigma2_mqtt.box import OSCAM_VERSION, OSCAM_VERSION_MAX

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


@pytest.mark.parametrize(
    "version",
    [
        "1.20_svn build r11718-079",
        "1.20_svn build r11718",
        "1.20",
        "1.20-unstable_svn build r12345",
    ],
)
async def test_the_versions_oscam_really_reports_are_published(
    hass, mqtt_mock, retained, config_entry, version
):
    """The receiver this was written against reports `1.20_svn build r11718-079`.

    The pattern had no room for the patch suffix on the revision, so the one number a
    support question starts with arrived as null.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    retained[OSCAM_TOPIC] = json.dumps({**payload(READER), "version": version})
    await async_setup_box(hass, config_entry)

    assert config_entry.runtime_data.state.oscam["version"] == version


@pytest.mark.parametrize(
    "version", ["'; DROP TABLE", "x" * 200, "not a version", 1.2, None, "1.20 build rXYZ"]
)
async def test_a_version_this_cannot_read_costs_only_the_version(
    hass, mqtt_mock, retained, config_entry, version
):
    """Dropping the whole payload would take every OSCam entity with it.

    A version is a label on a support question, not a fact anything depends on, so an
    unreadable one is left out and the health that matters is still published.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    retained[OSCAM_TOPIC] = json.dumps({**payload(READER), "version": version})
    await async_setup_box(hass, config_entry)

    state = config_entry.runtime_data.state.oscam
    assert state is not None, "the payload was rejected over its version string"
    assert state["version"] is None
    assert state["software_running"] is True
    assert state["readers_configured"] == 1
    assert len(state["readers"]) == 1


async def test_source_entities_appear_when_the_receiver_answers_after_setup(
    hass, mqtt_mock, retained, config_entry
):
    """This is the order a real receiver uses, and it produced no entities at all.

    `async_setup_entry` subscribes and returns; the retained burst that carries `info`,
    and with it the capability list, arrives after the platforms have already been set
    up. The per-source entities were gated on that capability at setup time, so on a
    live box they were never created — while the aggregate OSCam sensors, which follow
    `info` rather than reading it once, appeared exactly as expected. The branch taken
    instead also deleted every `oscam_` registration the box had.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(
        hass,
        INFO_TOPIC,
        json.dumps(
            {
                **INFO,
                "capabilities": [*INFO["capabilities"], "oscam"],
                "settings": {"oscam_telemetry": True},
            }
        ),
    )
    async_fire_mqtt_message(hass, OSCAM_TOPIC, json.dumps(payload(READER, SERVER)))
    await hass.async_block_till_done()

    assert source_entity(hass, READER["id"], "status") is not None
    assert source_entity(hass, READER["id"], "ready_cards") is not None
    assert source_entity(hass, SERVER["id"], "status") is not None
    assert source_entity(hass, SERVER["id"], "shared_cards") is not None
    assert hass.states.get(source_entity(hass, SERVER["id"], "shared_cards")).state == "4"


async def test_a_box_that_has_said_nothing_keeps_the_source_entities_it_had(
    hass, mqtt_mock, retained, config_entry
):
    """A reload is a moment of silence, not an answer of "no".

    Deleting the registrations then would lose every name, area and icon the household
    had given these entities, on every restart.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    retained[OSCAM_TOPIC] = json.dumps(payload(READER, SERVER))
    await async_setup_box(hass, config_entry)
    before = source_entity(hass, READER["id"], "status")
    assert before is not None

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert source_entity(hass, READER["id"], "status") == before


async def test_an_endlessly_long_version_is_refused_before_it_is_matched(
    hass, mqtt_mock, retained, config_entry
):
    """The pattern repeats a group, so what it accepts has no length of its own.

    `1.20` followed by a megabyte of `_a` matches it perfectly well. A version is a
    label on a bug report; anything this long is not one, and matching it is work done
    on a receiver's say-so.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    long_but_valid = "1.20" + "_a" * 200
    assert OSCAM_VERSION.fullmatch(long_but_valid), "the pattern itself accepts this"
    retained[OSCAM_TOPIC] = json.dumps({**payload(READER), "version": long_but_valid})
    await async_setup_box(hass, config_entry)

    state = config_entry.runtime_data.state.oscam
    assert state is not None
    assert state["version"] is None
    assert state["readers_configured"] == 1


async def test_a_version_exactly_at_the_limit_is_still_published(
    hass, mqtt_mock, retained, config_entry
):
    """The bound is generous: every version a real OSCam reports is far inside it."""
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], "oscam"],
            "settings": {"oscam_telemetry": True},
        }
    )
    version = "1.20" + "_a" * ((OSCAM_VERSION_MAX - 4) // 2)
    assert len(version) == OSCAM_VERSION_MAX
    retained[OSCAM_TOPIC] = json.dumps({**payload(READER), "version": version})
    await async_setup_box(hass, config_entry)

    assert config_entry.runtime_data.state.oscam["version"] == version

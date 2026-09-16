"""The diagnostics download is safe to attach to a public issue."""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import BOX_NAME, BOXTYPE, IMAGE, NODE_ID, PLUGIN_VERSION


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Tolerate the MQTT integration's own periodic timer.

    `mqtt_mock` sets up the real MQTT integration, which schedules
    `MQTT._async_start_misc_periodic` and does not cancel it on teardown. Home
    Assistant's own MQTT tests make the same allowance; it says nothing about this
    integration.
    """
    return True


async def test_diagnostics_redact_what_identifies_a_household(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """The MAC, the address and every credential key are redacted; the rest stays."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["announcement"]["mac"] == REDACTED
    assert diagnostics["announcement"]["ip"] == REDACTED
    assert diagnostics["info"]["mac"] == REDACTED
    assert diagnostics["info"]["ip"] == REDACTED
    assert diagnostics["device"]["configuration_url"] == REDACTED

    assert diagnostics["entry"]["unique_id"] == NODE_ID
    assert diagnostics["box"]["node_id"] == NODE_ID
    assert diagnostics["box"]["available"] is True
    assert "hdd" in diagnostics["box"]["capabilities"]
    assert diagnostics["announcement"]["name"] == BOX_NAME
    assert diagnostics["info"]["image"] == IMAGE
    assert diagnostics["info"]["plugin"] == PLUGIN_VERSION
    assert diagnostics["device"]["model"] == BOXTYPE
    assert diagnostics["device"]["manufacturer"] == "Vu+"


async def test_diagnostics_redact_credentials_that_do_not_exist_yet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """The installer of M4 cannot add a secret to a file that is already shared."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            "username": "root",
            "password": "hunter2",
            "ssh_password": "hunter2",
            "broker_password": "hunter2",
        },
    )
    await hass.async_block_till_done()

    entry_data = (await async_get_config_entry_diagnostics(hass, config_entry))["entry"]

    assert entry_data["data"]["username"] == REDACTED
    assert entry_data["data"]["password"] == REDACTED
    assert entry_data["data"]["ssh_password"] == REDACTED
    assert entry_data["data"]["broker_password"] == REDACTED
    assert entry_data["data"]["node_id"] == NODE_ID

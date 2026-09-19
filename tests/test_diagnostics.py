"""The diagnostics download is safe to attach to a public issue."""

from __future__ import annotations

import json

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    BOX_NAME,
    BOXTYPE,
    EPG_GRID,
    EPG_GRID_TOPIC,
    IMAGE,
    NODE_ID,
    PLUGIN_VERSION,
    SCREEN,
    SREF,
    async_setup_box,
)


async def test_diagnostics_redact_what_identifies_a_household(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """The MAC, the address and every credential key are redacted; the rest stays."""
    await async_setup_box(hass, config_entry)

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
    await async_setup_box(hass, config_entry)

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


async def test_diagnostics_carry_the_last_payload_of_every_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A bug report about an entity is unanswerable without the topic behind it."""
    box_on_the_broker[EPG_GRID_TOPIC] = json.dumps(EPG_GRID)
    await async_setup_box(hass, config_entry)

    topics = (await async_get_config_entry_diagnostics(hass, config_entry))["topics"]

    assert topics["power"] == "on"
    assert topics["service"]["sref"] == SREF
    assert topics["epg"]["now"]["title"] == "Wiadomości"
    assert topics["tuner"]["snr"] == 78
    assert topics["volume"] == {"level": 35, "muted": False}
    assert topics["timers"][0]["state"] == "waiting"
    assert topics["hdd"]["mounted"] is True
    assert topics["last_error"] is None
    assert topics["epg_grid"]["ulubione_tv"]["channels"] == 1


async def test_diagnostics_summarise_the_screenshot_and_the_channel_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A picture of somebody's television and their channel list are not evidence.

    The size of the frame and the shape of the list answer every question a report
    asks; the contents answer none of them and describe a household.
    """
    await async_setup_box(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    topics = diagnostics["topics"]

    assert topics["screen"]["bytes"] == len(SCREEN)
    assert topics["screen"]["updated"] is not None
    assert topics["channels"]["bouquets"] == [
        {"name": "Ulubione TV", "channels": 2},
        {"name": "Sport", "channels": 2},
    ]
    # The key topic is who pressed what a moment ago, and is not state at all.
    assert "key" not in topics
    # A channel that is only in the list, never on the screen, does not appear at all.
    assert "Eurosport" not in json.dumps(diagnostics, default=str)

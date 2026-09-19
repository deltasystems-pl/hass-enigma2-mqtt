"""The options flow, and following a box whose topics moved."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_BOUQUETS,
    CONF_DANGEROUS_BUTTONS,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_WOL_MAC,
    DOMAIN,
)

from .conftest import INFO, NODE_ID, async_setup_box


async def test_the_options_offer_the_bouquets_the_box_published(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A user should not have to type the name of a bouquet the box already named."""
    await async_setup_box(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    selector = result["data_schema"].schema[CONF_BOUQUETS]
    options = selector.config["options"]
    assert options == ["Ulubione TV", "Sport"]
    # And a name can still be typed, for a bouquet the box has not published yet.
    assert selector.config["custom_value"] is True


async def test_saving_the_options_reloads_the_entry(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The buttons appear without anybody restarting Home Assistant."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("button.dekoder_salon_reboot") is None

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DANGEROUS_BUTTONS: True,
            CONF_WOL_MAC: "  00:00:5e:00:53:07  ",
            CONF_BOUQUETS: ["Ulubione TV"],
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_DANGEROUS_BUTTONS: True,
        # Trimmed: a MAC with a space around it is a MAC nothing would match.
        CONF_WOL_MAC: "00:00:5e:00:53:07",
        CONF_BOUQUETS: ["Ulubione TV"],
    }
    assert hass.states.get("button.dekoder_salon_reboot") is not None


async def test_the_options_open_on_a_box_that_never_loaded(
    hass: HomeAssistant, mqtt_mock, config_entry: MockConfigEntry
) -> None:
    """That is exactly when somebody wants to fix the Wake-on-LAN address."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"].schema[CONF_BOUQUETS].config["options"] == []


async def test_reconfigure_follows_a_new_base_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Changing only the address keeps the device and everything on it."""
    await async_setup_box(hass, config_entry)
    device_id = _device_id(hass, config_entry)
    retained[f"dekodery/{NODE_ID}/info"] = json.dumps(INFO)

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_TOPIC: "dekodery",
            CONF_NODE_ID: NODE_ID,
            CONF_NAME: "Dekoder salon",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_BASE_TOPIC] == "dekodery"
    assert _device_id(hass, config_entry) == device_id


async def test_reconfigure_to_a_new_node_id_replaces_the_device(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A changed node id is a changed identity, so the old device does not linger."""
    await async_setup_box(hass, config_entry)
    old_device_id = _device_id(hass, config_entry)
    retained["enigma2/vuuno4kse_0053ff/info"] = json.dumps(INFO)

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
    assert config_entry.unique_id == "vuuno4kse_0053ff"
    assert dr.async_get(hass).async_get(old_device_id) is None

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "vuuno4kse_0053ff"), config_entry.entry_id
    )
    assert device is not None


async def test_reconfigure_reports_a_topic_with_no_box_on_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A typo in the base topic is silent otherwise: the entities just stop."""
    await async_setup_box(hass, config_entry)

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_TOPIC: "nothing-publishes-here",
            CONF_NODE_ID: NODE_ID,
            CONF_NAME: "Dekoder salon",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_found"}
    assert config_entry.data[CONF_BASE_TOPIC] == "enigma2"


async def test_reconfigure_refuses_a_wildcard(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A wildcard in a base topic subscribes to other people's boxes."""
    await async_setup_box(hass, config_entry)

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_TOPIC: "enigma2/+", CONF_NODE_ID: NODE_ID, CONF_NAME: "x"},
    )

    assert result["errors"] == {"base": "invalid_topic"}


async def test_reconfigure_refuses_a_node_id_another_entry_owns(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Two entries with one node id would fight over the same topics."""
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Dekoder sypialnia",
        unique_id="vuuno4kse_0053aa",
        data={
            CONF_NODE_ID: "vuuno4kse_0053aa",
            CONF_BASE_TOPIC: "enigma2",
            CONF_NAME: "Dekoder sypialnia",
        },
    )
    other.add_to_hass(hass)
    await async_setup_box(hass, config_entry)

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_TOPIC: "enigma2",
            CONF_NODE_ID: "vuuno4kse_0053aa",
            CONF_NAME: "x",
        },
    )

    assert result["errors"] == {"base": "already_configured"}


def _device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the device id of the box this entry configures."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.data[CONF_NODE_ID]), entry.entry_id
    )
    assert device is not None
    return device.id

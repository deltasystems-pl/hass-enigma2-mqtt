"""The options flow, and following a box whose topics moved."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_BOUQUETS,
    CONF_CAM_TELEMETRY,
    CONF_DANGEROUS_BUTTONS,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_PUBLISH_KEYS,
    CONF_RECEIVER_HOST,
    CONF_SCREENSHOT,
    CONF_SCREENSHOT_DELAY,
    CONF_SCREENSHOT_INTERVAL,
    CONF_WOL_MAC,
    DOMAIN,
)

from .conftest import (
    INFO,
    INFO_TOPIC,
    NODE_ID,
    async_arm_ha_mode_ack,
    async_setup_box,
    command_topic,
)

PLUGIN_SETTINGS = {
    CONF_PUBLISH_KEYS: False,
    CONF_SCREENSHOT: "interval",
    CONF_SCREENSHOT_INTERVAL: 120,
}
PLUGIN_DEFAULT_SETTINGS = {
    CONF_PUBLISH_KEYS: True,
    CONF_SCREENSHOT: "on_zap",
    CONF_SCREENSHOT_INTERVAL: 60,
}
PLUGIN_MODERN_SETTINGS = {
    **PLUGIN_DEFAULT_SETTINGS,
    CONF_SCREENSHOT_DELAY: 4,
    CONF_CAM_TELEMETRY: False,
}


async def _arm_config_ack(hass: HomeAssistant, settings=PLUGIN_SETTINGS) -> None:
    @callback
    def _command_received(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "settings": settings}))

    await mqtt.async_subscribe(hass, command_topic("config"), _command_received)


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
    async_fire_mqtt_message(
        hass, INFO_TOPIC, json.dumps({**INFO, "settings": PLUGIN_DEFAULT_SETTINGS})
    )
    await hass.async_block_till_done()
    await _arm_config_ack(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DANGEROUS_BUTTONS: True,
            CONF_WOL_MAC: "  00:00:5e:00:53:07  ",
            CONF_BOUQUETS: ["Ulubione TV"],
            **PLUGIN_SETTINGS,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_DANGEROUS_BUTTONS: True,
        # Trimmed: a MAC with a space around it is a MAC nothing would match.
        CONF_WOL_MAC: "00:00:5e:00:53:07",
        CONF_BOUQUETS: ["Ulubione TV"],
        **PLUGIN_SETTINGS,
    }
    assert hass.states.get("button.dekoder_salon_reboot") is not None


async def test_modern_plugin_offers_delay_and_accepts_ack_with_extra_settings(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Only advertised settings are sent and future ACK fields are harmless."""
    await async_setup_box(hass, config_entry)
    async_fire_mqtt_message(
        hass, INFO_TOPIC, json.dumps({**INFO, "settings": PLUGIN_MODERN_SETTINGS})
    )
    await hass.async_block_till_done()
    requested = {
        **PLUGIN_SETTINGS,
        CONF_SCREENSHOT_DELAY: 7,
        CONF_CAM_TELEMETRY: True,
    }
    await _arm_config_ack(hass, {**requested, "future_setting": "supported"})

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert CONF_SCREENSHOT_DELAY in result["data_schema"].schema
    assert CONF_CAM_TELEMETRY in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DANGEROUS_BUTTONS: False,
            CONF_WOL_MAC: "",
            CONF_BOUQUETS: [],
            **requested,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_SCREENSHOT_DELAY] == 7


async def test_future_plugin_setting_does_not_trigger_an_unchanged_config_write(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Unknown advertised settings are preserved when visible values did not move."""
    await async_setup_box(hass, config_entry)
    advertised = {**PLUGIN_MODERN_SETTINGS, "future_setting": "supported"}
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "settings": advertised}))
    await hass.async_block_till_done()
    box = config_entry.runtime_data
    box.async_command = AsyncMock()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DANGEROUS_BUTTONS: True,
            CONF_WOL_MAC: "",
            CONF_BOUQUETS: [],
            **PLUGIN_MODERN_SETTINGS,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    box.async_command.assert_not_awaited()


async def test_the_options_open_on_a_box_that_never_loaded(
    hass: HomeAssistant, mqtt_mock, config_entry: MockConfigEntry
) -> None:
    """That is exactly when somebody wants to fix the Wake-on-LAN address."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"].schema[CONF_BOUQUETS].config["options"] == []


async def test_options_failure_preserves_the_previous_options(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A rejected plugin setting must not leave HA claiming it was applied."""
    await async_setup_box(hass, config_entry)
    original = dict(config_entry.options)
    async_fire_mqtt_message(
        hass, INFO_TOPIC, json.dumps({**INFO, "settings": PLUGIN_DEFAULT_SETTINGS})
    )
    await hass.async_block_till_done()
    box = config_entry.runtime_data
    box.async_command = AsyncMock(side_effect=HomeAssistantError("rejected"))

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DANGEROUS_BUTTONS: True,
            CONF_WOL_MAC: "",
            CONF_BOUQUETS: [],
            **PLUGIN_SETTINGS,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "plugin_config_failed"}
    assert config_entry.options == original


async def test_legacy_plugin_still_saves_local_options_without_config_command(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """M4 receiver controls must not regress local options on an older plugin."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data
    box.async_command = AsyncMock()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert CONF_PUBLISH_KEYS not in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DANGEROUS_BUTTONS: True,
            CONF_WOL_MAC: "00:00:5e:00:53:07",
            CONF_BOUQUETS: ["Sport"],
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    box.async_command.assert_not_awaited()
    assert config_entry.options[CONF_BOUQUETS] == ["Sport"]


async def test_reconfigure_no_ack_preserves_entry_and_device(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Do not forget the working identity until the new plugin accepts takeover."""
    await async_setup_box(hass, config_entry)
    old_device_id = _device_id(hass, config_entry)
    retained["enigma2/vuuno4kse_0053ff/info"] = json.dumps(INFO)

    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_request_ha_mode",
        AsyncMock(return_value=None),
    ):
        result = await config_entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BASE_TOPIC: "enigma2",
                CONF_NODE_ID: "vuuno4kse_0053ff",
                CONF_NAME: "Dekoder salon",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_ack"}
    assert config_entry.data[CONF_NODE_ID] == NODE_ID
    assert _device_id(hass, config_entry) == old_device_id


async def test_reconfigure_follows_a_new_base_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Changing only the address keeps the device and everything on it."""
    await async_setup_box(hass, config_entry)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            "ssh_host": "pinned-receiver.example",
            "ssh_host_key": "ssh-ed25519 AAAApinned",
        },
    )
    device_id = _device_id(hass, config_entry)
    retained[f"dekodery/{NODE_ID}/info"] = json.dumps(INFO)
    await async_arm_ha_mode_ack(hass, base_topic="dekodery")

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_TOPIC: "dekodery",
            CONF_NODE_ID: NODE_ID,
            CONF_NAME: "Dekoder salon",
            CONF_RECEIVER_HOST: "metadata-receiver.example",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_BASE_TOPIC] == "dekodery"
    assert config_entry.data[CONF_RECEIVER_HOST] == "metadata-receiver.example"
    assert config_entry.data["ssh_host"] == "pinned-receiver.example"
    assert config_entry.data["ssh_host_key"] == "ssh-ed25519 AAAApinned"
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
    await async_arm_ha_mode_ack(hass, node_id="vuuno4kse_0053ff")

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

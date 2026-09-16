"""The two ways a receiver reaches Home Assistant, and every way they can fail."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_MQTT, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_NAME,
    CONF_NODE_ID,
    DOMAIN,
)

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    BASE_TOPIC,
    BOX_NAME,
    BOXTYPE,
    HA_MODE_TOPIC,
    IMAGE,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    async_arm_ha_mode_ack,
)


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Tolerate the MQTT integration's own periodic timer.

    `mqtt_mock` sets up the real MQTT integration, which schedules
    `MQTT._async_start_misc_periodic` and does not cancel it on teardown. Home
    Assistant's own MQTT tests make the same allowance; it says nothing about this
    integration.
    """
    return True


def _service_info(
    payload: str | bytes, topic: str = ANNOUNCEMENT_TOPIC
) -> MqttServiceInfo:
    """Build the discovery message the MQTT integration would hand the flow."""
    return MqttServiceInfo(
        topic=topic,
        payload=payload,
        qos=0,
        retain=True,
        subscribed_topic="enigma2mqtt/discovery/#",
        timestamp=0.0,
    )


async def _start_discovered_flow(
    hass: HomeAssistant, payload: str | bytes, topic: str = ANNOUNCEMENT_TOPIC
) -> dict[str, Any]:
    """Start the flow the way the MQTT integration starts it."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_MQTT}, data=_service_info(payload, topic)
    )


async def test_discovered_flow_creates_the_entry(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """An announced box is confirmed, switched over and added."""
    result = await _start_discovered_flow(hass, json.dumps(ANNOUNCEMENT))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {
        "name": BOX_NAME,
        "boxtype": BOXTYPE,
        "image": IMAGE,
    }

    await async_arm_ha_mode_ack(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == BOX_NAME
    assert result["data"] == {
        CONF_NODE_ID: NODE_ID,
        CONF_BASE_TOPIC: BASE_TOPIC,
        CONF_NAME: BOX_NAME,
    }
    assert result["result"].unique_id == NODE_ID

    mqtt_mock.async_publish.assert_any_call(
        HA_MODE_TOPIC, "integration", 1, False, message_expiry_interval=None
    )


async def test_discovered_flow_aborts_when_already_configured(
    hass: HomeAssistant, mqtt_mock, config_entry: MockConfigEntry
) -> None:
    """A box that is already set up is not offered twice."""
    config_entry.add_to_hass(hass)

    result = await _start_discovered_flow(hass, json.dumps(ANNOUNCEMENT))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_discovered_flow_updates_the_existing_entry(
    hass: HomeAssistant, mqtt_mock, config_entry: MockConfigEntry
) -> None:
    """A renamed box updates the entry it already owns instead of adding a second."""
    config_entry.add_to_hass(hass)

    await _start_discovered_flow(
        hass, json.dumps({**ANNOUNCEMENT, "name": "Dekoder sypialnia"})
    )

    assert config_entry.data[CONF_NAME] == "Dekoder sypialnia"


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("enigma2mqtt/discovery/vuuno4kse_005301/status", json.dumps(ANNOUNCEMENT)),
        ("enigma2mqtt/discovery/config", json.dumps(ANNOUNCEMENT)),
        (ANNOUNCEMENT_TOPIC, "not json at all"),
        (ANNOUNCEMENT_TOPIC, json.dumps(["a", "list"])),
        (ANNOUNCEMENT_TOPIC, json.dumps({"name": "no node id"})),
    ],
)
async def test_discovered_flow_rejects_foreign_messages(
    hass: HomeAssistant, mqtt_mock, topic: str, payload: str
) -> None:
    """Anything that is not this plugin's announcement is dropped, not half-handled."""
    result = await _start_discovered_flow(hass, payload, topic)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_enigma2_announcement"


async def test_discovered_flow_aborts_on_a_retraction(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """An empty retained payload is the plugin withdrawing itself."""
    result = await _start_discovered_flow(hass, "")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "retracted"


async def test_discovered_flow_retries_after_a_missing_acknowledgement(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """A box that does not answer leaves the form up, and the retry still works."""
    result = await _start_discovered_flow(hass, json.dumps(ANNOUNCEMENT))

    with patch("custom_components.enigma2_mqtt.config_flow.ACK_TIMEOUT", 0.01):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "no_ack"}

    await async_arm_ha_mode_ack(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_manual_flow_creates_the_entry(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """A box typed in by hand is probed on its retained `info` topic, then added."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    retained[INFO_TOPIC] = json.dumps(INFO)
    await async_arm_ha_mode_ack(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID, CONF_NAME: BOX_NAME},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == BOX_NAME
    assert result["data"][CONF_NODE_ID] == NODE_ID
    assert result["result"].unique_id == NODE_ID


async def test_manual_flow_reports_a_box_that_is_not_there(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """Nothing retained on the topic means the box is not on this broker."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch("custom_components.enigma2_mqtt.config_flow.PROBE_TIMEOUT", 0.01):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: "vuuno4kse_000000"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_found"}


async def test_manual_flow_reports_a_box_that_does_not_acknowledge(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """The box is there, but it never confirms the mode switch."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    retained[INFO_TOPIC] = json.dumps(INFO)

    with patch("custom_components.enigma2_mqtt.config_flow.ACK_TIMEOUT", 0.01):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_ack"}


async def test_manual_flow_aborts_when_already_configured(
    hass: HomeAssistant, mqtt_mock, config_entry: MockConfigEntry
) -> None:
    """The node id is the unique id, whichever path added the box."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("base_topic", "node_id"),
    [("enigma2/#", NODE_ID), ("enigma2", "+"), ("", NODE_ID), ("enigma2", "")],
)
async def test_manual_flow_rejects_a_wildcard_topic(
    hass: HomeAssistant, mqtt_mock, base_topic: str, node_id: str
) -> None:
    """A wildcard would subscribe to every box on the broker at once."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_TOPIC: base_topic, CONF_NODE_ID: node_id},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_topic"}


async def test_manual_flow_falls_back_to_the_node_id_as_a_name(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """An empty name is not an empty device page."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    retained[INFO_TOPIC] = json.dumps(INFO)
    await async_arm_ha_mode_ack(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID, CONF_NAME: "  "},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NODE_ID

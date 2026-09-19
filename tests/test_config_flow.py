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
    CONF_RECEIVER_HOST,
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


async def _start_manual_flow(hass: HomeAssistant) -> dict[str, Any]:
    """Choose the existing-plugin branch from the first-user menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
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
    result = await _start_manual_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

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


async def test_manual_flow_keeps_an_optional_receiver_address_as_metadata(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """The address helps links and SSH but is not used to find the MQTT box."""
    result = await _start_manual_flow(hass)
    retained[INFO_TOPIC] = json.dumps(INFO)
    await async_arm_ha_mode_ack(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_TOPIC: BASE_TOPIC,
            CONF_NODE_ID: NODE_ID,
            CONF_RECEIVER_HOST: "receiver.example",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_RECEIVER_HOST] == "receiver.example"


@pytest.mark.parametrize(
    "host",
    [
        "http://receiver",
        "user@receiver",
        "receiver/path",
        "bad host",
        "fe80::1%foo]@example.com",
    ],
)
async def test_manual_flow_rejects_a_url_or_credential_as_receiver_host(
    hass: HomeAssistant, mqtt_mock, host: str
) -> None:
    result = await _start_manual_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BASE_TOPIC: BASE_TOPIC,
            CONF_NODE_ID: NODE_ID,
            CONF_RECEIVER_HOST: host,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_host"}


async def test_manual_flow_reports_a_box_that_is_not_there(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """Nothing retained on the topic means the box is not on this broker."""
    result = await _start_manual_flow(hass)

    with patch("custom_components.enigma2_mqtt.config_flow.PROBE_TIMEOUT", 0.01):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: "vuuno4kse_000000"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_found"}

    # The form is retryable, so correcting the node id in the same flow adds the box.
    retained[INFO_TOPIC] = json.dumps(INFO)
    await async_arm_ha_mode_ack(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_NODE_ID] == NODE_ID


async def test_manual_flow_reports_a_box_that_does_not_acknowledge(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """The box is there, but it never confirms the mode switch."""
    result = await _start_manual_flow(hass)

    retained[INFO_TOPIC] = json.dumps(INFO)

    with patch("custom_components.enigma2_mqtt.config_flow.ACK_TIMEOUT", 0.01):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_ack"}

    # Nothing was written, so the same form adds the box once the plugin answers.
    await async_arm_ha_mode_ack(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_manual_flow_aborts_when_already_configured(
    hass: HomeAssistant, mqtt_mock, config_entry: MockConfigEntry
) -> None:
    """The node id is the unique id, whichever path added the box."""
    config_entry.add_to_hass(hass)

    result = await _start_manual_flow(hass)
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
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str],
    base_topic: str,
    node_id: str,
) -> None:
    """A wildcard would subscribe to every box on the broker at once."""
    result = await _start_manual_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_TOPIC: base_topic, CONF_NODE_ID: node_id},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_topic"}

    # Nothing was subscribed to, and a sound pair in the same form still works.
    retained[INFO_TOPIC] = json.dumps(INFO)
    await async_arm_ha_mode_ack(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_manual_flow_falls_back_to_the_node_id_as_a_name(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """An empty name is not an empty device page."""
    result = await _start_manual_flow(hass)

    retained[INFO_TOPIC] = json.dumps(INFO)
    await async_arm_ha_mode_ack(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID, CONF_NAME: "  "},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NODE_ID


async def test_a_retained_info_is_not_an_acknowledgement(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """A box that is switched off cannot confirm anything.

    Its retained `info` may already say `integration` — from the last time it was set
    up — and the broker replays that on every new subscription. Accepting it would add
    a device for a receiver nobody asked and nobody answered.
    """
    retained[INFO_TOPIC] = json.dumps({**INFO, "ha_mode": "integration"})

    result = await _start_manual_flow(hass)
    with patch("custom_components.enigma2_mqtt.config_flow.ACK_TIMEOUT", 0.05):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_ack"}

    # The live box publishing the same payload is accepted, because it is not retained.
    await async_arm_ha_mode_ack(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_discovered_flow_aborts_when_mqtt_went_away(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """MQTT can be removed between the announcement and the confirm."""
    result = await _start_discovered_flow(hass, json.dumps(ANNOUNCEMENT))

    with patch(
        "homeassistant.components.mqtt.async_wait_for_mqtt_client", return_value=False
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mqtt_unavailable"


async def test_manual_flow_aborts_without_mqtt(hass: HomeAssistant, mqtt_mock) -> None:
    """Without a broker there is nothing to subscribe to, and saying so beats a traceback."""
    with patch(
        "homeassistant.components.mqtt.async_wait_for_mqtt_client", return_value=False
    ):
        result = await _start_manual_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mqtt_unavailable"


async def test_a_second_announcement_does_not_start_a_second_flow(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """The plugin republishes its announcement on every reconnect.

    With the confirm card open that is a second discovery of the same box, and two
    cards for one receiver is how a user ends up with two entries for it.
    """
    first = await _start_discovered_flow(hass, json.dumps(ANNOUNCEMENT))
    assert first["type"] is FlowResultType.FORM

    second = await _start_discovered_flow(hass, json.dumps(ANNOUNCEMENT))

    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_the_command_waits_for_the_subscription_to_reach_the_broker(
    hass: HomeAssistant, mqtt_mock, mqtt_client_mock
) -> None:
    """The receiver answers faster than Home Assistant sends its SUBSCRIBE.

    Subscriptions are batched behind a debouncer, so a command published the instant a
    subscription exists in process is answered while the broker is still not sending
    that topic anywhere. Measured against a real receiver, the answer came in 0.1 s and
    the flow still timed out at 10 s. The SUBSCRIBE has to be on the wire first.
    """
    result = await _start_discovered_flow(hass, json.dumps(ANNOUNCEMENT))
    await async_arm_ha_mode_ack(hass)
    mqtt_client_mock.reset_mock()

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    traffic = [call for call in mqtt_client_mock.mock_calls
               if call[0] in ("subscribe", "publish")]
    subscribed_at = next(
        i for i, call in enumerate(traffic)
        if call[0] == "subscribe" and INFO_TOPIC in str(call)
    )
    published_at = next(
        i for i, call in enumerate(traffic)
        if call[0] == "publish" and HA_MODE_TOPIC in str(call)
    )
    assert subscribed_at < published_at

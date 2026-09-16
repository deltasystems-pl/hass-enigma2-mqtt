"""Fixtures shared by the Enigma2 MQTT tests.

The box in every fixture is the documentation's example receiver: node id
`vuuno4kse_005301`, MAC `00:00:5e:00:53:01` (the IEEE documentation range) and IP
`192.0.2.12` (RFC 5737 TEST-NET-1). No real receiver appears in this repository.
"""

from __future__ import annotations

from collections.abc import Generator
import json
from typing import Any
from unittest.mock import patch

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_NAME,
    CONF_NODE_ID,
    DISCOVERY_PREFIX,
    DOMAIN,
)

NODE_ID = "vuuno4kse_005301"
BASE_TOPIC = "enigma2"
BOX_NAME = "Dekoder salon"
BOXTYPE = "vuuno4kse"
IMAGE = "OpenViX 6.6.007"
MAC = "00:00:5e:00:53:01"
IP = "192.0.2.12"
PLUGIN_VERSION = "0.1.0"

CAPABILITIES = [
    "power",
    "service",
    "epg",
    "tuner",
    "recording",
    "timers",
    "volume",
    "keys",
    "screenshot",
    "message",
    "hdd",
]

ANNOUNCEMENT_TOPIC = f"{DISCOVERY_PREFIX}/{NODE_ID}/config"
INFO_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/info"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/availability"
HA_MODE_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/cmd/ha_mode"

ANNOUNCEMENT: dict[str, Any] = {
    "node_id": NODE_ID,
    "name": BOX_NAME,
    "base_topic": BASE_TOPIC,
    "image": IMAGE,
    "enigma": "5.4",
    "plugin": PLUGIN_VERSION,
    "boxtype": BOXTYPE,
    "mac": MAC,
    "ip": IP,
    "capabilities": CAPABILITIES,
    "ha_mode": "discovery",
}

INFO: dict[str, Any] = {
    "image": IMAGE,
    "enigma": "5.4",
    "plugin": PLUGIN_VERSION,
    "boxtype": BOXTYPE,
    "mac": MAC,
    "ip": IP,
    "uptime": 384210,
    "ha_mode": "discovery",
    "capabilities": CAPABILITIES,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Load `custom_components/` in every test."""
    yield


@pytest.fixture
def retained(hass: HomeAssistant) -> Generator[dict[str, str]]:
    """Give the fake broker a retained store.

    Everything this integration reads is retained, and a subscriber is supposed to get
    that payload the moment it subscribes. The MQTT test harness has no retained store,
    so without this a test would have to guess when the subscription landed and fire the
    message at it. Put a payload in the returned mapping and any subscription to that
    topic receives it, exactly as a broker would deliver it.
    """
    store: dict[str, str] = {}
    real_subscribe = mqtt.async_subscribe

    async def _subscribe(
        hass_: HomeAssistant,
        topic: str,
        msg_callback: Any,
        qos: int = 0,
        encoding: str | None = "utf-8",
    ):
        unsubscribe = await real_subscribe(hass_, topic, msg_callback, qos, encoding)
        if (payload := store.get(topic)) is not None:
            async_fire_mqtt_message(hass_, topic, payload, retain=True)
        return unsubscribe

    with patch("homeassistant.components.mqtt.async_subscribe", _subscribe):
        yield store


@pytest.fixture
def box_on_the_broker(retained: dict[str, str]) -> dict[str, str]:
    """Put the example box's retained topics on the broker."""
    retained[ANNOUNCEMENT_TOPIC] = json.dumps(ANNOUNCEMENT)
    retained[INFO_TOPIC] = json.dumps(INFO)
    retained[AVAILABILITY_TOPIC] = "online"
    return retained


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry for the example box."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=BOX_NAME,
        unique_id=NODE_ID,
        data={
            CONF_NODE_ID: NODE_ID,
            CONF_BASE_TOPIC: BASE_TOPIC,
            CONF_NAME: BOX_NAME,
        },
    )


async def async_arm_ha_mode_ack(
    hass: HomeAssistant, info: dict[str, Any] | None = None
) -> None:
    """Answer `cmd/ha_mode` with an `info` payload that echoes the new mode.

    The plugin acknowledges a mode switch by republishing `info`, so a fake box is one
    subscription: the MQTT test harness loops a published message back to its
    subscribers, which makes the acknowledgement arrive inside the publish itself
    rather than after a timer nobody can predict.
    """
    payload = dict(info if info is not None else INFO)

    @callback
    def _command_received(msg: mqtt.models.ReceiveMessage) -> None:
        mode = msg.payload
        if isinstance(mode, (bytes, bytearray)):
            mode = mode.decode()
        async_fire_mqtt_message(
            hass, INFO_TOPIC, json.dumps({**payload, "ha_mode": mode})
        )

    await mqtt.async_subscribe(hass, HA_MODE_TOPIC, _command_received)

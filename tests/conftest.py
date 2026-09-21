"""Fixtures shared by the Enigma2 MQTT tests.

The box in every fixture is the documentation's example receiver: node id
`vuuno4kse_005301`, MAC `00:00:5e:00:53:01` (the IEEE documentation range) and IP
`192.0.2.12` (RFC 5737 TEST-NET-1). No real receiver appears in this repository.

Two fixtures do the heavy lifting. `retained` gives the fake broker the retained store
the MQTT test harness does not have, including for wildcard subscriptions — which the
integration relies on, since one subscription covers every topic of a box.
`box_on_the_broker` fills that store with a receiver that is switched on and watching
television, so a test can assert what an entity says without publishing anything first.
"""

from __future__ import annotations

from collections.abc import Generator
import json
from typing import Any
from unittest.mock import patch

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
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
    SUPPORTED_PLUGIN_VERSION,
)

NODE_ID = "vuuno4kse_005301"
BASE_TOPIC = "enigma2"
BOX_NAME = "Dekoder salon"
BOXTYPE = "vuuno4kse"
IMAGE = "OpenViX 6.6.007"
MAC = "00:00:5e:00:53:01"
IP = "192.0.2.12"
# The example box runs the plugin this release ships, so "nothing to install" is the
# default state of the update entity and a lower version in a test means something.
PLUGIN_VERSION = SUPPORTED_PLUGIN_VERSION

# The entity id every entity of the example box is prefixed with: Home Assistant builds
# it from the device name and the entity's English name.
SLUG = "dekoder_salon"

CAPABILITIES = [
    "power",
    "service",
    "epg",
    "epg_grid",
    "tuner",
    "recording",
    "timers",
    "volume",
    "channels",
    "keys",
    "screenshot",
    "message",
    "hdd",
]

ANNOUNCEMENT_TOPIC = f"{DISCOVERY_PREFIX}/{NODE_ID}/config"
INFO_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/info"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/availability"
POWER_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/power"
SERVICE_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/service"
EPG_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/epg"
TUNER_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/tuner"
RECORDING_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/recording"
TIMERS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/timers"
VOLUME_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/volume"
HDD_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/hdd"
SCREEN_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/screen"
KEY_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/key"
LAST_ERROR_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/last_error"
CHANNELS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/channels"
BOUQUET_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/bouquet"
EPG_GRID_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/epg_grid/ulubione_tv"
HA_MODE_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/cmd/ha_mode"


def command_topic(name: str) -> str:
    """Return one of the example box's command topics."""
    return f"{BASE_TOPIC}/{NODE_ID}/cmd/{name}"


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

SREF = "1:0:19:283D:3FB:1:C00000:0:0:0:"
SREF_TWO = "1:0:19:2B66:3F3:1:C00000:0:0:0:"
PICON_URL = f"http://{IP}/picon/1_0_19_283D_3FB_1_C00000_0_0_0.png"

SERVICE: dict[str, Any] = {
    "sref": SREF,
    "name": "TVP 1 HD",
    "bouquet": "Ulubione TV",
    "provider": "Cyfrowy Polsat",
    "width": 1920,
    "height": 1080,
}

EPG: dict[str, Any] = {
    "now": {
        "title": "Wiadomości",
        "begin": 1789459200,
        "end": 1789460700,
        "event_id": 27431,
        "short": "Serwis informacyjny",
        "long": "Najważniejsze wydarzenia dnia.",
    },
    "next": {
        "title": "Pogoda",
        "begin": 1789460700,
        "end": 1789461000,
        "event_id": 27432,
        "short": "",
        "long": "",
    },
}

TUNER: dict[str, Any] = {"snr": 78, "agc": 62, "ber": 0, "tuner": "A"}

RECORDING_IDLE: dict[str, Any] = {
    "active": [],
    "next": {
        "name": "Pogoda",
        "sref": SREF,
        "begin": 1789460700,
        "end": 1789461000,
    },
}

RECORDING_ACTIVE: dict[str, Any] = {
    "active": [
        {
            "name": "Wiadomości",
            "sref": SREF,
            "begin": 1789459200,
            "end": 1789460700,
        }
    ],
    "next": None,
}

TIMERS: list[dict[str, Any]] = [
    {
        "name": "Wiadomości",
        "sref": SREF,
        "begin": 1789459200,
        "end": 1789460700,
        "state": "waiting",
        "repeated": 0,
    }
]

VOLUME: dict[str, Any] = {"level": 35, "muted": False}
HDD: dict[str, Any] = {"mounted": True, "path": "/media/hdd", "free_mb": 412330}

# Not a real picture — nothing in the integration decodes it, and a test fixture that
# is a real JPEG only makes the diff harder to read.
SCREEN = b"\xff\xd8\xff\xdb--not-really-a-jpeg--\xff\xd9"

CHANNELS: dict[str, Any] = {
    "generated": 1789459200,
    "bouquets": [
        {
            "name": "Ulubione TV",
            "sref": "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"userbouquet.fav.tv\"",
            "channels": [
                {"sref": SREF, "name": "TVP 1 HD"},
                {"sref": SREF_TWO, "name": "TVN HD"},
            ],
        },
        {
            "name": "Sport",
            "sref": "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"userbouquet.sport.tv\"",
            "channels": [
                {"sref": "1:0:19:1234:3F3:1:C00000:0:0:0:", "name": "Eurosport 1"},
                # The same name in two bouquets is the case `select_source` has to
                # resolve without asking the box to guess.
                {"sref": "1:0:19:5678:3F3:1:C00000:0:0:0:", "name": "TVN HD"},
            ],
        },
    ],
}

BOUQUET: dict[str, Any] = {
    "name": "Ulubione TV",
    "sref": CHANNELS["bouquets"][0]["sref"],
}

EPG_GRID: dict[str, Any] = {
    "bouquet": "Ulubione TV",
    "generated": 1789459200,
    "channels": [
        {
            "sref": SREF,
            "name": "TVP 1 HD",
            "events": [
                {
                    "title": "Wiadomości",
                    "begin": 1789459200,
                    "end": 1789460700,
                    "event_id": 27431,
                }
            ],
        }
    ],
}


def topic_matches(subscription: str, topic: str) -> bool:
    """Return whether an MQTT topic filter matches a topic, wildcards included."""
    filter_parts = subscription.split("/")
    topic_parts = topic.split("/")
    for index, part in enumerate(filter_parts):
        if part == "#":
            return index <= len(topic_parts)
        if index >= len(topic_parts):
            return False
        if part not in ("+", topic_parts[index]):
            return False
    return len(filter_parts) == len(topic_parts)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Load `custom_components/` in every test."""
    yield


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Tolerate the MQTT integration's own periodic timer.

    `mqtt_mock` sets up the real MQTT integration, which schedules
    `MQTT._async_start_misc_periodic` and does not cancel it on teardown. Home
    Assistant's own MQTT tests make the same allowance; it says nothing about this
    integration.
    """
    return True


@pytest.fixture
def retained(hass: HomeAssistant) -> Generator[dict[str, str | bytes]]:
    """Give the fake broker a retained store.

    Everything this integration reads is retained, and a subscriber is supposed to get
    that payload the moment it subscribes. The MQTT test harness has no retained store,
    so without this a test would have to guess when the subscription landed and fire the
    message at it. Put a payload in the returned mapping and any subscription that
    matches that topic receives it, exactly as a broker would deliver it — including the
    `<base>/<node>/#` subscription the integration actually uses.
    """
    store: dict[str, str | bytes] = {}
    real_subscribe = mqtt.async_subscribe

    async def _subscribe(
        hass_: HomeAssistant,
        topic: str,
        msg_callback: Any,
        qos: int = 0,
        encoding: str | None = "utf-8",
    ):
        unsubscribe = await real_subscribe(hass_, topic, msg_callback, qos, encoding)
        for stored_topic, payload in list(store.items()):
            if topic_matches(topic, stored_topic):
                async_fire_mqtt_message(hass_, stored_topic, payload, retain=True)
        return unsubscribe

    with patch("homeassistant.components.mqtt.async_subscribe", _subscribe):
        yield store


@pytest.fixture
def box_on_the_broker(retained: dict[str, str | bytes]) -> dict[str, str | bytes]:
    """Put a switched-on receiver's retained topics on the broker."""
    retained.update(
        {
            ANNOUNCEMENT_TOPIC: json.dumps(ANNOUNCEMENT),
            INFO_TOPIC: json.dumps(INFO),
            AVAILABILITY_TOPIC: "online",
            POWER_TOPIC: "on",
            SERVICE_TOPIC: json.dumps(SERVICE),
            EPG_TOPIC: json.dumps(EPG),
            TUNER_TOPIC: json.dumps(TUNER),
            RECORDING_TOPIC: json.dumps(RECORDING_IDLE),
            TIMERS_TOPIC: json.dumps(TIMERS),
            VOLUME_TOPIC: json.dumps(VOLUME),
            HDD_TOPIC: json.dumps(HDD),
            SCREEN_TOPIC: SCREEN,
            CHANNELS_TOPIC: json.dumps(CHANNELS),
        }
    )
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


async def async_setup_box(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry to Home Assistant and set it up."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def async_setup_box_then_retained(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    retained_store: dict[str, str | bytes],
) -> None:
    """Set the entry up first, and let the retained burst arrive afterwards.

    This is the order a real broker produces and `async_setup_box` does not.
    `box.async_start` registers the subscription and returns, Home Assistant holds the
    SUBSCRIBE packet behind a tenth of a second of debouncing, and the platforms are set
    up in the meantime — so `info`, `last_error` and the rest land on entities that
    already exist. The `retained` fixture delivers them inside `mqtt.async_subscribe`
    instead, which is the opposite order, and it hides every bug that depends on what a
    thing knew before the box had said anything.

    Use this for anything whose behaviour turns on that difference; the other order is
    still the right one for the many tests that only want a box already in a state.
    """
    burst = dict(retained_store)
    retained_store.clear()
    await async_setup_box(hass, entry)
    for topic, payload in burst.items():
        async_fire_mqtt_message(hass, topic, payload, retain=True)
    await hass.async_block_till_done()


async def async_arm_ha_mode_ack(
    hass: HomeAssistant,
    info: dict[str, Any] | None = None,
    *,
    base_topic: str = BASE_TOPIC,
    node_id: str = NODE_ID,
) -> None:
    """Answer `cmd/ha_mode` with an `info` payload that echoes the new mode.

    The plugin acknowledges a mode switch by republishing `info`, so a fake box is one
    subscription: the MQTT test harness loops a published message back to its
    subscribers, which makes the acknowledgement arrive inside the publish itself
    rather than after a timer nobody can predict.
    """
    payload = dict(info if info is not None else INFO)

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        mode = msg.payload
        if isinstance(mode, (bytes, bytearray)):
            mode = mode.decode()
        async_fire_mqtt_message(
            hass,
            f"{base_topic}/{node_id}/info",
            json.dumps({**payload, "ha_mode": mode}),
        )

    await mqtt.async_subscribe(
        hass, f"{base_topic}/{node_id}/cmd/ha_mode", _command_received
    )


async def async_arm_box_reply(
    hass: HomeAssistant, command: str, topic: str, payload: str | bytes
) -> None:
    """Make the fake box answer one command by publishing a state topic.

    This is what a receiver does: there is no acknowledgement, only the topic that
    moves. Arming a reply before calling an action is therefore the whole of "the box
    did as it was told" in these tests.
    """

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, topic, payload)

    await mqtt.async_subscribe(hass, command_topic(command), _command_received)


async def async_arm_box_error(hass: HomeAssistant, command: str, error: str) -> None:
    """Make the fake box refuse one command, the way the contract says it does."""

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps({"cmd": command, "error": error, "ts": 1789459213}),
        )

    await mqtt.async_subscribe(hass, command_topic(command), _command_received)


def assert_published(mqtt_mock: Any, topic: str, payload: str) -> None:
    """Assert that exactly this command went out, at the QoS the contract sets.

    Commands are QoS 1 and never retained; a test that only checked the payload would
    pass on a retained command, which is the mistake the plugin refuses to act on.
    """
    mqtt_mock.async_publish.assert_any_call(
        topic, payload, 1, False, message_expiry_interval=None
    )


def published_payloads(mqtt_mock: Any, topic: str) -> list[str]:
    """Return every payload published to one topic, in order."""
    return [
        call.args[1]
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == topic
    ]


async def async_arm_box_ack(hass: HomeAssistant, command: str) -> None:
    """Make the fake box clear `last_error` when it carries a command out.

    The contract has no acknowledgement topic, so a command with no state topic of its
    own — `send_key`, `message` — is only ever answered by silence or by a complaint.
    Clearing `last_error` is the one positive signal the plugin does publish, and the
    integration takes it as one; a box that does not send it simply costs the caller
    the grace period.
    """

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")

    await mqtt.async_subscribe(hass, command_topic(command), _command_received)

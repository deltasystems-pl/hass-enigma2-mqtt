"""The runtime object that represents one Enigma2 box on the broker.

Everything the integration knows about a box arrives on retained MQTT topics, so this
object is a cache with subscriptions rather than a client: it holds the last
announcement, the last `info` payload and the availability flag, and it publishes
commands. The entity platforms (M3) read it and register listeners.

The two module level helpers are used by the config flow, which has no runtime object
yet because the config entry does not exist while the flow is running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import (
    ACK_TIMEOUT,
    CONF_BASE_TOPIC,
    CONF_NAME,
    CONF_NODE_ID,
    DEFAULT_BASE_TOPIC,
    DEFAULT_MANUFACTURER,
    DISCOVERY_PREFIX,
    DOMAIN,
    MANUFACTURERS,
    PAYLOAD_ONLINE,
    PROBE_TIMEOUT,
    TOPIC_AVAILABILITY,
    TOPIC_INFO,
)

_LOGGER = logging.getLogger(__name__)

type Enigma2MqttConfigEntry = ConfigEntry[Enigma2Box]


def state_topic(base_topic: str, node_id: str, suffix: str) -> str:
    """Return a state topic of one box."""
    return f"{base_topic}/{node_id}/{suffix}"


def command_topic(base_topic: str, node_id: str, name: str) -> str:
    """Return a command topic of one box."""
    return f"{base_topic}/{node_id}/cmd/{name}"


def announcement_topic(node_id: str) -> str:
    """Return the announcement topic of one box."""
    return f"{DISCOVERY_PREFIX}/{node_id}/config"


def parse_json_payload(payload: Any) -> dict[str, Any] | None:
    """Return a payload decoded as a JSON object, or None.

    An empty payload is a retraction and a payload that is not an object is not part of
    this contract; both mean "nothing to read here", which is what None says.
    """
    if not payload:
        return None
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        decoded = json.loads(payload)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def manufacturer_for(boxtype: str | None) -> str:
    """Return the vendor that sells a box type, by its prefix."""
    if not boxtype:
        return DEFAULT_MANUFACTURER
    lowered = boxtype.lower()
    for prefix in sorted(MANUFACTURERS, key=len, reverse=True):
        if lowered.startswith(prefix):
            return MANUFACTURERS[prefix]
    return DEFAULT_MANUFACTURER


def software_version(info: dict[str, Any]) -> str | None:
    """Return the version string shown on the device page.

    Two versions matter and neither alone is enough: the image the box runs and the
    plugin that talks to us. A bug report needs both.
    """
    image = info.get("image")
    plugin = info.get("plugin")
    if image and plugin:
        return f"{image} · plugin {plugin}"
    if plugin:
        return f"plugin {plugin}"
    return image or None


async def async_wait_for_info(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    timeout: float = PROBE_TIMEOUT,
) -> dict[str, Any] | None:
    """Return the box's retained `info` payload, or None if nothing arrives.

    This is the "is that box really there" probe of the manual config flow. `info` is
    retained, so a box that is on the broker answers as fast as the broker can deliver.
    """
    return await _async_wait_for_info_matching(hass, base_topic, node_id, None, timeout)


async def async_request_ha_mode(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    mode: str,
    timeout: float = ACK_TIMEOUT,
) -> dict[str, Any] | None:
    """Switch a box's `ha_mode` and return the `info` payload that acknowledges it.

    There is no acknowledgement topic in the contract: the plugin answers by
    republishing `info` with the new mode. Subscribing before publishing is not
    optional — the plugin can answer inside the same publish burst.
    """
    subscription = await _async_subscribe_info(hass, base_topic, node_id, mode)
    future, unsubscribe = subscription
    try:
        await mqtt.async_publish(
            hass,
            command_topic(base_topic, node_id, "ha_mode"),
            mode,
            qos=1,
            retain=False,
        )
        async with asyncio.timeout(timeout):
            return await future
    except TimeoutError:
        _LOGGER.debug("Box %s did not acknowledge ha_mode=%s", node_id, mode)
        return None
    finally:
        unsubscribe()


async def _async_wait_for_info_matching(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    mode: str | None,
    timeout: float,
) -> dict[str, Any] | None:
    """Wait for an `info` payload, optionally one that reports a given mode."""
    future, unsubscribe = await _async_subscribe_info(hass, base_topic, node_id, mode)
    try:
        async with asyncio.timeout(timeout):
            return await future
    except TimeoutError:
        return None
    finally:
        unsubscribe()


async def _async_subscribe_info(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    mode: str | None,
) -> tuple[asyncio.Future[dict[str, Any]], CALLBACK_TYPE]:
    """Subscribe to `info` and resolve a future on the first payload that matches."""
    future: asyncio.Future[dict[str, Any]] = hass.loop.create_future()

    @callback
    def _message_received(msg: ReceiveMessage) -> None:
        if future.done():
            return
        if (info := parse_json_payload(msg.payload)) is None:
            return
        if mode is not None and info.get("ha_mode") != mode:
            return
        future.set_result(info)

    unsubscribe = await mqtt.async_subscribe(
        hass, state_topic(base_topic, node_id, TOPIC_INFO), _message_received
    )
    return future, unsubscribe


class Enigma2Box:
    """One Enigma2 receiver, as seen through the broker."""

    def __init__(self, hass: HomeAssistant, entry: Enigma2MqttConfigEntry) -> None:
        """Initialise the box from its config entry."""
        self.hass = hass
        self.entry = entry
        self.node_id: str = entry.data[CONF_NODE_ID]
        self.base_topic: str = entry.data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC)
        self.name: str = entry.data.get(CONF_NAME) or self.node_id
        self.available: bool = False
        self.info: dict[str, Any] = {}
        self.announcement: dict[str, Any] = {}
        self._unsubscribes: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []

    @property
    def capabilities(self) -> list[str]:
        """Return the feature areas the plugin managed to hook on this image."""
        source = self.info or self.announcement
        capabilities = source.get("capabilities")
        return list(capabilities) if isinstance(capabilities, list) else []

    def state_topic(self, suffix: str) -> str:
        """Return one of this box's state topics."""
        return state_topic(self.base_topic, self.node_id, suffix)

    def command_topic(self, name: str) -> str:
        """Return one of this box's command topics."""
        return command_topic(self.base_topic, self.node_id, name)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Register a callback for every change, and return its remover."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    async def async_start(self) -> None:
        """Subscribe to the topics that describe the box."""
        self._unsubscribes = [
            await mqtt.async_subscribe(
                self.hass,
                self.state_topic(TOPIC_AVAILABILITY),
                self._availability_received,
            ),
            await mqtt.async_subscribe(
                self.hass, self.state_topic(TOPIC_INFO), self._info_received
            ),
            await mqtt.async_subscribe(
                self.hass,
                announcement_topic(self.node_id),
                self._announcement_received,
            ),
        ]

    @callback
    def async_stop(self) -> None:
        """Unsubscribe from everything this box subscribed to."""
        while self._unsubscribes:
            self._unsubscribes.pop()()

    async def async_publish_cmd(
        self, name: str, payload: str, qos: int = 1
    ) -> None:
        """Send a command to the box.

        Commands are never retained: a retained command is delivered again the instant
        the plugin subscribes, so the box would obey it after every reboot.
        """
        await mqtt.async_publish(
            self.hass, self.command_topic(name), payload, qos=qos, retain=False
        )

    async def async_request_ha_mode(
        self, mode: str, timeout: float = ACK_TIMEOUT
    ) -> bool:
        """Switch the box's Home Assistant mode and wait for the acknowledgement."""
        info = await async_request_ha_mode(
            self.hass, self.base_topic, self.node_id, mode, timeout
        )
        if info is None:
            return False
        self.info = info
        self._async_updated()
        return True

    @callback
    def async_register_device(self) -> dr.DeviceEntry:
        """Create or update this box's entry in the device registry."""
        source = {**self.announcement, **self.info}
        name = self.announcement.get("name") or self.name
        boxtype = source.get("boxtype")
        ip_address = source.get("ip")
        return dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.node_id)},
            name=name,
            manufacturer=manufacturer_for(boxtype),
            model=boxtype,
            sw_version=software_version(source),
            configuration_url=f"http://{ip_address}/" if ip_address else None,
        )

    @callback
    def _availability_received(self, msg: ReceiveMessage) -> None:
        """Track the box's last will."""
        payload = msg.payload
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        available = str(payload).strip() == PAYLOAD_ONLINE
        if available == self.available:
            return
        self.available = available
        _LOGGER.debug(
            "Box %s is %s", self.node_id, "online" if available else "offline"
        )
        self._async_updated()

    @callback
    def _info_received(self, msg: ReceiveMessage) -> None:
        """Track the box's `info` topic and keep the device page current."""
        if (info := parse_json_payload(msg.payload)) is None:
            return
        self.info = info
        self.async_register_device()
        self._async_updated()

    @callback
    def _announcement_received(self, msg: ReceiveMessage) -> None:
        """Track the box's announcement, which carries the user's chosen name."""
        if (announcement := parse_json_payload(msg.payload)) is None:
            return
        self.announcement = announcement
        self.async_register_device()
        self._async_updated()

    @callback
    def _async_updated(self) -> None:
        """Tell every listener that something about this box changed."""
        for listener in list(self._listeners):
            listener()

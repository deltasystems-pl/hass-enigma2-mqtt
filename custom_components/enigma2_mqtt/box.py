"""The runtime object that represents one Enigma2 box on the broker.

Everything the integration knows about a box arrives on retained MQTT topics, so this
object is a cache with subscriptions rather than a client: it holds the last payload of
every state topic, and it publishes commands. The entity platforms read it and register
listeners; nothing below this module knows that MQTT exists.

One wildcard subscription covers every state topic of a box. That is a single SUBSCRIBE
packet instead of fourteen, one retained burst instead of fourteen, and — because the
`screen` topic is raw JPEG and the rest is UTF-8 JSON — it has to be taken without an
encoding and decoded per topic, which is what `_message_received` does. The box's own
`cmd/#` echoes arrive on it too and are dropped: a broker delivers what a client
publishes back to that client's matching subscriptions, and a command is not state.

The module level helpers at the top are used by the config flow, which has no runtime
object yet because the config entry does not exist while the flow is running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
import homeassistant.util.dt as dt_util

from .const import (
    ACK_TIMEOUT,
    ATTR_KEY,
    ATTR_NODE_ID,
    ATTR_PRESS,
    COMMAND_TIMEOUT,
    CONF_BASE_TOPIC,
    CONF_BOUQUETS,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_WOL_MAC,
    DEFAULT_BASE_TOPIC,
    DEFAULT_MANUFACTURER,
    DISCOVERY_PREFIX,
    DOMAIN,
    ERROR_GRACE,
    EVENT_KEY,
    KEY_PREFIX,
    MANUFACTURERS,
    PAYLOAD_ONLINE,
    POWER_ON,
    PRESS_LONG,
    PRESS_SHORT,
    PROBE_TIMEOUT,
    SUBSCRIBE_TIMEOUT,
    TOPIC_AVAILABILITY,
    TOPIC_CHANNELS,
    TOPIC_EPG,
    TOPIC_EPG_GRID,
    TOPIC_HDD,
    TOPIC_INFO,
    TOPIC_KEY,
    TOPIC_LAST_ERROR,
    TOPIC_POWER,
    TOPIC_RECORDING,
    TOPIC_SCREEN,
    TOPIC_SERVICE,
    TOPIC_TIMERS,
    TOPIC_TUNER,
    TOPIC_VOLUME,
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


def decode_payload(payload: Any) -> str | None:
    """Return a payload as text, or None if it is empty or not text at all."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str) or not payload:
        return None
    return payload


def parse_json_payload(payload: Any) -> dict[str, Any] | None:
    """Return a payload decoded as a JSON object, or None.

    An empty payload is a retraction and a payload that is not an object is not part of
    this contract; both mean "nothing to read here", which is what None says.
    """
    if (text := decode_payload(payload)) is None:
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def parse_json_list(payload: Any) -> list[Any] | None:
    """Return a payload decoded as a JSON list, or None. `timers` is a list."""
    if (text := decode_payload(payload)) is None:
        return None
    try:
        decoded = json.loads(text)
    except ValueError:
        return None
    return decoded if isinstance(decoded, list) else None


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


def normalise_key(command: str) -> str:
    """Return a remote key name the plugin will recognise.

    `red`, `Red`, `key_red` and `KEY_RED` are the same key: the topic spells them the
    way the Linux input layer does, and a person writing an automation does not. Spaces
    are dropped rather than turned into underscores, because the Linux names run the
    words together — `channel up` is `KEY_CHANNELUP`, never `KEY_CHANNEL_UP`. A name
    this does not recognise is passed through untouched, because the box is the side
    that knows which keys it has and it answers on `last_error` when it does not.
    """
    key = command.strip().upper().replace(" ", "")
    if not key.startswith(KEY_PREFIX):
        key = f"{KEY_PREFIX}{key}"
    return key


def picon_url(ip_address: str | None, sref: str | None) -> str | None:
    """Return the URL OpenWebif serves a channel's picon at, if it can be built.

    Picons are not published over MQTT — a few hundred kilobytes of PNG per channel on
    a retained topic would be an abuse of a broker — so the browser fetches them from
    the box, which already serves them at a name derived from the service reference:
    every colon becomes an underscore and the trailing one is dropped.

    Everything about this is best effort. No address, no picon; no picon on the box,
    a broken image the media browser simply does not draw.
    """
    if not ip_address or not sref:
        return None
    return f"http://{ip_address}/picon/{sref.replace(':', '_').rstrip('_')}.png"


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
    republishing `info` with the new mode, and that answer comes within a fraction of
    a second.

    Subscribing before publishing is therefore not enough. Home Assistant batches its
    SUBSCRIBE packets behind a short debouncer, so a subscription that exists in
    process still reaches the broker a tenth of a second later — about when the
    receiver replies. The answer is then published to a broker that is not yet sending
    this topic anywhere, and the only copy Home Assistant ever sees is the retained one
    that arrives with the subscription, which is not an acknowledgement. Waiting for
    the subscription to be established on the broker before the command goes out is
    what closes that race; measured against a real receiver, it is the difference
    between an acknowledgement in 0.1 s and a timeout at 10 s.
    """
    watch = await _async_watch_info(hass, base_topic, node_id, mode)
    try:
        await watch.async_wait_until_established()
        await mqtt.async_publish(
            hass,
            command_topic(base_topic, node_id, "ha_mode"),
            mode,
            qos=1,
            retain=False,
        )
        async with asyncio.timeout(timeout):
            return await watch.matched
    except TimeoutError:
        _LOGGER.debug("Box %s did not acknowledge ha_mode=%s", node_id, mode)
        return None
    finally:
        watch.cancel()


async def _async_wait_for_info_matching(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    mode: str | None,
    timeout: float,
) -> dict[str, Any] | None:
    """Wait for an `info` payload, optionally one that reports a given mode."""
    watch = await _async_watch_info(hass, base_topic, node_id, mode)
    try:
        async with asyncio.timeout(timeout):
            return await watch.matched
    except TimeoutError:
        return None
    finally:
        watch.cancel()


@dataclass(slots=True)
class _InfoWatch:
    """A live subscription to one box's `info` topic."""

    matched: asyncio.Future[dict[str, Any]]
    established: asyncio.Future[None]
    cancel: CALLBACK_TYPE

    async def async_wait_until_established(
        self, timeout: float = SUBSCRIBE_TIMEOUT
    ) -> None:
        """Wait until the broker is actually sending this topic to us.

        Degrades rather than hangs: if the confirmation never comes, the caller goes
        ahead anyway and falls back on its own timeout.
        """
        with suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self.established


async def _async_watch_info(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    mode: str | None,
) -> _InfoWatch:
    """Subscribe to `info` and resolve a future on the first payload that matches."""
    topic = state_topic(base_topic, node_id, TOPIC_INFO)
    matched: asyncio.Future[dict[str, Any]] = hass.loop.create_future()
    established: asyncio.Future[None] = hass.loop.create_future()

    @callback
    def _message_received(msg: ReceiveMessage) -> None:
        if matched.done():
            return
        if (info := parse_json_payload(msg.payload)) is None:
            return
        if mode is not None and (msg.retain or info.get("ha_mode") != mode):
            # A retained payload is what the broker had before we asked, so it can
            # never be the answer to the command we just sent: a box that is switched
            # off but was last in this mode would otherwise acknowledge instantly. A
            # real acknowledgement is a fresh publish, and a broker clears the retain
            # flag on everything it delivers to a subscription that is already open.
            return
        matched.set_result(info)

    @callback
    def _subscription_established() -> None:
        if not established.done():
            established.set_result(None)

    # Registered before subscribing, so that a subscription completing immediately
    # cannot land between the two calls and go unnoticed.
    stop_tracking = mqtt.async_on_subscribe_done(
        hass, topic, mqtt.DEFAULT_QOS, _subscription_established
    )
    unsubscribe = await mqtt.async_subscribe(hass, topic, _message_received)

    @callback
    def _cancel() -> None:
        stop_tracking()
        unsubscribe()

    return _InfoWatch(matched=matched, established=established, cancel=_cancel)


class Enigma2CommandError(HomeAssistantError):
    """A command the box refused, or never carried out."""


@dataclass(slots=True)
class _Pending:
    """One command whose effect is being waited for."""

    cmd: str
    predicate: Callable[[], bool]
    future: asyncio.Future[None]

    @callback
    def resolve(self) -> None:
        """Report that the effect happened."""
        if not self.future.done():
            self.future.set_result(None)

    @callback
    def fail(self, error: str) -> None:
        """Report that the box complained about this command."""
        if not self.future.done():
            self.future.set_exception(
                Enigma2CommandError(
                    translation_domain=DOMAIN,
                    translation_key="command_failed",
                    translation_placeholders={"command": self.cmd, "error": error},
                )
            )


@dataclass(slots=True)
class _Listener:
    """A callback and the topics it cares about. No topics means every topic."""

    callback: Callable[[], None]
    topics: frozenset[str] | None = None

    def wants(self, suffix: str) -> bool:
        """Return whether this listener should hear about a topic."""
        return self.topics is None or suffix in self.topics


@dataclass(slots=True)
class Enigma2State:
    """The last payload of every state topic of one box.

    Kept as one object rather than a dozen attributes on the box so that a platform can
    be handed the state, a test can build one, and diagnostics can serialise it, all
    without either of them knowing which topic carried which field.
    """

    info: dict[str, Any] = field(default_factory=dict)
    announcement: dict[str, Any] = field(default_factory=dict)
    power: str | None = None
    service: dict[str, Any] | None = None
    epg: dict[str, Any] | None = None
    tuner: dict[str, Any] | None = None
    recording: dict[str, Any] | None = None
    timers: list[Any] | None = None
    volume: dict[str, Any] | None = None
    hdd: dict[str, Any] | None = None
    channels: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    screen: bytes | None = None
    screen_updated: datetime | None = None
    epg_grid: dict[str, dict[str, Any]] = field(default_factory=dict)


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
        self.device_id: str | None = None
        self.state = Enigma2State()
        self.seen: set[str] = set()
        # How many payloads each topic has delivered. A command whose effect is "this
        # topic was published again" — adding a timer, taking a screenshot — has no
        # value to compare, only the fact that something arrived.
        self.updates: dict[str, int] = {}
        self._unsubscribes: list[CALLBACK_TYPE] = []
        self._listeners: list[_Listener] = []
        self._key_listeners: list[Callable[[str, str], None]] = []
        self._pending: list[_Pending] = []
        self._subscribed = asyncio.Event()

    # ------------------------------------------------------------------ properties

    @property
    def info(self) -> dict[str, Any]:
        """Return the last `info` payload."""
        return self.state.info

    @property
    def announcement(self) -> dict[str, Any]:
        """Return the last announcement payload."""
        return self.state.announcement

    @property
    def capabilities(self) -> list[str]:
        """Return the feature areas the plugin managed to hook on this image."""
        source = self.state.info or self.state.announcement
        capabilities = source.get("capabilities")
        return list(capabilities) if isinstance(capabilities, list) else []

    @property
    def ip_address(self) -> str | None:
        """Return the box's LAN address, as it last reported it."""
        source = {**self.state.announcement, **self.state.info}
        ip_address = source.get("ip")
        return ip_address if isinstance(ip_address, str) and ip_address else None

    @property
    def mac_address(self) -> str | None:
        """Return the Wake-on-LAN target: the option if set, else what the box says.

        The override exists because the address that has to be woken is not always the
        one enigma2 reports — a box with both a wired and a wireless interface reports
        the one it is using, and the magic packet has to go to the one that is plugged
        in and listening.
        """
        if override := (self.entry.options.get(CONF_WOL_MAC) or "").strip():
            return override
        source = {**self.state.announcement, **self.state.info}
        mac = source.get("mac")
        return mac if isinstance(mac, str) and mac else None

    @property
    def is_on(self) -> bool:
        """Return whether the box is out of standby and reachable."""
        return self.available and self.state.power == POWER_ON

    @property
    def bouquets(self) -> list[dict[str, Any]]:
        """Return the bouquets of the `channels` topic, filtered by the options.

        An empty or absent option means every bouquet the plugin published, which is
        already the plugin's own `bouquets_for_select` selection: the option narrows
        what the box offers, it can never widen it.
        """
        channels = self.state.channels or {}
        bouquets = channels.get("bouquets")
        if not isinstance(bouquets, list):
            return []
        usable = [
            bouquet
            for bouquet in bouquets
            if isinstance(bouquet, dict) and isinstance(bouquet.get("channels"), list)
        ]
        if not (chosen := self.entry.options.get(CONF_BOUQUETS)):
            return usable
        wanted = set(chosen)
        return [bouquet for bouquet in usable if bouquet.get("name") in wanted]

    @property
    def bouquet_names(self) -> list[str]:
        """Return every bouquet name the box published, options ignored.

        The options flow offers this: a user cannot choose from a list that has already
        been filtered by the choice they are about to change.
        """
        channels = self.state.channels or {}
        bouquets = channels.get("bouquets")
        if not isinstance(bouquets, list):
            return []
        return [
            name
            for bouquet in bouquets
            if isinstance(bouquet, dict) and isinstance(name := bouquet.get("name"), str)
        ]

    @property
    def channel_names(self) -> list[str]:
        """Return the channel names of the selected bouquets, in bouquet order.

        Duplicates are dropped rather than numbered: the same channel in two bouquets
        is one channel, and a source list with "TVP 1 HD" twice helps nobody.
        """
        names: list[str] = []
        seen: set[str] = set()
        for bouquet in self.bouquets:
            for channel in bouquet["channels"]:
                if not isinstance(channel, dict):
                    continue
                name = channel.get("name")
                if isinstance(name, str) and name and name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def channels_named(self, name: str) -> list[dict[str, Any]]:
        """Return every channel of the selected bouquets with this exact name."""
        matches: list[dict[str, Any]] = []
        for bouquet in self.bouquets:
            for channel in bouquet["channels"]:
                if isinstance(channel, dict) and channel.get("name") == name:
                    matches.append(channel)
        return matches

    def bouquet_by_sref(self, sref: str) -> dict[str, Any] | None:
        """Return one selected bouquet by its service reference."""
        for bouquet in self.bouquets:
            if bouquet.get("sref") == sref:
                return bouquet
        return None

    def picon_url(self, sref: str | None) -> str | None:
        """Return the picon URL of a service on this box."""
        return picon_url(self.ip_address, sref)

    # ------------------------------------------------------------------- topics

    def state_topic(self, suffix: str) -> str:
        """Return one of this box's state topics."""
        return state_topic(self.base_topic, self.node_id, suffix)

    def command_topic(self, name: str) -> str:
        """Return one of this box's command topics."""
        return command_topic(self.base_topic, self.node_id, name)

    # ---------------------------------------------------------------- listeners

    @callback
    def async_add_listener(
        self, listener: Callable[[], None], topics: Iterable[str] | None = None
    ) -> CALLBACK_TYPE:
        """Register a callback for the topics it names, and return its remover.

        Availability is added to every filter: an entity that only reads `volume` still
        has to go unavailable when the box does, and a box that goes offline publishes
        nothing else to say so.
        """
        filtered = (
            None if topics is None else frozenset({*topics, TOPIC_AVAILABILITY})
        )
        entry = _Listener(callback=listener, topics=filtered)
        self._listeners.append(entry)

        @callback
        def _remove() -> None:
            self._listeners.remove(entry)

        return _remove

    @callback
    def async_add_key_listener(
        self, listener: Callable[[str, str], None]
    ) -> CALLBACK_TYPE:
        """Register a callback for key presses, and return its remover."""
        self._key_listeners.append(listener)

        @callback
        def _remove() -> None:
            self._key_listeners.remove(listener)

        return _remove

    # -------------------------------------------------------------- life cycle

    async def async_start(self) -> None:
        """Subscribe to everything this box publishes.

        The subscribe-done tracker is registered before the subscribe call, so that a
        subscription that completes immediately cannot land between the two and go
        unnoticed. Every command path waits on the event it sets before publishing,
        which is what keeps a command from racing the subscription meant to hear its
        answer.
        """
        wildcard = f"{self.base_topic}/{self.node_id}/#"

        @callback
        def _subscription_established() -> None:
            self._subscribed.set()

        self._unsubscribes.append(
            mqtt.async_on_subscribe_done(
                self.hass, wildcard, mqtt.DEFAULT_QOS, _subscription_established
            )
        )
        self._unsubscribes.append(
            await mqtt.async_subscribe(
                self.hass, wildcard, self._message_received, encoding=None
            )
        )
        self._unsubscribes.append(
            await mqtt.async_subscribe(
                self.hass, announcement_topic(self.node_id), self._announcement_received
            )
        )

    @callback
    def async_stop(self) -> None:
        """Unsubscribe from everything this box subscribed to."""
        while self._unsubscribes:
            self._unsubscribes.pop()()
        for pending in self._pending:
            pending.fail("the receiver was removed from Home Assistant")
        self._pending.clear()

    async def async_wait_subscribed(self, timeout: float = SUBSCRIBE_TIMEOUT) -> None:
        """Wait until the broker is actually sending this box's topics to us.

        Degrades rather than hangs: if the confirmation never comes, the caller goes
        ahead anyway and falls back on its own timeout.
        """
        with suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._subscribed.wait()

    # ----------------------------------------------------------------- commands

    async def async_publish_cmd(
        self, name: str, payload: str, qos: int = 1
    ) -> None:
        """Send a command to the box and do not wait for anything.

        Commands are never retained: a retained command is delivered again the instant
        the plugin subscribes, so the box would obey it after every reboot.

        This is what the entities use. An entity press that blocked for ten seconds to
        confirm itself would make the dashboard feel broken, and the state topic moves
        the entity a moment later anyway.
        """
        await self.async_wait_subscribed()
        await mqtt.async_publish(
            self.hass, self.command_topic(name), payload, qos=qos, retain=False
        )

    async def async_command(
        self,
        name: str,
        payload: str,
        *,
        effect: Callable[[], bool] | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        """Send a command and wait for proof that it worked.

        This is what the actions use, because an action is called by an automation that
        wants to know. There is no acknowledgement topic in the contract: a command is
        proved by the state topic it moves, and disproved by `last_error`. So the wait
        is for whichever comes first.

        `effect` returns True once the command has visibly happened. A command whose
        effect is already true — zapping to the channel that is already tuned — is
        still sent, but there is nothing left to wait for and pretending otherwise
        would mean waiting out the whole timeout for a state that will never change.

        A command with no observable effect at all (`send_key`, `message`) passes no
        `effect` and waits only long enough to hear a complaint.
        """
        already_done = effect is not None and effect()
        pending = _Pending(
            cmd=name,
            predicate=effect if effect is not None else lambda: False,
            future=self.hass.loop.create_future(),
        )
        self._pending.append(pending)
        try:
            await self.async_publish_cmd(name, payload)
            if already_done:
                return
            async with asyncio.timeout(ERROR_GRACE if effect is None else timeout):
                await pending.future
        except TimeoutError:
            if effect is None:
                # Silence is the only "it worked" this contract offers here.
                return
            raise Enigma2CommandError(
                translation_domain=DOMAIN,
                translation_key="command_timeout",
                translation_placeholders={"command": name},
            ) from None
        finally:
            if pending in self._pending:
                self._pending.remove(pending)

    async def async_request_ha_mode(
        self, mode: str, timeout: float = ACK_TIMEOUT
    ) -> bool:
        """Switch the box's Home Assistant mode and wait for the acknowledgement."""
        await self.async_wait_subscribed()
        info = await async_request_ha_mode(
            self.hass, self.base_topic, self.node_id, mode, timeout
        )
        if info is None:
            return False
        self.state.info = info
        self._async_updated(TOPIC_INFO)
        return True

    # ------------------------------------------------------------------ device

    @callback
    def async_register_device(self) -> dr.DeviceEntry:
        """Create or update this box's entry in the device registry."""
        source = {**self.state.announcement, **self.state.info}
        name = self.state.announcement.get("name") or self.name
        boxtype = source.get("boxtype")
        ip_address = source.get("ip")
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.node_id)},
            name=name,
            manufacturer=manufacturer_for(boxtype),
            model=boxtype,
            sw_version=software_version(source),
            configuration_url=f"http://{ip_address}/" if ip_address else None,
        )
        self.device_id = device.id
        return device

    # --------------------------------------------------------------- dispatch

    # Topics that carry a JSON object and are read by name elsewhere, mapped to the
    # attribute of `Enigma2State` that holds the last one. `timers` is a JSON list and
    # the rest are handled individually, because each of them does something besides
    # being stored.
    _OBJECT_TOPICS = {
        TOPIC_SERVICE: "service",
        TOPIC_EPG: "epg",
        TOPIC_TUNER: "tuner",
        TOPIC_RECORDING: "recording",
        TOPIC_VOLUME: "volume",
        TOPIC_HDD: "hdd",
        TOPIC_CHANNELS: "channels",
    }

    @callback
    def _message_received(self, msg: ReceiveMessage) -> None:
        """Route one message from the wildcard subscription to its handler."""
        prefix = f"{self.base_topic}/{self.node_id}/"
        if not msg.topic.startswith(prefix):
            return
        suffix = msg.topic[len(prefix) :]

        if suffix.startswith("cmd/"):
            # Our own commands, echoed back by the broker. Not state.
            return
        if suffix.startswith(f"{TOPIC_EPG_GRID}/"):
            self._epg_grid_received(suffix[len(TOPIC_EPG_GRID) + 1 :], msg.payload)
        elif (attribute := self._OBJECT_TOPICS.get(suffix)) is not None:
            setattr(self.state, attribute, parse_json_payload(msg.payload))
            self._async_updated(suffix)
        elif suffix == TOPIC_TIMERS:
            self.state.timers = parse_json_list(msg.payload)
            self._async_updated(suffix)
        elif suffix == TOPIC_AVAILABILITY:
            self._availability_received(msg)
        elif suffix == TOPIC_INFO:
            self._info_received(msg)
        elif suffix == TOPIC_POWER:
            self._power_received(msg)
        elif suffix == TOPIC_SCREEN:
            self._screen_received(msg)
        elif suffix == TOPIC_KEY:
            self._key_received(msg)
        elif suffix == TOPIC_LAST_ERROR:
            self._last_error_received(msg)
        else:
            _LOGGER.debug("Ignoring unknown topic %s", msg.topic)

    @callback
    def _availability_received(self, msg: ReceiveMessage) -> None:
        """Track the box's last will."""
        available = (decode_payload(msg.payload) or "").strip() == PAYLOAD_ONLINE
        if available == self.available:
            return
        self.available = available
        # Once each way, and at a level somebody reading a log will see: "when did the
        # receiver drop off the network" is the first question every other symptom of
        # this integration leads back to.
        if available:
            _LOGGER.info("Receiver %s is back on the broker", self.node_id)
        else:
            _LOGGER.warning(
                "Receiver %s is offline; its entities are unavailable until it returns",
                self.node_id,
            )
        self._async_updated(TOPIC_AVAILABILITY)

    @callback
    def _info_received(self, msg: ReceiveMessage) -> None:
        """Track the box's `info` topic and keep the device page current."""
        if (info := parse_json_payload(msg.payload)) is None:
            return
        self.state.info = info
        self.async_register_device()
        self._async_updated(TOPIC_INFO)

    @callback
    def _announcement_received(self, msg: ReceiveMessage) -> None:
        """Track the box's announcement, which carries the user's chosen name."""
        if (announcement := parse_json_payload(msg.payload)) is None:
            return
        self.state.announcement = announcement
        self.async_register_device()
        self._async_updated(TOPIC_INFO)

    @callback
    def _power_received(self, msg: ReceiveMessage) -> None:
        """Track standby. Deep standby is `availability`, not a power value."""
        self.state.power = decode_payload(msg.payload)
        self._async_updated(TOPIC_POWER)

    @callback
    def _screen_received(self, msg: ReceiveMessage) -> None:
        """Track the last screenshot. This topic is bytes, not JSON."""
        payload = msg.payload
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            return
        self.state.screen = bytes(payload)
        self.state.screen_updated = dt_util.utcnow()
        self._async_updated(TOPIC_SCREEN)

    @callback
    def _key_received(self, msg: ReceiveMessage) -> None:
        """Republish a key press as a bus event, and hand it to the event entity.

        The bus event is fired here rather than from the event entity because the
        device triggers built on it must keep working when the entity is disabled — a
        household that only wants the colour keys in the automation editor should not
        have to keep an entity it never looks at.
        """
        if (payload := parse_json_payload(msg.payload)) is None:
            return
        key = payload.get(ATTR_KEY)
        press = payload.get(ATTR_PRESS, PRESS_SHORT)
        if not isinstance(key, str) or not key:
            return
        if press not in (PRESS_SHORT, PRESS_LONG):
            press = PRESS_SHORT

        self.hass.bus.async_fire(
            EVENT_KEY,
            {
                ATTR_DEVICE_ID: self.device_id,
                ATTR_NODE_ID: self.node_id,
                ATTR_KEY: key,
                ATTR_PRESS: press,
            },
        )
        for listener in list(self._key_listeners):
            listener(key, press)

    @callback
    def _last_error_received(self, msg: ReceiveMessage) -> None:
        """Track the box's complaints, and fail the command that caused one.

        A retained payload is the complaint the broker had before we asked, so it can
        never be the answer to a command we just sent. An empty payload is the plugin
        clearing the topic, which is the closest thing the contract has to "that
        worked" — a command still waiting for an answer takes it as one.
        """
        error = parse_json_payload(msg.payload)
        self.state.last_error = error
        if not msg.retain:
            if error is None:
                for pending in list(self._pending):
                    pending.resolve()
            else:
                cmd = error.get("cmd")
                text = str(error.get("error") or "")
                for pending in list(self._pending):
                    if pending.cmd == cmd:
                        pending.fail(text)
        self._async_updated(TOPIC_LAST_ERROR)

    @callback
    def _epg_grid_received(self, slug: str, payload: Any) -> None:
        """Track one bouquet's EPG grid, or forget it when it is retracted."""
        if not slug or "/" in slug:
            return
        if (grid := parse_json_payload(payload)) is None:
            self.state.epg_grid.pop(slug, None)
        else:
            self.state.epg_grid[slug] = grid
        self._async_updated(TOPIC_EPG_GRID)

    @callback
    def _async_updated(self, suffix: str) -> None:
        """Tell the listeners of a topic that it moved, and re-check the commands."""
        self.seen.add(suffix)
        self.updates[suffix] = self.updates.get(suffix, 0) + 1
        for pending in list(self._pending):
            if pending.predicate():
                pending.resolve()
        for listener in list(self._listeners):
            if listener.wants(suffix):
                listener.callback()


async def async_send_magic_packet(box: Enigma2Box) -> None:
    """Wake a box that is in deep standby.

    `wake_on_lan` is an `after_dependencies`, which means Home Assistant sets it up
    before this integration when the user already has it — and does nothing at all when
    they do not. Setting it up here costs nothing (its `async_setup` only registers one
    action) and is the difference between a working button and an error about a service
    that does not exist.
    """
    # Imported here rather than at module scope: this is the only path that needs it,
    # and it must not become an import every entity platform pays for.
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    if not (mac := box.mac_address):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_mac"
        )
    hass = box.hass
    if not hass.services.has_service("wake_on_lan", "send_magic_packet"):
        await async_setup_component(hass, "wake_on_lan", {})
    if not hass.services.has_service("wake_on_lan", "send_magic_packet"):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="wake_on_lan_unavailable"
        )
    await hass.services.async_call(
        "wake_on_lan", "send_magic_packet", {"mac": mac}, blocking=True
    )

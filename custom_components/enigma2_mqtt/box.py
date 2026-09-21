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
import ipaddress
import json
import logging
import re
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
    CONF_RECEIVER_HOST,
    CONF_SOURCE_LIST_SCOPE,
    CONF_WOL_MAC,
    DEFAULT_BASE_TOPIC,
    DEFAULT_MANUFACTURER,
    DEFAULT_SOURCE_LIST_SCOPE,
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
    SERVICE_FIELDS,
    SOURCE_LIST_SCOPE_ACTIVE_BOUQUET,
    SUBSCRIBE_TIMEOUT,
    TOPIC_AVAILABILITY,
    TOPIC_BOUQUET,
    TOPIC_CAM,
    TOPIC_CHANNELS,
    TOPIC_EPG,
    TOPIC_EPG_GRID,
    TOPIC_HDD,
    TOPIC_INFO,
    TOPIC_KEY,
    TOPIC_LAST_ERROR,
    TOPIC_OSCAM,
    TOPIC_POWER,
    TOPIC_RECORDING,
    TOPIC_SCREEN,
    TOPIC_SERVICE,
    TOPIC_TIMERS,
    TOPIC_TUNER,
    TOPIC_VOLUME,
)

_LOGGER = logging.getLogger(__name__)

# Public conditional-access identifiers emitted by the plugin. Free-form strings are
# deliberately not retained: ECM readers can expose server, account or card details.
CAM_SYSTEMS = frozenset(
    {
        "BetaCrypt",
        "BISS",
        "BulCrypt",
        "Conax",
        "CryptoWorks",
        "DRE-Crypt",
        "Irdeto",
        "Mediaguard",
        "Nagra",
        "Nagravision",
        "PowerVu",
        "SECA",
        "Viaccess",
        "VideoGuard",
    }
)
_NO_PENDING_CAM = object()
_CAM_TOMBSTONE = object()
_NO_PENDING_OSCAM = object()
_OSCAM_TOMBSTONE = object()
OSCAM_PROTOCOLS = frozenset(
    {
        "camd33",
        "camd35",
        "camd35_tcp",
        "cccam",
        "constcw",
        "gbox",
        "ghttp",
        "internal",
        "mouse",
        "mp35",
        "newcamd",
        "pcsc",
        "phoenix",
        "radegast",
        "scam",
        "sc8in1",
        "serial",
        "smartreader",
        "stapi",
        "stapi5",
    }
)
OSCAM_STATUSES = frozenset(
    {
        "ready",
        "no_card",
        "initializing",
        "connected",
        "disconnected",
        "connecting",
        "error",
        "sleeping",
        "duplicate",
        "disabled",
        "unknown",
    }
)
# Real OSCam builds put a patch suffix on the revision — `1.20_svn build r11718-079` is
# what the receiver this was written against reports. A version that does not match is
# dropped on its own, so the pattern being too narrow cost only the version field; it
# still meant the one number a support question starts with was never shown.
OSCAM_VERSION = re.compile(
    r"^[0-9]{1,3}\.[0-9]{1,3}(?:[._-][A-Za-z0-9]+)*"
    r"(?: build r[0-9]{1,8}(?:-[A-Za-z0-9]{1,8})?)?$"
)
# The pattern has a repeating group, so what it accepts has no length limit of its own:
# a megabyte of `1.20_a_a_a…` would match, and it is a label for a bug report. The
# longest version anyone has seen is a quarter of this.
OSCAM_VERSION_MAX = 64

type Enigma2MqttConfigEntry = ConfigEntry[Enigma2Box]


def state_topic(base_topic: str, node_id: str, suffix: str) -> str:
    """Return a state topic of one box."""
    return f"{base_topic}/{node_id}/{suffix}"


def _configuration_url(host: Any) -> str | None:
    """Build a receiver URL only from a plain IP address or hostname."""
    if not isinstance(host, str) or not host or "%" in host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not re.fullmatch(
            r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
            host,
        ):
            return None
        url_host = host
    else:
        url_host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{url_host}/"


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


def service_identity(sref: Any) -> str:
    """Return the fields that identify a service, padded and upper-cased.

    One channel has more than one spelling. A reference can end at the tenth colon or
    without it, an IPTV entry carries its stream URL and its name after the fields that
    identify it, and the case of the hexadecimal fields is not agreed on anywhere. A
    raw `==` between two of those spellings says two different channels, which is how a
    select ends up permanently showing nothing and a zap the receiver carried out is
    reported as a timeout.

    🔴 This mirrors `identity()` in the receiver plugin's `enigma2.py`, field count
    included. The two halves have to mean the same thing by "the same service"; a
    better rule on one side only would be worse than the rule they share.
    """
    parts = [part.strip().upper() for part in str(sref or "").strip().split(":")]
    if not any(parts):
        return ""
    parts = parts[:SERVICE_FIELDS]
    parts += [""] * (SERVICE_FIELDS - len(parts))
    return ":".join(parts)


def same_service(one: Any, other: Any) -> bool:
    """Return whether two service references name the same service.

    Two empty references are not the same service; they are two absences.
    """
    identity = service_identity(one)
    return bool(identity) and identity == service_identity(other)


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

    async def async_wait_until_established(self, timeout: float = SUBSCRIBE_TIMEOUT) -> None:
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


@callback
def _discard(future: asyncio.Future[None]) -> None:
    """Retire a future nobody is going to wait on.

    A command whose effect had already happened is published and returns at once, and a
    command that timed out has stopped listening -- but the box can still answer either of
    them on `last_error` in the meantime, and an exception set on a future nobody reads
    is logged by asyncio as an unretrieved error, from a module that is working
    correctly. Retiring it here makes the difference invisible.
    """
    if not future.done():
        future.cancel()
    elif not future.cancelled():
        future.exception()


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
    cam: dict[str, Any] | None = None
    oscam: dict[str, Any] | None = None
    bouquet: dict[str, Any] | None = None
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
        self._pending_cam: object = _NO_PENDING_CAM
        self._pending_oscam: object = _NO_PENDING_OSCAM
        self._subscribed = asyncio.Event()
        # The bouquet the source list scope last complained about, so that it is said
        # once rather than on every republished channel list.
        self._scope_fallback_warned: str | None = None

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
    def capabilities_declared(self) -> bool:
        """Return whether the box has stated its capability list at all.

        An empty `capabilities` is not the same answer as never having said: the first
        is a box that has hooked nothing, the second is a box that has not spoken yet.
        Anything that would take entities away on "no" has to be able to tell them
        apart, or a reload before the first payload deletes what the household built.
        """
        source = self.state.info or self.state.announcement
        return isinstance(source.get("capabilities"), list)

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
    def active_bouquet(self) -> dict[str, Any] | None:
        """Return the bouquet whose channel list the receiver is currently walking.

        The `bouquet` topic carries the receiver's own channel-up/down context, and
        both of its fields being null is ordinary operation: the radio list, the movie
        list, or a bouquet the plugin was not told to publish. So is a context that
        names a bouquet the options here have filtered out. Either way there is no
        bouquet of ours to point at, which is what None says.
        """
        context = self.state.bouquet or {}
        sref = context.get("sref")
        if isinstance(sref, str) and sref:
            for bouquet in self.bouquets:
                if same_service(bouquet.get("sref"), sref):
                    return bouquet
            return None
        name = context.get("name")
        if isinstance(name, str) and name:
            for bouquet in self.bouquets:
                if bouquet.get("name") == name:
                    return bouquet
        return None

    @property
    def scoped_bouquets(self) -> list[dict[str, Any]]:
        """Return the bouquets the media player's source list draws on.

        This is a preference about how long a dropdown is. The default is every
        bouquet on offer, which is what this integration has always done; scoping it to
        the bouquet the receiver is on makes a thousand-row list into a short one, and
        it is the list the receiver's own channel ± walks.

        Three cases cannot be narrowed at all, and every one of them falls back to the
        full list rather than to nothing: a box that has published no context (an older
        plugin with no `bouquet_context`, or one that has not answered yet), a context
        naming a bouquet the bouquets option has filtered out, and a context naming a
        bouquet that holds no playable channel. An empty source list would leave the
        media player with no way to change channel at all, which is worse than a list
        that is longer than asked for.
        """
        scope = self.entry.options.get(CONF_SOURCE_LIST_SCOPE, DEFAULT_SOURCE_LIST_SCOPE)
        if scope != SOURCE_LIST_SCOPE_ACTIVE_BOUQUET:
            return self.bouquets
        if (active := self.active_bouquet) is not None and active["channels"]:
            self._scope_fallback_warned = None
            return [active]
        self._warn_scope_fallback()
        return self.bouquets

    @callback
    def _warn_scope_fallback(self) -> None:
        """Say once why the source list is not the bouquet the option asked for.

        Once per bouquet, not once per payload: `channels` and `bouquet` are republished
        whenever anything about them moves, and a line per message would bury the log of
        a box that is simply configured this way.
        """
        context = self.state.bouquet or {}
        name = context.get("name")
        if not isinstance(name, str) or not name:
            # No context at all is the documented older-plugin case and is not news.
            self._scope_fallback_warned = None
            return
        if self._scope_fallback_warned == name:
            return
        self._scope_fallback_warned = name
        _LOGGER.warning(
            "Receiver %s is on bouquet %s, which this receiver's options do not offer "
            "or which holds no playable channel, so the media player is listing every "
            "bouquet instead. Add it to the receiver's bouquets option to narrow the "
            "list again",
            self.node_id,
            name,
        )

    @property
    def channel_names(self) -> list[str]:
        """Return the channel names the source list offers, in bouquet order.

        Duplicates are dropped rather than numbered: the same channel in two bouquets
        is one channel, and a source list with "TVP 1 HD" twice helps nobody. The
        „Kanał" select numbers them instead, because it is scoped to one bouquet and a
        repeat inside one bouquet is a channel that would otherwise be unreachable.
        """
        names: list[str] = []
        seen: set[str] = set()
        for bouquet in self.scoped_bouquets:
            for channel in bouquet["channels"]:
                if not isinstance(channel, dict):
                    continue
                name = channel.get("name")
                if isinstance(name, str) and name and name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def channels_named(
        self, name: str, bouquets: Iterable[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Return every channel with this exact name, in the bouquets given.

        The default is every bouquet the options offer, which is the set the plugin
        would have to resolve a zap by name against. A caller that knows which copy is
        meant — the source list, scoped to the active bouquet — passes that instead.
        """
        matches: list[dict[str, Any]] = []
        for bouquet in self.bouquets if bouquets is None else bouquets:
            for channel in bouquet.get("channels") or []:
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
        filtered = None if topics is None else frozenset({*topics, TOPIC_AVAILABILITY})
        entry = _Listener(callback=listener, topics=filtered)
        self._listeners.append(entry)

        @callback
        def _remove() -> None:
            self._listeners.remove(entry)

        return _remove

    @callback
    def async_add_key_listener(self, listener: Callable[[str, str], None]) -> CALLBACK_TYPE:
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
            await mqtt.async_subscribe(self.hass, wildcard, self._message_received, encoding=None)
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

    async def async_publish_cmd(self, name: str, payload: str, qos: int = 1) -> None:
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
            _discard(pending.future)

    async def async_request_ha_mode(self, mode: str, timeout: float = ACK_TIMEOUT) -> bool:
        """Switch the box's Home Assistant mode and wait for the acknowledgement."""
        await self.async_wait_subscribed()
        info = await async_request_ha_mode(self.hass, self.base_topic, self.node_id, mode, timeout)
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
        configuration_url = _configuration_url(source.get("ip"))
        if configuration_url is None:
            configuration_url = _configuration_url(self.entry.data.get(CONF_RECEIVER_HOST))
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.node_id)},
            name=name,
            manufacturer=manufacturer_for(boxtype),
            model=boxtype,
            sw_version=software_version(source),
            configuration_url=configuration_url,
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
        TOPIC_BOUQUET: "bouquet",
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
        if suffix == TOPIC_CAM:
            self._cam_received(msg)
        elif suffix == TOPIC_OSCAM:
            self._oscam_received(msg)
        elif suffix.startswith(f"{TOPIC_EPG_GRID}/"):
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
        if self.cam_enabled and self._pending_cam is not _NO_PENDING_CAM:
            pending, self._pending_cam = self._pending_cam, _NO_PENDING_CAM
            if pending is _CAM_TOMBSTONE:
                self._invalidate_cam()
            elif isinstance(pending, dict):
                self.state.cam = pending
                self._async_updated(TOPIC_CAM)
        elif not self.cam_enabled:
            self._pending_cam = _NO_PENDING_CAM
            if self.state.cam is not None or TOPIC_CAM in self.seen:
                self._invalidate_cam()
        if self.oscam_enabled and self._pending_oscam is not _NO_PENDING_OSCAM:
            pending, self._pending_oscam = self._pending_oscam, _NO_PENDING_OSCAM
            if pending is _OSCAM_TOMBSTONE:
                self._invalidate_oscam()
            elif isinstance(pending, dict):
                self.state.oscam = pending
                self._async_updated(TOPIC_OSCAM)
        elif not self.oscam_enabled:
            self._pending_oscam = _NO_PENDING_OSCAM
            if self.state.oscam is not None or TOPIC_OSCAM in self.seen:
                self._invalidate_oscam()
        self.async_register_device()
        self._async_updated(TOPIC_INFO)

    def telemetry_declared(self, setting: str) -> bool:
        """Return whether the box has stated this opt-in either way.

        An absent setting is not "off": it is a plugin that does not have the option, or
        one that has not said yet. The difference matters to anything that would remove
        entities on "off".
        """
        settings = self.state.info.get("settings")
        return isinstance(settings, dict) and setting in settings

    @property
    def cam_enabled(self) -> bool:
        """Return whether this box opted into supported CAM telemetry."""
        settings = self.state.info.get("settings")
        return (
            "cam" in self.capabilities
            and isinstance(settings, dict)
            and settings.get("cam_telemetry") is True
        )

    @callback
    def _cam_received(self, msg: ReceiveMessage) -> None:
        """Cache only the bounded public CAM schema while explicitly enabled."""
        if not (decode_payload(msg.payload) or "").strip():
            if not self.state.info:
                self._pending_cam = _CAM_TOMBSTONE
            elif self.cam_enabled:
                self._invalidate_cam()
            return
        payload = parse_json_payload(msg.payload)
        if payload is None:
            return
        normalized = self._normalize_cam(payload)
        if not self.state.info:
            self._pending_cam = normalized
            return
        if not self.cam_enabled:
            return
        self.state.cam = normalized
        self._async_updated(TOPIC_CAM)

    @staticmethod
    def _normalize_cam(payload: dict[str, Any]) -> dict[str, Any]:
        """Strip an untrusted CAM payload to the bounded public contract."""
        system = payload.get("system")
        active = payload.get("active")
        encrypted = payload.get("encrypted")
        ecm_ms = payload.get("ecm_ms")
        return {
            "system": (
                system
                if system is None or isinstance(system, str) and system in CAM_SYSTEMS
                else None
            ),
            "active": active if isinstance(active, bool) else None,
            "encrypted": encrypted if isinstance(encrypted, bool) else None,
            "ecm_ms": (
                ecm_ms
                if isinstance(ecm_ms, int)
                and not isinstance(ecm_ms, bool)
                and 0 <= ecm_ms <= 600_000
                else None
            ),
        }

    @callback
    def _invalidate_cam(self) -> None:
        """Forget CAM data after opt-out or a retained-topic retraction."""
        self.state.cam = None
        self.seen.discard(TOPIC_CAM)
        self.updates[TOPIC_CAM] = self.updates.get(TOPIC_CAM, 0) + 1
        for listener in list(self._listeners):
            if listener.wants(TOPIC_CAM):
                listener.callback()

    @property
    def oscam_enabled(self) -> bool:
        """Return whether this box opted into supported OSCam telemetry."""
        settings = self.state.info.get("settings")
        return (
            "oscam" in self.capabilities
            and isinstance(settings, dict)
            and settings.get("oscam_telemetry") is True
        )

    @callback
    def _oscam_received(self, msg: ReceiveMessage) -> None:
        """Cache only normalized OSCam health while explicitly opted in."""
        if not (decode_payload(msg.payload) or "").strip():
            if not self.state.info:
                self._pending_oscam = _OSCAM_TOMBSTONE
            elif self.oscam_enabled:
                self._invalidate_oscam()
            return
        payload = parse_json_payload(msg.payload)
        if payload is None:
            return
        normalized = self._normalize_oscam(payload)
        if normalized is None:
            return
        if not self.state.info:
            self._pending_oscam = normalized
            return
        if not self.oscam_enabled:
            return
        self.state.oscam = normalized
        self._async_updated(TOPIC_OSCAM)

    @staticmethod
    def _normalize_oscam(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Strip OSCam input to the exact bounded public schema."""
        readers = payload.get("readers")
        if not isinstance(readers, list) or len(readers) > 64:
            return None

        def bounded(value: Any, maximum: int = 65535) -> int | None:
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum
                else None
            )

        normalized_readers: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for reader in readers:
            if not isinstance(reader, dict):
                return None
            source_id = reader.get("id")
            kind = reader.get("kind")
            if (
                not isinstance(source_id, str)
                or not re.fullmatch(r"(?:reader|server|source)_[0-9a-f]{12}", source_id)
                or source_id in seen_ids
                or kind not in ("reader", "server", "unknown")
                or not source_id.startswith(
                    {"reader": "reader_", "server": "server_", "unknown": "source_"}[kind]
                )
            ):
                return None
            seen_ids.add(source_id)
            status = reader.get("status")
            protocol = reader.get("protocol")
            enabled = reader.get("enabled")
            normalized_readers.append(
                {
                    "id": source_id,
                    "kind": kind,
                    "enabled": enabled if isinstance(enabled, bool) else None,
                    "status": status if status in OSCAM_STATUSES else "unknown",
                    "protocol": protocol
                    if protocol is None or protocol in OSCAM_PROTOCOLS
                    else None,
                    "shared_cards": bounded(reader.get("shared_cards")),
                }
            )
        access = payload.get("api_access")
        software = payload.get("software")
        version = payload.get("version")
        return {
            "software": software if software in (None, "OSCam") else None,
            "version": version
            if isinstance(version, str)
            and len(version) <= OSCAM_VERSION_MAX
            and OSCAM_VERSION.fullmatch(version)
            else None,
            "software_running": payload.get("software_running")
            if isinstance(payload.get("software_running"), bool)
            else None,
            "api_reachable": payload.get("api_reachable")
            if isinstance(payload.get("api_reachable"), bool)
            else None,
            "api_access": access if access in (None, "granted", "denied") else None,
            "readonly": payload.get("readonly")
            if isinstance(payload.get("readonly"), bool)
            else None,
            "uptime_s": bounded(payload.get("uptime_s"), 315_360_000),
            "readers_configured": bounded(payload.get("readers_configured")),
            "readers_enabled": bounded(payload.get("readers_enabled")),
            "readers_healthy": bounded(payload.get("readers_healthy")),
            "cards_ready": bounded(payload.get("cards_ready")),
            "servers_connected": bounded(payload.get("servers_connected")),
            "shared_cards": bounded(payload.get("shared_cards")),
            "readers": normalized_readers,
        }

    @callback
    def _invalidate_oscam(self) -> None:
        self.state.oscam = None
        self.seen.discard(TOPIC_OSCAM)
        self.updates[TOPIC_OSCAM] = self.updates.get(TOPIC_OSCAM, 0) + 1
        for listener in list(self._listeners):
            if listener.wants(TOPIC_OSCAM):
                listener.callback()

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
        never be the answer to a command we just sent. An empty payload only clears
        state: it carries no command identity, so treating it as an acknowledgement
        could complete an unrelated concurrent command and hide its later error.
        """
        error = parse_json_payload(msg.payload)
        self.state.last_error = error
        if not msg.retain and error is not None:
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
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_mac")
    hass = box.hass
    if not hass.services.has_service("wake_on_lan", "send_magic_packet"):
        await async_setup_component(hass, "wake_on_lan", {})
    if not hass.services.has_service("wake_on_lan", "send_magic_packet"):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="wake_on_lan_unavailable"
        )
    await hass.services.async_call("wake_on_lan", "send_magic_packet", {"mac": mac}, blocking=True)

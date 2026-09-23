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
from typing import Any, Final

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
    CONF_DEEP_STANDBY_ALLOWED,
    CONF_EPG_IMPORT_ALLOWED,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_RECEIVER_HOST,
    CONF_SOFTCAM_RESTART_ALLOWED,
    CONF_SOURCE_LIST_SCOPE,
    CONF_WOL_MAC,
    DEFAULT_BASE_TOPIC,
    DEFAULT_MANUFACTURER,
    DEFAULT_SOURCE_LIST_SCOPE,
    DISCOVERY_PREFIX,
    DOMAIN,
    EPG_IMPORT_STATES,
    ERROR_GRACE,
    ERROR_TEXT_MAX,
    EVENT_KEY,
    KEY_PREFIX,
    MANUFACTURERS,
    MAX_EPG_IMPORT_EPOCH,
    MAX_EPG_IMPORT_EVENTS,
    MAX_SOFTCAM_EPOCH,
    MAX_SOFTCAM_INSTANCES,
    MAX_SOFTCAM_MANAGER_MINUTES,
    MAX_SOFTCAM_RESTARTS_TODAY,
    PAYLOAD_ONLINE,
    POWER_ON,
    PRESS_LONG,
    PRESS_SHORT,
    PROBE_TIMEOUT,
    SERVICE_FIELDS,
    SOFTCAM_NAME_MAX,
    SOFTCAM_RESTART_REASONS,
    SOURCE_LIST_SCOPE_ACTIVE_BOUQUET,
    SUBSCRIBE_TIMEOUT,
    TOPIC_AVAILABILITY,
    TOPIC_BOUQUET,
    TOPIC_CAM,
    TOPIC_CHANNELS,
    TOPIC_EPG,
    TOPIC_EPG_GRID,
    TOPIC_EPG_IMPORT,
    TOPIC_HDD,
    TOPIC_INFO,
    TOPIC_KEY,
    TOPIC_LAST_ERROR,
    TOPIC_OSCAM,
    TOPIC_POWER,
    TOPIC_PROCESS,
    TOPIC_RECORDING,
    TOPIC_SCREEN,
    TOPIC_SERVICE,
    TOPIC_SOFTCAM,
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

# What the `process` topic may say, and the largest value of each that is a measurement
# rather than a bug. The topic is five integers about the enigma2 process itself, and
# every one of them is a number somebody would put on a graph — so the job here is to
# make sure nothing that is not a number ever gets there. A receiver has under a
# gigabyte of RAM and a few dozen threads; these ceilings are orders of magnitude above
# anything real, which is what makes a value above them evidence of a different bug.
PROCESS_BOUNDS: Final[dict[str, int]] = {
    "rss_kb": 64 * 1024 * 1024,
    "hwm_kb": 64 * 1024 * 1024,
    "threads": 65_536,
    "fds": 1_048_576,
    # Unix seconds. Far enough ahead that no receiver clock reaches it, close enough
    # that a millisecond timestamp published by mistake is refused rather than shown as
    # a date in the year 57000.
    "started": 4_102_444_800,
}

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


# The four spellings a MAC address is written in. Anything else is not a near miss to be
# repaired but a value nobody can act on: the failure it used to cause was
# `bytes.fromhex` complaining about a character at position 12, which tells a user
# nothing about what they typed.
_MAC_FORMS = (
    r"[0-9a-f]{12}",
    r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}",
    r"[0-9a-f]{2}(?:-[0-9a-f]{2}){5}",
    r"[0-9a-f]{4}(?:\.[0-9a-f]{4}){2}",
)
_MAC_SEPARATORS = re.compile(r"[:.-]")


def normalise_mac(value: Any) -> str | None:
    """Return a MAC address as lower-case colon-separated hex, or None.

    One spelling is stored, sent and registered, whatever was typed: the device registry
    keys connections by the string, so `AA-BB-…` and `aa:bb:…` would otherwise be two
    different devices' worth of address for one box.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not any(re.fullmatch(form, text) for form in _MAC_FORMS):
        return None
    digits = _MAC_SEPARATORS.sub("", text)
    return ":".join(digits[index : index + 2] for index in range(0, 12, 2))


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
                    translation_key="command_refused",
                    translation_placeholders={"command": self.cmd, "reason": error},
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
    # Whether this callback's last call raised, so a run of failures is logged once.
    failed: bool = False

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
    process: dict[str, Any] | None = None
    softcam: dict[str, Any] | None = None
    epg_import: dict[str, Any] | None = None
    # Whether the last `epg_import` payload arrived with the retain flag set: a replay of
    # what the broker already held, which can describe an earlier run and is therefore
    # never the answer to a press — the same reading `last_error_retained` gives.
    epg_import_retained: bool = False
    bouquet: dict[str, Any] | None = None
    channels: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    # Whether the last complaint arrived with the retain flag set. A retained payload
    # is what the broker had before we subscribed, so it is a replay of something that
    # already happened rather than news — which matters to anything that stamps a time
    # on it, because a reconnect replays it again.
    last_error_retained: bool = False
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
    def reported_mac(self) -> str | None:
        """Return the address the box reports on `info`, normalised."""
        source = {**self.state.announcement, **self.state.info}
        return normalise_mac(source.get("mac"))

    @property
    def mac_address(self) -> str | None:
        """Return the Wake-on-LAN target: the option if set, else what the box says.

        The override exists because the address that has to be woken is not always the
        one enigma2 reports — a box with both a wired and a wireless interface reports
        the one it is using, and the magic packet has to go to the one that is plugged
        in and listening.

        It is normalised here as well as in the options flow, because an entry written
        before the flow validated anything, or edited by hand in `.storage`, reaches
        this property without ever having passed through a form. An override that is not
        an address at all is no address, so the box's own one is used — which is what
        the setup repair leaves behind anyway.
        """
        if override := normalise_mac(self.entry.options.get(CONF_WOL_MAC)):
            return override
        return self.reported_mac

    @property
    def deep_standby_permission(self) -> bool | None:
        """Return the box's own answer about deep standby and reboot, if it gave one.

        `deep_standby_allowed` is set on the receiver's setup screen and deliberately
        cannot be written over MQTT, so this is the only way Home Assistant can tell
        „the box will refuse" from „the box has not been asked". `None` is the second
        one — an older plugin that does not report the key — and it must not be read as
        a refusal, because that would delete the buttons of every installation that
        works today.
        """
        settings = self.state.info.get("settings")
        if not isinstance(settings, dict):
            return None
        value = settings.get(CONF_DEEP_STANDBY_ALLOWED)
        return value if isinstance(value, bool) else None

    @property
    def softcam_restart_permission(self) -> bool | None:
        """Return the box's own answer about restarting its softcam, if it gave one.

        The same three-valued answer as `deep_standby_permission`, and for the same
        reason: `softcam_restart_allowed` is set on the receiver's setup screen, cannot
        be written over MQTT, and `None` is a plugin that has never been asked rather
        than one that said no.

        What differs is what `None` means to the caller. Deep standby had buttons before
        it had a permission, so silence there has to keep them. Nothing has ever shipped
        a „Restart softcam" button, so silence here creates nothing — an older plugin
        would only refuse the command anyway.
        """
        settings = self.state.info.get("settings")
        if not isinstance(settings, dict):
            return None
        value = settings.get(CONF_SOFTCAM_RESTART_ALLOWED)
        return value if isinstance(value, bool) else None

    @property
    def epg_import_permission(self) -> bool | None:
        """Return the box's own answer about importing EPG on demand, if it gave one.

        Read exactly like `softcam_restart_permission`, and silence means the same
        thing: `epg_import_allowed` is set on the receiver's setup screen, cannot be
        written over MQTT, and no „Pobierz EPG" button has ever shipped that silence
        would have to keep.
        """
        settings = self.state.info.get("settings")
        if not isinstance(settings, dict):
            return None
        value = settings.get(CONF_EPG_IMPORT_ALLOWED)
        return value if isinstance(value, bool) else None

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
        """Return one selected bouquet by its service reference.

        🔴 An exact string match, deliberately, and not the identity comparison the
        service references elsewhere get. This is what validates a reference before
        `cmd/bouquet` carries it, and the plugin activates a bouquet on an exact
        allowlist match — so accepting a spelling here that the receiver will refuse
        would only move the refusal somewhere less clear. Reading the *active* context
        is the other direction and does compare by identity: there the receiver's own
        spelling is the one that has to be recognised.
        """
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

        This is the bottom of `async_command`, and what the optimistic controls use —
        the switches and the volume number, which move the moment they are pressed
        because the state topic confirms them a moment later. Anything whose only
        answer is an error goes through `async_command` instead: the buttons used this
        directly, and a refusal had nowhere to arrive.
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
        # The address the packet is actually sent to, so that everything else in Home
        # Assistant — a DHCP discovery, a router integration listing what is on the
        # network — can recognise the same box. Without it the device page knew the
        # receiver's MAC and nothing else did.
        mac = self.mac_address
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.node_id)},
            connections=(
                {(dr.CONNECTION_NETWORK_MAC, mac)} if mac is not None else set()
            ),
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
        elif suffix == TOPIC_PROCESS:
            self._process_received(msg)
        elif suffix == TOPIC_OSCAM:
            self._oscam_received(msg)
        elif suffix == TOPIC_SOFTCAM:
            self._softcam_received(msg)
        elif suffix == TOPIC_EPG_IMPORT:
            self._epg_import_received(msg)
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
        self._notify_listeners(TOPIC_CAM)

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
        self._notify_listeners(TOPIC_OSCAM)

    @callback
    def _process_received(self, msg: ReceiveMessage) -> None:
        """Cache what the plugin measured about the enigma2 process itself.

        Unlike the CAM and OSCam topics this is not an opt-in: there is nothing private
        in a resident set size, and the entities follow the capability rather than a
        setting. What it shares with them is that the payload arrives from a box and is
        therefore not trusted to be what the contract says.
        """
        raw = msg.payload
        if isinstance(raw, (bytes, bytearray, str)) and not raw.strip():
            # An empty retained payload is a retraction: a plugin that stopped
            # publishing, or a topic cleared by hand. The numbers are gone; the
            # entities stay and go unknown, which is the truth.
            #
            # Tested on the raw payload rather than on the decoded text, because this
            # topic arrives as bytes and bytes that are not UTF-8 decode to nothing —
            # which would make a corrupt publish indistinguishable from a retraction
            # and quietly throw away the last good sample.
            self.state.process = None
            self._async_updated(TOPIC_PROCESS)
            return
        payload = parse_json_payload(msg.payload)
        if payload is None:
            # Not JSON, or not an object. Nothing to read here, and the last sample
            # that did parse is more use than throwing it away over one bad publish.
            _LOGGER.debug("Ignoring an unreadable process payload from %s", self.node_id)
            return
        self.state.process = self._normalize_process(payload)
        self._async_updated(TOPIC_PROCESS)

    @staticmethod
    def _normalize_process(payload: dict[str, Any]) -> dict[str, Any]:
        """Strip process telemetry to five bounded integers, or None for each.

        `True` is an `int` in Python and would otherwise become a resident set size of
        one kilobyte on a graph. Everything else that is not a plain integer in range —
        a float, a string, a negative, a missing key, a number from a different unit —
        is None, which is the only honest answer and the one a sensor shows as unknown.
        """

        def bounded(key: str) -> int | None:
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return value if 0 <= value <= PROCESS_BOUNDS[key] else None

        return {key: bounded(key) for key in PROCESS_BOUNDS}

    @callback
    def _softcam_received(self, msg: ReceiveMessage) -> None:
        """Cache the softcam snapshot, normalised to the published contract.

        Three payloads, three different answers, and the difference is the whole of what
        this handler does. An **empty payload is a retraction** — the plugin withdrawing
        a retained topic — and it clears the sample, because the alternative is a reading
        that outlives the thing it describes. A payload that is **not JSON** leaves the
        last good sample exactly where it is: one malformed message is a bug at the other
        end, not news about the softcam. Anything else is normalised and kept.

        Unlike `cam` and `oscam` this needs no opt-in dance. Those two publish what a
        household would not want on a public issue, so they are gated on a setting and
        have to wait for `info` before they may be believed. This carries a binary name
        and three counters, is gated on a capability the plugin only claims where a
        restart is actually possible, and is therefore free to be read the moment it
        arrives.
        """
        if not (decode_payload(msg.payload) or "").strip():
            self._invalidate_softcam()
            return
        if (payload := parse_json_payload(msg.payload)) is None:
            return
        self.state.softcam = self._normalize_softcam(payload)
        self._async_updated(TOPIC_SOFTCAM)

    @staticmethod
    def _normalize_softcam(payload: dict[str, Any]) -> dict[str, Any]:
        """Strip an untrusted softcam payload to the bounded public contract.

        Every key is always present and a value that could not be believed is `None`,
        which is the same shape the topic promises — so a sensor reading this never has
        to know whether a field was missing or nonsense.

        🔴 Two traps are the reason this exists rather than the payload being used as it
        arrives. **`True` is an `int` in Python**, so `running_instances: true` would be
        stored as 1, graph as one healthy instance, and be indistinguishable from a
        measurement; `isinstance(value, bool)` is checked first everywhere below.
        And **`None` is not zero**: a count that could not be taken has to read `unknown`,
        because `0` instances is a real and very interesting reading — it is a channel
        that has stopped decoding.

        Building a fresh dictionary rather than filtering the one that arrived is the
        second half of it. The plugin's own detector reads `/tmp/ecm.info`, which carries
        a card-sharing account, a server address and the live control words; nothing from
        it belongs anywhere near Home Assistant, and a payload that grew an extra key
        would otherwise put whatever it held onto a sensor attribute and into the
        diagnostics download. Only the seven names below can pass, whatever arrives.
        """

        def counted(value: Any, maximum: int) -> int | None:
            """Return a non-negative whole number inside its bounds, or None."""
            return (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= maximum
                else None
            )

        selected = payload.get("selected")
        reason = payload.get("last_restart_reason")
        check_on_start = payload.get("manager_check_on_start")
        # Epoch seconds, so zero is a field that was never filled in rather than a
        # restart in 1970 — the same reading `_refused_at` takes of a complaint's `ts`.
        last_restart = counted(payload.get("last_restart"), MAX_SOFTCAM_EPOCH) or None
        return {
            "selected": (
                selected
                if isinstance(selected, str) and 0 < len(selected) <= SOFTCAM_NAME_MAX
                else None
            ),
            "running_instances": counted(
                payload.get("running_instances"), MAX_SOFTCAM_INSTANCES
            ),
            "last_restart": last_restart,
            "last_restart_reason": (
                reason if reason in SOFTCAM_RESTART_REASONS else None
            ),
            "restarts_today": counted(
                payload.get("restarts_today"), MAX_SOFTCAM_RESTARTS_TODAY
            ),
            "manager_check_on_start": (
                check_on_start if isinstance(check_on_start, bool) else None
            ),
            "manager_timer_minutes": counted(
                payload.get("manager_timer_minutes"), MAX_SOFTCAM_MANAGER_MINUTES
            ),
        }

    @callback
    def _invalidate_softcam(self) -> None:
        """Forget the softcam snapshot after the plugin retracted its topic.

        The topic leaves `seen` as well as the state, so the sensor reports unavailable
        rather than `unknown`. On a page whose job is "is anything wrong", a diagnostic
        stuck at `unknown` reads as a fault; a receiver that has withdrawn the topic has
        simply stopped answering, which is what unavailable says. This mirrors
        `_invalidate_cam` deliberately — one rule in this file for a retracted topic, not
        two.
        """
        self.state.softcam = None
        self.seen.discard(TOPIC_SOFTCAM)
        self.updates[TOPIC_SOFTCAM] = self.updates.get(TOPIC_SOFTCAM, 0) + 1
        self._notify_listeners(TOPIC_SOFTCAM)

    @callback
    def _epg_import_received(self, msg: ReceiveMessage) -> None:
        """Cache the EPG import's state, normalised to the published contract.

        The same three answers as `_softcam_received`, for the same reasons: an empty
        payload is a retraction and clears the reading, a payload that is not JSON
        leaves the last good one alone, and anything else is normalised and kept.
        """
        if not (decode_payload(msg.payload) or "").strip():
            self.state.epg_import = None
            self.seen.discard(TOPIC_EPG_IMPORT)
            self.updates[TOPIC_EPG_IMPORT] = self.updates.get(TOPIC_EPG_IMPORT, 0) + 1
            self._notify_listeners(TOPIC_EPG_IMPORT)
            return
        if (payload := parse_json_payload(msg.payload)) is None:
            return
        self.state.epg_import = self._normalize_epg_import(payload)
        self.state.epg_import_retained = msg.retain
        self._async_updated(TOPIC_EPG_IMPORT)

    @staticmethod
    def _normalize_epg_import(payload: dict[str, Any]) -> dict[str, Any]:
        """Strip an untrusted `epg_import` payload to the five fields of the contract.

        Every key is always present and a value that could not be believed is `None`.
        `True` is an `int` in Python and is refused wherever a number is expected; a
        time of zero is a field nobody filled in, not an import in 1970. `events`
        may be zero — an import that finished with nothing is exactly the failure the
        plugin reports — so zero is kept there. The error is the receiver's sentence,
        cut where a Home Assistant attribute stays readable.
        """

        def whole(value: Any, minimum: int, maximum: int) -> int | None:
            return (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and minimum <= value <= maximum
                else None
            )

        state = payload.get("state")
        error = payload.get("error")
        return {
            "state": state if state in EPG_IMPORT_STATES else None,
            "started": whole(payload.get("started"), 1, MAX_EPG_IMPORT_EPOCH),
            "finished": whole(payload.get("finished"), 1, MAX_EPG_IMPORT_EPOCH),
            "events": whole(payload.get("events"), 0, MAX_EPG_IMPORT_EVENTS),
            "error": (
                error[:ERROR_TEXT_MAX] if isinstance(error, str) and error else None
            ),
        }

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
        self.state.last_error_retained = msg.retain
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
        self._notify_listeners(suffix)

    @callback
    def _notify_listeners(self, suffix: str) -> None:
        """Call every listener of a topic, and keep one that raises from stopping the rest.

        This runs on the callback that feeds every topic, so an exception in one entity's
        read would otherwise skip every listener after it for that message, and reach the
        MQTT client, which logs a traceback on each message for as long as the entity
        keeps raising. The first failure of a listener is logged with its traceback, at
        error level, because it is a bug; the repeats go to debug, because the tenth copy
        of the same traceback on every grid refresh only buries the log around it. A
        successful call ends the run, so the next failure — which may be a different bug
        entirely — is logged at error level again rather than hidden until a reload.
        """
        for listener in list(self._listeners):
            if not listener.wants(suffix):
                continue
            try:
                listener.callback()
            except Exception:
                if listener.failed:
                    _LOGGER.debug(
                        "A listener of receiver %s failed again on %s",
                        self.node_id,
                        suffix,
                        exc_info=True,
                    )
                else:
                    listener.failed = True
                    _LOGGER.exception(
                        "A listener of receiver %s failed on %s; the others still ran, "
                        "and its further failures are logged at debug level until it "
                        "next succeeds",
                        self.node_id,
                        suffix,
                    )
            else:
                listener.failed = False


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

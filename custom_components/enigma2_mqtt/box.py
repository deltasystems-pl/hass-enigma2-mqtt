"""The runtime object that represents one Enigma2 box on the broker.

Everything the integration knows about a box arrives on retained MQTT topics, so this
object is a cache with subscriptions rather than a client: it holds the last payload of
every state topic, and it publishes commands. The entity platforms read it and register
listeners; nothing below this module knows that MQTT exists.

One wildcard subscription covers every state topic of a box. That is a single SUBSCRIBE
packet instead of fourteen, one retained burst instead of fourteen, and - because the
`screen` topic is raw JPEG and the rest is UTF-8 JSON - it has to be taken without an
encoding and decoded per topic, which is what `_message_received` does. The box's own
`cmd/#` echoes arrive on it too and are dropped: a broker delivers what a client
publishes back to that client's matching subscriptions, and a command is not state.

The module level helpers at the top are used by the config flow, which has no runtime
object yet because the config entry does not exist while the flow is running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
import ipaddress
import json
import logging
import re
import secrets
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
import homeassistant.util.dt as dt_util
from yarl import URL

from .buildid import base_version
from .const import (
    ACK_TIMEOUT,
    ATTR_KEY,
    ATTR_NODE_ID,
    ATTR_PRESS,
    CAPABILITY_CHANNEL_TOPICS,
    CAPABILITY_COMMAND_ID,
    CAPABILITY_RECORDING_DELETE,
    CAPABILITY_SELF_UPDATE,
    CAPABILITY_STREAM,
    CAPABILITY_STREAM_STOP,
    CAPABILITY_UNINSTALL,
    CHANNEL_TOPICS_SETTLE,
    CHANNELS_REFETCH_GRACE,
    COMMAND_ID_BYTES,
    COMMAND_MAX_BYTES,
    COMMAND_TIMEOUT,
    COMMANDS_WITHOUT_ENVELOPE,
    CONF_BASE_TOPIC,
    CONF_BOUQUETS,
    CONF_DEEP_STANDBY_ALLOWED,
    CONF_EPG_IMPORT_ALLOWED,
    CONF_HISTORY_HIDDEN_BOUQUETS,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_RECEIVER_HOST,
    CONF_RECORDING_DELETE_ALLOWED,
    CONF_SCREENSHOT,
    CONF_SOFTCAM_RESTART_ALLOWED,
    CONF_SOURCE_LIST_SCOPE,
    CONF_UNINSTALL_ALLOWED,
    CONF_UPDATE_ALLOWED,
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
    INFO_NOT_PUBLISHED,
    INFO_RECORDING_TRASH,
    INFO_WOL,
    KEY_PREFIX,
    MANUFACTURERS,
    MAX_EPG_IMPORT_EPOCH,
    MAX_EPG_IMPORT_EVENTS,
    MAX_SOFTCAM_EPOCH,
    MAX_SOFTCAM_INSTANCES,
    MAX_SOFTCAM_MANAGER_MINUTES,
    MAX_SOFTCAM_RESTARTS_TODAY,
    NOT_PUBLISHED_MAX,
    NOT_PUBLISHED_TOPIC_MAX,
    PAYLOAD_ONLINE,
    POWER_ON,
    PRESS_LONG,
    PRESS_SHORT,
    PROBE_TIMEOUT,
    SCREENSHOT_OFF,
    SERVICE_FIELDS,
    SOFTCAM_NAME_MAX,
    SOFTCAM_RESTART_REASONS,
    SOURCE_LIST_SCOPE_ACTIVE_BOUQUET,
    STREAM_PLAYABLE_TIMEOUT,
    SUBSCRIBE_TIMEOUT,
    TOPIC_AVAILABILITY,
    TOPIC_BOUQUET,
    TOPIC_BOUQUET_CHANNELS,
    TOPIC_BOUQUETS,
    TOPIC_CAM,
    TOPIC_CHANNELS,
    TOPIC_EPG,
    TOPIC_EPG_GRID,
    TOPIC_EPG_IMPORT,
    TOPIC_ERROR_CLEARED,
    TOPIC_HDD,
    TOPIC_INFO,
    TOPIC_KEY,
    TOPIC_LAST_ERROR,
    TOPIC_LIST_ANSWER,
    TOPIC_LOCAL_ERROR,
    TOPIC_MENU_ACTION,
    TOPIC_MENU_EDIT,
    TOPIC_OSCAM,
    TOPIC_POWER,
    TOPIC_PROCESS,
    TOPIC_RECORDING,
    TOPIC_RECORDING_FOLDERS,
    TOPIC_RECORDINGS,
    TOPIC_RELAY_REQUEST,
    TOPIC_SCREEN,
    TOPIC_SERVICE,
    TOPIC_SOFTCAM,
    TOPIC_STREAM,
    TOPIC_STREAM_CLIENTS,
    TOPIC_STREAM_PLAYABLE,
    TOPIC_TIMERS,
    TOPIC_TIMESHIFT,
    TOPIC_TUNER,
    TOPIC_UPDATE,
    TOPIC_VOLUME,
    TOPIC_ZAP_HISTORY,
)
from .recordings import (
    normalise_folder,
    normalise_index,
    normalise_timeshift,
    visible,
)
from .stream_facts import (
    build_stream_url,
    normalise_stream,
    normalise_stream_clients,
    same_client_host,
)

if TYPE_CHECKING:
    from .choice_list import ChoiceLists
    from .own_go2rtc import Go2rtcFollower
    from .picture import PiconResolver

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
# Real OSCam builds put a patch suffix on the revision - `1.20_svn build r11718-079` is
# what the receiver this was written against reports. A version that does not match is
# dropped on its own, so the pattern being too narrow cost only the version field; it
# still meant the one number a support question starts with was never shown.
OSCAM_VERSION = re.compile(
    r"^[0-9]{1,3}\.[0-9]{1,3}(?:[._-][A-Za-z0-9]+)*"
    r"(?: build r[0-9]{1,8}(?:-[A-Za-z0-9]{1,8})?)?$"
)
# The pattern has a repeating group, so what it accepts has no length limit of its own:
# a megabyte of `1.20_a_a_a...` would match, and it is a label for a bug report. The
# longest version anyone has seen is a quarter of this.
OSCAM_VERSION_MAX = 64
# The shape of a source id under any kind, the ones a later plugin may add included: a
# short lowercase prefix and twelve hexadecimal digits. Only for remembering which
# skipped sources are there; a source that gets entities is held to its own prefix.
_OSCAM_ANY_SOURCE_ID = re.compile(r"[a-z]{1,16}_[0-9a-f]{12}")

# What the `process` topic may say, and the largest value of each that is a measurement
# rather than a bug. The topic is five integers about the enigma2 process itself, and
# every one of them is a number somebody would put on a graph - so the job here is to
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


def capabilities_of(state: Enigma2State) -> list[str]:
    """Return the capabilities a box's state names: from `info`, or from the announcement.

    `Enigma2Box.capabilities`, for a reader that is handed the state and not the box.
    """
    source = state.info or state.announcement
    capabilities = source.get("capabilities")
    return list(capabilities) if isinstance(capabilities, list) else []


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


# A host name as HTTP will take it, which is wider than a name a certificate may carry:
# underscores, and the final dot of a fully qualified name. Labels still cannot begin or
# end with a hyphen, and nothing that would end the host part of an address gets in.
_HTTP_HOST_LABEL: Final = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
_HTTP_HOST_NAME: Final = re.compile(rf"(?:{_HTTP_HOST_LABEL}\.)*{_HTTP_HOST_LABEL}\.?")


def _typed_host_url(host: Any) -> str | None:
    """Return the HTTP address of a host somebody typed, or None if it cannot have one.

    The config flow stores what was typed for SSH, which takes more than an address
    does: `fe80::1%eth0` is a fine SSH target and no host of a URL. So this judges only
    whether the value can be put between `http://` and `/` - an IP address without a
    zone, or a name - and never what kind of address it is: the choice is the
    administrator's.
    """
    if not isinstance(host, str) or not (host := host.strip()) or "%" in host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not _HTTP_HOST_NAME.fullmatch(host):
            return None
        try:
            # A label that claims to be punycode and is not is refused by the URL parser,
            # and only when the host is read.
            _ = URL(f"http://{host}/").host
        except ValueError:
            return None
        return f"http://{host}/"
    return f"http://[{address}]/" if address.version == 6 else f"http://{address}/"


def command_topic(base_topic: str, node_id: str, name: str) -> str:
    """Return a command topic of one box."""
    return f"{base_topic}/{node_id}/cmd/{name}"


def new_command_id() -> str:
    """Return a fresh id for one command: random, hexadecimal, and nothing else."""
    return secrets.token_hex(COMMAND_ID_BYTES)


def command_envelope(command_id: str, payload: str) -> str:
    """Return the envelope that carries a command's payload with its id.

    The payload travels as a string, also when it is the text of a JSON object: the
    plugin hands a string to the command character for character, and writes an object
    again its own way - other spacing, other escapes, a number spelled differently. As a
    string the command reads exactly what it reads from a plugin that is sent no
    envelope, so there is one behaviour to know and nothing here has to tell which
    payloads are objects. Characters outside ASCII are not escaped, for the same reason
    the payload is not: fewer bytes, and a string UTF-8 cannot carry fails where it
    always did, in the publish.
    """
    return json.dumps(
        {"id": command_id, "payload": payload}, ensure_ascii=False, separators=(",", ":")
    )


def refusal_id(error: Any) -> str | None:
    """Return the command id a `last_error` payload says back, or None when it has none.

    Only a non-empty string is an id. The plugin leaves the member out rather than
    sending `null`, and anything else there was not written by it.
    """
    if not isinstance(error, dict):
        return None
    said = error.get("id")
    return said if isinstance(said, str) and said else None


def refusal_of_another(error: Any, command_id: str | None) -> bool:
    """Return whether a `last_error` payload is about a command other than the one asked of.

    `command_id` is the id that command was sent with, None when it went out without
    an envelope. A refusal that says an id is about the command that carried it and no
    other, so one with another id - or with any id, for a command sent without one - is
    somebody else's: another broker client's, or another command of this integration. A
    refusal without an id decides nothing here: the plugin drops an id it cannot use and
    the command still runs, an older plugin knows no ids, and what was started at the
    television has none.
    """
    said = refusal_id(error)
    return said is not None and said != command_id


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
    keys connections by the string, so `AA-BB-...` and `aa:bb:...` would otherwise be two
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
    words together - `channel up` is `KEY_CHANNELUP`, never `KEY_CHANNEL_UP`. A name
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


def normalise_not_published(info: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return `info.not_published` as the contract shapes it, or None when it is not there.

    None is a plugin that does not report the member - anything up to 0.4.0 - and is not
    the same answer as `[]`, which is a plugin that reports it and withheld nothing. A
    member that is not a list is not this contract's member, so it is read as absent
    rather than as "nothing withheld".

    An entry is kept when it names a topic; its sizes are whole numbers or None. Anything
    else an entry carries is dropped, a topic named twice counts once, and both the length
    of a topic and the number of entries are bounded, because what is kept here ends up in
    a state attribute, a repair's text and the diagnostics download.
    """
    raw = info.get(INFO_NOT_PUBLISHED)
    if not isinstance(raw, list):
        return None

    def whole(value: Any) -> int | None:
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else None
        )

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if len(entries) >= NOT_PUBLISHED_MAX:
            break
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        if not isinstance(topic, str) or not topic or len(topic) > NOT_PUBLISHED_TOPIC_MAX:
            continue
        if topic in seen:
            continue
        seen.add(topic)
        entries.append(
            {"topic": topic, "bytes": whole(item.get("bytes")), "limit": whole(item.get("limit"))}
        )
    return entries


# Where a channel list came from. `none` is a receiver that has published no list at all,
# or has retracted it.
CHANNEL_SOURCE_NONE: Final = "none"
CHANNEL_SOURCE_COMBINED: Final = "channels"
CHANNEL_SOURCE_BOUQUET_TOPICS: Final = "bouquet_topics"


@dataclass(frozen=True, slots=True)
class ChannelList:
    """The receiver's bouquets and their channels, whichever topics carried them.

    Everything that offers, resolves or filters a channel reads this and nothing else, so
    a receiver whose lists arrive one bouquet a topic and one that publishes them all on
    `channels` look the same from every entity, action and filter.

    `bouquets` holds what a reader may walk: each is a dict with `name`, `sref` and a
    `channels` list. From `channels` they are the payload's own objects. From the
    per-bouquet topics each also carries `slug`, `count` and `listed`, and `listed` is
    False for a bouquet the index names whose list is not here - withheld by the receiver,
    retracted, or not arrived - which then has an empty `channels`. `names` is every
    bouquet name the receiver published, for the options form.
    """

    source: str = CHANNEL_SOURCE_NONE
    bouquets: tuple[dict[str, Any], ...] = ()
    names: tuple[str, ...] = ()
    generated: Any = None
    # False while the index names a bouquet whose list has neither arrived nor been
    # reported as withheld. Always True for the other two sources.
    complete: bool = True


def _retracted(payload: Any) -> bool:
    """Return whether a payload is a retraction: empty, or nothing but white space.

    Asked of the raw payload, not of its text: bytes that are not UTF-8 decode to
    nothing, and a corrupt publish read as a retraction would take away a bouquet's list
    - which the zap-history filter then has to treat as unknown - over one bad message.
    """
    return isinstance(payload, (bytes, bytearray, str)) and not payload.strip()


def _combined_channel_list(channels: dict[str, Any] | None) -> ChannelList:
    """Read the `channels` topic, every bouquet with its list, as a channel list."""
    if channels is None:
        return ChannelList()
    bouquets = channels.get("bouquets")
    if not isinstance(bouquets, list):
        return ChannelList(source=CHANNEL_SOURCE_COMBINED, generated=channels.get("generated"))
    return ChannelList(
        source=CHANNEL_SOURCE_COMBINED,
        bouquets=tuple(
            bouquet
            for bouquet in bouquets
            if isinstance(bouquet, dict) and isinstance(bouquet.get("channels"), list)
        ),
        names=tuple(
            name
            for bouquet in bouquets
            if isinstance(bouquet, dict) and isinstance(name := bouquet.get("name"), str)
        ),
        generated=channels.get("generated"),
    )


def _normalise_bouquet_index(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the `bouquets` index to what the contract names, in the receiver's order.

    A bouquet is kept when it has a name. Its slug is the address of its
    `channels/<slug>` topic and `""` when it has none; a slug that could not be a single
    topic level, or one an earlier bouquet already owns, is read as none, because two
    bouquets cannot be given one list.
    """
    raw = payload.get("bouquets")
    bouquets: list[dict[str, Any]] = []
    slugs: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        sref = item.get("sref")
        slug = item.get("slug")
        if not isinstance(slug, str) or "/" in slug or "+" in slug or "#" in slug or slug in slugs:
            slug = ""
        if slug:
            slugs.add(slug)
        count = item.get("count")
        bouquets.append(
            {
                "name": name,
                "sref": sref if isinstance(sref, str) else "",
                "slug": slug,
                "count": (
                    count
                    if isinstance(count, int) and not isinstance(count, bool) and count >= 0
                    else None
                ),
            }
        )
    return {"generated": payload.get("generated"), "bouquets": bouquets}


def picon_url(ip_address: str | None, sref: str | None) -> str | None:
    """Return the URL OpenWebif serves a channel's picon at, if it can be built.

    Picons are not published over MQTT - a few hundred kilobytes of PNG per channel on
    a retained topic would be an abuse of a broker - so the browser fetches them from
    the box, which already serves them at a name derived from the service reference:
    every colon becomes an underscore and the trailing one is dropped.

    Everything about this is best effort. No address, no picon; no picon on the box,
    a broken image the media browser simply does not draw.

    This name is only the first of several OpenWebif tries, so it is the fallback: the
    media browser and the player use the link OpenWebif itself gives for a service
    (`picture.PiconResolver`) whenever it gives one - and the resolver builds the
    fallback too, at the address it asks, not at whatever address is passed in here.
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
    *,
    wire: Callable[[str], str] | None = None,
) -> dict[str, Any] | None:
    """Switch a box's `ha_mode` and return the `info` payload that acknowledges it.

    `wire` turns the mode into what is published, at the moment it is: the runtime
    object passes its envelope (`Enigma2Box.async_wire_payload`). The config flow has
    no runtime object and passes nothing, so its command goes out bare, which every
    plugin takes.

    There is no acknowledgement topic in the contract: the plugin answers by
    republishing `info` with the new mode, and that answer comes within a fraction of
    a second.

    Subscribing before publishing is therefore not enough. Home Assistant batches its
    SUBSCRIBE packets behind a short debouncer, so a subscription that exists in
    process still reaches the broker a tenth of a second later - about when the
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
            mode if wire is None else wire(mode),
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


class Enigma2CommandRefused(Enigma2CommandError, ServiceValidationError):
    """A command the box refused with a code that says the moment was wrong, not the box.

    The receiver did nothing and said why in a code this integration knows: standby, a
    zap history too short to clear, a recording being played back. Home Assistant shows
    a `ServiceValidationError` as the sentence it carries, where a plain
    `HomeAssistantError` reads as an action that failed (`errors.py`). Still an
    `Enigma2CommandError`, so everything that catches a refusal catches this one.
    """


@dataclass(slots=True)
class _Pending:
    """One command whose effect is being waited for."""

    cmd: str
    predicate: Callable[[], bool]
    future: asyncio.Future[None]
    # The refusal codes this command has a translation of its own for, each under
    # `<cmd>_<reason>`. Empty for nearly every command: the receiver's sentence is then
    # the whole answer, as it always was.
    reasons: frozenset[str] = frozenset()
    # Those of `reasons` that say the receiver tried and did not manage, rather than
    # that it refused: raised as a failure, not as a refusal.
    failures: frozenset[str] = frozenset()
    # Whether the command has gone to the broker. A complaint that arrives before it has
    # cannot be about it.
    sent: bool = False
    # The id it went out with, None for one sent without an envelope.
    command_id: str | None = None

    @callback
    def sent_as(self, command_id: str | None) -> None:
        """Note that the command is being published, and under which id."""
        self.sent = True
        self.command_id = command_id

    @callback
    def refused_by(self, error: dict[str, Any]) -> bool:
        """Return whether a fresh `last_error` payload is a complaint about this command.

        One that says this command's id is, whatever name it gives. One that says another
        id is not, and neither is one that says any id when this command carried none
        (`refusal_of_another`). One without an id is judged as before there were ids, by
        the command's name - once the command has been sent.
        """
        if not self.sent:
            return False
        said = refusal_id(error)
        if said is not None:
            return said == self.command_id
        return error.get("cmd") == self.cmd

    @callback
    def resolve(self) -> None:
        """Report that the effect happened."""
        if not self.future.done():
            self.future.set_result(None)

    @callback
    def fail(self, error: str, reason: Any = None) -> None:
        """Report that the box complained about this command.

        A `reason` this command knows is raised as its own translation, so the household
        reads why in its own language; anything else - no code, a code from a newer
        plugin, a code that is not a string - is the receiver's English sentence, which
        is never wrong, only untranslated.

        A known code is also what says whose fault it was: a refusal is raised as one
        (`Enigma2CommandRefused`), a code listed in `failures` as the failure it is. The
        sentence without a code can be either, and stays the general error.
        """
        if self.future.done():
            return
        refusal: Enigma2CommandError
        if isinstance(reason, str) and reason in self.reasons:
            kind = Enigma2CommandError if reason in self.failures else Enigma2CommandRefused
            refusal = kind(
                translation_domain=DOMAIN,
                translation_key=f"{self.cmd}_{reason}",
            )
        else:
            refusal = Enigma2CommandError(
                translation_domain=DOMAIN,
                translation_key="command_refused",
                translation_placeholders={"command": self.cmd, "reason": error},
            )
        self.future.set_exception(refusal)


@dataclass(slots=True)
class _PlayableQuestion:
    """One `cmd/stream_playable` whose answer is being waited for."""

    sref: str
    future: asyncio.Future[bool | None]
    # Whether the question has gone to the broker, and the id it went out with - None
    # for one sent without an envelope. An answer that arrives before it has cannot be
    # its answer.
    sent: bool = False
    command_id: str | None = None

    @callback
    def sent_as(self, command_id: str | None) -> None:
        """Note that the question is being published, and under which id."""
        self.sent = True
        self.command_id = command_id

    @callback
    def answer(self, playable: bool | None) -> None:
        """Give the answer: yes, no, or None for "the receiver could not say"."""
        if not self.future.done():
            self.future.set_result(playable)


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
    # never the answer to a press - the same reading `last_error_retained` gives.
    epg_import_retained: bool = False
    bouquet: dict[str, Any] | None = None
    # The combined `channels` topic. None when it has not arrived, was retracted - or was
    # let go because the per-bouquet topics carry the same lists; `channels_released` says
    # which, and readers go through `Enigma2Box.channel_list` rather than through this.
    channels: dict[str, Any] | None = None
    channels_released: bool = False
    # The `bouquets` index, normalised (`_normalise_bouquet_index`), and each bouquet's
    # `channels/<slug>` payload by slug. Kept as they arrive, whatever the capability list
    # says so far: `info` can land after them.
    bouquet_index: dict[str, Any] | None = None
    bouquet_channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    # `info.not_published`, normalised: None for a plugin that does not report it.
    not_published: list[dict[str, Any]] | None = None
    # The receiver's zap history, normalised; see `_normalize_zap_history`.
    zap_history: dict[str, Any] | None = None
    # Whether the last `zap_history` arrived retained: a replay of what the broker held,
    # which can never be the answer to a press of the clear button.
    zap_history_retained: bool = False
    # The plugin's `update` topic: what its signed index offers, and its last or running
    # update transaction (`mqtt_update.parse_update` reads it). None until it has published one,
    # and again after a retraction.
    update: dict[str, Any] | None = None
    # Whether the last `update` arrived retained: the broker's replay, which can describe an
    # earlier transaction and is never the answer to a request just sent.
    update_retained: bool = False
    # Whether the last `info` that was read arrived retained: the broker's replay of what a
    # plugin said earlier, and not a plugin speaking now.
    info_retained: bool = False
    # Where the image's stream server is, normalised (`stream_facts.normalise_stream`).
    # None until the topic arrives, and again after a retraction.
    stream: dict[str, Any] | None = None
    # Who the image's stream server is serving, normalised
    # (`stream_facts.normalise_stream_clients`). None until the topic arrives, and again
    # while the plugin has emptied it because the image's list could not be read.
    stream_clients: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    # Whether the last complaint arrived with the retain flag set. A retained payload
    # is what the broker had before we subscribed, so it is a replay of something that
    # already happened rather than news - which matters to anything that stamps a time
    # on it, because a reconnect replays it again.
    last_error_retained: bool = False
    # Whether the last payload on `last_error` was the empty one - the receiver clearing
    # the topic. `last_error` is None for that and for a payload that is not a JSON
    # object alike, and only the first is a clearing.
    last_error_empty: bool = False
    # A failure Home Assistant records itself on „Ostatni błąd": `cmd`, `error`, `ts`.
    local_error: dict[str, Any] | None = None
    screen: bytes | None = None
    screen_updated: datetime | None = None
    epg_grid: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The timeshift state, normalised (`recordings.normalise_timeshift`). None until the
    # topic arrives, and again after a retraction.
    timeshift: dict[str, Any] | None = None
    # The recording library: the `recordings` index, normalised (`recordings.normalise_index`),
    # and each folder's `recordings/<slug>` by slug (`recordings.normalise_folder`). A folder
    # topic is kept as it arrives, whatever the index says so far - the two arrive in no
    # order - and read only for a folder the index names.
    recordings_index: dict[str, Any] | None = None
    # Whether the last index arrived retained: the broker's replay of an earlier read,
    # which is never the answer to a refresh just asked for.
    recordings_index_retained: bool = False
    recording_folders: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


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
        # topic was published again" - adding a timer, taking a screenshot - has no
        # value to compare, only the fact that something arrived.
        self.updates: dict[str, int] = {}
        self._unsubscribes: list[CALLBACK_TYPE] = []
        self._listeners: list[_Listener] = []
        self._key_listeners: list[Callable[[str, str], None]] = []
        self._pending: list[_Pending] = []
        # The `cmd/stream_playable` questions whose answers are being waited for.
        self._questions: list[_PlayableQuestion] = []
        self._pending_cam: object = _NO_PENDING_CAM
        self._pending_oscam: object = _NO_PENDING_OSCAM
        self._subscribed = asyncio.Event()
        # Whether the last `info` the broker delivered was empty - the plugin retracting
        # it, which an uninstall does and nothing else a running plugin does for long.
        # The last good payload is kept for everything that reads it, as it always was;
        # this flag only withdraws what that payload *permitted*. See `uninstall_offered`.
        self.info_retracted: bool = False
        # Whether an `info` that could not be read - not empty, and not a JSON object -
        # has arrived since the last one that could. The last good payload is kept here
        # too; this flag only keeps `takes_command_ids` from reading a claim out of it.
        self.info_unreadable: bool = False
        # Whether an `info` that is a JSON object has arrived in this run. `state.info`
        # cannot say: an `info` of `{}` is as false as none at all, and the topic's place
        # in `seen` cannot either, because the announcement wakes the same listeners.
        self.info_received: bool = False
        # The last install of the plugin this entry ran to a proven end, as
        # `async_record_install` keeps it. None until there has been one in this run.
        self.last_install: dict[str, str] | None = None
        # The bouquet the source list scope last complained about, so that it is said
        # once rather than on every republished channel list.
        self._scope_fallback_warned: str | None = None
        # Hidden bouquet names already reported as missing from `channels`, for the same
        # reason: the filter runs on every payload of three topics.
        self._hidden_missing_warned: set[str] = set()
        # Who answers the receiver's `relay_request`: set when the entry is set up.
        self.relay_request_handler: Callable[[Any], None] | None = None
        # Who checks and performs a viewer's `menu_action`: the receiver menu, set when the
        # entry is set up.
        self.menu_action_handler: Callable[[ReceiveMessage], None] | None = None
        # Who checks and stores an edited menu from the receiver's web interface: the menu
        # creator, set when the entry is set up.
        self.menu_edit_handler: Callable[[ReceiveMessage], None] | None = None
        # The lists of choices Home Assistant shows on this receiver, and the answers they
        # are waited for with: set when the entry is set up, stopped with the box.
        self.lists: ChoiceLists | None = None
        # Finds and fetches channel picons through the receiver's web interface: set
        # when the entry is set up, stopped with the box.
        self.picons: PiconResolver | None = None
        # Keeps the user's own go2rtc on the channel that is on, where the options
        # name one: set when the entry is set up.
        self.go2rtc: Go2rtcFollower | None = None
        # The channel list last shown from the per-bouquet topics; None while the combined
        # `channels` topic is the source. See `channel_list`.
        self._topic_list: ChannelList | None = None
        # The combined topic read as a channel list, and the payload it was read from.
        self._combined_list: ChannelList = ChannelList()
        self._combined_from: dict[str, Any] | None = None
        # Counts every change of the channel list, so what is derived from it - the
        # members of the hidden bouquets - is rebuilt when it moved and not per message.
        self._channel_generation: int = 0
        self._hidden_members: tuple[tuple[int, frozenset[str]], set[str] | None] | None = None
        # What `info` last said that the per-bouquet list depends on: whether the
        # capability is claimed, and which bouquets' lists the receiver withheld.
        self._channel_topics_inputs: tuple[bool, frozenset[str], bool] | None = None
        self._withheld_lists: frozenset[str] = frozenset()
        self._withheld_combined: bool = False
        self._settle_cancel: CALLBACK_TYPE | None = None
        # Ends the request that asks the broker for the retained `channels` again:
        # its subscription, its confirmation tracker and its timer.
        self._refetch: CALLBACK_TYPE | None = None
        # When the broker's answer to that request arrived, to know the same delivery
        # again should the wildcard subscription be handed it too.
        self._refetch_answered: float | None = None

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
        return capabilities_of(self.state)

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
    def takes_command_ids(self) -> bool:
        """Return whether the plugin opens the envelope a command's id travels in.

        Read from `info` alone, as it stands when a command is sent: the announcement is
        not asked, an `info` that has not arrived claims nothing, and neither does one
        that was retracted or one that is not a JSON object - the last good payload is
        kept for the device page, but it no longer says what the plugin on the receiver
        takes. A plugin that does not open the envelope refuses most commands sent in
        one, so "not known" is "no".
        """
        # Everywhere else an `info` that is not a JSON object changes nothing: every
        # reader keeps the last good one. Here it is the receiver's last word, and it
        # does not claim the capability.
        if self.info_retracted or self.info_unreadable:
            return False
        capabilities = self.state.info.get("capabilities")
        return isinstance(capabilities, list) and CAPABILITY_COMMAND_ID in capabilities

    @property
    def ip_address(self) -> str | None:
        """Return the box's LAN address, as it last reported it."""
        source = {**self.state.announcement, **self.state.info}
        ip_address = source.get("ip")
        return ip_address if isinstance(ip_address, str) and ip_address else None

    @property
    def web_url(self) -> str | None:
        """Return the address of the receiver's web interface, or None if unknown.

        The address the receiver reports, and the host typed when it was added only while
        it has reported none. Built from a plain IP address or host name and nothing else.
        """
        source = {**self.state.announcement, **self.state.info}
        return _configuration_url(source.get("ip")) or _configuration_url(
            self.entry.data.get(CONF_RECEIVER_HOST)
        )

    @property
    def picon_lookup_url(self) -> str | None:
        """Return the address Home Assistant itself asks for picons, or None for none.

        Not the same rule as `web_url`, which only makes a link for a person to click.
        This one decides where Home Assistant sends requests of its own, and the address
        a receiver reports arrives over the broker, where anything that may publish can
        have written it. So the host typed when the receiver was added wins whenever
        there is one: that is the administrator's word, and it is not second-guessed.
        A typed host that cannot be the host of an address - one only SSH can use, like
        an IPv6 address with a zone, or a host with a port - means no address and no
        picons, never the reported one: once a host is typed, nothing on the broker
        chooses where Home Assistant asks. Only without a typed host is the reported
        address used, and only when it is an IP address written out - never a name,
        which would be resolved by this machine - and not loopback, link-local,
        unspecified, multicast or reserved: none of those is a receiver, and the first
        two are where a machine keeps what only it should reach.
        """
        typed = self.entry.data.get(CONF_RECEIVER_HOST)
        if isinstance(typed, str) and typed.strip():
            return _typed_host_url(typed)
        try:
            address = ipaddress.ip_address(self.ip_address or "")
        except ValueError:
            return None
        for candidate in (address, getattr(address, "ipv4_mapped", None)):
            if candidate is not None and (
                candidate.is_loopback
                or candidate.is_link_local
                or candidate.is_unspecified
                or candidate.is_multicast
                or candidate.is_reserved
            ):
                return None
        return _configuration_url(str(address))

    @property
    def screenshots_off(self) -> bool:
        """Return True when the receiver says it takes no screenshots."""
        settings = self.state.info.get("settings")
        return isinstance(settings, dict) and settings.get(CONF_SCREENSHOT) == SCREENSHOT_OFF

    @property
    def reported_mac(self) -> str | None:
        """Return the address the box reports on `info`, normalised."""
        source = {**self.state.announcement, **self.state.info}
        return normalise_mac(source.get("mac"))

    @property
    def mac_address(self) -> str | None:
        """Return the Wake-on-LAN target: the option if set, else what the box says.

        The override exists because the address that has to be woken is not always the
        one enigma2 reports - a box with both a wired and a wireless interface reports
        the one it is using, and the magic packet has to go to the one that is plugged
        in and listening.

        It is normalised here as well as in the options flow, because an entry written
        before the flow validated anything, or edited by hand in `.storage`, reaches
        this property without ever having passed through a form. An override that is not
        an address at all is no address, so the box's own one is used - which is what
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
        one - an older plugin that does not report the key - and it must not be read as
        a refusal, because that would delete the buttons of every installation that
        works today.
        """
        settings = self.state.info.get("settings")
        if not isinstance(settings, dict):
            return None
        value = settings.get(CONF_DEEP_STANDBY_ALLOWED)
        return value if isinstance(value, bool) else None

    @property
    def wake_on_lan_supported(self) -> bool | None:
        """Return whether the receiver says a magic packet can wake it, if it said.

        `info.wol.supported` is the image's own answer: whether it found the
        front-processor switch through which it arms Wake-on-LAN for deep standby. It is
        not the network card's `Supports Wake-on`, which describes a suspend path these
        images never take, so a receiver can report `false` here while `ethtool` says the
        opposite - and `false` is the answer that matches what happens. Three values,
        like the permissions: `None` is a plugin that does not report `wol` at all, or a
        value that is not a boolean, and must not be read as `false`, because that would
        tell a household its receiver cannot be woken when nobody has said so.
        """
        wol = self.state.info.get(INFO_WOL)
        if not isinstance(wol, dict):
            return None
        value = wol.get("supported")
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
        a „Restart softcam" button, so silence here creates nothing - an older plugin
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
    def uninstall_offered(self) -> bool:
        """Return whether removing the plugin may be offered right now.

        Three conditions, all read from what the receiver is saying *now*: it is on the
        broker, its current `info` states `uninstall_allowed: true`, and the same `info`
        names the `uninstall` capability. The announcement is not consulted, although
        `capabilities` falls back on it: a capability is the plugin's own finding about the
        box it runs on, and only `info` carries the permission beside it.

        🔴 The polarity is the opposite of `deep_standby_permission`'s, deliberately
        (ADR-0004): silence - an older plugin, a box that has not answered - offers
        nothing, because the act behind it cannot be undone from here. A payload that
        cannot be read is not that silence once a good one is held: it is dropped, and
        the last good `info` goes on deciding.
        An empty `info` counts as silence even though the last good payload is still held:
        it is what the plugin publishes on its way out, and a menu entry left standing for
        a receiver whose plugin has gone would be a control that can only ever time out.
        """
        if not self.available or self.info_retracted:
            return False
        info = self.state.info
        settings = info.get("settings")
        capabilities = info.get("capabilities")
        return (
            isinstance(settings, dict)
            and settings.get(CONF_UNINSTALL_ALLOWED) is True
            and isinstance(capabilities, list)
            and CAPABILITY_UNINSTALL in capabilities
        )

    @property
    def self_update_offered(self) -> bool:
        """Return whether this receiver takes plugin updates over MQTT (ADR-0008).

        Both halves of the receiver's own word on its current `info`: the capability
        `self_update` - a plugin the package manager installed, which can put itself back - and
        its box-only permission `update_allowed`, stated `true`. A permission that is not a
        boolean is not a yes, and an `info` the plugin retracted states nothing. Availability is
        deliberately not part of it: a receiver restarting in the middle of its own update is
        offline for a moment, and an install pressed then must not go over SSH instead.
        """
        if self.info_retracted:
            return False
        info = self.state.info
        settings = info.get("settings")
        capabilities = info.get("capabilities")
        return (
            isinstance(settings, dict)
            and settings.get(CONF_UPDATE_ALLOWED) is True
            and isinstance(capabilities, list)
            and CAPABILITY_SELF_UPDATE in capabilities
        )

    def plugin_at_least(self, version: tuple[int, int, int]) -> bool | None:
        """Return whether the plugin on the receiver is `version` or a later release.

        For a command the plugin announces no capability for: the release that added it is
        then all there is to go by. Read from `info.plugin`, the number in front of any
        build label, so a development build counts as the release it says it is. `None`
        while the receiver has not said - no `info` yet, or a version that is not
        `N.N.N` - which is not a "no": nothing is taken away on silence. A retracted
        `info` leaves the last version standing, as it leaves the device details.
        """
        running = base_version(self.state.info.get("plugin"))
        if running is None:
            return None
        return running >= version

    @property
    def is_on(self) -> bool:
        """Return whether the box is out of standby and reachable."""
        return self.available and self.state.power == POWER_ON

    @property
    def bouquets(self) -> list[dict[str, Any]]:
        """Return the bouquets of the channel list, filtered by the options.

        An empty or absent option means every bouquet the plugin published, which is
        already the plugin's own `bouquets_for_select` selection: the option narrows
        what the box offers, it can never widen it.
        """
        usable = self.published_bouquets
        if not (chosen := self.entry.options.get(CONF_BOUQUETS)):
            return usable
        wanted = set(chosen)
        return [bouquet for bouquet in usable if bouquet.get("name") in wanted]

    @property
    def channel_list(self) -> ChannelList:
        """Return the receiver's bouquets and channels, from whichever topics carry them.

        The one model behind every reader. It has three sources and they are told apart
        here and nowhere else:

        - **`channels`**, every bouquet with its list in one payload - the only source of a
          plugin up to 0.4.0, and of a later one until its index has arrived.
        - **the per-bouquet topics** - the `bouquets` index and one `channels/<slug>` for
          each bouquet - while the receiver claims `channel_topics` and the index is here.
          They win over `channels` then, which a receiver with a very large channel list
          does not publish at all.
        - **nothing**, before either has arrived or after a retraction.

        The per-bouquet list is a snapshot, replaced as a whole (`_async_show_topic_list`):
        retained topics arrive one by one, and a list that grew by a bouquet per message
        would rewrite every select once per bouquet. So while the index names a bouquet
        whose topic has not arrived, readers keep what they had - the `channels` list,
        the previous snapshot, or nothing - until the last one lands or the burst is over.
        """
        if self._topic_list is not None:
            return self._topic_list
        channels = self.state.channels
        if channels is not self._combined_from:
            self._combined_from = channels
            self._combined_list = _combined_channel_list(channels)
            self._channel_generation += 1
        return self._combined_list

    @property
    def published_bouquets(self) -> list[dict[str, Any]]:
        """Return every bouquet the receiver published a place for, options ignored.

        From `channels` that is every bouquet that lists channels. From the per-bouquet
        topics it is every bouquet of the index: one whose own topic the receiver withheld
        is still a bouquet of this receiver, with its name and no channels.
        """
        return list(self.channel_list.bouquets)

    @property
    def bouquet_names(self) -> list[str]:
        """Return every bouquet name the box published, options ignored.

        The options flow offers this: a user cannot choose from a list that has already
        been filtered by the choice they are about to change.
        """
        return list(self.channel_list.names)

    @property
    def channel_topics_claimed(self) -> bool:
        """Return whether the receiver says it publishes the per-bouquet channel topics."""
        return CAPABILITY_CHANNEL_TOPICS in self.capabilities

    def bouquet_name_for_slug(self, slug: str) -> str | None:
        """Return the name the index gives the bouquet whose channel topic has this slug."""
        for entry in (self.state.bouquet_index or {}).get("bouquets") or []:
            if entry["slug"] == slug:
                return entry["name"]
        return None

    def bouquet_names_without_topic(self) -> list[str]:
        """Return the bouquets of the index that have no channel topic of their own.

        Their channels are published on `channels` only, so they are what a withheld
        `channels` takes away that the receiver's own list of withheld topics cannot name.
        """
        return [
            entry["name"]
            for entry in (self.state.bouquet_index or {}).get("bouquets") or []
            if not entry["slug"]
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
        meant - the source list, scoped to the active bouquet - passes that instead.
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
        allowlist match - so accepting a spelling here that the receiver will refuse
        would only move the refusal somewhere less clear. Reading the *active* context
        is the other direction and does compare by identity: there the receiver's own
        spelling is the one that has to be recognised.
        """
        for bouquet in self.bouquets:
            if bouquet.get("sref") == sref:
                return bouquet
        return None

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
        self._cancel_settle()
        self._cancel_refetch()
        if self.picons is not None:
            self.picons.async_stop()
        for pending in self._pending:
            pending.fail("the receiver was removed from Home Assistant")
        self._pending.clear()
        for question in self._questions:
            question.answer(None)
        if self.lists is not None:
            self.lists.async_stop()

    @property
    def subscribed(self) -> bool:
        """Return whether the broker has confirmed the subscription to this box's topics."""
        return self._subscribed.is_set()

    async def async_wait_subscribed(self, timeout: float = SUBSCRIBE_TIMEOUT) -> None:
        """Wait until the broker is actually sending this box's topics to us.

        Degrades rather than hangs: if the confirmation never comes, the caller goes
        ahead anyway and falls back on its own timeout.
        """
        with suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._subscribed.wait()

    # ----------------------------------------------------------------- commands

    @callback
    def async_wire_payload(self, name: str, payload: str) -> tuple[str, str | None]:
        """Return what to publish for a command, and the id it carries.

        For a plugin that claims `command_id` that is the payload in an envelope with a
        fresh id; for every other plugin it is the payload as it stands, and None. Asked
        at the moment of the publish, so a plugin that gained or lost the capability
        since the last command - an update, a downgrade - is sent what it takes now.

        Three things go out bare to a plugin that takes ids as well: `cmd/relay`, which
        is not a command (`COMMANDS_WITHOUT_ENVELOPE`); a payload whose envelope would
        be over the plugin's size limit, because an oversized command is discarded
        unanswered and the id is not worth the command - one that is over the limit by
        itself goes out as it always did; and a payload UTF-8 cannot carry, which fails
        in the publish as it did before.
        """
        if name in COMMANDS_WITHOUT_ENVELOPE or not self.takes_command_ids:
            return payload, None
        command_id = new_command_id()
        wrapped = command_envelope(command_id, payload)
        try:
            too_long = len(wrapped.encode("utf-8")) > COMMAND_MAX_BYTES
        except UnicodeEncodeError:
            return payload, None
        if too_long:
            _LOGGER.debug(
                "cmd/%s for %s would be over the receiver's limit in an envelope; sent "
                "without an id",
                name,
                self.node_id,
            )
            return payload, None
        return wrapped, command_id

    async def async_publish_cmd(
        self,
        name: str,
        payload: str,
        qos: int = 1,
        *,
        sent_as: Callable[[str | None], None] | None = None,
    ) -> None:
        """Send a command to the box and do not wait for anything.

        Commands are never retained: a retained command is delivered again the instant
        the plugin subscribes, so the box would obey it after every reboot.

        This is the bottom of `async_command`, and what the optimistic controls use -
        the switches and the volume number, which move the moment they are pressed
        because the state topic confirms them a moment later. Anything whose only
        answer is an error goes through `async_command` instead: the buttons used this
        directly, and a refusal had nowhere to arrive.

        The payload goes out in an envelope with an id when the plugin takes one
        (`async_wire_payload`). `sent_as` is called with that id - None for a command
        sent bare - immediately before the publish, for a caller that waits for a
        refusal: the answer can arrive before the publish has returned.
        """
        await self.async_wait_subscribed()
        wire, command_id = self.async_wire_payload(name, payload)
        if sent_as is not None:
            sent_as(command_id)
        await mqtt.async_publish(
            self.hass, self.command_topic(name), wire, qos=qos, retain=False
        )

    async def async_publish_enveloped(self, name: str, wire: str) -> None:
        """Publish a command that is already in its envelope, under an id its caller made.

        For `cmd/list` (`choice_list.py`), whose answer is matched by the list's id and by
        nothing else: it must never go out bare, the way `async_wire_payload` sends a command
        that fits only without an envelope. So the caller builds the envelope
        (`command_envelope`), refuses one over the limit, and waits for this box's
        subscription before it notes the id it is about to publish. QoS 1, never retained,
        as every command.
        """
        await mqtt.async_publish(
            self.hass, self.command_topic(name), wire, qos=1, retain=False
        )

    async def async_command(
        self,
        name: str,
        payload: str,
        *,
        effect: Callable[[], bool] | None = None,
        timeout: float = COMMAND_TIMEOUT,
        reasons: Collection[str] = (),
        failures: Collection[str] = (),
    ) -> None:
        """Send a command and wait for proof that it worked.

        This is what the actions use, because an action is called by an automation that
        wants to know. There is no acknowledgement topic in the contract: a command is
        proved by the state topic it moves, and disproved by `last_error`. So the wait
        is for whichever comes first.

        A complaint is this command's when it says the id the command was sent with, and
        never when it says another; one that says no id is this command's by its name,
        as it was before the plugin had ids (`_Pending.refused_by`).

        `effect` returns True once the command has visibly happened. A command whose
        effect is already true - zapping to the channel that is already tuned - is
        still sent, but there is nothing left to wait for and pretending otherwise
        would mean waiting out the whole timeout for a state that will never change.

        A command with no observable effect at all (`send_key`, `message`) passes no
        `effect` and waits only long enough to hear a complaint.

        `reasons` are the refusal codes the command has translations for; a complaint
        carrying one of them in `last_error.reason` raises `<name>_<reason>` instead of
        the receiver's English sentence - as a refusal, `Enigma2CommandRefused`, unless
        the code is also in `failures`, the codes that say the receiver tried and failed.
        """
        already_done = effect is not None and effect()
        pending = _Pending(
            cmd=name,
            predicate=effect if effect is not None else lambda: False,
            future=self.hass.loop.create_future(),
            reasons=frozenset(reasons),
            failures=frozenset(failures),
        )
        self._pending.append(pending)
        try:
            await self.async_publish_cmd(name, payload, sent_as=pending.sent_as)
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
        info = await async_request_ha_mode(
            self.hass,
            self.base_topic,
            self.node_id,
            mode,
            timeout,
            wire=lambda payload: self.async_wire_payload("ha_mode", payload)[0],
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
        # The address the packet is actually sent to, so that everything else in Home
        # Assistant - a DHCP discovery, a router integration listing what is on the
        # network - can recognise the same box. Without it the device page knew the
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
            configuration_url=self.web_url,
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
        elif suffix == TOPIC_ZAP_HISTORY:
            self._zap_history_received(msg)
        elif suffix == TOPIC_STREAM:
            self._stream_received(msg)
        elif suffix == TOPIC_STREAM_PLAYABLE:
            self._stream_playable_received(msg)
        elif suffix == TOPIC_STREAM_CLIENTS:
            self._stream_clients_received(msg)
        elif suffix.startswith(f"{TOPIC_EPG_GRID}/"):
            self._epg_grid_received(suffix[len(TOPIC_EPG_GRID) + 1 :], msg.payload)
        elif suffix == TOPIC_TIMESHIFT:
            self._timeshift_received(msg)
        elif suffix == TOPIC_RECORDINGS:
            self._recordings_index_received(msg)
        elif suffix.startswith(f"{TOPIC_RECORDINGS}/"):
            self._recording_folder_received(suffix[len(TOPIC_RECORDINGS) + 1 :], msg)
        elif suffix == TOPIC_CHANNELS:
            self._channels_received(msg)
        elif suffix.startswith(f"{TOPIC_CHANNELS}/"):
            self._bouquet_channels_received(suffix[len(TOPIC_CHANNELS) + 1 :], msg)
        elif suffix == TOPIC_BOUQUETS:
            # The index of every bouquet. `bouquet`, in the singular, is the one the
            # receiver's channel keys are walking, and is stored by name below.
            self._bouquet_index_received(msg)
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
        elif suffix == TOPIC_RELAY_REQUEST:
            self._relay_request_received(msg)
        elif suffix == TOPIC_MENU_ACTION:
            if self.menu_action_handler is not None:
                self.menu_action_handler(msg)
        elif suffix == TOPIC_MENU_EDIT:
            if self.menu_edit_handler is not None:
                self.menu_edit_handler(msg)
        elif suffix == TOPIC_LIST_ANSWER:
            if self.lists is not None:
                self.lists.async_answer_received(msg)
        elif suffix == TOPIC_UPDATE:
            self._update_received(msg)
        else:
            _LOGGER.debug("Ignoring unknown topic %s", msg.topic)

    @callback
    def _update_received(self, msg: ReceiveMessage) -> None:
        """Track the plugin's `update` topic.

        An empty payload is a retraction and clears it; a payload that is not a JSON object
        leaves the last good one alone, as for every state topic.
        """
        if decode_payload(msg.payload) is None:
            self.state.update = None
        elif (update := parse_json_payload(msg.payload)) is not None:
            self.state.update = update
        else:
            return
        self.state.update_retained = msg.retain
        self._async_updated(TOPIC_UPDATE)

    @callback
    def _relay_request_received(self, msg: ReceiveMessage) -> None:
        """Hand a receiver's request for a package to the relay - unless it is retained.

        An event, never retained: a retained request is somebody's leftover, and answering it
        at every reconnect would issue an address nobody is waiting for.
        """
        if msg.retain or self.relay_request_handler is None:
            return
        self.relay_request_handler(msg.payload)

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
        """Track the box's `info` topic and keep the device page current.

        A payload that does not parse leaves the last good one alone, as it always has,
        and is remembered only as unreadable, for `takes_command_ids`. An
        empty one is different in one respect only: it is a retraction, and it withdraws
        the permission `uninstall_offered` reads - the rest of the last payload stays,
        because every entity reading it would otherwise lose its device details to a
        receiver that is merely resetting.
        """
        if not msg.payload:
            if not self.info_retracted:
                self.info_retracted = True
                self._async_updated(TOPIC_INFO)
            return
        if (info := parse_json_payload(msg.payload)) is None:
            if not self.info_unreadable:
                self.info_unreadable = True
                _LOGGER.debug(
                    "Receiver %s published an `info` that is not a JSON object (%d bytes); "
                    "commands go out without an id until one that can be read arrives",
                    self.node_id,
                    len(msg.payload),
                )
            return
        self.info_retracted = False
        self.info_unreadable = False
        self.info_received = True
        self.state.info = info
        self.state.info_retained = msg.retain
        self.state.not_published = normalise_not_published(info)
        self._async_channel_topics_reviewed()
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
        skipped: list[str] = []
        seen_ids: set[str] = set()
        for reader in readers:
            if not isinstance(reader, dict):
                return None
            source_id = reader.get("id")
            kind = reader.get("kind")
            if isinstance(kind, str) and kind not in ("reader", "server", "unknown"):
                # A kind of source a later plugin added. This integration cannot describe
                # it, so it gets no entity - but the payload is not broken, and rejecting
                # it would leave every OSCam entity on its last sample for good.
                #
                # Its id is kept, though, as a source that is there: a later plugin that
                # learns to classify a source it used to call `unknown` reports the same
                # id under the new kind, and a source missing from an authoritative report
                # has its entities removed - the name, area and history somebody gave them
                # with it. Not being able to describe a source is not seeing it gone.
                if isinstance(source_id, str) and _OSCAM_ANY_SOURCE_ID.fullmatch(source_id):
                    skipped.append(source_id)
                continue
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
            # The ids of the sources skipped above for a kind this cannot describe: present,
            # with no entity made for them here.
            "skipped": skipped,
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
            # topic arrives as bytes and bytes that are not UTF-8 decode to nothing -
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
        one kilobyte on a graph. Everything else that is not a plain integer in range -
        a float, a string, a negative, a missing key, a number from a different unit -
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
        this handler does. An **empty payload is a retraction** - the plugin withdrawing
        a retained topic - and it clears the sample, because the alternative is a reading
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
        which is the same shape the topic promises - so a sensor reading this never has
        to know whether a field was missing or nonsense.

        🔴 Two traps are the reason this exists rather than the payload being used as it
        arrives. **`True` is an `int` in Python**, so `running_instances: true` would be
        stored as 1, graph as one healthy instance, and be indistinguishable from a
        measurement; `isinstance(value, bool)` is checked first everywhere below.
        And **`None` is not zero**: a count that could not be taken has to read `unknown`,
        because `0` instances is a real and very interesting reading - it is a channel
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
        # restart in 1970 - the same reading `_refused_at` takes of a complaint's `ts`.
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
        `_invalidate_cam` deliberately - one rule in this file for a retracted topic, not
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
        may be zero - an import that finished with nothing is exactly the failure the
        plugin reports - so zero is kept there. The error is the receiver's sentence,
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
    def _zap_history_received(self, msg: ReceiveMessage) -> None:
        """Cache the receiver's zap history, normalised to the published contract.

        The three answers of `_softcam_received`: an empty payload is a retraction and
        clears the list, a payload that is not JSON leaves the last good one alone, and
        anything else is normalised and kept. A retraction leaves the selects of a
        receiver that names the capability available with nothing to offer - an empty
        history, which is what a receiver that has just restarted its interface has too -
        and so does a topic the broker no longer holds after a restart of Home Assistant:
        the selects do not wait for it. A retraction is not a history, so it does not keep
        them available for a receiver that no longer names the capability.
        """
        if not (decode_payload(msg.payload) or "").strip():
            self.state.zap_history = None
            self.state.zap_history_retained = False
            self._async_updated(TOPIC_ZAP_HISTORY)
            return
        if (payload := parse_json_payload(msg.payload)) is None:
            return
        self.state.zap_history = self._normalize_zap_history(payload)
        self.state.zap_history_retained = msg.retain
        self._async_updated(TOPIC_ZAP_HISTORY)

    @staticmethod
    def _normalize_zap_history(payload: dict[str, Any]) -> dict[str, Any]:
        """Strip an untrusted `zap_history` payload to the four fields of the contract.

        An entry is kept whenever it has a reference: the list has to be exactly the
        receiver's history, so an entry whose name the receiver could not resolve stays,
        with `name` `None`, and the selects give it a label of their own. An entry with
        no reference is dropped, because nothing could tune it. `bouquet` and
        `bouquet_name` are `None` unless they are non-empty strings: an entry with no
        path, a radio bouquet or one the plugin does not publish all say `null`, and
        the filter treats every one of them alike. `True` is an `int` in Python and is
        refused where a number is expected.
        """

        def text(value: Any) -> str | None:
            return value if isinstance(value, str) and value else None

        def whole(value: Any) -> int | None:
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )

        raw = payload.get("entries")
        entries: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            sref = text(item.get("sref"))
            name = text(item.get("name"))
            if sref is None:
                continue
            entries.append(
                {
                    "sref": sref,
                    "name": name,
                    "bouquet": text(item.get("bouquet")),
                    "bouquet_name": text(item.get("bouquet_name")),
                }
            )
        panic_button = payload.get("panic_button")
        return {
            "entries": entries,
            "current": whole(payload.get("current")),
            "limit": whole(payload.get("limit")),
            "panic_button": panic_button if isinstance(panic_button, bool) else None,
        }

    def published_channel_name(self, sref: Any) -> str | None:
        """Return the name a published bouquet gives this service, by identity, if any."""
        for bouquet in self.published_bouquets:
            for channel in bouquet["channels"]:
                if not isinstance(channel, dict):
                    continue
                name = channel.get("name")
                if isinstance(name, str) and name and same_service(channel.get("sref"), sref):
                    return name
        return None

    @property
    def zap_history_panic_button(self) -> bool | None:
        """Return the receiver's panic-button setting, as the history last stated it.

        `None` is a receiver that has not said, which is not `False`: only a stated
        "off" means the 0 key would go back one channel instead of clearing.
        """
        return (self.state.zap_history or {}).get("panic_button")

    def zap_history_entries(self, *, filtered: bool) -> list[dict[str, Any]]:
        """Return the zap history, newest first, with the hidden bouquets left out if asked.

        The unfiltered list is exactly what the receiver published. The filtered one
        applies `history_hidden_bouquets`, and with that option empty it is the same
        list. With it set, an entry stays only when **both** hold:

        1. its bouquet is a published bouquet, and not a hidden one; and
        2. its channel is not a member of any hidden bouquet - so a channel of a hidden
           bouquet reached through another one stays hidden too.

        **It fails toward hiding.** An entry with no path, a radio bouquet or a bouquet
        the plugin does not publish cannot be checked against the option, so while the
        option is set it is left out. The unfiltered select still shows it. And while a
        hidden bouquet is on the receiver's list of bouquets without its channels - its
        `channels/<slug>` topic was withheld, retracted, or has not arrived - rule 2
        cannot be checked for any entry, so every entry is left out: which channels to
        hide is exactly what is not known.

        This hides entries from one Home Assistant entity and nothing else. The receiver
        publishes every entry on `zap_history`, and every channel of every bouquet on
        `channels` or `channels/<slug>`, to anybody on the broker.
        """
        entries = list((self.state.zap_history or {}).get("entries") or [])
        if not filtered:
            return entries
        hidden = self.hidden_bouquet_names
        if not hidden:
            return entries
        published = self.published_bouquets
        self._warn_missing_hidden(hidden, published)
        if (hidden_members := self._members_of_hidden(hidden, published)) is None:
            return []
        visible: list[dict[str, Any]] = []
        for entry in entries:
            bouquet = next(
                (
                    candidate
                    for candidate in published
                    if same_service(candidate.get("sref"), entry.get("bouquet"))
                ),
                None,
            )
            if bouquet is None or bouquet.get("name") in hidden:
                continue
            if service_identity(entry.get("sref")) in hidden_members:
                continue
            visible.append(entry)
        return visible

    @property
    def hidden_bouquet_names(self) -> set[str]:
        """Return the bouquets the options hide, by name. Empty hides nothing."""
        return {
            name
            for name in self.entry.options.get(CONF_HISTORY_HIDDEN_BOUQUETS) or []
            if isinstance(name, str) and name
        }

    def service_hidden(self, sref: Any, bouquet: Any) -> bool:
        """Return whether a service is one the options hide.

        The zap history's filter (`zap_history_entries`), asked about one service that
        is not an entry of the history: the channel on the decoder, a channel a client
        is streaming. `bouquet` is the name the receiver gives with it - the first of
        its configured bouquets that holds the service, or nothing. With the option
        empty nothing is hidden. With it set, a service is shown only when **both**
        hold:

        1. the bouquet named with it is a published bouquet, and not a hidden one; and
        2. the service is not a member of any hidden bouquet - so a channel of a hidden
           bouquet that the receiver names under another one stays hidden.

        **It fails toward hiding**, exactly as that filter does: no bouquet named, a
        bouquet the receiver does not publish, no reference, or a hidden bouquet that
        is on the list without its channels - which then hides every service, because
        which channels to hide is what is not known.

        This decides what Home Assistant shows and offers. The receiver publishes
        every service and every stream client to anybody on the broker.
        """
        hidden = self.hidden_bouquet_names
        if not hidden:
            return False
        published = self.published_bouquets
        self._warn_missing_hidden(hidden, published)
        if (hidden_members := self._members_of_hidden(hidden, published)) is None:
            return True
        if not isinstance(bouquet, str) or bouquet in hidden:
            return True
        if not any(candidate.get("name") == bouquet for candidate in published):
            return True
        identity = service_identity(sref)
        return not identity or identity in hidden_members

    def stream_url_for(self, sref: Any, bouquet: Any) -> str | None:
        """Return the address the receiver streams one service at, or None for none.

        Built at the address Home Assistant itself asks the receiver at
        (`picon_lookup_url`: the typed host when there is one, and never the reported
        address then), with the port and path the receiver publishes on `stream`.

        None - no address at all, never a partial or a guessed one - when the receiver
        does not claim `stream` or has published no usable `stream`; when its stream
        server wants a login, because an address is never built with credentials and
        one without them only answers 401; when the service is one the options hide
        (`service_hidden`, with the bouquet the receiver names for it); and when there
        is no address to ask the receiver at.
        """
        if CAPABILITY_STREAM not in self.capabilities:
            return None
        stream = self.state.stream
        if stream is None or stream["authentication"] is True:
            return None
        if self.service_hidden(sref, bouquet):
            return None
        return build_stream_url(self.picon_lookup_url, stream, sref)

    @callback
    def _stream_received(self, msg: ReceiveMessage) -> None:
        """Cache where the stream server is, normalised.

        An empty payload is a retraction and clears it, a payload that is not a JSON
        object leaves the last good one alone, and anything else is normalised and kept.
        """
        if not (decode_payload(msg.payload) or "").strip():
            self.state.stream = None
            self._async_updated(TOPIC_STREAM)
            return
        if (payload := parse_json_payload(msg.payload)) is None:
            return
        self.state.stream = normalise_stream(payload)
        self._async_updated(TOPIC_STREAM)

    def streamable_bouquets(self) -> list[dict[str, Any]]:
        """Return the bouquets a stream may be offered from, without their channels.

        Read from the one channel list every reader shares (`channel_list`): the
        bouquets the options offer, without the hidden ones. Each is the list's own
        object; `streamable_channels` gives the channels of one of them. The two are
        apart so that listing the bouquets, or one bouquet, does not judge every
        channel of every bouquet - thousands on a large list.
        """
        hidden = self.hidden_bouquet_names
        return [bouquet for bouquet in self.bouquets if bouquet.get("name") not in hidden]

    def streamable_channels(self, bouquet: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the channels of one streamable bouquet that may be shown.

        The ones with a reference that the options do not hide (`service_hidden`) - a
        channel of a hidden bouquet is left out of every other bouquet too.
        """
        return [
            channel
            for channel in bouquet["channels"]
            if self.channel_streamable(bouquet, channel)
        ]

    def channel_streamable(self, bouquet: dict[str, Any], channel: Any) -> bool:
        """Return whether one entry of a streamable bouquet is a channel that may be shown."""
        return (
            isinstance(channel, dict)
            and isinstance(channel.get("sref"), str)
            and bool(channel["sref"])
            and not self.service_hidden(channel["sref"], bouquet.get("name"))
        )

    async def async_stream_playable(
        self, sref: str, timeout: float | None = None
    ) -> bool | None:
        """Ask the receiver whether a service could be played now; None for unknown.

        `cmd/stream_playable` is a question, answered by one message on the event
        topic `stream_playable` - never retained, true for the moment it was asked.
        The answer is this question's when it says the id the question was sent with;
        an answer without an id is told by its reference, which is all a plugin that
        takes no ids - or dropped this one - has to go by (`_stream_playable_received`).

        None is every way of not knowing: no answer within the timeout, a refusal on
        `last_error` (the image will not say), a question that could not be published.
        A caller treats it as "not known", never as "no".
        """
        question = _PlayableQuestion(sref=sref, future=self.hass.loop.create_future())
        self._questions.append(question)
        try:
            await self.async_publish_cmd("stream_playable", sref, sent_as=question.sent_as)
            async with asyncio.timeout(
                STREAM_PLAYABLE_TIMEOUT if timeout is None else timeout
            ):
                return await question.future
        except TimeoutError:
            return None
        except HomeAssistantError as error:
            _LOGGER.debug(
                "Could not ask %s whether a service can be played: %s", self.node_id, error
            )
            return None
        finally:
            self._questions.remove(question)
            if not question.future.done():
                question.future.cancel()

    @callback
    def _stream_playable_received(self, msg: ReceiveMessage) -> None:
        """Hand an answer on `stream_playable` to the question it answers.

        An event, not state: nothing is stored and no listener is woken. A retained
        message is a leftover - the plugin never retains this topic - and an answer
        about tuners that have moved on. `playable` has to be a boolean; anything else
        answers nothing and the question runs into its timeout, which is "unknown".
        """
        if msg.retain or (answer := parse_json_payload(msg.payload)) is None:
            return
        playable = answer.get("playable")
        if not isinstance(playable, bool):
            return
        said = refusal_id(answer)
        sref = answer.get("sref")
        for question in list(self._questions):
            if not question.sent:
                continue
            if said is not None:
                if said == question.command_id:
                    question.answer(playable)
            elif isinstance(sref, str) and same_service(sref, question.sref):
                question.answer(playable)

    def stream_client_rows(self) -> list[dict[str, Any]]:
        """Return the stream clients as they may be shown, the hidden ones without a channel.

        Every row keeps its host and whether it is transcoded. A row whose service the
        options hide (`service_hidden`) loses its reference, its name and its bouquet,
        and says `hidden`. The count is not changed: how many clients hold a tuner is
        the fact the sensor is for.

        `can_stop` says whether the stop action can end that row: the receiver claims
        `stream_stop`, and the row has a reference - the plugin lists a client whose
        reference it holds back, and nothing can name that one. A hidden row with a
        reference can be stopped by its host alone, but only while it is the host's one
        hidden stream: with two hidden services on one host nothing here can say which
        is meant, and the stop is refused (`stream_client_references`).

        Nothing is offered for a receiver that is not online: the list stays retained
        when the plugin stops, and is then a list of clients that have gone.
        """
        if not self.available:
            return []
        stops = CAPABILITY_STREAM_STOP in self.capabilities
        rows: list[dict[str, Any]] = []
        for client in (self.state.stream_clients or {}).get("clients") or []:
            can_stop = stops and client["sref"] is not None
            if self.service_hidden(client["sref"], client["bouquet"]):
                if can_stop:
                    references, _hidden = self.stream_client_references(client["host"])
                    can_stop = len(references) == 1
                rows.append(
                    {
                        "host": client["host"],
                        "sref": None,
                        "name": None,
                        "bouquet": None,
                        "transcoded": client["transcoded"],
                        "hidden": True,
                        "can_stop": can_stop,
                    }
                )
            else:
                rows.append({**client, "hidden": False, "can_stop": can_stop})
        return rows

    def stream_client_references(self, host: str) -> tuple[list[str], bool]:
        """Return the services one host is being streamed, for a stop that names no service.

        A hidden row has no reference anywhere a caller could read it, so a stop of it
        can only name the host; the reference is supplied from here, where the
        receiver's own payload is held, and goes nowhere but into the command. When the
        host has hidden streams, those are the ones meant - a shown one can be named by
        its reference - and the second value says so. One service listed twice for a
        host - once transcoded, once not - is one client to the receiver's command, and
        is returned once. A row without a reference cannot be named and is not among
        them, and an offline receiver's retained list names nobody.
        """
        if not self.available:
            return [], False
        rows = [
            client
            for client in (self.state.stream_clients or {}).get("clients") or []
            if client["sref"] is not None and same_client_host(client["host"], host)
        ]
        hidden = [
            client
            for client in rows
            if self.service_hidden(client["sref"], client["bouquet"])
        ]
        references: dict[str, str] = {}
        for client in hidden or rows:
            references.setdefault(service_identity(client["sref"]), client["sref"])
        return list(references.values()), bool(hidden)

    @callback
    def _stream_clients_received(self, msg: ReceiveMessage) -> None:
        """Cache who the stream server is serving, normalised.

        The three answers of `_zap_history_received`: an empty payload is the plugin
        saying the image's list could not be read, and clears it - a list that names a
        client that has left is worse than none; a payload that is not a JSON object
        leaves the last good one alone; anything else is normalised and kept.
        """
        if not (decode_payload(msg.payload) or "").strip():
            self.state.stream_clients = None
            self._async_updated(TOPIC_STREAM_CLIENTS)
            return
        if (payload := parse_json_payload(msg.payload)) is None:
            return
        self.state.stream_clients = normalise_stream_clients(payload)
        self._async_updated(TOPIC_STREAM_CLIENTS)

    def _members_of_hidden(
        self, hidden: set[str], published: list[dict[str, Any]]
    ) -> set[str] | None:
        """Return the identity of every channel of the hidden bouquets, or None.

        None is "cannot be known": a hidden bouquet is on the list without its channels.
        The set is kept until the channel list or the option changes, because the filter
        runs on every payload of three topics and a hidden bouquet can hold thousands of
        channels.
        """
        key = (self._channel_generation, frozenset(hidden))
        if self._hidden_members is not None and self._hidden_members[0] == key:
            return self._hidden_members[1]
        members: set[str] | None = set()
        for bouquet in published:
            if bouquet.get("name") not in hidden:
                continue
            if not bouquet.get("listed", True):
                members = None
                break
            members.update(
                service_identity(channel.get("sref"))
                for channel in bouquet["channels"]
                if isinstance(channel, dict)
            )
        if members is not None:
            members.discard("")
        self._hidden_members = (key, members)
        return members

    @callback
    def _warn_missing_hidden(
        self, hidden: set[str], published: list[dict[str, Any]]
    ) -> None:
        """Say once, per name, that a hidden bouquet is not on the channel list.

        Nothing is said before a channel list has arrived at all: every name is missing
        from a list nobody has sent yet. A renamed or deleted bouquet is the usual cause,
        and its channels are then no longer recognised as hidden when reached through
        another bouquet - which is what the log line is for.
        """
        if self.channel_list.source == CHANNEL_SOURCE_NONE:
            return
        names = {bouquet.get("name") for bouquet in published}
        for name in sorted(hidden - names - self._hidden_missing_warned):
            _LOGGER.warning(
                "Receiver %s: bouquet %s is hidden from the zap history but is not on "
                "the receiver's channel list; its channels are no longer recognised "
                "when they are reached through another bouquet",
                self.node_id,
                name,
            )
        self._hidden_missing_warned = (self._hidden_missing_warned | hidden) - names

    @callback
    def _announcement_received(self, msg: ReceiveMessage) -> None:
        """Track the box's announcement, which carries the user's chosen name."""
        if (announcement := parse_json_payload(msg.payload)) is None:
            return
        self.state.announcement = announcement
        # The capability list is read from here while `info` has not arrived.
        self._async_channel_topics_reviewed()
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
        device triggers built on it must keep working when the entity is disabled - a
        household that only wants the colour keys in the automation editor should not
        have to keep an entity it never looks at.
        """
        if (payload := parse_json_payload(msg.payload)) is None:
            return
        key = payload.get(ATTR_KEY)
        press = payload.get(ATTR_PRESS, PRESS_SHORT)
        if not isinstance(key, str) or not key:
            return
        if not isinstance(press, str) or press not in (PRESS_SHORT, PRESS_LONG):
            # A kind of press a later plugin may add. Read as a short press it would fire
            # every automation and device trigger written for one; it is no event this
            # integration can describe, so nothing fires. A payload that does not say how
            # long the key was held is a short press.
            _LOGGER.debug("Ignoring key %s with a press this does not know: %r", key, press)
            return

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
    def async_record_install(self, path: str, version: str, proof: str) -> None:
        """Keep how the last install of the plugin from here was proven, for diagnostics.

        `path` is how it was installed - `mqtt`, `ssh`, `ssh_forced` or `ssh_downgrade` -
        and `proof` the one that passed (`installer.PROOF_*`, `mqtt_update.PROOF_*`). Held
        for this run of the entry only: it is a note beside the log line, not a state, and
        the receiver reports the version it runs by itself.
        """
        self.last_install = {
            "path": path,
            "version": version,
            "proof": proof,
            "time": dt_util.utcnow().isoformat(),
        }

    @callback
    def async_record_local_error(self, command: str, error: str) -> None:
        """Put a failure of Home Assistant's own on „Ostatni błąd".

        The sensor remembers what the receiver refused. A forced reinstall is refused or
        fails on this side, often for a receiver whose plugin will never publish again, so
        its failure is recorded here and the sensor takes it as the newest complaint, with
        Home Assistant as its source.
        """
        self.state.local_error = {
            "cmd": command,
            "error": error,
            "ts": dt_util.utcnow().isoformat(),
        }
        self._async_updated(TOPIC_LOCAL_ERROR)

    @callback
    def async_clear_error_record(self) -> None:
        """Forget the complaint "Ostatni błąd" shows, the receiver's or Home Assistant's own.

        Called once the receiver has emptied its `last_error` for `cmd/clear_error`. The
        sensor keeps its own record precisely because the topic is cleared by the next
        command that succeeds; a clear somebody asked for is the one case in which the
        record has to go as well.
        """
        self.state.local_error = None
        self._async_updated(TOPIC_ERROR_CLEARED)

    @callback
    def _last_error_received(self, msg: ReceiveMessage) -> None:
        """Track the box's complaints, and fail the command that caused one.

        A retained payload is the complaint the broker had before we asked, so it can
        never be the answer to a command we just sent. An empty payload only clears
        state: it carries no command identity, so treating it as an acknowledgement
        could complete an unrelated concurrent command and hide its later error.

        Whose complaint it is: the command's whose id it says, and without an id every
        waiting command's of that name (`_Pending.refused_by`).
        """
        error = parse_json_payload(msg.payload)
        self.state.last_error = error
        self.state.last_error_retained = msg.retain
        self.state.last_error_empty = not msg.payload
        if not msg.retain and error is not None:
            text = str(error.get("error") or "")
            reason = error.get("reason")
            for pending in list(self._pending):
                if pending.refused_by(error):
                    pending.fail(text, reason)
                elif pending.sent and pending.cmd == error.get("cmd"):
                    _LOGGER.debug(
                        "A refusal of cmd/%s on %s says another command's id; the "
                        "cmd/%s waited for here is not failed by it",
                        pending.cmd,
                        self.node_id,
                        pending.cmd,
                    )
            # A question that could not be asked is refused here and answered nowhere:
            # its wait ends as "the receiver could not say".
            said = refusal_id(error)
            for question in list(self._questions):
                if question.sent and (
                    said == question.command_id
                    if said is not None
                    else error.get("cmd") == "stream_playable"
                ):
                    question.answer(None)
            # A list that could not be shown is refused by its id, and by nothing else.
            if self.lists is not None:
                self.lists.async_refusal_received(error)
        self._async_updated(TOPIC_LAST_ERROR)

    # --------------------------------------------------------------- recordings

    @callback
    def _timeshift_received(self, msg: ReceiveMessage) -> None:
        """Track the timeshift state: a retraction clears it, a payload not JSON is ignored."""
        if _retracted(msg.payload):
            self.state.timeshift = None
        elif (payload := parse_json_payload(msg.payload)) is None:
            return
        else:
            self.state.timeshift = normalise_timeshift(payload)
        self._async_updated(TOPIC_TIMESHIFT)

    @callback
    def _recordings_index_received(self, msg: ReceiveMessage) -> None:
        """Track the library index.

        The answers every retained topic here gets: an empty payload is a retraction and
        clears it, a payload that is not a JSON object leaves the last good one alone. The
        broker's replay of the index already held changes nothing and wakes nobody. A
        fresh one is always news, also when it lists the same library: it is the answer to
        a refresh, and says when the library was read.
        """
        if _retracted(msg.payload):
            if self.state.recordings_index is None:
                return
            self.state.recordings_index = None
        elif (payload := parse_json_payload(msg.payload)) is None:
            return
        else:
            index = normalise_index(payload)
            if index == self.state.recordings_index and msg.retain:
                return
            self.state.recordings_index = index
        self.state.recordings_index_retained = msg.retain
        self._async_updated(TOPIC_RECORDINGS)

    @callback
    def _recording_folder_received(self, slug: str, msg: ReceiveMessage) -> None:
        """Track one folder's `recordings/<slug>`, or forget it when it is retracted.

        Kept whatever the index says so far, because the index can arrive after it.
        A folder published again as it was wakes nobody.
        """
        if not slug or "/" in slug:
            return
        folders = self.state.recording_folders
        if _retracted(msg.payload):
            if folders.pop(slug, None) is None:
                return
        elif (payload := parse_json_payload(msg.payload)) is None:
            return
        else:
            recordings = normalise_folder(payload)
            if folders.get(slug) == recordings:
                return
            folders[slug] = recordings
        self._async_updated(TOPIC_RECORDING_FOLDERS)

    @property
    def recording_locations(self) -> list[dict[str, Any]]:
        """Return the movie locations of the library index, or none while there is none."""
        return list((self.state.recordings_index or {}).get("locations") or [])

    def visible_library(
        self,
    ) -> list[tuple[dict[str, Any], list[tuple[dict[str, Any], list[dict[str, Any]]]]]]:
        """Return the library as Home Assistant shows it: locations, folders, recordings.

        Every list Home Assistant builds of the library is this one - the media browser,
        `get_recordings`, the library's counts, what may be played or deleted. A folder is
        in it only with at least one recording the options do not hide, and a location
        only with at least one such folder: the name of a folder that holds nothing but a
        hidden bouquet's recordings says what is in it.
        """
        shown = []
        for location in self.recording_locations:
            folders = [
                (folder, recordings)
                for folder in location["folders"]
                # The folder is the index's own, so its topic is read without looking it up.
                if (
                    recordings := visible(
                        self.state.recording_folders.get(folder["slug"]) or [],
                        self.service_hidden,
                    )
                )
            ]
            if folders:
                shown.append((location, folders))
        return shown

    def visible_recording_count(self) -> int | None:
        """Return how many recordings the library shows, or None while there is no index."""
        if self.state.recordings_index is None:
            return None
        return sum(
            len(recordings)
            for _location, folders in self.visible_library()
            for _folder, recordings in folders
        )

    def find_recording(
        self, rec_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        """Return a recording the library shows, with its location and folder, by its id.

        A hidden one is not found: it is played or deleted from here exactly as often as
        it is shown here, which is never.
        """
        for location, folders in self.visible_library():
            for folder, recordings in folders:
                for recording in recordings:
                    if recording["id"] == rec_id:
                        return location, folder, recording
        return None

    @property
    def recording_delete_permission(self) -> bool | None:
        """Return the receiver's own answer about deleting recordings, if it gave one.

        `recording_delete_allowed` is set on the receiver and cannot be written over MQTT.
        None is a plugin that does not report it, which is not a "no".
        """
        settings = self.state.info.get("settings")
        if not isinstance(settings, dict):
            return None
        value = settings.get(CONF_RECORDING_DELETE_ALLOWED)
        return value if isinstance(value, bool) else None

    @property
    def recording_trash(self) -> bool | None:
        """Return whether the receiver says its trash is on; None when it does not say."""
        value = self.state.info.get(INFO_RECORDING_TRASH)
        return value if isinstance(value, bool) else None

    def recording_delete_refusal(self) -> str | None:
        """Return why a delete cannot be asked of this receiver now, or None when it can.

        The receiver claims `recording_delete` only while its permission is on and its
        trash is there to receive the recording, so a receiver that claims it is asked.
        One that does not is refused here, with the reason its `info` gives: the
        permission off, then the trash off; a receiver that says neither has a plugin or
        an image that cannot do it. A retracted `info` claims nothing. The answer is a
        translation key.
        """
        if not self.info_retracted and CAPABILITY_RECORDING_DELETE in self.capabilities:
            return None
        if self.recording_delete_permission is False:
            return "recording_delete_not_permitted"
        if self.recording_trash is False:
            return "recording_delete_trash_off"
        return "recording_delete_unsupported"

    # ------------------------------------------------------------ channel list

    @property
    def _channel_topics_live(self) -> bool:
        """Return whether the per-bouquet topics are what the channel list is read from."""
        return self.channel_topics_claimed and self.state.bouquet_index is not None

    def _index_bouquets(self) -> list[dict[str, Any]]:
        """Return the bouquets of the index, or none while there is no index."""
        return (self.state.bouquet_index or {}).get("bouquets") or []

    def _index_needs_channels(self) -> bool:
        """Return whether the index names a bouquet that has no topic of its own.

        A bouquet whose name has no ASCII letter or digit in it - one written in Cyrillic,
        Greek, Arabic, Hebrew or CJK - has no slug, so the plugin publishes no
        `channels/<slug>` for it. It is in the index, with `slug` empty, and its channels
        are in `channels` and nowhere else.
        """
        return any(not entry["slug"] for entry in self._index_bouquets())

    @callback
    def _channels_received(self, msg: ReceiveMessage) -> None:
        """Track the combined `channels` topic, unless the per-bouquet topics have it all.

        While it is the source it is read as it always was: an empty payload, or one that
        is not a JSON object, is no channel list. While the list on show comes from the
        per-bouquet topics it is not kept - it would be a second copy of every list they
        carry, and they win where the two differ - and `channels_released` remembers that
        it is on the broker, so it can be asked for again (`_async_leave_topic_list`).

        The exception is an index with a bouquet that has no topic of its own: `channels`
        is then the only place that bouquet's list is, so it is kept, and read for that
        bouquet alone.
        """
        if msg.retain and msg.timestamp == self._refetch_answered:
            # The broker's answer to the request below, handed to the wildcard
            # subscription as well because it never had a retained `channels`: read once.
            self._refetch_answered = None
            return
        channels = parse_json_payload(msg.payload)
        self._cancel_refetch()
        if self._topic_list is not None and self._channel_topics_live:
            if not self._index_needs_channels():
                self.state.channels = None
                self.state.channels_released = channels is not None
                return
            self.state.channels_released = False
            if channels == self.state.channels:
                # Published again as it was: the payload in hand stays the one copy.
                return
            self.state.channels = channels
            self._async_channel_topics_changed(adopt=True)
            return
        self.state.channels = channels
        self.state.channels_released = False
        if self._topic_list is not None:
            # A list kept on show after the receiver stopped claiming the per-bouquet
            # topics. `channels` has spoken, so it is the source again.
            self._topic_list = None
            self._channel_generation += 1
        elif self._channel_topics_live:
            # The per-bouquet list was being waited for. `channels` may be what it was
            # waiting for - a bouquet with no topic of its own - and then that list is
            # shown, and has woken everybody.
            self._async_channel_topics_changed()
            if self._topic_list is not None:
                return
        self._async_updated(TOPIC_CHANNELS)

    @callback
    def _bouquet_index_received(self, msg: ReceiveMessage) -> None:
        """Track the `bouquets` index of the per-bouquet channel topics.

        The answers every retained topic here gets: an empty payload is a retraction and
        clears it, a payload that is not a JSON object leaves the last good one alone. An
        index equal to the one held - which is what every reconnect sends - changes
        nothing and wakes nobody.
        """
        if _retracted(msg.payload):
            if self.state.bouquet_index is None:
                return
            self.state.bouquet_index = None
        elif (payload := parse_json_payload(msg.payload)) is None:
            return
        else:
            index = _normalise_bouquet_index(payload)
            if index == self.state.bouquet_index:
                return
            self.state.bouquet_index = index
        self._async_updated(TOPIC_BOUQUETS)
        self._async_channel_topics_changed()

    @callback
    def _bouquet_channels_received(self, slug: str, msg: ReceiveMessage) -> None:
        """Track one bouquet's `channels/<slug>`, or forget it when it is retracted.

        Kept whatever the capability list says so far, because `info` can arrive after
        it; whether it is read is `channel_list`'s decision. Listeners are not woken per
        topic: the list they read is replaced as a whole.

        A payload that differs from the one held only in `generated` is the same list
        with a new stamp - a restart of the plugin stamps every bouquet anew - so the
        stamp is taken and nothing is rebuilt.
        """
        if not slug or "/" in slug:
            return
        lists = self.state.bouquet_channels
        if _retracted(msg.payload):
            if lists.pop(slug, None) is None:
                return
        elif (payload := parse_json_payload(msg.payload)) is None:
            return
        else:
            channels = payload.get("channels")
            if not isinstance(channels, list):
                return
            sref = payload.get("sref")
            received = {
                "bouquet": payload.get("bouquet"),
                "sref": sref if isinstance(sref, str) else None,
                "generated": payload.get("generated"),
                "channels": channels,
            }
            held = lists.get(slug)
            if (
                held is not None
                and held["sref"] == received["sref"]
                and held["bouquet"] == received["bouquet"]
                and held["channels"] == channels
            ):
                held["generated"] = received["generated"]
                return
            lists[slug] = received
        self.seen.add(TOPIC_BOUQUET_CHANNELS)
        self.updates[TOPIC_BOUQUET_CHANNELS] = self.updates.get(TOPIC_BOUQUET_CHANNELS, 0) + 1
        self._async_channel_topics_changed()

    @callback
    def _async_channel_topics_reviewed(self) -> None:
        """Re-read what `info` says about the per-bouquet topics, and act if it moved.

        Three things: whether `channel_topics` is claimed, which bouquets' lists the
        receiver says it withheld, and whether it withheld `channels` - a topic that will
        never arrive, which is not the same as one that has not arrived yet. `info` is
        published far more often than any of them changes, so nothing is recomputed
        unless one did. The capability is read from the announcement while `info` has not
        arrived, which is why the announcement is reviewed too.
        """
        topics = {entry["topic"] for entry in self.state.not_published or []}
        withheld = frozenset(
            topic[len(TOPIC_CHANNELS) + 1 :]
            for topic in topics
            if topic.startswith(f"{TOPIC_CHANNELS}/")
        )
        inputs = (self.channel_topics_claimed, withheld, TOPIC_CHANNELS in topics)
        if inputs == self._channel_topics_inputs:
            return
        self._channel_topics_inputs = inputs
        self._withheld_lists = withheld
        self._withheld_combined = TOPIC_CHANNELS in topics
        self._async_channel_topics_changed()

    def _bouquet_list(self, entry: dict[str, Any]) -> list[Any] | None:
        """Return the channels of one bouquet of the index, when a topic has them.

        From the bouquet's own topic, and the payload counts only when it says it is this
        bouquet's: two bouquets that slug alike swap topics when the receiver's order
        changes, and between the index and the two payloads arriving the topic still
        holds the other bouquet's list.

        A bouquet with no topic of its own is read from `channels`, by its reference and
        its name, when `channels` is here and lists it.
        """
        if not entry["slug"]:
            bouquets = (self.state.channels or {}).get("bouquets")
            for bouquet in bouquets if isinstance(bouquets, list) else []:
                if (
                    isinstance(bouquet, dict)
                    and bouquet.get("sref") == entry["sref"]
                    and bouquet.get("name") == entry["name"]
                ):
                    channels = bouquet.get("channels")
                    return channels if isinstance(channels, list) else None
            return None
        received = self.state.bouquet_channels.get(entry["slug"])
        if received is None or received["sref"] != entry["sref"]:
            return None
        return received["channels"]

    def _bouquet_list_awaited(self, entry: dict[str, Any]) -> bool:
        """Return whether a bouquet's missing list may still arrive.

        Not when the receiver says it withheld the topic the list would be on. For a
        bouquet with no topic of its own that topic is `channels`, and a `channels` that
        is here without the bouquet has nothing more to say about it either.
        """
        if entry["slug"]:
            return entry["slug"] not in self._withheld_lists
        return self.state.channels is None and not self._withheld_combined

    def _bouquet_lists_awaited(self) -> list[str]:
        """Return the bouquets whose list may still arrive: by slug, or by name without one."""
        return [
            entry["slug"] or entry["name"]
            for entry in self._index_bouquets()
            if self._bouquet_list(entry) is None and self._bouquet_list_awaited(entry)
        ]

    @callback
    def _async_channel_topics_changed(self, *, adopt: bool = False) -> None:
        """Decide what the channel list is after anything about the per-bouquet topics moved.

        Complete - every bouquet of the index has its list, or is one the receiver says it
        withheld - and it is shown at once. Incomplete, and what is on show stays until
        `CHANNEL_TOPICS_SETTLE` seconds pass without another message; then the list is
        shown as far as it goes. Not the source at all - no capability, or no index - and
        `channels` is.
        """
        if not self._channel_topics_live:
            self._async_leave_topic_list()
            return
        if self._index_needs_channels() and self.state.channels is None:
            if self.state.channels_released:
                # Let go while every bouquet had a topic of its own; one of them has
                # none now, so the broker is asked for `channels` again.
                self._async_refetch_channels()
        if self._bouquet_lists_awaited():
            self._cancel_settle()
            self._settle_cancel = async_call_later(
                self.hass, CHANNEL_TOPICS_SETTLE, self._settled
            )
            return
        self._async_show_topic_list(adopt=adopt)

    @callback
    def _settled(self, _now: datetime) -> None:
        """The burst is over and a bouquet's list is still missing: show what there is."""
        self._settle_cancel = None
        if self._channel_topics_live:
            self._async_show_topic_list()

    @callback
    def _cancel_settle(self) -> None:
        if self._settle_cancel is not None:
            self._settle_cancel()
            self._settle_cancel = None

    @callback
    def _async_show_topic_list(self, *, adopt: bool = False) -> None:
        """Replace the channel list with one built from the index and the per-bouquet topics.

        The lists are the payloads' own - nothing is copied - and the combined `channels`
        payload is let go, so a receiver's channels are held once. It is kept only while
        the index names a bouquet with no topic of its own, whose list is nowhere else.
        Listeners are woken once, and not at all when the list is the one already on
        show; `adopt` then still takes the new list in place of the equal one, so that an
        old `channels` payload is not kept alive by the list that was read from it.
        """
        self._cancel_settle()
        index = self.state.bouquet_index or {}
        bouquets: list[dict[str, Any]] = []
        awaited = False
        for entry in self._index_bouquets():
            channels = self._bouquet_list(entry)
            if channels is None and self._bouquet_list_awaited(entry):
                awaited = True
            bouquets.append(
                {
                    "name": entry["name"],
                    "sref": entry["sref"],
                    "channels": channels if channels is not None else [],
                    "slug": entry["slug"],
                    "count": entry["count"],
                    "listed": channels is not None,
                }
            )
        shown = ChannelList(
            source=CHANNEL_SOURCE_BOUQUET_TOPICS,
            bouquets=tuple(bouquets),
            names=tuple(bouquet["name"] for bouquet in bouquets),
            generated=index.get("generated"),
            complete=not awaited,
        )
        # What was read from `channels` while it was the source holds that payload as
        # well, and nothing reads it while this list is on show. Let go on both paths
        # below: where `channels` is kept for a bouquet with no topic of its own, the
        # payload kept is the one in hand, and the list read from an earlier one would
        # keep that superseded payload alive beside it.
        self._combined_from = None
        self._combined_list = ChannelList()
        if not self._index_needs_channels():
            if self.state.channels is not None:
                self.state.channels = None
                self.state.channels_released = True
            self._cancel_refetch()
        elif self.state.channels is not None:
            self._cancel_refetch()
        if shown == self._topic_list:
            if adopt:
                self._topic_list = shown
            return
        if awaited:
            _LOGGER.debug(
                "Receiver %s: showing the channel list without %s, whose topics have not arrived",
                self.node_id,
                ", ".join(self._bouquet_lists_awaited()),
            )
        self._topic_list = shown
        self._channel_generation += 1
        self._async_updated(TOPIC_CHANNELS)

    @callback
    def _async_leave_topic_list(self) -> None:
        """Go back to `channels` after the per-bouquet topics stopped being the source.

        That is a receiver that no longer claims `channel_topics` - a plugin put back to
        0.4.0 - or an index that was retracted. With the `channels` payload in hand, or
        none on the broker, the list is simply what `channels` holds. When it was let go,
        the list on show stays - it is the same lists - and the broker is asked for
        `channels` again by a subscription of its own, which is answered with the retained
        payload; `_channels_received` then makes it the source. A broker that does not
        answer ends the wait (`_async_refetch_unanswered`).
        """
        self._cancel_settle()
        if self._topic_list is None:
            return
        if self.state.channels_released:
            self._async_refetch_channels()
            return
        self._topic_list = None
        self._channel_generation += 1
        self._async_updated(TOPIC_CHANNELS)

    @callback
    def _async_refetch_channels(self) -> None:
        """Ask the broker for the retained `channels` again, once, and not for ever.

        A subscription of its own, because a broker answers a subscribe with the retained
        message and the wildcard subscription has had its answer. Only that retained
        answer is taken from it; a `channels` published meanwhile arrives on the wildcard
        like any other, and either one ends the request.

        `channels_released` can be out of date: a `channels` retracted while Home
        Assistant was not connected leaves nothing to answer with. So the wait is bound -
        `CHANNELS_REFETCH_GRACE` seconds from the broker confirming the subscription, and
        `SUBSCRIBE_TIMEOUT` more than that when the confirmation never comes - and an
        unanswered request is read as "there is no `channels`".
        """
        if self._refetch is not None:
            return
        topic = self.state_topic(TOPIC_CHANNELS)
        cleanups: list[CALLBACK_TYPE] = []
        timer: list[CALLBACK_TYPE] = []

        @callback
        def _cancel() -> None:
            while cleanups:
                cleanups.pop()()

        @callback
        def _disarm() -> None:
            while timer:
                timer.pop()()

        @callback
        def _unanswered(_now: datetime) -> None:
            timer.clear()
            if self._refetch is _cancel:
                self._async_refetch_unanswered()

        @callback
        def _arm(seconds: float) -> None:
            _disarm()
            timer.append(async_call_later(self.hass, seconds, _unanswered))

        @callback
        def _established() -> None:
            if self._refetch is _cancel:
                _arm(CHANNELS_REFETCH_GRACE)

        @callback
        def _answered(answer: ReceiveMessage) -> None:
            if self._refetch is _cancel and answer.retain:
                self._channels_received(answer)
                self._refetch_answered = answer.timestamp

        async def _subscribe() -> None:
            try:
                unsubscribe = await mqtt.async_subscribe(
                    self.hass, topic, _answered, encoding=None
                )
            except HomeAssistantError:
                # No broker to ask; the bound above still ends the wait.
                return
            if self._refetch is _cancel:
                cleanups.append(unsubscribe)
            else:
                # Answered, or stopped, while the subscription was being made.
                unsubscribe()

        self._refetch = _cancel
        cleanups.append(_disarm)
        # Registered before subscribing, as everywhere here, so a subscription that
        # completes at once cannot land between the two calls and go unnoticed.
        cleanups.append(
            mqtt.async_on_subscribe_done(self.hass, topic, mqtt.DEFAULT_QOS, _established)
        )
        _arm(SUBSCRIBE_TIMEOUT + CHANNELS_REFETCH_GRACE)
        self.entry.async_create_task(
            self.hass, _subscribe(), f"{DOMAIN} channels {self.node_id}"
        )

    @callback
    def _async_refetch_unanswered(self) -> None:
        """The broker had no `channels` to answer with: stop waiting for one.

        The list kept on show for the wait goes, because there is nothing to go back to.
        A receiver still read from the per-bouquet topics needs nothing done here: it
        asked for the sake of a bouquet with no topic of its own, which is already being
        waited for and is shown without its list when that wait is over.
        """
        self._cancel_refetch()
        self.state.channels_released = False
        if not self._channel_topics_live and self._topic_list is not None:
            self._topic_list = None
            self._channel_generation += 1
            self._async_updated(TOPIC_CHANNELS)

    @callback
    def _cancel_refetch(self) -> None:
        if self._refetch is not None:
            refetch, self._refetch = self._refetch, None
            refetch()

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
        successful call ends the run, so the next failure - which may be a different bug
        entirely - is logged at error level again rather than hidden until a reload.
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
    before this integration when the user already has it - and does nothing at all when
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

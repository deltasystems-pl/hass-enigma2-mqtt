"""The relay: a verified plugin package served to one receiver that has no internet of its own.

A receiver without internet cannot fetch a release from the plugin's origin, and it must not
have to (ADR-0008, section 5). Home Assistant fetches and verifies the package (`release_package`),
keeps the bytes in memory and serves them on a short-lived, unguessable address on the local
network; the receiver downloads them from there and verifies them itself against its own signed
index before opkg sees a byte. The relay is a courier, not an authority: the worst an attacker on
the path achieves is a refusal.

**Who may fetch.** Nobody authenticates to this view - the receiver has no Home Assistant
credentials, and must not be given any - so the grant is bound instead:

- to one **token**, `secrets.token_urlsafe(32)` (43 url-safe characters, 256 bits), in the path;
- to the **receiver's IPv4 address** as it reports it on `info.ip`, compared as an address with
  an IPv4-mapped IPv6 address (how a dual-stack listener reports an IPv4 peer) unwrapped first. A
  request from any other address - a neighbour on the receiver's own subnet included - gets the
  same `404` as an unknown token, and a warning in the log. The address is the connection's peer
  as Home Assistant's HTTP server reports it, never a header: Home Assistant itself rewrites the
  peer from `X-Forwarded-For` for a configured trusted proxy, so the binding is exactly as strong
  as that configuration. A receiver with no usable address, or only an IPv6 one, gets no grant at
  all - never an unbound one;
- to **ten minutes**. Until then the address may be fetched again and again: the URL crosses the
  broker, so a single use would only let another broker client spend it first, and the bytes are a
  public signed package anyway.

**What it serves.** Exactly the verified bytes of that grant, from memory: no file, no directory,
no other path. `GET` only (a `HEAD` is not routed), the whole package whatever `Range` asks for.

**How many.** One grant per (receiver, version) - asking again for the same version gets the
same address while enough of its ten minutes are left - and at most three per receiver. A fourth
version evicts the receiver's oldest grant, unless that grant is being sent at this moment or an
update over MQTT that Home Assistant is following downloads from it; such a grant is not
replaced either. Then the new one is refused, so that a forged request
cannot take the package away from a download in flight. A receiver's grants go when its entry is
unloaded - but for one an update over MQTT downloads from, which goes at its own expiry.

**Which address.** Home Assistant's own IPv4 address on the receiver's subnet, among the adapters
Home Assistant knows; else its internal URL (`get_url` with the external and cloud URLs
disallowed); never the external URL: the package is for one device on the local network. Whatever
is chosen must have the one shape the receiver accepts - `http` or `https`, an IPv4 address or a
host name of letters, digits, dots and hyphens, a port if any, nothing else - or it is not
offered: an internal URL that carries a user name and password never reaches the broker.

**The log and the household.** A warning of each kind is written once per receiver in ten
minutes and repeated at debug level, so that no broker client and no holder of a token can fill
the log. The token is never written whole. A refusal after a request has been accepted - no
address for the receiver, none for Home Assistant, the download - is also a persistent
notification. So is an answer nobody fetched before it expired, worded as exactly what Home
Assistant knows - offered, not downloaded - because the request may have been forged by any
broker client, or the receiver may have declined for a reason of its own. A refusal by the
version rule is only logged: any broker client can ask for a refused version for free.

**Starvation, the residual.** A new download is made once a minute per receiver; a broker
client that asks first for another eligible version each minute - four of them, since a live
grant answers again for free and a fourth version evicts the oldest - takes every minute. It
could deny the install more cheaply anyway, by answering the receiver's request id on
`cmd/relay` before Home Assistant does; what it costs Home Assistant is one verified download a
minute per receiver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components import network, persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.translation import async_get_translations
import homeassistant.util.dt as dt_util

from . import release_index, release_package
from .const import DOMAIN
from .release_package import PackageError, PackageSource, async_fetch_release, eligible_release
from .release_store import async_release_index_cache

_LOGGER = logging.getLogger(__name__)

RELAY_PATH = "/api/enigma2_mqtt/relay/"
RELAY_LIFETIME = 600.0
# A grant asked for again is reused only while this much of it is left: the receiver waits 120 s
# for an answer and then downloads, and an address that expires in the middle is no answer.
RELAY_REUSE_MARGIN = 180.0
RELAY_GRANTS_PER_RECEIVER = 3
# A download for `relay_request` is made at most once a minute per receiver.
RELAY_REQUEST_INTERVAL = 60.0
# A warning of one kind about one receiver is written once in this window, then at debug level.
RELAY_WARNING_WINDOW = 600.0
# A request is a few dozen bytes; anything longer is not one.
RELAY_REQUEST_MAX_BYTES = 1024

_DATA_KEY = f"{DOMAIN}_relay"
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
# The only address the receiver accepts from Home Assistant (plugin `updatehelper.py`,
# RELAY_URL, and TRANSACTION.md section 7): its host - an IPv4 address or a host name, no user
# part - a port if any, the fixed path and a 43-character token, nothing after it.
_RELAY_URL = re.compile(
    r"https?://[A-Za-z0-9.-]{1,253}(?::([0-9]{1,5}))?"
    + re.escape(RELAY_PATH)
    + r"[A-Za-z0-9_-]{43}",
    re.ASCII,
)
_EXAMPLE_TOKEN = "A" * 43

# A refused download, in the card's words (the same keys `update.py` raises).
_NOT_FETCHED = {
    release_package.WITHDRAWN: "update_version_withdrawn",
    release_package.BELOW_FLOOR: "update_version_below_floor",
    release_package.DOWNLOAD: "update_download_failed",
    release_package.DIGEST: "update_download_failed",
}


class RelayError(HomeAssistantError):
    """A relay that cannot be offered, with the sentence that says why."""

    def __init__(self, key: str, *, detail: str | None = None, **placeholders: str) -> None:
        """Keep the translation key, and for the log the key and what exactly was wrong."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders=placeholders or None,
        )
        self.key = key
        self.detail = detail
        self.placeholders = placeholders


def peer_address(value: Any) -> ipaddress.IPv4Address | None:
    """An IPv4 address from a peer or a reported address, IPv4-mapped IPv6 unwrapped."""
    if not isinstance(value, str) or not value:
        return None
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        return address.ipv4_mapped
    return address


def receiver_address(value: Any) -> ipaddress.IPv4Address | None:
    """The address a grant may be bound to: an IPv4 address some host on a network can have.

    Unspecified, multicast and reserved addresses (the limited broadcast among them) are never
    a TCP peer; a receiver reporting one gets the same refusal as a receiver reporting none,
    rather than a grant nobody can fetch.
    """
    address = peer_address(value)
    if address is None or address.is_unspecified or address.is_multicast or address.is_reserved:
        return None
    return address


def relay_url_ok(url: str) -> bool:
    """Whether the receiver accepts `url` as an address from Home Assistant."""
    match = _RELAY_URL.fullmatch(url)
    return match is not None and (match.group(1) is None or 0 < int(match.group(1)) < 65536)


def notification_id(node_id: str) -> str:
    """The one relay notification of a receiver, replaced by the next."""
    return f"{DOMAIN}_relay_{node_id}"


@dataclass(slots=True)
class Grant:
    """One package, one receiver address, one token, ten minutes."""

    token: str
    node_id: str
    version: str
    data: bytes = field(repr=False)
    sha256: str
    address: ipaddress.IPv4Address
    issued: float
    expires: float
    # The receiver's name, for a notification about it.
    receiver: str = ""
    # Where the receiver was told to fetch it, without the token: set when it was answered.
    base: str | None = None
    # Responses being sent right now: a grant being sent is neither evicted nor replaced.
    fetching: int = 0
    # Updates over MQTT following a transaction that downloads from this grant: held the same
    # way, for as long as Home Assistant follows it (`mqtt_update`) - and, when the card that
    # followed it went away mid-follow, until the grant expires.
    held: int = 0
    served: int = 0
    cancel: CALLBACK_TYPE | None = field(default=None, repr=False)


class Relay:
    """Every grant this Home Assistant holds, and the view that serves them."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Hold nothing yet; the view is registered with the first grant."""
        self.hass = hass
        self._grants: dict[str, Grant] = {}
        self._answered: dict[str, float] = {}
        # (receiver, kind) -> [when the last warning was written, how many were held back].
        self._warned: dict[tuple[str, str], list[float]] = {}
        self._view_registered = False

    # ------------------------------------------------------------------- grants --

    def grants(self, node_id: str) -> list[Grant]:
        """The live grants of one receiver, oldest first."""
        self._purge()
        return sorted(
            (grant for grant in self._grants.values() if grant.node_id == node_id),
            key=lambda grant: grant.issued,
        )

    def reusable(
        self, node_id: str, version: str, sha256: str, address: ipaddress.IPv4Address
    ) -> Grant | None:
        """The live grant that already answers this request, when one does.

        The same version, the same signed bytes, the same receiver address, and enough of its
        ten minutes left for the receiver to start the download.
        """
        now = dt_util.utcnow().timestamp()
        for grant in self.grants(node_id):
            if (
                grant.version == version
                and grant.sha256 == sha256
                and grant.address == address
                and grant.expires - now >= RELAY_REUSE_MARGIN
            ):
                return grant
        return None

    @callback
    def grant(
        self, node_id: str, receiver_ip: Any, package: PackageSource, *, receiver: str = ""
    ) -> Grant:
        """A grant of `package` to the receiver at `receiver_ip`, new or reused.

        Raises RelayError when the receiver has no usable IPv4 address, or when making room
        would take a grant that is being sent.
        """
        address = receiver_address(receiver_ip)
        if address is None:
            raise RelayError("relay_no_ipv4")
        if (reused := self.reusable(node_id, package.version, package.sha256, address)) is not None:
            return reused
        now = dt_util.utcnow().timestamp()
        held = self.grants(node_id)
        for grant in [grant for grant in held if grant.version == package.version]:
            # Too close to its end, or for other bytes or another address: replaced, never
            # a second grant of the same version - unless it is being sent right now.
            if grant.fetching or grant.held:
                raise RelayError("relay_busy")
            self.drop(grant)
            held.remove(grant)
        if len(held) >= RELAY_GRANTS_PER_RECEIVER:
            evictable = [grant for grant in held if not grant.fetching and not grant.held]
            if not evictable:
                raise RelayError("relay_busy")
            self.drop(evictable[0])
        self._register_view()
        grant = Grant(
            token=secrets.token_urlsafe(32),
            node_id=node_id,
            version=package.version,
            data=package.data,
            sha256=package.sha256,
            address=address,
            issued=now,
            expires=now + RELAY_LIFETIME,
            receiver=receiver,
        )
        grant.cancel = async_call_later(
            self.hass, RELAY_LIFETIME, callback(lambda _now: self._expired(grant))
        )
        self._grants[grant.token] = grant
        return grant

    @callback
    def drop_node(self, node_id: str) -> None:
        """Forget every grant of one receiver - its entry is going.

        Except one an update over MQTT downloads from (`held`): the receiver's transaction does
        not stop because Home Assistant reloaded the entry - saving its options does that - and
        taking the address away mid-download would fail an update the card that replaces this one
        is showing. Such a grant goes at its own expiry, within ten minutes.
        """
        for grant in list(self._grants.values()):
            if grant.node_id == node_id and not grant.held:
                self.drop(grant)
        self._answered.pop(node_id, None)
        for key in [key for key in self._warned if key[0] == node_id]:
            del self._warned[key]

    @callback
    def clear(self) -> None:
        """Forget every grant."""
        for grant in list(self._grants.values()):
            self.drop(grant)

    def lookup(self, token: str) -> Grant | None:
        """The live grant of `token`, or None."""
        self._purge()
        return self._grants.get(token)

    @callback
    def drop(self, grant: Grant) -> None:
        """Forget one grant and its timer. A response being sent keeps its own bytes."""
        if self._grants.get(grant.token) is grant:
            del self._grants[grant.token]
        if grant.cancel is not None:
            grant.cancel()
            grant.cancel = None

    @callback
    def _expired(self, grant: Grant) -> None:
        """A grant's ten minutes are over; an answer nobody fetched is worth saying."""
        grant.cancel = None
        self.drop(grant)
        if grant.base is not None and grant.served == 0:
            self.hass.async_create_task(
                async_notify(
                    self.hass,
                    grant.node_id,
                    grant.receiver,
                    "relay_not_downloaded",
                    {"version": grant.version, "url": f"{grant.base}{RELAY_PATH}..."},
                ),
                f"{DOMAIN} relay notification {grant.node_id}",
            )

    def _purge(self) -> None:
        now = dt_util.utcnow().timestamp()
        for grant in [grant for grant in self._grants.values() if grant.expires <= now]:
            self.drop(grant)

    def _register_view(self) -> None:
        if self._view_registered:
            return
        http = getattr(self.hass, "http", None)
        if http is None:
            raise RelayError(
                "relay_no_address", detail="Home Assistant's HTTP server is not set up"
            )
        http.register_view(RelayView(self))
        self._view_registered = True

    # ---------------------------------------------------------- relay_request --

    def may_answer(self, node_id: str) -> bool:
        """Whether a download for this receiver may be made now, and count it."""
        now = dt_util.utcnow().timestamp()
        last = self._answered.get(node_id)
        if last is not None and 0 <= now - last < RELAY_REQUEST_INTERVAL:
            return False
        self._answered[node_id] = now
        return True

    # ------------------------------------------------------------------- the log --

    def warn(self, node_id: str, kind: str, message: str, *args: Any) -> None:
        """A warning of one kind about one receiver, once in RELAY_WARNING_WINDOW.

        Anything a broker client or a token holder can repeat - a malformed request, a request
        too soon, a version refused, a fetch from another address - is written as a warning
        the first time and at debug level after that; the next warning says how many were
        held back. The keys are receivers this Home Assistant has entries for, so the memory is
        bounded by them.
        """
        now = dt_util.utcnow().timestamp()
        key = (node_id, kind)
        state = self._warned.get(key)
        if state is not None and 0 <= now - state[0] < RELAY_WARNING_WINDOW:
            state[1] += 1
            _LOGGER.debug(message, *args)
            return
        held_back = int(state[1]) if state is not None else 0
        self._warned[key] = [now, 0]
        if held_back:
            message += " (%d more like this since the last warning were logged at debug level)"
            args = (*args, held_back)
        _LOGGER.warning(message, *args)


def async_get_relay(hass: HomeAssistant) -> Relay:
    """The one relay of this Home Assistant."""
    if (relay := hass.data.get(_DATA_KEY)) is None:
        relay = hass.data[_DATA_KEY] = Relay(hass)
    return relay


class RelayView(HomeAssistantView):
    """`GET /api/enigma2_mqtt/relay/{token}` - the package of one grant, to its receiver only."""

    url = RELAY_PATH + "{token}"
    name = "api:enigma2_mqtt:relay"
    requires_auth = False

    def __init__(self, relay: Relay) -> None:
        """Serve the grants of `relay`."""
        self._relay = relay

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        """The bytes, to the grant's address; the same 404 for everything else.

        The address is `request.remote` - the connection's peer, which Home Assistant rewrites
        from `X-Forwarded-For` only for a proxy it was told to trust - and never a header read
        here.
        """
        grant = self._relay.lookup(token)
        if grant is None:
            _LOGGER.debug("Plugin relay: no live grant for the address asked for")
            return web.Response(status=404)
        peer = peer_address(request.remote)
        if peer != grant.address:
            self._relay.warn(
                grant.node_id,
                "address",
                "Plugin relay: %s asked for the package of plugin %s granted to %s (%s); "
                "refused. A reverse proxy in front of Home Assistant makes every request "
                "come from the proxy - give the receiver Home Assistant's direct LAN address",
                request.remote,
                grant.version,
                grant.node_id,
                grant.address,
            )
            return web.Response(status=404)
        grant.served += 1
        _LOGGER.info(
            "Plugin relay: serving plugin %s (%d bytes) to %s at %s (%s..., fetch %d)",
            grant.version,
            len(grant.data),
            grant.node_id,
            peer,
            grant.token[:6],
            grant.served,
        )
        persistent_notification.async_dismiss(self._relay.hass, notification_id(grant.node_id))
        # Prepared here, so Home Assistant's header middleware never sees this response: the
        # headers it would add are set explicitly, and an empty Server header keeps aiohttp
        # from announcing itself. The version in the file name passed `is_version`.
        response = web.StreamResponse(
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="{release_index.PACKAGE}_{grant.version}_all.ipk"'
                ),
                "Referrer-Policy": "no-referrer",
                "Server": "",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "SAMEORIGIN",
            }
        )
        response.content_type = "application/octet-stream"
        response.content_length = len(grant.data)
        # Sent here rather than returned, so that the grant counts as in use for exactly as
        # long as its bytes are on their way. A grant that expires meanwhile is dropped all
        # the same; this response holds its own reference to the bytes.
        data = grant.data
        grant.fetching += 1
        try:
            await response.prepare(request)
            await response.write(data)
            await response.write_eof()
        finally:
            grant.fetching -= 1
        return response


# ------------------------------------------------------------------ the address --


def _host(url: str) -> str:
    """`scheme://host[:port]` of `url`, without a user part - what a log line may carry."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "an address that cannot be read"
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def _why_not(url: str) -> str:
    """Why the receiver would refuse `url`, in words for the log."""
    try:
        parts = urlsplit(url)
        credentials = parts.username is not None or parts.password is not None
        host = parts.hostname or ""
    except ValueError:
        return "it cannot be read as an address"
    if credentials:
        return "it carries a user name or password, which is never sent to the broker"
    if ":" in host:
        return "it is an IPv6 address; the receiver accepts an IPv4 address or a host name"
    return (
        "the receiver accepts only http or https, an IPv4 address or a host name of letters, "
        "digits, dots and hyphens, and a port"
    )


def _base_ok(base: str) -> bool:
    return relay_url_ok(f"{base.rstrip('/')}{RELAY_PATH}{_EXAMPLE_TOKEN}")


async def async_relay_base(hass: HomeAssistant, receiver: ipaddress.IPv4Address) -> str:
    """The base URL the receiver fetches the relay from, or RelayError.

    Home Assistant's IPv4 address on the receiver's own subnet first - the one address the
    receiver certainly reaches without a router - then Home Assistant's internal URL, never the
    external one. An internal URL the receiver would refuse (an IPv6 address, a host name with
    an underscore, a user name and password) gives way to Home Assistant's own IPv4 address when
    it serves plain HTTP; otherwise nothing is offered, with the sentence that says where to
    set an address the receiver can use.
    """
    api = hass.config.api
    if api is not None:
        for host in await _async_same_subnet(hass, receiver):
            scheme = "https" if api.use_ssl else "http"
            return f"{scheme}://{host}:{api.port}"
    try:
        base = get_url(
            hass,
            allow_internal=True,
            allow_external=False,
            allow_cloud=False,
            allow_ip=True,
            prefer_external=False,
        ).rstrip("/")
    except NoURLAvailableError as err:
        raise RelayError(
            "relay_no_address", detail="no internal URL and no address on the receiver's subnet"
        ) from err
    if _base_ok(base):
        return base
    reason = _why_not(base)
    if api is not None and not api.use_ssl:
        local = receiver_address(api.local_ip)
        if local is not None and not local.is_loopback:
            fallback = f"http://{local}:{api.port}"
            if _base_ok(fallback):
                _LOGGER.info(
                    "Plugin relay: Home Assistant's internal URL %s cannot be given to a "
                    "receiver (%s); offering %s instead",
                    _host(base),
                    reason,
                    fallback,
                )
                return fallback
    raise RelayError(
        "relay_unreachable",
        detail=f"Home Assistant's internal URL {_host(base)} cannot be given to it: {reason}",
        url=_host(base),
    )


async def _async_same_subnet(
    hass: HomeAssistant, receiver: ipaddress.IPv4Address
) -> list[str]:
    try:
        adapters = await network.async_get_adapters(hass)
    except Exception:  # A Home Assistant without the network integration knows no adapter.
        _LOGGER.debug("Plugin relay: Home Assistant's network adapters could not be read")
        return []
    found = []
    for adapter in adapters:
        if not adapter.get("enabled"):
            continue
        for item in adapter.get("ipv4") or ():
            try:
                interface = ipaddress.ip_interface(
                    f"{item['address']}/{item['network_prefix']}"
                )
            except (KeyError, ValueError):
                continue
            if receiver in interface.network and not interface.ip.is_loopback:
                found.append(str(interface.ip))
    return found


# ------------------------------------------------------------- the household --


async def async_notify(
    hass: HomeAssistant, node_id: str, receiver: str, key: str, placeholders: dict[str, str]
) -> None:
    """Say a refusal where the household looks: one persistent notification per receiver.

    The receiver's own screen only knows that Home Assistant did not answer; this says why, in
    the sentence the card would use, and the next refusal or a successful download replaces or
    clears it.
    """
    language = hass.config.language
    exceptions = await async_get_translations(hass, language, "exceptions", {DOMAIN})
    common = await async_get_translations(hass, language, "common", {DOMAIN})
    template = exceptions.get(f"component.{DOMAIN}.exceptions.{key}.message") or key
    title = common.get(f"component.{DOMAIN}.common.relay_notification_title") or (
        "{receiver}: plugin download from Home Assistant"
    )
    try:
        message = template.format(**placeholders)
    except (IndexError, KeyError, ValueError):
        message = template
    try:
        title = title.format(receiver=receiver or node_id)
    except (IndexError, KeyError, ValueError):
        pass
    persistent_notification.async_create(
        hass, message, title=title, notification_id=notification_id(node_id)
    )


# ------------------------------------------------------------- relay_request --


async def async_eligible_release(hass: HomeAssistant, version: str) -> dict[str, Any]:
    """The signed entry of `version` when the rule lets this integration install it.

    Asked of the index Home Assistant already holds, before anything is spent on a request:
    nothing is downloaded and nothing is logged. Raises PackageError otherwise.
    """
    cache = async_release_index_cache(hass)
    await cache.async_load()
    return eligible_release(cache.index, version, integration_version=cache.integration_version)


def _parse_request(payload: Any) -> dict[str, Any] | None:
    # Measured in bytes before anything is decoded or parsed.
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "replace")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > RELAY_REQUEST_MAX_BYTES:
        return None
    try:
        body = json.loads(bytes(payload).decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    request_id, version = body.get("id"), body.get("version")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        return None
    if not release_index.is_version(version):
        return None
    # `serial` - the receiver's own index - is not needed: Home Assistant answers from the
    # index it holds, and the receiver verifies the bytes against its own.
    return {"id": request_id, "version": version}


def _loaded(entry: ConfigEntry) -> bool:
    """Whether the receiver's entry is still set up - asked after every wait."""
    return entry.state is ConfigEntryState.LOADED


async def async_answer_relay_request(
    hass: HomeAssistant, entry: ConfigEntry, box: Any, payload: Any
) -> None:
    """Answer a receiver without internet that asks for a package: `cmd/relay`, or nothing.

    A request is a question, never an instruction: it installs nothing - the receiver does,
    after verifying the bytes against its own index - and a request that is malformed, for a
    version the rule refuses, too soon after a download, or from a receiver with no IPv4 address
    gets no answer (the receiver says on its screen that Home Assistant did not answer). Nothing
    is sent back to the broker but the address.

    The order is what keeps a broker client from starving the receiver: the version is judged
    against the held index first, a live grant of the same bytes answers again at no cost, and
    only a new download spends the receiver's once-a-minute. Runs as a background task of the
    entry - unloading cancels it - and asks after every wait whether the entry is still loaded,
    so no grant and no answer outlive it.
    """
    node_id = box.node_id
    relay = async_get_relay(hass)
    request = _parse_request(payload)
    if request is None:
        relay.warn(
            node_id, "malformed", "Plugin relay: ignored a malformed relay_request from %s", node_id
        )
        return
    version = request["version"]
    try:
        entry_of_version = await async_eligible_release(hass, version)
    except PackageError as err:
        relay.warn(
            node_id,
            "refused",
            "Plugin relay: not answering %s's request for plugin %s (%s)",
            node_id,
            version,
            err.code,
        )
        return
    if not _loaded(entry):
        return
    address = receiver_address(box.ip_address)
    grant = (
        relay.reusable(node_id, version, entry_of_version["sha256"], address)
        if address is not None
        else None
    )
    if grant is not None and grant.base is not None:
        _LOGGER.debug(
            "Plugin relay: %s asked again for plugin %s; answering with the live address "
            "(%s...)",
            node_id,
            version,
            grant.token[:6],
        )
    else:
        if not relay.may_answer(node_id):
            relay.warn(
                node_id,
                "too_soon",
                "Plugin relay: ignored a relay_request from %s less than a minute after the last",
                node_id,
            )
            return
        try:
            grant = await _async_new_grant(hass, entry, box, version, address)
        except PackageError as err:
            relay.warn(
                node_id,
                "refused",
                "Plugin relay: not answering %s's request for plugin %s (%s)",
                node_id,
                version,
                err.code,
            )
            if _loaded(entry):
                await async_notify(
                    hass,
                    node_id,
                    entry.title,
                    _NOT_FETCHED.get(err.code, "update_version_unavailable"),
                    err.placeholders,
                )
            return
        except RelayError as err:
            relay.warn(
                node_id,
                "refused",
                "Plugin relay: not answering %s's request for plugin %s (%s%s)",
                node_id,
                version,
                err.key,
                f": {err.detail}" if err.detail else "",
            )
            if _loaded(entry):
                await async_notify(hass, node_id, entry.title, err.key, err.placeholders)
            return
        if grant is None:
            return
    await box.async_publish_cmd(
        "relay",
        json.dumps(
            {
                "id": request["id"],
                "version": version,
                "url": f"{grant.base}{RELAY_PATH}{grant.token}",
                "expires": int(grant.expires),
            },
            separators=(",", ":"),
        ),
    )


async def _async_new_grant(
    hass: HomeAssistant,
    entry: ConfigEntry,
    box: Any,
    version: str,
    address: ipaddress.IPv4Address | None,
) -> Grant | None:
    """Choose the address, download and verify, and grant - or None once the entry is gone.

    The address comes first: a Home Assistant with none the receiver can use is not worth a
    download.
    """
    if address is None:
        raise RelayError("relay_no_ipv4")
    base = await async_relay_base(hass, address)
    if not relay_url_ok(f"{base}{RELAY_PATH}{_EXAMPLE_TOKEN}"):
        raise RelayError("relay_unreachable", detail=_why_not(base), url=_host(base))
    if not _loaded(entry):
        return None
    package = await async_fetch_release(hass, version)
    if not _loaded(entry):
        return None
    grant = async_get_relay(hass).grant(box.node_id, str(address), package, receiver=entry.title)
    grant.base = base
    _LOGGER.info(
        "Plugin relay: plugin %s for %s at %s is served from %s%s%s... until %s",
        package.version,
        box.node_id,
        grant.address,
        base,
        RELAY_PATH,
        grant.token[:6],
        dt_util.utc_from_timestamp(grant.expires).isoformat(),
    )
    return grant

"""The relay: a verified plugin package served to one receiver that has no internet of its own.

A receiver without internet cannot fetch a release from the plugin's origin, and it must not
have to (the design's OD 2). Home Assistant fetches and verifies the package (`release_package`),
keeps the bytes in memory and serves them on a short-lived, unguessable address on the local
network; the receiver downloads them from there and verifies them itself against its own signed
index before opkg sees a byte. The relay is a courier, not an authority: the worst an attacker on
the path achieves is a refusal.

**Who may fetch.** Nobody authenticates to this view - the receiver has no Home Assistant
credentials, and must not be given any - so the grant is bound instead:

- to one **token**, `secrets.token_urlsafe(32)` (43 url-safe characters, 256 bits), in the path;
- to the **receiver's IPv4 address** as it reports it on `info.ip`, compared as an address with
  an IPv4-mapped IPv6 address (how a dual-stack listener reports an IPv4 peer) unwrapped first. A
  request from any other address gets the same `404` as an unknown token, and a warning in the log.
  A receiver with no address, or only an IPv6 one, gets no grant at all - never an unbound one;
- to **ten minutes**. Until then the address may be fetched again and again: the URL crosses the
  broker, so a single use would only let another broker client spend it first, and the bytes are a
  public signed package anyway.

**What it serves.** Exactly the verified bytes of that grant, from memory: no file, no directory,
no other path. `GET` only (a `HEAD` is not routed), the whole package whatever `Range` asks for.

**How many.** One grant per (receiver, version) - asking again for the same version gets the
same address while enough of its ten minutes are left - and at most three per receiver. A fourth
version evicts the receiver's oldest grant, unless a transaction Home Assistant knows of is using
it; then the new one is refused, so that a forged request for another version cannot cut an
in-flight download. A receiver's grants go when its entry is unloaded.

**Which address.** Home Assistant's own IPv4 address on the receiver's subnet, among the adapters
Home Assistant knows; else its internal URL (`get_url` with the external and cloud URLs
disallowed); else no relay. Never the external URL: the package is for one device on the local
network. The receiver-facing half - `relay_request` from a receiver without internet, answered
with `cmd/relay` - is `async_answer_relay_request`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import logging
import re
import secrets
from typing import Any

from aiohttp import web
from homeassistant.components import network
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import NoURLAvailableError, get_url
import homeassistant.util.dt as dt_util

from . import release_index
from .const import DOMAIN
from .release_package import PackageError, PackageSource, async_fetch_release

_LOGGER = logging.getLogger(__name__)

RELAY_PATH = "/api/enigma2_mqtt/relay/"
RELAY_LIFETIME = 600.0
# A grant asked for again is reused only while this much of it is left: the receiver waits 120 s
# for an answer and then downloads, and an address that expires in the middle is no answer.
RELAY_REUSE_MARGIN = 180.0
RELAY_GRANTS_PER_RECEIVER = 3
# `relay_request` is answered at most once a minute per receiver.
RELAY_REQUEST_INTERVAL = 60.0

_DATA_KEY = f"{DOMAIN}_relay"
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)


class RelayError(HomeAssistantError):
    """A relay that cannot be offered, with the sentence that says why."""

    def __init__(self, key: str, **placeholders: str) -> None:
        """Keep the translation key; the log gets the key too."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders=placeholders or None,
        )
        self.key = key


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
    # Set by a transaction Home Assistant started and follows (the MQTT path): not evicted.
    in_use: bool = False
    served: int = 0
    cancel: CALLBACK_TYPE | None = field(default=None, repr=False)


class Relay:
    """Every grant this Home Assistant holds, and the view that serves them."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Hold nothing yet; the view is registered with the first grant."""
        self.hass = hass
        self._grants: dict[str, Grant] = {}
        self._answered: dict[str, float] = {}
        self._view_registered = False

    # ------------------------------------------------------------------- grants --

    def grants(self, node_id: str) -> list[Grant]:
        """The live grants of one receiver, oldest first."""
        self._purge()
        return sorted(
            (grant for grant in self._grants.values() if grant.node_id == node_id),
            key=lambda grant: grant.issued,
        )

    @callback
    def grant(
        self, node_id: str, receiver_ip: Any, package: PackageSource
    ) -> Grant:
        """A grant of `package` to the receiver at `receiver_ip`, new or reused.

        Raises RelayError when the receiver has no IPv4 address, or when it already holds three
        grants that are all in use.
        """
        address = peer_address(receiver_ip)
        if address is None:
            raise RelayError("relay_no_ipv4")
        now = dt_util.utcnow().timestamp()
        held = self.grants(node_id)
        for grant in held:
            if grant.version != package.version:
                continue
            if (
                grant.sha256 == package.sha256
                and grant.address == address
                and grant.expires - now >= RELAY_REUSE_MARGIN
            ):
                return grant
            # Too close to its end, or for other bytes or another address: replaced, never
            # a second grant of the same version.
            self.drop(grant)
            held.remove(grant)
        if len(held) >= RELAY_GRANTS_PER_RECEIVER:
            evictable = [grant for grant in held if not grant.in_use]
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
        )
        grant.cancel = async_call_later(
            self.hass, RELAY_LIFETIME, callback(lambda _now: self.drop(grant))
        )
        self._grants[grant.token] = grant
        return grant

    @callback
    def mark_in_use(self, token: str, in_use: bool) -> None:
        """Say whether a transaction Home Assistant follows is downloading this grant."""
        if (grant := self._grants.get(token)) is not None:
            grant.in_use = in_use

    @callback
    def drop_node(self, node_id: str) -> None:
        """Forget every grant of one receiver - its entry is going."""
        for grant in list(self._grants.values()):
            if grant.node_id == node_id:
                self.drop(grant)
        self._answered.pop(node_id, None)

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
        """Forget one grant and its timer."""
        if self._grants.get(grant.token) is grant:
            del self._grants[grant.token]
        if grant.cancel is not None:
            grant.cancel()
            grant.cancel = None

    def _purge(self) -> None:
        now = dt_util.utcnow().timestamp()
        for grant in [grant for grant in self._grants.values() if grant.expires <= now]:
            self.drop(grant)

    def _register_view(self) -> None:
        if self._view_registered:
            return
        http = getattr(self.hass, "http", None)
        if http is None:
            raise RelayError("relay_no_address")
        http.register_view(RelayView(self))
        self._view_registered = True

    # ---------------------------------------------------------- relay_request --

    def may_answer(self, node_id: str) -> bool:
        """Whether a `relay_request` of this receiver may be answered now, and count it."""
        now = dt_util.utcnow().timestamp()
        last = self._answered.get(node_id)
        if last is not None and 0 <= now - last < RELAY_REQUEST_INTERVAL:
            return False
        self._answered[node_id] = now
        return True


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

    async def get(self, request: web.Request, token: str) -> web.Response:
        """The bytes, to the grant's address; the same 404 for everything else."""
        grant = self._relay.lookup(token)
        if grant is None:
            _LOGGER.debug("Plugin relay: no live grant for the address asked for")
            return web.Response(status=404)
        peer = peer_address(request.remote)
        if peer != grant.address:
            _LOGGER.warning(
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
            "Plugin relay: served plugin %s (%d bytes) to %s at %s (%s..., fetch %d)",
            grant.version,
            len(grant.data),
            grant.node_id,
            peer,
            grant.token[:6],
            grant.served,
        )
        return web.Response(
            body=grant.data,
            content_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


# ------------------------------------------------------------------ the address --


async def async_relay_base(hass: HomeAssistant, receiver: ipaddress.IPv4Address) -> str:
    """The base URL the receiver fetches the relay from, or RelayError(relay_no_address).

    Home Assistant's IPv4 address on the receiver's own subnet first - the one address the
    receiver certainly reaches without a router - then Home Assistant's internal URL, never the
    external one.
    """
    api = hass.config.api
    if api is not None:
        for host in await _async_same_subnet(hass, receiver):
            scheme = "https" if api.use_ssl else "http"
            return f"{scheme}://{host}:{api.port}"
    try:
        return get_url(
            hass,
            allow_internal=True,
            allow_external=False,
            allow_cloud=False,
            allow_ip=True,
            prefer_external=False,
        )
    except NoURLAvailableError as err:
        raise RelayError("relay_no_address") from err


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


async def async_offer(
    hass: HomeAssistant, node_id: str, receiver_ip: Any, package: PackageSource
) -> tuple[str, Grant]:
    """A grant of `package` to one receiver, and the URL it fetches it from."""
    relay = async_get_relay(hass)
    grant = relay.grant(node_id, receiver_ip, package)
    try:
        base = await async_relay_base(hass, grant.address)
    except RelayError:
        # A grant nobody can reach is not kept.
        relay.drop(grant)
        raise
    url = f"{base.rstrip('/')}{RELAY_PATH}{grant.token}"
    _LOGGER.info(
        "Plugin relay: plugin %s for %s at %s is served from %s%s%s... until %s",
        package.version,
        node_id,
        grant.address,
        base.rstrip("/"),
        RELAY_PATH,
        grant.token[:6],
        dt_util.utc_from_timestamp(grant.expires).isoformat(),
    )
    return url, grant


# ------------------------------------------------------------- relay_request --


def _parse_request(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", "replace")
    if not isinstance(payload, str) or len(payload) > 1024:
        return None
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    request_id, version = body.get("id"), body.get("version")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        return None
    if not release_index.is_version(version):
        return None
    return {"id": request_id, "version": version, "serial": body.get("serial")}


async def async_answer_relay_request(hass: HomeAssistant, box: Any, payload: Any) -> None:
    """Answer a receiver without internet that asks for a package: `cmd/relay`, or nothing.

    A request is a question, never an instruction: it installs nothing - the receiver does,
    after verifying the bytes against its own index - and a request that is malformed, too
    soon, for a version the rule refuses, or from a receiver with no IPv4 address gets no answer
    (the receiver says on its screen that Home Assistant did not answer). Nothing is sent back to
    the broker but the address.
    """
    request = _parse_request(payload)
    if request is None:
        _LOGGER.warning("Plugin relay: ignored a malformed relay_request from %s", box.node_id)
        return
    relay = async_get_relay(hass)
    if not relay.may_answer(box.node_id):
        _LOGGER.warning(
            "Plugin relay: ignored a relay_request from %s less than a minute after the last",
            box.node_id,
        )
        return
    version = request["version"]
    try:
        # The whole rule, not only the floor and withdrawals the receiver asks about: this
        # integration offers nothing it would not install itself.
        package = await async_fetch_release(hass, version)
        url, grant = await async_offer(hass, box.node_id, box.ip_address, package)
    except PackageError as err:
        _LOGGER.warning(
            "Plugin relay: not answering %s's request for plugin %s (%s)",
            box.node_id,
            version,
            err.code,
        )
        return
    except RelayError as err:
        _LOGGER.warning(
            "Plugin relay: not answering %s's request for plugin %s (%s)",
            box.node_id,
            version,
            err.key,
        )
        return
    await box.async_publish_cmd(
        "relay",
        json.dumps(
            {
                "id": request["id"],
                "version": version,
                "url": url,
                "expires": int(grant.expires),
            },
            separators=(",", ":"),
        ),
    )

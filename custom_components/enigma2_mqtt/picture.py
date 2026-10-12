"""The picture on the media player: the screenshot, the channel's picon, and which of them.

Two things live here, and neither knows about entities.

**Which picture is shown** (`PlayerPicture`). The plugin publishes a screenshot of the
television, and the receiver has a picon - a channel logo - for most channels. The option
`player_picture` chooses between them:

- `screenshot` shows the screenshot, as this integration always has, with one addition: right
  after a channel change the last screenshot is still of the previous channel, so the picon
  of the new channel is shown until a screenshot taken after the change arrives. With
  screenshots switched off on the receiver none arrives, and the picon stays.
- `picon` shows the picon of the current channel, and the screenshot only for a channel
  that has none.

"A screenshot taken after the change" is decided from the bytes, not from a clock: the
`screen` topic is retained, so a reconnect to the broker replays the picture it already
delivered, with a new arrival time. Only a screenshot that differs from the one held at the
moment of the change counts.

A receiver in standby keeps the picture it had. Nothing is swapped because the service
payload emptied or the power state changed.

**Where a picon comes from** (`PiconResolver`). Picons are files on the receiver, named
after the service reference - or after a reference with another namespace or service type,
or after the channel's name, or whatever the image's own resolver decides. OpenWebif knows
all of those rules and this module copies none of them: it asks.

- `GET /api/getservices?sRef=<bouquet>&picon=1` lists a bouquet's services, each with the
  link of its picon. One call covers a bouquet, for the media browser's thumbnails and for
  the player.
- `GET /api/getpicon?sRef=<service>&json=true` answers for one service: the current channel
  when no bouquet on offer lists it.
- The name built from the service reference is the last resort, for an OpenWebif that
  answers neither.

Home Assistant then fetches the picture itself and serves it as the media image, so a
browser never needs to reach the receiver for it.

Everything the receiver sends is treated as hostile: a link is used only when it is a plain
path under `/picon/`, requests go to the receiver's own address and follow no redirect,
every body has a size limit, and a picture is accepted by its first bytes - PNG or JPEG -
never by the content type it claims. Every failure ends in the other picture, silently:
one line in the log per cause until it recovers.

The receiver is asked as little as possible. Nothing is polled: a picon is looked up when
the player needs one and when somebody opens a bouquet in the media browser. Answers are
kept - a bouquet's links until its channel list changes, pictures and "no picon" per
service - one request runs at a time, and after a connection failure or a refusal (an
OpenWebif behind a login) nothing is asked again for minutes.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote

import aiohttp
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .box import service_identity
from .const import (
    CONF_PLAYER_PICTURE,
    DEFAULT_PLAYER_PICTURE,
    DOMAIN,
    PLAYER_PICTURE_PICON,
    PLAYER_PICTURES,
)

if TYPE_CHECKING:
    from .box import Enigma2Box, Enigma2MqttConfigEntry

_LOGGER = logging.getLogger(__name__)

KIND_SCREENSHOT: Final = "screenshot"
KIND_PICON: Final = "picon"

# Where OpenWebif serves picons, and what it answers for a service that has none.
PICON_PREFIX: Final = "/picon/"
DEFAULT_PICON: Final = "/images/default_picon.png"

# One request to the receiver may take this long, connection and body together.
REQUEST_TIMEOUT: Final = 5.0
# A bouquet of a thousand services answers in about 200 kB of JSON.
MAX_JSON_BYTES: Final = 2 * 1024 * 1024
# A picon is a small PNG, usually well under 100 kB.
MAX_IMAGE_BYTES: Final = 1024 * 1024
# Services read from one answer, and the longest link taken from one.
MAX_SERVICES: Final = 10000
MAX_LINK_LENGTH: Final = 512

# How many services' pictures are kept, and how many bytes of them altogether.
MAX_PICTURES: Final = 64
MAX_PICTURE_BYTES: Final = 8 * 1024 * 1024
# How many bouquets' links are kept.
MAX_BOUQUETS: Final = 64

# How long an answer is believed. A picon added to the receiver later shows after the
# shorter one; a replaced picon, and a bouquet's links, after the longer one, or when the
# bouquet's channel list changes, or when the entry is reloaded.
ANSWER_TTL: Final = 24 * 60 * 60.0
NEGATIVE_TTL: Final = 60 * 60.0

# What stops the lookup for a while, and for how long.
PROBLEM_UNREACHABLE: Final = "unreachable"
PROBLEM_REFUSED: Final = "refused"
BACKOFF: Final = {PROBLEM_UNREACHABLE: 5 * 60.0, PROBLEM_REFUSED: 15 * 60.0}

# What the diagnostics say about the lookup besides the two problems.
STATUS_IDLE: Final = "idle"
STATUS_OK: Final = "ok"
STATUS_NO_ADDRESS: Final = "no_address"

# In a bouquet's links: OpenWebif's answer that a service has no picon.
NO_PICON: Final = ""

_PNG: Final = b"\x89PNG\r\n\x1a\n"
_JPEG: Final = b"\xff\xd8\xff"


class PendingPicon:
    """The lookup's answer for a picon that is being fetched: not known yet."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "PENDING"


PENDING: Final = PendingPicon()


@dataclass(frozen=True, slots=True)
class Picture:
    """One picture the player can show, with the hash the frontend caches it under."""

    content: bytes
    content_type: str
    kind: str
    digest: str


def screenshot_picture(screen: bytes) -> Picture:
    """Return a screenshot as a picture. The plugin publishes it as a JPEG."""
    return Picture(
        screen, "image/jpeg", KIND_SCREENSHOT, hashlib.sha256(screen).hexdigest()[:16]
    )


def picon_picture(content: bytes) -> Picture | None:
    """Return fetched bytes as a picon, or None when they are not a PNG or a JPEG.

    The first bytes decide, not the content type the receiver sent: Home Assistant serves
    this from its own origin, and an SVG or an HTML page served there is a script.
    """
    if content.startswith(_PNG):
        content_type = "image/png"
    elif content.startswith(_JPEG):
        content_type = "image/jpeg"
    else:
        return None
    # The kind is part of the hash, so a picon and a screenshot never share one.
    digest = hashlib.sha256(b"picon:" + content).hexdigest()[:16]
    return Picture(content, content_type, KIND_PICON, digest)


def player_picture_mode(options: Any) -> str:
    """Return the chosen picture mode, and the default for anything else stored there."""
    mode = options.get(CONF_PLAYER_PICTURE, DEFAULT_PLAYER_PICTURE)
    return mode if mode in PLAYER_PICTURES else DEFAULT_PLAYER_PICTURE


def valid_picon_link(link: Any) -> str | None:
    """Return a picon link from OpenWebif if it is safe to request, else None.

    Safe means a plain absolute path under `/picon/`: no scheme and no host - it starts
    with the prefix - no query, no fragment, no backslash, no control character, and no
    empty, `.` or `..` segment, written plainly or percent-encoded.
    """
    if not isinstance(link, str) or not link.startswith(PICON_PREFIX):
        return None
    if len(link) > MAX_LINK_LENGTH:
        return None
    for text in (link, unquote(link)):
        if any(ord(char) < 0x20 or ord(char) == 0x7F or char in "\\?#" for char in text):
            return None
        if any(segment in ("", ".", "..") for segment in text.split("/")[2:]):
            return None
    return link


def legacy_picon_link(sref: str) -> str | None:
    """Return the link built from the service reference alone, if that makes one.

    The first name OpenWebif itself tries: every colon an underscore, the trailing one
    dropped. Used only when OpenWebif gave no answer of its own.
    """
    name = sref.replace(":", "_").rstrip("_")
    return valid_picon_link(f"{PICON_PREFIX}{name}.png") if name else None


def getpicon_reference(sref: str) -> str:
    """Return a service reference the way `getpicon` has to be given it.

    OpenWebif cuts the reference at its last colon before it builds the file name, which
    is right for a reference that ends in one and removes the last field from one that
    does not. Both spellings exist, so the colon is added where it is missing.
    """
    sref = sref.strip()
    return f"{sref}:" if sref.count(":") == 9 else sref


def _loads(body: bytes) -> Any:
    """Return a JSON body parsed, or None for anything that is not JSON."""
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return None


def parse_service_picons(body: bytes) -> dict[str, str] | None:
    """Return a `getservices?picon=1` answer as service identity -> picon link.

    The value is `NO_PICON` for a service OpenWebif gave its default picon. A service is
    left out - not known - when its entry has no usable link: an OpenWebif that ignores
    `picon=1`, a link that points outside `/picon/`, and a service listed twice with two
    different answers. None means the body was not a service list at all.

    Both OpenWebif families answer `{"services": [{"servicereference", "servicename",
    "picon", ...}]}`. The reference carries the channel's name after the fields that
    identify it, or not, depending on the family and the request; the identity ignores it.
    """
    payload = _loads(body)
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, list):
        return None
    links: dict[str, str] = {}
    ambiguous: set[str] = set()
    for service in services[:MAX_SERVICES]:
        if not isinstance(service, dict):
            continue
        reference = service.get("servicereference")
        identity = service_identity(reference) if isinstance(reference, str) else ""
        if not identity or identity in ambiguous:
            continue
        picon = service.get("picon")
        if picon == DEFAULT_PICON:
            link = NO_PICON
        elif (link := valid_picon_link(picon)) is None:
            continue
        if links.setdefault(identity, link) != link:
            del links[identity]
            ambiguous.add(identity)
    return links


def parse_getpicon(body: bytes) -> str | None:
    """Return a `getpicon?json=true` answer: a link, `NO_PICON`, or None for no answer.

    Both families answer `{"result": true, "path": ..., "link": "/picon/..."}` for a
    service with a picon and `{"result": false}` for one without.
    """
    payload = _loads(body)
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if result is False:
        return NO_PICON
    if result is not True:
        return None
    link = payload.get("link")
    return NO_PICON if link == DEFAULT_PICON else valid_picon_link(link)


class PlayerPicture:
    """Which picture the media player shows, decided again on every payload."""

    def __init__(self, mode: str) -> None:
        """Start with no picture and no channel seen."""
        self.mode = mode
        self.shown: Picture | None = None
        # The channel last seen, as an identity. None until the first one: the channel a
        # session starts on is not a channel change.
        self._service: str | None = None
        # A channel change was seen and no screenshot has arrived since; and the
        # screenshot held at that moment, which a retained replay delivers again.
        self._awaiting = False
        self._screen_then: bytes | None = None
        self._shot: Picture | None = None

    def update(
        self,
        *,
        sref: Any,
        screen: bytes | None,
        is_on: bool,
        picon: Callable[[str], Picture | PendingPicon | None],
    ) -> Picture | None:
        """Take the current channel and screenshot, and return the picture to show.

        `picon` answers for a service reference with its picon, with None when there is
        none to be had, or with `PENDING` while it is being fetched - in which case it
        reports when it knows, and the picture already shown stays until then.
        """
        # Anything but text is not a channel: the payload is the receiver's, not ours.
        identity = service_identity(sref) if isinstance(sref, str) else ""
        if identity:
            if self._service not in (None, identity):
                self._awaiting = True
                self._screen_then = screen
            self._service = identity
        if self._awaiting and screen is not None and screen != self._screen_then:
            self._awaiting = False
            self._screen_then = None

        if not is_on and self.shown is not None:
            # Standby and an unreachable receiver hold the last picture.
            return self.shown

        if is_on and identity and (self.mode == PLAYER_PICTURE_PICON or self._awaiting):
            found = picon(sref)
            if isinstance(found, Picture):
                self.shown = found
                return found
            if found is PENDING and self.shown is not None:
                return self.shown
        self.shown = self._screenshot(screen)
        return self.shown

    def _screenshot(self, screen: bytes | None) -> Picture | None:
        """Return the screenshot as a picture, hashed once per screenshot."""
        if screen is None:
            return None
        if self._shot is None or self._shot.content is not screen:
            self._shot = screenshot_picture(screen)
        return self._shot


class _Unavailable(Exception):
    """The receiver's web interface cannot be asked now: unreachable, or it refused."""

    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


@dataclass(slots=True)
class _BouquetLinks:
    """One bouquet's picon links, and the channel list they were asked for."""

    signature: int
    expires: float
    links: dict[str, str] | None


class PiconResolver:
    """Finds and fetches the picons of one receiver through its OpenWebif."""

    def __init__(
        self, hass: HomeAssistant, entry: Enigma2MqttConfigEntry, box: Enigma2Box
    ) -> None:
        """Set up empty caches. Nothing is requested until a picon is asked for."""
        self.hass = hass
        self.entry = entry
        self.box = box
        self._clock: Callable[[], float] = time.monotonic
        self._listeners: list[Callable[[], None]] = []
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        # The service reference the player is waiting for; the newest one wins.
        self._wanted: str | None = None
        # One request to the receiver at a time, whoever asks.
        self._lock = asyncio.Lock()
        # Per service reference: when the answer expires, and the picon or None.
        self._pictures: OrderedDict[str, tuple[float, Picture | None]] = OrderedDict()
        self._bouquets: dict[str, _BouquetLinks] = {}
        self._problem: str | None = None
        self._retry_at = 0.0
        self._logged: str | None = None
        self._answered = False

    # ------------------------------------------------------------------ life cycle

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Register a callback for "a picon that was pending is known now"."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    @callback
    def async_stop(self) -> None:
        """Stop for good: the entry is unloading. Nothing is asked or reported after it."""
        self._stopped = True
        self._wanted = None
        self._listeners.clear()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # ---------------------------------------------------------------------- answers

    @callback
    def async_picon(self, sref: str) -> Picture | PendingPicon | None:
        """Return a service's picon, None when there is none, or `PENDING`.

        Never waits and never raises: this is called while an MQTT message is being
        handled. `PENDING` means a background task of the config entry is asking the
        receiver, and the listeners are called when it has finished - whatever the result.
        """
        key = sref.strip()
        if (cached := self._pictures.get(key)) is not None:
            if cached[0] > self._clock():
                self._pictures.move_to_end(key)
                return cached[1]
            del self._pictures[key]
        if not self._may_ask():
            return None
        self._wanted = key
        if self._task is None or self._task.done():
            # Not started eagerly: the caller is in the middle of reading its state, and
            # a lookup answered without a wait would report back into that read.
            self._task = self.entry.async_create_background_task(
                self.hass,
                self._async_run(),
                f"{DOMAIN} picon {self.entry.entry_id}",
                eager_start=False,
            )
        return PENDING

    async def async_bouquet_thumbnails(self, bouquet: dict[str, Any]) -> dict[str, str | None]:
        """Return a bouquet's picons for the media browser: identity -> URL, or None.

        The URL points at the receiver, as the media browser's thumbnails always have. A
        service OpenWebif says has no picon maps to None, and one it did not answer for is
        left out, so the caller can fall back on the name built from the reference.
        """
        if self._stopped or (base := self._base_url()) is None:
            return {}
        if (known := self._known_bouquet_links(bouquet)) is not None:
            links = known.links
        elif not self._may_ask():
            return {}
        else:
            try:
                async with self._lock:
                    links = await self._async_bouquet_links(base, bouquet)
            except _Unavailable as error:
                self._async_unavailable(error.problem)
                return {}
        return {
            identity: str(base.with_path(link)) if link else None
            for identity, link in (links or {}).items()
        }

    @callback
    def async_status(self) -> dict[str, Any]:
        """Return the state of the lookup for diagnostics: counts and causes, no address."""
        now = self._clock()
        if self._problem is not None:
            status = self._problem
        elif self._base_url() is None:
            status = STATUS_NO_ADDRESS
        else:
            status = STATUS_OK if self._answered else STATUS_IDLE
        return {
            "status": status,
            "retry_in_seconds": max(0, round(self._retry_at - now)) if self._problem else None,
            "bouquets_cached": len(self._bouquets),
            "picons_cached": sum(1 for _, picture in self._pictures.values() if picture),
            "services_without_picon": sum(
                1 for _, picture in self._pictures.values() if picture is None
            ),
        }

    # -------------------------------------------------------------------- internals

    def _base_url(self) -> URL | None:
        """Return the receiver's web address, or None when it is not known."""
        url = self.box.web_url
        return URL(url) if url else None

    def _may_ask(self) -> bool:
        """Return whether the receiver may be asked anything right now."""
        return (
            not self._stopped
            and self.box.available
            and self._clock() >= self._retry_at
            and self._base_url() is not None
        )

    async def _async_run(self) -> None:
        """Resolve what the player is waiting for, newest first, then report."""
        while not self._stopped and (sref := self._wanted) is not None:
            self._wanted = None
            try:
                async with self._lock:
                    picture = await self._async_resolve(sref)
            except _Unavailable as error:
                if self._stopped:
                    return
                self._async_unavailable(error.problem)
            else:
                if self._stopped:
                    return
                self._remember(sref, picture)
            for listener in list(self._listeners):
                listener()

    async def _async_resolve(self, sref: str) -> Picture | None:
        """Ask the receiver for one service's picon. None: it has none we can use."""
        if (base := self._base_url()) is None:
            return None
        link = await self._async_link(base, sref)
        if not link:
            return None
        body = await self._async_get(base.with_path(link), MAX_IMAGE_BYTES)
        return picon_picture(body) if body is not None else None

    async def _async_link(self, base: URL, sref: str) -> str | None:
        """Return the link of a service's picon: the bouquet's answer, then its own."""
        identity = service_identity(sref)
        for bouquet in self.box.bouquets:
            if not any(
                isinstance(channel, dict) and service_identity(channel.get("sref")) == identity
                for channel in bouquet.get("channels") or ()
            ):
                continue
            links = await self._async_bouquet_links(base, bouquet)
            if links is not None and identity in links:
                return links[identity]
            # One bouquet is asked per service; a second one would say the same.
            break
        body = await self._async_get(
            base.with_path("/api/getpicon").with_query(
                {"sRef": getpicon_reference(sref), "json": "true"}
            ),
            MAX_JSON_BYTES,
        )
        answer = parse_getpicon(body) if body is not None else None
        return legacy_picon_link(sref) if answer is None else answer

    @staticmethod
    def _signature(bouquet: dict[str, Any]) -> int:
        """Return a value that changes when a bouquet's channel list does."""
        return hash(
            tuple(
                service_identity(channel.get("sref"))
                for channel in bouquet.get("channels") or ()
                if isinstance(channel, dict)
            )
        )

    def _known_bouquet_links(self, bouquet: dict[str, Any]) -> _BouquetLinks | None:
        """Return what is held for a bouquet, if it is for this channel list and fresh."""
        cached = self._bouquets.get(bouquet.get("sref") or "")
        if (
            cached is not None
            and cached.signature == self._signature(bouquet)
            and cached.expires > self._clock()
        ):
            return cached
        return None

    async def _async_bouquet_links(
        self, base: URL, bouquet: dict[str, Any]
    ) -> dict[str, str] | None:
        """Return one bouquet's picon links, asking only when its channel list changed."""
        sref = bouquet.get("sref")
        if not isinstance(sref, str) or not sref:
            return None
        if (known := self._known_bouquet_links(bouquet)) is not None:
            return known.links
        signature = self._signature(bouquet)
        now = self._clock()
        body = await self._async_get(
            base.with_path("/api/getservices").with_query({"sRef": sref, "picon": "1"}),
            MAX_JSON_BYTES,
        )
        links = parse_service_picons(body) if body is not None else None
        if self._stopped:
            return links
        # Only the bouquets on offer are ever asked for, so only they are kept.
        offered = {offer.get("sref") for offer in self.box.bouquets}
        for known in [known for known in self._bouquets if known not in offered]:
            del self._bouquets[known]
        if len(self._bouquets) >= MAX_BOUQUETS:
            self._bouquets.clear()
        self._bouquets[sref] = _BouquetLinks(
            signature, now + (ANSWER_TTL if links is not None else NEGATIVE_TTL), links
        )
        return links

    async def _async_get(self, url: URL, limit: int) -> bytes | None:
        """Return the body of one GET, or None for an answer that cannot be used.

        Not usable: any status but 200 - a redirect included, which is not followed - and
        a body over the limit. A connection failure, a timeout and a 401 or 403 are not
        answers at all and raise, which stops the lookup for a while.
        """
        session = async_get_clientsession(self.hass)
        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT),
                session.get(url, allow_redirects=False) as response,
            ):
                if response.status in (401, 403):
                    raise _Unavailable(PROBLEM_REFUSED)
                self._async_reachable()
                if response.status != 200:
                    return None
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > limit:
                        return None
                return bytes(body)
        except (TimeoutError, aiohttp.ClientError) as error:
            raise _Unavailable(PROBLEM_UNREACHABLE) from error

    @callback
    def _async_reachable(self) -> None:
        """Note that the web interface answered, which ends any problem."""
        self._answered = True
        if self._problem is not None or self._logged is not None:
            _LOGGER.debug("The web interface of %s answers again", self.box.node_id)
        self._problem = None
        self._logged = None
        self._retry_at = 0.0

    @callback
    def _async_unavailable(self, problem: str) -> None:
        """Stop asking for a while, and say why once."""
        if self._stopped:
            return
        self._problem = problem
        self._retry_at = self._clock() + BACKOFF[problem]
        if self._logged == problem:
            return
        self._logged = problem
        if problem == PROBLEM_REFUSED:
            _LOGGER.warning(
                "The web interface (OpenWebif) of %s asks for a login, so channel picons "
                "cannot be fetched and the screenshot is shown instead. This is logged "
                "once; it is tried again every %d minutes",
                self.box.node_id,
                BACKOFF[problem] // 60,
            )
        else:
            _LOGGER.warning(
                "The web interface (OpenWebif) of %s could not be reached, so channel "
                "picons cannot be fetched and the screenshot is shown instead. This is "
                "logged once; it is tried again every %d minutes",
                self.box.node_id,
                BACKOFF[problem] // 60,
            )

    def _remember(self, sref: str, picture: Picture | None) -> None:
        """Keep one service's answer, within the bounds on count and bytes."""
        ttl = ANSWER_TTL if picture is not None else NEGATIVE_TTL
        self._pictures[sref] = (self._clock() + ttl, picture)
        self._pictures.move_to_end(sref)

        def held() -> int:
            return sum(len(kept.content) for _, kept in self._pictures.values() if kept)

        while len(self._pictures) > MAX_PICTURES or (
            len(self._pictures) > 1 and held() > MAX_PICTURE_BYTES
        ):
            self._pictures.popitem(last=False)

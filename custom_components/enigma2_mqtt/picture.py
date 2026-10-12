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
moment of the change counts. Which channel a screenshot shows is not known: one taken of a
channel that was only passed through counts as new for the channel that followed it.

A receiver in standby keeps the picture it had, and a receiver that wakes keeps it until
it says which channel it is on. Nothing is swapped because the service payload emptied or
the power state changed.

**Where a picon comes from** (`PiconResolver`). Picons are files on the receiver, named
after the service reference - or after a reference with another namespace or service type,
or after the channel's name, or whatever the image's own resolver decides. OpenWebif knows
all of those rules and this module copies none of them: it asks.

- For the player, `GET /api/getpicon?sRef=<service>&json=true` answers for the one
  service that is on. A bouquet's answer is used instead when one is already held.
- For the media browser, `GET /api/getservices?sRef=<bouquet>&picon=1` lists a bouquet's
  services, each with the link of its picon. It makes the receiver look for a picon for
  every service of the bouquet, so it is asked only when somebody opens that bouquet in
  the browser, in the background, and never for the player.
- The name built from the service reference is the last resort, for an OpenWebif that
  does not answer `getpicon`.

Home Assistant then fetches the picture itself and serves it as the media image, so a
browser never needs to reach the receiver for it.

**Which address is asked** is `Enigma2Box.picon_lookup_url`: the host typed when the
receiver was added, and the address the receiver reports only when none was typed and it
is a plain IP address of a kind a receiver can have.

Everything the receiver sends is treated as hostile: a link is used only when it is a plain
path under `/picon/`, requests follow no redirect, every body has a size limit, and a
picture is accepted by its first bytes - PNG or JPEG - never by the content type it claims.
Every failure ends in the other picture, silently: one line in the log per cause until it
recovers.

The receiver is asked as little as possible. Nothing is polled. One task of the config
entry does all the asking, one request at a time; a channel change abandons whatever it
was doing for the channel that is on now, and unloading the entry cancels it. Answers are
kept - a bouquet's links until its channel list changes, pictures and "no picon" per
service - and after a connection failure, a refusal (an OpenWebif behind a login) or
answers that are not OpenWebif's, nothing is asked again for minutes.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote

import aiohttp
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
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
# How many bouquets' answers are kept, and how many links in them altogether.
MAX_BOUQUETS: Final = 64
MAX_BOUQUET_LINKS: Final = 20000

# How long an answer is believed. A picon added to the receiver later shows after the
# shorter one; a replaced picon, and a bouquet's links, after the longer one, or when the
# bouquet's channel list changes, or when the entry is reloaded.
ANSWER_TTL: Final = 24 * 60 * 60.0
NEGATIVE_TTL: Final = 60 * 60.0

# What stops the lookup for a while, and for how long. `unexpected` is a web interface
# that answers, but not as OpenWebif does: anything but 200, or no JSON, on its API.
# `error` is a fault in this module, kept from becoming a request per payload.
PROBLEM_UNREACHABLE: Final = "unreachable"
PROBLEM_REFUSED: Final = "refused"
PROBLEM_UNEXPECTED: Final = "unexpected"
PROBLEM_ERROR: Final = "error"
BACKOFF: Final = {
    PROBLEM_UNREACHABLE: 5 * 60.0,
    PROBLEM_REFUSED: 15 * 60.0,
    PROBLEM_UNEXPECTED: 15 * 60.0,
    PROBLEM_ERROR: 15 * 60.0,
}

# What the diagnostics say about the lookup besides the problems.
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


def getpicon_answered(body: bytes) -> bool:
    """Return whether a body is a `getpicon` answer at all: an object with a `result`.

    An answer can say yes and still name a link that is not used here - a file outside
    the directory OpenWebif serves. That is OpenWebif answering, not a web interface
    that is something else.
    """
    payload = _loads(body)
    return isinstance(payload, dict) and isinstance(payload.get("result"), bool)


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
        # The receiver was off and has not said which channel it is on since.
        self._held = False

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

        if not is_on:
            if self.shown is not None:
                # Standby and an unreachable receiver hold the last picture.
                self._held = True
                return self.shown
        elif self._held:
            if not identity:
                # Awake, but the channel is not known yet: `power` arrives before
                # `service`, and the last screenshot is of whatever was on before.
                return self.shown
            self._held = False

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
    """The receiver's web interface cannot be asked now; `problem` says why."""

    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


@dataclass(slots=True)
class _BouquetLinks:
    """One bouquet's picon links, and the channel list they were asked for.

    `links` is None for a bouquet whose request failed: remembered, so that it is not
    made again every time the bouquet is opened.
    """

    signature: int
    expires: float
    links: dict[str, str] | None


def _bouquet_sref(bouquet: Any) -> str | None:
    """Return a bouquet's reference if it is one: text, and not empty."""
    sref = bouquet.get("sref") if isinstance(bouquet, dict) else None
    return sref if isinstance(sref, str) and sref else None


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
        # The one task that asks the receiver, and what it is to do: the service the
        # player is waiting for, which comes first, and the bouquets opened in the media
        # browser, by reference.
        self._task: asyncio.Task[None] | None = None
        self._wanted: str | None = None
        self._scans: dict[str, dict[str, Any]] = {}
        # Per service reference: when the answer expires, and the picon or None.
        self._pictures: OrderedDict[str, tuple[float, Picture | None]] = OrderedDict()
        self._bouquets: OrderedDict[str, _BouquetLinks] = OrderedDict()
        # Until when `getpicon` is not asked: it did not answer, and the name built from
        # the reference found a picture, which is an OpenWebif without it.
        self._getpicon_missing_until = 0.0
        self._problem: str | None = None
        self._retry_at = 0.0
        self._logged: str | None = None
        self._answered = False
        # The one retry after a failed lookup, and the lookup it started.
        self._retry_cancel: CALLBACK_TYPE | None = None
        self._retried: str | None = None

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
        self._scans.clear()
        self._listeners.clear()
        self._cancel_retry()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # ---------------------------------------------------------------------- answers

    @callback
    def async_picon(self, sref: str) -> Picture | PendingPicon | None:
        """Return a service's picon, None when there is none, or `PENDING`.

        Never waits and never raises: this is called while an MQTT message is being
        handled. `PENDING` means the task is asking the receiver, and the listeners are
        called when it has finished - whatever the result. Asking for another service
        than the one being looked up abandons that lookup: the channel has changed.
        """
        if not isinstance(sref, str):
            return None
        key = sref.strip()
        if (cached := self._pictures.get(key)) is not None:
            if cached[0] > self._clock():
                self._pictures.move_to_end(key)
                return cached[1]
            del self._pictures[key]
        if not self._may_ask():
            return None
        if self._wanted != key or not self._working():
            self._wanted = key
            self._start(abandon=True)
        return PENDING

    @callback
    def async_thumbnails(self, bouquet: dict[str, Any]) -> dict[str, str | None]:
        """Return what is held of a bouquet's picons: service identity -> URL, or None.

        Never waits. A bouquet nothing is held for is asked about in the background, so
        the next time it is opened its links are here; until then this is empty and the
        caller uses the name built from the reference. A service OpenWebif says has no
        picon maps to None, and one it did not answer for is left out. The URL points at
        the receiver, as the media browser's thumbnails always have.
        """
        try:
            sref = _bouquet_sref(bouquet)
            if self._stopped or sref is None or (base := self._base_url()) is None:
                return {}
            if (known := self._known_bouquet_links(bouquet)) is None:
                if self._may_ask():
                    self._scans.setdefault(sref, bouquet)
                    self._start(abandon=False)
                return {}
            return {
                identity: str(base.with_path(link)) if link else None
                for identity, link in (known.links or {}).items()
            }
        except Exception:
            self._async_unavailable(PROBLEM_ERROR, exc_info=True)
            return {}

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
            "bouquets_cached": sum(1 for kept in self._bouquets.values() if kept.links is not None),
            "picons_cached": sum(1 for _, picture in self._pictures.values() if picture),
            "services_without_picon": sum(
                1 for _, picture in self._pictures.values() if picture is None
            ),
        }

    # -------------------------------------------------------------------- the task

    def _base_url(self) -> URL | None:
        """Return the address picons are asked at, or None when there is none to ask."""
        url = self.box.picon_lookup_url
        return URL(url) if url else None

    def _may_ask(self) -> bool:
        """Return whether the receiver may be asked anything right now."""
        return (
            not self._stopped
            and self.box.available
            and self._clock() >= self._retry_at
            and self._base_url() is not None
        )

    def _working(self) -> bool:
        """Return whether the task is alive."""
        return self._task is not None and not self._task.done()

    @callback
    def _start(self, *, abandon: bool) -> None:
        """Make sure the task runs; with `abandon`, drop what it is doing for a new one.

        The request in flight is cancelled with it. A bouquet it was asking about stays
        queued and is asked about again afterwards.
        """
        if self._working():
            if not abandon or asyncio.current_task() is self._task:
                # Called from the task itself, by a listener: its loop takes it from here.
                return
            self._task.cancel()
        # Not started eagerly: the caller is in the middle of reading its state, and a
        # lookup answered without a wait would report back into that read.
        self._task = self.entry.async_create_background_task(
            self.hass,
            self._async_run(),
            f"{DOMAIN} picon {self.entry.entry_id}",
            eager_start=False,
        )

    async def _async_run(self) -> None:
        """Ask the receiver: the player's service first, then the bouquets opened.

        One request at a time, and the back-off is read again before each of them: a
        failure ends everything that was queued behind it. Any failure of the player's
        lookup is reported like an answer, so that a pending picture is never left
        pending.
        """
        while not self._stopped:
            sref = self._wanted
            bouquet: dict[str, Any] | None = None
            if sref is None:
                if not self._scans:
                    return
                bouquet = next(iter(self._scans.values()))
            if not self._may_ask():
                self._scans.clear()
                if sref is None:
                    return
            else:
                try:
                    if sref is not None:
                        picture = await self._async_resolve(sref)
                        if self._stopped:
                            return
                        self._remember(sref, picture)
                    elif bouquet is not None:
                        await self._async_scan(bouquet)
                except _Unavailable as error:
                    self._async_unavailable(error.problem, sref=sref, bouquet=bouquet)
                except Exception:
                    self._async_unavailable(
                        PROBLEM_ERROR, sref=sref, bouquet=bouquet, exc_info=True
                    )
            if self._stopped:
                return
            if sref is None:
                self._scans.pop(_bouquet_sref(bouquet) or "", None)
                continue
            if self._wanted == sref:
                self._wanted = None
            self._retried = None
            self._tell()

    @callback
    def _tell(self) -> None:
        """Call the listeners; one that raises does not stop the others or the task."""
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                _LOGGER.debug("A listener of the picon lookup failed", exc_info=True)

    async def _async_resolve(self, sref: str) -> Picture | None:
        """Ask the receiver for one service's picon. None: it has none we can use.

        A bouquet's answer that is already held, then `getpicon`, then the name built
        from the reference. A bouquet is never listed for this.
        """
        if (base := self._base_url()) is None:
            return None
        link = self._known_link(sref)
        # Whether this is known to be an OpenWebif: it answered for this service, or it
        # is one without `getpicon` that has served a picon under the old name before.
        proven = link is not None or self._clock() < self._getpicon_missing_until
        if not proven:
            body = await self._async_get(
                base.with_path("/api/getpicon").with_query(
                    {"sRef": getpicon_reference(sref), "json": "true"}
                ),
                MAX_JSON_BYTES,
            )
            if body is not None and getpicon_answered(body):
                proven = True
                link = parse_getpicon(body)
                self._async_answered()
        if link == NO_PICON:
            return None
        if link is None:
            # No link from OpenWebif - no answer, or one that cannot be used.
            link = legacy_picon_link(sref)
        body = (
            await self._async_get(base.with_path(link), MAX_IMAGE_BYTES)
            if link is not None
            else None
        )
        picture = picon_picture(body) if body is not None else None
        if not proven:
            # A web interface that answers neither question is not asked a third one
            # for every channel: it has to show a picon under the old name, or wait.
            if picture is None:
                raise _Unavailable(PROBLEM_UNEXPECTED)
            self._getpicon_missing_until = self._clock() + NEGATIVE_TTL
        if picture is not None:
            self._async_answered()
        return picture

    def _known_link(self, sref: str) -> str | None:
        """Return a service's link from a bouquet's answer that is held, if any has it."""
        identity = service_identity(sref)
        for bouquet in self.box.bouquets:
            if _bouquet_sref(bouquet) is None:
                continue
            known = self._known_bouquet_links(bouquet)
            if known is not None and known.links is not None and identity in known.links:
                return known.links[identity]
        return None

    async def _async_scan(self, bouquet: dict[str, Any]) -> None:
        """Ask for one bouquet's picon links, for the media browser, and keep them."""
        sref = _bouquet_sref(bouquet)
        if sref is None or self._known_bouquet_links(bouquet) is not None:
            return
        if (base := self._base_url()) is None:
            return
        body = await self._async_get(
            base.with_path("/api/getservices").with_query({"sRef": sref, "picon": "1"}),
            MAX_JSON_BYTES,
        )
        if body is None:
            raise _Unavailable(PROBLEM_UNEXPECTED)
        # A large bouquet's answer takes a fraction of a second to read: not on the loop.
        links = await self.hass.async_add_executor_job(parse_service_picons, body)
        if links is None:
            raise _Unavailable(PROBLEM_UNEXPECTED)
        if self._stopped:
            return
        self._async_answered()
        self._keep_bouquet(bouquet, links, ANSWER_TTL)

    @staticmethod
    def _signature(bouquet: dict[str, Any]) -> int:
        """Return a value that changes when a bouquet's channel list does."""
        channels = bouquet.get("channels")
        return hash(
            tuple(
                service_identity(channel.get("sref"))
                for channel in (channels if isinstance(channels, list) else ())
                if isinstance(channel, dict)
            )
        )

    def _known_bouquet_links(self, bouquet: dict[str, Any]) -> _BouquetLinks | None:
        """Return what is held for a bouquet, if it is for this channel list and fresh."""
        if (sref := _bouquet_sref(bouquet)) is None:
            return None
        cached = self._bouquets.get(sref)
        if (
            cached is not None
            and cached.signature == self._signature(bouquet)
            and cached.expires > self._clock()
        ):
            return cached
        return None

    def _keep_bouquet(
        self, bouquet: dict[str, Any], links: dict[str, str] | None, ttl: float
    ) -> None:
        """Keep one bouquet's answer, within the bounds on bouquets and links."""
        if self._stopped or (sref := _bouquet_sref(bouquet)) is None:
            return
        # Only the bouquets on offer are ever asked about, so only they are kept.
        offered = {_bouquet_sref(offer) for offer in self.box.bouquets}
        for known in [known for known in self._bouquets if known not in offered]:
            del self._bouquets[known]
        self._bouquets[sref] = _BouquetLinks(
            self._signature(bouquet), self._clock() + ttl, links
        )
        self._bouquets.move_to_end(sref)

        def held() -> int:
            return sum(len(kept.links or ()) for kept in self._bouquets.values())

        while len(self._bouquets) > 1 and (
            len(self._bouquets) > MAX_BOUQUETS or held() > MAX_BOUQUET_LINKS
        ):
            self._bouquets.popitem(last=False)

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

    # ------------------------------------------------------------- failure, recovery

    @callback
    def _async_answered(self) -> None:
        """Note that OpenWebif gave an answer that could be used, which ends any problem."""
        self._answered = True
        if self._problem is not None:
            _LOGGER.debug("The web interface of %s answers again", self.box.node_id)
        self._problem = None
        self._logged = None
        self._retry_at = 0.0
        self._cancel_retry()

    @callback
    def _async_unavailable(
        self,
        problem: str,
        *,
        sref: str | None = None,
        bouquet: dict[str, Any] | None = None,
        exc_info: bool = False,
    ) -> None:
        """Stop asking for a while, remember what failed, and say why once.

        A bouquet whose request failed is remembered as failed for at least as long as
        the back-off. A failed lookup for the player gets one retry when the back-off
        ends, in the picon setting only, where nothing else would bring the picon back
        before the next payload; a retry that fails is not followed by another.
        """
        if self._stopped:
            return
        self._problem = problem
        self._retry_at = self._clock() + BACKOFF[problem]
        if bouquet is not None:
            self._keep_bouquet(bouquet, None, max(NEGATIVE_TTL, BACKOFF[problem]))
        if (
            sref is not None
            and sref != self._retried
            and self._retry_cancel is None
            and player_picture_mode(self.entry.options) == PLAYER_PICTURE_PICON
        ):
            self._retry_cancel = async_call_later(
                self.hass, BACKOFF[problem] + 1, self._async_retry
            )
        if self._logged == problem:
            return
        self._logged = problem
        minutes = BACKOFF[problem] // 60
        if problem == PROBLEM_ERROR:
            _LOGGER.warning(
                "The picon lookup for %s failed unexpectedly; the screenshot is shown "
                "instead. This is logged once; it is tried again in %d minutes",
                self.box.node_id,
                minutes,
                exc_info=exc_info,
            )
            return
        cause = {
            PROBLEM_REFUSED: "asks for a login",
            PROBLEM_UNREACHABLE: "could not be reached",
            PROBLEM_UNEXPECTED: "does not answer the way OpenWebif does",
        }[problem]
        _LOGGER.warning(
            "The web interface (OpenWebif) of %s %s, so channel picons cannot be fetched "
            "and the screenshot is shown instead. This is logged once; it is tried again "
            "in %d minutes at the earliest",
            self.box.node_id,
            cause,
            minutes,
        )

    @callback
    def _async_retry(self, _now: datetime) -> None:
        """Let the player ask once more, now that the back-off has ended."""
        self._retry_cancel = None
        if self._stopped:
            return
        self._tell()
        # Whatever the listeners asked for is the retry; if it fails, that is the end.
        self._retried = self._wanted

    @callback
    def _cancel_retry(self) -> None:
        """Forget the retry that was waiting."""
        if self._retry_cancel is not None:
            self._retry_cancel()
            self._retry_cancel = None

    def _remember(self, sref: str, picture: Picture | None) -> None:
        """Keep one service's answer, within the bounds on count and bytes."""
        if self._stopped:
            return
        ttl = ANSWER_TTL if picture is not None else NEGATIVE_TTL
        self._pictures[sref] = (self._clock() + ttl, picture)
        self._pictures.move_to_end(sref)

        def held() -> int:
            return sum(len(kept.content) for _, kept in self._pictures.values() if kept)

        while len(self._pictures) > 1 and (
            len(self._pictures) > MAX_PICTURES or held() > MAX_PICTURE_BYTES
        ):
            self._pictures.popitem(last=False)

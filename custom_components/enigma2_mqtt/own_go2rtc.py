"""One stream of the user's own go2rtc, kept on the channel the receiver is on.

A receiver serves a transport stream that a browser cannot play. go2rtc can turn it into
something a dashboard card plays, and many households already run one - the go2rtc
add-on, for instance. This module does one thing for them: while an address is set in
the receiver's options, it keeps **one** stream of that go2rtc, under one name, pointed
at the service on the decoder:

    PUT <address>/api/streams?name=<name>&src=<source>

sent when the service changes, and once after start-up when the first `service` has
arrived. `<source>` is the options' template with the receiver's stream address in the
place of `{url}`.

It is the user's go2rtc, not Home Assistant's: neither the `go2rtc` nor the `stream`
integration is used or depended on, no camera entity is made, and nothing but that one
request is ever sent there. Off while the address is empty.

What is known about go2rtc (measured against 1.9.14) and respected here:

- A source that contains a space is refused with HTTP 400, "source with spaces may be
  insecure". The template is therefore refused on the options form when it has white
  space in it, and the stream address is percent-encoded. A filter that needs spaces
  belongs in a named template in go2rtc's own configuration.
- go2rtc cannot read the receiver's stream port without `ffmpeg:`, which is why the
  default template starts with it.

When it does not re-point - and leaves the stream on the last channel it was given:

- in standby, and while the receiver is offline: the media player keeps its last
  picture then, and this keeps its last stream;
- for a channel of a bouquet the options hide: pointing the stream at nothing would
  need a second request that says "something hidden is on", and a card on a dashboard
  would break; the last shown channel stays;
- while the receiver's stream server wants a login: no address is built then.

A failure is reported once: one line in the log and one repair for the receiver, until
a request succeeds again. It is tried again at the next change of channel and every
five minutes. Nothing here raises into whatever delivered the message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
import logging
import re
from typing import Any

import aiohttp
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from yarl import URL

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import (
    CONF_GO2RTC_SOURCE,
    CONF_GO2RTC_STREAM,
    CONF_GO2RTC_URL,
    DEFAULT_GO2RTC_SOURCE,
    DOMAIN,
    GO2RTC_RETRY_SECONDS,
    GO2RTC_TIMEOUT,
    TOPIC_CHANNELS,
    TOPIC_INFO,
    TOPIC_POWER,
    TOPIC_SERVICE,
    TOPIC_STREAM,
)
from .withheld import _named

_LOGGER = logging.getLogger(__name__)

# Where the receiver's stream address goes in the source template, exactly once.
URL_PLACEHOLDER = "{url}"

# The names this integration lets the stream have. go2rtc itself is more liberal; these
# are the characters that need no escaping in an address, in go2rtc's YAML or in a
# dashboard card's configuration.
_STREAM_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_NOT_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_WHITE_SPACE = re.compile(r"\s")

ERROR_URL = "go2rtc_invalid_url"
ERROR_STREAM = "go2rtc_invalid_stream"
ERROR_SPACES = "go2rtc_source_spaces"
ERROR_PLACEHOLDER = "go2rtc_source_placeholder"

PROBLEM_UNREACHABLE = "unreachable"
PROBLEM_REFUSED = "refused"

ISSUE_KEY = "go2rtc"


def issue_id(entry_id: str) -> str:
    """Return the id of one receiver's go2rtc repair."""
    return f"{ISSUE_KEY}_{entry_id}"


@callback
def async_delete_go2rtc_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Delete one receiver's go2rtc repair."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id))


def default_stream_name(node_id: str) -> str:
    """Return the stream name used when none is typed: `enigma2_<node id>`."""
    return _NOT_NAME.sub("_", f"enigma2_{node_id}").strip("_")[:64] or "enigma2"


def normalise_base_url(value: Any) -> str | None:
    """Return the address of a go2rtc API as it is used, or None if it is not one.

    `http://` or `https://`, a host, optionally a port, a user name and a path in
    front of the API - and nothing after it: a query or a fragment is somebody pasting
    a whole request. The trailing slash is dropped.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or _WHITE_SPACE.search(text):
        return None
    try:
        url = URL(text)
        _ = url.port
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.host:
        return None
    if url.query_string or url.fragment or "?" in text or "#" in text:
        return None
    return text.rstrip("/")


def source_error(source: str) -> str | None:
    """Return why a source template cannot be used, or None when it can.

    go2rtc refuses a source with a space in it, so white space anywhere fails here,
    where the person typing it can read why; and the receiver's address has to have
    exactly one place to go.
    """
    if _WHITE_SPACE.search(source):
        return ERROR_SPACES
    if source.count(URL_PLACEHOLDER) != 1:
        return ERROR_PLACEHOLDER
    return None


def validate_options(values: Mapping[str, Any], node_id: str) -> tuple[dict[str, str], str | None]:
    """Return the go2rtc options to store, and the error of the first field that is wrong.

    Only what was set is stored: an empty address is "off" and is not stored, and a name
    or a template equal to the default is the default and is not stored either - so an
    installation that never used this has none of its keys.
    """
    url = str(values.get(CONF_GO2RTC_URL) or "").strip()
    stream = str(values.get(CONF_GO2RTC_STREAM) or "").strip()
    source = str(values.get(CONF_GO2RTC_SOURCE) or "").strip()
    stored: dict[str, str] = {}
    error: str | None = None
    if url:
        if (normalised := normalise_base_url(url)) is None:
            error = ERROR_URL
        else:
            stored[CONF_GO2RTC_URL] = normalised
    if stream and stream != default_stream_name(node_id):
        if not _STREAM_NAME.fullmatch(stream):
            error = error or ERROR_STREAM
        stored[CONF_GO2RTC_STREAM] = stream
    if source and source != DEFAULT_GO2RTC_SOURCE:
        error = error or source_error(source)
        stored[CONF_GO2RTC_SOURCE] = source
    return stored, error


class Go2rtcFollower:
    """Keeps one go2rtc stream on the service one receiver's decoder is showing."""

    def __init__(
        self, hass: HomeAssistant, entry: Enigma2MqttConfigEntry, box: Enigma2Box
    ) -> None:
        """Read the options; without a usable address this does nothing at all."""
        self.hass = hass
        self.entry = entry
        self.box = box
        options = entry.options
        self._base = normalise_base_url(options.get(CONF_GO2RTC_URL))
        stream = options.get(CONF_GO2RTC_STREAM)
        self.stream_name: str = (
            stream
            if isinstance(stream, str) and _STREAM_NAME.fullmatch(stream)
            else default_stream_name(box.node_id)
        )
        template = options.get(CONF_GO2RTC_SOURCE)
        self._template: str = (
            template
            if isinstance(template, str) and template and source_error(template) is None
            else DEFAULT_GO2RTC_SOURCE
        )
        # The source go2rtc last took, the one it should have, and the request that is
        # bringing the two together.
        self._applied: str | None = None
        self._wanted: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._retry: CALLBACK_TYPE | None = None
        # Why the last request failed, None while the last one worked or none was made.
        self.problem: str | None = None
        self.http_status: int | None = None
        self.requests: int = 0

    @property
    def enabled(self) -> bool:
        """Return whether an address is set, which is the whole switch."""
        return self._base is not None

    def start(self) -> CALLBACK_TYPE:
        """Follow the receiver from now on, and return what stops it."""
        if not self.enabled:
            # Switched off in the options: a repair about it has nothing left to say.
            async_delete_go2rtc_issue(self.hass, self.entry.entry_id)
            return lambda: None
        remove = self.box.async_add_listener(
            self._update,
            (TOPIC_SERVICE, TOPIC_POWER, TOPIC_STREAM, TOPIC_INFO, TOPIC_CHANNELS),
        )
        self._update()

        @callback
        def _stop() -> None:
            remove()
            self._cancel_retry()
            if self._task is not None:
                self._task.cancel()

        return _stop

    @callback
    def async_status(self) -> dict[str, Any]:
        """Return the state of this for the diagnostics download: no address, no channel."""
        if not self.enabled:
            state = "off"
        elif self.problem is not None:
            state = "failed"
        else:
            state = "ok" if self._applied is not None else "idle"
        return {
            "state": state,
            "stream": self.stream_name,
            "problem": self.problem,
            "http_status": self.http_status,
            "requests": self.requests,
        }

    def _source(self) -> str | None:
        """Return the source go2rtc should have now, or None to leave it as it is.

        None in standby and while the receiver is offline, and whenever there is no
        stream address for the service on the decoder: a hidden channel, a stream server
        that wants a login, no `stream` facts, nothing playing.
        """
        box = self.box
        if not box.is_on:
            return None
        service = box.state.service or {}
        url = box.stream_url_for(service.get("sref"), service.get("bouquet"))
        if url is None:
            return None
        return self._template.replace(URL_PLACEHOLDER, url)

    @callback
    def _update(self) -> None:
        """Ask for a request when the source go2rtc should have is a new one.

        The same source again - `service` is published again when the resolution or the
        audio track becomes known - asks for nothing, also when the last request for it
        failed: that one is retried by the timer, not by every message.
        """
        source = self._source()
        if source is None or source == self._wanted:
            return
        self._wanted = source
        self._cancel_retry()
        self._async_ensure_worker()

    @callback
    def _async_ensure_worker(self) -> None:
        """Start the task that sends requests, unless one is running."""
        if self._task is not None and not self._task.done():
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_work(), f"{DOMAIN} go2rtc {self.entry.entry_id}"
        )

    @callback
    def _cancel_retry(self) -> None:
        if self._retry is not None:
            self._retry()
            self._retry = None

    @callback
    def _async_retry(self, _now: datetime) -> None:
        """Try the source that failed once more."""
        self._retry = None
        if self._wanted != self._applied:
            self._async_ensure_worker()

    async def _async_work(self) -> None:
        """Send requests, one at a time, until go2rtc has the source it should have.

        A channel change while a request is running is picked up when it returns, so
        zapping through five channels costs the requests that had time to finish and
        ends on the last one. A failure ends the run; the same source is tried again
        after `GO2RTC_RETRY_SECONDS`, a newer one at once.
        """
        try:
            while self._wanted is not None and self._wanted != self._applied:
                source = self._wanted
                problem, status = await self._async_put(source)
                if problem is None:
                    self._applied = source
                    self._async_succeeded()
                    continue
                self._async_failed(problem, status)
                if self._wanted == source:
                    self._retry = async_call_later(
                        self.hass, GO2RTC_RETRY_SECONDS, self._async_retry
                    )
                    return
        except Exception:
            # A fault in this module must not reach whatever started the task, and must
            # not repeat on every channel change either.
            if self.problem is None:
                _LOGGER.exception(
                    "Keeping go2rtc on the channel of receiver %s failed unexpectedly",
                    self.box.node_id,
                )
            self.problem = "error"

    async def _async_put(self, source: str) -> tuple[str | None, int | None]:
        """Send one request, and return what went wrong with it, if anything.

        Home Assistant's shared session, a bounded wait, and no redirect followed: a
        go2rtc that answers 3xx is not taking the stream.
        """
        assert self._base is not None
        self.requests += 1
        url = (URL(self._base) / "api" / "streams").with_query(
            {"name": self.stream_name, "src": source}
        )
        session = async_get_clientsession(self.hass)
        try:
            async with (
                asyncio.timeout(GO2RTC_TIMEOUT),
                session.put(url, allow_redirects=False) as response,
            ):
                status = response.status
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return PROBLEM_UNREACHABLE, None
        if 200 <= status < 300:
            return None, status
        return PROBLEM_REFUSED, status

    @callback
    def _async_succeeded(self) -> None:
        """Note that go2rtc took the stream, which ends any problem."""
        if self.problem is not None:
            _LOGGER.info(
                "go2rtc takes the stream %s of receiver %s again",
                self.stream_name,
                self.box.node_id,
            )
        self.problem = None
        self.http_status = None
        # Also one left by an earlier run of this entry, which this run never logged.
        async_delete_go2rtc_issue(self.hass, self.entry.entry_id)

    @callback
    def _async_failed(self, problem: str, status: int | None) -> None:
        """Report a failure once: a line in the log and a repair, until it works again.

        The address is named by its host alone, here and in the repair: the address
        somebody typed can carry a user name and a password.
        """
        again = self.problem == problem and self.http_status == status
        self.problem = problem
        self.http_status = status
        if again:
            return
        assert self._base is not None
        host = URL(self._base).host or "?"
        _LOGGER.warning(
            "go2rtc at %s did not take the stream %s of receiver %s (%s); it still shows "
            "an earlier channel. Tried again at the next channel change and every %d "
            "minutes, and not logged again until it works",
            host,
            self.stream_name,
            self.box.node_id,
            "no answer" if status is None else f"HTTP {status}",
            GO2RTC_RETRY_SECONDS // 60,
        )
        placeholders = {
            "name": _named(
                str(self.box.announcement.get("name") or ""),
                self.box.name,
                self.entry.title,
                self.box.node_id,
            ),
            "host": _named(host),
            "stream": self.stream_name,
        }
        if problem == PROBLEM_REFUSED:
            placeholders["status"] = str(status)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self.entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=f"{ISSUE_KEY}_{problem}",
            translation_placeholders=placeholders,
        )

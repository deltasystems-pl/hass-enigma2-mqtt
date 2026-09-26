"""The plugin's signed release index as this Home Assistant knows it: fetched, verified, kept.

One cache serves every receiver. The index is the same document for all of them, so two receivers
checking at the same moment make one request, and the verdict - which serial, which key, which
versions - is one verdict. It lives in Home Assistant's storage, not in an entity, because an
entity is built again on every reload, every options save and every restart, and a limit kept in
one is reset by all three: six reloads were once six requests.

**What is stored**: the exact bytes of the last accepted index and its signature file, their
serial and ETag, when they were fetched, when anything was last asked and what went wrong then,
and the trust memory - the last serial per key and the keys silenced - in the one shape both
programs write (`release_index.store`). The trust memory is never rewritten from a file it could
not read: a damaged memory is reported and judges nothing, because reading it as "nothing
remembered" would put the reader back at first sight with no key silenced.

**When it asks.** Never on its own. The daily check runs only for a receiver whose option asks for
it, at most once every 24 hours; a press of „Sprawdź aktualizacje wtyczki" and Home Assistant's
own "Check for updates" are requests for one, at most once every ten minutes. Every stamp is
written before the request, so a request that fails has still spent its turn - retrying on every
reload is the behaviour most likely to keep a rate-limited service limited.

**How it asks.** From the plugin's fixed HTTPS origin only, through an SSL context that verifies
the certificate and the host name - asserted, not assumed - and with redirects refused: the
origin serves the files directly, so a redirect is somebody else's answer. Ten seconds, 64 KiB for
the index and 1 KiB for the signature, read with a ceiling so an answer with no end is never
pulled into memory whole. The ETag makes an unchanged index cost one `304`.

**What a new index does.** Every newly accepted index is announced - a warning in the log and a
persistent notification naming its serial, its key, the versions it adds and withdraws, and its
floor - because the signing key is used in the plugin repository's CI: an index the maintainer did
not approve must be seen by anybody who looks, not only installed. It is then published, retained,
on `enigma2mqtt/release_index`, so a receiver with no internet of its own can verify and use it.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import ssl
from typing import Any

import aiohttp
from homeassistant.components import mqtt, persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import async_get_integration
import homeassistant.util.dt as dt_util
from homeassistant.util.ssl import get_default_context

from . import release_index
from .const import (
    DOMAIN,
    LEGACY_RELEASE_CHECK_STORAGE_KEY,
    PLUGIN_INDEX_FILE,
    PLUGIN_INDEX_ORIGIN,
    PLUGIN_INDEX_SIGNATURE_FILE,
    RELEASE_CHECK_INTERVAL,
    RELEASE_CHECK_SLACK,
    RELEASE_CHECK_TIMEOUT,
    RELEASE_INDEX_STORAGE_KEY,
    RELEASE_INDEX_STORAGE_VERSION,
    RELEASE_MANUAL_INTERVAL,
    SIGNAL_RELEASE_INDEX,
    TOPIC_RELEASE_INDEX,
)

_LOGGER = logging.getLogger(__name__)

# What went wrong with the last check, as `check_error` names it. The index's own refusals are
# `release_index.REASONS`; these are the ones that happen before an index is judged.
ERROR_UNREACHABLE = "unreachable"
ERROR_REDIRECT = "redirect"
ERROR_HTTP = "http_error"
ERROR_BAD_MEMORY = "bad_memory"
# The refusals a household is told as "the list has a bad signature or is older than the one
# already known"; everything else from the rule is a malformed or oversized file.
SIGNATURE_OR_ORDER = frozenset(
    {"unknown_key", "key_mismatch", "rank", "replay", "jump", "first_sight"}
)
# A signature that did not verify twice: said as "could not be verified", because a pair read
# while a new index was being published fails the same way, and a false alarm at every release
# would teach anybody to ignore the real one.
ERROR_BAD_SIGNATURE = "bad_signature"

# What a check came to.
RESULT_SKIPPED = "skipped"
RESULT_UNCHANGED = "unchanged"
RESULT_ACCEPTED = "accepted"
RESULT_FAILED = "failed"


class CheckRateLimited(Exception):
    """A manual check inside the ten minutes after the last one."""

    def __init__(self, checked: datetime, remaining_seconds: float) -> None:
        """Keep when the last check ran and how long until the next may."""
        super().__init__("checked less than ten minutes ago")
        self.checked = checked
        self.remaining_seconds = remaining_seconds


class _Fetched(Exception):
    """A fetch that ended without two files to judge, with the error code to report."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class CheckResult:
    """What one check did: skipped, unchanged, accepted or failed - and why it failed."""

    outcome: str
    error: str | None = None


def verified_context() -> ssl.SSLContext:
    """An SSL context that verifies the origin's certificate and host name, or an error.

    Home Assistant's default client context does both; this says so where the request is made,
    so that a change anywhere else cannot quietly turn the index fetch into an unverified one.
    """
    context = get_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("the release index is fetched only with a verifying SSL context")
    return context


async def _async_get(
    session: aiohttp.ClientSession,
    url: str,
    limit: int,
    headers: dict[str, str],
) -> tuple[int, bytes, str | None]:
    """`(status, body, etag)` of one GET, reading at most `limit + 1` bytes of the body.

    One byte past the limit is read on purpose: the rule refuses a file over its cap as
    `too_large`, and a body cut exactly at the cap would be judged as a different, shorter file.
    """
    async with (
        asyncio.timeout(RELEASE_CHECK_TIMEOUT),
        session.get(
            url,
            headers=headers,
            allow_redirects=False,
            ssl=verified_context(),
        ) as response,
    ):
        status = response.status
        if 300 <= status < 400 and status != 304:
            raise _Fetched(ERROR_REDIRECT, f"{url} answered with a redirect ({status})")
        if status not in (200, 304):
            raise _Fetched(ERROR_HTTP, f"{url} answered HTTP {status}")
        body = bytearray()
        if status == 200:
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > limit:
                    del body[limit + 1 :]
                    break
        etag = response.headers.get("ETag")
        return status, bytes(body), etag if isinstance(etag, str) else None


def _torn(index_raw: bytes, signature_raw: bytes) -> bool:
    """Whether a pair fails the signature check - the one failure a publication race makes."""
    try:
        release_index.authenticate(index_raw, signature_raw, release_index.EMBEDDED)
    except release_index.Refused as error:
        return error.reason == "bad_signature"
    return False


def _b64(value: bytes | None) -> str | None:
    return base64.b64encode(value).decode("ascii") if value is not None else None


def _unb64(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        return None


def _stamp(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


def _withdrawn(index: dict[str, Any] | None) -> set[str]:
    if not index:
        return set()
    return {
        release["version"]
        for release in index["releases"]
        if release.get("withdrawn") is not None
    }


def _versions(index: dict[str, Any] | None) -> set[str]:
    if not index:
        return set()
    return {release["version"] for release in index["releases"]}


class ReleaseIndexCache:
    """The one verified release index every receiver's entities read."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Open the store without reading it."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, RELEASE_INDEX_STORAGE_VERSION, RELEASE_INDEX_STORAGE_KEY
        )
        self._loaded = False
        # One check at a time: a second caller waits and then finds the stamp the first wrote.
        self._lock = asyncio.Lock()
        self.index_raw: bytes | None = None
        self.signature_raw: bytes | None = None
        self.index: dict[str, Any] | None = None
        self.key_id: str | None = None
        self.etag: str | None = None
        self.fetched: datetime | None = None
        self.checked: datetime | None = None
        self.check_error: str | None = None
        self.trust: Any = None
        self.integration_version: str | None = None

    # ----------------------------------------------------------------------- storage --

    async def async_load(self) -> None:
        """Read the store once, and drop the per-receiver records of the old release check."""
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            integration = await async_get_integration(self.hass, DOMAIN)
            self.integration_version = str(integration.version) if integration.version else None
            stored = await self._store.async_load()
            if isinstance(stored, dict):
                self._restore(stored)
            # The old check kept one record per receiver about GitHub's latest release. The
            # index takes its place; nothing in those records is worth carrying over.
            legacy: Store[Any] = Store(self.hass, 1, LEGACY_RELEASE_CHECK_STORAGE_KEY)
            if await legacy.async_load() is not None:
                await legacy.async_remove()
            self._loaded = True

    def _restore(self, stored: dict[str, Any]) -> None:
        """Take the stored values back, believing the index only if it still verifies."""
        self.trust = stored.get("trust")
        self.etag = stored.get("etag") if isinstance(stored.get("etag"), str) else None
        self.fetched = _stamp(stored.get("fetched"))
        self.checked = _stamp(stored.get("checked"))
        error = stored.get("check_error")
        self.check_error = error if isinstance(error, str) else None
        index_raw = _unb64(stored.get("index"))
        signature_raw = _unb64(stored.get("sig"))
        if index_raw is None or signature_raw is None:
            return
        try:
            index, key = release_index.authenticate(
                index_raw, signature_raw, release_index.EMBEDDED
            )
        except release_index.Refused:
            # A key this release no longer embeds, or a file changed on disk: the cached index
            # is not one this reader would accept today, so it is not shown. The trust memory
            # stays - it is about keys, not about this file.
            _LOGGER.debug("The stored plugin release index no longer verifies; dropping it")
            self.etag = None
            return
        self.index_raw, self.signature_raw = index_raw, signature_raw
        self.index, self.key_id = index, key.key_id

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "index": _b64(self.index_raw),
                "sig": _b64(self.signature_raw),
                "serial": self.serial,
                "key_id": self.key_id,
                "etag": self.etag,
                "fetched": self.fetched.isoformat() if self.fetched else None,
                "checked": self.checked.isoformat() if self.checked else None,
                "check_error": self.check_error,
                "trust": self.trust,
            }
        )

    # ---------------------------------------------------------------------- readings --

    @property
    def serial(self) -> int | None:
        """The accepted index's serial, or None."""
        return self.index["serial"] if self.index else None

    @property
    def issued(self) -> int | None:
        """When the accepted index was built, epoch seconds, or None."""
        return self.index["issued"] if self.index else None

    # ------------------------------------------------------------------------ checks --

    async def async_check(self, *, manual: bool) -> CheckResult:
        """Check the origin for a newer index, within the limit of the kind of check asked for.

        `manual` is a person asking - the button, or Home Assistant's "Check for updates" - and
        raises CheckRateLimited inside ten minutes of the last check. Otherwise it is the daily
        check, which is skipped inside a day of the last one.
        """
        await self.async_load()
        async with self._lock:
            now = dt_util.utcnow()
            # A stamp in the future was written by a clock that was ahead - a host that
            # booted before its time was set - and is not a check that happened: it would
            # otherwise block every check for as long as the clock had been wrong.
            if self.checked is not None and self.checked <= now:
                elapsed = now - self.checked
                if manual and elapsed < RELEASE_MANUAL_INTERVAL:
                    raise CheckRateLimited(
                        self.checked, (RELEASE_MANUAL_INTERVAL - elapsed).total_seconds()
                    )
                # The daily timer fires a day after it was scheduled, and the stamp was taken
                # a moment after that: without the slack every other firing is skipped, and a
                # daily check becomes one every two days.
                if not manual and elapsed < RELEASE_CHECK_INTERVAL - RELEASE_CHECK_SLACK:
                    return CheckResult(RESULT_SKIPPED)
            # Stamped and stored before the request: a check that fails has spent its turn.
            self.checked = now
            await self._async_save()
            result = await self._async_fetch_and_judge()
            self.check_error = result.error
            await self._async_save()
        async_dispatcher_send(self.hass, SIGNAL_RELEASE_INDEX)
        return result

    async def _async_fetch_and_judge(self) -> CheckResult:
        """Fetch the pair and judge it - reading it a second time before calling it forged.

        The index and its signature are two requests, and a publication can land between
        them: a new index with the old signature, or the other way round. That pair does
        not verify, and it is no forgery. So a pair whose signature does not verify is read
        once more, fresh; only a second failure is judged - and even then the household is
        told that the list could not be verified, not that it was forged (the log keeps the
        warning, for whoever looks).
        """
        session = async_get_clientsession(self.hass)
        headers = {"If-None-Match": self.etag} if self.etag and self.index_raw else {}
        for attempt in (1, 2):
            try:
                status, index_raw, etag = await _async_get(
                    session,
                    PLUGIN_INDEX_ORIGIN + PLUGIN_INDEX_FILE,
                    release_index.MAX_INDEX_BYTES,
                    headers,
                )
                if status == 304:
                    self.fetched = dt_util.utcnow()
                    return CheckResult(RESULT_UNCHANGED)
                _, signature_raw, _ = await _async_get(
                    session,
                    PLUGIN_INDEX_ORIGIN + PLUGIN_INDEX_SIGNATURE_FILE,
                    release_index.MAX_SIGNATURE_BYTES,
                    {},
                )
            except _Fetched as error:
                _LOGGER.debug("The plugin release index check failed: %s", error)
                return CheckResult(RESULT_FAILED, error.code)
            except (TimeoutError, aiohttp.ClientError) as error:
                _LOGGER.debug("The plugin release index could not be fetched: %s", error)
                return CheckResult(RESULT_FAILED, ERROR_UNREACHABLE)
            self.fetched = dt_util.utcnow()
            if index_raw == self.index_raw and signature_raw == self.signature_raw:
                # The exact bytes already held: nothing new, and not a failure.
                self.etag = etag
                return CheckResult(RESULT_UNCHANGED)
            if attempt == 1 and _torn(index_raw, signature_raw):
                _LOGGER.debug(
                    "The plugin release index and its signature do not match; reading both "
                    "again in case a publication landed between the two requests"
                )
                headers = {}
                continue
            break
        return await self._async_judge(index_raw, signature_raw, etag)

    async def _async_judge(
        self, index_raw: bytes, signature_raw: bytes, etag: str | None
    ) -> CheckResult:
        keys = release_index.EMBEDDED
        try:
            memory = release_index.memory_for(self.trust, keys, acceptance=False)
        except release_index.BadMemory as error:
            _LOGGER.warning(
                "The stored trust memory of the plugin release index cannot be read, so no "
                "index is judged until it is removed: %s",
                error,
            )
            return CheckResult(RESULT_FAILED, ERROR_BAD_MEMORY)
        try:
            accepted = release_index.accept(index_raw, signature_raw, keys, memory)
        except release_index.Refused as error:
            if error.reason == "replay" and await self._async_take_back(
                index_raw, signature_raw, etag, memory
            ):
                return CheckResult(RESULT_UNCHANGED)
            _LOGGER.warning(
                "The plugin release index was refused (%s): %s", error.reason, error.detail
            )
            return CheckResult(RESULT_FAILED, error.reason)
        previous = self.index
        self.trust = release_index.store(self.trust, keys, accepted.memory, acceptance=False)
        self.index_raw, self.signature_raw = index_raw, signature_raw
        self.index, self.key_id, self.etag = accepted.index, accepted.key.key_id, etag
        await self._async_save()
        await self._async_announce(previous, accepted.index)
        await self._async_publish()
        return CheckResult(RESULT_ACCEPTED)

    async def _async_take_back(
        self,
        index_raw: bytes,
        signature_raw: bytes,
        etag: str | None,
        memory: dict[str, Any],
    ) -> bool:
        """Hold again an index this reader already accepted, when it holds no index at all.

        The case: the stored bytes were damaged or dropped (`_restore`), while the trust
        memory - rightly - still says which serial was accepted. The same genuine index then
        comes back from the origin, and the rule calls its equal serial a replay. It is not a
        replay of anything held; it is the very index the memory remembers. So, and only so -
        nothing held, the signature verified by one of this reader's keys, and a serial
        **equal** to the one the memory holds for that key - the bytes are taken back:
        silently, because nothing new was learned, and without touching the memory. A lower
        serial stays a replay, and so does an equal serial while another index is held (an
        equal serial with other bytes is exactly what the rule refuses).
        """
        if self.index_raw is not None:
            return False
        try:
            index, key = release_index.authenticate(
                index_raw, signature_raw, release_index.EMBEDDED
            )
        except release_index.Refused:
            return False
        if memory["serials"].get(key.key_id) != index["serial"]:
            return False
        self.index_raw, self.signature_raw = index_raw, signature_raw
        self.index, self.key_id, self.etag = index, key.key_id, etag
        await self._async_save()
        _LOGGER.info(
            "Took back the plugin release index serial %s, which this Home Assistant had "
            "accepted before and no longer held",
            index["serial"],
        )
        await self._async_publish()
        return True

    async def async_republish(self) -> None:
        """Put the held index on the broker again, retained - verified first.

        On every setup and every reconnect to the broker: a broker restarted without
        persistence, a publish that failed while it was down, or a broker client that
        overwrote the retained topic would otherwise leave receivers without internet on a
        wrong or missing index until the next serial. A receiver sees the same serial as
        nothing new. The bytes are verified again before they go out, because what is
        published in this integration's name must be what it would accept today.
        """
        await self.async_load()
        if self.index_raw is None or self.signature_raw is None:
            return
        try:
            release_index.authenticate(
                self.index_raw, self.signature_raw, release_index.EMBEDDED
            )
        except release_index.Refused:
            return
        await self._async_publish()

    async def _async_announce(
        self, previous: dict[str, Any] | None, index: dict[str, Any]
    ) -> None:
        """Say that a new index was accepted: a warning, and a persistent notification."""
        added = sorted(
            _versions(index) - _versions(previous), key=release_index.version_key
        )
        withdrawn = sorted(
            _withdrawn(index) - _withdrawn(previous), key=release_index.version_key
        )
        _LOGGER.warning(
            "Accepted a new signed plugin release index: serial %s, key %s, versions added: "
            "%s, withdrawn: %s, floor %s. If no plugin release or withdrawal was expected, "
            "read SECURITY.md before installing anything",
            index["serial"],
            index["key_id"],
            ", ".join(added) or "none",
            ", ".join(withdrawn) or "none",
            index["floor"],
        )
        texts = await async_get_translations(
            self.hass, self.hass.config.language, "common", {DOMAIN}
        )
        prefix = f"component.{DOMAIN}.common."
        none = texts.get(prefix + "release_index_none") or "-"
        title = texts.get(prefix + "release_index_notification_title") or (
            "Plugin release index"
        )
        template = texts.get(prefix + "release_index_notification_message") or (
            "serial {serial}, key {key_id}, added {added}, withdrawn {withdrawn}, floor {floor}"
        )
        placeholders = {
            "serial": str(index["serial"]),
            "key_id": index["key_id"],
            "added": ", ".join(added) or none,
            "withdrawn": ", ".join(withdrawn) or none,
            "floor": index["floor"],
        }
        try:
            message = template.format(**placeholders)
        except (IndexError, KeyError):
            message = (
                "serial {serial}, key {key_id}, added {added}, withdrawn {withdrawn}, "
                "floor {floor}"
            ).format(**placeholders)
        persistent_notification.async_create(
            self.hass,
            message,
            title=title,
            notification_id=f"{DOMAIN}_release_index_{index['key_id']}_{index['serial']}",
        )

    async def _async_publish(self) -> None:
        """Put the accepted index on the broker, retained, for receivers without internet."""
        if self.index_raw is None or self.signature_raw is None:
            return
        payload = json.dumps(
            {"index": _b64(self.index_raw), "sig": _b64(self.signature_raw)},
            separators=(",", ":"),
        )
        try:
            await mqtt.async_publish(
                self.hass, TOPIC_RELEASE_INDEX, payload, qos=1, retain=True
            )
        except HomeAssistantError as error:
            _LOGGER.debug("Could not publish the plugin release index: %s", error)


@callback
def async_release_index_cache(hass: HomeAssistant) -> ReleaseIndexCache:
    """Return the one cache every receiver shares."""
    if (cache := hass.data.get(RELEASE_INDEX_STORAGE_KEY)) is None:
        cache = hass.data[RELEASE_INDEX_STORAGE_KEY] = ReleaseIndexCache(hass)
    return cache

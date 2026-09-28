"""The plugin's own update over MQTT, and the receiver's update transactions as the card shows them.

From the release after 0.3.0 a plugin can install one of its signed releases by itself: an update
helper outside enigma2 downloads it, verifies it against the receiver's own signed index, keeps a
rollback point, runs the package manager and restarts the interface, and puts the previous
version back when the new one does not start (the plugin's `docs/TRANSACTION.md`). Home Assistant
asks for that with `cmd/update` and follows it on the `update` topic. This module is that asking
and following, and the one reading of `update.transaction` the card and its attributes share.

**Which path, and never both** (ADR-0008). A receiver that states the capability `self_update`
together with its box-only permission `update_allowed: true` is updated over MQTT, whatever SSH
credentials are stored; one that does not is updated over SSH when credentials are stored, and not
at all otherwise. An update over MQTT that is refused, fails or is rolled back is reported as it
is, and is **never** retried over SSH: the receiver has said how it wants to be updated, and a
second path behind the first would install over a transaction the receiver may still be undoing.
Only a release the signed index lists goes over MQTT - the receiver verifies the package against
its own copy of that index, so anything else it would refuse - and never an older one: a
downgrade is the options flow's, over SSH, behind a tick box.

**The request.** Home Assistant downloads and verifies the release itself (`release_package`),
serves it on a relay address bound to the receiver's IPv4 address (`relay`), and sends
`{"version", "sha256", "relay": {"url", "expires"}}` - the signed entry's version and digest, so
the receiver needs no internet and installs exactly those bytes. The grant is held for as long as
this transaction is followed, so that a forged `relay_request` for other versions cannot evict it
mid-download. The command goes out only once the broker has confirmed the subscription that will
carry the answer.

**The answer.** A refusal is `last_error` with `cmd: update` and a `reason`; an acceptance is a
new transaction on `update` - another id than the one held before the request, the version asked
for, `started_by: home_assistant` - published as the receiver starts its helper. A retained
payload is what the broker held before the request and is never an answer to it.

**Following it.** The helper bounds itself: the forward path ends within 945 s, a rollback after
it takes at most 409 s, 1353 s in all (TRANSACTION.md section 2.4). Home Assistant follows for
**25 minutes** from the acceptance, which covers that with the clock skew it allows and room for
the copying the measurement leaves out. A transaction still running then is not called a failure:
the card says the update is still running on the receiver, and keeps showing what `update` says.
Success is `result: installed` **and** the receiver reporting the target on `info`, with the
signed commit when the entry names one - the transaction's word alone is not the proof.

**Transactions Home Assistant did not start** - at the television, on the OpenWebif page, or by a
broker client - are shown on the card only while they plausibly run: not `finished`, and started
within the last 25 minutes by the receiver's clock (and no more than a minute ahead of Home
Assistant's). Outside that the attribute says `stale` and the card is idle. A broker client can
publish a fresh fake phase and keep it fresh, and Home Assistant cannot tell - which is why the
rescue, the forced reinstall over SSH, asks the lock on the receiver and never this topic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import json
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    MQTT_UPDATE_ANSWER_TIMEOUT,
    MQTT_UPDATE_FOLLOW,
    MQTT_UPDATE_FUTURE_TOLERANCE,
    MQTT_UPDATE_PROOF_TIMEOUT,
    TOPIC_INFO,
    TOPIC_LAST_ERROR,
    TOPIC_UPDATE,
)
from .relay import RELAY_PATH, RelayError, async_get_relay, async_relay_base, receiver_address
from .release_package import PackageSource

_LOGGER = logging.getLogger(__name__)

# The helper's phases, in the order a transaction passes them (plugin TOPICS.md, `update`).
PHASES = (
    "downloading",
    "verifying",
    "snapshot",
    "installing",
    "restarting",
    "proving",
    "rolling_back",
    "finished",
)
RESULTS = ("installed", "withdrawn_before_restart", "rolled_back", "failed", "interrupted")
# The card has one bar. A rollback has no place on it that would not read as progress, so it
# shows as running without a percentage.
_PERCENT = {
    "downloading": 10,
    "verifying": 20,
    "snapshot": 35,
    "installing": 50,
    "restarting": 65,
    "proving": 85,
}

STARTED_BY_HOME_ASSISTANT = "home_assistant"

# How the card reads a transaction.
STATE_IN_PROGRESS = "in_progress"
STATE_FINISHED = "finished"
STATE_STALE = "stale"

_TRANSACTION_ID = re.compile(r"[0-9a-f]{12}", re.ASCII)
# A reason code is a lowercase word; anything else is not one and is not repeated.
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
# The receiver's `busy` sentence for a lock whose helper has provably stopped names the minutes
# until it expires; it is the one refusal whose number the household needs.
_MINUTES = re.compile(r"about (\d{1,4}) minutes?", re.ASCII)

# Refusals of `cmd/update` (TOPICS.md) and helper reasons (TRANSACTION.md section 7) that read the
# same whichever of the two said them, each under `update_mqtt_<reason>`.
_OWN_SENTENCE = frozenset(
    {
        "not_permitted",
        "no_capability",
        "busy",
        "opkg_busy",
        "recording_unknown",
        "epg_import",
        "cannot_restart",
        "bad_request",
        "unknown_version",
        "withdrawn",
        "below_floor",
        "incompatible",
        "depends",
        "current",
        "checksum",
        "relay",
        "no_space",
        "rate_limited",
        "no_relay",
        "clock_skew",
        "unreachable",
        "snapshot_failed",
        "opkg_failed",
        "manifest",
        "internal_error",
        "restore_failed",
        "restore_incomplete",
        "not_stopped",
        "interface_not_started",
        "time_limit",
        "interrupted",
    }
)
# Sentences the card had before this path, which say exactly the same.
_SHARED = {
    "standby": "update_standby",
    "downgrade": "update_downgrade",
    "recording": "update_mqtt_recording",
    "recording_due": "update_mqtt_recording",
    "bad_package": "update_download_failed",
    "download": "update_mqtt_download",
}
# A withdraw at the restart says what held it; a rollback says why the previous version is back.
_WITHDRAWN = {
    "question": "update_mqtt_question",
    "retraction": "update_mqtt_retraction",
    "standby": "update_mqtt_withdrawn_standby",
    "recording": "update_mqtt_withdrawn_recording",
    "epg_import": "update_mqtt_withdrawn_epg_import",
}
_ROLLED_BACK = {
    "not_started": "update_mqtt_not_started",
    "time_limit": "update_mqtt_rolled_back_time_limit",
    "interrupted": "update_mqtt_rolled_back_interrupted",
    "drill": "update_mqtt_drill",
}


def _text(value: Any, limit: int = 256) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _whole(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_update(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """`update.transaction` as this integration reads it, or None.

    Only what the contract defines, each member checked: a transaction without a valid id is
    not one, a phase or a result from outside the contract's words is none, and a `result`
    counts only with `finished`. Free text is cut, never trusted to be short.
    """
    if not isinstance(payload, dict):
        return None
    source = payload.get("transaction")
    if not isinstance(source, dict):
        return None
    ident = source.get("id")
    if not isinstance(ident, str) or not _TRANSACTION_ID.fullmatch(ident):
        return None
    phase = source.get("phase") if source.get("phase") in PHASES else None
    result = source.get("result") if source.get("result") in RESULTS else None
    if phase != "finished":
        result = None
    reason = source.get("reason")
    return {
        "id": ident,
        "started_by": _text(source.get("started_by"), 32),
        "target": _text(source.get("target"), 64),
        "from": _text(source.get("from"), 64),
        "phase": phase,
        "started": _whole(source.get("started")),
        "finished": _whole(source.get("finished")),
        "result": result,
        "reason": (
            reason
            if result not in (None, "installed")
            and isinstance(reason, str)
            and _CODE.fullmatch(reason)
            else None
        ),
        "error": _text(source.get("error"), 512),
    }


def transaction_state(transaction: dict[str, Any] | None, now: float) -> str | None:
    """`in_progress`, `finished` or `stale` - or None without a transaction.

    In progress only while not finished and started within the last 25 minutes, and at most a
    minute ahead of this clock. The window is the helper's own bound with room to spare, so a
    real transaction is never cut short by it, and a leftover or forged phase stops holding the
    card once it is over.
    """
    if transaction is None:
        return None
    if transaction["phase"] == "finished":
        return STATE_FINISHED
    started = transaction["started"]
    if (
        started is None
        or transaction["phase"] is None
        or started < now - MQTT_UPDATE_FOLLOW
        or started > now + MQTT_UPDATE_FUTURE_TOLERANCE
    ):
        return STATE_STALE
    return STATE_IN_PROGRESS


def window_edge(transaction: dict[str, Any] | None, now: float) -> float | None:
    """When `transaction_state` next changes by the clock alone, in epoch seconds, or None."""
    if transaction is None or transaction["phase"] in (None, "finished"):
        return None
    started = transaction["started"]
    if started is None:
        return None
    opens = started - MQTT_UPDATE_FUTURE_TOLERANCE
    closes = started + MQTT_UPDATE_FOLLOW
    if now < opens:
        return opens
    if now < closes:
        return closes
    return None


def phase_percentage(phase: str | None) -> int | None:
    """The card's percentage for a helper phase; None where a bar would mislead."""
    return _PERCENT.get(phase) if phase is not None else None


def refusal_sentence(reason: Any, error: Any) -> tuple[str, dict[str, str]]:
    """The translation key, and its extra placeholders, of a refusal of `cmd/update`."""
    if reason == "busy" and isinstance(error, str) and (found := _MINUTES.search(error)):
        return "update_busy_stalled", {"minutes": found.group(1)}
    if isinstance(reason, str):
        if (shared := _SHARED.get(reason)) is not None:
            return shared, {}
        if reason in _OWN_SENTENCE:
            return f"update_mqtt_{reason}", {}
    return "update_mqtt_refused", {"error": _text(error, 512) or str(reason or "")}


def end_sentence(transaction: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
    """The translation key of how a finished transaction ended, or None for `installed`.

    The same `reason` means different things with different results - `time_limit` with
    `failed` changed nothing, with `rolled_back` it was undone - so the result is read first.
    """
    result, reason = transaction["result"], transaction["reason"]
    if result == "installed":
        return None
    if result == "withdrawn_before_restart":
        return _WITHDRAWN.get(reason or "", "update_mqtt_withdrawn_before_restart"), {}
    if result == "rolled_back":
        return _ROLLED_BACK.get(reason or "", "update_mqtt_rolled_back"), {}
    if result == "interrupted":
        return "update_mqtt_interrupted", {}
    if reason in _OWN_SENTENCE or reason in _SHARED:
        return refusal_sentence(reason, transaction["error"])
    return "update_mqtt_failed", {
        "error": transaction["error"] or str(reason or result or "")
    }


def sentence_placeholders(
    transaction: dict[str, Any] | None, version: str | None, extra: dict[str, str]
) -> dict[str, str]:
    """Every placeholder any of these sentences uses, so none of them can be missing."""
    return {
        "version": str(version or (transaction or {}).get("target") or "?"),
        "from_version": str((transaction or {}).get("from") or "?"),
        "minutes": "?",
        "url": "?",
        "error": "",
        **extra,
    }


def _error(key: str, placeholders: dict[str, str]) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


class _Watch:
    """One request's answer and end, read off the box as its topics arrive.

    Registered before the command is published, so an answer that arrives at once is not missed;
    what came before is told apart by the box's own counters and the transaction id.
    """

    def __init__(self, box: Any, version: str, before_id: str | None) -> None:
        self.box = box
        self.version = version
        self.before_id = before_id
        self.ident: str | None = None
        self.transaction: dict[str, Any] | None = None
        self.refusal: tuple[Any, Any] | None = None
        self.phase_callback: Callable[[str | None], None] | None = None
        self.changed = asyncio.Event()
        self._seen = {
            TOPIC_UPDATE: box.updates.get(TOPIC_UPDATE, 0),
            TOPIC_LAST_ERROR: box.updates.get(TOPIC_LAST_ERROR, 0),
        }
        self._remove = box.async_add_listener(
            self._updated, (TOPIC_UPDATE, TOPIC_LAST_ERROR, TOPIC_INFO)
        )

    def _new(self, topic: str) -> bool:
        count = self.box.updates.get(topic, 0)
        if count == self._seen[topic]:
            return False
        self._seen[topic] = count
        return True

    @callback
    def _updated(self) -> None:
        box = self.box
        state = box.state
        if self._new(TOPIC_LAST_ERROR) and self.ident is None and not state.last_error_retained:
            error = state.last_error
            if isinstance(error, dict) and error.get("cmd") == "update":
                self.refusal = (error.get("reason"), error.get("error"))
        if self._new(TOPIC_UPDATE):
            transaction = parse_update(state.update)
            if transaction is not None:
                if self.ident is None:
                    # The acceptance: a new transaction for this version, started by a request
                    # carrying Home Assistant's address - never the broker's replay.
                    if (
                        not state.update_retained
                        and transaction["id"] != self.before_id
                        and transaction["target"] == self.version
                        and transaction["started_by"] == STARTED_BY_HOME_ASSISTANT
                    ):
                        self.ident = transaction["id"]
                if transaction["id"] == self.ident:
                    self.transaction = transaction
                    if self.phase_callback is not None:
                        self.phase_callback(transaction["phase"])
        self.changed.set()

    def stop(self) -> None:
        self._remove()

    async def wait(self, until: Callable[[], bool], timeout: float) -> bool:
        """Wait until `until()` holds or the time is up; whether it holds."""
        with suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                while not until():
                    self.changed.clear()
                    await self.changed.wait()
        return until()


def _proven(box: Any, version: str, commit: str | None) -> bool:
    """Whether the receiver reports the target on `info`, with the signed commit if there is one."""
    info = box.info
    if info.get("plugin") != version:
        return False
    if not commit:
        return True
    build = info.get("build")
    return isinstance(build, dict) and build.get("commit") == commit


async def async_install_over_mqtt(
    hass: HomeAssistant,
    box: Any,
    receiver: str,
    package: PackageSource,
    on_phase: Callable[[str | None], None],
) -> None:
    """Install the verified release `package` with `cmd/update`, and follow it to its end.

    Returns when the receiver runs the release; raises HomeAssistantError, in the household's
    words, for everything else - including "still running" at the 25-minute bound, which is not a
    failure. Never falls back to SSH.
    """
    version = package.version
    placeholders = sentence_placeholders(None, version, {})
    address = receiver_address(box.ip_address)
    if address is None:
        raise RelayError("relay_no_ipv4")
    base = await async_relay_base(hass, address)
    relay = async_get_relay(hass)
    fresh = relay.reusable(box.node_id, version, package.sha256, address) is None
    grant = relay.grant(box.node_id, str(address), package, receiver=receiver)
    grant.base = base
    grant.held += 1
    url = f"{base}{RELAY_PATH}{grant.token}"
    watch = _Watch(box, version, (parse_update(box.state.update) or {}).get("id"))
    try:
        _LOGGER.info(
            "Plugin update over MQTT: asking %s to install %s from %s%s%s...",
            box.node_id,
            version,
            base,
            RELAY_PATH,
            grant.token[:6],
        )
        await box.async_publish_cmd(
            "update",
            json.dumps(
                {
                    "version": version,
                    "sha256": package.sha256,
                    "relay": {"url": url, "expires": int(grant.expires)},
                },
                separators=(",", ":"),
            ),
        )
        answered = await watch.wait(
            lambda: watch.ident is not None or watch.refusal is not None,
            MQTT_UPDATE_ANSWER_TIMEOUT,
        )
        if not answered or watch.ident is None:
            if watch.refusal is not None:
                reason, error = watch.refusal
                key, extra = refusal_sentence(reason, error)
                _LOGGER.warning(
                    "Plugin update over MQTT: %s refused %s (%s): %s",
                    box.node_id,
                    version,
                    reason,
                    error,
                )
                raise _error(key, sentence_placeholders(None, version, extra))
            raise _error("update_mqtt_no_answer", placeholders)
        _LOGGER.info(
            "Plugin update over MQTT: %s accepted %s as transaction %s",
            box.node_id,
            version,
            watch.ident,
        )
        watch.phase_callback = on_phase
        on_phase((watch.transaction or {}).get("phase"))
        ended = await watch.wait(
            lambda: watch.transaction is not None and watch.transaction["phase"] == "finished",
            MQTT_UPDATE_FOLLOW,
        )
        if not ended:
            _LOGGER.warning(
                "Plugin update over MQTT: transaction %s on %s has not finished after %d "
                "minutes; no longer waiting for it",
                watch.ident,
                box.node_id,
                MQTT_UPDATE_FOLLOW // 60,
            )
            raise _error("update_mqtt_still_running", placeholders)
        transaction = watch.transaction
        assert transaction is not None
        if (end := end_sentence(transaction)) is not None:
            key, extra = end
            if key == "update_mqtt_download":
                extra = {**extra, "url": base}
            _LOGGER.warning(
                "Plugin update over MQTT: transaction %s on %s ended %s (%s): %s",
                watch.ident,
                box.node_id,
                transaction["result"],
                transaction["reason"],
                transaction["error"],
            )
            raise _error(key, sentence_placeholders(transaction, version, extra))
        if not await watch.wait(
            lambda: _proven(box, version, package.commit), MQTT_UPDATE_PROOF_TIMEOUT
        ):
            running = box.info.get("plugin")
            _LOGGER.warning(
                "Plugin update over MQTT: transaction %s on %s says installed, but the "
                "receiver reports %s (build %s), not %s (%s)",
                watch.ident,
                box.node_id,
                running,
                (box.info.get("build") or {}).get("commit")
                if isinstance(box.info.get("build"), dict)
                else None,
                version,
                package.commit,
            )
            raise _error(
                "update_mqtt_unproven",
                sentence_placeholders(transaction, version, {"error": str(running or "?")}),
            )
        _LOGGER.info("Plugin update over MQTT: %s runs %s", box.node_id, version)
    finally:
        watch.stop()
        grant.held -= 1
        if fresh and grant.served == 0:
            # Offered and never fetched - a refusal, or an end before the download. The card
            # has said why; a notification ten minutes later that it was "not downloaded"
            # would only repeat it worse.
            relay.drop(grant)

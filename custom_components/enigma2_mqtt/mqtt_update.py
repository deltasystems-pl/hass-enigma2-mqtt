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

**The answer.** A refusal is `last_error` with `cmd: update` and a `reason` - not the end of a
transaction, which the plugin repeats there too (`is_refusal`); an acceptance is a
new transaction on `update` - another id than the one held before the request, the version asked
for, `started_by: home_assistant` - published as the receiver starts its helper. A retained
payload is what the broker held before the request and is never an answer to it.

**Following it.** The helper bounds itself: the forward path ends within 945 s, a rollback after
it takes at most 409 s, 1353 s in all (TRANSACTION.md section 2.4). Home Assistant follows for
**25 minutes** from the acceptance, which covers that with the clock skew it allows and room for
the copying the measurement leaves out. A transaction still running then is not called a failure:
the card says the update is still running on the receiver, and keeps showing what `update` says.
Success is `result: installed` **and** the receiver reporting the target on `info`, with the
signed commit when the entry names one - the transaction's word alone is not the proof. An
`interrupted` end does not say by its result whether the package manager had run, so it is said by
the evidence there is: the files may be a mix when the receiver's words say so or a phase from
`installing` on was seen (`reached`); nothing changed only when the receiver's own sentence says
the package manager never ran; otherwise Home Assistant says it cannot tell. Silence after the
request keeps the relay address for a late start. When the card that asked goes away - its entry
reloaded - the call ends at once with a sentence that says so, and the grant stays for its ten
minutes: the receiver goes on regardless.

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
    MQTT_UPDATE_END_WAIT,
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
# From `installing` on the package manager may have touched the plugin's files (the plugin's own
# `FILES_PHASES`). Seeing only earlier phases proves nothing: the plugin polls its helper once a
# second, so `installing` - and the package manager's start - can fall between two polls.
FILES_PHASES = ("installing", "restarting", "proving", "rolling_back")
# The plugin's words for an `interrupted` whose helper stopped once the package manager had
# started (`selfupdate._helper_died`); the end's own sentence carries them, the repair does not.
_PACKAGE_MANAGER_STARTED = "after the package manager had started"
# Every sentence in which the plugin says its files may no longer be whole names this repair; for
# a helper that stopped part-way it is appended to the end's repeat on `last_error`.
_REINSTALL_WORDS = "install the plugin again"
# The one `interrupted` whose own sentence says the package manager never ran: the helper checks
# that the interface which asked still runs only until it starts the package manager. A signal or
# the lock taken can come after it as well as before, and a helper that stopped or a receiver that
# lost power is judged by a marker that exists only from `installing` on.
_NOTHING_CHANGED = (
    "the update was interrupted: the receiver's interface restarted before the update was "
    "installed"
)
# How long after a transaction's `finished` the plugin may still stamp its repeat of that end on
# `last_error`: at once as a rule, but after a reload only once the fresh session is connected,
# and after an interface restart only once the new one has read the helper's end. A sentence
# stamped later is not that end, even in the same words - a refusal shares the helper's sentence
# for `busy`, `opkg_busy`, `checksum`, `no_space` and others.
_END_REPEAT_SECONDS = 120
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
# Reasons that only ever end a transaction (TRANSACTION.md section 7) and never refuse
# `cmd/update` (TOPICS.md): on `last_error` they are an end the plugin repeats there, not a
# refusal. Every other reason - a new one included, since the list is open - may be a refusal.
_END_ONLY = frozenset(
    {
        "not_started",
        "question",
        "retraction",
        "drill",
        "time_limit",
        "interrupted",
        "unreachable",
        "download",
        "bad_package",
        "snapshot_failed",
        "opkg_failed",
        "manifest",
        "restore_failed",
        "restore_incomplete",
        "not_stopped",
        "interface_not_started",
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


def reached(
    previous: tuple[str, str | None] | None, transaction: dict[str, Any] | None
) -> tuple[str, str | None] | None:
    """The furthest phase seen of a transaction, as `(id, phase)`, after `transaction` arrived.

    `update.transaction` says where a transaction is, never where it has been, and its end does
    not say whether the package manager had run. What was seen on the way is the one record of
    that Home Assistant has: a new id starts it again, and `finished` or an unknown phase adds
    nothing.
    """
    if transaction is None:
        return previous
    phase = transaction["phase"]
    seen = phase if phase in PHASES and phase != "finished" else None
    if previous is None or previous[0] != transaction["id"]:
        return transaction["id"], seen
    before = previous[1]
    if seen is None or (before is not None and PHASES.index(before) >= PHASES.index(seen)):
        return previous
    return transaction["id"], seen


def _interrupted(
    transaction: dict[str, Any], furthest: str | None, repair_by_ssh: bool, said: str | None
) -> str:
    """How an `interrupted` end is said: the files may be mixed, nothing changed, or unknown.

    `interrupted` comes before the package manager ran and after it - a signal, the lock taken,
    a helper that stopped, a receiver that lost power - and its result does not say which
    (TRANSACTION.md section 7). The files may be a mix on any evidence of it: the end's sentence
    saying the package manager had started, or naming the reinstall; the end's repeat on
    `last_error` (`said`) naming it; a phase from `installing` on seen on the way. Nothing
    changed only when the receiver's own sentence says so, since a phase seen before
    `installing` does not prove that the next one never came. Anything else Home Assistant
    cannot tell, and says so with the repair to use if the receiver asks for it: the forced
    reinstall over SSH when SSH credentials are stored - the button exists only then - and the
    manual installation otherwise.
    """
    repair = "reinstall" if repair_by_ssh else "manual"
    error = transaction["error"] or ""
    if (
        _PACKAGE_MANAGER_STARTED in error
        or _REINSTALL_WORDS in error
        or (said is not None and _REINSTALL_WORDS in said)
        or furthest in FILES_PHASES
    ):
        return f"update_mqtt_interrupted_files_{repair}"
    if error == _NOTHING_CHANGED:
        return "update_mqtt_interrupted"
    return f"update_mqtt_interrupted_unknown_{repair}"


def end_sentence(
    transaction: dict[str, Any],
    furthest: str | None = None,
    *,
    repair_by_ssh: bool = True,
    said: str | None = None,
) -> tuple[str, dict[str, str]] | None:
    """The translation key of how a finished transaction ended, or None for `installed`.

    The same `reason` means different things with different results - `time_limit` with
    `failed` changed nothing, with `rolled_back` it was undone - so the result is read first.
    `furthest` is the furthest phase seen of it (`reached`) and `said` the sentence of its repeat
    on `last_error` (`end_repeated`), which only `interrupted` needs.
    """
    result, reason = transaction["result"], transaction["reason"]
    if result == "installed":
        return None
    if result == "withdrawn_before_restart":
        return _WITHDRAWN.get(reason or "", "update_mqtt_withdrawn_before_restart"), {}
    if result == "rolled_back":
        return _ROLLED_BACK.get(reason or "", "update_mqtt_rolled_back"), {}
    if result == "interrupted":
        return _interrupted(transaction, furthest, repair_by_ssh, said), {}
    if reason in _OWN_SENTENCE or reason in _SHARED:
        return refusal_sentence(reason, transaction["error"])
    return "update_mqtt_failed", {
        "error": transaction["error"] or str(reason or result or "")
    }


def end_repeated(error: Any, transaction: dict[str, Any] | None) -> str | None:
    """The sentence of `transaction`'s end as the plugin repeats it on `last_error`, or None.

    The plugin repeats every end but `installed` there, with the end's reason and its sentence -
    the repair appended when its doors stay closed - stamped as it says it, which is soon after
    the end's `finished` on the same clock. A payload under the end's reason or result, starting
    with its sentence and stamped no later than `_END_REPEAT_SECONDS` after it, is that repeat;
    one without a usable stamp is judged by its words alone.
    """
    if (
        not isinstance(error, dict)
        or error.get("cmd") != "update"
        or transaction is None
        or transaction["phase"] != "finished"
        or transaction["result"] in (None, "installed")
    ):
        return None
    said, text = transaction["error"], error.get("error")
    if not (
        error.get("reason") in (transaction["reason"], transaction["result"])
        and isinstance(said, str)
        and isinstance(text, str)
        and text.startswith(said)
    ):
        return None
    stamped, finished = _whole(error.get("ts")), transaction["finished"]
    if stamped is not None and finished is not None and stamped > finished + _END_REPEAT_SECONDS:
        return None
    return text


def is_refusal(error: Any, transaction: dict[str, Any] | None) -> bool:
    """Whether a `last_error` payload is a refusal of `cmd/update`, and not an end.

    A reason only an end carries is an end, and so is the repeat of the transaction that has
    just finished (`end_repeated`). The rest is a refusal: it came before any transaction
    started. The plugin carries no request id, so a refusal of somebody else's `cmd/update` in
    the same minute - an install started at the television, say - reads as this one's; that is
    rare, and accepted.
    """
    if not isinstance(error, dict) or error.get("cmd") != "update":
        return False
    reason = error.get("reason")
    if isinstance(reason, str) and reason in _END_ONLY:
        return False
    return end_repeated(error, transaction) is None


def sentence_placeholders(
    transaction: dict[str, Any] | None, version: str | None, extra: dict[str, str]
) -> dict[str, str]:
    """Every placeholder any of these sentences uses, so none of them can be missing."""
    return {
        "version": str(version or (transaction or {}).get("target") or "?"),
        "from_version": str((transaction or {}).get("from") or "?"),
        "minutes": "?",
        "url": "?",
        "running": "?",
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

    def __init__(self, box: Any, version: str, before: dict[str, Any] | None) -> None:
        self.box = box
        self.version = version
        self.before_id = before["id"] if before is not None else None
        # A transaction that had ended before the request went out had its end said then: a
        # sentence arriving after the request is not that end again, whatever its words.
        self.before_ended = before is not None and before["phase"] == "finished"
        self.ident: str | None = None
        self.transaction: dict[str, Any] | None = None
        # The furthest phase seen of the accepted transaction (`reached`).
        self.reached: tuple[str, str | None] | None = None
        self.refusal: tuple[Any, Any] | None = None
        # The receiver's last complaint about `cmd/update` after the acceptance: the end's
        # repeat, once it comes (`end_said`); one about another command leaves it.
        self.complaint: Any = None
        self.phase_callback: Callable[[str | None], None] | None = None
        # Set when the card that asked is gone - its entry reloaded or unloaded: this box's
        # topics stop, and what the receiver does next is the new card's to show.
        self.abandoned = False
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
        if self._new(TOPIC_LAST_ERROR) and not state.last_error_retained:
            error = state.last_error
            if self.ident is not None:
                if isinstance(error, dict) and error.get("cmd") == "update":
                    self.complaint = error
            else:
                held = parse_update(state.update)
                if self.before_ended and held is not None and held["id"] == self.before_id:
                    held = None
                if is_refusal(error, held):
                    assert isinstance(error, dict)
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
                    self.reached = reached(self.reached, transaction)
                    if self.phase_callback is not None:
                        self.phase_callback(transaction["phase"])
        self.changed.set()

    def end_said(self) -> str | None:
        """The followed transaction's end as repeated on `last_error`, once it is there."""
        return end_repeated(self.complaint, self.transaction)

    @callback
    def abandon(self) -> None:
        """Stop waiting: the card that asked is gone."""
        self.abandoned = True
        self.changed.set()

    def stop(self) -> None:
        self._remove()

    async def wait(self, until: Callable[[], bool], timeout: float) -> bool:
        """Wait until `until()` holds, the time is up or the follow is abandoned; whether it
        holds."""
        with suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                while not until() and not self.abandoned:
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
    *,
    repair_by_ssh: bool = True,
    on_follow: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Install the verified release `package` with `cmd/update`, and follow it to its end.

    Returns when the receiver runs the release; raises HomeAssistantError, in the household's
    words, for everything else - including "still running" at the 25-minute bound, which is not a
    failure. Never falls back to SSH. `repair_by_ssh` says whether the forced reinstall is there
    to name when the files may be a mix; `on_follow` is handed the function that abandons the
    follow, for the card to call when it goes away.
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
    watch = _Watch(box, version, parse_update(box.state.update))
    if on_follow is not None:
        on_follow(watch.abandon)
    published = False
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
        published = True
        answered = await watch.wait(
            lambda: watch.ident is not None or watch.refusal is not None,
            MQTT_UPDATE_ANSWER_TIMEOUT,
        )
        _raise_if_abandoned(watch, box, placeholders)
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
            # Silence is not a refusal: the receiver may still start, late, and download from
            # the address it was given - which stays until its own ten minutes are over.
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
        _raise_if_abandoned(watch, box, placeholders)
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
        furthest = watch.reached[1] if watch.reached is not None else None
        end = end_sentence(
            transaction, furthest, repair_by_ssh=repair_by_ssh, said=watch.end_said()
        )
        if end is not None and end[0].startswith("update_mqtt_interrupted_unknown_"):
            # The plugin says the end again on `last_error` right after `update`, with the
            # repair appended when its doors stay closed: wait a moment for what it adds.
            if await watch.wait(lambda: watch.end_said() is not None, MQTT_UPDATE_END_WAIT):
                end = end_sentence(
                    transaction, furthest, repair_by_ssh=repair_by_ssh, said=watch.end_said()
                )
            _raise_if_abandoned(watch, box, placeholders)
        if end is not None:
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
        proven = await watch.wait(
            lambda: _proven(box, version, package.commit), MQTT_UPDATE_PROOF_TIMEOUT
        )
        _raise_if_abandoned(watch, box, placeholders)
        if not proven:
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
                sentence_placeholders(transaction, version, {"running": str(running or "?")}),
            )
        _LOGGER.info("Plugin update over MQTT: %s runs %s", box.node_id, version)
    finally:
        watch.stop()
        # An abandoned follow leaves the grant held: nobody here follows the transaction any
        # more, but the receiver may be downloading from it, so it survives the entry's unload
        # (which spares a held grant) and goes at its own expiry.
        if not watch.abandoned:
            grant.held -= 1
            ended_early = (
                watch.transaction is not None and watch.transaction["phase"] == "finished"
            )
            if (
                fresh
                and grant.served == 0
                and (not published or watch.refusal is not None or ended_early)
            ):
                # Offered and never fetched, and never going to be: a refusal, or an end before
                # the download. The card has said why; a notification ten minutes later that it
                # was "not downloaded" would only repeat it worse. Silence and a follow that ran
                # out keep it: the receiver may still come for it.
                relay.drop(grant)


def _raise_if_abandoned(watch: _Watch, box: Any, placeholders: dict[str, str]) -> None:
    """End the call truthfully when the card that asked went away mid-follow."""
    if watch.abandoned:
        _LOGGER.info(
            "Plugin update over MQTT: the card of %s went away (its entry was reloaded or "
            "unloaded); the receiver's own transaction is shown by the card that replaces it",
            box.node_id,
        )
        raise _error("update_mqtt_reloaded", placeholders)

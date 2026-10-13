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
the evidence there is: the files may be a mix when the receiver's words say so; nothing changed,
or the files are whole, when the helper's own sentence says so (the interface restarted before
the package manager ran, a signal, the lock taken); else a mix when a phase from `installing` on
was seen (`reached`); otherwise Home Assistant says it cannot tell. Silence after the
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

from .box import refusal_of_another
from .const import (
    MQTT_UPDATE_ANSWER_TIMEOUT,
    MQTT_UPDATE_END_WAIT,
    MQTT_UPDATE_FOLLOW,
    MQTT_UPDATE_FUTURE_TOLERANCE,
    MQTT_UPDATE_PROOF_TIMEOUT,
    TOPIC_INFO,
    TOPIC_LAST_ERROR,
    TOPIC_UPDATE,
)
from .errors import service_error
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
# that the interface which asked still runs only until it starts the package manager. A helper
# that stopped or a receiver that lost power is judged by a marker that exists only from
# `installing` on.
_NOTHING_CHANGED = (
    "the update was interrupted: the receiver's interface restarted before the update was "
    "installed"
)
# Two more `interrupted` ends only the helper itself writes, and neither leaves a mix. A signal
# ends `interrupted` only with the files untouched or put back whole - a restore that did not
# complete ends `failed` instead - and the interface that asked still runs. The lock taken is
# noticed only between steps, so the package manager had not started or had finished: the files
# are the previous version or the new one, whole, and when the new one is on disk under the old
# interface the receiver asks for its interface to be restarted, not for a reinstall. A phase
# seen on the way does not change either, so these are said by their own words - exactly as the
# helper writes them - unless the receiver itself names the reinstall.
_SIGNALLED = re.compile(r"the update was interrupted: signal \d{1,3}", re.ASCII)
_LOCK_TAKEN = "the update was interrupted: the lock was taken"
# How long after a transaction's `finished` the plugin may still stamp its repeat of that end on
# `last_error`: at once as a rule, but after a reload only once the fresh session is connected,
# and after an interface restart only once the new one has read the helper's end. A sentence
# stamped later is not that end, even in the same words - a refusal shares the helper's sentence
# for `busy`, `opkg_busy`, `checksum`, `no_space` and others.
# TODO: measure this on a receiver - the gap between an end's `finished` and its repeat's `ts`
# after a doors-reopen reload, after an R2 end read by the restarted interface, and for
# `interface_not_started`. The two minutes are reasoned from the plugin's code, not measured.
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

# Which proof ended an update over MQTT as installed (`async_install_over_mqtt` returns it):
# `info` naming the target version and the signed entry's build commit, or - for an entry
# that names no commit - the version alone.
PROOF_INFO_BUILD = "info_build"
PROOF_INFO_VERSION = "info_version"

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
        # The receiver's number, and never more than the thirty minutes a lock is
        # honoured for: the sentence is the receiver's text, and its clock may be wrong.
        return "update_busy_stalled", {"minutes": str(min(30, int(found.group(1))))}
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
    """How an `interrupted` end is said: files mixed, nothing changed, whole, or unknown.

    `interrupted` comes before the package manager ran and after it - a signal, the lock taken,
    a helper that stopped, a receiver that lost power - and its result does not say which
    (TRANSACTION.md section 7). The receiver's words come first: the end's sentence saying the
    package manager had started, or naming the reinstall, or the end's repeat on `last_error`
    (`said`) naming it, say the files may be a mix. Then the helper's own sentences that say
    how the files are: nothing changed when the interface restarted before the package manager
    ran; the previous version in place after a signal; whole, one version or the other, after
    the lock was taken - each of these whatever phase was seen, since the helper wrote it
    knowing where it was. Only then the phases: one from `installing` on seen on the way says
    the files may be a mix, and a phase seen before `installing` proves nothing, since the next
    one can fall between two of the plugin's polls. Anything else Home Assistant cannot tell,
    and says so with the repair to use if the receiver asks for it: the forced reinstall over
    SSH when SSH credentials are stored - the button exists only then - and the manual
    installation otherwise.
    """
    repair = "reinstall" if repair_by_ssh else "manual"
    error = transaction["error"] or ""
    if (
        _PACKAGE_MANAGER_STARTED in error
        or _REINSTALL_WORDS in error
        # No end the plugin writes today puts the reinstall on its repeat alone - it is in
        # the end's own sentence whenever it is on the repeat - so this guards a wording to
        # come, not one that exists.
        or (said is not None and _REINSTALL_WORDS in said)
    ):
        return f"update_mqtt_interrupted_files_{repair}"
    if error == _NOTHING_CHANGED:
        return "update_mqtt_interrupted"
    if _SIGNALLED.fullmatch(error):
        return "update_mqtt_interrupted_signal"
    if error == _LOCK_TAKEN:
        return "update_mqtt_interrupted_lock"
    if furthest in FILES_PHASES:
        return f"update_mqtt_interrupted_files_{repair}"
    return f"update_mqtt_interrupted_unknown_{repair}"


# The ends of `interrupted` that say the files are whole, and the one that cannot tell: for
# these the call waits a moment for the end's repeat on `last_error`, whose reinstall words -
# if the receiver ever says them - win (`_interrupted`).
_HEAR_THE_REPEAT = (
    "update_mqtt_interrupted",
    "update_mqtt_interrupted_signal",
    "update_mqtt_interrupted_lock",
    "update_mqtt_interrupted_unknown_reinstall",
    "update_mqtt_interrupted_unknown_manual",
)


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
    on `last_error` (`end_repeated`), which only `interrupted` needs. `repair_by_ssh` says
    whether the forced reinstall is there to name, for the ends that name a reinstall.
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
    if reason in ("restore_failed", "restore_incomplete") and not repair_by_ssh:
        # Both name the forced reinstall, whose button exists only with SSH credentials
        # stored; without them the repair is the manual installation.
        return f"update_mqtt_{reason}_manual", {}
    if reason in _OWN_SENTENCE or reason in _SHARED:
        return refusal_sentence(reason, transaction["error"])
    return "update_mqtt_failed", {
        "error": transaction["error"] or str(reason or result or "")
    }


def end_words(error: Any, transaction: dict[str, Any] | None) -> str | None:
    """The `last_error` payload's sentence when it is in the words of `transaction`'s end.

    Under the end's reason or result, and starting with the end's sentence - the repair the
    plugin appends when its doors stay closed may follow. The words alone: whether it is that
    end's repeat, or a refusal that shares them, `end_repeated` decides by its stamp and the
    card by when it arrived.
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
    return text


def end_repeated(error: Any, transaction: dict[str, Any] | None) -> str | None:
    """The sentence of `transaction`'s end as the plugin repeats it on `last_error`, or None.

    The plugin repeats every end but `installed` there, with the end's reason and its sentence -
    the repair appended when its doors stay closed - stamped as it says it, which is soon after
    the end's `finished` on the same clock. A payload in the end's words (`end_words`) stamped
    no earlier than the transaction started and no later than `_END_REPEAT_SECONDS` after it
    finished is that repeat; one without a usable stamp is judged by its words alone.
    """
    text = end_words(error, transaction)
    if text is None:
        return None
    assert transaction is not None
    stamped = _whole(error.get("ts"))
    if stamped is None:
        return text
    finished, started = transaction["finished"], transaction["started"]
    if finished is not None and stamped > finished + _END_REPEAT_SECONDS:
        return None
    if started is not None and stamped < started:
        return None
    return text


def is_refusal(error: Any, transaction: dict[str, Any] | None) -> bool:
    """Whether a `last_error` payload is a refusal of `cmd/update`, and not an end.

    A reason only an end carries is an end, and so is the repeat of the transaction that has
    just finished (`end_repeated`). The rest is a refusal: it came before any transaction
    started. Whose refusal is not asked here. The follow of a request (`_Watch`) first drops
    a payload that says another command's id; one that says none - from a plugin without
    ids, or for an install started at the television, which has none - still reads as the
    request's own when it arrives in the same minute. That is rare, and accepted.
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


# The refusals of `cmd/update` that the household can act on or wait out: the receiver's
# own setting, the moment, or the version asked for. They are raised as a refusal; the
# rest - a request the receiver could not read, an image that cannot restart its
# interface, a reason from a newer plugin - as a failure, like every end of a transaction.
_CALLER_CAN_ACT = frozenset(
    {
        "not_permitted",
        "no_capability",
        "busy",
        "opkg_busy",
        "standby",
        "recording",
        "recording_due",
        "recording_unknown",
        "epg_import",
        "unknown_version",
        "withdrawn",
        "below_floor",
        "incompatible",
        "depends",
        "downgrade",
        "current",
        "no_space",
        "rate_limited",
    }
)


def _error(
    key: str, placeholders: dict[str, str], *, refused: bool = False
) -> HomeAssistantError:
    return service_error(refused, key, placeholders)


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
        # What the receiver said it ran when it accepted the request, as `_running` reads it.
        self.accepted_as: tuple[Any, Any] | None = None
        # The plugin last heard to speak on `info`, and what its first `update` after that
        # said of the followed transaction: True when it did not carry it, False when it
        # did or when a replay took the order away, None while it has not said one yet
        # (`replaced`).
        self.speaker: tuple[Any, Any] | None = None
        self.disowned: bool | None = None
        self.refusal: tuple[Any, Any] | None = None
        # The receiver's last complaint about `cmd/update` after the acceptance: the end's
        # repeat, once it comes (`end_said`); one about another command leaves it.
        self.complaint: Any = None
        self.phase_callback: Callable[[str | None], None] | None = None
        # Set when the card that asked is gone - its entry reloaded or unloaded: this box's
        # topics stop, and what the receiver does next is the new card's to show.
        self.abandoned = False
        # Whether the request has gone to the broker, and the id it went out with - None
        # for one sent without an envelope. A complaint before it went cannot be its
        # answer, and one that says another id is about another `cmd/update`.
        self.sent = False
        self.command_id: str | None = None
        self.changed = asyncio.Event()
        self._seen = {
            TOPIC_UPDATE: box.updates.get(TOPIC_UPDATE, 0),
            TOPIC_LAST_ERROR: box.updates.get(TOPIC_LAST_ERROR, 0),
            TOPIC_INFO: box.updates.get(TOPIC_INFO, 0),
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
        if (
            self._new(TOPIC_LAST_ERROR)
            and not state.last_error_retained
            and self.sent
            and not refusal_of_another(state.last_error, self.command_id)
        ):
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
        if self._new(TOPIC_INFO):
            running = _running(box)
            # An `info` that names no plugin says nothing of who runs - a retracted one
            # leaves the last payload as it was - and the same plugin saying `info`
            # again, as every connect does and a capability that binds late, is not
            # another one starting.
            if _named(running) and running != self.speaker:
                self.speaker = running
                # A replay is in no order with `update`: who runs is known from it, what
                # that plugin said first is not, and is never taken for "nothing".
                self.disowned = False if state.info_retained else None
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
                        self.accepted_as = self.speaker = _running(box)
                if transaction["id"] == self.ident:
                    self.transaction = transaction
                    self.reached = reached(self.reached, transaction)
                    if self.phase_callback is not None:
                        self.phase_callback(transaction["phase"])
            if self.ident is not None and isinstance(state.update, dict):
                # `update` is published whole, with the transaction in progress or the
                # last one that ended, so the first one a plugin says after its `info`
                # tells whether it has a record of the followed one: a plugin that knows
                # it carries it from its first connect on. A replay is nobody's first
                # word, and takes the order away.
                if state.update_retained:
                    self.disowned = False
                elif self.disowned is None:
                    self.disowned = transaction is None or transaction["id"] != self.ident
        self.changed.set()

    @property
    def finished(self) -> bool:
        """Whether the followed transaction has ended."""
        return self.transaction is not None and self.transaction["phase"] == "finished"

    @property
    def replaced(self) -> bool:
        """Whether the plugin that accepted the request has been replaced under the follow.

        An install this integration did not start does that: the release put over a
        development build of the same number by hand, an install over SSH, the receiver's
        own package manager. The new plugin has no record of the transaction and will
        never report its end, so the follow would otherwise run to its 25-minute bound
        and say "still running" of an update that is not.

        **The rule.** A plugin that starts says `online`, then `info`, then its other
        topics with `update` last, at every connect (the plugin's `Bridge.on_connect`),
        and one that knows the followed transaction - it read the helper's marker before
        its first connect - carries it on `update` from that first word on. So "replaced"
        is that sequence, heard live after the acceptance and in that order:

        1. an `info` that names another plugin - version or build commit - than the one
           heard before it: a new plugin speaks;
        2. the **first** `update` after that `info` does not carry the followed
           transaction: that plugin has no record of it;
        3. and that plugin is another one than the plugin that accepted the request.

        The verdict rests on the plugin heard at step 1 (`speaker`), not on what `info`
        holds when it is read: an `info` of `{}` after it takes nothing back and adds no
        "?" to the sentence.

        Neither sign alone ends the follow, and neither do both in another order. An
        `update` without the transaction while no new plugin has spoken, or after the new
        plugin's own first `update` carried it, is a leftover or a broker client's - a
        healthy plugin never publishes it - and changes nothing, however `info` changes
        afterwards; `info` changes in every update that works, while `update` still
        carries the transaction. A retained payload is the broker's replay, never a
        plugin speaking now: a replayed `info` says who runs but not what that plugin
        said first, and a replayed `update` is nobody's first word, so after either
        nothing counts until yet another plugin speaks. An `info` of `{}` or without a
        plugin names nobody. `availability` is not asked: the box reports only its
        changes, and a plugin that reconnects says `online` as a new one does.

        What this cannot tell is followed to the bound as before: a replacement by the
        very build that accepted the request, and one heard only as a replay - after Home
        Assistant's own reconnect to the broker. And it is no defence against a broker
        client that forges the whole sequence, or slips its `update` between a starting
        plugin's `info` and its `update`; the success below still needs `info` to name
        the release that was asked for.
        """
        return (
            self.ident is not None
            and not self.finished
            and self.disowned is True
            and self.speaker != self.accepted_as
        )

    def end_said(self) -> str | None:
        """The followed transaction's end as repeated on `last_error`, once it is there."""
        return end_repeated(self.complaint, self.transaction)

    @callback
    def sent_as(self, command_id: str | None) -> None:
        """Note that the request is being published, and under which id."""
        self.sent = True
        self.command_id = command_id

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


def _running(box: Any) -> tuple[Any, Any]:
    """The plugin the receiver says it runs: `info.plugin` and `info.build.commit`."""
    info = box.info
    build = info.get("build")
    return info.get("plugin"), build.get("commit") if isinstance(build, dict) else None


def _named(running: tuple[Any, Any]) -> bool:
    """Whether `_running` names a plugin at all: an `info` of `{}` names nobody."""
    return isinstance(running[0], str) and bool(running[0])


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
) -> str:
    """Install the verified release `package` with `cmd/update`, and follow it to its end.

    Returns when the receiver runs the release, with the proof that said so (`PROOF_INFO_BUILD`
    or `PROOF_INFO_VERSION`); raises HomeAssistantError, in the household's
    words, for everything else - including "still running" at the 25-minute bound, which is not a
    failure. Never falls back to SSH. `repair_by_ssh` says whether the forced reinstall is there
    to name when the files may be a mix; `on_follow` is handed the function that abandons the
    follow, for the card to call when it goes away.

    A follow whose plugin was replaced under it (`_Watch.replaced`: a new plugin heard to
    start, whose first `update` does not carry the transaction) ends at once: as a success
    when the receiver runs the release that was asked for, whoever installed it, and with a
    sentence that says the update is no longer reported otherwise.
    """
    proof = PROOF_INFO_BUILD if package.commit else PROOF_INFO_VERSION
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
            sent_as=watch.sent_as,
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
                raise _error(
                    key,
                    sentence_placeholders(None, version, extra),
                    refused=reason in _CALLER_CAN_ACT,
                )
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
        ended = await watch.wait(lambda: watch.finished or watch.replaced, MQTT_UPDATE_FOLLOW)
        _raise_if_abandoned(watch, box, placeholders)
        if ended and not watch.finished:
            # The plugin that was heard to start, as the watch recorded it: `info` may
            # have been replaced by one that names nobody since.
            assert watch.speaker is not None
            running, build = watch.speaker
            if running == version and (not package.commit or build == package.commit):
                _LOGGER.info(
                    "Plugin update over MQTT: %s no longer reports transaction %s, and runs "
                    "%s - installed by something other than this update. Proof: %s",
                    box.node_id,
                    watch.ident,
                    version,
                    _PROOF_WORDS[proof],
                )
                return proof
            _LOGGER.warning(
                "Plugin update over MQTT: %s no longer reports transaction %s; its plugin "
                "was replaced while the update was followed, and it runs %s, not %s",
                box.node_id,
                watch.ident,
                running,
                version,
            )
            raise _error(
                "update_mqtt_replaced",
                sentence_placeholders(None, version, {"running": str(running or "?")}),
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
        furthest = watch.reached[1] if watch.reached is not None else None
        # `said` changes the sentence only when the repeat names a reinstall its end does not,
        # which no current plugin writes (`_interrupted`): the wait below is for a wording to
        # come, and costs at most `MQTT_UPDATE_END_WAIT` on an end whose repeat never comes.
        end = end_sentence(
            transaction, furthest, repair_by_ssh=repair_by_ssh, said=watch.end_said()
        )
        if end is not None and end[0] in _HEAR_THE_REPEAT:
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
        _LOGGER.info(
            "Plugin update over MQTT: %s runs %s. Proof: %s",
            box.node_id,
            version,
            _PROOF_WORDS[proof],
        )
        return proof
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


# The proofs in a log line's words.
_PROOF_WORDS = {
    PROOF_INFO_BUILD: "the receiver reports the version and the signed build commit on info",
    PROOF_INFO_VERSION: (
        "the receiver reports the version on info; the signed entry names no build commit"
    ),
}


def _raise_if_abandoned(watch: _Watch, box: Any, placeholders: dict[str, str]) -> None:
    """End the call truthfully when the card that asked went away mid-follow."""
    if watch.abandoned:
        _LOGGER.info(
            "Plugin update over MQTT: the card of %s went away (its entry was reloaded or "
            "unloaded); the receiver's own transaction is shown by the card that replaces it",
            box.node_id,
        )
        raise _error("update_mqtt_reloaded", placeholders)

"""Removing the receiver plugin from Home Assistant: the plugin acts, SSH verifies.

The receiver does the removal itself, in an order only it can keep: it stops every
publisher, retracts every retained topic it owns, says `offline` last, waits for the
broker to acknowledge all of it, disconnects, and only then asks its package manager to
remove it (ADR-0004 in both repositories). An `opkg remove` typed over SSH would skip all
of that and leave the broker serving a snapshot of a receiver that is gone, for ever - so
SSH is never an uninstall path here. Where the entry kept the installer's credentials it is
the *witness*: it reads the guards and the facts before the command, and the absence of
every file after it, and it compares the receiver's own settings block by a hash computed
on the receiver, so that „settings kept" is measured rather than promised.

Without credentials there is still an observable that is not a guess. A receiver that is
switched off leaves `info` and its announcement retained and ends on its last will; one
that uninstalled retracts both and then publishes `offline`. The watch below waits for
exactly that shape, and never for a retained replay, which is what the broker held before
the command and so cannot be its answer.

🔴 **`offline` is not the end of the answer.** The plugin says it before it waits for the
broker's acknowledgements and before it runs opkg, and both can still fail - opkg's lock is
shared with the image's own update check, and a killed opkg reports success. A removal that
fails there comes back: `online`, the snapshot, the announcement, and `last_error` for
`uninstall` with the step that failed. So the watch stays subscribed for the whole window,
through the SSH readbacks as well, and a `last_error` for the command after the shape ends
the flow as a removal that was started and rolled back, in the receiver's words.

Nothing here writes to the receiver over SSH. Every command is a fixed read, and the
settings are never transferred - only their count and their SHA-256, computed where they
live.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import re
from typing import Any

import asyncssh
from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .box import (
    Enigma2Box,
    announcement_topic,
    command_topic,
    decode_payload,
    parse_json_payload,
    state_topic,
)
from .const import (
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    PAYLOAD_OFFLINE,
    PAYLOAD_ONLINE,
    SUBSCRIBE_TIMEOUT,
    TOPIC_AVAILABILITY,
    TOPIC_INFO,
    TOPIC_LAST_ERROR,
    UNINSTALL_TIMEOUT,
)
from .installer import (
    PACKAGE,
    PLUGIN_DIR,
    TIMER_GUARD_SECONDS,
    CommandResult,
    Connector,
    InstallerError,
    InstallerErrorCode,
    InstallerSession,
    SshCredentials,
    _async_connect,
    _async_wait_for_enigma,
    _timer_guard,
)

_LOGGER = logging.getLogger(__name__)

# The command's name on the wire, and the `cmd` a refusal of it carries on `last_error`.
COMMAND = "uninstall"

# The receiver's settings block, the part of `/etc/enigma2/settings` that is the plugin's.
# Sorted under the C locale so the hash does not depend on the order enigma2 happened to
# write the lines in, and only the hash leaves the receiver: the block holds the broker
# password. The whole file is not comparable across the restart - enigma2 rewrites its own
# `config.misc.*` lines on every shutdown - so the block is what „settings kept" means.
_SETTINGS_BLOCK = (
    "grep '^config\\.plugins\\.mqttbridge\\.' /etc/enigma2/settings | LC_ALL=C sort "
    "| sha256sum; grep -c '^config\\.plugins\\.mqttbridge\\.' /etc/enigma2/settings "
    "|| true"
)
# Both opkg databases the images this plugin supports use; the helper's `OPKG_DATABASES`.
_OPKG_INFO_FILES = (
    f"ls -d /var/lib/opkg/info/{PACKAGE}.* /usr/lib/opkg/info/{PACKAGE}.* 2>/dev/null"
)
# The plugin's directory, the legacy bytecode beside it, `__pycache__` copies and the
# OpenWebif hook's `MQTTBridge.py` / `MQTTBridge.pyc` in one pass.
_LEFTOVERS = (
    "find /usr/lib/enigma2/python \\( -name 'MQTTBridge*' -o -path '*/MQTTBridge/*' \\)"
)
# OpenWebif is up when its own status page answers. It comes up some seconds after the
# interface's new process exists, and until it does every request is refused - which is
# not the same answer as „that page is not here".
_WEBIF_UP = "wget -q -O /dev/null http://127.0.0.1/api/statusinfo"
# OpenWebif serves the hook for as long as its module is loaded, which a removal of the
# files alone does not end: only the restart does. A 404 is the one answer that says the
# page is gone; every other status is a page still being served. (Measured 2026-09-23 on
# a current build: the page answers 200 from the receiver itself, not 403 - the hook now
# follows OpenWebif's own authentication.)
_HOOK_STATUS = "wget -S -O /dev/null http://127.0.0.1/mqttbridge 2>&1"
_HTTP_STATUS = re.compile(r"HTTP/\d(?:\.\d)?\s+(\d{3})")
# opkg takes `/run/opkg.lock` even to answer `status`, and the image's own update check or
# its plugin browser can be holding it. A refusal for that reason is asked again, briefly.
# Only the two refusals that mean somebody else holds the lock right now and will let go:
# opkg's own `Could not lock <path>: ...` and the image's `Command failed to capture privilege
# lock`. `Could not create lock file ...` - the file or its directory - is a receiver that
# cannot take the lock at all, which asking again does not change; nor is „blocked" a lock.
# Case-sensitive, because these are opkg's fixed strings.
_LOCKED = re.compile(r"Could not lock |failed to capture privilege lock")
LOCK_RETRIES = 5
LOCK_RETRY_SECONDS = 2.0
# How long the readbacks wait for OpenWebif after the interface restarted, and how often
# they look. The interface itself takes eleven to fourteen seconds on the receivers
# measured; OpenWebif is a plugin of it and comes up with it.
WEBIF_READY_TIMEOUT = 60.0
WEBIF_READY_POLL_SECONDS = 3.0
# Named in the unverified sentence. Punctuation and paths, never words, where possible -
# the sentence around them is Polish or German; these few are the exceptions.
RESTART_NOT_SEEN = "restart not seen"
WEBIF_DOWN = "OpenWebif not answering"
# What a transport failure is called there.
SSH_LOST = "ssh (connection lost)"
SSH_TIMED_OUT = "ssh (timed out)"


class UninstallOutcome(StrEnum):
    """How an uninstall ended, named by what the options flow tells the user."""

    # The receiver retracted and said offline, and every SSH readback agreed.
    VERIFIED = "uninstall_verified"
    # The receiver retracted and said offline; SSH could not confirm, and says why.
    NOT_VERIFIED = "uninstall_not_verified"
    # The receiver retracted and said offline; there were no credentials to check with.
    REPORTED = "uninstall_reported"
    # The receiver answered the command on `last_error` before it changed anything.
    REFUSED = "uninstall_refused"
    # The receiver had begun retracting, then came back and said on `last_error` which
    # step failed: the removal was started and put back. Its own sentence says what state
    # the package is in.
    ROLLED_BACK = "uninstall_rolled_back"
    # The receiver retracted and said offline, then came back without saying why.
    INCOMPLETE = "uninstall_incomplete"
    # Nothing in the shape of an uninstall arrived before the timeout.
    NO_ACTION = "uninstall_no_action"


@dataclass(frozen=True, slots=True)
class UninstallResult:
    """The outcome, and the one fact that explains it.

    `detail` is the receiver's own sentence for a refusal or a rollback, and for an
    unverified removal the readbacks that disagreed - named by the command or path they
    read, so it drops into the Polish and German sentences largely unchanged.
    """

    outcome: UninstallOutcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _Before:
    """What the receiver looked like before the command, for the after to compare."""

    pids: set[int]
    settings_count: int
    settings_hash: str


def credentials_from_entry(data: Mapping[str, Any]) -> SshCredentials | None:
    """Return the installer's kept SSH credentials, or None when the entry has none.

    A pinned host key is part of the credential: a password without the identity it was
    confirmed against is never sent anywhere.
    """
    if not all(data.get(key) for key in (CONF_SSH_HOST, CONF_SSH_PASSWORD, CONF_SSH_HOST_KEY)):
        return None
    return SshCredentials(
        host=data[CONF_SSH_HOST],
        port=data.get(CONF_SSH_PORT, 22),
        username=data.get(CONF_SSH_USERNAME) or "root",
        password=data[CONF_SSH_PASSWORD],
        host_key=data[CONF_SSH_HOST_KEY],
    )


async def async_uninstall(
    hass: HomeAssistant,
    box: Enigma2Box,
    credentials: SshCredentials | None,
    *,
    _connector: Connector | None = None,
    timeout: float | None = None,
) -> UninstallResult:
    """Ask the receiver to remove its plugin, and report what can be shown of it.

    Raises `InstallerError` with `RECORDING` or `TIMER_DUE` when SSH shows the receiver is
    recording or about to - before anything is published, because the plugin would refuse
    for the same reason and the restart at the end of a removal would cost the recording.
    Any other failure to read the receiver beforehand, a dropped connection included, only
    costs the verification: the command still goes, and the result says it was not
    verified and why.
    """
    connector = _connector or _async_connect
    timeout = UNINSTALL_TIMEOUT if timeout is None else timeout
    before: _Before | None = None
    unverified = ""
    if credentials is not None:
        try:
            before = await _async_read_before(credentials, connector)
        except InstallerError as err:
            if err.code in (
                InstallerErrorCode.RECORDING,
                InstallerErrorCode.TIMER_DUE,
                InstallerErrorCode.BUSY,
            ):
                raise
            unverified = _check_name(err)
            _LOGGER.warning(
                "Could not read receiver %s over SSH before uninstalling (%s); the "
                "removal goes ahead and will not be verified",
                box.node_id,
                err.detail or err.code.value,
            )

    # In `ha_mode: off` the plugin publishes no announcement, so there is none to retract
    # and the shape is `info` retracted, then `offline`. The mode is the one the receiver
    # reported last, before the command changed anything.
    expect_announcement = box.info.get("ha_mode") != "off"
    watch = await _async_watch_uninstall(
        hass, box.base_topic, box.node_id, expect_announcement=expect_announcement
    )
    loop = hass.loop
    try:
        await watch.async_wait_until_established()
        # From here an emptied topic can be the receiver's answer. A retraction before it
        # is somebody else's - a third client switching `ha_mode` off, a `cmd/reset`, an
        # `availability` emptied by hand - and must not turn a refusal into a rollback.
        # Armed before the publish, not after it: the publish awaits the broker's
        # acknowledgement, and Home Assistant's client dispatches incoming messages
        # synchronously as it reads them, so after a stall of the event loop the
        # acknowledgement and the receiver's first retractions arrive in one read - and
        # are handled before the publish returns, or while it waits out its own timeout.
        watch.arm()
        await mqtt.async_publish(
            hass,
            command_topic(box.base_topic, box.node_id, COMMAND),
            box.node_id,
            qos=1,
            retain=False,
        )
        deadline = loop.time() + timeout
        await asyncio.wait(
            {watch.refused, watch.shape, watch.rolled_back},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # A failure after the retractions had begun is a rollback even when the `offline`
        # never reached us - the connection dropped mid-retraction, say.
        if (result := _rolled_back(watch, box)) is not None:
            return result
        if watch.refused.done():
            refusal = watch.refused.result()
            _LOGGER.warning("Receiver %s refused to uninstall: %s", box.node_id, refusal)
            return UninstallResult(UninstallOutcome.REFUSED, refusal)
        if not watch.shape.done():
            _LOGGER.warning(
                "Receiver %s did not uninstall its plugin within %s s: no retraction of "
                "its topics followed by offline arrived",
                box.node_id,
                timeout,
            )
            return UninstallResult(UninstallOutcome.NO_ACTION)
        _LOGGER.info(
            "Receiver %s retracted its topics and went offline after cmd/uninstall",
            box.node_id,
        )

        if before is None:
            # Nothing will be read back, so the rest of the window is the only witness.
            await asyncio.wait({watch.rolled_back}, timeout=max(0.0, deadline - loop.time()))
            if (result := _rolled_back(watch, box)) is not None:
                return result
            if watch.came_back:
                _LOGGER.warning(
                    "Receiver %s came back after cmd/uninstall without saying why; the "
                    "removal did not complete",
                    box.node_id,
                )
                return UninstallResult(UninstallOutcome.INCOMPLETE)
            if credentials is None:
                return UninstallResult(UninstallOutcome.REPORTED)
            return UninstallResult(UninstallOutcome.NOT_VERIFIED, unverified)

        # The readbacks run while the watch goes on listening: a receiver whose opkg
        # failed after `offline` never restarts, and its own sentence arrives long before
        # the readbacks would give up waiting for a restart.
        assert credentials is not None
        readbacks = loop.create_task(_async_read_after(credentials, connector, before))
        try:
            await asyncio.wait(
                {readbacks, watch.rolled_back}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not readbacks.done():
                readbacks.cancel()
                with suppress(asyncio.CancelledError):
                    await readbacks
        if (result := _rolled_back(watch, box)) is not None:
            return result
        failed = readbacks.result()
    finally:
        watch.cancel()

    if failed:
        _LOGGER.warning(
            "Receiver %s uninstalled its plugin, but these readbacks disagree: %s",
            box.node_id,
            ", ".join(failed),
        )
        return UninstallResult(UninstallOutcome.NOT_VERIFIED, ", ".join(failed))
    return UninstallResult(UninstallOutcome.VERIFIED)


def _rolled_back(watch: _UninstallWatch, box: Enigma2Box) -> UninstallResult | None:
    """Return the rollback, if the receiver came back and said why."""
    if not watch.rolled_back.done():
        return None
    sentence = watch.rolled_back.result()
    _LOGGER.warning(
        "Receiver %s started removing its plugin and put it back: %s", box.node_id, sentence
    )
    return UninstallResult(UninstallOutcome.ROLLED_BACK, sentence)


def _check_name(err: InstallerError) -> str:
    """Name the readback an SSH failure stopped at, for the unverified sentence."""
    if err.code in (
        InstallerErrorCode.SSH_UNAVAILABLE,
        InstallerErrorCode.AUTH_FAILED,
        InstallerErrorCode.HOST_KEY_CHANGED,
    ):
        return err.detail or f"ssh ({err.code.value})"
    return err.detail or err.code.value


async def _run(
    session: InstallerSession, command: str, *, timeout: float = 30
) -> CommandResult:
    """Run one fixed read, turning every transport failure into an `InstallerError`.

    `asyncssh.ConnectionLost` is not an `OSError`, and a failure that is neither reaches
    the flow as „unknown". Every SSH call in this module goes through here, so a dropped
    connection or a timeout is always named, before the command and after it.
    """
    try:
        return await session.run(command, timeout=timeout)
    except TimeoutError as err:
        raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE, SSH_TIMED_OUT) from err
    except (OSError, asyncssh.Error) as err:
        raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE, SSH_LOST) from err


async def _run_ok(session: InstallerSession, command: str, detail: str) -> CommandResult:
    """Run a fixed read that must succeed; `detail` names it when it does not."""
    result = await _run(session, command)
    if result.exit_status:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED, detail)
    return result


async def _async_session(credentials: SshCredentials, connector: Connector):
    """Connect, with a connection error named the way `_run` names it."""
    try:
        return await connector(credentials)
    except InstallerError:
        raise
    except TimeoutError as err:
        raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE, SSH_TIMED_OUT) from err
    except (OSError, asyncssh.Error) as err:
        raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE, SSH_LOST) from err


async def _async_close(session: InstallerSession) -> None:
    """Close a session whose connection may already be gone."""
    with suppress(OSError, TimeoutError, asyncssh.Error):
        await session.close()


async def _async_read_before(
    credentials: SshCredentials, connector: Connector
) -> _Before:
    """Read the guards and the facts the after-readbacks compare against."""
    session = await _async_session(credentials, connector)
    try:
        status = await _run_ok(
            session, "wget -qO- http://127.0.0.1/api/statusinfo", "/api/statusinfo"
        )
        timers = await _run_ok(
            session, "wget -qO- http://127.0.0.1/api/timerlist", "/api/timerlist"
        )
        recording, due = _timer_guard(status.stdout, timers.stdout)
        if recording:
            raise InstallerError(
                InstallerErrorCode.RECORDING,
                "the receiver is recording, and the removal ends in a GUI restart",
            )
        if due:
            raise InstallerError(
                InstallerErrorCode.TIMER_DUE,
                f"{due} recording(s) are due on the receiver within the next "
                f"{TIMER_GUARD_SECONDS // 60} minutes",
            )
        pids = await _async_pids(session)
        installed, busy = await _async_opkg_status(session)
        if busy:
            # opkg's lock held through every retry: the image's own update check, or its
            # plugin browser. The plugin's removal would meet the same lock after it had
            # already retracted everything, and roll back. Refused here, before anything is
            # published, and nothing on the receiver changes.
            raise InstallerError(InstallerErrorCode.BUSY, "opkg lock held")
        if installed is None or f"Package: {PACKAGE}" not in installed.splitlines():
            # A plugin opkg does not know about claims no capability and would refuse;
            # if it did not, there would be nothing opkg could be shown to have removed.
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED, "opkg status")
        count, digest = await _async_settings_block(session)
        return _Before(pids, count, digest)
    finally:
        await _async_close(session)


async def _async_pids(session: InstallerSession) -> set[int]:
    """`pidof enigma2`, read through `_run` so a timeout or a drop is named like any read.

    The installer's own `_async_enigma_pids` reports a timeout as `restart_failed`, a
    code that means nothing before a command that has not been sent; the parsing is the
    same: `pidof` exits 1 and prints nothing when there is no match, and anything that
    is not a list of numbers is not an answer.
    """
    result = await _run(session, "pidof enigma2")
    values = result.stdout.split()
    if result.exit_status not in (0, 1) or any(not value.isdigit() for value in values):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED, "pidof enigma2")
    return {int(value) for value in values}


async def _async_opkg_status(session: InstallerSession) -> tuple[str | None, bool]:
    """Return `opkg status` of the package, and whether opkg's lock stayed held.

    Only a refusal for the lock is asked again - the image's own update check or its
    plugin browser holds it for a while and lets go. Any other failure is an answer,
    given at once: `(None, False)`. A lock held through every retry is `(None, True)`.
    """
    for attempt in range(LOCK_RETRIES + 1):
        result = await _run(session, f"opkg status {PACKAGE}")
        if result.exit_status in (0, 1):
            return result.stdout, False
        if not _LOCKED.search(result.stdout + result.stderr):
            return None, False
        if attempt < LOCK_RETRIES:
            await asyncio.sleep(LOCK_RETRY_SECONDS)
    return None, True


async def _async_wait_for_webif(session: InstallerSession) -> bool:
    """Return whether OpenWebif answered within its bound after the restart."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WEBIF_READY_TIMEOUT
    while True:
        if (await _run(session, _WEBIF_UP)).exit_status == 0:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(WEBIF_READY_POLL_SECONDS)


async def _async_read_after(
    credentials: SshCredentials, connector: Connector, before: _Before
) -> list[str]:
    """Return the readbacks that disagree with a removed plugin; empty means verified.

    The interface restart comes first, because the hook stays loaded and the page stays
    served until it happens. The plugin asks for it last and cannot promise it: the
    image's own restart asks on screen, with no timeout, whenever something is streaming
    or a background job runs. A restart that never came is a readback that failed, and it
    does not hide the others - the package, the files and the settings are read either
    way, and only the hook, which a restart is what unloads, is left out.

    Any SSH failure on the way is a readback that could not be made: it is named and
    ends the list, never raised past the flow as „unknown".
    """
    failed: list[str] = []
    try:
        session = await _async_session(credentials, connector)
    except InstallerError as err:
        return [_check_name(err)]
    try:
        restarted = True
        try:
            await _async_wait_for_enigma(session, before.pids)
        except InstallerError as err:
            if err.code is not InstallerErrorCode.ROLLBACK_RESTART_FAILED:
                raise
            restarted = False
            failed.append(RESTART_NOT_SEEN)
        except asyncssh.Error as err:
            raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE, SSH_LOST) from err
        webif = restarted and await _async_wait_for_webif(session)
        status, _busy = await _async_opkg_status(session)
        if status is None or status.strip():
            failed.append("opkg status")
        if (await _run(session, _OPKG_INFO_FILES)).stdout.strip():
            failed.append("opkg info")
        if (await _run(session, f"test -e {PLUGIN_DIR}")).exit_status != 1:
            failed.append(PLUGIN_DIR)
        leftovers = await _run(session, _LEFTOVERS, timeout=60)
        if leftovers.exit_status != 0 or leftovers.stdout.strip():
            failed.append("find MQTTBridge")
        if restarted and not webif:
            failed.append(WEBIF_DOWN)
        elif webif:
            hook = await _run(session, _HOOK_STATUS)
            statuses = _HTTP_STATUS.findall(hook.stdout + hook.stderr)
            if not statuses:
                failed.append("/mqttbridge ?")
            elif statuses[-1] != "404":
                failed.append(f"/mqttbridge {statuses[-1]}")
        count, digest = await _async_settings_block(session)
        if count != before.settings_count:
            failed.append("config.plugins.mqttbridge (count)")
        if digest != before.settings_hash:
            failed.append("config.plugins.mqttbridge (sha256)")
    except InstallerError as err:
        failed.append(_check_name(err))
    finally:
        await _async_close(session)
    return failed


async def _async_settings_block(session: InstallerSession) -> tuple[int, str]:
    """Return the plugin settings block's line count and hash, computed on the receiver."""
    result = await _run_ok(session, _SETTINGS_BLOCK, "config.plugins.mqttbridge")
    lines = result.stdout.split("\n")
    digest = lines[0].split()[0] if lines and lines[0].split() else ""
    count = lines[1].strip() if len(lines) > 1 else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or not count.isdigit():
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED, "config.plugins.mqttbridge"
        )
    return int(count), digest


@dataclass(slots=True)
class _UninstallWatch:
    """Four live subscriptions, and what they have heard since the command.

    Three futures, each resolved at most once and never by a retained message:

    - `refused`, with the receiver's sentence, for a `last_error` about `uninstall`
      before any retraction arrived - nothing on the receiver had changed;
    - `shape`, when the retractions and then `offline` have arrived;
    - `rolled_back`, with the receiver's sentence, for a `last_error` about `uninstall`
      after any retraction - the plugin's failure path, which reconnects first, and
      which may follow a teardown whose `offline` never reached us.

    `came_back` records `online` or a republished `info` after the shape.
    """

    refused: asyncio.Future[str]
    shape: asyncio.Future[None]
    rolled_back: asyncio.Future[str]
    established: asyncio.Future[None]
    cancel: CALLBACK_TYPE
    seen: dict[str, bool] = field(default_factory=dict)

    def arm(self) -> None:
        """Start counting retractions toward a rollback; the command has been sent."""
        self.seen["armed"] = True

    @property
    def came_back(self) -> bool:
        """Return whether the receiver came back after the removal's shape."""
        return self.seen.get("came_back", False)

    async def async_wait_until_established(self) -> None:
        """Wait until the broker is sending all four topics to us.

        Home Assistant debounces SUBSCRIBE packets by a tenth of a second, and a receiver
        on the same network answers in about that: a command published the moment the
        subscriptions exist in-process can be answered to a broker that is not yet
        forwarding them. Degrades rather than hangs, like every other wait of its kind here.
        """
        try:
            async with asyncio.timeout(SUBSCRIBE_TIMEOUT):
                await self.established
        except TimeoutError:
            _LOGGER.debug("Subscriptions for the uninstall watch were not confirmed")


async def _async_watch_uninstall(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    *,
    expect_announcement: bool = True,
) -> _UninstallWatch:
    """Subscribe to the four topics whose fresh messages make up an uninstall."""
    loop = hass.loop
    refused: asyncio.Future[str] = loop.create_future()
    shape: asyncio.Future[None] = loop.create_future()
    rolled_back: asyncio.Future[str] = loop.create_future()
    established: asyncio.Future[None] = loop.create_future()
    gone = {"info": False, "announcement": not expect_announcement}
    # `retracted`: any fresh empty `info`, announcement or `availability` - the teardown
    # has begun, whatever else did or did not arrive after it.
    seen = {"came_back": False, "retracted": False, "armed": False}
    confirmed = 0

    def _retracted_or_back(key: str, msg: ReceiveMessage) -> None:
        # A retained message is what the broker held before the command; it can never be
        # the receiver's answer to it. A fresh non-empty one after a retraction is the
        # plugin putting everything back, and undoes the retraction - or, once the whole
        # shape has been seen, is the receiver coming back.
        if msg.retain:
            return
        if not msg.payload and seen["armed"]:
            seen["retracted"] = True
        if shape.done():
            if key == "info" and msg.payload:
                seen["came_back"] = True
            return
        if key == "announcement" and not expect_announcement:
            return
        gone[key] = not msg.payload

    @callback
    def _info(msg: ReceiveMessage) -> None:
        _retracted_or_back("info", msg)

    @callback
    def _announcement(msg: ReceiveMessage) -> None:
        _retracted_or_back("announcement", msg)

    @callback
    def _availability(msg: ReceiveMessage) -> None:
        if msg.retain:
            return
        if not msg.payload and seen["armed"]:
            seen["retracted"] = True
        payload = (decode_payload(msg.payload) or "").strip()
        if payload == PAYLOAD_ONLINE and shape.done():
            seen["came_back"] = True
        elif payload == PAYLOAD_OFFLINE and all(gone.values()) and not shape.done():
            if not refused.done():
                shape.set_result(None)
        elif payload == PAYLOAD_ONLINE:
            gone.update(info=False, announcement=not expect_announcement)

    @callback
    def _last_error(msg: ReceiveMessage) -> None:
        if msg.retain:
            return
        error = parse_json_payload(msg.payload)
        if error is None or error.get("cmd") != COMMAND:
            return
        sentence = str(error.get("error") or "").strip() or COMMAND
        # After any retraction the removal had begun, so its failure is a rollback -
        # even when the `offline` that completes the shape never reached us. With none,
        # nothing changed on the receiver, and it is a refusal.
        if shape.done() or seen["retracted"]:
            if not rolled_back.done():
                rolled_back.set_result(sentence)
        elif not refused.done():
            refused.set_result(sentence)

    @callback
    def _subscribed() -> None:
        nonlocal confirmed
        confirmed += 1
        if confirmed == 4 and not established.done():
            established.set_result(None)

    subscriptions = (
        (state_topic(base_topic, node_id, TOPIC_INFO), _info),
        (announcement_topic(node_id), _announcement),
        (state_topic(base_topic, node_id, TOPIC_AVAILABILITY), _availability),
        (state_topic(base_topic, node_id, TOPIC_LAST_ERROR), _last_error),
    )
    cancels: list[CALLBACK_TYPE] = []

    @callback
    def _cancel() -> None:
        while cancels:
            cancels.pop()()
        for future in (refused, shape, rolled_back, established):
            if not future.done():
                future.cancel()

    try:
        # Each tracker is registered before its subscription, so a subscription that
        # completes at once cannot land between the two calls and go unnoticed.
        for topic, handler in subscriptions:
            cancels.append(
                mqtt.async_on_subscribe_done(hass, topic, mqtt.DEFAULT_QOS, _subscribed)
            )
            cancels.append(await mqtt.async_subscribe(hass, topic, handler))
    except BaseException:
        _cancel()
        raise
    return _UninstallWatch(
        refused=refused,
        shape=shape,
        rolled_back=rolled_back,
        established=established,
        cancel=_cancel,
        seen=seen,
    )

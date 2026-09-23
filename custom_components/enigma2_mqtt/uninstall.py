"""Removing the receiver plugin from Home Assistant: the plugin acts, SSH verifies.

The receiver does the removal itself, in an order only it can keep: it stops every
publisher, retracts every retained topic it owns, says `offline` last, waits for the
broker to acknowledge all of it, disconnects, and only then asks its package manager to
remove it (ADR-0004 in both repositories). An `opkg remove` typed over SSH would skip all
of that and leave the broker serving a snapshot of a receiver that is gone, for ever — so
SSH is never an uninstall path here. Where the entry kept the installer's credentials it is
the *witness*: it reads the guards and the facts before the command, and the absence of
every file after it, and it compares the receiver's own settings block by a hash computed
on the receiver, so that „settings kept" is measured rather than promised.

Without credentials there is still an observable that is not a guess. A receiver that is
switched off leaves `info` and its announcement retained and ends on its last will; one
that uninstalled retracts both and then publishes `offline`. The watch below waits for
exactly that shape, and never for a retained replay, which is what the broker held before
the command and so cannot be its answer.

Nothing here writes to the receiver over SSH. Every command is a fixed read, and the
settings are never transferred — only their count and their SHA-256, computed where they
live.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import logging
import re
from typing import Any

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
    Connector,
    InstallerError,
    InstallerErrorCode,
    InstallerSession,
    SshCredentials,
    _async_connect,
    _async_enigma_pids,
    _async_wait_for_enigma,
    _run_checked,
    _timer_guard,
)

_LOGGER = logging.getLogger(__name__)

# The command's name on the wire, and the `cmd` a refusal of it carries on `last_error`.
COMMAND = "uninstall"

# The receiver's settings block, the part of `/etc/enigma2/settings` that is the plugin's.
# Sorted under the C locale so the hash does not depend on the order enigma2 happened to
# write the lines in, and only the hash leaves the receiver: the block holds the broker
# password. The whole file is not comparable across the restart — enigma2 rewrites its own
# `config.misc.*` lines on every shutdown — so the block is what „settings kept" means.
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
# OpenWebif serves the hook for as long as its module is loaded, which a removal of the
# files alone does not end: only the restart does. A 404 is the one answer that says the
# page is gone; every other status is a page still being served.
_HOOK_STATUS = "wget -S -O /dev/null http://127.0.0.1/mqttbridge 2>&1"
_HTTP_STATUS = re.compile(r"HTTP/\d(?:\.\d)?\s+(\d{3})")


class UninstallOutcome(StrEnum):
    """How an uninstall ended, named by what the options flow tells the user."""

    # The receiver retracted and said offline, and every SSH readback agreed.
    VERIFIED = "uninstall_verified"
    # The receiver retracted and said offline; SSH could not confirm, and says why.
    NOT_VERIFIED = "uninstall_not_verified"
    # The receiver retracted and said offline; there were no credentials to check with.
    REPORTED = "uninstall_reported"
    # The receiver answered the command on `last_error`.
    REFUSED = "uninstall_refused"
    # Nothing in the shape of an uninstall arrived before the timeout.
    NO_ACTION = "uninstall_no_action"


@dataclass(frozen=True, slots=True)
class UninstallResult:
    """The outcome, and the one fact that explains it.

    `detail` is the receiver's own sentence for a refusal, and for an unverified removal
    the readbacks that disagreed — named by the command or path they read, which is
    punctuation and file names rather than words, so it drops into the Polish and German
    sentences unchanged.
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
    recording or about to — before anything is published, because the plugin would refuse
    for the same reason and the restart at the end of a removal would cost the recording.
    Any other failure to read the receiver beforehand only costs the verification: the
    command still goes, and the result says it was not verified and why.
    """
    connector = _connector or _async_connect
    timeout = UNINSTALL_TIMEOUT if timeout is None else timeout
    before: _Before | None = None
    unverified = ""
    if credentials is not None:
        try:
            before = await _async_read_before(credentials, connector)
        except InstallerError as err:
            if err.code in (InstallerErrorCode.RECORDING, InstallerErrorCode.TIMER_DUE):
                raise
            unverified = _check_name(err)
            _LOGGER.warning(
                "Could not read receiver %s over SSH before uninstalling (%s); the "
                "removal goes ahead and will not be verified",
                box.node_id,
                err.detail or err.code.value,
            )

    watch = await _async_watch_uninstall(hass, box.base_topic, box.node_id)
    try:
        await watch.async_wait_until_established()
        await mqtt.async_publish(
            hass,
            command_topic(box.base_topic, box.node_id, COMMAND),
            box.node_id,
            qos=1,
            retain=False,
        )
        try:
            async with asyncio.timeout(timeout):
                refusal = await watch.done
        except TimeoutError:
            _LOGGER.warning(
                "Receiver %s did not uninstall its plugin within %s s: no retraction "
                "of its info and announcement followed by offline arrived",
                box.node_id,
                timeout,
            )
            return UninstallResult(UninstallOutcome.NO_ACTION)
    finally:
        watch.cancel()

    if refusal is not None:
        _LOGGER.warning("Receiver %s refused to uninstall: %s", box.node_id, refusal)
        return UninstallResult(UninstallOutcome.REFUSED, refusal)

    _LOGGER.info(
        "Receiver %s retracted its topics and went offline after cmd/uninstall",
        box.node_id,
    )
    if credentials is None:
        return UninstallResult(UninstallOutcome.REPORTED)
    if before is None:
        return UninstallResult(UninstallOutcome.NOT_VERIFIED, unverified)
    try:
        failed = await _async_read_after(credentials, connector, before)
    except InstallerError as err:
        failed = [_check_name(err)]
    if failed:
        _LOGGER.warning(
            "Receiver %s uninstalled its plugin, but these readbacks disagree: %s",
            box.node_id,
            ", ".join(failed),
        )
        return UninstallResult(UninstallOutcome.NOT_VERIFIED, ", ".join(failed))
    return UninstallResult(UninstallOutcome.VERIFIED)


def _check_name(err: InstallerError) -> str:
    """Name the readback an SSH failure stopped at, for the unverified sentence."""
    if err.code in (
        InstallerErrorCode.SSH_UNAVAILABLE,
        InstallerErrorCode.AUTH_FAILED,
        InstallerErrorCode.HOST_KEY_CHANGED,
    ):
        return f"ssh ({err.code.value})"
    if err.code is InstallerErrorCode.ROLLBACK_RESTART_FAILED:
        return "pidof enigma2"
    return err.detail or err.code.value


async def _async_read_before(
    credentials: SshCredentials, connector: Connector
) -> _Before:
    """Read the guards and the facts the after-readbacks compare against."""
    session = await connector(credentials)
    try:
        status = await _run_checked(
            session,
            "wget -qO- http://127.0.0.1/api/statusinfo",
            InstallerErrorCode.PREFLIGHT_FAILED,
            detail="/api/statusinfo",
        )
        timers = await _run_checked(
            session,
            "wget -qO- http://127.0.0.1/api/timerlist",
            InstallerErrorCode.PREFLIGHT_FAILED,
            detail="/api/timerlist",
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
        pids = await _async_enigma_pids(session)
        installed = await _run_checked(
            session,
            f"opkg status {PACKAGE}",
            InstallerErrorCode.PREFLIGHT_FAILED,
            detail="opkg status",
        )
        if f"Package: {PACKAGE}" not in installed.stdout.splitlines():
            # A plugin opkg does not know about claims no capability and would refuse;
            # if it did not, there would be nothing opkg could be shown to have removed.
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED, "opkg status")
        count, digest = await _async_settings_block(session)
        return _Before(pids, count, digest)
    finally:
        await session.close()


async def _async_read_after(
    credentials: SshCredentials, connector: Connector, before: _Before
) -> list[str]:
    """Return the readbacks that disagree with a removed plugin; empty means verified.

    The interface restart comes first, because the hook stays loaded and the page stays
    served until it happens. The plugin asks for it last and cannot promise it: the
    image's own restart asks on screen, with no timeout, whenever something is streaming
    or a background job runs. A restart that never came is therefore a readback that
    failed, not an uninstall that did — the plugin is disconnected and off the disk by
    then either way.
    """
    session = await connector(credentials)
    try:
        await _async_wait_for_enigma(session, before.pids)
        failed: list[str] = []
        status = await session.run(f"opkg status {PACKAGE}", timeout=30)
        if status.exit_status not in (0, 1) or status.stdout.strip():
            failed.append("opkg status")
        info_files = await session.run(_OPKG_INFO_FILES, timeout=30)
        if info_files.stdout.strip():
            failed.append("opkg info")
        directory = await session.run(f"test -e {PLUGIN_DIR}", timeout=30)
        if directory.exit_status != 1:
            failed.append(PLUGIN_DIR)
        leftovers = await session.run(_LEFTOVERS, timeout=60)
        if leftovers.exit_status != 0 or leftovers.stdout.strip():
            failed.append("find MQTTBridge")
        hook = await session.run(_HOOK_STATUS, timeout=30)
        statuses = _HTTP_STATUS.findall(hook.stdout + hook.stderr)
        # No status line at all is a `wget` this check cannot read, not a page that is
        # still there; the `find` above has already looked for the hook's files.
        if statuses and statuses[-1] != "404":
            failed.append(f"/mqttbridge {statuses[-1]}")
        count, digest = await _async_settings_block(session)
        if (count, digest) != (before.settings_count, before.settings_hash):
            failed.append("config.plugins.mqttbridge")
        return failed
    finally:
        await session.close()


async def _async_settings_block(session: InstallerSession) -> tuple[int, str]:
    """Return the plugin settings block's line count and hash, computed on the receiver."""
    result = await _run_checked(
        session,
        _SETTINGS_BLOCK,
        InstallerErrorCode.PREFLIGHT_FAILED,
        detail="config.plugins.mqttbridge",
    )
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
    """Four live subscriptions, and the answer they add up to.

    `done` resolves with None when the receiver has retracted `info` and its announcement
    and then said `offline`, and with the receiver's sentence when it answered the
    command on `last_error`.
    """

    done: asyncio.Future[str | None]
    established: asyncio.Future[None]
    cancel: CALLBACK_TYPE

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
    hass: HomeAssistant, base_topic: str, node_id: str
) -> _UninstallWatch:
    """Subscribe to the four topics whose fresh messages make up an uninstall."""
    done: asyncio.Future[str | None] = hass.loop.create_future()
    established: asyncio.Future[None] = hass.loop.create_future()
    gone = {"info": False, "announcement": False}
    confirmed = 0

    def _retracted_or_back(key: str, msg: ReceiveMessage) -> None:
        # A retained message is what the broker held before the command; it can never be
        # the receiver's answer to it. A fresh non-empty one after a retraction is the
        # plugin's failure path putting everything back, and undoes the retraction.
        if msg.retain or done.done():
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
        if msg.retain or done.done():
            return
        payload = (decode_payload(msg.payload) or "").strip()
        if payload == PAYLOAD_OFFLINE and all(gone.values()):
            done.set_result(None)
        elif payload == PAYLOAD_ONLINE:
            gone.update(info=False, announcement=False)

    @callback
    def _last_error(msg: ReceiveMessage) -> None:
        if msg.retain or done.done():
            return
        error = parse_json_payload(msg.payload)
        if error is not None and error.get("cmd") == COMMAND:
            done.set_result(str(error.get("error") or "").strip() or COMMAND)

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
        for future in (done, established):
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
    return _UninstallWatch(done=done, established=established, cancel=_cancel)

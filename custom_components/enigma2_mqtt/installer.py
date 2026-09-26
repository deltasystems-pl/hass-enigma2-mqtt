"""Transactional SSH installer for the receiver plugin.

The installer deliberately has no discovery, release-check, or download path. It
installs only the IPK bundled with this integration, after the bundle loader verifies
its provenance and digest. SSH host identity is explicitly confirmed by the config
flow and pinned on every authenticated connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import inspect
from io import BytesIO
import json
import logging
import math
from pathlib import Path
import re
import secrets
import shlex
import tarfile
import time
from typing import Any, Protocol

# asyncssh pulls in cryptography, which is slow to import and reads from disk. Importing
# it here means that happens once, while Home Assistant is importing this module in an
# executor, rather than inside a coroutine the first time somebody configures SSH.
import asyncssh
from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import HomeAssistant, callback
from packaging.version import InvalidVersion, Version

from . import installer_helper, restart_rule
from .box import parse_json_payload, same_service, state_topic
from .bundle import BundledPlugin, BundleError, load_bundled_plugin
from .const import (
    DEFAULT_BASE_TOPIC,
    HA_MODE_INTEGRATION,
    PLUGIN_SETTING_DEFAULTS,
    TOPIC_INFO,
)

_LOGGER = logging.getLogger(__name__)

PACKAGE = "enigma2-plugin-extensions-mqttbridge"
PLUGIN_DIR = "/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
PROVISION_PATH = "/etc/enigma2/mqttbridge.json"
# There is deliberately no opkg database path here. Preflight asks `opkg status` and
# opkg resolves its own configuration; the helper, which has to touch the files, does
# the same resolution itself in `installer_helper.opkg_paths`. Two constants naming
# `/usr/lib/opkg` used to sit here, unused, and they were wrong: OpenViX 6.6 keeps the
# database under `/var/lib/opkg` and says so in `/etc/opkg/opkg.conf`. A second opinion
# about where a database lives is a way for the two halves of this installer to
# disagree, and neither half needs one.
ENIGMA_SETTINGS = "/etc/enigma2/settings"
# Where the pre-change snapshots and the durable transaction lock live on the receiver.
# The abort messages name this directory, so it is a path a person is sent to.
BACKUP_ROOT = "/home/root/mqttbridge-backups"
MIN_PYTHON = (3, 9)
# The command timeouts of the two helper steps that take opkg's lock. The helper's own
# wait for that lock has to end inside them - `installer_helper.OPKG_LOCK_WAIT_*` - or the
# installer gives up on a helper that is still waiting and takes the lock later.
SNAPSHOT_TIMEOUT = 30
RESTORE_TIMEOUT = 60
MIN_FREE_BYTES = 2 * 1024 * 1024
TIMER_GUARD_SECONDS = 10 * 60
ANNOUNCEMENT_TIMEOUT = 120.0
# How long a rollback waits for the interface it stopped to come back, and how often it
# looks. The success path judges its restart by the plugin's fresh MQTT announcement and
# allows ANNOUNCEMENT_TIMEOUT for it; a rollback has no announcement coming - it has just
# put the old plugin back, or no plugin at all - so it watches the process instead, and
# it is given the same time. `init 3` returns immediately and the interface takes eleven
# to fourteen seconds to answer on the receivers measured, so a single look straight
# afterwards reads a box that is coming back perfectly well as one that is not.
ROLLBACK_RESTART_TIMEOUT = ANNOUNCEMENT_TIMEOUT
ROLLBACK_RESTART_POLL_SECONDS = 3.0
# How long the forward restart waits for enigma2's pid to change after asking OpenWebif
# for a clean restart (power state 3). A clean restart takes a few seconds; when it has
# not happened in this time, the image is asking a question on the television - it does
# for timeshift and for a background job - and that question has no timeout of its own.
RESTART_QUESTION_TIMEOUT = 60.0
RESTART_POLL_SECONDS = 2.0
# How long a rollback follows its stop-and-restore script on the receiver before it gives
# up waiting for the interface to be started again: the script's own wait for enigma2 to
# stop, the restore's own bound, and a margin.
R2_FOLLOW_TIMEOUT = installer_helper.R2_STOP_WAIT_SECONDS + RESTORE_TIMEOUT + 60.0
R2_FOLLOW_POLL_SECONDS = 2.0

ProgressCallback = Callable[[str], None | Awaitable[None]]


class InstallerErrorCode(StrEnum):
    """Stable error codes consumed by the config flow and update entity."""

    HOST_KEY_CHANGED = "host_key_changed"
    SSH_UNAVAILABLE = "ssh_unavailable"
    AUTH_FAILED = "auth_failed"
    PREFLIGHT_FAILED = "preflight_failed"
    UNSUPPORTED_PYTHON = "unsupported_python"
    NO_SPACE = "no_space"
    RECORDING = "recording"
    TIMER_DUE = "timer_due"
    # The two conditions a restart of the interface would break, which only the install
    # path checks: the options screen's credential probe restarts nothing.
    STANDBY = "standby"
    STREAMING = "streaming"
    BUNDLE_MISSING = "bundle_missing"
    BUNDLE_INVALID = "bundle_invalid"
    UPLOAD_FAILED = "upload_failed"
    INSTALL_FAILED = "install_failed"
    NEWER_INSTALLED = "newer_installed"
    PROVISION_FAILED = "provision_failed"
    RESTART_FAILED = "restart_failed"
    ANNOUNCEMENT_TIMEOUT = "announcement_timeout"
    # The image asked on the television whether to restart, and the install was
    # withdrawn: the old plugin's files are back and nothing was restarted.
    RESTART_WITHDRAWN = "restart_withdrawn"
    ROLLBACK_FAILED = "rollback_failed"
    ROLLBACK_RESTART_FAILED = "rollback_restart_failed"
    ROLLBACK_LOCK_FAILED = "rollback_lock_failed"
    ROLLBACK_OPKG_BUSY = "rollback_opkg_busy"
    ROLLBACK_OPKG_OVERLAP = "rollback_opkg_overlap"
    BUSY = "busy"
    OPKG_BUSY = "opkg_busy"
    IDENTITY_MISMATCH = "identity_mismatch"


class InstallerError(Exception):
    """An expected installer failure with no credentials in its text."""

    def __init__(
        self,
        code: InstallerErrorCode,
        detail: str = "",
        placeholders: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.detail = detail
        # What the abort sentence for this code needs filling in. Empty for every code
        # whose sentence has no holes, which is all but one of them.
        self.placeholders = placeholders or {}


def _log_install_refusal(error: InstallerError) -> None:
    """Write the one warning an install refused before any change leaves behind.

    An install stopped before the receiver was touched used to leave nothing at all: no
    line at INFO, none at WARNING, and the one sentence explaining it on a config-flow
    screen that is gone the moment it is read. Asked afterwards why an install had been
    refused, the log had no answer - measured on a receiver on 2026-09-22.

    Every guard carries the facts it judged in `detail`; this is the only thing that
    writes them, and only on the install path. The same guards are what the options
    screen's no-write credential probe and reauthentication run, and there a receiver
    that happens to be recording is a message on a form to be retried, not a refused
    install - a line claiming otherwise, once per retry, would be a false one. One
    boundary, one line, and nothing to keep in step about which of two places has
    already logged.

    Never a credential: the addresses, passwords and keys this module handles are not
    facts a guard judges, and no guard puts one in `detail`.
    """
    _LOGGER.warning(
        "Receiver install refused before the plugin or its settings were touched "
        "(%s): %s. No snapshot was taken and no transaction lock claimed; the "
        "installer's own helper script may remain in the receiver's /tmp.",
        error.code.value,
        error.detail or "the receiver did not answer one of the checks in a usable form",
    )


class _RollbackError(InstallerError):
    """A rollback step that failed, named by the outcome it leaves the receiver in.

    The steps of a rollback fail for different reasons and leave the receiver in
    different states - files restored or not, interface up or not, lock released or
    not - and telling a person to inspect a box that only needs restarting is as
    unhelpful as the reverse. A marker class rather than a bare `InstallerError` so
    that the caller can tell the rollback's own verdict from anything else.
    """


@dataclass(slots=True)
class _RollbackProgress:
    """What a rollback achieved, readable even when it ends in a cancellation.

    The transaction lock is the part the caller must know about whatever happened: a
    lock it wrongly believes is still held is an error line about a receiver that is
    fine, and one it wrongly believes is gone is silence about a receiver that will
    refuse the next install as busy. An exception cannot carry that when the exception
    is a `CancelledError`, which has to stay exactly what it is.
    """

    lock_released: bool = False


@dataclass(frozen=True, slots=True)
class HostKey:
    """A server key shown to the user before any credentials are sent."""

    algorithm: str
    public_key: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SshCredentials:
    """Credentials for a host whose public key has already been confirmed."""

    host: str
    username: str
    password: str = field(repr=False)
    host_key: str
    port: int = 22


@dataclass(frozen=True, slots=True)
class Provisioning:
    """One-shot plugin settings written through SSH stdin."""

    broker_host: str
    broker_port: int
    broker_username: str
    broker_password: str = field(repr=False)
    node_id: str
    base_topic: str = DEFAULT_BASE_TOPIC
    friendly_name: str | None = None

    def display_name(self) -> str:
        """Return the name this document would set, or an empty string for none.

        Spaces are not a name. A field left blank and a field holding two spaces are
        the same intention, and telling them apart anywhere downstream would mean a
        receiver named `  ` on somebody's wall.
        """
        return (self.friendly_name or "").strip()

    def as_json(self) -> bytes:
        """Return the provisioning document without ever logging it."""
        values: dict[str, Any] = {
            "enabled": True,
            "host": self.broker_host,
            "port": self.broker_port,
            "username": self.broker_username,
            "password": self.broker_password,
            "node_id": self.node_id,
            "base_topic": self.base_topic,
            "ha_mode": "integration",
        }
        # Omitted rather than emitted empty, because the plugin applies every key the
        # document holds and leaves every key it does not: a receiver whose name the
        # form did not give keeps the one it has, and one that has none is named by
        # the plugin after its box type, as it is on a first start.
        if self.display_name():
            values["friendly_name"] = self.display_name()
        return (json.dumps(values, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """Everything needed for one install or update transaction."""

    credentials: SshCredentials
    provisioning: Provisioning | None = None
    keep_credentials: bool = False
    expect_running: bool = False
    node_id: str = ""
    base_topic: str = DEFAULT_BASE_TOPIC

    def target(self) -> tuple[str, str]:
        """Return the expected MQTT target for fresh post-restart proof."""
        if self.provisioning is not None:
            return self.provisioning.base_topic, self.provisioning.node_id
        if not self.node_id:
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
        return self.base_topic, self.node_id

    def validate(self) -> tuple[str, str]:
        """Validate the MQTT identity before making any receiver connection."""
        base_topic, node_id = self.target()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", node_id):
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", base_topic):
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
        return base_topic, node_id


@dataclass(frozen=True, slots=True)
class Preflight:
    """Facts measured before the box is changed."""

    image: str
    python_version: str
    free_bytes: int
    installed_version: str | None
    plugin_files_present: bool
    recording: bool
    timers_due: int


@dataclass(frozen=True, slots=True)
class InstallResult:
    """A completed transaction."""

    version: str
    backup_created: bool
    restarted: bool
    # What the receiver is called now the transaction is over: the name the form gave,
    # or the one the receiver already held when the form gave none. Empty when nothing
    # was provisioned - an update writes no settings - and the caller then keeps
    # whatever name it already had for the box.
    friendly_name: str = ""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Transport-neutral remote command result."""

    exit_status: int
    stdout: str = ""
    stderr: str = ""


class InstallerSession(Protocol):
    """The narrow SSH surface used by the transaction and fake tests."""

    async def run(
        self, command: str, *, input: bytes | None = None, timeout: float = 30
    ) -> CommandResult: ...

    async def close(self) -> None: ...


Connector = Callable[[SshCredentials], Awaitable[InstallerSession]]
_ACTIVE_HOSTS: set[tuple[str, int]] = set()


class _AsyncSshSession:
    """AsyncSSH adapter; receivers need no SFTP subsystem."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def run(
        self, command: str, *, input: bytes | None = None, timeout: float = 30
    ) -> CommandResult:
        result = await self._connection.run(command, input=input, timeout=timeout)
        stdout = (result.stdout or b"")[: 64 * 1024].decode(errors="replace")
        stderr = (result.stderr or b"")[: 64 * 1024].decode(errors="replace")
        return CommandResult(result.exit_status, stdout, stderr)

    async def close(self) -> None:
        self._connection.close()
        await self._connection.wait_closed()


async def async_probe_host_key(host: str, port: int = 22) -> HostKey:
    """Read a host key without sending a username, password, or command."""
    class _CaptureClient(asyncssh.SSHClient):
        key: Any = None

        def validate_host_public_key(
            self, probe_host: str, addr: str, probe_port: int, key: Any
        ) -> bool:
            del probe_host, addr, probe_port
            self.key = key
            return True

    capture = _CaptureClient()
    connection = None
    try:
        connection = await asyncssh.connect(
            host,
            port=port,
            known_hosts=([], [], [], [], [], [], []),
            client_factory=lambda: capture,
            username="",
            password=None,
            client_keys=[],
            preferred_auth=[],
            login_timeout=10,
        )
    except asyncssh.PermissionDenied:
        # Expected on a receiver which correctly refuses the empty username. Host-key
        # exchange and proof of possession happen before authentication.
        pass
    except (OSError, asyncssh.Error) as err:
        raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE) from err
    finally:
        if connection is not None:
            connection.close()
            await connection.wait_closed()
    key = capture.key
    if key is None:
        raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE)
    exported = key.export_public_key().decode().strip()
    return HostKey(key.get_algorithm(), exported, key.get_fingerprint("sha256"))


async def _async_connect(credentials: SshCredentials) -> InstallerSession:
    """Connect with the exact key the user confirmed."""
    try:
        key = asyncssh.import_public_key(credentials.host_key)
        known_hosts = ([key], [], [], [], [], [], [])
        connection = await asyncssh.connect(
            credentials.host,
            port=credentials.port,
            username=credentials.username,
            password=credentials.password,
            client_keys=[],
            known_hosts=known_hosts,
            encoding=None,
            login_timeout=15,
        )
    except (asyncssh.HostKeyNotVerifiable, asyncssh.KeyImportError) as err:
        # `KeyImportError` is the stored key itself being unreadable, and it subclasses
        # `ValueError` rather than `asyncssh.Error` - so it used to sail past both
        # handlers below and reach the user as a traceback and the word "Unknown". It
        # belongs here: whatever the cause - a truncated write, a hand-edited entry, a
        # restored backup from another receiver - the pinned identity is no longer
        # usable, and the recovery is the same one a changed host key gets, which is to
        # be shown the fingerprint again and asked to accept it.
        raise InstallerError(InstallerErrorCode.HOST_KEY_CHANGED) from err
    except asyncssh.PermissionDenied as err:
        raise InstallerError(InstallerErrorCode.AUTH_FAILED) from err
    except (OSError, asyncssh.Error) as err:
        raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE) from err
    return _AsyncSshSession(connection)


async def _run_checked(
    session: InstallerSession,
    command: str,
    code: InstallerErrorCode,
    *,
    input: bytes | None = None,
    timeout: float = 30,
    detail: str = "",
    busy: InstallerErrorCode | None = None,
) -> CommandResult:
    """Run a fixed command and map failures without retaining its output.

    `detail` says what the command was for, never what it was: the command line is
    fixed and uninteresting, and the caller's own words are what makes a refusal
    readable in the log. It is not logged here, because this runs in steps that are
    recovered from as well as in ones that refuse.

    `busy` is the code for a helper step that could not get opkg's lock in time. That
    one failure gets its own sentence because it is the one a person can fix by waiting
    - somebody is installing something from the receiver's menu - and the helper's own
    words for it would otherwise be discarded with the rest of its output.
    """
    try:
        result = await session.run(command, input=input, timeout=timeout)
    except (OSError, TimeoutError) as err:
        raise InstallerError(code, detail) from err
    if busy is not None and result.exit_status == installer_helper.EXIT_OPKG_BUSY:
        raise InstallerError(busy, "the receiver's opkg lock stayed held by another opkg run")
    if result.exit_status:
        raise InstallerError(code, detail)
    return result


def _python_version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+)+", value.strip()):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    return tuple(int(part) for part in value.strip().split("."))


def _package_version(value: str) -> Version:
    """Parse the supported opkg version subset without silently reordering it."""
    normalized = value.strip()
    revision = re.fullmatch(r"(\d+(?:\.\d+)*)(?:-r(\d+))?", normalized)
    prerelease = re.fullmatch(r"(\d+(?:\.\d+)*?)-(alpha|beta|rc)[.-]?(\d+)", normalized)
    if revision:
        normalized = revision.group(1)
        if revision.group(2) is not None:
            normalized += f".post{revision.group(2)}"
    elif prerelease:
        label = {"alpha": "a", "beta": "b", "rc": "rc"}[prerelease.group(2)]
        normalized = f"{prerelease.group(1)}{label}{prerelease.group(3)}"
    else:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    try:
        return Version(normalized)
    except InvalidVersion as err:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED) from err


def _strict_bool(value: Any) -> bool:
    """Parse OpenWebif booleans without accepting arbitrary truthy values."""
    if isinstance(value, bool):
        return value
    if value in (0, "0", "false", "False"):
        return False
    if value in (1, "1", "true", "True"):
        return True
    raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)


def _finite_timestamp(value: Any) -> float:
    """Parse a finite Unix timestamp; bools and missing fields are invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    try:
        parsed = float(value)
    except ValueError as err:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED) from err
    if not math.isfinite(parsed) or parsed < 0:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    return parsed


def _restart_guard(status_raw: str) -> tuple[bool, bool]:
    """Return whether the receiver is in standby and whether it is streaming.

    Fail closed like the rest of the preflight: an install restarts the interface, and
    a restart wakes a receiver in standby - which, with HDMI-CEC on, switches the
    television on too - and cuts off whoever is watching a stream from it. A receiver
    that does not say which it is gets no restart.
    """
    try:
        status = json.loads(status_raw)
    except (TypeError, ValueError) as err:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED) from err
    if not isinstance(status, dict) or "inStandby" not in status or "isStreaming" not in status:
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "OpenWebif did not say whether the receiver is in standby or streaming",
        )
    return _strict_bool(status["inStandby"]), _strict_bool(status["isStreaming"])


def _timer_guard(status_raw: str, timers_raw: str) -> tuple[bool, int]:
    """Fail closed unless OpenWebif returns understood structured state."""
    try:
        status = json.loads(status_raw)
        timers_doc = json.loads(timers_raw)
    except (TypeError, ValueError) as err:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED) from err
    if not isinstance(status, dict) or not isinstance(timers_doc, dict):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    recording_raw = status.get("isRecording", status.get("is_recording"))
    if recording_raw is None:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    recording_value = _strict_bool(recording_raw)
    timers = timers_doc.get("timers")
    if not isinstance(timers, list):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    now = time.time()
    due = 0
    for timer in timers:
        if not isinstance(timer, dict):
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
        if "disabled" not in timer or "state" not in timer:
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
        disabled = _strict_bool(timer["disabled"])
        if disabled:
            continue
        running_raw = timer.get("isRunning", timer.get("is_running"))
        running = False if running_raw is None else _strict_bool(running_raw)
        state = timer.get("state")
        if running is True or state in (2, "2", "running"):
            recording_value = True
        begin = _finite_timestamp(timer.get("begin"))
        end = _finite_timestamp(timer.get("end"))
        if end <= begin:
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
        if end > now and begin <= now + TIMER_GUARD_SECONDS:
            due += 1
    return recording_value, due


async def _async_measure_preflight(
    session: InstallerSession, *, bundle_size: int = 0, restart_guards: bool = False
) -> Preflight:
    """Measure every install guard through fixed receiver-local commands.

    `restart_guards` adds the two that only a restart of the interface needs - standby
    and streaming - and only the install path asks for them.
    """
    image = (
        await _run_checked(
            session,
            "cat /etc/image-version 2>/dev/null || cat /etc/issue 2>/dev/null",
            InstallerErrorCode.PREFLIGHT_FAILED,
            detail="the receiver did not say which image it runs",
        )
    ).stdout.strip()[:200]
    python_version = (
        await _run_checked(
            session,
            "python3 -c 'import sys; print(\".\".join(map(str, sys.version_info[:3])))'",
            InstallerErrorCode.PREFLIGHT_FAILED,
            detail="the receiver did not say which Python it runs",
        )
    ).stdout.strip()
    if _python_version(python_version)[:2] < MIN_PYTHON:
        raise InstallerError(
            InstallerErrorCode.UNSUPPORTED_PYTHON,
            f"the receiver runs Python {python_version}; this plugin needs "
            f"{'.'.join(str(part) for part in MIN_PYTHON)} or newer",
        )
    free_raw = (
        await _run_checked(
            session,
            "for p in /tmp /usr /etc; do df -Pk \"$p\" | awk 'NR==2 {print $4 * 1024}'; done",
            InstallerErrorCode.PREFLIGHT_FAILED,
            detail="the free space on /tmp, /usr and /etc could not be read",
        )
    ).stdout.splitlines()
    try:
        free_values = [int(float(value)) for value in free_raw]
    except ValueError as err:
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "the receiver's free-space figures are not numbers",
        ) from err
    if len(free_values) != 3 or any(value < 0 for value in free_values):
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            f"the receiver reported {len(free_values)} free-space figures, expected three",
        )
    plugin_size_raw = (
        await _run_checked(
            session,
            f"if test -d {PLUGIN_DIR}; then du -sk {PLUGIN_DIR} | awk '{{print $1 * 1024}}'; "
            "else echo 0; fi",
            InstallerErrorCode.PREFLIGHT_FAILED,
            detail="the size of the installed plugin directory could not be read",
        )
    ).stdout.strip()
    try:
        plugin_size = int(float(plugin_size_raw))
    except ValueError as err:
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "the size of the installed plugin directory is not a number",
        ) from err
    required_tmp = max(MIN_FREE_BYTES, bundle_size * 2 + plugin_size + 1024 * 1024)
    required_usr = max(MIN_FREE_BYTES, bundle_size * 3 + plugin_size * 2)
    required_etc = 128 * 1024
    required_values = (required_tmp, required_usr, required_etc)
    if any(
        free < required
        for free, required in zip(free_values, required_values, strict=True)
    ):
        raise InstallerError(
            InstallerErrorCode.NO_SPACE,
            "the receiver has "
            + ", ".join(
                f"{where} {free} bytes free of {required} needed"
                for where, free, required in zip(
                    ("/tmp", "/usr", "/etc"), free_values, required_values, strict=True
                )
            ),
        )
    await _run_checked(
        session,
        "command -v opkg >/dev/null && opkg --version >/dev/null",
        InstallerErrorCode.PREFLIGHT_FAILED,
        detail="the receiver has no opkg, or it would not run",
    )
    files_result = await session.run(f"test -d {PLUGIN_DIR}", timeout=30)
    if files_result.exit_status not in (0, 1):
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "the receiver would not say whether the plugin directory exists",
        )
    plugin_files_present = files_result.exit_status == 0
    status_result = await session.run(f"opkg status {PACKAGE}", timeout=30)
    if status_result.exit_status not in (0, 1):
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "`opkg status` failed on the receiver",
        )
    installed = None
    if status_result.stdout.strip():
        package_seen = False
        for line in status_result.stdout.splitlines():
            if line == f"Package: {PACKAGE}":
                package_seen = True
            if line.startswith("Version: "):
                if installed is not None:
                    raise InstallerError(
                        InstallerErrorCode.PREFLIGHT_FAILED,
                        "`opkg status` reported the package twice",
                    )
                installed = line.removeprefix("Version: ").strip()
        if not package_seen or not installed:
            raise InstallerError(
                InstallerErrorCode.PREFLIGHT_FAILED,
                "`opkg status` answered without naming the package and its version",
            )
        _package_version(installed)
    status = await _run_checked(
        session,
        "wget -qO- http://127.0.0.1/api/statusinfo",
        InstallerErrorCode.PREFLIGHT_FAILED,
        detail="OpenWebif did not answer, so whether the receiver is recording is unknown",
    )
    timers = await _run_checked(
        session,
        "wget -qO- http://127.0.0.1/api/timerlist",
        InstallerErrorCode.PREFLIGHT_FAILED,
        detail="OpenWebif did not answer with the receiver's timers",
    )
    recording, timers_due = _timer_guard(status.stdout, timers.stdout)
    standby, streaming = _restart_guard(status.stdout) if restart_guards else (False, False)
    if recording:
        raise InstallerError(
            InstallerErrorCode.RECORDING,
            "the receiver is recording, and a GUI restart would lose the recording",
        )
    if timers_due:
        raise InstallerError(
            InstallerErrorCode.TIMER_DUE,
            f"{timers_due} recording(s) are due on the receiver within the next "
            f"{TIMER_GUARD_SECONDS // 60} minutes",
        )
    if standby:
        raise InstallerError(
            InstallerErrorCode.STANDBY,
            "the receiver is in standby, and restarting its interface would wake it "
            "and could switch the television on",
        )
    if streaming:
        raise InstallerError(
            InstallerErrorCode.STREAMING,
            "the receiver is streaming, and restarting its interface would cut the stream off",
        )
    return Preflight(
        image,
        python_version,
        min(free_values),
        installed,
        plugin_files_present,
        recording,
        timers_due,
    )


async def _async_enigma_pids(session: InstallerSession) -> set[int]:
    """Return every running Enigma PID, which on some images is more than one.

    This used to insist on exactly one and refuse anything else as an ambiguous
    lifecycle. Images that run a wrapper beside the interface it starts report two for
    as long as the box is up, so on one of those the restart proof could never be
    satisfied and neither an install nor a rollback could finish on a receiver that was
    working perfectly.

    `pidof` exits 1 and says nothing when there is no match, which is a receiver with
    its interface down - a box still booting, or one stopped on purpose - rather than a
    receiver that failed to answer. The empty set is the answer, not an error.
    """
    try:
        result = await session.run("pidof enigma2", timeout=30)
    except (OSError, TimeoutError) as err:
        raise InstallerError(InstallerErrorCode.RESTART_FAILED) from err
    if result.exit_status not in (0, 1):
        raise InstallerError(InstallerErrorCode.RESTART_FAILED)
    values = result.stdout.split()
    if any(not value.isdigit() for value in values):
        raise InstallerError(InstallerErrorCode.RESTART_FAILED)
    return {int(value) for value in values}


async def _async_wait_for_enigma(session: InstallerSession, old_pids: set[int]) -> set[int]:
    """Wait until Enigma is running under a pid that was not there before.

    `init 3` returns as soon as the runlevel change is accepted, not when the interface
    is up, so asking `pidof` once straight afterwards asks a question the receiver
    cannot yet answer. A box that was coming back normally was read as one that had not
    come back at all, which turned a rollback that had worked into a reported failure -
    and, because that verdict came before the lock was released, it wedged every later
    install on that receiver.

    The proof is any pid that is not in the set read before the interface was stopped,
    because such a process was started since. "One pid, and a different one" is too
    narrow: some images run a wrapper that survives a GUI restart beside the child that
    does not, and a receiver like that would spend the whole timeout being told it had
    an ambiguous lifecycle. A set that never gains a member is an interface that never
    came back, which is what the timeout is for.
    """
    deadline = time.monotonic() + ROLLBACK_RESTART_TIMEOUT
    while True:
        with suppress(InstallerError):
            pids = await _async_enigma_pids(session)
            if pids - old_pids:
                return pids
        if time.monotonic() >= deadline:
            raise InstallerError(InstallerErrorCode.ROLLBACK_RESTART_FAILED)
        await asyncio.sleep(ROLLBACK_RESTART_POLL_SECONDS)


def _stored_setting(identity: dict[str, Any], name: str) -> Any:
    """Return a stored plugin setting, reading an absent one as the plugin's default.

    `None` from the receiver-side helper means the settings file has no line for this
    setting, and enigma2 writes no line for a setting that still equals its default -
    so absence is the default, never a difference. The defaults come from
    `PLUGIN_SETTING_DEFAULTS`, which is the only copy of them on this side.
    """
    stored = identity.get(name)
    return PLUGIN_SETTING_DEFAULTS[name] if stored is None else stored


def _stored_for_display(identity: dict[str, Any], name: str) -> str:
    """Render a stored setting for the abort sentence, marking one that is not stored.

    Brackets mean exactly one thing: the receiver has no line for this setting. What is
    inside them is the plugin's default, which is what applies, or `-` where the plugin
    has none to apply. That is the difference between "the box says `enigma2`" and "the
    box says nothing and `enigma2` is what that means", which is the whole diagnosis
    when the two sides look identical and the install was refused anyway.

    A setting stored as an empty string is stored, so it gets no brackets: it is shown
    as `""`, because it is the one value that would otherwise appear as nothing at all
    in the middle of a sentence - and because it is a real difference from the default,
    which is how `_stored_setting` compares it. Collapsing the two would put the same
    two-identical-looking-values problem back, one layer down.

    Punctuation rather than words throughout, because this string is dropped into the
    Polish and German sentences unchanged and a word in them would be an English one.
    """
    stored = identity.get(name)
    if stored is None:
        default = PLUGIN_SETTING_DEFAULTS[name]
        return f"({default})" if default != "" else "(-)"
    return str(stored) if stored != "" else '""'


async def _async_validate_receiver_identity(
    session: InstallerSession,
    remote_helper: str,
    request: InstallRequest,
) -> str:
    """Refuse to update or overwrite a differently configured receiver.

    Returns the name the receiver stores, stripped, or an empty string for a receiver
    that stores none. It is not part of any comparison - a box may be called anything
    - but this is the one point in the transaction that has already read the settings
    file, and it is the only way to know what a box whose name the form did not give
    is going to be called.

    What the helper reports is what the settings file holds, and a setting that is not
    in it is `null` rather than a value - enigma2 writes no line for a value that still
    equals its default, so a receiver on the default base topic stores no base topic.
    Every comparison here is therefore made after the plugin's own defaults have been
    applied, from `PLUGIN_SETTING_DEFAULTS`, which is the only copy of them on this
    side and which CI compares against the bundled plugin's own `config.py`. The helper
    used to apply them instead, on the receiver, where nothing checked them against the
    plugin and where absence and the default value arrived here as the same answer.

    An absent node id is the one that is not defaulted, because the plugin has no
    default worth comparing against - it derives `<boxtype>_<mac6>` on its first run
    and writes it, so a box with no node id is a box whose plugin has never started.
    There is nothing there to take over, so provisioning proceeds and writes the one
    the form gave. Deriving the expected id instead would mean guessing the MAC at a
    point in the transaction where the only source for it is an announcement a box in
    `ha_mode: off` is not publishing. An update, which writes no settings, still
    refuses such a box: it has to already be the box the entry was made for.
    """
    result = await _run_checked(
        session,
        f"python3 {shlex.quote(remote_helper)} identity",
        InstallerErrorCode.PREFLIGHT_FAILED,
    )
    try:
        identity = json.loads(result.stdout)
    except (TypeError, ValueError) as err:
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "the receiver's plugin settings could not be read as JSON",
        ) from err
    if not isinstance(identity, dict) or set(identity) != {
        "node_id",
        "base_topic",
        "enabled",
        "ha_mode",
        "friendly_name",
    }:
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "the receiver did not report the settings that bind it to an entry",
        )
    stored_node = identity["node_id"]
    stored_base = identity["base_topic"]
    stored_name = identity["friendly_name"]
    if (
        not isinstance(stored_node, (str, type(None)))
        or not isinstance(stored_base, (str, type(None)))
        or not isinstance(identity["enabled"], (bool, type(None)))
        or not isinstance(identity["ha_mode"], (str, type(None)))
        or not isinstance(stored_name, (str, type(None)))
    ):
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "the receiver reported a plugin setting of the wrong type",
        )
    configured_base = _stored_setting(identity, "base_topic")
    configured_enabled = _stored_setting(identity, "enabled")
    configured_mode = _stored_setting(identity, "ha_mode")
    expected_base, expected_node = request.validate()
    placeholders = {
        "box_node_id": _stored_for_display(identity, "node_id"),
        "box_base_topic": _stored_for_display(identity, "base_topic"),
        "node_id": expected_node,
        "base_topic": expected_base,
    }
    if request.provisioning is None:
        if (
            stored_node != expected_node
            or configured_base != expected_base
            or configured_enabled is not True
            or configured_mode != HA_MODE_INTEGRATION
        ):
            raise InstallerError(
                InstallerErrorCode.IDENTITY_MISMATCH,
                f"the receiver is configured for node {placeholders['box_node_id']} on "
                f"{placeholders['box_base_topic']} (enabled={configured_enabled}, "
                f"ha_mode={configured_mode}), and this entry expects node "
                f"{expected_node} on {expected_base} in integration mode",
                placeholders,
            )
    elif stored_node and (stored_node != expected_node or configured_base != expected_base):
        raise InstallerError(
            InstallerErrorCode.IDENTITY_MISMATCH,
            f"the receiver is already configured for node {placeholders['box_node_id']} "
            f"on {placeholders['box_base_topic']}, and the form gives node "
            f"{expected_node} on {expected_base}",
            placeholders,
        )
    return (stored_name or "").strip()


async def async_preflight(
    hass: HomeAssistant,
    credentials: SshCredentials,
    *,
    _connector: Connector = _async_connect,
) -> Preflight:
    """Connect to a pinned host and perform the no-write preflight."""
    del hass
    session = await _connector(credentials)
    try:
        return await _async_measure_preflight(session)
    finally:
        await session.close()


async def _async_progress(callback_fn: ProgressCallback | None, step: str) -> None:
    if callback_fn is None:
        return
    result = callback_fn(step)
    if inspect.isawaitable(result):
        await result


@dataclass(slots=True)
class _RestartWatch:
    """Fresh post-restart MQTT proof; retained state can never satisfy it."""

    completed: asyncio.Future[None]
    established: asyncio.Future[None]
    cancel_callbacks: list[Callable[[], None]]
    arm: Callable[[], None]

    def cancel(self) -> None:
        for cancel_callback in self.cancel_callbacks:
            cancel_callback()
        if not self.completed.done():
            self.completed.cancel()
        if not self.established.done():
            self.established.cancel()


async def _async_watch_restart(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    version: str,
    *,
    require_offline: bool,
) -> _RestartWatch:
    completed: asyncio.Future[None] = hass.loop.create_future()
    established: asyncio.Future[None] = hass.loop.create_future()
    offline_seen = False
    online_seen = False
    version_seen = False
    armed = False
    subscriptions_ready = 0

    def _finish_if_ready() -> None:
        offline_ready = offline_seen or not require_offline
        if offline_ready and online_seen and version_seen and not completed.done():
            completed.set_result(None)

    @callback
    def _availability(message: ReceiveMessage) -> None:
        nonlocal offline_seen, online_seen
        if message.retain or not armed:
            return
        payload = (
            message.payload.decode() if isinstance(message.payload, bytes) else message.payload
        )
        if payload == "offline":
            offline_seen = True
        elif payload == "online" and (offline_seen or not require_offline):
            online_seen = True
        _finish_if_ready()

    @callback
    def _info(message: ReceiveMessage) -> None:
        nonlocal version_seen
        if message.retain or not armed or not online_seen:
            return
        info = parse_json_payload(message.payload)
        if (
            info is not None
            and info.get("plugin") == version
            and info.get("ha_mode") == "integration"
        ):
            version_seen = True
        _finish_if_ready()

    @callback
    def _subscribed() -> None:
        nonlocal subscriptions_ready
        subscriptions_ready += 1
        if subscriptions_ready == 2 and not established.done():
            established.set_result(None)

    availability_topic = state_topic(base_topic, node_id, "availability")
    info_topic = state_topic(base_topic, node_id, TOPIC_INFO)
    cancel_callbacks: list[Callable[[], None]] = []
    try:
        cancel_callbacks.extend(
            (
                mqtt.async_on_subscribe_done(
                    hass, availability_topic, mqtt.DEFAULT_QOS, _subscribed
                ),
                mqtt.async_on_subscribe_done(hass, info_topic, mqtt.DEFAULT_QOS, _subscribed),
            )
        )
        cancel_callbacks.append(await mqtt.async_subscribe(hass, availability_topic, _availability))
        cancel_callbacks.append(await mqtt.async_subscribe(hass, info_topic, _info))
    except BaseException:
        for cancel_callback in cancel_callbacks:
            cancel_callback()
        completed.cancel()
        established.cancel()
        raise

    @callback
    def _arm() -> None:
        nonlocal armed
        armed = True

    return _RestartWatch(completed, established, cancel_callbacks, _arm)


async def _async_load_bundle(hass: HomeAssistant) -> BundledPlugin:
    try:
        return await hass.async_add_executor_job(load_bundled_plugin)
    except FileNotFoundError as err:
        raise InstallerError(InstallerErrorCode.BUNDLE_MISSING) from err
    except BundleError as err:
        raise InstallerError(InstallerErrorCode.BUNDLE_INVALID) from err


def _installed_hash_manifest(ipk: bytes) -> bytes:
    """Build a sha256sum manifest for every regular file in an ar-format IPK."""
    if not ipk.startswith(b"!<arch>\n"):
        raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
    offset = 8
    payload: bytes | None = None
    while offset + 60 <= len(ipk):
        header = ipk[offset : offset + 60]
        if header[58:60] != b"`\n":
            raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
        try:
            size = int(header[48:58].decode().strip())
        except ValueError as err:
            raise InstallerError(InstallerErrorCode.BUNDLE_INVALID) from err
        name = header[:16].decode(errors="replace").strip().rstrip("/")
        offset += 60
        member = ipk[offset : offset + size]
        if len(member) != size:
            raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
        if name.startswith("data.tar"):
            payload = member
        offset += size + (size % 2)
    if payload is None:
        raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
    lines: list[str] = []
    try:
        with tarfile.open(fileobj=BytesIO(payload), mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                relative = member.name.removeprefix("./").lstrip("/")
                if not relative or ".." in Path(relative).parts:
                    raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
                digest = hashlib.sha256(extracted.read()).hexdigest()
                lines.append(f"{digest}  /{relative}")
    except (tarfile.TarError, OSError) as err:
        raise InstallerError(InstallerErrorCode.BUNDLE_INVALID) from err
    if not lines:
        raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
    return ("\n".join(sorted(lines)) + "\n").encode()


async def _async_remove_helper(session: InstallerSession, remote_helper: str) -> None:
    """Delete the uploaded helper, treating a failure as untidy rather than fatal."""
    try:
        await session.run(f"rm -f {shlex.quote(remote_helper)}", timeout=30)
    except Exception:
        _LOGGER.warning("Installer helper left behind on the receiver at %s", remote_helper)


def _r2_files(remote_helper: str) -> tuple[str, str, str]:
    """Return the stop-and-restore script's own files: script, status, output.

    Named after the transaction, beside the helper in `/tmp`, so they go away with a
    reboot and never collide with another transaction's.
    """
    stem = remote_helper.removesuffix(".py").replace(
        "enigma2-mqtt-installer-", "enigma2-mqtt-r2-"
    )
    return f"{stem}.sh", f"{stem}.status", f"{stem}.log"


def _r2_lines(stdout: str) -> dict[str, str]:
    """Read the script's status file into `{step: argument}`; the last word wins."""
    steps: dict[str, str] = {}
    for line in stdout.splitlines():
        step, _, argument = line.strip().partition(" ")
        if step:
            steps[step] = argument
    return steps


async def _async_follow_r2(
    session: InstallerSession | None,
    credentials: SshCredentials,
    connector: Connector,
    status_file: str,
) -> tuple[InstallerSession | None, dict[str, str]]:
    """Follow the stop-and-restore script until it has started the interface again.

    It runs on the receiver whatever happens here. A dropped connection is only a gap in
    what this end can see, so the following reconnects and carries on; the script, which
    is in a session of its own, notices nothing.
    """
    deadline = time.monotonic() + R2_FOLLOW_TIMEOUT
    steps: dict[str, str] = {}
    while True:
        try:
            if session is None:
                session = await connector(credentials)
            result = await session.run(f"cat {shlex.quote(status_file)}", timeout=30)
            steps = _r2_lines(result.stdout)
            if "started" in steps:
                return session, steps
        except (OSError, TimeoutError, asyncssh.Error, InstallerError):
            if session is not None:
                with suppress(Exception):
                    await session.close()
            session = None
        if time.monotonic() >= deadline:
            return session, steps
        await asyncio.sleep(R2_FOLLOW_POLL_SECONDS)


def _r2_restore_code(steps: dict[str, str]) -> InstallerErrorCode | None:
    """Return the verdict on the restore the script ran, or None when it succeeded."""
    rc = steps.get("restored")
    if rc == "0":
        return None
    if rc == str(installer_helper.EXIT_OPKG_BUSY):
        return InstallerErrorCode.ROLLBACK_OPKG_BUSY
    if rc == str(installer_helper.EXIT_OPKG_LOCK_LOST):
        return InstallerErrorCode.ROLLBACK_OPKG_OVERLAP
    return InstallerErrorCode.ROLLBACK_FAILED


def _rollback_record(
    record: restart_rule.RestartRecord | None, service: str | None, standby: bool | None
) -> restart_rule.RestartRecord:
    """Return what R2 keeps: what the receiver plays now, else what it played before.

    Now wins, because it is what the household is watching. The record taken before
    the install's restart is the fallback for an interface that no longer answers, and
    the bouquet recorded then is carried over only when it was recorded with the same
    channel - otherwise nothing says the channel is in it.
    """
    if service is None:
        return record or restart_rule.RestartRecord()
    if record is not None and same_service(record.service, service):
        return restart_rule.RestartRecord(
            service, standby, record.bouquet, record.bouquet_holds_service
        )
    return restart_rule.RestartRecord(service, standby)


async def _async_rollback(
    credentials: SshCredentials,
    backup: str,
    remote_ipk: str,
    remote_helper: str,
    remote_manifest: str,
    remote_provision_tmp: str,
    remote_lock: str,
    install_started: bool,
    provision_started: bool,
    restart_started: bool,
    connector: Connector,
    progress: _RollbackProgress | None = None,
    *,
    record: restart_rule.RestartRecord | None = None,
    hass: HomeAssistant | None = None,
    target: tuple[str, str] | None = None,
) -> None:
    """Restore only installer-owned paths and the exact pre-transaction metadata.

    Two ways, by whether the interface was restarted onto the new plugin:

    - **Not restarted**: the old enigma2 is still the running process, so the files and
      opkg's metadata go back underneath it - each by a rename, the plugin directory by
      two - and the settings block is left alone: the transaction changed none, and a
      block written now would be overwritten from memory by the next clean quit.
    - **Restarted** (R2 of the restart rule): the new plugin ran and may have written its
      settings, so they go back too, which needs enigma2 stopped. The channel being
      watched is recorded, then one script on the receiver - started detached, followed
      through its status file - stops the interface, restores, writes the recorded
      channel into the settings and starts it again. Then R3 compares and zaps back at
      most once.

    The order is restore, restart, prove the restart, release the lock, and the last of
    those is reached whatever the ones before it did. A receiver whose files are back
    and whose interface did not start is a receiver to restart by hand, not one to
    refuse the next install on; and even a restore that failed outright is better left
    unlocked, because the next attempt takes its own snapshot before it touches
    anything, while a lock nobody holds refuses every attempt until it goes stale.

    Each step that failed raises a `_RollbackError` naming the outcome, and the first
    failure wins because it is the one the later steps are working around. A
    cancellation is not an outcome: the release is still attempted, and then the
    cancellation is re-raised as itself. `progress` is how the caller learns about the
    lock in that case, and it is filled in whatever this raises.
    """
    progress = progress if progress is not None else _RollbackProgress()
    session: InstallerSession | None = None
    old_enigma_pids: set[int] = set()
    restore_error: BaseException | None = None
    restart_error: BaseException | None = None
    release_error: BaseException | None = None
    cancelled: asyncio.CancelledError | None = None
    r2_script, r2_status, r2_log = _r2_files(remote_helper)
    # Whether the helper and the script's files may be deleted afterwards: not while a
    # script this end lost sight of may still be using them.
    tidy = True
    try:
        session = await connector(credentials)
        # This one goes first and outside every condition. It is the file holding the
        # broker password in cleartext, deleting it needs nothing stopped, and if it is
        # left behind it is left on flash: a failure to stop Enigma below must not be
        # able to orphan it.
        with suppress(OSError, TimeoutError):
            await session.run(f"rm -f {shlex.quote(remote_provision_tmp)}", timeout=30)
        if not restart_started:
            commands = [f"rm -f {shlex.quote(remote_ipk)} {shlex.quote(remote_manifest)}"]
            if install_started:
                commands.append(
                    f"python3 {shlex.quote(remote_helper)} restore {shlex.quote(backup)}"
                    + (" --provisioning" if provision_started else "")
                )
            _LOGGER.info("Installer rollback: restoring the receiver from %s", backup)
            result = await session.run(
                "set -eu; " + "; ".join(commands), timeout=RESTORE_TIMEOUT
            )
            if install_started and result.exit_status == installer_helper.EXIT_OPKG_BUSY:
                # Nothing was restored: the helper takes opkg's lock before it touches a
                # file. Which plugin the receiver is on depends on how far the install
                # got - the likeliest way here is an opkg run from the receiver's menu
                # that also made the install's own `opkg install` fail on the lock, and
                # then nothing had changed - so this says "not restored", not "on the new
                # plugin", and apart from a restore that broke half-way.
                raise InstallerError(InstallerErrorCode.ROLLBACK_OPKG_BUSY)
            if install_started and result.exit_status == installer_helper.EXIT_OPKG_LOCK_LOST:
                # The files are back; what is in doubt is opkg's database, which an opkg
                # run may have written at the same time. "Could not be put back" would
                # send somebody to redo a restore that happened.
                raise InstallerError(InstallerErrorCode.ROLLBACK_OPKG_OVERLAP)
            if result.exit_status:
                raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
        else:
            # Nothing measured here may veto the restore, and the preflight is not
            # measured at all: it needs OpenWebif, which is an Enigma plugin and so is
            # down in exactly the failure this rollback exists for. The pids are read
            # because the restart proof below compares against them, and the channel
            # because R3 puts it back; failing to read either is not a reason to stop.
            with suppress(InstallerError):
                old_enigma_pids = await _async_enigma_pids(session)
            service, standby = await restart_rule.async_read_state(session, remote_helper)
            keep = _rollback_record(record, service, standby)
            with suppress(OSError, TimeoutError):
                await session.run(
                    f"rm -f {shlex.quote(remote_ipk)} {shlex.quote(remote_manifest)}",
                    timeout=30,
                )
            command = (
                f"python3 {shlex.quote(remote_helper)} r2-start {shlex.quote(backup)} "
                f"--script {shlex.quote(r2_script)} --status {shlex.quote(r2_status)} "
                f"--log {shlex.quote(r2_log)}"
                + (f" --service {shlex.quote(keep.service)}" if keep.service else "")
                + (" --provisioning" if provision_started else "")
            )
            _LOGGER.info(
                "Installer rollback: stopping the receiver interface, restoring from %s "
                "and starting it again, as one script on the receiver",
                backup,
            )
            started = await session.run(command, timeout=30)
            if started.exit_status:
                # The script never ran, so nothing was stopped: the receiver is on the
                # new plugin with its interface up.
                raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
            try:
                session, steps = await _async_follow_r2(
                    session, credentials, connector, r2_status
                )
                if "started" not in steps:
                    tidy = False
                    if "restored" in steps and (code := _r2_restore_code(steps)) is not None:
                        restore_error = InstallerError(code)
                    raise InstallerError(InstallerErrorCode.ROLLBACK_RESTART_FAILED)
                if (code := _r2_restore_code(steps)) is not None:
                    restore_error = InstallerError(code)
                if steps["started"] not in ("", "0"):
                    raise InstallerError(InstallerErrorCode.ROLLBACK_RESTART_FAILED)
                if session is None:
                    session = await connector(credentials)
                # A pid that was not there before the interface was stopped proves the
                # receiver came back; no pid beforehand means Enigma was already stopped
                # when the rollback began, and any live process is then the proof. It is
                # waited for rather than sampled, because `init 3` is answered long
                # before the interface is.
                restarted_pids = await _async_wait_for_enigma(session, old_enigma_pids)
                _LOGGER.info(
                    "Installer rollback: the receiver interface is running again as pid %s",
                    ", ".join(str(pid) for pid in sorted(restarted_pids)),
                )
                await _async_verify_restart(
                    session, remote_helper, keep, "stopped", hass, target
                )
            except BaseException as restart_err:
                restart_error = restart_err
                if isinstance(restart_err, asyncio.CancelledError):
                    cancelled = restart_err
    except BaseException as err:
        restore_error = err
        if isinstance(err, asyncio.CancelledError):
            cancelled = err
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                # Never a reason to skip the release below: the lock is on the receiver
                # and this is a socket on this end.
                _LOGGER.warning("Installer rollback: closing the SSH session failed")
    try:
        released = await connector(credentials)
        try:
            result = await released.run(
                f"python3 {shlex.quote(remote_helper)} release {shlex.quote(remote_lock)}",
                timeout=30,
            )
            if result.exit_status:
                raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
            progress.lock_released = True
            _LOGGER.info("Installer rollback: the transaction lock is released")
            if tidy:
                # The helper had to outlive the restore that used it; nothing needs it now.
                with suppress(Exception):
                    await released.run(
                        f"rm -f {shlex.quote(r2_script)} {shlex.quote(r2_status)} "
                        f"{shlex.quote(r2_log)}",
                        timeout=30,
                    )
                await _async_remove_helper(released, remote_helper)
            else:
                _LOGGER.warning(
                    "Installer rollback: the stop-and-restore script on the receiver did "
                    "not report the interface started in time; its status is in %s, and "
                    "the helper it uses was left in place",
                    r2_status,
                )
        finally:
            await released.close()
    except BaseException as release_err:
        release_error = release_err
        if cancelled is None and isinstance(release_err, asyncio.CancelledError):
            cancelled = release_err
        if progress.lock_released:
            # The lock went; something after it did not - closing the session, most
            # likely. Saying the receiver is locked here would send somebody to delete a
            # directory that is not there.
            _LOGGER.warning(
                "Installer rollback: the transaction lock was released, but tidying up "
                "after it did not finish",
                exc_info=release_err,
            )
        else:
            _LOGGER.error(
                "Installer rollback could not release the transaction lock at %s; until it is "
                "removed by hand or goes stale, every install on this receiver reports it as busy",
                remote_lock,
                exc_info=release_err,
            )
    # A cancellation is what it is, and it outranks every verdict below: those describe a
    # rollback that ran to its own conclusion, and this one did not.
    if cancelled is not None:
        raise cancelled
    if restore_error is not None:
        if restart_error is not None:
            restore_error.add_note("the receiver interface did not come back either")
        # The two opkg outcomes are verdicts of their own; everything else that went
        # wrong in the restore is the restore failing.
        code = (
            restore_error.code
            if isinstance(restore_error, InstallerError)
            and restore_error.code
            in (InstallerErrorCode.ROLLBACK_OPKG_BUSY, InstallerErrorCode.ROLLBACK_OPKG_OVERLAP)
            else InstallerErrorCode.ROLLBACK_FAILED
        )
        raise _RollbackError(code) from restore_error
    if restart_error is not None:
        raise _RollbackError(InstallerErrorCode.ROLLBACK_RESTART_FAILED) from restart_error
    if release_error is not None and not progress.lock_released:
        # The receiver is back as it was and the only thing wrong with it is a lock
        # nobody holds. Reporting the original failure alone would be true and useless:
        # the next attempt is refused as busy, which is the incident this came from.
        raise _RollbackError(InstallerErrorCode.ROLLBACK_LOCK_FAILED) from release_error


async def _async_verify_restart(
    session: InstallerSession,
    remote_helper: str,
    record: restart_rule.RestartRecord,
    restart: str,
    hass: HomeAssistant | None,
    target: tuple[str, str] | None,
) -> restart_rule.RestartOutcome | None:
    """R3, never a reason to fail: the restart it looks at has already happened.

    The outcome goes into the log as the transaction's record of the restart - what
    `docs/TRANSACTION.md` in the plugin's repository calls `restart`, `channel`,
    `bouquet` and `standby`.
    """
    try:
        outcome = await restart_rule.async_verify(
            session, remote_helper, record, restart=restart
        )
        if hass is not None and target is not None:
            outcome.bouquet = await restart_rule.async_restore_bouquet(
                hass, target[0], target[1], record, outcome.channel
            )
    except Exception:
        _LOGGER.warning(
            "The receiver restarted, but whether it kept its channel could not be checked",
            exc_info=True,
        )
        return None
    _LOGGER.info("Receiver interface restart: %s", outcome.describe())
    return outcome


async def _async_recover_abandoned(
    session: InstallerSession, remote_helper: str, claimed: str, nonce: str
) -> None:
    """Put back what an abandoned installer transaction may have left half-done.

    The claim reports the id of an installer transaction whose lock it reclaimed as
    stale - one that never released it: its connection died, Home Assistant restarted,
    the receiver lost power. That transaction writes no record of how far it got, so
    this cannot say whether it had begun to change or to restore anything, and restores
    its snapshot again, which is safe to repeat. Which snapshot is decided by the id in
    the lock's owner record, never by a file time: many receivers have no battery-
    backed clock, and "newest" is then whatever NTP made of it.

    The interface is running, so this is the restore that runs under it: files and
    opkg's metadata only, by renames, never the settings block.
    """
    try:
        reclaimed = json.loads(claimed or "{}").get("reclaimed", "")
    except (AttributeError, ValueError):
        return
    if (
        not isinstance(reclaimed, str)
        or not installer_helper.TRANSACTION_ID.fullmatch(reclaimed)
        or reclaimed == nonce
    ):
        return
    abandoned = f"{BACKUP_ROOT}/ha-installer-{reclaimed}"
    present = await session.run(f"test -d {shlex.quote(abandoned)}", timeout=30)
    if present.exit_status:
        return
    _LOGGER.warning(
        "Installer: transaction %s was abandoned without releasing its lock; putting "
        "back its snapshot %s before this one starts",
        reclaimed,
        abandoned,
    )
    await _run_checked(
        session,
        f"python3 {shlex.quote(remote_helper)} withdraw {shlex.quote(abandoned)} --provisioning",
        InstallerErrorCode.INSTALL_FAILED,
        timeout=RESTORE_TIMEOUT,
        detail="the snapshot of an abandoned transaction could not be put back",
        busy=InstallerErrorCode.OPKG_BUSY,
    )


async def _async_clean_restart(
    session: InstallerSession, remote_helper: str, old_pids: set[int]
) -> bool:
    """R1: ask for the image's own clean restart and wait for a new enigma2.

    OpenWebif's power state 3 is `TryQuitMainloop(3)`: the image stops its services,
    saves its settings - the channel being watched among them - and exits, and init
    starts it again. `init 4` never got to the save, which is how a receiver came back
    on a channel saved hours earlier.

    Judged by the pid, never by the call's answer: the quit can reset the connection
    that asked for it. True as soon as a new enigma2 runs. At the bound, an interface
    that went away and did not come back is a restart too - a failed one, which the
    proof then turns into a rollback. False only when the old enigma2 is still there,
    unchanged: the image is asking a question on the television, which waits for ever.
    """
    with suppress(OSError, TimeoutError):
        await session.run(
            f"python3 {shlex.quote(remote_helper)} powerstate --state 3", timeout=30
        )
    deadline = time.monotonic() + RESTART_QUESTION_TIMEOUT
    pids = set(old_pids)
    while True:
        with suppress(InstallerError):
            pids = await _async_enigma_pids(session)
            if pids - old_pids:
                return True
        if time.monotonic() >= deadline:
            return not old_pids <= pids
        await asyncio.sleep(RESTART_POLL_SECONDS)


async def _async_withdraw(
    session: InstallerSession, remote_helper: str, backup: str, provision_started: bool
) -> bool:
    """Put the old files back under a running enigma2; return whether it restarted meanwhile.

    Files and opkg's metadata only, never the settings block: the transaction changed no
    setting, and a block written while enigma2 runs is overwritten from memory by its
    next clean quit - writing it would only race the household's own saves. The helper
    swaps the plugin directory in by two renames, so a restart answered in the middle
    meets the whole old tree or the whole new one.

    Then the pid is read again. A new one means somebody answered the question while
    the files went back; the receiver restarted, and "withdrawn" would be a false
    report - the caller goes on to the proof, and to R2 if it fails.
    """
    _LOGGER.warning(
        "Installer: the receiver's interface did not restart within %d s - the image is "
        "asking on the television - so the update is withdrawn and the previous files "
        "put back",
        int(RESTART_QUESTION_TIMEOUT),
    )
    before = await _async_enigma_pids(session)
    await _run_checked(
        session,
        f"python3 {shlex.quote(remote_helper)} withdraw {shlex.quote(backup)}"
        + (" --provisioning" if provision_started else ""),
        InstallerErrorCode.ROLLBACK_FAILED,
        timeout=RESTORE_TIMEOUT,
        detail="the previous plugin files could not be put back under the running interface",
        busy=InstallerErrorCode.ROLLBACK_OPKG_BUSY,
    )
    return await _async_enigma_pids(session) != before


async def _async_release_after_withdraw(
    session: InstallerSession,
    remote_helper: str,
    remote_lock: str,
    *leftovers: str,
) -> bool:
    """Release the lock after a withdrawal and tidy up; return whether it was released."""
    try:
        released = await session.run(
            f"python3 {shlex.quote(remote_helper)} release {shlex.quote(remote_lock)}",
            timeout=30,
        )
    except (OSError, TimeoutError):
        return False
    if released.exit_status:
        return False
    with suppress(OSError, TimeoutError):
        await session.run(
            "rm -f " + " ".join(shlex.quote(path) for path in (*leftovers, remote_helper)),
            timeout=30,
        )
    return True


async def async_install(
    hass: HomeAssistant,
    request: InstallRequest,
    progress_cb: ProgressCallback | None = None,
    *,
    _connector: Connector = _async_connect,
) -> InstallResult:
    """Serialize transactions for one physical SSH endpoint."""
    host = (request.credentials.host, request.credentials.port)
    if host in _ACTIVE_HOSTS:
        raise InstallerError(InstallerErrorCode.BUSY)
    _ACTIVE_HOSTS.add(host)
    try:
        return await _async_install_locked(hass, request, progress_cb, _connector)
    finally:
        _ACTIVE_HOSTS.discard(host)


async def _async_install_locked(
    hass: HomeAssistant,
    request: InstallRequest,
    progress_cb: ProgressCallback | None = None,
    connector: Connector = _async_connect,
) -> InstallResult:
    """Install or update the bundled plugin as a rollback-safe transaction."""
    request.validate()
    bundle = await _async_load_bundle(hass)
    bundle_bytes = await hass.async_add_executor_job(Path(bundle.path).read_bytes)
    if hashlib.sha256(bundle_bytes).hexdigest() != bundle.sha256:
        raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
    installed_manifest = await hass.async_add_executor_job(_installed_hash_manifest, bundle_bytes)

    nonce = secrets.token_hex(6)
    remote_ipk = f"/tmp/{PACKAGE}-{nonce}.ipk"
    remote_helper = f"/tmp/enigma2-mqtt-installer-{nonce}.py"
    remote_manifest = f"/tmp/enigma2-mqtt-manifest-{nonce}"
    # Beside the real file so the rename is atomic, but never at a name an attacker
    # could have pre-created as a symlink: this file holds the broker password, and a
    # fixed name following a planted link would write it wherever the link points.
    remote_provision_tmp = f"{PROVISION_PATH}.ha-{nonce}"
    backup_name = f"ha-installer-{nonce}"
    backup = f"{BACKUP_ROOT}/{backup_name}"
    remote_lock = f"{BACKUP_ROOT}/.ha-installer.lock"
    session: InstallerSession | None = None
    watch: _RestartWatch | None = None
    install_started = False
    provision_started = False
    restart_started = False
    backup_created = False
    remote_lock_claimed = False
    committed = False
    # The image asked a question, the install was withdrawn and the lock released.
    withdrawn = False
    record: restart_rule.RestartRecord | None = None
    provisioned_name = ""
    try:
        # Everything up to the transaction lock is a guard, and a guard that refuses
        # leaves the receiver as it found it. Each of those refusals is written to the
        # log here - the one place that knows an install was being attempted - because
        # the sentence a user is shown lives on a config-flow screen that is gone as
        # soon as it is read, and afterwards there was nothing at all to say one had
        # even happened. The guards themselves log nothing: the same code runs for the
        # options screen's no-write probe, where there is no install to refuse.
        try:
            await _async_progress(progress_cb, "preflight")
            session = await connector(request.credentials)
            preflight = await _async_measure_preflight(
                session, bundle_size=len(bundle_bytes), restart_guards=True
            )
            if preflight.installed_version and _package_version(
                preflight.installed_version
            ) > _package_version(bundle.version):
                raise InstallerError(
                    InstallerErrorCode.NEWER_INSTALLED,
                    f"the receiver runs plugin {preflight.installed_version} and this "
                    f"integration ships {bundle.version}",
                )

            await _async_progress(progress_cb, "backup")
            helper_bytes = await hass.async_add_executor_job(
                Path(installer_helper.__file__).read_bytes
            )
            await _run_checked(
                session,
                f"umask 077; cat > {shlex.quote(remote_helper)}",
                InstallerErrorCode.INSTALL_FAILED,
                input=helper_bytes,
                detail="the installer helper could not be written to the receiver's /tmp",
            )
            stored_name = await _async_validate_receiver_identity(
                session, remote_helper, request
            )
        except InstallerError as err:
            _log_install_refusal(err)
            raise
        claim = await session.run(
            f"mkdir -p {shlex.quote(BACKUP_ROOT)} && "
            f"python3 {shlex.quote(remote_helper)} claim {shlex.quote(remote_lock)} "
            f"--id {nonce}",
            timeout=30,
        )
        if claim.exit_status:
            raise InstallerError(InstallerErrorCode.BUSY)
        remote_lock_claimed = True
        await _async_recover_abandoned(session, remote_helper, claim.stdout, nonce)
        await _run_checked(
            session,
            f"python3 {shlex.quote(remote_helper)} snapshot {shlex.quote(backup)}",
            InstallerErrorCode.INSTALL_FAILED,
            timeout=SNAPSHOT_TIMEOUT,
            busy=InstallerErrorCode.OPKG_BUSY,
        )
        backup_created = True

        await _async_progress(progress_cb, "upload")
        await _run_checked(
            session,
            f"umask 077; cat > {shlex.quote(remote_ipk)}",
            InstallerErrorCode.UPLOAD_FAILED,
            input=bundle_bytes,
            timeout=60,
        )
        remote_hash = await _run_checked(
            session,
            f"sha256sum {shlex.quote(remote_ipk)} | awk '{{print $1}}'",
            InstallerErrorCode.UPLOAD_FAILED,
        )
        if remote_hash.stdout.strip() != bundle.sha256:
            raise InstallerError(InstallerErrorCode.UPLOAD_FAILED)

        await _async_measure_preflight(
            session, bundle_size=len(bundle_bytes), restart_guards=True
        )
        await _async_progress(progress_cb, "install")
        install_started = True
        await _run_checked(
            session,
            f"opkg install --force-reinstall {shlex.quote(remote_ipk)}",
            InstallerErrorCode.INSTALL_FAILED,
            timeout=120,
        )
        await _run_checked(
            session,
            f"umask 077; cat > {shlex.quote(remote_manifest)}",
            InstallerErrorCode.INSTALL_FAILED,
            input=installed_manifest,
        )
        await _run_checked(
            session,
            f"python3 {shlex.quote(remote_helper)} verify {shlex.quote(remote_manifest)}",
            InstallerErrorCode.INSTALL_FAILED,
        )
        if request.provisioning is not None:
            await _async_progress(progress_cb, "provision")
            provision_started = True
            # What the receiver ends up called, which an empty name field leaves to the
            # receiver: the document omits the key, the plugin keeps the name it holds,
            # and this is only the answer to what that name is. Reported rather than
            # written back - a value the box already holds is not this transaction's to
            # rewrite, and writing it would make an install that changed nothing
            # indistinguishable from one that renamed the box to the same thing.
            provisioned_name = request.provisioning.display_name() or stored_name
            provisioning = request.provisioning.as_json()
            quoted_tmp = shlex.quote(remote_provision_tmp)
            await _run_checked(
                session,
                f"set -eu; umask 077; "
                f"if [ -e {quoted_tmp} ] || [ -L {quoted_tmp} ]; then exit 1; fi; "
                f"cat > {quoted_tmp}",
                InstallerErrorCode.PROVISION_FAILED,
                input=provisioning,
            )
            await _run_checked(
                session,
                f"set -eu; if [ -L {quoted_tmp} ]; then exit 1; fi; "
                f"chmod 600 {quoted_tmp}; mv {quoted_tmp} {PROVISION_PATH}",
                InstallerErrorCode.PROVISION_FAILED,
            )

        # The guard is measured again immediately before the only disruptive step.
        await _async_measure_preflight(
            session, bundle_size=len(bundle_bytes), restart_guards=True
        )
        # Read before the restart, so that afterwards a process that was not running
        # then is proof the interface really went down and came back. An empty set is a
        # perfectly ordinary answer - a box still booting has no Enigma yet - and any
        # pid at all afterwards is then the proof.
        old_enigma_pids = await _async_enigma_pids(session)
        base_topic, node_id = request.target()
        watch = await _async_watch_restart(
            hass,
            base_topic,
            node_id,
            bundle.version,
            require_offline=request.expect_running,
        )
        try:
            async with asyncio.timeout(5):
                await watch.established
        except TimeoutError as err:
            raise InstallerError(InstallerErrorCode.RESTART_FAILED) from err

        # What the restart has to keep - the channel, standby, the channel-list bouquet -
        # recorded as late as possible, so that it is what the household is watching.
        record = await restart_rule.async_record(
            hass, session, remote_helper, base_topic, node_id
        )

        await _async_progress(progress_cb, "restart")
        restart_started = True
        watch.arm()
        if not await _async_clean_restart(session, remote_helper, old_enigma_pids):
            # The image asked on the television instead of restarting. Nothing was
            # stopped; the old enigma2 still runs. Put the old files back under it and
            # say so - unless somebody answered while that happened, in which case the
            # receiver did restart, and the proof below decides as for any restart.
            try:
                restarted_meanwhile = await _async_withdraw(
                    session, remote_helper, backup, provision_started
                )
            except InstallerError:
                # Not put back - opkg busy, most likely. Still no stop-and-restore: that
                # would stop the interface the image has just asked to keep running.
                withdrawn = True
                remote_lock_claimed = not await _async_release_after_withdraw(
                    session, remote_helper, remote_lock
                )
                raise
            if not restarted_meanwhile:
                withdrawn = True
                remote_lock_claimed = not await _async_release_after_withdraw(
                    session, remote_helper, remote_lock, remote_ipk, remote_manifest,
                    remote_provision_tmp,
                )
                if remote_lock_claimed:
                    raise InstallerError(InstallerErrorCode.ROLLBACK_LOCK_FAILED)
                raise InstallerError(
                    InstallerErrorCode.RESTART_WITHDRAWN,
                    "the receiver asked on screen whether to restart its interface; the "
                    "update was withdrawn and the previous plugin files are back",
                )
        try:
            await session.close()
        except BaseException:
            _LOGGER.warning("SSH close failed after receiver restart; continuing verification")
        session = None
        await _async_progress(progress_cb, "announcement")
        try:
            async with asyncio.timeout(ANNOUNCEMENT_TIMEOUT):
                await watch.completed
        except TimeoutError as err:
            raise InstallerError(InstallerErrorCode.ANNOUNCEMENT_TIMEOUT) from err

        cleanup = await connector(request.credentials)
        try:
            if not await _async_enigma_pids(cleanup) - old_enigma_pids:
                raise InstallerError(InstallerErrorCode.RESTART_FAILED)
            await _async_verify_restart(
                cleanup, remote_helper, record, "clean", hass, (base_topic, node_id)
            )
            released = await cleanup.run(
                f"python3 {shlex.quote(remote_helper)} release {shlex.quote(remote_lock)}",
                timeout=30,
            )
            if released.exit_status:
                raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
            remote_lock_claimed = False
            committed = True
            # Before the helper is deleted, and never a reason to fail an install that
            # is already committed: the snapshot just taken is the way back from this
            # install, and the ones from earlier installs have been superseded by it.
            try:
                pruned = await cleanup.run(
                    f"python3 {shlex.quote(remote_helper)} prune {shlex.quote(BACKUP_ROOT)} "
                    f"--keep-name {shlex.quote(backup_name)}",
                    timeout=30,
                )
                if pruned.exit_status:
                    _LOGGER.warning(
                        "Installed plugin verified; superseded installer snapshots "
                        "remain on the receiver under %s",
                        BACKUP_ROOT,
                    )
            except BaseException:
                _LOGGER.warning(
                    "Installed plugin verified; superseded installer snapshots remain "
                    "on the receiver under %s",
                    BACKUP_ROOT,
                )
            try:
                await cleanup.run(
                    f"rm -f {shlex.quote(remote_ipk)} {shlex.quote(remote_helper)} "
                    f"{shlex.quote(remote_manifest)} {shlex.quote(remote_provision_tmp)}",
                    timeout=30,
                )
            except BaseException:
                _LOGGER.warning("Installed plugin verified; temporary-file cleanup failed")
        finally:
            try:
                await cleanup.close()
            except BaseException:
                if not committed:
                    raise
                _LOGGER.warning("Installed plugin verified; cleanup SSH close failed")
        try:
            await _async_progress(progress_cb, "done")
        except BaseException:
            _LOGGER.warning("Installed plugin verified; final progress callback failed")
        return InstallResult(bundle.version, backup_created, True, provisioned_name)
    except BaseException as err:
        if committed:
            _LOGGER.warning("Installed plugin verified; ignoring post-commit cleanup failure")
            return InstallResult(bundle.version, backup_created, True, provisioned_name)
        if withdrawn:
            # Settled on the receiver already: the old files are back under the old
            # enigma2 and nothing was stopped. A rollback now would stop the interface
            # the image just refused to restart.
            raise
        if session is not None:
            try:
                await session.close()
            except BaseException:
                _LOGGER.warning("SSH close failed while preparing installer rollback")
            session = None
        if backup_created:
            rollback = _RollbackProgress()
            try:
                await _async_rollback(
                    request.credentials,
                    backup,
                    remote_ipk,
                    remote_helper,
                    remote_manifest,
                    remote_provision_tmp,
                    remote_lock,
                    install_started,
                    provision_started,
                    restart_started,
                    connector,
                    rollback,
                    record=record,
                    hass=hass,
                    target=request.target(),
                )
            except asyncio.CancelledError:
                _LOGGER.error(
                    "Installer rollback was cancelled; receiver backup remains at %s", backup
                )
                raise
            except Exception as rollback_err:
                code = (
                    rollback_err.code
                    if isinstance(rollback_err, _RollbackError)
                    else InstallerErrorCode.ROLLBACK_FAILED
                )
                # The cause is the only thing that says which step went wrong, and it
                # used to be dropped here: the log said a rollback had failed and the
                # receiver was the only place left to find out why.
                _LOGGER.error(
                    "Installer rollback ended as %s; receiver backup remains at %s",
                    code.value,
                    backup,
                    exc_info=rollback_err,
                )
                if isinstance(err, asyncio.CancelledError):
                    err.add_note(f"rollback failed; backup remains at {backup}")
                else:
                    raise InstallerError(code) from rollback_err
            finally:
                # Whatever the rollback raised, including a cancellation: this is the
                # difference between an error line about a receiver that is fine and
                # silence about one that will refuse the next install.
                if rollback.lock_released:
                    remote_lock_claimed = False
        elif remote_lock_claimed:
            try:
                release = await connector(request.credentials)
                try:
                    result = await release.run(
                        f"python3 {shlex.quote(remote_helper)} release {shlex.quote(remote_lock)}",
                        timeout=30,
                    )
                    if result.exit_status:
                        raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
                    remote_lock_claimed = False
                    await _async_remove_helper(release, remote_helper)
                finally:
                    await release.close()
            except Exception as release_err:
                _LOGGER.error(
                    "Installer transaction lock remains on %s:%s",
                    request.credentials.host,
                    request.credentials.port,
                    exc_info=release_err,
                )
                if not isinstance(err, asyncio.CancelledError):
                    raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED) from release_err
        raise
    finally:
        if watch is not None:
            watch.cancel()
        if session is not None:
            try:
                await session.close()
            except BaseException:
                _LOGGER.warning("SSH close failed during installer cleanup")
        if remote_lock_claimed:
            _LOGGER.error(
                "Installer transaction lock remains on %s:%s",
                request.credentials.host,
                request.credentials.port,
            )

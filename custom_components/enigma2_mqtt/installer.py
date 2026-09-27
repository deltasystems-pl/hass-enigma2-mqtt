"""Transactional SSH installer for the receiver plugin.

The installer deliberately has no discovery, release-check, or download path of its own. It
installs one of two packages: the IPK bundled with this integration, after the bundle loader
verifies its provenance and digest, or a release the signed index lists, handed to it as bytes
that `release_package` has already held against the signed entry's size and sha256 - and whose
checksum it checks once more before a byte reaches the receiver. SSH host identity is
explicitly confirmed by the config flow and pinned on every authenticated connection.
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

from . import installer_helper, release_package, restart_rule
from .box import command_topic, parse_json_payload, same_service, state_topic
from .bundle import BundledPlugin, BundleError, load_bundled_plugin
from .const import (
    DEFAULT_BASE_TOPIC,
    HA_MODE_INTEGRATION,
    PLUGIN_SETTING_DEFAULTS,
    TOPIC_INFO,
)
from .plugin_versions import bundle_refusal
from .release_index import release_of
from .release_package import PackageSource
from .release_store import async_release_index_cache

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
R2_FOLLOW_TIMEOUT = (
    installer_helper.R2_STOP_WAIT_SECONDS + installer_helper.R2_RESTORE_LIMIT_SECONDS + 60.0
)
# When the answer to the command that starts the script was lost, how long a missing
# status file means only "not yet": Python has to start and import on the receiver
# before the script's directory exists. After it, the receiver itself is asked whether
# the directory or any process naming it exists.
R2_START_GRACE = 30.0
R2_FOLLOW_POLL_SECONDS = 2.0
# The forced reinstall reads the interface's state before any guard that needs OpenWebif:
# `pidof enigma2` this many times, this far apart - three samples over ten seconds, so
# that a respawn gap (absent once, present on the next look) is not taken for a respawn
# loop (absent on every look).
FORCE_GUI_SAMPLES = 3
FORCE_GUI_SAMPLE_SECONDS = 5.0
# How often the forced reinstall's proof looks for the plugin's log in the new process.
FORCE_PROOF_POLL_SECONDS = 3.0
# A self-update's helper rewrites the lock's owner record every 60 s. Three missed beats
# say it stopped; the lock is still left to the released rule, and the refusal only says
# when that rule frees it.
STALLED_SILENCE_SECONDS = 180
# What a forced reinstall's preflight reports for opkg records it could not parse.
UNREADABLE_RECORDS = "unreadable"

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
    # A restart was asked for and its outcome could not be seen - the connection was
    # lost and did not come back. Nothing is undone, and the lock stays on the receiver
    # so that the next install recovers the transaction by its id.
    RESTART_UNOBSERVED = "restart_unobserved"
    # OpenWebif did not confirm the restart request and nothing restarted within the
    # bound: withdrawn like a question, but not described as one.
    RESTART_UNCONFIRMED = "restart_unconfirmed"
    # Nothing restarted within the bound and the old files could not be put back: the
    # new ones are on the receiver, and a question may still be on the television.
    WITHDRAW_FAILED = "withdraw_failed"
    # The stop-and-restore script of a rollback was started and its end was not seen.
    # It goes on by itself on the receiver; the lock stays until it is stale.
    ROLLBACK_UNOBSERVED = "rollback_unobserved"
    ROLLBACK_FAILED = "rollback_failed"
    ROLLBACK_RESTART_FAILED = "rollback_restart_failed"
    ROLLBACK_LOCK_FAILED = "rollback_lock_failed"
    ROLLBACK_OPKG_BUSY = "rollback_opkg_busy"
    ROLLBACK_OPKG_OVERLAP = "rollback_opkg_overlap"
    BUSY = "busy"
    OPKG_BUSY = "opkg_busy"
    # The last verified signed index rules the bundle out - below its floor, or withdrawn -
    # on every install path, before the receiver is connected to.
    BUNDLE_BELOW_FLOOR = "bundle_below_floor"
    BUNDLE_WITHDRAWN = "bundle_withdrawn"
    # The same rule for a release of the index handed to the installer as bytes: checked
    # when the bytes were fetched, and again here - the whole rule - because an index
    # accepted in between may have withdrawn the version or raised the floor.
    VERSION_BELOW_FLOOR = "version_below_floor"
    VERSION_WITHDRAWN = "version_withdrawn"
    # The rest of the rule, asked again the same way: the newest index held no longer lists
    # the version, has moved it to another contract, or asks for a newer integration.
    VERSION_NOT_INSTALLABLE = "version_not_installable"
    # Verified bytes that are no longer the ones that were verified - or no longer the ones
    # the newest index held signs under that version.
    PACKAGE_INVALID = "package_invalid"
    # A package the release needs is not installed on the receiver.
    DEPENDS_MISSING = "depends_missing"
    IDENTITY_MISMATCH = "identity_mismatch"
    # The lock is held by a plugin self-update whose helper stopped beating: refused like
    # any busy lock - the released rule frees it, nothing earlier - with when that is.
    BUSY_STALLED = "busy_stalled"
    # opkg's records name a newer plugin than the one that runs and than the one to be
    # installed: the residue of a restore that did not complete. Not "a newer plugin is
    # installed"; the forced reinstall is the repair.
    RECORDS_MISMATCH = "records_mismatch"
    # The forced reinstall's GUI-state table (ADR-0008, section 8): an interface that runs
    # but whose OpenWebif is silent, and one stopped on purpose (runlevel 4, no stop of
    # ours).
    GUI_NOT_ANSWERING = "gui_not_answering"
    GUI_STOPPED = "gui_stopped"
    # A forced reinstall's new interface never opened the plugin's log: it did not load it.
    PLUGIN_NOT_STARTED = "plugin_not_started"


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
    # The plugin's permission to obey `cmd/update` over MQTT (the guided installer's tick box).
    update_allowed: bool = False

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
        # A permission is written only when it is given. The document is applied key by key, so
        # an unticked box leaves whatever the receiver holds - off as shipped - and an older
        # plugin that does not know the key logs it and ignores it. This is the one permission
        # the installer writes (it supersedes "the installer never writes a permission" for
        # this key alone): whoever ticks it is setting the receiver up, only through Home
        # Assistant rather than at the television.
        if self.update_allowed:
            values["update_allowed"] = True
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
    # What to install: None for the bundle, or a release of the signed index as verified bytes.
    package: PackageSource | None = None
    # A downgrade confirmed in the options flow. Only then may the package be older than the
    # plugin on the receiver - opkg is told so - and the older plugin is asked to retract
    # what the newer one published (`cmd/reset`) once it has proved itself.
    downgrade: bool = False
    # The forced reinstall (ADR-0008, section 8): the bundle only, over any version, into
    # whatever state the interface is in, proved without waiting for the plugin to answer.
    force: bool = False
    # The version the running plugin reports over MQTT, when the caller knows it. opkg's
    # records are what opkg believes was installed, and after a restore that did not
    # complete they name a version that is not on disk; they are never read as this.
    running_version: str | None = None

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
    # Whether the plugin is switched on in its own settings. A forced reinstall installs
    # and proves a switched-off plugin, changes no setting, and says so.
    plugin_enabled: bool = True


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
        try:
            result = await self._connection.run(command, input=input, timeout=timeout)
        except TimeoutError:
            raise
        except asyncssh.Error as err:
            # A connection that closed under the command, a channel that could not be
            # opened: the same thing to every caller as a socket that went away, and
            # every caller already handles that one.
            raise ConnectionError(str(err)) from err
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
    except (OSError, TimeoutError, asyncssh.Error) as err:
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
    session: InstallerSession,
    *,
    bundle_size: int = 0,
    restart_guards: bool = False,
    webif: bool = True,
    strict_records: bool = True,
) -> Preflight:
    """Measure every install guard through fixed receiver-local commands.

    `restart_guards` adds the two that only a restart of the interface needs - standby
    and streaming - and only the install path asks for them.

    Two relaxations exist for the forced reinstall alone. `webif=False` skips every guard
    OpenWebif answers - recording, timers, standby, streaming - which the forced mode asks
    for only when there is no enigma2 to answer them: nothing records without it.
    `strict_records=False` reads a version opkg's records give in a form this installer
    does not parse as "unknown" rather than refusing, because the forced reinstall exists
    for exactly the receiver whose records went wrong.
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
    try:
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
    except InstallerError:
        if strict_records:
            raise
        _LOGGER.warning(
            "Forced reinstall: opkg's records of the plugin could not be read (%s); they "
            "are treated as naming an unknown version",
            (installed or "no version")[:80],
        )
        installed = UNREADABLE_RECORDS
    if not webif:
        return Preflight(
            image, python_version, min(free_values), installed, plugin_files_present, False, 0
        )
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
    except (OSError, TimeoutError, asyncssh.Error) as err:
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
    return _check_receiver_identity(
        await _async_read_receiver_identity(session, remote_helper), request
    )


async def _async_read_receiver_identity(
    session: InstallerSession, remote_helper: str
) -> dict[str, Any]:
    """Read and type-check the settings that bind a receiver to an entry."""
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
    return identity


def _check_receiver_identity(identity: dict[str, Any], request: InstallRequest) -> str:
    """Refuse a receiver that is not the entry's; return the name it stores.

    The forced reinstall relaxes exactly one thing: the plugin may be switched off. It is
    the recovery path for a plugin that does not run, and switching it off in its own
    settings is the one way of not running that breaks no file - and the reinstall never
    changes `enabled`, so it stays off. The node id, the base topic and the mode still
    have to be this entry's: a forced reinstall of somebody else's receiver is not a
    recovery.
    """
    stored_node = identity["node_id"]
    stored_name = identity["friendly_name"]
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
            or (configured_enabled is not True and not request.force)
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
    commit: str | None = None,
) -> _RestartWatch:
    """Follow the receiver's restart to the announcement of the plugin just installed.

    Proof is a fresh (never retained) `online` - after an `offline` when a plugin was running
    before - and an `info` naming the installed version in `integration` mode. When the plugin
    reports its build (`info.build`, from the release after 0.3.0) and the package's commit is
    known, the build has to be that commit too: a same-number build that is not the one
    installed is not the proof of this install. A plugin that predates build ids proves by its
    version alone.
    """
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
            and _same_build(info.get("build"), commit)
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


def _same_build(build: Any, commit: str | None) -> bool:
    """Whether an `info.build` is the build of `commit` - or says nothing either way."""
    if not commit or not isinstance(build, dict):
        return True
    reported = build.get("commit")
    if not isinstance(reported, str) or not reported:
        return True
    return reported == commit


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


def _r2_dir(remote_helper: str) -> str:
    """Return the stop-and-restore script's own directory on the receiver.

    Named after the transaction, beside the helper in `/tmp`, so it goes away with a
    reboot and never collides with another transaction's. It holds the script's own
    copy of the helper, so nothing this end tidies up can pull a file from under it.
    """
    return remote_helper.removesuffix(".py").replace(
        "enigma2-mqtt-installer-", "enigma2-mqtt-r2-"
    )


def _r2_lines(stdout: str) -> dict[str, str]:
    """Read the script's status file into `{step: argument}`; the last word wins."""
    steps: dict[str, str] = {}
    for line in stdout.splitlines():
        step, _, argument = line.strip().partition(" ")
        if step:
            steps[step] = argument
    return steps


async def _async_r2_absent(session: InstallerSession, r2_dir: str) -> bool:
    """Ask the receiver whether the script's directory, and any process naming it, is absent.

    The path is assembled on the receiver from two halves, so this command's own command
    line - which the scan of `/proc` also reads - never contains it whole; `case` is a
    shell builtin, so no child's arguments carry it either.
    """
    head, tail = r2_dir[:-4], r2_dir[-4:]
    result = await session.run(
        f"d=$(printf '%s%s' {shlex.quote(head)} {shlex.quote(tail)}); "
        'test -e "$d" && exit 1; '
        "for f in /proc/[0-9]*/cmdline; do "
        "c=$(tr '\\000' ' ' < \"$f\" 2>/dev/null) || continue; "
        'case "$c" in *"$d"*) exit 1;; esac; '
        "done; exit 0",
        timeout=30,
    )
    return result.exit_status == 0


async def _async_follow_r2(
    session: InstallerSession | None,
    credentials: SshCredentials,
    connector: Connector,
    status_file: str,
    *,
    answered: bool = True,
) -> tuple[InstallerSession | None, dict[str, str], bool]:
    """Follow the stop-and-restore script until it has started the interface again.

    It runs on the receiver whatever happens here. A dropped connection is only a gap in
    what this end can see, so the following reconnects and carries on; the script, which
    is in a session of its own, notices nothing. The last value says whether the script
    exists at all.

    When `r2-start` answered, its status file exists, and a missing one means it never
    will. When the answer was lost, a missing file may only be early: the command may
    still be starting on the receiver. Then "never started" is concluded only after a
    grace period, and only when the receiver has neither the script's directory nor a
    process naming it - otherwise the script is followed like any other.
    """
    started_at = time.monotonic()
    deadline = started_at + R2_FOLLOW_TIMEOUT
    r2_dir = status_file.rsplit("/", 1)[0]
    steps: dict[str, str] = {}
    while True:
        try:
            if session is None:
                session = await connector(credentials)
            result = await session.run(f"cat {shlex.quote(status_file)}", timeout=30)
            if result.exit_status:
                if answered or (
                    time.monotonic() - started_at >= R2_START_GRACE
                    and await _async_r2_absent(session, r2_dir)
                ):
                    return session, steps, False
            else:
                steps = _r2_lines(result.stdout)
                if "started" in steps:
                    return session, steps, True
        except (InstallerError, *_CONNECTION_LOST):
            await _async_drop(session)
            session = None
        if time.monotonic() >= deadline:
            return session, steps, True
        await asyncio.sleep(R2_FOLLOW_POLL_SECONDS)


def _r2_restore_code(steps: dict[str, str]) -> InstallerErrorCode | None:
    """Return the verdict on the restore the script ran, or None when it succeeded.

    `restored lost` - a restore gone without writing its exit status, killed - is a
    failure like any other status but the helper's two opkg ones.
    """
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
    r2_dir = _r2_dir(remote_helper)
    r2_status = f"{r2_dir}/status"
    # False from the moment the stop-and-restore script may be running until its end is
    # seen. While it is False the lock is not released and nothing of the script's is
    # deleted: it goes on by itself on the receiver, and the lock with this
    # transaction's id is what a later install recovers it by.
    tidy = True
    # Whether the script's end was seen - the only thing that allows removing its files.
    r2_finished = False
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
                f"--dir {shlex.quote(r2_dir)}"
                + (f" --service {shlex.quote(keep.service)}" if keep.service else "")
                + (" --provisioning" if provision_started else "")
            )
            _LOGGER.info(
                "Installer rollback: stopping the receiver interface, restoring from %s "
                "and starting it again, as one script on the receiver",
                backup,
            )
            # Whether it started is decided by its status file, never by this command's
            # answer: a connection lost under it says nothing about the receiver.
            tidy = False
            answered = True
            try:
                started = await session.run(command, timeout=30)
            except _CONNECTION_LOST:
                answered = False
                await _async_drop(session)
                session = None
            else:
                if started.exit_status:
                    # The script never ran, so nothing was stopped: the receiver is on
                    # the new plugin with its interface up.
                    tidy = True
                    raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
            try:
                session, steps, present = await _async_follow_r2(
                    session, credentials, connector, r2_status, answered=answered
                )
                if present and "started" not in steps:
                    raise InstallerError(InstallerErrorCode.ROLLBACK_UNOBSERVED)
                tidy = True
                r2_finished = present
                if (code := _r2_restore_code(steps)) is not None:
                    restore_error = InstallerError(code)
                if not present:
                    # The script never started - its status file is not there - so
                    # nothing was stopped and nothing restored.
                    restore_error = InstallerError(
                        InstallerErrorCode.ROLLBACK_FAILED,
                        "the stop-and-restore script did not start",
                    )
                elif "stop_timeout" in steps:
                    # enigma2 never stopped. The script put the files back and left the
                    # settings and the channel to the running interface, which writes its
                    # own over anything written now; nothing restarted. "The files were
                    # put back" is only true of a restore that succeeded: a restore
                    # refused because opkg was busy put nothing back, and its own verdict
                    # says so. The overlap's verdict says the receiver was put back as it
                    # was, which is false without the settings, so it is a failure here.
                    if restore_error is None:
                        restore_error = InstallerError(
                            InstallerErrorCode.ROLLBACK_FAILED,
                            "the receiver's interface did not stop, so only the plugin's "
                            "files were put back, not its settings",
                        )
                    elif (
                        isinstance(restore_error, InstallerError)
                        and restore_error.code is InstallerErrorCode.ROLLBACK_OPKG_OVERLAP
                    ):
                        restore_error = InstallerError(
                            InstallerErrorCode.ROLLBACK_FAILED,
                            "the receiver's interface did not stop, so its settings were "
                            "not restored; the plugin's files were, while another opkg run "
                            "took opkg's lock",
                        )
                    else:
                        restore_error.add_note(
                            "the receiver's interface did not stop, so its settings were "
                            "not restored either"
                        )
                elif steps["started"] not in ("", "0"):
                    raise InstallerError(InstallerErrorCode.ROLLBACK_RESTART_FAILED)
                else:
                    if session is None:
                        session = await connector(credentials)
                    # A pid that was not there before the interface was stopped proves the
                    # receiver came back; no pid beforehand means Enigma was already
                    # stopped when the rollback began, and any live process is then the
                    # proof. It is waited for rather than sampled, because `init 3` is
                    # answered long before the interface is.
                    restarted_pids = await _async_wait_for_enigma(session, old_enigma_pids)
                    _LOGGER.info(
                        "Installer rollback: the receiver interface is running again as "
                        "pid %s",
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
    if not tidy:
        # The script's end was not seen: it may still be restoring, and it still needs its
        # directory. The lock stays, with this transaction's id in it.
        _LOGGER.error(
            "Installer rollback: the stop-and-restore script on the receiver was started "
            "and its end was not seen. It carries on by itself; its status is in %s. The "
            "transaction lock %s stays until it is stale, and if the receiver shows no "
            "picture by then, switch it off and on again",
            r2_status,
            remote_lock,
        )
    else:
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
                # The helper had to outlive the restore that used it; nothing needs it,
                # or the script's own directory, now. A directory is removed only after
                # its script was seen to finish, never on the strength of its absence.
                if r2_finished:
                    with suppress(Exception):
                        await released.run(f"rm -rf {shlex.quote(r2_dir)}", timeout=30)
                await _async_remove_helper(released, remote_helper)
            finally:
                await released.close()
        except BaseException as release_err:
            release_error = release_err
            if cancelled is None and isinstance(release_err, asyncio.CancelledError):
                cancelled = release_err
            if progress.lock_released:
                # The lock went; something after it did not - closing the session, most
                # likely. Saying the receiver is locked here would send somebody to
                # delete a directory that is not there.
                _LOGGER.warning(
                    "Installer rollback: the transaction lock was released, but tidying "
                    "up after it did not finish",
                    exc_info=release_err,
                )
            else:
                _LOGGER.error(
                    "Installer rollback could not release the transaction lock at %s; until "
                    "it is removed by hand or goes stale, every install on this receiver "
                    "reports it as busy",
                    remote_lock,
                    exc_info=release_err,
                )
    # A cancellation is what it is, and it outranks every verdict below: those describe a
    # rollback that ran to its own conclusion, and this one did not.
    if cancelled is not None:
        raise cancelled
    if not tidy:
        raise _RollbackError(InstallerErrorCode.ROLLBACK_UNOBSERVED) from (
            restart_error or restore_error
        )
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
    session: InstallerSession,
    remote_helper: str,
    claimed: str,
    nonce: str,
    *,
    settings_for: frozenset[str] = frozenset(),
) -> bool:
    """Put back what an abandoned installer transaction may have left half-done.

    Returns whether the settings block was written, so that the caller reads again
    everything it had read from it.

    The claim reports the id of an installer transaction whose lock it reclaimed as
    stale - one that never released it: its connection died, Home Assistant restarted,
    the receiver lost power. That transaction writes no record of how far it got, so
    this cannot say whether it had begun to change or to restore anything, and restores
    its snapshot again, which is safe to repeat. Which snapshot is decided by the id in
    the lock's owner record, never by a file time: many receivers have no battery-
    backed clock, and "newest" is then whatever NTP made of it.

    The interface is running, so this is the restore that runs under it: files and
    opkg's metadata only, by renames, never the settings block.

    `settings_for` is the one exception: the forced reinstall into runlevel 4 left by an
    interrupted stop-and-restore of ours names the transactions whose stop-and-restore
    was cut off before it wrote `restored`. Only when the reclaimed lock is one of those
    is the settings block put back too - that is what the rollback was stopped in the
    middle of - and only while enigma2 is stopped, which is when it can be written
    without being overwritten from memory. A lock whose transaction died before any
    rollback, or whose rollback already restored, never gets an old snapshot's settings:
    every plugin setting changed since would be reverted.
    """
    try:
        reclaimed = json.loads(claimed or "{}").get("reclaimed", "")
    except (AttributeError, ValueError):
        return False
    if (
        not isinstance(reclaimed, str)
        or not installer_helper.TRANSACTION_ID.fullmatch(reclaimed)
        or reclaimed == nonce
    ):
        return False
    abandoned = f"{BACKUP_ROOT}/ha-installer-{reclaimed}"
    present = await session.run(f"test -d {shlex.quote(abandoned)}", timeout=30)
    if present.exit_status:
        return False
    settings = reclaimed in settings_for
    if settings and await _async_enigma_pids(session):
        _LOGGER.warning(
            "Installer: the receiver's interface runs again, so the settings of the "
            "interrupted transaction %s are not put back - only its files",
            reclaimed,
        )
        settings = False
    _LOGGER.warning(
        "Installer: transaction %s was abandoned without releasing its lock; putting "
        "back its snapshot %s%s before this one starts",
        reclaimed,
        abandoned,
        ", its settings block included" if settings else "",
    )
    verb = "restore" if settings else "withdraw"
    await _run_checked(
        session,
        f"python3 {shlex.quote(remote_helper)} {verb} {shlex.quote(abandoned)} --provisioning"
        + (" --settings" if settings else ""),
        InstallerErrorCode.INSTALL_FAILED,
        timeout=RESTORE_TIMEOUT,
        detail="the snapshot of an abandoned transaction could not be put back",
        busy=InstallerErrorCode.OPKG_BUSY,
    )
    return settings


# Every way an SSH command can fail to come back. asyncssh's own errors - a connection
# that closed under a command, a channel that could not be opened on it - are not
# `OSError`s; the adapter below turns them into one, and the restart code names them as
# well, because a session is anything with a `run` method.
_CONNECTION_LOST = (OSError, TimeoutError, asyncssh.Error)
# What a requested restart was seen to do.
_RESTARTED = "restarted"
_QUESTION = "question"
_UNOBSERVED = "unobserved"
_UNCONFIRMED = "unconfirmed"


async def _async_drop(session: InstallerSession | None) -> None:
    """Close a session that is no longer usable; closing it may fail too."""
    if session is not None:
        with suppress(Exception):
            await session.close()


async def _async_pids_or_none(
    session: InstallerSession | None, credentials: SshCredentials, connector: Connector
) -> tuple[InstallerSession | None, set[int] | None]:
    """Read enigma2's pids, connecting again first when there is no session.

    None for the pids means they could not be read - the connection is gone or would
    not come back - never that enigma2 is not running, which is an empty set.
    """
    try:
        if session is None:
            session = await connector(credentials)
        return session, await _async_enigma_pids(session)
    except (InstallerError, *_CONNECTION_LOST):
        await _async_drop(session)
        return None, None


async def _async_pids_within(
    session: InstallerSession | None,
    credentials: SshCredentials,
    connector: Connector,
    bound: float,
) -> tuple[InstallerSession | None, set[int] | None]:
    """Read enigma2's pids, trying again - and connecting again - for up to `bound`."""
    deadline = time.monotonic() + bound
    while True:
        session, pids = await _async_pids_or_none(session, credentials, connector)
        if pids is not None or time.monotonic() >= deadline:
            return session, pids
        await asyncio.sleep(RESTART_POLL_SECONDS)


async def _async_clean_restart(
    session: InstallerSession | None,
    credentials: SshCredentials,
    connector: Connector,
    remote_helper: str,
    old_pids: set[int],
) -> tuple[InstallerSession | None, str]:
    """R1: ask for the image's own clean restart and watch what it does.

    OpenWebif's power state 3 is `TryQuitMainloop(3)`: the image stops its services,
    saves its settings - the channel being watched among them - and exits, and init
    starts it again. `init 4` never got to the save, which is how a receiver came back
    on a channel saved hours earlier.

    Judged by the pid, never by the call's answer: the quit can reset the connection
    that asked for it. A connection that drops while this watches is connected again;
    it is a gap in what this end sees, not an answer about the receiver. Returns

    - `restarted`: a new enigma2 runs - or the old one went away and none came back,
      which is a failed restart for the proof to find;
    - `question`: the old enigma2 is still there, unchanged, at the bound, and OpenWebif
      confirmed the request: the image is asking on the television, and that question
      waits for ever;
    - `unconfirmed`: the same, but OpenWebif never confirmed the request - its answer was
      lost or said nothing - so whether a question is on screen is not known;
    - `unobserved`: the receiver could not be read at all after the request.
    """
    confirmed = False
    try:
        if session is None:
            session = await connector(credentials)
        asked = await session.run(
            f"python3 {shlex.quote(remote_helper)} powerstate --state 3", timeout=30
        )
        with suppress(AttributeError, TypeError, ValueError):
            confirmed = json.loads(asked.stdout).get("answered") is True
    except (InstallerError, *_CONNECTION_LOST):
        await _async_drop(session)
        session = None
    deadline = time.monotonic() + RESTART_QUESTION_TIMEOUT
    seen: set[int] | None = None
    while True:
        session, pids = await _async_pids_or_none(session, credentials, connector)
        if pids is not None:
            seen = pids
            if pids - old_pids:
                return session, _RESTARTED
        if time.monotonic() >= deadline:
            if seen is None:
                return session, _UNOBSERVED
            if not old_pids <= seen:
                return session, _RESTARTED
            return session, _QUESTION if confirmed else _UNCONFIRMED
        await asyncio.sleep(RESTART_POLL_SECONDS)


async def _async_withdraw(
    session: InstallerSession | None,
    credentials: SshCredentials,
    connector: Connector,
    remote_helper: str,
    backup: str,
    provision_started: bool,
) -> tuple[InstallerSession | None, str]:
    """Put the old files back under a running enigma2, and say how that went.

    Files and opkg's metadata only, never the settings block: the transaction changed no
    setting, and a block written while enigma2 runs is overwritten from memory by its
    next clean quit - writing it would only race the household's own saves. The helper
    swaps the plugin directory in by two renames, so a restart answered in the middle
    meets the whole old tree or the whole new one.

    A connection lost under the command is connected again and the command repeated:
    a restore of the same snapshot is safe to repeat. Returns `done`, `overlap` (done,
    but an opkg run may have written its database meanwhile), `busy` (opkg's lock stayed
    held, nothing put back), `failed`, or `unobserved`.
    """
    _LOGGER.warning(
        "Installer: the receiver's interface did not restart within %d s - the image is "
        "asking on the television - so the update is withdrawn and the previous files "
        "put back",
        int(RESTART_QUESTION_TIMEOUT),
    )
    command = f"python3 {shlex.quote(remote_helper)} withdraw {shlex.quote(backup)}" + (
        " --provisioning" if provision_started else ""
    )
    deadline = time.monotonic() + 2 * RESTORE_TIMEOUT
    while True:
        try:
            if session is None:
                session = await connector(credentials)
            result = await session.run(command, timeout=RESTORE_TIMEOUT)
        except (InstallerError, *_CONNECTION_LOST):
            await _async_drop(session)
            session = None
            if time.monotonic() >= deadline:
                return None, _UNOBSERVED
            await asyncio.sleep(RESTART_POLL_SECONDS)
            continue
        if result.exit_status == 0:
            return session, "done"
        if result.exit_status == installer_helper.EXIT_OPKG_LOCK_LOST:
            return session, "overlap"
        if result.exit_status == installer_helper.EXIT_OPKG_BUSY:
            return session, "busy"
        return session, "failed"


def _withdrawn_code(outcome: str, withdrawal: str) -> InstallerErrorCode:
    """How a withdrawal after which nothing restarted is reported.

    Files back (an opkg run meanwhile only puts its database in doubt, which is logged):
    withdrawn - described as the question the image asked only when OpenWebif confirmed
    the request. Files not back: the new ones are on the receiver, and whatever is on the
    television may still be answered - which the sentence has to say.
    """
    if withdrawal in ("done", "overlap"):
        if outcome == _QUESTION:
            return InstallerErrorCode.RESTART_WITHDRAWN
        return InstallerErrorCode.RESTART_UNCONFIRMED
    return InstallerErrorCode.WITHDRAW_FAILED


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


async def _async_refuse_what_the_index_rules_out(
    hass: HomeAssistant, version: str
) -> dict[str, Any] | None:
    """Refuse a bundle below the floor, or withdrawn, in the last verified index.

    Here rather than in each caller, because every path that installs the bundle - the
    update card, the guided installer's update of an older plugin, the forced reinstall -
    installs through this module, and a rule one of them forgot would be a rule that does
    not hold. With no verified index yet, only this integration's own floor applies.
    Returns the index the rule was judged by.
    """
    cache = async_release_index_cache(hass)
    await cache.async_load()
    if (refusal := bundle_refusal(version, cache.index)) is None:
        return cache.index
    code, placeholders = refusal
    _LOGGER.warning(
        "Receiver install refused before connecting to it: the bundled plugin %s is %s",
        version,
        f"withdrawn in the signed release index: {placeholders['reason']}"
        if code == "withdrawn"
        else f"below {placeholders['floor']}, the lowest version the signed release index "
        "allows",
    )
    raise InstallerError(
        InstallerErrorCode.BUNDLE_WITHDRAWN
        if code == "withdrawn"
        else InstallerErrorCode.BUNDLE_BELOW_FLOOR,
        f"the bundled plugin {version} is ruled out by the signed release index ({code})",
        placeholders,
    )


# The fetch's refusals under the rule, as the installer's codes. Anything else the rule says -
# not listed, another contract, a newer integration needed, no index at all - is one code.
_PACKAGE_REFUSALS: dict[str, InstallerErrorCode] = {
    release_package.WITHDRAWN: InstallerErrorCode.VERSION_WITHDRAWN,
    release_package.BELOW_FLOOR: InstallerErrorCode.VERSION_BELOW_FLOOR,
}


async def _async_refuse_a_package_the_index_no_longer_offers(
    hass: HomeAssistant, package: PackageSource
) -> None:
    """Ask the whole rule again for a release of the index, against the newest index held.

    The bytes were verified when they were fetched, under the index held then. A check in
    between may have accepted a newer index - one that withdraws the version, raises the
    floor, moves it to another contract, asks for a newer integration, drops it, or signs
    other bytes under its number - and the install is the last moment that can still say no
    without the receiver having been touched. So this is the fetch's own rule
    (`release_package.eligible_release`), not a part of it, and the entry's sha256 and size
    are held to the bytes: a package is installed only while the newest index would have
    let it be fetched.
    """
    cache = async_release_index_cache(hass)
    await cache.async_load()
    try:
        entry = release_package.eligible_release(
            cache.index, package.version, integration_version=cache.integration_version
        )
    except release_package.PackageError as error:
        _LOGGER.warning(
            "Receiver install refused before connecting to it: plugin %s is no longer "
            "installable under the newest signed release index held (%s)",
            package.version,
            error.detail,
        )
        raise InstallerError(
            _PACKAGE_REFUSALS.get(error.code, InstallerErrorCode.VERSION_NOT_INSTALLABLE),
            f"plugin {package.version} is ruled out by the signed release index ({error.code})",
            error.placeholders
            if error.code in _PACKAGE_REFUSALS
            else {"version": package.version},
        ) from error
    if entry["sha256"] != package.sha256 or entry["size"] != len(package.data):
        _LOGGER.warning(
            "Receiver install refused before connecting to it: the newest signed release "
            "index held names other bytes for plugin %s than the ones that were verified",
            package.version,
        )
        raise InstallerError(
            InstallerErrorCode.PACKAGE_INVALID,
            f"the signed release index no longer names these bytes for plugin "
            f"{package.version}",
            {"version": package.version},
        )


# How the forced reinstall found the receiver's interface, and so how it starts it again.
_GUI_RUNNING = "running"
_GUI_RESPAWN = "respawn"
_GUI_OURS_STOPPED = "ours_stopped"


@dataclass(frozen=True, slots=True)
class _ForcedGui:
    """The row of the GUI-state table a forced reinstall is in (ADR-0008, section 8)."""

    mode: str
    # What R3 puts back afterwards: for runlevel 4 of ours, the channel the interrupted
    # stop-and-restore was keeping; nothing for a respawn loop, which played nothing.
    record: restart_rule.RestartRecord = field(default_factory=restart_rule.RestartRecord)
    # The transactions whose stop-and-restore was cut off before it wrote `restored`: the
    # only ones whose snapshot's settings block the recovery may put back.
    unrestored: frozenset[str] = frozenset()


def _runlevel(stdout: str) -> str | None:
    """sysvinit's `runlevel` prints the previous and the current level; the current one."""
    words = stdout.split()
    if words and words[-1] in ("0", "1", "2", "3", "4", "5", "6", "S"):
        return words[-1]
    return None


async def _async_forced_gui(session: InstallerSession, remote_helper: str) -> _ForcedGui:
    """Decide the forced reinstall's row from what SSH says, before any OpenWebif guard.

    The guards the update path runs need OpenWebif, which is an enigma2 plugin: on a
    receiver whose interface is down they cannot answer, and on one that is merely slow to
    answer, reading silence as "not recording" would let a reinstall restart a recording.
    So the state of the interface is read first, by `runlevel` and three `pidof` samples
    over ten seconds, and each row has its own decision:

    - enigma2 running (on any sample - absent once and present on the next is a respawn
      gap) and OpenWebif answers: the update path's guards and its clean restart;
    - enigma2 running and OpenWebif silent: refused - a hung interface may be recording,
      and nothing can say it is not;
    - runlevel 3 and enigma2 absent on every sample: a respawn loop, in which nothing can
      record; the guards are skipped and the interface is started with `init 4; init 3`;
    - runlevel 4 left by an interrupted stop of ours - the helper's `leftovers` says so
      only for an unfinished stop-and-restore of this boot, or a self-update of this boot
      cut off while rolling back; a lock, a marker or a kept transaction directory alone
      is residue, not a stop - recovered and started with `init 3`;
    - runlevel 4 with no such stop of ours: somebody stopped the interface on purpose, and
      a reinstall would start it - refused;
    - anything else - enigma2 still running in runlevel 4, or no interface and no runlevel
      to read: not a state this table decides, refused.

    Ten seconds is shorter than an interface can take to show a pid: on a receiver,
    `pidof enigma2` stays silent for 11-14 s after `init 3`, and a box that is still
    booting reads as runlevel 3 before its interface starts. Such a receiver is read as a
    respawn loop here. That reading is never acted on alone: `_async_recheck_forced_gui`
    looks again immediately before the interface is stopped or started, and an interface
    that runs by then gets every guard of the first row and the clean restart instead.
    """
    try:
        level = await session.run("runlevel", timeout=30)
    except _CONNECTION_LOST as err:
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED, "the receiver did not say its runlevel"
        ) from err
    runlevel = _runlevel(level.stdout) if level.exit_status == 0 else None
    running = False
    for sample in range(FORCE_GUI_SAMPLES):
        if sample:
            await asyncio.sleep(FORCE_GUI_SAMPLE_SECONDS)
        try:
            pids = await _async_enigma_pids(session)
        except InstallerError as err:
            raise InstallerError(
                InstallerErrorCode.PREFLIGHT_FAILED,
                "the receiver would not say whether its interface runs",
            ) from err
        if pids:
            running = True
            break
    if running:
        if runlevel == "4":
            raise InstallerError(
                InstallerErrorCode.PREFLIGHT_FAILED,
                "the receiver's interface still runs in runlevel 4 - it is being stopped",
            )
        await _run_checked(
            session,
            "wget -qO- http://127.0.0.1/api/statusinfo",
            InstallerErrorCode.GUI_NOT_ANSWERING,
            detail="the receiver's interface runs and OpenWebif does not answer, so "
            "whether it is recording cannot be known",
        )
        return _ForcedGui(_GUI_RUNNING)
    samples = (
        f"{FORCE_GUI_SAMPLES} samples over "
        f"{int((FORCE_GUI_SAMPLES - 1) * FORCE_GUI_SAMPLE_SECONDS)} s"
    )
    if runlevel == "3":
        _LOGGER.warning(
            "Forced reinstall: the receiver's interface is not running in runlevel 3 "
            "(absent on %s) - a respawn loop. The recording and timer guards are skipped, "
            "since nothing records without enigma2; the interface is started again with "
            "init 4 and init 3 after the install, and it then starts any overdue timer",
            samples,
        )
        return _ForcedGui(_GUI_RESPAWN)
    if runlevel == "4":
        result = await _run_checked(
            session,
            f"python3 {shlex.quote(remote_helper)} leftovers",
            InstallerErrorCode.PREFLIGHT_FAILED,
            detail="the receiver would not say what an interrupted transaction left",
        )
        try:
            found = json.loads(result.stdout)
        except ValueError as err:
            raise InstallerError(
                InstallerErrorCode.PREFLIGHT_FAILED,
                "the receiver's account of interrupted transactions is not JSON",
            ) from err
        if not isinstance(found, dict) or found.get("ours") is not True:
            raise InstallerError(
                InstallerErrorCode.GUI_STOPPED,
                "the receiver's interface is stopped (runlevel 4) and no interrupted stop "
                "of this project's is on the receiver, so somebody stopped it on purpose",
            )
        service = found.get("service")
        service = service if isinstance(service, str) and service else None
        unrestored = found.get("unrestored")
        unrestored_ids = frozenset(
            transaction
            for transaction in (unrestored if isinstance(unrestored, list) else ())
            if isinstance(transaction, str)
            and installer_helper.TRANSACTION_ID.fullmatch(transaction)
        )
        _LOGGER.warning(
            "Forced reinstall: the receiver's interface is stopped in runlevel 4 by an "
            "interrupted transaction of this project (unfinished stop-and-restores %s, of "
            "which cut off before their restore %s; self-updates cut off while rolling "
            "back %s). The recording and timer guards are skipped; the lock is taken only "
            "by the released stale rule, the interrupted snapshot is put back, and the "
            "interface is started with init 3 on %s",
            found.get("r2"),
            sorted(unrestored_ids),
            found.get("rolling_back"),
            "the channel that transaction recorded" if service else "its saved channel",
        )
        return _ForcedGui(
            _GUI_OURS_STOPPED, restart_rule.RestartRecord(service), unrestored_ids
        )
    raise InstallerError(
        InstallerErrorCode.PREFLIGHT_FAILED,
        f"the receiver's interface is not running and its runlevel is {runlevel or 'unknown'}",
    )


async def _async_recheck_forced_gui(session: InstallerSession, gui: _ForcedGui) -> _ForcedGui:
    """Look at the interface again immediately before a row without one stops or starts it.

    The rows without an interface skip every guard OpenWebif answers, and the respawn
    loop's start begins with `init 4`. Both are right only while there is still no
    interface, and minutes have passed since the three samples: a box that was booting, or
    slower to show a pid than the samples waited, has an interface by now - which may be
    recording, or starting a timer that fell due while it was down - and a person may have
    run `init 3` on a receiver left in runlevel 4. So `pidof` is asked once more, as the
    last read before the start. An interface that runs now is the first row: OpenWebif
    must answer (a hung one is refused, as in the first look), and the caller measures
    every guard - recording, timers, standby, streaming - and restarts it cleanly. Nothing
    is ever sent to init over an interface that runs.

    What remains is the moment between this read and the start itself, and an interface
    still inside its own silent start; a start inside the respawn script waits for the
    old process to be gone before `init 3` either way.
    """
    try:
        pids = await _async_enigma_pids(session)
    except InstallerError as err:
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "the receiver would not say whether its interface runs",
        ) from err
    if not pids:
        return gui
    _LOGGER.warning(
        "Forced reinstall: the receiver's interface was %s and now runs (pid %s). It is "
        "treated as a running interface: every OpenWebif guard is measured and it is "
        "restarted cleanly, never stopped by init",
        "in a respawn loop or still starting"
        if gui.mode == _GUI_RESPAWN
        else "stopped in runlevel 4 and somebody started it",
        ", ".join(str(pid) for pid in sorted(pids)),
    )
    await _run_checked(
        session,
        "wget -qO- http://127.0.0.1/api/statusinfo",
        InstallerErrorCode.GUI_NOT_ANSWERING,
        detail="the receiver's interface runs and OpenWebif does not answer, so "
        "whether it is recording cannot be known",
    )
    return _ForcedGui(_GUI_RUNNING)


async def _async_busy_error(
    session: InstallerSession,
    remote_helper: str,
    remote_lock: str,
    *,
    interface_stopped: bool = False,
) -> InstallerError:
    """Say why the lock was refused, and when a stalled self-update's lock frees itself.

    The claim is the decision - the released stale rule, the same in every installer and
    in the plugin - and nothing here takes a lock back, however long its owner has been
    silent. A heartbeat three minutes old says a self-update's helper stopped, but not
    that nothing of it still runs: its `opkg` may be orphaned mid-write, and a helper
    that is stopped rather than dead beats again when it is continued. So what changes is
    only the sentence: when the rule will free the lock, so that the household knows the
    wait has an end.

    `interface_stopped` is runlevel 4 left by a stop of ours: the household has no
    picture, so an installer's lock - which has no heartbeat to judge and frees itself
    30 minutes after its claim - gets the same "in about N minutes" rather than a plain
    "busy" with no end in sight.
    """
    try:
        result = await session.run(
            f"python3 {shlex.quote(remote_helper)} lock-info {shlex.quote(remote_lock)}",
            timeout=30,
        )
        info = json.loads(result.stdout) if result.exit_status == 0 else None
    except (ValueError, *_CONNECTION_LOST):
        info = None
    if isinstance(info, dict):
        silent = info.get("silent")
        remaining = info.get("remaining")
        if (
            isinstance(info.get("origin"), str)
            and isinstance(silent, int)
            and silent > STALLED_SILENCE_SECONDS
            and isinstance(remaining, int)
            and remaining > 0
        ):
            minutes = max(1, math.ceil(remaining / 60))
            return InstallerError(
                InstallerErrorCode.BUSY_STALLED,
                f"a plugin self-update holds the receiver's lock and has not renewed it for "
                f"{silent} s; the released rule frees it in about {minutes} min",
                {"minutes": str(minutes)},
            )
        if (
            interface_stopped
            and info.get("origin") is None
            and isinstance(remaining, int)
            and remaining > 0
        ):
            minutes = max(1, math.ceil(remaining / 60))
            return InstallerError(
                InstallerErrorCode.BUSY_STALLED,
                "an installation of this integration holds the receiver's lock while its "
                f"interface is stopped; the released rule frees it in about {minutes} min",
                {"minutes": str(minutes)},
            )
    return InstallerError(InstallerErrorCode.BUSY)


def _downgrade(installed: str | None, target: str, request: InstallRequest) -> bool:
    """Whether opkg must be told to accept an older package - or why not to install at all.

    opkg compares the package with its own records, so its records decide whether the flag
    is needed. What they never decide alone is whether a *newer plugin is running*: after
    a restore that did not complete they name the version that was being installed while
    the code on disk is the one before it, or no plugin directory is there at all.

    - The forced reinstall puts the bundle over whatever is recorded, so the flag goes on
      whenever the records name a newer version, or one they cannot be read as.
    - A confirmed downgrade (options flow) passes it when the records name a newer one.
    - Any other install is refused when a newer plugin runs - by what the running plugin
      reports, when the caller knows it, whatever the records say; else by the records,
      and then said to be the records. Records newer than both the running plugin and
      the target are a mismatch, refused before anything changes, with the forced
      reinstall named as the repair.
    """
    if request.force:
        return bool(installed) and (
            installed == UNREADABLE_RECORDS
            or _package_version(installed) > _package_version(target)
        )
    running = request.running_version
    running_version = None
    if running:
        with suppress(InstallerError):
            running_version = _package_version(running)
    if (
        not request.downgrade
        and running_version is not None
        and running_version > _package_version(target)
    ):
        # Whatever the records say: they may name an older version than the plugin that
        # runs, which is the same kind of mismatch the other way round.
        raise InstallerError(
            InstallerErrorCode.NEWER_INSTALLED,
            f"the receiver runs plugin {running} and this install would put {target} over it",
        )
    if not installed:
        return False
    if _package_version(installed) <= _package_version(target):
        return False
    if request.downgrade:
        return True
    if running_version is None:
        raise InstallerError(
            InstallerErrorCode.NEWER_INSTALLED,
            f"opkg's records on the receiver name plugin {installed}, and this install "
            f"would put {target} over it",
        )
    raise InstallerError(
        InstallerErrorCode.RECORDS_MISMATCH,
        f"opkg's records on the receiver name plugin {installed}, while the plugin that "
        f"runs reports {running}; the forced reinstall is the repair",
        {"recorded": installed, "running": running},
    )


async def _async_prove_forced(
    credentials: SshCredentials,
    connector: Connector,
    remote_helper: str,
    old_pids: set[int],
    watch: _RestartWatch | None,
) -> None:
    """The forced reinstall's proof: a new enigma2 holding the plugin's log open.

    The plugin configures its logging before it reads `enabled`, so the log is opened by
    the new interface process whether the plugin is switched on or off and whether or not
    it can reach the broker - which is the whole point of this path: it needs no answer
    from the plugin. When the plugin is switched on, `watch` also has to see its fresh
    announcement and the bundle's version and build, as the update path does. Both are
    measured from the restart and share one bound.
    """
    deadline = time.monotonic() + ANNOUNCEMENT_TIMEOUT
    session: InstallerSession | None = None
    try:
        while True:
            try:
                if session is None:
                    session = await connector(credentials)
                new = sorted(await _async_enigma_pids(session) - old_pids)
                if new:
                    result = await session.run(
                        f"python3 {shlex.quote(remote_helper)} logfd "
                        + " ".join(f"--pid {pid}" for pid in new),
                        timeout=30,
                    )
                    holding = (
                        json.loads(result.stdout).get("holding")
                        if result.exit_status == 0
                        else None
                    )
                    if isinstance(holding, list) and holding:
                        _LOGGER.info(
                            "Forced reinstall: the new interface process %s holds the "
                            "plugin's log open",
                            ", ".join(str(pid) for pid in holding),
                        )
                        break
            except (InstallerError, ValueError, AttributeError, *_CONNECTION_LOST):
                await _async_drop(session)
                session = None
            if time.monotonic() >= deadline:
                raise InstallerError(
                    InstallerErrorCode.PLUGIN_NOT_STARTED,
                    "no new interface process opened the plugin's log",
                )
            await asyncio.sleep(FORCE_PROOF_POLL_SECONDS)
    finally:
        await _async_drop(session)
    if watch is None or watch.completed.done():
        if watch is not None:
            await watch.completed
        return
    try:
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            await watch.completed
    except TimeoutError as err:
        raise InstallerError(InstallerErrorCode.ANNOUNCEMENT_TIMEOUT) from err


async def _async_upload_helper(
    hass: HomeAssistant, session: InstallerSession, remote_helper: str
) -> None:
    """Stream the receiver-side helper into the receiver's /tmp."""
    helper_bytes = await hass.async_add_executor_job(Path(installer_helper.__file__).read_bytes)
    await _run_checked(
        session,
        f"umask 077; cat > {shlex.quote(remote_helper)}",
        InstallerErrorCode.INSTALL_FAILED,
        input=helper_bytes,
        detail="the installer helper could not be written to the receiver's /tmp",
    )


async def _async_start_after_failure(
    hass: HomeAssistant,
    credentials: SshCredentials,
    connector: Connector,
    remote_helper: str,
) -> None:
    """Start the interface a forced reinstall found stopped by us, after it failed.

    Runlevel 4 left by an interrupted stop of ours is a receiver with no picture. A
    forced reinstall that claimed its lock and then failed before starting the interface
    itself - the interrupted transaction could not be put back, the install failed and
    was rolled back - must not leave it so: the picture comes first (TRANSACTION.md,
    section 5.2), on whatever files are there, and the sentence says what was not put
    back. The helper is uploaded again, because the rollback and the release remove it.
    Nothing here raises: the failure already has its verdict, and a start that does not
    come is logged beside it.
    """
    session: InstallerSession | None = None
    try:
        session = await connector(credentials)
        await _async_upload_helper(hass, session, remote_helper)
        await _run_checked(
            session,
            f"python3 {shlex.quote(remote_helper)} respawn",
            InstallerErrorCode.RESTART_FAILED,
            detail="the receiver's interface could not be started",
        )
        await _async_wait_for_enigma(session, set())
        _LOGGER.warning(
            "Forced reinstall failed; the receiver's interface, stopped by an interrupted "
            "transaction of this project, was started again on the files that are there"
        )
        await _async_remove_helper(session, remote_helper)
    except Exception as err:
        _LOGGER.error(
            "Forced reinstall failed and the receiver's interface could not be started "
            "again; it stays stopped (runlevel 4) until somebody starts it",
            exc_info=err,
        )
    finally:
        await _async_drop(session)


@dataclass(frozen=True, slots=True)
class _Source:
    """The package one transaction installs, whichever way it came."""

    version: str
    data: bytes = field(repr=False)
    sha256: str
    # The commit the restart proof holds a receiver that reports `info.build` to.
    commit: str | None
    # What the release needs installed on the receiver, from its signed entry.
    depends: tuple[str, ...]


async def _async_source(hass: HomeAssistant, request: InstallRequest) -> _Source:
    """The bundle, or the verified bytes the request carries - each under the one rule."""
    if request.package is None:
        bundle = await _async_load_bundle(hass)
        index = await _async_refuse_what_the_index_rules_out(hass, bundle.version)
        data = await hass.async_add_executor_job(Path(bundle.path).read_bytes)
        if hashlib.sha256(data).hexdigest() != bundle.sha256:
            raise InstallerError(InstallerErrorCode.BUNDLE_INVALID)
        entry = release_of(index, bundle.version)
        return _Source(
            bundle.version,
            data,
            bundle.sha256,
            (bundle.build or {}).get("commit") or None,
            tuple(entry.get("depends") or ()) if entry is not None else (),
        )
    package = request.package
    await _async_refuse_a_package_the_index_no_longer_offers(hass, package)
    if hashlib.sha256(package.data).hexdigest() != package.sha256:
        raise InstallerError(
            InstallerErrorCode.PACKAGE_INVALID,
            f"the package of plugin {package.version} no longer has its verified checksum",
            {"version": package.version},
        )
    return _Source(
        package.version, package.data, package.sha256, package.commit, package.depends
    )


async def _async_check_depends(
    session: InstallerSession, depends: tuple[str, ...], version: str
) -> None:
    """Refuse, before anything changes, a release that needs a package the receiver lacks.

    opkg would otherwise find out halfway through the install - and try the image's feeds for
    it, which a receiver without internet cannot reach. `opkg status` is a reader and never
    takes opkg's lock, so this asks nothing a running opkg could make wait. The names come
    from the signed entry, whose schema allows nothing a shell would read as syntax; they are
    quoted anyway. Every missing package is named, so that one look at the receiver's feeds
    installs them all.

    `opkg status <name>` sees a package of exactly that name. opkg itself would also accept a
    dependency that another installed package declares in `Provides:`, so on an image that
    merges a named package into another one this refuses an install opkg would have done.
    That is the check the design prescribes, and the direction is the safe one - a refusal
    changes nothing on the receiver and names what it looked for.
    """
    if not depends:
        return
    names = " ".join(shlex.quote(name) for name in depends)
    result = await session.run(
        f'for p in {names}; do opkg status "$p" 2>/dev/null | '
        f"grep -q '^Status: .* installed$' || echo \"$p\"; done",
        timeout=60,
    )
    if result.exit_status:
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "the receiver would not say which of the release's dependencies it has",
        )
    missing = [line.strip() for line in result.stdout.splitlines() if line.strip() in depends]
    if missing:
        named = ", ".join(missing)
        raise InstallerError(
            InstallerErrorCode.DEPENDS_MISSING,
            f"plugin {version} needs {named}, which the receiver does not have installed",
            # One placeholder for one sentence however many are missing: the list, in the
            # signed entry's order.
            {"version": version, "package": named},
        )


async def _async_reset_after_downgrade(
    hass: HomeAssistant, base_topic: str, node_id: str, version: str
) -> None:
    """Ask the older plugin, once it has proved itself, to retract what the newer published.

    Its state file was written by the newer plugin and it loads it tolerantly, so it knows
    every retained topic the newer one left - `update` among them - and `cmd/reset` retracts
    them all and republishes its own. Best effort: the downgrade is committed, and a topic
    left retained is a stale entity, not a broken receiver.
    """
    try:
        await mqtt.async_publish(
            hass, command_topic(base_topic, node_id, "reset"), "PRESS", qos=1, retain=False
        )
    except Exception:
        _LOGGER.warning(
            "Plugin %s installed over a newer one; asking it to retract the newer plugin's "
            "topics failed, so some may stay retained until the receiver is reset",
            version,
            exc_info=True,
        )


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
    """Install or update the plugin as a rollback-safe transaction."""
    request.validate()
    if request.force and request.package is not None:
        # The forced reinstall installs the bytes CI reproduced, and nothing fetched.
        raise InstallerError(
            InstallerErrorCode.PREFLIGHT_FAILED,
            "a forced reinstall installs only the package bundled with the integration",
        )
    source = await _async_source(hass, request)
    bundle_bytes = source.data
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
    # A confirmed downgrade that found a newer plugin on the receiver: opkg is told to
    # accept the older one, and the older one retracts what the newer published.
    force_downgrade = False
    # Set from the restart request until a new enigma2 is seen: no rollback may run.
    no_rollback = False
    record: restart_rule.RestartRecord | None = None
    provisioned_name = ""
    # The forced reinstall's row of the GUI-state table; None on every other path.
    gui: _ForcedGui | None = None
    # Whether OpenWebif is asked for the recording, timer, standby and streaming guards:
    # always, except in a forced reinstall that found no interface to answer them.
    webif = True
    plugin_enabled = True
    # Runlevel 4 of ours with the lock claimed: a failure before this install starts the
    # interface itself still starts it, on what is there.
    start_after_failure = False
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
            if request.force:
                # The helper first: the interface's state is read before any guard that
                # needs OpenWebif, and part of it is read by the helper.
                await _async_upload_helper(hass, session, remote_helper)
                gui = await _async_forced_gui(session, remote_helper)
                webif = gui.mode == _GUI_RUNNING
            preflight = await _async_measure_preflight(
                session,
                bundle_size=len(bundle_bytes),
                restart_guards=webif,
                webif=webif,
                strict_records=not request.force,
            )
            force_downgrade = _downgrade(preflight.installed_version, source.version, request)

            await _async_progress(progress_cb, "backup")
            if not request.force:
                await _async_upload_helper(hass, session, remote_helper)
            identity = await _async_read_receiver_identity(session, remote_helper)
            stored_name = _check_receiver_identity(identity, request)
            plugin_enabled = _stored_setting(identity, "enabled") is True
            await _async_check_depends(session, source.depends, source.version)
        except InstallerError as err:
            _log_install_refusal(err)
            raise
        claim = await session.run(
            f"mkdir -p {shlex.quote(BACKUP_ROOT)} && "
            f"python3 {shlex.quote(remote_helper)} claim {shlex.quote(remote_lock)} "
            f"--id {nonce}",
            timeout=30,
        )
        interface_down = gui is not None and gui.mode == _GUI_OURS_STOPPED
        if claim.exit_status:
            raise await _async_busy_error(
                session, remote_helper, remote_lock, interface_stopped=interface_down
            )
        remote_lock_claimed = True
        # From here a failure in runlevel 4 of ours ends with the interface started on what
        # is there (below, where every failure ends): the picture comes first.
        start_after_failure = interface_down
        try:
            settings_restored = await _async_recover_abandoned(
                session,
                remote_helper,
                claim.stdout,
                nonce,
                settings_for=gui.unrestored if gui is not None and interface_down else frozenset(),
            )
        except InstallerError as err:
            if not interface_down:
                raise
            # The interrupted transaction could not be put back, and the interface is
            # started on what it left: "put back as it was" would not be true, and the
            # snapshot stays for a person.
            raise InstallerError(
                InstallerErrorCode.ROLLBACK_FAILED,
                "the snapshot of the interrupted transaction could not be put back "
                f"({err.code.value}); the interface is started on the files that are there",
            ) from err
        if settings_restored:
            # The settings block is now the snapshot's: what was read from it before - the
            # identity, whether the plugin is switched on, its name - may no longer hold.
            identity = await _async_read_receiver_identity(session, remote_helper)
            stored_name = _check_receiver_identity(identity, request)
            plugin_enabled = _stored_setting(identity, "enabled") is True
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
        if remote_hash.stdout.strip() != source.sha256:
            raise InstallerError(InstallerErrorCode.UPLOAD_FAILED)

        await _async_measure_preflight(
            session,
            bundle_size=len(bundle_bytes),
            restart_guards=webif,
            webif=webif,
            strict_records=not request.force,
        )
        await _async_progress(progress_cb, "install")
        install_started = True
        await _run_checked(
            session,
            "opkg install --force-reinstall "
            + ("--force-downgrade " if force_downgrade else "")
            + shlex.quote(remote_ipk),
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
            session,
            bundle_size=len(bundle_bytes),
            restart_guards=webif,
            webif=webif,
            strict_records=not request.force,
        )
        # Read before the restart, so that afterwards a process that was not running
        # then is proof the interface really went down and came back. An empty set is a
        # perfectly ordinary answer - a box still booting has no Enigma yet - and any
        # pid at all afterwards is then the proof.
        old_enigma_pids = await _async_enigma_pids(session)
        base_topic, node_id = request.target()
        # A forced reinstall of a plugin that is switched off has no announcement to wait
        # for - its proof is the log - so nothing listens on the broker for it.
        if plugin_enabled or not request.force:
            watch = await _async_watch_restart(
                hass,
                base_topic,
                node_id,
                source.version,
                require_offline=request.expect_running,
                commit=source.commit,
            )
            try:
                async with asyncio.timeout(5):
                    await watch.established
            except TimeoutError as err:
                raise InstallerError(InstallerErrorCode.RESTART_FAILED) from err

        if gui is not None and gui.mode != _GUI_RUNNING:
            # The last read before the interface is stopped or started, and the only one
            # that decides whether it may be: an interface that runs by now - a box that
            # was booting, a person's `init 3` - is guarded and restarted as a running one.
            gui = await _async_recheck_forced_gui(session, gui)
            if gui.mode == _GUI_RUNNING:
                webif = True
                start_after_failure = False
                await _async_measure_preflight(
                    session,
                    bundle_size=len(bundle_bytes),
                    restart_guards=True,
                    webif=True,
                    strict_records=False,
                )
                old_enigma_pids = await _async_enigma_pids(session)

        if gui is None or gui.mode == _GUI_RUNNING:
            # What the restart has to keep - the channel, standby, the channel-list
            # bouquet - recorded as late as possible, so that it is what the household is
            # watching.
            record = await restart_rule.async_record(
                hass, session, remote_helper, base_topic, node_id
            )
        else:
            # Nothing plays without an interface. What there is to keep is what an
            # interrupted transaction of ours recorded, or nothing.
            record = gui.record

        await _async_progress(progress_cb, "restart")
        if watch is not None:
            watch.arm()
        # From the request until a new enigma2 is seen, nothing may end in the stop-and-
        # restore rollback: the image may be asking a question on the television because
        # timeshift runs or a job works, and `init 4` would break exactly what it asks
        # about. A connection lost here is connected again; one that will not come back,
        # or Home Assistant stopping, leaves the transaction as it is - locked, with its
        # id in the lock and its snapshot beside it - for the next install to recover.
        if gui is not None and gui.mode != _GUI_RUNNING:
            # No interface to restart cleanly: a respawn loop, or runlevel 4 of ours. It is
            # started - after `init 4` for the loop - by one detached command, and the new
            # pid is waited for. From here a failure is a rollback like any other, by R2.
            restart_started = True
            await _run_checked(
                session,
                f"python3 {shlex.quote(remote_helper)} respawn"
                + (" --stop" if gui.mode == _GUI_RESPAWN else ""),
                InstallerErrorCode.RESTART_FAILED,
                detail="the receiver's interface could not be started",
            )
            try:
                await _async_wait_for_enigma(session, old_enigma_pids)
            except InstallerError as err:
                raise InstallerError(
                    InstallerErrorCode.RESTART_FAILED,
                    "the receiver's interface did not start after the forced reinstall",
                ) from err
        else:
            no_rollback = True
            session, outcome = await _async_clean_restart(
                session, request.credentials, connector, remote_helper, old_enigma_pids
            )
            if outcome == _UNOBSERVED:
                raise InstallerError(
                    InstallerErrorCode.RESTART_UNOBSERVED,
                    "the receiver was asked to restart its interface and could not be reached "
                    "afterwards",
                )
            if outcome in (_QUESTION, _UNCONFIRMED):
                # The image asked on the television instead of restarting. Nothing was
                # stopped; the old enigma2 still runs. Put the old files back under it and
                # say so - unless the receiver restarted meanwhile.
                session, withdrawal = await _async_withdraw(
                    session,
                    request.credentials,
                    connector,
                    remote_helper,
                    backup,
                    provision_started,
                )
                session, pids = (
                    (session, None)
                    if withdrawal == _UNOBSERVED
                    else await _async_pids_within(
                        session, request.credentials, connector, RESTART_QUESTION_TIMEOUT
                    )
                )
                if pids is None:
                    raise InstallerError(
                        InstallerErrorCode.RESTART_UNOBSERVED,
                        "the receiver asked on screen whether to restart and could not be "
                        "reached while the previous files were being put back",
                    )
                if pids == old_enigma_pids:
                    # Compared with the enigma2 from before the request, not with a later
                    # reading: a restart that began after the last look is still a restart.
                    remote_lock_claimed = not await _async_release_after_withdraw(
                        session,
                        remote_helper,
                        remote_lock,
                        remote_ipk,
                        remote_manifest,
                        remote_provision_tmp,
                    )
                    if withdrawal == "overlap":
                        _LOGGER.warning(
                            "Installer: the previous plugin files are back, but another opkg run "
                            "held opkg's lock for part of it and may have changed its database"
                        )
                    if remote_lock_claimed:
                        raise InstallerError(InstallerErrorCode.ROLLBACK_LOCK_FAILED)
                    raise InstallerError(
                        _withdrawn_code(outcome, withdrawal),
                        "the receiver asked on screen whether to restart its interface; the "
                        f"update was withdrawn ({withdrawal}) and nothing was restarted",
                    )
                # Somebody answered the question: the receiver restarted.
                no_rollback = False
                restart_started = True
                if withdrawal in ("done", "overlap"):
                    # The files on disk are the old ones now, whichever the new enigma2 read
                    # - and when the answer came while the withdraw waited for opkg's lock,
                    # it read the new ones. A proof would pass on a plugin whose files are
                    # gone, so this is a failed restart: the rollback makes both agree.
                    raise InstallerError(
                        InstallerErrorCode.RESTART_FAILED,
                        "the receiver restarted while the previous plugin files were being "
                        "put back",
                    )
                # Nothing was put back, so the new files are what started: the proof decides.
        no_rollback = False
        restart_started = True
        try:
            await session.close()
        except BaseException:
            _LOGGER.warning("SSH close failed after receiver restart; continuing verification")
        session = None
        await _async_progress(progress_cb, "announcement")
        if request.force:
            await _async_prove_forced(
                request.credentials, connector, remote_helper, old_enigma_pids, watch
            )
        else:
            assert watch is not None
            try:
                async with asyncio.timeout(ANNOUNCEMENT_TIMEOUT):
                    await watch.completed
            except TimeoutError as err:
                raise InstallerError(InstallerErrorCode.ANNOUNCEMENT_TIMEOUT) from err

        cleanup = await connector(request.credentials)
        try:
            if not await _async_enigma_pids(cleanup) - old_enigma_pids:
                raise InstallerError(InstallerErrorCode.RESTART_FAILED)
            released = await cleanup.run(
                f"python3 {shlex.quote(remote_helper)} release {shlex.quote(remote_lock)}",
                timeout=30,
            )
            if released.exit_status:
                raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
            remote_lock_claimed = False
            committed = True
            # R3 after the commit, never before it: the install has proved itself, and
            # nothing that goes wrong while the channel is checked - a dropped
            # connection, Home Assistant stopping - may roll it back.
            await _async_verify_restart(
                cleanup,
                remote_helper,
                record,
                "clean" if gui is None or gui.mode == _GUI_RUNNING else "stopped",
                hass,
                (base_topic, node_id),
            )
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
        if force_downgrade and not request.force:
            # The confirmed downgrade's older plugin retracts what the newer one published.
            # A forced reinstall asks the plugin for nothing, this included.
            await _async_reset_after_downgrade(hass, base_topic, node_id, source.version)
        try:
            await _async_progress(progress_cb, "done")
        except BaseException:
            _LOGGER.warning("Installed plugin verified; final progress callback failed")
        return InstallResult(
            source.version, backup_created, True, provisioned_name, plugin_enabled
        )
    except BaseException as err:
        if committed:
            if isinstance(err, asyncio.CancelledError):
                # Committed is committed; a stop is still a stop, and the caller must see it.
                _LOGGER.warning("Installed plugin verified; stopped before tidying up")
                raise
            _LOGGER.warning("Installed plugin verified; ignoring post-commit cleanup failure")
            return InstallResult(
                source.version, backup_created, True, provisioned_name, plugin_enabled
            )
        if no_rollback:
            # A restart was asked for and no new enigma2 has been seen. Whatever
            # happened - withdrawn and released, or not observable at all - a rollback
            # now would stop an interface the image may be keeping up on purpose.
            if remote_lock_claimed:
                _LOGGER.error(
                    "Installer: the receiver was asked to restart its interface and the "
                    "outcome was not seen (%s). Nothing was undone: the new plugin files "
                    "are on the receiver, and the transaction lock stays at %s with this "
                    "transaction's id %s, so the next install - once the lock is stale - "
                    "puts back the snapshot %s first",
                    type(err).__name__,
                    remote_lock,
                    nonce,
                    backup,
                )
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
        if start_after_failure and not restart_started and not committed:
            await _async_start_after_failure(hass, request.credentials, connector, remote_helper)
        if remote_lock_claimed:
            _LOGGER.error(
                "Installer transaction lock remains on %s:%s",
                request.credentials.host,
                request.credentials.port,
            )

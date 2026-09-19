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

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import HomeAssistant, callback
from packaging.version import InvalidVersion, Version

from . import installer_helper
from .box import parse_json_payload, state_topic
from .bundle import BundledPlugin, BundleError, load_bundled_plugin
from .const import TOPIC_INFO

_LOGGER = logging.getLogger(__name__)

PACKAGE = "enigma2-plugin-extensions-mqttbridge"
PLUGIN_DIR = "/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
PROVISION_PATH = "/etc/enigma2/mqttbridge.json"
OPKG_STATUS = "/usr/lib/opkg/status"
OPKG_INFO_GLOB = f"/usr/lib/opkg/info/{PACKAGE}.*"
ENIGMA_SETTINGS = "/etc/enigma2/settings"
MIN_PYTHON = (3, 9)
MIN_FREE_BYTES = 2 * 1024 * 1024
TIMER_GUARD_SECONDS = 10 * 60
ANNOUNCEMENT_TIMEOUT = 120.0

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
    BUNDLE_MISSING = "bundle_missing"
    BUNDLE_INVALID = "bundle_invalid"
    UPLOAD_FAILED = "upload_failed"
    INSTALL_FAILED = "install_failed"
    NEWER_INSTALLED = "newer_installed"
    PROVISION_FAILED = "provision_failed"
    RESTART_FAILED = "restart_failed"
    ANNOUNCEMENT_TIMEOUT = "announcement_timeout"
    ROLLBACK_FAILED = "rollback_failed"
    BUSY = "busy"
    IDENTITY_MISMATCH = "identity_mismatch"


class InstallerError(Exception):
    """An expected installer failure with no credentials in its text."""

    def __init__(self, code: InstallerErrorCode, detail: str = "") -> None:
        super().__init__(code.value)
        self.code = code
        self.detail = detail


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
    base_topic: str = "enigma2"
    friendly_name: str | None = None

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
        if self.friendly_name:
            values["friendly_name"] = self.friendly_name
        return (json.dumps(values, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """Everything needed for one install or update transaction."""

    credentials: SshCredentials
    provisioning: Provisioning | None = None
    keep_credentials: bool = False
    expect_running: bool = False
    node_id: str = ""
    base_topic: str = "enigma2"

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
    import asyncssh

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
    import asyncssh

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
    except asyncssh.HostKeyNotVerifiable as err:
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
) -> CommandResult:
    """Run a fixed command and map failures without retaining its output."""
    try:
        result = await session.run(command, input=input, timeout=timeout)
    except (OSError, TimeoutError) as err:
        raise InstallerError(code) from err
    if result.exit_status:
        raise InstallerError(code)
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


async def _async_measure_preflight(session: InstallerSession, *, bundle_size: int = 0) -> Preflight:
    """Measure every install guard through fixed receiver-local commands."""
    image = (
        await _run_checked(
            session,
            "cat /etc/image-version 2>/dev/null || cat /etc/issue 2>/dev/null",
            InstallerErrorCode.PREFLIGHT_FAILED,
        )
    ).stdout.strip()[:200]
    python_version = (
        await _run_checked(
            session,
            "python3 -c 'import sys; print(\".\".join(map(str, sys.version_info[:3])))'",
            InstallerErrorCode.PREFLIGHT_FAILED,
        )
    ).stdout.strip()
    if _python_version(python_version)[:2] < MIN_PYTHON:
        raise InstallerError(InstallerErrorCode.UNSUPPORTED_PYTHON)
    free_raw = (
        await _run_checked(
            session,
            "for p in /tmp /usr /etc; do df -Pk \"$p\" | awk 'NR==2 {print $4 * 1024}'; done",
            InstallerErrorCode.PREFLIGHT_FAILED,
        )
    ).stdout.splitlines()
    try:
        free_values = [int(float(value)) for value in free_raw]
    except ValueError as err:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED) from err
    if len(free_values) != 3 or any(value < 0 for value in free_values):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    plugin_size_raw = (
        await _run_checked(
            session,
            f"if test -d {PLUGIN_DIR}; then du -sk {PLUGIN_DIR} | awk '{{print $1 * 1024}}'; "
            "else echo 0; fi",
            InstallerErrorCode.PREFLIGHT_FAILED,
        )
    ).stdout.strip()
    try:
        plugin_size = int(float(plugin_size_raw))
    except ValueError as err:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED) from err
    required_tmp = max(MIN_FREE_BYTES, bundle_size * 2 + plugin_size + 1024 * 1024)
    required_usr = max(MIN_FREE_BYTES, bundle_size * 3 + plugin_size * 2)
    required_etc = 128 * 1024
    if any(
        free < required
        for free, required in zip(
            free_values, (required_tmp, required_usr, required_etc), strict=True
        )
    ):
        raise InstallerError(InstallerErrorCode.NO_SPACE)
    await _run_checked(
        session,
        "command -v opkg >/dev/null && opkg --version >/dev/null",
        InstallerErrorCode.PREFLIGHT_FAILED,
    )
    files_result = await session.run(f"test -d {PLUGIN_DIR}", timeout=30)
    if files_result.exit_status not in (0, 1):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    plugin_files_present = files_result.exit_status == 0
    status_result = await session.run(f"opkg status {PACKAGE}", timeout=30)
    if status_result.exit_status not in (0, 1):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    installed = None
    if status_result.stdout.strip():
        package_seen = False
        for line in status_result.stdout.splitlines():
            if line == f"Package: {PACKAGE}":
                package_seen = True
            if line.startswith("Version: "):
                if installed is not None:
                    raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
                installed = line.removeprefix("Version: ").strip()
        if not package_seen or not installed:
            raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
        _package_version(installed)
    status = await _run_checked(
        session,
        "wget -qO- http://127.0.0.1/api/statusinfo",
        InstallerErrorCode.PREFLIGHT_FAILED,
    )
    timers = await _run_checked(
        session,
        "wget -qO- http://127.0.0.1/api/timerlist",
        InstallerErrorCode.PREFLIGHT_FAILED,
    )
    recording, timers_due = _timer_guard(status.stdout, timers.stdout)
    if recording:
        raise InstallerError(InstallerErrorCode.RECORDING)
    if timers_due:
        raise InstallerError(InstallerErrorCode.TIMER_DUE)
    return Preflight(
        image,
        python_version,
        min(free_values),
        installed,
        plugin_files_present,
        recording,
        timers_due,
    )


async def _async_enigma_pid(session: InstallerSession) -> int:
    """Return the single running Enigma PID, refusing an ambiguous lifecycle."""
    result = await _run_checked(session, "pidof enigma2", InstallerErrorCode.RESTART_FAILED)
    values = result.stdout.split()
    if len(values) != 1 or not values[0].isdigit():
        raise InstallerError(InstallerErrorCode.RESTART_FAILED)
    return int(values[0])


async def _async_validate_receiver_identity(
    session: InstallerSession,
    remote_helper: str,
    request: InstallRequest,
) -> None:
    """Refuse to update or overwrite a differently configured receiver."""
    result = await _run_checked(
        session,
        f"python3 {shlex.quote(remote_helper)} identity",
        InstallerErrorCode.PREFLIGHT_FAILED,
    )
    try:
        identity = json.loads(result.stdout)
    except (TypeError, ValueError) as err:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED) from err
    if not isinstance(identity, dict) or set(identity) != {
        "node_id",
        "base_topic",
        "enabled",
        "ha_mode",
    }:
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    configured_node = identity["node_id"]
    configured_base = identity["base_topic"]
    if not isinstance(configured_node, str) or not isinstance(configured_base, str):
        raise InstallerError(InstallerErrorCode.PREFLIGHT_FAILED)
    expected_base, expected_node = request.validate()
    if request.provisioning is None:
        if (
            configured_node != expected_node
            or configured_base != expected_base
            or identity["enabled"] is not True
            or identity["ha_mode"] != "integration"
        ):
            raise InstallerError(InstallerErrorCode.IDENTITY_MISMATCH)
    elif configured_node and (configured_node != expected_node or configured_base != expected_base):
        raise InstallerError(InstallerErrorCode.IDENTITY_MISMATCH)


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
) -> None:
    """Restore only installer-owned paths and the exact pre-transaction metadata."""
    session = await connector(credentials)
    enigma_stopped = False
    old_enigma_pid: int | None = None
    rollback_error: BaseException | None = None
    try:
        if restart_started:
            # Re-measure the guard if it can still be measured, then stop Enigma before
            # restoring settings so shutdown cannot overwrite the old values.
            #
            # Nothing measured here may veto the restore. The preflight needs OpenWebif,
            # which is an Enigma plugin, so it is down in exactly the failure this
            # rollback exists for — a half-restarted or non-booting receiver. Letting it
            # raise meant the restore never ran at all and the box was left on the new
            # plugin. A receiver whose interface is down is also not recording, so the
            # recording guard has nothing left to protect.
            with suppress(InstallerError):
                await _async_measure_preflight(session)
            with suppress(InstallerError):
                old_enigma_pid = await _async_enigma_pid(session)
            enigma_stopped = True
            stopped = await session.run("init 4 || exit $?; sleep 3", timeout=30)
            if stopped.exit_status:
                raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
        commands = [
            f"rm -f {shlex.quote(remote_ipk)} {shlex.quote(remote_provision_tmp)} "
            f"{shlex.quote(remote_manifest)}"
        ]
        if install_started:
            commands.append(
                f"python3 {shlex.quote(remote_helper)} restore {shlex.quote(backup)}"
                + (" --provisioning" if provision_started else "")
                + (" --settings" if restart_started else "")
            )
        result = await session.run("set -eu; " + "; ".join(commands), timeout=60)
        if result.exit_status:
            raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
    except BaseException as err:
        rollback_error = err
    finally:
        if enigma_stopped:
            try:
                restart = await session.run("init 3", timeout=30)
                if restart.exit_status:
                    raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
                # `_async_enigma_pid` refuses anything but one running Enigma, so this
                # proves the receiver came back. A pid that never changed means it never
                # went down; no pid beforehand means Enigma was already stopped when the
                # rollback began, and any single live process is then the proof.
                restarted_pid = await _async_enigma_pid(session)
                if old_enigma_pid is not None and restarted_pid == old_enigma_pid:
                    raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
            except BaseException as restart_err:
                if rollback_error is None:
                    rollback_error = restart_err
                else:
                    rollback_error.add_note("Enigma restart after rollback also failed")
        await session.close()
    if rollback_error is not None:
        raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED) from rollback_error
    released = await connector(credentials)
    try:
        result = await released.run(
            f"python3 {shlex.quote(remote_helper)} release {shlex.quote(remote_lock)}",
            timeout=30,
        )
        if result.exit_status:
            raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
        # The helper had to outlive the restore that used it; nothing needs it now.
        await _async_remove_helper(released, remote_helper)
    finally:
        await released.close()


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
    backup = f"/home/root/mqttbridge-backups/ha-installer-{nonce}"
    remote_lock = "/home/root/mqttbridge-backups/.ha-installer.lock"
    session: InstallerSession | None = None
    watch: _RestartWatch | None = None
    install_started = False
    provision_started = False
    restart_started = False
    backup_created = False
    remote_lock_claimed = False
    committed = False
    try:
        await _async_progress(progress_cb, "preflight")
        session = await connector(request.credentials)
        preflight = await _async_measure_preflight(session, bundle_size=len(bundle_bytes))
        if preflight.installed_version and _package_version(
            preflight.installed_version
        ) > _package_version(bundle.version):
            raise InstallerError(InstallerErrorCode.NEWER_INSTALLED)

        await _async_progress(progress_cb, "backup")
        helper_bytes = await hass.async_add_executor_job(Path(installer_helper.__file__).read_bytes)
        await _run_checked(
            session,
            f"umask 077; cat > {shlex.quote(remote_helper)}",
            InstallerErrorCode.INSTALL_FAILED,
            input=helper_bytes,
        )
        await _async_validate_receiver_identity(session, remote_helper, request)
        claim = await session.run(
            f"mkdir -p /home/root/mqttbridge-backups && "
            f"python3 {shlex.quote(remote_helper)} claim {shlex.quote(remote_lock)}",
            timeout=30,
        )
        if claim.exit_status:
            raise InstallerError(InstallerErrorCode.BUSY)
        remote_lock_claimed = True
        await _run_checked(
            session,
            f"python3 {shlex.quote(remote_helper)} snapshot {shlex.quote(backup)}",
            InstallerErrorCode.INSTALL_FAILED,
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

        await _async_measure_preflight(session, bundle_size=len(bundle_bytes))
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
        await _async_measure_preflight(session, bundle_size=len(bundle_bytes))
        old_enigma_pid = await _async_enigma_pid(session)
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

        await _async_progress(progress_cb, "restart")
        restart_started = True
        watch.arm()
        await _run_checked(
            session,
            'sh -c \'trap "init 3" EXIT HUP INT TERM; init 4; sleep 3; '
            "init 3; trap - EXIT HUP INT TERM'",
            InstallerErrorCode.RESTART_FAILED,
            timeout=30,
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
            if await _async_enigma_pid(cleanup) == old_enigma_pid:
                raise InstallerError(InstallerErrorCode.RESTART_FAILED)
            released = await cleanup.run(
                f"python3 {shlex.quote(remote_helper)} release {shlex.quote(remote_lock)}",
                timeout=30,
            )
            if released.exit_status:
                raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED)
            remote_lock_claimed = False
            committed = True
            try:
                await cleanup.run(
                    f"rm -f {shlex.quote(remote_ipk)} {shlex.quote(remote_helper)} "
                    f"{shlex.quote(remote_manifest)}",
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
        return InstallResult(bundle.version, backup_created, True)
    except BaseException as err:
        if committed:
            _LOGGER.warning("Installed plugin verified; ignoring post-commit cleanup failure")
            return InstallResult(bundle.version, backup_created, True)
        if session is not None:
            try:
                await session.close()
            except BaseException:
                _LOGGER.warning("SSH close failed while preparing installer rollback")
            session = None
        if backup_created:
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
                )
                remote_lock_claimed = False
            except Exception as rollback_err:
                _LOGGER.error("Installer rollback failed; receiver backup remains at %s", backup)
                if isinstance(err, asyncio.CancelledError):
                    err.add_note(f"rollback failed; backup remains at {backup}")
                else:
                    raise InstallerError(InstallerErrorCode.ROLLBACK_FAILED) from rollback_err
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

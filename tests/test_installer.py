"""The SSH installer state machine, exercised without touching a receiver."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.enigma2_mqtt.bundle import BundledPlugin
from custom_components.enigma2_mqtt.installer import (
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    Provisioning,
    SshCredentials,
    _async_measure_preflight,
    _async_rollback,
    _async_validate_receiver_identity,
    _async_watch_restart,
    _package_version,
    async_install,
)

STATUS = json.dumps({"isRecording": False})
TIMERS = json.dumps({"timers": []})


@dataclass
class FakeReceiver:
    """A command-aware fake SSH receiver with persistent transaction state."""

    fail_on: str | None = None
    installed_version: str | None = "0.0.9"
    recording: bool = False
    timers: list[dict[str, Any]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    inputs: list[bytes] = field(default_factory=list)
    backup_exists: bool = False
    installed: bool = False
    rolled_back: bool = False
    enigma_pid: int = 100
    node_id: str = ""
    base_topic: str = "enigma2"
    enabled: bool = True
    ha_mode: str = "discovery"
    closed: int = 0
    close_raises_on: set[int] = field(default_factory=set)
    free_bytes: int = 50_000_000
    python_version: str = "3.12.8"
    # OpenWebif is an Enigma plugin, so it goes down with the interface. A receiver whose
    # restart went wrong answers SSH and nothing on 127.0.0.1.
    webif_ok: bool = True
    webif_dies_at_restart: bool = False
    # What is actually on the box, so a rollback can be checked by what it put back
    # rather than by which command was sent.
    files: dict[str, str] = field(
        default_factory=lambda: {
            "plugin": "old",
            "opkg": "old",
            "settings": "old",
            "provisioning": "old",
        }
    )
    restore_flags: list[str] = field(default_factory=list)
    helper_removed: bool = False

    async def connect(self, credentials: SshCredentials) -> FakeSession:
        assert credentials.host_key == "ssh-ed25519 AAAATEST"
        return FakeSession(self)


class FakeSession:
    def __init__(self, receiver: FakeReceiver) -> None:
        self.receiver = receiver

    async def run(
        self, command: str, *, input: bytes | None = None, timeout: float = 30
    ) -> CommandResult:
        del timeout
        receiver = self.receiver
        receiver.commands.append(command)
        if input is not None:
            receiver.inputs.append(input)
        if receiver.fail_on and receiver.fail_on in command:
            return CommandResult(1, "", "deliberately failed")
        if "cat /etc/image-version" in command:
            return CommandResult(0, "OpenViX 6.6\n")
        if "sys.version_info" in command:
            return CommandResult(0, f"{receiver.python_version}\n")
        if "df -Pk" in command:
            return CommandResult(0, f"{receiver.free_bytes}\n" * 3)
        if "du -sk" in command:
            return CommandResult(0, "500000\n")
        if command.endswith(" identity"):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "node_id": receiver.node_id,
                        "base_topic": receiver.base_topic,
                        "enabled": receiver.enabled,
                        "ha_mode": receiver.ha_mode,
                    }
                ),
            )
        if command.startswith("opkg status"):
            version = receiver.installed_version or ""
            return CommandResult(
                0,
                f"Package: enigma2-plugin-extensions-mqttbridge\nVersion: {version}\n"
                if version
                else "",
            )
        if "/api/statusinfo" in command:
            if not receiver.webif_ok:
                return CommandResult(1, "", "wget: can't connect to remote host")
            return CommandResult(0, json.dumps({"isRecording": receiver.recording}))
        if "/api/timerlist" in command:
            if not receiver.webif_ok:
                return CommandResult(1, "", "wget: can't connect to remote host")
            return CommandResult(0, json.dumps({"timers": receiver.timers}))
        if " snapshot " in command:
            receiver.backup_exists = True
        if command.startswith("opkg install"):
            receiver.installed = True
            receiver.files["plugin"] = "new"
            receiver.files["opkg"] = "new"
        if command.startswith("set -eu; if [ -L ") and " mv " in command:
            receiver.files["provisioning"] = "new"
        if " restore " in command:
            receiver.rolled_back = True
            receiver.restore_flags = [word for word in command.split() if word.startswith("--")]
            receiver.files["plugin"] = "old"
            receiver.files["opkg"] = "old"
            if "--provisioning" in receiver.restore_flags:
                receiver.files["provisioning"] = "old"
            if "--settings" in receiver.restore_flags:
                receiver.files["settings"] = "old"
        if "rm -f /tmp/enigma2-mqtt-installer-" in command:
            receiver.helper_removed = True
        if command == "pidof enigma2":
            return CommandResult(0, f"{receiver.enigma_pid}\n")
        if command == "init 3" or 'trap "init 3"' in command:
            receiver.enigma_pid += 1
        if 'trap "init 3"' in command:
            # Enigma writes its settings out on the way down, which is why a rollback
            # has to stop it before restoring them.
            receiver.files["settings"] = "new"
            if receiver.webif_dies_at_restart:
                receiver.webif_ok = False
        if "sha256sum" in command:
            return CommandResult(0, "a" * 64 + "\n")
        return CommandResult(0)

    async def close(self) -> None:
        self.receiver.closed += 1
        if self.receiver.closed in self.receiver.close_raises_on:
            raise OSError("simulated SSH close failure")


@pytest.fixture
def credentials() -> SshCredentials:
    return SshCredentials(
        host="192.0.2.12",
        username="root",
        password="not-a-real-password",
        host_key="ssh-ed25519 AAAATEST",
    )


@pytest.fixture
def install_request(credentials: SshCredentials) -> InstallRequest:
    return InstallRequest(
        credentials,
        Provisioning(
            broker_host="192.0.2.10",
            broker_port=1883,
            broker_username="vuuno4kse_005301",
            broker_password="not-a-real-broker-password",
            node_id="vuuno4kse_005301",
            friendly_name="Living room receiver",
        ),
    )


async def test_preflight_measures_supported_idle_receiver() -> None:
    receiver = FakeReceiver()
    measured = await _async_measure_preflight(FakeSession(receiver))
    assert measured.python_version == "3.12.8"
    assert measured.installed_version == "0.0.9"
    assert measured.recording is False
    assert measured.timers_due == 0


async def test_preflight_accepts_opkg_exit_zero_with_empty_absent_status() -> None:
    receiver = FakeReceiver(installed_version=None)
    measured = await _async_measure_preflight(FakeSession(receiver))
    assert measured.installed_version is None


def test_package_versions_handle_revisions_and_fail_closed() -> None:
    assert _package_version("0.1.0-r1") > _package_version("0.1.0-r0")
    assert _package_version("0.1.0-beta.1") < _package_version("0.1.0")
    with pytest.raises(InstallerError):
        _package_version("not-an-opkg-version")


async def test_update_refuses_receiver_with_different_identity(
    credentials: SshCredentials,
) -> None:
    receiver = FakeReceiver(node_id="another_receiver", base_topic="enigma2", ha_mode="integration")
    request = InstallRequest(
        credentials,
        provisioning=None,
        expect_running=True,
        node_id="vuuno4kse_005301",
        base_topic="enigma2",
    )

    with pytest.raises(InstallerError) as raised:
        await _async_validate_receiver_identity(FakeSession(receiver), "/tmp/helper.py", request)

    assert raised.value.code is InstallerErrorCode.IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("receiver", "code"),
    [
        (FakeReceiver(recording=True), InstallerErrorCode.RECORDING),
        (
            FakeReceiver(
                timers=[
                    {
                        "begin": 1,
                        "end": 4_000_000_000,
                        "state": 0,
                        "disabled": False,
                    }
                ]
            ),
            InstallerErrorCode.TIMER_DUE,
        ),
        (FakeReceiver(fail_on="/api/statusinfo"), InstallerErrorCode.PREFLIGHT_FAILED),
    ],
)
async def test_preflight_fails_closed_on_recording_timer_or_unknown_guard(
    receiver: FakeReceiver, code: InstallerErrorCode
) -> None:
    with pytest.raises(InstallerError) as raised:
        await _async_measure_preflight(FakeSession(receiver))
    assert raised.value.code is code


async def test_install_streams_bundle_and_provisioning_then_cleans_up(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    bundle = BundledPlugin(artifact, "0.1.0", "a" * 64, "1" * 40)
    receiver = FakeReceiver(close_raises_on={2})
    steps: list[str] = []

    async def completed_watch(*args: Any, **kwargs: Any):
        del args, kwargs
        loop = asyncio.get_running_loop()
        watch = type("Watch", (), {})()
        watch.established = loop.create_future()
        watch.completed = loop.create_future()
        watch.established.set_result(None)
        watch.completed.set_result(None)
        watch.cancel = lambda: None
        watch.arm = lambda: None
        return watch

    real_hash = __import__("hashlib").sha256(b"ipk bytes").hexdigest()
    bundle = BundledPlugin(artifact, "0.1.0", real_hash, "1" * 40)
    original_run = FakeSession.run

    async def hash_aware_run(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, real_hash + "\n")
        return await original_run(self, command, **kwargs)

    with (
        patch("custom_components.enigma2_mqtt.installer.load_bundled_plugin", return_value=bundle),
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch("custom_components.enigma2_mqtt.installer._async_watch_restart", completed_watch),
        patch.object(FakeSession, "run", hash_aware_run),
    ):
        result = await async_install(
            hass, install_request, steps.append, _connector=receiver.connect
        )

    assert result.version == "0.1.0"
    assert receiver.installed is True
    assert b"ipk bytes" in receiver.inputs
    provision = next(json.loads(value) for value in receiver.inputs if value.startswith(b"{"))
    assert provision["ha_mode"] == "integration"
    assert provision["node_id"] == "vuuno4kse_005301"
    assert steps == [
        "preflight",
        "backup",
        "upload",
        "install",
        "provision",
        "restart",
        "announcement",
        "done",
    ]
    joined = "\n".join(receiver.commands)
    assert "not-a-real-password" not in joined
    assert "not-a-real-broker-password" not in joined


async def test_install_failure_restores_preexisting_plugin(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    bundle = BundledPlugin(artifact, "0.1.0", digest, "1" * 40)
    receiver = FakeReceiver(
        fail_on="cat > /etc/enigma2/mqttbridge.json.ha-", close_raises_on={1}
    )
    original_run = FakeSession.run

    async def hash_aware_run(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, digest + "\n")
        return await original_run(self, command, **kwargs)

    with (
        patch("custom_components.enigma2_mqtt.installer.load_bundled_plugin", return_value=bundle),
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch.object(FakeSession, "run", hash_aware_run),
    ):
        with pytest.raises(InstallerError) as raised:
            await async_install(hass, install_request, _connector=receiver.connect)

    assert raised.value.code is InstallerErrorCode.PROVISION_FAILED
    assert receiver.backup_exists is True
    assert receiver.installed is True
    assert receiver.rolled_back is True


async def test_cancellation_runs_rollback_and_remains_cancelled(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    bundle = BundledPlugin(artifact, "0.1.0", digest, "1" * 40)
    receiver = FakeReceiver()
    original_run = FakeSession.run

    async def cancelling_run(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            return CommandResult(0, digest + "\n")
        if command.startswith("opkg install"):
            self.receiver.installed = True
            raise asyncio.CancelledError
        return await original_run(self, command, **kwargs)

    with (
        patch("custom_components.enigma2_mqtt.installer.load_bundled_plugin", return_value=bundle),
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch.object(FakeSession, "run", cancelling_run),
    ):
        with pytest.raises(asyncio.CancelledError):
            await async_install(hass, install_request, _connector=receiver.connect)

    assert receiver.backup_exists is True


async def test_rollback_restores_a_receiver_whose_openwebif_died_at_the_restart(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    """The failure the rollback exists for is the one that used to disable it.

    The receiver's interface does not come back, so OpenWebif — which is an Enigma
    plugin — stops answering and the announcement never arrives. The rollback re-measured
    the install guards over that same dead OpenWebif, the measurement failed, and the
    restore was abandoned before a single command was sent: the box was left running the
    new plugin with the old one nowhere. The guards may inform the rollback; they may
    not veto it.
    """
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    bundle = BundledPlugin(artifact, "0.1.0", digest, "1" * 40)
    receiver = FakeReceiver(webif_dies_at_restart=True)
    original_run = FakeSession.run

    async def hash_aware_run(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, digest + "\n")
        return await original_run(self, command, **kwargs)

    async def never_announced(*args: Any, **kwargs: Any):
        del args, kwargs
        loop = asyncio.get_running_loop()
        watch = type("Watch", (), {})()
        watch.established = loop.create_future()
        watch.established.set_result(None)
        watch.completed = loop.create_future()
        watch.cancel = lambda: watch.completed.cancel()
        watch.arm = lambda: None
        return watch

    with (
        patch("custom_components.enigma2_mqtt.installer.load_bundled_plugin", return_value=bundle),
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch("custom_components.enigma2_mqtt.installer._async_watch_restart", never_announced),
        patch("custom_components.enigma2_mqtt.installer.ANNOUNCEMENT_TIMEOUT", 0.01),
        patch.object(FakeSession, "run", hash_aware_run),
    ):
        with pytest.raises(InstallerError) as raised:
            await async_install(hass, install_request, _connector=receiver.connect)

    # The caller is told what actually went wrong, not that the recovery went wrong.
    assert raised.value.code is InstallerErrorCode.ANNOUNCEMENT_TIMEOUT
    assert receiver.rolled_back is True
    assert sorted(receiver.restore_flags) == ["--provisioning", "--settings"]
    # Everything the transaction touched is back as it was.
    assert receiver.files == {
        "plugin": "old",
        "opkg": "old",
        "settings": "old",
        "provisioning": "old",
    }
    # Enigma was stopped before the settings were restored and started again afterwards.
    assert "init 4 || exit $?; sleep 3" in receiver.commands
    assert receiver.commands.index("init 4 || exit $?; sleep 3") < next(
        index for index, command in enumerate(receiver.commands) if " restore " in command
    )
    assert "init 3" in receiver.commands
    assert receiver.helper_removed is True


async def test_rollback_restarts_enigma_when_restore_transport_raises(
    credentials: SshCredentials,
) -> None:
    receiver = FakeReceiver()

    class RaisingRestoreSession(FakeSession):
        async def run(
            self, command: str, *, input: bytes | None = None, timeout: float = 30
        ) -> CommandResult:
            if " restore " in command:
                self.receiver.commands.append(command)
                raise OSError("simulated transport loss")
            return await super().run(command, input=input, timeout=timeout)

    async def connect(_credentials: SshCredentials) -> RaisingRestoreSession:
        return RaisingRestoreSession(receiver)

    with pytest.raises(InstallerError) as raised:
        await _async_rollback(
            credentials,
            "/backup",
            "/tmp/plugin.ipk",
            "/tmp/helper.py",
            "/tmp/manifest",
            "/etc/enigma2/mqttbridge.json.ha-abc123",
            "/tmp/lock",
            True,
            True,
            True,
            connect,
        )

    assert raised.value.code is InstallerErrorCode.ROLLBACK_FAILED
    assert "init 4 || exit $?; sleep 3" in receiver.commands
    assert "init 3" in receiver.commands


async def test_restart_watch_ignores_retained_and_requires_upgrade_offline(
    hass: HomeAssistant,
) -> None:
    callbacks: dict[str, Any] = {}

    async def subscribe(_hass: HomeAssistant, topic: str, callback: Any):
        callbacks[topic] = callback
        return lambda: None

    def subscribed(_hass: HomeAssistant, _topic: str, _qos: int, callback: Any) -> Any:
        callback()
        return lambda: None

    with (
        patch(
            "custom_components.enigma2_mqtt.installer.mqtt.async_subscribe",
            subscribe,
        ),
        patch(
            "custom_components.enigma2_mqtt.installer.mqtt.async_on_subscribe_done",
            subscribed,
        ),
    ):
        watch = await _async_watch_restart(
            hass, "enigma2", "vuuno4kse_005301", "0.1.0", require_offline=True
        )
        watch.arm()
        availability = callbacks["enigma2/vuuno4kse_005301/availability"]
        info = callbacks["enigma2/vuuno4kse_005301/info"]
        availability(SimpleNamespace(payload="offline", retain=True))
        availability(SimpleNamespace(payload="online", retain=False))
        info(SimpleNamespace(payload='{"plugin":"0.1.0","ha_mode":"integration"}', retain=False))
        assert not watch.completed.done()
        availability(SimpleNamespace(payload="offline", retain=False))
        availability(SimpleNamespace(payload="online", retain=False))
        info(SimpleNamespace(payload='{"plugin":"0.1.0","ha_mode":"integration"}', retain=False))
        assert watch.completed.done()
        watch.cancel()


async def test_restart_watch_cleans_partial_subscription_failure(
    hass: HomeAssistant,
) -> None:
    cancelled: list[str] = []
    subscribe_count = 0

    async def subscribe(_hass: HomeAssistant, topic: str, _callback: Any):
        nonlocal subscribe_count
        subscribe_count += 1
        if subscribe_count == 2:
            raise OSError("second subscription failed")
        return lambda: cancelled.append(f"subscription:{topic}")

    def subscribed(_hass: HomeAssistant, topic: str, _qos: int, _callback: Any) -> Any:
        return lambda: cancelled.append(f"tracker:{topic}")

    with (
        patch(
            "custom_components.enigma2_mqtt.installer.mqtt.async_subscribe",
            subscribe,
        ),
        patch(
            "custom_components.enigma2_mqtt.installer.mqtt.async_on_subscribe_done",
            subscribed,
        ),
        pytest.raises(OSError, match="second subscription"),
    ):
        await _async_watch_restart(
            hass, "enigma2", "vuuno4kse_005301", "0.1.0", require_offline=False
        )

    assert sorted(cancelled) == [
        "subscription:enigma2/vuuno4kse_005301/availability",
        "tracker:enigma2/vuuno4kse_005301/availability",
        "tracker:enigma2/vuuno4kse_005301/info",
    ]


async def test_same_host_install_is_serialized(
    hass: HomeAssistant, install_request: InstallRequest
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_install(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        entered.set()
        await release.wait()
        return object()

    with patch("custom_components.enigma2_mqtt.installer._async_install_locked", held_install):
        first = asyncio.create_task(async_install(hass, install_request))
        await entered.wait()
        with pytest.raises(InstallerError) as raised:
            await async_install(hass, install_request)
        release.set()
        await first

    assert raised.value.code is InstallerErrorCode.BUSY

"""The SSH installer state machine, exercised without touching a receiver."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.enigma2_mqtt import installer, installer_helper
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

STATUS = json.dumps({"isRecording": False, "inStandby": "false", "isStreaming": "false"})
TIMERS = json.dumps({"timers": []})
WATCHED = "1:0:19:283D:3FB:1:C00000:0:0:0:"
SAVED_EARLIER = "1:0:19:2B66:3F3:1:C00000:0:0:0:"


@dataclass
class FakeReceiver:
    """A command-aware fake SSH receiver with persistent transaction state.

    It models what the restart rule depends on: enigma2's pid and runlevel, OpenWebif's
    state (the channel, standby, streaming), the saved `config.tv.lastservice`, the
    image's question on a clean restart, and the stop-and-restore script, which runs on
    the receiver whatever happens to the connection that started it.
    """

    fail_on: str | None = None
    # A helper step refused because opkg's lock stayed held: the helper's own exit
    # status for it, and nothing done.
    busy_on: str | None = None
    # A helper step that finished and then found opkg's lock file had lost its name.
    lock_lost_on: str | None = None
    # The timeout each command was run under, in order, so that a test can see that a
    # step runs under the bound its helper's wait was sized for.
    timeouts: list[tuple[str, float]] = field(default_factory=list)
    installed_version: str | None = "0.0.9"
    recording: bool = False
    timers: list[dict[str, Any]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    inputs: list[bytes] = field(default_factory=list)
    backup_exists: bool = False
    installed: bool = False
    rolled_back: bool = False
    enigma_pid: int = 100
    # Some images run a wrapper beside the interface it starts; the wrapper survives a
    # GUI restart and `pidof enigma2` reports both, for ever.
    enigma_wrapper_pid: int | None = None
    enigma_running: bool = True
    # A restart that takes the interface down and does not bring it back. On a wrapper
    # image that still leaves a pid answering `pidof enigma2` - the wrapper's.
    enigma_dies_at_restart: bool = False
    # What `read_identity` answers, and `None` is the answer for a setting enigma2 has
    # no line for. It has no line for any setting still at its default, so a receiver
    # whose plugin has never been configured says nothing about all four of these - and
    # one that has been configured normally still says nothing about its base topic.
    # These defaults are therefore a real box, not an empty one - including a box with
    # no name of its own, which is what a plugin that has never run leaves behind.
    node_id: str | None = None
    base_topic: str | None = None
    enabled: bool | None = None
    ha_mode: str | None = None
    friendly_name: str | None = None
    closed: int = 0
    close_raises_on: set[int] = field(default_factory=set)
    free_bytes: int = 50_000_000
    python_version: str = "3.12.8"
    # OpenWebif is an Enigma plugin, so it goes down with the interface. A receiver whose
    # restart went wrong answers SSH and nothing on 127.0.0.1.
    webif_ok: bool = True
    webif_dies_at_restart: bool = False
    # What OpenWebif reports, and what the settings file would start the image on.
    service: str | None = WATCHED
    saved_service: str = SAVED_EARLIER
    standby: bool = False
    streaming: bool = False
    # OpenWebif's power state 3 opens the image's own `TryQuitMainloop`, which asks on
    # the television - and waits for ever - while timeshift runs or a job is working.
    question_on_restart: bool = False
    # Somebody answers "yes" to that question while the installer is putting the old
    # files back.
    question_answered_during_withdraw: bool = False
    restarts: list[str] = field(default_factory=list)
    zaps: list[str] = field(default_factory=list)
    # An image that does not start on the saved channel - hypothesis H2 of the restart
    # rule being wrong - starts on this one instead.
    start_channel: str | None = None
    zap_takes_effect: bool = True
    # A restarted interface takes a while before OpenWebif answers: this many reads of
    # the state after a restart find nothing.
    blind_reads_after_restart: int = 0
    # A claim that reclaims an abandoned installer lock reports its id; the snapshot
    # named after it may or may not still be there.
    reclaimed_id: str = ""
    snapshots: set[str] = field(default_factory=set)
    # The stop-and-restore script's status lines, as its status file holds them.
    r2_steps: list[str] = field(default_factory=list)
    r2_restore_status: int = 0
    r2_init3_status: int = 0
    # An enigma2 that does not go away when told to stop.
    r2_stop_timeout: bool = False
    # The stop-and-restore script's own directories, and which of them were removed.
    r2_dirs: set[str] = field(default_factory=set)
    r2_removed: list[str] = field(default_factory=list)
    withdrawn: bool = False
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

    def start_interface(self, how: str) -> None:
        """Start enigma2 again, on the channel its settings file names.

        The install's restart is the one a test can break; a rollback's restart, onto the
        plugin that worked before, is the one that works in these fakes.
        """
        self.restarts.append(how)
        self.enigma_pid += 1
        self.enigma_running = how == "stopped" or not self.enigma_dies_at_restart
        if how == "stopped":
            self.webif_ok = True
        elif self.webif_dies_at_restart:
            self.webif_ok = False
        self.service = (
            (self.start_channel or self.saved_service) if self.enigma_running else None
        )

    def run_r2(self, command: str) -> None:
        """What the detached script does on the receiver: stop, restore, write, start."""
        self.r2_steps.append("begun 4242")
        self.r2_steps.append("stopping 0")
        stopped = not self.r2_stop_timeout
        if stopped:
            self.enigma_running = False
            self.r2_steps.append("stopped")
        else:
            self.r2_steps.append("stop_timeout")
        self.restore_flags = (["--settings"] if stopped else []) + (
            ["--provisioning"] if "--provisioning" in command.split() else []
        )
        self.r2_steps.append(f"restored {self.r2_restore_status}")
        if self.r2_restore_status == 0:
            self.rolled_back = True
            self.files["plugin"] = "old"
            self.files["opkg"] = "old"
            if stopped:
                self.files["settings"] = "old"
            if "--provisioning" in self.restore_flags:
                self.files["provisioning"] = "old"
        words = command.split()
        if stopped and "--service" in words:
            self.saved_service = words[words.index("--service") + 1]
            self.r2_steps.append("written 0")
        else:
            self.r2_steps.append("not_written")
        self.r2_steps.append(f"started {self.r2_init3_status}")
        if stopped and self.r2_init3_status == 0:
            self.start_interface("stopped")
        self.r2_steps.append("done")


class FakeSession:
    def __init__(self, receiver: FakeReceiver) -> None:
        self.receiver = receiver

    async def run(
        self, command: str, *, input: bytes | None = None, timeout: float = 30
    ) -> CommandResult:
        receiver = self.receiver
        receiver.commands.append(command)
        receiver.timeouts.append((command, timeout))
        if input is not None:
            receiver.inputs.append(input)
        if receiver.fail_on and receiver.fail_on in command:
            return CommandResult(1, "", "deliberately failed")
        if receiver.lock_lost_on and receiver.lock_lost_on in command:
            return CommandResult(
                installer_helper.EXIT_OPKG_LOCK_LOST, "", "opkg's lock file was replaced"
            )
        if receiver.busy_on and receiver.busy_on in command:
            return CommandResult(
                installer_helper.EXIT_OPKG_BUSY, "", "opkg is busy: /run/opkg.lock is held"
            )
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
                        "friendly_name": receiver.friendly_name,
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
            return CommandResult(
                0,
                json.dumps(
                    {
                        "isRecording": receiver.recording,
                        "inStandby": "true" if receiver.standby else "false",
                        "isStreaming": "true" if receiver.streaming else "false",
                        "currservice_serviceref": receiver.service or "",
                    }
                ),
            )
        if "/api/timerlist" in command:
            if not receiver.webif_ok:
                return CommandResult(1, "", "wget: can't connect to remote host")
            return CommandResult(0, json.dumps({"timers": receiver.timers}))
        if command.endswith(" record"):
            answering = receiver.webif_ok and receiver.enigma_running
            if receiver.restarts and receiver.blind_reads_after_restart:
                receiver.blind_reads_after_restart -= 1
                answering = False
            return CommandResult(
                0,
                json.dumps(
                    {
                        "service": receiver.service if answering else None,
                        "standby": receiver.standby if answering else None,
                        "saved": receiver.saved_service,
                    }
                ),
            )
        if " powerstate --state 3" in command:
            if not receiver.question_on_restart:
                # A clean quit saves the settings: the channel being watched among them,
                # and whatever the new plugin wrote into its own block.
                if receiver.service:
                    receiver.saved_service = receiver.service
                receiver.files["settings"] = "new"
                receiver.start_interface("clean")
            return CommandResult(0, '{"answered": true}\n')
        if " powerstate --state 5" in command:
            receiver.standby = True
            return CommandResult(0, '{"answered": true}\n')
        if " zap --service " in command:
            reference = command.rsplit(" ", 1)[1]
            receiver.zaps.append(reference)
            if receiver.zap_takes_effect:
                receiver.service = reference
            return CommandResult(0)
        if " claim " in command:
            return CommandResult(0, json.dumps({"reclaimed": receiver.reclaimed_id}) + "\n")
        if command.startswith("test -d /home/root/mqttbridge-backups/"):
            name = command.split("/home/root/mqttbridge-backups/", 1)[1]
            return CommandResult(0 if name in receiver.snapshots else 1)
        if " snapshot " in command:
            receiver.backup_exists = True
        if command.startswith("opkg install"):
            receiver.installed = True
            receiver.files["plugin"] = "new"
            receiver.files["opkg"] = "new"
        if command.startswith("set -eu; if [ -L ") and " mv " in command:
            receiver.files["provisioning"] = "new"
        if " withdraw " in command:
            receiver.withdrawn = True
            receiver.restore_flags = [word for word in command.split() if word.startswith("--")]
            receiver.files["plugin"] = "old"
            receiver.files["opkg"] = "old"
            if "--provisioning" in receiver.restore_flags:
                receiver.files["provisioning"] = "old"
            if receiver.question_answered_during_withdraw:
                receiver.start_interface("clean")
        if " restore " in command:
            receiver.rolled_back = True
            receiver.restore_flags = [word for word in command.split() if word.startswith("--")]
            receiver.files["plugin"] = "old"
            receiver.files["opkg"] = "old"
            if "--provisioning" in receiver.restore_flags:
                receiver.files["provisioning"] = "old"
            if "--settings" in receiver.restore_flags:
                receiver.files["settings"] = "old"
        if " r2-start " in command:
            words = command.split()
            receiver.r2_dirs.add(words[words.index("--dir") + 1])
            receiver.run_r2(command)
            return CommandResult(0, '{"pid": 4242}\n')
        if command.startswith("cat ") and command.endswith("/status"):
            if command.split()[1].rsplit("/", 1)[0] not in receiver.r2_dirs:
                return CommandResult(1, "", "cat: can't open: No such file or directory")
            return CommandResult(0, "".join(line + "\n" for line in receiver.r2_steps))
        if command.startswith("rm -rf /tmp/enigma2-mqtt-r2-"):
            receiver.r2_removed.append(command.split()[2])
        if "rm -f /tmp/enigma2-mqtt-installer-" in command or (
            command.startswith("rm -f ") and "/tmp/enigma2-mqtt-installer-" in command
        ):
            receiver.helper_removed = True
        if command == "pidof enigma2":
            pids = [receiver.enigma_wrapper_pid]
            if receiver.enigma_running:
                pids.append(receiver.enigma_pid)
            running = [str(pid) for pid in pids if pid is not None]
            if not running:
                # What `pidof` does when nothing matches: exit 1, and say nothing.
                return CommandResult(1, "", "")
            return CommandResult(0, " ".join(running) + "\n")
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


async def _async_committed_install(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    receiver: FakeReceiver,
):
    """Drive one install that reaches its commit, against a fake receiver."""
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = hashlib.sha256(b"ipk bytes").hexdigest()
    bundle = BundledPlugin(artifact, "0.1.0", digest, "1" * 40)
    original_run = FakeSession.run

    async def hash_aware_run(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, digest + "\n")
        return await original_run(self, command, **kwargs)

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

    with (
        patch("custom_components.enigma2_mqtt.installer.load_bundled_plugin", return_value=bundle),
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch("custom_components.enigma2_mqtt.installer._async_watch_restart", completed_watch),
        patch.object(FakeSession, "run", hash_aware_run),
    ):
        return await async_install(hass, install_request, _connector=receiver.connect)


async def test_a_committed_install_prunes_superseded_snapshots(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    """Otherwise every guided install leaves another plugin directory on the flash.

    It has to happen after the lock is released - the receiver is free at that point -
    and before the helper that does it is deleted.
    """
    receiver = FakeReceiver()

    result = await _async_committed_install(hass, install_request, tmp_path, receiver)

    assert result.restarted is True
    prune = next(index for index, sent in enumerate(receiver.commands) if " prune " in sent)
    release = next(index for index, sent in enumerate(receiver.commands) if " release " in sent)
    removal = next(
        index
        for index, sent in enumerate(receiver.commands)
        if sent.startswith("rm -f ") and "enigma2-mqtt-installer-" in sent
    )
    assert release < prune < removal
    # The snapshot this install just took is named, so that pruning cannot rank it by
    # a receiver clock that was wrong when it was written and delete it.
    snapshot = next(
        sent.rsplit(" ", 1)[-1] for sent in receiver.commands if " snapshot " in sent
    )
    assert receiver.commands[prune].endswith(
        f" prune /home/root/mqttbridge-backups --keep-name {snapshot.rsplit('/', 1)[-1]}"
    )


def _named_install(credentials: SshCredentials, name: str | None) -> InstallRequest:
    """One guided install of a box, with whatever the name field was left holding."""
    return InstallRequest(
        credentials,
        Provisioning(
            broker_host="192.0.2.10",
            broker_port=1883,
            broker_username="vuuno4kse_005301",
            broker_password="not-a-real-broker-password",
            node_id="vuuno4kse_005301",
            friendly_name=name,
        ),
    )


def _provisioned(receiver: FakeReceiver) -> dict[str, Any]:
    """The provisioning document the transaction wrote to the receiver."""
    return next(json.loads(value) for value in receiver.inputs if value.startswith(b"{"))


def test_a_provisioning_document_treats_a_name_of_spaces_as_no_name() -> None:
    """Otherwise a receiver ends up called `  ` and nothing says why.

    The key has to be absent rather than empty: the plugin applies every key the
    document holds, so an empty one is a rename to nothing, which the plugin then
    fills with the box type on its next start.
    """
    blank = Provisioning(
        broker_host="192.0.2.10",
        broker_port=1883,
        broker_username="vuuno4kse_005301",
        broker_password="not-a-real-broker-password",
        node_id="vuuno4kse_005301",
        friendly_name="   ",
    )

    assert blank.display_name() == ""
    assert "friendly_name" not in json.loads(blank.as_json())
    assert "friendly_name" in json.loads(replace(blank, friendly_name=" Kitchen ").as_json())


async def test_an_empty_name_leaves_the_receiver_s_own_name_alone_and_reports_it(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
) -> None:
    """A name the receiver already holds is not this transaction's to rewrite.

    The document says nothing about the name, so the plugin keeps it - and the caller
    still has to be told what that name is, because it is what the box is called when
    the install is over and the only thing the entry can sensibly be titled with.
    """
    receiver = FakeReceiver(
        node_id="vuuno4kse_005301", base_topic="enigma2", friendly_name="Living room receiver"
    )

    result = await _async_committed_install(
        hass, _named_install(credentials, None), tmp_path, receiver
    )

    assert "friendly_name" not in _provisioned(receiver)
    assert result.friendly_name == "Living room receiver"


async def test_a_name_of_nothing_but_spaces_is_the_same_as_an_empty_one(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
) -> None:
    """Spaces must not reach the receiver, and must not win over its own name either."""
    receiver = FakeReceiver(
        node_id="vuuno4kse_005301", base_topic="enigma2", friendly_name="Living room receiver"
    )

    result = await _async_committed_install(
        hass, _named_install(credentials, "   "), tmp_path, receiver
    )

    assert "friendly_name" not in _provisioned(receiver)
    assert result.friendly_name == "Living room receiver"


async def test_a_typed_name_outranks_the_one_the_receiver_holds(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
) -> None:
    """Renaming a box is what the field is for."""
    receiver = FakeReceiver(
        node_id="vuuno4kse_005301", base_topic="enigma2", friendly_name="Living room receiver"
    )

    result = await _async_committed_install(
        hass, _named_install(credentials, "Kitchen receiver"), tmp_path, receiver
    )

    assert _provisioned(receiver)["friendly_name"] == "Kitchen receiver"
    assert result.friendly_name == "Kitchen receiver"


async def test_a_box_with_no_name_is_provisioned_without_one(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
) -> None:
    """A first install has no name from either side, and must not write a blank.

    An empty value is a value: it would blank the setting, and the plugin fills a blank
    name with the box type on its next start anyway. Omitting the key leaves that to
    the plugin, which is where the rule lives.
    """
    receiver = FakeReceiver()

    result = await _async_committed_install(
        hass, _named_install(credentials, None), tmp_path, receiver
    )

    assert "friendly_name" not in _provisioned(receiver)
    assert result.friendly_name == ""


async def test_an_image_that_runs_a_wrapper_beside_enigma_can_be_installed_on(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    """Two Enigma processes is a lifecycle some images have, not a fault.

    The install's restart proof read `pidof enigma2` and refused anything but exactly
    one pid - before the restart as well as after it - so on an image that runs a
    wrapper beside the interface it starts, the guided install stopped with
    `restart_failed` at the step in front of the only disruptive command, on a receiver
    with nothing wrong with it. The wrapper survives the restart and the interface does
    not, so the proof is the child's new pid.
    """
    receiver = FakeReceiver(enigma_pid=100, enigma_wrapper_pid=42)

    result = await _async_committed_install(hass, install_request, tmp_path, receiver)

    assert result.restarted is True
    assert receiver.installed is True
    assert receiver.enigma_pid == 101


async def test_an_interface_that_died_behind_its_wrapper_is_not_a_restart(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    """The proof before the commit is a new process, not a different set of them.

    On a wrapper image the wrapper answers `pidof enigma2` whether or not the interface
    it started is there, so „the pids changed" is satisfied by the interface simply
    dying: 42 and 100 before, 42 alone after. Only „a pid that was not running before"
    tells those apart, and this is the last guard in front of the commit - after it the
    lock is released and the install is declared good.
    """
    receiver = FakeReceiver(enigma_pid=100, enigma_wrapper_pid=42, enigma_dies_at_restart=True)

    with pytest.raises(InstallerError) as raised:
        await _async_committed_install(hass, install_request, tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RESTART_FAILED
    # And the receiver is put back rather than left on a plugin it never started.
    assert receiver.rolled_back is True
    assert receiver.files["plugin"] == "old"


async def test_a_prune_that_fails_does_not_fail_a_committed_install(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The plugin is installed and verified by then; tidying cannot undo that."""
    receiver = FakeReceiver(fail_on=" prune ")

    result = await _async_committed_install(hass, install_request, tmp_path, receiver)

    assert result.version == "0.1.0"
    assert receiver.installed is True
    assert "superseded installer snapshots" in caplog.text


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

    The receiver's interface does not come back, so OpenWebif - which is an Enigma
    plugin - stops answering and the announcement never arrives. The rollback re-measured
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
    # Enigma was stopped before the settings were restored and started again afterwards
    # - by one script on the receiver, which is where that order now lives.
    assert receiver.r2_steps[:4] == ["begun 4242", "stopping 0", "stopped", "restored 0"]
    assert receiver.restarts == ["clean", "stopped"]
    assert not any(command.startswith("init ") for command in receiver.commands)
    assert receiver.helper_removed is True


def _slow_to_come_back(digest: str, misses: int) -> Any:
    """Return a `run` whose Enigma takes `misses` polls to reappear after `init 3`.

    This is the receiver measured in the second on-site drill: `init 3` is answered at
    once and `pidof enigma2` says nothing for another eleven to fourteen seconds. A
    negative `misses` never brings it back at all.
    """
    original = FakeSession.run
    state = {"restarted": False, "polls": 0}

    async def run(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, digest + "\n")
        if command == "pidof enigma2" and state["restarted"]:
            state["polls"] += 1
            if misses < 0 or state["polls"] <= misses:
                self.receiver.commands.append(command)
                # What `pidof` does when nothing matches: exit 1, and say nothing.
                return CommandResult(1, "", "")
        result = await original(self, command, **kwargs)
        if " r2-start " in command:
            state["restarted"] = True
        return result

    return run


async def test_a_rollback_waits_for_the_interface_it_restarted(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`init 3` is answered long before the interface is up.

    The rollback asked `pidof enigma2` once, immediately afterwards, and a receiver that
    was coming back perfectly normally answered nothing for another eleven seconds. That
    verdict - "the restart failed" - replaced the real reason the install had failed, and
    it came before the transaction lock was released, so the next install was refused as
    busy on a box that was in order. Measured on an OpenViX 6.6 receiver, 2026-09-22.
    """
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    bundle = BundledPlugin(artifact, "0.1.0", digest, "1" * 40)
    receiver = FakeReceiver(webif_dies_at_restart=True)

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
        patch("custom_components.enigma2_mqtt.installer.ROLLBACK_RESTART_POLL_SECONDS", 0),
        patch.object(FakeSession, "run", _slow_to_come_back(digest, 3)),
    ):
        with pytest.raises(InstallerError) as raised:
            await async_install(hass, install_request, _connector=receiver.connect)

    # The caller is told what actually went wrong, not that the recovery went wrong.
    assert raised.value.code is InstallerErrorCode.ANNOUNCEMENT_TIMEOUT
    assert receiver.rolled_back is True
    assert any(" release " in command for command in receiver.commands)
    assert receiver.helper_removed is True
    assert "Installer transaction lock remains" not in caplog.text


async def test_a_rollback_whose_interface_never_returns_says_so_and_still_unlocks(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    """A receiver whose files are back is not a receiver to refuse the next install on.

    The files were restored; only the interface did not come back, and the one thing
    that fixes that is a person restarting the box. Holding the transaction lock as well
    would make the next attempt - after that restart - refuse itself as busy.
    """
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    bundle = BundledPlugin(artifact, "0.1.0", digest, "1" * 40)
    receiver = FakeReceiver(webif_dies_at_restart=True)

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
        patch("custom_components.enigma2_mqtt.installer.ROLLBACK_RESTART_TIMEOUT", 0.05),
        patch("custom_components.enigma2_mqtt.installer.ROLLBACK_RESTART_POLL_SECONDS", 0.01),
        patch.object(FakeSession, "run", _slow_to_come_back(digest, -1)),
    ):
        with pytest.raises(InstallerError) as raised:
            await async_install(hass, install_request, _connector=receiver.connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_RESTART_FAILED
    # Distinct from `rollback_failed`, which says the receiver itself needs looking at.
    assert receiver.rolled_back is True
    assert receiver.files == {
        "plugin": "old",
        "opkg": "old",
        "settings": "old",
        "provisioning": "old",
    }
    assert any(" release " in command for command in receiver.commands)


async def test_a_rollback_whose_script_never_started_stopped_nothing(
    credentials: SshCredentials,
) -> None:
    """The stop and the start are one script on the receiver, so a transport lost
    before it started leaves the interface running - never stopped with nobody to
    start it again, which is what two separate commands could do.
    """
    receiver = FakeReceiver()

    class RaisingStartSession(FakeSession):
        async def run(
            self, command: str, *, input: bytes | None = None, timeout: float = 30
        ) -> CommandResult:
            if " r2-start " in command:
                self.receiver.commands.append(command)
                raise OSError("simulated transport loss")
            return await super().run(command, input=input, timeout=timeout)

    async def connect(_credentials: SshCredentials) -> RaisingStartSession:
        return RaisingStartSession(receiver)

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
    assert receiver.enigma_running is True
    assert receiver.restarts == []
    assert any(" release " in command for command in receiver.commands)


async def _rollback(credentials: SshCredentials, connect: Any) -> None:
    """Run one rollback of a transaction that got as far as restarting the receiver."""
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


async def test_a_receiver_that_always_reports_two_pids_is_not_a_failed_restart(
    credentials: SshCredentials,
) -> None:
    """A wrapper beside the interface is a lifecycle, not an ambiguity.

    The proof used to be "exactly one pid, and a different one from before". On an image
    whose `pidof enigma2` always reports a wrapper and its child, that is never true, so
    a rollback on a perfectly healthy receiver spent the whole two-minute timeout being
    refused and then reported a restart that had in fact happened. The wrapper survives
    the GUI restart and the child does not, so the child's new pid is the proof.
    """
    receiver = FakeReceiver(enigma_pid=100, enigma_wrapper_pid=42)

    await _rollback(credentials, receiver.connect)

    assert receiver.rolled_back is True
    assert receiver.enigma_pid == 101
    assert any(" release " in command for command in receiver.commands)


async def test_a_rollback_whose_init_3_is_refused_is_a_restart_failure(
    credentials: SshCredentials,
) -> None:
    """The command not being accepted is the same outcome as it not working."""
    receiver = FakeReceiver(r2_init3_status=1)

    with pytest.raises(InstallerError) as raised:
        await _rollback(credentials, receiver.connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_RESTART_FAILED
    assert receiver.rolled_back is True
    assert any(" release " in command for command in receiver.commands)


async def test_a_rollback_that_cannot_unlock_says_so_rather_than_the_original_reason(
    credentials: SshCredentials,
) -> None:
    """A receiver that is back but still locked is the incident this came from.

    Reporting the install's original failure and nothing else is true and useless: the
    box has been restored, the person fixes what was wrong and presses install again,
    and that attempt is refused as busy by a lock nobody holds - for the half hour
    before it is judged stale. The abort names the lock and where it is instead.
    """
    receiver = FakeReceiver(fail_on=" release ")

    with pytest.raises(InstallerError) as raised:
        await _rollback(credentials, receiver.connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_LOCK_FAILED
    assert receiver.rolled_back is True
    assert receiver.files == {
        "plugin": "old",
        "opkg": "old",
        "settings": "old",
        "provisioning": "old",
    }


async def test_a_lock_released_before_a_late_failure_is_not_reported_as_held(
    credentials: SshCredentials,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Closing the session is not the lock, and the two used to share one message.

    The release succeeded and the SSH session then failed to close, which sent the
    person to delete a lock directory that was no longer there.
    """
    # The rollback opens one session for the restore and a second for the release.
    receiver = FakeReceiver(close_raises_on={2})

    await _rollback(credentials, receiver.connect)

    assert any(" release " in command for command in receiver.commands)
    assert "could not release the transaction lock" not in caplog.text
    assert "the transaction lock was released" in caplog.text


async def test_a_rollback_cancelled_at_the_restart_stays_cancelled(
    credentials: SshCredentials,
) -> None:
    """A cancellation is not a verdict about the receiver.

    Converting it into `rollback_failed` told Home Assistant that a shutdown had broken
    a receiver, and left the task believing it had not been cancelled. The release is
    still attempted on the way out - it is one short command and the lock outlives the
    process that holds it.
    """
    receiver = FakeReceiver()
    original = FakeSession.run
    state = {"restarted": False}

    async def cancelling(self: FakeSession, command: str, **kwargs: Any):
        if command == "pidof enigma2" and state["restarted"]:
            self.receiver.commands.append(command)
            raise asyncio.CancelledError
        result = await original(self, command, **kwargs)
        if " r2-start " in command:
            state["restarted"] = True
        return result

    with patch.object(FakeSession, "run", cancelling):
        with pytest.raises(asyncio.CancelledError):
            await _rollback(credentials, receiver.connect)

    assert receiver.rolled_back is True
    assert any(" release " in command for command in receiver.commands)


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


async def _install_with_the_bundle(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path, receiver: FakeReceiver
) -> None:
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    bundle = BundledPlugin(artifact, "0.1.0", digest, "1" * 40)
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
        await async_install(hass, install_request, _connector=receiver.connect)


async def test_a_snapshot_refused_for_a_busy_opkg_says_so(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    """„Try again in a minute" is the whole remedy, and a generic failure hid it.

    The helper's refusal went out on stderr, which the installer discards, so a box
    whose owner was installing something from the receiver's menu reported the same
    „could not be installed" as a broken one.
    """
    receiver = FakeReceiver(busy_on=" snapshot ")

    with pytest.raises(InstallerError) as raised:
        await _install_with_the_bundle(hass, install_request, tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.OPKG_BUSY
    assert receiver.installed is False
    assert receiver.rolled_back is False
    # Nothing was taken, so there is nothing to restore - only the transaction lock to
    # give back.
    assert any(" release " in command for command in receiver.commands)


async def test_a_restore_refused_for_a_busy_opkg_says_so(
    credentials: SshCredentials,
) -> None:
    """The receiver is still on the new plugin, which is not what a half-done restore is."""
    receiver = FakeReceiver(r2_restore_status=installer_helper.EXIT_OPKG_BUSY)

    with pytest.raises(InstallerError) as raised:
        await _rollback(credentials, receiver.connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_OPKG_BUSY
    assert receiver.rolled_back is False
    # The interface is started again and the transaction lock released regardless.
    assert receiver.restarts == ["stopped"]
    assert any(" release " in command for command in receiver.commands)


async def test_only_the_helpers_busy_status_is_read_as_busy(
    credentials: SshCredentials,
) -> None:
    """Any other failure of the restore is still the failure it always was."""
    receiver = FakeReceiver(r2_restore_status=1)

    with pytest.raises(InstallerError) as raised:
        await _rollback(credentials, receiver.connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_FAILED


def test_the_helpers_waits_end_inside_the_commands_that_run_them() -> None:
    """A helper still waiting when the installer gives up takes the lock from under nobody.

    The margin is for the work each step does after it has the lock, which on a
    receiver's flash is seconds, not the wait.
    """
    margin = 5
    assert (
        installer_helper.OPKG_LOCK_WAIT_SNAPSHOT_SECONDS + margin <= installer.SNAPSHOT_TIMEOUT
    )
    assert installer_helper.OPKG_LOCK_WAIT_RESTORE_SECONDS + margin <= installer.RESTORE_TIMEOUT
    # The second look at a granted lock waits out opkg's unlock-close-delete, which is
    # three system calls; it must be a real pause, and far shorter than a poll.
    assert 0 < installer_helper.OPKG_LOCK_SETTLE_SECONDS < installer_helper.OPKG_LOCK_POLL_SECONDS
    # And a rollback, the worse thing to give up on, waits longer than an install.
    assert (
        installer_helper.OPKG_LOCK_WAIT_RESTORE_SECONDS
        > installer_helper.OPKG_LOCK_WAIT_SNAPSHOT_SECONDS
    )


async def test_a_restore_that_lost_opkgs_lock_says_it_was_put_back(
    credentials: SshCredentials,
) -> None:
    """The files are back; only what opkg recorded meanwhile is in doubt.

    „Could not be put back as it was" would send somebody to redo a restore that
    happened, so this is its own outcome rather than the restore failing.
    """
    receiver = FakeReceiver(r2_restore_status=installer_helper.EXIT_OPKG_LOCK_LOST)

    with pytest.raises(InstallerError) as raised:
        await _rollback(credentials, receiver.connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_OPKG_OVERLAP
    assert receiver.restarts == ["stopped"]
    assert any(" release " in command for command in receiver.commands)


async def test_the_lock_taking_steps_run_under_the_timeouts_their_waits_fit(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
) -> None:
    """The bounds are only safe if the commands are actually given them.

    The helper waits up to 20 s for opkg in a snapshot and 40 s in a restore; a
    command cut off before that leaves a helper still waiting, which then takes the
    lock from under nobody.
    """
    receiver = FakeReceiver(fail_on="cat > /etc/enigma2/mqttbridge.json.ha-")

    with pytest.raises(InstallerError):
        await _install_with_the_bundle(hass, install_request, tmp_path, receiver)

    snapshot_timeouts = [t for command, t in receiver.timeouts if " snapshot " in command]
    restore_timeouts = [t for command, t in receiver.timeouts if " restore " in command]
    assert snapshot_timeouts == [installer.SNAPSHOT_TIMEOUT]
    assert restore_timeouts == [installer.RESTORE_TIMEOUT]

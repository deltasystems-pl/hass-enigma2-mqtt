"""The SSH installer's forced mode - the recovery path - against a fake receiver.

A forced reinstall is for a receiver whose plugin does not answer, is switched off, or is
too old to update itself, so nothing in it may wait for the plugin: it decides from what
SSH says about the interface (runlevel and `pidof` over three samples), installs only the
bundled package, and proves the start by a new enigma2 holding the plugin's log open - and
by the announcement only when the plugin is switched on.

The rows of the GUI-state table (ADR-0008, section 8) are one test each. What only a real
receiver can show - that `init 4; init 3` really resets init's respawn throttle, that the
plugin opens its log before it reads `enabled` on every image - is joint acceptance.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
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
    SshCredentials,
    async_install,
)
from custom_components.enigma2_mqtt.release_package import PackageSource

from .test_installer import SAVED_EARLIER, WATCHED, FakeReceiver, FakeSession, credentials

__all__ = ["credentials"]

NODE = "vuuno4kse_005301"
RECORDED = "1:0:19:1B1C:3EE:1:C00000:0:0:0:"


@dataclass
class ForceReceiver(FakeReceiver):
    """The fake receiver, plus what the forced mode reads that an update never does."""

    runlevel: str = "N 3"
    # `pidof enigma2` over the three GUI-state samples, in order; after them the
    # receiver's own state answers.
    samples: list[bool] = field(default_factory=list)
    plugin_dir: bool = True
    # What the helper finds of this project on the receiver's disk.
    leftovers: dict[str, Any] = field(
        default_factory=lambda: {
            "ours": False,
            "lock": False,
            "marker": False,
            "r2": [],
            "transactions": [],
            "service": None,
        }
    )
    # Whether a new interface process opens the plugin's log.
    opens_log: bool = True
    claim_busy: bool = False
    lock: dict[str, Any] = field(default_factory=lambda: {"held": False})
    respawns: list[str] = field(default_factory=list)
    # A recording that begins when a command containing this is sent - the install
    # itself, say - so that only a guard measured after it can see it.
    recording_from: str | None = None
    # The interface, down on the three samples, runs from this command on: a box that
    # was slower to show a pid than the samples waited, or a person's `init 3`. Whether
    # its OpenWebif answers is `webif_ok`, which nothing reads while it is down.
    interface_from: str | None = None
    # What the settings block of the snapshot a recovery puts back says, as identity
    # fields; applied when a restore with `--settings` runs.
    restored_settings: dict[str, Any] | None = None


def _ours(**overrides: Any) -> dict[str, Any]:
    """What the helper reports for runlevel 4 left by our own interrupted rollback: the
    lock names its transaction, and its script was cut off before it wrote `restored`."""
    values: dict[str, Any] = {
        "ours": True,
        "lock": True,
        "marker": False,
        "r2": ["0123456789ab"],
        "unrestored": ["0123456789ab"],
        "rolling_back": [],
        "transactions": [],
        "service": RECORDED,
    }
    values.update(overrides)
    return values


def _identity(**overrides: Any) -> dict[str, Any]:
    values = {"node_id": NODE, "base_topic": None, "enabled": True, "ha_mode": "integration"}
    values.update(overrides)
    return values


def _receiver(**overrides: Any) -> ForceReceiver:
    identity = _identity(
        **{key: overrides.pop(key) for key in ("enabled", "ha_mode") if key in overrides}
    )
    return ForceReceiver(**identity, **overrides)


def _request(credentials: SshCredentials, **overrides: Any) -> InstallRequest:
    values: dict[str, Any] = {
        "provisioning": None,
        "expect_running": False,
        "node_id": NODE,
        "force": True,
    }
    values.update(overrides)
    return InstallRequest(credentials, **values)


def _watch(*, announces: bool, created: list[bool] | None = None):
    async def watch(*args: Any, **kwargs: Any):
        del args, kwargs
        if created is not None:
            created.append(True)
        loop = asyncio.get_running_loop()
        result = type("Watch", (), {})()
        result.established = loop.create_future()
        result.established.set_result(None)
        result.completed = loop.create_future()
        if announces:
            result.completed.set_result(None)
        result.cancel = lambda: result.completed.cancel()
        result.arm = lambda: None
        return result

    return watch


async def _force_run(self: FakeSession, command: str, **kwargs: Any) -> CommandResult | None:
    """Answer the commands only the forced mode sends; None hands the rest on."""
    del kwargs
    receiver = self.receiver
    assert isinstance(receiver, ForceReceiver)
    if receiver.recording_from and receiver.recording_from in command:
        receiver.recording = True
    if receiver.interface_from and receiver.interface_from in command:
        receiver.enigma_running = True
    if (
        receiver.restored_settings is not None
        and " restore " in command
        and "--settings" in command.split()
    ):
        for key, value in receiver.restored_settings.items():
            setattr(receiver, key, value)
    if command == "runlevel":
        receiver.commands.append(command)
        return CommandResult(0, receiver.runlevel + "\n")
    if command == "pidof enigma2" and receiver.samples:
        receiver.commands.append(command)
        if receiver.samples.pop(0):
            return CommandResult(0, f"{receiver.enigma_pid}\n")
        return CommandResult(1, "", "")
    if command.endswith(" leftovers"):
        receiver.commands.append(command)
        return CommandResult(0, json.dumps(receiver.leftovers) + "\n")
    if command == f"test -d {installer.PLUGIN_DIR}":
        receiver.commands.append(command)
        return CommandResult(0 if receiver.plugin_dir else 1)
    if " claim " in command and receiver.claim_busy:
        receiver.commands.append(command)
        return CommandResult(1, "", "FileExistsError")
    if " lock-info " in command:
        receiver.commands.append(command)
        return CommandResult(0, json.dumps(receiver.lock) + "\n")
    if " logfd " in command:
        receiver.commands.append(command)
        words = command.split()
        pids = [int(words[i + 1]) for i, word in enumerate(words) if word == "--pid"]
        holding = [
            pid
            for pid in pids
            if receiver.opens_log and receiver.enigma_running and pid == receiver.enigma_pid
        ]
        return CommandResult(0, json.dumps({"holding": holding}) + "\n")
    if " respawn" in command:
        receiver.commands.append(command)
        stop = "--stop" in command.split()
        receiver.respawns.append("stop-start" if stop else "start")
        receiver.enigma_running = False
        receiver.start_interface("stopped")
        return CommandResult(0, '{"pid": 4343}\n')
    return None


async def _install(
    hass: HomeAssistant,
    request: InstallRequest,
    tmp_path: Path,
    receiver: FakeReceiver,
    *,
    version: str = "0.2.0",
    announces: bool = True,
    created: list[bool] | None = None,
) -> Any:
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = hashlib.sha256(b"ipk bytes").hexdigest()
    bundle = BundledPlugin(artifact, version, digest, "1" * 40)
    original = FakeSession.run

    async def run(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, digest + "\n")
        if isinstance(self.receiver, ForceReceiver):
            answer = await _force_run(self, command, **kwargs)
            if answer is not None:
                return answer
        return await original(self, command, **kwargs)

    with (
        patch("custom_components.enigma2_mqtt.installer.load_bundled_plugin", return_value=bundle),
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch(
            "custom_components.enigma2_mqtt.installer._async_watch_restart",
            _watch(announces=announces, created=created),
        ),
        patch("custom_components.enigma2_mqtt.installer.ANNOUNCEMENT_TIMEOUT", 0.05),
        patch.object(FakeSession, "run", run),
    ):
        return await async_install(hass, request, _connector=receiver.connect)


def _opkg(receiver: FakeReceiver) -> list[str]:
    return [command for command in receiver.commands if command.startswith("opkg install")]


def _webif(receiver: FakeReceiver) -> list[str]:
    return [
        command
        for command in receiver.commands
        if "/api/statusinfo" in command or "/api/timerlist" in command
    ]


def _init(receiver: FakeReceiver) -> list[str]:
    return [command for command in receiver.commands if "init 4" in command or "init 3" in command]


# ---------------------------------------------------------------- the GUI-state table


async def test_a_healthy_interface_gets_the_normal_guards_and_a_clean_restart(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Row 1: runlevel 3, enigma2 running, OpenWebif answers - R1, never `init 4`."""
    receiver = _receiver()

    result = await _install(hass, _request(credentials), tmp_path, receiver)

    assert result.restarted is True
    assert receiver.restarts == ["clean"]
    assert receiver.respawns == []
    assert _init(receiver) == []
    assert _webif(receiver)
    assert _opkg(receiver) == [_opkg(receiver)[0]]
    assert "--force-reinstall" in _opkg(receiver)[0]
    assert "--force-downgrade" not in _opkg(receiver)[0]
    # The GUI's state is decided before any guard that needs OpenWebif.
    first_webif = receiver.commands.index(_webif(receiver)[0])
    assert receiver.commands.index("runlevel") < first_webif
    assert receiver.commands.count("pidof enigma2") >= 3


async def test_a_hung_interface_is_refused_before_anything_changes(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Row 2: enigma2 runs and OpenWebif is silent - whether it records is unknown."""
    receiver = _receiver(webif_ok=False)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.GUI_NOT_ANSWERING
    assert not any(" claim " in command for command in receiver.commands)
    assert not any(" snapshot " in command for command in receiver.commands)
    assert _opkg(receiver) == []
    assert receiver.restarts == []


async def test_an_interface_absent_on_three_samples_is_installed_and_respawned(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Row 3: a respawn loop. Nothing can record without enigma2, so the OpenWebif guards
    are skipped and logged; the start is `init 4; init 3`, which resets init's throttle."""
    receiver = _receiver(enigma_running=False, webif_ok=False, samples=[False, False, False])

    with caplog.at_level(logging.WARNING):
        result = await _install(hass, _request(credentials), tmp_path, receiver)

    assert result.restarted is True
    assert _webif(receiver) == []
    assert receiver.respawns == ["stop-start"]
    assert receiver.restarts == ["stopped"]
    assert "--force-reinstall" in _opkg(receiver)[0]
    assert "recording and timer guards" in caplog.text
    assert "overdue" in caplog.text


async def test_a_respawn_gap_is_a_running_interface(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Absent on one sample and present on the next is enigma2 respawning, not a loop."""
    receiver = _receiver(samples=[False, True])

    await _install(hass, _request(credentials), tmp_path, receiver)

    assert _webif(receiver)
    assert receiver.respawns == []
    assert receiver.restarts == ["clean"]


async def test_our_own_stopped_interface_is_recovered_and_started_on_its_channel(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Row 4 (S3-g): runlevel 4 and an interrupted rollback of ours. The lock is reclaimed by
    the released rule, the abandoned snapshot put back with its settings - enigma2 is
    stopped, so they can be - then `init 3` and R3 from the record that rollback left."""
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        webif_ok=False,
        samples=[False, False, False],
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        saved_service=SAVED_EARLIER,
        leftovers=_ours(),
    )

    with caplog.at_level(logging.WARNING):
        result = await _install(hass, _request(credentials), tmp_path, receiver)

    assert result.restarted is True
    assert _webif(receiver) == []
    recovery = [command for command in receiver.commands if "ha-installer-0123456789ab" in command]
    assert any(" restore " in command and "--settings" in command for command in recovery)
    assert not any(" withdraw " in command for command in recovery)
    assert receiver.respawns == ["start"]
    # R3: the image started on the saved channel; the recorded one is put back, once.
    assert receiver.zaps == [RECORDED]
    assert "runlevel 4" in caplog.text


async def test_an_interface_stopped_on_purpose_is_left_stopped(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Row 5: runlevel 4 with nothing of ours - somebody stopped it, and a reinstall would
    start it."""
    receiver = _receiver(
        runlevel="3 4", enigma_running=False, webif_ok=False, samples=[False, False, False]
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.GUI_STOPPED
    assert not any(" claim " in command for command in receiver.commands)
    assert _opkg(receiver) == []
    assert receiver.respawns == []


async def test_an_unknown_runlevel_without_an_interface_is_refused(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """No runlevel to read and no enigma2: not a state this table decides, so nothing."""
    receiver = _receiver(
        runlevel="unknown", enigma_running=False, webif_ok=False, samples=[False, False, False]
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED
    assert _opkg(receiver) == []


async def test_an_unknown_runlevel_with_a_running_interface_is_runlevel_3(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """An image without sysvinit's `runlevel` still has a running interface to guard."""
    receiver = _receiver(runlevel="unknown")

    await _install(hass, _request(credentials), tmp_path, receiver)

    assert _webif(receiver)
    assert receiver.restarts == ["clean"]


# ------------------------------------------------------------------ what gets installed


async def test_a_forced_reinstall_installs_only_the_bundle(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The bytes CI reproduced, never a download."""
    receiver = _receiver()
    package = PackageSource("0.3.0", b"x", hashlib.sha256(b"x").hexdigest(), None, (), "github")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials, package=package), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED
    assert receiver.commands == []


async def test_a_withdrawn_bundle_is_refused_in_the_forced_mode_too(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """v5.5: the next integration release carries a fixed bundle; this one is not forced on."""
    receiver = _receiver()

    with (
        patch(
            "custom_components.enigma2_mqtt.installer.bundle_refusal",
            return_value=("withdrawn", {"version": "0.2.0", "reason": "broken"}),
        ),
        pytest.raises(InstallerError) as raised,
    ):
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.BUNDLE_WITHDRAWN
    assert receiver.commands == []


async def test_a_newer_plugin_is_reinstalled_over_with_force_downgrade(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The recovery path: the bundle is the version this integration knows."""
    receiver = _receiver(installed_version="0.9.0")

    await _install(hass, _request(credentials), tmp_path, receiver)

    assert "--force-downgrade" in _opkg(receiver)[0]
    # A forced reinstall is not the options flow's downgrade: it asks the plugin nothing.
    assert not any("reset" in command for command in receiver.commands)


async def test_records_naming_a_version_that_is_not_on_disk_do_not_stop_it(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """After `restore_failed`/`restore_incomplete` opkg may name the new version while the
    plugin directory holds the old code - or nothing at all."""
    receiver = _receiver(installed_version="0.9.0", plugin_dir=False)

    result = await _install(hass, _request(credentials), tmp_path, receiver)

    assert result.restarted is True
    assert "--force-downgrade" in _opkg(receiver)[0]


async def test_unreadable_records_are_forced_over_rather_than_refused(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver(installed_version="0.9.0~garbled")

    await _install(hass, _request(credentials), tmp_path, receiver)

    assert "--force-downgrade" in _opkg(receiver)[0]


async def test_force_downgrade_is_never_passed_outside_the_forced_mode(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """An update over a newer plugin is refused, as it always was."""
    receiver = _receiver(installed_version="0.9.0")

    with pytest.raises(InstallerError) as raised:
        await _install(
            hass,
            _request(credentials, force=False, expect_running=True, running_version="0.9.0"),
            tmp_path,
            receiver,
        )

    assert raised.value.code is InstallerErrorCode.NEWER_INSTALLED
    assert _opkg(receiver) == []


# --------------------------------------------------------------------------- the proof


async def test_a_switched_off_plugin_is_proved_by_its_log_and_says_so(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Tier 2: the new enigma2 holds the plugin's log open - the plugin configures its
    logging before it reads `enabled` - and no announcement is awaited."""
    receiver = _receiver(enabled=False)
    created: list[bool] = []

    result = await _install(
        hass, _request(credentials), tmp_path, receiver, announces=False, created=created
    )

    assert result.restarted is True
    assert result.plugin_enabled is False
    assert created == []
    assert any(" logfd " in command for command in receiver.commands)
    # Nothing the reinstall wrote changed a setting.
    assert not any(
        " mv " in command and "mqttbridge.json" in command for command in receiver.commands
    )


async def test_a_switched_on_plugin_must_also_announce_itself(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Its log is not enough when it is switched on: silence is a failed start, and the
    snapshot goes back."""
    receiver = _receiver()

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver, announces=False)

    assert raised.value.code is InstallerErrorCode.ANNOUNCEMENT_TIMEOUT
    assert receiver.rolled_back is True


async def test_a_new_interface_that_never_opens_the_log_is_rolled_back(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """A new pid alone is an interface; it is not the plugin."""
    receiver = _receiver(enabled=False, opens_log=False)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver, announces=False)

    assert raised.value.code is InstallerErrorCode.PLUGIN_NOT_STARTED
    assert receiver.rolled_back is True


async def test_a_switched_on_plugin_proved_both_ways_is_enabled(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver()
    created: list[bool] = []

    result = await _install(hass, _request(credentials), tmp_path, receiver, created=created)

    assert result.plugin_enabled is True
    assert created == [True]
    assert any(" logfd " in command for command in receiver.commands)


async def test_a_switched_off_plugin_is_still_refused_by_an_update(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Only the forced mode reinstalls a plugin that is switched off."""
    receiver = _receiver(enabled=False)

    with pytest.raises(InstallerError) as raised:
        await _install(
            hass,
            _request(credentials, force=False, expect_running=True, running_version="0.1.0"),
            tmp_path,
            receiver,
        )

    assert raised.value.code is InstallerErrorCode.IDENTITY_MISMATCH


async def test_another_receiver_is_still_refused_in_the_forced_mode(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver()
    receiver.node_id = "another_receiver"

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.IDENTITY_MISMATCH
    assert _opkg(receiver) == []


# ----------------------------------------------------------------------------- the lock


async def test_a_live_transaction_is_never_broken(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """A self-update whose heartbeat is fresh holds the lock: busy, whatever else."""
    receiver = _receiver(
        claim_busy=True,
        lock={"held": True, "origin": "mqtt", "silent": 20, "remaining": 1780},
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.BUSY
    assert _opkg(receiver) == []
    assert not any(" snapshot " in command for command in receiver.commands)


async def test_a_stalled_self_update_says_when_the_reinstall_can_run(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Three beats missed: its helper stopped. The lock is still not taken back before the
    released rule frees it; the refusal says when that is."""
    receiver = _receiver(
        claim_busy=True,
        lock={"held": True, "origin": "mqtt", "silent": 600, "remaining": 1200},
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.BUSY_STALLED
    assert raised.value.placeholders == {"minutes": "20"}
    assert _opkg(receiver) == []


async def test_an_installer_lock_has_no_heartbeat_to_judge_and_is_plain_busy(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver(
        claim_busy=True, lock={"held": True, "origin": None, "silent": 900, "remaining": 900}
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.BUSY


# ---------------------------------------------- opkg's records are not the running plugin


async def test_records_newer_than_the_running_plugin_are_named_not_believed(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """An update from the card after a failed self-update restore: opkg names 0.9.0, the
    plugin that runs is 0.1.0. That is not "a newer plugin is installed"; it is a
    mismatch, refused before anything changes, and the forced reinstall is the repair."""
    receiver = _receiver(installed_version="0.9.0")

    with pytest.raises(InstallerError) as raised:
        await _install(
            hass,
            _request(credentials, force=False, expect_running=True, running_version="0.1.0"),
            tmp_path,
            receiver,
        )

    assert raised.value.code is InstallerErrorCode.RECORDS_MISMATCH
    assert raised.value.placeholders == {"recorded": "0.9.0", "running": "0.1.0"}
    assert _opkg(receiver) == []
    assert not any(" snapshot " in command for command in receiver.commands)


async def test_without_a_running_version_the_records_decide_and_are_named_as_records(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver(installed_version="0.9.0")

    with pytest.raises(InstallerError) as raised:
        await _install(
            hass,
            _request(credentials, force=False, expect_running=True),
            tmp_path,
            receiver,
        )

    assert raised.value.code is InstallerErrorCode.NEWER_INSTALLED
    assert "opkg's records" in raised.value.detail


async def test_an_update_from_a_plugin_matching_its_records_is_unchanged(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver(installed_version="0.1.0")

    result = await _install(
        hass,
        _request(credentials, force=False, expect_running=True, running_version="0.1.0"),
        tmp_path,
        receiver,
    )

    assert result.restarted is True
    assert "--force-downgrade" not in _opkg(receiver)[0]


def test_every_new_code_is_a_stable_string() -> None:
    assert InstallerErrorCode.GUI_NOT_ANSWERING.value == "gui_not_answering"
    assert InstallerErrorCode.GUI_STOPPED.value == "gui_stopped"
    assert InstallerErrorCode.BUSY_STALLED.value == "busy_stalled"
    assert InstallerErrorCode.RECORDS_MISMATCH.value == "records_mismatch"
    assert InstallerErrorCode.PLUGIN_NOT_STARTED.value == "plugin_not_started"
    assert WATCHED


# ------------------------------------------ the last look before the interface is touched


async def test_an_interface_that_came_up_during_the_install_is_never_stopped_by_init(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Absent on the three samples - a start gap is 11-14 s on a receiver, longer than the
    ten seconds they cover - and running, and recording, by the time the install reaches
    the restart. `init 4` would cut the recording: the interface is looked at again, is a
    running one, and gets every guard of the first row; the recording refuses."""
    receiver = _receiver(
        enigma_running=False,
        samples=[False, False, False],
        recording=True,
        interface_from="opkg install",
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RECORDING
    assert receiver.respawns == []
    assert _init(receiver) == []
    assert receiver.restarts == []
    # The install is put back under the running interface, never by stopping it.
    assert receiver.files["plugin"] == "old"


async def test_a_box_that_was_still_starting_is_restarted_cleanly_with_every_guard(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A booting box reads as a respawn loop on the three samples. By the last look its
    interface runs: the clean restart, never `init 4`, and OpenWebif asked first."""
    receiver = _receiver(
        enigma_running=False, samples=[False, False, False], interface_from="opkg install"
    )

    with caplog.at_level(logging.WARNING):
        result = await _install(hass, _request(credentials), tmp_path, receiver)

    assert result.restarted is True
    assert receiver.respawns == []
    assert receiver.restarts == ["clean"]
    statusinfo = [
        index for index, command in enumerate(receiver.commands) if "/api/statusinfo" in command
    ]
    opkg = receiver.commands.index(_opkg(receiver)[0])
    assert statusinfo and statusinfo[0] > opkg
    assert "now runs" in caplog.text


async def test_an_interface_started_by_hand_in_runlevel_4_is_restarted_cleanly(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Runlevel 4 of ours, and somebody ran `init 3` while the install ran: harmless, and
    logged - the interface is then a running one, guarded and restarted cleanly."""
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        samples=[False, False, False],
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        leftovers=_ours(),
        interface_from="opkg install",
    )

    with caplog.at_level(logging.WARNING):
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert receiver.respawns == []
    assert receiver.restarts == ["clean"]
    assert "somebody started it" in caplog.text


async def test_an_interface_that_came_up_hung_is_refused_at_the_last_look(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Running by the last look, and OpenWebif silent: whether it records is unknown, as
    in the first row - refused, and nothing sent to init."""
    receiver = _receiver(
        enigma_running=False,
        webif_ok=False,
        samples=[False, False, False],
        interface_from="opkg install",
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.GUI_NOT_ANSWERING
    assert receiver.respawns == []
    assert _init(receiver) == []


async def test_a_recording_that_begins_during_the_install_stops_the_restart(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The guard measured again immediately before the only disruptive step is the one
    that sees a recording begun while opkg ran."""
    receiver = _receiver(recording_from="opkg install")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RECORDING
    assert _opkg(receiver)
    assert receiver.restarts == []
    assert receiver.files["plugin"] == "old"


# ------------------------------------------------- what the recovery in runlevel 4 writes


async def test_settings_go_back_only_for_the_reclaimed_transactions_own_cut_off_rollback(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The reclaimed lock names X, and the rollback cut off before its restore is Y's: X's
    snapshot is put back as files only. Its settings block is days old - writing it would
    revert every plugin setting changed since, `enabled` among them."""
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        webif_ok=False,
        samples=[False, False, False],
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        leftovers=_ours(r2=["ba9876543210"], unrestored=["ba9876543210"], service=None),
    )

    await _install(hass, _request(credentials), tmp_path, receiver)

    recovery = [command for command in receiver.commands if "ha-installer-0123456789ab" in command]
    assert any(" withdraw " in command for command in recovery)
    assert not any("--settings" in command for command in recovery)


async def test_a_rollback_that_recorded_its_restore_gets_no_second_settings_write(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        webif_ok=False,
        samples=[False, False, False],
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        leftovers=_ours(unrestored=[]),
    )

    await _install(hass, _request(credentials), tmp_path, receiver)

    recovery = [command for command in receiver.commands if "ha-installer-0123456789ab" in command]
    assert not any("--settings" in command for command in recovery)


async def test_settings_are_never_written_under_an_interface_that_runs_again(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Somebody started the interface between the samples and the recovery: a settings
    block written now would be overwritten from memory, so only the files go back."""
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        samples=[False, False, False],
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        leftovers=_ours(),
        interface_from=" claim ",
    )

    await _install(hass, _request(credentials), tmp_path, receiver)

    recovery = [command for command in receiver.commands if "ha-installer-0123456789ab" in command]
    assert not any("--settings" in command for command in recovery)
    assert receiver.restarts == ["clean"]


async def test_what_the_restored_settings_say_is_read_again(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The snapshot's settings switch the plugin off: the proof then waits for no
    announcement, and the result says the plugin is off - not what was read before."""
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        webif_ok=False,
        samples=[False, False, False],
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        leftovers=_ours(),
        restored_settings={"enabled": False},
    )
    created: list[bool] = []

    result = await _install(
        hass, _request(credentials), tmp_path, receiver, announces=False, created=created
    )

    assert result.plugin_enabled is False
    assert created == []
    identity_reads = [command for command in receiver.commands if command.endswith(" identity")]
    assert len(identity_reads) == 2


async def test_restored_settings_of_another_receiver_stop_it_and_bring_the_picture_back(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        webif_ok=False,
        samples=[False, False, False],
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        leftovers=_ours(),
        restored_settings={"node_id": "another_receiver"},
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.IDENTITY_MISMATCH
    assert _opkg(receiver) == []
    assert receiver.respawns == ["start"]


# ------------------------------------------------ a failure in runlevel 4 of ours


def _ours_stopped(**extra: Any) -> ForceReceiver:
    values: dict[str, Any] = {
        "runlevel": "3 4",
        "enigma_running": False,
        "webif_ok": False,
        "samples": [False, False, False],
        "reclaimed_id": "0123456789ab",
        "snapshots": {"ha-installer-0123456789ab"},
        "saved_service": SAVED_EARLIER,
        "leftovers": _ours(),
    }
    values.update(extra)
    return _receiver(**values)


async def test_a_failed_recovery_in_runlevel_4_still_brings_the_picture_back(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The interrupted transaction's snapshot cannot be put back. The picture comes first
    (TRANSACTION.md section 5.2): the interface is started on what is there, and the
    verdict says the receiver could not be put back - not "put back as it was"."""
    receiver = _ours_stopped(fail_on=" restore /home/root/mqttbridge-backups/ha-installer-")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_FAILED
    assert _opkg(receiver) == []
    assert receiver.respawns == ["start"]
    assert receiver.enigma_running is True
    # The lock was released before the start; the helper uploaded for it is gone again.
    assert receiver.helper_removed is True


async def test_a_failed_install_in_runlevel_4_is_rolled_back_and_started(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _ours_stopped(fail_on="opkg install")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.INSTALL_FAILED
    assert receiver.rolled_back is True
    assert receiver.respawns == ["start"]


async def test_a_failed_install_in_a_respawn_loop_sends_nothing_to_init(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """A respawn loop was not stopped by us, and init is already starting it: a failure
    before the restart puts the files back and leaves init to it."""
    receiver = _receiver(
        enigma_running=False,
        webif_ok=False,
        samples=[False, False, False],
        fail_on="opkg install",
    )

    with pytest.raises(InstallerError):
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert receiver.respawns == []
    assert _init(receiver) == []


async def test_runlevel_4_of_ours_with_a_live_installer_lock_says_when(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """No picture, and the lock of the interrupted installer has no heartbeat to judge: the
    refusal says when the released rule frees it, not a plain "busy" with no end."""
    receiver = _ours_stopped(
        claim_busy=True, lock={"held": True, "origin": None, "silent": 300, "remaining": 1501}
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.BUSY_STALLED
    assert raised.value.placeholders == {"minutes": "26"}
    assert receiver.respawns == []
    assert _opkg(receiver) == []


async def test_a_kept_self_update_directory_does_not_make_runlevel_4_ours(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """End to end with the helper's own answer: a receiver that once had a failed
    self-update, stopped on purpose, is refused - not started and reinstalled."""
    root = tmp_path / "root"
    kept = root / "home/root/mqttbridge-backups/update-0123456789ab"
    kept.mkdir(parents=True)
    (kept / "status.json").write_text(
        json.dumps({"id": "0123456789ab", "phase": "finished", "result": "failed"}),
        encoding="ascii",
    )
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        webif_ok=False,
        samples=[False, False, False],
        leftovers=installer_helper.leftovers(root),
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.GUI_STOPPED
    assert receiver.respawns == []
    assert _init(receiver) == []
    assert _opkg(receiver) == []


# ------------------------------------------------------------- gaps the review closed


async def test_an_interface_still_running_in_runlevel_4_is_refused(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Runlevel 4 with enigma2 still there: it is being stopped, by somebody else."""
    receiver = _receiver(runlevel="3 4")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED
    assert not any(" claim " in command for command in receiver.commands)
    assert _opkg(receiver) == []
    assert _init(receiver) == []


async def test_a_running_interface_in_standby_is_refused_before_anything_changes(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The forced mode keeps the standby guard while the interface runs: the restart would
    wake the receiver, and HDMI-CEC the television with it."""
    receiver = _receiver(standby=True)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.STANDBY
    assert not any(" claim " in command for command in receiver.commands)
    assert _opkg(receiver) == []


def test_the_runlevel_is_the_last_word_of_sysvinits_answer() -> None:
    assert installer._runlevel("N 3\n") == "3"
    assert installer._runlevel("3 4\n") == "4"
    assert installer._runlevel("unknown\n") is None
    assert installer._runlevel("") is None
    assert installer._runlevel("N 5") == "5"
    assert installer._runlevel("4 S") == "S"
    assert installer._runlevel("N 3 extra") is None


async def test_records_equal_to_the_bundle_get_no_force_downgrade(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver(installed_version="0.2.0")

    await _install(hass, _request(credentials), tmp_path, receiver)

    assert "--force-reinstall" in _opkg(receiver)[0]
    assert "--force-downgrade" not in _opkg(receiver)[0]


async def test_the_forced_mode_still_refuses_a_receiver_not_in_integration_mode(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _receiver(ha_mode="off")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.IDENTITY_MISMATCH
    assert _opkg(receiver) == []


@pytest.mark.parametrize(
    "lock",
    [
        # Silent for exactly three beats is not yet stalled.
        {"held": True, "origin": "mqtt", "silent": 180, "remaining": 1621},
        # Nothing left: the rule frees it now, and the claim was what said no.
        {"held": True, "origin": "mqtt", "silent": 2000, "remaining": 0},
        {"held": True, "origin": "mqtt", "silent": None, "remaining": None},
    ],
)
async def test_the_edges_of_a_stalled_lock_are_plain_busy(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path, lock: dict[str, Any]
) -> None:
    receiver = _receiver(claim_busy=True, lock=lock)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.BUSY


async def test_an_update_over_a_newer_running_plugin_is_refused_whatever_the_records_say(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The card knows 0.9.0 runs; opkg's records say 0.0.9; the target is 0.2.0. The
    records are wrong the other way round, and a newer plugin still runs."""
    receiver = _receiver(installed_version="0.0.9")

    with pytest.raises(InstallerError) as raised:
        await _install(
            hass,
            _request(credentials, force=False, expect_running=True, running_version="0.9.0"),
            tmp_path,
            receiver,
        )

    assert raised.value.code is InstallerErrorCode.NEWER_INSTALLED
    assert _opkg(receiver) == []


async def test_an_interface_started_by_hand_that_then_records_gets_nothing_from_init(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """Runlevel 4 of ours, started by a person during the install, and recording by the
    last look: refused and put back under the running interface - and, since it runs,
    no start of the interface afterwards either."""
    receiver = _receiver(
        runlevel="3 4",
        enigma_running=False,
        samples=[False, False, False],
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        leftovers=_ours(),
        interface_from="opkg install",
        recording_from="opkg install",
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RECORDING
    assert receiver.respawns == []
    assert _init(receiver) == []


# ------------------------------------------------------------- review round 2


@pytest.mark.parametrize(
    ("state", "code"),
    [("standby", InstallerErrorCode.STANDBY), ("streaming", InstallerErrorCode.STREAMING)],
)
async def test_a_box_that_came_up_in_standby_is_not_woken_by_the_last_look(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
    state: str,
    code: InstallerErrorCode,
) -> None:
    """A booting box read as a respawn loop, and by the last look its interface runs - in
    standby, as a box that was booting often does, or streaming. The clean restart would
    wake it (and the television with it over HDMI-CEC) or cut the stream: the switched row
    measures standby and streaming too, and refuses."""
    receiver = _receiver(
        enigma_running=False,
        samples=[False, False, False],
        interface_from="opkg install",
        **{state: True},
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is code
    assert receiver.restarts == []
    assert receiver.respawns == []
    assert _init(receiver) == []
    assert receiver.files["plugin"] == "old"


async def test_the_last_look_rereads_which_interface_was_there_before_the_restart(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """The interface comes up after the pids before the restart were read - an empty set -
    and before the last look. Kept, that set would make the interface that came up count
    as "new" at once, and the proof would pass on it - the plugin's log is open in it -
    while the new files never started. Here the image asks on the television instead of
    restarting: nothing new ever runs, so the install must not be reported as installed."""
    receiver = _receiver(
        # Absent on the three samples and on the read before the restart; running from
        # the last look on.
        samples=[False, False, False, False],
        question_on_restart=True,
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RESTART_WITHDRAWN
    assert receiver.files["plugin"] == "old"
    assert receiver.respawns == []
    assert _init(receiver) == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"free_bytes": 1000}, InstallerErrorCode.NO_SPACE),
        ({"python_version": "3.7.3"}, InstallerErrorCode.UNSUPPORTED_PYTHON),
        ({"ha_mode": "off"}, InstallerErrorCode.IDENTITY_MISMATCH),
    ],
)
async def test_a_refusal_before_the_lock_in_runlevel_4_of_ours_says_the_interface_stays_down(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, Any],
    code: InstallerErrorCode,
) -> None:
    """Without the lock nothing says the interrupted transaction is not still running, so
    nothing is started - and the refusal is marked, so that whoever shows it says the
    interface stays stopped and how to start it."""
    receiver = _ours_stopped(**overrides)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(InstallerError) as raised:
            await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is code
    assert raised.value.interface_stopped is True
    assert not any(" claim " in command for command in receiver.commands)
    assert receiver.respawns == []
    assert _init(receiver) == []
    assert _opkg(receiver) == []
    assert "interface stays stopped" in caplog.text


@pytest.mark.parametrize(
    "row",
    [
        {},
        # A respawn loop is init's to start, not a stop of ours.
        {"enigma_running": False, "webif_ok": False, "samples": [False, False, False]},
    ],
)
@pytest.mark.parametrize("busy", [False, True])
async def test_the_same_refusal_in_another_row_is_not_marked(
    hass: HomeAssistant,
    credentials: SshCredentials,
    tmp_path: Path,
    row: dict[str, Any],
    busy: bool,
) -> None:
    receiver = _receiver(
        **row,
        **(
            {"claim_busy": True, "lock": {"held": True, "origin": None, "remaining": 900}}
            if busy
            else {"free_bytes": 1000}
        ),
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is (InstallerErrorCode.BUSY if busy else InstallerErrorCode.NO_SPACE)
    assert raised.value.interface_stopped is False


async def test_a_live_self_update_in_runlevel_4_of_ours_is_plain_busy(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    """A self-update whose heartbeat is fresh holds the lock: it has not "stopped without
    finishing", and no minutes are promised - its end is its own. The interface still
    stays stopped, and the refusal says so."""
    receiver = _ours_stopped(
        claim_busy=True,
        lock={"held": True, "origin": "mqtt", "silent": 20, "remaining": 1780},
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.BUSY
    assert raised.value.placeholders == {}
    assert raised.value.interface_stopped is True
    assert receiver.respawns == []
    assert _init(receiver) == []


async def test_a_stalled_installer_lock_in_runlevel_4_of_ours_is_marked_too(
    hass: HomeAssistant, credentials: SshCredentials, tmp_path: Path
) -> None:
    receiver = _ours_stopped(
        claim_busy=True, lock={"held": True, "origin": None, "silent": 300, "remaining": 1501}
    )

    with pytest.raises(InstallerError) as raised:
        await _install(hass, _request(credentials), tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.BUSY_STALLED
    assert raised.value.interface_stopped is True

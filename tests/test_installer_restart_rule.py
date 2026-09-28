"""The restart rule in the SSH installer, against a fake receiver.

R1: an install restarts the interface by the image's own clean quit (OpenWebif power
state 3), judged by enigma2's pid, and withdraws when the image asks a question instead.
R2: a rollback that must put the settings back stops, restores, writes the recorded
channel and starts, as one script on the receiver. R3: after every restart the channel
and the standby state are compared with the record and put back at most once.

The fake receiver models what these depend on - see `FakeReceiver` in `test_installer.py`.
What only a real receiver can show (that the image saves on a clean quit, that it starts
on a `lastservice` written while it is stopped) is not claimed here: those are joint
acceptance on hardware.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.bundle import BundledPlugin
from custom_components.enigma2_mqtt.installer import (
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    SshCredentials,
    _async_rollback,
    async_install,
    async_preflight,
)
from custom_components.enigma2_mqtt.restart_rule import RestartRecord

from .test_installer import (
    SAVED_EARLIER,
    WATCHED,
    FakeReceiver,
    FakeSession,
    credentials,
    install_request,
)

__all__ = ["credentials", "install_request"]

ELSEWHERE = "1:0:19:1B1C:3EE:1:C00000:0:0:0:"


def _watch(*, announces: bool):
    """A restart watch with no broker behind it: it announces at once, or never."""

    async def watch(*args: Any, **kwargs: Any):
        del args, kwargs
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


async def _install(
    hass: HomeAssistant,
    request: InstallRequest,
    tmp_path: Path,
    receiver: FakeReceiver,
    *,
    announces: bool = True,
    run: Any = None,
) -> Any:
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = hashlib.sha256(b"ipk bytes").hexdigest()
    bundle = BundledPlugin(artifact, "0.2.0", digest, "1" * 40)
    original = FakeSession.run

    async def hash_aware(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, digest + "\n")
        if run is not None:
            answer = await run(self, command, **kwargs)
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
            _watch(announces=announces),
        ),
        patch("custom_components.enigma2_mqtt.installer.ANNOUNCEMENT_TIMEOUT", 0.01),
        patch.object(FakeSession, "run", hash_aware),
    ):
        return await async_install(hass, request, _connector=receiver.connect)


def _init_commands(receiver: FakeReceiver) -> list[str]:
    return [command for command in receiver.commands if "init 4" in command or "init 3" in command]


async def test_an_install_restarts_by_a_clean_quit_and_never_by_init_4(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """A stop by signal never reaches the image's save; OpenWebif's power state 3 does.

    `init 4` brought a receiver back on a channel saved hours before, because the
    channel being watched is saved only on a clean quit.
    """
    receiver = FakeReceiver()

    result = await _install(hass, install_request, tmp_path, receiver)

    assert result.restarted is True
    assert receiver.restarts == ["clean"]
    assert _init_commands(receiver) == []
    assert any(" powerstate --state 3" in command for command in receiver.commands)
    # The clean quit saved the channel, and the receiver came back on it.
    assert receiver.saved_service == WATCHED
    assert receiver.service == WATCHED
    assert receiver.zaps == []


async def test_a_restart_is_judged_by_the_pid_and_not_by_the_answer(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """The quit can reset the connection that asked for it; the new pid is the proof."""
    receiver = FakeReceiver()
    original = FakeSession.run

    async def reset_by_the_quit(self: FakeSession, command: str, **kwargs: Any):
        if " powerstate --state 3" in command:
            await original(self, command, **kwargs)
            raise OSError("connection reset by peer")
        return None

    result = await _install(hass, install_request, tmp_path, receiver, run=reset_by_the_quit)

    assert result.restarted is True
    assert receiver.restarts == ["clean"]


async def test_an_answered_restart_whose_pid_never_changes_is_not_a_restart(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """OpenWebif answering is not the interface restarting.

    The image asked a question - it does for timeshift and for a background job - and
    the old enigma2 still runs, same pid. Reading the answer as success would commit an
    install the receiver never started.
    """
    receiver = FakeReceiver(question_on_restart=True)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RESTART_WITHDRAWN
    assert receiver.enigma_pid == 100
    assert receiver.restarts == []


async def test_a_question_withdraws_the_install_and_leaves_the_settings_alone(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """Q3-A: the question stays on the television, the old files go back under it.

    Files and opkg metadata only - the provisioning document is a file the transaction
    wrote - and never the settings block: a running enigma2 owns it until it quits.
    Nothing is stopped, so there is no stop-and-restore and no rollback afterwards, and
    the lock is released.
    """
    receiver = FakeReceiver(question_on_restart=True)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RESTART_WITHDRAWN
    assert receiver.withdrawn is True
    assert receiver.files == {
        "plugin": "old",
        "opkg": "old",
        "settings": "old",
        "provisioning": "old",
    }
    assert "--settings" not in receiver.restore_flags
    assert not any(" r2-start " in command for command in receiver.commands)
    assert not any(" restore " in command for command in receiver.commands)
    assert _init_commands(receiver) == []
    assert any(" release " in command for command in receiver.commands)
    assert receiver.helper_removed is True


async def test_a_question_answered_while_the_files_go_back_is_a_restart(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """Somebody pressed "yes" meanwhile: the receiver restarted, so it is never "withdrawn".

    And it is never committed either, even when the new plugin announces itself: the
    answer may have come while the withdraw waited for opkg's lock, so the new plugin
    started and then its files were replaced by the old ones. A proof would pass on a
    plugin whose files are gone. The rollback that stops, restores and starts makes the
    running plugin and the files on disk agree again.
    """
    receiver = FakeReceiver(question_on_restart=True, question_answered_during_withdraw=True)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, announces=True)

    assert raised.value.code is InstallerErrorCode.RESTART_FAILED
    assert receiver.restarts == ["clean", "stopped"]
    assert any(" r2-start " in command for command in receiver.commands)
    assert not any(" prune " in command for command in receiver.commands)
    assert receiver.files["plugin"] == "old"


async def test_a_withdraw_that_cannot_put_the_files_back_still_stops_nothing(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """opkg busy under a question: say so, release, and leave the interface running."""
    receiver = FakeReceiver(question_on_restart=True, busy_on=" withdraw ")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver)

    # Its own sentence: the new files are still there, and the question on the television
    # may still be answered.
    assert raised.value.code is InstallerErrorCode.WITHDRAW_FAILED
    assert receiver.restarts == []
    assert not any(" r2-start " in command for command in receiver.commands)
    assert any(" release " in command for command in receiver.commands)


@pytest.mark.parametrize(
    ("receiver", "code"),
    [
        (FakeReceiver(standby=True), InstallerErrorCode.STANDBY),
        (FakeReceiver(streaming=True), InstallerErrorCode.STREAMING),
    ],
)
async def test_standby_and_streaming_refuse_the_install_before_anything_changes(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    receiver: FakeReceiver,
    code: InstallerErrorCode,
) -> None:
    """A restart wakes a receiver in standby - with HDMI-CEC the TV too - and cuts a stream."""
    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver)

    assert raised.value.code is code
    assert not any(" claim " in command for command in receiver.commands)
    assert receiver.restarts == []


async def test_the_credential_probe_does_not_refuse_a_sleeping_receiver(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    """The options screen's probe restarts nothing; standby is no reason to refuse it."""
    receiver = FakeReceiver(standby=True, streaming=True)

    measured = await async_preflight(hass, credentials, _connector=receiver.connect)

    assert measured.recording is False


async def test_an_openwebif_that_does_not_say_whether_it_sleeps_fails_closed(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    receiver = FakeReceiver()

    async def older_statusinfo(self: FakeSession, command: str, **kwargs: Any):
        if "/api/statusinfo" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, '{"isRecording": false}')
        return None

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, run=older_statusinfo)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED
    assert not any(" claim " in command for command in receiver.commands)


async def test_the_image_starting_elsewhere_is_zapped_back_once(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3 by effect: if the image does not start on the saved channel, zap back once."""
    receiver = FakeReceiver(start_channel=SAVED_EARLIER)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.installer")

    await _install(hass, install_request, tmp_path, receiver)

    assert receiver.zaps == [WATCHED]
    assert receiver.service == WATCHED
    assert "restart: clean, channel: restored" in caplog.text


async def test_a_zap_that_does_not_take_is_not_repeated(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    receiver = FakeReceiver(start_channel=SAVED_EARLIER, zap_takes_effect=False)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.installer")

    result = await _install(hass, install_request, tmp_path, receiver)

    assert result.restarted is True
    assert receiver.zaps == [WATCHED]
    assert "channel: lost" in caplog.text


async def test_a_channel_the_household_chose_after_the_start_is_never_overridden(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The zap back is only for the image's own choice at start, not for a person's."""
    receiver = FakeReceiver(start_channel=SAVED_EARLIER)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.installer")
    reads = {"after_start": 0}

    async def household_zaps(self: FakeSession, command: str, **kwargs: Any):
        if command.endswith(" record") and self.receiver.restarts:
            reads["after_start"] += 1
            if reads["after_start"] == 2:
                # Between the first look after the start and the zap, somebody picked
                # a channel with the remote.
                self.receiver.service = ELSEWHERE
        return None

    await _install(hass, install_request, tmp_path, receiver, run=household_zaps)

    assert receiver.zaps == []
    assert receiver.service == ELSEWHERE
    # Not "lost": the channel was kept until somebody chose another, and a log reader
    # looking for a restart that lost the channel must be able to tell the two apart.
    assert "channel: changed by the household" in caplog.text
    assert "channel: lost" not in caplog.text


async def test_a_rollback_after_a_restart_is_one_script_that_writes_the_channel(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """R2: record, stop, restore, write the recorded channel, start - never several commands.

    OpenWebif died with the new plugin, so nothing can be read now; the channel recorded
    before the install's restart is the one written.
    """
    receiver = FakeReceiver(webif_dies_at_restart=True)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, announces=False)

    assert raised.value.code is InstallerErrorCode.ANNOUNCEMENT_TIMEOUT
    r2 = [command for command in receiver.commands if " r2-start " in command]
    assert len(r2) == 1
    assert f"--service {WATCHED}" in r2[0]
    assert _init_commands(receiver) == []
    assert receiver.restarts == ["clean", "stopped"]
    assert receiver.saved_service == WATCHED
    assert receiver.service == WATCHED
    assert receiver.files["settings"] == "old"


async def test_a_connection_lost_while_the_receiver_restores_is_followed_again(
    credentials: SshCredentials,
) -> None:
    """The script runs on the receiver whatever this end does; following it reconnects."""
    receiver = FakeReceiver()
    dropped = {"left": 2, "connects": 0}

    class Flaky(FakeSession):
        async def run(
            self, command: str, *, input: bytes | None = None, timeout: float = 30
        ) -> CommandResult:
            if command.startswith("cat ") and command.endswith("/status") and dropped["left"]:
                dropped["left"] -= 1
                raise OSError("connection lost")
            return await super().run(command, input=input, timeout=timeout)

    async def connect(_credentials: SshCredentials) -> Flaky:
        dropped["connects"] += 1
        return Flaky(receiver)

    await _async_rollback(
        credentials,
        "/home/root/mqttbridge-backups/ha-installer-0123456789ab",
        "/tmp/plugin.ipk",
        "/tmp/enigma2-mqtt-installer-0123456789ab.py",
        "/tmp/manifest",
        "/etc/enigma2/mqttbridge.json.ha-0123456789ab",
        "/tmp/lock",
        True,
        True,
        True,
        connect,
    )

    assert dropped["left"] == 0
    # The first session, one per dropped read, and one for the release.
    assert dropped["connects"] == 4
    assert receiver.restarts == ["stopped"]
    assert receiver.files == {
        "plugin": "old",
        "opkg": "old",
        "settings": "old",
        "provisioning": "old",
    }


async def test_a_rollback_that_began_in_standby_ends_in_standby(
    credentials: SshCredentials, caplog: pytest.LogCaptureFixture
) -> None:
    """A rollback corner: the forward paths refuse standby, so only a rollback meets it."""
    receiver = FakeReceiver(standby=True)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.installer")
    original = receiver.start_interface

    def wakes(how: str) -> None:
        original(how)
        receiver.standby = False

    receiver.start_interface = wakes  # type: ignore[method-assign]

    await _async_rollback(
        credentials,
        "/backup",
        "/tmp/plugin.ipk",
        "/tmp/enigma2-mqtt-installer-0123456789ab.py",
        "/tmp/manifest",
        "/etc/enigma2/mqttbridge.json.ha-0123456789ab",
        "/tmp/lock",
        True,
        False,
        True,
        receiver.connect,
    )

    assert any(" powerstate --state 5" in command for command in receiver.commands)
    assert receiver.standby is True
    assert "standby: restored" in caplog.text


async def test_nothing_recorded_means_nothing_restored_and_the_record_says_so(
    credentials: SshCredentials, caplog: pytest.LogCaptureFixture
) -> None:
    receiver = FakeReceiver(webif_ok=False)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.installer")

    await _async_rollback(
        credentials,
        "/backup",
        "/tmp/plugin.ipk",
        "/tmp/enigma2-mqtt-installer-0123456789ab.py",
        "/tmp/manifest",
        "/etc/enigma2/mqttbridge.json.ha-0123456789ab",
        "/tmp/lock",
        True,
        False,
        True,
        receiver.connect,
        record=RestartRecord(),
    )

    r2 = next(command for command in receiver.commands if " r2-start " in command)
    assert "--service" not in r2
    assert receiver.zaps == []
    assert "channel: not recorded" in caplog.text


async def test_the_lock_owner_carries_the_transaction_id(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    receiver = FakeReceiver()

    await _install(hass, install_request, tmp_path, receiver)

    claim = next(command for command in receiver.commands if " claim " in command)
    snapshot = next(command for command in receiver.commands if " snapshot " in command)
    nonce = snapshot.rsplit("ha-installer-", 1)[1]
    assert claim.endswith(f"--id {nonce}")


async def test_an_abandoned_transaction_is_put_back_by_its_id(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """Which snapshot is the id's, never the newest: receivers boot in 1970 and jump."""
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab", "ha-installer-ffffffffffff"},
    )

    await _install(hass, install_request, tmp_path, receiver)

    recoveries = [command for command in receiver.commands if " withdraw " in command]
    assert len(recoveries) == 1
    assert "ha-installer-0123456789ab --provisioning" in recoveries[0]
    assert "--settings" not in recoveries[0]
    # Before this transaction takes its own snapshot, which is then the rollback point.
    assert receiver.commands.index(recoveries[0]) < next(
        index for index, command in enumerate(receiver.commands) if " snapshot " in command
    )


@pytest.mark.parametrize(
    ("reclaimed", "snapshots"),
    [("", set()), ("0123456789ab", set()), ("../../etc", {"ha-installer-../../etc"})],
)
async def test_nothing_is_recovered_without_an_id_and_its_snapshot(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    reclaimed: str,
    snapshots: set[str],
) -> None:
    receiver = FakeReceiver(reclaimed_id=reclaimed, snapshots=snapshots)

    await _install(hass, install_request, tmp_path, receiver)

    assert not any(" withdraw " in command for command in receiver.commands)


# --- a recovery that fails keeps the abandoned transaction's id ------------------------


def _nonce(receiver: FakeReceiver) -> str:
    claim = next(command for command in receiver.commands if " claim " in command)
    return claim.rsplit("--id ", 1)[1]


def _sent(receiver: FakeReceiver, word: str) -> list[str]:
    return [command for command in receiver.commands if f" {word} " in command]


async def _recovery_fails_on(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    receiver: FakeReceiver,
    how: str,
) -> InstallerError:
    """Install with a recovery of the abandoned snapshot that fails in the way named."""

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, run=_recovery(how))
    return raised.value


def _recovery(how: str) -> Any:
    """A recovery of the abandoned snapshot that fails in the way named, every time."""

    async def run(self: FakeSession, command: str, **kwargs: Any):
        del kwargs
        if " withdraw /home/root/mqttbridge-backups/ha-installer-0123456789ab" in command:
            self.receiver.commands.append(command)
            if how == "dropped":
                raise OSError("connection reset")
            if how == "timeout":
                raise TimeoutError
            if how == "busy":
                return CommandResult(75, "", "opkg is busy")
            if how == "overlap":
                return CommandResult(76, "", "opkg's lock file was replaced")
            return CommandResult(1, "", "deliberately failed")
        return None

    return run


# The failures a later try may get past, and what the household is told about each: the
# receiver's opkg was busy, or the connection went - nothing about the receiver being put
# back, because it was not.
MAY_PASS = {
    "busy": InstallerErrorCode.OPKG_BUSY,
    "dropped": InstallerErrorCode.SSH_UNAVAILABLE,
    "timeout": InstallerErrorCode.SSH_UNAVAILABLE,
}


@pytest.mark.parametrize("how", sorted(MAY_PASS))
async def test_a_recovery_that_may_pass_next_time_hands_the_lock_back(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    how: str,
) -> None:
    """Released, the lock would forget the one record of which snapshot is still to be put
    back, and the next install would start over a half-changed receiver with nothing to
    recover. Handed back - to the abandoned id, stale as it was - the next claim reclaims it
    at once and puts that snapshot back first. Only for a failure that may pass: opkg busy
    with somebody else's run, a connection that dropped or did not answer in time."""
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab", snapshots={"ha-installer-0123456789ab"}
    )
    caplog.set_level(logging.WARNING, logger="custom_components.enigma2_mqtt.installer")

    err = await _recovery_fails_on(hass, install_request, tmp_path, receiver, how)

    assert err.code is MAY_PASS[how]
    handed = _sent(receiver, "hand-back")
    assert len(handed) == 1
    assert handed[0].endswith(f"--owner {_nonce(receiver)} --id 0123456789ab --attempts 1")
    assert _sent(receiver, "release") == []
    assert receiver.helper_removed is True
    assert not any(" snapshot " in command for command in receiver.commands)
    assert "handed back to the abandoned transaction 0123456789ab" in caplog.text


async def test_a_recovery_that_fails_the_same_way_every_time_does_not_hold_installs_off(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restore that fails on what is in the snapshot - a record it cannot read, a flag it
    does not know, a filesystem it cannot rename across - fails the same way on every try.
    Handed back, it would be tried and failed by every install after it, for ever. So the
    lock is released as before, the household is told the receiver could not be put back,
    and the log names the snapshot to look at; the next install goes ahead."""
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab", snapshots={"ha-installer-0123456789ab"}
    )
    caplog.set_level(logging.WARNING, logger="custom_components.enigma2_mqtt.installer")

    err = await _recovery_fails_on(hass, install_request, tmp_path, receiver, "failed")

    assert err.code is InstallerErrorCode.ROLLBACK_FAILED
    assert "/home/root/mqttbridge-backups/ha-installer-0123456789ab" in err.detail
    assert _sent(receiver, "hand-back") == []
    assert len(_sent(receiver, "release")) == 1
    assert "ha-installer-0123456789ab" in caplog.text
    assert "by hand" in caplog.text

    # The second and the third install over the same receiver: nothing to recover.
    for _ in range(2):
        receiver.commands.clear()
        await _install(hass, install_request, tmp_path, receiver)
        assert _sent(receiver, "withdraw") == []
        assert len(_sent(receiver, "snapshot")) == 1


async def test_a_recovery_that_keeps_failing_is_tried_three_times_and_then_given_up(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """opkg busy on every try - somebody's own run that never ends - may pass, and is handed
    back; but not for ever. The count travels with the lock, and the third failed recovery
    releases it, names the snapshot for a person, and lets the next install go ahead."""
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab", snapshots={"ha-installer-0123456789ab"}
    )
    caplog.set_level(logging.WARNING, logger="custom_components.enigma2_mqtt.installer")

    codes = []
    for attempt in (1, 2, 3):
        receiver.commands.clear()
        err = await _recovery_fails_on(hass, install_request, tmp_path, receiver, "busy")
        codes.append(err.code)
        if attempt < 3:
            assert _sent(receiver, "hand-back")[0].endswith(f" --attempts {attempt}")
            assert _sent(receiver, "release") == []
        else:
            assert _sent(receiver, "hand-back") == []
            assert len(_sent(receiver, "release")) == 1

    assert codes == [
        InstallerErrorCode.OPKG_BUSY,
        InstallerErrorCode.OPKG_BUSY,
        InstallerErrorCode.ROLLBACK_FAILED,
    ]
    assert "3 times" in caplog.text
    assert "by hand" in caplog.text

    receiver.commands.clear()
    await _install(hass, install_request, tmp_path, receiver)
    assert _sent(receiver, "withdraw") == []


async def test_a_snapshot_cut_off_before_its_record_is_nothing_to_recover(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A power cut, or a killed helper, while the snapshot was being copied leaves its
    directory without `snapshot.json`. That record is the snapshot's last write, and its
    transaction changes nothing until the snapshot has returned - so a snapshot without it
    belongs to a transaction that changed nothing, and there is nothing to put back. Trying
    would fail on the missing record every time. Two installs in a row both go ahead."""
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab", partial_snapshots={"ha-installer-0123456789ab"}
    )
    caplog.set_level(logging.WARNING, logger="custom_components.enigma2_mqtt.installer")

    for _ in range(2):
        receiver.commands.clear()
        await _install(hass, install_request, tmp_path, receiver)
        assert _sent(receiver, "withdraw") == []
        assert _sent(receiver, "hand-back") == []

    assert "ha-installer-0123456789ab" in caplog.text
    assert "snapshot.json" in caplog.text


async def test_a_recovery_that_put_the_snapshot_back_under_a_lost_opkg_lock_says_so(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The helper's restore ran to its end and then found opkg's lock lost under it (76): the
    snapshot is back, and only the package database is in doubt. That is not "could not be
    put back" - which would send somebody to redo a restore that happened - and there is
    nothing left to recover, so the lock is released rather than handed back. The install
    stops: another opkg run is about."""
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab", snapshots={"ha-installer-0123456789ab"}
    )
    caplog.set_level(logging.WARNING, logger="custom_components.enigma2_mqtt.installer")

    err = await _recovery_fails_on(hass, install_request, tmp_path, receiver, "overlap")

    assert err.code is InstallerErrorCode.ROLLBACK_OPKG_OVERLAP
    assert "could not be put back" not in (err.detail or "")
    assert "could not be put back" not in caplog.text
    assert "was put back" in caplog.text
    assert _sent(receiver, "hand-back") == []
    assert len(_sent(receiver, "release")) == 1
    assert not any(" snapshot " in command for command in receiver.commands)

    receiver.commands.clear()
    await _install(hass, install_request, tmp_path, receiver)
    assert _sent(receiver, "withdraw") == []


async def test_a_helper_that_cannot_be_read_here_is_not_an_unreachable_receiver(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper is read from this integration's own files before it is uploaded; a failure
    there is local, and saying the receiver's SSH service could not be reached would send
    somebody to check the wrong machine."""
    receiver = FakeReceiver()
    monkeypatch.setattr(installer_helper, "__file__", str(tmp_path / "missing" / "helper.py"))

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.UPLOAD_FAILED
    assert "this integration's files" in raised.value.detail
    assert _sent(receiver, "claim") == []


async def test_a_hand_back_that_fails_still_frees_the_receiver_and_names_the_snapshot(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab",
        snapshots={"ha-installer-0123456789ab"},
        fail_on=" hand-back ",
    )
    caplog.set_level(logging.WARNING, logger="custom_components.enigma2_mqtt.installer")

    await _recovery_fails_on(hass, install_request, tmp_path, receiver, "busy")

    assert len(_sent(receiver, "release")) == 1
    assert "ha-installer-0123456789ab" in caplog.text
    assert "by hand" in caplog.text


async def test_a_hand_back_refused_as_somebody_elses_lock_releases_nothing(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hand-back refuses a lock that no longer names this transaction - it went stale
    under a slow recovery and somebody else took it. Releasing it then would take a live
    self-update's or another install's lock away, so neither is done."""
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab", snapshots={"ha-installer-0123456789ab"}
    )
    caplog.set_level(logging.WARNING, logger="custom_components.enigma2_mqtt.installer")
    recovery = _recovery("busy")

    async def run(self: FakeSession, command: str, **kwargs: Any):
        if " hand-back " in command:
            self.receiver.commands.append(command)
            return CommandResult(
                installer_helper.EXIT_LOCK_NOT_OURS, "", "the lock is not this transaction's"
            )
        return await recovery(self, command, **kwargs)

    with pytest.raises(InstallerError):
        await _install(hass, install_request, tmp_path, receiver, run=run)

    assert len(_sent(receiver, "hand-back")) == 1
    assert _sent(receiver, "release") == []
    assert receiver.helper_removed is True
    assert "ha-installer-0123456789ab" in caplog.text
    assert "no longer this transaction's" in caplog.text


@pytest.mark.parametrize("fail_on", [None, "opkg install"], ids=["committed", "rolled_back"])
async def test_every_release_names_the_transaction_that_claimed_the_lock(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path, fail_on: str | None
) -> None:
    receiver = FakeReceiver(fail_on=fail_on)

    if fail_on is None:
        await _install(hass, install_request, tmp_path, receiver)
    else:
        with pytest.raises(InstallerError):
            await _install(hass, install_request, tmp_path, receiver)

    claim = _sent(receiver, "claim")[0].split()
    nonce = claim[claim.index("--id") + 1]
    releases = _sent(receiver, "release")
    assert releases
    assert all(release.split()[-2:] == ["--owner", nonce] for release in releases)


async def test_the_release_after_a_failed_hand_back_releases_only_this_transaction_s_lock(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hand-back that failed for any other reason is followed by a release - by then the
    lock may be a claimer's that reclaimed ours in between. The release names this
    transaction, and a lock that is no longer ours is left alone: not an error about a lock
    that "remains", which would send somebody to delete a live install's lock by hand."""
    receiver = FakeReceiver(
        reclaimed_id="0123456789ab", snapshots={"ha-installer-0123456789ab"}
    )
    caplog.set_level(logging.WARNING, logger="custom_components.enigma2_mqtt.installer")
    recovery = _recovery("busy")

    async def run(self: FakeSession, command: str, **kwargs: Any):
        if " hand-back " in command:
            self.receiver.commands.append(command)
            return CommandResult(1, "", "No such file or directory")
        if " release " in command:
            self.receiver.commands.append(command)
            return CommandResult(
                installer_helper.EXIT_LOCK_NOT_OURS, "", "the lock is not this transaction's"
            )
        return await recovery(self, command, **kwargs)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, run=run)

    claim = _sent(receiver, "claim")[0].split()
    nonce = claim[claim.index("--id") + 1]
    (release,) = _sent(receiver, "release")
    assert release.split()[-2:] == ["--owner", nonce]
    assert raised.value.code is MAY_PASS["busy"]
    assert receiver.helper_removed is True
    assert "no longer this transaction's, so it is left alone" in caplog.text
    assert "lock remains" not in caplog.text


@pytest.mark.parametrize(
    ("reclaimed", "snapshots"),
    [
        # Recovered, then the install failed on its own snapshot: nothing is left to recover.
        ("0123456789ab", {"ha-installer-0123456789ab"}),
        # An id whose snapshot is gone: there was nothing to recover.
        ("0123456789ab", set()),
        ("", set()),
    ],
    ids=["recovered", "no_snapshot", "nothing_reclaimed"],
)
async def test_any_other_failure_before_the_snapshot_releases_the_lock(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    reclaimed: str,
    snapshots: set[str],
) -> None:
    receiver = FakeReceiver(reclaimed_id=reclaimed, snapshots=snapshots, fail_on=" snapshot ")

    with pytest.raises(InstallerError):
        await _install(hass, install_request, tmp_path, receiver)

    assert _sent(receiver, "hand-back") == []
    assert len(_sent(receiver, "release")) == 1

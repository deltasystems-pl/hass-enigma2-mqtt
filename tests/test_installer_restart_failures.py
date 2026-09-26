"""The restart rule when the connection drops or Home Assistant stops.

The paths a working receiver and a working network never take, and the ones on which a
wrong step costs most: an `init 4` under a question the image is asking on the
television, a rollback whose script loses its helper, an install that proved itself and
is then undone because checking the channel afterwards failed.

A connection that closes under a command raises asyncssh's own errors, which are not
`OSError`s; the fake receiver raises exactly those.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncssh
from homeassistant.core import HomeAssistant
import pytest

from custom_components.enigma2_mqtt import installer
from custom_components.enigma2_mqtt.installer import (
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    SshCredentials,
    _async_rollback,
    _AsyncSshSession,
)
from custom_components.enigma2_mqtt.restart_rule import RestartRecord

from .test_installer import WATCHED, FakeReceiver, FakeSession, credentials, install_request
from .test_installer_restart_rule import ELSEWHERE, _install

__all__ = ["credentials", "install_request"]

HELPER = "/tmp/enigma2-mqtt-installer-0123456789ab.py"
BACKUP = "/home/root/mqttbridge-backups/ha-installer-0123456789ab"
R2_DIR = "/tmp/enigma2-mqtt-r2-0123456789ab"


def _r2(receiver: FakeReceiver) -> list[str]:
    return [command for command in receiver.commands if " r2-start " in command]


def _released(receiver: FakeReceiver) -> bool:
    return any(" release " in command for command in receiver.commands)


def _after_request(receiver: FakeReceiver) -> bool:
    """Whether the clean restart has been asked for yet."""
    return any(" powerstate --state 3" in sent for sent in receiver.commands)


async def _rollback(credentials: SshCredentials, connect: Any, **kwargs: Any) -> None:
    await _async_rollback(
        credentials,
        BACKUP,
        "/tmp/plugin.ipk",
        HELPER,
        "/tmp/manifest",
        "/etc/enigma2/mqttbridge.json.ha-0123456789ab",
        "/tmp/lock",
        True,
        True,
        True,
        connect,
        **kwargs,
    )


# --- MF1: nothing between the request and a new enigma2 may end in `init 4` ----------


async def test_a_connection_dropped_while_the_question_is_asked_still_withdraws(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """A gap in what this end sees is not an answer: connect again and read the pid."""
    receiver = FakeReceiver(question_on_restart=True)
    dropped = {"done": False}

    async def drop(self: FakeSession, command: str, **kwargs: Any):
        if command == "pidof enigma2" and _after_request(self.receiver):
            if not dropped["done"]:
                dropped["done"] = True
                raise asyncssh.ChannelOpenError(2, "SSH connection closed")
        return None

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, run=drop)

    assert dropped["done"]
    assert raised.value.code is InstallerErrorCode.RESTART_WITHDRAWN
    assert _r2(receiver) == []
    assert receiver.restarts == []
    assert receiver.withdrawn is True


async def test_home_assistant_stopping_while_the_question_is_asked_forces_nothing(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stop leaves the transaction as it is: locked, with its id, for the next install."""
    receiver = FakeReceiver(question_on_restart=True)

    async def stop(self: FakeSession, command: str, **kwargs: Any):
        if command == "pidof enigma2" and _after_request(self.receiver):
            raise asyncio.CancelledError
        return None

    with pytest.raises(asyncio.CancelledError):
        await _install(hass, install_request, tmp_path, receiver, run=stop)

    assert _r2(receiver) == []
    assert receiver.restarts == []
    assert not _released(receiver)
    assert not any(" restore " in command for command in receiver.commands)
    assert "outcome was not seen" in caplog.text


async def test_a_connection_dropped_under_the_withdraw_repeats_it(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    receiver = FakeReceiver(question_on_restart=True)
    dropped = {"done": False}

    async def drop(self: FakeSession, command: str, **kwargs: Any):
        if " withdraw " in command and not dropped["done"]:
            dropped["done"] = True
            self.receiver.commands.append(command)
            raise asyncssh.ConnectionLost("gone")
        return None

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, run=drop)

    assert raised.value.code is InstallerErrorCode.RESTART_WITHDRAWN
    assert len([command for command in receiver.commands if " withdraw " in command]) == 2
    assert _r2(receiver) == []
    assert _released(receiver)


async def test_home_assistant_stopping_under_the_withdraw_forces_nothing(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    receiver = FakeReceiver(question_on_restart=True)

    async def stop(self: FakeSession, command: str, **kwargs: Any):
        if " withdraw " in command:
            raise asyncio.CancelledError
        return None

    with pytest.raises(asyncio.CancelledError):
        await _install(hass, install_request, tmp_path, receiver, run=stop)

    assert _r2(receiver) == []
    assert not _released(receiver)


async def test_a_receiver_that_cannot_be_reached_after_the_request_is_left_as_it_is(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """Nothing seen, nothing undone: the lock and its id are what the next install recovers."""
    receiver = FakeReceiver(question_on_restart=True)
    gone = {"now": False}
    original_connect = receiver.connect

    async def connect(credentials: SshCredentials) -> FakeSession:
        if gone["now"]:
            raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE)
        return await original_connect(credentials)

    receiver.connect = connect  # type: ignore[method-assign]

    async def unplug(self: FakeSession, command: str, **kwargs: Any):
        if gone["now"]:
            raise asyncssh.ConnectionLost("gone")
        if " powerstate --state 3" in command:
            gone["now"] = True
        return None

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, run=unplug)

    assert raised.value.code is InstallerErrorCode.RESTART_UNOBSERVED
    assert _r2(receiver) == []
    assert not _released(receiver)


async def test_a_restart_just_after_the_last_look_is_never_reported_withdrawn(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """The pid after the withdraw is compared with the one from before the request."""
    receiver = FakeReceiver(question_on_restart=True)
    original = installer._async_withdraw

    async def answered_first(*args: Any, **kwargs: Any):
        receiver.start_interface("clean")  # "yes", pressed after the last poll
        return await original(*args, **kwargs)

    with patch.object(installer, "_async_withdraw", answered_first):
        with pytest.raises(InstallerError) as raised:
            await _install(hass, install_request, tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RESTART_FAILED
    assert receiver.restarts == ["clean", "stopped"]


async def test_the_last_look_before_the_restart_still_refuses_standby(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """The receiver went to sleep while the package was being installed."""
    receiver = FakeReceiver()

    async def sleeps_after_install(self: FakeSession, command: str, **kwargs: Any):
        if command.startswith("opkg install"):
            self.receiver.standby = True
        return None

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, run=sleeps_after_install)

    assert raised.value.code is InstallerErrorCode.STANDBY
    assert not any(" powerstate " in command for command in receiver.commands)
    assert _r2(receiver) == []
    assert receiver.rolled_back is True


# --- MF3: the commit comes before the channel check ------------------------------------


async def test_a_connection_lost_while_the_channel_is_checked_keeps_a_proven_install(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    receiver = FakeReceiver()
    dead: set[int] = set()

    async def blip(self: FakeSession, command: str, **kwargs: Any):
        if id(self) in dead:
            raise asyncssh.ChannelOpenError(2, "SSH connection closed")
        if self.receiver.restarts == ["clean"] and command.endswith(" record") and not dead:
            dead.add(id(self))
            raise asyncssh.ChannelOpenError(2, "SSH connection closed")
        return None

    result = await _install(hass, install_request, tmp_path, receiver, run=blip)

    assert result.restarted is True
    assert _r2(receiver) == []
    assert receiver.files["plugin"] == "new"
    # The lock went - the install was committed - before the channel was looked at.
    request = next(i for i, c in enumerate(receiver.commands) if " powerstate --state 3" in c)
    release = next(i for i, c in enumerate(receiver.commands) if " release " in c)
    assert release > request
    assert not any(c.endswith(" record") for c in receiver.commands[request:release])


async def test_home_assistant_stopping_while_the_channel_is_checked_keeps_it_too(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    receiver = FakeReceiver()
    stopped = {"done": False}

    async def stop(self: FakeSession, command: str, **kwargs: Any):
        if self.receiver.restarts == ["clean"] and command.endswith(" record"):
            if not stopped["done"]:
                stopped["done"] = True
                raise asyncio.CancelledError
        return None

    # Committed, and the stop still reaches the caller.
    with pytest.raises(asyncio.CancelledError):
        await _install(hass, install_request, tmp_path, receiver, run=stop)

    assert stopped["done"]
    assert _r2(receiver) == []
    assert _released(receiver)
    assert receiver.files["plugin"] == "new"


# --- MF2: the rollback's script keeps what it needs, and so does the lock ---------------


async def test_home_assistant_stopping_while_the_script_runs_keeps_the_lock_and_its_files(
    credentials: SshCredentials,
) -> None:
    receiver = FakeReceiver()

    def still_stopping(command: str) -> None:
        receiver.r2_steps += ["begun 4242", "stopping 0"]

    receiver.run_r2 = still_stopping  # type: ignore[method-assign]

    class Stopping(FakeSession):
        async def run(
            self, command: str, *, input: bytes | None = None, timeout: float = 30
        ) -> CommandResult:
            if command.startswith("cat ") and command.endswith("/status"):
                raise asyncio.CancelledError
            return await super().run(command, input=input, timeout=timeout)

    async def connect(_credentials: SshCredentials) -> Stopping:
        return Stopping(receiver)

    with pytest.raises(asyncio.CancelledError):
        await _rollback(credentials, connect)

    assert not _released(receiver)
    assert not receiver.helper_removed
    assert receiver.r2_removed == []


async def test_a_script_whose_end_is_never_seen_keeps_the_lock_and_its_files(
    credentials: SshCredentials, caplog: pytest.LogCaptureFixture
) -> None:
    receiver = FakeReceiver()

    def still_stopping(command: str) -> None:
        receiver.r2_steps += ["begun 4242", "stopping 0"]

    receiver.run_r2 = still_stopping  # type: ignore[method-assign]

    with pytest.raises(InstallerError) as raised:
        await _rollback(credentials, receiver.connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_UNOBSERVED
    assert not _released(receiver)
    assert not receiver.helper_removed
    assert receiver.r2_removed == []
    assert "switch it off and on again" in caplog.text


async def test_a_script_started_under_a_dropped_connection_is_followed_to_its_end(
    credentials: SshCredentials,
) -> None:
    """Whether it started is read from its status file, not from the lost answer."""
    receiver = FakeReceiver()

    class Dropping(FakeSession):
        async def run(
            self, command: str, *, input: bytes | None = None, timeout: float = 30
        ) -> CommandResult:
            result = await super().run(command, input=input, timeout=timeout)
            if " r2-start " in command:
                raise ConnectionError("SSH connection closed")
            return result

    async def connect(_credentials: SshCredentials) -> Dropping:
        return Dropping(receiver)

    await _rollback(credentials, connect)

    assert receiver.restarts == ["stopped"]
    assert _released(receiver)
    assert receiver.r2_removed == [R2_DIR]
    assert receiver.helper_removed


async def test_the_script_and_its_directory_go_only_after_its_end_is_seen(
    credentials: SshCredentials,
) -> None:
    receiver = FakeReceiver()

    await _rollback(credentials, receiver.connect)

    removal = receiver.commands.index(f"rm -rf {R2_DIR}")
    started = max(i for i, c in enumerate(receiver.commands) if c.endswith("/status"))
    assert started < removal
    assert f"--dir {R2_DIR}" in _r2(receiver)[0]


async def test_an_interface_that_would_not_stop_is_not_reported_put_back(
    credentials: SshCredentials,
) -> None:
    """Only the files went back; the running interface keeps - and saves - its own settings."""
    receiver = FakeReceiver(r2_stop_timeout=True)
    receiver.files["settings"] = "new"

    with pytest.raises(InstallerError) as raised:
        await _rollback(credentials, receiver.connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_FAILED
    assert receiver.files["settings"] == "new"
    assert receiver.files["plugin"] == "old"
    assert receiver.restore_flags == ["--provisioning"]
    assert receiver.zaps == []
    assert _released(receiver)


# --- the reviewer's surviving mutants ---------------------------------------------------


async def test_a_rollback_keeps_what_the_household_watches_now(
    credentials: SshCredentials,
) -> None:
    """The channel recorded before the install is only the fallback for a silent interface."""
    receiver = FakeReceiver(service=ELSEWHERE)

    await _rollback(credentials, receiver.connect, record=RestartRecord(WATCHED, False))

    assert f"--service {ELSEWHERE}" in _r2(receiver)[0]


async def test_the_channel_is_checked_only_once_openwebif_answers(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restarted interface answers after eleven to fourteen seconds; silence is not "lost"."""
    receiver = FakeReceiver(start_channel=ELSEWHERE, blind_reads_after_restart=2)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.installer")

    await _install(hass, install_request, tmp_path, receiver)

    assert receiver.zaps == [WATCHED]
    assert "channel: restored" in caplog.text


async def test_a_receiver_already_back_in_standby_is_not_sent_there_again(
    credentials: SshCredentials, caplog: pytest.LogCaptureFixture
) -> None:
    receiver = FakeReceiver(standby=True)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.installer")

    await _rollback(credentials, receiver.connect)

    assert not any(" powerstate --state 5" in command for command in receiver.commands)
    assert "standby: kept" in caplog.text


# --- the SSH adapter --------------------------------------------------------------------


async def test_the_adapter_turns_a_closed_connection_into_an_os_error() -> None:
    """Every handler here catches `OSError`; asyncssh's own errors are not one."""

    class _Connection:
        async def run(self, command: str, *, input: bytes | None = None, timeout: float = 30):
            del command, input, timeout
            raise asyncssh.ChannelOpenError(2, "SSH connection closed")

    with pytest.raises(ConnectionError):
        await _AsyncSshSession(_Connection()).run("pidof enigma2")


# --- review round 2 ---------------------------------------------------------------------


@pytest.mark.parametrize("late", [3, 15])
async def test_a_script_that_starts_late_under_a_lost_answer_is_not_given_up(
    credentials: SshCredentials, monkeypatch: pytest.MonkeyPatch, late: int
) -> None:
    """The answer to `r2-start` was lost, and the receiver is still starting the script.

    A missing status file then means "not yet", not "never": the first reads come before
    the directory exists. Giving up there released the lock and deleted the directory
    under a script that had just stopped the interface. The late case runs past the grace
    period and is kept by asking the receiver for the script's directory and process.
    """
    monkeypatch.setattr(installer, "R2_FOLLOW_TIMEOUT", 5.0)
    receiver = FakeReceiver(r2_start_late=late)

    class Dropping(FakeSession):
        async def run(
            self, command: str, *, input: bytes | None = None, timeout: float = 30
        ) -> CommandResult:
            result = await super().run(command, input=input, timeout=timeout)
            if " r2-start " in command:
                raise ConnectionError("SSH connection closed")
            return result

    async def connect(_credentials: SshCredentials) -> Dropping:
        return Dropping(receiver)

    await _rollback(credentials, connect)

    assert receiver.restarts == ["stopped"]
    assert receiver.files["settings"] == "old"
    assert _released(receiver)
    # Removed once, after its end was seen - not while it was still starting.
    assert receiver.r2_removed == [R2_DIR]
    release = next(i for i, c in enumerate(receiver.commands) if " release " in c)
    last_read = max(i for i, c in enumerate(receiver.commands) if c.endswith("/status"))
    assert last_read < release


async def test_a_script_that_never_started_leaves_nothing_to_delete(
    credentials: SshCredentials,
) -> None:
    """Concluded only after the grace period and the receiver's own answer; nothing removed."""
    receiver = FakeReceiver()

    class LostBefore(FakeSession):
        async def run(
            self, command: str, *, input: bytes | None = None, timeout: float = 30
        ) -> CommandResult:
            if " r2-start " in command:
                self.receiver.commands.append(command)
                raise ConnectionError("SSH connection closed")
            return await super().run(command, input=input, timeout=timeout)

    async def connect(_credentials: SshCredentials) -> LostBefore:
        return LostBefore(receiver)

    with pytest.raises(InstallerError) as raised:
        await _rollback(credentials, connect)

    assert raised.value.code is InstallerErrorCode.ROLLBACK_FAILED
    assert any("/proc/[0-9]*/cmdline" in command for command in receiver.commands)
    assert receiver.r2_removed == []
    assert not any(command.startswith("rm -rf") for command in receiver.commands)
    assert _released(receiver)


async def test_a_restart_openwebif_never_confirmed_is_not_called_a_question(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """Withdrawn all the same - nothing restarted - but the sentence does not claim a question."""
    receiver = FakeReceiver(question_on_restart=True, powerstate_answered=False)

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver)

    assert raised.value.code is InstallerErrorCode.RESTART_UNCONFIRMED
    assert receiver.withdrawn is True
    assert _r2(receiver) == []


async def test_a_lost_answer_to_the_restart_request_is_not_called_a_question(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    receiver = FakeReceiver(question_on_restart=True)

    async def lose_the_answer(self: FakeSession, command: str, **kwargs: Any):
        if " powerstate --state 3" in command:
            self.receiver.commands.append(command)
            raise asyncssh.ConnectionLost("gone")
        return None

    with pytest.raises(InstallerError) as raised:
        await _install(hass, install_request, tmp_path, receiver, run=lose_the_answer)

    assert raised.value.code is InstallerErrorCode.RESTART_UNCONFIRMED
    assert _r2(receiver) == []


async def test_the_receivers_own_answer_about_the_script_is_read_from_proc(
    tmp_path: Path,
) -> None:
    """The probe run by a real shell: its own command line never names the path whole."""
    directory = tmp_path / "tmp" / "enigma2-mqtt-r2-0123456789ab"

    class Local:
        async def run(self, command: str, **kwargs: Any) -> CommandResult:
            del kwargs
            process = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", command,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await process.communicate()
            return CommandResult(process.returncode or 0, out.decode(), err.decode())

    assert await installer._async_r2_absent(Local(), str(directory)) is True

    # A process naming the script, before its directory exists.
    running = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", "sleep 5", str(directory / "r2.sh")
    )
    try:
        assert await installer._async_r2_absent(Local(), str(directory)) is False
    finally:
        running.kill()
        await running.wait()

    directory.mkdir(parents=True)
    assert await installer._async_r2_absent(Local(), str(directory)) is False

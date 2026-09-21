"""Production AsyncSSH adapter against an in-process SSH server.

Everything else about the installer is tested against a fake session, which is the right
place to test a state machine. It is the wrong place to test the transport: the fake
answers because it was written to, and the mapping from what a real SSH library raises
onto the error codes the config flow shows is exactly what a fake cannot check. So this
file runs the real adapter against a real server in this process — one that refuses the
password, one that is not there at all, one that never answers, and one that fails a
command — and asserts the code a user would end up reading.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import asyncssh
import pytest

from custom_components.enigma2_mqtt.installer import (
    InstallerError,
    InstallerErrorCode,
    SshCredentials,
    _async_connect,
    _AsyncSshSession,
    _run_checked,
    async_probe_host_key,
)


class _Server(asyncssh.SSHServer):
    def __init__(self, passwords: list[tuple[str, str]], *, needs_auth: bool = True) -> None:
        self._passwords = passwords
        self._needs_auth = needs_auth

    def begin_auth(self, username: str) -> bool:
        return self._needs_auth

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        self._passwords.append((username, password))
        return username == "root" and password == "test-password"


async def _process(process: Any) -> None:
    data = await process.stdin.read()
    process.stdout.write(b"received:" + data)
    process.exit(0)


async def _refusing_process(process: Any) -> None:
    """A command that fails the way opkg fails: exit status and a complaint."""
    with suppress(Exception):
        await process.stdin.read()
    process.stderr.write(b"opkg: satisfy_dependencies_for: Cannot satisfy the dependencies\n")
    process.exit(1)


@asynccontextmanager
async def _server(
    *,
    process_factory: Callable[[Any], Any] = _process,
    needs_auth: bool = True,
) -> AsyncIterator[tuple[int, Any, list[tuple[str, str]]]]:
    passwords: list[tuple[str, str]] = []
    key = asyncssh.generate_private_key("ssh-ed25519")
    listener = await asyncssh.create_server(
        lambda: _Server(passwords, needs_auth=needs_auth),
        "127.0.0.1",
        0,
        server_host_keys=[key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        yield listener.get_port(), key, passwords
    finally:
        listener.close()
        await listener.wait_closed()


async def _closed_port() -> int:
    """Return a port that was listening a moment ago and is not any more."""
    async with _server() as (port, _key, _passwords):
        pass
    return port


def _credentials(port: int, key: Any, password: str = "test-password") -> SshCredentials:
    return SshCredentials(
        "127.0.0.1",
        "root",
        password,
        key.export_public_key().decode().strip(),
        port,
    )


async def test_probe_reads_key_without_sending_password(socket_enabled: None) -> None:
    async with _server() as (port, key, passwords):
        probed = await async_probe_host_key("127.0.0.1", port)

    assert probed.algorithm == key.get_algorithm()
    assert probed.fingerprint == key.get_fingerprint("sha256")
    assert passwords == []


async def test_probe_closes_a_connection_it_managed_to_open(socket_enabled: None) -> None:
    """A server that wants no authentication lets the probe all the way in.

    The probe asks for no authentication method at all, which most receivers answer by
    refusing — and that refusal is the path the other test takes. A box that does not
    refuse leaves this holding an open connection it never wanted, and it has to be given
    back rather than left to the garbage collector.
    """
    async with _server(needs_auth=False) as (port, key, passwords):
        probed = await async_probe_host_key("127.0.0.1", port)

    assert probed.public_key == key.export_public_key().decode().strip()
    assert passwords == []


async def test_a_probe_that_never_saw_a_key_is_not_a_success(socket_enabled: None) -> None:
    """Refused before the key exchange is not the same as refused after it.

    The probe treats `PermissionDenied` as the expected end of its conversation, because
    a receiver refuses the empty username after proving possession of its host key. If
    the refusal arrives without a key having been seen, there is nothing to show the user
    and nothing to pin, and "unreachable" is the honest answer.
    """
    with patch(
        "custom_components.enigma2_mqtt.installer.asyncssh.connect",
        side_effect=asyncssh.PermissionDenied("no such user"),
    ):
        with pytest.raises(InstallerError) as raised:
            await async_probe_host_key("127.0.0.1", 22)

    assert raised.value.code is InstallerErrorCode.SSH_UNAVAILABLE


async def test_a_receiver_that_is_not_there_is_reported_as_unreachable(
    socket_enabled: None,
) -> None:
    """The first thing a user does is type the wrong address, or type it too early."""
    port = await _closed_port()

    with pytest.raises(InstallerError) as raised:
        await async_probe_host_key("127.0.0.1", port)
    assert raised.value.code is InstallerErrorCode.SSH_UNAVAILABLE

    # A pinned key from a previous, successful enrolment: the receiver moved or went
    # away, and nothing about the stored identity says so.
    with pytest.raises(InstallerError) as raised:
        await _async_connect(_credentials(port, asyncssh.generate_private_key("ssh-ed25519")))
    assert raised.value.code is InstallerErrorCode.SSH_UNAVAILABLE


async def test_pinned_adapter_authenticates_and_streams_bytes(socket_enabled: None) -> None:
    async with _server() as (port, key, passwords):
        session = await _async_connect(_credentials(port, key))
        try:
            result = await session.run("ignored", input=b"secret streamed bytes")
        finally:
            await session.close()

    assert result.exit_status == 0
    assert result.stdout == "received:secret streamed bytes"
    assert passwords == [("root", "test-password")]


async def test_a_wrong_password_is_an_authentication_failure(socket_enabled: None) -> None:
    """The one failure that has its own recovery: the flow starts a reauth on it."""
    async with _server() as (port, key, passwords):
        with pytest.raises(InstallerError) as raised:
            await _async_connect(_credentials(port, key, password="not-the-password"))

    assert raised.value.code is InstallerErrorCode.AUTH_FAILED
    assert passwords == [("root", "not-the-password")]


async def test_changed_host_key_is_rejected_before_authentication(socket_enabled: None) -> None:
    wrong = asyncssh.generate_private_key("ssh-ed25519")
    async with _server() as (port, _key, passwords):
        credentials = SshCredentials(
            "127.0.0.1",
            "root",
            "test-password",
            wrong.export_public_key().decode().strip(),
            port,
        )
        with pytest.raises(InstallerError) as raised:
            await _async_connect(credentials)

    assert raised.value.code is InstallerErrorCode.HOST_KEY_CHANGED
    assert passwords == []


async def test_a_command_that_never_answers_becomes_its_own_error_code(
    socket_enabled: None,
) -> None:
    """A receiver whose flash has stalled answers SSH and then nothing at all.

    Every installer command carries a timeout for this, and the value of the timeout is
    that it reaches the caller as the failure of that step rather than as a flow that
    never returns.
    """
    release = asyncio.Event()

    async def _hanging_process(process: Any) -> None:
        with suppress(Exception):
            await process.stdin.read()
        await release.wait()
        process.exit(0)

    async with _server(process_factory=_hanging_process) as (port, key, _passwords):
        session = await _async_connect(_credentials(port, key))
        try:
            with pytest.raises(InstallerError) as raised:
                await _run_checked(
                    session,
                    "opkg install /tmp/plugin.ipk",
                    InstallerErrorCode.INSTALL_FAILED,
                    input=b"",
                    timeout=0.2,
                )
        finally:
            release.set()
            await session.close()

    assert raised.value.code is InstallerErrorCode.INSTALL_FAILED


async def test_a_command_that_exits_non_zero_is_a_failure_without_its_output(
    socket_enabled: None,
) -> None:
    """opkg refusing to install is the failure this maps, and it maps only the code.

    The receiver's complaint is not carried into the error: an installer error is shown
    in a config flow and written to a log, and neither is a place for whatever a remote
    command decided to print.
    """
    async with _server(process_factory=_refusing_process) as (port, key, _passwords):
        session = await _async_connect(_credentials(port, key))
        try:
            with pytest.raises(InstallerError) as raised:
                await _run_checked(
                    session,
                    "opkg install /tmp/plugin.ipk",
                    InstallerErrorCode.INSTALL_FAILED,
                    input=b"",
                )
        finally:
            await session.close()

    assert raised.value.code is InstallerErrorCode.INSTALL_FAILED
    assert "satisfy" not in str(raised.value)
    assert raised.value.detail == ""


async def test_a_command_that_shouts_is_cut_before_it_is_kept() -> None:
    """A megabyte of remote output must not become a megabyte of Home Assistant memory.

    A receiver answering a mistyped command with the contents of a device is not a case
    worth standing a server up for, but it is a case worth being sure about, so the
    adapter is given the answer directly.
    """

    class _Connection:
        async def run(self, command: str, *, input: bytes | None = None, timeout: float = 30):
            del command, input, timeout
            return SimpleNamespace(
                exit_status=0,
                stdout=b"o" * (1024 * 1024),
                stderr=b"e" * (1024 * 1024),
            )

    result = await _AsyncSshSession(_Connection()).run("cat /dev/urandom")

    assert result.exit_status == 0
    assert len(result.stdout) == 64 * 1024
    assert len(result.stderr) == 64 * 1024


async def test_a_command_with_no_output_at_all_is_not_a_failure() -> None:
    """A receiver can answer `None` where a pipe would answer an empty string."""

    class _Connection:
        async def run(self, command: str, *, input: bytes | None = None, timeout: float = 30):
            del command, input, timeout
            return SimpleNamespace(exit_status=0, stdout=None, stderr=None)

    result = await _AsyncSshSession(_Connection()).run("true")

    assert result.stdout == ""
    assert result.stderr == ""

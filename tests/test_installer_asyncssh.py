"""Production AsyncSSH adapter against an in-process SSH server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncssh
import pytest

from custom_components.enigma2_mqtt.installer import (
    InstallerError,
    InstallerErrorCode,
    SshCredentials,
    _async_connect,
    async_probe_host_key,
)


class _Server(asyncssh.SSHServer):
    def __init__(self, passwords: list[tuple[str, str]]) -> None:
        self._passwords = passwords

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        self._passwords.append((username, password))
        return username == "root" and password == "test-password"


async def _process(process: Any) -> None:
    data = await process.stdin.read()
    process.stdout.write(b"received:" + data)
    process.exit(0)


@asynccontextmanager
async def _server() -> AsyncIterator[tuple[int, Any, list[tuple[str, str]]]]:
    passwords: list[tuple[str, str]] = []
    key = asyncssh.generate_private_key("ssh-ed25519")
    listener = await asyncssh.create_server(
        lambda: _Server(passwords),
        "127.0.0.1",
        0,
        server_host_keys=[key],
        process_factory=_process,
        encoding=None,
    )
    try:
        yield listener.get_port(), key, passwords
    finally:
        listener.close()
        await listener.wait_closed()


async def test_probe_reads_key_without_sending_password(socket_enabled: None) -> None:
    async with _server() as (port, key, passwords):
        probed = await async_probe_host_key("127.0.0.1", port)

    assert probed.algorithm == key.get_algorithm()
    assert probed.fingerprint == key.get_fingerprint("sha256")
    assert passwords == []


async def test_pinned_adapter_authenticates_and_streams_bytes(socket_enabled: None) -> None:
    async with _server() as (port, key, passwords):
        credentials = SshCredentials(
            "127.0.0.1",
            "root",
            "test-password",
            key.export_public_key().decode().strip(),
            port,
        )
        session = await _async_connect(credentials)
        try:
            result = await session.run("ignored", input=b"secret streamed bytes")
        finally:
            await session.close()

    assert result.exit_status == 0
    assert result.stdout == "received:secret streamed bytes"
    assert passwords == [("root", "test-password")]


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

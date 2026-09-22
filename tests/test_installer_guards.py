"""The installer's refusals, and the paths taken when recovery itself goes wrong.

Everything here is a failure path. They are the parts that only run on a bad day, which
is exactly why they are worth a test: the happy path is exercised by every other file,
and a guard that has never been executed is a guard nobody has checked.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.enigma2_mqtt.bundle import BundledPlugin, load_bundled_plugin
from custom_components.enigma2_mqtt.installer import (
    PACKAGE,
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    Provisioning,
    SshCredentials,
    _async_validate_receiver_identity,
    _installed_hash_manifest,
    async_install,
)

from .test_installer import FakeReceiver, FakeSession, credentials, install_request

# Re-exported so pytest resolves the fixtures this module borrows.
__all__ = ["credentials", "install_request"]


def _bundle(tmp_path: Path) -> BundledPlugin:
    artifact = tmp_path / "plugin.ipk"
    artifact.write_bytes(b"ipk bytes")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return BundledPlugin(artifact, "0.1.0", digest, "1" * 40)


async def _install(
    hass: HomeAssistant,
    request: InstallRequest,
    receiver: FakeReceiver,
    bundle: BundledPlugin,
    *,
    run: Any = None,
    reach_restart: bool = False,
) -> InstallerErrorCode:
    """Run a transaction that is expected to fail, and return how."""
    original = FakeSession.run

    async def default_run(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, bundle.sha256 + "\n")
        return await original(self, command, **kwargs)

    async def established_watch(*args: Any, **kwargs: Any):
        """A restart watch that subscribes without a broker behind it."""
        del args, kwargs
        loop = asyncio.get_running_loop()
        watch = type("Watch", (), {})()
        watch.established = loop.create_future()
        watch.established.set_result(None)
        watch.completed = loop.create_future()
        watch.cancel = lambda: watch.completed.cancel()
        watch.arm = lambda: None
        return watch

    if reach_restart:
        watch_patch = patch(
            "custom_components.enigma2_mqtt.installer._async_watch_restart", established_watch
        )
    else:
        watch_patch = patch.object(FakeSession, "close", FakeSession.close)

    with (
        watch_patch,
        patch("custom_components.enigma2_mqtt.installer.load_bundled_plugin", return_value=bundle),
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch.object(FakeSession, "run", run or default_run),
    ):
        with pytest.raises(InstallerError) as raised:
            await async_install(hass, request, _connector=receiver.connect)
    return raised.value.code


def test_the_manifest_covers_the_real_bundle_and_nothing_else() -> None:
    """The file list is built from the bundle Home Assistant actually ships.

    Everywhere else this function is patched out, so the one thing never checked was
    whether it can read the artifact in the repository at all. It also must not carry
    the package's own opkg metadata: `control.tar.gz` is a sibling member of the same
    archive, and verifying receiver files against control data would fail every install.
    """
    bundle = load_bundled_plugin()
    lines = _installed_hash_manifest(bundle.path.read_bytes()).decode().splitlines()

    assert 20 <= len(lines) <= 200
    assert lines == sorted(lines)
    paths = [line.split("  ", 1)[1] for line in lines]
    assert all(path.startswith("/usr/lib/enigma2/python/Plugins/Extensions/") for path in paths)
    assert not [path for path in paths if "control" in path.lower()]
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)


def test_a_truncated_bundle_is_refused_rather_than_partly_verified() -> None:
    """An archive member shorter than its own header is not a bundle."""
    bundle = load_bundled_plugin()
    truncated = bundle.path.read_bytes()[: 8 + 60 + 4]

    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(truncated)

    assert raised.value.code is InstallerErrorCode.BUNDLE_INVALID


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (b"not an archive at all", InstallerErrorCode.BUNDLE_INVALID),
        (b"!<arch>\n", InstallerErrorCode.BUNDLE_INVALID),
    ],
)
def test_a_bundle_that_is_not_an_ipk_is_refused(value: bytes, code: InstallerErrorCode) -> None:
    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(value)

    assert raised.value.code is code


async def test_a_receiver_running_a_newer_plugin_is_left_alone(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """Downgrading somebody's box because this integration is older is not an update."""
    receiver = FakeReceiver(installed_version="9.9.9")

    code = await _install(hass, install_request, receiver, _bundle(tmp_path))

    assert code is InstallerErrorCode.NEWER_INSTALLED
    assert receiver.installed is False
    assert receiver.backup_exists is False


async def test_an_upload_that_arrives_corrupted_is_never_installed(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """The digest is re-read from the receiver, not assumed from the transfer."""
    receiver = FakeReceiver()
    bundle = _bundle(tmp_path)
    original = FakeSession.run

    async def wrong_digest(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, "b" * 64 + "\n")
        return await original(self, command, **kwargs)

    code = await _install(hass, install_request, receiver, bundle, run=wrong_digest)

    assert code is InstallerErrorCode.UPLOAD_FAILED
    assert receiver.installed is False


async def test_a_receiver_without_room_is_refused_before_anything_is_written(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """A full box is the one where a half-written plugin does the most damage."""
    receiver = FakeReceiver(free_bytes=1024)

    code = await _install(hass, install_request, receiver, _bundle(tmp_path))

    assert code is InstallerErrorCode.NO_SPACE
    assert receiver.backup_exists is False


async def test_a_receiver_on_an_old_python_is_refused(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """The plugin needs 3.9; installing it on less would break the receiver's GUI."""
    receiver = FakeReceiver(python_version="3.8.2")

    code = await _install(hass, install_request, receiver, _bundle(tmp_path))

    assert code is InstallerErrorCode.UNSUPPORTED_PYTHON
    assert receiver.backup_exists is False


async def test_provisioning_a_box_already_bound_to_another_node_is_refused(
    credentials: SshCredentials,
) -> None:
    """Re-provisioning would silently steal a receiver from another Home Assistant."""
    receiver = FakeReceiver(node_id="another_receiver", base_topic="enigma2")
    request = InstallRequest(
        credentials,
        Provisioning(
            broker_host="192.0.2.10",
            broker_port=1883,
            broker_username="vuuno4kse_005301",
            broker_password="not-a-real-broker-password",
            node_id="vuuno4kse_005301",
        ),
    )

    with pytest.raises(InstallerError) as raised:
        await _async_validate_receiver_identity(FakeSession(receiver), "/tmp/helper.py", request)

    assert raised.value.code is InstallerErrorCode.IDENTITY_MISMATCH


async def test_a_receiver_holding_the_durable_lock_is_busy(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """Two installs on one receiver would interleave two rollbacks."""
    receiver = FakeReceiver(fail_on=" claim ")

    code = await _install(hass, install_request, receiver, _bundle(tmp_path))

    assert code is InstallerErrorCode.BUSY
    assert receiver.backup_exists is False
    assert receiver.installed is False


async def test_a_failed_rollback_is_reported_as_such_and_names_the_backup(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the recovery itself fails there is nothing left to do but say where to look.

    Two things are said, and both were missing. The **cause** — the log named a failed
    rollback and nothing about which step failed or why, leaving the receiver as the only
    place to find out. And the **lock**: the restore failing is not a reason to hold the
    transaction lock, because the next attempt takes its own snapshot before it touches
    anything, while a lock nobody holds refuses every attempt until it goes stale.
    """
    receiver = FakeReceiver()
    bundle = _bundle(tmp_path)
    original = FakeSession.run

    async def failing(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, bundle.sha256 + "\n")
        if "opkg install" in command:
            self.receiver.commands.append(command)
            return CommandResult(1, "", "no space left on device")
        if " restore " in command:
            self.receiver.commands.append(command)
            return CommandResult(1, "", "restore failed too")
        return await original(self, command, **kwargs)

    code = await _install(hass, install_request, receiver, bundle, run=failing)

    assert code is InstallerErrorCode.ROLLBACK_FAILED
    assert "/home/root/mqttbridge-backups/ha-installer-" in caplog.text
    assert any(" release " in command for command in receiver.commands)
    assert "Installer transaction lock remains" not in caplog.text
    reported = [record for record in caplog.records if "Installer rollback ended" in record.message]
    assert len(reported) == 1
    assert reported[0].exc_info is not None
    assert "rollback_failed" in caplog.text


async def test_a_lock_that_cannot_be_released_is_reported_not_forgotten(
    hass: HomeAssistant,
    install_request: InstallRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lock left behind wedges every later install, so it must reach the log.

    With the reason it could not be released: the message says which receiver is wedged,
    and only the chained cause says what the receiver answered when asked to let go.
    """
    receiver = FakeReceiver()
    bundle = _bundle(tmp_path)
    original = FakeSession.run

    async def failing(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, bundle.sha256 + "\n")
        # Fail after the lock is claimed but before the backup exists, so the cleanup
        # path is the bare lock release rather than a rollback.
        if " snapshot " in command:
            self.receiver.commands.append(command)
            return CommandResult(1, "", "snapshot failed")
        if " release " in command:
            self.receiver.commands.append(command)
            return CommandResult(1, "", "release failed")
        return await original(self, command, **kwargs)

    code = await _install(hass, install_request, receiver, bundle, run=failing)

    assert code is InstallerErrorCode.ROLLBACK_FAILED
    assert "Installer transaction lock remains on" in caplog.text
    assert receiver.backup_exists is False
    reported = [
        record
        for record in caplog.records
        if "Installer transaction lock remains on" in record.message and record.exc_info is not None
    ]
    assert reported, "the lock message does not carry why the release failed"


async def test_the_uploaded_helper_is_removed_once_the_lock_is_released(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """The helper outlives the restore that needs it, and nothing longer than that."""
    receiver = FakeReceiver()
    bundle = _bundle(tmp_path)
    original = FakeSession.run

    async def failing(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, bundle.sha256 + "\n")
        if "opkg install" in command:
            self.receiver.commands.append(command)
            self.receiver.installed = True
            return CommandResult(1, "", "install failed")
        return await original(self, command, **kwargs)

    code = await _install(hass, install_request, receiver, bundle, run=failing)

    assert code is InstallerErrorCode.INSTALL_FAILED
    assert receiver.rolled_back is True
    assert receiver.helper_removed is True


async def test_provisioning_is_written_through_an_unguessable_name_that_is_not_a_symlink(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """The temporary file holds the broker password.

    A fixed `.ha-new` beside the real file is a name anybody with shell access can
    pre-create as a symlink, and `cat >` follows one — so the password would land
    wherever the link pointed. The name carries the transaction's nonce, the shell
    refuses to write to it if anything is already there, and the rename is checked too.
    """
    receiver = FakeReceiver()
    bundle = _bundle(tmp_path)
    original = FakeSession.run

    async def stop_after_provisioning(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, bundle.sha256 + "\n")
        if command.startswith("set -eu; if [ -L ") and " mv " in command:
            self.receiver.commands.append(command)
            return CommandResult(1, "", "stop here")
        return await original(self, command, **kwargs)

    code = await _install(hass, install_request, receiver, bundle, run=stop_after_provisioning)

    assert code is InstallerErrorCode.PROVISION_FAILED
    written = [
        command
        for command in receiver.commands
        if "/etc/enigma2/mqttbridge.json.ha-" in command and "cat >" in command
    ]
    assert len(written) == 1
    assert ".ha-new" not in written[0]
    temporary = written[0].rsplit("cat > ", 1)[1].strip()
    nonce = temporary.rsplit(".ha-", 1)[1]
    assert len(nonce) >= 8
    assert f"[ -e {temporary} ]" in written[0]
    assert f"[ -L {temporary} ]" in written[0]
    renamed = next(
        command for command in receiver.commands if command.startswith("set -eu; if [ -L ")
    )
    assert renamed.endswith(f"mv {temporary} /etc/enigma2/mqttbridge.json")
    # The nonce is the transaction's, so the rollback deletes the same file it wrote.
    assert any(nonce in command and command.startswith("rm -f ") for command in receiver.commands)


async def test_the_provisioning_temp_is_removed_even_when_enigma_will_not_stop(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """That file is the broker password in cleartext, sitting on the receiver's flash.

    It used to be deleted by the rollback's first command *after* Enigma had been
    stopped, so a receiver that would not stop — which is a receiver in trouble, exactly
    when a rollback runs — kept it. Deleting it needs nothing stopped, so it goes first
    and outside every condition.
    """
    receiver = FakeReceiver()
    bundle = _bundle(tmp_path)
    original = FakeSession.run

    async def stop_fails(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, bundle.sha256 + "\n")
        if command.startswith("init 4"):
            self.receiver.commands.append(command)
            raise OSError("the receiver stopped answering")
        if 'trap "init 3"' in command:
            self.receiver.commands.append(command)
            return CommandResult(1, "", "restart failed")
        return await original(self, command, **kwargs)

    code = await _install(
        hass, install_request, receiver, bundle, run=stop_fails, reach_restart=True
    )

    assert code is InstallerErrorCode.ROLLBACK_FAILED
    written = next(
        command
        for command in receiver.commands
        if "/etc/enigma2/mqttbridge.json.ha-" in command and "cat >" in command
    )
    temporary = written.rsplit("cat > ", 1)[1].strip()
    removals = [
        command
        for command in receiver.commands
        if command.startswith("rm -f ") and temporary in command
    ]
    assert removals, "the temporary provisioning file was left on the receiver"
    # It went before the attempt to stop Enigma, not after it.
    assert receiver.commands.index(removals[0]) < next(
        index for index, command in enumerate(receiver.commands) if command.startswith("init 4")
    )


async def test_two_transactions_against_one_receiver_do_not_interleave(
    hass: HomeAssistant, install_request: InstallRequest, tmp_path: Path
) -> None:
    """The in-process guard refuses the second before it opens a connection."""
    receiver = FakeReceiver()
    bundle = _bundle(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = FakeSession.run

    async def slow(self: FakeSession, command: str, **kwargs: Any):
        if "sha256sum" in command:
            return CommandResult(0, bundle.sha256 + "\n")
        if " snapshot " in command:
            entered.set()
            await release.wait()
            return CommandResult(1, "", "stopped")
        return await original(self, command, **kwargs)

    with (
        patch("custom_components.enigma2_mqtt.installer.load_bundled_plugin", return_value=bundle),
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch.object(FakeSession, "run", slow),
    ):
        first = asyncio.create_task(
            async_install(hass, install_request, _connector=receiver.connect)
        )
        await entered.wait()
        with pytest.raises(InstallerError) as raised:
            await async_install(hass, install_request, _connector=receiver.connect)
        assert raised.value.code is InstallerErrorCode.BUSY
        release.set()
        with pytest.raises(InstallerError):
            await first


def test_the_package_name_is_the_one_the_bundle_ships() -> None:
    """A rename on either side would make every opkg command address nothing."""
    assert load_bundled_plugin().path.name.startswith(PACKAGE + "_")

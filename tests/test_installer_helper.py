"""The exact receiver-side rollback helper against a temporary filesystem."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import (
    PACKAGE,
    STALE_LOCK_SECONDS,
    boot_id,
    claim_transaction,
    read_identity,
    release_transaction,
    restore,
    snapshot,
    uptime,
    verify_manifest,
)


def _write(root: Path, relative: str, value: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    return target


def _receiver_tree(root: Path) -> None:
    _write(
        root,
        "usr/lib/opkg/status",
        f"Package: unrelated\nVersion: 7\n\nPackage: {PACKAGE}\nVersion: 0.0.9\nDescription: old\n",
    )
    _write(root, f"usr/lib/opkg/info/{PACKAGE}.list", "/old/plugin.py\n")
    _write(root, "usr/lib/opkg/info/unrelated.list", "/keep\n")
    _write(
        root,
        "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py",
        "old plugin\n",
    )
    _write(
        root,
        "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/WebChilds/External/MQTTBridge.py",
        "old shim\n",
    )
    _write(
        root,
        "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/"
        "WebChilds/External/__pycache__/MQTTBridge.cpython-312.pyc",
        "old bytecode\n",
    )
    _write(
        root,
        "etc/enigma2/settings",
        "config.misc.keep=true\n"
        "config.plugins.mqttbridge.host=old-broker\n"
        "config.plugins.mqttbridge.port=1883\n",
    )
    _write(root, "etc/enigma2/mqttbridge.json", '{"old":true}\n')


def test_restore_preserves_unrelated_package_and_settings(tmp_path: Path) -> None:
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver_tree(root)
    metadata = snapshot(root, backup)
    assert metadata == {
        "schema": 2,
        "plugin": True,
        "package_status": True,
        "opkg_info": True,
        "provisioning": True,
        "settings": True,
        "webif_shim": True,
        "webif_cache": True,
    }

    _write(
        root,
        "usr/lib/opkg/status",
        "Package: unrelated\nVersion: 8\n\n"
        "Package: newly-installed-unrelated\nVersion: 1\n\n"
        f"Package: {PACKAGE}\nVersion: 0.1.0\n",
    )
    _write(root, f"usr/lib/opkg/info/{PACKAGE}.list", "/new/plugin.py\n")
    _write(
        root,
        "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py",
        "new plugin\n",
    )
    _write(
        root,
        "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/WebChilds/External/MQTTBridge.py",
        "new shim\n",
    )
    old_cache = (
        root / "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/"
        "WebChilds/External/__pycache__/MQTTBridge.cpython-312.pyc"
    )
    old_cache.write_text("new bytecode\n", encoding="utf-8")
    _write(
        root,
        "etc/enigma2/settings",
        "config.misc.keep=changed-concurrently\nconfig.plugins.mqttbridge.host=new-broker\n",
    )
    _write(root, "etc/enigma2/mqttbridge.json", '{"new":true}\n')

    restore(root, backup, restore_provisioning=True, restore_settings=True)

    status = (root / "usr/lib/opkg/status").read_text()
    assert "Package: unrelated\nVersion: 8" in status
    assert "Package: newly-installed-unrelated\nVersion: 1" in status
    assert f"Package: {PACKAGE}\nVersion: 0.0.9" in status
    assert "Version: 0.1.0" not in status
    assert (root / f"usr/lib/opkg/info/{PACKAGE}.list").read_text() == "/old/plugin.py\n"
    assert (root / "usr/lib/opkg/info/unrelated.list").read_text() == "/keep\n"
    assert (
        root / "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py"
    ).read_text() == "old plugin\n"
    assert (
        root / "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/"
        "WebChilds/External/MQTTBridge.py"
    ).read_text() == "old shim\n"
    assert old_cache.read_text(encoding="utf-8") == "old bytecode\n"
    settings = (root / "etc/enigma2/settings").read_text()
    assert "config.misc.keep=changed-concurrently" in settings
    assert "config.plugins.mqttbridge.host=old-broker" in settings
    assert "config.plugins.mqttbridge.host=new-broker" not in settings
    assert (root / "etc/enigma2/mqttbridge.json").read_text() == '{"old":true}\n'


def test_restore_removes_only_new_openwebif_shim(tmp_path: Path) -> None:
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver_tree(root)
    shim = (
        root / "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/"
        "WebChilds/External/MQTTBridge.py"
    )
    shim.unlink()
    for bytecode in shim.parent.joinpath("__pycache__").glob("MQTTBridge.*.pyc"):
        bytecode.unlink()
    sibling = _write(root, str(shim.parent.relative_to(root) / "OtherPlugin.py"), "keep\n")

    metadata = snapshot(root, backup)
    assert metadata["webif_shim"] is False
    shim.write_text("new package shim\n", encoding="utf-8")
    generated = _write(
        root,
        str(shim.parent.relative_to(root) / "__pycache__/MQTTBridge.cpython-312.pyc"),
        "new bytecode\n",
    )

    restore(root, backup, restore_provisioning=True, restore_settings=True)

    assert not shim.exists()
    assert not generated.exists()
    assert sibling.read_text(encoding="utf-8") == "keep\n"


def test_snapshot_refuses_symlink_openwebif_shim(tmp_path: Path) -> None:
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver_tree(root)
    shim = (
        root / "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/"
        "WebChilds/External/MQTTBridge.py"
    )
    target = tmp_path / "outside-shim"
    target.write_text("outside\n", encoding="utf-8")
    shim.unlink()
    shim.symlink_to(target)

    with pytest.raises(ValueError, match="OpenWebif shim"):
        snapshot(root, backup)


def test_restore_refuses_unsafe_live_webif_cache_before_changes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver_tree(root)
    snapshot(root, backup)
    status = root / "usr/lib/opkg/status"
    status.write_text("Package: concurrent\nVersion: 1\n", encoding="utf-8")
    before = status.read_bytes()
    cache = (
        root / "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/"
        "WebChilds/External/__pycache__"
    )
    shutil_target = tmp_path / "outside-cache"
    shutil_target.mkdir()
    for child in cache.iterdir():
        child.unlink()
    cache.rmdir()
    cache.symlink_to(shutil_target, target_is_directory=True)

    with pytest.raises(ValueError, match="bytecode directory"):
        restore(root, backup, restore_provisioning=True, restore_settings=True)

    assert status.read_bytes() == before


def test_incomplete_snapshot_is_rejected_before_live_files_change(tmp_path: Path) -> None:
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver_tree(root)
    snapshot(root, backup)
    (backup / "plugin").rename(backup / "plugin-missing")
    before = (root / "usr/lib/opkg/status").read_bytes()

    with pytest.raises(ValueError, match="incomplete"):
        restore(root, backup, restore_provisioning=True, restore_settings=True)

    assert (root / "usr/lib/opkg/status").read_bytes() == before


def test_snapshot_refuses_symlink_plugin_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver_tree(root)
    plugin = root / "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
    target = tmp_path / "elsewhere"
    plugin.rename(target)
    plugin.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        snapshot(root, backup)


def test_restore_refuses_live_symlink_before_metadata_changes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver_tree(root)
    snapshot(root, backup)
    status = root / "usr/lib/opkg/status"
    before = status.read_bytes()
    plugin = root / "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
    target = tmp_path / "replacement"
    plugin.rename(target)
    plugin.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="live plugin"):
        restore(root, backup, restore_provisioning=True, restore_settings=True)

    assert status.read_bytes() == before


def test_read_identity_exposes_only_binding_fields(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write(
        root,
        "etc/enigma2/settings",
        "config.plugins.mqttbridge.host=broker.example\n"
        "config.plugins.mqttbridge.password=not-returned\n"
        "config.plugins.mqttbridge.node_id=vuuno4kse_005301\n"
        "config.plugins.mqttbridge.base_topic=enigma2/rooms\n"
        "config.plugins.mqttbridge.enabled=true\n"
        "config.plugins.mqttbridge.ha_mode=integration\n",
    )

    assert read_identity(root) == {
        "node_id": "vuuno4kse_005301",
        "base_topic": "enigma2/rooms",
        "enabled": True,
        "ha_mode": "integration",
    }


def test_verify_manifest_checks_actual_installed_bytes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    installed = _write(root, "usr/lib/enigma2/plugin.py", "exact bytes")
    digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest"
    manifest.write_text(f"{digest}  /usr/lib/enigma2/plugin.py\n", encoding="ascii")

    verify_manifest(root, manifest)
    installed.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        verify_manifest(root, manifest)


def test_snapshot_metadata_must_be_typed_and_complete(tmp_path: Path) -> None:
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver_tree(root)
    snapshot(root, backup)
    (backup / "snapshot.json").write_text(json.dumps({"plugin": "yes"}), encoding="utf-8")

    with pytest.raises(ValueError, match="metadata"):
        restore(root, backup, restore_provisioning=True, restore_settings=True)


def test_verify_manifest_ignores_files_the_image_added_beside_the_plugin(
    tmp_path: Path,
) -> None:
    """The receiver byte-compiles what it imports, and that is not a tampered install.

    The manifest lists the `.py` files the IPK ships. OpenViX writes `.pyc` files next
    to them the first time the plugin loads, so an install that verified only by "these
    files and nothing else" would fail on every healthy receiver. Verification proves
    the listed bytes are the shipped bytes; it deliberately says nothing about extras.
    """
    root = tmp_path / "root"
    installed = _write(root, "usr/lib/enigma2/plugin.py", "exact bytes")
    digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest"
    manifest.write_text(f"{digest}  /usr/lib/enigma2/plugin.py\n", encoding="ascii")
    _write(root, "usr/lib/enigma2/__pycache__/plugin.cpython-312.pyc", "bytecode")
    _write(root, "usr/lib/enigma2/plugin.pyc", "bytecode")

    verify_manifest(root, manifest)


def _owner(lock_dir: Path) -> dict:
    return json.loads((lock_dir / "owner.json").read_text(encoding="ascii"))


def test_the_durable_lock_refuses_a_second_live_transaction(tmp_path: Path) -> None:
    """Two installs on one receiver would interleave two rollbacks."""
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)

    with pytest.raises(FileExistsError):
        claim_transaction(lock_dir)

    release_transaction(lock_dir)
    assert not lock_dir.exists()


def test_a_lock_from_before_the_receiver_rebooted_is_reclaimed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A box pulled out of the wall mid-install must not be locked out for ever.

    The lock is a directory on flash, so it survives the reboot that killed the process
    holding it. The boot id says so without having to guess from a timestamp.
    """
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    stale = {**_owner(lock_dir), "boot_id": "00000000-0000-0000-0000-000000000000"}
    (lock_dir / "owner.json").write_text(json.dumps(stale), encoding="ascii")

    claim_transaction(lock_dir)

    assert _owner(lock_dir)["boot_id"] == boot_id()
    assert "reclaiming stale installer lock" in capsys.readouterr().err


def test_a_lock_older_than_any_plausible_install_is_reclaimed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The boot id only helps across a reboot; a crashed process leaves the same wedge."""
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    stale = {
        **_owner(lock_dir),
        "started": int(time.time()) - STALE_LOCK_SECONDS - 1,
        "uptime": (uptime() or 0.0) - STALE_LOCK_SECONDS - 1,
    }
    (lock_dir / "owner.json").write_text(json.dumps(stale), encoding="ascii")

    claim_transaction(lock_dir)

    assert _owner(lock_dir)["started"] > stale["started"]
    assert "reclaiming stale installer lock" in capsys.readouterr().err


def test_a_lock_that_is_merely_slow_is_still_respected(tmp_path: Path) -> None:
    """Reclaiming an install that is only taking its time would start a second one."""
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    recent = {**_owner(lock_dir), "started": int(time.time()) - STALE_LOCK_SECONDS + 60}
    (lock_dir / "owner.json").write_text(json.dumps(recent), encoding="ascii")

    with pytest.raises(FileExistsError):
        claim_transaction(lock_dir)


@pytest.mark.parametrize("content", ["", "not json", json.dumps(["a list"])])
def test_a_lock_with_an_unreadable_owner_record_is_reclaimed_once_it_is_old(
    tmp_path: Path, content: str
) -> None:
    """A claim that died mid-write is a wedge with nobody behind it.

    Age has to come into it, because the same unreadable directory is what a claim that
    started a millisecond ago looks like.
    """
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    (lock_dir / "owner.json").write_text(content, encoding="ascii")
    old = time.time() - STALE_LOCK_SECONDS - 60
    os.utime(lock_dir, (old, old))

    claim_transaction(lock_dir)

    assert _owner(lock_dir)["boot_id"] == boot_id()


def test_an_owner_record_that_is_not_an_object_is_reclaimed(tmp_path: Path) -> None:
    """Valid JSON that is not a record cannot have been written by a claim."""
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    (lock_dir / "owner.json").write_text(json.dumps({}), encoding="ascii")

    claim_transaction(lock_dir)

    assert _owner(lock_dir)["boot_id"] == boot_id()


def test_releasing_a_lock_without_an_owner_is_refused(tmp_path: Path) -> None:
    """`release` removes an installer lock, not any directory it is pointed at."""
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()

    with pytest.raises(ValueError, match="owner"):
        release_transaction(lock_dir)


def test_the_boot_id_is_empty_rather_than_fatal_on_a_kernel_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Then the age bound is the only staleness rule, which is still better than none."""
    monkeypatch.setattr(installer_helper, "BOOT_ID_PATH", tmp_path / "nothing-here")

    assert boot_id() == ""
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    assert _owner(lock_dir)["boot_id"] == ""
    with pytest.raises(FileExistsError):
        claim_transaction(lock_dir)

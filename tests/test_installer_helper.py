"""The exact receiver-side rollback helper against a temporary filesystem."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from custom_components.enigma2_mqtt.installer_helper import (
    PACKAGE,
    read_identity,
    restore,
    snapshot,
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

"""The helper as the receiver actually runs it: one file, one argv, one exit code.

Every other test in this repository calls the helper's functions. The receiver does not:
the installer uploads this module and runs `python3 <helper> <operation> …` over SSH, so
the command line is the real interface and an argument the installer spells one way and
the parser reads another is a rollback that does nothing on a box nobody is watching.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from custom_components.enigma2_mqtt.installer_helper import PACKAGE, main


def _write(root: Path, relative: str, value: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    return target


def _receiver(root: Path) -> None:
    """The smallest tree the helper recognises as a receiver with the plugin on it."""
    _write(root, "usr/lib/opkg/status", f"Package: {PACKAGE}\nVersion: 0.0.9\n")
    _write(root, f"usr/lib/opkg/info/{PACKAGE}.list", "/plugin.py\n")
    _write(root, "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py", "old\n")
    _write(
        root,
        "etc/enigma2/settings",
        "config.plugins.mqttbridge.node_id=vuuno4kse_005301\n"
        "config.plugins.mqttbridge.base_topic=/enigma2/\n"
        "config.plugins.mqttbridge.enabled=True\n"
        "config.plugins.mqttbridge.ha_mode=integration\n",
    )
    _write(root, "etc/enigma2/mqttbridge.json", '{"old":true}\n')


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["installer_helper.py", *argv])
    return main()


def test_identity_is_printed_as_the_json_the_installer_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The installer refuses to touch a receiver whose identity it cannot read."""
    root = tmp_path / "root"
    _receiver(root)

    assert _run(monkeypatch, "identity", "--root", str(root)) == 0

    identity = json.loads(capsys.readouterr().out)
    assert identity == {
        "node_id": "vuuno4kse_005301",
        # The receiver stores it with the slashes it was typed with; the installer
        # compares it against a topic that has none.
        "base_topic": "enigma2",
        "enabled": True,
        "ha_mode": "integration",
        "friendly_name": None,
    }


@pytest.mark.parametrize(
    "operation", ["snapshot", "restore", "verify", "claim", "release", "prune"]
)
def test_identity_needs_no_path_and_the_others_refuse_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A missing path would otherwise be a traceback on a box with no console."""
    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, operation, "--root", str(tmp_path))

    assert raised.value.code == 2


def test_snapshot_then_restore_puts_the_receiver_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two halves of the rollback, run the way the installer runs them."""
    root = tmp_path / "root"
    backup = tmp_path / "backup"
    _receiver(root)

    assert _run(monkeypatch, "snapshot", str(backup), "--root", str(root)) == 0

    _write(root, "usr/lib/opkg/status", f"Package: {PACKAGE}\nVersion: 0.1.0\n")
    _write(root, "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py", "new\n")
    _write(root, "etc/enigma2/mqttbridge.json", '{"new":true}\n')

    assert (
        _run(
            monkeypatch,
            "restore",
            str(backup),
            "--root",
            str(root),
            "--provisioning",
            "--settings",
        )
        == 0
    )

    status = (root / "usr/lib/opkg/status").read_text(encoding="utf-8")
    assert "Version: 0.0.9" in status
    assert (
        root / "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py"
    ).read_text(encoding="utf-8") == "old\n"
    assert (root / "etc/enigma2/mqttbridge.json").read_text(encoding="utf-8") == '{"old":true}\n'


def test_verify_accepts_the_files_opkg_installed_and_refuses_a_changed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check that the package on the box is the package that was uploaded."""
    root = tmp_path / "root"
    relative = "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py"
    installed = _write(root, relative, "x\n")
    manifest = tmp_path / "manifest"
    digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    manifest.write_text(f"{digest}  /{relative}\n", encoding="ascii")

    assert _run(monkeypatch, "verify", str(manifest), "--root", str(root)) == 0

    installed.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        _run(monkeypatch, "verify", str(manifest), "--root", str(root))


def test_prune_leaves_the_two_newest_snapshots_and_says_what_it_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The last thing a successful install runs before deleting the helper itself."""
    backups = tmp_path / "mqttbridge-backups"
    backups.mkdir()
    for index, nonce in enumerate(("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc")):
        directory = backups / f"ha-installer-{nonce}"
        directory.mkdir()
        os.utime(directory, (1_700_000_000 + index, 1_700_000_000 + index))

    assert (
        _run(
            monkeypatch,
            "prune",
            str(backups),
            "--keep-name",
            "ha-installer-aaaaaaaaaaaa",
        )
        == 0
    )

    # The one named on the command line stays whatever its timestamp says, and it
    # takes one of the two places rather than being kept beside them.
    assert sorted(child.name for child in backups.iterdir()) == [
        "ha-installer-aaaaaaaaaaaa",
        "ha-installer-cccccccccccc",
    ]
    assert "ha-installer-bbbbbbbbbbbb" in capsys.readouterr().err


def test_claim_and_release_are_the_lock_the_installer_serialises_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two installers on one receiver is the race the lock exists for."""
    lock = tmp_path / "backups" / ".ha-installer.lock"
    lock.parent.mkdir()

    assert _run(monkeypatch, "claim", str(lock)) == 0
    assert (lock / "owner.json").is_file()

    with pytest.raises(FileExistsError):
        _run(monkeypatch, "claim", str(lock))

    assert _run(monkeypatch, "release", str(lock)) == 0
    assert not lock.exists()

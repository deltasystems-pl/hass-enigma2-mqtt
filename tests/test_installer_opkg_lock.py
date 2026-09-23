"""The helper excludes opkg itself, by taking the lock opkg actually takes.

opkg on OpenViX 6.6 (opkg 0.6.3) creates `/run/opkg.lock`, takes a POSIX record lock on
it with `lockf(F_TLOCK)`, gives up at once with "Could not lock" when that fails, and
deletes the file when it is done. The helper used to lock `/var/lock/opkg.lock`, a file
this opkg never opens, so the image's daily update check could rewrite the database in
the middle of a snapshot or a restore.

A record lock belongs to a process, so opkg is played here by a second process making
the same two calls opkg makes. A lock tested from inside the process holding it would
always be granted and would prove nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import PACKAGE, restore, snapshot

# What opkg's `opkg_lock()` does: create the lock directory and the file, then one
# non-blocking `lockf` over the whole file. With `hold`, it keeps the lock until its
# stdin closes and then lets go as opkg does — unlock, close, delete.
FAKE_OPKG = r"""
import fcntl, os, sys
path, hold = sys.argv[1], sys.argv[2] == "hold"
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("busy", flush=True)
    sys.exit(1)
print("locked", flush=True)
if hold:
    sys.stdin.read()
fcntl.lockf(fd, fcntl.LOCK_UN)
os.close(fd)
os.unlink(path)
"""


def opkg_can_lock(path: Path) -> bool:
    """Would an opkg run starting now get its lock?"""
    result = subprocess.run(
        [sys.executable, "-c", FAKE_OPKG, str(path), "once"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.stdout.strip() in ("locked", "busy"), result.stderr
    return result.stdout.strip() == "locked"


@contextmanager
def opkg_running(path: Path) -> Iterator[None]:
    """An opkg run that holds its lock for the duration of the block."""
    process = subprocess.Popen(
        [sys.executable, "-c", FAKE_OPKG, str(path), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        yield
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=30)


def _write(root: Path, relative: str, value: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    return target


def _receiver(root: Path) -> None:
    """The parts of an OpenViX 6.6 receiver a snapshot reads."""
    _write(root, "etc/opkg/opkg.conf", "option lists_dir /var/lib/opkg/lists\n")
    _write(root, "var/lib/opkg/status", f"Package: {PACKAGE}\nVersion: 0.2.0\n")
    _write(root, f"var/lib/opkg/info/{PACKAGE}.list", "/plugin.py\n")
    _write(root, "etc/enigma2/settings", "config.plugins.mqttbridge.host=broker\n")
    (root / "run").mkdir()
    (root / "var/lock").mkdir(parents=True)


def _while_the_database_is_read(monkeypatch: pytest.MonkeyPatch, probe) -> list:
    """Run `probe` at the point where the helper is inside its lock."""
    seen: list = []
    real = installer_helper.opkg_paths

    def reading_the_database(root: Path):
        seen.append(probe())
        return real(root)

    monkeypatch.setattr(installer_helper, "opkg_paths", reading_the_database)
    return seen


def test_an_opkg_run_cannot_start_while_a_snapshot_reads_its_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image's update check fails its lock instead of racing the snapshot."""
    root = tmp_path / "root"
    _receiver(root)
    seen = _while_the_database_is_read(
        monkeypatch, lambda: opkg_can_lock(root / "run/opkg.lock")
    )

    snapshot(root, tmp_path / "backup")

    assert seen == [False]
    # Let go as opkg does, so an opkg run afterwards is not refused by a leftover.
    assert not (root / "run/opkg.lock").exists()
    assert opkg_can_lock(root / "run/opkg.lock")


def test_an_opkg_run_cannot_start_while_a_restore_rewrites_its_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore is the half that writes opkg's status file and info directory."""
    root = tmp_path / "root"
    _receiver(root)
    backup = tmp_path / "backup"
    snapshot(root, backup)
    seen: list = []
    real_atomic_text = installer_helper._atomic_text

    def writing_the_status_file(path: Path, text: str) -> None:
        seen.append(opkg_can_lock(root / "run/opkg.lock"))
        real_atomic_text(path, text)

    monkeypatch.setattr(installer_helper, "_atomic_text", writing_the_status_file)

    restore(root, backup, restore_provisioning=False, restore_settings=False)

    assert seen == [False]


def test_a_snapshot_waits_for_a_running_opkg_and_then_refuses_touching_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opkg itself does not wait at all; the helper waits a bounded time and says why."""
    root = tmp_path / "root"
    _receiver(root)
    monkeypatch.setattr(installer_helper, "OPKG_LOCK_WAIT_SECONDS", 0.5, raising=False)
    backup = tmp_path / "backup"

    with opkg_running(root / "run/opkg.lock"), pytest.raises(TimeoutError, match="opkg is busy"):
        snapshot(root, backup)

    assert not (backup / "snapshot.json").exists()
    assert not (backup / "plugin-settings").exists()


def test_a_lock_the_configuration_names_is_the_one_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`option lock_file` moves opkg's lock, and then only that file excludes opkg."""
    root = tmp_path / "root"
    _receiver(root)
    _write(root, "etc/opkg/lock.conf", "option lock_file /var/run/custom/opkg.lock\n")
    seen = _while_the_database_is_read(
        monkeypatch,
        lambda: (
            opkg_can_lock(root / "var/run/custom/opkg.lock"),
            opkg_can_lock(root / "run/opkg.lock"),
        ),
    )

    snapshot(root, tmp_path / "backup")

    assert seen == [(False, True)]


def test_an_older_opkg_with_the_var_lock_default_is_still_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing on a receiver says which default its opkg was built with, so both hold."""
    root = tmp_path / "root"
    _receiver(root)
    seen = _while_the_database_is_read(
        monkeypatch, lambda: opkg_can_lock(root / "var/lock/opkg.lock")
    )

    snapshot(root, tmp_path / "backup")

    assert seen == [False]


def test_a_lock_on_a_file_opkg_has_just_deleted_is_taken_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opkg deletes its lock file when it finishes.

    A helper that opened the old file and was granted the lock just as opkg let go
    holds a lock on a name nobody can see; the next opkg creates a new file and locks
    it unopposed. This replays that interleaving: the first lock is granted on a file
    that is replaced in the same instant.
    """
    root = tmp_path / "root"
    _receiver(root)
    lock_path = root / "run/opkg.lock"
    replaced: list = []

    def granted_as_opkg_lets_go(descriptor: int, operation: int) -> None:
        fcntl.lockf(descriptor, operation)
        if (
            not replaced
            and lock_path.exists()
            and os.fstat(descriptor).st_ino == os.stat(lock_path).st_ino
        ):
            lock_path.unlink()
            lock_path.touch()
            replaced.append(True)

    monkeypatch.setattr(
        installer_helper,
        "fcntl",
        SimpleNamespace(
            lockf=granted_as_opkg_lets_go, LOCK_EX=fcntl.LOCK_EX, LOCK_NB=fcntl.LOCK_NB
        ),
    )
    seen = _while_the_database_is_read(monkeypatch, lambda: opkg_can_lock(lock_path))

    snapshot(root, tmp_path / "backup")

    assert replaced == [True]
    assert seen == [False]


def test_letting_go_never_deletes_a_lock_file_that_is_no_longer_ours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a file somebody else has since created would unlock them, not us."""
    root = tmp_path / "root"
    _receiver(root)
    lock_path = root / "run/opkg.lock"

    def replace_the_lock_file() -> None:
        lock_path.unlink()
        lock_path.write_text("someone else's\n", encoding="utf-8")

    _while_the_database_is_read(monkeypatch, replace_the_lock_file)

    snapshot(root, tmp_path / "backup")

    assert lock_path.read_text(encoding="utf-8") == "someone else's\n"

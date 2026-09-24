"""The helper excludes opkg itself, by taking the lock opkg actually takes.

opkg on OpenViX 6.6 (opkg 0.6.3) creates `/run/opkg.lock`, takes a POSIX record lock on
it with `lockf(F_TLOCK)`, gives up at once with "Could not lock" when that fails, and
lets go by unlocking, closing and then deleting the file. The helper used to lock
`/var/lock/opkg.lock`, a file this opkg never opens.

A record lock belongs to a process, so opkg is played here by a second process making
the same calls opkg makes. A lock tested from inside the process holding it would
always be granted and would prove nothing.

Every test here is bounded: a helper that blocked on the lock instead of polling it
would otherwise hang the suite rather than fail it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import (
    EXIT_OPKG_BUSY,
    EXIT_OPKG_LOCK_LOST,
    PACKAGE,
    OpkgBusyError,
    OpkgLockLostError,
    prune_snapshots,
    restore,
    snapshot,
)

pytestmark = pytest.mark.timeout(60)

HELPER = Path(installer_helper.__file__)

# What opkg's `opkg_lock()` and `opkg_unlock()` do: create the lock directory and the
# file, one non-blocking `lockf` over the whole file; then unlock, close, and delete the
# file if it is still there. With `hold`, it keeps the lock until its stdin closes; with a
# number of seconds, for that long.
FAKE_OPKG = r"""
import fcntl, os, sys, time
path, hold = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("busy", flush=True)
    sys.exit(1)
print("locked", flush=True)
if hold == "hold":
    sys.stdin.read()
elif hold != "once":
    time.sleep(float(hold))
fcntl.lockf(fd, fcntl.LOCK_UN)
os.close(fd)
if os.path.exists(path):
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
def opkg_running(path: Path, seconds: str = "hold") -> Iterator[subprocess.Popen]:
    """An opkg run that holds its lock for the block, or for `seconds`."""
    process = subprocess.Popen(
        [sys.executable, "-c", FAKE_OPKG, str(path), seconds],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        yield process
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


def _short_waits(monkeypatch: pytest.MonkeyPatch, snapshot_wait: float, restore_wait: float):
    monkeypatch.setattr(installer_helper, "OPKG_LOCK_WAIT_SNAPSHOT_SECONDS", snapshot_wait)
    monkeypatch.setattr(installer_helper, "OPKG_LOCK_WAIT_RESTORE_SECONDS", restore_wait)


def _lockf_calling(monkeypatch: pytest.MonkeyPatch, lockf) -> None:
    monkeypatch.setattr(
        installer_helper,
        "fcntl",
        SimpleNamespace(lockf=lockf, LOCK_EX=fcntl.LOCK_EX, LOCK_NB=fcntl.LOCK_NB),
    )


def test_an_opkg_run_cannot_start_while_a_snapshot_reads_its_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install from the receiver's menu fails its lock instead of racing the snapshot."""
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


def test_a_busy_snapshot_refuses_and_leaves_no_snapshot_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opkg itself does not wait at all; the helper waits a bounded time and says why.

    And the snapshot directory is not made until the lock is held. Every
    `ha-installer-*` directory counts as a snapshot to the pruning, so an empty one left
    by a refusal would push a real rollback point out of the two that are kept.
    """
    root = tmp_path / "root"
    _receiver(root)
    _short_waits(monkeypatch, 0.5, 30)
    backups = tmp_path / "backups"
    backups.mkdir()
    backup = backups / "ha-installer-000000000001"
    # Looked at while the helper is still waiting, which is when a prune running beside
    # it would count the directory - cleaning it up afterwards is not enough.
    during_the_wait: list = []
    real_take = installer_helper._take_opkg_lock

    def take(path: Path, deadline: float, wait: float) -> int:
        during_the_wait.append(backup.exists())
        return real_take(path, deadline, wait)

    monkeypatch.setattr(installer_helper, "_take_opkg_lock", take)

    with opkg_running(root / "run/opkg.lock"), pytest.raises(OpkgBusyError, match="opkg is busy"):
        snapshot(root, backup)

    assert during_the_wait == [False]
    assert not backup.exists()
    assert prune_snapshots(backups, keep_name="ha-installer-000000000002") == []


def test_a_busy_restore_refuses_before_it_touches_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore waits on its own, longer bound - and here it runs out."""
    root = tmp_path / "root"
    _receiver(root)
    backup = tmp_path / "backup"
    snapshot(root, backup)
    status = root / "var/lib/opkg/status"
    status.write_text(f"Package: {PACKAGE}\nVersion: 0.3.0\n", encoding="utf-8")
    _short_waits(monkeypatch, 30, 0.5)

    with opkg_running(root / "run/opkg.lock"), pytest.raises(OpkgBusyError):
        restore(root, backup, restore_provisioning=False, restore_settings=False)

    assert "0.3.0" in status.read_text(encoding="utf-8")


@pytest.mark.parametrize("operation", ["snapshot", "restore"])
def test_each_step_waits_on_its_own_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """The snapshot on the short one, the restore on the long one, and never the other's."""
    root = tmp_path / "root"
    _receiver(root)
    backup = tmp_path / "backup"
    snapshot(root, backup)
    short, long = 0.4, 30.0
    if operation == "snapshot":
        _short_waits(monkeypatch, short, long)
        step = lambda: snapshot(root, tmp_path / "second")  # noqa: E731
    else:
        _short_waits(monkeypatch, long, short)
        step = lambda: restore(root, backup, False, False)  # noqa: E731

    started = time.monotonic()
    with opkg_running(root / "run/opkg.lock"), pytest.raises(OpkgBusyError):
        step()

    assert time.monotonic() - started < 5


def test_the_bound_is_the_whole_wait_across_every_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two default lock files must not mean two waits end to end.

    `/run` is held for most of the bound and then let go; `/var/lock` is held
    throughout. A wait per file would spend the first stretch on `/run` and then a whole
    bound more on `/var/lock` - past the installer's command timeout on a real
    receiver. One deadline for both ends it on time.
    """
    root = tmp_path / "root"
    _receiver(root)
    bound = 2.0
    _short_waits(monkeypatch, bound, bound)

    started = time.monotonic()
    with (
        opkg_running(root / "var/lock/opkg.lock"),
        opkg_running(root / "run/opkg.lock", seconds="1.2"),
        pytest.raises(OpkgBusyError),
    ):
        snapshot(root, tmp_path / "backup")
    elapsed = time.monotonic() - started

    assert bound <= elapsed < bound + 0.8


def test_the_helper_exits_with_its_own_status_when_opkg_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """That status is how the installer tells „try again in a minute" from a failure."""
    root = tmp_path / "root"
    _receiver(root)
    _short_waits(monkeypatch, 0.3, 0.3)
    monkeypatch.setattr(
        sys, "argv", ["installer_helper.py", "--root", str(root), "snapshot", str(tmp_path / "b")]
    )

    with opkg_running(root / "run/opkg.lock"):
        status = installer_helper.main()

    assert status == EXIT_OPKG_BUSY
    assert "opkg is busy" in capsys.readouterr().err


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


def test_a_lock_on_a_file_replaced_as_it_was_granted_is_taken_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is granted on a file that another opkg replaces in the same instant."""
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

    _lockf_calling(monkeypatch, granted_as_opkg_lets_go)
    seen = _while_the_database_is_read(monkeypatch, lambda: opkg_can_lock(lock_path))

    snapshot(root, tmp_path / "backup")

    assert replaced == [True]
    assert seen == [False]


def test_a_lock_on_a_file_deleted_and_not_recreated_is_taken_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opkg deleted its file after the grant, and nobody has made a new one yet."""
    root = tmp_path / "root"
    _receiver(root)
    lock_path = root / "run/opkg.lock"
    deleted: list = []

    def granted_then_deleted(descriptor: int, operation: int) -> None:
        fcntl.lockf(descriptor, operation)
        if not deleted and lock_path.exists():
            lock_path.unlink()
            deleted.append(True)

    _lockf_calling(monkeypatch, granted_then_deleted)
    seen = _while_the_database_is_read(monkeypatch, lambda: opkg_can_lock(lock_path))

    snapshot(root, tmp_path / "backup")

    assert deleted == [True]
    assert seen == [False]


def test_opkgs_own_order_of_letting_go_cannot_slip_past_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opkg unlocks, closes, and only then deletes the file.

    Granted in that gap, the helper's first look finds the name still on its file -
    and the departing opkg deletes it a moment later. The look is repeated after a
    pause longer than that gap, and the lock taken again on a fresh file.
    """
    root = tmp_path / "root"
    _receiver(root)
    lock_path = root / "run/opkg.lock"
    real_same = installer_helper._same_file
    fired: list = []

    def looked_then_opkg_deletes(first, second) -> bool:
        result = real_same(first, second)
        if result and not fired:
            lock_path.unlink()
            fired.append(True)
        return result

    monkeypatch.setattr(installer_helper, "_same_file", looked_then_opkg_deletes)
    seen = _while_the_database_is_read(monkeypatch, lambda: opkg_can_lock(lock_path))

    snapshot(root, tmp_path / "backup")

    assert fired == [True]
    assert seen == [False]


def test_a_lock_lost_inside_the_step_fails_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the name went anyway, an opkg run may have overlapped, and the step says so.

    The snapshot taken under it is not kept, and the file now on the name - somebody
    else's - is not deleted on the way out.
    """
    root = tmp_path / "root"
    _receiver(root)
    lock_path = root / "run/opkg.lock"

    def replace_the_lock_file() -> None:
        lock_path.unlink()
        lock_path.write_text("someone else's\n", encoding="utf-8")

    _while_the_database_is_read(monkeypatch, replace_the_lock_file)
    backup = tmp_path / "backup"

    with pytest.raises(OpkgLockLostError):
        snapshot(root, backup)

    assert not backup.exists()
    assert lock_path.read_text(encoding="utf-8") == "someone else's\n"


def test_a_lock_error_that_is_not_busy_is_raised_rather_than_waited_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ENOLCK` does not clear by waiting, and it is not „opkg is busy" either."""
    root = tmp_path / "root"
    _receiver(root)
    _short_waits(monkeypatch, 10, 10)

    def no_locks_here(descriptor: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    _lockf_calling(monkeypatch, no_locks_here)
    started = time.monotonic()

    with pytest.raises(OSError) as raised:
        snapshot(root, tmp_path / "backup")

    assert raised.value.errno == errno.ENOLCK
    assert not isinstance(raised.value, OpkgBusyError)
    assert time.monotonic() - started < 2
    assert not (tmp_path / "backup").exists()


def test_a_restore_that_lost_the_lock_exits_with_its_own_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The restore's files are written by then, which the installer has to be able to say."""
    root = tmp_path / "root"
    _receiver(root)
    backup = tmp_path / "backup"
    snapshot(root, backup)
    lock_path = root / "run/opkg.lock"
    real_atomic_text = installer_helper._atomic_text

    def written_while_the_lock_file_is_replaced(path: Path, text: str) -> None:
        real_atomic_text(path, text)
        lock_path.unlink()
        lock_path.write_text("someone else's\n", encoding="utf-8")

    monkeypatch.setattr(installer_helper, "_atomic_text", written_while_the_lock_file_is_replaced)
    monkeypatch.setattr(
        sys, "argv", ["installer_helper.py", "--root", str(root), "restore", str(backup)]
    )

    assert installer_helper.main() == EXIT_OPKG_LOCK_LOST
    assert "replaced while the helper held it" in capsys.readouterr().err

"""The helper's lock probe: is opkg's lock free right now, asked of the lock itself.

`opkg status` does not take opkg's lock (measured on opkg 0.6.3, OpenViX 6.6), so it
answers normally while another opkg run holds it. The uninstall's before-reads therefore
ask the lock: the helper takes every lock opkg could be using, non-blocking, and lets go
at once. It is fed to `python3 -` on standard input, which is how these tests run it too.
opkg is played by a second process, because a record lock belongs to a process.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import EXIT_OPKG_BUSY, probe_opkg_lock

from .test_installer_opkg_lock import _receiver, _write, opkg_can_lock, opkg_running

pytestmark = pytest.mark.timeout(60)

HELPER = Path(installer_helper.__file__)


def _probe_from_stdin(root: Path) -> subprocess.CompletedProcess:
    """Run the probe the way the uninstall does: the helper on stdin, `python3 -`."""
    return subprocess.run(
        [sys.executable, "-", "--root", str(root), "lock-probe"],
        input=HELPER.read_bytes(),
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_a_free_lock_is_free_and_left_as_it_was_found(tmp_path: Path) -> None:
    """Taken and let go the way opkg lets go: the lock files are gone afterwards."""
    root = tmp_path / "root"
    _receiver(root)

    result = _probe_from_stdin(root)

    assert result.returncode == 0, result.stderr
    assert not (root / "run/opkg.lock").exists()
    assert not (root / "var/lock/opkg.lock").exists()
    assert opkg_can_lock(root / "run/opkg.lock")


@pytest.mark.parametrize("lock", ["run/opkg.lock", "var/lock/opkg.lock"])
def test_a_held_lock_is_busy(tmp_path: Path, lock: str) -> None:
    """opkg 0.6.3's `/run` default, and the `/var/lock` default older releases use."""
    root = tmp_path / "root"
    _receiver(root)

    with opkg_running(root / lock):
        result = _probe_from_stdin(root)
        # The probe let go of whatever it took, and the holder still holds.
        assert not opkg_can_lock(root / lock)

    assert result.returncode == EXIT_OPKG_BUSY, result.stderr


def test_the_lock_the_configuration_names_is_the_one_probed(tmp_path: Path) -> None:
    """`option lock_file` moves opkg's lock, and then only that file says busy."""
    root = tmp_path / "root"
    _receiver(root)
    _write(root, "etc/opkg/lock.conf", "option lock_file /var/run/custom/opkg.lock\n")

    with opkg_running(root / "run/opkg.lock"):
        assert probe_opkg_lock(root) is True
    with opkg_running(root / "var/run/custom/opkg.lock"):
        assert probe_opkg_lock(root) is False


def test_the_probe_does_not_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One attempt: the retries belong to the caller, which can say what it is waiting for."""
    root = tmp_path / "root"
    _receiver(root)
    attempts: list[Path] = []
    real = installer_helper._take_opkg_lock

    def counted(path: Path, deadline: float, wait: float) -> int:
        attempts.append(path)
        assert wait == 0.0
        return real(path, deadline, wait)

    monkeypatch.setattr(installer_helper, "_take_opkg_lock", counted)
    with opkg_running(root / "run/opkg.lock"):
        assert probe_opkg_lock(root) is False

    assert attempts == [root / "run/opkg.lock"]


def test_a_lock_that_lost_its_name_while_probed_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock file replaced under the probe means an opkg run was starting or letting go."""
    root = tmp_path / "root"
    _receiver(root)
    real = installer_helper._still_named
    calls: list[int] = []

    def replaced_after_it_was_granted(path: Path, descriptor: int) -> bool:
        # The two checks while taking the lock pass; the one on the way out does not.
        calls.append(descriptor)
        return real(path, descriptor) if len(calls) <= 2 else False

    monkeypatch.setattr(installer_helper, "OPKG_DEFAULT_LOCKS", ("run/opkg.lock",))
    monkeypatch.setattr(installer_helper, "_still_named", replaced_after_it_was_granted)

    assert probe_opkg_lock(root) is False

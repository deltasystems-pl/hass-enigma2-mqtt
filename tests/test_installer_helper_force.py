"""The receiver-side facts a forced reinstall decides on, against a temporary root.

A forced reinstall is the path for a receiver whose plugin does not answer, so what it
needs from the receiver cannot come from the plugin: whether an unfinished transaction of
this project is lying about (and which channel it was keeping), how long the shared lock
has before the released rule frees it, whether a new interface process has opened the
plugin's log, and a start of the interface that no dropped connection can cut in half.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import (
    STALE_LOCK_SECONDS,
    boot_id,
    claim_transaction,
    leftovers,
    lock_info,
    log_holders,
    start_respawn,
    uptime,
)

WATCHED = "1:0:19:283D:3FB:1:C00000:0:0:0:"
BACKUPS = "home/root/mqttbridge-backups"


def _r2_dir(root: Path, transaction: str, *, service: str, steps: str) -> Path:
    """A stop-and-restore script's directory as the helper writes it."""
    directory = root / "tmp" / f"enigma2-mqtt-r2-{transaction}"
    directory.mkdir(parents=True)
    (directory / "r2.sh").write_text(
        "#!/bin/sh\n"
        f"python=python3\nhelper=/tmp/x\nroot=/\nbackup=/b\nservice={shlex.quote(service)}\n",
        encoding="utf-8",
    )
    (directory / "status").write_text(steps, encoding="utf-8")
    return directory


def test_a_receiver_with_nothing_of_ours_has_no_leftovers(tmp_path: Path) -> None:
    """Somebody stopped the interface on purpose: nothing here says it was us."""
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "enigma2-mqtt-r2-notours").mkdir()
    (tmp_path / BACKUPS).mkdir(parents=True)
    (tmp_path / BACKUPS / "ha-installer-0123456789ab").mkdir()

    found = leftovers(tmp_path)

    assert found["ours"] is False
    assert found["service"] is None


def _lock(root: Path, **record: object) -> Path:
    """The shared lock with an owner record, as a claim writes it."""
    lock = root / BACKUPS / ".ha-installer.lock"
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "owner.json").write_text(json.dumps(record), encoding="ascii")
    return lock


def _marker(root: Path, **record: object) -> Path:
    marker = root / "etc/enigma2/mqttbridge-update.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(record), encoding="ascii")
    return marker


def _update_status(root: Path, transaction: str, **record: object) -> Path:
    directory = root / BACKUPS / f"update-{transaction}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "status.json").write_text(json.dumps(record), encoding="ascii")
    return directory


def test_only_an_interrupted_stop_of_ours_counts_as_ours(tmp_path: Path) -> None:
    """Only R2 ever sends `init 4`, so runlevel 4 is ours only when an R2 of ours was cut
    off: an unfinished stop-and-restore of this boot. A lock alone, a marker alone, a kept
    self-update directory alone are ordinary residue - any receiver that ever had a
    refused or rolled-back update carries them - and prove nothing about who stopped it."""
    _lock(tmp_path, id="0123456789ab")
    assert leftovers(tmp_path)["ours"] is False
    shutil.rmtree(tmp_path / BACKUPS / ".ha-installer.lock")

    _marker(tmp_path)
    assert leftovers(tmp_path)["ours"] is False
    (tmp_path / "etc/enigma2/mqttbridge-update.json").unlink()

    (tmp_path / BACKUPS / "update-0123456789ab").mkdir()
    found = leftovers(tmp_path)
    assert found["ours"] is False
    assert found["transactions"] == ["update-0123456789ab"]
    (tmp_path / BACKUPS / "update-0123456789ab").rmdir()

    _r2_dir(tmp_path, "0123456789ab", service=WATCHED, steps="begun 1\nstopping 0\n")
    assert leftovers(tmp_path)["ours"] is True


def test_a_kept_finished_self_update_directory_is_not_ours(tmp_path: Path) -> None:
    """A self-update that ended keeps its directory (TRANSACTION.md section 1). Somebody
    who later stops the interface to edit its settings by hand must still be refused."""
    _update_status(
        tmp_path, "0123456789ab", id="0123456789ab", phase="finished", result="failed"
    )
    assert leftovers(tmp_path)["ours"] is False


def test_a_marker_of_another_boot_without_its_lock_is_not_ours(tmp_path: Path) -> None:
    """The plugin ignores such a marker (TRANSACTION.md section 4); the runlevel of this
    boot cannot be its doing, whatever phase it names."""
    _marker(tmp_path, id="0123456789ab", boot_id="another-boot", phase="rolling_back")
    assert leftovers(tmp_path)["ours"] is False


def test_a_self_update_cut_off_while_rolling_back_in_this_boot_is_ours(tmp_path: Path) -> None:
    """The self-update's own R2: its marker of this boot says it was rolling back and never
    ended - or its transaction directory says so and the lock of this boot names it."""
    _marker(tmp_path, id="0123456789ab", boot_id=boot_id(), phase="rolling_back")
    found = leftovers(tmp_path)
    assert found["ours"] is True
    assert found["rolling_back"] == ["0123456789ab"]
    (tmp_path / "etc/enigma2/mqttbridge-update.json").unlink()

    _update_status(tmp_path, "0123456789ab", id="0123456789ab", phase="rolling_back")
    assert leftovers(tmp_path)["ours"] is False
    _lock(tmp_path, id="0123456789ab", boot_id=boot_id(), origin="mqtt")
    assert leftovers(tmp_path)["ours"] is True


def test_a_self_update_record_that_ended_or_belongs_elsewhere_is_not_ours(
    tmp_path: Path,
) -> None:
    """Rolling back and then finished; a lock of another boot; a lock naming another id."""
    _update_status(
        tmp_path,
        "0123456789ab",
        id="0123456789ab",
        phase="rolling_back",
        result="rolled_back",
        finished=1790410100,
    )
    _lock(tmp_path, id="0123456789ab", boot_id=boot_id(), origin="mqtt")
    assert leftovers(tmp_path)["ours"] is False

    _update_status(tmp_path, "0123456789ab", id="0123456789ab", phase="rolling_back")
    _lock(tmp_path, id="0123456789ab", boot_id="another-boot", origin="mqtt")
    assert leftovers(tmp_path)["ours"] is False

    _lock(tmp_path, id="ba9876543210", boot_id=boot_id(), origin="mqtt")
    assert leftovers(tmp_path)["ours"] is False

    # A marker of this boot whose id is not a transaction id names nothing.
    _marker(tmp_path, id="../../etc", boot_id=boot_id(), phase="rolling_back")
    assert leftovers(tmp_path)["ours"] is False


def test_the_channel_an_interrupted_r2_was_keeping_is_read_from_its_script(
    tmp_path: Path,
) -> None:
    """R3 after the recovery puts back the channel the interrupted rollback recorded - the
    rollback whose transaction the lock names."""
    _r2_dir(tmp_path, "0123456789ab", service=WATCHED, steps="begun 1\nstopped\n")
    _lock(tmp_path, id="0123456789ab")

    found = leftovers(tmp_path)

    assert found["ours"] is True
    assert found["service"] == WATCHED


def test_the_channel_is_never_taken_from_another_transaction(tmp_path: Path) -> None:
    """The lock names X; the only unfinished R2 is Y's. Y's channel is not X's: R3 would
    zap the household's screen to a stale channel. With no lock there is no match either."""
    _r2_dir(tmp_path, "ba9876543210", service=WATCHED, steps="begun 1\nstopped\n")
    assert leftovers(tmp_path)["service"] is None

    _lock(tmp_path, id="0123456789ab")
    found = leftovers(tmp_path)
    assert found["ours"] is True
    assert found["service"] is None


def test_only_an_r2_that_never_recorded_its_restore_is_unrestored(tmp_path: Path) -> None:
    """The settings block goes back only for a rollback cut off before its restore: one
    that wrote `restored` - whatever its status - reached its restore, and writing that
    snapshot's settings again is not what it was stopped in the middle of."""
    _r2_dir(tmp_path, "0123456789ab", service=WATCHED, steps="begun 1\nstopped\n")
    _r2_dir(tmp_path, "ba9876543210", service=WATCHED, steps="stopped\nrestored 0\n")
    _r2_dir(tmp_path, "aaaaaaaaaaaa", service=WATCHED, steps="stopped\nrestored 1\n")

    found = leftovers(tmp_path)

    assert found["r2"] == ["0123456789ab", "aaaaaaaaaaaa", "ba9876543210"]
    assert found["unrestored"] == ["0123456789ab"]


def test_a_finished_r2_is_not_an_interrupted_one(tmp_path: Path) -> None:
    """A script that started the interface again finished; its directory is only untidy."""
    _r2_dir(tmp_path, "0123456789ab", service=WATCHED, steps="stopped\nstarted 0\ndone\n")

    found = leftovers(tmp_path)

    assert found["service"] is None


def test_two_interrupted_r2s_name_no_channel_unless_the_lock_says_which(
    tmp_path: Path,
) -> None:
    """Guessing between two recorded channels would be a coin toss over the household's."""
    _r2_dir(tmp_path, "0123456789ab", service=WATCHED, steps="stopped\n")
    _r2_dir(tmp_path, "ba9876543210", service="1:0:1:1:1:1:C00000:0:0:0:", steps="stopped\n")
    assert leftovers(tmp_path)["service"] is None

    _lock(tmp_path, id="0123456789ab")

    assert leftovers(tmp_path)["service"] == WATCHED


def _owner(lock: Path, **record: object) -> None:
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "owner.json").write_text(json.dumps(record), encoding="ascii")


def test_no_lock_is_not_held(tmp_path: Path) -> None:
    assert lock_info(tmp_path / "lock")["held"] is False


def test_a_self_update_lock_that_beat_a_moment_ago_is_live(tmp_path: Path) -> None:
    """A fresh heartbeat: `silent` is small, and the released rule frees it in 30 minutes."""
    lock = tmp_path / "lock"
    now = uptime()
    assert now is not None
    _owner(
        lock,
        pid=1,
        started=int(time.time()),
        boot_id=boot_id(),
        uptime=now - 5,
        origin="mqtt",
        id="0123456789ab",
    )

    info = lock_info(lock)

    assert info["held"] is True
    assert info["origin"] == "mqtt"
    assert info["silent"] is not None and info["silent"] < 60
    assert info["remaining"] is not None
    assert STALE_LOCK_SECONDS - 60 < info["remaining"] <= STALE_LOCK_SECONDS


def test_a_self_update_lock_whose_helper_stopped_beating_says_how_long_is_left(
    tmp_path: Path,
) -> None:
    """Ten minutes without a beat: twenty left by the same rule every installer uses."""
    lock = tmp_path / "lock"
    now = uptime()
    assert now is not None
    _owner(
        lock,
        pid=1,
        started=int(time.time()) - 600,
        boot_id=boot_id(),
        uptime=now - 600,
        origin="mqtt",
    )

    info = lock_info(lock)

    assert 590 <= info["silent"] <= 610
    assert STALE_LOCK_SECONDS - 610 <= info["remaining"] <= STALE_LOCK_SECONDS - 590


def test_a_lock_the_released_rule_calls_stale_has_nothing_left(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    _owner(lock, pid=1, started=1, boot_id="another-boot", uptime=1.0, origin="mqtt")

    assert lock_info(lock)["remaining"] == 0


def test_a_lock_the_rule_calls_stale_for_its_record_alone_has_nothing_left(
    tmp_path: Path,
) -> None:
    """A record that is not an object is stale at once, with no age to count down from."""
    lock = tmp_path / "lock"
    lock.mkdir()
    (lock / "owner.json").write_text("[1]", encoding="ascii")

    assert lock_info(lock)["remaining"] == 0


def test_lock_info_agrees_with_the_claim(tmp_path: Path) -> None:
    """What `lock-info` calls live the claim refuses, and what it calls free the claim takes."""
    lock = tmp_path / "lock"
    now = uptime()
    assert now is not None
    _owner(lock, pid=1, started=int(time.time()), boot_id=boot_id(), uptime=now - 60)
    assert lock_info(lock)["remaining"] > 0
    with pytest.raises(FileExistsError):
        claim_transaction(lock)

    _owner(
        lock,
        pid=1,
        started=int(time.time()) - STALE_LOCK_SECONDS - 60,
        boot_id=boot_id(),
        uptime=now - STALE_LOCK_SECONDS - 60,
    )
    assert lock_info(lock)["remaining"] == 0
    claim_transaction(lock)


def test_a_self_update_lock_whose_heartbeat_stopped_is_not_taken_back_early(
    tmp_path: Path,
) -> None:
    """Ten minutes of silence is not death: a stopped helper beats again when continued,
    and its `opkg` may still be writing. Only the released rule frees the lock."""
    lock = tmp_path / "lock"
    now = uptime()
    assert now is not None
    _owner(
        lock,
        pid=1,
        started=int(time.time()) - 600,
        boot_id=boot_id(),
        uptime=now - 600,
        origin="mqtt",
        id="0123456789ab",
    )

    with pytest.raises(FileExistsError):
        claim_transaction(lock, "ba9876543210")

    assert json.loads((lock / "owner.json").read_text(encoding="ascii"))["id"] == "0123456789ab"


def test_the_log_is_found_open_in_the_process_that_holds_it(tmp_path: Path) -> None:
    """The fd proof: the new enigma2 holds the plugin's log, whether it is enabled or not."""
    root = tmp_path / "root"
    log = root / "home/root/mqttbridge.log"
    log.parent.mkdir(parents=True)
    log.write_text("", encoding="utf-8")
    proc = tmp_path / "proc"
    for pid, target in ((101, log), (102, root / "home/root/other.log")):
        fd = proc / str(pid) / "fd"
        fd.mkdir(parents=True)
        (fd / "3").symlink_to(target)
    (proc / "103").mkdir()

    assert log_holders([101, 102, 103, 104], root, proc) == [101]


def test_the_fallback_log_counts_too(tmp_path: Path) -> None:
    root = tmp_path / "root"
    log = root / "tmp/mqttbridge.log"
    log.parent.mkdir(parents=True)
    log.write_text("", encoding="utf-8")
    fd = tmp_path / "proc" / "101" / "fd"
    fd.mkdir(parents=True)
    (fd / "7").symlink_to(log)

    assert log_holders([101], root, tmp_path / "proc") == [101]


def _stand_ins(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An `init` that writes down what it was asked, and a `pidof` that reads a flag."""
    events = tmp_path / "events"
    running = tmp_path / "enigma2.running"
    running.write_text("100\n", encoding="ascii")
    init = tmp_path / "bin" / "init"
    init.parent.mkdir()
    init.write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> {shlex.quote(str(events))}\n'
        f'if [ "$1" = 4 ]; then rm -f {shlex.quote(str(running))}; fi\n'
        f'if [ "$1" = 3 ]; then echo 101 > {shlex.quote(str(running))}; fi\n',
        encoding="ascii",
    )
    init.chmod(0o755)
    pidof = tmp_path / "bin" / "pidof"
    pidof.write_text(
        "#!/bin/sh\n"
        f"test -f {shlex.quote(str(running))} || exit 1\n"
        f"cat {shlex.quote(str(running))}\n",
        encoding="ascii",
    )
    pidof.chmod(0o755)
    return init, pidof, events


def _wait_for(path: Path, text: str, seconds: float = 10.0) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="ascii") == text:
            return text
        time.sleep(0.05)
    return path.read_text(encoding="ascii") if path.exists() else ""


def test_a_respawn_stops_then_starts_detached(tmp_path: Path) -> None:
    """`init 4` then `init 3`, from a session of its own: a dropped SSH cannot cut it."""
    init, pidof, events = _stand_ins(tmp_path)

    pid = start_respawn(stop=True, init=str(init), pidof=str(pidof), stop_wait=5)

    assert _wait_for(events, "4\n3\n") == "4\n3\n"
    assert os.getsid(pid) == pid


def test_a_respawn_waits_for_the_old_interface_to_go_before_starting(tmp_path: Path) -> None:
    """`init 4` only asks init to stop enigma2, and the process takes a while to go. The
    start waits for it: `init 3` over an interface still stopping is not a restart."""
    events = tmp_path / "events"
    running = tmp_path / "enigma2.running"
    running.write_text("100\n", encoding="ascii")
    init = tmp_path / "bin" / "init"
    init.parent.mkdir()
    init.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = 4 ]; then (sleep 2; rm -f {shlex.quote(str(running))}) '
        "</dev/null >/dev/null 2>&1 & fi\n"
        f'if [ "$1" = 3 ] && [ -f {shlex.quote(str(running))} ]; then '
        f"echo 3-over-a-running-interface >> {shlex.quote(str(events))}; exit 0; fi\n"
        f'echo "$1" >> {shlex.quote(str(events))}\n',
        encoding="ascii",
    )
    init.chmod(0o755)
    pidof = tmp_path / "bin" / "pidof"
    pidof.write_text(
        f"#!/bin/sh\ntest -f {shlex.quote(str(running))} || exit 1\necho 100\n",
        encoding="ascii",
    )
    pidof.chmod(0o755)

    start_respawn(stop=True, init=str(init), pidof=str(pidof), stop_wait=10)

    assert _wait_for(events, "4\n3\n", seconds=15) == "4\n3\n"


def test_a_start_only_respawn_never_stops_anything(tmp_path: Path) -> None:
    """Runlevel 4 already: only `init 3` is asked for."""
    init, pidof, events = _stand_ins(tmp_path)

    start_respawn(stop=False, init=str(init), pidof=str(pidof), stop_wait=5)

    assert _wait_for(events, "3\n") == "3\n"


def test_a_respawn_under_the_tests_refuses_the_real_init(tmp_path: Path) -> None:
    """The suite must never be able to stop the machine it runs on."""
    with pytest.raises(RuntimeError):
        start_respawn(stop=True, init="init", pidof="pidof", stop_wait=5)


def test_the_new_verbs_answer_on_the_command_line(tmp_path: Path) -> None:
    """The installer only ever sees the verbs, so the verbs are what is held to the shape."""
    helper = tmp_path / "helper.py"
    helper.write_bytes(Path(installer_helper.__file__).read_bytes())
    _r2_dir(tmp_path, "0123456789ab", service=WATCHED, steps="stopped\n")
    env = {**os.environ}

    def run(*args: str) -> dict:
        answer = subprocess.run(
            [sys.executable, str(helper), *args, "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return json.loads(answer.stdout)

    _lock(tmp_path, id="0123456789ab")
    assert run("leftovers") == {
        "ours": True,
        "lock": True,
        "marker": False,
        "r2": ["0123456789ab"],
        "unrestored": ["0123456789ab"],
        "rolling_back": [],
        "transactions": [],
        "service": WATCHED,
    }
    assert run("lock-info", str(tmp_path / "no-lock"))["held"] is False
    assert run("logfd", "--pid", "1", "--proc", str(tmp_path / "proc")) == {"holding": []}

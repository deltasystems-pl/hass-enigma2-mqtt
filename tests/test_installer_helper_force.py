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


def test_every_kind_of_leftover_counts_as_ours(tmp_path: Path) -> None:
    """A lock, an unfinished R2, the self-update's marker or its transaction directory."""
    lock = tmp_path / BACKUPS / ".ha-installer.lock"
    lock.mkdir(parents=True)
    assert leftovers(tmp_path)["ours"] is True
    lock.rmdir()

    marker = tmp_path / "etc/enigma2/mqttbridge-update.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="ascii")
    assert leftovers(tmp_path)["ours"] is True
    marker.unlink()

    (tmp_path / BACKUPS / "update-0123456789ab").mkdir()
    assert leftovers(tmp_path)["ours"] is True
    (tmp_path / BACKUPS / "update-0123456789ab").rmdir()

    _r2_dir(tmp_path, "0123456789ab", service=WATCHED, steps="begun 1\nstopping 0\n")
    assert leftovers(tmp_path)["ours"] is True


def test_the_channel_an_interrupted_r2_was_keeping_is_read_from_its_script(
    tmp_path: Path,
) -> None:
    """R3 after the recovery puts back the channel the interrupted rollback recorded."""
    _r2_dir(tmp_path, "0123456789ab", service=WATCHED, steps="begun 1\nstopped\n")

    found = leftovers(tmp_path)

    assert found["ours"] is True
    assert found["service"] == WATCHED


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

    lock = tmp_path / BACKUPS / ".ha-installer.lock"
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(json.dumps({"id": "0123456789ab"}), encoding="ascii")

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

    assert run("leftovers") == {
        "ours": True,
        "lock": False,
        "marker": False,
        "r2": ["0123456789ab"],
        "transactions": [],
        "service": WATCHED,
    }
    assert run("lock-info", str(tmp_path / "no-lock"))["held"] is False
    assert run("logfd", "--pid", "1", "--proc", str(tmp_path / "proc")) == {"holding": []}

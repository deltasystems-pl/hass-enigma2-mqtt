"""The durable transaction lock, and the races a second claimer can lose it to.

The lock is a directory on the receiver's flash. That is what lets it survive a power
cut, and it is also what makes it awkward: a directory is not a mutex, "is this lock
stale" and "take it" are two separate acts, and the only clock a receiver is sure to
have is its uptime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import (
    STALE_LOCK_SECONDS,
    boot_id,
    claim_transaction,
    release_transaction,
    uptime,
)


def owner(lock_dir: Path) -> dict:
    return json.loads((lock_dir / "owner.json").read_text(encoding="ascii"))


def test_a_lock_being_claimed_right_now_is_not_an_owner_less_lock(tmp_path: Path) -> None:
    """Creating the directory and writing the record are two steps, not one.

    A second claimer arriving between them finds a directory with no owner record. It
    used to read that as "the owner died mid-write" and reclaim a lock whose owner was
    about to start an install - two transactions, two rollbacks, one receiver.
    """
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir(mode=0o700)

    with pytest.raises(FileExistsError):
        claim_transaction(lock_dir)


def test_an_owner_less_lock_older_than_any_install_is_still_reclaimed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Otherwise a claim that died between the two steps wedges the box for ever."""
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir(mode=0o700)
    old = time.time() - STALE_LOCK_SECONDS - 60
    os.utime(lock_dir, (old, old))

    claim_transaction(lock_dir)

    assert owner(lock_dir)["boot_id"] == boot_id()
    assert "reclaiming stale installer lock" in capsys.readouterr().err


def test_only_one_of_two_claimers_can_take_the_same_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both see the same stale lock; exactly one may end up holding it.

    Deciding "this is stale" and then writing into the directory are two acts, and a
    second claimer can do both of them in between. It would then be installing while
    the first claimer, still acting on a verdict that is now out of date, writes itself
    in on top - two transactions unwinding one receiver.

    This drives that interleaving directly: the second claimer runs to completion inside
    the first one's staleness check.
    """
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    stale = {**owner(lock_dir), "boot_id": "00000000-0000-0000-0000-000000000000"}
    (lock_dir / "owner.json").write_text(json.dumps(stale), encoding="ascii")

    real_is_stale = installer_helper._is_stale
    second: list[BaseException | None] = []
    interleaved = False

    def judge_then_let_another_claimer_in(path: Path) -> str:
        nonlocal interleaved
        verdict = real_is_stale(path)
        if not interleaved and path == lock_dir:
            interleaved = True
            try:
                claim_transaction(lock_dir)
                second.append(None)
            except FileExistsError as error:
                second.append(error)
        return verdict

    monkeypatch.setattr(installer_helper, "_is_stale", judge_then_let_another_claimer_in)
    first: BaseException | None = None
    try:
        claim_transaction(lock_dir)
    except FileExistsError as error:
        first = error

    outcomes = [first, second[0]]
    assert sum(outcome is None for outcome in outcomes) == 1, "both claimers took the lock"
    assert all(
        isinstance(outcome, FileExistsError) for outcome in outcomes if outcome is not None
    )
    assert sorted(item.name for item in lock_dir.iterdir()) == ["owner.json"]
    assert [item for item in tmp_path.iterdir() if item.name.startswith(".lock-stale-")] == []


def test_the_owner_record_is_written_whole_or_not_at_all(tmp_path: Path) -> None:
    """A partly written record parses as garbage, and garbage read as stale.

    It is written through the same atomic replace the helper uses for the receiver's
    own settings, so a reader sees either the previous contents or the new ones.
    """
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)

    written = owner(lock_dir)
    assert written["boot_id"] == boot_id()
    assert isinstance(written["uptime"], float)
    # Nothing is left behind by the replace.
    assert [item.name for item in lock_dir.iterdir()] == ["owner.json"]


def test_a_clock_that_jumps_forward_does_not_reclaim_a_live_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receivers without a battery-backed clock boot in 1970 and jump when NTP answers.

    An install that started before the jump would look decades old measured against the
    wall clock. Measured against uptime - which cannot jump - it is seconds old, and the
    boot id proves the two measurements are from the same boot.
    """
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    booted_before_ntp = {**owner(lock_dir), "started": 60}
    (lock_dir / "owner.json").write_text(json.dumps(booted_before_ntp), encoding="ascii")

    with pytest.raises(FileExistsError):
        claim_transaction(lock_dir)


def test_a_lock_held_across_the_stale_bound_by_uptime_is_reclaimed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Uptime is the measure, so it has to be able to condemn a lock as well."""
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    long_ago = {**owner(lock_dir), "uptime": (uptime() or 0.0) - STALE_LOCK_SECONDS - 60}
    (lock_dir / "owner.json").write_text(json.dumps(long_ago), encoding="ascii")

    claim_transaction(lock_dir)

    assert "reclaiming stale installer lock" in capsys.readouterr().err


def test_a_lock_from_another_boot_is_stale_however_young_it_looks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its owner cannot exist any more, whatever its timestamps say."""
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    other_boot = {
        **owner(lock_dir),
        "boot_id": "00000000-0000-0000-0000-000000000000",
        "started": int(time.time()),
        "uptime": uptime(),
    }
    (lock_dir / "owner.json").write_text(json.dumps(other_boot), encoding="ascii")

    claim_transaction(lock_dir)

    assert "before the receiver last rebooted" in capsys.readouterr().err


def test_a_kernel_with_no_uptime_falls_back_to_the_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worse measure is still better than none."""
    monkeypatch.setattr(installer_helper, "UPTIME_PATH", tmp_path / "nothing-here")
    assert uptime() is None

    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir)
    old = {**owner(lock_dir), "started": int(time.time()) - STALE_LOCK_SECONDS - 60}
    (lock_dir / "owner.json").write_text(json.dumps(old), encoding="ascii")

    claim_transaction(lock_dir)

    assert "it has been held for" in capsys.readouterr().err
    release_transaction(lock_dir)

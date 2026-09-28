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


# --- a recovery that failed hands the lock back to the transaction it recovers ----------

ABANDONED = "0123456789ab"
OURS = "fedcba987654"


def _stale_lock_of(lock_dir: Path, transaction: str) -> None:
    """A lock an installer transaction abandoned: its id, claimed long before the bound."""
    claim_transaction(lock_dir, transaction)
    long_ago = {**owner(lock_dir), "uptime": (uptime() or 0.0) - STALE_LOCK_SECONDS - 60}
    (lock_dir / "owner.json").write_text(json.dumps(long_ago), encoding="ascii")


def test_a_lock_handed_back_names_the_abandoned_transaction_again(tmp_path: Path) -> None:
    """The claim reclaimed the abandoned transaction's lock, and its recovery failed. Released,
    the lock would forget the only record of which snapshot is still to be put back; handed
    back, the next claim reclaims it at once and recovers by that id again."""
    lock_dir = tmp_path / "lock"
    _stale_lock_of(lock_dir, ABANDONED)
    assert claim_transaction(lock_dir, OURS) == ABANDONED

    installer_helper.hand_back_transaction(lock_dir, OURS, ABANDONED)

    assert owner(lock_dir)["id"] == ABANDONED
    # Stale as it was when this install reclaimed it: nothing waits thirty minutes.
    assert installer_helper._is_stale(lock_dir)
    assert claim_transaction(lock_dir, "a1b2c3d4e5f6") == ABANDONED


def test_the_released_helper_sees_a_handed_back_lock_as_stale_too(tmp_path: Path) -> None:
    """The rule is shared with every released installer and the plugin: a lock handed back
    must be one they reclaim as well, never a new lock that holds them off."""
    from . import released_installer_helper_0_3_1 as released

    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir, OURS)
    installer_helper.hand_back_transaction(lock_dir, OURS, ABANDONED)

    assert released._is_stale(lock_dir)
    released.claim_transaction(lock_dir)


def _plugin_rule(lock_dir: Path, current_boot: str, now_uptime: float | None) -> str:
    """The plugin's update helper's stale rule, restated from TRANSACTION.md 2.3.

    Restated rather than copied: the plugin is another licence. It is the released
    installer's rule with its own sentences - a boot id that differs is stale, the same
    boot is judged by uptime, and only without either does the wall clock decide.
    """
    try:
        recorded = json.loads((lock_dir / "owner.json").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return ""
    if not isinstance(recorded, dict):
        return "not an object"
    boot = recorded.get("boot_id")
    if current_boot and isinstance(boot, str) and boot and boot != current_boot:
        return "claimed before the last reboot"
    then = recorded.get("uptime")
    if (
        current_boot
        and boot == current_boot
        and now_uptime is not None
        and isinstance(then, (int, float))
        and not isinstance(then, bool)
    ):
        return "held too long" if now_uptime - then > STALE_LOCK_SECONDS else ""
    started = recorded.get("started")
    if not isinstance(started, int) or isinstance(started, bool):
        return "no start time"
    return "held too long" if int(time.time()) - started > STALE_LOCK_SECONDS else ""


@pytest.mark.parametrize(
    ("boot", "now_uptime", "clock_offset"),
    [
        ("boot-A", 510.0, 0),
        # The clock stepped back after the hand-back: by a minute, by an hour.
        ("boot-A", 510.0, -60),
        ("boot-A", 4000.0, -3600),
        # The receiver rebooted, with no battery-backed clock, and NTP has not answered.
        ("boot-B", 30.0, -3600),
        ("boot-B", 30.0, -365 * 86400),
        # A kernel that names no boot at all: the wall clock is all there is.
        ("", None, 0),
    ],
    ids=["same_clock", "clock_back_60s", "clock_back_1h", "reboot_1h", "reboot_1y", "no_boot"],
)
def test_a_handed_back_lock_is_stale_to_every_rule_on_any_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boot: str,
    now_uptime: float | None,
    clock_offset: int,
) -> None:
    """Dated back alone, a handed-back record was stale only while the wall clock stayed
    where it was: a clock stepped back, or a receiver that rebooted into 1970 before NTP
    answered, made it a young lock again - and every install, and the plugin's own update
    and removal, refused as busy for as long as the clock was wrong. Its placeholder boot
    id is no boot that ever runs, so every rule finds it claimed before the last reboot."""
    from . import released_installer_helper_0_3_1 as released

    lock_dir = tmp_path / "lock"
    monkeypatch.setattr(installer_helper, "boot_id", lambda: "boot-A")
    monkeypatch.setattr(installer_helper, "uptime", lambda: 500.0)
    claim_transaction(lock_dir, OURS)
    installer_helper.hand_back_transaction(lock_dir, OURS, ABANDONED)

    now = time.time() + clock_offset
    for module in (installer_helper, released):
        monkeypatch.setattr(module, "boot_id", lambda: boot)
        monkeypatch.setattr(module, "uptime", lambda: now_uptime)
    monkeypatch.setattr(time, "time", lambda: now)

    assert installer_helper._is_stale(lock_dir)
    assert released._is_stale(lock_dir)
    assert _plugin_rule(lock_dir, boot, now_uptime)
    assert claim_transaction(lock_dir, "a1b2c3d4e5f6") == ABANDONED


def test_a_handed_back_record_names_no_boot_that_ever_runs(tmp_path: Path) -> None:
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir, OURS)

    installer_helper.hand_back_transaction(lock_dir, OURS, ABANDONED)

    record = owner(lock_dir)
    assert record["boot_id"] == installer_helper.HANDED_BACK_BOOT_ID
    assert record["boot_id"] != boot_id()
    assert "uptime" not in record
    assert record["started"] < time.time() - STALE_LOCK_SECONDS
    assert "handed back" in installer_helper._is_stale(lock_dir)


# --- the recovery is tried a bounded number of times -----------------------------------


def test_a_hand_back_counts_the_failed_recoveries_and_the_claim_reports_them(
    tmp_path: Path,
) -> None:
    """Three installs in a row over the same abandoned transaction, each recovery failing:
    the count travels with the lock, so the installer can stop handing it back."""
    lock_dir = tmp_path / "lock"
    _stale_lock_of(lock_dir, ABANDONED)
    nonces = ["00000000000a", "00000000000b", "00000000000c"]

    seen = []
    for attempt, nonce in enumerate(nonces, start=1):
        seen.append(installer_helper.claim_with_attempts(lock_dir, nonce))
        installer_helper.hand_back_transaction(lock_dir, nonce, ABANDONED, attempt)

    assert seen == [
        {"reclaimed": ABANDONED, "attempts": 0},
        {"reclaimed": ABANDONED, "attempts": 1},
        {"reclaimed": ABANDONED, "attempts": 2},
    ]
    assert owner(lock_dir)["attempts"] == 3
    # Released, the lock forgets the abandoned transaction and its count.
    release_transaction(lock_dir)
    assert installer_helper.claim_with_attempts(lock_dir, "00000000000d") == {
        "reclaimed": "",
        "attempts": 0,
    }


@pytest.mark.parametrize("attempts", [-1, True, "2", 1.5, None])
def test_a_count_that_is_not_one_reads_as_none(tmp_path: Path, attempts: object) -> None:
    lock_dir = tmp_path / "lock"
    _stale_lock_of(lock_dir, ABANDONED)
    stale = {**owner(lock_dir), "attempts": attempts}
    (lock_dir / "owner.json").write_text(json.dumps(stale), encoding="ascii")

    assert installer_helper.claim_with_attempts(lock_dir, OURS)["attempts"] == 0


@pytest.mark.parametrize("attempts", [True, False, -1, "2", 1.5, None])
def test_the_installer_reads_a_count_that_is_not_one_as_none(attempts: object) -> None:
    """The installer's own reading of the claim, beside the helper's: a boolean is no count,
    though Python calls `True` an int that equals one."""
    from custom_components.enigma2_mqtt import installer

    claimed = json.dumps({"reclaimed": ABANDONED, "attempts": attempts})
    assert installer._reclaimed(claimed, OURS) == (ABANDONED, 0)


@pytest.mark.parametrize("attempts", [-1, 10_000, True, 1.0])
def test_a_hand_back_refuses_a_count_out_of_range(tmp_path: Path, attempts: int) -> None:
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir, OURS)

    with pytest.raises(ValueError):
        installer_helper.hand_back_transaction(lock_dir, OURS, ABANDONED, attempts)

    assert owner(lock_dir)["id"] == OURS


@pytest.mark.parametrize(
    "record",
    [
        # Another installer's lock.
        {"pid": 1, "started": 1, "id": "a1b2c3d4e5f6"},
        # The plugin's self-update's, which names where it was started from.
        {"pid": 1, "started": 1, "id": OURS, "origin": "mqtt"},
        {"pid": 1, "started": 1},
        [],
    ],
)
def test_only_the_transactions_own_lock_is_handed_back(tmp_path: Path, record: object) -> None:
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    text = json.dumps(record)
    (lock_dir / "owner.json").write_text(text, encoding="ascii")

    with pytest.raises(installer_helper.LockNotOursError):
        installer_helper.hand_back_transaction(lock_dir, OURS, ABANDONED)

    assert (lock_dir / "owner.json").read_text(encoding="ascii") == text


@pytest.mark.parametrize("taken", ["replaced", "renamed away"])
def test_a_lock_taken_while_it_is_handed_back_is_left_to_its_new_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, taken: str
) -> None:
    """Our lock is stale by the time a slow recovery has failed, and a claimer may reclaim it
    between the hand-back's read and its write: it renames the directory away and makes a new
    one at its name. Written by path, the handed-back record would land in that new lock and
    make a live install's lock look handed back, and stale, to the next claimer."""
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir, OURS)
    theirs = {"pid": 2, "started": int(time.time()), "id": "a1b2c3d4e5f6"}
    reading = installer_helper.json.loads

    def loads(text: str, *args: object, **kwargs: object) -> object:
        # The claimer's reclaim, in the moment after our record was read.
        monkeypatch.setattr(installer_helper.json, "loads", reading)
        os.rename(lock_dir, tmp_path / ".lock-stale-claimer")
        if taken == "replaced":
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(json.dumps(theirs), encoding="ascii")
        return reading(text, *args, **kwargs)

    monkeypatch.setattr(installer_helper.json, "loads", loads)

    with pytest.raises(installer_helper.LockNotOursError):
        installer_helper.hand_back_transaction(lock_dir, OURS, ABANDONED)

    if taken == "replaced":
        assert owner(lock_dir) == theirs
    else:
        assert not lock_dir.exists()


def test_a_lock_without_an_owner_record_is_not_handed_back(tmp_path: Path) -> None:
    """A directory with no record is a claim being made right now, or one that died between
    its two steps: whichever, not this transaction's any more."""
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()

    with pytest.raises(installer_helper.LockNotOursError):
        installer_helper.hand_back_transaction(lock_dir, OURS, ABANDONED)

    assert lock_dir.is_dir()


def _run_helper(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the helper as the receiver does: a lone file, away from the integration.

    Started from the package directory, Python would put that directory first on
    `sys.path`, where the integration's `select.py` stands in for the standard library's.
    """
    standalone = tmp_path / "tmp" / "enigma2-mqtt-installer-0123456789ab.py"
    standalone.parent.mkdir(exist_ok=True)
    shutil.copyfile(installer_helper.__file__, standalone)
    return subprocess.run(
        [sys.executable, str(standalone), *args], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("record", [None, {"pid": 1, "started": 1, "id": "a1b2c3d4e5f6"}])
def test_a_refused_hand_back_says_so_by_its_own_exit_status(
    tmp_path: Path, record: dict | None
) -> None:
    """The caller releases the lock when a hand-back fails - but never one the hand-back
    refused as somebody else's: a release checks no owner, and would take a live
    self-update's or another install's lock away."""
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    if record is not None:
        (lock_dir / "owner.json").write_text(json.dumps(record), encoding="ascii")

    result = _run_helper(tmp_path, "hand-back", str(lock_dir), "--owner", OURS, "--id", ABANDONED)

    assert result.returncode == installer_helper.EXIT_LOCK_NOT_OURS
    assert "not this transaction's" in result.stderr
    assert lock_dir.is_dir()


def test_the_claim_and_the_hand_back_carry_the_count_on_the_command_line(
    tmp_path: Path,
) -> None:
    """What the installer runs on the receiver, end to end with the real helper."""
    lock_dir = tmp_path / "lock"
    _stale_lock_of(lock_dir, ABANDONED)

    def helper(*args: str) -> subprocess.CompletedProcess:
        return _run_helper(tmp_path, *args)

    claimed = helper("claim", str(lock_dir), "--id", OURS)
    handed = helper(
        "hand-back", str(lock_dir), "--owner", OURS, "--id", ABANDONED, "--attempts", "2"
    )
    again = helper("claim", str(lock_dir), "--id", "a1b2c3d4e5f6")

    assert json.loads(claimed.stdout) == {"reclaimed": ABANDONED, "attempts": 0}
    assert handed.returncode == 0
    assert json.loads(again.stdout) == {"reclaimed": ABANDONED, "attempts": 2}


@pytest.mark.parametrize("ids", [(OURS, "../../etc"), ("nothex", ABANDONED), (OURS, OURS)])
def test_a_hand_back_takes_two_different_transaction_ids(
    tmp_path: Path, ids: tuple[str, str]
) -> None:
    lock_dir = tmp_path / "lock"
    claim_transaction(lock_dir, OURS)

    with pytest.raises(ValueError):
        installer_helper.hand_back_transaction(lock_dir, *ids)

    assert owner(lock_dir)["id"] == OURS

"""Receiver-side filesystem transaction helper used by the SSH installer.

This module intentionally uses only Python 3.9's standard library. The installer
streams this exact file to the receiver after preflight, and its tests execute the
same functions against a temporary root.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request

PACKAGE = "enigma2-plugin-extensions-mqttbridge"
PLUGIN_DIR = "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
WEBIF_SHIM = (
    "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/WebChilds/External/MQTTBridge.py"
)
# What the receiver actually compiles the hook into. CPython's default is
# `__pycache__/MQTTBridge.<tag>.pyc`; OpenViX 6.6 writes the legacy name beside the
# source instead, and Python imports that as a complete module. Both are treated as one
# thing, because a restore that put back "no hook" and left either behind would leave an
# importable hook reaching for a plugin that is no longer installed.
WEBIF_LEGACY_BYTECODE = "MQTTBridge.pyc"
# Where opkg keeps its database is a configuration item, not a constant. OpenViX 6.6
# ships `/etc/opkg/opkg.conf` naming `/var/lib/opkg`, and leaves `/usr/lib/opkg`
# containing nothing but `alternatives/` - so a hard-coded `/usr/lib/opkg/status` found
# no database at all, and the first snapshot of a guided install died on a box that was
# perfectly healthy. Older images put it under `/usr/lib/opkg`, which is why both are
# still searched when nothing says otherwise.
OPKG_CONF_DIR = "etc/opkg"
OPKG_DATABASES = ("var/lib/opkg", "usr/lib/opkg")
SETTINGS = "etc/enigma2/settings"
PROVISION = "etc/enigma2/mqttbridge.json"
# opkg's own lock, which this helper takes around every change to opkg's database. Which
# file that is depends on the opkg: `option lock_file` in its configuration when there is
# one, otherwise the default it was built with. opkg 0.6.3 on OpenViX 6.6 is built with
# `/run/opkg.lock` and never touches `/var/lock/opkg.lock`, which is where this helper
# used to lock - so for as long as it did, it excluded nothing. Older opkg releases were
# built with the `/var/lock` default, and nothing on the receiver says which one this is
# without running opkg, so when the configuration is silent both are taken.
#
# What the lock buys is narrower than it sounds. opkg reads its status file before it
# takes the lock, so the lock does not keep an opkg run from starting on a view of the
# database taken just before this helper's change; it only keeps the two from holding
# the lock at once. An opkg run already holding it is waited for, and one that asks while
# this helper holds it fails its own lock and changes nothing. The image's daily
# package-list update barely touches the status file at all; the case that matters is
# somebody installing or removing a package from the receiver's own menu while a guided
# install or its rollback is under way.
OPKG_LOCK_OPTION = "lock_file"
OPKG_DEFAULT_LOCKS = ("run/opkg.lock", "var/lock/opkg.lock")
# opkg does not wait for its lock: it tries once and fails "Could not lock". This helper
# waits a little, because a snapshot refused for an operation that would have ended in
# seconds is a worse install than one that starts late - but never past the installer's
# command timeout, because a helper still blocked after the installer gave up would take
# the lock later, from under nobody. The bound is the whole wait, across every lock file
# taken, not a wait per file. The snapshot runs under a 30 s command and the restore
# under a 60 s one; a rollback that gives up is the worse outcome, so it waits longer.
OPKG_LOCK_WAIT_SNAPSHOT_SECONDS = 20.0
OPKG_LOCK_WAIT_RESTORE_SECONDS = 40.0
OPKG_LOCK_POLL_SECONDS = 0.25
# opkg lets go in three steps - unlock, close, delete the file - so a lock granted
# between the first and the last is on a file that is about to lose its name. The
# holder looks again after this pause, which is far longer than those three calls take.
OPKG_LOCK_SETTLE_SECONDS = 0.1
# The exit status of a helper that could not get opkg's lock within its bound, so that
# the installer can tell "opkg is busy, try again" from every other failure. EX_TEMPFAIL.
EXIT_OPKG_BUSY = 75
# The exit status of a step that finished its work and then found that opkg's lock file
# had lost its name while the helper held it. For a restore that is its own outcome: the
# files are back, but an opkg run may have written the database at the same time.
EXIT_OPKG_LOCK_LOST = 76
SETTINGS_PREFIX = "config.plugins.mqttbridge."
# The snapshot directories this installer makes, and nothing else. The nonce is
# `secrets.token_hex(6)`; anything else under the backups directory belongs to whoever
# put it there and is never a candidate for pruning.
SNAPSHOT_NAME = re.compile(r"ha-installer-[0-9a-f]{12}")
# The newest snapshot is the operator's rollback point. The one before it is kept so
# that an install which has just replaced a rollback point still leaves the previous
# one reachable; everything older has been superseded twice.
KEEP_SNAPSHOTS = 2
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
UPTIME_PATH = Path("/proc/uptime")
# Generous: the slowest measured install is minutes, and reclaiming too eagerly would
# let a second transaction start on top of a first one that is merely slow.
STALE_LOCK_SECONDS = 30 * 60
# A transaction's id, as the lock's owner record, the snapshot's name and the files of
# its stop-and-restore carry it: twelve lowercase hexadecimal digits, and nothing else.
TRANSACTION_ID = re.compile(r"[0-9a-f]{12}")
# Where a restore builds the plugin directory before it swaps it in, and where the
# directory it replaces waits until the swap is over. Two things decide the place. A
# rename does not cross filesystems, so it is beside the `Plugins` tree rather than in
# `/tmp`. And it is outside every directory enigma2 scans for plugins: enigma2 imports
# every directory under `Plugins/<category>/`, so a complete copy of this plugin there,
# under any name, is a second installation of it - the same trap as a backup copy of a
# Home Assistant integration left inside `custom_components/`.
PLUGINS_TREE = "usr/lib/enigma2/python/Plugins"
# OpenWebif on the receiver itself. It is asked for the channel being watched and the
# standby state before a restart, to put them back afterwards, and for the restart.
WEBIF_URL = "http://127.0.0.1"
WEBIF_TIMEOUT_SECONDS = 5.0
# The line of enigma2's settings file that says which channel to tune at start.
LASTSERVICE_KEY = "config.tv.lastservice"
# A service reference is written into the settings file as a line of its own, so it may
# carry nothing that ends a line or hides in one. The length is far beyond any real
# reference, stream URL and name included.
SERVICE_REF = re.compile(r"[^\x00-\x1f\x7f]{1,1024}")


def _path(root: Path, relative: str) -> Path:
    return root / relative


class OpkgPaths(NamedTuple):
    """Where this receiver's opkg database actually is."""

    status: Path
    info: Path
    # What was looked at to decide, so that a failure can say where it looked rather
    # than name one path the box was never going to have.
    searched: tuple


def _opkg_config_files(root: Path) -> list[Path]:
    """Return the configuration files opkg would read, in the order it reads them.

    `opkg.conf` first and then the rest of `/etc/opkg/*.conf` in name order, because
    that is the order in which a later `option` overrides an earlier one. A box with
    two files disagreeing about the database is already in trouble; what matters here
    is that this and opkg reach the same answer.
    """
    directory = _path(root, OPKG_CONF_DIR)
    main = directory / "opkg.conf"
    ordered = [main] + sorted(
        Path(name) for name in glob.glob(str(directory / "*.conf")) if Path(name) != main
    )
    return [path for path in ordered if path.is_file()]


def _opkg_options(root: Path) -> dict:
    """Return the `option name value` settings from this receiver's opkg config.

    Only the `option`/`opt` form is read, which is what every image in reach writes and
    the only form the two settings this needs are given in. `dest`, `src` and `arch`
    lines are not options and are ignored; on OpenViX the architectures are in a
    separate `arch.conf` and have nothing to do with where the database lives.
    """
    options: dict = {}
    for path in _opkg_config_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # A conf that cannot be read is not a reason to abandon the ones that can;
            # the defaults below are still there, and a missing status file is reported
            # with every path that was considered.
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) >= 3 and parts[0] in ("option", "opt"):
                options[parts[1]] = parts[2]
    return options


def _configured_path(root: Path, configured: str) -> Path:
    """Place a path out of the opkg config under the root this helper works against.

    The config names absolute paths, and on the receiver the root is `/` so this is the
    identity. It is not the identity in a test, or in anything that ever runs this
    against a mounted image, so the join is written once and guarded once: a `..` in
    there would reach outside the tree this is allowed to touch, and there is no
    legitimate reason for one.
    """
    parts = [part for part in PurePosixPath(configured.strip()).parts if part not in ("/", "")]
    if not parts or ".." in parts:
        raise ValueError(f"opkg configuration names an unusable path: {configured}")
    return root.joinpath(*parts)


def opkg_paths(root: Path) -> OpkgPaths:
    """Resolve the status file and info directory the way opkg itself does.

    The configuration wins where it speaks. Where it does not, the database is looked
    for in one place and then the other - and both halves are taken from the same one,
    because a status file in `/var` and an info directory in `/usr` is not a layout any
    image has, and writing one of each would be worse than failing.
    """
    options = _opkg_options(root)
    bases = tuple(_path(root, base) for base in OPKG_DATABASES)
    chosen = next((base for base in bases if (base / "status").is_file()), bases[0])
    status_option = options.get("status_file")
    info_option = options.get("info_dir")
    return OpkgPaths(
        status=(
            _configured_path(root, status_option)
            if status_option is not None
            else chosen / "status"
        ),
        info=(
            _configured_path(root, info_option) if info_option is not None else chosen / "info"
        ),
        searched=(bases if status_option is None else ()),
    )


def _missing_status(paths: OpkgPaths) -> str:
    """Say which database was looked for, so the answer is not "no such file"."""
    if not paths.searched:
        return f"opkg status file {paths.status} does not exist"
    considered = ", ".join(str(base / "status") for base in paths.searched)
    return (
        f"opkg status file {paths.status} does not exist; no opkg configuration named "
        f"one and neither default is there ({considered})"
    )


def _stanzas(text: str) -> list[str]:
    return [part.strip("\n") for part in text.split("\n\n") if part.strip()]


def _package_name(stanza: str) -> str | None:
    for line in stanza.splitlines():
        if line.startswith("Package: "):
            return line.removeprefix("Package: ").strip()
    return None


def _atomic_text(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".ha-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def opkg_lock_paths(root: Path) -> tuple:
    """Return the lock files an opkg run on this receiver could be holding.

    The configuration wins where it speaks, exactly as it does for the database: an
    image that moved the lock says so in `option lock_file`, and then that file is the
    only one opkg will ever take. Where it is silent, both built-in defaults are
    returned, newest first, because the default is compiled into opkg and cannot be
    read from here. Taking a lock no opkg uses costs nothing; missing the one it does
    use is the defect this replaced.
    """
    configured = _opkg_options(root).get(OPKG_LOCK_OPTION)
    if configured is not None:
        return (_configured_path(root, configured),)
    return tuple(_path(root, name) for name in OPKG_DEFAULT_LOCKS)


class OpkgBusyError(TimeoutError):
    """opkg's lock stayed held by somebody else for the whole of the wait."""


class OpkgLockLostError(OSError):
    """A lock file this helper held lost its name while the helper was inside.

    Whatever was done under it may have overlapped an opkg run that took a fresh file
    of the same name, so the work is not to be trusted.
    """


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _still_named(path: Path, descriptor: int) -> bool:
    """Return whether `path` still leads to the file open on `descriptor`."""
    try:
        return _same_file(os.fstat(descriptor), os.stat(str(path)))
    except FileNotFoundError:
        return False


def _take_opkg_lock(path: Path, deadline: float, wait: float) -> int:
    """Hold `path` the way opkg holds it, or raise once `deadline` has passed.

    opkg creates the file and takes a POSIX record lock on the whole of it with
    `lockf`; `lockf` here joins that same lock domain, where `flock` would not. opkg
    also deletes the file when it lets go - after unlocking and closing it. A process
    granted the lock in that gap holds it on a file whose name is about to go, and the
    next opkg creates a fresh file and locks that one unopposed. So a lock only counts
    once the name still leads to the locked file, and still does after a pause longer
    than opkg's unlock, close and delete take; otherwise it is taken again.

    Only "somebody else holds it" is waited on. Any other failure to lock - a kernel
    or filesystem without record locks answers `ENOLCK` - is not going to clear by
    waiting, and is raised at once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        else:
            if _still_named(path, descriptor):
                time.sleep(OPKG_LOCK_SETTLE_SECONDS)
                if _still_named(path, descriptor):
                    return descriptor
            os.close(descriptor)
        if time.monotonic() >= deadline:
            raise OpkgBusyError(
                f"opkg is busy: {path} is held by another opkg run (an install or "
                f"removal from the receiver's menu, or the image's update check); "
                f"waited {wait:.0f} s"
            )
        time.sleep(OPKG_LOCK_POLL_SECONDS)


def _release_opkg_lock(path: Path, descriptor: int) -> None:
    """Let go the way opkg does - remove the file, then close it.

    Only while the name is still ours: removing a file somebody else has since
    created and locked would hand the next opkg an unopposed lock.
    """
    try:
        if _still_named(path, descriptor):
            path.unlink()
    except OSError:
        pass
    finally:
        os.close(descriptor)


@contextmanager
def _lock(root: Path, wait: float):
    """Hold every lock opkg could be using on this receiver, in a fixed order.

    `wait` bounds the whole acquisition, however many files there are. On the way
    out, a lock whose file lost its name while the helper was inside is reported: the
    work it covered may have overlapped an opkg run, and passing that off as a clean
    step is the one outcome worse than failing it.
    """
    deadline = time.monotonic() + wait
    held: list = []
    try:
        for path in opkg_lock_paths(root):
            held.append((path, _take_opkg_lock(path, deadline, wait)))
        yield
        lost = [str(path) for path, descriptor in held if not _still_named(path, descriptor)]
        if lost:
            raise OpkgLockLostError(
                "opkg's lock file was replaced while the helper held it, so an opkg run "
                f"may have overlapped this step: {', '.join(lost)}"
            )
    finally:
        for path, descriptor in reversed(held):
            _release_opkg_lock(path, descriptor)


def probe_opkg_lock(root: Path) -> bool:
    """Return whether opkg's lock is free right now, taking and releasing it at once.

    One attempt, no wait: every lock opkg could be using is taken with a non-blocking
    `lockf` and let go the way opkg lets go, so a probe on a free receiver leaves it as
    it found it. Asking opkg instead tells nothing: `opkg status`, `opkg info` and `opkg
    list-installed` do not take the lock at all (measured on opkg 0.6.3, OpenViX 6.6).
    A lock file that lost its name while it was held means an opkg run was letting go or
    starting in that instant, which is busy too.
    """
    try:
        with _lock(root, 0.0):
            return True
    except (OpkgBusyError, OpkgLockLostError):
        return False


def _webif_bytecode_files(root: Path) -> list[Path]:
    """Return every compiled copy of the OpenWebif hook, in both locations.

    The legacy sibling is the one this image writes, and it is the one a first
    install's rollback used to leave behind: `opkg remove` deletes the files it
    installed, the snapshot said there had been no hook, and `MQTTBridge.pyc` stayed
    there importable.
    """
    shim = _path(root, WEBIF_SHIM)
    cache_dir = shim.parent / "__pycache__"
    if cache_dir.is_symlink() or (cache_dir.exists() and not cache_dir.is_dir()):
        raise ValueError("OpenWebif bytecode directory is unsafe")
    files = sorted(cache_dir.glob("MQTTBridge.*.pyc")) if cache_dir.is_dir() else []
    legacy = shim.parent / WEBIF_LEGACY_BYTECODE
    if legacy.is_symlink() or legacy.exists():
        files.append(legacy)
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError("OpenWebif bytecode must be regular files")
    return files


def snapshot(root: Path, backup: Path) -> dict[str, object]:
    """Snapshot only files owned or consumed by this package."""
    if backup.exists():
        raise FileExistsError(backup)
    # The directory is made only once opkg's lock is held, and taken away again if the
    # snapshot does not complete. Every `ha-installer-*` directory counts as a snapshot
    # to the pruning, so one left behind by a refusal would push a real rollback point
    # out of the two that are kept. That includes a snapshot that completed under a lock
    # which turned out to have been lost: it may be a view of a database mid-change.
    created = False
    try:
        with _lock(root, OPKG_LOCK_WAIT_SNAPSHOT_SECONDS):
            backup.mkdir(mode=0o700, parents=True)
            created = True
            return _snapshot_into(root, backup)
    except BaseException:
        if created:
            shutil.rmtree(backup, ignore_errors=True)
        raise


def _snapshot_into(root: Path, backup: Path) -> dict[str, object]:
    """Copy what the package owns into `backup`; opkg's lock is already held."""
    plugin = _path(root, PLUGIN_DIR)
    opkg = opkg_paths(root)
    status_path = opkg.status
    if not status_path.is_file():
        raise FileNotFoundError(_missing_status(opkg))
    package_stanza = next(
        (
            s
            for s in _stanzas(status_path.read_text(encoding="utf-8"))
            if _package_name(s) == PACKAGE
        ),
        None,
    )
    if package_stanza is not None:
        (backup / "package-status").write_text(package_stanza + "\n", encoding="utf-8")
    if plugin.is_symlink():
        raise ValueError("plugin directory may not be a symlink")
    if plugin.is_dir():
        shutil.copytree(plugin, backup / "plugin", symlinks=True)
    webif_shim = _path(root, WEBIF_SHIM)
    if webif_shim.is_symlink() or (webif_shim.exists() and not webif_shim.is_file()):
        raise ValueError("OpenWebif shim must be a regular file")
    if webif_shim.is_file():
        shutil.copy2(webif_shim, backup / "webif-shim")
    webif_cache_files = _webif_bytecode_files(root)
    if webif_cache_files:
        cache_backup = backup / "webif-cache"
        cache_backup.mkdir()
        for source in webif_cache_files:
            shutil.copy2(source, cache_backup / source.name)
    info_backup = backup / "opkg-info"
    info_files = [Path(name) for name in glob.glob(str(opkg.info / f"{PACKAGE}.*"))]
    if info_files:
        info_backup.mkdir()
        for source in info_files:
            if source.is_symlink() or not source.is_file():
                raise ValueError("opkg metadata must be regular files")
            shutil.copy2(source, info_backup / source.name)
    settings_path = _path(root, SETTINGS)
    settings_lines = []
    if settings_path.is_file():
        settings_lines = [
            line
            for line in settings_path.read_text(encoding="utf-8").splitlines()
            if line.startswith(SETTINGS_PREFIX)
        ]
    (backup / "plugin-settings").write_text(
        "".join(line + "\n" for line in settings_lines), encoding="utf-8"
    )
    provision = _path(root, PROVISION)
    if provision.is_symlink():
        raise ValueError("provisioning may not be a symlink")
    if provision.is_file():
        shutil.copy2(provision, backup / "provisioning")
    metadata = {
        "schema": 2,
        "plugin": plugin.is_dir(),
        "package_status": package_stanza is not None,
        "opkg_info": bool(info_files),
        "provisioning": provision.is_file(),
        "settings": settings_path.is_file(),
        "webif_shim": webif_shim.is_file(),
        "webif_cache": bool(webif_cache_files),
    }
    (backup / "snapshot.json").write_text(
        json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def restore(
    root: Path,
    backup: Path,
    restore_provisioning: bool,
    restore_settings: bool,
) -> None:
    """Restore the snapshot while preserving unrelated package and user state."""
    metadata = json.loads((backup / "snapshot.json").read_text(encoding="utf-8"))
    expected_flags = {
        "plugin",
        "package_status",
        "opkg_info",
        "provisioning",
        "settings",
        "webif_shim",
        "webif_cache",
    }
    if (
        set(metadata) != expected_flags | {"schema"}
        or metadata["schema"] != 2
        or any(not isinstance(metadata[key], bool) for key in expected_flags)
    ):
        raise ValueError("invalid snapshot metadata")
    required = [backup / "plugin-settings"]
    if metadata["plugin"]:
        required.append(backup / "plugin")
    if metadata["package_status"]:
        required.append(backup / "package-status")
    if metadata["opkg_info"]:
        required.append(backup / "opkg-info")
    if metadata["provisioning"]:
        required.append(backup / "provisioning")
    if metadata["webif_shim"]:
        required.append(backup / "webif-shim")
    if metadata["webif_cache"]:
        required.append(backup / "webif-cache")
    if any(not path.exists() for path in required):
        raise ValueError("snapshot is incomplete")
    opkg = opkg_paths(root)
    status_path = opkg.status
    info_dir = opkg.info
    plugin = _path(root, PLUGIN_DIR)
    webif_shim = _path(root, WEBIF_SHIM)
    if not status_path.is_file() or status_path.is_symlink():
        raise ValueError(f"opkg status is unavailable or unsafe: {status_path}")
    if not info_dir.is_dir() or info_dir.is_symlink():
        raise ValueError(f"opkg metadata directory is unavailable or unsafe: {info_dir}")
    if plugin.is_symlink():
        raise ValueError("live plugin directory may not be a symlink")
    if webif_shim.is_symlink() or (webif_shim.exists() and not webif_shim.is_file()):
        raise ValueError("live OpenWebif shim is unsafe")
    live_cache_files = _webif_bytecode_files(root)
    if metadata["plugin"] and (
        (backup / "plugin").is_symlink() or not (backup / "plugin").is_dir()
    ):
        raise ValueError("backed-up plugin is unsafe")
    if metadata["package_status"] and not (backup / "package-status").is_file():
        raise ValueError("backed-up package status is unsafe")
    if metadata["opkg_info"]:
        info_sources = list((backup / "opkg-info").iterdir())
        if not info_sources or any(
            source.is_symlink() or not source.is_file() for source in info_sources
        ):
            raise ValueError("backed-up opkg metadata is unsafe")
    else:
        info_sources = []
    if metadata["provisioning"] and (
        (backup / "provisioning").is_symlink() or not (backup / "provisioning").is_file()
    ):
        raise ValueError("backed-up provisioning is unsafe")
    if metadata["webif_shim"] and (
        (backup / "webif-shim").is_symlink() or not (backup / "webif-shim").is_file()
    ):
        raise ValueError("backed-up OpenWebif shim is unsafe")
    if metadata["webif_cache"]:
        cache_backup = backup / "webif-cache"
        if cache_backup.is_symlink() or not cache_backup.is_dir():
            raise ValueError("backed-up OpenWebif bytecode is unsafe")
        cache_sources = list(cache_backup.iterdir())
        if not cache_sources or any(
            source.is_symlink()
            or not source.is_file()
            or not source.name.startswith("MQTTBridge.")
            or source.suffix != ".pyc"
            for source in cache_sources
        ):
            raise ValueError("backed-up OpenWebif bytecode is unsafe")
    else:
        cache_sources = []
    token = _staging_token(backup)
    if metadata["plugin"]:
        # Before anything moves: a swap that cannot be a rename must not leave opkg's
        # records restored under a plugin tree that is still the new one.
        _require_one_filesystem(root, plugin)
    with _lock(root, OPKG_LOCK_WAIT_RESTORE_SECONDS):
        current = _stanzas(status_path.read_text(encoding="utf-8"))
        current = [stanza for stanza in current if _package_name(stanza) != PACKAGE]
        if metadata["package_status"]:
            current.append((backup / "package-status").read_text(encoding="utf-8").strip())
        _atomic_text(status_path, "\n\n".join(current) + "\n")

        # Every file below goes back by a rename, and the plugin directory by two: a
        # restore can run while the old enigma2 is still the running process - when the
        # image asked a question instead of restarting, or when the install failed before
        # the restart - and a restart answered in the middle of it must find either the
        # whole old tree or the whole new one, never a mix of the two.
        wanted_info = {source.name for source in info_sources}
        for name in glob.glob(str(info_dir / f"{PACKAGE}.*")):
            candidate = Path(name)
            if candidate.name in wanted_info and not candidate.is_dir():
                continue
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            else:
                candidate.unlink(missing_ok=True)
        for source in info_sources:
            _replace_file(source, info_dir / source.name)

        _replace_tree(
            root, backup / "plugin" if metadata["plugin"] else None, plugin, token
        )

        if metadata["webif_shim"] or metadata["webif_cache"]:
            webif_shim.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(backup / "webif-shim" if metadata["webif_shim"] else None, webif_shim)
        cache_dir = webif_shim.parent / "__pycache__"
        # The name says which of the two locations a copy came out of: exactly
        # `MQTTBridge.pyc` is the legacy sibling, anything else is a cache tag.
        placed = {
            (
                webif_shim.parent / source.name
                if source.name == WEBIF_LEGACY_BYTECODE
                else cache_dir / source.name
            ): source
            for source in cache_sources
        }
        for cache_file in live_cache_files:
            if cache_file not in placed:
                cache_file.unlink()
        for target, source in placed.items():
            target.parent.mkdir(exist_ok=True)
            _replace_file(source, target)

        if restore_settings:
            settings_path = _path(root, SETTINGS)
            if metadata["settings"] and not settings_path.is_file():
                raise ValueError("settings file is unavailable")
            unrelated = []
            if settings_path.is_file():
                unrelated = [
                    line
                    for line in settings_path.read_text(encoding="utf-8").splitlines()
                    if not line.startswith(SETTINGS_PREFIX)
                ]
            old_plugin_settings = (backup / "plugin-settings").read_text(encoding="utf-8")
            if metadata["settings"] or unrelated:
                settings_text = "".join(line + "\n" for line in unrelated) + old_plugin_settings
                _atomic_text(settings_path, settings_text)
        if restore_provisioning:
            _replace_file(
                backup / "provisioning" if metadata["provisioning"] else None,
                _path(root, PROVISION),
            )


def _replace_file(source: Path | None, target: Path) -> None:
    """Put a copy of `source` at `target` by one rename, or remove `target` for None.

    The copy is made beside the target, so the rename stays on one filesystem and is
    atomic: a reader sees the old file or the new one, never a half-written one. Its
    permissions are the source's, which for the provisioning file is 0600 - it holds the
    broker password - and the temporary is 0600 from the moment it exists.
    """
    if source is None:
        target.unlink(missing_ok=True)
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=target.name + ".ha-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        shutil.copyfile(source, temporary)
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def staging_parent(root: Path) -> Path:
    """Return where a plugin tree is staged and set aside: beside `Plugins`, not in it."""
    return _path(root, PLUGINS_TREE).parent


def staging_names(token: str) -> tuple[str, str]:
    """Return the staged and the set-aside directory's names for one transaction.

    These two names, and only these, are what a restore ever creates or removes beside
    `Plugins` - where enigma2's own `Components`, `Screens` and `Tools` live.
    """
    return f".mqttbridge-staging-{token}", f".mqttbridge-aside-{token}"


def _staging_token(backup: Path) -> str:
    """Name the staging directories after the transaction whose snapshot is restored.

    A restore of the same snapshot again - a recovery of an interrupted one - then finds
    and removes exactly what the interrupted one left. A snapshot not named by this
    installer gets a fresh token, and so never matches anything already there.
    """
    if SNAPSHOT_NAME.fullmatch(backup.name):
        return backup.name.rsplit("-", 1)[1]
    return secrets.token_hex(6)


def _require_one_filesystem(root: Path, live: Path) -> None:
    parent = staging_parent(root)
    beside = live.parent
    while not beside.exists() and beside != beside.parent:
        beside = beside.parent
    if os.stat(str(parent)).st_dev != os.stat(str(beside)).st_dev:
        raise ValueError(
            f"{parent} and {live.parent} are on different filesystems; "
            "the plugin directory cannot be swapped in by a rename"
        )


def _remove_own(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _replace_tree(root: Path, source: Path | None, live: Path, token: str) -> None:
    """Swap a copy of `source` in at `live` with two renames, or take `live` away.

    `rename` cannot replace a directory that is not empty, so this is two steps: the
    live tree aside, the staged one in. For the moment between them the plugin
    directory is absent. A restart landing exactly there starts an enigma2 without the
    plugin, which the installer's proof then catches like any other failed start; it is
    never a tree that is half old and half new, which nothing could catch.

    What an earlier, interrupted restore of the same snapshot left is removed first -
    by this transaction's two exact names, never by a pattern: the directory beside
    `Plugins` is enigma2's own.
    """
    parent = staging_parent(root)
    staged_name, aside_name = staging_names(token)
    staged = parent / staged_name
    aside = parent / aside_name
    if source is not None:
        _require_one_filesystem(root, live)
    # A set-aside tree left by an interrupted restore of this snapshot is either the
    # superseded one or - cut off between the two renames - the only copy of what was
    # live; this restore replaces the live tree either way.
    _remove_own(staged)
    _remove_own(aside)
    if source is not None:
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staged, symlinks=True)
    try:
        if live.exists():
            os.rename(live, aside)
        if source is not None:
            try:
                os.rename(staged, live)
            except BaseException:
                if aside.exists() and not live.exists():
                    os.rename(aside, live)
                raise
    finally:
        # The tree set aside is the only copy of what was live until the new one is in
        # place, so it goes only once something is at the live path again - or when
        # taking the live tree away was the whole point.
        if source is None or live.exists():
            shutil.rmtree(aside, ignore_errors=True)
        shutil.rmtree(staged, ignore_errors=True)


def verify_manifest(root: Path, manifest_path: Path) -> None:
    """Verify every regular payload file from the bundled IPK after opkg."""
    for line in manifest_path.read_text(encoding="ascii").splitlines():
        digest, absolute = line.split("  ", 1)
        relative = absolute.lstrip("/")
        if not relative or ".." in Path(relative).parts:
            raise ValueError("unsafe manifest path")
        target = _path(root, relative)
        if not target.is_file() or target.is_symlink():
            raise ValueError("installed file missing or unsafe")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError("installed file digest mismatch")


def read_identity(root: Path) -> dict[str, object]:
    """Read the non-secret settings which bind an HA entry to this box, and its name.

    Raw facts, and `null` is one of them: it means enigma2's settings file has no line
    for that setting, which is neither an empty value nor a difference. Enigma2 never
    writes out a setting that still equals its default, so a receiver left on the
    default base topic has no `base_topic` line at all, and this used to answer
    `enigma2` for a box that had said nothing - the right answer, from a copy of the
    plugin's defaults kept on the wrong side of the link. Applying the defaults is the
    caller's job: they belong to the plugin, they are worth having in exactly one
    place, and this half of the installer runs on the receiver with no way to import
    anything of Home Assistant's.

    `friendly_name` binds nothing and is reported for the opposite reason: it is the
    name the household reads, and an install whose name field was left empty leaves
    the receiver called this, so it is the only way for the caller to know what the box
    it has just configured is called. Raw like the rest - an empty string is a receiver
    that stores an empty name, and `null` a receiver that stores none - because the
    caller decides what to do with a blank, and cannot if this has already flattened
    the two.
    """
    settings = _path(root, SETTINGS)
    values: dict[str, str] = {}
    if settings.is_file() and not settings.is_symlink():
        for line in settings.read_text(encoding="utf-8").splitlines():
            if not line.startswith(SETTINGS_PREFIX) or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values[name.removeprefix(SETTINGS_PREFIX)] = value
    base_topic = values.get("base_topic")
    enabled = values.get("enabled")
    return {
        "node_id": values.get("node_id"),
        "base_topic": None if base_topic is None else base_topic.strip("/"),
        "enabled": None if enabled is None else enabled.strip().lower() == "true",
        "ha_mode": values.get("ha_mode"),
        "friendly_name": values.get("friendly_name"),
    }


def boot_id() -> str:
    """Return this boot's identifier, or an empty string if the kernel has none."""
    try:
        return BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def uptime() -> float | None:
    """Return seconds since the receiver booted, or None if the kernel does not say."""
    try:
        return float(UPTIME_PATH.read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _age_of(lock_dir: Path) -> float | None:
    try:
        return time.time() - lock_dir.stat().st_mtime
    except OSError:
        return None


def _is_stale(lock_dir: Path) -> str:
    """Return why an existing lock may be reclaimed, or an empty string if it may not.

    The lock is a directory on the receiver's flash, so it survives the one failure it
    cannot survive: a box pulled out of the wall halfway through an install comes back
    with a lock nobody holds and refuses every later attempt for ever. A lock written
    under a different boot is held by a process that no longer exists, and a lock older
    than any plausible install has outlived its owner either way.

    Age is measured against uptime rather than the clock whenever the lock was taken
    under this same boot. Many receivers have no battery-backed clock: they boot in
    1970 and jump to the real time the moment NTP answers, which can be minutes into an
    install. A wall-clock age would then read as decades and reclaim a live lock.
    Uptime cannot jump; across boots the boot id has already settled the question.
    """
    owner = lock_dir / "owner.json"
    try:
        recorded = json.loads(owner.read_text(encoding="ascii"))
    except (OSError, ValueError):
        # The record is written atomically, so it is never half there: it is either a
        # claim that has not got to it yet - a matter of milliseconds - or one that died
        # in between. Only age tells those apart, and the directory's own mtime is the
        # only age an owner-less lock has.
        age = _age_of(lock_dir)
        if age is not None and age > STALE_LOCK_SECONDS:
            return f"its owner record is unreadable and it is {int(age)} seconds old"
        return ""
    if not isinstance(recorded, dict):
        return "its owner record is not an object"
    current = boot_id()
    recorded_boot = recorded.get("boot_id")
    if current and isinstance(recorded_boot, str) and recorded_boot and recorded_boot != current:
        return "it was claimed before the receiver last rebooted"

    now_uptime = uptime()
    recorded_uptime = recorded.get("uptime")
    if (
        current
        and recorded_boot == current
        and now_uptime is not None
        and isinstance(recorded_uptime, (int, float))
        and not isinstance(recorded_uptime, bool)
    ):
        held = now_uptime - recorded_uptime
        if held > STALE_LOCK_SECONDS:
            return f"it has been held for {int(held)} seconds"
        return ""

    started = recorded.get("started")
    if not isinstance(started, int) or isinstance(started, bool):
        return "its start time is missing or malformed"
    age = int(time.time()) - started
    if age > STALE_LOCK_SECONDS:
        return f"it has been held for {age} seconds"
    return ""


def _reclaimable_id(lock_dir: Path) -> str:
    """Return the transaction id a stale lock of this installer names, or ''.

    Only a record the SSH installer wrote counts, and that is a record without an
    `origin`: the plugin's own update helper names where it was started from, keeps
    its snapshots under another name and recovers its own transactions. An id that is
    not exactly twelve hexadecimal digits names nothing this installer made.
    """
    try:
        recorded = json.loads((lock_dir / "owner.json").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return ""
    if not isinstance(recorded, dict) or "origin" in recorded:
        return ""
    candidate = recorded.get("id")
    if isinstance(candidate, str) and TRANSACTION_ID.fullmatch(candidate):
        return candidate
    return ""


def claim_transaction(lock_dir: Path, transaction_id: str = "") -> str:
    """Claim the durable per-receiver transaction lock or fail busy.

    `transaction_id` goes into the owner record as `id`, so that whoever later finds
    this lock abandoned knows which snapshot belongs to it - by name, never by a file
    time, which a receiver without a battery-backed clock gets wrong. The return value
    is the other side of that: the id of an abandoned installer transaction whose lock
    this claim reclaimed, or an empty string.

    Reclaiming a stale lock is a rename, not a delete in place. Deciding that a lock is
    stale and then writing into it is check-then-act: two claimers can both decide it,
    both write, and both believe they hold it, and then two transactions unwind one
    receiver. A rename has exactly one winner.

    The verdict is then reached a second time, on the directory the rename actually took
    away. The first verdict can be overtaken - between judging and renaming, another
    claimer can have reclaimed the lock and started an install - and the second verdict
    is the one made while nobody else can touch the directory. A lock that turns out to
    be alive is put straight back and this claimer is told the receiver is busy.
    """
    if transaction_id and not TRANSACTION_ID.fullmatch(transaction_id):
        raise ValueError("a transaction id is twelve lowercase hexadecimal digits")
    reclaimed = ""
    try:
        lock_dir.mkdir(mode=0o700, parents=False)
    except FileExistsError:
        if not _is_stale(lock_dir):
            raise
        retired = lock_dir.with_name(
            f".{lock_dir.name}-stale-{os.getpid()}-{secrets.token_hex(4)}"
        )
        try:
            os.rename(lock_dir, retired)
        except OSError as error:
            raise FileExistsError(lock_dir) from error
        reason = _is_stale(retired)
        if not reason:
            try:
                os.rename(retired, lock_dir)
            except OSError:
                shutil.rmtree(retired, ignore_errors=True)
            raise FileExistsError(lock_dir) from None
        reclaimed = _reclaimable_id(retired)
        shutil.rmtree(retired, ignore_errors=True)
        lock_dir.mkdir(mode=0o700, parents=False)
        print(f"reclaiming stale installer lock: {reason}", file=sys.stderr)
    _write_owner(lock_dir, transaction_id)
    return reclaimed


def _write_owner(lock_dir: Path, transaction_id: str = "") -> None:
    """Record who holds the lock, under which boot, and at what point in that boot.

    ASCII only, which `json.dumps` guarantees: every released helper decodes the record
    as ASCII, and one it cannot decode is judged by the directory's age - so a live lock
    would look abandoned after thirty minutes. A released helper ignores `id`.
    """
    record: dict = {
        "pid": os.getpid(),
        "started": int(time.time()),
        "boot_id": boot_id(),
        "uptime": uptime(),
    }
    if transaction_id:
        record["id"] = transaction_id
    _atomic_text(lock_dir / "owner.json", json.dumps(record) + "\n")


def release_transaction(lock_dir: Path) -> None:
    """Release only a well-formed installer transaction lock."""
    owner = lock_dir / "owner.json"
    if not owner.is_file():
        raise ValueError("transaction lock has no owner")
    owner.unlink()
    lock_dir.rmdir()


def prune_snapshots(
    backups: Path, keep: int = KEEP_SNAPSHOTS, keep_name: str = ""
) -> list[str]:
    """Delete all but the newest `keep` installer snapshots, and report what went.

    A snapshot is the only way back from an install, so one is kept - the one taken by
    the install that just succeeded - and one more behind it. Without this, every guided
    install left another copy of the plugin directory on the receiver's flash for ever.

    `keep_name` is that install's own snapshot, and it is never removed whatever its
    timestamp says. The order here is the modification time, and on these receivers a
    timestamp is not a clock: many have no battery-backed one, boot in 1970 and jump to
    the real time when NTP answers - which can be after the snapshot was taken. Ranking
    the newest snapshot last and deleting it would throw away the only way back from the
    install that is committing.

    Only this installer's own directories are candidates: the name has to be exactly
    `ha-installer-<nonce>`, it has to be a directory, and it may not be a symlink, which
    `shutil.rmtree` would otherwise be asked to follow out of the backups directory. The
    transaction lock, and anything a person put there, is not touched.
    """
    if keep < 1:
        raise ValueError("at least one snapshot is kept")
    if backups.is_symlink() or not backups.is_dir():
        return []
    candidates = []
    current_present = False
    for child in backups.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if not SNAPSHOT_NAME.fullmatch(child.name):
            continue
        if keep_name and child.name == keep_name:
            current_present = True
            continue
        try:
            modified = child.stat().st_mtime
        except OSError:
            continue
        candidates.append((modified, child))
    # The name breaks a tie, because two directories made in the same second are
    # otherwise ordered by whatever `iterdir` happened to return.
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1].name))
    # The one that is kept by name has already taken a place.
    retain = max(0, keep - 1 if current_present else keep)
    removed = []
    for _modified, child in candidates[: max(0, len(candidates) - retain)]:
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child.name)
    return removed


def _webif(base: str, path: str) -> bytes | None:
    """Return OpenWebif's answer to a GET on the receiver itself, or None.

    None covers every way of not answering - OpenWebif not running, which is also
    enigma2 not running, a refused or reset connection, a status that is not 200.
    Nothing here raises: every caller treats "no answer" as a fact to record.
    """
    try:
        with urllib_request.urlopen(base + path, timeout=WEBIF_TIMEOUT_SECONDS) as answer:
            if answer.status != 200:
                return None
            return answer.read(1024 * 1024)
    except (OSError, ValueError, urllib_error.URLError):
        return None


def _webif_bool(value: object) -> bool | None:
    """Read one of OpenWebif's booleans, which arrive as JSON or as the words."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def record_state(root: Path, base: str = WEBIF_URL) -> dict:
    """Return what a restart has to keep: the channel, the standby state, the saved channel.

    `service` and `standby` come from OpenWebif - what the receiver is doing now - and
    are None when it does not answer: then there is nothing to keep, and the transaction
    says so rather than inventing a channel. `saved` is the `config.tv.lastservice` the
    settings file holds, the channel the image would start on; it is reported so that a
    reader can see whether the two differ, and it is never printed anywhere but here.
    """
    service = None
    standby = None
    answer = _webif(base, "/api/statusinfo")
    if answer is not None:
        try:
            status = json.loads(answer.decode("utf-8", errors="replace"))
        except ValueError:
            status = None
        if isinstance(status, dict):
            reference = status.get("currservice_serviceref")
            if isinstance(reference, str) and SERVICE_REF.fullmatch(reference.strip()):
                service = reference.strip()
            standby = _webif_bool(status.get("inStandby"))
    return {"service": service, "standby": standby, "saved": _saved_service(root)}


def _saved_service(root: Path) -> str | None:
    settings = _path(root, SETTINGS)
    if not settings.is_file() or settings.is_symlink():
        return None
    for line in settings.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(LASTSERVICE_KEY + "="):
            return line.split("=", 1)[1]
    return None


def write_lastservice(root: Path, reference: str) -> None:
    """Make `reference` the channel enigma2 tunes when it next starts.

    Only meaningful while enigma2 is stopped: a running enigma2 holds its settings in
    memory and writes all of them out on its next clean quit, over this line. The stop-
    and-restore script calls this after the plugin's settings are back, never before,
    so that the restore - which rewrites the same file - cannot take the channel away.
    """
    if not SERVICE_REF.fullmatch(reference):
        raise ValueError("not a service reference that fits on one settings line")
    settings = _path(root, SETTINGS)
    if settings.is_symlink():
        raise ValueError("the settings file may not be a symlink")
    lines = []
    if settings.is_file():
        lines = [
            line
            for line in settings.read_text(encoding="utf-8").splitlines()
            if not line.startswith(LASTSERVICE_KEY + "=")
        ]
    lines.append(f"{LASTSERVICE_KEY}={reference}")
    settings.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(settings, "".join(line + "\n" for line in lines))


def zap(reference: str, base: str = WEBIF_URL) -> bool:
    """Tune `reference` through OpenWebif; True when it says it did."""
    if not SERVICE_REF.fullmatch(reference):
        return False
    answer = _webif(base, "/api/zap?sRef=" + urllib_parse.quote(reference, safe=""))
    if answer is None:
        return False
    try:
        result = json.loads(answer.decode("utf-8", errors="replace"))
    except ValueError:
        return False
    return isinstance(result, dict) and _webif_bool(result.get("result")) is True


def power_state(state: int, base: str = WEBIF_URL) -> bool:
    """Ask OpenWebif for a power state; True when it answered.

    The answer is not the result. State 3 is a clean restart of the interface, and the
    quit can reset the very connection that asked for it, so a restart is judged by
    enigma2's pid changing and never by this.
    """
    return _webif(base, f"/api/powerstate?newstate={int(state)}") is not None


# The stop-and-restore of a rollback that has to put the plugin's settings back, run on
# the receiver as one script in a session of its own. What is decided here and why:
#
# - One script, started once and detached: between `init 4` and `init 3` the household
#   has no picture, so nothing that can be cut off from outside the receiver - an SSH
#   connection, Home Assistant restarting - may sit between the two. The installer
#   follows it by reading the status file, and a dropped connection sends it nothing.
# - It depends on nothing Home Assistant tidies up. It runs from a directory of its own
#   (`/tmp/enigma2-mqtt-r2-<id>/`) holding its own copy of this helper, its status and
#   its output; the installer removes that directory only after it has seen the script
#   start the interface again, and keeps the transaction lock until then.
# - The trap is set before `init 4`, on EXIT HUP INT TERM PIPE. Without PIPE a shell
#   whose output reader went away dies on its next write and never runs the trap. The
#   trap first ignores further signals, so a second one cannot cut off the first.
# - The trap waits for a restore that is still running. A signal to the shell alone
#   runs the trap at once, while the restore child is still working; starting the
#   interface or writing the channel under it would let the restore rename the
#   settings file over the channel.
# - The restore is bounded. A watchdog ends it after its limit, so the picture comes
#   back even from a restore that hangs; a restore cut off part-way is repeated by the
#   next install's recovery, which is safe, while a missing picture is not.
# - The settings block and the channel are written only once enigma2 is seen to be
#   gone. An enigma2 that has not stopped within the wait still owns its settings and
#   writes them out on its next clean quit, over anything written now; then the script
#   puts back the files alone, says `stop_timeout`, and leaves the settings to it.
# - Order: the settings block first, then `lastservice`, then `init 3`, each by a rename.
# - It ends in `exit`: after a signal trap that does not exit, the shell carries on with
#   its next step. `finish` is idempotent, because the EXIT trap runs it a second time.
# - It does not keep the transaction lock alive; the installer holds it. What no trap
#   covers is SIGKILL or power between the stop and the start. Power reboots the
#   receiver, which frees the lock at once; a killed script leaves the interface stopped
#   until the receiver is switched off and on again. That residual is stated, not
#   hidden behind a heartbeat nobody would be left to stop.
R2_SCRIPT = """#!/bin/sh
# Enigma2 MQTT Bridge installer: stop the receiver's interface, put the plugin and its
# settings back, write the channel to start on, and start the interface again.
python={python}
helper={helper}
root={root}
backup={backup}
service={service}
provisioning={provisioning}
status={status}
init={init}
pidof={pidof}
restore_pid=
watchdog=
restored=
stopped=
started=
say() {{
    echo "$1" >> "$status"
}}
finish() {{
    if [ -n "$started" ]; then
        return 0
    fi
    started=1
    trap '' HUP INT TERM PIPE
    if [ -n "$restore_pid" ]; then
        wait "$restore_pid"
        rc=$?
        restore_pid=
        if [ -z "$restored" ]; then
            restored=$rc
            say "restored $rc"
        fi
    fi
    if [ -n "$watchdog" ]; then
        kill "$watchdog" 2>/dev/null
        watchdog=
    fi
    if [ -n "$stopped" ] && [ -n "$service" ]; then
        "$python" "$helper" lastservice --service "$service" --root "$root"
        say "written $?"
    else
        say "not_written"
    fi
    "$init" 3
    say "started $?"
}}
trap 'finish; exit 1' HUP INT TERM PIPE
trap 'finish' EXIT
say "begun $$"
"$init" 4
say "stopping $?"
waited=0
while "$pidof" enigma2 >/dev/null 2>&1; do
    if [ "$waited" -ge {stop_wait} ]; then
        break
    fi
    sleep 1
    waited=$((waited + 1))
done
if "$pidof" enigma2 >/dev/null 2>&1; then
    settings=
    say "stop_timeout"
else
    settings=--settings
    stopped=1
    say "stopped"
fi
"$python" "$helper" restore "$backup" $settings $provisioning --root "$root" &
restore_pid=$!
( sleep {restore_limit}; kill -TERM "$restore_pid" 2>/dev/null ) &
watchdog=$!
wait "$restore_pid"
rc=$?
restored=$rc
restore_pid=
say "restored $rc"
finish
trap - EXIT HUP INT TERM PIPE
say "done"
exit 0
"""
R2_STOP_WAIT_SECONDS = 30
R2_RESTORE_LIMIT_SECONDS = 90
# Set by this project's own test suite, and nowhere else. While it is set, the script is
# refused unless `init` and `pidof` are stand-ins named by absolute path outside the
# system's directories: a test that forgot them would otherwise stop the interface of the
# machine the tests run on - as root, in the development environment this was written in.
TEST_GUARD = "ENIGMA2_MQTT_TESTS_FORBID_SYSTEM_INIT"
_SYSTEM_DIRECTORIES = ("/sbin/", "/bin/", "/usr/", "/lib/", "/etc/")


def _stand_in(command: str) -> bool:
    return os.path.isabs(command) and not os.path.realpath(command).startswith(
        _SYSTEM_DIRECTORIES
    )


def r2_files(directory: Path) -> tuple[Path, Path, Path, Path]:
    """Return the script's own files: its helper copy, the script, its status, its output."""
    return (
        directory / "helper.py",
        directory / "r2.sh",
        directory / "status",
        directory / "log",
    )


def start_r2(
    root: Path,
    backup: Path,
    *,
    directory: Path,
    service: str,
    provisioning: bool,
    shell: str = "/bin/sh",
    python: str = "",
    init: str = "init",
    pidof: str = "pidof",
    stop_wait: int = R2_STOP_WAIT_SECONDS,
    restore_limit: int = R2_RESTORE_LIMIT_SECONDS,
) -> int:
    """Write the stop-and-restore script into its own directory and start it detached.

    A new session is a new process group with no controlling terminal: a signal the
    SSH server sends the command's group when its connection closes - which one, if
    any, dropbear sends is not measured - does not reach it, and neither does the end of
    the Python process that started it. Its output goes to a file from its first line,
    so a vanished reader cannot kill it with SIGPIPE either. Returns its pid.

    `init` and `pidof` are the receiver's own unless a test names stand-ins, by absolute
    path: busybox's shell runs its built-in applets of those names before anything on
    `PATH`, so a stand-in found through `PATH` would not be the one that runs.
    """
    if os.environ.get(TEST_GUARD) and not (_stand_in(init) and _stand_in(pidof)):
        raise RuntimeError(
            "refusing to run the system's own init or pidof under the test suite; "
            "name stand-ins by absolute path"
        )
    if service and not SERVICE_REF.fullmatch(service):
        raise ValueError("not a service reference that fits on one settings line")
    directory.mkdir(mode=0o700)
    helper, script, status, log = r2_files(directory)
    shutil.copyfile(Path(__file__).resolve(), helper)
    text = R2_SCRIPT.format(
        python=shlex.quote(python or sys.executable or "python3"),
        helper=shlex.quote(str(helper)),
        root=shlex.quote(str(root)),
        backup=shlex.quote(str(backup)),
        service=shlex.quote(service),
        provisioning="--provisioning" if provisioning else "''",
        status=shlex.quote(str(status)),
        stop_wait=int(stop_wait),
        restore_limit=int(restore_limit),
        init=shlex.quote(init),
        pidof=shlex.quote(pidof),
    )
    descriptor = os.open(str(script), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.close(os.open(str(status), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    output = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
    try:
        process = subprocess.Popen(
            [shell, str(script)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            close_fds=True,
            start_new_session=True,
            cwd="/",
        )
    finally:
        os.close(output)
    return process.pid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=(
            "snapshot",
            "restore",
            "withdraw",
            "verify",
            "claim",
            "release",
            "prune",
            "identity",
            "lock-probe",
            "record",
            "lastservice",
            "zap",
            "powerstate",
            "r2-start",
        ),
    )
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--root", type=Path, default=Path("/"))
    # The snapshot of the transaction that is committing, which `prune` keeps whatever
    # the receiver's clock says about it.
    parser.add_argument("--keep-name", default="")
    parser.add_argument("--provisioning", action="store_true")
    parser.add_argument("--settings", action="store_true")
    # The transaction's id, written into the lock's owner record by `claim`.
    parser.add_argument("--id", default="")
    # A service reference is an option, never the positional path: `Path` would fold the
    # `//` of an IPTV reference's stream URL into one slash.
    parser.add_argument("--service", default="")
    parser.add_argument("--state", type=int, default=0)
    parser.add_argument("--webif", default=WEBIF_URL)
    # The stop-and-restore script's own directory, which it never shares.
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--stop-wait", type=int, default=R2_STOP_WAIT_SECONDS)
    parser.add_argument("--restore-limit", type=int, default=R2_RESTORE_LIMIT_SECONDS)
    parser.add_argument("--shell", default="/bin/sh")
    # The interpreter the stop-and-restore script runs this helper with; the one running
    # it now unless said otherwise.
    parser.add_argument("--python", default="")
    parser.add_argument("--init", default="init")
    parser.add_argument("--pidof", default="pidof")
    args = parser.parse_args()
    if args.operation == "identity":
        print(json.dumps(read_identity(args.root), sort_keys=True))
    elif args.operation == "lock-probe":
        # The same status a busy snapshot or restore exits with: "opkg is busy".
        return 0 if probe_opkg_lock(args.root) else EXIT_OPKG_BUSY
    elif args.operation == "record":
        print(json.dumps(record_state(args.root, args.webif), sort_keys=True))
    elif args.operation == "lastservice":
        write_lastservice(args.root, args.service)
    elif args.operation == "zap":
        return 0 if zap(args.service, args.webif) else 1
    elif args.operation == "powerstate":
        # Always 0: whether the receiver restarted is read from its pid, not from here.
        print(json.dumps({"answered": power_state(args.state, args.webif)}))
    elif args.path is None:
        parser.error("path is required for this operation")
    elif args.operation == "claim":
        print(json.dumps({"reclaimed": claim_transaction(args.path, args.id)}))
    elif args.operation == "release":
        release_transaction(args.path)
    elif args.operation == "prune":
        for name in prune_snapshots(args.path, keep_name=args.keep_name):
            print(f"pruned superseded installer snapshot {name}", file=sys.stderr)
    elif args.operation == "r2-start":
        if args.dir is None:
            parser.error("r2-start needs --dir")
        pid = start_r2(
            args.root,
            args.path,
            directory=args.dir,
            service=args.service,
            provisioning=args.provisioning,
            shell=args.shell,
            python=args.python,
            init=args.init,
            pidof=args.pidof,
            stop_wait=args.stop_wait,
            restore_limit=args.restore_limit,
        )
        print(json.dumps({"pid": pid}))
    elif args.operation in ("snapshot", "restore", "withdraw"):
        if args.operation == "withdraw" and args.settings:
            # Withdrawing runs under a live enigma2, which rewrites the whole settings
            # file from memory on its next clean quit: a settings block written now
            # would be lost, and could only race the household's own saves.
            parser.error("withdraw never restores the settings block")
        try:
            if args.operation == "snapshot":
                snapshot(args.root, args.path)
            else:
                restore(
                    args.root,
                    args.path,
                    args.provisioning,
                    args.settings and args.operation == "restore",
                )
        except OpkgBusyError as error:
            # Its own exit status and one line, because this is the one failure the
            # person at the other end can do something about: wait, and press again.
            print(str(error), file=sys.stderr)
            return EXIT_OPKG_BUSY
        except OpkgLockLostError as error:
            print(str(error), file=sys.stderr)
            return EXIT_OPKG_LOCK_LOST
    else:
        verify_manifest(args.root, args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

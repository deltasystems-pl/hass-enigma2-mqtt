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
import shutil
import stat
import sys
import tempfile
import time
from typing import NamedTuple

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
# containing nothing but `alternatives/` — so a hard-coded `/usr/lib/opkg/status` found
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
# used to lock — so for as long as it did, it excluded nothing. Older opkg releases were
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
# seconds is a worse install than one that starts late — but never past the installer's
# command timeout, because a helper still blocked after the installer gave up would take
# the lock later, from under nobody. The bound is the whole wait, across every lock file
# taken, not a wait per file. The snapshot runs under a 30 s command and the restore
# under a 60 s one; a rollback that gives up is the worse outcome, so it waits longer.
OPKG_LOCK_WAIT_SNAPSHOT_SECONDS = 20.0
OPKG_LOCK_WAIT_RESTORE_SECONDS = 40.0
OPKG_LOCK_POLL_SECONDS = 0.25
# opkg lets go in three steps — unlock, close, delete the file — so a lock granted
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
    for in one place and then the other — and both halves are taken from the same one,
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
    also deletes the file when it lets go — after unlocking and closing it. A process
    granted the lock in that gap holds it on a file whose name is about to go, and the
    next opkg creates a fresh file and locks that one unopposed. So a lock only counts
    once the name still leads to the locked file, and still does after a pause longer
    than opkg's unlock, close and delete take; otherwise it is taken again.

    Only "somebody else holds it" is waited on. Any other failure to lock — a kernel
    or filesystem without record locks answers `ENOLCK` — is not going to clear by
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
    """Let go the way opkg does — remove the file, then close it.

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
    with _lock(root, OPKG_LOCK_WAIT_RESTORE_SECONDS):
        current = _stanzas(status_path.read_text(encoding="utf-8"))
        current = [stanza for stanza in current if _package_name(stanza) != PACKAGE]
        if metadata["package_status"]:
            current.append((backup / "package-status").read_text(encoding="utf-8").strip())
        _atomic_text(status_path, "\n\n".join(current) + "\n")

        for name in glob.glob(str(info_dir / f"{PACKAGE}.*")):
            candidate = Path(name)
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            else:
                candidate.unlink(missing_ok=True)
        if metadata["opkg_info"]:
            for source in info_sources:
                shutil.copy2(source, info_dir / source.name)

        if plugin.exists():
            shutil.rmtree(plugin)
        if metadata["plugin"]:
            shutil.copytree(backup / "plugin", plugin, symlinks=True)

        if metadata["webif_shim"]:
            webif_shim.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / "webif-shim", webif_shim)
        else:
            webif_shim.unlink(missing_ok=True)
        for cache_file in live_cache_files:
            cache_file.unlink()
        if metadata["webif_cache"]:
            webif_shim.parent.mkdir(parents=True, exist_ok=True)
            cache_dir = webif_shim.parent / "__pycache__"
            for source in cache_sources:
                # The name says which of the two locations it came out of: exactly
                # `MQTTBridge.pyc` is the legacy sibling, anything else is a cache tag.
                if source.name == WEBIF_LEGACY_BYTECODE:
                    shutil.copy2(source, webif_shim.parent / source.name)
                    continue
                cache_dir.mkdir(exist_ok=True)
                shutil.copy2(source, cache_dir / source.name)

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
            provision = _path(root, PROVISION)
            if metadata["provisioning"]:
                shutil.copy2(backup / "provisioning", provision)
            else:
                provision.unlink(missing_ok=True)


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
    `enigma2` for a box that had said nothing — the right answer, from a copy of the
    plugin's defaults kept on the wrong side of the link. Applying the defaults is the
    caller's job: they belong to the plugin, they are worth having in exactly one
    place, and this half of the installer runs on the receiver with no way to import
    anything of Home Assistant's.

    `friendly_name` binds nothing and is reported for the opposite reason: it is the
    name the household reads, and an install whose name field was left empty leaves
    the receiver called this, so it is the only way for the caller to know what the box
    it has just configured is called. Raw like the rest — an empty string is a receiver
    that stores an empty name, and `null` a receiver that stores none — because the
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
        # claim that has not got to it yet — a matter of milliseconds — or one that died
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


def claim_transaction(lock_dir: Path) -> None:
    """Claim the durable per-receiver transaction lock or fail busy.

    Reclaiming a stale lock is a rename, not a delete in place. Deciding that a lock is
    stale and then writing into it is check-then-act: two claimers can both decide it,
    both write, and both believe they hold it, and then two transactions unwind one
    receiver. A rename has exactly one winner.

    The verdict is then reached a second time, on the directory the rename actually took
    away. The first verdict can be overtaken — between judging and renaming, another
    claimer can have reclaimed the lock and started an install — and the second verdict
    is the one made while nobody else can touch the directory. A lock that turns out to
    be alive is put straight back and this claimer is told the receiver is busy.
    """
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
        shutil.rmtree(retired, ignore_errors=True)
        lock_dir.mkdir(mode=0o700, parents=False)
        print(f"reclaiming stale installer lock: {reason}", file=sys.stderr)
    _write_owner(lock_dir)


def _write_owner(lock_dir: Path) -> None:
    """Record who holds the lock, under which boot, and at what point in that boot."""
    _atomic_text(
        lock_dir / "owner.json",
        json.dumps(
            {
                "pid": os.getpid(),
                "started": int(time.time()),
                "boot_id": boot_id(),
                "uptime": uptime(),
            }
        )
        + "\n",
    )


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

    A snapshot is the only way back from an install, so one is kept — the one taken by
    the install that just succeeded — and one more behind it. Without this, every guided
    install left another copy of the plugin directory on the receiver's flash for ever.

    `keep_name` is that install's own snapshot, and it is never removed whatever its
    timestamp says. The order here is the modification time, and on these receivers a
    timestamp is not a clock: many have no battery-backed one, boot in 1970 and jump to
    the real time when NTP answers — which can be after the snapshot was taken. Ranking
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=("snapshot", "restore", "verify", "claim", "release", "prune", "identity"),
    )
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--root", type=Path, default=Path("/"))
    # The snapshot of the transaction that is committing, which `prune` keeps whatever
    # the receiver's clock says about it.
    parser.add_argument("--keep-name", default="")
    parser.add_argument("--provisioning", action="store_true")
    parser.add_argument("--settings", action="store_true")
    args = parser.parse_args()
    if args.operation == "identity":
        print(json.dumps(read_identity(args.root), sort_keys=True))
    elif args.path is None:
        parser.error("path is required for this operation")
    elif args.operation == "claim":
        claim_transaction(args.path)
    elif args.operation == "release":
        release_transaction(args.path)
    elif args.operation == "prune":
        for name in prune_snapshots(args.path, keep_name=args.keep_name):
            print(f"pruned superseded installer snapshot {name}", file=sys.stderr)
    elif args.operation in ("snapshot", "restore"):
        try:
            if args.operation == "snapshot":
                snapshot(args.root, args.path)
            else:
                restore(args.root, args.path, args.provisioning, args.settings)
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

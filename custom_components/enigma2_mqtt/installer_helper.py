"""Receiver-side filesystem transaction helper used by the SSH installer.

This module intentionally uses only Python 3.9's standard library. The installer
streams this exact file to the receiver after preflight, and its tests execute the
same functions against a temporary root.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time

PACKAGE = "enigma2-plugin-extensions-mqttbridge"
PLUGIN_DIR = "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
OPKG_STATUS = "usr/lib/opkg/status"
OPKG_INFO = "usr/lib/opkg/info"
SETTINGS = "etc/enigma2/settings"
PROVISION = "etc/enigma2/mqttbridge.json"
LOCK = "var/lock/opkg.lock"
SETTINGS_PREFIX = "config.plugins.mqttbridge."


def _path(root: Path, relative: str) -> Path:
    return root / relative


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


def _lock(root: Path):
    lock_path = _path(root, LOCK)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    # opkg uses a POSIX record lock on this file. `lockf`, unlike `flock`, joins
    # that lock domain.
    fcntl.lockf(handle.fileno(), fcntl.LOCK_EX)
    return handle


def snapshot(root: Path, backup: Path) -> dict[str, bool]:
    """Snapshot only files owned or consumed by this package."""
    if backup.exists():
        raise FileExistsError(backup)
    backup.mkdir(mode=0o700, parents=True)
    with _lock(root):
        plugin = _path(root, PLUGIN_DIR)
        status_path = _path(root, OPKG_STATUS)
        if not status_path.is_file():
            raise FileNotFoundError(status_path)
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
        info_backup = backup / "opkg-info"
        info_files = [
            Path(name) for name in glob.glob(str(_path(root, OPKG_INFO) / f"{PACKAGE}.*"))
        ]
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
            "plugin": plugin.is_dir(),
            "package_status": package_stanza is not None,
            "opkg_info": bool(info_files),
            "provisioning": provision.is_file(),
            "settings": settings_path.is_file(),
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
    expected = {"plugin", "package_status", "opkg_info", "provisioning", "settings"}
    if set(metadata) != expected or any(not isinstance(metadata[key], bool) for key in expected):
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
    if any(not path.exists() for path in required):
        raise ValueError("snapshot is incomplete")
    status_path = _path(root, OPKG_STATUS)
    info_dir = _path(root, OPKG_INFO)
    plugin = _path(root, PLUGIN_DIR)
    if not status_path.is_file() or status_path.is_symlink():
        raise ValueError("opkg status is unavailable or unsafe")
    if not info_dir.is_dir() or info_dir.is_symlink():
        raise ValueError("opkg metadata directory is unavailable or unsafe")
    if plugin.is_symlink():
        raise ValueError("live plugin directory may not be a symlink")
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
    with _lock(root):
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
    """Read only the non-secret settings which bind an HA entry to this box."""
    settings = _path(root, SETTINGS)
    values: dict[str, str] = {}
    if settings.is_file() and not settings.is_symlink():
        for line in settings.read_text(encoding="utf-8").splitlines():
            if not line.startswith(SETTINGS_PREFIX) or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values[name.removeprefix(SETTINGS_PREFIX)] = value
    return {
        "node_id": values.get("node_id", ""),
        "base_topic": values.get("base_topic", "enigma2").strip("/"),
        "enabled": values.get("enabled", "true").lower() == "true",
        "ha_mode": values.get("ha_mode", "discovery"),
    }


def claim_transaction(lock_dir: Path) -> None:
    """Claim the durable per-receiver transaction lock or fail busy."""
    lock_dir.mkdir(mode=0o700, parents=False)
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": os.getpid(), "started": int(time.time())}) + "\n",
        encoding="ascii",
    )


def release_transaction(lock_dir: Path) -> None:
    """Release only a well-formed installer transaction lock."""
    owner = lock_dir / "owner.json"
    if not owner.is_file():
        raise ValueError("transaction lock has no owner")
    owner.unlink()
    lock_dir.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation", choices=("snapshot", "restore", "verify", "claim", "release", "identity")
    )
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--root", type=Path, default=Path("/"))
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
    elif args.operation == "snapshot":
        snapshot(args.root, args.path)
    elif args.operation == "restore":
        restore(args.root, args.path, args.provisioning, args.settings)
    else:
        verify_manifest(args.root, args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

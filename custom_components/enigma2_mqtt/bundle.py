"""Validated access to the receiver plugin bundled with this integration."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tarfile
from typing import Any

_BUNDLE_DIR = Path(__file__).with_name("bundled")
_METADATA = _BUNDLE_DIR / "metadata.json"
_METADATA_LIMIT = 16 * 1024
_IPK_LIMIT = 8 * 1024 * 1024
_SOURCE_LIMIT = 16 * 1024 * 1024
_SOURCE_EXPANDED_LIMIT = 64 * 1024 * 1024
_SOURCE_MEMBER_LIMIT = 5000
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FIELDS = {
    "schema",
    "filename",
    "version",
    "sha256",
    "size",
    "source_filename",
    "source_sha256",
    "source_size",
    "source_commit",
    "source_url",
    "license",
}


class BundleError(RuntimeError):
    """The packaged plugin cannot be trusted or read."""


@dataclass(frozen=True)
class BundledPlugin:
    """A validated receiver package ready for the installer to re-read."""

    path: Path
    version: str
    sha256: str
    source_commit: str


def _fail() -> BundleError:
    return BundleError("the bundled receiver plugin is missing or invalid")


def _regular_child(name: Any, maximum: int, expected_size: Any) -> Path:
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise _fail()
    path = _BUNDLE_DIR / name
    try:
        stat = path.lstat()
    except OSError as error:
        raise _fail() from error
    if path.is_symlink() or not path.is_file():
        raise _fail()
    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        raise _fail()
    if expected_size <= 0 or expected_size > maximum or stat.st_size != expected_size:
        raise _fail()
    return path


def _digest(path: Path, maximum: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(128 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise _fail()
                digest.update(chunk)
    except OSError as error:
        raise _fail() from error
    return digest.hexdigest()


def _metadata() -> dict[str, Any]:
    try:
        if _METADATA.is_symlink() or _METADATA.stat().st_size > _METADATA_LIMIT:
            raise _fail()
        raw = _METADATA.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError, TypeError) as error:
        raise _fail() from error
    schema = value.get("schema") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != _FIELDS
        or isinstance(schema, bool)
        or schema != 1
    ):
        raise _fail()
    return value


def _validate_source_archive(path: Path, source_commit: str) -> None:
    prefix = f"enigma2-mqtt-bridge-{source_commit}/"
    expanded = 0
    license_found = False
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > _SOURCE_MEMBER_LIMIT:
                raise _fail()
            for member in members:
                in_tree = member.name == prefix.rstrip("/") or member.name.startswith(prefix)
                if not in_tree or member.issym() or member.islnk():
                    raise _fail()
                expanded += member.size
                if expanded > _SOURCE_EXPANDED_LIMIT:
                    raise _fail()
                if member.name == prefix + "LICENSE" and member.isfile():
                    license_found = True
    except (OSError, tarfile.TarError) as error:
        raise _fail() from error
    if not license_found:
        raise _fail()


def load_bundled_plugin() -> BundledPlugin:
    """Return the bundled IPK only after verifying it and its corresponding source."""
    metadata = _metadata()
    version = metadata.get("version")
    sha256 = metadata.get("sha256")
    source_sha256 = metadata.get("source_sha256")
    source_commit = metadata.get("source_commit")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise _fail()
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise _fail()
    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
        raise _fail()
    if not isinstance(source_commit, str) or _COMMIT.fullmatch(source_commit) is None:
        raise _fail()
    if metadata.get("license") != "GPL-2.0-or-later":
        raise _fail()
    expected_package = f"enigma2-plugin-extensions-mqttbridge_{version}_all.ipk"
    expected_source = f"enigma2-mqtt-bridge-{source_commit}.tar.gz"
    expected_url = (
        "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/tree/" + source_commit
    )
    if metadata.get("filename") != expected_package:
        raise _fail()
    if metadata.get("source_filename") != expected_source:
        raise _fail()
    if metadata.get("source_url") != expected_url:
        raise _fail()

    package = _regular_child(metadata.get("filename"), _IPK_LIMIT, metadata.get("size"))
    source = _regular_child(
        metadata.get("source_filename"), _SOURCE_LIMIT, metadata.get("source_size")
    )
    if _digest(package, _IPK_LIMIT) != sha256 or _digest(source, _SOURCE_LIMIT) != source_sha256:
        raise _fail()
    _validate_source_archive(source, source_commit)
    return BundledPlugin(package, version, sha256, source_commit)

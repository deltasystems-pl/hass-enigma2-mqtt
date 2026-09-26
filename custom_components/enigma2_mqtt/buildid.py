"""Which build of the plugin a version string stands for, and which of two builds is newer.

A version number says which release a build claims to be, not which code it is. A development
build of the commit after 0.3.0 still says 0.3.0 until the plugin's release pull request bumps
it, so a receiver running it and one running the release looked identical here, and the update
card called both of them current. From the release after 0.3.0 the plugin publishes a **build
id** on `info.build` - the commit, its time, whether the tracked files matched it, and the
build's flavour - and every package carries the same values in its `buildinfo.py`, which is how
the bundled package is read here too.

**The display rule** is the plugin's (`buildid.display_version`, TOPICS.md "How to show the
version"), with one addition only this side can make: `0.3.0` for a release build - flavour
`release`, a clean tree, and, where the signed index or the bundle names that version's commit,
that commit - and for a build whose commit nobody knows (every plugin up to 0.3.x); `0.3.0+g<sha7>`
for everything else, with `.dirty` when the tracked files differed. The part after `+` is a local
label, never a pre-release: opkg and PEP 440 disagree about how `0.4.0rc1` sorts against `0.4.0`,
and nothing here orders versions by the label.

**The order is the release number, and nothing else** (decided 2026-09-26, ADR-0008 section 3).
A higher `N.N.N` is newer. The same `N.N.N` is never newer, in either direction: a development
build of 0.3.0 may be newer or older code than the 0.3.0 release, so neither is offered over the
other as an update - that would be a one-click downgrade for somebody running a fix. A
development build is still never shown as current: the display says what it is, and the card
says that the release is available. A same-number build is installed only when somebody asks for
it by name - the version select, or `update.install` with that version. The commit time is kept
as information and orders nothing.

An unknown build of a version the signed index lists is given that release's commit time, for
the attributes: it displays as the release.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import gzip
import io
import re
import tarfile
from typing import Any

RELEASE = "release"

_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_PLAIN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)

# Where the package carries its build id, and how much of it is believed. The file is a few
# lines of literals; anything bigger is not the file the plugin's builder writes.
BUILDINFO_MEMBER = "./usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/buildinfo.py"
_BUILDINFO_LIMIT = 16 * 1024
_DATA_MEMBER_LIMIT = 8 * 1024 * 1024
_EXPANDED_LIMIT = 64 * 1024 * 1024
_BUILDINFO_NAMES = {
    "COMMIT": "commit",
    "COMMIT_TIME": "time",
    "DIRTY": "dirty",
    "FLAVOUR": "flavour",
}


class BuildInfoError(ValueError):
    """A package whose build id cannot be read as the plugin's builder writes it."""


@dataclass(frozen=True)
class Build:
    """One build of the plugin, as far as anybody can tell."""

    version: str
    commit: str = ""
    time: int | None = None
    dirty: bool | None = None
    flavour: str | None = None
    # The commit the signed index (or the bundle) names for this version, when it names one.
    release_commit: str | None = None

    @property
    def known(self) -> bool:
        """Whether the build names its commit at all."""
        return bool(self.commit)

    @property
    def is_release(self) -> bool:
        """Whether this is the release of its version - or a build nobody can tell from it."""
        if not self.known:
            return True
        if self.flavour != RELEASE or self.dirty is not False:
            return False
        return self.release_commit is None or self.release_commit == self.commit

    @property
    def display(self) -> str:
        """`0.3.0` for a release build or an unknown one, else `0.3.0+g<sha7>[.dirty]`."""
        if self.is_release:
            return self.version
        marker = ".dirty" if self.dirty is True else ""
        return f"{self.version}+g{self.commit[:7]}{marker}"


def validated(values: Any) -> dict[str, Any] | None:
    """`{commit, time, dirty, flavour}` from an `info.build` payload, or None when unusable.

    Lenient where the contract is lenient - `time`, `dirty` and `flavour` may be `null` - and
    strict where a wrong value would change the display: a commit is 40 lowercase hex digits or
    empty. A flavour this integration does not know is kept and read as "not a release".
    """
    if not isinstance(values, dict):
        return None
    commit = values.get("commit")
    if not isinstance(commit, str) or (commit and not _COMMIT.fullmatch(commit)):
        return None
    when = values.get("time")
    if isinstance(when, bool) or not isinstance(when, int) or when <= 0:
        when = None
    dirty = values.get("dirty")
    if not isinstance(dirty, bool):
        dirty = None
    flavour = values.get("flavour")
    if not isinstance(flavour, str) or not flavour:
        flavour = None
    return {"commit": commit, "time": when, "dirty": dirty, "flavour": flavour}


def build_of(
    version: str,
    values: Any,
    *,
    release_commit: str | None = None,
    release_time: int | None = None,
) -> Build:
    """The build a receiver or a package reports, held against the release's commit and time.

    `release_commit` and `release_time` come from the signed index's entry for `version`, or
    from the bundle when it is that version's release build. An unknown build borrows the
    release's time, because it displays - and is compared - as that release.
    """
    build = validated(values)
    if build is None or not build["commit"]:
        return Build(version=version, time=release_time, release_commit=release_commit)
    return Build(
        version=version,
        commit=build["commit"],
        time=build["time"],
        dirty=build["dirty"],
        flavour=build["flavour"],
        release_commit=release_commit,
    )


def base_version(value: str | None) -> tuple[int, int, int] | None:
    """The `N.N.N` in front of a display string, as numbers, or None when there is none."""
    if not isinstance(value, str):
        return None
    match = _PLAIN.fullmatch(value.split("+", 1)[0])
    if match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def is_newer(latest: Build, installed: Build) -> bool:
    """Whether `latest` has a higher release number than `installed` - and nothing else."""
    latest_base = base_version(latest.version)
    installed_base = base_version(installed.version)
    if latest_base is None or installed_base is None:
        return False
    return latest_base > installed_base


def same_number(one: Build, other: Build) -> bool:
    """Whether two builds carry the same release number."""
    base = base_version(one.version)
    return base is not None and base == base_version(other.version)


# ------------------------------------------------------------------ reading a package --


def _ar_members(package: bytes) -> dict[str, bytes]:
    """The members of an `ar` archive - an IPK is one - by name."""
    if not package.startswith(b"!<arch>\n"):
        raise BuildInfoError("not an ar archive")
    members: dict[str, bytes] = {}
    offset = 8
    while offset < len(package):
        header = package[offset : offset + 60]
        if len(header) < 60 or header[58:60] != b"`\n":
            raise BuildInfoError("a truncated ar header")
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as error:
            raise BuildInfoError("an ar member without a size") from error
        start = offset + 60
        if size < 0 or start + size > len(package):
            raise BuildInfoError("an ar member runs past the archive")
        members[name] = package[start : start + size]
        offset = start + size + (size & 1)
    return members


def parse_buildinfo(text: str) -> dict[str, Any] | None:
    """The build id in a `buildinfo.py`'s text, read as literals and never run."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in _BUILDINFO_NAMES:
            try:
                values[_BUILDINFO_NAMES[target.id]] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                return None
    if set(values) != set(_BUILDINFO_NAMES.values()):
        return None
    return validated(values)


def package_build(package: bytes) -> dict[str, Any] | None:
    """The build id a plugin package carries, or None when it carries none (0.3.x and older).

    Raises BuildInfoError when the package has a build id that is not one: a `buildinfo.py`
    that cannot be read is a package that is not what the plugin's builder makes.
    """
    data = _ar_members(package).get("data.tar.gz")
    if data is None:
        raise BuildInfoError("the package has no data.tar.gz")
    if len(data) > _DATA_MEMBER_LIMIT:
        raise BuildInfoError("data.tar.gz is too large")
    try:
        with (
            gzip.GzipFile(fileobj=io.BytesIO(data)) as unzipped,
            tarfile.open(fileobj=unzipped, mode="r|") as archive,
        ):
            expanded = 0
            for member in archive:
                expanded += member.size
                if expanded > _EXPANDED_LIMIT:
                    raise BuildInfoError("data.tar.gz expands too far")
                if member.name.removeprefix("./") != BUILDINFO_MEMBER.removeprefix("./"):
                    continue
                if not member.isfile() or member.size > _BUILDINFO_LIMIT:
                    raise BuildInfoError("buildinfo.py is not a small regular file")
                handle = archive.extractfile(member)
                if handle is None:
                    raise BuildInfoError("buildinfo.py cannot be read")
                text = handle.read().decode("utf-8")
                build = parse_buildinfo(text)
                if build is None:
                    raise BuildInfoError("buildinfo.py is not a build id")
                return build
    except (OSError, EOFError, tarfile.TarError, UnicodeDecodeError) as error:
        raise BuildInfoError(f"data.tar.gz cannot be read: {error}") from error
    return None

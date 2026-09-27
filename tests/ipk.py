"""A minimal plugin package, built the way the plugin's `tools/build-ipk.sh` lays one out.

An IPK is an `ar` archive of `debian-binary`, `control.tar.gz` and `data.tar.gz`. The tests need
packages with and without a build id, and with a broken one, and building them is shorter than
keeping binary fixtures that nobody can read in a diff.
"""

from __future__ import annotations

import io
import tarfile

BUILDINFO_PATH = "./usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/buildinfo.py"


def buildinfo(
    commit: str = "c" * 40,
    time: int = 1790400000,
    dirty: bool = False,
    flavour: str = "release",
) -> str:
    """The text the plugin's `tools/make-buildinfo.py` renders."""
    return (
        '"""What this build of the plugin is."""\n\n'
        f'COMMIT = "{commit}"\n'
        f"COMMIT_TIME = {time}\n"
        f"DIRTY = {dirty}\n"
        f'FLAVOUR = "{flavour}"\n'
    )


def _tar_gz(files: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 1790400000
            archive.addfile(info, io.BytesIO(content))
    return raw.getvalue()


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, content in members:
        header = (
            f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(content):<10}".encode("ascii")
            + b"`\n"
        )
        out += header + content
        if len(content) & 1:
            out += b"\n"
    return bytes(out)


def make_ipk(build: str | None = None, *, extra: dict[str, bytes] | None = None) -> bytes:
    """A package whose data carries `build` as its `buildinfo.py`, or no build id at all."""
    files = {"./usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py": b"# plugin\n"}
    if build is not None:
        files[BUILDINFO_PATH] = build.encode("utf-8")
    files.update(extra or {})
    return _ar(
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", _tar_gz({"./control": b"Package: x\n"})),
            ("data.tar.gz", _tar_gz(files)),
        ]
    )

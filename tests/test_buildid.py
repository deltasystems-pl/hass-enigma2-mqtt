"""Which build a version string stands for, and which of two builds is newer.

The display rule is the plugin's (TOPICS.md, "How to show the version"), so the cases here are
the plugin's own: a release build and an unknown one show the plain version, everything else
`N.N.N+g<sha7>`, and `.dirty` when the tree was not clean. The order is this integration's, and
item 9's point is its first line: a development build is never current against the release of
its own number.
"""

from __future__ import annotations

import pytest

from custom_components.enigma2_mqtt.buildid import (
    Build,
    BuildInfoError,
    base_version,
    build_of,
    is_newer,
    package_build,
    parse_buildinfo,
    validated,
)

from .ipk import buildinfo, make_ipk

RELEASE_COMMIT = "26c996bcfa6cecc3ad8540f3fe2a86d74862c93b"
DEV_COMMIT = "46ea0e3" + "0" * 33
CANDIDATE_COMMIT = "abc1234" + "1" * 33


def _payload(commit: str, *, time: int = 1790400000, dirty=False, flavour="development"):
    return {"commit": commit, "time": time, "dirty": dirty, "flavour": flavour, "on_disk": None}


# ------------------------------------------------------------------------- display --


@pytest.mark.parametrize(
    ("payload", "release_commit", "shown"),
    [
        # Every plugin up to 0.3.x: no build id, shown as the version it reports.
        (None, None, "0.3.0"),
        ({"commit": "", "time": None, "dirty": None, "flavour": None}, None, "0.3.0"),
        (_payload(RELEASE_COMMIT, flavour="release"), None, "0.3.0"),
        (_payload(RELEASE_COMMIT, flavour="release"), RELEASE_COMMIT, "0.3.0"),
        # A release flavour the signed index does not recognise as the release is not one.
        (_payload(DEV_COMMIT, flavour="release"), RELEASE_COMMIT, "0.3.0+g46ea0e3"),
        (_payload(DEV_COMMIT), None, "0.3.0+g46ea0e3"),
        (_payload(DEV_COMMIT, dirty=True), None, "0.3.0+g46ea0e3.dirty"),
        (_payload(RELEASE_COMMIT, flavour="release", dirty=True), None, "0.3.0+g26c996b.dirty"),
        # A dirty of null says nothing - so it is not a clean release either.
        (_payload(RELEASE_COMMIT, flavour="release", dirty=None), None, "0.3.0+g26c996b"),
        (_payload(DEV_COMMIT, flavour="acceptance"), None, "0.3.0+g46ea0e3"),
        # A flavour nobody knows is "not a release".
        (_payload(DEV_COMMIT, flavour="nightly"), None, "0.3.0+g46ea0e3"),
    ],
)
def test_the_display_rule(payload, release_commit, shown) -> None:
    assert build_of("0.3.0", payload, release_commit=release_commit).display == shown


@pytest.mark.parametrize(
    "payload",
    [
        {"commit": "NOT-HEX", "time": 1, "dirty": False, "flavour": "release"},
        {"commit": "abc", "time": 1, "dirty": False, "flavour": "release"},
        {"commit": 7},
        "a string",
        [],
    ],
)
def test_a_build_id_that_is_not_one_is_an_unknown_build(payload) -> None:
    """A malformed build id is read as none, never as a wrong name for the build."""
    assert validated(payload) is None
    assert build_of("0.3.0", payload).display == "0.3.0"


def test_an_unknown_build_borrows_the_release_s_time() -> None:
    """It shows as the release, so it is compared as the release."""
    assert build_of("0.3.0", None, release_time=123).time == 123
    assert build_of("0.3.0", _payload(DEV_COMMIT, time=456), release_time=123).time == 456


# --------------------------------------------------------------------------- order --


def _dev(time: int, commit: str = DEV_COMMIT, version: str = "0.3.0") -> Build:
    return Build(version=version, commit=commit, time=time, dirty=False, flavour="development")


def _release(time: int | None, version: str = "0.3.0") -> Build:
    return Build(
        version=version, commit=RELEASE_COMMIT, time=time, dirty=False, flavour="release"
    )


@pytest.mark.parametrize(
    ("latest", "installed", "newer"),
    [
        # Release numbers, and nothing else (decided 2026-09-26, ADR-0008 section 3).
        (_release(1, "0.4.0"), _release(2), True),
        (_release(2), _release(1, "0.4.0"), False),
        (_dev(1, version="0.4.0"), _release(9), True),
        (_release(1, "0.4.0"), _dev(9), True),
        (Build(version="0.10.0"), Build(version="0.9.0"), True),
        # The same number never badges, in either direction and whichever commit is later:
        # a development build may be newer or older code than the release of its number.
        (_release(1), _dev(2), False),
        (_release(3), _dev(2), False),
        (Build(version="0.3.0", time=1), _dev(2), False),
        (_dev(2, CANDIDATE_COMMIT), _release(1), False),
        (_dev(1, CANDIDATE_COMMIT), _release(2), False),
        (_dev(2, CANDIDATE_COMMIT), _dev(1), False),
        (Build(version="0.3.0", commit=CANDIDATE_COMMIT, dirty=True), _release(1), False),
        (_release(1), _release(1), False),
        # A version that does not sort is not a comparison.
        (Build(version="nightly"), _release(1), False),
        (_release(1), Build(version="nightly"), False),
    ],
)
def test_the_order(latest: Build, installed: Build, newer: bool) -> None:
    assert is_newer(latest, installed) is newer


def test_the_label_is_never_ordered_by() -> None:
    """`+g...` is a local label: `0.3.0+gffffff0` is not "above" `0.3.0` for being longer."""
    assert base_version("0.3.0+gffffff0.dirty") == (0, 3, 0)
    assert base_version("0.3.0") == (0, 3, 0)
    assert base_version("0.3") is None
    assert base_version("v0.3.0") is None
    assert base_version(None) is None


# ---------------------------------------------------------------- reading a package --


def test_a_package_s_build_id_is_read_as_literals() -> None:
    assert package_build(make_ipk(buildinfo(commit=RELEASE_COMMIT))) == {
        "commit": RELEASE_COMMIT,
        "time": 1790400000,
        "dirty": False,
        "flavour": "release",
    }


def test_a_package_without_a_build_id_has_none() -> None:
    assert package_build(make_ipk(None)) is None


@pytest.mark.parametrize(
    "package",
    [
        b"not an ar archive",
        b"!<arch>\n" + b"x" * 30,
        make_ipk(buildinfo()).replace(b"data.tar.gz", b"data.tar.xz"),
        make_ipk("COMMIT = __import__('os').getcwd()\n"),
        make_ipk(buildinfo() + "x" * 20000),
    ],
    ids=["not ar", "truncated header", "no data member", "not literals", "too large"],
)
def test_a_package_that_is_not_what_the_builder_makes_is_refused(package: bytes) -> None:
    with pytest.raises(BuildInfoError):
        package_build(package)


def test_the_text_the_plugin_s_builder_renders_parses() -> None:
    text = (
        '"""What this build of the plugin is."""\n\n'
        f'COMMIT = "{RELEASE_COMMIT}"\nCOMMIT_TIME = 0\nDIRTY = False\nFLAVOUR = "release"\n'
    )
    # A time of 0 is the builder saying it did not know.
    assert parse_buildinfo(text) == {
        "commit": RELEASE_COMMIT,
        "time": None,
        "dirty": False,
        "flavour": "release",
    }
    assert parse_buildinfo("COMMIT = 1\n") is None

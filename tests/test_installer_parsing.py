"""Everything the installer refuses to believe.

The installer reads four kinds of untrusted text: a version string from opkg, a Python
version from the receiver, OpenWebif's JSON, and an IPK's own archive headers. Each has
a parser that fails closed, because the alternative in every case is worse than stopping:
a version it guesses at reorders an upgrade into a downgrade, a boolean it guesses at
interrupts a recording, and an archive it guesses at writes files outside the package.

These are plain functions, so they are tested as plain functions.
"""

from __future__ import annotations

from io import BytesIO
import json
import tarfile
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.enigma2_mqtt.bundle import BundleError
from custom_components.enigma2_mqtt.installer import (
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    SshCredentials,
    _async_enigma_pids,
    _async_load_bundle,
    _async_validate_receiver_identity,
    _finite_timestamp,
    _installed_hash_manifest,
    _package_version,
    _python_version,
    _run_checked,
    _strict_bool,
    _timer_guard,
    async_preflight,
)

from .test_installer import FakeReceiver, FakeSession, credentials

__all__ = ["credentials"]


class _Fixed:
    """A session that answers everything with one canned result."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result

    async def run(self, command: str, **kwargs: object) -> CommandResult:
        del command, kwargs
        return self.result

    async def close(self) -> None:
        return None


def _ipk(payload: bytes, name: bytes = b"data.tar.gz", size: bytes | None = None) -> bytes:
    """Wrap a payload in a one-member ar archive, the format opkg ships."""
    length = size if size is not None else str(len(payload)).encode()
    header = name.ljust(16) + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
    header += b"100644".ljust(8) + length.ljust(10) + b"\x60\n"
    return b"!<arch>\n" + header + payload


def _tar(*members: tuple[str, bytes], directory: str | None = None) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
        if directory is not None:
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "value", ["", "not-a-version", "1.2.3.4-r", "0.1.0-gamma.1", "1..2", "v1.0", "-1"]
)
def test_an_opkg_version_outside_the_supported_subset_is_refused(value: str) -> None:
    """Versions this parser does not understand would be ordered by guesswork."""
    with pytest.raises(InstallerError) as raised:
        _package_version(value)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


@pytest.mark.parametrize("value", ["", "3", "3.x", "three.twelve", " ", "3.12.8b"])
def test_a_python_version_that_is_not_a_version_is_refused(value: str) -> None:
    """A receiver that cannot say what Python it runs is not one to install on."""
    with pytest.raises(InstallerError) as raised:
        _python_version(value)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


@pytest.mark.parametrize("value", ["maybe", 2, None, [], "TRUE"])
def test_an_openwebif_boolean_is_read_strictly(value: object) -> None:
    """`bool("maybe")` is True, and that is how a recording gets interrupted."""
    with pytest.raises(InstallerError) as raised:
        _strict_bool(value)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


@pytest.mark.parametrize(
    "value", [True, None, {}, "soon", float("inf"), float("nan"), -1]
)
def test_a_timer_timestamp_that_is_not_a_finite_time_is_refused(value: object) -> None:
    """A timer window that cannot be read is a guard that cannot be measured."""
    with pytest.raises(InstallerError) as raised:
        _finite_timestamp(value)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


@pytest.mark.parametrize(
    ("status", "timers"),
    [
        ("not json", '{"timers": []}'),
        ('{"isRecording": false}', "not json"),
        ("[]", '{"timers": []}'),
        ('{"isRecording": false}', "[]"),
        ("{}", '{"timers": []}'),
        ('{"isRecording": false}', '{"timers": "none"}'),
        ('{"isRecording": false}', '{"timers": ["not an object"]}'),
        ('{"isRecording": false}', '{"timers": [{"disabled": false}]}'),
        (
            '{"isRecording": false}',
            '{"timers": [{"disabled": false, "state": 0, "begin": 9, "end": 1}]}',
        ),
    ],
)
def test_an_unreadable_recording_guard_fails_closed(status: str, timers: str) -> None:
    """Not knowing whether the box is recording is not the same as it not recording."""
    with pytest.raises(InstallerError) as raised:
        _timer_guard(status, timers)

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


def test_a_running_timer_counts_as_recording_whatever_the_status_said() -> None:
    """OpenWebif reports the same fact twice, and the two can disagree."""
    recording, due = _timer_guard(
        '{"isRecording": false}',
        json.dumps(
            {"timers": [{"disabled": False, "state": 2, "begin": 1, "end": 4_000_000_000}]}
        ),
    )

    assert recording is True
    assert due == 1


def test_a_disabled_timer_is_not_a_guard() -> None:
    """Somebody switched it off; honouring it would block installs indefinitely."""
    recording, due = _timer_guard(
        '{"isRecording": false}',
        json.dumps(
            {"timers": [{"disabled": True, "state": 0, "begin": 1, "end": 4_000_000_000}]}
        ),
    )

    assert recording is False
    assert due == 0


@pytest.mark.parametrize(
    ("node_id", "base_topic"),
    [("", "enigma2"), ("has space", "enigma2"), ("ok", "enigma2/+"), ("ok", "enigma2/#")],
)
def test_an_unusable_mqtt_identity_is_refused_before_any_connection(
    credentials: SshCredentials, node_id: str, base_topic: str
) -> None:
    """A wildcard in a topic would make this receiver answer for every receiver."""
    request = InstallRequest(
        credentials, provisioning=None, node_id=node_id, base_topic=base_topic
    )

    with pytest.raises(InstallerError) as raised:
        request.validate()

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


def test_an_update_with_no_node_id_has_no_target(credentials: SshCredentials) -> None:
    """An update proves itself by a fresh announcement, so it must know where to look."""
    with pytest.raises(InstallerError) as raised:
        InstallRequest(credentials, provisioning=None).target()

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


async def test_a_transport_failure_becomes_the_step_s_own_error_code() -> None:
    """The caller is told which step failed, not which socket did."""

    class Broken:
        async def run(self, command: str, **kwargs: object) -> CommandResult:
            raise OSError("connection reset")

        async def close(self) -> None:
            return None

    with pytest.raises(InstallerError) as raised:
        await _run_checked(Broken(), "true", InstallerErrorCode.UPLOAD_FAILED)

    assert raised.value.code is InstallerErrorCode.UPLOAD_FAILED


@pytest.mark.parametrize(
    "result",
    [
        # The restart proof is arithmetic on these numbers; a word in there is not one.
        CommandResult(0, "not-a-pid\n"),
        # `pidof` says "no match" with 1 and nothing else with anything above it, so an
        # exit status of its own is a question that was not answered — not a receiver
        # with its interface down, which is what the empty set means.
        CommandResult(2, ""),
        CommandResult(127, "", "pidof: not found"),
    ],
)
async def test_an_answer_that_is_not_a_pid_list_is_refused(result: CommandResult) -> None:
    with pytest.raises(InstallerError) as raised:
        await _async_enigma_pids(_Fixed(result))

    assert raised.value.code is InstallerErrorCode.RESTART_FAILED


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        # What `pidof` does when nothing matches. It is an interface that is down — a
        # box still booting, or one stopped on purpose — not a receiver that failed to
        # answer, and it used to be refused as an ambiguous lifecycle.
        (CommandResult(1, ""), set()),
        (CommandResult(0, "100\n"), {100}),
        # A wrapper beside the interface it starts. Both are Enigma, for as long as the
        # box is up, and refusing this shape meant no install could finish on such an
        # image at all.
        (CommandResult(0, "100 101\n"), {100, 101}),
    ],
)
async def test_every_shape_pidof_reports_is_read_rather_than_refused(
    result: CommandResult, expected: set[int]
) -> None:
    assert await _async_enigma_pids(_Fixed(result)) == expected


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FileNotFoundError("gone"), InstallerErrorCode.BUNDLE_MISSING),
        (BundleError("bad"), InstallerErrorCode.BUNDLE_INVALID),
    ],
)
async def test_a_damaged_distribution_is_named_rather_than_crashed_on(
    hass: HomeAssistant, error: Exception, code: InstallerErrorCode
) -> None:
    """The bundle ships inside the integration, so this is a reinstall, not a box fault."""
    with (
        patch(
            "custom_components.enigma2_mqtt.installer.load_bundled_plugin", side_effect=error
        ),
        pytest.raises(InstallerError) as raised,
    ):
        await _async_load_bundle(hass)

    assert raised.value.code is code


@pytest.mark.parametrize(
    "identity",
    [
        "not json",
        '"a string"',
        '{"node_id": "x"}',
        '{"node_id": 1, "base_topic": "e", "enabled": true, "ha_mode": "integration"}',
    ],
)
async def test_an_unreadable_receiver_identity_is_refused(
    credentials: SshCredentials, identity: str
) -> None:
    """Overwriting a box whose configuration cannot be read is how one gets stolen."""
    request = InstallRequest(
        credentials, provisioning=None, node_id="vuuno4kse_005301", base_topic="enigma2"
    )

    with pytest.raises(InstallerError) as raised:
        await _async_validate_receiver_identity(
            _Fixed(CommandResult(0, identity)), "/tmp/helper.py", request
        )

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


@pytest.mark.parametrize(
    ("node_id", "base_topic", "enabled", "ha_mode"),
    [
        ("vuuno4kse_005301", "enigma2", False, "integration"),
        ("vuuno4kse_005301", "enigma2", True, "discovery"),
        ("vuuno4kse_005301", "other", True, "integration"),
    ],
)
async def test_an_update_refuses_a_receiver_that_is_not_the_one_configured(
    credentials: SshCredentials,
    node_id: str,
    base_topic: str,
    enabled: bool,
    ha_mode: str,
) -> None:
    """An update writes no settings, so the box has to already be the right box."""
    receiver = FakeReceiver(
        node_id=node_id, base_topic=base_topic, enabled=enabled, ha_mode=ha_mode
    )
    request = InstallRequest(
        credentials, provisioning=None, node_id="vuuno4kse_005301", base_topic="enigma2"
    )

    with pytest.raises(InstallerError) as raised:
        await _async_validate_receiver_identity(
            FakeSession(receiver), "/tmp/helper.py", request
        )

    assert raised.value.code is InstallerErrorCode.IDENTITY_MISMATCH


async def test_the_no_write_preflight_closes_its_session(
    credentials: SshCredentials,
) -> None:
    """It is offered from the options flow, where it must leave nothing behind."""
    receiver = FakeReceiver()

    measured = await async_preflight(None, credentials, _connector=receiver.connect)

    assert measured.python_version == "3.12.8"
    assert receiver.closed == 1


def test_an_ipk_member_header_that_does_not_end_where_it_must_is_refused() -> None:
    """An ar header is fixed-width and ends in a known two bytes; this one does not."""
    broken = b"!<arch>\n" + b"data.tar.gz".ljust(58) + b"XX"

    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(broken)

    assert raised.value.code is InstallerErrorCode.BUNDLE_INVALID


def test_an_ipk_member_with_an_unreadable_size_is_refused() -> None:
    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(_ipk(b"payload", size=b"not-a-size"))

    assert raised.value.code is InstallerErrorCode.BUNDLE_INVALID


def test_an_ipk_member_shorter_than_its_own_size_is_refused() -> None:
    """A truncated download is the failure a package store actually produces."""
    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(_ipk(b"short", size=b"9999"))

    assert raised.value.code is InstallerErrorCode.BUNDLE_INVALID


def test_an_ipk_with_no_payload_member_is_refused() -> None:
    """Control data alone describes a package; it does not install one."""
    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(_ipk(b"2.0\n", name=b"debian-binary"))

    assert raised.value.code is InstallerErrorCode.BUNDLE_INVALID


def test_an_ipk_whose_payload_is_not_a_tar_is_refused() -> None:
    """opkg would fail on it too, but only after the backup had been taken."""
    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(_ipk(b"this is not a tar archive"))

    assert raised.value.code is InstallerErrorCode.BUNDLE_INVALID


def test_an_ipk_whose_payload_escapes_the_root_is_refused() -> None:
    """`..` inside an archive is how a package writes outside its own files."""
    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(_ipk(_tar(("./../../etc/passwd", b"bad"))))

    assert raised.value.code is InstallerErrorCode.BUNDLE_INVALID


def test_an_ipk_carrying_no_regular_files_is_refused() -> None:
    """A manifest with nothing in it would verify anything at all."""
    with pytest.raises(InstallerError) as raised:
        _installed_hash_manifest(_ipk(_tar(directory="usr/lib/empty")))

    assert raised.value.code is InstallerErrorCode.BUNDLE_INVALID


def test_a_payload_file_is_listed_by_its_absolute_installed_path() -> None:
    """The manifest is checked on the receiver, where paths start at the root."""
    manifest = _installed_hash_manifest(_ipk(_tar(("./usr/lib/x.py", b"bytes")))).decode()

    digest, path = manifest.strip().split("  ", 1)
    assert path == "/usr/lib/x.py"
    assert len(digest) == 64

"""The SSH installer with a release of the signed index instead of the bundle.

Spec section ae.7, "SSH path": the installer takes an IPK source - the bundle, or verified bytes
held in memory - under the same lock, snapshot and rollback; checks every name in the release's
`depends` before anything changes; adds `info.build.commit` to the restart proof when the plugin
reports a build; and installs an older version only for the options flow's confirmed downgrade,
after which the older plugin is asked to retract what the newer one published (`cmd/reset`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.enigma2_mqtt.bundle import BundleError
from custom_components.enigma2_mqtt.const import PLUGIN_INDEX_ORIGIN, PLUGIN_RELEASE_API
from custom_components.enigma2_mqtt.installer import (
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    SshCredentials,
    _async_watch_restart,
    async_install,
)
from custom_components.enigma2_mqtt.release_package import (
    ORIGIN_DOWNLOAD,
    PackageSource,
    async_fetch_release,
)
from custom_components.enigma2_mqtt.release_store import async_release_index_cache

from .ipk import plugin_ipk
from .signed_index import release
from .test_installer import FakeReceiver, FakeSession

LOAD_BUNDLE = "custom_components.enigma2_mqtt.bundle.load_bundled_plugin"

NODE = "vuuno4kse_005301"
DATA = b"verified package bytes"
SHA = hashlib.sha256(DATA).hexdigest()
COMMIT = "d" * 40
PUBLISH = "custom_components.enigma2_mqtt.installer.mqtt.async_publish"


@pytest.fixture
def credentials() -> SshCredentials:
    return SshCredentials(
        host="192.0.2.12",
        username="root",
        password="not-a-real-password",
        host_key="ssh-ed25519 AAAATEST",
    )


def _listed(version: str, **changes: Any) -> dict[str, Any]:
    """A signed entry for `version` whose size and sha256 are the test package's."""
    return release(version, **{"size": len(DATA), "sha256": SHA, **changes})


async def _hold(
    hass: HomeAssistant, *releases: dict[str, Any], floor: str = "0.2.0"
) -> None:
    """Make the cache hold a verified index listing `releases` - the index a newer check
    accepted between the fetch and the install, when a test says so."""
    cache = async_release_index_cache(hass)
    await cache.async_load()
    cache.index = {"floor": floor, "releases": list(releases)}
    cache.integration_version = "0.4.0"


@pytest.fixture(autouse=True)
async def held_index(hass: HomeAssistant) -> None:
    """The index the package was fetched under: a package of the index exists only while an
    index that lists it is held."""
    await _hold(hass, _listed("0.3.0"), _listed("0.4.0"))


def _package(version: str = "0.4.0", **changes: Any) -> PackageSource:
    values: dict[str, Any] = {
        "version": version,
        "data": DATA,
        "sha256": SHA,
        "commit": COMMIT,
        "depends": ("python3-core",),
        "origin": ORIGIN_DOWNLOAD,
    }
    values.update(changes)
    return PackageSource(**values)


def _request(
    credentials: SshCredentials, package: PackageSource, *, downgrade: bool = False
) -> InstallRequest:
    return InstallRequest(
        credentials,
        provisioning=None,
        expect_running=True,
        node_id=NODE,
        base_topic="enigma2",
        package=package,
        downgrade=downgrade,
    )


class DependsReceiver(FakeReceiver):
    """A receiver that answers the dependency question from a list of what it has."""

    def __init__(self, *args: Any, have: tuple[str, ...] = ("python3-core",), **kwargs: Any):
        # The receiver this entry manages: an update checks that it is the same one.
        kwargs = {
            "node_id": NODE,
            "base_topic": "enigma2",
            "enabled": True,
            "ha_mode": "integration",
            **kwargs,
        }
        super().__init__(*args, **kwargs)
        self.have = have


async def _completed_watch(*args: Any, **kwargs: Any) -> Any:
    del args, kwargs
    loop = asyncio.get_running_loop()
    watch = type("Watch", (), {})()
    watch.established = loop.create_future()
    watch.completed = loop.create_future()
    watch.established.set_result(None)
    watch.completed.set_result(None)
    watch.cancel = lambda: None
    watch.arm = lambda: None
    return watch


def _run_in_a_shell(shell: tuple[str, ...], opkg_dir: Path, command: str) -> CommandResult:
    """Run one command line the way the receiver's shell would, with the fake opkg first on
    PATH, and report its end as asyncssh does: a shell killed by a signal is exit status -1."""
    done = subprocess.run(
        [*shell, "-c", command],
        env={**os.environ, "PATH": f"{opkg_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        # Its own process group, so a fake opkg that kills "its" group kills only the shell.
        start_new_session=True,
    )
    return CommandResult(-1 if done.returncode < 0 else done.returncode, done.stdout, done.stderr)


async def _install(
    hass: HomeAssistant,
    receiver: FakeReceiver,
    request: InstallRequest,
    *,
    shell: tuple[str, ...] | None = None,
    opkg_dir: Path | None = None,
) -> Any:
    """Install through the fake receiver. The dependency question is answered from the
    receiver's list of packages, or - given a shell and a directory holding a fake opkg - by
    running the installer's command line exactly as written."""
    original_run = FakeSession.run
    connects: list[SshCredentials] = []

    async def connect(credentials: SshCredentials) -> Any:
        connects.append(credentials)
        return await receiver.connect(credentials)

    async def run(self: FakeSession, command: str, **kwargs: Any) -> CommandResult:
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, SHA + "\n")
        if command.startswith("for p in ") and "opkg status" in command:
            self.receiver.commands.append(command)
            if shell is not None and opkg_dir is not None:
                return await hass.async_add_executor_job(
                    _run_in_a_shell, shell, opkg_dir, command
                )
            have = getattr(self.receiver, "have", ())
            asked = command.split(";", 1)[0].removeprefix("for p in ").split()
            missing = [name for name in asked if name not in have]
            return CommandResult(0, "".join(f"{name}\n" for name in missing))
        return await original_run(self, command, **kwargs)

    receiver.connects = connects
    with (
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch("custom_components.enigma2_mqtt.installer._async_watch_restart", _completed_watch),
        patch.object(FakeSession, "run", run),
    ):
        return await async_install(hass, request, _connector=connect)


async def test_the_verified_bytes_are_what_is_uploaded(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    receiver = DependsReceiver(installed_version="0.3.0")
    with patch(PUBLISH, new_callable=AsyncMock) as published:
        result = await _install(hass, receiver, _request(credentials, _package()))

    assert result.version == "0.4.0"
    assert DATA in receiver.inputs
    install = next(command for command in receiver.commands if command.startswith("opkg install"))
    assert "--force-downgrade" not in install
    # Not a downgrade: nothing to retract.
    published.assert_not_called()


async def test_bytes_that_changed_after_verification_are_refused_before_connecting(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    receiver = DependsReceiver()
    with pytest.raises(InstallerError) as raised:
        await _install(hass, receiver, _request(credentials, _package(data=b"other bytes")))

    assert raised.value.code is InstallerErrorCode.PACKAGE_INVALID
    assert receiver.commands == []


@pytest.mark.parametrize(
    ("releases", "code", "placeholders"),
    [
        (
            [_listed("0.4.0", withdrawn="it breaks EPG")],
            InstallerErrorCode.VERSION_WITHDRAWN,
            {"version": "0.4.0", "reason": "it breaks EPG"},
        ),
        (
            [_listed("0.4.0")],
            InstallerErrorCode.VERSION_BELOW_FLOOR,
            {"version": "0.4.0", "floor": "0.5.0"},
        ),
        (
            [_listed("0.4.0", contract=2)],
            InstallerErrorCode.VERSION_NOT_INSTALLABLE,
            {"version": "0.4.0"},
        ),
        (
            [_listed("0.4.0", min_integration="9.9.9")],
            InstallerErrorCode.VERSION_NOT_INSTALLABLE,
            {"version": "0.4.0"},
        ),
        (
            [_listed("0.3.0")],
            InstallerErrorCode.VERSION_NOT_INSTALLABLE,
            {"version": "0.4.0"},
        ),
        (
            [_listed("0.4.0", sha256="ab" * 32)],
            InstallerErrorCode.PACKAGE_INVALID,
            {"version": "0.4.0"},
        ),
        (
            [_listed("0.4.0", size=len(DATA) + 1)],
            InstallerErrorCode.PACKAGE_INVALID,
            {"version": "0.4.0"},
        ),
    ],
    ids=[
        "withdrawn since it was fetched",
        "below a floor raised since",
        "another contract since",
        "needs a newer integration since",
        "no longer listed",
        "another checksum since",
        "another size since",
    ],
)
async def test_the_rule_is_asked_again_at_install_time(
    hass: HomeAssistant,
    credentials: SshCredentials,
    releases: list[dict[str, Any]],
    code: InstallerErrorCode,
    placeholders: dict[str, str],
) -> None:
    """An index accepted between the fetch and the install may have withdrawn the version,
    raised the floor, moved it to another contract, asked for a newer integration, dropped it
    or signed other bytes under its number; the whole rule is asked again against the newest
    index held, and the install is refused before the receiver is connected to."""
    await _hold(hass, *releases, floor=placeholders.get("floor", "0.2.0"))
    receiver = DependsReceiver()

    with pytest.raises(InstallerError) as raised:
        await _install(hass, receiver, _request(credentials, _package()))

    assert raised.value.code is code
    assert raised.value.placeholders == placeholders
    assert receiver.connects == []
    assert receiver.commands == []


async def test_a_release_fetched_as_compatible_is_not_installed_under_a_newer_contract(
    hass: HomeAssistant, credentials: SshCredentials, aioclient_mock: AiohttpClientMocker
) -> None:
    """The review's case, end to end: the package is fetched and verified while the index
    calls it compatible, a newer index moves that release to another contract before the
    install, and the install stops before any SSH connection is opened."""
    package_bytes = plugin_ipk("0.4.0")
    sha = hashlib.sha256(package_bytes).hexdigest()
    entry = release("0.4.0", size=len(package_bytes), sha256=sha)
    await _hold(hass, entry)
    aioclient_mock.get(PLUGIN_RELEASE_API + "0.4.0", status=404)
    aioclient_mock.get(PLUGIN_INDEX_ORIGIN + entry["filename"], content=package_bytes)
    with patch(LOAD_BUNDLE, side_effect=BundleError("none in this test")):
        fetched = await async_fetch_release(hass, "0.4.0")

    await _hold(hass, {**entry, "contract": 2})
    receiver = DependsReceiver(installed_version="0.3.0")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, receiver, _request(credentials, fetched))

    assert raised.value.code is InstallerErrorCode.VERSION_NOT_INSTALLABLE
    assert receiver.connects == []
    assert receiver.commands == []


async def test_a_missing_dependency_refuses_before_anything_changes(
    hass: HomeAssistant, credentials: SshCredentials, caplog: pytest.LogCaptureFixture
) -> None:
    receiver = DependsReceiver(installed_version="0.3.0", have=("python3-core",))
    package = _package(depends=("python3-json", "python3-core", "python3-netclient"))

    with pytest.raises(InstallerError) as raised:
        await _install(hass, receiver, _request(credentials, package))

    assert raised.value.code is InstallerErrorCode.DEPENDS_MISSING
    # Every missing package, in the entry's order: one round trip to install them all, not one
    # refusal per package.
    assert raised.value.placeholders == {
        "version": "0.4.0",
        "package": "python3-json, python3-netclient",
    }
    joined = "\n".join(receiver.commands)
    assert " claim " not in joined
    assert " snapshot " not in joined
    assert "opkg install" not in joined
    assert receiver.files["plugin"] == "old"
    assert "needs python3-json, python3-netclient" in caplog.text


# The receiver's shells: OpenViX and OpenPLi run busybox sh; a CI runner has dash and bash.
SHELLS = [
    shell
    for shell in (("sh",), ("dash",), ("bash",), ("busybox", "sh"))
    if shutil.which(shell[0]) is not None
]

# An opkg that answers `opkg status <name>` the way opkg 0.6 does: a paragraph per package, a
# `Status:` line whose last word is the state, nothing at all for a package it has never heard
# of - and, for one name, a shell that dies under the question, as a dropped session does.
FAKE_OPKG = r"""#!/bin/sh
[ "$1" = status ] || exit 1
case "$2" in
  python3-core)
    printf 'Package: python3-core\nVersion: 3.12.3-r0\nDepends: libc6\n'
    printf 'Status: install ok installed\nArchitecture: cortexa15hf-neon-vfpv4\n\n' ;;
  python3-json) printf 'Package: python3-json\nStatus: install user installed\n\n' ;;
  python3-held) printf 'Package: python3-held\nStatus: install hold installed\n\n' ;;
  python3-threading) printf 'Package: python3-threading\nStatus: install ok not-installed\n\n' ;;
  python3-netclient) printf 'Package: python3-netclient\nStatus: install ok half-installed\n\n' ;;
  python3-lost) kill -9 0 ;;
esac
exit 0
"""


@pytest.fixture
def opkg_dir(tmp_path: Path) -> Path:
    opkg = tmp_path / "opkg"
    opkg.write_text(FAKE_OPKG, encoding="utf-8")
    opkg.chmod(0o755)
    return tmp_path


@pytest.mark.skipif(not SHELLS, reason="no POSIX shell to run the command in")
@pytest.mark.parametrize("shell", SHELLS, ids=[" ".join(shell) for shell in SHELLS])
async def test_the_dependency_question_is_judged_by_opkg_s_status_line(
    hass: HomeAssistant, credentials: SshCredentials, opkg_dir: Path, shell: tuple[str, ...]
) -> None:
    """The command line itself, run by a real shell against an opkg that answers as opkg does:
    only `installed` is installed - `not-installed` and `half-installed` end in the same word,
    and a package opkg has never heard of prints nothing."""
    receiver = DependsReceiver(installed_version="0.3.0")
    package = _package(
        depends=(
            "python3-core",
            "python3-json",
            "python3-held",
            "python3-threading",
            "python3-netclient",
            "python3-missing",
        )
    )

    with pytest.raises(InstallerError) as raised:
        await _install(
            hass, receiver, _request(credentials, package), shell=shell, opkg_dir=opkg_dir
        )

    assert raised.value.code is InstallerErrorCode.DEPENDS_MISSING
    assert raised.value.placeholders["package"] == (
        "python3-threading, python3-netclient, python3-missing"
    )
    assert not any(command.startswith("opkg install") for command in receiver.commands)


@pytest.mark.skipif(not SHELLS, reason="no POSIX shell to run the command in")
@pytest.mark.parametrize("shell", SHELLS, ids=[" ".join(shell) for shell in SHELLS])
async def test_a_receiver_that_has_every_dependency_is_installed(
    hass: HomeAssistant, credentials: SshCredentials, opkg_dir: Path, shell: tuple[str, ...]
) -> None:
    receiver = DependsReceiver(installed_version="0.3.0")
    package = _package(depends=("python3-core", "python3-json", "python3-held"))

    with patch(PUBLISH, new_callable=AsyncMock):
        result = await _install(
            hass, receiver, _request(credentials, package), shell=shell, opkg_dir=opkg_dir
        )

    assert result.version == "0.4.0"
    assert any(command.startswith("opkg install") for command in receiver.commands)


@pytest.mark.skipif(not SHELLS, reason="no POSIX shell to run the command in")
@pytest.mark.parametrize("shell", SHELLS, ids=[" ".join(shell) for shell in SHELLS])
async def test_a_dependency_question_that_dies_changes_nothing(
    hass: HomeAssistant, credentials: SshCredentials, opkg_dir: Path, shell: tuple[str, ...]
) -> None:
    """A shell that dies under the question has printed no missing package - which is not
    the same as having none missing."""
    receiver = DependsReceiver(installed_version="0.3.0")
    package = _package(depends=("python3-core", "python3-lost"))

    with pytest.raises(InstallerError) as raised:
        await _install(
            hass, receiver, _request(credentials, package), shell=shell, opkg_dir=opkg_dir
        )

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED
    joined = "\n".join(receiver.commands)
    assert " claim " not in joined
    assert "opkg install" not in joined
    assert receiver.files["plugin"] == "old"


async def test_every_dependency_is_asked_about_and_quoted(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    receiver = DependsReceiver(installed_version="0.3.0", have=("python3-core", "python3-json"))
    package = _package(depends=("python3-core", "python3-json"))
    with patch(PUBLISH, new_callable=AsyncMock):
        await _install(hass, receiver, _request(credentials, package))

    asked = next(command for command in receiver.commands if 'opkg status "$p"' in command)
    assert asked.startswith("for p in python3-core python3-json;")
    # A reader: `opkg status` never takes opkg's lock.
    assert "opkg install" not in asked


async def test_a_release_with_no_dependencies_asks_nothing(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    receiver = DependsReceiver(installed_version="0.3.0")
    with patch(PUBLISH, new_callable=AsyncMock):
        await _install(hass, receiver, _request(credentials, _package(depends=())))

    assert not any("opkg status \"" in command for command in receiver.commands)


async def test_an_older_package_over_a_newer_plugin_is_refused_without_a_downgrade(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    receiver = DependsReceiver(installed_version="0.4.0")

    with pytest.raises(InstallerError) as raised:
        await _install(hass, receiver, _request(credentials, _package("0.3.0")))

    assert raised.value.code is InstallerErrorCode.NEWER_INSTALLED
    assert not any(command.startswith("opkg install") for command in receiver.commands)


async def test_a_confirmed_downgrade_forces_opkg_and_resets_the_older_plugin(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    receiver = DependsReceiver(installed_version="0.4.0")
    order: list[str] = []
    original_release = FakeSession.run

    with patch(PUBLISH, new_callable=AsyncMock) as published:
        published.side_effect = lambda *args, **kwargs: order.append("reset")

        async def run(self: FakeSession, command: str, **kwargs: Any) -> CommandResult:
            if " release " in command:
                order.append("released")
            return await original_release(self, command, **kwargs)

        with patch.object(FakeSession, "run", run):
            result = await _install(
                hass, receiver, _request(credentials, _package("0.3.0"), downgrade=True)
            )

    assert result.version == "0.3.0"
    install = next(command for command in receiver.commands if command.startswith("opkg install"))
    assert install.startswith("opkg install --force-reinstall --force-downgrade ")
    published.assert_awaited_once_with(
        hass, f"enigma2/{NODE}/cmd/reset", "PRESS", qos=1, retain=False
    )
    # After the proof and the commit, never before: a reset of a plugin that is then rolled
    # back would retract the topics of the one that stays.
    assert order == ["released", "reset"]


async def test_a_downgrade_request_for_a_newer_package_is_an_ordinary_install(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    receiver = DependsReceiver(installed_version="0.3.0")
    with patch(PUBLISH, new_callable=AsyncMock) as published:
        await _install(hass, receiver, _request(credentials, _package("0.4.0"), downgrade=True))

    install = next(command for command in receiver.commands if command.startswith("opkg install"))
    assert "--force-downgrade" not in install
    published.assert_not_called()


async def test_a_failed_reset_does_not_fail_a_committed_downgrade(
    hass: HomeAssistant, credentials: SshCredentials, caplog: pytest.LogCaptureFixture
) -> None:
    receiver = DependsReceiver(installed_version="0.4.0")
    with patch(PUBLISH, new_callable=AsyncMock, side_effect=OSError("broker gone")):
        result = await _install(
            hass, receiver, _request(credentials, _package("0.3.0"), downgrade=True)
        )

    assert result.version == "0.3.0"
    assert "retract the newer plugin's topics failed" in caplog.text


async def test_a_downgrade_that_fails_rolls_back_and_resets_nothing(
    hass: HomeAssistant, credentials: SshCredentials
) -> None:
    receiver = DependsReceiver(installed_version="0.4.0", fail_on="opkg install")
    with (
        patch(PUBLISH, new_callable=AsyncMock) as published,
        pytest.raises(InstallerError),
    ):
        await _install(hass, receiver, _request(credentials, _package("0.3.0"), downgrade=True))

    assert receiver.rolled_back is True
    published.assert_not_called()


# ------------------------------------------------------------------ the restart proof --


async def _watch_outcome(hass: HomeAssistant, commit: str | None, info: dict[str, Any]) -> bool:
    watch = await _async_watch_restart(
        hass, "enigma2", NODE, "0.4.0", require_offline=False, commit=commit
    )
    try:
        watch.arm()
        async_fire_mqtt_message(hass, f"enigma2/{NODE}/availability", "online")
        async_fire_mqtt_message(hass, f"enigma2/{NODE}/info", json.dumps(info))
        await hass.async_block_till_done()
        return watch.completed.done()
    finally:
        watch.cancel()


@pytest.mark.parametrize(
    ("commit", "build", "proved"),
    [
        (COMMIT, {"commit": COMMIT, "flavour": "release"}, True),
        (COMMIT, {"commit": "e" * 40, "flavour": "release"}, False),
        (COMMIT, None, True),
        (COMMIT, {"commit": "", "flavour": None}, True),
        (None, {"commit": "e" * 40, "flavour": "development"}, True),
    ],
    ids=[
        "the installed commit",
        "another build of the same number",
        "a plugin that predates build ids",
        "a build that names no commit",
        "no commit to hold it to",
    ],
)
async def test_the_proof_holds_a_reported_build_to_the_installed_commit(
    hass: HomeAssistant,
    mqtt_mock: Any,
    commit: str | None,
    build: dict[str, Any] | None,
    proved: bool,
) -> None:
    info: dict[str, Any] = {"plugin": "0.4.0", "ha_mode": "integration"}
    if build is not None:
        info["build"] = build

    assert await _watch_outcome(hass, commit, info) is proved

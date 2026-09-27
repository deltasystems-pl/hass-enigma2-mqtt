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
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.enigma2_mqtt.installer import (
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    SshCredentials,
    _async_watch_restart,
    async_install,
)
from custom_components.enigma2_mqtt.release_package import ORIGIN_DOWNLOAD, PackageSource
from custom_components.enigma2_mqtt.release_store import async_release_index_cache

from .signed_index import release
from .test_installer import FakeReceiver, FakeSession

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


async def _install(
    hass: HomeAssistant, receiver: FakeReceiver, request: InstallRequest
) -> Any:
    original_run = FakeSession.run

    async def run(self: FakeSession, command: str, **kwargs: Any) -> CommandResult:
        if "sha256sum" in command:
            self.receiver.commands.append(command)
            return CommandResult(0, SHA + "\n")
        if command.startswith("for p in ") and "opkg status" in command:
            self.receiver.commands.append(command)
            have = getattr(self.receiver, "have", ())
            asked = command.split(";", 1)[0].removeprefix("for p in ").split()
            missing = [name for name in asked if name not in have]
            return CommandResult(0, "".join(f"{name}\n" for name in missing))
        return await original_run(self, command, **kwargs)

    with (
        patch(
            "custom_components.enigma2_mqtt.installer._installed_hash_manifest",
            return_value=b"hash  /file\n",
        ),
        patch("custom_components.enigma2_mqtt.installer._async_watch_restart", _completed_watch),
        patch.object(FakeSession, "run", run),
    ):
        return await async_install(hass, request, _connector=receiver.connect)


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
    ("entry", "code", "placeholders"),
    [
        (
            release("0.4.0", withdrawn="it breaks EPG"),
            InstallerErrorCode.VERSION_WITHDRAWN,
            {"version": "0.4.0", "reason": "it breaks EPG"},
        ),
        (
            release("0.4.0"),
            InstallerErrorCode.VERSION_BELOW_FLOOR,
            {"version": "0.4.0", "floor": "0.5.0"},
        ),
    ],
    ids=["withdrawn since it was fetched", "below a floor raised since"],
)
async def test_the_rule_is_asked_again_at_install_time(
    hass: HomeAssistant,
    credentials: SshCredentials,
    entry: dict[str, Any],
    code: InstallerErrorCode,
    placeholders: dict[str, str],
) -> None:
    """An index accepted between the fetch and the install may have withdrawn the version or
    raised the floor; the install is refused before the receiver is connected to."""
    cache = async_release_index_cache(hass)
    await cache.async_load()
    cache.index = {"floor": placeholders.get("floor", "0.2.0"), "releases": [entry]}
    receiver = DependsReceiver()

    with pytest.raises(InstallerError) as raised:
        await _install(hass, receiver, _request(credentials, _package()))

    assert raised.value.code is code
    assert raised.value.placeholders == placeholders
    assert receiver.commands == []


async def test_a_missing_dependency_refuses_before_anything_changes(
    hass: HomeAssistant, credentials: SshCredentials, caplog: pytest.LogCaptureFixture
) -> None:
    receiver = DependsReceiver(installed_version="0.3.0", have=("python3-core",))
    package = _package(depends=("python3-core", "python3-json"))

    with pytest.raises(InstallerError) as raised:
        await _install(hass, receiver, _request(credentials, package))

    assert raised.value.code is InstallerErrorCode.DEPENDS_MISSING
    assert raised.value.placeholders == {"version": "0.4.0", "package": "python3-json"}
    joined = "\n".join(receiver.commands)
    assert " claim " not in joined
    assert " snapshot " not in joined
    assert "opkg install" not in joined
    assert receiver.files["plugin"] == "old"
    assert "needs python3-json" in caplog.text


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

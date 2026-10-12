"""Which proof a successful install passed, and where that is kept.

The installer accepts more than one proof that the new plugin runs: the plugin's fresh
announcement, with or without the package's build commit on it, and - for the forced
reinstall, which may meet a plugin that is switched off - a new interface process
holding the plugin's log open. An update over MQTT is proven by `info`, with the signed
build commit when the entry names one. Once an install is over the outcome alone does not
say which of them it was, so the result names it, the log says it at INFO, and the
diagnostics download carries the last one of this run.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.update import ATTR_VERSION
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt import installer
from custom_components.enigma2_mqtt.diagnostics import async_get_config_entry_diagnostics
from custom_components.enigma2_mqtt.installer import (
    InstallerError,
    InstallerErrorCode,
    InstallResult,
    _async_watch_restart,
    _proof,
)

from .conftest import PLUGIN_VERSION
from .test_update_entity import PLUGIN, _report, _setup

COMMIT = "1" * 40
SSH_INSTALL = "custom_components.enigma2_mqtt.update.async_install"


async def _announce(
    hass: HomeAssistant, *, commit: str | None, build: Any
) -> installer._RestartWatch:
    """Play a restart to the real watch: `offline`, `online`, then `info` with `build`."""
    callbacks: dict[str, Any] = {}

    async def subscribe(_hass: HomeAssistant, topic: str, handler: Any):
        callbacks[topic] = handler
        return lambda: None

    def subscribed(_hass: HomeAssistant, _topic: str, _qos: int, handler: Any) -> Any:
        handler()
        return lambda: None

    with (
        patch("custom_components.enigma2_mqtt.installer.mqtt.async_subscribe", subscribe),
        patch(
            "custom_components.enigma2_mqtt.installer.mqtt.async_on_subscribe_done", subscribed
        ),
    ):
        watch = await _async_watch_restart(
            hass, "enigma2", "vuuno4kse_005301", "0.5.0", require_offline=True, commit=commit
        )
    watch.arm()
    availability = callbacks["enigma2/vuuno4kse_005301/availability"]
    availability(SimpleNamespace(payload="offline", retain=False))
    availability(SimpleNamespace(payload="online", retain=False))
    info = {"plugin": "0.5.0", "ha_mode": "integration"}
    if build is not ...:
        info["build"] = build
    callbacks["enigma2/vuuno4kse_005301/info"](
        SimpleNamespace(payload=json.dumps(info), retain=False)
    )
    return watch


@pytest.mark.parametrize(
    ("commit", "build", "named", "proof"),
    [
        (COMMIT, {"commit": COMMIT}, True, "announcement_build"),
        # No build on `info` - a plugin from before build ids - proves by its version alone.
        (COMMIT, ..., False, "announcement"),
        (COMMIT, {"commit": ""}, False, "announcement"),
        (COMMIT, "not an object", False, "announcement"),
        # A package whose commit nobody knows cannot be matched, whatever `info` names.
        (None, {"commit": COMMIT}, False, "announcement"),
    ],
    ids=["the commit", "no build", "an empty commit", "not a build", "an unknown package"],
)
async def test_the_announcement_says_whether_it_named_the_package_s_build(
    hass: HomeAssistant, commit: str | None, build: Any, named: bool, proof: str
) -> None:
    watch = await _announce(hass, commit=commit, build=build)

    assert watch.completed.done()
    assert watch.completed.result() is named
    assert _proof(False, watch) == proof
    assert _proof(True, watch) == f"log_open+{proof}"
    watch.cancel()


async def test_an_announcement_of_another_build_is_no_proof_at_all(hass: HomeAssistant) -> None:
    """Unchanged by the naming: a same-number build that is not the one installed."""
    watch = await _announce(hass, commit=COMMIT, build={"commit": "2" * 40})

    assert not watch.completed.done()
    watch.cancel()


def test_a_switched_off_plugin_is_proven_by_its_open_log_alone() -> None:
    assert _proof(True, None) == "log_open"


async def test_a_stand_in_watch_without_a_word_on_the_build_is_the_plain_announcement() -> None:
    """A future resolved with nothing - as every stand-in in these tests is - names no build."""
    completed = asyncio.get_running_loop().create_future()
    completed.set_result(None)

    assert _proof(False, SimpleNamespace(completed=completed)) == "announcement"


# ------------------------------------------------------------- where it is kept --


async def _last_install(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return (await async_get_config_entry_diagnostics(hass, entry))["plugin"]["last_install"]


async def test_diagnostics_say_nothing_before_an_install(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)

    assert await _last_install(hass, config_entry) is None


async def test_an_install_over_ssh_records_the_proof_the_installer_named(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "0.2.0")
    before = dt_util.utcnow()
    done = InstallResult(PLUGIN_VERSION, True, True, proof="announcement_build")

    with patch(SSH_INSTALL, AsyncMock(return_value=done)):
        await hass.services.async_call(
            "update", "install", {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
            blocking=True,
        )

    record = await _last_install(hass, config_entry)
    assert {key: record[key] for key in ("path", "version", "proof")} == {
        "path": "ssh",
        "version": PLUGIN_VERSION,
        "proof": "announcement_build",
    }
    assert dt_util.parse_datetime(record["time"]) >= before


async def test_an_install_that_failed_records_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "0.2.0")
    failed = InstallerError(InstallerErrorCode.ANNOUNCEMENT_TIMEOUT)

    with patch(SSH_INSTALL, AsyncMock(side_effect=failed)), pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "update", "install", {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
            blocking=True,
        )

    assert await _last_install(hass, config_entry) is None

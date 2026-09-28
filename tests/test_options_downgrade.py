"""„Zainstaluj starszą wersję wtyczki": the options flow's confirmed downgrade over SSH.

ADR-0008, section 6: SSH only; versions from the floor up to below the installed one, from
the verified index and under the one rule; the form names what disappears - the entities whose
capability the target predates, and updates over MQTT when the target cannot update itself; a
tick box is required; the install passes `downgrade` so the installer forces opkg and resets the
older plugin after its proof; the flow ends as an abort, because it changes the receiver and never
the entry.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt import release_package
from custom_components.enigma2_mqtt.const import (
    CONF_CONFIRM_DOWNGRADE,
    CONF_DOWNGRADE_VERSION,
)
from custom_components.enigma2_mqtt.downgrade import CAPABILITY_SINCE, downgrade_candidates
from custom_components.enigma2_mqtt.installer import InstallerError, InstallerErrorCode
from custom_components.enigma2_mqtt.release_package import (
    ORIGIN_DOWNLOAD,
    PackageError,
    PackageSource,
)
from custom_components.enigma2_mqtt.release_store import async_release_index_cache

from .conftest import CAPABILITIES, INFO, INFO_TOPIC
from .signed_index import release, sign
from .test_update_entity import CREDENTIALS

FETCH = "custom_components.enigma2_mqtt.config_flow.async_fetch_release"
INSTALL = "custom_components.enigma2_mqtt.config_flow.async_install"
INDEX = [
    release("0.5.0"),
    release("0.4.0", self_update=True),
    release("0.3.1", withdrawn="it breaks EPG"),
    release("0.3.0"),
    release("0.2.5", contract=2),
    release("0.2.0"),
    release("0.1.0", contract=0),
]


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    credentials: bool = True,
    installed: str = "0.4.0",
    capabilities: list[str] | None = None,
    index: list[dict[str, Any]] | None = INDEX,
    floor: str = "0.2.0",
) -> None:
    entry.add_to_hass(hass)
    if credentials:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **CREDENTIALS})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    info = {**INFO, "plugin": installed, "capabilities": capabilities or CAPABILITIES}
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(info))
    await hass.async_block_till_done()
    cache = async_release_index_cache(hass)
    await cache.async_load()
    cache.index = json.loads(sign(1, index, floor=floor)[0]) if index is not None else None
    cache.integration_version = "0.4.0"


def _source(version: str) -> PackageSource:
    return PackageSource(
        version=version,
        data=b"verified",
        sha256="ab" * 32,
        commit=release(version)["commit"],
        depends=("python3-core",),
        origin=ORIGIN_DOWNLOAD,
    )


async def _open(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "downgrade" in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "downgrade"}
    )


async def _choose(hass: HomeAssistant, result: dict[str, Any], version: str) -> dict[str, Any]:
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DOWNGRADE_VERSION: version}
    )


async def _confirm(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM_DOWNGRADE: True}
    )
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    return result


# ---------------------------------------------------------------- which versions --


def test_the_candidates_are_the_rule_s_versions_below_the_installed_one() -> None:
    index = json.loads(sign(1, INDEX)[0])
    assert downgrade_candidates(index, "0.4.0", "0.4.0") == ["0.3.0", "0.2.0"]
    # A development build of 0.4.0 counts as 0.4.0.
    assert downgrade_candidates(index, "0.4.0+gabc1234", "0.4.0") == ["0.3.0", "0.2.0"]
    # The index's own floor is the floor too.
    raised = json.loads(sign(1, INDEX, floor="0.3.0")[0])
    assert downgrade_candidates(raised, "0.4.0", "0.4.0") == ["0.3.0"]
    assert downgrade_candidates(index, "0.2.0", "0.4.0") == []
    assert downgrade_candidates(None, "0.4.0", "0.4.0") == []
    assert downgrade_candidates(index, None, "0.4.0") == []


async def test_the_step_offers_only_those_versions(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    result = await _open(hass, config_entry)

    assert result["step_id"] == "downgrade"
    selector = result["data_schema"].schema[CONF_DOWNGRADE_VERSION]
    assert selector.config["options"] == ["0.3.0", "0.2.0"]
    assert result["description_placeholders"] == {"installed": "0.4.0"}


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ({"credentials": False}, "no SSH credentials"),
        ({"index": None}, "no verified index"),
        ({"installed": "0.2.0"}, "nothing older the rule allows"),
    ],
    ids=["no credentials", "no index", "nothing older"],
)
async def test_the_menu_entry_exists_only_with_a_version_to_offer(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    setup: dict[str, Any],
    why: str,
) -> None:
    await _setup(hass, config_entry, **setup)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    # Without anything else to offer, Configure opens on the options as it always has.
    assert result["type"] is FlowResultType.FORM, why
    assert result["step_id"] == "settings"


async def test_the_menu_offers_it_beside_the_options(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["menu_options"] == ["settings", "downgrade"]


# -------------------------------------------------------------- what disappears --


async def test_the_confirmation_names_what_the_older_version_takes_away(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    hass.config.language = "pl"
    await _setup(
        hass,
        config_entry,
        capabilities=[*CAPABILITIES, "zap_history", "softcam", "epg_import", "self_update"],
    )

    result = await _choose(hass, await _open(hass, config_entry), "0.2.0")

    assert result["step_id"] == "downgrade_confirm"
    placeholders = result["description_placeholders"]
    assert placeholders["version"] == "0.2.0"
    assert placeholders["installed"] == "0.4.0"
    lost = placeholders["lost"].splitlines()
    for name in ("Ostatnio oglądane", "Softcam", "Import EPG", "Pobierz EPG"):
        assert f"- {name}" in lost
    assert any("MQTT" in line for line in lost)
    # Only what the receiver has: it claims no `process` capability, so nothing of it is lost.
    assert not any("Enigma2" in line for line in lost)


async def test_a_target_that_has_a_capability_loses_nothing_of_it(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, capabilities=[*CAPABILITIES, "zap_history"])

    result = await _choose(hass, await _open(hass, config_entry), "0.3.0")

    assert result["description_placeholders"]["lost"] == "-"


async def test_a_target_that_updates_itself_keeps_the_updates_over_mqtt(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    """Going down to a release that can itself update over MQTT loses no MQTT updates: the
    line is the target's to earn, not the running plugin's."""
    await _setup(
        hass,
        config_entry,
        installed="0.5.0",
        capabilities=[*CAPABILITIES, "zap_history", "self_update"],
    )

    result = await _choose(hass, await _open(hass, config_entry), "0.4.0")

    assert result["description_placeholders"]["lost"] == "-"


def test_every_capability_newer_than_the_floor_is_in_the_table() -> None:
    """The 0.2.0 -> 0.3.0 row of the plugin's TOPICS.md: capabilities with entities."""
    assert set(CAPABILITY_SINCE) == {
        "zap_history",
        "history_clear",
        "softcam",
        "epg_import",
        "process",
        "toast",
    }


# ---------------------------------------------------------------- the install --


async def test_an_unticked_form_fetches_and_changes_nothing(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    result = await _choose(hass, await _open(hass, config_entry), "0.3.0")

    with patch(FETCH, AsyncMock()) as fetch, patch(INSTALL, AsyncMock()) as install:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_CONFIRM_DOWNGRADE: False}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "downgrade_not_confirmed"}
    fetch.assert_not_called()
    install.assert_not_called()


async def test_a_confirmed_downgrade_fetches_then_installs_it_as_a_downgrade(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    result = await _choose(hass, await _open(hass, config_entry), "0.3.0")
    source = _source("0.3.0")
    options_before = dict(config_entry.options)

    with (
        patch(FETCH, AsyncMock(return_value=source)) as fetch,
        patch(INSTALL, AsyncMock()) as install,
    ):
        result = await _confirm(hass, result)

    fetch.assert_awaited_once_with(hass, "0.3.0")
    request = install.await_args.args[1]
    assert request.package is source
    assert request.downgrade is True
    assert request.provisioning is None
    assert request.expect_running is True
    assert request.credentials.host == CREDENTIALS["ssh_host"]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "downgrade_done"
    assert result["description_placeholders"] == {"version": "0.3.0"}
    assert dict(config_entry.options) == options_before


@pytest.mark.parametrize(
    ("error", "reason", "placeholders"),
    [
        (PackageError(release_package.DOWNLOAD, "x", {"version": "0.3.0"}), "package_invalid",
         {"version": "0.3.0"}),
        (PackageError(release_package.DIGEST, "x", {"version": "0.3.0"}), "package_invalid",
         {"version": "0.3.0"}),
        (
            PackageError(release_package.WITHDRAWN, "x", {"version": "0.3.0", "reason": "r"}),
            "version_withdrawn",
            {"version": "0.3.0", "reason": "r"},
        ),
    ],
    ids=["download", "digest", "withdrawn meanwhile"],
)
async def test_a_version_that_cannot_be_had_ends_the_flow_before_any_ssh(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    error: PackageError,
    reason: str,
    placeholders: dict[str, str],
) -> None:
    await _setup(hass, config_entry)
    result = await _choose(hass, await _open(hass, config_entry), "0.3.0")

    with patch(FETCH, AsyncMock(side_effect=error)), patch(INSTALL, AsyncMock()) as install:
        result = await _confirm(hass, result)

    assert result["reason"] == reason
    assert result["description_placeholders"] == placeholders
    install.assert_not_called()


async def test_the_installer_s_refusal_is_the_flow_s_end(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    result = await _choose(hass, await _open(hass, config_entry), "0.3.0")

    with (
        patch(FETCH, AsyncMock(return_value=_source("0.3.0"))),
        patch(INSTALL, AsyncMock(side_effect=InstallerError(InstallerErrorCode.STANDBY))),
    ):
        result = await _confirm(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "standby"


async def test_a_version_withdrawn_while_the_form_was_open_is_not_installed(
    hass: HomeAssistant, mqtt_mock: Any, box_on_the_broker: Any, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    result = await _choose(hass, await _open(hass, config_entry), "0.3.0")
    withdrawn = [dict(item) for item in INDEX]
    for item in withdrawn:
        if item["version"] == "0.3.0":
            item["withdrawn"] = "found broken"
    async_release_index_cache(hass).index = json.loads(sign(2, withdrawn)[0])

    with patch(FETCH, AsyncMock()) as fetch, patch(INSTALL, AsyncMock()) as install:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_CONFIRM_DOWNGRADE: True}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "downgrade_not_offered"
    fetch.assert_not_called()
    install.assert_not_called()


def _texts() -> dict[str, dict[str, Any]]:
    """strings.json and every translation, by file name."""
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "enigma2_mqtt"
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (root / "strings.json", *sorted((root / "translations").glob("*.json")))
    }


# What each language calls the refusal in standby, and the update card offering the newer
# release again.
_REFUSED_IN_STANDBY = {"en": "refused", "pl": "odrzucona", "de": "abgelehnt"}
_OFFERED_AGAIN = {"en": "update card", "pl": "Karta aktualizacji", "de": "Update-Karte"}


def test_the_confirmation_promises_only_what_the_install_does() -> None:
    """The installer refuses a receiver in standby - the restart would wake it and may turn the
    television on - so the confirmation cannot promise that the receiver keeps its standby
    state; it says the receiver has to be on."""
    promises = ("standby state", "tryb czuwania", "Standby-Zustand")
    for name, texts in _texts().items():
        language = "en" if name == "strings.json" else name.removesuffix(".json")
        text = texts["options"]["step"]["downgrade_confirm"]["description"]
        assert not any(promise in text for promise in promises), name
        assert _REFUSED_IN_STANDBY[language] in text, name


def test_the_end_says_the_card_offers_the_newer_release_again() -> None:
    """Right after a downgrade the newer release is the newest compatible one again, so the
    update card badges it; the end of the flow says so, and how to stay."""
    for name, texts in _texts().items():
        language = "en" if name == "strings.json" else name.removesuffix(".json")
        text = texts["options"]["abort"]["downgrade_done"]
        assert _OFFERED_AGAIN[language] in text, name
        assert text.count("{version}") == 2, name


def test_every_installer_failure_has_an_options_abort_string() -> None:
    """The downgrade ends by aborting with the installer's code as the reason."""
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "enigma2_mqtt"
    for path in (root / "strings.json", *sorted((root / "translations").glob("*.json"))):
        aborts = json.loads(path.read_text(encoding="utf-8"))["options"]["abort"]
        for code in (*InstallerErrorCode, "downgrade_done", "downgrade_not_offered"):
            value = code.value if isinstance(code, InstallerErrorCode) else code
            assert aborts.get(value, "").strip(), f"{path.name} has no options abort {value}"

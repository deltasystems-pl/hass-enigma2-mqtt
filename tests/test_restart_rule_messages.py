"""What a household is told when a restart is refused or withdrawn.

A code on the update card (`restart_withdrawn`) would not say the one thing that matters
after a withdrawal: that a question may still be on the television and that either answer
is safe. So these outcomes get sentences of their own, on the card and on the guided
install's last screen, in every language.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.components.update import ATTR_VERSION
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt.const import (
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_USERNAME,
)
from custom_components.enigma2_mqtt.installer import InstallerError, InstallerErrorCode

from .conftest import PLUGIN_VERSION
from .test_installer_flow import _install_watching_the_frontend

PLUGIN = "update.dekoder_salon_plugin"
TRANSLATIONS = Path(__file__).parent.parent / "custom_components/enigma2_mqtt/translations"


@pytest.mark.parametrize(
    ("code", "key"),
    [
        (InstallerErrorCode.RESTART_WITHDRAWN, "update_withdrawn"),
        (InstallerErrorCode.STANDBY, "update_standby"),
        (InstallerErrorCode.STREAMING, "update_streaming"),
    ],
)
async def test_the_update_card_says_it_in_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    code: InstallerErrorCode,
    key: str,
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "192.0.2.12",
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "example-only",
            CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
        },
    )
    install = AsyncMock(side_effect=InstallerError(code))
    with patch("custom_components.enigma2_mqtt.update.async_install", install):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call(
                "update",
                "install",
                {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
                blocking=True,
            )

    assert raised.value.translation_key == key


async def _withdrawn_install(_hass, _request, progress_cb):
    progress_cb("restart")
    await asyncio.sleep(0)
    raise InstallerError(InstallerErrorCode.RESTART_WITHDRAWN)


async def test_the_guided_install_ends_on_the_withdrawal_sentence(
    hass: HomeAssistant, mqtt_mock
) -> None:
    answers, _ = await _install_watching_the_frontend(hass, _withdrawn_install)

    assert answers[0]["type"] is FlowResultType.ABORT
    assert answers[0]["reason"] == "restart_withdrawn"


def test_the_polish_sentence_is_the_one_the_operator_chose() -> None:
    """Q3-A word for word: withdrawn, the old plugin runs, the question may stay, both safe."""
    polish = json.loads((TRANSLATIONS / "pl.json").read_text(encoding="utf-8"))

    assert polish["exceptions"]["update_withdrawn"]["message"] == (
        "Dekoder zapytał na ekranie, czy uruchomić ponownie interfejs, więc aktualizację "
        "wycofano; działa poprzednia wersja wtyczki. Pytanie może nadal być widoczne na "
        "ekranie telewizora - odpowiedź „tak” albo „nie” jest bezpieczna."
    )
    assert polish["exceptions"]["update_standby"]["message"] == (
        "Dekoder jest w trybie czuwania. Aktualizacja uruchamia ponownie interfejs "
        "dekodera, co go wybudzi i może włączyć telewizor. Włącz dekoder i spróbuj ponownie."
    )
    for language in ("en", "de", "pl"):
        texts = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
        assert texts["config"]["abort"]["restart_withdrawn"]
        assert texts["exceptions"]["update_withdrawn"]["message"]

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
        (InstallerErrorCode.RESTART_UNCONFIRMED, "update_restart_unconfirmed"),
        (InstallerErrorCode.WITHDRAW_FAILED, "update_withdraw_failed"),
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
    # When the files could not be put back, the question may still be answered - and
    # "yes" then starts the new, unchecked version. The sentence has to say so.
    assert "Pytanie może nadal być widoczne" in polish["exceptions"]["update_withdraw_failed"][
        "message"
    ]
    # A restart OpenWebif never confirmed is not described as a question it asked.
    assert "zapytał" not in polish["exceptions"]["update_restart_unconfirmed"]["message"]
    for language in ("en", "de", "pl"):
        texts = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
        assert texts["config"]["abort"]["restart_withdrawn"]
        assert texts["exceptions"]["update_withdrawn"]["message"]


# What each language says where the sentences were wrong, and what it may no longer say.
#
# `withdraw_failed` is every withdrawal that did not end with the old files back: opkg's
# lock held (nothing was put back), a restore that failed part-way (the package records are
# written first and the plugin directory after them, so some of it is back), and one whose
# end was not seen. "The new ones are still on the receiver" is true of the first only.
#
# `restart_unconfirmed` is a withdrawal that did put the old files back, after a restart
# request OpenWebif never confirmed. Whether the image is asking on the television is not
# known - so the sentence may not say it asked, and has to say what holds if it did: the
# files are as before, so either answer is safe.
WITHDRAWAL_WORDS = {
    "en": {
        "partly": "partly the new ones and partly the previous ones",
        "whole": "the new ones are still on the receiver",
        "restarts": "restarts the interface on the files that are there now",
        "if_asked": 'If the television shows a question about restarting, answering "yes" '
        'or "no" is safe.',
    },
    "pl": {
        "partly": "częściowo nowe, a częściowo poprzednie",
        "whole": "więc na dekoderze są nowe",
        "restarts": "uruchomi interfejs z plikami, które są tam teraz",
        "if_asked": "Jeśli na ekranie telewizora widać pytanie o ponowne uruchomienie, "
        "odpowiedź „tak” albo „nie” jest bezpieczna.",
    },
    "de": {
        "partly": "teils die neuen und teils die bisherigen",
        "whole": "die neuen liegen noch auf dem Receiver",
        "restarts": "startet die Oberfläche mit den Dateien, die jetzt dort liegen",
        "if_asked": "Zeigt der Fernseher eine Frage zum Neustart, sind „Ja“ und „Nein“ "
        "beide unbedenklich.",
    },
}


@pytest.mark.parametrize("language", ["en", "pl", "de"])
def test_a_withdrawal_that_did_not_complete_does_not_say_which_files_are_there(
    language: str,
) -> None:
    texts = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
    words = WITHDRAWAL_WORDS[language]

    for sentence in (
        texts["config"]["abort"]["withdraw_failed"],
        texts["options"]["abort"]["withdraw_failed"],
        texts["exceptions"]["update_withdraw_failed"]["message"],
    ):
        assert words["partly"] in sentence
        assert words["whole"] not in sentence
        # "Yes" starts what is on the receiver, which need not be the new version whole.
        assert words["restarts"] in sentence


@pytest.mark.parametrize("language", ["en", "pl", "de"])
def test_an_unconfirmed_restart_says_what_holds_if_a_question_is_on_screen(
    language: str,
) -> None:
    texts = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
    words = WITHDRAWAL_WORDS[language]

    for sentence in (
        texts["config"]["abort"]["restart_unconfirmed"],
        texts["options"]["abort"]["restart_unconfirmed"],
        texts["exceptions"]["update_restart_unconfirmed"]["message"],
    ):
        assert sentence.endswith(words["if_asked"])
    # The confirmed question keeps its own sentence: there the image did ask.
    assert words["if_asked"] not in texts["config"]["abort"]["restart_withdrawn"]


def test_the_two_withdrawal_codes_are_told_apart_by_whether_the_files_went_back() -> None:
    """The code path the sentences describe, pinned: only `done` and `overlap` are "back"."""
    from custom_components.enigma2_mqtt import installer  # noqa: PLC0415

    for withdrawal in ("done", "overlap"):
        assert (
            installer._withdrawn_code(installer._UNCONFIRMED, withdrawal)
            is InstallerErrorCode.RESTART_UNCONFIRMED
        )
        assert (
            installer._withdrawn_code(installer._QUESTION, withdrawal)
            is InstallerErrorCode.RESTART_WITHDRAWN
        )
    for withdrawal in ("busy", "failed", installer._UNOBSERVED):
        for outcome in (installer._QUESTION, installer._UNCONFIRMED):
            assert (
                installer._withdrawn_code(outcome, withdrawal)
                is InstallerErrorCode.WITHDRAW_FAILED
            )

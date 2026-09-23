"""A receiver that cannot be woken over the network says so on the two buttons it concerns.

„Głębokie uśpienie" implies that a magic packet brings the receiver back, and „Obudź
(WoL)" that it will. From plugin 0.3.0 the receiver reports `info.wol.supported`, read
from the image's own Wake-on-LAN switch; where that is `false`, both buttons carry a
`wake_on_lan` attribute whose translated value says what wakes the box instead. Where it
is `true`, or where the plugin reports nothing, nothing changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_mock_service,
)

from custom_components.enigma2_mqtt.const import CONF_DANGEROUS_BUTTONS, DOMAIN

from .conftest import (
    AVAILABILITY_TOPIC,
    INFO,
    INFO_TOPIC,
    MAC,
    async_setup_box_then_retained,
)

DEEP_STANDBY = "button.dekoder_salon_deep_standby"
WAKE = "button.dekoder_salon_wake"
# The receiver the specification measured: the image found no switch to arm.
UNSUPPORTED = {"supported": False, "armed": None, "iface": "eth0", "mechanism": None}
SUPPORTED = {"supported": True, "armed": True, "iface": "eth0", "mechanism": "fp"}
TRANSLATIONS = Path(__file__).parents[1] / "custom_components" / DOMAIN / "translations"


def _info(wol: Any = None, **extra: Any) -> str:
    payload = {**INFO, "settings": {"deep_standby_allowed": True}, **extra}
    if wol is not None:
        payload["wol"] = wol
    return json.dumps(payload)


async def _setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    box_on_the_broker: dict[str, str | bytes],
    info: str,
) -> None:
    box_on_the_broker[INFO_TOPIC] = info
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)


async def test_a_receiver_that_cannot_be_woken_says_so_on_both_buttons(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Both buttons that promise a wake carry the note; no other button does."""
    await _setup(hass, config_entry, box_on_the_broker, _info(UNSUPPORTED))

    for entity_id in (DEEP_STANDBY, WAKE):
        assert hass.states.get(entity_id).attributes.get("wake_on_lan") == "not_supported"
    for entity_id in ("button.dekoder_salon_reboot", "button.dekoder_salon_screenshot"):
        assert "wake_on_lan" not in hass.states.get(entity_id).attributes


@pytest.mark.parametrize(
    "info",
    [
        pytest.param(_info(SUPPORTED), id="supported"),
        pytest.param(_info(), id="older-plugin-without-wol"),
        pytest.param(_info({**UNSUPPORTED, "supported": "false"}), id="string-false"),
        pytest.param(_info({**UNSUPPORTED, "supported": 0}), id="zero"),
        pytest.param(_info({**UNSUPPORTED, "supported": None}), id="null"),
        pytest.param(_info(["supported", False]), id="not-an-object"),
    ],
)
async def test_nothing_changes_unless_the_receiver_states_false(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    info: str,
) -> None:
    """Silence, or an answer that is not a boolean, is not „cannot be woken"."""
    await _setup(hass, config_entry, box_on_the_broker, info)

    assert config_entry.runtime_data.wake_on_lan_supported is not False
    for entity_id in (DEEP_STANDBY, WAKE):
        assert "wake_on_lan" not in hass.states.get(entity_id).attributes


async def test_the_note_follows_the_receiver_without_a_reload(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A plugin upgrade, or another image, answers on the next `info`."""
    await _setup(hass, config_entry, box_on_the_broker, _info())
    assert "wake_on_lan" not in hass.states.get(WAKE).attributes

    async_fire_mqtt_message(hass, INFO_TOPIC, _info(UNSUPPORTED))
    await hass.async_block_till_done()
    assert hass.states.get(WAKE).attributes.get("wake_on_lan") == "not_supported"

    async_fire_mqtt_message(hass, INFO_TOPIC, _info(SUPPORTED))
    await hass.async_block_till_done()
    assert "wake_on_lan" not in hass.states.get(WAKE).attributes


async def test_the_note_stays_while_the_receiver_is_asleep_and_the_packet_is_still_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """That is when somebody reads it; and the button still does what it did."""
    await _setup(hass, config_entry, box_on_the_broker, _info(UNSUPPORTED))
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    state = hass.states.get(WAKE)
    assert state.state != STATE_UNAVAILABLE
    assert state.attributes.get("wake_on_lan") == "not_supported"

    magic_packets = async_mock_service(hass, "wake_on_lan", "send_magic_packet")
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: WAKE}, blocking=True
    )
    assert magic_packets[0].data["mac"] == MAC


@pytest.mark.parametrize("language", ["en", "pl", "de"])
async def test_every_language_says_what_wakes_the_receiver_instead(
    hass: HomeAssistant, language: str
) -> None:
    """The value is a key; what a household reads is its translation, per button."""
    translations = await async_get_translations(hass, language, "entity", [DOMAIN])
    prefix = f"component.{DOMAIN}.entity.button"
    for key in ("deep_standby", "wake"):
        attribute = f"{prefix}.{key}.state_attributes.wake_on_lan"
        assert translations[f"{attribute}.name"] == "Wake-on-LAN"
        assert translations[f"{attribute}.state.not_supported"]
    assert (
        translations[f"{prefix}.deep_standby.state_attributes.wake_on_lan.state.not_supported"]
        != translations[f"{prefix}.wake.state_attributes.wake_on_lan.state.not_supported"]
    )


def test_neither_button_is_renamed_in_any_language() -> None:
    """On a Polish installation the entity id comes from the Polish name.

    Renaming either button in any language would move its entity id on some
    installation and take the automations, dashboards and history with it.
    """
    names = {
        "en": ("Deep standby", "Wake"),
        "pl": ("Głębokie uśpienie", "Obudź (WoL)"),
        "de": ("Tiefschlaf", "Wecken (WoL)"),
    }
    for language, expected in names.items():
        buttons = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))[
            "entity"
        ]["button"]
        assert (buttons["deep_standby"]["name"], buttons["wake"]["name"]) == expected

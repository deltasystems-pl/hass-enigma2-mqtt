"""The press combo's three receiver settings as entities: the key, the presses, the gap."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.enigma2_mqtt.const import MENU_COMBO_KEYS
from custom_components.enigma2_mqtt.diagnostics import async_get_config_entry_diagnostics
from custom_components.enigma2_mqtt.number import (
    Enigma2MenuComboGap,
    Enigma2MenuComboPresses,
    gap_ms,
)

from .conftest import (
    INFO,
    INFO_TOPIC,
    NODE_ID,
    SLUG,
    async_arm_box_error,
    async_setup_box,
    command_topic,
    published_payloads,
)
from .menu_common import EDITOR_INFO, menu_entry
from .test_translations import LANGUAGES, TRANSLATIONS

KEY = f"select.{SLUG}_menu_shortcut_key"
PRESSES = f"number.{SLUG}_menu_shortcut_presses"
GAP = f"number.{SLUG}_menu_shortcut_gap"
COMBO = (KEY, PRESSES, GAP)
SETTINGS = {
    "menu_allowed": True,
    "menu_hotkey": "help_long",
    "menu_transparency": 20,
    "menu_combo_key": "previous",
    "menu_combo_presses": 3,
    "menu_combo_gap_ms": 1200,
}


def _info(settings: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    info = {**EDITOR_INFO, **extra}
    if settings is not None:
        info["settings"] = settings
    return info


async def _setup(
    hass: HomeAssistant, retained: dict[str, str | bytes], settings: dict[str, Any] | None
) -> None:
    retained[INFO_TOPIC] = json.dumps(_info(settings))
    await async_setup_box(hass, menu_entry(None))


async def _arm_settings_echo(hass: HomeAssistant, settings: dict[str, Any]) -> None:
    """Make the fake receiver apply a partial `cmd/config` and republish `info`."""

    @callback
    def _command_received(msg: Any) -> None:
        payload = msg.payload.decode() if isinstance(msg.payload, bytes) else msg.payload
        settings.update(json.loads(payload))
        async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(_info(settings)))

    await mqtt.async_subscribe(hass, command_topic("config"), _command_received)


async def _set_gap(hass: HomeAssistant, seconds: float) -> None:
    await hass.services.async_call(
        "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: GAP, ATTR_VALUE: seconds}, blocking=True
    )


# Creation


async def test_all_three_exist_for_a_receiver_that_reports_the_combo(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker, dict(SETTINGS))

    key = hass.states.get(KEY)
    presses = hass.states.get(PRESSES)
    gap = hass.states.get(GAP)
    assert key is not None and key.state == "previous"
    assert key.attributes["options"] == ["off", "previous", "exit", "playpause", "stop"]
    assert presses is not None and presses.state == "3"
    assert (presses.attributes["min"], presses.attributes["max"]) == (2, 5)
    assert presses.attributes["step"] == 1
    assert gap is not None and gap.state == "1.2"
    assert (gap.attributes["min"], gap.attributes["max"]) == (0.3, 3.0)
    assert gap.attributes["step"] == 0.1
    assert gap.attributes["unit_of_measurement"] == "s"
    registry = er.async_get(hass)
    unique_ids = set()
    for entity_id in COMBO:
        entry = registry.async_get(entity_id)
        assert entry is not None and entry.entity_category is EntityCategory.CONFIG
        unique_ids.add(entry.unique_id)
    assert unique_ids == {
        f"{NODE_ID}_menu_combo_key",
        f"{NODE_ID}_menu_combo_presses",
        f"{NODE_ID}_menu_combo_gap",
    }


@pytest.mark.parametrize(
    "settings",
    [
        None,
        # A receiver before the combo: the hotkey's two, and nothing of the combo.
        {"menu_allowed": True, "menu_hotkey": "help_long", "menu_transparency": 20},
        # The count and the gap without the key are not a receiver that has the combo.
        {"menu_transparency": 20, "menu_combo_presses": 3, "menu_combo_gap_ms": 1200},
        # Without the transparency the receiver takes no partial `cmd/config`.
        {"menu_combo_key": "off", "menu_combo_presses": 3, "menu_combo_gap_ms": 1200},
    ],
)
async def test_none_exists_without_the_combo_key_setting(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    settings: dict[str, Any] | None,
) -> None:
    await _setup(hass, box_on_the_broker, settings)

    for entity_id in COMBO:
        assert hass.states.get(entity_id) is None, entity_id


async def test_the_key_alone_creates_all_three_and_the_missing_two_are_unavailable(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker, {"menu_transparency": 20, "menu_combo_key": "exit"})

    assert hass.states.get(KEY).state == "exit"
    assert hass.states.get(PRESSES).state == STATE_UNAVAILABLE
    assert hass.states.get(GAP).state == STATE_UNAVAILABLE


async def test_created_when_info_starts_reporting_it_and_unavailable_after_a_downgrade(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """A plugin that stops reporting the combo is an older one; nothing is deleted."""
    old = {"menu_allowed": True, "menu_hotkey": "help_long", "menu_transparency": 20}
    await _setup(hass, box_on_the_broker, dict(old))
    assert hass.states.get(KEY) is None

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(_info(dict(SETTINGS))))
    await hass.async_block_till_done()
    assert hass.states.get(KEY).state == "previous"
    assert hass.states.get(PRESSES).state == "3"
    assert hass.states.get(GAP).state == "1.2"

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(_info(dict(old))))
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for entity_id in COMBO:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id
        assert registry.async_get(entity_id) is not None, entity_id
    # The hotkey's two are untouched by the combo's going.
    assert hass.states.get(f"select.{SLUG}_menu_hotkey").state == "help_long"

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "settings": {}}))
    await hass.async_block_till_done()
    for entity_id in COMBO:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id
        assert registry.async_get(entity_id) is not None, entity_id


# Read-back


@pytest.mark.parametrize(
    ("reported", "shown"),
    [
        (1200, "1.2"),
        (300, "0.3"),
        (3000, "3.0"),
        # Off the steps or out of range: shown as the receiver says it, never rewritten.
        (1250, "1.25"),
        (1, "0.001"),
        (5000, "5.0"),
        ("1200", STATE_UNKNOWN),
        (1.2, STATE_UNKNOWN),
        (True, STATE_UNKNOWN),
        (None, STATE_UNKNOWN),
    ],
)
async def test_the_gap_reads_the_receivers_milliseconds_as_seconds(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    reported: Any,
    shown: str,
) -> None:
    await _setup(hass, box_on_the_broker, {**SETTINGS, "menu_combo_gap_ms": reported})

    assert hass.states.get(GAP).state == shown


@pytest.mark.parametrize(
    ("reported", "shown"),
    [(2, "2"), (5, "5"), (7, "7"), ("3", STATE_UNKNOWN), (3.0, STATE_UNKNOWN),
     (False, STATE_UNKNOWN)],
)
async def test_the_presses_read_as_reported(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    reported: Any,
    shown: str,
) -> None:
    await _setup(hass, box_on_the_broker, {**SETTINGS, "menu_combo_presses": reported})

    assert hass.states.get(PRESSES).state == shown


@pytest.mark.parametrize("reported", ["help_long", "back", "", 1, None, ["previous"]])
async def test_a_key_the_select_does_not_offer_reads_unknown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    reported: Any,
) -> None:
    await _setup(hass, box_on_the_broker, {**SETTINGS, "menu_combo_key": reported})

    assert hass.states.get(KEY).state == STATE_UNKNOWN


# Writes


@pytest.mark.parametrize("option", ["off", "exit", "playpause", "stop"])
async def test_choosing_the_key_sends_its_key_alone_and_waits_for_it(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], option: str
) -> None:
    settings = dict(SETTINGS)
    await _setup(hass, box_on_the_broker, settings)
    await _arm_settings_echo(hass, settings)

    await hass.services.async_call(
        "select", SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: KEY, ATTR_OPTION: option},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert published_payloads(mqtt_mock, command_topic("config")) == [
        json.dumps({"menu_combo_key": option}, separators=(",", ":"))
    ]
    assert hass.states.get(KEY).state == option


@pytest.mark.parametrize("presses", [2, 4, 5, 3.0])
async def test_setting_the_presses_sends_a_whole_number_alone(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], presses: float
) -> None:
    settings = dict(SETTINGS)
    await _setup(hass, box_on_the_broker, settings)
    await _arm_settings_echo(hass, settings)

    await hass.services.async_call(
        "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: PRESSES, ATTR_VALUE: presses},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert published_payloads(mqtt_mock, command_topic("config")) == [
        f'{{"menu_combo_presses":{int(presses)}}}'
    ]
    assert hass.states.get(PRESSES).state == str(int(presses))


@pytest.mark.parametrize(
    ("seconds", "milliseconds"),
    [
        (0.3, 300),
        (1.2, 1200),
        (3.0, 3000),
        (3, 3000),
        (1.5, 1500),
        # The float noise a slider or a template leaves is rounded away.
        (1.2000000001, 1200),
        (1.1999999999, 1200),
        (0.1 + 0.2, 300),
        (2.9999999999, 3000),
    ],
)
async def test_setting_the_gap_sends_milliseconds_alone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    seconds: float,
    milliseconds: int,
) -> None:
    settings = {**SETTINGS, "menu_combo_gap_ms": 1000}
    await _setup(hass, box_on_the_broker, settings)
    await _arm_settings_echo(hass, settings)

    await _set_gap(hass, seconds)
    await hass.async_block_till_done()

    assert published_payloads(mqtt_mock, command_topic("config")) == [
        f'{{"menu_combo_gap_ms":{milliseconds}}}'
    ]
    assert hass.states.get(GAP).state == str(milliseconds / 1000)


@pytest.mark.parametrize(
    ("seconds", "milliseconds"),
    [(0.3, 300), (1.2, 1200), (3.0, 3000), (1.2000000001, 1200), (0.7, 700), (2.3, 2300)],
)
def test_the_gap_conversion_is_round_to_the_millisecond(seconds: float, milliseconds: int) -> None:
    converted = gap_ms(seconds)
    assert converted == milliseconds and isinstance(converted, int)


@pytest.mark.parametrize("seconds", [1.25, 0.35, 2.95, 1.21])
async def test_a_gap_off_the_steps_is_refused_before_anything_is_sent(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], seconds: float
) -> None:
    await _setup(hass, box_on_the_broker, dict(SETTINGS))

    with pytest.raises(ServiceValidationError) as raised:
        await _set_gap(hass, seconds)

    assert raised.value.translation_key == "menu_combo_gap_step"
    assert published_payloads(mqtt_mock, command_topic("config")) == []


@pytest.mark.parametrize(("entity_id", "value"), [(GAP, 0.2), (GAP, 3.1), (GAP, 0), (PRESSES, 1),
                                                  (PRESSES, 6)])
async def test_a_value_out_of_range_is_refused_before_anything_is_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    entity_id: str,
    value: float,
) -> None:
    await _setup(hass, box_on_the_broker, dict(SETTINGS))

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
            blocking=True,
        )

    assert published_payloads(mqtt_mock, command_topic("config")) == []


@pytest.mark.parametrize("value", [2.5, 3.0001])
async def test_presses_that_are_not_whole_are_refused_before_anything_is_sent(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], value: float
) -> None:
    await _setup(hass, box_on_the_broker, dict(SETTINGS))

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: PRESSES, ATTR_VALUE: value},
            blocking=True,
        )

    assert raised.value.translation_key == "menu_combo_presses_step"
    assert published_payloads(mqtt_mock, command_topic("config")) == []


@pytest.mark.parametrize(
    ("entity_class", "values"),
    [
        (Enigma2MenuComboGap, (0.2, 0.29, 3.01, 3.1, 0.0, -1.2)),
        (Enigma2MenuComboPresses, (1, 6, 0, -3)),
    ],
)
async def test_the_entities_refuse_out_of_range_on_their_own(
    entity_class: type, values: tuple[float, ...]
) -> None:
    """The entity's own check, without Home Assistant's range check in front of it."""
    entity = entity_class.__new__(entity_class)
    sent: list[Any] = []

    async def _write(value: Any) -> None:
        sent.append(value)

    entity.async_write_setting = _write
    for value in values:
        with pytest.raises(ServiceValidationError):
            await entity.async_set_native_value(value)
    assert sent == []
    # And the edges themselves are taken.
    for value in (0.3, 3.0) if entity_class is Enigma2MenuComboGap else (2, 5):
        await entity.async_set_native_value(value)
    assert sent == ([300, 3000] if entity_class is Enigma2MenuComboGap else [2, 5])


async def test_a_refusal_from_the_receiver_is_raised(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker, dict(SETTINGS))
    await async_arm_box_error(hass, "config", "menu_combo_key: not allowed")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select", SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: KEY, ATTR_OPTION: "stop"},
            blocking=True,
        )
    with pytest.raises(HomeAssistantError):
        await _set_gap(hass, 2.0)

    assert hass.states.get(KEY).state == "previous"
    assert hass.states.get(GAP).state == "1.2"


# Translations and diagnostics


def test_every_language_names_every_combo_key() -> None:
    for language in LANGUAGES:
        strings = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
        entity = strings["entity"]
        states = entity["select"]["menu_combo_key"]["state"]
        assert set(states) == set(MENU_COMBO_KEYS), language
        assert all(isinstance(label, str) and label for label in states.values()), language
        for key in ("menu_combo_presses", "menu_combo_gap"):
            assert entity["number"][key]["name"], (language, key)
        for key in ("menu_combo_presses_step", "menu_combo_gap_step"):
            assert "{value}" in strings["exceptions"][key]["message"], (language, key)


async def test_diagnostics_carry_the_bound_combo_as_it_is(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    menu = {
        "screen": "styled",
        "hotkey": "help_long",
        "reason": None,
        "combo": {
            "key": "previous",
            "presses": 3,
            "gap_ms": 1200,
            "replaces": ["InfobarChannelSelection.historyBack", "ButtonSetup.previous"],
        },
        "combo_reason": None,
    }
    box_on_the_broker[INFO_TOPIC] = json.dumps(_info(dict(SETTINGS), menu=menu))
    await async_setup_box(hass, menu_entry(None))
    entry = hass.config_entries.async_entries("enigma2_mqtt")[0]

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["info"]["menu"] == menu
    assert diagnostics["info"]["settings"]["menu_combo_gap_ms"] == 1200


async def test_diagnostics_carry_an_unbound_combo_and_its_reason(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    menu = {
        "screen": "styled",
        "hotkey": None,
        "reason": "The menu is switched off on the receiver.",
        "combo": None,
        "combo_reason": "The menu is switched off on the receiver.",
    }
    box_on_the_broker[INFO_TOPIC] = json.dumps(_info(dict(SETTINGS), menu=menu))
    await async_setup_box(hass, menu_entry(None))
    entry = hass.config_entries.async_entries("enigma2_mqtt")[0]

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["info"]["menu"] == menu

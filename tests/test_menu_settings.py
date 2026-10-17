"""The menu's two receiver settings as entities: transparency and the hotkey."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from homeassistant.components import mqtt
from homeassistant.components.number import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from .conftest import (
    INFO,
    INFO_TOPIC,
    SLUG,
    async_arm_box_error,
    async_setup_box,
    command_topic,
    published_payloads,
)
from .menu_common import EDITOR_INFO, menu_entry

TRANSPARENCY = f"number.{SLUG}_menu_transparency"
HOTKEY = f"select.{SLUG}_menu_hotkey"
SETTINGS = {"menu_allowed": True, "menu_hotkey": "help_long", "menu_transparency": 20}


def _info(settings: dict[str, Any] | None) -> dict[str, Any]:
    info = dict(EDITOR_INFO)
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


async def test_both_exist_for_a_receiver_that_reports_the_settings(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker, dict(SETTINGS))

    transparency = hass.states.get(TRANSPARENCY)
    hotkey = hass.states.get(HOTKEY)
    assert transparency is not None and transparency.state == "20"
    assert transparency.attributes["min"] == 0
    assert transparency.attributes["max"] == 80
    assert transparency.attributes["step"] == 10
    assert transparency.attributes["mode"] == "slider"
    assert transparency.attributes["unit_of_measurement"] == "%"
    assert hotkey is not None and hotkey.state == "help_long"
    assert hotkey.attributes["options"] == ["help_long", "red_long", "f3", "f4", "off"]
    registry = er.async_get(hass)
    for entity_id in (TRANSPARENCY, HOTKEY):
        entry = registry.async_get(entity_id)
        assert entry is not None and entry.entity_category is EntityCategory.CONFIG


@pytest.mark.parametrize(
    "settings",
    [
        None,
        {"menu_allowed": True},
        # A plugin with the hotkey but without the transparency takes no partial
        # `cmd/config`, so neither is offered.
        {"menu_allowed": True, "menu_hotkey": "help_long"},
    ],
)
async def test_neither_exists_without_the_transparency_setting(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    settings: dict[str, Any] | None,
) -> None:
    await _setup(hass, box_on_the_broker, settings)

    assert hass.states.get(TRANSPARENCY) is None
    assert hass.states.get(HOTKEY) is None


async def test_the_transparency_alone_creates_no_hotkey(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker, {"menu_transparency": 40})

    assert hass.states.get(TRANSPARENCY).state == "40"
    assert hass.states.get(HOTKEY) is None


async def test_created_when_info_starts_reporting_them_and_kept_when_it_stops(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """A plugin that stops reporting a setting is an older one; nothing is deleted."""
    await _setup(hass, box_on_the_broker, {"menu_allowed": True})
    assert hass.states.get(TRANSPARENCY) is None

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(_info(dict(SETTINGS))))
    await hass.async_block_till_done()
    assert hass.states.get(TRANSPARENCY).state == "20"
    assert hass.states.get(HOTKEY).state == "help_long"

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "settings": {}}))
    await hass.async_block_till_done()
    assert hass.states.get(TRANSPARENCY).state == STATE_UNAVAILABLE
    assert hass.states.get(HOTKEY).state == STATE_UNAVAILABLE
    assert er.async_get(hass).async_get(TRANSPARENCY) is not None


@pytest.mark.parametrize("reported", [25, 90, -10, "20", True, False, None, 20.5])
async def test_a_value_outside_the_setting_reads_unknown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    reported: Any,
) -> None:
    await _setup(
        hass, box_on_the_broker, {**SETTINGS, "menu_transparency": reported, "menu_hotkey": "x"}
    )

    assert hass.states.get(TRANSPARENCY).state == STATE_UNKNOWN
    assert hass.states.get(HOTKEY).state == STATE_UNKNOWN


async def test_setting_the_transparency_sends_its_key_alone_and_waits_for_it(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    settings = dict(SETTINGS)
    await _setup(hass, box_on_the_broker, settings)
    await _arm_settings_echo(hass, settings)

    await hass.services.async_call(
        "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: TRANSPARENCY, ATTR_VALUE: 60},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert published_payloads(mqtt_mock, command_topic("config")) == ['{"menu_transparency":60}']
    assert hass.states.get(TRANSPARENCY).state == "60"


@pytest.mark.parametrize("value", [25, 15.5])
async def test_a_transparency_off_the_steps_is_refused_before_anything_is_sent(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes], value: float
) -> None:
    await _setup(hass, box_on_the_broker, dict(SETTINGS))

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: TRANSPARENCY, ATTR_VALUE: value},
            blocking=True,
        )

    assert raised.value.translation_key == "menu_transparency_step"
    assert published_payloads(mqtt_mock, command_topic("config")) == []


async def test_a_refusal_from_the_receiver_is_raised(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    await _setup(hass, box_on_the_broker, dict(SETTINGS))
    await async_arm_box_error(hass, "config", "menu_transparency: not allowed")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: TRANSPARENCY, ATTR_VALUE: 40},
            blocking=True,
        )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select", SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: HOTKEY, ATTR_OPTION: "f3"},
            blocking=True,
        )

    assert hass.states.get(TRANSPARENCY).state == "20"


async def test_choosing_the_hotkey_sends_its_key_alone_and_waits_for_it(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    settings = dict(SETTINGS)
    await _setup(hass, box_on_the_broker, settings)
    await _arm_settings_echo(hass, settings)

    await hass.services.async_call(
        "select", SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: HOTKEY, ATTR_OPTION: "red_long"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert published_payloads(mqtt_mock, command_topic("config")) == ['{"menu_hotkey":"red_long"}']
    assert hass.states.get(HOTKEY).state == "red_long"


async def test_the_write_waits_for_the_receiver_to_report_the_new_value(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker: dict[str, str | bytes]
) -> None:
    """The proof is `info.settings` carrying the value, not the publish."""
    settings = dict(SETTINGS)
    await _setup(hass, box_on_the_broker, settings)
    box = hass.config_entries.async_entries("enigma2_mqtt")[0].runtime_data
    seen: list[Any] = []

    async def _command(name: str, payload: str, *, effect: Any = None, **_kw: Any) -> None:
        seen.append((name, payload, effect))

    with patch.object(box, "async_command", _command):
        await hass.services.async_call(
            "number", SERVICE_SET_VALUE, {ATTR_ENTITY_ID: TRANSPARENCY, ATTR_VALUE: 70},
            blocking=True,
        )
        await hass.services.async_call(
            "select", SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: HOTKEY, ATTR_OPTION: "f4"},
            blocking=True,
        )

    (name, payload, effect), (_name, hotkey_payload, hotkey_effect) = seen
    assert (name, payload) == ("config", '{"menu_transparency":70}')
    assert hotkey_payload == '{"menu_hotkey":"f4"}'
    assert effect() is False and hotkey_effect() is False
    async_fire_mqtt_message(
        hass, INFO_TOPIC,
        json.dumps(_info({**SETTINGS, "menu_transparency": 70, "menu_hotkey": "f4"})),
    )
    await hass.async_block_till_done()
    assert effect() is True and hotkey_effect() is True

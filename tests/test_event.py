"""Key presses: the event entity, the bus event, and the device triggers on top."""

from __future__ import annotations

import json

from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_get_device_automations,
    async_mock_service,
)

from custom_components.enigma2_mqtt.const import (
    ATTR_KEY,
    ATTR_NODE_ID,
    ATTR_PRESS,
    DOMAIN,
    EVENT_KEY,
)
from custom_components.enigma2_mqtt.device_trigger import TRIGGER_TYPES

from .conftest import KEY_TOPIC, NODE_ID, async_setup_box

EVENT_ENTITY = "event.dekoder_salon_key"


def _device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the device id of the example box."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_ID), entry.entry_id
    )
    assert device is not None
    return device.id


async def test_a_key_press_reaches_the_event_entity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The key name is the event type and the length of the press an attribute."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(
        hass, KEY_TOPIC, json.dumps({"key": "KEY_RED", "press": "long"})
    )
    await hass.async_block_till_done()

    state = hass.states.get(EVENT_ENTITY)
    assert state.attributes["event_type"] == "KEY_RED"
    assert state.attributes[ATTR_PRESS] == "long"


async def test_an_unknown_key_is_dropped_by_the_entity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An event entity may only fire types it declared; it must not crash on the rest."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(
        hass, KEY_TOPIC, json.dumps({"key": "KEY_INVENTED", "press": "short"})
    )
    await hass.async_block_till_done()

    assert hass.states.get(EVENT_ENTITY).state == "unknown"


async def test_every_key_reaches_the_bus(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The bus event is what device triggers listen to, entity or no entity."""
    await async_setup_box(hass, config_entry)
    device_id = _device_id(hass, config_entry)

    fired: list[dict] = []
    hass.bus.async_listen(EVENT_KEY, lambda event: fired.append(dict(event.data)))

    async_fire_mqtt_message(
        hass, KEY_TOPIC, json.dumps({"key": "KEY_INVENTED", "press": "short"})
    )
    await hass.async_block_till_done()

    assert fired == [
        {
            ATTR_DEVICE_ID: device_id,
            ATTR_NODE_ID: NODE_ID,
            ATTR_KEY: "KEY_INVENTED",
            ATTR_PRESS: "short",
        }
    ]


@pytest.mark.parametrize(
    "payload",
    ["not json", "{}", json.dumps({"press": "short"}), json.dumps({"key": ""})],
)
async def test_a_broken_key_payload_is_ignored(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    payload: str,
) -> None:
    """A key topic with no key in it is not an event."""
    await async_setup_box(hass, config_entry)

    fired: list[dict] = []
    hass.bus.async_listen(EVENT_KEY, lambda event: fired.append(dict(event.data)))

    async_fire_mqtt_message(hass, KEY_TOPIC, payload)
    await hass.async_block_till_done()

    assert fired == []


async def test_an_unknown_press_is_treated_as_short(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Fail towards the harmless reading, rather than inventing a third kind."""
    await async_setup_box(hass, config_entry)

    fired: list[dict] = []
    hass.bus.async_listen(EVENT_KEY, lambda event: fired.append(dict(event.data)))

    async_fire_mqtt_message(
        hass, KEY_TOPIC, json.dumps({"key": "KEY_OK", "press": "double"})
    )
    await hass.async_block_till_done()

    assert fired[0][ATTR_PRESS] == "short"


async def test_the_device_offers_eight_colour_key_triggers(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Four colours, short and long, in the automation editor."""
    await async_setup_box(hass, config_entry)
    device_id = _device_id(hass, config_entry)

    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    ours = sorted(
        trigger["type"] for trigger in triggers if trigger["domain"] == DOMAIN
    )

    assert ours == sorted(TRIGGER_TYPES)
    assert len(ours) == 8


async def test_a_device_with_no_receiver_offers_none(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A device id that is not one of ours is not an error, it is an empty list."""
    from custom_components.enigma2_mqtt.device_trigger import async_get_triggers

    await async_setup_box(hass, config_entry)
    assert await async_get_triggers(hass, "not-a-device") == []


async def test_a_device_trigger_fires_on_its_key_only(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The trigger has to match the device, the key and the length of the press."""
    await async_setup_box(hass, config_entry)
    device_id = _device_id(hass, config_entry)
    calls = async_mock_service(hass, "test", "automation")

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "trigger": {
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": device_id,
                    "type": "yellow_long",
                },
                "action": {"service": "test.automation"},
            }
        },
    )
    await hass.async_block_till_done()

    # The right key, the wrong press.
    async_fire_mqtt_message(
        hass, KEY_TOPIC, json.dumps({"key": "KEY_YELLOW", "press": "short"})
    )
    await hass.async_block_till_done()
    assert len(calls) == 0

    # The wrong key, the right press.
    async_fire_mqtt_message(
        hass, KEY_TOPIC, json.dumps({"key": "KEY_BLUE", "press": "long"})
    )
    await hass.async_block_till_done()
    assert len(calls) == 0

    async_fire_mqtt_message(
        hass, KEY_TOPIC, json.dumps({"key": "KEY_YELLOW", "press": "long"})
    )
    await hass.async_block_till_done()
    assert len(calls) == 1

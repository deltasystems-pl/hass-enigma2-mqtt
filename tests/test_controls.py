"""The binary sensors, switches, the number and the buttons.

Four small platforms in one file, because each of them is one topic in and one command
out and a file apiece would be four copies of the same three fixtures.
"""

from __future__ import annotations

import json

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_mock_service,
)

from custom_components.enigma2_mqtt.const import CONF_DANGEROUS_BUTTONS, CONF_WOL_MAC

from .conftest import (
    AVAILABILITY_TOPIC,
    HDD_TOPIC,
    INFO,
    INFO_TOPIC,
    MAC,
    POWER_TOPIC,
    RECORDING_ACTIVE,
    RECORDING_TOPIC,
    VOLUME_TOPIC,
    assert_published,
    async_setup_box,
    command_topic,
)


async def test_the_recording_binary_sensor(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """This is what an automation checks before it reboots anything."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("binary_sensor.dekoder_salon_recording").state == STATE_OFF

    async_fire_mqtt_message(hass, RECORDING_TOPIC, json.dumps(RECORDING_ACTIVE))
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.dekoder_salon_recording")
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "running"


async def test_the_recording_disk_binary_sensor(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A disk that unmounts itself is the point of this topic."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get("binary_sensor.dekoder_salon_recording_disk")
    assert state.state == STATE_ON
    assert state.attributes["path"] == "/media/hdd"
    assert state.attributes["free_mb"] == 412330

    async_fire_mqtt_message(
        hass,
        HDD_TOPIC,
        json.dumps({"mounted": False, "path": "/media/hdd", "free_mb": None}),
    )
    await hass.async_block_till_done()

    assert (
        hass.states.get("binary_sensor.dekoder_salon_recording_disk").state == STATE_OFF
    )


async def test_the_power_switch_follows_the_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Standby is a switch, and the state topic is what settles it."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("switch.dekoder_salon_power").state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.dekoder_salon_power"},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("power"), "standby")

    # Optimistic: the switch has already moved, before the box said anything.
    assert hass.states.get("switch.dekoder_salon_power").state == STATE_OFF

    async_fire_mqtt_message(hass, POWER_TOPIC, "on")
    await hass.async_block_till_done()
    assert hass.states.get("switch.dekoder_salon_power").state == STATE_ON


async def test_the_mute_switch(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Mute lives on the volume topic, which carries both halves."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("switch.dekoder_salon_mute").state == STATE_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "switch.dekoder_salon_mute"},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("mute"), "ON")

    async_fire_mqtt_message(
        hass, VOLUME_TOPIC, json.dumps({"level": 35, "muted": True})
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.dekoder_salon_mute").state == STATE_ON


async def test_the_volume_number(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The number is the receiver's own 0-100 scale, not the media player's 0-1."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get("number.dekoder_salon_volume")
    assert state.state == "35.0"
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 100
    assert state.attributes["mode"] == "slider"

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: "number.dekoder_salon_volume", ATTR_VALUE: 20},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("volume"), "20")


@pytest.mark.parametrize(
    ("entity_id", "command"),
    [
        ("button.dekoder_salon_restart_gui", "restart_gui"),
        ("button.dekoder_salon_screenshot", "screenshot"),
        ("button.dekoder_salon_refresh_discovery", "discovery"),
    ],
)
async def test_the_safe_buttons_send_their_command(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    entity_id: str,
    command: str,
) -> None:
    """One press, one command, no state."""
    await async_setup_box(hass, config_entry)

    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )

    assert_published(mqtt_mock, command_topic(command), "PRESS")


async def test_the_dangerous_buttons_are_absent_by_default(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Deep standby and reboot are not one mis-tap from the volume."""
    await async_setup_box(hass, config_entry)

    assert hass.states.get("button.dekoder_salon_deep_standby") is None
    assert hass.states.get("button.dekoder_salon_reboot") is None


async def test_the_option_brings_the_dangerous_buttons_back(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """And turning it off again takes them out of the registry, not just off."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_deep_standby"},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("deep_standby"), "PRESS")

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_reboot"},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("reboot"), "PRESS")

    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: False}
    )
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("button.dekoder_salon_deep_standby") is None
    assert (
        er.async_get(hass).async_get("button.dekoder_salon_deep_standby") is None
    )


async def test_the_wake_button_works_while_the_box_is_gone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Every other entity is unavailable by then, which is why this one is not."""
    await async_setup_box(hass, config_entry)
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    assert (
        hass.states.get("button.dekoder_salon_screenshot").state == STATE_UNAVAILABLE
    )
    assert hass.states.get("button.dekoder_salon_wake").state != STATE_UNAVAILABLE

    magic_packets = async_mock_service(hass, "wake_on_lan", "send_magic_packet")
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_wake"},
        blocking=True,
    )

    assert magic_packets[0].data["mac"] == MAC


async def test_the_wol_option_overrides_the_reported_mac(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A box on Wi-Fi does not answer a packet sent to its cable port."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_WOL_MAC: "00:00:5e:00:53:02"}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    magic_packets = async_mock_service(hass, "wake_on_lan", "send_magic_packet")
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_wake"},
        blocking=True,
    )

    assert magic_packets[0].data["mac"] == "00:00:5e:00:53:02"


async def test_waking_a_box_that_reported_no_mac_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """There is nowhere to send the packet, and saying so beats sending it nowhere."""
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {key: value for key, value in INFO.items() if key != "mac"}
    )
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.dekoder_salon_wake"},
            blocking=True,
        )

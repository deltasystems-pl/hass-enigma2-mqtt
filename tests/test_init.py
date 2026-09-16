"""Setting a box up gives Home Assistant a device, and taking it down releases it."""

from __future__ import annotations

import json
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.box import manufacturer_for
from custom_components.enigma2_mqtt.const import DOMAIN

from .conftest import (
    AVAILABILITY_TOPIC,
    BOX_NAME,
    BOXTYPE,
    IMAGE,
    INFO,
    INFO_TOPIC,
    IP,
    NODE_ID,
    PLUGIN_VERSION,
)


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Tolerate the MQTT integration's own periodic timer.

    `mqtt_mock` sets up the real MQTT integration, which schedules
    `MQTT._async_start_misc_periodic` and does not cancel it on teardown. Home
    Assistant's own MQTT tests make the same allowance; it says nothing about this
    integration.
    """
    return True


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry to Home Assistant and set it up."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_registers_the_device(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """The retained announcement becomes a device page."""
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_ID), config_entry.entry_id
    )
    assert device is not None
    assert device.name == BOX_NAME
    assert device.manufacturer == "Vu+"
    assert device.model == BOXTYPE
    assert device.sw_version == f"{IMAGE} · plugin {PLUGIN_VERSION}"
    assert device.configuration_url == f"http://{IP}/"
    assert device.config_entry_id == config_entry.entry_id


async def test_setup_without_the_box_still_registers_the_device(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str],
    config_entry: MockConfigEntry
) -> None:
    """A box that is off is still a device, so the user can see it is off."""
    await _setup(hass, config_entry)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_ID), config_entry.entry_id
    )
    assert device is not None
    assert device.name == BOX_NAME
    assert device.manufacturer == "Enigma2"
    assert device.sw_version is None
    assert config_entry.runtime_data.available is False


async def test_availability_follows_the_last_will(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """`online` and `offline` on the availability topic move the box's flag."""
    await _setup(hass, config_entry)
    box = config_entry.runtime_data

    assert box.available is True

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()
    assert box.available is False

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "online")
    await hass.async_block_till_done()
    assert box.available is True


async def test_a_new_info_updates_the_device(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """Updating the plugin on the box updates what the device page reports."""
    await _setup(hass, config_entry)

    async_fire_mqtt_message(
        hass,
        INFO_TOPIC,
        json.dumps({**INFO, "plugin": "0.2.0", "ip": "192.0.2.13"}),
    )
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_ID), config_entry.entry_id
    )
    assert device is not None
    assert device.sw_version == f"{IMAGE} · plugin 0.2.0"
    assert device.configuration_url == "http://192.0.2.13/"


async def test_a_broken_info_is_ignored(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """A payload that is not a JSON object does not wipe what is known."""
    await _setup(hass, config_entry)
    box = config_entry.runtime_data

    async_fire_mqtt_message(hass, INFO_TOPIC, "not json")
    await hass.async_block_till_done()

    assert box.info["plugin"] == PLUGIN_VERSION


async def test_listeners_hear_every_change(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """The entity platforms of M3 subscribe to the box, not to MQTT."""
    await _setup(hass, config_entry)
    box = config_entry.runtime_data

    calls: list[None] = []
    remove = box.async_add_listener(lambda: calls.append(None))

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()
    assert len(calls) == 1

    remove()
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "online")
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_unload_releases_the_subscriptions(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """After an unload nothing arriving on the box's topics is acted on."""
    await _setup(hass, config_entry)
    box = config_entry.runtime_data

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()
    assert box.available is True


async def test_setup_retries_without_an_mqtt_client(
    hass: HomeAssistant, mqtt_mock, config_entry: MockConfigEntry
) -> None:
    """No broker is a reason to retry, not a reason to half-load."""
    with patch(
        "homeassistant.components.mqtt.async_wait_for_mqtt_client", return_value=False
    ):
        config_entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    ("boxtype", "manufacturer"),
    [
        ("vuuno4kse", "Vu+"),
        ("dm900", "Dream Multimedia"),
        ("gbquad4k", "GigaBlue"),
        ("et8500", "Xtrend"),
        ("xp1000", "Xtrend"),
        ("h9combo", "Zgemma"),
        ("somethingelse", "Enigma2"),
        (None, "Enigma2"),
    ],
)
async def test_manufacturer_is_derived_from_the_box_type(
    boxtype: str | None, manufacturer: str
) -> None:
    """A box type nobody mapped is still an Enigma2 receiver."""
    assert manufacturer_for(boxtype) == manufacturer

"""The Enigma2 MQTT integration.

Consumes the topics published by the `enigma2-mqtt-bridge` plugin that runs inside
enigma2 on the receiver, and turns them into a Home Assistant device.

M1 sets up the device and the MQTT plumbing behind it. The entity platforms land in M3;
`PLATFORMS` is deliberately empty rather than absent, so that milestone only appends to
a list instead of rewriting the setup.
"""

from __future__ import annotations

import logging

from homeassistant.components import mqtt
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration.

    There is nothing to configure in YAML: boxes are added through the config flow.
    """
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> bool:
    """Set up one box from a config entry."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="mqtt_not_available"
        )

    box = Enigma2Box(hass, entry)
    entry.runtime_data = box
    await box.async_start()
    box.async_register_device()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> bool:
    """Unload a box, releasing its subscriptions."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry.runtime_data.async_stop()
    return unload_ok

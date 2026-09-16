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
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .box import Enigma2Box, Enigma2MqttConfigEntry, command_topic
from .const import (
    CONF_BASE_TOPIC,
    CONF_NODE_ID,
    DEFAULT_BASE_TOPIC,
    DOMAIN,
    HA_MODE_DISCOVERY,
)

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


async def async_remove_entry(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> None:
    """Hand a removed box back to MQTT discovery.

    Adding a box switches its plugin into `integration` mode, which makes the plugin
    retract the discovery payloads the core MQTT integration builds entities from.
    Removing the entry without undoing that would leave the receiver publishing state
    that nothing listens to, and the user with no entities and no obvious reason why.
    This is best effort on purpose: the box may be off, the broker may be gone, and
    neither is a reason to refuse to remove a config entry.
    """
    try:
        if not await mqtt.async_wait_for_mqtt_client(hass):
            _LOGGER.debug(
                "MQTT is unavailable, leaving %s in integration mode",
                entry.data[CONF_NODE_ID],
            )
            return
        await mqtt.async_publish(
            hass,
            command_topic(
                entry.data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC),
                entry.data[CONF_NODE_ID],
                "ha_mode",
            ),
            HA_MODE_DISCOVERY,
            qos=1,
            retain=False,
        )
    except (HomeAssistantError, KeyError):
        _LOGGER.warning(
            "Could not switch %s back to discovery mode; do it on the receiver's "
            "setup screen if you want its MQTT discovery entities back",
            entry.data.get(CONF_NODE_ID),
        )

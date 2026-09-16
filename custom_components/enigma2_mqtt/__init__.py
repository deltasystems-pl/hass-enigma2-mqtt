"""The Enigma2 MQTT integration.

Consumes the topics published by the `enigma2-mqtt-bridge` plugin that runs inside
enigma2 on the receiver, and turns them into a Home Assistant device.

This module is the scaffold of the repository: it registers the domain and nothing
else. The config flow, the entity platforms and the MQTT plumbing land in M1 and M3
(see the roadmap in README.md).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The integration takes no YAML configuration. Once boxes are added through the config
# flow (M1) this becomes cv.config_entry_only_config_schema.
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration.

    There is nothing to configure in YAML: boxes are added through the config flow.
    """
    _LOGGER.debug("%s loaded", DOMAIN)
    return True

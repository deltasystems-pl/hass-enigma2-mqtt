"""Constants for the Enigma2 MQTT integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "enigma2_mqtt"

# The plugin announces every box it runs on under this prefix. The integration
# subscribes to `<DISCOVERY_PREFIX>/#` and each box owns `<DISCOVERY_PREFIX>/<node_id>/config`.
DISCOVERY_PREFIX: Final = "enigma2mqtt/discovery"

# State and command topics live under `<base_topic>/<node_id>/`. The base topic is
# configurable on the box; this is the plugin's default.
DEFAULT_BASE_TOPIC: Final = "enigma2"

# How the plugin presents a box to Home Assistant, switchable with `cmd/ha_mode`.
HA_MODE_DISCOVERY: Final = "discovery"
HA_MODE_INTEGRATION: Final = "integration"
HA_MODE_OFF: Final = "off"
HA_MODES: Final = (HA_MODE_DISCOVERY, HA_MODE_INTEGRATION, HA_MODE_OFF)

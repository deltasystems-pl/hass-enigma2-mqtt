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

# Config entry keys. `node_id` is also the unique id of the entry.
CONF_NODE_ID: Final = "node_id"
CONF_BASE_TOPIC: Final = "base_topic"
CONF_NAME: Final = "name"

# How the plugin presents a box to Home Assistant, switchable with `cmd/ha_mode`.
HA_MODE_DISCOVERY: Final = "discovery"
HA_MODE_INTEGRATION: Final = "integration"
HA_MODE_OFF: Final = "off"
HA_MODES: Final = (HA_MODE_DISCOVERY, HA_MODE_INTEGRATION, HA_MODE_OFF)

# Seconds to wait for the plugin to echo a new `ha_mode` back on `info`. The plugin
# answers within a publish burst; anything slower is a box that is not listening.
ACK_TIMEOUT: Final = 10

# Seconds to wait for the retained `info` topic when a box is added by hand. A box that
# is on the broker answers immediately, because the payload is already retained there.
PROBE_TIMEOUT: Final = 10

# Seconds to wait for a subscription to reach the broker before sending a command that
# is answered on it. Home Assistant batches SUBSCRIBE packets behind a 0.1 s debouncer,
# and the receiver answers in about the same time, so a command sent the instant the
# subscription exists in process races the subscription that is meant to hear it.
SUBSCRIBE_TIMEOUT: Final = 5

# Topic suffixes the integration reads in M1. The entity platforms (M3) add the rest.
TOPIC_AVAILABILITY: Final = "availability"
TOPIC_INFO: Final = "info"

PAYLOAD_ONLINE: Final = "online"
PAYLOAD_OFFLINE: Final = "offline"

# Manufacturer names keyed by the prefix of the box type enigma2 reports. Longest
# prefix wins. A box type nobody has mapped yet is still a perfectly good device, so
# the fallback is the platform rather than "unknown".
DEFAULT_MANUFACTURER: Final = "Enigma2"
MANUFACTURERS: Final[dict[str, str]] = {
    "vu": "Vu+",
    "dm": "Dream Multimedia",
    "gb": "GigaBlue",
    "et": "Xtrend",
    "xp": "Xtrend",
    "sf": "Octagon",
    "osmi": "Edision",
    "zgemma": "Zgemma",
    "h9": "Zgemma",
    "h11": "Zgemma",
    "ax": "AX Technology",
    "formuler": "Formuler",
}

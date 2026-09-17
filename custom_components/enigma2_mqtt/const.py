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

# Options. Everything here is a preference about what the integration shows or how it
# reaches the box, never a fact about the box: a fact belongs on a topic.
CONF_DANGEROUS_BUTTONS: Final = "dangerous_buttons"
CONF_WOL_MAC: Final = "wol_mac"
CONF_BOUQUETS: Final = "bouquets"
CONF_PUBLISH_KEYS: Final = "publish_keys"
CONF_RECEIVER_HOST: Final = "receiver_host"
CONF_SCREENSHOT: Final = "screenshot"
CONF_SCREENSHOT_INTERVAL: Final = "screenshot_interval"
CONF_SCREENSHOT_DELAY: Final = "screenshot_delay"
CONF_CAM_TELEMETRY: Final = "cam_telemetry"
CONF_SSH_HOST: Final = "ssh_host"
CONF_SSH_PORT: Final = "ssh_port"
CONF_SSH_USERNAME: Final = "ssh_username"
CONF_SSH_PASSWORD: Final = "ssh_password"
CONF_SSH_HOST_KEY: Final = "ssh_host_key"
CONF_KEEP_SSH_CREDENTIALS: Final = "keep_ssh_credentials"

SCREENSHOT_OFF: Final = "off"
SCREENSHOT_ON_ZAP: Final = "on_zap"
SCREENSHOT_INTERVAL: Final = "interval"
SCREENSHOT_MODES: Final = (SCREENSHOT_OFF, SCREENSHOT_ON_ZAP, SCREENSHOT_INTERVAL)
DEFAULT_PUBLISH_KEYS: Final = True
DEFAULT_SCREENSHOT: Final = SCREENSHOT_ON_ZAP
DEFAULT_SCREENSHOT_INTERVAL: Final = 60
DEFAULT_SCREENSHOT_DELAY: Final = 4
DEFAULT_CAM_TELEMETRY: Final = False
MIN_SCREENSHOT_INTERVAL: Final = 5
MAX_SCREENSHOT_INTERVAL: Final = 3600
MIN_SCREENSHOT_DELAY: Final = 1
MAX_SCREENSHOT_DELAY: Final = 30

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

# Seconds an action waits for the state topic that proves its command worked.
COMMAND_TIMEOUT: Final = 10

# Seconds an action waits for a complaint about a command that has no observable
# effect. `send_key` and `message` change no state topic, so the contract offers no
# positive acknowledgement for them: the plugin either says nothing or publishes
# `last_error`. Waiting the full command timeout on every key press would make the
# action unusable, and not waiting at all would swallow "unknown key name". A box on
# the same LAN answers in well under a tenth of this.
ERROR_GRACE: Final = 1.0

# State topic suffixes, relative to `<base_topic>/<node_id>/`.
TOPIC_AVAILABILITY: Final = "availability"
TOPIC_INFO: Final = "info"
TOPIC_POWER: Final = "power"
TOPIC_SERVICE: Final = "service"
TOPIC_EPG: Final = "epg"
TOPIC_TUNER: Final = "tuner"
TOPIC_RECORDING: Final = "recording"
TOPIC_TIMERS: Final = "timers"
TOPIC_VOLUME: Final = "volume"
TOPIC_HDD: Final = "hdd"
TOPIC_SCREEN: Final = "screen"
TOPIC_KEY: Final = "key"
TOPIC_LAST_ERROR: Final = "last_error"
TOPIC_CHANNELS: Final = "channels"
TOPIC_EPG_GRID: Final = "epg_grid"
TOPIC_CAM: Final = "cam"

PAYLOAD_ONLINE: Final = "online"
PAYLOAD_OFFLINE: Final = "offline"

POWER_ON: Final = "on"
POWER_STANDBY: Final = "standby"

# The bus event the key topic is republished as, so that an automation can react to a
# colour key through the device automation editor without the event entity existing.
EVENT_KEY: Final = f"{DOMAIN}_key"
ATTR_NODE_ID: Final = "node_id"
ATTR_KEY: Final = "key"
ATTR_PRESS: Final = "press"

# Every remote key name starts with this, in the topics and in the commands alike.
KEY_PREFIX: Final = "KEY_"

PRESS_SHORT: Final = "short"
PRESS_LONG: Final = "long"
PRESSES: Final = (PRESS_SHORT, PRESS_LONG)

# The colour keys, which are the ones an Enigma2 skin puts a labelled function on and
# therefore the ones worth offering as device triggers.
COLOUR_KEYS: Final[dict[str, str]] = {
    "red": "KEY_RED",
    "green": "KEY_GREEN",
    "yellow": "KEY_YELLOW",
    "blue": "KEY_BLUE",
}

# The key names an Enigma2 remote emits, which are the Linux input event names the
# driver reports rather than anything enigma2 invents. An event entity has to declare
# its event types up front, so this list is the declaration; a key outside it is logged
# and dropped rather than crashing the entity, and the bus event still carries it.
KEY_NAMES: Final[tuple[str, ...]] = (
    # Power and standby
    "KEY_POWER",
    "KEY_POWER2",
    "KEY_SLEEP",
    "KEY_WAKEUP",
    # Digits
    "KEY_0",
    "KEY_1",
    "KEY_2",
    "KEY_3",
    "KEY_4",
    "KEY_5",
    "KEY_6",
    "KEY_7",
    "KEY_8",
    "KEY_9",
    # Navigation
    "KEY_UP",
    "KEY_DOWN",
    "KEY_LEFT",
    "KEY_RIGHT",
    "KEY_OK",
    "KEY_EXIT",
    "KEY_BACK",
    "KEY_MENU",
    "KEY_HOME",
    "KEY_INFO",
    "KEY_EPG",
    "KEY_GUIDE",
    "KEY_TEXT",
    "KEY_HELP",
    "KEY_LIST",
    "KEY_FAVORITES",
    "KEY_TIMER",
    "KEY_SCREEN",
    # Colour keys
    "KEY_RED",
    "KEY_GREEN",
    "KEY_YELLOW",
    "KEY_BLUE",
    # Sound and channel
    "KEY_VOLUMEUP",
    "KEY_VOLUMEDOWN",
    "KEY_MUTE",
    "KEY_CHANNELUP",
    "KEY_CHANNELDOWN",
    "KEY_AUDIO",
    "KEY_SUBTITLE",
    # Transport
    "KEY_PLAY",
    "KEY_PAUSE",
    "KEY_PLAYPAUSE",
    "KEY_STOP",
    "KEY_RECORD",
    "KEY_REWIND",
    "KEY_FASTFORWARD",
    "KEY_PREVIOUS",
    "KEY_NEXT",
    "KEY_PREVIOUSSONG",
    "KEY_NEXTSONG",
    # Sources
    "KEY_TV",
    "KEY_TV2",
    "KEY_RADIO",
    "KEY_VIDEO",
    "KEY_PVR",
    "KEY_MEDIA",
)

# The message types the `cmd/message` popup understands.
MESSAGE_TYPES: Final = ("info", "warning", "error")
MESSAGE_MAX_LENGTH: Final = 500
MESSAGE_DEFAULT_TIMEOUT: Final = 10

# What `cmd/record` accepts.
RECORD_ACTIONS: Final = ("start", "stop")

# The plugin release this version of the integration is written against. The update
# entity compares it with `info.plugin`. M4 replaces the constant with the version of
# the IPK the integration bundles, and grows an install step to go with it.
SUPPORTED_PLUGIN_VERSION: Final = "0.1.0"
PLUGIN_RELEASES_URL: Final = (
    "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases"
)

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

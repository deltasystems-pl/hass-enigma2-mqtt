"""Constants for the Enigma2 MQTT integration."""

from __future__ import annotations

from datetime import timedelta
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
CONF_OSCAM_TELEMETRY: Final = "oscam_telemetry"
CONF_SSH_HOST: Final = "ssh_host"
CONF_SSH_PORT: Final = "ssh_port"
CONF_SSH_USERNAME: Final = "ssh_username"
CONF_SSH_PASSWORD: Final = "ssh_password"
CONF_SSH_HOST_KEY: Final = "ssh_host_key"
CONF_KEEP_SSH_CREDENTIALS: Final = "keep_ssh_credentials"
CONF_CHECK_GITHUB_RELEASES: Final = "check_github_releases"
CONF_SOURCE_LIST_SCOPE: Final = "source_list_scope"
CONF_SOFTCAM_AUTOHEAL: Final = "softcam_autoheal"
CONF_SOFTCAM_AUTOHEAL_SECONDS: Final = "softcam_autoheal_seconds"

# Read-only members of `info.settings`. They are settings a consumer may read, not ones
# `cmd/config` will accept: `deep_standby_allowed` gates a command, and a setting that
# enables a command can only be turned on by somebody standing in front of the
# television. Presence in `info.settings` therefore says nothing about writability.
#
# `softcam_restart_allowed` is the second of them and follows the same rule for the same
# reason. Note what is *not* here: `softcam_autoheal` and `softcam_autoheal_seconds` only
# tune a command that is already permitted, so they are ordinary remotely-writable
# settings and the options flow sends them like any other.
CONF_DEEP_STANDBY_ALLOWED: Final = "deep_standby_allowed"
CONF_SOFTCAM_RESTART_ALLOWED: Final = "softcam_restart_allowed"
# The third, for the on-demand EPG import (item i), under the same rule.
CONF_EPG_IMPORT_ALLOWED: Final = "epg_import_allowed"

SCREENSHOT_OFF: Final = "off"
SCREENSHOT_ON_ZAP: Final = "on_zap"
SCREENSHOT_INTERVAL: Final = "interval"
SCREENSHOT_MODES: Final = (SCREENSHOT_OFF, SCREENSHOT_ON_ZAP, SCREENSHOT_INTERVAL)
DEFAULT_PUBLISH_KEYS: Final = True
DEFAULT_SCREENSHOT: Final = SCREENSHOT_ON_ZAP
DEFAULT_SCREENSHOT_INTERVAL: Final = 60
DEFAULT_SCREENSHOT_DELAY: Final = 4
DEFAULT_CAM_TELEMETRY: Final = False
DEFAULT_OSCAM_TELEMETRY: Final = False
DEFAULT_CHECK_GITHUB_RELEASES: Final = False
MIN_SCREENSHOT_INTERVAL: Final = 5
MAX_SCREENSHOT_INTERVAL: Final = 3600
MIN_SCREENSHOT_DELAY: Final = 1
MAX_SCREENSHOT_DELAY: Final = 30

# What the options form will accept for the auto-heal window, mirroring the range the
# plugin's own setting declares. The plugin is the side that enforces it; this only keeps
# a number the box would refuse from being sent at all.
MIN_SOFTCAM_AUTOHEAL_SECONDS: Final = 30
MAX_SOFTCAM_AUTOHEAL_SECONDS: Final = 600
# Shown on the form only when the box reports a window that is not a number in that
# range — which is a plugin that is misbehaving, not a value to be preserved. A box that
# reports a usable window is its own default, so this is never the suggested value on a
# receiver that is working. 🔴 It is deliberately *not* in `PLUGIN_SETTING_DEFAULTS`:
# that table is checked against the bundled plugin's own source, and the bundle here is
# 0.2.0, which has no such setting.
DEFAULT_SOFTCAM_AUTOHEAL_SECONDS: Final = 90

# The bounds the retained `softcam` payload is believed inside. Nothing here is a limit
# the receiver enforces; they are the difference between a number and a value that has
# arrived from somewhere over a broker.
#
# `running_instances` is deliberately generous. The runaway this field exists to make
# visible adds one copy every six minutes for as long as nobody looks, so a tight ceiling
# would blank the reading exactly when it had something to say; the ceiling is only here
# so that a nonsense payload cannot be graphed.
MAX_SOFTCAM_INSTANCES: Final = 10_000
# The most restarts a day's counter is believed to hold. Same reasoning, same generosity.
MAX_SOFTCAM_RESTARTS_TODAY: Final = 10_000
# The image's own periodic check interval, in minutes. A day is already absurd for it.
MAX_SOFTCAM_MANAGER_MINUTES: Final = 1440
# The furthest into the future `last_restart` is read as a clock rather than as a
# payload: 2100-01-01T00:00:00Z. Zero is rejected too — it is a field nobody filled in.
MAX_SOFTCAM_EPOCH: Final = 4_102_444_800
# The longest binary name that can be a sensor state at all: Home Assistant drops a state
# over 255 characters rather than cutting it, so a longer one is not a name to show.
SOFTCAM_NAME_MAX: Final = 255
# What the plugin says caused the last restart. Anything else is not part of this
# contract and is read as "it did not say".
SOFTCAM_RESTART_REASONS: Final = ("manual", "autoheal")

# What the media player's `source_list` offers. Every chosen bouquet is the default,
# because it is what this integration has always done and narrowing it under somebody
# who has automations naming a channel would break them. `active_bouquet` is the opt-in:
# a receiver with a thousand channels makes one dropdown of a thousand rows, and the
# bouquet the receiver is on is both short and the list its own channel ± walks.
#
# It is a preference about the length of a list and nothing more. It is *not* about the
# recorder: Home Assistant lists `source_list` in the media player's
# `_entity_component_unrecorded_attributes`, and the recorder strips those before it
# measures a state against its 16 384-byte limit, so the long list never reached the
# database in the first place.
SOURCE_LIST_SCOPE_ACTIVE_BOUQUET: Final = "active_bouquet"
SOURCE_LIST_SCOPE_ALL: Final = "all"
SOURCE_LIST_SCOPES: Final = (SOURCE_LIST_SCOPE_ACTIVE_BOUQUET, SOURCE_LIST_SCOPE_ALL)
DEFAULT_SOURCE_LIST_SCOPE: Final = SOURCE_LIST_SCOPE_ALL

# How many colon-separated fields identify a service. Two spellings of one channel
# differ in what follows them — a trailing colon, a stream URL, a name — so a comparison
# that is not made over exactly these fields will call the same channel two channels.
# 🔴 This mirrors `SERVICE_FIELDS` in the receiver plugin's `enigma2.identity()`. The two
# halves have to agree about what "the same service" means; a cleverer rule here than
# there would be worse than either rule on its own.
SERVICE_FIELDS: Final = 11

# How the plugin presents a box to Home Assistant, switchable with `cmd/ha_mode`.
HA_MODE_DISCOVERY: Final = "discovery"
HA_MODE_INTEGRATION: Final = "integration"
HA_MODE_OFF: Final = "off"
HA_MODES: Final = (HA_MODE_DISCOVERY, HA_MODE_INTEGRATION, HA_MODE_OFF)

# What the receiver plugin declares as each setting's default, mirroring the
# `ConfigText`/`ConfigSelection`/… declarations in its own `src/MQTTBridge/config.py`.
#
# 🔴 Enigma2 does not write out a setting whose value still equals its default, so a
# receiver that has never been moved off the default base topic has no `base_topic`
# line in `/etc/enigma2/settings` at all — and nor has one on the default `port`, or
# `enabled`, or any other untouched setting. A box measured after a working install had
# exactly ten of these lines out of twenty-six settings. Anything here that reads a
# stored plugin setting must therefore read an absent key as the value in this table;
# reading absence as "some other value" would refuse a reinstall over a plugin that is
# working perfectly. The receiver-side helper used to apply these itself, which put a
# copy of them on the far side of the link where nothing compared it with the plugin,
# and made "the settings file says nothing" indistinguishable from "it says `enigma2`"
# for everything downstream — including the message a refusal puts on the screen.
#
# One table, because the alternative is a default written down beside each comparison
# and one of them drifting. `tests/test_plugin_setting_defaults.py` reads the bundled
# plugin's own source and checks every entry against it, so this is a mirror that is
# verified rather than a comment that claims to be one.
PLUGIN_SETTING_DEFAULTS: Final[dict[str, object]] = {
    "enabled": True,
    "host": "",
    "port": 1883,
    "tls": False,
    "ca_file": "",
    "username": "",
    "password": "",
    "node_id": "",
    "friendly_name": "",
    "base_topic": DEFAULT_BASE_TOPIC,
    "ha_discovery_prefix": "homeassistant",
    "ha_mode": HA_MODE_DISCOVERY,
    "publish_keys": DEFAULT_PUBLISH_KEYS,
    "screenshot": DEFAULT_SCREENSHOT,
    "screenshot_interval": DEFAULT_SCREENSHOT_INTERVAL,
    "screenshot_delay": DEFAULT_SCREENSHOT_DELAY,
    "cam_telemetry": DEFAULT_CAM_TELEMETRY,
    "oscam_telemetry": DEFAULT_OSCAM_TELEMETRY,
    "oscam_port": 8888,
    "oscam_username": "",
    "oscam_password": "",
    "oscam_identity_salt": "",
    "bouquets_for_select": "",
    "deep_standby_allowed": False,
    "log_level": "info",
    "epg_grid_events": 4,
}

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

# The most of a complaint the „Ostatni błąd" sensor keeps, and it bounds both halves:
# the command name it shows as its state and the receiver's text in the attributes. A
# Home Assistant state over 255 characters is dropped entirely rather than cut, and the
# command name arrives from the box like everything else here, so neither is trusted to
# be short. A cut error is still readable; an entity that silently refused to take one
# would not be.
ERROR_TEXT_MAX: Final = 255

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
TOPIC_BOUQUET: Final = "bouquet"
TOPIC_OSCAM: Final = "oscam"
TOPIC_PROCESS: Final = "process"

# The capability the plugin announces when it publishes `process`. A box whose image
# did not let it hook the measurement never names it, and the entities are never built.
CAPABILITY_PROCESS: Final = "process"

TOPIC_SOFTCAM: Final = "softcam"

# The capability behind the softcam topic and its diagnostic sensor. The plugin claims it
# only where restarting the selected cam is actually possible — the binary resolves under
# `/usr/softcams/`, its family has a start line, and the image starts it through the
# manager's poller rather than through `/etc/init.d/softcam`.
CAPABILITY_SOFTCAM: Final = "softcam"

# The two capabilities the „EPG – aktywny bukiet" sensor needs: the per-bouquet grids it
# reads, and the channel-list context that says which of them is the one in use.
CAPABILITY_EPG_GRID: Final = "epg_grid"
CAPABILITY_BOUQUET_CONTEXT: Final = "bouquet_context"

# The on-demand EPG import. The plugin claims the capability only where it found the
# image's own EPG-Importer already loaded and the EPG cache can import in place; the
# topic says what the importer is doing, whoever started it.
TOPIC_EPG_IMPORT: Final = "epg_import"
CAPABILITY_EPG_IMPORT: Final = "epg_import"
# The four states the topic may report. Anything else is not part of the contract and
# is read as "it did not say".
EPG_IMPORT_STATES: Final = ("idle", "running", "done", "failed")
# The most events one import is believed to report. A real run here is about 120 000;
# the ceiling only keeps a nonsense payload off a graph.
MAX_EPG_IMPORT_EVENTS: Final = 100_000_000
# The furthest ahead a `started` or `finished` is read as a clock: 2100-01-01T00:00:00Z.
MAX_EPG_IMPORT_EPOCH: Final = 4_102_444_800

# How much of a programme title the active-bouquet sensor carries. A few titles run to a
# whole sentence of episode blurb, and the sensor holds one per channel twice over; eighty
# characters is a full title on any card that will show it.
EPG_TITLE_MAX: Final = 80

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

# The two ways `cmd/message` can show a text. The popup is what the command has always
# done: a dialog that takes focus and waits in the image's notification queue. The toast
# is a small overlay in a corner that takes no key and hides itself, and the plugin only
# offers it where the screen for it was actually built — which it says by naming the
# `toast` capability. The popup is the default because a payload without `style` has to
# mean exactly what it meant before the field existed.
MESSAGE_STYLE_POPUP: Final = "popup"
MESSAGE_STYLE_TOAST: Final = "toast"
MESSAGE_STYLES: Final = (MESSAGE_STYLE_POPUP, MESSAGE_STYLE_TOAST)
CAPABILITY_TOAST: Final = "toast"

# A toast is a glance, not a letter: the plugin cuts its text at 200 characters, hides it
# after five seconds unless told otherwise, and will not keep one up for longer than
# thirty — nor show one "until dismissed", since nothing on it can be dismissed.
TOAST_MAX_LENGTH: Final = 200
TOAST_DEFAULT_TIMEOUT: Final = 5
TOAST_MIN_TIMEOUT: Final = 1
TOAST_MAX_TIMEOUT: Final = 30

# What `cmd/record` accepts.
RECORD_ACTIONS: Final = ("start", "stop")

# The plugin release this version of the integration is written against. The update
# entity compares it with `info.plugin`. M4 replaces the constant with the version of
# the IPK the integration bundles, and grows an install step to go with it.
SUPPORTED_PLUGIN_VERSION: Final = "0.2.0"
PLUGIN_RELEASES_URL: Final = (
    "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases"
)

# The one request this integration can make to anything that is not the user's own
# broker, and it is off unless somebody turns it on. It informs; it never downloads, and
# it can never raise `latest_version` above the bundle the installer is able to install.
PLUGIN_LATEST_RELEASE_URL: Final = (
    "https://api.github.com/repos/deltasystems-pl/enigma2-mqtt-bridge/releases/latest"
)
# GitHub's unauthenticated rate limit is sixty requests an hour per address, shared by
# everything else on that address. One request a day per receiver keeps this invisible
# inside it, and a plugin release is not news that goes stale in an afternoon.
#
# "A day" is measured against a stamp in Home Assistant's own storage, not against the
# life of an entity. A reload, an options save and a restart each build a new entity,
# and a limit that any of those resets is not a limit — six reloads were six requests.
# Between checks the entity shows the stored answer, so the tag survives a restart
# without anybody being asked for it again.
RELEASE_CHECK_INTERVAL: Final = timedelta(hours=24)
RELEASE_CHECK_TIMEOUT: Final = 10
RELEASE_CHECK_STORAGE_KEY: Final = f"{DOMAIN}.release_check"
RELEASE_CHECK_STORAGE_VERSION: Final = 1

# The most of an answer this will read. A GitHub release document is a few kilobytes;
# anything past this is not the endpoint that was asked for, and pulling it into memory
# to discover that would be the bug rather than the check.
RELEASE_BODY_LIMIT: Final = 64 * 1024
# The longest tag it will believe. A version is a handful of characters, and everything
# here is somebody else's text arriving on a device page.
RELEASE_TAG_MAX: Final = 64

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

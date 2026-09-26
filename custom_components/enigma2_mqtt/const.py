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
# Bouquets whose channels "Ostatnio oglądane" leaves out, stored as names like
# `bouquets`. It filters that one select and nothing else: "Ostatnio oglądane
# (wszystkie)", "Kanał", "Bukiet" and the media player are untouched by it, and the
# receiver still publishes every entry on the broker.
CONF_HISTORY_HIDDEN_BOUQUETS: Final = "history_hidden_bouquets"
# The tick box on the options flow's removal step. A form field, never stored.
CONF_CONFIRM_UNINSTALL: Final = "confirm_uninstall"

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
# The fourth, for removing the plugin from the receiver (ADR-0004). Same rule, with the
# opposite reading of silence: only a stated `true` offers anything, because the one act
# this permission opens cannot be undone from here.
CONF_UNINSTALL_ALLOWED: Final = "uninstall_allowed"

# `info.wol`: `{supported, armed, iface, mechanism}`, what the receiver's image can do
# about Wake-on-LAN from deep standby (plugin 0.3.0 onward). Only `supported` is read
# here. Where it is `false`, the two buttons it concerns carry `ATTR_WAKE_ON_LAN`, whose
# one value is translated per button into what the household can do instead.
INFO_WOL: Final = "wol"
ATTR_WAKE_ON_LAN: Final = "wake_on_lan"
WAKE_ON_LAN_NOT_SUPPORTED: Final = "not_supported"

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
# range - which is a plugin that is misbehaving, not a value to be preserved. A box that
# reports a usable window is its own default, so this is never the suggested value on a
# receiver that is working. It is also the plugin's own default, and so the value
# `PLUGIN_SETTING_DEFAULTS` holds for the setting, which CI checks against the bundled
# plugin's source.
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
# payload: 2100-01-01T00:00:00Z. Zero is rejected too - it is a field nobody filled in.
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
# differ in what follows them - a trailing colon, a stream URL, a name - so a comparison
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
# `ConfigText`/`ConfigSelection`/... declarations in its own `src/MQTTBridge/config.py`.
#
# 🔴 Enigma2 does not write out a setting whose value still equals its default, so a
# receiver that has never been moved off the default base topic has no `base_topic`
# line in `/etc/enigma2/settings` at all - and nor has one on the default `port`, or
# `enabled`, or any other untouched setting. A box measured after a working install had
# exactly ten of these lines out of twenty-six settings. Anything here that reads a
# stored plugin setting must therefore read an absent key as the value in this table;
# reading absence as "some other value" would refuse a reinstall over a plugin that is
# working perfectly. The receiver-side helper used to apply these itself, which put a
# copy of them on the far side of the link where nothing compared it with the plugin,
# and made "the settings file says nothing" indistinguishable from "it says `enigma2`"
# for everything downstream - including the message a refusal puts on the screen.
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
    "osd_toast": True,
    "cam_telemetry": DEFAULT_CAM_TELEMETRY,
    "oscam_telemetry": DEFAULT_OSCAM_TELEMETRY,
    "oscam_port": 8888,
    "oscam_username": "",
    "oscam_password": "",
    "oscam_identity_salt": "",
    "bouquets_for_select": "",
    "deep_standby_allowed": False,
    "wol_arm": False,
    "cec_standby_workaround": False,
    "softcam_restart_allowed": False,
    "softcam_autoheal": False,
    "softcam_autoheal_seconds": DEFAULT_SOFTCAM_AUTOHEAL_SECONDS,
    "epg_import_allowed": False,
    "uninstall_allowed": False,
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
# only where restarting the selected cam is actually possible - the binary resolves under
# `/usr/softcams/`, its family has a start line, and the image starts it through the
# manager's poller rather than through `/etc/init.d/softcam`.
CAPABILITY_SOFTCAM: Final = "softcam"

# The two capabilities the `epg_active_bouquet` sensor needs: the per-bouquet grids it
# reads, and the channel-list context that says which of them is the one in use.
CAPABILITY_EPG_GRID: Final = "epg_grid"
CAPABILITY_BOUQUET_CONTEXT: Final = "bouquet_context"

# The on-demand EPG import. The plugin claims the capability only where it found the
# image's own EPG-Importer already loaded and the EPG cache can import in place; the
# topic says what the importer is doing, whoever started it.
TOPIC_EPG_IMPORT: Final = "epg_import"
CAPABILITY_EPG_IMPORT: Final = "epg_import"
# Removing the plugin from the receiver. The plugin claims the capability only where the
# package manager installed it - an executable opkg, the package's own control file, and a
# file list naming the `plugin.py` that is running - so a plugin unpacked by hand or baked
# into an image is never offered a removal it cannot perform.
CAPABILITY_UNINSTALL: Final = "uninstall"
# Seconds the options flow waits, after `cmd/uninstall`, for the receiver to retract its
# `info` and its announcement and then say `offline`. The plugin's own bound on the
# broker's acknowledgements is 15 s, so a box that has not finished by this is a box
# that has not acted.
UNINSTALL_TIMEOUT: Final = 60

# The receiver's own zap history - the list its "History Zap" screen shows on NEXT and
# PREVIOUS - newest first. Two capabilities, because an image can offer one without the
# other: `zap_history` is the topic and `cmd/zap_history`, `history_clear` is
# `cmd/history_clear`, which runs what the remote's 0 key runs.
TOPIC_ZAP_HISTORY: Final = "zap_history"
CAPABILITY_ZAP_HISTORY: Final = "zap_history"
CAPABILITY_HISTORY_CLEAR: Final = "history_clear"
# The reasons the receiver gives, in `last_error.reason`, for refusing to clear its
# history - every case in which the 0 key would not clear it either. Each one has its own
# translation, `history_clear_<reason>`, so the refusal is read in the household's
# language; a reason not listed here falls back to the receiver's English sentence.
HISTORY_CLEAR_REASONS: Final = (
    "standby",
    "panic_off",
    "too_short",
    "timeshift",
    "zap_blocked",
    "pip",
    "playback",
    "not_cleared",
    "screen_open",
)
# The one reason code `cmd/zap_history` refuses with; its other refusals (the channel
# left the history, a malformed payload) carry no code and are shown in the receiver's
# own words. Translated as `zap_history_<reason>`.
ZAP_HISTORY_REASONS: Final = ("playback",)

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
# offers it where the screen for it was actually built - which it says by naming the
# `toast` capability. The popup is the default because a payload without `style` has to
# mean exactly what it meant before the field existed.
MESSAGE_STYLE_POPUP: Final = "popup"
MESSAGE_STYLE_TOAST: Final = "toast"
MESSAGE_STYLES: Final = (MESSAGE_STYLE_POPUP, MESSAGE_STYLE_TOAST)
CAPABILITY_TOAST: Final = "toast"

# A toast is a glance, not a letter: the plugin cuts its text at 200 characters, hides it
# after five seconds unless told otherwise, and will not keep one up for longer than
# thirty - nor show one "until dismissed", since nothing on it can be dismissed.
TOAST_MAX_LENGTH: Final = 200
TOAST_DEFAULT_TIMEOUT: Final = 5
TOAST_MIN_TIMEOUT: Final = 1
TOAST_MAX_TIMEOUT: Final = 30

# What `cmd/record` accepts.
RECORD_ACTIONS: Final = ("start", "stop")

# The plugin release this version of the integration is written against: the version
# the update entity shows as the one to install when no bundle loads. With a bundle, the
# bundle's own version decides.
SUPPORTED_PLUGIN_VERSION: Final = "0.3.0"
PLUGIN_RELEASES_URL: Final = (
    "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases"
)

# ---------------------------------------------------------------------------------------
# Which plugin releases this integration may offer (ADR-0008 section 2). The rule is shared with
# the plugin, which applies it to the same signed index: the same contract major, at or
# above the higher of this floor and the index's, this integration at or above a
# release's `min_integration`, and not withdrawn. The manifest cannot carry these -
# hassfest refuses keys it does not know - so they live here, and the integration
# publishes them for the receiver on `enigma2mqtt/integration/<node_id>`.
#
# Contract 0 is 0.1.0 alone, which is also why the floor is 0.2.0: nothing in this
# project offers a plugin below the contract this integration speaks.
PLUGIN_CONTRACT: Final = 1
PLUGIN_MIN_VERSION: Final = "0.2.0"
# The named in-major exceptions of contract 1 that this integration has been checked
# against (the plugin's TOPICS.md, "Named in-major exceptions", and `contract.json`). A
# change of meaning stays inside a major only as one of these, decided in the plugin's
# pull request that makes it after checking it against this integration's code; CI
# compares this list with the plugin's `contract.json` at the pinned commit, so a new one
# cannot arrive without somebody reading it here.
PLUGIN_CONTRACT_EXCEPTIONS: Final = (
    "zap-moves-channel-list",
    "epg-grid-generated-means-changed",
    "timers-lists-finished",
    "zap-under-popup-recorded",
)

# The plugin's signed release index: a fixed origin, not a setting. Fetched with verified
# TLS and no redirects, and believed only when an embedded key signed it.
PLUGIN_INDEX_ORIGIN: Final = "https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/"
PLUGIN_INDEX_FILE: Final = "releases.json"
PLUGIN_INDEX_SIGNATURE_FILE: Final = "releases.json.sig"
# The keys the index is signed with, as the plugin embeds them (its `trust.py`): the main
# key signs every index in the plugin repository's CI after the maintainer approves the
# job; the spare, of higher rank, is sealed offline and signs nothing unless the main key
# is leaked or lost - and one index it signs silences the main key for good. A `key_id` is
# the first 16 hex digits of sha256 over the raw public key; `baseline` is the serial
# published when this set was embedded, from which a key seen for the first time may be at
# most 1000 ahead. A test pins every value, and compares them with the shared vectors.
PLUGIN_INDEX_KEYS: Final[tuple[dict[str, object], ...]] = (
    {
        "key_id": "5de3b24c97e88660",
        "rank": 1,
        "public": "39Ndn8vAkeWAhYIYWvNubezKuF5F/5aY5E1uo+hSnqQ=",
        "baseline": 0,
    },
    {
        "key_id": "c72fd83e3e514a25",
        "rank": 2,
        "public": "1F2ajhsDoTuqAGdV2QOHdRl8hV4B0kNE0xwVGrpHdfs=",
        "baseline": 0,
    },
)

# The check makes no request unless somebody asked for one: the daily check is opt-in, on
# the existing option, and a press of „Sprawdź aktualizacje wtyczki" or Home Assistant's
# own "Check for updates" is a request for one. One verified index serves every receiver.
#
# "A day" and "ten minutes" are measured against stamps in Home Assistant's storage, not
# against the life of an entity: a reload, an options save and a restart each build new
# entities, and a limit that any of those resets is not a limit. Ten minutes is the time
# GitHub Pages caches the file for anyway (`max-age=600`), so a second look inside it
# could only ever see the same bytes.
RELEASE_CHECK_INTERVAL: Final = timedelta(hours=24)
RELEASE_MANUAL_INTERVAL: Final = timedelta(minutes=10)
RELEASE_CHECK_TIMEOUT: Final = 10
# The second layout of the release check's storage. The first lived under
# `enigma2_mqtt.release_check`, one record per receiver; it is removed the first time
# this one loads.
RELEASE_INDEX_STORAGE_KEY: Final = f"{DOMAIN}.release_index"
RELEASE_INDEX_STORAGE_VERSION: Final = 2
LEGACY_RELEASE_CHECK_STORAGE_KEY: Final = f"{DOMAIN}.release_check"
# Sent after every newly accepted index and whenever a check ends, so every receiver's
# entities read the cache again.
SIGNAL_RELEASE_INDEX: Final = f"{DOMAIN}_release_index"

# The two retained topics this integration publishes for the receiver, outside any
# box's base topic because they are about this integration rather than about a box.
# `integration/<node_id>` says which integration manages that receiver and which plugin
# releases it can work with; it is retracted when the entry is removed. `release_index`
# carries the newest index this integration accepted, so a receiver with no internet of
# its own can verify and use it - it is signed, so it is harmless to anybody who reads it,
# and it is never retracted.
TOPIC_INTEGRATION_PREFIX: Final = "enigma2mqtt/integration"
TOPIC_RELEASE_INDEX: Final = "enigma2mqtt/release_index"

# The plugin version the household chose in „Wersja wtyczki do instalacji", stored in the
# entry's options so it survives a restart. Absent means the select's first option,
# `latest`: the newest compatible release this card can install.
CONF_PLUGIN_TARGET_VERSION: Final = "plugin_target_version"
TARGET_LATEST: Final = "latest"
SIGNAL_TARGET_VERSION: Final = f"{DOMAIN}_target_version"

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

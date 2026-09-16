# Enigma2 MQTT — documentation

The full guide to the Home Assistant side of **Enigma2 MQTT Bridge**. The box side — the
plugin, its setup screen and the topic contract — is documented in the
[enigma2-mqtt-bridge](https://github.com/deltasystems-pl/enigma2-mqtt-bridge) repository.

This document grows with the integration. Sections that describe something not written yet
say so; nothing here is speculation about behaviour that has never run.

---

## 1. Concepts

**One receiver is one device.** Everything the integration creates hangs off a single device
identified by the plugin's `node_id` — `<boxtype>_<mac6>`, for example `vuuno4kse_005301`,
stable across reinstalls of the plugin and of Home Assistant.

**The broker is the only channel.** The plugin publishes state under
`<base_topic>/<node_id>/` (default base topic `enigma2`) and subscribes to
`<base_topic>/<node_id>/cmd/#`. The integration never talks to OpenWebif; the only other
connection it ever opens is SSH, and only when you ask it to install or update the plugin.

**Push, not poll.** State topics are retained, so Home Assistant has the full picture the
moment it subscribes, and every later change arrives as the enigma2 event that caused it.
Availability is the MQTT last will: within about 45 seconds of a box disappearing its
entities go `unavailable`, and they come back with a full snapshot when it reconnects.

**Two modes.** `ha_mode = discovery` lets the core MQTT integration build entities from the
plugin's discovery payloads — useful without this integration at all. `ha_mode = integration`
hands that job to this integration, which can express what discovery cannot. Adding a box
here switches it and makes the plugin retract its discovery payloads first, so nothing is
created twice.

## 2. Installation

See the [README](README.md#installation) for HACS and manual installation, and
[§3 Configuration](#3-configuration) for what the flow asks. The plugin must be on the box;
from M4 the integration can put it there over SSH.

## 3. Configuration

The config flow has two entries today and a third in M4.

**Discovered.** The plugin publishes a retained announcement on
`enigma2mqtt/discovery/<node_id>/config`, and the integration's manifest subscribes to that
prefix, so a configured box appears by itself in *Settings → Devices & Services*. The card
names the box, its type and its image. Confirming publishes `cmd/ha_mode = integration` and
waits up to ten seconds for the plugin to echo the new mode back on `info`; only then is the
entry created. A box that does not answer leaves the form up with an error and the confirm
can simply be pressed again — nothing has been changed on the Home Assistant side.

**Manual.** *Add integration → Enigma2 MQTT* asks for the **base topic** (the plugin's
default is `enigma2`), the **node ID** from the plugin's setup screen, for example
`vuuno4kse_005301`, and an optional **name**. The flow subscribes to
`<base topic>/<node ID>/info` and waits up to ten seconds; because that topic is retained, a
box that is on the broker answers at once and one that is not reports "no receiver was found
on this topic". It then switches the mode exactly as the discovered path does. This entry is
for a box behind an MQTT bridge that rewrites the topic prefix, or for one whose announcement
never arrived.

**Install the plugin from here.** M4.

The node id is the entry's unique id, so the same box cannot be added twice by either path,
and a box that renames itself updates the entry it already owns.

**Options and reconfigure arrive in M3** — screenshot policy, key events, deep-standby button
visibility, the Wake-on-LAN MAC and which bouquets feed the channel list. Until then the two
fields above are the whole configuration; to change one, remove the box and add it again.

## 4. Entities

*Arrives in M3.* Unique ids follow one scheme: `<node_id>_<key>`, where the key is the
English translation key of the entity. Entity ids derive from the same key, so they are
stable in every language; only the display name changes.

### 4.1 Media player

| Key | Polish name | unique_id |
|---|---|---|
| (the device itself) | *Dekoder salon* | `<node_id>_media_player` |

### 4.2 Remote, notify, event, image

| Key | Polish name | unique_id |
|---|---|---|
| `remote` | *Pilot* | `<node_id>_remote` |
| `osd` | *Ekran OSD* | `<node_id>_osd` |
| `remote_key` | *Pilot – klawisz* | `<node_id>_remote_key` |
| `screen` | *Ekran* | `<node_id>_screen` |

### 4.3 Sensors

| Key | Polish name | unique_id |
|---|---|---|
| `channel` | *Kanał* | `<node_id>_channel` |
| `program` | *Program* | `<node_id>_program` |
| `next_program` | *Następny program* | `<node_id>_next_program` |
| `active_recordings` | *Aktywne nagrania* | `<node_id>_active_recordings` |
| `next_timer` | *Następny timer* | `<node_id>_next_timer` |
| `snr` | *SNR* | `<node_id>_snr` |
| `agc` | *AGC* | `<node_id>_agc` |
| `ber` | *BER* | `<node_id>_ber` |
| `uptime` | *Czas pracy* | `<node_id>_uptime` |

### 4.4 Binary sensors, switches, number

| Key | Polish name | unique_id |
|---|---|---|
| `recording` | *Nagrywanie* | `<node_id>_recording` |
| `hdd` | *Dysk nagrań* | `<node_id>_hdd` |
| `power` | *Zasilanie* | `<node_id>_power` |
| `mute` | *Wyciszenie* | `<node_id>_mute` |
| `volume` | *Głośność* | `<node_id>_volume` |

### 4.5 Buttons and update

| Key | Polish name | unique_id |
|---|---|---|
| `deep_standby` | *Głębokie uśpienie* | `<node_id>_deep_standby` |
| `restart_gui` | *Restart GUI* | `<node_id>_restart_gui` |
| `reboot` | *Restart* | `<node_id>_reboot` |
| `wake_on_lan` | *Obudź (WoL)* | `<node_id>_wake_on_lan` |
| `screenshot` | *Zrzut ekranu* | `<node_id>_screenshot` |
| `refresh_discovery` | *Odśwież discovery* | `<node_id>_refresh_discovery` |
| `plugin_update` | *Wtyczka MQTT Bridge* | `<node_id>_plugin_update` |

## 5. Actions

*Arrives in M3.* Every action takes a device or an entity as its target, and every one is
answered by the plugin on the matching state topic — a failure raises `HomeAssistantError`
carrying what the box reported on `last_error`.

| Action | Fields |
|---|---|
| `enigma2_mqtt.zap` | `sref` **or** `name` (a channel name, refused when it is not unique) |
| `enigma2_mqtt.send_key` | `key` (a `KEY_*` name), `long` (boolean) |
| `enigma2_mqtt.message` | `text` (≤ 500 characters), `type`, `timeout` |
| `enigma2_mqtt.add_timer` | `sref` + `event_id`, **or** `sref` + `begin` + `end` + `name` |
| `enigma2_mqtt.delete_timer` | `sref`, `begin`, `end` |
| `enigma2_mqtt.record` | `action`: `start` or `stop` |
| `enigma2_mqtt.screenshot` | — |
| `enigma2_mqtt.set_ha_mode` | `mode`: `discovery`, `integration` or `off` |
| `enigma2_mqtt.get_epg_grid` | `bouquet` (M3; **returns a response**, see below) |

`get_epg_grid` reads the plugin's retained `epg_grid/<bouquet_slug>` topic and returns the
grid as the action's response. It is deliberately not a state attribute: a grid runs to tens
of kilobytes per bouquet, and an attribute of that size is written to the recorder on every
update.

## 6. Device triggers

*Arrives in M3.* The colour keys — red, green, yellow, blue — each as a short and a long
press, offered in the automation editor as device triggers on the receiver. They are built on
the `event` entity, which carries every key the box reports, so an automation on a key without
a device trigger is written against that entity instead.

## 7. Diagnostics

The device page offers a diagnostics download: the config entry, the box's announcement, its
last `info` payload, the availability flag, the capability list and the device as the registry
holds it. It is meant to be attached to an issue, so the MAC address, the IP address and the
configuration URL built from it are redacted, and so are the broker and SSH credential keys —
those are named in the redaction list before the installer of M4 can create one, rather than
after. M3 adds the retained state of the remaining topics.

## 8. Troubleshooting

*Filled in as failure modes are found on real boxes, from M2 on.* The first three questions
are already clear:

- **Nothing is discovered.** Check that the MQTT integration is connected, then subscribe to
  `enigma2mqtt/discovery/#` with an MQTT client. No retained message there means the plugin is
  not running, not configured, or pointed at a different broker.
- **The device is `unavailable`.** `<base_topic>/<node_id>/availability` should read `online`.
  A retained `offline` that never clears is the plugin's last will; the box lost the broker.
- **Entities appear twice.** The box is in `discovery` mode *and* added here. Re-run the setup
  or send `cmd/ha_mode = integration`; the plugin retracts the discovery payloads.

## 9. FAQ

**Can I use the plugin without this integration?** Yes. That is what `ha_mode = discovery` is
for, and the plugin's own documentation covers it, including a `universal` media player recipe.

**Does this replace OpenWebif?** No. OpenWebif stays what it is; the plugin is a bridge, and
full EPG browsing and streaming remain OpenWebif's job. A compact EPG grid over MQTT is part
of v1 as an action that returns a response — see [ADR-0001](docs/adr/0001-m0-decisions.md).

**Does it work with the core `enigma2` integration?** They can coexist, but both would talk to
the same box; the point of this one is to stop polling, so remove the other when you switch.

**Which images are supported?** OpenViX 6.x and OpenATV 7.x are the supported set; OpenPLi 9
and OpenBH 6 are best effort pending testers. VTi (Python 2) is out of scope.

**Is any of this in the HACS default store?** Not yet — add the repository as a custom
repository. The default-store request follows `v1.0.0`.

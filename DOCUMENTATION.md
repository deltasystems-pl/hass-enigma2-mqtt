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

### Options

*Settings → Devices & Services → Enigma2 MQTT → Configure.* Three preferences, all about this
end of the link rather than about the box; saving them reloads the entry, so a change takes
effect without a restart.

| Option | Default | What it does |
|---|---|---|
| **Show the deep standby and reboot buttons** | off | Creates the „Głębokie uśpienie" and „Restart" buttons. Turning it off again removes them from the entity registry rather than leaving them behind unavailable. This is about what appears on a dashboard, **not** a safety mechanism: the plugin refuses both commands while a recording is running whatever is set here. |
| **Wake-on-LAN MAC address** | the address the box reports on `info` | The target of the magic packet the „Obudź (WoL)" button and `media_player.turn_on` send. Set it when the receiver reports a different interface from the one that is plugged in — a box on Wi-Fi does not answer a packet sent to its cable port. |
| **Bouquets to offer** | every bouquet the box publishes | Which bouquets feed the media player's channel list and the media browser. The choices are the bouquets on the `channels` topic, and a name can be typed for one the box has not published yet. This narrows the plugin's own `bouquets_for_select`; it cannot widen it. |

The screenshot policy and key publishing are settings **on the box**, not here: they decide
what is published at all, so they belong where the publishing happens.

### Reconfigure

*…→ Enigma2 MQTT → the receiver → Reconfigure.* This is for following a box whose **node ID**
or **base topic** was changed on the plugin's own setup screen. The flow checks that a box
really answers on the new topic before it saves anything.

🔴 **A changed node ID is a changed identity.** Every unique id is `<node_id>_<key>`, so a box
with a new node ID is, as far as Home Assistant can tell, a different device doing the same
job. The old device is removed — with its entities and their history — and the reload builds
the box again under its new name. Changing only the base topic or the display name keeps
everything.

## 4. Entities

Twenty-six entities on one device. Unique ids follow one scheme: `<node_id>_<key>`, where the
key is the English translation key of the entity. Entity ids derive from the same key, so
`sensor.dekoder_salon_channel` is `sensor.dekoder_salon_channel` in every language and only
the display name changes.

Two rules run through the table. **An entity is unavailable when the box is offline, and also
when the topic it reads has never arrived** — an image that gave the plugin no tuner hook
publishes no `tuner` topic, and three sensors that say nothing are better than three that
invent a zero. **The media player and the wake button are the exceptions**: they stay usable
while the box is unreachable, because that is precisely when somebody wants to wake it.

### 4.1 Media player

| Key | Polish name | unique_id | Topics |
|---|---|---|---|
| (the device itself) | *Dekoder salon* | `<node_id>_media_player` | `power`, `service`, `epg`, `volume`, `screen`, `channels` |

- **State** — `playing` when `power` is `on` and the box is online, `off` otherwise. Standby
  and deep standby are both `off`: `MediaPlayerState.STANDBY` was deprecated in Home Assistant
  2026.8, and what the household sees is a dark television either way. The „Zasilanie" switch
  and the device's availability tell the two apart for anyone who needs it.
- **Source** — the current channel name; the source list is the channels of the selected
  bouquets, in bouquet order, with a duplicate name listed once. Selecting one publishes
  `cmd/zap {"name": …}` when that name is unique, and the service reference when it is not —
  the plugin refuses an ambiguous name, and rightly, but a person picking a name off a list
  has already made the choice it is refusing to make.
- **Browsing** — bouquets, then channels, each with the picon the box's own web interface
  serves at `http://<ip>/picon/<sref>.png`. Picons are not published over MQTT; no address on
  `info` simply means no thumbnail.
- **Playing** — `media_content_type: channel` takes a service reference, and `channel_name`
  takes a name. The browser sends the first.
- **Volume** — set, step and mute, on the box's own 0–100 scale underneath.
- **Turning on** — `cmd/power on` when the box is listening, a Wake-on-LAN magic packet when
  it is not. Turning off is always `cmd/power standby`; nothing here sends deep standby.
- **Picture** — the retained `screen` JPEG is both the media image and the entity picture, and
  its hash changes with every frame so the browser fetches the new one.
- **Position** — from `epg.now`, so the progress bar is the programme, not a stream.

### 4.2 Remote, notify, event, image

| Key | Polish name | unique_id | Source |
|---|---|---|---|
| `remote` | *Pilot* | `<node_id>_remote` | `power` / `cmd/key` |
| `osd` | *Ekran OSD* | `<node_id>_osd` | `cmd/message` |
| `key` | *Pilot – klawisz* | `<node_id>_key` | `key` |
| `screen` | *Ekran* | `<node_id>_screen` | `screen` |

- **Pilot** — `remote.send_command` takes `KEY_RED` and `red` alike; spaces are dropped, since
  the Linux names run the words together (`channel up` is `KEY_CHANNELUP`). `hold_secs`
  greater than zero sends a long press, `delay_secs` spaces a sequence out, `num_repeats`
  repeats it. A key name this integration has never heard of is passed through: the box is the
  side that knows which keys it has, and it answers on `last_error` when it does not.
- **Ekran OSD** — `notify.send_message` puts a popup on the television. The popup has one text
  field, so a title becomes the first thing in it rather than being dropped; the text is cut
  at 500 characters, where the plugin cuts it.
- **Pilot – klawisz** — fires for every key the box reports, with the key name as the event
  type and `press` (`short` or `long`) as an attribute. An event entity may only fire types it
  declared, so a key outside the declared list is logged at debug and dropped here — the bus
  event below still carries it.
- **Ekran** — the last screenshot, with the moment it was taken as the state. It is a
  snapshot, not a live view, and it is unavailable until the first frame arrives.

### 4.3 Sensors

| Key | Polish name | unique_id | Topic | Notes |
|---|---|---|---|---|
| `channel` | *Kanał* | `<node_id>_channel` | `service` | attributes `sref`, `bouquet`, `provider`, `width`, `height` |
| `program` | *Program* | `<node_id>_program` | `epg` | `epg.now.title`; attributes `begin`, `end` (ISO 8601), `event_id`, `short`, `long` |
| `next_program` | *Następny program* | `<node_id>_next_program` | `epg` | the same, for `epg.next` |
| `active_recordings` | *Aktywne nagrania* | `<node_id>_active_recordings` | `recording` | the count; attribute `recordings` is the list |
| `next_timer` | *Następny timer* | `<node_id>_next_timer` | `recording` | a timestamp, `unknown` when nothing is due |
| `snr` | *SNR* | `<node_id>_snr` | `tuner` | % · diagnostic · **disabled by default** |
| `agc` | *AGC* | `<node_id>_agc` | `tuner` | % · diagnostic · **disabled by default** |
| `ber` | *BER* | `<node_id>_ber` | `tuner` | count · diagnostic · **disabled by default** |
| `uptime` | *Czas pracy* | `<node_id>_uptime` | `info` | seconds · diagnostic · **disabled by default** |

Times in attributes are ISO 8601 strings rather than the epoch seconds the topics carry,
because a template can read one and not the other. The long programme description and the list
of running recordings are excluded from the recorder: they are kilobytes that change every
quarter of an hour, and a database is not where a plot summary belongs.

### 4.4 Binary sensors, switches, number

| Key | Polish name | unique_id | Topic | Notes |
|---|---|---|---|---|
| `recording` | *Nagrywanie* | `<node_id>_recording` | `recording` | device class `running` — what to check before rebooting anything |
| `recording_disk` | *Dysk nagrań* | `<node_id>_recording_disk` | `hdd` | device class `connectivity`; attributes `path`, `free_mb` |
| `power` | *Zasilanie* | `<node_id>_power` | `power` | ↔ `cmd/power on\|standby` |
| `mute` | *Wyciszenie* | `<node_id>_mute` | `volume` | ↔ `cmd/mute ON\|OFF` |
| `volume` | *Głośność* | `<node_id>_volume` | `volume` | 0–100, the box's own scale ↔ `cmd/volume` |

The switches and the number are optimistic: they move the moment they are pressed and the
state topic confirms a moment later. That is not `assumed_state` — the box does report back —
it is the opposite: the answer is coming, and the dashboard should not sit still until it does.

### 4.5 Buttons and update

| Key | Polish name | unique_id | Command |
|---|---|---|---|
| `deep_standby` | *Głębokie uśpienie* | `<node_id>_deep_standby` | `cmd/deep_standby` · **hidden unless the option asks for it** |
| `restart_gui` | *Restart GUI* | `<node_id>_restart_gui` | `cmd/restart_gui` |
| `reboot` | *Restart* | `<node_id>_reboot` | `cmd/reboot` · **hidden unless the option asks for it** |
| `wake` | *Obudź (WoL)* | `<node_id>_wake` | `wake_on_lan.send_magic_packet` — no MQTT, and available while the box is not |
| `screenshot` | *Zrzut ekranu* | `<node_id>_screenshot` | `cmd/screenshot` |
| `refresh_discovery` | *Odśwież discovery* | `<node_id>_refresh_discovery` | `cmd/discovery` |
| `plugin` | *Wtyczka MQTT Bridge* | `<node_id>_plugin` | an `update` entity: installed = `info.plugin`, latest = the plugin release this version was written against |

The version entity **cannot install anything yet** — the installer is M4. It is there because a
box running an older plugin than the integration expects is the first thing to check when
something is missing, and the device page is where that should be visible without reading a
log. A box running a *newer* plugin is reported as up to date: the constant is what this code
was written against, not what exists.

## 5. Actions

Every action targets the receiver's **media player**, which is how a device, an area or an
entity all resolve to the same box — Home Assistant expands a device target to the entities of
the platform an action was registered on, and one box has exactly one media player.

| Action | Fields |
|---|---|
| `enigma2_mqtt.zap` | `sref` **or** `name` — exactly one |
| `enigma2_mqtt.send_key` | `key` (`KEY_RED` or `red`), `long` |
| `enigma2_mqtt.message` | `text` (cut at 500 characters), `type` (`info`\|`warning`\|`error`), `timeout` |
| `enigma2_mqtt.add_timer` | `sref` + `event_id`, **or** `sref` + `begin` + `end` + `name` |
| `enigma2_mqtt.delete_timer` | `sref`, `begin`, `end` |
| `enigma2_mqtt.record` | `action`: `start` or `stop` |
| `enigma2_mqtt.screenshot` | — |
| `enigma2_mqtt.set_ha_mode` | `mode`: `discovery`, `integration` or `off` |
| `enigma2_mqtt.get_epg_grid` | `bouquet` (optional) · **returns a response** |

`begin` and `end` take a date and time or the epoch seconds the topics use; both end up as
epoch seconds on the wire.

### How an action knows it worked

The contract has **no acknowledgement topic**: a command is proved by the state topic it moves
and disproved by `last_error`. So every action that changes something waits up to ten seconds
for whichever comes first, and raises `HomeAssistantError` carrying the box's own words when
the box refuses. A retained `last_error` is ignored — that is the complaint the broker had
before the command was sent, not an answer to it.

| Action | What proves it |
|---|---|
| `zap` | `service` names the requested channel |
| `record` | `recording.active` becomes non-empty, or empty |
| `add_timer`, `delete_timer` | `timers` is republished |
| `screenshot` | `screen` is republished |
| `set_ha_mode` | `info.ha_mode` echoes the new mode |
| `get_epg_grid` | the grid is already retained; a box with none is asked once |

Two commands move nothing at all — `send_key` and `message` — and for those the only answer
the contract offers is silence. They wait a second for a complaint and then report success; a
plugin that clears `last_error` on success ends the wait immediately.

Zapping to the channel that is already on is still sent, but nothing is waited for: there is
no change coming, and pretending otherwise would mean waiting out the whole timeout.

### `get_epg_grid`

Returns `{"bouquets": [...]}`, assembled from the retained `epg_grid/<bouquet_slug>` topics the
box publishes, each entry carrying the bouquet's real name and the slug it is addressed by. An
optional `bouquet` selects one, by either. Like every entity action's response it is keyed by
the entity it came from:

```yaml
action: enigma2_mqtt.get_epg_grid
target:
  entity_id: media_player.dekoder_salon
data:
  bouquet: Ulubione TV
response_variable: grid
```

…and then `grid['media_player.dekoder_salon'].bouquets[0].channels`.

It is deliberately not a state attribute anywhere: a grid runs to tens of kilobytes per
bouquet, and an attribute of that size is written to the recorder database on every refresh.

## 6. Device triggers

The colour keys — red, green, yellow, blue — each as a short and a long press: eight triggers
on the receiver in the automation editor.

| Trigger type | Fires on |
|---|---|
| `red_short` … `blue_short` | a tap of that colour key |
| `red_long` … `blue_long` | that colour key held |

They listen to the `enigma2_mqtt_key` bus event, which carries `device_id`, `node_id`, `key`
and `press` and is fired for **every** key the box reports. The event is fired by the
integration itself rather than by the „Pilot – klawisz" entity, so the triggers keep working
when that entity is disabled — a household that only wants the colour keys in the automation
editor should not have to keep an entity it never looks at.

Any other key is still reachable: either through the event entity, or through a plain event
trigger on `enigma2_mqtt_key` with the key name in its event data.

## 7. Diagnostics

The device page offers a diagnostics download: the config entry, the box's announcement, its
last `info` payload, the availability flag, the capability list, the last payload of every
state topic, and the device as the registry holds it. It is meant to be attached to an issue,
so the MAC address, the IP address and the configuration URL built from it are redacted, and so
are the broker and SSH credential keys — those are named in the redaction list before the
installer of M4 can create one, rather than after.

Three things are summarised rather than included, because their contents answer no question a
bug report asks and describe a household instead. **`screen`** appears as a size and the moment
it was taken, never as the picture of somebody's television. **`channels`** appears as the
bouquet names and how many services each holds, not as the channel list. **`key`** does not
appear at all: it is who pressed what a moment ago, which is not state.

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

### Removing a receiver

Delete the entry in *Settings → Devices & Services*. On the way out the integration publishes
`cmd/ha_mode = discovery`, so the plugin announces itself again and the core MQTT integration
rebuilds its own entities — a box that was taken over here is handed straight back. That
publish is best effort: if the receiver is off or the broker is unreachable the entry is still
removed, and the mode can be set on the plugin's setup screen afterwards.

**If you are also uninstalling the plugin, send `cmd/reset` to the box first.** Retained topics
outlive the plugin that created them: remove the package without resetting and the broker goes
on serving a snapshot of a receiver that is gone, for as long as the broker lives. `cmd/reset`
retracts everything the node owns and republishes it in one burst, so it is safe to run at any
time — but only while the plugin is still running, because after `opkg remove` there is nothing
left to ask. The plugin's
[topic contract](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/TOPICS.md)
describes it in full.

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

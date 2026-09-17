<p align="center">
  <img src="custom_components/enigma2_mqtt/brand/logo.png" alt="Enigma2 MQTT for Home Assistant" width="420">
</p>

<h1 align="center">Enigma2 MQTT for Home Assistant</h1>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
  <a href="https://www.home-assistant.io/"><img src="https://img.shields.io/badge/Home%20Assistant-2026.3%2B-41BDF5.svg" alt="Home Assistant"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/iot__class-local__push-blue.svg" alt="iot_class: local_push">
  <img src="https://img.shields.io/github/v/release/deltasystems-pl/hass-enigma2-mqtt" alt="Release">
</p>

Your **Enigma2 receiver** (Vu+, Dreambox, Zgemma, Octagon … on OpenViX, OpenATV, OpenPLi)
in Home Assistant **without polling**. The
[`enigma2-mqtt-bridge`](https://github.com/deltasystems-pl/enigma2-mqtt-bridge) plugin runs
inside enigma2 and publishes the moment something happens — a zap, a programme change,
standby, a recording, a volume step, a key on the remote. This integration turns those
topics into a native `media_player`, a `remote`, an OSD `notify` target and device triggers.

> **Status — M4 development candidate.** The entity and action surface is implemented and the
> guided SSH installer, local verified plugin bundle and update action are now in code. They
> have not completed live installation acceptance and are not a published release yet.

## What you get

One receiver becomes **one device** with these entities (display names are Polish in the
`pl` translation, English in `en`; entity ids come from the English keys):

| Platform | Name | What it shows or does |
|---|---|---|
| `media_player` | *Dekoder salon* (the device name) | off or playing, the channel list of the bouquets you choose, `select_source`, `play_media` by service reference or channel name, `browse_media` through bouquets, volume and mute, channel ± , the screen grab as artwork |
| `remote` | *Pilot* | `send_command` with `KEY_*` names; `hold_secs` makes it a long press |
| `notify` | *Ekran OSD* | a message on the television screen |
| `event` | *Pilot – klawisz* | every remote key as an event, with `press` = short or long |
| `image` | *Ekran* | the last screen grab from the box |
| `sensor` | *Kanał*, *Program*, *Następny program*, *Aktywne nagrania*, *Następny timer*, *SNR*, *AGC*, *BER*, *Czas pracy* | what is on, what is next, recordings and timers, tuner quality, uptime (tuner and uptime sensors off by default) |
| `binary_sensor` | *Nagrywanie*, *Dysk nagrań* | whether a recording is running, whether the recording disk is mounted |
| `switch` | *Zasilanie*, *Wyciszenie* | standby, mute |
| `number` | *Głośność* | volume 0–100 |
| `button` | *Głębokie uśpienie*, *Restart GUI*, *Restart*, *Obudź (WoL)*, *Zrzut ekranu*, *Odśwież discovery* | one-shot box actions; deep standby and reboot stay hidden until you enable them |
| `update` | *Wtyczka MQTT Bridge* | the installed plugin version and, when SSH credentials were retained, a guarded reinstall/update from the verified local bundle |
| device triggers | red / green / yellow / blue × short / long | remote keys as automation triggers |

Plus the actions `zap`, `send_key`, `message`, `add_timer`, `delete_timer`, `record`,
`screenshot`, `set_ha_mode` and `get_epg_grid` — each verified by the plugin and answered on
its state topic. `get_epg_grid` returns a bouquet's programme grid as a response, never as a
state attribute, because a grid is tens of kilobytes and an attribute goes to the recorder.

## How it works

```
Enigma2 box                         MQTT broker                    Home Assistant
  MQTTBridge plugin  ──publishes──►  enigma2/<node_id>/…   ──────►  enigma2_mqtt
  (hooks inside enigma2)            enigma2mqtt/discovery/…         (this repository)
                     ◄──commands──  enigma2/<node_id>/cmd/#
```

The plugin has a `ha_mode` setting with two ways of reaching Home Assistant:

- **`discovery`** (the plugin's default) — the plugin publishes standard Home Assistant MQTT
  discovery and the core MQTT integration builds a basic set of entities. This integration is
  not needed at all.
- **`integration`** — the plugin publishes only its announcement and this integration builds
  every entity, including the ones discovery cannot express (media player with browsing,
  remote, notify, device triggers, installer, update). Adding a box here switches it into this
  mode and the plugin **retracts** its discovery payloads first, so entities are never
  duplicated.

Availability is the broker's last will, so a box that loses power is `unavailable` within
about 45 seconds instead of at the end of a poll cycle.

## Requirements

- Home Assistant **2026.3** or newer
- The **MQTT integration** configured and connected (the Mosquitto add-on is fine)
- The **[enigma2-mqtt-bridge](https://github.com/deltasystems-pl/enigma2-mqtt-bridge)** plugin
  installed on the receiver

| Integration | Plugin | Status |
|---|---|---|
| 0.1.0 | 0.1.0 | current |

The integration refuses nothing when the versions differ, but the `update` entity tells you
when the box runs a plugin older than the one this release was written against.

## Installation

### HACS (recommended)

1. HACS → ⋮ → *Custom repositories* → add `deltasystems-pl/hass-enigma2-mqtt`, category
   **Integration**
2. Install **Enigma2 MQTT**, restart Home Assistant

### Manual

Copy `custom_components/enigma2_mqtt` into your `config/custom_components/` and restart.

## Setup

- **Discovered** — a box whose plugin is already configured announces itself on
  `enigma2mqtt/discovery/#`; Home Assistant offers it in *Settings → Devices & Services* and
  one click confirms it. Confirming switches the box into integration mode and waits for it to
  say so, so a box that cannot be reached is reported rather than added as a dead device.
- **Manual** — *Add integration → Enigma2 MQTT*, then the base topic and the node id, for
  boxes behind a bridged broker or with a custom prefix. The node id is on the plugin's setup
  screen; the flow checks the box is really on that topic before it adds anything.
- **Install the plugin from here** — host, SSH user and password; the flow runs a
  preflight over SSH, uploads the bundled IPK, installs it, writes the provisioning file and
  waits for the announcement. The SSH password is discarded afterwards unless you ask to keep
  it for updates.

## Security

Give the box **its own broker login**, not the one Home Assistant uses, and limit it to its
own topics. For the Mosquitto add-on, in the ACL file:

```
user enigma2-<node_id>
topic readwrite enigma2/<node_id>/#
topic write enigma2mqtt/discovery/<node_id>/#
topic write homeassistant/device/<node_id>/#
topic write homeassistant/device_automation/<node_id>/#
```

Most Enigma2 images ship with a **default root password and an open telnet/SSH**. Anyone on
that network can read the broker credential out of the box's settings — change the root
password first, and treat the receiver as the least trusted device in the chain.

If you let the installer keep the SSH password for later updates, it is stored in the config
entry. Home Assistant's `.storage` is **not encrypted at rest**; leave the box unticked and
the password is discarded as soon as the install finishes. The options flow can add retained
credentials later or forget them again. They are never written to the log or to diagnostics.

The integration contains the GPL plugin IPK together with its exact corresponding source
archive and provenance metadata; it does not fetch executable code at runtime. Installation
takes a private receiver backup, verifies the uploaded package and rolls back a failed
transaction where possible. A loss of receiver power or storage during the transaction can
still require recovery from that backup.

## Privacy

The `key` and `epg` topics say what is being watched and which buttons are pressed, and the
`image` entity is a picture of the screen. Nothing leaves your network — there is no
telemetry and no cloud — but the recorder keeps a history unless you tell it not to:

```yaml
recorder:
  exclude:
    domains:
      - image
    entity_globs:
      - event.*_remote_key
```

The integration options can switch key publishing off, select `off`, `on_zap` or `interval`
screenshots, and set the interval. The plugin validates and persists all three settings as one
transaction; disabling screenshots also retracts the retained image.

## Documentation

**[Read the full documentation → DOCUMENTATION.md](DOCUMENTATION.md)** — concepts, every
entity and action, diagnostics, troubleshooting and an FAQ. The topic contract itself lives
with the [plugin](https://github.com/deltasystems-pl/enigma2-mqtt-bridge). Design decisions
are recorded as [ADRs](docs/adr/); version history is in the [CHANGELOG](CHANGELOG.md); the
quality bar we hold ourselves to is [docs/QUALITY.md](docs/QUALITY.md).

## Roadmap

- [x] **M0** — product requirements approved ([ADR-0000](docs/adr/0000-prd.md),
      [ADR-0001](docs/adr/0001-m0-decisions.md))
- [x] **M1** — config flow (discovered + manual), the device page, diagnostics
- [ ] **M2** — plugin state and discovery complete, commands with guards
- [ ] **M3** — the entities above, actions, device triggers, diagnostics, translations
- [ ] **M4** — SSH installer, bundled IPK and `update` implemented; live acceptance pending
- [ ] **M5** — public beta `v0.x`: releases, HACS custom repository, call for testers
- [ ] **M6** — `v1.0.0`: HACS default store, deep standby and Wake-on-LAN drilled
- [ ] **M7** — afterwards: broker-login provisioning, further images

## Contributing

Pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the dev loop and the
house rules. Two things help most right now:

- **Testers on other images.** OpenATV, OpenPLi and OpenBH have no test box yet.
- **Translators.** English is the source language, Polish is reviewed, **German is drafted
  and needs a native speaker** — open a pull request against
  `custom_components/enigma2_mqtt/translations/de.json`, or add your own language.

## License

[MIT](LICENSE). The receiver plugin is a separate program under GPL-2.0-or-later; see
[NOTICE](NOTICE).

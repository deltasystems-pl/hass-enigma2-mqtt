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
| `media_player` | *Dekoder salon* (the device name) | off or playing, the channel list of the bouquet the receiver is on (or of every bouquet you choose — an option), `select_source`, `play_media` by service reference, channel name or active bouquet, `browse_media` through playable bouquets, volume and mute, channel ±, the screen grab as artwork |
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
| `select` | *Bukiet*, *Kanał* | the bouquet the receiver's channel ± walks, and the channels inside it; only on a plugin that can switch a bouquet |
| device triggers | red / green / yellow / blue × short / long | remote keys as automation triggers |

OSCam health is optional and off by default. When the receiver plugin advertises support, the
options page can expose neutral process/API health, ready local-reader counts, connected servers
and server-reported shared-card counts. Per-source entities use stable opaque IDs; private reader
labels, server addresses, accounts and card identifiers are neither published nor retained by
the integration.

Plus the actions `zap`, `select_bouquet`, `send_key`, `message`, `add_timer`, `delete_timer`, `record`,
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
| 0.2.0 (unreleased) | 0.2.0 (unreleased) | in development |
| 0.1.0 | 0.1.0 | current release |

The integration ships the plugin it was built against, so the two move together. The bundled
build of an unreleased integration is an unreleased plugin build.

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
  boxes behind a bridged broker or with a custom prefix. A receiver hostname or IP address is
  optional metadata for its device link and later SSH setup; MQTT-only bridges can leave it
  blank. The flow checks the box is really on the MQTT topic before it adds anything.
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
screenshots, set the interval and choose how long an on-zap capture waits for the new picture.
The plugin validates and persists the settings as one transaction; disabling screenshots also
retracts the retained image. Optional conditional-access telemetry publishes only the current
service's system, encrypted/active result and ECM time. It never publishes server, account or
card details, and creates no entities until explicitly enabled.

## Documentation

**[Read the full documentation → DOCUMENTATION.md](DOCUMENTATION.md)** — concepts, every
entity and action, diagnostics, troubleshooting and an FAQ. The topic contract itself lives
with the [plugin](https://github.com/deltasystems-pl/enigma2-mqtt-bridge). Design decisions
are recorded as [ADRs](docs/adr/); version history is in the [CHANGELOG](CHANGELOG.md); the
quality bar we hold ourselves to is [docs/QUALITY.md](docs/QUALITY.md).

## Roadmap

- [x] **M0** — product requirements approved ([ADR-0000](docs/adr/0000-prd.md),
      [ADR-0001](docs/adr/0001-m0-decisions.md)); the scope added since is
      [ADR-0002](docs/adr/0002-scope-after-m0.md)
- [x] **M1** — config flow (discovered + manual), the device page, diagnostics. Released as
      **v0.1.0**, which is what HACS still serves
- [ ] **M2** — plugin state and discovery complete, commands with guards: *the receiver plugin's
      milestone — code complete and running, its long soak and the deep-standby drill still open*
- [ ] **M3** — the entities above, actions, device triggers, diagnostics, translations: *live on
      the maintainer's Home Assistant, not released*
- [ ] **M4** — SSH installer, bundled IPK and `update`: *coded and reviewed, awaiting acceptance.*
      The installer has never been run end to end on a receiver without the plugin, and no real
      rollback has been exercised
- [ ] **M5** — public beta `v0.x`: releases, HACS custom repository, call for testers
- [ ] **M6** — `v1.0.0`: HACS default store, deep standby and Wake-on-LAN drilled
- [ ] **M7** — afterwards: broker-login provisioning, further images

Everything after M1 is unreleased, and both halves still report `0.1.0`: a coordinated version bump
with the plugin has not happened yet.

### What 0.2.0 and 0.3.0 will carry

Two days of household use produced a list of problems and a list of wants, and they are split into
two releases. The reasoning is in
[ADR-0003](docs/adr/0003-control-feedback-and-household-features.md). **What is struck through
below is built and unreleased; the rest is not implemented.**

**0.2.0 — fixes.** Cut after the guided installer has been run end to end on a receiver.

- **Buttons wait for the receiver and raise when it refuses**, instead of publishing and returning.
  A refused button used to be indistinguishable from a broken one.
- **A „Last error" sensor** that remembers the receiver's last refusal with its text and time, and
  keeps it across restarts — the plugin clears the topic on the next success, and the user is
  usually looking afterwards.
- **Deep standby and reboot appear only when the receiver says they are permitted**, and the option
  text names both gates and where the box-side switch is.
- **The Wake-on-LAN address is validated and normalised** instead of being passed through, is
  registered on the device, and a malformed stored value is repaired once.
- **„Remote" is hidden by default** on new installations — Home Assistant gives every remote entity
  a power toggle, which made three power controls on one device. Existing installations are not
  rewritten.
- ~~**A `select` platform: „Bouquet" and „Channel"**, which is what integration mode was missing
  entirely — and a `source_list_scope` option defaulting to the active bouquet, because a source
  list of many hundred channels exceeds the recorder's 16 KB attribute limit and loses the media
  player its history.~~ **Built** — see the [changelog](CHANGELOG.md).
- **A button that rebuilds the EPG grid.**

**0.3.0 — features**, following the receiver plugin: a **second notify entity** for the discreet
toast and a `style` field on the `message` action; a **softcam** button and sensor (the auto-heal
settings are in the options, the permission is not); an **EPG import** button and status sensor; an
**EPG sensor for the active bouquet** whose payload is declared unrecorded; and the **process**
sensors, already in review.

**Testers wanted: open an issue.** OpenATV, OpenPLi and OpenBH have no test box. There is no
per-image thread to find yet — yours would start it.

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

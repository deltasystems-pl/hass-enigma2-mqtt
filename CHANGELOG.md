# Changelog

All notable changes to this integration are documented here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Planned as 0.2.0. Everything below has landed since 0.1.0 and is not released yet: the
guided installer, the update path and the new diagnostics have been tested locally but
have not been accepted on a receiver, and no tag has been cut.

### Added

- **The receiver as a media player.** What is on and what is next, the channel list of the
  bouquets you choose, browsing bouquet by bouquet with the box's own picons, volume, mute,
  channel up and down, and the last screen grab as the artwork. It stays usable while the box
  is unreachable, because that is when you want to wake it — and turning it on then sends a
  Wake-on-LAN packet instead of a message the box cannot hear.
- **A remote.** `remote.send_command` takes `KEY_RED` or just `red`, holds a key when you ask
  it to, and sends a sequence with a pause between the presses.
- **A message on the television.** `notify.send_message` puts a popup on the screen.
- **Every remote key as an event**, with short and long presses told apart, and the four
  colour keys offered in the automation editor as device triggers on the receiver.
- **Sensors** for the channel, the programme now and next, running recordings and the next
  timer — and, for when something is wrong, signal quality and uptime, switched off until you
  ask for them.
- **Binary sensors** for whether a recording is running and whether the recording disk is
  still mounted, which is the failure that otherwise goes unnoticed for weeks.
- **Switches** for standby and mute, a **volume slider**, and **buttons** for a GUI restart, a
  screen grab, a Wake-on-LAN packet and a discovery refresh. Deep standby and reboot are there
  too, hidden until you turn them on, so neither sits one mis-tap from the volume.
- **An image** of the last screen grab, and an **update entity** that says when the receiver
  runs an older plugin than this release expects. With explicitly retained SSH credentials it
  can install the verified plugin bundled with the integration without replacing the box's
  existing broker or plugin settings.
- **Ten actions** — `zap`, `select_bouquet`, `send_key`, `message`, `add_timer`, `delete_timer`,
  `record`, `screenshot`, `set_ha_mode` and `get_epg_grid` — each aimed at a receiver, a device or an
  area. They wait for the receiver to actually do the thing and report what it says when it
  refuses, rather than reporting success for a message that was merely sent.
- A playable bouquet in the media browser activates the receiver's real channel-list context,
  so subsequent channel-up and channel-down follow that list. The current channel is preserved
  when it belongs to the bouquet; otherwise the receiver tunes the first playable channel.
- **Options**: whether the deep standby and reboot buttons appear, which address a Wake-on-LAN
  packet goes to, which bouquets are worth browsing, key-event publishing and the screenshot
  policy, interval and post-zap delay. Receiver options are acknowledged by fresh plugin state
  before HA saves them. Opt-in conditional-access diagnostics expose only the bounded current
  service result and never retain reader, server, account or card details.
- **Optional receiver address metadata** in manual setup and reconfigure, used for the device
  link and as an SSH suggestion without making direct receiver access a requirement.
- **Opt-in OSCam health diagnostics** from the plugin's privacy-bounded topic: process and API
  health, aggregate reader/server counts, and dynamic per-source status under stable opaque IDs.
  Reader labels, addresses, accounts and card identifiers never enter integration state or
  diagnostics; API outages preserve customized entities as unavailable until a complete snapshot.
- **A guided SSH installer** with host-key confirmation, recording/timer guards, pre-change
  backup, uploaded-file verification, guarded GUI restart and rollback. SSH credentials are
  opt-in for later updates and can be enrolled, refreshed or forgotten without unloading MQTT.
- **A reproducible local plugin bundle** with pinned commit metadata and the corresponding GPL
  source archive. Runtime installation has no release-site or other network download path.
- **Reconfigure**, for following a receiver whose node ID or base topic was changed on its own
  setup screen.
- **Polish and German** names for every entity, action and trigger. German is still a draft
  and would welcome a native speaker.

### Changed

- **The diagnostics download now carries the last payload of every state topic.** The screen
  grab appears as a size and a timestamp, the channel list as its shape, and the remote keys
  not at all: a bug report should answer a question, not describe a household.
- Command cleanup now retires internal waiters without spurious asyncio errors. Clearing an old
  plugin error no longer reports concurrent commands as complete before their own result is
  known.

## [0.1.0] - 2026-09-16

The first release. It sets a receiver up and gives it a device page; the entities and
actions the README describes arrive in a later release.

### Added

- **Setup by discovery.** A receiver running the `enigma2-mqtt-bridge` plugin announces
  itself and turns up in *Settings → Devices & Services*, named with its box type and its
  image. Confirming it switches the receiver into integration mode and waits for the
  receiver to say the switch worked, so a box that cannot be reached is reported instead of
  being added as a dead device.
- **Setup by hand.** Base topic, node id and an optional name, for a receiver behind an MQTT
  bridge that rewrites the topic prefix, or one whose announcement never arrived. The flow
  checks the receiver is really on that topic before it adds anything.
- **A device page for each receiver** — the manufacturer, the model, the image and plugin
  versions, and a link to the receiver's own web interface. It follows the receiver, so
  updating the plugin or moving the box to another address updates the page.
- **A diagnostics download** to attach to a bug report, with the MAC address, the IP address,
  the web interface link and every password removed.
- **Removing a receiver hands it back** to Home Assistant's own MQTT discovery, so the basic
  entities return. It is best effort: a receiver that is switched off, or a broker that
  cannot be reached, never blocks the removal.
- **Polish and German translations** of everything the setup screens show. German is a draft
  and would welcome a native speaker.
- **Installation through HACS** as a custom repository, alongside the README, the full
  documentation, the security and contribution policies and the decision records behind the
  design.
- **Checks on every change** — hassfest, the HACS action, ruff and the test suite run on
  every push, every pull request, once a week and on the release tag itself, and a release
  is only published when the tag, the manifest version and the changelog agree.

[Unreleased]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/releases/tag/v0.1.0

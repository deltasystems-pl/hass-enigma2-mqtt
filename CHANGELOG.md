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
- **The setup screen now says how to give the receiver a broker login.** The broker password ends
  up in a file on the box's own flash, and most Enigma2 images answer SSH with the image's default
  root password, so the step that asks for it now shows the ACL that confines that login to one
  receiver — and says plainly that the Home Assistant Mosquitto add-on does not enforce one, where
  a dedicated login names the receiver in the broker log but does not confine it.
- **An opt-in release check on the version entity**, off by default and off means no outbound
  connection at all. Turned on, it asks the plugin's repository which release is published —
  at most once every 24 hours — and reports the tag in the summary, in an attribute and in the
  release link. It downloads nothing, and it never raises the offered version above the bundle
  that is actually shipped here, because a card cannot offer what `install` would refuse.
  The limit is kept where it survives: the time of the last request and its answer are stored
  per receiver, so reloading, saving the options or restarting Home Assistant shows what is
  already known rather than spending another request, and removing the receiver deletes the
  record. The answer is read up to 64 KiB and no further, a tag is believed only if it is short
  enough and parses as a version, and the release link is only followed if it points into this
  plugin's own releases — it arrives over the network, and it ends up as a link a household is
  invited to click.
- **The version entity explains itself.** An older plugin than the bundled one says what
  installing will do, or — with no SSH credentials stored — says that it cannot install and how to
  change that. A newer plugin says it is ahead rather than pretending to be in step, and the
  diagnostics download carries the same verdict in words, which is the one version mismatch no
  screen in Home Assistant can show.
- **Polish and German** names for every entity, action and trigger. German is still a draft
  and would welcome a native speaker.
- **„Bukiet" and „Kanał", two selects.** The receiver plugin publishes a channel select of its
  own only in MQTT discovery mode, and this integration takes a box out of that mode — so a
  receiver set up the way this integration wants it listed neither bouquets nor channels
  anywhere. „Bukiet" lists what the receiver published and switches its real channel-up/down
  context; „Kanał" lists the channels of whichever bouquet that is, and reshapes the moment the
  context moves. Both select by service reference rather than by name, both wait for the
  receiver and raise its own words when it refuses, and both exist only while the receiver says
  it can switch a bouquet at all. A name repeated inside one bouquet is numbered rather than
  dropped, because a dropped one would be a channel Home Assistant could not reach.

### Fixed

Found by running this release against a real receiver rather than a test one.

- **The per-source OSCam entities now appear.** A receiver answers after Home Assistant has
  finished setting the integration up, and the entities for each reader and server were only
  created for a box that had already answered — so on a real receiver they were never created
  at all. They also survive a reload now: only switching the option off removes them.
- **An OSCam version with a build suffix is shown** instead of nothing. Real builds report
  versions like `1.20_svn build r11718-079`, which the version check had no room for. A
  receiver installed from the bundle shipped here now reports that version too.
- **The Wake-on-LAN address is redacted** from the diagnostics download, like every other
  hardware address in it.
- **A receiver whose stored SSH identity had become unreadable said "Unknown error".** A
  truncated write, a hand-edited entry or a backup restored from a different receiver leaves a
  host key that cannot be parsed, and that failure took a different route out of the installer
  from every other one: a raw traceback, no explanation, and — worst of it — no offer to
  re-accept the fingerprint, which is the one thing that fixes it. It is now reported as a
  changed host key and starts the same re-pinning it does.
- **A release could be published without the checks having run.** The release workflow built and
  uploaded the zip on a tag without hassfest, the HACS action, ruff, the tests or the bundle
  reproduction between the tag and the upload. A tag is pushed by a person at whatever commit
  they choose, so "main was green" was never evidence about the tagged tree. The release workflow
  now runs the whole validation suite first and publishes nothing if any of it fails.
- **The media player has its history back.** A receiver with 988 channels put 16.6 kB of names
  into `source_list`, which took the entity's attributes to 17.3 kB — past the recorder's
  16 384-byte limit, so Home Assistant dropped *all* of them and kept no history for the channel,
  the programme or the artwork either. The list now follows the bouquet the receiver is on, which
  fits. A new option, **Channels in the media player's source list**, takes it back to every
  bouquet for anybody who would rather have the long list than the history, and a receiver that
  publishes no channel-list context at all is left with the full list rather than an empty one.

### Changed

- **The diagnostics download now carries the last payload of every state topic.** The screen
  grab appears as a size and a timestamp, the channel list as its shape, and the remote keys
  not at all: a bug report should answer a question, not describe a household.
- Command cleanup now retires internal waiters without spurious asyncio errors. Clearing an old
  plugin error no longer reports concurrent commands as complete before their own result is
  known.
- **`select_source` is scoped to the list it offers.** Asking the media player for a channel that
  the receiver has but the source list is not currently showing now says so, and names the two
  ways on — switch „Bukiet", or use the `zap` action, which takes a service reference and ignores
  the scope. Where the same name is in several bouquets, the copy in the bouquet on the list is
  the one tuned. `play_media` with `channel_name` is unchanged and is not scoped.

### Documentation

- **[ADR-0002](docs/adr/0002-scope-after-m0.md) records the scope added and changed after M0** —
  bouquet activation, the opt-in conditional-access diagnostics, an options page that writes to the
  receiver and the privacy boundary that puts on the broker login, the hardened installer, the
  reproducible bundle, and the two lessons that came out of things breaking: a backup of this
  integration must live outside `custom_components/`, and a subscription is not in place until the
  broker says so. It also states what is **not** done: no 0.2.0 release, an installer never run end
  to end, no broker-credential help in the setup form, no hassfest re-run at release time, no
  opt-in release check, and coverage at 93 % against the project's own 95 %. The README's roadmap
  is corrected to match.
- **[ADR-0003](docs/adr/0003-control-feedback-and-household-features.md) records the 0.2.0 and
  0.3.0 plan** that came out of two days of household use. 0.2.0 is fixes: buttons that wait for
  the receiver and raise when it refuses, a „Last error" sensor that remembers a refusal the plugin
  has already cleared, power-off buttons gated on what the receiver says it permits, a Wake-on-LAN
  address that is validated rather than passed through, a remote hidden by default, a `select`
  platform for bouquet and channel, a source list that fits back inside the recorder's attribute
  limit, and a button for the EPG grid. 0.3.0 follows the receiver: a second notify entity for the
  discreet toast, softcam and EPG-import controls gated on box-side permissions, and an EPG sensor
  whose payload is declared unrecorded. **Nothing in the plan is implemented**, and the README's
  roadmap says so.

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

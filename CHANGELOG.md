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
- **A „Last error" sensor.** The receiver's last refusal, with its own words and the time the
  receiver put on it, kept across a reload and a restart — and across the replay of the retained
  complaint that follows every reconnect, which is why the time is the receiver's rather than a
  reading of the clock. The plugin clears its error topic on the next command that succeeds, so
  by the time anybody asks why a button did nothing the evidence is usually gone; this sensor is
  where it stays. It is the one entity here that stays available while the receiver is not,
  because deep standby is exactly a receiver that has left the network. There is no clear
  button — the next error replaces it.
- **A „Refresh EPG" button**, which rebuilds and republishes the programme grids. It exists only
  on a receiver that publishes them: with the plugin's grid setting at zero the capability is
  absent and so is the button.
- **„Bukiet" and „Kanał", two selects.** The receiver plugin publishes a channel select of its
  own only in MQTT discovery mode, and this integration takes a box out of that mode — so a
  receiver set up the way this integration wants it listed neither bouquets nor channels
  anywhere. „Bukiet" lists what the receiver published and switches its real channel-up/down
  context; „Kanał" lists the channels of whichever bouquet that is, and reshapes the moment the
  context moves. Both select by service reference rather than by name, both wait for the
  receiver and raise its own words when it refuses, and both are created only while the receiver
  reports **both** the `channels` and the `bouquet_context` capabilities. A name repeated inside
  one bouquet is numbered rather than dropped, because a dropped one would be a channel Home
  Assistant could not reach; the number follows the service reference, so reordering the bouquet
  on the receiver does not silently swap which channel „TVN HD (2)" tunes. It can still shift
  when a duplicate is added or removed, so an automation belongs on the `zap` action with a
  reference rather than on a label.
- **An option for how long the media player's source list is.** *Channels in the media player's
  source list* offers every bouquet you have chosen — which is what it has always done and stays
  the default — or just the bouquet the receiver is on, which on a receiver with a thousand
  channels is the difference between a dropdown of a thousand rows and one of ninety. A receiver
  that has published no channel-list context, or one whose context names a bouquet the bouquets
  option excludes or that holds no playable channel, keeps the long list: there is nothing to
  shorten it to, and an empty source list would leave no way to change channel at all. The last
  two of those say so in the log, once.

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
- **A button that the receiver refused looked exactly like one that worked.** „Restart" and
  „Deep standby" were pressed, the receiver refused both, and nothing was reported anywhere: the
  buttons published their command and returned, only the actions waited for an answer, and the
  refusal reached the diagnostics download and nothing else. Every button now waits for the
  receiver and raises its own sentence. Where there is an effect to watch — a new screen grab, a
  republished announcement — that is the proof. Where there is none, because the box is about to
  restart or because an unchanged EPG grid is not republished, the press waits a moment for a
  complaint and treats silence as success. One consequence you will see straight away: „Zrzut
  ekranu" pressed twice inside the plugin's minimum of five seconds between captures now says
  so, where it used to look like a press that worked.
- **„Deep standby" and „Restart" now also need the receiver's permission.** `deep_standby_allowed`
  is set on the box's own setup screen and deliberately cannot be written over MQTT, so Home
  Assistant had no way to know the two buttons it was offering would always be refused. They are
  created only while the Home Assistant option is on **and** the receiver reports that permission,
  and the option's text now names both gates and where the box-side one is. A receiver that never
  reports it — an older plugin — behaves exactly as before, with the option deciding alone.
- **The Wake-on-LAN address is validated, normalised and registered.** The option was stored with
  nothing but its spaces trimmed and handed to `wake_on_lan` as typed, so a malformed one failed
  with a complaint about a non-hexadecimal character at position 12 — from a component the user
  never went near. All four spellings (`00:00:5e:00:53:01`, `00-00-5e-00-53-01`, `0000.5e00.5301`
  and `00005e005301`) are now accepted and stored as one, anything else fails the form with a
  sentence, an empty field means the address the receiver reports, and that address is registered
  on the device so the rest of Home Assistant knows it too. A stored value that is not an address
  is dropped once at startup and logged.
- **A release could be published without the checks having run.** The release workflow built and
  uploaded the zip on a tag without hassfest, the HACS action, ruff, the tests or the bundle
  reproduction between the tag and the upload. A tag is pushed by a person at whatever commit
  they choose, so "main was green" was never evidence about the tagged tree. The release workflow
  now runs the whole validation suite first and publishes nothing if any of it fails.
- **A zap is confirmed by which service it is, not by how the reference is spelled.** One
  channel has more than one spelling — a reference can stop at the tenth colon or carry it, an
  IPTV entry adds its stream URL and its name, and the case of the hexadecimal fields is not
  agreed on anywhere. The `zap` and `select_bouquet` actions compared the receiver's answer with
  the string they had sent, so on a receiver that answers in its own spelling a command that had
  plainly worked was reported as a timeout ten seconds later. They now compare the fields that
  identify a service, by the same rule and the same field count as the receiver plugin.

### Changed

- **The diagnostics download now carries the last payload of every state topic.** The screen
  grab appears as a size and a timestamp, the channel list as its shape, and the remote keys
  not at all: a bug report should answer a question, not describe a household.
- Command cleanup now retires internal waiters without spurious asyncio errors. Clearing an old
  plugin error no longer reports concurrent commands as complete before their own result is
  known.
- **The bundled receiver plugin is rebuilt from a newer pinned commit**, which reports whether the
  box permits deep standby and clears bytecode an upgrade has orphaned. Its version is unchanged.
- **„Pilot" is hidden on the device page of a new installation.** Home Assistant gives every
  remote entity a power toggle, which put three controls that all switch power on one device and
  no way to tell which was the real one; „Zasilanie" is the labelled one. The remote is hidden,
  not disabled: it still has a state and `remote.send_command` still works. **Existing
  installations are not rewritten** — their registry already holds a visibility decision, and
  quietly hiding an entity somebody may have put on a dashboard would be worse than the confusion
  it fixes. Un-hide it in the entity's settings if you want it back.
- **`select_source` is scoped to the list it offers.** Asking the media player for a channel that
  the receiver has but the source list is not currently showing now says so, and names the two
  ways on — switch „Bukiet", or use the `zap` action, which takes a service reference and ignores
  the scope. Where the same name is in several bouquets, the copy in the bouquet on the list is
  the one tuned. `play_media` with `channel_name` is unchanged and is not scoped. What the media
  player reports as its **source** is still whatever is playing, even when the scope means that
  is not on the list: what is on is a fact and the length of a list is a preference.

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
  whose payload is declared unrecorded. The 0.2.0 fixes above are landing against that plan; the
  README's roadmap says which of them are done.

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

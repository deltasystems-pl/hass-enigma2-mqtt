# Changelog

All notable changes to this integration are documented here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **What enigma2 itself is using** — memory, its high-water mark, threads, open files and when
  the process started — when the receiver plugin can measure it. The memory is on by default,
  because "it has been getting slower for a fortnight" is a question the recorder can only
  answer if it was already collecting; the rest are switched off until something is wrong. They
  appear whenever the receiver announces the capability, and a receiver that stops announcing it
  never loses them.
- **„Restart softcam", a button that restarts the receiver's card-sharing client**, and a
  „Softcam" diagnostic beside it. The household symptom is a channel that stops decoding;
  underneath it, on images whose softcam binary has a long name, the image's own liveness
  check cannot recognise the process it started and adds another copy at every interface
  restart. The button stops every instance and starts exactly one, with the line the image
  itself would have used, so it collapses the copies as well as unsticking a frozen cam.
  The sensor states which binary the image selected — not a family name and not the
  protocol it speaks outward — and carries the instance count, the last restart and its
  reason, the restarts since local midnight, and two read-only facts about the image that
  say whether this receiver is one that accumulates copies at all.
- **Auto-heal, opt-in, on the options form.** With it on the receiver restarts its own
  softcam when the channel is encrypted and has not decoded for a window of 30 to 600
  seconds, at most once every ten minutes, and never while a recording is running or due —
  the same guard a manual press goes through, because a restart landing on the opening
  seconds of a recording is worse than a scrambled one.
- 🔴 The button needs **two gates** and both are the receiver's. The **permission**
  `softcam_restart_allowed` is set on the receiver's own setup screen and refused over
  MQTT, as `deep_standby_allowed` already is: a setting that enables a command stays
  outside what anything with publish rights on the broker can reach. It is therefore not
  on the options form, and the form says where it lives. The **`softcam` capability** is
  the other, and it is claimed only where a cam binary actually resolves — a receiver with
  the permission on and nothing to restart reports the permission, claims no capability
  and refuses the command, so no button is offered for it. A plugin that does not report
  the permission gets no button, and a receiver that has simply gone quiet keeps whatever
  it had.
- **[ADR-0005](docs/adr/0005-softcam-restart.md) records both gates and the rule behind
  them**, mirroring the plugin's own ADR-0005. The decision worth knowing about is that
  „Created on X" no longer implies „removed on not-X" here: a control that would be
  permanently refused is removed when its capability says so, and a diagnostic with a
  history is not, because deleting it takes the household's rename, area and recorded
  history with it and a quiet capability is not a decision anybody made.
- **„EPG – aktywny bukiet", what is on now and next across the bouquet the receiver is
  walking.** The state is how many of its channels have something to show; the `channels`
  attribute lists every channel of that bouquet with its programme now and next (title, begin
  and end), read from the grid the receiver already publishes for it. It follows the bouquet:
  switching bouquets on the remote or through „Bukiet" switches the list, and a grid for any
  other bouquet leaves it alone. Now and next follow the clock as well as the grid, so a
  programme that has ended stops being „now" when it ends rather than at the next grid refresh.
  A receiver in no bouquet at all reads `0`; a bouquet whose grid has not arrived reads
  `unknown`, so „no grid" and „nothing on" stay apart. 🔴 The channel list is excluded from the
  recorder — Home Assistant does not do that for an attribute of ours on its own, and a
  bouquet's worth of programmes rewritten every time one of them ends does not belong in the
  database. Created while the receiver announces both the `epg_grid` and `bouquet_context`
  capabilities, and never removed when they go quiet.
- **„Ekran – dyskretnie", a discreet toast on the television** (English „OSD toast"), beside
  „Ekran OSD". The popup takes focus and waits in the receiver's queue behind an open channel
  list; the toast is a small overlay in a corner that takes no key press, hides itself after
  five seconds and is replaced by the next one. A title is folded into the text as it is for
  the popup, and the text is cut at 200 characters, where the receiver cuts a toast. Created
  when the receiver announces the `toast` capability — which the plugin claims only once the
  screen has actually been built — and never removed when it goes quiet; sending to it while
  the receiver does not name the capability is refused with the reason instead of published.
- **The `message` action gains a `style` field**, `popup` (the default) or `toast`. A toast
  without a `timeout` stays five seconds, and one outside 1–30 seconds is refused before
  anything is sent, since nothing on a toast can dismiss it. A toast aimed at a receiver
  that does not offer one is refused the same way.

- **„Pobierz EPG", a button that runs the receiver's own EPG-Importer now** (English „Import
  EPG"), and **„Import EPG", a diagnostic** (English „EPG import") that says whether an import is
  `idle`, `running`, `done` or `failed`, with when it started and finished, how many events the
  importer processed, and why it failed. The sensor follows every import, including the ones the
  image's own schedule starts, so a press refused as „already running" is never unexplained. The
  press waits until the receiver reports the import running; every refusal — no permission, an
  import already running, a recording running or due within ten minutes, the image's own run due
  within ten minutes, no sources selected — raises the receiver's own sentence, and so does a
  start that failed. The import is the image's: at its end menus freeze for two to three
  seconds while the image saves the guide, and the importer's own deep-standby and „clear old
  EPG" settings apply as they do to a scheduled run.
- 🔴 The button has **two gates, both the receiver's**, as „Restart softcam" does: the
  permission `epg_import_allowed`, set on the receiver and refused over MQTT, and the
  `epg_import` capability, claimed only where the plugin found the importer loaded and able to
  import in place. Only a stated „no" to the permission removes the button; the sensor, which
  follows the capability alone, is never removed.
- **„Głębokie uśpienie" and „Obudź (WoL)" say so when the receiver cannot be woken over the
  network.** From plugin 0.3.0 the receiver reports `info.wol`, read from its image's own
  Wake-on-LAN switch. Where it reports `supported: false` — as a receiver whose image has no
  such switch does — both buttons carry a `wake_on_lan` attribute,
  shown in their more-info dialog as „Wake-on-LAN" with a sentence saying that the receiver
  wakes from deep standby only by its remote, its front button or a timer; on „Obudź (WoL)" it
  adds that the packet is still sent. The raw value is `not_supported`, for automations. Where
  the receiver reports `supported: true`, or an older plugin reports nothing, the buttons are
  exactly as they were. Neither button is renamed in any language, so no entity id moves.

### Changed

- **A `message` action without `style` is byte-for-byte the popup it was**, but its `timeout`
  default of ten seconds now comes from the handler rather than the action schema, so that a
  toast without a `timeout` gets its own five seconds rather than the popup's ten.

- **A guided install whose name field was left empty now titles the entry with the name the
  receiver already has**, rather than with the node ID. Nothing is written to the receiver for an
  empty field — a box that has a name keeps it, and one that has none is named after its box type
  by the plugin, as on a first start — so this only changes what Home Assistant calls the entry it
  creates. A name of nothing but spaces counts as empty on both sides, and a name that was typed is
  written and wins as before.

### Fixed

- **One entity that fails on a message no longer stops the others from updating.** Every entity
  of a receiver is told about a message in turn, and an exception in one of them used to skip
  every entity after it for that message and put a traceback in the log on each message for as
  long as the fault lasted. The failing entity is now logged once per run of failures, with its
  traceback, and the repeats in that run go to the debug log; the rest update as usual.

## [0.2.0] - 2026-09-22

The release that makes the receiver usable from Home Assistant: a media player, a remote, an
OSD notify target, the sensors, the switches, the buttons, the two selects and ten actions —
and a guided installer that puts the receiver plugin on a box that has never had it.

0.1.0 set a receiver up and gave it a device page. This one builds everything on top of that
page, and answers the question the first release could not: what happened when a control did
nothing. Every button and every action waits for the receiver and raises the receiver's own
sentence when it refuses; „Ostatni błąd" keeps that refusal after the plugin has cleared its
topic; and the two controls a box can always refuse — deep standby and reboot — appear only
while it says they are permitted.

The guided installer has been run end to end on a receiver that had never had the plugin, and a
rollback exercised for real with a deliberately wrong broker password: the plugin refused, the
receiver was restored to the byte and its interface restarted. The four defects those runs found
are fixed here, and the run after them passed — the rollback reported what had actually happened,
released its lock, and a successful install ended on its own screen. The receiver plugin bundled
with this release is byte for byte the package published as the plugin's own v0.2.0 release.

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

- **A receiver that had announced before could not be installed on from the guided flow.**
  The plugin's announcement is retained, so Home Assistant offers a discovery card for the box
  again at every start and at every reconnect to the broker — and the guided install refused to
  run beside that waiting offer. It aborted with "this receiver is already being added" at the one
  step where two passwords had just been typed. The install now takes the offer down instead of
  giving way to it, so there is one card for one receiver. An announcement that arrives while an
  install is running still stands aside, and adding a box by hand is unchanged. A second guided
  install for a receiver that is already being installed on now waits its turn instead, rather
  than cancelling the first one in the middle of its transaction.
- **A guided install ended on "Invalid flow specified"** instead of on the new receiver or, worse,
  instead of on the reason it failed. Each install phase asked the screen to re-read the flow, and
  re-reading is what finishes it — so a phase reported close to the end of the transaction put a
  second request on the flow at the same moment Home Assistant put its own there. One of them
  finished the install and the other arrived to find nothing left. No phase asks for anything now:
  the **progress bar still moves through all eight of them**, live, and the text beside it is one
  sentence for the whole install rather than a caption that could only change when the screen was
  asked to re-read — which is the thing that has been taken away.
- **A rollback left the OpenWebif hook's compiled copy behind.** This image compiles into the
  legacy location — `MQTTBridge.pyc` beside the source, not in `__pycache__` — and Python imports
  that as a complete module. Undoing a first install therefore restored "there was no hook here"
  and left an importable hook reaching for a plugin that had just been removed. Both locations are
  now backed up and restored as one.
- **Installer snapshots no longer accumulate on the receiver.** Every guided install left another
  full copy of the plugin directory under `/home/root/mqttbridge-backups/`, for ever. A successful
  install now keeps its own snapshot and one more, and removes the rest. Its own is kept by name
  rather than by date, because a receiver without a battery-backed clock boots in 1970 and can
  stamp its newest snapshot as the oldest one there. It only ever touches directories it made
  itself, and a failure to tidy up is never a reason to undo an install that has been verified.
- **The guided installer could not find opkg's database on OpenViX.** Where that database
  lives is a setting, not a constant: OpenViX 6.6 keeps it under `/var/lib/opkg` and says so
  in `/etc/opkg/opkg.conf`, leaving `/usr/lib/opkg` with nothing in it but `alternatives/`.
  The installer looked only in `/usr/lib/opkg`, so the first snapshot of an install failed —
  safely, before anything was touched, but on a receiver that was perfectly healthy — and a
  rollback would have refused for the same reason. Both halves now read `/etc/opkg/*.conf`
  the way opkg does, falling back to `/var/lib/opkg` and then `/usr/lib/opkg` when nothing
  says otherwise, and a database that is genuinely missing is reported with the paths that
  were looked at. Found during the release-gate session on hardware.
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
- **A rollback that had worked was reported as a rollback that had failed, and left the receiver
  locked.** Undoing an install restarts the receiver's interface and then checks that it is
  running — and it checked six seconds after asking, while the interface takes eleven to fourteen
  to come back. So a box that was recovering normally was judged not to have recovered: the
  screen said the receiver could not be put back and named a backup directory, instead of saying
  why the install had failed, and the transaction lock was never released — so the next attempt,
  on a receiver that was in perfect order, refused itself with "another installation is already
  running" until the lock was deleted by hand. The restart is now waited for, for as long as the
  install itself waits for the plugin to announce itself, and the lock is released whatever else
  went wrong: a receiver that has been put back is not one to refuse the next install on, and the
  next install snapshots again before it touches anything. A rollback that restored the files but
  could not bring the interface back is now its own outcome — "restart the receiver by hand" —
  rather than being reported as a receiver that needs inspecting, and a receiver that came back
  but could not be unlocked is a third, naming the lock directory to delete. A rollback
  interrupted by a shutdown now reports itself as interrupted instead of as a broken receiver.
- **A receiver whose image runs a wrapper beside Enigma can be installed on at all.** Both the
  install and its rollback proved a restart by asking for the running Enigma process and refusing
  anything but exactly one — and images that start the interface from a wrapper report two, for
  as long as the box is up. The guided install stopped with "the interface did not restart
  safely" at the step immediately before the only disruptive command, on a receiver with nothing
  wrong with it, and a rollback there waited out its whole timeout before reporting a restart
  that had in fact happened. A restart is now proved by any process that was not running before
  it, which is what a restart means; no process at all beforehand — a box still booting — is an
  ordinary answer rather than a refusal.
- **A failed rollback said so without saying why.** The log line named the backup directory and
  dropped the exception that had caused it, so the only place left to find out what had gone
  wrong was the receiver. Both that line and the one about a transaction lock that could not be
  released now carry the cause, and the rollback's own steps — stopping the interface, restoring,
  restarting, releasing the lock — are logged as they happen, so a bad day leaves a trail without
  debug logging having been on beforehand.
- **"Configured for a different node ID or base topic" now says which, and what.** The sentence
  named neither side, on the one screen where the answer could be read — the flow is gone
  afterwards. An install over a receiver that already had the plugin on it was refused by it, and
  from what was on the screen there was no way to tell whether the node ID or the base topic had
  disagreed, or what the receiver was actually configured for. It now carries the receiver's node
  ID and base topic and the ones the form gave; a value the receiver has never stored is shown in
  brackets, because otherwise the default it is running on and a value it holds look identical.
- **The plugin's defaults are read out of the plugin instead of being remembered twice.** Enigma2
  writes no line for a setting whose value still equals its default, so a receiver left on the
  default base topic has none in its settings file at all — a box measured today stored ten of
  the plugin's twenty-six settings, and neither `base_topic` nor `port` was among them. The
  receiver-side helper papered over that by substituting its own copy of the plugin's defaults,
  which put a second copy of them in a place nothing compared against the plugin and made "the
  settings file says nothing" indistinguishable from "the settings file says `enigma2`" for every
  caller downstream — including the message above, which could not have marked the difference
  even if it had wanted to. The helper now reports what the file holds and nothing else, and the
  defaults live in one table here that a test checks entry by entry against the bundled plugin's
  own `config.py`. What the installer accepts and refuses is unchanged.
- **An install refused before anything was changed left nothing in the log.** No line at any
  level: afterwards there was no way to tell an install had even been attempted, let alone which
  check refused it. Every guard that refuses before the receiver is touched — the Python version,
  free space, a recording, a timer about to start, a newer plugin already installed, a mismatched
  identity, and each of the checks that fail closed on an answer they cannot read — now leaves one
  warning naming the check and the facts it judged, and saying what the receiver was left holding.
  No credential and no address is among them. The no-write credential probe the options screen and
  reauthentication run goes through the same checks and stays silent, because a receiver that
  happens to be recording while a password is being tested is not a refused install.

### Changed

- **The diagnostics download now carries the last payload of every state topic.** The screen
  grab appears as a size and a timestamp, the channel list as its shape, and the remote keys
  not at all: a bug report should answer a question, not describe a household.
- Command cleanup now retires internal waiters without spurious asyncio errors. Clearing an old
  plugin error no longer reports concurrent commands as complete before their own result is
  known.
- **The bundled receiver plugin is the plugin's own 0.2.0 release.** It reports whether the box
  permits deep standby, clears bytecode an upgrade has orphaned, and sweeps compiled bytecode when
  the package is removed — removal used to leave the `.pyc` files the image had compiled beside
  the sources, and the next GUI restart loaded them as a complete module, so a plugin opkg said
  was gone reconnected to the broker anyway. It is built from the tag's own commit
  [`edc7ca6`](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/tree/edc7ca6b37d0bc06ce485613e7a7ecb8f7ee7d3a)
  and is byte for byte the package published on that release, which anyone can check against the
  SHA-256 in `bundled/metadata.json`.
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

- **[ADR-0004](docs/adr/0004-remote-uninstall.md) records the remote-uninstall decision**, for
  0.3.0. Removing the plugin from a receiver becomes an explicit, confirmed action — never a side
  effect of deleting the configuration entry, which still only hands the box back to MQTT
  discovery. The action is offered only while the receiver says it permits removal, a permission
  that is off by default and granted on the box rather than over the broker; it prefers the SSH
  path where the entry kept credentials, because that path can prove the outcome, and falls back
  to the plugin's command otherwise. It is a one-way door — afterwards only SSH or the receiver's
  own package manager can put the plugin back — and the confirmation says so, along with the fact
  that the receiver keeps its settings for a later reinstall. The receiver's half, including the
  order of operations that retracts the retained topics before anything is removed, is ADR-0004 in
  the plugin repository.
- **[ADR-0003](docs/adr/0003-control-feedback-and-household-features.md) carries a dated
  correction: the recorder diagnosis behind decision 5 was wrong.** A long source list was said to
  push the media player's attributes past the recorder's 16 KB limit and cost the entity its
  history. It does not. Home Assistant declares `source_list` — and a select's `options` —
  unrecorded, and the recorder strips every unrecorded attribute *before* it weighs a state
  against that limit; measured through the recorder's own function, a thousand-channel media player
  is 27 620 bytes of raw attributes and 327 stored — a few hundred bytes, with every other
  attribute intact. Nothing was ever dropped from
  history. In consequence `source_list_scope` ships defaulting to **`all`**, so an existing
  installation does not change, `active_bouquet` is opt-in, and the reason for offering it is that
  a dropdown of a thousand rows is not a control. The size-based test was replaced by one that
  routes a real state through the recorder's own encoder and asserts the attributes survive.
  Decision 10's requirement — the EPG sensor's own attribute really is recorded unless excluded —
  is unaffected and now says why in as many words.
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
  platform for bouquet and channel, a source list that can follow the active bouquet, and a button
  for the EPG grid. 0.3.0 follows the receiver: a second notify entity for the
  discreet toast, softcam and EPG-import controls gated on box-side permissions, and an EPG sensor
  whose payload is declared unrecorded. The 0.2.0 fixes above are this release, against that plan;
  the README's roadmap says which of them are done.

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

[Unreleased]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/releases/tag/v0.1.0

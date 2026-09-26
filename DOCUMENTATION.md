# Enigma2 MQTT - documentation

The full guide to the Home Assistant side of **Enigma2 MQTT Bridge**. The box side - the
plugin, its setup screen and the topic contract - is documented in the
[enigma2-mqtt-bridge](https://github.com/deltasystems-pl/enigma2-mqtt-bridge) repository.

This document grows with the integration. Sections that describe something not written yet
say so; nothing here is speculation about behaviour that has never run.

---

## 1. Concepts

**One receiver is one device.** Everything the integration creates hangs off a single device
identified by the plugin's `node_id` - `<boxtype>_<mac6>`, for example `vuuno4kse_005301`,
stable across reinstalls of the plugin and of Home Assistant.

**The broker is the only channel.** The plugin publishes state under
`<base_topic>/<node_id>/` (default base topic `enigma2`) and subscribes to
`<base_topic>/<node_id>/cmd/#`. The integration never talks to OpenWebif; the only other
connection it ever opens is SSH, and only when you ask it to install or update the plugin, or
to verify a removal you asked for.

```
Enigma2 box                         MQTT broker                    Home Assistant
  MQTTBridge plugin  --publishes-->  enigma2/<node_id>/... ------->  enigma2_mqtt
  (hooks inside enigma2)            enigma2mqtt/discovery/...       (this repository)
                     <--commands--  enigma2/<node_id>/cmd/#
```

**Push, not poll.** State topics are retained, so Home Assistant has the full picture the
moment it subscribes, and every later change arrives as the enigma2 event that caused it.
Availability is the MQTT last will: within about 45 seconds of a box disappearing its
entities go `unavailable`, and they come back with a full snapshot when it reconnects.

**Two modes.** `ha_mode = discovery` lets the core MQTT integration build entities from the
plugin's discovery payloads - useful without this integration at all. `ha_mode = integration`
hands that job to this integration, which can express what discovery cannot. Adding a box
here switches it and makes the plugin retract its discovery payloads first, so nothing is
created twice.

## 2. Installation

**Requirements.** Home Assistant 2026.3 or newer; the MQTT integration configured and
connected (the Mosquitto add-on is fine); and the
[enigma2-mqtt-bridge](https://github.com/deltasystems-pl/enigma2-mqtt-bridge) plugin on the
receiver - which the integration can put there over SSH.

**HACS.** HACS -> the three-dot menu -> *Custom repositories* -> add `deltasystems-pl/hass-enigma2-mqtt`,
category **Integration**; install **Enigma2 MQTT** and restart Home Assistant.

**Manual.** Download `enigma2_mqtt.zip` from the
[latest release](https://github.com/deltasystems-pl/hass-enigma2-mqtt/releases/latest), extract
it into `config/custom_components/enigma2_mqtt/` and restart. A checkout of this repository
works too: copy `custom_components/enigma2_mqtt` into your `config/custom_components/`.

[§3 Configuration](#3-configuration) covers what the flow asks.

**Versions.**

| Integration | Plugin | Status |
|---|---|---|
| 0.3.1 | 0.3.0 | current release |
| 0.3.0 | 0.3.0 | superseded |
| 0.2.0 | 0.2.0 | superseded |
| 0.1.0 | 0.1.0 | superseded |

The integration ships the plugin it was built against, so the two move together. The bundle in
this release is byte for byte the package on the plugin's own
[releases page](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases/tag/v0.3.0), and
`custom_components/enigma2_mqtt/bundled/metadata.json` carries the SHA-256 to check it with.
The integration refuses nothing when the versions differ, but the `update` entity tells you
when the box runs a plugin older than the one this release was written against.

🔴 **Never keep a backup copy of this component inside `custom_components/`.** Home Assistant
reads the `manifest.json` of every directory it finds there, and a copy declares the same
`enigma2_mqtt` domain as the original - so which one is loaded is not decided by the name, the
date or the order you would expect. A copy called something like `enigma2_mqtt.bak-2026-09-19`
is worse still: it can win, and then it cannot be imported at all, because that is not a legal
Python module name. The symptom is `Setup failed for custom integration 'enigma2_mqtt'` with a
`ModuleNotFoundError` naming the backup, and every entity of the receiver going unavailable -
from a directory that was only ever meant to be a safety net. Keep upgrade backups anywhere
else: `/config/enigma2_mqtt-backups/` is one directory up and out of reach of the scan.

## 3. Configuration

The config flow has three entries.

**Discovered.** The plugin publishes a retained announcement on
`enigma2mqtt/discovery/<node_id>/config`, and the integration's manifest subscribes to that
prefix, so a configured box appears by itself in *Settings -> Devices & Services*. The card
names the box, its type and its image. Confirming publishes `cmd/ha_mode = integration` and
waits up to ten seconds for the plugin to echo the new mode back on `info`; only then is the
entry created. A box that does not answer leaves the form up with an error and the confirm
can simply be pressed again - nothing has been changed on the Home Assistant side.

**Manual.** *Add integration -> Enigma2 MQTT* asks for the **base topic** (the plugin's
default is `enigma2`), the **node ID** from the plugin's setup screen, for example
`vuuno4kse_005301`, an optional **name**, and an optional receiver hostname or IP address.
That address only supplies the device-page link and a later SSH suggestion; it is neither
probed nor required, so MQTT-only and topic-rewriting bridges remain supported. The flow subscribes to
`<base topic>/<node ID>/info` and waits up to ten seconds; because that topic is retained, a
box that is on the broker answers at once and one that is not reports "no receiver was found
on this topic". It then switches the mode exactly as the discovered path does. This entry is
for a box behind an MQTT bridge that rewrites the topic prefix, or for one whose announcement
never arrived.

**Install the plugin from here.** *Add integration -> Enigma2 MQTT -> Install MQTT Bridge over
SSH* reads the receiver's SSH host key, shows its fingerprint, and only then asks for the SSH
password and the broker settings it will write onto the box.

**The name field, left empty, changes nothing on the receiver.** The install writes no name, so a
receiver that already has one keeps it, and one that has none is named after its box type by the
plugin, as on a first start. The Home Assistant entry is then **titled with the name the receiver
already has** rather than with the node ID. A name that was typed is written and wins; a field
holding nothing but spaces counts as empty.

🔴 **Give the receiver a broker login of its own.** The broker password ends up in a file on the
receiver's own flash, and most Enigma2 images answer SSH with the image's default root password
- so a login shared with Home Assistant is the whole broker, one box away. On a standalone
Mosquitto these lines confine it to one receiver:

```
user enigma2-<node_id>
topic readwrite enigma2/<node_id>/#
topic write enigma2mqtt/discovery/<node_id>/#
topic write homeassistant/device/<node_id>/#
topic write homeassistant/device_automation/<node_id>/#
```

The **Home Assistant Mosquitto add-on does not enforce an `acl_file`**: it accepts the file and
never asks it, because its authentication plugin answers "superuser" for every login and the
chain stops at the first allow ([home-assistant/addons#4721](https://github.com/home-assistant/addons/issues/4721)).
A dedicated login there still names the receiver in the broker log, which is worth having - but
it does not confine it, and a documented ACL that enforces nothing is worse than none, because
the next person to read it believes it. Until the add-on gains ACL support, treat a receiver on
it as able to publish anywhere on that broker.

**What the install leaves on the receiver.** The plugin, and the provisioning file
`/etc/enigma2/mqttbridge.json` - mode 0600, because it holds the broker password. Before anything
is changed the installer takes a snapshot of everything it is about to touch into
`/home/root/mqttbridge-backups/ha-installer-<nonce>/`, mode 0700; that is what a rollback restores
from, and it is what to reach for to put a receiver back by hand later. A successful install keeps
**its own snapshot and one more**, and removes the rest, so the directory does not grow with every
install. Its own is kept by name rather than by timestamp, because a receiver without a
battery-backed clock can stamp it before the time it was actually taken. A failed install leaves
its snapshot behind as well; from then on it is an ordinary one, and it goes once two newer ones
exist. The uploaded package, the manifest and the helper script live in `/tmp` and are deleted when
the transaction commits. A restart that has to be undone uses two more places: a rollback
that puts the plugin's settings back runs as a script from `/tmp/enigma2-mqtt-r2-<nonce>/`,
removed once it has been seen to finish. Putting the plugin directory back builds it first in
`/usr/lib/enigma2/python/.mqttbridge-staging-<nonce>` and swaps it in by renames, while the
directory it replaces waits in `.mqttbridge-aside-<nonce>` until the swap is over. Both sit beside
the `Plugins` directory, never inside it, so enigma2 cannot load either as a second copy of the
plugin. Nothing else is written.

**How the install restarts the receiver.** The plugin only loads at the interface's start, so
every install and update ends with a restart of the Enigma interface, and it keeps the channel
the household is watching:

- The installer asks OpenWebif on the receiver for the image's own restart (power state 3). The
  image saves its settings on the way down - the channel being watched among them - and init
  starts it again. The installer judges this by the interface's process id changing within 60
  seconds, never by OpenWebif's answer, which the restart can cut off.
- If the process id has not changed after 60 seconds, the image is asking on the television
  whether to restart - it does while timeshift runs or a background job works. The installer does
  not force it: it puts the previous plugin's files and package records back under the running
  interface, leaves the plugin's settings alone, and ends with a sentence saying so. The question
  may stay on the television; either answer is safe, because the files are back before the
  sentence is shown. A receiver with timeshift permanently on will ask every time. For those
  up to 60 seconds the plugin that is running has the new files on disk, and a part of it that
  it loads for the first time in that window is the new version's - a bounded window,
  accepted; with the old `init 4` restart it was a few seconds.
- After the restart the installer compares the channel and the standby state with what they were
  before, zaps back once if the image started on another channel - never over a channel somebody
  picked after the start - and puts the channel-list bouquet back through the plugin where that
  cannot change the channel. The log has one line with the outcome.
- A rollback that has to put the plugin's settings back stops the interface instead, because a
  running interface writes its settings over them when it quits. It runs as one script on the
  receiver - record the channel, stop, restore, write the channel, start - which Home Assistant
  starts detached and follows, so a lost connection cannot leave the receiver stopped.

An install is refused before anything is changed while the receiver is **in standby** (a restart
would wake it, and with HDMI-CEC the television) or **streaming** to another device.

**When an install fails.** Every failure ends on a sentence rather than a code, and the sentence
says what state the receiver was left in. Most of them - no space, a recording running, a bad
password, the plugin never announcing itself - mean the receiver was put back exactly as it was
and there is nothing to do but fix the cause and press install again; the snapshot the run took
stays behind as evidence and is pruned by the next two successful installs. Two are different:

- *„...the receiver was put back as it was, but its interface did not start again. Restart the
  receiver by hand."* The files, the plugin settings and the opkg database are back; only the
  Enigma interface did not come up within two minutes of being told to. Power-cycle the receiver
  or start it over SSH (`init 3`), then install again. Nothing needs undoing first.
- *„...the receiver could not be put back as it was. Check the receiver by hand."* This one names
  `/home/root/mqttbridge-backups`, and it is the only outcome that asks you to look at the box:
  the restore itself did not complete, so the receiver may be part-way between the two versions.
  The named `ha-installer-<nonce>` directory holds everything the install was about to change.
- *„...but the installer's transaction lock could not be released."* The receiver is back as it
  was and the only thing left behind is the lock that keeps two installs off one box. Delete
  `/home/root/mqttbridge-backups/.ha-installer.lock` over SSH, or wait thirty minutes for it to
  be judged stale; until then the next attempt is refused with "another installation is already
  running".
- *„...the receiver's package manager (opkg) is busy..."* Somebody is installing or removing a
  package from the receiver's own menu, or its update check is running. Before a snapshot this
  means nothing was changed: wait a minute and install again. During a rollback it means nothing
  was restored, and the receiver may be on either plugin: on the new one if the install got as
  far as installing it, on the old one if the same opkg run had already made the install's own
  `opkg install` fail on the lock - the likelier case. Check it by hand as for the outcome above,
  once opkg has finished.
- *„...the receiver was put back as it was, but another run of its package manager ... may have
  changed the package database at the same time."* The restore finished, but opkg's lock file
  was replaced while the restore held it, so an opkg run may have overlapped it. The files are
  back; check what opkg now records for the plugin (`opkg status
  enigma2-plugin-extensions-mqttbridge`) before installing again.

**opkg's own lock.** The snapshot and the restore take the lock opkg itself takes - the file
`option lock_file` names in the receiver's opkg configuration, or `/run/opkg.lock` and
`/var/lock/opkg.lock` when it names none - and wait for it up to 20 and 40 seconds respectively,
inside the commands that run them. That keeps the two from holding it at once and no more: opkg
reads its status file before it locks, so an opkg run started just before a restore can still
work from the status it read. It matters for an install from the receiver's menu during a
guided install, far less for the image's daily package-list update.

The installer releases its transaction lock on the way out of every other failure, including one where the
restore itself did not finish, so a receiver is not left refusing installs because a recovery
went wrong. The Home Assistant log carries the reason for every step that failed, which the abort
screen has no room for, and the rollback's own steps are logged as it takes them.

**A refusal before anything is changed** - the receiver's Python is too old, it is short of
space, it is recording, a timer is about to start, it already runs a newer plugin, or its plugin
settings say it is another box - leaves a warning in the Home Assistant log naming the check and
what it measured, beside the sentence on the screen. The screen is gone as soon as it is read;
the log line is what is left to look at afterwards. The receiver keeps its own state through all
of it: no snapshot is taken, no transaction lock is claimed, and nothing of the plugin is
touched. A refusal that happens after the identity is read can leave the installer's own helper
script in `/tmp`, under a name of its own, which the next install replaces and a reboot clears. A
discovery card the box was already being offered on is still waiting afterwards, because nothing
ran that would have consumed it - measured across twenty-five samples of one refused install.

A box that has announced itself is usually being offered on the discovery card at the same time.
Installing over SSH takes that offer down as soon as the credentials are submitted, so there is one
card for one receiver rather than two.

The node id is the entry's unique id, so the same box cannot be added twice by either path,
and a box that renames itself updates the entry it already owns.

**Installing over a plugin that is already there.** Reinstalling, and upgrading through the
guided flow rather than the update entity, are ordinary things to do - after deleting an entry
and starting again, or to move a receiver to another broker. Before it writes anything the
installer reads the plugin settings the receiver already holds and compares the **node ID** and
the **base topic** with the ones on the form. They have to be the same box: re-provisioning a
receiver that belongs to somebody else's Home Assistant would take it over silently, and that is
what this refuses. Nothing else is compared, so a different broker, a different name or a
different password is an ordinary change the install goes on to make.

A receiver whose plugin has **never been configured** stores no node ID - the plugin derives
`<boxtype>_<mac6>` on its first start and writes it then - and it is provisioned with the one the
form gives, because there is nothing there to take over. A setting the receiver has never been
moved off is not in its settings file at all, which is not the same as a difference: enigma2
writes no line for a value that still equals its default, so a box on the default base topic
`enigma2` says nothing about its base topic. The comparison supplies the plugin's defaults for
anything the receiver has not stored. When it does refuse, the message names both sides, and a
value in brackets is one the receiver has no line for, and what is inside the brackets is the
default that applies in its place; a value stored as empty is shown as `""`.

The **update entity** compares more, because it writes no settings and therefore has to find the
box already correct: the node ID, the base topic, that the plugin is enabled, and that it is in
`integration` mode.

### Options

*Settings -> Devices & Services -> Enigma2 MQTT -> Configure.* Three preferences, all about this
end of the link rather than about the box; saving them reloads the entry, so a change takes
effect without a restart.

On a receiver that permits its own removal, *Configure* opens on a menu of two entries instead:
**Options**, which is this form, and **Remove the plugin from the receiver** („Usuń wtyczkę z
dekodera"), described in [§8](#removing-the-plugin-from-the-receiver). Every other receiver -
and that is every receiver as shipped, because the permission is off by default - opens straight
on the form, as before.

| Option | Default | What it does |
|---|---|---|
| **Show the deep standby and reboot buttons** | off | The Home Assistant half of the gate on „Głębokie uśpienie" and „Restart"; the receiver's own `deep_standby_allowed` is the other half, and [§4.5](#45-buttons-and-update) says how the two combine. Turning this off removes the buttons from the entity registry rather than leaving them behind unavailable. It is about what appears on a dashboard, **not** a safety mechanism: the plugin refuses both commands while a recording is running whatever is set here. |
| **Wake-on-LAN MAC address** | the address the box reports on `info` | The target of the magic packet the „Obudź (WoL)" button, `media_player.turn_on` and `remote.turn_on` send. Set it when the receiver reports a different interface from the one that is plugged in - a box on Wi-Fi does not answer a packet sent to its cable port. Written as `00:00:5e:00:53:01`, `00-00-5e-00-53-01`, `0000.5e00.5301` or `00005e005301`, in any case; it is stored lower-case and colon-separated whichever you type, and anything that is not an address fails the form rather than failing later from inside `wake_on_lan`. Whichever address a packet would go to - the override, or the one the box reports - is registered on the device as its MAC connection, so the rest of Home Assistant knows it too. On current Home Assistant that connection does not merge devices across integrations, so a router or DHCP integration may still show a second card for the same receiver; it is there so that what *is* keyed on a MAC can find this one. A value stored by an earlier release that is not an address is dropped once at startup, with a line in the log. |
| **Bouquets to offer** | every bouquet the box publishes | Which bouquets feed the media player's channel list and the media browser. The choices are the bouquets on the `channels` topic, and a name can be typed for one the box has not published yet. This narrows the plugin's own `bouquets_for_select`; it cannot widen it. |
| **Channels in the media player's source list** | every bouquet on offer | What `media_player.source_list` holds, and nothing else. **Every bouquet on offer** is what this integration has always done and stays the default, so an existing installation does not change under an automation that names a channel; on a receiver with about a thousand channels it is a dropdown of about a thousand rows. **The active bouquet** is the short list the receiver's own channel ± is walking, which is also what the „Kanał" select shows. `select_source` follows this setting; the `zap` action takes a service reference and does not. Three cases keep the long list whatever is chosen, because there is nothing to shorten it to and an empty source list would leave no way to change channel: a receiver that publishes no channel-list context (an older plugin without `bouquet_context`), a context naming a bouquet the **Bouquets to offer** option excludes, and a context naming a bouquet with no playable channel in it. The last two are logged as a warning, once per bouquet. 🔴 This setting is **not** about the recorder: Home Assistant declares `source_list` an unrecorded attribute and the recorder removes it before it measures a state against its size limit, so the long list never reached the database in the first place. |
| **Bouquets hidden from "Recently watched"** („Bukiety ukryte w „Ostatnio oglądane"") | none | Which bouquets' channels [„Ostatnio oglądane"](#46-selects) leaves out - and that one entity only: „Ostatnio oglądane (wszystkie)", „Kanał", „Bukiet" and the media player are untouched. The choices are the bouquets on the `channels` topic, and a name can be typed. With it empty every entry shows. With a bouquet chosen, an entry shows only when its bouquet is a published bouquet that is not hidden **and** its channel is not a member of any hidden bouquet, so a channel of a hidden bouquet reached through another one stays hidden. While anything is chosen, an entry that cannot be checked - no bouquet path, a radio bouquet, a bouquet the plugin does not publish - is hidden too. A hidden name that is no longer on the `channels` topic is logged once. 🔴 **This is a filter in Home Assistant, not privacy on the network**: the receiver publishes every entry of its history on `zap_history` and every channel of every bouquet on `channels`, retained, to any broker login; its own History Zap screen, the plugin's OpenWebif page and „Ostatnio oglądane (wszystkie)" show everything. See [§4.6](#46-selects) for what the recorder keeps. |
| **Check for published plugin releases** | off | The only thing this integration can do that is not talking to your own broker, which is why it is off. Turned on, the version entity asks the plugin repository which release is published - **at most once every 24 hours** - and reports the tag in its summary, in a `published_version` attribute and in the release link. The time of the last request and its answer are written to `.storage/enigma2_mqtt.release_check`, keyed by config entry, so reloading the receiver, saving the options or restarting Home Assistant shows what is already known instead of spending another request; the record is deleted when the receiver is removed. At most 64 KiB of the answer is read, and a tag is only believed if it is at most 64 characters and parses as a version. The release link is only followed if it points into this plugin's own releases. It downloads nothing and it never raises `latest_version`: `install` can only ever put the bundle shipped here on a receiver, and offering a version the installer would refuse would be a button that lies. Failures - a rate limit, a timeout, an answer that is not a release - are a debug line and nothing else. |

When a recent plugin advertises its configurable publishers, the same form also controls key
events, screenshot mode and interval, and the delay before an on-zap screenshot. Older plugins
simply omit controls they do not support. Enabling conditional-access telemetry adds four
diagnostic entities for system, encryption, fresh ECM activity and ECM time. Values may be
unknown, association on multi-tuner receivers is best effort, and server, account and card
details are never accepted into integration state or diagnostics.

Only a stated „off" takes these entities away again. A receiver that stops advertising the
capability - an older plugin, a reload before it has answered, a box that is simply not
there - is saying nothing, and nothing leaves the per-source OSCam entities exactly as they
are, with the names, areas and history they have been given. Switch the option off to remove
them.

The same form carries the two **softcam auto-heal** fields where the plugin advertises
them. *Restart the softcam on its own when decoding stops* is off by default; with it on,
the receiver restarts its own card-sharing client when the channel is encrypted and has
not been decoding for the window in the second field - at most once every ten minutes, and
never while a recording is running or due. That is the identical guard a manual press goes
through, deliberately: a restart landing on the opening seconds of a recording is worse
than a scrambled recording, because a scrambled one is recoverable and a truncated one is
not. The window defaults to ninety seconds, takes 30 to 600, and starts again at every
channel change; a healthy encrypted channel renews about every ten seconds, so a short
window reports a fault on a receiver that is working.

🔴 **The permission behind both is not on this form and cannot be.** Whether the receiver
will restart its softcam at all is `softcam_restart_allowed`, set on the box under *Menu ->
Plugins -> MQTT Bridge*. A setting that *enables* a command is deliberately outside what
anything holding publish rights on the broker can reach, so the plugin refuses it on
`cmd/config`; these two only tune a command it has already been permitted to run.

### Reconfigure

*...-> Enigma2 MQTT -> the receiver -> Reconfigure.* This is for following a box whose **node ID**
or **base topic** was changed on the plugin's own setup screen. The flow checks that a box
really answers on the new topic before it saves anything.

🔴 **A changed node ID is a changed identity.** Every unique id is `<node_id>_<key>`, so a box
with a new node ID is, as far as Home Assistant can tell, a different device doing the same
job. The old device is removed - with its entities and their history - and the reload builds
the box again under its new name. Changing only the base topic or the display name keeps
everything.

## 4. Entities

**Twenty-five entities on every receiver**, and thirty-two more that exist only while
something says they should - plus one set of three per OSCam source, which has no fixed
number because it follows however many readers and servers that receiver has. The
conditional ones are, in full:

| How many | What | Created while |
|---|---|---|
| 2 | „Bukiet" and „Kanał" | the receiver reports the `channels` **and** `bouquet_context` capabilities |
| 1 | „Odśwież EPG" | the receiver reports the `epg_grid` capability |
| 1 | „EPG &ndash; aktywny bukiet" | the receiver reports the `epg_grid` **and** `bouquet_context` capabilities |
| 1 | „Softcam" | the receiver reports the `softcam` capability |
| 1 | „Import EPG" | the receiver reports the `epg_import` capability |
| 1 | „Ekran &ndash; dyskretnie" | the receiver reports the `toast` capability |
| 5 | the enigma2 process diagnostics | the receiver reports the `process` capability |
| 2 | „Głębokie uśpienie", „Restart" | the Home Assistant option asks for them **and** the receiver permits deep standby |
| 1 | „Restart softcam" | the receiver reports the `softcam` capability **and** permits a softcam restart |
| 1 | „Pobierz EPG" | the receiver reports the `epg_import` capability **and** permits an EPG import |
| 4 | the conditional-access diagnostics | the `cam_telemetry` option is on |
| 12 | the OSCam aggregates | the `oscam_telemetry` option is on |
| 3 per OSCam source | status, ready cards, shared cards | the `oscam_telemetry` option is on, for each reader or server that receiver reports |

**What takes one away again is not the same question as what creates it**, and the table
splits on it. A row that follows an **option or a permission** is removed on a stated
„no": somebody decided, at the television or on the options form, and an entity that can
never say anything again is worse than none. A row that follows a **capability** is
removed only where the control behind it would be permanently refused - „Odśwież EPG" on
a receiver that builds no grids. „Softcam", „Import EPG", „EPG &ndash; aktywny bukiet" and the five process
diagnostics are neither: they are sensors with a history, and a capability that stops being named is an older
plugin after a downgrade, a hook that failed to attach on one boot, or a receiver that has
not answered yet. None of those is a decision, so they stay and say nothing until the
topic returns. „Ekran &ndash; dyskretnie" stays for the same reason: automations notify it by name,
and a receiver that stops naming `toast` - a downgrade, a skin reload whose rebuild of the
toast screen failed - has decided nothing. Sending to it then is refused in Home Assistant
with the reason, rather than published into a refusal on `last_error`.

Unique ids follow one scheme: `<node_id>_<key>`, where the key is the English translation key
of the entity, and they are the same in every language. **Entity ids are not.** Home Assistant
makes an entity id from the entity's name in the installation's language when the entity is
first registered: the channel sensor is `sensor.dekoder_salon_kanal` on a Polish installation
and `sensor.dekoder_salon_channel` on an English one. That holds for the languages Home
Assistant lists as making native entity ids - Polish, German and English among them; in any
other language the id comes from the English name. An entity id that exists keeps its
name when the language changes later. Look the ids up on the device page before you copy an
example from this document. The decision and what it costs are
[ADR-0007](docs/adr/0007-entity-ids-follow-the-installation-language.md).

Two rules run through the table. **An entity is unavailable when the box is offline, and also
when the topic it reads has never arrived** - an image that gave the plugin no tuner hook
publishes no `tuner` topic, and three sensors that say nothing are better than three that
invent a zero. **The media player and the wake button are the exceptions**: they stay usable
while the box is unreachable, because that is precisely when somebody wants to wake it.

### 4.1 Media player

| Key | Polish name | unique_id | Topics |
|---|---|---|---|
| (the device itself) | *Dekoder salon* | `<node_id>_media_player` | `power`, `service`, `epg`, `volume`, `screen`, `channels`, `bouquet` |

- **State** - `playing` when `power` is `on` and the box is online, `off` otherwise. Standby
  and deep standby are both `off`: `MediaPlayerState.STANDBY` was deprecated in Home Assistant
  2026.8, and what the household sees is a dark television either way. The „Zasilanie" switch
  and the device's availability tell the two apart for anyone who needs it.
- **Source** - the current channel name; the source list is the channels of every bouquet the
  options offer, in bouquet order, with a duplicate name listed once. The
  [source list scope option](#options) narrows it to the bouquet the receiver is on. Selecting
  one publishes `cmd/zap {"name": ...}` when that name is unique across the offered bouquets, and
  the service reference when it is not - the plugin refuses an ambiguous name, and rightly, but
  a person picking a name off a list has already made the choice it is refusing to make, and
  where the list is scoped the copy on it is the one sent. A name the receiver has but the list
  is not showing is refused with both ways on: switch „Bukiet", or call `zap` with a service
  reference, which is not scoped.
- **Source is not promised to be in the source list.** In the scoped setting the receiver can
  perfectly well be tuned to something outside the bouquet it is on - somebody typed a channel
  number on the remote. `source` and `media_channel` still say what is playing, because what is
  on is a fact and the length of a dropdown is a preference. Use `media_content_id`, which is
  the service reference, for anything that has to be exact.
- **Browsing** - bouquets, then channels, each with the picon the box's own web interface
  serves at `http://<ip>/picon/<sref>.png`. Picons are not published over MQTT; no address on
  `info` simply means no thumbnail.
- **Playing** - `media_content_type: channel` takes a service reference, and `channel_name`
  takes a name. The browser sends the first.
- **Volume** - set, step and mute, on the box's own 0-100 scale underneath.
- **Turning on** - `cmd/power on` when the box is listening, a Wake-on-LAN magic packet when
  it is not; the packet wakes nothing from deep standby on a receiver that reports it cannot be
  woken over the network ([§4.5](#45-buttons-and-update)). `remote.turn_on` does the same.
  Turning off is always `cmd/power standby`; nothing here sends deep standby.
- **Picture** - the retained `screen` JPEG is both the media image and the entity picture, and
  its hash changes with every frame so the browser fetches the new one.
- **Position** - from `epg.now`, so the progress bar is the programme, not a stream.

### 4.2 Remote, notify, event, image

| Key | Polish name | unique_id | Source |
|---|---|---|---|
| `remote` | *Pilot* | `<node_id>_remote` | `power` / `cmd/key` |
| `osd` | *Ekran OSD* | `<node_id>_osd` | `cmd/message` |
| `osd_toast` | *Ekran &ndash; dyskretnie* | `<node_id>_osd_toast` | `cmd/message` with `style: toast` · only while the receiver names the `toast` capability |
| `key` | *Pilot &ndash; klawisz* | `<node_id>_key` | `key` |
| `screen` | *Ekran* | `<node_id>_screen` | `screen` |

- **Pilot** - `remote.send_command` takes `KEY_RED` and `red` alike; spaces are dropped, since
  the Linux names run the words together (`channel up` is `KEY_CHANNELUP`). `hold_secs`
  greater than zero sends a long press, `delay_secs` spaces a sequence out, `num_repeats`
  repeats it. A key name this integration has never heard of is passed through: the box is the
  side that knows which keys it has, and it answers on `last_error` when it does not.
  **Hidden on the device page of a new installation.** Home Assistant gives every remote entity
  a power toggle, so the device showed three controls that all switch power - this one,
  „Zasilanie" and the media player - and no way to tell which was the real one. „Zasilanie" is
  the labelled one. Hidden is not disabled: the entity exists, has a state and answers
  `remote.send_command` from an automation. An installation that already has this entity keeps
  whatever visibility it was given; un-hide it in the entity's settings to put it back on the
  page.
- **Ekran OSD** - `notify.send_message` puts a popup on the television. The popup has one text
  field, so a title becomes the first thing in it rather than being dropped; the text is cut
  at 500 characters, where the plugin cuts it.
- **Ekran &ndash; dyskretnie** - the same message as a **toast**: a small overlay in a corner of the
  screen, headed „MQTT Bridge", that takes no key press, hides itself after five seconds and is
  replaced by the next one. Nothing waits in the receiver's queue behind the channel list, and
  nothing interrupts whoever is holding the remote. The title is folded in the same way, and
  the text is cut at 200 characters, where the plugin cuts a toast. The receiver shows no toast
  in standby and refuses one sent then. The entity exists only once the receiver names the
  `toast` capability, which the plugin claims after the screen has actually been built - a
  receiver with the plugin's `osd_toast` setting off, or an image where the screen could not
  be built, gets popups only. The entity stays if the receiver later stops offering toasts, and
  a send to it then raises an error, which stops an automation at that step; put
  `continue_on_error: true` on the step to let the automation carry on without the toast.
- **Pilot &ndash; klawisz** - fires for every key the box reports, with the key name as the event
  type and `press` (`short` or `long`) as an attribute. An event entity may only fire types it
  declared, so a key outside the declared list is logged at debug and dropped here - the bus
  event below still carries it.
- **Ekran** - the last screenshot, with the moment it was taken as the state. It is a
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
| `epg_active_bouquet` | *EPG &ndash; aktywny bukiet* | `<node_id>_epg_active_bouquet` | `bouquet`, `epg_grid/<bouquet_slug>` | how many channels of the active bouquet have a programme now or next · no unit · attribute `channels` (**not recorded**) · only while the receiver names the `epg_grid` and `bouquet_context` capabilities |
| `softcam` | *Softcam* | `<node_id>_softcam` | `softcam` | the selected cam binary · diagnostic · only while the receiver names the `softcam` capability |
| `epg_import` | *Import EPG* | `<node_id>_epg_import` | `epg_import` | `idle`, `running`, `done` or `failed` · diagnostic · attributes `started`, `finished`, `events`, `error` · only while the receiver names the `epg_import` capability |
| `last_error` | *Ostatni błąd* | `<node_id>_last_error` | `last_error` | the refused command's name · diagnostic · attributes `error` and `time` |
| `process_memory` | *Pamięć Enigma2* | `<node_id>_process_memory` | `process` | MiB · `data_size` · diagnostic · **on by default** |
| `process_memory_peak` | *Pamięć Enigma2 (szczyt)* | `<node_id>_process_memory_peak` | `process` | MiB · the high-water mark since the process started · diagnostic · **disabled by default** |
| `process_threads` | *Wątki Enigma2* | `<node_id>_process_threads` | `process` | count · diagnostic · **disabled by default** |
| `process_open_files` | *Otwarte pliki Enigma2* | `<node_id>_process_open_files` | `process` | count of open file descriptors · diagnostic · **disabled by default** |
| `process_started` | *Start Enigma2* | `<node_id>_process_started` | `process` | a timestamp · diagnostic · **disabled by default** |

**Ostatni błąd** is the one sensor whose memory belongs to Home Assistant rather than to a
topic. The plugin clears `last_error` on the next command that succeeds, so by the time
somebody asks why a button did nothing, the next volume step has usually wiped the evidence.
This sensor keeps it: a cleared topic does not clear it, and it is restored with its text and
its time across a reload and a restart. The state is the failed command - `deep_standby`,
`zap` - and `unknown` until something fails; `error` is the receiver's own sentence, cut at
255 characters, and `time` is the receiver's own `ts`, falling back to the moment Home
Assistant learned of it when the payload has none. Taking the receiver's timestamp is what
makes the replay harmless: the retained complaint arrives again on every reconnect and at
every start-up, and a clock reading would walk the time forward each time. There is no clear
button in 0.2.0: the next error replaces it.

It is also the one entity of this device that **stays available while the receiver is not**.
Deep standby is the headline case and it is precisely a box that has left the network; an
entity that went unavailable with it would hide the explanation at the moment it was wanted,
and a restart taken in the meantime would lose it for good.

The five `process` sensors exist only when the plugin announces the **`process`** capability -
an older plugin, or an image that would not let it hook the measurement, simply has none of
them. They are created whenever that capability arrives, including long after Home Assistant
has finished setting the integration up, which is the normal case on a real receiver. They are
never removed for a capability that stops being named: that is a downgraded plugin or a box
that has not answered yet, not a decision, and deleting them would take somebody's renames,
areas, dashboards and history with them. Switch them off in the entity registry instead.

`process_memory` is the one of the five that is **on** by default. The others are looked up
once something is already wrong, so they cost a household nothing until they are wanted; a
resident set size over weeks is the opposite - the recorder cannot go back and collect it after
the question has been asked. Every field of the topic may be `null`, and a null is `unknown`
rather than a zero: a process with no threads and one that started at the epoch are both
readings, and neither is what "the plugin could not measure this" means.

**Softcam** is the state of the card-sharing client the *image* starts, which is not the
same thing as the conditional-access diagnostics above: those are about the channel being
descrambled, this is about the program doing the descrambling. The state is the **binary
the image selected for autostart** - `OSCam_00000-r000`, say - and not a family name and
not the protocol it speaks outward, which are three different things that a support
thread will otherwise spend an afternoon confusing.

| Attribute | What |
|---|---|
| `running_instances` | how many **instances** are running, counting a supervisor and the worker it keeps as one. A healthy receiver reports **1**; more than one is the fault this exists for. `unknown` means the count could not be taken - it is never reported as `0`, because no instances at all is a real and very interesting reading |
| `last_restart` | when this integration's button or the receiver's own auto-heal last restarted it, ISO 8601 |
| `last_restart_reason` | `manual` or `autoheal` |
| `restarts_today` | restarts since local midnight. It lives in the receiver's memory and the topic is retained, so after a receiver reboot this shows the last published number until the plugin publishes again: it is a counter, not a durable total |
| `manager_check_on_start` | whether the image's own softcam liveness check will add a copy at every interface restart on this box. This is the difference between a receiver that needs the restart button and one that merely has it |
| `manager_timer_minutes` | the image's periodic check interval when it is switched on, `unknown` when it is not. A receiver with both this and `manager_check_on_start` gains an instance every interval, for ever - which is the runaway worth seeing on a dashboard before it becomes a household symptom |

**Import EPG** is what the receiver's own EPG-Importer is doing, **whoever started it** -
„Pobierz EPG", the image's daily schedule or the importer's own screen. The plugin looks at the
importer every minute while it is idle and every two seconds while it runs, so an import the
image started shows as `running` too, and a press refused as „already running" is never
unexplained.

| Attribute | What |
|---|---|
| `started` | when the run began, ISO 8601 - for a run the plugin did not start, when it first saw it |
| `finished` | when the importer says it finished, ISO 8601. After a restart of the receiver it is the importer's own record of the last run |
| `events` | how many events the importer **processed** in that run - not how many were new to the guide. A run one day after the last adds days at the far end of the guide, not events in the next few hours |
| `error` | why it failed, for a person, cut at 255 characters |

`failed` has only three causes the plugin can see: the import did not start, it finished with
**no events** (the importer reports every run as finished, even one whose every download
failed, and names no source), or it did not finish within **30 minutes**. The plugin cannot
cancel an import, so after the watchdog it keeps refusing a new one for as long as the old one
still runs. Every value that is not what the contract says - a state outside
the four, a time of zero, `true` as a count - is `unknown` rather than guessed at.

**EPG &ndash; aktywny bukiet** is what is on across the bouquet the receiver's channel ± is
walking - the same list its own EPG would show for it. It reads the retained grid whose
`bouquet` names the active context, so switching bouquets on the remote or through „Bukiet"
switches it, and a grid for any other bouquet leaves it untouched. The `channels` attribute
is every channel of that bouquet, in the receiver's order:

| Field | What |
|---|---|
| `name`, `sref` | the channel, as the grid names it |
| `now` | `{title, begin, end}` for the programme on air, or `null` |
| `next` | the same for the one after it, or `null` |

`begin` and `end` are **epoch seconds**, not ISO strings - the one exception to the rule
below, because a card drawing a progress bar for two hundred channels wants numbers it can
subtract. Titles are cut at 80 characters. **`now` and `next` follow the clock**: the plugin
republishes a grid only when its content changes, so the sensor re-reads the grid itself the
moment any channel's programme ends (or, on a channel between programmes, the moment the
next one starts) instead of showing a finished programme until the next publish.

The state keeps three answers apart. A receiver in **no bouquet** - the radio list, the movie
list - is `0` with an empty list. An active bouquet whose **grid has not arrived**, was
retracted, or is not one the receiver builds grids for is `unknown` with an empty list: that is
„no grid", not „nothing on". Only a grid that is here can say `0` about itself. There is no
unit and no state class; it is a completeness indicator, not a measurement. The sensor shows the
bouquet the **receiver** is using and does **not** apply the **Bouquets to offer** option, so a
bouquet left out there still shows its programme titles in this sensor's attributes whenever
the receiver is on it.

🔴 **`channels` is excluded from the recorder.** Home Assistant excludes a media player's
`source_list` on its own, but this is an attribute of ours on an ordinary sensor, and a
bouquet's worth of programmes rewritten every time one of them ends would otherwise go into
the database each time. History keeps the count; the list is only ever the current one. The
full multi-event grids stay in the [`get_epg_grid`](#get_epg_grid) action, which returns them
and stores nothing.

Times in attributes are ISO 8601 strings rather than the epoch seconds the topics carry,
because a template can read one and not the other. The long programme description and the list
of running recordings are excluded from the recorder: they are kilobytes that change every
quarter of an hour, and a database is not where a plot summary belongs.

### 4.4 Binary sensors, switches, number

| Key | Polish name | unique_id | Topic | Notes |
|---|---|---|---|---|
| `recording` | *Nagrywanie* | `<node_id>_recording` | `recording` | device class `running` - what to check before rebooting anything |
| `recording_disk` | *Dysk nagrań* | `<node_id>_recording_disk` | `hdd` | device class `connectivity`; attributes `path`, `free_mb` |
| `power` | *Zasilanie* | `<node_id>_power` | `power` | <-> `cmd/power on\|standby` |
| `mute` | *Wyciszenie* | `<node_id>_mute` | `volume` | <-> `cmd/mute ON\|OFF` |
| `volume` | *Głośność* | `<node_id>_volume` | `volume` | 0-100, the box's own scale <-> `cmd/volume` |

The switches and the number are optimistic: they move the moment they are pressed and the
state topic confirms a moment later. That is not `assumed_state` - the box does report back -
it is the opposite: the answer is coming, and the dashboard should not sit still until it does.

### 4.5 Buttons and update

| Key | Polish name | unique_id | Command | What proves it |
|---|---|---|---|---|
| `deep_standby` | *Głębokie uśpienie* | `<node_id>_deep_standby` | `cmd/deep_standby` · **both gates below**; attribute `wake_on_lan` where the receiver cannot be woken over the network | silence |
| `restart_gui` | *Restart GUI* | `<node_id>_restart_gui` | `cmd/restart_gui` | silence |
| `reboot` | *Restart* | `<node_id>_reboot` | `cmd/reboot` · **both gates below** | silence |
| `wake` | *Obudź (WoL)* | `<node_id>_wake` | `wake_on_lan.send_magic_packet` - no MQTT, and available while the box is not; attribute `wake_on_lan` where the receiver cannot be woken over the network | - |
| `screenshot` | *Zrzut ekranu* | `<node_id>_screenshot` | `cmd/screenshot` | `screen` is republished |
| `refresh_discovery` | *Odśwież discovery* | `<node_id>_refresh_discovery` | `cmd/discovery` | the announcement is republished |
| `refresh_epg` | *Odśwież EPG* | `<node_id>_refresh_epg` | `cmd/epg_grid` · only while the box names the `epg_grid` capability | silence |
| `softcam_restart` | *Restart softcam* | `<node_id>_softcam_restart` | `cmd/softcam_restart` · **both gates below** | silence |
| `epg_import` | *Pobierz EPG* | `<node_id>_epg_import` | `cmd/epg_import` · **both gates below** | a new `epg_import` payload in `running` |
| `history_clear` | *Wyczyść ostatnio oglądane* | `<node_id>_history_clear` | `cmd/history_clear` · only while the receiver names the `history_clear` capability; unavailable while `zap_history` says `panic_button: false` | a **new** `zap_history` payload with at most one entry |
| `plugin` | *Wtyczka MQTT Bridge* | `<node_id>_plugin` | an `update` entity: installed = `info.plugin`, latest = the plugin release this version was written against | - |

**Every button waits for the receiver**, on the same path as the actions below. A refusal
raises `HomeAssistantError` carrying the box's own sentence, and „Ostatni błąd" records it.
Where the command has an observable effect, that effect is the proof and the press waits up to
ten seconds for it. Where it has none - the box is about to restart, or an EPG grid whose
content has not changed is not republished - the contract offers no positive acknowledgement,
so the press waits a second for a complaint and treats **silence as success**. „Zrzut ekranu"
pressed twice inside the plugin's minimum of five seconds between captures is therefore a
visible refusal now, where it used to be a press that appeared to work and did nothing.

The proof is that the topic moved, not that *this* command moved it: a screenshot the plugin
publishes on its own interval, or a retained picture or announcement replayed by the broker
after a reconnect, can end a press's wait. The press then reports success for something it did
not cause. It is benign - the command was sent, and the receiver carries it out or complains on
its own - and the alternative is a correlation id the contract does not have.

**A receiver that cannot be woken over the network says so on „Głębokie uśpienie" and „Obudź
(WoL)".** From plugin 0.3.0 the receiver reports `info.wol`, whose `supported` is the image's own
answer - whether it found a switch to arm Wake-on-LAN for deep standby - and not the network
card's `Supports Wake-on`, which describes a suspend path these images do not take. Where it is
`false`, both buttons carry the attribute `wake_on_lan` with the value `not_supported`, for
automations and dashboards to read. Its translation, „Wake-on-LAN" followed by a sentence in the
household's language, says that from deep standby that receiver is woken only by its remote,
its front button or a timer, and on some receivers also by switching on a TV connected over
HDMI, if HDMI-CEC is on. On „Obudź (WoL)" it adds that the magic packet is still sent but will
not wake it. Home Assistant shows it only to administrators, under ⋮ -> Details in the button's
dialog: a button has no control of its own there, and that menu is drawn for administrators
alone. The same applies to `media_player.turn_on` and `remote.turn_on` while the box is in deep
standby, which send the same packet. A button has no description of its own in Home Assistant,
and an attribute leaves the name - and so the entity id - alone. Where the receiver
reports `supported: true`, or an older plugin reports no `wol` at all, or `supported` is not a
boolean, neither button has the attribute and nothing else changes.

**„Odśwież EPG"** exists only while the receiver names the `epg_grid` capability. With the
plugin's `epg_grid_events` setting at zero there are no grids to rebuild, the capability is
absent, and the button is not created. A capability can also arrive late, so the button appears
when the receiver says it can do it rather than only at startup.

**„Restart softcam"** stops every instance of the card-sharing client the image started
and starts exactly one, with the line the image itself would have used. On a receiver
whose cam binary has a long name it is mostly a way to **collapse the copies the image
left behind**, which also fixes a frozen one.

**It needs two gates open and both of them are the receiver's**, because they answer
different questions and a receiver gives the two answers independently. The permission
`softcam_restart_allowed` is set under *Menu -> Plugins -> MQTT Bridge* and is not writable
over MQTT, for the same reason as deep standby; it is a plain checkbox that exists on
every installation and says whether the command is wanted. The **`softcam` capability**
says whether the receiver could carry it out at all - the plugin claims it only where the
cam binary resolves under the softcam directory, its family has a known start line, and
the image starts it through its manager's poller rather than through an init script. A
receiver with the permission on and no resolvable cam therefore reports the permission,
claims no capability and refuses the command; no button is offered for it, which is also
what the plugin's own MQTT discovery mode does with the same receiver.

There is no Home Assistant option beside either gate - the press costs a few seconds of a
scrambled picture and nothing else. A plugin that does not report the permission gets no
button, because none has ever existed there to keep. Only a stated „no" to the
*permission* takes an existing button away; a capability that stops being named does not,
for the reason [§4](#4-entities) gives.

The receiver refuses the command in six situations and says which in its own words on
„Ostatni błąd": without the permission; while a recording is running; with one due in the
next ten minutes; when the image will not say whether it is recording at all; inside the
rate limit of one manual restart a minute; and for the first minute after the plugin
starts, because the image's own liveness check fires about a second after every interface
start and restarting into that races a copy that is already on its way.

The press is proved by **silence**, like the other restarts. The receiver's own sequence
can take about ten seconds before it republishes `softcam` - it waits for the instances to
go, kills what survives, starts one and lets it settle - and waiting for that topic would
report „the receiver did not carry this out" for a restart that worked on exactly the
slowest boxes. Every refusal arrives immediately, which is what the wait is for.

**„Pobierz EPG"** runs the receiver's own EPG-Importer now, with the sources selected on the
receiver, instead of waiting for its schedule. The import is the image's, not the plugin's:

- **At its end the image saves the guide on the thread that draws the picture**, so menus freeze
  for two to three seconds. The plugin adds nothing blocking of its own and cannot move that.
- If the importer's **„clear old EPG"** setting is on, the image empties the whole guide before it
  imports, and the grids stay empty until it finishes.
- **The importer's own deep-standby settings apply** to an import started here exactly as to a
  scheduled one: a receiver in standby that the importer is set to shut down afterwards will shut
  down. That is the image's behaviour, not a fault of the button.

It has the same two gates as „Restart softcam", both the receiver's: the permission
`epg_import_allowed`, set under *Menu -> Plugins -> MQTT Bridge* and not writable over MQTT, and
the **`epg_import` capability**, which the plugin claims only where it found the importer the
image already loaded and the guide can be imported in place - on an image where the importer
would finish by restarting the interface, there is no capability and no button. Only a stated
„no" to the permission removes the button.

The receiver refuses the press, in its own words on „Ostatni błąd", without the permission;
while an import is running, whoever started it; while recording or with a recording due within
ten minutes, because the final save holds up the thread that starts recordings; within ten
minutes of the importer's own scheduled run; and when no sources are selected. The press waits
for a **new** `epg_import` payload saying `running` - the one already held does not count, since
an import the schedule started is already `running` and the receiver refuses a second one. If
the new payload says `failed` instead, the press raises its `error`; the first answer stands.
A retained copy the broker replays after a reconnect is never the answer, because it may
describe the previous run. While an import runs the
receiver also refuses its own deep standby, reboot and interface restart, which would lose it.

**„Wyczyść ostatnio oglądane"** does what the remote's **0** key does on the receiver with its
panic-button setting on: it empties the receiver's zap history and **switches to channel 1** -
the first channel of the first bouquet, because the image has no „panic channel" setting of its
own - and closes picture-in-picture if it is open. The receiver runs the key's own handler; no
key is injected. There is no permission behind it: `cmd/zap` needs none, and `cmd/key KEY_0`
already does the same with none. The settings that change what it does are the receiver's:
`panicbutton` (off, and 0 only goes back one channel - the button is then unavailable),
`multibouquet`, `pip_zero_button` and `check_timeshift`.

The receiver refuses the press in every case where 0 would not clear the history either, and
each refusal carries a reason code that Home Assistant shows **in the household's language**:
standby; the panic-button setting off; a history of at most one channel; timeshift, where 0
would ask on the television whether to leave it; the moment after timeshift during which the
receiver holds zaps; picture-in-picture showing with 0 set to act on it; a recording being
played back; a menu, the channel list or the EPG open on the receiver's screen, where the key
would not reach the history; and, defensively, a history that was still longer than one entry afterwards. A
code this release does not know, or none, is raised in the receiver's own English sentence.
The press waits for a **new** `zap_history` payload holding at most one entry - not for the
list to be short, which it already is before a press the receiver refuses for exactly that
reason - and a retained copy the broker replays after a reconnect is never the answer.

**„Głębokie uśpienie" and „Restart" need two gates open**: the Home Assistant option
[below](#options), and the receiver's own `deep_standby_allowed`, which is set on the box under
*Menu -> Plugins -> MQTT Bridge* and cannot be written over MQTT - a setting that *enables* a
command is deliberately outside what anything with publish rights can reach. Only a stated
„no" removes the buttons: a receiver that never reports the permission is an older plugin, not
a refusal, and there the option decides alone as it always did. Turning the permission on at
the television makes the buttons appear without a Home Assistant restart.

The version entity compares what the box reports on `info` with the plugin bundled here, and its
summary says what that means. A box running an **older** plugin has an update: with SSH
credentials retained the card can install it and says so; without them it cannot, and the
summary says how to get there - reconfigure with „Zachowaj dane SSH do aktualizacji", or copy
the IPK onto the box and install it by hand. A box running a **newer** plugin is reported as up
to date, because offering it a downgrade would be worse than saying nothing; the summary states
that it is ahead, and the [diagnostics download](#7-diagnostics) carries the verdict in words
for a bug report. A version neither side can parse is reported as `unknown` rather than guessed.

Nothing on this card reaches the internet unless the **release check** option is on, and then
only once a day, and then only to report a tag - see [Options](#options).

### 4.6 Selects

| Key | Polish name | unique_id | Topics | Command |
|---|---|---|---|---|
| `bouquet` | *Bukiet* | `<node_id>_bouquet` | `channels`, `bouquet` | `cmd/bouquet` |
| `channel` | *Kanał* | `<node_id>_channel` | `channels`, `bouquet`, `service` | `cmd/zap` |
| `zap_history` | *Ostatnio oglądane* | `<node_id>_zap_history` | `zap_history`, `service`, `channels` | `cmd/zap_history` |
| `zap_history_all` | *Ostatnio oglądane (wszystkie)* · **disabled by default** | `<node_id>_zap_history_all` | `zap_history`, `service`, `channels` | `cmd/zap_history` |

„Bukiet" and „Kanał" exist **only while the receiver names the `channels` and `bouquet_context` capabilities**.
A plugin that cannot switch a channel-list context has nothing for either of them to do, and a
receiver that loses the capability has them taken out of the entity registry rather than left
behind unavailable. A receiver that has not said what it can do yet keeps whatever it has -
silence is not "no".

- **„Bukiet"** lists the bouquets on the `channels` topic, in the order the receiver published
  them and narrowed by the [bouquets option](#options), and points at the one the `bouquet`
  topic names. Nothing is selected when the receiver reports a null context: that is the radio
  list, the movie list, or a bouquet the plugin was not told to publish, and all three are
  ordinary operation. Choosing one sends `cmd/bouquet` with the bouquet's **service reference**
  and waits for the `bouquet` topic to come back - the plugin republishes it on every success,
  including a no-op, so there is always something to wait for. The receiver's own refusal is
  raised as the error.
- **„Kanał"** lists the channels of **that** bouquet only, and nothing else: the whole list is
  about a thousand rows on a real receiver, which is not a control. It reshapes the instant
  „Bukiet" or the
  channel list moves. Choosing one sends `cmd/zap` with the channel's **service reference** -
  never its name, because resolving a name is the plugin's ambiguity problem and there is no
  reason to hand it one. Nothing is selected while the playing service is not one of these
  channels.
- **A name that appears twice inside one bouquet** is numbered - „TVN HD", then „TVN HD (2)".
  A select cannot offer the same option twice, and dropping the repeat would put a channel out
  of reach. The number follows the **service reference**, not the position, so reordering the
  bouquet in the receiver's own editor does not change which channel „TVN HD (2)" tunes; the
  list itself still follows the receiver's order. 🔴 It cannot be stable when a duplicate is
  **added or removed** - the numbers are positions in a set that changed - so an automation
  should use the `zap` action with a service reference rather than name a label.
- **Service references are compared by identity, not as strings.** One channel has more than one
  spelling: a reference may stop at the tenth colon or carry it, an IPTV entry always adds its
  stream URL and its name, and the case of the hexadecimal fields is not agreed on anywhere. The
  selects compare the fields that identify a service, by the same rule and the same field count
  as the receiver plugin, so what is playing is recognised and a zap that worked is not reported
  as a timeout.

Both selects report **nothing selected** rather than an invented option - the receiver is on the
radio list, on a bouquet nobody published, or tuned outside the bouquet it is walking. That is
ordinary operation and not a fault.

**„Ostatnio oglądane" and „Ostatnio oglądane (wszystkie)"** follow the receiver's own zap
history - the list its „History Zap" screen shows when NEXT or PREVIOUS is pressed - which the
plugin publishes, newest first, on the retained `zap_history` topic. They exist while the
receiver names the `zap_history` capability, and a receiver that stops naming it keeps them.

- **The options are exactly the channels in that history**, in its order, numbered like
  „Kanał" when two carry the same name. Nothing is kept on this side: the history is the
  receiver's, holds at most twenty channels there, and a restart of its interface empties it.
  An entry the receiver sends without a name keeps its place: it is labelled with the name
  the channel list gives the same service, or, if no published bouquet has it, „Kanał bez
  nazwy (...)" („Unnamed channel", „Sender ohne Namen") with its service reference.
- **The state is the channel playing now**, matched to `service` by identity, when it is one of
  the options. Since plugin 0.3.0 nearly every zap - from the remote, „Kanał", the media player
  or the `zap` action - enters the history, so that is normally the first option. It is
  **nothing selected** when the playing channel is not in the list: a zap the receiver does not
  record (during timeshift, in picture-in-picture zap mode, or to a channel in no published
  bouquet), a zap by something the plugin does not see (OpenWebif, a zap timer), or just after
  an interface restart. An invented option would claim a way back that does not exist.
- **Choosing one** sends `cmd/zap_history` with its service reference - the call the
  receiver's own History Zap screen makes, so the channel moves to the front - and waits for
  `service` to name it. Choosing what is already playing sends nothing. From standby the
  receiver wakes first. The history screen has no timeshift question, so neither does this.
  The receiver refuses while it is playing back a recording, and that refusal is shown in
  the household's language; a channel that has left the history in the meantime is refused
  in the receiver's own words.
- **„Ostatnio oglądane" leaves out** the bouquets named in the
  [option](#options); with the option empty the two lists are the same. It never has a hidden
  channel as its state, because its state can only be one of its options.
- **„Ostatnio oglądane (wszystkie)" leaves out nothing** and is **disabled by default**. 🔴 Its
  options are not recorded - Home Assistant declares a select's `options` unrecorded - but
  **its state is**: once enabled, the name of the channel playing now, hidden bouquets
  included, goes into the recorder and the logbook on every zap, exactly as the „Kanał"
  sensor already records it. An integration cannot exclude an entity's state from the
  recorder; if that must not happen, exclude it in `configuration.yaml`:

  ```yaml
  recorder:
    exclude:
      entities:
        - select.dekoder_salon_ostatnio_ogladane_wszystkie
  ```

  A dashboard card that shows it only to some users hides a card, nothing more: Home Assistant
  has no per-entity permissions.

The contract of the topic and both commands, and why the plugin's zaps are recorded, are in the
plugin's `docs/TOPICS.md` and its ADR-0014.

## 5. Actions

Every action targets the receiver's **media player**, which is how a device, an area or an
entity all resolve to the same box - Home Assistant expands a device target to the entities of
the platform an action was registered on, and one box has exactly one media player.

| Action | Fields |
|---|---|
| `enigma2_mqtt.zap` | `sref` **or** `name` - exactly one |
| `enigma2_mqtt.send_key` | `key` (`KEY_RED` or `red`), `long` |
| `enigma2_mqtt.message` | `text` (cut at 500 characters, 200 for a toast), `style` (`popup`\|`toast`, default `popup`), `type` (`info`\|`warning`\|`error`), `timeout` |
| `enigma2_mqtt.add_timer` | `sref` + `event_id`, **or** `sref` + `begin` + `end` + `name` |
| `enigma2_mqtt.delete_timer` | `sref`, `begin`, `end` |
| `enigma2_mqtt.record` | `action`: `start` or `stop` |
| `enigma2_mqtt.screenshot` | - |
| `enigma2_mqtt.set_ha_mode` | `mode`: `discovery`, `integration` or `off` |
| `enigma2_mqtt.get_epg_grid` | `bouquet` (optional) · **returns a response** |

`begin` and `end` take a date and time or the epoch seconds the topics use; both end up as
epoch seconds on the wire.

`message` without a `style` is the popup it has always been, byte for byte: without a
`timeout` it stays ten seconds, and `0` keeps it up until dismissed. `style: toast` sends the
discreet toast described under [„Ekran &ndash; dyskretnie"](#42-remote-notify-event-image): without
a `timeout` it stays five seconds, and a `timeout` outside 1-30 is refused before anything is
sent, because a toast cannot be dismissed. The field exists on every receiver - an action's
fields are the same for the whole integration - so a toast aimed at a receiver that does not
name the `toast` capability is refused with the reason, and nothing is published. `type` is
passed on for a toast too; the receiver validates it and shows every toast the same way.

### How an action knows it worked

The contract has **no acknowledgement topic**: a command is proved by the state topic it moves
and disproved by `last_error`. So every action that changes something waits up to ten seconds
for whichever comes first, and raises `HomeAssistantError` carrying the box's own words when
the box refuses. A retained `last_error` is ignored - that is the complaint the broker had
before the command was sent, not an answer to it.

| Action | What proves it |
|---|---|
| `zap` | `service` names the requested channel |
| `record` | `recording.active` becomes non-empty, or empty |
| `add_timer`, `delete_timer` | `timers` is republished |
| `screenshot` | `screen` is republished |
| `set_ha_mode` | `info.ha_mode` echoes the new mode |
| `get_epg_grid` | the grid is already retained; a box with none is asked once |

Two commands move nothing at all - `send_key` and `message` - and for those the only answer
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

...and then `grid['media_player.dekoder_salon'].bouquets[0].channels`.

It is deliberately not a state attribute anywhere: a grid runs to tens of kilobytes per
bouquet, and an attribute of that size is written to the recorder database on every refresh.

## 6. Device triggers

The colour keys - red, green, yellow, blue - each as a short and a long press: eight triggers
on the receiver in the automation editor.

| Trigger type | Fires on |
|---|---|
| `red_short` ... `blue_short` | a tap of that colour key |
| `red_long` ... `blue_long` | that colour key held |

They listen to the `enigma2_mqtt_key` bus event, which carries `device_id`, `node_id`, `key`
and `press` and is fired for **every** key the box reports. The event is fired by the
integration itself rather than by the „Pilot &ndash; klawisz" entity, so the triggers keep working
when that entity is disabled - a household that only wants the colour keys in the automation
editor should not have to keep an entity it never looks at.

Any other key is still reachable: either through the event entity, or through a plain event
trigger on `enigma2_mqtt_key` with the key name in its event data.

## 7. Diagnostics

The device page offers a diagnostics download: the config entry, the box's announcement, its
last `info` payload, the availability flag, the capability list, the last payload of every
state topic, and the device as the registry holds it. It is meant to be attached to an issue,
so the MAC address, the IP address and the configuration URL built from it are redacted, and so
are the broker and SSH credential keys - those are named in the redaction list before the
installer of M4 can create one, rather than after.

Four things are summarised rather than included, because their contents answer no question a
bug report asks and describe a household instead. **`screen`** appears as a size and the moment
it was taken, never as the picture of somebody's television. **`channels`** appears as the
bouquet names and how many services each holds, not as the channel list. **`key`** does not
appear at all: it is who pressed what a moment ago, which is not state. **`zap_history`**
appears as the number of entries, `current`, `limit` and `panic_button` - no channel name and
no reference, whatever the hide option says.

One thing is stated rather than left to be worked out. The **`plugin`** block gives the version
installed on the box, the version bundled here, the version this release was written against,
and the verdict - `matched`, `older_than_bundle`, `newer_than_bundle` or `unknown`. A receiver
running a *newer* plugin reads "up to date" everywhere in the UI, because the version entity
refuses to offer a downgrade; that is right for a household and useless in a bug report, and a
plugin mismatch is where a missing entity or an unrecognised command usually ends up.

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

Delete the entry in *Settings -> Devices & Services*. On the way out the integration publishes
`cmd/ha_mode = discovery`, so the plugin announces itself again and the core MQTT integration
rebuilds its own entities - a box that was taken over here is handed straight back. That
publish is best effort: if the receiver is off or the broker is unreachable the entry is still
removed, and the mode can be set on the plugin's setup screen afterwards.

🔴 **Deleting the entry never uninstalls anything**, whatever the entry holds and whatever the
receiver permits: the one `cmd/ha_mode` publish above is the whole of what it sends, and it opens
no SSH connection. Removing the plugin is the separate action below
([ADR-0004](docs/adr/0004-remote-uninstall.md), [ADR-0006](docs/adr/0006-remote-uninstall-plugin-acts-ssh-verifies.md)).

**If you are uninstalling the plugin by hand** - over SSH, or from the receiver's own plugin
menu - **send `cmd/reset` to the box first.** Retained topics outlive the plugin that created
them: remove the package without resetting and the broker goes on serving a snapshot of a
receiver that is gone, for as long as the broker lives. `cmd/reset` retracts everything the node
owns and republishes it in one burst, so it is safe to run at any time - but only while the
plugin is still running, because after `opkg remove` there is nothing left to ask. The plugin's
[topic contract](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/TOPICS.md)
describes it in full. The action below does not need it: the plugin's own teardown retracts
without republishing.

### Removing the plugin from the receiver

*Configure -> Remove the plugin from the receiver* („Usuń wtyczkę z dekodera"). The entry is
offered only while all three of these hold, read from what the receiver is saying now:

- it is **on the broker** (`availability` is `online`);
- its current `info` states **`uninstall_allowed: true`** - a permission set on the receiver's
  own setup screen (*Menu -> Plugins -> MQTT Bridge*) and refused over MQTT, like
  `deep_standby_allowed`; off as shipped;
- the same `info` names the **`uninstall` capability**, which the plugin claims only where the
  package manager installed it, so a plugin unpacked by hand is never offered a removal it cannot
  perform.

Silence is no: an older plugin that does not report the permission offers nothing, and so does an
**empty `info`** - the plugin retracts it on its way out, and the entry goes with it rather than
standing for a receiver whose plugin has already gone.

The one form says what the step costs before it is taken, and it needs a tick to proceed. It is a
**one-way door**: once the plugin is gone nothing in Home Assistant can bring it back - only SSH or
the receiver's own package manager can. The receiver **keeps its plugin settings**, so a reinstall
from its package manager starts on the same broker, node ID and `integration` mode, and this entry
picks it up again with every entity id unchanged; the guided installer cannot run while the entry
exists, and asks for the broker password again when it does. Until a reinstall the device looks
exactly like a switched-off receiver - every entity unavailable - and the entry, its entities and
their history stay where they are. Delete the entry afterwards if you want it gone.

**The receiver does the removal; Home Assistant only asks and watches.** After the tick the
integration publishes `cmd/uninstall` with the node id as its payload, once, and only after the
broker has confirmed the subscriptions that are to hear the answer. The plugin stops its
publishers, retracts every retained topic it owns, publishes `offline` last, waits for the
broker's acknowledgements, disconnects, removes its package and restarts the interface. An
`opkg remove` over SSH would skip that order and leave the retained topics behind, so SSH is never
used to remove anything here.

**What a removal looks like on the broker.** Fresh messages, never retained replays: the
retraction of `info` and of the announcement, then `offline`. A receiver in `ha_mode: off`
publishes no announcement, so for it the retraction of `info` and then `offline` is the whole
shape. A switched-off receiver leaves `info` and its announcement retained and ends on its last
will, so it never produces this shape.

🔴 **`offline` is not the end of the answer.** The plugin says `offline` before it waits for the
broker's acknowledgements and before it runs its package manager, and either can still fail - the
image's own update check can hold opkg's lock, and a killed opkg reports success. The plugin then
reconnects and publishes `online`, its snapshot and its announcement, and then says on `last_error`
which step failed. So the flow keeps listening after the shape - through the SSH readbacks, when
there are any, and for the rest of the 60-second window when there are not - and a `last_error`
about the removal ends it with the receiver's own sentence. Without SSH readbacks a removal
therefore always takes the full minute.

Where the entry kept the installer's SSH credentials, SSH is the **witness**. Before the command
it reads whether the receiver is recording or about to (and refuses if so, having published
nothing), the running interface's process ids, `opkg status` of the package, and the line count
and SHA-256 of the receiver's `config.plugins.mqttbridge.*` block - computed on the receiver; the
settings never leave it. After the receiver says `offline` it waits for the interface restart and
then for OpenWebif to answer, and reads that `opkg status` is empty, that no opkg info file, plugin
directory, bytecode or OpenWebif hook file is left, that `/mqttbridge` answers 404, and that the
settings block's count and hash are unchanged. A restart that never came does not stop the other
readbacks; only the `/mqttbridge` check, which a restart is what settles, is left out.

🔴 **Whether opkg is busy is asked of its lock.** `opkg status` does not take opkg's lock - on opkg
0.6.3 it answers normally while another opkg run holds it - so before the command the integration
probes the lock itself: the installer's helper, fed to `python3 -` over the SSH session's standard
input so nothing is copied onto the receiver, takes every lock file opkg could be using without
waiting and lets go at once, as opkg does. If the lock is still held after five tries two seconds
apart, the flow stops with „opkg on the receiver is busy &mdash; try again in a few minutes" and publishes
nothing: the plugin's own removal would meet the same lock after it had already retracted
everything, and roll back. A probe that cannot run (no Python on the image, say) costs the
verification and nothing else - the command still goes. Among the readbacks, an `opkg status` that
does fail for a lock somebody holds (`Could not lock <path>`, `Command failed to capture privilege
lock`; not `Could not create lock file ...`) is asked again the same way before it counts.

Every command is a fixed read - the lock probe creates and removes opkg's lock file exactly as
opkg does, and nothing else - and an SSH connection that drops or times out, before the command or
after it, costs the verification and nothing else; the sentence says so (`ssh (timed out)`, `ssh
(connection lost)`).

The flow ends on one of these, and never on a success nobody saw:

| Result | When |
|---|---|
| **Removed and verified** | the shape arrived, and every SSH readback agreed |
| **Removed, not verified** | the shape arrived, but SSH could not be used or a readback disagreed - the sentence names which, for example `restart not seen`, `opkg status`, `OpenWebif not answering`, `ssh (connection lost)`. 🟡 The image's own restart asks on screen, with no timeout, while something is streaming or a background job runs; a restart that never came is reported here, with the plugin already disconnected and off the disk |
| **The receiver said it removed the plugin** | the shape arrived, there are no SSH readbacks, and the receiver stayed away for the rest of the 60-second window |
| **The receiver refused** | the receiver answered on `last_error`, with its own sentence, before a single retraction arrived - the permission is off, a recording is running or due, an EPG import is running, an uninstall is already running |
| **The receiver started the removal but aborted it** | at least one retraction (`info`, the announcement or `availability` emptied) arrived once the command was being sent (not somebody else's before it), and then the receiver said on `last_error` which step failed - opkg refused, opkg reported success with the package still there, the broker's acknowledgements never came, the connection dropped mid-retraction. The `offline` need not have arrived. The receiver's own sentence says what state the package is in; after an opkg that reported success with the package still there, some of its files may already be gone, and the sentence gives the command that puts it back whole |
| **The removal did not complete** | the shape arrived, then the receiver came back - `online`, or `info` published again - without a `last_error` about the removal |
| **The receiver did not act** | nothing of that shape arrived within 60 seconds |

## 9. FAQ

**Can I use the plugin without this integration?** Yes. That is what `ha_mode = discovery` is
for, and the plugin's own documentation covers it, including a `universal` media player recipe.

**Does this replace OpenWebif?** No. OpenWebif stays what it is; the plugin is a bridge, and
full EPG browsing and streaming remain OpenWebif's job. A compact EPG grid over MQTT is part
of v1 as an action that returns a response - see [ADR-0001](docs/adr/0001-m0-decisions.md).

**Does it work with the core `enigma2` integration?** They can coexist, but both would talk to
the same box; the point of this one is to stop polling, so remove the other when you switch.

**Which images are supported?** OpenViX 6.x and OpenATV 7.x are the supported set; OpenPLi 9
and OpenBH 6 are best effort pending testers. VTi (Python 2) is out of scope.

**Is any of this in the HACS default store?** Not yet - add the repository as a custom
repository. The default-store request follows `v1.0.0`.

## 10. Privacy

The `key` and `epg` topics say what is being watched and which buttons are pressed, and the
„Ekran" image entity is a picture of the screen. Nothing leaves your network - there is no
telemetry and no cloud - but the recorder keeps a history unless you tell it not to. Exclude
the image and the remote key event; take the key event's id from the device page, because it
follows your language (see [§4](#4-entities)):

```yaml
recorder:
  exclude:
    domains:
      - image
    entities:
      - event.dekoder_salon_pilot_klawisz   # English installation: event.<device>_key
```

The zap history is the same kind of record. The receiver publishes it whole, retained, on the
broker, and the option that hides bouquets from „Ostatnio oglądane" filters that one entity in
Home Assistant - it hides nothing on the network. „Ostatnio oglądane (wszystkie)" is recorded
once enabled; [§4.6](#46-selects) shows how to exclude it.

The [options](#options) can switch key publishing off, select `off`, `on_zap` or `interval`
screenshots, set the interval and choose how long an on-zap capture waits for the new picture.
The plugin validates and persists the settings as one transaction; disabling screenshots also
retracts the retained image. Optional conditional-access telemetry publishes only the current
service's system, encrypted/active result and ECM time. It never publishes server, account or
card details, and creates no entities until explicitly enabled. OSCam health is optional and off
by default; its per-source entities use stable opaque ids, and private reader labels, server
addresses, accounts and card identifiers are neither published nor retained by the integration.

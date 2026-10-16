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
`<base_topic>/<node_id>/cmd/#`. Everything the entities show and do goes over the broker.
The other connections the integration opens are few, and [SECURITY.md](SECURITY.md) lists
them: SSH and the receiver's web interface when you ask it to install, update or remove the
plugin; the receiver's web interface for channel picons; and, only if you name one in the
options, one request per channel change to your own go2rtc ([§4.7](#47-streams)).

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
| 0.4.0 | 0.4.0 | current release |
| 0.3.1 | 0.3.0 | superseded |
| 0.3.0 | 0.3.0 | superseded |
| 0.2.0 | 0.2.0 | superseded |
| 0.1.0 | 0.1.0 | superseded |

The integration ships the plugin it was built against, so the two move together. The bundle in
this release is byte for byte the package on the plugin's own
[releases page](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases/tag/v0.4.0), and
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

**Allow remote plugin updates** („Zezwalaj na zdalną aktualizację wtyczki") is off by default. Ticked,
the install writes the plugin's `update_allowed` permission into its provisioning file, so the
plugin obeys `cmd/update` over MQTT - from the release that can update itself; an older plugin
ignores the key. Unticked, nothing is written and the receiver keeps what it holds (off as
shipped). **The tick box takes effect only with a plugin that knows `update_allowed`: 0.4.0
and later.** Plugin 0.2.0 and 0.3.0 import the rest of the provisioning file, log one line about
an unknown key and delete the file, so the tick is dropped harmlessly and not remembered - on such
a receiver, set the permission on its own setup screen once it runs 0.4.0. Any device that can publish on the broker can then ask for an update to a newer, signed
release - never a downgrade - and the Home Assistant Mosquitto add-on enforces no ACL (below). The
permission can be changed later only on the receiver's own setup screen or its OpenWebif page,
never from Home Assistant.

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
would wake it, and with HDMI-CEC the television) or **streaming** to another device. With HDMI-CEC
set to follow the television, switching the television off - or a television that switches itself
off at night - puts the receiver into standby as well, so an install tried after that is refused
for standby: switch the receiver on first (its „Zasilanie" switch in Home Assistant, or the
remote).

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
  be judged stale; until then the next attempt is refused with "another installation holds the
  lock on this receiver ... The lock expires in about N min at the latest", N being what is
  left of the thirty minutes. The same sentence answers an install - the update card's, the
  guided installer's or the forced reinstall - while another one really is running on the
  receiver, since the lock's record cannot say which of the two it is. A plain "another
  installation is already running" is left for an install this Home Assistant itself is
  running on that receiver, and for a lock whose age cannot be read.
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

When there is more to offer than this form, *Configure* opens on a menu instead: **Options**,
which is this form; **Receiver menu** („Menu na dekoderze"), on a receiver that can show one or
an entry that holds one, described in [Receiver menu](#receiver-menu); **Install an older plugin
version** („Zainstaluj starszą wersję wtyczki"), described below; and, on a receiver that permits
its own removal, **Remove the plugin from the receiver** („Usuń wtyczkę z dekodera"), described in
[§8](#removing-the-plugin-from-the-receiver). With none of them on offer it opens straight on the
form, as before.

**Install an older plugin version** is the only way back to an older plugin: never over MQTT and
never from the update card. It is offered only with SSH credentials stored, once the receiver has
reported its plugin version, and while the verified signed index lists an older version this
integration may install - at or above the floor (the higher of 0.2.0 and the index's), not
withdrawn, the same contract. Press *Check for plugin updates* first if no index has been read
yet. The first step chooses the version; the second names what it takes away - the entities
whose capability the older plugin predates (they stay in Home Assistant, but nothing updates
them) and, going below a release that can update itself, updates over MQTT - and installs only
once its box is ticked. The package is downloaded and verified as for the update card, then the
SSH installer puts it over the newer plugin (opkg's `--force-downgrade`), restarts the interface
by the restart rule and, once the older plugin has announced itself, asks it to retract every
retained topic the newer one left (`cmd/reset`). A failed install rolls back to the newer
version. The receiver has to be switched on: in standby the install is refused, as on the update
card, because the restart would wake it; it keeps its channel. When the older version is the
plugin bundled with this integration, its bytes are used and nothing is downloaded. The flow ends
with a message and saves nothing: the entry and its options are unchanged. Settings the older
plugin does not know stay on the receiver and come back with their values on the next upgrade.
**After a downgrade the update card offers the newer release again** - it is still the newest
compatible one, and the card never offers anything below the version running. Skip it on the
card to stay on the older version; a skip lasts until a newer release than the skipped one
appears.

| Option | Default | What it does |
|---|---|---|
| **Show the deep standby and reboot buttons** | off | The Home Assistant half of the gate on „Głębokie uśpienie" and „Restart"; the receiver's own `deep_standby_allowed` is the other half, and [§4.5](#45-buttons-and-update) says how the two combine. Turning this off removes the buttons from the entity registry rather than leaving them behind unavailable. It is about what appears on a dashboard, **not** a safety mechanism: the plugin refuses both commands while a recording is running whatever is set here. |
| **Wake-on-LAN MAC address** | the address the box reports on `info` | The target of the magic packet the „Obudź (WoL)" button, `media_player.turn_on` and `remote.turn_on` send. Set it when the receiver reports a different interface from the one that is plugged in - a box on Wi-Fi does not answer a packet sent to its cable port. Written as `00:00:5e:00:53:01`, `00-00-5e-00-53-01`, `0000.5e00.5301` or `00005e005301`, in any case; it is stored lower-case and colon-separated whichever you type, and anything that is not an address fails the form rather than failing later from inside `wake_on_lan`. Whichever address a packet would go to - the override, or the one the box reports - is registered on the device as its MAC connection, so the rest of Home Assistant knows it too. On current Home Assistant that connection does not merge devices across integrations, so a router or DHCP integration may still show a second card for the same receiver; it is there so that what *is* keyed on a MAC can find this one. A value stored by an earlier release that is not an address is dropped once at startup, with a line in the log. |
| **Bouquets to offer** | every bouquet the box publishes | Which bouquets feed the media player's channel list and the media browser. The choices are the bouquets on the `channels` topic, and a name can be typed for one the box has not published yet. This narrows the plugin's own `bouquets_for_select`; it cannot widen it. |
| **Channels in the media player's source list** | every bouquet on offer | What `media_player.source_list` holds, and nothing else. **Every bouquet on offer** is what this integration has always done and stays the default, so an existing installation does not change under an automation that names a channel; on a receiver with about a thousand channels it is a dropdown of about a thousand rows. **The active bouquet** is the short list the receiver's own channel ± is walking, which is also what the „Kanał" select shows. `select_source` follows this setting; the `zap` action takes a service reference and does not. Three cases keep the long list whatever is chosen, because there is nothing to shorten it to and an empty source list would leave no way to change channel: a receiver that publishes no channel-list context (an older plugin without `bouquet_context`), a context naming a bouquet the **Bouquets to offer** option excludes, and a context naming a bouquet with no playable channel in it. The last two are logged as a warning, once per bouquet. 🔴 This setting is **not** about the recorder: Home Assistant declares `source_list` an unrecorded attribute and the recorder removes it before it measures a state against its size limit, so the long list never reached the database in the first place. |
| **Picture on the media player card** | screenshot | Which picture the media player shows. **Screenshot** shows the screenshot of the television the plugin publishes, as before, and the picon of the new channel only between a channel change and the next screenshot: the last screenshot still shows the previous channel then. With screenshots off on the receiver no next one arrives, and the picon stays. **Channel picon** always shows the picon of the current channel, and the screenshot only for a channel that has none. Home Assistant fetches picons from the receiver's web interface (OpenWebif, plain HTTP on port 80) and serves them itself; [§4.1](#41-media-player) says which address it asks, how, and what happens when that fails. The „Ekran" image entity is the screenshot in both settings. |
| **Bouquets hidden from "Recently watched"** („Bukiety ukryte w „Ostatnio oglądane"") | none | Which bouquets' channels [„Ostatnio oglądane"](#46-selects) leaves out - and that one entity only: „Ostatnio oglądane (wszystkie)", „Kanał", „Bukiet" and the media player are untouched. The choices are the bouquets on the `channels` topic, and a name can be typed. With it empty every entry shows. With a bouquet chosen, an entry shows only when its bouquet is a published bouquet that is not hidden **and** its channel is not a member of any hidden bouquet, so a channel of a hidden bouquet reached through another one stays hidden. While anything is chosen, an entry that cannot be checked - no bouquet path, a radio bouquet, a bouquet the plugin does not publish - is hidden too. And while a hidden bouquet is listed without its channels - plugin 0.5.0 could not publish that bouquet's own topic, or it has not arrived - every entry is hidden, because which channels to hide is what is missing. A hidden name that is no longer on the `channels` topic is logged once. 🔴 **This is a filter in Home Assistant, not privacy on the network**: the receiver publishes every entry of its history on `zap_history` and every channel of every bouquet on `channels`, retained, to any broker login; its own History Zap screen, the plugin's OpenWebif page and „Ostatnio oglądane (wszystkie)" show everything. See [§4.6](#46-selects) for what the recorder keeps. **With plugin 0.5.0 the same bouquets are also kept out of what Home Assistant shows about streams** ([§4.7](#47-streams)): a stream client on such a channel is listed without the channel, such a channel gets no stream address and no stream name, it is not offered as a media source, and your go2rtc is not pointed at it. The option's name still says only "Recently watched". |
| **Check daily for available plugin versions** | off | Off means this integration asks nothing on its own. The plugin's **signed release index** - `releases.json` and its signature from the plugin's fixed HTTPS origin, `https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/` - is fetched only when somebody presses **Check for plugin updates** (*Sprawdź aktualizacje wtyczki*), or uses Home Assistant's own "Check for updates" while this option is on. On, it is also fetched **at most once every 24 hours**, one index for every receiver. It is fetched with a verified TLS context and no redirects, at most 64 KiB, and it is accepted only when its Ed25519 signature verifies with a key built into this integration and its serial rises by the rule in [ADR-0008](docs/adr/0008-signed-plugin-index.md) - the same rule the receiver applies. Manual checks share a ten-minute limit. The index, its signature, the stamps and the record of accepted serials are kept in `.storage/enigma2_mqtt.release_index`, so a reload or a restart asks nothing again; the old `.storage/enigma2_mqtt.release_check` is removed. **Every newly accepted index is announced**: a warning in the log and a persistent notification with its serial, its key, the versions it adds and withdraws, and its floor - the index is signed in the plugin repository's CI, so an index nobody expected must be visible. It is then published, retained, on `enigma2mqtt/release_index`, where every receiver on plugin 0.4.0 or later verifies it again - the way a receiver with no internet of its own learns of releases - and put back, verified first, when the first receiver is set up and at every reconnect to the broker, if the broker lost it. What the broker retains there is read first: an index of a higher-ranked key, or a newer one of the same key - such as an index signed with the plugin's spare key in an emergency - is never overwritten, and when the rule accepts it, Home Assistant takes it and announces it like any new index. An index and a signature that do not match are read once more before they are judged, because a publication can land between the two requests; a pair that still does not verify is reported as "could not be verified", and the log keeps a warning. Nothing else is downloaded on a check; a release package is downloaded only when somebody installs it (see [Buttons and update](#45-buttons-and-update)). |

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

**Your own go2rtc (optional)** („Własne go2rtc (opcjonalnie)") is a section of the same form, shown for a
receiver that claims the `stream` capability, and collapsed until it is in use. Three fields:

| Field | Default | What it is |
|---|---|---|
| **go2rtc address** | empty, which is "off" | The address of the API of a go2rtc you run yourself, for example `http://homeassistant.local:1984` for the go2rtc add-on. `http://` or `https://`, a host, optionally a port and a path in front of the API; nothing after it. Emptying it switches the feature off; the stream stays in your go2rtc ([§4.7](#47-streams)). |
| **Stream name** | `enigma2_<node_id>` | The one stream Home Assistant keeps up to date there. Letters, digits, `_`, `-` and `.`, at most 64 characters - this integration's own, conservative rule. |
| **Source template** | `ffmpeg:{url}#video=h264#audio=opus#audio=aac` | What go2rtc is given to play, with `{url}` - exactly once - where the receiver's stream address goes. **No white space**: go2rtc refuses a source that contains a space, so the form refuses the template with a sentence that says so. |

Nothing is stored for a field left at its default, and nothing at all while the address is
empty. [§4.7](#47-streams) says what is sent, when, and what the template is for.

### Receiver menu

*Configure -> Receiver menu* („Menu na dekoderze"), for a receiver on plugin 0.5.0 or later that
claims the `menu` capability. The plugin then shows a **"Home Assistant" menu on the television**,
opened from the receiver's *Extensions* menu and by a key on the remote (long HELP unless changed
on the receiver), and this is where its contents are chosen. The receiver never learns what a row
does: it shows what Home Assistant publishes and, when the viewer presses OK, sends back which
row and - for a slider, a list or a text - which value. Home Assistant performs it
([ADR-0012](docs/adr/0012-receiver-menu.md)).

The menu is **one level of groups**, each a list of controls, plus controls at the top. At most
**8 groups**, at most **24 entries** in the top list (groups and controls together) and at most 24
controls in each group. The step shows the menu as it stands and offers *Add a group*, *Add a
control*, *Change a group*, *Change a control*, *Move a group or a control*, *Remove groups or
controls* and *Save the menu*.
**Nothing changes until you save**; closing the dialog discards the edits. Saving stores the
menu in the entry's options and reloads the entry, and the reload publishes it.

- **A group** has a name (shown cut at 60 characters) and an icon.
- **A control** is any entity, scene or script picked with Home Assistant's entity picker, the
  place it goes (the top list or a group), and optionally a name and an icon. Without a name the
  row shows the entity's own name; without an icon it gets one that fits the entity. When the
  entity can be shown in more than one way - a light as a switch or as a brightness slider - a
  second step asks which. An entity of a domain the table below does not list is shown as an
  indicator, with its state.
- A control stores the entity's **registry id**, so it follows the entity when its entity ID is
  renamed: the row keeps showing it and acting on it under the new ID, without opening the
  options. An entity that has no registry entry is stored by its entity ID.
- New groups and controls go to the end of the list they are put in. Changing a control's place
  moves it to the end of the new list. *Move a group or a control* moves one entry one place up or
  down within its own list - the top list, or the group it is in; at the end of its list it
  stays.

The icons are the plugin's own, by name: `neutral`, `lamp`, `ceiling-light`, `blinds`, `scene`,
`script`, `power`, `fan`, `lock`, `radiator`, `thermometer`, `tv`, `speaker`, `door`, `gate`,
`garage`, `alarm`, `camera`, `sofa`, `bed`, `kitchen`, `garden`, `movie`, `note`, `home`.

| Control | Entities | What OK does | What the row shows |
|---|---|---|---|
| Button | `script`, `scene`, `button`, `input_button`, `automation` | `script.turn_on`, `scene.turn_on`, `button.press`, `input_button.press`, `automation.trigger` | nothing on the right |
| Switch | `light`, `switch`, `input_boolean`, `fan`, `cover`, `lock` | the domain's `toggle`; for a cover `close_cover` when it is open and `open_cover` when it is closed; for a lock `unlock` when it is locked and `lock` when it is unlocked - by the state when OK arrives, not the one the row showed | the state in your language, green while on (open, locked), red while off |
| Indicator | any entity except a script, scene, button, input button or automation, which are buttons only | nothing - it cannot be acted on | the state in your language, coloured where it is on or off; a sensor shows its reading with its unit, and a state that is neither on nor off has no colour |
| List of options | `select`, `input_select`, `climate` (HVAC mode) | `select_option`, or `climate.set_hvac_mode` | the current option; a climate's modes in your language |
| Slider | `light` (brightness), `cover` (position), `media_player` (volume), `number`, `input_number` | `light.turn_on` with `brightness_pct`, `cover.set_cover_position`, `media_player.volume_set`, `set_value` | the value with its unit. Brightness, position and volume run 0-100 % in steps of 10; a number runs over its own range, its step made coarser where it would have more than 200 |
| Text field | `input_text`, `text` | `set_value` | the text, cut at 24 characters; the receiver's keyboard takes the entity's maximum length, at most 255. **A field in password mode never has its text published** - neither here nor as an indicator |

**Green means all good, red means look at it.** A switch is green while on (a cover open, a lock
locked) and red while off. An indicator follows the same rule, except for a binary sensor whose
"on" needs attention - door, window, opening, garage door, smoke, gas, carbon monoxide, moisture,
safety, problem, tamper, lock, battery, heat, cold, update, vibration, sound - which is green
while off: a closed door is green, an open one red. A connection is green while connected.

A light that is only on or off is not offered as a slider, nor a cover without a position or a
player whose volume cannot be set. An entity that is unavailable, or that no longer exists, is
shown greyed and cannot be acted on.

**When it is published.** Retained, QoS 1, on `enigma2mqtt/menu/<node_id>` - outside the
receiver's base topic, because it is this integration's - at setup, after every save, when the
receiver comes back online, when Home Assistant's connection to the broker comes back, and
whenever a bound entity's state changes in a way the menu shows:
the first change at once, a burst of changes as one publish half a second later. An attribute
the menu does not show (a player's position) publishes nothing. The whole payload stays under
64 KiB; whatever would not fit, or would break a bound above (more than 24 options in a list,
for example), is left out from the end with one warning in the log, and an action on a control
that was left out is refused. An empty menu is published
as the empty payload, which the receiver reads as "no menu". **Removing the entry retracts the
menu**, and reconfiguring to a new node ID retracts the old node's; unloading or reloading
leaves it as it is.

**When the viewer presses OK.** The receiver sends one `menu_action` on
`<base_topic>/<node_id>/menu_action`. Home Assistant performs it only for a receiver that
claims `menu`, only for a row the saved menu holds now, with the action that row's control
takes, on an entity that is available, and with a value inside what the entity accepts now - an
option it offers, a number inside the slider's range (put on its step), a text no longer than
its maximum. Anything else is refused with a warning in the log, once per kind of refusal, and
nothing is called. A retained action, one whose id was just seen, and **one whose time is more
than 120 seconds off** - older, or ahead - by the time it carries (`ts`, the receiver's clock)
and Home Assistant's clock are refused too; an action without a time is accepted. **One
action per control at a time, and at most five a second from one receiver**: an OK on a
control whose last action is still being carried out is refused, and so is every action beyond
five in any second - a key held down, or anyone on the broker, does not become a stream of
service calls. Only actions that pass every other check count towards the five, so refused
ones cost nothing and cannot crowd out a real press. **A button does not wait for what it
starts**: a script, a scene, an automation or a button entity takes the next OK as soon as Home
Assistant accepted the call, so a script with a long delay can be pressed again while it runs.
Every other control waits for its service call, but **at most 30 seconds**: then it takes the
next OK, with one warning in the log, and the call that has not returned is left to finish, not
cancelled - until the integration is reloaded or removed, which cancels it without an event. Keep the
receiver's clock right - an action from a receiver whose clock is more than two minutes behind
or ahead is always refused. A clock behind also raises a repair, „the receiver's clock is
behind", at the first such refusal; it goes by itself at the next action Home Assistant accepts
([§8](#8-troubleshooting)). A row keeps
its id across restarts and while only its name or icon change; bound to another entity or shown
as another control, it gets a new id, so a press the receiver sent for the old binding cannot
reach the new one. A service call that fails is logged; there is no repair.

After an action was performed, Home Assistant fires the event **`enigma2_mqtt_menu_action`** -
for a button once Home Assistant accepted the call (what the script or automation does after
that is not waited for, and an error inside it is in Home Assistant's own log), for every other
control once the service call returned without an error, even when that took longer than the 30
seconds after which the control was freed. A call that fails fires nothing:

| Field | What it holds |
|---|---|
| `device_id` | the receiver's device |
| `node_id` | the receiver's node ID |
| `row_id` | the row's id |
| `entity_id` | the entity the row is bound to |
| `action` | `press`, `toggle` or `set` |
| `value` | for `set`, the value applied (the entity's own option, the number on its step, the text); otherwise `null` |

```yaml
triggers:
  - trigger: event
    event_type: enigma2_mqtt_menu_action
    event_data:
      entity_id: script.movie_night
```

🔴 **Anyone who can publish to your broker can press a row.** The Mosquitto add-on does not
enforce an ACL, and `menu_action` carries no secret. What it can do is exactly what you put into
the menu and nothing more, so **only put into it what you would hand to anyone holding the
remote**. The menu's states are on the broker too, retained, for any login to read. Anyone on
the broker can also raise the repair about the receiver's clock, with an action stamped in the
past, and clear it, with an action stamped now - a nuisance, nothing more.

### Reconfigure

*...-> Enigma2 MQTT -> the receiver -> Reconfigure.* This is for following a box whose **node ID**
or **base topic** was changed on the plugin's own setup screen. The flow checks that a box
really answers on the new topic before it saves anything.

🔴 **A changed node ID is a changed identity.** Every unique id is `<node_id>_<key>`, so a box
with a new node ID is, as far as Home Assistant can tell, a different device doing the same
job. The old device is removed - with its entities and their history - and the reload builds
the box again under its new name. Changing only the base topic or the display name keeps
everything. The [receiver menu](#receiver-menu) moves with it: the old node's is retracted and
the reload publishes it under the new one.

## 4. Entities

**Twenty-five entities on every receiver**, and forty more that exist only while
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
| 1 | „Nieopublikowane dane" | the receiver's `info` carries `not_published` - plugin 0.5.0 or later |
| 5 | „Kodek obrazu", „Liczba klatek na sekundę", „Kodek dźwięku", „Język dźwięku", „Obraz z przeplotem" | the receiver reports the `service_codecs` capability - plugin 0.5.0 or later |
| 1 | „Passthrough dźwięku" | the receiver reports the `volume_passthrough` capability - plugin 0.5.0 or later |
| 1 | „Klienci strumienia" | the receiver reports the `stream_clients` capability - plugin 0.5.0 or later |
| 2 | „Głębokie uśpienie", „Restart" | the Home Assistant option asks for them **and** the receiver permits deep standby |
| 1 | „Restart softcam" | the receiver reports the `softcam` capability **and** permits a softcam restart |
| 1 | „Pobierz EPG" | the receiver reports the `epg_import` capability **and** permits an EPG import |
| 4 | the conditional-access diagnostics | the `cam_telemetry` option is on |
| 12 | the OSCam aggregates | the `oscam_telemetry` option is on |
| 3 per OSCam source | status, ready cards, shared cards | the `oscam_telemetry` option is on, for each reader or server that receiver reports; a source of a kind this integration does not know gets none, and the rest of the report still applies |

**What takes one away again is not the same question as what creates it**, and the table
splits on it. A row that follows an **option or a permission** is removed on a stated
„no": somebody decided, at the television or on the options form, and an entity that can
never say anything again is worse than none. A row that follows a **capability** is
removed only where the control behind it would be permanently refused - „Odśwież EPG" on
a receiver that builds no grids. „Softcam", „Import EPG", „EPG &ndash; aktywny bukiet", „Nieopublikowane dane" and the five process
diagnostics are neither: they are sensors with a history, and a capability that stops being named is an older
plugin after a downgrade, a hook that failed to attach on one boot, or a receiver that has
not answered yet. None of those is a decision, so they stay and say nothing until the
topic returns. „Ekran &ndash; dyskretnie" stays for the same reason: automations notify it by name,
and a receiver that stops naming `toast` - a downgrade, a skin reload whose rebuild of the
toast screen failed - has decided nothing. Sending to it then is refused in Home Assistant
with the reason, rather than published into a refusal on `last_error`.

The seven entities of plugin 0.5.0's stream and audio facts follow the same rule: the five
codec diagnostics, „Passthrough dźwięku" and „Klienci strumienia" are created when their
capability is first named and are never removed for it going quiet. After a downgrade of the
plugin they say `unknown`. The attributes the media player and the volume number carry for
those capabilities are different: an attribute has no history of its own, so it is there only
while the capability is claimed.

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
- **Browsing** - bouquets, then channels, each with its picon as the thumbnail. Picons are not
  published over MQTT: the thumbnail is an address on the receiver's own web interface, which the
  browser fetches. The media browser never waits for the receiver. The first time a bouquet is
  opened its thumbnails are `http://<host>/picon/<sref>.png` - at the address picons are asked
  at, which is the typed receiver host when there is one (see *Which address is asked*) - and Home
  Assistant asks OpenWebif in the background for that bouquet's picon links
  (`/api/getservices?sRef=<bouquet>&picon=1`). The next time the bouquet is opened the
  thumbnails are those links, so a picon OpenWebif finds under another name - the channel's
  name, another namespace - is found here too, and a channel without one gets no thumbnail.
  A bouquet is listed at most once each time it is opened. A channel change while the list is
  being fetched interrupts it for the channel that is on, and the list is then dropped, not
  started again: its thumbnails arrive the next time the bouquet is opened.
  Where OpenWebif does not answer, the thumbnails stay as they were. No address that may be
  asked simply means no thumbnail.
- **Playing** - `media_content_type: channel` takes a service reference, and `channel_name`
  takes a name. The browser sends the first.
- **Volume** - set, step and mute, on the box's own 0-100 scale underneath.
- **Turning on** - `cmd/power on` when the box is listening, a Wake-on-LAN magic packet when
  it is not; the packet wakes nothing from deep standby on a receiver that reports it cannot be
  woken over the network ([§4.5](#45-buttons-and-update)). `remote.turn_on` does the same.
  Turning off is always `cmd/power standby`; nothing here sends deep standby.
- **Picture** - the screenshot or the channel's picon, chosen with the
  [picture option](#options). Home Assistant serves either one itself, as the media image and
  the entity picture, so a browser never has to reach the receiver for it; the hash changes
  whenever the picture does - another channel's picon, a picon giving way to a screenshot, a new
  frame - so the browser fetches the new one.
  - **Screenshot** (the default): the retained `screen` JPEG. After a channel change the picon
    of the new channel is shown until a screenshot **taken after the change** arrives - judged by
    the picture's bytes, so the same picture delivered again after a reconnect does not count.
    If no new screenshot arrives, the picon stays. The channel a session starts on is not a
    change: after a restart the screenshot is shown, and the receiver is asked nothing. With
    screenshots switched off on the receiver and none held, the picon is shown from the start.
  - **Channel picon**: the picon of the current channel, and the screenshot only for a channel
    without one. For the moment the lookup of a channel seen for the first time takes, the
    previous channel's picon stays on the card under the new title.
  - **The picon on the card is fitted to it.** The media player card draws its picture into a
    square, crops what does not fit and takes its background from the picture's colours. A
    picon is wider than it is high and often a light logo on nothing, so it would lose its
    sides and end up light on light. Home Assistant therefore puts the picon, whole, in the
    middle of a square: in the picon's own corner colour when its corners are not transparent,
    and otherwise dark behind a light logo and light behind a dark one. The thumbnails in the
    media browser are the receiver's files as they are.
  - **Standby keeps the last picture**, and a receiver that wakes keeps it until it has said
    which channel it is on. As with any media player, Home Assistant shows no picture on the
    card while the state is `off`.
  - **Where a picon comes from.** OpenWebif is asked rather than its file names guessed. For
    the player that is one question about the one channel that is on,
    `/api/getpicon?sRef=<service>&json=true`, and then the picture. Where the media browser
    has already listed the channel's bouquet (see *Browsing*), that answer is used and the
    question is skipped. The player never lists a bouquet itself: that makes the receiver look
    for a picon for every service in it. Only when OpenWebif does not answer `getpicon` is
    `/picon/<sref>.png` tried, and once that has found a picture on a web interface whose
    `getpicon` has never answered, the question is not asked again for an hour. OpenWebif's
    default picon counts as none.
  - **Which address is asked.** The receiver host typed when the receiver was added, if one
    was - and if that cannot be the host of a web address (it has a port, a user name, an
    IPv6 zone, brackets around an IPv6 address, or a character outside ASCII - an
    international name is typed in its `xn--` form), there are no picons and no thumbnails,
    never the reported address. An IPv6 address typed bare, without a zone, is a host like
    an IPv4 address or a name, and is asked. Only with no host typed, the address the
    receiver reports on `info`, and only when it is an IP address written out - never a host
    name - that is not loopback, link-local, unspecified, multicast or reserved. That address
    arrives over the broker, where anything allowed to publish could have written it;
    [SECURITY.md](SECURITY.md) says what that leaves open. With no usable address there are
    no picons.
  - **What is accepted.** No redirect is followed. A link is used only if it is a plain path
    under `/picon/`; an answer larger than 2 MiB and a picture larger than 1 MiB are dropped;
    and only a PNG or a JPEG, told by its first bytes, is shown. A request waits five seconds at
    most.
  - **How often.** Nothing is polled, and one request runs at a time. A channel change to a
    channel not seen before costs at most two requests - the question and the picture - and a
    channel change abandons the lookup for the channel before it. A bouquet is listed when it is
    opened in the media browser: one request per opening at most, whatever the zapping, and none
    once its links are held, until its channel list changes or a day passes. A list a channel
    change interrupted is asked for again only at the next opening; a request for it that
    failed is not repeated for an hour. Up to 64 channels' pictures (8 MiB)
    are kept for a day, "no picon" for an hour, and a channel whose question got no answer
    for five minutes.
  - **When it fails** the other picture is shown and nothing else changes. A receiver whose web
    interface cannot be reached is left alone for 5 minutes; one that asks for a login (401 or
    403), or that answers but not as OpenWebif does - a status other than 200, or a page instead
    of an answer - for 15. Once OpenWebif has answered, such a failure for one channel is only
    that channel without a picon for five minutes; the 15 minutes start when it has happened
    to three different channels with no answer in between. Each is logged once, as a warning, until it works again. In the
    **Channel picon** setting the lookup is tried once more when that time is up, so the picon
    comes back without waiting for the receiver's next message; a second failure waits for that
    message. OpenWebif behind a login, on HTTPS only, or on another port than 80 therefore means
    no picons; credentials for it are not stored. A picon added to the receiver later appears
    after the hour or the day above, or when the integration is reloaded.
  - **Known limit: a screenshot does not say which channel it shows.** A screenshot that arrives
    after a channel change counts as taken after it. Zap through three channels quickly and the
    screenshot of the second - taken a few seconds after it was tuned, by the on-zap delay -
    arrives while the third is on, and replaces the third channel's picon until the third
    channel's own screenshot follows. The same goes for a screenshot already being taken at the
    moment of the change. Home Assistant cannot tell these apart; a later plugin release could
    say which channel a screenshot shows.
- **Position** - from `epg.now`, so the progress bar is the programme, not a stream.
- **Volume without effect** - for a receiver that claims `volume_passthrough` (plugin 0.5.0),
  the attribute `volume_passthrough`: `true` while the receiver hands the audio track being
  played to the television undecoded, which is when its own volume and mute move a bar on
  the screen and nothing anybody can hear; `false` while it decodes the track; `null` when it
  cannot tell. The volume stays settable either way ([§4.4](#44-binary-sensors-switches-number)).
- **Stream address** - for a receiver that claims `stream` (plugin 0.5.0), the attributes
  `stream_url`, `stream_name` and `stream_authentication`, and `go2rtc_stream` while your own
  go2rtc is set in the options. [§4.7](#47-streams) has all four.

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
  type and `press` (`short` or `long`; a payload without one is a short press, and one with any
  other value fires nothing) as an attribute. An event entity may only fire types it
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
| `epg_active_bouquet` | *EPG &ndash; aktywny bukiet* | `<node_id>_epg_active_bouquet` | `bouquet`, `epg_grid/<bouquet_slug>` | how many channels of the active bouquet have a programme now or next · no unit · attribute `channels` (**not recorded**), and `events_per_channel` from plugin 0.5.0 on · only while the receiver names the `epg_grid` and `bouquet_context` capabilities |
| `withheld_payloads` | *Nieopublikowane dane* | `<node_id>_withheld_payloads` | `info` | how many payloads the receiver did not publish because they do not fit one MQTT packet · diagnostic · **on by default** · attributes `topics` and `not_shown` · only for a receiver whose `info` carries `not_published` (plugin 0.5.0 or later) |
| `softcam` | *Softcam* | `<node_id>_softcam` | `softcam` | the selected cam binary · diagnostic · only while the receiver names the `softcam` capability |
| `epg_import` | *Import EPG* | `<node_id>_epg_import` | `epg_import` | `idle`, `running`, `done` or `failed` · diagnostic · attributes `started`, `finished`, `events`, `error` · only while the receiver names the `epg_import` capability |
| `last_error` | *Ostatni błąd* | `<node_id>_last_error` | `last_error` | the refused command's name · diagnostic · attributes `error`, `time` and `source` |
| `process_memory` | *Pamięć Enigma2* | `<node_id>_process_memory` | `process` | MiB · `data_size` · diagnostic · **on by default** |
| `process_memory_peak` | *Pamięć Enigma2 (szczyt)* | `<node_id>_process_memory_peak` | `process` | MiB · the high-water mark since the process started · diagnostic · **disabled by default** |
| `process_threads` | *Wątki Enigma2* | `<node_id>_process_threads` | `process` | count · diagnostic · **disabled by default** |
| `process_open_files` | *Otwarte pliki Enigma2* | `<node_id>_process_open_files` | `process` | count of open file descriptors · diagnostic · **disabled by default** |
| `process_started` | *Start Enigma2* | `<node_id>_process_started` | `process` | a timestamp · diagnostic · **disabled by default** |
| `video_codec` | *Kodek obrazu* | `<node_id>_video_codec` | `service` | `h264`, `hevc`, `mpeg2`, ... as the receiver names it · diagnostic · **disabled by default** · only while the receiver names the `service_codecs` capability |
| `frame_rate` | *Liczba klatek na sekundę* | `<node_id>_frame_rate` | `service` | fps, to three decimal places; the frame rate, not the field rate · diagnostic · **disabled by default** · `service_codecs` |
| `audio_codec` | *Kodek dźwięku* | `<node_id>_audio_codec` | `service` | the codec of the audio track being played: `mp2`, `ac3`, `eac3`, `aac`, ... · attributes `description` and `audio_tracks` (**not recorded**) · diagnostic · **disabled by default** · `service_codecs` |
| `audio_language` | *Język dźwięku* | `<node_id>_audio_language` | `service` | the language of that track, as the image gives it - usually an ISO 639 code · diagnostic · **disabled by default** · `service_codecs` |
| `stream_clients` | *Klienci strumienia* | `<node_id>_stream_clients` | `stream_clients` | how many clients the receiver's stream server is serving · attribute `clients` (**not recorded**) · **on by default** · only while the receiver names the `stream_clients` capability |

**Ostatni błąd** is the one sensor whose memory belongs to Home Assistant rather than to a
topic. The plugin clears `last_error` on the next command that succeeds, so by the time
somebody asks why a button did nothing, the next volume step has usually wiped the evidence.
This sensor keeps it: a cleared topic does not clear it, and it is restored with its text and
its time across a reload and a restart. The state is the failed command - `deep_standby`,
`zap` - and `unknown` until something fails; `error` is the receiver's own sentence, cut at
255 characters, and `time` is the receiver's own `ts`, falling back to the moment Home
Assistant learned of it when the payload has none. Taking the receiver's timestamp is what
makes the replay harmless: the retained complaint arrives again on every reconnect and at
every start-up, and a clock reading would walk the time forward each time. The next error
replaces it, and "Wyczyść ostatni błąd" (section 4.5) clears it on a receiver that runs plugin
0.5.0 or later. `source` says whose complaint it is: `receiver`, or
`home_assistant` for a failure Home Assistant records itself - a forced reinstall over SSH
(section 4.5), which the plugin may never be able to report. Home Assistant's own text is kept
up to 1024 characters. A replay of the receiver's retained complaint replaces Home Assistant's
record only when the receiver dated it later than that record and no more than five minutes
ahead of Home Assistant's clock at the moment of the replay. An undated replay never replaces
it. A replay dated further ahead does not either - but the five minutes are measured from each
replay, so a receiver whose clock runs ahead by some amount D keeps a stale complaint out only
for about D minus five minutes after Home Assistant recorded its own failure: from then on the
complaint's date is no longer too far ahead, it is later than the record, and the next reload
or restart puts it back. Only a clock that is wildly wrong - a year ahead - keeps it out for
good. The price is that a complaint made while Home Assistant was down, by a receiver whose
clock is that wrong, stays behind Home Assistant's record; anything the receiver publishes while
Home Assistant listens is shown whatever its date.

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

**Nieopublikowane dane** (*Withheld payloads*) counts the topics the receiver did not publish.
A broker closes the connection on an MQTT packet over its limit, so plugin 0.5.0 does not send
a payload that would need one: it leaves the topic out, retracts an older copy of it, and names
it in `info.not_published`. The state is the length of that list - `0` on nearly every
receiver - and `unknown` if the plugin stops reporting it: a receiver that has had the sensor
keeps it, also after a restart of Home Assistant, so a plugin put back to an older version
leaves a sensor that says `unknown` rather than one that is gone. The attribute `topics` holds up to
twenty entries, each `topic`, `bytes` (the size the packet would have needed when the plugin
first withheld it) and `limit`; `not_shown` is how many more there are. A topic under the
receiver's own tree is named without its prefix: `channels`, `channels/<bouquet_slug>`,
`epg_grid/<bouquet_slug>`. While the count is not zero, Home Assistant also shows a repair
that says the same in words ([§8](#8-troubleshooting)). A receiver on plugin 0.4.0 or older
never gets this sensor.

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

From plugin 0.5.0 on the sensor also has `events_per_channel`: the most programmes a channel
carries in this guide. It equals the receiver's `epg_grid_events` unless the plugin shortened
the guide to fit one MQTT packet, which is how "two programmes left today" is told from "cut
to two". With an older plugin the attribute is not there.

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

**What is on the decoder.** Plugin 0.5.0 adds the video codec, the scan, the frame rate and
the audio tracks to `service`, and claims `service_codecs` on an image that can fill them in.
They are technical facts about the current service, like the tuner's numbers, so the four
sensors and „Obraz z przeplotem" ([§4.4](#44-binary-sensors-switches-number)) are diagnostics
and disabled until you enable them. Each is `unknown` when the receiver does not say: nothing
is playing, the decoder has not decoded a frame yet, the stream type is one the plugin has no
name for. A codec name this integration has never heard of is shown as it arrives - the lists
are open. „Kodek dźwięku" and „Język dźwięku" are about the track **being played**, and
are `unknown` when the image names no active track. The attribute `audio_tracks` on
„Kodek dźwięku" is every track of the service, at most 32, each `index`, `description` (the
image's own word, which differs between images), `language`, `pid`, `codec` and `active`; it is
`null` when the receiver publishes no list, and it is excluded from the recorder. The plugin
notices another track picked on the remote within five seconds.

**Klienci strumienia** is the image's own count of who its stream server is serving. Every
open stream holds a tuner input, so this is the number behind "no free tuner". The attribute
`clients` has one row for each, at most 32: `host`, `sref`, `name`, `bouquet`, `transcoded`,
`hidden` and `can_stop`. The state is `unknown` while the plugin has emptied the topic because the
image's list could not be read - also after a restart of Home Assistant, when the broker holds
nothing on the topic at all - and `0` only for a published empty list. While the state is
`unknown`, `clients` is `null` rather than an empty list, so a template that counts the rows
does not read "no clients". The plugin looks every five seconds, so a client that came and
went between two looks was never on the list. [§4.7](#47-streams) says what `hidden` does and
how to stop a client.

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
| `volume` | *Głośność* | `<node_id>_volume` | `volume` | 0-100, the box's own scale <-> `cmd/volume` · attribute `volume_passthrough` while the receiver names the `volume_passthrough` capability |
| `volume_passthrough` | *Passthrough dźwięku* | `<node_id>_volume_passthrough` | `volume` | on while the receiver's volume does nothing audible · only while the receiver names the `volume_passthrough` capability |
| `interlaced` | *Obraz z przeplotem* | `<node_id>_interlaced` | `service` | on for an interlaced picture · diagnostic · **disabled by default** · only while the receiver names the `service_codecs` capability |

The switches and the number are optimistic: they move the moment they are pressed and the
state topic confirms a moment later. That is not `assumed_state` - the box does report back -
it is the opposite: the answer is coming, and the dashboard should not sit still until it does.

**Passthrough dźwięku** (*Audio passthrough*) answers "why does the volume do nothing". A
receiver can hand AC-3, AAC and other encoded audio to the television or an amplifier
undecoded, and then its own volume and mute move a bar on the screen and nothing else. Plugin
0.5.0 says whether that is so for the audio track being played now:

| `volume.passthrough` | „Passthrough dźwięku" | attribute `volume_passthrough` on „Głośność" and the media player |
|---|---|---|
| `true` - the track is passed on undecoded | on | `true` |
| `false` - the receiver decodes it, and its volume acts on it | off | `false` |
| `null`, or not there - it cannot be told | `unknown`, **not** off | `null` |

The volume entities stay available and settable in all three: the answer follows the track,
so it changes with the next programme or the next audio track, and a level set now is the
level that applies then. Use the binary sensor to grey a volume slider out on a dashboard or
to send the volume keys to the television instead. It says what the receiver is set to do,
not what the television does with the bitstream.

**How soon it follows.** The receiver reads it again with every reconciliation of the volume:
within five seconds of an audio track picked on the remote or a setting changed in its menu,
and **within thirty seconds** when a broadcaster changes the codec of the track being played
without changing the track - the one case the receiver only re-reads every sixth time. An AAC
track is `unknown` while the receiver's own AAC transcode (to AC-3 or DTS) is switched on, whatever
its downmix setting says: what reaches the television is then neither of the two answers.

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
| `clear_error` | *Wyczyść ostatni błąd* · diagnostic | `<node_id>_clear_error` | `cmd/clear_error` · only for a receiver whose `info.plugin` is 0.5.0 or later (below) | a **new**, empty `last_error` payload |
| `check_plugin_update` | *Sprawdź aktualizacje wtyczki* · diagnostic | `<node_id>_check_plugin_update` | no MQTT: fetches the plugin's signed release index, at most once in ten minutes, and available while the box is not | the update card's `index_serial`, `last_check` and `check_error` |
| `force_reinstall` | *Wymuś reinstalację wtyczki (SSH)* · diagnostic, disabled by default, only while SSH credentials are stored | `<node_id>_force_reinstall` | no MQTT: the SSH installer's forced mode, after two presses by an administrator (below) | a new interface process holding the plugin's log open, and the announcement when the plugin is switched on |
| `plugin` | *Wtyczka MQTT Bridge* | `<node_id>_plugin` | an `update` entity: installed = `info.plugin` with its build (`0.3.0+g1a2b3c4` for a development build), latest = what the card would install, and the installed version when it cannot install anything (below) | - |

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
its own. The contract has an id for a command from plugin 0.5.0 on, but only a refusal says it
back ([§5](#how-an-action-knows-it-worked)): success is still the topic moving.

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

**"Wyczyść ostatni błąd"** (*Clear last error*) empties the receiver's retained `last_error`
and, once the receiver has done that, the record "Ostatni błąd" keeps. It sends
`cmd/clear_error`, which changes nothing else on the receiver and needs no permission.

- **Only for plugin 0.5.0 or later.** The command came with that release and the plugin
  announces no capability for it, so the button is created when `info.plugin` states 0.5.0 or
  a later release, and not before `info` has arrived. An older plugin would answer the press
  with "unknown command", so it gets no button. The number is what counts: a development build
  that still reports 0.4.0 gets none either. The button is never removed again; on a receiver
  taken back to an older plugin it is **unavailable**.
- **What it clears.** The press waits up to ten seconds for a **new**, empty `last_error` from
  the receiver, which the plugin sends whether or not an error was there. Then "Ostatni błąd"
  goes back to `unknown` with empty `error`, `time` and `source` - also when what it showed was
  Home Assistant's own record of a failed forced reinstall - and the update card drops the
  refusal it was showing, as after any command that succeeds. Nothing is forgotten before the
  receiver has answered: a sensor cleared on this side alone would get the retained complaint
  back at the next reload.
- **While the receiver is offline the button is unavailable**, like every button that sends a
  command, and "Ostatni błąd" keeps what it shows; Home Assistant never writes to the receiver's
  topic itself. A receiver that goes away between the press and its answer raises "The receiver
  did not carry out `clear_error`" and clears nothing. The one refusal of its own - while an update is being
  applied - is raised in the receiver's sentence, and that refusal is then what "Ostatni błąd"
  shows. At the moments of an update when the plugin publishes nothing at all, the press ends
  as unanswered instead. A new complaint that arrives right behind the receiver's clearing
  stays on the sensor.

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

**The card offers what it will install, and only when it can.** Without SSH credentials there is
no install path, so `latest_version` is the installed version and there is **no update badge** -
the summary says which version is bundled and how to get an install path. This is a change from
0.3.1, which showed the bundled version as an update it could not install. With credentials,
`latest_version` is the bundle whenever its release number is higher than what the receiver runs,
it is not below the signed index's floor (this integration's own, 0.2.0, while no index is known),
and the index has not withdrawn it; a bundle the rule refuses is named in the summary with the
reason, and never installed - by the card, or by the guided installer updating an older plugin,
which ends with the same reason in words.

**Versions are shown with their build.** From plugin 0.4.0 on the receiver reports
a build id on `info.build`, and the card shows a development build as `0.3.0+g1a2b3c4` (with
`.dirty` when its tree was not clean) and a release build as `0.3.0`. **The card compares release
numbers only.** A higher number is an update. The same number is never one, in either direction:
a development build of 0.3.0 may be newer or older code than the 0.3.0 release, so the card does
not offer the release over it - and does not call it current either: the summary says „Wersja
rozwojowa; dostępne wydanie 0.3.0." (*Development build; release 0.3.0 available.*), and the
attributes `development_build` and `same_version_release` say the same. A development build
bundled in a candidate integration is not offered over an installed release of its number
either. Either is installed only when chosen by name: in *Wersja wtyczki do instalacji* (below),
which then offers it on the card, or with `update.install` and that version. The builds' commit,
time, flavour and dirtiness are in `installed_build` and `bundled_build`, as information. A
plugin up to 0.3.x reports no build id and is shown as the version it reports.

**What the card installs.** With SSH credentials stored, the newest release the signed index
lists that this integration may install - or the bundle, when nothing newer is listed; a release
comes before a development build of its own number. Its versions also appear in the summary and in
the attributes - `available_versions` (newest first, each with whether this integration may offer
it and why not; not recorded), `published_version`, `index_serial`, `index_age` (whole days since
it was built), `last_check`, `check_error` and `update_path`. Without SSH credentials they are
information only. Nothing on this card reaches the internet unless somebody asks for a check, the
daily check is on, or somebody presses install.

**Installing a release of the index** downloads it from the plugin's fixed origin - the same one
the index comes from, verified TLS, no redirect, at most its signed size - and believes it only
at the signed size and sha256, with a control file naming this package and this version. When the
bundle is those very bytes, nothing is downloaded. Once per version a day, GitHub's own `digest`
of the release asset is compared with the signed checksum: a disagreement refuses; an API that
cannot be reached, is rate-limited or has no digest is noted in the log and does not refuse. All
of that happens before the receiver is connected to, so a refusal - „Nie udało się pobrać wtyczki
{version} albo jej suma kontrolna się nie zgadza. Na dekoderze nic nie zmieniono." - leaves it as
it was. Then the SSH installer runs as for the bundle. Before it connects, it asks the whole rule
again against the newest index held - withdrawn, floor, contract, `min_integration`, and the same
size and sha256 as the bytes it holds - because a check in between may have accepted a newer
index; a version that no longer passes is refused with the receiver untouched. Once connected, and
before it takes the lock or a snapshot, it asks the receiver with `opkg status` for every package
the release `depends` on, and a refusal names every missing one. `opkg status <name>` sees only a
package of exactly that name: a dependency that another installed package merely `Provides:` is
reported missing, although opkg itself would accept it - the check the design prescribes, and the
safe direction, since nothing on the receiver changes. The card never installs an older version
than the one running.

**A receiver that updates itself is updated over MQTT, and only that way** (ADR-0008 §4). From
plugin 0.4.0 on, a receiver whose `info` names the capability `self_update` and
whose own setting `update_allowed` is on (set on the receiver, or ticked in the guided installer -
never over MQTT) is updated by the plugin itself: `update_path` is `mqtt`, whatever SSH
credentials are stored. Pressing install downloads and verifies the release as above, serves it
on a relay address bound to the receiver (as for a receiver without internet, below), and sends
`cmd/update` with the version, its signed sha256 and that address - so the receiver needs no
internet. The card then shows the receiver's own progress (downloading, verifying, snapshot,
installing, restarting, proving; a rollback without a percentage) and ends when the receiver says
how it ended: success only when it reports `installed` **and** runs the target with its signed
commit. Every refusal and every end is said in a sentence - „Na dekoderze trwa już instalacja,
aktualizacja albo usuwanie wtyczki.", „Dekoder nagrywa albo za chwilę zacznie nagrywać. Spróbuj ponownie po
zakończeniu nagrania.", „Nowa wersja wtyczki nie uruchomiła się. Dekoder przywrócił poprzednią
wersję 0.3.0." and the others - and **none of them is retried over SSH**. The card follows a
transaction for at most 25 minutes (the receiver's own worst case is under 23); after that it says
„Aktualizacja wciąż trwa na dekoderze." rather than that it failed. Over MQTT only releases the
signed index lists are offered, never the bundle unless it is such a release, and never an older
version: a downgrade stays in the options flow, over SSH. With no index held there is nothing to
install and no badge - press *Sprawdź aktualizacje wtyczki* first.

**Which path the card takes, in short.** `update_allowed` is off as the plugin ships, and an
update keeps whatever the receiver had; only the guided installer's tick box (*Allow remote plugin
updates*, [§3](#3-configuration)), when a receiver is added, writes it from Home Assistant. So
on most receivers the card installs **over SSH** when SSH credentials are stored (`update_path`
`ssh`) - a receiver already on plugin 0.4.0 included - and has no install path without them. To
have it update **over MQTT** instead, switch `update_allowed` on at the receiver: its setup screen
(*Menu -> Plugins -> MQTT Bridge*) or the plugin's OpenWebif page. From then on the card never uses
SSH for it: `update_path` reads `mqtt` once Home Assistant holds a signed index, and until then it
is empty and the card installs nothing, SSH credentials or not. While the card's `index_serial` is
empty, press *Sprawdź aktualizacje wtyczki* once: over MQTT only releases of a signed index are
offered, and Home Assistant fetches none by itself unless the daily check is on.

An update the receiver reports as **interrupted** is said by the evidence there is, since the
result alone does not say whether the receiver's package manager had already run. The receiver's
own words come first. When they say it had started, or that the plugin must be installed again
(on the update topic or on *Ostatni błąd*), the card says the plugin's files may be a mix of two
versions and names **„Wymuś reinstalację wtyczki (SSH)"**, or the manual installation when no SSH
credentials are stored. It says nothing was changed when the receiver says its interface restarted
before the update was installed; that the previous version stays when the update was stopped by a
signal (its files were untouched or put back whole); and that the files are whole - the previous
version or the new one - when another process took the update's lock, in which case *Ostatni błąd*
may ask for the receiver's interface to be restarted. Only then do the phases count: if Home
Assistant saw `installing` or a later one, the files may be a mix. Anything else it cannot tell,
and says so, naming the same repair for when *Ostatni błąd* asks for it: a phase seen before
`installing` proves nothing, because the next one can pass between two of the receiver's polls.
When a failed update could not put the previous files back (`restore_failed`, `restore_incomplete`),
the card names the forced reinstall only when SSH credentials are stored, and the manual
installation otherwise. If the silence after the request lasts a minute, the card says
so, and the download address stays valid for its ten minutes in case the receiver starts late.
Saving the integration's options reloads it: an install that was being followed then ends at once
with „Home Assistant przeładował integrację w trakcie aktualizacji, więc to zlecenie już jej nie
śledzi. Przebieg aktualizacji na dekoderze pokazuje karta." - the receiver goes on, its download
address stays, and the card shows its progress from the `update` topic.

**Updates the receiver runs by itself** - started at the television, on the plugin's OpenWebif
page, or by another broker client - appear on the card too: as running only while they are not
finished and started within the last 25 minutes (by the receiver's clock, at most a minute ahead
of Home Assistant's), and in the attribute `receiver_transaction` (`state` is `in_progress`,
`finished` with the end in words in `message`, or `stale`); a card with no install path has no
progress bar, and shows it in the attribute only. Beside a plugin that says it cannot update
itself - an older one, or one its package manager did not install - a phase before `installing`
is `stale` at once, because only the plugin that asked reports those. A later phase is `stale`
once the update topic has stood still for ten minutes: a plugin from 0.4.0 on that started in
the middle of an update reports it to its end without claiming that it can update itself, and
says nothing while the receiver's helper proves it or puts the previous version back. The price
is that a phase nobody will move any more - its helper was killed, say - keeps the card busy for
those ten minutes. The count is kept when the integration reloads and starts again when Home
Assistant restarts, so each restart can add up to ten minutes, never past the 25. Removing
the receiver from Home Assistant drops the count. A refusal of
such an install that only
the receiver sees - no internet and no answer from Home Assistant, a clock that calls Home
Assistant's address expired - is in `last_refusal`, in the household's language, for as long as it
is on the receiver's `last_error`: the next command that succeeds clears both. The end of a
transaction, which the receiver repeats on `last_error`, is never a refusal there; it is said once,
in `receiver_transaction`. The receiver repeats an end once, together with the end on the update
topic; it also publishes that topic for other reasons, so only the message that first carried
the end counts. A refusal in the very words of the last end - a second install refused for the
same reason soon after, even one that follows an unrelated update message - is still shown as a
refusal. A phase on the
broker that keeps the card busy cannot keep **„Wymuś reinstalację wtyczki (SSH)"** from running:
it asks the lock on the receiver, never this topic.

**A receiver without internet** can still install a release from its own screen: it asks this
Home Assistant on `relay_request`, and Home Assistant - holding a verified index that lists the
version, under the same rule as the integration's own installs (a downgrade the receiver asks for
is answered too) - downloads and verifies the package and answers on `cmd/relay` with an address
on itself, `/api/enigma2_mqtt/relay/<token>`. That address serves nothing but those bytes, to
nobody but that receiver (by the IPv4 address it reports on `info.ip`), for ten minutes; the
receiver verifies them again against its own signed index before installing. Asking again for the
same version while its address is live gets the same address and downloads nothing; a new
download is made at most once a minute per receiver.

The address is Home Assistant's own on the receiver's subnet, else its *Local network* URL
(Settings -> System -> Network), never the external URL; the log says which at INFO. It must be an
address the receiver accepts - `http` or `https`, an IPv4 address or a host name of letters,
digits, dots and hyphens, a port - so a *Local network* URL with an IPv6 address, an underscore in
the host name or a user name and password is not offered: Home Assistant uses its own IPv4 address
instead when it serves plain HTTP, and otherwise says that the receiver could not download the
plugin. A user name and password never reach the broker.

**Behind a reverse proxy the binding sees the proxy, not the receiver**, and refuses it: give the
receiver Home Assistant's direct LAN address. The binding is exactly as strong as Home
Assistant's `http:` settings `use_x_forwarded_for` and `trusted_proxies`, because Home Assistant
takes the requesting address from `X-Forwarded-For` only for a proxy listed there:

- a trusted proxy that passes a client's `X-Forwarded-For: <receiver's address>` on unchanged
  makes that client the receiver, and it is served;
- a trusted proxy that appends the real client, as a correctly configured one does, is judged by
  the real client, and anybody else gets `404`;
- without `use_x_forwarded_for`, Home Assistant refuses a request that carries the header with
  `400` before this integration sees it. `X-Real-IP` and `Forwarded` are never read.

So keep `trusted_proxies` to the proxy itself - a range that also covers other hosts (a whole LAN,
a Docker network) lets any of them claim to be the receiver. What they get is the public signed
package and nothing else.

A receiver that reports no usable IPv4 address is not answered. When Home Assistant has accepted a
request and cannot answer it - no IPv4 address for the receiver, no address of its own the receiver
can use, the download failed - it says why in a notification, one per receiver. When an answer
expires without a single download, the notification says only that: the plugin was offered and not
downloaded within ten minutes. Home Assistant cannot tell a receiver that could not reach the
address from one that declined for a reason of its own, or from a request any broker client
forged. A version the rule refuses is only logged. The receiver's own screen only says that
Home Assistant did not answer.

A new download is made at most once a minute per receiver, so a broker client that asks first for
another eligible version every minute can take that minute each time and keep the receiver from
being answered. Such a client can deny the install more cheaply anyway - by answering the
receiver's request on `cmd/relay` itself before Home Assistant does - and the cost to Home
Assistant is one verified download a minute per receiver. Anything a broker client can repeat is logged as a warning once per
receiver in ten minutes and at debug level after that. The full address, token included, appears
in no line this integration writes; Home Assistant's own MQTT debug log shows every message it
carries, this one too.

**Check for plugin updates** (*Sprawdź aktualizacje wtyczki*, `button.<device>_sprawdz_aktualizacje_wtyczki`
on a Polish installation) asks for the index now, at most once in ten minutes; inside that it
says when the list was read and when it can be read again. **Plugin version to install**
(*Wersja wtyczki do instalacji*, a select, **disabled by default**) holds a receiver on one version:
its first option, *Najnowsza zgodna* (`latest`), is the newest version the card can install, and it
is what counts while the select is disabled. Its other options are the versions the card can
install that differ from the one running: a higher release number - the bundle's or a release of
the index - or a build of the same number. Choosing a same-number build is the one way the card offers it. A choice is stored in the entry's options
and survives a restart; changing it moves `latest_version`, which makes Home Assistant forget a
skipped version.

**„Wymuś reinstalację wtyczki (SSH)"** (*Force plugin reinstall (SSH)*, `force_reinstall`,
diagnostic, **disabled by default**; `button.<device>_wymus_reinstalacje_wtyczki_ssh` on a Polish
installation) is the recovery and bootstrap path: it reinstalls the plugin package bundled with
this integration over SSH and needs nothing from the plugin - so it works when the plugin is
dead, switched off, or too old to update itself. **It exists only while SSH credentials are
stored**: *Configure* -> tick „Skonfiguruj dane SSH do aktualizacji wtyczki" and enter them, then
enable the button on the device page. Forgetting the credentials (untick „Zachowaj dane SSH do
aktualizacji") removes it from the entity registry; enrolling again brings a new button, disabled
again - and on a Polish installation with an area, with the area in front of its entity id. It is
available while the receiver is offline over MQTT.

- **Two presses.** Home Assistant has no confirmation for a button, so the first press arms it for
  30 seconds and posts the confirmation as a notification, naming the bundled version - and
  saying so when it is older than the plugin installed. The first press raises nothing. A second
  press by the same administrator inside the 30 seconds runs the reinstall. Only administrators:
  a press by anybody else - and by an automation, which runs without a user even when an
  administrator triggers it - posts a notice and does nothing else, so an automation can never
  confirm it. A **script** runs as the user who started it, so a script with two presses, started
  by an administrator, confirms in one run: that is the same administrator's explicit act.
  A press while a reinstall runs starts nothing and posts a notice saying so.
- **Standby.** While the receiver's interface is running, the receiver must be switched on, not
  in standby: the restart would wake it, and HDMI-CEC may switch the television on with it, so the
  reinstall is refused before anything changes. Only when the interface is not running - a
  respawn loop, or runlevel 4 left by an interrupted install of this project - does the reinstall
  start it; the confirmation says both.
- **What it does.** Always SSH, never MQTT, and only the bundled package, over any version
  installed (`--force-downgrade` when opkg's records name a newer or unreadable version). It
  reads the interface's state over SSH before any guard that needs OpenWebif: a running
  interface gets the usual recording, timer, standby and streaming guards and the clean restart;
  one whose OpenWebif does not answer is refused, since whether it records cannot be known;
  a respawn loop is installed without those guards (nothing records without enigma2) and started
  with `init 4` and `init 3` - the new interface then starts any timer that became due. Ten
  seconds of samples can mistake a box that is still starting for a respawn loop, so the
  interface is looked at again immediately before it is stopped or started, and one that runs by
  then gets every guard and the clean restart. Runlevel 4 left by an interrupted install of this
  project - a stop-and-restore cut off in this boot - is recovered and started once the lock is
  taken, and a failure after that still starts it. A refusal before the lock - no space, an old
  Python, another receiver, the lock held - starts nothing, because without the lock nothing
  says the interrupted transaction has ended; its message says the interface stays stopped and
  that switching the receiver off and on, or running the reinstall again once the cause is
  dealt with, starts it. A connection to the receiver lost during those checks is such a
  refusal too. One case is told otherwise: when the lock belongs to a plugin self-update whose
  heartbeat is fresh, that update is rolling back right now and starts the interface itself when
  it finishes, so the message says to wait for it and not to switch the receiver off, which
  would cut its restore off half-way. Runlevel 4 with no such stop of this project's is left
  alone. It changes
  no setting of its own - a plugin switched off stays off - and its proof is a new interface
  process holding the plugin's log open, plus the announcement when the plugin is switched on.
- **The shared lock decides, not a topic.** It claims the receiver's install lock like every
  install, by the 30-minute rule every installer and the plugin share: a lock whose holder is
  alive refuses it, and it never takes a lock back early - a plugin self-update that stopped
  without finishing holds its lock until 30 minutes after its last heartbeat, and the refusal says
  in how many minutes. A stale or forged `update` phase on the broker does not block it.
- **Result.** The update card shows it running. Success posts a notification - or "installed, but
  switched off in its settings on the receiver". A failure raises its reason and puts it on
  „Ostatni błąd" with `source: home_assistant`, because the plugin may never publish again -
  an unexpected error too, named by its type, with the details in Home Assistant's log.
- **When a restart is the repair, not a reinstall.** When the plugin says its update could not
  stop the interface and asks for the receiver's interface to be restarted (`not_stopped`), the
  files on the receiver are already the right ones: restart the interface („Restart GUI", or from
  the receiver's menu). Reinstall only when the plugin asks to be installed again
  (`restore_failed`, `restore_incomplete`), does not start, or does not answer.
- **Bootstrap for plugin 0.2.0 and 0.3.0**, which cannot update themselves: the update card's SSH
  path is the way onto the first self-updating release; this button is for when the card cannot
  act.

### 4.6 Selects

| Key | Polish name | unique_id | Topics | Command |
|---|---|---|---|---|
| `bouquet` | *Bukiet* | `<node_id>_bouquet` | `channels`, `bouquet` | `cmd/bouquet` |
| `channel` | *Kanał* | `<node_id>_channel` | `channels`, `bouquet`, `service` | `cmd/zap` |
| `zap_history` | *Ostatnio oglądane* | `<node_id>_zap_history` | `zap_history`, `service`, `channels` | `cmd/zap_history` |
| `zap_history_all` | *Ostatnio oglądane (wszystkie)* · **disabled by default** | `<node_id>_zap_history_all` | `zap_history`, `service`, `channels` | `cmd/zap_history` |
| `plugin_install_version` | *Wersja wtyczki do instalacji* · diagnostic · **disabled by default** | `<node_id>_plugin_install_version` | `info` | none - stored in the entry's options (see the update entity) |

„Bukiet" and „Kanał" exist **only while the receiver names the `channels` and `bouquet_context` capabilities**.
A plugin that cannot switch a channel-list context has nothing for either of them to do, and a
receiver that loses the capability has them taken out of the entity registry rather than left
behind unavailable. A receiver that has not said what it can do yet keeps whatever it has -
silence is not "no".

**Where the channel list comes from.** Up to plugin 0.4.0 it is the `channels` topic: every
bouquet with its channels in one payload. From plugin 0.5.0 on the receiver also publishes the
`bouquets` topic - the index of its bouquets - and one `channels/<bouquet_slug>` per bouquet,
and names the capability `channel_topics`; on a receiver whose list is too big for one MQTT
packet `channels` is then not published at all. While the capability is named and the index
has arrived, the per-bouquet topics are what everything below reads - the two selects, the
media player's sources and browser, zapping by name, the [options](#options) and the filter
behind „Ostatnio oglądane" - and they win where the two disagree. Otherwise `channels` is read,
exactly as before. Three things follow from topics that arrive one at a time:

- **The list appears whole.** Nothing is offered bouquet by bouquet while the topics arrive.
  When every bouquet of the index has its list, the list is shown at once; if one is still
  missing five seconds after the last message, the list is shown as far as it goes.
- **A bouquet without its own topic stays a bouquet.** When the receiver could not publish one
  bouquet's list - it is too big for a packet by itself, and named in the repair - „Bukiet"
  still offers the bouquet and „Kanał" is empty while the receiver is on it.
- **A bouquet whose name has no ASCII letter or digit has no topic of its own.** The topic's
  name is made from the bouquet's name, and a name written only in Cyrillic, Greek, Arabic,
  Hebrew or CJK characters leaves nothing to make it from. The plugin lists such a bouquet in
  the index and publishes its channels on `channels` only, so the integration reads that one
  bouquet from `channels` - and keeps `channels` in memory for it, beside the per-bouquet
  lists. On a receiver whose `channels` is too big to be published there is nowhere to read it
  from: the bouquet is offered without channels, and the repair names it
  ([§8](#8-troubleshooting)). The way out is on the receiver - narrow
  `bouquets_for_select` until `channels` fits one packet again, or give the bouquet a name with
  a Latin letter or a digit in it.
- **„Ostatnio oglądane" fails toward hiding.** While a bouquet named in the hide option is on
  the list without its channels, the filtered list shows nothing at all.

After a plugin is put back to 0.4.0 the capability is gone and `channels` is read again; the
per-bouquet topics the newer plugin left on the broker are ignored. The integration asks the
broker for `channels` once more at that point and keeps the list it was showing meanwhile; a
broker that has no `channels` to answer with ends that after about ten seconds, and the list
is then empty until the receiver publishes one.

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
**Until a history arrives they are `unknown` with no options**, not unavailable - like
„Klienci strumienia". The plugin empties the retained topic when it withdraws the history, and a
broker then holds nothing on it, so after a restart of Home Assistant no payload arrives at all;
that is the same answer as the empty payload, and both are `unknown`. They are unavailable
while the receiver is offline, and while it does not name the capability unless it has published
a history since Home Assistant started that it has not withdrawn since. An empty payload is not
a history: a plugin downgraded to one without the capability withdraws the topic, and both
selects are unavailable at once.

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

### 4.7 Streams

Every enigma2 image serves the transport stream of a service over HTTP to whoever asks.
Plugin 0.5.0 publishes what is needed to use that - where the stream server is, who it is
serving, whether a service could be played right now - and this integration turns it into an
address, a media source and, if you want it, one stream of your own go2rtc that follows the
channel. **It starts no stream itself, carries no video and makes no camera entity**: the
reasons are in [ADR-0011](docs/adr/0011-stream-facts-addresses-and-a-media-source.md).
Everything in this section exists only for a receiver whose `info` names the capability beside
it.

**The limits come first, because they decide how to use any of it.**

- **A receiver serves as many streams as it has free tuner inputs** - two on a dish with two
  inputs, and the television and every recording take theirs first.
- **An open stream holds its tuner for as long as it is open.** A card that opens the stream
  whenever a dashboard is shown takes a tuner from the next recording. Open a stream on a tap,
  and never preload one.
- **A receiver's stream is MPEG-TS**, usually H.264 or HEVC with MPEG or Dolby audio, often
  interlaced. A browser and a Chromecast play none of that as it is.
- **A channel that is not being descrambled still answers `200`** and then sends nothing a
  decoder can use. A player or an `ffmpeg` without a timeout of its own waits for ever.
- **The receiver's own hardware transcoder is not used here and not recommended**: few
  receivers have one, and its output is the least compatible of the options.

#### The stream address and the stream name (`stream`)

For a receiver that claims `stream`, the media player has these attributes:

| Attribute | What |
|---|---|
| `stream_url` | The address the receiver serves the channel on the decoder at, or `null` |
| `stream_name` | A stable name for that channel's stream, or `null` |
| `stream_authentication` | `true` when the receiver's stream server wants a login, `false` when it does not, `null` when the image has no such setting |
| `go2rtc_stream` | The name of the stream your own go2rtc is kept on this channel under - only while a go2rtc address is set in the options |

**How the address is built:** `http://<host>:<port><path>`. The port and the form of the path
are the receiver's own, from the `stream` topic (on every image today `8001` and `/<service
reference>`). The host is the one Home Assistant asks for picons, by the same rule
([§4.1](#41-media-player), *Which address is asked*): the receiver host typed when the receiver
was added, if one was - and never the reported address then - and otherwise the address the
receiver reports, when it is a plain IP address. The service reference goes in the way the
receiver's stream server reads it: **colons kept, the trailing colon included**, and every
other character that is not plain percent-encoded:

```
1:0:19:283D:3FB:1:C00000:0:0:0:   ->   http://192.0.2.12:8001/1:0:19:283D:3FB:1:C00000:0:0:0:
```

**`stream_url` is `null`, and no address is built at all,** when:

- the receiver's stream server wants a login (`stream_authentication` is `true`). **No address
  is ever built with credentials in it**, and this integration stores none. Switch the login
  off in the receiver's streaming settings to use anything in this section;
- the channel belongs to a bouquet the options hide - then `stream_name` is `null` too;
- there is no usable address for the receiver: a typed host that cannot be the host of a web
  address, or no typed host and a reported address that is not a plain, ordinary IP address;
- nothing is playing (standby), or the receiver has published no usable `stream` payload.

**The stream name** is derived from the service reference and from nothing else: lower-cased,
every run of characters other than `a`-`z` and `0`-`9` collapsed to one `_`, the ends stripped,
and `ch_` in front.

```
1:0:19:283D:3FB:1:C00000:0:0:0:   ->   ch_1_0_19_283d_3fb_1_c00000_0_0_0
```

It is the transform existing go2rtc configurations with one stream per channel already use,
and it will not change. In a template: `{{ state_attr('media_player.dekoder_salon',
'stream_name') }}`.

#### The media source

*Media -> Enigma2 MQTT* lists the receivers that claim `stream`, then a receiver's bouquets,
then a bouquet's channels. A channel resolves to the receiver's stream address with the type
`video/mp2t`. It is the channel list the integration already holds - nothing is fetched for
it - narrowed the way the media player's own browser is by **Bouquets to offer**, and without
the hidden bouquets and their channels.

It is for **players that decode a transport stream themselves**: Kodi, VLC, an Android TV
player. The player fetches the stream from the receiver; nothing passes through Home Assistant.

```yaml
action: media_player.play_media
target:
  entity_id: media_player.kodi_salon
data:
  media_content_id: media-source://enigma2_mqtt/vuuno4kse_005301/c/1:0:19:283D:3FB:1:C00000:0:0:0:
  media_content_type: video/mp2t
```

The address of a channel is `media-source://enigma2_mqtt/<node_id>/c/<service reference>`, of a
bouquet `.../<node_id>/b/<bouquet reference>`, with the reference percent-encoded and its
colons kept. A channel resolves only while it is in the tree.

Resolving is refused, with a sentence in your language, when:

| When | Why |
|---|---|
| the player is the browser's own | a browser does not play MPEG-TS. Home Assistant resolves without naming a media player only for the browser, which is how it is told |
| the player is a Chromecast | the same; told by the `cast` platform its entity belongs to |
| the receiver has published nothing usable about its stream server | no `stream` payload has arrived, or its port or path cannot be used; check that the plugin is running - it publishes `stream` at every connect |
| the receiver's stream server wants a login | no address is built with credentials |
| there is no address to reach the receiver at | type the receiver's host with *Reconfigure* |
| the receiver says the channel cannot be played now | see below |
| the receiver, the bouquet or the channel is not in the tree | removed, not offered, or hidden |

**Browsing is shown to every player**, including those two: a browse request does not say
which player is asking, so the refusal can only come when a channel is picked. Any other
player is taken at its word - one that cannot decode the stream fails in its own way.

**Can it be played right now?** A receiver that claims `stream_playable` is asked just before
an address is handed out (`cmd/stream_playable`), and the answer is matched by the command's
id. "No" means the channel needs a tuner that the television, a recording or another stream is
holding, and the channel is refused with that sentence instead of a player opening a stream
that never starts. **No answer within three seconds is not a "no"**: the address is handed out
and the player finds out. The answer is true for the moment it was asked - a recording that
starts a second later takes the tuner it counted on. A receiver that is offline is not asked.
Only one playable service is asked about - never a bouquet, a marker or a hidden entry, which
the receiver would refuse; the channel list holds none of those anyway. A refusal of the
question is not a "no" either: the address is handed out.

#### Stream clients, and stopping one (`stream_clients`, `stream_stop`)

„Klienci strumienia" ([§4.3](#43-sensors)) counts the clients, and its attribute `clients`
lists them:

| Field | What |
|---|---|
| `host` | the client's address |
| `sref`, `name` | the channel it is being sent |
| `bouquet` | the first of the receiver's configured bouquets that holds that channel, `null` when none does |
| `transcoded` | whether it is served through the receiver's encoder |
| `hidden` | whether this integration left the channel out of the row |
| `can_stop` | whether `stop_stream` can end this row: the receiver claims `stream_stop`, and the row has a reference. The plugin lists a client whose reference it holds back (one longer than 512 characters, or none) with `sref`, `name` and `bouquet` `null`; nothing can name such a client, so nothing can stop it |

The list holds only while the receiver is online. The plugin retracts nothing when it stops,
so the retained list of a receiver that is off is a list of clients that have gone: the sensor
is unavailable then, like every other entity, and `stop_stream` finds no client - also when
it is given a reference, because nobody would answer the command. An emptied topic - the
plugin could not read the image's list - is `unknown`, never "no clients", and so is a topic
the broker no longer holds; only a published empty list is zero.

**The plugin publishes every client; hiding is done here.** While the option **Bouquets hidden
from "Recently watched"** names a bouquet, a client on a channel of such a bouquet is shown
with its `host` and `transcoded` and with `sref`, `name` and `bouquet` `null`, and `hidden` is
`true`. The rule is the one that option's filter applies to the zap history: a channel is
shown only when the bouquet the receiver names for it is a published bouquet that is not
hidden **and** the channel is not a member of any hidden bouquet. It fails toward hiding in the
same cases - no bouquet named, a bouquet the plugin does not publish, a hidden bouquet that is
listed without its channels. The count is never reduced: a hidden client holds a tuner like any
other. 🔴 As with that option everywhere, this is a filter in Home Assistant and **not
privacy on the network**: the receiver publishes every client with its channel, retained, to
any broker login, and its own OpenWebif page shows them all.

`enigma2_mqtt.stop_stream` ends one client's stream. The action is there for every receiver's
media player, and works for one that claims `stream_stop`; for any other - an image whose
stream server lists its clients and cannot stop one claims only `stream_clients` - it is
refused before anything is sent.

```yaml
action: enigma2_mqtt.stop_stream
target:
  entity_id: media_player.dekoder_salon
data:
  host: 192.0.2.20
  sref: "1:0:19:283D:3FB:1:C00000:0:0:0:"
```

- `host` is the client's address as the sensor shows it. Upper and lower case, and the
  image's own spelling of an IPv4 client (`::ffff:192.0.2.20`), name the same client.
- `sref` may be left out when that host has one stream open. **A hidden client is stopped by
  its host alone**: its reference is in no state, no attribute, no event and no log line of
  this integration, so nobody could type it. Home Assistant takes it from the receiver's
  payload, which it holds, and puts it into the command and nowhere else. When a host has both
  a hidden and a shown stream, the host alone means the hidden one - the shown one can be
  named. A host with two shown streams needs the reference and is refused without it:
  nothing here ever stops "all of them".
- **Two hidden streams to one host cannot be stopped from Home Assistant.** Neither reference
  is shown anywhere, so nothing could say which one is meant: both rows say `can_stop`
  `false`, and the host alone is refused with a sentence that says so. Stop them on the
  receiver, or take the bouquet off the hidden list first. One channel listed twice for a
  host, once transcoded or in two spellings, is one stream and stays stoppable.
- A reference is sent as it was given. The receiver first looks for a row of that host whose
  `sref` is spelled exactly so, and only then for one that is the same service by its first
  eleven fields - so of two rows of one channel in two spellings, copying the row's `sref`
  ends that row.
- The receiver answers at once and the connection ends a moment later; the list is read
  again two seconds after the command. **A player that reconnects by itself is back by then**:
  stopping a client is not blocking it.
- The receiver's refusals become this integration's usual errors
  ([§5](#which-error-a-refused-call-raises)): "that stream client is no longer connected" is a
  refusal (`ServiceValidationError`), "the receiver did not stop that stream client" a failure
  (`HomeAssistantError`). No permission on the receiver guards the command: whoever may publish
  to its command topics may already change the channel and put it into standby.

#### Your own go2rtc

go2rtc turns a transport stream into something a browser plays. If you run one - the go2rtc
add-on, for instance - set its address in the [options](#options), and Home Assistant keeps
**one** of its streams on the channel the receiver is on. That is all it does there: it does
not install, configure or query your go2rtc, and Home Assistant's own `go2rtc` and `stream`
integrations are not involved.

**What is sent.** When the service on the decoder changes, and once after start-up when the
first `service` has arrived:

```
PUT <go2rtc address>/api/streams?name=<stream name>&src=<source>
```

`<source>` is the source template with the receiver's stream address in the place of `{url}`.
With the defaults, for the example receiver on TVP 1 HD:

```
name = enigma2_vuuno4kse_005301
src  = ffmpeg:http://192.0.2.12:8001/1:0:19:283D:3FB:1:C00000:0:0:0:#video=h264#audio=opus#audio=aac
```

The same channel published again - the resolution became known, another audio track - sends
nothing. Zapping through channels sends the requests that had time to finish and ends on the
last channel. Going back to the channel go2rtc last took sends nothing either, while every
request has worked; **while a failure is pending**, every channel change sends a request,
also one back to that channel, because what go2rtc holds after a failure is not known - and
the one that works clears the repair.

**When it does not re-point**, and the stream stays on the last channel it was given:

- in standby and while the receiver is offline, as the media player keeps its last picture;
- for a channel of a hidden bouquet. Pointing the stream at nothing was considered and
  rejected: it is a second kind of request that says "something hidden is on", and it breaks
  the card for the channel that was fine;
- while the receiver's stream server wants a login.

**What is known about go2rtc**, measured against go2rtc 1.9.14:

- **A source that contains a space is refused** with HTTP 400, "source with spaces may be
  insecure". That is why the options form refuses a template with white space, and why a
  filter with spaces has to be a **named template** in your own `go2rtc.yaml`.
- **go2rtc cannot read the receiver's stream port without `ffmpeg:`** in front of the address.
- **WebRTC needs `#audio=opus`; MSE takes AAC.** The default template asks for both.
- **Every channel change rewrites the stream's entry in go2rtc's configuration file.** A
  request that changes the source makes go2rtc save it there; the same source again leaves the
  file alone.
- **A request can be answered 400 and still have worked.** When go2rtc cannot write its
  configuration file - a `streams` key written in flow style, such as `streams: {}`, is one
  measured case - it answers 400 (`yaml: line 7: did not find expected key`, or similar), after
  it has already re-pointed the stream in memory. So on any answer that is not a 2xx, Home
  Assistant reads the stream back (`GET <go2rtc address>/api/streams?src=<stream name>`, with
  the same limits as the request). When the stream is there with exactly one producer, and
  that producer has exactly the source that was sent, the channel change counts as done: no
  repair, no retry, and **one** warning in the log - once until the entry is reloaded (saving
  its options reloads it) or Home Assistant restarts - saying that go2rtc could not save its
  configuration, with the status and the start of go2rtc's answer. Of that answer at most 512
  bytes are read; it is percent-decoded (at most twice); then the source, the receiver's
  stream address, the service reference and the receiver's host (as Home Assistant reaches it,
  as typed and as reported) are removed wherever they appear, in any case; then only the first
  line is kept, written IPv4 and IPv6 addresses and anything with a scheme (`http://...`,
  `ffmpeg:...`) are removed, and the rest is cut to 80 printable ASCII characters. A host name
  that is not the receiver's can remain. go2rtc's file then still holds an older source, which is what the
  stream is after a restart of go2rtc until the next channel change; writing `streams:` in
  block style, or removing it, ends that. A read-back that fails or shows another source
  leaves the failure as it was (below).
- **A card or player that is already open keeps showing the previous channel.** After a
  re-point, a viewer that was connected stays on the old source - and go2rtc keeps the
  receiver's stream of that channel open, and with it a tuner - until the viewer reconnects;
  go2rtc lists it under no consumer meanwhile. Measured with an HTTP MP4 viewer
  (`/api/stream.mp4`) on go2rtc 1.9.14. **Open the card after the channel change**, on a tap,
  rather than leaving it open across one.

**A recipe.** In your `go2rtc.yaml` (the add-on's configuration), a named template that
deinterlaces - most broadcast channels are interlaced, and a browser shows the combing - and
an input line with a read timeout, so that a channel that is not being descrambled does not
leave an `ffmpeg` waiting for ever:

```yaml
ffmpeg:
  # A custom video template needs its own -map 0:v:0.
  tv: "-map 0:v:0 -c:v libx264 -preset:v superfast -tune:v zerolatency -vf yadif -g:v 50"
  # The input side for http sources, with a ten-second read timeout (microseconds).
  http: "-rw_timeout 10000000 -fflags nobuffer -flags low_delay -i {input}"
```

Then, in the receiver's options, the source template `ffmpeg:{url}#video=tv#audio=opus#audio=aac`.
Those two lines are an example to adapt, written from ffmpeg's and go2rtc's documentation;
this project measured the refusal of spaces and the need for `ffmpeg:`, not these templates.

On a dashboard, show the stream **by its name through the go2rtc add-on's ingress path**. A
page of Home Assistant served over HTTPS may not load `http://<host>:1984/...` - the browser
blocks it as mixed content - and the ingress path is on Home Assistant's own address:

```yaml
type: iframe
url: /api/hassio_ingress/<the add-on's ingress token>/stream.html?src=enigma2_vuuno4kse_005301&mode=webrtc,mse
aspect_ratio: "16:9"
```

The ingress token is the part after `/api/hassio_ingress/` in the address of the add-on's own
*Open Web UI* page. The stream name is the one in the options, and the media player's
`go2rtc_stream` attribute. **Put that card where it opens on a tap** - a subview, a popup - and
not on a dashboard that is always on a wall: the page starts playing when it loads, go2rtc
opens the receiver's stream for it, and that stream holds a tuner until the card is gone. A
card left open across a channel change also goes on showing the channel it opened (above).

**When it fails** - go2rtc does not answer within ten seconds, or answers anything but a 2xx
that the read-back above does not confirm -
Home Assistant logs one warning and raises one repair for the receiver, naming the host and
the HTTP status. Both are said **once**, not at every channel change - also when the failure
changes, from one status to another or to no answer at all; the one repair then says the
latest - and the repair goes by itself with the first request that succeeds. A failed request is tried again at the next
channel change and every five minutes, always with the channel the receiver is on at that
moment, never an older one. The address is named by its host alone in the log and
in the repair, and is redacted in the diagnostics download: an address you typed can carry a
user name and a password for go2rtc's API. Emptying the address switches all of it off and
removes the repair. **The stream itself stays in your go2rtc**, under its name and on the last
channel it was sent: this integration does not delete it. Delete it there if you no longer
want it - in go2rtc's web interface, or with
`DELETE <go2rtc address>/api/streams?src=<stream name>` on its API.

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
| `enigma2_mqtt.stop_stream` | `host`, `sref` (optional) · on every receiver; refused before anything is sent unless it names the `stream_stop` capability ([§4.7](#47-streams)) |

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

**Whose refusal it is.** `last_error` names the refused command, and until plugin 0.5.0 that
was all there was to go by: a refusal of `zap` failed every zap being waited for, whoever had
sent the one refused. A plugin that claims the capability `command_id` is sent each command in
an envelope with a fresh random id - `{"id": "<16 hexadecimal digits>", "payload": "<the
command's payload>"}` - and says the id back on `last_error` when it refuses that command.
Then:

| `last_error` says | The waiting command is failed by it |
|---|---|
| the command's own id | yes - also when the receiver reports it under another name |
| another id, or any id when the command went out without one | no: it is another client's command, or another of this integration's |
| no id | when it names the command, as before |

A refusal without an id still counts because it can be the command's own: the plugin drops an
id that happens to hold one of its passwords and runs the command without it, and a command
that would pass the plugin's 4096-byte limit only without the envelope is sent without one. A
complaint that arrives before the command has left Home Assistant is never its answer, with any
plugin.

The same rule decides the answer to an update over MQTT and to a removal of the plugin: a
second `cmd/update` or `cmd/uninstall` from another client, refused while Home Assistant's own
is carried out, no longer ends it as "refused". An install started at the television has no
id, so its refusal in the same minute can still be read as the answer to Home Assistant's.

Nothing changes for a plugin that does not claim the capability - 0.4.0 and older - or whose
`info` has not arrived, was retracted, or was last published as something that is not a JSON
object: no envelope, and every payload is what it always was. The question is asked again
for every command, so an update or a downgrade of the plugin takes effect with its next
`info`. `cmd/relay` is never in an envelope, and neither are the few commands sent where
nothing has read `info` - the mode switch of the config flow and of a
removed entry, and the installer's `cmd/reset` and `cmd/bouquet`. The payload inside the
envelope is always a string, the text an older plugin is sent as the whole payload, so a
command reads the same characters either way. The id is shown on no entity; the diagnostics
download carries `last_error` as it arrived, with it
([ADR-0010](docs/adr/0010-a-command-carries-an-id-and-a-refusal-is-matched-by-it.md)).

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

`stop_stream` waits the same way, for a complaint only. The stream server's client list is
republished two seconds after the command, and a player that reconnects by itself is on it
again by then, so "the row is gone" is not something the action can wait for without
reporting a stop that worked as one that failed ([§4.7](#47-streams)).

Zapping to the channel that is already on is still sent, but nothing is waited for: there is
no change coming, and pretending otherwise would mean waiting out the whole timeout.

### Which error a refused call raises

Every button, select, action and the update card answer a call that did not do what was asked
by raising, with a sentence in the household's language. There are two kinds, and both are a
`HomeAssistantError` to anything that catches one:

| Raised | When | Examples |
|---|---|---|
| `ServiceValidationError` | The call was **refused and nothing was done**, for a reason the caller can act on or wait out: the moment, or what was asked for | *Sprawdź aktualizacje wtyczki* pressed again inside its ten minutes; the zap history refusing to clear in standby or with one channel in it; an install refused for a recording, a timer about to start, standby, a stream, another install or the package manager running; a version the signed index withdraws or does not list; a downgrade |
| `HomeAssistantError` | Something was **tried and failed**, the receiver did not answer, or the refusal is the receiver's own English sentence without a reason code, which can be either | no answer within ten seconds; an install, restart or rollback that failed; a download that failed; a receiver that cannot be reached over SSH; `not_cleared` after the 0 key |

Over the WebSocket API - the one Home Assistant's own pages use - the answer carries the code
`service_validation_error` or `home_assistant_error` and the sentence's `translation_key`, so a
client can say the sentence in the household's language either way. Home Assistant logs one line
for both: the sentence alone for a refusal, and "Error during service call to ..." in front of
it for a failure, with the traceback at debug level only (Home Assistant 2026.9.2).

**The REST API answers 500 for both.** `POST /api/services/<domain>/<service>` turns only a
schema error and an unknown service into a client error (measured on Home Assistant 2026.9.2):
an action that raises either kind is answered `500 Internal Server Error`, and Home Assistant's
web server logs the traceback itself. That is Home Assistant's handling of every integration's
errors, and nothing an integration raises changes it. A script that calls these actions over
REST reads a 500 as "refused or failed" and finds the sentence in the Home Assistant log, or
uses the WebSocket API.

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
bouquet names and how many services each holds, not as the channel list - and so do the
`bouquets` index and each `channels/<bouquet_slug>` of plugin 0.5.0, under `bouquets` and
`bouquet_channels`. **`key`** does not
appear at all: it is who pressed what a moment ago, which is not state. **`zap_history`**
appears as the number of entries, `current`, `limit` and `panic_button` - no channel name and
no reference, whatever the hide option says.

`box.channel_list` says which of the two sources the entities are reading (`channels`,
`bouquet_topics` or `none`), whether every bouquet's list is in, and how many channels each
bouquet has here - `null` for a bouquet whose own topic is missing. `box.not_published` is the
receiver's list of what it did not publish, `null` for a plugin that does not report it, and
each `epg_grid` entry carries its `events_per_channel`.

`box.player_picture` gives the picture chosen for the media player and the state of the picon
lookup: `idle` (nothing asked yet), `ok`, `unreachable`, `refused` (the web interface asked for a
login), `unexpected` (it answered, but not as OpenWebif does), `error` (a fault in the lookup
itself, with a warning in the log) or `no_address`, the seconds until it is tried again, and how
many bouquets, picons and "no picon" answers are held. No address, channel or picture is in it.

`topics.stream` is what the receiver says about its stream server, normalised: the port, the
form of the path and three flags; there is no host in it. `topics.stream_clients` is three
counts - the image's count, the rows listed, and how many are transcoded - and never a
client's address or channel, hidden or not. `box.go2rtc` says whether your own go2rtc is kept
on the channel (`off`, `idle`, `ok` or `failed`), under which stream name, why the last
request failed and with which HTTP status, and how many requests were sent; the go2rtc
address in the options is redacted, because a typed address can carry a login.

One thing is stated rather than left to be worked out. The **`plugin`** block gives the version
installed on the box, the version bundled here, the version this release was written against,
and the verdict - `matched`, `older_than_bundle`, `newer_than_bundle` or `unknown`. A receiver
running a *newer* plugin reads "up to date" everywhere in the UI, because the version entity
refuses to offer a downgrade; that is right for a household and useless in a bug report, and a
plugin mismatch is where a missing entity or an unrecognised command usually ends up.

`plugin.last_install` is the last install of the plugin this Home Assistant ran to its end
since the entry was loaded, `null` when there was none: `path` (`mqtt`, `ssh`, `ssh_forced` for
the forced reinstall, `ssh_downgrade` for the options flow's older version), `version`, `time`,
and `proof` - which of the proofs that the new plugin runs it passed. The installer accepts
more than one, and afterwards the outcome alone does not say which:

| `proof` | What was seen |
|---|---|
| `announcement` | a fresh `online` - after an `offline` when a plugin ran before - and a fresh `info` naming the installed version in `integration` mode |
| `announcement_build` | the same, and that `info` named the package's own build commit |
| `log_open` | the forced reinstall of a plugin that is switched off: a new interface process holding the plugin's log open, and nothing the plugin said |
| `log_open+announcement`, `log_open+announcement_build` | the forced reinstall of a plugin that is switched on, which has to pass both |
| `info_build` | an update over MQTT: the transaction ended `installed` - or the plugin was replaced under it - and `info` names the version and the signed entry's build commit |
| `info_version` | the same for a signed entry that names no commit: the version alone |

The same proof is in the Home Assistant log at INFO, on the line that says the plugin is
running. It is kept for this run only and is not a state: the receiver reports the version
it runs by itself.

## 8. Troubleshooting

*Filled in as failure modes are found on real boxes, from M2 on.* These are already clear:

- **Nothing is discovered.** Check that the MQTT integration is connected, then subscribe to
  `enigma2mqtt/discovery/#` with an MQTT client. No retained message there means the plugin is
  not running, not configured, or pointed at a different broker.
- **The device is `unavailable`.** `<base_topic>/<node_id>/availability` should read `online`.
  A retained `offline` that never clears is the plugin's last will; the box lost the broker.
- **Entities appear twice.** The box is in `discovery` mode *and* added here. Re-run the setup
  or send `cmd/ha_mode = integration`; the plugin retracts the discovery payloads.
- **An install or update is refused for standby, although nobody switched the receiver off.**
  With HDMI-CEC following the television, switching the television off puts the receiver into
  standby too. Switch the receiver on, then install ([§3](#3-configuration)).
- **The card installs over SSH, not over MQTT, on a receiver that runs plugin 0.4.0.** The
  receiver's `update_allowed` is off, which is how the plugin ships; see *Which path the card
  takes* in [§4.5](#45-buttons-and-update).
- **A repair says „the receiver is not publishing everything", or a bouquet has no channels or
  no programme guide.** The receiver has a payload that does not fit one MQTT packet, and
  plugin 0.5.0 did not send it. The repair lists what is missing - the complete channel list,
  one bouquet's channel list, one bouquet's programme guide, the device's MQTT discovery
  message - and „Nieopublikowane dane" has the topics and sizes ([§4.3](#43-sensors)). A missing
  complete channel list costs nothing here by itself: the integration reads the per-bouquet
  lists instead. The exception is a bouquet whose name has no ASCII letter or digit, which has
  no list of its own to read ([§4.6](#46-selects)); the repair names it too. For the rest, set
  `bouquets_for_select` on the receiver - the plugin's setup screen
  or its OpenWebif page - to the bouquets the household uses, or split a very large bouquet
  into smaller ones. Lowering `epg_grid_events` does not bring a missing guide back: the plugin
  shortens a guide as far as it can before it gives up on it. The repair goes by itself once
  the receiver publishes everything. **Ignore** holds for as long as anything is withheld -
  across a reload and a restart, and also when the list changes, which the sensor shows - and
  is forgotten when the repair goes, when the receiver is removed, and, as for every repair,
  when Home Assistant is updated. Unloading or disabling the receiver leaves the repair where
  it is until Home Assistant restarts. The list inside the repair is written in the
  installation's language, also for a user whose profile is set to another one. The plugin's
  [troubleshooting page](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/TROUBLESHOOTING.md#entities-keep-going-unavailable-and-coming-back)
  has the receiver's side of it.
- **Entities keep going unavailable and coming back, and the selects are empty, on a receiver
  with a very large channel list and plugin 0.4.0 or older.** That plugin sends the oversize
  payload and the broker closes the connection each time. Update the plugin to 0.5.0, or
  narrow `bouquets_for_select` on the receiver.
- **A repair says „the receiver's clock is behind", and nothing in the receiver menu does
  anything.** An action from the [receiver menu](#receiver-menu) was stamped more than two
  minutes before Home Assistant's clock, so it was refused, and so is every other OK while the
  clock stays behind. Let the receiver take its time from the internet (NTP) or from the
  transponder in its time settings, check its time zone, and check Home Assistant's own clock.
  The repair is raised at the first such refusal and goes by itself at the next action Home
  Assistant accepts, or when the receiver is removed; unloading the receiver leaves it. A clock
  **ahead** by more than two minutes raises no repair: those actions are refused too, and the log
  says `future`.

### The plugin does not start, and Home Assistant cannot help

With SSH credentials stored, „Wymuś reinstalację wtyczki (SSH)" (section 4.5) is the repair. When
Home Assistant itself is unavailable, install the plugin by hand on the receiver, over SSH.
**This path has no signature and no compatibility check**: the image's opkg cannot check feed
signatures, the feed carries no key, and the key that signs the plugin's release index is this
project's, which opkg does not use. So install the version the update card names, and check the
package yourself before opkg sees it - which means downloading it first, as its own step:

1. Download the package of that version, and the index, onto the receiver - here `0.3.0`, into
   `/tmp`:

   ```sh
   cd /tmp
   wget -O enigma2-plugin-extensions-mqttbridge_0.3.0_all.ipk \
       https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/enigma2-plugin-extensions-mqttbridge_0.3.0_all.ipk
   wget -O releases.json https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/releases.json
   ```

   The `.ipk` asset of the plugin's GitHub release for that version is the same file.
2. Compare the package's checksum with the `sha256` the index gives for that version. If they
   differ, stop and delete the file.

   ```sh
   sha256sum enigma2-plugin-extensions-mqttbridge_0.3.0_all.ipk
   grep '"version": "0.3.0"' releases.json
   ```

   This catches a damaged or truncated download, not a compromised origin: the index's signature,
   `releases.json.sig`, is checked by Home Assistant and by the plugin, and not here.
3. Install that file - `opkg install ./enigma2-plugin-extensions-mqttbridge_0.3.0_all.ipk` (add
   `--force-reinstall` when that version is already installed, and `--force-downgrade` for an
   older one) - and restart the receiver's interface from its menu.

**The feed.** The plugin's installation guide adds its opkg feed with this one line, which is also
what lets the receiver's own plugin browser offer the plugin again:

```sh
echo 'src/gz enigma2-mqtt-bridge https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed' \
    > /etc/opkg/enigma2-mqtt-bridge.conf
opkg update
```

With the feed configured, `opkg upgrade enigma2-plugin-extensions-mqttbridge` - or the image's
own software manager - downloads and installs in one step, with no moment to check the package:
use it only if you accept that.

### Removing a receiver

Delete the entry in *Settings -> Devices & Services*. On the way out the integration publishes
`cmd/ha_mode = discovery`, so the plugin announces itself again and the core MQTT integration
rebuilds its own entities - a box that was taken over here is handed straight back. It also
retracts the [receiver menu](#receiver-menu), whose rows nothing would perform any more. These
publishes are best effort: if the receiver is off or the broker is unreachable the entry is still
removed, and the mode can be set on the plugin's setup screen afterwards.

🔴 **Deleting the entry never uninstalls anything**, whatever the entry holds and whatever the
receiver permits: the `cmd/ha_mode` publish above is the only command it sends, and it opens
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
telemetry and no cloud; channel picons are fetched from the receiver itself - but the recorder
keeps a history unless you tell it not to. Exclude
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

The stream clients are the same kind of record again: who is watching which channel from
which address. The receiver publishes them whole, retained; the option that hides bouquets
takes the channel out of what Home Assistant shows and offers, and hides nothing on the
network ([§4.7](#47-streams)). The `clients` attribute is not recorded. The media player's
`stream_url` is recorded with its state and contains the receiver's address and the service
reference of the channel that is on - as `media_content_id` already does. The one thing in
this section that leaves Home Assistant is the request to **your own go2rtc**, and only
while you set its address: it carries the stream name and the source, which contains that
same address and reference.

The [receiver menu](#receiver-menu) publishes the names and states of the entities you put
into it, retained, on the broker, for any login to read; an `input_text` or `text` in password
mode is published without its text, whether it is a text field or an indicator. Leave out of the menu what the broker's users should not see.

The [options](#options) can switch key publishing off, select `off`, `on_zap` or `interval`
screenshots, set the interval and choose how long an on-zap capture waits for the new picture.
The plugin validates and persists the settings as one transaction; disabling screenshots also
retracts the retained image. Optional conditional-access telemetry publishes only the current
service's system, encrypted/active result and ECM time. It never publishes server, account or
card details, and creates no entities until explicitly enabled. OSCam health is optional and off
by default; its per-source entities use stable opaque ids, and private reader labels, server
addresses, accounts and card identifiers are neither published nor retained by the integration.

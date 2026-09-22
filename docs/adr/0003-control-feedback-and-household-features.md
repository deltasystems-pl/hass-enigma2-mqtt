# ADR-0003: Control feedback and household features — the 0.2.0 and 0.3.0 plan

**Status:** accepted
**Date:** 2026-09-21
**Amended:** 2026-09-22 — the guided installer ran on a receiver for the first time, its
rollback ran on a receiver for the first time, and it then ran against a receiver that already
had the plugin on it

## Context

Two days of ordinary household use produced a list of complaints. Each was diagnosed against the
code and against evidence from the running system rather than guessed at, and the answers below are
decisions, not a wishlist.

The first complaint is the one that matters most, because it is about trust rather than features:
**a button did nothing, and nothing said why.** Two buttons were being refused by the receiver, for
a correct reason, and every layer between the receiver and the user dropped the explanation. An
integration that silently swallows a refusal is worse than one that cannot do the thing at all.

The complaints divide cleanly, and so does the work:

- **Things that already exist and behave wrongly or invisibly** — **fixes**, released as **0.2.0**.
- **Things the receiver can do that nothing asks it to** — **features**, released as **0.3.0**.

0.2.0 is cut after the guided installer has been run end to end on hardware, which is the gate
[ADR-0002](0002-scope-after-m0.md) left open. Inside 0.3.0 the receiver-side features come first
because they are what a household notices without opening Home Assistant.

This record covers this integration's half. The receiver plugin's half is ADR-0003 in
[its repository](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0003-control-feedback-and-household-features.md),
and the two must be read together for anything that spans both.

## Decision — 0.2.0, fixes

### 1. Every button waits for the receiver, and a refusal becomes visible

**The problem, in three compounding parts.** The entity buttons publish their command and return
without waiting; only the *actions* wait for the effect or for the receiver's `last_error`. The
`last_error` topic reaches the diagnostics download and nothing else — no entity carries it. And
the plugin clears `last_error` as soon as any command succeeds, so even somebody who knew to
download diagnostics would find it gone after the next volume step. A refused button was therefore
indistinguishable from a broken one.

- **Every button moves to the waiting path.** Where the command has an observable effect, that is
  the proof. Where it has none — deep standby, reboot, a GUI restart, an EPG rebuild — the contract
  offers no positive acknowledgement and the receiver is often about to disappear anyway, so the
  button waits the short error-grace window for a `last_error` naming that command: **an error
  raises, silence is success.**
- **A refusal raises a translated `HomeAssistantError` carrying the receiver's own sentence,
  verbatim.** Those sentences are written for the person who will read them and must not be
  paraphrased into something tidier and less useful.
- **A new diagnostic sensor, „Last error",** whose state is the failed command's name and whose
  attributes are the receiver's text and when it happened.
  - 🔴 **The integration owns that memory, not the topic.** It records the last error it has seen
    and restores it across restarts. A cleared `last_error` does **not** clear the sensor — which
    is the entire point, because the plugin clears the topic on the next success and the user is
    usually looking afterwards.
  - **No contract change.** `last_error` keeps its payload, its retention and its
    clear-on-success behaviour; this is a decision about what a consumer does with it.

### 2. The two power-off buttons obey both gates, and the option says so

The receiver refuses deep standby and reboot unless a box-side permission is on, and that
permission is deliberately unreachable from the broker — a setting that *enables a destructive
command* must not be settable by anything holding a broker password. Home Assistant had no way to
know, so it offered two buttons that would always be refused, and the option text described only
the Home Assistant gate, which read as though it were the only one.

The plugin now echoes that permission read-only. This integration creates the two buttons only
while **both** are true: the Home Assistant option is on **and** the receiver reports the
permission. They follow the optional-entity rule already used elsewhere here — **only a stated
`false` removes anything**, so a receiver running an older plugin, or one whose payload cannot be
read, behaves exactly as it does today. The option's description names both gates and says where
the box-side switch lives.

### 3. The Wake-on-LAN address is validated, normalised and registered

The stored override was accepted with nothing but whitespace trimming and handed to the wake
helper verbatim, so a malformed value surfaced as a hex-parsing error about a character position —
a message that tells a user nothing. The device also carried no MAC connection, so nothing else in
Home Assistant knew the receiver's address either.

Accept the four spellings people actually type (`:`, `-`, `.` separated, or bare), in any case;
normalise to lowercase colon-separated; reject anything else in the form with a translated error.
Normalise again at send time, because an entry written before this release must not reach the wake
helper unchecked. Fall back to the address the receiver publishes when the override is empty — the
override exists for the case where the packet must go somewhere else. Register the address as a
device connection. And **repair existing entries once**: an override that does not normalise is
dropped and the fact is logged a single time, rather than raising a card about a value the
integration has already fixed.

### 4. „Remote" is hidden by default

Home Assistant gives **every** remote entity a power toggle, so the device showed three controls
that all switch power and no indication which is the real one. The power switch stays the labelled
one; the remote is registered hidden by default and still works for `send_command`.

**Existing installations are not rewritten.** Their registry already holds a visibility decision,
and silently hiding an entity somebody may have put on a dashboard is worse than the confusion it
fixes. It is documented instead.

### 5. A `select` platform, and a source list that fits the recorder

*[„fits the recorder" in this heading is void — see the correction immediately below.]*

> 🔴 **Corrected 2026-09-22 — the recorder half of this decision was wrong.**
>
> This section, as accepted, says that a long source list pushes the media player's attributes
> past the recorder's 16 KB limit and costs the entity its history. **It does not, and it never
> did.** Home Assistant's `MediaPlayerEntity` declares `source_list` an **unrecorded** attribute,
> and the recorder removes every unrecorded attribute *before* it weighs a state against the
> 16 384-byte limit. `SelectEntity.options` is unrecorded on the same terms, so the „Channel"
> select added here was never at risk either. Measured by running a thousand-channel state through
> the recorder's own function: the media player is **27 620 bytes of raw attributes and 327 bytes
> stored**, the select 27 053 and 41. The stored row is **a few hundred bytes** with every other
> attribute intact — the difference between the two is the media player's other attributes, which
> are kept; the select has almost nothing besides its options.
>
> Three statements below are therefore void: the heading's „fits the recorder", the problem
> statement's second paragraph, and the claim that the new option „brings the media player's
> attributes back under the recorder's limit and restores its history". Nothing was ever dropped
> from history.
>
> **What changed as built**, in consequence:
>
> - **`source_list_scope` defaults to `all`**, not to the active bouquet. With the recorder
>   rationale gone there was no reason to shorten every existing installation's source list, and a
>   shorter default would have broken automations naming a channel outside the active bouquet.
>   `active_bouquet` is opt-in.
> - **The reason that survives is ergonomic, not technical**: a dropdown of a thousand rows is not
>   a control, and the short list is the one the receiver's own channel ± is walking.
> - **The acceptance criterion was replaced.** „Stays under 16 384 bytes" is meaningless here;
>   the test now routes a real state-changed event through the recorder's own encoder and asserts
>   that the attributes **survive** — including that `friendly_name` is still there, because a
>   state over the limit comes back as an empty object and „small" alone would look like success.
>
> The lesson is worth more than the correction: **a limit that exists is not a limit that
> applies.** The size was measured on the producer's side of the recorder's own filter and nothing
> was checked against a stored row until the feature had already been designed around it. The near
> miss is instructive too — the wrong number argued for a *safer* default, so nothing looked
> broken.
>
> Decision 10's requirement is **not** affected by this and still stands; see the note there.


**Two defects, one cause.** In integration mode **nothing listed bouquets or channels** — a channel
selector exists only in the plugin's discovery mode, and this integration had no `select` platform
at all, although the topics, the commands and the actions behind one were all already there.
Separately, the media player's source list holds every channel of every configured bouquet; **on a
receiver with many hundred channels that pushes the entity's attributes past the recorder's 16 KB
limit**, so Home Assistant drops them from history entirely.
*[Void — see the correction at the head of this decision: it does not, and nothing was dropped.]*

- **„Bouquet"** — options are the bouquets the receiver publishes; the current option is the
  receiver's **active** bouquet; selecting one sends the bouquet command and **waits for the
  bouquet topic** as proof. When the receiver is on the radio list, the movie list or a bouquet
  outside the configured set, the current option is empty rather than invented — that is ordinary
  operation, not a fault.
- **„Channel"** — options are the channels of the **active bouquet only**, because a select with a
  thousand options is not a control. The current option is the playing service, or empty when it is
  not in the active bouquet. Selecting zaps **by service reference**, never by name: name
  resolution is the plugin's ambiguity problem and there is no reason to hand it one when the
  reference is in hand. Options are re-emitted whenever the active bouquet or the channel list
  changes, so switching bouquet immediately reshapes the channel select. Duplicate names inside one
  bouquet are disambiguated rather than dropped, because a select cannot offer the same option
  twice and dropping one would make a channel unreachable.
- **A `source_list_scope` option**, defaulting to **the active bouquet**, which brings the media
  player's attributes back under the recorder's limit and restores its history.
  *[Void — see the correction at the head of this decision: it defaults to `all`.]*
  `all` keeps today's
  behaviour for somebody who prefers one long list, and the option says what that costs. Selecting
  a source outside the active bouquet raises an error naming the alternatives — switch bouquet, or
  use the `zap` action, which takes a reference and is not scoped.

### 6. A button that rebuilds the EPG grid

The command existed with nothing in the interface to press. It is created only while the receiver
names the EPG-grid capability, so a receiver with the grid switched off correctly has no button.

## Decision — 0.3.0, features

The receiver-side halves of these are specified in the plugin's ADR-0003. What this integration
adds:

### 7. A second notify entity for the discreet toast

The plugin gains a non-modal, auto-hiding, top-right message style that never takes focus and never
waits behind an open channel list. This integration exposes it as a **second notify entity** beside
the existing on-screen one, and the `message` action gains a `style` field — `popup` (the default,
today's behaviour) or `toast`. Both appear only while the receiver names the capability.

Two entities rather than one toggle, because „put this on the television" and „mention this
quietly" are different intentions and a dashboard should be able to hold both.

### 8. Softcam: a button, a sensor, and settings that are not the permission

The plugin can restart the cam the image selected, and can optionally heal a stuck decode by
itself. Here that becomes a **button**, created only while the receiver reports the permission, and
a **diagnostic sensor** carrying which cam is selected, how many instances are running, when it was
last restarted, why, and how many times today — the last of which is what makes a restart loop
visible on a dashboard rather than only in a log.

🔴 **The auto-heal settings are in the options flow; the permission is not.** The general rule this
project follows is that a setting which *enables* a command is box-only and a setting which *tunes*
a command already permitted may be remote. The option text says where the permission lives.

### 9. EPG import: a button and a status sensor

A button, created only while the receiver reports the permission, and a diagnostic sensor following
the import's state and timestamps — an import is minutes of work on a small machine, and a button
with no progress is a button people press twice.

### 10. An EPG sensor for the active bouquet

> **Checked 2026-09-22, and this reasoning stands.** The correction under decision 5 does not
> weaken it, and the cross-reference in the paragraph below is the one thing here that was wrong:
> decision 5 does **not** „solve the same problem by shrinking the payload", because decision 5
> had no recorder problem to solve. The difference is **who declares the attribute unrecorded**.
> `source_list` and a select's `options` are declared unrecorded by Home Assistant itself, so they
> never needed anything from this integration. `channels` here is an attribute of ours on an
> ordinary sensor, and an ordinary sensor's attributes **are** recorded — so this one is written
> to the database on every refresh unless this integration excludes it. It must therefore be
> declared in `_unrecorded_attributes`, and a test must assert that rather than assume it.


A sensor whose state is how many channels in the **active bouquet** have guide data, and whose
attribute carries now and next for each of them, built from the grid the receiver already
publishes.

🔴 **That attribute is declared unrecorded and never reaches the recorder.** It is the same trap
the grid topic carries: an attribute of this size, rewritten on every update, bloats the database —
and it is **not** the problem decision 5 turned out to have (see the notes on both). Here the
attribute is ours rather than one Home Assistant already excludes, so it is excluded here. Titles
are capped. Full multi-event grids stay in the `get_epg_grid` action, which returns a response and
stores nothing.

### 11. Wake-on-LAN, and process sensors

The plugin gains a setting that arms Wake-on-LAN on the receiver's interface and reports what is
actually true about it. This integration does not add an entity for that in 0.3.0 — the
`turn_on` path and the wake button already exist and are fixed by decision 3. 🔴 **The README keeps
its warning that deep standby may be one-way until the drill passes on hardware**: a setting that
issues the right command is not a receiver that wakes up.

Process and memory sensors ship as already drafted, gated on the receiver naming that capability.

## Amendment — 2026-09-22: what the first guided install on a receiver found

The decisions above were written while the guided installer had never been run on hardware. It has
now been, and it found four defects. None of them changes a decision above; they are recorded here
because two of them are about a mechanism nothing else in this integration uses, and a reader who
only has the code will not see why it is written the way it is.

### 12. A pending discovery flow may not block the guided install

The plugin's announcement is retained, so Home Assistant offers a discovery card for the receiver
again at every start and at every reconnect to the broker. On any box that has ever announced, one
of those offers is therefore almost always waiting — and the guided install's
`async_set_unique_id` refused to proceed beside it, so the install aborted with
`already_in_progress` at the one step where two passwords have just been typed.

The offer and the install are about the same receiver, and the install is the one with a person
behind it: it claims the unique id without raising, and retires the **discovery offer** for that
id. Only that one. Aborting every flow with the same unique id would mean a second guided install
cancelling the first one's task in the middle of its transaction, and the first would then be
unwinding a receiver the second is writing to. A flow of this handler parked on the progress step
holds a transaction against a real box, so the second install is refused instead — it aborts
itself with `already_in_progress`, which is what that string was always for.

The reverse stays as it was — an announcement arriving while an install is running aborts, because
the install owns the id from the moment the credentials are submitted. So does the manual path,
which never raised on a pending offer and does not withdraw one either; Home Assistant retires it
by itself once an entry claims the id, which is a moment the manual path reaches in seconds and
the install does not reach for minutes.

### 13. A progress flow has exactly one caller that may finish it

Home Assistant advances a progress step itself when the task it was handed resolves, and then tells
the frontend the flow changed; the frontend answers by posting to the flow, and that post is what
creates the entry. The install also reports its own phases so that the card says what is happening,
and each report asked the frontend for the same refresh — including the last phase, which is
reported when the work is already committed, microseconds before the task resolves.

Two posts then raced for one flow. The first created the entry and removed the flow; the second
found nothing and was answered with „Invalid flow specified" — on the screen that should have said
the receiver was ready, at the end of an install that had in fact succeeded. It is worse on a
**failure**: a phase reported shortly before an error produced the same race, and there the
message that is lost is the reason, which is the only thing on that screen worth reading.

**No phase asks the frontend for anything.** Suppressing the notification for the last phase only
would have narrowed the window rather than closed it — every phase is followed by work that can
fail quickly. The mechanism is the one Home Assistant's own integrations use: the flow is shown
with a single `progress_action` for the whole install, and the phases drive
`async_update_progress`, which reaches the frontend as a bar position without asking it to post.
The only notification left is the one Home Assistant sends when the install task resolves, so
there is one caller and one answer, on success and on failure alike.

**The trade-off is the caption.** It no longer names the phase — eight per-phase strings became
one sentence covering the whole install. A caption can only change when the step is entered again,
and the step is only entered again on a post; with nothing asking for one, a phase in the caption
would freeze on „preflight" for the length of the install and read as a receiver that had stopped.
A bar that moves eight times says the true thing, and says it live.

### 14. A rollback has to delete the bytecode the receiver actually writes

CPython compiles into `__pycache__/<module>.<tag>.pyc`; this image compiles into the legacy
location, `<module>.pyc` beside the source. `opkg remove` deletes the files it installed, which are
the `.py` files, so the rollback of a first install restored "there was no OpenWebif hook here" and
left `MQTTBridge.pyc` next to where the source had been — and Python imports a legacy-location
`.pyc` as a complete module, so the hook stayed importable and would go looking for a plugin that is
no longer installed. Both locations are now snapshotted and restored as one thing. The plugin
directory itself was already removed wholesale rather than file by file, which is the only approach
that survives an image whose set of compiled files is not the set of files of any one build.

### 15. Snapshots are pruned, and the newest two are kept

Every guided install left another full copy of the plugin directory under
`/home/root/mqttbridge-backups/`, for ever, on a receiver's flash. A successful install now prunes
its predecessors and keeps its own snapshot and one more. Its own is kept **by name**, not by
timestamp: many receivers have no battery-backed clock, boot in 1970 and jump to the real time
when NTP answers, which can be after the snapshot was written — so ordering by modification time
can rank the only way back from the committing install as the oldest thing in the directory and
delete it. Pruning happens after the transaction lock is released and before the helper that does
it is deleted, it only ever considers directories named `ha-installer-<nonce>` that are not
symlinks, and a failure to prune is logged and otherwise ignored: by then the plugin is installed
and verified, and tidying cannot be a reason to undo that.

## Amendment — 2026-09-22, second drill: the rollback path was never watched

The first drill exercised an install that succeeded. The second deliberately gave the installer a
broker password that would not authenticate, so that the rollback — the part of this transaction
that only ever runs on a bad day — would run for the first time on hardware. It worked, and it
reported that it had not.

### 16. A rollback waits for the interface it restarted, exactly as the install does

The plugin installed, the receiver restarted, the broker answered `Not authorized` seven times
over 103 seconds, and no announcement arrived; the installer rolled back. Measured on the box:
`init 4`, the restore, `init 3`, and eleven to fourteen seconds later the interface answering
again — the same recovery time every restart took that day. Six seconds after `init 3`, Home
Assistant reported that the rollback had failed.

`init 3` is answered when the runlevel change is accepted, not when Enigma is up, and the rollback
asked `pidof enigma2` once, straight afterwards. **The success path never made that mistake**: it
waits up to two minutes for the plugin's fresh MQTT announcement before judging the restart. The
rollback has no announcement coming — it has just put the old plugin back, or no plugin at all —
so it now polls for the process instead, with the same timeout. A restart that genuinely does not
come back is still a failure, but a truthful one.

Two consequences followed from one wrong verdict, and both are worse than the verdict:

**The reason for the failure was replaced.** After a rollback that succeeds, the abort names the
*original* failure — here, that the plugin never announced itself — because that is the thing the
person has to fix. A rollback that reports failure overwrites it with "the receiver could not be
put back", which is both untrue and unactionable.

**The receiver stayed locked.** The release of the durable transaction lock sat after the restart
proof, so the false verdict skipped it. `owner.json` remained under
`/home/root/mqttbridge-backups/.ha-installer.lock/`, and the operator's next attempt — on a
receiver that was in perfect order — aborted with „Na tym dekoderze trwa już inna instalacja". It
had to be deleted over SSH.

### 17. The lock is released whatever else went wrong, and the two halves are told apart

The order is restore, restart, prove the restart, release — and the release is reached from every
path. A receiver whose files are back and whose interface did not start needs a person to restart
it, not a lock that refuses the next install. **Even a restore that failed outright releases it**:
the argument for holding it is that the box is in an unknown state and should be protected, but
the next attempt takes its own snapshot before it touches anything, so the protection is worth
little — while a lock nobody holds refuses every attempt for the full half-hour before it is
judged stale, which is the failure the operator actually hit. The lock exists to keep two
transactions off one receiver, not to quarantine a receiver.

Because the steps fail for different reasons and leave the box in different states, they are now
different outcomes. `rollback_failed` keeps its meaning — the restore itself did not complete, the
receiver may be part-way between two versions, and the abort names the snapshot directory to look
in. `rollback_restart_failed` is new: the files are back, only the interface did not come up, and
the abort says to restart the receiver by hand and nothing else. Reporting both as the first sent
a person to inspect a box that needed a power cycle.

`rollback_lock_failed` is the third, and it exists because "release the lock from every path" is
not the same as "the lock was released". A release that fails after an otherwise perfect rollback
leaves the receiver correct and unusable by the installer, and reporting only the install's
original reason would be true and useless — the person fixes what was wrong, presses install, and
is refused as busy by a lock nobody holds. The abort names the directory and the half hour after
which it is judged stale. The distinction is drawn on what actually happened, not on what was
attempted: a release that succeeded and a tidy-up that failed after it is not this outcome, and
the log line for it does not claim the receiver is locked.

**A cancellation is not an outcome.** Home Assistant shutting down mid-transaction was being
converted into `rollback_failed` — a verdict about a receiver, from an event that says nothing
about one, and one that also left the task believing it had not been cancelled. It now propagates
as itself from every step, with the lock release still attempted on the way out because it is one
short command and the lock outlives the process holding it.

### 19. The restart proof counts processes that are new, not processes that are singular

`pidof enigma2` returning exactly one pid was treated as the only healthy shape, so the proof was
"one pid, and a different one from before". Some images run a wrapper that survives a GUI restart
beside the child that does not, and on such a receiver that proof is never satisfied.

It is the **install** that this hurts most, and it was found by looking for the same narrowness
elsewhere after the rollback was fixed rather than on the box: the install reads the pid at the
step immediately before the only disruptive command, so a receiver of that shape was refused with
`restart_failed` having had nothing done to it — the guided installer simply could not be used on
that image. In the rollback the same proof cost the full two-minute timeout and then a false
report of a restart that had happened.

Both now take the set of pids before the interface is stopped and look for a pid that is not in
it, because such a process was started since, which is what a restart means. A set that never
gains a member is an interface that never came back, which is what the timeout and the
`restart_failed` refusal are for. An **empty** set beforehand is an ordinary answer — a box still
booting, or one stopped on purpose — where it used to be refused as an ambiguous lifecycle; any
pid at all afterwards is then the proof. The two halves are deliberately asymmetric about failing
to read that set at all: the rollback suppresses it, because it runs against a receiver in its
worst state and nothing measured there may veto the restore, so an unreadable set becomes an empty
one and any pid afterwards satisfies the proof — while the install lets the failure stop it, since
at that point nothing has been touched and there is no reason to accept a weaker proof.

### 18. A recovery that fails has to say why in the log

`_LOGGER.error("Installer rollback failed; receiver backup remains at %s", backup)` dropped the
exception that caused it, and the line about a lock that could not be released said only which
receiver. During the drill that left the receiver as the only place to find out what had happened,
which is exactly the position the log exists to avoid. Both lines now carry the cause, and the
rollback's steps — stop, restore, restart, release — are logged at INFO as they happen, so the
trail exists without debug logging having been turned on before the failure.

Snapshot pruning is unchanged and was confirmed by the same drill: it runs only after a commit, so
the failed run's snapshot stayed on the receiver and made three. That is correct — a failed run's
snapshot is evidence — and the next successful install treats it as an ordinary candidate, keeping
its own and the newest one behind it.

## Amendment — 2026-09-22, third drill: installing over a plugin that was already there

The first two drills ran against a receiver with no plugin on it. The third ran the guided
installer against one that already had the plugin — working, at 0.2.0, with its Home Assistant
entry deleted so that it had been told `ha_mode: off`. That is the ordinary reinstall, and it is
the shape of every upgrade done through the flow rather than the update entity. It was refused
immediately, before anything was touched, with „the receiver is configured for a different node
ID or base topic. Nothing was changed."

### 20. A refusal that names neither side cannot be acted on, or diagnosed

That sentence is the whole of what the operator saw, and it was also the whole of what anybody
looking afterwards had: the flow is gone once the screen is read, and the installer wrote nothing
to the log (decision 21). Which of the two values disagreed, and what the receiver was actually
configured for, were both unavailable — so the refusal could not be acted on, and the session
that investigated it could not tell a wrong refusal from a right one without going to the box.

The first theory was that the receiver's settings file was being misread. It holds ten
`config.plugins.mqttbridge.*` lines out of the plugin's twenty-six settings — the ones somebody
had changed — and `base_topic` is not among them, because the box is on `enigma2` and always has
been; nor is `port`, nor `enabled`, because enigma2 writes no line for a setting whose value
still equals its default. **Measured, and it is not what happened:** run against those exact
eleven lines, the code in `main` answers `base_topic: enigma2` and accepts the install. The
receiver-side helper substitutes the plugin's defaults itself, so absence and the default value
are the same answer, and the comparison was never going to refuse on it.

What is left is that one of the two values really did differ — a node id or a base topic on the
form that was not the receiver's — and there was no way to see which, because the message named
neither. That is the defect, and the fix is the message: it now carries the receiver's node id
and base topic and the ones the form gave, in all three languages, through the config flow's
`description_placeholders`.

The theory was wrong but the thing it pointed at is real, and it is why the message could not
have marked the difference even if it had tried. The plugin's defaults had a second copy, inside
the helper — on the receiver's side of the link, in the process whose job is reading raw lines
off a filesystem, where nothing compares it against the plugin it is a copy of. Downstream of it,
"the settings file says nothing" and "the settings file says `enigma2`" are indistinguishable.
The helper now reports raw facts, `null` for a setting with no line, and the defaults live on the
Home Assistant side in one table — `PLUGIN_SETTING_DEFAULTS` — which a test reads out of the
bundled plugin's own `config.py` and compares entry by entry. The bundle is pinned by digest and
commit and is the same source as the package the installer ships, so this is a mirror that fails
in CI rather than on a receiver. Brackets in the message then mean exactly one thing — the
receiver has no line for this — and what is inside them is the default that applies in its place,
so a default it is running on and a value it holds do not read the same. A setting stored as an
empty string is stored, and is shown as `""` without brackets: it is a real difference from the
default, it compares as one, and it is the one value that would otherwise appear as nothing at
all in the middle of the sentence.

What the installer accepts and refuses is unchanged by any of this. Two of the comparisons are
deliberately not symmetrical. **`node_id` is not defaulted**: the plugin has
no default worth comparing against, since it derives `<boxtype>_<mac6>` on its first start and
writes it, so a receiver with no node id is one whose plugin has never run. Provisioning accepts
such a box and writes the id the form gave — there is nothing there to take over — and an update,
which writes no settings, still refuses it, because it has to find the box already correct.
Deriving the expected id instead was considered and rejected: the only source for the MAC at that
point in the transaction is an announcement a box in `ha_mode: off` is not publishing, so it
would be a guess made to satisfy a check.

**`enabled` and `ha_mode` are defaulted**, which keeps the update path working on the very boxes
that would otherwise have broken next: `enabled` is on by default and therefore absent on every
receiver that never turned it off.

Brackets rather than a word for the unstored values, because the string is dropped into the
Polish and German sentences unchanged and a word in it would be an English one.

### 21. A refusal that changes nothing still has to be written down

The refused install left **no log line at any level**. The only account of it was the sentence on
a config-flow screen, which is gone the moment it is read; afterwards there was nothing to say an
install had been attempted, let alone which check had refused it or what it had measured. Every
guard that refuses before the receiver is touched now leaves one warning naming the check and its
facts — the Python version and the floor, the free bytes against the bytes needed for each of the
three filesystems, the running recording, the number of timers inside the guard window, the
installed version against the bundled one, both halves of an identity, and, for the checks that
fail closed on an answer they cannot parse, which answer that was. No credential, no address and
no broker password is among them, because none of those is something a guard judges.

Exactly one line per refused install, and the **guards themselves log nothing**. The facts
travel with the refusal and a single boundary around the steps that run before the receiver is
touched writes them. The alternative — each guard logging as it raises — is wrong twice over: the
same guards are what `async_preflight` runs for the options screen's no-write credential probe
and for reauthentication, where the failure goes to a form to be retried and a receiver that
happens to be recording is not a refused install at all, so the log would fill with a sentence
that was untrue once per retry; and a guard reached through a helper that re-raises would need a
flag on the exception to avoid writing itself down twice. One place that knows an install was
being attempted, one line.

The line says what the receiver was left holding rather than „nothing was changed", because that
is not quite true: no snapshot is taken and no transaction lock claimed, but the identity check
reads the receiver's settings by running the installer's own helper, so a refusal at that check
can leave that one file in `/tmp`. Uploading the helper after the check is not available — the
check is what runs it.

What a refusal still does not do is consume the discovery card the box is being offered on —
measured across twenty-five samples of the refused install. That is correct and stays: the card
is taken down when an install starts changing things, and this one never did.

## Consequences

- **Buttons can now raise.** An automation pressing a button that the receiver refuses will fail
  where it used to silently succeed. That is the point, and it is a behaviour change worth a
  changelog line: an automation written against the old silence may start reporting errors it was
  previously blind to.
- **Two new entity categories appear and disappear with a receiver-side permission** (softcam, EPG
  import) and two more with an option (the selects are unconditional, the telemetry is not).
  Anything keying on „the integration's entities" must tolerate a set that changes shape — which it
  already had to, and which is the price of not creating controls that can only fail.
- **The default source list gets shorter.** Somebody relying on `select_source` finding any channel
  on the receiver will need `source_list_scope: all`, or the `zap` action, or the new channel
  select. The option's description and the documentation both say so, and the error message names
  the alternatives rather than just refusing.
- **The remote is hidden on new installations only.** Two installations of the same version can
  therefore differ, which is the price of not rewriting somebody's dashboard.
- **There are two more ways an install can end**, `rollback_restart_failed` and
  `rollback_lock_failed`, whose advice is "restart the receiver and try again" and "delete the
  lock, or wait, and try again" rather than "look at the receiver" or "fix this and press
  install". Anything matching on abort reasons — a test, a script, a translation file — has to
  carry them.
- **A refused install is now noisy at WARNING.** An installation attempt that changes nothing
  writes a line to the Home Assistant log, where before it wrote none. That is the point, and it
  is visible to anybody watching the log: a receiver that is recording, or one an install is
  retried on, produces a warning per attempt.
- **The integration now remembers something the broker does not.** „Last error" is the first piece
  of state here that is not a projection of a topic. It is deliberate — the topic is cleared by
  design and the memory is the whole feature — but it means one entity survives a reload with a
  value nothing on the broker can confirm, and that is worth knowing when reading diagnostics.

## Not yet done

- **None of this is implemented.** This record is the plan.
- **0.2.0 is still gated on the guided installer running end to end on hardware**, which has never
  happened ([ADR-0002](0002-scope-after-m0.md)).
- **There is still no 0.2.0 release**; HACS serves 0.1.0 and both halves still report `0.1.0`.
- **Test coverage is still 93 % against the 95 % this project set itself**, and the new platforms
  arrive with their own tests rather than closing that gap.
- **Replacing an OpenWebif-based EPG collector is explicitly not in scope here.** Decision 10 adds
  a sensor; migrating a household off a separate collector belongs to finishing the media-panel
  work, and conflating the two is how a feature becomes a migration.

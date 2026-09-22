# ADR-0003: Control feedback and household features — the 0.2.0 and 0.3.0 plan

**Status:** accepted
**Date:** 2026-09-21
**Amended:** 2026-09-22 — the guided installer ran on a receiver for the first time

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

# ADR-0003: Control feedback and household features — the 0.2.0 and 0.3.0 plan

**Status:** accepted
**Date:** 2026-09-21

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

**Two defects, one cause.** In integration mode **nothing listed bouquets or channels** — a channel
selector exists only in the plugin's discovery mode, and this integration had no `select` platform
at all, although the topics, the commands and the actions behind one were all already there.
Separately, the media player's source list holds every channel of every configured bouquet; **on a
receiver with many hundred channels that pushes the entity's attributes past the recorder's 16 KB
limit**, so Home Assistant drops them from history entirely.

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
  player's attributes back under the recorder's limit and restores its history. `all` keeps today's
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

A sensor whose state is how many channels in the **active bouquet** have guide data, and whose
attribute carries now and next for each of them, built from the grid the receiver already
publishes.

🔴 **That attribute is declared unrecorded and never reaches the recorder.** It is the same trap
the grid topic carries: an attribute of this size, rewritten on every update, bloats the database —
and it is the same problem decision 5 solves for the media player by shrinking the payload. Here
the payload cannot shrink, so it is excluded instead. Titles are capped. Full multi-event grids
stay in the `get_epg_grid` action, which returns a response and stores nothing.

### 11. Wake-on-LAN, and process sensors

The plugin gains a setting that arms Wake-on-LAN on the receiver's interface and reports what is
actually true about it. This integration does not add an entity for that in 0.3.0 — the
`turn_on` path and the wake button already exist and are fixed by decision 3. 🔴 **The README keeps
its warning that deep standby may be one-way until the drill passes on hardware**: a setting that
issues the right command is not a receiver that wakes up.

Process and memory sensors ship as already drafted, gated on the receiver naming that capability.

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

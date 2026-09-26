# ADR-0004: Remote uninstall behind a box-side permission

**Status:** accepted 2026-09-22; §3 and the first consequence superseded by
[ADR-0006](0006-remote-uninstall-plugin-acts-ssh-verifies.md), which also records the implementation;
§4's "does nothing else" superseded in part by [ADR-0008](0008-signed-plugin-index.md) (proposed)
**Date:** 2026-09-22

## Context

This integration can install the plugin on a receiver: a guided flow, two passwords, a snapshot, a
verified upload, a restart and a rollback if any of it goes wrong. It cannot remove one. Removal
is an SSH session and a package name, which is precisely the knowledge the guided installer exists
to spare somebody. **An install path with no matching removal path is the defect**, and it is felt
by exactly the people the installer was built for.

Two things about a receiver-side removal are not obvious from here, and both shape the decision.

**Retained topics outlive the plugin.** Removing the package without first retracting them leaves
the broker serving a snapshot of a receiver that is gone, for ever, and a consumer showing
entities nothing will ever update. The correct order - retract while the plugin is still
connected, then remove - is documented and is easy to forget, and forgetting it is invisible until
much later.

**Deleting a configuration entry is not a statement about the receiver.** It is a local decision
about Home Assistant, reached from a page people delete things on by accident, and it is ordinarily
undone by adding the entry back. Today it does one thing to the receiver and one thing only: it
hands the box back to MQTT discovery, because adding it switched the plugin into `integration`
mode and leaving it there would be a receiver publishing state nothing listens to. That is
reversible, best effort, and proportionate to the click.

Hanging an irreversible change to another machine on the same click would not be. It would turn a
recoverable mistake into an unrecoverable one, and it would do so silently, by implication. The
same argument holds a level down: a message on a broker must not carry that much power because
something inferred it. Removal has to be asked for, in as many words, by somebody who meant it.

**Alternatives considered.**

- **SSH only.** It is the better path where it exists, because it can *verify* the outcome rather
  than trust it. But SSH credentials are deliberately opt-in here, and most entries will not have
  them; as the only path it leaves the majority of installations exactly where they started.
- **No remote removal at all.** Rejected for the same reason, with the retained-topic ordering on
  top: the people most likely to get that ordering wrong are the people who will not be running a
  reset by hand first.

The receiver's half of this decision - the permission, the command, the order of operations and
why removing one's own files while running is safe - is ADR-0004 in
[the plugin repository](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0004-remote-uninstall.md).
The two are read together.

## Decision

Target release **0.3.0**.

### 1. An explicit, confirmed action: „Remove the plugin from the receiver"

Not a button on the device page beside the volume, and not a box on the delete dialog. An action
with its own name, its own confirmation step, and a sentence that says what it costs before it is
taken.

### 2. It is offered only while the receiver says it is permitted

The plugin gains `uninstall_allowed` in the read-only part of `info.settings`: off by default,
granted on the receiver and never over MQTT, on the terms
[ADR-0003](0003-control-feedback-and-household-features.md) set for the power-off permission.

🔴 **The polarity here is the opposite of the one decision 2 of ADR-0003 uses, deliberately.**
There, only a *stated* `false` removes anything, so a receiver running an older plugin - or one
whose payload cannot be read - keeps behaving as it always did. Here the action exists only on a
stated `true`. Silence means „this receiver has not said it permits this", and for an operation
that cannot be undone the safe reading of silence is no. The three states ADR-0003 already
identified - not yet stated, stated true, stated false - are the same three; only which of them
opens the door changes.

### 3. SSH is preferred where the entry has it; the command is the fallback

- **With stored SSH credentials**, removal goes the way the installer went. That path can *prove*
  the result: the same readbacks that verify an install verify that the package is gone and that
  nothing loadable was left behind. It is the path that can tell success from a receiver that
  merely stopped answering.
- **Without them**, the integration publishes the plugin's `cmd/uninstall`, whose payload is the
  node id - a confirmation that this receiver was meant, not a secret. The receiver retracts its
  retained topics, says `offline`, removes its package and restarts, in that order.

### 4. Deleting the configuration entry still never removes anything from a receiver

Unchanged, and now written down as a decision rather than left as an implementation detail:
removing the entry hands the box back to MQTT discovery and does nothing else. Uninstalling the
plugin is the action above, and only the action above.

> **Superseded in part by [ADR-0008](0008-signed-plugin-index.md) (proposed, 2026-09-26):** removing
> the entry is also to retract the retained topic `enigma2mqtt/integration/<node_id>`, on which the
> integration tells the receiver which plugin versions it can work with - a second publish, so
> "does nothing else" no longer holds. Nothing is removed from the receiver, as this section says.
> Until ADR-0008 is accepted and shipped, this section describes every released integration exactly.

### 5. The one-way door is stated before it is opened, not after

When the plugin is gone there is nothing left to listen: no message from Home Assistant can bring
it back, and the receiver returns only through SSH or its own package manager. The confirmation
says that, in one sentence, in the household's language, before the action runs - and it says that
the receiver keeps its settings, so a later reinstall finds its configuration where it left it.

## Consequences

- **Over MQTT, success cannot be proved.** The last thing the broker hears is `offline`, and a
  receiver that has removed the plugin looks exactly like one that was switched off mid-command.
  The action reports what it asked for and what it observed, and says which of the two it is -
  rather than reporting a success it cannot see. The SSH path does not have this problem and is
  preferred for that reason, not for speed.
- **The entry and its entities survive the removal.** Nothing will update them again, and the
  device goes unavailable and stays there. That is correct - the configuration is the user's, and
  this integration does not delete it out from under them - but it means the visible outcome of a
  successful uninstall is indistinguishable at a glance from a receiver that is off. The
  confirmation says what to expect, and deleting the entry afterwards is the user's next step if
  they want one.
- **One more control appears and disappears with a receiver-side permission**, joining softcam and
  EPG import. Anything keying on „the integration's entities and actions" must already tolerate a
  set that changes shape.
- **The removal path has two implementations to keep honest**, SSH and MQTT, and they fail
  differently. The tests cover both, and the one that must not regress quietly is the refusal: the
  permission is off on every receiver as shipped, so „the action is not offered, and the command
  is refused" is the behaviour almost every installation will ever exercise.
- **New strings**, including the confirmation sentence, in every language this integration ships.
  A warning nobody can read is not a warning.

## Not yet done

- **None of this is implemented.** This record is the decision; the release is 0.3.0, and the
  plugin's half has to land first - there is nothing to offer until a receiver can echo the
  permission.
- **The composed sequence has never been run on hardware.** Each step is proven separately; a
  plugin removing itself while connected is not.

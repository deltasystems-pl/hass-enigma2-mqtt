# ADR-0005: Two gates for the softcam button, and a diagnostic that is never taken away

**Status:** accepted 2026-09-22
**Date:** 2026-09-22
**Supersedes:** [ADR-0003](0003-control-feedback-and-household-features.md) §3, in part — it
described this feature before it had been measured, and the parts corrected here turned out to be
wrong. Everything else in ADR-0003 stands.

The receiver's half of this decision — how instances are counted, how the cam is resolved and
restarted, the guards, the decode signal and the privacy boundary around the ECM file — is
ADR-0005 in
[the plugin repository](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0005-softcam-restart.md).
The two are read together; this one records only what Home Assistant decides.

## Context

The feature gives a household one button and one diagnostic. Deciding when each of them **exists**
turned out to be the whole of the design work, and it forced two questions this repository had
previously answered only by example.

**A permission and a capability are not the same answer.** The plugin publishes both, from
different places, and a receiver can give opposite answers to them. `softcam_restart_allowed` is a
checkbox on the receiver's own setup screen: it exists on every installation and says whether the
command is *wanted*. The `softcam` capability says whether the receiver could carry the command
out at all — the plugin claims it only where the cam binary resolves under the softcam directory,
its family has a known start line, and the image starts it through its manager's poller rather
than through an init script. A receiver with the permission on and nothing to restart publishes
the permission, claims no capability, and refuses the command. An integration that read the
permission alone would offer a button that is always refused, and — because the plugin's own MQTT
discovery mode creates no button for that receiver — would make one box behave two ways depending
on which mode it was in.

**Removing an entity and creating one are not the same question either**, and this repository had
been answering them with one rule. Everything conditional here is created on an answer and removed
on the opposite answer: the conditional-access diagnostics follow `cam_telemetry`, „Odśwież EPG"
follows the `epg_grid` capability, and both disappear when their gate says no. Applied to a
diagnostic, that rule destroys something: an entity id is what automations, dashboards, templates
and the recorder's history all know an entity by, and the registry entry carries the household's
rename, its area and its icon. Deleting it is only defensible when the thing behind it is
genuinely gone for good.

For a **control** it is. „Odśwież EPG" on a receiver that builds no grids would be refused every
time it was pressed, for as long as that setting stayed at zero, and a permanently-refused button
is worse than no button — that is the argument ADR-0003 already made for the power-off buttons.
For a **diagnostic with history** it is not. A capability that stops being named is an older
plugin after a downgrade, a hook that failed to attach on one boot, or a receiver that has not
answered yet. None of those is a decision anybody made, and none of them means the sensor will
never have anything to say again.

## Decision

### 1. The button exists only on the capability **and** a stated permission

Both, and the permission must be a stated `true`. Silence about the permission creates nothing:
unlike the power-off buttons, no release has ever shipped this one, so there is no installation
whose button has to survive a receiver that has not spoken, and an older plugin would refuse the
command anyway. The gate listens to `info` rather than reading it once, because a receiver answers
after Home Assistant has finished setting the integration up — a capability or a permission read
during platform setup is read as "not said" on every single start.

### 2. Only a stated `false` on the **permission** removes the button

Somebody who turns the permission off at the television has decided, and an unavailable button
left on the device page reads as a fault rather than as a control that was switched off. The
capability is deliberately **not** in the remove predicate: a receiver that stops naming it has
gone quiet, which is not the same statement.

### 3. The „Softcam" diagnostic follows the capability and is **never** removed

Created when the receiver names `softcam`; taken away by nothing. This is the departure from
„Odśwież EPG" above, and it is deliberate: a control that would be permanently refused and a
diagnostic with history are different cases, and the cost of being wrong runs in opposite
directions. A sensor left behind says nothing until its topic returns, which is honest. A sensor
deleted takes a household's customisation and its recorded history with it, and the next payload
brings it back as a stranger under a new id.

### 4. Auto-heal is tuned from Home Assistant; permission is not granted from Home Assistant

`softcam_autoheal` and `softcam_autoheal_seconds` travel the ordinary `cmd/config` path, because
they only tune a command the receiver has already been permitted to run. The permission never
appears on the options form, and the form's own text says where it lives and why.

### 5. A press is proved by silence, not by the state topic moving

The receiver's sequence waits for the instances to stop, kills what survives, starts one and lets
it settle before it republishes — about ten seconds on a cam that ignores a polite signal, which
is exactly the command timeout. Watching the topic would report "the receiver did not carry this
out" for a restart that worked, on precisely the slowest receivers. Every refusal arrives on
`last_error` immediately, and the error-grace window is what catches it, as it does for the other
restarts.

### 6. The payload is rebuilt, not filtered

The snapshot is normalised into a fresh object holding exactly the fields the contract names.
Two readings are fixed by it and both have bitten this project's shape of code before: a boolean
is an integer in Python and would be stored as a count of one, and a count that could not be taken
answers "unknown" and never zero, because no instances at all is a real and interesting reading.
Rebuilding rather than filtering is also what keeps the privacy boundary: the receiver's own
detector reads a file carrying a card-sharing account, a sharing server's address and live control
words, and a field that is not in the contract has nowhere to go — not to an entity attribute, not
into the diagnostics download, not into a log record of ours.

## Consequences

- **The two gates have to be tested separately, and one of them is easy to miss.** A test fixture
  that always supplies both cannot see a gate that checks only one, which is how the capability
  half was left out of the first implementation. The case that matters is the receiver that
  permits the restart and cannot perform it.
- **"Created on X" no longer implies "removed on not-X" in this repository.** Anything added later
  that follows a capability has to state which of the two rules it takes and why. The entity table
  in `DOCUMENTATION.md` §4 carries the distinction so that the next person meets it before they
  meet the code.
- **A stale „Softcam" can sit on a device page indefinitely** — a receiver downgraded to a plugin
  without the capability keeps the entity, unavailable, for ever. That is the accepted cost of not
  deleting somebody's history, and removing the receiver is the way to remove the entity.
- **`running_instances` is `unknown` where a naive implementation would say `0`.** Any automation
  or template reading it has to handle the unknown explicitly; a threshold written as
  `< 1` will fire on a receiver that simply could not take the count.
- **`restarts_today` must not be read as a durable total.** It lives in the receiver's memory while
  the topic is retained, so after a receiver reboot a consumer sees the last published number until
  the plugin publishes again.
- **The press reports success it cannot always see.** Silence is the proof, so a command the
  receiver never received looks the same as one it carried out. That is the same trade the other
  restarts already make, and the alternative — a correlation id — is not in the contract.

## Not yet done

- **None of this has been exercised on hardware from this side.** The integration half is built
  against the topic contract with synthetic payloads; the on-receiver acceptance is the plugin's,
  and the two halves meet for the first time when both are released.
- **The two new plugin settings are absent from `PLUGIN_SETTING_DEFAULTS`.** That table is verified
  in CI against the *bundled* plugin's own source, and the bundle is 0.2.0, which has no softcam
  settings. They belong there when the 0.3.0 plugin is bundled, and not before.

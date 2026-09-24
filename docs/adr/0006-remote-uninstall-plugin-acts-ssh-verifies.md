# ADR-0006: Remote uninstall — the plugin acts, SSH verifies, and the confirmation is a flow step

**Status:** accepted 2026-09-23
**Date:** 2026-09-23
**Supersedes:** [ADR-0004](0004-remote-uninstall.md) §3 and its first consequence, in part — they
were written before the mechanism had been read out of code, and both turned out to be wrong.
Every other decision in ADR-0004 stands: the explicit, confirmed action; the permission and its
polarity; deleting the entry never removing anything; the one-way door stated before it is opened.

The receiver's half — the permission, the capability, the command, the ordered teardown and its
failure path — is ADR-0004 in
[the plugin repository](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0004-remote-uninstall.md),
amended there for the same reasons. The two are read together; this one records only what Home
Assistant decides.

## Context

ADR-0004 was a decision about *whether*. Building it meant deciding *how*, and a measurement pass
over the receiver's image, the plugin and this integration found four places where the record did
not survive contact with the code.

**SSH cannot be the removal path.** ADR-0004 §3 preferred SSH where the entry kept the installer's
credentials, because SSH can see the result. But an `opkg remove` over SSH never passes through the
plugin, so it never runs the plugin's ordered retraction: every retained topic the receiver owns
stays on the broker, for ever, which is precisely the failure the item exists to prevent. The
order — stop publishing, retract, say `offline`, wait for the broker's acknowledgement, disconnect,
remove — is one only the running plugin can keep.

**Home Assistant has no confirmation for an action.** A button and a service action both run on
one call; neither can ask. The only native place a sentence can be shown and a tick required
before anything happens is a flow step.

**An empty `info` was ignored.** The integration kept the last good `info` whenever a payload did
not parse, and an empty payload — a retraction — does not parse. After an uninstall the plugin's
last stated permission and capability stayed in memory until Home Assistant restarted, so the
action would have gone on being offered for a receiver whose plugin had already gone.

**„Looks exactly like switched off" is wrong over MQTT.** A receiver that goes off ends on its last
will and leaves `info` and its announcement retained. One that uninstalls retracts both *before* it
says `offline`. That is an observable, and ADR-0004's first consequence gave it away.

## Decision

### 1. A menu entry in the entry's options flow, with one form

*Configure* opens on a menu — the ordinary options, and „Remove the plugin from the receiver"
(„Usuń wtyczkę z dekodera") — but only while the action is offered; otherwise it opens straight on
the options form, as it always has. The removal step is one form that states the one-way door, that
the receiver keeps its settings, and that the device will then look like a switched-off one, and it
requires a tick. It ends on an **abort-style result**, never a saved entry, so the options are not
written and the entry is not reloaded: the act changes the receiver, not the entry. There is no
button entity and no service action for scripts.

### 2. Offered only on what the receiver says now

Available **and** the current `info` states `uninstall_allowed: true` **and** the same `info` names
the `uninstall` capability. The announcement is not consulted. An **empty `info` withdraws the
permission** while the rest of the last payload stays — every entity reading the device details
would otherwise lose them to a receiver that is merely resetting. The form asks again at the click,
because it can stay open while the box goes offline or the permission is switched off at the
television.

### 3. The plugin always does the removal; SSH is the witness

The integration publishes `cmd/uninstall`, payload the node id, once, at QoS 1, never retained, and
only after the broker has confirmed the subscriptions that are to hear the answer: Home Assistant
debounces SUBSCRIBE by a tenth of a second, about as long as a receiver on the same network takes
to answer. It then watches, for at most 60 seconds, for fresh — never retained — messages: the
retraction of `info` and of the announcement, then `offline`. A fresh non-empty `info`, announcement
or `online` after a retraction is the plugin's failure path putting everything back and undoes it; a
fresh `last_error` for the command is the answer.

🔴 **Without readbacks the shape does not end the watch.** The plugin publishes `offline` before it
waits for the broker's acknowledgements and before it runs opkg, and its failure path — reconnect,
republish, `last_error` for `uninstall` — comes after both. So when there is nothing to read back
(no credentials, or SSH unreachable before the command), the watch runs to the end of its 60-second
window after the shape: a `last_error` for the command is the refusal with its text; `online` or a
republished `info` without one is „the removal did not complete"; only a window that ends quietly is
„the receiver said it removed the plugin". With readbacks to follow, the shape ends the watch and
the readbacks decide.

With the installer's credentials kept, SSH reads **before** the command — whether the receiver is
recording or about to (a refusal before anything is published), the interface's process ids, `opkg
status` of the package, and the line count and SHA-256 of the sorted `config.plugins.mqttbridge.*`
block, computed on the receiver — and **after** it: a new interface process, then `opkg status`
empty, no opkg info file, no plugin directory, no `MQTTBridge` file anywhere under the Python tree,
`/mqttbridge` answering 404, and the block's count and hash unchanged. Every command is a fixed
read; nothing is uploaded and the settings never leave the receiver.

### 4. Six endings, each named by what was seen

„Removed and verified"; „removed, not verified" with the readback that disagreed or the SSH failure
named; „the receiver said it removed the plugin" without readbacks, after a quiet window; „the
receiver refused" with its own sentence, including a removal that failed after `offline`; „the
removal did not complete" when the receiver came back without a reason; „the receiver did not act"
after 60 seconds of nothing in that shape.

### 5. Deleting the entry: one publish, no SSH — now a test

ADR-0004 §4 unchanged, and now enforced under every combination of credentials, permission and
availability: the complete list of MQTT publishes is one `cmd/ha_mode = discovery`, and no SSH
connection is attempted.

## Consequences

- **Over MQTT alone the removal is an observation, not a proof** — the receiver's own statement, in
  a shape a switched-off box does not produce. SSH turns it into a proof where credentials exist;
  it is preferred for that reason, and it never acts.
- **Without readbacks a removal always takes the full minute**, because a failure after `offline`
  can only be ruled out by waiting for it. A receiver that stays away for the whole window is still
  only the receiver's own statement; a failure that reports later than 60 seconds after the command
  is not seen. With readbacks the flow goes to look as soon as the shape arrives.
- **„Removed, not verified" covers a restart that never came.** The image's own interface restart
  asks on screen, with no timeout, while something is streaming or a background job runs. The
  plugin is disconnected and off the disk by then, so the verdict is about what could be shown, not
  about what happened.
- **The hook readback assumes OpenWebif answers 404 for a path it does not serve.** A receiver whose
  `wget` prints no status line leaves that one readback out; the file search above it has already
  looked for the hook's files.
- **The broker connection is not read.** Whether the receiver still holds a connection to the
  broker port is a check the on-hardware drill makes; the integration does not know the port the
  plugin was configured with and does not read the settings to find it.
- **The options flow changed shape for receivers that permit removal**: its form is now the step
  `settings`, behind a menu. Nothing a user configured moves; the form is the same.
- **Reinstalling is the receiver's package manager**, with the entry kept: the plugin comes back on
  its kept broker, node id and mode, and the entry picks it up with every entity id unchanged. The
  guided installer cannot run while the entry exists and would ask for the broker password again.

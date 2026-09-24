# ADR-0006: Remote uninstall - the plugin acts, SSH verifies, and the confirmation is a flow step

**Status:** accepted 2026-09-23
**Date:** 2026-09-23
**Supersedes:** [ADR-0004](0004-remote-uninstall.md) §3 and its first consequence, in part - they
were written before the mechanism had been read out of code, and both turned out to be wrong.
Every other decision in ADR-0004 stands: the explicit, confirmed action; the permission and its
polarity; deleting the entry never removing anything; the one-way door stated before it is opened.

The receiver's half - the permission, the capability, the command, the ordered teardown and its
failure path - is ADR-0004 in
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
order - stop publishing, retract, say `offline`, wait for the broker's acknowledgement, disconnect,
remove - is one only the running plugin can keep.

**Home Assistant has no confirmation for an action.** A button and a service action both run on
one call; neither can ask. The only native place a sentence can be shown and a tick required
before anything happens is a flow step.

**An empty `info` was ignored.** The integration kept the last good `info` whenever a payload did
not parse, and an empty payload - a retraction - does not parse. After an uninstall the plugin's
last stated permission and capability stayed in memory until Home Assistant restarted, so the
action would have gone on being offered for a receiver whose plugin had already gone.

**„Looks exactly like switched off" is wrong over MQTT.** A receiver that goes off ends on its last
will and leaves `info` and its announcement retained. One that uninstalls retracts both *before* it
says `offline`. That is an observable, and ADR-0004's first consequence gave it away.

## Decision

### 1. A menu entry in the entry's options flow, with one form

*Configure* opens on a menu - the ordinary options, and „Remove the plugin from the receiver"
(„Usuń wtyczkę z dekodera") - but only while the action is offered; otherwise it opens straight on
the options form, as it always has. The removal step is one form that states the one-way door, that
the receiver keeps its settings, and that the device will then look like a switched-off one, and it
requires a tick. It ends on an **abort-style result**, never a saved entry, so the options are not
written and the entry is not reloaded: the act changes the receiver, not the entry. There is no
button entity and no service action for scripts.

### 2. Offered only on what the receiver says now

Available **and** the current `info` states `uninstall_allowed: true` **and** the same `info` names
the `uninstall` capability. The announcement is not consulted. An **empty `info` withdraws the
permission** while the rest of the last payload stays - every entity reading the device details
would otherwise lose them to a receiver that is merely resetting. The form asks again at the click,
because it can stay open while the box goes offline or the permission is switched off at the
television.

### 3. The plugin always does the removal; SSH is the witness

The integration publishes `cmd/uninstall`, payload the node id, once, at QoS 1, never retained, and
only after the broker has confirmed the subscriptions that are to hear the answer: Home Assistant
debounces SUBSCRIBE by a tenth of a second, about as long as a receiver on the same network takes
to answer. It then watches, for at most 60 seconds, for fresh - never retained - messages: the
retraction of `info` and of the announcement, then `offline`. For a receiver whose last `info`
said `ha_mode: off` there is no announcement to retract, and `info` then `offline` is the shape. A
fresh non-empty `info`, announcement or `online` after a retraction is the plugin putting
everything back and undoes it. A fresh `last_error` for the command is a **refusal** when no
retraction at all has arrived, and a **rollback** as soon as any one has - an emptied `info`,
announcement or `availability` (the plugin retracts in sorted order, its own `availability`
first) - even when the `offline` that completes the shape never reached Home Assistant because
the connection dropped mid-retraction. Only a retraction that arrives once the command is about
to be published counts toward the rollback: one before it is somebody else's - a third client
switching `ha_mode` off, a `cmd/reset`, an `availability` emptied by hand - and must not turn a
refusal into a rollback. The count starts immediately **before** the publish call, not after it
returns: the call awaits the broker's acknowledgement, and Home Assistant's client dispatches
incoming messages as it reads them, so after a stall of the event loop the acknowledgement and the
receiver's first retractions arrive in one read and are handled before the call returns. The
removal's *shape* is scoped differently, deliberately: it is what the broker holds, whoever emptied
it - an announcement retracted by `ha_mode: off` a moment before the command is one the plugin no
longer has to retract. A fresh non-empty payload is a republish, never a retraction. A
`last_error` about any other command - the plugin defers `cmd/config` during a removal and says so
under `config` - is not an answer to this one.

🔴 **The shape does not end the listening.** The plugin publishes `offline` before it waits for the
broker's acknowledgements and before it runs opkg, and its failure path - reconnect, `online`, the
snapshot, the announcement, then `last_error` for `uninstall` - comes after both (plugin
`uninstall.py`, `restart_after_failed_uninstall`). So the subscriptions stay up after the shape: while
the SSH readbacks run, when there are readbacks, and for the rest of the 60-second window when there
are not. A `last_error` for the command after the shape ends the flow at once as a removal that was
started and rolled back, in the receiver's words, whether or not SSH is in use. Without readbacks,
`online` or a republished `info` without one is „the removal did not complete", and only a window
that ends quietly is „the receiver said it removed the plugin".

With the installer's credentials kept, SSH reads **before** the command - whether the receiver is
recording or about to (a refusal before anything is published), the interface's process ids, `opkg
status` of the package, and the line count and SHA-256 of the sorted `config.plugins.mqttbridge.*`
block, computed on the receiver - and **after** it: a new interface process, OpenWebif answering
its status page (bounded), then `opkg status` empty, no opkg info file, no plugin directory, no
`MQTTBridge` file anywhere under the Python tree, `/mqttbridge` answering 404, and the block's count
and hash unchanged. A restart that never came is named and does not hide the other readbacks; the
404 check, which only a restart settles, is then left out. A refusal of `opkg status` for its lock
- and only for a lock somebody holds: opkg's `Could not lock <path>` or the image's `Command
failed to capture privilege lock`; `Could not create lock file ...`, for the file or its directory,
is a receiver that cannot take the lock at all and is not retried - is asked again, briefly. If
the lock is still held before the command,
the flow refuses there, „opkg on the receiver is busy", and publishes nothing: the plugin would meet
the same lock after retracting everything, and roll back. Every SSH failure - a dropped connection
or a timeout included, `pidof` among the before-reads as much as any other, before the
command or after it - costs the verification and is named; it never ends the flow as „unknown".
Every command is a fixed read; nothing is uploaded and the settings never leave the receiver.

### 4. Seven endings, each named by what was seen

„Removed and verified"; „removed, not verified" with the readback that disagreed or the SSH failure
named; „the receiver said it removed the plugin" without readbacks, after a quiet window; „the
receiver refused" with its own sentence, before any retraction; „the receiver started the removal
but aborted it" with its own sentence - which states the package's condition, so the ending
adds nothing about it - when it said why after any retraction;
„the removal did not complete" when it came back without a reason; „the receiver did not act" after
60 seconds of nothing in that shape.

### 5. Deleting the entry: one publish, no SSH - now a test

ADR-0004 §4 unchanged, and now enforced under every combination of credentials, permission and
availability: the complete list of MQTT publishes is one `cmd/ha_mode = discovery`, and no SSH
connection is attempted.

## Consequences

- **Over MQTT alone the removal is an observation, not a proof** - the receiver's own statement, in
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
- **The hook readback assumes OpenWebif answers 404 for a path it does not serve**, and asks only
  once OpenWebif answers its own status page. The page answers 200 from the receiver itself on the
  current plugin (measured 2026-09-23), not the 403 an earlier measurement from another host saw;
  any status other than 404, and a missing status line, is a readback that disagrees. OpenWebif
  that never comes back within its bound is named as such rather than read as the page being gone.
- **Mid-readback, a rollback wins.** A receiver whose opkg failed after `offline` never restarts,
  and the readbacks would otherwise spend two minutes waiting for that restart and then blame it;
  its own `last_error` arrives within seconds and ends the flow first.
- **The broker connection is not read.** Whether the receiver still holds a connection to the
  broker port is a check the on-hardware drill makes; the integration does not know the port the
  plugin was configured with and does not read the settings to find it.
- **The options flow changed shape for receivers that permit removal**: its form is now the step
  `settings`, behind a menu. Nothing a user configured moves; the form is the same.
- **Reinstalling is the receiver's package manager**, with the entry kept: the plugin comes back on
  its kept broker, node id and mode, and the entry picks it up with every entity id unchanged. The
  guided installer cannot run while the entry exists and would ask for the broker password again.

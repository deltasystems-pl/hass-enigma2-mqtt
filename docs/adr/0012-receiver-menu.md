# ADR-0012: The receiver menu - the receiver sends a row id and a value, Home Assistant decides and performs

**Status:** proposed 2026-10-15
**Date:** 2026-10-15
**Amended:** 2026-10-18 - a toggle says what its row showed, and Home Assistant performs that
intent rather than a flip (section 11)
**Extends:** [ADR-0000](0000-prd.md) §7 - Home Assistant now publishes, retained, the names and
states of the entities the user puts into the menu, and performs services on a message from the
broker. No telemetry, no cloud service and nothing outside the household's network are
unchanged. The receiver's half is the plugin's menu ADR and the `menu` and `menu_action` sections
of its `docs/TOPICS.md`.
**Extended by:** [ADR-0013](0013-menu-creator.md) - the menu can also be edited from the receiver's
web interface. Section 8's options flow stays; its rules are now functions the creator's edit calls
as well, and an edit stored from the receiver does not reload the entry.

## Context

A plugin after 0.4.0 can show a "Home Assistant" menu on the television, opened from the
*Extensions* menu and by a key on the remote: one level of groups, each a list of rows, with six
kinds of control - button, switch, indicator, list of options, slider and text field. The
household decides what is in it, and what is in it lives in Home Assistant: scenes, scripts,
lights, covers, the heating.

The receiver could have been given service calls to send. That would put Home Assistant's
service names, their data and the entities into a payload anyone on the broker can read and
imitate, and it would make the receiver - and whoever can publish as it - decide what Home
Assistant does. The Mosquitto add-on accepts an ACL file and never consults it, so nothing on
the broker keeps a publisher from sending what the receiver sends.

## Decision

**1. The receiver never learns what a row does.** Home Assistant publishes the whole menu on
`enigma2mqtt/menu/<node_id>` - retained, QoS 1, outside the receiver's base topic, like
`enigma2mqtt/integration/<node_id>`, because it is this integration's - and the receiver answers
an OK with `<base>/<node_id>/menu_action`: an action id, the menu's `rev`, a row id, the action
(`press`, `toggle` or `set`) and, for `set`, a value. Which service that is, with which data, is
decided here from the options. The integration publishes no service name and no service data.

**2. `menu_action` is untrusted input, and the menu is the whole of its power.** An action is
performed only for a receiver whose `info` claims `menu`; only when it is not retained and its id
was not just seen; only for a row the saved options hold now, with the action that row's control
takes; only on an entity that is available; only with a value inside what the entity accepts
now - an option it offers, a number inside the slider's range snapped to its step, a text no
longer than the entity's maximum; and only when its `ts` is at most 120 seconds before or after
Home Assistant's clock. The plugin never queues an action across a disconnect, so an older one
was pressed for a screen that is gone; one stamped further ahead comes from a clock the age
limit cannot judge, and would pass it for as long as that clock is ahead. An action without `ts`
is accepted, and one whose `ts` is not a number is refused. And only one at a time per row and
five a second per receiver: an action on a row whose last action is still being performed is
refused, and so is one beyond five in any second. The five are counted only over actions that
passed every check that needs no service call - the row, whether the last menu carried it, the
action its control takes, its entity's availability and the value, all checked before the
action is counted - so an action refused for any cause costs nothing, and junk on the broker
cannot use up the second a real press needs; a key held down, or anyone on the broker, cannot
turn the menu into a stream of service calls. A toggle whose target is already met calls no
service and is neither counted nor held (section 11). A selector's value is checked against the options
in the words of the last menu published, which are the words the receiver shows. "Still being
performed" depends on the control. A button - a script, a scene, an automation, a button
entity - is fire-and-forget: its call is made without waiting for what it starts, so a script
with a long delay does not keep its row busy for the whole run, and the row is free again as
soon as Home Assistant accepted the call. Every other row waits for its call, because the next
toggle or value should go by what the last one did - but for at most 30 seconds; then the row
takes the next action, with one warning in the log, and the call goes on, uncancelled, since a
device in the middle of a command is not interrupted for the menu's sake. Only unloading the
entry cancels it, and then no event follows. Any exception a
service raises is logged as one line and performs nothing more. Anything else is refused with a log line - a warning the first
time for each cause, then debug, because anyone on the broker can send these and the log must
not be theirs to fill - and nothing is called. A text that reads as a number is not a number.

**3. A row's id is generated once and stands for one binding.** `r_` or `g_` and 40 random bits,
stored with the row in the options, so it survives restarts and reloads and the receiver can keep
a row selected across publishes. It stays while only the row's name or icon change. A row bound
to another entity, or shown as another control, gets a new id: a press the receiver sent for the
old binding then names a row the menu no longer holds and is refused. That is why `rev` is
logged and otherwise not consulted - an older `rev` is accepted when the row still exists, so a
state change between drawing the menu and pressing OK never makes the remote dead, and a binding
that changed in between cannot be reached by an id that no longer exists. Random rather than
counted, so a removed row's id is never handed to the next one.

**4. The control follows from the domain, with a choice where a domain allows two.** Scripts,
scenes, buttons and automations are buttons and nothing else; lights, switches, input booleans,
fans, covers and locks are switches - a light also a brightness slider and a cover also a
position slider; select, input select and a climate's HVAC mode are lists of options; numbers,
input numbers and a player's volume are sliders; input texts and texts are text fields; and any
other entity, of any domain, can be an indicator showing its state. A cover and a lock take
the explicit pair by the entity's state when the action arrives (open -> close, locked ->
unlock) - not the state the row showed when OK was pressed - rather than a
toggle, and refuse when that state is neither. A toggle that says what its row showed is
taken by section 11 instead. Percent sliders move in tens, because they are
moved with the arrow keys of a remote; an entity's own range keeps its step, coarsened by a whole
multiple to at most 200 steps.

**5. What the row shows is Home Assistant's own word for the state, in the household's
language.** The lookup is the frontend's - the entity's translation key, its device class, its
domain - over the translations loaded for the bound domains. A climate's modes are shown in that
language and mapped back to the entity's own word on `set`; two options that read the same once
translated or cut fall back to the entity's word, or are left out. **Green means all good**: on,
open and locked are green and their opposites red, except that a binary sensor whose "on" needs
attention - a door, a window, smoke, a leak, a problem, an update waiting, a vibration, a
sound - is green while off, as the contract's example of a closed door shows. The plugin draws the colour it is given; the rule is this
integration's. An entity in password mode is published without its text - no `state`, no `value`
and no `color`, which for a text of "on" would say it - whatever control the row shows it as, an indicator included.

**6. Publishing follows the contract's bounds and debounce.** At setup, after the options are
saved, when the receiver comes back online, when the broker connection comes back (a broker
restarted without persistence has lost the retained menu, and the receiver then says `online`
without having been seen offline), and when a bound entity changes: the first change at
once and a burst after it as one publish half a second later, which keeps both "at most one every
0.5 s" and "within 1 s of a change". A change the payload does not show publishes nothing - except after a toggle whose target was
already met, which publishes the unchanged menu again (section 11). Eight
groups, 24 entries per list, labels at 60, state texts at 24, options at 24 of 60, units at 8; the
whole payload under 64 KiB - measured with the `rev` it goes out with - dropping from the end
with one warning per thing left out. A row left out is not on the receiver, and an action naming
it is refused as an unknown row. `rev` counts the publishes of one Home Assistant run and
survives reloads within it.

**7. The menu goes with the entry.** Removing the entry publishes the empty retained payload,
which the receiver reads as "no menu". Reconfiguring to a new node ID retracts the old node's
menu. Unloading and reloading do not retract: the television would blank for the length of the
reload. A publish still under way when the entry stops - waiting for translations - publishes
nothing, so it can land neither after the retraction nor after the next run's menu.

**8. The options build the menu in a menu step, not in sections.** The menu is a list whose
length the user decides; sections are a fixed set of fields, and eight groups of 24 rows would be
hundreds of mostly empty fields. A menu step with add, change and remove, each a short form with
Home Assistant's own selectors - the entity picker, a list of the plugin's icons, the controls the
entity allows - edits a working copy that is stored only by *Save the menu*. A move step puts an
entry one place up or down within its own list. The step is offered for a receiver that claims
`menu` and for any entry that holds a menu, so a menu can always be emptied.

**9. An event follows every performed action.** `enigma2_mqtt_menu_action` with the device, the
node, the row, the entity, the action and the value applied, so an automation can follow what
the viewer did. For a button it is fired once Home Assistant accepted the call, since nothing
waits for more; for every other row once the service call returned without an error - also
when that was after the 30 seconds that freed the row, because the event says what was
performed, and a call that has not returned has not been. A failing call is logged and fires
nothing. No entity is added for the menu.

**10. A row stores the entity's registry id.** So a row follows its entity through a rename of
its entity id: the state, the label and the service calls use the entity id the registry gives
now, and the state is followed under the new id from the moment the registry says so. An entity
without a registry entry is stored by its entity id; both forms are read, and a row stored by
entity id that is saved again once the entity has a registry entry keeps its row id. A registry
id that names nothing any more, or now names an entity the row's control does not fit, shows the
row greyed and refuses every action on it.

**11. A toggle says what its row showed, and Home Assistant performs the intent, not the
flip.** An OK can reach Home Assistant late - a network stall shorter
than the MQTT keepalive keeps the session, and the press waits in a socket buffer - and by then
an earlier OK may have changed the entity. A plain `toggle` then does the opposite of what the
viewer meant: the row showed "off", the lamp went on meanwhile, and the late OK switched it off.
Refusing an older `rev` is no answer (section 3 accepts one on purpose), so the action carries
the intent instead. A `toggle` may carry `seen`: the `value` the row showed when OK was pressed -
`true`, `false`, or `null` for a row that showed no state. With `true` or `false` the viewer
meant the other value, and Home Assistant calls the explicit service for it - `turn_on` or
`turn_off`, `open_cover` or `close_cover`, `lock` or `unlock` - so a late or repeated OK repeats
the intent and never inverts it. When the entity already is there, judged by the same reading
of its state that builds the row's `value` (a cover is open exactly when its row would show
`true`, opening included), nothing is called, one debug line is logged, no event is fired since
nothing was performed, and the menu is published again - forced, since nothing in it changed -
so the receiver redraws the row from the current state. Such an action calls no service, so it
is not counted towards the five a second and does not hold its row; its publish goes through
the menu's debouncer like any other. A state that is neither on nor off (unknown, jammed) is not
"already there", so the explicit service is called rather than refused. `seen` absent or `null`
is the toggle of section 4 unchanged - a plugin that does not send it gets exactly the earlier
behaviour. `seen` of any other type, or on `press` or `set`, is refused as malformed. Every other
check - the row, its control, its availability, the age of `ts`, the duplicate id - is the same
as for any action, and `rev` is still not consulted.

## Consequences

- The receiver can only ever do what the user put into the menu, and so can anyone who can
  publish to the broker. The documentation says so where the menu is configured: only put into
  it what you would hand to anyone holding the remote.
- The names and states of what is in the menu are on the broker, retained, for any login to
  read. A password field's text is not.
- The icon names are a contract shared with the plugin: the options offer exactly the plugin's
  list, and a name the plugin does not know is drawn as the neutral icon, so the list may only
  grow on the plugin's side first.
- A receiver whose clock is more than two minutes behind or ahead of Home Assistant's has every
  action refused; the log says why. A clock behind also raises a repair (not fixable, one per
  receiver) at the first action refused as stale, which the next accepted action - one stamped
  inside the limit - deletes, as does removing the entry; unloading leaves it, like the
  integration's other repairs. An action without `ts` says nothing about the clock and leaves it.
  A clock ahead raises none.
- Removing an entity from the registry greys its row; it stays in the options until it is
  removed or bound again.

# ADR-0012: The receiver menu - the receiver sends a row id and a value, Home Assistant decides and performs

**Status:** proposed 2026-10-15
**Date:** 2026-10-15
**Extends:** [ADR-0000](0000-prd.md) §7 - Home Assistant now publishes, retained, the names and
states of the entities the user puts into the menu, and performs services on a message from the
broker. No telemetry, no cloud service and nothing outside the household's network are
unchanged. The receiver's half is the plugin's menu ADR and the `menu` and `menu_action` sections
of its `docs/TOPICS.md`.

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
is accepted, and one whose `ts` is not a number is refused. Anything else is refused with a log line - a warning the first
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
the explicit pair by the state the row showed (open -> close, locked -> unlock) rather than a
toggle, and refuse when that state is neither. Percent sliders move in tens, because they are
moved with the arrow keys of a remote; an entity's own range keeps its step, coarsened by a whole
multiple to at most 200 steps.

**5. What the row shows is Home Assistant's own word for the state, in the household's
language.** The lookup is the frontend's - the entity's translation key, its device class, its
domain - over the translations loaded for the bound domains. A climate's modes are shown in that
language and mapped back to the entity's own word on `set`; two options that read the same once
translated or cut fall back to the entity's word, or are left out. **Green means all good**: on,
open and locked are green and their opposites red, except that a binary sensor whose "on" needs
attention - a door, a window, smoke, a leak, a problem - is green while off, as the contract's
example of a closed door shows. The plugin draws the colour it is given; the rule is this
integration's. An entity in password mode is published without its text - no `state` text and
no text `value` - whatever control the row shows it as, an indicator included.

**6. Publishing follows the contract's bounds and debounce.** At setup, after the options are
saved, when the receiver comes back online, and when a bound entity changes: the first change at
once and a burst after it as one publish half a second later, which keeps both "at most one every
0.5 s" and "within 1 s of a change". A change the payload does not show publishes nothing. Eight
groups, 24 entries per list, labels at 60, state texts at 24, options at 24 of 60, units at 8; the
whole payload under 64 KiB, dropping from the end with one warning per thing left out. `rev`
counts the publishes of one Home Assistant run and survives reloads within it.

**7. The menu goes with the entry.** Removing the entry publishes the empty retained payload,
which the receiver reads as "no menu". Reconfiguring to a new node ID retracts the old node's
menu. Unloading and reloading do not retract: the television would blank for the length of the
reload.

**8. The options build the menu in a menu step, not in sections.** The menu is a list whose
length the user decides; sections are a fixed set of fields, and eight groups of 24 rows would be
hundreds of mostly empty fields. A menu step with add, change and remove, each a short form with
Home Assistant's own selectors - the entity picker, a list of the plugin's icons, the controls the
entity allows - edits a working copy that is stored only by *Save the menu*. A move step puts an
entry one place up or down within its own list. The step is offered for a receiver that claims
`menu` and for any entry that holds a menu, so a menu can always be emptied.

**10. A row stores the entity's registry id.** So a row follows its entity through a rename of
its entity id: the state, the label and the service calls use the entity id the registry gives
now, and the state is followed under the new id from the moment the registry says so. An entity
without a registry entry is stored by its entity id; both forms are read, and a row stored by
entity id that is saved again once the entity has a registry entry keeps its row id. A registry
id that names nothing any more, or now names an entity the row's control does not fit, shows the
row greyed and refuses every action on it.

**9. An event follows every performed action.** `enigma2_mqtt_menu_action` with the device, the
node, the row, the entity, the action and the value applied, fired after the service call
returned, so an automation can follow what the viewer did. A failing call is logged and fires
nothing. No entity is added for the menu.

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

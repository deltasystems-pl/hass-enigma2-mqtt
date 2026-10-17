# ADR-0013: The menu creator - Home Assistant publishes a catalogue, the receiver sends the edited menu back, Home Assistant decides and stores

**Status:** proposed 2026-10-16
**Date:** 2026-10-16
**Extends:** [ADR-0012](0012-receiver-menu.md) - the menu can now also be edited from a page in the
receiver's web interface, and [ADR-0000](0000-prd.md) §7 - Home Assistant now publishes, retained,
the name, area and possible controls of every entity it has. The receiver's half is the plugin's
menu creator ADR and the `menu_catalogue`, `menu_edit` and `menu_edit_result` sections of its
`docs/TOPICS.md`.

## Context

The receiver menu of ADR-0012 is built in this integration's options: a menu step with a short
form for every change. That works, and it is slow for a menu of twenty rows - every row is a
dialog of its own, and the menu itself is only visible as a list of text. A plugin after 0.4.0
offers a page in the receiver's web interface where the whole menu is laid out and edited at once:
groups and rows, moved, renamed and bound to entities with a search like Home Assistant's entity
picker.

That page needs to know which entities there are, and its result has to end up in this
integration's options, because Home Assistant performs what the menu offers. Giving the receiver
a Home Assistant token would let the receiver - and whoever controls it - do anything that token
can. And whatever the page sends travels over the broker, where the Mosquitto add-on accepts an
ACL file and never consults it.

## Decision

**1. Home Assistant stays the owner, and the receiver holds no token.** Home Assistant publishes a
catalogue; the page edits a copy of the menu and the receiver sends the whole edited menu back on
`<base>/<node_id>/menu_edit`; Home Assistant reads it with the options flow's rules, stores it in
the entry's options and answers on `enigma2mqtt/menu_edit_result/<node_id>`. The receiver never
writes the menu anywhere: what it shows is still only what Home Assistant publishes on
`enigma2mqtt/menu/<node_id>`.

**2. The catalogue holds every usable entity and the stored menu.** `enigma2mqtt/menu_catalogue/
<node_id>`, retained, QoS 1, outside the receiver's base topic because it is this integration's,
like the menu: version `1`, the menu's `revision`, the names of the areas, how many entities were
`left_out`, the stored `menu` and the `entities`. An entity is usable when it has a state - a
disabled entity has none, and neither has one that does not exist - which is also what Home
Assistant's entity picker lists; hidden ones and configuration and diagnostic ones are included and
marked (`h`, `k`). Each carries its entity id, the name a row bound to it shows when the user gives
it none (cut at 60), its area - its own, else its device's - as an index into `areas`, and the
controls the options flow offers for it now, the default first. They are sorted by name, then
entity id. The menu is the stored one, ids included, with each row bound to the entity id its
binding names now; a row whose binding names no usable entity carries its stored binding (a
registry id or an entity id) and `missing: true`, so that the page can show it and send it back
unchanged. `label` and `icon` are the user's overrides, `null` where there are none.

**3. The catalogue stays under 1 MiB.** Measured as compact UTF-8 JSON. Over it, entities are left
out in this order until it fits: hidden ones, then diagnostic, then configuration, then the rest -
each from the end of the name order - `left_out` says how many, and one warning goes to the log
for each catalogue published that way. The menu is never cut; its own bounds keep it far below the
limit.

**4. When the catalogue is published.** Only while the receiver claims `menu_editor` - which a
plugin does while its menu permission is on and its web interface page is registered. At setup;
within a second of a stored menu change; when the receiver comes back online and when the broker
connection comes back (both published even when nothing changed, since a broker restarted without
persistence lost it); once Home Assistant has started, since entities get their states while it
starts; and when the entity, area or device registry changes - debounced, at most one publish
every five seconds, the burst published at its end. Not on a state change: a light's possible
controls can change with its state, the next registry change or forced publish catches that, and an
edit is checked against the states at the moment it arrives anyway. A catalogue identical to the one
published last is not published again, except in the two forced cases. When the claim disappears
from a later `info`, the catalogue is retracted (the empty retained payload, read as "no
catalogue"); a receiver that states its capabilities without the claim gets the empty payload once
per Home Assistant run as well, so that a catalogue an earlier run left on the broker does not stay
there. A retraction is not urgent, so it waits like a registry change - at most five seconds - and
a claim that comes back meanwhile publishes instead. Removing the entry and reconfiguring it to another node ID retract it; unloading and
reloading do not.

**5. The revision is a content hash.** The first 16 hexadecimal digits of the SHA-256 of the
stored menu as canonical JSON - sorted keys, no spaces, UTF-8 without escaping; the empty menu is
the hash of `[]`. It changes exactly when the stored menu changes, whoever changed it - the options
flow knows nothing of the creator and need not - and there is no counter to keep or migrate.

**6. An edit is untrusted input, read in a fixed order.** A retained `menu_edit` is ignored. A
receiver that does not claim `menu_editor` is answered `not_claimed`. An edit over 65 536 bytes,
not UTF-8, not JSON, not a JSON object, or without a usable `id` (12 lowercase hexadecimal digits)
is ignored with no answer; one with a usable `id` and a `base` that is not a revision is answered
`invalid` with the path `base`. Then: **one save at a time per receiver, and at most one accepted
save every two seconds** - anything more is `busy`; refusals do not count towards the two seconds.
Then the revision the edit started from: any other than the current one is **`stale`**, decided
before the menu is read. Then the menu itself. `ts` is logged and nothing else - a save has no time
meaning, so the 120-second rule of `menu_action` does not apply. Each refusal is logged as a warning
the first time for its cause and quietly after that, because anyone on the broker can send these.

**7. The menu is read with the options flow's own rules, by the same code.** The flow's checks are
functions in `menu.py` that both call: at most eight groups and 24 entries in the top list and in
every group, one level; a name on one line, cut at 60, required for a group and dropped when empty
for a row; an icon from the plugin's list, else `neutral` for a group and none for a row; a row's
entity is an entity id that is usable now - or, for a row whose id is stored, exactly that row's
stored binding, which lets a row whose entity is missing survive an edit of the rest of the menu
(its control then cannot change, since there is no entity to check it against); a control that
entity may be shown as now. An id that is not `null` must be in the stored menu, of the same kind,
and appear once. A row keeps its id while it stays bound to the same entity as the same control,
and gets a new one otherwise, as in the flow; a new entry gets a new one; a group keeps its id. Any
failure is answered `invalid` with the path of what failed (`menu[2].rows[5].control`) and one
English sentence of at most 200 characters, and nothing is stored.

**8. An accepted edit is stored without a reload.** In the entry's options, in the form the flow
stores - registry ids where the entity has a registry entry, and no menu key at all for an emptied
menu - and the menu and the catalogue are republished within a second. The entry is not reloaded,
so no entity goes unavailable for a menu change. The answer is `saved` with the new revision, after
the new catalogue has been handed to the broker; an accepted edit that changes nothing is `saved`
with the same revision. Saving the menu in the options flow still reloads the entry. A flow opened
before a save from the receiver works on the older copy, and saving it overwrites that save: Home
Assistant is the owner, and the flow is a second editor, not a merge.

**9. Every edit with a usable id is answered.** `enigma2mqtt/menu_edit_result/<node_id>`, not
retained, QoS 1: the edit's `id`, the `result` (`saved`, `stale`, `invalid`, `busy`,
`not_claimed`), the revision after handling, and for `invalid` the `path` and the `detail` - both
`null` otherwise.

**10. Editing from the receiver is always on.** There is no switch for it on either side, by the
maintainer's choice: whoever can publish to the broker can replace the menu with any entities Home
Assistant has, as whoever can publish there can already press its rows, and whoever can read the
broker can read the catalogue. The documentation and SECURITY.md say so plainly.

**11. The menu's two receiver settings are entities too.** "Menu transparency" (a number, 0-80 %
in steps of 10) and "Menu hotkey" (a select of `help_long`, `red_long`, `f3`, `f4`, `off`), both
configuration entities of the receiver's device. Each is written with a `cmd/config` that carries
only its own key - partial `cmd/config`, which every plugin that reports `menu_transparency`
accepts - and waits until `info.settings` says the new value; a refusal on `last_error` is raised.
They are created for a receiver whose `info.settings` carries `menu_transparency` (the hotkey also
needs `menu_hotkey`), never removed by this integration when a later `info` stops carrying it -
that is an older plugin or one that has not answered yet - and unavailable meanwhile. Their values
are always the receiver's: no default of them is kept on this side.

## Consequences

- Anyone who can publish to the broker can replace the menu, with any entity Home Assistant has,
  and so choose what the television's menu controls. The menu's own rules still hold - what a row
  can do is what its control does to its entity - but the choice of entity is no longer only the
  user's. Put the broker where only the household can reach it.
- The name, area and possible controls of every entity Home Assistant has - hidden ones included
  - are retained on the broker for any login to read while a receiver claims the editor.
- An options-flow save after a save from the receiver overwrites it.
- A catalogue that leaves entities out cannot offer them on the page; they can still be bound in
  the options flow.
- The flow's rules and the edit's are the same functions, so a rule changed for one changes for
  the other.

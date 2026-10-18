# ADR-0014: A list of choices - the receiver answers with a row id, once; the action returns it and the bus carries it

**Status:** proposed 2026-10-18
**Date:** 2026-10-18
**Extends:** [ADR-0010](0010-a-command-carries-an-id-and-a-refusal-is-matched-by-it.md) - the
command's id is also the id an answer is matched by. The receiver's half is the plugin's list ADR
and the `cmd/list` and `list_answer` sections of its `docs/TOPICS.md`.

## Context

A plugin after 0.4.0 can put a question on the television, on top of whatever is on screen - live
TV, a recording, its own menu: a title, an optional text and up to 24 labelled rows, each with an
optional colour or number key. "Doorbell: open the gate / ignore", "The TV goes off in a minute:
+15 minutes / cancel". The viewer picks a row with the remote, presses EXIT, or does nothing until
the list's timeout.

Home Assistant has two ways to hand such an answer to an automation. An action can return data -
the caller writes `response_variable:` and reads the answer on the next line. A bus event can
start an automation that was not running when the question was asked, and device triggers make
that event something the automation editor offers. A script asks and waits; a dashboard button
asks and lets go; a household that asks from one automation and acts from another needs the
event.

The broker is not a trusted party. The Mosquitto add-on does not enforce an ACL, so anybody who can
publish can show a list on the television, and anybody can publish an answer for any list.

## Decision

**1. The receiver never learns what a row does.** `cmd/list` carries texts and row ids, and
`list_answer` carries one row id back - `{"id", "ending", "row", "ts"}`. Whatever "open the gate"
means happens in the automation that asked, as for the receiver menu ([ADR-0012](0012-receiver-menu.md)).
A yes/no question is a list of two rows.

**2. One action, two ways to call it.** `enigma2_mqtt.show_list` is an entity action on the media
player with `SupportsResponse.OPTIONAL`. Called **with** a response it waits, and returns
`{"ending", "row", "label"}`: `ending` is the receiver's - `picked`, `timeout`, `dismissed`,
`replaced`, `closed` - or `no_answer`, Home Assistant's own; `row` is the picked row's id, or for a
timeout the default row's id; `label` is that row's label as it was sent. Called **without** one it
returns once the list is published. The wait ends at the list's own timeout plus 15 seconds as
`no_answer`, whatever happened in between: there is no list without a timeout (5 to 600 seconds,
60 by default), so there is no wait without an end. The receiver going offline does not end it
earlier - the list stays on the television, and a choice made after the receiver is back is still
answered.

**3. The list's id is the command's id.** Sixteen random hexadecimal digits in the envelope of
[ADR-0010](0010-a-command-carries-an-id-and-a-refusal-is-matched-by-it.md); the answer and a refusal
on `last_error` say it back. A list is never sent bare - `list` implies `command_id`, and a list
whose envelope would pass the receiver's 4096 bytes is refused before it is sent, rather than sent
without the id that makes its answer readable. A refusal ends the wait only when it says the list's
id: one with another id, or without one, is about another command or another client's list. The
refusal is read as it arrives, whether the plugin published it retained or not - while a silent
self-update holds the receiver it sends the doors' refusal of a list non-retained, so that it cannot
outlive a downgrade's retraction. A message published while the subscription is open reaches it with
the retain flag clear either way; only the broker's replay of an older `last_error` on subscribing
carries the flag, and that one is never a list's answer.

**4. The answer is never queued.** The plugin takes an unacknowledged answer out of its client's
queue when the broker connection drops, as it does `menu_action`: a choice made while the broker is
away is lost, and the caller's wait ends as `no_answer`. A late "open the gate" is worse than none.

**5. The answer is untrusted input.** It is read strictly: an id of the plugin's command id rule
(1 to 64 letters, digits or `.` `_` `:` `-`) or none at all, an ending of the five, and a row of
the shape its ending takes - a row id for `picked`, a row id or `null` for `timeout`, `null` for
the rest. A member this release does not know is ignored, so the plugin can add one. Anything else
is dropped with one log line - a warning the first time for its cause, quietly after that - that
names the cause and the member, never what the sender wrote. An answer to a list this Home
Assistant sent must also name a row of that list, and for a timeout the default it was sent with.
A retained answer is a leftover, and a second answer with an id already answered is a duplicate
delivery or a copy: both are dropped, so a list is answered once.

**6. Every readable answer is an event, and three are device triggers.** `enigma2_mqtt_list_answer`
carries `device_id`, `node_id`, `list_id`, `ending`, `row` and `label` - the label only for a list
this Home Assistant sent, `null` otherwise - and it is fired also for a list another client or an
older run sent: the receiver answered, whoever asked. The receiver offers *a row of a list was
picked* (any row, or the row whose id the trigger names), *a list timed out* and *a list was
dismissed*. `replaced` and `closed` are no one's choice and have no trigger; they are on the event.

**7. The text on the screen is cleaned the same way on both sides.** A title, the text and every
label are cleaned by the plugin's rule, in its order: every backslash removed; in the text,
`\r\n` and `\r` made a line break; every Unicode Cf and Cs character dropped; every Cc, Zl and Zp
character made a space, except a line break in the text; cut at the limit (60, or 200 for the
text); the ends stripped. Blank is judged on the result. Home Assistant applies it before its own
checks, so it refuses in the caller's language exactly what the receiver would refuse as blank, and
the label it reports back is the one on the screen. Both repositories test the same vectors.

**8. Several receivers answer one each.** A target of more than one receiver shows the list on
each, and the response has one entry per media player, each answer its own event and trigger.
Home Assistant gathers an entity action's calls and raises the first exception, which would throw
away every answer that did arrive; so with several targets and a response, a receiver that refuses
or cannot be reached returns `{"ending": "refused", "error": <the translated sentence>, "row":
null, "label": null}` in its own entry. `refused` is Home Assistant's, never a receiver's. One
target raises as before, a mistake in the fields is raised for any number of targets, and without
a response there is no entry, so a failure is raised once every receiver's call has run.

**9. No permission and no option.** A list changes nothing on the receiver and calls nothing in
Home Assistant by itself, like a message. What it can cost is a question on the screen nobody
asked for, and an answer nobody gave - which is why SECURITY.md says that an automation must not
treat an answer as proof of who pressed what.

## Consequences

- An automation that opens a gate from an answer opens it for anybody who can publish on the
  broker. The documentation says so where the doorbell example is, and SECURITY.md says it again;
  a lock or an alarm needs a second factor this integration cannot provide.
- A caller with a response blocks for up to the list's timeout plus 15 seconds. The media player's
  actions run in parallel (`PARALLEL_UPDATES = 0`), so a waiting list holds nothing else up.
- Unloading or reloading the entry ends every wait with an error, `list_unloaded`: nothing reads
  the receiver's answers between the unload and the next setup.
- The response is keyed by the media player it came from, like every entity action's; a target of
  several receivers shows the list on each and returns one answer per receiver (section 8).
- A field of the action is checked here, in the household's language and with the row's number,
  before anything is published - a payload the receiver would refuse on `last_error` is refused
  in the dialog instead. An unknown member of a row is refused here, though the receiver ignores
  one: a misspelt `key` would otherwise vanish without a word.

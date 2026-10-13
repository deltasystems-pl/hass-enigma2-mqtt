# ADR-0010: A command carries an id where the plugin takes one, and a refusal is matched by it

**Status:** accepted 2026-10-13
**Date:** 2026-10-13
**Extends:** [ADR-0003](0003-control-feedback-and-household-features.md) - how a control learns
that the receiver refused. The receiver's half is the plugin's
[ADR-0016](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0016-a-commands-id-comes-in-an-envelope-and-only-a-refusal-says-it-back.md).

## Context

A command has no acknowledgement. It is proved by the state topic it moves and disproved by
`last_error`, which names the refused command in `cmd`. The integration therefore failed every
command it was waiting on whose name equalled `last_error.cmd`, and that is wrong in three cases:

- two commands of one name in flight - two zaps, two volume steps - are both failed by the
  refusal of one;
- a refusal of somebody else's command of that name fails ours: another broker client, an
  automation publishing by hand, the receiver's own page;
- a refusal made before our command had left Home Assistant fails it.

The update over MQTT and the remote uninstall had the same rule in their own words: any fresh
`last_error` about `update`, or about `uninstall`, was the answer to the request being followed.

A plugin after 0.4.0 has an answer for the first two. A sender may put a command's payload in an
envelope, `{"id": ..., "payload": ...}`, and a refusal of that command says the id back as
`last_error.id` - also when the refusal is published later, or by the plugin process that starts
after an update. Success is still answered by the state topic and nothing else. A plugin says it
opens the envelope with the capability `command_id`; one that does not claim it takes an envelope
for the command's own payload, and refuses most commands sent in one.

## Decision

**1. An envelope is sent to a plugin whose `info` claims `command_id` now, and to no other.** The
question is asked for each command at the moment it is published, of `info` alone: an `info` that
has not arrived claims nothing, a retracted one claims nothing although its last payload is kept
for the device page, and the announcement is not asked. A plugin that gains the capability with
an update, or loses it with a downgrade, is sent what it takes from its next `info` on. Not known
is "no": the cost of a wrong "yes" is a command the receiver refuses, the cost of a wrong "no" is
the matching of before.

**2. The id is 16 random hexadecimal digits, new for every command.** That is 64 bits in a
quarter of the 64 characters the plugin allows. An id has to differ only from the others being
waited for at the same time - seconds for a command, minutes for an update - and 64 bits do
that. A shorter id is also dropped less often: the plugin drops an id that holds one of its
secrets, and the fewer digits there are, the less often they happen to spell a short password
made of digits and the letters a to f. For the same reason there is no prefix: a fixed word in
front of every id could hold a short password every time.

**3. The payload travels as a string, also when it is the text of a JSON object.** The plugin
takes an object there too, but it writes an object again before the command reads it: other
spacing, other escapes, a number spelled its encoder's way. A string is handed on character for
character. So a command reads exactly the same text from a plugin with ids as from one without,
nothing here has to know which payloads are objects, and a payload that only looks like an object
is not turned into one. The price is one backslash for every quote in the payload.

**4. The envelope never costs a command its place.** The plugin discards a command over 4096
bytes unanswered, and the envelope counts. A command that fits only without it is sent without
it, and so is a payload UTF-8 cannot carry, which fails in the publish as it did before.

**5. Whose refusal a `last_error` is:**

| The refusal says | It is about a waiting command when |
|---|---|
| an id | that is the command's own id - whatever name it gives |
| another id, or any id for a command sent without one | never |
| no id | it names the command, as before - and the command has been published |

Only a non-empty string is an id; the plugin leaves the member out rather than sending `null`.
A retained `last_error` is nobody's answer, as before.

**6. A refusal without an id keeps the old rule.** It cannot be told apart from somebody else's,
and it can be ours: the plugin drops an id that holds one of its secrets and runs the command
without it; a plugin that does not claim the capability says no ids; and an install started at
the television, which the update card must not take for its own, says none either. Reading every
refusal without an id as "not ours" would turn the first case into a command that times out with
no reason given, in exactly the households whose broker password is short.

**7. Two things are tightened for every plugin, with ids or without.** A complaint that arrives
before the command has been published is not about it - the command, the update request and the
uninstall each mark the moment they go out. And a refusal that says an id is never about a
command that went out without one: only an envelope gives a command an id.

**8. What is not put in an envelope.**

| Command | Why |
|---|---|
| `cmd/relay` | Not a command: Home Assistant's answer to the receiver's `relay_request`. The plugin opens no envelope there, it never reaches `last_error`, and its `id` is the request's |
| `cmd/ha_mode` from the config flow and for a removed entry | There is no runtime object that has read `info`. Its answer is `info`, and nobody reads its refusal |
| `cmd/reset` after a downgrade over SSH | Sent to an older plugin that has only just started; the payload is ignored |
| `cmd/bouquet` from the installer's restart | The installer has no runtime object; its answer is the `bouquet` topic |

All four are bare payloads, which every plugin takes.

**9. The id is plumbing.** It is in no entity's state or attributes - „Ostatni błąd" keeps the
command, the sentence, the time and the source - and in no event; an id there would be a new row
in the recorder for every refusal. The diagnostics download carries `last_error` as it arrived,
id included, and the log says at debug level when a refusal was left alone for its id.

## Alternatives that were not taken

- **The payload as an object when it is one.** Fewer bytes for a payload with many quotes. But
  the command would then read other text from a plugin with ids than from one without, and "the
  same value" would have to be argued for every command that takes an object.
- **Ignoring a refusal without an id once an id was sent.** It closes the last gap - an install
  started at the television in the same minute as ours - and opens a worse one (decision 6).
- **Turning the capability on by plugin version.** `info.plugin` says a version; the capability
  says the envelope is opened. A development build and a fork answer the second question only.
- **Envelopes from the installer and the config flow.** They would have to read `info` for a
  command whose refusal they do not wait for.
- **A longer id.** 32 digits were built first. They buy nothing a command's wait needs, cost 16
  more bytes of every command, and double the chance of an id the plugin drops.
- **Showing the id on „Ostatni błąd".** It says nothing a person can act on.

## Consequences

- **Two commands of one name are refused one by one**, and another client's refusal no longer
  fails a waiting button, action, update or uninstall - with a plugin that claims `command_id`
  and a sender that uses ids. A client that sends without an id is still read by name.
- **Nothing on the wire changes for a plugin without the capability.** Every command is the
  bytes it was; the differential test in `tests/test_command_id.py` sends each kind of command
  to both and compares.
- **The largest command that still gets an id is 38 bytes smaller than the plugin's limit**, less
  one byte for every quote and backslash in it. Past that the command goes out bare.
- **A command the plugin reports under another name is now matched.** With the id, the name in
  `last_error.cmd` is not asked a second time.
- **The update card's `last_refusal` is unchanged.** It shows the receiver's last refusal of an
  update whoever asked - that is its purpose - and so does not look at the id.
- **A new sender has to go through the runtime object**, or be added to the list of decision 8;
  a test holds the list of modules that build a command topic.

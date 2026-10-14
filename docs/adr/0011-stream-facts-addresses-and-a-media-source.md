# ADR-0011: Stream facts, addresses and a media source - no camera, the user's own go2rtc, and hiding done here

**Status:** accepted 2026-10-13
**Date:** 2026-10-13
**Amended:** 2026-10-14 - checked against the plugin's stream area as submitted for review
**Extends:** [ADR-0000](0000-prd.md) §7 - Home Assistant now also sends one request per channel
change to a go2rtc address the user typed, and hands players the receiver's own stream address.
No telemetry, no cloud service and nothing outside the household's network are unchanged.
The receiver's half is the plugin's ADR-0017, ADR-0018 and ADR-0019 and the `stream`,
`stream_clients`, `stream_playable`, `service` and `volume` sections of its `docs/TOPICS.md`.

## Context

Every enigma2 image serves a service's transport stream over HTTP to whoever asks. Plugin 0.5.0
publishes what a consumer needs to use that: where the stream server is (`stream`), who it is
serving (`stream_clients`), whether a service could be played now (`cmd/stream_playable`), what
is on the decoder (the codec members of `service`), and whether the receiver's volume acts on
the audio being played (`volume.passthrough`). It starts no stream and carries no video.

Households already do something with those streams: a go2rtc of their own, with one stream per
channel named after the service reference, and a dashboard card. What they lacked was the
address of the channel that is on, and something that keeps one go2rtc stream on it.

A receiver's stream is MPEG-TS, usually H.264 or HEVC with MPEG or Dolby audio, often
interlaced. A browser plays none of that. Each open stream holds a tuner input: a receiver with
two inputs serves two streams, and a stream opened by a dashboard that nobody is looking at
takes the tuner a recording needed.

The options already have a way to say "these bouquets are nobody else's business": the bouquets
hidden from the zap history. The plugin publishes every channel and every stream client of every
bouquet, on purpose - it is a bridge, and what a household wants hidden differs from one
dashboard to the next.

## Decision

**1. No camera entity, and neither `stream` nor `go2rtc` of Home Assistant.** The integration
exposes facts and addresses: an attribute with the stream address, a media source that resolves
to it, sensors for what is on the decoder. It does not turn the stream into something a browser
plays, and the manifest names neither integration. A camera entity would make Home Assistant
open the stream - for a preview, for a snapshot, for a card somebody left on a dashboard - and
every one of those is a tuner.

**2. A stream address is built here, at the host Home Assistant already asks.** The plugin
publishes a port and the form of a path, never a host. The host is the one the picon lookup uses
([ADR-0009](0009-player-picture-and-picons-from-openwebif.md)): the typed receiver host when
there is one - and then never the reported address - and otherwise the address the receiver
reports, only when it is a plain IP address that is not loopback, link-local, unspecified,
multicast or reserved. The reference is percent-encoded with its colons kept.

**3. Never a login in an address.** While the receiver says its stream server wants a login,
no address is built, the media source does not resolve and go2rtc is not re-pointed. The
integration stores no stream credentials. An attribute says that a login is wanted.

**4. The stream name is `ch_` and the reference, lower-cased, every run of other characters one
underscore.** It is what existing go2rtc configurations use, so it is frozen: a different
transform would orphan every stream somebody already named.

**5. The plugin publishes everything; hiding is the integration's job.** A service of a bouquet
the options hide gets no stream address and no stream name, is not in the media source, is not
sent to go2rtc, and a stream client on it is shown without its channel. One rule decides all of
them - `Enigma2Box.service_hidden`, the zap history's filter asked about one service - and it
fails toward hiding in the cases that filter does. A hidden client can still be stopped: the
action takes the client's address alone, and the reference is taken from the payload the box
holds and put into the command and nowhere else.

**6. The media source refuses where it can know it cannot work, and only there.** Resolving
without a media player is the browser's own player, and a player registered by the `cast`
platform is a Chromecast; both are refused with a sentence. Browsing carries no player, so the
tree is shown to every player. Before an address is handed out the receiver is asked whether the
channel could be played now, where it claims it can be asked; "no" is refused, and no answer
within three seconds is not a "no".

**7. go2rtc is the user's, and is told one thing.** An address in the options switches it on:
one stream, one name, re-pointed with `PUT /api/streams?name=..&src=..` when the service on the
decoder changes. Home Assistant does not install, configure or query that go2rtc, and sends it
nothing else. It does not re-point in standby, for a hidden channel or while a login is wanted;
the stream then stays on the last channel it was given. A source template with white space is
refused on the options form, because go2rtc refuses such a source (measured against 1.9.14:
HTTP 400, "source with spaces may be insecure").

**8. The volume controls stay.** With `volume.passthrough` true the receiver's volume does
nothing audible, and the number and the media player say so in an attribute, with a binary
sensor of its own. They are not made unavailable: the answer follows the audio track, and a
level set now is the level that applies when the next track is decoded. Unknown is unknown,
never "no".

**9. Entities that follow a capability are never removed for it going quiet**, as
[ADR-0005](0005-softcam-restart.md) decided for the softcam sensor: the codec sensors, the
passthrough sensor and the stream-clients sensor stay and say `unknown`.

## Alternatives that were not taken

- **A camera entity through Home Assistant's `go2rtc` or `stream`.** It is what a user asks for
  first. It makes Home Assistant a client of the receiver's stream server whenever something
  wants a frame, on a device that has two tuner inputs, and it needs a transcode the receiver
  cannot do well and Home Assistant should not be assumed to do. Left for a later release, if at
  all, as an explicit opt-in.
- **The receiver's own hardware transcoder** (`stream.encoder_port`). Published by the plugin
  and deliberately unused here: it is not available on most receivers, and where it is, its
  output is the least compatible of the options.
- **Hiding in the plugin.** One setting on the receiver cannot serve a household dashboard and
  an administrator's at once, and the broker would still carry `service` and `zap_history`.
- **An opaque id for each stream client**, to stop a hidden one. It would have to be stable
  across republished lists and would be one more thing in the state machine that maps to a
  channel. The client's address is already there.
- **Pointing go2rtc at nothing for a hidden channel.** It is a second kind of request that says
  "something hidden is on now", and it breaks the card for the channel that was fine.
- **Managing one go2rtc stream per channel.** Hundreds of streams in somebody else's
  configuration; the stream name attribute lets a user who wants that build it.

## Consequences

- A dashboard that shows the stream holds a tuner for as long as it is open. The documentation
  says to open it on a tap and never to preload it.
- A household whose receiver wants a stream login gets facts and no addresses.
- The hidden-bouquets option now decides more than one select. Its key and its label still say
  "Recently watched"; the help text and the documentation say what else it hides.
- Home Assistant calls an address a user typed. It sends the stream name and the source - which
  contains the receiver's address and the service reference of the channel that is on - and
  nothing else; [SECURITY.md](../../SECURITY.md) says so.
- The stream address is an attribute of the media player and is recorded with its state, as the
  service reference in `media_content_id` already is.
- A plugin whose shapes change before 0.5.0 is released changes one module here,
  `stream_facts.py`, which holds every reader of these payloads. That module and `own_go2rtc.py`
  are named so that neither can be taken for a platform of Home Assistant's own `stream` or
  `go2rtc` integration.

## Amendment - 2026-10-14: checked against the plugin's stream area as submitted for review

The integration was written against an earlier draft of the plugin's half. Read against the
plugin's TOPICS.md, contract.json and its ADR-0017, ADR-0018 and ADR-0019 as they stand for
review, four points narrow what is decided above; nothing in it is reversed.

- **Decision 6: the receiver is asked only about one playable service.** The plugin refuses
  `cmd/stream_playable` for a bouquet, a marker, a service with alternatives and an entry hidden
  in its bouquet - the flags in the reference's second field - and a refusal is retained on
  `last_error`, where a household reads it as a fault. The channel list leaves those out by the
  same test, so this only holds a list that broke the contract to it: such a reference is not
  asked about, and its address is handed out as when no answer comes. A refusal of the question,
  by its id or - from a plugin without ids - by the command's name, is "not known", never "no".
- **Decision 9: "say `unknown`" includes a list the broker no longer holds.** The plugin empties
  `stream_clients` when the image's list cannot be read, and a broker deletes a retained message
  on an empty payload, so Home Assistant started afterwards receives nothing on the topic. The
  stream-clients sensor is therefore available whenever the receiver is, and `unknown` until a
  list arrives; only a published empty list is zero.
- **Decision 5: a stop names what the plugin can stop.** A row is kept with a reference of at most
  512 characters, the plugin's own bound; a longer one is no reference and the row cannot be
  stopped. Only a well-formed dotted quad is taken out of the `::ffff:` form. A reference given
  to the action is sent as it was typed, because the plugin matches it as text before it falls
  back to the eleven-field rule. Nothing is sent for a receiver that is offline: nobody would
  answer, and silence is what that path reads as success. Two hidden services streamed to one
  host cannot be stopped by the host alone - neither reference is shown, so nothing can say
  which is meant: both rows say `can_stop` false, and the action refuses with a sentence that
  sends the user to the receiver or to the option, never one that asks for a reference. No
  opaque id is added for them, and they are never stopped together.
- **Decision 7: one warning for a run of failures.** The warning is written for the first failure
  after a success, whatever the failures that follow are; the one repair is replaced under its id
  when the failure changes, so it says the latest.
- **Decision 7: an error answer is read back before it counts as a failure.** Measured on go2rtc
  1.9.14: a PUT that changes a stream's source rewrites go2rtc's configuration file, and when
  go2rtc cannot write it (a `streams` key in flow style) it answers 400 after it has already
  re-pointed the stream in memory. On any answer that is not a 2xx the stream is read back with
  `GET /api/streams?src=<name>` - same session, same bounded wait, no redirect followed - and a
  stream whose producers include exactly the source sent counts as re-pointed: no repair, no
  retry, and one warning in the run of the entry that go2rtc could not save its configuration,
  with the status and a short head of the answer from which anything that could be an address
  is removed. A read-back that fails, cannot be read or shows another source leaves the
  failure as it was. Also measured: a viewer already open when the source changes stays on the
  old source, and keeps the receiver's stream of the previous channel open, until it
  reconnects - so the documentation says to open a card after the channel change. Nothing here
  can move such a viewer, and nothing tries.

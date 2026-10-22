# ADR-0015: Recordings are browsed here, played there, deleted into the trash, and hidden here

**Status:** proposed 2026-10-18
**Date:** 2026-10-18
**Extends:** [ADR-0011](0011-stream-facts-addresses-and-a-media-source.md) - the media source
gains a branch that only the receiver's own media player offers, and the hidden-bouquets rule
that decides what Home Assistant shows about streams now decides what it shows about
recordings. The receiver's half is the plugin's ADR on the recording library and the
`recording`, `timeshift`, `recordings` and `recordings/<folder_slug>` sections of its
`docs/TOPICS.md`.

## Context

Until now Home Assistant knew that a receiver was recording, how many recordings were running,
when the next timer started and whether the recording disk was mounted. Plugin 0.5.0 publishes
each running recording in detail, the timeshift state, and the whole recording library: every
configured movie location with its subfolders, the image's trash left out, as an index and one
retained topic per folder, each bounded to one MQTT packet - a folder too large for one is cut,
oldest first, and says how many it left out. It takes three commands: play a recording, move one
into the trash, and read the library again.

Three facts shape what Home Assistant can do with that.

- **A recording plays on the receiver.** The image's movie player plays it on the television,
  with the image's own rule about where to start. It is not a stream Home Assistant could hand
  to another player, and a stream of a recording would hold a tuner input exactly as a live one
  does ([ADR-0011](0011-stream-facts-addresses-and-a-media-source.md)).
- **Home Assistant browses a media source without saying which player asks.** A branch listed
  in the media source is offered to every player, the browser's own included.
- **The plugin has no rule for hidden bouquets.** It lists every recording, of every bouquet,
  with the service it was recorded from and the first published bouquet holding that service,
  as it lists every channel and every stream client. The integration has the rule: the
  hidden-bouquets option, applied by one function.

## Decision

**1. A recording is addressed by an opaque id, never by a path.** The id is the first sixteen
hexadecimal digits of the sha256 of the recording's path on the receiver. Every command carries
the id and nothing else, and the receiver acts only on a recording its own library lists. Home
Assistant refuses, before anything is sent, an id that is not sixteen hexadecimal digits and one
the library it holds does not show.

**2. The recordings are a branch of the media source that only the receiver's own media player
offers.** The media source serves the branch - the library, its movie locations, their folders,
a folder's recordings - but does not list it under a receiver, so no other player is offered it.
The receiver's media player puts "Recordings" beside its bouquets. Playing a recording there
sends `cmd/play_recording` with its id; nothing is resolved to an address. Resolving a recording
for any player is refused with a sentence. `media_player.play_media` on the receiver also takes
the type `recording` with the id, for an automation. With one movie location its folders are
listed directly.

**3. The receiver lists everything; hiding is done here** - by the hidden-bouquets option, with
a narrower rule than the one streams follow
([ADR-0011](0011-stream-facts-addresses-and-a-media-source.md) decision 5):
`Enigma2Box.recording_hidden(sref, bouquet)` hides a recording **when the bouquet the receiver
names with it is a hidden one, or its channel belongs to any hidden bouquet** - hidden means
hidden everywhere in Home Assistant, so a channel of a hidden bouquet named under another one
stays hidden. It is shown only when no hidden bouquet holds it. A recording the receiver placed
in no bouquet (`bouquet` null), or in one it does not publish, is therefore shown when its
channel is in no hidden bouquet: a recording is a file already made, and one with no bouquet
is mostly an ordinary one - a channel since dropped from every bouquet, a radio service, a
copied file - and hiding all of them took such recordings out of the library on a real
receiver. Where membership cannot be told - a hidden bouquet listed without its channels, a
recording without a reference - it still fails toward hiding. (Amended before release; the
first version used `service_hidden` and hid every recording without a published bouquet.) A recording
the option hides is not in the media browser, not in the count of the library, and cannot be
played or deleted from here: Home Assistant refuses it as a recording it does not know. A running
recording that the option hides is shown without its title, its channel, its bouquet, its file
and its id, and marked `hidden` - but it is counted, as a hidden stream client is: it holds a
tuner, the "Recording" sensor and every guard on the receiver count it, and a dashboard does not
make it stop. **A folder left with no recording to show is not listed, nor a location left
with no such folder** - in the media browser, `get_recordings` or any count - because a
folder's name can say what it holds. The free-space sensor's list of locations follows the same
rule, with one exception: the default location is always in it, because its free space is the
sensor's state. A running recording from an older plugin carries no bouquet and is judged by
its channel alone, with the same rule. Before any channel list has arrived - retained
recordings can land first - every recording is hidden while the option names a bouquet,
because which channels a hidden bouquet holds is not known yet.

**4. Delete moves a recording into the receiver's trash, and only there, behind the receiver's
permission.** `enigma2_mqtt.delete_recording` is refused before anything is sent unless the
receiver claims `recording_delete` - which it does only while its box-only permission
`recording_delete_allowed` is on and the image's trash is switched on - and the refusal says
which of the two is missing, from `info.settings` and `info.recording_trash`. Home Assistant
also refuses a recording that is being recorded. Nothing Home Assistant asks for is deleted for
good.

**5. The library is read on events, and Home Assistant offers the refresh.** The receiver reads
it at its start and when something changes it, never on a timer, so a disk is never woken for a
list. "Refresh recordings" asks for a read now - the one press that may wake a disk - and is a
configuration entity, not something a dashboard needs. Its proof is an index that is not the
broker's replay and says a new `read`, within sixty seconds: the receiver republishes the index
when free space changes, with `read` as it was, and that is not a read. A delete is proved by
the library no longer listing the id, within ninety seconds - longer than the receiver's own
minute for the move, so that its refusal reaches the household before Home Assistant gives up.
A play is proved by `service`: the image writes the path in a reference percent-encoded, and
Home Assistant decodes it before comparing it with the location's resolved path and the file.

**6. The entities stay modest.** One free-space sensor - the default movie location's, with the
locations the library shows something in, and always the default one, in an attribute (decision
3) - rather than one per location; one diagnostic count; one binary
sensor for timeshift; one button. Every list attribute is unrecorded. Entities that follow a
capability are never removed for it going quiet ([ADR-0011](0011-stream-facts-addresses-and-a-media-source.md)
decision 9).

**7. Every reader of the new payloads is in one module**, `recordings.py`, which normalises them:
a value that cannot be believed is `None`, a member the contract does not name is dropped, and
every list is bounded. A shape that changes before the plugin's release changes that module.

**8. The library is an answer, never an attribute.** `enigma2_mqtt.get_recordings` returns the
library as decision 3 shows it - locations, folders by slug and path, and each recording's id,
file, title, description, channel, start, length and size - from the topics already held, as
`get_epg_grid` returns the grids. It names no service reference and no bouquet.

## Alternatives that were not taken

- **Listing the recordings in the media source for every player.** Every player but one would be
  offered something it cannot play, and the refusal could only come when a recording is picked.
- **Streaming a recording to another player.** It is what OpenWebif can do. It holds a tuner
  input only where a live stream would, but it is a second way of watching a recording that the
  receiver's own resume rule does not see, and it would need an address with the file's path in
  it.
- **Addressing a recording by its path.** A path from the broker naming a file to delete is the
  one thing this design exists to avoid.
- **A sensor per movie location.** Most receivers have one location; a few have three or four,
  and entities that come and go with a USB stick are worse than an attribute.
- **Hiding the count of running recordings too.** It would make "is anything recording" disagree
  with the receiver about a recording that is running.
- **Listing a folder that has nothing to show.** A folder that holds only a hidden bouquet's
  recordings is usually named after what is in it.

## Consequences

- The receiver publishes the whole library to the broker, as it publishes every channel and the
  zap history: anybody who can read the broker sees every title of every bouquet.
- A recording copied onto the disk some other way, or deleted on the receiver itself, shows after
  the next event or a press of "Refresh recordings".
- The free space shown moves when the library is read, and at the receiver's minute mount check
  when it has changed - never in between, and never by reading a directory.
- A folder's `cut` is the receiver's count, hidden bouquets' recordings included: nothing here
  is sent them to judge.
- An automation finds a recording's id with `get_recordings`, or on the running recordings'
  attribute.
- `file` is relative to its movie location everywhere, and `locations[0]` is the image's default
  movie location: both are the contract's, and the free-space sensor reads the second.

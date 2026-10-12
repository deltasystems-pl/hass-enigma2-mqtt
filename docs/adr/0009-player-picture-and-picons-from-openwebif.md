# ADR-0009: The media player's picture is a choice, and picons are found by asking OpenWebif

**Status:** accepted 2026-10-12
**Date:** 2026-10-12
**Extends:** [ADR-0000](0000-prd.md) §7 - the integration now also makes HTTP requests to the
receiver's own web interface. No telemetry, no cloud service and nothing outside the household's
network are unchanged.

## Context

The media player showed the plugin's screenshot and nothing else. A user coming from the core
`enigma2` integration, which shows the channel's picon, reported the difference. Both pictures
have a case:

- The screenshot says what is on the television, but it is late. It is taken a few seconds
  after a channel change, so for those seconds the card shows the previous channel under the new
  channel's name - and for as long as the channel stays, on a receiver with screenshots off.
- The picon is right the moment the channel changes and says nothing about the programme.

Picons were already used for the media browser's thumbnails, by building one file name from the
service reference. That name is only the first of several OpenWebif tries: it also looks under
the reference with the sub-network removed from the namespace, under another reference type and
service type, and under the channel's name in two spellings, and newer images hand the whole
question to their own picon resolver. A second copy of those rules here would be wrong on some
image from the first day, and wrong on more of them with every OpenWebif release.

The receiver publishes no picons over MQTT, and should not: a few hundred kilobytes for each
channel on retained topics is not what a broker is for.

OpenWebif answers the question in two ways, and they do not cost the receiver the same.
`getpicon` looks for one service's picon. `getservices` with `picon=1` looks for the picon of
every service of a bouquet in one request, inside the process that draws the receiver's own
menus - thousands of file checks for a large bouquet.

## Decision

**1. The user chooses, and the default stays the screenshot.** The option `player_picture` has two
values. `screenshot` is the default, so an existing installation keeps the picture it has.
`picon` shows the picon of the current channel, and the screenshot only for a channel without
one.

**2. In the `screenshot` setting the picon stands in after a channel change.** From a channel
change until a screenshot taken after it arrives, the picon of the new channel is shown. Whether
a screenshot was taken after the change is decided from its bytes: the `screen` topic is retained,
and a reconnect to the broker delivers the same picture again with a new arrival time. If no new
screenshot arrives, the picon stays. The channel a session starts on is not a change, so after a
restart the screenshot is shown and the receiver is asked nothing.

**3. Standby keeps the last picture.** It is not blanked or swapped when the receiver goes to
standby or its `service` payload empties, and a receiver that wakes keeps it until it has named
the channel it is on.

**4. OpenWebif is asked which picon a channel has, and the player asks about one channel.** For
the player: `GET /api/getpicon?sRef=<service>&json=true`, then the picture. A bouquet's answer
that is already held is used instead. For the media browser: `GET
/api/getservices?sRef=<bouquet>&picon=1`, when that bouquet is opened, in the background - the
browser lists the channels at once, with the thumbnails it always had, and has OpenWebif's links
the next time. The player never lists a bouquet. The file name built from the service reference
is used only when OpenWebif gives no answer of its own; an OpenWebif without `getpicon` that has
served a picon under that name is then asked for that name alone. OpenWebif's default picon means
"none". Both OpenWebif families - E2OpenPlugins and oe-alliance - answer with the same keys.

**5. Home Assistant fetches the picon and serves it.** The player's picture is delivered as bytes
through Home Assistant, as the screenshot is, so it works wherever Home Assistant does and the
browser needs no route to the receiver. The media browser's thumbnails stay addresses on the
receiver, with the link OpenWebif gave.

**6. The address asked is the typed one, and a reported one only within limits.** The host typed
when the receiver was added is asked whenever there is one, and is not filtered: it is the
administrator's choice. A typed value that cannot be the host of a web address - an IPv6 address
with a zone, which only SSH can use - counts as none. Without one, the address the receiver reports on `info` is asked only when
it is an IP address written out - never a host name, which this machine would resolve - and not
loopback, link-local, unspecified, multicast or reserved. Otherwise there is no address and no
picon. The reported address arrives over the broker, where anything that may publish can have
written it. The link on the device page is not changed by this: a person clicks that.

**7. What the receiver sends is not trusted.** No redirect is followed. A link is used only when
it is a plain path under `/picon/`. An answer over 2 MiB and a picture over 1 MiB are dropped. A
picture is accepted by its first bytes, PNG or JPEG, never by its content type: Home Assistant
serves it from its own origin, where an SVG or an HTML page would be a script. A status other
than 200 is never a picture.

**8. The receiver is asked as little as possible.** Nothing is polled. One task of the config
entry does all the asking, one request at a time; unloading the entry cancels it, and nothing
that was queued is asked afterwards. A channel change abandons the lookup for the channel before
it. Answers are kept: pictures for a day, "no picon" for an hour, a bouquet's links until its
channel list changes or a day, and a bouquet whose request failed is not asked about again for an
hour. After a connection failure nothing is asked for 5 minutes; after a 401 or 403, or an
interface that answers but not as OpenWebif does, for 15. In the `picon` setting a failed lookup
is tried once more when that time is up, and a second failure waits for the receiver's next
message. Every failure shows the other picture and is logged once per cause until it recovers.

## Consequences

- OpenWebif has to be reachable from Home Assistant without a login, over HTTP on port 80.
  Behind a login, on HTTPS only or on another port there are no picons, and the screenshot is
  shown. Storing OpenWebif credentials was not taken on for a picture.
- **What the receiver is asked.** A channel change to a channel not seen in the last day: at most
  one `getpicon` and one picture. A channel seen before, or one whose bouquet the media browser
  has listed: nothing, or the picture alone. Start-up in the `screenshot` setting: nothing; in
  the `picon` setting: the current channel, as above. The whole-bouquet request is made only when
  a bouquet is opened in the media browser: at most one per opening, none while its links are
  held - until its channel list changes or a day passes - and not again for an hour when it
  failed.
- The player does not wait behind the media browser: a lookup for the channel that is on
  interrupts a bouquet's list that is being fetched. The interrupted list is dropped, not started
  again, so a bouquet's list is started at most once each time the bouquet is opened, whatever
  the zapping. The price: thumbnails for a bouquet whose list a channel change interrupted -
  picking a channel in the browser while it lists is enough - arrive the next time the bouquet
  is opened. Bouquets that were waiting behind it are still listed, once.
- **A broker client can still choose an address on the network when no host was typed.** It
  cannot make Home Assistant ask itself, a link-local address or a name. It can name another
  private address. Home Assistant then sends that host the current channel's service reference -
  an IPTV reference contains the stream's address - and shows a PNG or JPEG of at most 1 MiB from
  it on the card. Typing the receiver's host closes this, and SECURITY.md says so.
- **A screenshot does not say which channel it shows.** Any screenshot that differs from the one
  held at the channel change counts as taken after it. The common case: the on-zap screenshot
  of a channel that was only zapped through arrives while the next channel is on and replaces
  that channel's picon, until that channel's own screenshot follows. A screenshot already being
  taken at the moment of the change does the same. No rule on this side can tell them apart; a
  later plugin release could say which channel a screenshot shows, and this record would then be
  amended.
- In the `picon` setting the previous channel's picon stays under the new title for the moment
  the lookup of a channel seen for the first time takes.
- A picon added or replaced on the receiver shows after the hour or the day above, or when the
  integration is reloaded.
- An image whose picon resolver returns a file outside the directory OpenWebif serves gives a link
  that is not under `/picon/`. It is not used, and the name built from the reference is tried.
- The picture in the `screenshot` setting can now be a PNG as well as a JPEG.
- If the plugin ever publishes which picon a channel has, or the picon itself, this record is
  superseded for the lookup; the choice and the order of the pictures stay.

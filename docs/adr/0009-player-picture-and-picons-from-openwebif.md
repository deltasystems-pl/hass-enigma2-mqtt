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
standby or its `service` payload empties.

**4. OpenWebif is asked which picon a channel has.** `GET /api/getservices?sRef=<bouquet>&picon=1`
gives the link for every service of a bouquet, and is asked once per bouquet on offer, for the
player and for the media browser. `GET /api/getpicon?sRef=<service>&json=true` answers for the
current channel when no bouquet on offer lists it. The file name built from the service reference
is used only when OpenWebif gives no answer of its own. OpenWebif's default picon means "none".
Both OpenWebif families - E2OpenPlugins and oe-alliance - answer with the same keys.

**5. Home Assistant fetches the picon and serves it.** The player's picture is delivered as bytes
through Home Assistant, as the screenshot is, so it works wherever Home Assistant does and the
browser needs no route to the receiver. The media browser's thumbnails stay addresses on the
receiver, with the link OpenWebif gave.

**6. What the receiver sends is not trusted.** Requests go to the receiver's own address and
follow no redirect. A link is used only when it is a plain path under `/picon/`. A service list
over 2 MiB and a picture over 1 MiB are dropped. A picture is accepted by its first bytes, PNG or
JPEG, never by its content type: Home Assistant serves it from its own origin, where an SVG or an
HTML page would be a script.

**7. The receiver is asked as little as possible.** Nothing is polled. A lookup runs when the
player needs a picon it does not hold and when a bouquet is opened in the media browser, as a
background task of the config entry that unloading cancels, one request at a time. Answers are
kept: a bouquet's links until its channel list changes, pictures for a day, "no picon" for an
hour. After a connection failure nothing is asked for 5 minutes, after a 401 or 403 for 15. Every
failure shows the other picture and is logged once per cause until it recovers.

## Consequences

- OpenWebif has to be reachable from Home Assistant without a login, over HTTP on port 80 of the
  address the receiver reports. Behind a login, on HTTPS only or on another port there are no
  picons, and the screenshot is shown. Storing OpenWebif credentials was not taken on for a
  picture.
- `getservices` with `picon=1` makes the receiver look for a picon for every service of the
  bouquet in one request. It is asked once per bouquet and kept for a day, not per channel
  change; a receiver with very large bouquets pays that once.
- A picon added or replaced on the receiver shows after the hour or the day above, or when the
  integration is reloaded.
- An image whose picon resolver returns a file outside the directory OpenWebif serves gives a link
  that is not under `/picon/`. It is not used, and the name built from the reference is tried.
- A screenshot published just after a channel change that still shows the previous channel - one
  already being taken when the channel changed - counts as new. Nothing in the picture says which
  channel it shows.
- The picture in the `screenshot` setting can now be a PNG as well as a JPEG.
- If the plugin ever publishes which picon a channel has, or the picon itself, this record is
  superseded for the lookup; the choice and the order of the pictures stay.

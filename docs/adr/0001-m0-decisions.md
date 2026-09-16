# ADR-0001: M0 sign-off — the three open questions

**Status:** accepted
**Date:** 2026-09-16
**Supersedes:** the proposed answers in [ADR-0000](0000-prd.md) §12

## Context

The product requirements document was approved at milestone M0 with three questions left
open in its §12, each carrying a proposal rather than a decision. They are closed here, and
where the answer differs from the proposal this ADR wins.

## Decision

### 1. A compact EPG grid over MQTT is implemented in v1

The PRD proposed *no* — EPG is pull by nature and belongs to OpenWebif. That is reversed: the
operator's own EPG reducer exists precisely because a household screen wants "what is on next
on the channels we watch" without a web request per channel, and every other consumer of this
plugin will want the same. A grid narrow enough to fit an MQTT payload is not the same thing
as EPG browsing.

Scope, deliberately small:

- The plugin publishes the grid for the **configured bouquets** only, with the **next N
  events per channel** — plugin setting `epg_grid_events`, default **4**, `0` switches the
  feature off entirely.
- Retained JSON on `enigma2/<node_id>/epg_grid`:
  `{bouquet, generated, channels: [{sref, name, events: [{title, begin, end, event_id}]}]}`.
- Refreshed when the bouquet selection changes, every **15 minutes**, and on demand via
  `cmd/epg_grid`.
- **Full EPG search and timer browsing stay on OpenWebif.** This is a grid, not an EPG API.

The plugin side lands in **M2**. This integration consumes it in **M3 through an action that
returns a response** — never as a state attribute: an 80 KB attribute would be written to the
recorder on every update and would bloat the database and every state-changed event.

### 2. Both documents stay in the wiki's `integrations` book

No separate book on the *Applications* shelf. The operator's wiki taxonomy gives a developed
application its own book, but this product is consumed by the lab as an integration and its
two documents — the design note and this PRD — are read alongside the other Home Assistant
integration pages. A second location would split the reader's search for no gain.

### 3. Telnet-only boxes: documented, not implemented in v1

Some images ship with SSH off and only telnet enabled. Version 1 **documents "enable SSH
first"** as a prerequisite of the guided installer; the installer itself speaks SSH only.

A telnet transport is a **v1.1** item, tracked as an issue once M4 exists and the installer's
transport is a real interface to implement twice. Telnet carries the root password in clear
text, so even in v1.1 it will be an explicit, warned-about choice.

## Consequences

- The plugin gains one setting, one retained topic and one command; the topic contract in the
  plugin's `docs/TOPICS.md` must document `epg_grid` from M2.
- This integration gains an action with a response in M3, and its documentation must state
  plainly that the grid is not available as a sensor attribute.
- The `epg_grid` payload is the largest retained message the plugin publishes; its size is
  bounded by the bouquet selection and by `epg_grid_events`, and both are the user's choice.
- The installer's preflight must detect "SSH unavailable" and say what to do about it rather
  than failing with a connection error.
- The lab's wiki pages keep their current location; no taxonomy change is needed.

## Amendment 2026-09-16

Decision 1 above put the EPG grid on one retained topic. Building it showed that one topic
for every bouquet makes each consumer re-read every bouquet whenever any one of them moves
on, so the grid is published as **one retained topic per configured bouquet**:
`enigma2/<node_id>/epg_grid/<bouquet_slug>`. The slug is the bouquet's name lower-cased,
transliterated to ASCII, with every run of non-alphanumeric characters collapsed to a single
`_` and leading and trailing `_` trimmed — an addressable name, nothing more. The payload is
unchanged and its `bouquet` field carries the **original** name, which is what a user is
shown. `cmd/epg_grid` still regenerates every configured bouquet at once, and the plugin
retracts the slugs it published for bouquets that are no longer configured, the same way it
retracts a discovery component it no longer announces. A consumer that wants one bouquet
subscribes to one topic; one that wants them all subscribes to `epg_grid/+`. The plugin
repository carries the same amendment, and its `docs/TOPICS.md` is the normative version.

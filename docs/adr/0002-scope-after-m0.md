# ADR-0002: Scope added and changed after M0

**Status:** accepted; §6 superseded in part by [ADR-0008](0008-signed-plugin-index.md) (accepted)
**Date:** 2026-09-21

## Context

[ADR-0000](0000-prd.md) is the product requirements document as it was approved at M0. It stays as
it was written - a record whose value is that it was not edited afterwards - but the integration
that now runs on a Home Assistant instance does several things it does not mention. Some came from
the one person with a test receiver asking for them; some came out of reviews, and two came out of
things that broke.

This record names them, says why each exists, and says what each costs in safety and in privacy.
The entity and action surface is documented in [DOCUMENTATION.md](../../DOCUMENTATION.md) and the
topic contract lives with the [plugin](https://github.com/deltasystems-pl/enigma2-mqtt-bridge); this
is why, not what.

It also says what is **not** done, so the gap between the README and the behaviour is written down
rather than discovered.

## Decision

### 1. Bouquet activation - the `select_bouquet` action, and a bouquet as playable media

Browsing a bouquet here was never changing the receiver's own channel list. The box has one active
television bouquet, and it is what its channel-up and channel-down actions walk; picking another
one in Home Assistant left that untouched, so the next CH+ on the physical remote carried on
through the previous list.

- `enigma2_mqtt.select_bouquet` takes a bouquet's service reference and publishes `cmd/bouquet`.
  Selecting a bouquet in the **media browser** does the same thing, because a bouquet is a playable
  item rather than a folder that only contains playable ones.
- The receiver **keeps the current channel when it belongs to the chosen bouquet**, and otherwise
  tunes that bouquet's first playable channel. Channel ± then walks the chosen list.
- The action waits for the receiver's fresh `bouquet` topic - the plugin republishes it on every
  success, including when the bouquet was already active - so the action reports what happened
  rather than that a message was sent. A refusal is raised as an error carrying the receiver's own
  sentence.

### 2. Conditional-access diagnostics, created only while they are switched on

A receiver whose card stops decrypting looks exactly like one that is working: same channel, same
programme, black picture. There was no entity for it.

- With **CAM telemetry** on, the integration exposes the current service's system, its encrypted
  and active flags and the ECM time. With **OSCam telemetry** on, it exposes twelve aggregate
  entities - software and API status, API access, uptime, configured, enabled and healthy readers,
  cards ready, servers connected, shared cards - plus a dynamic set **per source**, reader or
  server, under the stable opaque id the receiver publishes.
- **These entities exist only while the option is on.** Switching the option off removes them;
  nothing is created speculatively for a receiver that publishes nothing.
- Reader labels, addresses, accounts, card identifiers, CAIDs and WebIf credentials **never enter
  integration state or the diagnostics download** - they never leave the receiver in the first
  place, and the integration does not reconstruct them.
- An API outage keeps existing entities `unavailable` until a complete snapshot arrives, rather
  than publishing a partial one. Old health presented as current is worse than no health.
- Shared-card counts are kept **separate** from physical reader counts. They are a server's own
  claim about what it offers, and adding the two would produce a number that means nothing.

### 3. The options page writes to the receiver, over `cmd/config`

An options page that changes only what Home Assistant displays is half an options page; the things
a household wants to change - whether the screen is photographed, whether key presses are published
- live on the receiver.

- The receiver-side options (`publish_keys`, `screenshot`, `screenshot_interval`,
  `screenshot_delay`, `cam_telemetry`, `oscam_telemetry`) are published on `cmd/config`, and
  **Home Assistant waits for the receiver's fresh `info.settings` before saving them**. An options
  page that stores a preference the box never accepted is a lie the user will believe.
- The rest of the options are Home Assistant's own: whether the deep-standby and reboot buttons
  appear, the Wake-on-LAN address, which bouquets are worth browsing, and the receiver's optional
  host address.

🔴 **The consequence is that the broker login is the privacy boundary.** This flow is the reason
`cmd/config` is writable at all, so anything else that can publish there can switch the same
privacy options on - and then ask for a picture of the television. Give the receiver its own broker
credential and restrict it with an ACL; the recipe is in the README. It is stated here because it
is a property of this design, not an accident of the plugin's.

### 4. Post-zap screenshot delay, as an option

An on-zap capture taken the moment the receiver reports a tune is frequently a picture of the
previous channel. The receiver now waits a configurable number of seconds - four by default, one to
thirty - and the option is here because the right value depends on the tuner and the transponder,
which is exactly the kind of thing the person with the box knows and the software does not.

### 5. Installer hardening beyond the PRD

ADR-0000 §6.2 describes an installer with a preflight and a rollback. Writing one that can be
trusted with somebody else's receiver took more than that.

- **The SSH host key is shown and pinned before any credential is sent.** An installer that asks
  for a root password and then decides what it connected to has already handed the password over.
  There is a reauth flow, and a distinct host-key-change flow, because those are different events
  and only one of them is routine.
- **A durable lock on the receiver**, with boot-id and uptime staleness detection, so a lock left
  behind by a box that has since rebooted does not block every later attempt.
- **A rollback that does not depend on OpenWebif.** The web interface is one of the things a failed
  install can take down, so a recovery path that needs it is a recovery path that is absent when it
  matters.
- **A private pre-change backup on the receiver**, verification of the uploaded bytes, and a
  provisioning file written **0600 under a nonce** - it carries a broker password until the plugin
  imports and deletes it.
- **Credentials are opt-in and reversible.** The SSH password is discarded when the install
  finishes unless it is explicitly kept for updates, and it can be enrolled, refreshed or forgotten
  later without unloading MQTT. It is never written to the log or to diagnostics.
- **An optional receiver host** in manual setup and reconfigure. It is metadata for the device link
  and a suggestion for SSH - not a requirement, so a receiver reachable only over a bridged broker
  can still be added.

What this does **not** claim: a loss of receiver power or storage in the middle of the transaction
can still require recovery from that backup. That is said in the README too.

### 6. A reproducible bundle, not a download

The integration ships the receiver plugin's IPK **together with its exact corresponding GPL source
archive** and provenance metadata pinning the commit, the sizes and the SHA-256 values. CI
**rebuilds the IPK byte for byte** from the named plugin commit rather than trusting an opaque
artifact.

There is **no runtime download path for executable code**, and there is no release check that
phones home. An integration that installs software on a device the user cannot easily inspect
should be inspectable itself; „it works" is not the standard.

> **Superseded in part by [ADR-0008](0008-signed-plugin-index.md) (accepted 2026-09-26):** the
> bundle stays, CI still rebuilds it byte for byte, and it is still the only thing the forced
> reinstall installs. What ADR-0008 changes is the first sentence above: the integration is to
> download a plugin package at runtime - from the plugin's fixed origin only, only a version its
> signed release index lists, verified by signature, size and sha256 before it is uploaded or
> relayed to a receiver. The second half, "no release check that phones home", stopped being true
> in 0.2.0, which added the opt-in release check (off by default) that ADR-0000 §6.3 described and
> that this section and "Not yet done" below said did not exist; it asks the plugin repository which
> release is published. Nothing recorded that at the time, and ADR-0008 does. Until ADR-0008 is accepted and
> shipped, the first half describes every released integration exactly.

### 7. Duration and position on the media player

`media_duration` and `media_position` come from the programme's own begin and end, so a card shows
how far through the programme is. It is the one thing a media player is expected to have that the
receiver's topics did not obviously provide.

### 8. Backups of this integration must live outside `custom_components/`

Home Assistant maps a domain to a directory by reading **every** `manifest.json` under
`custom_components/`. A copy of this integration kept beside the live one - `enigma2_mqtt.bak-...`,
or anything else - therefore declares the same domain twice, and **which copy wins is not
predictable from its name or its date**. When the backup won, its directory name was not an
importable module and setup failed with every entity unavailable.

Five such directories sat harmlessly for days before the sixth did it. The rule is therefore
absolute rather than careful: **a backup of a custom component leaves `custom_components/`
entirely.** This is the same shape as two repositories declaring one domain, and it has the same
answer - one directory per domain, no exceptions for copies.

### 9. Waiting for the broker, not for the subscribe call

Home Assistant's MQTT client batches SUBSCRIBE packets behind a 0.1 s debouncer, so
`async_subscribe()` returns before the broker has the subscription. A receiver on the same network
answers a command in about the same time - so an action that subscribed and published immediately
raced the subscription that was meant to hear the answer, and the only copy it saw was the retained
replay, which correctly does not count as an acknowledgement. A perfectly healthy receiver produced
a permanent „no acknowledgement".

Every publish-then-await path now waits for the subscription to be confirmed by the broker before
it publishes.

## Consequences

- The action surface is ten actions rather than eight: `select_bouquet` and `get_epg_grid` join the
  PRD's list. `get_epg_grid` returns a **response** and never a state attribute, because a grid is
  tens of kilobytes and an attribute is rewritten into the recorder on every update.
- Two groups of entities now appear and disappear with an option. Anything that keys on „the
  integration's entities" has to tolerate a set that changes shape, which is the price of not
  creating diagnostics nobody asked for.
- The installer's guarantees are now specific enough to be tested and specific enough to be wrong
  in public. That is the intent: an installer whose failure modes are vague has none that can be
  fixed.
- Bundling the plugin ties a release of this integration to a build of that one. The compatibility
  table in both READMEs is how that is stated, and the `update` entity is how a user finds out.

## Not yet done

Stated so that the README and the behaviour do not drift apart again:

- **There is no 0.2.0 release.** Everything above is on `main` and running on the maintainer's
  Home Assistant; HACS still serves **0.1.0**. A coordinated bump with the plugin comes first -
  until then both report `0.1.0`, and version equality cannot tell two development builds apart.
- **The guided installer has never been run end to end**, and neither has a real rollback. The code
  is reviewed and covered by tests against a fake SSH server, including its refusals and the races
  its lock can lose. That is not the same as installing onto a receiver that has never had the
  plugin, and it is not claimed to be.
- **The broker-credential help text and the ACL snippet are in the README but not in the setup
  form**, which is where somebody typing a broker password is actually looking. ADR-0000 §6.2 asked
  for them there.
- **The release workflow does not re-run hassfest and the HACS action** on the tagged tree. They
  run on every push and pull request, so a tag cut from a green `main` is fine in practice - but
  „in practice" is not a gate.
- **There is no opt-in release check** on the `update` entity. ADR-0000 offered one, off by
  default. Not having it is the safe direction, and it means the entity compares the receiver
  against the bundled build and nothing else.
- **Test coverage is 93 %, against the 95 % this project set itself** in
  [QUALITY.md](../QUALITY.md). What is uncovered is concentrated in the asyncssh transport, which
  has no receiver to talk to in CI.

## Proposed

The receiver plugin's [ADR-0002](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0002-scope-after-m0.md)
proposes publishing the receiver process's memory, thread count and uptime as diagnostics, so that
a consumer can record the curve. If that lands, the entities belong here and would follow the same
rule as the telemetry above: off by default, created only while switched on.

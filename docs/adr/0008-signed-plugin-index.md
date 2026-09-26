# ADR-0008: Plugin versions come from the plugin's signed release index; the update card installs through the receiver when it may and over SSH when it must; older versions only through a confirmed SSH step

**Status:** accepted 2026-09-26, with the first code that implements it: the signed index
reader, the compatibility rule, the build ids, the two new entities and the no-badge rule
(§1-§3), the integration topic and the two messages at removal (§2, §10). The install paths of
§4-§6 and §8-§9 are built in later changes; until each ships, the released integration behaves
as the records it supersedes describe
**Date:** 2026-09-26
**Supersedes:** in part, once accepted - [ADR-0002](0002-scope-after-m0.md) §6, "There is no
runtime download path for executable code, and there is no release check that phones home" (its
second half stopped being true in 0.2.0, whose opt-in release check nothing superseded at the time;
this record says so); [ADR-0000](0000-prd.md) §7's "no outbound connection other than the user's
broker" and its supply-chain bullet, and §6.3's `update` row, whose latest version is the higher of
the installed and the bundled one; [ADR-0004](0004-remote-uninstall.md)
§4's "does nothing else" and [ADR-0006](0006-remote-uninstall-plugin-acts-ssh-verifies.md) §5's
"one publish" - removing an entry publishes two messages; and the "No telemetry" and "Supply chain"
paragraphs of [SECURITY.md](../../SECURITY.md). Each of them describes every released integration
exactly until the first release that implements this record ships; each carries a marker saying
where it stops doing so, and SECURITY.md's policy is rewritten in that release.

The receiver's half is [ADR-0015](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0015-signed-self-update.md)
in the plugin repository. Three documents there are shared ground and are not repeated here: the
contract version and its rule, in [TOPICS.md, "Contract version"](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/TOPICS.md#contract-version),
with the same contract as data in [contract.json](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/contract.json);
and the install transaction both programs implement - the names on the receiver, the lock and its
stale rule, the snapshot, the marker and the restart rule - in
[TRANSACTION.md](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/TRANSACTION.md).
The records are read together; this one says only what Home Assistant decides.

## Context

The update entity reports `info.plugin` as installed and, as the latest version, the higher of the
installed and the bundled plugin (`update.py` l.155-166 at 0.3.1), and `install` runs the SSH
installer with the bundled package when the installer's credentials were kept. It compares version
strings, so a development build and the release with the same number look the same. A receiver on
an older plugin without stored SSH credentials is shown **an available update with no install
button**: the install feature follows the credentials (l.424-429), the latest version does not, and
the summary says how to get there by hand. The opt-in release check added in 0.2.0 asks the plugin
repository which release is published, and deliberately never raises the offered version above the
bundle, because nothing could install it.

The plugin repository now defines what a compatible plugin is - a contract major, and a rule for
what a release may change inside one - and proposes that the plugin update itself, from a release
index signed with Ed25519. The **main key** is used only in the plugin repository's CI, by a signing
job that runs in the GitHub Environment `release-signing`, which admits the `main` branch and nothing
else and releases the key only to a run the maintainer approves by hand; release tags `v*` are
protected by a tag ruleset. A **spare key of higher rank** stays sealed offline and signs nothing
unless the main key is leaked or lost; its emergency publication is a separate workflow that holds
no secret. Some receivers have no internet access and are reached only through the household's LAN;
the Home Assistant that manages them usually does have it.

Every restart the released installer causes stops the receiver's interface with `init 4` and starts
it with `init 3`: the install under one trap (`installer.py` l.1452 at 0.3.1), the rollback as two
separate SSH commands (l.1136 and l.1171). The image saves its settings - the channel being watched among
them - only on a clean quit, and a signal stop never reaches that code; on a receiver, measured, the
interface came back on another channel than the one playing. OpenWebif's own restart (power state 3)
is the image's clean quit.

Two facts about Home Assistant shape the recovery button below: `update.install` is for
administrators only and `button.press` is not; and the options flow reloads the entry only when its
options change, so storing or forgetting SSH credentials - which live in the entry's data - reloads
nothing.

## Decision

### 1. The signed index is the only list of versions

Versions, their sizes, sha256 values, commits, compatibility, dependencies, the floor and the
withdrawals come from the plugin's release index and nowhere else. The integration fetches it from
the plugin's fixed HTTPS origin with verified TLS and no redirects, and accepts it only when the
Ed25519 signature verifies with a public key embedded in this integration, and when its trust rule
holds: the serial increases for that key and by at most 1000 (a key seen for the first time within
1000 of a baseline embedded at build time), and the key's rank is not below the highest rank already
accepted. Two keys are embedded, a main key and a sealed spare of higher rank; the key set and ranks
equal the plugin's. The trust memory is kept per embedded key set, by its fingerprint, so a build
carrying other keys can never raise the rank the release keys are judged by.

Every newly accepted index is announced: a warning in the log and a persistent notification naming
its serial, its `key_id`, the versions it adds and withdraws, and its floor - so an index the
maintainer did not approve is seen by anybody who looks, not only installed.

An explicit check, an install or a downgrade is consent to fetch. The daily check stays opt-in, on
the existing option, and manual checks share a ten-minute limit. **Both limits live in Home
Assistant's storage**, not in the entity: one verified index cache for all receivers - the index, its
signature, its serial, its ETag and when it was fetched - which the daily stamp and the ten-minute
limit are judged from. A limit kept in the entity is reset by every reload, options save and restart,
which is why the release check's store exists today; the index cache takes its place
(`.storage/enigma2_mqtt.release_index`), and the per-receiver records of the old release check
(`.storage/enigma2_mqtt.release_check`) are removed the first time it loads. GitHub's API is
used only for one digest cross-check per install - a mismatch refuses, an unreachable API is noted
and does not - and its answer is kept per version for a day.

### 2. Compatible means the same contract major

A release is offered when its contract major is this integration's (`1`), its version is at or
above both this integration's floor (`0.2.0`, below which lies contract 0) and the index's floor,
this integration is at or above the release's own `min_integration`, it is not withdrawn, and the
receiver has every package it depends on - which the receiver checks and refuses. There is no upper
bound inside a major: the plugin's rule lets a release add topics, members, commands, capabilities
and settings, add a value to an enumeration, add a refusal and tighten free text, and never remove,
retype or change a meaning - except as a named in-major exception, which the plugin decides in the
pull request that makes it, after checking it against this integration's code and citing it by file
and line. Four are named so far: `zap-moves-channel-list` and `epg-grid-generated-means-changed` (0.3.0),
`timers-lists-finished` and `zap-under-popup-recorded` (not yet released).

This integration's half of the rule: ignore what it does not know, treat a value of an enumeration
it does not know as unknown, and read a member it expects and does not find as the older plugin it
is. A plugin that publishes no `info.contract` is contract 0 below 0.2.0 and contract 1 for 0.2.0 and
0.3.x. The major and the floor are declared in `const.py` - the manifest cannot carry them - and
published, retained, on `enigma2mqtt/integration/<node_id>` so a receiver applies the same rule;
`SUPPORTED_PLUGIN_VERSION` stays what it is, the version shown when no bundle loads. The rule is tested
against the plugin's own vectors: a byte-identical copy of the plugin's
`tests/vectors/release-index.json`, pinned to the plugin commit `tests/vectors/SOURCE.json` names
and compared with the plugin repository in CI together with the embedded keys and the list of
named exceptions (`tools/check-plugin-shared.py`) - the bundled source archive is a release's, and
0.3.0's predates the vectors.

### 3. The card offers what it will install, and only when it can

`latest_version` is the version this card will install - the newest compatible release, or the one
chosen in a select - while an install path exists. **Without one there is no update badge**:
`latest_version` equals the installed version, so the entity's state is `off` - not `None`, which
would make it `unknown` - and the newer versions, the bundled one included, go into the summary and
the attributes with the way to an install path. That is a **behaviour change** from 0.3.1, where a
receiver behind the bundle without credentials shows an available update with no install button;
it is called out in the changelog of the release that makes it. A release build displays `N.N.N`; any other
build, installed or offered, displays `N.N.N+g<sha7>`, with `.dirty` when its tree was not clean.

**The card compares release numbers, and nothing else** (decided by the maintainer on
2026-09-26, replacing this record's earlier "two builds of the same `N.N.N` compare by commit
time"). A higher number badges as usual. The same number never badges, in either direction: a
development build installed over the release of its number, and a development candidate bundled
over an installed release of the same number, are each possibly older code than the other, and a
badge would make a downgrade one press away. A development build is still never shown as
current: its display says what it is, and the summary and the attributes say "development build;
release N.N.N available" (in Polish „Wersja rozwojowa; dostępne wydanie N.N.N."). A same-number
build is installed only by an explicit choice - picked in the version select, which then turns
the state on so that the card's own button installs it, or named in `update.install`. The commit
time stays in the attributes as information and orders nothing.

The rule of section 2 holds for the bundle too: a bundled plugin below the floor, or withdrawn in
the last verified index, is never offered and never installed, and the summary says why. With no
verified index yet, only this integration's own floor applies.

| Key | Platform | en | pl | de | Enabled by default |
|---|---|---|---|---|---|
| `check_plugin_update` | button | Check for plugin updates | Sprawdź aktualizacje wtyczki | Nach Plugin-Updates suchen | yes |
| `plugin_install_version` | select | Plugin version to install | Wersja wtyczki do instalacji | Zu installierende Plugin-Version | no |
| `force_reinstall` | button | Force plugin reinstall (SSH) | Wymuś reinstalację wtyczki (SSH) | Plugin-Neuinstallation erzwingen (SSH) | no |

All three are diagnostic. Their names are chosen once, in every language
([ADR-0007](0007-entity-ids-follow-the-installation-language.md)); no existing name changes. The
select's first option is `latest` ("Latest compatible"), resolved to the newest compatible release
when the card installs; it is also what counts while the select is disabled. Any other choice is
stored in the entry's options, honoured only while the select is enabled, and offers only versions
above the installed one.

### 4. Through the receiver when it may, over SSH when it must - never both

| The receiver reports | SSH credentials | Upgrade | Downgrade |
|---|---|---|---|
| `self_update`, and `update_allowed` on | any | over MQTT, `cmd/update` with a relay address | options flow, SSH |
| otherwise | stored | the SSH installer, with the verified package | options flow, SSH |
| otherwise | none | none | none |

After a refusal or a rollback over MQTT the card says so and does not try SSH. Over MQTT the
command is published once the broker has confirmed the subscription that hears the answer, and the
transaction is followed for up to 21 minutes - one more than the receiver's own hard limit - after
which the card says it is still running on the receiver and keeps following. Success is `installed`
together with the target's version and commit on `info`. A transaction the receiver started shows
as in progress only while it started within the last 21 minutes, so a stale or forged retained
state cannot hold the card for longer.

### 5. A receiver without internet gets both from Home Assistant, and verifies both itself

After every newly accepted index the integration publishes it, retained, on
`enigma2mqtt/release_index`. To a receiver that asks, it serves the package, already verified, at
`/api/enigma2_mqtt/relay/<token>`: unauthenticated, 32 random bytes, answering only the receiver's
own IPv4 address from `info` (no address, or only an IPv6 one, and no token is issued), for ten
minutes, repeatably - the address crosses the broker, so single use would only let a broker client
spend it first, and the bytes are a public signed package anyway. One token per receiver and
version, three per receiver at most, and never one a known transaction is using. The address is
Home Assistant's own on the receiver's subnet, else its internal URL, never an external or cloud
one; with neither, the relay is refused and the text says where to set the local address. The
receiver checks the index and the package against the signature itself: Home Assistant is a
courier, not an authority.

### 6. Older versions only through a confirmed SSH step

A downgrade is never sent over MQTT. The options flow offers the versions from the floor up to below
the installed one, names what disappears - the entities and actions whose capability the target
predates, and updating over MQTT when the target cannot - and needs a tick box. It installs with
`--force-downgrade`, restarts by the rule below, and ends with the target's own `cmd/reset` after
the proof, so the older plugin forgets the retained topics only newer ones publish. Never below the
floor, never a withdrawn version.

### 7. Every restart the installer causes keeps the household's channel - from 0.4.0

The installer follows the restart rule of
[TRANSACTION.md §5](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/TRANSACTION.md#5-the-restart-rule-planned-for-040-both-programs):

- **Restart (R1)** wherever only the plugin's files changed: the preflight refuses while recording,
  streaming, in standby or with a timer due within ten minutes, and checks every package the release
  depends on; then OpenWebif's power state 3 on the receiver itself, judged by the enigma2 pid
  changing within 60 seconds, never by the call's exit status. No new pid means the image asked a
  question on the television. It is not forced: the plugin's files and package metadata go back by
  staged renames (never the settings), the pid is read again, and the result says that the update
  was withdrawn, that the previous plugin is running, and that the question may still be on the
  television, where either answer is safe.
- **Stop (R2)** only where the interface must not run - a rollback that puts the settings back, a
  forced reinstall into an interface that keeps crashing: the playing service and standby state are
  recorded, then the stop, the restore, the service written back into the settings and the start
  run on the receiver as one detached script whose trap starts the interface on any interruption.
- **Verify (R3)** after every restart on every path: the service and standby state are compared
  with the record and restored if they differ - the channel at most once, and only while the
  receiver is still on the service it started on - and the recorded bouquet is restored with
  `cmd/bouquet` where the plugin has it.

The lock's owner record carries the installer's transaction id, so the recovery of an interrupted
transaction finds its snapshot by id, never by a clock that may have started in 1970. Two
assumptions under R2 - that `init 4` loses unsaved settings, and that the image comes back on a
channel written while it is stopped - are not yet measured on a receiver, and are measured before
the code that relies on them merges.

**The released 0.3.x installer is not changed.** It keeps `init 4` / `init 3` on every path, so an
update through it can bring a receiver back on another channel than the one it was playing.

### 8. A recovery path that needs nothing from the plugin

"Force plugin reinstall (SSH)" reinstalls the bundled package - only the bundled package - over SSH,
with every installer safeguard, the shared lock and the restart rule, even over a newer plugin
(with `--force-downgrade`, said in the confirmation), and refuses a bundle the index has withdrawn.
Its proof does not need the plugin to answer: a new enigma2 pid holding the plugin's log file open,
plus the announcement and the bundle's version and commit when the plugin is switched on; a plugin
switched off in its own settings is reinstalled and stays off, and the result says so.

- **Two presses, one administrator.** The first press arms it for 30 seconds and posts the
  confirmation as a notice, never an error, so an automation is not aborted; a second press within
  30 seconds by the same Home Assistant administrator starts it. A press by anybody else arms
  nothing, starts nothing, raises nothing, and posts a notice saying who may.
- **It reads the receiver, not the topic.** It asks the lock on the receiver, never the retained
  state, so a forged in-progress state cannot block it. It refuses an interface that runs while
  OpenWebif is silent, and one stopped on purpose (runlevel 4 with nothing of ours); it proceeds
  without the recording guards only when enigma2 is absent on three samples over ten seconds - a
  respawn loop, in which nothing can record; and it recovers runlevel 4 left by an interrupted
  transaction of this project.
- **It exists only while SSH credentials are stored.** One helper writes and removes the credentials
  and signals the change; the button is created then, disabled by default, and removed with its
  registry entry when they are forgotten. That is a deliberate exception to "entities are never
  removed when a capability stops being named": that rule protects against silence from a receiver,
  and this follows a decision the user took in Home Assistant.

### 9. The guided installer may grant `update_allowed`, and nothing else

A tick box, off by default, whose help text says that anything able to publish on the broker could
then order an update to a newer version, and that the Home Assistant Mosquitto add-on enforces no
ACL. Only when it is ticked is `update_allowed` written into the provisioning file the installer
already writes over SSH.

The rule of [ADR-0003](0003-control-feedback-and-household-features.md) §8 - a setting that enables a
command is **box-only** - is read the way the plugin defines it: box-only means **set on the
receiver** - its setup screen, its OpenWebif page, or the provisioning file it reads at start - and
never over the broker. On that reading the rule holds: the permission is never on the options form
and never in `cmd/config`, and the receiver refuses it from the broker. This repository has said it
more narrowly until now - "set at the television and refused over MQTT" in `config_flow.py`
(l.858-859 at 0.3.1), "set on the receiver's own setup screen" in DOCUMENTATION.md (l.995-997) - and
both change, to the reading above, with the code that adds the tick box. It is the first permission
this integration writes, and the only one.

### 10. Removing an entry publishes two messages

The retraction of `enigma2mqtt/integration/<node_id>` and the existing `cmd/ha_mode` = `discovery`.
Still no SSH, and still nothing is removed from the receiver. The retained `enigma2mqtt/release_index`
stays: a signed index is harmless to anybody who reads it.

## Consequences

- The integration downloads executable code for a receiver - from one origin, verified by signature,
  size and sha256 - and serves it unauthenticated on the LAN, to one address, for ten minutes, to a
  receiver that verifies it again. The relay address travels over the broker, so a broker client can
  see it and name a host for the receiver to fetch from; the worst it achieves is a refusal.
- This card offers a plugin release only after the maintainer has approved the signing job for it in
  the plugin repository's CI; between the release and that approval, the version is on the opkg feed
  and in the next bundle, but not in the index.
- A deleted environment secret is not a lost key: it is entered again from the main key's encrypted
  offline backup. Only losing both copies moves signing to the spare, and that is irreversible - every
  reader that accepts a spare-signed index ignores the main key from then on - so the spare is never
  exercised in production. A **leak** of the main key - through a malicious workflow, a compromised
  action or the maintainer's account - is treated as a theft: the secret is deleted, the spare signs
  the next index, and the next release of both halves drops the main key, because a receiver or an
  installation reset later still accepts the leaked key until a release no longer embeds it.
- **What CI signing does not protect against.** Because the main key is used in CI, a compromise of
  the maintainer's GitHub account, a malicious workflow change merged to `main`, or a compromised
  action in the signing job can produce a validly signed index - and this integration would then
  offer and install what it names. What stands in the way: the approval gate (the key reaches only a
  run the maintainer approves), the environment that admits `main` only (never another branch, a tag
  or a fork), the tag ruleset on `v*`, the `main` ruleset (a workflow change needs a pull request and
  green CI), enforced pinning of every action to a full commit SHA, a passkey or two-factor
  authentication on the account, the persistent notification for every newly accepted index (§1),
  and the offline spare with a release that drops the main key. Stated plainly: against a compromise
  of the maintainer's account this makes a malicious update **detectable, and recoverable once the
  account - or another channel for a spare-signed index - is under the maintainer's control again;
  it does not prevent it**.
- There is no index expiry: a withheld index cannot be detected, and a withdrawal reaches an
  installation only with a newer index.
- HACS, this integration's own update path, stays unsigned and rooted in GitHub - and this
  integration holds the receiver's root credentials when they are kept. With the main key in CI, the
  signed index now shares that root of trust: the signature adds the checks above, not a second,
  independent root. `SECURITY.md` says so when it is rewritten.
- Three new diagnostic entities, two of them disabled by default; no rename. One of them appears and
  disappears with the stored credentials, and a dashboard card pointing at it goes blank when they
  are forgotten.
- The installer and the plugin's helper share one lock. The released installer's 30-minute stale
  rule binds every later implementation, which may only lengthen it. Only the plugin's tests run
  against the other program's released code - a copy of this integration's released
  `installer_helper.py`; this integration tests the plugin's helper from the bundled source archive,
  which is the candidate it ships, not a release.
- On the SSH path the running plugin has no doors: it runs with the new files on disk until the
  restart, for up to 60 seconds when the image asks a question. Accepted as bounded; the MQTT path
  closes its doors.
- **Two known defects contradict this integration's half of the contract rule** (lines at 0.3.1), and each
  needs an integration release that fixes it before any plugin release adds such a value - the
  plugin's TOPICS.md lists both as not tolerated:
  - an `oscam` reader `kind` other than `reader`, `server` or `unknown` makes `_normalize_oscam`
    return nothing (`box.py` l.1486, the check at l.1510), and the caller then discards the whole
    payload (l.1474-1476): the last good sample stays on the panel and goes stale;
  - a `key` `press` other than `short` or `long` is read as `short` (`box.py` l.1985-1987), so a new
    kind of press would fire the automations and device triggers of a short press.
- The test that deleting an entry makes exactly one publish becomes a test for exactly two.
- If this is reversed, the card goes back to the bundle over SSH, and the integration back to making
  no connection but the broker and, when asked, the receiver.

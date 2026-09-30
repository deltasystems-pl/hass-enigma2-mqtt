# Security policy

## Reporting a vulnerability

Report privately through **GitHub security advisories**:
[*Security -> Report a vulnerability*](https://github.com/deltasystems-pl/hass-enigma2-mqtt/security/advisories/new)
on this repository. Please do not open a public issue for a vulnerability, and please do not
send exploit details to a public discussion.

What to expect:

- **Acknowledgement within 7 days** of the report.
- An assessment, and a fix or a documented mitigation, **before the issue is published**.
- Credit in the advisory and the changelog, unless you prefer otherwise.

Supported: the latest release. There are no long-term support branches.

## Supported versions

| Version | Supported |
|---|---|
| latest release | yes |
| anything older | no |

## What this integration handles

**Broker credentials.** Give each receiver its own broker login, restricted by ACL to its own
topics - [DOCUMENTATION.md §3](DOCUMENTATION.md#3-configuration) has the snippet, and says
why the Home Assistant Mosquitto add-on does not enforce it. The credential lives on the box, and
most Enigma2 images ship with a default root password and an open telnet or SSH service, so treat
the receiver as the least trusted device on the network and change that password.

**SSH password.** The guided installer asks for the receiver's SSH password. It is
**discarded after a successful install** unless you explicitly ask to keep it for later plugin
updates. If you do keep it, it is stored in the config entry - and Home Assistant's `.storage`
is **not encrypted at rest**, so anyone with the configuration directory or a backup of it can
read it. The password is never written to the log, and diagnostics redact both it and the
broker credential.

**Where it connects, and when.** There is no telemetry and no cloud service. Besides your broker,
and your receiver over SSH when you ask it to install, update or remove the plugin, the integration
connects - since 0.4.0, and only for the receiver plugin's releases - to:

- the plugin's release origin, `https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/`, over
  HTTPS with a verified certificate and no redirects: for the signed release index when somebody
  presses *Sprawdź aktualizacje wtyczki* (at most once in ten minutes, a limit shared with Home
  Assistant's own check for updates, which asks only while the daily option is on), and once a
  day when the option to check daily is on (off by default); and for the package of a release that
  an install, a downgrade or the relay below needs, unless the bundled package is those very
  bytes;
- GitHub's API, when such a package is downloaded, to compare GitHub's own digest of the release
  asset with the signed checksum - its answer is kept for a day, in memory. A disagreement refuses
  the package; an API that cannot be reached does not.

It also **serves** a package: when a receiver without internet asks, or when the card updates a
receiver over MQTT, Home Assistant offers the verified package at
`/api/enigma2_mqtt/relay/<token>`, without authentication, for ten minutes, to that receiver's IPv4
address alone. The address travels over the broker; the binding to the receiver's address is as
strong as your `trusted_proxies` setting
([DOCUMENTATION.md §4.5](DOCUMENTATION.md#45-buttons-and-update) says what it does behind a
reverse proxy). Whoever gets past that binding can download the public, signed package - nothing
more.

**What an install trusts** ([ADR-0008](docs/adr/0008-signed-plugin-index.md)). Home Assistant
installs, and asks a receiver to install, only a release named in the plugin's signed release
index, and holds the package to that entry's size and SHA-256 before the receiver is connected to
or the package is relayed; the receiver checks it again. The index is verified with two Ed25519
public keys built into this integration and into the plugin: a **main key**, used only in the
plugin repository's CI, in a signing job the maintainer approves by hand for each index, and a
**spare key** of higher rank, kept sealed offline, which signs only if the main key is lost or
leaked. An index is accepted only when its serial rises for its key - by at most 1000 - and its key is not ranked below one already
accepted; every newly accepted index is announced in the log and as a persistent notification with
its serial, its key, the versions it adds and withdraws, and its floor. The origin, the broker and
Home Assistant's relay are couriers, not authorities. Home Assistant never starts a downgrade over
MQTT: it offers an older plugin only over SSH, through a confirmed step in the options flow. A
downgrade chosen on the receiver itself, on its screen or its OpenWebif page, may fetch its package
through the relay; the receiver verifies it against its own index. A lost or leaked
main key is handled as the plugin's
[RELEASE-INDEX.md](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/RELEASE-INDEX.md#when-the-main-key-is-lost-or-leaked)
says: a leak is treated as a theft - the spare signs the next index, and the next release of both
halves no longer embeds the main key.

**What that does not cover.**

- Whoever controls the origin, the broker or the network in between can delay or withhold an index
  or an install - a broker client can, for example, forge a receiver's retained `info` so that the
  card binds its relay to an address of the forger's choosing - but cannot get anything installed
  that the index does not name. That is deny and delay, never an install. A forged
  `relay_request` also makes Home Assistant download a release the rule allows from the origin -
  at most once a minute per receiver - and cross-check it with GitHub, which is asked about each
  version at most once a day while Home Assistant runs, whoever asks.
- An index has **no expiry**: a withheld index cannot be detected, and a withdrawal reaches an
  installation only with a newer index.
- Because the main key is used in CI, a compromise of the maintainer's GitHub account, a malicious
  workflow change merged to the plugin's `main`, or a compromised action in the signing job can
  produce a validly signed index, and this integration would offer and install what it names. The
  approval gate, the environment that admits only `main`, the tag and `main` rulesets, actions
  pinned to full commit SHAs, two-factor authentication and the notification for every new index
  make that **detectable, and recoverable with the spare key; they do not prevent it**.
- **HACS, this integration's own update path, is not signed** and is rooted in GitHub, and this
  integration holds the receiver's root credentials when you keep them. The signed index shares
  that root of trust: the signature adds the checks above, not a second, independent root.
- The image's own package manager reading the plugin's opkg feed, and any install by hand, check no
  signature.

**Privacy of the topics.** The `key` and `epg` topics reveal what is watched and which buttons
are pressed, and the screen image is a picture of the television.
[DOCUMENTATION.md §10](DOCUMENTATION.md#10-privacy) documents the recorder exclusions; key
publishing can be switched off on the box.

## Supply chain

Releases are built by GitHub Actions from the tag. The bundled receiver plugin is built
reproducibly from the exact public commit of
[enigma2-mqtt-bridge](https://github.com/deltasystems-pl/enigma2-mqtt-bridge) recorded in
`bundled/metadata.json`, together with the complete corresponding source archive, and both files'
hashes are recorded there. CI rebuilds both byte for byte, and the release workflow refuses to
publish unless the bundled package is a release build of a commit that carries the plugin's
release tag - between releases `main` may bundle a development build of the plugin's candidate,
which says so in its build id. The installer verifies the package again before it uploads it.

Since 0.4.0 a package can also be downloaded at runtime: only a release the signed index lists,
from the plugin's origin, verified by signature, size and SHA-256 - not rebuilt by this
repository's CI. The forced reinstall installs only the bundle.

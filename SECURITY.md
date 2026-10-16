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

**The receiver menu.** The receiver's menu ([DOCUMENTATION.md](DOCUMENTATION.md#receiver-menu))
makes Home Assistant call services on a message from the broker, `menu_action`. Anyone who can
publish to the broker can send one, so the integration treats it as untrusted input: it performs
only an action on a row the user put into the menu, with that row's action and a value the bound
entity accepts, one action per row at a time and at most five a second per receiver (refused
actions do not count towards the five), and refuses everything else ([ADR-0012](docs/adr/0012-receiver-menu.md)). Put into
the menu only what you would hand to anyone holding the remote. The menu - the names and states
of the entities in it - is retained on the broker for any login to read; a text entity in password
mode is published without its text, whether the menu shows it as a text field or as an indicator.
Anyone who can publish to the broker can also raise and clear the repair about the receiver's
clock - a nuisance only, since it changes nothing but what that repair says.

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

It also asks **your receiver's own web interface** (OpenWebif) for channel picons, over plain
HTTP on port 80: one question and one picture when the media player needs the picon of a channel
it has not seen, and at most one list each time a bouquet is opened in the media browser. It sends no
credentials, follows no redirect, takes only a path under `/picon/` from the answer, limits every
answer in size, and shows only a PNG or a JPEG
([DOCUMENTATION.md §4.1](DOCUMENTATION.md#41-media-player)).

**Which host that is, and what is left open.** If a receiver host was typed when the receiver was
added, that host is asked, whatever it is, and nothing on the broker can change it - a name,
an IPv4 address, or an IPv6 address written bare, without a zone and without brackets. A typed
host that cannot be the host of a web address - an IPv6 address with a zone (`fe80::1%eth0`), a
host with a port or a user name, which SSH accepts and HTTP does not, an IPv6 address written in
brackets, a name with a character outside ASCII - means no picons at all,
never the reported address. If none was
typed, the address the receiver reports on its `info` topic is asked - and anything that may
publish on your broker can write that topic. Such an address is used only when it is an IP address
written out, never a host name, and is not loopback, link-local, unspecified, multicast or
reserved, so Home Assistant cannot be pointed at itself or at a cloud metadata address this way.
It **can** still be pointed at another address on your network. What that gets a broker client:
Home Assistant sends that host the request with the current channel's service reference in it -
for an IPTV channel the reference contains the stream's address - and shows on the media player
card a PNG or JPEG of at most 1 MiB that the host serves under `/picon/`. Nothing else of the
answer is used or shown. Typing the receiver's host when adding it (or with *Reconfigure*) closes
this.

**Streams** (since the release after 0.4.0, with plugin 0.5.0;
[DOCUMENTATION.md §4.7](DOCUMENTATION.md#47-streams)). Home Assistant opens no stream and
carries no video. Two things about streams do leave it:

- **Your own go2rtc, if you name one.** While an address is set in a receiver's options, Home
  Assistant sends that address `PUT /api/streams?name=<name>&src=<source>` when the channel
  on that receiver changes. **Home Assistant calls an address you typed**, whatever it is, and
  sends it the stream name and the source and nothing else - no token of Home Assistant's, no
  body, no redirect followed. When go2rtc answers with anything but a 2xx, Home Assistant
  also reads the stream back from the same address (`GET /api/streams?src=<name>`, the same
  limits), because go2rtc can answer 400 for a change it did make; at most the first 512
  bytes of the error answer are read, and at most 80 characters of its first line reach the
  log - after percent-decoding, and with the source, the receiver's stream address, the
  service reference, the receiver's host (as reached, typed and reported), written IP
  addresses and anything with a scheme taken out. A host name that is not the receiver's
  can remain. The source is your template
  with the receiver's stream address in it, which contains the receiver's address and the
  service reference of the channel that is on. If the address you typed carries a user name
  and a password, they are sent to that host as HTTP basic authentication, over plain HTTP
  if the address says `http://`; they are stored in the config entry like every option, are
  never written to the log or a repair - both name the host alone - and are redacted in the
  diagnostics download. It is off while the address is empty. Nothing on the broker can set
  or change the address, the name or the template. What a broker client **can** do is the
  same as for picons, below: forge the `info.ip` and the `stream` port and path of a receiver
  without a typed host, and so choose the address *inside* the source your go2rtc is told to
  fetch. go2rtc would then open `http://<that address>:<that port>/<path>` on your network.
  The path takes only letters, digits and `/ . _ ~ -` around the reference; the address
  rule is the picons' rule. Typing the receiver's host closes the address half of it.
- **The stream address handed to a player.** The media player's `stream_url` attribute and
  the media source give a player the receiver's own stream address, plain HTTP on the
  receiver's stream port. The player fetches it from the receiver directly. No address is
  built while the receiver says its stream server wants a login, and this integration never
  puts credentials into an address and stores none for streams.

The option that hides bouquets is **not an access control**: it decides what Home Assistant
shows and offers. The receiver publishes every stream client and every channel on the broker,
and whoever may publish to the receiver's command topics may stop any client's stream -
the plugin guards `cmd/stream_stop` with no permission, as it guards a zap with none.

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

# Security policy

## Reporting a vulnerability

Report privately through **GitHub security advisories**:
[*Security → Report a vulnerability*](https://github.com/deltasystems-pl/hass-enigma2-mqtt/security/advisories/new)
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
topics — the README has the snippet. The credential lives on the box, and most Enigma2 images
ship with a default root password and an open telnet or SSH service, so treat the receiver as
the least trusted device on the network and change that password.

**SSH password.** The guided installer asks for the receiver's SSH password. It is
**discarded after a successful install** unless you explicitly ask to keep it for later plugin
updates. If you do keep it, it is stored in the config entry — and Home Assistant's `.storage`
is **not encrypted at rest**, so anyone with the configuration directory or a backup of it can
read it. The password is never written to the log, and diagnostics redact both it and the
broker credential.

**No telemetry.** The integration makes no outbound connection other than to your broker, and
over SSH to your receiver when you ask it to install or update the plugin. The optional check
for a newer plugin release on GitHub is **off by default**. Nothing is reported anywhere.

**Privacy of the topics.** The `key` and `epg` topics reveal what is watched and which buttons
are pressed, and the screen image is a picture of the television. The README documents the
recorder exclusions; key publishing can be switched off on the box.

## Supply chain

Releases are built by GitHub Actions from the tag. The bundled receiver plugin (from M4) is built
reproducibly from the exact public commit recorded in package metadata. The integration artifact
also carries the complete corresponding source archive and hashes both files. A development
candidate may pin reviewed code newer than the last plugin tag without pretending it is that
tagged release; a published release pins an immutable public commit of
[enigma2-mqtt-bridge](https://github.com/deltasystems-pl/enigma2-mqtt-bridge). CI reproduces both
archives before publishing, and the installer verifies the IPK again before uploading it.

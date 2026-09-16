# Changelog

All notable changes to this integration are documented here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-16

The first release. It sets a receiver up and gives it a device page; the entities and
actions the README describes arrive in a later release.

### Added

- **Setup by discovery.** A receiver running the `enigma2-mqtt-bridge` plugin announces
  itself and turns up in *Settings → Devices & Services*, named with its box type and its
  image. Confirming it switches the receiver into integration mode and waits for the
  receiver to say the switch worked, so a box that cannot be reached is reported instead of
  being added as a dead device.
- **Setup by hand.** Base topic, node id and an optional name, for a receiver behind an MQTT
  bridge that rewrites the topic prefix, or one whose announcement never arrived. The flow
  checks the receiver is really on that topic before it adds anything.
- **A device page for each receiver** — the manufacturer, the model, the image and plugin
  versions, and a link to the receiver's own web interface. It follows the receiver, so
  updating the plugin or moving the box to another address updates the page.
- **A diagnostics download** to attach to a bug report, with the MAC address, the IP address,
  the web interface link and every password removed.
- **Removing a receiver hands it back** to Home Assistant's own MQTT discovery, so the basic
  entities return. It is best effort: a receiver that is switched off, or a broker that
  cannot be reached, never blocks the removal.
- **Polish and German translations** of everything the setup screens show. German is a draft
  and would welcome a native speaker.
- **Installation through HACS** as a custom repository, alongside the README, the full
  documentation, the security and contribution policies and the decision records behind the
  design.
- **Checks on every change** — hassfest, the HACS action, ruff and the test suite run on
  every push, every pull request, once a week and on the release tag itself, and a release
  is only published when the tag, the manifest version and the changelog agree.

[Unreleased]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/releases/tag/v0.1.0

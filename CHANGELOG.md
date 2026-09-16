# Changelog

All notable changes to this integration are documented here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The public scaffold of the repository: README, full documentation skeleton, security and
  contribution policies, code of conduct, changelog, quality checklist and the first
  architecture decision records (the approved PRD and the M0 sign-off).
- The integration skeleton `custom_components/enigma2_mqtt` — manifest, constants and the
  English, Polish and German translation files — which loads in Home Assistant and does
  nothing else yet.
- Continuous integration: hassfest, the HACS action, ruff and pytest on every push, every
  pull request and weekly; and a release workflow that checks the tag against the manifest
  version and the changelog before publishing the HACS zip.
- A config flow for a receiver that announces itself on `enigma2mqtt/discovery/#`: the card
  names the box, its type and its image, and confirming it switches the box into integration
  mode and waits for the plugin to acknowledge the switch before the entry is created.
- A manual config flow — base topic, node ID and an optional name — for a receiver behind an
  MQTT bridge that rewrites the topic prefix, or one whose announcement never arrived. The
  flow checks the receiver is really on that topic before it adds anything.
- A device page per receiver, with the manufacturer derived from the box type, the model, the
  image and plugin versions, and a link to the receiver's web interface; it follows the `info`
  topic, so updating the plugin or moving the box to another address updates the page.
- A diagnostics download that redacts the MAC address, the IP address, the configuration URL
  and every credential key, including the ones the guided installer will add later.
- Removing a receiver publishes `cmd/ha_mode = discovery`, so the plugin announces itself
  again and the core MQTT integration rebuilds its own entities. It is best effort: a
  receiver that is off, or a broker that is unreachable, never blocks the removal.
- Polish and German translations of everything the setup flow shows. German is a draft and
  needs a native speaker.

### Changed

- The validation workflow also runs on a release tag, so hassfest, the HACS action, ruff and
  pytest all have to pass before a release is published.
- `requirements_test.txt` pins the test harness and the linter, so CI and a developer's
  machine run the same Home Assistant release.

### Fixed

- The release workflow no longer put the changelog's link block into the release notes.

[Unreleased]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/commits/main

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

[Unreleased]: https://github.com/deltasystems-pl/hass-enigma2-mqtt/commits/main

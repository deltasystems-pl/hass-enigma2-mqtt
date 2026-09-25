# Contributing

Thank you for helping. This is a small project with a public record: every change lands
through a pull request with green CI, and every decision that shapes the product is written
down as an ADR.

## What helps most right now

- **Testers on other images.** OpenViX 6.6 on a Vu+ Uno 4K SE is the only box the maintainer
  can run. OpenATV, OpenPLi and OpenBH need someone with the hardware - say so in an issue and
  you get a call-for-testers checklist.
- **Translations.** English is the source language, Polish is reviewed by the maintainer,
  **German is drafted and needs a native speaker**. Other languages are welcome.
- **Bug reports with the diagnostics download** attached (it redacts the credentials) and the
  retained topics of the box, from any MQTT client.

## Development loop

The integration is tested with
[`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component),
which pins the Home Assistant release the tests run against. Home Assistant 2026.3 and later
need **Python 3.14**.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements_test.txt

ruff check .
pytest -q
```

Tests live in `tests/`, mirroring the module they cover. `tests/conftest.py` enables custom
integrations for every test; the MQTT side is exercised with the `mqtt_mock` fixture, never
against a real broker, and the SSH installer (M4) is tested against a fake SSH server.

**hassfest** and the **HACS action** are not run locally - they are GitHub Actions, and CI
runs them on every push and pull request. Open a draft pull request if you want their verdict
before the work is finished.

For work that needs a real receiver, the plugin repository
([enigma2-mqtt-bridge](https://github.com/deltasystems-pl/enigma2-mqtt-bridge)) has the deploy
script and the by-effect checklist.

## Pull requests

- One topic per pull request, branched off `main`.
- **CI must be green** - hassfest, the HACS action, ruff and pytest. This applies to the
  maintainer too; nothing is pushed straight to `main`.
- Update `CHANGELOG.md` under `## [Unreleased]` when the change is visible to a user.
- Update the documentation in the same pull request as the behaviour.
- New or changed behaviour comes with a test.

## Commit and comment style

- Plain, conventional messages: an imperative subject line of about 70 characters, a body that
  explains *why* when the change is not obvious.
- **No trailers, no attribution footers, no generated-by lines** - in commit messages, pull
  request bodies, issues, release notes or code comments. Authorship is the person opening the
  pull request.
- Comments explain intent, not syntax.

## Architecture decisions

Anything that constrains later work - a topic contract, a dependency, a mode, a naming
scheme - is recorded in [`docs/adr/`](docs/adr/). Superseded decisions are marked as
superseded, never deleted. [ADR-0000](docs/adr/0000-prd.md) is the approved product
requirements document; [ADR-0001](docs/adr/0001-m0-decisions.md) closed its open questions.

## Translations

- `custom_components/enigma2_mqtt/translations/en.json` is the **source**; every key starts
  there.
- `pl.json` is reviewed by the maintainer. `de.json` is a draft and **needs community
  review** - corrections are welcome as pull requests.
- Translation **keys are English** and unique ids derive from them, so a key is never renamed.
  🔴 Entity ids derive from the **displayed name in the installation's language**: Home
  Assistant slugifies the `pl.json` name on a Polish installation and the `en.json` name on an
  English one. So the name of an existing entity is never renamed **in any language** - new
  installations would get different entity ids from existing ones, and the documented
  examples and shared automations would stop matching.
- Every file covers the same ground: the config and options flows, the name of each of the
  twenty-six entities, the eight device triggers, and the name, description and every field
  of each of the nine actions. A file that is missing a key falls back to English, which
  looks like a bug rather than a gap.
- A new language is one file plus a line in the pull request saying who reviewed it.

## Code of conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

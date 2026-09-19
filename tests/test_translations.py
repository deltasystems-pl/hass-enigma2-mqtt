"""The strings a household actually reads, checked against each other.

`strings.json` is the source Home Assistant validates against, and `translations/en.json`
is what an English installation displays. Nothing keeps the two in step by itself, and
they have already drifted once: a sentence added to the install step never reached
en.json, so the screen that asks for a password said less than the source claimed it
did. Byte equality is the cheapest rule that cannot drift.

The other languages may say it differently, but they have to say all of it, and they
have to leave the same holes for Home Assistant to fill — a placeholder that exists in
one language and not another is a message that renders `{bouquet}` at somebody.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from custom_components.enigma2_mqtt.installer import InstallerErrorCode

COMPONENT = Path(__file__).parent.parent / "custom_components" / "enigma2_mqtt"
SOURCE = COMPONENT / "strings.json"
TRANSLATIONS = COMPONENT / "translations"
LANGUAGES = ("en", "pl", "de")
PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def _flatten(value: object, prefix: str = "") -> dict[str, str]:
    """Return every leaf of a translation file, keyed by its dotted path."""
    if isinstance(value, dict):
        flat: dict[str, str] = {}
        for key, child in value.items():
            flat.update(_flatten(child, f"{prefix}.{key}" if prefix else key))
        return flat
    return {prefix: value if isinstance(value, str) else json.dumps(value)}


def test_the_english_translation_is_the_source_verbatim() -> None:
    """Anything else means an English installation shows a different text."""
    assert (TRANSLATIONS / "en.json").read_bytes() == SOURCE.read_bytes()


def test_every_language_covers_every_string() -> None:
    """A missing key falls back to English; an extra key is a string nobody sees."""
    expected = set(_flatten(json.loads(SOURCE.read_text(encoding="utf-8"))))

    for language in LANGUAGES:
        path = TRANSLATIONS / f"{language}.json"
        keys = set(_flatten(json.loads(path.read_text(encoding="utf-8"))))
        assert keys == expected, f"{language}.json does not carry the same keys"


def test_every_installer_failure_has_an_abort_string() -> None:
    """The guided install ends by aborting with the error code as the reason.

    `config.error` and `config.abort` are different sections, and the installer's codes
    only lived in the first one. A flow that aborted with `no_space` therefore put the
    word `no_space` on the screen — a token, in English, where a sentence belonged, and
    in every language at once.
    """
    for language in LANGUAGES:
        path = TRANSLATIONS / f"{language}.json"
        aborts = json.loads(path.read_text(encoding="utf-8"))["config"]["abort"]
        for code in InstallerErrorCode:
            assert code.value in aborts, f"{language}.json has no abort string for {code.value}"
            assert aborts[code.value].strip(), f"{language}.json:{code.value} is empty"

    source = json.loads(SOURCE.read_text(encoding="utf-8"))["config"]["abort"]
    for code in InstallerErrorCode:
        assert code.value in source
    # A rollback that failed is the one outcome a user has to act on themselves.
    assert "mqttbridge-backups" in source["rollback_failed"]


def test_every_language_leaves_the_same_placeholders() -> None:
    """Home Assistant fills `{name}`; a language that drops it shows the braces."""
    source = _flatten(json.loads(SOURCE.read_text(encoding="utf-8")))
    translations = {
        language: _flatten(
            json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))
        )
        for language in LANGUAGES
    }

    for key, text in source.items():
        expected = set(PLACEHOLDER.findall(text))
        for language, flat in translations.items():
            assert set(PLACEHOLDER.findall(flat[key])) == expected, (
                f"{language}.json:{key} does not use the same placeholders"
            )

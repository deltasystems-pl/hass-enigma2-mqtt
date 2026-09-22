"""The plugin's defaults, read out of the plugin rather than remembered.

🔴 Enigma2 does not write out a setting whose value still equals its default, so a
receiver's `/etc/enigma2/settings` is a list of what somebody changed and nothing else.
Everything that compares a stored plugin setting therefore has to supply the default
itself, and a default that is believed here but not declared there is a silent wrong
answer — a box on the default base topic read as a box configured for another one, and
a reinstall over a plugin that is working refused for a difference it was never given.
The defaults used to be applied on the receiver, inside the installer helper, which is
where nothing could compare them with the plugin at all (2026-09-22).

`PLUGIN_SETTING_DEFAULTS` is the one copy of them. This checks it against the bundled
plugin's own `config.py` — the source the metadata pins by digest and commit, which is
also the source of the package the installer puts on the receiver — so the table is a
mirror that is verified rather than a comment claiming to be one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import tarfile

import pytest

from custom_components.enigma2_mqtt.const import PLUGIN_SETTING_DEFAULTS

_BUNDLE_DIR = (
    Path(__file__).parent.parent / "custom_components" / "enigma2_mqtt" / "bundled"
)


def _plugin_config_source() -> str:
    """Return `src/MQTTBridge/config.py` from the bundled plugin source archive."""
    metadata = json.loads((_BUNDLE_DIR / "metadata.json").read_text(encoding="utf-8"))
    archive = _BUNDLE_DIR / metadata["source_filename"]
    prefix = f"enigma2-mqtt-bridge-{metadata['source_commit']}/"
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(f"{prefix}src/MQTTBridge/config.py")
        assert member is not None
        return member.read().decode("utf-8")


def _declared_defaults() -> dict[str, object]:
    """Return every `section.<name> = Config…(default=…)` the plugin declares.

    Parsed rather than imported: the plugin's `config.py` imports enigma2's own
    `Components.config`, which exists only on a receiver. The module's own constants
    are resolved first, because two of the defaults are written as names.
    """
    tree = ast.parse(_plugin_config_source())
    constants: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    constants[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue

    declared: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            not isinstance(target, ast.Attribute)
            or not isinstance(target.value, ast.Name)
            or target.value.id != "section"
            or not isinstance(node.value, ast.Call)
        ):
            continue
        for keyword in node.value.keywords:
            if keyword.arg != "default":
                continue
            if isinstance(keyword.value, ast.Name):
                declared[target.attr] = constants[keyword.value.id]
            else:
                declared[target.attr] = ast.literal_eval(keyword.value)
    return declared


def test_every_default_is_the_one_the_receiver_plugin_declares() -> None:
    """One table, checked against the plugin, so the two cannot drift apart."""
    assert _declared_defaults() == PLUGIN_SETTING_DEFAULTS


@pytest.mark.parametrize("name", sorted(PLUGIN_SETTING_DEFAULTS))
def test_each_setting_is_declared_with_the_type_this_side_expects(name: str) -> None:
    """A table entry of the wrong type compares unequal to everything the box stores."""
    declared = _declared_defaults()[name]
    expected = PLUGIN_SETTING_DEFAULTS[name]
    assert type(declared) is type(expected)
    assert declared == expected

#!/usr/bin/env python3
"""Check that what this integration shares with the plugin is the plugin's, at the pinned commit.

    tools/check-plugin-shared.py --plugin-tree <the plugin, checked out at SOURCE.json's commit>

Three things are written down twice, once in each repository, and a reader that disagreed with
the other would install what the other refuses:

- the **shared vectors** of the signed release index - a byte-identical copy here, whose
  sha256 `tests/vectors/SOURCE.json` records with the plugin commit it was taken from;
- the **embedded keys** - `PLUGIN_INDEX_KEYS` in `const.py` against `EMBEDDED` in the plugin's
  `src/MQTTBridge/trust.py`: ids, ranks, public keys, baselines;
- the **contract** - `PLUGIN_CONTRACT` against `docs/contract.json`'s major, and
  `PLUGIN_CONTRACT_EXCEPTIONS` against its named in-major exceptions, so that a new exception
  cannot arrive without somebody reading it here.

Both files are read as text and parsed as literals; nothing from either tree is imported, so
this runs on the runner's Python with nothing installed. Exit 0 when all agree, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONST = ROOT / "custom_components/enigma2_mqtt/const.py"
READER = ROOT / "custom_components/enigma2_mqtt/release_index.py"
BUNDLE_METADATA = ROOT / "custom_components/enigma2_mqtt/bundled/metadata.json"
VECTORS = ROOT / "tests/vectors/release-index.json"
SOURCE = ROOT / "tests/vectors/SOURCE.json"


def _assignments(path: Path) -> dict[str, ast.expr]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            found[node.target.id] = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
    return found


def integration_keys() -> list[tuple[str, int, str, int]]:
    """`PLUGIN_INDEX_KEYS` from const.py, as `(key_id, rank, public, baseline)`."""
    value = ast.literal_eval(_assignments(CONST)["PLUGIN_INDEX_KEYS"])
    return sorted((k["key_id"], k["rank"], k["public"], k["baseline"]) for k in value)


def plugin_keys(trust: Path) -> list[tuple[str, int, str, int]]:
    """The keys the plugin's `trust.py` embeds: every `_key(...)` named in `EMBEDDED`."""
    found = _assignments(trust)
    embedded = found["EMBEDDED"]
    if not isinstance(embedded, ast.Tuple):
        raise SystemExit("trust.py: EMBEDDED is not a tuple of names")
    keys = []
    for element in embedded.elts:
        if not isinstance(element, ast.Name):
            raise SystemExit("trust.py: EMBEDDED names something that is not a key constant")
        call = found[element.id]
        if not isinstance(call, ast.Call) or len(call.args) != 4:
            raise SystemExit(f"trust.py: {element.id} is not a _key(...) call")
        keys.append(tuple(ast.literal_eval(argument) for argument in call.args))
    return sorted(keys)


def _value(node: ast.expr) -> object:
    """A literal, or a product of literals such as `64 * 1024`."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _value(node.left) * _value(node.right)  # type: ignore[operator]
    return ast.literal_eval(node)


def _bundled_source_checks() -> list[str]:
    """The same comparison against the plugin this integration bundles, once it can be made.

    The bundled source archive is a release's, and 0.3.0 predates the signed index: it has no
    `trust.py` and no vectors, and then the pinned commit above is the only comparison there
    is. From the first bundle that carries them, the bundled plugin must agree too - otherwise
    this integration could ship a plugin whose keys or vectors are not the ones it checks
    against.
    """
    metadata = json.loads(BUNDLE_METADATA.read_text(encoding="utf-8"))
    archive = BUNDLE_METADATA.parent / metadata["source_filename"]
    prefix = f"enigma2-mqtt-bridge-{metadata['source_commit']}/"
    failures: list[str] = []
    with tarfile.open(archive, "r:gz") as source:
        names = set(source.getnames())
        trust_name = prefix + "src/MQTTBridge/trust.py"
        vectors_name = prefix + "tests/vectors/release-index.json"
        if trust_name not in names:
            print("check-plugin-shared: the bundled plugin predates the signed index; "
                  "compared with the pinned commit only")
            return failures
        with tempfile.TemporaryDirectory() as temporary:
            trust = Path(temporary) / "trust.py"
            trust.write_bytes(source.extractfile(trust_name).read())
            if integration_keys() != plugin_keys(trust):
                failures.append("the bundled plugin's embedded keys are not PLUGIN_INDEX_KEYS")
        if vectors_name in names:
            if source.extractfile(vectors_name).read() != VECTORS.read_bytes():
                failures.append("the bundled plugin's vectors are not tests/vectors' copy")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-tree", type=Path, required=True)
    args = parser.parse_args(argv)
    plugin = args.plugin_tree
    failures: list[str] = []

    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    copied = VECTORS.read_bytes()
    if hashlib.sha256(copied).hexdigest() != source["sha256"]:
        failures.append("tests/vectors/release-index.json is not the file SOURCE.json records")
    if (plugin / source["path"]).read_bytes() != copied:
        failures.append(
            f"tests/vectors/release-index.json differs from the plugin's at {source['commit']}"
        )

    trust = plugin / "src/MQTTBridge/trust.py"
    if integration_keys() != plugin_keys(trust):
        failures.append("PLUGIN_INDEX_KEYS in const.py is not the plugin's EMBEDDED key set")

    # The limits and the origin, which the vectors carry only in part.
    theirs_values = _assignments(trust)
    ours_values = {**_assignments(READER), **_assignments(CONST)}
    for theirs_name, ours_name in (
        ("ORIGIN", "PLUGIN_INDEX_ORIGIN"),
        ("MAX_JUMP", "MAX_JUMP"),
        ("MAX_INDEX_BYTES", "MAX_INDEX_BYTES"),
        ("MAX_SIGNATURE_BYTES", "MAX_SIGNATURE_BYTES"),
        ("MAX_PACKAGE_BYTES", "MAX_PACKAGE_BYTES"),
        ("PACKAGE", "PACKAGE"),
    ):
        if _value(theirs_values[theirs_name]) != _value(ours_values[ours_name]):
            failures.append(f"{ours_name} is not the plugin's {theirs_name}")

    failures.extend(_bundled_source_checks())

    contract = json.loads((plugin / "docs/contract.json").read_text(encoding="utf-8"))
    constants = _assignments(CONST)
    if ast.literal_eval(constants["PLUGIN_CONTRACT"]) != contract["contract"]:
        failures.append("PLUGIN_CONTRACT is not the plugin's contract major")
    ours = list(ast.literal_eval(constants["PLUGIN_CONTRACT_EXCEPTIONS"]))
    theirs = [item["name"] for item in contract["exceptions"]]
    if ours != theirs:
        failures.append(
            "PLUGIN_CONTRACT_EXCEPTIONS is not the plugin's list of named exceptions: "
            f"{ours} against {theirs}"
        )

    for failure in failures:
        print(f"check-plugin-shared: {failure}", file=sys.stderr)
    if not failures:
        print(f"check-plugin-shared: vectors, keys and contract match the plugin at "
              f"{source['commit']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

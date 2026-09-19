#!/usr/bin/env python3
"""Build a reproducible plugin IPK and corresponding source bundle from one commit."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "custom_components/enigma2_mqtt/bundled"
PACKAGE = "enigma2-plugin-extensions-mqttbridge"
SOURCE_REPOSITORY = "https://github.com/deltasystems-pl/enigma2-mqtt-bridge"


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def version_from(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__version__"
                   for target in statement.targets):
            continue
        value = ast.literal_eval(statement.value)
        if isinstance(value, str) and re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?", value
        ):
            return value
    raise SystemExit("plugin version is missing or is not a string literal")


def replace_bundle(staged: Path, destination: Path) -> None:
    if destination.exists():
        try:
            current = json.loads((destination / "metadata.json").read_text(encoding="utf-8"))
            expected = {"metadata.json", current["filename"], current["source_filename"]}
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SystemExit("existing bundle directory is not owned metadata") from error
        actual = {item.name for item in destination.iterdir()}
        if actual != expected or not all(item.is_file() for item in destination.iterdir()):
            raise SystemExit("existing bundle directory contains unexpected files")

    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}-old-{uuid.uuid4().hex}")
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except Exception:
        if backup.exists():
            os.replace(backup, destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plugin-tree", type=Path,
        default=ROOT.parent / "enigma2-mqtt-bridge",
        help="clean enigma2-mqtt-bridge git checkout",
    )
    parser.add_argument("--output", type=Path, default=DESTINATION)
    args = parser.parse_args()
    plugin = args.plugin_tree.resolve()
    if run("git", "status", "--porcelain", cwd=plugin):
        raise SystemExit("plugin source tree is dirty; bundle provenance requires a clean commit")
    commit = run("git", "rev-parse", "HEAD", cwd=plugin)
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise SystemExit("plugin source commit is invalid")
    epoch = run("git", "show", "-s", "--format=%ct", commit, cwd=plugin)

    with tempfile.TemporaryDirectory(prefix="enigma2-plugin-bundle-") as temporary:
        temporary_path = Path(temporary)
        exported = temporary_path / "tree"
        exported.mkdir()
        archive = temporary_path / "source.tar"
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "archive", "--format=tar", commit],
                cwd=plugin,
                stdout=output,
                check=True,
            )
        with tarfile.open(archive) as source:
            source.extractall(exported, filter="data")
        environment = {**os.environ, "SOURCE_DATE_EPOCH": epoch}
        subprocess.run(
            ["bash", "tools/build-ipk.sh", "--allow-unreleased"],
            cwd=exported,
            env=environment,
            check=True,
        )
        version = version_from(exported / "src/MQTTBridge/version.py")
        built = exported / f"dist/{PACKAGE}_{version}_all.ipk"
        if not built.is_file():
            raise SystemExit("plugin build produced no IPK")

        source_archive = temporary_path / "plugin-source.tar.gz"
        with source_archive.open("wb") as output:
            subprocess.run(
                [
                    "git", "archive", "--format=tar.gz",
                    f"--prefix=enigma2-mqtt-bridge-{commit}/", commit,
                ],
                cwd=plugin,
                stdout=output,
                check=True,
            )
        with tarfile.open(source_archive) as source:
            expected_license = f"enigma2-mqtt-bridge-{commit}/LICENSE"
            if expected_license not in source.getnames():
                raise SystemExit("corresponding source archive has no GPL license")

        destination = args.output.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=f".{destination.name}-new-", dir=destination.parent))
        package_name = f"{PACKAGE}_{version}_all.ipk"
        source_name = f"enigma2-mqtt-bridge-{commit}.tar.gz"
        package_target = staged / package_name
        source_target = staged / source_name
        shutil.copyfile(built, package_target)
        shutil.copyfile(source_archive, source_target)
        metadata = {
            "schema": 1,
            "filename": package_name,
            "version": version,
            "sha256": digest(package_target),
            "size": package_target.stat().st_size,
            "source_filename": source_name,
            "source_sha256": digest(source_target),
            "source_size": source_target.stat().st_size,
            "source_commit": commit,
            "source_url": f"{SOURCE_REPOSITORY}/tree/{commit}",
            "license": "GPL-2.0-or-later",
        }
        (staged / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        replace_bundle(staged, destination)
        print(f"plugin bundle: version={version} commit={commit} files=3")


if __name__ == "__main__":
    main()

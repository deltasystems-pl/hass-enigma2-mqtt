#!/usr/bin/env python3
"""Refuse to release this integration with a plugin bundle that is not a plugin release.

    tools/check-release-bundle.py --plugin-tree <the plugin, with its tags, at any commit>

Between two releases `main` may bundle a candidate of the plugin - a development build of a
commit that carries no release tag - so that the integration can be tested against it. A tag
cut from such a `main` would ship that candidate to every household: a receiver on an older
plugin would be offered it, every guided install would put it on the receiver, and the forced
reinstall would put it over a released plugin. The release workflow runs this before it builds
the zip, and it passes only for a bundle that is a release of the plugin:

- the bundled package's build id says `release`, is not dirty, and names the commit the
  bundle's `metadata.json` names - the package the plugin's release workflow published is
  exactly that, because the bundle builder passes the flavour it finds at the tag;
- that commit carries the plugin's tag `v<version>` for the version `metadata.json` names, in
  the plugin repository checked out at `--plugin-tree` with its tags.

Plugin releases before 0.4.0 were built before build ids existed, so their packages carry
none; for those the tag alone decides. From 0.4.0 on a package without a build id is refused,
because every release from then on carries one.

Nothing from the integration is imported except `buildid.py`, which is standard library only
and is loaded by its path, so this runs on the runner's Python with nothing installed. Exit 0
when the bundle is a release, 1 otherwise.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "custom_components/enigma2_mqtt/bundled"
BUILDID = ROOT / "custom_components/enigma2_mqtt/buildid.py"
# The first plugin release whose package carries a build id.
FIRST_WITH_BUILD_ID = (0, 4, 0)


def _buildid():
    spec = importlib.util.spec_from_file_location("enigma2_mqtt_buildid", BUILDID)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses look their module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tags_at(plugin: Path, commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "tag", "--points-at", commit],
        cwd=plugin,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise SystemExit(
            f"check-release-bundle: cannot list the plugin's tags at {commit}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.split()


def failures(bundle: Path, plugin: Path) -> list[str]:
    """Everything that makes `bundle` something other than a plugin release."""
    buildid = _buildid()
    metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    version = metadata.get("version")
    commit = metadata.get("source_commit")
    if not isinstance(version, str) or buildid.base_version(version) is None:
        return [f"metadata.json names no plain release version: {version!r}"]
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        return [f"metadata.json names no source commit: {commit!r}"]
    found: list[str] = []

    package = (bundle / metadata["filename"]).read_bytes()
    try:
        build = buildid.package_build(package)
    except buildid.BuildInfoError as error:
        found.append(f"the bundled package's build id cannot be read: {error}")
    else:
        if build is None:
            if buildid.base_version(version) >= FIRST_WITH_BUILD_ID:
                found.append(
                    f"the bundled package carries no build id, and every plugin release "
                    f"from {'.'.join(map(str, FIRST_WITH_BUILD_ID))} on carries one"
                )
        else:
            if build["flavour"] != buildid.RELEASE:
                found.append(
                    f"the bundled package's build flavour is {build['flavour']!r}, "
                    "not a release build"
                )
            if build["dirty"] is not False:
                found.append("the bundled package was not built from a clean tree")
            if build["commit"] != commit:
                found.append(
                    f"the bundled package was built from {build['commit'] or 'no commit'}, "
                    f"not from the commit metadata.json names ({commit})"
                )

    if f"v{version}" not in _tags_at(plugin, commit):
        found.append(
            f"the plugin commit {commit} does not carry the release tag v{version}"
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-tree", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, default=BUNDLE)
    args = parser.parse_args(argv)
    found = failures(args.bundle, args.plugin_tree)
    for failure in found:
        print(f"check-release-bundle: {failure}", file=sys.stderr)
    if found:
        print(
            "check-release-bundle: this integration bundles a plugin that is not a release; "
            "bundle the plugin's release before tagging",
            file=sys.stderr,
        )
        return 1
    metadata = json.loads((args.bundle / "metadata.json").read_text(encoding="utf-8"))
    print(
        f"check-release-bundle: the bundle is the plugin's release v{metadata['version']} "
        f"({metadata['source_commit']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

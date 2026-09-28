"""The release workflow refuses to publish a bundle that is not a plugin release.

`main` may bundle a candidate of the plugin between releases - a development build - so that
it can be tested here. A tag cut from such a `main` would ship the candidate to every household,
so `release.yml` runs `tools/check-release-bundle.py` before it builds the zip. These tests are
that check's rehearsal: the committed candidate bundle is refused, a release build at its tag
passes, and each way of not being a release is refused on its own.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest
import yaml

from .ipk import buildinfo, make_ipk

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check-release-bundle.py"
COMMITTED_BUNDLE = ROOT / "custom_components/enigma2_mqtt/bundled"
PACKAGE = "enigma2-plugin-extensions-mqttbridge"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("check_release_bundle", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def plugin(tmp_path: Path) -> tuple[Path, str]:
    """A plugin repository with one commit and no tags."""
    repo = tmp_path / "plugin"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README").write_text("plugin\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "one")
    return repo, _git(repo, "rev-parse", "HEAD")


def _bundle(
    directory: Path, version: str, commit: str, build: str | None, **metadata: Any
) -> Path:
    """A bundle directory as the builder lays it out, around a package carrying `build`."""
    directory.mkdir()
    filename = f"{PACKAGE}_{version}_all.ipk"
    (directory / filename).write_bytes(make_ipk(build))
    values = {"filename": filename, "version": version, "source_commit": commit}
    values.update(metadata)
    (directory / "metadata.json").write_text(json.dumps(values), encoding="utf-8")
    return directory


def test_the_committed_candidate_bundle_is_refused(gate, plugin, monkeypatch) -> None:
    """The bundle on `main` today is a development build of an untagged plugin commit, and
    it stays refused even if somebody later tags that commit: the package says what it is."""
    repo, _ = plugin
    metadata = json.loads((COMMITTED_BUNDLE / "metadata.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(gate, "_tags_at", lambda plugin, commit: [])
    untagged = gate.failures(COMMITTED_BUNDLE, repo)
    monkeypatch.setattr(gate, "_tags_at", lambda plugin, commit: [f"v{metadata['version']}"])
    tagged = gate.failures(COMMITTED_BUNDLE, repo)

    assert any("flavour is 'development', not a release build" in failure for failure in untagged)
    assert any("does not carry the release tag" in failure for failure in untagged)
    assert tagged == ["the bundled package's build flavour is 'development', not a release build"]


def test_a_release_build_at_its_tag_passes(gate, plugin, tmp_path, capsys) -> None:
    repo, commit = plugin
    _git(repo, "tag", "v0.4.0")
    bundle = _bundle(tmp_path / "bundle", "0.4.0", commit, buildinfo(commit=commit))

    assert gate.failures(bundle, repo) == []
    assert gate.main(["--plugin-tree", str(repo), "--bundle", str(bundle)]) == 0
    assert "the bundle is the plugin's release v0.4.0" in capsys.readouterr().out


def test_an_annotated_release_tag_counts(gate, plugin, tmp_path) -> None:
    repo, commit = plugin
    _git(repo, "tag", "-a", "v0.4.0", "-m", "release")
    bundle = _bundle(tmp_path / "bundle", "0.4.0", commit, buildinfo(commit=commit))

    assert gate.failures(bundle, repo) == []


@pytest.mark.parametrize(
    ("tag", "build", "expected"),
    [
        ("v0.4.0", {"flavour": "development"}, "flavour is 'development', not a release build"),
        ("v0.4.0", {"flavour": "acceptance"}, "flavour is 'acceptance', not a release build"),
        ("v0.4.0", {"dirty": True}, "not built from a clean tree"),
        ("v0.4.0", {"commit": "d" * 40}, "not from the commit metadata.json names"),
        (None, {}, "does not carry the release tag v0.4.0"),
        ("v0.4.1", {}, "does not carry the release tag v0.4.0"),
        ("0.4.0", {}, "does not carry the release tag v0.4.0"),
        ("v0.4.0", None, "carries no build id"),
    ],
)
def test_anything_but_a_release_is_refused(
    gate, plugin, tmp_path, capsys, tag: str | None, build: dict | None, expected: str
) -> None:
    repo, commit = plugin
    if tag:
        _git(repo, "tag", tag)
    text = None if build is None else buildinfo(**{"commit": commit, **build})
    bundle = _bundle(tmp_path / "bundle", "0.4.0", commit, text)

    found = gate.failures(bundle, repo)

    assert any(expected in failure for failure in found), found
    assert gate.main(["--plugin-tree", str(repo), "--bundle", str(bundle)]) == 1
    assert "is not a release" in capsys.readouterr().err


def test_an_unreadable_build_id_is_refused(gate, plugin, tmp_path) -> None:
    repo, commit = plugin
    _git(repo, "tag", "v0.4.0")
    bundle = _bundle(tmp_path / "bundle", "0.4.0", commit, "COMMIT = (\n")

    assert any("cannot be read" in failure for failure in gate.failures(bundle, repo))


@pytest.mark.parametrize("tagged", [True, False])
def test_a_release_from_before_build_ids_is_judged_by_its_tag(
    gate, plugin, tmp_path, tagged: bool
) -> None:
    """Plugin 0.3.x packages carry no build id: the tag alone says whether it is a release."""
    repo, commit = plugin
    if tagged:
        _git(repo, "tag", "v0.3.0")
    bundle = _bundle(tmp_path / "bundle", "0.3.0", commit, None)

    assert (gate.failures(bundle, repo) == []) is tagged


def test_a_bundle_without_a_plain_version_or_commit_is_refused(gate, plugin, tmp_path) -> None:
    repo, commit = plugin
    assert gate.failures(
        _bundle(tmp_path / "one", "0.4.0-rc1", commit, buildinfo(commit=commit)), repo
    )
    assert gate.failures(
        _bundle(tmp_path / "two", "0.4.0", "HEAD", buildinfo(commit=commit)), repo
    )


def test_the_release_workflow_builds_the_zip_only_after_the_gate() -> None:
    """The gate is a job of its own, with the plugin's tags, and the publishing job needs it."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    gate_job = jobs["bundle-is-release"]
    needs = jobs["release"]["needs"]

    assert "bundle-is-release" in ([needs] if isinstance(needs, str) else needs)
    checkouts = [step for step in gate_job["steps"] if step.get("with", {}).get("repository")]
    assert checkouts == [
        {
            "uses": "actions/checkout@v4",
            "with": {
                "repository": "deltasystems-pl/enigma2-mqtt-bridge",
                "ref": "${{ steps.bundle.outputs.commit }}",
                "path": "plugin-source",
                "fetch-depth": 0,
            },
        }
    ]
    assert any(
        "tools/check-release-bundle.py --plugin-tree plugin-source" in step.get("run", "")
        for step in gate_job["steps"]
    )

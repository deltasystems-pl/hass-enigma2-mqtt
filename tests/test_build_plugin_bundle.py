"""The bundle builder tells the plugin's build what it is building.

The plugin's `build-ipk.sh` records a build id in every package - commit, time, flavour - and,
run from the `git archive` extraction this builder makes, it has no `.git` to read them from.
Without them the bundled package was a `development` build with an empty commit: not the bytes
the plugin's release published, and a version string that could not be told from any other
build. CI's byte-for-byte rebuild of the committed bundle is the end-to-end check; these are the
two decisions it rests on.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "build-plugin-bundle.py"


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("build_plugin_bundle", TOOL)
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
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "plugin"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README").write_text("plugin\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "one")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_a_commit_at_its_release_tag_is_a_release_build(builder, repository) -> None:
    repo, commit = repository
    _git(repo, "tag", "v1.2.3")
    assert builder.flavour_for(repo, commit, "1.2.3") == "release"


@pytest.mark.parametrize("tag", [None, "v1.2.4", "1.2.3", "v1.2.3-rc1"])
def test_any_other_commit_is_a_development_build(builder, repository, tag) -> None:
    """A candidate is bundled as what it is, never as the release it will become."""
    repo, commit = repository
    if tag:
        _git(repo, "tag", tag)
    assert builder.flavour_for(repo, commit, "1.2.3") == "development"


def test_the_build_is_told_its_commit_time_and_flavour(builder, monkeypatch) -> None:
    monkeypatch.setenv("MQTTBRIDGE_BUILD_ORIGIN", "https://example.invalid/feed/")
    monkeypatch.setenv("MQTTBRIDGE_BUILD_INDEX_KEYS", "[]")
    environment = builder.build_environment("c" * 40, "1790324861", "release")

    assert environment["MQTTBRIDGE_BUILD_COMMIT"] == "c" * 40
    assert environment["MQTTBRIDGE_BUILD_FLAVOUR"] == "release"
    assert environment["SOURCE_DATE_EPOCH"] == "1790324861"
    # A test origin or test keys are an acceptance build's; this builder never makes one.
    assert "MQTTBRIDGE_BUILD_ORIGIN" not in environment
    assert "MQTTBRIDGE_BUILD_INDEX_KEYS" not in environment

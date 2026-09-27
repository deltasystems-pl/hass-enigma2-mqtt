"""The bundled receiver package is local, bounded and cryptographically linked to source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tarfile

import pytest

from custom_components.enigma2_mqtt import bundle
from custom_components.enigma2_mqtt.const import SUPPORTED_PLUGIN_VERSION

from .ipk import buildinfo, make_ipk

COMMIT = "a" * 40
VERSION = "1.2.3"
PREFIX = f"enigma2-mqtt-bridge-{COMMIT}/"


def test_the_committed_bundle_validates():
    found = bundle.load_bundled_plugin()
    assert found.path.is_file()
    assert len(found.sha256) == 64
    assert len(found.source_commit) == 40


def test_the_bundle_is_the_version_this_release_expects():
    """The constant is what the update entity falls back on when the bundle will not load.

    If the two disagree, a receiver is offered a version the installer would then refuse,
    because `async_install` only accepts the version that is actually in the bundle. Pinning
    them to each other here means a bundle swap that forgets the constant fails in CI rather
    than on somebody's receiver.
    """
    assert bundle.load_bundled_plugin().version == SUPPORTED_PLUGIN_VERSION


def test_missing_metadata_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(bundle, "_METADATA", tmp_path / "missing.json")
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_unknown_metadata_field_fails_closed(monkeypatch, tmp_path):
    metadata = json.loads(bundle._METADATA.read_text(encoding="utf-8"))
    metadata["unexpected"] = "value"
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(bundle, "_METADATA", path)
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("filename", "../plugin.ipk"),
        ("filename", "enigma2-plugin-extensions-mqttbridge_9.9.9_all.ipk"),
        ("source_filename", "wrong-source.tar.gz"),
        ("source_url", "https://example.invalid/source"),
    ],
)
def test_metadata_identity_mismatches_fail_closed(monkeypatch, tmp_path, field, value):
    metadata = json.loads(bundle._METADATA.read_text(encoding="utf-8"))
    metadata[field] = value
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(bundle, "_METADATA", path)
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_a_corrupt_package_fails_closed(monkeypatch, tmp_path):
    metadata = json.loads(bundle._METADATA.read_text(encoding="utf-8"))
    package = tmp_path / metadata["filename"]
    package.write_bytes(b"not the package")
    source = tmp_path / metadata["source_filename"]
    source.write_bytes(b"source")
    metadata["size"] = package.stat().st_size
    metadata["source_size"] = source.stat().st_size
    metadata["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(bundle, "_BUNDLE_DIR", tmp_path)
    monkeypatch.setattr(bundle, "_METADATA", path)
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_a_symlinked_artifact_is_refused(monkeypatch, tmp_path):
    metadata = json.loads(bundle._METADATA.read_text(encoding="utf-8"))
    package = tmp_path / "real.ipk"
    package.write_bytes(b"package")
    (tmp_path / metadata["filename"]).symlink_to(package)
    source = tmp_path / metadata["source_filename"]
    source.write_bytes(b"source")
    metadata["size"] = package.stat().st_size
    metadata["source_size"] = source.stat().st_size
    metadata["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(bundle, "_BUNDLE_DIR", tmp_path)
    monkeypatch.setattr(bundle, "_METADATA", path)
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_oversized_metadata_is_rejected_before_parsing(monkeypatch, tmp_path):
    path = tmp_path / "metadata.json"
    path.write_bytes(b" " * (bundle._METADATA_LIMIT + 1))
    monkeypatch.setattr(bundle, "_METADATA", path)
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


# ------------------------------------------------- a bundle built here, then broken
#
# The tests above break the committed bundle's *metadata*, which is enough to prove the
# integration refuses it. They cannot reach the checks that read the source archive,
# because the committed archive is valid and there is no way to make it otherwise. So
# this half builds a whole bundle from scratch - a package, a source tarball and the
# metadata that binds them - and then breaks one thing at a time.


def _source_archive(path: Path, staging: Path, *, license_file: bool = True, extras=()) -> None:
    """Write a source tarball shaped the way the release workflow writes one."""
    staging.mkdir(exist_ok=True)
    payload = staging / "payload"
    payload.write_text("source", encoding="utf-8")
    with tarfile.open(path, "w:gz") as archive:
        if license_file:
            archive.add(payload, arcname=PREFIX + "LICENSE")
        archive.add(payload, arcname=PREFIX + "plugin.py")
        for arcname in extras:
            archive.add(payload, arcname=arcname)


def _install_bundle(monkeypatch, tmp_path: Path, **overrides) -> None:
    """Point the loader at a freshly built bundle, with fields optionally broken."""
    package = tmp_path / f"enigma2-plugin-extensions-mqttbridge_{VERSION}_all.ipk"
    package.write_bytes(overrides.pop("_package", make_ipk(overrides.pop("_buildinfo", None))))
    source = tmp_path / f"enigma2-mqtt-bridge-{COMMIT}.tar.gz"
    if "_raw_source" in overrides:
        source.write_bytes(overrides.pop("_raw_source"))
    else:
        _source_archive(source, tmp_path / "staging", **overrides.pop("_archive", {}))
    metadata = {
        "schema": 1,
        "filename": package.name,
        "version": VERSION,
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "size": package.stat().st_size,
        "source_filename": source.name,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_size": source.stat().st_size,
        "source_commit": COMMIT,
        "source_url": "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/tree/" + COMMIT,
        "license": "GPL-2.0-or-later",
    }
    metadata.update(overrides)
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(bundle, "_BUNDLE_DIR", tmp_path)
    monkeypatch.setattr(bundle, "_METADATA", path)


def test_a_bundle_built_to_the_contract_loads(monkeypatch, tmp_path):
    """Without this the rest of the file could pass by refusing everything."""
    _install_bundle(monkeypatch, tmp_path)
    found = bundle.load_bundled_plugin()
    assert found.version == VERSION
    assert found.source_commit == COMMIT
    # A plugin from before build ids carries none, and that is not a fault.
    assert found.build is None


def test_the_package_s_own_build_id_is_read(monkeypatch, tmp_path):
    """The build id comes out of the package, which is what the receiver will report."""
    _install_bundle(
        monkeypatch, tmp_path, _buildinfo=buildinfo(commit=COMMIT, flavour="release")
    )
    found = bundle.load_bundled_plugin()
    assert found.build == {
        "commit": COMMIT,
        "time": 1790400000,
        "dirty": False,
        "flavour": "release",
    }


def test_a_package_built_from_another_commit_is_refused(monkeypatch, tmp_path):
    """A package that names another commit than its source archive is not this bundle."""
    _install_bundle(monkeypatch, tmp_path, _buildinfo=buildinfo(commit="d" * 40))
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


@pytest.mark.parametrize(
    "text",
    [
        "COMMIT = open('/etc/passwd').read()\n",
        'COMMIT = "' + "c" * 40 + '"\n',
        "this is not python",
    ],
    ids=["not a literal", "members missing", "not python"],
)
def test_a_build_id_that_is_not_one_is_refused(monkeypatch, tmp_path, text):
    """A `buildinfo.py` the plugin's builder would never write is a package it did not make."""
    _install_bundle(monkeypatch, tmp_path, _buildinfo=text)
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_a_package_that_is_not_an_ipk_is_refused(monkeypatch, tmp_path):
    """Checksums that match prove the bytes are the ones described, not that they install."""
    _install_bundle(monkeypatch, tmp_path, _package=b"a plausible ipk")
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


@pytest.mark.parametrize(
    "override",
    [
        {"version": "not-a-version"},
        {"sha256": "not-a-digest"},
        {"source_sha256": "not-a-digest"},
        {"source_commit": "not-a-commit"},
        {"license": "MIT"},
        {"size": True},
        {"size": 0},
    ],
)
def test_a_field_that_does_not_look_right_is_refused(monkeypatch, tmp_path, override):
    """Every one of these would be a supply-chain question nobody can answer later."""
    _install_bundle(monkeypatch, tmp_path, **override)
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_a_source_archive_without_its_licence_is_refused(monkeypatch, tmp_path):
    """The IPK is GPL, so the corresponding source is an obligation, not a nicety."""
    _install_bundle(monkeypatch, tmp_path, _archive={"license_file": False})
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_a_source_archive_reaching_outside_its_own_tree_is_refused(monkeypatch, tmp_path):
    """An archive that writes outside its prefix is the oldest tar trick there is."""
    _install_bundle(monkeypatch, tmp_path, _archive={"extras": ("../escaped",)})
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_an_empty_source_archive_is_refused(monkeypatch, tmp_path):
    empty = tmp_path / f"enigma2-mqtt-bridge-{COMMIT}.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    _install_bundle(monkeypatch, tmp_path, _raw_source=empty.read_bytes())
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_a_source_archive_that_expands_too_far_is_refused(monkeypatch, tmp_path):
    """A few kilobytes that become a few gigabytes is a denial of service, not a bundle."""
    _install_bundle(monkeypatch, tmp_path)
    monkeypatch.setattr(bundle, "_SOURCE_EXPANDED_LIMIT", 1)
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_a_source_that_is_not_an_archive_at_all_is_refused(monkeypatch, tmp_path):
    _install_bundle(monkeypatch, tmp_path, _raw_source=b"this is not a tarball")
    with pytest.raises(bundle.BundleError, match="missing or invalid"):
        bundle.load_bundled_plugin()


def test_a_named_artifact_must_be_a_plain_file_in_the_bundle_directory(monkeypatch, tmp_path):
    """Directly, because reaching these through the loader needs metadata that already fails."""
    monkeypatch.setattr(bundle, "_BUNDLE_DIR", tmp_path)
    for name in (None, "", "sub/plugin.ipk", "../plugin.ipk"):
        with pytest.raises(bundle.BundleError):
            bundle._regular_child(name, 1024, 7)
    with pytest.raises(bundle.BundleError):
        bundle._regular_child("absent.ipk", 1024, 7)
    (tmp_path / "present.ipk").write_bytes(b"1234567")
    with pytest.raises(bundle.BundleError):
        bundle._regular_child("present.ipk", 1024, True)
    with pytest.raises(bundle.BundleError):
        bundle._regular_child("present.ipk", 1024, 8)
    assert bundle._regular_child("present.ipk", 1024, 7).name == "present.ipk"


def test_a_digest_stops_reading_at_the_limit_and_survives_an_unreadable_file(tmp_path):
    """The limit is what keeps a swapped artifact from being read into memory first."""
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"x" * 1024)
    assert bundle._digest(artifact, 1024) == hashlib.sha256(b"x" * 1024).hexdigest()
    with pytest.raises(bundle.BundleError):
        bundle._digest(artifact, 16)
    with pytest.raises(bundle.BundleError):
        bundle._digest(tmp_path, 1024)

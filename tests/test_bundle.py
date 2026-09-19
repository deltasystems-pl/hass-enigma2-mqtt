"""The bundled receiver package is local, bounded and cryptographically linked to source."""

from __future__ import annotations

import hashlib
import json

import pytest

from custom_components.enigma2_mqtt import bundle
from custom_components.enigma2_mqtt.const import SUPPORTED_PLUGIN_VERSION


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

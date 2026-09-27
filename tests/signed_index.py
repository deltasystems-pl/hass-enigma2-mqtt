"""Signed release indexes for tests, made with the throwaway keys of the shared vectors.

The integration trusts only the release keys, and nobody outside the plugin repository's signing
job holds their private halves - nor should a test. So the tests that go through Home Assistant
replace the embedded key set with the vectors' public test keys `t1` (rank 1) and `t2` (rank 2),
whose seeds the vectors publish because nothing trusts them, and sign with those.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custom_components.enigma2_mqtt import release_index

VECTORS_PATH = Path(__file__).parent / "vectors" / "release-index.json"
VECTORS: dict[str, Any] = json.loads(VECTORS_PATH.read_text("ascii"))

PACKAGE = "enigma2-plugin-extensions-mqttbridge"


def keyset(name: str = "test") -> tuple[release_index.Key, ...]:
    """One of the vectors' key sets, as `release_index` holds keys."""
    return release_index.keys_from_data(VECTORS["keysets"][name]["keys"])


def _seed(key_id: str) -> bytes:
    for key in VECTORS["test_keys"].values():
        if key["key_id"] == key_id:
            return bytes.fromhex(key["seed"])
    raise KeyError(key_id)


def commit_of(version: str) -> str:
    """A made-up commit id for a release, the same every time it is asked for."""
    return hashlib.sha1(version.encode("ascii")).hexdigest()


def commit_time_of(version: str) -> int:
    """A made-up commit time that grows with the version."""
    major, minor, patch = (int(part) for part in version.split("."))
    return 1790000000 + major * 1_000_000 + minor * 1_000 + patch


def release(version: str, **changes: Any) -> dict[str, Any]:
    """One release entry, shaped the way `tools/make-index.py` writes it."""
    entry = {
        "version": version,
        "filename": f"{PACKAGE}_{version}_all.ipk",
        "size": 300000,
        "sha256": "ab" * 32,
        "commit": commit_of(version),
        "commit_time": commit_time_of(version),
        "contract": 1,
        "min_integration": None,
        "depends": ["python3-core"],
        "self_update": False,
        "withdrawn": None,
    }
    entry.update(changes)
    return entry


def sign(
    serial: int,
    releases: list[dict[str, Any]],
    *,
    key: str = "t1",
    floor: str = "0.2.0",
    issued: int = 1790500000,
    named_key: str | None = None,
) -> tuple[bytes, bytes]:
    """`(releases.json, releases.json.sig)` signed with test key `t1` or `t2`."""
    key_id = VECTORS["test_keys"][key]["key_id"]
    index = {
        "schema": 1,
        "package": PACKAGE,
        "serial": serial,
        "issued": issued,
        "key_id": named_key or key_id,
        "floor": floor,
        "releases": releases,
    }
    raw = (json.dumps(index, indent=2) + "\n").encode("utf-8")
    signature = Ed25519PrivateKey.from_private_bytes(_seed(key_id)).sign(raw)
    sig = {
        "key_id": key_id,
        "algorithm": "ed25519",
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return raw, (json.dumps(sig) + "\n").encode("ascii")

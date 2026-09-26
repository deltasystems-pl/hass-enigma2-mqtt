"""The signed release index's rule, held to the plugin's own vectors.

The receiver and Home Assistant read one signed list with one rule, implemented once per
program. `tests/vectors/release-index.json` is the plugin's definition of that rule - signed
indexes in sequence, the key set the reader holds and the verdict it must reach - copied byte for
byte from the plugin commit `tests/vectors/SOURCE.json` names; CI compares the copy with the
plugin repository (`tools/check-plugin-shared.py`). A verdict that differed here would be an
index one side installs from and the other refuses.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from custom_components.enigma2_mqtt import release_index
from custom_components.enigma2_mqtt.const import (
    PLUGIN_CONTRACT,
    PLUGIN_CONTRACT_EXCEPTIONS,
    PLUGIN_INDEX_KEYS,
    PLUGIN_INDEX_ORIGIN,
    PLUGIN_MIN_VERSION,
)

from .signed_index import VECTORS, VECTORS_PATH, release

SOURCE = json.loads((VECTORS_PATH.parent / "SOURCE.json").read_text("ascii"))
SCENARIOS = VECTORS["scenarios"]


def _keysets() -> dict[str, tuple[release_index.Key, ...]]:
    return {
        name: release_index.keys_from_data(item["keys"])
        for name, item in VECTORS["keysets"].items()
    }


# ------------------------------------------------------------------------- the copy --


def test_the_vectors_are_the_plugin_s_at_the_pinned_commit() -> None:
    """The copy is the file SOURCE.json records; CI compares it with the plugin's own."""
    assert SOURCE["repository"] == "deltasystems-pl/enigma2-mqtt-bridge"
    assert len(SOURCE["commit"]) == 40
    assert hashlib.sha256(VECTORS_PATH.read_bytes()).hexdigest() == SOURCE["sha256"]


# ---------------------------------------------------------------- the release keys --


def test_the_release_keys_are_pinned() -> None:
    """Changing one of these is a key rotation: a release of both halves, never an edit."""
    assert [
        (key.key_id, key.rank, base64.b64encode(key.public).decode(), key.baseline)
        for key in release_index.EMBEDDED
    ] == [
        ("5de3b24c97e88660", 1, "39Ndn8vAkeWAhYIYWvNubezKuF5F/5aY5E1uo+hSnqQ=", 0),
        ("c72fd83e3e514a25", 2, "1F2ajhsDoTuqAGdV2QOHdRl8hV4B0kNE0xwVGrpHdfs=", 0),
    ]


def test_a_key_id_is_the_first_16_hex_digits_of_the_public_key_s_sha256() -> None:
    for key in release_index.EMBEDDED:
        assert key.key_id == hashlib.sha256(key.public).hexdigest()[:16]
        assert key.key_id == release_index.key_id(key.public)


def test_the_keys_and_ranks_are_the_plugin_s() -> None:
    """The vectors carry the plugin's embedded set; this integration's must be the same."""
    assert VECTORS["release_keys"]["keys"] == list(PLUGIN_INDEX_KEYS)
    assert release_index.keys_to_data(release_index.EMBEDDED) == list(PLUGIN_INDEX_KEYS)
    assert (
        release_index.fingerprint(release_index.EMBEDDED)
        == VECTORS["release_keys"]["fingerprint"]
    )


def test_the_fingerprint_does_not_depend_on_order() -> None:
    keys = release_index.EMBEDDED
    assert release_index.fingerprint(tuple(reversed(keys))) == release_index.fingerprint(keys)
    assert release_index.fingerprint(keys[:1]) != release_index.fingerprint(keys)


def test_the_origin_is_the_plugin_s_published_feed() -> None:
    assert PLUGIN_INDEX_ORIGIN == "https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/"


# ---------------------------------------------------------------------- the rule --


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario["name"] for scenario in SCENARIOS])
def test_every_shared_scenario(scenario: dict) -> None:
    """Each step is judged as a reader that stores its state after every acceptance.

    The state goes through JSON between steps, as it does through Home Assistant's storage.
    """
    sets = _keysets()
    state = release_index.empty_state()
    for number, step in enumerate(scenario["steps"], 1):
        keys = sets[step["keyset"]]
        acceptance = step["lineage"] == "acceptance"
        memory = release_index.memory_for(
            json.loads(json.dumps(state)), keys, acceptance=acceptance
        )
        try:
            accepted = release_index.accept(
                base64.b64decode(step["index"]), base64.b64decode(step["sig"]), keys, memory
            )
        except release_index.Refused as error:
            verdict = error.reason
        else:
            verdict = "accept"
            state = release_index.store(state, keys, accepted.memory, acceptance=acceptance)
        assert verdict == step["expect"], f"step {number}: {step['note']}"


def test_every_reason_is_the_plugin_s_and_is_exercised() -> None:
    seen = {step["expect"] for scenario in SCENARIOS for step in scenario["steps"]}
    assert seen == {"accept", *release_index.REASONS}
    assert VECTORS["reasons"] == list(release_index.REASONS)
    assert VECTORS["max_jump"] == release_index.MAX_JUMP
    assert VECTORS["max_index_bytes"] == release_index.MAX_INDEX_BYTES


@pytest.mark.parametrize(
    "case", VECTORS["signatures"], ids=[case["name"] for case in VECTORS["signatures"]]
)
def test_the_verifier_agrees_with_the_plugin_s(case: dict) -> None:
    """OpenSSL through `cryptography` gives the plugin's verdict on every forgery it names."""
    assert (
        release_index.verify(
            bytes.fromhex(case["public"]),
            bytes.fromhex(case["message"]),
            bytes.fromhex(case["signature"]),
        )
        is case["valid"]
    )


def test_a_public_key_that_is_not_canonical_is_refused_before_openssl_sees_it() -> None:
    """The one check made here rather than by OpenSSL, because OpenSSL is laxer about it.

    `y` is the field prime itself - the same point as `y = 0`, spelled a second way. The
    plugin's verifier refuses such a key, and so must this one, whatever the signature.
    """
    prime = 2**255 - 19
    spelled_twice = prime.to_bytes(32, "little")
    case = VECTORS["signatures"][0]
    assert not release_index.verify(
        spelled_twice, bytes.fromhex(case["message"]), bytes.fromhex(case["signature"])
    )
    with pytest.raises(release_index.Refused, match="not an Ed25519 public key"):
        release_index.check_keys(
            [
                release_index.Key(
                    release_index.key_id(spelled_twice), 1, spelled_twice, 0
                )
            ]
        )


def test_the_vectors_key_sets_derive_their_ids_and_fingerprints() -> None:
    for name, item in VECTORS["keysets"].items():
        keys = release_index.keys_from_data(item["keys"])
        assert release_index.fingerprint(keys) == item["fingerprint"], name
    for key in VECTORS["test_keys"].values():
        assert release_index.key_id(base64.b64decode(key["public"])) == key["key_id"]


@pytest.mark.parametrize("case", VECTORS["memories"], ids=[c["note"] for c in VECTORS["memories"]])
def test_a_stored_memory_is_loaded_as_it_is_or_refused(case: dict) -> None:
    if case["valid"]:
        assert release_index.check_memory(case["memory"]) == case["memory"]
    else:
        with pytest.raises(release_index.BadMemory):
            release_index.check_memory(case["memory"])


@pytest.mark.parametrize("case", VECTORS["states"], ids=[c["note"] for c in VECTORS["states"]])
def test_a_stored_state_is_loaded_as_it_is_or_refused(case: dict) -> None:
    if case["valid"]:
        assert release_index.check_state(case["state"]) == case["state"]
    else:
        with pytest.raises(release_index.BadMemory):
            release_index.check_state(case["state"])


def test_accepting_does_not_change_the_memory_it_was_given() -> None:
    step = SCENARIOS[0]["steps"][0]
    other = "0" * 16
    memory = {"serials": {other: 3}, "silenced": []}
    accepted = release_index.accept(
        base64.b64decode(step["index"]),
        base64.b64decode(step["sig"]),
        _keysets()["test"],
        memory,
    )
    assert memory == {"serials": {other: 3}, "silenced": []}
    assert accepted.memory == {"serials": {other: 3, accepted.key.key_id: 1}, "silenced": []}


def test_which_part_of_the_state_is_meant_is_never_a_default() -> None:
    keys = _keysets()["test"]
    with pytest.raises(TypeError):
        release_index.memory_for(None, keys, acceptance=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        release_index.store(None, keys, None, acceptance="release")  # type: ignore[arg-type]


def test_a_later_build_s_members_survive_a_store_by_this_one() -> None:
    """A downgrade and a return lose nothing a later build wrote."""
    keys = _keysets()["test"]
    name = release_index.fingerprint(keys)
    state = {
        "schema": 7,
        "future": {"x": 1},
        "release": {
            name: {"keys": [keys[0].key_id], "serials": {}, "silenced": [], "extra": True}
        },
        "acceptance": {},
    }
    stored = release_index.store(
        state, keys, {"serials": {keys[0].key_id: 2}, "silenced": []}, acceptance=False
    )
    assert stored["schema"] == 7
    assert stored["future"] == {"x": 1}
    assert stored["release"][name]["extra"] is True
    assert stored["release"][name]["serials"] == {keys[0].key_id: 2}
    assert stored["acceptance"] == {}


# ------------------------------------------------------------------ compatibility --


def _index(*releases: dict, floor: str = "0.2.0") -> dict:
    return {"floor": floor, "releases": list(releases)}


@pytest.mark.parametrize(
    ("entry", "integration", "reason"),
    [
        (release("0.4.0"), "0.4.0", None),
        # There is no upper bound inside a major.
        (release("9.0.0"), "0.4.0", None),
        (release("0.4.0", contract=2), "0.4.0", "contract"),
        (release("0.1.0", contract=0), "0.4.0", "contract"),
        (release("0.4.0", withdrawn="broken"), "0.4.0", "withdrawn"),
        (release("0.4.0", min_integration="0.4.1"), "0.4.0", "needs_integration"),
        (release("0.4.0", min_integration="0.4.0"), "0.4.0", None),
        # An integration that cannot say its own version meets no stated minimum.
        (release("0.4.0", min_integration="0.4.0"), None, "needs_integration"),
        (release("0.1.9"), "0.4.0", "below_floor"),
    ],
)
def test_the_compatibility_rule(entry: dict, integration: str | None, reason: str | None) -> None:
    assert (
        release_index.incompatibility(
            entry, contract=PLUGIN_CONTRACT, floor="0.2.0", integration_version=integration
        )
        == reason
    )


def test_below_the_floor_is_never_offered_and_never_listed() -> None:
    """The higher of this integration's floor and the index's decides."""
    listed = release_index.available(
        _index(release("0.2.0"), release("0.3.0"), release("0.4.0"), floor="0.3.0"),
        contract=PLUGIN_CONTRACT,
        own_floor=PLUGIN_MIN_VERSION,
        integration_version="0.4.0",
    )
    assert [item["version"] for item in listed] == ["0.4.0", "0.3.0"]
    listed = release_index.available(
        _index(release("0.1.0", contract=0), release("0.2.0"), floor="0.1.0"),
        contract=PLUGIN_CONTRACT,
        own_floor=PLUGIN_MIN_VERSION,
        integration_version="0.4.0",
    )
    assert [item["version"] for item in listed] == ["0.2.0"]


def test_withdrawn_releases_are_never_listed() -> None:
    listed = release_index.available(
        _index(release("0.4.0", withdrawn="it broke"), release("0.3.0")),
        contract=PLUGIN_CONTRACT,
        own_floor=PLUGIN_MIN_VERSION,
        integration_version="0.4.0",
    )
    assert listed == [{"version": "0.3.0", "compatible": True, "reason": None}]


def test_the_list_is_newest_first_with_a_reason_for_what_is_not_offered() -> None:
    listed = release_index.available(
        _index(
            release("0.3.0"),
            release("0.10.0", contract=2),
            release("0.4.0", min_integration="9.0.0"),
        ),
        contract=PLUGIN_CONTRACT,
        own_floor=PLUGIN_MIN_VERSION,
        integration_version="0.4.0",
    )
    assert listed == [
        {"version": "0.10.0", "compatible": False, "reason": "contract"},
        {"version": "0.4.0", "compatible": False, "reason": "needs_integration"},
        {"version": "0.3.0", "compatible": True, "reason": None},
    ]


def test_the_list_holds_the_twenty_newest() -> None:
    listed = release_index.available(
        _index(*(release(f"0.{minor}.0") for minor in range(2, 40))),
        contract=PLUGIN_CONTRACT,
        own_floor=PLUGIN_MIN_VERSION,
        integration_version="0.4.0",
    )
    assert len(listed) == release_index.AVAILABLE_LIMIT
    assert listed[0]["version"] == "0.39.0"


def test_the_contract_and_its_named_exceptions_are_pinned() -> None:
    """CI holds these to the plugin's contract.json; a new exception is read here first."""
    assert PLUGIN_CONTRACT == 1
    assert PLUGIN_MIN_VERSION == "0.2.0"
    assert PLUGIN_CONTRACT_EXCEPTIONS == (
        "zap-moves-channel-list",
        "epg-grid-generated-means-changed",
        "timers-lists-finished",
        "zap-under-popup-recorded",
    )

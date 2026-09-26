"""The plugin's signed release index: its format, its keys, and the one rule for accepting it.

The receiver plugin will only ever be offered, and installed, as a release named in **one signed
list**: `releases.json` with its signature `releases.json.sig`, published next to the plugin's opkg
feed ([ADR-0008](../../docs/adr/0008-signed-plugin-index.md); the format and the publication chain
are the plugin repository's `docs/RELEASE-INDEX.md`). The receiver reads the same list with the
same rule, implemented once per program: two readers that disagreed about which index is valid
would each install something the other refuses. So this module is a port, member for member and
reason for reason, of the plugin's `trust.py`, and `tests/test_release_index.py` holds it to the
plugin's own vectors - a byte-identical copy of `tests/vectors/release-index.json` from a pinned
plugin commit, which CI compares with the plugin repository.

**Accepting an index**, in this order, each refusal a reason code the vectors name:

1. both files within their size caps (`too_large`), and the signature file well formed
   (`malformed_signature`);
2. the signature file's `key_id` names an embedded key (`unknown_key`);
3. the signature verifies over the index's exact bytes (`bad_signature`) - before anything in the
   index is believed;
4. the index is well formed (`malformed_index`), and names the key that signed it
   (`key_mismatch`);
5. the key has not been silenced, and its rank is not below the highest rank this reader has
   accepted under its embedded keys (`rank`);
6. the serial is above the last one accepted **for that key** (`replay`) and at most 1000 above it
   (`jump`); for a key never accepted before, within 1000 of the key's embedded baseline
   (`first_sight`).

**The one difference from the plugin is the verifier.** The receiver may have no cryptography
library, so the plugin carries a pure-Python Ed25519 verifier. Home Assistant ships `cryptography`,
which verifies with OpenSSL, and OpenSSL accepts exactly the signatures the plugin's verifier
accepts for every well-formed public key - the vectors carry the forgeries that tell verifiers
apart (`S >= L`, a non-canonical `R`, `(R, L - S)`, a shared `x`, a torsion key). The plugin is
stricter than OpenSSL on a public key that is not a canonical point encoding, so that one check is
made here in Python before OpenSSL is asked; no embedded key set contains such a key either way.

**Memory and state.** What a reader has learned - the last serial per key, and the keys it has
silenced - is kept in one stored state both programs write the same way, with a `release` and an
`acceptance` part, each keyed by the fingerprint of the key set that wrote it. This integration
has no acceptance build of its own (a lab-only tool replaces the keys and the storage key, and is
never committed), so it always reads and writes the `release` part; the other is kept untouched.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Sequence
import hashlib
import json
import re
from typing import Any, NamedTuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .const import PLUGIN_INDEX_KEYS

PACKAGE = "enigma2-plugin-extensions-mqttbridge"
SCHEMA = 1
ALGORITHM = "ed25519"

MAX_INDEX_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 1024
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_JUMP = 1000

PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64

# A release is a plain N.N.N: ASCII digits, no leading zeros, matched whole - so opkg, PEP 440 and
# Home Assistant order every pair the same way, and neither a Unicode digit nor a trailing newline
# slips through.
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)
_KEY_ID = re.compile(r"[0-9a-f]{16}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_DEPENDENCY = re.compile(r"[a-z0-9][a-z0-9+.-]*", re.ASCII)

REASONS = (
    "too_large",
    "malformed_signature",
    "unknown_key",
    "bad_signature",
    "malformed_index",
    "key_mismatch",
    "rank",
    "replay",
    "jump",
    "first_sight",
)

# The curve's field prime and group order, for the two checks OpenSSL is not asked to make the
# plugin's way: a canonical public key, and (belt and braces) `S` below the order.
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


class Key(NamedTuple):
    """One embedded public key, with its rank and the baseline serial of its first sight."""

    key_id: str
    rank: int
    public: bytes
    baseline: int


class Refused(Exception):
    """An index, a signature file or a key set that is not accepted, with the reason code."""

    def __init__(self, reason: str, detail: str) -> None:
        """Keep the reason code apart from the sentence, which is for a log."""
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class BadMemory(Exception):
    """A stored state whose known parts are not what every reader writes.

    Never read as "nothing remembered": that would put the reader back at first sight with no key
    silenced - the one direction a damaged file must not move it. A reader that meets this judges
    no index and says so. Recovery is removing the store on purpose, which forgets which keys were
    silenced until the published index is accepted again.
    """


class Accepted(NamedTuple):
    """An accepted index, the key that signed it, and the memory to store after it."""

    index: dict[str, Any]
    key: Key
    memory: dict[str, Any]


def key_id(public: bytes) -> str:
    """The id of a raw 32-byte public key: the first 16 hex digits of its sha256."""
    return hashlib.sha256(bytes(public)).hexdigest()[:16]


def _whole(value: Any) -> bool:
    """An int that is not a bool: JSON's `true` is not a serial."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_version(value: Any) -> bool:
    """Whether `value` is a plain `N.N.N`."""
    return isinstance(value, str) and _VERSION.fullmatch(value) is not None


def version_key(value: str) -> tuple[int, ...]:
    """A release number as a tuple that sorts the way every tool sorts it."""
    return tuple(int(part) for part in value.split("."))


# ------------------------------------------------------------------------- the verifier --


def _canonical_point(encoded: bytes) -> bool:
    """Whether 32 bytes are the canonical encoding of a point on the curve.

    The plugin's verifier refuses any other public key; OpenSSL is laxer about a `y` at or above
    the field prime and a set sign bit on `x = 0`. Asking this first makes the two readers agree
    on every input, not only on the keys anybody embeds.
    """
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return False
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return not sign
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    return (x * x - x2) % _P == 0


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` is `public`'s Ed25519 signature over `message`."""
    if not all(isinstance(value, (bytes, bytearray)) for value in (public, message, signature)):
        return False
    if len(public) != PUBLIC_KEY_BYTES or len(signature) != SIGNATURE_BYTES:
        return False
    if not _canonical_point(bytes(public)):
        return False
    if int.from_bytes(bytes(signature[32:]), "little") >= _L:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes(public)).verify(
            bytes(signature), bytes(message)
        )
    except (InvalidSignature, ValueError):
        return False
    return True


# ------------------------------------------------------------------------------ key sets --


def check_keys(keys: Iterable[Key]) -> tuple[Key, ...]:
    """`keys` ordered by rank, or Refused("malformed_index") saying what is wrong."""
    keys = tuple(keys)
    if not keys:
        raise Refused("malformed_index", "a key set needs at least one key")
    for key in keys:
        if not isinstance(key, Key) or not isinstance(key.public, bytes):
            raise Refused("malformed_index", f"not a key: {key!r}")
        if len(key.public) != PUBLIC_KEY_BYTES or not _canonical_point(key.public):
            raise Refused("malformed_index", f"key {key.key_id}: not an Ed25519 public key")
        if key.key_id != key_id(key.public):
            raise Refused("malformed_index", f"key {key.key_id}: its id is not derived from it")
        if not _whole(key.rank) or key.rank < 1:
            raise Refused("malformed_index", f"key {key.key_id}: rank must be a whole number >= 1")
        if not _whole(key.baseline) or key.baseline < 0:
            raise Refused("malformed_index", f"key {key.key_id}: baseline must be >= 0")
    if len({key.key_id for key in keys}) != len(keys):
        raise Refused("malformed_index", "two keys share an id")
    if len({key.rank for key in keys}) != len(keys):
        raise Refused("malformed_index", "two keys share a rank")
    return tuple(sorted(keys, key=lambda key: key.rank))


def keys_from_data(items: Iterable[dict[str, Any]]) -> tuple[Key, ...]:
    """A key set from its data form, `[{"key_id", "rank", "public", "baseline"}]`."""
    try:
        keys = [
            Key(
                item["key_id"],
                item["rank"],
                base64.b64decode(item["public"], validate=True),
                item["baseline"],
            )
            for item in items
        ]
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise Refused("malformed_index", f"not a key set: {error}") from error
    return check_keys(keys)


def keys_to_data(keys: Iterable[Key]) -> list[dict[str, Any]]:
    """The data form of a key set, as the vectors and the plugin's tools write it."""
    return [
        {
            "key_id": key.key_id,
            "rank": key.rank,
            "public": base64.b64encode(key.public).decode("ascii"),
            "baseline": key.baseline,
        }
        for key in keys
    ]


def fingerprint(keys: Iterable[Key]) -> str:
    """sha256 over the set's `key_id rank public` lines, sorted by `key_id`: a name for the set."""
    lines = "".join(
        f"{key.key_id} {key.rank} {base64.b64encode(key.public).decode('ascii')}\n"
        for key in sorted(keys, key=lambda key: key.key_id)
    )
    return hashlib.sha256(lines.encode("ascii")).hexdigest()


def by_id(keys: Iterable[Key], wanted: str) -> Key | None:
    """The key of `keys` with this id, or None."""
    for key in keys:
        if key.key_id == wanted:
            return key
    return None


# The embedded keys, from `const.py`. Checked when the module loads: a typo in a key must stop the
# reader, not make it trust nothing - or something else.
EMBEDDED: tuple[Key, ...] = keys_from_data(PLUGIN_INDEX_KEYS)


# ------------------------------------------------------------------------------- parsing --


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # Python keeps the last of two equal keys and other parsers may keep the first; an index that
    # means two things to two readers is refused by both.
    seen: dict[str, Any] = {}
    for name, value in pairs:
        if name in seen:
            raise ValueError(f"duplicate key {name!r}")
        seen[name] = value
    return seen


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _json(raw: bytes, reason: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise Refused(reason, f"not JSON: {error}") from error


def parse_signature(raw: Any) -> tuple[str, bytes]:
    """`(key_id, 64 signature bytes)` from a `releases.json.sig`, or Refused."""
    if not isinstance(raw, (bytes, bytearray)):
        raise Refused("malformed_signature", "the signature file is not bytes")
    if len(raw) > MAX_SIGNATURE_BYTES:
        raise Refused("too_large", f"the signature file is over {MAX_SIGNATURE_BYTES} bytes")
    data = _json(bytes(raw), "malformed_signature")
    if not isinstance(data, dict):
        raise Refused("malformed_signature", "the signature file is not an object")
    if data.get("algorithm") != ALGORITHM:
        raise Refused("malformed_signature", f"the algorithm is not {ALGORITHM}")
    wanted = data.get("key_id")
    if not isinstance(wanted, str) or not _KEY_ID.fullmatch(wanted):
        raise Refused("malformed_signature", "the key_id is not 16 lowercase hex digits")
    encoded = data.get("signature")
    try:
        signature = (
            base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
        )
    except (binascii.Error, ValueError):
        signature = b""
    if len(signature) != SIGNATURE_BYTES:
        raise Refused("malformed_signature", "the signature is not 64 bytes of base64")
    return wanted, signature


_TOP = ("schema", "package", "serial", "issued", "key_id", "floor", "releases")
_RELEASE = (
    "version",
    "filename",
    "size",
    "sha256",
    "commit",
    "commit_time",
    "contract",
    "min_integration",
    "depends",
    "self_update",
    "withdrawn",
)


def _bad(detail: str) -> Refused:
    return Refused("malformed_index", detail)


def _check_release(entry: Any, position: int) -> str:
    where = f"releases[{position}]"
    if not isinstance(entry, dict):
        raise _bad(f"{where} is not an object")
    missing = [name for name in _RELEASE if name not in entry]
    if missing:
        raise _bad(f"{where} lacks {', '.join(missing)}")
    version = entry["version"]
    if not is_version(version):
        raise _bad(f"{where}: version {version!r} is not a plain N.N.N")
    where = f"release {version}"
    if entry["filename"] != f"{PACKAGE}_{version}_all.ipk":
        raise _bad(f"{where}: filename is not the package's own")
    if not _whole(entry["size"]) or not 0 < entry["size"] <= MAX_PACKAGE_BYTES:
        raise _bad(f"{where}: size must be 1 to {MAX_PACKAGE_BYTES} bytes")
    if not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]):
        raise _bad(f"{where}: sha256 is not 64 lowercase hex digits")
    if not isinstance(entry["commit"], str) or not _COMMIT.fullmatch(entry["commit"]):
        raise _bad(f"{where}: commit is not 40 lowercase hex digits")
    if not _whole(entry["commit_time"]) or entry["commit_time"] <= 0:
        raise _bad(f"{where}: commit_time must be a positive whole number")
    if not _whole(entry["contract"]) or entry["contract"] < 0:
        raise _bad(f"{where}: contract must be a whole number >= 0")
    if entry["min_integration"] is not None and not is_version(entry["min_integration"]):
        raise _bad(f"{where}: min_integration is neither null nor N.N.N")
    depends = entry["depends"]
    if not isinstance(depends, list) or not all(
        isinstance(name, str) and _DEPENDENCY.fullmatch(name) for name in depends
    ):
        raise _bad(f"{where}: depends is not a list of package names")
    if not isinstance(entry["self_update"], bool):
        raise _bad(f"{where}: self_update is not true or false")
    withdrawn = entry["withdrawn"]
    if withdrawn is not None and (not isinstance(withdrawn, str) or not withdrawn.strip()):
        raise _bad(f"{where}: withdrawn is neither null nor a reason")
    return version


def parse_index(raw: Any) -> dict[str, Any]:
    """The index in `raw` - the exact bytes that were signed - checked field by field.

    Tolerant of members it does not know, at the top and in each release, as every reader is: a
    later index may add one, and a reader that refused it could never be updated to understand it.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise _bad("the index is not bytes")
    if len(raw) > MAX_INDEX_BYTES:
        raise Refused("too_large", f"the index is over {MAX_INDEX_BYTES} bytes")
    data = _json(bytes(raw), "malformed_index")
    if not isinstance(data, dict):
        raise _bad("the index is not an object")
    missing = [name for name in _TOP if name not in data]
    if missing:
        raise _bad(f"the index lacks {', '.join(missing)}")
    if not _whole(data["schema"]) or data["schema"] != SCHEMA:
        raise _bad(f"schema is not {SCHEMA}")
    if data["package"] != PACKAGE:
        raise _bad(f"package is not {PACKAGE}")
    if not _whole(data["serial"]) or data["serial"] < 1:
        raise _bad("serial must be a whole number >= 1")
    if not _whole(data["issued"]) or data["issued"] < 0:
        raise _bad("issued must be a whole number >= 0")
    if not isinstance(data["key_id"], str) or not _KEY_ID.fullmatch(data["key_id"]):
        raise _bad("key_id is not 16 lowercase hex digits")
    if not is_version(data["floor"]):
        raise _bad(f"floor {data['floor']!r} is not a plain N.N.N")
    releases = data["releases"]
    if not isinstance(releases, list):
        raise _bad("releases is not a list")
    versions = [_check_release(entry, position) for position, entry in enumerate(releases)]
    if len(set(versions)) != len(versions):
        raise _bad("a version is listed twice")
    return data


# ----------------------------------------------------------------------------- memory --


def empty_memory() -> dict[str, Any]:
    """The memory of a reader that has accepted nothing."""
    return {"serials": {}, "silenced": []}


def check_memory(memory: Any) -> dict[str, Any]:
    """`memory` if its known members are what readers write, an empty one for None; else BadMemory.

    Open for extension, closed for known members: a member this reader does not know is
    tolerated, a known member that is missing or of the wrong type is refused.
    """
    if memory is None:
        return empty_memory()
    if not isinstance(memory, dict) or not {"serials", "silenced"} <= set(memory):
        raise BadMemory(f"a memory has serials and silenced, not {memory!r:.80}")
    serials, silenced = memory["serials"], memory["silenced"]
    if not isinstance(serials, dict) or not all(
        isinstance(key_id_, str)
        and _KEY_ID.fullmatch(key_id_)
        and _whole(serial)
        and serial >= 1
        for key_id_, serial in serials.items()
    ):
        raise BadMemory("serials must map key ids to whole numbers >= 1")
    if not isinstance(silenced, list) or not all(
        isinstance(key_id_, str) and _KEY_ID.fullmatch(key_id_) for key_id_ in silenced
    ):
        raise BadMemory("silenced must be a list of key ids")
    return memory


def _serials(memory: Any) -> dict[str, int]:
    return check_memory(memory)["serials"]


def _silenced(memory: Any) -> set[str]:
    return set(check_memory(memory)["silenced"])


def rank_floor(keys: Iterable[Key], memory: Any) -> int:
    """The highest rank, among `keys`, of a key this reader has accepted an index from (0: none)."""
    serials = _serials(memory)
    return max((key.rank for key in keys if key.key_id in serials), default=0)


def remember(memory: Any, key: Key, serial: int, keys: Iterable[Key]) -> dict[str, Any]:
    """The memory after accepting `serial` from `key`, every key ranked below it silenced."""
    serials = dict(_serials(memory))
    serials[key.key_id] = serial
    silenced = _silenced(memory) | {other.key_id for other in keys if other.rank < key.rank}
    return {"serials": serials, "silenced": sorted(silenced)}


# ------------------------------------------------------------------------ stored state --

STATE_SCHEMA = 1
LINEAGES = ("release", "acceptance")


def empty_state() -> dict[str, Any]:
    """The state of a reader that has never stored anything."""
    return {"schema": STATE_SCHEMA, "release": {}, "acceptance": {}}


def check_state(state: Any) -> dict[str, Any]:
    """A reader's stored state, or BadMemory. None is a reader that has never stored anything.

    `{"schema": 1, "release": {...}, "acceptance": {...}}`, each part mapping a key-set
    fingerprint to `{"keys": [key_id], "serials": ..., "silenced": ...}`. Unknown members and a
    higher schema are tolerated; a known member that is missing or malformed is refused.
    """
    if state is None:
        return empty_state()
    if not isinstance(state, dict) or not {"schema", *LINEAGES} <= set(state):
        raise BadMemory(f"a state has schema, release and acceptance, not {state!r:.80}")
    if not _whole(state["schema"]) or state["schema"] < 1:
        raise BadMemory(f"state schema {state['schema']!r} is not a whole number >= 1")
    for lineage in LINEAGES:
        entries = state[lineage]
        if not isinstance(entries, dict):
            raise BadMemory(f"{lineage} is not an object")
        for fingerprint_, entry in entries.items():
            if not isinstance(fingerprint_, str) or not _SHA256.fullmatch(fingerprint_):
                raise BadMemory(f"{lineage}: {fingerprint_!r:.20} is not a key-set fingerprint")
            if not isinstance(entry, dict) or not {"keys", "serials", "silenced"} <= set(entry):
                raise BadMemory(f"{lineage} {fingerprint_[:12]}: needs keys, serials and silenced")
            keys = entry["keys"]
            if (
                not isinstance(keys, list)
                or not keys
                or not all(
                    isinstance(key_id_, str) and _KEY_ID.fullmatch(key_id_) for key_id_ in keys
                )
            ):
                raise BadMemory(f"{lineage} {fingerprint_[:12]}: keys is not a list of key ids")
            check_memory({"serials": entry["serials"], "silenced": entry["silenced"]})
    return state


def _lineage(acceptance: Any) -> str:
    if not isinstance(acceptance, bool):
        raise TypeError("acceptance must be True or False: say which part of the state is meant")
    return LINEAGES[1] if acceptance else LINEAGES[0]


def memory_for(state: Any, keys: Sequence[Key], *, acceptance: bool) -> dict[str, Any]:
    """The memory a reader holding `keys` judges with, from its stored `state`.

    Only the entries of its own lineage whose key set shares a key with `keys`: the largest serial
    per key it holds, and every silenced key.
    """
    lineage = _lineage(acceptance)
    state = check_state(state)
    held = {key.key_id for key in keys}
    serials: dict[str, int] = {}
    silenced: set[str] = set()
    for entry in state[lineage].values():
        if not held & set(entry["keys"]):
            continue
        for key_id_, serial in entry["serials"].items():
            if key_id_ in held:
                serials[key_id_] = max(serial, serials.get(key_id_, 0))
        silenced |= set(entry["silenced"])
    return {"serials": serials, "silenced": sorted(silenced)}


def store(
    state: Any, keys: Sequence[Key], memory: Any, *, acceptance: bool
) -> dict[str, Any]:
    """The state after a reader holding `keys` accepted an index and holds `memory`.

    Everything this reader does not know about is kept as it was - other members, the other
    lineage, other members of the entry it rewrites, a higher schema number.
    """
    lineage = _lineage(acceptance)
    state = check_state(state)
    memory = check_memory(memory)
    updated = dict(state)
    updated["schema"] = max(state["schema"], STATE_SCHEMA)
    part = dict(state[lineage])
    name = fingerprint(keys)
    part[name] = {
        **part.get(name, {}),
        "keys": sorted(key_.key_id for key_ in keys),
        "serials": dict(memory["serials"]),
        "silenced": list(memory["silenced"]),
    }
    updated[lineage] = part
    return updated


# ---------------------------------------------------------------------------- accepting --


def authenticate(
    index_raw: Any, signature_raw: Any, keys: Sequence[Key]
) -> tuple[dict[str, Any], Key]:
    """`(index, key)` when one of `keys` signed exactly these bytes (steps 1-4), else Refused."""
    if isinstance(index_raw, (bytes, bytearray)) and len(index_raw) > MAX_INDEX_BYTES:
        raise Refused("too_large", f"the index is over {MAX_INDEX_BYTES} bytes")
    signer_id, signature = parse_signature(signature_raw)
    key = by_id(keys, signer_id)
    if key is None:
        raise Refused("unknown_key", f"key {signer_id} is not one of this reader's keys")
    if not isinstance(index_raw, (bytes, bytearray)) or not verify(
        key.public, bytes(index_raw), signature
    ):
        raise Refused("bad_signature", f"the signature does not verify with key {key.key_id}")
    index = parse_index(index_raw)
    if index["key_id"] != key.key_id:
        raise Refused(
            "key_mismatch",
            f"the index names key {index['key_id']}, its signature key {key.key_id}",
        )
    return index, key


def judge(
    index: dict[str, Any], key: Key, keys: Sequence[Key], memory: Any
) -> dict[str, Any]:
    """Steps 5 and 6 for an authentic index: the memory to store after it, or Refused."""
    if key.key_id in _silenced(memory):
        raise Refused(
            "rank", f"key {key.key_id} was silenced by an index signed with a higher-ranked key"
        )
    floor = rank_floor(keys, memory)
    if key.rank < floor:
        raise Refused(
            "rank", f"key {key.key_id} has rank {key.rank}, and rank {floor} is already accepted"
        )
    serial = index["serial"]
    stored = _serials(memory).get(key.key_id)
    if stored is None:
        if not key.baseline <= serial <= key.baseline + MAX_JUMP:
            raise Refused(
                "first_sight",
                f"serial {serial} is not within {MAX_JUMP} of key {key.key_id}'s "
                f"baseline {key.baseline}",
            )
    elif serial <= stored:
        raise Refused("replay", f"serial {serial} is not above {stored}, the last accepted")
    elif serial > stored + MAX_JUMP:
        raise Refused("jump", f"serial {serial} is more than {MAX_JUMP} above {stored}")
    return remember(memory, key, serial, keys)


def accept(
    index_raw: Any, signature_raw: Any, keys: Sequence[Key], memory: Any
) -> Accepted:
    """Accept a signed index, or raise Refused - the rule in the module docstring, in its order."""
    index, key = authenticate(index_raw, signature_raw, keys)
    return Accepted(index, key, judge(index, key, keys, memory))


# ------------------------------------------------------------------------ compatibility --

# Why a release is not offered. Each has a translation, `release_reason_<code>`.
REASON_CONTRACT = "contract"
REASON_BELOW_FLOOR = "below_floor"
REASON_NEEDS_INTEGRATION = "needs_integration"
REASON_WITHDRAWN = "withdrawn"

# How many releases the attribute lists: the newest, as the receiver's own `update` topic does.
AVAILABLE_LIMIT = 20


def incompatibility(
    release: dict[str, Any],
    *,
    contract: int,
    floor: str,
    integration_version: str | None,
) -> str | None:
    """Why this integration may not offer `release`, or None when it may.

    The one rule both programs apply: the same contract major; at or above the floor, which is
    the higher of this integration's own and the index's; this integration at or above the
    release's `min_integration`; not withdrawn. A release's `depends` is the receiver's to check
    - it knows what it has installed and refuses a missing package itself.
    """
    if release.get("withdrawn") is not None:
        return REASON_WITHDRAWN
    if release.get("contract") != contract:
        return REASON_CONTRACT
    if version_key(release["version"]) < version_key(floor):
        return REASON_BELOW_FLOOR
    needed = release.get("min_integration")
    if needed is not None and (
        not is_version(integration_version)
        or version_key(integration_version) < version_key(needed)
    ):
        return REASON_NEEDS_INTEGRATION
    return None


def effective_floor(index: dict[str, Any], own_floor: str) -> str:
    """The higher of this integration's floor and the index's."""
    return max(own_floor, index["floor"], key=version_key)


def available(
    index: dict[str, Any],
    *,
    contract: int,
    own_floor: str,
    integration_version: str | None,
) -> list[dict[str, Any]]:
    """The newest releases at or above the floor and not withdrawn, newest first, with a reason
    for every one this integration may not offer."""
    floor = effective_floor(index, own_floor)
    listed = [
        release
        for release in index["releases"]
        if release.get("withdrawn") is None
        and version_key(release["version"]) >= version_key(floor)
    ]
    listed.sort(key=lambda release: version_key(release["version"]), reverse=True)
    result = []
    for release in listed[:AVAILABLE_LIMIT]:
        reason = incompatibility(
            release,
            contract=contract,
            floor=floor,
            integration_version=integration_version,
        )
        result.append(
            {"version": release["version"], "compatible": reason is None, "reason": reason}
        )
    return result


def release_of(index: dict[str, Any] | None, version: str | None) -> dict[str, Any] | None:
    """The index's entry for `version`, or None."""
    if not index or version is None:
        return None
    for release in index["releases"]:
        if release["version"] == version:
            return release
    return None

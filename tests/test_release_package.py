"""A release of the signed index, fetched and verified before any receiver sees a byte of it.

What the SSH path, the options flow's downgrade and the relay all stand on (spec section ae.7,
"SSH path", and ae.3, "Channels"): only a version the rule allows; from the fixed origin, with no
redirect; believed only at the signed size and sha256, with a control file that names this package
and this version; and cross-checked once a day per version against GitHub's own digest of the
release asset, whose disagreement refuses and whose silence does not.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.enigma2_mqtt import release_index, release_package
from custom_components.enigma2_mqtt.bundle import BundledPlugin, BundleError
from custom_components.enigma2_mqtt.const import PLUGIN_INDEX_ORIGIN, PLUGIN_RELEASE_API
from custom_components.enigma2_mqtt.release_package import (
    BELOW_FLOOR,
    DIGEST,
    DOWNLOAD,
    INCOMPATIBLE,
    NO_INDEX,
    ORIGIN_BUNDLE,
    ORIGIN_DOWNLOAD,
    UNKNOWN_VERSION,
    WITHDRAWN,
    PackageError,
    async_fetch_release,
)
from custom_components.enigma2_mqtt.release_store import async_release_index_cache

from .ipk import plugin_ipk
from .signed_index import keyset, release, sign

VERSION = "0.4.0"
PACKAGE = plugin_ipk(VERSION)
SHA = hashlib.sha256(PACKAGE).hexdigest()
FILENAME = f"enigma2-plugin-extensions-mqttbridge_{VERSION}_all.ipk"
URL = PLUGIN_INDEX_ORIGIN + FILENAME
API = PLUGIN_RELEASE_API + VERSION
LOAD_BUNDLE = "custom_components.enigma2_mqtt.bundle.load_bundled_plugin"


@pytest.fixture(autouse=True)
def test_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_index, "EMBEDDED", keyset("test"))


@pytest.fixture(autouse=True)
def no_bundle() -> Any:
    """No bundle of the version asked for, unless a test hands one over."""
    with patch(LOAD_BUNDLE, side_effect=BundleError("none in the tests")) as loader:
        yield loader


def _entry(**changes: Any) -> dict[str, Any]:
    return release(VERSION, **{"size": len(PACKAGE), "sha256": SHA, **changes})


async def _hold(hass: HomeAssistant, *releases: dict[str, Any], floor: str = "0.2.0") -> None:
    """Make the cache hold a verified index listing `releases`."""
    cache = async_release_index_cache(hass)
    await cache.async_load()
    raw, _ = sign(1, list(releases), floor=floor)
    cache.index = json.loads(raw)
    cache.integration_version = "0.4.0"


def _asset(digest: str | None) -> dict[str, Any]:
    return {"assets": [{"name": FILENAME, "digest": digest}, {"name": "other", "digest": "x"}]}


async def _refused(hass: HomeAssistant, version: str = VERSION) -> PackageError:
    with pytest.raises(PackageError) as raised:
        await async_fetch_release(hass, version)
    return raised.value


async def test_a_listed_release_is_fetched_and_held_to_its_signed_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _hold(hass, _entry(depends=["python3-core", "python3-json"], self_update=True))
    aioclient_mock.get(API, json=_asset(f"sha256:{SHA}"))
    aioclient_mock.get(URL, content=PACKAGE)

    source = await async_fetch_release(hass, VERSION)

    assert source.data == PACKAGE
    assert source.sha256 == SHA
    assert source.origin == ORIGIN_DOWNLOAD
    assert source.commit == release(VERSION)["commit"]
    assert source.depends == ("python3-core", "python3-json")
    assert source.self_update is True
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [API, URL]


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        ({"status": 302, "headers": {"Location": "https://example.com/x.ipk"}}, "redirect"),
        ({"status": 404}, "HTTP 404"),
        ({"content": PACKAGE + b"x"}, "bytes"),
        ({"content": PACKAGE[:-1]}, "bytes"),
        ({"content": PACKAGE[:-1] + bytes([PACKAGE[-1] ^ 1])}, "sha256"),
    ],
    ids=["redirect", "http error", "longer", "shorter", "same size other bytes"],
)
async def test_anything_but_the_signed_bytes_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any],
    detail: str,
) -> None:
    await _hold(hass, _entry())
    aioclient_mock.get(API, json=_asset(f"sha256:{SHA}"))
    aioclient_mock.get(URL, **answer)

    refused = await _refused(hass)

    assert refused.code == DOWNLOAD
    assert refused.placeholders == {"version": VERSION}
    assert detail in refused.detail
    assert "Nothing was sent to any receiver" in caplog.text


@pytest.mark.parametrize(
    ("package", "version"),
    [
        ("enigma2-plugin-extensions-other", VERSION),
        ("enigma2-plugin-extensions-mqttbridge", "0.3.9"),
    ],
    ids=["another package", "another version"],
)
async def test_a_package_that_names_itself_otherwise_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, package: str, version: str
) -> None:
    """The checksum is what the index signed; a control file that disagrees with the entry is
    an entry written wrong, and opkg would be the one to find out."""
    other = plugin_ipk(version, package=package)
    await _hold(hass, _entry(size=len(other), sha256=hashlib.sha256(other).hexdigest()))
    aioclient_mock.get(API, status=404)
    aioclient_mock.get(URL, content=other)

    refused = await _refused(hass)

    assert refused.code == DOWNLOAD
    assert "names itself" in refused.detail


@pytest.mark.parametrize(
    ("releases", "floor", "code"),
    [
        ([], "0.2.0", UNKNOWN_VERSION),
        ([_entry(withdrawn="it breaks EPG")], "0.2.0", WITHDRAWN),
        ([_entry()], "0.5.0", BELOW_FLOOR),
        ([_entry(contract=2)], "0.2.0", INCOMPATIBLE),
        ([_entry(min_integration="0.9.0")], "0.2.0", INCOMPATIBLE),
    ],
    ids=[
        "not listed",
        "withdrawn",
        "below the floor",
        "another contract",
        "needs a newer integration",
    ],
)
async def test_the_rule_refuses_before_any_request(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    releases: list[dict[str, Any]],
    floor: str,
    code: str,
) -> None:
    await _hold(hass, *releases, floor=floor)

    refused = await _refused(hass)

    assert refused.code == code
    assert aioclient_mock.call_count == 0


async def test_without_a_verified_index_nothing_is_fetched(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    refused = await _refused(hass)

    assert refused.code == NO_INDEX
    assert aioclient_mock.call_count == 0


async def test_a_withdrawal_keeps_its_reason_for_the_sentence(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _hold(hass, _entry(withdrawn="it breaks EPG"))

    refused = await _refused(hass)

    assert refused.placeholders == {"version": VERSION, "reason": "it breaks EPG"}


async def test_github_disagreeing_with_the_signed_checksum_refuses_before_the_download(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _hold(hass, _entry())
    aioclient_mock.get(API, json=_asset("sha256:" + "0" * 64))
    aioclient_mock.get(URL, content=PACKAGE)

    refused = await _refused(hass)

    assert refused.code == DIGEST
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [API]


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 403},
        {"status": 429},
        {"json": _asset(None)},
        {"json": {"assets": []}},
        {"content": b"not json"},
        {"status": 301, "headers": {"Location": "https://api.github.com/elsewhere"}},
    ],
    ids=["rate-limited", "too many", "no digest", "no such asset", "not json", "moved"],
)
async def test_github_saying_nothing_does_not_refuse(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any],
) -> None:
    await _hold(hass, _entry())
    aioclient_mock.get(API, **answer)
    aioclient_mock.get(URL, content=PACKAGE)

    source = await async_fetch_release(hass, VERSION)

    assert source.data == PACKAGE
    assert "going on with the signed checksum alone" in caplog.text


async def test_github_is_asked_once_a_day_per_version_whoever_asks(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory
) -> None:
    """The relay can be asked for a package by anybody on the broker: that must not be a way
    to spend the requests GitHub allows this address."""
    await _hold(hass, _entry())
    aioclient_mock.get(API, status=403)
    aioclient_mock.get(URL, content=PACKAGE)

    await async_fetch_release(hass, VERSION)
    freezer.tick(timedelta(hours=23))
    await async_fetch_release(hass, VERSION)
    api_calls = [call for call in aioclient_mock.mock_calls if str(call[1]) == API]
    assert len(api_calls) == 1

    freezer.tick(timedelta(hours=2))
    await async_fetch_release(hass, VERSION)
    api_calls = [call for call in aioclient_mock.mock_calls if str(call[1]) == API]
    assert len(api_calls) == 2


async def test_a_remembered_disagreement_refuses_without_asking_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _hold(hass, _entry())
    aioclient_mock.get(API, json=_asset("sha256:" + "0" * 64))

    assert (await _refused(hass)).code == DIGEST
    assert (await _refused(hass)).code == DIGEST
    assert aioclient_mock.call_count == 1


async def test_the_bundle_is_used_when_it_is_those_very_bytes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    no_bundle: Any,
    tmp_path: Path,
) -> None:
    """The release this integration shipped needs no download - and no second opinion: CI
    reproduced it byte for byte."""
    artifact = tmp_path / FILENAME
    artifact.write_bytes(PACKAGE)
    no_bundle.side_effect = None
    no_bundle.return_value = BundledPlugin(artifact, VERSION, SHA, "1" * 40)
    await _hold(hass, _entry())

    source = await async_fetch_release(hass, VERSION)

    assert source.origin == ORIGIN_BUNDLE
    assert source.data == PACKAGE
    assert aioclient_mock.call_count == 0


async def test_a_bundle_of_the_same_number_but_other_bytes_is_not_used(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    no_bundle: Any,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / FILENAME
    artifact.write_bytes(b"a development build")
    no_bundle.side_effect = None
    no_bundle.return_value = BundledPlugin(
        artifact, VERSION, hashlib.sha256(b"a development build").hexdigest(), "1" * 40
    )
    await _hold(hass, _entry())
    aioclient_mock.get(API, json=_asset(f"sha256:{SHA}"))
    aioclient_mock.get(URL, content=PACKAGE)

    source = await async_fetch_release(hass, VERSION)

    assert source.origin == ORIGIN_DOWNLOAD
    assert source.data == PACKAGE


async def test_the_download_is_capped_at_the_signed_size(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """One byte past the signed size is read, so a longer answer is refused as longer, and an
    answer with no end is never pulled into memory whole."""
    await _hold(hass, _entry())
    aioclient_mock.get(API, status=404)
    aioclient_mock.get(URL, content=PACKAGE + b"\0" * 1_000_000)

    with patch.object(
        release_package, "_async_get", wraps=release_package._async_get
    ) as fetched:
        refused = await _refused(hass)

    assert refused.code == DOWNLOAD
    limits = [call.args[2] for call in fetched.call_args_list if call.args[1] == URL]
    assert limits == [len(PACKAGE)]
    assert f"is {len(PACKAGE) + 1} bytes" in refused.detail

"""The relay: a verified package served on the LAN to one receiver without internet.

Spec section ae.7, "Relay view" and "Which address", and the relay handshake of ae.6: no
authentication, a 43-character token, bound to the receiver's IPv4 address from `info.ip` (an
IPv4-mapped IPv6 peer unwrapped; no IPv4 address, no grant), ten minutes, repeatable until then,
404 for everything else; one grant per (receiver, version) and at most three per receiver, a
grant in use never evicted; the address on the receiver's subnet, else the internal URL, never the
external one; `relay_request` answered with `cmd/relay` at most once a minute per receiver.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import ipaddress
import json
import re
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt import release_package
from custom_components.enigma2_mqtt.relay import (
    RELAY_LIFETIME,
    RELAY_PATH,
    RelayError,
    async_get_relay,
    async_relay_base,
    peer_address,
)
from custom_components.enigma2_mqtt.release_package import (
    ORIGIN_DOWNLOAD,
    PackageError,
    PackageSource,
)

from .conftest import INFO, INFO_TOPIC, NODE_ID, async_setup_box

REQUEST_TOPIC = f"enigma2/{NODE_ID}/relay_request"
RELAY_TOPIC = f"enigma2/{NODE_ID}/cmd/relay"
ADAPTERS = "custom_components.enigma2_mqtt.relay.network.async_get_adapters"
FETCH = "custom_components.enigma2_mqtt.relay.async_fetch_release"
URL = re.compile(r"http://192\.0\.2\.5:8123/api/enigma2_mqtt/relay/[A-Za-z0-9_-]{43}")


def _package(version: str = "0.3.0", data: bytes = b"signed package bytes") -> PackageSource:
    return PackageSource(
        version=version,
        data=data,
        sha256=f"{version}-sha".ljust(64, "0"),
        commit=None,
        depends=(),
        origin=ORIGIN_DOWNLOAD,
    )


def _adapter(address: str, prefix: int, *, enabled: bool = True) -> dict[str, Any]:
    return {
        "name": "eth0",
        "index": 1,
        "enabled": enabled,
        "auto": True,
        "default": True,
        "ipv4": [{"address": address, "network_prefix": prefix}],
        "ipv6": [],
    }


@pytest.fixture
async def relay(hass: HomeAssistant) -> Any:
    assert await async_setup_component(hass, "http", {})
    relay = async_get_relay(hass)
    yield relay
    relay.clear()


# ------------------------------------------------------------------ addresses --


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.0.2.12", "192.0.2.12"),
        ("::ffff:192.0.2.12", "192.0.2.12"),
        ("2001:db8::12", None),
        ("", None),
        (None, None),
        ("not an address", None),
    ],
)
def test_peer_addresses_are_compared_as_ipv4_addresses(value: Any, expected: str | None) -> None:
    result = peer_address(value)
    assert result == (ipaddress.IPv4Address(expected) if expected else None)


# ------------------------------------------------------------------ the view --


async def test_the_receiver_may_fetch_again_and_again_until_expiry(
    hass: HomeAssistant, relay: Any, hass_client_no_auth: Any, freezer: FrozenDateTimeFactory
) -> None:
    grant = relay.grant(NODE_ID, "127.0.0.1", _package())
    client = await hass_client_no_auth()

    for _ in range(3):
        response = await client.get(RELAY_PATH + grant.token)
        assert response.status == 200
        assert await response.read() == b"signed package bytes"
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert response.headers["Cache-Control"] == "no-store"
    assert len(grant.token) == 43

    freezer.tick(timedelta(seconds=RELAY_LIFETIME + 1))
    response = await client.get(RELAY_PATH + grant.token)
    assert response.status == 404


async def test_any_other_address_gets_the_same_404_and_a_log_line(
    hass: HomeAssistant, relay: Any, hass_client_no_auth: Any, caplog: pytest.LogCaptureFixture
) -> None:
    grant = relay.grant(NODE_ID, "192.0.2.12", _package())
    client = await hass_client_no_auth()

    response = await client.get(RELAY_PATH + grant.token)

    assert response.status == 404
    assert await response.read() == b""
    ours = [r.getMessage() for r in caplog.records if r.name.endswith(".relay")]
    assert any("refused" in message for message in ours)
    # The token is the address; this integration never writes it whole.
    assert not any(grant.token in message for message in ours)


async def test_a_forwarded_for_header_does_not_make_another_address_the_receiver(
    hass: HomeAssistant, relay: Any, hass_client_no_auth: Any
) -> None:
    grant = relay.grant(NODE_ID, "192.0.2.12", _package())
    client = await hass_client_no_auth()

    response = await client.get(
        RELAY_PATH + grant.token, headers={"X-Forwarded-For": "192.0.2.12"}
    )

    assert response.status != 200
    assert await response.read() != b"signed package bytes"


@pytest.mark.parametrize(
    "path",
    ["A" * 43, "", "../../config", "x/y"],
    ids=["unknown token", "no token", "traversal", "nested"],
)
async def test_nothing_else_is_served(
    hass: HomeAssistant, relay: Any, hass_client_no_auth: Any, path: str
) -> None:
    relay.grant(NODE_ID, "127.0.0.1", _package())
    client = await hass_client_no_auth()

    response = await client.get(RELAY_PATH + path)

    assert response.status in (404, 405)
    assert b"signed package bytes" not in await response.read()


async def test_head_is_not_routed_and_range_gets_the_whole_package(
    hass: HomeAssistant, relay: Any, hass_client_no_auth: Any
) -> None:
    grant = relay.grant(NODE_ID, "127.0.0.1", _package())
    client = await hass_client_no_auth()

    head = await client.head(RELAY_PATH + grant.token)
    ranged = await client.get(RELAY_PATH + grant.token, headers={"Range": "bytes=0-3"})

    assert head.status == 405
    assert ranged.status == 200
    assert await ranged.read() == b"signed package bytes"
    assert "Content-Range" not in ranged.headers


async def test_concurrent_fetches_each_get_the_whole_package(
    hass: HomeAssistant, relay: Any, hass_client_no_auth: Any
) -> None:
    grant = relay.grant(NODE_ID, "127.0.0.1", _package(data=b"x" * 300_000))
    client = await hass_client_no_auth()

    responses = await asyncio.gather(
        *(client.get(RELAY_PATH + grant.token) for _ in range(5))
    )

    for response in responses:
        assert response.status == 200
        assert await response.read() == b"x" * 300_000
    assert grant.served == 5


# ------------------------------------------------------------------ the grants --


async def test_a_second_request_for_the_same_version_reuses_its_grant(
    hass: HomeAssistant, relay: Any, freezer: FrozenDateTimeFactory
) -> None:
    first = relay.grant(NODE_ID, "192.0.2.12", _package())
    freezer.tick(timedelta(minutes=5))
    again = relay.grant(NODE_ID, "192.0.2.12", _package())

    assert again is first
    assert len(relay.grants(NODE_ID)) == 1


async def test_a_grant_near_its_end_is_replaced_not_doubled(
    hass: HomeAssistant, relay: Any, freezer: FrozenDateTimeFactory
) -> None:
    first = relay.grant(NODE_ID, "192.0.2.12", _package())
    freezer.tick(timedelta(minutes=8))
    again = relay.grant(NODE_ID, "192.0.2.12", _package())

    assert again.token != first.token
    assert relay.lookup(first.token) is None
    assert [grant.token for grant in relay.grants(NODE_ID)] == [again.token]


async def test_a_fourth_version_evicts_the_oldest_grant(
    hass: HomeAssistant, relay: Any, freezer: FrozenDateTimeFactory
) -> None:
    grants = []
    for version in ("0.2.0", "0.3.0", "0.4.0"):
        grants.append(relay.grant(NODE_ID, "192.0.2.12", _package(version)))
        freezer.tick(timedelta(seconds=1))

    fourth = relay.grant(NODE_ID, "192.0.2.12", _package("0.5.0"))

    assert relay.lookup(grants[0].token) is None
    assert [grant.version for grant in relay.grants(NODE_ID)] == ["0.3.0", "0.4.0", "0.5.0"]
    assert fourth.version == "0.5.0"


async def test_a_grant_in_use_is_never_evicted(
    hass: HomeAssistant, relay: Any, freezer: FrozenDateTimeFactory
) -> None:
    grants = []
    for version in ("0.2.0", "0.3.0", "0.4.0"):
        grants.append(relay.grant(NODE_ID, "192.0.2.12", _package(version)))
        freezer.tick(timedelta(seconds=1))
    relay.mark_in_use(grants[0].token, True)

    relay.grant(NODE_ID, "192.0.2.12", _package("0.5.0"))
    assert relay.lookup(grants[0].token) is grants[0]
    assert relay.lookup(grants[1].token) is None

    for grant in relay.grants(NODE_ID):
        relay.mark_in_use(grant.token, True)
    with pytest.raises(RelayError) as raised:
        relay.grant(NODE_ID, "192.0.2.12", _package("0.6.0"))
    assert raised.value.key == "relay_busy"


async def test_each_receiver_has_its_own_grants(hass: HomeAssistant, relay: Any) -> None:
    for version in ("0.2.0", "0.3.0", "0.4.0"):
        relay.grant(NODE_ID, "192.0.2.12", _package(version))
    other = relay.grant("vuuno4kse_005302", "192.0.2.13", _package("0.2.0"))

    assert len(relay.grants(NODE_ID)) == 3
    relay.drop_node("vuuno4kse_005302")
    assert relay.lookup(other.token) is None
    assert len(relay.grants(NODE_ID)) == 3


@pytest.mark.parametrize("address", [None, "", "2001:db8::12", "receiver.local"])
async def test_no_ipv4_address_no_grant(hass: HomeAssistant, relay: Any, address: Any) -> None:
    with pytest.raises(RelayError) as raised:
        relay.grant(NODE_ID, address, _package())

    assert raised.value.key == "relay_no_ipv4"
    assert relay.grants(NODE_ID) == []


async def test_an_expired_grant_lets_go_of_its_bytes(
    hass: HomeAssistant, relay: Any, freezer: FrozenDateTimeFactory
) -> None:
    grant = relay.grant(NODE_ID, "192.0.2.12", _package())

    freezer.tick(timedelta(seconds=RELAY_LIFETIME + 1))
    async_fire_time_changed_now(hass)
    await hass.async_block_till_done()

    assert grant.token not in relay._grants  # noqa: SLF001 - the timer, not a lookup, freed it


def async_fire_time_changed_now(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass)


# ------------------------------------------------------------------ the address --


async def test_the_address_on_the_receiver_s_subnet_comes_first(hass: HomeAssistant) -> None:
    hass.config.api = ApiConfig("198.51.100.2", "0.0.0.0", 8123, False)
    await hass.config.async_update(internal_url="http://198.51.100.2:8123")
    adapters = [
        _adapter("127.0.0.1", 8),
        _adapter("192.0.2.99", 24, enabled=False),
        _adapter("198.51.100.2", 24),
        _adapter("192.0.2.5", 24),
    ]
    with patch(ADAPTERS, AsyncMock(return_value=adapters)):
        base = await async_relay_base(hass, ipaddress.IPv4Address("192.0.2.12"))

    assert base == "http://192.0.2.5:8123"


async def test_without_one_the_internal_url(hass: HomeAssistant) -> None:
    hass.config.api = ApiConfig("198.51.100.2", "0.0.0.0", 8123, False)
    await hass.config.async_update(
        internal_url="http://198.51.100.2:8123", external_url="https://example.com"
    )
    with patch(ADAPTERS, AsyncMock(return_value=[_adapter("198.51.100.2", 24)])):
        base = await async_relay_base(hass, ipaddress.IPv4Address("192.0.2.12"))

    assert base == "http://198.51.100.2:8123"


async def test_never_the_external_url(hass: HomeAssistant) -> None:
    hass.config.api = None
    await hass.config.async_update(internal_url=None, external_url="https://example.com")
    with (
        patch(ADAPTERS, AsyncMock(return_value=[])),
        pytest.raises(RelayError) as raised,
    ):
        await async_relay_base(hass, ipaddress.IPv4Address("192.0.2.12"))

    assert raised.value.key == "relay_no_address"


# ------------------------------------------------------------- relay_request --


async def _box(hass: HomeAssistant, config_entry: MockConfigEntry, **info: Any) -> None:
    assert await async_setup_component(hass, "http", {})
    await async_setup_box(hass, config_entry)
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, **info}))
    await hass.async_block_till_done()


def _answers(mqtt_mock: Any) -> list[dict[str, Any]]:
    return [
        json.loads(call.args[1])
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == RELAY_TOPIC
    ]


async def _ask(hass: HomeAssistant, payload: Any, *, retain: bool = False) -> None:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    async_fire_mqtt_message(hass, REQUEST_TOPIC, body, retain=retain)
    # The answer runs as a task of the entry.
    await hass.async_block_till_done()


REQUEST = {"id": "a1b2c3d4e5f6", "version": "0.3.0", "serial": 1}


@pytest.fixture
def lan() -> Any:
    with patch(ADAPTERS, AsyncMock(return_value=[_adapter("192.0.2.5", 24)])):
        yield


async def test_a_relay_request_is_answered_with_an_address_bound_to_the_receiver(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,
    hass_client_no_auth: Any,
) -> None:
    await _box(hass, config_entry)
    with patch(FETCH, AsyncMock(return_value=_package())) as fetch:
        await _ask(hass, REQUEST)

    fetch.assert_awaited_once_with(hass, "0.3.0")
    (answer,) = _answers(mqtt_mock)
    assert answer["id"] == "a1b2c3d4e5f6"
    assert answer["version"] == "0.3.0"
    assert URL.fullmatch(answer["url"])
    grant = async_get_relay(hass).grants(NODE_ID)[0]
    assert answer["expires"] == int(grant.expires)
    assert grant.address == ipaddress.IPv4Address("192.0.2.12")
    publish = next(
        call for call in mqtt_mock.async_publish.call_args_list if call.args[0] == RELAY_TOPIC
    )
    assert publish.args[2:] == (1, False)
    # The test client is not the receiver: the address is bound.
    client = await hass_client_no_auth()
    response = await client.get(answer["url"].split(":8123", 1)[1])
    assert response.status == 404


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        {"version": "0.3.0"},
        {"id": "a" * 65, "version": "0.3.0"},
        {"id": "a b", "version": "0.3.0"},
        {"id": "a1", "version": "0.3"},
        {"id": "a1", "version": "latest"},
        ["a1", "0.3.0"],
    ],
    ids=["not json", "no id", "long id", "id with a space", "short version", "latest", "a list"],
)
async def test_a_malformed_request_gets_no_answer(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,
    payload: Any,
) -> None:
    await _box(hass, config_entry)
    with patch(FETCH, AsyncMock(return_value=_package())) as fetch:
        await _ask(hass, payload)

    fetch.assert_not_called()
    assert _answers(mqtt_mock) == []


async def test_a_retained_request_gets_no_answer(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,
) -> None:
    await _box(hass, config_entry)
    with patch(FETCH, AsyncMock(return_value=_package())) as fetch:
        await _ask(hass, REQUEST, retain=True)

    fetch.assert_not_called()
    assert _answers(mqtt_mock) == []


async def test_one_answer_a_minute_per_receiver(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,
) -> None:
    """The minute is moved by hand rather than by freezing the clock: a frozen clock also
    freezes the MQTT client's own subscribe debounce, and the box would never be subscribed."""
    await _box(hass, config_entry)
    relay = async_get_relay(hass)
    with patch(FETCH, AsyncMock(return_value=_package())) as fetch:
        await _ask(hass, REQUEST)
        relay._answered[NODE_ID] -= 30  # noqa: SLF001
        await _ask(hass, {**REQUEST, "id": "second"})
        assert fetch.await_count == 1
        relay._answered[NODE_ID] -= 31  # noqa: SLF001
        await _ask(hass, {**REQUEST, "id": "third"})

    assert [answer["id"] for answer in _answers(mqtt_mock)] == ["a1b2c3d4e5f6", "third"]
    # The same version: the same grant, the same address.
    assert len({answer["url"] for answer in _answers(mqtt_mock)}) == 1


@pytest.mark.parametrize(
    "code",
    [
        release_package.WITHDRAWN,
        release_package.BELOW_FLOOR,
        release_package.INCOMPATIBLE,
        release_package.UNKNOWN_VERSION,
        release_package.NO_INDEX,
        release_package.DOWNLOAD,
        release_package.DIGEST,
    ],
)
async def test_a_version_the_rule_or_the_download_refuses_gets_no_answer(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,
    code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _box(hass, config_entry)
    with patch(FETCH, AsyncMock(side_effect=PackageError(code, "no", {"version": "0.3.0"}))):
        await _ask(hass, REQUEST)

    assert _answers(mqtt_mock) == []
    assert async_get_relay(hass).grants(NODE_ID) == []
    assert f"({code})" in caplog.text


@pytest.mark.parametrize("ip", [None, "2001:db8::12"], ids=["no address", "IPv6 only"])
async def test_a_receiver_without_an_ipv4_address_gets_no_answer(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,
    ip: str | None,
) -> None:
    await _box(hass, config_entry, ip=ip)
    with patch(FETCH, AsyncMock(return_value=_package())):
        await _ask(hass, REQUEST)

    assert _answers(mqtt_mock) == []


async def test_unloading_the_entry_ends_its_grants(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,
) -> None:
    await _box(hass, config_entry)
    with patch(FETCH, AsyncMock(return_value=_package())):
        await _ask(hass, REQUEST)
    assert len(async_get_relay(hass).grants(NODE_ID)) == 1

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert async_get_relay(hass).grants(NODE_ID) == []


async def test_an_expired_grant_is_refused_even_before_its_timer_runs(
    hass: HomeAssistant, relay: Any, hass_client_no_auth: Any
) -> None:
    """The timer frees the bytes; the expiry itself is judged at every request."""
    grant = relay.grant(NODE_ID, "127.0.0.1", _package())
    grant.expires = dt_util.utcnow().timestamp() - 1
    client = await hass_client_no_auth()

    response = await client.get(RELAY_PATH + grant.token)

    assert response.status == 404

"""The relay under attack and at its edges - the security review of the first version.

Each test here failed against that version: an answer being made when the entry unloads, the
address binding at its realistic edge (a neighbour on the receiver's own subnet, a forwarded
header, the token in the log), warnings any broker client could multiply, a forged request that
spent the receiver's minute, an address the receiver's own check refuses, a grant replaced while
it was being fetched, and refusals nobody but the log heard about.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import ipaddress
import json
import logging
import re
from typing import Any
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from homeassistant.components.http import ApiConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.enigma2_mqtt import release_index, release_package
from custom_components.enigma2_mqtt.relay import (
    RELAY_LIFETIME,
    RELAY_PATH,
    RELAY_WARNING_WINDOW,
    RelayError,
    RelayView,
    async_get_relay,
    async_relay_base,
    relay_url_ok,
)
from custom_components.enigma2_mqtt.release_package import PackageError
from custom_components.enigma2_mqtt.release_store import async_release_index_cache

from .conftest import INFO, INFO_TOPIC, NODE_ID
from .signed_index import keyset, release, sign
from .test_relay import (
    ADAPTERS,
    FETCH,
    JUDGE,
    REQUEST,
    REQUEST_TOPIC,
    _adapter,
    _answers,
    _ask,
    _box,
    _judge,
    _package,
    lan,  # noqa: F401 - a fixture
    relay,  # noqa: F401 - a fixture
)

NOTIFY = "custom_components.enigma2_mqtt.relay.persistent_notification.async_create"
DISMISS = "custom_components.enigma2_mqtt.relay.persistent_notification.async_dismiss"
RECEIVER = "192.0.2.12"
NEIGHBOUR = "192.0.2.13"
# The receiver's own check of an address from Home Assistant (plugin `updatehelper.py`,
# RELAY_URL): repeated here so that a change on either side is a failing test, not a refusal
# on the television.
PLUGIN_RELAY_URL = re.compile(
    r"https?://[A-Za-z0-9.-]{1,253}(?::([0-9]{1,5}))?/api/enigma2_mqtt/relay/[A-Za-z0-9_-]{43}"
)


def _direct(
    view: RelayView, token: str, remote: Any, headers: dict[str, str] | None = None
) -> tuple[Any, Any]:
    """A request that reaches the view itself, with no middleware in front of it."""
    request = make_mocked_request("GET", RELAY_PATH + token, headers=headers or {})
    return request.clone(remote=remote), request


async def _get(
    view: RelayView, token: str, remote: Any, headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    request, original = _direct(view, token, remote, headers)
    response = await view.get(request, token)
    writer = original._payload_writer  # noqa: SLF001 - the mocked transport's writer
    body = b"".join(call.args[0] for call in writer.write.call_args_list)
    return response.status, body


# ------------------------------------------------------------- the unload race --


async def test_unloading_while_an_answer_is_made_leaves_no_grant_no_answer_and_no_wait(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,  # noqa: F811
) -> None:
    """The download is held while the entry unloads: the unload must not wait for it, and
    what it would have produced - a grant, an answer on the broker - must never appear."""
    gate = asyncio.Event()

    async def slow_fetch(_hass: Any, version: str) -> Any:
        await gate.wait()
        return _package(version)

    await _box(hass, config_entry)
    with patch(FETCH, side_effect=slow_fetch):
        async_fire_mqtt_message(hass, REQUEST_TOPIC, json.dumps(REQUEST))
        for _ in range(5):
            await asyncio.sleep(0)
        unload = hass.async_create_task(hass.config_entries.async_unload(config_entry.entry_id))
        await asyncio.wait({unload}, timeout=2)
        finished_while_held = unload.done()
        gate.set()
        assert await unload
        await hass.async_block_till_done(wait_background_tasks=True)

    assert finished_while_held, "the unload waited for the answer"
    assert async_get_relay(hass).grants(NODE_ID) == []
    assert _answers(mqtt_mock) == []
    assert config_entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize("where", ["judge", "address", "download"])
async def test_an_entry_that_began_to_unload_during_a_wait_gets_nothing(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    where: str,
) -> None:
    """Whatever the answer waited for, the entry is asked again before a grant or a publish."""

    def unloading() -> None:
        config_entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)

    async def judge(_hass: Any, version: str) -> Any:
        if where == "judge":
            unloading()
        return _judge(_hass, version)

    async def adapters(_hass: Any) -> Any:
        if where == "address":
            unloading()
        return [_adapter("192.0.2.5", 24)]

    async def fetch(_hass: Any, version: str) -> Any:
        if where == "download":
            unloading()
        return _package(version)

    await _box(hass, config_entry)
    with (
        patch(JUDGE, side_effect=judge, create=True),
        patch(ADAPTERS, side_effect=adapters),
        patch(FETCH, side_effect=fetch),
    ):
        await _ask(hass, REQUEST)

    assert async_get_relay(hass).grants(NODE_ID) == []
    assert _answers(mqtt_mock) == []
    config_entry.mock_state(hass, ConfigEntryState.LOADED)


# ------------------------------------------------------------ the address binding --


async def test_a_neighbour_on_the_receiver_s_own_subnet_gets_404(
    hass: HomeAssistant,
    relay: Any,  # noqa: F811
) -> None:
    """The realistic attacker is on the receiver's LAN, not on another network."""
    grant = relay.grant(NODE_ID, RECEIVER, _package())
    view = RelayView(relay)

    assert await _get(view, grant.token, NEIGHBOUR) == (404, b"")
    assert await _get(view, grant.token, "192.0.2.1") == (404, b"")
    # The same call from the receiver itself is served: the view really ran.
    assert await _get(view, grant.token, RECEIVER) == (200, b"signed package bytes")


@pytest.mark.parametrize(
    "header",
    ["X-Forwarded-For", "X-Real-IP", "Forwarded"],
)
async def test_the_view_judges_the_peer_never_a_forwarded_header(
    hass: HomeAssistant,
    relay: Any,  # noqa: F811
    header: str,
) -> None:
    """No middleware here: the view is called with a peer and a header that disagree."""
    grant = relay.grant(NODE_ID, RECEIVER, _package())
    view = RelayView(relay)
    naming = {"Forwarded": f"for={RECEIVER}"}.get(header, RECEIVER)
    naming_the_neighbour = {"Forwarded": f"for={NEIGHBOUR}"}.get(header, NEIGHBOUR)

    assert await _get(view, grant.token, NEIGHBOUR, {header: naming}) == (404, b"")
    assert await _get(view, grant.token, RECEIVER, {header: naming_the_neighbour}) == (
        200,
        b"signed package bytes",
    )


@pytest.mark.parametrize(
    ("http", "headers", "status"),
    [
        # A proxy trusted to say who asked, which passes a client's header on unchanged.
        (
            {"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"]},
            {"X-Forwarded-For": RECEIVER},
            200,
        ),
        # The same proxy appending the real client, as a correct proxy does.
        (
            {"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"]},
            {"X-Forwarded-For": f"{RECEIVER}, 203.0.113.9"},
            404,
        ),
        # No proxy configured: Home Assistant refuses the header itself.
        ({}, {"X-Forwarded-For": RECEIVER}, 400),
    ],
    ids=["trusted proxy passes the header on", "trusted proxy appends", "no proxy configured"],
)
async def test_the_binding_is_as_strong_as_trusted_proxies(
    hass: HomeAssistant,
    hass_client_no_auth: Any,
    http: dict[str, Any],
    headers: dict[str, str],
    status: int,
) -> None:
    """The three behaviours DOCUMENTATION.md states next to the reverse-proxy sentence."""
    assert await async_setup_component(hass, "http", {"http": http})
    grants = async_get_relay(hass)
    grant = grants.grant(NODE_ID, RECEIVER, _package())
    client = await hass_client_no_auth()

    response = await client.get(RELAY_PATH + grant.token, headers=headers)

    assert response.status == status
    grants.clear()


async def test_the_whole_token_reaches_no_log_line_at_any_level(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Offered, served, refused to another address, asked for again, expired: every record."""
    caplog.set_level(logging.DEBUG)
    await _box(hass, config_entry)
    with patch(FETCH, AsyncMock(return_value=_package())):
        await _ask(hass, REQUEST)
        async_get_relay(hass)._answered[NODE_ID] -= 61  # noqa: SLF001
        await _ask(hass, {**REQUEST, "id": "again"})
    (grant,) = async_get_relay(hass).grants(NODE_ID)
    view = RelayView(async_get_relay(hass))
    assert (await _get(view, grant.token, RECEIVER))[0] == 200
    assert (await _get(view, grant.token, NEIGHBOUR))[0] == 404
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RELAY_LIFETIME + 1))
    await hass.async_block_till_done()

    assert len(_answers(mqtt_mock)) == 2
    # Every record of every logger - except Home Assistant's MQTT client, which traces each
    # message it carries at DEBUG, the address included, as it does for every other topic.
    lines = [
        record.getMessage()
        for record in caplog.records
        if not record.name.startswith("homeassistant.components.mqtt")
    ]
    assert any(grant.token[:6] in line for line in lines)
    assert not [line for line in lines if grant.token in line]


# --------------------------------------------------------------------- the log --


async def test_a_flood_of_requests_writes_a_warning_per_kind_not_per_message(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """200 malformed requests, then 200 well-formed ones from any broker client."""
    await _box(hass, config_entry)
    caplog.clear()
    with patch(FETCH, AsyncMock(return_value=_package())) as fetch:
        for _ in range(200):
            async_fire_mqtt_message(hass, REQUEST_TOPIC, "x")
        await hass.async_block_till_done(wait_background_tasks=True)
        for number in range(200):
            async_fire_mqtt_message(
                hass, REQUEST_TOPIC, json.dumps({**REQUEST, "id": f"n{number}"})
            )
        await hass.async_block_till_done(wait_background_tasks=True)

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name.endswith(".relay")
    ]
    assert 1 <= len(warnings) <= 2
    assert fetch.await_count == 1


async def test_refused_fetches_from_another_address_are_warned_once_a_window(
    hass: HomeAssistant,
    relay: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anyone who read the token on the broker can ask from anywhere; the log says so once."""
    grant = relay.grant(NODE_ID, RECEIVER, _package())
    view = RelayView(relay)

    for number in range(50):
        assert (await _get(view, grant.token, f"198.51.100.{number + 1}"))[0] == 404

    def warnings() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING and record.name.endswith(".relay")
        ]

    assert len(warnings()) == 1
    # A window later the next one is a warning again, and says what was held back.
    relay._warned[(NODE_ID, "address")][0] -= RELAY_WARNING_WINDOW  # noqa: SLF001
    await _get(view, grant.token, NEIGHBOUR)
    assert len(warnings()) == 2
    assert "49" in warnings()[1]


# ------------------------------------------------------------------ the minute --


@pytest.fixture
def test_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_index, "EMBEDDED", keyset("test"))


async def _hold(hass: HomeAssistant, *versions: str) -> None:
    """Make Home Assistant hold a verified index listing `versions`."""
    cache = async_release_index_cache(hass)
    await cache.async_load()
    raw, _ = sign(1, [release(version) for version in versions], floor="0.2.0")
    cache.index = json.loads(raw)
    cache.integration_version = "0.4.0"


async def test_a_request_the_rule_refuses_does_not_spend_the_receiver_s_minute(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    test_keys: Any,
) -> None:
    """A broker client asks for a version nobody has; the receiver asks 30 s later.

    The real index and the real rule judge both - no stand-in for the judge.
    """
    await _box(hass, config_entry)
    await _hold(hass, "0.3.0")
    with (
        patch(ADAPTERS, AsyncMock(return_value=[_adapter("192.0.2.5", 24)])),
        patch(FETCH, AsyncMock(side_effect=lambda _hass, version: _package(version))) as fetch,
    ):
        await _ask(hass, {"id": "evil", "version": "9.9.9", "serial": 1})
        if NODE_ID in async_get_relay(hass)._answered:  # noqa: SLF001
            async_get_relay(hass)._answered[NODE_ID] -= 30  # noqa: SLF001
        await _ask(hass, REQUEST)

    assert [answer["id"] for answer in _answers(mqtt_mock)] == [REQUEST["id"]]
    fetch.assert_awaited_once_with(hass, "0.3.0")


async def test_a_live_grant_answers_again_without_a_download_or_the_minute(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,  # noqa: F811
) -> None:
    """A broker client asks first for the version the receiver wants; the receiver still
    gets its answer, from the same grant, and nothing is downloaded twice."""
    await _box(hass, config_entry)
    with patch(FETCH, AsyncMock(return_value=_package())) as fetch:
        await _ask(hass, {**REQUEST, "id": "forged"})
        async_get_relay(hass)._answered[NODE_ID] -= 30  # noqa: SLF001
        await _ask(hass, REQUEST)

    answers = _answers(mqtt_mock)
    assert [answer["id"] for answer in answers] == ["forged", REQUEST["id"]]
    assert answers[0]["url"] == answers[1]["url"]
    assert fetch.await_count == 1


# ------------------------------------------------------------ the address shape --


@pytest.mark.parametrize(
    ("local_ip", "internal", "ssl", "expected"),
    [
        ("2001:db8::5", None, False, None),
        ("198.51.100.2", "http://user:secret@198.51.100.2:8123", False, "http://198.51.100.2:8123"),
        ("198.51.100.2", "http://ha_box.lan:8123", False, "http://198.51.100.2:8123"),
        ("198.51.100.2", "http://[fd00::5]:8123", False, "http://198.51.100.2:8123"),
        ("198.51.100.2", "http://[fd00::5]:8123", True, None),
        ("198.51.100.2", "http://homeassistant.local:8123", False, "http://homeassistant.local:8123"),
        ("198.51.100.2", "https://ha.example.com", False, "https://ha.example.com"),
    ],
    ids=[
        "IPv6 local address",
        "user and password",
        "underscore",
        "IPv6 internal URL",
        "IPv6 internal URL with TLS",
        "a host name",
        "https host name",
    ],
)
async def test_every_address_offered_is_one_the_receiver_accepts(
    hass: HomeAssistant,
    local_ip: str,
    internal: str | None,
    ssl: bool,
    expected: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The receiver refuses anything but its one shape; Home Assistant must not offer it."""
    caplog.set_level(logging.DEBUG)
    hass.config.api = ApiConfig(local_ip, "0.0.0.0", 8123, ssl)
    await hass.config.async_update(internal_url=internal)
    with patch(ADAPTERS, AsyncMock(return_value=[])):
        if expected is None:
            with pytest.raises(RelayError) as raised:
                await async_relay_base(hass, ipaddress.IPv4Address(RECEIVER))
            assert raised.value.key == "relay_unreachable"
            assert "secret" not in str(raised.value.translation_placeholders)
        else:
            base = await async_relay_base(hass, ipaddress.IPv4Address(RECEIVER))
            assert base.rstrip("/") == expected
            assert PLUGIN_RELAY_URL.fullmatch(base.rstrip("/") + RELAY_PATH + "A" * 43)
    # This integration's lines only: Home Assistant traces its own configuration at DEBUG.
    assert not [
        record
        for record in caplog.records
        if record.name.startswith("custom_components") and "secret" in record.getMessage()
    ]


@pytest.mark.parametrize(
    ("local_ip", "answered"),
    [("198.51.100.2", "http://198.51.100.2:8123"), ("2001:db8::5", None)],
    ids=["own IPv4 address instead", "nothing to offer"],
)
async def test_a_user_and_password_never_reach_the_broker(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    local_ip: str,
    answered: str | None,
) -> None:
    await _box(hass, config_entry)
    hass.config.api = ApiConfig(local_ip, "0.0.0.0", 8123, False)
    await hass.config.async_update(internal_url="http://user:secret@198.51.100.2:8123")
    with (
        patch(ADAPTERS, AsyncMock(return_value=[])),
        patch(JUDGE, AsyncMock(side_effect=_judge)),
        patch(FETCH, AsyncMock(return_value=_package())),
    ):
        await _ask(hass, REQUEST)

    urls = [answer["url"].split("/api/", 1)[0] for answer in _answers(mqtt_mock)]
    assert urls == ([answered] if answered else [])
    assert not [
        call for call in mqtt_mock.async_publish.call_args_list if "secret" in str(call.args[1])
    ]


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("http://192.0.2.5:8123" + RELAY_PATH + "A" * 43, True),
        ("https://ha.example.com" + RELAY_PATH + "A" * 43, True),
        ("http://192.0.2.5:0" + RELAY_PATH + "A" * 43, False),
        ("http://192.0.2.5:65536" + RELAY_PATH + "A" * 43, False),
        ("http://192.0.2.5:8123" + RELAY_PATH + "A" * 42, False),
        ("http://192.0.2.5:8123" + RELAY_PATH + "A" * 43 + "?x=1", False),
        ("http://u:p@192.0.2.5:8123" + RELAY_PATH + "A" * 43, False),
        ("http://[fd00::5]:8123" + RELAY_PATH + "A" * 43, False),
        ("ftp://192.0.2.5" + RELAY_PATH + "A" * 43, False),
    ],
)
def test_the_relay_url_check_is_the_receiver_s(url: str, accepted: bool) -> None:
    plugin = PLUGIN_RELAY_URL.fullmatch(url)
    plugin_accepts = plugin is not None and (
        plugin.group(1) is None or 0 < int(plugin.group(1)) < 65536
    )
    assert plugin_accepts is accepted
    assert relay_url_ok(url) is accepted


async def test_never_the_cloud_address(hass: HomeAssistant) -> None:
    hass.config.api = None
    await hass.config.async_update(internal_url=None, external_url=None)
    with (
        patch(ADAPTERS, AsyncMock(return_value=[])),
        patch(
            "homeassistant.helpers.network._get_cloud_url",
            return_value="https://abcdefgh.ui.nabu.casa",
        ),
        pytest.raises(RelayError),
    ):
        await async_relay_base(hass, ipaddress.IPv4Address(RECEIVER))


# ------------------------------------------------------------- a grant in use --


async def test_a_grant_is_marked_as_being_fetched_while_the_view_sends_it(
    hass: HomeAssistant,
    relay: Any,  # noqa: F811
) -> None:
    grant = relay.grant(NODE_ID, RECEIVER, _package())
    seen: list[int] = []
    original = web.StreamResponse.write

    async def write(self: Any, data: bytes) -> None:
        seen.append(grant.fetching)
        await original(self, data)

    with patch.object(web.StreamResponse, "write", write):
        status, _ = await _get(RelayView(relay), grant.token, RECEIVER)

    assert status == 200
    assert seen == [1]
    assert grant.fetching == 0


async def test_a_grant_being_fetched_is_neither_replaced_nor_evicted(
    hass: HomeAssistant,
    relay: Any,  # noqa: F811
) -> None:
    grants = [
        relay.grant(NODE_ID, RECEIVER, _package(version)) for version in ("0.2.0", "0.3.0")
    ]
    grants[0].fetching = 1

    # The same version for another address would replace it.
    with pytest.raises(RelayError) as raised:
        relay.grant(NODE_ID, NEIGHBOUR, _package("0.2.0"))
    assert raised.value.key == "relay_busy"
    assert relay.lookup(grants[0].token) is grants[0]

    # A third and a fourth version: the one being fetched stays, the other goes.
    relay.grant(NODE_ID, RECEIVER, _package("0.4.0"))
    relay.grant(NODE_ID, RECEIVER, _package("0.5.0"))
    assert relay.lookup(grants[0].token) is grants[0]
    assert relay.lookup(grants[1].token) is None


async def test_a_new_address_or_new_bytes_replace_the_grant_of_a_version(
    hass: HomeAssistant,
    relay: Any,  # noqa: F811
) -> None:
    """The receiver's DHCP lease changed, or the index names other bytes: a fresh grant."""
    first = relay.grant(NODE_ID, RECEIVER, _package())
    moved = relay.grant(NODE_ID, "192.0.2.77", _package())
    assert moved.token != first.token
    assert relay.lookup(first.token) is None
    assert moved.address == ipaddress.IPv4Address("192.0.2.77")

    other = relay.grant(
        NODE_ID,
        "192.0.2.77",
        replace(_package(), data=b"other signed bytes", sha256="f" * 64),
    )
    assert other.token != moved.token
    assert relay.lookup(moved.token) is None
    assert [grant.token for grant in relay.grants(NODE_ID)] == [other.token]


# ---------------------------------------------------------- refusals, visibly --


def _notified(create: Any) -> list[tuple[str, str]]:
    return [(call.args[1], call.kwargs["notification_id"]) for call in create.call_args_list]


@pytest.mark.parametrize(
    ("setup", "sentence"),
    [
        ("no ipv4", "The receiver did not report an IPv4 address"),
        ("no address", "Home Assistant does not know its address on the local network"),
        ("ipv6 address", "The receiver could not download the plugin from Home Assistant (http://[2001:db8::5]:8123)"),
        ("download", "Could not download plugin 0.3.0"),
    ],
)
async def test_a_refusal_is_said_where_the_household_looks(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    setup: str,
    sentence: str,
) -> None:
    await _box(hass, config_entry, **({"ip": None} if setup == "no ipv4" else {}))
    if setup == "no address":
        hass.config.api = None
        await hass.config.async_update(internal_url=None, external_url=None)
    if setup == "ipv6 address":
        hass.config.api = ApiConfig("2001:db8::5", "0.0.0.0", 8123, False)
        await hass.config.async_update(internal_url=None)
    fetch = (
        AsyncMock(side_effect=PackageError(release_package.DOWNLOAD, "no", {"version": "0.3.0"}))
        if setup == "download"
        else AsyncMock(return_value=_package())
    )
    adapters = [_adapter("192.0.2.5", 24)] if setup in ("no ipv4", "download") else []
    with (
        patch(JUDGE, AsyncMock(side_effect=_judge)),
        patch(ADAPTERS, AsyncMock(return_value=adapters)),
        patch(FETCH, fetch),
        patch(NOTIFY) as create,
    ):
        await _ask(hass, REQUEST)

    assert _answers(mqtt_mock) == []
    ((message, notification_id),) = _notified(create)
    assert message.startswith(sentence)
    assert notification_id == f"enigma2_mqtt_relay_{NODE_ID}"
    assert create.call_args.kwargs["title"] == (
        f"{config_entry.title}: the plugin could not be shared"
    )


async def test_an_answer_the_receiver_never_fetched_says_so_when_it_expires(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,  # noqa: F811
) -> None:
    await _box(hass, config_entry)
    with patch(FETCH, AsyncMock(return_value=_package())), patch(NOTIFY) as create:
        await _ask(hass, REQUEST)
        (answer,) = _answers(mqtt_mock)
        assert create.call_count == 0
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RELAY_LIFETIME + 1))
        await hass.async_block_till_done()

    ((message, _),) = _notified(create)
    assert message.startswith(
        "The receiver could not download the plugin from Home Assistant (http://192.0.2.5:8123"
    )
    token = answer["url"].rsplit("/", 1)[1]
    assert token not in message


async def test_an_answer_the_receiver_fetched_says_nothing_and_clears_an_old_notice(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,  # noqa: F811
) -> None:
    await _box(hass, config_entry)
    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch(NOTIFY) as create,
        patch(DISMISS) as dismiss,
    ):
        await _ask(hass, REQUEST)
        (grant,) = async_get_relay(hass).grants(NODE_ID)
        assert (await _get(RelayView(async_get_relay(hass)), grant.token, RECEIVER))[0] == 200
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RELAY_LIFETIME + 1))
        await hass.async_block_till_done()

    assert create.call_count == 0
    dismiss.assert_called_with(hass, f"enigma2_mqtt_relay_{NODE_ID}")


# ----------------------------------------------------------------- the edges --


async def test_a_request_over_the_size_limit_gets_no_answer(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,  # noqa: F811
) -> None:
    await _box(hass, config_entry)
    body = json.dumps(REQUEST)
    with patch(FETCH, AsyncMock(return_value=_package())) as fetch:
        await _ask(hass, body + " " * (1025 - len(body)))
        await _ask(hass, body + " " * (1024 - len(body)))

    assert fetch.await_count == 1


@pytest.mark.parametrize("address", ["0.0.0.0", "224.0.0.1", "255.255.255.255"])
async def test_an_address_no_receiver_can_have_gets_no_grant(
    hass: HomeAssistant,
    relay: Any,  # noqa: F811
    address: str,
) -> None:
    with pytest.raises(RelayError) as raised:
        relay.grant(NODE_ID, address, _package())

    assert raised.value.key == "relay_no_ipv4"


async def test_the_forged_info_address_is_the_only_one_served(
    hass: HomeAssistant,
    mqtt_mock: Any,
    box_on_the_broker: Any,
    config_entry: MockConfigEntry,
    lan: Any,  # noqa: F811
) -> None:
    """A live `info` from any broker client moves the binding (review A5): the grant then
    serves that address and no other - still only the public signed package."""
    await _box(hass, config_entry)
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "ip": "192.0.2.66"}))
    await hass.async_block_till_done()
    with patch(FETCH, AsyncMock(return_value=_package())):
        await _ask(hass, REQUEST)

    (grant,) = async_get_relay(hass).grants(NODE_ID)
    assert grant.address == ipaddress.IPv4Address("192.0.2.66")
    view = RelayView(async_get_relay(hass))
    assert (await _get(view, grant.token, RECEIVER))[0] == 404

"""The signed release index as Home Assistant fetches, judges, keeps and announces it.

One cache serves every receiver, in Home Assistant's storage. These tests drive it the way a
household does - the option, the daily timer, a reload, a restart, the button, Home Assistant's
own "Check for updates" - with the origin answered by `aioclient_mock` and the index signed with
the vectors' throwaway test keys, which replace the embedded release keys for the test.
"""

from __future__ import annotations

import base64
from datetime import timedelta
import json
import logging
import ssl
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.mqtt.const import MQTT_CONNECTION_STATE
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.enigma2_mqtt import release_index, release_store
from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_CHECK_GITHUB_RELEASES,
    CONF_NAME,
    CONF_NODE_ID,
    DOMAIN,
    LEGACY_RELEASE_CHECK_STORAGE_KEY,
    PLUGIN_INDEX_ORIGIN,
    RELEASE_CHECK_INTERVAL,
    RELEASE_INDEX_STORAGE_KEY,
    RELEASE_INDEX_STORAGE_VERSION,
    RELEASE_MANUAL_INTERVAL,
    TOPIC_RELEASE_INDEX,
)

from .conftest import BASE_TOPIC, async_setup_box
from .signed_index import keyset, release, sign

INDEX_URL = PLUGIN_INDEX_ORIGIN + "releases.json"
SIG_URL = PLUGIN_INDEX_ORIGIN + "releases.json.sig"
PLUGIN = "update.dekoder_salon_plugin"
CHECK = "button.dekoder_salon_check_for_plugin_updates"
NOTIFY = "custom_components.enigma2_mqtt.release_store.persistent_notification.async_create"

RELEASES = [release("0.4.0"), release("0.3.0"), release("0.2.0")]


@pytest.fixture(autouse=True)
def test_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judge by the vectors' test keys: nobody signs with the release keys but CI."""
    monkeypatch.setattr(release_index, "EMBEDDED", keyset("test"))


def _serve(
    aioclient_mock: AiohttpClientMocker,
    pair: tuple[bytes, bytes],
    *,
    etag: str | None = '"one"',
    status: int = 200,
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        INDEX_URL, status=status, content=pair[0], headers={"ETag": etag} if etag else None
    )
    aioclient_mock.get(SIG_URL, content=pair[1])


async def _setup_checking(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the receiver up with the daily check on, as the options flow would."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_CHECK_GITHUB_RELEASES: True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _published(mqtt_mock: Any) -> list[Any]:
    return [
        call for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == TOPIC_RELEASE_INDEX
    ]


def _relayed(mqtt_mock: Any) -> list[bytes]:
    """The distinct indexes relayed on the retained topic, in the order they first went out.

    The held index is relayed again on every setup and reconnect, so what a test asks is which
    indexes were relayed, not how often.
    """
    seen: list[bytes] = []
    for call in _published(mqtt_mock):
        index = base64.b64decode(json.loads(call.args[1])["index"])
        if index not in seen:
            seen.append(index)
    return seen


def _cache(hass: HomeAssistant) -> release_store.ReleaseIndexCache:
    return release_store.async_release_index_cache(hass)


# ------------------------------------------------------------ asking, and not asking --


async def test_off_by_default_nothing_is_asked(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Off means this integration talks to nothing but the broker."""
    await async_setup_box(hass, config_entry)

    assert aioclient_mock.call_count == 0
    assert _published(mqtt_mock) == []
    state = hass.states.get(PLUGIN)
    assert state.attributes["index_serial"] is None
    assert state.attributes["available_versions"] == []


async def test_a_new_index_is_accepted_announced_and_relayed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """V5-S7: every newly accepted index is a warning and a notification, then a retained topic."""
    pair = sign(1, RELEASES)
    _serve(aioclient_mock, pair)
    with patch(NOTIFY) as notify, caplog.at_level(logging.WARNING):
        await _setup_checking(hass, config_entry)

    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [INDEX_URL, SIG_URL]
    state = hass.states.get(PLUGIN)
    assert state.attributes["index_serial"] == 1
    assert state.attributes["check_error"] is None
    assert [item["version"] for item in state.attributes["available_versions"]] == [
        "0.4.0",
        "0.3.0",
        "0.2.0",
    ]

    warning = [
        record
        for record in caplog.records
        if "Accepted a new signed plugin release index" in record.message
    ]
    assert len(warning) == 1
    assert warning[0].levelno == logging.WARNING
    assert "serial 1" in warning[0].message
    assert "0.2.0, 0.3.0, 0.4.0" in warning[0].message

    assert notify.call_count == 1
    message = notify.call_args.args[1]
    key_id = keyset("test")[0].key_id
    for fragment in ("serial 1", key_id, "0.2.0, 0.3.0, 0.4.0", "Withdrawn: none", "0.2.0."):
        assert fragment in message
    assert notify.call_args.kwargs["notification_id"] == f"{DOMAIN}_release_index_{key_id}_1"

    assert _relayed(mqtt_mock) == [pair[0]]
    relayed = _published(mqtt_mock)
    topic, payload, qos, retain = relayed[0].args[:4]
    assert (topic, qos, retain) == (TOPIC_RELEASE_INDEX, 1, True)
    body = json.loads(payload)
    assert base64.b64decode(body["index"]) == pair[0]
    assert base64.b64decode(body["sig"]) == pair[1]


async def test_two_receivers_make_one_request(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The index is the same for every receiver; so is the answer."""
    _serve(aioclient_mock, sign(1, RELEASES))
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Dekoder sypialnia",
        unique_id="vuduo4k_005302",
        data={CONF_NODE_ID: "vuduo4k_005302", CONF_BASE_TOPIC: BASE_TOPIC, CONF_NAME: "Sypialnia"},
        options={CONF_CHECK_GITHUB_RELEASES: True},
    )
    other.add_to_hass(hass)
    with patch(NOTIFY):
        # Setting up the first loads every entry of the domain, the second included.
        await _setup_checking(hass, config_entry)
    assert other.state is ConfigEntryState.LOADED

    assert aioclient_mock.call_count == 2
    assert len(_relayed(mqtt_mock)) == 1


async def test_the_daily_check_asks_once_a_day_with_the_etag(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An unchanged index costs one `304`, and announces nothing."""
    pair = sign(1, RELEASES)
    _serve(aioclient_mock, pair, etag='"v1"')
    with patch(NOTIFY) as notify:
        await _setup_checking(hass, config_entry)
        assert aioclient_mock.call_count == 2

        # A timer that goes off early is the case the stored stamp exists for.
        freezer.tick(RELEASE_CHECK_INTERVAL / 2)
        async_fire_time_changed(hass, fire_all=True)
        await hass.async_block_till_done()
        assert aioclient_mock.call_count == 2

        _serve(aioclient_mock, pair, status=304, etag='"v1"')
        published = len(_published(mqtt_mock))
        freezer.tick(RELEASE_CHECK_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        # A check that finds nothing new relays nothing: only setup and reconnect repeat it.
        assert len(_published(mqtt_mock)) == published

    assert aioclient_mock.call_count == 1
    assert aioclient_mock.mock_calls[0][3] == {"If-None-Match": '"v1"'}
    assert notify.call_count == 1
    assert len(_relayed(mqtt_mock)) == 1
    assert hass.states.get(PLUGIN).attributes["check_error"] is None


async def test_the_same_bytes_again_are_nothing_new(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A re-delivered index is not a failure, and not news either."""
    pair = sign(1, RELEASES)
    _serve(aioclient_mock, pair, etag=None)
    with patch(NOTIFY) as notify:
        await _setup_checking(hass, config_entry)
        cache = _cache(hass)
        cache.checked = dt_util.utcnow() - RELEASE_MANUAL_INTERVAL - timedelta(seconds=1)
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)
        await hass.async_block_till_done()

    assert aioclient_mock.call_count == 4
    assert notify.call_count == 1
    assert len(_relayed(mqtt_mock)) == 1
    assert hass.states.get(PLUGIN).attributes["check_error"] is None


async def test_six_reloads_are_still_one_request(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A limit that lived on the entity was reset by every reload."""
    _serve(aioclient_mock, sign(1, RELEASES))
    with patch(NOTIFY):
        await _setup_checking(hass, config_entry)
        for _ in range(5):
            await hass.config_entries.async_reload(config_entry.entry_id)
            await hass.async_block_till_done()

    assert aioclient_mock.call_count == 2
    assert hass.states.get(PLUGIN).attributes["index_serial"] == 1


async def test_a_check_that_failed_has_still_spent_the_day(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Stamped before the request: a service that is down is not asked again on every reload."""
    aioclient_mock.get(INDEX_URL, status=403)
    await _setup_checking(hass, config_entry)
    assert aioclient_mock.call_count == 1
    assert hass.states.get(PLUGIN).attributes["check_error"] == "http_error"

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1


async def test_a_restart_within_the_day_asks_nothing_and_still_knows(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The stamp, the index and the trust memory outlive the process."""
    index, sig = sign(3, RELEASES)
    keys = keyset("test")
    trust = release_index.store(
        None, keys, {"serials": {keys[0].key_id: 3}, "silenced": []}, acceptance=False
    )
    hass_storage[RELEASE_INDEX_STORAGE_KEY] = {
        "version": RELEASE_INDEX_STORAGE_VERSION,
        "data": {
            "index": base64.b64encode(index).decode(),
            "sig": base64.b64encode(sig).decode(),
            "serial": 3,
            "key_id": keys[0].key_id,
            "etag": '"x"',
            "fetched": (dt_util.utcnow() - timedelta(hours=3)).isoformat(),
            "checked": (dt_util.utcnow() - timedelta(hours=3)).isoformat(),
            "check_error": None,
            "trust": trust,
        },
    }
    await _setup_checking(hass, config_entry)

    assert aioclient_mock.call_count == 0
    state = hass.states.get(PLUGIN)
    assert state.attributes["index_serial"] == 3
    assert state.attributes["published_version"] == "0.4.0"


async def test_a_stored_index_that_no_longer_verifies_is_not_shown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """A file changed on disk, or a key a release dropped: not an index this reader accepts."""
    index, sig = sign(3, RELEASES)
    hass_storage[RELEASE_INDEX_STORAGE_KEY] = {
        "version": RELEASE_INDEX_STORAGE_VERSION,
        "data": {
            "index": base64.b64encode(index.replace(b"0.4.0", b"0.9.0")).decode(),
            "sig": base64.b64encode(sig).decode(),
            "checked": dt_util.utcnow().isoformat(),
            "trust": None,
        },
    }
    await async_setup_box(hass, config_entry)

    assert hass.states.get(PLUGIN).attributes["index_serial"] is None


async def test_the_old_per_receiver_records_are_dropped(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """The release check's store of 0.3.x, one record per receiver, has nothing to carry over."""
    hass_storage[LEGACY_RELEASE_CHECK_STORAGE_KEY] = {
        "version": 1,
        "data": {config_entry.entry_id: {"checked": "2026-09-01T00:00:00+00:00"}},
    }
    await async_setup_box(hass, config_entry)
    await hass.async_block_till_done()

    assert LEGACY_RELEASE_CHECK_STORAGE_KEY not in hass_storage


# --------------------------------------------------------------------- refusals --


@pytest.mark.parametrize(
    ("stored", "offered", "reason"),
    [
        # A lower or equal serial is a replay, whatever the bytes.
        (5, sign(5, RELEASES[:1]), "replay"),
        (5, sign(4, RELEASES), "replay"),
        # More than a thousand above the last accepted one.
        (5, sign(1006, RELEASES), "jump"),
        # The index names another key than the one that signed it.
        (5, sign(6, RELEASES, named_key="14282ed755f38d55"), "key_mismatch"),
    ],
    ids=["equal serial", "lower serial", "jump", "key mismatch"],
)
async def test_an_index_the_rule_refuses_changes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    stored: int,
    offered: tuple[bytes, bytes],
    reason: str,
) -> None:
    index, sig = sign(stored, RELEASES)
    keys = keyset("test")
    hass_storage[RELEASE_INDEX_STORAGE_KEY] = {
        "version": RELEASE_INDEX_STORAGE_VERSION,
        "data": {
            "index": base64.b64encode(index).decode(),
            "sig": base64.b64encode(sig).decode(),
            "checked": (dt_util.utcnow() - timedelta(days=2)).isoformat(),
            "trust": release_index.store(
                None, keys, {"serials": {keys[0].key_id: stored}, "silenced": []},
                acceptance=False,
            ),
        },
    }
    _serve(aioclient_mock, offered, etag=None)
    with patch(NOTIFY) as notify:
        await _setup_checking(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["check_error"] == reason
    assert state.attributes["index_serial"] == stored
    assert notify.call_count == 0
    # The held index is relayed again at setup; the refused one never is.
    assert _relayed(mqtt_mock) == [index]


async def test_after_the_spare_key_the_main_key_is_refused_for_good(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """One index signed by the higher-ranked key silences the lower one on this reader."""
    _serve(aioclient_mock, sign(1, RELEASES, key="t2"))
    with patch(NOTIFY):
        await _setup_checking(hass, config_entry)
        assert hass.states.get(PLUGIN).attributes["index_serial"] == 1

        _serve(aioclient_mock, sign(50, RELEASES, key="t1"))
        _cache(hass).checked = dt_util.utcnow() - timedelta(minutes=11)
        with pytest.raises(HomeAssistantError, match="invalid signature or is older"):
            await hass.services.async_call(
                "button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True
            )

    assert hass.states.get(PLUGIN).attributes["check_error"] == "rank"
    stored = _cache(hass).trust["release"]
    assert keyset("test")[0].key_id in next(iter(stored.values()))["silenced"]


@pytest.mark.parametrize(
    ("index", "reason"),
    [
        (sign(1, RELEASES)[0].replace(b'"serial": 1', b'"serial": 2'), "bad_signature"),
        (b"x" * (release_index.MAX_INDEX_BYTES + 1), "too_large"),
    ],
    ids=["tampered", "over 64 KiB"],
)
async def test_an_index_that_is_not_signed_or_too_large_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    index: bytes,
    reason: str,
) -> None:
    _serve(aioclient_mock, (index, sign(1, RELEASES)[1]))
    with patch(NOTIFY) as notify:
        await _setup_checking(hass, config_entry)
    # A signature that does not verify is read once more before it is believed.
    assert aioclient_mock.call_count == (4 if reason == "bad_signature" else 2)

    state = hass.states.get(PLUGIN)
    assert state.attributes["check_error"] == reason
    assert state.attributes["index_serial"] is None
    assert notify.call_count == 0
    assert _published(mqtt_mock) == []


@pytest.mark.parametrize(("status", "reason"), [(302, "redirect"), (301, "redirect"),
                                                 (404, "http_error"), (500, "http_error")])
async def test_a_redirect_or_an_error_is_not_followed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    reason: str,
) -> None:
    """The origin serves the files itself; a redirect is somebody else's answer."""
    aioclient_mock.get(
        INDEX_URL, status=status, headers={"Location": "https://evil.example/releases.json"}
    )
    await _setup_checking(hass, config_entry)

    assert hass.states.get(PLUGIN).attributes["check_error"] == reason
    assert aioclient_mock.call_count == 1


async def test_an_unreachable_origin_is_a_code_not_an_exception(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.get(INDEX_URL, exc=TimeoutError())
    await _setup_checking(hass, config_entry)

    assert hass.states.get(PLUGIN).attributes["check_error"] == "unreachable"


async def test_a_damaged_trust_memory_judges_nothing_and_is_kept(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Read as "nothing remembered" it would put the reader back at first sight."""
    damaged = {"schema": 1, "release": {"not-a-fingerprint": {}}, "acceptance": {}}
    hass_storage[RELEASE_INDEX_STORAGE_KEY] = {
        "version": RELEASE_INDEX_STORAGE_VERSION,
        "data": {"trust": damaged},
    }
    _serve(aioclient_mock, sign(1, RELEASES))
    with patch(NOTIFY) as notify:
        await _setup_checking(hass, config_entry)
    await hass.async_block_till_done()

    assert hass.states.get(PLUGIN).attributes["check_error"] == "bad_memory"
    assert notify.call_count == 0
    assert hass_storage[RELEASE_INDEX_STORAGE_KEY]["data"]["trust"] == damaged


async def test_a_withdrawal_is_announced(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    _serve(aioclient_mock, sign(1, RELEASES))
    with patch(NOTIFY) as notify:
        await _setup_checking(hass, config_entry)
        _serve(
            aioclient_mock,
            sign(2, [release("0.4.0", withdrawn="it breaks EPG"), *RELEASES[1:]]),
        )
        _cache(hass).checked = dt_util.utcnow() - timedelta(minutes=11)
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)

    message = notify.call_args.args[1]
    assert "serial 2" in message
    assert "Added: none" in message
    assert "Withdrawn: 0.4.0" in message
    listed = hass.states.get(PLUGIN).attributes["available_versions"]
    assert "0.4.0" not in [item["version"] for item in listed]
    assert len(_relayed(mqtt_mock)) == 2


# ---------------------------------------------------------------- the manual limit --


async def test_the_button_checks_once_in_ten_minutes_and_says_when(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A press is consent to ask, even with the daily check off - once in ten minutes."""
    await async_setup_box(hass, config_entry)
    _serve(aioclient_mock, sign(1, RELEASES))
    with patch(NOTIFY):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)
    assert aioclient_mock.call_count == 2
    checked = dt_util.as_local(_cache(hass).checked).strftime("%H:%M")

    # 6.5 minutes left is said as 7: "in 6 min" would send somebody back too early.
    freezer.tick(timedelta(minutes=3, seconds=30))
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)
    assert raised.value.translation_key == "release_check_rate_limited"
    assert raised.value.translation_placeholders == {"time": checked, "minutes": "7"}
    assert aioclient_mock.call_count == 2

    freezer.tick(timedelta(minutes=7))
    with patch(NOTIFY):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)
    assert aioclient_mock.call_count == 4


async def test_check_for_updates_shares_the_limit_and_says_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Home Assistant's own "Check for updates" asks every update entity at once."""
    assert await async_setup_component(hass, "homeassistant", {})
    _serve(aioclient_mock, sign(1, RELEASES))
    with patch(NOTIFY):
        await _setup_checking(hass, config_entry)
    assert aioclient_mock.call_count == 2

    # Inside the ten minutes: nothing asked, and nothing raised.
    await hass.services.async_call(
        "homeassistant", "update_entity", {ATTR_ENTITY_ID: PLUGIN}, blocking=True
    )
    assert aioclient_mock.call_count == 2
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)

    freezer.tick(RELEASE_MANUAL_INTERVAL + timedelta(seconds=1))
    await hass.services.async_call(
        "homeassistant", "update_entity", {ATTR_ENTITY_ID: PLUGIN}, blocking=True
    )
    assert aioclient_mock.call_count == 4


async def test_check_for_updates_asks_nothing_while_the_option_is_off(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    await async_setup_box(hass, config_entry)
    await hass.services.async_call(
        "homeassistant", "update_entity", {ATTR_ENTITY_ID: PLUGIN}, blocking=True
    )
    assert aioclient_mock.call_count == 0


async def test_a_failed_press_says_so_in_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await async_setup_box(hass, config_entry)
    aioclient_mock.get(INDEX_URL, exc=TimeoutError())
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)
    assert raised.value.translation_key == "release_check_failed"
    assert raised.value.translation_placeholders == {"reason": "unreachable"}


async def test_the_button_works_while_the_receiver_sleeps(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The index is not the receiver's."""
    box_on_the_broker[f"{BASE_TOPIC}/vuuno4kse_005301/availability"] = "offline"
    await async_setup_box(hass, config_entry)
    assert hass.states.get(CHECK).state != "unavailable"


# ------------------------------------------------------------------ how it is asked --


class _Response:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    @property
    def content(self) -> Any:
        body = self._body

        class _Content:
            async def iter_chunked(self, size: int):
                for start in range(0, len(body), size):
                    yield body[start : start + size]

        return _Content()


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return self.response


async def test_the_origin_is_asked_with_verified_tls_and_no_redirects() -> None:
    session = _Session(_Response(200, b"{}"))
    status, body, _ = await release_store._async_get(session, INDEX_URL, 1024, {})

    assert (status, body) == (200, b"{}")
    ((url, kwargs),) = session.calls
    assert url == INDEX_URL
    assert kwargs["allow_redirects"] is False
    context = kwargs["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_a_body_is_read_to_one_byte_past_its_limit_and_no_further() -> None:
    session = _Session(_Response(200, b"x" * 100_000))
    _, body, _ = await release_store._async_get(session, INDEX_URL, 1024, {})
    assert len(body) == 1025


def test_an_unverified_context_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A change anywhere else must not quietly turn the fetch into an unverified one."""
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(release_store, "get_default_context", lambda: unverified)
    with pytest.raises(RuntimeError, match="verifying"):
        release_store.verified_context()


# ------------------------------------------------------------------ recovery and repair --


async def test_a_dropped_index_is_taken_back_when_the_same_one_is_fetched(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """The stored bytes were damaged; the memory says serial 3 was accepted. The same genuine
    serial 3 is not a replay of anything held: it is taken back, silently - nothing new was
    learned, so nothing is announced or relayed."""
    index, sig = sign(3, RELEASES)
    keys = keyset("test")
    hass_storage[RELEASE_INDEX_STORAGE_KEY] = {
        "version": RELEASE_INDEX_STORAGE_VERSION,
        "data": {
            "index": base64.b64encode(index.replace(b"0.4.0", b"0.9.0")).decode(),
            "sig": base64.b64encode(sig).decode(),
            "checked": (dt_util.utcnow() - timedelta(days=2)).isoformat(),
            "trust": release_index.store(
                None, keys, {"serials": {keys[0].key_id: 3}, "silenced": []},
                acceptance=False,
            ),
        },
    }
    _serve(aioclient_mock, (index, sig))
    with patch(NOTIFY) as notify:
        await _setup_checking(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["check_error"] is None
    assert state.attributes["index_serial"] == 3
    assert notify.call_count == 0
    # Relayed, because a verified index is held again - but announced to nobody.
    assert _relayed(mqtt_mock) == [index]

    # A genuinely older serial is still a replay.
    _serve(aioclient_mock, sign(2, RELEASES))
    _cache(hass).checked = dt_util.utcnow() - timedelta(minutes=11)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)
    assert hass.states.get(PLUGIN).attributes["check_error"] == "replay"
    assert hass.states.get(PLUGIN).attributes["index_serial"] == 3


async def test_a_stamp_in_the_future_blocks_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A host that booted with its clock ahead wrote it; it is not a check that happened."""
    await async_setup_box(hass, config_entry)
    cache = _cache(hass)
    await cache.async_load()
    cache.checked = dt_util.utcnow() + timedelta(days=1)
    _serve(aioclient_mock, sign(1, RELEASES))
    with patch(NOTIFY):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)
    assert aioclient_mock.call_count == 2
    assert hass.states.get(PLUGIN).attributes["index_serial"] == 1


async def test_the_daily_check_runs_every_day_not_every_other(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The stamp is taken a moment after the timer starts, so the timer's next firing is a
    moment short of a day after it."""
    _serve(aioclient_mock, sign(1, RELEASES))
    with patch(NOTIFY):
        await _setup_checking(hass, config_entry)
        cache = _cache(hass)
        cache.checked = cache.checked + timedelta(milliseconds=50)
        _serve(aioclient_mock, sign(1, RELEASES))
        freezer.tick(RELEASE_CHECK_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert aioclient_mock.call_count >= 1


async def test_the_held_index_is_relayed_again_on_setup_and_on_reconnect(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A broker restarted without persistence, or a retained topic overwritten, is repaired
    from the verified copy Home Assistant holds - which a receiver sees as nothing new."""
    index, sig = sign(3, RELEASES)
    keys = keyset("test")
    hass_storage[RELEASE_INDEX_STORAGE_KEY] = {
        "version": RELEASE_INDEX_STORAGE_VERSION,
        "data": {
            "index": base64.b64encode(index).decode(),
            "sig": base64.b64encode(sig).decode(),
            "checked": dt_util.utcnow().isoformat(),
            "trust": release_index.store(
                None, keys, {"serials": {keys[0].key_id: 3}, "silenced": []},
                acceptance=False,
            ),
        },
    }
    with patch(NOTIFY) as notify:
        await async_setup_box(hass, config_entry)
        relayed = _published(mqtt_mock)
        assert len(relayed) == 1
        body = json.loads(relayed[0].args[1])
        assert base64.b64decode(body["index"]) == index
        assert relayed[0].args[3] is True

        async_dispatcher_send(hass, MQTT_CONNECTION_STATE, True)
        await hass.async_block_till_done()
        assert len(_published(mqtt_mock)) == 2
    assert notify.call_count == 0
    assert aioclient_mock.call_count == 0


async def test_nothing_unverified_is_relayed_on_setup(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    index, sig = sign(3, RELEASES)
    hass_storage[RELEASE_INDEX_STORAGE_KEY] = {
        "version": RELEASE_INDEX_STORAGE_VERSION,
        "data": {
            "index": base64.b64encode(index.replace(b"0.4.0", b"0.9.0")).decode(),
            "sig": base64.b64encode(sig).decode(),
            "checked": dt_util.utcnow().isoformat(),
            "trust": None,
        },
    }
    await async_setup_box(hass, config_entry)
    assert _published(mqtt_mock) == []


def _sequence(*bodies: bytes):
    """A side effect answering each request with the next body, then the last one again."""
    remaining = list(bodies)

    async def _answer(method, url, data):
        body = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return AiohttpClientMockResponse(method, url, response=body)

    return _answer


async def test_a_pair_caught_mid_publication_is_read_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The index and its signature are two requests; a publication can land between them."""
    old, new = sign(1, RELEASES[1:]), sign(2, RELEASES)
    await async_setup_box(hass, config_entry)
    _serve(aioclient_mock, old)
    with patch(NOTIFY):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)

    aioclient_mock.clear_requests()
    aioclient_mock.get(INDEX_URL, side_effect=_sequence(new[0], new[0]))
    aioclient_mock.get(SIG_URL, side_effect=_sequence(old[1], new[1]))
    _cache(hass).checked = dt_util.utcnow() - timedelta(minutes=11)
    with patch(NOTIFY) as notify, caplog.at_level(logging.WARNING):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)

    assert aioclient_mock.call_count == 4
    # The second read asks for the whole pair again: a 304 there would leave a torn pair.
    assert aioclient_mock.mock_calls[0][3] == {"If-None-Match": '"one"'}
    assert not aioclient_mock.mock_calls[2][3]
    assert hass.states.get(PLUGIN).attributes["index_serial"] == 2
    assert not [r for r in caplog.records if "refused" in r.message]
    assert notify.call_count == 1


async def test_a_pair_that_stays_torn_is_said_to_be_unverified_not_forged(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    old, new = sign(1, RELEASES[1:]), sign(2, RELEASES)
    await async_setup_box(hass, config_entry)
    _serve(aioclient_mock, old)
    with patch(NOTIFY):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)

    _serve(aioclient_mock, (new[0], old[1]))
    _cache(hass).checked = dt_util.utcnow() - timedelta(minutes=11)
    with patch(NOTIFY) as notify, pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)

    assert raised.value.translation_key == "release_check_unverified"
    assert aioclient_mock.call_count == 4
    assert hass.states.get(PLUGIN).attributes["check_error"] == "bad_signature"
    assert hass.states.get(PLUGIN).attributes["index_serial"] == 1
    notify.assert_not_called()


async def test_a_lower_serial_is_never_taken_back(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Holding nothing is not a reason to believe an older index: only the very serial the
    memory remembers is taken back."""
    index, sig = sign(3, RELEASES)
    keys = keyset("test")
    hass_storage[RELEASE_INDEX_STORAGE_KEY] = {
        "version": RELEASE_INDEX_STORAGE_VERSION,
        "data": {
            "index": base64.b64encode(index.replace(b"0.4.0", b"0.9.0")).decode(),
            "sig": base64.b64encode(sig).decode(),
            "checked": (dt_util.utcnow() - timedelta(days=2)).isoformat(),
            "trust": release_index.store(
                None, keys, {"serials": {keys[0].key_id: 3}, "silenced": []},
                acceptance=False,
            ),
        },
    }
    _serve(aioclient_mock, sign(2, RELEASES))
    with patch(NOTIFY):
        await _setup_checking(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["check_error"] == "replay"
    assert state.attributes["index_serial"] is None
    assert _relayed(mqtt_mock) == []


async def test_a_held_index_that_no_longer_verifies_is_not_relayed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """What goes out in this integration's name is checked again on the way out."""
    await async_setup_box(hass, config_entry)
    cache = _cache(hass)
    index, sig = sign(3, RELEASES)
    cache.index_raw, cache.signature_raw = index.replace(b"0.4.0", b"0.9.0"), sig
    await cache.async_republish()
    assert _published(mqtt_mock) == []
    cache.index_raw = index
    await cache.async_republish()
    assert _relayed(mqtt_mock) == [index]

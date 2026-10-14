"""The user's own go2rtc, kept on the channel the receiver is on.

While an address is set in the options, one stream of that go2rtc is re-pointed with
`PUT <address>/api/streams?name=<name>&src=<source>` when the service changes. It is
off while the address is empty, it never re-points to a hidden channel, in standby or
to a stream that wants a login, and a failure is reported once.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
from pathlib import Path
import re
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.enigma2_mqtt import own_go2rtc as go2rtc
from custom_components.enigma2_mqtt.const import (
    CONF_BOUQUETS,
    CONF_DANGEROUS_BUTTONS,
    CONF_GO2RTC_SOURCE,
    CONF_GO2RTC_STREAM,
    CONF_GO2RTC_URL,
    DEFAULT_GO2RTC_SOURCE,
    DOMAIN,
)
from custom_components.enigma2_mqtt.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    IP,
    NODE_ID,
    POWER_TOPIC,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    async_setup_box,
    async_setup_box_then_retained,
)

STREAM_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/stream"
STREAM: dict[str, Any] = {
    "port": 8001, "path": "/{sref}", "authentication": False, "encoder": False,
    "encoder_port": None,
}
GO2RTC = "http://192.0.2.50:1984"
GO2RTC_HOST = "192.0.2.50"
API = f"{GO2RTC}/api/streams"
NAME = f"enigma2_{NODE_ID}"
PLAYER = "media_player.dekoder_salon"
EUROSPORT = "1:0:19:1234:3F3:1:C00000:0:0:0:"
SERVICE_TWO = {**SERVICE, "sref": SREF_TWO, "name": "TVN HD"}
SERVICE_SPORT = {**SERVICE, "sref": EUROSPORT, "name": "Eurosport 1", "bouquet": "Sport"}


def source_for(sref: str, template: str = DEFAULT_GO2RTC_SOURCE) -> str:
    return template.replace("{url}", f"http://{IP}:8001/{sref}")


def with_options(entry: MockConfigEntry, **options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        options=options,
    )


def following(entry: MockConfigEntry, **options: Any) -> MockConfigEntry:
    return with_options(entry, **{CONF_GO2RTC_URL: GO2RTC, **options})


def streaming(store: dict[str, str | bytes], stream: dict[str, Any] = STREAM) -> None:
    store[INFO_TOPIC] = json.dumps(
        {**INFO, "capabilities": [*INFO["capabilities"], "stream"]}
    )
    store[STREAM_TOPIC] = json.dumps(stream)


def puts(aioclient_mock: AiohttpClientMocker) -> list[Any]:
    """Return the address of every PUT sent to the go2rtc, in order."""
    return [
        url
        for method, url, *_ in aioclient_mock.mock_calls
        if url.host == GO2RTC_HOST and method.upper() == "PUT"
    ]


def reads(aioclient_mock: AiohttpClientMocker) -> list[Any]:
    """Return the address of every read-back sent to the go2rtc, in order."""
    return [
        url
        for method, url, *_ in aioclient_mock.mock_calls
        if url.host == GO2RTC_HOST and method.upper() == "GET"
    ]


def holding(
    aioclient_mock: AiohttpClientMocker,
    source: str | None = None,
    *,
    suffix: str = "",
    status: int = 200,
    pad: int = 0,
) -> Any:
    """Return a read-back that answers with the source of the last PUT, or this one."""

    async def _answer(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        held = source if source is not None else puts(aioclient_mock)[-1].query["src"]
        body: dict[str, Any] = {"producers": [{"url": held + suffix}], "consumers": None}
        if pad:
            body["pad"] = "x" * pad
        return AiohttpClientMockResponse(method, url, status=status, json=body)

    return _answer


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def zap(hass: HomeAssistant, service: dict[str, Any]) -> None:
    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(service))
    await settle(hass)


def answering(aioclient_mock: AiohttpClientMocker, **response: Any) -> None:
    """Make the go2rtc answer differently from now on; the receiver still has no picons."""
    aioclient_mock.clear_requests()
    aioclient_mock.put(API, **response)
    aioclient_mock.get(re.compile(rf"^http://{re.escape(IP)}/"), status=404)


def issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"go2rtc_{entry.entry_id}")


# ------------------------------------------------------------- when it re-points


async def test_it_is_off_while_the_address_is_empty(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Nothing is sent anywhere, and the player names no go2rtc stream."""
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await zap(hass, SERVICE_TWO)

    assert puts(aioclient_mock) == []
    assert "go2rtc_stream" not in hass.states.get(PLAYER).attributes


async def test_the_first_service_after_start_points_the_stream(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """One PUT, with the name and the source in the query and nothing else."""
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    ((method, url, data),) = [
        (method, url, data)
        for method, url, data, *_ in aioclient_mock.mock_calls
        if url.host == GO2RTC_HOST
    ]
    assert method.upper() == "PUT"
    assert f"{url.scheme}://{url.host}:{url.port}{url.path}" == API
    assert dict(url.query) == {"name": NAME, "src": source_for(SREF)}
    assert not data
    # As it goes over the wire: no space, and the `#` of the source is not a fragment.
    assert " " not in str(url)
    assert "#" not in str(url)
    assert url.fragment == ""
    assert hass.states.get(PLAYER).attributes["go2rtc_stream"] == NAME


async def test_a_channel_change_re_points_and_the_same_channel_again_does_not(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`service` is published again when the resolution becomes known; that is no zap."""
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, following(config_entry), box_on_the_broker)
    await settle(hass)

    await zap(hass, {**SERVICE, "width": 1280, "height": 720})
    assert len(puts(aioclient_mock)) == 1

    await zap(hass, SERVICE_TWO)
    assert [url.query["src"] for url in puts(aioclient_mock)] == [
        source_for(SREF),
        source_for(SREF_TWO),
    ]


async def test_the_name_and_the_template_are_the_options(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A named deinterlace template of the user's own go2rtc, and a name of their own."""
    template = "ffmpeg:{url}#video=tv#audio=opus"
    aioclient_mock.put(f"{GO2RTC}/prefix/api/streams", text="")
    streaming(box_on_the_broker)
    entry = with_options(
        config_entry,
        **{
            CONF_GO2RTC_URL: f"{GO2RTC}/prefix",
            CONF_GO2RTC_STREAM: "salon",
            CONF_GO2RTC_SOURCE: template,
        },
    )
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    (url,) = puts(aioclient_mock)
    assert url.path == "/prefix/api/streams"
    assert dict(url.query) == {"name": "salon", "src": source_for(SREF, template)}
    assert hass.states.get(PLAYER).attributes["go2rtc_stream"] == "salon"


async def test_a_hidden_channel_leaves_the_stream_on_the_last_shown_one(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Nothing is sent for it - not its address, and not a request that says "nothing"."""
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    entry = following(config_entry, history_hidden_bouquets=["Sport"])
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    await zap(hass, SERVICE_SPORT)
    assert len(puts(aioclient_mock)) == 1
    assert EUROSPORT not in str(puts(aioclient_mock)[0])

    await zap(hass, SERVICE_TWO)
    assert [url.query["src"] for url in puts(aioclient_mock)] == [
        source_for(SREF),
        source_for(SREF_TWO),
    ]


async def test_a_receiver_that_starts_on_a_hidden_channel_sends_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(SERVICE_SPORT)
    entry = following(config_entry, history_hidden_bouquets=["Sport"])
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    assert puts(aioclient_mock) == []


async def test_standby_does_not_re_point(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """As the player keeps its last picture in standby, the stream keeps its channel."""
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, following(config_entry), box_on_the_broker)
    await settle(hass)

    async_fire_mqtt_message(hass, POWER_TOPIC, "standby")
    await zap(hass, SERVICE_TWO)
    assert len(puts(aioclient_mock)) == 1

    async_fire_mqtt_message(hass, POWER_TOPIC, "on")
    await settle(hass)
    assert [url.query["src"] for url in puts(aioclient_mock)] == [
        source_for(SREF),
        source_for(SREF_TWO),
    ]


async def test_a_stream_server_that_wants_a_login_is_never_pointed_at(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker, {**STREAM, "authentication": True})
    await async_setup_box_then_retained(hass, following(config_entry), box_on_the_broker)
    await settle(hass)
    await zap(hass, SERVICE_TWO)

    assert puts(aioclient_mock) == []


async def test_a_receiver_that_does_not_claim_stream_is_never_pointed_at(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An address left in the options of a receiver whose plugin was put back."""
    aioclient_mock.put(API, text="")
    box_on_the_broker[STREAM_TOPIC] = json.dumps(STREAM)
    await async_setup_box_then_retained(hass, following(config_entry), box_on_the_broker)
    await settle(hass)

    assert puts(aioclient_mock) == []
    assert "go2rtc_stream" not in hass.states.get(PLAYER).attributes


# --------------------------------------------------------------------- failures


async def test_a_refusal_is_reported_once_and_cleared_when_it_works_again(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One line and one repair for three failed zaps; both end with the first success."""
    aioclient_mock.put(API, status=400, text="source with spaces may be insecure")
    streaming(box_on_the_broker)
    entry = following(config_entry)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.own_go2rtc")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)
    await zap(hass, SERVICE_TWO)
    await zap(hass, SERVICE)

    assert len(puts(aioclient_mock)) == 3
    assert caplog.text.count("did not take the stream") == 1
    assert "HTTP 400" in caplog.text
    raised = issue(hass, entry)
    assert raised is not None
    assert raised.translation_key == "go2rtc_refused"
    assert raised.translation_placeholders == {
        "name": "Dekoder salon",
        "host": GO2RTC_HOST,
        "stream": NAME,
        "status": "400",
    }
    assert raised.is_fixable is False
    # The player is untouched by any of it.
    assert hass.states.get(PLAYER).state == "playing"

    aioclient_mock.clear_requests()
    aioclient_mock.put(API, text="")
    await zap(hass, SERVICE_TWO)

    assert issue(hass, entry) is None
    assert caplog.text.count("takes the stream") == 1
    assert caplog.text.count("did not take the stream") == 1


@pytest.mark.parametrize("status", [200, 500])
async def test_a_channel_changed_while_a_request_runs_is_sent_when_it_returns(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    status: int,
) -> None:
    """Whether the first request worked or not, the newer channel goes out at once."""
    zapped: list[bool] = []

    async def _answer(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        if not zapped:
            zapped.append(True)
            # Let the request be under way first, as a real one is before it answers.
            await asyncio.sleep(0)
            async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(SERVICE_TWO))
            return AiohttpClientMockResponse(method, url, status=status)
        return AiohttpClientMockResponse(method, url, status=200)

    aioclient_mock.put(API, side_effect=_answer)
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    assert [url.query["src"] for url in puts(aioclient_mock)] == [
        source_for(SREF),
        source_for(SREF_TWO),
    ]
    assert issue(hass, entry) is None


async def test_a_redirect_is_not_go2rtc_taking_the_stream(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A 3xx is a failure, and the request asks aiohttp not to follow one.

    The mocked session never follows a redirect, so the status alone would pass with
    the flag gone; what the request was sent with is read off the session itself.
    """
    sent_with: list[Any] = []
    session = async_get_clientsession(hass)
    answer = aioclient_mock.match_request

    async def _request(method: str, url: Any, **kwargs: Any) -> Any:
        if URL(str(url)).host == GO2RTC_HOST:
            sent_with.append(kwargs.get("allow_redirects"))
        return await answer(method, url, **kwargs)

    object.__setattr__(session, "_request", _request)
    aioclient_mock.put(API, status=302, headers={"Location": "http://192.0.2.51/"})
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    # The PUT, and the read-back of its error answer.
    assert sent_with == [False, False]
    raised = issue(hass, entry)
    assert raised is not None
    assert raised.translation_placeholders["status"] == "302"


async def test_a_stored_name_or_template_that_cannot_be_used_is_the_default(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Options edited by hand past the form: nothing go2rtc would refuse is sent."""
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    entry = following(
        config_entry,
        **{CONF_GO2RTC_STREAM: "salon tv", CONF_GO2RTC_SOURCE: "ffmpeg:{url} #video=h264"},
    )
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    (url,) = puts(aioclient_mock)
    assert url.query["name"] == NAME
    assert url.query["src"] == source_for(SREF)


async def test_a_failure_that_changes_its_kind_is_still_one_warning(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """502, then no answer, then 503: one line until a request works; the repair follows."""
    aioclient_mock.put(API, status=502, text="bad gateway")
    streaming(box_on_the_broker)
    entry = following(config_entry)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.own_go2rtc")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)
    assert issue(hass, entry).translation_placeholders["status"] == "502"

    answering(aioclient_mock, exc=aiohttp.ClientConnectionError("refused"))
    await zap(hass, SERVICE_TWO)
    assert issue(hass, entry).translation_key == "go2rtc_unreachable"

    answering(aioclient_mock, status=503, text="unavailable")
    await zap(hass, SERVICE)

    assert caplog.text.count("did not take the stream") == 1
    raised = issue(hass, entry)
    assert raised.translation_key == "go2rtc_refused"
    assert raised.translation_placeholders["status"] == "503"
    assert entry.runtime_data.go2rtc.async_status()["http_status"] == 503


@pytest.mark.parametrize("failure", [aiohttp.ClientConnectionError("refused"), TimeoutError()])
async def test_a_go2rtc_that_does_not_answer_is_a_repair_and_raises_nowhere(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    aioclient_mock.put(API, exc=failure)
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    raised = issue(hass, entry)
    assert raised is not None
    assert raised.translation_key == "go2rtc_unreachable"
    assert set(raised.translation_placeholders) == {"name", "host", "stream"}
    assert "no answer" in caplog.text
    assert "Traceback" not in caplog.text
    assert hass.states.get(PLAYER).attributes["stream_url"] == f"http://{IP}:8001/{SREF}"


async def test_a_failed_request_is_tried_again_after_five_minutes_and_not_per_message(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The same channel published again does not retry; the timer does."""
    aioclient_mock.put(API, status=503)
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)
    await zap(hass, {**SERVICE, "width": 1280})
    await zap(hass, {**SERVICE, "width": 1920})
    assert len(puts(aioclient_mock)) == 1

    aioclient_mock.clear_requests()
    aioclient_mock.put(API, text="")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await settle(hass)

    assert [url.query["src"] for url in puts(aioclient_mock)] == [source_for(SREF)]
    assert issue(hass, entry) is None


async def test_no_login_typed_into_the_address_reaches_a_log_a_repair_or_the_download(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The address may carry a user name and a password for go2rtc's API."""
    aioclient_mock.put(API, status=401)
    streaming(box_on_the_broker)
    entry = following(config_entry, **{CONF_GO2RTC_URL: "http://admin:s3cret@192.0.2.50:1984"})
    caplog.set_level(logging.DEBUG, logger="custom_components.enigma2_mqtt")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    assert len(puts(aioclient_mock)) == 1
    raised = issue(hass, entry)
    assert raised is not None
    download = await async_get_config_entry_diagnostics(hass, entry)

    for text in (
        caplog.text,
        json.dumps(raised.translation_placeholders),
        json.dumps(download),
        json.dumps(dict(hass.states.get(PLAYER).attributes), default=str),
    ):
        assert "s3cret" not in text
        assert "admin" not in text
    assert download["entry"]["options"][CONF_GO2RTC_URL] == "**REDACTED**"
    assert download["box"]["go2rtc"] == {
        "state": "failed",
        "stream": NAME,
        "problem": "refused",
        "http_status": 401,
        "requests": 1,
        "unsaved": False,
    }


async def test_the_download_says_off_without_an_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await async_setup_box(hass, config_entry)

    download = await async_get_config_entry_diagnostics(hass, config_entry)

    assert download["box"]["go2rtc"]["state"] == "off"


async def test_emptying_the_address_takes_the_repair_away(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Switched off, it has nothing left to complain about."""
    aioclient_mock.put(API, status=500)
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)
    assert issue(hass, entry) is not None

    hass.config_entries.async_update_entry(entry, options={})
    await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)

    assert issue(hass, entry) is None


# ------------------------------------------------------------------ the options


def test_what_is_stored_and_what_is_refused() -> None:
    """Only what was set is stored, and each field has its own sentence."""
    check = go2rtc.validate_options

    assert check({}, NODE_ID) == ({}, None)
    assert check(
        {CONF_GO2RTC_URL: " ", CONF_GO2RTC_STREAM: NAME,
         CONF_GO2RTC_SOURCE: DEFAULT_GO2RTC_SOURCE},
        NODE_ID,
    ) == ({}, None)
    assert check({CONF_GO2RTC_URL: f" {GO2RTC}/ "}, NODE_ID) == (
        {CONF_GO2RTC_URL: GO2RTC},
        None,
    )
    assert check({CONF_GO2RTC_STREAM: "salon-tv.1"}, NODE_ID) == (
        {CONF_GO2RTC_STREAM: "salon-tv.1"},
        None,
    )
    for url in (
        "192.0.2.50:1984",
        "ftp://192.0.2.50",
        "http://",
        "http://192.0.2.50:1984/api/streams?name=x",
        "http://192.0.2.50:1984/#x",
        "http://192.0.2.50:99999",
        "http://192.0.2.50 :1984",
    ):
        assert check({CONF_GO2RTC_URL: url}, NODE_ID)[1] == "go2rtc_invalid_url", url
    for name in ("salon tv", "salon/tv", "a" * 65, "żółw"):
        assert check({CONF_GO2RTC_STREAM: name}, NODE_ID)[1] == "go2rtc_invalid_stream"
    for source in (
        "ffmpeg:{url}#video=h264 #audio=opus",
        "ffmpeg:{url}#raw=-vf yadif",
        "ffmpeg:{url}\t#video=h264",
        "ffmpeg: {url}",
    ):
        assert check({CONF_GO2RTC_SOURCE: source}, NODE_ID)[1] == "go2rtc_source_spaces"
    for source in ("ffmpeg:#video=h264", "ffmpeg:{url}{url}", "ffmpeg:{URL}"):
        assert check({CONF_GO2RTC_SOURCE: source}, NODE_ID)[1] == (
            "go2rtc_source_placeholder"
        )
    # The spaces around a template are somebody's paste, not part of it.
    assert check({CONF_GO2RTC_SOURCE: "  ffmpeg:{url}#video=tv\n"}, NODE_ID) == (
        {CONF_GO2RTC_SOURCE: "ffmpeg:{url}#video=tv"},
        None,
    )


def test_the_default_template_is_one_go2rtc_takes() -> None:
    """`ffmpeg:` in front, H.264, Opus for WebRTC and AAC for MSE - and no space."""
    assert DEFAULT_GO2RTC_SOURCE == "ffmpeg:{url}#video=h264#audio=opus#audio=aac"
    assert go2rtc.source_error(DEFAULT_GO2RTC_SOURCE) is None
    assert go2rtc.default_stream_name(NODE_ID) == NAME
    assert go2rtc.default_stream_name("box 1/ä") == "enigma2_box_1"


async def _open_options(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    return result


def _form(**go2rtc_fields: str) -> dict[str, Any]:
    return {CONF_DANGEROUS_BUTTONS: False, CONF_BOUQUETS: [], "go2rtc": go2rtc_fields}


async def test_the_section_is_offered_only_for_a_receiver_with_a_stream_server(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await async_setup_box(hass, config_entry)
    result = await _open_options(hass, config_entry)
    assert "go2rtc" not in result["data_schema"].schema

    async_fire_mqtt_message(
        hass,
        INFO_TOPIC,
        json.dumps({**INFO, "capabilities": [*INFO["capabilities"], "stream"]}),
    )
    await hass.async_block_till_done()
    result = await _open_options(hass, config_entry)
    section = result["data_schema"].schema["go2rtc"]
    assert set(section.schema.schema) == {
        CONF_GO2RTC_URL,
        CONF_GO2RTC_STREAM,
        CONF_GO2RTC_SOURCE,
    }
    # Collapsed until somebody uses it, with the default name to be seen.
    assert section.options == {"collapsed": True}
    suggested = {
        str(key): key.description["suggested_value"] for key in section.schema.schema
    }
    assert suggested[CONF_GO2RTC_STREAM] == NAME


async def test_a_stored_address_is_offered_for_emptying_without_a_stream_server(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver that stopped claiming `stream` must not leave the option stuck on."""
    entry = following(config_entry)
    await async_setup_box(hass, entry)

    result = await _open_options(hass, entry)

    assert "go2rtc" in result["data_schema"].schema


async def test_saving_an_address_stores_it_and_the_defaults_are_not_stored(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    result = await _open_options(hass, config_entry)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _form(
            **{
                CONF_GO2RTC_URL: f"{GO2RTC}/",
                CONF_GO2RTC_STREAM: NAME,
                CONF_GO2RTC_SOURCE: DEFAULT_GO2RTC_SOURCE,
            }
        ),
    )
    await settle(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_GO2RTC_URL] == GO2RTC
    assert CONF_GO2RTC_STREAM not in config_entry.options
    assert CONF_GO2RTC_SOURCE not in config_entry.options


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({CONF_GO2RTC_SOURCE: "ffmpeg:{url}#video=h264 #audio=opus"}, "go2rtc_source_spaces"),
        ({CONF_GO2RTC_SOURCE: "ffmpeg:#video=h264"}, "go2rtc_source_placeholder"),
        ({CONF_GO2RTC_URL: "192.0.2.50:1984"}, "go2rtc_invalid_url"),
        ({CONF_GO2RTC_STREAM: "salon tv"}, "go2rtc_invalid_stream"),
    ],
)
async def test_a_field_that_is_wrong_fails_the_form_and_saves_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    fields: dict[str, str],
    error: str,
) -> None:
    """A template with a space is refused here, where the reason can be read."""
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    result = await _open_options(hass, config_entry)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _form(**{CONF_GO2RTC_URL: GO2RTC, **fields})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert config_entry.options == {}


async def test_the_form_shows_what_is_stored_and_emptying_the_address_switches_it_off(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    aioclient_mock.put(API, text="")
    streaming(box_on_the_broker)
    entry = following(config_entry, **{CONF_GO2RTC_STREAM: "salon"})
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    result = await _open_options(hass, entry)

    section = result["data_schema"].schema["go2rtc"]
    assert section.options == {"collapsed": False}
    suggested = {
        str(key): key.description["suggested_value"] for key in section.schema.schema
    }
    assert suggested == {
        CONF_GO2RTC_URL: GO2RTC,
        CONF_GO2RTC_STREAM: "salon",
        CONF_GO2RTC_SOURCE: DEFAULT_GO2RTC_SOURCE,
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _form(**{CONF_GO2RTC_URL: "", CONF_GO2RTC_STREAM: "salon"}),
    )
    await settle(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_GO2RTC_URL not in entry.options
    assert entry.options[CONF_GO2RTC_STREAM] == "salon"


async def test_a_form_without_the_section_keeps_what_was_stored(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Saving another option does not switch go2rtc off behind somebody's back."""
    entry = following(config_entry, **{CONF_GO2RTC_STREAM: "salon"})
    await async_setup_box(hass, entry)
    result = await _open_options(hass, entry)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DANGEROUS_BUTTONS: True, CONF_BOUQUETS: []}
    )
    await settle(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_GO2RTC_URL] == GO2RTC
    assert entry.options[CONF_GO2RTC_STREAM] == "salon"


def test_home_assistants_own_stream_and_go2rtc_stay_out_of_the_manifest() -> None:
    """No camera entity in this release: it is the user's go2rtc, not Home Assistant's."""
    manifest = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "enigma2_mqtt"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )

    named = {*manifest.get("dependencies", []), *manifest.get("after_dependencies", [])}
    assert not named & {"stream", "go2rtc", "camera"}


# ------------------------------------------- an error answer that go2rtc applied anyway

UNSAVED = "yaml: line 7: did not find expected key"


async def test_a_400_that_go2rtc_applied_anyway_is_a_success_said_once(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """go2rtc 1.9.14 re-points the stream and then fails to write its file: done, said once."""
    aioclient_mock.put(API, status=400, text=UNSAVED)
    aioclient_mock.get(API, side_effect=holding(aioclient_mock))
    streaming(box_on_the_broker)
    entry = following(config_entry)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.own_go2rtc")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)
    await zap(hass, SERVICE_TWO)
    await zap(hass, SERVICE)

    assert [url.query["src"] for url in puts(aioclient_mock)] == [
        source_for(SREF),
        source_for(SREF_TWO),
        source_for(SREF),
    ]
    assert [url.query["src"] for url in reads(aioclient_mock)] == [NAME] * 3
    assert issue(hass, entry) is None
    assert caplog.text.count("could not save it") == 1
    assert f"HTTP 400: {UNSAVED}" in caplog.text
    assert "did not take the stream" not in caplog.text
    status = entry.runtime_data.go2rtc.async_status()
    assert status["state"] == "ok"
    assert status["unsaved"] is True
    # Nothing is retried: the timer would send the same source again.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await settle(hass)
    assert len(puts(aioclient_mock)) == 3


async def test_an_applied_400_ends_a_failure_and_its_repair_as_a_success_does(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """503, then an applied 400, then 503 again: two failure runs, one note on the file."""
    aioclient_mock.put(API, status=503)
    streaming(box_on_the_broker)
    entry = following(config_entry)
    caplog.set_level(logging.INFO, logger="custom_components.enigma2_mqtt.own_go2rtc")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)
    assert issue(hass, entry) is not None

    answering(aioclient_mock, status=400, text=UNSAVED)
    aioclient_mock.get(API, side_effect=holding(aioclient_mock))
    await zap(hass, SERVICE_TWO)
    assert issue(hass, entry) is None

    answering(aioclient_mock, status=503)
    await zap(hass, SERVICE)
    assert issue(hass, entry) is not None

    # A third channel: back on the second one, go2rtc already has it and nothing is sent.
    answering(aioclient_mock, status=400, text=UNSAVED)
    aioclient_mock.get(API, side_effect=holding(aioclient_mock))
    await zap(hass, SERVICE_SPORT)

    assert issue(hass, entry) is None
    assert caplog.text.count("did not take the stream") == 2
    assert caplog.text.count("could not save it") == 1


async def test_a_400_whose_stream_has_another_source_is_a_failure(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aioclient_mock.put(API, status=400, text=UNSAVED)
    aioclient_mock.get(API, side_effect=holding(aioclient_mock, source_for(SREF_TWO)))
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    raised = issue(hass, entry)
    assert raised is not None
    assert raised.translation_placeholders["status"] == "400"
    assert "could not save it" not in caplog.text
    assert entry.runtime_data.go2rtc.async_status()["unsaved"] is False


@pytest.mark.parametrize(
    "read_back",
    [
        {"exc": aiohttp.ClientConnectionError("refused")},
        {"exc": TimeoutError()},
        {"text": "<html>not json</html>"},
        {"json": [{"producers": []}]},
        {"json": {"producers": "ffmpeg"}},
        {"json": {"consumers": None}},
        {"status": 404, "text": "stream not found"},
        {"text": "x" * 70000},
        "longer",
        "matching-404",
        "matching-huge",
    ],
    ids=[
        "error", "timeout", "garbage", "list", "producers", "no-producers", "404", "huge",
        "longer", "matching-404", "matching-huge",
    ],
)
async def test_a_400_that_cannot_be_read_back_is_a_failure(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    read_back: dict[str, Any] | str,
) -> None:
    """Not confirmed is not done: the PUT's failure stands, with its repair and its retry."""
    aioclient_mock.put(API, status=400, text=UNSAVED)
    if read_back == "longer":
        # The source sent, with more behind it, is another source.
        aioclient_mock.get(API, side_effect=holding(aioclient_mock, suffix="#audio=pcm"))
    elif read_back == "matching-404":
        aioclient_mock.get(API, side_effect=holding(aioclient_mock, status=404))
    elif read_back == "matching-huge":
        aioclient_mock.get(API, side_effect=holding(aioclient_mock, pad=70000))
    else:
        aioclient_mock.get(API, **read_back)
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    raised = issue(hass, entry)
    assert raised is not None
    assert raised.translation_key == "go2rtc_refused"
    assert entry.runtime_data.go2rtc.async_status()["state"] == "failed"


async def test_a_500_that_is_not_confirmed_is_reported_as_before(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aioclient_mock.put(API, status=500, text="internal error")
    aioclient_mock.get(API, side_effect=holding(aioclient_mock, "ffmpeg:another#video=h264"))
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)
    await zap(hass, SERVICE_TWO)
    await zap(hass, SERVICE)

    raised = issue(hass, entry)
    assert raised.translation_placeholders["status"] == "500"
    assert caplog.text.count("did not take the stream") == 1
    assert "could not save it" not in caplog.text


async def test_unloading_during_the_read_back_ends_it_quietly(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reading = asyncio.Event()

    async def _never(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        reading.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    aioclient_mock.put(API, status=400, text=UNSAVED)
    aioclient_mock.get(API, side_effect=_never)
    streaming(box_on_the_broker)
    entry = following(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    async with asyncio.timeout(5):
        await reading.wait()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert issue(hass, entry) is None
    assert "Traceback" not in caplog.text
    assert "failed unexpectedly" not in caplog.text


async def test_no_log_line_carries_the_receivers_address_or_channel(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """go2rtc may say back what it was sent; the source names the receiver and the channel."""
    echo = (
        f"cannot save {source_for(SREF)}: {UNSAVED} at http://{IP}:8001/x and {IP} "
        "or 2001:db8::12\nsecond line http://192.0.2.99/"
    )
    aioclient_mock.put(API, status=400, text=echo)
    aioclient_mock.get(API, side_effect=holding(aioclient_mock))
    streaming(box_on_the_broker)
    entry = following(config_entry)
    caplog.set_level(logging.DEBUG, logger="custom_components.enigma2_mqtt")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await settle(hass)

    assert "could not save it" in caplog.text
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.enigma2_mqtt.own_go2rtc")
    )
    assert IP not in ours
    assert SREF not in ours
    assert "2001:db8" not in ours
    assert "192.0.2.99" not in ours
    assert "yaml: line 7" in ours


def test_the_answer_is_cut_and_cleaned_before_it_is_logged() -> None:
    source = source_for(SREF)
    assert go2rtc._log_safe(UNSAVED.encode(), source) == UNSAVED
    assert go2rtc._log_safe(b"", source) == ""
    assert go2rtc._log_safe(b"\n\nlater", source) == ""
    assert go2rtc._log_safe(("x" * 200).encode(), source) == "x" * 80
    assert go2rtc._log_safe(f"bad {source} here".encode(), source) == "bad ... here"
    assert go2rtc._log_safe(b"see rtsp://user:pw@198.51.100.7/a now", source) == "see ... now"
    assert go2rtc._log_safe(b"tab\there \x1b[31mred", source) == "tabhere [31mred"
    assert go2rtc._log_safe(b"\xff\xfe broken", source) == "broken"
    assert go2rtc._log_safe(b"peer 2001:db8::12 gone", source) == "peer ... gone"
    assert go2rtc._log_safe(b"peer 198.51.100.7 gone", source) == "peer ... gone"

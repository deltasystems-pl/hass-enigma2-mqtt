"""The picture on the media player: the screenshot, the channel's picon, and which of them.

The receiver's web interface is answered by `aioclient_mock`, so every request the
integration makes is on record - which is how these tests say not only what picture is
shown, but what the receiver was asked for it, and that nothing but the receiver was asked.
A few tests need what that mock does not keep or do - the arguments of a request, a
request that never ends, two requests at once - and use a small session of their own.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import io
import json
import logging
from pathlib import Path
import re
from typing import Any

import aiohttp
from homeassistant.components.media_player import DATA_COMPONENT, MediaPlayerState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.util.dt as dt_util
from PIL import Image
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

from custom_components.enigma2_mqtt import box as box_module, picture
from custom_components.enigma2_mqtt.box import service_identity
from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_BOUQUETS,
    CONF_DANGEROUS_BUTTONS,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_PLAYER_PICTURE,
    CONF_RECEIVER_HOST,
    CONF_WOL_MAC,
    DEFAULT_PLAYER_PICTURE,
    DOMAIN,
    PLAYER_PICTURE_PICON,
    PLAYER_PICTURE_SCREENSHOT,
)
from custom_components.enigma2_mqtt.diagnostics import async_get_config_entry_diagnostics
from custom_components.enigma2_mqtt.picture import (
    KIND_PICON,
    PENDING,
    Picture,
    PlayerPicture,
    getpicon_reference,
    legacy_picon_link,
    parse_getpicon,
    parse_service_picons,
    picon_picture,
    screenshot_picture,
    valid_picon_link,
)

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    BASE_TOPIC,
    BOX_NAME,
    CHANNELS,
    CHANNELS_TOPIC,
    INFO,
    INFO_TOPIC,
    IP,
    NODE_ID,
    PICON_URL,
    POWER_TOPIC,
    SCREEN,
    SCREEN_TOPIC,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    async_setup_box_then_retained,
    unexpected_receiver_requests,
)

PLAYER = "media_player.dekoder_salon"

BASE = f"http://{IP}"
GETSERVICES = f"{BASE}/api/getservices"
GETPICON = f"{BASE}/api/getpicon"
EVERYTHING = re.compile(rf"^{re.escape(BASE)}/")
ANYWHERE = re.compile(r".")

# Not real pictures: the integration reads the first bytes and nothing else.
PNG_ONE = b"\x89PNG\r\n\x1a\n--the-picon-of-tvp-1--"
PNG_TWO = b"\x89PNG\r\n\x1a\n--the-picon-of-tvn--"
JPEG_OTHER = b"\xff\xd8\xff\xe0--the-picon-of-a-channel-in-no-bouquet--"
NEW_SCREEN = b"\xff\xd8\xff\xdb--a-frame-taken-after-the-zap--\xff\xd9"

# The first link is the name built from the reference; the others are names only
# OpenWebif could have found, by the channel's name.
LINK_ONE = "/picon/1_0_19_283D_3FB_1_C00000_0_0_0.png"
LINK_TWO = "/picon/tvnhd.png"
LINK_OTHER = "/picon/polsathd.png"
LEGACY_TWO = "/picon/1_0_19_2B66_3F3_1_C00000_0_0_0.png"

FAVOURITES = CHANNELS["bouquets"][0]
SPORT = CHANNELS["bouquets"][1]
# A channel no bouquet on offer lists: somebody typed its number on the remote.
SREF_OTHER = "1:0:19:3DCD:640:13E:820000:0:0:0:"
LEGACY_OTHER = "/picon/1_0_19_3DCD_640_13E_820000_0_0_0.png"

TVN = {**SERVICE, "sref": SREF_TWO, "name": "TVN HD"}
POLSAT = {**SERVICE, "sref": SREF_OTHER, "name": "Polsat HD"}

PICONS = {SREF: LINK_ONE, SREF_TWO: LINK_TWO, SREF_OTHER: LINK_OTHER}
FILES = {LINK_ONE: PNG_ONE, LINK_TWO: PNG_TWO, LINK_OTHER: JPEG_OTHER}


def _services(*picons: tuple[str, str, str]) -> str:
    """Return a `getservices?picon=1` answer for (reference, name, picon link) rows."""
    return json.dumps(
        {
            "result": True,
            "processingtime": "0:00:00.012345",
            "pos": len(picons),
            "services": [
                {
                    "pos": index,
                    "picon": picon,
                    "servicename": name,
                    "servicereference": sref,
                    "program": int(sref.split(":")[3], 16),
                }
                for index, (sref, name, picon) in enumerate(picons, 1)
            ],
        }
    )


FAVOURITES_ANSWER = _services((SREF, "TVP 1 HD", LINK_ONE), (SREF_TWO, "TVN HD", LINK_TWO))


def _getpicon_answer(link: str | None) -> dict[str, Any]:
    """Return what `getpicon?json=true` says for a service with this link, or without."""
    if link is None:
        return {"result": False}
    return {"result": True, "path": f"/usr/share/enigma2{link}", "link": link}


def _serve(
    aioclient_mock: AiohttpClientMocker,
    *,
    picons: dict[str, str | None] | None = PICONS,
    services: str | None = FAVOURITES_ANSWER,
    files: dict[str, bytes] = FILES,
) -> None:
    """Put an OpenWebif on the example receiver's address, in place of the last one.

    `picons` is what `getpicon` answers by service reference, with "no picon" for a
    reference it does not hold; None is an OpenWebif without `getpicon`.
    """

    async def _getpicon(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        assert picons is not None
        return AiohttpClientMockResponse(
            method, url, json=_getpicon_answer(picons.get(url.query.get("sRef", "")))
        )

    aioclient_mock.clear_requests()
    if picons is not None:
        aioclient_mock.get(GETPICON, side_effect=_getpicon)
    if services is not None:
        aioclient_mock.get(GETSERVICES, text=services)
    for link, content in files.items():
        aioclient_mock.get(f"{BASE}{link}", content=content)
    # Whatever else is asked does not exist, as on a receiver.
    aioclient_mock.get(EVERYTHING, status=404)


def _asked(aioclient_mock: AiohttpClientMocker) -> list[str]:
    """Return the path of every request made, in order."""
    return [call[1].path for call in aioclient_mock.mock_calls]


def _player(hass: HomeAssistant):
    """Return the media player entity object."""
    return hass.data[DATA_COMPONENT].get_entity(PLAYER)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, mode: str | None = None) -> None:
    """Set the example box up, with the picture option chosen when one is given."""
    entry.add_to_hass(hass)
    if mode is not None:
        hass.config_entries.async_update_entry(entry, options={CONF_PLAYER_PICTURE: mode})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _publish(hass: HomeAssistant, topic: str, payload: Any) -> None:
    """Deliver one payload and let everything it started finish, lookups included."""
    async_fire_mqtt_message(
        hass, topic, payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    )
    await hass.async_block_till_done(wait_background_tasks=True)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return what the picon lookup logged at warning level or above."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == picture.__name__ and record.levelno >= logging.WARNING
    ]


def _clock(resolver: Any, start: float = 1000.0) -> list[float]:
    """Give the lookup a clock the test moves, and return it as a one-item list."""
    now = [start]
    resolver._clock = lambda: now[0]
    return now


async def _browse(hass: HomeAssistant, bouquet: dict[str, Any] = FAVOURITES) -> Any:
    """Open one bouquet in the media browser."""
    return await _player(hass).async_browse_media(media_content_id=f"bouquet:{bouquet['sref']}")


class _FakeResponse:
    """What a request of `_FakeSession` answers."""

    def __init__(self, status: int = 200, body: bytes = b"") -> None:
        self.status = status
        self._body = body
        self.content = self

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _FakeSession:
    """A client session that keeps the arguments of each request and counts overlaps."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.active = 0
        self.peak = 0

    def get(self, url: Any, **kwargs: Any) -> _FakeRequest:
        self.calls.append((url, kwargs))
        return _FakeRequest(self, url)


class _FakeRequest:
    """One request of `_FakeSession`, as the context manager aiohttp returns."""

    def __init__(self, session: _FakeSession, url: Any) -> None:
        self.session = session
        self.url = url

    async def __aenter__(self) -> _FakeResponse:
        self.session.active += 1
        self.session.peak = max(self.session.peak, self.session.active)
        try:
            return await self.session.handler(self.url)
        except BaseException:
            self.session.active -= 1
            raise

    async def __aexit__(self, *_: Any) -> None:
        self.session.active -= 1


async def _openwebif(url: Any) -> _FakeResponse:
    """Answer one request the way the example OpenWebif does, after a moment."""
    await asyncio.sleep(0.01)
    if url.path == "/api/getpicon":
        answer = _getpicon_answer(PICONS.get(url.query.get("sRef", "")))
        return _FakeResponse(200, json.dumps(answer).encode())
    if url.path == "/api/getservices":
        return _FakeResponse(200, FAVOURITES_ANSWER.encode())
    if url.path in FILES:
        return _FakeResponse(200, FILES[url.path])
    return _FakeResponse(404)


# ------------------------------------------------------------- the screenshot setting --


async def test_without_a_channel_change_the_screenshot_is_shown_and_nothing_is_asked(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The default at start-up is what it always was, and costs the receiver nothing."""
    _serve(aioclient_mock)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    assert aioclient_mock.call_count == 0


async def test_a_channel_change_shows_the_picon_until_a_new_screenshot_arrives(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Picon first, screenshot after - and a new hash at each step, or nothing redraws."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    entity = _player(hass)
    hashes = [entity.media_image_hash]

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    hashes.append(entity.media_image_hash)
    # One question about the one service, one picture - and the link is the one
    # OpenWebif named, which the name built from the reference would never have found.
    assert _asked(aioclient_mock) == ["/api/getpicon", LINK_TWO]
    assert aioclient_mock.mock_calls[0][1].query == {"sRef": SREF_TWO, "json": "true"}

    await _publish(hass, SCREEN_TOPIC, NEW_SCREEN)

    assert await entity.async_get_media_image() == (NEW_SCREEN, "image/jpeg")
    hashes.append(entity.media_image_hash)
    assert len(set(hashes)) == 3
    assert aioclient_mock.call_count == 2


async def test_the_picon_stays_while_no_new_screenshot_arrives(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Screenshots switched off on the receiver: the old frame never comes back."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)
    # Anything else the receiver publishes re-reads the state; none of it is a screenshot.
    await _publish(hass, POWER_TOPIC, "on")

    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")


async def test_a_retained_replay_of_the_old_screenshot_is_not_a_new_one(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The same picture delivered again is not a screenshot taken after the change.

    The topic is retained, so the broker replays it to a new subscription, and the plugin
    publishes what it holds again when it reconnects. Either way the bytes are the ones
    Home Assistant already had, with a new arrival time. Sent here without the retain
    flag: Home Assistant's MQTT client drops a second retained delivery on its own, and
    the rule has to hold without that help.
    """
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    entity = _player(hass)
    await _publish(hass, SERVICE_TOPIC, TVN)
    shown = entity.media_image_hash

    # The same bytes in a new object, as a broker delivers them: equal, not identical.
    replay = bytes(bytearray(SCREEN))
    assert replay == SCREEN and replay is not SCREEN
    before = config_entry.runtime_data.updates["screen"]
    async_fire_mqtt_message(hass, SCREEN_TOPIC, replay)
    await hass.async_block_till_done(wait_background_tasks=True)

    # It did arrive, and it changed nothing.
    assert config_entry.runtime_data.updates["screen"] == before + 1
    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    assert entity.media_image_hash == shown

    await _publish(hass, SCREEN_TOPIC, NEW_SCREEN)

    assert await entity.async_get_media_image() == (NEW_SCREEN, "image/jpeg")


async def test_a_channel_seen_before_costs_nothing_and_no_bouquet_is_ever_listed(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A channel change costs one question and one picture, once per channel.

    And never `getservices`, which makes the receiver look for a picon for every
    service of a bouquet: the player asks about the one service that is on.
    """
    _serve(aioclient_mock)
    await _setup(hass, config_entry)

    for service in (TVN, SERVICE, POLSAT, TVN, SERVICE, POLSAT):
        await _publish(hass, SERVICE_TOPIC, service)

    assert _asked(aioclient_mock) == [
        "/api/getpicon",
        LINK_TWO,
        "/api/getpicon",
        LINK_ONE,
        "/api/getpicon",
        LINK_OTHER,
    ]


async def test_the_player_uses_a_bouquets_answer_that_is_already_held(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """After the media browser listed a bouquet, its channels need no question at all."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _asked(aioclient_mock) == ["/api/getservices"]

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert _asked(aioclient_mock) == ["/api/getservices", LINK_TWO]
    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")


# ------------------------------------------------------------------ the picon setting --


async def test_the_picon_setting_shows_the_picon_whatever_the_screenshot_does(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """From the first payload, across a channel change, and past a new screenshot."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    await hass.async_block_till_done(wait_background_tasks=True)
    entity = _player(hass)

    assert await entity.async_get_media_image() == (PNG_ONE, "image/png")
    assert _asked(aioclient_mock) == ["/api/getpicon", LINK_ONE]
    first = entity.media_image_hash

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    second = entity.media_image_hash
    assert second != first

    await _publish(hass, SCREEN_TOPIC, NEW_SCREEN)

    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    assert entity.media_image_hash == second


@pytest.mark.parametrize("mode", [PLAYER_PICTURE_SCREENSHOT, PLAYER_PICTURE_PICON])
@pytest.mark.parametrize(
    "answer",
    [{"result": False}, {"result": True, "link": "/images/default_picon.png"}],
)
async def test_a_channel_without_a_picon_shows_the_screenshot(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    mode: str,
    answer: dict[str, Any],
) -> None:
    """"No" is an answer: nothing else is tried, it is kept, and it is not a failure."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETPICON, json=answer)
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry, mode)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    assert set(_asked(aioclient_mock)) == {"/api/getpicon"}
    # And it is remembered: the next payload does not ask again.
    asked = aioclient_mock.call_count
    await _publish(hass, POWER_TOPIC, "on")
    assert aioclient_mock.call_count == asked
    assert config_entry.runtime_data.picons.async_status()["status"] == picture.STATUS_OK


async def test_a_reference_without_its_last_colon_is_asked_about_with_it(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """OpenWebif cuts at the last colon; without one it would cut a field off."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, {**POLSAT, "sref": SREF_OTHER.rstrip(":")})

    assert aioclient_mock.mock_calls[0][1].query == {"sRef": SREF_OTHER, "json": "true"}
    assert await _player(hass).async_get_media_image() == (JPEG_OTHER, "image/jpeg")


async def test_an_openwebif_without_getpicon_is_asked_for_the_old_name_only(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The last resort is the name built from the service reference.

    Once that name has found a picture, the question OpenWebif does not answer is not
    asked again for every channel, and a channel without a file is simply without a
    picon - not a web interface to stay away from.
    """
    _serve(aioclient_mock, picons=None, files={LEGACY_TWO: PNG_TWO, LEGACY_OTHER: JPEG_OTHER})
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert _asked(aioclient_mock) == ["/api/getpicon", LEGACY_TWO]
    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")
    assert resolver.async_status()["status"] == picture.STATUS_OK

    await _publish(hass, SERVICE_TOPIC, POLSAT)
    await _publish(hass, SERVICE_TOPIC, SERVICE)

    assert _asked(aioclient_mock)[2:] == [LEGACY_OTHER, LINK_ONE]
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    assert resolver.async_status()["status"] == picture.STATUS_OK
    assert resolver.async_status()["services_without_picon"] == 1


def _new_channel(number: int) -> dict[str, Any]:
    """Return a service payload for a channel nothing has asked about before."""
    return {**POLSAT, "sref": f"1:0:19:{0x5000 + number:X}:640:13E:820000:0:0:0:"}


def _failing_for(
    aioclient_mock: AiohttpClientMocker, *failing: str, files: dict[str, bytes]
) -> None:
    """Serve the example OpenWebif, with `getpicon` answering 500 for some services."""

    async def _getpicon(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        sref = url.query.get("sRef", "")
        if sref in failing or "*" in failing:
            return AiohttpClientMockResponse(method, url, status=500, text="Internal Server Error")
        return AiohttpClientMockResponse(method, url, json=_getpicon_answer(PICONS.get(sref)))

    aioclient_mock.clear_requests()
    aioclient_mock.get(GETPICON, side_effect=_getpicon)
    for link, content in files.items():
        aioclient_mock.get(f"{BASE}{link}", content=content)
    aioclient_mock.get(EVERYTHING, status=404)


async def test_one_question_that_fails_does_not_switch_every_picon_off(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An OpenWebif that has answered and then fails for one service is still OpenWebif.

    That service has no picon for five minutes; the next channel is asked about as if
    nothing had happened, and nothing is backed off.
    """
    assert picture.MISS_TTL == 5 * 60
    _failing_for(aioclient_mock, SREF_OTHER, files=FILES)
    await _setup(hass, config_entry)
    entity = _player(hass)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)

    await _publish(hass, SERVICE_TOPIC, TVN)
    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")

    await _publish(hass, SERVICE_TOPIC, POLSAT)
    assert _asked(aioclient_mock)[2:] == ["/api/getpicon", LEGACY_OTHER]
    assert await entity.async_get_media_image() == (SCREEN, "image/jpeg")
    assert resolver.async_status()["status"] == picture.STATUS_OK
    assert resolver.async_status()["retry_in_seconds"] is None

    await _publish(hass, SERVICE_TOPIC, SERVICE)
    assert await entity.async_get_media_image() == (PNG_ONE, "image/png")
    assert resolver.async_status()["status"] == picture.STATUS_OK

    # The service that failed is left alone for five minutes, and then asked about again.
    asked = aioclient_mock.call_count
    now[0] += picture.MISS_TTL - 1
    await _publish(hass, SERVICE_TOPIC, POLSAT)
    assert aioclient_mock.call_count == asked
    await _publish(hass, SERVICE_TOPIC, SERVICE)
    now[0] += 2
    await _publish(hass, SERVICE_TOPIC, POLSAT)
    assert aioclient_mock.call_count == asked + 2
    assert resolver.async_status()["status"] == picture.STATUS_OK


async def test_a_picon_under_the_old_name_does_not_retire_a_getpicon_that_works(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """One failed question answered by the old name is not an OpenWebif without getpicon."""
    _failing_for(aioclient_mock, SREF_OTHER, files={**FILES, LEGACY_OTHER: JPEG_OTHER})
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)
    await _publish(hass, SERVICE_TOPIC, POLSAT)
    assert await _player(hass).async_get_media_image() == (JPEG_OTHER, "image/jpeg")
    await _publish(hass, SERVICE_TOPIC, SERVICE)

    assert _asked(aioclient_mock)[2:] == [
        "/api/getpicon",
        LEGACY_OTHER,
        "/api/getpicon",
        LINK_ONE,
    ]
    assert await _player(hass).async_get_media_image() == (PNG_ONE, "image/png")


async def test_three_questions_in_a_row_that_fail_are_a_web_interface_to_leave_alone(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Once is a service; three times running is the web interface, answered or not."""
    assert picture.MAX_MISSES == 3
    _failing_for(aioclient_mock, files=FILES)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    _clock(resolver)
    await _publish(hass, SERVICE_TOPIC, TVN)
    _failing_for(aioclient_mock, "*", files={})

    await _publish(hass, SERVICE_TOPIC, _new_channel(1))
    await _publish(hass, SERVICE_TOPIC, _new_channel(2))
    assert resolver.async_status()["status"] == picture.STATUS_OK
    assert aioclient_mock.call_count == 4

    await _publish(hass, SERVICE_TOPIC, _new_channel(3))
    assert aioclient_mock.call_count == 6
    assert resolver.async_status()["status"] == "unexpected"
    assert resolver.async_status()["retry_in_seconds"] == 900
    await _publish(hass, SERVICE_TOPIC, _new_channel(4))
    assert aioclient_mock.call_count == 6

    # An answer in between starts the count again.
    _failing_for(aioclient_mock, files=FILES)
    resolver._clock = lambda: 10_000.0
    await _publish(hass, SERVICE_TOPIC, SERVICE)
    assert resolver.async_status()["status"] == picture.STATUS_OK
    _failing_for(aioclient_mock, "*", files={})
    await _publish(hass, SERVICE_TOPIC, _new_channel(5))
    await _publish(hass, SERVICE_TOPIC, _new_channel(6))
    assert resolver.async_status()["status"] == picture.STATUS_OK


async def test_one_service_that_keeps_failing_is_counted_once(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The same channel asked about every five minutes is one channel, not a dead web interface."""
    _failing_for(aioclient_mock, SREF_OTHER, files=FILES)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)
    await _publish(hass, SERVICE_TOPIC, TVN)

    for _ in range(picture.MAX_MISSES + 2):
        asked = aioclient_mock.call_count
        await _publish(hass, SERVICE_TOPIC, POLSAT)
        assert aioclient_mock.call_count == asked + 2
        assert resolver.async_status()["status"] == picture.STATUS_OK
        # Back to a channel whose picon is held, which asks nothing and answers nothing.
        await _publish(hass, SERVICE_TOPIC, TVN)
        now[0] += picture.MISS_TTL + 1


async def test_the_back_off_starts_the_count_of_failures_again(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """After the back-off one failure is one service again, not the third in a row."""
    _failing_for(aioclient_mock, files=FILES)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)
    await _publish(hass, SERVICE_TOPIC, TVN)
    _failing_for(aioclient_mock, "*", files={})
    for number in range(1, picture.MAX_MISSES + 1):
        await _publish(hass, SERVICE_TOPIC, _new_channel(number))
    assert resolver.async_status()["retry_in_seconds"] == 900

    now[0] += picture.BACKOFF["unexpected"] + 1
    asked = aioclient_mock.call_count
    await _publish(hass, SERVICE_TOPIC, _new_channel(11))
    await _publish(hass, SERVICE_TOPIC, _new_channel(12))
    assert aioclient_mock.call_count == asked + 4


def test_a_typed_name_the_address_parser_refuses_is_no_host() -> None:
    """A label that claims to be punycode and is not cannot be asked, and raises nothing."""
    assert box_module._typed_host_url("xn--a.lan") is None
    assert box_module._typed_host_url("xn--bcher-kva.example") == "http://xn--bcher-kva.example/"


@pytest.mark.parametrize(
    ("typed", "usable"),
    [
        # 253 characters is the longest a name can be, and 63 the longest a label.
        (".".join(["a" * 63] * 3 + ["a" * 61]), True),
        (".".join(["a" * 63] * 3 + ["a" * 62]), False),
        ("a" * 63 + ".lan", True),
        ("a" * 64 + ".lan", False),
        ("box.l-n", True),
        ("box-.lan", False),
        ("box.lan-", False),
    ],
    ids=[
        "253 characters",
        "254 characters",
        "a label of 63",
        "a label of 64",
        "a hyphen inside a label",
        "a label that ends in a hyphen",
        "a last label that ends in a hyphen",
    ],
)
def test_a_typed_name_is_held_to_the_bounds_of_a_host_name(typed: str, usable: bool) -> None:
    """The whole name, each label, and the hyphen a label cannot end with."""
    assert box_module._typed_host_url(typed) == (f"http://{typed}/" if usable else None)


async def test_an_address_that_went_away_during_a_lookup_is_not_an_answer(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """No address is nothing to remember: the service is asked about when there is one."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    box = config_entry.runtime_data
    resolver = box.picons
    told: list[None] = []
    resolver.async_add_listener(lambda: told.append(None))

    # The lookup is queued while there is an address, and runs when there is none.
    assert resolver.async_picon(SREF_TWO) is PENDING
    box.state.info["ip"] = "localhost"
    box.state.announcement["ip"] = "localhost"
    resolver._may_ask = lambda: True
    await hass.async_block_till_done(wait_background_tasks=True)

    assert told
    assert SREF_TWO not in resolver._pictures
    assert aioclient_mock.call_count == 0

    del resolver._may_ask
    box.state.info["ip"] = IP
    await _publish(hass, SERVICE_TOPIC, TVN)
    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")


# ----------------------------------------------------------------------------- standby --


@pytest.mark.parametrize("mode", [PLAYER_PICTURE_SCREENSHOT, PLAYER_PICTURE_PICON])
async def test_standby_keeps_the_last_picture(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    mode: str,
) -> None:
    """Nothing is swapped because the receiver went dark or its service payload emptied."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry, mode)
    entity = _player(hass)
    await _publish(hass, SERVICE_TOPIC, TVN)
    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    shown = entity.media_image_hash
    asked = aioclient_mock.call_count

    await _publish(hass, POWER_TOPIC, "standby")
    await _publish(hass, SERVICE_TOPIC, {})
    await _publish(hass, SCREEN_TOPIC, NEW_SCREEN)

    assert hass.states.get(PLAYER).state == MediaPlayerState.OFF
    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    assert entity.media_image_hash == shown
    assert aioclient_mock.call_count == asked


async def test_waking_does_not_flash_the_old_screenshot(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`power` arrives before `service`: the held picture stays until the channel is known."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    entity = _player(hass)
    await _publish(hass, SERVICE_TOPIC, TVN)
    await _publish(hass, POWER_TOPIC, "standby")
    await _publish(hass, SERVICE_TOPIC, {})
    shown = entity.media_image_hash

    await _publish(hass, POWER_TOPIC, "on")

    assert hass.states.get(PLAYER).state == MediaPlayerState.PLAYING
    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    assert entity.media_image_hash == shown

    await _publish(hass, SERVICE_TOPIC, SERVICE)

    assert await entity.async_get_media_image() == (PNG_ONE, "image/png")


# ---------------------------------------------------------------------------- failures --


@pytest.mark.parametrize(
    ("answer", "problem", "minutes"),
    [
        ({"exc": aiohttp.ClientConnectionError()}, "unreachable", 5),
        ({"exc": TimeoutError()}, "unreachable", 5),
        ({"status": 401}, "refused", 15),
        ({"status": 403}, "refused", 15),
        ({"status": 404}, "unexpected", 15),
        ({"status": 302, "headers": {"Location": f"{BASE}/login"}}, "unexpected", 15),
        ({"status": 500, "content": PNG_TWO}, "unexpected", 15),
        ({"text": "<html><body>It works!</body></html>"}, "unexpected", 15),
    ],
)
async def test_a_receiver_that_cannot_be_asked_is_left_alone_for_minutes(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any],
    problem: str,
    minutes: int,
) -> None:
    """One attempt, one line in the log, the screenshot - and then quiet until it is due.

    Unreachable, behind a login, or answering every question with something that is not
    OpenWebif's answer: a status that is not 200, or a page.
    """
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, **answer)
    await _setup(hass, config_entry)
    entity = _player(hass)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)
    # One request where the first already says it all; two where the web interface
    # answered, and was given the old name to prove itself with.
    attempt = 2 if problem == "unexpected" else 1

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await entity.async_get_media_image() == (SCREEN, "image/jpeg")
    assert aioclient_mock.call_count == attempt
    assert resolver.async_status()["status"] == problem
    assert resolver.async_status()["retry_in_seconds"] == minutes * 60

    # Every channel change in the meantime asks nothing.
    for service in (SERVICE, TVN, POLSAT, SERVICE):
        await _publish(hass, SERVICE_TOPIC, service)
    now[0] += minutes * 60 - 1
    await _publish(hass, SERVICE_TOPIC, TVN)
    assert aioclient_mock.call_count == attempt

    # When it is due it is asked once more, fails the same way, and says nothing new.
    now[0] += 2
    await _publish(hass, SERVICE_TOPIC, SERVICE)
    assert aioclient_mock.call_count == 2 * attempt
    assert len(_warnings(caplog)) == 1
    assert IP not in "".join(_warnings(caplog))

    # A receiver that answers again is used again, and a later failure is news again.
    _serve(aioclient_mock)
    now[0] += minutes * 60 + 1
    await _publish(hass, SERVICE_TOPIC, TVN)
    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    assert resolver.async_status()["status"] == picture.STATUS_OK
    assert resolver.async_status()["retry_in_seconds"] is None

    # For a receiver that cannot be reached or wants a login that is the next lookup.
    # One that has just answered as OpenWebif does gets the benefit of the doubt for a
    # service or two before it is something else again.
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, **answer)
    for number in range(picture.MAX_MISSES if problem == "unexpected" else 1):
        assert len(_warnings(caplog)) == 1
        await _publish(hass, SERVICE_TOPIC, _new_channel(number))
    assert len(_warnings(caplog)) == 2
    assert resolver.async_status()["status"] == problem


async def test_a_status_that_is_not_200_is_never_a_picture(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An error page can carry anything, a PNG included; it is not what was asked for."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETPICON, json=_getpicon_answer(LINK_TWO))
    aioclient_mock.get(f"{BASE}{LINK_TWO}", status=404, content=PNG_TWO)
    aioclient_mock.get(EVERYTHING, status=500, content=PNG_ONE)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    assert not config_entry.runtime_data.picons.async_status()["picons_cached"]


async def test_a_redirect_is_not_followed(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every request says so itself, and a 302 is an answer that cannot be used."""

    async def _redirecting(url: Any) -> _FakeResponse:
        if url.path == LINK_TWO:
            return _FakeResponse(302, PNG_TWO)
        return await _openwebif(url)

    session = _FakeSession(_redirecting)
    monkeypatch.setattr(picture, "async_get_clientsession", lambda _hass: session)
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [url.path for url, _ in session.calls] == [
        "/api/getpicon",
        LINK_TWO,
        "/api/getservices",
    ]
    assert all(kwargs == {"allow_redirects": False} for _, kwargs in session.calls)
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")


async def test_a_request_that_never_ends_is_given_up_after_the_timeout(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five seconds, and here a twentieth of one: the lookup does not hang with it."""

    async def _never(url: Any) -> _FakeResponse:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    assert picture.REQUEST_TIMEOUT == 5.0
    monkeypatch.setattr(picture, "REQUEST_TIMEOUT", 0.05)
    session = _FakeSession(_never)
    monkeypatch.setattr(picture, "async_get_clientsession", lambda _hass: session)
    await _setup(hass, config_entry)

    async with asyncio.timeout(5):
        await _publish(hass, SERVICE_TOPIC, TVN)

    assert config_entry.runtime_data.picons.async_status()["status"] == "unreachable"
    assert session.active == 0


async def test_a_receiver_that_is_not_available_is_not_asked(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Offline on the broker is off the network, as far as anybody here knows."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    box = config_entry.runtime_data
    box.available = False

    assert box.picons.async_picon(SREF_TWO) is None
    assert box.picons.async_thumbnails(FAVOURITES) == {}
    await hass.async_block_till_done(wait_background_tasks=True)

    assert aioclient_mock.call_count == 0


HOSTILE_LINKS = [
    "http://203.0.113.7/picon/x.png",
    "//203.0.113.7/picon/x.png",
    "/picon/../etc/passwd",
    "/picon/%2e%2e/etc/passwd",
    "/picon/./x.png",
    "/picon//x.png",
    "/picon/x.png?redirect=http://203.0.113.7/",
    "/picon/x.png#fragment",
    "/picon/a\\..\\b.png",
    "/picon/a\nb.png",
    "/picon/a\x00b.png",
    "/picon/a\x1fb.png",
    "/picon/a\x7fb.png",
    "/picon/a%7fb.png",
    "/picon/a%00b.png",
    "/picon/a%0d%0ab.png",
    "/picon/",
    "/usr/share/enigma2/picon/x.png",
    "/images/default_picon.png",
    "picon/x.png",
    "/picon/" + "a" * 600 + ".png",
    None,
    42,
]


@pytest.mark.parametrize("link", HOSTILE_LINKS)
def test_a_link_that_is_not_a_plain_picon_path_is_refused(link: Any) -> None:
    """Only a path under /picon/ with nothing clever in it is ever requested."""
    assert valid_picon_link(link) is None


@pytest.mark.parametrize(
    "link", ["/picon/tvnhd.png", "/picon/1_0_19_283D_3FB_1_C00000_0_0_0.png", "/picon/TVP 1 HD.png"]
)
def test_a_plain_picon_path_is_accepted(link: str) -> None:
    """Named after the reference or after the channel, spaces and all."""
    assert valid_picon_link(link) == link


@pytest.mark.parametrize("link", [link for link in HOSTILE_LINKS if isinstance(link, str)])
async def test_a_hostile_link_never_sends_a_request_anywhere_else(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    link: str,
) -> None:
    """Whatever both answers name, only the receiver is asked, and only for a picon."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSERVICES, text=_services((SREF_TWO, "TVN HD", link)))
    aioclient_mock.get(GETPICON, text=json.dumps({"result": True, "path": link, "link": link}))
    aioclient_mock.get(ANYWHERE, status=404)
    await _setup(hass, config_entry)

    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _publish(hass, SERVICE_TOPIC, TVN)
    bouquet = await _browse(hass)

    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    assert aioclient_mock.call_count >= 1
    for _method, url, _data, _headers in aioclient_mock.mock_calls:
        assert (url.scheme, url.host) == ("http", IP)
        assert url.path in ("/api/getservices", "/api/getpicon", LEGACY_TWO)
    for child in bouquet.children:
        assert child.thumbnail is None or child.thumbnail.startswith(f"{BASE}/picon/")
    # An answer naming a link that is not used is still OpenWebif answering.
    assert config_entry.runtime_data.picons.async_status()["status"] == picture.STATUS_OK


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>Login</body></html>",
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"GIF89a--not-a-kind-we-take--",
        b"",
    ],
)
async def test_a_file_that_is_not_a_png_or_a_jpeg_is_not_shown(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    body: bytes,
) -> None:
    """The first bytes decide; the content type the receiver claims is not asked."""
    _serve(aioclient_mock, files={LINK_ONE: PNG_ONE})
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETPICON, json=_getpicon_answer(LINK_TWO))
    aioclient_mock.get(f"{BASE}{LINK_TWO}", content=body, headers={"Content-Type": "image/png"})
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert _asked(aioclient_mock) == ["/api/getpicon", LINK_TWO]
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")


async def test_a_picture_over_the_size_limit_is_not_shown(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One byte past the cap and the body is dropped, PNG or not."""
    assert picture.MAX_IMAGE_BYTES == 1024 * 1024
    monkeypatch.setattr(picture, "MAX_IMAGE_BYTES", len(PNG_TWO) - 1)
    _serve(aioclient_mock)
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")

    monkeypatch.setattr(picture, "MAX_IMAGE_BYTES", len(JPEG_OTHER))
    await _publish(hass, SERVICE_TOPIC, POLSAT)

    assert await _player(hass).async_get_media_image() == (JPEG_OTHER, "image/jpeg")


async def test_an_answer_over_the_size_limit_is_not_read(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer too big to be one is no answer: for one service, and for a bouquet."""
    assert picture.MAX_JSON_BYTES == 2 * 1024 * 1024
    monkeypatch.setattr(picture, "MAX_JSON_BYTES", 16)
    _serve(aioclient_mock, files={LEGACY_TWO: PNG_TWO})
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    # `{"result": true, ...}` did not fit, so the old name was the only thing left.
    assert _asked(aioclient_mock) == ["/api/getpicon", LEGACY_TWO]

    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _asked(aioclient_mock)[2:] == ["/api/getservices"]
    assert config_entry.runtime_data.picons.async_status()["status"] == "unexpected"
    assert config_entry.runtime_data.picons.async_status()["bouquets_cached"] == 0


# ----------------------------------------------------------- which address is asked --

REFUSED_ADDRESSES = [
    "127.0.0.1",
    "127.8.8.8",
    "169.254.169.254",
    "0.0.0.0",
    "224.0.0.251",
    "240.0.0.1",
    "255.255.255.255",
    "::1",
    "::",
    "fe80::1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "localhost",
    "receiver.example",
    "metadata.google.internal",
    "192.0.2.12.example",
    "",
    None,
    7,
]


def _entry_with_host(host: str) -> MockConfigEntry:
    """Return the example box's entry with a receiver host typed when it was added."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=BOX_NAME,
        unique_id=NODE_ID,
        data={
            CONF_NODE_ID: NODE_ID,
            CONF_BASE_TOPIC: BASE_TOPIC,
            CONF_NAME: BOX_NAME,
            CONF_RECEIVER_HOST: host,
        },
    )


@pytest.mark.parametrize("reported", REFUSED_ADDRESSES)
async def test_a_reported_address_no_receiver_can_have_is_never_asked(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """Anything on the broker can write `info`: it does not get to aim Home Assistant.

    Loopback, link-local, unspecified, multicast and reserved addresses, and every name -
    a name is resolved here, and `localhost` is a name.
    """
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "ip": reported})
    box_on_the_broker[ANNOUNCEMENT_TOPIC] = json.dumps({**ANNOUNCEMENT, "ip": reported})
    aioclient_mock.clear_requests()
    aioclient_mock.get(ANYWHERE, content=PNG_TWO)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    box = config_entry.runtime_data

    await _publish(hass, SERVICE_TOPIC, TVN)
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert box.picon_lookup_url is None
    assert aioclient_mock.call_count == 0
    assert box.picons.async_status()["status"] == picture.STATUS_NO_ADDRESS
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")


@pytest.mark.parametrize(
    ("reported", "url"),
    [
        (IP, f"http://{IP}/"),
        ("10.0.0.7", "http://10.0.0.7/"),
        ("2001:db8::12", "http://[2001:db8::12]/"),
    ],
)
async def test_a_reported_address_is_asked_when_no_host_was_typed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: str,
    url: str,
) -> None:
    """An IP address written out, of a kind a receiver on a network has."""
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "ip": reported})
    await _setup(hass, config_entry)

    assert config_entry.runtime_data.picon_lookup_url == url


@pytest.mark.parametrize("reported", [IP, "127.0.0.1", "169.254.169.254", "localhost", None])
async def test_the_typed_host_is_asked_whatever_the_receiver_reports(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    reported: Any,
) -> None:
    """The administrator's word wins over the broker's, in every case."""
    typed = "198.51.100.7"
    entry = _entry_with_host(typed)
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "ip": reported})
    box_on_the_broker[ANNOUNCEMENT_TOPIC] = json.dumps({**ANNOUNCEMENT, "ip": reported})
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"http://{typed}/api/getpicon", json=_getpicon_answer(LINK_TWO))
    aioclient_mock.get(f"http://{typed}{LINK_TWO}", content=PNG_TWO)
    aioclient_mock.get(ANYWHERE, status=404)
    await _setup(hass, entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert entry.runtime_data.picon_lookup_url == f"http://{typed}/"
    assert [str(call[1].origin()) for call in aioclient_mock.mock_calls] == [f"http://{typed}"] * 2
    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")


@pytest.mark.parametrize(
    ("typed", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1/"),
        ("localhost", "http://localhost/"),
        ("receiver.example", "http://receiver.example/"),
        ("  192.0.2.40 ", "http://192.0.2.40/"),
        ("2001:db8::40", "http://[2001:db8::40]/"),
        # What the installer and SSH take and a certificate would not: still a host.
        ("vu_uno.lan", "http://vu_uno.lan/"),
        ("box.lan.", "http://box.lan./"),
        ("_dmz_.box-2.lan.", "http://_dmz_.box-2.lan./"),
        # What cannot be the host of an HTTP address at all is no address - and never
        # the reported one: once a host is typed, the broker does not choose.
        ("fe80::1%eth0", None),
        ("http://192.0.2.40/", None),
        ("192.0.2.40/../x", None),
        ("box lan", None),
        ("box.lan:8080", None),
        ("user@box.lan", None),
        ("[2001:db8::40]", None),
        ("b" + chr(0xFC) + "cher.example", None),
        ("-box.lan", None),
        ("a" * 254, None),
        # Nothing typed, or only blanks, is nothing typed: the reported address.
        ("", f"http://{IP}/"),
        ("   ", f"http://{IP}/"),
    ],
)
async def test_a_typed_host_is_asked_when_it_can_be_the_host_of_an_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    typed: str,
    url: str | None,
) -> None:
    """What the administrator typed is the administrator's choice, and is not filtered."""
    entry = _entry_with_host(typed)
    await _setup(hass, entry)

    assert entry.runtime_data.picon_lookup_url == url


@pytest.mark.parametrize(
    "reported", ["192.0.2.12", "127.0.0.1", "169.254.169.254", "localhost", None]
)
@pytest.mark.parametrize("typed", ["fe80::1%eth0", "box.lan:8080", "user@box.lan"])
async def test_an_unusable_typed_host_is_never_replaced_by_the_reported_address(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    typed: str,
    reported: Any,
) -> None:
    """A host was typed, so nothing the broker says is asked - not even a good address."""
    entry = _entry_with_host(typed)
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "ip": reported})
    box_on_the_broker[ANNOUNCEMENT_TOPIC] = json.dumps({**ANNOUNCEMENT, "ip": reported})
    aioclient_mock.clear_requests()
    aioclient_mock.get(ANYWHERE, status=404)
    await _setup(hass, entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert entry.runtime_data.picon_lookup_url is None
    assert entry.runtime_data.picons.async_status()["status"] == picture.STATUS_NO_ADDRESS
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize("mode", [PLAYER_PICTURE_SCREENSHOT, PLAYER_PICTURE_PICON])
@pytest.mark.parametrize(
    "typed",
    ["box.lan:8080", "user@box", "[2001:db8::40]", "b" + chr(0xFC) + "cher.example"],
    ids=["a port", "a user name", "brackets", "not ASCII"],
)
async def test_a_typed_host_that_is_no_host_means_no_picon_and_no_thumbnail(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    caplog: pytest.LogCaptureFixture,
    typed: str,
    mode: str,
) -> None:
    """The receiver reports a good address, and it is not asked in the typed one's place.

    The card shows the screenshot, the media browser has no thumbnails, nothing is
    requested anywhere and nothing is logged about it.
    """
    entry = _entry_with_host(typed)
    aioclient_mock.clear_requests()
    aioclient_mock.get(ANYWHERE, content=PNG_TWO)
    await _setup(hass, entry, mode)
    box = entry.runtime_data
    assert box.ip_address == IP

    await _publish(hass, SERVICE_TOPIC, TVN)
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    bouquet = await _browse(hass)

    assert box.picon_lookup_url is None
    assert aioclient_mock.call_count == 0
    assert box.picons.async_status()["status"] == picture.STATUS_NO_ADDRESS
    assert [child.thumbnail for child in bouquet.children] == [None, None]
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    assert _warnings(caplog) == []


@pytest.mark.parametrize("typed", ["vu_uno.lan", "box.lan."])
async def test_a_typed_name_with_an_underscore_or_a_final_dot_is_really_asked(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    typed: str,
) -> None:
    """Not only judged usable: the request is made, to that name."""
    entry = _entry_with_host(typed)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"http://{typed}/api/getpicon", json=_getpicon_answer(LINK_TWO))
    aioclient_mock.get(f"http://{typed}{LINK_TWO}", content=PNG_TWO)
    aioclient_mock.get(ANYWHERE, status=404)
    await _setup(hass, entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert [call[1].host for call in aioclient_mock.mock_calls] == [typed, typed]
    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")


# -------------------------------------------------- what the receiver's payloads hold --


async def test_a_bouquet_whose_reference_is_not_text_breaks_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One malformed bouquet in the channel list, and the others work as before."""
    _serve(aioclient_mock)
    broken = json.loads(json.dumps(CHANNELS))
    for sref in (["not", "text"], {"not": "text"}, 7, None, ""):
        broken["bouquets"].append(
            {"name": f"Broken {sref!r}", "sref": sref, "channels": [{"sref": SREF_OTHER}]}
        )
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(broken)
    await _setup(hass, config_entry)
    entity = _player(hass)
    resolver = config_entry.runtime_data.picons

    first = await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _publish(hass, SERVICE_TOPIC, TVN)
    second = await _browse(hass)
    for bouquet in config_entry.runtime_data.bouquets:
        resolver.async_thumbnails(bouquet)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert first.children[0].thumbnail == PICON_URL
    assert [child.thumbnail for child in second.children] == [
        f"{BASE}{LINK_ONE}",
        f"{BASE}{LINK_TWO}",
    ]
    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    assert resolver.async_status()["status"] == picture.STATUS_OK
    assert _warnings(caplog) == []


async def test_a_fault_in_the_lookup_is_a_failure_like_any_other(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    receiver_without_a_web_interface: list[str],
) -> None:
    """Whatever goes wrong inside: reported, logged once, backed off - never a loop."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    _clock(resolver)
    faults: list[str] = []
    told: list[None] = []
    resolver.async_add_listener(lambda: told.append(None))

    async def _fault(sref: str) -> None:
        faults.append(sref)
        raise RuntimeError("a bug")

    resolver._async_resolve = _fault

    for service in (TVN, SERVICE, TVN, POLSAT):
        await _publish(hass, SERVICE_TOPIC, service)

    assert faults == [SREF_TWO]
    assert len(told) == 1
    assert resolver.async_status()["status"] == "error"
    assert resolver.async_status()["retry_in_seconds"] == 900
    assert len(_warnings(caplog)) == 1
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    assert aioclient_mock.call_count == 0
    assert len(receiver_without_a_web_interface) == 1
    receiver_without_a_web_interface.clear()


async def test_a_fault_while_a_bouquet_is_opened_still_opens_it(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    receiver_without_a_web_interface: list[str],
) -> None:
    """The media browser lists the channels, with the thumbnails it always had."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons

    def _fault(bouquet: Any) -> None:
        raise RuntimeError("a bug")

    resolver._known_bouquet_links = _fault

    bouquet = await _browse(hass)
    bouquet = await _browse(hass)

    assert [child.title for child in bouquet.children] == ["TVP 1 HD", "TVN HD"]
    assert bouquet.children[0].thumbnail == PICON_URL
    assert resolver.async_status()["status"] == "error"
    assert len(_warnings(caplog)) == 1
    assert aioclient_mock.call_count == 0
    assert len(receiver_without_a_web_interface) == 1
    receiver_without_a_web_interface.clear()


# -------------------------------------------------------------------------- the task --


async def test_a_channel_change_does_not_wait_behind_the_one_before_it(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The lookup for a channel that was left is abandoned, not waited for."""
    held = asyncio.Event()
    started = asyncio.Event()

    async def _getpicon(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        sref = url.query["sRef"]
        if sref == SREF_TWO:
            started.set()
            await held.wait()
        return AiohttpClientMockResponse(method, url, json=_getpicon_answer(PICONS.get(sref)))

    aioclient_mock.clear_requests()
    aioclient_mock.get(GETPICON, side_effect=_getpicon)
    aioclient_mock.get(f"{BASE}{LINK_OTHER}", content=JPEG_OTHER)
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons

    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(TVN))
    await hass.async_block_till_done()
    await asyncio.wait_for(started.wait(), 2)
    stale = resolver._task

    # The request for TVN is still open. The next channel is looked up at once.
    async with asyncio.timeout(2):
        await _publish(hass, SERVICE_TOPIC, POLSAT)

    assert stale.cancelled()
    assert await _player(hass).async_get_media_image() == (JPEG_OTHER, "image/jpeg")
    assert _asked(aioclient_mock) == ["/api/getpicon", "/api/getpicon", LINK_OTHER]

    held.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert SREF_TWO not in resolver._pictures
    assert await _player(hass).async_get_media_image() == (JPEG_OTHER, "image/jpeg")
    assert aioclient_mock.call_count == 3


async def test_unloading_cancels_everything_and_nothing_happens_after_it(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A request held open while the entry unloads ends there, and so does what waited.

    A bouquet opened in the media browser is queued behind the player's lookup; it must
    not be asked about once the entry is gone. Nor is any answer kept, any state written,
    any listener called, or any retry left waiting.
    """
    held = asyncio.Event()
    started = asyncio.Event()

    async def _hold(method: str, url: Any, data: Any) -> Any:
        started.set()
        await held.wait()
        return AiohttpClientMockResponse(method, url, text=FAVOURITES_ANSWER)

    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, side_effect=_hold)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    resolver = config_entry.runtime_data.picons
    told: list[None] = []
    resolver.async_add_listener(lambda: told.append(None))

    await asyncio.wait_for(started.wait(), 2)
    await _browse(hass)
    await _browse(hass, SPORT)
    task = resolver._task
    assert task is not None and not task.done()
    assert _asked(aioclient_mock) == ["/api/getpicon"]

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    written = hass.states.get(PLAYER)

    assert task.cancelled()
    assert not resolver._scans and resolver._wanted is None
    held.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    await asyncio.sleep(0.05)

    assert _asked(aioclient_mock) == ["/api/getpicon"]
    assert resolver.async_picon(SREF_TWO) is None
    assert resolver.async_thumbnails(FAVOURITES) == {}
    assert resolver._task is None
    assert resolver._retry_cancel is None
    assert not resolver._listeners and not told
    assert hass.states.get(PLAYER) is written

    # And what a late answer or a late failure would have done does nothing.
    resolver._remember(SREF_TWO, picon_picture(PNG_TWO))
    resolver._keep_bouquet(FAVOURITES, {}, 60)
    resolver._async_unavailable("unreachable", sref=SREF_TWO)
    resolver._tell()
    assert not resolver._pictures and not resolver._bouquets
    assert resolver.async_status()["status"] != "unreachable"
    assert resolver._retry_cancel is None
    assert aioclient_mock.call_count == 1


async def test_after_unloading_not_even_what_is_held_is_given_out(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A bouquet's links and a picture that were here are not answers any more."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    await _browse(hass)
    await _publish(hass, SERVICE_TOPIC, TVN)
    assert len(resolver.async_thumbnails(FAVOURITES)) == 2
    assert isinstance(resolver.async_picon(SREF_TWO), Picture)
    asked = aioclient_mock.call_count

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert resolver.async_thumbnails(FAVOURITES) == {}
    assert resolver.async_thumbnails(SPORT) == {}
    assert resolver.async_picon(SREF) is None
    await hass.async_block_till_done(wait_background_tasks=True)
    assert resolver._task is None or resolver._task.done()
    assert not resolver._scans
    assert aioclient_mock.call_count == asked


async def test_the_receiver_is_asked_one_thing_at_a_time(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two bouquets opened and two channel changes at once are four turns, not a burst."""
    session = _FakeSession(_openwebif)
    monkeypatch.setattr(picture, "async_get_clientsession", lambda _hass: session)
    await _setup(hass, config_entry)

    await _browse(hass)
    await _browse(hass, SPORT)
    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(TVN))
    await hass.async_block_till_done()
    await asyncio.sleep(0.005)
    await _browse(hass)
    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(POLSAT))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert session.peak == 1
    assert session.active == 0
    paths = [url.path for url, _ in session.calls]
    # The channel that is on came first, and what was waiting behind it came after.
    assert paths.index(LINK_OTHER) < len(paths) - 1
    assert paths[-1] == "/api/getservices"
    assert 1 <= paths.count("/api/getservices") <= 3
    assert await _player(hass).async_get_media_image() == (JPEG_OTHER, "image/jpeg")
    assert config_entry.runtime_data.picons.async_status()["bouquets_cached"] >= 1


async def test_a_bouquet_is_listed_once_per_time_it_is_opened_whatever_the_zapping(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list interrupted by a channel change is dropped, not started again.

    Listing a bouquet is the expensive request, and a channel change interrupts it for
    the channel that is on. If the interrupted list went back into the queue, every
    channel change while it runs would start it over: twenty of them, twenty-one lists,
    none finished. So it is asked for once per time the bouquet is opened in the media
    browser, and its thumbnails come with the next opening.
    """
    listed: list[str] = []
    finished: list[str] = []

    async def _slow_list(url: Any) -> _FakeResponse:
        if url.path == "/api/getservices":
            listed.append(url.query["sRef"])
            await asyncio.sleep(0.2)
            finished.append(url.query["sRef"])
            return _FakeResponse(200, FAVOURITES_ANSWER.encode())
        return await _openwebif(url)

    session = _FakeSession(_slow_list)
    monkeypatch.setattr(picture, "async_get_clientsession", lambda _hass: session)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons

    await _browse(hass)
    await asyncio.sleep(0.02)
    assert listed == [FAVOURITES["sref"]]
    for number in range(8):
        # Eight channels never seen before, each one a lookup of its own.
        sref = f"1:0:19:{0x4000 + number:X}:640:13E:820000:0:0:0:"
        async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps({**POLSAT, "sref": sref}))
        await hass.async_block_till_done()
        await asyncio.sleep(0.03)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert listed == [FAVOURITES["sref"]]
    assert finished == []
    assert not resolver._scans
    assert resolver.async_status()["bouquets_cached"] == 0
    assert session.peak == 1

    # Opened again, it is listed again - once - and this time nothing interrupts it.
    again = await _browse(hass)
    assert again.children[1].thumbnail == f"{BASE}{LEGACY_TWO}"
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert listed == finished + [FAVOURITES["sref"]] == [FAVOURITES["sref"]] * 2
    assert (await _browse(hass)).children[1].thumbnail == f"{BASE}{LINK_TWO}"
    assert len(listed) == 2


async def test_a_bouquet_waiting_its_turn_is_listed_once_after_the_channel(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the list in flight is dropped: one that had not started is not interrupted."""
    listed: list[str] = []

    async def _counting(url: Any) -> _FakeResponse:
        if url.path == "/api/getservices":
            listed.append(url.query["sRef"])
            await asyncio.sleep(0.1)
        return await _openwebif(url)

    session = _FakeSession(_counting)
    monkeypatch.setattr(picture, "async_get_clientsession", lambda _hass: session)
    await _setup(hass, config_entry)

    await _browse(hass)
    await _browse(hass, SPORT)
    await asyncio.sleep(0.02)
    await _publish(hass, SERVICE_TOPIC, TVN)

    # "Ulubione TV" was being listed and is dropped; "Sport" was waiting and is listed.
    assert listed == [FAVOURITES["sref"], SPORT["sref"]]
    assert config_entry.runtime_data.picons.async_status()["bouquets_cached"] == 1
    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")


async def test_a_list_that_failed_is_not_the_list_in_flight_any_more(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A channel change drops the list in flight, and a list that failed is not in flight.

    The same bouquet, opened again after its hour and waiting behind the player's
    lookup, keeps its turn through the next channel change.
    """
    held = asyncio.Event()
    started = asyncio.Event()

    async def _getpicon(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        sref = url.query["sRef"]
        if sref == SREF_TWO:
            started.set()
            await held.wait()
        return AiohttpClientMockResponse(method, url, json=_getpicon_answer(PICONS.get(sref)))

    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSERVICES, status=500)
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)

    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _asked(aioclient_mock) == ["/api/getservices"]
    assert resolver.async_status()["status"] == "unexpected"

    # An hour later the receiver answers. The bouquet is opened while TVN is looked up.
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETPICON, side_effect=_getpicon)
    aioclient_mock.get(GETSERVICES, text=FAVOURITES_ANSWER)
    aioclient_mock.get(f"{BASE}{LINK_OTHER}", content=JPEG_OTHER)
    aioclient_mock.get(EVERYTHING, status=404)
    now[0] += picture.NEGATIVE_TTL + 1
    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(TVN))
    await hass.async_block_till_done()
    await asyncio.wait_for(started.wait(), 2)
    await _browse(hass)
    await _publish(hass, SERVICE_TOPIC, POLSAT)

    assert _asked(aioclient_mock) == [
        "/api/getpicon",
        "/api/getpicon",
        LINK_OTHER,
        "/api/getservices",
    ]
    assert resolver.async_status()["bouquets_cached"] == 1
    held.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_list_that_was_cancelled_is_not_the_list_in_flight_any_more(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task cancelled from outside leaves no bouquet marked as being listed."""
    started = asyncio.Event()

    async def _never(url: Any) -> _FakeResponse:
        started.set()
        await asyncio.Event().wait()
        return _FakeResponse(404)

    session = _FakeSession(_never)
    monkeypatch.setattr(picture, "async_get_clientsession", lambda _hass: session)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons

    await _browse(hass)
    await asyncio.wait_for(started.wait(), 2)
    task = resolver._task
    assert resolver._scanning == FAVOURITES["sref"]

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert resolver._scanning is None
    assert session.active == 0


async def test_a_dropped_list_that_ends_late_does_not_unmark_the_list_after_it(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that takes a moment to close ends after the next task has begun.

    The list a channel change dropped is still closing its connection when the bouquet
    behind it is being listed. When it has closed, that second list is still the one
    in flight: the next channel change drops it, and it is not listed a second time.
    """
    listed: list[str] = []
    listing = [asyncio.Event(), asyncio.Event()]
    closing = asyncio.Event()

    async def _slow_to_close(url: Any) -> _FakeResponse:
        if url.path != "/api/getservices":
            return await _openwebif(url)
        listed.append(url.query["sRef"])
        if len(listed) > 2:
            return _FakeResponse(200, FAVOURITES_ANSWER.encode())
        listing[len(listed) - 1].set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if len(listed) == 1:
                await closing.wait()
            raise
        return _FakeResponse(404)

    session = _FakeSession(_slow_to_close)
    monkeypatch.setattr(picture, "async_get_clientsession", lambda _hass: session)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons

    await _browse(hass)
    await _browse(hass, SPORT)
    await asyncio.wait_for(listing[0].wait(), 2)
    dropped = resolver._task

    # TVN interrupts the first list, and "Sport" is listed behind it while the first
    # request is still closing.
    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(TVN))
    await asyncio.wait_for(listing[1].wait(), 2)
    assert not dropped.done()
    closing.set()
    await asyncio.gather(dropped, return_exceptions=True)

    await _publish(hass, SERVICE_TOPIC, POLSAT)

    assert listed == [FAVOURITES["sref"], SPORT["sref"]]
    assert not resolver._scans
    assert session.active == 0
    assert await _player(hass).async_get_media_image() == (JPEG_OTHER, "image/jpeg")


async def test_what_was_queued_behind_a_failure_is_not_asked(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The back-off is read again before every request, not once when it was queued."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, exc=aiohttp.ClientConnectionError())
    await _setup(hass, config_entry)

    for _ in range(2):
        await _browse(hass)
        await _browse(hass, SPORT)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _asked(aioclient_mock) == ["/api/getservices"]
    for _ in range(2):
        await _browse(hass)
        await _browse(hass, SPORT)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 1


async def test_the_picon_setting_tries_once_more_when_the_back_off_ends(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Without a payload from the receiver: the card would stay on the screenshot.

    Once. A retry that fails is not followed by another, or this would be polling.
    """
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, exc=aiohttp.ClientConnectionError())
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    await hass.async_block_till_done(wait_background_tasks=True)
    entity = _player(hass)
    resolver = config_entry.runtime_data.picons
    assert aioclient_mock.call_count == 1
    assert await entity.async_get_media_image() == (SCREEN, "image/jpeg")
    assert resolver._retry_cancel is not None
    # The test's clock takes over where the real one was when the lookup failed.
    now = _clock(resolver, resolver._retry_at - 300)

    # The retry itself fails: asked once, and then nothing waits any more.
    now[0] += 302
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=302))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 2
    assert resolver._retry_cancel is None
    now[0] += 302
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=700))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 2

    # A payload asks again, fails again, and that failure gets its one retry -
    # which finds the receiver back and puts the picon on the card.
    await _publish(hass, POWER_TOPIC, "on")
    assert aioclient_mock.call_count == 3
    assert resolver._retry_cancel is not None
    _serve(aioclient_mock)
    now[0] += 302
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1100))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert await entity.async_get_media_image() == (PNG_ONE, "image/png")
    assert resolver._retry_cancel is None


async def test_a_retry_that_was_waiting_goes_with_the_entry(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Unloading takes the timer too: nothing is asked when its time comes."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, exc=aiohttp.ClientConnectionError())
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    await hass.async_block_till_done(wait_background_tasks=True)
    resolver = config_entry.runtime_data.picons
    assert resolver._retry_cancel is not None
    told: list[None] = []
    resolver.async_add_listener(lambda: told.append(None))

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert resolver._retry_cancel is None
    resolver._clock = lambda: resolver._retry_at + 10
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=400))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not told
    assert aioclient_mock.call_count == 1


async def test_the_screenshot_setting_has_no_retry(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """There the picon is for the seconds after a channel change, long gone by then."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, exc=aiohttp.ClientConnectionError())
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert aioclient_mock.call_count == 1
    assert config_entry.runtime_data.picons._retry_cancel is None


# ----------------------------------------------------------------------- media browser --


async def test_the_media_browser_never_waits_for_the_receiver(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Opened while the receiver takes its time: listed at once, as it always was."""
    held = asyncio.Event()

    async def _hold(method: str, url: Any, data: Any) -> Any:
        await held.wait()
        return AiohttpClientMockResponse(method, url, text=FAVOURITES_ANSWER)

    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSERVICES, side_effect=_hold)
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry)

    async with asyncio.timeout(1):
        first = await _browse(hass)
        again = await _browse(hass)

    assert [child.thumbnail for child in first.children] == [PICON_URL, f"{BASE}{LEGACY_TWO}"]
    assert [child.thumbnail for child in again.children] == [PICON_URL, f"{BASE}{LEGACY_TWO}"]

    held.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    later = await _browse(hass)

    assert [child.thumbnail for child in later.children] == [
        f"{BASE}{LINK_ONE}",
        f"{BASE}{LINK_TWO}",
    ]
    assert _asked(aioclient_mock) == ["/api/getservices"]
    assert aioclient_mock.mock_calls[0][1].query == {"sRef": FAVOURITES["sref"], "picon": "1"}


async def test_the_media_browser_uses_the_links_openwebif_gives(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """One call per bouquet; a channel without a picon gets no thumbnail at all."""
    _serve(
        aioclient_mock,
        services=_services(
            (SREF, "TVP 1 HD", "/picon/tvp1hd.png"),
            (SREF_TWO, "TVN HD", "/images/default_picon.png"),
        ),
    )
    await _setup(hass, config_entry)

    for _ in range(3):
        bouquet = await _browse(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert [child.thumbnail for child in bouquet.children] == [f"{BASE}/picon/tvp1hd.png", None]
    assert _asked(aioclient_mock) == ["/api/getservices"]


@pytest.mark.parametrize(
    ("answer", "problem"),
    [
        ({"exc": TimeoutError()}, "unreachable"),
        ({"status": 404}, "unexpected"),
        ({"text": "<html>not a list</html>"}, "unexpected"),
        ({"text": '{"result": false}'}, "unexpected"),
    ],
)
async def test_a_bouquet_that_could_not_be_listed_is_not_asked_about_again(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    answer: dict[str, Any],
    problem: str,
) -> None:
    """A list that timed out once is the expensive one: remembered, for that bouquet."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSERVICES, **answer)
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)

    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _asked(aioclient_mock) == ["/api/getservices"]
    assert resolver.async_status()["status"] == problem

    # The back-off is over and the receiver is fine: that bouquet is still left alone,
    # while another one is asked about.
    _serve(aioclient_mock)
    now[0] += picture.BACKOFF[problem] + 1
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 0

    await _browse(hass, SPORT)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _asked(aioclient_mock) == ["/api/getservices"]
    assert aioclient_mock.mock_calls[0][1].query["sRef"] == SPORT["sref"]

    # After the hour it is asked about again.
    now[0] += picture.NEGATIVE_TTL
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 2
    assert (await _browse(hass)).children[1].thumbnail == f"{BASE}{LINK_TWO}"


async def test_a_bouquet_opened_during_a_back_off_is_not_asked_about(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The media browser keeps to the same back-off as the player."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, status=401)
    await _setup(hass, config_entry)
    await _publish(hass, SERVICE_TOPIC, TVN)
    assert aioclient_mock.call_count == 1
    resolver = config_entry.runtime_data.picons
    finished = resolver._task

    bouquet = await _browse(hass)

    # Not even queued: no task is started to find out that it may not ask.
    assert resolver._task is finished and finished.done()
    assert not resolver._scans
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 1
    assert bouquet.children[0].thumbnail == PICON_URL


@pytest.mark.parametrize("reported", ["evil.example/x?", "203.0.113.9", "127.0.0.1", IP])
async def test_every_thumbnail_points_at_the_address_picons_are_asked_at(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    reported: str,
) -> None:
    """The typed host, whatever `info` says - before OpenWebif answered, and after."""
    typed = "198.51.100.7"
    entry = _entry_with_host(typed)
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "ip": reported})
    box_on_the_broker[ANNOUNCEMENT_TOPIC] = json.dumps({**ANNOUNCEMENT, "ip": reported})
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"http://{typed}/api/getservices",
        text=_services((SREF, "TVP 1 HD", "/picon/tvp1hd.png")),
    )
    aioclient_mock.get(ANYWHERE, status=404)
    await _setup(hass, entry)

    first = await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    second = await _browse(hass)

    assert [child.thumbnail for child in first.children] == [
        f"http://{typed}{LINK_ONE}",
        f"http://{typed}{LEGACY_TWO}",
    ]
    assert [child.thumbnail for child in second.children] == [
        f"http://{typed}/picon/tvp1hd.png",
        f"http://{typed}{LEGACY_TWO}",
    ]
    assert {call[1].host for call in aioclient_mock.mock_calls} == {typed}


@pytest.mark.parametrize(
    "reported", ["evil.example/x?", "localhost", "127.0.0.1", "169.254.169.254", None]
)
async def test_without_an_address_to_ask_there_is_no_thumbnail(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """A thumbnail is a request the browser makes: not to an address nothing here asks."""
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "ip": reported})
    box_on_the_broker[ANNOUNCEMENT_TOPIC] = json.dumps({**ANNOUNCEMENT, "ip": reported})
    aioclient_mock.clear_requests()
    aioclient_mock.get(ANYWHERE, status=404)
    await _setup(hass, config_entry)

    bouquet = await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [child.title for child in bouquet.children] == ["TVP 1 HD", "TVN HD"]
    assert [child.thumbnail for child in bouquet.children] == [None, None]
    assert aioclient_mock.call_count == 0


async def test_a_reference_that_makes_no_file_name_makes_no_thumbnail(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An IPTV reference carries a stream address; that is not put into a thumbnail."""
    _serve(aioclient_mock)
    iptv = "4097:0:1:0:0:0:0:0:0:0:http%3a//example.invalid/stream?token=secret:Kanal"
    changed = json.loads(json.dumps(CHANNELS))
    changed["bouquets"][0]["channels"].append({"sref": iptv, "name": "Kanal"})
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(changed)
    await _setup(hass, config_entry)

    bouquet = await _browse(hass)

    assert [child.thumbnail for child in bouquet.children] == [
        PICON_URL,
        f"{BASE}{LEGACY_TWO}",
        None,
    ]


# ----------------------------------------------------------------- what is kept, and how long --


async def test_answers_are_believed_for_a_day_and_no_picon_for_an_hour(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A picture, a missing picture and a bouquet's links each expire by themselves."""
    assert (picture.ANSWER_TTL, picture.NEGATIVE_TTL) == (24 * 3600, 3600)
    _serve(aioclient_mock, picons={SREF_TWO: LINK_TWO})
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    await hass.async_block_till_done(wait_background_tasks=True)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)

    async def _zap_through() -> list[str]:
        aioclient_mock.mock_calls.clear()
        for service in (TVN, POLSAT, TVN, POLSAT):
            await _publish(hass, SERVICE_TOPIC, service)
        await _browse(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        return _asked(aioclient_mock)

    # TVN has a picon, Polsat has none, and the bouquet is listed.
    assert await _zap_through() == ["/api/getpicon", LINK_TWO, "/api/getpicon", "/api/getservices"]
    now[0] += picture.NEGATIVE_TTL - 1
    assert await _zap_through() == []
    now[0] += 2
    assert await _zap_through() == ["/api/getpicon"]
    now[0] += picture.ANSWER_TTL - picture.NEGATIVE_TTL - 2
    assert "/api/getservices" not in await _zap_through()
    assert LINK_TWO not in _asked(aioclient_mock)
    # A day after the first answers: the picture and the bouquet's links are asked for
    # again. The bouquet's answer is no longer used for the player either.
    now[0] += 2
    assert await _zap_through() == ["/api/getpicon", LINK_TWO, "/api/getservices"]


@pytest.mark.parametrize(
    "behind_the_link",
    [{"response": b"<html><body>Login</body></html>"}, {"status": 404}],
    ids=["a file that is no picture", "no file"],
)
async def test_a_link_without_a_usable_picture_is_not_asked_about_again_for_an_hour(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    behind_the_link: dict[str, Any],
) -> None:
    """OpenWebif named a picon and there is no picture behind the link: no picon, for an hour."""
    assert picture.NEGATIVE_TTL == 3600

    async def _getpicon(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(
            method, url, json=_getpicon_answer(PICONS.get(url.query.get("sRef", "")))
        )

    async def _no_picture(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        # A request to a receiver gives the loop a turn, and the mock by itself does not.
        # Without one, a lookup that asks again without end would starve the bound below.
        await asyncio.sleep(0)
        return AiohttpClientMockResponse(method, url, **behind_the_link)

    aioclient_mock.clear_requests()
    aioclient_mock.get(GETPICON, side_effect=_getpicon)
    aioclient_mock.get(f"{BASE}{LINK_TWO}", side_effect=_no_picture)
    aioclient_mock.get(f"{BASE}{LINK_ONE}", content=PNG_ONE)
    aioclient_mock.get(f"{BASE}{LINK_OTHER}", content=JPEG_OTHER)
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    await hass.async_block_till_done(wait_background_tasks=True)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)

    async def _zap_through() -> list[str]:
        aioclient_mock.mock_calls.clear()
        # An answer that is not kept is asked for again without end, and this bound is
        # then what fails the test.
        async with asyncio.timeout(5):
            for service in (TVN, POLSAT, TVN, POLSAT):
                await _publish(hass, SERVICE_TOPIC, service)
        return _asked(aioclient_mock)

    # TVN has a link and no picture behind it; Polsat has a picon, kept for a day.
    assert await _zap_through() == ["/api/getpicon", LINK_TWO, "/api/getpicon", LINK_OTHER]
    assert resolver.async_status()["services_without_picon"] == 1
    now[0] += picture.NEGATIVE_TTL - 1
    assert await _zap_through() == []
    now[0] += 2
    assert await _zap_through() == ["/api/getpicon", LINK_TWO]


async def test_a_bouquets_links_are_asked_for_again_when_its_channel_list_changes(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The same bouquet with another channel in it is another list to ask about."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    changed = json.loads(json.dumps(CHANNELS))
    changed["bouquets"][0]["channels"].append({"sref": SREF_OTHER, "name": "Polsat HD"})
    await _publish(hass, CHANNELS_TOPIC, changed)
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _asked(aioclient_mock) == ["/api/getservices", "/api/getservices"]


async def test_the_pictures_kept_are_bounded_by_count_and_by_bytes(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The oldest answer goes first, and the newest is always kept."""
    assert (picture.MAX_PICTURES, picture.MAX_PICTURE_BYTES) == (64, 8 * 1024 * 1024)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    one, two = picon_picture(PNG_ONE), picon_picture(PNG_TWO)

    monkeypatch.setattr(picture, "MAX_PICTURES", 2)
    resolver._remember("a", one)
    resolver._remember("b", None)
    assert resolver.async_picon("a") is one  # used, so "b" is now the oldest
    resolver._remember("c", two)
    assert list(resolver._pictures) == ["a", "c"]

    monkeypatch.setattr(picture, "MAX_PICTURES", 64)
    monkeypatch.setattr(picture, "MAX_PICTURE_BYTES", len(PNG_ONE) + len(PNG_TWO))
    resolver._remember("d", None)
    assert list(resolver._pictures) == ["a", "c", "d"]
    resolver._remember("e", picon_picture(JPEG_OTHER))
    assert list(resolver._pictures) == ["d", "e"]
    monkeypatch.setattr(picture, "MAX_PICTURE_BYTES", 1)
    resolver._remember("f", one)
    assert list(resolver._pictures) == ["f"]


async def test_the_bouquets_kept_are_bounded_and_follow_what_is_on_offer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """By count, by links altogether, and never a bouquet that is no longer offered."""
    assert (picture.MAX_BOUQUETS, picture.MAX_BOUQUET_LINKS) == (64, 20000)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    links = {service_identity(SREF): LINK_ONE, service_identity(SREF_TWO): LINK_TWO}

    resolver._keep_bouquet(FAVOURITES, links, 60)
    resolver._keep_bouquet(SPORT, links, 60)
    assert list(resolver._bouquets) == [FAVOURITES["sref"], SPORT["sref"]]

    monkeypatch.setattr(picture, "MAX_BOUQUETS", 1)
    resolver._keep_bouquet(FAVOURITES, links, 60)
    assert list(resolver._bouquets) == [FAVOURITES["sref"]]

    monkeypatch.setattr(picture, "MAX_BOUQUETS", 64)
    monkeypatch.setattr(picture, "MAX_BOUQUET_LINKS", 3)
    resolver._keep_bouquet(SPORT, links, 60)
    assert list(resolver._bouquets) == [SPORT["sref"]]
    resolver._keep_bouquet(FAVOURITES, None, 60)
    assert list(resolver._bouquets) == [SPORT["sref"], FAVOURITES["sref"]]

    # "Sport" leaves the channel list: its answer goes the next time anything is kept,
    # with room to spare for it.
    monkeypatch.setattr(picture, "MAX_BOUQUET_LINKS", 20000)
    only = json.loads(json.dumps(CHANNELS))
    del only["bouquets"][1]
    await _publish(hass, CHANNELS_TOPIC, only)
    resolver._keep_bouquet(FAVOURITES, links, 60)
    assert list(resolver._bouquets) == [FAVOURITES["sref"]]
    # And one that was never on offer is not kept at all without being dropped at once.
    resolver._keep_bouquet({"sref": ["not", "text"], "channels": []}, links, 60)
    assert list(resolver._bouquets) == [FAVOURITES["sref"]]


def test_a_service_list_is_read_up_to_a_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten thousand services, and here two: what follows is not read."""
    assert picture.MAX_SERVICES == 10000
    monkeypatch.setattr(picture, "MAX_SERVICES", 2)
    body = _services(
        (SREF, "TVP 1 HD", LINK_ONE),
        (SREF_TWO, "TVN HD", LINK_TWO),
        (SREF_OTHER, "Polsat HD", LINK_OTHER),
    ).encode()

    assert parse_service_picons(body) == {
        service_identity(SREF): LINK_ONE,
        service_identity(SREF_TWO): LINK_TWO,
    }


# --------------------------------------------- edges a careless change would not trip over --


async def test_the_retry_is_set_for_after_the_back_off_and_only_once(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second longer than the back-off, never shorter - and one timer, not one a failure."""
    delays: list[float] = []
    real = picture.async_call_later

    def _recording(hass_: HomeAssistant, delay: float, action: Any) -> Any:
        delays.append(delay)
        return real(hass_, delay, action)

    monkeypatch.setattr(picture, "async_call_later", _recording)
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, exc=aiohttp.ClientConnectionError())
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    await hass.async_block_till_done(wait_background_tasks=True)
    resolver = config_entry.runtime_data.picons

    assert delays == [picture.BACKOFF["unreachable"] + 1]
    waiting = resolver._retry_cancel

    # The clock runs ahead of the timer, a payload asks, and that fails too: the timer
    # that is waiting is the retry, and no second one is set beside it. The clock is
    # stopped there: one that stayed ahead of every new back-off would ask without end.
    now = resolver._retry_at + 1
    resolver._clock = lambda: now
    await _publish(hass, SERVICE_TOPIC, TVN)
    assert aioclient_mock.call_count == 2
    assert len(delays) == 1
    assert resolver._retry_cancel is waiting

    # Behind a login the wait is the longer one.
    resolver._cancel_retry()
    now = resolver._retry_at + 1
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, status=401)
    await _publish(hass, SERVICE_TOPIC, SERVICE)
    assert delays == [301, 901]


async def test_opening_a_bouquet_does_not_interrupt_the_players_lookup(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The channel that is on comes first, and a bouquet waits its turn behind it."""
    held = asyncio.Event()
    started = asyncio.Event()

    async def _getpicon(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        started.set()
        await held.wait()
        return AiohttpClientMockResponse(method, url, json=_getpicon_answer(LINK_TWO))

    _serve(aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETPICON, side_effect=_getpicon)
    aioclient_mock.get(GETSERVICES, text=FAVOURITES_ANSWER)
    aioclient_mock.get(f"{BASE}{LINK_TWO}", content=PNG_TWO)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons

    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(TVN))
    await hass.async_block_till_done()
    await asyncio.wait_for(started.wait(), 2)
    task = resolver._task

    await _browse(hass)
    await _browse(hass, SPORT)
    await asyncio.sleep(0.02)

    assert resolver._task is task and not task.done()
    assert _asked(aioclient_mock) == ["/api/getpicon"]
    held.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not task.cancelled()
    assert _asked(aioclient_mock) == [
        "/api/getpicon",
        LINK_TWO,
        "/api/getservices",
        "/api/getservices",
    ]
    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")


async def test_a_bouquet_queued_behind_a_failure_is_forgotten_not_postponed(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """After the back-off a bouquet is listed because somebody opened it, not before."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, exc=aiohttp.ClientConnectionError())
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    now = _clock(resolver)

    await _browse(hass)
    await _browse(hass, SPORT)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _asked(aioclient_mock) == ["/api/getservices"]
    assert not resolver._scans

    # The receiver is back, and the player asks: "Sport" is not listed in its wake.
    _serve(aioclient_mock)
    now[0] += 301
    await _publish(hass, SERVICE_TOPIC, TVN)

    assert _asked(aioclient_mock) == ["/api/getpicon", LINK_TWO]
    assert await _player(hass).async_get_media_image() == (PNG_TWO, "image/png")


async def test_a_listener_that_asks_for_another_picon_does_not_cancel_the_task_it_runs_in(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Told about one picon, a listener asks for the next: the same task fetches it."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons
    seen: list[Any] = []

    def _ask_for_the_next() -> None:
        seen.append((asyncio.current_task(), resolver.async_picon(SREF_OTHER)))

    resolver.async_add_listener(_ask_for_the_next)
    assert resolver.async_picon(SREF_TWO) is PENDING
    first = resolver._task
    await hass.async_block_till_done(wait_background_tasks=True)

    assert seen[0] == (first, PENDING)
    assert resolver._task is first
    assert first.done() and not first.cancelled() and first.exception() is None
    assert isinstance(resolver.async_picon(SREF_TWO), Picture)
    assert isinstance(resolver.async_picon(SREF_OTHER), Picture)
    assert _asked(aioclient_mock) == ["/api/getpicon", LINK_TWO, "/api/getpicon", LINK_OTHER]


@pytest.mark.parametrize("mode", [PLAYER_PICTURE_SCREENSHOT, PLAYER_PICTURE_PICON])
def test_holding_a_picture_ends_when_the_receiver_names_a_channel(mode: str) -> None:
    """Held across standby and the wake - and then no longer, whatever comes next."""
    one = picon_picture(PNG_ONE)
    chosen = PlayerPicture(mode)
    chosen.update(sref=SREF, screen=SCREEN, is_on=True, picon=lambda _: one)
    chosen.update(sref=None, screen=SCREEN, is_on=False, picon=_never)
    chosen.update(sref=SREF, screen=SCREEN, is_on=True, picon=lambda _: one)

    # On, and the service payload empties - a recording is played, say: the screenshot
    # follows the television again, as it did before the receiver was ever off.
    later = chosen.update(sref=None, screen=NEW_SCREEN, is_on=True, picon=_never)

    assert later is not None and later.content == NEW_SCREEN


def test_with_screenshots_switched_off_the_picon_is_shown_without_a_channel_change() -> None:
    """No screenshot will ever come, so the channel a session starts on gets its picon."""
    one = picon_picture(PNG_ONE)
    chosen = PlayerPicture(PLAYER_PICTURE_SCREENSHOT)

    shown = chosen.update(
        sref=SREF, screen=None, is_on=True, picon=lambda _: one, screenshots_off=True
    )
    assert shown is one

    # A screenshot that is only not here yet is waited for, and nothing is asked.
    waiting = PlayerPicture(PLAYER_PICTURE_SCREENSHOT)
    assert waiting.update(sref=SREF, screen=None, is_on=True, picon=_never) is None

    # And one that is held - the button was pressed - is shown, switched off or not.
    held = PlayerPicture(PLAYER_PICTURE_SCREENSHOT)
    kept = held.update(sref=SREF, screen=SCREEN, is_on=True, picon=_never, screenshots_off=True)
    assert kept is not None and kept.content == SCREEN


def _png(size: tuple[int, int], colour: tuple[int, int, int, int], logo=None) -> bytes:
    """Return a real PNG: one colour, and a block of another in the middle if asked."""
    image = Image.new("RGBA", size, colour)
    if logo is not None:
        width, height = size
        image.paste(logo, (width // 4, height // 4, 3 * width // 4, 3 * height // 4))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize(
    ("logo", "backdrop"),
    [
        ((255, 255, 255, 255), picture.CARD_BACKDROP_DARK),
        ((10, 10, 10, 255), picture.CARD_BACKDROP_LIGHT),
    ],
)
def test_a_picon_is_put_whole_on_a_square_that_its_logo_stands_out_from(
    logo: tuple[int, int, int, int], backdrop: tuple[int, int, int]
) -> None:
    """The card crops to a square and takes its colour from the picture: neither costs the logo."""
    fitted = picture.card_picon(_png((220, 132), (0, 0, 0, 0), logo))

    assert fitted is not None and fitted.content_type == "image/png"
    assert fitted.kind == picture.KIND_PICON
    image = Image.open(io.BytesIO(fitted.content))
    assert image.size == (220, 220) and image.mode == "RGB"
    # Bars above and below, the picon's own transparent edge, and the logo untouched.
    assert image.getpixel((110, 5)) == backdrop
    assert image.getpixel((2, 110)) == backdrop
    assert image.getpixel((110, 110)) == logo[:3]
    assert image.getpixel((56, 110)) == logo[:3] and image.getpixel((164, 110)) == logo[:3]
    assert fitted.digest != picon_picture(_png((220, 132), (0, 0, 0, 0), logo)).digest


def test_a_picon_with_a_background_of_its_own_is_padded_in_that_colour() -> None:
    """An opaque picon is not put on a backdrop it was not drawn for."""
    fitted = picture.card_picon(_png((220, 132), (200, 30, 30, 255), (255, 255, 255, 255)))

    image = Image.open(io.BytesIO(fitted.content))
    assert image.size == (220, 220)
    assert image.getpixel((110, 5)) == (200, 30, 30)
    assert image.getpixel((110, 110)) == (255, 255, 255)


def test_a_picon_that_needs_nothing_or_cannot_be_read_is_shown_as_it_came() -> None:
    """Square and opaque, not really an image, or far larger than a picon: left alone."""
    square = _png((100, 100), (200, 30, 30, 255))
    assert picture.card_picon(square).content == square
    assert picture.card_picon(PNG_ONE).content == PNG_ONE
    huge = _png((picture.MAX_PICON_SIDE + 1, 10), (0, 0, 0, 0))
    assert picture.card_picon(huge).content == huge
    assert picture.card_picon(b"<svg></svg>") is None


@pytest.mark.parametrize("mode", [PLAYER_PICTURE_SCREENSHOT, PLAYER_PICTURE_PICON])
def test_a_receiver_that_starts_off_with_nothing_to_show_holds_nothing(mode: str) -> None:
    """Off at start-up with no screenshot yet: there is no picture to keep through a wake."""
    chosen = PlayerPicture(mode)

    assert chosen.update(sref=None, screen=None, is_on=False, picon=_never) is None
    # The retained screenshot arrives while it is still off, and is shown.
    late = chosen.update(sref=None, screen=SCREEN, is_on=False, picon=_never)
    assert late is not None and late.content == SCREEN

    fresh = PlayerPicture(mode)
    assert fresh.update(sref=None, screen=None, is_on=False, picon=_never) is None
    # It comes on before it has named a channel: the screenshot, not the nothing held.
    woken = fresh.update(sref=None, screen=SCREEN, is_on=True, picon=_never)
    assert woken is not None and woken.content == SCREEN


@pytest.mark.parametrize("mode", [PLAYER_PICTURE_SCREENSHOT, PLAYER_PICTURE_PICON])
def test_a_receiver_that_was_never_off_is_not_held_to_an_old_picture(mode: str) -> None:
    """The wake rule is for a wake. On all along, an empty service is just that."""
    one = picon_picture(PNG_ONE)
    chosen = PlayerPicture(mode)
    first = chosen.update(sref=SREF, screen=SCREEN, is_on=True, picon=lambda _: one)
    assert first is not None

    emptied = chosen.update(sref=None, screen=NEW_SCREEN, is_on=True, picon=_never)

    assert emptied is not None and emptied.content == NEW_SCREEN


# ------------------------------------------------------------------ options, diagnostics --


async def test_the_picture_option_is_offered_and_saved(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Both pictures on the form, the screenshot the default, and the choice kept."""
    await _setup(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    selector = result["data_schema"].schema[CONF_PLAYER_PICTURE]
    assert selector.config["options"] == ["screenshot", "picon"]
    assert selector.config["translation_key"] == CONF_PLAYER_PICTURE
    assert result["data_schema"]({})[CONF_PLAYER_PICTURE] == DEFAULT_PLAYER_PICTURE
    assert DEFAULT_PLAYER_PICTURE == PLAYER_PICTURE_SCREENSHOT

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DANGEROUS_BUTTONS: False,
            CONF_WOL_MAC: "",
            CONF_BOUQUETS: [],
            CONF_PLAYER_PICTURE: PLAYER_PICTURE_PICON,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_PLAYER_PICTURE] == PLAYER_PICTURE_PICON
    # The entry reloaded, so the player now runs on the new setting, and the form opens
    # on it.
    assert _player(hass)._picture.mode == PLAYER_PICTURE_PICON
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    suggested = {
        str(key): key.description.get("suggested_value")
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested[CONF_PLAYER_PICTURE] == PLAYER_PICTURE_PICON


async def test_an_unknown_stored_setting_reads_as_the_default(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An entry written by a later release, or by hand, still gets a picture."""
    await _setup(hass, config_entry, "hologram")

    assert _player(hass)._picture.mode == PLAYER_PICTURE_SCREENSHOT


async def test_diagnostics_say_which_picture_and_how_the_lookup_is_doing(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Counts and a cause - no address, no channel, no picture."""
    _serve(aioclient_mock, picons={SREF: LINK_ONE, SREF_TWO: LINK_TWO})
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    await _publish(hass, SERVICE_TOPIC, TVN)
    await _publish(hass, SERVICE_TOPIC, POLSAT)
    await _browse(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["box"]["player_picture"] == {
        "mode": PLAYER_PICTURE_PICON,
        "picon_lookup": {
            "status": "ok",
            "retry_in_seconds": None,
            "bouquets_cached": 1,
            "picons_cached": 2,
            "services_without_picon": 1,
        },
    }
    text = json.dumps(diagnostics["box"]["player_picture"])
    assert IP not in text and "picon/" not in text


def test_the_three_languages_name_the_option_alike() -> None:
    """The card in every label, and the Polish noun in the gender Polish gives it."""
    component = Path(picture.__file__).parent
    texts = {}
    for language in ("en", "pl", "de"):
        data = json.loads((component / "translations" / f"{language}.json").read_text("utf-8"))
        settings = data["options"]["step"]["settings"]
        texts[language] = (
            settings["data"]["player_picture"],
            settings["data_description"]["player_picture"],
            *data["selector"]["player_picture"]["options"].values(),
        )

    assert texts["en"][0] == "Picture on the media player card"
    assert texts["pl"][0] == "Obraz na karcie odtwarzacza"
    assert texts["de"][0] == "Bild auf der Media-Player-Karte"
    polish = " ".join(texts["pl"]).lower()
    assert "picon" in polish
    assert not re.search(r"picon(a|\u0119|\u0105|ie)\b", polish)
    # Each help text says the screenshot setting shows the screenshot, first of all.
    assert texts["en"][1].startswith("Screenshot shows the screenshot")
    assert texts["pl"][1].startswith("Zrzut ekranu pokazuje zrzut ekranu")
    assert texts["de"][1].startswith("Bildschirmfoto zeigt das Bildschirmfoto")


# ------------------------------------------- requests nothing here is meant to make --


async def test_a_request_the_picon_lookup_does_not_make_is_noticed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The check every test ends with sees a request to another page of the receiver.

    Every test runs with the example receiver's web interface answering 404, which is
    what keeps a channel change in an unrelated test from opening a connection - and
    would as quietly swallow a request no code here is meant to make. So each test ends
    by looking at what was asked, and this is that look, shown to work.
    """
    session = async_get_clientsession(hass)
    for path in ("/api/getpicon?sRef=x&json=true", "/api/getservices?sRef=x", "/picon/x.png"):
        await session.get(f"{BASE}{path}")
    assert unexpected_receiver_requests(aioclient_mock) == []

    aioclient_mock.get("http://198.51.100.7/elsewhere", text="not the receiver")
    await session.get(f"{BASE}/web/about")
    await session.get(f"{BASE}/api/zap?sRef=x")
    await session.get("http://198.51.100.7/elsewhere")

    assert unexpected_receiver_requests(aioclient_mock) == [
        f"{BASE}/web/about",
        f"{BASE}/api/zap?sRef=x",
    ]
    aioclient_mock.mock_calls.clear()


async def test_a_request_to_a_host_no_test_serves_is_noticed(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    receiver_without_a_web_interface: list[str],
) -> None:
    """A request to an address nothing was registered for does not pass in silence.

    The fake session refuses it with an AssertionError, and the lookup treats every
    fault of its own as a failure - which would turn a test asking the wrong host into
    a test that passes on the screenshot. The check every test ends with therefore also
    fails on a fault of the lookup, unless the test says it meant one by taking the
    record away, as this one does.
    """
    entry = _entry_with_host("198.51.100.9")
    await _setup(hass, entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert entry.runtime_data.picons.async_status()["status"] == "error"
    assert len(receiver_without_a_web_interface) == 1
    assert "failed unexpectedly" in receiver_without_a_web_interface[0]
    assert unexpected_receiver_requests(aioclient_mock) == []
    receiver_without_a_web_interface.clear()


# --------------------------------------------------------- what OpenWebif answers, read --


def test_a_service_list_is_read_whichever_openwebif_wrote_it() -> None:
    """Both families use the same keys; the references differ in spelling, not in meaning."""
    e2openplugins = json.dumps(
        {
            "result": True,
            "processingtime": "0:00:00.004",
            "pos": 3,
            "services": [
                {
                    "pos": 0,
                    "picon": "/images/default_picon.png",
                    "servicename": "--- News ---",
                    "servicereference": "1:64:1:0:0:0:0:0:0:0::--- News ---",
                    "program": 0,
                },
                {
                    "pos": 1,
                    "picon": LINK_ONE,
                    "servicename": "TVP 1 HD",
                    "servicereference": SREF,
                    "program": 10301,
                },
                {
                    "pos": 2,
                    "picon": "/picon/tvnhd.png",
                    "servicename": "TVN HD",
                    # With the channel's name after the fields that identify it.
                    "servicereference": f"{SREF_TWO}:TVN HD",
                    "program": 11110,
                },
            ],
        }
    ).encode()
    oe_alliance = json.dumps(
        {
            "result": True,
            "processingtime": "0:00:00.004000",
            "pos": 2,
            "services": [
                {
                    "pos": 1,
                    "picon": LINK_ONE,
                    "servicename": "TVP 1 HD",
                    # Lower-case hexadecimal fields, and no colon at the end.
                    "servicereference": SREF.lower().rstrip(":"),
                    "program": 10301,
                    "lcn": 1,
                    "streamrelay": False,
                },
                {
                    "pos": 2,
                    "picon": "/picon/tvnhd.png",
                    "servicename": "TVN HD",
                    "servicereference": SREF_TWO,
                    "lcn": 7,
                },
            ],
        }
    ).encode()

    for body in (e2openplugins, oe_alliance):
        links = parse_service_picons(body)
        assert links is not None
        assert links[service_identity(SREF)] == LINK_ONE
        assert links[service_identity(SREF_TWO)] == "/picon/tvnhd.png"
    # The marker's default picon is an answer too: it has none.
    assert parse_service_picons(e2openplugins)[
        service_identity("1:64:1:0:0:0:0:0:0:0:")
    ] == picture.NO_PICON


def test_a_service_list_says_only_what_it_is_sure_of() -> None:
    """No link, a bad link and two different links are all "not known", not "none"."""
    body = json.dumps(
        {
            "services": [
                # An OpenWebif that ignored `picon=1`.
                {"servicereference": SREF, "servicename": "TVP 1 HD"},
                # A resolver that answered with a path on the receiver's disk.
                {"servicereference": SREF_TWO, "picon": "/media/hdd/picon/tvnhd.png"},
                # One service listed twice, with two answers.
                {"servicereference": SREF_OTHER, "picon": "/picon/a.png"},
                {"servicereference": SREF_OTHER, "picon": "/picon/b.png"},
                {"servicereference": SREF_OTHER, "picon": "/picon/a.png"},
                "not a service",
                {"servicereference": 7, "picon": "/picon/c.png"},
            ]
        }
    ).encode()

    assert parse_service_picons(body) == {}
    for not_a_list in (b"", b"<html>", b"[]", b'{"services": "many"}', b'{"result": false}'):
        assert parse_service_picons(not_a_list) is None


def test_a_single_answer_is_a_link_none_or_no_answer() -> None:
    """`getpicon?json=true`, as both families write it."""
    found = json.dumps(
        {"result": True, "path": "/usr/share/enigma2/picon/tvnhd.png", "link": "/picon/tvnhd.png"}
    ).encode()

    assert parse_getpicon(found) == "/picon/tvnhd.png"
    assert parse_getpicon(b'{"result": false}') == picture.NO_PICON
    assert (
        parse_getpicon(b'{"result": true, "link": "/images/default_picon.png"}')
        == picture.NO_PICON
    )
    for no_answer in (
        b"",
        b"<html>login</html>",
        b"[]",
        b"{}",
        b'{"result": "yes", "link": "/picon/x.png"}',
        b'{"result": true, "link": "http://203.0.113.7/picon/x.png"}',
        b'{"result": true}',
    ):
        assert parse_getpicon(no_answer) is None
    # Whether it was OpenWebif that answered is a separate question from what it said.
    assert picture.getpicon_answered(b'{"result": true, "link": "/media/hdd/picon/x.png"}')
    assert picture.getpicon_answered(b'{"result": false}')
    for not_openwebif in (b"", b"<html>", b"[]", b"{}", b'{"result": "yes"}', b'{"result": 1}'):
        assert not picture.getpicon_answered(not_openwebif)


def test_getpicon_is_given_a_reference_it_will_not_cut_a_field_from() -> None:
    """OpenWebif drops everything after the last colon, so there has to be one to drop."""
    assert getpicon_reference("1:0:19:283D:3FB:1:C00000:0:0:0") == SREF
    assert getpicon_reference(SREF) == SREF
    assert getpicon_reference(f" {SREF} ") == SREF
    iptv = "4097:0:1:0:0:0:0:0:0:0:http%3a//example.invalid/stream:Kanal"
    assert getpicon_reference(iptv) == iptv


def test_the_name_built_from_the_reference() -> None:
    """Every colon an underscore and the last one dropped - or nothing, if that is unsafe."""
    assert legacy_picon_link(SREF) == LINK_ONE
    assert legacy_picon_link("4097:0:1:0:0:0:0:0:0:0:http%3a//example.invalid/s:K") is None
    assert legacy_picon_link("") is None


def test_only_a_png_or_a_jpeg_becomes_a_picon() -> None:
    """And a picon never shares a hash with a screenshot of the same bytes."""
    png = picon_picture(PNG_ONE)
    jpeg = picon_picture(JPEG_OTHER)

    assert png is not None and (png.content_type, png.kind) == ("image/png", KIND_PICON)
    assert jpeg is not None and jpeg.content_type == "image/jpeg"
    assert picon_picture(b"<svg/>") is None
    assert jpeg.digest != screenshot_picture(JPEG_OTHER).digest


# -------------------------------------------------------------- the decision by itself --


def _never(_sref: str) -> None:
    raise AssertionError("no picon may be asked for here")


def test_the_first_channel_of_a_session_is_not_a_channel_change() -> None:
    """Retained payloads at start-up say where the receiver is, not that it moved."""
    chosen = PlayerPicture(PLAYER_PICTURE_SCREENSHOT)

    assert chosen.update(sref=None, screen=None, is_on=True, picon=_never) is None
    shown = chosen.update(sref=SREF, screen=SCREEN, is_on=True, picon=_never)

    assert shown is not None and shown.content == SCREEN
    # The same channel under another spelling is still the same channel.
    chosen.update(sref=SREF.lower().rstrip(":"), screen=SCREEN, is_on=True, picon=_never)
    # And a reference that is not text is no channel at all, in either setting.
    chosen.update(sref=7, screen=SCREEN, is_on=True, picon=_never)
    PlayerPicture(PLAYER_PICTURE_PICON).update(sref=7, screen=SCREEN, is_on=True, picon=_never)


def test_a_pending_picon_leaves_the_picture_alone_until_it_is_known() -> None:
    """No flicker to nothing between two picons, and no stale one once the answer is no."""
    picons: dict[str, Any] = {SREF: picon_picture(PNG_ONE), SREF_TWO: PENDING}
    chosen = PlayerPicture(PLAYER_PICTURE_PICON)

    first = chosen.update(sref=SREF, screen=None, is_on=True, picon=picons.get)
    assert isinstance(first, Picture) and first.content == PNG_ONE

    assert chosen.update(sref=SREF_TWO, screen=None, is_on=True, picon=picons.get) is first

    picons[SREF_TWO] = None
    assert chosen.update(sref=SREF_TWO, screen=None, is_on=True, picon=picons.get) is None


def test_a_receiver_that_is_off_is_not_asked_and_keeps_its_picture() -> None:
    """Standby holds what was shown; a receiver that starts off shows the screenshot."""
    chosen = PlayerPicture(PLAYER_PICTURE_PICON)

    off = chosen.update(sref=SREF, screen=SCREEN, is_on=False, picon=_never)
    assert off is not None and off.content == SCREEN

    assert chosen.update(sref=None, screen=NEW_SCREEN, is_on=False, picon=_never) is off

    on = chosen.update(sref=SREF, screen=NEW_SCREEN, is_on=True, picon=lambda _: None)
    assert on is not None and on.content == NEW_SCREEN


@pytest.mark.parametrize("mode", [PLAYER_PICTURE_SCREENSHOT, PLAYER_PICTURE_PICON])
def test_a_receiver_that_wakes_keeps_its_picture_until_it_names_a_channel(mode: str) -> None:
    """On, with no service yet and the old frame: nothing to show that is newer."""
    one = picon_picture(PNG_ONE)
    chosen = PlayerPicture(mode)
    chosen.update(sref=SREF, screen=SCREEN, is_on=True, picon=lambda _: one)
    held = chosen.update(sref=None, screen=SCREEN, is_on=False, picon=_never)

    for sref in (None, "", 7):
        assert chosen.update(sref=sref, screen=NEW_SCREEN, is_on=True, picon=_never) is held

    named = chosen.update(sref=SREF, screen=NEW_SCREEN, is_on=True, picon=lambda _: one)
    assert named is (one if mode == PLAYER_PICTURE_PICON else chosen._shot)
    # A receiver that was never off and names no channel shows its screenshot as before.
    fresh = PlayerPicture(mode)
    shown = fresh.update(sref=None, screen=SCREEN, is_on=True, picon=_never)
    assert shown is not None and shown.content == SCREEN
    newer = fresh.update(sref=None, screen=NEW_SCREEN, is_on=True, picon=_never)
    assert newer is not None and newer.content == NEW_SCREEN


def test_a_change_seen_during_standby_still_counts_when_the_receiver_is_back() -> None:
    """Back on another channel with the old frame: the picon, until a new frame."""
    two = picon_picture(PNG_TWO)
    chosen = PlayerPicture(PLAYER_PICTURE_SCREENSHOT)
    chosen.update(sref=SREF, screen=SCREEN, is_on=True, picon=_never)
    chosen.update(sref=None, screen=SCREEN, is_on=False, picon=_never)

    assert chosen.update(sref=SREF_TWO, screen=SCREEN, is_on=True, picon=lambda _: two) is two
    back = chosen.update(sref=SREF_TWO, screen=NEW_SCREEN, is_on=True, picon=_never)
    assert back is not None and back.content == NEW_SCREEN

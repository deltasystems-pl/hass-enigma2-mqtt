"""The picture on the media player: the screenshot, the channel's picon, and which of them.

The receiver's web interface is answered by `aioclient_mock`, so every request the
integration makes is on record - which is how these tests say not only what picture is
shown, but what the receiver was asked for it, and that nothing but the receiver was asked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp
from homeassistant.components.media_player import DATA_COMPONENT, MediaPlayerState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.enigma2_mqtt import picture
from custom_components.enigma2_mqtt.const import (
    CONF_BOUQUETS,
    CONF_DANGEROUS_BUTTONS,
    CONF_PLAYER_PICTURE,
    CONF_WOL_MAC,
    DEFAULT_PLAYER_PICTURE,
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
    CHANNELS,
    CHANNELS_TOPIC,
    IP,
    POWER_TOPIC,
    SCREEN,
    SCREEN_TOPIC,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    async_setup_box,
    async_setup_box_then_retained,
)

PLAYER = "media_player.dekoder_salon"

BASE = f"http://{IP}"
GETSERVICES = f"{BASE}/api/getservices"
GETPICON = f"{BASE}/api/getpicon"
EVERYTHING = re.compile(rf"^{re.escape(BASE)}/")

# Not real pictures: the integration reads the first bytes and nothing else.
PNG_ONE = b"\x89PNG\r\n\x1a\n--the-picon-of-tvp-1--"
PNG_TWO = b"\x89PNG\r\n\x1a\n--the-picon-of-tvn--"
JPEG_OTHER = b"\xff\xd8\xff\xe0--the-picon-of-a-channel-in-no-bouquet--"
NEW_SCREEN = b"\xff\xd8\xff\xdb--a-frame-taken-after-the-zap--\xff\xd9"

# The first link is the name built from the reference; the second is one only OpenWebif
# could have found, by the channel's name.
LINK_ONE = "/picon/1_0_19_283D_3FB_1_C00000_0_0_0.png"
LINK_TWO = "/picon/tvnhd.png"
LEGACY_TWO = "/picon/1_0_19_2B66_3F3_1_C00000_0_0_0.png"

FAVOURITES = CHANNELS["bouquets"][0]
# A channel no bouquet on offer lists: somebody typed its number on the remote.
SREF_OTHER = "1:0:19:3DCD:640:13E:820000:0:0:0:"

TVN = {**SERVICE, "sref": SREF_TWO, "name": "TVN HD"}


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


def _serve(
    aioclient_mock: AiohttpClientMocker,
    *,
    services: str | None = FAVOURITES_ANSWER,
    getpicon: str | None = None,
    files: dict[str, bytes] | None = None,
) -> None:
    """Put an OpenWebif on the example receiver's address, in place of the last one."""
    aioclient_mock.clear_requests()
    if services is not None:
        aioclient_mock.get(GETSERVICES, text=services)
    if getpicon is not None:
        aioclient_mock.get(GETPICON, text=getpicon)
    if files is None:
        files = {LINK_ONE: PNG_ONE, LINK_TWO: PNG_TWO}
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
    # One call for the bouquet's links, one for the picture - and the link is the one
    # OpenWebif named, which the name built from the reference would never have found.
    assert _asked(aioclient_mock) == ["/api/getservices", LINK_TWO]
    assert aioclient_mock.mock_calls[0][1].query == {"sRef": FAVOURITES["sref"], "picon": "1"}

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


async def test_one_bouquet_is_asked_once_until_its_channel_list_changes(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Zapping inside a bouquet costs one list and one picture per channel, once."""
    _serve(aioclient_mock)
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)
    await _publish(hass, SERVICE_TOPIC, SERVICE)
    await _publish(hass, SERVICE_TOPIC, TVN)

    assert _asked(aioclient_mock) == ["/api/getservices", LINK_TWO, LINK_ONE]

    # The same bouquet with another channel in it is another list to ask about.
    changed = json.loads(json.dumps(CHANNELS))
    changed["bouquets"][0]["channels"].append({"sref": SREF_OTHER, "name": "Polsat HD"})
    await _publish(hass, CHANNELS_TOPIC, changed)
    aioclient_mock.mock_calls.clear()
    await _publish(hass, SERVICE_TOPIC, {**SERVICE, "sref": SREF_OTHER, "name": "Polsat HD"})

    assert _asked(aioclient_mock)[0] == "/api/getservices"


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
    first = entity.media_image_hash

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    second = entity.media_image_hash
    assert second != first

    await _publish(hass, SCREEN_TOPIC, NEW_SCREEN)

    assert await entity.async_get_media_image() == (PNG_TWO, "image/png")
    assert entity.media_image_hash == second


@pytest.mark.parametrize("mode", [PLAYER_PICTURE_SCREENSHOT, PLAYER_PICTURE_PICON])
async def test_a_channel_without_a_picon_shows_the_screenshot(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    mode: str,
) -> None:
    """OpenWebif's default picon is its way of saying there is none, and is not fetched."""
    _serve(
        aioclient_mock,
        services=_services(
            (SREF, "TVP 1 HD", LINK_ONE),
            (SREF_TWO, "TVN HD", "/images/default_picon.png"),
        ),
    )
    await _setup(hass, config_entry, mode)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    assert "/images/default_picon.png" not in _asked(aioclient_mock)
    assert LEGACY_TWO not in _asked(aioclient_mock)
    # And it is remembered: the next payload does not ask again.
    asked = aioclient_mock.call_count
    await _publish(hass, POWER_TOPIC, "on")
    assert aioclient_mock.call_count == asked


async def test_a_channel_in_no_bouquet_on_offer_is_asked_for_by_itself(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`getpicon` answers for one service, and no bouquet is listed for it."""
    _serve(
        aioclient_mock,
        getpicon=json.dumps(
            {
                "result": True,
                "path": "/usr/share/enigma2/picon/polsathd.png",
                "link": "/picon/polsathd.png",
            }
        ),
        files={"/picon/polsathd.png": JPEG_OTHER},
    )
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    aioclient_mock.mock_calls.clear()

    await _publish(hass, SERVICE_TOPIC, {**SERVICE, "sref": SREF_OTHER, "name": "Polsat HD"})

    assert await _player(hass).async_get_media_image() == (JPEG_OTHER, "image/jpeg")
    assert _asked(aioclient_mock) == ["/api/getpicon", "/picon/polsathd.png"]
    assert aioclient_mock.mock_calls[0][1].query == {"sRef": SREF_OTHER, "json": "true"}


async def test_only_the_bouquets_on_offer_are_listed(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A channel of a bouquet the options exclude is asked for by itself."""
    _serve(aioclient_mock, getpicon=json.dumps({"result": False}))
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={CONF_BOUQUETS: ["Sport"], CONF_PLAYER_PICTURE: PLAYER_PICTURE_PICON},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # TVP 1 HD is in "Ulubione TV" only, which is not on offer. `{"result": false}` is
    # an answer: no picon, and the name built from the reference is not tried after it.
    assert _asked(aioclient_mock) == ["/api/getpicon"]
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")


async def test_an_openwebif_that_answers_neither_question_gets_the_old_name(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The last resort is the name built from the service reference."""
    _serve(aioclient_mock, services=None, files={LEGACY_TWO: PNG_TWO})
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert _asked(aioclient_mock) == ["/api/getservices", "/api/getpicon", LEGACY_TWO]
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


# ---------------------------------------------------------------------------- failures --


@pytest.mark.parametrize(
    ("answer", "problem", "minutes"),
    [
        ({"exc": aiohttp.ClientConnectionError()}, picture.PROBLEM_UNREACHABLE, 5),
        ({"exc": TimeoutError()}, picture.PROBLEM_UNREACHABLE, 5),
        ({"status": 401}, picture.PROBLEM_REFUSED, 15),
        ({"status": 403}, picture.PROBLEM_REFUSED, 15),
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
    """One request, one line in the log, the screenshot - and then quiet until it is due."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, **answer)
    await _setup(hass, config_entry)
    entity = _player(hass)
    resolver = config_entry.runtime_data.picons
    now = [1000.0]
    resolver._clock = lambda: now[0]

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await entity.async_get_media_image() == (SCREEN, "image/jpeg")
    assert aioclient_mock.call_count == 1
    assert resolver.async_status()["status"] == problem

    # Every channel change in the meantime asks nothing.
    await _publish(hass, SERVICE_TOPIC, SERVICE)
    await _publish(hass, SERVICE_TOPIC, TVN)
    now[0] += minutes * 60 - 1
    await _publish(hass, SERVICE_TOPIC, SERVICE)
    assert aioclient_mock.call_count == 1

    # When it is due it is asked once more, fails the same way, and says nothing new.
    now[0] += 2
    await _publish(hass, SERVICE_TOPIC, TVN)
    assert aioclient_mock.call_count == 2
    assert len(_warnings(caplog)) == 1
    assert IP not in "".join(_warnings(caplog))

    # A receiver that answers again is used again, and a later failure is news again.
    _serve(aioclient_mock)
    now[0] += minutes * 60 + 1
    await _publish(hass, SERVICE_TOPIC, SERVICE)
    assert await entity.async_get_media_image() == (PNG_ONE, "image/png")
    assert resolver.async_status()["status"] == picture.STATUS_OK


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
    aioclient_mock.get(re.compile(r"."), status=404)
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")
    for _method, url, _data, _headers in aioclient_mock.mock_calls:
        assert (url.scheme, url.host) == ("http", IP)
        assert url.path in ("/api/getservices", "/api/getpicon", LEGACY_TWO)


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
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSERVICES, text=FAVOURITES_ANSWER)
    aioclient_mock.get(f"{BASE}{LINK_TWO}", content=body, headers={"Content-Type": "image/png"})
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)

    await _publish(hass, SERVICE_TOPIC, TVN)

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
    monkeypatch.setattr(picture, "MAX_IMAGE_BYTES", len(PNG_TWO) - 1)
    _serve(aioclient_mock)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")


async def test_a_service_list_over_the_size_limit_is_not_read(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list too big to be one is no answer, so the service is asked for by itself."""
    monkeypatch.setattr(picture, "MAX_JSON_BYTES", 64)
    _serve(aioclient_mock, getpicon=json.dumps({"result": False}))
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert _asked(aioclient_mock) == ["/api/getservices", "/api/getpicon"]
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")


async def test_a_redirect_is_not_followed(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A 302 is an answer that cannot be used, wherever it points."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSERVICES, text=FAVOURITES_ANSWER)
    aioclient_mock.get(
        f"{BASE}{LINK_TWO}", status=302, headers={"Location": "http://203.0.113.7/x.png"}
    )
    aioclient_mock.get("http://203.0.113.7/x.png", content=PNG_TWO)
    aioclient_mock.get(EVERYTHING, status=404)
    await _setup(hass, config_entry)

    await _publish(hass, SERVICE_TOPIC, TVN)

    assert _asked(aioclient_mock) == ["/api/getservices", LINK_TWO]
    assert await _player(hass).async_get_media_image() == (SCREEN, "image/jpeg")


# ------------------------------------------------------------------------------ unload --


async def test_unloading_cancels_a_lookup_and_nothing_happens_after_it(
    hass: HomeAssistant,
    mqtt_mock,
    aioclient_mock: AiohttpClientMocker,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A request held open while the entry unloads ends there: no answer kept, no state."""
    held = asyncio.Event()
    started = asyncio.Event()

    async def _hold(method: str, url: Any, data: Any) -> Any:
        started.set()
        await held.wait()
        raise AssertionError("the request outlived the entry")

    aioclient_mock.clear_requests()
    aioclient_mock.get(EVERYTHING, side_effect=_hold)
    await _setup(hass, config_entry)
    resolver = config_entry.runtime_data.picons

    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(TVN))
    await hass.async_block_till_done()
    await asyncio.wait_for(started.wait(), 2)
    task = resolver._task
    assert task is not None and not task.done()

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    written = hass.states.get(PLAYER)

    assert task.cancelled()
    held.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert resolver.async_picon(SREF_TWO) is None
    assert resolver._task is None
    assert not resolver._pictures
    assert hass.states.get(PLAYER) is written


# ----------------------------------------------------------------------- media browser --


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
    entity = _player(hass)
    root = await entity.async_browse_media()

    for _ in range(2):
        bouquet = await entity.async_browse_media(
            media_content_id=root.children[0].media_content_id
        )

    assert [child.thumbnail for child in bouquet.children] == [f"{BASE}/picon/tvp1hd.png", None]
    assert _asked(aioclient_mock) == ["/api/getservices"]


# ------------------------------------------------------------------ options, diagnostics --


async def test_the_picture_option_is_offered_and_saved(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Both pictures on the form, the screenshot the default, and the choice kept."""
    await async_setup_box(hass, config_entry)

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
    _serve(aioclient_mock)
    await _setup(hass, config_entry, PLAYER_PICTURE_PICON)
    await _publish(hass, SERVICE_TOPIC, TVN)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["box"]["player_picture"] == {
        "mode": PLAYER_PICTURE_PICON,
        "picon_lookup": {
            "status": "ok",
            "retry_in_seconds": None,
            "bouquets_cached": 1,
            "picons_cached": 2,
            "services_without_picon": 0,
        },
    }
    text = json.dumps(diagnostics["box"]["player_picture"])
    assert IP not in text and "picon/" not in text


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
    from custom_components.enigma2_mqtt.box import service_identity

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
    from custom_components.enigma2_mqtt.box import service_identity

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
    assert service_identity(SREF_OTHER) not in parse_service_picons(body)
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


def test_a_change_seen_during_standby_still_counts_when_the_receiver_is_back() -> None:
    """Back on another channel with the old frame: the picon, until a new frame."""
    two = picon_picture(PNG_TWO)
    chosen = PlayerPicture(PLAYER_PICTURE_SCREENSHOT)
    chosen.update(sref=SREF, screen=SCREEN, is_on=True, picon=_never)
    chosen.update(sref=None, screen=SCREEN, is_on=False, picon=_never)

    assert chosen.update(sref=SREF_TWO, screen=SCREEN, is_on=True, picon=lambda _: two) is two
    back = chosen.update(sref=SREF_TWO, screen=NEW_SCREEN, is_on=True, picon=_never)
    assert back is not None and back.content == NEW_SCREEN

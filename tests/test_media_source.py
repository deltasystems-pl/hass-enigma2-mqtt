"""The receivers' channels as a media source.

Receivers, then bouquets, then channels; a channel resolves to the receiver's own
stream address as `video/mp2t`. The tree is the channel list the integration already
holds, without the bouquets the options hide, and a channel resolves only while it is
in that tree. Before an address is handed out the receiver is asked, where it can be,
whether the channel could be played now.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from homeassistant.components import media_source, mqtt
from homeassistant.components.media_player import BrowseError
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt import box as box_module, stream_facts
from custom_components.enigma2_mqtt.const import CONF_BOUQUETS

from .conftest import (
    AVAILABILITY_TOPIC,
    BASE_TOPIC,
    CHANNELS,
    CHANNELS_TOPIC,
    INFO,
    INFO_TOPIC,
    IP,
    LAST_ERROR_TOPIC,
    NODE_ID,
    SREF,
    SREF_TWO,
    async_setup_box_then_retained,
    command_topic,
    opened_command,
    published_payloads,
)

STREAM_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/stream"
PLAYABLE_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/stream_playable"
STREAM: dict[str, Any] = {
    "port": 8001, "path": "/{sref}", "authentication": False, "encoder": False,
    "encoder_port": None,
}
ROOT = "media-source://enigma2_mqtt"
RECEIVER = f"{ROOT}/{NODE_ID}"
FAVOURITES = CHANNELS["bouquets"][0]["sref"]
SPORT = CHANNELS["bouquets"][1]["sref"]
EUROSPORT = "1:0:19:1234:3F3:1:C00000:0:0:0:"
KODI = "media_player.kodi"


def bouquet_id(sref: str) -> str:
    return f"{RECEIVER}/b/{quote(sref, safe=':')}"


def channel_id(sref: str) -> str:
    return f"{RECEIVER}/c/{quote(sref, safe=':')}"


def info_with(*capabilities: str) -> str:
    return json.dumps({**INFO, "capabilities": [*INFO["capabilities"], *capabilities]})


def with_options(entry: MockConfigEntry, **options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        options=options,
    )


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    store: dict[str, str | bytes],
    *capabilities: str,
    stream: dict[str, Any] | None = STREAM,
) -> None:
    store[INFO_TOPIC] = info_with("stream", *capabilities)
    if stream is not None:
        store[STREAM_TOPIC] = json.dumps(stream)
    assert await async_setup_component(hass, "media_source", {})
    await async_setup_box_then_retained(hass, entry, store)


async def _arm_playable(
    hass: HomeAssistant, playable: bool, *, say_id: bool = True, retain: bool = False
) -> None:
    """Make the fake receiver answer `cmd/stream_playable` on its event topic."""

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        command_id, payload = opened_command(msg.payload)
        try:
            sref = json.loads(payload)["sref"]
        except (ValueError, TypeError, KeyError):
            sref = payload
        answer: dict[str, Any] = {"sref": sref, "playable": playable, "ts": 1789459213}
        if say_id and command_id is not None:
            answer["id"] = command_id
        async_fire_mqtt_message(hass, PLAYABLE_TOPIC, json.dumps(answer), retain=retain)

    await mqtt.async_subscribe(hass, command_topic("stream_playable"), _command_received)


# ------------------------------------------------------------------- browsing


async def test_the_root_lists_the_receivers_that_have_a_stream_server(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, box_on_the_broker)

    root = await media_source.async_browse_media(hass, ROOT)

    assert [(child.title, child.media_content_id) for child in root.children] == [
        ("Dekoder salon", RECEIVER)
    ]
    assert root.children[0].can_expand is True
    assert root.children[0].can_play is False


async def test_a_receiver_that_does_not_claim_stream_is_not_in_the_tree(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Plugin 0.4.0: no stream facts, so nothing here could resolve."""
    assert await async_setup_component(hass, "media_source", {})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    root = await media_source.async_browse_media(hass, ROOT)

    assert root.children == []
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(hass, RECEIVER)
    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)
    assert raised.value.translation_key == "stream_source_unknown"


async def test_a_receiver_lists_its_bouquets_and_a_bouquet_its_channels(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, box_on_the_broker)

    receiver = await media_source.async_browse_media(hass, RECEIVER)
    assert [(child.title, child.media_content_id) for child in receiver.children] == [
        ("Ulubione TV", bouquet_id(FAVOURITES)),
        ("Sport", bouquet_id(SPORT)),
    ]

    bouquet = await media_source.async_browse_media(hass, bouquet_id(FAVOURITES))
    assert [
        (child.title, child.media_content_id, child.media_content_type, child.can_play)
        for child in bouquet.children
    ] == [
        ("TVP 1 HD", channel_id(SREF), "video/mp2t", True),
        ("TVN HD", channel_id(SREF_TWO), "video/mp2t", True),
    ]


async def test_the_tree_is_built_from_the_list_already_held_and_asks_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """No command and no second subscription: the channel topics are the source."""
    await _setup(hass, config_entry, box_on_the_broker)
    before = len(mqtt_mock.async_publish.call_args_list)
    subscribed = mqtt_mock.async_subscribe.call_count

    await media_source.async_browse_media(hass, RECEIVER)
    await media_source.async_browse_media(hass, bouquet_id(SPORT))

    assert len(mqtt_mock.async_publish.call_args_list) == before
    assert mqtt_mock.async_subscribe.call_count == subscribed


async def test_the_bouquets_option_narrows_the_tree(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    entry = with_options(config_entry, **{CONF_BOUQUETS: ["Sport"]})
    await _setup(hass, entry, box_on_the_broker)

    receiver = await media_source.async_browse_media(hass, RECEIVER)

    assert [child.title for child in receiver.children] == ["Sport"]
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(hass, bouquet_id(FAVOURITES))


async def test_a_hidden_bouquet_is_left_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Not listed, not browsable by its address, and its channels do not resolve."""
    entry = with_options(config_entry, history_hidden_bouquets=["Sport"])
    await _setup(hass, entry, box_on_the_broker)

    receiver = await media_source.async_browse_media(hass, RECEIVER)
    assert [child.title for child in receiver.children] == ["Ulubione TV"]

    with pytest.raises(BrowseError) as browse:
        await media_source.async_browse_media(hass, bouquet_id(SPORT))
    assert browse.value.translation_key == "stream_source_unknown"

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(EUROSPORT), KODI)
    assert raised.value.translation_key == "stream_source_unknown"
    assert published_payloads(mqtt_mock, command_topic("stream_playable")) == []


async def test_a_channel_of_a_hidden_bouquet_is_left_out_of_every_other_bouquet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """TVN HD is in the hidden bouquet as well, so the shown bouquet loses it."""
    channels = json.loads(json.dumps(CHANNELS))
    channels["bouquets"][1]["channels"].append({"sref": SREF_TWO, "name": "TVN HD"})
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(channels)
    entry = with_options(config_entry, history_hidden_bouquets=["Sport"])
    await _setup(hass, entry, box_on_the_broker)

    bouquet = await media_source.async_browse_media(hass, bouquet_id(FAVOURITES))

    assert [child.title for child in bouquet.children] == ["TVP 1 HD"]
    # Not in the tree at all - not "a channel with no address".
    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF_TWO), KODI)
    assert raised.value.translation_key == "stream_source_unknown"


# ------------------------------------------------------------------ resolving


async def test_a_channel_resolves_to_the_receivers_transport_stream(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, box_on_the_broker)

    play = await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert play.url == f"http://{IP}:8001/{SREF}"
    assert play.mime_type == "video/mp2t"
    # A receiver that does not claim `stream_playable` is not asked.
    assert published_payloads(mqtt_mock, command_topic("stream_playable")) == []


async def test_the_reference_in_the_address_is_matched_by_identity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An automation may spell it without the last colon; the list's spelling is used."""
    await _setup(hass, config_entry, box_on_the_broker)

    play = await media_source.async_resolve_media(
        hass, channel_id(SREF.rstrip(":").lower()), KODI
    )

    assert play.url == f"http://{IP}:8001/{SREF}"


@pytest.mark.parametrize(
    "identifier",
    [
        f"{ROOT}/vuuno4kse_999999/c/{SREF}",
        f"{RECEIVER}/c/1:0:19:FFFF:3FB:1:C00000:0:0:0:",
        f"{RECEIVER}/c/",
        f"{RECEIVER}/x/{SREF}",
        RECEIVER,
    ],
)
async def test_what_is_not_a_channel_of_the_tree_does_not_resolve(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    identifier: str,
) -> None:
    await _setup(hass, config_entry, box_on_the_broker)

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, identifier, KODI)

    assert raised.value.translation_key == "stream_source_unknown"


async def test_the_browsers_own_player_is_told_it_cannot_play_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Home Assistant resolves without a media player only for the browser."""
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable")

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), None)

    assert raised.value.translation_key == "stream_player_browser"
    assert published_payloads(mqtt_mock, command_topic("stream_playable")) == []


async def test_chromecast_is_told_it_cannot_play_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, box_on_the_broker)
    cast = er.async_get(hass).async_get_or_create("media_player", "cast", "abc-123")
    kodi = er.async_get(hass).async_get_or_create("media_player", "kodi", "def-456")

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), cast.entity_id)
    assert raised.value.translation_key == "stream_player_cast"

    play = await media_source.async_resolve_media(hass, channel_id(SREF), kodi.entity_id)
    assert play.mime_type == "video/mp2t"


async def test_a_stream_server_that_wants_a_login_does_not_resolve(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Never an address with credentials, and none that only answers 401."""
    await _setup(
        hass, config_entry, box_on_the_broker, stream={**STREAM, "authentication": True}
    )

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert raised.value.translation_key == "stream_login_required"


@pytest.mark.parametrize(
    "stream",
    [None, {**STREAM, "port": "8001"}, {**STREAM, "path": "/?{sref}"}],
    ids=["absent", "port", "path"],
)
async def test_no_usable_stream_facts_is_said_as_such(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    stream: dict[str, Any] | None,
) -> None:
    """The capability is claimed and the topic has not arrived, or cannot be used.

    That is not the receiver's address being unknown, and "enter the host with
    Reconfigure" would send somebody to fix what is not broken.
    """
    await _setup(hass, config_entry, box_on_the_broker, stream=stream)

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert raised.value.translation_key == "stream_no_facts"


async def test_no_address_for_the_receiver_is_said_as_such(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Loopback is not where a receiver is: the facts are there, an address is not."""
    await _setup(hass, config_entry, box_on_the_broker)
    async_fire_mqtt_message(
        hass,
        INFO_TOPIC,
        json.dumps(
            {**INFO, "ip": "127.0.0.1", "capabilities": [*INFO["capabilities"], "stream"]}
        ),
    )
    await hass.async_block_till_done()

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert raised.value.translation_key == "stream_no_address"


# ------------------------------------------------- can it be played right now


async def test_a_channel_the_receiver_cannot_play_now_does_not_resolve(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Every tuner it could use is busy: said before a player opens a dead stream."""
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")
    await _arm_playable(hass, False)

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert raised.value.translation_key == "stream_not_playable"
    assert raised.value.translation_placeholders == {"channel": "TVP 1 HD"}
    (sent,) = published_payloads(mqtt_mock, command_topic("stream_playable"))
    command_id, payload = opened_command(sent)
    assert command_id
    assert payload == SREF


async def test_a_channel_the_receiver_can_play_resolves(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")
    await _arm_playable(hass, True)

    play = await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert play.url == f"http://{IP}:8001/{SREF}"


async def test_an_answer_without_an_id_is_matched_by_the_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A question sent bare, or one whose id the plugin dropped, is told by `sref`."""
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable")
    await _arm_playable(hass, False, say_id=False)

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert raised.value.translation_key == "stream_not_playable"
    assert published_payloads(mqtt_mock, command_topic("stream_playable")) == [SREF]


async def test_somebody_elses_answer_is_not_taken(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another id, another channel, a retained replay, a `playable` that is no boolean."""
    monkeypatch.setattr(box_module, "STREAM_PLAYABLE_TIMEOUT", 0.2)
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        command_id, payload = opened_command(msg.payload)
        for answer, retain in (
            ({"sref": payload, "playable": False, "id": "somebody-else"}, False),
            ({"sref": SREF_TWO, "playable": False}, False),
            ({"sref": payload, "playable": False, "id": command_id}, True),
            ({"sref": payload, "playable": "no", "id": command_id}, False),
            ({"playable": False}, False),
        ):
            async_fire_mqtt_message(hass, PLAYABLE_TOPIC, json.dumps(answer), retain=retain)
        async_fire_mqtt_message(hass, PLAYABLE_TOPIC, "not json")

    await mqtt.async_subscribe(hass, command_topic("stream_playable"), _command_received)

    play = await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert play.url == f"http://{IP}:8001/{SREF}"


async def test_no_answer_in_time_is_unknown_and_the_address_is_handed_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence is not "no": the wait is short, and it ends in the address."""
    monkeypatch.setattr(box_module, "STREAM_PLAYABLE_TIMEOUT", 0.05)
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")

    started = asyncio.get_running_loop().time()
    play = await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert play.url == f"http://{IP}:8001/{SREF}"
    assert asyncio.get_running_loop().time() - started < 2
    assert len(published_payloads(mqtt_mock, command_topic("stream_playable"))) == 1
    # Nothing is left waiting: a late answer is nobody's.
    async_fire_mqtt_message(
        hass, PLAYABLE_TOPIC, json.dumps({"sref": SREF, "playable": False})
    )
    await hass.async_block_till_done()
    assert config_entry.runtime_data._questions == []  # noqa: SLF001


async def test_the_wait_is_bounded_by_the_constant(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A few seconds, as the contract says - a media browser is waiting on it."""
    from custom_components.enigma2_mqtt.const import STREAM_PLAYABLE_TIMEOUT

    assert 1 <= STREAM_PLAYABLE_TIMEOUT <= 5
    assert box_module.STREAM_PLAYABLE_TIMEOUT == STREAM_PLAYABLE_TIMEOUT


async def test_a_question_the_receiver_refuses_is_unknown_at_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal on `last_error` with the question's id ends the wait, as unknown."""
    monkeypatch.setattr(box_module, "STREAM_PLAYABLE_TIMEOUT", 30)
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        command_id, _payload = opened_command(msg.payload)
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps(
                {"cmd": "stream_playable", "id": command_id, "ts": 1789459213,
                 "error": "this image cannot say whether a service can be played"}
            ),
        )

    await mqtt.async_subscribe(hass, command_topic("stream_playable"), _command_received)

    started = asyncio.get_running_loop().time()
    play = await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert play.url == f"http://{IP}:8001/{SREF}"
    # At once, not at the end of the thirty seconds the wait was given here.
    assert asyncio.get_running_loop().time() - started < 5


async def test_a_receiver_that_is_offline_is_not_asked(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable")
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    play = await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert play.url == f"http://{IP}:8001/{SREF}"
    assert published_payloads(mqtt_mock, command_topic("stream_playable")) == []


async def test_a_refusal_without_an_id_ends_the_wait_as_unknown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin without ids refuses by name: the refusal is about `stream_playable`.

    "takes the reference of one playable service" is what the plugin says for a bouquet,
    a marker or a hidden entry. It is not a "no", and the address is handed out.
    """
    monkeypatch.setattr(box_module, "STREAM_PLAYABLE_TIMEOUT", 30)
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable")

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps(
                {"cmd": "stream_playable", "ts": 1789459213,
                 "error": "cmd/stream_playable takes the reference of one playable service"}
            ),
        )

    await mqtt.async_subscribe(hass, command_topic("stream_playable"), _command_received)

    started = asyncio.get_running_loop().time()
    play = await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert play.url == f"http://{IP}:8001/{SREF}"
    assert asyncio.get_running_loop().time() - started < 5


async def test_a_refusal_of_another_command_does_not_end_the_wait(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A zap refused while the question is out is the zap's, and the answer still counts."""
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable")

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps({"cmd": "zap", "ts": 1789459213, "error": "no such channel"}),
        )
        async_fire_mqtt_message(
            hass,
            PLAYABLE_TOPIC,
            json.dumps({"sref": SREF, "playable": False, "ts": 1789459213}),
        )

    await mqtt.async_subscribe(hass, command_topic("stream_playable"), _command_received)

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert raised.value.translation_key == "stream_not_playable"


async def test_a_list_entry_that_is_no_single_service_is_not_asked_about(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The plugin's channel list holds none, and its question refuses them on `last_error`."""
    marker = "1:64:0:0:0:0:0:0:0:0::Sport"
    channels = json.loads(json.dumps(CHANNELS))
    channels["bouquets"][0]["channels"].append({"sref": marker, "name": "Sport"})
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(channels)
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")
    await _arm_playable(hass, False)

    play = await media_source.async_resolve_media(hass, channel_id(marker), KODI)

    assert play.url.startswith(f"http://{IP}:8001/1:64:0:0:0:0:0:0:0:0::")
    assert published_payloads(mqtt_mock, command_topic("stream_playable")) == []


@pytest.mark.parametrize(
    ("sref", "one"),
    [
        (SREF, True),
        ("4097:0:1:0:0:0:0:0:0:0:http%3a//example.org/x.ts:IPTV", True),
        (" 1:0:19:283D:3FB:1:C00000:0:0:0: ", True),
        ("1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"userbouquet.fav.tv\"", False),
        ("1:134:1:0:0:0:0:0:0:0:FROM BOUQUET \"alternatives.x.tv\"", False),
        ("1:64:0:0:0:0:0:0:0:0::Marker", False),
        ("1:320:0:0:0:0:0:0:0:0::Numbered", False),
        ("1:512:19:283D:3FB:1:C00000:0:0:0:", False),
        ("1:2:0:0:0:0:0:0:0:0:", False),
        ("1:x:19:283D", False),
        ("1:0:19", False),
        ("", False),
        (None, False),
    ],
)
def test_one_playable_service_is_the_receivers_own_test(sref: Any, one: bool) -> None:
    assert stream_facts.one_playable_service(sref) is one


async def test_a_question_that_cannot_be_published_is_unknown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publish that fails asked nothing, so nothing was answered "no"."""
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")

    async def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise HomeAssistantError("the broker is gone")

    monkeypatch.setattr(config_entry.runtime_data, "async_publish_cmd", _refuse)

    play = await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert play.url == f"http://{IP}:8001/{SREF}"


async def test_an_answer_that_is_no_boolean_answers_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`playable` 0 is not the plugin's answer; the boolean that follows it is."""
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable")

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        for playable in (0, "no", False):
            async_fire_mqtt_message(
                hass,
                PLAYABLE_TOPIC,
                json.dumps({"sref": SREF, "playable": playable, "ts": 1789459213}),
            )

    await mqtt.async_subscribe(hass, command_topic("stream_playable"), _command_received)

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert raised.value.translation_key == "stream_not_playable"


async def test_what_arrives_before_the_question_goes_out_is_not_its_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another client's answer about the same channel, and a refusal, while subscribing.

    Both arrive while the question waits for the subscription and before it is
    published, so neither can be about it. Only the answer after the publish counts.
    """
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")
    box = config_entry.runtime_data
    subscribed = box.async_wait_subscribed

    async def _wait_subscribed(*args: Any, **kwargs: Any) -> None:
        async_fire_mqtt_message(
            hass,
            PLAYABLE_TOPIC,
            json.dumps({"sref": SREF, "playable": True, "ts": 1789459213}),
        )
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps(
                {"cmd": "stream_playable", "ts": 1789459213,
                 "error": "this image cannot say whether a service can be played"}
            ),
        )
        await hass.async_block_till_done()
        await subscribed(*args, **kwargs)

    monkeypatch.setattr(box, "async_wait_subscribed", _wait_subscribed)
    await _arm_playable(hass, False)

    with pytest.raises(media_source.Unresolvable) as raised:
        await media_source.async_resolve_media(hass, channel_id(SREF), KODI)

    assert raised.value.translation_key == "stream_not_playable"


async def test_unloading_the_receiver_ends_the_wait(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question still out when the entry goes is "not known" at once."""
    monkeypatch.setattr(box_module, "STREAM_PLAYABLE_TIMEOUT", 30)
    await _setup(hass, config_entry, box_on_the_broker, "stream_playable", "command_id")
    box = config_entry.runtime_data
    # A receiver that hears the question and has not answered yet.
    heard: list[ReceiveMessage] = []
    await mqtt.async_subscribe(hass, command_topic("stream_playable"), heard.append)
    # Not a task Home Assistant waits for: the wait below is what is being tested.
    waiting = asyncio.ensure_future(box.async_stream_playable(SREF))
    async with asyncio.timeout(5):
        while not heard:
            await asyncio.sleep(0.01)
    await hass.async_block_till_done()
    assert not waiting.done()
    assert [opened_command(msg.payload)[1] for msg in heard] == [SREF]

    box.async_stop()

    assert await asyncio.wait_for(waiting, 5) is None

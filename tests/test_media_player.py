"""The media player: what it shows, what it sends, and what it can browse."""

from __future__ import annotations

import json

from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_INPUT_SOURCE_LIST,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    DATA_COMPONENT,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
    SERVICE_SELECT_SOURCE,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    MediaPlayerState,
)
from homeassistant.components.media_player.errors import BrowseError
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_mock_service,
)

from custom_components.enigma2_mqtt.const import CONF_BOUQUETS

from .conftest import (
    AVAILABILITY_TOPIC,
    MAC,
    PICON_URL,
    POWER_TOPIC,
    SCREEN,
    SCREEN_TOPIC,
    SREF,
    SREF_TWO,
    assert_published,
    async_setup_box,
    command_topic,
)

PLAYER = "media_player.dekoder_salon"


async def _call(hass: HomeAssistant, service: str, **data) -> None:
    """Call one media player action on the example box."""
    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN,
        service,
        {ATTR_ENTITY_ID: PLAYER, **data},
        blocking=True,
    )


async def test_a_watching_box_is_playing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Everything on the screen comes off the retained topics."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get(PLAYER)
    assert state is not None
    assert state.state == MediaPlayerState.PLAYING
    assert state.attributes[ATTR_INPUT_SOURCE] == "TVP 1 HD"
    assert state.attributes["media_channel"] == "TVP 1 HD"
    assert state.attributes["media_title"] == "Wiadomości"
    assert state.attributes["media_series_title"] == "Serwis informacyjny"
    assert state.attributes[ATTR_MEDIA_CONTENT_ID] == SREF
    assert state.attributes[ATTR_MEDIA_VOLUME_LEVEL] == 0.35
    assert state.attributes[ATTR_MEDIA_VOLUME_MUTED] is False
    assert state.attributes["media_duration"] == 1500
    assert state.attributes["device_class"] == "receiver"


async def test_standby_reads_as_off(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A dark television is off, whichever kind of standby it is in."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, POWER_TOPIC, "standby")
    await hass.async_block_till_done()
    assert hass.states.get(PLAYER).state == MediaPlayerState.OFF


async def test_an_unreachable_box_is_off_but_not_unavailable(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The player stays usable when the box is gone: it is the wake button."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    assert hass.states.get(PLAYER).state == MediaPlayerState.OFF


async def test_the_source_list_is_every_selected_bouquet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Channels come from the `channels` topic, and a duplicate name is listed once."""
    await async_setup_box(hass, config_entry)

    sources = hass.states.get(PLAYER).attributes[ATTR_INPUT_SOURCE_LIST]
    assert sources == ["TVP 1 HD", "TVN HD", "Eurosport 1"]


async def test_the_bouquet_option_narrows_the_source_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A household that only watches one bouquet is not shown the rest."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_BOUQUETS: ["Ulubione TV"]}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(PLAYER).attributes[ATTR_INPUT_SOURCE_LIST] == [
        "TVP 1 HD",
        "TVN HD",
    ]


async def test_selecting_a_unique_source_zaps_by_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A name the box can resolve by itself is sent as a name."""
    await async_setup_box(hass, config_entry)

    await _call(hass, SERVICE_SELECT_SOURCE, **{ATTR_INPUT_SOURCE: "TVP 1 HD"})

    assert_published(
        mqtt_mock, command_topic("zap"), json.dumps({"name": "TVP 1 HD"})
    )


async def test_selecting_an_ambiguous_source_zaps_by_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A name in two bouquets would be refused, so the reference is sent instead."""
    await async_setup_box(hass, config_entry)

    await _call(hass, SERVICE_SELECT_SOURCE, **{ATTR_INPUT_SOURCE: "TVN HD"})

    assert_published(mqtt_mock, command_topic("zap"), SREF_TWO)


async def test_selecting_an_unknown_source_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A channel that is in no bouquet is a mistake worth naming."""
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_SELECT_SOURCE, **{ATTR_INPUT_SOURCE: "HBO"})


async def test_playing_a_channel_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`play_media` with a reference is what the media browser sends."""
    await async_setup_box(hass, config_entry)

    await _call(
        hass,
        SERVICE_PLAY_MEDIA,
        **{ATTR_MEDIA_CONTENT_TYPE: "channel", ATTR_MEDIA_CONTENT_ID: SREF},
    )

    assert_published(mqtt_mock, command_topic("zap"), SREF)


async def test_playing_a_channel_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`channel_name` is for an automation written by a person, not a browser."""
    await async_setup_box(hass, config_entry)

    await _call(
        hass,
        SERVICE_PLAY_MEDIA,
        **{ATTR_MEDIA_CONTENT_TYPE: "channel_name", ATTR_MEDIA_CONTENT_ID: "TVP 1 HD"},
    )

    assert_published(
        mqtt_mock, command_topic("zap"), json.dumps({"name": "TVP 1 HD"})
    )


async def test_playing_anything_else_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver plays channels; it is not a music player."""
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            SERVICE_PLAY_MEDIA,
            **{ATTR_MEDIA_CONTENT_TYPE: "music", ATTR_MEDIA_CONTENT_ID: "x"},
        )


async def test_volume_and_mute(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Home Assistant's 0-1 scale becomes the receiver's 0-100."""
    await async_setup_box(hass, config_entry)

    await _call(hass, SERVICE_VOLUME_SET, **{ATTR_MEDIA_VOLUME_LEVEL: 0.42})
    assert_published(mqtt_mock, command_topic("volume"), "42")

    await _call(hass, SERVICE_VOLUME_MUTE, **{ATTR_MEDIA_VOLUME_MUTED: True})
    assert_published(mqtt_mock, command_topic("mute"), "ON")


async def test_turning_a_reachable_box_on_uses_mqtt(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A box that is only in standby is still listening."""
    await async_setup_box(hass, config_entry)

    await _call(hass, SERVICE_TURN_ON)
    assert_published(mqtt_mock, command_topic("power"), "on")

    await _call(hass, SERVICE_TURN_OFF)
    assert_published(mqtt_mock, command_topic("power"), "standby")


async def test_turning_an_unreachable_box_on_sends_a_magic_packet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Deep standby is off the network, so MQTT cannot reach it and WoL can."""
    await async_setup_box(hass, config_entry)
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    magic_packets = async_mock_service(hass, "wake_on_lan", "send_magic_packet")
    await _call(hass, SERVICE_TURN_ON)

    assert len(magic_packets) == 1
    assert magic_packets[0].data["mac"] == MAC


async def test_next_and_previous_track_zap(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """There is no playlist on a receiver; the next track is the next channel."""
    await async_setup_box(hass, config_entry)

    await _call(hass, SERVICE_MEDIA_NEXT_TRACK)
    assert_published(mqtt_mock, command_topic("key"), "KEY_CHANNELUP")

    await _call(hass, SERVICE_MEDIA_PREVIOUS_TRACK)
    assert_published(mqtt_mock, command_topic("key"), "KEY_CHANNELDOWN")


async def test_browsing_the_root_lists_bouquets(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The first level of the browser is the bouquets the box published."""
    await async_setup_box(hass, config_entry)

    entity = _player(hass)
    browse = await entity.async_browse_media()

    assert browse.media_content_id == "bouquets"
    assert [child.title for child in browse.children] == ["Ulubione TV", "Sport"]
    assert all(child.can_expand for child in browse.children)
    assert not any(child.can_play for child in browse.children)


async def test_browsing_a_bouquet_lists_playable_channels_with_picons(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The second level is channels, each with the picon OpenWebif serves."""
    await async_setup_box(hass, config_entry)

    entity = _player(hass)
    root = await entity.async_browse_media()
    bouquet = await entity.async_browse_media(
        media_content_id=root.children[0].media_content_id
    )

    assert [child.title for child in bouquet.children] == ["TVP 1 HD", "TVN HD"]
    assert bouquet.children[0].media_content_id == SREF
    assert bouquet.children[0].can_play is True
    assert bouquet.children[0].thumbnail == PICON_URL


async def test_browsing_an_unknown_bouquet_is_an_error(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A stale link in the browser says so rather than showing an empty list."""
    await async_setup_box(hass, config_entry)

    with pytest.raises(BrowseError):
        await _player(hass).async_browse_media(media_content_id="bouquet:nonsense")


async def test_browsing_a_box_with_no_channel_list_is_an_error(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A box that never published its channels cannot be browsed, and says why."""
    retained[AVAILABILITY_TOPIC] = "online"
    await async_setup_box(hass, config_entry)

    with pytest.raises(BrowseError):
        await _player(hass).async_browse_media()


async def test_the_media_image_is_the_screenshot(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The picture of what is playing is the frame the plugin grabbed."""
    await async_setup_box(hass, config_entry)
    entity = _player(hass)

    assert await entity.async_get_media_image() == (SCREEN, "image/jpeg")
    first_hash = entity.media_image_hash

    async_fire_mqtt_message(hass, SCREEN_TOPIC, b"\xff\xd8another-frame\xff\xd9")
    await hass.async_block_till_done()

    # The hash is what makes the browser fetch the new frame instead of its cache.
    assert entity.media_image_hash != first_hash


def _player(hass: HomeAssistant):
    """Return the media player entity object, for the methods with no action."""
    return hass.data[DATA_COMPONENT].get_entity(PLAYER)

"""A hidden bouquet is offered nowhere: the EPG, the source list's scope, zaps by name.

The option "Bouquets hidden from Recently watched" takes a bouquet and its channels out of
every list Home Assistant shows - selects, the browser, the EPG of other channels, the
recordings - and out of the repair that names what the receiver withheld. What is on the
screen stays visible, and a zap by service reference is not judged; a zap by a name only a
hidden bouquet's channel has is refused like a channel the receiver does not have.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
    SERVICE_SELECT_SOURCE,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.withheld import withheld_lines

from .conftest import (
    BASE_TOPIC,
    BOUQUET,
    BOUQUET_TOPIC,
    CHANNELS,
    CHANNELS_TOPIC,
    EPG_GRID_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    SREF,
    SREF_TWO,
    async_setup_box_then_retained,
    command_topic,
)
from .test_epg_active_bouquet import (
    FAVOURITES_GRID,
    SENSOR,
    SPORT_CONTEXT,
    SPORT_GRID,
    SPORT_GRID_TOPIC,
    TEN_MINUTES_IN,
    WITH_CONTEXT,
)

PLAYER = "media_player.dekoder_salon"
SPORT = CHANNELS["bouquets"][1]
EUROSPORT = SPORT["channels"][0]["sref"]
TVN_IN_SPORT = SPORT["channels"][1]["sref"]
BOUQUETS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/bouquets"


def _entry(entry: MockConfigEntry, **options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        options=options,
    )


def _hiding(entry: MockConfigEntry, **options: Any) -> MockConfigEntry:
    return _entry(entry, history_hidden_bouquets=["Sport"], **options)


def _grids(store: dict[str, str | bytes], *, on: dict[str, Any] = BOUQUET) -> None:
    """Both grids, the favourites' one also listing Eurosport 1, a channel of "Sport"."""
    favourites = {
        **FAVOURITES_GRID,
        "channels": [
            *FAVOURITES_GRID["channels"],
            {"sref": EUROSPORT, "name": "Eurosport 1", "events": []},
        ],
    }
    store[INFO_TOPIC] = json.dumps(WITH_CONTEXT)
    store[BOUQUET_TOPIC] = json.dumps(on)
    store[EPG_GRID_TOPIC] = json.dumps(favourites)
    store[SPORT_GRID_TOPIC] = json.dumps(SPORT_GRID)


def _published(mqtt_mock, topic: str) -> list[str]:
    return [
        call.args[1]
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == topic
    ]


# ----------------------------------------------------------------------- the EPG


async def test_the_epg_sensor_leaves_a_hidden_bouquets_channel_out_of_another_grid(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(TEN_MINUTES_IN)
    _grids(box_on_the_broker)
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    names = [channel["name"] for channel in hass.states.get(SENSOR).attributes["channels"]]
    assert names == ["TVP 1 HD", "TVN HD"]


async def test_without_the_option_the_epg_sensor_lists_the_whole_grid(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(TEN_MINUTES_IN)
    _grids(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    names = [channel["name"] for channel in hass.states.get(SENSOR).attributes["channels"]]
    assert names == ["TVP 1 HD", "TVN HD", "Eurosport 1"]


async def test_on_a_hidden_bouquet_the_epg_sensor_reads_as_no_bouquet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(TEN_MINUTES_IN)
    _grids(box_on_the_broker, on=SPORT_CONTEXT)
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state.state == "0"
    assert state.attributes["channels"] == []
    assert "Snooker" not in json.dumps(dict(state.attributes))


async def test_get_epg_grid_leaves_hidden_bouquets_and_their_channels_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    _grids(box_on_the_broker)
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    response = await hass.services.async_call(
        "enigma2_mqtt", "get_epg_grid", {ATTR_ENTITY_ID: PLAYER},
        blocking=True, return_response=True,
    )
    grids = response[PLAYER]["bouquets"]
    assert [grid["bouquet"] for grid in grids] == ["Ulubione TV"]
    assert [channel["name"] for channel in grids[0]["channels"]] == ["TVP 1 HD", "TVN HD"]

    # Asked for by name or by slug, it is a bouquet with no grid here.
    for asked in ("Sport", "sport"):
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                "enigma2_mqtt", "get_epg_grid", {ATTR_ENTITY_ID: PLAYER, "bouquet": asked},
                blocking=True, return_response=True,
            )
        assert raised.value.translation_key == "unknown_bouquet"


async def test_without_the_option_get_epg_grid_returns_every_grid_whole(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    _grids(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    response = await hass.services.async_call(
        "enigma2_mqtt", "get_epg_grid", {ATTR_ENTITY_ID: PLAYER},
        blocking=True, return_response=True,
    )
    grids = response[PLAYER]["bouquets"]
    assert [grid["bouquet"] for grid in grids] == ["Sport", "Ulubione TV"]
    assert len(grids[1]["channels"]) == 3


# --------------------------------------------------------- the source list's scope


async def test_scoped_to_a_hidden_active_bouquet_the_source_list_falls_back(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Like a bouquet the bouquets option leaves out: the full list, without Sport's."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "capabilities": [*INFO["capabilities"], "bouquet_context"]}
    )
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(SPORT_CONTEXT)
    entry = _hiding(config_entry, source_list_scope="active_bouquet")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    assert hass.states.get(PLAYER).attributes["source_list"] == ["TVP 1 HD", "TVN HD"]
    # The fallback is the household's choice, and the hidden bouquet is not named in the log.
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "Sport" in record.getMessage()
    ]
    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN, SERVICE_SELECT_SOURCE,
        {ATTR_ENTITY_ID: PLAYER, ATTR_INPUT_SOURCE: "TVP 1 HD"}, blocking=True,
    )
    assert _published(mqtt_mock, command_topic("zap"))


# --------------------------------------------------------------- zaps by name


async def test_a_hidden_channels_name_is_refused_everywhere_a_name_is_taken(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    calls = [
        (MEDIA_PLAYER_DOMAIN, SERVICE_SELECT_SOURCE, {ATTR_INPUT_SOURCE: "Eurosport 1"}),
        (
            MEDIA_PLAYER_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {ATTR_MEDIA_CONTENT_TYPE: "channel_name", ATTR_MEDIA_CONTENT_ID: "Eurosport 1"},
        ),
        ("enigma2_mqtt", "zap", {"name": "Eurosport 1"}),
    ]
    for domain, service, data in calls:
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: PLAYER, **data}, blocking=True
            )
        assert raised.value.translation_key == "unknown_channel", service
    assert _published(mqtt_mock, command_topic("zap")) == []


async def test_a_hidden_channel_is_still_zapped_to_by_its_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN, SERVICE_PLAY_MEDIA,
        {ATTR_ENTITY_ID: PLAYER, ATTR_MEDIA_CONTENT_TYPE: "channel",
         ATTR_MEDIA_CONTENT_ID: EUROSPORT},
        blocking=True,
    )
    assert _published(mqtt_mock, command_topic("zap")) == [EUROSPORT]


async def test_a_name_shared_with_a_hidden_channel_zaps_the_offered_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """"TVN HD" is in both bouquets: the copy in "Sport" is hidden, the other is sent."""
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN, SERVICE_SELECT_SOURCE,
        {ATTR_ENTITY_ID: PLAYER, ATTR_INPUT_SOURCE: "TVN HD"}, blocking=True,
    )
    (sent,) = _published(mqtt_mock, command_topic("zap"))
    assert TVN_IN_SPORT not in sent


async def test_without_the_option_every_name_is_taken(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN, SERVICE_SELECT_SOURCE,
        {ATTR_ENTITY_ID: PLAYER, ATTR_INPUT_SOURCE: "Eurosport 1"}, blocking=True,
    )
    assert _published(mqtt_mock, command_topic("zap")) == [json.dumps({"name": "Eurosport 1"})]


# ------------------------------------------------------ what the screen shows


async def test_the_channel_on_the_screen_is_still_shown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A household automation reads what is on: it is not masked."""
    service_topic = f"{BASE_TOPIC}/{NODE_ID}/service"
    box_on_the_broker[service_topic] = json.dumps(
        {"name": "Eurosport 1", "sref": EUROSPORT, "bouquet": "Sport"}
    )
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    state = hass.states.get(PLAYER)
    assert state.attributes.get("media_channel") == "Eurosport 1"
    assert state.attributes.get("source") == "Eurosport 1"
    assert state.attributes.get("media_content_id") == EUROSPORT


# ------------------------------------------------------------- the repair


async def test_the_withheld_repair_does_not_name_a_hidden_bouquet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    index = {
        "generated": 1789459200,
        "bouquets": [
            {"name": "Ulubione TV", "sref": CHANNELS["bouquets"][0]["sref"],
             "slug": "ulubione_tv", "count": 2},
            {"name": "Sport", "sref": SPORT["sref"], "slug": "sport", "count": 2},
            {"name": "Sport extra", "sref": "1:7:1:0:0:0:0:0:0:0:x", "slug": "", "count": 1},
        ],
    }
    box_on_the_broker[BOUQUETS_TOPIC] = json.dumps(index)
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "not_published": [
                {"topic": "channels", "bytes": 2, "limit": 1},
                {"topic": "channels/sport", "bytes": 2, "limit": 1},
                {"topic": "epg_grid/sport", "bytes": 2, "limit": 1},
                {"topic": "epg_grid/ulubione_tv", "bytes": 2, "limit": 1},
            ],
        }
    )
    entry = _entry(config_entry, history_hidden_bouquets=["Sport", "Sport extra"])
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    text = withheld_lines(entry.runtime_data, "en")
    assert "Sport" not in text
    assert "the channel list of a bouquet hidden in Home Assistant" in text
    assert "the EPG of a bouquet hidden in Home Assistant" in text
    assert "the EPG of the bouquet \u201cUlubione TV\u201d" in text
    for language in ("pl", "de"):
        assert "Sport" not in withheld_lines(entry.runtime_data, language), language


async def test_the_channels_on_show_follow_a_channel_list_that_arrives(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The EPG sensor reads the channel list too, so it follows it without a new grid."""
    freezer.move_to(TEN_MINUTES_IN)
    _grids(box_on_the_broker)
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)
    assert "Eurosport 1" not in json.dumps(hass.states.get(SENSOR).attributes["channels"])

    channels = json.loads(json.dumps(CHANNELS))
    channels["bouquets"][1]["channels"] = [
        channel for channel in channels["bouquets"][1]["channels"] if channel["sref"] != EUROSPORT
    ]
    async_fire_mqtt_message(hass, CHANNELS_TOPIC, json.dumps(channels))
    await hass.async_block_till_done()
    names = [channel["name"] for channel in hass.states.get(SENSOR).attributes["channels"]]
    assert names == ["TVP 1 HD", "TVN HD", "Eurosport 1"]
    assert SREF in json.dumps(hass.states.get(SENSOR).attributes["channels"])


async def test_kanal_labels_still_map_to_the_channel_they_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Two "TVN HD" in the active bouquet, one of them the hidden bouquet's: the label
    left is the visible one, unnumbered, and it stands for that one."""
    channels = json.loads(json.dumps(CHANNELS))
    channels["bouquets"][0]["channels"].append({"sref": TVN_IN_SPORT, "name": "TVN HD"})
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(channels)
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "capabilities": [*INFO["capabilities"], "bouquet_context"]}
    )
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    channel_select = "select.dekoder_salon_channel"
    assert hass.states.get(channel_select).attributes["options"] == ["TVP 1 HD", "TVN HD"]
    entity = hass.data["entity_components"]["select"].get_entity(channel_select)
    assert entity._sref_for("TVN HD") == SREF_TWO


async def test_a_shared_name_never_tunes_the_hidden_copy_even_when_it_comes_first(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """With "Sport" listed first, its "TVN HD" is the first match by name; it is hidden,
    so the visible copy is the one sent."""
    channels = json.loads(json.dumps(CHANNELS))
    channels["bouquets"].reverse()
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(channels)
    await async_setup_box_then_retained(hass, _hiding(config_entry), box_on_the_broker)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN, SERVICE_PLAY_MEDIA,
        {ATTR_ENTITY_ID: PLAYER, ATTR_MEDIA_CONTENT_TYPE: "channel_name",
         ATTR_MEDIA_CONTENT_ID: "TVN HD"},
        blocking=True,
    )
    assert _published(mqtt_mock, command_topic("zap")) == [SREF_TWO]


async def test_a_hidden_channels_name_is_refused_also_outside_the_offered_bouquets(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """"Sport" hidden and not offered either: the plugin would still tune its channel by
    name, so the zap action refuses the name."""
    entry = _hiding(config_entry, bouquets=["Ulubione TV"])
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            "enigma2_mqtt", "zap", {ATTR_ENTITY_ID: PLAYER, "name": "Eurosport 1"},
            blocking=True,
        )
    assert raised.value.translation_key == "unknown_channel"
    assert _published(mqtt_mock, command_topic("zap")) == []
    # A name only an unoffered, visible bouquet has is the plain case: the receiver
    # answers for it, Home Assistant does not refuse it as hidden.
    assert entry.runtime_data.channel_name_hidden("No such channel") is False


async def test_without_the_hidden_option_an_unoffered_bouquets_name_is_not_judged(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    entry = _entry(config_entry, bouquets=["Ulubione TV"])
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    assert entry.runtime_data.channel_name_hidden("Eurosport 1") is False


async def test_the_withheld_repair_names_no_bare_slug_while_bouquets_are_hidden(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A slug the index does not name could be a hidden bouquet's."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "not_published": [
                {"topic": "channels/secret_slug", "bytes": 2, "limit": 1},
                {"topic": "epg_grid/secret_slug", "bytes": 2, "limit": 1},
            ],
        }
    )
    entry = _hiding(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    text = withheld_lines(entry.runtime_data, "en")
    assert "secret_slug" not in text
    assert "the channel list of a bouquet hidden in Home Assistant" in text
    assert "the EPG of a bouquet hidden in Home Assistant" in text


async def test_without_the_hidden_option_the_withheld_repair_names_the_slug(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "not_published": [{"topic": "epg_grid/secret_slug", "bytes": 2, "limit": 1}]}
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert "secret_slug" in withheld_lines(config_entry.runtime_data, "en")


async def test_a_name_with_an_offered_copy_is_not_refused_as_hidden(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """"TVN HD" has a copy in the hidden "Sport" and one that is offered."""
    entry = _hiding(config_entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    assert entry.runtime_data.channel_name_hidden("TVN HD") is False
    assert entry.runtime_data.channel_name_hidden("Eurosport 1") is True

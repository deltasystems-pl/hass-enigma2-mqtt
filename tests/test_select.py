"""„Bukiet" and „Kanał": what they list, and what selecting one sends.

Both selects exist because the box runs in *integration* mode, where the plugin retracts
the discovery entities it would otherwise publish — including the channel select. The
topics were always there; nothing in Home Assistant listed them.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DATA_COMPONENT,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from .conftest import (
    AVAILABILITY_TOPIC,
    BOUQUET,
    BOUQUET_TOPIC,
    CHANNELS,
    CHANNELS_TOPIC,
    INFO,
    INFO_TOPIC,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    assert_published,
    async_arm_box_error,
    async_arm_box_reply,
    async_setup_box,
    command_topic,
)

BOUQUET_SELECT = "select.dekoder_salon_bouquet"
CHANNEL_SELECT = "select.dekoder_salon_channel"

SPORT = CHANNELS["bouquets"][1]
SPORT_CONTEXT: dict[str, Any] = {"name": SPORT["name"], "sref": SPORT["sref"]}
# The receiver on a list the plugin was not told to publish. Both fields null is
# ordinary operation, not a fault: the radio list and the movie list look like this.
NO_CONTEXT: dict[str, Any] = {"name": None, "sref": None}


def _with_bouquet_context(store: dict[str, str | bytes]) -> None:
    """Make the example box one that can activate a channel-list context."""
    store[INFO_TOPIC] = json.dumps(
        {**INFO, "capabilities": [*INFO["capabilities"], "bouquet_context"]}
    )


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    """Pick one option, the way the interface does."""
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


# ------------------------------------------------------------------ when they exist


async def test_the_selects_need_the_bouquet_context_capability(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Without `cmd/bouquet` there is nothing to select into, so neither is created."""
    await async_setup_box(hass, config_entry)

    assert hass.states.get(BOUQUET_SELECT) is None
    assert hass.states.get(CHANNEL_SELECT) is None


async def test_the_selects_appear_when_the_box_says_it_can(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Both capabilities, both entities."""
    _with_bouquet_context(box_on_the_broker)
    await async_setup_box(hass, config_entry)

    assert hass.states.get(BOUQUET_SELECT) is not None
    assert hass.states.get(CHANNEL_SELECT) is not None


async def test_a_capability_that_arrives_late_still_creates_them(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver answers after setup, and the entities have to follow it there."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get(BOUQUET_SELECT) is None

    async_fire_mqtt_message(
        hass,
        INFO_TOPIC,
        json.dumps({**INFO, "capabilities": [*INFO["capabilities"], "bouquet_context"]}),
    )
    await hass.async_block_till_done()

    assert hass.states.get(BOUQUET_SELECT) is not None
    assert hass.states.get(CHANNEL_SELECT) is not None


async def test_losing_the_capability_takes_them_out_of_the_registry(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A downgraded plugin leaves no unavailable entity behind looking like a fault."""
    _with_bouquet_context(box_on_the_broker)
    await async_setup_box(hass, config_entry)
    registry = er.async_get(hass)
    assert registry.async_get(BOUQUET_SELECT) is not None

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    await hass.async_block_till_done()

    assert registry.async_get(BOUQUET_SELECT) is None
    assert registry.async_get(CHANNEL_SELECT) is None


# ------------------------------------------------------------------------ „Bukiet"


async def test_the_bouquet_select_lists_what_the_channels_topic_carries(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """In the order the box published them, and pointing at the active one."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    state = hass.states.get(BOUQUET_SELECT)
    assert state.attributes[ATTR_OPTIONS] == ["Ulubione TV", "Sport"]
    assert state.state == "Ulubione TV"


async def test_a_null_context_selects_nothing_rather_than_inventing_an_option(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Both fields null is the radio list or the movie list, not a fault."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(NO_CONTEXT)
    await async_setup_box(hass, config_entry)

    state = hass.states.get(BOUQUET_SELECT)
    assert state.attributes[ATTR_OPTIONS] == ["Ulubione TV", "Sport"]
    assert state.state == STATE_UNKNOWN


async def test_selecting_a_bouquet_sends_its_reference_and_waits(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The name is what a person reads; the reference is what goes on the wire."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass, "bouquet", BOUQUET_TOPIC, json.dumps(SPORT_CONTEXT)
    )

    await _select(hass, BOUQUET_SELECT, "Sport")

    assert_published(
        mqtt_mock, command_topic("bouquet"), json.dumps({"sref": SPORT["sref"]})
    )
    assert hass.states.get(BOUQUET_SELECT).state == "Sport"


async def test_a_refused_bouquet_raises_with_the_boxs_own_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A control that springs back silently is the defect this replaces."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)
    await async_arm_box_error(hass, "bouquet", "bouquet has no playable service")

    with pytest.raises(HomeAssistantError) as raised:
        await _select(hass, BOUQUET_SELECT, "Sport")

    assert "bouquet has no playable service" in str(raised.value)


async def test_a_bouquet_option_that_vanished_is_refused_rather_than_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The list can be republished while somebody is choosing from it."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    entity = _entity(hass, BOUQUET_SELECT)
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("A bouquet that is no longer there")
    assert not any(
        call.args[0] == command_topic("bouquet")
        for call in mqtt_mock.async_publish.call_args_list
    )


# -------------------------------------------------------------------------- „Kanał"


async def test_the_channel_select_offers_the_active_bouquet_only(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Not the whole list: a select with a thousand rows is not a control."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    state = hass.states.get(CHANNEL_SELECT)
    assert state.attributes[ATTR_OPTIONS] == ["TVP 1 HD", "TVN HD"]
    assert state.state == "TVP 1 HD"


async def test_the_channel_select_reshapes_when_the_context_moves(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Switching „Bukiet" has to change „Kanał" immediately, on the topic alone."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, BOUQUET_TOPIC, json.dumps(SPORT_CONTEXT))
    await hass.async_block_till_done()

    state = hass.states.get(CHANNEL_SELECT)
    assert state.attributes[ATTR_OPTIONS] == ["Eurosport 1", "TVN HD"]
    # „TVP 1 HD" is still playing and is not in this bouquet.
    assert state.state == STATE_UNKNOWN


async def test_the_channel_select_reshapes_when_the_channel_list_changes(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A bouquet edited on the box republishes `channels`, and the list follows."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    edited = json.loads(json.dumps(CHANNELS))
    edited["bouquets"][0]["channels"].append(
        {"sref": "1:0:19:9999:3F3:1:C00000:0:0:0:", "name": "TVP 2 HD"}
    )
    async_fire_mqtt_message(hass, CHANNELS_TOPIC, json.dumps(edited))
    await hass.async_block_till_done()

    assert hass.states.get(CHANNEL_SELECT).attributes[ATTR_OPTIONS] == [
        "TVP 1 HD",
        "TVN HD",
        "TVP 2 HD",
    ]


async def test_duplicate_names_inside_one_bouquet_are_numbered(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Dropping the repeat would make that channel unreachable from Home Assistant."""
    _with_bouquet_context(box_on_the_broker)
    doubled = json.loads(json.dumps(CHANNELS))
    doubled["bouquets"][0]["channels"].append(
        {"sref": "1:0:19:4242:3F3:1:C00000:0:0:0:", "name": "TVP 1 HD"}
    )
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(doubled)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    assert hass.states.get(CHANNEL_SELECT).attributes[ATTR_OPTIONS] == [
        "TVP 1 HD",
        "TVN HD",
        "TVP 1 HD (2)",
    ]

    await async_arm_box_reply(
        hass,
        "zap",
        SERVICE_TOPIC,
        json.dumps({**SERVICE, "sref": "1:0:19:4242:3F3:1:C00000:0:0:0:"}),
    )
    await _select(hass, CHANNEL_SELECT, "TVP 1 HD (2)")

    assert_published(mqtt_mock, command_topic("zap"), "1:0:19:4242:3F3:1:C00000:0:0:0:")


async def test_selecting_a_channel_zaps_by_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Never by name: name resolution is the plugin's ambiguity problem, not ours."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass, "zap", SERVICE_TOPIC, json.dumps({**SERVICE, "sref": SREF_TWO})
    )

    await _select(hass, CHANNEL_SELECT, "TVN HD")

    assert_published(mqtt_mock, command_topic("zap"), SREF_TWO)
    assert hass.states.get(CHANNEL_SELECT).state == "TVN HD"


async def test_a_refused_channel_raises_with_the_boxs_own_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A zap the receiver will not carry out has to be visible where it was asked for."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)
    await async_arm_box_error(hass, "zap", "no tuner is free")

    with pytest.raises(HomeAssistantError) as raised:
        await _select(hass, CHANNEL_SELECT, "TVN HD")

    assert "no tuner is free" in str(raised.value)


async def test_a_channel_option_that_vanished_is_refused_rather_than_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A stale label must not be sent as if it were a service reference."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    entity = _entity(hass, CHANNEL_SELECT)
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("A channel that is no longer there")
    assert not any(
        call.args[0] == command_topic("zap")
        for call in mqtt_mock.async_publish.call_args_list
    )


async def test_a_channel_without_a_reference_is_left_off_the_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A marker line in a bouquet has a name and nothing to tune.

    The payload is the receiver's, not ours, so an entry that is not an object at all
    is dropped on the same rule rather than raising out of a state read.
    """
    _with_bouquet_context(box_on_the_broker)
    with_marker = json.loads(json.dumps(CHANNELS))
    with_marker["bouquets"][0]["channels"].insert(1, {"name": "--- Sport ---"})
    with_marker["bouquets"][0]["channels"].insert(2, "not an object at all")
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(with_marker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    assert hass.states.get(CHANNEL_SELECT).attributes[ATTR_OPTIONS] == [
        "TVP 1 HD",
        "TVN HD",
    ]


async def test_a_context_named_only_by_name_still_resolves(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The reference is what identifies a bouquet, but the name is enough to find it."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps({"name": "Sport", "sref": None})
    await async_setup_box(hass, config_entry)

    assert hass.states.get(BOUQUET_SELECT).state == "Sport"
    assert hass.states.get(CHANNEL_SELECT).attributes[ATTR_OPTIONS] == [
        "Eurosport 1",
        "TVN HD",
    ]


async def test_both_selects_are_unavailable_while_the_box_is(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A list of channels on a receiver that is not there is not worth offering."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    assert hass.states.get(BOUQUET_SELECT).state == STATE_UNAVAILABLE
    assert hass.states.get(CHANNEL_SELECT).state == STATE_UNAVAILABLE


async def test_the_channel_select_points_at_what_is_playing_by_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Two channels share a name across bouquets; only the reference tells them apart."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(SPORT_CONTEXT)
    # „TVN HD" is in both bouquets; this is the copy in „Sport".
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(
        {**SERVICE, "sref": "1:0:19:5678:3F3:1:C00000:0:0:0:", "name": "TVN HD"}
    )
    await async_setup_box(hass, config_entry)

    assert hass.states.get(CHANNEL_SELECT).state == "TVN HD"

    # The copy in the other bouquet is playing, so nothing in this list is.
    async_fire_mqtt_message(
        hass, SERVICE_TOPIC, json.dumps({**SERVICE, "sref": SREF, "name": "TVP 1 HD"})
    )
    await hass.async_block_till_done()
    assert hass.states.get(CHANNEL_SELECT).state == STATE_UNKNOWN


def _entity(hass: HomeAssistant, entity_id: str):
    """Return a select entity object, for the paths no service call can reach."""
    return hass.data[DATA_COMPONENT].get_entity(entity_id)

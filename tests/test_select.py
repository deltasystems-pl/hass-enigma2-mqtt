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

from custom_components.enigma2_mqtt.const import DOMAIN

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    AVAILABILITY_TOPIC,
    BOUQUET,
    BOUQUET_TOPIC,
    CHANNELS,
    CHANNELS_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    assert_published,
    async_arm_box_error,
    async_arm_box_reply,
    async_setup_box,
    async_setup_box_then_retained,
    command_topic,
)

BOUQUET_SELECT = "select.dekoder_salon_bouquet"
CHANNEL_SELECT = "select.dekoder_salon_channel"

ULUBIONE = CHANNELS["bouquets"][0]
SPORT = CHANNELS["bouquets"][1]
SPORT_CONTEXT: dict[str, Any] = {"name": SPORT["name"], "sref": SPORT["sref"]}
# The receiver on a list the plugin was not told to publish. Both fields null is
# ordinary operation, not a fault: the radio list and the movie list look like this.
NO_CONTEXT: dict[str, Any] = {"name": None, "sref": None}


def _with_bouquet_context(store: dict[str, str | bytes]) -> None:
    """Make the example box one that can activate a channel-list context.

    Both topics, because the plugin publishes the same capability list on both and a
    fixture where they disagree would be testing a receiver that does not exist — and
    would hide, or invent, churn in the gate that reads them.
    """
    capabilities = [*INFO["capabilities"], "bouquet_context"]
    store[INFO_TOPIC] = json.dumps({**INFO, "capabilities": capabilities})
    store[ANNOUNCEMENT_TOPIC] = json.dumps(
        {**ANNOUNCEMENT, "capabilities": capabilities}
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


async def test_a_capable_box_creates_them_once_in_the_order_a_broker_delivers(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The retained burst lands after setup on a real broker, not inside it.

    A gate that judged the capability from whichever payload happened to arrive first
    would create both selects and delete them again a moment later: registry churn, a
    transient entity in the recorder, and — on a restart — the loss of whatever name,
    area or dashboard place the household had given them. The end state cannot tell
    "created once" from "created, deleted and created again", so the registry is
    watched.

    The `update` events that do appear are the registry catching up with the `options`
    list, which every select produces when its list arrives. They are not churn, and
    asserting them away would only make this test fragile.
    """
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    config_entry.add_to_hass(hass)

    touched: list[tuple[str, str]] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: touched.append((event.data["entity_id"], event.data["action"])),
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(BOUQUET_SELECT) is not None
    assert hass.states.get(CHANNEL_SELECT) is not None
    ours = [entry for entry in touched if entry[0] in (BOUQUET_SELECT, CHANNEL_SELECT)]
    assert [entity_id for entity_id, action in ours if action == "create"] == [
        BOUQUET_SELECT,
        CHANNEL_SELECT,
    ]
    assert not [entry for entry in ours if entry[1] == "remove"]
    assert {action for _entity_id, action in ours} <= {"create", "update"}


async def test_a_start_up_the_box_slept_through_keeps_the_selects(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """"Not yet known" is not a stated „no", and a box in deep standby says nothing.

    The dangerous case is a restart, not a first run: the registry already holds these
    two, with whatever name, area and dashboard place the household gave them. A gate
    that reads silence as "the capability is gone" deletes all of that before the
    receiver has had a chance to answer — and a first-run test cannot catch it, because
    there is nothing registered for a wrong answer to destroy.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = {
        key: registry.async_get_or_create(
            "select",
            DOMAIN,
            f"{NODE_ID}_{key}",
            config_entry=config_entry,
            suggested_object_id=f"dekoder_salon_{key}",
        ).id
        for key in ("bouquet", "channel")
    }

    touched: list[tuple[str, str]] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: touched.append((event.data["entity_id"], event.data["action"])),
    )

    await async_setup_box_then_retained(hass, config_entry, retained)

    assert registry.async_get(BOUQUET_SELECT).id == existing["bouquet"]
    assert registry.async_get(CHANNEL_SELECT).id == existing["channel"]
    assert not [entry for entry in touched if entry[1] == "remove"]


async def test_a_payload_that_states_no_capabilities_keeps_them_too(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An `info` with no capability list said nothing about capabilities.

    It is not the same answer as a list that does not contain `bouquet_context`, and
    treating "a payload arrived" as "the box has answered" reads the first as the
    second — which deletes two entities on any receiver whose plugin is old enough, or
    whose first `info` was published before it had read its own configuration.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {key: value for key, value in INFO.items() if key != "capabilities"}
    )
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in ("bouquet", "channel"):
        registry.async_get_or_create(
            "select",
            DOMAIN,
            f"{NODE_ID}_{key}",
            config_entry=config_entry,
            suggested_object_id=f"dekoder_salon_{key}",
        )

    await async_setup_box_then_retained(hass, config_entry, retained)

    assert registry.async_get(BOUQUET_SELECT) is not None
    assert registry.async_get(CHANNEL_SELECT) is not None


async def test_a_stated_capability_list_without_it_does_remove_them(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The other half: a list that was stated and does not name it is a „no"."""
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(INFO)
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in ("bouquet", "channel"):
        registry.async_get_or_create(
            "select",
            DOMAIN,
            f"{NODE_ID}_{key}",
            config_entry=config_entry,
            suggested_object_id=f"dekoder_salon_{key}",
        )

    await async_setup_box_then_retained(hass, config_entry, retained)

    assert registry.async_get(BOUQUET_SELECT) is None
    assert registry.async_get(CHANNEL_SELECT) is None


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


async def test_a_bouquet_option_that_vanished_says_the_list_moved(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The list can be republished while somebody is choosing from it.

    The message has to be the one about the list having moved — "the receiver does not
    offer that bouquet" is what the action says about a bouquet that was never there,
    and it sends the reader looking at the receiver's configuration instead of at the
    list in front of them.
    """
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    entity = _entity(hass, BOUQUET_SELECT)
    with pytest.raises(ServiceValidationError) as raised:
        await entity.async_select_option("A bouquet that is no longer there")
    assert raised.value.translation_key == "option_no_longer_offered"
    assert not any(
        call.args[0] == command_topic("bouquet")
        for call in mqtt_mock.async_publish.call_args_list
    )


async def test_the_active_bouquet_is_matched_by_identity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """One bouquet has more than one spelling, and a raw `==` calls them two."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(
        {"name": None, "sref": SPORT["sref"].lower()}
    )
    await async_setup_box(hass, config_entry)

    assert hass.states.get(BOUQUET_SELECT).state == "Sport"
    assert hass.states.get(CHANNEL_SELECT).attributes[ATTR_OPTIONS] == [
        "Eurosport 1",
        "TVN HD",
    ]


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


async def test_a_numbered_duplicate_keeps_its_channel_when_the_bouquet_is_reordered(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Otherwise moving one of them in the receiver's bouquet editor swaps them.

    A label that silently starts tuning a different channel is worse than a label that
    is missing: an automation naming it keeps working and does something else. The
    number follows the service reference, so the list order can change underneath it.
    """
    _with_bouquet_context(box_on_the_broker)
    # The repeat is listed second here and first after the reorder below.
    doubled = json.loads(json.dumps(CHANNELS))
    doubled["bouquets"][0]["channels"] = [
        {"sref": SREF, "name": "TVP 1 HD"},
        {"sref": "1:0:19:4242:3F3:1:C00000:0:0:0:", "name": "TVP 1 HD"},
        {"sref": SREF_TWO, "name": "TVN HD"},
    ]
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(doubled)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    before = _labels_by_sref(hass)
    assert sorted(before) == ["TVP 1 HD", "TVP 1 HD (2)"]

    reordered = json.loads(json.dumps(doubled))
    reordered["bouquets"][0]["channels"] = [
        reordered["bouquets"][0]["channels"][1],
        reordered["bouquets"][0]["channels"][0],
        reordered["bouquets"][0]["channels"][2],
    ]
    async_fire_mqtt_message(hass, CHANNELS_TOPIC, json.dumps(reordered))
    await hass.async_block_till_done()

    # The list follows the receiver's new order …
    assert hass.states.get(CHANNEL_SELECT).attributes[ATTR_OPTIONS] == [
        "TVP 1 HD (2)",
        "TVP 1 HD",
        "TVN HD",
    ]
    # … and every label still tunes exactly the channel it tuned before.
    assert _labels_by_sref(hass) == before


def _labels_by_sref(hass: HomeAssistant) -> dict[str, str]:
    """Return the „Kanał" select's label for each duplicated reference."""
    srefs = _entity(hass, CHANNEL_SELECT)._srefs
    return {label: sref for label, sref in srefs.items() if label.startswith("TVP 1 HD")}


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


async def test_a_channel_option_that_vanished_says_the_list_moved(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A stale label must not be sent as if it were a service reference.

    And the complaint is that the list moved, not that the receiver has no such
    channel: it may have it, in the bouquet this list is no longer showing.
    """
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)

    entity = _entity(hass, CHANNEL_SELECT)
    with pytest.raises(ServiceValidationError) as raised:
        await entity.async_select_option("A channel that is no longer there")
    assert raised.value.translation_key == "option_no_longer_offered"
    assert not any(
        call.args[0] == command_topic("zap")
        for call in mqtt_mock.async_publish.call_args_list
    )


async def test_the_playing_channel_is_matched_by_identity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The `service` topic need not spell a reference the way `channels` does.

    A receiver that answers with a name after the tenth colon, or in another case, is
    tuned to exactly the channel on this list — and a raw comparison would leave the
    select showing nothing for as long as it stayed there.
    """
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(
        {**SERVICE, "sref": SREF_TWO.lower().rstrip(":")}
    )
    await async_setup_box(hass, config_entry)

    assert hass.states.get(CHANNEL_SELECT).state == "TVN HD"


async def test_a_zap_is_proved_by_identity_not_by_spelling(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Otherwise a zap the receiver plainly carried out is reported as a timeout."""
    _with_bouquet_context(box_on_the_broker)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass,
        "zap",
        SERVICE_TOPIC,
        # The receiver's own spelling: lower case, and stopping at the tenth colon.
        json.dumps({**SERVICE, "sref": SREF_TWO.lower().rstrip(":"), "name": "TVN HD"}),
    )

    await _select(hass, CHANNEL_SELECT, "TVN HD")

    assert_published(mqtt_mock, command_topic("zap"), SREF_TWO)


async def test_an_iptv_channels_stream_name_does_not_make_it_a_second_channel(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An IPTV reference always carries its URL and its name; the name is not identity.

    This is the shape the identifying-field count exists for: the channel list holds
    the reference up to the stream URL and the receiver answers with the name after it.
    """
    _with_bouquet_context(box_on_the_broker)
    iptv_sref = "4097:0:1:0:0:0:0:0:0:0:http%3a//192.0.2.30/stream.m3u8"
    with_iptv = json.loads(json.dumps(CHANNELS))
    with_iptv["bouquets"][0]["channels"].append(
        {"sref": iptv_sref, "name": "Kanał IPTV"}
    )
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(with_iptv)
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(
        {**SERVICE, "sref": f"{iptv_sref}:Kanał IPTV", "name": "Kanał IPTV"}
    )
    await async_setup_box(hass, config_entry)

    assert hass.states.get(CHANNEL_SELECT).state == "Kanał IPTV"


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

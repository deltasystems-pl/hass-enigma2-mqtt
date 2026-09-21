"""The runtime object: one subscription, and what it does and does not act on."""

from __future__ import annotations

import asyncio
import gc
import json

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.box import normalise_mac, picon_url
from custom_components.enigma2_mqtt.const import (
    TOPIC_EPG_GRID,
    TOPIC_SCREEN,
    TOPIC_SERVICE,
)

from .conftest import (
    EPG_GRID,
    EPG_GRID_TOPIC,
    IP,
    PICON_URL,
    SCREEN,
    SCREEN_TOPIC,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    async_arm_box_error,
    async_setup_box,
    command_topic,
)


async def test_one_subscription_collects_every_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A wildcard is one SUBSCRIBE and one retained burst instead of fourteen."""
    await async_setup_box(hass, config_entry)
    state = config_entry.runtime_data.state

    assert state.power == "on"
    assert state.service == SERVICE
    assert state.volume == {"level": 35, "muted": False}
    assert state.timers[0]["state"] == "waiting"
    # The JPEG comes through as bytes, on the same subscription as the JSON.
    assert state.screen == SCREEN


async def test_our_own_commands_are_not_state(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A broker echoes what we publish back to a matching subscription."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data
    before = dict(box.updates)

    async_fire_mqtt_message(hass, command_topic("zap"), SREF)
    async_fire_mqtt_message(hass, command_topic("power"), "standby")
    await hass.async_block_till_done()

    assert box.updates == before
    assert box.state.power == "on"


async def test_an_unknown_topic_is_ignored(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A later plugin adding a topic must not break an older integration."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data

    async_fire_mqtt_message(
        hass, "enigma2/vuuno4kse_005301/something_new", json.dumps({"a": 1})
    )
    await hass.async_block_till_done()

    assert "something_new" not in box.seen


async def test_a_retracted_epg_grid_is_forgotten(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Dropping a bouquet retracts its topic, and the cache has to follow."""
    box_on_the_broker[EPG_GRID_TOPIC] = json.dumps(EPG_GRID)
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data

    assert "ulubione_tv" in box.state.epg_grid

    async_fire_mqtt_message(hass, EPG_GRID_TOPIC, "")
    await hass.async_block_till_done()

    assert box.state.epg_grid == {}


async def test_a_listener_only_hears_the_topics_it_asked_for(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Twenty-six entities woken by every message would be twenty-six times the work."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data

    heard: list[None] = []
    remove = box.async_add_listener(lambda: heard.append(None), (TOPIC_SERVICE,))

    async_fire_mqtt_message(hass, SCREEN_TOPIC, b"\xff\xd8x\xff\xd9")
    await hass.async_block_till_done()
    assert heard == []

    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(SERVICE))
    await hass.async_block_till_done()
    assert len(heard) == 1

    remove()


async def test_a_listener_always_hears_availability(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A box that goes offline publishes nothing else to say so."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data

    heard: list[None] = []
    remove = box.async_add_listener(lambda: heard.append(None), (TOPIC_SCREEN,))

    async_fire_mqtt_message(hass, "enigma2/vuuno4kse_005301/availability", "offline")
    await hass.async_block_till_done()

    assert len(heard) == 1
    remove()


async def test_a_command_whose_effect_already_happened_does_not_wait(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A late refusal cannot leave an unhandled future behind."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data
    await async_arm_box_error(hass, "zap", "already tuned")
    unhandled: list[dict[str, object]] = []
    previous_handler = hass.loop.get_exception_handler()
    hass.loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    try:
        await box.async_command("zap", SREF, effect=lambda: True, timeout=0.05)
        await hass.async_block_till_done()
        gc.collect()
        await asyncio.sleep(0)
    finally:
        hass.loop.set_exception_handler(previous_handler)

    assert box.updates.get(TOPIC_EPG_GRID) is None
    assert box._pending == []
    assert not [
        context
        for context in unhandled
        if context.get("message") == "Future exception was never retrieved"
    ]


async def test_cancelling_a_command_retires_its_pending_future(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Cancelling an automation cannot leave a command waiter attached to the box."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data

    command = hass.async_create_task(
        box.async_command("zap", "nowhere", effect=lambda: False, timeout=60)
    )
    await asyncio.sleep(0)
    assert len(box._pending) == 1

    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command

    assert box._pending == []


@pytest.mark.parametrize(
    ("address", "sref", "expected"),
    [
        (IP, SREF, PICON_URL),
        (None, SREF, None),
        (IP, None, None),
        (IP, "", None),
    ],
)
def test_the_picon_url_is_guarded(
    address: str | None, sref: str | None, expected: str | None
) -> None:
    """No address means no picon, rather than a URL that cannot resolve."""
    assert picon_url(address, sref) == expected


@pytest.mark.parametrize(
    "spelling",
    [
        "00:00:5e:00:53:01",
        "00:00:5E:00:53:01",
        "00-00-5e-00-53-01",
        "0000.5e00.5301",
        "00005e005301",
        "  00005E005301  ",
    ],
)
def test_every_spelling_of_an_address_normalises_to_one(spelling: str) -> None:
    """The four forms a MAC is written in, and the one form it is stored in."""
    assert normalise_mac(spelling) == "00:00:5e:00:53:01"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        # The one that was actually stored: an address with a note after it.
        "00:00:5e:00:53:01 (kabel)",
        "00:00:5e:00:53:01x",
        # Too short, too long, not hexadecimal, and separators that do not agree.
        "00:00:5e:00:53",
        "00:00:5e:00:53:01:02",
        "zz:00:5e:00:53:01",
        "00:00-5e:00.53:01",
        "0000.5e00.53.01",
        192,
    ],
)
def test_anything_that_is_not_an_address_is_refused(value: object) -> None:
    """A near miss is still not an address, and guessing at one wakes the wrong box."""
    assert normalise_mac(value) is None

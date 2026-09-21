"""The actions: what they send, what they wait for, and how they fail.

Every one of them is an entity action on the media player, so a test that targets the
device rather than the entity is testing that Home Assistant resolves the device to the
box — which is the whole reason the actions were registered there.
"""

from __future__ import annotations

import asyncio
import json

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.box import Enigma2CommandError
from custom_components.enigma2_mqtt.const import DOMAIN, TOPIC_SCREEN

from .conftest import (
    BOUQUET,
    BOUQUET_TOPIC,
    EPG_GRID,
    EPG_GRID_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    RECORDING_ACTIVE,
    RECORDING_TOPIC,
    SCREEN_TOPIC,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    TIMERS,
    TIMERS_TOPIC,
    assert_published,
    async_arm_box_ack,
    async_arm_box_error,
    async_arm_box_reply,
    async_arm_ha_mode_ack,
    async_setup_box,
    command_topic,
    published_payloads,
)

PLAYER = "media_player.dekoder_salon"


async def _call(hass: HomeAssistant, service: str, **data):
    """Call one of the integration's actions on the example box."""
    return await hass.services.async_call(
        DOMAIN, service, {"entity_id": PLAYER, **data}, blocking=True
    )


async def test_zapping_by_reference_waits_for_the_service_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """There is no acknowledgement in the contract; the state topic is the answer."""
    await async_setup_box(hass, config_entry)
    other = "1:0:19:1234:3F3:1:C00000:0:0:0:"
    await async_arm_box_reply(
        hass, "zap", SERVICE_TOPIC, json.dumps({**SERVICE, "sref": other})
    )

    await _call(hass, "zap", sref=other)

    assert_published(mqtt_mock, command_topic("zap"), other)


async def test_the_zap_action_accepts_the_receivers_own_spelling(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The answer on `service` is the receiver's spelling, not the one that was sent.

    A reference can stop at the tenth colon or carry one, and its hexadecimal fields
    are not written in an agreed case, so comparing the two as strings reports a zap
    that plainly happened as a timeout — ten seconds after it happened.
    """
    await async_setup_box(hass, config_entry)
    other = "1:0:19:1234:3F3:1:C00000:0:0:0:"
    await async_arm_box_reply(
        hass,
        "zap",
        SERVICE_TOPIC,
        json.dumps({**SERVICE, "sref": other.lower().rstrip(":")}),
    )

    await _call(hass, "zap", sref=other)

    assert_published(mqtt_mock, command_topic("zap"), other)


async def test_the_select_bouquet_action_accepts_the_receivers_own_spelling(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Same for the context readback, for the same reason."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "capabilities": [*INFO["capabilities"], "bouquet_context"]}
    )
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass,
        "bouquet",
        BOUQUET_TOPIC,
        json.dumps({**BOUQUET, "sref": BOUQUET["sref"].lower()}),
    )

    await _call(hass, "select_bouquet", sref=BOUQUET["sref"])

    assert_published(
        mqtt_mock, command_topic("bouquet"), json.dumps({"sref": BOUQUET["sref"]})
    )


async def test_select_bouquet_action_waits_for_fresh_context(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "capabilities": [*INFO["capabilities"], "bouquet_context"]}
    )
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(BOUQUET)
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(hass, "bouquet", BOUQUET_TOPIC, json.dumps(BOUQUET))

    await _call(hass, "select_bouquet", sref=BOUQUET["sref"])

    assert_published(
        mqtt_mock,
        command_topic("bouquet"),
        json.dumps({"sref": BOUQUET["sref"]}),
    )


async def test_selecting_a_bouquet_the_box_never_published_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Nothing is sent to the receiver, and the complaint names the bouquet."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "capabilities": [*INFO["capabilities"], "bouquet_context"]}
    )
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "select_bouquet", sref="1:7:1:0:0:0:0:0:0:0:FROM BOGUS")

    assert raised.value.translation_key == "bouquet_not_published"
    assert published_payloads(mqtt_mock, command_topic("bouquet")) == []


async def test_zapping_by_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A name goes out as a JSON object, because a bare string is a reference."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass, "zap", SERVICE_TOPIC, json.dumps({**SERVICE, "name": "TVN HD"})
    )

    await _call(hass, "zap", name="TVN HD")

    assert_published(
        mqtt_mock, command_topic("zap"), json.dumps({"name": "TVN HD"})
    )


async def test_zapping_to_the_channel_already_on_returns_at_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Nothing will change, so waiting for a change would wait out the timeout."""
    await async_setup_box(hass, config_entry)

    await _call(hass, "zap", sref=SREF)

    assert_published(mqtt_mock, command_topic("zap"), SREF)


@pytest.mark.parametrize(
    "arguments", [{}, {"sref": SREF, "name": "TVN HD"}], ids=["neither", "both"]
)
async def test_zapping_needs_exactly_one_of_the_two(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    arguments: dict,
) -> None:
    """A reference and a name can disagree, and guessing between them is worse."""
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError):
        await _call(hass, "zap", **arguments)


async def test_a_refused_command_raises_with_the_box_s_own_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`last_error` is the disproof, and it says something worth repeating."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_error(
        hass, "zap", "service name 'Sport' is not unique (4 matches)"
    )

    with pytest.raises(HomeAssistantError) as raised:
        await _call(hass, "zap", name="TVN HD")

    assert "not unique" in str(raised.value)


async def test_a_stale_last_error_does_not_fail_a_new_command(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A retained complaint is what the broker had before we asked, not an answer."""
    retained["enigma2/vuuno4kse_005301/availability"] = "online"
    retained["enigma2/vuuno4kse_005301/last_error"] = json.dumps(
        {"cmd": "zap", "error": "an old failure", "ts": 1}
    )
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass, "zap", SERVICE_TOPIC, json.dumps({**SERVICE, "sref": SREF})
    )

    await _call(hass, "zap", sref=SREF)


async def test_a_command_that_never_lands_times_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A box that says nothing at all is a failure, not a success.

    The timeout is exercised directly on the runtime object rather than through an
    action, so that the test costs a twentieth of a second instead of the ten seconds a
    real caller is given.
    """
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data

    with pytest.raises(Enigma2CommandError):
        await box.async_command(
            "zap", "nowhere", effect=lambda: False, timeout=0.05
        )

    assert box._pending == []


async def test_sending_a_key_through_the_action(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A key changes no state topic, so silence through the grace period is success."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_ack(hass, "key")

    await _call(hass, "send_key", key="green", long=True)

    assert_published(
        mqtt_mock, command_topic("key"), json.dumps({"key": "KEY_GREEN", "long": True})
    )


async def test_an_unknown_key_is_refused_by_the_box(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The plugin knows which keys the box has; this side does not pretend to."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_error(hass, "key", "unknown key name KEY_INVENTED")

    with pytest.raises(HomeAssistantError):
        await _call(hass, "send_key", key="invented")


async def test_error_clear_cannot_acknowledge_concurrent_commands(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An anonymous clear cannot hide a later error for one of two commands."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data
    key = hass.async_create_task(box.async_command("key", "KEY_GREEN"))
    message = hass.async_create_task(box.async_command("message", "hello"))
    await asyncio.sleep(0)

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await asyncio.sleep(0)
    assert not key.done()
    assert not message.done()

    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "key", "error": "unknown key"}),
    )
    with pytest.raises(Enigma2CommandError, match="unknown key"):
        await key
    await message


async def test_the_message_action_carries_type_and_timeout(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Unlike the notify entity, the action can choose the icon and how long it stays."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_ack(hass, "message")

    await _call(hass, "message", text="Uwaga", type="warning", timeout=30)

    assert_published(
        mqtt_mock,
        command_topic("message"),
        json.dumps({"text": "Uwaga", "type": "warning", "timeout": 30}),
    )


async def test_adding_a_timer_from_an_epg_event(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The event form is the one to prefer, and the timer list is the proof."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass, "timer", TIMERS_TOPIC, json.dumps([*TIMERS, TIMERS[0]])
    )

    await _call(hass, "add_timer", sref=SREF, event_id=27431)

    assert_published(
        mqtt_mock,
        command_topic("timer"),
        json.dumps({"action": "add", "sref": SREF, "event_id": 27431}),
    )


async def test_adding_a_timer_from_a_window_converts_the_times(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Every time in the contract is epoch seconds; the action takes a datetime."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(hass, "timer", TIMERS_TOPIC, json.dumps(TIMERS))

    await _call(
        hass,
        "add_timer",
        sref=SREF,
        begin="2026-09-15T08:00:00+00:00",
        end="2026-09-15T08:25:00+00:00",
        name="Wiadomości",
    )

    payload = json.loads(published_payloads(mqtt_mock, command_topic("timer"))[0])
    assert payload == {
        "action": "add",
        "sref": SREF,
        "begin": 1789459200,
        "end": 1789460700,
        "name": "Wiadomości",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"sref": SREF},
        {"sref": SREF, "begin": "2026-09-15T08:00:00+00:00"},
        {"sref": SREF, "event_id": 27431, "begin": "2026-09-15T08:00:00+00:00"},
    ],
    ids=["nothing", "half-a-window", "both-forms"],
)
async def test_adding_a_timer_takes_one_form_or_the_other(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    arguments: dict,
) -> None:
    """Half a window is not a timer, and two forms at once is a contradiction."""
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError):
        await _call(hass, "add_timer", **arguments)


async def test_deleting_a_timer_sends_the_identifying_triple(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """enigma2 has no timer id; the channel and the two times are the identity."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(hass, "timer", TIMERS_TOPIC, "[]")

    await _call(
        hass,
        "delete_timer",
        sref=SREF,
        begin=1789459200,
        end=1789460700,
    )

    assert_published(
        mqtt_mock,
        command_topic("timer"),
        json.dumps(
            {
                "action": "delete",
                "sref": SREF,
                "begin": 1789459200,
                "end": 1789460700,
            }
        ),
    )


async def test_starting_a_recording_waits_for_it_to_be_running(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`record start` is proved by the recording topic, not by the publish."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass, "record", RECORDING_TOPIC, json.dumps(RECORDING_ACTIVE)
    )

    await _call(hass, "record", action="start")

    assert_published(mqtt_mock, command_topic("record"), "start")


async def test_taking_a_screenshot_waits_for_the_picture(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The point of the action is the new frame, so that is what it waits for."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass, "screenshot", SCREEN_TOPIC, b"\xff\xd8fresh\xff\xd9"
    )

    await _call(hass, "screenshot")

    assert_published(mqtt_mock, command_topic("screenshot"), "PRESS")


async def test_clearing_an_old_error_does_not_acknowledge_a_screenshot(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An effect-bearing command waits for its effect, not an error-topic clear."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data
    before = box.updates.get(TOPIC_SCREEN, 0)
    command = hass.async_create_task(
        box.async_command(
            "screenshot",
            "PRESS",
            effect=lambda: box.updates.get(TOPIC_SCREEN, 0) > before,
        )
    )
    await asyncio.sleep(0)

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await asyncio.sleep(0)
    assert not command.done()

    async_fire_mqtt_message(hass, SCREEN_TOPIC, b"\xff\xd8fresh\xff\xd9")
    await command


async def test_screenshot_error_after_a_clear_still_fails_the_command(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A grab failure after startup's error clear is still the command's result."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data
    before = box.updates.get(TOPIC_SCREEN, 0)
    command = hass.async_create_task(
        box.async_command(
            "screenshot",
            "PRESS",
            effect=lambda: box.updates.get(TOPIC_SCREEN, 0) > before,
        )
    )
    await asyncio.sleep(0)

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    await asyncio.sleep(0)
    assert not command.done()

    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "screenshot", "error": "grab failed"}),
    )
    with pytest.raises(Enigma2CommandError, match="grab failed"):
        await command


async def test_setting_the_ha_mode_waits_for_the_echo_on_info(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """This is the one command the contract does acknowledge, on `info`."""
    await async_setup_box(hass, config_entry)
    await async_arm_ha_mode_ack(hass)

    await _call(hass, "set_ha_mode", mode="off")

    assert config_entry.runtime_data.info["ha_mode"] == "off"


async def test_a_device_target_reaches_the_same_box(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Targeting the device is how a person writes this in the editor."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_ack(hass, "key")
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_ID), config_entry.entry_id
    )

    await hass.services.async_call(
        DOMAIN, "send_key", {"device_id": device.id, "key": "KEY_OK"}, blocking=True
    )

    assert_published(
        mqtt_mock, command_topic("key"), json.dumps({"key": "KEY_OK", "long": False})
    )


async def test_reading_the_epg_grid(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The grid is returned, never stored on an entity: it is far too big for that."""
    box_on_the_broker[EPG_GRID_TOPIC] = json.dumps(EPG_GRID)
    await async_setup_box(hass, config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        "get_epg_grid",
        {"entity_id": PLAYER},
        blocking=True,
        return_response=True,
    )

    # An entity action's response is keyed by the entity it came from.
    grids = response[PLAYER]["bouquets"]
    assert [grid["bouquet"] for grid in grids] == ["Ulubione TV"]
    assert grids[0]["slug"] == "ulubione_tv"
    assert grids[0]["channels"][0]["name"] == "TVP 1 HD"


async def test_reading_one_bouquet_of_the_epg_grid(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A household that wants one bouquet should not be handed every bouquet."""
    box_on_the_broker[EPG_GRID_TOPIC] = json.dumps(EPG_GRID)
    box_on_the_broker["enigma2/vuuno4kse_005301/epg_grid/sport"] = json.dumps(
        {**EPG_GRID, "bouquet": "Sport", "channels": []}
    )
    await async_setup_box(hass, config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        "get_epg_grid",
        {"entity_id": PLAYER, "bouquet": "Sport"},
        blocking=True,
        return_response=True,
    )

    assert [grid["bouquet"] for grid in response[PLAYER]["bouquets"]] == ["Sport"]


async def test_reading_a_bouquet_the_box_never_published(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An empty answer would read as „nothing is on"; this says what really happened."""
    box_on_the_broker[EPG_GRID_TOPIC] = json.dumps(EPG_GRID)
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "get_epg_grid",
            {"entity_id": PLAYER, "bouquet": "Filmy"},
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == "unknown_bouquet"


async def test_an_empty_epg_grid_is_rebuilt_on_demand(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A broker with no grid on it is worth one request before giving up."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_reply(
        hass, "epg_grid", EPG_GRID_TOPIC, json.dumps(EPG_GRID)
    )

    response = await hass.services.async_call(
        DOMAIN,
        "get_epg_grid",
        {"entity_id": PLAYER},
        blocking=True,
        return_response=True,
    )

    assert_published(mqtt_mock, command_topic("epg_grid"), "PRESS")
    assert response[PLAYER]["bouquets"][0]["bouquet"] == "Ulubione TV"

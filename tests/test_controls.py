"""The binary sensors, switches, the number and the buttons.

Four small platforms in one file, because each of them is one topic in and one command
out and a file apiece would be four copies of the same three fixtures.
"""

from __future__ import annotations

import asyncio
import json

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_mock_service,
)

from custom_components.enigma2_mqtt.const import (
    CONF_DANGEROUS_BUTTONS,
    CONF_WOL_MAC,
    DOMAIN,
)

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    AVAILABILITY_TOPIC,
    HDD_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    MAC,
    NODE_ID,
    POWER_TOPIC,
    RECORDING_ACTIVE,
    RECORDING_TOPIC,
    SCREEN,
    SCREEN_TOPIC,
    VOLUME_TOPIC,
    assert_published,
    async_arm_box_error,
    async_setup_box,
    async_setup_box_then_retained,
    command_topic,
)


async def test_the_recording_binary_sensor(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """This is what an automation checks before it reboots anything."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("binary_sensor.dekoder_salon_recording").state == STATE_OFF

    async_fire_mqtt_message(hass, RECORDING_TOPIC, json.dumps(RECORDING_ACTIVE))
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.dekoder_salon_recording")
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "running"


async def test_the_recording_disk_binary_sensor(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A disk that unmounts itself is the point of this topic."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get("binary_sensor.dekoder_salon_recording_disk")
    assert state.state == STATE_ON
    assert state.attributes["path"] == "/media/hdd"
    assert state.attributes["free_mb"] == 412330

    async_fire_mqtt_message(
        hass,
        HDD_TOPIC,
        json.dumps({"mounted": False, "path": "/media/hdd", "free_mb": None}),
    )
    await hass.async_block_till_done()

    assert (
        hass.states.get("binary_sensor.dekoder_salon_recording_disk").state == STATE_OFF
    )


async def test_the_power_switch_follows_the_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Standby is a switch, and the state topic is what settles it."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("switch.dekoder_salon_power").state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.dekoder_salon_power"},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("power"), "standby")

    # Optimistic: the switch has already moved, before the box said anything.
    assert hass.states.get("switch.dekoder_salon_power").state == STATE_OFF

    async_fire_mqtt_message(hass, POWER_TOPIC, "on")
    await hass.async_block_till_done()
    assert hass.states.get("switch.dekoder_salon_power").state == STATE_ON


async def test_the_mute_switch(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Mute lives on the volume topic, which carries both halves."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get("switch.dekoder_salon_mute").state == STATE_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "switch.dekoder_salon_mute"},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("mute"), "ON")

    async_fire_mqtt_message(
        hass, VOLUME_TOPIC, json.dumps({"level": 35, "muted": True})
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.dekoder_salon_mute").state == STATE_ON


async def test_the_volume_number(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The number is the receiver's own 0-100 scale, not the media player's 0-1."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get("number.dekoder_salon_volume")
    assert state.state == "35.0"
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 100
    assert state.attributes["mode"] == "slider"

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: "number.dekoder_salon_volume", ATTR_VALUE: 20},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("volume"), "20")


async def _command_sent(mqtt_mock, topic: str) -> None:
    """Wait for a press to get its command out, but not for the press to finish.

    `async_block_till_done` would wait for the press itself, which is the whole ten
    seconds of a command nothing is going to answer.
    """
    for _ in range(200):
        await asyncio.sleep(0.005)
        if any(
            call.args[0] == topic for call in mqtt_mock.async_publish.call_args_list
        ):
            # And then a few more turns, so that a press which is going to return
            # without waiting for anything has finished by the time it is looked at.
            for _ in range(5):
                await asyncio.sleep(0)
            return
    raise AssertionError(f"{topic} was never published")


@pytest.mark.parametrize(
    ("entity_id", "command"),
    [
        ("button.dekoder_salon_restart_gui", "restart_gui"),
        ("button.dekoder_salon_refresh_epg", "epg_grid"),
    ],
)
async def test_a_button_with_no_effect_treats_silence_as_success(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    entity_id: str,
    command: str,
) -> None:
    """A GUI restart and an EPG rebuild move no topic the integration can watch.

    The box is about to stop answering, or the grid it would republish has not
    changed. The contract offers no positive acknowledgement for either, so the press
    holds the error-grace window open and reports success when nothing complains.
    """
    await async_setup_box(hass, config_entry)

    press = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    )
    await _command_sent(mqtt_mock, command_topic(command))

    # The old button published and returned here, which is how a refusal stayed
    # invisible: there was no longer anything listening when it arrived.
    assert not press.done()
    assert_published(mqtt_mock, command_topic(command), "PRESS")

    await press


async def test_the_screenshot_button_waits_for_the_picture(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A new `screen` payload is the proof, and it is what ends the wait."""
    await async_setup_box(hass, config_entry)

    press = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.dekoder_salon_screenshot"},
            blocking=True,
        )
    )
    await _command_sent(mqtt_mock, command_topic("screenshot"))
    assert not press.done()
    assert_published(mqtt_mock, command_topic("screenshot"), "PRESS")

    async_fire_mqtt_message(hass, SCREEN_TOPIC, SCREEN)
    await press


async def test_the_discovery_button_waits_for_the_announcement(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A republished announcement is usually identical, so the payload is not compared."""
    await async_setup_box(hass, config_entry)

    press = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.dekoder_salon_refresh_discovery"},
            blocking=True,
        )
    )
    await _command_sent(mqtt_mock, command_topic("discovery"))
    assert not press.done()
    assert_published(mqtt_mock, command_topic("discovery"), "PRESS")

    async_fire_mqtt_message(hass, ANNOUNCEMENT_TOPIC, json.dumps(ANNOUNCEMENT))
    await press


@pytest.mark.parametrize(
    ("entity_id", "command"),
    [
        ("button.dekoder_salon_restart_gui", "restart_gui"),
        ("button.dekoder_salon_refresh_epg", "epg_grid"),
        ("button.dekoder_salon_screenshot", "screenshot"),
    ],
)
async def test_a_refused_button_raises_the_boxs_own_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    entity_id: str,
    command: str,
) -> None:
    """This is the whole point of (a): a refusal used to look like a working press."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_error(hass, command, "a recording is running")

    with pytest.raises(HomeAssistantError, match="a recording is running"):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )


async def test_somebody_elses_complaint_does_not_fail_a_button(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`last_error` names the command it is about, and only that one waits on it.

    The complaint has to arrive *inside* the window the press is holding open, or
    this proves nothing: a receiver refusing an automation's screenshot while
    somebody restarts the GUI is one topic carrying two conversations.
    """
    await async_setup_box(hass, config_entry)

    press = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.dekoder_salon_restart_gui"},
            blocking=True,
        )
    )
    await _command_sent(mqtt_mock, command_topic("restart_gui"))
    assert not press.done()

    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps(
            {"cmd": "screenshot", "error": "one screenshot every five seconds", "ts": 1}
        ),
    )
    await hass.async_block_till_done()

    # Silence about `restart_gui` is still success, and the complaint that was about
    # something else is on the sensor rather than in an exception.
    await press
    assert hass.states.get("sensor.dekoder_salon_last_error").state == "screenshot"


async def test_the_epg_button_needs_the_capability(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """With `epg_grid_events` at zero there are no grids, and nothing to refresh."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "capabilities": [
                name for name in INFO["capabilities"] if name != "epg_grid"
            ],
        }
    )
    await async_setup_box(hass, config_entry)

    assert hass.states.get("button.dekoder_salon_refresh_epg") is None


async def test_the_epg_button_appears_when_the_capability_does(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A capability can arrive late, and a box that has said nothing is not a "no"."""
    retained[AVAILABILITY_TOPIC] = "online"
    await async_setup_box(hass, config_entry)
    assert hass.states.get("button.dekoder_salon_refresh_epg") is None

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    await hass.async_block_till_done()

    assert hass.states.get("button.dekoder_salon_refresh_epg") is not None


async def test_an_info_with_no_capability_list_keeps_the_epg_button(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A payload that arrived is not the same as a question that was answered.

    An `info` without a `capabilities` key has said nothing about capabilities - an
    older plugin, or one that published before it had read its own configuration. Read
    as "the box has spoken", it becomes a stated „no" and the button is deleted from the
    registry with whatever the household had done to it. The next `info` brings the
    button back under a new id, so the end state hides it; the registry does not.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {key: value for key, value in INFO.items() if key != "capabilities"}
    )
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{NODE_ID}_refresh_epg",
        config_entry=config_entry,
        suggested_object_id="dekoder_salon_refresh_epg",
    ).id

    await async_setup_box_then_retained(hass, config_entry, retained)

    surviving = registry.async_get("button.dekoder_salon_refresh_epg")
    assert surviving is not None
    assert surviving.id == existing


async def test_the_dangerous_buttons_are_absent_by_default(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Deep standby and reboot are not one mis-tap from the volume."""
    await async_setup_box(hass, config_entry)

    assert hass.states.get("button.dekoder_salon_deep_standby") is None
    assert hass.states.get("button.dekoder_salon_reboot") is None


async def test_the_option_brings_the_dangerous_buttons_back(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """And turning it off again takes them out of the registry, not just off."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_deep_standby"},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("deep_standby"), "PRESS")

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_reboot"},
        blocking=True,
    )
    assert_published(mqtt_mock, command_topic("reboot"), "PRESS")

    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: False}
    )
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("button.dekoder_salon_deep_standby") is None
    assert (
        er.async_get(hass).async_get("button.dekoder_salon_deep_standby") is None
    )


async def test_the_box_can_refuse_the_dangerous_buttons_too(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`deep_standby_allowed` is set at the television, and it is the second gate.

    A box with it off refuses both commands whatever Home Assistant thinks, so
    offering the buttons only produces the silent failure this release is about.
    """
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "settings": {"deep_standby_allowed": False}}
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )
    # A gate that read the permission before `info` arrived would create both buttons
    # and delete them again a moment later, on every single start-up: registry churn, a
    # transient entity in the recorder, and a race in which the deletion overtakes the
    # addition and the button is left registered for good. The end state cannot tell
    # "never created" from "created and then deleted", so the registry is watched.
    touched: list[str] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: touched.append(event.data["entity_id"]),
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get("button.dekoder_salon_deep_standby") is None
    assert hass.states.get("button.dekoder_salon_reboot") is None
    registry = er.async_get(hass)
    assert registry.async_get("button.dekoder_salon_deep_standby") is None
    assert registry.async_get("button.dekoder_salon_reboot") is None
    assert not [entity_id for entity_id in touched if "deep_standby" in entity_id]
    assert not [entity_id for entity_id in touched if "reboot" in entity_id]


async def test_a_permitted_button_keeps_its_entity_id_across_a_restart(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Created once, when the box says it may be, and the same one afterwards."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "settings": {"deep_standby_allowed": True}}
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    registry = er.async_get(hass)
    first = registry.async_get("button.dekoder_salon_deep_standby")
    assert first is not None

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    again = registry.async_get("button.dekoder_salon_deep_standby")
    assert again is not None
    assert again.id == first.id


async def test_a_start_up_the_box_slept_through_keeps_the_buttons(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """"Not yet known" is not a stated „no", and a box in deep standby says nothing.

    Which is the one state in which somebody is most likely to want the buttons that
    put it there to still be where they left them.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{NODE_ID}_deep_standby",
        config_entry=config_entry,
        suggested_object_id="dekoder_salon_deep_standby",
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(existing.entity_id) is not None


async def test_permission_at_the_television_brings_the_buttons_back(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Turning it on at the box must not need a Home Assistant restart."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "settings": {"deep_standby_allowed": False}}
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert hass.states.get("button.dekoder_salon_deep_standby") is None

    async_fire_mqtt_message(
        hass, INFO_TOPIC, json.dumps({**INFO, "settings": {"deep_standby_allowed": True}})
    )
    await hass.async_block_till_done()

    assert hass.states.get("button.dekoder_salon_deep_standby") is not None
    assert hass.states.get("button.dekoder_salon_reboot") is not None


async def test_an_older_plugin_leaves_the_dangerous_buttons_to_the_option(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Silence is not a refusal: a plugin without the key keeps today's behaviour."""
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "settings": {}})
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get("button.dekoder_salon_deep_standby") is not None
    assert hass.states.get("button.dekoder_salon_reboot") is not None


@pytest.mark.parametrize("stated", ["false", 1, None, []])
async def test_only_a_boolean_permission_is_an_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    stated: object,
) -> None:
    """The string „false" is not False, and 1 is not True.

    A receiver that answers in something other than a boolean has not answered, and
    reading a truthy value out of it would either delete working buttons or offer two
    the box is about to refuse.
    """
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "settings": {"deep_standby_allowed": stated}}
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert config_entry.runtime_data.deep_standby_permission is None
    # And so the Home Assistant option decides alone, as it did before the key existed.
    assert hass.states.get("button.dekoder_salon_deep_standby") is not None


async def test_an_unparseable_info_leaves_the_dangerous_buttons_alone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Nothing a box says badly may delete an entity somebody put on a dashboard."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_DANGEROUS_BUTTONS: True}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    async_fire_mqtt_message(hass, INFO_TOPIC, "not json at all")
    await hass.async_block_till_done()

    assert hass.states.get("button.dekoder_salon_deep_standby") is not None


async def test_the_wake_button_works_while_the_box_is_gone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Every other entity is unavailable by then, which is why this one is not."""
    await async_setup_box(hass, config_entry)
    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    assert (
        hass.states.get("button.dekoder_salon_screenshot").state == STATE_UNAVAILABLE
    )
    assert hass.states.get("button.dekoder_salon_wake").state != STATE_UNAVAILABLE

    magic_packets = async_mock_service(hass, "wake_on_lan", "send_magic_packet")
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_wake"},
        blocking=True,
    )

    assert magic_packets[0].data["mac"] == MAC


async def test_the_wol_option_overrides_the_reported_mac(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A box on Wi-Fi does not answer a packet sent to its cable port."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_WOL_MAC: "00:00:5e:00:53:02"}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    magic_packets = async_mock_service(hass, "wake_on_lan", "send_magic_packet")
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_wake"},
        blocking=True,
    )

    assert magic_packets[0].data["mac"] == "00:00:5e:00:53:02"


async def test_an_override_in_any_spelling_reaches_wake_on_lan_normalised(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An entry written by hand never passed through the form that validates."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_WOL_MAC: "00-00-5E-00-53-02"}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    magic_packets = async_mock_service(hass, "wake_on_lan", "send_magic_packet")
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_wake"},
        blocking=True,
    )

    assert magic_packets[0].data["mac"] == "00:00:5e:00:53:02"


async def test_an_override_written_after_setup_is_still_normalised(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Defence in depth, and the only thing that covers it.

    The set-up repair canonicalises what is already stored, so every other path
    reaches `wake_on_lan` through a value that has been through it. An entry edited
    in `.storage` while Home Assistant is running has not.
    """
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data

    hass.config_entries.async_update_entry(
        config_entry, options={CONF_WOL_MAC: "00-00-5E-00-53-02"}
    )
    assert box.mac_address == "00:00:5e:00:53:02"

    # And an override that is not an address is no address: the packet goes where the
    # receiver says it should rather than to something `bytes.fromhex` will reject.
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_WOL_MAC: "00:00:5e:00:53:01x"}
    )
    assert box.mac_address == MAC


async def test_a_stored_override_that_is_not_an_address_is_dropped_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bug this fixes, in the shape it arrived in.

    `wake_on_lan` strips the separators and unpacks the rest with `bytes.fromhex`, so
    a stray character after the address surfaced as a complaint about a non-hexadecimal
    number at position 12 - a message that says nothing about what was typed, from a
    component the user never went near.
    """
    stored = "00:00:5e:00:53:01x"
    with pytest.raises(ValueError, match="position 12"):
        bytes.fromhex(stored.replace(":", ""))

    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_WOL_MAC: stored})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.options[CONF_WOL_MAC] == ""
    assert sum("not a MAC address" in record.message for record in caplog.records) == 1

    # And the packet goes where the box says it should, which is what an empty
    # override has always meant.
    magic_packets = async_mock_service(hass, "wake_on_lan", "send_magic_packet")
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.dekoder_salon_wake"},
        blocking=True,
    )

    assert magic_packets[0].data["mac"] == MAC


async def test_the_device_carries_the_address_the_packet_goes_to(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Without a MAC connection the device page knew the address and nothing else did."""
    await async_setup_box(hass, config_entry)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_ID), config_entry.entry_id
    )
    assert (dr.CONNECTION_NETWORK_MAC, MAC) in device.connections


async def test_the_override_is_what_the_device_carries(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The registered address is the one a packet would be sent to, not a second one."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_WOL_MAC: "0000.5e00.5302"}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, NODE_ID), config_entry.entry_id
    )
    assert (dr.CONNECTION_NETWORK_MAC, "00:00:5e:00:53:02") in device.connections


async def test_waking_a_box_that_reported_no_mac_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """There is nowhere to send the packet, and saying so beats sending it nowhere."""
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = json.dumps(
        {key: value for key, value in INFO.items() if key != "mac"}
    )
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.dekoder_salon_wake"},
            blocking=True,
        )

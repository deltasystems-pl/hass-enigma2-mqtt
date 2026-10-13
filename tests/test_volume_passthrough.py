"""Whether the receiver's volume changes anything that can be heard.

A receiver set to hand the playing track to the television undecoded moves a bar on the
screen and nothing else. A plugin after 0.4.0 says so on `volume.passthrough` - true,
false or `null` for "cannot be told" - and claims `volume_passthrough`. The volume
controls stay as they are; an attribute on each and a binary sensor of its own carry
the fact, and unknown is never shown as "no".
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from .conftest import (
    INFO,
    INFO_TOPIC,
    NODE_ID,
    VOLUME,
    VOLUME_TOPIC,
    async_setup_box_then_retained,
)

PASSTHROUGH = "binary_sensor.dekoder_salon_audio_passthrough"
NUMBER = "number.dekoder_salon_volume"
PLAYER = "media_player.dekoder_salon"
ATTRIBUTE = "volume_passthrough"


def info_with(*capabilities: str) -> str:
    return json.dumps({**INFO, "capabilities": [*INFO["capabilities"], *capabilities]})


def registered(hass: HomeAssistant) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "binary_sensor", "enigma2_mqtt", f"{NODE_ID}_volume_passthrough"
    )


async def test_without_the_capability_nothing_is_added(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A plugin that publishes the member on an image without the settings claims nothing."""
    box_on_the_broker[VOLUME_TOPIC] = json.dumps({**VOLUME, "passthrough": None})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert registered(hass) is None
    assert ATTRIBUTE not in hass.states.get(NUMBER).attributes
    assert ATTRIBUTE not in hass.states.get(PLAYER).attributes


@pytest.mark.parametrize(
    ("passthrough", "state"),
    [(True, "on"), (False, "off")],
)
async def test_a_stated_answer_is_on_or_off_and_on_both_volume_controls(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    passthrough: bool,
    state: str,
) -> None:
    """True is "the volume does nothing now"; the controls stay usable either way."""
    box_on_the_broker[INFO_TOPIC] = info_with("volume_passthrough")
    box_on_the_broker[VOLUME_TOPIC] = json.dumps({**VOLUME, "passthrough": passthrough})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(PASSTHROUGH).state == state
    number = hass.states.get(NUMBER)
    assert number.state == "35.0"
    assert number.attributes[ATTRIBUTE] is passthrough
    player = hass.states.get(PLAYER)
    assert player.attributes[ATTRIBUTE] is passthrough
    assert player.attributes["volume_level"] == 0.35


@pytest.mark.parametrize(
    "volume",
    [
        {**VOLUME, "passthrough": None},
        # A plugin without the member, on a receiver whose info still claims it.
        dict(VOLUME),
        {**VOLUME, "passthrough": "yes"},
        {**VOLUME, "passthrough": 1},
    ],
)
async def test_what_cannot_be_told_is_unknown_and_not_off(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    volume: dict[str, Any],
) -> None:
    """`null` is nothing playing, a codec nobody named, a setting that is neither."""
    box_on_the_broker[INFO_TOPIC] = info_with("volume_passthrough")
    box_on_the_broker[VOLUME_TOPIC] = json.dumps(volume)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(PASSTHROUGH).state == "unknown"
    assert hass.states.get(NUMBER).state == "35.0"
    assert hass.states.get(NUMBER).attributes[ATTRIBUTE] is None
    assert hass.states.get(PLAYER).attributes[ATTRIBUTE] is None


async def test_it_follows_the_track(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Another track, another answer - within the plugin's five seconds."""
    box_on_the_broker[INFO_TOPIC] = info_with("volume_passthrough")
    box_on_the_broker[VOLUME_TOPIC] = json.dumps({**VOLUME, "passthrough": False})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert hass.states.get(PASSTHROUGH).state == "off"

    async_fire_mqtt_message(hass, VOLUME_TOPIC, json.dumps({**VOLUME, "passthrough": True}))
    await hass.async_block_till_done()

    assert hass.states.get(PASSTHROUGH).state == "on"
    assert hass.states.get(NUMBER).attributes[ATTRIBUTE] is True
    assert hass.states.get(PLAYER).attributes[ATTRIBUTE] is True


async def test_a_capability_that_arrives_after_the_volume_adds_the_attribute(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`info` is published more than once, and the claim can come with a later one."""
    box_on_the_broker[VOLUME_TOPIC] = json.dumps({**VOLUME, "passthrough": True})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert ATTRIBUTE not in hass.states.get(NUMBER).attributes

    async_fire_mqtt_message(hass, INFO_TOPIC, info_with("volume_passthrough"))
    await hass.async_block_till_done()

    assert hass.states.get(PASSTHROUGH).state == "on"
    assert hass.states.get(NUMBER).attributes[ATTRIBUTE] is True
    assert hass.states.get(PLAYER).attributes[ATTRIBUTE] is True


async def test_a_capability_that_goes_quiet_keeps_the_sensor(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A downgraded plugin: the sensor stays and says unknown, the attribute goes."""
    box_on_the_broker[INFO_TOPIC] = info_with("volume_passthrough")
    box_on_the_broker[VOLUME_TOPIC] = json.dumps({**VOLUME, "passthrough": True})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    async_fire_mqtt_message(hass, VOLUME_TOPIC, json.dumps(VOLUME))
    await hass.async_block_till_done()

    assert registered(hass) is not None
    assert hass.states.get(PASSTHROUGH).state == "unknown"
    assert ATTRIBUTE not in hass.states.get(NUMBER).attributes
    assert ATTRIBUTE not in hass.states.get(PLAYER).attributes

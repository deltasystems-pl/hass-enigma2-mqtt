"""Diagnostics that exist only while the receiver is actually reporting them.

A box can advertise that it *can* report conditional-access or OSCam health and still
have that reporting switched off — which is the default, because both are opt-in. The
entities used to be created from the capability alone, so a device page filled up with
diagnostics that read `unknown` for ever and could not be made to say anything. On a
page whose whole job is "is anything wrong", a permanent unknown is indistinguishable
from a fault.
"""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from .conftest import AVAILABILITY_TOPIC, INFO, INFO_TOPIC, async_setup_box

CAM_ENTITIES = (
    ("sensor", "cam_system"),
    ("sensor", "cam_ecm_time"),
    ("binary_sensor", "cam_active"),
    ("binary_sensor", "service_encrypted"),
)
OSCAM_ENTITIES = (
    ("sensor", "oscam_status"),
    ("sensor", "oscam_uptime"),
    ("binary_sensor", "oscam_api_reachable"),
    ("binary_sensor", "oscam_readonly"),
)


def registered(hass: HomeAssistant, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        platform, "enigma2_mqtt", f"vuuno4kse_005301_{key}"
    )


def info(*capabilities: str, **settings: bool) -> str:
    return json.dumps(
        {
            **INFO,
            "capabilities": [*INFO["capabilities"], *capabilities],
            "settings": settings,
        }
    )


@pytest.mark.parametrize(
    ("capability", "setting", "entities"),
    [
        ("cam", "cam_telemetry", CAM_ENTITIES),
        ("oscam", "oscam_telemetry", OSCAM_ENTITIES),
    ],
)
async def test_the_capability_alone_does_not_create_diagnostics(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    capability: str,
    setting: str,
    entities: tuple[tuple[str, str], ...],
) -> None:
    """Telemetry is off, so there is nothing to show and nothing is created."""
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = info(capability, **{setting: False})
    await async_setup_box(hass, config_entry)

    for platform, key in entities:
        assert registered(hass, platform, key) is None


@pytest.mark.parametrize(
    ("capability", "setting", "entities"),
    [
        ("cam", "cam_telemetry", CAM_ENTITIES),
        ("oscam", "oscam_telemetry", OSCAM_ENTITIES),
    ],
)
async def test_turning_the_telemetry_on_creates_them(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    capability: str,
    setting: str,
    entities: tuple[tuple[str, str], ...],
) -> None:
    """The option is answered on `info`, which can arrive long after setup."""
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = info(capability, **{setting: False})
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, INFO_TOPIC, info(capability, **{setting: True}))
    await hass.async_block_till_done()

    for platform, key in entities:
        assert registered(hass, platform, key) is not None


@pytest.mark.parametrize(
    ("capability", "setting", "entities"),
    [
        ("cam", "cam_telemetry", CAM_ENTITIES),
        ("oscam", "oscam_telemetry", OSCAM_ENTITIES),
    ],
)
async def test_turning_it_off_again_takes_them_away(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    capability: str,
    setting: str,
    entities: tuple[tuple[str, str], ...],
) -> None:
    """An entity left behind would keep a stale reading on the page for ever."""
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = info(capability, **{setting: True})
    await async_setup_box(hass, config_entry)
    assert all(registered(hass, platform, key) for platform, key in entities)

    async_fire_mqtt_message(hass, INFO_TOPIC, info(capability, **{setting: False}))
    await hass.async_block_till_done()

    for platform, key in entities:
        assert registered(hass, platform, key) is None


@pytest.mark.parametrize(
    ("capability", "setting", "entities"),
    [
        ("cam", "cam_telemetry", CAM_ENTITIES),
        ("oscam", "oscam_telemetry", OSCAM_ENTITIES),
    ],
)
async def test_an_info_payload_that_says_nothing_about_the_option_removes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    capability: str,
    setting: str,
    entities: tuple[tuple[str, str], ...],
) -> None:
    """Silence is not "off", and the difference is somebody's customisation.

    A plugin that has not read its own configuration yet, or an older one that has no
    such option, publishes `info` without the setting in it. Reading that as "turned
    off" deleted the entities from the registry — with the names, the areas, the icons
    and the history the household had given them — and the next `info` a second later
    brought them back as strangers.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    retained[INFO_TOPIC] = info(capability, **{setting: True})
    await async_setup_box(hass, config_entry)
    before = {key: registered(hass, platform, key) for platform, key in entities}
    assert all(before.values())

    async_fire_mqtt_message(
        hass,
        INFO_TOPIC,
        json.dumps({**INFO, "capabilities": [*INFO["capabilities"], capability]}),
    )
    await hass.async_block_till_done()

    assert {key: registered(hass, platform, key) for platform, key in entities} == before


async def test_a_box_without_the_capability_never_gets_them(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Asking for telemetry a plugin cannot publish is not a reason to show entities."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(
        hass,
        INFO_TOPIC,
        json.dumps({**INFO, "settings": {"cam_telemetry": True, "oscam_telemetry": True}}),
    )
    await hass.async_block_till_done()

    for platform, key in (*CAM_ENTITIES, *OSCAM_ENTITIES):
        assert registered(hass, platform, key) is None


async def test_the_everyday_entities_are_untouched_by_any_of_this(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Whatever the options say, the channel and the recording light are always there."""
    await async_setup_box(hass, config_entry)

    assert registered(hass, "sensor", "channel") is not None
    assert registered(hass, "binary_sensor", "recording") is not None

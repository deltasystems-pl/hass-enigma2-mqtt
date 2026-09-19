"""Two yes-or-no facts about the receiver.

„Nagrywanie" is what the deep-standby and reboot guards on the box read, so it is also
what an automation should read before it does anything that would interrupt a
recording. „Dysk nagrań" exists because a recording disk that silently unmounts is a
receiver that records nothing and says nothing — which is the whole reason the plugin
publishes `hdd` at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry, Enigma2State
from .const import TOPIC_CAM, TOPIC_HDD, TOPIC_OSCAM, TOPIC_RECORDING
from .entity import Enigma2Entity, OptionalEntities

PARALLEL_UPDATES = 0


def _is_recording(state: Enigma2State) -> bool:
    """Return whether anything is being recorded right now."""
    recording = state.recording or {}
    active = recording.get("active")
    return bool(active) if isinstance(active, list) else False


def _cam_bool(state: Enigma2State, key: str) -> bool | None:
    """Keep an unknown CAM result unknown rather than falsely healthy."""
    value = (state.cam or {}).get(key)
    return value if isinstance(value, bool) else None


@dataclass(frozen=True, kw_only=True)
class Enigma2BinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor, and the topic and payload fields behind it."""

    topics: tuple[str, ...]
    value_fn: Callable[[Enigma2State], bool | None]
    attributes_fn: Callable[[Enigma2State], dict[str, Any]] | None = None


BINARY_SENSORS: tuple[Enigma2BinarySensorDescription, ...] = (
    Enigma2BinarySensorDescription(
        key="recording",
        topics=(TOPIC_RECORDING,),
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=_is_recording,
    ),
    Enigma2BinarySensorDescription(
        key="recording_disk",
        topics=(TOPIC_HDD,),
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda state: bool((state.hdd or {}).get("mounted")),
        attributes_fn=lambda state: {
            "path": (state.hdd or {}).get("path"),
            "free_mb": (state.hdd or {}).get("free_mb"),
        },
    ),
)

CAM_BINARY_SENSORS: tuple[Enigma2BinarySensorDescription, ...] = (
    Enigma2BinarySensorDescription(
        key="cam_active",
        topics=(TOPIC_CAM,),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _cam_bool(state, "active"),
    ),
    Enigma2BinarySensorDescription(
        key="service_encrypted",
        topics=(TOPIC_CAM,),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _cam_bool(state, "encrypted"),
    ),
)

OSCAM_BINARY_SENSORS: tuple[Enigma2BinarySensorDescription, ...] = tuple(
    Enigma2BinarySensorDescription(
        key=f"oscam_{key}",
        topics=(TOPIC_OSCAM,),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state, field=key: (
            (state.oscam or {}).get(field)
            if isinstance((state.oscam or {}).get(field), bool)
            else None
        ),
    )
    for key in ("software_running", "api_reachable", "readonly")
) + (
    Enigma2BinarySensorDescription(
        key="oscam_api_access",
        topics=(TOPIC_OSCAM,),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: (
            True
            if (state.oscam or {}).get("api_access") == "granted"
            else False
            if (state.oscam or {}).get("api_access") == "denied"
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors of one box."""
    box = entry.runtime_data
    async_add_entities(Enigma2BinarySensor(box, description) for description in BINARY_SENSORS)

    by_key = {
        description.key: description
        for description in (*CAM_BINARY_SENSORS, *OSCAM_BINARY_SENSORS)
    }
    for keys, enabled in (
        ([description.key for description in CAM_BINARY_SENSORS], lambda: box.cam_enabled),
        ([description.key for description in OSCAM_BINARY_SENSORS], lambda: box.oscam_enabled),
    ):
        entry.async_on_unload(
            OptionalEntities(
                hass,
                box,
                "binary_sensor",
                keys,
                lambda key: Enigma2BinarySensor(box, by_key[key]),
                enabled,
                async_add_entities,
            ).start()
        )


class Enigma2BinarySensor(Enigma2Entity, BinarySensorEntity):
    """One yes-or-no value read off one of the box's topics."""

    entity_description: Enigma2BinarySensorDescription

    def __init__(self, box: Enigma2Box, description: Enigma2BinarySensorDescription) -> None:
        """Set up the binary sensor from its description."""
        super().__init__(box, description.key, topics=description.topics)
        self.entity_description = description

    @callback
    def _async_read_state(self) -> None:
        """Read the value, and the attributes that explain it."""
        description = self.entity_description
        self._attr_is_on = description.value_fn(self.box.state)
        if description.attributes_fn is not None:
            self._attr_extra_state_attributes = description.attributes_fn(self.box.state)

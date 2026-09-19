"""What the receiver is doing, as sensors.

Nine of them, from two groups. The first five are what a household looks at — the
channel, what is on now and next, whether anything is being recorded and when the next
recording starts. The last four are diagnostics: signal quality and uptime matter when
something is wrong and are noise on a dashboard, so they are in the diagnostic category
and disabled until somebody turns them on.

Two conventions run through the file. Epoch seconds from the topics become ISO strings
in attributes and `datetime` objects in states, because those are what a template and a
timestamp sensor respectively can work with. And the long programme description and the
list of running recordings are excluded from the recorder: they are kilobytes that
change every quarter of an hour, and a database is not where a plot summary belongs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
import homeassistant.util.dt as dt_util

from .box import Enigma2Box, Enigma2MqttConfigEntry, Enigma2State
from .const import (
    DOMAIN,
    TOPIC_CAM,
    TOPIC_EPG,
    TOPIC_INFO,
    TOPIC_RECORDING,
    TOPIC_SERVICE,
    TOPIC_TUNER,
)
from .entity import Enigma2Entity

PARALLEL_UPDATES = 0


def _iso(value: Any) -> str | None:
    """Return epoch seconds as an ISO 8601 string in UTC, or None."""
    if not isinstance(value, (int, float)):
        return None
    return dt_util.utc_from_timestamp(value).isoformat()


def _event(state: Enigma2State, which: str) -> dict[str, Any]:
    """Return `epg.now` or `epg.next`, which are objects or null."""
    epg = state.epg or {}
    event = epg.get(which)
    return event if isinstance(event, dict) else {}


def _event_attributes(state: Enigma2State, which: str) -> dict[str, Any]:
    """Return one programme's details, with its times as ISO strings."""
    event = _event(state, which)
    return {
        "begin": _iso(event.get("begin")),
        "end": _iso(event.get("end")),
        "event_id": event.get("event_id"),
        "short": event.get("short") or None,
        "long": event.get("long") or None,
    }


def _active_recordings(state: Enigma2State) -> list[Any]:
    """Return the recordings running right now."""
    recording = state.recording or {}
    active = recording.get("active")
    return active if isinstance(active, list) else []


def _next_timer(state: Enigma2State) -> datetime | None:
    """Return when the next recording starts, or None when none is due."""
    recording = state.recording or {}
    following = recording.get("next")
    if not isinstance(following, dict):
        return None
    begin = following.get("begin")
    if not isinstance(begin, (int, float)):
        return None
    return dt_util.utc_from_timestamp(begin)


def _cam_value(state: Enigma2State, key: str, expected: type) -> Any:
    """Return one CAM value only when its JSON type is exact."""
    value = (state.cam or {}).get(key)
    if expected is int and isinstance(value, bool):
        return None
    return value if isinstance(value, expected) else None


@dataclass(frozen=True, kw_only=True)
class Enigma2SensorDescription(SensorEntityDescription):
    """A sensor, and the topics and payload fields behind it."""

    topics: tuple[str, ...]
    value_fn: Callable[[Enigma2State], StateType | datetime]
    attributes_fn: Callable[[Enigma2State], dict[str, Any]] | None = None


SENSORS: tuple[Enigma2SensorDescription, ...] = (
    Enigma2SensorDescription(
        key="channel",
        topics=(TOPIC_SERVICE,),
        value_fn=lambda state: (state.service or {}).get("name"),
        attributes_fn=lambda state: {
            "sref": (state.service or {}).get("sref"),
            "bouquet": (state.service or {}).get("bouquet"),
            "provider": (state.service or {}).get("provider"),
            "width": (state.service or {}).get("width"),
            "height": (state.service or {}).get("height"),
        },
    ),
    Enigma2SensorDescription(
        key="program",
        topics=(TOPIC_EPG,),
        value_fn=lambda state: _event(state, "now").get("title"),
        attributes_fn=lambda state: _event_attributes(state, "now"),
    ),
    Enigma2SensorDescription(
        key="next_program",
        topics=(TOPIC_EPG,),
        value_fn=lambda state: _event(state, "next").get("title"),
        attributes_fn=lambda state: _event_attributes(state, "next"),
    ),
    Enigma2SensorDescription(
        key="active_recordings",
        topics=(TOPIC_RECORDING,),
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: len(_active_recordings(state)),
        attributes_fn=lambda state: {"recordings": _active_recordings(state)},
    ),
    Enigma2SensorDescription(
        key="next_timer",
        topics=(TOPIC_RECORDING,),
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_next_timer,
    ),
    Enigma2SensorDescription(
        key="snr",
        topics=(TOPIC_TUNER,),
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: (state.tuner or {}).get("snr"),
    ),
    Enigma2SensorDescription(
        key="agc",
        topics=(TOPIC_TUNER,),
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: (state.tuner or {}).get("agc"),
    ),
    Enigma2SensorDescription(
        key="ber",
        topics=(TOPIC_TUNER,),
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: (state.tuner or {}).get("ber"),
    ),
    Enigma2SensorDescription(
        key="uptime",
        topics=(TOPIC_INFO,),
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.info.get("uptime"),
    ),
    Enigma2SensorDescription(
        key="cam_system",
        topics=(TOPIC_CAM,),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _cam_value(state, "system", str),
    ),
    Enigma2SensorDescription(
        key="cam_ecm_time",
        topics=(TOPIC_CAM,),
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _cam_value(state, "ecm_ms", int),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up every sensor of one box."""
    box = entry.runtime_data
    descriptions = SENSORS if "cam" in box.capabilities else SENSORS[:-2]
    async_add_entities(Enigma2Sensor(box, description) for description in descriptions)
    if "cam" not in box.capabilities:
        registry = er.async_get(hass)
        for key in ("cam_system", "cam_ecm_time"):
            if entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{box.node_id}_{key}"):
                registry.async_remove(entity_id)


class Enigma2Sensor(Enigma2Entity, SensorEntity):
    """One value read off one of the box's topics."""

    entity_description: Enigma2SensorDescription

    # A plot summary and a list of running recordings are kilobytes that change every
    # quarter of an hour. The state is worth recording; these are not.
    _unrecorded_attributes = frozenset({"long", "recordings"})

    def __init__(self, box: Enigma2Box, description: Enigma2SensorDescription) -> None:
        """Set up the sensor from its description."""
        super().__init__(box, description.key, topics=description.topics)
        self.entity_description = description

    @callback
    def _async_read_state(self) -> None:
        """Read the value, and the attributes that explain it."""
        description = self.entity_description
        self._attr_native_value = description.value_fn(self.box.state)
        if description.attributes_fn is not None:
            self._attr_extra_state_attributes = description.attributes_fn(
                self.box.state
            )

"""What the receiver is doing, as sensors.

Ten of them, from three groups. The first five are what a household looks at — the
channel, what is on now and next, whether anything is being recorded and when the next
recording starts. The next four are diagnostics: signal quality and uptime matter when
something is wrong and are noise on a dashboard, so they are in the diagnostic category
and disabled until somebody turns them on. The last one is „Ostatni błąd", which is the
opposite case: a diagnostic that is enabled, because the only time anybody goes looking
for it is after something has already gone wrong.

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
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import StateType
import homeassistant.util.dt as dt_util

from .box import Enigma2Box, Enigma2MqttConfigEntry, Enigma2State
from .const import (
    CONF_CAM_TELEMETRY,
    CONF_OSCAM_TELEMETRY,
    DOMAIN,
    ERROR_TEXT_MAX,
    TOPIC_CAM,
    TOPIC_EPG,
    TOPIC_INFO,
    TOPIC_LAST_ERROR,
    TOPIC_OSCAM,
    TOPIC_RECORDING,
    TOPIC_SERVICE,
    TOPIC_TUNER,
)
from .entity import Enigma2Entity, OptionalEntities

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
)

CAM_SENSORS: tuple[Enigma2SensorDescription, ...] = (
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

OSCAM_SENSORS: tuple[Enigma2SensorDescription, ...] = (
    Enigma2SensorDescription(
        key="oscam_status",
        topics=(TOPIC_OSCAM,),
        device_class=SensorDeviceClass.ENUM,
        options=["running", "stopped"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: (
            "running"
            if (state.oscam or {}).get("software_running") is True
            else "stopped"
            if (state.oscam or {}).get("software_running") is False
            else None
        ),
        attributes_fn=lambda state: {
            "version": (state.oscam or {}).get("version"),
            "api_access": (state.oscam or {}).get("api_access"),
            "readonly": (state.oscam or {}).get("readonly"),
        },
    ),
    *(
        Enigma2SensorDescription(
            key=f"oscam_{key}",
            topics=(TOPIC_OSCAM,),
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda state, field=key: (state.oscam or {}).get(field),
        )
        for key in (
            "readers_configured",
            "readers_enabled",
            "readers_healthy",
            "cards_ready",
            "servers_connected",
            "shared_cards",
        )
    ),
    Enigma2SensorDescription(
        key="oscam_uptime",
        topics=(TOPIC_OSCAM,),
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: (state.oscam or {}).get("uptime_s"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up every sensor of one box."""
    box = entry.runtime_data
    async_add_entities(
        [
            *(Enigma2Sensor(box, description) for description in SENSORS),
            Enigma2LastErrorSensor(box),
        ]
    )

    by_key = {description.key: description for description in (*CAM_SENSORS, *OSCAM_SENSORS)}
    for keys, enabled, declared in (
        (
            [description.key for description in CAM_SENSORS],
            lambda: box.cam_enabled,
            lambda: box.telemetry_declared(CONF_CAM_TELEMETRY),
        ),
        (
            [description.key for description in OSCAM_SENSORS],
            lambda: box.oscam_enabled,
            lambda: box.telemetry_declared(CONF_OSCAM_TELEMETRY),
        ),
    ):
        entry.async_on_unload(
            OptionalEntities(
                hass,
                box,
                "sensor",
                keys,
                lambda key: Enigma2Sensor(box, by_key[key]),
                enabled,
                declared,
                async_add_entities,
            ).start()
        )

    # The manager is started whatever the box has said so far, because at this point it
    # has usually said nothing: `async_setup_entry` subscribes and returns, and the
    # retained burst that carries `info` — and with it the capability list — arrives
    # after the platforms have been set up. Reading the capability here meant that on
    # every real receiver the per-source entities were never created at all, and that
    # the branch taken instead deleted every `oscam_` registration the box had. The
    # manager already listens to `info`, so it can decide when there is something to
    # decide from.
    entry.async_on_unload(_OscamEntityManager(hass, box, async_add_entities).start())


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
            self._attr_extra_state_attributes = description.attributes_fn(self.box.state)


class Enigma2LastErrorSensor(Enigma2Entity, RestoreEntity, SensorEntity):
    """The last thing the receiver refused to do, and what it said about it.

    The integration owns this memory rather than the topic. The plugin clears
    `last_error` on the next command that succeeds, and the person who wants to know why
    „Restart" did nothing is looking afterwards — usually after the next volume step has
    already wiped the evidence. So a cleared topic leaves this sensor alone, and a
    restart does too: the last complaint is restored with the time it was first seen,
    not with the time the retained payload was replayed at start-up.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the sensor on the complaint topic, with no state to wait for."""
        super().__init__(box, "last_error", topics=(TOPIC_LAST_ERROR,), requires=None)
        # How many `last_error` payloads had arrived when this entity last looked. The
        # count rather than the payload, because the same command failing the same way
        # twice is two errors and the second one has its own time.
        self._handled = 0
        self._attr_native_value = None
        self._attr_extra_state_attributes: dict[str, Any] = {"error": None, "time": None}

    async def async_added_to_hass(self) -> None:
        """Restore the last complaint before reading anything new."""
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            self._attr_native_value = last_state.state
            self._attr_extra_state_attributes = {
                "error": last_state.attributes.get("error"),
                "time": last_state.attributes.get("time"),
            }
            # The retained complaint is about to be replayed by the broker, and it is
            # the one just restored. Taking it again would move its timestamp to now
            # and lose the only thing that says when it happened.
            self._handled = self.box.updates.get(TOPIC_LAST_ERROR, 0)
        await super().async_added_to_hass()

    @callback
    def _async_read_state(self) -> None:
        """Take a new complaint, and only a new one."""
        arrived = self.box.updates.get(TOPIC_LAST_ERROR, 0)
        if arrived == self._handled:
            return
        self._handled = arrived
        payload = self.box.state.last_error
        if not isinstance(payload, dict):
            # An empty payload is the plugin clearing the topic after a success. It
            # says nothing about the last failure, and this entity exists precisely
            # because that clear happens before anybody looks.
            return
        command = payload.get("cmd")
        if not isinstance(command, str) or not command:
            return
        self._attr_native_value = command[:ERROR_TEXT_MAX]
        self._attr_extra_state_attributes = {
            "error": str(payload.get("error") or "")[:ERROR_TEXT_MAX] or None,
            "time": dt_util.utcnow().isoformat(),
        }


class OscamSourceSensor(Enigma2Entity, SensorEntity):
    """One privacy-neutral OSCam reader or server diagnostic."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, box: Enigma2Box, source_id: str, metric: str) -> None:
        super().__init__(box, f"oscam_{source_id}_{metric}", topics=(TOPIC_OSCAM,))
        self._source_id = source_id
        self._metric = metric
        self._attr_translation_key = f"oscam_source_{metric}"
        self._attr_translation_placeholders = {"id": source_id.split("_", 1)[1]}
        if metric == "status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [
                "ready",
                "connected",
                "disabled",
                "no_card",
                "initializing",
                "connecting",
                "disconnected",
                "sleeping",
                "duplicate",
                "error",
                "unknown",
            ]

    @property
    def available(self) -> bool:
        payload = self.box.state.oscam or {}
        return (
            super().available
            and payload.get("api_reachable") is True
            and payload.get("api_access") == "granted"
            and self._source() is not None
        )

    def _source(self) -> dict[str, Any] | None:
        for source in (self.box.state.oscam or {}).get("readers") or []:
            if isinstance(source, dict) and source.get("id") == self._source_id:
                return source
        return None

    @callback
    def _async_read_state(self) -> None:
        source = self._source()
        if source is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = None
        elif self._metric == "status":
            self._attr_native_value = source.get("status")
            self._attr_extra_state_attributes = {
                "kind": source.get("kind"),
                "enabled": source.get("enabled"),
                "protocol": source.get("protocol"),
            }
        elif self._metric == "ready_cards":
            self._attr_native_value = (
                1
                if source.get("status") == "ready"
                else 0
                if source.get("status") in ("no_card", "disabled")
                else None
            )
        else:
            self._attr_native_value = source.get("shared_cards")


class _OscamEntityManager:
    """Reconcile dynamic source entities only from authoritative snapshots."""

    def __init__(self, hass, box, add_entities) -> None:
        self.hass = hass
        self.box = box
        self.add_entities = add_entities
        self.known: set[str] = self._registry_source_ids()
        self.active: set[str] = set()

    def _registry_source_ids(self) -> set[str]:
        registry = er.async_get(self.hass)
        prefix = f"{self.box.node_id}_oscam_"
        found = set()
        for entry in er.async_entries_for_config_entry(registry, self.box.entry.entry_id):
            if entry.platform != DOMAIN or not entry.unique_id.startswith(prefix):
                continue
            match = re.fullmatch(
                r"((?:reader|server|source)_[0-9a-f]{12})_"
                r"(?:status|ready_cards|shared_cards)",
                entry.unique_id[len(prefix) :],
            )
            if match:
                found.add(match.group(1))
        return found

    def start(self):
        unsubscribe = self.box.async_add_listener(self._update, (TOPIC_OSCAM, TOPIC_INFO))
        self._update()
        return unsubscribe

    @callback
    def _update(self) -> None:
        payload = self.box.state.oscam
        if payload is None:
            # Only a stated "off" removes anything, the same rule the fixed optional
            # entities follow. Before `info` arrives the box has not said "off"; it has
            # said nothing, and nothing is not a reason to delete somebody's entities.
            if self.box.telemetry_declared(CONF_OSCAM_TELEMETRY) and not self.box.oscam_enabled:
                self._remove(self.known)
            return
        readers = payload.get("readers") or []
        authoritative = (
            payload.get("api_reachable") is True and payload.get("api_access") == "granted"
        )
        current = {source["id"] for source in readers if isinstance(source, dict)}
        additions = current - self.active
        entities = []
        for source in readers:
            kind = source["kind"]
            source_id = source["id"]
            if source_id not in additions:
                continue
            entities.append(OscamSourceSensor(self.box, source_id, "status"))
            if kind == "reader":
                entities.append(OscamSourceSensor(self.box, source_id, "ready_cards"))
            elif kind == "server":
                entities.append(OscamSourceSensor(self.box, source_id, "shared_cards"))
        if entities:
            self.add_entities(entities)
        self.known.update(additions)
        self.active.update(additions)
        if authoritative:
            self._remove(self.known - current)

    def _remove(self, source_ids: set[str]) -> None:
        if not source_ids:
            return
        registry = er.async_get(self.hass)
        for source_id in set(source_ids):
            for metric in ("status", "ready_cards", "shared_cards"):
                unique_id = f"{self.box.node_id}_oscam_{source_id}_{metric}"
                if entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id):
                    registry.async_remove(entity_id)
            self.known.discard(source_id)
            self.active.discard(source_id)

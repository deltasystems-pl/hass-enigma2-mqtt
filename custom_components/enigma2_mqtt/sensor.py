"""What the receiver is doing, as sensors.

Ten always, and more when the box says it can measure them. The first five are what a
household looks at — the channel, what is on now and next, whether anything is being
recorded and when the next recording starts. The next four are diagnostics: signal
quality and uptime matter when something is wrong and are noise on a dashboard, so they
are in the diagnostic category and disabled until somebody turns them on. The tenth is
„Ostatni błąd", which is the opposite case: a diagnostic that is enabled, because the
only time anybody goes looking for it is after something has already gone wrong.

The rest depend on what the plugin announced. The `process` group — what enigma2 itself
is using — appears when the box names that capability. The conditional-access and OSCam
groups follow a setting instead of a capability, because they are a choice about privacy
rather than about what the image allows. „EPG – aktywny bukiet" follows two capabilities
at once — the grids and the channel-list context — and is the one sensor here whose state
moves with the clock as well as with the topics.

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
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import StateType
import homeassistant.util.dt as dt_util

from .box import Enigma2Box, Enigma2MqttConfigEntry, Enigma2State
from .const import (
    CAPABILITY_BOUQUET_CONTEXT,
    CAPABILITY_EPG_GRID,
    CAPABILITY_EPG_IMPORT,
    CAPABILITY_PROCESS,
    CAPABILITY_SOFTCAM,
    CONF_CAM_TELEMETRY,
    CONF_OSCAM_TELEMETRY,
    DOMAIN,
    EPG_IMPORT_STATES,
    EPG_TITLE_MAX,
    ERROR_TEXT_MAX,
    TOPIC_BOUQUET,
    TOPIC_CAM,
    TOPIC_EPG,
    TOPIC_EPG_GRID,
    TOPIC_EPG_IMPORT,
    TOPIC_INFO,
    TOPIC_LAST_ERROR,
    TOPIC_OSCAM,
    TOPIC_PROCESS,
    TOPIC_RECORDING,
    TOPIC_SERVICE,
    TOPIC_SOFTCAM,
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


def _refused_at(ts: Any) -> str | None:
    """Return when the receiver says it refused, or None when it does not say.

    The complaint carries `ts`, and using it is what makes a replay idempotent: the
    retained payload arrives again on every reconnect and at every start-up, and a
    clock reading would put a new time on the same event each time. A `ts` the caller
    can rely on therefore answers the whole question of whether two payloads are the
    same refusal, which is why this says nothing rather than guessing when there is
    none to rely on.

    Zero and negative values are not timestamps here, whatever `utc_from_timestamp`
    makes of them: the plugin stamps the moment it refused, and a complaint dated
    1969 is a field that was never filled in.
    """
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        return None
    try:
        return dt_util.utc_from_timestamp(ts).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _process_value(state: Enigma2State, key: str) -> int | None:
    """Return one bounded integer from the `process` topic, or None."""
    value = (state.process or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mebibytes(state: Enigma2State, key: str) -> float | None:
    """Return a kilobyte count as MiB, at the precision the number deserves.

    The plugin reports kilobytes because that is what `/proc` gives it. A hundredth of a
    mebibyte is ten kilobytes of jitter, which on an hour-by-hour memory curve is noise
    with a decimal point on it; one place is enough to see a leak and not enough to make
    a graph look busy when nothing is happening.
    """
    value = _process_value(state, key)
    return None if value is None else round(value / 1024, 1)


def _started(state: Enigma2State) -> datetime | None:
    """Return when the enigma2 process started, as a moment rather than a number."""
    value = _process_value(state, "started")
    return None if value is None else dt_util.utc_from_timestamp(value)


def _softcam_attributes(state: Enigma2State) -> dict[str, Any]:
    """Return everything the `softcam` topic carries besides the binary's name.

    Read straight out of the normalised payload, because `box._normalize_softcam` has
    already decided what each field is worth: every key is present there, a value that
    could not be believed is `None`, and nothing that was not named in the contract
    survived. Repeating those checks here would be a second opinion on the same bytes,
    and the two would eventually disagree.

    `last_restart` is the one that changes shape. The topic carries epoch seconds and
    this file's convention is that a time in an attribute is an ISO 8601 string, because
    that is what a template can read.

    🔴 `running_instances` is `None` rather than `0` when the count could not be taken.
    Zero instances is a real reading — it is a channel that has stopped decoding — and a
    number that means "we do not know" would be the one thing an automation must never be
    given here.
    """
    softcam = state.softcam or {}
    return {
        "running_instances": softcam.get("running_instances"),
        "last_restart": _iso(softcam.get("last_restart")),
        "last_restart_reason": softcam.get("last_restart_reason"),
        "restarts_today": softcam.get("restarts_today"),
        # Whether the image's own liveness check adds a copy of the cam at every GUI
        # start on this box — the difference between a receiver that needs the restart
        # button and one that merely has it.
        "manager_check_on_start": softcam.get("manager_check_on_start"),
        # The image's periodic check interval when it is switched on, and `None` when it
        # is not. A box with it on gains an instance every interval for ever, which is
        # the runaway worth seeing on a dashboard before it becomes a symptom.
        "manager_timer_minutes": softcam.get("manager_timer_minutes"),
    }


def _cam_value(state: Enigma2State, key: str, expected: type) -> Any:
    """Return one CAM value only when its JSON type is exact."""
    value = (state.cam or {}).get(key)
    if expected is int and isinstance(value, bool):
        return None
    return value if isinstance(value, expected) else None


def _epoch(value: Any) -> int | None:
    """Return a whole number of epoch seconds, or None.

    `True` is an `int` in Python, and a programme that began at second one of 1970 is a
    field nobody filled in; neither is a time a card should draw.

    A number too large to be a date is refused here too, by asking the same conversion
    the clock timer will make rather than by guessing a ceiling: the grid is read on the
    callback that feeds every topic, and one event ending in the year 300 000 would
    otherwise raise out of it on every message for as long as that bouquet is active.
    Such an event is left out, exactly like one whose time is not a number at all.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    try:
        dt_util.utc_from_timestamp(value)
    except (OverflowError, OSError, ValueError):
        return None
    return value


def _grid_events(channel: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one channel's usable events, as `{title, begin, end}`, in time order.

    The grid arrives from a box and is not trusted to be what the contract says: an event
    without a title or with an end that is not after its begin is left out, rather than
    drawn as a programme that lasts no time at all. The title is capped here, once, so
    everything downstream — the attribute and the comparison that decides whether the
    state moved — sees the same string.
    """
    events = channel.get("events")
    if not isinstance(events, list):
        return []
    usable: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        title = event.get("title")
        begin = _epoch(event.get("begin"))
        end = _epoch(event.get("end"))
        if not isinstance(title, str) or not title or begin is None or end is None:
            continue
        if end <= begin:
            continue
        usable.append({"title": title[:EPG_TITLE_MAX], "begin": begin, "end": end})
    usable.sort(key=lambda event: event["begin"])
    return usable


def _now_and_next(
    events: list[dict[str, Any]], now: float
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return the programme on air at `now` and the one after it.

    Read against the clock rather than taken from the order of the list: the plugin
    republishes a grid only when its content changes, so the first event in a payload an
    hour old is very likely over. A channel between two programmes has no `now` and still
    has a `next`, which is what its own EPG would show.
    """
    upcoming = [event for event in events if event["end"] > now]
    if not upcoming:
        return None, None
    if upcoming[0]["begin"] <= now:
        return upcoming[0], upcoming[1] if len(upcoming) > 1 else None
    return None, upcoming[0]


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

# What the enigma2 process itself is doing, which is the question behind "the box has
# gone slow", "the interface died again" and "it was fine until Thursday". Only the
# memory is on by default: a resident set size over weeks is the one of these that
# answers a question nobody thought to ask at the time, and the recorder cannot go back
# and collect it later.
PROCESS_SENSORS: tuple[Enigma2SensorDescription, ...] = (
    Enigma2SensorDescription(
        key="process_memory",
        topics=(TOPIC_PROCESS,),
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: _mebibytes(state, "rss_kb"),
    ),
    Enigma2SensorDescription(
        key="process_memory_peak",
        topics=(TOPIC_PROCESS,),
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: _mebibytes(state, "hwm_kb"),
    ),
    Enigma2SensorDescription(
        key="process_threads",
        topics=(TOPIC_PROCESS,),
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: _process_value(state, "threads"),
    ),
    Enigma2SensorDescription(
        key="process_open_files",
        topics=(TOPIC_PROCESS,),
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: _process_value(state, "fds"),
    ),
    Enigma2SensorDescription(
        key="process_started",
        topics=(TOPIC_PROCESS,),
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_started,
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

SOFTCAM_SENSORS: tuple[Enigma2SensorDescription, ...] = (
    Enigma2SensorDescription(
        key="softcam",
        topics=(TOPIC_SOFTCAM,),
        entity_category=EntityCategory.DIAGNOSTIC,
        # The binary the image selected for autostart, e.g. `OSCam_00000-r000`. Not a
        # family name and not the protocol it speaks outward: on the receiver this was
        # written against the binary is OSCam and the protocol is cccam, and confusing
        # the two is how a support answer ends up about the wrong program.
        value_fn=lambda state: (state.softcam or {}).get("selected"),
        attributes_fn=_softcam_attributes,
    ),
)

# „Import EPG": what the receiver's own EPG-Importer is doing, whoever started it. The
# state is the topic's `state`; the attributes are the rest of the contract, with the two
# times as ISO strings like every other time in an attribute here.
EPG_IMPORT_SENSORS: tuple[Enigma2SensorDescription, ...] = (
    Enigma2SensorDescription(
        key="epg_import",
        topics=(TOPIC_EPG_IMPORT,),
        device_class=SensorDeviceClass.ENUM,
        options=list(EPG_IMPORT_STATES),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: (state.epg_import or {}).get("state"),
        attributes_fn=lambda state: {
            "started": _iso((state.epg_import or {}).get("started")),
            "finished": _iso((state.epg_import or {}).get("finished")),
            "events": (state.epg_import or {}).get("events"),
            "error": (state.epg_import or {}).get("error"),
        },
    ),
)

KEY_EPG_ACTIVE_BOUQUET = "epg_active_bouquet"

# The grids to read, and the context that says which one is in use. A box that publishes
# grids but no context has no „active" bouquet to speak of, and one with a context but no
# grids has nothing to show for it.
EPG_ACTIVE_BOUQUET_CAPABILITIES = frozenset(
    {CAPABILITY_EPG_GRID, CAPABILITY_BOUQUET_CONTEXT}
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

    by_key = {
        description.key: description
        for description in (*PROCESS_SENSORS, *CAM_SENSORS, *OSCAM_SENSORS)
    }
    for keys, enabled, declared in (
        (
            [description.key for description in PROCESS_SENSORS],
            lambda: CAPABILITY_PROCESS in box.capabilities,
            # Never. The CAM and OSCam entities follow a setting a household can turn
            # off, and turning it off is a reason to take them away. This follows a
            # capability, and a capability that stops being named is not a decision —
            # it is a downgraded plugin, an image that lost a hook, or a box that has
            # not answered yet. None of those is a reason to delete somebody's history.
            lambda: False,
        ),
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

    softcam = {description.key: description for description in SOFTCAM_SENSORS}
    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "sensor",
            list(softcam),
            lambda key: Enigma2Sensor(box, softcam[key]),
            lambda: CAPABILITY_SOFTCAM in box.capabilities,
            # 🔴 Nothing here ever removes this sensor, which is why the "has the box
            # answered" side is a flat no. The two above follow a *setting*: somebody
            # turned the telemetry off, meant it, and an entity that can never say
            # anything again is worse than none. This follows a *capability*, and a
            # capability going quiet is not a decision anybody made — it is an older
            # plugin, a hook that failed to attach on this boot, or a receiver that is
            # simply not there. Deleting on that takes the rename, the area, the
            # dashboard card and the whole history of a diagnostic with it, and the next
            # payload brings it back as a stranger. It stays, and says nothing until the
            # topic returns.
            lambda: False,
            async_add_entities,
        ).start()
    )

    epg_import = {description.key: description for description in EPG_IMPORT_SENSORS}
    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "sensor",
            list(epg_import),
            lambda key: Enigma2Sensor(box, epg_import[key]),
            lambda: CAPABILITY_EPG_IMPORT in box.capabilities,
            # Never removed, for the reason the softcam sensor above gives: a capability
            # that goes quiet is not a decision, and the history of a diagnostic is not
            # the price of a downgrade or a receiver that has not answered yet.
            lambda: False,
            async_add_entities,
        ).start()
    )

    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "sensor",
            (KEY_EPG_ACTIVE_BOUQUET,),
            lambda _key: Enigma2EpgActiveBouquetSensor(box),
            lambda: EPG_ACTIVE_BOUQUET_CAPABILITIES.issubset(box.capabilities),
            # Never removed, for the reason the softcam sensor above gives: two
            # capabilities that go quiet are an older plugin or a box that has not
            # answered, and a household's dashboard card and history are not the price
            # of either. A box that stops building grids leaves this sensor saying
            # nothing new, which is the truth.
            lambda: False,
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


class Enigma2EpgActiveBouquetSensor(Enigma2Entity, SensorEntity):
    """What is on now and next across the bouquet the receiver is walking.

    The plugin publishes one retained grid per configured bouquet and, separately, which
    bouquet the receiver's channel ± is in. This joins the two: it reads the grid whose
    `bouquet` names the active context, so switching bouquets — on the remote or through
    „Bukiet" — switches what it shows, and a grid for any other bouquet leaves it alone.
    The grid's `bouquet` field is matched rather than its topic's slug, because the
    contract says the slug is only an address and the payload carries the name.

    **The state is how many channels have something to show**, and it keeps three
    situations apart that a single number would merge. No active bouquet at all — the
    radio list, the movie list — is `0` with an empty list: the receiver is not in a
    bouquet, so there is nothing to count. An active bouquet whose grid has not arrived,
    was retracted, or was never configured on the box is `unknown` with an empty list:
    that is „no grid", and reporting it as „a grid with nothing in it" would tell an
    automation the EPG is empty when nobody has looked. Only a grid that is here can say
    zero about itself.

    **`now` and `next` follow the clock.** The plugin republishes a grid only when its
    content changes, so between two publishes the programme that was on can end without
    any message saying so. Each read therefore picks `now` and `next` against the current
    time, and arms one timer for the earliest moment any channel's answer changes: the end
    of a programme on air, or the start of one on a channel that is between programmes.
    The timer is re-armed on every read and cancelled when the entity goes, so there is
    only ever one.

    🔴 **`channels` is declared unrecorded, and that is not optional.** Home Assistant
    excludes `source_list` and a select's `options` on its own; this is an attribute of
    ours on an ordinary sensor, so it is written to the database on every change unless
    it is named here. A bouquet of two hundred channels is tens of kilobytes, rewritten
    every time a programme ends somewhere in it.
    """

    _unrecorded_attributes = frozenset({"channels"})

    def __init__(self, box: Enigma2Box) -> None:
        """Follow the context and every grid; available once the context has arrived."""
        super().__init__(
            box,
            KEY_EPG_ACTIVE_BOUQUET,
            topics=(TOPIC_BOUQUET, TOPIC_EPG_GRID),
            requires=TOPIC_BOUQUET,
        )
        self._attr_native_value = None
        self._attr_extra_state_attributes = {"channels": []}
        self._cancel_clock: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Start listening, and make sure a pending timer dies with the entity."""
        self.async_on_remove(self._disarm)
        await super().async_added_to_hass()

    @callback
    def _disarm(self) -> None:
        """Cancel the timer for the next programme change, if one is armed."""
        if self._cancel_clock is not None:
            self._cancel_clock()
            self._cancel_clock = None

    def _active_grid(self) -> tuple[bool, dict[str, Any] | None]:
        """Return whether the receiver is in a bouquet, and that bouquet's grid.

        Both fields of the context being null is ordinary operation and means „not in a
        bouquet"; a context without a readable name is read the same way, since there
        is nothing to match a grid against.
        """
        name = (self.box.state.bouquet or {}).get("name")
        if not isinstance(name, str) or not name:
            return False, None
        for grid in self.box.state.epg_grid.values():
            if isinstance(grid, dict) and grid.get("bouquet") == name:
                return True, grid
        return True, None

    @callback
    def _async_read_state(self) -> None:
        """Rebuild the list against the current time, and arm the next change."""
        self._disarm()
        in_bouquet, grid = self._active_grid()
        if not in_bouquet:
            self._attr_native_value = 0
            self._attr_extra_state_attributes = {"channels": []}
            return
        grid_channels = (grid or {}).get("channels")
        if not isinstance(grid_channels, list):
            self._attr_native_value = None
            self._attr_extra_state_attributes = {"channels": []}
            return

        now = dt_util.utcnow().timestamp()
        channels: list[dict[str, Any]] = []
        with_data = 0
        wake: int | None = None
        for channel in grid_channels:
            if not isinstance(channel, dict):
                continue
            current, following = _now_and_next(_grid_events(channel), now)
            if current is not None or following is not None:
                with_data += 1
            # The moment this channel's answer changes: its programme ends, or — on a
            # channel between programmes — the next one starts.
            change = (
                current["end"]
                if current is not None
                else following["begin"]
                if following is not None
                else None
            )
            if change is not None and (wake is None or change < wake):
                wake = change
            name = channel.get("name")
            sref = channel.get("sref")
            channels.append(
                {
                    "name": name if isinstance(name, str) else None,
                    "sref": sref if isinstance(sref, str) else None,
                    "now": current,
                    "next": following,
                }
            )
        self._attr_native_value = with_data
        self._attr_extra_state_attributes = {"channels": channels}
        if wake is not None:
            self._cancel_clock = async_track_point_in_utc_time(
                self.hass, self._clock_moved, dt_util.utc_from_timestamp(wake)
            )

    @callback
    def _clock_moved(self, _now: datetime) -> None:
        """A programme ended or began somewhere in the bouquet: read again."""
        self._cancel_clock = None
        self._async_read_state()
        self.async_write_ha_state()


class Enigma2LastErrorSensor(Enigma2Entity, RestoreEntity, SensorEntity):
    """The last thing the receiver refused to do, and what it said about it.

    The integration owns this memory rather than the topic. The plugin clears
    `last_error` on the next command that succeeds, and the person who wants to know why
    „Restart" did nothing is looking afterwards — usually after the next volume step has
    already wiped the evidence. So a cleared topic leaves this sensor alone.

    Two things follow from that, and both of them are about the receiver being gone.

    **The time is the receiver's own `ts`** whenever the payload carries one, not the
    moment this integration read it. The retained complaint is replayed on every
    reconnect and again at every start-up, and a time taken from the clock moves each
    time — so the one number that says when the receiver refused would drift forward
    for as long as nobody pressed anything else. With a `ts` in hand a replay writes
    exactly what is already there and Home Assistant drops it, and two refusals that
    differ only in when they happened are still two refusals: the sentence a permission
    error carries is the same every time, so the text cannot tell them apart and is not
    asked to.

    **It is available even when the receiver is not.** Deep standby is the headline
    case and it is exactly a box that has left the network: an entity that went
    unavailable with the box would lose the explanation at the moment it was wanted, and
    a restart taken while the box was away would store `unavailable` and lose it for
    good.
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

    @property
    def available(self) -> bool:
        """Return True, always: the complaint outlives the box that made it."""
        return True

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
        command = command[:ERROR_TEXT_MAX]
        error = str(payload.get("error") or "")[:ERROR_TEXT_MAX] or None
        when = _refused_at(payload.get("ts"))
        if (
            when is None
            and self.box.state.last_error_retained
            and command == self._attr_native_value
            and error == self._attr_extra_state_attributes.get("error")
        ):
            # A replay of what is already shown, by a payload that gives no time of its
            # own: taking it would move the time to now, and a reconnect would do it
            # again. The text alone only settles this when there is no `ts` — a
            # permission refusal says the same sentence every time, so the second one,
            # made while Home Assistant was down, is byte-identical to the first except
            # for the moment it happened.
            return
        self._attr_native_value = command
        self._attr_extra_state_attributes = {
            "error": error,
            # Writing the same `ts` again writes the same state and the same
            # attributes, which Home Assistant drops: a replay costs nothing and
            # changes nothing, without this having to recognise it.
            "time": when or dt_util.utcnow().isoformat(),
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

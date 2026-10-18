"""The actions, and the mixin that carries them.

Everything the entities do is also available as an action, plus the things no entity
shape fits: adding a recording timer, deleting one, reading the EPG grid.

They are **entity actions on the media player**, which is what lets a target be a
device, an area or an entity without a line of code here resolving any of it - Home
Assistant expands a device target to the entities of the platform an action was
registered on, and one box has exactly one media player.

Every action that changes something waits for proof. The contract has no
acknowledgement topic, so the proof is the state topic the command moves and the
disproof is `last_error`; `Enigma2Box.async_command` waits for whichever comes first
and raises with the box's own words. Two commands move nothing - `send_key` and
`message` - and for those the only available answer is silence, so they wait just long
enough to hear a complaint and then report success.

🔴 `get_epg_grid` is the one action that returns data, and it is deliberately not a
state attribute anywhere: a grid is tens of kilobytes per bouquet and putting it on an
entity would write all of it into the recorder database every quarter of an hour.

`show_list` returns data when asked to: how a list of choices on the television ended. It
waits for the viewer, up to the list's own timeout and a little more, so it is the one
action that can take minutes; without a response it returns once the list is published
(`choice_list.py`).
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, ENTITY_MATCH_ALL
from homeassistant.core import ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    entity_platform,
    target as target_helpers,
)
import homeassistant.util.dt as dt_util
import voluptuous as vol

from .box import Enigma2Box, Enigma2CommandError, normalise_key, same_service
from .choice_list import (
    ATTR_DEFAULT,
    ATTR_ROWS,
    ATTR_TITLE,
    async_refused_response,
    build_list,
    check_can_show,
)
from .const import (
    CAPABILITY_RECORDING_PLAY,
    CAPABILITY_RECORDINGS,
    CAPABILITY_STREAM_STOP,
    DELETE_RECORDING_REASONS,
    DOMAIN,
    HA_MODES,
    MESSAGE_STYLE_POPUP,
    MESSAGE_STYLE_TOAST,
    MESSAGE_STYLES,
    MESSAGE_TYPES,
    PLAY_RECORDING_REASONS,
    PLAY_RECORDING_TIMESHIFT_TIMEOUT,
    RECORD_ACTIONS,
    RECORDING_DELETE_TIMEOUT,
    STREAM_STOP_FAILURES,
    STREAM_STOP_REASONS,
    TOPIC_BOUQUET,
    TOPIC_EPG_GRID,
    TOPIC_SCREEN,
    TOPIC_TIMERS,
)
from .notify import check_toast, message_payload
from .recordings import active_recording_ids, library_response, parse_recording_id, plays

SERVICE_ZAP = "zap"
SERVICE_SELECT_BOUQUET = "select_bouquet"
SERVICE_SEND_KEY = "send_key"
SERVICE_MESSAGE = "message"
SERVICE_ADD_TIMER = "add_timer"
SERVICE_DELETE_TIMER = "delete_timer"
SERVICE_RECORD = "record"
SERVICE_SCREENSHOT = "screenshot"
SERVICE_SET_HA_MODE = "set_ha_mode"
SERVICE_GET_EPG_GRID = "get_epg_grid"
SERVICE_STOP_STREAM = "stop_stream"
SERVICE_DELETE_RECORDING = "delete_recording"
SERVICE_GET_RECORDINGS = "get_recordings"
SERVICE_SHOW_LIST = "show_list"

ATTR_SREF = "sref"
ATTR_NAME = "name"
ATTR_KEY = "key"
ATTR_LONG = "long"
ATTR_TEXT = "text"
ATTR_TYPE = "type"
ATTR_STYLE = "style"
ATTR_TIMEOUT = "timeout"
ATTR_EVENT_ID = "event_id"
ATTR_BEGIN = "begin"
ATTR_END = "end"
ATTR_ACTION = "action"
ATTR_MODE = "mode"
ATTR_BOUQUET = "bouquet"
ATTR_HOST = "host"
ATTR_ID = "id"

# Epoch seconds, or anything Home Assistant can read as a moment in time. An automation
# built in the UI hands over a `datetime`; one written by hand is as likely to have the
# integer the topics use.
TIMESTAMP = vol.Any(cv.datetime, cv.positive_int)

ZAP_SCHEMA = {
    vol.Optional(ATTR_SREF): cv.string,
    vol.Optional(ATTR_NAME): cv.string,
}
SELECT_BOUQUET_SCHEMA = {vol.Required(ATTR_SREF): cv.string}
SEND_KEY_SCHEMA = {
    vol.Required(ATTR_KEY): cv.string,
    vol.Optional(ATTR_LONG, default=False): cv.boolean,
}
# 🔴 `timeout` has no default here. The default belongs to the style - ten seconds for a
# popup, five for a toast - and a schema default of ten would reach the handler as if the
# caller had asked for it, giving every toast without a timeout a popup's ten seconds.
MESSAGE_SCHEMA = {
    vol.Required(ATTR_TEXT): cv.string,
    vol.Optional(ATTR_TYPE, default=MESSAGE_TYPES[0]): vol.In(MESSAGE_TYPES),
    vol.Optional(ATTR_STYLE, default=MESSAGE_STYLE_POPUP): vol.In(MESSAGE_STYLES),
    vol.Optional(ATTR_TIMEOUT): cv.positive_int,
}
ADD_TIMER_SCHEMA = {
    vol.Required(ATTR_SREF): cv.string,
    vol.Optional(ATTR_EVENT_ID): cv.positive_int,
    vol.Optional(ATTR_BEGIN): TIMESTAMP,
    vol.Optional(ATTR_END): TIMESTAMP,
    vol.Optional(ATTR_NAME): cv.string,
}
DELETE_TIMER_SCHEMA = {
    vol.Required(ATTR_SREF): cv.string,
    vol.Required(ATTR_BEGIN): TIMESTAMP,
    vol.Required(ATTR_END): TIMESTAMP,
}
RECORD_SCHEMA = {vol.Required(ATTR_ACTION): vol.In(RECORD_ACTIONS)}
SET_HA_MODE_SCHEMA = {vol.Required(ATTR_MODE): vol.In(HA_MODES)}
GET_EPG_GRID_SCHEMA = {vol.Optional(ATTR_BOUQUET): cv.string}
STOP_STREAM_SCHEMA = {
    vol.Required(ATTR_HOST): cv.string,
    vol.Optional(ATTR_SREF): cv.string,
}
DELETE_RECORDING_SCHEMA = {vol.Required(ATTR_ID): cv.string}
# Every field is taken as it comes and checked by `choice_list.build_list`, which says what
# is wrong in the household's language and names the row; a schema error would say it in
# English, without the row. Only a field the action does not have - a misspelt `titel` -
# is refused by the schema, with voluptuous' English message.
SHOW_LIST_SCHEMA = {
    vol.Optional(ATTR_TITLE): object,
    vol.Optional(ATTR_TEXT): object,
    vol.Optional(ATTR_ROWS): object,
    vol.Optional(ATTR_TIMEOUT): object,
    vol.Optional(ATTR_DEFAULT): object,
}


def _epoch(value: datetime | int) -> int:
    """Return a moment in time as the epoch seconds every topic here speaks in."""
    if isinstance(value, datetime):
        return int(dt_util.as_utc(value).timestamp())
    return int(value)


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    """Return a translated complaint about how an action was called."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


async def async_select_bouquet(box: Enigma2Box, sref: str) -> None:
    """Activate a published bouquet and wait for a fresh context readback."""
    if "bouquet_context" not in box.capabilities:
        raise _invalid("bouquet_context_unsupported")
    if box.bouquet_by_sref(sref) is None:
        raise _invalid("bouquet_not_published", bouquet=sref)
    before = box.updates.get(TOPIC_BOUQUET, 0)
    await box.async_command(
        "bouquet",
        json.dumps({"sref": sref}),
        effect=lambda: (
            box.updates.get(TOPIC_BOUQUET, 0) > before
            and same_service((box.state.bouquet or {}).get("sref"), sref)
        ),
    )


async def async_stop_stream(box: Enigma2Box, host: str, sref: str | None = None) -> None:
    """End one stream client's stream, named by its host and the service it is sent.

    The receiver's command takes both and nothing else, so nothing here ever stops "all
    of them". Without a reference the host has to have one stream open - or one whose
    channel the options hide, which is the stream meant then, because its reference is
    shown nowhere and the box supplies it (`Enigma2Box.stream_client_references`). Two
    hidden services on one host cannot be told apart by anybody, and are refused with a
    sentence of their own.

    The command changes no state topic at once: the image answers and the connection
    ends a moment later, and a player that reconnects by itself is back on the list by
    the time it is read again. So the wait is only for a complaint, as for a key press.
    `not_connected` is a refusal and `not_stopped` a failure, each with its own
    sentence.
    """
    if CAPABILITY_STREAM_STOP not in box.capabilities:
        raise _invalid("stream_stop_unsupported")
    host = host.strip()
    sref = (sref or "").strip() or None
    # An offline receiver lists nobody - its retained list is of clients that have gone -
    # and a command sent to it now is answered by nobody, which this path would report
    # as done. An empty address names no client; the receiver would refuse it in English.
    if not host or not box.available:
        raise _invalid("stream_client_unknown", host=host)
    if not sref:
        references, hidden = box.stream_client_references(host)
        if not references:
            raise _invalid("stream_client_unknown", host=host)
        if len(references) > 1:
            # Two hidden services have no reference anybody could give, so asking for
            # one would be asking the impossible: they are stopped on the receiver, or
            # shown again first. Never all of them at once.
            raise _invalid(
                "stream_client_hidden_ambiguous" if hidden else "stream_client_ambiguous",
                host=host,
            )
        sref = references[0]
    await box.async_command(
        "stream_stop",
        json.dumps({"host": host, "sref": sref}),
        reasons=STREAM_STOP_REASONS,
        failures=STREAM_STOP_FAILURES,
    )


def _listed_recording(box: Enigma2Box, value: Any) -> tuple[str, Any]:
    """Return a recording id the receiver's library lists now, and where it is listed.

    Refused with a sentence for an id that cannot be one, and for one the library Home
    Assistant holds does not list - a recording deleted since, one of a folder the
    receiver cut to fit one packet, or one of another receiver. Nothing from here ever
    names a file: the receiver is sent the id and finds the file in its own library.
    """
    rec_id = parse_recording_id(value)
    if rec_id is None:
        raise _invalid("recording_id_invalid")
    if (found := box.find_recording(rec_id)) is None:
        raise _invalid("recording_unknown", id=rec_id)
    return rec_id, found


async def async_play_recording(box: Enigma2Box, value: Any) -> None:
    """Play one recording of the library on the receiver, as its movie list would.

    The image's own movie player plays it, and the receiver's own rule decides whether it
    resumes where it was left, asks, or starts over. From standby the receiver wakes first,
    as for a zap; with a timeshift buffer running it first asks the viewer whether to leave
    timeshift, and one who keeps it ends the command with a refusal. The proof is `service`
    naming the recording's file - as for a zap, a recording that is already playing is
    proved at once, and the command still goes out. A refusal is the receiver's, and the
    codes this integration knows are said in the household's language.
    """
    if CAPABILITY_RECORDING_PLAY not in box.capabilities:
        raise _invalid("recording_play_unsupported")
    rec_id, (location, _folder, recording) = _listed_recording(box, value)
    if not box.available:
        raise _invalid("recording_receiver_offline")
    # With a timeshift buffer running the receiver asks the viewer first, as its movie list
    # does, and the answer - the recording playing, or `timeshift_kept` - comes when somebody
    # has pressed a key.
    timeshift = (box.state.timeshift or {}).get("active") is True
    await box.async_command(
        "play_recording",
        json.dumps({"id": rec_id}),
        effect=lambda: plays(box.state.service, location, recording),
        reasons=PLAY_RECORDING_REASONS,
        **({"timeout": PLAY_RECORDING_TIMESHIFT_TIMEOUT} if timeshift else {}),
    )


async def async_delete_recording(box: Enigma2Box, value: Any) -> None:
    """Move one recording of the library into the receiver's trash, and see it gone.

    Asked only of a receiver that claims `recording_delete`, which it does only while its
    own permission is on and its trash is there; otherwise refused here, with the reason
    its `info` gives. Nothing is ever deleted for good: the receiver moves the recording,
    and its companion files, into the image's trash. A recording that is being recorded is
    refused here too, as the receiver would refuse it; one that is being played is refused
    by the receiver. The proof is the library, read again by the receiver, no longer
    listing the id, within 90 seconds - longer than the receiver's own bound on the move, so
    that its refusal arrives before this gives up.
    """
    if (refusal := box.recording_delete_refusal()) is not None:
        raise _invalid(refusal)
    rec_id, _found = _listed_recording(box, value)
    if not box.available:
        raise _invalid("recording_receiver_offline")
    if rec_id in active_recording_ids(box.state.recording):
        raise _invalid("delete_recording_in_use")
    await box.async_command(
        "delete_recording",
        json.dumps({"id": rec_id}),
        effect=lambda: box.find_recording(rec_id) is None,
        timeout=RECORDING_DELETE_TIMEOUT,
        reasons=DELETE_RECORDING_REASONS,
    )


class Enigma2Actions:
    """The action handlers, mixed into the media player they are registered on.

    They live here rather than on the entity because none of them is about being a
    media player; the entity is only the target Home Assistant resolves to.
    """

    box: Enigma2Box

    async def async_zap(
        self, sref: str | None = None, name: str | None = None
    ) -> None:
        """Tune to a service, by reference or by name."""
        if bool(sref) == bool(name):
            raise _invalid("zap_needs_one")

        box = self.box
        if sref:
            await box.async_command(
                "zap",
                sref,
                # By identity, not by string: the receiver answers on `service` with
                # its own spelling of the reference - a trailing colon, a stream URL,
                # a different case - and a raw comparison reports a zap that plainly
                # happened as a timeout.
                effect=lambda: same_service((box.state.service or {}).get("sref"), sref),
            )
            return
        await box.async_command(
            "zap",
            json.dumps({"name": name}),
            effect=lambda: (box.state.service or {}).get("name") == name,
        )

    async def async_select_bouquet(self, sref: str) -> None:
        """Make one published bouquet the active channel-up/down context."""
        await async_select_bouquet(self.box, sref)

    async def async_send_key(self, key: str, long: bool = False) -> None:
        """Inject one remote key.

        A key press changes no state topic, so there is nothing to wait for beyond the
        box having a chance to refuse the name.
        """
        await self.box.async_command(
            "key", json.dumps({"key": normalise_key(key), "long": long})
        )

    async def async_message(
        self,
        text: str,
        # Named after the action's field, which is what Home Assistant passes.
        type: str = MESSAGE_TYPES[0],  # noqa: A002
        style: str = MESSAGE_STYLE_POPUP,
        timeout: int | None = None,
    ) -> None:
        """Put a popup or a toast on the television.

        The `style` field exists on every receiver, because an action's fields are the
        same for the whole domain and cannot appear per box. A toast aimed at a receiver
        that has not named the `toast` capability is therefore refused here, before
        anything is published, the way `select_bouquet` refuses a box without
        `bouquet_context`.
        """
        if style == MESSAGE_STYLE_TOAST:
            check_toast(self.box, timeout)
        await self.box.async_command(
            "message", message_payload(text, type, timeout, style)
        )

    async def async_add_timer(
        self,
        sref: str,
        event_id: int | None = None,
        begin: datetime | int | None = None,
        end: datetime | int | None = None,
        name: str | None = None,
    ) -> None:
        """Add a recording timer, from an EPG event or from a window.

        The event form is the one to prefer: enigma2 resolves the event itself, so the
        timer inherits the programme's name and the padding the box is configured with.
        The window form is for recording something the EPG does not know about.
        """
        if event_id is None and (begin is None or end is None):
            raise _invalid("timer_needs_event_or_window")
        if event_id is not None and (begin is not None or end is not None):
            raise _invalid("timer_takes_one_form")

        payload: dict[str, Any] = {"action": "add", "sref": sref}
        if event_id is not None:
            payload["event_id"] = event_id
        else:
            payload["begin"] = _epoch(begin)  # type: ignore[arg-type]
            payload["end"] = _epoch(end)  # type: ignore[arg-type]
            payload["name"] = name or ""

        await self._async_timer_command(payload)

    async def async_delete_timer(
        self, sref: str, begin: datetime | int, end: datetime | int
    ) -> None:
        """Delete a recording timer.

        enigma2 identifies a timer by the triple service, start and end, so those three
        are what deletion takes - there is no id to hold on to.
        """
        await self._async_timer_command(
            {
                "action": "delete",
                "sref": sref,
                "begin": _epoch(begin),
                "end": _epoch(end),
            }
        )

    async def _async_timer_command(self, payload: dict[str, Any]) -> None:
        """Send a timer command and wait for the timer list to be republished."""
        box = self.box
        before = box.updates.get(TOPIC_TIMERS, 0)
        await box.async_command(
            "timer",
            json.dumps(payload),
            effect=lambda: box.updates.get(TOPIC_TIMERS, 0) > before,
        )

    async def async_record(self, action: str) -> None:
        """Start or stop an instant recording of the current service."""
        box = self.box
        want_recording = action == "start"

        def _recording() -> bool:
            active = (box.state.recording or {}).get("active")
            return bool(active) if isinstance(active, list) else False

        await box.async_command(
            "record", action, effect=lambda: _recording() is want_recording
        )

    async def async_screenshot(self) -> None:
        """Capture the screen now, and wait for the picture."""
        box = self.box
        before = box.updates.get(TOPIC_SCREEN, 0)
        await box.async_command(
            "screenshot",
            "PRESS",
            effect=lambda: box.updates.get(TOPIC_SCREEN, 0) > before,
        )

    async def async_set_ha_mode(self, mode: str) -> None:
        """Switch how the box presents itself to Home Assistant.

        This one has an acknowledgement of its own: the plugin echoes the new mode on
        `info`, and the box helper already knows how to wait for it.
        """
        if not await self.box.async_request_ha_mode(mode):
            raise Enigma2CommandError(
                translation_domain=DOMAIN,
                translation_key="command_timeout",
                translation_placeholders={"command": "ha_mode"},
            )

    async def async_stop_stream(self, host: str, sref: str | None = None) -> None:
        """End one stream the receiver is serving to another device."""
        await async_stop_stream(self.box, host, sref)

    async def async_delete_recording(self, id: str) -> None:  # noqa: A002
        """Move one recording into the receiver's trash, by its id."""
        await async_delete_recording(self.box, id)

    async def async_get_recordings(self) -> ServiceResponse:
        """Return the recording library as Home Assistant shows it.

        From the retained topics the integration already holds: nothing is asked of the
        receiver, and no disk is woken. The hidden-bouquets option applies exactly as in
        the media browser - a hidden recording is left out, and a folder or a location with
        nothing left to show with it. Before the library has arrived the answer says so:
        `read` is null and there are no locations. Like `get_epg_grid`, it is an answer and
        not a state attribute: a library is far too large for the recorder.
        """
        box = self.box
        if CAPABILITY_RECORDINGS not in box.capabilities:
            raise _invalid("recordings_unsupported")
        return library_response(box.state.recordings_index, box.visible_library())

    async def async_get_epg_grid(self, bouquet: str | None = None) -> ServiceResponse:
        """Return what is on, per bouquet, from the retained grid topics.

        The grids are published by the box and refreshed on their own; this asks for a
        rebuild only when there is nothing there at all, which is the case on a box
        whose `epg_grid_events` setting is zero - and that one never answers, so it
        fails with a timeout rather than an empty result that looks like "nothing is
        on".
        """
        box = self.box
        if not box.state.epg_grid:
            before = box.updates.get(TOPIC_EPG_GRID, 0)
            await box.async_command(
                "epg_grid",
                "PRESS",
                effect=lambda: box.updates.get(TOPIC_EPG_GRID, 0) > before,
            )

        grids = [
            {"slug": slug, **grid} for slug, grid in sorted(box.state.epg_grid.items())
        ]
        if bouquet:
            grids = [
                grid
                for grid in grids
                if bouquet in (grid.get("bouquet"), grid.get("slug"))
            ]
            if not grids:
                raise _invalid("unknown_bouquet", bouquet=bouquet)
        return {"bouquets": grids}


def _target_count(entity: Enigma2Actions, call: ServiceCall) -> int:
    """How many receivers one call of the action reaches, as Home Assistant resolves it.

    The receivers' media players, among those the call names - by entity, device, area,
    floor or label - or all of them for `all`, as `entity_service_call` picks them.

    It does not ask Home Assistant's per-user entity permissions, which that resolution
    applies: for `all`, a user who is not an administrator is served only the receivers they
    may control. So such a user allowed one of two receivers is counted two targets, and that
    one receiver's refusal comes back as its entry rather than raised. A target that names a
    receiver the user may not control is refused by Home Assistant before this runs.
    """
    platform = getattr(entity, "platform", None)
    entities = platform.domain_platform_entities if platform is not None else {}
    if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
        return sum(1 for candidate in entities.values() if candidate.available)
    selected = target_helpers.async_extract_referenced_entity_ids(
        call.hass, target_helpers.TargetSelection(call.data), True
    )
    named = selected.referenced | selected.indirectly_referenced
    return sum(
        1
        for entity_id, candidate in entities.items()
        if entity_id in named and candidate.available
    )


async def async_show_list(entity: Enigma2Actions, call: ServiceCall) -> ServiceResponse:
    """Put a list of choices on the television; with a response, wait for how it ended.

    Called with the whole service call, not its fields, because whether the caller asked
    for a response decides what this does: with one it returns `ending`, `row` and `label`
    once the list is answered - or `no_answer` fifteen seconds after its timeout - and
    without one it returns as soon as the list is published.

    Aimed at several receivers with a response, a receiver that refuses the list or cannot
    be reached answers in its own entry (`async_refused_response`) instead of raising:
    Home Assistant raises the first exception of an entity action's calls and drops every
    other receiver's answer with it. A mistake in the fields is the same for every receiver
    and is raised either way; so is everything aimed at one receiver.
    """
    box = entity.box
    several = call.return_response and _target_count(entity, call) > 1
    try:
        check_can_show(box)
    except HomeAssistantError as error:
        if not several:
            raise
        return await async_refused_response(call.hass, error)
    request = build_list(call.data)
    if box.lists is None:  # pragma: no cover - set before the platforms are
        raise _invalid("list_unsupported")
    try:
        return await box.lists.async_show(request, wait=call.return_response)
    except HomeAssistantError as error:
        if not several:
            raise
        return await async_refused_response(call.hass, error)


def async_setup_services() -> None:
    """Register every action on the media player platform that is being set up.

    Called from the media player's own setup, because an entity action has to be
    registered on a platform. Registering twice is a no-op, so a second receiver does
    not have to be special-cased.
    """
    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(SERVICE_ZAP, ZAP_SCHEMA, "async_zap")
    platform.async_register_entity_service(
        SERVICE_SELECT_BOUQUET,
        SELECT_BOUQUET_SCHEMA,
        "async_select_bouquet",
    )
    platform.async_register_entity_service(
        SERVICE_SEND_KEY, SEND_KEY_SCHEMA, "async_send_key"
    )
    platform.async_register_entity_service(
        SERVICE_MESSAGE, MESSAGE_SCHEMA, "async_message"
    )
    platform.async_register_entity_service(
        SERVICE_ADD_TIMER, ADD_TIMER_SCHEMA, "async_add_timer"
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_TIMER, DELETE_TIMER_SCHEMA, "async_delete_timer"
    )
    platform.async_register_entity_service(
        SERVICE_RECORD, RECORD_SCHEMA, "async_record"
    )
    platform.async_register_entity_service(SERVICE_SCREENSHOT, None, "async_screenshot")
    platform.async_register_entity_service(
        SERVICE_SET_HA_MODE, SET_HA_MODE_SCHEMA, "async_set_ha_mode"
    )
    platform.async_register_entity_service(
        SERVICE_STOP_STREAM, STOP_STREAM_SCHEMA, "async_stop_stream"
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_RECORDING, DELETE_RECORDING_SCHEMA, "async_delete_recording"
    )
    platform.async_register_entity_service(
        SERVICE_GET_RECORDINGS,
        None,
        "async_get_recordings",
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_GET_EPG_GRID,
        GET_EPG_GRID_SCHEMA,
        "async_get_epg_grid",
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_SHOW_LIST,
        SHOW_LIST_SCHEMA,
        async_show_list,
        supports_response=SupportsResponse.OPTIONAL,
    )

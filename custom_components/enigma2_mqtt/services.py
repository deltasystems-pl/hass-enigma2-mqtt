"""The actions, and the mixin that carries them.

Everything the entities do is also available as an action, plus the things no entity
shape fits: adding a recording timer, deleting one, reading the EPG grid.

They are **entity actions on the media player**, which is what lets a target be a
device, an area or an entity without a line of code here resolving any of it — Home
Assistant expands a device target to the entities of the platform an action was
registered on, and one box has exactly one media player.

Every action that changes something waits for proof. The contract has no
acknowledgement topic, so the proof is the state topic the command moves and the
disproof is `last_error`; `Enigma2Box.async_command` waits for whichever comes first
and raises with the box's own words. Two commands move nothing — `send_key` and
`message` — and for those the only available answer is silence, so they wait just long
enough to hear a complaint and then report success.

🔴 `get_epg_grid` is the one action that returns data, and it is deliberately not a
state attribute anywhere: a grid is tens of kilobytes per bouquet and putting it on an
entity would write all of it into the recorder database every quarter of an hour.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from homeassistant.core import ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
import homeassistant.util.dt as dt_util
import voluptuous as vol

from .box import Enigma2Box, Enigma2CommandError, normalise_key
from .const import (
    DOMAIN,
    HA_MODES,
    MESSAGE_DEFAULT_TIMEOUT,
    MESSAGE_TYPES,
    RECORD_ACTIONS,
    TOPIC_BOUQUET,
    TOPIC_EPG_GRID,
    TOPIC_SCREEN,
    TOPIC_TIMERS,
)
from .notify import message_payload

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

ATTR_SREF = "sref"
ATTR_NAME = "name"
ATTR_KEY = "key"
ATTR_LONG = "long"
ATTR_TEXT = "text"
ATTR_TYPE = "type"
ATTR_TIMEOUT = "timeout"
ATTR_EVENT_ID = "event_id"
ATTR_BEGIN = "begin"
ATTR_END = "end"
ATTR_ACTION = "action"
ATTR_MODE = "mode"
ATTR_BOUQUET = "bouquet"

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
MESSAGE_SCHEMA = {
    vol.Required(ATTR_TEXT): cv.string,
    vol.Optional(ATTR_TYPE, default=MESSAGE_TYPES[0]): vol.In(MESSAGE_TYPES),
    vol.Optional(ATTR_TIMEOUT, default=MESSAGE_DEFAULT_TIMEOUT): cv.positive_int,
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
            and (box.state.bouquet or {}).get("sref") == sref
        ),
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
                effect=lambda: (box.state.service or {}).get("sref") == sref,
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
        timeout: int = MESSAGE_DEFAULT_TIMEOUT,
    ) -> None:
        """Put a popup on the television."""
        await self.box.async_command("message", message_payload(text, type, timeout))

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
        are what deletion takes — there is no id to hold on to.
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

    async def async_get_epg_grid(self, bouquet: str | None = None) -> ServiceResponse:
        """Return what is on, per bouquet, from the retained grid topics.

        The grids are published by the box and refreshed on their own; this asks for a
        rebuild only when there is nothing there at all, which is the case on a box
        whose `epg_grid_events` setting is zero — and that one never answers, so it
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
        SERVICE_GET_EPG_GRID,
        GET_EPG_GRID_SCHEMA,
        "async_get_epg_grid",
        supports_response=SupportsResponse.ONLY,
    )

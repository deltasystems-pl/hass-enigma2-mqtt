"""The receiver menu: what Home Assistant publishes, and what it does on a viewer's OK.

A plugin after 0.4.0 can show a "Home Assistant" menu on the television: one level of
groups, each a list of rows, every row bound to an entity, a scene or a script that the user
picked in this integration's options. The receiver never learns what a row does. It shows
the rows this module publishes on `enigma2mqtt/menu/<node_id>` and answers an OK with one
`<base>/<node_id>/menu_action` carrying a row id and, for a control that has one, a value.
Everything else happens here: which service a row calls, what values it may be given, and
whether the receiver may act at all (ADR-0012).

`menu_action` is untrusted input. The broker's access control is not a boundary anybody can
rely on, so anyone who can publish can send one. The only power it carries is what the user
put into the menu: an action names a row the current options hold, with the action that
row's control takes, and a value inside what the bound entity accepts now - anything else is
refused with a log line and nothing is called.

A row's id is generated once, when the row is added in the options, and stored with it. It
changes only when the row's binding does - another entity, or another control - so an
action aimed at a binding that no longer exists names a row the menu no longer holds and is
refused, whatever `rev` it carries.

A row stores the entity's registry id, so it follows the entity through a rename; an entity
that has no registry entry is stored by its entity id. Both forms are read wherever a row is.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import json
import logging
import math
import re
import secrets
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
    valid_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .box import Enigma2Box, Enigma2MqttConfigEntry, decode_payload
from .const import (
    ATTR_NODE_ID,
    CAPABILITY_MENU,
    CONF_MENU,
    DOMAIN,
    EVENT_MENU_ACTION,
    MENU_ICON_NEUTRAL,
    MENU_ICONS,
    TOPIC_AVAILABILITY,
    TOPIC_MENU_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

# The payload's version, and what the menu is called on the receiver's header.
MENU_VERSION = 1
MENU_TITLE = "Home Assistant"

# The contract's bounds (ADR-0012): eight groups, twenty-four entries in the top list and
# in every group, labels at 60 characters, state texts at 24, a selector's options at 24 of
# 60 characters each, a unit at 8, at most 200 steps on a slider, and a text field of at most
# 255 characters.
MAX_GROUPS = 8
MAX_ENTRIES = 24
MAX_LABEL = 60
MAX_STATE = 24
MAX_OPTIONS = 24
MAX_OPTION = 60
MAX_UNIT = 8
MAX_STEPS = 200
MAX_INPUT = 255
# The whole payload stays under 64 KiB; what does not fit is dropped from the end.
MAX_PAYLOAD_BYTES = 64 * 1024
# At most one publish every half second, and the first change after a quiet spell at once -
# which is how "at least one within a second of a change" is kept.
PUBLISH_COOLDOWN = 0.5
# An action is a few short fields and at most 255 characters of text; anything much larger
# is not one, and is not parsed.
ACTION_MAX_BYTES = 4096
# How many action ids are remembered to drop a redelivered message.
ACTION_IDS_REMEMBERED = 64
# Seconds an action may be old, by its `ts` and this Home Assistant's clock (ADR-0012).
# The plugin never queues an action across a disconnect; one older than this was pressed for
# a screen that is long gone. An action without `ts` is accepted.
MAX_ACTION_AGE = 120

# The six controls of the contract, and the action the receiver sends for each.
BUTTON = "button"
TOGGLE = "toggle"
INDICATOR = "indicator"
SELECTOR = "selector"
SLIDER = "slider"
INPUT = "input"
CONTROLS = (BUTTON, TOGGLE, INDICATOR, SELECTOR, SLIDER, INPUT)
ACTION_PRESS = "press"
ACTION_TOGGLE = "toggle"
ACTION_SET = "set"
ACTION_FOR_CONTROL: dict[str, str] = {
    BUTTON: ACTION_PRESS,
    TOGGLE: ACTION_TOGGLE,
    SELECTOR: ACTION_SET,
    SLIDER: ACTION_SET,
    INPUT: ACTION_SET,
}

# The domains a row may be bound to, and the controls each can be shown as - the first is
# the one a new row gets unless the user chooses another (ADR-0012). Any entity can be a
# plain indicator showing its state - a domain not in this table is nothing
# else. What the menu runs - a script, a scene, a button, an automation - is a button only:
# its state is whether it is running or when it last ran, which is nothing to show.
CONTROLS_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "script": (BUTTON,),
    "scene": (BUTTON,),
    "button": (BUTTON,),
    "input_button": (BUTTON,),
    "automation": (BUTTON,),
    "light": (TOGGLE, SLIDER, INDICATOR),
    "switch": (TOGGLE, INDICATOR),
    "input_boolean": (TOGGLE, INDICATOR),
    "fan": (TOGGLE, INDICATOR),
    "cover": (TOGGLE, SLIDER, INDICATOR),
    "lock": (TOGGLE, INDICATOR),
    "binary_sensor": (INDICATOR,),
    "sensor": (INDICATOR,),
    "select": (SELECTOR, INDICATOR),
    "input_select": (SELECTOR, INDICATOR),
    "climate": (SELECTOR, INDICATOR),
    "number": (SLIDER, INDICATOR),
    "input_number": (SLIDER, INDICATOR),
    "media_player": (SLIDER, INDICATOR),
    "input_text": (INPUT, INDICATOR),
    "text": (INPUT, INDICATOR),
}


def controls_for_domain(domain: str) -> tuple[str, ...]:
    """The controls an entity of this domain may be shown as, the default first."""
    return CONTROLS_BY_DOMAIN.get(domain, (INDICATOR,))


# The service a button row calls.
BUTTON_SERVICES: dict[str, str] = {
    "script": "turn_on",
    "scene": "turn_on",
    "button": "press",
    "input_button": "press",
    "automation": "trigger",
}
# Toggle rows whose domain has a `toggle` service of its own; cover and lock take the
# explicit pair, chosen by the state the row showed.
TOGGLE_SERVICE_DOMAINS = ("light", "switch", "input_boolean", "fan")

# A slider in percent - a light's brightness, a cover's position, a player's volume - moves
# in tens: it is moved with the arrow keys of a remote.
PERCENT_BOUNDS = (0, 100, 10)
PERCENT = "%"

# Binary sensor classes whose "on" is something to look at - open, wet, burning, low. An
# indicator shows those green while off, as the contract's example of a closed door does;
# every other on/off state is green while on.
ATTENTION_CLASSES = frozenset(
    {
        "battery",
        "carbon_monoxide",
        "cold",
        "door",
        "garage_door",
        "gas",
        "heat",
        "lock",
        "moisture",
        "opening",
        "problem",
        "safety",
        "smoke",
        "tamper",
        "window",
    }
)

# The icon a row gets when the user chose none: by device class first, then by domain.
_ICON_BY_CLASS: dict[tuple[str, str], str] = {
    ("cover", "garage"): "garage",
    ("cover", "gate"): "gate",
    ("cover", "door"): "door",
    ("binary_sensor", "door"): "door",
    ("binary_sensor", "garage_door"): "garage",
    ("binary_sensor", "window"): "door",
    ("binary_sensor", "opening"): "door",
    ("binary_sensor", "smoke"): "alarm",
    ("binary_sensor", "gas"): "alarm",
    ("binary_sensor", "carbon_monoxide"): "alarm",
    ("binary_sensor", "moisture"): "alarm",
    ("binary_sensor", "safety"): "alarm",
    ("binary_sensor", "lock"): "lock",
    ("sensor", "temperature"): "thermometer",
    ("media_player", "tv"): "tv",
    ("media_player", "speaker"): "speaker",
    ("media_player", "receiver"): "speaker",
}
_ICON_BY_DOMAIN: dict[str, str] = {
    "script": "script",
    "scene": "scene",
    "automation": "script",
    "button": "power",
    "input_button": "power",
    "light": "lamp",
    "switch": "power",
    "input_boolean": "power",
    "fan": "fan",
    "cover": "blinds",
    "lock": "lock",
    "climate": "radiator",
    "media_player": "speaker",
    "input_text": "note",
    "text": "note",
    "alarm_control_panel": "alarm",
    "camera": "camera",
    "person": "home",
    "device_tracker": "home",
    "water_heater": "radiator",
}

ROW_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ACTION_ID = re.compile(r"^[0-9a-f]{12}$")
# An entity registry id, as Home Assistant writes one.
REGISTRY_ID = re.compile(r"^[0-9a-f]{32}$")

# What a stored group and a stored row look like in the options. A row's `entity_id` holds
# the entity's registry id, or its entity id where it has no registry entry.
KIND = "type"
KIND_GROUP = "group"
KIND_ROW = "row"
ITEM_ID = "id"
ITEM_LABEL = "label"
ITEM_ICON = "icon"
GROUP_ROWS = "rows"
ROW_ENTITY = "entity_id"
ROW_CONTROL = "control"

# Texts of this integration a row may show, keyed as in `common` of the translations.
_TEXT_KEYS = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def domain_of(entity_id: str | None) -> str:
    """The domain of an entity id; empty for anything that is not one."""
    return split_entity_id(entity_id)[0] if entity_id and valid_entity_id(entity_id) else ""


def resolve_entity(hass: HomeAssistant, stored: Any) -> str | None:
    """The entity id a stored binding names now: a registry id followed through a rename.

    An entity id is taken as it is - the entity may simply not exist yet - and a registry id
    that names no entry any more is None.
    """
    if not isinstance(stored, str) or not stored:
        return None
    if valid_entity_id(stored):
        return stored
    return er.async_resolve_entity_id(er.async_get(hass), stored)


def stored_binding(hass: HomeAssistant, entity_id: str) -> str:
    """What a row stores for an entity: its registry id, else its entity id."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.id if entry is not None else entity_id


def resolved_row(hass: HomeAssistant, row: Mapping[str, Any]) -> dict[str, Any]:
    """A stored row with its binding read as the entity id it names now ("" for none)."""
    return {**row, ROW_ENTITY: resolve_entity(hass, row[ROW_ENTITY]) or ""}


def resolved_menu(hass: HomeAssistant, menu: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A stored menu with every row's binding resolved."""
    resolved: list[dict[str, Any]] = []
    for item in menu:
        if item.get(KIND) == KIND_GROUP:
            resolved.append(
                {**item, GROUP_ROWS: [resolved_row(hass, row) for row in item[GROUP_ROWS]]}
            )
        else:
            resolved.append(resolved_row(hass, item))
    return resolved


def menu_topic(node_id: str) -> str:
    """The retained topic that carries one receiver's menu."""
    return f"{TOPIC_MENU_PREFIX}/{node_id}"


async def async_retract_menu(hass: HomeAssistant, node_id: str) -> None:
    """Publish the empty retained payload: this receiver has no menu any more."""
    await mqtt.async_publish(hass, menu_topic(node_id), "", qos=1, retain=True)


# ----------------------------------------------------------------------- storage


def new_item_id(kind: str, taken: Iterable[str]) -> str:
    """A fresh id for a group (`g_`) or a row (`r_`), unique among `taken`.

    Random rather than counted: a removed row's id is never handed to the next row added, so
    an action meant for the removed one cannot reach its successor.
    """
    prefix = "g_" if kind == KIND_GROUP else "r_"
    used = set(taken)
    while (candidate := f"{prefix}{secrets.token_hex(5)}") in used:
        pass  # pragma: no cover - 40 random bits colliding
    return candidate


def _clean_row(raw: Any) -> dict[str, Any] | None:
    """A stored row as this release reads it, or None for one it cannot."""
    if not isinstance(raw, Mapping) or raw.get(KIND) != KIND_ROW:
        return None
    row_id, entity_id, control = raw.get(ITEM_ID), raw.get(ROW_ENTITY), raw.get(ROW_CONTROL)
    if not isinstance(row_id, str) or not ROW_ID.match(row_id):
        return None
    if not isinstance(entity_id, str):
        return None
    if valid_entity_id(entity_id):
        if control not in controls_for_domain(domain_of(entity_id)):
            return None
    elif not REGISTRY_ID.match(entity_id) or control not in CONTROLS:
        # A registry id says nothing of the domain; the control is checked against the
        # entity it names when the row is shown and when it is acted on.
        return None
    row: dict[str, Any] = {
        KIND: KIND_ROW,
        ITEM_ID: row_id,
        ROW_ENTITY: entity_id,
        ROW_CONTROL: control,
    }
    if isinstance(label := raw.get(ITEM_LABEL), str) and label.strip():
        row[ITEM_LABEL] = label.strip()
    if raw.get(ITEM_ICON) in MENU_ICONS:
        row[ITEM_ICON] = raw[ITEM_ICON]
    return row


def stored_menu(options: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The menu the options hold, as a fresh copy with anything unreadable left out.

    Ids must be unique across the whole menu; a second item with an id already seen is left
    out rather than allowed to answer for the first.
    """
    seen: set[str] = set()
    menu: list[dict[str, Any]] = []
    raw_menu = options.get(CONF_MENU)
    if not isinstance(raw_menu, list):
        return menu
    for raw in raw_menu:
        if isinstance(raw, Mapping) and raw.get(KIND) == KIND_GROUP:
            group_id = raw.get(ITEM_ID)
            label = raw.get(ITEM_LABEL)
            if (
                not isinstance(group_id, str)
                or not ROW_ID.match(group_id)
                or group_id in seen
                or not isinstance(label, str)
                or not label.strip()
            ):
                continue
            seen.add(group_id)
            rows: list[dict[str, Any]] = []
            for raw_row in raw.get(GROUP_ROWS) or []:
                if (row := _clean_row(raw_row)) is not None and row[ITEM_ID] not in seen:
                    seen.add(row[ITEM_ID])
                    rows.append(row)
            icon = raw.get(ITEM_ICON)
            menu.append(
                {
                    KIND: KIND_GROUP,
                    ITEM_ID: group_id,
                    ITEM_LABEL: label.strip(),
                    ITEM_ICON: icon if icon in MENU_ICONS else MENU_ICON_NEUTRAL,
                    GROUP_ROWS: rows,
                }
            )
        elif (row := _clean_row(raw)) is not None and row[ITEM_ID] not in seen:
            seen.add(row[ITEM_ID])
            menu.append(row)
    return menu


def menu_rows(menu: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every row of a stored menu, in order, groups' rows included."""
    rows: list[dict[str, Any]] = []
    for item in menu:
        if item.get(KIND) == KIND_GROUP:
            rows.extend(item.get(GROUP_ROWS) or [])
        else:
            rows.append(dict(item))
    return rows


def menu_ids(menu: Iterable[Mapping[str, Any]]) -> set[str]:
    """Every id a stored menu uses."""
    menu = list(menu)
    return {item[ITEM_ID] for item in menu} | {row[ITEM_ID] for row in menu_rows(menu)}


def find_row(menu: Iterable[Mapping[str, Any]], row_id: str) -> dict[str, Any] | None:
    """The stored row with this id, or None."""
    for row in menu_rows(menu):
        if row[ITEM_ID] == row_id:
            return row
    return None


def allowed_controls(hass: HomeAssistant, entity_id: str) -> list[str]:
    """The controls a row bound to this entity may be, the default first.

    The domain decides (ADR-0012); what the entity says it supports narrows it, where it
    says: a light that is only on/off has no brightness, a cover that cannot be put at a
    position and a player whose volume cannot be set have no slider. An entity that has no
    state yet keeps the whole list - it may be one that is still starting.
    """
    controls = list(controls_for_domain(domain_of(entity_id)))
    if SLIDER not in controls or (state := hass.states.get(entity_id)) is None:
        return controls
    domain = state.domain
    if domain == "light":
        modes = state.attributes.get("supported_color_modes")
        if isinstance(modes, (list, tuple, set)) and modes and set(modes) <= {"onoff"}:
            controls.remove(SLIDER)
    elif domain in ("cover", "media_player"):
        features = state.attributes.get("supported_features")
        # SET_POSITION for a cover and VOLUME_SET for a player are both bit 4.
        if isinstance(features, int) and not features & 4:
            controls.remove(SLIDER)
    return controls


# ---------------------------------------------------------------------- rendering


def _cut(text: Any, limit: int) -> str:
    """Text on one line, cut at `limit` characters."""
    return " ".join(str(text).split())[:limit]


def _number(value: float) -> int | float:
    """A number as JSON should carry it: whole numbers without a fraction."""
    value = round(float(value), 6)
    return int(value) if value.is_integer() else value


def _float(value: Any) -> float | None:
    """A finite number, or None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def coarsen(low: Any, high: Any, step: Any) -> tuple[float, float, float] | None:
    """A slider's bounds with at most 200 steps between them, or None for none at all.

    A step that would make more is multiplied by the smallest whole number that brings the
    count down, so every value the slider offers is still one the entity's own step allows.
    """
    low, high, step = _float(low), _float(high), _float(step)
    if low is None or high is None or step is None or step <= 0 or high <= low:
        return None
    steps = (high - low) / step
    if steps > MAX_STEPS:
        step = round(step * math.ceil(steps / MAX_STEPS - 1e-9), 10)
    return low, high, step


def snap(value: float, bounds: tuple[float, float, float]) -> float:
    """The value on the slider's grid nearest to `value`, inside its bounds."""
    low, high, step = bounds
    snapped = low + round((value - low) / step) * step
    return round(min(max(snapped, low), high), 10)


def _on_off(domain: str, raw: str) -> bool | None:
    """Whether an entity is on, by the state that means "on" in its domain; None when neither."""
    if domain == "lock":
        return {"locked": True, "unlocked": False, "open": False}.get(raw)
    if domain == "cover":
        return {"open": True, "opening": True, "closed": False, "closing": False}.get(raw)
    if domain == "media_player":
        if raw in ("off", "standby"):
            return False
        return True if raw in ("on", "playing", "paused", "idle", "buffering") else None
    return {"on": True, "off": False}.get(raw)


def _colour(value: bool | None, *, inverted: bool = False) -> str | None:
    """Green for a true value and red for a false one - the other way round when inverted."""
    if value is None:
        return None
    return "green" if value != inverted else "red"


@dataclass(slots=True)
class RowView:
    """One row as it is published now, and what an action on it may carry."""

    payload: dict[str, Any]
    available: bool
    # The entity the row is bound to now; "" when its binding names nothing.
    entity_id: str = ""
    value: Any = None
    # A selector's shown option -> the entity's own option.
    options: dict[str, str] = field(default_factory=dict)
    bounds: tuple[float, float, float] | None = None
    max_length: int | None = None
    # What was left out to stay inside the contract's bounds, for one log line.
    dropped: str | None = None


def default_icon(domain: str, device_class: Any) -> str:
    """The icon of a row whose user chose none."""
    return (
        _ICON_BY_CLASS.get((domain, device_class))
        or _ICON_BY_DOMAIN.get(domain)
        or MENU_ICON_NEUTRAL
    )


def row_label(hass: HomeAssistant, row: Mapping[str, Any]) -> str:
    """What a row is called: the user's label, else the entity's name, else its id."""
    if label := row.get(ITEM_LABEL):
        return str(label)
    entity_id = resolve_entity(hass, row[ROW_ENTITY])
    state = hass.states.get(entity_id) if entity_id else None
    if state is not None:
        return state.name
    return entity_id or row[ROW_ENTITY]


class _Translator:
    """Turns an entity's state into the words Home Assistant shows for it.

    The same lookup as Home Assistant's own state translation - the entity's translation
    key first, then its device class, then its domain - but in the translations loaded for
    the rows in hand, rather than in the cache of the integrations that happen to be set
    up: the menu may name an entity of a domain whose integration this one never loads.
    """

    def __init__(self, hass: HomeAssistant, texts: Mapping[str, str]) -> None:
        self.texts = texts
        self.registry = er.async_get(hass)

    def state(self, state: State, raw: str | None = None) -> str:
        """The household's word for a state (the entity's own state when `raw` is None)."""
        raw = state.state if raw is None else raw
        if raw in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return self.texts.get(raw) or raw
        domain = state.domain
        entry = self.registry.async_get(state.entity_id)
        keys = []
        if entry is not None and entry.translation_key:
            keys.append(
                f"component.{entry.platform}.entity.{domain}.{entry.translation_key}"
                f".state.{raw}"
            )
        if device_class := state.attributes.get("device_class"):
            keys.append(f"component.{domain}.entity_component.{device_class}.state.{raw}")
        keys.append(f"component.{domain}.entity_component._.state.{raw}")
        return next((self.texts[key] for key in keys if key in self.texts), raw)


def render_row(
    hass: HomeAssistant, row: Mapping[str, Any], texts: Mapping[str, str]
) -> RowView:
    """One stored row as the contract's row object, from the bound entity's state now."""
    translator = _Translator(hass, texts)
    entity_id = resolve_entity(hass, row[ROW_ENTITY]) or ""
    domain = domain_of(entity_id)
    control: str = row[ROW_CONTROL]
    state = hass.states.get(entity_id) if entity_id else None
    device_class = state.attributes.get("device_class") if state is not None else None
    payload: dict[str, Any] = {
        "type": control,
        "id": row[ITEM_ID],
        "label": _cut(row_label(hass, row), MAX_LABEL),
        "icon": row.get(ITEM_ICON) or default_icon(domain, device_class),
    }
    # A registry id now naming an entity of a domain this control does not fit is shown,
    # never acted on.
    available = (
        state is not None
        and state.state != STATE_UNAVAILABLE
        and control in controls_for_domain(domain)
    )
    view = RowView(payload=payload, available=available, entity_id=entity_id)
    shown = (
        _cut(translator.state(state), MAX_STATE)
        if state is not None
        else _cut(texts.get(STATE_UNAVAILABLE) or STATE_UNAVAILABLE, MAX_STATE)
    )

    if control == BUTTON:
        payload["state"] = None
    elif control in (TOGGLE, INDICATOR):
        value = _on_off(domain, state.state) if available and state is not None else None
        inverted = domain == "binary_sensor" and device_class in ATTENTION_CLASSES
        if state is not None and available and control == INDICATOR and value is None:
            # A state that is not on or off - a sensor's reading - shown as text.
            shown = _sensor_text(translator, state)
        payload.update(value=value, state=shown, color=_colour(value, inverted=inverted))
        view.value = value
    elif control == SELECTOR:
        _render_selector(translator, state if available else None, view)
        if not available:
            payload["state"] = shown
    elif control == SLIDER:
        _render_slider(state if available else None, domain, view)
        if not available:
            payload["state"] = shown
    elif control == INPUT:
        _render_input(state if available else None, view)
        if not available:
            payload["state"] = shown
    payload["available"] = view.available
    return view


def _sensor_text(translator: _Translator, state: State) -> str:
    """A reading with its unit, or the translated state where it is a word."""
    text = translator.state(state)
    unit = state.attributes.get("unit_of_measurement")
    if text == state.state and isinstance(unit, str) and unit:
        text = f"{state.state} {unit}"
    return _cut(text, MAX_STATE)


def _render_selector(translator: _Translator, state: State | None, view: RowView) -> None:
    """The options the entity offers, in the household's words, and the current one."""
    payload = view.payload
    raw_options: Any = []
    if state is not None:
        key = "hvac_modes" if state.domain == "climate" else "options"
        raw_options = state.attributes.get(key) or []
    mapping: dict[str, str] = {}
    dropped = 0
    for raw in raw_options if isinstance(raw_options, (list, tuple)) else []:
        if not isinstance(raw, str):
            continue
        if len(mapping) >= MAX_OPTIONS:
            dropped += 1
            continue
        shown = _cut(translator.state(state, raw), MAX_OPTION) if state is not None else raw
        if not shown or shown in mapping:
            # Two options read the same once translated or cut: the entity's own word.
            shown = _cut(raw, MAX_OPTION)
        if not shown or shown in mapping:
            dropped += 1
            continue
        mapping[shown] = raw
    if dropped:
        view.dropped = f"{dropped} option(s)"
    view.options = mapping
    current = next(
        (shown for shown, raw in mapping.items() if state is not None and raw == state.state),
        None,
    )
    view.value = current
    payload.update(
        value=current,
        options=list(mapping),
        state=_cut(translator.state(state), MAX_STATE) if state is not None else None,
    )


def _slider_value(state: State, domain: str) -> float | None:
    """Where the slider stands: a percentage, or the entity's own number."""
    attributes = state.attributes
    if domain == "light":
        if state.state == "off":
            return 0
        brightness = _float(attributes.get("brightness"))
        return None if brightness is None else round(brightness / 255 * 100)
    if domain == "cover":
        return _float(attributes.get("current_position"))
    if domain == "media_player":
        volume = _float(attributes.get("volume_level"))
        return None if volume is None else round(volume * 100)
    return _float(state.state)


def _render_slider(state: State | None, domain: str, view: RowView) -> None:
    """Bounds, step, unit and value, coarsened to at most 200 steps."""
    payload = view.payload
    unit: Any = None
    if domain in ("light", "cover", "media_player"):
        bounds = coarsen(*PERCENT_BOUNDS)
        unit = PERCENT
    elif state is not None:
        bounds = coarsen(
            state.attributes.get("min"),
            state.attributes.get("max"),
            state.attributes.get("step", 1),
        )
        unit = state.attributes.get("unit_of_measurement")
    else:
        bounds = None
    unit = _cut(unit, MAX_UNIT) or None if isinstance(unit, str) else None
    value = _slider_value(state, domain) if state is not None and bounds is not None else None
    if bounds is None:
        # Nothing to move between: shown, never acted on.
        view.available = False
        low, high, step = coarsen(*PERCENT_BOUNDS) or (0, 100, 10)
    else:
        low, high, step = bounds
    view.bounds = bounds
    view.value = value
    state_text = None
    if value is not None:
        state_text = _cut(f"{_number(value)} {unit}" if unit else f"{_number(value)}", MAX_STATE)
    payload.update(
        value=None if value is None else _number(value),
        min=_number(low),
        max=_number(high),
        step=_number(step),
        unit=unit,
        state=state_text,
    )


def _render_input(state: State | None, view: RowView) -> None:
    """The text and how long it may be; a password field's text is never published."""
    payload = view.payload
    maximum = MAX_INPUT
    value: str | None = None
    if state is not None:
        if (declared := _float(state.attributes.get("max"))) is not None:
            maximum = int(min(max(declared, 1), MAX_INPUT))
        if state.attributes.get("mode") != "password" and state.state != STATE_UNKNOWN:
            value = state.state[:maximum]
    view.max_length = maximum
    view.value = value
    payload.update(
        value=value,
        max_length=maximum,
        state=None if value is None else _cut(value, MAX_STATE),
    )


def build_menu(
    hass: HomeAssistant, menu: list[dict[str, Any]], texts: Mapping[str, str]
) -> tuple[list[dict[str, Any]], dict[str, RowView], list[str]]:
    """The contract's `items`, every row's view by id, and what had to be left out."""
    items: list[dict[str, Any]] = []
    views: dict[str, RowView] = {}
    notes: list[str] = []
    groups = 0

    def _row(row: dict[str, Any]) -> dict[str, Any]:
        view = render_row(hass, row, texts)
        views[row[ITEM_ID]] = view
        if view.dropped:
            notes.append(f"{view.dropped} of row {row[ITEM_ID]} ({row[ROW_ENTITY]})")
        return view.payload

    for item in menu:
        if len(items) >= MAX_ENTRIES:
            notes.append(f"item {item[ITEM_ID]}: the top list holds {MAX_ENTRIES}")
            continue
        if item[KIND] == KIND_GROUP:
            if groups >= MAX_GROUPS:
                notes.append(f"group {item[ITEM_ID]}: a menu holds {MAX_GROUPS} groups")
                continue
            groups += 1
            rows = item[GROUP_ROWS]
            if len(rows) > MAX_ENTRIES:
                notes.append(
                    f"{len(rows) - MAX_ENTRIES} row(s) of group {item[ITEM_ID]}: a group "
                    f"holds {MAX_ENTRIES}"
                )
            items.append(
                {
                    "type": KIND_GROUP,
                    "id": item[ITEM_ID],
                    "label": _cut(item[ITEM_LABEL], MAX_LABEL),
                    "icon": item[ITEM_ICON],
                    "rows": [_row(row) for row in rows[:MAX_ENTRIES]],
                }
            )
        else:
            items.append(_row(item))
    return items, views, notes


def encode_menu(payload: Mapping[str, Any]) -> bytes:
    """The payload as it goes on the wire: compact JSON in UTF-8."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fit_menu(payload: dict[str, Any]) -> tuple[bytes, int]:
    """The encoded payload under 64 KiB, and how many entries had to be dropped for it.

    Entries go from the end - the last row of the last group, then that group once it is
    empty - so what the user put first survives. Each row is measured once; the sum is only
    an estimate of the separators, so the result is measured again until it fits.
    """
    encoded = encode_menu(payload)
    dropped = 0
    items: list[dict[str, Any]] = payload["items"]
    while len(encoded) >= MAX_PAYLOAD_BYTES and items:
        excess = len(encoded) - MAX_PAYLOAD_BYTES + 1
        while excess > 0 and items:
            last = items[-1]
            if last.get("type") == KIND_GROUP and last["rows"]:
                excess -= len(encode_menu(last["rows"].pop())) + 1
            else:
                excess -= len(encode_menu(items.pop())) + 1
            dropped += 1
        encoded = encode_menu(payload)
    return encoded, dropped


# ------------------------------------------------------------------------ actions


@dataclass(slots=True, frozen=True)
class MenuAction:
    """One `menu_action`, read and checked against the contract's shape."""

    action_id: str
    rev: int
    row: str
    action: str
    value: Any


class Refused(Exception):
    """An action that is not performed, and the cause logged for it."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(cause)
        self.cause = cause
        self.detail = detail


def parse_action(payload: Any, now: float | None = None) -> MenuAction:
    """Read one `menu_action`, or raise `Refused` with what was wrong with it.

    `now` is this Home Assistant's clock in epoch seconds; an action whose `ts` is more than
    `MAX_ACTION_AGE` seconds before it is refused as stale.
    """
    if isinstance(payload, (bytes, bytearray)) and len(payload) > ACTION_MAX_BYTES:
        raise Refused("unreadable", f"{len(payload)} bytes")
    if (text := decode_payload(payload)) is None or len(text) > ACTION_MAX_BYTES:
        raise Refused("unreadable", "empty, too long or not UTF-8")
    try:
        data = json.loads(text)
    except ValueError:
        raise Refused("unreadable", "not JSON") from None
    if not isinstance(data, dict):
        raise Refused("unreadable", "not a JSON object")
    action_id, rev, row, action = (data.get(key) for key in ("id", "rev", "row", "action"))
    if not isinstance(action_id, str) or not ACTION_ID.match(action_id):
        raise Refused("malformed", "id")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        raise Refused("malformed", "rev")
    if not isinstance(row, str) or not ROW_ID.match(row):
        raise Refused("malformed", "row")
    if action not in (ACTION_PRESS, ACTION_TOGGLE, ACTION_SET):
        raise Refused("malformed", "action")
    if action == ACTION_SET and "value" not in data:
        raise Refused("malformed", "value")
    if "ts" in data:
        # A number, as the contract says - not a text that reads as one.
        raw_ts = data["ts"]
        if not isinstance(raw_ts, (int, float)) or (ts := _float(raw_ts)) is None:
            raise Refused("malformed", "ts")
        now = dt_util.utcnow().timestamp() if now is None else now
        if now - ts > MAX_ACTION_AGE:
            raise Refused("stale", f"{now - ts:.0f} s old")
    return MenuAction(action_id, rev, row, action, data.get("value"))


def service_call(
    row: Mapping[str, Any], view: RowView, action: MenuAction
) -> tuple[str, str, dict[str, Any], Any]:
    """The service one action on one row calls: domain, service, data and the value applied.

    Raises `Refused` for an action the row does not take, a row that cannot be acted on now,
    and a value outside what the entity accepts.
    """
    control = row[ROW_CONTROL]
    entity_id = view.entity_id or row[ROW_ENTITY]
    domain = domain_of(view.entity_id)
    expected = ACTION_FOR_CONTROL.get(control)
    if expected is None:
        raise Refused("not_actionable", f"{control} row {row[ITEM_ID]}")
    if action.action != expected:
        raise Refused("wrong_action", f"{action.action} on {control} row {row[ITEM_ID]}")
    if not view.available:
        raise Refused("unavailable", f"row {row[ITEM_ID]} ({entity_id})")

    if control == BUTTON:
        return domain, BUTTON_SERVICES[domain], {}, None
    if control == TOGGLE:
        if domain in TOGGLE_SERVICE_DOMAINS:
            return domain, "toggle", {}, None
        if view.value is None:
            raise Refused("state_unknown", f"row {row[ITEM_ID]} ({entity_id})")
        if domain == "cover":
            return domain, "close_cover" if view.value else "open_cover", {}, None
        return domain, "unlock" if view.value else "lock", {}, None

    value = action.value
    if control == SELECTOR:
        if not isinstance(value, str) or value not in view.options:
            raise Refused("invalid_value", f"option for row {row[ITEM_ID]}")
        option = view.options[value]
        if domain == "climate":
            return domain, "set_hvac_mode", {"hvac_mode": option}, option
        return domain, "select_option", {"option": option}, option
    if control == SLIDER:
        # A number, as the contract says - not a text that reads as one.
        number = _float(value) if isinstance(value, (int, float)) else None
        bounds = view.bounds
        if bounds is None or number is None or not bounds[0] <= number <= bounds[1]:
            raise Refused("invalid_value", f"number for row {row[ITEM_ID]}")
        applied = _number(snap(number, bounds))
        if domain == "light":
            return domain, "turn_on", {"brightness_pct": applied}, applied
        if domain == "cover":
            return domain, "set_cover_position", {"position": applied}, applied
        if domain == "media_player":
            return domain, "volume_set", {"volume_level": round(applied / 100, 4)}, applied
        return domain, "set_value", {"value": applied}, applied
    # INPUT
    if not isinstance(value, str) or len(value) > (view.max_length or MAX_INPUT):
        raise Refused("invalid_value", f"text for row {row[ITEM_ID]}")
    return domain, "set_value", {"value": value}, value


async def async_menu_texts(
    hass: HomeAssistant, rows: Iterable[Mapping[str, Any]]
) -> dict[str, str]:
    """Load the state translations the rows need, and this integration's own words."""
    language = hass.config.language
    entity_ids = [
        entity_id for row in rows if (entity_id := resolve_entity(hass, row[ROW_ENTITY]))
    ]
    domains = {domain_of(entity_id) for entity_id in entity_ids} - {""}
    registry = er.async_get(hass)
    platforms = {
        entry.platform
        for entity_id in entity_ids
        if (entry := registry.async_get(entity_id)) is not None
    }
    texts: dict[str, str] = {}
    if domains:
        texts.update(
            await async_get_translations(hass, language, "entity_component", domains)
        )
    if platforms:
        texts.update(await async_get_translations(hass, language, "entity", platforms))
    common = await async_get_translations(hass, language, "common", {DOMAIN})
    for key in _TEXT_KEYS:
        texts[key] = common.get(f"component.{DOMAIN}.common.menu_state_{key}") or key
    return texts


# ---------------------------------------------------------------------- publisher


class ReceiverMenu:
    """One receiver's menu: published while the entry is loaded, acted on when asked."""

    def __init__(
        self, hass: HomeAssistant, entry: Enigma2MqttConfigEntry, box: Enigma2Box
    ) -> None:
        """Read the menu from the options; nothing is published or tracked yet."""
        self.hass = hass
        self.entry = entry
        self.box = box
        self.menu = stored_menu(entry.options)
        self._unsubscribes: list[CALLBACK_TYPE] = []
        # Following the bound entities' states, and which entity ids that is for.
        self._untrack: CALLBACK_TYPE | None = None
        self._tracked: list[str] = []
        self._debouncer: Debouncer[Any] = Debouncer(
            hass,
            _LOGGER,
            cooldown=PUBLISH_COOLDOWN,
            immediate=True,
            function=self._async_publish,
        )
        # The last `items` published, so that an attribute nobody shows - a player's
        # position, a light's colour - does not republish the same menu.
        self._published: bytes | None = None
        self._force = True
        self._was_available = box.available
        self._recent_ids: deque[str] = deque(maxlen=ACTION_IDS_REMEMBERED)
        self._logged_causes: set[str] = set()
        self._logged_notes: set[str] = set()

    @property
    def topic(self) -> str:
        """Where this receiver's menu is published."""
        return menu_topic(self.box.node_id)

    async def async_start(self) -> None:
        """Publish the menu now, and again whenever what it shows changes."""
        self._async_track()
        if self.menu:
            self._unsubscribes.append(
                self.hass.bus.async_listen(
                    er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed
                )
            )
        self._unsubscribes.append(
            self.box.async_add_listener(self._box_changed, topics=(TOPIC_AVAILABILITY,))
        )
        await self._debouncer.async_call()

    @callback
    def async_stop(self) -> None:
        """Stop following the entities. The retained menu stays; removal retracts it."""
        while self._unsubscribes:
            self._unsubscribes.pop()()
        if self._untrack is not None:
            self._untrack()
            self._untrack = None
        self._debouncer.async_shutdown()

    def _bound_entities(self) -> list[str]:
        """The entity ids the rows name now."""
        return sorted(
            {
                entity_id
                for row in menu_rows(self.menu)
                if (entity_id := resolve_entity(self.hass, row[ROW_ENTITY]))
            }
        )

    @callback
    def _async_track(self) -> None:
        """Follow the state of every entity the rows name now."""
        if self._untrack is not None:
            self._untrack()
            self._untrack = None
        self._tracked = self._bound_entities()
        if self._tracked:
            self._untrack = async_track_state_change_event(
                self.hass, self._tracked, self._state_changed
            )

    @callback
    def _registry_changed(self, _event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """A bound entity was renamed, added or removed: follow it under its new id."""
        if self._bound_entities() != self._tracked:
            self._async_track()
            self._debouncer.async_schedule_call()

    @callback
    def _state_changed(self, _event: Event[EventStateChangedData]) -> None:
        self._debouncer.async_schedule_call()

    @callback
    def _box_changed(self) -> None:
        """Publish again when the receiver comes back: it may have lost what it held."""
        available = self.box.available
        if available and not self._was_available:
            self._force = True
            self._debouncer.async_schedule_call()
        self._was_available = available

    async def _async_texts(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
        return await async_menu_texts(self.hass, rows)

    def _next_rev(self) -> int:
        """The next revision: one more than the last this Home Assistant run published."""
        revs: dict[str, int] = self.hass.data.setdefault(f"{DOMAIN}_menu_rev", {})
        revs[self.box.node_id] = revs.get(self.box.node_id, 0) + 1
        return revs[self.box.node_id]

    async def _async_publish(self) -> None:
        """Publish the whole menu, unless it is the one already published."""
        force, self._force = self._force, False
        if not self.menu:
            wire = b""
        else:
            texts = await self._async_texts(menu_rows(self.menu))
            items, _views, notes = build_menu(self.hass, self.menu, texts)
            payload: dict[str, Any] = {
                "v": MENU_VERSION,
                "rev": 0,
                "title": MENU_TITLE,
                "items": items,
            }
            _encoded, dropped = fit_menu(payload)
            if dropped:
                notes.append(f"{dropped} entr(ies) from the end, to stay under 64 KiB")
            self._log_notes(notes)
            wire = encode_menu(payload["items"]) if payload["items"] else b""
        if wire == self._published and not force:
            return
        self._published = wire
        if wire:
            payload["rev"] = self._next_rev()
            text = encode_menu(payload).decode("utf-8")
        else:
            text = ""
        try:
            await mqtt.async_publish(self.hass, self.topic, text, qos=1, retain=True)
        except HomeAssistantError as error:
            # Published again on the next change, or when the receiver comes back.
            self._published = None
            _LOGGER.debug("Could not publish the menu of %s: %s", self.box.node_id, error)

    def _log_notes(self, notes: list[str]) -> None:
        """Say once what the menu could not carry, and again only if it changes."""
        for note in notes:
            if note not in self._logged_notes:
                self._logged_notes.add(note)
                _LOGGER.warning(
                    "The menu of %s leaves out %s", self.box.node_id, note
                )

    # ------------------------------------------------------------------- actions

    @callback
    def async_action_received(self, msg: ReceiveMessage) -> None:
        """Check one `menu_action` and, when it may be performed, perform it."""
        try:
            if msg.retain:
                # Somebody's leftover: performing it at every reconnect would act for nobody.
                raise Refused("retained")
            if CAPABILITY_MENU not in self.box.capabilities:
                raise Refused("not_claimed", f"receiver {self.box.node_id}")
            action = parse_action(msg.payload)
            if action.action_id in self._recent_ids:
                raise Refused("duplicate", action.action_id)
            self._recent_ids.append(action.action_id)
        except Refused as refusal:
            self._log_refusal(refusal)
            return
        self.entry.async_create_background_task(
            self.hass,
            self._async_perform(action),
            f"{DOMAIN} menu_action {self.box.node_id}",
        )

    async def _async_perform(self, action: MenuAction) -> None:
        """Perform an action against the row the options hold now, or refuse it."""
        try:
            if (row := find_row(self.menu, action.row)) is None:
                raise Refused("unknown_row", action.row)
            texts = await self._async_texts([row])
            view = render_row(self.hass, row, texts)
            domain, service, data, applied = service_call(row, view, action)
        except Refused as refusal:
            self._log_refusal(refusal)
            return
        entity_id = view.entity_id
        try:
            await self.hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
            )
        except (HomeAssistantError, vol.Invalid) as error:
            _LOGGER.warning(
                "The menu of %s could not call %s.%s for %s: %s",
                self.box.node_id,
                domain,
                service,
                entity_id,
                error,
            )
            return
        self.hass.bus.async_fire(
            EVENT_MENU_ACTION,
            {
                ATTR_DEVICE_ID: self.box.device_id,
                ATTR_NODE_ID: self.box.node_id,
                "row_id": row[ITEM_ID],
                ATTR_ENTITY_ID: entity_id,
                "action": action.action,
                "value": applied,
            },
        )

    def _log_refusal(self, refusal: Refused) -> None:
        """A refusal is logged as a warning the first time for its cause, then quietly."""
        level = logging.DEBUG if refusal.cause in self._logged_causes else logging.WARNING
        self._logged_causes.add(refusal.cause)
        _LOGGER.log(
            level,
            "Refused a menu action from %s (%s%s); nothing was performed",
            self.box.node_id,
            refusal.cause,
            f": {refusal.detail}" if refusal.detail else "",
        )

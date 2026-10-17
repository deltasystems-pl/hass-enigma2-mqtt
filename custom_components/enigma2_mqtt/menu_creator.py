"""The menu creator: the receiver's web interface edits the menu, Home Assistant stores it.

A plugin after 0.4.0 offers a page in the receiver's web interface where the menu of
ADR-0012 is built. Home Assistant stays the owner of the menu and the receiver never holds
a Home Assistant token (ADR-0013):

- While the receiver claims `menu_editor`, this module publishes a **catalogue** on
  `enigma2mqtt/menu_catalogue/<node_id>`, retained: every entity a row may be bound to, with
  its name, area and the controls it may be shown as, and the stored menu with its
  revision - a content hash of the menu the options hold.
- The page sends the whole edited menu back on `<base>/<node_id>/menu_edit`, with the
  revision it started from. That message is untrusted input - anyone on the broker can
  publish it - and it is read with the options flow's own rules (the shared functions in
  `menu.py`), refused when it started from another revision, and otherwise stored in the
  entry's options without a reload, so no entity goes unavailable for a menu change.
- Every edit that carried a readable id is answered on
  `enigma2mqtt/menu_edit_result/<node_id>`.

Editing from the receiver is off unless the user turns it on in the receiver menu's
options (`menu_remote_edit`). Off, the catalogue says only that editing is off, and every
edit is answered `disabled`. On, whoever can publish to the broker can replace the menu
with any entity Home Assistant has, and then operate it from the menu; the documentation
and SECURITY.md say so.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import logging
import math
import re
import time
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    Event,
    HomeAssistant,
    callback,
    valid_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.start import async_at_started

from .box import Enigma2Box, Enigma2MqttConfigEntry, decode_payload
from .const import (
    CAPABILITY_MENU_EDITOR,
    CONF_MENU,
    CONF_MENU_REMOTE_EDIT,
    DEFAULT_MENU_REMOTE_EDIT,
    DOMAIN,
    TOPIC_AVAILABILITY,
    TOPIC_INFO,
    TOPIC_MENU_CATALOGUE_PREFIX,
    TOPIC_MENU_EDIT_RESULT_PREFIX,
)
from .menu import (
    GROUP_ROWS,
    ITEM_ICON,
    ITEM_ID,
    ITEM_LABEL,
    KIND,
    KIND_GROUP,
    KIND_ROW,
    MAX_ENTRIES,
    MAX_GROUPS,
    MAX_LABEL,
    ROW_CONTROL,
    ROW_ENTITY,
    ReceiverMenu,
    allowed_controls,
    clean_label,
    control_allowed,
    encode_menu,
    group_icon,
    keeps_row_id,
    menu_ids,
    menu_revision,
    menu_rows,
    new_item_id,
    resolve_entity,
    row_draft,
    stored_binding,
    stored_menu,
    stored_row,
)

_LOGGER = logging.getLogger(__name__)

# The catalogue's version, and its bound: under 1 MiB, compact UTF-8 JSON (ADR-0013).
CATALOGUE_VERSION = 1
MAX_CATALOGUE_BYTES = 1024 * 1024
# Names of areas, cut like labels.
MAX_AREA = 60
# A registry change republishes the catalogue at most once every five seconds; a burst of
# them - an integration adding its entities - is one publish at the end of the window.
CATALOGUE_COOLDOWN = 5.0
# An edit is the menu and three short fields: the menu's own bounds keep it far below this,
# and anything larger is not read.
MAX_EDIT_BYTES = 64 * 1024
# One save at a time per receiver, and at most one accepted save every two seconds.
SAVE_INTERVAL = 2.0
# A refusal's `detail` is one English sentence of at most this many characters, and what it
# quotes from the edit is cut at this many before it is quoted.
MAX_DETAIL = 200
MAX_QUOTED = 60
# An integer in an edit with more digits than this is not one anything here reads: `ts` is
# the only number an edit carries, and it is only logged.
MAX_INTEGER_DIGITS = 20

EDIT_ID = re.compile(r"^[0-9a-f]{12}$")
REVISION = re.compile(r"^[0-9a-f]{16}$")

# The answers an edit gets.
RESULT_SAVED = "saved"
RESULT_STALE = "stale"
RESULT_INVALID = "invalid"
RESULT_BUSY = "busy"
RESULT_NOT_CLAIMED = "not_claimed"
RESULT_DISABLED = "disabled"

# Where the warning about entities left out of a catalogue was given in this run.
_TRIMMED_LOGGED = f"{DOMAIN}_menu_catalogue_trimmed"

# How the catalogue marks an entity that is hidden, and the categories it names.
_CATEGORIES = ("config", "diagnostic")


def _monotonic() -> float:
    """The clock the save interval counts by, apart so that a test can move it."""
    return time.monotonic()


def catalogue_topic(node_id: str) -> str:
    """The retained topic that carries one receiver's catalogue."""
    return f"{TOPIC_MENU_CATALOGUE_PREFIX}/{node_id}"


def result_topic(node_id: str) -> str:
    """The topic one receiver's edits are answered on."""
    return f"{TOPIC_MENU_EDIT_RESULT_PREFIX}/{node_id}"


async def async_retract_catalogue(hass: HomeAssistant, node_id: str) -> None:
    """Publish the empty retained payload: this receiver has no catalogue any more."""
    await mqtt.async_publish(hass, catalogue_topic(node_id), "", qos=1, retain=True)


def _cut(text: Any, limit: int) -> str:
    """Text on one line, cut at `limit` characters."""
    return " ".join(str(text).split())[:limit]


def quoted(value: Any) -> str:
    """Something an edit sent, as an answer's `detail` or a log line may show it.

    Cut first, then written as an ASCII literal: no line break, no control character and
    nothing outside ASCII from the edit reaches the answer or the log.
    """
    if isinstance(value, str):
        value = value[:MAX_QUOTED]
    return ascii(value)


def editing_allowed(options: Mapping[str, Any]) -> bool:
    """Whether the user allowed editing the menu from the receiver (off unless turned on)."""
    return options.get(CONF_MENU_REMOTE_EDIT, DEFAULT_MENU_REMOTE_EDIT) is True


# ---------------------------------------------------------------------- catalogue


def usable(hass: HomeAssistant, entity_id: Any) -> bool:
    """Whether a row may be bound to this entity now: an entity id with a state.

    A disabled entity has no state, and neither has one that does not exist; both are what
    the catalogue leaves out and what an edit is refused for.
    """
    return (
        isinstance(entity_id, str)
        and valid_entity_id(entity_id)
        and hass.states.get(entity_id) is not None
    )


def catalogue_menu(
    hass: HomeAssistant, menu: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The stored menu in the catalogue's form: each row bound to the entity id it names now.

    A row whose binding names no usable entity keeps its stored binding and says `missing`,
    so that the page can show it and send it back unchanged.
    """

    def _row(row: Mapping[str, Any]) -> dict[str, Any]:
        entity_id = resolve_entity(hass, row[ROW_ENTITY])
        item: dict[str, Any] = {
            "type": KIND_ROW,
            "id": row[ITEM_ID],
            "entity": entity_id if usable(hass, entity_id) else row[ROW_ENTITY],
            "control": row[ROW_CONTROL],
            "label": row.get(ITEM_LABEL),
            "icon": row.get(ITEM_ICON),
        }
        if not usable(hass, entity_id):
            item["missing"] = True
        return item

    items: list[dict[str, Any]] = []
    for item in menu:
        if item[KIND] == KIND_GROUP:
            items.append(
                {
                    "type": KIND_GROUP,
                    "id": item[ITEM_ID],
                    "label": item[ITEM_LABEL],
                    "icon": item[ITEM_ICON],
                    "rows": [_row(row) for row in item[GROUP_ROWS]],
                }
            )
        else:
            items.append(_row(item))
    return items


def catalogue_entities(hass: HomeAssistant) -> tuple[list[str], list[dict[str, Any]]]:
    """Every usable entity, sorted by name, and the names of the areas they are in.

    An entity's area is its own, else its device's. The name is the one a row bound to it
    shows when the user gives it none. The controls are exactly what the options flow
    offers for that entity now, the default first.
    """
    entities_registry = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    found: list[tuple[dict[str, Any], str | None]] = []
    for state in hass.states.async_all():
        entity_id = state.entity_id
        entry = entities_registry.async_get(entity_id)
        if entry is not None and entry.disabled_by is not None:
            continue
        controls = allowed_controls(hass, entity_id)
        if not controls:
            continue  # pragma: no cover - every entity can be an indicator
        item: dict[str, Any] = {"e": entity_id, "n": _cut(state.name, MAX_LABEL)}
        area_id: str | None = None
        if entry is not None:
            area_id = entry.area_id
            if area_id is None and entry.device_id is not None:
                device = devices.async_get(entry.device_id)
                area_id = device.area_id if device is not None else None
            if entry.hidden_by is not None:
                item["h"] = True
            if entry.entity_category is not None and (
                category := str(entry.entity_category.value)
            ) in _CATEGORIES:
                item["k"] = category
        item["c"] = controls
        found.append((item, area_id))

    names: dict[str, str] = {}
    for _item, area_id in found:
        if area_id is not None and area_id not in names:
            area = areas.async_get_area(area_id)
            if area is not None:
                names[area_id] = _cut(area.name, MAX_AREA)
    ordered = sorted(names, key=lambda area_id: (names[area_id], area_id))
    index = {area_id: position for position, area_id in enumerate(ordered)}

    entities: list[dict[str, Any]] = []
    for item, area_id in found:
        # The members in the contract's order: e, n, a, c, then h and k where they apply.
        shaped: dict[str, Any] = {
            "e": item["e"],
            "n": item["n"],
            "a": index.get(area_id) if area_id is not None else None,
            "c": item["c"],
        }
        for key in ("h", "k"):
            if key in item:
                shaped[key] = item[key]
        entities.append(shaped)
    entities.sort(key=lambda entity: (entity["n"], entity["e"]))
    return [names[area_id] for area_id in ordered], entities


def disabled_catalogue() -> dict[str, Any]:
    """The catalogue while editing from the receiver is off: nothing about any entity."""
    return {"v": CATALOGUE_VERSION, "editing": False}


def build_catalogue(hass: HomeAssistant, menu: list[dict[str, Any]]) -> dict[str, Any]:
    """The whole catalogue for one stored menu, before it is fitted under its bound."""
    areas, entities = catalogue_entities(hass)
    return {
        "v": CATALOGUE_VERSION,
        "editing": True,
        "revision": menu_revision(menu),
        "areas": areas,
        "left_out": 0,
        "menu": catalogue_menu(hass, menu),
        "entities": entities,
    }


def _leave_out_rank(position: int, entity: Mapping[str, Any]) -> tuple[int, int]:
    """Which entity goes first when the catalogue is too large (ADR-0013).

    Hidden ones first, then diagnostic, then config, then the rest - each from the end of
    the name order.
    """
    if entity.get("h"):
        tier = 0
    elif entity.get("k") == "diagnostic":
        tier = 1
    elif entity.get("k") == "config":
        tier = 2
    else:
        tier = 3
    return tier, -position


def fit_catalogue(payload: dict[str, Any]) -> tuple[bytes, int]:
    """The encoded catalogue under 1 MiB, and how many entities had to be left out for it.

    The menu is never cut: its own bounds keep it far below the limit. Neither is an entity
    the menu names - the page could not show what a row is bound to - so only the rest are
    left out, in the order of `_leave_out_rank`. Each entity is measured once; the sum is
    only an estimate of the separators and of `left_out`'s own digits, so the result is
    measured again until it fits.
    """
    encoded = encode_menu(payload)
    if len(encoded) < MAX_CATALOGUE_BYTES:
        return encoded, 0
    entities: list[dict[str, Any]] = payload["entities"]
    named = {
        row["entity"] for row in menu_rows(payload.get("menu") or []) if not row.get("missing")
    }
    order = sorted(
        (i for i, entity in enumerate(entities) if entity["e"] not in named),
        key=lambda i: _leave_out_rank(i, entities[i]),
    )
    sizes = [len(encode_menu(entity)) + 1 for entity in entities]
    left_out: set[int] = set()
    taken = 0
    while len(encoded) >= MAX_CATALOGUE_BYTES and taken < len(order):
        excess = len(encoded) - MAX_CATALOGUE_BYTES + 1
        while excess > 0 and taken < len(order):
            left_out.add(order[taken])
            excess -= sizes[order[taken]]
            taken += 1
        payload["entities"] = [
            entity for position, entity in enumerate(entities) if position not in left_out
        ]
        payload["left_out"] = len(left_out)
        encoded = encode_menu(payload)
    return encoded, len(left_out)


# --------------------------------------------------------------------------- edit


@dataclass(slots=True, frozen=True)
class MenuEdit:
    """One `menu_edit`, read as far as its envelope."""

    edit_id: str
    base: str
    menu: Any
    ts: Any = None


class EditRefused(Exception):
    """An edit that is not stored: the answer it gets, and what the log says about it.

    `result` None is an edit that gets no answer at all - one without an id to answer.
    """

    def __init__(
        self,
        cause: str,
        result: str | None,
        *,
        path: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(cause)
        self.cause = cause
        self.result = result
        self.path = path
        self.detail = detail[:MAX_DETAIL] if detail is not None else None


def _invalid(path: str, detail: str) -> EditRefused:
    return EditRefused("invalid", RESULT_INVALID, path=path, detail=detail)


def _integer(text: str) -> int | None:
    """A JSON integer, or None for one with more digits than anything here reads."""
    return int(text) if len(text.lstrip("-")) <= MAX_INTEGER_DIGITS else None


def _no_constant(_name: str) -> None:
    """`NaN` and `Infinity` are not JSON; read as nothing."""
    return


def _all_text_is_utf8(data: Any) -> bool:
    """Whether every text in the decoded edit, keys included, can be written as UTF-8.

    JSON may escape a lone surrogate (`\\ud800`) in perfectly valid UTF-8 bytes, and Python
    reads it into a text no UTF-8 encoder accepts: stored, it would break the revision, the
    publishers and the saving of every config entry. Walked without recursion; the parser
    has already bounded the depth.
    """
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return False
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return True


def decode_edit(payload: Any) -> Any:
    """The edit's JSON, or None for anything too large, not UTF-8 or not JSON.

    Also None for JSON that carries a text UTF-8 cannot hold. An integer with more than 20
    digits, `NaN` and `Infinity` are read as null rather than refusing the edit: the only
    number an edit carries is `ts`, which is only logged.
    """
    if isinstance(payload, (bytes, bytearray)) and len(payload) > MAX_EDIT_BYTES:
        return None
    text = decode_payload(payload)
    if text is None or len(text.encode("utf-8")) > MAX_EDIT_BYTES:
        return None
    try:
        data = json.loads(text, parse_int=_integer, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        # A nesting too deep for the parser is not an edit.
        return None
    return data if _all_text_is_utf8(data) else None


def edit_id_of(data: Any) -> str | None:
    """The id of a decoded edit, where one can be read from it; None otherwise.

    Read before anything else is checked, so that every refusal of an edit with a usable id
    can be answered.
    """
    if isinstance(data, dict) and isinstance(edit_id := data.get("id"), str):
        return edit_id if EDIT_ID.match(edit_id) else None
    return None


def _timestamp(raw: Any) -> float | None:
    """The edit's `ts` for the log: a finite number, else nothing."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) if math.isfinite(raw) else None


def parse_edit(data: Any) -> MenuEdit:
    """Read one decoded `menu_edit` as far as its envelope, or raise `EditRefused`.

    Anything that is not a JSON object with a usable id gets no answer; a usable id with a
    `base` that is not a revision is answered as invalid, with the path `base`.
    """
    if not isinstance(data, dict):
        raise EditRefused("unreadable", None)
    edit_id = data.get("id")
    if not isinstance(edit_id, str) or not EDIT_ID.match(edit_id):
        raise EditRefused("unreadable", None)
    base = data.get("base")
    if not isinstance(base, str) or not REVISION.match(base):
        raise EditRefused(
            "unreadable",
            RESULT_INVALID,
            path="base",
            detail="The edit does not say which revision of the menu it started from.",
        )
    return MenuEdit(edit_id, base, data.get("menu"), _timestamp(data.get("ts")))


class _EditReader:
    """Reads an edited menu into the stored form, with the options flow's rules."""

    def __init__(self, hass: HomeAssistant, stored: list[dict[str, Any]]) -> None:
        self.hass = hass
        self.stored_kinds: dict[str, str] = {}
        self.stored_rows: dict[str, dict[str, Any]] = {}
        for item in stored:
            self.stored_kinds[item[ITEM_ID]] = item[KIND]
            if item[KIND] == KIND_GROUP:
                for row in item[GROUP_ROWS]:
                    self.stored_kinds[row[ITEM_ID]] = KIND_ROW
                    self.stored_rows[row[ITEM_ID]] = row
            else:
                self.stored_rows[item[ITEM_ID]] = item
        # Every id the stored menu or this edit uses: a new id is none of them, so a removed
        # row's id is never handed to a new one.
        self.taken: set[str] = set(menu_ids(stored))
        self.seen: set[str] = set()

    def read(self, menu: Any) -> list[dict[str, Any]]:
        if not isinstance(menu, list):
            raise _invalid("menu", "The menu must be a list.")
        if len(menu) > MAX_ENTRIES:
            raise _invalid(
                "menu", f"The top list holds at most {MAX_ENTRIES} entries, not {len(menu)}."
            )
        result: list[dict[str, Any]] = []
        groups = 0
        for index, item in enumerate(menu):
            path = f"menu[{index}]"
            kind = item.get("type") if isinstance(item, dict) else None
            if not isinstance(item, dict):
                raise _invalid(path, "An entry must be a JSON object.")
            if kind == KIND_GROUP:
                groups += 1
                if groups > MAX_GROUPS:
                    raise _invalid(path, f"A menu holds at most {MAX_GROUPS} groups.")
                result.append(self._group(item, path))
            elif kind == KIND_ROW:
                result.append(self._row(item, path))
            else:
                raise _invalid(f"{path}.type", 'An entry is of type "group" or "row".')
        return result

    def _id(self, raw: Any, kind: str, path: str) -> str | None:
        """The stored id an entry claims, checked; None for a new entry."""
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise _invalid(f"{path}.id", "An id is a text, or null for a new entry.")
        if raw in self.seen:
            raise _invalid(f"{path}.id", f"The id {quoted(raw)} appears more than once.")
        stored_kind = self.stored_kinds.get(raw)
        if stored_kind is None:
            raise _invalid(
                f"{path}.id",
                f"The id {quoted(raw)} is not in the stored menu; a new entry carries null.",
            )
        if stored_kind != kind:
            raise _invalid(
                f"{path}.id", f"The id {quoted(raw)} is a {stored_kind}'s, not a {kind}'s."
            )
        self.seen.add(raw)
        return raw

    def _new_id(self, kind: str) -> str:
        item_id = new_item_id(kind, self.taken)
        self.taken.add(item_id)
        return item_id

    def _group(self, item: dict[str, Any], path: str) -> dict[str, Any]:
        group_id = self._id(item.get("id"), KIND_GROUP, path)
        raw_label = item.get("label")
        if not isinstance(raw_label, str):
            raise _invalid(f"{path}.label", "A group's name is a text.")
        if not (label := clean_label(raw_label)):
            raise _invalid(f"{path}.label", "A group needs a name.")
        rows = item.get("rows")
        if not isinstance(rows, list):
            raise _invalid(f"{path}.rows", "A group's rows are a list.")
        if len(rows) > MAX_ENTRIES:
            raise _invalid(
                f"{path}.rows", f"A group holds at most {MAX_ENTRIES} rows, not {len(rows)}."
            )
        read_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            row_path = f"{path}.rows[{index}]"
            if not isinstance(row, dict):
                raise _invalid(row_path, "A row must be a JSON object.")
            if row.get("type") != KIND_ROW:
                raise _invalid(
                    f"{row_path}.type", "A group holds rows only; groups are one level deep."
                )
            read_rows.append(self._row(row, row_path))
        return {
            KIND: KIND_GROUP,
            ITEM_ID: group_id if group_id is not None else self._new_id(KIND_GROUP),
            ITEM_LABEL: label,
            ITEM_ICON: group_icon(item.get("icon")),
            GROUP_ROWS: read_rows,
        }

    def _row(self, item: dict[str, Any], path: str) -> dict[str, Any]:
        row_id = self._id(item.get("id"), KIND_ROW, path)
        existing = self.stored_rows.get(row_id) if row_id is not None else None
        entity = item.get("entity")
        control = item.get("control")
        if not isinstance(control, str):
            raise _invalid(f"{path}.control", "A row's control is a text.")
        # The entity the control is checked against: one that is usable now, either named
        # by the edit or named by the row's own stored binding, sent back as the catalogue
        # gave it - which lets a row whose entity is missing survive an edit of the rest.
        checked: str | None
        if usable(self.hass, entity):
            binding = stored_binding(self.hass, entity)
            checked = entity
        elif existing is not None and entity == existing[ROW_ENTITY]:
            binding = existing[ROW_ENTITY]
            resolved = resolve_entity(self.hass, binding)
            checked = resolved if usable(self.hass, resolved) else None
        elif isinstance(entity, str):
            raise _invalid(
                f"{path}.entity", f"{quoted(entity)} is not an entity Home Assistant has now."
            )
        else:
            raise _invalid(f"{path}.entity", "A row's entity is an entity id.")
        raw_label = item.get("label")
        if raw_label is not None and not isinstance(raw_label, str):
            raise _invalid(f"{path}.label", "A row's name is a text, or null for none.")
        unchanged = keeps_row_id(self.hass, existing, binding, control)
        # A row sent back as it is stored is taken as it is, as the options flow keeps a row
        # nobody changed - even when its entity no longer offers that control. A new row and
        # a changed one are checked against what the entity offers now.
        if not unchanged:
            if checked is None:
                raise _invalid(
                    f"{path}.control",
                    "This row's entity is missing, so its control cannot be changed; bind "
                    "it to an entity first.",
                )
            self._check_control(checked, control, path)
        if unchanged:
            assert row_id is not None
            kept = row_id
        else:
            kept = self._new_id(KIND_ROW)
        return stored_row(kept, row_draft(binding, raw_label, item.get("icon")), control)

    def _check_control(self, entity_id: str, control: str, path: str) -> None:
        if not control_allowed(self.hass, entity_id, control):
            offered = ", ".join(allowed_controls(self.hass, entity_id))
            raise _invalid(
                f"{path}.control",
                f"{entity_id} cannot be shown as {quoted(control)}; it can be: {offered}.",
            )


def read_edited_menu(
    hass: HomeAssistant, stored: list[dict[str, Any]], menu: Any
) -> list[dict[str, Any]]:
    """The edited menu in the form the options store it, or `EditRefused` as invalid.

    The options flow's rules, by the same functions (`menu.py`): eight groups, 24 entries
    in the top list and in every group, one level; names on one line and cut at 60, a
    group's required; icons from the plugin's list or the default; a row's entity one Home
    Assistant has now - or, for a stored row, exactly its stored binding, so that a row
    whose entity is missing survives - and a control that entity may be shown as. Ids that
    are not null must be the stored menu's, of the same kind, each at most once. A row keeps
    its id while its entity and its control stay; otherwise, and when new, it gets a new
    one. A group keeps its id. A row sent back exactly as it is stored is taken without the
    control check, as the options flow keeps a row nobody changed.
    """
    return _EditReader(hass, stored).read(menu)


# ---------------------------------------------------------------------- publisher


class MenuCreator:
    """One receiver's catalogue and edits, while the entry is loaded."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Enigma2MqttConfigEntry,
        box: Enigma2Box,
        receiver_menu: ReceiverMenu,
    ) -> None:
        """Nothing is published or listened to until `async_start`.

        Built before the box subscribes, so that its handler is in place for the first
        `menu_edit` the broker delivers.
        """
        self.hass = hass
        self.entry = entry
        self.box = box
        self.receiver_menu = receiver_menu
        self._unsubscribes: list[CALLBACK_TYPE] = []
        self._unlisten_registries: list[CALLBACK_TYPE] = []
        self._debouncer: Debouncer[Any] = Debouncer(
            hass,
            _LOGGER,
            cooldown=CATALOGUE_COOLDOWN,
            immediate=False,
            function=self._async_publish_debounced,
        )
        # Whether the receiver claims `menu_editor`, as this run last read it, and whether
        # the empty payload was published for a receiver that says it does not.
        self._claimed = False
        self._retracted = False
        self._was_available = False
        # The catalogue last published; an unchanged one is not published again.
        self._published: bytes | None = None
        # One reconcile waiting to run, and whether any of the calls it stands for forced a
        # publish: a burst of messages at start is one publish, not one per message.
        self._pending: asyncio.Task[None] | None = None
        self._pending_force = False
        self._saving = False
        self._last_saved: float | None = None
        self._logged_causes: set[str] = set()
        self._stopped = False

    @property
    def topic(self) -> str:
        """Where this receiver's catalogue is published."""
        return catalogue_topic(self.box.node_id)

    @property
    def revision(self) -> str:
        """The revision of the menu the options hold now."""
        return menu_revision(stored_menu(self.entry.options))

    @property
    def claimed(self) -> bool:
        """Whether the receiver claims the capability now."""
        return CAPABILITY_MENU_EDITOR in self.box.capabilities

    @property
    def editing(self) -> bool:
        """Whether the user allowed editing from the receiver."""
        return editing_allowed(self.entry.options)

    async def async_start(self) -> None:
        """Publish the catalogue if the receiver claims it, and follow what changes it."""
        self._was_available = self.box.available
        self._unsubscribes.append(
            self.box.async_add_listener(
                self._box_changed, topics=(TOPIC_INFO, TOPIC_AVAILABILITY)
            )
        )
        self._unsubscribes.append(
            mqtt.async_subscribe_connection_status(self.hass, self._connection_changed)
        )
        if self.claimed:
            await self._async_reconcile(force=True)
        elif self.box.capabilities_declared:
            # Nothing to hurry for: the retraction below waits like a registry change.
            self._debouncer.async_schedule_call()
        if self.hass.state is not CoreState.running:
            # Entities get their states while Home Assistant starts; a catalogue built
            # during the start lists only those that had one. Built again once it has
            # started - and not at once on a Home Assistant that is running already.
            self._unsubscribes.append(async_at_started(self.hass, self._started))

    @callback
    def async_stop(self) -> None:
        """Stop following the receiver and the registries. The retained catalogue stays.

        A reload republishes it at once; removing the entry and reconfiguring to another
        node retract it.
        """
        self._stopped = True
        while self._unsubscribes:
            self._unsubscribes.pop()()
        self._stop_listening()
        self._debouncer.async_shutdown()

    # ------------------------------------------------------------- triggers

    @callback
    def _listen(self) -> None:
        if self._unlisten_registries:
            return
        for event in (
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            ar.EVENT_AREA_REGISTRY_UPDATED,
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
        ):
            self._unlisten_registries.append(
                self.hass.bus.async_listen(event, self._registry_changed)
            )

    @callback
    def _stop_listening(self) -> None:
        while self._unlisten_registries:
            self._unlisten_registries.pop()()

    @callback
    def _registry_changed(self, _event: Event[Any]) -> None:
        self._debouncer.async_schedule_call()

    async def _async_publish_debounced(self) -> None:
        await self._async_reconcile(force=False)

    @callback
    def _started(self, _hass: HomeAssistant) -> None:
        if self.claimed:
            self._schedule(force=False)

    @callback
    def _box_changed(self) -> None:
        """Publish when the claim appears or the receiver comes back; retract when it goes."""
        available = self.box.available
        came_back = available and not self._was_available
        self._was_available = available
        claimed = self.claimed
        if claimed and (not self._claimed or came_back):
            self._schedule(force=True)
        elif not claimed and not self._retracted and self.box.capabilities_declared:
            # A retraction is not urgent - the page has nothing to save with it - so it
            # waits like a registry change, at most five seconds; a claim that comes back
            # meanwhile publishes instead.
            self._debouncer.async_schedule_call()

    @callback
    def _connection_changed(self, connected: bool) -> None:
        """Publish again when the broker connection comes back - it may have lost it."""
        if connected and self.claimed:
            self._schedule(force=True)

    @callback
    def _schedule(self, *, force: bool) -> None:
        """Reconcile soon, once for every call made before it runs.

        Not started eagerly: this runs inside the dispatch of a message, and a publish made
        there would be made in the middle of the receiver's burst.
        """
        if self._stopped:
            return
        self._pending_force = self._pending_force or force
        if self._pending is not None:
            return
        self._pending = self.entry.async_create_task(
            self.hass,
            self._async_run_pending(),
            f"{DOMAIN} menu catalogue {self.box.node_id}",
            eager_start=False,
        )

    async def _async_run_pending(self) -> None:
        force, self._pending_force = self._pending_force, False
        self._pending = None
        await self._async_reconcile(force=force)

    # ------------------------------------------------------------- publishing

    async def _async_reconcile(self, *, force: bool) -> None:
        """Publish the catalogue while the receiver claims it, and retract it when it stops.

        A receiver that has said what it can do, and does not name `menu_editor`, gets the
        empty payload once in this run, so that a catalogue left by an earlier run - before
        the permission was turned off on the receiver, say - does not stay on the broker.
        A receiver that has not said anything yet is left alone. The registries are followed
        only while there is a full catalogue to keep up to date.
        """
        if self._stopped:
            return
        if self.claimed:
            self._claimed = True
            self._retracted = False
            if self.editing:
                self._listen()
            else:
                self._stop_listening()
            await self._async_publish(force=force)
            return
        if not self.box.capabilities_declared:
            return
        self._claimed = False
        self._stop_listening()
        if self._retracted:
            return
        self._retracted = True
        self._published = b""
        try:
            await async_retract_catalogue(self.hass, self.box.node_id)
        except HomeAssistantError as error:
            self._retracted = False
            self._published = None
            _LOGGER.debug("Could not retract the catalogue of %s: %s", self.box.node_id, error)

    async def _async_publish(self, *, force: bool) -> None:
        """Build the catalogue, fit it under 1 MiB, and publish it unless it is unchanged.

        While editing from the receiver is off, the catalogue says only that.
        """
        left_out = 0
        if self.editing:
            payload = build_catalogue(self.hass, stored_menu(self.entry.options))
            encoded, left_out = fit_catalogue(payload)
        else:
            encoded = encode_menu(disabled_catalogue())
        if encoded == self._published and not force:
            return
        if left_out:
            logged: set[str] = self.hass.data.setdefault(_TRIMMED_LOGGED, set())
            level = logging.DEBUG if self.box.node_id in logged else logging.WARNING
            logged.add(self.box.node_id)
            _LOGGER.log(
                level,
                "The menu catalogue of %s leaves out %s entities to stay under 1 MiB: "
                "hidden ones first, then diagnostic, then config, then the rest by name; "
                "none that the menu uses",
                self.box.node_id,
                left_out,
            )
        self._published = encoded
        try:
            await mqtt.async_publish(
                self.hass, self.topic, encoded.decode("utf-8"), qos=1, retain=True
            )
        except HomeAssistantError as error:
            # Published again on the next change, or when the receiver comes back.
            self._published = None
            _LOGGER.debug("Could not publish the catalogue of %s: %s", self.box.node_id, error)

    # ------------------------------------------------------------------ edits

    @callback
    def async_edit_received(self, msg: ReceiveMessage) -> None:
        """Check one `menu_edit` in the contract's order, and store it when it may be.

        Retained, ignored; then `not_claimed`; then unreadable (no answer, or `invalid` for
        a bad `base`); then `disabled` while editing from the receiver is off; then `busy`,
        `stale` and the menu's own rules.
        """
        if msg.retain:
            # Somebody's leftover: storing it at every reconnect would edit for nobody.
            self._log_refusal(EditRefused("retained", None))
            return
        data = decode_edit(msg.payload)
        edit_id = edit_id_of(data)
        try:
            if not self.claimed:
                raise EditRefused("not_claimed", RESULT_NOT_CLAIMED)
            edit = parse_edit(data)
            if not self.editing:
                raise EditRefused("disabled", RESULT_DISABLED)
            now = _monotonic()
            if self._saving or (
                self._last_saved is not None and now - self._last_saved < SAVE_INTERVAL
            ):
                raise EditRefused("busy", RESULT_BUSY)
            stored = stored_menu(self.entry.options)
            if edit.base != menu_revision(stored):
                raise EditRefused("stale", RESULT_STALE)
            menu = read_edited_menu(self.hass, stored, edit.menu)
        except EditRefused as refusal:
            self._log_refusal(refusal)
            if edit_id is not None and refusal.result is not None:
                self._answer(edit_id, refusal.result, refusal.path, refusal.detail)
            return
        self._saving = True
        self._last_saved = now
        _LOGGER.debug(
            "Storing the menu edit %s from %s (ts %s)", edit.edit_id, self.box.node_id, edit.ts
        )
        options = dict(self.entry.options)
        if menu:
            options[CONF_MENU] = menu
        else:
            options.pop(CONF_MENU, None)
        # Stored without a reload: nothing goes unavailable for a menu change. This entry
        # has no update listener - the options flow reloads it itself - so nothing else
        # follows from the update.
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.receiver_menu.async_menu_changed()
        self.entry.async_create_task(
            self.hass,
            self._async_after_save(edit.edit_id),
            f"{DOMAIN} menu_edit {self.box.node_id}",
        )

    async def _async_after_save(self, edit_id: str) -> None:
        """Publish the new catalogue, then answer; the next save may come after that."""
        try:
            if not self._stopped and self.claimed:
                await self._async_publish(force=False)
            await self._async_publish_answer(edit_id, RESULT_SAVED, None, None)
        finally:
            self._saving = False

    @callback
    def _answer(
        self, edit_id: str, result: str, path: str | None, detail: str | None
    ) -> None:
        self.entry.async_create_task(
            self.hass,
            self._async_publish_answer(edit_id, result, path, detail),
            f"{DOMAIN} menu_edit_result {self.box.node_id}",
        )

    async def _async_publish_answer(
        self, edit_id: str, result: str, path: str | None, detail: str | None
    ) -> None:
        """Answer one edit, with the revision the stored menu has now."""
        payload = {
            "id": edit_id,
            "result": result,
            "revision": self.revision,
            "path": path,
            "detail": detail,
        }
        try:
            await mqtt.async_publish(
                self.hass,
                result_topic(self.box.node_id),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                qos=1,
                retain=False,
            )
        except HomeAssistantError as error:
            _LOGGER.debug(
                "Could not answer the menu edit %s of %s: %s", edit_id, self.box.node_id, error
            )

    def _log_refusal(self, refusal: EditRefused) -> None:
        """A refusal is logged as a warning the first time for its cause, then quietly.

        The path is built from indices and the detail quotes the edit as ASCII literals, so
        nothing an edit sent can start a line of its own in the log.
        """
        level = logging.DEBUG if refusal.cause in self._logged_causes else logging.WARNING
        self._logged_causes.add(refusal.cause)
        _LOGGER.log(
            level,
            "Refused a menu edit from %s (%s%s); nothing was stored",
            self.box.node_id,
            refusal.cause,
            f": {refusal.path}: {refusal.detail}" if refusal.path else "",
        )

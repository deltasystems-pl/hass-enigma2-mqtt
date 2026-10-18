"""A list of choices on the television, put there by Home Assistant and answered by row id.

`enigma2_mqtt.show_list` publishes `cmd/list`: a title, an optional text and up to 24
labelled rows, each with an id. The viewer picks one with the remote, and the receiver says
which row id was picked - once - on `list_answer`, or that the list timed out, was dismissed
with EXIT, was replaced by a newer list or was closed by standby or the plugin stopping. The
receiver never learns what a row does: whatever is done with the answer is done here, by the
automation that asked (ADR-0014).

Three things are held here:

- **The request** is checked against the contract's rules before anything is published, so
  the caller reads what is wrong in the dialog they are looking at, in their language, and
  not on `last_error`. The list's id is the command's id, sixteen random hexadecimal digits
  in the envelope; a list never goes out without one, because nothing could match its answer.
- **The answer is untrusted.** Anybody who can publish on the broker can send one, for any
  list. It is read strictly - an id of the contract's shape, an ending it names, a row of the
  shape that ending takes - and anything else is dropped with one log line. An answer to a
  list this Home Assistant sent must also name a row that list had, and for a timeout the
  row it was sent as the default. A second answer with the same id is a duplicate delivery or
  somebody else's, and is dropped: a list is answered once.
- **The wait** of a caller who asked for the answer ends with that answer, with the
  receiver's refusal on `last_error` (matched by the list's id and nothing else), when the
  entry is unloaded, or as `no_answer` fifteen seconds after the list's own timeout. The
  receiver never queues an answer across a disconnect - a late "open the gate" is worse than
  none - so a choice made while the broker was away is lost, and the wait says so. The
  receiver going offline does not end it sooner: the list stays open on the television, and
  a choice made after the receiver is back is still answered.

Every readable answer is also the bus event `enigma2_mqtt_list_answer`, which the device
triggers listen to - for any list, also one this Home Assistant did not send; the row's label
is in it only for a list sent from here.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import json
import logging
import math
import re
from typing import TYPE_CHECKING, Any
import unicodedata

from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import translation
from homeassistant.helpers.event import async_call_later

from .box import (
    Enigma2CommandError,
    Enigma2CommandRefused,
    command_envelope,
    decode_payload,
    new_command_id,
    refusal_id,
)
from .const import (
    ATTR_NODE_ID,
    CAPABILITY_LIST,
    COMMAND_LIST,
    COMMAND_MAX_BYTES,
    DOMAIN,
    ERROR_TEXT_MAX,
    EVENT_LIST_ANSWER,
    LIST_ANSWER_GRACE,
    LIST_DEFAULT_TIMEOUT,
    LIST_ENDINGS,
    LIST_KEYS,
    LIST_LABEL_MAX,
    LIST_NO_ANSWER,
    LIST_PICKED,
    LIST_REFUSAL_REASONS,
    LIST_REFUSED,
    LIST_ROW_ID_CHARACTERS,
    LIST_ROW_ID_MAX,
    LIST_ROWS_MAX,
    LIST_ROWS_MIN,
    LIST_TEXT_MAX,
    LIST_TIMEOUT,
    LIST_TIMEOUT_MAX,
    LIST_TIMEOUT_MIN,
    LIST_TITLE_MAX,
)

if TYPE_CHECKING:
    from .box import Enigma2Box

_LOGGER = logging.getLogger(__name__)

ROW_ID = re.compile(rf"[{LIST_ROW_ID_CHARACTERS}]{{1,{LIST_ROW_ID_MAX}}}")
# The id an answer says back is the command's id exactly as it was sent - by this Home
# Assistant or by any other client - so it is held to the plugin's rule for a command's id:
# 1 to 64 characters out of the same set as a row id.
ANSWER_ID = re.compile(rf"[{LIST_ROW_ID_CHARACTERS}]{{1,64}}")
# An answer is four short members. Anything longer is not one, and is not parsed.
ANSWER_MAX_BYTES = 1024
# The ids of the answers last read, so that the same answer delivered twice - QoS 1 may - or
# a copy somebody else publishes is not a second answer to one list.
ANSWERS_REMEMBERED = 64

# The members a row has. Anything else is refused by name: a misspelt `key` would otherwise
# vanish without a word, and the row would have no shortcut.
ROW_MEMBERS = ("id", "label", "key", "icon")

ATTR_TITLE = "title"
ATTR_TEXT = "text"
ATTR_ROWS = "rows"
ATTR_TIMEOUT = "timeout"
ATTR_DEFAULT = "default"
ATTR_LIST_ID = "list_id"
ATTR_ENDING = "ending"
ATTR_ROW = "row"
ATTR_LABEL = "label"
ATTR_ERROR = "error"


def _invalid(translation_key: str, /, **placeholders: Any) -> ServiceValidationError:
    """Return a translated complaint about how the action was called."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=translation_key,
        translation_placeholders={name: str(value) for name, value in placeholders.items()}
        or None,
    )


def _failed(translation_key: str, /, **placeholders: Any) -> HomeAssistantError:
    """Return a translated failure of a list that was sent, or could not be."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=translation_key,
        translation_placeholders={name: str(value) for name, value in placeholders.items()}
        or None,
    )


def strip_backslashes(text: str) -> str:
    """`text` without a backslash, the first step of the plugin's rule for screen text.

    Every escape enigma2's renderer knows begins with one, so the plugin removes them all.
    """
    return text.replace("\\", "")


def _screen_characters(text: str, *, keep_line_breaks: bool) -> str:
    """Drop every format character and lone surrogate; make every control a space.

    Cf is the bidirectional overrides and isolates, the zero-width space and joiners, the
    soft hyphen and the byte order mark: they draw nothing and can make a row read as
    something it is not. Cs is half a surrogate pair, which no UTF-8 holds and which the
    image's renderer does not survive. Cc, Zl and Zp become a space - a line break too,
    except in the text under the title, which may have two lines.
    """
    pieces: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if category in ("Cf", "Cs"):
            continue
        if keep_line_breaks and character == "\n":
            pieces.append(character)
        elif category in ("Cc", "Zl", "Zp"):
            pieces.append(" ")
        else:
            pieces.append(character)
    return "".join(pieces)


def clean_line(text: str, limit: int) -> str:
    """A title or a label exactly as the receiver shows it - the plugin's `clean_line`.

    In this order: every backslash removed; every Cf and Cs character dropped; every Cc, Zl and Zp
    character a space; cut at `limit`; the ends stripped. Empty after that is blank. Done
    here before anything else is judged, so that a list this side accepts is never refused
    by the receiver as blank, and the label reported back is the one on the screen.
    """
    return _screen_characters(strip_backslashes(text), keep_line_breaks=False)[:limit].strip()


def clean_lines(text: str, limit: int) -> str:
    """The text under the title as the receiver shows it - the plugin's `clean_lines`.

    As `clean_line`, except that `\\r\\n` and `\\r` become `\\n` after the backslashes
    go, and a `\\n` stays a line break.
    """
    text = strip_backslashes(text).replace("\r\n", "\n").replace("\r", "\n")
    return _screen_characters(text, keep_line_breaks=True)[:limit].strip()


# --------------------------------------------------------------------- the request --


@dataclass(frozen=True, slots=True)
class ListRow:
    """One row as it is published: its id, its label and, optionally, a key and an icon."""

    row_id: str
    label: str
    key: str | None = None
    icon: str | None = None

    def wire(self) -> dict[str, str]:
        """The row in the payload; a key or an icon it does not have is left out."""
        row = {"id": self.row_id, "label": self.label}
        if self.key is not None:
            row["key"] = self.key
        if self.icon is not None:
            row["icon"] = self.icon
        return row


@dataclass(frozen=True, slots=True)
class ListRequest:
    """A list that passed every rule of the contract, ready to be published."""

    title: str
    rows: tuple[ListRow, ...]
    timeout: int = LIST_DEFAULT_TIMEOUT
    text: str | None = None
    default: str | None = None

    def payload(self) -> str:
        """The `cmd/list` payload: the timeout always written out, so both sides agree on it."""
        body: dict[str, Any] = {"title": self.title}
        if self.text is not None:
            body["text"] = self.text
        body["rows"] = [row.wire() for row in self.rows]
        body["timeout"] = self.timeout
        if self.default is not None:
            body["default"] = self.default
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    def label_of(self, row_id: str | None) -> str | None:
        """The label of a row of this list, None for no row or one it does not have."""
        for row in self.rows:
            if row.row_id == row_id:
                return row.label
        return None


def _as_text(value: Any) -> str | None:
    """A text the caller gave, or None when it is not one.

    A number counts: a template renders `{{ 1 }}` as an integer, and YAML reads `key: 1`
    as one, and both mean the text `1`. A boolean does not - `yes` in YAML is `True`, which
    is not what anybody meant as a label - and neither does a number that is not finite:
    `nan`, `inf`, or an integer too large to be one, which is a template's mistake rather
    than a text anybody wrote.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        return str(value) if finite else None
    return None


def _screen_text(
    value: Any, limit: int, member: str, row: int | None = None, *, lines: bool = False
) -> str:
    """A title, text or label as the receiver will show it (`clean_line`, `clean_lines`)."""
    if (text := _as_text(value)) is None:
        if row is None:
            raise _invalid("list_member_not_text", member=member)
        raise _invalid("list_row_member_not_text", row=row, member=member)
    return clean_lines(text, limit) if lines else clean_line(text, limit)


def _timeout(value: Any) -> int:
    """Whole seconds from 5 to 600; a number with a fraction, a text or a boolean is refused."""
    if value is None:
        return LIST_DEFAULT_TIMEOUT
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not LIST_TIMEOUT_MIN <= value <= LIST_TIMEOUT_MAX
    ):
        raise _invalid("list_timeout_invalid", min=LIST_TIMEOUT_MIN, max=LIST_TIMEOUT_MAX)
    return value


def _row(raw: Any, number: int) -> ListRow:
    """One row, checked; `number` counts from 1, as the caller reads the list."""
    if not isinstance(raw, Mapping):
        raise _invalid("list_row_not_mapping", row=number)
    if unknown := sorted(str(member) for member in raw if member not in ROW_MEMBERS):
        raise _invalid("list_row_member_unknown", row=number, member=unknown[0])
    row_id = _as_text(raw.get("id"))
    if row_id is None or not ROW_ID.fullmatch(row_id):
        raise _invalid("list_row_id_invalid", row=number, max=LIST_ROW_ID_MAX)
    if raw.get("label") is None:
        raise _invalid("list_row_label_blank", row=number)
    label = _screen_text(raw["label"], LIST_LABEL_MAX, "label", number)
    if not label:
        raise _invalid("list_row_label_blank", row=number)
    key: str | None = None
    if raw.get("key") is not None:
        key = _as_text(raw["key"])
        if key not in LIST_KEYS:
            raise _invalid("list_row_key_invalid", row=number)
    icon: str | None = None
    if raw.get("icon") is not None:
        if not isinstance(raw["icon"], str):
            raise _invalid("list_row_member_not_text", row=number, member="icon")
        # A name the plugin does not know is drawn as the neutral icon, not refused: a newer
        # plugin may know more names than this release lists. An empty one is no icon.
        icon = raw["icon"] or None
    return ListRow(row_id=row_id, label=label, key=key, icon=icon)


def build_list(data: Mapping[str, Any]) -> ListRequest:
    """Check an action's fields against the contract and return the list to publish.

    Raises `ServiceValidationError`, translated, naming the field - and for a row its
    number, counted from 1 - and never repeating a label or a title back.
    """
    if data.get(ATTR_TITLE) is None:
        raise _invalid("list_title_blank")
    title = _screen_text(data[ATTR_TITLE], LIST_TITLE_MAX, ATTR_TITLE)
    if not title:
        raise _invalid("list_title_blank")

    text: str | None = None
    if data.get(ATTR_TEXT) is not None:
        text = _screen_text(data[ATTR_TEXT], LIST_TEXT_MAX, ATTR_TEXT, lines=True) or None

    raw_rows = data.get(ATTR_ROWS)
    if not isinstance(raw_rows, (list, tuple)):
        raise _invalid("list_rows_not_list")
    if not LIST_ROWS_MIN <= len(raw_rows) <= LIST_ROWS_MAX:
        raise _invalid(
            "list_rows_count", count=len(raw_rows), min=LIST_ROWS_MIN, max=LIST_ROWS_MAX
        )
    rows: list[ListRow] = []
    ids: set[str] = set()
    keys: set[str] = set()
    for number, raw in enumerate(raw_rows, start=1):
        row = _row(raw, number)
        # Case matters, as it does on the receiver: `Open` and `open` are two rows.
        if row.row_id in ids:
            raise _invalid("list_row_id_duplicate", row=number, id=row.row_id)
        if row.key is not None and row.key in keys:
            raise _invalid("list_row_key_duplicate", row=number, key=row.key)
        ids.add(row.row_id)
        if row.key is not None:
            keys.add(row.key)
        rows.append(row)

    timeout = _timeout(data.get(ATTR_TIMEOUT))

    default: str | None = None
    if data.get(ATTR_DEFAULT) is not None:
        default = _as_text(data[ATTR_DEFAULT])
        if default is None or default not in ids:
            raise _invalid("list_default_unknown")

    request = ListRequest(
        title=title, rows=tuple(rows), timeout=timeout, text=text, default=default
    )
    # The size in the envelope, which is the same for every id: all are 16 digits. Checked
    # with the fields, because it is the list that is too long, not the receiver. Counted in
    # UTF-8, as the broker carries it: the envelope escapes no character outside ASCII.
    try:
        size = len(command_envelope(new_command_id(), request.payload()).encode("utf-8"))
    except UnicodeEncodeError:
        # Half a surrogate pair where the text rule does not reach - an icon's name.
        raise _invalid("list_not_utf8") from None
    if size > COMMAND_MAX_BYTES:
        raise _invalid("list_too_long", size=size, limit=COMMAND_MAX_BYTES)
    return request


def check_can_show(box: Enigma2Box) -> None:
    """Refuse a list the receiver cannot show, before its fields are even read.

    It needs the `list` capability and the envelope a command's id travels in, which `list`
    implies: a list without an id could be answered by nobody this side could tell. And the
    receiver has to be on the broker - a list published to one that is not is heard by
    nobody, and its caller would wait out the whole timeout to learn that.
    """
    if CAPABILITY_LIST not in box.capabilities or not box.takes_command_ids:
        raise _invalid("list_unsupported")
    if not box.available:
        raise _invalid("list_offline")


def answer_response(ending: str, row: str | None, label: str | None) -> dict[str, Any]:
    """What the action returns to a caller that asked for the answer."""
    return {ATTR_ENDING: ending, ATTR_ROW: row, ATTR_LABEL: label}


async def async_refused_response(hass: HomeAssistant, error: HomeAssistantError) -> dict[str, Any]:
    """One receiver's failure as its own entry in a response for several receivers.

    Home Assistant gathers an entity action's calls and raises the first exception, which
    would throw away the answers of every receiver that did answer. So with several targets
    a receiver that refuses the list, or cannot be reached, says so in its own entry -
    `ending: refused`, which only this side says, and the sentence it would have raised, in
    the installation's language.
    """
    return {
        ATTR_ENDING: LIST_REFUSED,
        ATTR_ERROR: await async_error_sentence(hass, error),
        ATTR_ROW: None,
        ATTR_LABEL: None,
    }


async def async_error_sentence(hass: HomeAssistant, error: HomeAssistantError) -> str:
    """The sentence an error stands for, in the installation's language where it has one."""
    domain, key = error.translation_domain, error.translation_key
    if domain and key:
        texts = await translation.async_get_translations(
            hass, hass.config.language, "exceptions", [domain]
        )
        if message := texts.get(f"component.{domain}.exceptions.{key}.message"):
            try:
                return message.format(**(error.translation_placeholders or {}))
            except (KeyError, IndexError, ValueError):
                return message
    return str(error)


NO_ANSWER: dict[str, Any] = answer_response(LIST_NO_ANSWER, None, None)


# ---------------------------------------------------------------------- the answer --


class Dropped(Exception):
    """An answer that is not read, and the cause logged for it."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(cause)
        self.cause = cause
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ListAnswer:
    """One `list_answer`, read and checked against the contract's shape."""

    list_id: str | None
    ending: str
    row: str | None


def parse_answer(payload: Any) -> ListAnswer:
    """Read one `list_answer`, or raise `Dropped` with what was wrong with it.

    `id` is optional - absent, not `null`, for a list sent without one. `row` is the picked
    row's id for `picked`, the default row's id or `null` for `timeout`, and `null` for
    every other ending; a member this release does not know, `ts` among them, is ignored.
    The detail of a `Dropped` names a member, never what it held.
    """
    if isinstance(payload, (bytes, bytearray)) and len(payload) > ANSWER_MAX_BYTES:
        raise Dropped("unreadable", f"{len(payload)} bytes")
    if (text := decode_payload(payload)) is None or len(text) > ANSWER_MAX_BYTES:
        raise Dropped("unreadable", "empty, too long or not UTF-8")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise Dropped("unreadable", "not JSON") from None
    if not isinstance(data, dict):
        raise Dropped("unreadable", "not a JSON object")
    list_id: str | None = None
    if "id" in data:
        list_id = data["id"]
        if not isinstance(list_id, str) or not ANSWER_ID.fullmatch(list_id):
            raise Dropped("malformed", "id")
    ending = data.get("ending")
    if not isinstance(ending, str) or ending not in LIST_ENDINGS:
        raise Dropped("malformed", "ending")
    row = data.get("row")
    if ending == LIST_PICKED:
        if not isinstance(row, str) or not ROW_ID.fullmatch(row):
            raise Dropped("malformed", "row")
    elif ending == LIST_TIMEOUT:
        if row is not None and (not isinstance(row, str) or not ROW_ID.fullmatch(row)):
            raise Dropped("malformed", "row")
    elif row is not None:
        raise Dropped("malformed", "row")
    return ListAnswer(list_id=list_id, ending=ending, row=row)


def _check_against(answer: ListAnswer, request: ListRequest) -> None:
    """Refuse an answer to a list sent from here that names a row the list did not offer.

    A picked row has to be one of the list's rows, and a timeout has to name exactly the
    default row the list was sent with - or none, when it had none. Anything else is not
    this list's answer, whoever published it.
    """
    if answer.ending == LIST_PICKED and request.label_of(answer.row) is None:
        raise Dropped("foreign_row", "a row the list did not have")
    if answer.ending == LIST_TIMEOUT and answer.row != request.default:
        raise Dropped("foreign_row", "a default the list was not sent with")


# ------------------------------------------------------------------------ the lists --


@dataclass(slots=True)
class _SentList:
    """A list this Home Assistant published, until it is answered or its wait runs out."""

    list_id: str
    request: ListRequest
    # The caller's wait, None for a caller that did not ask for the answer - or that stopped
    # waiting. The record stays either way, so that the event can carry the row's label.
    future: asyncio.Future[dict[str, Any]] | None
    cancel_timer: CALLBACK_TYPE | None = None

    @callback
    def end(self) -> None:
        """Stop the timer that would end this list as `no_answer`."""
        if self.cancel_timer is not None:
            self.cancel_timer()
            self.cancel_timer = None


class ChoiceLists:
    """One receiver's lists: published on request, answered by the receiver, waited for here."""

    def __init__(self, hass: HomeAssistant, box: Enigma2Box) -> None:
        """Nothing is published or waited for until a list is shown."""
        self.hass = hass
        self.box = box
        self._sent: dict[str, _SentList] = {}
        self._answered: deque[str] = deque(maxlen=ANSWERS_REMEMBERED)
        self._logged_causes: set[str] = set()
        self._stopped = False

    @property
    def waiting(self) -> int:
        """How many lists sent from here have not been answered yet."""
        return len(self._sent)

    async def async_show(self, request: ListRequest, *, wait: bool) -> dict[str, Any] | None:
        """Publish a list, and with `wait` return how it ended; without, return once sent.

        Refused before anything is published: a receiver that does not claim `list` - or
        does not open the envelope the list's id travels in, which `list` implies - one
        that is not on the broker, and a list over the receiver's 4096 bytes in its
        envelope. The broker has to have confirmed the subscription that carries the
        answer before the list goes out: Home Assistant debounces its SUBSCRIBE packets,
        and a receiver whose viewer is quick would otherwise answer into a subscription
        the broker does not have yet.
        """
        box = self.box
        check_can_show(box)
        list_id = new_command_id()
        # `build_list` has checked the size and the encoding of this very envelope.
        wire = command_envelope(list_id, request.payload())

        await box.async_wait_subscribed()
        if not box.subscribed:
            raise _failed("list_not_subscribed")
        if self._stopped:
            raise _failed("list_unloaded")

        sent = _SentList(
            list_id=list_id,
            request=request,
            future=self.hass.loop.create_future() if wait else None,
        )
        self._sent[list_id] = sent
        # From the publish: the receiver counts the timeout from the moment it shows the list,
        # which is after it heard it. The grace covers that and the answer's way back.
        sent.cancel_timer = async_call_later(
            self.hass,
            request.timeout + LIST_ANSWER_GRACE,
            partial(self._async_no_answer, list_id),
        )
        try:
            try:
                await box.async_publish_enveloped(COMMAND_LIST, wire)
            except Exception:
                # Not sent, so nothing will answer it. A caller cancelled in the middle of
                # the publish is not this: the list may be on the television already.
                self._forget(list_id)
                raise
            if sent.future is None:
                return None
            return await sent.future
        finally:
            # A caller that stopped waiting - its automation was stopped - leaves the record
            # to its timer, so that the answer still fires the event with the row's label.
            sent.future = None

    @callback
    def _forget(self, list_id: str) -> _SentList | None:
        """Take one list off the record, with its timer; None when it was not on it."""
        if (sent := self._sent.pop(list_id, None)) is not None:
            sent.end()
        return sent

    @callback
    def _async_no_answer(self, list_id: str, _now: datetime) -> None:
        """The list's timeout and the grace have passed with no answer."""
        if (sent := self._sent.pop(list_id, None)) is None:
            return
        sent.cancel_timer = None
        _LOGGER.debug(
            "No answer from %s to list %s within %s s",
            self.box.node_id,
            list_id,
            sent.request.timeout + LIST_ANSWER_GRACE,
        )
        if sent.future is not None and not sent.future.done():
            sent.future.set_result(dict(NO_ANSWER))

    @callback
    def async_answer_received(self, msg: ReceiveMessage) -> None:
        """Read one `list_answer`: end the wait of the list it answers, and fire the event."""
        try:
            if msg.retain:
                # The plugin never retains an answer: this one is somebody's leftover, about
                # a list nobody is looking at any more.
                raise Dropped("retained")
            answer = parse_answer(msg.payload)
            if answer.list_id is not None and answer.list_id in self._answered:
                raise Dropped("duplicate", "an id already answered")
            sent = self._sent.get(answer.list_id) if answer.list_id is not None else None
            if sent is not None:
                _check_against(answer, sent.request)
        except Dropped as dropped:
            self._log_drop(dropped)
            return

        label: str | None = None
        if answer.list_id is not None:
            self._answered.append(answer.list_id)
        if sent is not None:
            self._forget(sent.list_id)
            label = sent.request.label_of(answer.row)
            if sent.future is not None and not sent.future.done():
                sent.future.set_result(answer_response(answer.ending, answer.row, label))

        self.hass.bus.async_fire(
            EVENT_LIST_ANSWER,
            {
                ATTR_DEVICE_ID: self.box.device_id,
                ATTR_NODE_ID: self.box.node_id,
                ATTR_LIST_ID: answer.list_id,
                ATTR_ENDING: answer.ending,
                ATTR_ROW: answer.row,
                ATTR_LABEL: label,
            },
        )

    @callback
    def async_refusal_received(self, error: Mapping[str, Any]) -> None:
        """End the wait of the list a fresh `last_error` refuses, by its id and nothing else.

        The receiver refuses a list before it shows anything, and says the list's id back.
        A refusal with another id, or without one, is about another command - also another
        client's list - and leaves every wait here alone.
        """
        if (said := refusal_id(error)) is None or (sent := self._forget(said)) is None:
            return
        if sent.future is None or sent.future.done():
            return
        sentence = str(error.get("error") or error.get("reason") or "")[:ERROR_TEXT_MAX]
        # A code that says the moment or the request was wrong is a refusal the caller can
        # act on; anything else may be a failure, as for every other command.
        kind = (
            Enigma2CommandRefused
            if error.get("reason") in LIST_REFUSAL_REASONS
            else Enigma2CommandError
        )
        sent.future.set_exception(
            kind(
                translation_domain=DOMAIN,
                translation_key="list_refused",
                translation_placeholders={"reason": sentence},
            )
        )

    @callback
    def async_stop(self) -> None:
        """End every wait: the receiver is being unloaded, and nothing will read its answers."""
        self._stopped = True
        for sent in list(self._sent.values()):
            sent.end()
            if sent.future is not None and not sent.future.done():
                sent.future.set_exception(_failed("list_unloaded"))
        self._sent.clear()

    def _log_drop(self, dropped: Dropped) -> None:
        """A dropped answer is logged as a warning the first time for its cause, then quietly."""
        level = logging.DEBUG if dropped.cause in self._logged_causes else logging.WARNING
        self._logged_causes.add(dropped.cause)
        _LOGGER.log(
            level,
            "Dropped a list answer from %s (%s%s); nothing was fired",
            self.box.node_id,
            dropped.cause,
            f": {dropped.detail}" if dropped.detail else "",
        )

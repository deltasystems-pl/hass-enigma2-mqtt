"""A command's id: when a command goes out in an envelope, and whose a refusal is.

A plugin after 0.4.0 that claims the capability `command_id` opens an envelope around a
command's payload - `{"id": ..., "payload": ...}` - and says the id back on `last_error`
when it refuses that command (the plugin's TOPICS.md, section 2). Four things are held
here:

- **who is sent one** - a plugin whose `info` claims the capability now, and nobody else:
  an older plugin takes the envelope for the command's payload and refuses it;
- **what is in it** - the payload the command would have had without it, as a string,
  character for character, under a fresh random id;
- **whose refusal it is** - the command's whose id it says; never a command's with another
  id; and one without an id is read by the command's name, as before there were ids;
- **what stays bare** - `cmd/relay`, a command that only fits without the envelope, and
  the commands sent where no runtime object knows what the plugin takes.

Every message here is fired after the entry is set up, which is the order a broker
produces: the retained burst lands on entities that already exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from pathlib import Path
import re
from typing import Any
from unittest.mock import patch

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt import box as box_module
from custom_components.enigma2_mqtt.box import (
    Enigma2Box,
    Enigma2CommandError,
    async_request_ha_mode,
    command_envelope,
    new_command_id,
    refusal_id,
    refusal_of_another,
)
from custom_components.enigma2_mqtt.const import (
    CAPABILITY_COMMAND_ID,
    COMMAND_MAX_BYTES,
    COMMANDS_WITHOUT_ENVELOPE,
    DOMAIN,
)
from custom_components.enigma2_mqtt.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    BASE_TOPIC,
    BOUQUET,
    CAPABILITIES,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    async_setup_box_then_retained,
    command_topic,
    opened_command,
)

PLAYER = "media_player.dekoder_salon"
COMMANDS = f"{BASE_TOPIC}/{NODE_ID}/cmd/"
# What this integration makes an id of: 16 hexadecimal digits, inside the plugin's rule of
# 1 to 64 characters out of letters, digits, `.`, `_`, `:` and `-`.
AN_ID = re.compile(r"[0-9a-f]{16}")
SOMEBODY_ELSES = "7f9c2e4a-3b1d-4c8e-9f60-2d5a1b7c8e90"

# The example receiver, running a plugin that opens the envelope.
CLAIMS = {**INFO, "capabilities": [*CAPABILITIES, CAPABILITY_COMMAND_ID]}
# The same two plugins on an image where the channel-list bouquet can be set, which the
# bouquet action asks for before it sends anything.
OLDER_WITH_BOUQUETS = {**INFO, "capabilities": [*CAPABILITIES, "bouquet_context"]}
CLAIMS_WITH_BOUQUETS = {
    **INFO,
    "capabilities": [*CAPABILITIES, "bouquet_context", CAPABILITY_COMMAND_ID],
}


class Receiver:
    """A receiver that hears every command, and answers each the way it is told to."""

    def __init__(
        self,
        hass: HomeAssistant,
        answer: Callable[[str, str | None, str], None] | None = None,
    ) -> None:
        self.hass = hass
        self.answer = answer
        # Every command heard: its name, what was published, its id and its own payload.
        self.heard: list[tuple[str, str, str | None, str]] = []

    async def arm(self) -> Receiver:
        await mqtt.async_subscribe(self.hass, f"{COMMANDS}#", self._received)
        return self

    @callback
    def _received(self, msg: ReceiveMessage) -> None:
        name = msg.topic[len(COMMANDS) :]
        command_id, inner = opened_command(msg.payload)
        self.heard.append((name, str(msg.payload), command_id, inner))
        if self.answer is not None:
            self.answer(name, command_id, inner)

    def ids(self, name: str) -> list[str | None]:
        return [command_id for heard, _, command_id, _ in self.heard if heard == name]

    def refuse(
        self, name: str, command_id: Any = None, error: str = "refused", **more: Any
    ) -> None:
        """`last_error` about one command, with the id it carried when it carried one."""
        payload: dict[str, Any] = {"cmd": name, "error": error, "ts": 1789459213, **more}
        if command_id is not None:
            payload["id"] = command_id
        async_fire_mqtt_message(self.hass, LAST_ERROR_TOPIC, json.dumps(payload))


def _says(hass: HomeAssistant, info: dict[str, Any] | str) -> None:
    """The plugin says `info` now - live, as a running plugin does."""
    async_fire_mqtt_message(
        hass, INFO_TOPIC, info if isinstance(info, str) else json.dumps(info)
    )


async def _box(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    retained: dict[str, str | bytes],
    info: dict[str, Any] | None = CLAIMS,
) -> Enigma2Box:
    """The example box, set up before its retained topics arrive."""
    if info is None:
        retained.pop(INFO_TOPIC, None)
    else:
        retained[INFO_TOPIC] = json.dumps(info)
    await async_setup_box_then_retained(hass, entry, retained)
    box: Enigma2Box = entry.runtime_data
    # Past Home Assistant's debounced SUBSCRIBE: from here a command is published the
    # moment it is asked for, and not half a second later.
    await box.async_wait_subscribed()
    return box


# ------------------------------------------------------------------ the id itself --


def test_an_id_is_random_hexadecimal_and_inside_the_plugin_s_rule() -> None:
    """Sixteen hexadecimal digits and no prefix: the plugin drops an id that holds one of
    its secrets, and a fixed word in front could hold a short password every time."""
    ids = {new_command_id() for _ in range(200)}

    assert len(ids) == 200
    assert all(AN_ID.fullmatch(command_id) for command_id in ids)
    assert all(len(command_id) <= 64 for command_id in ids)


@pytest.mark.parametrize(
    "payload",
    [
        "PRESS",
        "",
        "40",
        SREF,
        NODE_ID,
        '{"sref": "1:0:19:283D:3FB:1:C00000:0:0:0:"}',
        # An object's text is carried as it was written: the spacing, the order, a number
        # in a spelling a re-encoder would change, an escape it would undo.
        '{"timeout": 1e2,  "level": 42.0, "text": "\\u017c\\u00f3\\u0142\\u0107"}',
        'Uwaga: "żółć" \\ 100 %',
        # A payload that looks like an envelope is the command's payload, once inside one.
        '{"id": "zap-17", "payload": "PRESS"}',
    ],
)
def test_the_envelope_carries_the_payload_as_the_text_it_was(payload: str) -> None:
    command_id = new_command_id()

    wire = command_envelope(command_id, payload)

    envelope = json.loads(wire)
    assert list(envelope) == ["id", "payload"]
    assert envelope["id"] == command_id
    # A string, also for an object's text: the plugin hands a string on unchanged.
    assert envelope["payload"] == payload
    assert opened_command(wire) == (command_id, payload)
    # Written without spaces, and without escaping what UTF-8 carries as it is.
    assert wire == f'{{"id":"{command_id}","payload":{json.dumps(payload, ensure_ascii=False)}}}'


@pytest.mark.parametrize(
    ("error", "said"),
    [
        ({"cmd": "zap", "error": "x", "id": "abc"}, "abc"),
        ({"cmd": "zap", "error": "x"}, None),
        # The plugin leaves the member out; anything but a non-empty string is not its id.
        ({"cmd": "zap", "error": "x", "id": None}, None),
        ({"cmd": "zap", "error": "x", "id": ""}, None),
        ({"cmd": "zap", "error": "x", "id": 7}, None),
        ({"cmd": "zap", "error": "x", "id": ["abc"]}, None),
        (None, None),
        ("zap", None),
    ],
)
def test_only_a_non_empty_string_is_the_id_a_refusal_says(error: Any, said: str | None) -> None:
    assert refusal_id(error) == said


@pytest.mark.parametrize(
    ("said", "sent", "another"),
    [
        ("abc", "abc", False),
        ("abc", "abd", True),
        # A refusal with an id is never about a command that carried none.
        ("abc", None, True),
        # A refusal without an id decides nothing: the plugin may have dropped ours.
        (None, "abc", False),
        (None, None, False),
        ("", "abc", False),
    ],
)
def test_a_refusal_is_somebody_else_s_only_when_it_says_another_id(
    said: str | None, sent: str | None, another: bool
) -> None:
    error = {"cmd": "update", "error": "x"}
    if said is not None:
        error["id"] = said

    assert refusal_of_another(error, sent) is another


# ------------------------------------------------------------- who is sent one --


async def test_a_plugin_that_claims_the_capability_is_sent_an_envelope_with_a_fresh_id(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()

    for _ in range(3):
        await box.async_publish_cmd("key", "KEY_OK")

    ids = receiver.ids("key")
    assert len(set(ids)) == 3
    assert all(command_id is not None and AN_ID.fullmatch(command_id) for command_id in ids)
    for _, wire, command_id, inner in receiver.heard:
        assert json.loads(wire) == {"id": command_id, "payload": "KEY_OK"}
        assert inner == "KEY_OK"
    # A command still: QoS 1, and never retained.
    for call in mqtt_mock.async_publish.call_args_list:
        if call.args[0] == command_topic("key"):
            assert call.args[2:4] == (1, False)


def _capabilities_not_a_list(retained: dict[str, str | bytes]) -> None:
    retained[INFO_TOPIC] = json.dumps({**INFO, "capabilities": CAPABILITY_COMMAND_ID})


def _only_the_announcement_claims(retained: dict[str, str | bytes]) -> None:
    # `info` has not arrived; the announcement is not what says what the plugin takes.
    del retained[INFO_TOPIC]
    retained[ANNOUNCEMENT_TOPIC] = json.dumps(
        {**ANNOUNCEMENT, "capabilities": [*CAPABILITIES, CAPABILITY_COMMAND_ID]}
    )


def _an_older_plugin(retained: dict[str, str | bytes]) -> None:
    retained[INFO_TOPIC] = json.dumps(INFO)


def _an_empty_info(retained: dict[str, str | bytes]) -> None:
    retained[INFO_TOPIC] = "{}"


@pytest.mark.parametrize(
    "arrange",
    [_an_older_plugin, _only_the_announcement_claims, _capabilities_not_a_list, _an_empty_info],
)
async def test_a_plugin_that_has_not_claimed_it_is_sent_the_bare_payload(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    arrange: Callable[[dict[str, str | bytes]], None],
) -> None:
    """An older plugin takes an envelope for the payload and refuses it: not known is no."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(CLAIMS)
    arrange(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    box: Enigma2Box = config_entry.runtime_data
    receiver = await Receiver(hass).arm()

    await box.async_publish_cmd("zap", json.dumps({"name": "TVN HD"}))

    assert not box.takes_command_ids
    assert receiver.heard == [("zap", '{"name": "TVN HD"}', None, '{"name": "TVN HD"}')]


async def test_what_the_plugin_takes_is_asked_for_each_command(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An update brings the capability, a downgrade takes it away, and a retracted `info`
    claims nothing - the last payload is kept for the device page, not for this."""
    box = await _box(hass, config_entry, box_on_the_broker, INFO)
    receiver = await Receiver(hass).arm()

    async def _enveloped() -> bool:
        await box.async_publish_cmd("volume", "40")
        return receiver.heard[-1][2] is not None

    assert not await _enveloped()
    _says(hass, CLAIMS)
    assert await _enveloped()
    _says(hass, "")
    assert box.info == CLAIMS
    assert not await _enveloped()
    _says(hass, CLAIMS)
    assert await _enveloped()
    _says(hass, INFO)
    assert not await _enveloped()
    assert [inner for _, _, _, inner in receiver.heard] == ["40"] * 5


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        json.dumps([CAPABILITY_COMMAND_ID]),
        json.dumps(CAPABILITY_COMMAND_ID),
        "7",
        "true",
        "null",
        "not json at all",
        json.dumps(CLAIMS)[:-1],
        b"\xff\xfe",
        "{}",
        "",
    ],
    ids=[
        "an empty list",
        "a list",
        "a string",
        "a number",
        "true",
        "null",
        "not JSON",
        "JSON cut short",
        "not UTF-8",
        "an empty object",
        "retracted",
    ],
)
async def test_an_info_that_says_nothing_readable_claims_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    payload: str | bytes,
) -> None:
    """After a plugin that claimed the capability, an `info` that is no JSON object, an
    empty object or a retraction: whatever runs now has not said it opens an envelope."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    await box.async_publish_cmd("volume", "40")
    assert receiver.heard[-1][2] is not None

    async_fire_mqtt_message(hass, INFO_TOPIC, payload)
    await box.async_publish_cmd("volume", "40")

    assert not box.takes_command_ids
    assert receiver.heard[-1] == ("volume", "40", None, "40")
    # Only an `info` that could be read replaces the last one, for the device page.
    assert box.info == ({} if payload == "{}" else CLAIMS)

    # The next `info` that can be read is the plugin's word again.
    _says(hass, CLAIMS)
    await box.async_publish_cmd("volume", "40")
    assert receiver.heard[-1][2] is not None


async def test_an_info_that_became_unreadable_is_logged_once_each_time(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One debug line when `info` stops being readable, not one for every such payload,
    with the payload's size and nothing of what was in it."""
    box = await _box(hass, config_entry, box_on_the_broker)

    def _lines() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == box_module.__name__
            and record.levelno == logging.DEBUG
            and "not a JSON object" in record.getMessage()
        ]

    with caplog.at_level(logging.DEBUG, logger=box_module.__name__):
        _says(hass, '["something-private"]')
        _says(hass, "7")
        _says(hass, "not json at all")

        assert len(_lines()) == 1
        assert NODE_ID in _lines()[0]
        assert "(21 bytes)" in _lines()[0]
        assert "private" not in _lines()[0]

        # Readable again, and unreadable again: that is a second time.
        _says(hass, CLAIMS)
        assert box.takes_command_ids
        _says(hass, "null")
        _says(hass, "null")

        assert len(_lines()) == 2
        assert not box.takes_command_ids


# ------------------------------------- nothing differs without the capability --


async def _call(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(DOMAIN, service, {"entity_id": PLAYER, **data}, blocking=True)


def _entity(domain: str, service: str, entity_id: str, **data: Any) -> Callable[..., Any]:
    async def _send(hass: HomeAssistant) -> None:
        await hass.services.async_call(
            domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
        )

    return _send


def _action(service: str, **data: Any) -> Callable[..., Any]:
    async def _send(hass: HomeAssistant) -> None:
        await _call(hass, service, **data)

    return _send


def _direct(name: str, payload: str) -> Callable[..., Any]:
    """A command no entity here sends without more of a receiver than this file builds:
    the same path, `Enigma2Box.async_command`, with the payload its sender uses."""

    async def _send(hass: HomeAssistant) -> None:
        box: Enigma2Box = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
        await box.async_command(name, payload)

    return _send


# One of every kind of sender that goes through the runtime object, and the command it is
# expected to publish, and whether the sender waits to hear a refusal - the actions and
# the buttons do, the controls that move the moment they are pressed do not. The update,
# the uninstall and the mode switch have their own tests below;
# `test_no_command_is_published_past_the_runtime_object` holds the list closed.
SENDERS: list[tuple[str, str, Callable[..., Awaitable[None]], bool]] = [
    ("action zap by reference", "zap", _action("zap", sref=SREF_TWO), True),
    ("action zap by name", "zap", _action("zap", name="TVN HD"), True),
    ("action select_bouquet", "bouquet", _action("select_bouquet", sref=BOUQUET["sref"]), True),
    ("action send_key", "key", _action("send_key", key="green", long=True), True),
    (
        "action message",
        "message",
        _action("message", text='Uwaga: "żółć" 100 %', type="warning"),
        True,
    ),
    ("action add_timer", "timer", _action("add_timer", sref=SREF, event_id=27431), True),
    ("action record", "record", _action("record", action="start"), True),
    ("action screenshot", "screenshot", _action("screenshot"), True),
    (
        "switch power off",
        "power",
        _entity("switch", "turn_off", "switch.dekoder_salon_power"),
        False,
    ),
    ("switch mute on", "mute", _entity("switch", "turn_on", "switch.dekoder_salon_mute"), False),
    (
        "number volume",
        "volume",
        _entity("number", "set_value", "number.dekoder_salon_volume", value=20),
        False,
    ),
    ("player off", "power", _entity("media_player", "turn_off", PLAYER), False),
    (
        "player volume",
        "volume",
        _entity("media_player", "volume_set", PLAYER, volume_level=0.4),
        False,
    ),
    (
        "player mute",
        "mute",
        _entity("media_player", "volume_mute", PLAYER, is_volume_muted=True),
        False,
    ),
    ("player next", "key", _entity("media_player", "media_next_track", PLAYER), False),
    ("player previous", "key", _entity("media_player", "media_previous_track", PLAYER), False),
    (
        "player play_media",
        "zap",
        _entity(
            "media_player",
            "play_media",
            PLAYER,
            media_content_type="channel",
            media_content_id=SREF_TWO,
        ),
        False,
    ),
    ("remote off", "power", _entity("remote", "turn_off", "remote.dekoder_salon_remote"), False),
    (
        "remote send_command",
        "key",
        _entity("remote", "send_command", "remote.dekoder_salon_remote", command=["KEY_OK"]),
        False,
    ),
    (
        "notify",
        "message",
        _entity("notify", "send_message", "notify.dekoder_salon_osd", message="Kolacja"),
        False,
    ),
    (
        "button screenshot",
        "screenshot",
        _entity("button", "press", "button.dekoder_salon_screenshot"),
        True,
    ),
    (
        "button refresh_discovery",
        "discovery",
        _entity("button", "press", "button.dekoder_salon_refresh_discovery"),
        True,
    ),
    (
        "button restart_gui",
        "restart_gui",
        _entity("button", "press", "button.dekoder_salon_restart_gui"),
        True,
    ),
    (
        "button refresh_epg",
        "epg_grid",
        _entity("button", "press", "button.dekoder_salon_refresh_epg"),
        True,
    ),
    ("press-style command", "epg_import", _direct("epg_import", "PRESS"), True),
    (
        "settings",
        "config",
        _direct("config", json.dumps({"publish_keys": False, "screenshot_interval": 90})),
        True,
    ),
    ("zap history", "zap_history", _direct("zap_history", json.dumps({"sref": SREF})), True),
]


@pytest.mark.parametrize(
    ("name", "send", "waits"),
    [(name, send, waits) for _, name, send, waits in SENDERS],
    ids=[label for label, _, _, _ in SENDERS],
)
async def test_a_command_is_the_same_text_with_and_without_the_envelope(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    name: str,
    send: Callable[[HomeAssistant], Awaitable[None]],
    waits: bool,
) -> None:
    """The same press three times: to an older plugin, to one that claims `command_id`,
    and to an older one again.

    Without the capability the command is what it always was - no member added, nothing
    re-spelled. With it, the envelope holds exactly that text as a string. The fake
    receiver refuses every command the way the plugin does, with the id when there was
    one, and a sender that waits for its answer raises that refusal all three times -
    by the command's name from an older plugin, by its id from one with ids.
    """
    await _box(hass, config_entry, box_on_the_broker, OLDER_WITH_BOUQUETS)
    receiver = Receiver(hass)
    receiver.answer = lambda heard, command_id, _inner: receiver.refuse(heard, command_id)
    await receiver.arm()

    refused: list[str | None] = []

    async def _sent() -> list[tuple[str, str, str | None, str]]:
        receiver.heard.clear()
        try:
            await send(hass)
        except HomeAssistantError as error:
            refused.append(error.translation_key)
        else:
            refused.append(None)
        await hass.async_block_till_done()
        return list(receiver.heard)

    bare = await _sent()
    _says(hass, CLAIMS_WITH_BOUQUETS)
    enveloped = await _sent()
    _says(hass, OLDER_WITH_BOUQUETS)
    again = await _sent()

    assert [heard for heard, _, _, _ in bare] == [name]
    assert refused == ["command_refused" if waits else None] * 3
    # To a plugin without the capability: the payload itself, and no id.
    assert all(wire == inner and command_id is None for _, wire, command_id, inner in bare)
    assert again == bare
    # To one with it: an envelope, and inside it the very same text.
    assert len(enveloped) == len(bare)
    for (_, wire, command_id, inner), (_, _, _, was) in zip(enveloped, bare, strict=True):
        assert command_id is not None and AN_ID.fullmatch(command_id)
        assert json.loads(wire) == {"id": command_id, "payload": was}
        assert inner == was


# The ways a command topic is written in source: the helper, a string with the topic's
# `/cmd/` level in it, and a string that starts with `cmd/`.
_BUILDS_A_COMMAND_TOPIC = re.compile(r"""\bcommand_topic\(|/cmd/|["']cmd/""")


def test_no_command_is_published_past_the_runtime_object() -> None:
    """Every command leaves through `Enigma2Box`, or is on this list with its reason.

    A new sender that built its own topic would publish bare to a plugin that takes ids
    and could not tell its refusal from another's. A topic can be built three ways and all
    three are looked for: `command_topic(...)`, a string with `/cmd/` in it, and a string
    that starts with `cmd/` to be joined to a base. The four modules below build a command
    topic themselves:

    - `uninstall.py` - enveloped, through `Enigma2Box.async_wire_payload`;
    - `__init__.py` - `cmd/ha_mode` for an entry that is being removed, whose runtime
      object is already stopped;
    - `installer.py` - `cmd/reset` after a downgrade, to a plugin that has only just
      started and by definition an older one, and whose payload is ignored;
    - `restart_rule.py` - `cmd/bouquet` from the installer, which has no runtime object.

    The last three are sent bare, which every plugin takes, and none reads `last_error`.
    """
    sources = Path(box_module.__file__).parent
    builders = {
        path.name
        for path in sources.glob("*.py")
        if _BUILDS_A_COMMAND_TOPIC.search(path.read_text(encoding="utf-8"))
    }

    assert builders == {"box.py", "uninstall.py", "__init__.py", "installer.py", "restart_rule.py"}


@pytest.mark.parametrize(
    "line",
    [
        'topic = command_topic(base_topic, node_id, "reset")',
        'topic = f"{base_topic}/{node_id}/cmd/{name}"',
        'topic = f"{base_topic}/{node_id}/cmd/reset"',
        'topic = base_topic + "/" + node_id + "/cmd/reset"',
        "topic = prefix + 'cmd/reset'",
        'topic = "/".join((base_topic, node_id, "cmd/reset"))',
    ],
)
def test_a_command_topic_is_recognised_however_it_is_built(line: str) -> None:
    """A literal topic is a sender too, and so is one joined from pieces."""
    assert _BUILDS_A_COMMAND_TOPIC.search(line)


@pytest.mark.parametrize(
    "line",
    [
        "# `cmd/reset` retracts them all and republishes its own.",
        '"Receiver %s retracted its topics and went offline after cmd/uninstall",',
        '"""Answer a receiver that asks for a package: `cmd/relay`, or nothing."""',
    ],
)
def test_a_command_named_in_a_comment_or_a_log_line_is_no_sender(line: str) -> None:
    assert not _BUILDS_A_COMMAND_TOPIC.search(line)


# --------------------------------------------------------------- what stays bare --


async def test_the_relay_answer_is_never_put_in_an_envelope(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`cmd/relay` is an answer, not a command: the plugin does not open an envelope there,
    and the `id` in it is the relay request's."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    answer = json.dumps({"id": "a1b2c3d4e5f6", "version": "0.4.1", "url": "http://x", "expires": 1})

    await box.async_publish_cmd("relay", answer)

    assert COMMANDS_WITHOUT_ENVELOPE == {"relay"}
    assert receiver.heard == [("relay", answer, None, answer)]


async def test_a_command_that_fits_only_without_the_envelope_goes_out_bare(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The plugin discards a command over its limit unanswered, and the envelope counts: a
    command the envelope would push over the limit keeps its place and loses its id."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    # The plugin's own number, `MAX_PAYLOAD_BYTES` in its `commands.py`, written out: a
    # limit that drifted here would be measured against itself by everything below.
    assert COMMAND_MAX_BYTES == 4096
    overhead = len(command_envelope(new_command_id(), ""))
    # `{"id":"` and `","payload":"` and `"}` around sixteen digits.
    assert overhead == 38
    fits = "x" * (COMMAND_MAX_BYTES - overhead)
    # Two bytes a character in UTF-8: the limit is in bytes, as the plugin counts it.
    fits_in_bytes = "ż" * ((COMMAND_MAX_BYTES - overhead) // 2)
    only_bare = "x" * (COMMAND_MAX_BYTES - overhead + 1)
    only_bare_in_bytes = "ż" * ((COMMAND_MAX_BYTES - overhead) // 2 + 1)
    # Every quote costs a backslash inside the envelope's string.
    quotes = '"' * (COMMAND_MAX_BYTES // 2)
    too_long_anyway = "x" * (COMMAND_MAX_BYTES + 1)

    for payload in (fits, fits_in_bytes, only_bare, only_bare_in_bytes, quotes, too_long_anyway):
        await box.async_publish_cmd("message", payload)

    sent = [(command_id is not None, inner) for _, _, command_id, inner in receiver.heard]
    assert sent == [
        (True, fits),
        (True, fits_in_bytes),
        (False, only_bare),
        (False, only_bare_in_bytes),
        (False, quotes),
        (False, too_long_anyway),
    ]
    assert len(receiver.heard[0][1].encode()) == COMMAND_MAX_BYTES
    assert len(receiver.heard[1][1].encode()) <= COMMAND_MAX_BYTES


async def test_a_bare_command_to_a_plugin_with_ids_is_refused_by_its_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A command that went out without its id is waited for the way it always was."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = Receiver(hass)
    receiver.answer = lambda heard, command_id, _inner: receiver.refuse(heard, command_id)
    await receiver.arm()

    with pytest.raises(Enigma2CommandError) as raised:
        await box.async_command("message", "x" * COMMAND_MAX_BYTES)

    assert raised.value.translation_key == "command_refused"
    assert receiver.ids("message") == [None]


async def test_a_payload_utf_8_cannot_carry_fails_in_the_publish_as_before(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Half a surrogate pair: escaped inside an envelope it would reach the plugin as an
    envelope it does not open. It is handed to the publish as it stands instead."""
    box = await _box(hass, config_entry, box_on_the_broker)

    assert box.async_wire_payload("message", "half \ud83d") == ("half \ud83d", None)


# ------------------------------------------------------------- whose refusal it is --


async def _zap(box: Enigma2Box, **kwargs: Any) -> None:
    """A zap that waits for `service` to name the channel, as the action does."""
    await box.async_command(
        "zap",
        SREF_TWO,
        effect=lambda: (box.state.service or {}).get("sref") == SREF_TWO,
        **kwargs,
    )


def _tuned(hass: HomeAssistant) -> None:
    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps({**SERVICE, "sref": SREF_TWO}))


def _start(command: Any) -> asyncio.Task[None]:
    """A command, running - not tracked by Home Assistant, so settling never waits for it."""
    return asyncio.get_running_loop().create_task(command)


async def _settle() -> None:
    """Run what is ready, without `async_block_till_done` waiting for a command to end."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _waiting(receiver: Receiver, name: str, count: int = 1) -> None:
    for _ in range(200):
        if len(receiver.ids(name)) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"cmd/{name} was never published")


async def test_a_refusal_that_says_the_command_s_id_fails_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    task = _start(_zap(box))
    await _waiting(receiver, "zap")

    receiver.refuse("zap", receiver.ids("zap")[0], "no such channel")

    with pytest.raises(Enigma2CommandError) as raised:
        await task
    assert raised.value.translation_placeholders == {
        "command": "zap",
        "reason": "no such channel",
    }


@pytest.mark.parametrize("with_id", [True, False], ids=["its id", "no id"])
@pytest.mark.parametrize("info", [CLAIMS, INFO], ids=["with ids", "an older plugin"])
async def test_a_retained_refusal_never_fails_a_waiting_command(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    info: dict[str, Any],
    with_id: bool,
) -> None:
    """A retained `last_error` is the broker's replay of what it held - Home Assistant
    reconnected and subscribed again while the command waited - and not the receiver
    speaking now. It fails nothing: not by the command's name, and not by its id
    either, which a replay of this very command's refusal would carry."""
    box = await _box(hass, config_entry, box_on_the_broker, info)
    receiver = await Receiver(hass).arm()
    task = _start(_zap(box))
    await _waiting(receiver, "zap")
    command_id = receiver.ids("zap")[0]
    assert (command_id is not None) is (info is CLAIMS)
    payload: dict[str, Any] = {"cmd": "zap", "error": "no such channel", "ts": 1789459213}
    if with_id and command_id is not None:
        payload["id"] = command_id

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(payload), retain=True)
    await _settle()
    assert not task.done()
    assert box.state.last_error == payload
    _tuned(hass)

    await task


async def test_a_refusal_that_says_another_id_does_not_fail_a_command_of_that_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Another broker client's zap is refused while ours is on its way: not our refusal."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    task = _start(_zap(box))
    await _waiting(receiver, "zap")

    receiver.refuse("zap", SOMEBODY_ELSES, "no such channel")
    await _settle()
    assert not task.done()
    _tuned(hass)

    await task


async def test_two_commands_of_one_name_are_refused_one_by_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Two zaps close together have one name; the id says which of them was refused."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    first = _start(_zap(box))
    await _waiting(receiver, "zap", 1)
    second = _start(_zap(box))
    await _waiting(receiver, "zap", 2)

    receiver.refuse("zap", receiver.ids("zap")[0], "replaced by a newer zap")
    await _settle()

    assert first.done() and not second.done()
    with pytest.raises(Enigma2CommandError) as raised:
        await first
    assert raised.value.translation_key == "command_refused"
    _tuned(hass)
    await second


async def test_a_refusal_with_the_command_s_id_is_its_own_under_another_name_too(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The id says which command; the name is not asked a second time."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    task = _start(
        box.async_command("zap_history", json.dumps({"sref": SREF_TWO}), effect=lambda: False)
    )
    await _waiting(receiver, "zap_history")

    receiver.refuse("zap", receiver.ids("zap_history")[0], "the box did not tune")

    with pytest.raises(Enigma2CommandError) as raised:
        await task
    # The refusal, in the words it came in - not the ten seconds' "did not carry out".
    assert raised.value.translation_key == "command_refused"
    assert raised.value.translation_placeholders == {
        "command": "zap_history",
        "reason": "the box did not tune",
    }


@pytest.mark.parametrize("said", [None, "", 7], ids=["absent", "empty", "a number"])
async def test_a_refusal_without_an_id_fails_the_waiting_command_of_that_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    said: Any,
) -> None:
    """The plugin drops an id it cannot use - one that holds a secret of its own - and the
    command still runs, so its refusal comes back without one and is read as before."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    task = _start(_zap(box))
    await _waiting(receiver, "zap")
    payload: dict[str, Any] = {"cmd": "zap", "error": "no such channel", "ts": 1}
    if said is not None:
        payload["id"] = said

    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(payload))

    with pytest.raises(Enigma2CommandError) as raised:
        await task
    assert raised.value.translation_key == "command_refused"


async def test_a_refusal_with_an_id_is_not_about_a_command_sent_without_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Only an envelope gives a command an id, so a refusal that says one is about a
    command somebody else sent in one."""
    box = await _box(hass, config_entry, box_on_the_broker)
    receiver = await Receiver(hass).arm()
    task = _start(box.async_command("message", "x" * COMMAND_MAX_BYTES))
    await _waiting(receiver, "message")
    assert receiver.ids("message") == [None]

    receiver.refuse("message", SOMEBODY_ELSES, "text is required")

    # Silence through the grace period is this command's success.
    await task


@pytest.mark.parametrize("info", [CLAIMS, INFO], ids=["with ids", "an older plugin"])
async def test_a_refusal_said_before_the_command_went_out_is_not_its_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    info: dict[str, Any],
) -> None:
    """A command waits for the broker to confirm the subscription before it is published.
    A complaint about its name that arrives in that wait was made before the receiver
    could have heard it, whatever plugin runs there."""
    box = await _box(hass, config_entry, box_on_the_broker, info)
    receiver = await Receiver(hass).arm()
    confirmed = asyncio.Event()

    with patch.object(box, "async_wait_subscribed", confirmed.wait):
        task = _start(_zap(box))
        await _settle()
        assert receiver.heard == []

        receiver.refuse("zap", None, "somebody else's zap")
        await _settle()
        assert not task.done()
        confirmed.set()
        await _waiting(receiver, "zap")
        _tuned(hass)

        await task


# ----------------------------------------------------------------- the mode switch --


async def _answer_ha_mode(hass: HomeAssistant, heard: list[Any]) -> None:
    """A plugin that answers `cmd/ha_mode` with `info`, envelope or not."""

    @callback
    def _received(msg: ReceiveMessage) -> None:
        heard.append(msg.payload)
        _, mode = opened_command(msg.payload)
        async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**CLAIMS, "ha_mode": mode}))

    await mqtt.async_subscribe(hass, command_topic("ha_mode"), _received)


async def test_the_mode_switch_of_a_running_box_goes_in_an_envelope(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box = await _box(hass, config_entry, box_on_the_broker)
    heard: list[Any] = []
    await _answer_ha_mode(hass, heard)

    assert await box.async_request_ha_mode("off")

    (wire,) = heard
    command_id, mode = opened_command(wire)
    assert mode == "off"
    assert command_id is not None and AN_ID.fullmatch(command_id)
    assert box.info["ha_mode"] == "off"


async def test_the_mode_switch_of_an_older_plugin_and_of_the_config_flow_is_bare(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The config flow has no runtime object to say what the plugin takes, and a bare
    payload is what every plugin takes."""
    box = await _box(hass, config_entry, box_on_the_broker, INFO)
    heard: list[Any] = []
    await _answer_ha_mode(hass, heard)

    assert await box.async_request_ha_mode("off")
    _says(hass, CLAIMS)
    assert await async_request_ha_mode(hass, BASE_TOPIC, NODE_ID, "integration") is not None

    assert heard == ["off", "integration"]


# --------------------------------------------------------------- where the id shows --


async def test_the_id_is_plumbing_and_is_shown_on_no_entity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The sensor keeps the command, the sentence, the time and whose it is - an id in
    its attributes would be one more row in the recorder for every refusal. The
    diagnostics download carries the payload as it arrived, id included."""
    await _box(hass, config_entry, box_on_the_broker)
    receiver = Receiver(hass)

    receiver.refuse("zap", SOMEBODY_ELSES, "no such channel")
    await hass.async_block_till_done()

    for state in hass.states.async_all():
        assert SOMEBODY_ELSES not in state.state
        assert SOMEBODY_ELSES not in json.dumps(state.attributes, default=str)
    sensor = hass.states.get("sensor.dekoder_salon_last_error")
    assert sensor.state == "zap"
    assert sensor.attributes["error"] == "no such channel"
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["topics"]["last_error"]["id"] == SOMEBODY_ELSES

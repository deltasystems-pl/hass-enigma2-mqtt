"""Removing the plugin from the receiver: offered, confirmed, acted on by the box, verified.

The receiver in these tests is the documentation's example box. Its MQTT side is the fake
broker every other test uses, answering `cmd/uninstall` the way the plugin's teardown does
— `info` and the announcement retracted, then `offline` — or refusing it on `last_error`.
Its SSH side is a fake that knows only the handful of fixed reads the verification makes,
and records every one of them, so an ordering can be asserted and a write would show up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import json
from typing import Any
from unittest.mock import call, patch

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_CONFIRM_UNINSTALL,
    CONF_KEEP_SSH_CREDENTIALS,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    DOMAIN,
)
from custom_components.enigma2_mqtt.installer import (
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    SshCredentials,
)

from .conftest import (
    ANNOUNCEMENT_TOPIC,
    AVAILABILITY_TOPIC,
    BASE_TOPIC,
    BOX_NAME,
    CAPABILITIES,
    HA_MODE_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    async_setup_box,
    async_setup_box_then_retained,
    command_topic,
)

UNINSTALL_TOPIC = command_topic("uninstall")
HOST_KEY = "ssh-ed25519 AAAATEST"
PERMITTED_INFO: dict[str, Any] = {
    **INFO,
    "ha_mode": "integration",
    "capabilities": [*CAPABILITIES, "uninstall"],
    "settings": {"deep_standby_allowed": False, "uninstall_allowed": True},
}
REFUSAL = "an uninstall is already running"
SETTINGS_HASH = "5" * 64


def _entry(*, credentials: bool = False) -> MockConfigEntry:
    data: dict[str, Any] = {
        CONF_NODE_ID: NODE_ID,
        CONF_BASE_TOPIC: BASE_TOPIC,
        CONF_NAME: BOX_NAME,
    }
    if credentials:
        data.update(
            {
                CONF_SSH_HOST: "192.0.2.12",
                CONF_SSH_PORT: 22,
                CONF_SSH_USERNAME: "root",
                CONF_SSH_PASSWORD: "not-a-real-password",
                CONF_SSH_HOST_KEY: HOST_KEY,
                CONF_KEEP_SSH_CREDENTIALS: True,
            }
        )
    return MockConfigEntry(domain=DOMAIN, title=BOX_NAME, unique_id=NODE_ID, data=data)


@pytest.fixture
def permitted(box_on_the_broker: dict[str, str | bytes]) -> dict[str, str | bytes]:
    """A receiver that states the permission and claims the capability."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(PERMITTED_INFO)
    return box_on_the_broker


# ------------------------------------------------------------------ the fake receiver


@dataclass
class FakeReceiver:
    """The receiver's SSH side, for the before and after readbacks only."""

    events: list[str] = field(default_factory=list)
    installed: bool = True
    recording: bool = False
    timers: list[dict[str, Any]] = field(default_factory=list)
    pid: int = 100
    # What the plugin's teardown does to the box. Each can be switched off to make one
    # readback disagree.
    restarts: bool = True
    removes_files: bool = True
    hook_after: str = "404"
    settings_hash: str = SETTINGS_HASH
    settings_hash_after: str = SETTINGS_HASH
    connect_fails: bool = False
    removed: bool = False
    connections: int = 0

    async def connect(self, credentials: SshCredentials) -> FakeSession:
        assert credentials.host_key == HOST_KEY
        self.connections += 1
        if self.connect_fails:
            raise InstallerError(InstallerErrorCode.SSH_UNAVAILABLE)
        return FakeSession(self)

    def uninstall(self) -> None:
        """What the plugin does to the box after its last `offline`."""
        self.removed = True
        if self.removes_files:
            self.installed = False
        if self.restarts:
            self.pid += 1
        self.settings_hash = self.settings_hash_after


class FakeSession:
    def __init__(self, receiver: FakeReceiver) -> None:
        self.receiver = receiver

    async def run(
        self, command: str, *, input: bytes | None = None, timeout: float = 30
    ) -> CommandResult:
        del timeout
        # Every command here is a read: a verification that sends stdin is writing.
        assert input is None
        receiver = self.receiver
        receiver.events.append(f"ssh:{command.split()[0]}")
        gone = receiver.removed and receiver.removes_files
        if "/api/statusinfo" in command:
            return CommandResult(0, json.dumps({"isRecording": receiver.recording}))
        if "/api/timerlist" in command:
            return CommandResult(0, json.dumps({"timers": receiver.timers}))
        if command == "pidof enigma2":
            return CommandResult(0, f"{receiver.pid}\n")
        if command.startswith("opkg status"):
            if not receiver.installed:
                return CommandResult(0, "")
            return CommandResult(
                0, "Package: enigma2-plugin-extensions-mqttbridge\nVersion: 0.3.0\n"
            )
        if "sha256sum" in command:
            return CommandResult(0, f"{receiver.settings_hash}  -\n15\n")
        if command.startswith("ls -d "):
            if gone:
                return CommandResult(1, "")
            return CommandResult(
                0, "/var/lib/opkg/info/enigma2-plugin-extensions-mqttbridge.control\n"
            )
        if command.startswith("test -e "):
            return CommandResult(1 if gone else 0)
        if command.startswith("find "):
            if gone:
                return CommandResult(0, "")
            return CommandResult(
                0, "/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py\n"
            )
        if command.startswith("wget -S "):
            code = receiver.hook_after if receiver.removed else "200"
            return CommandResult(8 if code == "404" else 0, f"  HTTP/1.1 {code} X\n")
        raise AssertionError(f"unexpected command on the receiver: {command}")

    async def close(self) -> None:
        return None


async def _arm_box(
    hass: HomeAssistant,
    answer: str,
    *,
    receiver: FakeReceiver | None = None,
    events: list[str] | None = None,
) -> list[Any]:
    """Make the fake box answer `cmd/uninstall`, and return the payloads it received."""
    received: list[Any] = []

    @callback
    def _command(msg: ReceiveMessage) -> None:
        received.append(msg.payload)
        if events is not None:
            events.append("mqtt:cmd/uninstall")
        if answer == "uninstall":
            async_fire_mqtt_message(hass, INFO_TOPIC, "")
            async_fire_mqtt_message(hass, ANNOUNCEMENT_TOPIC, "")
            async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
            if receiver is not None:
                receiver.uninstall()
        elif answer == "refuse":
            async_fire_mqtt_message(
                hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "uninstall", "error": REFUSAL})
            )
        elif answer == "switch_off":
            # Deep standby, or a power cut: the last will, and `info` left retained.
            async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
        elif answer == "fail_then_restore":
            # Retractions under way, then everything back and why — a failure the plugin
            # reports before it ever says `offline` (a connection that dropped mid-way).
            async_fire_mqtt_message(hass, INFO_TOPIC, "")
            async_fire_mqtt_message(hass, ANNOUNCEMENT_TOPIC, "")
            async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "online")
            async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(PERMITTED_INFO))
            async_fire_mqtt_message(
                hass,
                LAST_ERROR_TOPIC,
                json.dumps({"cmd": "uninstall", "error": "opkg remove exited 255"}),
            )

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _command)
    return received


async def _open_uninstall(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "uninstall"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "uninstall"
    return result


async def _confirm(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    """Tick the box, and follow the progress step to wherever it ends."""
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM_UNINSTALL: True}
    )
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    return result


def _uninstall_publishes(mqtt_mock: Any) -> list[Any]:
    return [
        published
        for published in mqtt_mock.async_publish.call_args_list
        if published.args[0] == UNINSTALL_TOPIC
    ]


# ------------------------------------------------------------------ offered or not


async def test_the_menu_offers_removal_only_to_a_receiver_that_permits_it(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """Available, permission stated true, capability claimed: the menu, and nothing else."""
    entry = _entry()
    await async_setup_box_then_retained(hass, entry, permitted)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "uninstall"]
    # The ordinary options are still one click away, on the same form as before.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"


def _stated_false(retained: dict[str, str | bytes]) -> None:
    retained[INFO_TOPIC] = json.dumps(
        {**PERMITTED_INFO, "settings": {"uninstall_allowed": False}}
    )


def _silent(retained: dict[str, str | bytes]) -> None:
    retained[INFO_TOPIC] = json.dumps({**PERMITTED_INFO, "settings": {}})


def _no_capability(retained: dict[str, str | bytes]) -> None:
    retained[INFO_TOPIC] = json.dumps({**PERMITTED_INFO, "capabilities": CAPABILITIES})


def _a_string_is_not_true(retained: dict[str, str | bytes]) -> None:
    retained[INFO_TOPIC] = json.dumps(
        {**PERMITTED_INFO, "settings": {"uninstall_allowed": "true"}}
    )


def _offline(retained: dict[str, str | bytes]) -> None:
    retained["enigma2/vuuno4kse_005301/availability"] = "offline"


@pytest.mark.parametrize(
    "withhold",
    [_stated_false, _silent, _no_capability, _a_string_is_not_true, _offline],
    ids=["stated_false", "silence", "no_capability", "string_true", "offline"],
)
async def test_the_menu_is_not_offered_without_all_three(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    withhold: Callable[[dict[str, str | bytes]], None],
) -> None:
    """Silence is no: Configure opens straight on the form, as it always has."""
    withhold(permitted)
    entry = _entry()
    await async_setup_box_then_retained(hass, entry, permitted)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        (INFO_TOPIC, json.dumps({**PERMITTED_INFO, "settings": {"uninstall_allowed": False}})),
        (INFO_TOPIC, json.dumps({**PERMITTED_INFO, "capabilities": CAPABILITIES})),
        (INFO_TOPIC, ""),
        (AVAILABILITY_TOPIC, "offline"),
    ],
    ids=["stated_false", "capability_gone", "empty_info", "offline"],
)
async def test_the_menu_entry_disappears_when_the_receiver_withdraws_it(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    topic: str,
    payload: str,
) -> None:
    """A receiver that was offering removal stops, and the entry goes with it."""
    entry = _entry()
    await async_setup_box(hass, entry)
    assert entry.runtime_data.uninstall_offered

    async_fire_mqtt_message(hass, topic, payload)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"


async def test_an_empty_info_withdraws_the_permission_and_keeps_the_rest(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """A retraction is not a payload to ignore, and not a reason to forget the device.

    The plugin retracts `info` on its way out. Ignoring that kept the permission it had
    stated in memory until Home Assistant restarted, so the menu went on offering to
    remove a plugin that was already gone. What the rest of the integration reads from
    the last payload — the image, the version, the address — stays.
    """
    entry = _entry()
    await async_setup_box(hass, entry)
    box = entry.runtime_data

    async_fire_mqtt_message(hass, INFO_TOPIC, "")
    await hass.async_block_till_done()
    assert box.uninstall_offered is False
    assert box.info_retracted is True
    assert box.info["image"] == INFO["image"]

    # A payload that is not JSON is still ignored, as it always was, and does not bring
    # the permission back.
    async_fire_mqtt_message(hass, INFO_TOPIC, "{not json")
    await hass.async_block_till_done()
    assert box.uninstall_offered is False

    # A reinstalled plugin that says it again is offered again.
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(PERMITTED_INFO))
    await hass.async_block_till_done()
    assert box.uninstall_offered is True


async def test_the_permission_is_asked_again_at_the_click(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """The form can stay open while the permission is switched off at the television."""
    entry = _entry()
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall")
    result = await _open_uninstall(hass, entry)

    async_fire_mqtt_message(
        hass, INFO_TOPIC, json.dumps({**PERMITTED_INFO, "settings": {"uninstall_allowed": False}})
    )
    await hass.async_block_till_done()
    result = await _confirm(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_not_offered"
    assert received == []


# ------------------------------------------------------------------ the confirmation


async def test_an_unticked_form_publishes_nothing_and_connects_nowhere(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """The tick box is the whole of the consent, so without it nothing leaves."""
    receiver = FakeReceiver()
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    result = await _open_uninstall(hass, entry)
    mqtt_mock.async_publish.reset_mock()

    with patch(
        "custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_CONFIRM_UNINSTALL: False}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "uninstall_not_confirmed"}
    assert mqtt_mock.async_publish.call_args_list == []
    assert receiver.connections == 0


async def test_ticked_publishes_the_node_id_once_after_the_subscriptions_are_live(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """Exactly one `cmd/uninstall`, carrying the node id, and never before the broker listens.

    Home Assistant debounces SUBSCRIBE by a tenth of a second and the receiver answers in
    about that, so a command sent the moment the subscriptions exist in-process can be
    answered to a broker not yet forwarding the answer. The trackers are held here until
    the test releases them; nothing may be published before that.
    """
    entry = _entry()
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall")
    result = await _open_uninstall(hass, entry)
    mqtt_mock.async_publish.reset_mock()
    held: list[Callable[[], None]] = []

    def _hold(_hass: HomeAssistant, _topic: str, _qos: int, done: Callable[[], None]):
        held.append(done)
        return lambda: None

    with patch(
        "custom_components.enigma2_mqtt.uninstall.mqtt.async_on_subscribe_done", _hold
    ):
        progress = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_CONFIRM_UNINSTALL: True}
        )
        assert progress["type"] is FlowResultType.SHOW_PROGRESS
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(held) == 4
        assert _uninstall_publishes(mqtt_mock) == []

        for done in held:
            done()
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(progress["flow_id"])

    assert _uninstall_publishes(mqtt_mock) == [
        call(UNINSTALL_TOPIC, NODE_ID, 1, False, message_expiry_interval=None)
    ]
    assert received == [NODE_ID]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_reported"


# ------------------------------------------------------------------ the outcomes


async def test_without_credentials_the_receiver_saying_so_is_the_result(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """Retractions then `offline`: „the receiver said it removed the plugin"."""
    entry = _entry()
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall")

    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_reported"
    # An abort, not a saved options entry: nothing about the entry changes or reloads.
    assert entry.options == {}
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry


async def test_with_credentials_every_readback_passing_is_verified(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """SSH reads before the command and after the restart, and the two agree."""
    events: list[str] = []
    receiver = FakeReceiver(events=events)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver, events=events)

    with patch(
        "custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect
    ):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_verified"
    command_at = events.index("mqtt:cmd/uninstall")
    before, after = events[:command_at], events[command_at + 1 :]
    # The guards, the pids, the package and the settings block before the command…
    assert before == ["ssh:wget", "ssh:wget", "ssh:pidof", "ssh:opkg", "ssh:grep"]
    # …and after it the restart first, then every absence and the block again.
    assert after == [
        "ssh:pidof",
        "ssh:opkg",
        "ssh:ls",
        "ssh:test",
        "ssh:find",
        "ssh:wget",
        "ssh:grep",
    ]


async def test_ssh_that_cannot_connect_leaves_the_removal_unverified(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """The command still goes; the result says it was not verified, and why."""
    receiver = FakeReceiver(connect_fails=True)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall", receiver=receiver)

    with patch(
        "custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect
    ):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert received == [NODE_ID]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"] == "ssh (ssh_unavailable)"


@pytest.mark.parametrize(
    ("receiver", "detail"),
    [
        (FakeReceiver(removes_files=False), "opkg status, opkg info"),
        (FakeReceiver(hook_after="200"), "/mqttbridge 200"),
        (
            FakeReceiver(settings_hash_after="6" * 64),
            "config.plugins.mqttbridge",
        ),
        (FakeReceiver(restarts=False), "pidof enigma2"),
    ],
    ids=["package_still_there", "hook_still_served", "settings_changed", "no_restart"],
)
async def test_a_readback_that_disagrees_is_named(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    receiver: FakeReceiver,
    detail: str,
) -> None:
    """„Removed, not verified", naming what SSH found instead."""
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver)

    with (
        patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect),
        patch("custom_components.enigma2_mqtt.installer.ROLLBACK_RESTART_TIMEOUT", 0),
    ):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"].startswith(detail)


async def test_a_refusal_is_raised_with_the_receivers_own_words(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """„The receiver refused: `last_error`"."""
    entry = _entry()
    await async_setup_box(hass, entry)
    await _arm_box(hass, "refuse")

    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_refused"
    assert result["description_placeholders"]["detail"] == REFUSAL


async def test_a_failure_reported_before_offline_is_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """The plugin puts everything back and says which step failed; that is the answer."""
    entry = _entry()
    await async_setup_box(hass, entry)
    await _arm_box(hass, "fail_then_restore")

    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_refused"
    assert result["description_placeholders"]["detail"] == "opkg remove exited 255"


@pytest.mark.parametrize("answer", ["silence", "switch_off"])
async def test_a_receiver_that_does_not_act_is_not_reported_as_removed(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    answer: str,
) -> None:
    """Silence, and a box that merely went off, are both „the receiver did not act".

    A switched-off box ends on `offline` too, but leaves `info` and its announcement
    retained; only an uninstall retracts both first. That difference is the whole of what
    can be observed without SSH, so it must not be traded away.
    """
    entry = _entry()
    await async_setup_box(hass, entry)
    await _arm_box(hass, answer)

    with patch("custom_components.enigma2_mqtt.uninstall.UNINSTALL_TIMEOUT", 0.2):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_no_action"


_REFUSED = json.dumps({"cmd": "uninstall", "error": REFUSAL})


@pytest.mark.parametrize(
    ("stored", "fresh"),
    [
        # The broker holds empty `info` and announcement when the watch subscribes, and
        # the box then goes off: a fresh last will after stale retractions.
        ({INFO_TOPIC: "", ANNOUNCEMENT_TOPIC: ""}, [(AVAILABILITY_TOPIC, "offline")]),
        # The whole shape, but all of it retained — a replay of some earlier removal.
        ({INFO_TOPIC: "", ANNOUNCEMENT_TOPIC: "", AVAILABILITY_TOPIC: "offline"}, []),
        # A refusal from some earlier session.
        ({LAST_ERROR_TOPIC: _REFUSED}, []),
    ],
    ids=["retained_retractions", "retained_shape", "retained_refusal"],
)
async def test_retained_replays_are_never_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    stored: dict[str, str],
    fresh: list[tuple[str, str]],
) -> None:
    """What the broker held before the command cannot be the receiver's reply to it.

    The stale payloads go into the broker's retained store after the box is set up, so
    they reach the watch the way a broker delivers them — once, flagged retained, as its
    subscriptions land — and never reach the box, which is still offering the removal.
    """
    entry = _entry()
    await async_setup_box(hass, entry)
    permitted.update(stored)

    @callback
    def _replay(_msg: ReceiveMessage) -> None:
        for topic, payload in fresh:
            async_fire_mqtt_message(hass, topic, payload)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _replay)
    with patch("custom_components.enigma2_mqtt.uninstall.UNINSTALL_TIMEOUT", 0.2):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_no_action"


@pytest.mark.parametrize(
    "messages",
    [
        # `info` retracted and then published again: a reset, not a removal.
        [
            (INFO_TOPIC, ""),
            (ANNOUNCEMENT_TOPIC, ""),
            (INFO_TOPIC, json.dumps(PERMITTED_INFO)),
            (AVAILABILITY_TOPIC, "offline"),
        ],
        # Retracted, back `online`, and only then the last will.
        [
            (INFO_TOPIC, ""),
            (ANNOUNCEMENT_TOPIC, ""),
            (AVAILABILITY_TOPIC, "online"),
            (AVAILABILITY_TOPIC, "offline"),
        ],
    ],
    ids=["info_republished", "back_online"],
)
async def test_a_retraction_that_is_undone_does_not_count(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    messages: list[tuple[str, str]],
) -> None:
    """Only retractions still standing when `offline` arrives make the uninstall shape."""
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        for topic, payload in messages:
            async_fire_mqtt_message(hass, topic, payload)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _answer)
    with patch("custom_components.enigma2_mqtt.uninstall.UNINSTALL_TIMEOUT", 0.2):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_no_action"


async def test_a_complaint_about_another_command_is_not_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """`last_error` names the command it is about; only `uninstall` refuses this one."""
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(
            hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "zap", "error": "no such channel"})
        )
        async_fire_mqtt_message(hass, INFO_TOPIC, "")
        async_fire_mqtt_message(hass, ANNOUNCEMENT_TOPIC, "")
        async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _answer)
    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_reported"


@pytest.mark.parametrize(
    ("receiver", "reason"),
    [
        (FakeReceiver(recording=True), "recording"),
        (
            FakeReceiver(
                timers=[{"begin": 1, "end": 4_000_000_000, "state": 0, "disabled": False}]
            ),
            "timer_due",
        ),
    ],
    ids=["recording", "timer_due"],
)
async def test_the_ssh_guards_refuse_before_anything_is_published(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    receiver: FakeReceiver,
    reason: str,
) -> None:
    """The removal ends in a GUI restart, and a restart costs a recording."""
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall", receiver=receiver)

    with patch(
        "custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect
    ):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert received == []
    assert _uninstall_publishes(mqtt_mock) == []


# ------------------------------------------------------------------ deleting the entry


@pytest.mark.parametrize("credentials", [True, False], ids=["ssh_kept", "no_ssh"])
@pytest.mark.parametrize("permission", [True, False], ids=["allowed", "not_allowed"])
@pytest.mark.parametrize("online", [True, False], ids=["online", "offline"])
async def test_deleting_the_entry_never_uninstalls(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    credentials: bool,
    permission: bool,
    online: bool,
) -> None:
    """🔴 The whole of what deleting an entry sends is one `cmd/ha_mode = discovery`.

    Deleting a configuration entry is a decision about Home Assistant, reached from a page
    people delete things on by accident. Uninstalling is always a separate, named,
    confirmed act (ADR-0004), whatever the entry holds and whatever the receiver permits.
    """
    info = PERMITTED_INFO if permission else {**PERMITTED_INFO, "settings": {}}
    box_on_the_broker[INFO_TOPIC] = json.dumps(info)
    if not online:
        box_on_the_broker[AVAILABILITY_TOPIC] = "offline"
    entry = _entry(credentials=credentials)
    await async_setup_box(hass, entry)
    assert entry.runtime_data.uninstall_offered is (permission and online)
    mqtt_mock.async_publish.reset_mock()

    # Recorded as well as refused: a caller that swallowed the refusal would otherwise
    # have opened a connection and hidden it.
    attempts: list[str] = []

    def _no_ssh(name: str) -> Callable[..., None]:
        def _connect(*_args: Any, **_kwargs: Any) -> None:
            attempts.append(name)
            raise AssertionError("deleting an entry opened an SSH connection")

        return _connect

    with (
        patch(
            "custom_components.enigma2_mqtt.installer.asyncssh.connect", _no_ssh("asyncssh")
        ),
        patch("custom_components.enigma2_mqtt.installer._async_connect", _no_ssh("installer")),
        patch("custom_components.enigma2_mqtt.uninstall._async_connect", _no_ssh("uninstall")),
    ):
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert mqtt_mock.async_publish.call_args_list == [
        call(HA_MODE_TOPIC, "discovery", 1, False, message_expiry_interval=None)
    ]
    assert attempts == []

"""Removing the plugin from the receiver: offered, confirmed, acted on by the box, verified.

The receiver in these tests is the documentation's example box. Its MQTT side is the fake
broker every other test uses, answering `cmd/uninstall` the way the plugin's teardown does
- `info` and the announcement retracted, then `offline` - or refusing it on `last_error`.
Its SSH side is a fake that knows only the handful of fixed reads the verification makes,
and records every one of them, so an ordering can be asserted and a write would show up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
from unittest.mock import call, patch

import asyncssh
from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.config_entries import ConfigEntryState
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
from custom_components.enigma2_mqtt.uninstall import (
    _UninstallWatch,
    credentials_from_entry,
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
_UNINSTALL = "custom_components.enigma2_mqtt.uninstall"
SETTINGS_HASH = "5" * 64
_LOCK_REFUSAL = "Could not lock /run/opkg.lock: Resource temporarily unavailable."


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


@pytest.fixture(autouse=True)
def short_window() -> Any:
    """Close the watch's window in half a second rather than a minute.

    Without SSH readbacks the flow watches the whole window before it reports a removal,
    so every such test would otherwise take the full minute. The fake box answers inside
    the publish, long before half a second is up.
    """
    with (
        patch("custom_components.enigma2_mqtt.uninstall.UNINSTALL_TIMEOUT", 0.5),
        # The SSH side's own waits, cut to what a fake needs: a restart that is coming is
        # seen on the first look, and one that is not is given up on at once.
        patch("custom_components.enigma2_mqtt.installer.ROLLBACK_RESTART_TIMEOUT", 0.2),
        patch("custom_components.enigma2_mqtt.installer.ROLLBACK_RESTART_POLL_SECONDS", 0.01),
        # `create=True` only so that this file can be run against a build that predates
        # these waits, to show its tests failing there.
        patch(f"{_UNINSTALL}.WEBIF_READY_TIMEOUT", 0.2, create=True),
        patch(f"{_UNINSTALL}.WEBIF_READY_POLL_SECONDS", 0.01, create=True),
        patch(f"{_UNINSTALL}.LOCK_RETRY_SECONDS", 0, create=True),
    ):
        yield


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
    removes_package: bool = True
    removes_directory: bool = True
    removes_leftovers: bool = True
    hook_after: str = "404"
    # Whether OpenWebif answers after the removal, and whether `wget` prints a status.
    webif_after: bool = True
    hook_prints_status: bool = True
    settings_hash: str = SETTINGS_HASH
    settings_hash_after: str = SETTINGS_HASH
    # An arbitrary number: nothing asserts what a real receiver holds, only that the
    # count before and after agree.
    settings_count: int = 3
    settings_count_after: int = 3
    # How many `opkg status` calls before and after the removal find opkg's lock held,
    # and whether it fails after the removal for a reason that is not the lock.
    opkg_locked_before: int = 0
    opkg_refusal_before: str = _LOCK_REFUSAL
    opkg_locked_after: int = 0
    opkg_broken_after: bool = False
    # What `pidof enigma2` answers before the removal, when not the pid.
    pidof_before: CommandResult | None = None
    # An opkg failure that mentions „blocked" - not the lock - before the removal.
    opkg_blocked_before: bool = False
    # A transport failure on the first command containing this text.
    drop_on: str | None = None
    timeout_on: str | None = None
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
        if self.removes_package:
            self.installed = False
        if self.restarts:
            self.pid += 1
        self.settings_hash = self.settings_hash_after
        self.settings_count = self.settings_count_after


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
        if receiver.drop_on is not None and receiver.drop_on in command:
            receiver.drop_on = None
            raise asyncssh.ConnectionLost("simulated drop")
        if receiver.timeout_on is not None and receiver.timeout_on in command:
            receiver.timeout_on = None
            raise TimeoutError
        removed = receiver.removed
        if command.startswith("wget -q -O /dev/null http://127.0.0.1/api/statusinfo"):
            return CommandResult(0 if (receiver.webif_after or not removed) else 4)
        if "/api/statusinfo" in command:
            return CommandResult(0, json.dumps({"isRecording": receiver.recording}))
        if "/api/timerlist" in command:
            return CommandResult(0, json.dumps({"timers": receiver.timers}))
        if command == "pidof enigma2":
            if not removed and receiver.pidof_before is not None:
                return receiver.pidof_before
            return CommandResult(0, f"{receiver.pid}\n")
        if command.startswith("opkg status"):
            if not removed and receiver.opkg_blocked_before:
                return CommandResult(255, "", "opkg: operation blocked by policy")
            if not removed and receiver.opkg_locked_before:
                receiver.opkg_locked_before -= 1
                return CommandResult(255, "", receiver.opkg_refusal_before)
            if removed and receiver.opkg_locked_after:
                receiver.opkg_locked_after -= 1
                return CommandResult(255, "", _LOCK_REFUSAL)
            if removed and receiver.opkg_broken_after:
                return CommandResult(255, "", "opkg: cannot read /var/lib/opkg/status")
            if not receiver.installed:
                return CommandResult(0, "")
            return CommandResult(
                0, "Package: enigma2-plugin-extensions-mqttbridge\nVersion: 0.3.0\n"
            )
        if "sha256sum" in command:
            return CommandResult(0, f"{receiver.settings_hash}  -\n{receiver.settings_count}\n")
        if command.startswith("ls -d "):
            if not receiver.installed:
                return CommandResult(1, "")
            return CommandResult(
                0, "/var/lib/opkg/info/enigma2-plugin-extensions-mqttbridge.control\n"
            )
        if command.startswith("test -e "):
            return CommandResult(1 if removed and receiver.removes_directory else 0)
        if command.startswith("find "):
            if removed and receiver.removes_leftovers:
                return CommandResult(0, "")
            return CommandResult(
                0, "/usr/lib/enigma2/python/Components/Converter/MQTTBridge.pyc\n"
            )
        if command.startswith("wget -S "):
            code = receiver.hook_after if removed else "200"
            if not receiver.hook_prints_status:
                return CommandResult(4, "wget: unable to connect\n")
            return CommandResult(8 if code == "404" else 0, f"  HTTP/1.1 {code} X\n")
        raise AssertionError(f"unexpected command on the receiver: {command}")

    async def close(self) -> None:
        return None


# How long after the command the fake receiver answers. The MQTT test harness hands a
# published message to its subscribers inside the publish call itself, before Home
# Assistant's client has had the broker's acknowledgement; a real receiver answers only
# after a network round trip and a turn of its own main loop. Answering a moment later is
# the order a broker produces, and the one the rollback rule depends on.
ANSWER_DELAY = 0.02


def _after_publish(
    hass: HomeAssistant, answer: Callable[[ReceiveMessage], None]
) -> Callable[[ReceiveMessage], None]:
    """Run a fake receiver's answer once the command's publish has returned."""

    @callback
    def _deferred(msg: ReceiveMessage) -> None:
        hass.loop.call_later(ANSWER_DELAY, answer, msg)

    return _deferred


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
            # Retractions under way, then everything back and why - a failure the plugin
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

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _command))
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
    the last payload - the image, the version, the address - stays.
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

        # Confirmed one at a time: three of four is still a broker that may not forward
        # the answer, so nothing goes until the last one.
        for done in held[:3]:
            done()
            for _ in range(20):
                await asyncio.sleep(0)
            assert _uninstall_publishes(mqtt_mock) == []
        held[3]()
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
    # The guards, the pids, the package and the settings block before the command...
    assert before == ["ssh:wget", "ssh:wget", "ssh:pidof", "ssh:opkg", "ssh:grep"]
    # ...and after it the restart first, OpenWebif up, then every absence and the block.
    assert after == [
        "ssh:pidof",
        "ssh:wget",
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


_DIR = "/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"


@pytest.mark.parametrize(
    ("receiver", "detail"),
    [
        (FakeReceiver(removes_package=False), "opkg status, opkg info"),
        (FakeReceiver(removes_directory=False), _DIR),
        (FakeReceiver(removes_leftovers=False), "find MQTTBridge"),
        (FakeReceiver(hook_after="200"), "/mqttbridge 200"),
        (FakeReceiver(hook_prints_status=False), "/mqttbridge ?"),
        (FakeReceiver(webif_after=False), "OpenWebif not answering"),
        (
            FakeReceiver(settings_hash_after="6" * 64),
            "config.plugins.mqttbridge (sha256)",
        ),
        (FakeReceiver(settings_count_after=2), "config.plugins.mqttbridge (count)"),
        (FakeReceiver(restarts=False), "restart not seen"),
        # A restart that never came does not hide what else is wrong.
        (
            FakeReceiver(restarts=False, removes_package=False, settings_count_after=2),
            "restart not seen, opkg status, opkg info, config.plugins.mqttbridge (count)",
        ),
        (FakeReceiver(opkg_locked_after=99), "opkg status"),
    ],
    ids=[
        "package_still_there",
        "directory_still_there",
        "leftover_files",
        "hook_still_served",
        "hook_status_unreadable",
        "webif_never_answers",
        "settings_changed",
        "settings_count_changed",
        "no_restart",
        "no_restart_and_more",
        "opkg_locked_throughout",
    ],
)
async def test_a_readback_that_disagrees_is_named(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    receiver: FakeReceiver,
    detail: str,
) -> None:
    """„Removed, not verified", naming exactly what SSH found instead - nothing more."""
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"] == detail


async def test_a_briefly_held_opkg_lock_is_asked_again(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """opkg's lock held for a moment - the image's update check - is not a failed readback."""
    receiver = FakeReceiver(opkg_locked_after=2)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_verified"
    assert receiver.events.count("ssh:opkg") == 1 + 3


@pytest.mark.parametrize(
    ("failure", "text", "detail"),
    [
        ("drop_on", "/api/timerlist", "ssh (connection lost)"),
        ("timeout_on", "/api/timerlist", "ssh (timed out)"),
        ("drop_on", "ls -d", "ssh (connection lost)"),
        ("timeout_on", "ls -d", "ssh (timed out)"),
    ],
    ids=["drop_before", "timeout_before", "drop_after", "timeout_after"],
)
async def test_an_ssh_failure_costs_only_the_verification(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    failure: str,
    text: str,
    detail: str,
) -> None:
    """A dropped or timed-out SSH session is named, before the command and after it.

    `asyncssh.ConnectionLost` is not an `OSError`, and it used to reach the flow as
    „unknown". Before the command it costs the verification and the command still goes;
    after it, the removal is „removed, not verified" with the reason.
    """
    receiver = FakeReceiver(**{failure: text})
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert received == [NODE_ID]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"] == detail


async def test_a_package_opkg_does_not_know_is_not_verified(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """Nothing can be shown to have been removed that opkg did not list beforehand."""
    receiver = FakeReceiver(installed=False)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"] == "opkg status"


def test_a_password_without_its_pinned_host_key_is_no_credential() -> None:
    """A password is sent only to the identity it was confirmed against."""
    data = {
        CONF_SSH_HOST: "192.0.2.12",
        CONF_SSH_USERNAME: "root",
        CONF_SSH_PASSWORD: "not-a-real-password",
    }
    assert credentials_from_entry(data) is None
    assert credentials_from_entry({**data, CONF_SSH_HOST_KEY: HOST_KEY}) is not None


@pytest.mark.parametrize(
    "refusal",
    [REFUSAL, "an EPG import is running"],
    ids=["already_running", "epg_import_running"],
)
async def test_a_refusal_is_raised_with_the_receivers_own_words(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    refusal: str,
) -> None:
    """„The receiver refused: `last_error`", in whatever words the receiver chose."""
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _refuse(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(
            hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "uninstall", "error": refusal})
        )

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _refuse))

    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_refused"
    assert result["description_placeholders"]["detail"] == refusal


# What the plugin publishes when the removal fails after its `offline`: opkg refused, or
# the broker's acknowledgements never came. The shape of a removal first, then the plugin
# back on the broker, everything republished, and - when it can - the reason.
_SHAPE = [(INFO_TOPIC, ""), (ANNOUNCEMENT_TOPIC, ""), (AVAILABILITY_TOPIC, "offline")]
_BACK = [(AVAILABILITY_TOPIC, "online"), (INFO_TOPIC, json.dumps(PERMITTED_INFO))]
_OPKG_FAILED = json.dumps({"cmd": "uninstall", "error": "opkg remove exited 255"})


# The plugin's own sentences for a removal that failed after `offline` (enigma2-mqtt-bridge
# `uninstall.py`, main 8ad4c72): opkg refused - its lock held, say - and opkg reporting
# success with the package still on the disk.
_OPKG_REFUSED = (
    "the uninstall stopped at the package removal: opkg exited with status 255; "
    "the plugin is still installed"
)
_NOT_REMOVED = (
    "the uninstall stopped at the package removal: opkg reported success but the package "
    "is still on the receiver, and some of its files may already be gone; to put it back "
    "whole, run: opkg install --force-reinstall enigma2-plugin-extensions-mqttbridge"
)
_ANNOUNCEMENT = json.dumps({"node_id": NODE_ID, "name": BOX_NAME, "base_topic": BASE_TOPIC})


def _teardown_then_failure(sentence: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The plugin's real publishes, in its order, split where the failure happens.

    Steps 2 and 3: every retained topic it owns retracted in sorted order - its own
    `availability` among them - then `offline`. Then, when opkg or the acknowledgements
    fail, `restart_after_failed_uninstall`: a fresh session's `on_connect` publishes
    `online`, the snapshot, the announcement, and only then the `last_error`.
    """
    base = f"{BASE_TOPIC}/{NODE_ID}"
    teardown = [
        (AVAILABILITY_TOPIC, ""),
        (INFO_TOPIC, ""),
        (LAST_ERROR_TOPIC, ""),
        (f"{base}/power", ""),
        (f"{base}/service", ""),
        (ANNOUNCEMENT_TOPIC, ""),
        (AVAILABILITY_TOPIC, "offline"),
    ]
    failure = [
        (AVAILABILITY_TOPIC, "online"),
        (INFO_TOPIC, json.dumps(PERMITTED_INFO)),
        (f"{base}/power", "on"),
        (ANNOUNCEMENT_TOPIC, _ANNOUNCEMENT),
        (LAST_ERROR_TOPIC, json.dumps({"cmd": "uninstall", "error": sentence, "ts": 1})),
    ]
    return teardown, failure


@pytest.mark.parametrize(
    "sentence", [_OPKG_REFUSED, _NOT_REMOVED], ids=["opkg_refused", "not_removed"]
)
@pytest.mark.parametrize(
    "ssh", ["none", "unreachable", "kept"], ids=["no_ssh", "ssh_unreachable", "ssh_kept"]
)
async def test_a_removal_that_fails_after_offline_is_a_rollback(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    ssh: str,
    sentence: str,
) -> None:
    """`offline` is not the end of the answer, with or without SSH.

    The plugin says `offline` before it runs opkg; a removal that fails afterwards comes
    back online and says why on `last_error`. That sentence is the result - not „the
    receiver said it removed the plugin", and not a readback that waits for a restart
    that is never coming and then blames the restart. The failure arrives a moment after
    the teardown, as it does on a receiver, so with SSH it lands while the readbacks run.
    """
    entry = _entry(credentials=ssh != "none")
    await async_setup_box(hass, entry)
    # The plugin never removed anything, so the interface never restarts either.
    receiver = FakeReceiver(connect_fails=ssh == "unreachable")
    teardown, failure = _teardown_then_failure(sentence)

    @callback
    def _fail() -> None:
        for topic, payload in failure:
            async_fire_mqtt_message(hass, topic, payload)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        for topic, payload in teardown:
            async_fire_mqtt_message(hass, topic, payload)
        hass.loop.call_later(0.05, _fail)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
    with (
        patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect),
        # Long enough that only the rollback can end the flow in time.
        patch("custom_components.enigma2_mqtt.installer.ROLLBACK_RESTART_TIMEOUT", 30),
        patch("custom_components.enigma2_mqtt.uninstall.UNINSTALL_TIMEOUT", 30),
    ):
        async with asyncio.timeout(10):
            result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_rolled_back"
    assert result["description_placeholders"]["detail"] == sentence


async def test_a_complaint_about_another_command_after_the_shape_changes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """Only a `last_error` about `uninstall` is a rollback; one about `zap` is not."""
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        for topic, payload in _SHAPE:
            async_fire_mqtt_message(hass, topic, payload)
        async_fire_mqtt_message(
            hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "zap", "error": "no such channel"})
        )

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_reported"


async def test_without_an_announcement_to_retract_info_and_offline_are_the_shape(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """In `ha_mode: off` the plugin publishes no announcement, so none is retracted."""
    permitted[INFO_TOPIC] = json.dumps({**PERMITTED_INFO, "ha_mode": "off"})
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, INFO_TOPIC, "")
        async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_reported"


async def test_with_an_announcement_its_retraction_is_part_of_the_shape(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """In `integration` mode `info` and `offline` alone are not a removal."""
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, INFO_TOPIC, "")
        async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_no_action"


async def test_a_box_left_on_an_entry_that_is_not_loaded_is_not_offered(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """A stale `runtime_data` on an entry that is not loaded offers and sends nothing.

    Home Assistant deletes `runtime_data` after a clean unload, but not on every path out
    of the loaded state; what a box last heard is not what the receiver says now.
    """
    entry = _entry()
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall")
    result = await _open_uninstall(hass, entry)
    assert entry.runtime_data.uninstall_offered

    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    result = await _confirm(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_not_offered"
    assert received == []
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] is FlowResultType.FORM


async def test_an_entry_unloaded_while_the_form_is_open_is_not_offered(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """A stale box left on an unloaded entry is not a receiver saying anything now."""
    entry = _entry()
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall")
    result = await _open_uninstall(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    result = await _confirm(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_not_offered"
    assert received == []


@pytest.mark.parametrize(
    "back",
    [_BACK, _BACK[:1], _BACK[1:]],
    ids=["online_and_info", "online", "info"],
)
async def test_a_receiver_that_comes_back_without_a_reason_did_not_complete(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    back: list[tuple[str, str]],
) -> None:
    """Back on the broker after the shape, and no `last_error`: „did not complete"."""
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        for topic, payload in [*_SHAPE, *back]:
            async_fire_mqtt_message(hass, topic, payload)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_incomplete"


async def test_with_readbacks_the_shape_is_enough_to_go_and_look(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """With SSH readbacks to follow, the flow does not sit out the window first."""
    receiver = FakeReceiver()
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver)

    with (
        patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect),
        # A window no test would sit out: reaching the readbacks proves it was not waited.
        patch("custom_components.enigma2_mqtt.uninstall.UNINSTALL_TIMEOUT", 3600),
    ):
        async with asyncio.timeout(10):
            result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_verified"


async def test_a_failure_after_retractions_but_before_offline_is_a_rollback(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """Retractions, then everything back and why, with no `offline` in between.

    The connection dropped mid-retraction, so the `offline` that completes the shape never
    reached Home Assistant - but the teardown had begun, so this is a rollback, not a
    refusal.
    """
    entry = _entry()
    await async_setup_box(hass, entry)
    await _arm_box(hass, "fail_then_restore")

    # A window no test would sit out: the answer is the `last_error`, not the timeout.
    with patch(f"{_UNINSTALL}.UNINSTALL_TIMEOUT", 3600):
        async with asyncio.timeout(10):
            result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_rolled_back"
    assert result["description_placeholders"]["detail"] == "opkg remove exited 255"


@pytest.mark.parametrize(
    "retraction",
    [(AVAILABILITY_TOPIC, ""), (INFO_TOPIC, ""), (ANNOUNCEMENT_TOPIC, "")],
    ids=["availability", "info", "announcement"],
)
async def test_any_one_retraction_before_the_failure_makes_it_a_rollback(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    retraction: tuple[str, str],
) -> None:
    """The plugin retracts in sorted order, its own `availability` first; one is enough."""
    entry = _entry()
    await async_setup_box(hass, entry)
    _teardown, failure = _teardown_then_failure(_OPKG_REFUSED)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, *retraction)
        for topic, payload in failure:
            async_fire_mqtt_message(hass, topic, payload)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_rolled_back"
    assert result["description_placeholders"]["detail"] == _OPKG_REFUSED


async def test_opkg_busy_before_the_command_refuses_without_publishing(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """opkg's lock held through every retry: the removal would roll back, so it is not sent."""
    receiver = FakeReceiver(opkg_locked_before=99)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "busy"
    assert received == []
    assert _uninstall_publishes(mqtt_mock) == []
    assert receiver.events.count("ssh:opkg") == 6


async def test_opkg_briefly_busy_before_the_command_is_asked_again(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """A lock that lets go within the retries is waited for, and the removal goes ahead."""
    receiver = FakeReceiver(opkg_locked_before=2)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_verified"


async def test_only_a_held_lock_is_asked_again(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """opkg failing for another reason is an answer at once, not five more questions."""
    receiver = FakeReceiver(opkg_broken_after=True)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"] == "opkg status"
    # Once before the command, once after it.
    assert receiver.events.count("ssh:opkg") == 2


async def test_a_pidof_timeout_before_the_command_reads_like_any_ssh_timeout(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """„ssh (timed out)", never the installer's `restart_failed` code."""
    receiver = FakeReceiver(timeout_on="pidof")
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"] == "ssh (timed out)"


@pytest.mark.parametrize("language", ["strings", "en", "pl", "de"])
def test_the_rollback_sentence_leaves_the_package_state_to_the_receiver(
    language: str,
) -> None:
    """The plugin's own sentence says whether the package is still there, or half gone.

    A fixed „the plugin is still installed" after it would contradict the sentence for an
    opkg that reported success with the package on the disk and some files already gone.
    """
    root = Path(__file__).parents[1] / "custom_components" / "enigma2_mqtt"
    path = root / ("strings.json" if language == "strings" else f"translations/{language}.json")
    text = json.loads(path.read_text(encoding="utf-8"))["options"]["abort"][
        "uninstall_rolled_back"
    ]
    assert text.rstrip().endswith("{detail}")


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
        # The whole shape, but all of it retained - a replay of some earlier removal.
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
    they reach the watch the way a broker delivers them - once, flagged retained, as its
    subscriptions land - and never reach the box, which is still offering the removal.
    """
    entry = _entry()
    await async_setup_box(hass, entry)
    permitted.update(stored)

    @callback
    def _replay(_msg: ReceiveMessage) -> None:
        for topic, payload in fresh:
            async_fire_mqtt_message(hass, topic, payload)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _replay))
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

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
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

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
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


# ------------------------------------------------------------------ whose retraction it is


def _before_the_command(
    hass: HomeAssistant, messages: list[tuple[str, str]]
) -> Callable[..., Any]:
    """Let a third client's messages land once the watch is listening, before the command.

    They arrive after the watch's subscriptions are confirmed and before the watch is armed
    and the command published - exactly the window in which somebody else's retraction used
    to be taken for the receiver's own teardown.
    """
    real = _UninstallWatch.async_wait_until_established

    async def _wait(self: _UninstallWatch) -> None:
        await real(self)
        for topic, payload in messages:
            async_fire_mqtt_message(hass, topic, payload)

    return _wait


@pytest.mark.parametrize(
    "third_party",
    [
        # Another client switched the plugin's `ha_mode` off: it retracts the announcement.
        [(ANNOUNCEMENT_TOPIC, "")],
        # Another client sent `cmd/reset`: everything retracted, then republished.
        [
            (AVAILABILITY_TOPIC, ""),
            (INFO_TOPIC, ""),
            (ANNOUNCEMENT_TOPIC, ""),
            (AVAILABILITY_TOPIC, "online"),
            (INFO_TOPIC, json.dumps(PERMITTED_INFO)),
            (ANNOUNCEMENT_TOPIC, _ANNOUNCEMENT),
        ],
        # Somebody emptied `availability` by hand.
        [(AVAILABILITY_TOPIC, "")],
    ],
    ids=["ha_mode_off", "cmd_reset", "empty_availability"],
)
async def test_somebody_elses_retraction_before_the_command_is_not_a_rollback(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    third_party: list[tuple[str, str]],
) -> None:
    """Only a retraction after the command has been sent counts toward a rollback."""
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _refuse(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, _REFUSED)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _refuse))
    with patch.object(
        _UninstallWatch, "async_wait_until_established", _before_the_command(hass, third_party)
    ):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "uninstall_refused"
    assert result["description_placeholders"]["detail"] == REFUSAL


@pytest.mark.parametrize(
    "republish",
    [
        (INFO_TOPIC, json.dumps(PERMITTED_INFO)),
        (ANNOUNCEMENT_TOPIC, _ANNOUNCEMENT),
        (AVAILABILITY_TOPIC, "online"),
    ],
    ids=["info", "announcement", "availability"],
)
async def test_an_ordinary_republish_before_a_refusal_is_not_a_retraction(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    republish: tuple[str, str],
) -> None:
    """A payload that is not empty is a republish, not the teardown beginning."""
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, *republish)
        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, _REFUSED)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _answer))
    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_refused"
    assert result["description_placeholders"]["detail"] == REFUSAL


async def test_an_opkg_failure_that_says_blocked_is_not_the_lock(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """„blocked" contains „lock"; only opkg's lock message is waited out or refused on."""
    receiver = FakeReceiver(opkg_blocked_before=True)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    # Not „busy": the command went, and only the verification was lost.
    assert received == [NODE_ID]
    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"] == "opkg status"
    assert receiver.events.count("ssh:opkg") == 1


@pytest.mark.parametrize(
    "answer",
    [CommandResult(2, "", "pidof: error"), CommandResult(0, "enigma2\n")],
    ids=["non_zero_exit", "not_a_number"],
)
async def test_an_unusable_pidof_before_the_command_costs_the_verification(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    answer: CommandResult,
) -> None:
    """`pidof` that fails or prints something that is not a pid is not a set of pids."""
    receiver = FakeReceiver(pidof_before=answer)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert received == [NODE_ID]
    assert result["reason"] == "uninstall_not_verified"
    assert result["description_placeholders"]["detail"] == "pidof enigma2"


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("strings", "The receiver started the removal but aborted it: {detail}"),
        ("en", "The receiver started the removal but aborted it: {detail}"),
        ("pl", "Dekoder rozpoczął usuwanie, ale je przerwał: {detail}"),
        ("de", "Der Receiver hat das Entfernen begonnen, aber abgebrochen: {detail}"),
    ],
)
def test_the_rollback_ending_says_the_removal_was_broken_off(language: str, text: str) -> None:
    """„przerwał", not „wycofał": the receiver stopped the removal; nothing is implied about
    how much of it was undone, which the receiver's own sentence says."""
    root = Path(__file__).parents[1] / "custom_components" / "enigma2_mqtt"
    path = root / ("strings.json" if language == "strings" else f"translations/{language}.json")
    abort = json.loads(path.read_text(encoding="utf-8"))["options"]["abort"]
    assert abort["uninstall_rolled_back"] == text


# ------------------------------------------------------------------ in-publish delivery


async def _answer_inside_the_publish(
    hass: HomeAssistant,
    inside: list[tuple[str, str]],
    after: list[tuple[str, str]] | None = None,
) -> None:
    """A receiver whose answer arrives before the publish call has returned.

    After a stall of the event loop the broker's acknowledgement and the receiver's first
    messages come in one read, and Home Assistant's client dispatches them as it reads -
    so the answer is handled inside the publish. The MQTT test harness delivers exactly
    that way when an answer is sent from the command's own callback.
    """

    @callback
    def _later() -> None:
        for topic, payload in after or []:
            async_fire_mqtt_message(hass, topic, payload)

    @callback
    def _answer(_msg: ReceiveMessage) -> None:
        for topic, payload in inside:
            async_fire_mqtt_message(hass, topic, payload)
        if after:
            hass.loop.call_later(0.05, _later)

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _answer)


async def test_a_teardown_delivered_inside_the_publish_is_a_removal(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """The retractions and `offline`, all handled before the publish returns."""
    entry = _entry()
    await async_setup_box(hass, entry)
    teardown, _failure = _teardown_then_failure(_OPKG_REFUSED)
    await _answer_inside_the_publish(hass, teardown)

    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_reported"


async def test_a_teardown_inside_the_publish_then_a_failure_is_aborted(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """The teardown inside the publish, the plugin's failure path a moment later."""
    entry = _entry()
    await async_setup_box(hass, entry)
    teardown, failure = _teardown_then_failure(_OPKG_REFUSED)
    await _answer_inside_the_publish(hass, teardown, failure)

    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_rolled_back"
    assert result["description_placeholders"]["detail"] == _OPKG_REFUSED


async def test_a_drop_mid_retraction_delivered_inside_the_publish_is_aborted(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """Retractions, no `offline`, then everything back and why - all inside the publish.

    Counting retractions only once the publish had returned read these as somebody
    else's, and reported a teardown that had begun as a refusal.
    """
    entry = _entry()
    await async_setup_box(hass, entry)
    teardown, failure = _teardown_then_failure(_OPKG_REFUSED)
    await _answer_inside_the_publish(hass, [*teardown[:-1], *failure])

    result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_rolled_back"
    assert result["description_placeholders"]["detail"] == _OPKG_REFUSED


async def test_an_announcement_retracted_just_before_the_command_completes_the_shape(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
) -> None:
    """The shape is what the broker holds, whoever emptied it; only the rollback is scoped.

    `ha_mode` switched off a moment before the command retracts the announcement, and the
    plugin, now in `off`, has no announcement left to retract. Its `info` retraction and
    `offline` still complete the removal's shape.
    """
    entry = _entry()
    await async_setup_box(hass, entry)

    @callback
    def _teardown(_msg: ReceiveMessage) -> None:
        async_fire_mqtt_message(hass, INFO_TOPIC, "")
        async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")

    await mqtt.async_subscribe(hass, UNINSTALL_TOPIC, _after_publish(hass, _teardown))
    with patch.object(
        _UninstallWatch,
        "async_wait_until_established",
        _before_the_command(hass, [(ANNOUNCEMENT_TOPIC, "")]),
    ):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    assert result["reason"] == "uninstall_reported"


@pytest.mark.parametrize(
    ("refusal", "busy"),
    [
        ("Could not lock /run/opkg.lock: Resource temporarily unavailable.", True),
        ("Command failed to capture privilege lock", True),
        ("Could not create lock file /run/opkg.lock: Read-only file system.", False),
        ("Could not create lock file directory /run: Read-only file system.", False),
    ],
    ids=["held_lock", "privilege_lock", "no_lock_file", "no_lock_directory"],
)
async def test_only_a_held_lock_is_busy(
    hass: HomeAssistant,
    mqtt_mock,
    permitted: dict[str, str | bytes],
    refusal: str,
    busy: bool,
) -> None:
    """A lock somebody holds is waited for; a lock that cannot be created is not."""
    receiver = FakeReceiver(opkg_locked_before=99, opkg_refusal_before=refusal)
    entry = _entry(credentials=True)
    await async_setup_box(hass, entry)
    received = await _arm_box(hass, "uninstall", receiver=receiver)

    with patch("custom_components.enigma2_mqtt.uninstall._async_connect", receiver.connect):
        result = await _confirm(hass, await _open_uninstall(hass, entry))

    if busy:
        assert result["reason"] == "busy"
        assert received == []
        assert receiver.events.count("ssh:opkg") == 6
    else:
        # Asked once, not retried, and only the verification is lost.
        assert received == [NODE_ID]
        assert result["reason"] == "uninstall_not_verified"
        assert result["description_placeholders"]["detail"] == "opkg status"
        assert receiver.events.count("ssh:opkg") == 1

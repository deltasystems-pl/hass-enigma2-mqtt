"""Guided install and SSH credential lifecycle in the config flow."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_MQTT, SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import (
    EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE,
    EVENT_DATA_ENTRY_FLOW_PROGRESSED,
    FlowResultType,
    UnknownFlow,
)
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt.config_flow import INSTALL_PHASES
from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_KEEP_SSH_CREDENTIALS,
    CONF_NODE_ID,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    DOMAIN,
)
from custom_components.enigma2_mqtt.installer import (
    HostKey,
    InstallerError,
    InstallerErrorCode,
    InstallResult,
    Preflight,
)

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    async_arm_ha_mode_ack,
    async_setup_box,
)

HOST_KEY = HostKey("ssh-ed25519", "ssh-ed25519 AAAAtest", "SHA256:test")
PREFLIGHT = Preflight("openvix", "3.12", 10_000_000, None, False, False, 0)


async def _install_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "install"}
    )
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_probe_host_key",
        AsyncMock(return_value=HOST_KEY),
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SSH_HOST: "receiver.local", CONF_SSH_PORT: 22}
        )


async def _announce(hass: HomeAssistant):
    """Start the discovery flow the retained announcement keeps re-creating."""
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_MQTT},
        data=MqttServiceInfo(
            topic=ANNOUNCEMENT_TOPIC,
            payload=json.dumps(ANNOUNCEMENT),
            qos=0,
            retain=True,
            subscribed_topic="enigma2mqtt/discovery/#",
            timestamp=0.0,
        ),
    )


def _install_input(keep: bool = False):
    return {
        CONF_SSH_USERNAME: "root",
        CONF_SSH_PASSWORD: "secret",
        "broker_host": "broker.local",
        "broker_port": 1883,
        "broker_username": "receiver",
        "broker_password": "broker-secret",
        CONF_NODE_ID: NODE_ID,
        CONF_BASE_TOPIC: "enigma2",
        CONF_KEEP_SSH_CREDENTIALS: keep,
    }


async def test_install_creates_entry_and_discards_credentials_by_default(
    hass: HomeAssistant, mqtt_mock
) -> None:
    result = await _install_form(hass)
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_install",
        AsyncMock(return_value=InstallResult("0.1.0", True, True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _install_input()
        )
        await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert CONF_SSH_PASSWORD not in entry.data
    assert entry.data[CONF_SSH_HOST] == "receiver.local"


async def test_install_keeps_credentials_only_with_explicit_consent(
    hass: HomeAssistant, mqtt_mock
) -> None:
    result = await _install_form(hass)
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_install",
        AsyncMock(return_value=InstallResult("0.1.0", True, True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _install_input(keep=True)
        )
        await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_SSH_PASSWORD] == "secret"
    assert entry.data[CONF_SSH_HOST_KEY] == HOST_KEY.public_key


async def test_a_waiting_announcement_does_not_block_the_guided_install(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """The offer a receiver's retained announcement keeps making gives way.

    The plugin's announcement is retained, so a box that has ever been on the broker
    has a `confirm` card waiting at every Home Assistant start and every MQTT
    reconnect. That is the same receiver the installer is being pointed at, and
    treating it as a competing flow aborted the install with `already_in_progress`
    the moment the SSH and broker passwords were submitted.
    """
    offer = await _announce(hass)
    assert offer["type"] is FlowResultType.FORM

    result = await _install_form(hass)
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_install",
        AsyncMock(return_value=InstallResult("0.1.0", True, True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _install_input()
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_the_install_takes_the_offer_down_while_it_is_still_running(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """Not at the end — at the start, which is where the difference is.

    Home Assistant retires a competing flow by itself when an entry claims the unique
    id. A guided install does not reach that point for minutes, and a discovery card
    for the same receiver sitting on the screen for that long is a second entry
    waiting to be made by whoever walks past.
    """
    running = asyncio.Event()
    finish = asyncio.Event()

    async def blocking_install(*_args):
        running.set()
        await finish.wait()
        return InstallResult("0.1.0", True, True)

    offer = await _announce(hass)
    assert offer["type"] is FlowResultType.FORM

    result = await _install_form(hass)
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_install",
        AsyncMock(side_effect=blocking_install),
    ):
        progress = await hass.config_entries.flow.async_configure(
            result["flow_id"], _install_input()
        )
        await running.wait()

        assert hass.config_entries.async_entries(DOMAIN) == []
        assert [
            flow["flow_id"]
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        ] == [progress["flow_id"]]

        finish.set()
        await hass.async_block_till_done()
        await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_second_guided_install_waits_rather_than_cancelling_the_first(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """Taking the unique id from a running install would unwind a live receiver.

    The first flow's task is what holds the transaction — the snapshot, the package,
    the receiver's own lock. Aborting that flow cancels the task, and the first
    install would then be rolling a box back while the second one writes to it.
    """
    running = asyncio.Event()
    finish = asyncio.Event()
    started = 0

    async def blocking_install(*_args):
        nonlocal started
        started += 1
        running.set()
        await finish.wait()
        return InstallResult("0.1.0", True, True)

    first = await _install_form(hass)
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_install",
        AsyncMock(side_effect=blocking_install),
    ):
        progress = await hass.config_entries.flow.async_configure(
            first["flow_id"], _install_input()
        )
        await running.wait()

        second = await _install_form(hass)
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"], _install_input()
        )

        assert second["type"] is FlowResultType.ABORT
        assert second["reason"] == "already_in_progress"
        assert started == 1
        assert [
            flow["flow_id"]
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        ] == [progress["flow_id"]]

        finish.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert started == 1


async def test_an_announcement_during_an_install_gives_way_to_it(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """The other direction: the install owns the node id once it has started."""
    running = asyncio.Event()
    finish = asyncio.Event()

    async def blocking_install(*_args):
        running.set()
        await finish.wait()
        return InstallResult("0.1.0", True, True)

    result = await _install_form(hass)
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_install",
        AsyncMock(side_effect=blocking_install),
    ):
        progress = await hass.config_entries.flow.async_configure(
            result["flow_id"], _install_input()
        )
        await running.wait()
        assert progress["type"] is FlowResultType.SHOW_PROGRESS

        offer = await _announce(hass)
        assert offer["type"] is FlowResultType.ABORT
        assert offer["reason"] == "already_in_progress"

        finish.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(progress["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_the_manual_path_still_adds_a_box_that_is_being_offered(
    hass: HomeAssistant, mqtt_mock, retained: dict[str, str]
) -> None:
    """Unchanged: it never raised on the offer, and it still does not.

    Nothing here withdraws the offer either. Home Assistant retires it by itself once
    an entry claims the unique id, which is the moment the manual path reaches — and
    is exactly the moment the guided install does not reach for several minutes,
    which is why that path has to take the offer out of the way on its own.
    """
    offer = await _announce(hass)
    assert offer["type"] is FlowResultType.FORM

    retained[INFO_TOPIC] = json.dumps(INFO)
    await async_arm_ha_mode_ack(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_TOPIC: BASE_TOPIC, CONF_NODE_ID: NODE_ID}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def _install_watching_the_frontend(
    hass: HomeAssistant, install
) -> tuple[list[Any], list[float]]:
    """Run one guided install with a frontend that behaves the way the real one does.

    It answers every „this flow changed" notification by posting to the flow — a post
    is what finishes a progress step — and its post cannot arrive in the same loop
    iteration as the notification that caused it, because it is on the other end of a
    socket. It also records the progress-bar updates, which reach it without it having
    to ask the flow for anything.
    """
    answers: list[Any] = []
    fractions: list[float] = []
    posts: list[asyncio.Task] = []
    flow_id = (await _install_form(hass))["flow_id"]

    async def _post() -> None:
        await asyncio.sleep(0)
        try:
            answers.append(await hass.config_entries.flow.async_configure(flow_id))
        except UnknownFlow:
            answers.append("invalid flow specified")

    @callback
    def _refresh_requested(event) -> None:
        if event.data.get("flow_id") == flow_id and event.data.get("refresh"):
            posts.append(hass.async_create_task(_post()))

    @callback
    def _bar_moved(event) -> None:
        if event.data.get("flow_id") == flow_id:
            fractions.append(event.data["progress"])

    unsubscribe = [
        hass.bus.async_listen(EVENT_DATA_ENTRY_FLOW_PROGRESSED, _refresh_requested),
        hass.bus.async_listen(EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE, _bar_moved),
    ]
    try:
        with patch(
            "custom_components.enigma2_mqtt.config_flow.async_install",
            AsyncMock(side_effect=install),
        ):
            progress = await hass.config_entries.flow.async_configure(flow_id, _install_input())
            assert progress["type"] is FlowResultType.SHOW_PROGRESS
            await hass.async_block_till_done()
            await asyncio.gather(*posts)
    finally:
        for cancel in unsubscribe:
            cancel()
    return answers, fractions


async def _reporting_install(_hass, _request, progress_cb):
    """Report every phase, yielding in between as a real transaction does."""
    for phase in INSTALL_PHASES:
        progress_cb(phase)
        await asyncio.sleep(0)
    return InstallResult("0.1.0", True, True)


async def _failing_install(_hass, _request, progress_cb):
    """Fail in the middle, which is where the reason on the screen matters most."""
    progress_cb("upload")
    await asyncio.sleep(0)
    raise InstallerError(InstallerErrorCode.NO_SPACE)


async def test_a_successful_install_ends_on_the_created_entry(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """Exactly one caller finishes the flow, and it answers the client waiting on it.

    Home Assistant advances a progress step itself when the task it was handed
    resolves, and notifies the frontend; the frontend answers by posting, and that
    post is what creates the entry. A phase that asked for the same notification put a
    second caller on the flow — and the one that lost the race was answered with
    „Invalid flow specified" at the end of an install that had succeeded.
    """
    answers, _ = await _install_watching_the_frontend(hass, _reporting_install)

    assert len(answers) == 1
    assert answers[0]["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def _rollback_restart_failed_install(_hass, _request, progress_cb):
    """Fail after the receiver has been restored but not restarted."""
    progress_cb("announcement")
    await asyncio.sleep(0)
    raise InstallerError(InstallerErrorCode.ROLLBACK_RESTART_FAILED)


async def test_a_rollback_that_only_lost_the_interface_says_that_on_the_screen(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """The advice differs, so the abort reason has to.

    A rollback whose restore worked and whose restart did not leaves a receiver that
    needs a power cycle and nothing else. Reported as `rollback_failed` it sent the
    person to inspect a backup directory instead, on a box with nothing wrong with its
    files.
    """
    answers, _ = await _install_watching_the_frontend(hass, _rollback_restart_failed_install)

    assert len(answers) == 1
    assert answers[0]["type"] is FlowResultType.ABORT
    # The literal, because that is the key the abort text is looked up by.
    assert answers[0]["reason"] == "rollback_restart_failed"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def _rollback_lock_failed_install(_hass, _request, progress_cb):
    """Fail after the receiver has been restored but could not be unlocked."""
    progress_cb("announcement")
    await asyncio.sleep(0)
    raise InstallerError(InstallerErrorCode.ROLLBACK_LOCK_FAILED)


async def test_a_receiver_left_locked_says_that_on_the_screen(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """The next attempt is refused as busy, so this cannot be a log line only."""
    answers, _ = await _install_watching_the_frontend(hass, _rollback_lock_failed_install)

    assert len(answers) == 1
    assert answers[0]["type"] is FlowResultType.ABORT
    # The literal, because that is the key the abort text is looked up by.
    assert answers[0]["reason"] == "rollback_lock_failed"


async def test_a_failed_install_ends_on_its_reason(hass: HomeAssistant, mqtt_mock) -> None:
    """The abort screen is the one that has something worth reading on it.

    A phase reported shortly before a failure raced the same way a phase reported
    shortly before success did, and losing that race replaced „the receiver does not
    have enough free space" with „Invalid flow specified".
    """
    answers, _ = await _install_watching_the_frontend(hass, _failing_install)

    assert len(answers) == 1
    assert answers[0]["type"] is FlowResultType.ABORT
    assert answers[0]["reason"] == InstallerErrorCode.NO_SPACE.value
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_the_progress_bar_still_moves_through_every_phase(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """What the phases are for now that they are not a caption that changes."""
    _, fractions = await _install_watching_the_frontend(hass, _reporting_install)

    assert fractions == [
        (index + 1) / len(INSTALL_PHASES) for index in range(len(INSTALL_PHASES))
    ]


async def test_unexpected_install_exception_never_creates_an_entry(
    hass: HomeAssistant, mqtt_mock, caplog: pytest.LogCaptureFixture
) -> None:
    """The screen can only say "unknown"; the log has to say more than that.

    A bare `except Exception` that sets a string and drops the traceback turns every
    bug in this path into an unreportable one. The message itself interpolates nothing,
    so neither the SSH password nor the broker password can reach the log through it.
    """
    result = await _install_form(hass)
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_install",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _install_input()
        )
        await hass.async_block_till_done()

    assert hass.config_entries.async_entries(DOMAIN) == []
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"
    assert "Unexpected failure while installing the receiver plugin" in caplog.text
    assert "RuntimeError: boom" in caplog.text
    assert "secret" not in caplog.text
    assert "broker-secret" not in caplog.text


async def test_aborting_progress_cancels_the_transaction_and_clears_secrets(
    hass: HomeAssistant, mqtt_mock
) -> None:
    """Home Assistant owns progress cancellation; the flow drops its request too."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_forever(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    result = await _install_form(hass)
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_install",
        AsyncMock(side_effect=wait_forever),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _install_input()
        )
        await started.wait()
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        progressed = asyncio.Event()

        @callback
        def progress_changed(event):
            if event.data.get("flow_id") == result["flow_id"]:
                progressed.set()

        # The bar, not a request to re-read the flow: a phase deliberately asks the
        # frontend for nothing, which is its own test.
        unsubscribe = hass.bus.async_listen(
            EVENT_DATA_ENTRY_FLOW_PROGRESS_UPDATE, progress_changed
        )
        try:
            flow._async_install_progress("upload")
            await asyncio.wait_for(progressed.wait(), timeout=1)
            assert flow._install_phase == "upload"
            assert progressed.is_set()
        finally:
            unsubscribe()
        hass.config_entries.flow.async_abort(result["flow_id"])
        await asyncio.wait_for(cancelled.wait(), timeout=5)

    assert flow._install_request is None
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_existing_entry_can_store_then_forget_pinned_credentials(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    await async_setup_box(hass, config_entry)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "dangerous_buttons": False,
            "wol_mac": "",
            "bouquets": [],
            "configure_ssh": True,
        },
    )
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_probe_host_key",
        AsyncMock(return_value=HOST_KEY),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SSH_HOST: "receiver.local", CONF_SSH_PORT: 22}
        )
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_preflight",
        AsyncMock(return_value=PREFLIGHT),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SSH_USERNAME: "root", CONF_SSH_PASSWORD: "secret"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.data[CONF_SSH_PASSWORD] == "secret"

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "dangerous_buttons": False,
            "wol_mac": "",
            "bouquets": [],
            CONF_KEEP_SSH_CREDENTIALS: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_SSH_PASSWORD not in config_entry.data
    assert CONF_SSH_HOST_KEY not in config_entry.data


async def test_failed_ssh_enrollment_does_not_apply_pending_plugin_settings(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """Receiver and HA settings remain coherent when SSH authentication fails."""
    await async_setup_box(hass, config_entry)
    box = config_entry.runtime_data
    box.info["settings"] = {
        "publish_keys": True,
        "screenshot": "on_zap",
        "screenshot_interval": 60,
    }
    box.async_command = AsyncMock()
    old_options = dict(config_entry.options)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "dangerous_buttons": False,
            "wol_mac": "",
            "bouquets": [],
            "publish_keys": False,
            "screenshot": "off",
            "screenshot_interval": 120,
            "configure_ssh": True,
        },
    )
    box.async_command.assert_not_awaited()
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_probe_host_key",
        AsyncMock(return_value=HOST_KEY),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SSH_HOST: "receiver.local", CONF_SSH_PORT: 22}
        )
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_preflight",
        AsyncMock(side_effect=InstallerError(InstallerErrorCode.AUTH_FAILED)),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SSH_USERNAME: "root", CONF_SSH_PASSWORD: "bad"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "auth_failed"}
    box.async_command.assert_not_awaited()
    assert config_entry.options == old_options


async def test_reauth_updates_only_ssh_fields_without_reloading_mqtt_entry(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """A bad optional update password must not take MQTT entities down."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "receiver.local",
            CONF_SSH_PORT: 22,
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "old",
            CONF_SSH_HOST_KEY: HOST_KEY.public_key,
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": config_entry.entry_id},
        data=dict(config_entry.data),
    )
    with (
        patch(
            "custom_components.enigma2_mqtt.config_flow.async_probe_host_key",
            AsyncMock(return_value=HOST_KEY),
        ),
        patch(
            "custom_components.enigma2_mqtt.config_flow.async_preflight",
            AsyncMock(return_value=PREFLIGHT),
        ),
        patch.object(hass.config_entries, "async_reload") as reload_entry,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SSH_USERNAME: "root", CONF_SSH_PASSWORD: "new"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_SSH_PASSWORD] == "new"
    reload_entry.assert_not_called()


async def test_changed_host_key_requires_an_explicit_confirmation(
    hass: HomeAssistant, mqtt_mock, config_entry: MockConfigEntry
) -> None:
    """Reauthentication never silently replaces a pinned receiver identity."""
    changed = HostKey("ssh-ed25519", "ssh-ed25519 AAAAchanged", "SHA256:changed")
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "receiver.local",
            CONF_SSH_PORT: 22,
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "old",
            CONF_SSH_HOST_KEY: HOST_KEY.public_key,
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": config_entry.entry_id},
        data=dict(config_entry.data),
    )
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_probe_host_key",
        AsyncMock(return_value=changed),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SSH_USERNAME: "root", CONF_SSH_PASSWORD: "new"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm_host_key"
    assert config_entry.data[CONF_SSH_HOST_KEY] == HOST_KEY.public_key

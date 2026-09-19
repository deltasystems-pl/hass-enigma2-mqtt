"""Guided install and SSH credential lifecycle in the config flow."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import EVENT_DATA_ENTRY_FLOW_PROGRESSED, FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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

from .conftest import NODE_ID, async_setup_box

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

        unsubscribe = hass.bus.async_listen(
            EVENT_DATA_ENTRY_FLOW_PROGRESSED, progress_changed
        )
        try:
            flow._async_install_progress("done")
            await asyncio.wait_for(progressed.wait(), timeout=1)
            assert flow._install_phase == "done"
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

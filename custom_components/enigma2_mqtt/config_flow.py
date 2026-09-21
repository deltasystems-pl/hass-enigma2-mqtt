"""Config flow for the Enigma2 MQTT integration.

A box reaches Home Assistant in one of two ways. Either the plugin announces itself on
`enigma2mqtt/discovery/#` and Home Assistant offers it — that is `async_step_mqtt` — or
the user types the base topic and the node id, which is what a box behind an MQTT bridge
with a rewritten prefix needs. Both paths end the same way: the box is switched into
`integration` mode and the entry is only created once the box has acknowledged it.

Two more ways in exist once there are entities to configure. **Reconfigure** follows a
box whose node id or base topic was changed on its own setup screen — and a changed node
id is a changed identity, so it takes the old device with it rather than leaving a ghost
beside the new one. **Options** are the three preferences that are about this end of the
link rather than about the box: whether the two buttons that can end the evening appear,
which address a magic packet goes to, and which bouquets are worth browsing.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt import valid_subscribe_topic
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, selector
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
import voluptuous as vol

from .box import (
    Enigma2Box,
    async_request_ha_mode,
    async_wait_for_info,
    normalise_mac,
    parse_json_payload,
)
from .const import (
    ACK_TIMEOUT,
    CONF_BASE_TOPIC,
    CONF_BOUQUETS,
    CONF_CAM_TELEMETRY,
    CONF_CHECK_GITHUB_RELEASES,
    CONF_DANGEROUS_BUTTONS,
    CONF_KEEP_SSH_CREDENTIALS,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_OSCAM_TELEMETRY,
    CONF_PUBLISH_KEYS,
    CONF_RECEIVER_HOST,
    CONF_SCREENSHOT,
    CONF_SCREENSHOT_DELAY,
    CONF_SCREENSHOT_INTERVAL,
    CONF_SOURCE_LIST_SCOPE,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_WOL_MAC,
    DEFAULT_BASE_TOPIC,
    DEFAULT_CAM_TELEMETRY,
    DEFAULT_CHECK_GITHUB_RELEASES,
    DEFAULT_OSCAM_TELEMETRY,
    DEFAULT_PUBLISH_KEYS,
    DEFAULT_SCREENSHOT,
    DEFAULT_SCREENSHOT_DELAY,
    DEFAULT_SCREENSHOT_INTERVAL,
    DEFAULT_SOURCE_LIST_SCOPE,
    DISCOVERY_PREFIX,
    DOMAIN,
    HA_MODE_INTEGRATION,
    MAX_SCREENSHOT_DELAY,
    MAX_SCREENSHOT_INTERVAL,
    MIN_SCREENSHOT_DELAY,
    MIN_SCREENSHOT_INTERVAL,
    PROBE_TIMEOUT,
    SCREENSHOT_MODES,
    SOURCE_LIST_SCOPES,
)
from .installer import (
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    Provisioning,
    SshCredentials,
    async_install,
    async_preflight,
    async_probe_host_key,
)

_LOGGER = logging.getLogger(__name__)

# Shown in place of a field the box did not report.
UNKNOWN_PLACEHOLDER = "—"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): str,
        vol.Required(CONF_NODE_ID): str,
        vol.Optional(CONF_NAME): str,
        vol.Optional(CONF_RECEIVER_HOST): str,
    }
)


class Enigma2MqttConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add an Enigma2 receiver that publishes to the broker."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> Enigma2MqttOptionsFlow:
        """Return the options flow of a configured box."""
        return Enigma2MqttOptionsFlow()

    def __init__(self) -> None:
        """Start with the plugin's defaults; discovery fills in the rest."""
        self._node_id: str = ""
        self._base_topic: str = DEFAULT_BASE_TOPIC
        self._name: str = ""
        self._boxtype: str = ""
        self._image: str = ""
        self._reauth_input: dict[str, Any] | None = None
        self._reauth_host_key: str = ""
        self._install_host: str = ""
        self._install_port: int = 22
        self._install_host_key = None
        self._install_task = None
        self._install_request: InstallRequest | None = None
        self._install_error: str | None = None
        self._install_result = None
        self._install_phase = "preflight"

    async def async_step_mqtt(
        self, discovery_info: MqttServiceInfo
    ) -> ConfigFlowResult:
        """Handle a box announcing itself on the discovery prefix."""
        if not discovery_info.payload:
            # An empty retained payload is the plugin retracting its announcement,
            # which is what `ha_mode: off` and `cmd/reset` publish.
            return self.async_abort(reason="retracted")

        topic = discovery_info.topic.split("/")
        prefix = DISCOVERY_PREFIX.split("/")
        if len(topic) != len(prefix) + 2 or topic[:-2] != prefix or topic[-1] != "config":
            return self.async_abort(reason="not_enigma2_announcement")

        announcement = parse_json_payload(discovery_info.payload)
        if announcement is None:
            return self.async_abort(reason="not_enigma2_announcement")

        node_id = announcement.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            return self.async_abort(reason="not_enigma2_announcement")

        self._node_id = node_id
        self._base_topic = announcement.get("base_topic") or DEFAULT_BASE_TOPIC
        self._name = announcement.get("name") or node_id
        self._boxtype = announcement.get("boxtype") or ""
        self._image = announcement.get("image") or ""

        await self.async_set_unique_id(node_id)
        self._abort_if_unique_id_configured(
            updates={
                CONF_NODE_ID: self._node_id,
                CONF_BASE_TOPIC: self._base_topic,
                CONF_NAME: self._name,
            }
        )

        self.context["title_placeholders"] = self._placeholders()
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before taking a box over, then take it over."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            return self.async_abort(reason="mqtt_unavailable")

        errors: dict[str, str] = {}
        if user_input is not None:
            if await self._async_take_over():
                return self._async_create_entry()
            errors["base"] = "no_ack"

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=self._placeholders(),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between an existing plugin and a guided installation."""
        return self.async_show_menu(step_id="user", menu_options=["manual", "install"])

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a box by hand, for a prefix Home Assistant never saw announced."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            return self.async_abort(reason="mqtt_unavailable")

        errors: dict[str, str] = {}
        if user_input is not None:
            base_topic = user_input[CONF_BASE_TOPIC].strip().strip("/")
            node_id = user_input[CONF_NODE_ID].strip()
            name = (user_input.get(CONF_NAME) or "").strip()
            receiver_host = (user_input.get(CONF_RECEIVER_HOST) or "").strip()

            if not _is_valid_topic(base_topic, node_id):
                errors["base"] = "invalid_topic"
            elif not _is_valid_host(receiver_host):
                errors["base"] = "invalid_host"
            else:
                await self.async_set_unique_id(node_id, raise_on_progress=False)
                self._abort_if_unique_id_configured()

                self._node_id = node_id
                self._base_topic = base_topic
                self._name = name or node_id

                info = await async_wait_for_info(
                    self.hass, base_topic, node_id, PROBE_TIMEOUT
                )
                if info is None:
                    errors["base"] = "not_found"
                else:
                    self._boxtype = info.get("boxtype") or ""
                    self._image = info.get("image") or ""
                    if await self._async_take_over():
                        return self._async_create_entry(receiver_host=receiver_host)
                    errors["base"] = "no_ack"

        return self.async_show_form(
            step_id="manual",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_install(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Probe SSH identity before asking for any credential."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._install_host = user_input[CONF_SSH_HOST].strip()
            self._install_port = user_input[CONF_SSH_PORT]
            if not self._install_host:
                errors["base"] = "invalid_host"
                return self._show_install_host(errors)
            try:
                self._install_host_key = await async_probe_host_key(
                    self._install_host, self._install_port
                )
            except InstallerError as err:
                errors["base"] = err.code.value
            else:
                return await self.async_step_install_confirm()
        return self._show_install_host(errors)

    def _show_install_host(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Render the non-secret receiver address form."""
        return self.async_show_form(
            step_id="install",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SSH_HOST): str,
                    vol.Required(CONF_SSH_PORT, default=22): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65535)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_install_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the fingerprint, credentials, and provisioning in one explicit step."""
        assert self._install_host_key is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if not await mqtt.async_wait_for_mqtt_client(self.hass):
                return self.async_abort(reason="mqtt_unavailable")
            credentials = SshCredentials(
                host=self._install_host,
                port=self._install_port,
                username=user_input[CONF_SSH_USERNAME].strip(),
                password=user_input[CONF_SSH_PASSWORD],
                host_key=self._install_host_key.public_key,
            )
            node_id = user_input[CONF_NODE_ID].strip()
            base_topic = user_input[CONF_BASE_TOPIC].strip().strip("/")
            broker_host = user_input["broker_host"].strip()
            broker_username = user_input["broker_username"].strip()
            if (
                not _is_valid_topic(base_topic, node_id)
                or not credentials.username
                or not broker_host
                or not broker_username
            ):
                errors["base"] = "invalid_topic"
                return self._show_install_confirm(errors, user_input)
            provisioning = Provisioning(
                broker_host=broker_host,
                broker_port=user_input["broker_port"],
                broker_username=broker_username,
                broker_password=user_input["broker_password"],
                node_id=node_id,
                base_topic=base_topic,
                friendly_name=(user_input.get(CONF_NAME) or "").strip() or None,
            )
            self._install_request = InstallRequest(
                credentials=credentials,
                provisioning=provisioning,
                keep_credentials=user_input[CONF_KEEP_SSH_CREDENTIALS],
            )
            self._node_id = provisioning.node_id
            self._base_topic = provisioning.base_topic
            self._name = provisioning.friendly_name or provisioning.node_id
            await self.async_set_unique_id(self._node_id)
            self._abort_if_unique_id_configured()
            self._install_task = self.hass.async_create_task(self._async_run_install())
            return await self.async_step_install_progress()

        return self._show_install_confirm(errors, user_input)

    def _show_install_confirm(
        self, errors: dict[str, str], user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Render provisioning fields while never suggesting passwords."""
        password = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SSH_USERNAME, default="root"): str,
                vol.Required(CONF_SSH_PASSWORD): password,
                vol.Required("broker_host"): str,
                vol.Required("broker_port", default=1883): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required("broker_username"): str,
                vol.Required("broker_password"): password,
                vol.Required(CONF_NODE_ID): str,
                vol.Required(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): str,
                vol.Optional(CONF_NAME): str,
                vol.Required(CONF_KEEP_SSH_CREDENTIALS, default=False): bool,
            }
        )
        suggested = {
            key: value
            for key, value in (user_input or {}).items()
            if key not in {CONF_SSH_PASSWORD, "broker_password"}
        }
        return self.async_show_form(
            step_id="install_confirm",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={
                "fingerprint": self._install_host_key.fingerprint
            },
        )

    async def _async_run_install(self) -> None:
        assert self._install_request is not None
        try:
            self._install_result = await async_install(
                self.hass, self._install_request, self._async_install_progress
            )
        except asyncio.CancelledError:
            self._install_error = "install_cancelled"
            raise
        except InstallerError as err:
            self._install_error = err.code.value
        except Exception:
            # The flow only ever shows "unknown", which is all a user can act on, but a
            # bug report needs the traceback. The message carries no interpolation, so
            # no credential or provisioning value can reach the log through it.
            _LOGGER.exception("Unexpected failure while installing the receiver plugin")
            self._install_error = "unknown"

    def _async_install_progress(self, phase: str) -> None:
        """Remember the backend phase without recording any credential or address."""
        self._install_phase = phase
        phases = (
            "preflight", "backup", "upload", "install", "provision",
            "restart", "announcement", "done",
        )
        if phase in phases:
            self.async_update_progress((phases.index(phase) + 1) / len(phases))
        self.async_notify_flow_changed()

    @callback
    def async_remove(self) -> None:
        """Drop credentials when Home Assistant finishes or aborts this flow."""
        self._install_request = None
        self._reauth_input = None

    async def async_step_install_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep the flow alive while the guarded transaction runs."""
        assert self._install_task is not None
        if self._install_task.done():
            if not self._install_task.cancelled():
                self._install_task.result()
            return self.async_show_progress_done(next_step_id="install_finish")
        return self.async_show_progress(
            step_id="install_progress",
            progress_action=self._install_phase,
            progress_task=self._install_task,
        )

    async def async_step_install_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the MQTT entry only after the backend proved its announcement."""
        if self._install_error is not None or self._install_result is None:
            self._install_request = None
            return self.async_abort(reason=self._install_error or "unknown")
        assert self._install_request is not None
        data = {
            CONF_NODE_ID: self._node_id,
            CONF_BASE_TOPIC: self._base_topic,
            CONF_NAME: self._name,
            CONF_RECEIVER_HOST: self._install_host,
            CONF_SSH_HOST: self._install_host,
            CONF_SSH_PORT: self._install_port,
        }
        if self._install_request.keep_credentials:
            data.update(
                {
                    CONF_SSH_USERNAME: self._install_request.credentials.username,
                    CONF_SSH_PASSWORD: self._install_request.credentials.password,
                    CONF_SSH_HOST_KEY: self._install_request.credentials.host_key,
                    CONF_KEEP_SSH_CREDENTIALS: True,
                }
            )
        self._install_request = None
        return self.async_create_entry(title=self._name, data=data)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Follow a box whose topics moved.

        The node id and the base topic are settings on the receiver, and changing one
        of them there is what brings a user here. The base topic is only an address, so
        a change to it is invisible once the entry has been updated. The node id is not:
        it is half of every unique id, so a box that has been renamed is — as far as
        Home Assistant can tell — a different device doing the same job. The old device
        is removed rather than left behind unavailable, and the reload rebuilds every
        entity under the new identity.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        current = {
            CONF_BASE_TOPIC: entry.data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC),
            CONF_NODE_ID: entry.data[CONF_NODE_ID],
            CONF_NAME: entry.data.get(CONF_NAME) or entry.title,
            CONF_RECEIVER_HOST: entry.data.get(CONF_RECEIVER_HOST, ""),
        }

        if user_input is not None:
            base_topic = user_input[CONF_BASE_TOPIC].strip().strip("/")
            node_id = user_input[CONF_NODE_ID].strip()
            name = (user_input.get(CONF_NAME) or "").strip() or node_id
            receiver_host = (user_input.get(CONF_RECEIVER_HOST) or "").strip()

            if not _is_valid_topic(base_topic, node_id):
                errors["base"] = "invalid_topic"
            elif not _is_valid_host(receiver_host):
                errors["base"] = "invalid_host"
            elif any(
                other.unique_id == node_id and other.entry_id != entry.entry_id
                for other in self.hass.config_entries.async_entries(DOMAIN)
            ):
                errors["base"] = "already_configured"
            elif not await mqtt.async_wait_for_mqtt_client(self.hass):
                return self.async_abort(reason="mqtt_unavailable")
            elif (
                await async_wait_for_info(self.hass, base_topic, node_id, PROBE_TIMEOUT)
            ) is None:
                errors["base"] = "not_found"
            elif (
                await async_request_ha_mode(
                    self.hass,
                    base_topic,
                    node_id,
                    HA_MODE_INTEGRATION,
                    ACK_TIMEOUT,
                )
            ) is None:
                errors["base"] = "no_ack"
            else:
                if node_id != entry.data[CONF_NODE_ID]:
                    self._async_forget_device(entry.data[CONF_NODE_ID], entry.entry_id)
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=node_id,
                    title=name,
                    data_updates={
                        CONF_BASE_TOPIC: base_topic,
                        CONF_NODE_ID: node_id,
                        CONF_NAME: name,
                        CONF_RECEIVER_HOST: receiver_host,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or current
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Refresh optional SSH credentials without disturbing the MQTT runtime."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if (
            user_input is not None
            and CONF_SSH_PASSWORD in user_input
            and CONF_NODE_ID not in user_input
        ):
            host = entry.data.get(CONF_SSH_HOST, "")
            port = entry.data.get(CONF_SSH_PORT, 22)
            try:
                found = await async_probe_host_key(host, port)
            except InstallerError as err:
                errors["base"] = err.code.value
            else:
                self._reauth_input = dict(user_input)
                self._reauth_host_key = found.public_key
                if found.public_key != entry.data.get(CONF_SSH_HOST_KEY):
                    return self.async_show_form(
                        step_id="reauth_confirm_host_key",
                        data_schema=vol.Schema({}),
                        description_placeholders={"fingerprint": found.fingerprint},
                    )
                return await self._async_finish_reauth(entry, found.public_key)

        return self.async_show_form(
            step_id="reauth",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_SSH_USERNAME): str,
                        vol.Required(CONF_SSH_PASSWORD): selector.TextSelector(
                            selector.TextSelectorConfig(
                                type=selector.TextSelectorType.PASSWORD
                            )
                        ),
                    }
                ),
                user_input
                or {CONF_SSH_USERNAME: entry.data.get(CONF_SSH_USERNAME, "root")},
            ),
            errors=errors,
        )

    async def async_step_reauth_confirm_host_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Require an explicit click before replacing a changed SSH identity."""
        if user_input is None:
            return self.async_abort(reason="reauth_failed")
        return await self._async_finish_reauth(
            self._get_reauth_entry(), self._reauth_host_key
        )

    async def _async_finish_reauth(
        self, entry: ConfigEntry, host_key: str
    ) -> ConfigFlowResult:
        """Authenticate the replacement password, then update only SSH data."""
        assert self._reauth_input is not None
        credentials = SshCredentials(
            host=entry.data[CONF_SSH_HOST],
            port=entry.data.get(CONF_SSH_PORT, 22),
            username=self._reauth_input[CONF_SSH_USERNAME],
            password=self._reauth_input[CONF_SSH_PASSWORD],
            host_key=host_key,
        )
        try:
            await async_preflight(self.hass, credentials)
        except InstallerError as err:
            if err.code is InstallerErrorCode.HOST_KEY_CHANGED:
                return self.async_abort(reason="host_key_changed")
            return self.async_show_form(
                step_id="reauth",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_SSH_USERNAME): str,
                        vol.Required(CONF_SSH_PASSWORD): selector.TextSelector(
                            selector.TextSelectorConfig(
                                type=selector.TextSelectorType.PASSWORD
                            )
                        ),
                    }
                ),
                errors={"base": err.code.value},
            )
        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_SSH_USERNAME: credentials.username,
                CONF_SSH_PASSWORD: credentials.password,
                CONF_SSH_HOST_KEY: host_key,
            },
        )
        return self.async_abort(reason="reauth_successful")

    @callback
    def _async_forget_device(self, node_id: str, entry_id: str) -> None:
        """Remove the device a renamed box used to be, and its entities with it."""
        registry = dr.async_get(self.hass)
        if device := registry.async_get_device_by_identifier(
            (DOMAIN, node_id), entry_id
        ):
            registry.async_remove_device(device.id)

    async def _async_take_over(self) -> bool:
        """Switch the box into `integration` mode and wait for it to say so.

        The plugin retracts its own Home Assistant discovery payloads before it echoes
        the new mode, so the entities this integration is about to build cannot end up
        next to a duplicate set from the core MQTT integration.
        """
        return (
            await async_request_ha_mode(
                self.hass,
                self._base_topic,
                self._node_id,
                HA_MODE_INTEGRATION,
                ACK_TIMEOUT,
            )
            is not None
        )

    def _async_create_entry(self, *, receiver_host: str = "") -> ConfigFlowResult:
        """Create the config entry for the box that just acknowledged the take-over."""
        data = {
            CONF_NODE_ID: self._node_id,
            CONF_BASE_TOPIC: self._base_topic,
            CONF_NAME: self._name,
        }
        if receiver_host:
            data[CONF_RECEIVER_HOST] = receiver_host
        return self.async_create_entry(
            title=self._name,
            data=data,
        )

    def _placeholders(self) -> dict[str, str]:
        """Return what the confirm card and the flow title say about the box.

        A box that reports no type or no image is rare but possible, and „( , )" on
        the card would look like a bug rather than a gap in what the box said.
        """
        return {
            "name": self._name,
            "boxtype": self._boxtype or UNKNOWN_PLACEHOLDER,
            "image": self._image or UNKNOWN_PLACEHOLDER,
        }


class Enigma2MqttOptionsFlow(OptionsFlowWithReload):
    """Preferences for the entities and the privacy-sensitive plugin publishers."""

    def __init__(self) -> None:
        """Keep a pending local edit while SSH identity is confirmed."""
        self._pending_options: dict[str, Any] | None = None
        self._pending_remote: dict[str, Any] | None = None
        self._ssh_host = ""
        self._ssh_port = 22
        self._ssh_host_key = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options, and save them."""
        errors: dict[str, str] = {}
        if user_input is not None:
            box = self._box()
            current = box.info.get("settings") if box is not None else None
            requested = (
                {
                    CONF_PUBLISH_KEYS: user_input[CONF_PUBLISH_KEYS],
                    CONF_SCREENSHOT: user_input[CONF_SCREENSHOT],
                    CONF_SCREENSHOT_INTERVAL: user_input[CONF_SCREENSHOT_INTERVAL],
                }
                if isinstance(current, dict)
                else None
            )
            if requested is not None and CONF_SCREENSHOT_DELAY in current:
                requested[CONF_SCREENSHOT_DELAY] = user_input[CONF_SCREENSHOT_DELAY]
            if requested is not None and CONF_CAM_TELEMETRY in current:
                requested[CONF_CAM_TELEMETRY] = user_input[CONF_CAM_TELEMETRY]
            if requested is not None and CONF_OSCAM_TELEMETRY in current:
                requested[CONF_OSCAM_TELEMETRY] = user_input[CONF_OSCAM_TELEMETRY]
            # An empty field means "use the address the box reports", which is the
            # common case and not an error. Anything else has to be an address before
            # it is stored: the value used to be passed through with nothing but a
            # `.strip()`, and a malformed one surfaced from inside `wake_on_lan` as a
            # complaint about a non-hexadecimal character at position 12.
            typed_mac = (user_input.get(CONF_WOL_MAC) or "").strip()
            wol_mac = normalise_mac(typed_mac) if typed_mac else ""
            if wol_mac is None:
                errors[CONF_WOL_MAC] = "invalid_mac"
            local_data = {
                CONF_DANGEROUS_BUTTONS: user_input[CONF_DANGEROUS_BUTTONS],
                CONF_WOL_MAC: wol_mac or "",
                CONF_BOUQUETS: user_input.get(CONF_BOUQUETS) or [],
                CONF_CHECK_GITHUB_RELEASES: user_input[CONF_CHECK_GITHUB_RELEASES],
                CONF_SOURCE_LIST_SCOPE: user_input[CONF_SOURCE_LIST_SCOPE],
            }
            if requested is not None:
                local_data.update(requested)
            # Nothing is sent to the receiver and no other step is entered while a field
            # on this form is wrong: a user correcting the address would otherwise have
            # already changed the box's privacy settings, or be three steps into an SSH
            # flow, before seeing the message.
            if not errors and user_input.get("configure_ssh"):
                self._pending_options = local_data
                self._pending_remote = (
                    requested
                    if requested is not None and not _settings_match(current, requested)
                    else None
                )
                return await self.async_step_ssh_host()
            if not errors and requested is not None and not _settings_match(current, requested):
                before = box.updates.get("info", 0)
                try:
                    await box.async_command(
                        "config",
                        json.dumps(requested, separators=(",", ":")),
                        effect=lambda: (
                            box.updates.get("info", 0) > before
                            and _settings_match(box.info.get("settings"), requested)
                        ),
                    )
                except HomeAssistantError:
                    errors["base"] = "plugin_config_failed"
            if not errors:
                data = local_data
                if (
                    CONF_SSH_PASSWORD in self.config_entry.data
                    and not user_input.get(CONF_KEEP_SSH_CREDENTIALS, True)
                ):
                    entry_data = dict(self.config_entry.data)
                    for key in (
                        CONF_SSH_USERNAME,
                        CONF_SSH_PASSWORD,
                        CONF_SSH_HOST_KEY,
                        CONF_KEEP_SSH_CREDENTIALS,
                    ):
                        entry_data.pop(key, None)
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=entry_data
                    )
                return self.async_create_entry(data=data)

        box = self._box()
        # Every bouquet the box published, not the ones the current option narrowed it
        # to: nobody can pick from a list filtered by the choice they came here to
        # change. A box that has not published its channels yet leaves the list empty,
        # and the selector still takes typed names.
        bouquets = box.bouquet_names if box is not None else []
        mac = box.reported_mac if box is not None else None
        settings = box.info.get("settings") if box is not None else None

        schema = vol.Schema(
            {
                vol.Required(CONF_DANGEROUS_BUTTONS, default=False): bool,
                vol.Optional(CONF_WOL_MAC, default=""): str,
                vol.Optional(CONF_BOUQUETS, default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=bouquets,
                        multiple=True,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        sort=False,
                    )
                ),
                vol.Required(
                    CONF_CHECK_GITHUB_RELEASES,
                    default=DEFAULT_CHECK_GITHUB_RELEASES,
                ): bool,
                vol.Required(
                    CONF_SOURCE_LIST_SCOPE, default=DEFAULT_SOURCE_LIST_SCOPE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SOURCE_LIST_SCOPES),
                        translation_key=CONF_SOURCE_LIST_SCOPE,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        sort=False,
                    )
                ),
            }
        )
        if isinstance(settings, dict):
            schema = schema.extend(
                {
                    vol.Required(
                        CONF_PUBLISH_KEYS,
                        default=settings.get(CONF_PUBLISH_KEYS, DEFAULT_PUBLISH_KEYS),
                    ): bool,
                    vol.Required(
                        CONF_SCREENSHOT,
                        default=settings.get(CONF_SCREENSHOT, DEFAULT_SCREENSHOT),
                    ): vol.In(SCREENSHOT_MODES),
                    vol.Required(
                        CONF_SCREENSHOT_INTERVAL,
                        default=settings.get(
                            CONF_SCREENSHOT_INTERVAL, DEFAULT_SCREENSHOT_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SCREENSHOT_INTERVAL,
                            max=MAX_SCREENSHOT_INTERVAL,
                        ),
                    ),
                }
            )
            if CONF_SCREENSHOT_DELAY in settings:
                schema = schema.extend(
                    {
                        vol.Required(
                            CONF_SCREENSHOT_DELAY,
                            default=settings.get(
                                CONF_SCREENSHOT_DELAY, DEFAULT_SCREENSHOT_DELAY
                            ),
                        ): vol.All(
                            vol.Coerce(int),
                            vol.Range(
                                min=MIN_SCREENSHOT_DELAY,
                                max=MAX_SCREENSHOT_DELAY,
                            ),
                        )
                    }
                )
            if CONF_CAM_TELEMETRY in settings:
                schema = schema.extend(
                    {
                        vol.Required(
                            CONF_CAM_TELEMETRY,
                            default=settings.get(
                                CONF_CAM_TELEMETRY, DEFAULT_CAM_TELEMETRY
                            ),
                        ): bool
                    }
                )
            if CONF_OSCAM_TELEMETRY in settings:
                schema = schema.extend(
                    {
                        vol.Required(
                            CONF_OSCAM_TELEMETRY,
                            default=settings.get(
                                CONF_OSCAM_TELEMETRY, DEFAULT_OSCAM_TELEMETRY
                            ),
                        ): bool
                    }
                )
        if CONF_SSH_PASSWORD in self.config_entry.data:
            schema = schema.extend(
                {vol.Required(CONF_KEEP_SSH_CREDENTIALS, default=True): bool}
            )
        else:
            schema = schema.extend(
                {vol.Optional("configure_ssh", default=False): bool}
            )

        suggested = dict(self.config_entry.options)
        if isinstance(settings, dict):
            suggested.update(settings)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or suggested
            ),
            errors=errors,
            description_placeholders={"mac": mac or UNKNOWN_PLACEHOLDER},
        )

    async def async_step_ssh_host(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Probe an existing receiver's SSH identity without a password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._ssh_host = user_input[CONF_SSH_HOST].strip()
            self._ssh_port = user_input[CONF_SSH_PORT]
            try:
                self._ssh_host_key = await async_probe_host_key(
                    self._ssh_host, self._ssh_port
                )
            except InstallerError as err:
                errors["base"] = err.code.value
            else:
                return await self.async_step_ssh_confirm()
        return self.async_show_form(
            step_id="ssh_host",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_SSH_HOST): str,
                        vol.Required(CONF_SSH_PORT, default=22): vol.All(
                            vol.Coerce(int), vol.Range(min=1, max=65535)
                        ),
                    }
                ),
                {CONF_SSH_HOST: self.config_entry.data.get(CONF_RECEIVER_HOST, "")},
            ),
            errors=errors,
        )

    async def async_step_ssh_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the fingerprint and verify credentials before retaining them."""
        assert self._ssh_host_key is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = SshCredentials(
                host=self._ssh_host,
                port=self._ssh_port,
                username=user_input[CONF_SSH_USERNAME],
                password=user_input[CONF_SSH_PASSWORD],
                host_key=self._ssh_host_key.public_key,
            )
            try:
                await async_preflight(self.hass, credentials)
            except InstallerError as err:
                errors["base"] = err.code.value
            else:
                if self._pending_remote is not None:
                    box = self._box()
                    if box is None:
                        errors["base"] = "plugin_unavailable"
                    else:
                        before = box.updates.get("info", 0)
                        try:
                            await box.async_command(
                                "config",
                                json.dumps(self._pending_remote, separators=(",", ":")),
                                effect=lambda: (
                                    box.updates.get("info", 0) > before
                                    and _settings_match(
                                        box.info.get("settings"), self._pending_remote
                                    )
                                ),
                            )
                        except HomeAssistantError:
                            errors["base"] = "plugin_config_failed"
                if errors:
                    return self._show_ssh_confirm(errors)
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        CONF_SSH_HOST: credentials.host,
                        CONF_SSH_PORT: credentials.port,
                        CONF_SSH_USERNAME: credentials.username,
                        CONF_SSH_PASSWORD: credentials.password,
                        CONF_SSH_HOST_KEY: credentials.host_key,
                        CONF_KEEP_SSH_CREDENTIALS: True,
                    },
                )
                return self.async_create_entry(data=self._pending_options or {})
        return self._show_ssh_confirm(errors)

    def _show_ssh_confirm(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Render the credential form without ever echoing a password."""
        password = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        return self.async_show_form(
            step_id="ssh_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SSH_USERNAME, default="root"): str,
                    vol.Required(CONF_SSH_PASSWORD): password,
                }
            ),
            errors=errors,
            description_placeholders={"fingerprint": self._ssh_host_key.fingerprint},
        )

    def _box(self) -> Enigma2Box | None:
        """Return the running box, when the entry is loaded.

        The options can be opened on an entry that failed to set up — no broker, no box
        — and that is exactly when somebody wants to change the address a magic packet
        goes to. So this answers None rather than raising, and the form degrades to
        typed values.
        """
        return getattr(self.config_entry, "runtime_data", None)


def _is_valid_topic(base_topic: str, node_id: str) -> bool:
    """Return whether the two halves make a topic that can be subscribed to."""
    if not base_topic or not node_id:
        return False
    try:
        valid_subscribe_topic(f"{base_topic}/{node_id}/info")
    except vol.Invalid:
        return False
    return "+" not in base_topic and "#" not in base_topic and "+" not in node_id


def _settings_match(actual: Any, requested: dict[str, Any]) -> bool:
    """Match only settings sent by this integration.

    New plugin versions can advertise additional settings while an older
    integration is still loaded. Those additions must not invalidate a fresh
    acknowledgement for the subset this flow changed.
    """
    return isinstance(actual, dict) and all(
        actual.get(key) == value for key, value in requested.items()
    )


def _is_valid_host(host: str) -> bool:
    """Accept an optional hostname or IP address, never a URL or credential."""
    if not host:
        return True
    if "%" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(
            len(host) <= 253
            and re.fullmatch(
                r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                host,
            )
        )

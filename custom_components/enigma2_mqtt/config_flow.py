"""Config flow for the Enigma2 MQTT integration.

A box reaches Home Assistant in one of two ways. Either the plugin announces itself on
`enigma2mqtt/discovery/#` and Home Assistant offers it — that is `async_step_mqtt` — or
the user types the base topic and the node id, which is what a box behind an MQTT bridge
with a rewritten prefix needs. Both paths end the same way: the box is switched into
`integration` mode and the entry is only created once the box has acknowledged it.

Options and reconfigure arrive in M3, with the settings there is something to configure.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.mqtt import valid_subscribe_topic
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
import voluptuous as vol

from .box import async_request_ha_mode, async_wait_for_info, parse_json_payload
from .const import (
    ACK_TIMEOUT,
    CONF_BASE_TOPIC,
    CONF_NAME,
    CONF_NODE_ID,
    DEFAULT_BASE_TOPIC,
    DISCOVERY_PREFIX,
    DOMAIN,
    HA_MODE_INTEGRATION,
    PROBE_TIMEOUT,
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): str,
        vol.Required(CONF_NODE_ID): str,
        vol.Optional(CONF_NAME): str,
    }
)


class Enigma2MqttConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add an Enigma2 receiver that publishes to the broker."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with the plugin's defaults; discovery fills in the rest."""
        self._node_id: str = ""
        self._base_topic: str = DEFAULT_BASE_TOPIC
        self._name: str = ""
        self._boxtype: str = ""
        self._image: str = ""

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
        """Add a box by hand, for a prefix Home Assistant never saw announced."""
        errors: dict[str, str] = {}
        if user_input is not None:
            base_topic = user_input[CONF_BASE_TOPIC].strip().strip("/")
            node_id = user_input[CONF_NODE_ID].strip()
            name = (user_input.get(CONF_NAME) or "").strip()

            if not _is_valid_topic(base_topic, node_id):
                errors["base"] = "invalid_topic"
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
                        return self._async_create_entry()
                    errors["base"] = "no_ack"

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

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

    def _async_create_entry(self) -> ConfigFlowResult:
        """Create the config entry for the box that just acknowledged the take-over."""
        return self.async_create_entry(
            title=self._name,
            data={
                CONF_NODE_ID: self._node_id,
                CONF_BASE_TOPIC: self._base_topic,
                CONF_NAME: self._name,
            },
        )

    def _placeholders(self) -> dict[str, str]:
        """Return what the confirm card and the flow title say about the box."""
        return {
            "name": self._name,
            "boxtype": self._boxtype,
            "image": self._image,
        }


def _is_valid_topic(base_topic: str, node_id: str) -> bool:
    """Return whether the two halves make a topic that can be subscribed to."""
    if not base_topic or not node_id:
        return False
    try:
        valid_subscribe_topic(f"{base_topic}/{node_id}/info")
    except vol.Invalid:
        return False
    return "+" not in base_topic and "#" not in base_topic and "+" not in node_id

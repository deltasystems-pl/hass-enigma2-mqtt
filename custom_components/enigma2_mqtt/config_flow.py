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
from homeassistant.helpers import device_registry as dr, selector
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
import voluptuous as vol

from .box import (
    Enigma2Box,
    async_request_ha_mode,
    async_wait_for_info,
    parse_json_payload,
)
from .const import (
    ACK_TIMEOUT,
    CONF_BASE_TOPIC,
    CONF_BOUQUETS,
    CONF_DANGEROUS_BUTTONS,
    CONF_NAME,
    CONF_NODE_ID,
    CONF_WOL_MAC,
    DEFAULT_BASE_TOPIC,
    DISCOVERY_PREFIX,
    DOMAIN,
    HA_MODE_INTEGRATION,
    PROBE_TIMEOUT,
)

# Shown in place of a field the box did not report.
UNKNOWN_PLACEHOLDER = "—"

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
        """Add a box by hand, for a prefix Home Assistant never saw announced."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            return self.async_abort(reason="mqtt_unavailable")

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
        }

        if user_input is not None:
            base_topic = user_input[CONF_BASE_TOPIC].strip().strip("/")
            node_id = user_input[CONF_NODE_ID].strip()
            name = (user_input.get(CONF_NAME) or "").strip() or node_id

            if not _is_valid_topic(base_topic, node_id):
                errors["base"] = "invalid_topic"
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
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or current
            ),
            errors=errors,
        )

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
    """The three preferences a receiver has on this side of the link."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options, and save them."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_DANGEROUS_BUTTONS: user_input[CONF_DANGEROUS_BUTTONS],
                    CONF_WOL_MAC: (user_input.get(CONF_WOL_MAC) or "").strip(),
                    CONF_BOUQUETS: user_input.get(CONF_BOUQUETS) or [],
                }
            )

        box = self._box()
        # Every bouquet the box published, not the ones the current option narrowed it
        # to: nobody can pick from a list filtered by the choice they came here to
        # change. A box that has not published its channels yet leaves the list empty,
        # and the selector still takes typed names.
        bouquets = box.bouquet_names if box is not None else []
        mac = box.info.get("mac") if box is not None else None

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
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, dict(self.config_entry.options)
            ),
            description_placeholders={"mac": mac or UNKNOWN_PLACEHOLDER},
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

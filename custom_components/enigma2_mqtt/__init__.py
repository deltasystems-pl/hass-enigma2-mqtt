"""The Enigma2 MQTT integration.

Consumes the topics published by the `enigma2-mqtt-bridge` plugin that runs inside
enigma2 on the receiver, and turns them into a Home Assistant device.

The integration takes a box over: on setup it switches the plugin into `integration`
mode, which makes the plugin retract the MQTT discovery payloads it would otherwise
publish. From then on the entities below are the only ones the box has, which is why
this list is long — there is no core MQTT integration filling in the gaps.
"""

from __future__ import annotations

import logging

from homeassistant.components import mqtt
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .box import Enigma2Box, Enigma2MqttConfigEntry, command_topic, normalise_mac
from .const import (
    CONF_BASE_TOPIC,
    CONF_NODE_ID,
    CONF_WOL_MAC,
    DEFAULT_BASE_TOPIC,
    DOMAIN,
    HA_MODE_DISCOVERY,
)
from .release_store import async_release_check_store

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    # The media player first: it is the platform the actions are registered on, and
    # the one a user opens the device page to find.
    Platform.MEDIA_PLAYER,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.NOTIFY,
    Platform.NUMBER,
    Platform.REMOTE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration.

    There is nothing to configure in YAML: boxes are added through the config flow.
    """
    return True


@callback
def _async_repair_wol_mac(hass: HomeAssistant, entry: Enigma2MqttConfigEntry) -> None:
    """Drop a stored Wake-on-LAN override that is not an address.

    The option used to be stored with nothing but a `.strip()`, so an entry written
    before this release can hold anything somebody typed. Leaving it there means the
    box's own address is never used and the button fails from inside `wake_on_lan` with
    a message about hexadecimal characters; dropping it means the packet goes where the
    receiver says it should, which is what an empty field has always meant.

    A log line rather than a repair issue: the value is already fixed by the time
    anybody could act on a card, and the operator asked to be told, not asked.
    """
    stored = entry.options.get(CONF_WOL_MAC)
    if not isinstance(stored, str) or not stored.strip():
        return
    if (normalised := normalise_mac(stored)) is not None:
        if normalised == stored:
            return
        _LOGGER.debug(
            "Normalising the Wake-on-LAN address of %s", entry.data.get(CONF_NODE_ID)
        )
    else:
        _LOGGER.warning(
            "The Wake-on-LAN address stored for %s is not a MAC address and has been "
            "removed; magic packets now go to the address the receiver reports. Set a "
            "new one in the integration's options if the receiver's own interface is "
            "not the one to wake",
            entry.data.get(CONF_NODE_ID),
        )
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_WOL_MAC: normalised or ""}
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> bool:
    """Set up one box from a config entry."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="mqtt_not_available"
        )

    _async_repair_wol_mac(hass, entry)

    box = Enigma2Box(hass, entry)
    entry.runtime_data = box
    box.async_register_device()
    await box.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> bool:
    """Unload a box, releasing its subscriptions."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry.runtime_data.async_stop()
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> None:
    """Hand a removed box back to MQTT discovery.

    Adding a box switches its plugin into `integration` mode, which makes the plugin
    retract the discovery payloads the core MQTT integration builds entities from.
    Removing the entry without undoing that would leave the receiver publishing state
    that nothing listens to, and the user with no entities and no obvious reason why.
    This is best effort on purpose: the box may be off, the broker may be gone, and
    neither is a reason to refuse to remove a config entry.

    The release check's stored stamp goes with it. It is keyed by entry id, and Home
    Assistant does not reuse one, so a record left behind is a row nothing will ever
    read again — and, if the same receiver is added back, a stamp from its previous life
    deciding whether its new one may ask a question.
    """
    await async_release_check_store(hass).async_remove(entry.entry_id)
    try:
        if not await mqtt.async_wait_for_mqtt_client(hass):
            _LOGGER.debug(
                "MQTT is unavailable, leaving %s in integration mode",
                entry.data[CONF_NODE_ID],
            )
            return
        await mqtt.async_publish(
            hass,
            command_topic(
                entry.data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC),
                entry.data[CONF_NODE_ID],
                "ha_mode",
            ),
            HA_MODE_DISCOVERY,
            qos=1,
            retain=False,
        )
    except (HomeAssistantError, KeyError):
        _LOGGER.warning(
            "Could not switch %s back to discovery mode; do it on the receiver's "
            "setup screen if you want its MQTT discovery entities back",
            entry.data.get(CONF_NODE_ID),
        )

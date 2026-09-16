"""The colour keys, as device triggers.

„When the yellow button on the receiver's remote is held" is a thing a person builds in
the automation editor, not a thing they should have to write an event trigger for. The
four colour keys are the ones an Enigma2 skin puts a labelled function on, so those
times short and long are the eight triggers offered here.

They listen to the `enigma2_mqtt_key` bus event, which the box fires for every key it
sees — including when the „Pilot – klawisz" event entity is disabled, which is why that
event exists separately from the entity at all. Every other key is still reachable
through the event entity or a plain event trigger; these eight are the ones worth a
menu item.
"""

from __future__ import annotations

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    ATTR_DEVICE_ID,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import ATTR_KEY, ATTR_PRESS, COLOUR_KEYS, DOMAIN, EVENT_KEY, PRESSES

# `<colour>_<press>`: red_short, red_long, green_short, … eight in all.
TRIGGER_TYPES: dict[str, tuple[str, str]] = {
    f"{colour}_{press}": (key, press)
    for colour, key in COLOUR_KEYS.items()
    for press in PRESSES
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate one stored trigger."""
    return TRIGGER_SCHEMA(config)  # type: ignore[no-any-return]


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the triggers a receiver offers."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None or not any(
        identifier[0] == DOMAIN for identifier in device.identifiers
    ):
        return []

    return [
        {
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_PLATFORM: "device",
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach one trigger to the key event of one receiver."""
    key, press = TRIGGER_TYPES[config[CONF_TYPE]]
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_KEY,
            event_trigger.CONF_EVENT_DATA: {
                ATTR_DEVICE_ID: config[CONF_DEVICE_ID],
                ATTR_KEY: key,
                ATTR_PRESS: press,
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )

"""The colour keys and the answers to a list of choices, as device triggers.

„When the yellow button on the receiver's remote is held" is a thing a person builds in
the automation editor, not a thing they should have to write an event trigger for. The
four colour keys are the ones an Enigma2 skin puts a labelled function on, so those
times short and long are the eight triggers offered here.

They listen to the `enigma2_mqtt_key` bus event, which the box fires for every key it
sees - including when the `key` event entity is disabled, which is why that
event exists separately from the entity at all. Every other key is still reachable
through the event entity or a plain event trigger; these eight are the ones worth a
menu item.

Three more follow a list of choices shown with `enigma2_mqtt.show_list`: a row was
picked - any row, or the one whose id the trigger names - the list timed out, and the
viewer dismissed it. They listen to `enigma2_mqtt_list_answer`, which is fired for every
readable answer, also to a list somebody else's automation or another client sent: the
trigger is about the receiver, not about who asked. A list that was replaced by a newer one
or closed by standby has no trigger - nobody chose anything - and is still on the event.
"""

from __future__ import annotations

from typing import Any

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
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .choice_list import ATTR_ENDING, ATTR_ROW, ROW_ID
from .const import (
    ATTR_KEY,
    ATTR_PRESS,
    COLOUR_KEYS,
    DOMAIN,
    EVENT_KEY,
    EVENT_LIST_ANSWER,
    LIST_DISMISSED,
    LIST_PICKED,
    LIST_TIMEOUT,
    PRESSES,
)

# `<colour>_<press>`: red_short, red_long, green_short, ... eight in all.
TRIGGER_TYPES: dict[str, tuple[str, str]] = {
    f"{colour}_{press}": (key, press)
    for colour, key in COLOUR_KEYS.items()
    for press in PRESSES
}

# The answers to a list, by the ending each one listens for.
TRIGGER_LIST_PICKED = "list_picked"
TRIGGER_LIST_TIMEOUT = "list_timeout"
TRIGGER_LIST_DISMISSED = "list_dismissed"
LIST_TRIGGER_TYPES: dict[str, str] = {
    TRIGGER_LIST_PICKED: LIST_PICKED,
    TRIGGER_LIST_TIMEOUT: LIST_TIMEOUT,
    TRIGGER_LIST_DISMISSED: LIST_DISMISSED,
}

# The one extra field: the id of the row a "picked" trigger is for. Left out, any row.
CONF_ROW = "row"


def _row_id(value: Any) -> str:
    """A row id of the contract's shape, so a trigger cannot wait for a row that cannot be.

    An empty one is the editor's field left blank, and means any row.
    """
    text = cv.string(value).strip()
    if text and not ROW_ID.fullmatch(text):
        raise vol.Invalid("a row id is 1 to 32 letters, digits or the characters . _ : -")
    return text


def _row_only_when_picked(config: ConfigType) -> ConfigType:
    """Only a "picked" trigger names a row; a timeout or a dismissal has none to name."""
    if CONF_ROW in config and not config[CONF_ROW]:
        config = {name: value for name, value in config.items() if name != CONF_ROW}
    if CONF_ROW in config and config[CONF_TYPE] != TRIGGER_LIST_PICKED:
        raise vol.Invalid("only a list_picked trigger takes a row")
    return config


TRIGGER_SCHEMA = vol.All(
    DEVICE_TRIGGER_BASE_SCHEMA.extend(
        {
            vol.Required(CONF_TYPE): vol.In([*TRIGGER_TYPES, *LIST_TRIGGER_TYPES]),
            vol.Optional(CONF_ROW): _row_id,
        }
    ),
    _row_only_when_picked,
)


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate one stored trigger."""
    return TRIGGER_SCHEMA(config)  # type: ignore[no-any-return]


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the triggers a receiver offers.

    The list triggers are offered on every receiver, as the colour keys are: whether one
    claims `list` can change with an update of its plugin, and an automation built for it
    should not disappear from the editor while it is between two versions.
    """
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
        for trigger_type in (*TRIGGER_TYPES, *LIST_TRIGGER_TYPES)
    ]


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """The "picked" trigger can name one row; nothing else has a field."""
    if config.get(CONF_TYPE) != TRIGGER_LIST_PICKED:
        return {}
    return {"extra_fields": vol.Schema({vol.Optional(CONF_ROW): cv.string})}


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach one trigger to the key event, or the list answer event, of one receiver."""
    if (ending := LIST_TRIGGER_TYPES.get(config[CONF_TYPE])) is not None:
        event_type = EVENT_LIST_ANSWER
        event_data: dict[str, Any] = {
            ATTR_DEVICE_ID: config[CONF_DEVICE_ID],
            ATTR_ENDING: ending,
        }
        if CONF_ROW in config:
            event_data[ATTR_ROW] = config[CONF_ROW]
    else:
        key, press = TRIGGER_TYPES[config[CONF_TYPE]]
        event_type = EVENT_KEY
        event_data = {
            ATTR_DEVICE_ID: config[CONF_DEVICE_ID],
            ATTR_KEY: key,
            ATTR_PRESS: press,
        }
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: event_type,
            event_trigger.CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )

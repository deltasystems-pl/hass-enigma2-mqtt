"""Diagnostics for the Enigma2 MQTT integration.

The download is what a bug report should carry, so it holds everything the integration
believes about a box — and nothing that would embarrass the person attaching it to a
public issue. The MAC and the IP address are redacted because they identify a household,
not because they are secret; the credential keys are listed before any credential
exists, so that the installer (M4) cannot add one to a file that is already being shared.

The `node_id` is kept deliberately, although its second half is the last six digits of
the same MAC address. It is the key every topic in a report is named after, and a
diagnostics file in which the topics cannot be matched to the box would answer nothing.
Six hex digits of a MAC are not the MAC, and they identify a receiver model far more
than a household.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .box import Enigma2MqttConfigEntry
from .const import DOMAIN

TO_REDACT = {
    "broker_password",
    # The configuration URL is built from the box's address, so redacting `ip` alone
    # would only move the address one key to the right.
    "configuration_url",
    "ip",
    "mac",
    "password",
    "ssh_password",
    "username",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one box."""
    box = entry.runtime_data
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, box.node_id), entry.entry_id
    )

    data: dict[str, Any] = {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "source": entry.source,
            "unique_id": entry.unique_id,
            "version": entry.version,
        },
        "box": {
            "node_id": box.node_id,
            "base_topic": box.base_topic,
            "available": box.available,
            "capabilities": box.capabilities,
        },
        "announcement": dict(box.announcement),
        "info": dict(box.info),
        "device": None
        if device is None
        else {
            "name": device.name,
            "name_by_user": device.name_by_user,
            "manufacturer": device.manufacturer,
            "model": device.model,
            "sw_version": device.sw_version,
            "configuration_url": device.configuration_url,
            "disabled_by": None if device.disabled_by is None else str(device.disabled_by),
        },
    }
    return async_redact_data(data, TO_REDACT)

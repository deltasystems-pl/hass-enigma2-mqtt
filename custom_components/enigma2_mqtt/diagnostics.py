"""Diagnostics for the Enigma2 MQTT integration.

The download is what a bug report should carry, so it holds everything the integration
believes about a box - and nothing that would embarrass the person attaching it to a
public issue. The MAC and the IP address are redacted because they identify a household,
not because they are secret; the credential keys are listed before any credential
exists, so that the installer (M4) cannot add one to a file that is already being shared.

The `node_id` is kept deliberately, although its second half is the last six digits of
the same MAC address. It is the key every topic in a report is named after, and a
diagnostics file in which the topics cannot be matched to the box would answer nothing.
Six hex digits of a MAC are not the MAC, and they identify a receiver model far more
than a household.

Two topics are summarised rather than included. `screen` is a JPEG of what is on the
television: the size and the time it was taken answer every question a bug report asks
of it, and the picture itself answers none of them. `key` is what somebody pressed on
the remote a moment ago - it is not state, it has no lasting value here, and a log of
household behaviour is not something to attach to a public issue by accident.

`zap_history` is summarised too, as a count and the three scalar fields, with no channel
name and no reference in it - filtered or not. It is the channels somebody watched, in
the order they watched them, and the hidden-bouquet option exists because some of them
are nobody else's business.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .box import Enigma2MqttConfigEntry
from .bundle import BundleError, load_bundled_plugin
from .const import DOMAIN, SUPPORTED_PLUGIN_VERSION
from .release_store import async_release_index_cache
from .update import plugin_compatibility

TO_REDACT = {
    "broker_host",
    "broker_password",
    "broker_username",
    # The configuration URL is built from the box's address, so redacting `ip` alone
    # would only move the address one key to the right.
    "configuration_url",
    "ip",
    "mac",
    "password",
    "receiver_host",
    "ssh_password",
    "ssh_host",
    "ssh_host_key",
    "ssh_username",
    "username",
    # The address a magic packet goes to is a hardware address of the same household as
    # the one `mac` is redacted for, and it is redacted for the same reason. It arrives
    # from a different direction - the user types it into the options rather than the
    # box announcing it - which is exactly how it was missed.
    "wol_mac",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one box."""
    box = entry.runtime_data
    state = box.state
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, box.node_id), entry.entry_id
    )
    try:
        bundled_version: str | None = (
            await hass.async_add_executor_job(load_bundled_plugin)
        ).version
    except (BundleError, OSError):
        # A distribution whose bundle will not load is itself worth reporting, and it is
        # the state the update entity falls back on, so the constant is what it compares.
        bundled_version = None

    installed_plugin = state.info.get("plugin")
    cache = async_release_index_cache(hass)
    await cache.async_load()
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
            "topics_seen": sorted(box.seen),
            "topic_updates": dict(sorted(box.updates.items())),
        },
        "announcement": dict(state.announcement),
        "info": dict(state.info),
        # A receiver ahead of this integration reads "up to date" on its card, because
        # offering it a downgrade would be worse. That makes the one version mismatch
        # nobody can see from the UI the one a bug report most needs stated.
        "plugin": {
            "installed": installed_plugin if isinstance(installed_plugin, str) else None,
            "bundled": bundled_version,
            "expected": SUPPORTED_PLUGIN_VERSION,
            "compatibility": plugin_compatibility(
                installed_plugin if isinstance(installed_plugin, str) else None,
                bundled_version or SUPPORTED_PLUGIN_VERSION,
            ),
        },
        # The signed release index as this Home Assistant knows it: which one, from which
        # key, and what went wrong with the last check. The index itself is public and
        # signed; its serial and key are what a report about an unexpected index needs.
        "release_index": {
            "serial": cache.serial,
            "key_id": cache.key_id,
            "issued": cache.issued,
            "floor": cache.index["floor"] if cache.index else None,
            "versions": [item["version"] for item in cache.index["releases"]]
            if cache.index
            else [],
            "last_check": cache.checked.isoformat() if cache.checked else None,
            "check_error": cache.check_error,
        },
        "topics": {
            "power": state.power,
            "service": state.service,
            "epg": state.epg,
            "tuner": state.tuner,
            "recording": state.recording,
            "timers": state.timers,
            "volume": state.volume,
            "hdd": state.hdd,
            "bouquet": state.bouquet,
            "cam": state.cam,
            "oscam": state.oscam,
            "process": state.process,
            # The normalised snapshot, not the payload that arrived: the plugin's own
            # auto-heal detector reads a file carrying a card-sharing account and the
            # live control words, and the shape written here can only ever hold the seven
            # fields the contract names.
            "softcam": state.softcam,
            "epg_import": state.epg_import,
            "channels": _summarise_channels(state.channels),
            "zap_history": _summarise_zap_history(state.zap_history),
            "last_error": state.last_error,
            "screen": {
                "bytes": len(state.screen) if state.screen else 0,
                "updated": None
                if state.screen_updated is None
                else state.screen_updated.isoformat(),
            },
            "epg_grid": {
                slug: {
                    "bouquet": grid.get("bouquet"),
                    "generated": grid.get("generated"),
                    "channels": len(grid.get("channels") or []),
                }
                for slug, grid in sorted(state.epg_grid.items())
            },
        },
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


def _summarise_channels(channels: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the channel list as its shape rather than its contents.

    A full channel list is a few hundred entries and says nothing a bug report needs:
    what matters is which bouquets exist, how many channels each has, and when the box
    last built it. It is also, in aggregate, a description of a household's television
    subscription.
    """
    if not channels:
        return None
    bouquets = channels.get("bouquets")
    return {
        "generated": channels.get("generated"),
        "bouquets": [
            {
                "name": bouquet.get("name"),
                "channels": len(bouquet.get("channels") or []),
            }
            for bouquet in bouquets
            if isinstance(bouquet, dict)
        ]
        if isinstance(bouquets, list)
        else None,
    }


def _summarise_zap_history(history: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the zap history as its shape: how many entries, and nothing they name."""
    if history is None:
        return None
    return {
        "entries": len(history.get("entries") or []),
        "current": history.get("current"),
        "limit": history.get("limit"),
        "panic_button": history.get("panic_button"),
    }

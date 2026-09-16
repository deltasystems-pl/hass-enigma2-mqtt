"""Whether the plugin on the box matches the one this integration was written for.

The two halves of this product are released together and the topic contract is
versioned with them, so a box running an older plugin than the integration expects is
the first thing to check when something is missing. This entity is that check, on the
device page, without anybody having to read a log.

It cannot install anything yet. The installer — SSH, the bundled IPK, a guarded GUI
restart — is M4; until then `latest_version` is a constant in `const.py` and the entity
points at the plugin's releases page, which is where a person updates it by hand.

A box running a **newer** plugin than this integration knows about is reported as up to
date rather than as needing a downgrade. The constant is what this code was written
against, not what exists.
"""

from __future__ import annotations

from homeassistant.components.update import UpdateEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import PLUGIN_RELEASES_URL, SUPPORTED_PLUGIN_VERSION, TOPIC_INFO
from .entity import Enigma2Entity

PARALLEL_UPDATES = 0


def _parts(version: str) -> tuple[int, ...]:
    """Return a dotted version as numbers, or an empty tuple if it is not one."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def latest_version(installed: str | None, supported: str) -> str:
    """Return the version to offer, given what is on the box.

    Whichever of the two is higher: a box that has run ahead of this integration is not
    out of date, and telling a user to install an older plugin over a newer one would
    be the one thing worse than saying nothing.
    """
    if not installed:
        return supported
    installed_parts, supported_parts = _parts(installed), _parts(supported)
    if not installed_parts or not supported_parts:
        return supported
    return installed if installed_parts > supported_parts else supported


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the plugin version entity."""
    async_add_entities([Enigma2PluginUpdate(entry.runtime_data)])


class Enigma2PluginUpdate(Enigma2Entity, UpdateEntity):
    """The MQTT Bridge plugin's version, against the one this release expects."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_release_url = PLUGIN_RELEASES_URL
    _attr_title = "Enigma2 MQTT Bridge"

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the update entity."""
        super().__init__(box, "plugin", topics=(TOPIC_INFO,))

    @callback
    def _async_read_state(self) -> None:
        """Read the plugin version the box reports on `info`."""
        installed = self.box.info.get("plugin")
        self._attr_installed_version = installed if isinstance(installed, str) else None
        self._attr_latest_version = latest_version(
            self._attr_installed_version, SUPPORTED_PLUGIN_VERSION
        )

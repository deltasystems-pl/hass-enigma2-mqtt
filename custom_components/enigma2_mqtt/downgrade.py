"""What an older plugin version would take away, and which older versions may be installed.

The options flow's „Zainstaluj starszą wersję wtyczki" is the only way back to an older plugin
(ADR-0008; never over MQTT, never one button press). It is SSH only, and it names what goes
before anybody ticks the box: the entities whose capability the older plugin predates stay in
Home Assistant - they are never removed - but nothing will update them, and a receiver taken
below the release that can update itself loses the updates over MQTT.

**Which versions.** Those the verified index lists from the floor up to below the one running,
and only those this integration may install at all: the same rule as everywhere else - contract,
the higher floor, `min_integration`, not withdrawn. A withdrawn version is never offered in
either direction; the floor exists to keep people off a bad version, older ones included.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from . import release_index
from .buildid import base_version
from .const import (
    CAPABILITY_EPG_IMPORT,
    CAPABILITY_HISTORY_CLEAR,
    CAPABILITY_PROCESS,
    CAPABILITY_SOFTCAM,
    CAPABILITY_TOAST,
    CAPABILITY_ZAP_HISTORY,
    DOMAIN,
    PLUGIN_CONTRACT,
    PLUGIN_MIN_VERSION,
)

# The capability a receiver claims when its plugin can update itself over MQTT.
CAPABILITY_SELF_UPDATE = "self_update"

# The first release that has each capability, and the entities that hang on it, by platform and
# translation key. Only capabilities newer than the floor (0.2.0) can be lost to a downgrade the
# rule allows; the plugin's TOPICS.md classifies each (the 0.2.0 -> 0.3.0 row).
CAPABILITY_SINCE: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    CAPABILITY_ZAP_HISTORY: ("0.3.0", (("select", "zap_history"), ("select", "zap_history_all"))),
    CAPABILITY_HISTORY_CLEAR: ("0.3.0", (("button", "history_clear"),)),
    CAPABILITY_SOFTCAM: ("0.3.0", (("sensor", "softcam"), ("button", "softcam_restart"))),
    CAPABILITY_EPG_IMPORT: ("0.3.0", (("sensor", "epg_import"), ("button", "epg_import"))),
    CAPABILITY_PROCESS: (
        "0.3.0",
        (
            ("sensor", "process_memory"),
            ("sensor", "process_memory_peak"),
            ("sensor", "process_threads"),
            ("sensor", "process_open_files"),
            ("sensor", "process_started"),
        ),
    ),
    CAPABILITY_TOAST: ("0.3.0", (("notify", "osd_toast"),)),
}


def downgrade_candidates(
    index: dict[str, Any] | None, installed: str | None, integration_version: str | None
) -> list[str]:
    """The versions the options flow may offer below `installed`, newest first."""
    installed_base = base_version(installed)
    if index is None or installed_base is None:
        return []
    floor = release_index.effective_floor(index, PLUGIN_MIN_VERSION)
    found = []
    for release in index["releases"]:
        if release_index.incompatibility(
            release,
            contract=PLUGIN_CONTRACT,
            floor=floor,
            integration_version=integration_version,
        ) is not None:
            continue
        if release_index.version_key(release["version"]) < installed_base:
            found.append(release["version"])
    return sorted(found, key=release_index.version_key, reverse=True)


async def async_lost_names(
    hass: HomeAssistant, capabilities: list[str], target: str
) -> list[str]:
    """The names, in Home Assistant's language, of the entities `target` would leave stale.

    Only capabilities the receiver claims now: an entity that does not exist cannot be lost.
    """
    target_key = release_index.version_key(target)
    wanted = [
        key
        for capability, (since, keys) in CAPABILITY_SINCE.items()
        if capability in capabilities and target_key < release_index.version_key(since)
        for key in keys
    ]
    if not wanted:
        return []
    texts = await async_get_translations(hass, hass.config.language, "entity", {DOMAIN})
    names = []
    for platform, key in wanted:
        name = texts.get(f"component.{DOMAIN}.entity.{platform}.{key}.name")
        names.append(name or key)
    return names


def loses_self_update(capabilities: list[str], entry: dict[str, Any] | None) -> bool:
    """Whether the receiver can update itself now and the target cannot."""
    return CAPABILITY_SELF_UPDATE in capabilities and not (entry or {}).get("self_update")

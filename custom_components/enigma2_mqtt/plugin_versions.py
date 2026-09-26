"""Which plugin versions one receiver's update card may offer, and which one it will install.

The update entity and „Wersja wtyczki do instalacji" read the same answer, so it is worked out
once, here, from four sources:

- **the receiver** - `info.plugin` and, from the release after 0.3.0, `info.build`;
- **the bundle** - the plugin package this integration ships, and the build id inside it;
- **the signed release index** - which releases exist, which are withdrawn, their commits and
  commit times, and which this integration may offer (`release_index.available`);
- **this entry** - whether SSH credentials are stored, and the version chosen in the select.

**The card offers what it will install, and only when it can** (ADR-0008 section 3).
`latest_version` is the version the card would install when somebody presses the button, and it
exists only while an install path does. Without one there is no badge: `latest_version` is the
installed version, and anything newer is said in the summary and the attributes, with the way to
an install path.
Today the only path is the SSH installer with the bundle, so the only installable version is the
bundle's, when it is newer than what the receiver runs and the index has not withdrawn it; the
versions the index lists beyond it are information, not offers, until this integration can
download a package.

**The select** is honoured only while it is enabled in the entity registry. Disabled - its
default - it counts as `latest`, whatever was stored the last time somebody used it, so a
household that never opened it gets exactly what it got before the select existed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from . import bundle as bundle_module, release_index
from .box import Enigma2Box, Enigma2MqttConfigEntry
from .buildid import Build, base_version, build_of, is_newer
from .bundle import BundledPlugin, BundleError
from .const import (
    CONF_PLUGIN_TARGET_VERSION,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_USERNAME,
    DOMAIN,
    PLUGIN_CONTRACT,
    PLUGIN_MIN_VERSION,
    TARGET_LATEST,
)
from .release_store import ReleaseIndexCache, async_release_index_cache

PATH_SSH = "ssh"

# The select's unique id suffix, which is also its translation key.
KEY_PLUGIN_INSTALL_VERSION = "plugin_install_version"

_DATA_KEY = f"{DOMAIN}_plugin_versions"


def has_credentials(entry: Enigma2MqttConfigEntry) -> bool:
    """Return whether a complete pinned SSH identity is retained for this receiver.

    The password is checked the same way as the rest: an empty string is a value Home
    Assistant will happily store, and offering an install that is certain to fail
    authentication is worse than not offering one.
    """
    data = entry.data
    return all(
        isinstance(data.get(key), str) and data[key]
        for key in (CONF_SSH_HOST, CONF_SSH_USERNAME, CONF_SSH_HOST_KEY, CONF_SSH_PASSWORD)
    )


def _load_bundle() -> BundledPlugin | None:
    """The bundled plugin, or None when it will not load - which is itself worth showing."""
    try:
        return bundle_module.load_bundled_plugin()
    except BundleError:
        return None


@dataclass
class PluginVersions:
    """The version picture of one receiver, recomputed on every read."""

    hass: HomeAssistant
    entry: Enigma2MqttConfigEntry
    box: Enigma2Box
    bundle: BundledPlugin | None
    cache: ReleaseIndexCache

    # ------------------------------------------------------------------ the pieces --

    @property
    def index(self) -> dict[str, Any] | None:
        """The accepted signed index, or None before one is."""
        return self.cache.index

    def _release(self, version: str | None) -> dict[str, Any] | None:
        return release_index.release_of(self.index, version)

    @property
    def path(self) -> str | None:
        """How this card would install: over SSH with the bundle, or not at all."""
        if self.bundle is not None and has_credentials(self.entry):
            return PATH_SSH
        return None

    def installed(self) -> Build | None:
        """What the receiver runs, as a build - or None until it has said."""
        version = self.box.info.get("plugin")
        if not isinstance(version, str) or not version:
            return None
        release = self._release(version)
        return build_of(
            version,
            self.box.info.get("build"),
            release_commit=release["commit"] if release else self._bundle_commit(version),
            release_time=release["commit_time"] if release else None,
        )

    def _bundle_commit(self, version: str) -> str | None:
        """The bundle's commit, when the bundle is the release build of `version`."""
        if (
            self.bundle is not None
            and self.bundle.version == version
            and self.bundle.build is not None
            and self.bundle.build.get("flavour") == "release"
            and self.bundle.build.get("dirty") is False
        ):
            return self.bundle.source_commit
        return None

    def bundled(self) -> Build | None:
        """The bundle as a build, or None when there is no usable bundle."""
        if self.bundle is None:
            return None
        release = self._release(self.bundle.version)
        return build_of(
            self.bundle.version,
            self.bundle.build,
            release_commit=release["commit"] if release else None,
            release_time=release["commit_time"] if release else None,
        )

    def bundle_withdrawn(self) -> str | None:
        """The index's reason for withdrawing the bundle's version, or None."""
        if self.bundle is None:
            return None
        release = self._release(self.bundle.version)
        return release.get("withdrawn") if release else None

    # ------------------------------------------------------------------ the offers --

    def offers(self) -> list[Build]:
        """The versions this card can install over what the receiver runs, newest first."""
        installed = self.installed()
        bundled = self.bundled()
        if (
            installed is None
            or bundled is None
            or self.path is None
            or self.bundle_withdrawn() is not None
        ):
            return []
        return [bundled] if is_newer(bundled, installed) else []

    def select_enabled(self) -> bool:
        """Whether „Wersja wtyczki do instalacji" is enabled in the entity registry."""
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            Platform.SELECT, DOMAIN, f"{self.box.node_id}_{KEY_PLUGIN_INSTALL_VERSION}"
        )
        if entity_id is None:
            return False
        registered = registry.async_get(entity_id)
        return registered is not None and registered.disabled_by is None

    def stored_target(self) -> str:
        """The choice stored in the entry, `latest` when there is none."""
        stored = self.entry.options.get(CONF_PLUGIN_TARGET_VERSION)
        return stored if isinstance(stored, str) and stored else TARGET_LATEST

    def target(self) -> str:
        """The choice that counts: the stored one while the select is enabled, else `latest`."""
        return self.stored_target() if self.select_enabled() else TARGET_LATEST

    def select_options(self) -> list[str]:
        """`latest`, then every version this card can install, newest first."""
        return [TARGET_LATEST, *(build.display for build in self.offers())]

    def chosen(self) -> Build | None:
        """The build the card would install now, or None when there is nothing to install."""
        offers = self.offers()
        if not offers:
            return None
        target = self.target()
        if target != TARGET_LATEST:
            for build in offers:
                if build.display == target:
                    return build
        return offers[0]

    def latest(self) -> Build | None:
        """`latest_version`: the chosen offer, else what is installed - never a badge without
        an install path."""
        installed = self.installed()
        if installed is None:
            return None
        return self.chosen() or installed

    # --------------------------------------------------------------- what to report --

    def available(self) -> list[dict[str, Any]]:
        """The index's releases at or above the floor, with a reason for any not offered."""
        if self.index is None:
            return []
        return release_index.available(
            self.index,
            contract=PLUGIN_CONTRACT,
            own_floor=PLUGIN_MIN_VERSION,
            integration_version=self.cache.integration_version,
        )

    def newest_listed(self) -> str | None:
        """The newest version the index lists that this integration may offer, or None."""
        for item in self.available():
            if item["compatible"]:
                return item["version"]
        return None

    def newest_beyond(self) -> str | None:
        """The newest compatible version in the index above what the card offers and runs.

        What the summary names: a release this card cannot install yet, or one above the
        version pinned in the select.
        """
        newest = self.newest_listed()
        newest_base = base_version(newest)
        if newest is None or newest_base is None:
            return None
        # Neither the version the card names nor the bundle - which the summary already
        # names on its own - is news.
        known = [
            base
            for build in (self.latest(), self.bundled())
            if build is not None and (base := base_version(build.version)) is not None
        ]
        if known and newest_base <= max(known):
            return None
        return newest

    def builds(self) -> dict[str, Build]:
        """Every build this entity names, by the string it names it with."""
        found: dict[str, Build] = {}
        for build in (self.installed(), self.bundled(), *self.offers()):
            if build is not None:
                found.setdefault(build.display, build)
        return found


async def async_plugin_versions(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry
) -> PluginVersions:
    """The version picture of one receiver, built once per setup of its entry.

    The update and select platforms are set up side by side and both ask; the first builds it -
    loading the bundle in the executor and the index cache from storage - and the second waits
    for the same result.
    """
    pending: dict[str, asyncio.Future[PluginVersions]] = hass.data.setdefault(_DATA_KEY, {})
    if (future := pending.get(entry.entry_id)) is None:
        future = pending[entry.entry_id] = hass.loop.create_future()

        @callback
        def _forget() -> None:
            if pending.get(entry.entry_id) is future:
                del pending[entry.entry_id]

        entry.async_on_unload(_forget)
        try:
            loaded = await hass.async_add_executor_job(_load_bundle)
            cache = async_release_index_cache(hass)
            await cache.async_load()
            future.set_result(
                PluginVersions(hass, entry, entry.runtime_data, loaded, cache)
            )
        except asyncio.CancelledError:
            _forget()
            future.cancel()
            raise
        except Exception as error:
            _forget()
            future.set_exception(error)
            # Retrieved here, so a failure nobody else was waiting for is not logged twice.
            future.exception()
            raise
    return await asyncio.shield(future)

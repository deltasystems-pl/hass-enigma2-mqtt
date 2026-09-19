"""The bundled receiver plugin version and its guarded update action."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from packaging.version import InvalidVersion, Version

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .bundle import BundleError, load_bundled_plugin
from .const import (
    CONF_BASE_TOPIC,
    CONF_NODE_ID,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    DEFAULT_BASE_TOPIC,
    PLUGIN_RELEASES_URL,
    SUPPORTED_PLUGIN_VERSION,
    TOPIC_INFO,
)
from .entity import Enigma2Entity
from .installer import (
    InstallerError,
    InstallerErrorCode,
    InstallRequest,
    SshCredentials,
    async_install,
)

PARALLEL_UPDATES = 0

# The installer's phases, in the order it reports them. The update card has one bar, so
# the phase becomes a percentage; the config flow shows the same phases as words.
INSTALL_PHASES = (
    "preflight",
    "backup",
    "upload",
    "install",
    "provision",
    "restart",
    "announcement",
    "done",
)


def _version(value: str | None) -> Version | None:
    """Parse a plugin version without guessing how an unknown version sorts."""
    if not value:
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def latest_version(installed: str | None, supported: str) -> str:
    """Return the version to offer, given what is on the box.

    Whichever of the two is higher: a box that has run ahead of this integration is not
    out of date, and telling a user to install an older plugin over a newer one would
    be the one thing worse than saying nothing.
    """
    installed_version = _version(installed)
    supported_version = _version(supported)
    if installed_version is not None and supported_version is not None:
        return installed if installed_version > supported_version else supported
    return supported


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the plugin version entity."""
    try:
        bundle = await hass.async_add_executor_job(load_bundled_plugin)
        bundled_version = bundle.version
    except BundleError:
        bundled_version = None
    async_add_entities(
        [Enigma2PluginUpdate(entry.runtime_data, entry, bundled_version)]
    )


class Enigma2PluginUpdate(Enigma2Entity, UpdateEntity):
    """The MQTT Bridge plugin's version, against the one this release expects."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_release_url = PLUGIN_RELEASES_URL
    _attr_title = "Enigma2 MQTT Bridge"

    def __init__(
        self,
        box: Enigma2Box,
        entry: Enigma2MqttConfigEntry,
        bundled_version: str | None,
    ) -> None:
        """Set up the update entity."""
        super().__init__(box, "plugin", topics=(TOPIC_INFO,))
        self._entry = entry
        self._bundled_version = bundled_version
        self._refresh_install_feature()

    def _has_credentials(self) -> bool:
        """Return whether a complete pinned SSH identity is retained for this box.

        The password is checked the same way as the rest: an empty string is a value
        Home Assistant will happily store, and advertising an install that is certain
        to fail authentication is worse than not offering one.
        """
        data = self._entry.data
        return all(
            isinstance(data.get(key), str) and data[key]
            for key in (CONF_SSH_HOST, CONF_SSH_USERNAME, CONF_SSH_HOST_KEY, CONF_SSH_PASSWORD)
        )

    def _refresh_install_feature(self) -> None:
        """Expose install only when a complete pinned SSH identity is retained."""
        self._attr_supported_features = (
            UpdateEntityFeature.INSTALL
            | UpdateEntityFeature.SPECIFIC_VERSION
            | UpdateEntityFeature.PROGRESS
            if self._bundled_version is not None and self._has_credentials()
            else UpdateEntityFeature(0)
        )

    @callback
    def _async_read_state(self) -> None:
        """Read the plugin version the box reports on `info`."""
        installed = self.box.info.get("plugin")
        self._attr_installed_version = installed if isinstance(installed, str) else None
        offered = self._bundled_version or SUPPORTED_PLUGIN_VERSION
        self._attr_latest_version = latest_version(self._attr_installed_version, offered)
        self._refresh_install_feature()

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the verified bundled plugin while preserving on-box settings."""
        del backup, kwargs
        installed = _version(self.installed_version)
        bundled = _version(self._bundled_version)
        if installed is None or bundled is None:
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key="update_version_unknown",
            )
        if installed > bundled:
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key="update_downgrade",
            )
        if version not in (None, self._bundled_version):
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key="update_version_unavailable",
            )

        data = self._entry.data
        self._refresh_install_feature()
        if not self.supported_features & UpdateEntityFeature.INSTALL:
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key=(
                    "update_no_credentials"
                    if self._bundled_version is not None
                    else "update_version_unavailable"
                ),
            )
        request = InstallRequest(
            credentials=SshCredentials(
                host=data[CONF_SSH_HOST],
                port=data.get(CONF_SSH_PORT, 22),
                username=data[CONF_SSH_USERNAME],
                password=data[CONF_SSH_PASSWORD],
                host_key=data[CONF_SSH_HOST_KEY],
            ),
            provisioning=None,
            expect_running=True,
            node_id=data[CONF_NODE_ID],
            base_topic=data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC),
        )
        self._attr_in_progress = True
        self._attr_update_percentage = None
        self.async_write_ha_state()
        try:
            await async_install(self.hass, request, self._async_installer_phase)
        except InstallerError as err:
            if err.code in (
                InstallerErrorCode.AUTH_FAILED,
                InstallerErrorCode.HOST_KEY_CHANGED,
            ):
                self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key="update_failed",
                translation_placeholders={"reason": err.code.value},
            ) from err
        finally:
            # A spinner that never stops is worse than none: whatever happened, the
            # entity has to stop claiming an install is still running.
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()

    @callback
    def _async_installer_phase(self, phase: str) -> None:
        """Turn the installer's named phase into a percentage for the update card."""
        if phase not in INSTALL_PHASES:
            return
        self._attr_update_percentage = (INSTALL_PHASES.index(phase) + 1) * 100 // len(
            INSTALL_PHASES
        )
        self.async_write_ha_state()

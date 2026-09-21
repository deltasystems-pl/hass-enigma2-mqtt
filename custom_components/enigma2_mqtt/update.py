"""The bundled receiver plugin version and its guarded update action.

Three versions meet in this entity and only two of them may move its state. The
**installed** version is what the box reports on `info`. The **bundled** version is the
IPK that ships inside this integration, and it is the only thing `install` can ever put
on a receiver — so it, and nothing else, is what `latest_version` offers. The
**published** version is what the plugin repository last released; it is asked for only
when somebody turns the option on, at most once a day, and it is allowed to appear in
the summary, in an attribute and in the release link and nowhere else. Letting it raise
`latest_version` would put an update button on a card that cannot install what it
promises.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any

import aiohttp
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.translation import async_get_translations
import homeassistant.util.dt as dt_util
from packaging.version import InvalidVersion, Version

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .bundle import BundleError, load_bundled_plugin
from .const import (
    CONF_BASE_TOPIC,
    CONF_CHECK_GITHUB_RELEASES,
    CONF_NODE_ID,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    DEFAULT_BASE_TOPIC,
    DEFAULT_CHECK_GITHUB_RELEASES,
    DOMAIN,
    PLUGIN_LATEST_RELEASE_URL,
    PLUGIN_RELEASES_URL,
    RELEASE_CHECK_INTERVAL,
    RELEASE_CHECK_TIMEOUT,
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

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# How the plugin on the box stands against the one this integration carries. The words
# are the answer to "is this receiver a version problem", which is the first question
# every missing entity and every unrecognised command leads back to.
COMPATIBILITY_MATCHED = "matched"
COMPATIBILITY_OLDER = "older_than_bundle"
COMPATIBILITY_NEWER = "newer_than_bundle"
COMPATIBILITY_UNKNOWN = "unknown"

# Sentences assembled into `release_summary`, which Home Assistant cuts at 255
# characters. They live in `common` so that they are translated like everything else a
# household reads; a language that is missing one simply leaves that sentence out.
SUMMARY_OLDER = "update_summary_older"
SUMMARY_OLDER_NO_INSTALL = "update_summary_older_no_install"
SUMMARY_NEWER = "update_summary_newer"
SUMMARY_PUBLISHED = "update_summary_published"
SUMMARY_LIMIT = 255

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


def _newer(candidate: str | None, than: str | None) -> bool:
    """Return whether one version sorts above another, refusing to guess."""
    first = _version(candidate)
    second = _version(than)
    return first is not None and second is not None and first > second


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


def plugin_compatibility(installed: str | None, bundled: str | None) -> str:
    """Return how the plugin on the box stands against the bundled one.

    `latest_version` already refuses to offer a downgrade, which makes a receiver ahead
    of this integration look exactly like one in step: both read "up to date". That is
    the right thing to show a household and the wrong thing to hand a bug report, so the
    difference is stated here instead, and the diagnostics download carries it.
    """
    installed_version = _version(installed)
    bundled_version = _version(bundled)
    if installed_version is None or bundled_version is None:
        return COMPATIBILITY_UNKNOWN
    if installed_version < bundled_version:
        return COMPATIBILITY_OLDER
    if installed_version > bundled_version:
        return COMPATIBILITY_NEWER
    return COMPATIBILITY_MATCHED


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
        self._installing = False
        self._check_releases = bool(
            entry.options.get(CONF_CHECK_GITHUB_RELEASES, DEFAULT_CHECK_GITHUB_RELEASES)
        )
        self._published_version: str | None = None
        self._published_url: str | None = None
        self._last_release_check: datetime | None = None
        self._texts: dict[str, str] = {}
        self._refresh_install_feature()

    async def async_added_to_hass(self) -> None:
        """Load the summary sentences, and start the opt-in release check."""
        await super().async_added_to_hass()
        self._texts = await async_get_translations(
            self.hass, self.hass.config.language, "common", {DOMAIN}
        )
        if self._check_releases:
            # Saving the options reloads the entry, so the option is read once here and
            # the timer only exists on an entity that was built while it was on.
            self.async_on_remove(
                async_track_time_interval(
                    self.hass,
                    self._async_check_release,
                    RELEASE_CHECK_INTERVAL,
                    cancel_on_shutdown=True,
                )
            )
            self.hass.async_create_task(self._async_check_release())
        self._async_read_state()
        self.async_write_ha_state()

    def _text(self, slug: str, **placeholders: str) -> str | None:
        """Return one translated sentence, or None when the language lacks it."""
        text = self._texts.get(f"component.{DOMAIN}.common.{slug}")
        if not text:
            return None
        try:
            return text.format(**placeholders)
        except (IndexError, KeyError):
            # A translation that invented a placeholder is a string nobody can render.
            # Dropping the sentence is better than putting braces on a device page.
            _LOGGER.debug("The translated sentence %s has an unexpected placeholder", slug)
            return None

    def _summary(self, offered: str) -> str | None:
        """Say what the version numbers mean, and what to do about it.

        `latest_version` is a number on a card with an install button that may not be
        there. Which of the two cases a household is looking at — and, when the button
        is missing, how to get it — is not deducible from two version strings.
        """
        sentences: list[str] = []
        compatibility = plugin_compatibility(self.installed_version, offered)
        if compatibility == COMPATIBILITY_OLDER:
            sentences.append(
                self._text(
                    SUMMARY_OLDER
                    if self.supported_features & UpdateEntityFeature.INSTALL
                    else SUMMARY_OLDER_NO_INSTALL,
                    version=offered,
                )
            )
        elif compatibility == COMPATIBILITY_NEWER:
            sentences.append(self._text(SUMMARY_NEWER, version=offered))
        if self._published_version is not None and _newer(self._published_version, offered):
            sentences.append(
                self._text(SUMMARY_PUBLISHED, version=self._published_version)
            )
        summary = " ".join(sentence for sentence in sentences if sentence)
        return summary[:SUMMARY_LIMIT] or None

    async def _async_check_release(self, now: datetime | None = None) -> None:
        """Ask the plugin repository what it last published. Opt-in, daily, silent.

        Every failure is a debug line and nothing else. This is a convenience nobody
        asked to be told about, running against a service with a rate limit shared by
        everything else on the same address, and a repair notification about GitHub on a
        receiver's device page would be noise about a problem that is not the receiver's.

        Only reached on an entity built while the option was on: the timer below is not
        registered otherwise, and changing the option reloads the entry. The interval
        sets the cadence and the stamp enforces it, because a timer that fires early —
        or twice — must not turn one day's request into two.
        """
        del now
        if (
            self._last_release_check is not None
            and dt_util.utcnow() - self._last_release_check < RELEASE_CHECK_INTERVAL
        ):
            return
        # Stamped before the request, not after it: a check that times out has still
        # spent this day's request, and retrying it on every state change would turn one
        # unreachable service into a loop.
        self._last_release_check = dt_util.utcnow()
        session = async_get_clientsession(self.hass)
        try:
            async with (
                asyncio.timeout(RELEASE_CHECK_TIMEOUT),
                session.get(
                    PLUGIN_LATEST_RELEASE_URL,
                    headers={"Accept": "application/vnd.github+json"},
                ) as response,
            ):
                if response.status != 200:
                    # 403 with no body is the rate limit, and it is the answer a busy
                    # address gets rather than a fault anybody can fix.
                    _LOGGER.debug(
                        "The plugin release check answered HTTP %s", response.status
                    )
                    return
                payload = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("The plugin release check did not answer: %s", err)
            return

        tag = payload.get("tag_name") if isinstance(payload, dict) else None
        if not isinstance(tag, str) or not tag.strip():
            _LOGGER.debug("The plugin release check returned no usable tag")
            return
        url = payload.get("html_url")
        self._published_version = tag.strip().removeprefix("v")
        self._published_url = url if isinstance(url, str) and url else None
        self._async_read_state()
        self.async_write_ha_state()

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
        # The link follows the published release when one is known, because a card that
        # names a version and links somewhere else is a card nobody can check.
        self._attr_release_url = self._published_url or PLUGIN_RELEASES_URL
        self._attr_extra_state_attributes = {
            "bundled_version": self._bundled_version,
            "published_version": self._published_version,
            "compatibility": plugin_compatibility(self._attr_installed_version, offered),
        }
        self._attr_release_summary = self._summary(offered)

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
        # A second `update.install` on the same entity is refused by the installer as
        # busy, and it must not take the first one's spinner down with it on the way
        # out. Only the call that raised the flag lowers it.
        mine = not self._installing
        if mine:
            self._installing = True
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
            # call that started this install has to stop claiming it is still running.
            if mine:
                self._installing = False
                self._attr_in_progress = False
                self._attr_update_percentage = None
                self.async_write_ha_state()

    @callback
    def _async_installer_phase(self, phase: str) -> None:
        """Turn the installer's named phase into a percentage for the update card."""
        if not self._installing or phase not in INSTALL_PHASES:
            return
        self._attr_update_percentage = (INSTALL_PHASES.index(phase) + 1) * 100 // len(
            INSTALL_PHASES
        )
        self.async_write_ha_state()

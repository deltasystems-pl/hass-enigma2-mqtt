"""The receiver plugin's version, what this card will install, and the guarded install.

Several versions meet in this entity and only two of them may move its state. The
**installed** version is what the box reports on `info`, shown with its build id: a
development build of 0.3.0 reads `0.3.0+g1a2b3c4`, never plain `0.3.0`. The **latest**
version is what pressing install would put on the receiver - and only while an install path
exists; without one it is the installed version, so there is no update badge on a card that
could not act on it (ADR-0008 section 3, a behaviour change from 0.3.1, which showed the bundle as
an update even with no way to install it). The **bundled** version is the package this
integration ships, today the only one the installer can put on a receiver. The versions the
plugin's **signed release index** lists are information: they appear in the summary and the
attributes, never as `latest_version` until this integration can install them.

The picture is worked out in `plugin_versions.py`, shared with the select that lets a
household pin a version. Which build is newer than which is `buildid.is_newer`, and it is the
release number and nothing else (ADR-0008 section 3, decided 2026-09-26): a higher number
badges; the same number never does, in either direction. A development build is not shown as
current - its display and the summary say what it is, and that the release of its number is
available - but the release is not offered over it, because it may be older code. A
same-number build is installed only when chosen by name: in the version select, which then
turns the state on, or with `update.install` and that version.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.translation import async_get_translations
import homeassistant.util.dt as dt_util

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .buildid import Build, base_version, is_newer
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
    PLUGIN_RELEASES_URL,
    RELEASE_CHECK_INTERVAL,
    SIGNAL_RELEASE_INDEX,
    SIGNAL_TARGET_VERSION,
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
from .plugin_versions import PluginVersions, async_plugin_versions
from .release_store import CheckRateLimited

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# How the plugin on the box stands against the one this integration carries, by `N.N.N`.
# The words are the answer to "is this receiver a version problem", which is the first
# question every missing entity and every unrecognised command leads back to.
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
SUMMARY_DEVELOPMENT = "update_summary_development"
SUMMARY_PUBLISHED = "update_summary_published"
SUMMARY_LIMIT = 255
# Why the bundle may not be installed, in the words the summary uses.
_REFUSAL_SENTENCES = {
    "withdrawn": "update_summary_withdrawn",
    "below_floor": "update_summary_below_floor",
}


def _build_attribute(build: Build | None) -> dict[str, Any] | None:
    """A build as the attributes show it: what it is, never how it sorts."""
    if build is None or not build.known:
        return None
    return {
        "commit": build.commit,
        "time": build.time,
        "dirty": build.dirty,
        "flavour": build.flavour,
    }

# The attributes the recorder is not given: the list of releases changes with every index and
# says nothing a history graph needs.
ATTR_AVAILABLE_VERSIONS = "available_versions"

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


# The installer outcomes the update card states as a sentence of their own.
_UPDATE_SENTENCES: dict[InstallerErrorCode, str] = {
    InstallerErrorCode.RESTART_WITHDRAWN: "update_withdrawn",
    InstallerErrorCode.STANDBY: "update_standby",
    InstallerErrorCode.STREAMING: "update_streaming",
    InstallerErrorCode.RESTART_UNOBSERVED: "update_restart_unobserved",
    InstallerErrorCode.ROLLBACK_UNOBSERVED: "update_rollback_unobserved",
    InstallerErrorCode.RESTART_UNCONFIRMED: "update_restart_unconfirmed",
    InstallerErrorCode.WITHDRAW_FAILED: "update_withdraw_failed",
}


def plugin_compatibility(installed: str | None, bundled: str | None) -> str:
    """Return how the plugin on the box stands against the bundled one, by `N.N.N`.

    `latest_version` never offers a downgrade, which makes a receiver ahead of this
    integration look exactly like one in step: both read "up to date". That is the right
    thing to show a household and the wrong thing to hand a bug report, so the difference
    is stated here instead, and the diagnostics download carries it. A build label after
    `+` is not a version and plays no part.
    """
    installed_version = base_version(installed)
    bundled_version = base_version(bundled)
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
    versions = await async_plugin_versions(hass, entry)
    async_add_entities([Enigma2PluginUpdate(entry.runtime_data, entry, versions)])


class Enigma2PluginUpdate(Enigma2Entity, UpdateEntity):
    """The MQTT Bridge plugin's version, and what this card will install over it."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_release_url = PLUGIN_RELEASES_URL
    _attr_title = "Enigma2 MQTT Bridge"
    _unrecorded_attributes = frozenset({ATTR_AVAILABLE_VERSIONS})

    def __init__(
        self,
        box: Enigma2Box,
        entry: Enigma2MqttConfigEntry,
        versions: PluginVersions,
    ) -> None:
        """Set up the update entity."""
        super().__init__(box, "plugin", topics=(TOPIC_INFO,))
        self._entry = entry
        self._versions = versions
        self._installing = False
        self._check_releases = bool(
            entry.options.get(CONF_CHECK_GITHUB_RELEASES, DEFAULT_CHECK_GITHUB_RELEASES)
        )
        self._texts: dict[str, str] = {}
        # The builds this entity names, by the string it names them with, so that Home
        # Assistant's `version_is_newer` - which only has the strings - can be answered with
        # what is known about each.
        self._builds: dict[str, Build] = {}
        # The build chosen by name in the enabled version select, whose display string then
        # counts as an update whatever its number (`version_is_newer`).
        self._explicit: str | None = None
        self._refresh_install_feature()

    async def async_added_to_hass(self) -> None:
        """Load the summary sentences, follow the index, and start the opt-in daily check."""
        await super().async_added_to_hass()
        self._texts = await async_get_translations(
            self.hass, self.hass.config.language, "common", {DOMAIN}
        )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_RELEASE_INDEX, self._handle_box_update)
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_TARGET_VERSION}_{self._entry.entry_id}",
                self._handle_box_update,
            )
        )
        if self._check_releases:
            # Saving the options reloads the entry, so the option is read once here and
            # the timer only exists on an entity that was built while it was on. The cache
            # keeps the stamp, so every receiver's timer together still makes one request a
            # day, and a reload or a restart none.
            self.async_on_remove(
                async_track_time_interval(
                    self.hass,
                    self._async_daily_check,
                    RELEASE_CHECK_INTERVAL,
                    cancel_on_shutdown=True,
                )
            )
            # On the entry, so that unloading the receiver takes the request with it
            # rather than leaving it to finish against an entity that is already gone.
            self._entry.async_create_background_task(
                self.hass,
                self._async_daily_check(),
                name=f"{DOMAIN} release check {self._entry.entry_id}",
            )
        self._async_read_state()
        self.async_write_ha_state()

    async def _async_daily_check(self, now: datetime | None = None) -> None:
        """The opt-in daily check. Its failures are the attributes' business, not a log's."""
        del now
        await self._versions.cache.async_check(manual=False)

    async def async_update(self) -> None:
        """Home Assistant's "Check for updates": a check now, if the option asks for checks.

        Off, this integration talks to nothing but the broker, and a button in Home
        Assistant's settings is not a reason to change that. On, it is a manual check and
        shares the ten-minute limit with „Sprawdź aktualizacje wtyczki" - silently, because
        this is asked of every update entity at once and a refusal would be noise.
        """
        if not self._check_releases:
            return
        try:
            await self._versions.cache.async_check(manual=True)
        except CheckRateLimited:
            return

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

    def _summary(self, installed: Build | None, bundled: Build | None) -> str | None:
        """Say what the version numbers mean, and what to do about it.

        `latest_version` is a number on a card with an install button that may not be
        there. Which of the cases a household is looking at - and, when the button is
        missing, how to get it - is not deducible from two version strings.
        """
        sentences: list[str | None] = []
        offered = bundled.display if bundled is not None else SUPPORTED_PLUGIN_VERSION
        compatibility = plugin_compatibility(
            installed.version if installed is not None else None, offered
        )
        has_path = bool(self.supported_features & UpdateEntityFeature.INSTALL)
        if (release := self._versions.same_version_release()) is not None:
            # First, because it is the one thing the version strings cannot say: the
            # receiver runs a development build, and the release of its number exists.
            sentences.append(self._text(SUMMARY_DEVELOPMENT, version=release))
        refusal = self._versions.bundle_refusal()
        if refusal is not None and compatibility == COMPATIBILITY_OLDER:
            # The bundle is newer than the receiver's plugin, and still may not be installed.
            code, placeholders = refusal
            sentences.append(self._text(_REFUSAL_SENTENCES[code], **placeholders))
        elif compatibility == COMPATIBILITY_OLDER:
            sentences.append(
                self._text(
                    SUMMARY_OLDER if has_path else SUMMARY_OLDER_NO_INSTALL,
                    version=offered,
                )
            )
        elif compatibility == COMPATIBILITY_NEWER:
            sentences.append(self._text(SUMMARY_NEWER, version=offered))
        if (newest := self._versions.newest_beyond()) is not None:
            sentences.append(self._text(SUMMARY_PUBLISHED, version=newest))

        # Home Assistant cuts a summary at 255 characters, and where it cuts is where
        # the 255th character happens to fall - mid-word, and in a language with long
        # compounds usually mid-sentence. The sentences here are in priority order: the
        # first says what the version numbers mean and what to do, the second is extra
        # context about the index. A second sentence that will not fit whole is dropped
        # whole, because half of it reads as a bug rather than as a summary.
        summary = ""
        for sentence in sentences:
            if not sentence:
                continue
            if not summary:
                summary = sentence
            elif len(summary) + 1 + len(sentence) <= SUMMARY_LIMIT:
                summary = f"{summary} {sentence}"
            else:
                _LOGGER.debug("The release summary has no room for every sentence")
                break
        return summary or None

    def _refresh_install_feature(self) -> None:
        """Expose install only when there is a way to install: today, SSH and the bundle."""
        self._attr_supported_features = (
            UpdateEntityFeature.INSTALL
            | UpdateEntityFeature.SPECIFIC_VERSION
            | UpdateEntityFeature.PROGRESS
            if self._versions.path is not None
            else UpdateEntityFeature(0)
        )

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Whether the card should say "update available" for `latest_version`.

        Home Assistant asks this only when the two strings differ. A higher release number
        is an update. So is a build somebody chose by name in the enabled version select -
        the choice is the consent, including for a build of the same number, which is never
        an update by itself.
        """
        if latest_version == self._explicit:
            return True
        latest = self._builds.get(latest_version) or Build(version=latest_version)
        installed = self._builds.get(installed_version) or Build(version=installed_version)
        return is_newer(latest, installed)

    @callback
    def _async_read_state(self) -> None:
        """Work the version picture out again, from the box, the bundle and the index."""
        versions = self._versions
        cache = versions.cache
        installed = versions.installed()
        bundled = versions.bundled()
        latest = versions.latest()
        self._builds = versions.builds()
        explicit = versions.explicit()
        self._explicit = explicit.display if explicit is not None else None
        self._refresh_install_feature()
        self._attr_installed_version = installed.display if installed is not None else None
        self._attr_latest_version = latest.display if latest is not None else None
        published = versions.newest_listed()
        # The link follows the newest listed release when there is one, because a card that
        # names a version and links somewhere else is a card nobody can check.
        self._attr_release_url = (
            f"{PLUGIN_RELEASES_URL}/tag/v{published}" if published else PLUGIN_RELEASES_URL
        )
        issued = cache.issued
        self._attr_extra_state_attributes = {
            "bundled_version": bundled.display if bundled is not None else None,
            "published_version": published,
            "compatibility": plugin_compatibility(
                installed.version if installed is not None else None,
                bundled.version if bundled is not None else SUPPORTED_PLUGIN_VERSION,
            ),
            ATTR_AVAILABLE_VERSIONS: versions.available(),
            "last_check": cache.checked.isoformat() if cache.checked else None,
            "check_error": cache.check_error,
            "index_serial": cache.serial,
            # Whole days since the index was built. There is no expiry - a withdrawal
            # reaches this card only with a newer index - so its age is what says how
            # fresh the list is.
            "index_age": (
                max(0, int((dt_util.utcnow().timestamp() - issued) // 86400))
                if issued is not None
                else None
            ),
            "update_path": versions.path,
            # A development build is never shown as current: these say what it is, and
            # which release of its own number is available instead of a badge for it.
            "development_build": (
                not installed.is_release if installed is not None else None
            ),
            "same_version_release": versions.same_version_release(),
            # Information only - nothing orders or offers by them.
            "installed_build": _build_attribute(installed),
            "bundled_build": _build_attribute(bundled),
        }
        self._attr_release_summary = self._summary(installed, bundled)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the verified bundled plugin while preserving on-box settings."""
        del backup, kwargs
        versions = self._versions
        installed = versions.installed()
        bundled = versions.bundled()
        installed_base = base_version(installed.version) if installed is not None else None
        bundled_base = base_version(bundled.version) if bundled is not None else None
        if installed_base is None or bundled_base is None or bundled is None:
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key="update_version_unknown",
            )
        if installed_base > bundled_base:
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key="update_downgrade",
            )
        if version not in (None, bundled.display, bundled.version):
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key="update_version_unavailable",
            )
        if (refusal := versions.bundle_refusal()) is not None:
            code, placeholders = refusal
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key=f"update_version_{code}",
                translation_placeholders=placeholders,
            )

        data = self._entry.data
        self._refresh_install_feature()
        if not self.supported_features & UpdateEntityFeature.INSTALL:
            raise HomeAssistantError(
                translation_domain="enigma2_mqtt",
                translation_key="update_no_credentials",
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
            if (sentence := _UPDATE_SENTENCES.get(err.code)) is not None:
                # Outcomes a household has to be told in words: a code on the update
                # card would not say that a question may still be on the television.
                raise HomeAssistantError(
                    translation_domain="enigma2_mqtt", translation_key=sentence
                ) from err
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

"""The bundled receiver plugin version and its guarded update action.

Three versions meet in this entity and only two of them may move its state. The
**installed** version is what the box reports on `info`. The **bundled** version is the
IPK that ships inside this integration, and it is the only thing `install` can ever put
on a receiver - so it, and nothing else, is what `latest_version` offers. The
**published** version is what the plugin repository last released; it is asked for only
when somebody turns the option on, at most once a day, and it is allowed to appear in
the summary, in an attribute and in the release link and nowhere else. Letting it raise
`latest_version` would put an update button on a card that cannot install what it
promises.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
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
    RELEASE_BODY_LIMIT,
    RELEASE_CHECK_INTERVAL,
    RELEASE_CHECK_TIMEOUT,
    RELEASE_TAG_MAX,
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
from .release_store import async_release_check_store

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

# The only place a release link may point. The JSON that carries it comes off the
# network, and a `release_url` is a link a user is invited to click from their own
# device page - so it is checked against where it is supposed to lead rather than
# trusted because of the field it arrived in.
RELEASE_URL_PREFIX = f"{PLUGIN_RELEASES_URL}/"

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


def _version(value: str | None) -> Version | None:
    """Parse a plugin version without guessing how an unknown version sorts."""
    if not value:
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def published_release_url(url: Any) -> str | None:
    """Return a release link only when it leads to this plugin's releases.

    `html_url` arrives in a JSON document fetched over the network, and it ends up as
    the "Release notes" link on a device page - somewhere a household is invited to
    click. A field being called `html_url` says nothing about where it points: a
    redirected DNS name, a captive portal or a compromised answer can put any address
    there, `javascript:` included. So the value is compared against where a release of
    this plugin can actually live, and anything else is dropped rather than shown.
    """
    if isinstance(url, str) and url.startswith(RELEASE_URL_PREFIX):
        return url
    return None


def published_release_version(tag: Any) -> str | None:
    """Return a tag as a version, or None if it is not one.

    Two separate refusals. The length cap is about the tag as *text*: it reaches an
    attribute and a sentence on a device page, and there is no version that needs more
    than a handful of characters. The parse is about the tag as a *version*: everything
    downstream compares it with the bundled build, and a string that does not sort is
    not a comparison - it is a label pretending to be one.
    """
    if not isinstance(tag, str) or len(tag) > RELEASE_TAG_MAX:
        return None
    candidate = tag.strip().removeprefix("v")
    return candidate if _version(candidate) is not None else None


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
            # What the last entity heard, and when it asked. This entity is a reload, an
            # options save or a restart away from that one, and none of those is a
            # reason to spend another request or to blank the tag on the card.
            (
                self._last_release_check,
                self._published_version,
                self._published_url,
            ) = await async_release_check_store(self.hass).async_get(
                self._entry.entry_id
            )
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
            # On the entry, so that unloading the receiver takes the request with it
            # rather than leaving it to finish against an entity that is already gone.
            self._entry.async_create_background_task(
                self.hass,
                self._async_check_release(),
                name=f"{DOMAIN} release check {self._entry.entry_id}",
            )
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
        there. Which of the two cases a household is looking at - and, when the button
        is missing, how to get it - is not deducible from two version strings.
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

        # Home Assistant cuts a summary at 255 characters, and where it cuts is where
        # the 255th character happens to fall - mid-word, and in a language with long
        # compounds usually mid-sentence. The sentences here are in priority order: the
        # first says what the version numbers mean and what to do, the second is extra
        # context about a published tag. A second sentence that will not fit whole is
        # dropped whole, because half of it reads as a bug rather than as a summary.
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

    async def _async_check_release(self, now: datetime | None = None) -> None:
        """Ask the plugin repository what it last published. Opt-in, daily, silent.

        Every failure is a debug line and nothing else. This is a convenience nobody
        asked to be told about, running against a service with a rate limit shared by
        everything else on the same address, and a repair notification about GitHub on a
        receiver's device page would be noise about a problem that is not the receiver's.

        Only reached on an entity built while the option was on: the timer below is not
        registered otherwise, and changing the option reloads the entry. The interval
        sets the cadence and the stored stamp enforces it, because a timer that fires
        early - or a reload, or a restart - must not turn one day's request into two.
        """
        del now
        if (
            self._last_release_check is not None
            and dt_util.utcnow() - self._last_release_check < RELEASE_CHECK_INTERVAL
        ):
            return
        # Stamped, and written to storage, before the request rather than after it. A
        # check that times out has still spent this day's request; a stamp that only
        # lives in this entity is spent again by the next reload; and retrying on every
        # state change would turn one unreachable service into a loop. The answer stored
        # alongside it is the last one that worked, so a failure costs the stamp and
        # leaves the card saying what it already knew.
        checked = self._last_release_check = dt_util.utcnow()
        await self._async_remember(checked)
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
                # Read in chunks with a ceiling rather than in one call: `read(n)` on a
                # live stream may return less than asked for, so a body that is merely
                # slow would look truncated, and a body with no end at all would
                # otherwise be pulled into memory in full before anything noticed.
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > RELEASE_BODY_LIMIT:
                        _LOGGER.debug(
                            "The plugin release check answered with more than %s bytes",
                            RELEASE_BODY_LIMIT,
                        )
                        return
            payload = json.loads(bytes(body))
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("The plugin release check did not answer: %s", err)
            return

        version = published_release_version(
            payload.get("tag_name") if isinstance(payload, dict) else None
        )
        if version is None:
            _LOGGER.debug("The plugin release check returned no usable tag")
            return
        self._published_version = version
        self._published_url = published_release_url(payload.get("html_url"))
        # Written against the time of the request, not of the answer: the stamp is what
        # was spent, and a slow reply must not buy back part of the day.
        await self._async_remember(checked)
        self._async_read_state()
        self.async_write_ha_state()

    async def _async_remember(self, checked: datetime) -> None:
        """Put the stamp and the answer where a reload or a restart will find them."""
        await async_release_check_store(self.hass).async_set(
            self._entry.entry_id,
            checked,
            self._published_version,
            self._published_url,
        )

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

"""The receiver as a media player.

This is the entity a household actually uses, so it is the one that carries the
compromises.

**It is never unavailable.** Every other entity of a box goes unavailable when the box
does, because saying nothing is better than saying something stale. This one stays, for
one reason: a box in deep standby is off the network, and the only way to wake it is a
magic packet — which a user cannot send from an entity the dashboard has greyed out.

**Standby is reported as `off`.** `MediaPlayerState.STANDBY` was deprecated in Home
Assistant 2026.8 in favour of `off` or `idle`, and the difference between a box in
standby and a box in deep standby is not worth a state that is being removed. What the
household sees is the same either way — a dark television — and the „Zasilanie" switch
and the device's availability still tell the two apart for anyone who needs it.

**The picture is the screenshot.** `screen` is a retained JPEG of what is on the
television, so it is both the media image and the entity picture, refreshed whenever
the plugin captures a new one.
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
import time
from typing import Any

from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
import homeassistant.util.dt as dt_util

from .box import Enigma2Box, Enigma2MqttConfigEntry, async_send_magic_packet
from .const import (
    DOMAIN,
    TOPIC_CHANNELS,
    TOPIC_EPG,
    TOPIC_POWER,
    TOPIC_SCREEN,
    TOPIC_SERVICE,
    TOPIC_VOLUME,
)
from .entity import Enigma2Entity
from .services import Enigma2Actions, async_setup_services

PARALLEL_UPDATES = 0

# What `play_media` accepts besides a service reference, so that an automation can name
# a channel the way a person would.
MEDIA_TYPE_CHANNEL_NAME = "channel_name"

# The media browser's root. Anything else is `bouquet:<service reference>`.
BROWSE_ROOT = "bouquets"
BROWSE_BOUQUET_PREFIX = "bouquet:"

# How much one press of the volume buttons moves it.
VOLUME_STEP = 0.05


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the media player, and the actions that target it."""
    async_setup_services()
    async_add_entities([Enigma2MediaPlayer(entry.runtime_data)])


class Enigma2MediaPlayer(Enigma2Entity, Enigma2Actions, MediaPlayerEntity):
    """One Enigma2 receiver, as something to watch — and every action's target."""

    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_media_content_type = MediaType.CHANNEL
    _attr_name = None
    _attr_volume_step = VOLUME_STEP
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
    )

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the player on every topic that changes what it shows."""
        super().__init__(
            box,
            "media_player",
            topics=(
                TOPIC_POWER,
                TOPIC_SERVICE,
                TOPIC_EPG,
                TOPIC_VOLUME,
                TOPIC_SCREEN,
                TOPIC_CHANNELS,
            ),
            requires=None,
        )
        # The player is named after its device, so it has no name of its own to
        # translate; the translation key would only make `hassfest` look for one.
        self._attr_translation_key = None

    @property
    def available(self) -> bool:
        """Return True, always. See the module docstring: this is the wake button."""
        return True

    # --------------------------------------------------------------------- state

    @callback
    def _async_read_state(self) -> None:
        """Recompute everything the player shows from the box's last payloads."""
        state = self.box.state
        service = state.service or {}
        epg = state.epg or {}
        now = epg.get("now") if isinstance(epg.get("now"), dict) else {}
        volume = state.volume or {}

        self._attr_state = (
            MediaPlayerState.PLAYING if self.box.is_on else MediaPlayerState.OFF
        )

        self._attr_source = service.get("name")
        self._attr_media_channel = service.get("name")
        self._attr_media_content_id = service.get("sref")
        self._attr_media_title = now.get("title")
        self._attr_media_series_title = now.get("short") or None

        begin = now.get("begin")
        end = now.get("end")
        if isinstance(begin, int) and isinstance(end, int) and end > begin:
            self._attr_media_duration = end - begin
            self._attr_media_position = max(0, min(end - begin, int(time.time()) - begin))
            self._attr_media_position_updated_at = dt_util.utcnow()
        else:
            self._attr_media_duration = None
            self._attr_media_position = None
            self._attr_media_position_updated_at = None

        level = volume.get("level")
        self._attr_volume_level = (
            max(0.0, min(1.0, level / 100)) if isinstance(level, (int, float)) else None
        )
        muted = volume.get("muted")
        self._attr_is_volume_muted = muted if isinstance(muted, bool) else None

        # A new frame has to change the hash, or the frontend keeps the cached picture
        # for as long as the entity lives.
        self._attr_media_image_hash = (
            hashlib.sha256(state.screen).hexdigest()[:16] if state.screen else None
        )

    @property
    def source_list(self) -> list[str] | None:
        """Return the channels of the bouquets the options selected."""
        return self.box.channel_names or None

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Return the last screenshot as the picture of what is playing."""
        if (screen := self.box.state.screen) is None:
            return None, None
        return screen, "image/jpeg"

    # ------------------------------------------------------------------ commands

    async def async_turn_on(self) -> None:
        """Wake the box: over MQTT if it is listening, with a magic packet if not."""
        if self.box.available:
            await self.box.async_publish_cmd("power", "on")
            return
        await async_send_magic_packet(self.box)

    async def async_turn_off(self) -> None:
        """Put the box into standby."""
        await self.box.async_publish_cmd("power", "standby")

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume, on the box's 0-100 scale."""
        await self.box.async_publish_cmd("volume", str(round(volume * 100)))

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute."""
        await self.box.async_publish_cmd("mute", "ON" if mute else "OFF")

    async def async_media_next_track(self) -> None:
        """Zap one channel up the current bouquet."""
        await self.box.async_publish_cmd("key", "KEY_CHANNELUP")

    async def async_media_previous_track(self) -> None:
        """Zap one channel down the current bouquet."""
        await self.box.async_publish_cmd("key", "KEY_CHANNELDOWN")

    async def async_select_source(self, source: str) -> None:
        """Zap to a channel by the name the bouquet gives it."""
        await async_zap_to_name(self.box, source)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Zap to a service reference, or to a channel name."""
        if media_type in (MediaType.CHANNEL, MediaType.CHANNELS):
            await self.box.async_publish_cmd("zap", media_id)
            return
        if media_type == MEDIA_TYPE_CHANNEL_NAME:
            await async_zap_to_name(self.box, media_id)
            return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_media_type",
            translation_placeholders={"media_type": str(media_type)},
        )

    # ------------------------------------------------------------------- browsing

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Walk the bouquets the box published, then the channels in one of them."""
        bouquets = self.box.bouquets
        if not bouquets:
            raise BrowseError(
                "This receiver has not published its channel list yet. Check that the "
                "plugin is running and that at least one bouquet is selected on its "
                "setup screen."
            )

        if media_content_id and media_content_id.startswith(BROWSE_BOUQUET_PREFIX):
            sref = media_content_id[len(BROWSE_BOUQUET_PREFIX) :]
            if (bouquet := self.box.bouquet_by_sref(sref)) is None:
                raise BrowseError(f"Unknown bouquet {sref}")
            return self._browse_bouquet(bouquet)

        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=BROWSE_ROOT,
            media_content_type=MediaType.CHANNELS,
            title=self.box.announcement.get("name") or self.box.name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=[
                BrowseMedia(
                    media_class=MediaClass.DIRECTORY,
                    media_content_id=f"{BROWSE_BOUQUET_PREFIX}{bouquet.get('sref', '')}",
                    media_content_type=MediaType.CHANNELS,
                    title=bouquet.get("name") or "",
                    can_play=False,
                    can_expand=True,
                )
                for bouquet in bouquets
            ],
        )

    def _browse_bouquet(self, bouquet: dict[str, Any]) -> BrowseMedia:
        """Return one bouquet as a list of playable channels."""
        children: Sequence[BrowseMedia] = [
            BrowseMedia(
                media_class=MediaClass.CHANNEL,
                media_content_id=channel.get("sref") or "",
                media_content_type=MediaType.CHANNEL,
                title=channel.get("name") or "",
                can_play=True,
                can_expand=False,
                thumbnail=self.box.picon_url(channel.get("sref")),
            )
            for channel in bouquet["channels"]
            if isinstance(channel, dict) and channel.get("sref")
        ]
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=f"{BROWSE_BOUQUET_PREFIX}{bouquet.get('sref', '')}",
            media_content_type=MediaType.CHANNELS,
            title=bouquet.get("name") or "",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.CHANNEL,
            children=children,
        )


async def async_zap_to_name(box: Enigma2Box, name: str) -> None:
    """Zap to a channel by name, falling back to its service reference.

    The plugin refuses a zap by name unless exactly one service in its configured
    bouquets matches, and it is right to: two channels called „TVP 1 HD" are two
    different transponders and guessing between them is worse than refusing. But a
    household picking a name out of a list has already made the choice the plugin is
    refusing to make, so a name that is ambiguous here is sent as the reference of the
    first match instead — the one that was on the list they picked from.
    """
    matches = box.channels_named(name)
    if not matches:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_channel",
            translation_placeholders={"channel": name},
        )
    if len(matches) == 1:
        await box.async_publish_cmd("zap", json.dumps({"name": name}))
        return
    await box.async_publish_cmd("zap", matches[0].get("sref") or "")

"""The receivers' channels as a media source: receivers, bouquets, channels.

A channel resolves to the address the receiver's own stream server serves it at, as an
MPEG transport stream. That is something a player has to decode itself - Kodi, VLC, an
Android TV player - so this is for those players. Nothing is transcoded and nothing
passes through Home Assistant: the player fetches the stream from the receiver.

The tree is built from the channel list the integration already holds
(`Enigma2Box.streamable_bouquets`) and adds no copy of it and no request for it. What
the options keep out is kept out here too: a bouquet that is not offered, a hidden
bouquet, and a channel of a hidden bouquet wherever it is listed. A channel resolves
only while it is in that tree, so the source cannot be asked about a channel it does
not show.

Two players cannot play a transport stream and are told so when a channel is resolved
for them: the browser's own player, which Home Assistant resolves for without naming a
media player, and Chromecast. Browsing cannot know which player is asking, so the tree
is shown to every one of them.

An identifier is `<node_id>`, `<node_id>/b/<bouquet reference>` or
`<node_id>/c/<service reference>`, the reference percent-encoded with its colons kept.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, unquote

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .box import Enigma2Box, same_service
from .const import (
    CAPABILITY_STREAM,
    CAPABILITY_STREAM_PLAYABLE,
    DOMAIN,
    STREAM_MIME_TYPE,
)
from .media_player import channel_thumbnail_lookup

KIND_BOUQUET = "b"
KIND_CHANNEL = "c"

# The platforms whose players cannot decode a transport stream, by the name their
# entities are registered under.
PLATFORMS_WITHOUT_MPEG_TS = frozenset({"cast"})


async def async_get_media_source(hass: HomeAssistant) -> Enigma2MediaSource:
    """Set up the media source."""
    return Enigma2MediaSource(hass)


def _identifier(node_id: str, kind: str | None = None, reference: str = "") -> str:
    """Return the identifier of a receiver, or of one of its bouquets or channels."""
    if kind is None:
        return node_id
    return f"{node_id}/{kind}/{quote(reference, safe=':')}"


def _parse(identifier: str | None) -> tuple[str, str | None, str]:
    """Return the node id, the kind and the reference an identifier names."""
    node_id, _, rest = (identifier or "").partition("/")
    kind, _, reference = rest.partition("/")
    return node_id, kind or None, unquote(reference)


def _unknown(error: type[BrowseError] | type[Unresolvable]) -> BrowseError | Unresolvable:
    """Return the error for a receiver, bouquet or channel that is not in the tree."""
    return error(translation_domain=DOMAIN, translation_key="stream_source_unknown")


class Enigma2MediaSource(MediaSource):
    """The channels of every receiver that says it has a stream server."""

    name = "Enigma2 MQTT"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the source."""
        super().__init__(DOMAIN)
        self.hass = hass

    def _boxes(self) -> list[Enigma2Box]:
        """Return the receivers that claim `stream`, in the order they were added."""
        return [
            entry.runtime_data
            for entry in self.hass.config_entries.async_loaded_entries(DOMAIN)
            if CAPABILITY_STREAM in entry.runtime_data.capabilities
        ]

    def _box(self, node_id: str) -> Enigma2Box | None:
        """Return one of those receivers by its node id."""
        return next((box for box in self._boxes() if box.node_id == node_id), None)

    # ------------------------------------------------------------------ browse

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Return the receivers, one receiver's bouquets, or one bouquet's channels."""
        node_id, kind, reference = _parse(item.identifier)
        if not node_id:
            return self._browse_root()
        if (box := self._box(node_id)) is None:
            raise _unknown(BrowseError)
        if kind is None:
            return self._browse_receiver(box)
        if kind == KIND_BOUQUET:
            for bouquet, channels in box.streamable_bouquets():
                if bouquet.get("sref") == reference:
                    return self._browse_bouquet(box, bouquet, channels)
        raise _unknown(BrowseError)

    def _browse_root(self) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.CHANNELS,
            title=self.name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=[self._receiver(box) for box in self._boxes()],
        )

    def _receiver(self, box: Enigma2Box) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=_identifier(box.node_id),
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.CHANNELS,
            title=box.announcement.get("name") or box.name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _bouquet(self, box: Enigma2Box, bouquet: dict[str, Any]) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=_identifier(box.node_id, KIND_BOUQUET, bouquet.get("sref") or ""),
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.CHANNELS,
            title=bouquet.get("name") or "",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.CHANNEL,
        )

    def _browse_receiver(self, box: Enigma2Box) -> BrowseMediaSource:
        receiver = self._receiver(box)
        receiver.children = [
            self._bouquet(box, bouquet) for bouquet, _channels in box.streamable_bouquets()
        ]
        return receiver

    def _browse_bouquet(
        self, box: Enigma2Box, bouquet: dict[str, Any], channels: list[dict[str, Any]]
    ) -> BrowseMediaSource:
        thumbnail = channel_thumbnail_lookup(box, bouquet)
        listing = self._bouquet(box, bouquet)
        listing.children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=_identifier(box.node_id, KIND_CHANNEL, channel["sref"]),
                media_class=MediaClass.CHANNEL,
                media_content_type=STREAM_MIME_TYPE,
                title=channel.get("name") or "",
                can_play=True,
                can_expand=False,
                thumbnail=thumbnail(channel["sref"]),
            )
            for channel in channels
        ]
        return listing

    # ----------------------------------------------------------------- resolve

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Return the receiver's stream address for one channel of the tree.

        Refused, with a sentence in the household's language: a channel that is not in
        the tree; a player that cannot play a transport stream; a stream server that
        wants a login; no address to reach the receiver at; and a channel the receiver
        says it cannot play now, beside everything it is doing - asked just before the
        address is handed out, where the receiver can be asked. No answer to that
        question in time is not a "no": the address is handed out and the player finds
        out.
        """
        node_id, kind, reference = _parse(item.identifier)
        box = self._box(node_id)
        if box is None or kind != KIND_CHANNEL or not reference:
            raise _unknown(Unresolvable)
        found = next(
            (
                (bouquet, channel)
                for bouquet, channels in box.streamable_bouquets()
                for channel in channels
                if same_service(channel["sref"], reference)
            ),
            None,
        )
        if found is None:
            raise _unknown(Unresolvable)
        bouquet, channel = found

        self._check_player(item.target_media_player)

        if (box.state.stream or {}).get("authentication") is True:
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="stream_login_required"
            )
        url = box.stream_url_for(channel["sref"], bouquet.get("name"))
        if url is None:
            raise Unresolvable(translation_domain=DOMAIN, translation_key="stream_no_address")

        if (
            CAPABILITY_STREAM_PLAYABLE in box.capabilities
            and box.available
            and await box.async_stream_playable(channel["sref"]) is False
        ):
            raise Unresolvable(
                translation_domain=DOMAIN,
                translation_key="stream_not_playable",
                translation_placeholders={"channel": channel.get("name") or reference},
            )
        return PlayMedia(url, STREAM_MIME_TYPE)

    def _check_player(self, entity_id: str | None) -> None:
        """Refuse a player that cannot decode a transport stream.

        Home Assistant resolves without a media player only for the browser's own
        player, which plays what a browser plays. Chromecast is told by the platform
        its entity is registered under. Every other player is taken at its word.
        """
        if entity_id is None:
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="stream_player_browser"
            )
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is not None and entry.platform in PLATFORMS_WITHOUT_MPEG_TS:
            raise Unresolvable(translation_domain=DOMAIN, translation_key="stream_player_cast")

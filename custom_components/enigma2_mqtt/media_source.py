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

**The recordings are a branch of their own**, and it is served here but offered only by the
receiver's own media player. A recording is played by the receiver, on the television, in
the image's own movie player - nothing is streamed anywhere - so the branch is something
only that one player can use. Home Assistant browses a media source without saying which
player asks, so the branch is not listed under a receiver here at all: the receiver's media
player puts it beside its bouquets, and every other player never sees it. Its identifiers
are `<node_id>/r` (the library), `<node_id>/l/<location path>` (one movie location, the
path percent-encoded), `<node_id>/f/<folder slug>` (one folder) and `<node_id>/p/<id>` (one
recording, by its id). Resolving a recording for any player is refused with a sentence:
the receiver's media player does not resolve it, it sends `cmd/play_recording`.

The tree is the receiver's library as its topics list it - every movie location with its
folders, the receiver's trash and the recordings of hidden bouquets left out there - and a
folder the receiver cut to fit one packet says how many it left out. With one movie
location its folders are listed directly under "Recordings".
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
from homeassistant.helpers.translation import async_get_translations

from .box import Enigma2Box, same_service
from .const import (
    CAPABILITY_RECORDING_PLAY,
    CAPABILITY_RECORDINGS,
    CAPABILITY_STREAM,
    CAPABILITY_STREAM_PLAYABLE,
    DOMAIN,
    MEDIA_TYPE_RECORDING,
    STREAM_MIME_TYPE,
)
from .media_player import channel_thumbnail_lookup
from .recordings import folder_title, parse_recording_id, recording_title
from .stream_facts import one_playable_service

KIND_BOUQUET = "b"
KIND_CHANNEL = "c"
KIND_RECORDINGS = "r"
KIND_LOCATION = "l"
KIND_FOLDER = "f"
KIND_RECORDING = "p"
RECORDING_KINDS = frozenset({KIND_RECORDINGS, KIND_LOCATION, KIND_FOLDER, KIND_RECORDING})
# What the library is called where no translation is at hand.
RECORDINGS_TITLE = "Recordings"

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
    if kind == KIND_RECORDINGS:
        return f"{node_id}/{kind}"
    if kind == KIND_LOCATION:
        # A path has slashes, and the identifier is split at them.
        return f"{node_id}/{kind}/{quote(reference, safe='')}"
    return f"{node_id}/{kind}/{quote(reference, safe=':')}"


def _parse(identifier: str | None) -> tuple[str, str | None, str]:
    """Return the node id, the kind and the reference an identifier names."""
    node_id, _, rest = (identifier or "").partition("/")
    kind, _, reference = rest.partition("/")
    return node_id, kind or None, unquote(reference)


MEDIA_SOURCE_PREFIX = f"media-source://{DOMAIN}/"


def parse_media_id(media_id: str) -> tuple[str, str | None, str] | None:
    """Return what a `media-source://enigma2_mqtt/...` id names, or None for another id."""
    if not media_id.startswith(MEDIA_SOURCE_PREFIX):
        return None
    return _parse(media_id[len(MEDIA_SOURCE_PREFIX) :])


def recording_id_from_media(media_id: str) -> str | None:
    """Return the recording id a media id names, for one of this receiver's recordings.

    Two spellings: the bare id an automation passes with the `recording` type, and the
    media source id the media browser hands over. The node in the second is checked by
    the caller.
    """
    if (parsed := parse_media_id(media_id)) is None:
        return parse_recording_id(media_id)
    _node, kind, reference = parsed
    return parse_recording_id(reference) if kind == KIND_RECORDING else None


async def async_recordings_title(hass: HomeAssistant) -> str:
    """Return what the library is called in the installation's language."""
    translations = await async_get_translations(
        hass, hass.config.language, "common", {DOMAIN}
    )
    return translations.get(f"component.{DOMAIN}.common.media_recordings") or RECORDINGS_TITLE


def recordings_entry(box: Enigma2Box, title: str) -> BrowseMediaSource:
    """Return the "Recordings" directory of one receiver, without its children."""
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=_identifier(box.node_id, KIND_RECORDINGS),
        media_class=MediaClass.DIRECTORY,
        media_content_type=MediaType.VIDEO,
        title=title,
        can_play=False,
        can_expand=True,
        children_media_class=MediaClass.DIRECTORY,
    )


def _location_entry(box: Enigma2Box, location: dict[str, Any]) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=_identifier(box.node_id, KIND_LOCATION, location["path"]),
        media_class=MediaClass.DIRECTORY,
        media_content_type=MediaType.VIDEO,
        title=location["path"],
        can_play=False,
        can_expand=True,
        children_media_class=MediaClass.DIRECTORY,
    )


def _folder_entry(
    box: Enigma2Box, location: dict[str, Any], folder: dict[str, Any]
) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=_identifier(box.node_id, KIND_FOLDER, folder["slug"]),
        media_class=MediaClass.DIRECTORY,
        media_content_type=MediaType.VIDEO,
        title=folder_title(location, folder),
        can_play=False,
        can_expand=True,
        children_media_class=MediaClass.VIDEO,
    )


def browse_recordings(
    box: Enigma2Box, kind: str, reference: str, title: str
) -> BrowseMediaSource:
    """Return one level of a receiver's recording library: itself, a location, a folder.

    Raises `BrowseError` for a level the library does not list - a location or a folder
    that has gone, or a receiver that does not claim `recordings`.
    """
    if CAPABILITY_RECORDINGS not in box.capabilities:
        raise _unknown(BrowseError)
    locations = box.recording_locations
    if kind == KIND_RECORDINGS:
        listing = recordings_entry(box, title)
        if len(locations) == 1:
            listing.children = [
                _folder_entry(box, locations[0], folder) for folder in locations[0]["folders"]
            ]
        else:
            listing.children = [_location_entry(box, location) for location in locations]
        return listing
    if kind == KIND_LOCATION:
        for location in locations:
            if location["path"] == reference:
                listing = _location_entry(box, location)
                listing.children = [
                    _folder_entry(box, location, folder) for folder in location["folders"]
                ]
                return listing
        raise _unknown(BrowseError)
    if kind == KIND_FOLDER and (found := box.recording_folder(reference)) is not None:
        location, folder = found
        playable = CAPABILITY_RECORDING_PLAY in box.capabilities
        listing = _folder_entry(box, location, folder)
        listing.children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=_identifier(box.node_id, KIND_RECORDING, recording["id"]),
                media_class=MediaClass.VIDEO,
                media_content_type=MEDIA_TYPE_RECORDING,
                title=recording_title(recording),
                can_play=playable,
                can_expand=False,
            )
            for recording in box.recordings_in(folder["slug"])
        ]
        # The recordings the receiver left out of this folder to keep its topic inside one
        # packet: the oldest ones, which the browser counts below the list.
        listing.not_shown = folder["cut"] or 0
        return listing
    raise _unknown(BrowseError)


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

    def _library_box(self, node_id: str) -> Enigma2Box | None:
        """Return a receiver that claims `recordings`, whether or not it claims `stream`."""
        return next(
            (
                entry.runtime_data
                for entry in self.hass.config_entries.async_loaded_entries(DOMAIN)
                if entry.runtime_data.node_id == node_id
                and CAPABILITY_RECORDINGS in entry.runtime_data.capabilities
            ),
            None,
        )

    # ------------------------------------------------------------------ browse

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Return the receivers, one receiver's bouquets, or one bouquet's channels."""
        node_id, kind, reference = _parse(item.identifier)
        if not node_id:
            return self._browse_root()
        if kind in RECORDING_KINDS - {KIND_RECORDING}:
            if (library := self._library_box(node_id)) is None:
                raise _unknown(BrowseError)
            return browse_recordings(
                library, kind, reference, await async_recordings_title(self.hass)
            )
        if (box := self._box(node_id)) is None:
            raise _unknown(BrowseError)
        if kind is None:
            return self._browse_receiver(box)
        if kind == KIND_BOUQUET:
            for bouquet in box.streamable_bouquets():
                if bouquet.get("sref") == reference:
                    return self._browse_bouquet(
                        box, bouquet, box.streamable_channels(bouquet)
                    )
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
        receiver.children = [self._bouquet(box, bouquet) for bouquet in box.streamable_bouquets()]
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
        the tree; a player that cannot play a transport stream; no usable `stream`
        facts from the receiver; a stream server that wants a login; no address to
        reach the receiver at; and a channel the receiver
        says it cannot play now, beside everything it is doing - asked just before the
        address is handed out, where the receiver can be asked. No answer to that
        question in time is not a "no": the address is handed out and the player finds
        out.
        """
        node_id, kind, reference = _parse(item.identifier)
        if kind == KIND_RECORDING:
            # Played by the receiver itself, never handed to a player as an address: the
            # receiver's media player sends the command and does not come here.
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="recording_plays_on_receiver"
            )
        box = self._box(node_id)
        if box is None or kind != KIND_CHANNEL or not reference:
            raise _unknown(Unresolvable)
        # The first bouquet, in the list's order, that shows the channel: a channel is
        # judged in the bouquet it is found in, and only there.
        found = next(
            (
                (bouquet, channel)
                for bouquet in box.streamable_bouquets()
                for channel in bouquet["channels"]
                if isinstance(channel, dict)
                and same_service(channel.get("sref"), reference)
                and box.channel_streamable(bouquet, channel)
            ),
            None,
        )
        if found is None:
            raise _unknown(Unresolvable)
        bouquet, channel = found

        self._check_player(item.target_media_player)

        stream = box.state.stream
        if stream is None or stream["port"] is None or stream["path"] is None:
            # Not an address problem: the receiver has said nothing usable about its
            # stream server yet, or retracted it.
            raise Unresolvable(translation_domain=DOMAIN, translation_key="stream_no_facts")
        if stream["authentication"] is True:
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="stream_login_required"
            )
        url = box.stream_url_for(channel["sref"], bouquet.get("name"))
        if url is None:
            raise Unresolvable(translation_domain=DOMAIN, translation_key="stream_no_address")

        # Asked only about one playable service: the receiver refuses a bouquet, a
        # marker, a service with alternatives and a hidden entry on `last_error`, where
        # the household would read the refusal as a fault. The channel list holds none
        # of those, so this only keeps a list that broke the contract from making one.
        if (
            CAPABILITY_STREAM_PLAYABLE in box.capabilities
            and box.available
            and one_playable_service(channel["sref"])
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

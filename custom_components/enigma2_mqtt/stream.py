"""What a receiver says about streaming and about its audio, read in one place.

A plugin after 0.4.0 publishes what is on the decoder (the codec members of `service`),
whether the volume acts on the audio being played (`volume.passthrough`), where its
stream server is (`stream`) and who it is serving (`stream_clients`). Every reader of
those payloads is in this module, one per topic, so that a change of shape on the
receiver's side is a change here and nowhere else.

The readers follow the consumer's half of the contract: a member this does not know is
ignored, a member that is missing or of another type is unknown - `None` - and a value
of an open enumeration is passed on as it is. None of them raises: they run on the
callback that feeds every topic.
"""

from __future__ import annotations

import math
from typing import Any

# The most audio tracks the plugin lists for one service, and the most characters of a
# track's description or language. Bounded here as well: the payload arrives from a
# broker, and the list becomes a state attribute.
AUDIO_TRACKS_MAX = 32
TRACK_TEXT_MAX = 64


def _text(value: Any, limit: int = TRACK_TEXT_MAX) -> str | None:
    """Return a non-empty string, cut to a length, or None."""
    return value[:limit] if isinstance(value, str) and value else None


def _whole(value: Any) -> int | None:
    """Return a whole number that is not negative, or None. `True` is not a number."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# ------------------------------------------------------------------- `service`


def video_codec(service: dict[str, Any] | None) -> str | None:
    """Return the video codec on the decoder, as the receiver names it.

    An open enumeration (`h264`, `hevc`, `mpeg2`, ...): a name a later plugin adds is
    shown as it is. `null` is an image that cannot say, or a stream type it has no
    name for.
    """
    return _text((service or {}).get("video_codec"))


def interlaced(service: dict[str, Any] | None) -> bool | None:
    """Return whether the picture is interlaced; None before the decoder knows."""
    value = (service or {}).get("interlaced")
    return value if isinstance(value, bool) else None


def frame_rate(service: dict[str, Any] | None) -> float | None:
    """Return frames a second, to three decimal places, or None.

    For an interlaced picture this is the frame rate, not the field rate, as the
    receiver publishes it. Zero, a negative number and anything that is not a finite
    number are not rates.
    """
    value = (service or {}).get("frame_rate")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return round(float(value), 3)


def audio_tracks(service: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Return the audio tracks of the service, normalised, or None when not stated.

    `[]` is a service without audio; None is nothing playing, an image that will not
    say, or a plugin without the member. Each track keeps the six members of the
    contract and nothing else; an entry that is not an object is left out.
    """
    raw = (service or {}).get("audio_tracks")
    if not isinstance(raw, list):
        return None
    tracks: list[dict[str, Any]] = []
    for item in raw[:AUDIO_TRACKS_MAX]:
        if not isinstance(item, dict):
            continue
        tracks.append(
            {
                "index": _whole(item.get("index")),
                "description": _text(item.get("description")),
                "language": _text(item.get("language")),
                "pid": _whole(item.get("pid")),
                "codec": _text(item.get("codec")),
                "active": item.get("active") is True,
            }
        )
    return tracks


def active_audio_track(service: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the track being played, or None when the receiver names none.

    At most one track is active. None is active when the image does not say, or when
    the active track is past the ones listed.
    """
    for track in audio_tracks(service) or []:
        if track["active"]:
            return track
    return None


def audio_codec(service: dict[str, Any] | None) -> str | None:
    """Return the codec of the track being played: `mp2`, `ac3`, `eac3`, `aac`, ..."""
    return (active_audio_track(service) or {}).get("codec")


def audio_language(service: dict[str, Any] | None) -> str | None:
    """Return the language of the track being played, as the image gives it."""
    return (active_audio_track(service) or {}).get("language")


def audio_attributes(service: dict[str, Any] | None) -> dict[str, Any]:
    """Return what explains the audio codec: the image's own word, and every track."""
    return {
        "description": (active_audio_track(service) or {}).get("description"),
        "audio_tracks": audio_tracks(service),
    }


# -------------------------------------------------------------------- `volume`


def volume_passthrough(volume: dict[str, Any] | None) -> bool | None:
    """Return whether the playing track is handed on undecoded, or None for unknown.

    True: the receiver's volume and mute do nothing that can be heard now. False: the
    receiver decodes the track and its volume acts on it. None: it cannot be told -
    nothing is playing, the codec is unknown, the image has no setting for it - or the
    plugin publishes no such member. Unknown is never read as "no".
    """
    value = (volume or {}).get("passthrough")
    return value if isinstance(value, bool) else None

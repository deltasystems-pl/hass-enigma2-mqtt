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
import re
from typing import Any
from urllib.parse import quote

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


# ------------------------------------------------------------ `stream_clients`

# The most rows the plugin lists, and the most characters of a host, a name and a
# reference that are kept. A reference is never cut - a cut one names no service - so
# one that is too long is dropped.
STREAM_CLIENTS_MAX = 32
CLIENT_HOST_MAX = 64
CLIENT_NAME_MAX = 255
CLIENT_SREF_MAX = 2048


def normalise_stream_clients(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip a `stream_clients` payload to what the contract names.

    `count` is how many clients the image lists, which can be more than the rows: the
    plugin cuts the list at 32. A payload without a usable count is counted by its rows.
    A row is kept whenever it names a host; its other members are None when they are
    missing or of another type, and `transcoded` is None rather than False then.
    """
    raw = payload.get("clients")
    clients: list[dict[str, Any]] = []
    for item in (raw if isinstance(raw, list) else [])[:STREAM_CLIENTS_MAX]:
        if not isinstance(item, dict):
            continue
        if (host := _text(item.get("host"), CLIENT_HOST_MAX)) is None:
            continue
        sref = item.get("sref")
        transcoded = item.get("transcoded")
        clients.append(
            {
                "host": host,
                "sref": sref
                if isinstance(sref, str) and 0 < len(sref) <= CLIENT_SREF_MAX
                else None,
                "name": _text(item.get("name"), CLIENT_NAME_MAX),
                "bouquet": _text(item.get("bouquet"), CLIENT_NAME_MAX),
                "transcoded": transcoded if isinstance(transcoded, bool) else None,
            }
        )
    count = _whole(payload.get("count"))
    return {"count": len(clients) if count is None else count, "clients": clients}


def same_client_host(one: str, other: str) -> bool:
    """Return whether two spellings name the same stream client's address.

    The topic shows an IPv4 client as `192.0.2.20`, the image holds it as
    `::ffff:192.0.2.20`, and the plugin takes either; letters in an address are
    compared without regard to case.
    """

    def plain(host: str) -> str:
        host = host.strip().lower()
        return host[len("::ffff:") :] if host.startswith("::ffff:") else host

    return plain(one) == plain(other)


# -------------------------------------------------------------------- `stream`

# The form of the stream server's request path: a plain path with `{sref}` where the
# service reference goes, once. The payload arrives from a broker, so nothing that
# would end the path or start another part of an address is taken - no query, no
# fragment, no user name, no white space.
_PATH_PART = r"[A-Za-z0-9/._~-]*"
_STREAM_PATH = re.compile(rf"/{_PATH_PART}\{{sref\}}{_PATH_PART}")
SREF_PLACEHOLDER = "{sref}"


def _port(value: Any) -> int | None:
    """Return a TCP port, or None."""
    port = _whole(value)
    return port if port is not None and 1 <= port <= 65535 else None


def normalise_stream(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip a `stream` payload to the five members of the contract.

    `authentication` is True, False or None - None is an image without that setting,
    which is not a login. A port or a path that cannot be used is None, and no address
    is built without both.
    """
    path = payload.get("path")
    authentication = payload.get("authentication")
    encoder = payload.get("encoder")
    return {
        "port": _port(payload.get("port")),
        "path": path if isinstance(path, str) and _STREAM_PATH.fullmatch(path) else None,
        "authentication": authentication if isinstance(authentication, bool) else None,
        "encoder": encoder if isinstance(encoder, bool) else None,
        "encoder_port": _port(payload.get("encoder_port")),
    }


def build_stream_url(
    base_url: str | None, stream: dict[str, Any] | None, sref: Any
) -> str | None:
    """Return the address the receiver serves one service's transport stream at.

    `base_url` is the receiver's web address as Home Assistant asks it,
    `http://<host>/`; the port and the form of the path are the receiver's own
    (`normalise_stream`). The reference goes in as the receiver publishes it, colons
    and the trailing colon included, which is how its stream server reads it; every
    other character that is not plain is percent-encoded, because the server decodes
    the path - so the `%3a` inside an IPTV reference arrives as `%3a` again.

    None without an address, a port, a path or a reference. Whether the server wants a
    login is the caller's question: this never adds one.
    """
    if not base_url or not stream or not isinstance(sref, str) or not sref:
        return None
    port, path = stream.get("port"), stream.get("path")
    if port is None or path is None:
        return None
    filled = path.replace(SREF_PLACEHOLDER, quote(sref, safe=":"))
    return f"{base_url.rstrip('/')}:{port}{filled}"


_NAME_RUN = re.compile(r"[^a-z0-9]+")


def stream_name(sref: Any) -> str | None:
    """Return the stable name of one channel's stream, from its reference alone.

    Lower-cased, every run of characters other than `a`-`z` and `0`-`9` collapsed to
    one `_`, the ends stripped, and `ch_` in front:
    `1:0:19:283D:3FB:1:C00000:0:0:0:` is `ch_1_0_19_283d_3fb_1_c00000_0_0_0`. It is
    the transform existing go2rtc configurations already use for their per-channel
    streams, so it must not change. None for a reference with nothing left in it.
    """
    if not isinstance(sref, str):
        return None
    body = _NAME_RUN.sub("_", sref.lower()).strip("_")
    return f"ch_{body}" if body else None

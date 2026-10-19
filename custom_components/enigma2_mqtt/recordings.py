"""Every reader of the receiver's recordings: the running ones, timeshift, the library.

The plugin's half of these payloads is new, so the shapes may still move before they are
released. Everything that reads them is therefore here and nowhere else: the box keeps
what these functions return, and the entities, the media browser and the actions read the
box. A member renamed on the receiver is one line in this module.

Four payloads (the plugin's TOPICS.md, `recording`, `timeshift`, `recordings` and
`recordings/<folder_slug>`), and one rule for all of them: a payload arrives from a box and
is not trusted to be what the contract says. Every value that cannot be believed is
`None`, every list has a bound, and nothing the contract does not name survives - a
recording's title, its channel and its file name end up in state attributes, the media
browser and the diagnostics download, so a member that grew on the receiver must not be
carried along with them.

**A recording is addressed by its id**, the first sixteen hexadecimal digits of the
sha256 of its path on the receiver. The receiver acts only on an id its own library
lists, and Home Assistant sends nothing else: no command here names a file.

**The receiver lists every recording; hiding is done here.** Each recording carries the
service it was recorded from and the first published bouquet that holds it, exactly as
`service` does, and every list Home Assistant shows is filtered by the hidden-bouquets
option through `Enigma2Box.service_hidden` - the rule the zap history, the stream
clients and the media source already follow, failing toward hiding. The functions here
take that rule as `hidden`; they never decide it themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import re
from typing import Any, Final
from urllib.parse import unquote

import homeassistant.util.dt as dt_util

# The shape of a recording's id. Lower case, because that is how the receiver writes it;
# what a person types is lowered before it is compared (`parse_recording_id`).
RECORDING_ID: Final = re.compile(r"[0-9a-f]{16}")

# Whether a service, named by its reference and the bouquet the receiver gives with it, is
# one the options hide (`Enigma2Box.service_hidden`).
type HiddenRule = Callable[[Any, Any], bool]

# The longest service reference kept, the bound the stream clients' rows use too; a longer one
# is no reference, and is then hidden whenever anything is.
SREF_MAX: Final = 512
# A short description is cut here on the receiver; a longer one is cut here too.
DESCRIPTION_MAX: Final = 200
# The longest title, channel name, file name or path that is kept. None of them is anywhere
# near this on a receiver; the bound only keeps a nonsense payload out of an attribute.
TEXT_MAX: Final = 255
PATH_MAX: Final = 1024
# How many movie locations, folders and recordings per folder are believed. A folder topic
# is bounded by the packet limit, which a few thousand recordings already reach.
LOCATIONS_MAX: Final = 64
FOLDERS_MAX: Final = 4096
RECORDINGS_MAX: Final = 20_000
# The furthest ahead a time is read as a clock: 2100-01-01T00:00:00Z.
EPOCH_MAX: Final = 4_102_444_800
# The longest recording believed, in seconds: a week.
DURATION_MAX: Final = 7 * 24 * 3600
# Why the library was last read, as the receiver says it. An open enumeration: anything
# else that is a short word is kept as it is, so a reason a later plugin adds is shown.
READ_REASONS: Final = ("start", "recording", "delete", "mount", "refresh")
REASON_MAX: Final = 32


def _text(value: Any, limit: int = TEXT_MAX) -> str | None:
    """Return a non-empty string cut at `limit`, or None."""
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _whole(value: Any, maximum: int | None = None) -> int | None:
    """Return a non-negative whole number, or None. `True` is not a number here."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _epoch(value: Any) -> int | None:
    """Return epoch seconds that can be a moment, or None. Zero is a field nobody filled."""
    value = _whole(value, EPOCH_MAX)
    return value or None


def _flag(value: Any) -> bool | None:
    """Return a boolean, or None for anything else."""
    return value if isinstance(value, bool) else None


def parse_recording_id(value: Any) -> str | None:
    """Return a recording id as the receiver writes it, or None when it cannot be one."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if RECORDING_ID.fullmatch(text) else None


def iso(value: Any) -> str | None:
    """Return epoch seconds as an ISO 8601 string in UTC, or None."""
    if (moment := _epoch(value)) is None:
        return None
    return dt_util.utc_from_timestamp(moment).isoformat()


# ---------------------------------------------------------------- running recordings


# What a running recording on a hidden service is shown without.
_ACTIVE_REVEALING = (
    "name",
    "sref",
    "bouquet",
    "service_name",
    "description",
    "location",
    "file",
    "id",
)


def active_recordings(
    recording: Any, *, details: bool, hidden: HiddenRule
) -> list[dict[str, Any]]:
    """Return the running recordings of a `recording` payload, normalised.

    The four members every plugin publishes - `name`, `sref`, `begin`, `end` - are kept as
    they always were, epoch seconds included, so nothing an automation reads today moves.
    `details` is whether the receiver claims `recording_details`: then every entry also has
    the seven members that capability adds, each `None` where the receiver gave nothing
    usable, so a template can read them without asking whether they are there. Without
    the capability they are left out altogether.

    Every entry also says `hidden`. One whose service the options hide keeps its times
    and `instant`, and loses everything that names the programme, the channel or the
    file - the rule the stream clients' rows follow. It is not left out: how many
    recordings run is what the count, the "Recording" sensor and every guard on the
    receiver are about, and a recording that holds a tuner is not hidden by a dashboard.

    Hiding is judged only for an entry that carries `bouquet`, which a plugin sends with
    the details. An older plugin's entry has none, and keeps exactly the attributes it
    always had: the rule could only ever say "hidden" about it, for every recording.
    """
    active = recording.get("active") if isinstance(recording, dict) else None
    rows: list[dict[str, Any]] = []
    for entry in active if isinstance(active, list) else []:
        if not isinstance(entry, dict):
            continue
        row: dict[str, Any] = {
            "name": entry.get("name"),
            "sref": entry.get("sref"),
            "begin": entry.get("begin"),
            "end": entry.get("end"),
        }
        if details:
            row.update(
                {
                    "bouquet": _text(entry.get("bouquet")),
                    "service_name": _text(entry.get("service_name")),
                    "description": _text(entry.get("description"), DESCRIPTION_MAX),
                    "location": _text(entry.get("location"), PATH_MAX),
                    "file": _text(entry.get("file"), PATH_MAX),
                    "id": parse_recording_id(entry.get("id")),
                    "instant": _flag(entry.get("instant")),
                }
            )
        concealed = "bouquet" in entry and hidden(entry.get("sref"), entry.get("bouquet"))
        if concealed:
            for key in _ACTIVE_REVEALING:
                if key in row:
                    row[key] = None
        row["hidden"] = concealed
        rows.append(row)
    return rows


def active_recording_ids(recording: Any) -> set[str]:
    """Return the library ids of the recordings running now, as far as the receiver says."""
    active = recording.get("active") if isinstance(recording, dict) else None
    entries = active if isinstance(active, list) else []
    return {
        rec_id
        for entry in entries
        if isinstance(entry, dict) and (rec_id := parse_recording_id(entry.get("id")))
    }


# ------------------------------------------------------------------------ timeshift


def normalise_timeshift(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip a `timeshift` payload to its four members.

    `active` false is no timeshift buffer, and then the other three say nothing whatever
    they hold: a buffer that is not running is neither paused, nor played from, nor
    started at any time. `behind` is whether the picture comes from the buffer rather
    than live.
    """
    active = _flag(payload.get("active"))
    if active is not True:
        return {"active": active, "paused": None, "behind": None, "since": None}
    return {
        "active": True,
        "paused": _flag(payload.get("paused")),
        "behind": _flag(payload.get("behind")),
        "since": _epoch(payload.get("since")),
    }


# ------------------------------------------------------------------- library index


def _slug(value: Any) -> str | None:
    """Return a folder slug that can be one topic level, or None."""
    if not isinstance(value, str) or not value or len(value) > TEXT_MAX:
        return None
    if any(character in value for character in "/+#") or value != value.strip():
        return None
    return value


def normalise_index(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip a `recordings` index to what the contract names, in the receiver's order.

    A location is kept when it has a path; a folder when it has a slug that can be one
    topic level and that no earlier folder of this receiver already owns - two folders
    cannot share one topic. Its `path` is relative to the location, `""` for the
    location's own top, and its numbers are whole or None: `count` is how many the folder
    topic lists, `cut` how many the receiver left out of it to keep it inside one packet.
    Both are the receiver's, hidden bouquets included: nothing here can filter a number.
    """
    reason = payload.get("reason")
    locations: list[dict[str, Any]] = []
    slugs: set[str] = set()
    raw_locations = payload.get("locations")
    for item in raw_locations if isinstance(raw_locations, list) else []:
        if len(locations) >= LOCATIONS_MAX:
            break
        if not isinstance(item, dict) or (path := _text(item.get("path"), PATH_MAX)) is None:
            continue
        folders: list[dict[str, Any]] = []
        raw_folders = item.get("folders")
        for folder in raw_folders if isinstance(raw_folders, list) else []:
            if len(slugs) >= FOLDERS_MAX:
                break
            if not isinstance(folder, dict) or (slug := _slug(folder.get("slug"))) is None:
                continue
            if slug in slugs:
                continue
            slugs.add(slug)
            relative = folder.get("path")
            folders.append(
                {
                    "slug": slug,
                    "path": relative[:PATH_MAX] if isinstance(relative, str) else "",
                    "count": _whole(folder.get("count")),
                    "size_mb": _whole(folder.get("size_mb")),
                    "cut": _whole(folder.get("cut")),
                }
            )
        locations.append(
            {
                "path": path,
                "mounted": _flag(item.get("mounted")),
                "free_mb": _whole(item.get("free_mb")),
                "folders": folders,
            }
        )
    return {
        "read": _epoch(payload.get("read")),
        "reason": reason
        if isinstance(reason, str) and reason and len(reason) <= REASON_MAX
        else None,
        "locations": locations,
    }


def visible(recordings: list[dict[str, Any]], hidden: HiddenRule) -> list[dict[str, Any]]:
    """Return the recordings of a list whose service the options do not hide."""
    return [
        recording
        for recording in recordings
        if not hidden(recording["sref"], recording["bouquet"])
    ]


# ----------------------------------------------------------------- folder topics


def normalise_folder(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Strip one `recordings/<folder_slug>` payload to its recordings, newest first.

    A recording is kept when it has an id and a file; an id the folder already listed
    is dropped, because it would name two recordings. The order is the receiver's, which
    is newest first. Hidden ones are kept here, with their `sref` and `bouquet`: what is
    hidden follows the option and the channel list, and is judged when a list is shown.
    """
    raw = payload.get("recordings")
    recordings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if len(recordings) >= RECORDINGS_MAX:
            break
        if not isinstance(item, dict):
            continue
        rec_id = parse_recording_id(item.get("id"))
        file = _text(item.get("file"), PATH_MAX)
        if rec_id is None or file is None or rec_id in seen:
            continue
        seen.add(rec_id)
        recordings.append(
            {
                "id": rec_id,
                "file": file,
                "name": _text(item.get("name")),
                "description": _text(item.get("description"), DESCRIPTION_MAX),
                "service_name": _text(item.get("service_name")),
                "sref": _text(item.get("sref"), SREF_MAX),
                "bouquet": _text(item.get("bouquet")),
                "begin": _epoch(item.get("begin")),
                "duration": _whole(item.get("duration"), DURATION_MAX),
                "size_mb": _whole(item.get("size_mb")),
            }
        )
    return recordings


def recording_path(location: dict[str, Any], recording: dict[str, Any]) -> str:
    """Return where a recording is on the receiver, as far as the payloads say.

    `file` is relative to its movie location, in a subfolder as well: `Series/x.ts`. Used to
    recognise the recording on `service` once it plays, and nothing else: no command
    carries a path.
    """
    return f"{location['path'].rstrip('/')}/{recording['file'].lstrip('/')}"


# How many colon-separated fields come before the path in a service reference.
_SREF_PATH_FIELD: Final = 10


def plays(service: Any, location: dict[str, Any], recording: dict[str, Any]) -> bool:
    """Return whether a `service` payload says this recording is what the decoder plays.

    A recording played from the movie list is a service whose reference carries the file's
    path in its eleventh field. The image writes that field percent-encoded - `:` as `%3a`,
    `%` as `%25`, a control character as its code - so a colon in a name can never end the
    field, and the field is decoded before it is compared with the location and the file. A
    name the image writes after the path, in a twelfth field, is not part of it.
    """
    sref = service.get("sref") if isinstance(service, dict) else None
    if not isinstance(sref, str) or not sref:
        return False
    fields = sref.split(":")
    if len(fields) <= _SREF_PATH_FIELD:
        return False
    return unquote(fields[_SREF_PATH_FIELD]) == recording_path(location, recording)


def library_response(
    index: dict[str, Any] | None,
    shown: list[tuple[dict[str, Any], list[tuple[dict[str, Any], list[dict[str, Any]]]]]],
) -> dict[str, Any]:
    """Return the library as `get_recordings` answers: what Home Assistant shows of it.

    `shown` is `Enigma2Box.visible_library()`: every location and folder with at least one
    recording the options do not hide, and only those recordings. Times are the epoch
    seconds the topics use, as `get_epg_grid` returns them. A recording's service reference
    and bouquet are not in it: they are what the hiding is judged by. A folder's `cut` is
    the receiver's own number, and counts the recordings of hidden bouquets it left out too.
    """
    return {
        "read": (index or {}).get("read"),
        "reason": (index or {}).get("reason"),
        "locations": [
            {
                "path": location["path"],
                "mounted": location["mounted"],
                "free_mb": location["free_mb"],
                "folders": [
                    {
                        "slug": folder["slug"],
                        "path": folder["path"],
                        "cut": folder["cut"],
                        "recordings": [
                            {
                                key: recording[key]
                                for key in (
                                    "id",
                                    "file",
                                    "name",
                                    "description",
                                    "service_name",
                                    "begin",
                                    "duration",
                                    "size_mb",
                                )
                            }
                            for recording in recordings
                        ],
                    }
                    for folder, recordings in folders
                ],
            }
            for location, folders in shown
        ],
    }


# --------------------------------------------------------------------- the browser


def folder_title(location: dict[str, Any], folder: dict[str, Any]) -> str:
    """Return the name a folder is shown under: its path, or the location's own name."""
    if folder["path"]:
        return folder["path"].strip("/") or folder["slug"]
    return location["path"].rstrip("/").rsplit("/", 1)[-1] or folder["slug"]


def recording_title(recording: dict[str, Any]) -> str:
    """Return what the media browser shows for one recording: title, channel, date, length.

    One line, because a browser row has one title: `Title - Channel - 2026-10-17 20:15
    (60 min)`, in Home Assistant's time zone, with whatever the receiver did not say
    left out. A recording with no title is shown by its file name.
    """
    parts = [recording["name"] or recording["file"].rsplit("/", 1)[-1]]
    if recording["service_name"]:
        parts.append(recording["service_name"])
    if recording["begin"] is not None:
        begin: datetime = dt_util.as_local(dt_util.utc_from_timestamp(recording["begin"]))
        parts.append(begin.strftime("%Y-%m-%d %H:%M"))
    title = " - ".join(parts)
    if recording["duration"]:
        title += f" ({max(1, round(recording['duration'] / 60))} min)"
    return title

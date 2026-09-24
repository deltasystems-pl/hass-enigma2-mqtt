"""Where the opt-in release check remembers what it last heard, and when.

The check is allowed one request a day, and an entity cannot enforce that by itself. It
is built again on every reload, on every options save and on every restart, so a limit
that lives in the entity is reset by all three: six reloads were six requests, and a
household that saves its options twice spent two days' budget in a minute.

The stamp and the answer therefore live in Home Assistant's storage, keyed by config
entry, and the entity reads them before it decides whether to ask anything. That also
means the tag survives a restart - the card keeps saying what it knows instead of going
blank until the next day's request.

This is deliberately its own module rather than part of `update.py`: the removal hook in
`__init__.py` has to reach it, and `update.py` imports the installer, which imports
asyncssh. Nothing should pay for a cryptography import to delete a stored timestamp.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .const import RELEASE_CHECK_STORAGE_KEY, RELEASE_CHECK_STORAGE_VERSION


class ReleaseCheckStore:
    """The last release check of every configured receiver, on disk."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Open the store without reading it; nothing needs it until a check runs."""
        self._store: Store[dict[str, Any]] = Store(
            hass, RELEASE_CHECK_STORAGE_VERSION, RELEASE_CHECK_STORAGE_KEY
        )
        self._entries: dict[str, Any] | None = None
        # One file, one receiver per key, and several receivers loading at once. The
        # lock is what keeps two of them from writing over each other's record.
        self._lock = asyncio.Lock()

    async def _async_entries(self) -> dict[str, Any]:
        """Return the stored mapping, reading the file at most once."""
        if self._entries is None:
            stored = await self._store.async_load()
            self._entries = dict(stored) if isinstance(stored, dict) else {}
        return self._entries

    async def async_get(
        self, entry_id: str
    ) -> tuple[datetime | None, str | None, str | None]:
        """Return when this receiver last asked, and what it was told.

        Anything unreadable in the file answers "never asked", because a stamp that
        cannot be parsed is not a stamp and the safe reading of that is to ask again.
        """
        async with self._lock:
            record = (await self._async_entries()).get(entry_id)
        if not isinstance(record, dict):
            return None, None, None
        checked = record.get("checked")
        version = record.get("version")
        url = record.get("url")
        return (
            dt_util.parse_datetime(checked) if isinstance(checked, str) else None,
            version if isinstance(version, str) else None,
            url if isinstance(url, str) else None,
        )

    async def async_set(
        self,
        entry_id: str,
        checked: datetime,
        version: str | None,
        url: str | None,
    ) -> None:
        """Record that this receiver asked, and what came back."""
        async with self._lock:
            entries = await self._async_entries()
            entries[entry_id] = {
                "checked": checked.isoformat(),
                "version": version,
                "url": url,
            }
            await self._store.async_save(entries)

    async def async_remove(self, entry_id: str) -> None:
        """Forget a receiver that has been removed from Home Assistant."""
        async with self._lock:
            entries = await self._async_entries()
            if entries.pop(entry_id, None) is None:
                return
            await self._store.async_save(entries)


@callback
def async_release_check_store(hass: HomeAssistant) -> ReleaseCheckStore:
    """Return the one store every receiver's check shares."""
    if (store := hass.data.get(RELEASE_CHECK_STORAGE_KEY)) is None:
        store = hass.data[RELEASE_CHECK_STORAGE_KEY] = ReleaseCheckStore(hass)
    return store

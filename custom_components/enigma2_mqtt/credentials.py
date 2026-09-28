"""The one writer of a receiver's SSH credentials, and the signal that follows every write.

SSH credentials are what makes the update card an install path and what the forced
reinstall needs, so two entities follow them: the update card's install feature and the
„Wymuś reinstalację wtyczki (SSH)" button, which exists only while they are stored
(ADR-0008, section 8). Neither can learn of a change by the usual means. The options flow is
an `OptionsFlowWithReload`, and Home Assistant refuses an update listener on an entry that
uses one; it reloads the entry only when the *options* changed, and credentials are kept in
the entry's *data*. Enrolling or forgetting them on their own therefore reloads nothing.

So every write goes through `async_set_ssh_credentials`, which updates the entry and then
sends `signal_ssh_credentials(entry_id)` on the dispatcher. Whoever depends on the
credentials listens for it and re-reads them. A test holds every other module to reading
the keys only - a second writer would be a change the entities never hear about.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_KEEP_SSH_CREDENTIALS,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    DOMAIN,
)
from .installer import SshCredentials

# What forgetting removes. The host and port stay: they say where the receiver is, not how
# to log in, and the options flow offers them again when somebody enrols.
_SECRET_KEYS = (CONF_SSH_USERNAME, CONF_SSH_PASSWORD, CONF_SSH_HOST_KEY, CONF_KEEP_SSH_CREDENTIALS)


def signal_ssh_credentials(entry_id: str) -> str:
    """The dispatcher signal sent after this entry's SSH credentials were written."""
    return f"{DOMAIN}_ssh_credentials_{entry_id}"


def ssh_entry_data(credentials: SshCredentials) -> dict[str, Any]:
    """The entry data that stores `credentials` - for the entry the guided installer creates.

    A new entry needs no signal: its platforms read the credentials when they are set up.
    """
    return {
        CONF_SSH_HOST: credentials.host,
        CONF_SSH_PORT: credentials.port,
        CONF_SSH_USERNAME: credentials.username,
        CONF_SSH_PASSWORD: credentials.password,
        CONF_SSH_HOST_KEY: credentials.host_key,
        CONF_KEEP_SSH_CREDENTIALS: True,
    }


def stored_ssh_credentials(entry: ConfigEntry) -> SshCredentials | None:
    """The complete pinned SSH identity this entry keeps, or None."""
    data = entry.data
    if not all(
        isinstance(data.get(key), str) and data[key]
        for key in (CONF_SSH_HOST, CONF_SSH_USERNAME, CONF_SSH_HOST_KEY, CONF_SSH_PASSWORD)
    ):
        return None
    return SshCredentials(
        host=data[CONF_SSH_HOST],
        port=data.get(CONF_SSH_PORT, 22),
        username=data[CONF_SSH_USERNAME],
        password=data[CONF_SSH_PASSWORD],
        host_key=data[CONF_SSH_HOST_KEY],
    )


@callback
def async_set_ssh_credentials(
    hass: HomeAssistant, entry: ConfigEntry, credentials: SshCredentials | None
) -> None:
    """Store `credentials` in the entry, or forget the stored ones with None, and say so."""
    data = dict(entry.data)
    if credentials is None:
        for key in _SECRET_KEYS:
            data.pop(key, None)
    else:
        data.update(ssh_entry_data(credentials))
    hass.config_entries.async_update_entry(entry, data=data)
    async_dispatcher_send(hass, signal_ssh_credentials(entry.entry_id))

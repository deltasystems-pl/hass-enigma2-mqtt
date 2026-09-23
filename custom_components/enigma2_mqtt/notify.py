"""The on-screen display, as two notification targets.

`notify.send_message` on „Ekran OSD" puts a popup on the television. The plugin caps the
text at 500 characters, and so does this — truncating here means the message a user sees
is the message they can read the start of, rather than one the box silently cut
somewhere else.

„Ekran – dyskretnie" is the same message as a **toast**: a small overlay in a corner of
the screen that takes no key press, hides itself after a few seconds and is replaced by
the next one. It exists only on a receiver that names the `toast` capability, which the
plugin claims only once the screen for it has actually been built; a receiver that
cannot show one never gets an entity that would publish into a refusal. Its text is cut
at 200, where the plugin cuts a toast, for the same reason the popup's is cut at 500.
"""

from __future__ import annotations

import json

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import (
    CAPABILITY_TOAST,
    DOMAIN,
    MESSAGE_DEFAULT_TIMEOUT,
    MESSAGE_MAX_LENGTH,
    MESSAGE_STYLE_POPUP,
    MESSAGE_STYLE_TOAST,
    TOAST_DEFAULT_TIMEOUT,
    TOAST_MAX_LENGTH,
    TOAST_MAX_TIMEOUT,
    TOAST_MIN_TIMEOUT,
)
from .entity import Enigma2Entity, OptionalEntities

PARALLEL_UPDATES = 0

KEY_OSD = "osd"
KEY_OSD_TOAST = "osd_toast"


def message_payload(
    text: str,
    message_type: str = "info",
    timeout: int | None = None,
    style: str = MESSAGE_STYLE_POPUP,
) -> str:
    """Return a `cmd/message` payload with the text capped the way the plugin caps it.

    🔴 A popup is written exactly as it was before toasts existed — three fields, no
    `style` — so that every popup is byte-for-byte what an older plugin already
    understands. Only a toast carries the field.

    The default timeout belongs to the style, which is why it is not a default of the
    argument: a toast that inherited the popup's ten seconds would sit on the screen
    twice as long as a toast is meant to.
    """
    if style == MESSAGE_STYLE_TOAST:
        return json.dumps(
            {
                "text": text[:TOAST_MAX_LENGTH],
                "type": message_type,
                "timeout": TOAST_DEFAULT_TIMEOUT if timeout is None else timeout,
                "style": MESSAGE_STYLE_TOAST,
            }
        )
    return json.dumps(
        {
            "text": text[:MESSAGE_MAX_LENGTH],
            "type": message_type,
            "timeout": MESSAGE_DEFAULT_TIMEOUT if timeout is None else timeout,
        }
    )


def check_toast(box: Enigma2Box, timeout: int | None = None) -> None:
    """Refuse a toast the receiver cannot show, before anything is published.

    The plugin refuses both cases itself, but its refusal lands on `last_error`, where a
    person who pressed a button in Home Assistant is not looking. Saying so here puts
    the reason in the dialog they are looking at.

    The timeout is checked against the toast's own range rather than left to the box's
    clamp: `0` means „until dismissed" to a popup, and a toast cannot be dismissed, so
    the plugin refuses it — and a request for a minute silently shown for thirty seconds
    is a surprise nobody asked for.
    """
    if CAPABILITY_TOAST not in box.capabilities:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="toast_unsupported"
        )
    if timeout is not None and not TOAST_MIN_TIMEOUT <= timeout <= TOAST_MAX_TIMEOUT:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="toast_timeout_out_of_range",
            translation_placeholders={
                "timeout": str(timeout),
                "min": str(TOAST_MIN_TIMEOUT),
                "max": str(TOAST_MAX_TIMEOUT),
            },
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the on-screen display, and the toast once the receiver offers one."""
    box = entry.runtime_data
    async_add_entities([Enigma2Notify(box, KEY_OSD, MESSAGE_STYLE_POPUP)])

    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "notify",
            (KEY_OSD_TOAST,),
            lambda key: Enigma2Notify(box, key, MESSAGE_STYLE_TOAST),
            # Created when the receiver first names the capability, which is always after
            # the platforms are set up: the capability arrives on `info` in the retained
            # burst that follows the subscription.
            lambda: CAPABILITY_TOAST in box.capabilities,
            # Never removed. A capability that stops being named is an older plugin after
            # a downgrade, a skin reload whose rebuild failed, or a receiver that has not
            # answered yet — none of them a decision anybody made. Deleting the entity
            # would take the automations that notify it by name with it, and its id is the
            # Polish name on a Polish installation, so the next payload would not even
            # bring it back under the id they use.
            lambda: False,
            async_add_entities,
        ).start()
    )


class Enigma2Notify(Enigma2Entity, NotifyEntity):
    """Show a message on the television, as a popup or as a toast."""

    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(self, box: Enigma2Box, key: str, style: str) -> None:
        """Set up the display. It sends and never reads, so it has no topic."""
        super().__init__(box, key, requires=None)
        self._style = style

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Put a message on the screen.

        Both have one text field, so a title becomes its first line rather than being
        dropped: a notification whose subject vanished is worse than one with a colon in
        it.
        """
        text = f"{title}: {message}" if title else message
        if self._style == MESSAGE_STYLE_TOAST:
            check_toast(self.box)
        await self.box.async_publish_cmd(
            "message", message_payload(text, style=self._style)
        )

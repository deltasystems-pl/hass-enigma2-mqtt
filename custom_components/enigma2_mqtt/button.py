"""The buttons: one press, one command, no state.

Two of them can end the evening. „Głębokie uśpienie" shuts the receiver down to the
point where only a magic packet brings it back, and „Restart" reboots it; on a
dashboard that a household shares, both sit one mis-tap away from the volume. So they
are not created at all unless the option asks for them, and turning the option off
takes them out of the entity registry rather than leaving an unavailable entity behind
that looks like a fault.

The plugin has the same setting on its own side and refuses both commands while a
recording is running. This option is not a safety mechanism — that one is — it is about
what appears on the screen.

„Obudź (WoL)" is the one button that works while the box is unreachable, because that
is the only time it is worth pressing.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import Enigma2Box, Enigma2MqttConfigEntry, async_send_magic_packet
from .const import CONF_DANGEROUS_BUTTONS, DOMAIN
from .entity import Enigma2Entity

PARALLEL_UPDATES = 0

# The payload the plugin's press-style commands take. Any payload works; this is the
# one the contract names, and a log on the box reading `PRESS` is easier to follow
# than one reading an empty string.
PRESS = "PRESS"


@dataclass(frozen=True, kw_only=True)
class Enigma2ButtonDescription(ButtonEntityDescription):
    """A button, and what pressing it does."""

    press_fn: Callable[[Enigma2Box], Coroutine[Any, Any, None]]
    # Whether this button is only offered when the option asks for it.
    dangerous: bool = False
    # Whether this button works while the box is unreachable.
    offline: bool = False


BUTTONS: tuple[Enigma2ButtonDescription, ...] = (
    Enigma2ButtonDescription(
        key="deep_standby",
        dangerous=True,
        press_fn=lambda box: box.async_publish_cmd("deep_standby", PRESS),
    ),
    Enigma2ButtonDescription(
        key="restart_gui",
        device_class=ButtonDeviceClass.RESTART,
        press_fn=lambda box: box.async_publish_cmd("restart_gui", PRESS),
    ),
    Enigma2ButtonDescription(
        key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        dangerous=True,
        press_fn=lambda box: box.async_publish_cmd("reboot", PRESS),
    ),
    Enigma2ButtonDescription(
        key="wake",
        offline=True,
        press_fn=async_send_magic_packet,
    ),
    Enigma2ButtonDescription(
        key="screenshot",
        press_fn=lambda box: box.async_publish_cmd("screenshot", PRESS),
    ),
    Enigma2ButtonDescription(
        key="refresh_discovery",
        press_fn=lambda box: box.async_publish_cmd("discovery", PRESS),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons of one box, minus the ones the options hide."""
    box = entry.runtime_data
    wanted = bool(entry.options.get(CONF_DANGEROUS_BUTTONS))
    registry = er.async_get(hass)

    entities: list[Enigma2Button] = []
    for description in BUTTONS:
        if description.dangerous and not wanted:
            # Turning the option off has to take the button away, not grey it out: an
            # unavailable entity on a dashboard is a fault report, and this is a
            # preference.
            unique_id = f"{box.node_id}_{description.key}"
            if entity_id := registry.async_get_entity_id(
                Platform.BUTTON, DOMAIN, unique_id
            ):
                registry.async_remove(entity_id)
            continue
        entities.append(Enigma2Button(box, description))

    async_add_entities(entities)


class Enigma2Button(Enigma2Entity, ButtonEntity):
    """One command, behind one press."""

    entity_description: Enigma2ButtonDescription

    def __init__(
        self, box: Enigma2Box, description: Enigma2ButtonDescription
    ) -> None:
        """Set up the button from its description."""
        super().__init__(box, description.key, requires=None)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Return whether the button can do anything right now."""
        if self.entity_description.offline:
            return True
        return super().available

    async def async_press(self) -> None:
        """Send the command."""
        await self.entity_description.press_fn(self.box)

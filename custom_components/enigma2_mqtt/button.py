"""The buttons: one press, one command, and an answer.

Two of them can end the evening. „Głębokie uśpienie" shuts the receiver down to the
point where at best a magic packet brings it back, and „Restart" reboots it; on a
dashboard that a household shares, both sit one mis-tap away from the volume. So they
are not created at all unless the option asks for them - and unless the box itself says
they are permitted, because `deep_standby_allowed` is set on the receiver's own setup
screen and a box that has it off refuses both commands whatever Home Assistant thinks.
Offering a button that is always refused is worse than offering none.

Turning either gate off takes the buttons out of the entity registry rather than leaving
an unavailable entity behind that looks like a fault.

Every button waits for the box. The plugin verifies each command by effect and complains
on `last_error`; a button that published and returned made a refusal indistinguishable
from a working press, which is exactly how „Restart" and „Głębokie uśpienie" failed
silently for two days. Where there is an effect to watch - a new screen grab, a
republished announcement - that is the proof. Where there is none, because the box is
about to disappear anyway, the wait is the error-grace window: a complaint raises,
silence is success.

„Restart softcam" has two gates and both of them are the box's. Restarting the
card-sharing client costs a few seconds of a scrambled picture and nothing else, so there
is no reason to hide it behind a Home Assistant option as well - but a box whose setup
screen has not permitted it refuses the command, and so does a box on which no cam binary
resolves at all. Those are two different answers from two different parts of the plugin,
and a receiver gives them independently: the permission is a checkbox every installation
has, the capability is claimed only where the restart could actually be carried out. A
button that is always refused is one somebody presses twice and then reports.

„Obudź (WoL)" is the one button that sends no command at all, and the one that works
while the box is unreachable, because that is the only time it is worth pressing.

Both of them promise something about Wake-on-LAN - „Głębokie uśpienie" that a magic
packet brings the receiver back, „Obudź (WoL)" that the packet will - and on many
receivers neither is true: the image has no way to arm the network port for deep standby,
and the box stays dark whatever arrives. A receiver that says so (`info.wol.supported`
is `false`) gets the `wake_on_lan` attribute on those two buttons, and its one value is
translated per button into what the household can do instead: the remote, the front
button or a timer. A button has no description of its own in Home Assistant, and no
control of its own in the more-info dialog either: the attribute is for automations and
dashboards to read, and the frontend shows it only to administrators, under ⋮ -> Details.
It changes neither the name nor the entity id, which on a Polish installation is derived
from the Polish name. A receiver that says Wake-on-LAN works, or
a plugin that says nothing, gets no attribute: nothing changes where nobody has said it
does not work.
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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .box import (
    Enigma2Box,
    Enigma2CommandError,
    Enigma2MqttConfigEntry,
    async_send_magic_packet,
)
from .const import (
    ATTR_WAKE_ON_LAN,
    CAPABILITY_EPG_IMPORT,
    CAPABILITY_SOFTCAM,
    CONF_DANGEROUS_BUTTONS,
    DOMAIN,
    TOPIC_EPG_IMPORT,
    TOPIC_SCREEN,
    WAKE_ON_LAN_NOT_SUPPORTED,
)
from .entity import Enigma2Entity, OptionalEntities

PARALLEL_UPDATES = 0

# The payload the plugin's press-style commands take. Any payload works; this is the
# one the contract names, and a log on the box reading `PRESS` is easier to follow
# than one reading an empty string.
PRESS = "PRESS"


async def _async_screenshot(box: Enigma2Box) -> None:
    """Ask for a screen grab and wait for the picture that proves it happened."""
    before = box.updates.get(TOPIC_SCREEN, 0)
    await box.async_command(
        "screenshot",
        PRESS,
        effect=lambda: box.updates.get(TOPIC_SCREEN, 0) > before,
    )


async def _async_refresh_discovery(box: Enigma2Box) -> None:
    """Ask the box to announce itself again, and wait for the announcement.

    The proof is a new payload, not a different one: the announcement a box republishes
    is usually identical to the one before it, so what is compared is the object the
    last message was parsed into rather than its contents.
    """
    before = box.state.announcement
    await box.async_command(
        "discovery", PRESS, effect=lambda: box.state.announcement is not before
    )


def _async_command(name: str) -> Callable[[Enigma2Box], Coroutine[Any, Any, None]]:
    """Return a press that sends one command and waits only for a complaint.

    For `deep_standby`, `reboot` and `restart_gui` the box is about to stop answering,
    and for `epg_grid` a grid whose content has not changed is not republished. None of
    them has an effect to watch, so the contract offers no positive acknowledgement and
    the error-grace window is the whole answer.
    """

    async def _press(box: Enigma2Box) -> None:
        await box.async_command(name, PRESS)

    return _press


async def _async_epg_import(box: Enigma2Box) -> None:
    """Ask the receiver to import EPG, and wait for the import to be seen running.

    The proof is the `epg_import` topic saying `running` - and it has to be a *new*
    payload that says so, not the state already held. An import the image's own
    schedule started is already `running`; the box refuses a second one with „already
    running" on `last_error`, and reading the old state as the answer would report that
    refusal as a success.

    Every guard refuses on `last_error`, and `async_command` raises that sentence. The
    one other answer is a new payload in `failed`: the plugin publishes it when starting
    the importer threw, with the reason in `error`. That ends the wait too and raises the
    receiver's own words, rather than sitting out the timeout and blaming the receiver
    for not answering when it did.
    """
    before = box.updates.get(TOPIC_EPG_IMPORT, 0)
    answer: dict[str, Any] = {}

    def answered() -> bool:
        # The first answer stands. The waiter resumes a tick after the future is set,
        # and every payload in between re-runs this check: a `failed` followed at once
        # by a `running` must still read as the failure it was.
        if answer:
            return True
        if box.updates.get(TOPIC_EPG_IMPORT, 0) <= before:
            return False
        # A retained delivery is what the broker already held - after a reconnect
        # Home Assistant resubscribes and the broker replays it - so it may be the
        # previous run's `failed`. It updates the sensor and answers nothing.
        if box.state.epg_import_retained:
            return False
        payload = box.state.epg_import or {}
        if payload.get("state") in ("running", "failed"):
            answer.update(payload)
            return True
        return False

    await box.async_command("epg_import", PRESS, effect=answered)
    if answer.get("state") == "failed":
        raise Enigma2CommandError(
            translation_domain=DOMAIN,
            translation_key="command_refused",
            translation_placeholders={
                "command": "epg_import",
                "reason": answer.get("error") or "the import failed",
            },
        )


@dataclass(frozen=True, kw_only=True)
class Enigma2ButtonDescription(ButtonEntityDescription):
    """A button, and what pressing it does."""

    press_fn: Callable[[Enigma2Box], Coroutine[Any, Any, None]]
    # Whether this button works while the box is unreachable.
    offline: bool = False
    # Whether what this button promises depends on the receiver waking to a magic
    # packet, so that a receiver which says it cannot has to be named on it.
    wake_on_lan_note: bool = False


BUTTONS: tuple[Enigma2ButtonDescription, ...] = (
    Enigma2ButtonDescription(
        key="restart_gui",
        device_class=ButtonDeviceClass.RESTART,
        press_fn=_async_command("restart_gui"),
    ),
    Enigma2ButtonDescription(
        key="wake",
        offline=True,
        wake_on_lan_note=True,
        press_fn=async_send_magic_packet,
    ),
    Enigma2ButtonDescription(
        key="screenshot",
        press_fn=_async_screenshot,
    ),
    Enigma2ButtonDescription(
        key="refresh_discovery",
        press_fn=_async_refresh_discovery,
    ),
)

# Created only while the Home Assistant option is on *and* the box has not said that
# deep standby is forbidden.
DANGEROUS_BUTTONS: tuple[Enigma2ButtonDescription, ...] = (
    Enigma2ButtonDescription(
        key="deep_standby",
        press_fn=_async_command("deep_standby"),
        wake_on_lan_note=True,
    ),
    Enigma2ButtonDescription(
        key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        press_fn=_async_command("reboot"),
    ),
)

# Created only while the box names the capability behind it. `epg_grid` is absent when
# `epg_grid_events` is zero, and then there is nothing for this button to refresh.
CAPABILITY_BUTTONS: tuple[tuple[str, Enigma2ButtonDescription], ...] = (
    (
        "epg_grid",
        Enigma2ButtonDescription(
            key="refresh_epg",
            press_fn=_async_command("epg_grid"),
        ),
    ),
)

# Created only while the box says the command is permitted *and* names the capability
# that says it could carry it out, and behind no Home Assistant option at all. There is
# nothing dangerous about it - a softcam restart costs a few seconds of a scrambled
# picture - so the only questions worth asking are whether the box will do it and whether
# it can, and the box answers both itself.
#
# Its proof is silence, like the three restarts above, and that is a measured decision
# rather than a shortcut. The command's own sequence stops every instance, waits up to
# five seconds for them to go, kills whatever survived, starts one, and settles another
# five before it republishes `softcam` - so on a cam that ignores SIGTERM the topic moves
# at about ten seconds, which is exactly the command timeout. Waiting for it would turn a
# restart that worked into „the receiver did not carry this out" on the slowest boxes,
# which is the one shape of bug this project has already paid for twice. Every refusal -
# no permission, a recording, a recording due, the rate limit, the sixty seconds after a
# start - arrives on `last_error` immediately, and that is what the grace window is for.
SOFTCAM_BUTTON = Enigma2ButtonDescription(
    key="softcam_restart",
    device_class=ButtonDeviceClass.RESTART,
    press_fn=_async_command("softcam_restart"),
)

# „Pobierz EPG": the receiver's own EPG-Importer, run now rather than at its scheduled
# time. Two gates, both the box's, exactly as for „Restart softcam": the permission
# `epg_import_allowed`, set on the receiver and refused over MQTT, and the `epg_import`
# capability, claimed only where the importer was found loaded and safe to call.
EPG_IMPORT_BUTTON = Enigma2ButtonDescription(
    key="epg_import",
    press_fn=_async_epg_import,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons of one box, minus the ones the gates hide."""
    box = entry.runtime_data
    async_add_entities(Enigma2Button(box, description) for description in BUTTONS)

    def wanted() -> bool:
        """Return whether the Home Assistant option asks for the two of them."""
        return bool(entry.options.get(CONF_DANGEROUS_BUTTONS))

    def has_spoken() -> bool:
        """Return whether the box has stated its capability list yet.

        Either topic will do: the announcement carries the same list. What it asks is
        whether the list was *stated*, not whether a payload arrived - an `info` with no
        `capabilities` in it is a box that has said nothing on the subject, and reading
        that as "it has none" would delete a button the next message brings back.
        """
        return box.capabilities_declared

    def has_answered() -> bool:
        """Return whether the box has published the topic its permissions live on.

        `info` and nothing else. The announcement arrives first and carries no
        `settings` at all, so treating it as "the box has spoken" reads every
        permission as "not said" and creates buttons the very next message deletes.
        """
        return bool(box.state.info)

    by_key = {
        description.key: description
        for description in (
            *DANGEROUS_BUTTONS,
            *(pair[1] for pair in CAPABILITY_BUTTONS),
            SOFTCAM_BUTTON,
            EPG_IMPORT_BUTTON,
        )
    }

    def factory(key: str) -> Entity:
        return Enigma2Button(box, by_key[key])

    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "button",
            [description.key for description in DANGEROUS_BUTTONS],
            factory,
            # Nothing is created until `info` has arrived. It lands after the platforms
            # are set up - the subscription is registered and returns, and Home
            # Assistant debounces the SUBSCRIBE behind it - so a gate that read the
            # permission at set-up time would read "not said" on every start, create
            # both buttons, and delete them again a moment later when `info` landed
            # with a "no": registry churn on every restart, and a race where the
            # deletion arrives before the addition and the button is left registered
            # for good. Once `info` is in, a box that never reports
            # `deep_standby_allowed` is an older plugin, and the behaviour there is the
            # one that shipped: the option decides alone.
            lambda: (
                wanted() and has_answered() and box.deep_standby_permission is not False
            ),
            # The option being off is an answer on its own, and the one this
            # integration has always acted on, so it still removes the buttons from a
            # box that has said nothing. Everything else waits for the box: "not yet
            # known" is not a stated "no", and an entity somebody already has must
            # survive a start-up the box slept through.
            lambda: not wanted() or (
                has_answered() and box.deep_standby_permission is not None
            ),
            async_add_entities,
        ).start()
    )

    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "button",
            [SOFTCAM_BUTTON.key],
            factory,
            # 🔴 Two gates, because they answer two different questions and a receiver
            # can easily give opposite answers to them. `softcam_restart_allowed` is a
            # plain checkbox on every installation and says whether the household wants
            # the command available; the `softcam` capability says whether the receiver
            # can carry it out at all - it is claimed only where a cam binary actually
            # resolves and its family has a start line. A box with the permission on and
            # no resolvable cam publishes the permission, claims no capability, and
            # refuses the command, which is exactly the button nobody should be offered.
            #
            # A stated „yes" to the permission and nothing weaker. Silence is an older
            # plugin, which would refuse the command anyway, and this button has never
            # existed on one - so unlike deep standby there is no installation whose
            # button has to survive a box that has not spoken. `info` has to have arrived
            # at all, for the same reason it does there: the announcement carries no
            # `settings`, so reading the permission before `info` lands reads „not said"
            # on every start.
            lambda: (
                CAPABILITY_SOFTCAM in box.capabilities
                and has_answered()
                and box.softcam_restart_permission is True
            ),
            # 🔴 And only a stated „no" to the *permission* removes it - the capability
            # is deliberately not in this half. Somebody who turns the permission off at
            # the television has decided; a capability that stops being named is an
            # older plugin after a downgrade, a hook that failed to attach on one boot,
            # or a receiver that has not answered yet, and none of those is a decision
            # anybody made.
            lambda: has_answered() and box.softcam_restart_permission is False,
            async_add_entities,
        ).start()
    )

    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "button",
            [EPG_IMPORT_BUTTON.key],
            factory,
            # The softcam button's gates, for the softcam button's reasons: a stated
            # „yes" to the permission on a box that names the capability creates it,
            # and only a stated „no" to the permission removes it.
            lambda: (
                CAPABILITY_EPG_IMPORT in box.capabilities
                and has_answered()
                and box.epg_import_permission is True
            ),
            lambda: has_answered() and box.epg_import_permission is False,
            async_add_entities,
        ).start()
    )

    for capability, description in CAPABILITY_BUTTONS:
        entry.async_on_unload(
            OptionalEntities(
                hass,
                box,
                "button",
                [description.key],
                factory,
                lambda capability=capability: capability in box.capabilities,
                # Before `info` or the announcement arrives the box has not said which
                # features it hooked; it has said nothing, and nothing is not a reason
                # to delete somebody's button.
                has_spoken,
                async_add_entities,
            ).start()
        )


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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Name a receiver that cannot be woken over the network, and only that one.

        Read from the box on every write rather than cached, and kept while the box is
        unreachable: `info` is retained, so the last thing the receiver said about
        itself is still the truth when it is asleep - which is exactly when somebody
        looks at „Obudź (WoL)".
        """
        if (
            self.entity_description.wake_on_lan_note
            and self.box.wake_on_lan_supported is False
        ):
            return {ATTR_WAKE_ON_LAN: WAKE_ON_LAN_NOT_SUPPORTED}
        return None

    async def async_press(self) -> None:
        """Send the command and wait for the box to answer for it."""
        await self.entity_description.press_fn(self.box)

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
button or a timer, and on some receivers switching on a TV connected over HDMI with
HDMI-CEC on. A
button has no description of its own in Home Assistant, and no control of its own
in the more-info dialog either: the attribute is for automations and
dashboards to read, and the frontend shows it only to administrators, under ⋮ -> Details.
It changes neither the name nor the entity id, which on a Polish installation is derived
from the Polish name. A receiver that says Wake-on-LAN works, or
a plugin that says nothing, gets no attribute: nothing changes where nobody has said it
does not work.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import logging
import math
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.translation import async_get_translations
import homeassistant.util.dt as dt_util

from .box import (
    Enigma2Box,
    Enigma2CommandError,
    Enigma2MqttConfigEntry,
    async_send_magic_packet,
)
from .buildid import base_version
from .const import (
    ATTR_WAKE_ON_LAN,
    CAPABILITY_EPG_IMPORT,
    CAPABILITY_HISTORY_CLEAR,
    CAPABILITY_SOFTCAM,
    CONF_BASE_TOPIC,
    CONF_DANGEROUS_BUTTONS,
    CONF_NODE_ID,
    DEFAULT_BASE_TOPIC,
    DOMAIN,
    HISTORY_CLEAR_REASONS,
    SIGNAL_FORCE_REINSTALL,
    TOPIC_EPG_IMPORT,
    TOPIC_SCREEN,
    TOPIC_ZAP_HISTORY,
    WAKE_ON_LAN_NOT_SUPPORTED,
)
from .credentials import signal_ssh_credentials, stored_ssh_credentials
from .entity import Enigma2Entity, OptionalEntities
from .installer import InstallerError, InstallerErrorCode, InstallRequest, async_install
from .plugin_versions import async_plugin_versions, has_credentials
from .release_store import (
    ERROR_BAD_MEMORY,
    ERROR_BAD_SIGNATURE,
    RESULT_FAILED,
    SIGNATURE_OR_ORDER,
    CheckRateLimited,
    async_release_index_cache,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

KEY_CHECK_PLUGIN_UPDATE = "check_plugin_update"
KEY_FORCE_REINSTALL = "force_reinstall"
# How long the first press of „Wymuś reinstalację wtyczki (SSH)" waits for the second.
FORCE_REINSTALL_WINDOW = 30.0

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


async def _async_history_clear(box: Enigma2Box) -> None:
    """Clear the receiver's zap history the way its 0 key does, and wait to see it.

    The proof is a `zap_history` payload that arrived **after** the press and holds at
    most one entry - not "the list holds at most one entry", which is already true
    before a press the receiver will refuse for exactly that reason. A retained copy the
    broker replays after a reconnect is never the answer either.

    Every refusal carries a reason code, and each code has its own translation, so the
    household reads why in its own language: standby, the panic-button setting off, a
    history too short to clear, timeshift, and the rest in `HISTORY_CLEAR_REASONS`.
    """
    before = box.updates.get(TOPIC_ZAP_HISTORY, 0)

    def cleared() -> bool:
        if box.updates.get(TOPIC_ZAP_HISTORY, 0) <= before:
            return False
        if box.state.zap_history_retained:
            return False
        history = box.state.zap_history
        return history is not None and len(history["entries"]) <= 1

    await box.async_command(
        "history_clear", PRESS, effect=cleared, reasons=HISTORY_CLEAR_REASONS
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
    # Whether the button is unavailable while the receiver says its panic-button setting
    # is off, the one state in which its command can never succeed.
    needs_panic_button: bool = False


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

# "Wyczyść ostatnio oglądane": what the remote's 0 key does with the receiver's panic
# button on - the zap history emptied and the receiver on channel 1, the first channel of
# its first bouquet. No permission: `cmd/zap` needs none, and `cmd/key KEY_0` already does
# the same with none. It is unavailable while the receiver says the panic button is off,
# because 0 then goes back one channel and clears nothing; every other case in which 0
# would not clear is a refusal the receiver explains, translated.
HISTORY_CLEAR_BUTTON = Enigma2ButtonDescription(
    key="history_clear",
    press_fn=_async_history_clear,
    needs_panic_button=True,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Enigma2MqttConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons of one box, minus the ones the gates hide."""
    box = entry.runtime_data
    async_add_entities(Enigma2Button(box, description) for description in BUTTONS)
    async_add_entities([Enigma2CheckPluginUpdateButton(box)])

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
            HISTORY_CLEAR_BUTTON,
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

    # „Wymuś reinstalację wtyczki (SSH)" exists exactly while SSH credentials are stored
    # (ADR-0008, section 8): never a present-but-refusing button. The gate is a fact Home
    # Assistant holds, not something the box says, so it is "declared" at once and followed
    # on the credentials signal - the only way to hear of an enrolment or a forgetting,
    # neither of which reloads the entry. Forgetting removes the registry entry for good,
    # so enrolling again brings a new button, disabled as the first one was. This is a
    # deliberate exception to "never removed when a capability goes quiet": that rule
    # protects against a box's silence, and this follows a decision taken here.
    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "button",
            [KEY_FORCE_REINSTALL],
            lambda _key: Enigma2ForceReinstallButton(box, entry),
            lambda: has_credentials(entry),
            lambda: True,
            async_add_entities,
            signal=signal_ssh_credentials(entry.entry_id),
            forget=True,
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

    entry.async_on_unload(
        OptionalEntities(
            hass,
            box,
            "button",
            [HISTORY_CLEAR_BUTTON.key],
            factory,
            lambda: CAPABILITY_HISTORY_CLEAR in box.capabilities,
            # Never removed, for the reason the history selects give: a capability that
            # goes quiet is not a decision, and a dashboard's button is not its price.
            lambda: False,
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


class Enigma2CheckPluginUpdateButton(Enigma2Entity, ButtonEntity):
    """„Sprawdź aktualizacje wtyczki": ask the plugin's signed release index now.

    The one button that talks to something other than the broker, and pressing it is the
    consent to do so: the daily check stays behind its option, off by default, but a person
    who presses this has asked. It needs nothing from the receiver - the index is the same
    for every receiver and is fetched by Home Assistant - so it works while the box is
    asleep, and one press refreshes every receiver's card.

    Every press after the first within ten minutes is answered, not repeated: GitHub Pages
    keeps the file for ten minutes anyway, so a second request could only return the same
    bytes, and the answer says when the list was read and when it can be read again. A
    check that fails raises, in words: the attribute on the update card keeps the code.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, box: Enigma2Box) -> None:
        """Set up the button; it reads no topic of the box."""
        super().__init__(box, KEY_CHECK_PLUGIN_UPDATE, requires=None)

    @property
    def available(self) -> bool:
        """Available whatever the receiver is doing: the index is not the receiver's."""
        return True

    async def async_press(self) -> None:
        """Check now, or say why not."""
        cache = async_release_index_cache(self.hass)
        try:
            result = await cache.async_check(manual=True)
        except CheckRateLimited as limited:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="release_check_rate_limited",
                translation_placeholders={
                    "time": dt_util.as_local(limited.checked).strftime("%H:%M"),
                    "minutes": str(max(1, math.ceil(limited.remaining_seconds / 60))),
                },
            ) from limited
        if result.outcome != RESULT_FAILED:
            return
        if result.error == ERROR_BAD_SIGNATURE:
            key = "release_check_unverified"
        elif result.error in SIGNATURE_OR_ORDER:
            key = "release_check_refused"
        elif result.error == ERROR_BAD_MEMORY:
            key = "release_check_bad_memory"
        else:
            key = "release_check_failed"
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"reason": str(result.error)},
        )


class Enigma2ForceReinstallButton(Enigma2Entity, ButtonEntity):
    """„Wymuś reinstalację wtyczki (SSH)": the bundled plugin, reinstalled over SSH.

    The recovery and bootstrap path (ADR-0008, section 8). Every other way of changing the
    plugin needs a plugin that answers; this needs SSH and the bytes this integration ships,
    and nothing else - so it works on a receiver whose plugin is dead, switched off, or too
    old to update itself, and it is available while the receiver is unreachable over MQTT,
    which is exactly when it is wanted. It asks the receiver's lock, never the retained
    `update` phase, so a forged or stale phase cannot block it.

    Home Assistant has no confirmation for a button, so the press is made safe instead:
    the first press by an administrator arms the button for thirty seconds and posts the
    confirmation as a notice - raising nothing, so an automation is not aborted by it -
    and a second press by the same administrator inside the window runs the reinstall.
    `button.press` is not admin-only in core the way `update.install` is, so both presses
    are checked, and a press by anybody else - an automation included, which runs without
    a user - is answered with a notice and does nothing else. A script started by an
    administrator runs with that administrator's user, so two presses in one script
    confirm in one run: the same administrator, and an explicit act of theirs. An
    automation - even one an administrator triggers by hand - runs without a user and
    never confirms.

    While a reinstall runs, a press starts nothing and says so in a notice, which goes
    when the reinstall ends: the call blocks for minutes, and a second administrator - or
    the same one on another screen - would otherwise see nothing happen.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, box: Enigma2Box, entry: Enigma2MqttConfigEntry) -> None:
        """Set up the button; it reads no topic of the box."""
        super().__init__(box, KEY_FORCE_REINSTALL, requires=None)
        self._entry = entry
        # (the administrator who armed it, when the arming lapses on the monotonic clock)
        self._armed: tuple[str, float] | None = None
        self._cancel_expiry: CALLBACK_TYPE | None = None
        self._running = False

    @property
    def available(self) -> bool:
        """Available whatever the receiver says over MQTT: it needs only SSH."""
        return True

    @property
    def _notice_id(self) -> str:
        return f"{DOMAIN}_force_reinstall_{self._entry.entry_id}"

    async def async_will_remove_from_hass(self) -> None:
        """Forget an arming, and its notice, with the button."""
        self._disarm()

    async def _async_texts(self, category: str) -> dict[str, str]:
        return await async_get_translations(
            self.hass, self.hass.config.language, category, {DOMAIN}
        )

    async def _async_common(self, slug: str, **placeholders: str) -> str:
        text = (await self._async_texts("common")).get(f"component.{DOMAIN}.common.{slug}", slug)
        try:
            return text.format(**placeholders)
        except (IndexError, KeyError):
            return text

    @callback
    def _disarm(self) -> None:
        self._armed = None
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None
        persistent_notification.async_dismiss(self.hass, self._notice_id)

    @callback
    def _expire(self, _now: Any) -> None:
        """Thirty seconds without the second press: the first one is forgotten."""
        self._cancel_expiry = None
        self._disarm()

    async def async_press(self) -> None:
        """Arm on the first press, reinstall on the second - for an administrator only."""
        user_id = self._context.user_id if self._context is not None else None
        user = await self.hass.auth.async_get_user(user_id) if user_id else None
        if user is None or not user.is_admin:
            # A notice, not an error: an automation's run is not aborted, and nothing is
            # armed or started. An administrator's arming is left as it is.
            _LOGGER.warning(
                "Forced plugin reinstall pressed by %s, who is not a Home Assistant "
                "administrator; nothing was armed or started",
                "no user" if user is None else "a user",
            )
            persistent_notification.async_create(
                self.hass,
                await self._async_common("force_reinstall_admin_message"),
                await self._async_common("force_reinstall_title"),
                f"{self._notice_id}_refused",
            )
            return
        if self._running:
            _LOGGER.info("Forced plugin reinstall already running; the press changes nothing")
            persistent_notification.async_create(
                self.hass,
                await self._async_common("force_reinstall_running_message"),
                await self._async_common("force_reinstall_title"),
                f"{self._notice_id}_running",
            )
            return
        armed = self._armed
        if armed is not None and armed[0] == user.id and time.monotonic() < armed[1]:
            self._disarm()
            await self._async_reinstall()
            return
        versions = await async_plugin_versions(self.hass, self._entry)
        if (refusal := versions.bundle_refusal()) is not None:
            code, placeholders = refusal
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=f"update_version_{code}",
                translation_placeholders=placeholders,
            )
        bundled = versions.bundled()
        installed = versions.installed()
        version = bundled.version if bundled is not None else "?"
        message = await self._async_common("force_reinstall_confirm_message")
        installed_base = base_version(installed.version) if installed is not None else None
        bundled_base = base_version(bundled.version) if bundled is not None else None
        if (
            installed is not None
            and installed_base is not None
            and bundled_base is not None
            and installed_base > bundled_base
        ):
            message = (
                f"{message} "
                + await self._async_common("force_reinstall_older", installed=installed.display)
            )
        self._disarm()
        self._armed = (user.id, time.monotonic() + FORCE_REINSTALL_WINDOW)
        self._cancel_expiry = async_call_later(self.hass, FORCE_REINSTALL_WINDOW, self._expire)
        persistent_notification.async_create(
            self.hass,
            message,
            await self._async_common("force_reinstall_confirm_title", version=version),
            self._notice_id,
        )

    async def _async_reinstall(self) -> None:
        """Run the SSH installer in its forced mode, and say how it ended."""
        credentials = stored_ssh_credentials(self._entry)
        if credentials is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="update_no_credentials"
            )
        data = self._entry.data
        request = InstallRequest(
            credentials=credentials,
            provisioning=None,
            expect_running=False,
            node_id=data[CONF_NODE_ID],
            base_topic=data.get(CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC),
            force=True,
        )
        signal = f"{SIGNAL_FORCE_REINSTALL}_{self._entry.entry_id}"
        self._running = True
        _LOGGER.warning("Forced plugin reinstall over SSH confirmed; starting")
        try:
            result = await async_install(
                self.hass, request, lambda phase: async_dispatcher_send(self.hass, signal, phase)
            )
        except InstallerError as err:
            if err.code in (InstallerErrorCode.AUTH_FAILED, InstallerErrorCode.HOST_KEY_CHANGED):
                self._entry.async_start_reauth(self.hass)
            reason = await self._async_reason(err)
            # On „Ostatni błąd" too, as Home Assistant's own: the plugin may never publish
            # again, and this is the one place the household looks for why.
            self.box.async_record_local_error(KEY_FORCE_REINSTALL, reason)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="force_reinstall_failed",
                translation_placeholders={"reason": reason},
            ) from err
        finally:
            self._running = False
            persistent_notification.async_dismiss(self.hass, f"{self._notice_id}_running")
            async_dispatcher_send(self.hass, signal, None)
        persistent_notification.async_create(
            self.hass,
            await self._async_common(
                "force_reinstall_done_message"
                if result.plugin_enabled
                else "force_reinstall_disabled_message",
                version=result.version,
            ),
            await self._async_common("force_reinstall_title"),
            f"{self._notice_id}_result",
        )

    async def _async_reason(self, err: InstallerError) -> str:
        """The installer's sentence for `err`, in the installation's language.

        A refusal before the lock on a receiver whose interface an interrupted transaction
        left stopped started nothing (ADR-0008, section 8), and the code's own sentence
        says only why it was refused - so the household is also told that the receiver
        stays without a picture, and how to get one.
        """
        texts = await self._async_texts("config")
        text = texts.get(f"component.{DOMAIN}.config.abort.{err.code.value}")
        if not text:
            reason = err.code.value
        else:
            try:
                reason = text.format(**err.placeholders)
            except (IndexError, KeyError):
                reason = text
        if err.interface_stopped:
            reason = f"{reason} " + await self._async_common(
                "force_reinstall_interface_stopped"
            )
        return reason


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
        if (
            self.entity_description.needs_panic_button
            and self.box.zap_history_panic_button is False
        ):
            return False
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

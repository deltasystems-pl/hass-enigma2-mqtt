"""„Wymuś reinstalację wtyczki (SSH)": exists only with SSH credentials, and confirms itself.

ADR-0008, section 8. The button follows a decision taken in Home Assistant - credentials
stored or forgotten - so its existence is asserted on the entity registry's own events,
never only on the end state, which cannot tell "never created" from "created then
removed". Enrolling or forgetting credentials changes the entry's data and not its
options, and reloads nothing, so every test here that enrols or forgets also asserts that
nothing was reloaded.

The press is the other half: Home Assistant has no confirmation for a button, so the first
press by an administrator is a notice and the second, inside thirty seconds, is the
reinstall. What the reinstall does on the receiver is `test_installer_force.py`.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import types
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import Context, Event, HomeAssistant, State, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.enigma2_mqtt import button as button_module, entity as entity_module
from custom_components.enigma2_mqtt.const import (
    CONF_KEEP_SSH_CREDENTIALS,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    DOMAIN,
)
from custom_components.enigma2_mqtt.credentials import async_set_ssh_credentials
from custom_components.enigma2_mqtt.installer import (
    HostKey,
    InstallerError,
    InstallerErrorCode,
    InstallResult,
    Preflight,
    SshCredentials,
)
from custom_components.enigma2_mqtt.plugin_versions import PluginVersions

from .conftest import (
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    async_setup_box_then_retained,
)

UNIQUE_ID = f"{NODE_ID}_force_reinstall"
LAST_ERROR = "sensor.dekoder_salon_last_error"
PLUGIN = "update.dekoder_salon_plugin"
HOST_KEY = HostKey("ssh-ed25519", "ssh-ed25519 AAAAtest", "SHA256:test")
PREFLIGHT = Preflight("openvix", "3.12", 10_000_000, None, False, False, 0)
CREDENTIALS = {
    CONF_SSH_HOST: "192.0.2.12",
    CONF_SSH_PORT: 22,
    CONF_SSH_USERNAME: "root",
    CONF_SSH_PASSWORD: "example-only",
    CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
    CONF_KEEP_SSH_CREDENTIALS: True,
}
# What the options form is submitted with, every time: the same, so that no test's enrolment
# or forgetting is also an options change.
SETTINGS = {"dangerous_buttons": False, "wol_mac": "", "bouquets": []}
INSTALL = "custom_components.enigma2_mqtt.button.async_install"


def _registry_events(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Every create and remove of the button's registry entry, from now on."""
    seen: list[tuple[str, str]] = []

    @callback
    def _listener(event: Event) -> None:
        if event.data["action"] in ("create", "remove") and event.data["entity_id"].startswith(
            "button."
        ):
            entry = er.async_get(hass).async_get(event.data["entity_id"])
            unique_id = entry.unique_id if entry is not None else None
            if unique_id in (UNIQUE_ID, None):
                seen.append((event.data["action"], event.data["entity_id"]))

    hass.bus.async_listen(EVENT_ENTITY_REGISTRY_UPDATED, _listener)
    return seen


def _button_entry(hass: HomeAssistant) -> er.RegistryEntry | None:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("button", DOMAIN, UNIQUE_ID)
    return registry.async_get(entity_id) if entity_id else None


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    retained: dict[str, Any],
    *,
    credentials: bool,
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **(CREDENTIALS if credentials else {})},
        options={**entry.options, **SETTINGS},
    )
    entry.add_to_hass = lambda _hass: None  # already added
    burst = dict(retained)
    await async_setup_box_then_retained(hass, entry, retained)
    # The form fills in more than it is given; one pass settles the options, so that no
    # later enrolment or forgetting in a test is also an options change. That pass reloads
    # the entry, and the broker replays what it holds to the new subscription.
    result = await _options(hass, entry, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    for topic, payload in burst.items():
        async_fire_mqtt_message(hass, topic, payload, retain=True)
    await hass.async_block_till_done()


async def _options(hass: HomeAssistant, entry: MockConfigEntry, extra: dict[str, Any]) -> Any:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {**SETTINGS, **extra}
    )


async def _enrol(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await _options(hass, entry, {"configure_ssh": True})
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_probe_host_key",
        AsyncMock(return_value=HOST_KEY),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SSH_HOST: "192.0.2.12", CONF_SSH_PORT: 22}
        )
    with patch(
        "custom_components.enigma2_mqtt.config_flow.async_preflight",
        AsyncMock(return_value=PREFLIGHT),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SSH_USERNAME: "root", CONF_SSH_PASSWORD: "secret"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def _forget(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await _options(hass, entry, {CONF_KEEP_SSH_CREDENTIALS: False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


# --------------------------------------------------------------------------- existence


async def test_no_credentials_no_button(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """Never a present-but-refusing button."""
    events = _registry_events(hass)

    await _setup(hass, config_entry, box_on_the_broker, credentials=False)

    assert events == []
    assert _button_entry(hass) is None


async def test_enrolling_creates_one_button_disabled_and_reloads_nothing(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """The credentials signal creates it; no options change, so no reload - and no churn."""
    await _setup(hass, config_entry, box_on_the_broker, credentials=False)
    events = _registry_events(hass)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        await _enrol(hass, config_entry)

    reload_entry.assert_not_called()
    assert [action for action, _ in events] == ["create"]
    registry_entry = _button_entry(hass)
    assert registry_entry is not None
    assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert registry_entry.entity_category == "diagnostic"


async def test_forgetting_removes_the_button_and_its_registry_entry(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """A decision taken here, not a box's silence: removed, and no ghost survives."""
    await _setup(hass, config_entry, box_on_the_broker, credentials=True)
    assert _button_entry(hass) is not None
    events = _registry_events(hass)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        await _forget(hass, config_entry)

    reload_entry.assert_not_called()
    assert [action for action, _ in events] == ["remove"]
    assert _button_entry(hass) is None
    assert ("button", DOMAIN, UNIQUE_ID) not in er.async_get(hass).deleted_entities


async def test_a_reload_with_credentials_churns_nothing(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, box_on_the_broker, credentials=True)
    before = _button_entry(hass)
    events = _registry_events(hass)

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert events == []
    assert _button_entry(hass).id == before.id


async def test_a_stale_registry_entry_goes_at_setup_without_credentials(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """Left from a session that had credentials; the setup without them removes it."""
    config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "button", DOMAIN, UNIQUE_ID, config_entry=config_entry
    )
    events = _registry_events(hass)
    config_entry.add_to_hass = lambda _hass: None

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert [action for action, _ in events] == ["remove"]
    assert _button_entry(hass) is None


async def test_enrolling_again_brings_a_new_button_disabled_again(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """Whatever was done to the old one - enabled here - does not come back."""
    await _setup(hass, config_entry, box_on_the_broker, credentials=True)
    registry = er.async_get(hass)
    old = _button_entry(hass)
    registry.async_update_entity(old.entity_id, disabled_by=None, name="Renamed")

    await _forget(hass, config_entry)
    await _enrol(hass, config_entry)

    new = _button_entry(hass)
    assert new is not None
    assert new.id != old.id
    assert new.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert new.name is None


async def test_the_update_card_hears_the_credentials_without_a_reload(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """Its install feature follows the same signal."""
    await _setup(hass, config_entry, box_on_the_broker, credentials=False)
    assert not hass.states.get(PLUGIN).attributes["supported_features"] & 1

    async_set_ssh_credentials(
        hass,
        config_entry,
        SshCredentials("192.0.2.12", "root", "secret", "ssh-ed25519 AAAAtest"),
    )
    await hass.async_block_till_done()

    assert hass.states.get(PLUGIN).attributes["supported_features"] & 1


# The keys `async_set_ssh_credentials` alone may write into an entry's data.
_SSH_KEYS = {
    "CONF_SSH_USERNAME",
    "CONF_SSH_PASSWORD",
    "CONF_SSH_HOST_KEY",
    "CONF_KEEP_SSH_CREDENTIALS",
}


def _dict_with_ssh_key(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Dict)
        and any(isinstance(key, ast.Name) and key.id in _SSH_KEYS for key in inner.keys)
        for inner in ast.walk(node)
    )


# Where a dict display becomes entry data: the calls that write an entry, `.update()` of a
# mapping, and an assignment to a name that holds entry data. A form's suggested values
# (`{CONF_SSH_USERNAME: "root"}`) are none of these.
_WRITERS = {"async_update_entry", "async_create_entry", "update"}
_DATA_NAMES = {"data", "entry_data", "new_data"}


def _stores_ssh_keys(tree: ast.AST) -> list[int]:
    """Lines that write or remove one of the keys in entry data."""
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _WRITERS
            and any(_dict_with_ssh_key(arg) for arg in [*node.args, *node.keywords])
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in _DATA_NAMES for target in node.targets
        ):
            if _dict_with_ssh_key(node.value):
                lines.append(node.lineno)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if isinstance(node.slice, ast.Name) and node.slice.id in _SSH_KEYS:
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("pop", "setdefault")
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in _SSH_KEYS
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            if any(isinstance(item, ast.Name) and item.id in _SSH_KEYS for item in node.iter.elts):
                lines.append(node.lineno)
    return lines


def test_only_the_credentials_helper_writes_ssh_keys() -> None:
    """A second writer would be a change the button and the update card never hear of."""
    package = Path(button_module.__file__).parent
    offenders = {
        path.name: lines
        for path in sorted(package.glob("*.py"))
        if path.name != "credentials.py"
        and (lines := _stores_ssh_keys(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert offenders == {}
    # And the check sees a writer when there is one.
    assert _stores_ssh_keys(ast.parse("data = {CONF_SSH_PASSWORD: 'x'}"))
    assert _stores_ssh_keys(ast.parse("data.pop(CONF_SSH_HOST_KEY, None)"))
    assert _stores_ssh_keys(
        ast.parse("hass.config_entries.async_update_entry(e, data={**d, CONF_SSH_USERNAME: u})")
    )
    assert not _stores_ssh_keys(ast.parse("show(user_input or {CONF_SSH_USERNAME: 'root'})"))


# ------------------------------------------------------------------------------ presses


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Created enabled, as a user who switched it on on the device page has it."""
    monkeypatch.setattr(
        button_module.Enigma2ForceReinstallButton, "_attr_entity_registry_enabled_default", True
    )


@pytest.fixture
async def hass_user(hass: HomeAssistant, hass_owner_user: Any) -> Any:
    """A household member: may press buttons, is not an administrator.

    A read-only user never reaches the button at all - core refuses the service call - so
    the gate this file tests is the one a user with control but without admin meets. The
    owner exists first: Home Assistant makes the first person it creates the owner, and an
    owner is an administrator.
    """
    del hass_owner_user
    user = await hass.auth.async_create_user("Household member", group_ids=["system-users"])
    assert not user.is_admin
    return user


def _notices(hass: HomeAssistant) -> dict[str, Any]:
    return dict(persistent_notification._async_get_or_create_notifications(hass))


async def _press(hass: HomeAssistant, user_id: str | None) -> None:
    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: _button_entry(hass).entity_id},
        blocking=True,
        context=Context(user_id=user_id),
    )


async def _ready(
    hass: HomeAssistant, entry: MockConfigEntry, retained: dict[str, Any]
) -> None:
    await _setup(hass, entry, retained, credentials=True)
    assert hass.states.get(_button_entry(hass).entity_id) is not None


def _result(enabled_plugin: bool = True) -> InstallResult:
    return InstallResult("0.3.0", True, True, "", enabled_plugin)


async def test_the_first_press_is_a_notice_and_starts_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_admin_user.id)

    install.assert_not_awaited()
    notice = _notices(hass)[f"{DOMAIN}_force_reinstall_{config_entry.entry_id}"]
    assert notice["message"].startswith("Press again within 30 seconds to confirm.")
    assert "0.3.0" in notice["title"]


def test_the_confirmation_is_written_in_all_three_languages() -> None:
    translations = Path(button_module.__file__).parent / "translations"
    for language, opening in (
        ("en", "Press again within 30 seconds"),
        ("pl", "Naciśnij ponownie w ciągu 30 sekund"),
        ("de", "Innerhalb von 30 Sekunden erneut drücken"),
    ):
        common = json.loads((translations / f"{language}.json").read_text(encoding="utf-8"))[
            "common"
        ]
        assert common["force_reinstall_confirm_message"].startswith(opening)
        assert common["force_reinstall_admin_message"]


async def test_the_second_press_by_the_same_admin_runs_the_forced_install_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)

    install.assert_awaited_once()
    request = install.await_args.args[1]
    assert request.force is True
    assert request.package is None
    assert request.provisioning is None
    assert request.expect_running is False
    assert request.node_id == NODE_ID
    assert request.base_topic == BASE_TOPIC
    notices = _notices(hass)
    assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}" not in notices
    assert "reinstalled over SSH" in (
        notices[f"{DOMAIN}_force_reinstall_{config_entry.entry_id}_result"]["message"]
    )


async def test_after_thirty_seconds_the_first_press_is_forgotten(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_admin_user.id)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
        assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}" not in _notices(hass)
        await _press(hass, hass_admin_user.id)

    install.assert_not_awaited()
    assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}" in _notices(hass)


async def test_two_sources_inside_the_window_run_it_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """The dashboard and a service call by the same administrator: one reinstall."""
    await _ready(hass, config_entry, box_on_the_broker)
    entity_id = _button_entry(hass).entity_id

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_admin_user.id)
        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )
        await _press(hass, hass_admin_user.id)

    install.assert_awaited_once()


async def test_an_automation_s_two_presses_both_run_and_do_not_abort_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
) -> None:
    """The first press raises nothing, so the automation goes on - without continue_on_error.

    An automation runs without a user, so it cannot confirm: both presses are refusals by
    notice, and the step after them still runs.
    """
    await _ready(hass, config_entry, box_on_the_broker)
    entity_id = _button_entry(hass).entity_id
    done: list[Event] = []
    hass.bus.async_listen("force_reinstall_test_done", done.append)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {"platform": "event", "event_type": "force_reinstall_test"},
                "action": [
                    {"action": "button.press", "target": {"entity_id": entity_id}},
                    {"action": "button.press", "target": {"entity_id": entity_id}},
                    {"event": "force_reinstall_test_done"},
                ],
            }
        },
    )

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        hass.bus.async_fire("force_reinstall_test")
        await hass.async_block_till_done()

    assert len(done) == 1
    install.assert_not_awaited()
    assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}_refused" in _notices(hass)


async def test_a_non_admin_arms_nothing_starts_nothing_and_raises_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_user.id)
        await _press(hass, hass_user.id)

    install.assert_not_awaited()
    notices = _notices(hass)
    assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}" not in notices
    assert notices[f"{DOMAIN}_force_reinstall_{config_entry.entry_id}_refused"][
        "message"
    ] == "Only a Home Assistant administrator can confirm a forced plugin reinstall."


async def test_a_non_admin_first_press_is_no_confirmation_for_an_admin(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
    hass_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_user.id)
        await _press(hass, hass_admin_user.id)

    install.assert_not_awaited()
    assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}" in _notices(hass)


async def test_an_admin_s_second_press_does_not_confirm_another_admin_s_first(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)
    other = await hass.auth.async_create_user("Other admin", group_ids=["system-admin"])

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_admin_user.id)
        await _press(hass, other.id)

    install.assert_not_awaited()


async def test_a_non_admin_second_press_confirms_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
    hass_user,
) -> None:
    """The check on the second press: an admin arms, somebody else presses."""
    await _ready(hass, config_entry, box_on_the_broker)

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_user.id)
        # Nor does it arm anything of its own: a third press by the same user is refused too.
        await _press(hass, hass_user.id)

    install.assert_not_awaited()
    assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}_refused" in _notices(hass)


async def test_a_press_with_no_user_is_refused_by_notice(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)
    system = await hass.auth.async_create_system_user(
        "Automations", group_ids=["system-users"]
    )

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, None)
        await _press(hass, None)
        await _press(hass, system.id)
        await _press(hass, system.id)

    install.assert_not_awaited()
    assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}_refused" in _notices(hass)


async def test_it_always_uses_ssh_even_when_the_box_updates_itself(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """A box offering `self_update` - and a forged, fresh `update` phase on the broker -
    changes nothing: the lock on the receiver decides, not a topic."""
    info = {**INFO, "capabilities": [*INFO.get("capabilities", []), "self_update"]}
    box_on_the_broker[INFO_TOPIC] = json.dumps(info)
    box_on_the_broker[f"{BASE_TOPIC}/{NODE_ID}/update"] = json.dumps(
        {"transaction": {"phase": "installing", "started": dt_util.utcnow().timestamp()}}
    )
    await _ready(hass, config_entry, box_on_the_broker)
    mqtt_mock.async_publish.reset_mock()

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)

    install.assert_awaited_once()
    topics = [call.args[0] for call in mqtt_mock.async_publish.call_args_list]
    assert not any(topic.endswith("/cmd/update") for topic in topics)


async def test_a_withdrawn_bundle_is_refused_at_the_first_press(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """v5.5: the next integration release carries a fixed bundle; this one is not armed."""
    await _ready(hass, config_entry, box_on_the_broker)

    with (
        patch.object(
            PluginVersions,
            "bundle_refusal",
            return_value=("withdrawn", {"version": "0.3.0", "reason": "broken"}),
        ),
        patch(INSTALL, AsyncMock(return_value=_result())) as install,
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass, hass_admin_user.id)

    assert raised.value.translation_key == "update_version_withdrawn"
    install.assert_not_awaited()
    assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}" not in _notices(hass)


async def test_over_a_newer_plugin_the_notice_says_the_bundle_is_older(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "plugin": "0.9.0"})
    await _ready(hass, config_entry, box_on_the_broker)

    await _press(hass, hass_admin_user.id)

    notice = _notices(hass)[f"{DOMAIN}_force_reinstall_{config_entry.entry_id}"]
    assert notice["message"].endswith(
        "This is an older version than the one installed (0.9.0)."
    )


async def test_a_switched_off_plugin_s_result_says_so(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)

    with patch(INSTALL, AsyncMock(return_value=_result(enabled_plugin=False))):
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)

    assert _notices(hass)[f"{DOMAIN}_force_reinstall_{config_entry.entry_id}_result"][
        "message"
    ] == "The plugin was installed, but it is switched off in its settings on the receiver."


async def test_a_failure_is_on_last_error_with_home_assistant_as_its_source(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """The plugin may never publish again; „Ostatni błąd" is where the household looks."""
    await _ready(hass, config_entry, box_on_the_broker)

    with (
        patch(
            INSTALL,
            AsyncMock(side_effect=InstallerError(InstallerErrorCode.GUI_STOPPED, "stopped")),
        ),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)

    assert raised.value.translation_key == "force_reinstall_failed"
    assert "stopped on purpose" in raised.value.translation_placeholders["reason"]
    state = hass.states.get(LAST_ERROR)
    assert state.state == "force_reinstall"
    assert state.attributes["source"] == "home_assistant"
    assert "stopped on purpose" in state.attributes["error"]


async def test_a_receiver_complaint_is_still_the_receiver_s(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, box_on_the_broker, credentials=False)

    async_fire_mqtt_message(
        hass,
        f"{BASE_TOPIC}/{NODE_ID}/last_error",
        json.dumps({"cmd": "zap", "error": "no such channel", "ts": 1_790_000_000}),
    )
    await hass.async_block_till_done()

    assert hass.states.get(LAST_ERROR).attributes["source"] == "receiver"


async def test_the_update_card_shows_the_forced_reinstall_running(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)
    seen: list[Any] = []

    async def _install(hass_: HomeAssistant, request: Any, progress_cb: Any = None) -> Any:
        progress_cb("install")
        await hass.async_block_till_done()
        seen.append(hass.states.get(PLUGIN).attributes["in_progress"])
        seen.append(hass.states.get(PLUGIN).attributes["update_percentage"])
        return _result()

    with patch(INSTALL, _install):
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)
    await hass.async_block_till_done()

    assert seen == [True, 50]
    assert hass.states.get(PLUGIN).attributes["in_progress"] is False


# ------------------------------------------------- „Ostatni błąd" across reloads and restarts

OLD_COMPLAINT = json.dumps({"cmd": "zap", "error": "no such channel", "ts": 1_700_000_000})


async def _fail_forced_reinstall(hass: HomeAssistant, admin_id: str) -> None:
    with (
        patch(INSTALL, AsyncMock(side_effect=InstallerError(InstallerErrorCode.GUI_STOPPED))),
        pytest.raises(HomeAssistantError),
    ):
        await _press(hass, admin_id)
        await _press(hass, admin_id)


async def test_a_home_assistant_failure_outlives_a_reload_and_the_old_complaint_replayed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """A dead plugin's retained complaint stays on the broker for good, and the broker
    replays it to every new subscription: after a reload it must not put a months-old
    refusal over the failure the household is looking for."""
    box_on_the_broker[f"{BASE_TOPIC}/{NODE_ID}/last_error"] = OLD_COMPLAINT
    await _ready(hass, config_entry, box_on_the_broker)
    await _fail_forced_reinstall(hass, hass_admin_user.id)
    assert hass.states.get(LAST_ERROR).state == "force_reinstall"

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    async_fire_mqtt_message(
        hass, f"{BASE_TOPIC}/{NODE_ID}/last_error", OLD_COMPLAINT, retain=True
    )
    await hass.async_block_till_done()

    state = hass.states.get(LAST_ERROR)
    assert state.state == "force_reinstall"
    assert state.attributes["source"] == "home_assistant"


async def test_a_home_assistant_failure_outlives_a_restart_and_the_old_complaint_replayed(
    hass: HomeAssistant, mqtt_mock, box_on_the_broker, config_entry: MockConfigEntry
) -> None:
    """A restart: the record is restored first, then the broker replays the receiver's
    older complaint to the entity that now exists."""
    registry = er.async_get(hass)
    config_entry.add_to_hass(hass)
    config_entry.add_to_hass = lambda _hass: None  # already added
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{NODE_ID}_last_error",
        config_entry=config_entry,
        suggested_object_id="dekoder_salon_last_error",
    )
    mock_restore_cache(
        hass,
        [
            State(
                LAST_ERROR,
                "force_reinstall",
                {
                    "error": "The receiver's interface was stopped on purpose (runlevel 4).",
                    "time": "2026-09-27T10:00:00+00:00",
                    "source": "home_assistant",
                },
            )
        ],
    )
    box_on_the_broker[f"{BASE_TOPIC}/{NODE_ID}/last_error"] = OLD_COMPLAINT

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(LAST_ERROR)
    assert state.state == "force_reinstall"
    assert state.attributes["source"] == "home_assistant"
    assert state.attributes["time"] == "2026-09-27T10:00:00+00:00"


async def test_a_later_or_live_receiver_complaint_replaces_the_home_assistant_record(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """Newer news does replace it: a replay the receiver dated later - by a clock a minute
    ahead of Home Assistant's, which is inside the tolerance - and anything it publishes
    now, whatever its clock says."""
    await _ready(hass, config_entry, box_on_the_broker)
    await _fail_forced_reinstall(hass, hass_admin_user.id)
    later = int(dt_util.utcnow().timestamp()) + 60
    async_fire_mqtt_message(
        hass,
        f"{BASE_TOPIC}/{NODE_ID}/last_error",
        json.dumps({"cmd": "restart_gui", "error": "later", "ts": later}),
        retain=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(LAST_ERROR).state == "restart_gui"

    await _fail_forced_reinstall(hass, hass_admin_user.id)
    assert hass.states.get(LAST_ERROR).state == "force_reinstall"
    async_fire_mqtt_message(hass, f"{BASE_TOPIC}/{NODE_ID}/last_error", OLD_COMPLAINT)
    await hass.async_block_till_done()
    state = hass.states.get(LAST_ERROR)
    assert state.state == "zap"
    assert state.attributes["source"] == "receiver"


# ------------------------------------------ a registry internal that changed under us


class _Registry:
    """The entity registry, with its `deleted_entities` missing, renamed or retyped."""

    def __init__(self, inner: er.EntityRegistry, shape: str) -> None:
        self._inner = inner
        self._shape = shape

    def __getattr__(self, name: str) -> Any:
        if name == "deleted_entities":
            if self._shape == "list":
                return []
            raise AttributeError(name)
        if name == "deleted_entity_records" and self._shape == "renamed":
            return self._inner.deleted_entities
        return getattr(self._inner, name)


@pytest.mark.parametrize("shape", ["missing", "renamed", "list"])
async def test_a_changed_registry_internal_never_breaks_the_button_platform(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    shape: str,
) -> None:
    """Every receiver without stored credentials reaches the forgetting at setup and on
    every `info`. A core without `deleted_entities` in this shape must cost only the
    forgetting - logged once - never the buttons registered after it."""
    real = er.async_get
    fake = types.SimpleNamespace(async_get=lambda hass_: _Registry(real(hass_), shape))

    with patch.object(entity_module, "er", fake):
        await _setup(hass, config_entry, box_on_the_broker, credentials=False)
        logged = caplog.text.count("keeps no deleted-entity records")
        for _ in range(3):
            async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
            await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("button", DOMAIN, f"{NODE_ID}_check_plugin_update")
    assert "Error while setting up" not in caplog.text
    assert "failed on" not in caplog.text
    # Once per setup of the platform, never once per `info`.
    assert logged >= 1
    assert caplog.text.count("keeps no deleted-entity records") == logged


# ------------------------------------------------------------ the presses, again


async def test_a_non_admin_press_leaves_an_admin_s_confirmation_pending(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
    hass_user,
) -> None:
    """A household member pressing in between neither confirms nor cancels: the
    administrator's own second press still runs it."""
    await _ready(hass, config_entry, box_on_the_broker)

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_user.id)
        assert f"{DOMAIN}_force_reinstall_{config_entry.entry_id}" in _notices(hass)
        await _press(hass, hass_admin_user.id)

    install.assert_awaited_once()


async def test_a_press_while_it_runs_starts_nothing_and_says_so(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """The call blocks for minutes. Presses meanwhile - even a whole new arming and
    confirmation - start no second install, and get a notice that goes when it ends.

    A press during the run arms nothing: an arming made then would still be live when the
    run ends, and one press inside its thirty seconds would start a second install."""
    await _ready(hass, config_entry, box_on_the_broker)
    gate = asyncio.Event()
    running = f"{DOMAIN}_force_reinstall_{config_entry.entry_id}_running"
    confirmation = f"{DOMAIN}_force_reinstall_{config_entry.entry_id}"

    async def _slow(*_args: Any, **_kwargs: Any) -> InstallResult:
        await gate.wait()
        return _result()

    with patch(INSTALL, AsyncMock(side_effect=_slow)) as install:
        await _press(hass, hass_admin_user.id)
        task = hass.async_create_task(_press(hass, hass_admin_user.id))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await _press(hass, hass_admin_user.id)
        assert running in _notices(hass)
        assert confirmation not in _notices(hass)
        await _press(hass, hass_admin_user.id)
        assert running in _notices(hass)
        assert confirmation not in _notices(hass)
        assert "already running" in _notices(hass)[running]["message"]
        gate.set()
        await task
        await hass.async_block_till_done()
        # The run has ended; one press is a first press again, never a confirmation.
        await _press(hass, hass_admin_user.id)
        await hass.async_block_till_done()

    install.assert_awaited_once()
    assert running not in _notices(hass)
    assert confirmation in _notices(hass)


@pytest.mark.parametrize("how", ["forget", "reload"])
async def test_the_confirmation_goes_with_the_button(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
    how: str,
) -> None:
    """A notice asking for a second press of a button that is gone would be a lie."""
    await _ready(hass, config_entry, box_on_the_broker)
    notice = f"{DOMAIN}_force_reinstall_{config_entry.entry_id}"

    await _press(hass, hass_admin_user.id)
    assert notice in _notices(hass)
    if how == "forget":
        async_set_ssh_credentials(hass, config_entry, None)
    else:
        assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert notice not in _notices(hass)


async def test_it_stays_available_while_the_receiver_is_really_offline(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
) -> None:
    """A last will as the broker delivers it, live: the box is unavailable - and the
    button, which needs only SSH, is not."""
    await _ready(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, f"{BASE_TOPIC}/{NODE_ID}/availability", "offline")
    await hass.async_block_till_done()

    assert config_entry.runtime_data.available is False
    assert hass.states.get(LAST_ERROR).state is not None
    assert hass.states.get(_button_entry(hass).entity_id).state != STATE_UNAVAILABLE


async def test_a_script_started_by_an_administrator_confirms_in_one_run(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """Decided: a script runs as the administrator who started it, so its two presses are
    that administrator's explicit act, and confirm."""
    await _ready(hass, config_entry, box_on_the_broker)
    entity_id = _button_entry(hass).entity_id
    press = {"action": "button.press", "target": {"entity_id": entity_id}}
    assert await async_setup_component(
        hass, "script", {"script": {"reinstall": {"sequence": [press, press]}}}
    )

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await hass.services.async_call(
            "script", "reinstall", blocking=True, context=Context(user_id=hass_admin_user.id)
        )
        await hass.async_block_till_done()

    install.assert_awaited_once()


async def test_an_automation_an_administrator_triggers_never_confirms(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """An automation runs without a user even when an administrator triggers it."""
    await _ready(hass, config_entry, box_on_the_broker)
    entity_id = _button_entry(hass).entity_id
    press = {"action": "button.press", "target": {"entity_id": entity_id}}
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "reinstall",
                "alias": "reinstall",
                "trigger": {"platform": "event", "event_type": "never_fired"},
                "action": [press, press],
            }
        },
    )

    with patch(INSTALL, AsyncMock(return_value=_result())) as install:
        await hass.services.async_call(
            "automation",
            "trigger",
            {ATTR_ENTITY_ID: "automation.reinstall"},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )
        await hass.async_block_till_done()

    install.assert_not_awaited()


def test_the_confirmation_says_standby_refuses_while_the_interface_runs() -> None:
    """Decided: the forced mode keeps the standby guard while the interface runs, so the
    confirmation may not promise to leave standby - only an interface that is not running
    is started by it."""
    translations = Path(button_module.__file__).parent / "translations"
    for language, words in (
        ("en", ("must be switched on, not in standby", "Only when the interface is not running")),
        ("pl", ("musi być włączony, a nie w trybie czuwania", "Tylko gdy interfejs nie działa")),
        ("de", ("muss der Receiver eingeschaltet sein", "Nur wenn die Oberfläche nicht läuft")),
    ):
        common = json.loads((translations / f"{language}.json").read_text(encoding="utf-8"))[
            "common"
        ]
        message = common["force_reinstall_confirm_message"]
        for phrase in words:
            assert phrase in message, (language, phrase)
        assert common["force_reinstall_running_message"]
    assert "will leave standby" not in json.loads(
        (translations / "en.json").read_text(encoding="utf-8")
    )["common"]["force_reinstall_confirm_message"]


# ------------------------------------------------------------------ review round 2


@pytest.mark.parametrize(
    "replayed",
    [
        # No date of its own: it cannot be placed.
        json.dumps({"cmd": "zap", "error": "no such channel"}),
        # Dated by a receiver whose clock runs years ahead.
        json.dumps({"cmd": "zap", "error": "no such channel", "ts": 1_900_000_000}),
    ],
    ids=["undated", "2030"],
)
async def test_a_replay_that_cannot_be_placed_never_replaces_the_home_assistant_record(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
    replayed: str,
) -> None:
    """The broker replays the receiver's retained complaint after every reload. Undated,
    or dated in 2030 by a fast clock, it would otherwise go over the failure the
    household is looking for - the 2030 one at every reload, for good."""
    box_on_the_broker[f"{BASE_TOPIC}/{NODE_ID}/last_error"] = replayed
    await _ready(hass, config_entry, box_on_the_broker)
    await _fail_forced_reinstall(hass, hass_admin_user.id)
    assert hass.states.get(LAST_ERROR).state == "force_reinstall"

    for _ in range(2):
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        async_fire_mqtt_message(
            hass, f"{BASE_TOPIC}/{NODE_ID}/last_error", replayed, retain=True
        )
        await hass.async_block_till_done()

        state = hass.states.get(LAST_ERROR)
        assert state.state == "force_reinstall"
        assert state.attributes["source"] == "home_assistant"


async def test_a_fast_clock_s_live_complaint_is_still_shown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """The tolerance is only for replays: what the receiver says while Home Assistant
    listens is news, whatever year its clock says."""
    await _ready(hass, config_entry, box_on_the_broker)
    await _fail_forced_reinstall(hass, hass_admin_user.id)

    async_fire_mqtt_message(
        hass,
        f"{BASE_TOPIC}/{NODE_ID}/last_error",
        json.dumps({"cmd": "zap", "error": "no such channel", "ts": 1_900_000_000}),
    )
    await hass.async_block_till_done()

    state = hass.states.get(LAST_ERROR)
    assert state.state == "zap"
    assert state.attributes["source"] == "receiver"


@pytest.mark.parametrize(
    ("error", "opening"),
    [
        (InstallerError(InstallerErrorCode.NO_SPACE), "The receiver does not have enough free"),
        (
            InstallerError(InstallerErrorCode.BUSY_STALLED, "", {"minutes": "26"}),
            "A plugin update on the receiver stopped without finishing",
        ),
    ],
    ids=["no_space", "busy_stalled"],
)
async def test_a_refusal_that_leaves_the_interface_stopped_says_so_and_how_to_start_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
    error: InstallerError,
    opening: str,
) -> None:
    """Refused before the lock, in runlevel 4 left by an interrupted transaction: nothing
    started the interface. The reason says why, then that the receiver stays without a
    picture and how to get one - whole, on „Ostatni błąd" too, although it is longer than
    the 255 characters a receiver's complaint is cut to."""
    error.interface_stopped = True
    await _ready(hass, config_entry, box_on_the_broker)

    with (
        patch(INSTALL, AsyncMock(side_effect=error)),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)

    reason = raised.value.translation_placeholders["reason"]
    assert reason.startswith(opening)
    # The code's own sentence comes whole, placeholders filled, before the addition.
    assert "{minutes}" not in reason
    assert ("about 26 min" in reason) is (error.code is InstallerErrorCode.BUSY_STALLED)
    assert "interface stays stopped" in reason
    assert reason.endswith("run the forced plugin reinstall again, which starts it.")
    shown = hass.states.get(LAST_ERROR).attributes["error"]
    assert shown == reason
    assert len(shown) > 255


async def test_a_refusal_with_the_interface_running_adds_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    await _ready(hass, config_entry, box_on_the_broker)

    with (
        patch(INSTALL, AsyncMock(side_effect=InstallerError(InstallerErrorCode.NO_SPACE))),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)

    assert raised.value.translation_placeholders["reason"] == (
        "The receiver does not have enough free space. Nothing was installed."
    )


def test_the_stopped_interface_sentence_is_written_in_all_three_languages() -> None:
    translations = Path(button_module.__file__).parent / "translations"
    for language, words in (
        ("en", ("interface stays stopped", "switch the receiver off and on", "forced plugin")),
        ("pl", ("pozostaje zatrzymany", "wyłącz dekoder i włącz go ponownie", "wymuszoną")),
        ("de", ("bleibt gestoppt", "aus und wieder ein", "erzwungene")),
    ):
        common = json.loads((translations / f"{language}.json").read_text(encoding="utf-8"))[
            "common"
        ]
        for phrase in words:
            assert phrase in common["force_reinstall_interface_stopped"], (language, phrase)


async def test_an_unexpected_failure_is_on_last_error_too(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """Not every failure is an InstallerError - a transport error from a step nobody wrapped,
    say. The press still ends in the button's own error, and „Ostatni błąd" still says the
    reinstall failed, instead of a generic error and nothing recorded."""
    await _ready(hass, config_entry, box_on_the_broker)

    with (
        patch(INSTALL, AsyncMock(side_effect=OSError("connection reset"))),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)

    assert raised.value.translation_key == "force_reinstall_failed"
    reason = raised.value.translation_placeholders["reason"]
    assert "unexpected error (OSError)" in reason
    state = hass.states.get(LAST_ERROR)
    assert state.state == "force_reinstall"
    assert state.attributes["source"] == "home_assistant"
    assert state.attributes["error"] == reason
    # The press is over: its "running" notice went with it.
    assert not _notices(hass).get(
        f"{DOMAIN}_force_reinstall_{config_entry.entry_id}_running"
    )


async def test_a_running_update_s_refusal_says_to_wait_not_to_pull_the_plug(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    enabled: None,
    hass_admin_user,
) -> None:
    """A self-update rolling back holds the lock and starts the interface when it ends:
    switching the receiver off now would cut its restore off."""
    error = InstallerError(InstallerErrorCode.BUSY)
    error.interface_stopped = True
    error.update_running = True
    await _ready(hass, config_entry, box_on_the_broker)

    with (
        patch(INSTALL, AsyncMock(side_effect=error)),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)

    reason = raised.value.translation_placeholders["reason"]
    assert reason.startswith("Another installation is already running on this receiver.")
    assert "Wait for it to finish" in reason
    assert "at its power switch or plug" not in reason
    assert hass.states.get(LAST_ERROR).attributes["error"] == reason


def test_the_new_sentences_are_written_in_all_three_languages() -> None:
    translations = Path(button_module.__file__).parent / "translations"
    for language, running, unexpected in (
        ("en", "Wait for it to finish", "unexpected error"),
        ("pl", "Poczekaj", "nieoczekiwany błąd"),
        ("de", "Warte", "unerwarteten Fehler"),
    ):
        common = json.loads((translations / f"{language}.json").read_text(encoding="utf-8"))[
            "common"
        ]
        assert running in common["force_reinstall_update_running"], language
        assert unexpected in common["force_reinstall_unexpected"], language
        assert "{error}" in common["force_reinstall_unexpected"], language

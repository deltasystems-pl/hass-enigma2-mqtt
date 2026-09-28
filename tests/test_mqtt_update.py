"""The plugin's update over MQTT: which path, the request, following it, and every end in words.

ADR-0008. Three things are held here:

- **the division of labour** - a receiver that states `self_update` with its box-only
  permission `update_allowed: true` is updated over MQTT, whatever SSH credentials are stored;
  otherwise over SSH with credentials; otherwise not at all, and without a path there is no badge.
  An update over MQTT that is refused, fails or is rolled back is never retried over SSH;
- **the request and its follow** - the signed entry's version and digest and a relay address,
  accepted only by a new, non-retained transaction of Home Assistant's; followed for at most 25
  minutes, after which the card says the update is still running rather than that it failed;
  installed only when the receiver reports the target and its signed commit;
- **the receiver's own transactions** on the card - in progress only while not finished and
  started within the last 25 minutes (and not more than a minute ahead), so a leftover or forged
  phase stops holding the card, and never holds the forced reinstall, which asks the receiver's
  lock.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import mqtt
from homeassistant.components.http import ApiConfig
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.enigma2_mqtt import (
    bundle as bundle_module,
    button as button_module,
    mqtt_update,
    release_index,
)
from custom_components.enigma2_mqtt.box import Enigma2Box
from custom_components.enigma2_mqtt.plugin_versions import async_plugin_versions
from custom_components.enigma2_mqtt.relay import RELAY_PATH, RelayError, async_get_relay
from custom_components.enigma2_mqtt.release_package import ORIGIN_DOWNLOAD, PackageSource

from .conftest import (
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    command_topic,
)
from .signed_index import commit_of, keyset, release
from .test_force_reinstall_button import INSTALL as FORCED_INSTALL, _press, _ready, _result
from .test_update_entity import PLUGIN, _accept, _load_real_bundle, _setup

UPDATE_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/update"
CMD_UPDATE = command_topic("update")
FETCH = "custom_components.enigma2_mqtt.update.async_fetch_release"
SSH_INSTALL = "custom_components.enigma2_mqtt.update.async_install"
ADAPTERS = "custom_components.enigma2_mqtt.relay.network.async_get_adapters"
RELAY_URL = re.compile(r"http://192\.0\.2\.5:8123/api/enigma2_mqtt/relay/[A-Za-z0-9_-]{43}")
TRANSLATIONS = Path(mqtt_update.__file__).parent / "translations"
STRINGS = Path(mqtt_update.__file__).parent / "strings.json"

IDENT = "a1b2c3d4e5f6"
TARGET = "0.4.0"
SHA = "cd" * 32

SELF_UPDATING = {
    **INFO,
    "capabilities": [*INFO["capabilities"], "self_update"],
    "settings": {"update_allowed": True},
}


@pytest.fixture(autouse=True)
def test_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judge by the vectors' test keys: nobody signs with the release keys but CI."""
    monkeypatch.setattr(release_index, "EMBEDDED", keyset("test"))


@pytest.fixture
def short_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer, follow and proof bounds in seconds instead of minutes, on the real clock.

    The values themselves are held by `test_the_bounds_cover_the_helper_s_worst_case`; this
    only makes the paths that end on a bound quick to walk.
    """
    monkeypatch.setattr(mqtt_update, "MQTT_UPDATE_ANSWER_TIMEOUT", 0.5)
    monkeypatch.setattr(mqtt_update, "MQTT_UPDATE_FOLLOW", 3)
    monkeypatch.setattr(mqtt_update, "MQTT_UPDATE_PROOF_TIMEOUT", 0.5)
    monkeypatch.setattr(mqtt_update, "MQTT_UPDATE_END_WAIT", 0.5)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forced-reinstall button created enabled, as a user who switched it on has it."""
    monkeypatch.setattr(
        button_module.Enigma2ForceReinstallButton, "_attr_entity_registry_enabled_default", True
    )


@pytest.fixture
async def lan(hass: HomeAssistant) -> Any:
    """Home Assistant on the receiver's subnet, with its HTTP server for the relay."""
    assert await async_setup_component(hass, "http", {})
    hass.config.api = ApiConfig("192.0.2.5", "0.0.0.0", 8123, False)
    adapter = {
        "name": "eth0",
        "index": 1,
        "enabled": True,
        "auto": True,
        "default": True,
        "ipv4": [{"address": "192.0.2.5", "network_prefix": 24}],
        "ipv6": [],
    }
    with patch(ADAPTERS, AsyncMock(return_value=[adapter])):
        yield
    async_get_relay(hass).clear()


def _package(version: str = TARGET) -> PackageSource:
    return PackageSource(
        version=version,
        data=f"signed {version}".encode(),
        sha256=SHA,
        commit=commit_of(version),
        depends=(),
        origin=ORIGIN_DOWNLOAD,
    )


def _transaction(
    phase: str,
    *,
    ident: str = IDENT,
    started_by: str = "home_assistant",
    target: str = TARGET,
    started: float | None = None,
    finished: float | None = None,
    result: str | None = None,
    reason: str | None = None,
    error: str | None = None,
) -> str:
    now = int(dt_util.utcnow().timestamp())
    return json.dumps(
        {
            "origin": "unknown",
            "checked": None,
            "check_error": None,
            "index": None,
            "latest_compatible": None,
            "available": [],
            "transaction": {
                "id": ident,
                "started_by": started_by,
                "target": target,
                "from": "0.3.0",
                "phase": phase,
                "started": int(now if started is None else started),
                "finished": (
                    None if phase != "finished" else int(now if finished is None else finished)
                ),
                "result": result,
                "reason": reason,
                "error": error,
            },
        }
    )


class FakeReceiver:
    """A receiver that answers `cmd/update` the way the plugin does, when told to."""

    def __init__(self, hass: HomeAssistant, answer: Any = "accept") -> None:
        self.hass = hass
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    async def arm(self) -> None:
        await mqtt.async_subscribe(self.hass, CMD_UPDATE, self._received)

    @callback
    def _received(self, msg: ReceiveMessage) -> None:
        self.requests.append(json.loads(msg.payload))
        if self.answer == "another complaint first":
            # A complaint about another command lands between the request and the answer.
            async_fire_mqtt_message(
                self.hass,
                LAST_ERROR_TOPIC,
                json.dumps({"cmd": "zap", "error": "no such channel", "ts": 1}),
            )
        elif self.answer == "accept":
            self.phase("downloading")
        elif isinstance(self.answer, tuple):
            reason, error = self.answer
            self.last_error(error, reason)

    def last_error(self, error: str, reason: str, ts: float | None = None) -> None:
        """`last_error` about `cmd/update`, stamped now unless told otherwise, as the plugin
        stamps it when it says it."""
        async_fire_mqtt_message(
            self.hass,
            LAST_ERROR_TOPIC,
            json.dumps(
                {
                    "cmd": "update",
                    "error": error,
                    "reason": reason,
                    "ts": int(dt_util.utcnow().timestamp() if ts is None else ts),
                }
            ),
        )

    def phase(self, phase: str, **kwargs: Any) -> None:
        retain = kwargs.pop("retain", False)
        async_fire_mqtt_message(
            self.hass, UPDATE_TOPIC, _transaction(phase, **kwargs), retain=retain
        )

    def report(self, version: str = TARGET, commit: str | None = None) -> None:
        async_fire_mqtt_message(
            self.hass,
            INFO_TOPIC,
            json.dumps(
                {
                    **SELF_UPDATING,
                    "plugin": version,
                    "build": {
                        "commit": commit if commit is not None else commit_of(version),
                        "time": 1790000000,
                        "dirty": False,
                        "flavour": "release",
                        "on_disk": None,
                    },
                }
            ),
        )


async def _self_updating(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    retained: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
    *,
    info: dict[str, Any] | None = None,
    credentials: bool = True,
    releases: list[dict[str, Any]] | None = None,
) -> None:
    """A receiver on 0.3.0 that updates itself, and an index listing 0.4.0."""
    retained[INFO_TOPIC] = json.dumps(SELF_UPDATING if info is None else info)
    await _setup(hass, entry, credentials=credentials)
    await _accept(
        hass,
        aioclient_mock,
        1,
        releases if releases is not None else [release(TARGET, sha256=SHA), release("0.3.0")],
    )


def _install(hass: HomeAssistant, version: str | None = None) -> asyncio.Task[Any]:
    """`update.install`, running - never tracked by Home Assistant, so waiting never hangs."""
    data: dict[str, Any] = {ATTR_ENTITY_ID: PLUGIN}
    if version is not None:
        data["version"] = version
    return asyncio.get_running_loop().create_task(
        hass.services.async_call("update", "install", data, blocking=True)
    )


async def _settle() -> None:
    """Run what is ready. Never `async_block_till_done` while an install runs: Home Assistant
    tracks the service call's task, so that would wait for the install itself."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _until(condition: Any) -> None:
    """Wait - briefly, and on the real clock - until `condition()` holds.

    The command goes out through the MQTT client and comes back to the fake receiver a few
    loop turns later; a test that fired the next phase before that would race its own receiver.
    """
    for turn in range(400):
        if condition():
            return
        await asyncio.sleep(0 if turn < 100 else 0.01)
    raise AssertionError("the fake receiver never heard the command")


def _commands(mqtt_mock: Any) -> list[Any]:
    return [call for call in mqtt_mock.async_publish.call_args_list if call.args[0] == CMD_UPDATE]


async def _refused(task: asyncio.Task[Any]) -> HomeAssistantError:
    with pytest.raises(HomeAssistantError) as raised:
        await task
    return raised.value


# ------------------------------------------------------------- the division of labour --


@pytest.mark.parametrize("credentials", [True, False])
async def test_a_receiver_that_updates_itself_is_updated_over_mqtt_whatever_ssh_says(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    credentials: bool,
) -> None:
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, credentials=credentials
    )

    state = hass.states.get(PLUGIN)
    assert state.attributes["update_path"] == "mqtt"
    assert state.state == STATE_ON
    assert state.attributes["latest_version"] == TARGET
    assert state.attributes["supported_features"] & 1


@pytest.mark.parametrize(
    ("info", "credentials", "path"),
    [
        # The permission off, stated: SSH with credentials, nothing without.
        ({**SELF_UPDATING, "settings": {"update_allowed": False}}, True, "ssh"),
        ({**SELF_UPDATING, "settings": {"update_allowed": False}}, False, None),
        # The capability missing: an older plugin, or one the package manager did not install.
        ({**SELF_UPDATING, "capabilities": INFO["capabilities"]}, True, "ssh"),
        ({**SELF_UPDATING, "capabilities": INFO["capabilities"]}, False, None),
        # A permission that is not a boolean is not a yes.
        ({**SELF_UPDATING, "settings": {"update_allowed": "true"}}, False, None),
        # No settings at all - a plugin that does not report them.
        ({key: value for key, value in SELF_UPDATING.items() if key != "settings"}, False, None),
    ],
)
async def test_without_capability_and_permission_the_path_is_ssh_or_none(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    info: dict[str, Any],
    credentials: bool,
    path: str | None,
) -> None:
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, info=info, credentials=credentials
    )

    state = hass.states.get(PLUGIN)
    assert state.attributes["update_path"] == path
    if path is None:
        # No install path, no badge.
        assert state.state == STATE_OFF
        assert state.attributes["latest_version"] == "0.3.0"
        assert state.attributes["supported_features"] == 0


async def test_over_mqtt_only_releases_the_index_lists_are_offered(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The receiver verifies against its signed index; a bundle it does not list is not offered,
    and with no index held there is nothing to install over MQTT - and no SSH instead."""
    box_on_the_broker[INFO_TOPIC] = json.dumps({**SELF_UPDATING, "plugin": "0.2.0"})
    await _setup(hass, config_entry, credentials=True)

    state = hass.states.get(PLUGIN)
    assert state.attributes["update_path"] is None
    assert state.state == STATE_OFF
    assert state.attributes["latest_version"] == "0.2.0"

    await _accept(hass, aioclient_mock, 1, [release("0.2.0")])
    state = hass.states.get(PLUGIN)
    assert state.attributes["update_path"] == "mqtt"
    assert state.state == STATE_OFF
    assert state.attributes["latest_version"] == "0.2.0"


async def test_a_retracted_info_is_no_claim(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    async_fire_mqtt_message(hass, INFO_TOPIC, "")
    await hass.async_block_till_done()

    assert hass.states.get(PLUGIN).attributes["update_path"] == "ssh"


# ------------------------------------------------------------------ the request --


async def test_the_request_carries_the_signed_entry_and_a_relay_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()
    seen: list[tuple[Any, Any]] = []

    with (
        patch(FETCH, AsyncMock(return_value=_package())) as fetch,
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        for phase in ("verifying", "snapshot", "installing", "restarting", "proving"):
            receiver.phase(phase)
            await _settle()
            state = hass.states.get(PLUGIN)
            seen.append((state.attributes["in_progress"], state.attributes["update_percentage"]))
        grant = async_get_relay(hass).grants(NODE_ID)[0]
        assert grant.held == 1
        receiver.report()
        receiver.phase("finished", result="installed")
        await task

    fetch.assert_awaited_once_with(hass, TARGET)
    ssh.assert_not_awaited()
    (command,) = _commands(mqtt_mock)
    assert command.args[2] == 1 and command.args[3] is False
    body = json.loads(command.args[1])
    assert set(body) == {"version", "sha256", "relay"}
    assert body["version"] == TARGET
    assert body["sha256"] == SHA
    assert RELAY_URL.fullmatch(body["relay"]["url"])
    assert isinstance(body["relay"]["expires"], int)
    assert body["relay"]["url"].endswith(RELAY_PATH + grant.token)
    assert seen == [(True, 20), (True, 35), (True, 50), (True, 65), (True, 85)]
    assert grant.held == 0
    state = hass.states.get(PLUGIN)
    assert state.attributes["in_progress"] is False
    assert state.attributes["installed_version"] == TARGET


async def test_no_downgrade_over_mqtt(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    await _self_updating(
        hass,
        config_entry,
        box_on_the_broker,
        aioclient_mock,
        releases=[release(TARGET, sha256=SHA), release("0.3.0"), release("0.2.0")],
    )

    with patch(FETCH, AsyncMock(return_value=_package("0.2.0"))) as fetch:
        error = await _refused(_install(hass, "0.2.0"))

    assert error.translation_key == "update_downgrade"
    fetch.assert_not_awaited()
    assert not _commands(mqtt_mock)


async def test_a_build_the_index_does_not_list_is_never_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """`update.install` without a version and nothing offered would reinstall the bundle over
    SSH; over MQTT the receiver can only take a signed release, so it is refused here."""
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, releases=[release("0.3.0")]
    )
    bundled = (await async_plugin_versions(hass, config_entry)).bundled()
    assert bundled is not None and not bundled.is_release

    with patch(SSH_INSTALL, AsyncMock()) as ssh:
        error = await _refused(_install(hass, bundled.display))

    assert error.translation_key == "update_mqtt_release_only"
    ssh.assert_not_awaited()
    assert not _commands(mqtt_mock)


async def test_an_offline_receiver_is_told_nothing_and_nothing_goes_over_ssh(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """A receiver restarting in its own update is offline for a moment: it stays on the MQTT path,
    and Home Assistant does not call install on the unavailable card at all."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    async_fire_mqtt_message(hass, f"{BASE_TOPIC}/{NODE_ID}/availability", "offline")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).state == "unavailable"

    with (
        patch(FETCH, AsyncMock(return_value=_package())) as fetch,
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        await hass.services.async_call(
            "update", "install", {ATTR_ENTITY_ID: PLUGIN}, blocking=True
        )

    fetch.assert_not_awaited()
    ssh.assert_not_awaited()
    assert not _commands(mqtt_mock)
    # The path is the receiver's word on `info`, not its availability: still MQTT while offline.
    assert config_entry.runtime_data.self_update_offered is True
    assert (await async_plugin_versions(hass, config_entry)).path == "mqtt"
    async_fire_mqtt_message(hass, f"{BASE_TOPIC}/{NODE_ID}/availability", "online")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["update_path"] == "mqtt"


# ------------------------------------------------------------------ the answer --

REFUSALS = [
    ("not_permitted", "updates over MQTT are switched off", "update_mqtt_not_permitted"),
    ("no_capability", "this plugin was not installed by the package manager",
     "update_mqtt_no_capability"),
    ("busy", "an update is already running on the receiver", "update_mqtt_busy"),
    ("opkg_busy", "the receiver's package manager is busy", "update_mqtt_opkg_busy"),
    ("standby", "the receiver is in standby", "update_standby"),
    ("recording", "a recording is running", "update_mqtt_recording"),
    ("recording_due", "a recording starts within ten minutes", "update_mqtt_recording"),
    ("recording_unknown", "the image will not say", "update_mqtt_recording_unknown"),
    ("epg_import", "an EPG import is running", "update_mqtt_epg_import"),
    ("cannot_restart", "cannot restart the interface", "update_mqtt_cannot_restart"),
    ("bad_request", "the update request could not be read", "update_mqtt_bad_request"),
    ("unknown_version", "version 0.4.0 is not in the index", "update_mqtt_unknown_version"),
    ("withdrawn", "version 0.4.0 has been withdrawn: broken", "update_mqtt_withdrawn"),
    ("below_floor", "below the lowest version", "update_mqtt_below_floor"),
    ("incompatible", "not compatible", "update_mqtt_incompatible"),
    ("depends", "needs python3-foo", "update_mqtt_depends"),
    ("downgrade", "a downgrade can only be started on the receiver", "update_downgrade"),
    ("current", "version 0.4.0 is already installed and running", "update_mqtt_current"),
    ("checksum", "the requested checksum does not match", "update_mqtt_checksum"),
    ("relay", "the download address from Home Assistant is not valid", "update_mqtt_relay"),
    ("no_space", "there is not enough free space", "update_mqtt_no_space"),
    ("rate_limited", "an update ran less than ten minutes ago", "update_mqtt_rate_limited"),
    ("internal_error", "the helper could not be launched", "update_mqtt_internal_error"),
    ("from_a_newer_plugin", "a sentence nobody translated", "update_mqtt_refused"),
]


@pytest.mark.parametrize(("reason", "error", "key"), REFUSALS)
async def test_every_refusal_is_said_in_words_and_never_retried_over_ssh(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    reason: str,
    error: str,
    key: str,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    await FakeReceiver(hass, (reason, error)).arm()

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        raised = await _refused(_install(hass))

    assert raised.translation_key == key
    if key == "update_mqtt_refused":
        assert raised.translation_placeholders["error"] == error
    ssh.assert_not_awaited()
    assert len(_commands(mqtt_mock)) == 1
    # Refused, never downloaded: the address is taken back, so no "not downloaded" notice
    # follows the sentence ten minutes later.
    assert async_get_relay(hass).grants(NODE_ID) == []
    assert hass.states.get(PLUGIN).attributes["in_progress"] is False


async def test_a_lock_left_by_a_stopped_helper_says_when_to_try_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    await FakeReceiver(
        hass,
        (
            "busy",
            "the previous update stopped without finishing; a new one is possible in about "
            "17 minutes, when its lock on the receiver expires",
        ),
    ).arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        raised = await _refused(_install(hass))

    assert raised.translation_key == "update_busy_stalled"
    assert raised.translation_placeholders["minutes"] == "17"


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"retain": True}, "the broker's replay"),
        ({"started_by": "mqtt"}, "started by a broker client without an address"),
        ({"started_by": "screen"}, "started at the television"),
        ({"target": "0.3.0"}, "another version"),
    ],
)
async def test_only_a_new_transaction_of_home_assistant_s_is_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
    kwargs: dict[str, Any],
    why: str,
) -> None:
    del why
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, None)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("downloading", **kwargs)
        await _settle()
        assert not task.done()
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_no_answer"


async def test_the_transaction_held_before_the_request_is_not_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    """The last transaction republished as it was - same id - is old news."""
    box_on_the_broker[UPDATE_TOPIC] = _transaction("finished", result="installed")
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, None)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("downloading")
        await _settle()
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_no_answer"


async def test_a_complaint_about_another_command_is_not_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, "another complaint first")
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        await _settle()
        assert not task.done()
        receiver.phase("downloading")
        await _settle()
        receiver.report()
        receiver.phase("finished", result="installed")
        await task


async def test_only_the_accepted_transaction_is_followed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """Another transaction's end - a leftover, or a broker client's - does not end this one."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase(
            "finished", ident="0123456789ab", result="failed", reason="opkg_failed"
        )
        await _settle()
        assert not task.done()
        receiver.report()
        receiver.phase("finished", result="installed")
        await task


# -------------------------------------------------------------------- the ends --

ENDS = [
    ("withdrawn_before_restart", "question", "update_mqtt_question"),
    ("withdrawn_before_restart", "standby", "update_mqtt_withdrawn_standby"),
    ("withdrawn_before_restart", "recording", "update_mqtt_withdrawn_recording"),
    ("withdrawn_before_restart", "epg_import", "update_mqtt_withdrawn_epg_import"),
    ("withdrawn_before_restart", "retraction", "update_mqtt_retraction"),
    ("withdrawn_before_restart", "from_a_newer_plugin", "update_mqtt_withdrawn_before_restart"),
    ("rolled_back", "not_started", "update_mqtt_not_started"),
    ("rolled_back", "time_limit", "update_mqtt_rolled_back_time_limit"),
    ("rolled_back", "interrupted", "update_mqtt_rolled_back_interrupted"),
    ("rolled_back", "drill", "update_mqtt_drill"),
    ("rolled_back", "internal_error", "update_mqtt_rolled_back"),
    # The phase `installing` was seen before this end: the files may be a mix.
    ("interrupted", "interrupted", "update_mqtt_interrupted_files_reinstall"),
    ("failed", "time_limit", "update_mqtt_time_limit"),
    ("failed", "not_stopped", "update_mqtt_not_stopped"),
    ("failed", "interface_not_started", "update_mqtt_interface_not_started"),
    ("failed", "restore_failed", "update_mqtt_restore_failed"),
    ("failed", "restore_incomplete", "update_mqtt_restore_incomplete"),
    ("failed", "download", "update_mqtt_download"),
    ("failed", "bad_package", "update_download_failed"),
    ("failed", "unreachable", "update_mqtt_unreachable"),
    ("failed", "snapshot_failed", "update_mqtt_snapshot_failed"),
    ("failed", "opkg_failed", "update_mqtt_opkg_failed"),
    ("failed", "manifest", "update_mqtt_manifest"),
    ("failed", "internal_error", "update_mqtt_internal_error"),
    ("failed", "busy", "update_mqtt_busy"),
    ("failed", "checksum", "update_mqtt_checksum"),
    ("failed", "no_space", "update_mqtt_no_space"),
    ("failed", "opkg_busy", "update_mqtt_opkg_busy"),
    ("failed", "from_a_newer_plugin", "update_mqtt_failed"),
]


@pytest.mark.parametrize(("result", "reason", "key"), ENDS)
async def test_every_end_is_said_in_words_and_never_retried_over_ssh(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    result: str,
    reason: str,
    key: str,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("installing")
        await _settle()
        receiver.phase("finished", result=result, reason=reason, error="the receiver's words")
        raised = await _refused(task)

    assert raised.translation_key == key
    placeholders = raised.translation_placeholders
    assert placeholders["version"] == TARGET
    assert placeholders["from_version"] == "0.3.0"
    if key == "update_mqtt_download":
        assert placeholders["url"] == "http://192.0.2.5:8123"
    ssh.assert_not_awaited()
    assert hass.states.get(PLUGIN).attributes["in_progress"] is False
    # Ended before anybody fetched it: the address is taken back.
    assert async_get_relay(hass).grants(NODE_ID) == []


async def test_still_running_at_the_bound_is_not_a_failure(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("rolling_back")
        await _settle()
        # Inside the bound, the call still waits.
        await asyncio.sleep(1.5)
        assert not task.done()
        state = hass.states.get(PLUGIN)
        assert state.attributes["in_progress"] is True
        assert state.attributes["update_percentage"] is None
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_still_running"
    ssh.assert_not_awaited()
    # The call is over; what the receiver says goes on being shown, within its own window,
    # which closes on its own timer.
    for _ in range(60):
        await hass.async_block_till_done()
        if hass.states.get(PLUGIN).attributes["receiver_transaction"]["state"] == "stale":
            break
        await asyncio.sleep(0.1)
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["id"] == IDENT
    assert attributes["receiver_transaction"]["state"] == "stale"
    assert attributes["in_progress"] is False


@pytest.mark.parametrize(
    ("version", "commit"),
    [("0.3.0", None), ("0.3.0", commit_of(TARGET)), (TARGET, "f" * 40)],
    ids=["the old build", "the old version", "another commit"],
)
async def test_installed_counts_only_when_the_receiver_runs_the_signed_build(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
    version: str,
    commit: str | None,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report(version, commit)
        receiver.phase("finished", result="installed")
        await _settle()
        assert not task.done()
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_unproven"


async def test_the_proof_may_arrive_after_the_end(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("finished", result="installed")
        await _settle()
        assert not task.done()
        receiver.report()
        await task


async def test_the_grant_being_followed_is_never_evicted(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """Three more versions asked for - by forged `relay_request`s, say - take other grants,
    never the one the receiver is downloading from."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()
    relay = async_get_relay(hass)

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        (ours,) = relay.grants(NODE_ID)
        for version in ("0.3.1", "0.3.2", "0.3.3"):
            package = PackageSource(version, b"x", "ef" * 32, None, (), ORIGIN_DOWNLOAD)
            relay.grant(NODE_ID, "192.0.2.12", package)
        assert ours in relay.grants(NODE_ID)
        receiver.report()
        receiver.phase("finished", result="installed")
        await task


# -------------------------------------------------- the receiver's own transactions --


async def test_a_receiver_started_transaction_is_shown_for_at_most_25_minutes(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    FakeReceiver(hass).phase("installing", started_by="screen")
    await hass.async_block_till_done()

    state = hass.states.get(PLUGIN)
    assert state.attributes["in_progress"] is True
    assert state.attributes["update_percentage"] == 50
    assert state.attributes["receiver_transaction"]["state"] == "in_progress"
    assert state.attributes["receiver_transaction"]["started_by"] == "screen"

    freezer.tick(timedelta(minutes=24, seconds=59))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["in_progress"] is True

    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(PLUGIN)
    assert state.attributes["in_progress"] is False
    assert state.attributes["receiver_transaction"]["state"] == "stale"


@pytest.mark.parametrize(
    ("offset", "shown"),
    [
        # Setting up takes a few real seconds, so the edges are not probed to the second here;
        # `test_a_phase_from_the_future_is_shown_once_its_window_opens` walks one exactly.
        (timedelta(seconds=30), True),
        (timedelta(minutes=5), False),
        (-timedelta(minutes=26), False),
    ],
)
async def test_a_phase_is_believed_only_inside_its_window(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    offset: timedelta,
    shown: bool,
) -> None:
    box_on_the_broker[UPDATE_TOPIC] = _transaction(
        "installing", started=(dt_util.utcnow() + offset).timestamp(), started_by="mqtt"
    )
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)

    state = hass.states.get(PLUGIN)
    assert state.attributes["in_progress"] is shown
    assert state.attributes["receiver_transaction"]["state"] == (
        "in_progress" if shown else "stale"
    )


async def test_a_phase_from_the_future_is_shown_once_its_window_opens(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    FakeReceiver(hass).phase(
        "installing", started=(dt_util.utcnow() + timedelta(minutes=3)).timestamp()
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["in_progress"] is False

    freezer.tick(timedelta(minutes=2, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["in_progress"] is True


async def test_a_finished_transaction_says_its_end_in_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    FakeReceiver(hass).phase(
        "finished", started_by="page", result="rolled_back", reason="not_started"
    )
    await hass.async_block_till_done()

    state = hass.states.get(PLUGIN)
    assert state.attributes["in_progress"] is False
    transaction = state.attributes["receiver_transaction"]
    assert transaction["state"] == "finished"
    assert transaction["result"] == "rolled_back"
    assert transaction["message"] == (
        "The new version of the plugin did not start. The receiver put the previous version "
        "0.3.0 back."
    )


async def test_a_refusal_at_the_television_is_said_in_words_on_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """`no_relay` and `clock_skew` refuse installs started on the receiver; Home Assistant can
    only say them, on the card, from `last_error`."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "update", "error": "...", "reason": "no_relay", "ts": 1790500000}),
    )
    await hass.async_block_till_done()

    refusal = hass.states.get(PLUGIN).attributes["last_refusal"]
    assert refusal["reason"] == "no_relay"
    assert refusal["message"].startswith("The receiver cannot reach")

    async_fire_mqtt_message(
        hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "zap", "error": "no", "ts": 1790500001})
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "no_relay"


async def test_the_forced_reinstall_is_not_held_by_a_forged_fresh_phase(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    enabled: None,
    hass_admin_user: Any,
) -> None:
    """A well-formed, fresh, retained phase holds the card; the rescue still runs, shows its own
    progress, and the card goes back to what the receiver says afterwards."""
    box_on_the_broker[UPDATE_TOPIC] = _transaction("installing", started_by="mqtt")
    await _ready(hass, config_entry, box_on_the_broker)
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(SELF_UPDATING))
    await _accept(hass, aioclient_mock, 1, [release(TARGET, sha256=SHA), release("0.3.0")])
    assert hass.states.get(PLUGIN).attributes["update_path"] == "mqtt"
    assert hass.states.get(PLUGIN).attributes["in_progress"] is True
    seen: list[Any] = []

    async def _forced(hass_: HomeAssistant, request: Any, progress_cb: Any = None) -> Any:
        progress_cb("upload")
        await hass.async_block_till_done()
        seen.append(hass.states.get(PLUGIN).attributes["update_percentage"])
        return _result()

    with patch(FORCED_INSTALL, _forced):
        await _press(hass, hass_admin_user.id)
        await _press(hass, hass_admin_user.id)
    await hass.async_block_till_done()

    assert seen == [37]
    state = hass.states.get(PLUGIN)
    assert state.attributes["in_progress"] is True
    assert state.attributes["update_percentage"] == 50


# ------------------------------------------------------------------- the words --


def _catalogue(language: str) -> dict[str, Any]:
    path = STRINGS if language == "strings" else TRANSLATIONS / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8"))["exceptions"]


def _every_key() -> set[str]:
    keys = {key for _, _, key in REFUSALS} | {key for _, _, key in ENDS}
    keys |= {
        "update_busy_stalled",
        "update_mqtt_no_answer",
        "update_mqtt_still_running",
        "update_mqtt_unproven",
        "update_mqtt_release_only",
        "update_mqtt_no_relay",
        "update_mqtt_clock_skew",
        "update_mqtt_interrupted",
        "update_mqtt_interrupted_files_reinstall",
        "update_mqtt_interrupted_files_manual",
        "update_mqtt_interrupted_unknown_reinstall",
        "update_mqtt_interrupted_unknown_manual",
        "update_mqtt_reloaded",
    }
    return keys


@pytest.mark.parametrize("language", ["strings", "en", "pl", "de"])
def test_every_sentence_exists_and_renders_in_every_language(language: str) -> None:
    catalogue = _catalogue(language)
    placeholders = mqtt_update.sentence_placeholders(None, TARGET, {"error": "x"})
    for key in sorted(_every_key()):
        message = catalogue[key]["message"]
        rendered = message.format(**placeholders)
        assert "{" not in rendered, key
        assert rendered.strip() == rendered and rendered, key


def test_the_polish_sentences_are_the_design_s() -> None:
    catalogue = _catalogue("pl")
    placeholders = mqtt_update.sentence_placeholders(
        {"target": TARGET, "from": "0.3.0"}, TARGET, {"url": "http://192.0.2.5:8123"}
    )

    def said(key: str) -> str:
        return catalogue[key]["message"].format(**placeholders)

    assert said("update_mqtt_question") == (
        "Dekoder nie uruchomił ponownie interfejsu (na ekranie odpowiedziano „nie” albo nikt "
        "nie odpowiedział). Aktualizację wycofano."
    )
    assert said("update_mqtt_not_started") == (
        "Nowa wersja wtyczki nie uruchomiła się. Dekoder przywrócił poprzednią wersję 0.3.0."
    )
    assert said("update_mqtt_busy") == (
        "Na dekoderze trwa już instalacja, aktualizacja albo usuwanie wtyczki."
    )
    assert said("update_mqtt_recording") == (
        "Dekoder nagrywa albo za chwilę zacznie nagrywać. Spróbuj ponownie po zakończeniu "
        "nagrania."
    )
    assert said("update_mqtt_still_running") == "Aktualizacja wciąż trwa na dekoderze."
    assert said("update_mqtt_download") == (
        "Dekoder nie mógł pobrać wtyczki z Home Assistant (http://192.0.2.5:8123). Sprawdź, czy "
        "dekoder ma dostęp do Home Assistant w sieci lokalnej."
    )
    assert said("update_standby").startswith("Dekoder jest w trybie czuwania.")


def test_every_reason_the_contract_names_has_a_sentence_of_its_own() -> None:
    """The refusals of `cmd/update` and the helper's reasons, as the plugin documents them."""
    refusals = [
        "not_permitted", "no_capability", "busy", "opkg_busy", "standby", "recording",
        "recording_due", "recording_unknown", "epg_import", "cannot_restart", "bad_request",
        "unknown_version", "withdrawn", "below_floor", "incompatible", "depends", "downgrade",
        "current", "checksum", "relay", "no_space", "rate_limited", "no_relay", "clock_skew",
        "internal_error",
    ]
    for reason in refusals:
        key, _ = mqtt_update.refusal_sentence(reason, "")
        assert key != "update_mqtt_refused", reason
    helper = [
        "busy", "bad_request", "unreachable", "unknown_version", "withdrawn", "below_floor",
        "incompatible", "depends", "downgrade", "checksum", "relay", "download", "bad_package",
        "no_space", "opkg_busy", "snapshot_failed", "opkg_failed", "manifest", "time_limit",
        "internal_error", "restore_failed", "restore_incomplete", "not_stopped",
        "interface_not_started",
    ]
    for reason in helper:
        transaction = mqtt_update.parse_update(
            json.loads(_transaction("finished", result="failed", reason=reason))
        )
        key, _ = mqtt_update.end_sentence(transaction)
        assert key != "update_mqtt_failed", reason


@pytest.mark.parametrize(
    ("change", "readable"),
    [
        ({}, True),
        ({"id": "A1B2C3D4E5F6"}, False),
        ({"id": "a1b2c3"}, False),
        ({"phase": "sleeping"}, True),
        ({"started": "yesterday"}, True),
    ],
)
def test_a_transaction_is_read_member_by_member(change: dict[str, Any], readable: bool) -> None:
    payload = json.loads(_transaction("installing"))
    payload["transaction"].update(change)
    transaction = mqtt_update.parse_update(payload)
    assert (transaction is not None) is readable
    if transaction is not None and "phase" in change:
        assert transaction["phase"] is None
        # An unknown phase is not a running transaction.
        assert mqtt_update.transaction_state(transaction, dt_util.utcnow().timestamp()) == "stale"


def test_a_result_counts_only_with_finished_and_a_reason_only_without_installed() -> None:
    payload = json.loads(_transaction("installing", result="installed"))
    assert mqtt_update.parse_update(payload)["result"] is None
    payload = json.loads(_transaction("finished", result="installed", reason="not_started"))
    assert mqtt_update.parse_update(payload)["reason"] is None


def test_the_bounds_cover_the_helper_s_worst_case() -> None:
    """The helper holds a transaction for at most 1353 s (the plugin's TRANSACTION.md 2.4); a
    follower needs that plus the minute of clock skew it allows. 25 minutes leaves room for the
    file copying the measurement leaves out."""
    assert mqtt_update.MQTT_UPDATE_FOLLOW == 25 * 60
    assert mqtt_update.MQTT_UPDATE_FOLLOW >= 1353 + mqtt_update.MQTT_UPDATE_FUTURE_TOLERANCE
    assert mqtt_update.MQTT_UPDATE_FUTURE_TOLERANCE == 60
    assert mqtt_update.MQTT_UPDATE_ANSWER_TIMEOUT == 60
    assert mqtt_update.MQTT_UPDATE_PROOF_TIMEOUT == 120
    assert mqtt_update.MQTT_UPDATE_END_WAIT == 10


@pytest.mark.parametrize(
    ("started", "state"),
    [
        (1_000_000 + 60, "in_progress"),
        (1_000_000 + 61, "stale"),
        (1_000_000 - 25 * 60, "in_progress"),
        (1_000_000 - 25 * 60 - 1, "stale"),
    ],
)
def test_the_window_edges_to_the_second(started: int, state: str) -> None:
    payload = json.loads(_transaction("installing", started=started))
    transaction = mqtt_update.parse_update(payload)
    assert mqtt_update.transaction_state(transaction, 1_000_000) == state


# ---------------------------------------------------------------- review round 1 --

# The plugin's words, as it says them (`selfupdate._helper_died`, `_verdict` and `_end`, and the
# helper's `interrupted` sentences). A helper that stopped once the package manager had started:
# the transaction's end ...
DIED_AFTER = (
    "the update was interrupted: the update helper stopped after the package manager had "
    "started on the plugin's files"
)
# ... and its repeat on `last_error`, with `selfupdate.STUCK_STOPPED` appended: the doors stay
# closed.
STUCK_STOPPED = (
    DIED_AFTER + "; an update stopped part-way, so the plugin's files may not be the running "
    "version's; install the plugin again (from Home Assistant: force plugin reinstall)"
)
# A helper this interface did not start, stopped with the plugin's files perhaps changed.
FOLLOWER_TOUCHED = (
    "the update was interrupted: the update helper stopped; the plugin's files may not be the "
    "running version's - restart the receiver's interface, or install the plugin again"
)
UNKNOWN_REINSTALL = "update_mqtt_interrupted_unknown_reinstall"
# The helper's and `cmd/update`'s shared sentence for `opkg_busy`.
OPKG_BUSY = "the receiver's package manager is busy"
# The helper's check between steps, made only until the package manager runs.
INTERFACE_RESTARTED = (
    "the update was interrupted: the receiver's interface restarted before the update was "
    "installed"
)
FILES_REINSTALL_EN = (
    "The update was interrupted on the receiver after its package manager had started replacing "
    "the plugin's files, so they may now be a mix of two versions. Reinstall the plugin with "
    "“Force plugin reinstall (SSH)” on the device page."
)
UNKNOWN_REINSTALL_EN = (
    "The update was interrupted on the receiver, and Home Assistant cannot tell whether the "
    "plugin's files had already been changed. If the receiver's “Last error” sensor says "
    "the plugin must be installed again, use “Force plugin reinstall (SSH)” on the device "
    "page."
)


@pytest.mark.parametrize(
    ("credentials", "key"),
    [
        (True, "update_mqtt_interrupted_files_reinstall"),
        (False, "update_mqtt_interrupted_files_manual"),
    ],
)
async def test_an_interruption_after_the_package_manager_ran_names_the_repair(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    credentials: bool,
    key: str,
) -> None:
    """`interrupted` after `installing` may have left a mix of two versions, and the receiver
    keeps its doors closed until the plugin is installed again: never "nothing was changed"."""
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, credentials=credentials
    )
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("installing")
        await _settle()
        receiver.phase("finished", result="interrupted", reason="interrupted", error="stopped")
        raised = await _refused(task)

    assert raised.translation_key == key
    ssh.assert_not_awaited()
    message = hass.states.get(PLUGIN).attributes["receiver_transaction"]["message"]
    assert "Nothing was changed" not in message
    if credentials:
        assert message == FILES_REINSTALL_EN
    else:
        assert "by hand" in message
        assert "Force plugin reinstall" not in message


@pytest.mark.parametrize("phases", [[], ["downloading"], ["downloading", "verifying", "snapshot"]])
@pytest.mark.parametrize(
    ("error", "key"),
    [
        # The one `interrupted` whose own sentence says the package manager never ran: the
        # helper looks for the interface that asked only until it starts the package manager.
        (INTERFACE_RESTARTED, "update_mqtt_interrupted"),
        # A signal, or the lock taken, can come after the package manager ran as well as
        # before: the phases seen do not prove which, since the next one can be lost between
        # two of the plugin's polls.
        ("the update was interrupted: signal 15", UNKNOWN_REINSTALL),
        ("the update was interrupted: the lock was taken", UNKNOWN_REINSTALL),
    ],
    ids=["interface restarted", "signal", "lock taken"],
)
async def test_nothing_changed_only_when_the_receiver_says_so(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
    phases: list[str],
    error: str,
    key: str,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, "accept" if phases else None)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        for phase in phases[1:]:
            receiver.phase(phase)
            await _settle()
        receiver.phase("finished", result="interrupted", reason="interrupted", error=error)
        # The plugin repeats the end on `last_error` - the same sentence, nothing appended.
        receiver.last_error(error, "interrupted")
        raised = await _refused(task)

    assert raised.translation_key == key
    await hass.async_block_till_done()
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["last_refusal"] is None
    message = attributes["receiver_transaction"]["message"]
    if key == "update_mqtt_interrupted":
        assert "Nothing was changed" in message
    else:
        assert message == UNKNOWN_REINSTALL_EN


@pytest.mark.parametrize("repeated", [True, False], ids=["with last_error", "update alone"])
async def test_a_helper_that_died_after_the_package_manager_started_is_said_as_such(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
    repeated: bool,
) -> None:
    """The helper wrote `installing` and died before the plugin's next poll, so Home Assistant
    saw `snapshot` last. The plugin's end says the package manager had started; its
    `last_error` adds the reinstall and its doors stay closed. Never "nothing was changed"."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        for phase in ("verifying", "snapshot"):
            receiver.phase(phase)
            await _settle()
        # `selfupdate._end`: `update` first, then `last_error`.
        receiver.phase("finished", result="interrupted", reason="interrupted", error=DIED_AFTER)
        if repeated:
            receiver.last_error(STUCK_STOPPED, "interrupted")
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_interrupted_files_reinstall"
    await hass.async_block_till_done()
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["message"] == FILES_REINSTALL_EN
    assert attributes["last_refusal"] is None


async def test_a_power_loss_after_the_snapshot_cannot_tell(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    """The receiver lost power with its marker at `installing`; `installing` itself was never
    published. The build that starts says `interrupted` (`selfupdate._verdict`) with a sentence
    that does not say whether the package manager had run - so neither may Home Assistant."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()
    restarted = "the update was interrupted: the receiver restarted"

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        for phase in ("verifying", "snapshot"):
            receiver.phase(phase)
            await _settle()
        receiver.phase("finished", result="interrupted", reason="interrupted", error=restarted)
        receiver.last_error(restarted, "interrupted")
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_interrupted_unknown_reinstall"
    await hass.async_block_till_done()
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["message"] == UNKNOWN_REINSTALL_EN
    assert attributes["last_refusal"] is None


async def test_the_receiver_s_own_words_about_the_files_win_over_the_phases_seen(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """A helper this interface did not start stopped once the package manager had run: the
    plugin says the files may not be the running version's and names the reinstall in the
    transaction's own sentence, which is believed over the `downloading` seen last."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase(
            "finished", result="interrupted", reason="interrupted", error=FOLLOWER_TOUCHED
        )
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_interrupted_files_reinstall"


async def test_after_a_restart_the_retained_end_and_its_repeat_are_one_end(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """After a restart of Home Assistant the broker's two retained payloads are all there is:
    the end and its repeat. They say one end, not an end and a refusal; a later refusal is one,
    and leaves the end's sentence alone."""
    box_on_the_broker[UPDATE_TOPIC] = _transaction(
        "finished", started_by="page", result="interrupted", reason="interrupted",
        error=DIED_AFTER,
    )
    box_on_the_broker[LAST_ERROR_TOPIC] = json.dumps(
        {
            "cmd": "update",
            "error": STUCK_STOPPED,
            "reason": "interrupted",
            "ts": int(dt_util.utcnow().timestamp()),
        }
    )
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["message"] == FILES_REINSTALL_EN
    assert attributes["last_refusal"] is None

    FakeReceiver(hass).last_error("an update is already running on the receiver", "busy")
    await hass.async_block_till_done()
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["message"] == FILES_REINSTALL_EN
    assert attributes["last_refusal"]["reason"] == "busy"


async def test_an_interruption_whose_way_nobody_saw_may_have_changed_the_files(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    """The end is the first word of the transaction Home Assistant hears - its phases went by
    unseen - so it says it cannot tell, and names the repair."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, None)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("finished", result="interrupted", reason="interrupted", error="stopped")
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_interrupted_unknown_reinstall"
    assert (
        hass.states.get(PLUGIN).attributes["receiver_transaction"]["message"]
        == UNKNOWN_REINSTALL_EN
    )


async def test_an_interruption_found_on_the_broker_is_said_as_unknown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    box_on_the_broker[UPDATE_TOPIC] = _transaction(
        "finished", started_by="page", result="interrupted", reason="interrupted", error="x"
    )
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, credentials=False
    )

    message = hass.states.get(PLUGIN).attributes["receiver_transaction"]["message"]
    assert message.startswith("The update was interrupted on the receiver, and Home Assistant")
    assert "by hand" in message


@pytest.mark.parametrize("update_first", [True, False], ids=["update first", "last_error first"])
@pytest.mark.parametrize(
    ("result", "reason", "message"),
    [
        ("rolled_back", "not_started", "The new version of the plugin did not start."),
        ("withdrawn_before_restart", "question", "The receiver did not restart its interface"),
        ("rolled_back", "drill", "The acceptance drill rolled the update back on purpose."),
        ("rolled_back", "time_limit", "The update ran out of time and was undone"),
        # A reason that also refuses: the end is told apart by its sentence.
        ("failed", "busy", "The receiver is already installing"),
    ],
)
async def test_the_end_of_a_transaction_on_last_error_is_not_a_refusal(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    update_first: bool,
    result: str,
    reason: str,
    message: str,
) -> None:
    """The plugin repeats every end on `last_error`; the card says it once, by its result."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    error = "the receiver's own sentence for this end"
    end = _transaction("finished", result=result, reason=reason, error=error)
    mirrored = json.dumps({"cmd": "update", "error": error, "reason": reason, "ts": 1790500000})
    for topic, payload in (
        [(UPDATE_TOPIC, end), (LAST_ERROR_TOPIC, mirrored)]
        if update_first
        else [(LAST_ERROR_TOPIC, mirrored), (UPDATE_TOPIC, end)]
    ):
        async_fire_mqtt_message(hass, topic, payload)
        await hass.async_block_till_done()

    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["last_refusal"] is None
    assert attributes["receiver_transaction"]["message"].startswith(message)


async def test_an_end_only_reason_is_never_a_refusal(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Even without the transaction it ended - Home Assistant restarted in between."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "update", "error": "x", "reason": "time_limit", "ts": 1790500000}),
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None

    # A refusal after an end is one.
    async_fire_mqtt_message(hass, UPDATE_TOPIC, _transaction("finished", result="installed"))
    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "update", "error": "y", "reason": "rate_limited", "ts": 1790500060}),
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "rate_limited"


async def test_a_cleared_last_error_takes_the_refusal_with_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    async_fire_mqtt_message(
        hass,
        LAST_ERROR_TOPIC,
        json.dumps({"cmd": "update", "error": "x", "reason": "no_relay", "ts": 1790500000}),
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "no_relay"

    # The next command that succeeded cleared `last_error`: the refusal is not on it any more.
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "", retain=True)
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None

    async_fire_mqtt_message(
        hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "zap", "error": "no", "ts": 1790500001})
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None


async def test_a_reload_during_the_follow_ends_the_call_and_the_new_card_follows(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """Saving the options reloads the entry. The old call says so at once - not "still
    running" 25 minutes later - the download address stays, and the new card shows the
    receiver's transaction to its end."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()
    relay = async_get_relay(hass)

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        await _settle()
        (grant,) = relay.grants(NODE_ID)
        # What the broker holds for the new subscription: the phase the receiver is in.
        box_on_the_broker[UPDATE_TOPIC] = _transaction("downloading")
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        raised = await asyncio.wait_for(_refused(task), 5)

    assert raised.translation_key == "update_mqtt_reloaded"
    # The receiver may be downloading right now: the address was not taken away, and requests
    # for other versions cannot evict it until its own ten minutes are over.
    assert grant in relay.grants(NODE_ID)
    for version in ("0.3.1", "0.3.2", "0.3.3"):
        package = PackageSource(version, b"x", "ef" * 32, None, (), ORIGIN_DOWNLOAD)
        with suppress(RelayError):
            relay.grant(NODE_ID, "192.0.2.12", package)
    assert grant in relay.grants(NODE_ID)
    await hass.async_block_till_done()
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["in_progress"] is True
    assert attributes["receiver_transaction"]["state"] == "in_progress"

    receiver.report()
    receiver.phase("finished", result="installed")
    await hass.async_block_till_done()
    state = hass.states.get(PLUGIN)
    assert state.attributes["installed_version"] == TARGET
    assert state.attributes["in_progress"] is False
    assert state.attributes["receiver_transaction"]["state"] == "finished"


async def test_no_answer_keeps_the_address_for_a_late_start(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    """The sentence says a late start shows on the card; so the address it downloads from is
    still there - until its own ten minutes are over."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, None)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        raised = await _refused(_install(hass))

    assert raised.translation_key == "update_mqtt_no_answer"
    (grant,) = async_get_relay(hass).grants(NODE_ID)
    assert grant.held == 0
    receiver.phase("downloading")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["in_progress"] is True
    assert async_get_relay(hass).grants(NODE_ID) == [grant]


async def test_the_card_agrees_with_the_receiver_once_the_call_is_over(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    """Home Assistant's update component clears the spinner as the call ends; the receiver's
    transaction is still inside its window, so the card shows it again right after."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase(
            "rolling_back", started=(dt_util.utcnow() + timedelta(seconds=30)).timestamp()
        )
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_still_running"
    await _settle()
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["state"] == "in_progress"
    assert attributes["in_progress"] is True


def test_the_sentences_hold_for_every_case_of_their_reason() -> None:
    english, polish, german = _catalogue("en"), _catalogue("pl"), _catalogue("de")
    # `internal_error` also ends an update after the package manager ran and the old files went
    # back: never "before anything was changed", always "the previous version stays".
    assert "before anything" not in english["update_mqtt_internal_error"]["message"]
    assert "still in place" in english["update_mqtt_internal_error"]["message"]
    assert "zanim" not in polish["update_mqtt_internal_error"]["message"]
    assert "bevor" not in german["update_mqtt_internal_error"]["message"]
    # `busy` is also the refusal while the plugin is being removed.
    assert "removing" in english["update_mqtt_busy"]["message"]
    assert "usuwanie" in polish["update_mqtt_busy"]["message"]
    assert "entfernt" in german["update_mqtt_busy"]["message"]
    # `relay` covers an address the receiver's clock already calls expired.
    assert "clock" in english["update_mqtt_relay"]["message"]
    assert "zegar" in polish["update_mqtt_relay"]["message"]
    assert "Uhr" in german["update_mqtt_relay"]["message"]


def test_the_polish_interruption_sentences_name_the_repair() -> None:
    polish = _catalogue("pl")
    assert polish["update_mqtt_interrupted_files_reinstall"]["message"] == (
        "Aktualizację przerwano na dekoderze, gdy menedżer pakietów zaczął już podmieniać pliki "
        "wtyczki, więc mogą one teraz pochodzić z dwóch różnych wersji. Zainstaluj wtyczkę od "
        "nowa przyciskiem „Wymuś reinstalację wtyczki (SSH)” na stronie urządzenia."
    )
    assert "Nic nie zmieniono" not in polish["update_mqtt_interrupted_unknown_reinstall"]["message"]
    assert "ręcznie" in polish["update_mqtt_interrupted_files_manual"]["message"]
    assert "ręcznie" in polish["update_mqtt_interrupted_unknown_manual"]["message"]
    german = _catalogue("de")
    assert "Installiere" in german["update_mqtt_interrupted_files_manual"]["message"]


async def test_a_release_the_index_does_not_list_is_never_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle built as a release the held index predates: a release number, and still not a
    release the receiver's index lists - it would only be refused there."""
    real = _load_real_bundle()
    monkeypatch.setattr(bundle_module, "load_bundled_plugin", lambda: replace(real, build=None))
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock,
        releases=[release(TARGET, sha256=SHA)],
    )
    bundled = (await async_plugin_versions(hass, config_entry)).bundled()
    assert bundled is not None and bundled.is_release and bundled.version != TARGET

    with (
        patch(FETCH, AsyncMock(return_value=_package(bundled.version))) as fetch,
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        error = await _refused(_install(hass, bundled.display))

    assert error.translation_key == "update_mqtt_release_only"
    fetch.assert_not_awaited()
    ssh.assert_not_awaited()
    assert not _commands(mqtt_mock)


async def test_a_followed_grant_is_not_replaced_for_the_same_version(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """Another package of the same version - a forged `relay_request`, say - is refused while
    the receiver downloads from the grant Home Assistant follows."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()
    relay = async_get_relay(hass)

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        (ours,) = relay.grants(NODE_ID)
        other = PackageSource(TARGET, b"other bytes", "ef" * 32, None, (), ORIGIN_DOWNLOAD)
        with pytest.raises(RelayError) as refused:
            relay.grant(NODE_ID, "192.0.2.12", other)
        assert refused.value.translation_key == "relay_busy"
        assert relay.grants(NODE_ID) == [ours]
        receiver.report()
        receiver.phase("finished", result="installed")
        await task


async def test_a_grant_the_install_reused_outlives_its_end(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """A grant made earlier - for an install started at the television, answered on its
    `relay_request` - and reused by Home Assistant's own request is not taken away when that
    request is refused: the television's download may be using it."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    relay = async_get_relay(hass)
    earlier = relay.grant(NODE_ID, "192.0.2.12", _package())
    await FakeReceiver(hass, ("busy", "an update is already running on the receiver")).arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        raised = await _refused(_install(hass))

    assert raised.translation_key == "update_mqtt_busy"
    assert relay.grants(NODE_ID) == [earlier]
    assert earlier.held == 0


async def test_a_retained_complaint_is_not_the_refusal(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    """The broker's replay of an old refusal - HA's MQTT client reconnected in the minute -
    says nothing about this request."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, None)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps({"cmd": "update", "error": "x", "reason": "busy", "ts": 1}),
            retain=True,
        )
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_no_answer"


def test_a_reason_that_is_not_a_code_word_is_not_repeated() -> None:
    payload = json.loads(
        _transaction("finished", result="failed", reason="Not A Code", error="its words")
    )
    transaction = mqtt_update.parse_update(payload)
    assert transaction["reason"] is None
    assert mqtt_update.end_sentence(transaction) == ("update_mqtt_failed", {"error": "its words"})


async def test_an_entry_without_a_commit_is_proven_by_its_version(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=replace(_package(), commit=None))):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report(TARGET, "f" * 40)
        receiver.phase("finished", result="installed")
        await asyncio.wait_for(task, 5)


async def test_a_refusal_after_an_end_of_the_same_reason_is_a_refusal(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The helper and `cmd/update` share their sentences, so an end and a later refusal can
    read alike word for word; the refusal is told apart by when the receiver said it."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp() - 600
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="failed",
        reason="opkg_busy", error=OPKG_BUSY,
    )
    receiver.last_error(OPKG_BUSY, "opkg_busy", ts=finished + 1)
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None

    # Ten minutes later, an install started at the television is refused the same way.
    receiver.last_error(OPKG_BUSY, "opkg_busy")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "opkg_busy"


async def test_a_refusal_in_other_words_right_after_an_end_is_a_refusal(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Inside the end's two minutes, a `busy` in the words of a lock left by a stopped helper is
    not the repeat of an end said `busy` in the words of one still running."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    running = "an update is already running on the receiver"
    receiver.phase("finished", result="failed", reason="busy", error=running)
    receiver.last_error(running, "busy")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None

    receiver.last_error(
        "the previous update stopped without finishing; a new one is possible in about 12 "
        "minutes, when its lock on the receiver expires",
        "busy",
    )
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "busy"


@pytest.mark.parametrize("later", [5, 600], ids=["seconds later", "ten minutes later"])
async def test_a_refusal_in_the_words_of_the_last_end_is_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    later: int,
) -> None:
    """The last transaction ended `failed`/`opkg_busy`, and its end was said on `last_error`;
    the next press is refused `opkg_busy` in the same sentence. That is the answer - not the
    old end again, which would be waited out as silence and keep the address ten minutes."""
    finished = dt_util.utcnow().timestamp() - later
    box_on_the_broker[UPDATE_TOPIC] = _transaction(
        "finished", ident="0123456789ab", started=finished - 30, finished=finished,
        result="failed", reason="opkg_busy", error=OPKG_BUSY,
    )
    box_on_the_broker[LAST_ERROR_TOPIC] = json.dumps(
        {"cmd": "update", "error": OPKG_BUSY, "reason": "opkg_busy", "ts": int(finished)}
    )
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    await FakeReceiver(hass, ("opkg_busy", OPKG_BUSY)).arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        raised = await asyncio.wait_for(_refused(_install(hass)), 5)

    assert raised.translation_key == "update_mqtt_opkg_busy"
    assert async_get_relay(hass).grants(NODE_ID) == []


async def test_an_end_repeated_on_last_error_is_not_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """The plugin says an earlier transaction's end again - a fresh session after its doors
    reopened - in the minute after the request: that is not a refusal of it."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, None)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        async_fire_mqtt_message(
            hass,
            LAST_ERROR_TOPIC,
            json.dumps(
                {
                    "cmd": "update",
                    "error": "the new plugin did not start; the previous version 0.3.0 is back",
                    "reason": "not_started",
                    "ts": 1,
                }
            ),
        )
        await _settle()
        assert not task.done()
        receiver.phase("downloading")
        await _settle()
        receiver.report()
        receiver.phase("finished", result="installed")
        await asyncio.wait_for(task, 5)


async def test_a_reload_before_the_answer_ends_the_call_at_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """The entry reloads while the request waits for its answer: the call says so, not "did
    not answer" a minute later."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, None)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        await _settle()
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        raised = await asyncio.wait_for(_refused(task), 5)

    assert raised.translation_key == "update_mqtt_reloaded"


async def test_a_request_that_never_went_out_takes_its_address_back(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """Publishing the command failed: no receiver was told the address, so none will come."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    relay = async_get_relay(hass)

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch.object(
            Enigma2Box, "async_publish_cmd", AsyncMock(side_effect=HomeAssistantError("down"))
        ),
    ):
        await _refused(_install(hass))

    assert relay.grants(NODE_ID) == []
    assert not _commands(mqtt_mock)

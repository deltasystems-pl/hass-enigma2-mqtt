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
import logging
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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
from custom_components.enigma2_mqtt.const import CAPABILITY_COMMAND_ID
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
    opened_command,
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

# The receiver of these scenarios runs 0.3.0 - the release before the one that updates itself -
# whatever version this integration bundles.
SELF_UPDATING = {
    **INFO,
    "plugin": "0.3.0",
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
    assert isinstance(error, ServiceValidationError)
    fetch.assert_not_awaited()
    assert not _commands(mqtt_mock)


async def test_a_build_the_index_does_not_list_is_never_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`update.install` without a version and nothing offered would reinstall the bundle over
    SSH; over MQTT the receiver can only take a signed release, so it is refused here."""
    real = _load_real_bundle()
    development = {**(real.build or {}), "flavour": "development"}
    monkeypatch.setattr(
        bundle_module, "load_bundled_plugin", lambda: replace(real, build=development)
    )
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, releases=[release("0.3.0")]
    )
    bundled = (await async_plugin_versions(hass, config_entry)).bundled()
    assert bundled is not None and not bundled.is_release

    with patch(SSH_INSTALL, AsyncMock()) as ssh:
        error = await _refused(_install(hass, bundled.display))

    assert error.translation_key == "update_mqtt_release_only"
    assert isinstance(error, ServiceValidationError)
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
# The refusals a household can act on or wait out - the receiver's own setting, the
# moment, the version asked for. They are raised as a refusal; a request the receiver
# could not read, an image that cannot restart, a helper that would not start and a
# reason nobody knows are raised as a failure.
ACTIONABLE = {
    "not_permitted",
    "no_capability",
    "busy",
    "opkg_busy",
    "standby",
    "recording",
    "recording_due",
    "recording_unknown",
    "epg_import",
    "unknown_version",
    "withdrawn",
    "below_floor",
    "incompatible",
    "depends",
    "downgrade",
    "current",
    "no_space",
    "rate_limited",
}


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
    assert isinstance(raised, ServiceValidationError) is (reason in ACTIONABLE)
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
    ("said", "minutes"), [("1", "1"), ("30", "30"), ("31", "30"), ("45", "30"), ("9999", "30")]
)
def test_the_receiver_s_minutes_are_never_more_than_a_lock_is_honoured_for(
    said: str, minutes: str
) -> None:
    """The number is read out of the receiver's sentence, and a lock is stale thirty
    minutes after its last heartbeat whatever that sentence says."""
    error = f"the previous update stopped; a new one is possible in about {said} minutes"

    assert mqtt_update.refusal_sentence("busy", error) == (
        "update_busy_stalled", {"minutes": minutes}
    )


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


async def test_a_second_install_while_one_is_followed_is_a_refusal_and_ends_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """Asked of the card itself, past Home Assistant's own check for an install in progress:
    the second request is refused for the moment, and the first one's spinner stays."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()
    entity = hass.data["entity_components"]["update"].get_entity(PLUGIN)

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        with pytest.raises(HomeAssistantError) as raised:
            await entity.async_install(version=None, backup=False)
        assert raised.value.translation_key == "update_mqtt_busy"
        assert isinstance(raised.value, ServiceValidationError)
        assert not task.done()
        assert hass.states.get(PLUGIN).attributes["in_progress"] is True
        assert len(receiver.requests) == 1
        receiver.report()
        receiver.phase("finished", result="installed")
        await task


# ----------------------------------- plugin 0.5.0 clears `last_error` at a start --
#
# From 0.5.0 the first connect of a new plugin process sends the empty retained `last_error`
# before `online`, unless that process has an end of its own to report - published in its
# place, with no empty payload in between - or the last update's record ends in a restore
# that did not complete, when nothing is sent and the sentence stays (the plugin's TOPICS.md,
# `last-error-cleared-at-start`). Every sequence here is live and in the plugin's order.

AVAILABILITY = f"{BASE_TOPIC}/{NODE_ID}/availability"
REINSTALL = (
    "an update failed and the plugin's previous files could not be put back; install the "
    "plugin again (from Home Assistant: force plugin reinstall)"
)


async def test_a_new_plugin_process_takes_the_refusal_off_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The refusal was of a run that is over. The card shows it only while it is on the
    topic, and a 0.5.0 plugin that starts takes it off before it says `online`."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    FakeReceiver(hass).last_error("an update ran less than ten minutes ago", "rate_limited")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "rate_limited"

    async_fire_mqtt_message(hass, AVAILABILITY, "offline")
    async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
    async_fire_mqtt_message(hass, AVAILABILITY, "online")
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(SELF_UPDATING))
    await hass.async_block_till_done()

    state = hass.states.get(PLUGIN)
    assert state.state != "unavailable"
    assert state.attributes["last_refusal"] is None


@pytest.mark.parametrize("credentials", [True, False], ids=["ssh stored", "no ssh"])
async def test_a_restore_that_did_not_complete_is_still_said_after_a_restart(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    credentials: bool,
) -> None:
    """The one case in which a starting 0.5.0 plugin clears nothing: the record of the last
    update ends `restore_failed`, the sentence on `last_error` says to install the plugin
    again, and a restart is not that repair. The card says the end from `update` either way,
    and does not turn the sentence that stayed into a refusal of a new update."""
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, credentials=credentials
    )
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp() - 30
    receiver.phase(
        "finished", started_by="page", started=finished - 60, finished=finished,
        result="failed", reason="restore_failed", error=REINSTALL,
    )
    receiver.last_error(REINSTALL, "restore_failed", ts=finished + 1)
    await hass.async_block_till_done()
    said = hass.states.get(PLUGIN).attributes["receiver_transaction"]["message"]
    assert said

    # The interface restarts. Nothing is cleared, and the new process republishes its end.
    async_fire_mqtt_message(hass, AVAILABILITY, "offline")
    async_fire_mqtt_message(hass, AVAILABILITY, "online")
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(SELF_UPDATING))
    receiver.phase(
        "finished", started_by="page", started=finished - 60, finished=finished,
        result="failed", reason="restore_failed", error=REINSTALL,
    )
    await hass.async_block_till_done()

    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["result"] == "failed"
    assert attributes["receiver_transaction"]["reason"] == "restore_failed"
    assert attributes["receiver_transaction"]["message"] == said
    assert ("force plugin reinstall" in said.lower()) is credentials
    assert attributes["last_refusal"] is None
    assert attributes["in_progress"] is False


async def test_an_update_is_followed_through_a_restart_that_clears_an_older_complaint(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """The update's own restart starts a new plugin process, which clears what an earlier
    run left on `last_error` before it says `online`. The empty payload is no word about
    the update: not a refusal, not its end, and nothing the proof waits for."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    async_fire_mqtt_message(
        hass, LAST_ERROR_TOPIC, json.dumps({"cmd": "zap", "error": "no such channel", "ts": 1})
    )
    await hass.async_block_till_done()
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        for phase in ("installing", "restarting"):
            receiver.phase(phase)
        await _settle()
        async_fire_mqtt_message(hass, AVAILABILITY, "offline")
        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
        async_fire_mqtt_message(hass, AVAILABILITY, "online")
        await _settle()
        assert not task.done()
        receiver.report()
        receiver.phase("proving")
        receiver.phase("finished", result="installed")
        await task

    assert hass.states.get(PLUGIN).attributes["installed_version"] == TARGET
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None


async def test_the_end_a_starting_process_reports_in_place_of_the_clearing_is_the_end(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """A release that did not start is rolled back, and the restored plugin reports that at
    its first connect - on `last_error` before `online`, with no empty payload first, then
    on `update`. The call ends with the rollback's sentence, said once."""
    not_started = "the new version did not start; the previous version is back"
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        for phase in ("installing", "restarting", "proving", "rolling_back"):
            receiver.phase(phase)
        await _settle()
        async_fire_mqtt_message(hass, AVAILABILITY, "offline")
        receiver.last_error(not_started, "not_started")
        async_fire_mqtt_message(hass, AVAILABILITY, "online")
        receiver.report("0.3.0")
        await _settle()
        assert not task.done()
        receiver.phase(
            "finished", result="rolled_back", reason="not_started", error=not_started
        )
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_not_started"
    ssh.assert_not_awaited()
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["result"] == "rolled_back"
    # The end's repeat is not shown a second time as a refusal of some other update.
    assert attributes["last_refusal"] is None


async def test_a_clearing_right_after_the_request_is_no_answer_to_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    """An empty `last_error` between the request and the receiver's answer - `cmd/clear_error`
    from somewhere else, a command that succeeded - neither refuses nor accepts."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass, answer="nothing")
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, "")
        await _settle()
        assert not task.done()
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_no_answer"


# ------------------------------------------- a plugin replaced under the follow --

NO_TRANSACTION = json.dumps(
    {"origin": "unknown", "checked": None, "check_error": None, "index": None,
     "latest_compatible": None, "available": [], "transaction": None}
)


def _replaced_update(kind: str) -> str:
    """The `update` a plugin that knows nothing of the followed transaction publishes."""
    if kind == "none":
        return NO_TRANSACTION
    return _transaction(
        "finished", ident="0123456789ab", started_by="page", result="installed"
    )


@pytest.mark.parametrize("kind", ["none", "another"])
async def test_a_plugin_replaced_by_the_release_asked_for_ends_the_follow_at_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    """The release is put on the receiver by something else while the update is followed -
    by hand, over SSH, from the receiver's own menu. The plugin that starts has no record of
    the transaction and will never report its end: it says `info`, naming another build
    than the one that accepted the request, and then an `update` that does not carry the
    transaction - the order every connect of the plugin has. The receiver runs what was
    asked for, so the call ends, as it would have after 25 minutes of nothing."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        caplog.at_level(logging.INFO, logger="custom_components.enigma2_mqtt.mqtt_update"),
    ):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("snapshot")
        await _settle()
        receiver.report()
        await _settle()
        assert not task.done()
        async_fire_mqtt_message(hass, UPDATE_TOPIC, _replaced_update(kind))
        await asyncio.wait_for(task, 5)

    assert "no longer reports transaction" in caplog.text
    assert hass.states.get(PLUGIN).attributes["in_progress"] is False
    assert hass.states.get(PLUGIN).attributes["installed_version"] == TARGET
    record = config_entry.runtime_data.last_install
    assert (record["path"], record["version"], record["proof"]) == ("mqtt", TARGET, "info_build")
    assert async_get_relay(hass).grants(NODE_ID)[0].held == 0


@pytest.mark.parametrize(
    ("version", "commit", "running"),
    [
        ("0.3.1", None, "0.3.1"),
        (TARGET, "f" * 40, TARGET),
        ("0.3.1", commit_of(TARGET), "0.3.1"),
    ],
    ids=["another version", "another build of the version", "the commit under another number"],
)
async def test_a_plugin_replaced_by_something_else_is_said_as_that(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    version: str,
    commit: str | None,
    running: str,
) -> None:
    """Not "still running", and not a failure of the update: nobody knows what it did."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report(version, commit)
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        raised = await asyncio.wait_for(_refused(task), 5)

    assert raised.translation_key == "update_mqtt_replaced"
    assert raised.translation_placeholders["running"] == running
    assert raised.translation_placeholders["version"] == TARGET
    assert not isinstance(raised, ServiceValidationError)
    ssh.assert_not_awaited()
    assert config_entry.runtime_data.last_install is None
    assert hass.states.get(PLUGIN).attributes["in_progress"] is False


@pytest.mark.parametrize("changed", ["version", "commit"])
async def test_a_replacement_is_told_by_the_version_or_by_the_build_alone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    changed: str,
) -> None:
    """The plugin that accepted the request named a version and a build commit. Another
    build of the same number - the release put over a development build - differs in the
    commit alone, and another version built from the same commit in the number alone;
    either is another plugin."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    receiver.report("0.3.0", "a" * 40)
    await hass.async_block_till_done()
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("snapshot")
        await _settle()
        if changed == "version":
            receiver.report("0.3.1", "a" * 40)
        else:
            receiver.report("0.3.0", "b" * 40)
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        raised = await asyncio.wait_for(_refused(task), 5)

    assert raised.translation_key == "update_mqtt_replaced"
    assert config_entry.runtime_data.last_install is None


async def test_the_followed_transaction_reported_again_is_followed_on(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """An update that works: `info` names the new build while `update` still carries the
    transaction, and another transaction's payload in between - a broker client's - is
    taken back by the receiver's next word."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        await _settle()
        receiver.phase("proving")
        receiver.report()
        await _settle()
        assert not task.done()
        receiver.phase("finished", result="installed")
        await task


async def test_a_retracted_update_topic_is_not_a_replaced_plugin(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """An empty `update` says nothing about any transaction; only a payload does."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        async_fire_mqtt_message(hass, UPDATE_TOPIC, "")
        receiver.report()
        await _settle()
        assert not task.done()
        receiver.phase("finished", result="installed")
        await task


ROLLED_BACK = {
    "result": "rolled_back",
    "reason": "not_started",
    "error": "the new version did not start; the previous version is back",
}


@pytest.mark.parametrize("retain", [False, True], ids=["live", "retained"])
@pytest.mark.parametrize("kind", ["none", "another"])
@pytest.mark.parametrize(
    "when", ["before the new plugin starts", "after its first update", "after its second info"]
)
async def test_a_stray_update_payload_never_ends_a_followed_update_as_installed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    retain: bool,
    kind: str,
    when: str,
) -> None:
    """An `update` payload without the followed transaction that no starting plugin said -
    a leftover, a broker client's, the broker's replay - while the update goes its way: the
    new plugin starts and names the target on `info`, and carries the transaction on its
    own `update`. The stray payload is not that plugin's word, before it speaks or after,
    so the follow goes on, and the end the helper writes - here the previous version put
    back - is the one that is said. Nothing is recorded as installed."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    def stray() -> None:
        async_fire_mqtt_message(hass, UPDATE_TOPIC, _replaced_update(kind), retain=retain)

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("restarting")
        await _settle()
        if when == "before the new plugin starts":
            stray()
            await _settle()
        receiver.report()
        await _settle()
        assert not task.done()
        receiver.phase("proving")
        await _settle()
        if when == "after its second info":
            # A capability that binds late: the same plugin says `info` again.
            receiver.report()
            await _settle()
        if when != "before the new plugin starts":
            stray()
            await _settle()
        assert not task.done()
        receiver.phase("finished", **ROLLED_BACK)
        raised = await asyncio.wait_for(_refused(task), 5)

    assert raised.translation_key == "update_mqtt_not_started"
    assert config_entry.runtime_data.last_install is None


def _replay(entry: MockConfigEntry, topic: str, payload: str) -> None:
    """Hand the box a retained payload, as the broker replays it to a new subscription.

    Straight to the box: Home Assistant's MQTT client delivers a retained payload to a
    subscription once per topic, and the box had its retained `info` when it was set up.
    """
    entry.runtime_data._message_received(
        ReceiveMessage(topic, payload, 0, True, f"{BASE_TOPIC}/{NODE_ID}/#", 0.0)
    )


@pytest.mark.parametrize("replayed", ["info and update", "info", "update"])
async def test_a_replay_of_the_broker_is_no_plugin_starting(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
    replayed: str,
) -> None:
    """Home Assistant's own reconnect to the broker replays what is retained there, in no
    order that says who spoke first. A replayed `info` naming the target is not a plugin
    heard to start, and a replayed `update` without the transaction is nobody's first
    word - and neither is a live `update` after either, since what that plugin said first
    was not heard. Followed to the bound, and said as still running."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        if "info" in replayed:
            _replay(config_entry, INFO_TOPIC, json.dumps({**SELF_UPDATING, "plugin": TARGET}))
        else:
            receiver.report()
        await _settle()
        if "update" in replayed:
            _replay(config_entry, UPDATE_TOPIC, NO_TRANSACTION)
            await _settle()
            assert not task.done()
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        await _settle()
        assert not task.done()
        raised = await asyncio.wait_for(_refused(task), 10)

    assert raised.translation_key == "update_mqtt_still_running"
    assert config_entry.runtime_data.last_install is None


@pytest.mark.parametrize("then", [None, "{}"], ids=["as it is", "then an empty info"])
async def test_the_build_that_accepted_put_back_is_followed_to_the_bound(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
    then: str | None,
) -> None:
    """Another plugin spoke and then the very build that accepted the request speaks
    again, with an `update` that does not carry the transaction. That cannot be told from
    the plugin that accepted it, and is not said to be something else's install - nor
    when an `info` of `{}` follows, which names nobody."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    receiver.report("0.3.0", "a" * 40)
    await hass.async_block_till_done()
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report()
        receiver.report("0.3.0", "a" * 40)
        if then is not None:
            async_fire_mqtt_message(hass, INFO_TOPIC, then)
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        await _settle()
        assert not task.done()
        raised = await asyncio.wait_for(_refused(task), 10)

    assert raised.translation_key == "update_mqtt_still_running"


@pytest.mark.parametrize("info", ["{}", json.dumps({"capabilities": []}), ""])
async def test_an_info_that_names_no_plugin_is_nobody_starting(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    info: str,
) -> None:
    """An `info` of `{}`, one without a plugin and a retracted one say nothing of what
    runs. With a stray `update` after it the call would otherwise state as fact that
    something else installed a plugin, and name it "?"."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        async_fire_mqtt_message(hass, INFO_TOPIC, info)
        await _settle()
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        await _settle()
        assert not task.done()
        receiver.phase("proving")
        receiver.report()
        receiver.phase("finished", result="installed")
        await asyncio.wait_for(task, 5)

    assert config_entry.runtime_data.last_install["proof"] == "info_build"


@pytest.mark.parametrize(
    ("version", "key"), [("0.3.1", "update_mqtt_replaced"), (TARGET, None)]
)
async def test_the_plugin_heard_to_start_is_the_one_that_is_named(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    version: str,
    key: str | None,
) -> None:
    """An `info` of `{}` between the new plugin's `info` and its `update` takes nothing
    back and names nobody: the verdict and the sentence are about the plugin that was
    heard to start, never about "?"."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report(version)
        async_fire_mqtt_message(hass, INFO_TOPIC, "{}")
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        if key is None:
            await asyncio.wait_for(task, 5)
            assert config_entry.runtime_data.last_install["version"] == TARGET
        else:
            raised = await asyncio.wait_for(_refused(task), 5)
            assert raised.translation_key == key
            assert raised.translation_placeholders["running"] == version


async def test_a_replacement_by_a_release_without_a_signed_commit_is_told_by_its_version(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """An entry that names no build commit is proven by the version alone, here as at the
    end of a transaction."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=replace(_package(), commit=None))):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report(TARGET, "f" * 40)
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        await asyncio.wait_for(task, 5)

    assert config_entry.runtime_data.last_install["proof"] == "info_version"


async def test_a_plugin_that_knew_the_update_can_still_be_replaced_by_another(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """The first word counts for each plugin that starts: the target started and carried
    the transaction, and then yet another build was put over it, whose first `update`
    does not."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report()
        receiver.phase("proving")
        await _settle()
        assert not task.done()
        receiver.report(TARGET, "f" * 40)
        async_fire_mqtt_message(hass, UPDATE_TOPIC, NO_TRANSACTION)
        raised = await asyncio.wait_for(_refused(task), 5)

    assert raised.translation_key == "update_mqtt_replaced"
    assert raised.translation_placeholders["running"] == TARGET


# ------------------------- a phase beside a plugin that cannot update itself --

CANNOT = INFO["capabilities"]
CAN = [*INFO["capabilities"], "self_update"]


def _says(hass: HomeAssistant, capabilities: list[str] | None) -> None:
    """The plugin that runs says `info`, with these capabilities or without the member."""
    info = {key: value for key, value in SELF_UPDATING.items() if key != "capabilities"}
    if capabilities is not None:
        info["capabilities"] = capabilities
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**info, "plugin": "0.3.0"}))


async def _after(hass: HomeAssistant, freezer: FrozenDateTimeFactory, **time: int) -> str:
    """How the card reads the receiver's transaction once this much more time has passed."""
    freezer.tick(timedelta(**time))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    attributes = hass.states.get(PLUGIN).attributes
    state = attributes["receiver_transaction"]["state"]
    assert attributes["in_progress"] is (state == "in_progress")
    return state


@pytest.mark.parametrize(
    ("capabilities", "phase", "at_once", "later"),
    [
        (CANNOT, "downloading", "stale", "stale"),
        (CANNOT, "verifying", "stale", "stale"),
        (CANNOT, "snapshot", "stale", "stale"),
        (CANNOT, "installing", "in_progress", "stale"),
        (CANNOT, "restarting", "in_progress", "stale"),
        (CANNOT, "proving", "in_progress", "stale"),
        (CANNOT, "rolling_back", "in_progress", "stale"),
        (CAN, "snapshot", "in_progress", "in_progress"),
        (CAN, "proving", "in_progress", "in_progress"),
        (None, "snapshot", "in_progress", "in_progress"),
        (None, "proving", "in_progress", "in_progress"),
    ],
)
async def test_a_phase_beside_a_plugin_that_cannot_update_itself_is_shown_while_it_can_move(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    capabilities: list[str] | None,
    phase: str,
    at_once: str,
    later: str,
) -> None:
    """A plugin that says it cannot update itself starts no transaction. A phase before
    `installing` - which only the plugin that asked reports - is then the plugin's before
    it, and the card stops showing it at once. From `installing` on the helper works
    outside the interface: it may still be proving the plugin that runs or putting the
    previous one back, so the phase is shown until `update` has stood still for ten
    minutes. A plugin that can, or has not said, keeps the 25-minute window, as before."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    FakeReceiver(hass).phase(phase, started_by="screen")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["in_progress"] is True

    _says(hass, capabilities)

    assert await _after(hass, freezer, seconds=0) == at_once
    assert await _after(hass, freezer, minutes=9, seconds=58) == at_once
    assert await _after(hass, freezer, seconds=4) == later


@pytest.mark.parametrize(
    ("retain", "state"), [(False, "in_progress"), (True, "stale")], ids=["live", "retained"]
)
async def test_a_word_on_update_is_the_update_moving_and_a_replay_is_not(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    retain: bool,
    state: str,
) -> None:
    """A plugin that started on the helper's marker reports the transaction to its end
    whether it claims the capability or not. Each word of it on `update` starts the ten
    minutes again; the broker's replay of the phase is not such a word."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    started = dt_util.utcnow().timestamp()
    receiver.phase("proving", started_by="screen", started=started)
    await hass.async_block_till_done()
    _says(hass, CANNOT)
    assert await _after(hass, freezer, minutes=9) == "in_progress"

    receiver.phase("rolling_back", started_by="screen", started=started, retain=retain)

    assert await _after(hass, freezer, minutes=1, seconds=2) == state
    assert await _after(hass, freezer, minutes=8, seconds=56) == state
    assert await _after(hass, freezer, seconds=4) == "stale"


async def test_the_window_still_closes_before_the_ten_minutes_are_over(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The ten minutes never keep a phase longer than its 25-minute window does."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    started = (dt_util.utcnow() - timedelta(minutes=20)).timestamp()
    FakeReceiver(hass).phase("proving", started_by="screen", started=started)
    await hass.async_block_till_done()
    _says(hass, CANNOT)

    assert await _after(hass, freezer, minutes=4, seconds=50) == "in_progress"
    assert await _after(hass, freezer, seconds=12) == "stale"


async def test_a_card_built_beside_such_a_plugin_counts_from_its_first_look(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a restart of Home Assistant or a reload the phase and the plugin's `info` are
    both the broker's replay, and how long the topic has stood still is not known: the
    card gives it the ten minutes from when it was built."""
    box_on_the_broker[UPDATE_TOPIC] = _transaction("proving", started_by="screen")
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock,
        info={**SELF_UPDATING, "capabilities": CANNOT},
    )

    assert await _after(hass, freezer, seconds=0) == "in_progress"
    assert await _after(hass, freezer, minutes=10, seconds=2) == "stale"


async def test_the_ten_minutes_start_again_with_each_plugin_that_cannot(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rollback restarts the interface more than once. A plugin that can update itself
    in between ends the count, and the next one that cannot starts its own."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    FakeReceiver(hass).phase("rolling_back", started_by="screen")
    await hass.async_block_till_done()
    _says(hass, CANNOT)
    assert await _after(hass, freezer, minutes=9) == "in_progress"
    _says(hass, CAN)
    assert await _after(hass, freezer, minutes=2) == "in_progress"

    _says(hass, CANNOT)

    assert await _after(hass, freezer, minutes=9) == "in_progress"
    assert await _after(hass, freezer, minutes=1, seconds=2) == "stale"


@pytest.mark.parametrize(
    ("between", "state"),
    [
        ("a reload", "stale"),
        ("a reload and another phase", "in_progress"),
        ("a reload and another transaction", "in_progress"),
        ("a plugin that can, and a reload", "in_progress"),
        ("a restart", "in_progress"),
    ],
)
async def test_a_reload_does_not_start_the_ten_minutes_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    between: str,
    state: str,
) -> None:
    """Saving the options reloads the entry, and the card that is built finds the same
    phase replayed by the broker: it goes on with the count of the card before it, so
    reloading does not keep a phase on the card. A phase or a transaction that changed in
    between is the update moving, a plugin that could update itself ended the count, and
    after a restart of Home Assistant nothing is left to go on with: those count from
    the new card's first look."""
    started = dt_util.utcnow().timestamp()
    box_on_the_broker[UPDATE_TOPIC] = _transaction(
        "proving", started_by="screen", started=started
    )
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock,
        info={**SELF_UPDATING, "capabilities": CANNOT},
    )
    assert await _after(hass, freezer, minutes=9) == "in_progress"

    if between == "a reload and another phase":
        box_on_the_broker[UPDATE_TOPIC] = _transaction(
            "rolling_back", started_by="screen", started=started
        )
    if between == "a reload and another transaction":
        box_on_the_broker[UPDATE_TOPIC] = _transaction(
            "proving", ident="0123456789ab", started_by="screen", started=started
        )
    if between == "a plugin that can, and a reload":
        _says(hass, CAN)
        await hass.async_block_till_done()
    if between == "a restart":
        # What a restart leaves of the count: nothing.
        hass.data.pop("enigma2_mqtt_update_stood_still")
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert await _after(hass, freezer, minutes=1, seconds=2) == state
    assert await _after(hass, freezer, minutes=9) == "stale"


LATE = {
    "info then update": (INFO_TOPIC, UPDATE_TOPIC),
    "update then info": (UPDATE_TOPIC, INFO_TOPIC),
    "info with the subscription, update late": (UPDATE_TOPIC,),
}


@pytest.mark.parametrize("order", list(LATE))
@pytest.mark.parametrize(
    ("phase", "state"), [("proving", "stale"), ("rolling_back", "in_progress")],
    ids=["the same phase", "another phase"],
)
async def test_the_count_is_kept_when_the_replay_arrives_after_the_card_is_built(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    order: str,
    phase: str,
    state: str,
) -> None:
    """The order a broker produces: the reloaded entry subscribes, its card is built
    beside a box that has heard nothing, and the retained `info` and `update` arrive
    afterwards, in either order. An empty box is not a plugin that can update itself and a
    topic not heard yet is not one without a transaction, so the count the card before
    kept is still there when the replay comes - and nothing is shown as running before
    it does."""
    started = dt_util.utcnow().timestamp()
    box_on_the_broker[UPDATE_TOPIC] = _transaction(
        "proving", started_by="screen", started=started
    )
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock,
        info={**SELF_UPDATING, "capabilities": CANNOT},
    )
    assert await _after(hass, freezer, minutes=9) == "in_progress"

    burst = dict(box_on_the_broker)
    burst[UPDATE_TOPIC] = _transaction(phase, started_by="screen", started=started)
    late = LATE[order]
    for topic in late:
        del box_on_the_broker[topic]
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes.get("in_progress") is not True
    for topic in late:
        async_fire_mqtt_message(hass, topic, burst[topic], retain=True)
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=1))

    assert await _after(hass, freezer, minutes=1) == state
    assert await _after(hass, freezer, minutes=9) == "stale"


async def test_a_replay_that_never_comes_keeps_nothing_busy(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The broker goes away with the reload: the new card hears nothing, shows no
    transaction, and the kept count holds nothing on it."""
    box_on_the_broker[UPDATE_TOPIC] = _transaction("proving", started_by="screen")
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock,
        info={**SELF_UPDATING, "capabilities": CANNOT},
    )
    assert await _after(hass, freezer, minutes=9) == "in_progress"

    box_on_the_broker.clear()
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    for minutes in (0, 5, 30):
        freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert hass.states.get(PLUGIN).attributes.get("in_progress") is not True


async def test_two_receivers_keep_separate_counts(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A second receiver that starts counting later does not move the first one's count."""
    other = "vuuno4kse_005302"
    box_on_the_broker[UPDATE_TOPIC] = _transaction("proving", started_by="screen")
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock,
        info={**SELF_UPDATING, "capabilities": CANNOT},
    )
    assert await _after(hass, freezer, minutes=9) == "in_progress"

    second = MockConfigEntry(
        domain=config_entry.domain,
        title="Second",
        unique_id=other,
        data={**config_entry.data, "node_id": other, "name": "Second"},
    )
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    for suffix, payload in (
        ("availability", "online"),
        ("info", json.dumps({**SELF_UPDATING, "capabilities": CANNOT})),
        ("update", _transaction("proving", started_by="screen")),
    ):
        async_fire_mqtt_message(hass, f"{BASE_TOPIC}/{other}/{suffix}", payload, retain=True)
    await hass.async_block_till_done()
    kept = hass.data["enigma2_mqtt_update_stood_still"]
    assert set(kept) == {config_entry.entry_id, second.entry_id}
    assert kept[config_entry.entry_id][1] + 9 * 60 == kept[second.entry_id][1]

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert await _after(hass, freezer, minutes=1, seconds=2) == "stale"


async def test_the_count_of_a_removed_receiver_goes_with_its_entry(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Nothing will go on with the count of an entry that was removed: it is dropped then,
    and not with the next restart. The other receiver's count is left as it is."""
    other = "vuuno4kse_005302"
    box_on_the_broker[UPDATE_TOPIC] = _transaction("proving", started_by="screen")
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock,
        info={**SELF_UPDATING, "capabilities": CANNOT},
    )
    assert await _after(hass, freezer, minutes=9) == "in_progress"
    second = MockConfigEntry(
        domain=config_entry.domain,
        title="Second",
        unique_id=other,
        data={**config_entry.data, "node_id": other, "name": "Second"},
    )
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    for suffix, payload in (
        ("availability", "online"),
        ("info", json.dumps({**SELF_UPDATING, "capabilities": CANNOT})),
        ("update", _transaction("proving", started_by="screen")),
    ):
        async_fire_mqtt_message(hass, f"{BASE_TOPIC}/{other}/{suffix}", payload, retain=True)
    await hass.async_block_till_done()
    kept = hass.data["enigma2_mqtt_update_stood_still"]
    assert set(kept) == {config_entry.entry_id, second.entry_id}
    of_the_second = kept[second.entry_id]

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.data["enigma2_mqtt_update_stood_still"] == {second.entry_id: of_the_second}


async def test_the_count_of_a_reloaded_receiver_stays_where_it_is_kept(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reload unloads the entry and does not remove it: the record is there for the
    card that is built next, with the moment the count began."""
    box_on_the_broker[UPDATE_TOPIC] = _transaction("proving", started_by="screen")
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock,
        info={**SELF_UPDATING, "capabilities": CANNOT},
    )
    assert await _after(hass, freezer, minutes=9) == "in_progress"
    kept = dict(hass.data["enigma2_mqtt_update_stood_still"])
    assert set(kept) == {config_entry.entry_id}

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data["enigma2_mqtt_update_stood_still"] == kept

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data["enigma2_mqtt_update_stood_still"] == kept
    assert await _after(hass, freezer, minutes=1, seconds=2) == "stale"


async def test_a_retracted_info_says_nothing_of_what_the_plugin_can_do(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An empty `info` keeps the last payload for the device page and withdraws what it
    stated: the capabilities in it are no longer anybody's word, and the window decides."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    FakeReceiver(hass).phase("snapshot", started_by="screen")
    await hass.async_block_till_done()
    _says(hass, CANNOT)
    assert await _after(hass, freezer, seconds=0) == "stale"

    async_fire_mqtt_message(hass, INFO_TOPIC, "")

    assert await _after(hass, freezer, seconds=0) == "in_progress"
    assert await _after(hass, freezer, minutes=11) == "in_progress"


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
    assert not isinstance(raised, ServiceValidationError)
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
    assert not isinstance(raised, ServiceValidationError)


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
    keys.add("update_mqtt_replaced")
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
        "update_mqtt_interrupted_signal",
        "update_mqtt_interrupted_lock",
        "update_mqtt_restore_failed_manual",
        "update_mqtt_restore_incomplete_manual",
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


@pytest.mark.parametrize(
    "phases",
    [
        [],
        ["downloading"],
        ["downloading", "verifying", "snapshot"],
        # The old interface polled `installing` in the instant before it went: the helper's
        # own sentence still says where it was, and wins over the phase.
        ["downloading", "verifying", "snapshot", "installing"],
    ],
)
@pytest.mark.parametrize(
    ("error", "key"),
    [
        # The one `interrupted` whose own sentence says the package manager never ran: the
        # helper looks for the interface that asked only until it starts the package manager.
        (INTERFACE_RESTARTED, "update_mqtt_interrupted"),
        # A signal ends `interrupted` only with the files untouched or put back whole - an
        # incomplete restore ends `failed` - so the previous version stays, whatever phase
        # was seen.
        ("the update was interrupted: signal 15", "update_mqtt_interrupted_signal"),
        # The lock taken is seen only between steps: the files are one version or the other,
        # whole, and the receiver asks for an interface restart, never a reinstall.
        ("the update was interrupted: the lock was taken", "update_mqtt_interrupted_lock"),
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
    assert message == _catalogue("en")[key]["message"]
    assert "mix" not in message
    assert "Force plugin reinstall" not in message


@pytest.mark.parametrize(
    ("error", "furthest", "said", "key"),
    [
        (INTERFACE_RESTARTED, "installing", None, "update_mqtt_interrupted"),
        (INTERFACE_RESTARTED, "rolling_back", None, "update_mqtt_interrupted"),
        ("the update was interrupted: signal 9", "installing", None,
         "update_mqtt_interrupted_signal"),
        ("the update was interrupted: signal 15", None, None, "update_mqtt_interrupted_signal"),
        ("the update was interrupted: the lock was taken", "restarting", None,
         "update_mqtt_interrupted_lock"),
        # The receiver's own reinstall words win over the helper's sentence.
        ("the update was interrupted: signal 15", "installing",
         "the update was interrupted: signal 15; install the plugin again",
         "update_mqtt_interrupted_files_reinstall"),
        ("the update was interrupted: the lock was taken", None,
         "the update was interrupted: the lock was taken; install the plugin again",
         "update_mqtt_interrupted_files_reinstall"),
        # Only the helper's sentence exactly: anything added is somebody else's words.
        ("the update was interrupted: signal 15 after the package manager had started on the "
         "plugin's files", None, None, "update_mqtt_interrupted_files_reinstall"),
        ("the update was interrupted: signal 15x", "snapshot", None, UNKNOWN_REINSTALL),
        ("the update was interrupted: signal ", None, None, UNKNOWN_REINSTALL),
        ("the update was interrupted: the lock was taken, twice", "installing", None,
         "update_mqtt_interrupted_files_reinstall"),
        ("the update was interrupted: the lock was taken, twice", "snapshot", None,
         UNKNOWN_REINSTALL),
        # A power loss or a helper gone: its sentence does not say, so the phase decides.
        ("the update was interrupted: the receiver restarted", "installing", None,
         "update_mqtt_interrupted_files_reinstall"),
        ("the update was interrupted: the receiver restarted", "snapshot", None,
         UNKNOWN_REINSTALL),
    ],
)
def test_an_interruption_is_said_by_the_receiver_s_words_first(
    error: str, furthest: str | None, said: str | None, key: str
) -> None:
    transaction = mqtt_update.parse_update(
        json.loads(
            _transaction("finished", result="interrupted", reason="interrupted", error=error)
        )
    )
    assert mqtt_update.end_sentence(transaction, furthest, said=said) == (key, {})


@pytest.mark.parametrize("reason", ["restore_failed", "restore_incomplete"])
@pytest.mark.parametrize("credentials", [True, False])
def test_a_restore_that_did_not_complete_names_the_repair_there_is(
    reason: str, credentials: bool
) -> None:
    """Both name the forced reinstall, whose button exists only with SSH credentials stored;
    without them the repair is the manual installation."""
    transaction = mqtt_update.parse_update(
        json.loads(_transaction("finished", result="failed", reason=reason, error="x"))
    )
    key, _ = mqtt_update.end_sentence(transaction, repair_by_ssh=credentials)
    assert key == (f"update_mqtt_{reason}" if credentials else f"update_mqtt_{reason}_manual")
    for language in ("en", "pl", "de"):
        message = _catalogue(language)[key]["message"]
        named = "(SSH)" in message
        assert named is credentials, (language, key)


async def test_the_receiver_s_repeat_may_still_contradict_a_whole_files_sentence(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
) -> None:
    """A signal says the previous version stays; the call still waits a moment for the end's
    repeat, and reinstall words there - should a plugin ever put them only there - win."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()
    signalled = "the update was interrupted: signal 15"

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("finished", result="interrupted", reason="interrupted", error=signalled)
        await _settle()
        assert not task.done()
        receiver.last_error(signalled + "; install the plugin again", "interrupted")
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_interrupted_files_reinstall"


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
    # Stamped as the plugin stamps it: when it says it, after the transaction started.
    mirrored = json.dumps(
        {
            "cmd": "update",
            "error": error,
            "reason": reason,
            "ts": int(dt_util.utcnow().timestamp()),
        }
    )
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
    assert not isinstance(raised, ServiceValidationError)
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
        hass, config_entry, box_on_the_broker, aioclient_mock, releases=[release("0.3.0")]
    )
    bundled = (await async_plugin_versions(hass, config_entry)).bundled()
    assert bundled is not None and bundled.is_release and bundled.version != "0.3.0"

    with (
        patch(FETCH, AsyncMock(return_value=_package(bundled.version))) as fetch,
        patch(SSH_INSTALL, AsyncMock()) as ssh,
    ):
        error = await _refused(_install(hass, bundled.display))

    assert error.translation_key == "update_mqtt_release_only"
    assert isinstance(error, ServiceValidationError)
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    await receiver.arm()

    with (
        patch(FETCH, AsyncMock(return_value=replace(_package(), commit=None))),
        caplog.at_level(logging.INFO, logger="custom_components.enigma2_mqtt.mqtt_update"),
    ):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report(TARGET, "f" * 40)
        receiver.phase("finished", result="installed")
        await asyncio.wait_for(task, 5)

    # The proof is named for what it was: the version alone, in the record and in the log.
    record = config_entry.runtime_data.last_install
    assert (record["path"], record["version"], record["proof"]) == ("mqtt", TARGET, "info_version")
    assert f"runs {TARGET}. Proof: the receiver reports the version on info" in caplog.text


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


# --- the card tells a refusal in an end's words from that end's repeat -------------------


async def test_a_refusal_in_the_end_s_words_within_two_minutes_is_a_refusal_on_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The end and its repeat came together; half a minute later an install started at the
    television is refused in the same words. No `update` came with it, so it is not the end
    again: the card shows the refusal, as the call would."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp() - 30
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="failed",
        reason="opkg_busy", error=OPKG_BUSY,
    )
    receiver.last_error(OPKG_BUSY, "opkg_busy", ts=finished)
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None

    receiver.last_error(OPKG_BUSY, "opkg_busy")
    await hass.async_block_till_done()
    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["last_refusal"]["reason"] == "opkg_busy"
    # The end stays said as it was.
    assert attributes["receiver_transaction"]["result"] == "failed"


async def test_an_end_repeated_late_with_its_update_is_not_a_refusal_on_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The broker was away when the transaction ended; the plugin says the end on `update`
    and repeats it on `last_error` at its reconnect, stamped minutes after `finished`. It
    came with its `update`, so it is the end again - not a refusal in the end's words."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp() - 200
    error = "the update helper failed: KeyError"
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="failed",
        reason="internal_error", error=error,
    )
    receiver.last_error(error, "internal_error")
    await hass.async_block_till_done()

    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["last_refusal"] is None
    assert attributes["receiver_transaction"]["result"] == "failed"


async def test_a_late_repeat_carries_the_receiver_s_words_to_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The repeat that came with its `update` is the end's, whatever its stamp: its reinstall
    words reach the card's sentence as they would inside the two minutes."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp() - 600
    ended = "the update was interrupted: the update helper stopped"
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="interrupted",
        reason="interrupted", error=ended,
    )
    receiver.last_error(ended + "; install the plugin again", "interrupted")
    await hass.async_block_till_done()

    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["receiver_transaction"]["message"] == FILES_REINSTALL_EN
    assert attributes["last_refusal"] is None


@pytest.mark.parametrize(("offset", "repeated"), [(-1, False), (0, True), (5, True)])
def test_a_repeat_is_never_stamped_before_its_transaction_started(
    offset: int, repeated: bool
) -> None:
    transaction = mqtt_update.parse_update(
        json.loads(
            _transaction(
                "finished", started=1790000000, finished=1790000060, result="failed",
                reason="opkg_busy", error=OPKG_BUSY,
            )
        )
    )
    payload = {
        "cmd": "update", "error": OPKG_BUSY, "reason": "opkg_busy", "ts": 1790000000 + offset
    }
    assert (mqtt_update.end_repeated(payload, transaction) is not None) is repeated


def test_polish_says_nothing_was_changed_one_way() -> None:
    """Two phrasings of one sentence read as two different outcomes; the catalogue uses one."""
    text = (TRANSLATIONS / "pl.json").read_text(encoding="utf-8")
    assert "Nic nie zostało zmienione" not in text
    assert "Nic nie zmieniono" in text


async def test_a_complaint_long_after_its_update_is_not_that_update_s_repeat(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The burst is the end on `update` and its repeat at once. A complaint in the end's words
    that comes long after the `update`, with no repeat seen, is judged by its stamp: here,
    minutes after the end, a refusal."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp() - 300
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="failed",
        reason="opkg_busy", error=OPKG_BUSY,
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))

    receiver.last_error(OPKG_BUSY, "opkg_busy")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "opkg_busy"


def _republish(hass: HomeAssistant, finished: float, **changes: Any) -> None:
    """The plugin says `update` again with the same ended transaction - on a connect, after
    an origin check or a relayed index - with `changes` to its payload."""
    payload = json.loads(
        _transaction(
            "finished", started=finished - 30, finished=finished, result="failed",
            reason="opkg_busy", error=OPKG_BUSY,
        )
    )
    payload.update(changes)
    async_fire_mqtt_message(hass, UPDATE_TOPIC, json.dumps(payload))


async def test_a_refusal_right_after_an_unrelated_update_is_a_refusal_on_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The end and its repeat came together. Five minutes later the plugin says `update` for
    another reason - its origin check changed the origin - and a second later refuses an
    install started at the television in the end's words. Only the `update` that first carried
    the end can carry its repeat: the card shows the refusal."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp()
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="failed",
        reason="opkg_busy", error=OPKG_BUSY,
    )
    receiver.last_error(OPKG_BUSY, "opkg_busy", ts=finished)
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None

    freezer.tick(timedelta(seconds=300))
    _republish(hass, finished, origin="reachable")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=1))
    receiver.last_error(OPKG_BUSY, "opkg_busy")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "opkg_busy"


async def test_a_refusal_after_an_unrelated_update_within_two_minutes_is_a_refusal(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """As above, inside the end's two minutes, where the stamp alone would call it the repeat:
    the repeat was taken already, and the plugin says it once."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp()
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="failed",
        reason="opkg_busy", error=OPKG_BUSY,
    )
    receiver.last_error(OPKG_BUSY, "opkg_busy", ts=finished)
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=30))
    _republish(hass, finished)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=1))
    receiver.last_error(OPKG_BUSY, "opkg_busy")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "opkg_busy"


async def test_a_repeat_taken_by_its_stamp_is_taken(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A loaded receiver repeats the end twelve seconds after its `update`: past the burst, the
    stamp says it is the repeat. A refusal in the same words twenty seconds later is then a
    refusal, not the repeat a second time."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp()
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="failed",
        reason="opkg_busy", error=OPKG_BUSY,
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=12))
    receiver.last_error(OPKG_BUSY, "opkg_busy")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"] is None

    freezer.tick(timedelta(seconds=20))
    receiver.last_error(OPKG_BUSY, "opkg_busy")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "opkg_busy"


async def test_the_repeat_after_the_session_that_reopens_the_doors_is_the_end_s(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With its doors closed the plugin says the end on `update`, then reopens them with a
    fresh session, which says `update` again and only then the repeat - with the reinstall
    words. That repeat is the end's, found by its stamp, and its words reach the card."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp()
    ended = "the update was interrupted: the update helper stopped"
    for _ in range(2):
        receiver.phase(
            "finished", started=finished - 30, finished=finished, result="interrupted",
            reason="interrupted", error=ended,
        )
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=6))
    receiver.last_error(ended + "; install the plugin again", "interrupted")
    await hass.async_block_till_done()

    attributes = hass.states.get(PLUGIN).attributes
    assert attributes["last_refusal"] is None
    assert attributes["receiver_transaction"]["message"] == FILES_REINSTALL_EN


async def test_a_refusal_right_after_a_reconnect_is_a_refusal_on_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The broker hands over an end from ten minutes ago, and its repeat is no longer on
    `last_error`. The receiver reconnects - which says `update` again - and a second later
    refuses an install in the end's words. That `update` did not first carry the end, so
    the burst is not its repeat: the card shows the refusal."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    receiver = FakeReceiver(hass)
    finished = dt_util.utcnow().timestamp() - 600
    receiver.phase(
        "finished", started=finished - 30, finished=finished, result="failed",
        reason="opkg_busy", error=OPKG_BUSY, retain=True,
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=5))

    _republish(hass, finished)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=1))
    receiver.last_error(OPKG_BUSY, "opkg_busy")
    await hass.async_block_till_done()
    assert hass.states.get(PLUGIN).attributes["last_refusal"]["reason"] == "opkg_busy"


# ------------------------------------------------------------- a command's id --
#
# A plugin that claims `command_id` is sent the request in an envelope with an id, and says
# the id back on `last_error` - for a refusal, and for the end of the transaction that
# request started (the plugin's TOPICS.md, section 2). `tests/test_command_id.py` holds the
# rule itself; these are the update's two readers of `last_error`.

COMMAND_ID = re.compile(r"[0-9a-f]{16}")
SOMEBODY_ELSES = "7f9c2e4a-3b1d-4c8e-9f60-2d5a1b7c8e90"

UPDATING_WITH_IDS = {
    **SELF_UPDATING,
    "capabilities": [*SELF_UPDATING["capabilities"], CAPABILITY_COMMAND_ID],
}


class UpdatingWithIds(FakeReceiver):
    """The update's fake receiver, opening the envelope as the plugin does."""

    def __init__(self, hass: HomeAssistant, answer: Any = None) -> None:
        super().__init__(hass, answer)
        self.ids: list[str | None] = []
        self.wire: list[str] = []

    @callback
    def _received(self, msg: ReceiveMessage) -> None:
        command_id, inner = opened_command(msg.payload)
        self.ids.append(command_id)
        self.wire.append(str(msg.payload))
        self.requests.append(json.loads(inner))
        if self.answer == "accept":
            self.phase("downloading")

    def refuse(self, reason: str, error: str, command_id: Any = None) -> None:
        payload = {
            "cmd": "update",
            "error": error,
            "reason": reason,
            "ts": int(dt_util.utcnow().timestamp()),
        }
        if command_id is not None:
            payload["id"] = command_id
        async_fire_mqtt_message(self.hass, LAST_ERROR_TOPIC, json.dumps(payload))


async def test_the_update_request_goes_in_an_envelope_as_the_text_it_was(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, info=UPDATING_WITH_IDS
    )
    receiver = UpdatingWithIds(hass, "accept")
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.report()
        receiver.phase("finished", result="installed")
        await task

    (command_id,) = receiver.ids
    assert command_id is not None and COMMAND_ID.fullmatch(command_id)
    (request,) = receiver.requests
    assert set(request) == {"version", "sha256", "relay"}
    assert (request["version"], request["sha256"]) == (TARGET, SHA)
    assert RELAY_URL.fullmatch(request["relay"]["url"])
    # The request as a string, in the spelling it has for a plugin without ids.
    envelope = json.loads(receiver.wire[0])
    assert envelope == {"id": command_id, "payload": json.dumps(request, separators=(",", ":"))}


@pytest.mark.parametrize("info", [UPDATING_WITH_IDS, SELF_UPDATING], ids=["with ids", "older"])
async def test_a_refusal_of_somebody_else_s_update_is_not_the_answer_to_this_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    info: dict[str, Any],
) -> None:
    """A second `cmd/update` from another client is refused `busy` while ours is being
    taken up. It says that client's id, so it is not ours - also when ours went out bare."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock, info=info)
    receiver = UpdatingWithIds(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.refuse("busy", "another installation holds the lock", SOMEBODY_ELSES)
        await _settle()
        assert not task.done()
        receiver.phase("downloading")
        await _settle()
        receiver.report()
        receiver.phase("finished", result="installed")
        await task

    assert (receiver.ids[0] is not None) is (info is UPDATING_WITH_IDS)


@pytest.mark.parametrize("with_id", [True, False], ids=["its id", "no id"])
async def test_a_refusal_of_the_update_with_its_id_or_with_none_is_the_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    with_id: bool,
) -> None:
    """Without an id it may still be ours: the plugin drops an id that holds one of its
    secrets, and the command runs - and is refused - without it."""
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, info=UPDATING_WITH_IDS
    )
    receiver = UpdatingWithIds(hass)
    await receiver.arm()

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.refuse(
            "opkg_busy",
            "the receiver's package manager is busy",
            receiver.ids[0] if with_id else None,
        )
        raised = await _refused(task)

    assert raised.translation_key == "update_mqtt_opkg_busy"


@pytest.mark.parametrize(
    ("whose", "key"),
    [
        ("ours", "update_mqtt_interrupted_files_reinstall"),
        ("none", "update_mqtt_interrupted_files_reinstall"),
        ("another", "update_mqtt_interrupted_signal"),
    ],
)
async def test_only_the_followed_update_s_own_words_are_its_end_said_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
    short_bounds: None,
    whose: str,
    key: str,
) -> None:
    """The plugin repeats an end on `last_error` under the id of the request that started
    it. A complaint about `cmd/update` in those words under another id is another
    request's, and adds nothing to this end."""
    await _self_updating(
        hass, config_entry, box_on_the_broker, aioclient_mock, info=UPDATING_WITH_IDS
    )
    receiver = UpdatingWithIds(hass, "accept")
    await receiver.arm()
    signalled = "the update was interrupted: signal 15"

    with patch(FETCH, AsyncMock(return_value=_package())):
        task = _install(hass)
        await _until(lambda: receiver.requests)
        receiver.phase("finished", result="interrupted", reason="interrupted", error=signalled)
        await _settle()
        assert not task.done()
        said = {"ours": receiver.ids[0], "none": None, "another": SOMEBODY_ELSES}[whose]
        receiver.refuse("interrupted", signalled + "; install the plugin again", said)
        raised = await _refused(task)

    assert raised.translation_key == key


async def test_a_refusal_said_before_the_update_request_went_out_is_not_its_answer(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    lan: None,
) -> None:
    """The request waits for the broker to confirm the subscription before it is published,
    and the follow is listening by then: a complaint in that wait is an earlier request's."""
    await _self_updating(hass, config_entry, box_on_the_broker, aioclient_mock)
    box: Enigma2Box = config_entry.runtime_data
    receiver = UpdatingWithIds(hass)
    await receiver.arm()
    confirmed = asyncio.Event()
    listeners = len(box._listeners)

    with (
        patch(FETCH, AsyncMock(return_value=_package())),
        patch.object(box, "async_wait_subscribed", confirmed.wait),
    ):
        task = _install(hass)
        await _until(lambda: len(box._listeners) > listeners)
        receiver.refuse("busy", "another installation holds the lock")
        await _settle()
        assert not receiver.requests
        confirmed.set()
        await _until(lambda: receiver.requests)
        await _settle()
        assert not task.done()
        receiver.phase("downloading")
        await _settle()
        receiver.report()
        receiver.phase("finished", result="installed")
        await task

"""What the version card says, what it offers, and what „Wersja wtyczki do instalacji" changes.

Three rules meet on this entity (ADR-0008 section 3):

- **the card offers what it will install, and only when it can** - `latest_version` is the
  installed version whenever there is no install path, so no badge appears on a card that could
  not act on it (a behaviour change from 0.3.1), and the versions the signed index lists beyond
  what the card can install are said in the summary and the attributes, never offered;
- **a build is shown as what it is** - a development build of 0.3.0 reads `0.3.0+g<sha7>`, and is
  never "current" against the release of its own number (backlog item 9);
- **the select is honoured only while enabled** - disabled, its default, it counts as `latest`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.update import ATTR_VERSION
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.enigma2_mqtt import bundle as bundle_module, release_index
from custom_components.enigma2_mqtt.const import (
    CONF_CHECK_GITHUB_RELEASES,
    CONF_PLUGIN_TARGET_VERSION,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    PLUGIN_INDEX_ORIGIN,
    TARGET_LATEST,
)
from custom_components.enigma2_mqtt.plugin_versions import async_plugin_versions
from custom_components.enigma2_mqtt.release_store import async_release_index_cache
from custom_components.enigma2_mqtt.update import (
    COMPATIBILITY_MATCHED,
    COMPATIBILITY_NEWER,
    COMPATIBILITY_OLDER,
    COMPATIBILITY_UNKNOWN,
    plugin_compatibility,
)

from .conftest import (
    INFO,
    INFO_TOPIC,
    NODE_ID,
    PLUGIN_VERSION,
    async_setup_box,
    async_setup_box_then_retained,
)
from .signed_index import commit_time_of, keyset, release, sign

PLUGIN = "update.dekoder_salon_plugin"
SELECT = "select.dekoder_salon_plugin_version_to_install"
CHECK = "button.dekoder_salon_check_for_plugin_updates"
INDEX_URL = PLUGIN_INDEX_ORIGIN + "releases.json"
SIG_URL = PLUGIN_INDEX_ORIGIN + "releases.json.sig"
NOTIFY = "custom_components.enigma2_mqtt.release_store.persistent_notification.async_create"

DEV_COMMIT = "46ea0e3" + "0" * 33
CANDIDATE_COMMIT = "abc1234" + "1" * 33

CREDENTIALS = {
    CONF_SSH_HOST: "192.0.2.12",
    CONF_SSH_PORT: 22,
    CONF_SSH_USERNAME: "root",
    CONF_SSH_PASSWORD: "example-only",
    CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
}


@pytest.fixture(autouse=True)
def test_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judge by the vectors' test keys: nobody signs with the release keys but CI."""
    monkeypatch.setattr(release_index, "EMBEDDED", keyset("test"))


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    credentials: bool = False,
    options: dict[str, Any] | None = None,
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **(CREDENTIALS if credentials else {})},
        options={**entry.options, **(options or {})},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _report(hass: HomeAssistant, version: str, build: dict | None = None) -> None:
    """Make the box report a plugin version, and a build id when given."""
    info = {**INFO, "plugin": version}
    if build is not None:
        info["build"] = build
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(info))
    await hass.async_block_till_done()


async def _accept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, serial: int, releases: list
) -> None:
    """Have the cache accept a signed index, as a press of the check button would."""
    pair = sign(serial, releases)
    aioclient_mock.clear_requests()
    aioclient_mock.get(INDEX_URL, content=pair[0])
    aioclient_mock.get(SIG_URL, content=pair[1])
    cache = async_release_index_cache(hass)
    cache.checked = None
    with patch(NOTIFY):
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: CHECK}, blocking=True)
    await hass.async_block_till_done()


def _dev_build(commit: str = DEV_COMMIT, time: int = 1790500000) -> dict[str, Any]:
    return {"commit": commit, "time": time, "dirty": False, "flavour": "development",
            "on_disk": None}


# ------------------------------------------------------------ the no-badge rule --


async def test_an_older_plugin_without_credentials_shows_no_badge_but_says_how(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Behaviour change from 0.3.1: a badge without an install button is not offered."""
    await async_setup_box(hass, config_entry)
    await _report(hass, "0.2.0")

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_OFF
    assert state.attributes["latest_version"] == "0.2.0"
    assert state.attributes["installed_version"] == "0.2.0"
    assert state.attributes["supported_features"] == 0
    assert state.attributes["update_path"] is None
    summary = state.attributes["release_summary"]
    assert PLUGIN_VERSION in summary
    assert "Keep SSH credentials for updates" in summary
    assert state.attributes["compatibility"] == COMPATIBILITY_OLDER


async def test_an_older_plugin_with_credentials_is_offered_the_bundle(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "0.2.0")

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_ON
    assert state.attributes["latest_version"] == PLUGIN_VERSION
    assert state.attributes["update_path"] == "ssh"
    summary = state.attributes["release_summary"]
    assert "broker settings" in summary
    assert "Keep SSH credentials" not in summary


async def test_versions_the_index_lists_beyond_the_bundle_are_said_not_offered(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Until this integration can download a package, a newer release is information."""
    await _setup(hass, config_entry, credentials=True)
    await _accept(hass, aioclient_mock, 1, [release("0.4.0"), release("0.3.0")])

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_OFF
    assert state.attributes["latest_version"] == PLUGIN_VERSION
    assert state.attributes["published_version"] == "0.4.0"
    assert "0.4.0" in state.attributes["release_summary"]
    assert state.attributes["release_url"].endswith("/releases/tag/v0.4.0")
    assert state.attributes["index_serial"] == 1
    assert state.attributes["index_age"] is not None


async def test_a_bundle_the_index_withdrew_is_not_offered_or_installed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "0.2.0")
    assert hass.states.get(PLUGIN).state == STATE_ON

    await _accept(
        hass, aioclient_mock, 1, [release("0.3.0", withdrawn="it breaks EPG"), release("0.2.0")]
    )
    state = hass.states.get(PLUGIN)
    assert state.state == STATE_OFF
    assert state.attributes["latest_version"] == "0.2.0"
    with (
        patch("custom_components.enigma2_mqtt.update.async_install") as install,
        pytest.raises(HomeAssistantError) as raised,
    ):
        # Asked for by name, as `update.install` allows while the card offers nothing.
        await hass.services.async_call(
            "update",
            "install",
            {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
            blocking=True,
        )
    assert raised.value.translation_key == "update_version_withdrawn"
    assert raised.value.translation_placeholders == {
        "version": PLUGIN_VERSION,
        "reason": "it breaks EPG",
    }
    install.assert_not_called()


async def test_a_skip_is_dropped_when_latest_version_moves(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Home Assistant forgets a skipped version once `latest_version` is another.

    The select moves `latest_version` the same way once more than one version can be
    installed; here the move comes from an index that withdraws the skipped one.
    """
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "0.2.0")
    await hass.services.async_call("update", "skip", {ATTR_ENTITY_ID: PLUGIN}, blocking=True)
    assert hass.states.get(PLUGIN).attributes["skipped_version"] == PLUGIN_VERSION

    await _accept(hass, aioclient_mock, 1, [release("0.3.0", withdrawn="no"), release("0.2.0")])
    assert hass.states.get(PLUGIN).attributes["skipped_version"] is None


# ------------------------------------------------------------------- build ids --


async def test_a_development_build_is_never_current_against_its_release(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Item 9: the receiver ran a development build of 0.3.0 and the card called it current.

    Its build id arrives after the entities exist, as on a real broker.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, **CREDENTIALS}
    )
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "build": _dev_build()})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(PLUGIN)
    assert state.attributes["installed_version"] == "0.3.0+g46ea0e3"
    assert state.attributes["latest_version"] == PLUGIN_VERSION
    assert state.state == STATE_ON
    assert "development build" in state.attributes["release_summary"]


async def test_a_development_build_without_an_install_path_is_named_not_badged(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await async_setup_box(hass, config_entry)
    await _report(hass, "0.3.0", {**_dev_build(), "dirty": True})

    state = hass.states.get(PLUGIN)
    assert state.attributes["installed_version"] == "0.3.0+g46ea0e3.dirty"
    assert state.attributes["latest_version"] == "0.3.0+g46ea0e3.dirty"
    assert state.state == STATE_OFF


async def test_a_release_build_the_index_confirms_is_current(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    entry = release("0.3.0")
    await _accept(hass, aioclient_mock, 1, [entry])
    await _report(
        hass,
        "0.3.0",
        {"commit": entry["commit"], "time": entry["commit_time"], "dirty": False,
         "flavour": "release", "on_disk": None},
    )

    state = hass.states.get(PLUGIN)
    assert state.attributes["installed_version"] == "0.3.0"
    assert state.state == STATE_OFF


async def test_a_release_flavour_the_index_does_not_know_is_a_development_build(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The builder's word is checked against the signed index where the index has a word."""
    await _setup(hass, config_entry, credentials=True)
    await _accept(hass, aioclient_mock, 1, [release("0.3.0")])
    await _report(hass, "0.3.0", {**_dev_build(), "flavour": "release"})

    state = hass.states.get(PLUGIN)
    assert state.attributes["installed_version"] == "0.3.0+g46ea0e3"
    assert state.state == STATE_ON


def _candidate_bundle(time: int):
    real = bundle_module.load_bundled_plugin()
    return replace(
        real,
        build={"commit": CANDIDATE_COMMIT, "time": time, "dirty": False,
               "flavour": "development"},
    )


async def test_a_candidate_bundle_is_offered_over_the_release_it_replaces(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The later commit of the same N.N.N is newer; the strings differ, so HA asks us."""
    candidate = _candidate_bundle(commit_time_of("0.3.0") + 100)
    with patch.object(bundle_module, "load_bundled_plugin", return_value=candidate):
        await _setup(hass, config_entry, credentials=True)
    # Without the index, the installed 0.3.0's time is unknown: not newer.
    assert hass.states.get(PLUGIN).state == STATE_OFF

    await _accept(hass, aioclient_mock, 1, [release("0.3.0")])
    state = hass.states.get(PLUGIN)
    assert state.attributes["installed_version"] == "0.3.0"
    assert state.attributes["latest_version"] == "0.3.0+gabc1234"
    assert state.attributes["bundled_version"] == "0.3.0+gabc1234"
    assert state.state == STATE_ON

    with patch("custom_components.enigma2_mqtt.update.async_install") as install:
        await hass.services.async_call(
            "update",
            "install",
            {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: "0.3.0+gabc1234"},
            blocking=True,
        )
    install.assert_awaited_once()


async def test_an_older_candidate_is_not_offered(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    candidate = _candidate_bundle(commit_time_of("0.3.0") - 100)
    with patch.object(bundle_module, "load_bundled_plugin", return_value=candidate):
        await _setup(hass, config_entry, credentials=True)
    await _accept(hass, aioclient_mock, 1, [release("0.3.0")])

    state = hass.states.get(PLUGIN)
    assert state.attributes["latest_version"] == "0.3.0"
    assert state.state == STATE_OFF


# ------------------------------------------------------------------- the select --


async def test_the_select_is_created_once_and_disabled(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Registered disabled by default, with no create-and-remove churn on a late `info`."""
    events: list[dict[str, Any]] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, lambda event: events.append(dict(event.data))
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    registry = er.async_get(hass)
    registered = registry.async_get(SELECT)
    assert registered is not None
    assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    ours = [event for event in events if event.get("entity_id") == SELECT]
    assert [event["action"] for event in ours] == ["create"]
    assert hass.states.get(SELECT) is None


async def _enable_select(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    er.async_get(hass).async_update_entity(SELECT, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_select_offers_latest_and_what_the_card_can_install(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _enable_select(hass, config_entry)
    await _report(hass, "0.2.0")

    state = hass.states.get(SELECT)
    assert state.attributes["options"] == [TARGET_LATEST, PLUGIN_VERSION]
    assert state.state == TARGET_LATEST

    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: SELECT, "option": PLUGIN_VERSION},
        blocking=True,
    )
    assert config_entry.options[CONF_PLUGIN_TARGET_VERSION] == PLUGIN_VERSION
    assert hass.states.get(SELECT).state == PLUGIN_VERSION
    assert hass.states.get(PLUGIN).attributes["latest_version"] == PLUGIN_VERSION

    # Choosing `latest` again forgets the choice rather than storing the word.
    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: SELECT, "option": TARGET_LATEST},
        blocking=True,
    )
    assert CONF_PLUGIN_TARGET_VERSION not in config_entry.options


async def test_the_select_never_offers_the_installed_version_or_an_older_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _enable_select(hass, config_entry)

    assert hass.states.get(SELECT).attributes["options"] == [TARGET_LATEST]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select", "select_option", {ATTR_ENTITY_ID: SELECT, "option": "0.2.0"},
            blocking=True,
        )


async def test_a_stored_choice_counts_only_while_the_select_is_enabled(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Disabled - its default - the select is `latest`, whatever was stored."""
    await _setup(hass, config_entry, options={CONF_PLUGIN_TARGET_VERSION: PLUGIN_VERSION})
    versions = await async_plugin_versions(hass, config_entry)
    assert versions.stored_target() == PLUGIN_VERSION
    assert versions.target() == TARGET_LATEST

    await _enable_select(hass, config_entry)
    versions = await async_plugin_versions(hass, config_entry)
    assert versions.target() == PLUGIN_VERSION


async def test_choosing_stores_the_option_and_reloads_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _enable_select(hass, config_entry)
    await _report(hass, "0.2.0")
    with patch.object(hass.config_entries, "async_reload") as reload:
        await hass.services.async_call(
            "select", "select_option", {ATTR_ENTITY_ID: SELECT, "option": PLUGIN_VERSION},
            blocking=True,
        )
        await hass.async_block_till_done()
    reload.assert_not_called()


# --------------------------------------------------------------- the attributes --


async def test_the_list_of_versions_is_not_recorded(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Through the component's own filter: the state carries it, the recorder does not."""
    await _setup(hass, config_entry)
    await _accept(hass, aioclient_mock, 1, [release("0.3.0")])

    state = hass.states.get(PLUGIN)
    assert state.attributes["available_versions"] == [
        {"version": "0.3.0", "compatible": True, "reason": None}
    ]
    assert "available_versions" in state.state_info["unrecorded_attributes"]
    assert "index_serial" not in state.state_info["unrecorded_attributes"]


async def test_the_check_is_reported_on_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await _setup(hass, config_entry)
    await _accept(hass, aioclient_mock, 1, [release("0.3.0")])

    state = hass.states.get(PLUGIN)
    checked = dt_util.parse_datetime(state.attributes["last_check"])
    assert checked is not None
    assert dt_util.utcnow() - checked < timedelta(minutes=1)
    assert state.attributes["check_error"] is None


# ------------------------------------------------ what the summary has to say, still --


async def test_a_newer_plugin_is_stated_rather_than_downgraded(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Up to date and ahead of us look identical on the card; only one is true."""
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "9.9.9")

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_OFF
    assert state.attributes["latest_version"] == "9.9.9"
    assert state.attributes["compatibility"] == COMPATIBILITY_NEWER
    assert "never offers a downgrade" in state.attributes["release_summary"]


async def test_a_matched_plugin_has_nothing_to_say(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await async_setup_box(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["release_summary"] is None
    assert state.attributes["compatibility"] == COMPATIBILITY_MATCHED
    assert state.attributes["bundled_version"] == PLUGIN_VERSION


async def test_an_unreadable_version_is_not_a_verdict(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "nightly")

    state = hass.states.get(PLUGIN)
    assert state.attributes["compatibility"] == COMPATIBILITY_UNKNOWN
    assert state.attributes["release_summary"] is None
    assert state.state == STATE_OFF


def test_compatibility_is_judged_on_n_n_n_alone() -> None:
    assert plugin_compatibility("0.1.0", "0.2.0") == COMPATIBILITY_OLDER
    assert plugin_compatibility("0.3.0", "0.2.0") == COMPATIBILITY_NEWER
    assert plugin_compatibility("0.2.0", "0.2.0") == COMPATIBILITY_MATCHED
    # A build label is not a version: PEP 440 would sort this above 0.3.0.
    assert plugin_compatibility("0.3.0+g46ea0e3", "0.3.0") == COMPATIBILITY_MATCHED
    assert plugin_compatibility(None, "0.2.0") == COMPATIBILITY_UNKNOWN
    assert plugin_compatibility("nightly", "0.2.0") == COMPATIBILITY_UNKNOWN
    assert plugin_compatibility("0.2.0", None) == COMPATIBILITY_UNKNOWN


async def test_a_language_without_the_sentences_simply_says_less(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    with patch(
        "custom_components.enigma2_mqtt.update.async_get_translations",
        AsyncMock(return_value={}),
    ):
        await _setup(hass, config_entry, credentials=True)
        await _report(hass, "0.0.9")

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_ON
    assert state.attributes["release_summary"] is None


async def test_a_sentence_with_a_placeholder_nobody_fills_is_dropped(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    with patch(
        "custom_components.enigma2_mqtt.update.async_get_translations",
        AsyncMock(
            return_value={
                "component.enigma2_mqtt.common.update_summary_older_no_install": (
                    "Zainstaluj {wersja}"
                )
            }
        ),
    ):
        await async_setup_box(hass, config_entry)
        await _report(hass, "0.0.9")

    assert hass.states.get(PLUGIN).attributes["release_summary"] is None


@pytest.mark.parametrize(("first", "second", "kept"), [(200, 60, False), (194, 60, True)])
async def test_a_second_sentence_is_kept_whole_or_dropped_whole(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    first: int,
    second: int,
    kept: bool,
) -> None:
    """Home Assistant cuts at 255 characters, wherever the 255th happens to fall."""
    with patch(
        "custom_components.enigma2_mqtt.update.async_get_translations",
        AsyncMock(
            return_value={
                "component.enigma2_mqtt.common.update_summary_older_no_install": "A" * first,
                "component.enigma2_mqtt.common.update_summary_published": "B" * second,
            }
        ),
    ):
        await async_setup_box(hass, config_entry)
        await _accept(hass, aioclient_mock, 1, [release("0.4.0"), release("0.3.0")])
        await _report(hass, "0.0.9")

    summary = hass.states.get(PLUGIN).attributes["release_summary"]
    if kept:
        assert summary == "A" * first + " " + "B" * second
        assert len(summary) == 255
    else:
        assert summary == "A" * first


# ---------------------------------------------------- what install refuses, and why --


async def test_install_refuses_to_replace_a_newer_plugin(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "9.9.9")

    with pytest.raises(HomeAssistantError, match="newer plugin"):
        await hass.services.async_call(
            "update", "install", {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
            blocking=True,
        )


async def test_install_refuses_a_version_the_bundle_does_not_hold(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """There is no download path yet, so a version that is not bundled cannot be installed."""
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "0.0.9")

    with pytest.raises(HomeAssistantError, match="verified bundle"):
        await hass.services.async_call(
            "update", "install", {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: "0.0.1"}, blocking=True
        )


async def test_a_development_build_can_be_put_back_on_the_release(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Same N.N.N is not a downgrade: `0.3.0+g...` sorts above `0.3.0` only in PEP 440."""
    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "0.3.0", _dev_build())
    with patch("custom_components.enigma2_mqtt.update.async_install") as install:
        await hass.services.async_call(
            "update", "install", {ATTR_ENTITY_ID: PLUGIN}, blocking=True
        )
    install.assert_awaited_once()


async def test_a_phase_the_card_cannot_draw_moves_no_bar(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    percentages: list[int | None] = []

    async def _install(hass_, request, progress_cb=None, **kwargs):
        del hass_, request, kwargs
        progress_cb("a phase from the future")
        percentages.append(hass.states.get(PLUGIN).attributes["update_percentage"])
        progress_cb("upload")
        percentages.append(hass.states.get(PLUGIN).attributes["update_percentage"])

    await _setup(hass, config_entry, credentials=True)
    await _report(hass, "0.0.9")

    with patch("custom_components.enigma2_mqtt.update.async_install", _install):
        await hass.services.async_call(
            "update", "install", {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
            blocking=True,
        )

    assert percentages == [None, 37]


async def test_the_daily_check_belongs_to_the_entry_that_started_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Started on the entry, the request is cancelled with everything the receiver owns."""
    pair = sign(1, [release("0.3.0")])
    aioclient_mock.get(INDEX_URL, content=pair[0])
    aioclient_mock.get(SIG_URL, content=pair[1])
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_CHECK_GITHUB_RELEASES: True}
    )
    with (
        patch(NOTIFY),
        patch.object(
            type(config_entry),
            "async_create_background_task",
            autospec=True,
            side_effect=ConfigEntry.async_create_background_task,
        ) as on_the_entry,
        patch.object(hass, "async_create_task", side_effect=hass.async_create_task) as on_hass,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    def _named(calls) -> list[str]:
        names = []
        for call in calls:
            if "name" in call.kwargs:
                names.append(str(call.kwargs["name"]))
            elif len(call.args) >= 4:
                names.append(str(call.args[3]))
        return names

    assert aioclient_mock.call_count == 2
    assert [name for name in _named(on_the_entry.call_args_list) if "release check" in name]
    assert not [name for name in _named(on_hass.call_args_list) if "release check" in name]


# ------------------------------------------------------ the topic the plugin reads --


async def test_the_integration_topic_says_what_this_integration_works_with(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Retained, QoS 1, on every setup: the receiver applies the same rule with it."""
    await async_setup_box(hass, config_entry)

    calls = [
        call for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == f"enigma2mqtt/integration/{NODE_ID}"
    ]
    assert len(calls) == 1
    topic, payload, qos, retain = calls[0].args[:4]
    assert (qos, retain) == (1, True)
    assert json.loads(payload) == {"integration": "0.3.1", "contract": 1, "plugin_min": "0.2.0"}


async def test_the_domain_holds_one_picture_per_entry(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The update and the select read the same picture, built once per setup."""
    await async_setup_box(hass, config_entry)
    first = await async_plugin_versions(hass, config_entry)
    assert await async_plugin_versions(hass, config_entry) is first
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await async_plugin_versions(hass, config_entry) is not first

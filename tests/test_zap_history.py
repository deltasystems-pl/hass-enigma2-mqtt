""""Ostatnio oglądane", "(wszystkie)" and "Wyczyść ostatnio oglądane": the zap history.

The receiver keeps the list of channels it zapped to - the list its own "History Zap"
screen shows on NEXT and PREVIOUS - and the plugin publishes it, newest first, on a
retained `zap_history` topic. Two selects offer it: one that leaves out the bouquets the
`history_hidden_bouquets` option names, and one that leaves out nothing and is disabled
until somebody enables it. A button clears it the way the remote's 0 key does.

The tests are shaped by what has gone wrong in this repository before. The retained
burst lands after the entities exist on a real broker, so ordering-sensitive tests use
`async_setup_box_then_retained`. An end state cannot tell "never created" from "created
then removed", so creation is watched on the registry. And a press must not report
success on a state it did not cause: a history of one entry is already "cleared" before
a press the receiver refuses for exactly that reason.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.media_player import ATTR_INPUT_SOURCE_LIST
from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.components.select import (
    ATTR_OPTION,
    ATTR_OPTIONS,
    DATA_COMPONENT,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_bytes
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.box import Enigma2Box
from custom_components.enigma2_mqtt.const import DOMAIN
from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    AVAILABILITY_TOPIC,
    BASE_TOPIC,
    BOUQUET_TOPIC,
    CHANNELS,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    assert_published,
    async_arm_box_error,
    async_arm_box_reply,
    async_setup_box,
    async_setup_box_then_retained,
    command_topic,
)

# The capabilities, the topic and the option, spelled out rather than imported, so this
# module still collects against a tree without the feature and every test can be shown
# red.
CAPABILITY_ZAP_HISTORY = "zap_history"
CAPABILITY_HISTORY_CLEAR = "history_clear"
HIDDEN_OPTION = "history_hidden_bouquets"
ZAP_HISTORY_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/zap_history"
REASONS = (
    "standby",
    "panic_off",
    "too_short",
    "timeshift",
    "zap_blocked",
    "pip",
    "playback",
    "not_cleared",
)

HISTORY_SELECT = "select.dekoder_salon_recently_watched"
HISTORY_ALL_SELECT = "select.dekoder_salon_recently_watched_all"
CLEAR_BUTTON = "button.dekoder_salon_clear_recently_watched"
BOUQUET_SELECT = "select.dekoder_salon_bouquet"
CHANNEL_SELECT = "select.dekoder_salon_channel"
PLAYER = "media_player.dekoder_salon"

ULUBIONE = CHANNELS["bouquets"][0]
SPORT = CHANNELS["bouquets"][1]
EUROSPORT = SPORT["channels"][0]["sref"]
# "TVN HD" as the Sport bouquet lists it: the same name as the one in Ulubione, another
# service. Reached through Ulubione's path below, it is still a member of Sport.
TVN_IN_SPORT = SPORT["channels"][1]["sref"]
RADIO_BOUQUET = '1:7:2:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.radio.radio" ORDER BY bouquet'
RADIO_SREF = "1:0:2:6F:3F3:1:C00000:0:0:0:"


def _entry(sref: str, name: str, bouquet: str | None, bouquet_name: str | None) -> dict:
    return {"sref": sref, "name": name, "bouquet": bouquet, "bouquet_name": bouquet_name}


TVP = _entry(SREF, "TVP 1 HD", ULUBIONE["sref"], ULUBIONE["name"])
TVN = _entry(SREF_TWO, "TVN HD", ULUBIONE["sref"], ULUBIONE["name"])
EURO = _entry(EUROSPORT, "Eurosport 1", SPORT["sref"], SPORT["name"])
TVN_SPORT_VIA_ULUBIONE = _entry(TVN_IN_SPORT, "TVN HD", ULUBIONE["sref"], ULUBIONE["name"])
RADIO = _entry(RADIO_SREF, "Radio Jazz", RADIO_BOUQUET, None)
NO_PATH = _entry("1:0:19:7070:3F3:1:C00000:0:0:0:", "Kanał bez ścieżki", None, None)


def history(*entries: dict, panic_button: Any = True, current: Any = 0) -> str:
    """Return a `zap_history` payload, newest first, as the plugin publishes it."""
    payload: dict[str, Any] = {
        "entries": list(entries),
        "current": current,
        "limit": 20,
    }
    if panic_button is not None:
        payload["panic_button"] = panic_button
    return json.dumps(payload)


def capable(store: dict[str, str | bytes], *extra: str) -> None:
    """Make the example box one that publishes its zap history and can clear it.

    Both topics, because the plugin publishes the same capability list on both and a
    fixture where they disagree would be testing a receiver that does not exist.
    """
    capabilities = [
        *INFO["capabilities"],
        CAPABILITY_ZAP_HISTORY,
        CAPABILITY_HISTORY_CLEAR,
        *extra,
    ]
    store[INFO_TOPIC] = json.dumps({**INFO, "capabilities": capabilities})
    store[ANNOUNCEMENT_TOPIC] = json.dumps({**ANNOUNCEMENT, "capabilities": capabilities})


def hiding(config_entry: MockConfigEntry, *names: str) -> MockConfigEntry:
    """Return the example entry with bouquets hidden from "Ostatnio oglądane"."""
    return MockConfigEntry(
        domain=config_entry.domain,
        title=config_entry.title,
        unique_id=config_entry.unique_id,
        data=dict(config_entry.data),
        options={HIDDEN_OPTION: list(names)},
    )


def enable_the_unfiltered_select(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Register "(wszystkie)" enabled before setup, as somebody who enabled it has."""
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "select",
        DOMAIN,
        f"{NODE_ID}_zap_history_all",
        config_entry=entry,
        suggested_object_id="dekoder_salon_recently_watched_all",
    )


def playing(sref: str, name: str) -> str:
    return json.dumps({**SERVICE, "sref": sref, "name": name})


def options(hass: HomeAssistant, entity_id: str) -> list[str]:
    return hass.states.get(entity_id).attributes[ATTR_OPTIONS]


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


async def _press(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: CLEAR_BUTTON}, blocking=True
    )


async def _arm_refusal(
    hass: HomeAssistant, command: str, error: str, reason: Any
) -> None:
    """Make the fake box refuse a command with a sentence and, if given, a reason code."""
    from homeassistant.components import mqtt  # noqa: PLC0415

    payload: dict[str, Any] = {"cmd": command, "error": error, "ts": 1789459213}
    if reason is not None:
        payload["reason"] = reason

    @callback
    def _command_received(msg) -> None:
        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(payload))

    await mqtt.async_subscribe(hass, command_topic(command), _command_received)


# ---------------------------------------------------------------- when they exist


async def test_all_three_are_created_once_on_capability_in_broker_order(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Created by the capability that arrives after setup, once, and never removed.

    The filtered select and the button are enabled; "(wszystkie)" is registered
    disabled, so it has no state at all until somebody enables it.
    """
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    config_entry.add_to_hass(hass)
    touched: list[tuple[str, str]] = []

    @callback
    def _record(event: Event) -> None:
        touched.append((event.data["action"], event.data["entity_id"]))

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _record)

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    ours = (HISTORY_SELECT, HISTORY_ALL_SELECT, CLEAR_BUTTON)
    created = [entity_id for action, entity_id in touched if action == "create"]
    assert sorted(entity_id for entity_id in created if entity_id in ours) == sorted(ours)
    assert not [entry for entry in touched if entry[0] == "remove" and entry[1] in ours]
    assert hass.states.get(HISTORY_SELECT) is not None
    assert hass.states.get(CLEAR_BUTTON) is not None
    registry = er.async_get(hass)
    assert registry.async_get(HISTORY_ALL_SELECT).disabled_by is (
        er.RegistryEntryDisabler.INTEGRATION
    )
    assert hass.states.get(HISTORY_ALL_SELECT) is None


async def test_a_capability_that_goes_quiet_removes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A downgrade or a hook that failed on one boot is not a decision anybody made."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    removed: list[str] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        callback(
            lambda event: removed.append(event.data["entity_id"])
            if event.data["action"] == "remove"
            else None
        ),
    )

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for entity_id in (HISTORY_SELECT, HISTORY_ALL_SELECT, CLEAR_BUTTON):
        assert registry.async_get(entity_id) is not None
    assert removed == []


async def test_without_the_capabilities_there_is_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An older plugin publishes no history and has none of the three."""
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    registry = er.async_get(hass)
    for entity_id in (HISTORY_SELECT, HISTORY_ALL_SELECT, CLEAR_BUTTON):
        assert registry.async_get(entity_id) is None


async def test_the_clear_button_needs_its_own_capability(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An image can publish its history and still lack what the 0 key runs."""
    capabilities = [*INFO["capabilities"], CAPABILITY_ZAP_HISTORY]
    box_on_the_broker[INFO_TOPIC] = json.dumps({**INFO, "capabilities": capabilities})
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(HISTORY_SELECT) is not None
    assert er.async_get(hass).async_get(CLEAR_BUTTON) is None


async def test_enabling_the_unfiltered_select_gives_it_a_state(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Disabled by default is a registry flag, not an entity that cannot work."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box(hass, config_entry)
    assert hass.states.get(HISTORY_ALL_SELECT) is None

    er.async_get(hass).async_update_entity(HISTORY_ALL_SELECT, disabled_by=None)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(HISTORY_ALL_SELECT)
    assert state is not None
    assert state.attributes[ATTR_OPTIONS] == ["TVP 1 HD", "TVN HD"]


# ------------------------------------------------------------------ what they list


async def test_both_list_the_history_in_the_receivers_order(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Newest first, as published, and the playing channel is the state."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVN, EURO, TVP)
    box_on_the_broker[SERVICE_TOPIC] = playing(SREF_TWO, "TVN HD")
    enable_the_unfiltered_select(hass, config_entry)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    for entity_id in (HISTORY_SELECT, HISTORY_ALL_SELECT):
        state = hass.states.get(entity_id)
        assert state.attributes[ATTR_OPTIONS] == ["TVN HD", "Eurosport 1", "TVP 1 HD"]
        assert state.state == "TVN HD"


async def test_the_list_follows_every_new_payload(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A zap reorders the history; the select follows on the topic alone."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, ZAP_HISTORY_TOPIC, history(EURO, TVP, TVN))
    await hass.async_block_till_done()

    assert options(hass, HISTORY_SELECT) == ["Eurosport 1", "TVP 1 HD", "TVN HD"]


async def test_duplicate_names_are_numbered_by_service_identity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Two services called "TVN HD": numbered like "Kanał", and stable under a reorder."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVN_SPORT_VIA_ULUBIONE, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    entity = hass.data[DATA_COMPONENT].get_entity(HISTORY_SELECT)
    before = dict(entity._srefs)
    assert sorted(before) == ["TVN HD", "TVN HD (2)"]

    async_fire_mqtt_message(hass, ZAP_HISTORY_TOPIC, history(TVN, TVN_SPORT_VIA_ULUBIONE))
    await hass.async_block_till_done()

    assert options(hass, HISTORY_SELECT) == [
        next(label for label, sref in before.items() if sref == SREF_TWO),
        next(label for label, sref in before.items() if sref == TVN_IN_SPORT),
    ]
    assert dict(entity._srefs) == before


async def test_a_playing_channel_outside_the_history_is_unknown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An unrecorded zap leaves the playing channel off the list; nothing is invented."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVN, EURO)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(HISTORY_SELECT)
    assert state.state == STATE_UNKNOWN
    assert "TVP 1 HD" not in state.attributes[ATTR_OPTIONS]


async def test_the_playing_channel_is_matched_by_identity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The `service` topic need not spell the reference the way the history does."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVN, TVP)
    box_on_the_broker[SERVICE_TOPIC] = playing(SREF_TWO.lower().rstrip(":"), "TVN HD")
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(HISTORY_SELECT).state == "TVN HD"


async def test_an_empty_history_is_an_available_select_with_nothing_in_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """What a receiver whose interface has just restarted publishes."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history()
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(HISTORY_SELECT)
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_OPTIONS] == []


async def test_a_retraction_clears_the_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An empty payload is the plugin withdrawing the topic, and the list goes with it."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD", "TVN HD"]

    async_fire_mqtt_message(hass, ZAP_HISTORY_TOPIC, "")
    await hass.async_block_till_done()

    state = hass.states.get(HISTORY_SELECT)
    assert state.attributes[ATTR_OPTIONS] == []
    assert state.state == STATE_UNKNOWN


async def test_a_payload_that_is_not_json_keeps_the_last_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """One malformed message is a bug at the other end, not news about the history."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, ZAP_HISTORY_TOPIC, "{not json")
    await hass.async_block_till_done()

    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD", "TVN HD"]


async def test_entries_that_cannot_be_read_are_dropped_not_raised(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The payload is the receiver's; an entry without a name or a reference is no row."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = json.dumps(
        {
            "entries": [TVP, {"name": "no reference"}, "not an object", {"sref": SREF_TWO}],
            "current": True,
            "limit": "20",
            "panic_button": "yes",
        }
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD"]
    box: Enigma2Box = config_entry.runtime_data
    assert box.state.zap_history["current"] is None
    assert box.state.zap_history["limit"] is None
    assert box.state.zap_history["panic_button"] is None


async def test_both_are_unavailable_while_the_box_is(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A history of a receiver that is not there is not worth offering."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP)
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, AVAILABILITY_TOPIC, "offline")
    await hass.async_block_till_done()

    assert hass.states.get(HISTORY_SELECT).state == STATE_UNAVAILABLE
    assert hass.states.get(CLEAR_BUTTON).state == STATE_UNAVAILABLE


# ------------------------------------------------------------------------ the filter


async def test_with_nothing_hidden_the_filtered_select_shows_everything(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Radio and pathless entries included: there is nothing to check them against."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, EURO, RADIO, NO_PATH)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert options(hass, HISTORY_SELECT) == [
        "TVP 1 HD",
        "Eurosport 1",
        "Radio Jazz",
        "Kanał bez ścieżki",
    ]


async def test_a_hidden_bouquet_leaves_the_filtered_select_only(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """By path, by membership through another bouquet, and failing toward hiding.

    Eurosport 1 was reached through Sport. The Sport copy of "TVN HD" was reached
    through Ulubione and is still a member of Sport. The radio entry and the pathless one
    cannot be checked while anything is hidden. "(wszystkie)" shows all of it.
    """
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(
        TVP, EURO, TVN_SPORT_VIA_ULUBIONE, RADIO, NO_PATH, TVN
    )
    entry = hiding(config_entry, SPORT["name"])
    enable_the_unfiltered_select(hass, entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD", "TVN HD"]
    entity = hass.data[DATA_COMPONENT].get_entity(HISTORY_SELECT)
    assert entity._srefs["TVN HD"] == SREF_TWO
    assert options(hass, HISTORY_ALL_SELECT) == [
        "TVP 1 HD",
        "Eurosport 1",
        # Numbered by identity, not position: 2B66 sorts before 5678.
        "TVN HD (2)",
        "Radio Jazz",
        "Kanał bez ścieżki",
        "TVN HD",
    ]


async def test_a_hidden_channel_playing_is_never_the_filtered_state(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """While a hidden bouquet's channel plays, the filtered select says unknown."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(EURO, TVP)
    box_on_the_broker[SERVICE_TOPIC] = playing(EUROSPORT, "Eurosport 1")
    entry = hiding(config_entry, SPORT["name"])
    enable_the_unfiltered_select(hass, entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    assert hass.states.get(HISTORY_SELECT).state == STATE_UNKNOWN
    assert hass.states.get(HISTORY_ALL_SELECT).state == "Eurosport 1"


async def test_the_option_leaves_every_other_list_alone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """"Kanał", "Bukiet" and the media player's source list still offer the hidden one.

    The option is about one entity. Filtering anything else with it would take a
    bouquet out of the controls somebody uses to change channel.
    """
    capable(box_on_the_broker, "bouquet_context")
    box_on_the_broker[BOUQUET_TOPIC] = json.dumps(
        {"name": SPORT["name"], "sref": SPORT["sref"]}
    )
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(EURO, TVP)
    entry = hiding(config_entry, SPORT["name"])
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    assert options(hass, BOUQUET_SELECT) == ["Ulubione TV", "Sport"]
    assert hass.states.get(BOUQUET_SELECT).state == "Sport"
    assert options(hass, CHANNEL_SELECT) == ["Eurosport 1", "TVN HD"]
    assert "Eurosport 1" in hass.states.get(PLAYER).attributes[ATTR_INPUT_SOURCE_LIST]
    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD"]


async def test_a_hidden_name_missing_from_the_channel_list_is_said_once(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A renamed bouquet's channels are no longer recognised through other bouquets."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP)
    entry = hiding(config_entry, "Bukiet, którego nie ma")
    caplog.set_level(logging.WARNING)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    async_fire_mqtt_message(hass, ZAP_HISTORY_TOPIC, history(TVN, TVP))
    await hass.async_block_till_done()

    assert caplog.text.count("Bukiet, którego nie ma is hidden from the zap history") == 1
    assert options(hass, HISTORY_SELECT) == ["TVN HD", "TVP 1 HD"]


# ------------------------------------------------------------------- the recorder


def _stored_attributes(state) -> bytes:
    """Return what the recorder would store as this state's attributes."""
    event: Event[EventStateChangedData] = Event(
        EVENT_STATE_CHANGED,
        {"entity_id": state.entity_id, "old_state": None, "new_state": state},
    )
    return StateAttributes.shared_attrs_bytes_from_event(event, None)


async def test_the_recorder_gets_no_hidden_name_and_no_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Through the recorder's own encoder, with a hidden channel playing.

    The filtered select's recorded state is `unknown` and its recorded attributes hold
    neither the list nor any hidden name. The unfiltered one's list is not recorded
    either; its state is, and that is documented rather than prevented.
    """
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(EURO, TVP)
    box_on_the_broker[SERVICE_TOPIC] = playing(EUROSPORT, "Eurosport 1")
    entry = hiding(config_entry, SPORT["name"])
    enable_the_unfiltered_select(hass, entry)
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    filtered = hass.states.get(HISTORY_SELECT)
    stored = _stored_attributes(filtered)
    assert b"friendly_name" in stored
    assert b"options" not in stored
    assert b"Eurosport" not in stored
    assert filtered.state == STATE_UNKNOWN
    # The list really is on the state; it is the recorder that leaves it out.
    assert b"TVP 1 HD" in json_bytes(dict(filtered.attributes))

    unfiltered = hass.states.get(HISTORY_ALL_SELECT)
    stored = _stored_attributes(unfiltered)
    assert b"friendly_name" in stored
    assert b"options" not in stored
    assert b"Eurosport" in json_bytes(dict(unfiltered.attributes))


# -------------------------------------------------------------------- diagnostics


async def test_diagnostics_count_the_history_and_name_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A count and three scalars; no channel name and no reference, hidden or not."""
    private_bouquet = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.private.tv" ORDER BY bouquet'
    private = [
        _entry("1:0:19:ABC1:3F3:1:C00000:0:0:0:", "Kanał prywatny A", private_bouquet, "Prywatne"),
        _entry("1:0:19:ABC2:3F3:1:C00000:0:0:0:", "Kanał prywatny B", private_bouquet, "Prywatne"),
    ]
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(*private, current=1)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["topics"]["zap_history"] == {
        "entries": 2,
        "current": 1,
        "limit": 20,
        "panic_button": True,
    }
    dumped = json.dumps(diagnostics, ensure_ascii=False)
    for needle in ("prywatny", "ABC1", "ABC2", "userbouquet.private", "Prywatne"):
        assert needle not in dumped


# ---------------------------------------------------------------- choosing from it


async def test_choosing_goes_back_through_cmd_zap_history_by_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The box's own screen's path, `{"sref": ...}`, proved by `service` naming it."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN, EURO)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_reply(hass, "zap_history", SERVICE_TOPIC, playing(EUROSPORT, "Eurosport 1"))

    await _select(hass, HISTORY_SELECT, "Eurosport 1")

    assert_published(mqtt_mock, command_topic("zap_history"), json.dumps({"sref": EUROSPORT}))
    assert not [
        call for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == command_topic("zap")
    ]
    assert hass.states.get(HISTORY_SELECT).state == "Eurosport 1"


async def test_choosing_what_is_playing_sends_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The receiver's own screen does nothing there either, and nothing is waited for."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    # The select exists and is on the playing channel, so the call below reaches it.
    assert hass.states.get(HISTORY_SELECT).state == "TVP 1 HD"

    await _select(hass, HISTORY_SELECT, "TVP 1 HD")

    assert not [
        call for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == command_topic("zap_history")
    ]


async def test_a_refused_history_zap_raises_the_boxs_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The entry can leave the history between the list and the click."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_error(
        hass, "zap_history", "that channel is no longer in the receiver's zap history"
    )

    with pytest.raises(HomeAssistantError, match="no longer in the receiver's zap history"):
        await _select(hass, HISTORY_SELECT, "TVN HD")


async def test_an_option_that_vanished_says_the_list_moved(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A stale label is never sent."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    entity = hass.data[DATA_COMPONENT].get_entity(HISTORY_SELECT)
    with pytest.raises(ServiceValidationError) as raised:
        await entity.async_select_option("Kanał, którego już nie ma")
    assert raised.value.translation_key == "option_no_longer_offered"
    assert not [
        call for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == command_topic("zap_history")
    ]


# ------------------------------------------------------------------- the clear button


async def test_a_press_is_proved_by_a_new_payload_of_at_most_one_entry(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The receiver clears, lands on channel 1, and republishes one entry."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVN, EURO, TVP)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_reply(hass, "history_clear", ZAP_HISTORY_TOPIC, history(TVP))

    await _press(hass)

    assert_published(mqtt_mock, command_topic("history_clear"), "PRESS")
    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD"]


@pytest.mark.parametrize(
    ("reply", "retain"),
    [
        # Nothing at all: the one entry held already is not the answer.
        (None, False),
        # The broker replaying what it held after a reconnect.
        (history(TVP), True),
        # A new payload that is not cleared.
        (history(TVP, TVN), False),
    ],
    ids=["silence", "retained_replay", "not_cleared"],
)
async def test_nothing_but_a_new_short_payload_is_the_proof(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    reply: str | None,
    retain: bool,
) -> None:
    """A history of one entry is "cleared" before a press the receiver would refuse."""
    from homeassistant.components import mqtt  # noqa: PLC0415
    from homeassistant.components.mqtt.models import DATA_MQTT  # noqa: PLC0415

    monkeypatch.setitem(Enigma2Box.async_command.__kwdefaults__, "timeout", 0.3)
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    # A live one-entry payload before the press, so that only "after the press" - not
    # the retain flag of the burst - stands between the held list and a false success.
    async_fire_mqtt_message(hass, ZAP_HISTORY_TOPIC, history(TVP))
    await hass.async_block_till_done()
    if reply is not None:

        client = hass.data[DATA_MQTT].client

        @callback
        def _command_received(msg) -> None:
            if retain:
                # What a reconnect does before it resubscribes: Home Assistant forgets
                # which retained topics it delivered, and the broker replays them.
                client._retained_topics.clear()
            async_fire_mqtt_message(hass, ZAP_HISTORY_TOPIC, reply, retain=retain)

        await mqtt.async_subscribe(hass, command_topic("history_clear"), _command_received)

    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass)
    assert raised.value.translation_key == "command_timeout"


@pytest.mark.parametrize("reason", REASONS)
async def test_every_refusal_is_raised_in_its_own_translation(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reason: str,
) -> None:
    """The reason code picks the message, so the household reads it in its language."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await _arm_refusal(hass, "history_clear", "an English sentence", reason)

    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass)
    assert raised.value.translation_key == f"history_clear_{reason}"


@pytest.mark.parametrize("reason", [None, "a_code_from_a_newer_plugin", 7])
async def test_an_unknown_or_absent_reason_is_the_boxs_own_sentence(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reason: Any,
) -> None:
    """Never wrong, only untranslated."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await _arm_refusal(hass, "history_clear", "the receiver said something new", reason)

    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass)
    assert raised.value.translation_key == "command_refused"
    assert raised.value.translation_placeholders == {
        "command": "history_clear",
        "reason": "the receiver said something new",
    }


async def test_a_reason_on_another_command_changes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Only a command that asked for its codes gets them; a zap stays in the box's words."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await _arm_refusal(hass, "zap_history", "the receiver is playing a recording", "playback")

    with pytest.raises(HomeAssistantError) as raised:
        await _select(hass, HISTORY_SELECT, "TVN HD")
    assert raised.value.translation_key == "command_refused"


@pytest.mark.parametrize(
    ("panic_button", "expected"),
    [(False, STATE_UNAVAILABLE), (True, STATE_UNKNOWN), (None, STATE_UNKNOWN)],
    ids=["off", "on", "not_said"],
)
async def test_the_button_is_unavailable_only_when_the_panic_button_is_off(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    panic_button: bool | None,
    expected: str,
) -> None:
    """With it off, 0 only goes back one channel; not said is not off."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(TVP, TVN, panic_button=panic_button)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(CLEAR_BUTTON).state == expected

    async_fire_mqtt_message(hass, ZAP_HISTORY_TOPIC, history(TVP, TVN, panic_button=True))
    await hass.async_block_till_done()
    assert hass.states.get(CLEAR_BUTTON).state == STATE_UNKNOWN


# ------------------------------------------------------------------ the option form


async def test_the_option_offers_the_published_bouquets_and_is_saved(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Every bouquet the box published, a typed name allowed, and it takes effect."""
    capable(box_on_the_broker)
    box_on_the_broker[ZAP_HISTORY_TOPIC] = history(EURO, TVP)
    await async_setup_box(hass, config_entry)
    assert options(hass, HISTORY_SELECT) == ["Eurosport 1", "TVP 1 HD"]

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    selector = result["data_schema"].schema[HIDDEN_OPTION]
    assert selector.config["options"] == ["Ulubione TV", "Sport"]
    assert selector.config["custom_value"] is True
    assert selector.config["multiple"] is True
    assert result["data_schema"]({})[HIDDEN_OPTION] == []

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"dangerous_buttons": False, "wol_mac": "", HIDDEN_OPTION: ["Sport"]},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[HIDDEN_OPTION] == ["Sport"]
    assert options(hass, HISTORY_SELECT) == ["TVP 1 HD"]


# -------------------------------------------------------------------- translations


def test_every_reason_and_entity_has_a_string_in_every_language() -> None:
    """A reason without a translation would fall back to the key, not to English."""
    from pathlib import Path  # noqa: PLC0415

    component = Path(__file__).parent.parent / "custom_components" / "enigma2_mqtt"
    for path in (component / "strings.json", *(component / "translations").glob("*.json")):
        strings = json.loads(path.read_text(encoding="utf-8"))
        for reason in REASONS:
            message = strings["exceptions"][f"history_clear_{reason}"]["message"]
            assert message.strip(), f"{path.name}: history_clear_{reason}"
        assert strings["entity"]["select"]["zap_history"]["name"]
        assert strings["entity"]["select"]["zap_history_all"]["name"]
        assert strings["entity"]["button"]["history_clear"]["name"]
        settings = strings["options"]["step"]["settings"]
        assert settings["data"][HIDDEN_OPTION]
        assert settings["data_description"][HIDDEN_OPTION]


def test_the_polish_names_give_the_ids_the_operator_expects() -> None:
    """On a Polish installation the id comes from the Polish name; these are fixed."""
    from pathlib import Path  # noqa: PLC0415

    from homeassistant.util import slugify  # noqa: PLC0415

    path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "enigma2_mqtt"
        / "translations"
        / "pl.json"
    )
    entity = json.loads(path.read_text(encoding="utf-8"))["entity"]
    assert slugify(entity["select"]["zap_history"]["name"]) == "ostatnio_ogladane"
    assert (
        slugify(entity["select"]["zap_history_all"]["name"])
        == "ostatnio_ogladane_wszystkie"
    )
    assert (
        slugify(entity["button"]["history_clear"]["name"])
        == "wyczysc_ostatnio_ogladane"
    )

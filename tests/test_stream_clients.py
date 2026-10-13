"""Who the receiver is streaming to, and the action that ends one stream.

A plugin after 0.4.0 lists the clients of the image's stream server on `stream_clients`
and takes `cmd/stream_stop` for one of them. The plugin publishes every client; hiding
the channels of the bouquets the options hide is done here, with the filter the zap
history already uses, and a hidden row can still be stopped without its reference ever
reaching a state, an attribute or a log line of this integration.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import patch

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.const import ATTR_ENTITY_ID, EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt import stream
from custom_components.enigma2_mqtt.box import Enigma2Box
from custom_components.enigma2_mqtt.const import DOMAIN

from .conftest import (
    BASE_TOPIC,
    CHANNELS,
    CHANNELS_TOPIC,
    INFO,
    INFO_TOPIC,
    LAST_ERROR_TOPIC,
    NODE_ID,
    SREF,
    SREF_TWO,
    async_arm_box_ack,
    async_setup_box_then_retained,
    command_topic,
    opened_command,
    published_payloads,
)

CLIENTS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/stream_clients"
SENSOR = "sensor.dekoder_salon_stream_clients"
PLAYER = "media_player.dekoder_salon"
EUROSPORT = "1:0:19:1234:3F3:1:C00000:0:0:0:"
HOST = "192.0.2.20"
HOST_V6 = "2001:db8::17"

ROW_VISIBLE: dict[str, Any] = {
    "host": HOST, "sref": SREF, "name": "TVP 1 HD", "bouquet": "Ulubione TV",
    "transcoded": False,
}
ROW_SPORT: dict[str, Any] = {
    "host": HOST_V6, "sref": EUROSPORT, "name": "Eurosport 1", "bouquet": "Sport",
    "transcoded": True,
}
CLIENTS: dict[str, Any] = {"count": 2, "clients": [ROW_VISIBLE, ROW_SPORT]}
HIDDEN_ROW = {
    "host": HOST_V6, "sref": None, "name": None, "bouquet": None, "transcoded": True,
    "hidden": True,
}


def info_with(*capabilities: str) -> str:
    return json.dumps({**INFO, "capabilities": [*INFO["capabilities"], *capabilities]})


def hiding(entry: MockConfigEntry, *bouquets: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        options={"history_hidden_bouquets": list(bouquets)},
    )


def streaming(
    store: dict[str, str | bytes], clients: dict[str, Any] = CLIENTS, *capabilities: str
) -> None:
    store[INFO_TOPIC] = info_with("stream_clients", "stream_stop", *capabilities)
    store[CLIENTS_TOPIC] = json.dumps(clients)


def _stored_attributes(state) -> bytes:
    """Return what the recorder would store as this state's attributes."""
    event: Event[EventStateChangedData] = Event(
        EVENT_STATE_CHANGED,
        {"entity_id": state.entity_id, "old_state": None, "new_state": state},
    )
    return StateAttributes.shared_attrs_bytes_from_event(event, None)


# ----------------------------------------------------------------- the sensor


async def test_without_the_capability_there_is_no_sensor(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box_on_the_broker[CLIENTS_TOPIC] = json.dumps(CLIENTS)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{NODE_ID}_stream_clients")
        is None
    )


async def test_the_count_is_the_state_and_the_rows_are_an_attribute(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state.state == "2"
    assert state.attributes["clients"] == [
        {**ROW_VISIBLE, "hidden": False},
        {**ROW_SPORT, "hidden": False},
    ]


async def test_the_rows_are_not_recorded(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Who watched what from where is not for the database; the count is."""
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    stored = _stored_attributes(hass.states.get(SENSOR))

    assert b'"clients"' not in stored
    assert HOST.encode() not in stored
    assert b"TVP" not in stored


async def test_the_count_says_how_many_there_are_when_the_list_is_cut(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The plugin lists at most 32 rows and still counts them all."""
    rows = [{**ROW_VISIBLE, "host": f"192.0.2.{index}"} for index in range(40)]
    streaming(box_on_the_broker, {"count": 40, "clients": rows})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state.state == "40"
    assert len(state.attributes["clients"]) == 32


async def test_an_emptied_topic_is_unknown_and_a_later_list_is_read(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The plugin empties the topic when the image's list cannot be read."""
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, CLIENTS_TOPIC, "")
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "unknown"
    assert state.attributes["clients"] == []

    async_fire_mqtt_message(hass, CLIENTS_TOPIC, json.dumps({"count": 0, "clients": []}))
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "0"


async def test_a_payload_that_is_not_json_changes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, CLIENTS_TOPIC, "[1, 2")
    async_fire_mqtt_message(hass, CLIENTS_TOPIC, "[]")
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR).state == "2"


def test_the_reader_keeps_what_the_contract_names_and_nothing_else() -> None:
    """Missing members are unknown, unknown members are ignored, junk rows are dropped."""
    read = stream.normalise_stream_clients(
        {
            "clients": [
                {"host": HOST, "sref": SREF, "name": "TVP 1 HD", "bouquet": None,
                 "transcoded": False, "bitrate": 9000},
                {"host": HOST_V6},
                {"sref": SREF},
                {"host": "", "sref": SREF},
                "192.0.2.9",
                None,
            ],
            "since": 1789459200,
        }
    )

    assert read == {
        "count": 2,
        "clients": [
            {"host": HOST, "sref": SREF, "name": "TVP 1 HD", "bouquet": None,
             "transcoded": False},
            {"host": HOST_V6, "sref": None, "name": None, "bouquet": None,
             "transcoded": None},
        ],
    }
    assert stream.normalise_stream_clients({}) == {"count": 0, "clients": []}
    assert stream.normalise_stream_clients({"count": True, "clients": "none"}) == {
        "count": 0,
        "clients": [],
    }
    assert stream.normalise_stream_clients({"count": -1, "clients": []})["count"] == 0
    long_host = stream.normalise_stream_clients({"clients": [{"host": "a" * 200}]})
    assert long_host["clients"][0]["host"] == "a" * 64


# ------------------------------------------------------------ hidden bouquets


async def test_a_client_on_a_hidden_bouquet_is_shown_without_its_channel(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The host and "transcoded" stay; the name, the reference and the bouquet go."""
    streaming(box_on_the_broker)
    entry = hiding(config_entry, "Sport")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state.state == "2"
    assert state.attributes["clients"] == [{**ROW_VISIBLE, "hidden": False}, HIDDEN_ROW]
    everything = json.dumps(state.as_dict(), default=str)
    assert EUROSPORT not in everything
    assert "Eurosport" not in everything
    assert "Sport" not in everything


async def test_a_channel_of_a_hidden_bouquet_reached_through_another_is_hidden_too(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The receiver names the first bouquet that holds it; the hidden one holds it too."""
    channels = json.loads(json.dumps(CHANNELS))
    channels["bouquets"][1]["channels"].append({"sref": SREF_TWO, "name": "TVN HD"})
    box_on_the_broker[CHANNELS_TOPIC] = json.dumps(channels)
    row = {**ROW_VISIBLE, "sref": SREF_TWO, "name": "TVN HD"}
    streaming(box_on_the_broker, {"count": 1, "clients": [row]})
    entry = hiding(config_entry, "Sport")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    (shown,) = hass.states.get(SENSOR).attributes["clients"]
    assert shown == {
        "host": HOST, "sref": None, "name": None, "bouquet": None, "transcoded": False,
        "hidden": True,
    }


@pytest.mark.parametrize(
    "row",
    [
        # In none of the configured bouquets: the receiver says null.
        {**ROW_VISIBLE, "bouquet": None},
        # A bouquet this receiver does not publish.
        {**ROW_VISIBLE, "bouquet": "Radio"},
        # No reference at all, and a name.
        {**ROW_VISIBLE, "sref": None},
    ],
)
async def test_a_client_that_cannot_be_checked_is_hidden_while_anything_is(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    row: dict[str, Any],
) -> None:
    """It fails toward hiding, as the zap history's filter does."""
    streaming(box_on_the_broker, {"count": 1, "clients": [row]})
    entry = hiding(config_entry, "Sport")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    (shown,) = hass.states.get(SENSOR).attributes["clients"]
    assert shown["hidden"] is True
    assert shown["name"] is None
    assert shown["sref"] is None


async def test_the_same_client_is_shown_while_nothing_is_hidden(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """With the option empty nothing is checked and every row is as published."""
    row = {**ROW_VISIBLE, "bouquet": None}
    streaming(box_on_the_broker, {"count": 1, "clients": [row]})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(SENSOR).attributes["clients"] == [{**row, "hidden": False}]


async def test_every_client_is_hidden_while_a_hidden_bouquet_has_no_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Which channels to hide is exactly what is not known then."""
    streaming(box_on_the_broker)
    entry = hiding(config_entry, "Sport")
    with patch.object(Enigma2Box, "_members_of_hidden", return_value=None):
        await async_setup_box_then_retained(hass, entry, box_on_the_broker)

        rows = hass.states.get(SENSOR).attributes["clients"]

    assert [row["hidden"] for row in rows] == [True, True]
    assert all(row["sref"] is None and row["name"] is None for row in rows)


async def test_a_changed_channel_list_is_applied_to_the_rows(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The hidden bouquet gains the channel being streamed: the row is hidden at once."""
    streaming(box_on_the_broker, {"count": 1, "clients": [ROW_VISIBLE]})
    entry = hiding(config_entry, "Sport")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    assert hass.states.get(SENSOR).attributes["clients"][0]["hidden"] is False

    channels = json.loads(json.dumps(CHANNELS))
    channels["bouquets"][1]["channels"].append({"sref": SREF, "name": "TVP 1 HD"})
    async_fire_mqtt_message(hass, CHANNELS_TOPIC, json.dumps(channels))
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR).attributes["clients"][0]["hidden"] is True


# ------------------------------------------------------------------ the action


async def _stop(hass: HomeAssistant, **data: str) -> None:
    await hass.services.async_call(
        DOMAIN, "stop_stream", {ATTR_ENTITY_ID: PLAYER, **data}, blocking=True
    )


async def _arm_refusal(hass: HomeAssistant, error: str, reason: str) -> None:
    """Make the fake receiver refuse `cmd/stream_stop`, saying the command's id back."""

    @callback
    def _command_received(msg: ReceiveMessage) -> None:
        command_id, _payload = opened_command(msg.payload)
        refusal = {"cmd": "stream_stop", "error": error, "reason": reason, "ts": 1789459213}
        if command_id is not None:
            refusal["id"] = command_id
        async_fire_mqtt_message(hass, LAST_ERROR_TOPIC, json.dumps(refusal))

    await mqtt.async_subscribe(hass, command_topic("stream_stop"), _command_received)


async def test_stop_names_one_client_by_host_and_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Both members and nothing else, in the envelope a plugin with ids takes."""
    streaming(box_on_the_broker, CLIENTS, "command_id")
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_ack(hass, "stream_stop")

    await _stop(hass, host=HOST, sref=SREF)

    (sent,) = published_payloads(mqtt_mock, command_topic("stream_stop"))
    command_id, payload = opened_command(sent)
    assert command_id
    assert json.loads(payload) == {"host": HOST, "sref": SREF}


async def test_stop_without_the_capability_is_refused_here(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An image whose server lists clients and cannot stop one claims only the first."""
    box_on_the_broker[INFO_TOPIC] = info_with("stream_clients")
    box_on_the_broker[CLIENTS_TOPIC] = json.dumps(CLIENTS)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    with pytest.raises(ServiceValidationError) as raised:
        await _stop(hass, host=HOST, sref=SREF)

    assert raised.value.translation_key == "stream_stop_unsupported"
    assert published_payloads(mqtt_mock, command_topic("stream_stop")) == []


async def test_a_client_that_has_left_is_the_translated_refusal(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`not_connected`: nothing was done, and the caller can see why."""
    streaming(box_on_the_broker, CLIENTS, "command_id")
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await _arm_refusal(hass, "that stream client is not connected", "not_connected")

    with pytest.raises(ServiceValidationError) as raised:
        await _stop(hass, host=HOST, sref=SREF)

    assert raised.value.translation_key == "stream_stop_not_connected"


async def test_a_client_the_image_did_not_stop_is_a_failure(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`not_stopped`: the receiver tried and did not manage - not a refusal."""
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await _arm_refusal(hass, "the receiver did not stop that stream client", "not_stopped")

    with pytest.raises(HomeAssistantError) as raised:
        await _stop(hass, host=HOST, sref=SREF)

    assert raised.value.translation_key == "stream_stop_not_stopped"
    assert not isinstance(raised.value, ServiceValidationError)


async def test_a_refusal_without_a_known_code_carries_the_receivers_sentence(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await _arm_refusal(hass, "stopping is not possible right now", "busy_elsewhere")

    with pytest.raises(HomeAssistantError) as raised:
        await _stop(hass, host=HOST, sref=SREF)

    assert raised.value.translation_key == "command_refused"
    assert raised.value.translation_placeholders == {
        "command": "stream_stop",
        "reason": "stopping is not possible right now",
    }


async def test_a_hidden_client_is_stopped_by_its_host_alone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reference is held here and sent to the receiver; nothing else ever has it."""
    streaming(box_on_the_broker)
    entry = hiding(config_entry, "Sport")
    caplog.set_level(logging.DEBUG, logger="custom_components.enigma2_mqtt")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await async_arm_box_ack(hass, "stream_stop")
    events: list[Event] = []
    hass.bus.async_listen("*", events.append)

    await _stop(hass, host=HOST_V6)
    await hass.async_block_till_done()

    (sent,) = published_payloads(mqtt_mock, command_topic("stream_stop"))
    assert json.loads(opened_command(sent)[1]) == {"host": HOST_V6, "sref": EUROSPORT}
    ours = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.enigma2_mqtt")
    ]
    assert not [line for line in ours if EUROSPORT in line or "Eurosport" in line]
    for event in events:
        assert EUROSPORT not in json.dumps(event.as_dict(), default=str)
    for state in hass.states.async_all():
        if state.entity_id.startswith("sensor.dekoder_salon_stream"):
            assert EUROSPORT not in json.dumps(state.as_dict(), default=str)


async def test_the_host_is_matched_as_the_image_may_spell_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Upper case and the IPv4-mapped form name the same client."""
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_ack(hass, "stream_stop")

    await _stop(hass, host=f" ::FFFF:{HOST} ")

    (sent,) = published_payloads(mqtt_mock, command_topic("stream_stop"))
    assert json.loads(opened_command(sent)[1]) == {"host": f"::FFFF:{HOST}", "sref": SREF}


async def test_a_host_that_is_not_on_the_list_is_refused_before_anything_is_sent(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    with pytest.raises(ServiceValidationError) as raised:
        await _stop(hass, host="192.0.2.99")

    assert raised.value.translation_key == "stream_client_unknown"
    assert raised.value.translation_placeholders == {"host": "192.0.2.99"}
    assert published_payloads(mqtt_mock, command_topic("stream_stop")) == []


async def test_a_host_with_two_streams_needs_the_reference(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Nothing here ever stops "all of them", and nothing guesses which one."""
    rows = [ROW_VISIBLE, {**ROW_VISIBLE, "sref": SREF_TWO, "name": "TVN HD"}]
    streaming(box_on_the_broker, {"count": 2, "clients": rows})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    with pytest.raises(ServiceValidationError) as raised:
        await _stop(hass, host=HOST)

    assert raised.value.translation_key == "stream_client_ambiguous"
    assert published_payloads(mqtt_mock, command_topic("stream_stop")) == []


async def test_one_service_sent_twice_to_one_host_is_one_client(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Once transcoded and once not: the plugin ends the first, a second call the other."""
    rows = [ROW_VISIBLE, {**ROW_VISIBLE, "transcoded": True}]
    streaming(box_on_the_broker, {"count": 2, "clients": rows})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_ack(hass, "stream_stop")

    await _stop(hass, host=HOST)

    (sent,) = published_payloads(mqtt_mock, command_topic("stream_stop"))
    assert json.loads(opened_command(sent)[1]) == {"host": HOST, "sref": SREF}


async def test_host_alone_means_the_hidden_stream_when_the_host_has_a_shown_one_too(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The shown one can be named by its reference; the hidden one cannot."""
    rows = [ROW_VISIBLE, {**ROW_SPORT, "host": HOST}]
    streaming(box_on_the_broker, {"count": 2, "clients": rows})
    entry = hiding(config_entry, "Sport")
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)
    await async_arm_box_ack(hass, "stream_stop")

    await _stop(hass, host=HOST)

    (sent,) = published_payloads(mqtt_mock, command_topic("stream_stop"))
    assert json.loads(opened_command(sent)[1]) == {"host": HOST, "sref": EUROSPORT}

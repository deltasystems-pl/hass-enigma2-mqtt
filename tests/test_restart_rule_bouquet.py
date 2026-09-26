"""The channel-list bouquet across a restart, through the plugin's own topics.

`lastservice` restores the channel, not the list channel up and down walk. Home Assistant
holds the MQTT session on the SSH paths, so it records the plugin's retained `bouquet`
before the restart and, when a plugin that publishes one is running afterwards, puts it
back with `cmd/bouquet` - but only where that cannot change the channel: `cmd/bouquet`
tunes the bouquet's first channel when the one playing is not in it.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import HomeAssistant, callback
import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.enigma2_mqtt import restart_rule
from custom_components.enigma2_mqtt.installer import CommandResult
from custom_components.enigma2_mqtt.restart_rule import RestartRecord

from .conftest import (
    BASE_TOPIC,
    BOUQUET_TOPIC,
    CHANNELS,
    CHANNELS_TOPIC,
    NODE_ID,
    SREF,
    command_topic,
    published_payloads,
)

FAVOURITES = CHANNELS["bouquets"][0]["sref"]
SPORT = CHANNELS["bouquets"][1]["sref"]


@pytest.fixture(autouse=True)
def broker_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long enough for Home Assistant's debounced SUBSCRIBE to be confirmed.

    The command waits for the broker to confirm the subscription before it goes out,
    and that confirmation comes after the MQTT client's subscribe cooldown.
    """
    monkeypatch.setattr(restart_rule, "BOUQUET_REPLAY_SECONDS", 3.0)
    monkeypatch.setattr(restart_rule, "BOUQUET_ACK_SECONDS", 3.0)


class _Receiver:
    """Just enough SSH to answer the helper's `record`."""

    def __init__(self, service: str | None) -> None:
        self.service = service

    async def run(self, command: str, **kwargs: Any) -> CommandResult:
        del kwargs
        assert command.endswith(" record")
        return CommandResult(0, json.dumps({"service": self.service, "standby": False}))


async def test_the_record_takes_the_bouquet_and_whether_the_channel_is_in_it(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Sport", "sref": SPORT})
    retained[CHANNELS_TOPIC] = json.dumps(CHANNELS)

    in_sport = await restart_rule.async_record(
        hass, _Receiver(SREF), "/tmp/helper.py", BASE_TOPIC, NODE_ID
    )
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Ulubione TV", "sref": FAVOURITES})
    in_favourites = await restart_rule.async_record(
        hass, _Receiver(SREF), "/tmp/helper.py", BASE_TOPIC, NODE_ID
    )

    assert in_sport == RestartRecord(SREF, False, SPORT, False)
    assert in_favourites == RestartRecord(SREF, False, FAVOURITES, True)


async def test_a_record_without_a_plugin_that_publishes_a_bouquet_has_none(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    record = await restart_rule.async_record(
        hass, _Receiver(SREF), "/tmp/helper.py", BASE_TOPIC, NODE_ID
    )

    assert record == RestartRecord(SREF, False, None, False)


async def _answering_bouquet(hass: HomeAssistant, answer: str | None) -> None:
    """A plugin that answers `cmd/bouquet` with a fresh `bouquet`, as the contract says."""

    @callback
    def _command(message: ReceiveMessage) -> None:
        if answer is not None:
            async_fire_mqtt_message(
                hass, BOUQUET_TOPIC, json.dumps({"name": "x", "sref": answer})
            )

    await mqtt.async_subscribe(hass, command_topic("bouquet"), _command)


async def test_a_bouquet_that_moved_is_put_back_and_proved_by_the_answer(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Sport", "sref": SPORT})
    await _answering_bouquet(hass, FAVOURITES)

    outcome = await restart_rule.async_restore_bouquet(
        hass, BASE_TOPIC, NODE_ID, RestartRecord(SREF, False, FAVOURITES, True), "kept"
    )

    assert outcome == "restored"
    assert published_payloads(mqtt_mock, command_topic("bouquet")) == [
        json.dumps({"sref": FAVOURITES})
    ]


async def test_a_bouquet_that_stayed_is_kept_and_nothing_is_sent(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Ulubione TV", "sref": FAVOURITES})

    outcome = await restart_rule.async_restore_bouquet(
        hass, BASE_TOPIC, NODE_ID, RestartRecord(SREF, False, FAVOURITES, True), "kept"
    )

    assert outcome == "kept"
    assert published_payloads(mqtt_mock, command_topic("bouquet")) == []


@pytest.mark.parametrize(
    ("record", "channel"),
    [
        # `cmd/bouquet` would tune the bouquet's first channel.
        (RestartRecord(SREF, False, SPORT, False), "kept"),
        # The channel is not where it should be; a bouquet change could move it again.
        (RestartRecord(SREF, False, FAVOURITES, True), "lost"),
        (RestartRecord(SREF, False, None, False), "kept"),
    ],
)
async def test_a_bouquet_is_never_sent_where_it_could_change_the_channel(
    hass: HomeAssistant,
    mqtt_mock: Any,
    retained: dict[str, str | bytes],
    record: RestartRecord,
    channel: str,
) -> None:
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Other", "sref": "1:7:1:0:0:0:0:0:0:0:x"})

    outcome = await restart_rule.async_restore_bouquet(hass, BASE_TOPIC, NODE_ID, record, channel)

    assert outcome == "not restored"
    assert published_payloads(mqtt_mock, command_topic("bouquet")) == []


async def test_a_plugin_that_does_not_answer_leaves_the_bouquet_not_restored(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Sport", "sref": SPORT})
    await _answering_bouquet(hass, None)

    outcome = await restart_rule.async_restore_bouquet(
        hass, BASE_TOPIC, NODE_ID, RestartRecord(SREF, False, FAVOURITES, True), "restored"
    )

    assert outcome == "not restored"


async def test_no_running_plugin_with_a_bouquet_means_none_is_sent(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    outcome = await restart_rule.async_restore_bouquet(
        hass, BASE_TOPIC, NODE_ID, RestartRecord(SREF, False, FAVOURITES, True), "kept"
    )

    assert outcome == "not restored"
    assert published_payloads(mqtt_mock, command_topic("bouquet")) == []

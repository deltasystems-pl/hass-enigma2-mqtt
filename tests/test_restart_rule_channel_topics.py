"""The restart record on a receiver whose channel list is one topic per bouquet.

Before a restart the installer records the receiver's channel-list bouquet and whether
the channel being watched is in it, because `cmd/bouquet` tunes a bouquet's first channel
when the current one is not a member. It reads the plugin's retained topics itself - it
runs where no receiver has been set up yet - and until now that meant `channels`. A
plugin after 0.4.0 does not publish `channels` on a receiver with a very large list; the
same lists are then on `bouquets` and `channels/<slug>`, and the record has to come out
the same from either.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant
import pytest

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
)

BOUQUETS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/bouquets"
FAVOURITES = CHANNELS["bouquets"][0]
SPORT = CHANNELS["bouquets"][1]
EUROSPORT = SPORT["channels"][0]["sref"]
# Not what the names would slug to: the slug is the index's to give.
SLUGS = {"Ulubione TV": "ulubione_tv_2", "Sport": "sport"}


@pytest.fixture(autouse=True)
def broker_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long enough for Home Assistant's debounced SUBSCRIBE to be confirmed."""
    monkeypatch.setattr(restart_rule, "BOUQUET_REPLAY_SECONDS", 1.0)


class _Receiver:
    """Just enough SSH to answer the helper's `record`."""

    def __init__(self, service: str | None) -> None:
        self.service = service

    async def run(self, command: str, **kwargs: Any) -> CommandResult:
        del kwargs
        assert command.endswith(" record")
        return CommandResult(0, json.dumps({"service": self.service, "standby": False}))


def list_topic(slug: str) -> str:
    return f"{BASE_TOPIC}/{NODE_ID}/channels/{slug}"


def index(*bouquets: dict[str, Any]) -> str:
    return json.dumps(
        {
            "generated": 1789459200,
            "bouquets": [
                {
                    "name": bouquet["name"],
                    "sref": bouquet["sref"],
                    "slug": SLUGS[bouquet["name"]],
                    "count": len(bouquet["channels"]),
                }
                for bouquet in bouquets
            ],
        }
    )


def own_list(bouquet: dict[str, Any]) -> str:
    return json.dumps(
        {
            "bouquet": bouquet["name"],
            "sref": bouquet["sref"],
            "generated": 1789459200,
            "channels": bouquet["channels"],
        }
    )


def publish_lists(store: dict[str, str | bytes], source: str) -> None:
    """Put the example receiver's channel list on the broker, one way or the other."""
    if source == "channels":
        store[CHANNELS_TOPIC] = json.dumps(CHANNELS)
        return
    store[BOUQUETS_TOPIC] = index(FAVOURITES, SPORT)
    store[list_topic(SLUGS["Ulubione TV"])] = own_list(FAVOURITES)
    store[list_topic(SLUGS["Sport"])] = own_list(SPORT)
    if source == "retracted_channels":
        # What a withheld `channels` looks like to a subscriber that still gets one.
        store[CHANNELS_TOPIC] = ""


async def _record(hass: HomeAssistant, service: str | None = SREF) -> RestartRecord:
    return await restart_rule.async_record(
        hass, _Receiver(service), "/tmp/helper.py", BASE_TOPIC, NODE_ID
    )


@pytest.mark.parametrize("source", ["channels", "bouquet_topics", "retracted_channels"])
async def test_the_record_is_the_same_from_either_way_of_publishing(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes], source: str
) -> None:
    publish_lists(retained, source)

    retained[BOUQUET_TOPIC] = json.dumps({"name": "Sport", "sref": SPORT["sref"]})
    in_sport = await _record(hass)
    # The second bouquet of the index, and a channel that is in it.
    eurosport_in_sport = await _record(hass, EUROSPORT)
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Ulubione TV", "sref": FAVOURITES["sref"]})
    in_favourites = await _record(hass)
    # By identity, not by spelling, as from `channels`.
    lower_case = await _record(hass, SREF.lower().rstrip(":"))
    nothing_playing = await _record(hass, None)

    assert in_sport == RestartRecord(SREF, False, SPORT["sref"], False)
    assert eurosport_in_sport == RestartRecord(EUROSPORT, False, SPORT["sref"], True)
    assert in_favourites == RestartRecord(SREF, False, FAVOURITES["sref"], True)
    assert lower_case.bouquet_holds_service is True
    assert nothing_playing == RestartRecord(None, False, FAVOURITES["sref"], False)


async def test_a_receiver_that_publishes_channels_is_asked_for_nothing_else(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    """An older plugin's record costs the two reads it always did.

    The index and the per-bouquet topic are each a subscription and a wait; they are
    spent only on a receiver whose `channels` is not there.
    """
    publish_lists(retained, "channels")
    # Lingering after a downgrade, and saying something else: not read.
    retained[BOUQUETS_TOPIC] = index(FAVOURITES)
    retained[list_topic(SLUGS["Ulubione TV"])] = own_list({**FAVOURITES, "channels": []})
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Ulubione TV", "sref": FAVOURITES["sref"]})
    subscribed: list[str] = []
    real_subscribe = mqtt.async_subscribe

    async def _spy(hass_: HomeAssistant, topic: str, *args: Any, **kwargs: Any) -> Any:
        subscribed.append(topic)
        return await real_subscribe(hass_, topic, *args, **kwargs)

    with patch("homeassistant.components.mqtt.async_subscribe", _spy):
        record = await _record(hass)

    assert record == RestartRecord(SREF, False, FAVOURITES["sref"], True)
    assert sorted(subscribed) == sorted([BOUQUET_TOPIC, CHANNELS_TOPIC])


async def test_without_a_bouquet_the_channel_topics_are_not_read_either(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    publish_lists(retained, "bouquet_topics")
    subscribed: list[str] = []
    real_subscribe = mqtt.async_subscribe

    async def _spy(hass_: HomeAssistant, topic: str, *args: Any, **kwargs: Any) -> Any:
        subscribed.append(topic)
        return await real_subscribe(hass_, topic, *args, **kwargs)

    with patch("homeassistant.components.mqtt.async_subscribe", _spy):
        record = await _record(hass)

    assert record == RestartRecord(SREF, False, None, False)
    assert sorted(subscribed) == sorted([BOUQUET_TOPIC, CHANNELS_TOPIC])


async def test_the_bouquets_topic_is_found_by_the_index_slug_not_by_its_name(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes]
) -> None:
    """`channels/ulubione_tv` holds another bouquet's list; the index says `_2` is ours."""
    publish_lists(retained, "bouquet_topics")
    retained[list_topic("ulubione_tv")] = own_list({**SPORT, "sref": "1:7:1:0:0:0:0:0:0:0:other"})
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Ulubione TV", "sref": FAVOURITES["sref"]})

    assert (await _record(hass)).bouquet_holds_service is True


@pytest.mark.parametrize(
    "broken",
    [
        "withheld",
        "retracted",
        "no_index",
        "not_in_index",
        "no_slug",
        "slug_with_a_level",
        "another_bouquets_list",
        "no_list",
        "unreadable",
    ],
)
async def test_a_list_that_cannot_be_read_leaves_the_bouquet_alone(
    hass: HomeAssistant, mqtt_mock: Any, retained: dict[str, str | bytes], broken: str
) -> None:
    """Every one of them answers "not in it", which never sends `cmd/bouquet`.

    Restoring a bouquet that does not hold the channel would change the channel the
    restart was meant to keep; not restoring it costs one press on the remote.
    """
    publish_lists(retained, "bouquet_topics")
    retained[BOUQUET_TOPIC] = json.dumps({"name": "Ulubione TV", "sref": FAVOURITES["sref"]})
    own = list_topic(SLUGS["Ulubione TV"])
    assert (await _record(hass)).bouquet_holds_service is True
    if broken == "withheld":
        del retained[own]
    elif broken == "retracted":
        retained[own] = ""
    elif broken == "no_index":
        del retained[BOUQUETS_TOPIC]
    elif broken == "not_in_index":
        retained[BOUQUETS_TOPIC] = index(SPORT)
    elif broken == "no_slug":
        entries = json.loads(index(FAVOURITES, SPORT))
        entries["bouquets"][0]["slug"] = ""
        retained[BOUQUETS_TOPIC] = json.dumps(entries)
    elif broken == "slug_with_a_level":
        entries = json.loads(index(FAVOURITES, SPORT))
        entries["bouquets"][0]["slug"] = "../sport"
        retained[BOUQUETS_TOPIC] = json.dumps(entries)
        retained[f"{BASE_TOPIC}/{NODE_ID}/channels/../sport"] = own_list(FAVOURITES)
    elif broken == "another_bouquets_list":
        retained[own] = own_list({**FAVOURITES, "sref": SPORT["sref"]})
    elif broken == "no_list":
        retained[own] = json.dumps({"bouquet": "Ulubione TV", "sref": FAVOURITES["sref"]})
    else:
        retained[own] = "not json"

    record = await _record(hass)

    assert record == RestartRecord(SREF, False, FAVOURITES["sref"], False)

"""The receiver's stream address for the channel on the decoder, and a name for it.

A plugin after 0.4.0 says where the image's stream server is on `stream`: a port, the
form of the path, and whether it wants a login. It publishes no host and no URL; the
address is built here, at the host Home Assistant already asks for picons. The media
player carries it as an attribute, with a stable name derived from the service
reference alone. No address is ever built with a login in it, and a channel of a
hidden bouquet gets neither an address nor a name.
"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt import stream_facts as stream
from custom_components.enigma2_mqtt.const import CONF_RECEIVER_HOST
from custom_components.enigma2_mqtt.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    IP,
    NODE_ID,
    SERVICE,
    SERVICE_TOPIC,
    SREF,
    SREF_TWO,
    async_setup_box_then_retained,
)

STREAM_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/stream"
CLIENTS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/stream_clients"
PLAYER = "media_player.dekoder_salon"
STREAM: dict[str, Any] = {
    "port": 8001, "path": "/{sref}", "authentication": False, "encoder": True,
    "encoder_port": 8002,
}
URL = f"http://{IP}:8001/{SREF}"
NAME = "ch_1_0_19_283d_3fb_1_c00000_0_0_0"
EUROSPORT = "1:0:19:1234:3F3:1:C00000:0:0:0:"
IPTV = "4097:0:1:0:0:0:0:0:0:0:http%3a//iptv.example/live 1/index.m3u8:Kanal 1"


def info_with(*capabilities: str) -> str:
    return json.dumps({**INFO, "capabilities": [*INFO["capabilities"], *capabilities]})


def streaming(store: dict[str, str | bytes], stream_payload: Any = STREAM) -> None:
    store[INFO_TOPIC] = info_with("stream")
    store[STREAM_TOPIC] = (
        stream_payload if isinstance(stream_payload, str) else json.dumps(stream_payload)
    )


def with_data(entry: MockConfigEntry, **extra: Any) -> MockConfigEntry:
    options = extra.pop("options", {})
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data={**entry.data, **extra},
        options=options,
    )


def attributes(hass: HomeAssistant) -> dict[str, Any]:
    return dict(hass.states.get(PLAYER).attributes)


async def test_without_the_capability_the_player_has_no_stream_attributes(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A stray `stream` payload is not a claim."""
    box_on_the_broker[STREAM_TOPIC] = json.dumps(STREAM)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    shown = attributes(hass)
    assert "stream_url" not in shown
    assert "stream_name" not in shown
    assert "stream_authentication" not in shown


async def test_the_player_carries_the_address_and_the_name_of_the_channel_that_is_on(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Port and path from the receiver, host as for picons, colons kept."""
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    shown = attributes(hass)
    assert shown["stream_url"] == URL
    assert shown["stream_name"] == NAME
    assert shown["stream_authentication"] is False


async def test_a_channel_change_changes_both(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(
        hass, SERVICE_TOPIC, json.dumps({**SERVICE, "sref": SREF_TWO, "name": "TVN HD"})
    )
    await hass.async_block_till_done()

    shown = attributes(hass)
    assert shown["stream_url"] == f"http://{IP}:8001/{SREF_TWO}"
    assert shown["stream_name"] == "ch_1_0_19_2b66_3f3_1_c00000_0_0_0"


async def test_a_typed_host_is_the_one_in_the_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The administrator's word, as for picons - not what the broker says."""
    streaming(box_on_the_broker)
    entry = with_data(config_entry, **{CONF_RECEIVER_HOST: "receiver.example"})
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    assert attributes(hass)["stream_url"] == f"http://receiver.example:8001/{SREF}"


async def test_a_typed_ipv6_host_is_bracketed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    streaming(box_on_the_broker)
    entry = with_data(config_entry, **{CONF_RECEIVER_HOST: "2001:db8::12"})
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    assert attributes(hass)["stream_url"] == f"http://[2001:db8::12]:8001/{SREF}"


async def test_a_typed_host_that_is_no_web_host_means_no_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Never the reported address once a host was typed. The name does not need one."""
    streaming(box_on_the_broker)
    entry = with_data(config_entry, **{CONF_RECEIVER_HOST: "fe80::1%eth0"})
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    shown = attributes(hass)
    assert shown["stream_url"] is None
    assert shown["stream_name"] == NAME


async def test_a_reported_address_that_is_no_receiver_means_no_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Anything on the broker can write `info.ip`; loopback is not where a receiver is."""
    streaming(box_on_the_broker)
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "ip": "127.0.0.1", "capabilities": [*INFO["capabilities"], "stream"]}
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert attributes(hass)["stream_url"] is None


async def test_a_stream_server_that_wants_a_login_gets_no_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """No address is built with a login in it, and none that would only answer 401."""
    streaming(box_on_the_broker, {**STREAM, "authentication": True})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    shown = attributes(hass)
    assert shown["stream_url"] is None
    assert shown["stream_authentication"] is True
    assert shown["stream_name"] == NAME
    assert "@" not in json.dumps(shown, default=str)


async def test_an_image_without_the_login_setting_gets_an_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`null` is "no such setting", which is not a login."""
    streaming(box_on_the_broker, {**STREAM, "authentication": None})
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    shown = attributes(hass)
    assert shown["stream_url"] == URL
    assert shown["stream_authentication"] is None


async def test_a_channel_of_a_hidden_bouquet_gets_no_address_and_no_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(
        {**SERVICE, "sref": EUROSPORT, "name": "Eurosport 1", "bouquet": "Sport"}
    )
    streaming(box_on_the_broker)
    entry = with_data(config_entry, options={"history_hidden_bouquets": ["Sport"]})
    await async_setup_box_then_retained(hass, entry, box_on_the_broker)

    shown = attributes(hass)
    assert shown["stream_url"] is None
    assert shown["stream_name"] is None

    async_fire_mqtt_message(hass, SERVICE_TOPIC, json.dumps(SERVICE))
    await hass.async_block_till_done()
    assert attributes(hass)["stream_url"] == URL


async def test_a_retracted_stream_topic_takes_the_address_away(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert attributes(hass)["stream_url"] == URL

    async_fire_mqtt_message(hass, STREAM_TOPIC, "")
    await hass.async_block_till_done()

    shown = attributes(hass)
    assert shown["stream_url"] is None
    assert shown["stream_authentication"] is None


async def test_standby_has_no_service_and_so_no_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """With nothing playing every member of `service` is null."""
    streaming(box_on_the_broker)
    box_on_the_broker[SERVICE_TOPIC] = json.dumps(dict.fromkeys(SERVICE))
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    shown = attributes(hass)
    assert shown["stream_url"] is None
    assert shown["stream_name"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {**STREAM, "port": "8001"},
        {**STREAM, "port": 0},
        {**STREAM, "port": 70000},
        {**STREAM, "port": True},
        {"path": "/{sref}", "authentication": False},
        {**STREAM, "path": "/stream"},
        {**STREAM, "path": "{sref}"},
        {**STREAM, "path": "/{sref}?user=root"},
        {**STREAM, "path": "@other.example/{sref}"},
        {**STREAM, "path": "/{sref}#x"},
        {**STREAM, "path": "/ {sref}"},
        {**STREAM, "path": None},
        {"port": 8001},
    ],
)
async def test_a_stream_payload_that_cannot_be_used_means_no_address(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    payload: dict[str, Any],
) -> None:
    """The payload arrives from a broker; only a port and a plain path make an address."""
    streaming(box_on_the_broker, payload)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert attributes(hass)["stream_url"] is None


async def test_unknown_members_are_ignored_and_another_path_form_is_followed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The receiver publishes the form of the path so that nobody has to know it."""
    streaming(
        box_on_the_broker,
        {"port": 18001, "path": "/live/{sref}.ts", "authentication": False, "tls": False},
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert attributes(hass)["stream_url"] == f"http://{IP}:18001/live/{SREF}.ts"


async def test_a_payload_that_is_not_json_keeps_the_last_good_one(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    streaming(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    async_fire_mqtt_message(hass, STREAM_TOPIC, "{port")
    await hass.async_block_till_done()

    assert attributes(hass)["stream_url"] == URL


def test_the_reference_is_encoded_the_way_the_receiver_reads_it() -> None:
    """Colons stay, the trailing one too; everything else that is not plain is escaped."""
    read = stream.normalise_stream(STREAM)

    assert stream.build_stream_url("http://192.0.2.12/", read, SREF) == URL
    assert stream.build_stream_url("http://192.0.2.12/", read, IPTV) == (
        "http://192.0.2.12:8001/4097:0:1:0:0:0:0:0:0:0:"
        "http%253a%2F%2Fiptv.example%2Flive%201%2Findex.m3u8:Kanal%201"
    )
    assert stream.build_stream_url(None, read, SREF) is None
    assert stream.build_stream_url("http://192.0.2.12/", None, SREF) is None
    assert stream.build_stream_url("http://192.0.2.12/", read, "") is None
    assert stream.build_stream_url("http://192.0.2.12/", read, None) is None


def test_the_stream_name_is_derived_from_the_reference_alone() -> None:
    """Lower case, every run of anything else one underscore, the ends stripped, `ch_`."""
    assert stream.stream_name(SREF) == NAME
    assert stream.stream_name("1:0:19:283D:3FB:1:C00000:0:0:0") == NAME
    assert stream.stream_name("1:0:1:445C:453:1:C00000:0:0:0:") == (
        "ch_1_0_1_445c_453_1_c00000_0_0_0"
    )
    assert stream.stream_name(IPTV) == (
        "ch_4097_0_1_0_0_0_0_0_0_0_http_3a_iptv_example_live_1_index_m3u8_kanal_1"
    )
    assert stream.stream_name("::: :::") is None
    assert stream.stream_name("") is None
    assert stream.stream_name(None) is None
    assert stream.stream_name(19) is None


async def test_the_download_carries_the_stream_facts_and_no_client(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The port and the path answer a bug report; who is watching what does not."""
    streaming(box_on_the_broker)
    box_on_the_broker[CLIENTS_TOPIC] = json.dumps(
        {
            "count": 2,
            "clients": [
                {"host": "192.0.2.20", "sref": SREF, "name": "TVP 1 HD",
                 "bouquet": "Ulubione TV", "transcoded": False},
                {"host": "192.0.2.21", "sref": EUROSPORT, "name": "Eurosport 1",
                 "bouquet": "Sport", "transcoded": True},
            ],
        }
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    download = await async_get_config_entry_diagnostics(hass, config_entry)

    assert download["topics"]["stream"] == STREAM
    assert download["topics"]["stream_clients"] == {"count": 2, "rows": 2, "transcoded": 1}
    text = json.dumps(download)
    assert "192.0.2.20" not in text
    assert "192.0.2.21" not in text
    assert EUROSPORT not in text
    assert "Eurosport" not in text

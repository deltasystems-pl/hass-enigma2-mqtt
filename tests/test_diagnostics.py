"""The diagnostics download is safe to attach to a public issue."""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import patch

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.bundle import BundleError
from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    AVAILABILITY_TOPIC,
    BOX_NAME,
    BOXTYPE,
    EPG_GRID,
    EPG_GRID_TOPIC,
    IMAGE,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    PLUGIN_VERSION,
    SCREEN,
    SREF,
    async_setup_box,
)

# Fields whose value is a version. A version may be four dotted numbers - `6.6.0.1` is
# an ordinary image or driver version - which is also the shape of an IPv4 address, so
# the address sweep below skips them by name rather than hoping none ever appears.
VERSION_KEYS = frozenset({"enigma", "image", "plugin", "sw_version", "version"})


def _without_version_fields(value: Any) -> Any:
    """Return the download with every version-valued field dropped."""
    if isinstance(value, dict):
        return {
            key: _without_version_fields(item)
            for key, item in value.items()
            if key not in VERSION_KEYS
        }
    if isinstance(value, list):
        return [_without_version_fields(item) for item in value]
    return value


async def test_diagnostics_redact_what_identifies_a_household(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """The MAC, the address and every credential key are redacted; the rest stays."""
    await async_setup_box(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["announcement"]["mac"] == REDACTED
    assert diagnostics["announcement"]["ip"] == REDACTED
    assert diagnostics["info"]["mac"] == REDACTED
    assert diagnostics["info"]["ip"] == REDACTED
    assert diagnostics["device"]["configuration_url"] == REDACTED

    assert diagnostics["entry"]["unique_id"] == NODE_ID
    assert diagnostics["box"]["node_id"] == NODE_ID
    assert diagnostics["box"]["available"] is True
    assert "hdd" in diagnostics["box"]["capabilities"]
    assert diagnostics["announcement"]["name"] == BOX_NAME
    assert diagnostics["info"]["image"] == IMAGE
    assert diagnostics["info"]["plugin"] == PLUGIN_VERSION
    assert diagnostics["device"]["model"] == BOXTYPE
    assert diagnostics["device"]["manufacturer"] == "Vu+"


async def test_diagnostics_redact_credentials_that_do_not_exist_yet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """The installer of M4 cannot add a secret to a file that is already shared."""
    await async_setup_box(hass, config_entry)

    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            "username": "root",
            "password": "hunter2",
            "ssh_password": "hunter2",
            "broker_password": "hunter2",
            "broker_host": "broker.example",
            "broker_username": "receiver",
            "ssh_host": "receiver.example",
            "ssh_host_key": "ssh-ed25519 AAAAprivateidentity",
            "ssh_username": "root",
            "receiver_host": "receiver.example",
        },
    )
    await hass.async_block_till_done()

    entry_data = (await async_get_config_entry_diagnostics(hass, config_entry))["entry"]

    assert entry_data["data"]["username"] == REDACTED
    assert entry_data["data"]["password"] == REDACTED
    assert entry_data["data"]["ssh_password"] == REDACTED
    assert entry_data["data"]["broker_password"] == REDACTED
    assert entry_data["data"]["broker_host"] == REDACTED
    assert entry_data["data"]["broker_username"] == REDACTED
    assert entry_data["data"]["ssh_host"] == REDACTED
    assert entry_data["data"]["ssh_host_key"] == REDACTED
    assert entry_data["data"]["ssh_username"] == REDACTED
    assert entry_data["data"]["receiver_host"] == REDACTED
    assert entry_data["data"]["node_id"] == NODE_ID


async def test_diagnostics_carry_the_last_payload_of_every_topic(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A bug report about an entity is unanswerable without the topic behind it."""
    box_on_the_broker[EPG_GRID_TOPIC] = json.dumps(EPG_GRID)
    await async_setup_box(hass, config_entry)

    topics = (await async_get_config_entry_diagnostics(hass, config_entry))["topics"]

    assert topics["power"] == "on"
    assert topics["service"]["sref"] == SREF
    assert topics["epg"]["now"]["title"] == "Wiadomości"
    assert topics["tuner"]["snr"] == 78
    assert topics["volume"] == {"level": 35, "muted": False}
    assert topics["timers"][0]["state"] == "waiting"
    assert topics["hdd"]["mounted"] is True
    assert topics["last_error"] is None
    assert topics["epg_grid"]["ulubione_tv"]["channels"] == 1


async def test_diagnostics_summarise_the_screenshot_and_the_channel_list(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A picture of somebody's television and their channel list are not evidence.

    The size of the frame and the shape of the list answer every question a report
    asks; the contents answer none of them and describe a household.
    """
    await async_setup_box(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    topics = diagnostics["topics"]

    assert topics["screen"]["bytes"] == len(SCREEN)
    assert topics["screen"]["updated"] is not None
    assert topics["channels"]["bouquets"] == [
        {"name": "Ulubione TV", "channels": 2},
        {"name": "Sport", "channels": 2},
    ]
    # The key topic is who pressed what a moment ago, and is not state at all.
    assert "key" not in topics
    # A channel that is only in the list, never on the screen, does not appear at all.
    assert "Eurosport" not in json.dumps(diagnostics, default=str)


async def test_the_wake_on_lan_address_is_redacted_too(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """It is a hardware address of the same household the box's own MAC is hidden for.

    Found in a real download: every address the receiver announces was redacted and the
    one the user had typed into the options was printed in full, because it arrives from
    the other direction and under a different key.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "wol_mac": "00:00:5e:00:53:01"}
    )
    await async_setup_box(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["entry"]["options"]["wol_mac"] == REDACTED
    assert "00:00:5e:00:53:01" not in json.dumps(diagnostics)


async def test_no_address_at_all_survives_the_download(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """A single sweep for the shapes, rather than one assertion per known key.

    Every value is visited except the ones that hold a version. `6.6.0.1` is a perfectly
    ordinary image or driver version and is also four dotted numbers, so a blind sweep
    would start failing the day a fixture grew one - and the honest fix for that failure
    would have been to weaken the sweep. Excluding those fields by name keeps the sweep
    strict everywhere it matters.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "wol_mac": "00:00:5e:00:53:01"}
    )
    await async_setup_box(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    checked = _without_version_fields(diagnostics)
    text = json.dumps(checked)

    assert VERSION_KEYS, "the exclusion list must not be empty by accident"
    assert not re.findall(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", text, re.I)
    assert not re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    # The sweep still sees everything else, including what the receiver announced.
    assert "vuuno4kse_005301" in text


def test_the_sweep_would_catch_an_address_under_a_new_name() -> None:
    """The exclusion is by key, so a new key carrying an address is still caught."""
    text = json.dumps(_without_version_fields({"entry": {"some_new_mac": "00:00:5e:00:53:01"}}))

    assert re.findall(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", text, re.I)


async def test_the_bouquet_context_is_in_the_download(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str],
    config_entry: MockConfigEntry,
) -> None:
    """Every state topic belongs in a bug report, and this one was left out.

    The `bouquet` topic arrived with bouquet activation and was not added to the list,
    so the one piece of state that explains what channel up and down will do next was
    the one thing a report about channel up and down did not carry.
    """
    await async_setup_box(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert "bouquet" in diagnostics["topics"]
    assert diagnostics["topics"]["bouquet"] == config_entry.runtime_data.state.bouquet


async def test_the_download_states_the_plugin_mismatch_the_card_cannot_show(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver ahead of the integration reads "up to date" everywhere else.

    The version entity refuses to offer a downgrade, which is right, and the cost is that
    a box running a plugin this release has never been tested against looks exactly like
    a box in step. That is the one question a bug report about a missing entity starts
    with, so the download answers it in words.
    """
    await async_setup_box(hass, config_entry)
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "plugin": "9.9.9"}))
    await hass.async_block_till_done()

    plugin = (await async_get_config_entry_diagnostics(hass, config_entry))["plugin"]

    assert plugin["installed"] == "9.9.9"
    assert plugin["bundled"] == PLUGIN_VERSION
    assert plugin["compatibility"] == "newer_than_bundle"


async def test_a_bundle_that_will_not_load_is_reported_rather_than_fatal(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A damaged HACS download is itself the bug, and the report has to survive it."""
    await async_setup_box(hass, config_entry)

    with patch(
        "custom_components.enigma2_mqtt.diagnostics.load_bundled_plugin",
        side_effect=BundleError("invalid"),
    ):
        plugin = (await async_get_config_entry_diagnostics(hass, config_entry))["plugin"]

    assert plugin["bundled"] is None
    assert plugin["expected"] == PLUGIN_VERSION
    assert plugin["compatibility"] == "matched"


async def test_a_box_that_has_published_no_channel_list_summarises_to_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An empty summary is not the same as an empty list, and neither is a failure."""
    retained.update({INFO_TOPIC: json.dumps(INFO), AVAILABILITY_TOPIC: "online"})
    await async_setup_box(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["topics"]["channels"] is None

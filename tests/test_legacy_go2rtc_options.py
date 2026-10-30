"""An entry that still holds the go2rtc option of development builds loads as any other.

That option - the address of a go2rtc of the user's own, a stream name and a source - was
never released and is gone. An entry a development build saved may still hold its keys,
and one may hold a repair it raised. Nothing reads the keys, the next save of the options
drops them, and the repair is deleted at setup, because its text no longer exists.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enigma2_mqtt.const import (
    CONF_BOUQUETS,
    CONF_DANGEROUS_BUTTONS,
    CONF_WOL_MAC,
    DOMAIN,
)
from custom_components.enigma2_mqtt.diagnostics import async_get_config_entry_diagnostics

from .conftest import INFO, INFO_TOPIC, async_setup_box

PLAYER = "media_player.dekoder_salon"
LEGACY = {
    "go2rtc": None,
    "go2rtc_url": "http://user:secret@go2rtc.example:1984",
    "go2rtc_stream": "enigma2_box",
    "go2rtc_source": "ffmpeg:{url}#video=h264",
}


def _with_legacy(entry: MockConfigEntry) -> MockConfigEntry:
    return MockConfigEntry(
        domain=entry.domain,
        title=entry.title,
        unique_id=entry.unique_id,
        data=dict(entry.data),
        options={CONF_BOUQUETS: [], **LEGACY},
    )


def _stream_capable(store: dict[str, Any]) -> None:
    store[INFO_TOPIC] = json.dumps({**INFO, "capabilities": [*INFO["capabilities"], "stream"]})


async def test_an_entry_with_the_old_keys_loads_and_reads_none_of_them(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    _stream_capable(box_on_the_broker)
    entry = _with_legacy(config_entry)
    entry.add_to_hass(hass)
    issues = ir.async_get(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"go2rtc_{entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="go2rtc_unreachable",
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        "go2rtc_another_entry",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="go2rtc_unreachable",
    )

    await async_setup_box(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get(PLAYER)
    assert state is not None
    assert "go2rtc_stream" not in state.attributes
    assert not hasattr(entry.runtime_data, "go2rtc")
    # This receiver's own repair is gone; another entry's is not this setup's business.
    assert issues.async_get_issue(DOMAIN, f"go2rtc_{entry.entry_id}") is None
    assert issues.async_get_issue(DOMAIN, "go2rtc_another_entry") is not None
    # The keys stay stored until the options are saved, and are redacted meanwhile.
    assert entry.options["go2rtc_url"] == LEGACY["go2rtc_url"]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["options"]["go2rtc_url"] == REDACTED
    assert "go2rtc" not in diagnostics["box"]
    assert "secret" not in json.dumps(diagnostics)


async def test_the_options_form_has_no_go2rtc_and_a_save_drops_the_old_keys(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    _stream_capable(box_on_the_broker)
    entry = _with_legacy(config_entry)
    await async_setup_box(hass, entry)
    entry.runtime_data.async_command = AsyncMock()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "settings"
    assert not [
        key for key in result["data_schema"].schema if "go2rtc" in str(key)
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DANGEROUS_BUTTONS: False, CONF_WOL_MAC: "", CONF_BOUQUETS: []},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not [key for key in entry.options if key.startswith("go2rtc")]


async def test_removing_the_entry_deletes_a_leftover_go2rtc_repair(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    entry = _with_legacy(config_entry)
    await async_setup_box(hass, entry)
    # Raised after setup, as a repair restored from an older store would stand.
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"go2rtc_{entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="go2rtc_unreachable",
    )

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"go2rtc_{entry.entry_id}") is None

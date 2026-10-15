"""What the receiver menu's tests share: a receiver that claims `menu`, and a menu to show."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from custom_components.enigma2_mqtt.const import (
    CONF_BASE_TOPIC,
    CONF_MENU,
    CONF_NAME,
    CONF_NODE_ID,
    DOMAIN,
)

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    BASE_TOPIC,
    BOX_NAME,
    CAPABILITIES,
    INFO,
    INFO_TOPIC,
    NODE_ID,
)

MENU_TOPIC = f"enigma2mqtt/menu/{NODE_ID}"
MENU_ACTION_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/menu_action"
MENU_CAPABILITIES = [*CAPABILITIES, "menu"]
MENU_INFO = {**INFO, "capabilities": MENU_CAPABILITIES}


def row(row_id: str, entity_id: str, control: str, **extra: Any) -> dict[str, Any]:
    """A stored row, as the options hold it."""
    return {"type": "row", "id": row_id, "entity_id": entity_id, "control": control, **extra}


def group(group_id: str, label: str, rows: list[dict[str, Any]], icon: str = "sofa") -> dict:
    """A stored group, as the options hold it."""
    return {"type": "group", "id": group_id, "label": label, "icon": icon, "rows": rows}


def menu_entry(menu: list[dict[str, Any]] | None) -> MockConfigEntry:
    """The example receiver's entry, holding this menu."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=BOX_NAME,
        unique_id=NODE_ID,
        data={CONF_NODE_ID: NODE_ID, CONF_BASE_TOPIC: BASE_TOPIC, CONF_NAME: BOX_NAME},
        options={CONF_MENU: menu} if menu is not None else {},
    )


def claim_menu(retained: dict[str, str | bytes]) -> None:
    """Make the example receiver on the broker one that claims `menu`."""
    retained[INFO_TOPIC] = json.dumps(MENU_INFO)
    retained[ANNOUNCEMENT_TOPIC] = json.dumps(
        {**ANNOUNCEMENT, "capabilities": MENU_CAPABILITIES}
    )


def menu_publishes(mqtt_mock: Any) -> list[tuple[str, int, bool]]:
    """Every publish to the menu topic: payload, QoS and retain."""
    return [
        (call.args[1], call.args[2], call.args[3])
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == MENU_TOPIC
    ]


def last_menu(mqtt_mock: Any) -> dict[str, Any] | None:
    """The last menu published, decoded; None for the empty payload."""
    payload, qos, retain = menu_publishes(mqtt_mock)[-1]
    assert (qos, retain) == (1, True)
    return json.loads(payload) if payload else None


def items_by_id(menu: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every group and row of a published menu by its id."""
    found: dict[str, dict[str, Any]] = {}
    for item in menu["items"]:
        found[item["id"]] = item
        for child in item.get("rows", []):
            found[child["id"]] = child
    return found


ACTION_ID = "3f9c2e4a1b7d"


def fire_action(
    hass: HomeAssistant,
    row_id: str,
    action: str,
    value: Any = None,
    *,
    action_id: str = ACTION_ID,
    rev: int = 1,
    retain: bool = False,
    **overrides: Any,
) -> None:
    """Send one `menu_action` as the plugin does."""
    payload: dict[str, Any] = {
        "id": action_id,
        "rev": rev,
        "row": row_id,
        "action": action,
        # Now, by this Home Assistant's clock: an action older than 120 s is refused.
        "ts": int(dt_util.utcnow().timestamp()),
    }
    if action == "set":
        payload["value"] = value
    payload.update(overrides)
    async_fire_mqtt_message(hass, MENU_ACTION_TOPIC, json.dumps(payload), retain=retain)

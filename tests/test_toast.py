"""The discreet toast: its notify entity, and the `style` field on the `message` action.

A toast is the same `cmd/message` a popup is, with `style: "toast"` in it. What there is
to get wrong is small and all of it has a history:

- **The popup must not change by a byte.** Every receiver in the field understands the
  three-field payload, and an older plugin would read an unexpected `style` as nothing
  at all — so a popup carries no `style`, and its default timeout stays ten seconds.
- **The default timeout belongs to the style.** The action schema used to fill in ten
  before the handler ever saw the call, which would have given every toast without a
  timeout a popup's ten seconds.
- **The entity follows a capability and is never taken away by one.** The receiver
  names `toast` only once the screen was actually built, and it answers after the
  platforms are set up — so the entity is created late, and a capability that goes
  quiet is an older plugin or a failed rebuild, not a decision. The registry is watched,
  because an end state cannot tell „never removed" from „removed and created again".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.components.notify import (
    ATTR_MESSAGE,
    ATTR_TITLE,
    DOMAIN as NOTIFY_DOMAIN,
    SERVICE_SEND_MESSAGE,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)
import voluptuous as vol

from custom_components.enigma2_mqtt.const import DOMAIN

from .conftest import (
    AVAILABILITY_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    assert_published,
    async_arm_box_ack,
    async_setup_box,
    async_setup_box_then_retained,
    command_topic,
    published_payloads,
)

PLAYER = "media_player.dekoder_salon"
OSD = "notify.dekoder_salon_osd"
TOAST = "notify.dekoder_salon_osd_toast"
MESSAGE_TOPIC = command_topic("message")
COMPONENT = Path(__file__).parent.parent / "custom_components" / "enigma2_mqtt"


def with_toast() -> str:
    """Return an `info` payload from a receiver that has built its toast screen."""
    return json.dumps({**INFO, "capabilities": [*INFO["capabilities"], "toast"]})


def registered(hass: HomeAssistant) -> str | None:
    """Return the entity id the toast's unique id resolves to, if it is registered."""
    return er.async_get(hass).async_get_entity_id(
        "notify", DOMAIN, f"{NODE_ID}_osd_toast"
    )


def watch_registry(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Record every (action, entity id) the entity registry announces from now on."""
    seen: list[tuple[str, str]] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: seen.append((event.data["action"], event.data["entity_id"])),
    )
    return seen


async def _message(hass: HomeAssistant, **data: Any) -> None:
    """Call the `message` action on the example box."""
    await hass.services.async_call(
        DOMAIN, "message", {ATTR_ENTITY_ID: PLAYER, **data}, blocking=True
    )


async def _notify(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    """Send a notification through one of the box's notify entities."""
    await hass.services.async_call(
        NOTIFY_DOMAIN,
        SERVICE_SEND_MESSAGE,
        {ATTR_ENTITY_ID: entity_id, **data},
        blocking=True,
    )


# --------------------------------------------------------------------- the entity


async def test_no_toast_entity_without_the_capability(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver that cannot show a toast is never offered an entity for one."""
    config_entry.add_to_hass(hass)
    seen = watch_registry(hass)

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert registered(hass) is None
    assert not [entity for _, entity in seen if entity == TOAST]
    # The popup's entity is there, so the absence is the capability talking.
    assert hass.states.get(OSD) is not None


async def test_the_toast_entity_arrives_with_the_capability(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The capability lands after the platforms are set up, as it does on a real box."""
    box_on_the_broker[INFO_TOPIC] = with_toast()

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert registered(hass) == TOAST
    assert hass.states.get(TOAST) is not None


async def test_a_late_capability_creates_the_toast_entity(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver that answers long after start-up still gets its entity."""
    retained[AVAILABILITY_TOPIC] = "online"
    await async_setup_box(hass, config_entry)
    assert registered(hass) is None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_toast())
    await hass.async_block_till_done()

    assert registered(hass) == TOAST


async def test_the_toast_entity_survives_the_capability_going_quiet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 A capability that stops being named is silence, not a decision.

    A downgraded plugin, or a skin reload whose rebuild of the screen failed, looks
    exactly like this. Removing the entity would take every automation that notifies it
    by name with it — and on a Polish installation its id is the Polish name, so the
    next payload would not even bring it back under the same id.
    """
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    registry = er.async_get(hass)
    before = registry.async_get(TOAST)
    assert before is not None
    seen = watch_registry(hass)

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    await hass.async_block_till_done()

    assert "toast" not in config_entry.runtime_data.capabilities
    again = registry.async_get(TOAST)
    assert again is not None
    assert again.id == before.id
    assert ("remove", TOAST) not in seen


async def test_the_toast_entity_sends_a_toast(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Title first, like its sibling, with the toast's style and its five seconds."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    await _notify(hass, TOAST, **{ATTR_MESSAGE: "skończyła", ATTR_TITLE: "Pralka"})

    assert_published(
        mqtt_mock,
        MESSAGE_TOPIC,
        json.dumps(
            {
                "text": "Pralka: skończyła",
                "type": "info",
                "timeout": 5,
                "style": "toast",
            }
        ),
    )


async def test_a_long_toast_is_cut_where_the_plugin_cuts_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """200, not the popup's 500: what is shown is the start of what was sent."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    await _notify(hass, TOAST, **{ATTR_MESSAGE: "x" * 900})

    payload = json.loads(published_payloads(mqtt_mock, MESSAGE_TOPIC)[0])
    assert payload["text"] == "x" * 200


async def test_the_toast_entity_refuses_once_the_capability_is_gone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The entity stays, but a toast the receiver would refuse is not sent to it.

    The receiver's refusal would land on `last_error`, and a notification has nowhere to
    report it; the person gets the reason here instead.
    """
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as raised:
        await _notify(hass, TOAST, **{ATTR_MESSAGE: "Pralka"})

    assert raised.value.translation_key == "toast_unsupported"
    assert published_payloads(mqtt_mock, MESSAGE_TOPIC) == []


async def test_the_popup_entity_is_unchanged(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A receiver with a toast still gets exactly the popup it always got from „OSD"."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    await _notify(hass, OSD, **{ATTR_MESSAGE: "Pralka"})

    assert published_payloads(mqtt_mock, MESSAGE_TOPIC) == [
        '{"text": "Pralka", "type": "info", "timeout": 10}'
    ]


async def test_on_a_polish_installation_the_id_follows_the_polish_name(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 The Polish name is load-bearing: it is the entity id a Polish household gets.

    Home Assistant builds an object id from the displayed name in the installation's
    language. The sibling is `…_ekran_osd` there, and this is `…_ekran_dyskretnie`; a
    change to either Polish name renames the entity out from under every automation.
    """
    hass.config.language = "pl"
    box_on_the_broker[INFO_TOPIC] = with_toast()

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert registered(hass) == "notify.dekoder_salon_ekran_dyskretnie"
    assert (
        er.async_get(hass).async_get_entity_id("notify", DOMAIN, f"{NODE_ID}_osd")
        == "notify.dekoder_salon_ekran_osd"
    )


# --------------------------------------------------------------------- the action


async def test_a_popup_without_a_timeout_is_byte_for_byte_what_it_was(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Three fields, no `style`, ten seconds — whether or not the box can toast."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box(hass, config_entry)
    await async_arm_box_ack(hass, "message")

    await _message(hass, text="Uwaga")
    await _message(hass, text="Uwaga", style="popup")

    assert published_payloads(mqtt_mock, MESSAGE_TOPIC) == [
        '{"text": "Uwaga", "type": "info", "timeout": 10}',
        '{"text": "Uwaga", "type": "info", "timeout": 10}',
    ]


async def test_a_popup_until_dismissed_is_still_allowed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`0` means „until dismissed" to a popup, and the toast's range does not touch it."""
    await async_setup_box(hass, config_entry)
    await async_arm_box_ack(hass, "message")

    await _message(hass, text="Uwaga", timeout=0)

    assert_published(
        mqtt_mock,
        MESSAGE_TOPIC,
        json.dumps({"text": "Uwaga", "type": "info", "timeout": 0}),
    )


async def test_a_toast_without_a_timeout_gets_five_seconds(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 Five, not the popup's ten that the schema used to fill in."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box(hass, config_entry)
    await async_arm_box_ack(hass, "message")

    await _message(hass, text="Pralka", style="toast")

    assert_published(
        mqtt_mock,
        MESSAGE_TOPIC,
        json.dumps({"text": "Pralka", "type": "info", "timeout": 5, "style": "toast"}),
    )


async def test_a_toast_carries_its_type_and_timeout(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The type goes to the box, which validates it and shows the toast the same."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box(hass, config_entry)
    await async_arm_box_ack(hass, "message")

    await _message(hass, text="x" * 300, style="toast", type="warning", timeout=7)

    assert_published(
        mqtt_mock,
        MESSAGE_TOPIC,
        json.dumps(
            {"text": "x" * 200, "type": "warning", "timeout": 7, "style": "toast"}
        ),
    )


@pytest.mark.parametrize("timeout", [1, 30])
async def test_a_toast_takes_the_ends_of_its_range(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    timeout: int,
) -> None:
    """One second and thirty are both a toast."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box(hass, config_entry)
    await async_arm_box_ack(hass, "message")

    await _message(hass, text="Pralka", style="toast", timeout=timeout)

    assert json.loads(published_payloads(mqtt_mock, MESSAGE_TOPIC)[0])["timeout"] == (
        timeout
    )


@pytest.mark.parametrize("timeout", [0, 31, 300])
async def test_a_toast_outside_its_range_is_refused_before_publishing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    timeout: int,
) -> None:
    """A toast hides itself, so „until dismissed" and „for five minutes" are refused.

    The receiver would clamp 31 to 30 and refuse 0 on `last_error`; refusing here puts
    the reason in the dialog of the person who asked.
    """
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await _message(hass, text="Pralka", style="toast", timeout=timeout)

    assert raised.value.translation_key == "toast_timeout_out_of_range"
    assert raised.value.translation_placeholders == {
        "timeout": str(timeout),
        "min": "1",
        "max": "30",
    }
    assert published_payloads(mqtt_mock, MESSAGE_TOPIC) == []


async def test_a_negative_toast_timeout_never_reaches_the_box(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The schema refuses a negative timeout for either style, as it always has."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box(hass, config_entry)

    with pytest.raises(vol.Invalid):
        await _message(hass, text="Pralka", style="toast", timeout=-5)

    assert published_payloads(mqtt_mock, MESSAGE_TOPIC) == []


async def test_a_toast_to_a_box_without_one_is_refused_before_publishing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The field exists on every receiver; the receiver that cannot toast says so here."""
    await async_setup_box(hass, config_entry)

    with pytest.raises(ServiceValidationError) as raised:
        await _message(hass, text="Pralka", style="toast")

    assert raised.value.translation_key == "toast_unsupported"
    assert published_payloads(mqtt_mock, MESSAGE_TOPIC) == []


async def test_an_unknown_style_is_refused(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Only the two the plugin knows, so a typo is not silently a popup."""
    box_on_the_broker[INFO_TOPIC] = with_toast()
    await async_setup_box(hass, config_entry)

    with pytest.raises(vol.Invalid):
        await _message(hass, text="Pralka", style="banner")

    assert published_payloads(mqtt_mock, MESSAGE_TOPIC) == []


# --------------------------------------------------------------------- the names


def _toast_name(path: Path) -> str:
    """Return the toast entity's display name from one translation file."""
    strings = json.loads(path.read_text(encoding="utf-8"))
    return strings["entity"]["notify"]["osd_toast"]["name"]


def test_the_names_are_the_ones_chosen_once() -> None:
    """🔴 The Polish name becomes the entity id on a Polish installation.

    „Ekran – dyskretnie", with an en dash, exactly: after the first install it is what
    every automation calls this entity, so it is fixed here rather than left to taste.
    """
    assert _toast_name(COMPONENT / "translations" / "pl.json") == "Ekran – dyskretnie"
    assert _toast_name(COMPONENT / "strings.json") == "OSD toast"
    assert (COMPONENT / "translations" / "en.json").read_bytes() == (
        COMPONENT / "strings.json"
    ).read_bytes()

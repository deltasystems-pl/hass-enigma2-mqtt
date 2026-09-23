"""The softcam restart button, the softcam diagnostic and the auto-heal options.

Three things are being tested here and only one of them is a feature.

The **feature** is small: a button that publishes `cmd/softcam_restart`, a sensor that
shows which cam binary the image selected and how many copies of it are running, and two
settings that travel the ordinary `cmd/config` path.

The **gates** are the part that is easy to get wrong, and the tests below are shaped by
what has already gone wrong in this repository. A permission read before `info` arrives
reads „not said" on every single start-up; a capability that goes quiet is not a decision
anybody made; and an end-state assertion cannot tell „never created" from „created and
then deleted", so where that distinction matters the entity registry is watched rather
than the state machine.

The **payload** is the third, and it is not trusted. `True` is an `int` in Python and
would graph as one running instance; `0` instances is a real reading and must never be
confused with a count that could not be taken; and the plugin's own auto-heal detector
reads a file carrying a card-sharing account, a server address and the live control
words, so a test here proves that nothing outside the published contract can reach a
sensor attribute, the diagnostics download or a log line — whatever arrives.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.const import (
    CAPABILITY_SOFTCAM,
    CONF_PUBLISH_KEYS,
    CONF_SCREENSHOT,
    CONF_SCREENSHOT_INTERVAL,
    CONF_SOFTCAM_AUTOHEAL,
    CONF_SOFTCAM_AUTOHEAL_SECONDS,
    CONF_SOFTCAM_RESTART_ALLOWED,
    DOMAIN,
)
from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    AVAILABILITY_TOPIC,
    BASE_TOPIC,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    assert_published,
    async_arm_box_error,
    async_setup_box,
    async_setup_box_then_retained,
    command_topic,
)

SOFTCAM_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/softcam"
BUTTON = "button.dekoder_salon_restart_softcam"
SENSOR = "sensor.dekoder_salon_softcam"

# A collapsed receiver: one instance of the cam the image selected, restarted by hand at
# a known moment, on an image whose own liveness check adds a copy at every GUI start.
SOFTCAM: dict[str, Any] = {
    "selected": "OSCam_00000-r000",
    "running_instances": 1,
    "last_restart": 1789459213,
    "last_restart_reason": "manual",
    "restarts_today": 2,
    "manager_check_on_start": True,
    "manager_timer_minutes": None,
}

# Everything a recent plugin advertises on the options form, so that a test which
# submits the form has every field the schema declares.
PLUGIN_SETTINGS: dict[str, Any] = {
    CONF_PUBLISH_KEYS: True,
    CONF_SCREENSHOT: "on_zap",
    CONF_SCREENSHOT_INTERVAL: 60,
    CONF_SOFTCAM_AUTOHEAL: False,
    CONF_SOFTCAM_AUTOHEAL_SECONDS: 90,
}


def info(*, capabilities: list[str] | None = None, **settings: Any) -> str:
    """Return an `info` payload with the softcam capability and settings asked for."""
    payload = {**INFO}
    if capabilities is not None:
        payload["capabilities"] = capabilities
    if settings:
        payload["settings"] = settings
    return json.dumps(payload)


def with_softcam(**settings: Any) -> str:
    """Return an `info` payload from a box that can restart its softcam."""
    return info(capabilities=[*INFO["capabilities"], "softcam"], **settings)


def registered(hass: HomeAssistant, platform: str, key: str) -> str | None:
    """Return the entity id one of this box's unique ids resolves to, if any."""
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{NODE_ID}_{key}")


async def _command_sent(mqtt_mock, topic: str) -> None:
    """Wait for a press to get its command out, but not for the press to finish.

    `async_block_till_done` would wait for the press itself, which is the whole of the
    window it holds open for a complaint that is not coming.
    """
    for _ in range(200):
        await asyncio.sleep(0.005)
        if any(call.args[0] == topic for call in mqtt_mock.async_publish.call_args_list):
            for _ in range(5):
                await asyncio.sleep(0)
            return
    raise AssertionError(f"{topic} was never published")


# --------------------------------------------------------------------- the button


async def test_the_button_is_absent_until_the_box_permits_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A stated „no" creates nothing, and creates nothing even for an instant.

    A gate that read the permission at platform-setup time would find „not said",
    create the button, and delete it again a moment later when `info` landed with the
    refusal — registry churn on every start-up, and a race in which the deletion
    overtakes the addition and the entity is left registered for good. The end state
    cannot tell „never created" from „created and then deleted", so the registry is
    watched.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam(softcam_restart_allowed=False)
    config_entry.add_to_hass(hass)
    touched: list[str] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: touched.append(event.data["entity_id"]),
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(BUTTON) is None
    assert registered(hass, "button", "softcam_restart") is None
    assert BUTTON not in touched
    # And the fixture really did offer a box that can do this, so the absence above is
    # the permission talking rather than a capability that never arrived.
    assert registered(hass, "sensor", "softcam") is not None


async def test_an_older_plugin_gets_no_button_at_all(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Silence is not permission.

    Deep standby had its buttons before it had a permission, so silence there has to
    keep them. Nothing has ever shipped this one, so silence here creates nothing — and
    an older plugin would only refuse the command anyway.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam()

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert hass.states.get(BUTTON) is None
    assert registered(hass, "button", "softcam_restart") is None


async def test_the_permission_alone_is_not_enough_without_the_capability(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 The permission and the capability answer different questions.

    `softcam_restart_allowed` is a plain checkbox on every installation and says whether
    the household wants the command available. The `softcam` capability says whether the
    receiver could carry it out — the plugin claims it only where a cam binary actually
    resolves, its family has a known start line, and the image starts it through its
    manager rather than through an init script. A receiver can perfectly well answer yes
    to the first and nothing to the second: it publishes the permission, claims no
    capability, and refuses the command.

    Offering a button there would also make one receiver behave two ways, because the
    plugin's own MQTT discovery mode creates no button for that box either.
    """
    box_on_the_broker[INFO_TOPIC] = info(softcam_restart_allowed=True)
    config_entry.add_to_hass(hass)
    touched: list[str] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: touched.append(event.data["entity_id"]),
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    # The permission really did arrive, so the absence below is the capability talking.
    assert config_entry.runtime_data.softcam_restart_permission is True
    assert hass.states.get(BUTTON) is None
    assert registered(hass, "button", "softcam_restart") is None
    assert BUTTON not in touched


async def test_the_capability_arriving_late_creates_the_permitted_button(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A cam that resolves only after a restart is still a receiver that can do this.

    The gate listens rather than reading once, so the second half arriving later is
    enough — no Home Assistant restart, and no button in the meantime.
    """
    box_on_the_broker[INFO_TOPIC] = info(softcam_restart_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert hass.states.get(BUTTON) is None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_softcam(softcam_restart_allowed=True))
    await hass.async_block_till_done()

    assert hass.states.get(BUTTON) is not None


async def test_the_permission_creates_the_button_and_it_sends_the_command(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """One gate, the box's own, and the press is QoS 1 and never retained."""
    box_on_the_broker[INFO_TOPIC] = with_softcam(softcam_restart_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert hass.states.get(BUTTON) is not None

    press = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
        )
    )
    await _command_sent(mqtt_mock, command_topic("softcam_restart"))

    # The press is still open: a button that published and returned is how a refusal
    # used to arrive with nothing left listening for it.
    assert not press.done()
    assert_published(mqtt_mock, command_topic("softcam_restart"), "PRESS")
    await press


async def test_a_refused_restart_raises_the_boxs_own_words(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Every guard on this command refuses with a sentence, and it has to surface.

    The rate limit, a running recording, a recording due, an image that will not say
    whether it is recording, a restart already in flight and the first minute after the
    plugin started are six different refusals, and the only thing Home Assistant can do
    with any of them is repeat what the receiver said.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam(softcam_restart_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await async_arm_box_error(
        hass, "softcam_restart", "a recording starts in under ten minutes"
    )

    with pytest.raises(HomeAssistantError, match="a recording starts in under ten"):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True
        )


async def test_permission_at_the_television_brings_the_button_back(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Turning it on at the box must not need a Home Assistant restart."""
    box_on_the_broker[INFO_TOPIC] = with_softcam(softcam_restart_allowed=False)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert hass.states.get(BUTTON) is None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_softcam(softcam_restart_allowed=True))
    await hass.async_block_till_done()

    assert hass.states.get(BUTTON) is not None


async def test_withdrawing_the_permission_takes_the_button_away(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A stated „no" is a decision somebody made, and it removes the registration.

    Not merely the state: an unavailable button left on the device page looks like a
    fault rather than like a control that was switched off at the television.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam(softcam_restart_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert registered(hass, "button", "softcam_restart") is not None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_softcam(softcam_restart_allowed=False))
    await hass.async_block_till_done()

    assert hass.states.get(BUTTON) is None
    assert registered(hass, "button", "softcam_restart") is None


async def test_a_start_up_the_box_slept_through_keeps_the_button(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """„Not yet known" is not a stated „no", and a box in standby says nothing.

    The button already exists, the household has put it somewhere, and Home Assistant
    restarts while the receiver is off the network. Nothing about that is an answer.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{NODE_ID}_softcam_restart",
        config_entry=config_entry,
        suggested_object_id="dekoder_salon_restart_softcam",
    )

    await async_setup_box_then_retained(hass, config_entry, retained)

    assert registry.async_get(existing.entity_id) is not None


async def test_an_info_that_says_nothing_about_the_permission_removes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A downgrade, or a plugin that answered before reading its own configuration.

    The registration carries the household's rename, its area and its history; an
    `info` that simply does not mention the permission is not a reason to take any of
    that away.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam(softcam_restart_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    before = er.async_get(hass).async_get(BUTTON)
    assert before is not None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_softcam())
    await hass.async_block_till_done()

    again = er.async_get(hass).async_get(BUTTON)
    assert again is not None
    assert again.id == before.id


async def test_the_button_survives_the_capability_going_quiet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 The capability gates creating this button and must never gate removing it.

    This is ADR-0005 §2, and it is the one decision in this file that looks like an
    inconsistency and is not. The two predicates are deliberately asymmetrical: a
    capability belongs in the half that asks „can this receiver do it", and nowhere near
    the half that asks „did somebody decide against it". Written symmetrically — which
    is exactly what a later tidy-up or a reviewer „fixing" it would do — a receiver
    downgraded to an older plugin, or one whose hook failed to attach on a single boot,
    loses the button *from the registry*, and with it the household's rename, its area,
    its place on a dashboard and its history. The next payload brings it back as a
    stranger under a new id, so nothing in the end state looks wrong afterwards.

    Which is why the registry is watched rather than the state machine. An end-state
    assertion cannot tell „never removed" from „removed and created again", and the
    entity id is what every automation and every recorded row knows this button by.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam(softcam_restart_allowed=True)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    registry = er.async_get(hass)
    before = registry.async_get(BUTTON)
    assert before is not None

    removals: list[str] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: (
            removals.append(event.data["entity_id"])
            if event.data.get("action") == "remove"
            else None
        ),
    )

    # The permission is unchanged and still granted; only the capability stops being
    # named — a downgraded plugin, or a hook that did not attach on this boot.
    async_fire_mqtt_message(hass, INFO_TOPIC, info(softcam_restart_allowed=True))
    await hass.async_block_till_done()

    assert config_entry.runtime_data.softcam_restart_permission is True
    assert CAPABILITY_SOFTCAM not in config_entry.runtime_data.capabilities
    again = registry.async_get(BUTTON)
    assert again is not None
    assert again.id == before.id
    assert BUTTON not in removals


# --------------------------------------------------------------------- the sensor


async def test_the_sensor_needs_the_capability(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A box whose cam cannot be restarted has nothing to report about one."""
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert registered(hass, "sensor", "softcam") is None


async def test_a_late_capability_creates_the_sensor(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The receiver answers after the platforms are set up, every time.

    `async_setup_entry` subscribes and returns, Home Assistant holds the SUBSCRIBE
    behind a tenth of a second of debouncing, and the retained burst arrives afterwards.
    A capability read once during platform setup therefore creates nothing at all on a
    real box.
    """
    retained[AVAILABILITY_TOPIC] = "online"
    await async_setup_box(hass, config_entry)
    assert registered(hass, "sensor", "softcam") is None

    async_fire_mqtt_message(hass, INFO_TOPIC, with_softcam())
    await hass.async_block_till_done()

    assert registered(hass, "sensor", "softcam") is not None


async def test_the_sensor_survives_the_capability_going_quiet(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 A capability that stops being named is silence, not a decision.

    An older plugin after a downgrade, a hook that failed to attach on this boot, or a
    receiver that has not answered yet all look identical from here. Deleting the sensor
    would take the rename, the area, the dashboard card and the whole history of a
    diagnostic with it, and the next payload would bring it back as a stranger.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam()
    box_on_the_broker[SOFTCAM_TOPIC] = json.dumps(SOFTCAM)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    before = er.async_get(hass).async_get(SENSOR)
    assert before is not None

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps(INFO))
    await hass.async_block_till_done()

    again = er.async_get(hass).async_get(SENSOR)
    assert again is not None
    assert again.id == before.id


async def test_the_sensor_shows_the_binary_and_everything_around_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The state is the selected binary; the rest of the contract is the attributes."""
    box_on_the_broker[INFO_TOPIC] = with_softcam()
    box_on_the_broker[SOFTCAM_TOPIC] = json.dumps(
        {**SOFTCAM, "manager_timer_minutes": 6}
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == "OSCam_00000-r000"
    assert state.attributes["running_instances"] == 1
    # Epoch seconds on the topic, an ISO 8601 string in an attribute: that is this
    # file's convention, and it is the one a template can read.
    assert state.attributes["last_restart"] == dt_util.utc_from_timestamp(
        1789459213
    ).isoformat()
    assert state.attributes["last_restart_reason"] == "manual"
    assert state.attributes["restarts_today"] == 2
    assert state.attributes["manager_check_on_start"] is True
    assert state.attributes["manager_timer_minutes"] == 6


# ------------------------------------------------------------------- the payload


async def _publish_softcam(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    box_on_the_broker: dict[str, str | bytes],
    payload: Any,
) -> None:
    """Set the box up with the capability, then deliver one softcam payload."""
    box_on_the_broker[INFO_TOPIC] = with_softcam()
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    async_fire_mqtt_message(
        hass, SOFTCAM_TOPIC, payload if isinstance(payload, str) else json.dumps(payload)
    )
    await hass.async_block_till_done()


async def test_a_boolean_is_not_an_instance_count(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 `True` is an `int` in Python, and it would graph as one healthy instance.

    Which is the one wrong answer that looks exactly like the right one: a box with a
    runaway would report „true", Home Assistant would store 1, and the dashboard would
    show a collapsed receiver.
    """
    await _publish_softcam(
        hass, config_entry, box_on_the_broker, {**SOFTCAM, "running_instances": True}
    )

    assert hass.states.get(SENSOR).attributes["running_instances"] is None


async def test_no_instances_is_a_reading_and_not_an_absence(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Zero copies of the cam is a dark channel, which is the fault worth seeing."""
    await _publish_softcam(
        hass, config_entry, box_on_the_broker, {**SOFTCAM, "running_instances": 0}
    )

    assert hass.states.get(SENSOR).attributes["running_instances"] == 0


@pytest.mark.parametrize("reported", ["1", -1, 1.5, None, 10_001, [1]])
async def test_a_count_that_is_not_one_is_unknown_rather_than_zero(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """A number that could not be taken must never arrive as a number."""
    await _publish_softcam(
        hass, config_entry, box_on_the_broker, {**SOFTCAM, "running_instances": reported}
    )

    assert hass.states.get(SENSOR).attributes["running_instances"] is None


@pytest.mark.parametrize("reported", ["restarted", "", None, True, "MANUAL"])
async def test_only_the_two_reasons_in_the_contract_are_reported(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """An automation branching on the reason should see `manual`, `autoheal` or nothing."""
    await _publish_softcam(
        hass, config_entry, box_on_the_broker, {**SOFTCAM, "last_restart_reason": reported}
    )

    assert hass.states.get(SENSOR).attributes["last_restart_reason"] is None


@pytest.mark.parametrize("reported", [0, -1, True, "1789459213"])
async def test_a_restart_time_that_is_not_a_clock_is_not_shown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    reported: Any,
) -> None:
    """Zero is a field nobody filled in, not a restart in 1970."""
    await _publish_softcam(
        hass, config_entry, box_on_the_broker, {**SOFTCAM, "last_restart": reported}
    )

    assert hass.states.get(SENSOR).attributes["last_restart"] is None


async def test_a_payload_that_is_not_json_leaves_the_last_reading_alone(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """One malformed message is a bug at the other end, not news about the softcam."""
    await _publish_softcam(hass, config_entry, box_on_the_broker, SOFTCAM)
    assert hass.states.get(SENSOR).state == "OSCam_00000-r000"

    async_fire_mqtt_message(hass, SOFTCAM_TOPIC, "not json at all")
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR).state == "OSCam_00000-r000"


async def test_an_empty_payload_retracts_the_reading(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A retracted retained topic is a receiver that has stopped answering.

    Which is what `unavailable` says. Leaving the last sample on the page would be a
    reading that outlives the thing it describes, and reporting `unknown` would look
    like a fault on a device page whose whole job is „is anything wrong".
    """
    await _publish_softcam(hass, config_entry, box_on_the_broker, SOFTCAM)
    assert hass.states.get(SENSOR).state == "OSCam_00000-r000"

    async_fire_mqtt_message(hass, SOFTCAM_TOPIC, "")
    await hass.async_block_till_done()

    assert hass.states.get(SENSOR).state == STATE_UNAVAILABLE
    assert config_entry.runtime_data.state.softcam is None


# The values a real `/tmp/ecm.info` holds, which the plugin's auto-heal detector reads
# for freshness and nothing else: a card-sharing account, the sharing server's address
# and the live control words. Every one of them is invented here.
MARKERS = {
    "reader": "MARKER-READER-8f2a",
    "from": "MARKER-FROM-8f2a",
    "caid": "MARKER-CAID-8f2a",
    "prov": "MARKER-PROV-8f2a",
    "chid": "MARKER-CHID-8f2a",
    "pid": "MARKER-PID-8f2a",
    "cw0": "MARKER-CW0-8f2a",
    "cw1": "MARKER-CW1-8f2a",
}


async def test_nothing_outside_the_contract_reaches_home_assistant(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """🔴 The privacy boundary, tested from this side of the broker.

    The plugin is the side that must never publish an ECM field, and its own tests say
    so. This is the second lock: a payload that carries one anyway — a bug, a
    third-party fork, anything at all with publish rights on the topic — still cannot
    put it on a sensor attribute, into a diagnostics download meant to be attached to a
    public issue, or into a log line of ours. The normaliser builds a fresh object out of
    the seven names the contract has, so there is nowhere for an eighth to go.

    The log half is scoped to this integration's own loggers deliberately. Home
    Assistant's MQTT client writes every raw payload of every topic at debug level, ours
    included and everybody else's too; that is its business and not something this
    integration can or should undo. What is ours is that nothing we write repeats it.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.enigma2_mqtt")
    await _publish_softcam(hass, config_entry, box_on_the_broker, {**SOFTCAM, **MARKERS})

    attributes = json.dumps(dict(hass.states.get(SENSOR).attributes))
    download = json.dumps(await async_get_config_entry_diagnostics(hass, config_entry))
    error = json.dumps(hass.states.get("sensor.dekoder_salon_last_error").attributes)
    logged = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.enigma2_mqtt")
    )

    for marker in MARKERS.values():
        assert marker not in attributes
        assert marker not in download
        assert marker not in error
        assert marker not in logged


async def test_the_download_carries_the_softcam_snapshot(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A bug report about a runaway cam is unanswerable without the count in it."""
    await _publish_softcam(hass, config_entry, box_on_the_broker, SOFTCAM)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["topics"]["softcam"] == SOFTCAM


# -------------------------------------------------------------------- the options


async def _arm_config_ack(hass: HomeAssistant, settings: dict[str, Any]) -> None:
    """Answer `cmd/config` the way the plugin does: by republishing `info`."""
    from homeassistant.components import mqtt  # noqa: PLC0415

    async def _command_received(_msg) -> None:
        async_fire_mqtt_message(hass, INFO_TOPIC, with_softcam(**settings))

    await mqtt.async_subscribe(hass, command_topic("config"), _command_received)


async def test_the_form_offers_the_autoheal_pair_when_the_box_does(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Both settings only tune a command the box has already permitted.

    So they travel the ordinary `cmd/config` path, like the screenshot mode and the
    telemetry switches beside them, and the box acknowledges by republishing `info`.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam(**PLUGIN_SETTINGS)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    wanted = {**PLUGIN_SETTINGS, CONF_SOFTCAM_AUTOHEAL: True, CONF_SOFTCAM_AUTOHEAL_SECONDS: 120}
    await _arm_config_ack(hass, wanted)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert CONF_SOFTCAM_AUTOHEAL in result["data_schema"].schema
    assert CONF_SOFTCAM_AUTOHEAL_SECONDS in result["data_schema"].schema

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "dangerous_buttons": False,
            "wol_mac": "",
            "bouquets": [],
            **wanted,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    sent = [
        json.loads(call.args[1])
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == command_topic("config")
    ]
    assert sent
    assert sent[-1][CONF_SOFTCAM_AUTOHEAL] is True
    assert sent[-1][CONF_SOFTCAM_AUTOHEAL_SECONDS] == 120


async def test_the_form_never_offers_the_permission(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """🔴 A setting that *enables* a command is set at the television and nowhere else.

    The plugin refuses it on `cmd/config` by design, so a field here would be a control
    that cannot work — and worse, one that suggests the permission is Home Assistant's
    to give.
    """
    box_on_the_broker[INFO_TOPIC] = with_softcam(
        **PLUGIN_SETTINGS, softcam_restart_allowed=True
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    await _arm_config_ack(hass, PLUGIN_SETTINGS)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert CONF_SOFTCAM_RESTART_ALLOWED not in result["data_schema"].schema

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"dangerous_buttons": False, "wol_mac": "", "bouquets": [], **PLUGIN_SETTINGS},
    )
    await hass.async_block_till_done()

    for call in mqtt_mock.async_publish.call_args_list:
        if call.args[0] == command_topic("config"):
            assert CONF_SOFTCAM_RESTART_ALLOWED not in json.loads(call.args[1])
    assert CONF_SOFTCAM_RESTART_ALLOWED not in config_entry.options


async def test_an_older_plugin_is_offered_neither_field(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A plugin that has no such setting must not be sent one."""
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {
            **INFO,
            "settings": {
                CONF_PUBLISH_KEYS: True,
                CONF_SCREENSHOT: "on_zap",
                CONF_SCREENSHOT_INTERVAL: 60,
            },
        }
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert CONF_SOFTCAM_AUTOHEAL not in result["data_schema"].schema
    assert CONF_SOFTCAM_AUTOHEAL_SECONDS not in result["data_schema"].schema


# ---------------------------------------------------------------- what must not move

# Every entity id this integration built for a fully-featured receiver before 0.3.0
# began, captured on `main` at 2275ade. 🔴 An entity id is the name automations,
# dashboards, templates and the recorder's history all know an entity by, so renaming
# one is a silent breakage in somebody else's house. This asserts the set is still a
# subset of what is created — new entities are welcome, a missing one is not.
#
# These are the ids of the **English** installation the tests run as. On the operator's
# receiver they read differently, because Home Assistant builds an object id from the
# entity's name in the language the installation is set to, not from the English one.
ENTITY_IDS_BEFORE_0_3_0 = {
    "binary_sensor.dekoder_salon_cam_active",
    "binary_sensor.dekoder_salon_oscam_api_access",
    "binary_sensor.dekoder_salon_oscam_api_reachable",
    "binary_sensor.dekoder_salon_oscam_api_read_only",
    "binary_sensor.dekoder_salon_oscam_running",
    "binary_sensor.dekoder_salon_recording",
    "binary_sensor.dekoder_salon_recording_disk",
    "binary_sensor.dekoder_salon_service_encrypted",
    "button.dekoder_salon_deep_standby",
    "button.dekoder_salon_reboot",
    "button.dekoder_salon_refresh_discovery",
    "button.dekoder_salon_refresh_epg",
    "button.dekoder_salon_restart_gui",
    "button.dekoder_salon_screenshot",
    "button.dekoder_salon_wake",
    "event.dekoder_salon_key",
    "image.dekoder_salon_screen",
    "media_player.dekoder_salon",
    "notify.dekoder_salon_osd",
    "number.dekoder_salon_volume",
    "remote.dekoder_salon_remote",
    "select.dekoder_salon_bouquet",
    "select.dekoder_salon_channel",
    "sensor.dekoder_salon_active_recordings",
    "sensor.dekoder_salon_agc",
    "sensor.dekoder_salon_ber",
    "sensor.dekoder_salon_channel",
    "sensor.dekoder_salon_conditional_access_system",
    "sensor.dekoder_salon_ecm_time",
    "sensor.dekoder_salon_last_error",
    "sensor.dekoder_salon_next_program",
    "sensor.dekoder_salon_next_timer",
    "sensor.dekoder_salon_oscam",
    "sensor.dekoder_salon_oscam_configured_sources",
    "sensor.dekoder_salon_oscam_connected_servers",
    "sensor.dekoder_salon_oscam_enabled_sources",
    "sensor.dekoder_salon_oscam_healthy_sources",
    "sensor.dekoder_salon_oscam_ready_local_readers",
    "sensor.dekoder_salon_oscam_reported_shared_cards",
    "sensor.dekoder_salon_oscam_source_0000005e0001_ready_readers",
    "sensor.dekoder_salon_oscam_source_0000005e0001_status",
    "sensor.dekoder_salon_oscam_source_0000005e0002_reported_shared_cards",
    "sensor.dekoder_salon_oscam_source_0000005e0002_status",
    "sensor.dekoder_salon_oscam_uptime",
    "sensor.dekoder_salon_program",
    "sensor.dekoder_salon_snr",
    "sensor.dekoder_salon_uptime",
    "switch.dekoder_salon_mute",
    "switch.dekoder_salon_power",
    "update.dekoder_salon_plugin",
}

# The five diagnostics the process work added to 0.3.0 before this branch. They are not
# this item's entities, and that is exactly why they are named: the guard below is what
# would notice a rebase that quietly dropped somebody else's rows.
PROCESS_ENTITY_IDS = {
    "sensor.dekoder_salon_enigma2_memory",
    "sensor.dekoder_salon_enigma2_memory_peak",
    "sensor.dekoder_salon_enigma2_threads",
    "sensor.dekoder_salon_enigma2_open_files",
    "sensor.dekoder_salon_enigma2_started",
}

# The EPG sensor for the active bouquet, item j, merged after the softcam pair. Named
# for the same reason as the process rows: so that losing it is noticed here too.
EPG_ENTITY_IDS = {"sensor.dekoder_salon_epg_active_bouquet"}

# The on-demand EPG import, item i: its button and its diagnostic. English ids, because
# the suite runs as an English installation; `test_epg_import.py` pins the Polish ones.
EPG_IMPORT_ENTITY_IDS = {
    "button.dekoder_salon_import_epg",
    "sensor.dekoder_salon_epg_import",
}

EVERYTHING_ON = {
    "deep_standby_allowed": True,
    "cam_telemetry": True,
    "oscam_telemetry": True,
    "publish_keys": True,
    "screenshot": "on_zap",
    "screenshot_interval": 60,
    "screenshot_delay": 4,
    "softcam_restart_allowed": True,
    "epg_import_allowed": True,
}
OSCAM_PAYLOAD: dict[str, Any] = {
    "software": "OSCam",
    "software_running": True,
    "api_reachable": True,
    "api_access": "granted",
    "readonly": True,
    "readers": [
        {
            "id": "reader_0000005e0001",
            "kind": "reader",
            "enabled": True,
            "status": "ready",
            "protocol": "internal",
            "shared_cards": 0,
        },
        {
            "id": "server_0000005e0002",
            "kind": "server",
            "enabled": True,
            "status": "connected",
            "protocol": "cccam",
            "shared_cards": 43,
        },
    ],
}


async def test_no_entity_id_this_integration_already_had_changes(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """0.3.0 so far adds ten entities and renames none.

    Renaming a translation key renames the entity it builds, and the household's
    automations, dashboards and recorded history all follow the old name into nothing.

    The receiver here names every capability this integration knows, so the count is the
    whole fleet rather than one item's corner of it: five process diagnostics, the
    softcam pair, the EPG sensor for the active bouquet and the EPG import pair. A guard that only
    knew about its own entities would not notice a rebase that lost somebody else's.
    """
    capabilities = [
        *INFO["capabilities"],
        "cam",
        "oscam",
        "bouquet_context",
        "process",
        "softcam",
        "epg_import",
    ]
    box_on_the_broker[INFO_TOPIC] = json.dumps(
        {**INFO, "capabilities": capabilities, "settings": EVERYTHING_ON}
    )
    box_on_the_broker[f"{BASE_TOPIC}/{NODE_ID}/oscam"] = json.dumps(OSCAM_PAYLOAD)
    box_on_the_broker[f"{BASE_TOPIC}/{NODE_ID}/cam"] = json.dumps(
        {"system": "Nagra", "active": True, "encrypted": True, "ecm_ms": 120}
    )
    box_on_the_broker[SOFTCAM_TOPIC] = json.dumps(SOFTCAM)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={"dangerous_buttons": True}
    )

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    registry = er.async_get(hass)
    built = {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    assert ENTITY_IDS_BEFORE_0_3_0 <= built
    assert built - ENTITY_IDS_BEFORE_0_3_0 == {
        BUTTON,
        SENSOR,
        *PROCESS_ENTITY_IDS,
        *EPG_ENTITY_IDS,
        *EPG_IMPORT_ENTITY_IDS,
    }

"""The remote, the on-screen display, the screenshot image and the version entity."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.notify import (
    ATTR_MESSAGE,
    ATTR_TITLE,
    DOMAIN as NOTIFY_DOMAIN,
    SERVICE_SEND_MESSAGE,
)
from homeassistant.components.remote import (
    ATTR_COMMAND,
    ATTR_DELAY_SECS,
    ATTR_HOLD_SECS,
    ATTR_NUM_REPEATS,
    DOMAIN as REMOTE_DOMAIN,
    SERVICE_SEND_COMMAND,
)
from homeassistant.components.update import ATTR_VERSION, UpdateEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.box import normalise_key
from custom_components.enigma2_mqtt.bundle import BundleError
from custom_components.enigma2_mqtt.const import (
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    MESSAGE_MAX_LENGTH,
)
from custom_components.enigma2_mqtt.installer import InstallerError, InstallerErrorCode
from custom_components.enigma2_mqtt.update import latest_version

from .conftest import (
    INFO,
    INFO_TOPIC,
    PLUGIN_VERSION,
    POWER_TOPIC,
    SCREEN,
    SCREEN_TOPIC,
    assert_published,
    async_setup_box,
    command_topic,
    published_payloads,
)

REMOTE = "remote.dekoder_salon_remote"
OSD = "notify.dekoder_salon_osd"
SCREEN_ENTITY = "image.dekoder_salon_screen"
PLUGIN = "update.dekoder_salon_plugin"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("KEY_RED", "KEY_RED"),
        ("red", "KEY_RED"),
        ("Red", "KEY_RED"),
        ("key_red", "KEY_RED"),
        (" volumeup ", "KEY_VOLUMEUP"),
        ("channel up", "KEY_CHANNELUP"),
    ],
)
def test_key_names_are_accepted_in_either_spelling(given: str, expected: str) -> None:
    """The topic spells keys the way Linux does; a person does not."""
    assert normalise_key(given) == expected


async def test_the_remote_follows_standby(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The remote is on when the box is out of standby."""
    await async_setup_box(hass, config_entry)
    assert hass.states.get(REMOTE).state == STATE_ON

    async_fire_mqtt_message(hass, POWER_TOPIC, "standby")
    await hass.async_block_till_done()
    assert hass.states.get(REMOTE).state == STATE_OFF


async def test_sending_one_key(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A single tap is one command with `long` false."""
    await async_setup_box(hass, config_entry)

    await hass.services.async_call(
        REMOTE_DOMAIN,
        SERVICE_SEND_COMMAND,
        {ATTR_ENTITY_ID: REMOTE, ATTR_COMMAND: "yellow"},
        blocking=True,
    )

    assert_published(
        mqtt_mock,
        command_topic("key"),
        json.dumps({"key": "KEY_YELLOW", "long": False}),
    )


async def test_holding_a_key(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`hold_secs` is what the platform offers for a long press, so it is used."""
    await async_setup_box(hass, config_entry)

    await hass.services.async_call(
        REMOTE_DOMAIN,
        SERVICE_SEND_COMMAND,
        {ATTR_ENTITY_ID: REMOTE, ATTR_COMMAND: "KEY_OK", ATTR_HOLD_SECS: 1.5},
        blocking=True,
    )

    assert_published(
        mqtt_mock, command_topic("key"), json.dumps({"key": "KEY_OK", "long": True})
    )


async def test_sending_a_sequence_with_repeats(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Every key of every repeat goes out, in order."""
    await async_setup_box(hass, config_entry)

    await hass.services.async_call(
        REMOTE_DOMAIN,
        SERVICE_SEND_COMMAND,
        {
            ATTR_ENTITY_ID: REMOTE,
            ATTR_COMMAND: ["1", "2"],
            ATTR_NUM_REPEATS: 2,
            # Nothing should wait a real second in a test suite.
            ATTR_DELAY_SECS: 0,
        },
        blocking=True,
    )

    assert published_payloads(mqtt_mock, command_topic("key")) == [
        json.dumps({"key": "KEY_1", "long": False}),
        json.dumps({"key": "KEY_2", "long": False}),
        json.dumps({"key": "KEY_1", "long": False}),
        json.dumps({"key": "KEY_2", "long": False}),
    ]


async def test_sending_a_message_to_the_screen(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The popup has one text field, so a title becomes the first thing in it."""
    await async_setup_box(hass, config_entry)

    await hass.services.async_call(
        NOTIFY_DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            ATTR_ENTITY_ID: OSD,
            ATTR_MESSAGE: "skończyła",
            ATTR_TITLE: "Pralka",
        },
        blocking=True,
    )

    assert_published(
        mqtt_mock,
        command_topic("message"),
        json.dumps(
            {"text": "Pralka: skończyła", "type": "info", "timeout": 10},
            ensure_ascii=True,
        ),
    )


async def test_a_long_message_is_cut_where_the_plugin_cuts_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Cutting here means what is shown is the start of what was sent."""
    await async_setup_box(hass, config_entry)

    await hass.services.async_call(
        NOTIFY_DOMAIN,
        SERVICE_SEND_MESSAGE,
        {ATTR_ENTITY_ID: OSD, ATTR_MESSAGE: "x" * 900},
        blocking=True,
    )

    payload = json.loads(published_payloads(mqtt_mock, command_topic("message"))[0])
    assert len(payload["text"]) == MESSAGE_MAX_LENGTH


async def test_the_screen_image(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    hass_client,
) -> None:
    """The retained JPEG is served as an image entity, and refreshes on a new frame."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get(SCREEN_ENTITY)
    assert state.state != STATE_UNAVAILABLE
    first_taken = state.state

    client = await hass_client()
    response = await client.get(f"/api/image_proxy/{SCREEN_ENTITY}")
    assert response.status == 200
    assert await response.read() == SCREEN
    assert response.content_type == "image/jpeg"

    async_fire_mqtt_message(hass, SCREEN_TOPIC, b"\xff\xd8second\xff\xd9")
    await hass.async_block_till_done()

    assert hass.states.get(SCREEN_ENTITY).state != first_taken


async def test_a_box_with_no_screenshot_yet(
    hass: HomeAssistant,
    mqtt_mock,
    retained: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`screenshot` can be switched off on the box, and then there is no picture."""
    retained["enigma2/vuuno4kse_005301/availability"] = "online"
    await async_setup_box(hass, config_entry)

    assert hass.states.get(SCREEN_ENTITY).state == STATE_UNAVAILABLE


async def test_the_plugin_version_entity(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A box on the expected plugin version has nothing to install."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_OFF
    assert state.attributes["installed_version"] == PLUGIN_VERSION
    assert state.attributes["latest_version"] == PLUGIN_VERSION
    assert "enigma2-mqtt-bridge/releases" in state.attributes["release_url"]


async def test_an_older_plugin_has_an_update(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """That is exactly the case a topic contract mismatch shows up as."""
    await async_setup_box(hass, config_entry)

    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "plugin": "0.0.9"}))
    await hass.async_block_till_done()

    assert hass.states.get(PLUGIN).state == STATE_ON


@pytest.mark.parametrize(
    ("installed", "supported", "expected"),
    [
        ("0.1.0", "0.1.0", "0.1.0"),
        ("0.0.9", "0.1.0", "0.1.0"),
        # A box that has run ahead of this integration is not out of date.
        ("0.3.0", "0.1.0", "0.3.0"),
        ("0.1.0-r0", "0.1.0", "0.1.0-r0"),
        ("0.1.0-beta.1", "0.1.0", "0.1.0"),
        (None, "0.1.0", "0.1.0"),
        ("nightly", "0.1.0", "0.1.0"),
    ],
)
def test_the_version_offered_is_never_a_downgrade(
    installed: str | None, supported: str, expected: str
) -> None:
    """Telling somebody to install an older plugin over a newer one helps nobody."""
    assert latest_version(installed, supported) == expected


async def test_update_installs_bundle_without_reprovisioning(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An update preserves the receiver's existing broker and plugin settings."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "192.0.2.12",
            CONF_SSH_PORT: 22,
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "example-only",
            CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
        },
    )
    install = AsyncMock()
    with patch("custom_components.enigma2_mqtt.update.async_install", install):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            "update",
            "install",
            {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
            blocking=True,
        )

    request = install.await_args.args[1]
    assert request.provisioning is None
    assert request.expect_running is True
    assert request.node_id == "vuuno4kse_005301"
    assert request.base_topic == "enigma2"
    assert request.credentials.host == "192.0.2.12"


async def test_update_requires_retained_credentials(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Entries without retained SSH credentials cannot invoke installation."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["supported_features"] == 0


async def test_missing_bundle_keeps_diagnostic_entity_without_install(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A damaged distribution cannot offer install or break MQTT entity setup."""
    with patch(
        "custom_components.enigma2_mqtt.update.load_bundled_plugin",
        side_effect=BundleError("invalid"),
    ):
        await async_setup_box(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["installed_version"] == PLUGIN_VERSION
    assert state.attributes["supported_features"] == 0


async def test_auth_failure_starts_isolated_reauth(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An expired SSH password starts reauth without unloading MQTT state."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "192.0.2.12",
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "example-only",
            CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
        },
    )
    start_reauth = Mock()
    config_entry.async_start_reauth = start_reauth
    install = AsyncMock(
        side_effect=InstallerError(InstallerErrorCode.AUTH_FAILED)
    )
    with patch("custom_components.enigma2_mqtt.update.async_install", install):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        with pytest.raises(HomeAssistantError, match="auth_failed"):
            await hass.services.async_call(
                "update",
                "install",
                {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
                blocking=True,
            )

    start_reauth.assert_called_once_with(hass)


async def test_unparseable_installed_version_cannot_be_overwritten(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Unknown installed versions are displayed but never treated as older."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "192.0.2.12",
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "example-only",
            CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "plugin": "nightly"}))
    await hass.async_block_till_done()

    assert hass.states.get(PLUGIN).attributes["supported_features"] != 0
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "update", "install", {ATTR_ENTITY_ID: PLUGIN}, blocking=True
        )


async def test_an_empty_stored_password_does_not_advertise_an_install(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An empty string is a value Home Assistant will store just as happily as a password.

    The other three fields were checked for emptiness and the password only for being a
    string, so an entry with a blank password offered an install that was certain to
    fail authentication — and failing authentication starts a reauth flow at somebody
    who never asked for one.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "192.0.2.12",
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "",
            CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(PLUGIN).attributes["supported_features"] == 0


async def test_an_install_without_credentials_says_which_setting_is_missing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """„The requested version is not available" is true and useless; this is actionable."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "192.0.2.12",
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "example-only",
            CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entity = hass.data["entity_components"]["update"].get_entity(PLUGIN)
    hass.config_entries.async_update_entry(
        config_entry,
        data={key: value for key, value in config_entry.data.items() if key != CONF_SSH_PASSWORD},
    )

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_install(version=None, backup=False)

    assert raised.value.translation_key == "update_no_credentials"


async def test_an_install_reports_progress_and_always_stops_reporting_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An install takes minutes; a card that shows nothing looks like a card that failed.

    The spinner also has to stop when the install does not succeed, or the entity claims
    for ever that something is still running.
    """
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SSH_HOST: "192.0.2.12",
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "example-only",
            CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
        },
    )
    seen: list[tuple[bool, int | None]] = []

    async def reporting_install(hass_, request, progress_cb=None):
        del hass_, request
        for phase in ("preflight", "install", "done"):
            progress_cb(phase)
            state = hass.states.get(PLUGIN)
            seen.append((state.attributes["in_progress"], state.attributes["update_percentage"]))
        raise InstallerError(InstallerErrorCode.RESTART_FAILED)

    with patch("custom_components.enigma2_mqtt.update.async_install", reporting_install):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert (
            hass.states.get(PLUGIN).attributes["supported_features"]
            & UpdateEntityFeature.PROGRESS
        )
        with pytest.raises(HomeAssistantError, match="restart_failed"):
            await hass.services.async_call(
                "update",
                "install",
                {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
                blocking=True,
            )

    assert seen == [(True, 12), (True, 50), (True, 100)]
    final = hass.states.get(PLUGIN)
    assert final.attributes["in_progress"] is False
    assert final.attributes["update_percentage"] is None

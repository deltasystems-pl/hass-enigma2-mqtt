"""What the version card says, and the one request it may make to say it.

Two things meet on this entity and they are deliberately kept apart. The **bundle** is
what `install` can put on a receiver, so it alone decides `latest_version`. The
**published release** is what the plugin repository last tagged; it is asked for only
when somebody turns the option on, and it may appear in the summary, in an attribute and
in the link — never in `latest_version`, because an install button that offers a version
the installer would refuse is a button that lies.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.update import ATTR_VERSION
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.enigma2_mqtt.const import (
    CONF_CHECK_GITHUB_RELEASES,
    CONF_SSH_HOST,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    PLUGIN_LATEST_RELEASE_URL,
    RELEASE_CHECK_INTERVAL,
)
from custom_components.enigma2_mqtt.update import (
    COMPATIBILITY_MATCHED,
    COMPATIBILITY_NEWER,
    COMPATIBILITY_OLDER,
    COMPATIBILITY_UNKNOWN,
    plugin_compatibility,
)

from .conftest import INFO, INFO_TOPIC, PLUGIN_VERSION, async_setup_box

PLUGIN = "update.dekoder_salon_plugin"
RELEASE_URL = "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases/tag/v9.9.9"

CREDENTIALS = {
    CONF_SSH_HOST: "192.0.2.12",
    CONF_SSH_PORT: 22,
    CONF_SSH_USERNAME: "root",
    CONF_SSH_PASSWORD: "example-only",
    CONF_SSH_HOST_KEY: "ssh-ed25519 example-only",
}


async def _setup_with_release_check(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the box up with the release check turned on, as the options flow would."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_CHECK_GITHUB_RELEASES: True}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _publish_plugin_version(hass: HomeAssistant, version: str) -> None:
    """Make the box report a different plugin version on `info`."""
    async_fire_mqtt_message(hass, INFO_TOPIC, json.dumps({**INFO, "plugin": version}))
    await hass.async_block_till_done()


# --------------------------------------------------------------- the release check


async def test_the_release_check_is_off_and_asks_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Off by default means this integration talks to nothing but the broker."""
    await async_setup_box(hass, config_entry)

    assert aioclient_mock.call_count == 0
    state = hass.states.get(PLUGIN)
    assert state.attributes["published_version"] is None
    assert state.attributes["release_url"].endswith("/releases")


async def test_the_release_check_reports_a_tag_without_offering_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A published version may inform; it may never become `latest_version`."""
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL},
    )
    await _setup_with_release_check(hass, config_entry)

    assert aioclient_mock.call_count == 1
    state = hass.states.get(PLUGIN)
    assert state.attributes["published_version"] == "9.9.9"
    # The one thing this must never do: a card that offers what install cannot deliver.
    assert state.attributes["latest_version"] == PLUGIN_VERSION
    assert state.state == STATE_OFF
    assert state.attributes["release_url"] == RELEASE_URL
    assert "9.9.9" in state.attributes["release_summary"]


async def test_the_release_check_asks_once_a_day_and_not_more(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rate limit shared with everything else on this address is not ours to spend."""
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL},
    )
    await _setup_with_release_check(hass, config_entry)
    assert aioclient_mock.call_count == 1

    # `fire_all` is a timer that goes off early — which is the case the stamp exists
    # for, and the only way to make one happen on purpose.
    freezer.tick(RELEASE_CHECK_INTERVAL / 2)
    async_fire_time_changed(hass, fire_all=True)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1

    freezer.tick(RELEASE_CHECK_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 2


async def test_a_release_check_that_fails_says_nothing_out_loud(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A rate-limited GitHub is not a receiver fault and must not look like one."""
    aioclient_mock.get(PLUGIN_LATEST_RELEASE_URL, status=403, text="")
    await _setup_with_release_check(hass, config_entry)

    assert aioclient_mock.call_count == 1
    state = hass.states.get(PLUGIN)
    assert state.state == STATE_OFF
    assert state.attributes["published_version"] is None
    assert state.attributes["release_url"].endswith("/releases")


async def test_a_release_check_that_times_out_says_nothing_out_loud(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The check has ten seconds; a slow answer is not worth an entity going odd."""
    aioclient_mock.get(PLUGIN_LATEST_RELEASE_URL, exc=TimeoutError())
    await _setup_with_release_check(hass, config_entry)

    assert hass.states.get(PLUGIN).attributes["published_version"] is None


async def test_a_release_check_that_answers_nonsense_is_discarded(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A captive portal answers 200 with HTML, and that is not a release."""
    aioclient_mock.get(PLUGIN_LATEST_RELEASE_URL, text="<html>sign in</html>")
    await _setup_with_release_check(hass, config_entry)

    assert hass.states.get(PLUGIN).attributes["published_version"] is None


async def test_a_release_without_a_usable_tag_is_discarded(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A draft release, or a schema change, is an answer with nothing in it."""
    aioclient_mock.get(PLUGIN_LATEST_RELEASE_URL, json={"tag_name": "   ", "html_url": 7})
    await _setup_with_release_check(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["published_version"] is None
    assert state.attributes["release_url"].endswith("/releases")


async def test_a_published_release_older_than_the_bundle_is_not_mentioned(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The bundle can be ahead of the last tag; saying so would read as a regression."""
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL, json={"tag_name": "v0.0.1", "html_url": RELEASE_URL}
    )
    await _setup_with_release_check(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["published_version"] == "0.0.1"
    assert state.attributes["release_summary"] is None


# ------------------------------------------------------ what the summary has to say


async def test_an_older_plugin_without_credentials_says_how_to_install_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The card offers an update it cannot perform, so it has to say what to do."""
    await async_setup_box(hass, config_entry)
    await _publish_plugin_version(hass, "0.0.9")

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_ON
    assert state.attributes["supported_features"] == 0
    summary = state.attributes["release_summary"]
    assert "Keep SSH credentials for updates" in summary
    assert "by hand" in summary
    assert state.attributes["compatibility"] == COMPATIBILITY_OLDER


async def test_an_older_plugin_with_credentials_says_what_install_will_do(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The reassurance that matters here is that the box keeps its own settings."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, **CREDENTIALS}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await _publish_plugin_version(hass, "0.0.9")

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_ON
    summary = state.attributes["release_summary"]
    assert "broker settings" in summary
    assert "Keep SSH credentials" not in summary


async def test_a_newer_plugin_is_stated_rather_than_downgraded(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Up to date and ahead of us look identical on the card; only one is true."""
    await async_setup_box(hass, config_entry)
    await _publish_plugin_version(hass, "9.9.9")

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_OFF
    assert state.attributes["latest_version"] == "9.9.9"
    assert state.attributes["compatibility"] == COMPATIBILITY_NEWER
    assert "never offers a downgrade" in state.attributes["release_summary"]


async def test_a_matched_plugin_has_nothing_to_say(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A summary on every card is a summary nobody reads."""
    await async_setup_box(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["release_summary"] is None
    assert state.attributes["compatibility"] == COMPATIBILITY_MATCHED
    assert state.attributes["bundled_version"] == PLUGIN_VERSION


async def test_an_unreadable_version_is_not_a_verdict(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A nightly build sorts nowhere, and guessing is worse than saying nothing."""
    await async_setup_box(hass, config_entry)
    await _publish_plugin_version(hass, "nightly")

    state = hass.states.get(PLUGIN)
    assert state.attributes["compatibility"] == COMPATIBILITY_UNKNOWN
    assert state.attributes["release_summary"] is None


def test_compatibility_refuses_to_sort_what_it_cannot_parse() -> None:
    """Two version strings, three verdicts, and an honest fourth."""
    assert plugin_compatibility("0.1.0", "0.2.0") == COMPATIBILITY_OLDER
    assert plugin_compatibility("0.3.0", "0.2.0") == COMPATIBILITY_NEWER
    assert plugin_compatibility("0.2.0", "0.2.0") == COMPATIBILITY_MATCHED
    assert plugin_compatibility(None, "0.2.0") == COMPATIBILITY_UNKNOWN
    assert plugin_compatibility("nightly", "0.2.0") == COMPATIBILITY_UNKNOWN
    assert plugin_compatibility("0.2.0", None) == COMPATIBILITY_UNKNOWN


# ------------------------------------------- when the sentences themselves go missing


async def test_a_language_without_the_sentences_simply_says_less(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A summary is an explanation, not state. Missing it must cost nothing else."""
    with patch(
        "custom_components.enigma2_mqtt.update.async_get_translations",
        AsyncMock(return_value={}),
    ):
        await async_setup_box(hass, config_entry)
        await _publish_plugin_version(hass, "0.0.9")

    state = hass.states.get(PLUGIN)
    assert state.state == STATE_ON
    assert state.attributes["release_summary"] is None
    assert state.attributes["compatibility"] == COMPATIBILITY_OLDER


async def test_a_sentence_with_a_placeholder_nobody_fills_is_dropped(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A translation that invents a placeholder renders braces at a household.

    Nothing in this repository can stop a downstream translation from doing it, so the
    sentence is dropped instead of shown. Everything else on the card is unaffected.
    """
    with patch(
        "custom_components.enigma2_mqtt.update.async_get_translations",
        AsyncMock(
            return_value={
                "component.enigma2_mqtt.common.update_summary_older_no_install": (
                    "Zainstaluj {wersja}"
                )
            }
        ),
    ):
        await async_setup_box(hass, config_entry)
        await _publish_plugin_version(hass, "0.0.9")

    assert hass.states.get(PLUGIN).attributes["release_summary"] is None


# ---------------------------------------------------- what install refuses, and why


async def _setup_with_credentials(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, **CREDENTIALS})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_install_refuses_to_replace_a_newer_plugin(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The card will not offer it, and the action will not do it when asked directly."""
    await _setup_with_credentials(hass, config_entry)
    await _publish_plugin_version(hass, "9.9.9")

    with pytest.raises(HomeAssistantError, match="newer plugin"):
        await hass.services.async_call(
            "update",
            "install",
            {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
            blocking=True,
        )


async def test_install_refuses_a_version_the_bundle_does_not_hold(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """There is no download path, so a version that is not bundled cannot be installed."""
    await _setup_with_credentials(hass, config_entry)
    await _publish_plugin_version(hass, "0.0.9")

    with pytest.raises(HomeAssistantError, match="verified bundle"):
        await hass.services.async_call(
            "update",
            "install",
            {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: "0.0.1"},
            blocking=True,
        )


async def test_a_phase_the_card_cannot_draw_moves_no_bar(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A newer installer could report a phase this release has never heard of."""
    percentages: list[int | None] = []

    async def _install(hass_, request, progress_cb=None, **kwargs):
        del hass_, request, kwargs
        progress_cb("a phase from the future")
        percentages.append(hass.states.get(PLUGIN).attributes["update_percentage"])
        progress_cb("upload")
        percentages.append(hass.states.get(PLUGIN).attributes["update_percentage"])

    await _setup_with_credentials(hass, config_entry)
    await _publish_plugin_version(hass, "0.0.9")

    with patch("custom_components.enigma2_mqtt.update.async_install", _install):
        await hass.services.async_call(
            "update",
            "install",
            {ATTR_ENTITY_ID: PLUGIN, ATTR_VERSION: PLUGIN_VERSION},
            blocking=True,
        )

    assert percentages == [None, 37]

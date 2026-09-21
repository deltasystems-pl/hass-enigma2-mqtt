"""What the version card says, and the one request it may make to say it.

Two things meet on this entity and they are deliberately kept apart. The **bundle** is
what `install` can put on a receiver, so it alone decides `latest_version`. The
**published release** is what the plugin repository last tagged; it is asked for only
when somebody turns the option on, and it may appear in the summary, in an attribute and
in the link — never in `latest_version`, because an install button that offers a version
the installer would refuse is a button that lies.
"""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.update import ATTR_VERSION
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import homeassistant.util.dt as dt_util
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
    PLUGIN_RELEASES_URL,
    RELEASE_BODY_LIMIT,
    RELEASE_CHECK_INTERVAL,
    RELEASE_CHECK_STORAGE_KEY,
    RELEASE_CHECK_STORAGE_VERSION,
    RELEASE_TAG_MAX,
)
from custom_components.enigma2_mqtt.update import (
    COMPATIBILITY_MATCHED,
    COMPATIBILITY_NEWER,
    COMPATIBILITY_OLDER,
    COMPATIBILITY_UNKNOWN,
    plugin_compatibility,
    published_release_url,
    published_release_version,
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


# ------------------------------------------- where the answer is allowed to point you


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/phish",
        "javascript:alert(1)",
        # The right host, the wrong repository — a release page somebody else owns.
        "https://github.com/someone-else/enigma2-mqtt-bridge/releases/tag/v9.9.9",
        # The prefix as a prefix of the *host*, which is the classic way past a naive
        # "does it contain our address" check.
        "https://github.com.evil.example/deltasystems-pl/enigma2-mqtt-bridge/releases/tag/v1",
        7,
        None,
    ],
)
async def test_a_release_link_that_is_not_ours_is_not_shown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hostile: object,
) -> None:
    """The release link is one a household is invited to click from its device page.

    It arrives in a JSON document fetched over the network, and the field it arrives in
    says nothing about where it leads. The tag is still reported; only the link is
    dropped, back to the releases page this integration was built against.
    """
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL, json={"tag_name": "v9.9.9", "html_url": hostile}
    )
    await _setup_with_release_check(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["release_url"] == PLUGIN_RELEASES_URL
    assert state.attributes["published_version"] == "9.9.9"


async def test_a_legitimate_release_link_is_kept(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Without this the check above could pass by refusing every link there is."""
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL, json={"tag_name": "v9.9.9", "html_url": RELEASE_URL}
    )
    await _setup_with_release_check(hass, config_entry)

    assert hass.states.get(PLUGIN).attributes["release_url"] == RELEASE_URL


def test_the_release_link_allowlist_is_a_prefix_of_the_real_thing() -> None:
    """Stated directly, because it is the one comparison the whole guard rests on."""
    assert published_release_url(RELEASE_URL) == RELEASE_URL
    assert published_release_url("https://evil.example/phish") is None
    assert published_release_url("javascript:alert(1)") is None
    # The releases page itself, without the trailing slash, is not a release.
    assert published_release_url(PLUGIN_RELEASES_URL) is None
    assert published_release_url(None) is None


# --------------------------------------------------- how much of an answer is believed


async def test_an_answer_too_large_to_be_a_release_is_not_read(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A release document is a few kilobytes. A megabyte is a different endpoint."""
    filler = "x" * (RELEASE_BODY_LIMIT + 1024)
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL, "body": filler},
    )
    await _setup_with_release_check(hass, config_entry)

    assert hass.states.get(PLUGIN).attributes["published_version"] is None


async def test_an_answer_just_under_the_ceiling_is_still_read(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """GitHub puts the whole release note in this document; it must not be the limit."""
    payload = {"tag_name": "v9.9.9", "html_url": RELEASE_URL, "body": ""}
    payload["body"] = "x" * (RELEASE_BODY_LIMIT - len(json.dumps(payload)) - 16)
    assert len(json.dumps(payload)) <= RELEASE_BODY_LIMIT
    aioclient_mock.get(PLUGIN_LATEST_RELEASE_URL, json=payload)
    await _setup_with_release_check(hass, config_entry)

    assert hass.states.get(PLUGIN).attributes["published_version"] == "9.9.9"


@pytest.mark.parametrize(
    "tag",
    [
        # 606 characters, which is text on a device page rather than a version.
        "v" + "9" * 605,
        # Inside the length cap and still not a version.
        "nightly",
        "v",
        "latest-and-greatest",
    ],
)
async def test_a_tag_that_is_not_a_version_never_reaches_the_card(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    tag: str,
) -> None:
    """Everything downstream compares this with the bundled build.

    A string that does not sort is not a comparison, it is a label pretending to be one
    — and it would be shown in an attribute and in a sentence either way.
    """
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL, json={"tag_name": tag, "html_url": RELEASE_URL}
    )
    await _setup_with_release_check(hass, config_entry)

    state = hass.states.get(PLUGIN)
    assert state.attributes["published_version"] is None
    assert state.attributes["release_url"] == PLUGIN_RELEASES_URL


def test_the_tag_guard_takes_versions_and_refuses_essays() -> None:
    assert published_release_version("v0.2.0") == "0.2.0"
    assert published_release_version("0.2.0") == "0.2.0"
    assert published_release_version("v" + "9" * 605) is None
    assert published_release_version("x" * (RELEASE_TAG_MAX + 1)) is None
    assert published_release_version("nightly") is None
    assert published_release_version(None) is None


# ------------------------------------ one request a day, across reloads and restarts


async def test_six_reloads_are_still_one_request(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The limit used to live on the entity, and a reload builds a new entity.

    Every options save reloads the entry, so a household adjusting three settings in a
    row spent three days' budget in a minute — on a rate limit shared with everything
    else on that address.
    """
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL},
    )
    await _setup_with_release_check(hass, config_entry)
    assert aioclient_mock.call_count == 1

    for _ in range(5):
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()

    assert aioclient_mock.call_count == 1
    assert hass.states.get(PLUGIN).attributes["published_version"] == "9.9.9"


async def test_a_restart_within_the_day_asks_nothing_and_still_knows(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A restart is a fresh process, and the stamp has to outlive it.

    The card also has to keep saying what it knows: going blank until the next day's
    request would make a restart look like the check had stopped working.
    """
    hass_storage[RELEASE_CHECK_STORAGE_KEY] = {
        "version": RELEASE_CHECK_STORAGE_VERSION,
        "data": {
            config_entry.entry_id: {
                "checked": (dt_util.utcnow() - timedelta(hours=3)).isoformat(),
                "version": "9.9.9",
                "url": RELEASE_URL,
            }
        },
    }
    await _setup_with_release_check(hass, config_entry)

    assert aioclient_mock.call_count == 0
    state = hass.states.get(PLUGIN)
    assert state.attributes["published_version"] == "9.9.9"
    assert state.attributes["release_url"] == RELEASE_URL


async def test_a_stamp_older_than_a_day_is_spent_again(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Otherwise the cache would be a way of never asking again."""
    hass_storage[RELEASE_CHECK_STORAGE_KEY] = {
        "version": RELEASE_CHECK_STORAGE_VERSION,
        "data": {
            config_entry.entry_id: {
                "checked": (dt_util.utcnow() - timedelta(hours=25)).isoformat(),
                "version": "0.0.1",
                "url": RELEASE_URL,
            }
        },
    }
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL},
    )
    await _setup_with_release_check(hass, config_entry)

    assert aioclient_mock.call_count == 1
    assert hass.states.get(PLUGIN).attributes["published_version"] == "9.9.9"


async def test_a_check_that_failed_keeps_the_answer_and_spends_the_day(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """A rate limit must cost the request, not the tag the card was showing.

    Stamping only on success would mean a service that is down is asked again on every
    reload, which is the behaviour most likely to keep it rate-limited.
    """
    hass_storage[RELEASE_CHECK_STORAGE_KEY] = {
        "version": RELEASE_CHECK_STORAGE_VERSION,
        "data": {
            config_entry.entry_id: {
                "checked": (dt_util.utcnow() - timedelta(hours=25)).isoformat(),
                "version": "9.9.9",
                "url": RELEASE_URL,
            }
        },
    }
    aioclient_mock.get(PLUGIN_LATEST_RELEASE_URL, status=403, text="")
    await _setup_with_release_check(hass, config_entry)
    assert aioclient_mock.call_count == 1
    assert hass.states.get(PLUGIN).attributes["published_version"] == "9.9.9"

    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert aioclient_mock.call_count == 1
    assert hass.states.get(PLUGIN).attributes["published_version"] == "9.9.9"


async def test_removing_the_receiver_forgets_its_stamp(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """An entry id is never reused, so a record left behind is one nobody reads.

    It is also a stamp from a receiver's previous life deciding whether its next one is
    allowed to ask a question.
    """
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL},
    )
    await _setup_with_release_check(hass, config_entry)
    entry_id = config_entry.entry_id
    assert entry_id in hass_storage[RELEASE_CHECK_STORAGE_KEY]["data"]

    assert await hass.config_entries.async_remove(entry_id)
    await hass.async_block_till_done()

    assert entry_id not in hass_storage[RELEASE_CHECK_STORAGE_KEY]["data"]


# ------------------------------------------------------ a summary that fits, or does not


async def test_a_second_sentence_that_will_not_fit_is_dropped_whole(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Home Assistant cuts at 255 characters, wherever the 255th happens to fall.

    Mid-word, usually — and in a language with long compounds, mid-sentence. The first
    sentence says what to do and the second is context about a published tag, so the
    second is dropped whole rather than shown as a fragment.
    """
    first = "A" * 200
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL},
    )
    with patch(
        "custom_components.enigma2_mqtt.update.async_get_translations",
        AsyncMock(
            return_value={
                "component.enigma2_mqtt.common.update_summary_older_no_install": first,
                "component.enigma2_mqtt.common.update_summary_published": "B" * 60,
            }
        ),
    ):
        await _setup_with_release_check(hass, config_entry)
        await _publish_plugin_version(hass, "0.0.9")

    summary = hass.states.get(PLUGIN).attributes["release_summary"]
    # 200 + 1 + 60 is 261, so the second sentence goes entirely rather than partly.
    assert summary == first
    assert "B" not in summary


async def test_two_sentences_that_do_fit_are_both_kept(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """One character under the ceiling is still under it."""
    first = "A" * 194
    second = "B" * 60
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL},
    )
    with patch(
        "custom_components.enigma2_mqtt.update.async_get_translations",
        AsyncMock(
            return_value={
                "component.enigma2_mqtt.common.update_summary_older_no_install": first,
                "component.enigma2_mqtt.common.update_summary_published": second,
            }
        ),
    ):
        await _setup_with_release_check(hass, config_entry)
        await _publish_plugin_version(hass, "0.0.9")

    summary = hass.states.get(PLUGIN).attributes["release_summary"]
    # Exactly 255: the boundary itself, which is the case an off-by-one would take.
    assert summary == f"{first} {second}"
    assert len(summary) == 255


async def test_the_release_check_belongs_to_the_entry_that_started_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A request started on `hass` outlives the receiver it was started for.

    Unloading the entry — a reload, a removal, a shutdown — then leaves a ten-second
    request running against an entity that has gone, with nothing holding a handle to
    cancel it. Started on the entry, it is cancelled with everything else the receiver
    owns.
    """
    aioclient_mock.get(
        PLUGIN_LATEST_RELEASE_URL,
        json={"tag_name": "v9.9.9", "html_url": RELEASE_URL},
    )
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_CHECK_GITHUB_RELEASES: True}
    )
    with (
        patch.object(
            type(config_entry),
            "async_create_background_task",
            autospec=True,
            side_effect=ConfigEntry.async_create_background_task,
        ) as on_the_entry,
        patch.object(
            hass, "async_create_task", side_effect=hass.async_create_task
        ) as on_hass,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    def _named(calls) -> list[str]:
        """Return the task names of a set of calls, however `name` was passed."""
        names = []
        for call in calls:
            if "name" in call.kwargs:
                names.append(str(call.kwargs["name"]))
            elif len(call.args) >= 4:
                names.append(str(call.args[3]))
        return names

    assert aioclient_mock.call_count == 1
    assert [name for name in _named(on_the_entry.call_args_list) if "release check" in name]
    # And not on hass, where nothing that unloads the receiver can reach it.
    assert not [name for name in _named(on_hass.call_args_list) if "release check" in name]

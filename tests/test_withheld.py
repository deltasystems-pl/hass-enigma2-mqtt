"""What the receiver withheld: `info.not_published`, the repair and the diagnostic sensor.

A plugin after 0.4.0 does not send a payload that would not fit one MQTT packet. It names
the topic in `info.not_published` instead, and without that list a receiver with a very
large channel list simply has no channels here and nothing says why. These tests hold the
integration to three things: the list is read whatever shape arrives, one repair per
receiver says what is missing for exactly as long as something is, and a diagnostic sensor
carries the count - for a receiver that reports the member, and for no other.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.enigma2_mqtt.const import DOMAIN
from custom_components.enigma2_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    ANNOUNCEMENT,
    ANNOUNCEMENT_TOPIC,
    BASE_TOPIC,
    BOX_NAME,
    INFO,
    INFO_TOPIC,
    NODE_ID,
    SLUG,
    async_setup_box,
    async_setup_box_then_retained,
)

# Spelled out rather than imported, so this module collects against a tree without the
# feature and every test can be shown red.
MEMBER = "not_published"
ISSUE_KEY = "payloads_withheld"
SENSOR = f"sensor.{SLUG}_withheld_payloads"
SENSOR_UNIQUE_ID = f"{NODE_ID}_withheld_payloads"
BOUQUETS_TOPIC = f"{BASE_TOPIC}/{NODE_ID}/bouquets"
DISCOVERY_PAYLOAD_TOPIC = f"homeassistant/device/{NODE_ID}/config"
ATTRIBUTE_MAX = 20
ISSUE_MAX = 10

COMPONENT = Path(__file__).parent.parent / "custom_components" / "enigma2_mqtt"
PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_]+)\}")

CHANNELS_ENTRY = {"topic": "channels", "bytes": 2315478, "limit": 1000000}
GRID_ENTRY = {"topic": "epg_grid/astra", "bytes": 1204611, "limit": 1000000}
LIST_ENTRY = {"topic": "channels/sport_hd_2", "bytes": 1000001, "limit": 1000000}

INDEX = {
    "generated": 1789459200,
    "bouquets": [
        {"name": "Astra 19.2E", "sref": "1:7:1:0:0:0:0:0:0:0:a", "slug": "astra", "count": 9000},
        {
            "name": "Sport (HD)",
            "sref": "1:7:1:0:0:0:0:0:0:0:b",
            "slug": "sport_hd_2",
            "count": 8000,
        },
    ],
}


def info(not_published: Any = ..., **extra: Any) -> str:
    """Return an `info` payload, with the member when one is given."""
    payload = {**INFO, **extra}
    if not_published is not ...:
        payload[MEMBER] = not_published
    return json.dumps(payload)


def reporting(store: dict[str, str | bytes], not_published: Any) -> None:
    """Make the example box one whose `info` carries the member."""
    store[INFO_TOPIC] = info(not_published)


def the_issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_KEY}_{entry.entry_id}")


def our_issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    return [issue for issue in ir.async_get(hass).issues.values() if issue.domain == DOMAIN]


async def publish(hass: HomeAssistant, topic: str, payload: str) -> None:
    """Publish as a receiver that is connected does: a broker delivers that unretained."""
    async_fire_mqtt_message(hass, topic, payload)
    await hass.async_block_till_done()


# ------------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # An older plugin: no member at all. Not the same answer as an empty list.
        ({}, None),
        # Not this contract's member.
        ({MEMBER: None}, None),
        ({MEMBER: "channels"}, None),
        ({MEMBER: {"topic": "channels"}}, None),
        ({MEMBER: 3}, None),
        ({MEMBER: True}, None),
        # Reported, and nothing withheld.
        ({MEMBER: []}, []),
        ({MEMBER: [CHANNELS_ENTRY, GRID_ENTRY]}, [CHANNELS_ENTRY, GRID_ENTRY]),
        # What is not an entry is dropped, and the entries around it are kept.
        (
            {MEMBER: ["channels", None, 7, [], CHANNELS_ENTRY, {"bytes": 5}, {"topic": ""}]},
            [CHANNELS_ENTRY],
        ),
        ({MEMBER: [{"topic": 12, "bytes": 1, "limit": 2}]}, []),
        ({MEMBER: [{"topic": "x" * 201, "bytes": 1, "limit": 2}]}, []),
        (
            {MEMBER: [{"topic": "x" * 200, "bytes": 1, "limit": 2}]},
            [{"topic": "x" * 200, "bytes": 1, "limit": 2}],
        ),
        # A size that is not a whole number is unknown, never a guess. `True` is an int.
        (
            {MEMBER: [{"topic": "channels", "bytes": True, "limit": "1000000"}]},
            [{"topic": "channels", "bytes": None, "limit": None}],
        ),
        (
            {MEMBER: [{"topic": "channels", "bytes": -1, "limit": 1.5}]},
            [{"topic": "channels", "bytes": None, "limit": None}],
        ),
        ({MEMBER: [{"topic": "channels"}]}, [{"topic": "channels", "bytes": None, "limit": None}]),
        (
            {MEMBER: [{"topic": "channels", "bytes": 0, "limit": 0}]},
            [{"topic": "channels", "bytes": 0, "limit": 0}],
        ),
        # A member a later plugin adds to an entry is ignored, not passed on.
        (
            {MEMBER: [{**CHANNELS_ENTRY, "since": 1789459200, "payload": "x" * 50}]},
            [CHANNELS_ENTRY],
        ),
        # One topic, one entry: the first.
        ({MEMBER: [CHANNELS_ENTRY, {**CHANNELS_ENTRY, "bytes": 9}]}, [CHANNELS_ENTRY]),
    ],
)
def test_the_member_is_read_whatever_arrives(payload: dict[str, Any], expected: Any) -> None:
    from custom_components.enigma2_mqtt.box import normalise_not_published  # noqa: PLC0415

    assert normalise_not_published(payload) == expected


def test_the_list_kept_is_bounded() -> None:
    """Two hundred entries at most, the first two hundred, whatever a payload carries."""
    from custom_components.enigma2_mqtt.box import normalise_not_published  # noqa: PLC0415

    entries = [
        {"topic": f"channels/bouquet_{number}", "bytes": 1000001, "limit": 1000000}
        for number in range(450)
    ]

    kept = normalise_not_published({MEMBER: entries})

    assert kept == entries[:200]


async def test_the_box_keeps_the_normalised_list_and_diagnostics_carry_it(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    reporting(box_on_the_broker, [{**GRID_ENTRY, "extra": "dropped"}, "junk"])
    await async_setup_box(hass, config_entry)

    assert config_entry.runtime_data.state.not_published == [GRID_ENTRY]
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["box"]["not_published"] == [GRID_ENTRY]

    await publish(hass, INFO_TOPIC, info())

    assert config_entry.runtime_data.state.not_published is None
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["box"]["not_published"] is None


# ------------------------------------------------------------------------- repair


@pytest.mark.parametrize("not_published", [..., [], None, "channels", [{"bytes": 3}]])
async def test_no_repair_without_something_withheld(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    not_published: Any,
) -> None:
    """An older plugin, an empty list and a member that cannot be read raise nothing."""
    box_on_the_broker[INFO_TOPIC] = info(not_published)
    # The assertion below is only worth something if this tree can raise the issue at all.
    from custom_components.enigma2_mqtt import withheld  # noqa: F401, PLC0415

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert our_issues(hass) == []


async def test_one_repair_per_receiver_says_what_and_where_to_read_more(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    reporting(box_on_the_broker, [CHANNELS_ENTRY, GRID_ENTRY, LIST_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    issues = our_issues(hass)
    assert len(issues) == 1, "one per receiver, not one per topic"
    issue = the_issue(hass, config_entry)
    assert issue is issues[0]
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_KEY
    assert issue.is_persistent is False
    assert issue.learn_more_url == (
        "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/"
        "TROUBLESHOOTING.md#entities-keep-going-unavailable-and-coming-back"
    )
    assert issue.translation_placeholders == {
        "name": BOX_NAME,
        "withheld": (
            "- the complete channel list (every bouquet in one message)\n"
            "- the programme guide of the bouquet “astra”\n"
            "- the channel list of the bouquet “sport_hd_2”"
        ),
    }


async def test_the_repair_follows_the_list_and_goes_when_it_is_empty(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    first = the_issue(hass, config_entry).translation_placeholders["withheld"]

    await publish(hass, INFO_TOPIC, info([CHANNELS_ENTRY, GRID_ENTRY]))

    second = the_issue(hass, config_entry).translation_placeholders["withheld"]
    assert first.count("\n") == 0
    assert second.count("\n") == 1
    assert second.startswith(first)
    assert len(our_issues(hass)) == 1

    await publish(hass, INFO_TOPIC, info([GRID_ENTRY]))

    assert the_issue(hass, config_entry).translation_placeholders["withheld"] == (
        "- the programme guide of the bouquet “astra”"
    )

    await publish(hass, INFO_TOPIC, info([]))

    assert our_issues(hass) == []

    await publish(hass, INFO_TOPIC, info([GRID_ENTRY]))

    assert the_issue(hass, config_entry) is not None


async def test_an_info_that_says_the_same_writes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """`info` is republished far more often than the list changes."""
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    events: list[Event] = []

    @callback
    def _record(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, _record)

    await publish(hass, INFO_TOPIC, info([CHANNELS_ENTRY], uptime=384999))
    await publish(hass, INFO_TOPIC, info([{**CHANNELS_ENTRY, "bytes": 2400000}], uptime=385000))

    assert events == []
    assert the_issue(hass, config_entry) is not None


async def test_the_repair_goes_when_the_plugin_stops_reporting_the_member(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A plugin put back to 0.4.0 withholds nothing and says nothing about it."""
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert the_issue(hass, config_entry) is not None

    await publish(hass, INFO_TOPIC, info())

    assert our_issues(hass) == []


async def test_the_repair_goes_when_the_plugin_retracts_its_info(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """An empty `info` is the plugin on its way out; the last payload is kept, not believed."""
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert the_issue(hass, config_entry) is not None

    await publish(hass, INFO_TOPIC, "")

    assert our_issues(hass) == []

    await publish(hass, INFO_TOPIC, info([CHANNELS_ENTRY]))

    assert the_issue(hass, config_entry) is not None


async def test_the_repair_goes_with_the_entry_on_unload_and_returns_on_setup(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    await async_setup_box(hass, config_entry)
    assert the_issue(hass, config_entry) is not None

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert our_issues(hass) == []

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert the_issue(hass, config_entry) is not None


async def test_the_repair_goes_when_the_entry_is_removed(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    await async_setup_box(hass, config_entry)
    assert the_issue(hass, config_entry) is not None

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert our_issues(hass) == []


async def test_a_bouquet_is_named_as_the_index_names_it_once_the_index_is_here(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The slug until then: `info` and the index arrive in no order."""
    reporting(box_on_the_broker, [GRID_ENTRY, LIST_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert "“sport_hd_2”" in the_issue(hass, config_entry).translation_placeholders[
        "withheld"
    ]

    await publish(hass, BOUQUETS_TOPIC, json.dumps(INDEX))

    assert the_issue(hass, config_entry).translation_placeholders["withheld"] == (
        "- the programme guide of the bouquet “Astra 19.2E”\n"
        "- the channel list of the bouquet “Sport (HD)”"
    )
    assert len(our_issues(hass)) == 1


async def test_a_name_cannot_break_out_of_its_line(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A topic and a bouquet's name are somebody else's text inside a Markdown list."""
    reporting(
        box_on_the_broker,
        [{"topic": "weird`topic\n- injected", "bytes": 1, "limit": 1}, LIST_ENTRY],
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    index = json.loads(json.dumps(INDEX))
    index["bouquets"][1]["name"] = "Sport\n\n# Heading " + "x" * 300
    await publish(hass, BOUQUETS_TOPIC, json.dumps(index))

    lines = the_issue(hass, config_entry).translation_placeholders["withheld"].split("\n")

    assert lines[0] == "- the topic `weird'topic - injected`"
    assert len(lines) == 2
    assert lines[1].startswith("- the channel list of the bouquet “Sport # Heading xxx")
    assert len(lines[1]) < 130


WORDS = {
    "en": {
        "channels": "the complete channel list",
        "list": "the channel list of the bouquet “Sport (HD)”",
        "grid": "the programme guide of the bouquet “Astra 19.2E”",
        "discovery": "the MQTT discovery message of the device",
        "other": "the topic `something/else`",
        "more": "and 3 more",
    },
    "pl": {
        "channels": "pełna lista kanałów",
        "list": "lista kanałów bukietu „Sport (HD)”",
        "grid": "przewodnik EPG bukietu „Astra 19.2E”",
        "discovery": "wiadomość MQTT discovery urządzenia",
        "other": "temat `something/else`",
        "more": "oraz kolejne: 3",
    },
    "de": {
        "channels": "die vollständige Senderliste",
        "list": "die Senderliste des Bouquets „Sport (HD)“",
        "grid": "der Programmführer (EPG) des Bouquets „Astra 19.2E“",
        "discovery": "die MQTT-Discovery-Nachricht des Geräts",
        "other": "das Topic `something/else`",
        "more": "und 3 weitere",
    },
}


@pytest.mark.parametrize("language", ["en", "pl", "de", "de-CH", "fr"])
async def test_the_repair_renders_in_every_language_with_every_placeholder_filled(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
    language: str,
) -> None:
    """Each kind of topic in the household's words, and no brace left for it to read.

    A regional variant reads its base language; a language this integration does not
    ship reads English, which is what Home Assistant shows it for the rest of the text.
    """
    hass.config.language = language
    shipped = language.split("-")[0] if language.split("-")[0] in WORDS else "en"
    filler = [
        {"topic": f"channels/extra_{number}", "bytes": 1000001, "limit": 1000000}
        for number in range(ISSUE_MAX - 5 + 3)
    ]
    reporting(
        box_on_the_broker,
        [
            CHANNELS_ENTRY,
            LIST_ENTRY,
            GRID_ENTRY,
            {"topic": DISCOVERY_PAYLOAD_TOPIC, "bytes": 1000050, "limit": 1000000},
            {"topic": "something/else", "bytes": 1000050, "limit": 1000000},
            *filler,
        ],
    )
    box_on_the_broker[BOUQUETS_TOPIC] = json.dumps(INDEX)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    placeholders = the_issue(hass, config_entry).translation_placeholders
    strings = json.loads(
        (COMPONENT / "translations" / f"{shipped}.json").read_text(encoding="utf-8")
    )["issues"][ISSUE_KEY]

    used: set[str] = set()
    for field in ("title", "description"):
        wanted = set(PLACEHOLDER.findall(strings[field]))
        assert wanted <= set(placeholders), f"{shipped}.json:{field} asks for {wanted}"
        used |= wanted
        rendered = PLACEHOLDER.sub(lambda match: placeholders[match.group(1)], strings[field])
        assert BOX_NAME in rendered
        if field == "description":
            lines = placeholders["withheld"].split("\n")
            assert len(lines) == ISSUE_MAX + 1
            words = WORDS[shipped]
            assert lines[0].startswith(f"- {words['channels']}")
            assert lines[1] == f"- {words['list']}"
            assert lines[2] == f"- {words['grid']}"
            assert lines[3] == f"- {words['discovery']}"
            assert lines[4] == f"- {words['other']}"
            assert lines[-1] == f"- {words['more']}"
            assert placeholders["withheld"] in rendered
            assert "`bouquets_for_select`" in rendered
            assert "`epg_grid_events`" in rendered
        # Nothing of a placeholder is left for somebody to read.
        assert "{" not in rendered.replace(placeholders["withheld"], "")
    assert used == set(placeholders), "a placeholder nobody's text uses"


def test_the_repair_has_a_title_and_a_description_in_every_language() -> None:
    for language in ("en", "pl", "de"):
        strings = json.loads(
            (COMPONENT / "translations" / f"{language}.json").read_text(encoding="utf-8")
        )["issues"][ISSUE_KEY]
        assert set(strings) == {"title", "description"}
        assert strings["title"].strip() and strings["description"].strip()
        assert "{withheld}" in strings["description"]
        assert "{name}" in strings["title"]


def test_the_learn_more_anchor_exists_in_the_plugins_document() -> None:
    """The anchor is a heading of the plugin's TROUBLESHOOTING.md, slugged as GitHub does.

    The heading is quoted here because the document lives in the other repository; when
    it is reworded there, this is the line that has to follow.
    """
    from custom_components.enigma2_mqtt.const import WITHHELD_LEARN_MORE_URL  # noqa: PLC0415

    heading = "Entities keep going unavailable and coming back"
    anchor = re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")

    assert WITHHELD_LEARN_MORE_URL.endswith(f"/docs/TROUBLESHOOTING.md#{anchor}")
    assert WITHHELD_LEARN_MORE_URL.startswith(
        "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/"
    )


# ------------------------------------------------------------------------- sensor


def _registered(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    """Return every entity of the entry as unique id -> entity id."""
    registry = er.async_get(hass)
    return {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


async def test_an_older_plugin_gets_no_sensor_and_keeps_every_entity_id(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """No member, no sensor - and the same entities under the same ids as with it."""
    touched: list[tuple[str, str]] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        callback(lambda event: touched.append((event.data["action"], event.data["entity_id"]))),
    )
    await async_setup_box_then_retained(hass, config_entry, dict(box_on_the_broker))
    older = _registered(hass, config_entry)
    assert SENSOR_UNIQUE_ID not in older
    assert hass.states.get(SENSOR) is None
    assert older, "the comparison below needs entities to compare"

    await publish(hass, INFO_TOPIC, info([]))

    newer = _registered(hass, config_entry)
    assert newer == {**older, SENSOR_UNIQUE_ID: SENSOR}
    assert [item for item in touched if item[0] == "remove"] == []
    assert touched.count(("create", SENSOR)) == 1


async def test_the_sensor_counts_and_names_what_was_withheld(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    reporting(box_on_the_broker, [CHANNELS_ENTRY, {**GRID_ENTRY, "more": "ignored"}])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state.state == "2"
    assert state.attributes["topics"] == [CHANNELS_ENTRY, GRID_ENTRY]
    assert state.attributes["not_shown"] == 0
    assert state.attributes["friendly_name"] == f"{BOX_NAME} Withheld payloads"
    registered = er.async_get(hass).async_get(SENSOR)
    assert registered.unique_id == SENSOR_UNIQUE_ID
    assert registered.translation_key == "withheld_payloads"
    assert registered.entity_category is EntityCategory.DIAGNOSTIC
    assert registered.disabled_by is None, "enabled by default, like the last error"

    await publish(hass, INFO_TOPIC, info([]))

    state = hass.states.get(SENSOR)
    assert state.state == "0"
    assert state.attributes["topics"] == []


async def test_the_sensors_attribute_is_capped_and_says_how_many_it_left_out(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    entries = [
        {"topic": f"channels/bouquet_{number:03}", "bytes": 1000001 + number, "limit": 1000000}
        for number in range(ATTRIBUTE_MAX + 7)
    ]
    reporting(box_on_the_broker, entries)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    state = hass.states.get(SENSOR)
    assert state.state == str(ATTRIBUTE_MAX + 7)
    assert state.attributes["topics"] == entries[:ATTRIBUTE_MAX]
    assert state.attributes["not_shown"] == 7

    # And at the bound itself nothing is left out.
    await publish(hass, INFO_TOPIC, info(entries[:ATTRIBUTE_MAX]))

    state = hass.states.get(SENSOR)
    assert len(state.attributes["topics"]) == ATTRIBUTE_MAX
    assert state.attributes["not_shown"] == 0


async def test_the_sensor_stays_and_says_unknown_when_the_member_goes(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A capability or a member that goes quiet is a downgrade, never a reason to delete."""
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    removed: list[str] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        callback(
            lambda event: removed.append(event.data["entity_id"])
            if event.data["action"] == "remove"
            else None
        ),
    )

    await publish(hass, INFO_TOPIC, info())

    assert removed == []
    state = hass.states.get(SENSOR)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["topics"] == []


def test_the_sensor_has_a_name_of_its_own_in_every_language() -> None:
    """ADR-0007: the name is the entity id, so it may not collide with another sensor's."""
    for language in ("en", "pl", "de"):
        sensors = json.loads(
            (COMPONENT / "translations" / f"{language}.json").read_text(encoding="utf-8")
        )["entity"]["sensor"]
        name = sensors["withheld_payloads"]["name"]
        assert name.strip()
        others = [item["name"] for key, item in sensors.items() if key != "withheld_payloads"]
        assert name not in others


async def test_the_announcement_alone_creates_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The member is `info`'s. An announcement that carried one is not the contract."""
    box_on_the_broker[ANNOUNCEMENT_TOPIC] = json.dumps({**ANNOUNCEMENT, MEMBER: [CHANNELS_ENTRY]})
    from custom_components.enigma2_mqtt import withheld  # noqa: F401, PLC0415

    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert our_issues(hass) == []
    assert hass.states.get(SENSOR) is None

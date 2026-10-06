"""What the receiver withheld: `info.not_published`, the repair and the diagnostic sensor.

A plugin after 0.4.0 does not send a payload that would not fit one MQTT packet. It names
the topic in `info.not_published` instead, and without that list a receiver with a very
large channel list simply has no channels here and nothing says why. These tests hold the
integration to three things: the list is read whatever shape arrives, one repair per
receiver says what is missing for exactly as long as something is, and a diagnostic sensor
carries the count - for a receiver that reports the member, and for no other.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import re
import tarfile
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
            "- the EPG of the bouquet “astra”\n"
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
        "- the EPG of the bouquet “astra”"
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


async def test_unloading_leaves_the_repair_and_setting_up_again_keeps_it_ignored(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Somebody pressed "Ignore". A reload is not a reason to ask them again.

    Deleting an issue deletes the record that it was ignored, so neither the unload nor
    the set-up - where the retained `info` has not arrived yet - may delete it.
    """
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    burst = dict(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    ir.async_ignore_issue(hass, DOMAIN, the_issue(hass, config_entry).issue_id, True)
    ignored = the_issue(hass, config_entry).dismissed_version
    assert ignored is not None
    touched: list[str] = []

    @callback
    def _record(event: Event) -> None:
        touched.append(event.data["action"])

    hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, _record)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert the_issue(hass, config_entry).dismissed_version == ignored

    # Set up again, in the order a broker produces: the entry first, `info` afterwards.
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert the_issue(hass, config_entry).dismissed_version == ignored
    for topic, payload in burst.items():
        async_fire_mqtt_message(hass, topic, payload, retain=True)
    await hass.async_block_till_done()

    assert the_issue(hass, config_entry).dismissed_version == ignored
    assert "remove" not in touched


async def test_an_ignored_repair_survives_a_restart_of_home_assistant(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Home Assistant loads an issue like this one inactive, with its "ignored" kept.

    That is what the registry holds after a restart, before any integration has spoken.
    The same list arriving again brings it back active and still ignored.
    """
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    burst = dict(box_on_the_broker)
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    issue = the_issue(hass, config_entry)
    ir.async_ignore_issue(hass, DOMAIN, issue.issue_id, True)
    ignored = the_issue(hass, config_entry).dismissed_version
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    registry = ir.async_get(hass)
    registry.issues[(DOMAIN, issue.issue_id)] = dataclasses.replace(
        the_issue(hass, config_entry), active=False
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert the_issue(hass, config_entry).active is False
    for topic, payload in burst.items():
        async_fire_mqtt_message(hass, topic, payload, retain=True)
    await hass.async_block_till_done()

    restored = the_issue(hass, config_entry)
    assert restored.active is True
    assert restored.dismissed_version == ignored
    assert restored.translation_placeholders == issue.translation_placeholders


async def test_a_changed_list_rewrites_an_ignored_repair_and_leaves_it_ignored(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The decision: "Ignore" holds for as long as anything is withheld.

    The diagnostic sensor is where a change shows. Once the receiver publishes
    everything the repair is deleted, and one raised later is a new one.
    """
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    ir.async_ignore_issue(hass, DOMAIN, the_issue(hass, config_entry).issue_id, True)

    await publish(hass, INFO_TOPIC, info([CHANNELS_ENTRY, GRID_ENTRY]))

    issue = the_issue(hass, config_entry)
    assert issue.dismissed_version is not None
    assert issue.translation_placeholders["withheld"].count("\n") == 1

    await publish(hass, INFO_TOPIC, info([]))
    assert the_issue(hass, config_entry) is None
    await publish(hass, INFO_TOPIC, info([GRID_ENTRY]))

    assert the_issue(hass, config_entry).dismissed_version is None


async def test_a_reload_before_the_receiver_has_spoken_deletes_nothing(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The announcement alone, or nothing at all, is no word on what is withheld."""
    reporting(box_on_the_broker, [CHANNELS_ENTRY])
    announcement = box_on_the_broker[ANNOUNCEMENT_TOPIC]
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)
    assert the_issue(hass, config_entry) is not None
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, ANNOUNCEMENT_TOPIC, announcement, retain=True)
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

    ir.async_ignore_issue(hass, DOMAIN, the_issue(hass, config_entry).issue_id, True)

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
        "- the EPG of the bouquet “Astra 19.2E”\n"
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
    assert lines[1].startswith("- the channel list of the bouquet “Sport Heading xxx")
    assert len(lines[1]) < 130


async def test_nothing_a_receiver_sends_is_ever_markdown(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """A bouquet's name, the device's name, a slug and a topic are text and stay text.

    The description is rendered as Markdown and the names come from the receiver, so a
    bouquet called like the first one below would otherwise put an image and a link that
    says "Fix it" into a Home Assistant repair.
    """
    hostile = (
        "![x](https://e.example/p.png) [Fix it](https://e.example/login) "
        "<b onclick=x>bold</b> <https://e.example> www.e.example WWW.E.EXAMPLE "
        "mailto:a@e.example **strong** __strong__ _em_ ~~gone~~ `code` \\[esc\\] "
        "&lt;i&gt; &#60;u&#62; | cell | # Heading {name} javascript:alert(1)"
    )
    index = json.loads(json.dumps(INDEX))
    index["bouquets"][0]["name"] = hostile
    index["bouquets"][1]["name"] = "](http://e.example)" + "x" * 300
    box_on_the_broker[ANNOUNCEMENT_TOPIC] = json.dumps({**ANNOUNCEMENT, "name": hostile})
    box_on_the_broker[BOUQUETS_TOPIC] = json.dumps(index)
    reporting(
        box_on_the_broker,
        [
            GRID_ENTRY,
            LIST_ENTRY,
            # No index entry: named by its slug, which is the receiver's text too.
            {"topic": "channels/[y](http:e.example)*z*", "bytes": 1, "limit": 1},
            # Not a topic this knows: named whole.
            {"topic": "x/[z](https://e.example)<i>`", "bytes": 1, "limit": 1},
        ],
    )
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    placeholders = the_issue(hass, config_entry).translation_placeholders
    lines = placeholders["withheld"].split("\n")

    assert len(lines) == 4
    for text in (placeholders["name"], *lines):
        assert "](" not in text
        assert "<" not in text and ">" not in text
        assert "[" not in text and "]" not in text
        assert "://" not in text and "http:" not in text and "https:" not in text
        assert "mailto:" not in text and "javascript:" not in text
        assert "www." not in text.lower()
        assert "@" not in text and "&" not in text and "\\" not in text
        assert "*" not in text and "~" not in text and "#" not in text and "|" not in text
        assert "{" not in text and "}" not in text
        assert " _" not in text and "_ " not in text
        assert len(text) <= 80 + 60
    # A code span is the only backtick left, and only around a topic.
    assert [line.count("`") for line in lines] == [0, 0, 0, 2]
    assert len(placeholders["name"]) <= 80
    # What a name is for survives: it can still be read.
    assert placeholders["name"].startswith(
        "!(x)(https /e.example/p.png) (Fix it)(https /e.example/login)"
    )
    assert lines[1].startswith("- the channel list of the bouquet “)(http /e.example)xxx")
    assert lines[2] == "- the channel list of the bouquet “(y)(http e.example)z”"
    # And a slug keeps the underscores that are its letters.
    assert "sport_hd_2" not in placeholders["withheld"]
    await publish(hass, BOUQUETS_TOPIC, json.dumps({"generated": 1, "bouquets": []}))
    assert "“sport_hd_2”" in the_issue(hass, config_entry).translation_placeholders["withheld"]


async def test_a_bouquet_with_no_topic_of_its_own_is_named_when_channels_is_withheld(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """Its list is on `channels` only, and the receiver's list has no topic to name.

    Told from the index and `not_published` together: an empty slug, and `channels`
    withheld. With `channels` published there is nothing missing and nothing is said.
    """
    cyrillic = "".join(map(chr, (0x420, 0x443, 0x441, 0x441, 0x43A, 0x438, 0x435)))
    index = json.loads(json.dumps(INDEX))
    index["bouquets"].insert(
        1, {"name": cyrillic, "sref": "1:7:1:0:0:0:0:0:0:0:c", "slug": "", "count": 40}
    )
    box_on_the_broker[BOUQUETS_TOPIC] = json.dumps(index)
    reporting(box_on_the_broker, [GRID_ENTRY])
    await async_setup_box_then_retained(hass, config_entry, box_on_the_broker)

    assert the_issue(hass, config_entry).translation_placeholders["withheld"] == (
        "- the EPG of the bouquet “Astra 19.2E”"
    )

    await publish(hass, INFO_TOPIC, info([CHANNELS_ENTRY, GRID_ENTRY]))

    assert the_issue(hass, config_entry).translation_placeholders["withheld"] == (
        "- the complete channel list (every bouquet in one message)\n"
        f"- the channel list of the bouquet “{cyrillic}”\n"
        "- the EPG of the bouquet “Astra 19.2E”"
    )
    assert len(our_issues(hass)) == 1


WORDS = {
    "en": {
        "channels": "the complete channel list",
        "list": "the channel list of the bouquet “Sport (HD)”",
        "grid": "the EPG of the bouquet “Astra 19.2E”",
        "discovery": "the MQTT discovery message of the device",
        "other": "the topic `something/else`",
        "more": "and 3 more",
    },
    "pl": {
        "channels": "pełna lista kanałów",
        "list": "lista kanałów bukietu „Sport (HD)”",
        "grid": "EPG bukietu „Astra 19.2E”",
        "discovery": "wiadomość wykrywania MQTT (discovery) urządzenia",
        "other": "temat `something/else`",
        "more": "i jeszcze 3",
    },
    "de": {
        "channels": "die vollständige Senderliste",
        "list": "die Senderliste des Bouquets „Sport (HD)“",
        "grid": "das EPG des Bouquets „Astra 19.2E“",
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


def _bundled_troubleshooting() -> str:
    """Return `docs/TROUBLESHOOTING.md` from the bundled plugin source archive."""
    bundled = COMPONENT / "bundled"
    metadata = json.loads((bundled / "metadata.json").read_text(encoding="utf-8"))
    prefix = f"enigma2-mqtt-bridge-{metadata['source_commit']}/"
    with tarfile.open(bundled / metadata["source_filename"], "r:gz") as tar:
        member = tar.extractfile(f"{prefix}docs/TROUBLESHOOTING.md")
        assert member is not None
        return member.read().decode("utf-8")


def _github_anchor(heading: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")


def test_the_learn_more_link_points_into_the_plugins_troubleshooting_document() -> None:
    from custom_components.enigma2_mqtt.const import WITHHELD_LEARN_MORE_URL  # noqa: PLC0415

    document, _, anchor = WITHHELD_LEARN_MORE_URL.partition("#")

    assert document == (
        "https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/TROUBLESHOOTING.md"
    )
    assert anchor and anchor == _github_anchor(anchor)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the section the link points at was written after plugin 0.4.0, which is the "
        "plugin source bundled here; this turns into a pass - and, being strict, into a "
        "failure that says to remove this marker - when the bundle moves to a plugin "
        "that has it"
    ),
)
def test_the_learn_more_anchor_is_a_heading_of_the_bundled_plugins_document() -> None:
    """The anchor is checked against the plugin's own document, not against a copy here.

    Until the bundled plugin has the section, the link is unverified by any test: it was
    checked by hand against the plugin's `main`.
    """
    from custom_components.enigma2_mqtt.const import WITHHELD_LEARN_MORE_URL  # noqa: PLC0415

    anchors = {
        _github_anchor(line[3:].strip())
        for line in _bundled_troubleshooting().splitlines()
        if line.startswith("## ")
    }

    assert WITHHELD_LEARN_MORE_URL.partition("#")[2] in anchors


def test_the_bundled_troubleshooting_document_is_read_at_all() -> None:
    """The expected failure above must be about the anchor, not about reading the file."""
    assert "## Reporting a problem" in _bundled_troubleshooting()


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


async def test_a_receiver_that_has_had_the_sensor_keeps_it_after_a_restart(
    hass: HomeAssistant,
    mqtt_mock,
    box_on_the_broker: dict[str, str | bytes],
    config_entry: MockConfigEntry,
) -> None:
    """The plugin was put back to an older one, and Home Assistant restarted since.

    The registry still has the sensor. Without an entity behind it that is an orphan
    nobody can explain; with one it says `unknown`, and says a number again the day the
    newer plugin is back.
    """
    config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        "enigma2_mqtt",
        SENSOR_UNIQUE_ID,
        config_entry=config_entry,
        suggested_object_id=f"{SLUG}_withheld_payloads",
    )
    burst = dict(box_on_the_broker)
    box_on_the_broker.clear()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    for topic, payload in burst.items():
        async_fire_mqtt_message(hass, topic, payload, retain=True)
    await hass.async_block_till_done()

    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["topics"] == []
    assert state.attributes.get("restored") is None

    await publish(hass, INFO_TOPIC, info([CHANNELS_ENTRY]))

    assert hass.states.get(SENSOR).state == "1"
    assert len(_registered(hass, config_entry)) == len(set(_registered(hass, config_entry)))


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

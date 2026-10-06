"""Saying what the receiver did not publish, where a household will see it.

A broker closes the connection on a packet over its limit, so a plugin after 0.4.0 does
not send a payload that would need one: it withholds it, retracts an older copy, and names
the topic in `info.not_published`. Without that list a receiver with a very large channel
list simply has no channels here, or a bouquet has no programme guide, and nothing says
why. This module turns the list into one repair per receiver: what is missing, in words,
and what to change on the receiver to get it back. The diagnostic sensor "Withheld
payloads" carries the same list as numbers.

One issue per config entry, never one per topic: a receiver over the limit is usually over
it for several bouquets at once, and the remedy is the same for all of them. It is not
fixable from Home Assistant - the settings that decide it are on the receiver - so it has
no flow, and it is not kept across a restart: the retained `info` says it again.

The sentences that name a topic are chosen here, in the installation's language, because
a repair's placeholders are plain strings Home Assistant does not translate - the same
reason the zap-history selects pick the label of a nameless channel themselves.
"""

from __future__ import annotations

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .box import Enigma2Box, Enigma2MqttConfigEntry
from .const import (
    DOMAIN,
    TOPIC_BOUQUETS,
    TOPIC_CHANNELS,
    TOPIC_EPG_GRID,
    TOPIC_INFO,
    WITHHELD_ISSUE_MAX,
    WITHHELD_LEARN_MORE_URL,
)

ISSUE_KEY = "payloads_withheld"

# How long a bouquet's name may be in the repair's text. A name is the user's own, typed
# on the receiver; the bound only keeps one line a line.
NAME_MAX = 80

# What each kind of topic is called. `{bouquet}` is the bouquet's name where the index
# knows it and its slug otherwise; `{topic}` is the topic as the receiver named it.
PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "channels": "the complete channel list (every bouquet in one message)",
        "bouquet_channels": "the channel list of the bouquet “{bouquet}”",
        "epg_grid": "the programme guide of the bouquet “{bouquet}”",
        "discovery": "the MQTT discovery message of the device",
        "other": "the topic `{topic}`",
        "more": "and {count} more",
    },
    "pl": {
        "channels": "pełna lista kanałów (wszystkie bukiety w jednej wiadomości)",
        "bouquet_channels": "lista kanałów bukietu „{bouquet}”",
        "epg_grid": "przewodnik EPG bukietu „{bouquet}”",
        "discovery": "wiadomość MQTT discovery urządzenia",
        "other": "temat `{topic}`",
        "more": "oraz kolejne: {count}",
    },
    "de": {
        "channels": "die vollständige Senderliste (alle Bouquets in einer Nachricht)",
        "bouquet_channels": "die Senderliste des Bouquets „{bouquet}“",
        "epg_grid": "der Programmführer (EPG) des Bouquets „{bouquet}“",
        "discovery": "die MQTT-Discovery-Nachricht des Geräts",
        "other": "das Topic `{topic}`",
        "more": "und {count} weitere",
    },
}


def issue_id(entry_id: str) -> str:
    """Return the id of one receiver's repair."""
    return f"{ISSUE_KEY}_{entry_id}"


def _plain(text: str) -> str:
    """Return a name or a topic as one line that cannot end the code span it sits in."""
    return " ".join(text.replace("`", "'").split())[:NAME_MAX]


def describe(box: Enigma2Box, topic: str, language: str) -> str:
    """Return what one withheld topic is, in words a household uses.

    A topic under the receiver's own tree arrives without its prefix; one outside it
    arrives in full, and the only such payload the plugin publishes is the device's
    discovery message. Anything this does not recognise - a topic a later plugin adds -
    is named by its topic, which is never wrong.
    """
    phrases = PHRASES.get(language.split("-")[0], PHRASES["en"])
    if topic == TOPIC_CHANNELS:
        return phrases["channels"]
    for prefix, key in (
        (f"{TOPIC_CHANNELS}/", "bouquet_channels"),
        (f"{TOPIC_EPG_GRID}/", "epg_grid"),
    ):
        slug = topic[len(prefix) :]
        if topic.startswith(prefix) and slug and "/" not in slug:
            # The grid's slug is the channel topic's for nearly every bouquet, and the
            # index is the only place that still names a bouquet whose topic is gone.
            name = box.bouquet_name_for_slug(slug) or slug
            return phrases[key].format(bouquet=_plain(name))
    if "/" in topic and topic.endswith("/config"):
        return phrases["discovery"]
    return phrases["other"].format(topic=_plain(topic))


def withheld_lines(box: Enigma2Box, language: str) -> str:
    """Return the withheld payloads as a list, one line each, bounded."""
    entries = box.state.not_published or []
    lines = [
        f"- {describe(box, entry['topic'], language)}"
        for entry in entries[:WITHHELD_ISSUE_MAX]
    ]
    if len(entries) > WITHHELD_ISSUE_MAX:
        phrases = PHRASES.get(language.split("-")[0], PHRASES["en"])
        lines.append(f"- {phrases['more'].format(count=len(entries) - WITHHELD_ISSUE_MAX)}")
    return "\n".join(lines)


@callback
def async_setup_withheld_issue(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry, box: Enigma2Box
) -> CALLBACK_TYPE:
    """Keep one receiver's repair in step with `info.not_published`; return the remover.

    Raised while the list has an entry, rewritten when what it says changes - the list
    itself, or a bouquet's name once the index arrives - and removed when the list is
    empty, when the plugin stops reporting it (an older plugin, or one that retracted its
    `info` on the way out), and when the entry is unloaded.

    `info` is published far more often than the list changes. Nothing is remembered here
    to tell the two apart: the issue registry writes, and tells its listeners, only when
    an issue is new or differs from the one it holds, and deleting one that is not there
    is nothing.
    """

    @callback
    def _update() -> None:
        if box.info_retracted or not box.state.not_published:
            ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
            return
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            learn_more_url=WITHHELD_LEARN_MORE_URL,
            translation_key=ISSUE_KEY,
            translation_placeholders={
                "name": _plain(str(box.announcement.get("name") or box.name)),
                "withheld": withheld_lines(box, hass.config.language or "en"),
            },
        )

    remove_listener = box.async_add_listener(_update, (TOPIC_INFO, TOPIC_BOUQUETS))
    _update()

    @callback
    def _remove() -> None:
        remove_listener()
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))

    return _remove

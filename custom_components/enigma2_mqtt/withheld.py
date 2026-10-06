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
no flow.

**"Ignore" is kept.** Home Assistant remembers an ignored issue by its id, also across a
restart, for as long as nobody deletes the issue. So it is deleted only on the receiver's
own word that nothing is withheld - an `info` with an empty list, an `info` without the
member, a retracted `info` - and when the entry is removed. Setting the entry up and
unloading it leave it alone: at setup the retained `info` has not arrived yet, and that
is no word at all. A list that changes while the issue is ignored rewrites its text and
leaves it ignored; the sensor is where a change shows.

The sentences that name a topic are chosen here, in the installation's language, because
a repair's placeholders are plain strings Home Assistant does not translate - the same
reason the zap-history selects pick the label of a nameless channel themselves. They are
therefore in the installation's language also for a user whose profile is set to another
one, and are written again only when the list or the index next changes.
"""

from __future__ import annotations

import re

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

# How long a bouquet's name, a device's name or a topic may be in the repair's text.
NAME_MAX = 80

# What each kind of topic is called. `{bouquet}` is the bouquet's name where the index
# knows it and its slug otherwise; `{topic}` is the topic as the receiver named it.
PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "channels": "the complete channel list (every bouquet in one message)",
        "bouquet_channels": "the channel list of the bouquet “{bouquet}”",
        "epg_grid": "the EPG of the bouquet “{bouquet}”",
        "discovery": "the MQTT discovery message of the device",
        "other": "the topic `{topic}`",
        "more": "and {count} more",
    },
    "pl": {
        "channels": "pełna lista kanałów (wszystkie bukiety w jednej wiadomości)",
        "bouquet_channels": "lista kanałów bukietu „{bouquet}”",
        "epg_grid": "EPG bukietu „{bouquet}”",
        "discovery": "wiadomość wykrywania MQTT (discovery) urządzenia",
        "other": "temat `{topic}`",
        "more": "i jeszcze {count}",
    },
    "de": {
        "channels": "die vollständige Senderliste (alle Bouquets in einer Nachricht)",
        "bouquet_channels": "die Senderliste des Bouquets „{bouquet}“",
        "epg_grid": "das EPG des Bouquets „{bouquet}“",
        "discovery": "die MQTT-Discovery-Nachricht des Geräts",
        "other": "das Topic `{topic}`",
        "more": "und {count} weitere",
    },
}

# Every character Markdown or HTML gives a meaning to, and what stands in for it. A
# bracket becomes a parenthesis, which without a bracket before it is only punctuation;
# what has no harmless stand-in goes.
_INERT = str.maketrans(
    {
        "[": "(",
        "]": ")",
        "<": "(",
        ">": ")",
        "{": "(",
        "}": ")",
        "\\": "/",
        "`": "'",
        "|": "/",
        "&": "+",
        "@": " at ",
        "*": "",
        "~": "",
        "#": "",
    }
)
# An underscore that is not inside a word is emphasis; inside one - a slug - it is a letter.
_LOOSE_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])")
# A colon with something straight after it is how a URL's scheme ends, and `www.` is the
# other thing a renderer turns into a link by itself.
_SCHEME = re.compile(r":(?=\S)")
_WWW = re.compile(r"www\.", re.IGNORECASE)


def issue_id(entry_id: str) -> str:
    """Return the id of one receiver's repair."""
    return f"{ISSUE_KEY}_{entry_id}"


def _plain(text: str) -> str:
    """Return somebody else's text as one short line that is only ever text.

    A bouquet's name, a device's name and a topic are typed on the receiver or arrive
    from the broker, and they land in a repair's title and in its Markdown description.
    Read as written there, a name could draw an image, offer a link that says "Fix it",
    or open a heading. So every character Markdown or HTML acts on is replaced or
    dropped, nothing is left that a renderer links on its own - a scheme, a `www.`, an
    address - the white space becomes single spaces, and the length is bounded.
    """
    text = _WWW.sub("www ", _SCHEME.sub(" ", text.translate(_INERT)))
    text = _LOOSE_UNDERSCORE.sub(" ", text).replace("//", "/")
    return " ".join(text.split())[:NAME_MAX].strip()


def _phrases(language: str) -> dict[str, str]:
    return PHRASES.get(language.split("-")[0], PHRASES["en"])


def describe(box: Enigma2Box, topic: str, language: str) -> str:
    """Return what one withheld topic is, in words a household uses.

    A topic under the receiver's own tree arrives without its prefix; one outside it
    arrives in full, and the only such payload the plugin publishes is the device's
    discovery message. Anything this does not recognise - a topic a later plugin adds -
    is named by its topic, which is never wrong.
    """
    phrases = _phrases(language)
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
    """Return the withheld payloads as a list, one line each, bounded.

    One line for each topic the receiver names. And, straight after `channels`, one for
    each bouquet that has no topic of its own: such a bouquet's channels are published
    on `channels` only, so with `channels` withheld its list is missing too, and the
    receiver's list does not say so - there is no topic to name.
    """
    phrases = _phrases(language)
    lines: list[str] = []
    for entry in box.state.not_published or []:
        lines.append(describe(box, entry["topic"], language))
        if entry["topic"] == TOPIC_CHANNELS:
            lines.extend(
                phrases["bouquet_channels"].format(bouquet=_plain(name))
                for name in box.bouquet_names_without_topic()
            )
    shown = lines[:WITHHELD_ISSUE_MAX]
    if len(lines) > WITHHELD_ISSUE_MAX:
        shown.append(phrases["more"].format(count=len(lines) - WITHHELD_ISSUE_MAX))
    return "\n".join(f"- {line}" for line in shown)


@callback
def async_delete_withheld_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Delete one receiver's repair, and with it the record that it was ignored."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id))


@callback
def async_setup_withheld_issue(
    hass: HomeAssistant, entry: Enigma2MqttConfigEntry, box: Enigma2Box
) -> CALLBACK_TYPE:
    """Keep one receiver's repair in step with `info.not_published`; return the remover.

    Raised while the list has an entry and rewritten when what it says changes - the
    list itself, or a bouquet's name once the index arrives. Deleted when the receiver
    says nothing is withheld: an empty list, an `info` without the member (an older
    plugin), or a retracted `info` (a plugin on its way out). Left exactly as it is while
    the receiver has said nothing in this run, and when the entry is unloaded - see the
    module docstring for why.

    `info` is published far more often than the list changes. Nothing is remembered here
    to tell the two apart: the issue registry writes, and tells its listeners, only when
    an issue is new or differs from the one it holds, and deleting one that is not there
    is nothing.
    """

    @callback
    def _update() -> None:
        if not box.state.info and not box.info_retracted:
            # Nothing has arrived yet. Not the same as "nothing is withheld".
            return
        if box.info_retracted or not box.state.not_published:
            async_delete_withheld_issue(hass, entry.entry_id)
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
    return remove_listener

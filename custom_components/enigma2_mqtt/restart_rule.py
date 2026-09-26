"""Keeping the household's channel across a restart of the receiver's interface.

The image saves its settings - the channel being watched among them, as
`config.tv.lastservice` - only on a clean quit. A stop by signal, which is what `init 4`
and `killall enigma2` are, never reaches that code, so a receiver stopped that way comes
back on whatever channel was saved last, which can be hours old. That was measured on a
receiver. So every restart the installer makes follows one rule, written down for both
this integration and the receiver plugin in the plugin's `docs/TRANSACTION.md`:

- R1, restart: only the plugin's files changed and the interface is healthy. OpenWebif's
  power state 3 - the image's own clean quit, which saves the settings - and at most 60
  seconds for enigma2's pid to change. When the image asks a question on the television
  instead (timeshift, a background job), the installer does not force it: it puts the
  old files back and says so.
- R2, stop: a rollback that has to put the plugin's settings block back. Record the
  channel, stop, restore, write the recorded channel into the settings, start - as one
  script on the receiver, so nothing from outside can cut it off in the middle.
- R3, verify, after every restart: compare with the record, zap back once if needed,
  put standby back, restore the channel-list bouquet where the plugin can.

This module is R3 and the record. The restarts themselves live in the installer, which
owns the SSH session and the transaction; this owns what "the channel was kept" means.

Nothing here reads the order of open screens. Some images keep an invisible screen open
from session start (a broadcaster's HbbTV plugin); what a restart did is read from
enigma2's pid and OpenWebif's state, never from what is on top.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import shlex
import time
from typing import Any, Protocol

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import HomeAssistant, callback

from .box import command_topic, parse_json_payload, same_service, state_topic
from .const import TOPIC_BOUQUET

_LOGGER = logging.getLogger(__name__)

# After the interface is back, how long OpenWebif may take to report a channel at all.
# The interface answers eleven to fourteen seconds after it starts on the receivers
# measured, and tunes its start channel shortly after that.
SETTLE_TIMEOUT = 60.0
SETTLE_POLL_SECONDS = 2.0
# How long a zap back, or a return to standby, may take to show in OpenWebif's state.
EFFECT_TIMEOUT = 10.0
EFFECT_POLL_SECONDS = 1.0
# How long the retained `bouquet` and `channels` topics are given to replay.
BOUQUET_REPLAY_SECONDS = 3.0
# How long a `cmd/bouquet` is given to be answered by a fresh `bouquet` state.
BOUQUET_ACK_SECONDS = 10.0
TOPIC_CHANNELS = "channels"
COMMAND_BOUQUET = "bouquet"


class _Session(Protocol):
    async def run(
        self, command: str, *, input: bytes | None = None, timeout: float = 30
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class RestartRecord:
    """What a restart has to keep, taken just before it.

    `service` and `standby` are OpenWebif's, and None when it did not answer - then
    there was no channel to keep, and the outcome says so rather than inventing one.
    `bouquet` is the plugin's retained channel-list bouquet, when a plugin that has one
    was running; `bouquet_holds_service` is whether the plugin's published channels put
    the recorded service in that bouquet, because `cmd/bouquet` tunes the bouquet's
    first channel when the current one is not in it, and a restore that changes the
    channel it was meant to keep is worse than none.
    """

    service: str | None = None
    standby: bool | None = None
    bouquet: str | None = None
    bouquet_holds_service: bool = False


@dataclass(slots=True)
class RestartOutcome:
    """The transaction's record of a restart, as `docs/TRANSACTION.md` names it."""

    restart: str
    channel: str = "not recorded"
    bouquet: str = "not restored"
    standby: str = "not recorded"

    def describe(self) -> str:
        """Return the record as one line for the log."""
        return (
            f"restart: {self.restart}, channel: {self.channel}, "
            f"bouquet: {self.bouquet}, standby: {self.standby}"
        )


def parse_state(stdout: str) -> tuple[str | None, bool | None]:
    """Read the helper's `record` answer; anything unreadable is "no answer"."""
    try:
        answer = json.loads(stdout)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(answer, dict):
        return None, None
    service = answer.get("service")
    standby = answer.get("standby")
    return (
        service if isinstance(service, str) and service else None,
        standby if isinstance(standby, bool) else None,
    )


async def async_read_state(
    session: _Session, helper: str
) -> tuple[str | None, bool | None]:
    """Ask the receiver what it is playing and whether it is in standby."""
    try:
        result = await session.run(f"python3 {shlex.quote(helper)} record", timeout=30)
    except (OSError, TimeoutError):
        return None, None
    if getattr(result, "exit_status", 1):
        return None, None
    return parse_state(result.stdout)


async def _async_settled_state(
    session: _Session, helper: str
) -> tuple[str | None, bool | None]:
    """Wait for the restarted interface to report a channel, and return its state.

    The first channel it reports is the one the image started on. That matters for the
    zap back, which may only override the image's own choice, never the household's.
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while True:
        service, standby = await async_read_state(session, helper)
        if service is not None or standby is True:
            return service, standby
        if time.monotonic() >= deadline:
            return service, standby
        await asyncio.sleep(SETTLE_POLL_SECONDS)


async def async_verify(
    session: _Session, helper: str, record: RestartRecord, *, restart: str
) -> RestartOutcome:
    """R3: compare the restarted receiver with the record and put back what differs.

    The channel is zapped back **at most once**, and only while the receiver still plays
    the channel the image started on: somebody who picked a channel in the seconds after
    the start has decided, and a zap now would override them. Standby is entered again
    only when the record says the receiver was in it - a rollback corner, because the
    forward paths refuse to start in standby - and with HDMI-CEC on, the image then also
    sends the television to standby, which is what it had been.
    """
    outcome = RestartOutcome(restart)
    started_on, standby_now = await _async_settled_state(session, helper)
    if record.service is not None:
        if same_service(started_on, record.service):
            outcome.channel = "kept"
        elif started_on is None:
            outcome.channel = "lost"
        else:
            current, _ = await async_read_state(session, helper)
            if not same_service(current, started_on):
                # Not the image's choice any more: the household moved on.
                outcome.channel = "lost"
            else:
                outcome.channel = (
                    "restored"
                    if await _async_zap_back(session, helper, record.service)
                    else "lost"
                )
    if record.standby is not None:
        if record.standby is not True or standby_now is True:
            outcome.standby = "kept"
        else:
            outcome.standby = (
                "restored" if await _async_enter_standby(session, helper) else "lost"
            )
    return outcome


async def _async_zap_back(session: _Session, helper: str, service: str) -> bool:
    """Zap to `service` once, and return whether the receiver then plays it."""
    try:
        await session.run(
            f"python3 {shlex.quote(helper)} zap --service {shlex.quote(service)}",
            timeout=30,
        )
    except (OSError, TimeoutError):
        return False
    deadline = time.monotonic() + EFFECT_TIMEOUT
    while True:
        current, _ = await async_read_state(session, helper)
        if same_service(current, service):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(EFFECT_POLL_SECONDS)


async def _async_enter_standby(session: _Session, helper: str) -> bool:
    try:
        await session.run(
            f"python3 {shlex.quote(helper)} powerstate --state 5", timeout=30
        )
    except (OSError, TimeoutError):
        return False
    deadline = time.monotonic() + EFFECT_TIMEOUT
    while True:
        _service, standby = await async_read_state(session, helper)
        if standby is True:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(EFFECT_POLL_SECONDS)


def _bouquet_sref(payload: Any) -> str | None:
    document = parse_json_payload(payload)
    if not isinstance(document, dict):
        return None
    sref = document.get("sref")
    return sref if isinstance(sref, str) and sref else None


def _bouquet_holds(channels: Any, bouquet: str, service: str | None) -> bool:
    """Return whether the plugin's published `channels` put `service` in `bouquet`."""
    if service is None:
        return False
    document = parse_json_payload(channels)
    if not isinstance(document, dict) or not isinstance(document.get("bouquets"), list):
        return False
    for entry in document["bouquets"]:
        if not isinstance(entry, dict) or entry.get("sref") != bouquet:
            continue
        members = entry.get("channels")
        if not isinstance(members, list):
            return False
        return any(
            isinstance(member, dict) and same_service(member.get("sref"), service)
            for member in members
        )
    return False


async def _async_retained(
    hass: HomeAssistant, topics: list[str], wait: float
) -> dict[str, Any]:
    """Return the retained payloads of `topics`, read through fresh subscriptions.

    A fresh subscription makes Home Assistant subscribe again, and the broker answers a
    subscribe with the retained message - so this sees the retained value even when
    another part of the integration is already subscribed to the same topic.
    """
    seen: dict[str, Any] = {}
    ready = asyncio.Event()
    remaining = set(topics)
    cancels: list[Any] = []

    @callback
    def _subscribed() -> None:
        ready.set()

    def _collector(topic: str):
        @callback
        def _message(message: ReceiveMessage) -> None:
            seen[topic] = message.payload

        return _message

    try:
        for topic in topics:
            cancels.append(
                mqtt.async_on_subscribe_done(hass, topic, mqtt.DEFAULT_QOS, _subscribed)
            )
            cancels.append(await mqtt.async_subscribe(hass, topic, _collector(topic)))
        async with asyncio.timeout(wait):
            await ready.wait()
        deadline = time.monotonic() + wait
        while remaining - set(seen) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
    except TimeoutError:
        pass
    finally:
        for cancel in cancels:
            cancel()
    return seen


async def async_record(
    hass: HomeAssistant,
    session: _Session,
    helper: str,
    base_topic: str,
    node_id: str,
) -> RestartRecord:
    """Take the record R3 compares against, just before a restart."""
    service, standby = await async_read_state(session, helper)
    bouquet = None
    holds = False
    try:
        retained = await _async_retained(
            hass,
            [
                state_topic(base_topic, node_id, TOPIC_BOUQUET),
                state_topic(base_topic, node_id, TOPIC_CHANNELS),
            ],
            BOUQUET_REPLAY_SECONDS,
        )
        bouquet = _bouquet_sref(retained.get(state_topic(base_topic, node_id, TOPIC_BOUQUET)))
        if bouquet is not None:
            holds = _bouquet_holds(
                retained.get(state_topic(base_topic, node_id, TOPIC_CHANNELS)),
                bouquet,
                service,
            )
    except Exception:  # noqa: BLE001 - a record without a bouquet is still a record
        _LOGGER.debug("The receiver's bouquet could not be recorded", exc_info=True)
    return RestartRecord(service, standby, bouquet, holds)


async def async_restore_bouquet(
    hass: HomeAssistant,
    base_topic: str,
    node_id: str,
    record: RestartRecord,
    channel: str,
) -> str:
    """Put the channel-list bouquet back through the plugin, where that is safe.

    `lastservice` restores the channel, not the list the channel buttons walk. The
    plugin that is running after the start can set it with `cmd/bouquet`, and says so
    with a fresh `bouquet` state - that republish is the proof. It is sent only when the
    channel is where it should be and the plugin's own channel list puts that channel in
    the recorded bouquet: `cmd/bouquet` tunes the bouquet's first channel otherwise.
    """
    if record.bouquet is None or channel not in ("kept", "restored"):
        return "not restored"
    if not record.bouquet_holds_service:
        return "not restored"
    topic = state_topic(base_topic, node_id, TOPIC_BOUQUET)
    try:
        now = _bouquet_sref(
            (await _async_retained(hass, [topic], BOUQUET_REPLAY_SECONDS)).get(topic)
        )
        if now is None:
            # No plugin with a channel-list bouquet is running, or it has not said so.
            return "not restored"
        if now == record.bouquet:
            return "kept"
        return await _async_send_bouquet(hass, base_topic, node_id, topic, record.bouquet)
    except Exception:  # noqa: BLE001 - never a reason to fail a restart that worked
        _LOGGER.debug("The receiver's bouquet could not be restored", exc_info=True)
        return "not restored"


async def _async_send_bouquet(
    hass: HomeAssistant, base_topic: str, node_id: str, topic: str, bouquet: str
) -> str:
    answered = asyncio.Event()
    ready = asyncio.Event()

    @callback
    def _message(message: ReceiveMessage) -> None:
        if not message.retain and _bouquet_sref(message.payload) == bouquet:
            answered.set()

    @callback
    def _subscribed() -> None:
        ready.set()

    cancels = [mqtt.async_on_subscribe_done(hass, topic, mqtt.DEFAULT_QOS, _subscribed)]
    try:
        cancels.append(await mqtt.async_subscribe(hass, topic, _message))
        # Subscribed at the broker before the command goes out, or a plugin that answers
        # in a fraction of a second answers a broker not yet forwarding the topic.
        async with asyncio.timeout(BOUQUET_REPLAY_SECONDS):
            await ready.wait()
        await mqtt.async_publish(
            hass,
            command_topic(base_topic, node_id, COMMAND_BOUQUET),
            json.dumps({"sref": bouquet}),
            qos=1,
            retain=False,
        )
        async with asyncio.timeout(BOUQUET_ACK_SECONDS):
            await answered.wait()
    except TimeoutError:
        return "not restored"
    finally:
        for cancel in cancels:
            cancel()
    return "restored"

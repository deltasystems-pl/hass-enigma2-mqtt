"""The preflight fails closed on an answer it does not understand.

Every measurement before the box is changed exists to decide whether to change it, so
the interesting cases are not the ones where a receiver answers correctly. They are the
ones where it answers something else: a `df` that prints a warning, an `opkg` that lists
the package twice, a `test -d` that could not run at all. None of those is a receiver
this installer knows how to reason about, and an installer that guesses at one has
already lost the argument about what it is for.
"""

from __future__ import annotations

import json

import pytest

from custom_components.enigma2_mqtt.installer import (
    CommandResult,
    InstallerError,
    InstallerErrorCode,
    _async_measure_preflight,
    _timer_guard,
)

from .test_installer import FakeReceiver, FakeSession

IDLE_TIMERS = json.dumps({"timers": []})


class _Session(FakeSession):
    """A working receiver, with named commands answered differently."""

    def __init__(self, overrides: dict[str, CommandResult]) -> None:
        super().__init__(FakeReceiver())
        self._overrides = overrides

    async def run(self, command: str, *, input: bytes | None = None, timeout: float = 30):
        for fragment, result in self._overrides.items():
            if fragment in command:
                return result
        return await super().run(command, input=input, timeout=timeout)


@pytest.mark.parametrize(
    ("fragment", "result"),
    [
        # Free space that is not a number, and free space for fewer filesystems than
        # were asked about.
        ("df -Pk", CommandResult(0, "df: /usr: Permission denied\n")),
        ("df -Pk", CommandResult(0, "100000000\n")),
        # The size of the installed plugin, which decides how much room the install and
        # its backup need.
        ("du -sk", CommandResult(0, "some kilobytes\n")),
        # `test -d` answers 0 or 1; anything else means it did not run.
        ("test -d", CommandResult(2)),
        # `opkg status` answers 0 or 1 in the same way.
        ("opkg status", CommandResult(2)),
        # One package, listed twice, with two versions: there is no right answer to
        # "what is installed" here.
        (
            "opkg status",
            CommandResult(
                0,
                "Package: enigma2-plugin-extensions-mqttbridge\nVersion: 0.0.9\n"
                "Package: enigma2-plugin-extensions-mqttbridge\nVersion: 0.1.0\n",
            ),
        ),
        # A version belonging to some other package's stanza.
        ("opkg status", CommandResult(0, "Package: something-else\nVersion: 0.0.9\n")),
    ],
)
async def test_an_answer_the_preflight_cannot_read_stops_the_install(
    fragment: str, result: CommandResult
) -> None:
    with pytest.raises(InstallerError) as raised:
        await _async_measure_preflight(_Session({fragment: result}))

    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED


async def test_a_receiver_with_no_room_is_refused_rather_than_half_installed() -> None:
    """Running out of flash halfway through an opkg install is the worst outcome here."""
    with pytest.raises(InstallerError) as raised:
        await _async_measure_preflight(_Session({"df -Pk": CommandResult(0, "1\n" * 3)}))

    assert raised.value.code is InstallerErrorCode.NO_SPACE


def test_openwebif_booleans_are_read_exactly_and_nothing_else_is_guessed() -> None:
    """OpenWebif answers `true`, `1` or `True` depending on the image and the endpoint.

    All three are the same answer and are read as one. Anything else is not a fourth
    spelling of "yes" — it is an endpoint this code has not seen, and the guard fails
    closed rather than deciding the box is idle.
    """
    for spelling in ("true", "True", "1", 1, True):
        recording, due = _timer_guard(json.dumps({"isRecording": spelling}), IDLE_TIMERS)
        assert recording is True
        assert due == 0

    for spelling in ("false", "False", "0", 0, False):
        recording, _due = _timer_guard(json.dumps({"isRecording": spelling}), IDLE_TIMERS)
        assert recording is False

    with pytest.raises(InstallerError) as raised:
        _timer_guard(json.dumps({"isRecording": "probably"}), IDLE_TIMERS)
    assert raised.value.code is InstallerErrorCode.PREFLIGHT_FAILED

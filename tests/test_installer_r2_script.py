"""The stop-and-restore script, run by a real shell against a temporary root.

Between `init 4` and `init 3` a household has no picture, so what matters about this
script is what it does when it is interrupted: a signal to the shell, a signal to the
whole process group, the connection that started it going away. Those were measured on
busybox and dash when `docs/TRANSACTION.md` was written; these tests hold the script to
the answers. `init` and `pidof` are stand-ins that write down what was asked of them, and
the restore is the real helper, slowed down by a wrapper so that there is a moment in
which it is still running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
from typing import Any

import asyncssh
import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import PACKAGE, snapshot, start_r2

HELPER = Path(installer_helper.__file__).resolve()
PLUGIN_DIR = "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
WATCHED = "1:0:19:283D:3FB:1:C00000:0:0:0:"
SHELLS = [["/bin/sh"]] + ([["busybox", "sh"]] if shutil.which("busybox") else [])


def _write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


class Box:
    """A receiver in a directory: files, a runlevel, an enigma2 that is up or down."""

    def __init__(self, base: Path, shell: list[str], restore_seconds: float) -> None:
        self.base = base
        self.root = base / "root"
        self.bin = base / "bin"
        self.events = base / "events"
        self.runlevel = base / "runlevel"
        self.enigma = base / "enigma2.running"
        self.status = base / "r2.status"
        self.script = base / "r2.sh"
        self.log = base / "r2.log"
        self.shell = shell
        root = self.root
        _write(
            root / "usr/lib/opkg/status",
            f"Package: {PACKAGE}\nVersion: 0.3.0\n",
        )
        _write(root / f"usr/lib/opkg/info/{PACKAGE}.list", "/old\n")
        _write(root / f"{PLUGIN_DIR}/plugin.py", "old plugin\n")
        _write(
            root / "etc/enigma2/settings",
            "config.tv.lastservice=1:0:19:2B66:3F3:1:C00000:0:0:0:\n"
            "config.plugins.mqttbridge.host=old-broker\n",
        )
        self.backup = root / "home/root/mqttbridge-backups/ha-installer-0123456789ab"
        snapshot(root, self.backup)
        # The new plugin ran and wrote its own settings.
        _write(root / f"{PLUGIN_DIR}/plugin.py", "new plugin\n")
        _write(root / f"usr/lib/opkg/info/{PACKAGE}.list", "/new\n")
        _write(
            root / "etc/enigma2/settings",
            "config.tv.lastservice=1:0:19:2B66:3F3:1:C00000:0:0:0:\n"
            "config.plugins.mqttbridge.host=new-broker\n",
        )
        self.runlevel.write_text("3\n")
        self.enigma.write_text("100\n")
        self.events.write_text("")
        # `init`: change the runlevel, stop or start the interface, write it down.
        _write(
            self.bin / "init",
            "#!/bin/sh\n"
            f'echo "init $1" >> {self.events}\n'
            f'echo "$1" > {self.runlevel}\n'
            f'if [ "$1" = 4 ]; then rm -f {self.enigma}; fi\n'
            f'if [ "$1" = 3 ]; then echo 101 > {self.enigma}; fi\n',
            0o755,
        )
        _write(
            self.bin / "pidof",
            f"#!/bin/sh\n[ -f {self.enigma} ] && cat {self.enigma}\n",
            0o755,
        )
        # The interpreter the script runs the helper with: the real one, except that a
        # restore takes `restore_seconds` first and says when it has finished.
        _write(
            self.bin / "python-slow",
            "#!/bin/sh\n"
            f'if [ "$2" = restore ]; then sleep {restore_seconds}; fi\n'
            f'"{sys.executable}" "$@"\n'
            "rc=$?\n"
            f'if [ "$2" = restore ]; then echo "restore done $rc" >> {self.events}; fi\n'
            f'if [ "$2" = lastservice ]; then echo "lastservice $rc" >> {self.events}; fi\n'
            "exit $rc\n",
            0o755,
        )

    def environ(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}

    def start(self, monkeypatch: pytest.MonkeyPatch) -> int:
        """Start the script the way the installer does: detached, output to a file."""
        monkeypatch.setattr(sys, "executable", str(self.bin / "python-slow"))
        monkeypatch.setenv("PATH", self.environ()["PATH"])
        return start_r2(
            self.root,
            self.backup,
            script=self.script,
            status=self.status,
            log=self.log,
            service=WATCHED,
            provisioning=False,
            shell=self.shell[0],
            init=str(self.bin / "init"),
            pidof=str(self.bin / "pidof"),
        )

    def events_list(self) -> list[str]:
        return self.events.read_text().splitlines()

    def wait_for(self, predicate: Any, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"timed out; events {self.events_list()}, status "
                    f"{self.status.read_text() if self.status.exists() else None}, log "
                    f"{self.log.read_text() if self.log.exists() else None}"
                )
            time.sleep(0.05)

    def finished(self) -> bool:
        return self.status.exists() and any(
            line.startswith("started") for line in self.status.read_text().splitlines()
        )

    def assert_put_back(self) -> None:
        """Runlevel 3, the old plugin and its settings, and the recorded channel."""
        assert self.runlevel.read_text().strip() == "3"
        assert (self.root / f"{PLUGIN_DIR}/plugin.py").read_text() == "old plugin\n"
        settings = (self.root / "etc/enigma2/settings").read_text().splitlines()
        assert "config.plugins.mqttbridge.host=old-broker" in settings
        assert f"config.tv.lastservice={WATCHED}" in settings
        events = self.events_list()
        # The restore is waited for; the channel goes in after it; the start comes last,
        # and only once however many times the trap ran.
        assert events.index("restore done 0") < events.index("lastservice 0")
        assert events.index("lastservice 0") < events.index("init 3")
        assert events.count("init 3") == 1


@pytest.fixture(params=SHELLS, ids=lambda shell: " ".join(shell))
def box(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Box]:
    shell = request.param
    if shell[0] == "busybox":
        # `start_r2` runs one executable with the script as its argument.
        wrapper = _write(
            tmp_path / "bin" / "busybox-sh", '#!/bin/sh\nexec busybox sh "$@"\n', 0o755
        )
        shell = [str(wrapper)]
    yield Box(tmp_path, shell, restore_seconds=1.0)


def test_the_script_stops_restores_writes_the_channel_and_starts(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    box.start(monkeypatch)

    box.wait_for(lambda: "done" in box.status.read_text().split())

    box.assert_put_back()
    assert box.events_list()[0] == "init 4"
    assert box.status.read_text().split("\n")[1:] == [
        "stopping 0",
        "stopped",
        "restored 0",
        "written 0",
        "started 0",
        "done",
        "",
    ]


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGPIPE])
def test_a_signal_while_the_restore_runs_still_ends_with_the_interface_up(
    box: Box, monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    """The trap waits for the restore, writes the channel after it, and starts once.

    Measured: a signal to the shell alone runs the trap at once, while the restore child
    is still working. A trap that did not wait would start the interface - and write the
    channel - under a restore that then renames the settings file over the channel.
    Without PIPE in the list, a shell whose output reader went away dies of it and runs
    no trap at all.
    """
    pid = box.start(monkeypatch)
    box.wait_for(lambda: "stopped" in box.status.read_text().split())

    os.kill(pid, signum)
    box.wait_for(box.finished)
    box.wait_for(lambda: "init 3" in box.events_list())

    box.assert_put_back()
    time.sleep(0.3)
    assert box.events_list().count("init 3") == 1


def test_a_second_signal_cannot_cut_the_first_one_short(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Somebody pressing twice: the trap ignores what arrives while it works."""
    pid = box.start(monkeypatch)
    box.wait_for(lambda: "stopped" in box.status.read_text().split())

    os.kill(pid, signal.SIGTERM)
    time.sleep(0.1)
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGHUP)
    box.wait_for(box.finished)
    box.wait_for(lambda: "init 3" in box.events_list())

    box.assert_put_back()


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False


def _hanging_up_server(groups: list[int]) -> Any:
    """An SSH server that runs each command in a real shell, in a process group of its own.

    `groups` collects those groups, so that the test can do to them what an SSH server
    may do when a connection goes away: send the whole group SIGHUP. Which signal, if
    any, dropbear sends is not measured; this is the harsher case.
    """

    async def process(channel: Any) -> None:
        child = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            channel.command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        groups.append(child.pid)
        out, err = await child.communicate()
        channel.stdout.write(out)
        channel.stderr.write(err)
        channel.exit(child.returncode or 0)

    return process


async def test_a_connection_dropped_during_the_stop_changes_nothing_on_the_receiver(
    box: Box, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """Close the SSH connection between `init 4` and `init 3` - not a signal from the test.

    The installer starts the script with the helper's `r2-start` over SSH and then only
    reads its status file. The connection is closed while the restore is still running,
    and the server then hangs up the command's whole process group. The script is in a
    session of its own, so the receiver still ends in runlevel 3, the restore complete
    and the recorded channel written.
    """
    groups: list[int] = []
    key = asyncssh.generate_private_key("ssh-ed25519")
    listener = await asyncssh.create_server(
        _Server,
        "127.0.0.1",
        0,
        server_host_keys=[key],
        process_factory=_hanging_up_server(groups),
        encoding=None,
    )
    monkeypatch.setenv("PATH", box.environ()["PATH"])
    command = " ".join(
        [
            str(box.bin / "python-slow"),
            str(HELPER),
            "r2-start",
            str(box.backup),
            "--root",
            str(box.root),
            "--script",
            str(box.script),
            "--status",
            str(box.status),
            "--log",
            str(box.log),
            "--service",
            WATCHED,
            "--shell",
            box.shell[0],
            "--python",
            str(box.bin / "python-slow"),
            "--init",
            str(box.bin / "init"),
            "--pidof",
            str(box.bin / "pidof"),
        ]
    )
    try:
        async with asyncssh.connect(
            "127.0.0.1",
            listener.get_port(),
            known_hosts=None,
            username="root",
            client_keys=[],
        ) as connection:
            started = await connection.run(command, check=False)
            assert started.exit_status == 0, started.stderr
            assert "pid" in json.loads(started.stdout)
            while "stopped" not in (box.status.read_text() if box.status.exists() else ""):
                await asyncio.sleep(0.05)
        # The connection is gone; the server hangs up everything it started.
        for group in groups:
            try:
                os.killpg(group, signal.SIGHUP)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 15
        while not box.finished() or "init 3" not in box.events_list():
            assert time.monotonic() < deadline, box.events_list()
            await asyncio.sleep(0.05)
    finally:
        listener.close()
        await listener.wait_closed()

    box.assert_put_back()


def test_the_script_names_nothing_it_was_not_given(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything the script is given arrives quoted: a reference is one word."""
    box.start(monkeypatch)
    box.wait_for(lambda: "done" in box.status.read_text().split())

    text = box.script.read_text()
    assert f"service={WATCHED}" in text or f"service='{WATCHED}'" in text
    assert oct(box.script.stat().st_mode & 0o777) == "0o700"

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
import random
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

import asyncssh
import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import PACKAGE, snapshot

PLUGIN_DIR = "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
WATCHED = "1:0:19:283D:3FB:1:C00000:0:0:0:"
# The receiver's /bin/sh is bash, run as `sh`; dash and busybox are what other images
# and development machines have.
SHELLS = (
    [["/bin/sh"]]
    + ([["busybox", "sh"]] if shutil.which("busybox") else [])
    + ([["bash-as-sh"], ["bash"]] if shutil.which("bash") else [])
)


def _write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


class Box:
    """A receiver in a directory: files, a runlevel, an enigma2 that is up or down."""

    def __init__(
        self,
        base: Path,
        shell: list[str],
        restore_seconds: float,
        *,
        enigma_stops: bool = True,
        stop_seconds: int = 1,
        stop_wait: int | None = 10,
        restore_limit: int = 60,
        shutdown: str | None = None,
        record_exit: bool = False,
        restore_ignores_term: bool = False,
    ) -> None:
        self.base = base
        self.root = base / "root"
        self.bin = base / "bin"
        self.events = base / "events"
        self.runlevel = base / "runlevel"
        self.enigma = base / "enigma2.running"
        # The script's own directory, as the installer names it.
        self.dir = base / "tmp" / "enigma2-mqtt-r2-0123456789ab"
        self.status = self.dir / "status"
        self.script = self.dir / "r2.sh"
        self.log = self.dir / "log"
        self.shell = shell
        self.stop_wait = stop_wait
        self.restore_limit = restore_limit
        self.shutdown = shutdown
        # The receiver runs the helper as a file of its own in /tmp; from inside the
        # package directory the integration's `select.py` would shadow the standard
        # library's `select`, which `subprocess` imports.
        self.helper = base / "tmp" / "enigma2-mqtt-installer-0123456789ab.py"
        self.helper.parent.mkdir(parents=True)
        shutil.copyfile(installer_helper.__file__, self.helper)
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
        # Where a stand-in signals the script: "<point> <signal>[,<signal>...]", consumed
        # once. The stand-in runs in the script's foreground (or, for the restore, as its
        # child), so the script's trap runs right after that command - at exactly the
        # step named, with no sleep deciding where it lands. Several signals are sent
        # together, all while the script waits for that one command.
        self.signal_at = base / "signal-at"
        # For the one point no stand-in runs at: "<script pid> <signal>", read by the
        # fork hook right after the script forks the restore.
        self.fork_arm = base / "fork-arm"
        downs = base / "pidof-downs"
        signal_here = (
            f'if [ -f {self.signal_at} ]; then read point sig < {self.signal_at}; '
            f'if [ "$point" = "{{point}}" ]; then rm -f {self.signal_at}; '
            f'echo "signalled $point" >> {self.events}; '
            'for s in $(echo "$sig" | tr , " "); do kill -"$s" "$PPID"; done; fi; fi\n'
        )
        # `init`: change the runlevel, stop or start the interface, write it down. Like
        # the real one it returns before the interface has gone: enigma2 stops a second
        # later - or, for a receiver whose interface will not stop, never. With no
        # seconds at all it is gone when `init` returns, so the script's steps - and its
        # forks - are the same on every run.
        if not enigma_stops:
            stop = ":"
        elif stop_seconds == 0:
            stop = f"rm -f {self.enigma}"
        else:
            stop = f"(sleep {stop_seconds}; rm -f {self.enigma}) &"
        _write(
            self.bin / "init",
            "#!/bin/sh\n"
            f'echo "init $1" >> {self.events}\n'
            f'echo "$1" > {self.runlevel}\n'
            f'if [ "$1" = 4 ]; then\n    {stop}\nfi\n'
            f'if [ "$1" = 3 ]; then echo 101 > {self.enigma}; fi\n'
            + signal_here.replace("{point}", "init$1"),
            0o755,
        )
        # `pidof`: "wait" signals from the first call that still finds enigma2 (the
        # stop wait); "stopped" and "fork" act on the second call that finds it gone,
        # which is the script's last look before it records the stop and forks the
        # restore.
        # "cutsleep" is not consumed: every call that still finds enigma2 has a signal
        # sent 0.3 s later, into the script's `sleep` between two looks.
        _write(
            self.bin / "pidof",
            "#!/bin/sh\n"
            f"if [ -f {self.enigma} ]; then\n"
            f"    cat {self.enigma}\n"
            "    " + signal_here.replace("{point}", "wait")
            + f'    if [ "$(cat {self.signal_at} 2>/dev/null)" = cutsleep ]; then '
            '(sleep 0.3; kill -TERM "$PPID") & fi\n'
            "    exit 0\nfi\n"
            f"echo x >> {downs}\n"
            f'if [ "$(wc -l < {downs})" -eq 2 ]; then\n'
            "    " + signal_here.replace("{point}", "stopped") + ""
            f"    if [ -f {self.signal_at} ]; then read point sig < {self.signal_at}; "
            f'if [ "$point" = fork ]; then rm -f {self.signal_at}; '
            f'echo "$PPID $sig" > {self.fork_arm}; fi; fi\n'
            "fi\n"
            "exit 1\n",
            0o755,
        )
        # The interpreter the script runs the helper with: the real one, except that a
        # restore takes `restore_seconds` first and says when it has finished.
        _write(
            self.bin / "python-slow",
            "#!/bin/sh\n"
            + signal_here.replace("{point}", "$2")
            + f'if [ "$2" = restore ]; then echo "restore pid $$" >> {self.events}; '
            + ("trap '' TERM; " if restore_ignores_term else "")
            + f"sleep {restore_seconds}; fi\n"
            f'"{sys.executable}" "$@"\n'
            "rc=$?\n"
            f'if [ "$2" = restore ]; then echo "restore done $rc" >> {self.events}; fi\n'
            f'if [ "$2" = lastservice ]; then echo "lastservice $rc" >> {self.events}; fi\n'
            "exit $rc\n",
            0o755,
        )
        # `runlevel`, named only when a test says the system is going down: what
        # sysvinit's utmp record says while it runs runlevel 0 or 6.
        _write(
            self.bin / "runlevel",
            f'#!/bin/sh\necho "4 {shutdown}"\n' if shutdown else "#!/bin/sh\necho 'N 3'\n",
            0o755,
        )
        if record_exit:
            # The script's exit status, which nothing else can see: it is detached.
            self.shell = [
                str(
                    _write(
                        self.bin / "exit-recorder",
                        f'#!/bin/sh\n"{shell[0]}" "$@"\necho "exit $?" >> {self.events}\n',
                        0o755,
                    )
                )
            ]

    def environ(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}

    def command(self) -> list[str]:
        """The helper's `r2-start`, as the installer sends it, with the stand-ins named."""
        return [
            sys.executable,
            str(self.helper),
            "r2-start",
            str(self.backup),
            "--root",
            str(self.root),
            "--dir",
            str(self.dir),
            *(["--stop-wait", str(self.stop_wait)] if self.stop_wait is not None else []),
            "--restore-limit",
            str(self.restore_limit),
            "--service",
            WATCHED,
            "--shell",
            self.shell[0],
            "--python",
            str(self.bin / "python-slow"),
            "--init",
            str(self.bin / "init"),
            "--pidof",
            str(self.bin / "pidof"),
            *(["--runlevel", str(self.bin / "runlevel")] if self.shutdown else []),
        ]

    def start(self, monkeypatch: pytest.MonkeyPatch) -> int:
        """Start the script the way the installer does: detached, output to a file."""
        monkeypatch.setenv("PATH", self.environ()["PATH"])
        started = subprocess.run(self.command(), check=True, capture_output=True, text=True)
        return int(json.loads(started.stdout)["pid"])

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


def _shell(tmp_path: Path, shell: list[str]) -> list[str]:
    """Return the one executable `start_r2` runs the script with."""
    if shell[0] == "busybox":
        wrapper = _write(
            tmp_path / "bin" / "busybox-sh", '#!/bin/sh\nexec busybox sh "$@"\n', 0o755
        )
        return [str(wrapper)]
    if shell[0] == "bash-as-sh":
        # bash started under the name `sh` runs in its POSIX mode, as on the receiver.
        link = tmp_path / "bash-as-sh" / "sh"
        link.parent.mkdir()
        link.symlink_to(shutil.which("bash"))
        return [str(link)]
    if shell[0] == "bash":
        return [str(shutil.which("bash"))]
    return shell


@pytest.fixture(params=SHELLS, ids=lambda shell: " ".join(shell))
def shell(request: pytest.FixtureRequest, tmp_path: Path) -> list[str]:
    return _shell(tmp_path, request.param)


@pytest.fixture
def box(shell: list[str], tmp_path: Path) -> Iterator[Box]:
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
    """The restore is waited for, the channel written after it, and the start runs once.

    Measured: a signal to the shell alone runs the trap at once, while the restore child
    is still working. A script that did not wait would start the interface - and write the
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


# The fork of the restore and the line that keeps its pid are two steps of the script with
# no command of its own between them, so no stand-in can signal there. This shim does:
# preloaded into the script's shell, it sends the armed signal to that shell right after a
# fork returns in it, once `pidof` has armed it - the next fork after the script's last
# look for enigma2 is the restore's. A statically linked shell (busybox, often) does not
# load it, and the test says so instead of passing.
_FORK_HOOK = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

pid_t fork(void)
{
    static pid_t (*real_fork)(void);
    if (!real_fork)
        real_fork = (pid_t (*)(void))dlsym(RTLD_NEXT, "fork");
    pid_t child = real_fork();
    const char *arm = getenv("R2_TEST_FORK_ARM");
    const char *events = getenv("R2_TEST_EVENTS");
    if (child > 0 && arm && events) {
        FILE *in = fopen(arm, "r");
        long pid = 0;
        int sig = 0;
        if (in) {
            int fields = fscanf(in, "%ld %d", &pid, &sig);
            fclose(in);
            if (fields == 2 && pid == (long)getpid()) {
                unlink(arm);
                FILE *out = fopen(events, "a");
                if (out) {
                    fputs("signalled fork\n", out);
                    fclose(out);
                }
                kill(getpid(), sig);
            }
        }
    }
    return child;
}
"""


@pytest.fixture(scope="session")
def fork_hook(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("no C compiler for the fork hook")
    directory = tmp_path_factory.mktemp("fork-hook")
    source = _write(directory / "hook.c", _FORK_HOOK)
    library = directory / "hook.so"
    subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", str(library), str(source), "-ldl"],
        check=True,
        capture_output=True,
    )
    return library


EARLY_POINTS = ("init4", "wait", "stopped", "fork")
SIGNALLED_POINTS = [
    # (point, signals) - where the signals land, and which; several are sent together.
    *(
        (point, (signum,))
        for point in EARLY_POINTS
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGPIPE)
    ),
    # INT and another signal during one foreground command: measured on bash, the later
    # wait for the restore then returned 143 or 129 at once, with the restore still
    # running, whether or not signals were ignored by then.
    *(
        (point, pair)
        for point in ("init4", "wait")
        for pair in (
            (signal.SIGINT, signal.SIGTERM),
            (signal.SIGINT, signal.SIGHUP),
            (signal.SIGTERM, signal.SIGINT),
        )
    ),
    ("restore", (signal.SIGTERM,)),
    ("lastservice", (signal.SIGTERM,)),
    ("init3", (signal.SIGTERM,)),
]


@pytest.mark.parametrize(
    ("point", "signums"),
    SIGNALLED_POINTS,
    ids=[
        f"{point}-{'+'.join(signum.name for signum in signums)}"
        for point, signums in SIGNALLED_POINTS
    ],
)
def test_a_signal_anywhere_after_the_trap_changes_nothing_the_script_does(
    shell: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    point: str,
    signums: tuple[signal.Signals, ...],
) -> None:
    """Wherever a signal lands, the restore completes, then the channel, then the start.

    The points are the script's steps in order: `init 4` (the stop requested), the wait
    for enigma2 to go, its last look before it records the stop, the fork of the restore
    (before its pid is kept), the restore, the channel and `init 3`. A trap that finished
    at once from any of the first four started enigma2 on the plugin the rollback exists
    to remove - or, from the fork, left the restore running on its own to rename the
    settings over the channel under a running interface, with no `restored` in the status
    for the installer to read. A signal that arrived before the finishing step began ends
    the script with exit status 1 and without `done`; one while it runs is ignored.
    """
    box = Box(
        tmp_path,
        shell,
        restore_seconds=0.5,
        stop_seconds=1 if point == "wait" else 0,
        record_exit=True,
    )
    box.signal_at.write_text(f"{point} {','.join(str(int(s)) for s in signums)}\n")
    if point == "fork":
        monkeypatch.setenv("LD_PRELOAD", str(request.getfixturevalue("fork_hook")))
        monkeypatch.setenv("R2_TEST_FORK_ARM", str(box.fork_arm))
        monkeypatch.setenv("R2_TEST_EVENTS", str(box.events))
    box.start(monkeypatch)

    box.wait_for(lambda: any(e.startswith("exit ") for e in box.events_list()))
    # A restore the script lost track of finishes after the start; wait for it either way.
    box.wait_for(lambda: any(e.startswith("restore done") for e in box.events_list()))

    if point == "fork" and f"signalled {point}" not in box.events_list():
        pytest.skip("this shell does not load the fork hook (statically linked)")
    assert f"signalled {point}" in box.events_list()
    box.assert_put_back()
    steps = box.status.read_text().split("\n")[1:]
    assert steps[:5] == ["stopping 0", "stopped", "restored 0", "written 0", "started 0"]
    if point in EARLY_POINTS:
        assert "done" not in steps
        assert "exit 1" in box.events_list()
    elif point in ("lastservice", "init3"):
        assert steps[5] == "done"
        assert "exit 0" in box.events_list()
    time.sleep(0.3)
    assert box.events_list().count("init 3") == 1
    assert box.events_list().count("restore done 0") == 1


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_flood_of_signals_changes_nothing_the_script_does(
    shell: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    """HUP, INT, TERM and PIPE in random order, a few milliseconds apart, from the moment
    the trap is set until the interface is started: the same restore, channel and start.
    """
    rng = random.Random(seed)
    box = Box(tmp_path, shell, restore_seconds=0.5, stop_seconds=1)
    pid = box.start(monkeypatch)
    box.wait_for(lambda: "begun" in box.status.read_text())

    sent = 0
    deadline = time.monotonic() + 15
    while not box.finished() and time.monotonic() < deadline:
        with suppress(ProcessLookupError):
            os.kill(
                pid, rng.choice([signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGPIPE])
            )
            sent += 1
        time.sleep(rng.uniform(0.001, 0.005))
    box.wait_for(box.finished)
    box.wait_for(lambda: any(e.startswith("restore done") for e in box.events_list()))

    assert sent > 50
    box.assert_put_back()
    steps = box.status.read_text().split("\n")[1:]
    assert steps[:5] == ["stopping 0", "stopped", "restored 0", "written 0", "started 0"]
    time.sleep(0.3)
    assert box.events_list().count("restore done 0") == 1


def test_signals_do_not_shorten_the_wait_for_the_interface_to_stop(
    shell: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop wait is a time, not a number of sleeps.

    busybox runs `sleep` itself, and a signal the script catches ends that sleep early:
    counted in sleeps, a TERM every 0.3 s made a four-second wait last about 1.3 s, and
    an interface that took two seconds to stop was reported as never stopping - its
    settings then not restored and its channel not written.
    """
    box = Box(tmp_path, shell, restore_seconds=0.1, stop_seconds=2, stop_wait=4)
    box.signal_at.write_text("cutsleep\n")
    box.start(monkeypatch)

    box.wait_for(box.finished, timeout=20)
    box.wait_for(lambda: "init 3" in box.events_list())

    steps = box.status.read_text().split("\n")[1:]
    assert steps[:3] == ["stopping 0", "stopped", "restored 0"]
    box.assert_put_back()


@pytest.mark.parametrize("level", ["0", "6"])
def test_a_system_going_down_is_not_started_again(
    shell: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """While sysvinit runs runlevel 0 or 6, `init 3` would ask it for the interface back.

    The restore and the channel still go in - the next start reads them - and the status
    says why nothing was started.
    """
    box = Box(tmp_path, shell, restore_seconds=0.1, stop_seconds=0, shutdown=level)
    box.start(monkeypatch)

    box.wait_for(lambda: "done" in box.status.read_text().split())

    steps = box.status.read_text().split("\n")[1:]
    assert steps[:5] == ["stopping 0", "stopped", "restored 0", "written 0", "started shutdown"]
    assert "init 3" not in box.events_list()
    settings = (box.root / "etc/enigma2/settings").read_text().splitlines()
    assert "config.plugins.mqttbridge.host=old-broker" in settings
    assert f"config.tv.lastservice={WATCHED}" in settings


def _strace_shell(box: Box, log: Path, options: list[str]) -> None:
    """Run the box's shell under strace, which follows only the shell itself."""
    wrapper = box.bin / f"strace-{log.name}"
    _write(
        wrapper,
        "#!/bin/sh\nexec strace -qq -o "
        + shlex.quote(str(log))
        + " "
        + " ".join(shlex.quote(option) for option in options)
        + " "
        + shlex.quote(box.shell[0])
        + ' "$@"\n',
        0o755,
    )
    box.shell = [str(wrapper)]


@pytest.mark.parametrize(
    "signum",
    [signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGPIPE],
    ids=lambda signum: signum.name,
)
def test_a_signal_during_the_fork_of_the_restore_changes_nothing(
    shell: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    """strace delivers the signal as the shell enters the system call that forks the restore.

    That is the step with nothing of the script's own around it, which the fork hook
    reaches only in a shell that loads it; strace reaches a statically linked one too.
    A first run, traced only, finds which fork made the restore; the second injects the
    signal into that one. The kernel handles the signal and then forks, so the shell
    sees it before it keeps the restore's pid.
    """
    if shutil.which("strace") is None:
        pytest.skip("strace is not installed")
    calls = "clone,clone3,fork,vfork"

    dry = Box(tmp_path / "dry", shell, restore_seconds=0.1, stop_seconds=0)
    trace = tmp_path / "dry.trace"
    _strace_shell(dry, trace, ["-e", f"trace={calls}"])
    dry.start(monkeypatch)
    dry.wait_for(lambda: "done" in dry.status.read_text().split())
    restore_pid = next(
        e.split()[2] for e in dry.events_list() if e.startswith("restore pid ")
    )
    seen: dict[str, int] = {}
    target: tuple[str, int] | None = None
    for line in trace.read_text().splitlines():
        name = line.split("(", 1)[0]
        if name not in calls.split(","):
            continue
        seen[name] = seen.get(name, 0) + 1
        if line.rstrip().endswith(f"= {restore_pid}"):
            target = (name, seen[name])
    assert target is not None, trace.read_text()

    box = Box(tmp_path / "run", shell, restore_seconds=0.5, stop_seconds=0)
    name, index = target
    _strace_shell(
        box,
        tmp_path / "run.trace",
        ["-e", f"trace={name}", "-e", f"inject={name}:signal={signum.name}:when={index}"],
    )
    box.start(monkeypatch)

    box.wait_for(box.finished)
    box.wait_for(lambda: "init 3" in box.events_list())
    box.wait_for(lambda: any(e.startswith("restore done") for e in box.events_list()))

    # strace writes down the signal it delivered.
    assert f"--- {signum.name} " in (tmp_path / "run.trace").read_text()
    box.assert_put_back()
    steps = box.status.read_text().split("\n")[1:]
    assert steps[:5] == ["stopping 0", "stopped", "restored 0", "written 0", "started 0"]
    assert "done" not in steps
    time.sleep(0.3)
    assert box.events_list().count("restore done 0") == 1


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
    command = " ".join(box.command())
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


def test_the_script_runs_on_its_own_copy_of_the_helper(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant tidying up its helper - on a stop, say - takes nothing from the script."""
    box.start(monkeypatch)
    box.helper.unlink()

    box.wait_for(lambda: "done" in box.status.read_text().split())

    box.assert_put_back()


def test_an_interface_that_does_not_stop_keeps_its_settings(
    shell: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running enigma2 writes its own settings on its next clean quit.

    So when it has not gone within the wait, the script puts back the files alone and
    writes neither the settings block nor the channel - both would be overwritten, and a
    report that they were put back would be false.
    """
    box = Box(tmp_path, shell, restore_seconds=0.1, enigma_stops=False, stop_wait=1)
    box.start(monkeypatch)

    box.wait_for(lambda: "done" in box.status.read_text().split())

    steps = box.status.read_text().split("\n")[1:]
    assert steps[:3] == ["stopping 0", "stop_timeout", "restored 0"]
    assert "not_written" in steps
    assert (box.root / f"{PLUGIN_DIR}/plugin.py").read_text() == "old plugin\n"
    settings = (box.root / "etc/enigma2/settings").read_text().splitlines()
    assert "config.plugins.mqttbridge.host=new-broker" in settings
    assert f"config.tv.lastservice={WATCHED}" not in settings


def test_a_restore_that_hangs_is_cut_off_and_the_picture_comes_back(
    shell: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picture first: a restore cut off part-way is repeated later; no picture is not."""
    box = Box(tmp_path, shell, restore_seconds=60, restore_limit=1)
    started = time.monotonic()
    box.start(monkeypatch)

    box.wait_for(box.finished, timeout=20)

    # The limit is one second: the end is seen when the restore is gone - a zombie is gone
    # too - not when a later bound runs out.
    assert time.monotonic() - started < 8
    steps = box.status.read_text().split("\n")
    restored = next(step for step in steps if step.startswith("restored "))
    # Killed, so it wrote no status of its own - and none is made up for it.
    assert restored == "restored lost"
    box.wait_for(lambda: "init 3" in box.events_list())
    assert box.runlevel.read_text().strip() == "3"
    assert box.events_list().count("init 3") == 1


def test_a_restore_that_ignores_the_watchdog_is_killed(
    shell: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finishing step does not wait for ever on a restore the watchdog's TERM missed.

    Ten seconds after the limit it sends KILL, and the interface starts. The restore here
    would get to work after 14 seconds; killed at 11, it never does - left alone, it would
    rename the settings under the running interface.
    """
    box = Box(tmp_path, shell, restore_seconds=14, restore_limit=1, restore_ignores_term=True)
    started = time.monotonic()
    box.start(monkeypatch)

    box.wait_for(box.finished, timeout=30)
    box.wait_for(lambda: "init 3" in box.events_list())

    assert time.monotonic() - started < 14
    assert "restored lost" in box.status.read_text().split("\n")
    time.sleep(max(0.0, started + 16 - time.monotonic()))
    assert not any(e.startswith("restore done") for e in box.events_list())
    assert box.events_list().count("init 3") == 1


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (["identity"], 0),
        (["restore", "{missing}"], 1),
        (["restore"], 2),
    ],
    ids=["returns", "raises", "exits"],
)
def test_the_helper_writes_its_own_exit_status_when_asked(
    tmp_path: Path, arguments: list[str], code: int
) -> None:
    """Whatever ends the operation inside Python, the file says what the process exits with."""
    rc_file = tmp_path / "restore-rc"
    root = tmp_path / "root"
    root.mkdir()
    helper = tmp_path / "helper.py"
    shutil.copyfile(installer_helper.__file__, helper)
    argv = [a.replace("{missing}", str(tmp_path / "no-such-backup")) for a in arguments]

    finished = subprocess.run(
        [sys.executable, str(helper), *argv, "--root", str(root), "--rc-file", str(rc_file)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert finished.returncode == code
    assert rc_file.read_text() == f"{code}\n"
    assert not (tmp_path / "restore-rc.tmp").exists()


@pytest.mark.parametrize("init", ["init", "/sbin/init", "/usr/sbin/init"])
def test_the_test_suite_cannot_reach_the_system_init(tmp_path: Path, init: str) -> None:
    """A test that forgot its stand-ins would stop the interface of the machine it runs on."""
    assert os.environ.get(installer_helper.TEST_GUARD)
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(RuntimeError, match="stand-ins"):
        installer_helper.start_r2(
            root,
            root / "backup",
            directory=tmp_path / "r2",
            service=WATCHED,
            provisioning=False,
            init=init,
            pidof=str(tmp_path / "pidof"),
        )

    assert not (tmp_path / "r2").exists()


def test_the_script_waits_for_an_interface_that_is_slow_to_stop(
    shell: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With its own default wait, a stop that takes seconds is waited for.

    A wait too short would take the files-only path on every real receiver, where
    enigma2 takes seconds to go after `init 4`.
    """
    box = Box(tmp_path, shell, restore_seconds=0.1, stop_seconds=3, stop_wait=None)
    box.start(monkeypatch)

    box.wait_for(lambda: "done" in box.status.read_text().split(), timeout=30)

    steps = box.status.read_text().split("\n")[1:]
    assert steps[:3] == ["stopping 0", "stopped", "restored 0"]
    box.assert_put_back()

"""The receiver-side half of the restart rule, against a temporary root and a fake OpenWebif.

What the helper does on the receiver for a restart: read the channel and the standby state
from OpenWebif, write the channel enigma2 starts on, zap, ask for a power state, put the
old plugin back under a running interface without a half-written tree, and record which
transaction holds the lock. Each is checked here against the thing it talks to - a
filesystem and an HTTP server - rather than against a description of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from custom_components.enigma2_mqtt import installer_helper
from custom_components.enigma2_mqtt.installer_helper import (
    PACKAGE,
    STALE_LOCK_SECONDS,
    claim_transaction,
    power_state,
    record_state,
    restore,
    snapshot,
    staging_parent,
    write_lastservice,
    zap,
)

from . import released_installer_helper_0_3_1 as released

HELPER = Path(installer_helper.__file__)
PLUGINS = "usr/lib/enigma2/python/Plugins"
PLUGIN_DIR = f"{PLUGINS}/Extensions/MQTTBridge"
WATCHED = "1:0:19:283D:3FB:1:C00000:0:0:0:"
# An IPTV reference: a stream URL with `//` and an escaped colon, then a name.
IPTV = "4097:0:1:0:0:0:0:0:0:0:http%3a//192.0.2.40/live.ts:Kanał z sieci"


def _write(root: Path, relative: str, value: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    return target


def _receiver(root: Path) -> None:
    _write(
        root,
        "usr/lib/opkg/status",
        f"Package: unrelated\nVersion: 7\n\nPackage: {PACKAGE}\nVersion: 0.3.0\n",
    )
    _write(root, f"usr/lib/opkg/info/{PACKAGE}.list", "/old\n")
    _write(root, f"{PLUGIN_DIR}/plugin.py", "old plugin\n")
    _write(root, f"{PLUGIN_DIR}/bridge.py", "old bridge\n")
    _write(root, f"{PLUGINS}/Extensions/Other/plugin.py", "somebody else's\n")
    _write(
        root,
        "etc/enigma2/settings",
        f"config.tv.lastservice={WATCHED}\n"
        "config.plugins.mqttbridge.host=old-broker\n",
    )
    _write(root, "etc/enigma2/mqttbridge.json", '{"old":true}\n')


def _upgrade(root: Path) -> None:
    """What `opkg install` of a newer plugin leaves behind."""
    _write(root, f"{PLUGIN_DIR}/plugin.py", "new plugin\n")
    _write(root, f"{PLUGIN_DIR}/bridge.py", "new bridge\n")
    _write(root, f"{PLUGIN_DIR}/update.py", "only in the new plugin\n")
    _write(root, f"usr/lib/opkg/info/{PACKAGE}.list", "/new\n")
    _write(root, "etc/enigma2/mqttbridge.json", '{"new":true}\n')


class _OpenWebif(BaseHTTPRequestHandler):
    """Enough of OpenWebif for the helper: statusinfo, zap, powerstate."""

    state: dict[str, Any]

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        url = urlparse(self.path)
        query = parse_qs(url.query)
        self.state["requests"].append(self.path)
        if url.path == "/api/statusinfo":
            body = {
                "currservice_serviceref": self.state["service"],
                "inStandby": self.state["standby"],
                "isStreaming": "false",
            }
        elif url.path == "/api/zap":
            self.state["service"] = query["sRef"][0]
            body = {"result": True, "message": "Active service is now ..."}
        elif url.path == "/api/powerstate":
            if self.state.get("reset_on_restart") and query["newstate"] == ["3"]:
                # What a quit can do to the connection that asked for it.
                self.connection.close()
                return
            body = {"result": True, "instandby": False}
        else:
            self.send_error(404)
            return
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        del args


@pytest.fixture
def webif(socket_enabled: None) -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"service": WATCHED, "standby": "false", "requests": []}
    handler = type("Handler", (_OpenWebif,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()


def test_the_record_reads_what_the_receiver_plays_and_whether_it_sleeps(
    tmp_path: Path, webif: tuple[str, dict[str, Any]]
) -> None:
    """OpenWebif spells its booleans as words; `true` is not a truthy string here."""
    _receiver(tmp_path)
    url, state = webif
    state["standby"] = "true"

    assert record_state(tmp_path, url) == {"service": WATCHED, "standby": True, "saved": WATCHED}


def test_an_interface_that_does_not_answer_records_no_channel(
    tmp_path: Path, socket_enabled: None
) -> None:
    """Nothing recorded is a fact to report, never a channel to invent."""
    _receiver(tmp_path)

    recorded = record_state(tmp_path, "http://127.0.0.1:9")

    assert recorded["service"] is None
    assert recorded["standby"] is None


def test_a_zap_carries_the_whole_reference_to_openwebif(
    tmp_path: Path, webif: tuple[str, dict[str, Any]]
) -> None:
    """An IPTV reference has a `//`, a `%3a` and a name with a space and a Polish letter.

    Passed as the positional path it would have gone through `Path`, which folds `//`
    into one slash - a different channel, or none. It is an option, quoted whole.
    """
    url, state = webif
    result = subprocess.run(
        [sys.executable, str(HELPER), "zap", "--service", IPTV, "--webif", url],
        check=False,
    )

    assert result.returncode == 0
    assert state["service"] == IPTV


def test_a_restart_is_asked_for_without_trusting_the_answer(
    tmp_path: Path, webif: tuple[str, dict[str, Any]]
) -> None:
    """The quit can reset the very connection that asked for it; that is not a failure."""
    url, state = webif
    state["reset_on_restart"] = True

    assert power_state(3, url) is False
    result = subprocess.run(
        [sys.executable, str(HELPER), "powerstate", "--state", "3", "--webif", url],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0
    assert "/api/powerstate?newstate=3" in state["requests"]
    assert power_state(5, url) is True


def test_the_channel_to_start_on_is_one_line_and_nothing_else_changes(tmp_path: Path) -> None:
    _receiver(tmp_path)
    settings = tmp_path / "etc/enigma2/settings"

    write_lastservice(tmp_path, IPTV)

    lines = settings.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "config.plugins.mqttbridge.host=old-broker",
        f"config.tv.lastservice={IPTV}",
    ]


@pytest.mark.parametrize("reference", ["", "1:0:1\nconfig.misc.x=1", "a\rb", "x" * 1025])
def test_a_reference_that_would_break_the_settings_file_is_refused(
    tmp_path: Path, reference: str
) -> None:
    _receiver(tmp_path)
    before = (tmp_path / "etc/enigma2/settings").read_bytes()

    with pytest.raises(ValueError):
        write_lastservice(tmp_path, reference)

    assert (tmp_path / "etc/enigma2/settings").read_bytes() == before


def test_withdrawing_never_touches_the_settings_block(tmp_path: Path) -> None:
    """Withdrawing runs under a live enigma2; its settings are its own until it quits.

    The transaction changed no setting, and a block written now is overwritten from
    memory by the next clean quit - it could only race the household's own saves.
    """
    _receiver(tmp_path)
    backup = tmp_path / "home/root/mqttbridge-backups/ha-installer-0123456789ab"
    snapshot(tmp_path, backup)
    _upgrade(tmp_path)
    settings = tmp_path / "etc/enigma2/settings"
    settings.write_text(settings.read_text() + "config.plugins.mqttbridge.host=new\n")
    before = settings.read_bytes()

    refused = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "withdraw",
            str(backup),
            "--settings",
            "--root",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
    )
    done = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "withdraw",
            str(backup),
            "--provisioning",
            "--root",
            str(tmp_path),
        ],
        check=False,
    )

    assert refused.returncode != 0
    assert done.returncode == 0
    assert settings.read_bytes() == before
    assert (tmp_path / f"{PLUGIN_DIR}/plugin.py").read_text() == "old plugin\n"
    assert not (tmp_path / f"{PLUGIN_DIR}/update.py").exists()
    assert (tmp_path / "etc/enigma2/mqttbridge.json").read_text() == '{"old":true}\n'


def _plugin_scan(root: Path) -> dict[str, str]:
    """What enigma2's plugin loader would import: every directory two levels under Plugins."""
    found: dict[str, str] = {}
    plugins = root / PLUGINS
    for category in plugins.iterdir() if plugins.is_dir() else ():
        if not category.is_dir():
            continue
        for plugin in category.iterdir():
            if plugin.is_dir():
                found[f"{category.name}/{plugin.name}"] = "".join(
                    sorted(path.name for path in plugin.iterdir())
                )
    return found


def test_the_plugin_goes_back_whole_and_never_where_the_loader_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart answered during a restore meets the old tree or the new, never a mix.

    Every rename the restore makes is watched. At each one the loader's view is taken:
    the plugin directory is either absent (the moment between the two renames) or one
    complete tree, and no other directory the loader imports ever holds a copy - a
    staged copy under `Plugins/Extensions/` would be a second installation of the plugin.
    """
    _receiver(tmp_path)
    backup = tmp_path / "home/root/mqttbridge-backups/ha-installer-0123456789ab"
    snapshot(tmp_path, backup)
    _upgrade(tmp_path)
    old_tree = "bridge.pyplugin.py"
    new_tree = "bridge.pyplugin.pyupdate.py"
    views: list[dict[str, str]] = []
    real_rename = os.rename

    def watched_rename(source: Any, target: Any) -> None:
        views.append(_plugin_scan(tmp_path))
        real_rename(source, target)
        views.append(_plugin_scan(tmp_path))

    monkeypatch.setattr(installer_helper.os, "rename", watched_rename)

    restore(tmp_path, backup, restore_provisioning=True, restore_settings=False)

    assert views, "the plugin directory was not swapped in by renames"
    for view in views:
        assert set(view) <= {"Extensions/MQTTBridge", "Extensions/Other"}
        assert view.get("Extensions/MQTTBridge", old_tree) in (old_tree, new_tree)
    assert views[-1]["Extensions/MQTTBridge"] == old_tree
    staging = staging_parent(tmp_path)
    assert staging == tmp_path / "usr/lib/enigma2/python"
    assert not [path for path in staging.iterdir() if path.name.startswith(".mqttbridge-")]


def test_a_swap_that_would_cross_filesystems_is_refused_before_anything_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename does not cross filesystems; a copy that pretends to is a half-written tree."""
    _receiver(tmp_path)
    backup = tmp_path / "home/root/mqttbridge-backups/ha-installer-0123456789ab"
    snapshot(tmp_path, backup)
    _upgrade(tmp_path)
    real_stat = os.stat
    parent = str(staging_parent(tmp_path))

    class _Elsewhere:
        def __init__(self, result: os.stat_result) -> None:
            self._result = result

        def __getattr__(self, name: str) -> Any:
            if name == "st_dev":
                return self._result.st_dev + 1
            return getattr(self._result, name)

    def split_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_stat(path, *args, **kwargs)
        return _Elsewhere(result) if str(path) == parent else result

    monkeypatch.setattr(installer_helper.os, "stat", split_stat)

    with pytest.raises(ValueError, match="different filesystems"):
        restore(tmp_path, backup, restore_provisioning=False, restore_settings=False)

    assert (tmp_path / f"{PLUGIN_DIR}/update.py").exists()


def _age(lock: Path, seconds: float, monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Make the lock's owner record `seconds` old by uptime, for `module`'s reading."""
    owner = json.loads((lock / "owner.json").read_text(encoding="ascii"))
    now = float(owner["uptime"] or 0) + seconds
    monkeypatch.setattr(module, "uptime", lambda: now)


@pytest.fixture
def one_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both helpers read the same boot id and uptime, as on one receiver."""
    boot = tmp_path / "boot_id"
    boot.write_text("7b1c2d3e-0000-4000-8000-000000000001\n", encoding="ascii")
    up = tmp_path / "uptime"
    up.write_text("1000.00 900.00\n", encoding="ascii")
    for module in (installer_helper, released):
        monkeypatch.setattr(module, "BOOT_ID_PATH", boot)
        monkeypatch.setattr(module, "UPTIME_PATH", up)


def test_the_lock_owner_names_the_transaction_in_ascii(tmp_path: Path, one_boot: None) -> None:
    """A released helper decodes the record as ASCII; anything else looks abandoned."""
    lock = tmp_path / ".ha-installer.lock"

    claim_transaction(lock, "0123456789ab")

    raw = (lock / "owner.json").read_bytes()
    raw.decode("ascii")
    owner = json.loads(raw)
    assert owner["id"] == "0123456789ab"
    assert "origin" not in owner
    with pytest.raises(ValueError):
        claim_transaction(tmp_path / "other.lock", "not-an-id")


def test_a_reclaimed_installer_lock_hands_over_its_transaction_id(
    tmp_path: Path, one_boot: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery restores the abandoned transaction's snapshot by id, never by time."""
    lock = tmp_path / ".ha-installer.lock"
    claim_transaction(lock, "0123456789ab")
    _age(lock, STALE_LOCK_SECONDS + 60, monkeypatch, installer_helper)

    assert claim_transaction(lock, "ba9876543210") == "0123456789ab"
    assert json.loads((lock / "owner.json").read_text())["id"] == "ba9876543210"


@pytest.mark.parametrize(
    "record",
    [
        # The plugin's own update helper: its snapshot has another name, and it
        # recovers its own transactions.
        {"origin": "mqtt", "id": "0123456789ab"},
        # The released installer: no id at all.
        {},
        {"id": "../../etc"},
    ],
)
def test_only_an_installer_lock_with_a_proper_id_is_handed_over(
    tmp_path: Path,
    one_boot: None,
    monkeypatch: pytest.MonkeyPatch,
    record: dict[str, str],
) -> None:
    lock = tmp_path / ".ha-installer.lock"
    lock.mkdir()
    owner = {
        "pid": 1,
        "started": int(time.time()),
        "boot_id": "7b1c2d3e-0000-4000-8000-000000000001",
        "uptime": 1000.0,
        **record,
    }
    (lock / "owner.json").write_text(json.dumps(owner), encoding="ascii")
    _age(lock, STALE_LOCK_SECONDS + 60, monkeypatch, installer_helper)

    assert claim_transaction(lock, "ba9876543210") == ""


def test_the_released_helper_respects_a_lock_this_one_claims(
    tmp_path: Path, one_boot: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.3.1 is what receivers in the field run; it must read the new record as a live lock."""
    lock = tmp_path / ".ha-installer.lock"
    claim_transaction(lock, "0123456789ab")

    assert released._is_stale(lock) == ""
    with pytest.raises(FileExistsError):
        released.claim_transaction(lock)
    _age(lock, STALE_LOCK_SECONDS - 60, monkeypatch, released)
    assert released._is_stale(lock) == ""
    _age(lock, STALE_LOCK_SECONDS + 60, monkeypatch, released)
    assert released._is_stale(lock) != ""


def test_this_helper_respects_a_lock_the_released_one_claims(
    tmp_path: Path, one_boot: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the other way round: busy while fresh, reclaimed when stale, nothing to recover."""
    lock = tmp_path / ".ha-installer.lock"
    released.claim_transaction(lock)

    with pytest.raises(FileExistsError):
        claim_transaction(lock, "0123456789ab")
    _age(lock, STALE_LOCK_SECONDS + 60, monkeypatch, installer_helper)
    assert claim_transaction(lock, "0123456789ab") == ""
    assert json.loads((lock / "owner.json").read_text())["id"] == "0123456789ab"


def test_the_claim_says_what_it_reclaimed_on_its_output(
    tmp_path: Path, one_boot: None
) -> None:
    """The installer reads the id from the claim's answer, so the answer is JSON."""
    lock = tmp_path / ".ha-installer.lock"

    result = subprocess.run(
        [sys.executable, str(HELPER), "claim", str(lock), "--id", "0123456789ab"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {"reclaimed": ""}


def test_a_zap_that_openwebif_refuses_is_not_a_zap(tmp_path: Path, socket_enabled: None) -> None:
    assert zap(WATCHED, "http://127.0.0.1:9") is False
    assert zap("bad\nref", "http://127.0.0.1:9") is False

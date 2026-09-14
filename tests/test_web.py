import argparse
import io
import json
import signal
import threading
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from wildcat3 import cli, web
from wildcat3.config import Config
from wildcat3.storage import Store
from wildcat3.web import FIELDS, ControlError, Controller, LocalServer


@pytest.fixture
def controller(tmp_path, monkeypatch):
    for key in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    return Controller(tmp_path)


def credentials(controller):
    controller.save({"KRAKEN_API_KEY": "example-api-key", "KRAKEN_API_SECRET": "example-private-key"})


def test_save_preserves_other_settings_and_hides_credentials(controller):
    path = controller.root / ".env"
    path.write_text("# Local settings\nWILDCAT_PAPER_MAKER_FEE=0.003\nWILDCAT_LIVE_ENABLED=true\n")
    credentials(controller)
    controller.save({"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "200"})
    data = dotenv_values(path)
    assert data["WILDCAT_PAPER_MAKER_FEE"] == "0.003"
    assert data["WILDCAT_LIVE_ENABLED"] == "false"
    assert path.stat().st_mode & 0o777 == 0o600
    snapshot = controller.snapshot()
    assert snapshot["configured"]["KRAKEN_API_KEY"]
    assert "example-api-key" not in json.dumps(snapshot)
    assert "example-private-key" not in json.dumps(snapshot)
    assert controller.sanitize_payload({"time": 200, "nested": ["test-token"]}) == {
        "time": 200, "nested": ["[redacted]"]}
    controller.save({"KRAKEN_API_KEY": "", "KRAKEN_API_SECRET": ""})
    assert not controller.snapshot()["configured"]["KRAKEN_API_KEY"]


@pytest.mark.parametrize("changes", [
    {"KRAKEN_API_KEY": "only-one"}, {"WILDCAT_LIVE_ENABLED": "true"},
    {"KRAKEN_API_SECRET": "x\nWILDCAT_LIVE_ENABLED=true"},
    {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "not-a-number"},
    {"KRAKEN_API_KEY": 123}, {},
])
def test_settings_validation(controller, changes):
    with pytest.raises(ControlError):
        controller.save(changes)
    assert not (controller.root / ".env").exists()


def test_settings_do_not_follow_symlinks(controller, tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("do not edit")
    (controller.root / ".env").symlink_to(target)
    with pytest.raises(ControlError):
        credentials(controller)
    assert target.read_text() == "do not edit"


def test_terminal_runner_blocks_settings_and_second_runner(controller):
    store = Store(controller.root / "data/live.sqlite3")
    try:
        with pytest.raises(ControlError, match="Terminal"):
            credentials(controller)
        with pytest.raises(ControlError, match="Terminal"):
            controller.start("run")
    finally:
        store.close()


class Process:
    def __init__(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        self.stdout = io.StringIO("example-api-key example-private-key\nAccount checked\n")

    def wait(self):
        return 0


def test_live_requires_check_and_confirmation_and_keys_never_in_arguments(controller, monkeypatch):
    credentials(controller)
    monkeypatch.setattr("wildcat3.web.subprocess.Popen", Process)
    with pytest.raises(ControlError, match="Check the account"):
        controller.start("run", "live", True)
    controller.start("check")
    controller.monitor.join(timeout=2)
    assert not controller.busy
    assert controller.snapshot()["live_ready"]
    with pytest.raises(ControlError):
        controller.start("run", "live", False)
    controller.start("run", "live", True)
    controller.monitor.join(timeout=2)
    assert "--live" in controller.process.command
    assert "example-api-key" not in " ".join(controller.process.command)
    assert controller.process.kwargs["env"]["KRAKEN_API_KEY"] == "example-api-key"
    assert controller.process.kwargs["env"]["WILDCAT_LIVE_ENABLED"] == "true"
    assert "example-private-key" not in json.dumps(controller.snapshot())
    credentials(controller)
    assert not controller.snapshot()["live_ready"]


def test_failed_check_cannot_enable_live(controller, monkeypatch):
    credentials(controller)
    monkeypatch.setattr(Process, "wait", lambda self: 1)
    monkeypatch.setattr("wildcat3.web.subprocess.Popen", Process)
    controller.start("check")
    controller.monitor.join(timeout=2)
    assert not controller.snapshot()["live_ready"]
    assert controller.result.startswith("Operation failed")


def test_stop_signals_gracefully_and_prevents_settings_changes(controller):
    class Running:
        def poll(self):
            return None

        def send_signal(self, sig):
            self.signal = sig

    controller.process = Running()
    controller.busy = True
    with pytest.raises(ControlError):
        credentials(controller)
    with pytest.raises(ControlError):
        controller.start("doctor")
    controller.stop()
    assert controller.process.signal == signal.SIGTERM
    assert controller.busy and controller.stopping


def test_closed_controller_cannot_restart(controller):
    controller.close()
    with pytest.raises(ControlError):
        controller.start("doctor")
    with pytest.raises(ControlError):
        credentials(controller)


def test_telegram_chat_discovery_is_read_only(controller, monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, params):
            calls.append((url, params))
            return httpx.Response(200, json={"ok": True, "result": [
                {"message": {"chat": {"type": "private", "id": 123, "first_name": "Example"}}},
                {"message": {"chat": {"type": "group", "id": -456}}},
            ]})

    monkeypatch.setattr("wildcat3.web.httpx.Client", Client)
    assert controller.telegram_chats("123:abcdefghijk") == [{"id": "123", "name": "Example"}]
    assert calls[0][0].endswith("/getUpdates")
    assert "offset" not in calls[0][1]
    assert not (controller.root / ".env").exists()
    with pytest.raises(ControlError):
        controller.telegram_chats("123:../../sendMessage")


@pytest.fixture
def server(controller):
    with LocalServer(controller.root, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_http_auth_origin_host_and_secret_redaction(server):
    with httpx.Client(base_url=server.origin, trust_env=False) as client:
        assert client.get("/api/state").status_code == 403
        headers = {"X-Wildcat-Token": server.token}
        assert client.get("/api/state", headers=headers).status_code == 200
        assert client.get("/api/state", headers=headers | {"Host": "evil.example"}).status_code == 403
        payload = {"KRAKEN_API_KEY": "secret-api", "KRAKEN_API_SECRET": "secret-private"}
        assert client.post("/api/settings", headers=headers, json=payload).status_code == 403
        assert client.post("/api/settings", headers=headers | {"Origin": "https://evil.example"}, json=payload).status_code == 403
        headers["Origin"] = server.origin
        assert client.post("/api/settings", headers=headers, json=payload).status_code == 200
        response = client.get("/api/state", headers=headers)
        assert "secret-api" not in response.text and "secret-private" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert client.get("/.env", headers=headers).status_code == 404
        assert client.get("/").status_code == 200
        assert server.token not in client.get("/").text
        assert client.post("/api/start", headers=headers, json={"action": "run", "mode": "live", "confirmed": True}).status_code == 400
        assert client.post("/api/settings", headers=headers, json={"unknown": "x"}).status_code == 400


@pytest.mark.asyncio
async def test_check_account_does_not_submit_orders(tmp_path, monkeypatch):
    calls = []

    class Exchange:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            calls.append("close")

    class Engine:
        def __init__(self, *args):
            self.view = {}

        async def bootstrap(self):
            calls.append("bootstrap")

    monkeypatch.setattr(cli, "Kraken", Exchange)
    monkeypatch.setattr(cli, "Engine", Engine)
    config = Config(mode="live", db_path=Path(tmp_path / "live.sqlite3"), api_key="key", api_secret="secret")
    await cli.runner(argparse.Namespace(command="check-account"), config)
    assert calls == ["bootstrap", "close"]


def executable(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def test_electron_binary_prefers_the_recorded_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("WILDCAT_ELECTRON", raising=False)
    modules = tmp_path / "desktop/node_modules"
    assert web.electron_binary(tmp_path) is None
    shim = executable(modules / ".bin/electron")
    assert web.electron_binary(tmp_path) == shim
    runtime = executable(modules / "electron/dist/Electron.app/Contents/MacOS/Electron")
    (modules / "electron/path.txt").write_text("Electron.app/Contents/MacOS/Electron\n")
    assert web.electron_binary(tmp_path) == runtime
    override = executable(tmp_path / "chosen-electron")
    monkeypatch.setenv("WILDCAT_ELECTRON", str(override))
    assert web.electron_binary(tmp_path) == override
    (modules / "electron/path.txt").write_text("../../../../../etc/passwd\n")
    monkeypatch.delenv("WILDCAT_ELECTRON")
    assert web.electron_binary(tmp_path) == shim


def test_the_window_receives_the_session_path_and_no_credentials(controller, monkeypatch):
    monkeypatch.setenv("WILDCAT_ELECTRON", str(executable(controller.root / "fake-electron")))
    monkeypatch.setenv("KRAKEN_API_KEY", "example-api-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "example-token")
    monkeypatch.setattr("wildcat3.web.subprocess.Popen", Process)
    session = controller.root / "data/web-session.json"
    child = web.open_window(controller.root, session)
    assert child.command == [str(controller.root / "fake-electron"), str(controller.root / "desktop")]
    assert child.kwargs["env"]["WILDCAT_SESSION"] == str(session)
    assert child.kwargs["env"]["WILDCAT_ROOT"] == str(controller.root)
    assert not set(FIELDS) & set(child.kwargs["env"])
    assert "example-api-key" not in " ".join(child.command)


def test_desktop_mode_keeps_the_session_url_off_the_terminal(controller, monkeypatch, capsys):
    monkeypatch.setattr("wildcat3.web.subprocess.Popen", Process)
    monkeypatch.setenv("WILDCAT_ELECTRON", str(executable(controller.root / "fake-electron")))
    url = "http://127.0.0.1:8765/#example-session-token"
    assert web.present(controller.root, controller.root / "data/web-session.json", url, "desktop") is not None
    assert "example-session-token" not in capsys.readouterr().out


@pytest.mark.parametrize("window,opens", [("desktop", True), ("browser", True), ("none", False)])
def test_a_missing_window_falls_back_to_the_browser(controller, monkeypatch, capsys, window, opens):
    opened = []
    monkeypatch.setattr("wildcat3.web.electron_binary", lambda root: None)
    monkeypatch.setattr("wildcat3.web.webbrowser.open", opened.append)
    url = "http://127.0.0.1:8765/#example-session-token"
    assert web.present(controller.root, controller.root / "data/web-session.json", url, window) is None
    assert opened == ([url] if opens else [])
    output = capsys.readouterr().out
    assert url in output
    assert ("npm install --prefix desktop" in output) == (window == "desktop")


class Window:
    def wait(self):
        return 0


class Panel:
    def __init__(self, controller):
        self.controller = controller
        self.exiting = threading.Event()
        self.stopped = False

    def shutdown(self):
        self.stopped = True


def test_closing_the_window_never_stops_a_running_bot(controller, capsys):
    panel = Panel(controller)
    controller.busy, controller.operation = True, "run"
    web.watch_window(Window(), panel)
    assert not panel.stopped
    assert "keeps running" in capsys.readouterr().out
    controller.busy, controller.operation = False, ""
    web.watch_window(Window(), panel)
    assert panel.stopped
    panel.stopped = False
    panel.exiting.set()
    web.watch_window(Window(), panel)
    assert not panel.stopped


def test_serve_rejects_an_unknown_window_mode(tmp_path):
    with pytest.raises(ControlError):
        web.serve(tmp_path, 0, "popup")
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("argv,window", [
    (["web"], "desktop"), (["web", "--browser"], "browser"),
    (["web", "--no-browser"], "none"), (["web", "--no-window"], "none"),
])
def test_the_web_command_selects_the_window(monkeypatch, argv, window):
    chosen = []
    monkeypatch.setattr("wildcat3.web.serve", lambda root, port, mode: chosen.append((port, mode)))
    monkeypatch.setattr("sys.argv", ["wildcat3", *argv])
    cli.main()
    assert chosen == [(8765, window)]

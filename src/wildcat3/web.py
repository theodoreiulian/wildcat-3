"""Loopback-only control panel. Secrets never enter JSON responses or process arguments."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values, set_key

FIELDS = ("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class ControlError(ValueError):
    pass


@contextlib.contextmanager
def database_locks(root):
    """Do not replace keys while a terminal runner owns either ledger."""
    handles = []
    try:
        for mode in ("paper", "live"):
            handle = open(root / "data" / f"{mode}.sqlite3.lock", "a+")
            handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ControlError("A bot is already running in Terminal. Stop it there before continuing.") from None
        yield
    finally:
        for handle in handles:
            handle.close()


class Controller:
    def __init__(self, root: Path):
        self.root = root.resolve()
        (self.root / "data").mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.process = None
        self.busy = False
        self.stopping = False
        self.operation = ""
        self.mode = "paper"
        self.result = "Ready. Start paper trading or connect your Kraken account."
        self.logs = deque(maxlen=30)
        self.checked = None
        self.monitor = None
        self.closing = False

    def values(self):
        path = self.root / ".env"
        if path.is_symlink():
            raise ControlError("The settings file is a symbolic link. Use a regular local .env file.")
        # A local file is authoritative; inherited variables are only an import fallback.
        values = dotenv_values(path, interpolate=False) if path.exists() else {}
        return {key: values.get(key, os.environ.get(key, "")) or "" for key in FIELDS}

    def fingerprint(self):
        values = self.values()
        return hashlib.sha256(json.dumps([values[k] for k in FIELDS[:2]]).encode()).hexdigest()

    def sanitize(self, text):
        text = ANSI.sub("", str(text))
        for value in self.values().values():
            if value:
                text = text.replace(value, "[redacted]")
        return text

    def sanitize_payload(self, value):
        if isinstance(value, str):
            return self.sanitize(value)
        if isinstance(value, list):
            return [self.sanitize_payload(item) for item in value]
        if isinstance(value, dict):
            return {key: self.sanitize_payload(item) for key, item in value.items()}
        return value

    def save(self, changes):
        with self.lock:
            if self.busy or self.closing:
                raise ControlError("Stop the current operation before changing settings.")
            if not isinstance(changes, dict) or not changes or set(changes) - set(FIELDS):
                raise ControlError("Unknown or empty settings request.")
            for value in changes.values():
                if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
                    raise ControlError("Settings must be single-line text, at most 4096 characters.")
            values = self.values() | {k: v.strip() for k, v in changes.items()}
            if bool(values[FIELDS[0]]) != bool(values[FIELDS[1]]):
                raise ControlError("Enter both the Kraken API key and private key.")
            if bool(values[FIELDS[2]]) != bool(values[FIELDS[3]]):
                raise ControlError("Enter both the Telegram bot token and chat ID, or leave both empty.")
            if values[FIELDS[3]] and not re.fullmatch(r"-?\d+", values[FIELDS[3]]):
                raise ControlError("Telegram chat ID must be a number.")
            with database_locks(self.root):
                path = self.root / ".env"
                fd, name = tempfile.mkstemp(prefix=".env.", dir=self.root)
                os.close(fd)
                try:
                    if path.exists():
                        Path(name).write_text(path.read_text())
                    for key, value in values.items():
                        set_key(name, key, value, quote_mode="always")
                    # The browser enables live only for the explicitly started child process.
                    set_key(name, "WILDCAT_LIVE_ENABLED", "false")
                    os.chmod(name, 0o600)
                    with open(name, "rb") as handle:
                        os.fsync(handle.fileno())
                    os.replace(name, path)
                finally:
                    Path(name).unlink(missing_ok=True)
            self.checked = None
            self.result = "Settings saved on this computer."

    def telegram_chats(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"\d+:[A-Za-z0-9_-]{10,200}", token):
            raise ControlError("Paste the bot token from BotFather first.")
        try:
            # Read updates without an offset: do not acknowledge/consume another client's messages.
            with httpx.Client(timeout=12, trust_env=False) as client:
                response = client.get(f"https://api.telegram.org/bot{token}/getUpdates",
                                      params={"limit": 100, "timeout": 0})
                data = response.json()
            if response.status_code != 200 or not data.get("ok"):
                raise ControlError("Telegram could not read this bot's messages. Check its token and webhook settings.")
            chats = {}
            for update in data.get("result", []):
                chat = update.get("message", {}).get("chat", {})
                if chat.get("type") == "private" and isinstance(chat.get("id"), int):
                    chats[str(chat["id"])] = str(chat.get("first_name", "Private chat"))
            return [{"id": cid, "name": name} for cid, name in chats.items()]
        except (httpx.HTTPError, ValueError, TypeError):
            raise ControlError("Could not read Telegram chats. Check the token and your internet connection.") from None

    def snapshot(self):
        with self.lock:
            values = self.values()
            view, events = {}, []
            path = self.root / "data" / f"{self.mode}.sqlite3"
            if path.exists():
                try:
                    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                        row = db.execute("SELECT value FROM meta WHERE key='dashboard'").fetchone()
                        view = json.loads(row[0]) if row else {}
                        events = [dict(zip(("time", "level", "message"), r, strict=True)) for r in db.execute(
                            "SELECT time,level,message FROM events ORDER BY id DESC LIMIT 12")]
                except sqlite3.Error:
                    view = {"health": "Saved dashboard is temporarily unavailable."}
            ready = bool(self.checked and self.checked[0] == self.fingerprint()
                         and time.monotonic() - self.checked[1] < 900)
            payload = {"configured": {k: bool(v) for k, v in values.items()}, "busy": self.busy,
                       "stopping": self.stopping, "operation": self.operation, "mode": self.mode,
                       "result": self.result, "logs": list(self.logs), "live_ready": ready,
                       "view": view, "events": events, "now": time.time()}
            # Defense in depth for third-party messages accidentally containing a credential.
            return self.sanitize_payload(payload)

    def start(self, action, mode="paper", confirmed=False):
        with self.lock:
            if self.busy or self.closing:
                raise ControlError("An operation is already running. Stop it before starting another.")
            if action not in {"doctor", "check", "run"} or mode not in {"paper", "live"}:
                raise ControlError("Unknown operation or mode.")
            if action == "check":
                mode = "live"
            if mode == "live":
                values = self.values()
                if not all(values[k] for k in FIELDS[:2]):
                    raise ControlError("Save your Kraken API key and private key first.")
                if action == "run" and (confirmed is not True or not self.snapshot()["live_ready"]):
                    raise ControlError("Check the account, then explicitly confirm real-money trading.")
            with database_locks(self.root):
                pass
            command = [sys.executable, "-m", "wildcat3.cli"]
            if action == "doctor":
                command += ["doctor"]
            elif action == "check":
                command += ["check-account", "--live"]
            else:
                command += ["run", "--no-ui"] + (["--live"] if mode == "live" else [])
            env = os.environ.copy()
            env.update(self.values())
            env["WILDCAT_LIVE_ENABLED"] = "true" if mode == "live" else "false"
            env["PYTHONUNBUFFERED"] = "1"
            # Ignore inherited import overrides; use the installed project environment.
            env.pop("PYTHONPATH", None)
            self.process = subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                                            start_new_session=True)
            self.busy, self.stopping = True, False
            self.operation, self.mode = action, mode
            self.result = {"doctor": "Checking Kraken public markets…", "check": "Checking your account…",
                           "run": f"Starting {mode} trading…"}[action]
            self.logs.clear()
            if action == "check":
                self.checked = None
            self.monitor = threading.Thread(target=self._monitor, args=(self.process, action, self.fingerprint()),
                                            daemon=True)
            self.monitor.start()

    def _monitor(self, process, action, fingerprint):
        try:
            for line in process.stdout:
                with self.lock:
                    self.logs.append(self.sanitize(line.rstrip())[:2000])
            code = process.wait()
            with self.lock:
                stopped = self.stopping
                if action == "check" and code == 0 and not stopped:
                    self.checked = (fingerprint, time.monotonic())
                if stopped:
                    self.result = ("Stopped. Native protective orders remain on Kraken." if code == 0
                                   else "Process exited during stop. Check Kraken to confirm order state.")
                elif code:
                    self.result = "Operation failed. Read the details below before continuing."
                else:
                    self.result = {"check": "Account authenticated and reconciled. No orders placed.",
                                   "doctor": "Public market check complete. No orders placed.",
                                   "run": "Bot stopped."}[action]
                self.busy, self.stopping = False, False
        finally:
            process.stdout.close()

    def stop(self):
        with self.lock:
            if self.busy and not self.stopping:
                self.stopping = True
                self.checked = None
                self.result = "Stopping safely; waiting for reconciliation and entry cancellation…"
                if self.process.poll() is None:
                    # Same signal as terminal Ctrl-C. Never force-kill a trading request.
                    self.process.send_signal(signal.SIGTERM)

    def close(self):
        with self.lock:
            self.closing = True
            self.stop()
        if self.monitor:
            self.monitor.join()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, port=8765):
        self.controller = Controller(root)
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "WildCat3"

    def log_message(self, *_):
        pass  # Do not log credentials, launch tokens or request bodies.

    def respond(self, code, data, mime="application/json"):
        body = json.dumps(data).encode() if mime == "application/json" else data
        self.send_response(code)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.respond(403, {"error": "Invalid local host."})
            return False
        if self.path.startswith("/api/"):
            token = self.headers.get("X-Wildcat-Token", "")
            if not hmac.compare_digest(token.encode(), self.server.token.encode()):
                self.respond(403, {"error": "Session expired. Reopen the app using its launcher."})
                return False
        if self.command == "POST" and self.headers.get("Origin") != self.server.origin:
            self.respond(403, {"error": "Only the local control panel can change settings."})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == "/api/state":
            try:
                self.respond(200, self.server.controller.snapshot())
            except (ValueError, OSError):
                self.respond(500, {"error": "Cannot read local settings. Check file permissions."})
            return
        name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css",
                "/favicon.svg": "favicon.svg"}.get(self.path)
        if name:
            mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css",
                    "favicon.svg": "image/svg+xml"}[name]
            self.respond(200, files("wildcat3").joinpath("static", name).read_bytes(), mime)
        else:
            self.respond(404, {"error": "Not found."})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384 or self.headers.get("Content-Type") != "application/json":
                raise ControlError("Expected a small JSON request.")
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ControlError("Expected a JSON object.")
            controller = self.server.controller
            if self.path == "/api/settings":
                controller.save(body)
            elif self.path == "/api/start":
                controller.start(body.get("action"), body.get("mode", "paper"), body.get("confirmed", False))
            elif self.path == "/api/stop":
                controller.stop()
            elif self.path == "/api/telegram-chats":
                self.respond(200, {"chats": controller.telegram_chats(body.get("token"))})
                return
            else:
                self.respond(404, {"error": "Not found."})
                return
            self.respond(200, {"ok": True})
        except ControlError as exc:
            self.respond(400, {"error": str(exc)})
        except (ValueError, TypeError, OSError):
            self.respond(400, {"error": "Could not complete the request. Check the input and local file permissions."})


def serve(root: Path, port=8765, open_browser=True):
    root = root.resolve()
    (root / "data").mkdir(mode=0o700, exist_ok=True)
    # One web controller per project; trading itself retains its existing ledger lock.
    with open(root / "data" / "web.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                session = json.loads((root / "data/web-session.json").read_text())
                origin = f"http://127.0.0.1:{int(session['port'])}"
                with httpx.Client(timeout=3, trust_env=False) as client:
                    response = client.get(origin + "/api/state", headers={"X-Wildcat-Token": session["token"]})
                if response.status_code != 200:
                    raise ValueError("Expired session")
                url = origin + "/#" + session["token"]
                if open_browser:
                    webbrowser.open(url)
                else:
                    print(f"WildCat-3 is already open: {url}", flush=True)
                return
            except (ValueError, KeyError, OSError, httpx.HTTPError):
                raise ControlError("The control panel is already starting. Try the launcher again in a moment.") from None
        with LocalServer(root, port) as server:
            url = f"{server.origin}/#{server.token}"
            session_path = root / "data/web-session.json"
            fd = os.open(session_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"port": server.server_port, "token": server.token}, handle)
            print(f"WildCat-3 control panel: {url}", flush=True)
            print("Keep this window open. Ctrl-C stops the bot safely and closes the panel.", flush=True)
            if open_browser:
                webbrowser.open(url)
            def interrupt(*_):
                raise KeyboardInterrupt

            previous = signal.signal(signal.SIGTERM, interrupt)
            try:
                server.serve_forever(poll_interval=0.3)
            except KeyboardInterrupt:
                print("Stopping the bot safely…", flush=True)
            finally:
                signal.signal(signal.SIGTERM, previous)
                server.controller.close()
                session_path.unlink(missing_ok=True)

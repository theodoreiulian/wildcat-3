"""Durable intents, idempotent fills, event outbox, and a single-host process lock."""
import fcntl
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


def encode(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path: Path):
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._lock = open(str(path) + ".lock", "a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise RuntimeError("Another WildCat-3 process holds this database") from None
        self.db = sqlite3.connect(path, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents(
                userref INTEGER PRIMARY KEY, created REAL NOT NULL, symbol TEXT NOT NULL,
                quantity TEXT NOT NULL, price TEXT NOT NULL, trail TEXT NOT NULL,
                status TEXT NOT NULL, txid TEXT, detail TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS orders(txid TEXT PRIMARY KEY, raw TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills(txid TEXT PRIMARY KEY, raw TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, time REAL NOT NULL, level TEXT NOT NULL,
                message TEXT NOT NULL, notify INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS equity(time REAL PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(intents)")}
        if "kind" not in columns:
            self.db.execute("ALTER TABLE intents ADD COLUMN kind TEXT NOT NULL DEFAULT 'entry'")

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, encode(value)))

    def next_nonce(self) -> int:
        # Transaction + process lock prevent rollback or overlapping requests on this DB.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            value = max(time.time_ns() // 1000, int(self.get("nonce", 0)) + 1)
            self.set("nonce", value)
            self.db.execute("COMMIT")
            return value
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def intent(self, symbol, quantity, price, trail, created=None, kind="entry") -> int:
        if kind not in {"entry", "protection"}:
            raise ValueError("Unknown intent kind")
        while True:
            ref = secrets.randbelow(2**31 - 1) + 1
            if not self.db.execute("SELECT 1 FROM intents WHERE userref=?", (ref,)).fetchone():
                break
        self.db.execute("INSERT INTO intents(userref,created,symbol,quantity,price,trail,status,kind) "
                        "VALUES (?,?,?,?,?,?,'SUBMITTING',?)",
                        (ref, created or time.time(), symbol, str(quantity), str(price), str(trail), kind))
        return ref

    def update_intent(self, ref: int, status: str, txid: str | None = None, detail: str = ""):
        self.db.execute("UPDATE intents SET status=?,txid=COALESCE(?,txid),detail=? WHERE userref=?",
                        (status, txid, detail, ref))

    def intents(self) -> list[dict]:
        return [dict(x) for x in self.db.execute("SELECT * FROM intents ORDER BY created")]

    def upsert_orders(self, orders: dict):
        with self.transaction():
            self.db.executemany("INSERT INTO orders VALUES (?,?) ON CONFLICT(txid) DO UPDATE SET raw=excluded.raw",
                                [(k, encode(v)) for k, v in orders.items()])

    def orders(self) -> dict:
        return {x[0]: json.loads(x[1]) for x in self.db.execute("SELECT txid,raw FROM orders")}

    def record_fill(self, txid: str, raw: dict, message: str) -> bool:
        with self.transaction():
            result = self.db.execute("INSERT OR IGNORE INTO fills VALUES (?,?)", (txid, encode(raw)))
            if result.rowcount:
                self.event("FILL", message, notify=True)
                return True
        return False

    def fills(self) -> dict:
        return {x[0]: json.loads(x[1]) for x in self.db.execute("SELECT txid,raw FROM fills")}

    def event(self, level: str, message: str, notify: bool = False):
        self.db.execute("INSERT INTO events(time,level,message,notify) VALUES (?,?,?,?)",
                        (time.time(), level, message, int(notify)))

    def events(self, limit: int = 12) -> list[dict]:
        return [dict(x) for x in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def notifications(self) -> list[dict]:
        return [dict(x) for x in self.db.execute(
            "SELECT * FROM events WHERE notify=1 AND sent=0 AND next_try<=? ORDER BY id LIMIT 20", (time.time(),))]

    def notification_result(self, event_id: int, success: bool):
        if success:
            self.db.execute("UPDATE events SET sent=1 WHERE id=?", (event_id,))
        else:
            row = self.db.execute("SELECT attempts FROM events WHERE id=?", (event_id,)).fetchone()
            delay = min(3600, 2 ** min(int(row[0]) + 2, 12)) if row else 4
            self.db.execute("UPDATE events SET attempts=attempts+1,next_try=? WHERE id=?",
                            (time.time() + delay, event_id))

    def add_equity(self, value, now=None):
        self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?)", (now or time.time(), str(value)))

    def equity_history(self, limit: int | None = None) -> list[tuple[float, float]]:
        if limit is None:
            rows = self.db.execute("SELECT time,value FROM equity ORDER BY time")
        else:
            rows = reversed(list(self.db.execute(
                "SELECT time,value FROM equity ORDER BY time DESC LIMIT ?", (max(1, int(limit)),))))
        return [(r[0], float(r[1])) for r in rows]

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def txn():
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return txn()

    def close(self):
        self.db.close()
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()

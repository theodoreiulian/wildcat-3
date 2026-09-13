import argparse
import asyncio
import csv
import json
import signal
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live

from .config import Config
from .dashboard import render
from .engine import Engine, SafetyError
from .exchange import Kraken
from .models import UNIVERSE, D, Signal, floor_step
from .notify import Notifier
from .paper import PaperExchange
from .risk import size_entry
from .storage import Store


async def doctor():
    exchange = Kraken()
    try:
        status = await exchange.status()
        markets = await exchange.markets()
        console = Console()
        console.print(f"Kraken public API: {status}; France / international EUR market metadata")
        for s in UNIVERSE:
            if s not in markets:
                console.print(f"{s}: unavailable for this universe")
                continue
            m = markets[s]
            book = await exchange.book(m)
            console.print(f"{s}: {m.status}, spread {book.spread_bps:.2f} bps, "
                          f"minimum {m.min_qty} tokens / €{m.min_cost}; price tick {m.price_tick}")
        console.print("Public checks only. No credentials used and no orders submitted.")
    finally:
        await exchange.close()


def _read_db(path: Path):
    if not path.exists():
        raise ValueError(f"No saved state at {path}. Start the runner first.")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def status(path):
    with _read_db(path) as db:
        row = db.execute("SELECT value FROM meta WHERE key='dashboard'").fetchone()
        if not row:
            raise ValueError("No dashboard snapshot available yet")
        events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 8")]
        health = db.execute("SELECT value FROM meta WHERE key='notification_health'").fetchone()
        Console().print(render(json.loads(row[0]), events, json.loads(health[0]) if health else ""))


def export(path, output):
    with _read_db(path) as db:
        fills = [(r[0], json.loads(r[1])) for r in db.execute("SELECT txid,raw FROM fills")]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["trade_id", "time", "symbol", "type", "vol", "price", "cost", "fee", "ordertxid"])
        writer.writeheader()
        for tid, fill in sorted(fills, key=lambda r: (float(r[1]["time"]), r[0])):
            writer.writerow({"trade_id": tid, **{k: fill.get(k, "") for k in writer.fieldnames if k != "trade_id"}})
    print(f"Exported {len(fills)} executions to {output}")


async def runner(args, config):
    store = Store(config.db_path)
    api = Kraken(config.api_key, config.api_secret, nonce=store.next_nonce)
    exchange = api if config.mode == "live" else PaperExchange(api, store, config.maker_fee, config.taker_fee, config.slippage_bps)
    engine = Engine(config, store, exchange)
    notifier = Notifier(store, config.telegram_token, config.telegram_chat)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    notify_task = None
    try:
        if args.command == "preflight":
            await engine.bootstrap()
            if store.get("halt"):
                raise SafetyError(store.get("halt"))
            if engine.account.positions or engine.open:
                raise SafetyError("Preflight sample validation requires no exposure or open orders")
            validated = False
            for s in UNIVERSE:
                if s not in engine.markets:
                    continue
                m = engine.markets[s]
                b = await exchange.book(m)
                sig = Signal(s, floor_step(b.bid, m.price_tick), D("3"), 0, 0, "validation only", 0)
                qty = size_entry(config, m, sig, b, engine.account.cash, D(engine.view["available"]), *engine.fee_map[s])
                if qty <= 0:
                    continue
                # Validation has no persisted trading intent: matching engine is never entered.
                await exchange.add_entry(m, qty, sig.limit_price, sig.trail_pct, 1, int(time.time()) + config.entry_ttl, validate=True)
                print(f"Validated {s} native OTO payload for {qty} tokens. No order submitted.")
                validated = True
                break
            if not validated:
                raise SafetyError("No risk-sized sample meets current venue/depth constraints")
            print("Account reconciliation passed. This does not verify fills or conditional-child behavior.")
            return
        notify_task = asyncio.create_task(notifier.run())
        if args.no_ui or args.once:
            await engine.step()
            Console().print(render(engine.view, store.events(), store.get("notification_health", "")))
            while not args.once and not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=config.poll_seconds)
                except TimeoutError:
                    await engine.step()
        else:
            with Live(render(engine.view, store.events()), refresh_per_second=2, screen=True) as live:
                while not stop.is_set():
                    work = asyncio.create_task(engine.step())
                    while not work.done():
                        live.update(render(engine.view, store.events(), store.get("notification_health", "")))
                        await asyncio.wait({work}, timeout=0.5)
                    await work
                    live.update(render(engine.view, store.events(), store.get("notification_health", "")))
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=config.poll_seconds)
                    except TimeoutError:
                        pass
        await engine.shutdown()
        # Allow a bounded final outbox drain; unavailable Telegram never traps shutdown.
        notifier.stopped.set()
        if notify_task:
            notify_task.cancel()
            await asyncio.gather(notify_task, return_exceptions=True)
            notify_task = None
        try:
            await asyncio.wait_for(notifier.drain(), timeout=10)
        except TimeoutError:
            pass
    finally:
        if notify_task:
            notify_task.cancel()
            await asyncio.gather(notify_task, return_exceptions=True)
        await notifier.close()
        await exchange.close()
        store.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description="WildCat-3 · decentralized AI · Kraken EUR Spot")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="Public API and eligibility check; no private calls")
    for command in ("run", "preflight", "status", "export", "resume", "resolve-unknown"):
        p = subs.add_parser(command)
        p.add_argument("--live", action="store_true", help="Use real-money mode/database")
        p.add_argument("--db", help="Explicit SQLite database path; never share one between modes")
        if command == "run":
            p.add_argument("--once", action="store_true")
            p.add_argument("--no-ui", action="store_true")
        elif command == "export":
            p.add_argument("--output", default="data/executions.csv")
        elif command == "resolve-unknown":
            p.add_argument("--ref", type=int, required=True)
            p.add_argument("--confirmed-not-accepted", action="store_true", required=True,
                           help="Record your authoritative verification with Kraken that the order was never accepted")
            p.add_argument("--reason", required=True, help="Audit note/source for the verification")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            asyncio.run(doctor())
            return
        mode = "live" if args.live else "paper"
        path = Path(args.db or f"data/{mode}.sqlite3")
        if args.command == "status":
            status(path)
        elif args.command == "export":
            export(path, args.output)
        elif args.command in {"resume", "resolve-unknown"}:
            if not path.exists():
                raise ValueError("Database does not exist")
            store = Store(path)
            try:
                if store.get("mode") != mode:
                    raise ValueError("Mode and database differ")
                if args.command == "resume":
                    store.set("halt", None)
                    store.event("OPERATOR", "Operator cleared entry halt; next boot must pass all audits", True)
                    print("Halt cleared. Risk breakers, reconciliation and unresolved intents still apply.")
                else:
                    intent = next((i for i in store.intents() if i["userref"] == args.ref), None)
                    if not intent or intent["status"] not in {"SUBMITTING", "UNCERTAIN"}:
                        raise ValueError("Intent is not unresolved")
                    if time.time() - intent["created"] < 300:
                        raise ValueError("Wait beyond the entry expiry and exchange propagation before resolving")
                    store.update_intent(args.ref, "REJECTED", detail="Operator verified: " + args.reason)
                    store.event("OPERATOR", f"Operator verified intent {args.ref} never accepted: {args.reason}", True)
                    print("Resolution recorded. The next runner boot must reconcile before any new entry.")
            finally:
                store.close()
        else:
            config = Config.from_env(mode, args.db)
            asyncio.run(runner(args, config))
    except (ValueError, RuntimeError, OSError) as exc:
        # Adapter errors are already sanitized. Never print network request objects.
        print(f"WildCat-3: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

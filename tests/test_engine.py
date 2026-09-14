import asyncio
import time
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D

import pytest

from wildcat3.config import Config
from wildcat3.engine import Engine, SafetyError
from wildcat3.exchange import AmbiguousOrderError, KrakenError
from wildcat3.models import Book, Candle, Market, Signal
from wildcat3.paper import PaperExchange
from wildcat3.storage import Store


class Public:
    def __init__(self):
        self.market = Market("TAO/EUR", "TAOEUR", "TAO", "ZEUR", D(".0001"), D(".00001"), D(".02"), D(".45"))
        self.bid, self.ask = D("200"), D("200.1")
        self.fail_status = False

    async def markets(self):
        return {"TAO/EUR": self.market, "BTC/EUR": replace(self.market, symbol="BTC/EUR", pair="XBTEUR", base="XXBT")}

    async def status(self):
        if self.fail_status:
            raise KrakenError("public unavailable")
        return "online"

    async def book(self, market):
        return Book(market.symbol, time.time(), ((self.bid, D("1000")),), ((self.ask, D("1000")),))

    async def candles(self, market, interval=60, since=None):
        return []

    async def close(self):
        pass


@pytest.fixture
async def fixture(tmp_path):
    store = Store(tmp_path / "paper.sqlite3")
    public = Public()
    exchange = PaperExchange(public, store)
    config = Config(db_path=store.path)
    engine = Engine(config, store, exchange)
    await engine.bootstrap()
    yield store, public, exchange, engine
    store.close()


def signal():
    return Signal("TAO/EUR", D("200"), D("3"), 1, 300, "test opportunity", int(time.time()) - 3600)


async def filled_position(fixture, qty=D(".1")):
    store, public, exchange, engine = fixture
    await engine.enter(signal(), qty)
    public.bid, public.ask = D("199.8"), D("199.9")
    await exchange.advance({"TAO/EUR": await public.book(public.market)})
    await engine.reconcile()
    return next(k for k, o in exchange.state["orders"].items() if o["descr"]["type"] == "sell")


async def test_held_asset_publishes_live_quote_and_bounded_price_history(fixture):
    _, public, _, engine = fixture
    await filled_position(fixture)
    await engine.refresh_market_data()
    engine.candle_cache["TAO/EUR"] = [
        Candle(index * 3600, 190, 225, 185, 190 + index, 10) for index in range(30)
    ]
    engine.mark_and_breakers()
    position = engine.view["positions"][0]
    assert position["price"] == "199.85"
    assert position["change_24h"] > 0
    assert len(engine.view["price_history"]["TAO/EUR"]) == 30
    assert engine.view["equity_history"]


async def test_failed_setup_does_not_bind_mistyped_key(tmp_path):
    store = Store(tmp_path / "live.sqlite3")
    public = Public()
    exchange = PaperExchange(public, store)

    async def failed_auth():
        raise KrakenError("Invalid key")

    exchange.balance_ex = failed_auth
    try:
        for key in ("mistyped-key", "corrected-key"):
            engine = Engine(Config(mode="live", db_path=store.path, api_key=key, api_secret="secret"), store, exchange)
            with pytest.raises(KrakenError, match="Invalid key"):
                await engine.bootstrap()
        assert store.get("inception") is None
        assert store.get("account_key") is None
    finally:
        store.close()


async def test_initialized_ledger_still_rejects_different_key(fixture):
    store, _, exchange, _ = fixture
    store.set("mode", "live")
    engine = Engine(Config(mode="live", db_path=store.path, api_key="different", api_secret="secret"), store, exchange)
    with pytest.raises(SafetyError, match="different API key"):
        await engine.bootstrap()


async def test_actual_ambiguous_subclass_stays_uncertain(fixture):
    store, _, exchange, engine = fixture
    async def ambiguous(*args, **kwargs):
        raise AmbiguousOrderError("lost acknowledgment")
    exchange.add_entry = ambiguous
    await engine.enter(signal(), D(".1"))
    assert store.intents()[0]["status"] == "UNCERTAIN"
    await engine.step()
    assert len(store.intents()) == 1
    assert engine.view["status"] == "UNCERTAIN"


async def test_definite_rejection_is_not_unknown(fixture):
    store, _, exchange, engine = fixture
    async def rejected(*args, **kwargs):
        raise KrakenError("EOrder:Insufficient funds")
    exchange.add_entry = rejected
    await engine.enter(signal(), D(".1"))
    assert store.intents()[0]["status"] == "REJECTED"


async def test_cancellation_during_submission_is_durable(fixture):
    store, _, exchange, engine = fixture
    async def canceled(*args, **kwargs):
        raise asyncio.CancelledError
    exchange.add_entry = canceled
    with pytest.raises(asyncio.CancelledError):
        await engine.enter(signal(), D(".1"))
    assert store.intents()[0]["status"] == "UNCERTAIN"


async def test_restart_recovers_accepted_order_after_lost_ack(fixture):
    store, _, exchange, engine = fixture
    real = exchange.add_entry
    async def accept_then_timeout(*args, **kwargs):
        await real(*args, **kwargs)
        raise AmbiguousOrderError("timeout")
    exchange.add_entry = accept_then_timeout
    await engine.enter(signal(), D(".1"))
    assert store.intents()[0]["status"] == "UNCERTAIN"
    resumed = Engine(engine.config, store, exchange)
    await resumed.bootstrap()
    assert store.intents()[0]["status"] == "ACKNOWLEDGED"
    assert len(await exchange.open_orders()) == 1
    assert len(store.intents()) == 1


async def test_missing_stop_still_records_fill_exposure_and_notification(fixture):
    store, _, exchange, engine = fixture
    child = await filled_position(fixture)
    # Recreate the exact crash path: no local fills yet, child rejected on venue.
    store.db.execute("DELETE FROM fills")
    store.db.execute("DELETE FROM events WHERE level='FILL'")
    exchange.state["orders"][child]["status"] = "canceled"
    exchange.save()
    with pytest.raises(SafetyError, match="coverage"):
        await engine.reconcile()
    assert len(store.fills()) == 1
    assert engine.account.positions["TAO/EUR"].quantity == D(".1")
    assert engine.view["positions"][0]["quantity"] == "0.1"
    assert engine.view["equity"] is None
    assert any(e["level"] == "FILL" for e in store.notifications())


@pytest.mark.parametrize("change", ["wide", "future", "expiry", "margin", "absent_stop", "low_stop", "quantity"])
async def test_ineffective_protection_rejected(fixture, change):
    _, _, exchange, engine = fixture
    child = await filled_position(fixture)
    orders = deepcopy(await exchange.open_orders())
    o = orders[child]
    if change == "wide":
        o["descr"]["price"] = "+99%"
    elif change == "future":
        o["starttm"] = time.time() + 1000
    elif change == "expiry":
        o["expiretm"] = time.time() + 1000
    elif change == "margin":
        o["margin"] = True
    elif change in {"absent_stop", "low_stop"}:
        o["descr"]["price"] = "0"
        o["stopprice"] = "0" if change == "absent_stop" else "1"
    else:
        o["vol"] = ".099"
    with pytest.raises(SafetyError):
        engine.audit_orders(engine.account, orders, engine.known)


async def test_documented_absolute_stopprice_verifies_bound(fixture):
    _, _, exchange, engine = fixture
    child = await filled_position(fixture)
    orders = deepcopy(await exchange.open_orders())
    orders[child]["descr"]["price"] = "0"
    orders[child]["stopprice"] = "194"
    engine.audit_orders(engine.account, orders, engine.known)


async def test_cancel_entries_retains_native_protective_children(fixture):
    _, _, exchange, engine = fixture
    child = await filled_position(fixture)
    await engine.cancel_entries()
    assert child in await exchange.open_orders()
    assert (await exchange.open_orders())[child]["status"] == "open"


async def test_native_stop_executed_offline_reconciles_once(fixture):
    store, public, exchange, engine = fixture
    await filled_position(fixture)
    public.bid, public.ask = D("190"), D("190.1")
    # Venue simulation advances independently of Engine (equivalent to a downtime fill).
    await exchange.advance({"TAO/EUR": await public.book(public.market)})
    restarted = Engine(engine.config, store, exchange)
    await restarted.bootstrap()
    assert not restarted.account.positions
    assert restarted.account.losses == 1
    assert restarted.account.realized < 0
    await restarted.reconcile()
    assert len(store.fills()) == 2
    assert len([e for e in store.events(100) if e["level"] == "FILL"]) == 2


async def test_public_failure_does_not_skip_private_fills(fixture):
    store, public, exchange, engine = fixture
    await filled_position(fixture)
    public.bid, public.ask = D("190"), D("190.1")
    await exchange.advance({"TAO/EUR": await public.book(public.market)})
    public.fail_status = True
    await engine.step()
    assert len(store.fills()) == 2
    assert not engine.account.positions
    assert engine.view["status"] == "DEGRADED"


async def test_emergency_stop_one_shot_and_halt_retained(fixture):
    store, _, exchange, engine = fixture
    child = await filled_position(fixture)
    exchange.state["orders"][child]["status"] = "canceled"
    exchange.save()
    await engine.step()
    repairs = [i for i in store.intents() if i["kind"] == "protection"]
    assert len(repairs) == 1
    assert repairs[0]["status"] == "ACKNOWLEDGED"
    assert store.get("halt")
    await engine.step()
    assert len([i for i in store.intents() if i["kind"] == "protection"]) == 1
    assert engine.view["status"] == "HALTED"
    assert len(await exchange.open_orders()) == 1


async def test_unknown_repair_is_never_retried(fixture):
    store, _, exchange, engine = fixture
    child = await filled_position(fixture)
    exchange.state["orders"][child]["status"] = "canceled"
    attempts = []
    async def ambiguous(*args, **kwargs):
        attempts.append(args)
        raise AmbiguousOrderError("uncertain repair")
    exchange.add_protection = ambiguous
    await engine.step()
    await engine.step()
    assert len(attempts) == 1
    assert store.intents()[-1]["status"] == "UNCERTAIN"


async def test_no_repair_while_parent_can_still_fill(fixture):
    store, public, exchange, engine = fixture
    await engine.enter(signal(), D(".1"))
    public.bid, public.ask = D("199.8"), D("199.9")
    b = Book("TAO/EUR", time.time(), ((public.bid, D("100")),), ((public.ask, D(".5")),))
    await exchange.advance({"TAO/EUR": b})  # .05 fill, .05 pending
    child = next(k for k, o in exchange.state["orders"].items() if o["descr"]["type"] == "sell")
    exchange.state["orders"][child]["status"] = "canceled"
    await engine.step()  # cancels remaining parent, but its prior snapshot remains open
    assert not [i for i in store.intents() if i["kind"] == "protection"]
    await engine.step()  # authoritative terminal parent permits one repair
    assert len([i for i in store.intents() if i["kind"] == "protection"]) == 1


async def test_external_balance_movement_halts_without_order(fixture):
    store, _, exchange, engine = fixture
    exchange.state["balances"]["ZEUR"] = "201"
    await engine.step()
    assert engine.view["status"] == "HALTED"
    assert "EUR balance" in store.get("halt")
    assert not store.intents()


async def test_unresolved_reference_is_preserved_despite_empty_history(fixture):
    store, _, _, engine = fixture
    ref = store.intent("TAO/EUR", D(".1"), D("200"), D("3"), created=time.time() - 900)
    await engine.reconcile()
    assert next(i for i in store.intents() if i["userref"] == ref)["status"] == "UNCERTAIN"


async def test_paper_gap_is_persistently_flagged(fixture):
    store, public, exchange, _ = fixture
    await filled_position(fixture)
    exchange.state["observed_at"] = time.time() - 9 * 3600
    await exchange.advance({"TAO/EUR": await public.book(public.market)})
    assert store.get("paper_exchange")["observation_gaps"] == 1
    assert any("observation gap" in e["message"] for e in store.events())


async def test_mode_database_mixing_refused(fixture):
    store, _, exchange, _ = fixture
    config = Config(mode="live", api_key="x", api_secret="x", db_path=store.path)
    with pytest.raises(SafetyError, match="share"):
        await Engine(config, store, exchange).bootstrap()


async def test_history_cursor_uses_sweep_start_not_completion(fixture, monkeypatch):
    import wildcat3.engine as engine_module
    store, _, exchange, engine = fixture
    clock = [time.time()]
    before = clock[0]
    original = exchange.trades
    async def delayed(start):
        result = await original(start)
        clock[0] += 1000
        return result
    exchange.trades = delayed
    monkeypatch.setattr(engine_module.time, "time", lambda: clock[0])
    await engine.reconcile()
    assert store.get("history_cursor") <= before


async def test_unresolved_intent_keeps_its_original_history_window(fixture):
    store, _, exchange, engine = fixture
    now = time.time()
    store.set("inception", now - 20000)
    store.set("history_cursor", now)
    ref = store.intent("TAO/EUR", D(".1"), D("200"), D("3"), created=now - 10000)
    calls = []
    original = exchange.closed_orders
    async def capture(start):
        calls.append(start)
        return await original(start)
    exchange.closed_orders = capture
    await engine.reconcile()
    assert calls and calls[0] < now - 10000
    assert next(i for i in store.intents() if i["userref"] == ref)["status"] == "UNCERTAIN"


async def test_closed_child_between_history_reads_is_hydrated(fixture):
    store, public, exchange, engine = fixture
    child = await filled_position(fixture)
    # Remove saved child metadata, then simulate a child absent from the earlier
    # ClosedOrders page but present in the later TradesHistory response.
    store.db.execute("DELETE FROM orders WHERE txid=?", (child,))
    public.bid, public.ask = D("190"), D("190.1")
    await exchange.advance({"TAO/EUR": await public.book(public.market)})
    original = exchange.closed_orders
    async def earlier_page(start):
        return {k: o for k, o in (await original(start)).items() if k != child}
    exchange.closed_orders = earlier_page
    await engine.reconcile()
    assert not engine.account.positions
    assert len(store.fills()) == 2
    assert child in store.orders()


async def test_base_fee_preference_is_not_treated_as_verified_protection(fixture):
    _, _, exchange, engine = fixture
    child = await filled_position(fixture)
    opened = deepcopy(await exchange.open_orders())
    opened[child]["oflags"] = "fcib"
    with pytest.raises(SafetyError, match="fee currency"):
        engine.audit_orders(engine.account, opened, engine.known)


async def test_pending_entry_cannot_exceed_durable_quantity(fixture):
    _, _, exchange, engine = fixture
    await engine.enter(signal(), D(".1"))
    await engine.reconcile()
    opened = deepcopy(await exchange.open_orders())
    next(iter(opened.values()))["vol"] = "100"
    with pytest.raises(SafetyError, match="quantity or limit"):
        engine.audit_orders(engine.account, opened, engine.known)

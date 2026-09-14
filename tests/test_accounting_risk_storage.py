from decimal import Decimal as D

import pytest

from wildcat3.accounting import AccountingError, ledger
from wildcat3.config import Config
from wildcat3.models import Book, Market, Signal
from wildcat3.risk import size_entry
from wildcat3.storage import Store


def fill(side, qty, cost, fee, at=1):
    return {"symbol": "TAO/EUR", "type": side, "vol": qty, "cost": cost, "fee": fee, "time": at}


def test_fee_inclusive_partial_exits_and_round_trip_statistics():
    fills = {
        "a": fill("buy", "1", "100", ".4"),
        "b": fill("sell", ".4", "44", ".352", 2),
        "c": fill("sell", ".6", "54", ".432", 3),
    }
    result = ledger(fills)
    assert result.realized == D("-3.184")
    assert result.cash == D("196.816")
    assert result.fees == D("1.184")
    assert result.completed == 1 and result.losses == 1 and result.wins == 0
    assert not result.positions
    assert result.exit_pnl["b"][0] > 0  # partial win must not become a separate round-trip win


def test_equal_timestamp_acquisition_precedes_lexically_earlier_sell():
    result = ledger({"Z-buy": fill("buy", "1", "10", ".04"),
                     "A-sell": fill("sell", "1", "11", ".088")})
    assert result.completed == 1
    assert result.realized == D(".872")


@pytest.mark.parametrize("fills", [
    {"x": fill("sell", "1", "10", ".1")},
    {"x": fill("buy", "NaN", "10", ".1")},
    {"x": fill("buy", "1", "10", "-1")},
])
def test_invalid_accounting_fails_closed(fills):
    with pytest.raises(AccountingError):
        ledger(fills)


def test_sizing_respects_total_loss_budget_not_just_stop_distance():
    m = Market("TAO/EUR", "TAOEUR", "TAO", "ZEUR", D(".0001"), D(".00001"), D(".02"), D(".45"))
    b = Book(m.symbol, 1, ((D("200"), D("1000")),), ((D("200.1"), D("1000")),))
    s = Signal(m.symbol, D("200"), D("3"), 1, 300, "fixture", 0)
    qty = size_entry(Config(), m, s, b, D("200"), D("200"), D(".004"), D(".008"))
    risk = qty * s.limit_price * (D(".03") + D(".012") + D(".003") + b.spread_bps / 10000)
    assert D(".9") < risk <= D("1")
    assert qty * s.limit_price <= D("60")
    assert qty % m.qty_step == 0
    assert size_entry(Config(), m, s, b, D("1"), D("1"), D(".004"), D(".008")) == 0


def test_store_lock_and_nonce_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    a = Store(path)
    nonce = a.next_nonce()
    with pytest.raises(RuntimeError, match="Another"):
        Store(path)
    a.close()
    b = Store(path)
    assert b.next_nonce() > nonce
    b.close()


def test_equity_history_can_be_bounded(tmp_path):
    store = Store(tmp_path / "equity.sqlite3")
    try:
        for index in range(5):
            store.add_equity(D(200 + index), now=index + 1)
        assert store.equity_history(3) == [(3, 202.0), (4, 203.0), (5, 204.0)]
        assert len(store.equity_history()) == 5
    finally:
        store.close()


def test_fill_and_outbox_are_idempotent_and_transactional(tmp_path):
    store = Store(tmp_path / "db")
    try:
        assert store.record_fill("trade", fill("buy", "1", "10", ".1"), "buy")
        assert not store.record_fill("trade", fill("buy", "1", "10", ".1"), "buy")
        assert len(store.fills()) == 1 and len(store.notifications()) == 1
        original = store.event
        def fail(*args, **kwargs):
            raise RuntimeError("outbox write failed")
        store.event = fail
        with pytest.raises(RuntimeError):
            store.record_fill("other", fill("buy", "1", "10", ".1"), "buy")
        store.event = original
        assert "other" not in store.fills()
    finally:
        store.close()


def test_live_requires_two_explicit_configuration_steps(monkeypatch):
    monkeypatch.delenv("WILDCAT_LIVE_ENABLED", raising=False)
    with pytest.raises(ValueError, match="WILDCAT_LIVE_ENABLED"):
        Config.from_env("live")
    monkeypatch.setenv("WILDCAT_LIVE_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    with pytest.raises(ValueError, match="requires"):
        Config.from_env("live")
    monkeypatch.setenv("KRAKEN_API_KEY", "secret-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "secret-value")
    paper = Config.from_env("paper")
    assert paper.api_key == paper.api_secret == ""

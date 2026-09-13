from dataclasses import replace
from decimal import Decimal as D

import pytest

from wildcat3.models import Book, Candle, Market
from wildcat3.strategy import ALLOWLIST, StrategyConfig, evaluate


def fixture_data():
    candles = []
    price = 100.0
    for i in range(200):
        previous = price
        price *= 1.0015
        candles.append(
            Candle(i * 3600, previous, price * 1.002, previous * 0.994, price, 4000 if i == 199 else 2000)
        )
    btc = [Candle(i * 3600, 100, 100.2, 99.8, 100, 10000) for i in range(200)]
    now = 200 * 3600 + 10
    market = Market("TAO/EUR", "TAOEUR", "TAO", "ZEUR", D(".01"), D(".00000001"), D(".02"), D(".45"))
    book = Book("TAO/EUR", now, ((D(str(price * 0.9995)), D(100)),), ((D(str(price * 1.0005)), D(100)),))
    return {"TAO/EUR": market}, {"TAO/EUR": candles}, {"TAO/EUR": book}, btc, now


def run(parts, **kw):
    return evaluate(*parts, D(".004"), D(".008"), **kw)


def test_eligible_signal_is_post_only_fee_aware_and_native_trail_bounded():
    parts = fixture_data()
    signals, rejected = run(parts)
    assert rejected == {}
    assert len(signals) == 1
    signal = signals[0]
    assert signal.limit_price < parts[2]["TAO/EUR"].ask
    assert signal.limit_price % parts[0]["TAO/EUR"].price_tick == 0
    assert D("2.5") <= signal.trail_pct <= D(6)
    assert signal.opportunity_margin_bps > 100
    assert "not a return forecast" in signal.reason


def test_unfinished_candle_does_not_change_decision():
    parts = fixture_data()
    baseline = run(parts)
    parts[1]["TAO/EUR"].append(Candle(200 * 3600, 100, 1e9, 1, 1, 1e9))
    assert run(parts) == baseline


def test_no_btc_trade_or_non_niche_pair():
    parts = fixture_data()
    markets, _candles, _books, _btc, _now = parts
    markets["BTC/EUR"] = replace(markets["TAO/EUR"], symbol="BTC/EUR")
    signals, rejected = run(parts)
    assert all(s.symbol in ALLOWLIST for s in signals)
    assert "allowlist" in rejected["BTC/EUR"]


@pytest.mark.parametrize(
    "problem",
    ["stale_book", "thin_book", "crossed_book", "gap", "spike", "btc_crash", "wide_spread", "fee_hurdle"],
)
def test_fail_closed_on_market_and_cost_conditions(problem):
    parts = fixture_data()
    _markets, candles, books, btc, now = parts
    if problem == "stale_book":
        books["TAO/EUR"] = replace(books["TAO/EUR"], time=now - 21)
    elif problem == "thin_book":
        books["TAO/EUR"] = replace(books["TAO/EUR"], bids=((books["TAO/EUR"].bid, D(".01")),))
    elif problem == "crossed_book":
        books["TAO/EUR"] = replace(books["TAO/EUR"], bids=books["TAO/EUR"].asks)
    elif problem == "gap":
        candles["TAO/EUR"].pop(-5)
    elif problem == "spike":
        candles["TAO/EUR"] = [replace(c, high=c.high * 1.04, low=c.low * 0.96) for c in candles["TAO/EUR"]]
    elif problem == "btc_crash":
        btc[-1] = replace(btc[-1], close=90, low=89)
    elif problem == "wide_spread":
        books["TAO/EUR"] = replace(books["TAO/EUR"], asks=((books["TAO/EUR"].ask * D("1.02"), D(100)),))
    if problem == "fee_hurdle":
        signals, rejected = evaluate(*parts, D(".04"), D(".04"))
    else:
        signals, rejected = run(parts)
    assert not signals
    assert "TAO/EUR" in rejected


def test_missing_benchmark_blocks_everything():
    parts = list(fixture_data())
    parts[3] = []
    assert not run(parts)[0]
    assert "benchmark" in run(parts)[1]["TAO/EUR"]


def test_invalid_book_number_fails_closed():
    parts = fixture_data()
    parts[2]["TAO/EUR"] = replace(parts[2]["TAO/EUR"], asks=((D("NaN"), D(100)),))
    assert not run(parts)[0]


def test_fee_units_and_lookback_validated():
    with pytest.raises(ValueError, match="fee"):
        evaluate(*fixture_data(), D("0.4"), D("0.8"))
    with pytest.raises(ValueError, match="lookback"):
        run(fixture_data(), config=StrategyConfig(lookback=24))

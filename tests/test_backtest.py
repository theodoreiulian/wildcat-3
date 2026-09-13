import math
from decimal import Decimal as D

import pytest

from wildcat3.backtest import (
    BacktestConfig,
    SimPosition,
    chronological_report,
    maker_fills,
    performance,
    read_csv,
    simulate,
    trailing_bar,
)
from wildcat3.models import Candle, Signal


def position(high=100):
    return SimPosition("TAO/EUR", 1, 100, 100.4, 0.025, high, 0)


def test_stop_opening_gap_fills_at_gap_not_stop():
    price, _ = trailing_bar(position(110), Candle(0, 90, 91, 85, 89, 1000), 0.008, 20)
    assert price == pytest.approx(90 * 0.998)


def test_choose_pessimistic_trailing_candle_path():
    price, high = trailing_bar(position(), Candle(0, 100, 110, 99, 109, 1000), 0, 0)
    assert price == pytest.approx(110 * 0.975)
    assert high == 110


def test_pre_entry_high_cannot_improve_stop():
    price, _ = trailing_bar(position(), Candle(0, 101, 120, 96, 115, 1000), 0, 0, entry_pending=True)
    assert price == pytest.approx(97.5)


def test_post_only_rejects_opening_gap_touch_and_insufficient_volume():
    assert not maker_fills(Candle(0, 99, 101, 98, 100, 1000), 100, 0.01, 20)
    assert not maker_fills(Candle(0, 101, 102, 100, 101, 1000), 100, 0.01, 20)
    assert not maker_fills(Candle(0, 101, 102, 99, 101, 0.01), 100, 0.01, 20)
    assert maker_fills(Candle(0, 101, 102, 99, 101, 1000), 100, 0.01, 20)


def test_drawdown_includes_open_position_and_sharpe_uses_daily_time_series():
    report = performance([(3600, 200), (7200, 180), (86400, 190)], [])
    assert report["max_drawdown_pct"] == pytest.approx(10)
    assert report["daily_sharpe_365"] is None
    assert report["return_pct"] == pytest.approx(-5)
    history = [(i * 86400, 200 * (1.001**i) * (1 + 0.001 * (i % 2))) for i in range(40)]
    assert math.isfinite(performance(history, [])["daily_sharpe_365"])


def test_simulator_never_passes_future_candles_to_strategy():
    bars = [Candle(i * 3600, 101, 102, 100, 101, 1000) for i in range(10)]
    observed = []

    def inspector(markets, candles, books, btc, now, maker, taker):
        assert all(c.time + 3600 <= now for series in candles.values() for c in series)
        observed.append(now)
        return [], {}

    result = simulate({"TAO/EUR": bars, "BTC/EUR": bars}, evaluator=inspector)
    assert len(observed) == 10
    assert result["final_liquidation_equity_eur"] == 200
    assert result["closed_trades"] == 0
    assert result["execution_mode"] == "hourly_sensitivity_only"


def test_entry_stop_and_fees_in_same_bar_reduce_cash():
    bars = [Candle(i * 3600, 101, 102, 95, 100, 1000) for i in range(2)]

    def signal(*args):
        return [Signal("TAO/EUR", D(100), D("2.5"), 1, 500, "test", 0)], {}

    result = simulate({"TAO/EUR": bars, "BTC/EUR": bars}, evaluator=signal)
    assert result["closed_trades"] == 1
    assert result["trades"][0]["fees_eur"] > 0
    assert result["trades"][0]["pnl_eur"] < 0
    assert result["final_liquidation_equity_eur"] < 200
    assert result["open_position"] is None


def test_minute_execution_respects_first_minute_ttl():
    hours = [Candle(i * 3600, 101, 102, 99, 101, 1000) for i in range(2)]
    minutes = [Candle(i * 60, 101, 102, 100.5 if i == 0 else 99, 101, 1000) for i in range(60)]

    def signal(*args):
        return [Signal("TAO/EUR", D(100), D("2.5"), 1, 500, "test", 0)], {}

    result = simulate({"TAO/EUR": hours, "BTC/EUR": hours}, execution={"TAO/EUR": minutes}, evaluator=signal)
    assert result["entry_fills"] == 0
    assert result["execution_mode"] == "minute_trade_through"


def test_execution_gaps_are_not_silent():
    bars = [Candle(i * 3600, 101, 102, 99, 101, 1000) for i in (0, 2)]
    with pytest.raises(ValueError, match="gaps"):
        simulate({"TAO/EUR": bars, "BTC/EUR": bars})


def test_csv_requires_utc_and_contiguous_bars(tmp_path):
    target = tmp_path / "bad.csv"
    target.write_text(
        "timestamp,symbol,open,high,low,close,volume\n2026-09-01T00:00:00,TAO/EUR,10,11,9,10,5\n"
    )
    with pytest.raises(ValueError, match="offset"):
        read_csv(target)
    target.write_text(
        "timestamp,symbol,open,high,low,close,volume\n0,TAO/EUR,10,11,9,10,5\n7200,TAO/EUR,10,11,9,10,5\n"
    )
    with pytest.raises(ValueError, match="missing"):
        read_csv(target)


def test_walk_forward_is_chronological_with_no_parameter_refit():
    bars = [Candle(i * 3600, 101, 102, 99, 101, 1000) for i in range(300)]
    result = chronological_report({"TAO/EUR": bars, "BTC/EUR": bars}, BacktestConfig(), folds=3)
    previous = None
    for fold in result["walk_forward_folds"]:
        assert fold["train_history_end_exclusive"] == fold["test_start"]
        assert fold["test_start"] < fold["test_end_exclusive"]
        assert fold["parameters_refit"] is False
        if previous is not None:
            assert fold["test_start"] == previous
        previous = fold["test_end_exclusive"]


def test_hard_capital_and_risk_limits():
    with pytest.raises(ValueError, match="200"):
        BacktestConfig(initial_eur=1000)
    with pytest.raises(ValueError, match="risk"):
        BacktestConfig(risk_fraction=0.1)
    with pytest.raises(ValueError, match="fees"):
        BacktestConfig(taker_fee=float("nan"))


def test_unfilled_attempts_obey_cooldown_and_daily_cap():
    bars = [Candle(i * 3600, 101, 102, 100.5, 101, 1000) for i in range(24)]

    def signal(*args):
        return [Signal("TAO/EUR", D(100), D("2.5"), 1, 500, "test", 0)], {}

    result = simulate({"TAO/EUR": bars, "BTC/EUR": bars}, evaluator=signal)
    assert result["candidate_decisions"] == 2
    assert result["entry_fills"] == 0


def test_daily_loss_halt_latches_after_position_exits():
    bars = [Candle(i * 3600, 101, 102, 95, 100, 1000) for i in range(30)]

    def signal(*args):
        return [Signal("TAO/EUR", D(100), D("2.5"), 1, 500, "test", 0)], {}

    result = simulate(
        {"TAO/EUR": bars, "BTC/EUR": bars}, config=BacktestConfig(max_daily_loss=0.0001), evaluator=signal
    )
    assert result["circuit_breaker_halted"]
    assert result["closed_trades"] == 1
    assert result["candidate_decisions"] == 1


def test_minute_coverage_must_span_all_evaluation_folds():
    hours = [Candle(i * 3600, 101, 102, 99, 101, 1000) for i in range(300)]
    minutes = [Candle(i * 60, 101, 102, 99, 101, 1000) for i in range(120)]
    with pytest.raises(ValueError, match="full evaluation period"):
        chronological_report(
            {"TAO/EUR": hours, "BTC/EUR": hours}, BacktestConfig(), execution={"TAO/EUR": minutes}
        )


def test_daily_returns_exclude_partial_days():
    result = performance([(3600, 190), (86400, 198), (172800, 200), (176400, 195)], [])
    assert result["daily_observations"] == 1

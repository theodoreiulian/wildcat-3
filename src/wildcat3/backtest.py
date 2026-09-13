"""Chronological, fee-aware strategy diagnostics with explicitly limited OHLC fills.

CSV schema: timestamp,symbol,open,high,low,close,volume. Timestamp is UTC epoch
seconds or ISO-8601 with an explicit offset. Supply hourly bars for signals and
optionally minute bars for execution. No authenticated exchange calls occur.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from statistics import mean, stdev

from .models import Book, Candle, Market
from .strategy import ALLOWLIST, StrategyConfig, evaluate


@dataclass(frozen=True)
class BacktestConfig:
    initial_eur: float = 200.0
    maker_fee: float = 0.004
    taker_fee: float = 0.008
    spread_bps: float = 20.0
    slippage_bps: float = 30.0
    risk_fraction: float = 0.005
    max_allocation: float = 0.30
    cooldown_hours: int = 6
    max_drawdown: float = 0.08
    max_daily_loss: float = 0.02
    entry_ttl_seconds: int = 60
    reserve_eur: float = 20.0
    max_entries_per_day: int = 2

    def __post_init__(self) -> None:
        if self.initial_eur != 200:
            raise ValueError("WildCat-3 initial capital must equal EUR 200")
        if not 0 <= self.maker_fee <= 0.05 or not 0 <= self.taker_fee <= 0.05:
            raise ValueError("fees must be finite fractional rates between 0 and .05")
        if not 0 <= self.spread_bps <= 1000 or not 0 <= self.slippage_bps <= 1000:
            raise ValueError("invalid spread/slippage assumptions")
        if not 0 < self.risk_fraction <= 0.005 or not 0 < self.max_allocation <= 0.30:
            raise ValueError("risk/allocation exceeds hard limit")
        if self.entry_ttl_seconds != 60:
            raise ValueError("OHLC execution model supports a 60-second entry TTL only")
        if not 0 <= self.reserve_eur < self.initial_eur or not 1 <= self.max_entries_per_day <= 2:
            raise ValueError("invalid reserve or daily entry limit")


@dataclass(frozen=True)
class SimPosition:
    symbol: str
    quantity: float
    entry: float
    cost: float
    trail: float
    high_water: float
    entered: int


def _timestamp(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError("CSV ISO timestamp must include a UTC offset") from None
        return int(dt.timestamp())


def read_csv(path: str | Path, interval: int = 3600) -> dict[str, list[Candle]]:
    out: dict[str, list[Candle]] = defaultdict(list)
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
        if not required <= set(reader.fieldnames or []):
            raise ValueError("CSV requires " + ",".join(sorted(required)))
        for row in reader:
            symbol = row["symbol"].upper().strip()
            if symbol not in ALLOWLIST | {"BTC/EUR"}:
                raise ValueError(f"unsupported symbol {symbol}")
            candle = Candle(
                _timestamp(row["timestamp"]),
                *(float(row[k]) for k in ("open", "high", "low", "close", "volume")),
            )
            vals = (candle.open, candle.high, candle.low, candle.close, candle.volume)
            if not all(math.isfinite(x) for x in vals) or min(vals[:4]) <= 0 or candle.volume < 0:
                raise ValueError(f"invalid numeric candle for {symbol} at {candle.time}")
            if (
                not candle.low
                <= min(candle.open, candle.close)
                <= max(candle.open, candle.close)
                <= candle.high
            ):
                raise ValueError("invalid OHLC ordering")
            if candle.time % interval:
                raise ValueError(f"timestamp is not aligned to {interval} seconds")
            out[symbol].append(candle)
    if not out:
        raise ValueError("empty CSV")
    for symbol, bars in out.items():
        bars.sort(key=lambda c: c.time)
        if any(b.time - a.time != interval for a, b in itertools.pairwise(bars)):
            raise ValueError(f"duplicate or missing {interval}-second bars for {symbol}")
    return dict(out)


def default_markets() -> dict[str, Market]:
    # Offline metadata assumptions must be rechecked for the chosen historical period.
    specs = {
        "TAO": (".0001", ".00001", ".02"),
        "RENDER": (".001", ".00001", "2.5"),
        "AKT": (".0001", ".00000001", "10"),
        "FET": (".0001", ".00000001", "18"),
    }
    return {
        f"{base}/EUR": Market(
            f"{base}/EUR",
            f"{base}EUR",
            base,
            "ZEUR",
            Decimal(tick),
            Decimal(step),
            Decimal(minimum),
            Decimal(".45"),
        )
        for base, (tick, step, minimum) in specs.items()
    }


def _path_exit(pos: SimPosition, points: list[float], entry_pending: bool) -> tuple[float | None, float]:
    high = pos.high_water
    active = not entry_pending
    previous = points[0]
    if active:
        if previous <= high * (1 - pos.trail):
            return previous, high  # opening gap: never fill at stale stop price
        high = max(high, previous)
    for price in points[1:]:
        if not active:
            if price < pos.entry <= previous:
                active = True
                high = pos.entry  # a pre-fill high must never tighten the stop
            else:
                previous = price
                continue
        stop = high * (1 - pos.trail)
        if price <= stop:
            return stop, high
        high = max(high, price)
        previous = price
    return None, high


def trailing_bar(
    pos: SimPosition, candle: Candle, taker_fee: float, slippage_bps: float, entry_pending: bool = False
) -> tuple[float | None, float]:
    """Choose the worse result of O-H-L-C and O-L-H-C, including same-bar stops."""
    paths = (
        [candle.open, candle.high, candle.low, candle.close],
        [candle.open, candle.low, candle.high, candle.close],
    )
    outcomes = []
    for points in paths:
        trigger, high = _path_exit(pos, points, entry_pending)
        execution = trigger * (1 - slippage_bps / 10000) if trigger is not None else None
        value = (execution if execution is not None else candle.close) * (1 - taker_fee)
        outcomes.append((value, execution, high))
    _, execution, high = min(outcomes, key=lambda result: result[0])
    return execution, high


def maker_fills(candle: Candle, limit: float, tick: float, notional: float) -> bool:
    """Reject marketable opening gaps; require price-through and volume capacity."""
    return candle.open > limit and candle.low < limit - tick and candle.volume * limit >= notional * 20


def performance(equity: list[tuple[int, float]], trades: list[dict], initial: float = 200) -> dict:
    peak = initial
    maximum_drawdown = 0.0
    daily: dict[int, float] = {}
    for timestamp, value in equity:
        peak = max(peak, value)
        maximum_drawdown = max(maximum_drawdown, 1 - value / peak)
        if timestamp % 86400 == 0:
            daily[timestamp] = value
    # Exclude partial first/last UTC days; annualization uses calendar days, not trades.
    returns = [
        value / previous - 1
        for (a, previous), (b, value) in itertools.pairwise(sorted(daily.items()))
        if b - a == 86400
    ]
    daily_sharpe = None
    if len(returns) >= 30 and stdev(returns) > 0:
        daily_sharpe = mean(returns) / stdev(returns) * math.sqrt(365)
    wins = sum(t["pnl_eur"] > 0 for t in trades)
    profits = sum(max(0, t["pnl_eur"]) for t in trades)
    losses = -sum(min(0, t["pnl_eur"]) for t in trades)
    return {
        "final_liquidation_equity_eur": equity[-1][1] if equity else initial,
        "return_pct": ((equity[-1][1] / initial - 1) * 100) if equity else 0,
        "max_drawdown_pct": maximum_drawdown * 100,
        "closed_trades": len(trades),
        "wins": wins,
        "losses": sum(t["pnl_eur"] < 0 for t in trades),
        "win_rate": wins / len(trades) if trades else None,
        "realized_pnl_eur": sum(t["pnl_eur"] for t in trades),
        "profit_factor": profits / losses if losses else None,
        "daily_sharpe_365": daily_sharpe,
        "daily_observations": len(returns),
    }


def simulate(
    data: dict[str, list[Candle]],
    *,
    config: BacktestConfig | None = None,
    execution: dict[str, list[Candle]] | None = None,
    start: int | None = None,
    end: int | None = None,
    evaluator: Callable = evaluate,
) -> dict:
    cfg = config or BacktestConfig()
    if "BTC/EUR" not in data:
        raise ValueError("BTC/EUR hourly benchmark required")
    markets = {s: m for s, m in default_markets().items() if s in data}
    if not markets:
        raise ValueError("at least one supported niche EUR pair required")
    source = execution if execution is not None else data
    step = 60 if execution is not None else 3600
    if execution is not None:
        for s in markets:
            if s not in execution:
                raise ValueError(f"minute execution data missing {s}")
    # Use the intersection so missing bars never silently suspend exchange-side stops.
    times = sorted(set.intersection(*({c.time for c in source[s]} for s in markets)))
    if not times:
        raise ValueError("no overlapping execution coverage")
    if any(b - a != step for a, b in itertools.pairwise(times)):
        raise ValueError("execution coverage has gaps")
    lookup = {s: {c.time: c for c in source[s]} for s in markets}
    history_times = {s: [c.time + 3600 for c in bars] for s, bars in data.items()}
    cash = cfg.initial_eur
    pos: SimPosition | None = None
    trades: list[dict] = []
    equity: list[tuple[int, float]] = []
    rejections: Counter = Counter()
    decisions = 0
    candidate_count = 0
    fills = 0
    cooldown_until = 0
    peak = cfg.initial_eur
    day_start_equity = cfg.initial_eur
    current_day = None
    halted = False
    halt_reason = None
    daily_attempts = 0
    active_times = [t for t in times if (start is None or t >= start) and (end is None or t < end)]
    for t in active_times:
        if current_day != t // 86400:
            current_day = t // 86400
            day_start_equity = equity[-1][1] if equity else cash
            daily_attempts = 0
        entry_pending = False
        if (
            t % 3600 == 0
            and pos is None
            and not halted
            and t >= cooldown_until
            and daily_attempts < cfg.max_entries_per_day
        ):
            histories = {s: bars[: bisect.bisect_right(history_times[s], t)] for s, bars in data.items()}
            books = {}
            for s in markets:
                if not histories[s]:
                    continue
                # Last completed close only. Never use the execution bar's future high/low.
                mid = histories[s][-1].close
                half = cfg.spread_bps / 20000
                books[s] = Book(
                    s,
                    float(t),
                    ((Decimal(str(mid * (1 - half))), Decimal(str(10000 / mid))),),
                    ((Decimal(str(mid * (1 + half))), Decimal(str(10000 / mid))),),
                )
            signals, rejected = evaluator(
                markets,
                histories,
                books,
                histories["BTC/EUR"],
                float(t),
                Decimal(str(cfg.maker_fee)),
                Decimal(str(cfg.taker_fee)),
            )
            decisions += 1
            rejections.update(rejected.values())
            if signals:
                candidate_count += 1
                signal = signals[0]
                market = markets[signal.symbol]
                limit = float(signal.limit_price)
                trail = float(signal.trail_pct) / 100
                friction = cfg.maker_fee + cfg.taker_fee + (cfg.spread_bps + cfg.slippage_bps) / 10000
                notional = min(
                    cash * cfg.max_allocation,
                    cash * cfg.risk_fraction / (trail + friction),
                    max(0, cash - cfg.reserve_eur) / (1 + cfg.maker_fee),
                )
                qty = float(
                    (Decimal(str(notional / limit)) / market.qty_step).to_integral_value(rounding=ROUND_DOWN)
                    * market.qty_step
                )
                if qty < float(market.min_qty) or qty * limit < float(market.min_cost):
                    rejections["exchange minimum prevents risk-sized ticket"] += 1
                else:
                    daily_attempts += 1
                    cooldown_until = t + cfg.cooldown_hours * 3600
                if (
                    qty >= float(market.min_qty)
                    and qty * limit >= float(market.min_cost)
                    and maker_fills(lookup[signal.symbol][t], limit, float(market.price_tick), qty * limit)
                ):
                    cost = qty * limit * (1 + cfg.maker_fee)
                    cash -= cost
                    pos = SimPosition(signal.symbol, qty, limit, cost, trail, limit, t)
                    fills += 1
                    entry_pending = True
        if pos is not None:
            candle = lookup[pos.symbol][t]
            exit_price, high = trailing_bar(pos, candle, cfg.taker_fee, cfg.slippage_bps, entry_pending)
            if exit_price is not None:
                proceeds = pos.quantity * exit_price * (1 - cfg.taker_fee)
                pnl = proceeds - pos.cost
                cash += proceeds
                trades.append(
                    {
                        "symbol": pos.symbol,
                        "entry_time": pos.entered,
                        "exit_bar_time": t,
                        "quantity": pos.quantity,
                        "entry_price": pos.entry,
                        "exit_price": exit_price,
                        "pnl_eur": pnl,
                        "pnl_pct": pnl / pos.cost * 100,
                        "fees_eur": pos.quantity * pos.entry * cfg.maker_fee
                        + pos.quantity * exit_price * cfg.taker_fee,
                    }
                )
                pos = None
            else:
                pos = SimPosition(pos.symbol, pos.quantity, pos.entry, pos.cost, pos.trail, high, pos.entered)
        mark = cash
        if pos is not None:
            mark += (
                pos.quantity
                * lookup[pos.symbol][t].close
                * (1 - cfg.spread_bps / 20000)
                * (1 - cfg.taker_fee - cfg.slippage_bps / 10000)
            )
        equity.append((t + step, mark))
        peak = max(peak, mark)
        if mark <= peak * (1 - cfg.max_drawdown) or mark <= cfg.initial_eur * (1 - cfg.max_drawdown):
            halted = True
            halt_reason = "8% drawdown circuit breaker"
        if mark <= day_start_equity * (1 - cfg.max_daily_loss):
            halted = True
            halt_reason = "2% daily loss circuit breaker (latched; manual restart required in live mode)"
    result = performance(equity, trades, cfg.initial_eur)
    result.update(
        {
            "configuration": asdict(cfg),
            "execution_mode": "minute_trade_through" if execution is not None else "hourly_sensitivity_only",
            "entry_fill_assumption": "first full minute; strict trade-through; no queue model"
            if execution is not None
            else "entire next hour available; incompatible with live 60-second TTL",
            "book_assumption": "fixed synthetic spread and EUR 10000 depth per side; liquidity gates cannot be validated",
            "decisions": decisions,
            "candidate_decisions": candidate_count,
            "entry_fills": fills,
            "rejection_counts": dict(rejections.most_common()),
            "trades": trades,
            "open_position": asdict(pos) if pos else None,
            "equity": equity,
            "circuit_breaker_halted": halted,
            "halt_reason": halt_reason,
            "evidence_warning": "Historical diagnostics do not establish live profitability. OHLC ordering, queue position, partial fills and impact remain unobserved.",
        }
    )
    return result


def chronological_report(
    data: dict[str, list[Candle]],
    config: BacktestConfig,
    execution: dict[str, list[Candle]] | None = None,
    folds: int = 3,
) -> dict:
    """Fixed-parameter expanding-history validation; never optimize on test folds."""
    common_start = max(bars[0].time for bars in data.values()) + StrategyConfig().lookback * 3600
    common_end = min(bars[-1].time + 3600 for bars in data.values())
    if execution is not None:
        for symbol in set(data) & ALLOWLIST:
            bars = execution.get(symbol, [])
            if not bars or bars[0].time > common_start or bars[-1].time + 60 < common_end:
                raise ValueError(f"minute execution coverage must span the full evaluation period: {symbol}")
    count = (common_end - common_start) // 3600
    if count < 72:
        raise ValueError("need at least 168 warmup + 72 overlapping evaluation hours")
    if folds < 1 or folds > 10:
        raise ValueError("folds must be 1..10")
    split = common_start + int(count * 0.6) * 3600
    test_hours = (common_end - split) // 3600
    windows = []
    for i in range(folds):
        a = split + (test_hours * i // folds) * 3600
        b = split + (test_hours * (i + 1) // folds) * 3600
        fold = simulate(data, config=config, execution=execution, start=a, end=b)
        fold.pop("equity")
        windows.append(
            {
                "train_history_end_exclusive": a,
                "test_start": a,
                "test_end_exclusive": b,
                "parameters_refit": False,
                "result": fold,
            }
        )
    development = simulate(data, config=config, execution=execution, start=common_start, end=split)
    held_out = simulate(data, config=config, execution=execution, start=split, end=common_end)
    stress = simulate(
        data,
        config=BacktestConfig(
            **{
                **asdict(config),
                "slippage_bps": min(1000, config.slippage_bps * 2),
                "spread_bps": min(1000, config.spread_bps * 1.5),
            }
        ),
        execution=execution,
        start=split,
        end=common_end,
    )
    return {
        "method": "Fixed parameters; first 60% development, final 40% held-out; expanding historical warmup; folds restart EUR 200.",
        "limitations": [
            "Reusing this held-out period to tune thresholds invalidates its out-of-sample status.",
            "Minute bars still approximate queue position and the precise 60-second order lifetime.",
            "One month's REST OHLC history is insufficient for statistical validation.",
            "Daily-loss and drawdown halts persist for each simulation; exchange trailing protection remains active.",
            "Signals are sampled at hourly boundaries; live scans once per minute and can discover a later eligible book.",
            "Current market metadata is used offline; historical fee/minimum changes need separate inputs.",
        ],
        "development": development,
        "held_out": held_out,
        "held_out_stress": stress,
        "walk_forward_folds": windows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="combined hourly BTC/EUR + niche EUR CSV")
    parser.add_argument("--execution-csv", type=Path, help="optional consecutive minute execution candles")
    parser.add_argument("--output", type=Path, default=Path("backtest-report.json"))
    parser.add_argument("--maker-fee", type=float, default=0.004)
    parser.add_argument("--taker-fee", type=float, default=0.008)
    parser.add_argument("--spread-bps", type=float, default=20)
    parser.add_argument("--slippage-bps", type=float, default=30)
    parser.add_argument("--folds", type=int, default=3)
    args = parser.parse_args()
    cfg = BacktestConfig(
        maker_fee=args.maker_fee,
        taker_fee=args.taker_fee,
        spread_bps=args.spread_bps,
        slippage_bps=args.slippage_bps,
    )
    report = chronological_report(
        read_csv(args.csv), cfg, read_csv(args.execution_csv, 60) if args.execution_csv else None, args.folds
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "report": str(args.output),
                "held_out": {
                    k: v
                    for k, v in report["held_out"].items()
                    if k not in {"trades", "equity", "rejection_counts"}
                },
                "limitations": report["limitations"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

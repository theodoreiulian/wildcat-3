"""Selective EUR compute-token continuation signals; no prediction of profitability.

All indicators use completed, consecutive hourly candles. `opportunity_margin_bps` is
an historical opportunity proxy less a conservative cost budget, NOT an expected
return. Book checks assess tradability, not a stand-alone order-flow alpha.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from statistics import median

from .models import Book, Candle, Market, Signal

ALLOWLIST = frozenset({"TAO/EUR", "RENDER/EUR", "AKT/EUR", "FET/EUR"})
HOUR = 3600


@dataclass(frozen=True)
class StrategyConfig:
    lookback: int = 168
    max_book_age_seconds: float = 15.0
    max_spread_bps: float = 25.0
    min_depth_eur: float = 1200.0  # each side, inside 50 bp, >=20x maximum initial ticket
    depth_band_bps: float = 50.0
    max_book_imbalance: float = 0.65
    min_daily_turnover_eur: float = 100_000.0
    min_relative_strength_bps: float = 100.0
    min_volume_ratio: float = 1.4
    max_volume_ratio: float = 5.0
    min_trail_pct: float = 2.5
    max_trail_pct: float = 6.0
    exit_slippage_bps: float = 30.0
    uncertainty_bps: float = 35.0
    minimum_net_margin_bps: float = 100.0


def _closed(candles: list[Candle], now: float, count: int) -> list[Candle]:
    completed = [c for c in candles if c.time + HOUR <= now]
    if len(completed) < count:
        raise ValueError(f"need {count} completed hourly candles")
    completed = completed[-count:]
    if completed[-1].time + HOUR < now - HOUR:
        raise ValueError("stale completed candles")
    previous = None
    for c in completed:
        if previous is not None and c.time - previous != HOUR:
            raise ValueError("duplicate, unordered or missing hourly candles")
        previous = c.time
        if c.time % HOUR:
            raise ValueError("candles must open on UTC hour boundaries")
        if not all(math.isfinite(x) and x > 0 for x in (c.open, c.high, c.low, c.close)):
            raise ValueError("invalid candle prices")
        if (
            not math.isfinite(c.volume)
            or c.volume < 0
            or not c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high
        ):
            raise ValueError("invalid candle range or volume")
    return completed


def _return(candles: list[Candle], periods: int) -> float:
    return candles[-1].close / candles[-1 - periods].close - 1.0


def robust_range(candles: list[Candle], periods: int = 72) -> float:
    """Median fractional true range, resilient to a single liquidation wick."""
    values = [
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) / p.close
        for p, c in itertools.pairwise(candles)
    ]
    return median(values[-periods:])


def book_metrics(book: Book, band_bps: float = 50.0) -> dict[str, float]:
    if not book.bids or not book.asks:
        raise ValueError("empty order book")
    bids = [(float(p), float(q)) for p, q in book.bids]
    asks = [(float(p), float(q)) for p, q in book.asks]
    if not all(math.isfinite(v) and v > 0 for side in (bids, asks) for level in side for v in level):
        raise ValueError("invalid book price or quantity")
    if bids != sorted(bids, key=lambda x: -x[0]) or asks != sorted(asks, key=lambda x: x[0]):
        raise ValueError("unordered book")
    bid, ask = bids[0][0], asks[0][0]
    if bid >= ask:
        raise ValueError("crossed order book")
    mid = (bid + ask) / 2
    depth_bid = sum(p * q for p, q in bids if p >= mid * (1 - band_bps / 10000))
    depth_ask = sum(p * q for p, q in asks if p <= mid * (1 + band_bps / 10000))
    return {
        "mid": mid,
        "spread_bps": (ask - bid) / mid * 10000,
        "bid_depth_eur": depth_bid,
        "ask_depth_eur": depth_ask,
        "imbalance": abs(depth_bid - depth_ask) / max(depth_bid + depth_ask, 1e-12),
    }


def evaluate(
    markets: dict[str, Market],
    candles: dict[str, list[Candle]],
    books: dict[str, Book],
    benchmark: list[Candle],
    now: float,
    maker_fee: Decimal,
    taker_fee: Decimal,
    config: StrategyConfig | None = None,
) -> tuple[list[Signal], dict[str, str]]:
    """Return ranked eligible signals and one clear rejection reason per pair."""
    cfg = config or StrategyConfig()
    if cfg.lookback < 73:
        raise ValueError("lookback must cover at least 72 complete return periods")
    fee_rates = float(maker_fee), float(taker_fee)
    if any(not math.isfinite(f) or f < 0 or f > 0.05 for f in fee_rates):
        raise ValueError("invalid fee rates; supply fractions, not percentages")
    rejects: dict[str, str] = {}
    signals: list[Signal] = []
    try:
        btc = _closed(benchmark, now, cfg.lookback)
        if _return(btc, 24) < -0.015 or _return(btc, 72) < -0.05:
            raise ValueError("BTC risk-off regime")
        if btc[-1].close / btc[-2].close - 1 < -0.012:
            raise ValueError("BTC shock in latest hour")
    except ValueError as exc:
        return [], {s: f"benchmark: {exc}" for s in markets}
    for symbol, market in sorted(markets.items()):
        try:
            if symbol not in ALLOWLIST:
                raise ValueError("outside decentralized AI/compute EUR allowlist")
            if market.status != "online":
                raise ValueError("market is not online")
            if market.price_tick <= 0 or market.qty_step <= 0:
                raise ValueError("invalid market precision")
            cs = _closed(candles.get(symbol, []), now, cfg.lookback)
            if cs[-1].time != btc[-1].time:
                raise ValueError("benchmark and asset timestamps do not match")
            book = books.get(symbol)
            if book is None or not -1 <= now - book.time <= cfg.max_book_age_seconds:
                raise ValueError("missing or stale order book")
            bm = book_metrics(book, cfg.depth_band_bps)
            if bm["spread_bps"] > cfg.max_spread_bps:
                raise ValueError("spread exceeds friction budget")
            if min(bm["bid_depth_eur"], bm["ask_depth_eur"]) < cfg.min_depth_eur:
                raise ValueError("insufficient two-sided EUR depth inside 50 bp")
            if bm["imbalance"] > cfg.max_book_imbalance:
                raise ValueError("extreme book imbalance/adverse-selection risk")
            turnover = sum(c.close * c.volume for c in cs[-24:])
            if turnover < cfg.min_daily_turnover_eur:
                raise ValueError("insufficient actual EUR-pair daily turnover")
            r24, r72 = _return(cs, 24), _return(cs, 72)
            relative = r24 - _return(btc, 24)
            if r24 < 0.02 or r72 <= 0 or relative * 10000 < cfg.min_relative_strength_bps:
                raise ValueError("no persistent sector leadership versus BTC")
            if r24 > 0.25 or r72 > 0.45:
                raise ValueError("overextended multi-day impulse")
            tr = robust_range(cs)
            if tr <= 0:
                raise ValueError("zero robust volatility")
            raw_trail = max(cfg.min_trail_pct, tr * 2.8 * 100)
            if raw_trail > cfg.max_trail_pct:
                raise ValueError("volatility needs trail wider than 6% limit")
            # Require a close through the prior 24-hour closing range, with a
            # strong candle close but no single-candle exhaustion move.
            prior_top = max(c.close for c in cs[-25:-1])
            last = cs[-1]
            if last.close < prior_top or last.close > prior_top * (1 + 1.5 * tr):
                raise ValueError("no controlled 24-hour closing-range breakout")
            if (last.close - last.low) / max(last.high - last.low, 1e-12) < 0.6:
                raise ValueError("weak close/rejection wick")
            if (last.high - last.low) / cs[-2].close > 3 * tr:
                raise ValueError("exhaustion candle/adverse selection")
            baseline_volume = median(c.volume for c in cs[-49:-1])
            ratio = last.volume / max(baseline_volume, 1e-12)
            if not cfg.min_volume_ratio <= ratio <= cfg.max_volume_ratio:
                raise ValueError("volume confirmation absent or climactic")
            # A stale candle price or post-news jump must not be chased.
            if abs(bm["mid"] / last.close - 1) > max(0.0025, tr * 0.5):
                raise ValueError("live price moved too far from confirmed signal")
            cost_bps = (
                ((1 + fee_rates[0]) / (1 - fee_rates[1]) - 1) * 10000
                + bm["spread_bps"]
                + cfg.exit_slippage_bps
                + max(cfg.uncertainty_bps, tr * 10000 * 0.25)
            )
            # A bounded historical movement scale. It is NOT a forecast: only
            # an exclusion test for opportunities too small to pay trading costs.
            opportunity_bps = min(r72, tr * math.sqrt(24) * 2) * 10000
            net_margin = opportunity_bps - cost_bps
            required = max(2 * cost_bps, raw_trail * 100 + cost_bps, cost_bps + cfg.minimum_net_margin_bps)
            if opportunity_bps < required:
                raise ValueError(
                    f"opportunity scale {opportunity_bps:.0f} bp below {required:.0f} bp fee/risk hurdle"
                )
            limit = (book.bids[0][0] / market.price_tick).to_integral_value(
                rounding=ROUND_DOWN
            ) * market.price_tick
            if limit <= 0 or limit >= book.asks[0][0]:
                raise ValueError("cannot quote valid post-only bid")
            # Round upward: do not narrow a calibrated exchange-side stop.
            trail = Decimal(str(raw_trail)).quantize(Decimal("0.01"), rounding="ROUND_UP")
            score = net_margin / (float(trail) * 100 + cost_bps) * min(relative / 0.02, 3)
            signals.append(
                Signal(
                    symbol=symbol,
                    limit_price=limit,
                    trail_pct=trail,
                    score=score,
                    opportunity_margin_bps=net_margin,
                    reason=(
                        f"24h relative strength {relative:.1%}; volume {ratio:.2f}x; "
                        f"cost budget {cost_bps:.0f} bp; historical opportunity "
                        f"{opportunity_bps:.0f} bp (not a return forecast)"
                    ),
                    candle_time=last.time,
                )
            )
        except (ValueError, ArithmeticError) as exc:
            rejects[symbol] = str(exc)
    return sorted(signals, key=lambda s: (-s.score, s.symbol)), rejects

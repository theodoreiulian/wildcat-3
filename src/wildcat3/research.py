"""Capture reproducible public Kraken EUR microstructure observations (no keys).

The REST endpoint supplies approximately 30 days of hourly candles. This command measures a
snapshot and exports data; it never treats that short history as an edge proof.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import itertools
import json
import math
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import correlation, median, stdev

import httpx

from .models import Book, Candle, Market
from .strategy import ALLOWLIST, book_metrics, evaluate, robust_range

BASE_URL = "https://api.kraken.com/0/public/"


async def public(client: httpx.AsyncClient, endpoint: str, **params: str | int) -> dict:
    response = await client.get(BASE_URL + endpoint, params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"Kraken {endpoint}: {payload['error']}")
    if "result" not in payload:
        raise RuntimeError(f"Kraken {endpoint}: result missing")
    return payload["result"]


def summarize(candles: list[Candle], book: Book, ticker: dict, btc: list[Candle]) -> dict:
    metrics = book_metrics(book)
    last = candles[-1]
    metrics.update(
        {
            "completed_hourly_candles": len(candles),
            "history_start_utc": datetime.fromtimestamp(candles[0].time, UTC).isoformat(),
            "last_completed_open_utc": datetime.fromtimestamp(last.time, UTC).isoformat(),
            "return_24h_pct": (last.close / candles[-25].close - 1) * 100,
            "return_72h_pct": (last.close / candles[-73].close - 1) * 100,
            "median_hourly_true_range_pct": robust_range(candles) * 100,
            "turnover_24h_eur": float(ticker["v"][1]) * float(ticker["p"][1]),
            "trades_24h": int(ticker["t"][1]),
            "median_hourly_volume": median(c.volume for c in candles[-169:-1]),
        }
    )
    xs = {b.time: math.log(b.close / a.close) for a, b in itertools.pairwise(candles)}
    ys = {b.time: math.log(b.close / a.close) for a, b in itertools.pairwise(btc)}
    overlap = sorted(xs.keys() & ys.keys())[-168:]
    if len(overlap) >= 24 and stdev(xs[t] for t in overlap) > 0 and stdev(ys[t] for t in overlap) > 0:
        metrics["hourly_return_correlation_to_btc_7d"] = correlation(
            [xs[t] for t in overlap], [ys[t] for t in overlap]
        )
    else:
        metrics["hourly_return_correlation_to_btc_7d"] = None
    return metrics


async def capture(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).isoformat()
    raw: dict = {
        "capture_started_utc": started,
        "source": BASE_URL,
        "limitations": "One book snapshot; not synchronized; no private fees, queue position, fill probability, or historical books.",
    }
    markets: dict[str, Market] = {}
    candles: dict[str, list[Candle]] = {}
    books: dict[str, Book] = {}
    tickers: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "WildCat-3-public-research/1"}) as client:
        metadata = await public(client, "AssetPairs")
        wanted = set(ALLOWLIST) | {"BTC/EUR"}
        for key, item in metadata.items():
            symbol = item.get("wsname", "").replace("XBT/", "BTC/")
            if symbol not in wanted or symbol in markets:
                continue
            markets[symbol] = Market(
                symbol,
                item["altname"],
                item["base"],
                item["quote"],
                Decimal(item.get("tick_size", str(10 ** -int(item["pair_decimals"])))),
                Decimal(10) ** -int(item["lot_decimals"]),
                Decimal(item["ordermin"]),
                Decimal(item.get("costmin", "0")),
                item.get("status", "online"),
            )
            raw.setdefault("asset_pairs", {})[symbol] = {"key": key, **item}
        raw["unavailable_pairs"] = sorted(wanted - markets.keys())
        if "BTC/EUR" not in markets:
            raise RuntimeError("Kraken BTC/EUR benchmark not available")
        for symbol, market in sorted(markets.items()):
            # Serialized requests are courteous to Kraken's public per-IP limits.
            ticker = await public(client, "Ticker", pair=market.pair)
            depth = await public(client, "Depth", pair=market.pair, count=100)
            fetched = time.time()
            ohlc = await public(client, "OHLC", pair=market.pair, interval=60)
            tickers[symbol] = next(iter(ticker.values()))
            levels = next(iter(depth.values()))
            books[symbol] = Book(
                symbol,
                fetched,
                tuple((Decimal(p), Decimal(q)) for p, q, *_ in levels["bids"]),
                tuple((Decimal(p), Decimal(q)) for p, q, *_ in levels["asks"]),
            )
            rows = next(value for key, value in ohlc.items() if key != "last")
            # Kraken's last OHLC row is incomplete; filter by interval closure too.
            candles[symbol] = [
                Candle(int(row[0]), *(float(row[i]) for i in (1, 2, 3, 4, 6)))
                for row in rows[:-1]
                if int(row[0]) + 3600 <= time.time()
            ]
            if len(candles[symbol]) < 169:
                raise RuntimeError(f"insufficient history for measurements: {symbol}")
            raw.setdefault("responses", {})[symbol] = {
                "book_fetched_utc": datetime.fromtimestamp(fetched, UTC).isoformat(),
                "ticker": ticker,
                "depth": depth,
                "ohlc": ohlc,
            }
    metrics = {s: summarize(candles[s], books[s], tickers[s], candles["BTC/EUR"]) for s in sorted(markets)}
    signals, rejections = evaluate(
        {s: m for s, m in markets.items() if s in ALLOWLIST},
        candles,
        books,
        candles["BTC/EUR"],
        time.time(),
        Decimal(".004"),
        Decimal(".008"),
    )
    raw.update(
        {
            "capture_finished_utc": datetime.now(UTC).isoformat(),
            "measurements": metrics,
            "candidate_symbols": [s.symbol for s in signals],
            "strategy_rejections": rejections,
            "signal_fees_assumed": {"maker": 0.004, "taker": 0.008},
        }
    )
    (output / "snapshot.json").write_text(json.dumps(raw, indent=2, allow_nan=False) + "\n")
    with (output / "hourly.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        for symbol, series in sorted(candles.items()):
            for c in series:
                writer.writerow([c.time, symbol, c.open, c.high, c.low, c.close, c.volume])
    lines = [
        "# Kraken public EUR microstructure snapshot",
        "",
        f"Captured {started}.",
        "",
        "| Pair | Spread bp | Bid depth ±50bp € | Ask depth ±50bp € | 24h turnover € | Median hourly TR % | 7d BTC correlation |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for symbol, m in metrics.items():
        corr = m["hourly_return_correlation_to_btc_7d"]
        corr_text = f"{corr:.3f}" if corr is not None else "unavailable"
        lines.append(
            f"| {symbol} | {m['spread_bps']:.2f} | {m['bid_depth_eur']:.0f} | {m['ask_depth_eur']:.0f} | {m['turnover_24h_eur']:.0f} | {m['median_hourly_true_range_pct']:.3f} | {corr_text} |"
        )
    lines.extend(
        [
            "",
            (
                "One observation of public books, collected serially. Displayed orders can disappear. "
                "These are actual EUR books, not USD liquidity translated into euros. No statistical edge is established."
            ),
            "",
            "## Current screening result",
            "",
        ]
    )
    lines.extend(f"- {s}: {reason}" for s, reason in rejections.items())
    lines.extend(f"- {s.symbol}: eligible observation; requires independent validation." for s in signals)
    lines.extend(
        [
            "",
            (
                "Sources: [Kraken Depth](https://docs.kraken.com/api/docs/rest-api/get-order-book/), "
                "[OHLC](https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/), "
                "[Ticker](https://docs.kraken.com/api/docs/rest-api/get-ticker-information/)."
            ),
            "",
            (
                "The CSV contains only the recent REST OHLC window of roughly 30 days. Longer data and minute or trade-level "
                "execution records are needed; the snapshot is not an adequate backtest sample."
            ),
        ]
    )
    (output / "snapshot.md").write_text("\n".join(lines) + "\n")
    return {
        "output": str(output),
        "captured_utc": started,
        "measurements": metrics,
        "candidate_symbols": raw["candidate_symbols"],
        "strategy_rejections": rejections,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("data/research") / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(capture(args.output)), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

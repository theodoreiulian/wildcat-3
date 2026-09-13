from decimal import Decimal

from .config import Config
from .models import Book, Market, Signal, floor_step

D = Decimal


def size_entry(config: Config, market: Market, signal: Signal, book: Book,
               equity: Decimal, available: Decimal, maker: Decimal, taker: Decimal) -> Decimal:
    """Budget stop loss + both fees + slippage; never round upward to a venue minimum."""
    if market.symbol != signal.symbol or market.status != "online":
        return D(0)
    if signal.limit_price <= 0 or not D("2.5") <= signal.trail_pct <= D("6"):
        return D(0)
    friction = maker + taker + config.slippage_bps / 10000 + book.spread_bps / 10000
    loss_fraction = signal.trail_pct / 100 + friction
    risk_budget = max(D(0), equity) * config.risk_fraction
    notional = min(risk_budget / loss_fraction, equity * config.max_allocation,
                   max(D(0), available - config.reserve_eur) / (1 + maker))
    qty = floor_step(notional / signal.limit_price, market.qty_step)
    # Consume at most 1% of visible bids within 50 bps. Exit liquidity matters most.
    depth = sum((p * q for p, q in book.bids if p >= book.bid * D("0.995")), D(0))
    qty = min(qty, floor_step(depth * D("0.01") / signal.limit_price, market.qty_step))
    if qty < market.min_qty or qty * signal.limit_price < market.min_cost:
        return D(0)
    return max(D(0), qty)

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

D = Decimal
ZERO = D("0")
UNIVERSE = ("TAO/EUR", "RENDER/EUR", "AKT/EUR", "FET/EUR")


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("Step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class Market:
    symbol: str
    pair: str
    base: str
    quote: str
    price_tick: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_cost: Decimal
    status: str = "online"


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Book:
    symbol: str
    time: float
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]

    @property
    def bid(self) -> Decimal:
        return self.bids[0][0]

    @property
    def ask(self) -> Decimal:
        return self.asks[0][0]

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * 10000


@dataclass(frozen=True)
class Signal:
    symbol: str
    limit_price: Decimal
    trail_pct: Decimal
    score: float
    opportunity_margin_bps: float
    reason: str
    candle_time: int


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    cost_eur: Decimal

    @property
    def avg_price(self) -> Decimal:
        return self.cost_eur / self.quantity if self.quantity else ZERO

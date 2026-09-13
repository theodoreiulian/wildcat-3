"""Deterministic average-cost spot accounting from deduplicated executions."""
from dataclasses import dataclass
from decimal import Decimal

from .models import Position

D = Decimal


class AccountingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Ledger:
    cash: Decimal
    positions: dict[str, Position]
    realized: Decimal
    fees: Decimal
    wins: int
    losses: int
    completed: int
    exit_pnl: dict[str, tuple[Decimal, Decimal]]


def ledger(fills: dict, initial: Decimal = D("200")) -> Ledger:
    cash, realized, fees = initial, D(0), D(0)
    holdings: dict[str, Position] = {}
    round_pnl: dict[str, Decimal] = {}
    exit_pnl = {}
    wins = losses = completed = 0
    # Exchange identifiers are not chronological. An attached close can share
    # its parent's timestamp; process acquisitions before disposals at that time.
    for txid, fill in sorted(fills.items(), key=lambda x: (D(str(x[1]["time"])), x[1]["type"] != "buy", x[0])):
        symbol, side = fill["symbol"], fill["type"]
        qty, cost, fee = (D(str(fill[k])) for k in ("vol", "cost", "fee"))
        if any(not v.is_finite() or v < 0 for v in (qty, cost, fee)) or qty <= 0:
            raise AccountingError("Nonfinite or invalid execution values")
        pos = holdings.get(symbol, Position(symbol, D(0), D(0)))
        fees += fee
        if side == "buy":
            cash -= cost + fee
            holdings[symbol] = Position(symbol, pos.quantity + qty, pos.cost_eur + cost + fee)
        elif side == "sell":
            if qty > pos.quantity:
                raise AccountingError(f"Sell exceeds owned {symbol} inventory")
            basis = pos.cost_eur * qty / pos.quantity
            pnl = cost - fee - basis
            realized += pnl
            round_pnl[symbol] = round_pnl.get(symbol, D(0)) + pnl
            exit_pnl[txid] = (pnl, pnl / basis * 100 if basis else D(0))
            cash += cost - fee
            remaining = pos.quantity - qty
            holdings[symbol] = Position(symbol, remaining, pos.cost_eur - basis)
            if remaining == 0:
                completed += 1
                wins += int(round_pnl[symbol] > 0)
                losses += int(round_pnl[symbol] < 0)
                round_pnl[symbol] = D(0)
        else:
            raise AccountingError("Unsupported execution side")
    return Ledger(cash, {s: p for s, p in holdings.items() if p.quantity}, realized, fees,
                  wins, losses, completed, exit_pnl)

"""Persistent sampled paper broker. Orders exist locally, never at Kraken.

Passive entry fills require a trade-through proxy; stop paths between samples
are unobservable. This broker is operational practice, not proof of live alpha.
"""
import time
import uuid
from decimal import Decimal

from .models import Book, Market, floor_step

D = Decimal
DEFAULT_MAKER = D("0.004")
DEFAULT_TAKER = D("0.008")
DEFAULT_SLIPPAGE = D("30")


class PaperExchange:
    def __init__(self, public, store, maker=DEFAULT_MAKER, taker=DEFAULT_TAKER, slippage_bps=DEFAULT_SLIPPAGE):
        self.public_api, self.store = public, store
        self.maker, self.taker, self.slippage = maker, taker, slippage_bps / 10000
        self.state = store.get("paper_exchange", {"balances": {"ZEUR": "200"}, "orders": {}, "trades": {}})
        self.market_map = {}

    def save(self):
        self.store.set("paper_exchange", self.state)

    async def markets(self):
        self.market_map = await self.public_api.markets()
        return self.market_map

    async def status(self):
        return await self.public_api.status()

    async def book(self, market):
        return await self.public_api.book(market)

    async def candles(self, market, interval=60, since=None):
        return await self.public_api.candles(market, interval, since)

    async def fees(self, pairs):
        return {s: (self.maker, self.taker) for s, m in self.market_map.items()
                if s in pairs or m.pair in pairs}

    async def balances(self):
        return {a: D(v) for a, v in self.state["balances"].items()}

    async def balance_ex(self):
        hold = D(0)
        for o in (await self.open_orders()).values():
            if o["descr"]["type"] == "buy":
                hold += (D(o["vol"]) - D(o["vol_exec"])) * D(o["descr"]["price"]) * (1 + self.maker)
        return {a: {"balance": b, "hold_trade": str(hold if a == "ZEUR" else 0),
                    "credit": "0", "credit_used": "0"} for a, b in self.state["balances"].items()}

    def expire(self):
        for o in self.state["orders"].values():
            if o["status"] == "open" and o["expiretm"] and o["expiretm"] <= time.time():
                o["status"] = "expired"
                o["closetm"] = time.time()
        self.save()

    async def open_orders(self):
        self.expire()
        return {k: dict(v) for k, v in self.state["orders"].items() if v["status"] == "open"}

    async def closed_orders(self, start):
        self.expire()
        return {k: dict(v) for k, v in self.state["orders"].items() if v["status"] != "open"}

    async def trades(self, start):
        return dict(self.state["trades"])

    async def query_orders(self, ids):
        return {k: dict(v) for k, v in self.state["orders"].items() if k in ids}

    async def add_entry(self, market, qty, limit, trail_pct, userref, expiry, validate=False):
        if validate:
            return None
        oid = "P-" + uuid.uuid4().hex[:16]
        self.state["orders"][oid] = {
            "status": "open", "userref": userref, "refid": None,
            "opentm": time.time(), "expiretm": expiry, "vol": str(qty), "vol_exec": "0",
            "oflags": "post,fciq", "descr": {"pair": market.pair, "type": "buy", "ordertype": "limit",
                "price": str(limit), "close": f"close @ trailing stop +{trail_pct}%"},
            "trail": str(trail_pct), "symbol": market.symbol,
        }
        self.save()
        return oid

    async def cancel_order(self, txid):
        o = self.state["orders"].get(txid)
        if o and o["status"] == "open":
            o["status"] = "canceled"
            o["closetm"] = time.time()
            self.save()
            return {"count": 1}
        return {"count": 0}

    async def add_protection(self, market, qty, stop_price, userref, validate=False):
        if validate:
            return None
        oid = "P-" + uuid.uuid4().hex[:16]
        self.state["orders"][oid] = {"status": "open", "userref": userref, "refid": None,
            "opentm": time.time(), "expiretm": 0, "vol": str(qty), "vol_exec": "0", "oflags": "fciq",
            "symbol": market.symbol, "stopprice": str(stop_price),
            "descr": {"pair": market.pair, "type": "sell", "ordertype": "stop-loss", "price": str(stop_price)}}
        self.save()
        return oid

    def _fill(self, oid: str, order: dict, market: Market, qty: Decimal, price: Decimal, rate: Decimal):
        cost, fee = price * qty, price * qty * rate
        side = order["descr"]["type"]
        balances = self.state["balances"]
        base = D(balances.get(market.base, "0"))
        cash = D(balances["ZEUR"])
        balances[market.base] = str(base + qty if side == "buy" else base - qty)
        balances["ZEUR"] = str(cash - cost - fee if side == "buy" else cash + cost - fee)
        order["vol_exec"] = str(D(order["vol_exec"]) + qty)
        if D(order["vol_exec"]) == D(order["vol"]):
            order["status"] = "closed"
            order["closetm"] = time.time()
        tid = "PT-" + uuid.uuid4().hex
        self.state["trades"][tid] = {"ordertxid": oid, "pair": market.pair, "symbol": market.symbol,
            "type": side, "time": time.time(), "vol": str(qty), "price": str(price),
            "cost": str(cost), "fee": str(fee), "margin": "0"}

    async def advance(self, books: dict[str, Book]):
        now = time.time()
        previous = self.state.get("observed_at", now)
        unobserved_order = any(o["status"] == "open" or previous < o.get("expiretm", 0) <= now
                               for o in self.state["orders"].values())
        if now - previous > 120 and unobserved_order:
            self.state["observation_gaps"] = self.state.get("observation_gaps", 0) + 1
            self.store.event("WARN", f"PAPER observation gap {now - previous:.0f}s: intervening fills/stops "
                             "cannot be reconstructed; results are not continuous execution evidence", True)
        self.state["observed_at"] = now
        self.expire()
        for oid, o in list(self.state["orders"].items()):
            if o["status"] != "open" or o["symbol"] not in books:
                continue
            m, b = self.market_map[o["symbol"]], books[o["symbol"]]
            remaining = D(o["vol"]) - D(o["vol_exec"])
            if o["descr"]["type"] == "buy":
                price = D(o["descr"]["price"])
                # Trade-through proxy plus 10% depth participation, never merely a touch.
                if b.ask > price - m.price_tick:
                    continue
                qty = min(remaining, floor_step(b.asks[0][1] * D("0.1"), m.qty_step))
                if qty <= 0:
                    continue
                self._fill(oid, o, m, qty, price, self.maker)
                child = "P-" + uuid.uuid4().hex[:16]
                trail = D(o["trail"]) / 100
                self.state["orders"][child] = {"status": "open", "refid": oid, "userref": o["userref"],
                    "opentm": time.time(), "expiretm": 0, "vol": str(qty), "vol_exec": "0",
                    "oflags": "fciq", "symbol": m.symbol, "trail": o["trail"], "peak": str(price),
                    "stopprice": str(price * (1 - trail)),
                    "descr": {"pair": m.pair, "type": "sell", "ordertype": "trailing-stop", "price": f"+{o['trail']}%"}}
            else:
                if o["descr"]["ordertype"] == "trailing-stop":
                    peak = max(D(o["peak"]), b.bid)
                    o["peak"] = str(peak)
                    o["stopprice"] = str(peak * (1 - D(o["trail"]) / 100))
                if b.bid <= D(o["stopprice"]) or o.get("triggered"):
                    o["triggered"] = True
                    # Walk observed bids; insufficient depth leaves an open residual.
                    filled, proceeds = D(0), D(0)
                    for price, volume in b.bids:
                        take = min(remaining - filled, volume)
                        filled += take
                        proceeds += take * price
                        if filled == remaining:
                            break
                    if filled:
                        self._fill(oid, o, m, filled, proceeds / filled * (1 - self.slippage), self.taker)
        self.save()

    async def close(self):
        self.save()
        await self.public_api.close()

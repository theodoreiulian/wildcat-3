"""Single-position execution coordinator; the venue is authoritative after every restart."""
import asyncio
import hashlib
import time
from datetime import datetime, timezone
from decimal import Decimal

from .accounting import AccountingError, ledger
from .config import Config
from .models import UNIVERSE, floor_step
from .risk import size_entry

D = Decimal
ACTIVE = {"open", "pending"}


class SafetyError(RuntimeError):
    pass


class Engine:
    def __init__(self, config: Config, store, exchange):
        self.config, self.store, self.exchange = config, store, exchange
        self.markets, self.books, self.fee_map = {}, {}, {}
        self.view = {"mode": config.mode, "status": "STARTING", "equity": "200", "cash": "200",
                     "available": "200", "positions": [], "orders": [], "reasons": {}, "health": "Starting"}
        self.started = time.time()
        self.candle_cache, self.candle_refresh, self.fee_refresh = {}, 0.0, 0.0
        self.open = {}
        self.known = {}
        self.account = ledger({})
        self.last_scan = 0
        self.stopping = False
        self.market_refresh = 0.0
        self.initialized = False

    def symbol(self, pair: str) -> str:
        pair = pair.replace("XBT", "BTC")
        for symbol, market in self.markets.items():
            aliases = {symbol, symbol.replace("/", ""), market.pair.replace("XBT", "BTC")}
            if pair in aliases:
                return symbol
        raise SafetyError(f"Unrecognized execution market: {pair}")

    def halt(self, reason: str):
        if self.store.get("halt") != reason:
            self.store.set("halt", reason)
            self.store.event("CRITICAL", reason, notify=True)
        self.view["status"] = "HALTED"
        self.view["health"] = reason

    async def bootstrap(self):
        old_mode = self.store.get("mode")
        if old_mode is not None and old_mode != self.config.mode:
            raise SafetyError("Cannot share a database between paper and live modes")
        fingerprint = hashlib.sha256(self.config.api_key.encode()).hexdigest() if self.config.mode == "live" else "paper"
        if self.store.get("inception") is not None and self.store.get("account_key", fingerprint) != fingerprint:
            raise SafetyError("Database belongs to a different API key/account; restore its original key")
        self.store.set("mode", self.config.mode)
        self.markets = await self.exchange.markets()
        self.market_refresh = time.time()
        if "BTC/EUR" not in self.markets:
            raise SafetyError("BTC/EUR regime benchmark unavailable")
        if not any(s in self.markets for s in UNIVERSE):
            raise SafetyError("No eligible French EUR niche markets available")
        if self.store.get("inception") is None:
            balances, opened = await self.exchange.balance_ex(), await self.exchange.open_orders()
            if opened:
                raise SafetyError("Initial account must have no open orders")
            self.check_credit(balances)
            cash = D(str(balances.get("ZEUR", balances.get("EUR", {})).get("balance", "0")))
            if cash != D("200") or any(D(str(b["balance"])) != 0 for a, b in balances.items() if a not in {"EUR", "ZEUR"}):
                raise SafetyError("Initial dedicated account must hold exactly EUR 200 and no other assets")
            if self.config.mode == "live":
                if await self.exchange.private("OpenPositions"):
                    raise SafetyError("Initial account has margin positions; a clean spot-only account is required")
            # Failed setup/authentication must not bind a new ledger to a mistyped key.
            with self.store.transaction():
                self.store.set("account_key", fingerprint)
                self.store.set("inception", time.time())
                self.store.set("high_water", "200")
                self.store.add_equity(D("200"))
        await self.refresh_fees()
        await self.reconcile()
        self.initialized = True
        self.store.event("INFO", f"{self.config.mode.upper()} startup reconciled: EUR {self.account.cash:.2f} cash, "
                         f"{len(self.account.positions)} positions, {len(self.open)} working orders; "
                         f"{len(self.store.fills())} executions in ledger", notify=True)

    @staticmethod
    def check_credit(balances):
        if any(D(str(b.get(k, "0"))) != 0 for b in balances.values() for k in ("credit", "credit_used")):
            raise SafetyError("Borrowing or credit detected; only unleveraged spot is allowed")

    async def refresh_fees(self):
        symbols = [s for s in UNIVERSE if s in self.markets]
        self.fee_map = await self.exchange.fees(symbols)
        if any(s not in self.fee_map for s in symbols):
            raise SafetyError("Missing pair-specific maker/taker fee schedule")
        for maker, taker in self.fee_map.values():
            if not (D(0) <= maker <= D("0.05") and D(0) <= taker <= D("0.05")):
                raise SafetyError("Invalid account fee schedule")
        self.fee_refresh = time.time()
        self.store.set("fees", {s: [str(m), str(t)] for s, (m, t) in self.fee_map.items()})

    def owned_orders(self, orders):
        intents = self.store.intents()
        refs = {i["userref"]: "buy" if i["kind"] == "entry" else "sell" for i in intents}
        parent_ids = {i["txid"] for i in intents if i["txid"]}
        # refid is Kraken's documented referral/parent order id; userref is optional on children.
        owned = {k: v for k, v in orders.items()
                 if k in parent_ids or (v.get("userref") in refs
                    and v.get("descr", {}).get("type") == refs[v["userref"]])}
        changed = True
        while changed:
            changed = False
            for k, v in orders.items():
                if k not in owned and v.get("refid") in owned:
                    owned[k] = v
                    changed = True
        return owned

    async def reconcile(self):
        start = float(self.store.get("inception")) - 2
        # Query before and after history: a fill during pagination must never look like a missing stop.
        for attempt in range(3):
            sweep_started = time.time()
            before = await self.exchange.open_orders()
            # Keep history overlap + known txid refresh; history is fully paginated by adapter.
            cursor = max(start, float(self.store.get("history_cursor", start)) - 300)
            unresolved_since = [float(i["created"]) - 5 for i in self.store.intents()
                                if i["status"] in {"SUBMITTING", "UNCERTAIN"}]
            if unresolved_since:
                cursor = max(start, min(cursor, min(unresolved_since)))
            closed = await self.exchange.closed_orders(cursor)
            raw_trades = await self.exchange.trades(cursor)
            extra_ids = {i["txid"] for i in self.store.intents() if i["txid"] and i["status"] != "CLOSED"}
            # A child can fill between ClosedOrders and TradesHistory. Hydrate its
            # metadata directly rather than misclassifying that race as external trading.
            seen = set(self.store.orders()) | set(before) | set(closed)
            extra_ids |= {t["ordertxid"] for t in raw_trades.values() if t["ordertxid"] not in seen}
            extra = await self.exchange.query_orders(sorted(extra_ids)) if extra_ids else {}
            balances = await self.exchange.balance_ex()
            self.view["balances"] = {a: str(b.get("balance", "0")) for a, b in balances.items()}
            after = await self.exchange.open_orders()
            # Set + execution quantity signatures detect both adds and partial executions.
            def signature(orders):
                return {(k, str(v.get("vol_exec", "0")), v.get("status")) for k, v in orders.items()}
            if signature(before) != signature(after):
                continue
            all_orders = {**self.store.orders(), **closed, **extra, **after}
            owned = self.owned_orders(all_orders)
            self.store.upsert_orders(owned)
            self.open, self.known = after, owned
            for intent in self.store.intents():
                matches = [(k, o) for k, o in owned.items() if o.get("userref") == intent["userref"]
                           and o.get("descr", {}).get("type") == ("buy" if intent["kind"] == "entry" else "sell")]
                if len(matches) > 1:
                    raise SafetyError("Multiple parent orders found for one intent")
                if matches:
                    oid, order = matches[0]
                    status = "ACKNOWLEDGED" if order.get("status") in ACTIVE else "CLOSED"
                    self.store.update_intent(intent["userref"], status, oid)
                elif intent["status"] in {"SUBMITTING", "UNCERTAIN"}:
                    # Absence is never proof of rejection. Preserve unresolved intent, including after expiry.
                    self.store.update_intent(intent["userref"], "UNCERTAIN", detail="No authoritative match yet")
            normalized = {}
            for tid, trade in raw_trades.items():
                if trade["ordertxid"] not in owned:
                    raise SafetyError("External execution found in dedicated account; attribution requires review")
                symbol = self.symbol(trade["pair"])
                if symbol not in UNIVERSE or D(str(trade.get("margin", "0"))) != 0:
                    raise SafetyError("Non-niche or margin execution detected")
                normalized[tid] = {**trade, "symbol": symbol}
            merged = {**self.store.fills(), **normalized}
            account = ledger(merged)
            # Executions are facts even when protection is missing. Commit and display
            # exposure BEFORE auditing safety so an alarm never hides a live holding.
            for tid, trade in sorted(normalized.items(), key=lambda x: (float(x[1]["time"]), x[0])):
                pnl, pct = account.exit_pnl.get(tid, (D(0), D(0)))
                label = f"; realized EUR {pnl:+.4f} ({pct:+.2f}%)" if trade["type"] == "sell" else "; realized EUR 0.00 (0.00%; opening)"
                self.store.record_fill(tid, trade, f"{self.config.mode.upper()} {trade['type'].upper()} {trade['symbol']} "
                    f"{trade['vol']} @ EUR {trade['price']}; fee EUR {trade['fee']}{label}")
            self.account = account
            self.publish_account(balances, after)
            # A stable open set can still race with balances after a closed fill; retry inconsistencies.
            try:
                self.audit_balances(account, balances)
                self.audit_orders(account, after, owned)
                for oid, order in owned.items():
                    total = sum((D(str(f["vol"])) for f in merged.values() if f["ordertxid"] == oid), D(0))
                    if total != D(str(order.get("vol_exec", "0"))):
                        raise SafetyError("Order executed volume and trade history disagree")
            except SafetyError:
                if attempt < 2:
                    continue
                raise
            self.store.set("history_cursor", sweep_started - 5)
            for item in self.view["orders"]:
                item["protected"] = item["side"] == "sell"
            self.view["last_reconciled"] = time.time()
            return
        raise SafetyError("Account changed throughout reconciliation; entries suspended")

    def publish_account(self, balances, opened):
        account = self.account
        eur = balances.get("ZEUR", balances.get("EUR", {}))
        self.view.update(cash=str(account.cash),
            available=str(D(str(eur.get("balance", "0"))) - D(str(eur.get("hold_trade", "0")))),
            realized=str(account.realized), fees=str(account.fees),
            wins=account.wins, losses=account.losses, completed=account.completed)
        self.view["positions"] = [{"symbol": s, "quantity": str(p.quantity), "average": str(p.avg_price),
            "value": None, "pnl": None} for s, p in account.positions.items()]
        # Null means unvalued, never a stale or fabricated EUR 200 while exposure exists.
        self.view["equity"] = None if account.positions else str(account.cash)
        self.view["orders"] = [{"id": oid, "symbol": o.get("descr", {}).get("pair", "unknown"),
            "side": o.get("descr", {}).get("type", "unknown"), "type": o.get("descr", {}).get("ordertype", "unknown"),
            "quantity": str(D(o["vol"]) - D(o.get("vol_exec", "0"))),
            "price": o.get("stopprice") or o.get("descr", {}).get("price", "—"),
            "protected": False} for oid, o in opened.items()]

    def audit_balances(self, account, balances):
        self.check_credit(balances)
        eur = D(str(balances.get("ZEUR", balances.get("EUR", {})).get("balance", "0")))
        if abs(eur - account.cash) > D("0.02"):
            raise SafetyError("EUR balance differs from fill ledger (cash movement or fee-currency mismatch)")
        for symbol, market in self.markets.items():
            expected = account.positions[symbol].quantity if symbol in account.positions else D(0)
            actual = D(str(balances.get(market.base, {}).get("balance", "0")))
            if abs(actual - expected) > D("0.0000000001"):
                raise SafetyError(f"{symbol} inventory differs from ledger; possible base-currency fee or external trade")
        allowed = {m.base for m in self.markets.values()} | {"EUR", "ZEUR"}
        if any(D(str(v.get("balance", "0"))) != 0 for a, v in balances.items() if a not in allowed):
            raise SafetyError("Unexpected asset or Earn balance in dedicated account")
        if account.cash < 0:
            raise SafetyError("Negative EUR ledger balance")

    def audit_orders(self, account, opened, owned):
        cover = {}
        pending_symbols = set()
        for oid, order in opened.items():
            if oid not in owned:
                raise SafetyError("Unrecognized open order; bot never cancels external orders")
            descr = order["descr"]
            s = self.symbol(descr["pair"])
            if (s not in UNIVERSE or str(descr.get("leverage", "none")) not in {"none", "0", "", "1"}
                    or order.get("margin") in {True, "true"}):
                raise SafetyError("Order violates niche spot-only boundary")
            if float(order.get("starttm", 0)) > time.time():
                raise SafetyError("Order activation is scheduled in the future")
            remaining = D(order["vol"]) - D(order.get("vol_exec", "0"))
            if descr["type"] == "buy":
                pending_symbols.add(s)
                intent = next((i for i in self.store.intents() if i["txid"] == oid and i["kind"] == "entry"), None)
                if (not intent or D(order["vol"]) != D(intent["quantity"])
                        or D(str(descr.get("price", "0"))) != D(intent["price"])):
                    raise SafetyError("Entry quantity or limit differs from its durable intent")
                if descr["ordertype"] != "limit" or "post" not in order.get("oflags", ""):
                    raise SafetyError("Entry is not a post-only limit")
                if "trailing" not in descr.get("close", "").lower():
                    raise SafetyError("Entry has no exchange-native trailing close")
                expiry = float(order.get("expiretm", 0))
                if expiry <= 0 or expiry > time.time() + self.config.entry_ttl + 10:
                    raise SafetyError("Entry has no short exchange-side expiry")
            elif descr["type"] == "sell":
                if descr["ordertype"] not in {"trailing-stop", "stop-loss"} or order.get("status") != "open":
                    raise SafetyError("Exit is not an active native protective stop")
                if float(order.get("expiretm", 0)) != 0:
                    raise SafetyError("Protective stop expires; unattended holding is unsafe")
                if "fciq" not in order.get("oflags", "").split(","):
                    raise SafetyError("Protective fee currency is not verified as EUR; inspect Kraken before unattended operation")
                intent = next((i for i in self.store.intents()
                               if i["txid"] == (order.get("refid") if descr["ordertype"] == "trailing-stop" else oid)), None)
                if not intent:
                    raise SafetyError("Protective stop has no persisted source intent")
                if descr["ordertype"] == "stop-loss":
                    if intent["kind"] != "protection" or D(str(descr.get("price", "0"))) != D(intent["price"]):
                        raise SafetyError("Emergency stop price differs from persisted protection intent")
                else:
                    price = str(descr.get("price", ""))
                    # Some REST responses include a relative offset in the primary
                    # price/description; others expose only the actual stopprice.
                    import re
                    match = re.search(r"\+([0-9]+(?:\.[0-9]+)?)%", price + " " + descr.get("order", ""))
                    if match:
                        if D(match[1]) != D(intent["trail"]):
                            raise SafetyError("Native trailing distance differs from entry intent")
                    else:
                        # REST does not guarantee a relative-price rendering. The
                        # documented absolute stopprice can verify the loss bound.
                        floor = D(intent["price"]) * (1 - D(intent["trail"]) / 100)
                        stop = D(str(order.get("stopprice", "0")))
                        if stop <= 0 or stop + self.markets[s].price_tick < floor:
                            raise SafetyError("Native stop price does not verify the intended loss bound")
                cover[s] = cover.get(s, D(0)) + remaining
            else:
                raise SafetyError("Unrecognized order side")
        if len(set(account.positions) | pending_symbols) > 1:
            raise SafetyError("More than one active risk position")
        for s in set(cover) | set(account.positions):
            qty = account.positions[s].quantity if s in account.positions else D(0)
            if cover.get(s, D(0)) != qty:
                raise SafetyError(f"CRITICAL: {s} native stop coverage does not equal inventory; inspect Kraken immediately")

    async def cancel_entries(self):
        # Cancel only acknowledged OWN parent buys. Never use cancel-all or cancel child stops.
        owned = self.owned_orders({**self.known, **self.open})
        for oid, order in self.open.items():
            if oid in owned and order.get("descr", {}).get("type") == "buy":
                try:
                    await self.exchange.cancel_order(oid)
                    self.store.event("INFO", f"Requested cancellation of entry {oid}; native child stops retained")
                except Exception as exc:
                    self.store.event("ERROR", f"Entry cancellation unconfirmed ({type(exc).__name__}); GTD expiry remains", True)

    async def recover_protection(self):
        """One durable repair attempt for proven owned, uncovered inventory.

        Reconciliation already retried three complete consistent snapshots. Never
        repair while a parent can still fill, or while any submission is unknown.
        This is best-effort exchange protection, not an offline availability guarantee.
        """
        from .exchange import AmbiguousOrderError, KrakenError
        intents = self.store.intents()
        if any(i["status"] in {"SUBMITTING", "UNCERTAIN"} for i in intents):
            return
        if any(o.get("descr", {}).get("type") == "buy" for o in self.open.values()):
            return  # cancellation/expiry must be authoritatively visible next cycle
        if len(self.account.positions) != 1 or any(k not in self.known for k in self.open):
            return
        for s, pos in self.account.positions.items():
            entries = [i for i in intents if i["symbol"] == s and i["kind"] == "entry"]
            if not entries or any(i["status"] != "CLOSED" for i in entries if i["status"] != "REJECTED"):
                return
            last = entries[-1]
            if any(i["kind"] == "protection" and i["created"] >= last["created"] for i in intents):
                return  # including explicit rejection: do not loop on failing protection
            cover = D(0)
            for order in self.open.values():
                if (self.symbol(order["descr"]["pair"]) != s
                        or order["descr"]["ordertype"] not in {"trailing-stop", "stop-loss"}
                        or order["status"] != "open"):
                    return
                cover += D(order["vol"]) - D(order.get("vol_exec", "0"))
            missing = pos.quantity - cover
            market = self.markets[s]
            if missing <= 0 or floor_step(missing, market.qty_step) != missing:
                return  # never add a competing/oversized sell or round inventory up
            try:
                book = await self.exchange.book(market)
                if time.time() - book.time > self.config.max_book_age:
                    return
                floor = D(last["price"]) * (1 - D(last["trail"]) / 100)
                stop = floor_step(min(floor, book.bid * D("0.999")), market.price_tick)
                if missing < market.min_qty or missing * stop < market.min_cost:
                    self.store.event("CRITICAL", f"{s} uncovered remainder is below exchange minimum; manual Kraken action required", True)
                    return
                ref = self.store.intent(s, missing, stop, D(0), kind="protection")
                try:
                    oid = await self.exchange.add_protection(market, missing, stop, ref)
                    if not oid:
                        raise AmbiguousOrderError("Missing protective order acknowledgement")
                    self.store.update_intent(ref, "ACKNOWLEDGED", oid)
                    self.store.event("CRITICAL", f"Emergency native stop submitted for {missing} {s} at EUR {stop}; "
                                     "entry halt retained pending review", True)
                except AmbiguousOrderError:
                    self.store.update_intent(ref, "UNCERTAIN")
                    self.store.event("CRITICAL", "Emergency stop outcome uncertain; no retry; inspect Kraken immediately", True)
                except KrakenError as exc:
                    self.store.update_intent(ref, "REJECTED", detail=str(exc))
                    self.store.event("CRITICAL", f"Emergency protection rejected: {exc}; inspect Kraken immediately", True)
                except (Exception, asyncio.CancelledError) as exc:
                    self.store.update_intent(ref, "UNCERTAIN", detail=type(exc).__name__)
                    self.store.event("CRITICAL", "Emergency stop outcome unknown; inspect Kraken immediately", True)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
            except Exception as exc:
                self.store.event("CRITICAL", f"Cannot restore protection ({type(exc).__name__}); inspect Kraken immediately", True)

    async def refresh_market_data(self):
        symbols = [s for s in UNIVERSE if s in self.markets]
        results = await asyncio.gather(*(self.exchange.book(self.markets[s]) for s in symbols), return_exceptions=True)
        books = {}
        for s, result in zip(symbols, results, strict=True):
            if isinstance(result, BaseException):
                if s in self.account.positions:
                    raise SafetyError(f"Cannot value held {s}; fresh market data unavailable")
                continue
            if not result.bids or not result.asks or result.bid <= 0 or result.ask <= result.bid:
                continue
            if time.time() - result.time > self.config.max_book_age:
                continue
            books[s] = result
        self.books = books
        for s in self.account.positions:
            if s not in books:
                raise SafetyError(f"Stale or invalid book for held {s}")
        if time.time() - self.candle_refresh > 180:
            all_symbols = symbols + ["BTC/EUR"]
            series = await asyncio.gather(*(self.exchange.candles(self.markets[s]) for s in all_symbols), return_exceptions=True)
            self.candle_cache = {s: c for s, c in zip(all_symbols, series, strict=True) if not isinstance(c, BaseException)}
            self.candle_refresh = time.time()

    def mark_and_breakers(self):
        equity = self.account.cash
        positions = []
        for s, p in self.account.positions.items():
            b = self.books[s]
            taker = self.fee_map[s][1]
            liquidation = p.quantity * b.bid * (1 - taker - self.config.slippage_bps / 10000)
            equity += liquidation
            candles = self.candle_cache.get(s, [])
            day_open = candles[-25].close if len(candles) >= 25 else candles[0].close if candles else None
            change = (float(b.mid) / day_open - 1) * 100 if day_open else None
            positions.append({"symbol": s, "quantity": str(p.quantity), "average": str(p.avg_price),
                              "price": str(b.mid), "value": str(liquidation),
                              "pnl": str(liquidation - p.cost_eur), "change_24h": change})
        self.view.update(equity=str(equity), positions=positions,
                         unrealized=str(sum((D(p["pnl"]) for p in positions), D(0))),
                         price_history={s: [[c.time, c.close] for c in self.candle_cache.get(s, [])[-168:]]
                                        for s in self.account.positions})
        peak = max(equity, D(self.store.get("high_water", "200")))
        self.store.set("high_water", str(peak))
        now = time.time()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if self.store.get("risk_day") != day:
            # Carry forward the last known valuation when asleep over midnight; do not erase offline losses.
            self.store.set("day_start", self.store.get("last_equity", "200"))
            self.store.set("risk_day", day)
        day_start = D(self.store.get("day_start", "200"))
        self.store.set("last_equity", str(equity))
        self.store.add_equity(equity)
        self.view["equity_history"] = self.store.equity_history(240)
        drawdown = 1 - equity / peak if peak else D(0)
        self.view["drawdown"] = str(drawdown * 100)
        if equity <= D("200") * (1 - self.config.drawdown_fraction) or drawdown >= self.config.drawdown_fraction:
            self.halt("Drawdown circuit breaker reached 8%; entries disabled, native stops retained")
        if day_start and (day_start - equity) / day_start >= self.config.daily_loss_fraction:
            self.halt("Daily loss circuit breaker reached 2%; entries disabled, native stops retained")
        return equity

    async def enter(self, signal, qty):
        from .exchange import AmbiguousOrderError, KrakenError
        m = self.markets[signal.symbol]
        price = floor_step(signal.limit_price, m.price_tick)
        ref = self.store.intent(signal.symbol, qty, price, signal.trail_pct)
        self.store.set("last_signal", [signal.symbol, signal.candle_time])
        try:
            oid = await self.exchange.add_entry(m, qty, price, signal.trail_pct, ref,
                                                int(time.time()) + self.config.entry_ttl)
            if not oid:
                raise AmbiguousOrderError("Missing order id")
            self.store.update_intent(ref, "ACKNOWLEDGED", oid)
            self.store.event("ORDER", f"{self.config.mode.upper()} posted {signal.symbol} {qty} @ EUR {price}; "
                             f"native trail {signal.trail_pct}%, expiry {self.config.entry_ttl}s")
        except AmbiguousOrderError as exc:
            self.store.update_intent(ref, "UNCERTAIN", detail=type(exc).__name__)
            self.store.event("CRITICAL", "Order submission outcome uncertain; no retries, reconciliation required", True)
        except KrakenError as exc:
            self.store.update_intent(ref, "REJECTED", detail=str(exc))
            self.store.event("WARN", f"Entry rejected: {exc}")
        except (Exception, asyncio.CancelledError) as exc:
            self.store.update_intent(ref, "UNCERTAIN", detail=type(exc).__name__)
            self.store.event("CRITICAL", "Order submission outcome uncertain; no retries, reconciliation required", True)
            if isinstance(exc, asyncio.CancelledError):
                raise
        # Always return to venue reconciliation before another decision.

    async def step(self):
        from .strategy import evaluate
        try:
            if not self.initialized:
                await self.bootstrap()
            # Private execution/protection monitoring must survive public-data failures.
            await self.reconcile()
            status = await self.exchange.status()
            if status != "online":
                self.view.update(status="DEGRADED", health=f"Kraken system status: {status}")
                await self.cancel_entries()
                return
            if time.time() - self.market_refresh > 3600:
                self.markets = await self.exchange.markets()
                self.market_refresh = time.time()
            if time.time() - self.fee_refresh > 1800:
                await self.refresh_fees()
            await self.refresh_market_data()
            if self.config.mode == "paper":
                await self.exchange.advance(self.books)
                await self.reconcile()
            equity = self.mark_and_breakers()
            if self.store.get("halt"):
                self.view.update(status="HALTED", health=self.store.get("halt"))
                await self.cancel_entries()
                return
            unresolved = any(i["status"] in {"SUBMITTING", "UNCERTAIN"} for i in self.store.intents())
            if unresolved:
                self.view.update(status="UNCERTAIN", health="Unresolved order intent; trading blocked until authoritative reconciliation")
                return
            self.view.update(status="PROTECTED" if self.account.positions else "SCANNING", health="Kraken connected; reconciliation passed")
            if self.account.positions or self.open or self.stopping:
                return
            now = time.time()
            intents = [i for i in self.store.intents() if i["kind"] == "entry"]
            if intents and now - intents[-1]["created"] < self.config.cooldown_seconds:
                self.view["status"] = "COOLDOWN"
                return
            day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if sum(i["created"] >= day_start for i in intents) >= self.config.max_entries_per_day:
                self.view["status"] = "DAILY ENTRY LIMIT"
                return
            if now - self.last_scan < 60:
                return
            self.last_scan = now
            maker = max(f[0] for f in self.fee_map.values())
            taker = max(f[1] for f in self.fee_map.values())
            signals, reasons = evaluate(self.markets, self.candle_cache, self.books,
                self.candle_cache.get("BTC/EUR", []), now, maker, taker)
            self.view["reasons"] = reasons
            for signal in signals:
                if self.store.get("last_signal") == [signal.symbol, signal.candle_time]:
                    continue
                b = self.books[signal.symbol]
                # Refetch at submission to reject moved prices and stale opportunities.
                latest = await self.exchange.book(self.markets[signal.symbol])
                if (time.time() - latest.time > self.config.max_book_age or latest.ask <= signal.limit_price
                        or abs(latest.mid / b.mid - 1) > D("0.001")):
                    continue
                qty = size_entry(self.config, self.markets[signal.symbol], signal, latest, equity,
                                 D(self.view["available"]), *self.fee_map[signal.symbol])
                if qty <= 0:
                    reasons[signal.symbol] = "Risk-sized order is below venue minimum or depth limit"
                    continue
                await self.enter(signal, qty)
                await self.reconcile()
                break
        except (SafetyError, AccountingError) as exc:
            self.halt(str(exc))
            await self.cancel_entries()
            if "native stop coverage does not equal inventory" in str(exc):
                await self.recover_protection()
        except Exception as exc:
            # Transport faults do not imply an order was rejected. Existing exchange risk orders remain active.
            self.view.update(status="DEGRADED", health=f"API/data failure: {type(exc).__name__}; entries suspended")
            if time.time() - self.store.get("last_api_alert", 0) > 300:
                self.store.event("ERROR", self.view["health"], True)
                self.store.set("last_api_alert", time.time())
        finally:
            self.view["uptime"] = time.time() - self.started
            self.view["updated"] = time.time()
            self.store.set("dashboard", self.view)

    async def shutdown(self):
        self.stopping = True
        try:
            await self.reconcile()
            await self.cancel_entries()
            self.store.event("INFO", "Orderly shutdown: entry cancellation requests completed; native protective orders retained", True)
        except Exception as exc:
            self.store.event("CRITICAL", f"Shutdown reconciliation unconfirmed ({type(exc).__name__}); check Kraken", True)
        self.view["status"] = "STOPPED"
        self.store.set("dashboard", self.view)

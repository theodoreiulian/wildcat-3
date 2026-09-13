"""Small, deliberately restricted Kraken Spot REST adapter.

Order submission is never retried: a missing acknowledgement is an unknown
exchange state, not a rejected order. The caller must persist its intent before
calling ``add_entry`` and reconcile an ``AmbiguousOrderError`` before continuing.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from .models import Book, Candle, Market

TRADING_SYMBOLS = frozenset({"TAO/EUR", "RENDER/EUR", "AKT/EUR", "FET/EUR"})
DATA_SYMBOLS = TRADING_SYMBOLS | {"BTC/EUR"}
_TXID = re.compile(r"O[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{6}\Z")
_READ_METHODS = frozenset({
    "Balance", "BalanceEx", "OpenOrders", "ClosedOrders", "QueryOrders",
    "TradesHistory", "QueryTrades", "TradeVolume", "OpenPositions", "Ledgers",
    "QueryLedgers", "GetApiKeyInfo", "CreditLines",
})
_REST_COST = {"TradesHistory": 2, "Ledgers": 2, "ClosedOrders": 2,
              "AddOrder": 0, "CancelOrder": 0}


class KrakenError(RuntimeError):
    """A definite exchange rejection or a failed read; messages contain no secrets."""


class AmbiguousOrderError(KrakenError):
    """An order mutation may have reached Kraken; reconcile without resubmitting."""


class RestRateLimiter:
    """Conservative Starter-tier token bucket for one dedicated API key.

    Kraken's documented REST counter allows 15 credits and decays 0.33/sec.
    A burst of six and 0.3/sec replenishment leave room for discrete server decay.
    Add/cancel use Kraken's separate matching-engine limit and cost zero here.
    Source: https://docs.kraken.com/exchange/guides/rest/ratelimits
    """

    def __init__(self, capacity: float = 6.0, rate: float = 0.3, *,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None) -> None:
        if (not math.isfinite(capacity) or not math.isfinite(rate)
                or capacity < 2 or rate <= 0):
            raise ValueError("Rate limiter requires capacity >= 2 and a positive finite rate")
        self._capacity, self._rate = capacity, rate
        self._clock, self._sleep = clock or time.monotonic, sleep or asyncio.sleep
        self._updated = self._clock()
        self._tokens = capacity
        self._lock = asyncio.Lock()

    async def acquire(self, cost: int) -> None:
        if isinstance(cost, bool) or not isinstance(cost, int) or not 0 <= cost <= self._capacity:
            raise ValueError("Invalid REST request cost")
        if cost == 0:
            return
        async with self._lock:
            while True:
                now = self._clock()
                elapsed = max(0.0, now - self._updated)
                self._updated = max(now, self._updated)
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens + 1e-12 >= cost:
                    self._tokens = max(0.0, self._tokens - cost)
                    return
                await self._sleep((cost - self._tokens) / self._rate)


class Exchange(Protocol):
    async def markets(self) -> dict[str, Market]: ...
    async def status(self) -> str: ...
    async def book(self, market: Market) -> Book: ...
    async def candles(self, market: Market, interval: int = 60,
                      since: int | None = None) -> list[Candle]: ...
    async def balance_ex(self) -> dict[str, Any]: ...
    async def balances(self) -> dict[str, Decimal]: ...
    async def open_orders(self) -> dict[str, Any]: ...
    async def closed_orders(self, start: float) -> dict[str, Any]: ...
    async def trades(self, start: float) -> dict[str, Any]: ...
    async def query_orders(self, ids: list[str]) -> dict[str, Any]: ...
    async def fees(self, pairs: list[str]) -> dict[str, tuple[Decimal, Decimal]]: ...
    async def add_entry(self, market: Market, qty: Decimal, limit: Decimal,
                        trail_pct: Decimal, userref: int, expiry: int,
                        validate: bool = False) -> str | None: ...
    async def add_protection(self, market: Market, qty: Decimal, stop_price: Decimal,
                             userref: int, validate: bool = False) -> str | None: ...
    async def cancel_order(self, txid: str) -> dict[str, Any]: ...
    async def close(self) -> None: ...


def kraken_signature(path: str, data: Mapping[str, Any], secret: str) -> str:
    """Sign the exact URL-encoded payload, including encoded '+' characters."""
    try:
        key = base64.b64decode(secret, validate=True)
    except (ValueError, binascii.Error):
        raise KrakenError("Kraken API secret is not valid base64") from None
    if not key:
        raise KrakenError("Kraken API secret is empty")
    encoded = (str(data["nonce"]) + urlencode(data)).encode("utf-8")
    message = path.encode("utf-8") + hashlib.sha256(encoded).digest()
    return base64.b64encode(hmac.new(key, message, hashlib.sha512).digest()).decode()


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise KrakenError(f"Invalid numeric {label} in Kraken data") from None
    if not result.is_finite() or (positive and result <= 0):
        raise KrakenError(f"Invalid numeric {label} in Kraken data")
    return result


def _symbol(pair: str) -> str | None:
    compact = pair.upper().replace("/", "")
    if compact in {"XBTEUR", "XXBTZEUR", "BTCEUR"}:
        return "BTC/EUR"
    return next((s for s in TRADING_SYMBOLS if s.replace("/", "") == compact), None)


class Kraken:
    """Kraken international Spot only. One instance per dedicated API key.

    Private requests are serialized through completion to preserve nonce order.
    Supply a persisted ``nonce`` callback for monotonicity across process restarts;
    the in-memory fallback is intended for public data and isolated tests only.
    """

    def __init__(self, api_key: str = "", api_secret: str = "",
                 nonce: Callable[[], int] | None = None,
                 client: httpx.AsyncClient | None = None,
                 rate_limiter: Callable[[int], Awaitable[None]] | None = None) -> None:
        self._key = api_key
        self._secret = api_secret
        self._nonce_source = nonce or (lambda: time.time_ns() // 1_000_000)
        self._last_nonce = 0
        self._private_lock = asyncio.Lock()
        self._rate_limit = rate_limiter or RestRateLimiter().acquire
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
            headers={"User-Agent": "WildCat-3/1.0"},
        )
        self._markets: dict[str, Market] = {}
        self._aliases: dict[str, str] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _safe_error(self, errors: list[str]) -> str:
        # Do not echo arbitrary API payload details (which may contain user data).
        safe = []
        for error in errors:
            for secret in (self._key, self._secret):
                if secret:
                    error = error.replace(secret, "[redacted]")
            match = re.match(r"[EW][A-Za-z]+:[A-Za-z /-]+", error)
            safe.append(match.group(0).strip()[:100] if match else "Exchange error")
        return "; ".join(safe)

    def _decode(self, response: httpx.Response, method: str,
                mutation: bool = False) -> dict[str, Any]:
        error_cls = AmbiguousOrderError if mutation else KrakenError
        if not 200 <= response.status_code < 300:
            raise error_cls(f"Kraken {method}: HTTP {response.status_code}; "
                            + ("reconciliation required" if mutation else "read failed"))
        try:
            # BalanceEx may encode monetary values as JSON numbers, not strings.
            # Parse them directly into Decimal before any binary-float rounding.
            payload = json.loads(response.content, parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise error_cls(f"Kraken {method}: malformed response") from None
        if (not isinstance(payload, dict) or not isinstance(payload.get("error"), list)
                or not all(isinstance(e, str) for e in payload["error"])):
            raise error_cls(f"Kraken {method}: invalid response envelope")
        errors = [e for e in payload["error"] if not e.startswith("W")]
        if errors:
            # An anomalous response containing an acknowledgement AND errors is
            # uncertain even though usual Kraken error envelopes are definite.
            if mutation and payload.get("result"):
                raise AmbiguousOrderError(f"Kraken {method}: contradictory response")
            raise KrakenError(f"Kraken {method}: {self._safe_error(errors)}")
        if not isinstance(payload.get("result"), dict):
            raise error_cls(f"Kraken {method}: missing result")
        return payload["result"]

    async def public(self, method: str,
                     params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z]+", method):
            raise KrakenError("Invalid Kraken public method")
        for attempt in range(3):
            try:
                response = await self._client.get(
                    f"https://api.kraken.com/0/public/{method}", params=params,
                    follow_redirects=False,
                )
            except httpx.RequestError:
                if attempt == 2:
                    raise KrakenError(f"Kraken {method}: network read failed") from None
            else:
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                    return self._decode(response, method)
            await asyncio.sleep(0.25 * 2 ** attempt)
        raise KrakenError(f"Kraken {method}: unavailable")  # defensive exhaustiveness

    async def private(self, method: str,
                      params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if method not in _READ_METHODS | {"AddOrder", "CancelOrder"}:
            raise KrakenError("Kraken private method is outside the Spot safety allowlist")
        if not self._key or not self._secret:
            raise KrakenError("Kraken API credentials are required")
        if params and "nonce" in params:
            raise KrakenError("Caller must not override the persisted nonce")
        params = dict(params or {})
        mutation = method in {"AddOrder", "CancelOrder"}
        async with self._private_lock:
            # Apply the same financial boundary to the lower-level entry point;
            # an accidental direct private('AddOrder', ...) must not bypass it.
            if method == "AddOrder":
                self._validate_order_payload(params)
            elif method == "CancelOrder" and (
                    set(params) != {"txid"} or not isinstance(params.get("txid"), str)
                    or not _TXID.fullmatch(params["txid"])):
                raise KrakenError("CancelOrder requires one Kraken order transaction ID")
            await self._rate_limit(_REST_COST.get(method, 1))
            candidate = self._nonce_source()
            if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
                raise KrakenError("Kraken nonce source must return a positive integer")
            nonce = max(candidate, self._last_nonce + 1)
            if nonce >= 2 ** 64:
                raise KrakenError("Kraken nonce exceeds unsigned 64-bit range")
            self._last_nonce = nonce
            payload = {"nonce": str(nonce), **dict(params or {})}
            # Kraken form booleans use lowercase; sign the exact body sent below.
            payload = {k: str(v).lower() if isinstance(v, bool) else v
                       for k, v in payload.items()}
            path = f"/0/private/{method}"
            signature = kraken_signature(path, payload, self._secret)
            try:
                response = await self._client.post(
                    "https://api.kraken.com" + path,
                    content=urlencode(payload), follow_redirects=False,
                    headers={"API-Key": self._key, "API-Sign": signature,
                             "Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.RequestError:
                error_cls = AmbiguousOrderError if mutation else KrakenError
                raise error_cls(f"Kraken {method}: network failure"
                                + ("; reconciliation required" if mutation else "")) from None
            return self._decode(response, method, mutation)

    @staticmethod
    def _validate_order_payload(params: Mapping[str, Any]) -> None:
        protection = params.get("type") == "sell" and params.get("ordertype") == "stop-loss"
        allowed = {"pair", "type", "ordertype", "volume", "price", "userref", "oflags",
                   "timeinforce", "trigger", "stptype", "validate"}
        if not protection:
            allowed |= {"expiretm", "close[ordertype]", "close[price]"}
        required = allowed - {"trigger", "stptype", "validate"}
        if not required <= set(params) or not set(params) <= allowed:
            raise KrakenError("AddOrder parameters are outside the protected spot-order contract")
        if (not isinstance(params["pair"], str) or _symbol(params["pair"]) not in TRADING_SYMBOLS
                or params.get("trigger", "last") != "last"
                or params.get("stptype", "cancel-newest") != "cancel-newest"):
            raise KrakenError("AddOrder is restricted to protected niche EUR spot orders")
        if protection:
            if params["oflags"] != "fciq" or params["timeinforce"] != "GTC":
                raise KrakenError("Protective stops must use GTC and quote-currency fees")
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", str(params["price"])):
                raise KrakenError("Protective stop requires an absolute price")
        else:
            if (params["type"] != "buy" or params["ordertype"] != "limit"
                    or params["oflags"] not in ("post,fciq", "fciq,post")
                    or params["timeinforce"] != "GTD"
                    or params["close[ordertype]"] != "trailing-stop"):
                raise KrakenError("Entries must be passive spot buys with native trailing protection")
            trail = params["close[price]"]
            if not isinstance(trail, str) or not re.fullmatch(r"\+[0-9]+(?:\.[0-9]+)?%", trail):
                raise KrakenError("AddOrder requires a positive percentage trailing stop")
            if not 0 < _decimal(trail[1:-1], "trailing stop") < 100:
                raise KrakenError("AddOrder trailing stop must be between zero and 100 percent")
            expiry = str(params["expiretm"])
            if not expiry.isdecimal() or not 5 <= int(expiry) - time.time() <= 120:
                raise KrakenError("AddOrder expiry must be an absolute timestamp 5–120 seconds ahead")
        _decimal(params["volume"], "order quantity", positive=True)
        _decimal(params["price"], "order price", positive=True)
        reference = str(params["userref"])
        if not reference.isdecimal() or not 0 < int(reference) < 2 ** 31:
            raise KrakenError("AddOrder userref must be a positive signed 32-bit integer")
        if str(params.get("validate", False)).lower() not in {"true", "false"}:
            raise KrakenError("AddOrder validate must be a boolean")

    async def markets(self) -> dict[str, Market]:
        result = await self.public("AssetPairs", {
            "country_code": "FR", "execution_venue": "international",
            "aclass_base": "currency", "info": "info",
        })
        markets: dict[str, Market] = {}
        aliases: dict[str, str] = {}
        for pair_id, raw in result.items():
            if not isinstance(raw, dict):
                raise KrakenError("Malformed Kraken market metadata")
            symbol = _symbol(str(raw.get("wsname", raw.get("altname", pair_id))))
            if symbol not in DATA_SYMBOLS or raw.get("quote") not in {"EUR", "ZEUR"}:
                continue
            if raw.get("aclass_base", "currency") != "currency":
                continue
            try:
                lot_decimals = int(raw["lot_decimals"])
                pair_decimals = int(raw["pair_decimals"])
                if not 0 <= lot_decimals <= 18 or not 0 <= pair_decimals <= 18:
                    raise ValueError
                market = Market(
                    symbol=symbol, pair=str(raw["altname"]), base=str(raw["base"]),
                    quote=str(raw["quote"]),
                    price_tick=_decimal(raw.get("tick_size", Decimal(10) ** -pair_decimals),
                                        "price tick", positive=True),
                    qty_step=Decimal(10) ** -lot_decimals,
                    min_qty=_decimal(raw["ordermin"], "minimum quantity", positive=True),
                    min_cost=_decimal(raw["costmin"], "minimum cost", positive=True),
                    status=str(raw["status"]),
                )
            except (KeyError, TypeError, ValueError):
                raise KrakenError("Incomplete Kraken niche-market metadata") from None
            if _symbol(market.pair) != symbol:
                raise KrakenError("Conflicting Kraken market identity")
            if symbol in markets:
                raise KrakenError("Duplicate Kraken niche-market identity")
            markets[symbol] = market
            for alias in (pair_id, raw.get("wsname", ""), market.pair, symbol):
                if alias:
                    aliases[str(alias).upper()] = symbol
        self._markets, self._aliases = markets, aliases
        return dict(markets)

    async def status(self) -> str:
        result = await self.public("SystemStatus")
        if not isinstance(result.get("status"), str):
            raise KrakenError("Missing Kraken system status")
        return result["status"]

    @staticmethod
    def _pair_result(result: dict[str, Any], endpoint: str) -> Any:
        values = [v for k, v in result.items() if k != "last"]
        if len(values) != 1:
            raise KrakenError(f"Kraken {endpoint}: expected one pair")
        return values[0]

    async def book(self, market: Market) -> Book:
        raw = self._pair_result(await self.public("Depth", {"pair": market.pair, "count": 100}),
                                "Depth")
        try:
            sides = []
            for side in ("bids", "asks"):
                levels = tuple((_decimal(r[0], "book price", positive=True),
                                _decimal(r[1], "book quantity", positive=True)) for r in raw[side])
                if not levels:
                    raise ValueError
                prices = [r[0] for r in levels]
                if prices != sorted(prices, reverse=side == "bids"):
                    raise ValueError
                sides.append(levels)
            if sides[0][0][0] >= sides[1][0][0]:
                raise ValueError
            return Book(market.symbol, time.time(), sides[0], sides[1])
        except (KeyError, TypeError, ValueError, IndexError):
            raise KrakenError("Kraken Depth: missing, crossed or unordered book") from None

    async def candles(self, market: Market, interval: int = 60,
                      since: int | None = None) -> list[Candle]:
        if interval not in {1, 5, 15, 30, 60, 240, 1440, 10080, 21600}:
            raise KrakenError("Unsupported Kraken candle interval")
        params: dict[str, Any] = {"pair": market.pair, "interval": interval}
        if since is not None:
            params["since"] = int(since)
        raw = self._pair_result(await self.public("OHLC", params), "OHLC")
        if not isinstance(raw, list):
            raise KrakenError("Kraken OHLC: malformed candles")
        candles: list[Candle] = []
        try:
            # Kraken always appends an uncommitted running interval, even if
            # since is later than that candle. It must NEVER enter our signals.
            for row in raw[:-1]:
                candle = Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                                float(row[4]), float(row[6]))
                vals = (candle.open, candle.high, candle.low, candle.close, candle.volume)
                if (not all(math.isfinite(v) for v in vals) or candle.volume < 0
                        or candle.low <= 0 or candle.low > min(candle.open, candle.close)
                        or candle.high < max(candle.open, candle.close)
                        or (candles and candle.time <= candles[-1].time)):
                    raise ValueError
                candles.append(candle)
        except (IndexError, TypeError, ValueError, OverflowError):
            raise KrakenError("Kraken OHLC: invalid candle values") from None
        return candles

    async def balance_ex(self) -> dict[str, Any]:
        result = await self.private("BalanceEx")
        for balance in result.values():
            if not isinstance(balance, dict) or "balance" not in balance:
                raise KrakenError("Kraken BalanceEx: malformed balance")
            for field in ("balance", "credit", "credit_used", "hold_trade"):
                _decimal(balance.get(field, "0"), field)
        return result

    async def balances(self) -> dict[str, Decimal]:
        """Total owned balances, including held funds; no credit is added."""
        return {asset: _decimal(raw["balance"], "balance")
                for asset, raw in (await self.balance_ex()).items()}

    async def open_orders(self) -> dict[str, Any]:
        result = await self.private("OpenOrders", {"trades": True})
        return self._records(result.get("open"), "OpenOrders")

    @staticmethod
    def _records(raw: Any, method: str) -> dict[str, Any]:
        if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
            raise KrakenError(f"Kraken {method}: malformed records")
        return raw

    async def _history(self, method: str, key: str, start: float,
                       extras: Mapping[str, Any]) -> dict[str, Any]:
        if not math.isfinite(start) or start < 0:
            raise KrakenError("History start must be a finite positive timestamp")
        # Freeze the window while paging. Overlap one second because Kraken's
        # start is exclusive and persisted timestamps may be fractional.
        params = {"start": max(0, int(start) - 1), "end": int(time.time()), **extras}
        offset = 0
        expected_count: int | None = None
        records: dict[str, Any] = {}
        while True:
            result = await self.private(method, {**params, "ofs": offset})
            page = self._records(result.get(key), method)
            count = result.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise KrakenError(f"Kraken {method}: missing history count")
            if expected_count is not None and count != expected_count:
                raise KrakenError(f"Kraken {method}: history changed during pagination")
            expected_count = count
            if records.keys() & page.keys():
                raise KrakenError(f"Kraken {method}: repeated history page")
            records.update(page)
            offset += len(page)
            if offset == count:
                return records
            if not page or offset > count:
                raise KrakenError(f"Kraken {method}: incomplete history")

    async def closed_orders(self, start: float) -> dict[str, Any]:
        return await self._history("ClosedOrders", "closed", start,
                                   {"trades": True, "closetime": "both",
                                    "consolidate_taker": False})

    async def trades(self, start: float) -> dict[str, Any]:
        return await self._history("TradesHistory", "trades", start,
                                   {"type": "all", "consolidate_taker": False,
                                    "ledgers": True})

    async def query_orders(self, ids: list[str]) -> dict[str, Any]:
        unique = list(dict.fromkeys(ids))
        if any(not _TXID.fullmatch(txid) for txid in unique):
            raise KrakenError("QueryOrders requires Kraken order transaction IDs")
        orders: dict[str, Any] = {}
        for i in range(0, len(unique), 50):
            result = await self.private("QueryOrders", {
                "txid": ",".join(unique[i:i + 50]), "trades": True,
                "consolidate_taker": False,
            })
            orders.update(self._records(result, "QueryOrders"))
        return orders

    async def fees(self, pairs: list[str]) -> dict[str, tuple[Decimal, Decimal]]:
        if not pairs:
            return {}
        symbols: list[str] = []
        rest_pairs: list[str] = []
        for pair in pairs:
            symbol = self._aliases.get(pair.upper()) or _symbol(pair)
            if symbol not in DATA_SYMBOLS:
                raise KrakenError("Fee query pair is outside the EUR data allowlist")
            symbols.append(symbol)
            market = self._markets.get(symbol)
            rest_pairs.append(market.pair if market else
                              ("XBTEUR" if symbol == "BTC/EUR" else symbol.replace("/", "")))
        result = await self.private("TradeVolume", {"pair": ",".join(dict.fromkeys(rest_pairs)),
                                                     "fee-info": True})
        makers = self._records(result.get("fees_maker"), "TradeVolume maker fees")
        takers = self._records(result.get("fees"), "TradeVolume taker fees")
        normalized: dict[str, dict[str, Decimal]] = {}
        for side, records in (("maker", makers), ("taker", takers)):
            for pair, raw in records.items():
                symbol = self._aliases.get(pair.upper()) or _symbol(pair)
                if symbol in symbols:
                    rate = _decimal(raw.get("fee"), "fee rate") / 100
                    if not Decimal(0) <= rate <= Decimal("0.05"):
                        raise KrakenError("Kraken TradeVolume: unsupported fee rate")
                    normalized.setdefault(symbol, {})[side] = rate
        if any(set(normalized.get(symbol, {})) != {"maker", "taker"} for symbol in symbols):
            raise KrakenError("Kraken TradeVolume: missing maker/taker schedule for requested pair")
        return {s: (normalized[s]["maker"], normalized[s]["taker"]) for s in symbols}

    @staticmethod
    def _validate_market_order(market: Market, qty: Decimal,
                               price: Decimal) -> tuple[Decimal, Decimal]:
        if (market.symbol not in TRADING_SYMBOLS or market.quote not in {"EUR", "ZEUR"}
                or _symbol(market.pair) != market.symbol
                or market.base != market.symbol.split("/")[0]):
            raise KrakenError("Orders are restricted to decentralized AI EUR spot pairs")
        if market.status != "online":
            raise KrakenError("Order market is not online")
        qty = _decimal(qty, "order quantity", positive=True)
        price = _decimal(price, "order price", positive=True)
        tick = _decimal(market.price_tick, "price tick", positive=True)
        step = _decimal(market.qty_step, "quantity step", positive=True)
        if qty % step != 0 or price % tick != 0:
            raise KrakenError("Order violates Kraken price or quantity precision")
        if qty < market.min_qty or qty * price < market.min_cost:
            raise KrakenError("Order is below Kraken minimum quantity or cost")
        return qty, price

    async def add_entry(self, market: Market, qty: Decimal, limit: Decimal,
                        trail_pct: Decimal, userref: int, expiry: int,
                        validate: bool = False) -> str | None:
        qty, limit = self._validate_market_order(market, qty, limit)
        trail_pct = _decimal(trail_pct, "trailing stop", positive=True)
        if trail_pct >= 100:
            raise KrakenError("Trailing stop percentage must be below 100")
        if isinstance(userref, bool) or not isinstance(userref, int) or not 0 < userref < 2 ** 31:
            raise KrakenError("Entry userref must be a positive signed 32-bit integer")
        if (isinstance(expiry, bool) or not isinstance(expiry, int)
                or not 5 <= expiry - time.time() <= 120):
            raise KrakenError("Entry expiry must be an absolute timestamp 5–120 seconds ahead")
        result = await self.private("AddOrder", {
            "pair": market.pair, "type": "buy", "ordertype": "limit",
            "volume": format(qty, "f"), "price": format(limit, "f"),
            "userref": userref, "oflags": "post,fciq", "timeinforce": "GTD",
            "expiretm": str(expiry), "trigger": "last", "stptype": "cancel-newest",
            "close[ordertype]": "trailing-stop",
            "close[price]": "+" + format(trail_pct, "f") + "%", "validate": validate,
        })
        return self._order_acknowledgement(result, validate)

    async def add_protection(self, market: Market, qty: Decimal, stop_price: Decimal,
                             userref: int, validate: bool = False) -> str | None:
        """Repair confirmed missing coverage with one exchange-side spot stop.

        Caller must durably reserve this quantity and prove missing protection
        after the entry is terminal. An uncertain response must never be retried.
        """
        qty, stop_price = self._validate_market_order(market, qty, stop_price)
        result = await self.private("AddOrder", {
            "pair": market.pair, "type": "sell", "ordertype": "stop-loss",
            "volume": format(qty, "f"), "price": format(stop_price, "f"),
            "userref": userref, "oflags": "fciq", "timeinforce": "GTC",
            "trigger": "last", "stptype": "cancel-newest", "validate": validate,
        })
        return self._order_acknowledgement(result, validate)

    @staticmethod
    def _order_acknowledgement(result: dict[str, Any], validate: bool) -> str | None:
        txids = result.get("txid")
        if validate:
            if txids:
                raise AmbiguousOrderError("Kraken AddOrder validation unexpectedly returned an order ID")
            if not isinstance(result.get("descr"), dict):
                raise KrakenError("Kraken AddOrder validation acknowledgement is malformed")
            return None
        if (not isinstance(txids, list) or len(txids) != 1
                or not isinstance(txids[0], str) or not _TXID.fullmatch(txids[0])):
            raise AmbiguousOrderError("Kraken AddOrder: no unique order acknowledgement; reconcile")
        return txids[0]

    async def cancel_order(self, txid: str) -> dict[str, Any]:
        """Cancel one explicitly identified entry. Never call on a protective stop."""
        if not isinstance(txid, str) or not _TXID.fullmatch(txid):
            raise KrakenError("CancelOrder requires one Kraken order transaction ID")
        result = await self.private("CancelOrder", {"txid": txid})
        if result.get("count") not in {0, 1}:
            raise AmbiguousOrderError("Kraken CancelOrder: invalid cancellation acknowledgement")
        return result

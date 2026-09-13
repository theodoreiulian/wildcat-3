"""Fault injection at the HTTP boundary, including Kraken's published auth vector."""

import asyncio
import base64
import time
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from wildcat3.exchange import AmbiguousOrderError, Kraken, KrakenError, RestRateLimiter, kraken_signature
from wildcat3.models import Market

MARKET = Market("TAO/EUR", "TAOEUR", "TAO", "ZEUR", D("0.01"), D("0.00001"),
                D("0.01"), D("0.45"))
ORDER_ID = "OUF4EM-FRGI2-MQMWZD"
KEY = "test-public-key"
SECRET = base64.b64encode(b"test-secret").decode()


async def unlimited(_cost):
    """HTTP tests inject an explicit no-wait limiter; live code always limits."""


def response(result=None, errors=None):
    return httpx.Response(200, json={"error": errors or [], "result": result or {}})


class SignatureTests(unittest.TestCase):
    def test_official_kraken_signature_vector(self):
        # https://docs.kraken.com/exchange/guides/rest/authentication
        secret = ("kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3"
                  "pd5nE9qa99HAZtuZuj6F1huXg==")
        data = {"nonce": "1616492376594", "ordertype": "limit", "pair": "XBTUSD",
                "price": 37500, "type": "buy", "volume": 1.25}
        self.assertEqual(
            kraken_signature("/0/private/AddOrder", data, secret),
            "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==",
        )

    def test_invalid_secret_is_redacted(self):
        with self.assertRaises(KrakenError) as ctx:
            kraken_signature("/0/private/Balance", {"nonce": "1"}, "private$material")
        self.assertNotIn("private$material", str(ctx.exception))


class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()

    def exchange(self, handler, nonce=lambda: 100, rate_limiter=unlimited):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return Kraken(KEY, SECRET, nonce=nonce, client=client, rate_limiter=rate_limiter)

    async def submit(self, exchange, **kwargs):
        values = {"market": MARKET, "qty": D("0.1"), "limit": D("250.00"), "trail_pct": D("3.50"),
                  "userref": 42, "expiry": int(time.time()) + 60}
        values.update(kwargs)
        return await exchange.add_entry(**values)

    async def test_entry_encodes_atomic_trailing_close_and_post_only(self):
        captured = []

        def handler(request):
            captured.append(request)
            return response({"txid": [ORDER_ID]})

        self.assertEqual(await self.submit(self.exchange(handler)), ORDER_ID)
        self.assertEqual(len(captured), 1)
        request = captured[0]
        fields = {k: values[0] for k, values in parse_qs(request.content.decode()).items()}
        self.assertEqual(fields["close[price]"], "+3.50%")
        self.assertIn(b"%2B3.50%25", request.content)
        self.assertEqual(fields["close[ordertype]"], "trailing-stop")
        self.assertEqual(fields["type"], "buy")
        self.assertEqual(fields["ordertype"], "limit")
        self.assertEqual(fields["oflags"], "post,fciq")
        self.assertEqual(fields["timeinforce"], "GTD")
        self.assertEqual(fields["validate"], "false")
        self.assertEqual(fields["volume"], "0.1")
        self.assertNotIn("leverage", fields)
        self.assertNotIn("reduce_only", fields)
        self.assertEqual(request.headers["API-Sign"], kraken_signature(request.url.path, fields, SECRET))

    async def test_validate_does_not_expect_an_order_id(self):
        ex = self.exchange(lambda _: response({"descr": {"order": "validated"}}))
        self.assertIsNone(await self.submit(ex, validate=True))

    async def test_validate_unexpected_order_id_is_ambiguous(self):
        ex = self.exchange(lambda _: response({"txid": [ORDER_ID]}))
        with self.assertRaises(AmbiguousOrderError):
            await self.submit(ex, validate=True)

    async def test_mutation_network_failures_never_retry_or_leak(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("connection lost " + SECRET, request=request)

        with self.assertRaises(AmbiguousOrderError) as ctx:
            await self.submit(self.exchange(handler))
        self.assertEqual(len(calls), 1)
        self.assertNotIn(SECRET, str(ctx.exception))

    async def test_mutation_http_and_malformed_acknowledgements_are_ambiguous(self):
        cases = [httpx.Response(503, text="gateway timeout"),
                 httpx.Response(200, text="not json"),
                 httpx.Response(200, json={"result": {"txid": [ORDER_ID]}}),
                 response({}), response({"txid": []}),
                 response({"txid": [ORDER_ID, ORDER_ID]}),
                 response({"txid": [ORDER_ID]}, ["EGeneral:Internal error"])]
        for result in cases:
            calls = []

            def handler(request, calls=calls, result=result):
                calls.append(request)
                return result

            with self.subTest(status=result.status_code, content=result.content):
                with self.assertRaises(AmbiguousOrderError):
                    await self.submit(self.exchange(handler))
                self.assertEqual(len(calls), 1)

    async def test_definite_exchange_rejection_never_retries(self):
        calls = []

        def handler(request):
            calls.append(request)
            return response(errors=["EOrder:Insufficient funds"])

        with self.assertRaises(KrakenError) as ctx:
            await self.submit(self.exchange(handler))
        self.assertNotIsInstance(ctx.exception, AmbiguousOrderError)
        self.assertIn("Insufficient funds", str(ctx.exception))
        self.assertEqual(len(calls), 1)

    async def test_non_eur_benchmark_and_forged_pairs_rejected_before_network(self):
        calls = []
        ex = self.exchange(lambda request: calls.append(request))
        markets = [replace(MARKET, symbol="TAO/USD", quote="ZUSD", pair="TAOUSD"),
                   replace(MARKET, symbol="BTC/EUR", base="XXBT", pair="XBTEUR"),
                   replace(MARKET, pair="TAOUSD"), replace(MARKET, base="BTC"),
                   replace(MARKET, status="cancel_only")]
        for market in markets:
            with self.subTest(market=market), self.assertRaises(KrakenError):
                await self.submit(ex, market=market)
        self.assertFalse(calls)

    async def test_bad_order_precision_size_and_expiry_rejected(self):
        calls = []
        ex = self.exchange(lambda request: calls.append(request))
        cases = [{"qty": D("NaN")}, {"qty": D(0)}, {"qty": D("0.001")},
                 {"qty": D("0.010001")}, {"limit": D("250.001")},
                 {"trail_pct": D(100)}, {"trail_pct": D(-2)},
                 {"expiry": 60}, {"expiry": int(time.time()) + 3600}, {"userref": 2 ** 31}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(KrakenError):
                await self.submit(ex, **kwargs)
        self.assertFalse(calls)

    async def test_private_requests_serialize_nonces_despite_clock_rollback(self):
        captured = []
        active = 0
        maximum = 0

        async def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            captured.append(int(parse_qs(request.content.decode())["nonce"][0]))
            await asyncio.sleep(0)
            active -= 1
            return response({"ZEUR": {"balance": "200.00"}})

        values = iter([1000, 999, 1000, 1001])
        ex = self.exchange(handler, nonce=lambda: next(values))
        await asyncio.gather(*(ex.balances() for _ in range(4)))
        self.assertEqual(captured, [1000, 1001, 1002, 1003])
        self.assertEqual(maximum, 1)

    async def test_caller_cannot_override_nonce_or_access_destructive_endpoints(self):
        calls = []
        ex = self.exchange(lambda request: calls.append(request))
        for method in ("CancelAll", "CancelAllOrdersAfter", "Withdraw", "AddOrderBatch"):
            with self.assertRaises(KrakenError):
                await ex.private(method)
        with self.assertRaises(KrakenError):
            await ex.private("Balance", {"nonce": 99})
        self.assertFalse(calls)

    async def test_authentication_error_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return response(errors=["EAPI:Invalid key " + KEY])

        with self.assertRaises(KrakenError) as ctx:
            await self.exchange(handler).balances()
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertEqual(len(calls), 1)

    async def test_public_transient_http_errors_are_bounded_retries(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503) if len(calls) < 3 else response({"status": "online"})

        with patch("wildcat3.exchange.asyncio.sleep", return_value=None):
            self.assertEqual(await self.exchange(handler).status(), "online")
        self.assertEqual(len(calls), 3)

    async def test_market_discovery_is_country_and_venue_restricted(self):
        captured = []
        raw = {"altname": "TAOEUR", "wsname": "TAO/EUR", "base": "TAO", "quote": "ZEUR",
               "lot_decimals": 5, "pair_decimals": 2, "ordermin": "0.01", "costmin": "0.45",
               "tick_size": "0.01", "status": "online", "aclass_base": "currency"}

        def handler(request):
            captured.append(request)
            return response({
                "TAOEUR": raw,
                "TAOUSD": {**raw, "altname": "TAOUSD", "wsname": "TAO/USD", "quote": "ZUSD"},
                "XXBTZEUR": {**raw, "altname": "XBTEUR", "wsname": "XBT/EUR", "base": "XXBT"},
                "DOGEEUR": {**raw, "altname": "DOGEEUR", "wsname": "DOGE/EUR", "base": "DOGE"},
            })

        markets = await self.exchange(handler).markets()
        self.assertEqual(set(markets), {"TAO/EUR", "BTC/EUR"})
        self.assertEqual(markets["TAO/EUR"], MARKET)
        self.assertEqual(captured[0].url.params["country_code"], "FR")
        self.assertEqual(captured[0].url.params["execution_venue"], "international")

    async def test_market_minimums_missing_fail_closed(self):
        ex = self.exchange(lambda _: response({"TAOEUR": {
            "altname": "TAOEUR", "wsname": "TAO/EUR", "base": "TAO", "quote": "ZEUR",
            "lot_decimals": 5, "pair_decimals": 2, "ordermin": "0.01", "status": "online",
        }}))
        with self.assertRaises(KrakenError):
            await ex.markets()

    async def test_book_decimals_and_crossed_book_rejection(self):
        ex = self.exchange(lambda _: response({"TAOEUR": {
            "bids": [["250.01", "10.12345", 1]], "asks": [["250.02", "11.23456", 1]],
        }}))
        book = await ex.book(MARKET)
        self.assertEqual(book.bid, D("250.01"))
        self.assertEqual(book.bids[0][1], D("10.12345"))
        ex = self.exchange(lambda _: response({"TAOEUR": {
            "bids": [["250.02", "10", 1]], "asks": [["250.01", "10", 1]],
        }}))
        with self.assertRaises(KrakenError):
            await ex.book(MARKET)

    async def test_running_candle_is_always_dropped(self):
        ex = self.exchange(lambda _: response({"TAOEUR": [
            [3600, "10", "11", "9", "10.5", "10", "200", 10],
            [7200, "10", "11", "9", "10.5", "10", "200", 10],
            [10800, "10", "11", "9", "10.5", "10", "200", 10],
        ], "last": 10800}))
        candles = await ex.candles(MARKET)
        self.assertEqual([c.time for c in candles], [3600, 7200])
        self.assertEqual(candles[0].volume, 200)

    async def test_balance_total_does_not_add_credit_or_subtract_holds(self):
        ex = self.exchange(lambda _: response({"ZEUR": {
            "balance": "200.001", "credit": "100", "credit_used": "50", "hold_trade": "30",
        }}))
        self.assertEqual(await ex.balances(), {"ZEUR": D("200.001")})
        self.assertEqual((await ex.balance_ex())["ZEUR"]["credit"], "100")

    async def test_trade_and_order_history_paginate_with_frozen_end(self):
        for _method, key in (("TradesHistory", "trades"), ("ClosedOrders", "closed")):
            calls = []

            def handler(request, calls=calls, key=key):
                fields = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                calls.append(fields)
                ofs = int(fields["ofs"])
                return response({key: {str(i): {"vol": "1"} for i in range(ofs, min(ofs + 50, 121))},
                                 "count": 121})

            ex = self.exchange(handler)
            with patch("wildcat3.exchange.time") as clock:
                clock.time.side_effect = [1000, 2000, 3000]
                result = await (ex.trades(100.9) if key == "trades" else ex.closed_orders(100.9))
            self.assertEqual(len(result), 121)
            self.assertEqual([c["ofs"] for c in calls], ["0", "50", "100"])
            self.assertEqual({c["end"] for c in calls}, {"1000"})
            self.assertEqual({c["start"] for c in calls}, {"99"})
            self.assertEqual({c["consolidate_taker"] for c in calls}, {"false"})
            if key == "closed":
                self.assertEqual({c["closetime"] for c in calls}, {"both"})

    async def test_history_repeated_page_or_changing_count_fails_closed(self):
        ex = self.exchange(lambda _: response({"trades": {"repeated": {}}, "count": 2}))
        with self.assertRaisesRegex(KrakenError, "repeated history page"):
            await ex.trades(1)
        calls = []

        def handler(request):
            calls.append(request)
            return response({"trades": {str(len(calls)): {}}, "count": len(calls) + 1})

        with self.assertRaisesRegex(KrakenError, "changed during pagination"):
            await self.exchange(handler).trades(1)

    async def test_query_orders_batches_fifty_ids(self):
        captured = []
        ids = [f"O{i:05d}-ABCDE-ABCDEF" for i in range(103)]

        def handler(request):
            txids = parse_qs(request.content.decode())["txid"][0].split(",")
            captured.append(txids)
            return response({txid: {"status": "closed"} for txid in txids})

        result = await self.exchange(handler).query_orders(ids + ids[:1])
        self.assertEqual([len(batch) for batch in captured], [50, 50, 3])
        self.assertEqual(set(result), set(ids))

    async def test_fee_percentages_convert_to_decimal_fractions(self):
        captured = []

        def handler(request):
            captured.append(request)
            return response({
                "fees": {"TAOEUR": {"fee": "0.8000"}, "XXBTZEUR": {"fee": "0.4000"}},
                "fees_maker": {"TAOEUR": {"fee": "0.4000"}, "XXBTZEUR": {"fee": "0.2500"}},
            })

        ex = self.exchange(handler)
        self.assertEqual(await ex.fees(["TAO/EUR", "BTC/EUR"]), {
            "TAO/EUR": (D("0.004"), D("0.008")), "BTC/EUR": (D("0.0025"), D("0.004")),
        })
        self.assertEqual(parse_qs(captured[0].content.decode())["pair"], ["TAOEUR,XBTEUR"])

    async def test_missing_maker_schedule_does_not_guess_fees(self):
        ex = self.exchange(lambda _: response({"fees": {"TAOEUR": {"fee": "0.8"}}}))
        with self.assertRaises(KrakenError):
            await ex.fees(["TAOEUR"])
        ex = self.exchange(lambda _: response({"fees": {"TAOEUR": {"fee": "0.8"}},
                                               "fees_maker": {"TAOEUR": {"fee": "0.4"}}}))
        with self.assertRaisesRegex(KrakenError, "missing maker/taker"):
            await ex.fees(["TAOEUR", "AKTEUR"])

    async def test_cancel_rejects_userrefs_groups_and_all_orders(self):
        captured = []

        def handler(request):
            captured.append(request)
            return response({"count": 1})

        ex = self.exchange(handler)
        for invalid in ("42", ORDER_ID + "," + ORDER_ID, "all", ""):
            with self.assertRaises(KrakenError):
                await ex.cancel_order(invalid)
        self.assertFalse(captured)
        self.assertEqual(await ex.cancel_order(ORDER_ID), {"count": 1})
        self.assertEqual(captured[0].url.path, "/0/private/CancelOrder")

    async def test_injected_client_lifetime_belongs_to_caller(self):
        ex = self.exchange(lambda _: response({}))
        await ex.close()
        self.assertFalse(self.clients[-1].is_closed)

    async def test_numeric_json_preserves_decimal_balance_precision(self):
        ex = self.exchange(lambda _: httpx.Response(200, content=(
            '{"error":[],"result":{"ZEUR":{"balance":200.000000000000000001}}}'
        )))
        self.assertEqual(await ex.balances(), {"ZEUR": D("200.000000000000000001")})

    async def test_raw_add_order_cannot_bypass_spot_and_protection_guards(self):
        captured = []

        def handler(request):
            captured.append(request)
            return response({"txid": [ORDER_ID]})

        ex = self.exchange(handler)
        await self.submit(ex)
        original = {k: v[0] for k, v in parse_qs(captured[0].content.decode()).items() if k != "nonce"}
        captured.clear()
        bad = [{**original, "leverage": "2"}, {**original, "type": "sell"},
               {**original, "pair": "BTC/EUR"}, {**original, "pair": "TAOUSD"},
               {**original, "oflags": "fciq"}, {**original, "oflags": ["post", "fciq"]},
               {**original, "ordertype": "market"}, {**original, "close[ordertype]": "limit"},
               {**original, "close[price]": "-3%"}, {**original, "timeinforce": "GTC"},
               {**original, "expiretm": "0"}, {**original, "volume": "-1"},
               {**original, "asset_class": "tokenized_asset"}, {**original, "reduce_only": True}]
        no_close = dict(original)
        no_close.pop("close[ordertype]")
        bad.append(no_close)
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(KrakenError):
                await ex.private("AddOrder", payload)
        self.assertFalse(captured)

    async def test_private_rest_costs_are_charged_before_requests(self):
        costs = []
        seen = []

        async def limit(cost):
            costs.append(cost)

        def handler(request):
            seen.append((request.url.path.rsplit("/", 1)[1], list(costs)))
            return response({"txid": [ORDER_ID], "count": 1})

        ex = self.exchange(handler, rate_limiter=limit)
        for method in ("BalanceEx", "TradesHistory", "ClosedOrders", "Ledgers", "QueryOrders"):
            await ex.private(method)
        await self.submit(ex)
        await ex.cancel_order(ORDER_ID)
        self.assertEqual(costs, [1, 2, 2, 2, 1, 0, 0])
        self.assertEqual([len(costs_at_request) for _, costs_at_request in seen], list(range(1, 8)))

    async def test_repair_submits_one_gtc_native_sell_stop(self):
        captured = []

        def handler(request):
            captured.append(request)
            return response({"txid": [ORDER_ID]})

        txid = await self.exchange(handler).add_protection(MARKET, D("0.1"), D("240.00"), 45)
        self.assertEqual(txid, ORDER_ID)
        self.assertEqual(len(captured), 1)
        fields = {k: v[0] for k, v in parse_qs(captured[0].content.decode()).items()}
        self.assertEqual(fields["type"], "sell")
        self.assertEqual(fields["ordertype"], "stop-loss")
        self.assertEqual(fields["price"], "240.00")
        self.assertEqual(fields["volume"], "0.1")
        self.assertEqual(fields["timeinforce"], "GTC")
        self.assertEqual(fields["oflags"], "fciq")
        self.assertEqual(fields["stptype"], "cancel-newest")
        for forbidden in ("leverage", "expiretm", "close[ordertype]", "close[price]"):
            self.assertNotIn(forbidden, fields)

    async def test_repair_enforces_market_minimum_and_precision(self):
        captured = []
        ex = self.exchange(lambda request: captured.append(request))
        invalid = [(replace(MARKET, quote="USD"), D("0.1"), D(240)),
                   (MARKET, D("0.001"), D(240)), (MARKET, D("0.010001"), D(240)),
                   (MARKET, D("0.1"), D("240.001")), (MARKET, D("0.1"), D(0)),
                   (MARKET, D("0.01"), D(1))]
        for market, qty, price in invalid:
            with self.subTest(qty=qty, price=price), self.assertRaises(KrakenError):
                await ex.add_protection(market, qty, price, 45)
        self.assertFalse(captured)

    async def test_repair_timeout_is_ambiguous_and_never_retried(self):
        captured = []

        def handler(request):
            captured.append(request)
            raise httpx.ReadTimeout("lost response", request=request)

        with self.assertRaises(AmbiguousOrderError):
            await self.exchange(handler).add_protection(MARKET, D("0.1"), D(240), 45)
        self.assertEqual(len(captured), 1)

    async def test_raw_protection_rejects_expiry_margin_or_relative_price(self):
        captured = []
        ex = self.exchange(lambda request: captured.append(request))
        original = {"pair": "TAOEUR", "type": "sell", "ordertype": "stop-loss",
                    "volume": "0.1", "price": "240", "userref": 45, "oflags": "fciq",
                    "timeinforce": "GTC"}
        for update in ({"leverage": "2"}, {"expiretm": "0"}, {"price": "+240"},
                       {"price": "-3%"}, {"timeinforce": "GTD"}, {"oflags": "post,fciq"},
                       {"close[ordertype]": "stop-loss"}, {"ordertype": "market"}):
            with self.subTest(update=update), self.assertRaises(KrakenError):
                await ex.private("AddOrder", {**original, **update})
        self.assertFalse(captured)


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    def limiter(self):
        self.now = 0.0
        self.waits = []

        async def sleep(duration):
            self.waits.append(duration)
            self.now += duration

        return RestRateLimiter(clock=lambda: self.now, sleep=sleep)

    async def test_small_initial_burst_then_starter_safe_refill(self):
        limiter = self.limiter()
        for _ in range(3):
            await limiter.acquire(2)
        self.assertFalse(self.waits)
        await limiter.acquire(2)
        self.assertAlmostEqual(sum(self.waits), 2 / 0.3)
        await limiter.acquire(0)
        self.assertAlmostEqual(sum(self.waits), 2 / 0.3)

    async def test_idle_refill_is_capped_and_clock_rollback_adds_no_credit(self):
        limiter = self.limiter()
        await limiter.acquire(6)
        self.now = 1000.0
        await limiter.acquire(6)
        self.assertFalse(self.waits)
        await limiter.acquire(1)
        self.assertAlmostEqual(sum(self.waits), 1 / 0.3)
        self.now -= 10
        await limiter.acquire(1)
        self.assertGreaterEqual(sum(self.waits), 10 + 2 / 0.3 - 1e-9)


if __name__ == "__main__":
    unittest.main()

# Kraken execution and regulatory research

Verified **13 September 2026** against Kraken's public API, current official API documentation, Kraken support, and the AMF register. These findings describe the venue; they are not a claim of profitable strategy performance or certification of the application. No authenticated order was placed for this research.

## Current fees: the old 0.25% / 0.40% assumption is stale

Kraken's current base spot tier is **0.40% maker and 0.80% taker**. The next tier starts at $2,500 of qualifying spot volume and charges 0.30% / 0.60%. The schedule warns that location/VPN effects may alter displayed fees. [Current fee schedule](https://www.kraken.com/features/fee-schedule).

The new cross-platform tier system took effect **9 July 2026**. Qualifying account assets can improve tiers, but a new €200 account does not meet those assets thresholds. [Kraken's announcement](https://blog.kraken.com/product/pro/new-kraken-pro-fee-tiers).

Request `TradeVolume` with the actual pairs: `fees` contains taker rates and `fees_maker` maker rates. Values are **percentages**, so `"0.8000"` means `0.008` as a return fraction. Fees are omitted if pairs are omitted. [TradeVolume reference](https://docs.kraken.com/api-reference/account-data/get-trade-volume).

Engineering consequence: maker entry plus market-stop exit costs approximately **1.20% of entry notional before spread, slippage, adverse selection and rounding**. An exact fee-only break-even exit/entry price ratio, assuming quote-paid fees, is `(1 + 0.004) / (1 - 0.008) = 1.01209677`, or **1.2097%**. A €50 trade pays approximately €0.60 in two-way fees at unchanged price. These are calculations, not expected returns. A fast turnover strategy must demonstrate a materially larger conditional gross edge before being allowed to spend money.

Fees are charged on executions, with a minimum linked to the pair's accounting precision. The requested fee currency is a preference, not an unconditional guarantee. Therefore reconciliation must inspect balances/ledgers rather than assuming every fill always settles fees in EUR. No inspected official document guarantees inheritance of the parent's `fciq` by conditional children. Check returned child flags and retain a EUR fee reserve. [How fees work](https://support.kraken.com/articles/201893638-how-trading-fees-work-on-kraken).

The expanded `TradesHistory` schema reports its fee field in quote units. That is a reporting denomination, not proof of which balance paid it. Requesting `ledgers=true` provides associated ledger IDs; `QueryLedgers` supplies the ledger asset, transaction amount, fee and resulting balance. Use those records to identify base-paid fees. A balance/fill discrepancy must stop new entries while it is resolved; it must not be dismissed as dust. [TradesHistory schema](https://docs.kraken.com/api-reference/account-data/get-trades-history.md), [QueryLedgers schema](https://docs.kraken.com/api-reference/account-data/query-ledgers.md).

## Native entry and protective-order parameters

The current REST `AddOrder` contract specifies:

- `close[ordertype]` supports `stop-loss` and `trailing-stop`, including their limit variants.
- `close[price]` specifies the secondary trigger; omit `close[price2]` for a market stop.
- A trailing `price` requires a **positive prefix**, for example `+3%`; Kraken derives direction from buy/sell. A long's trailing close therefore uses `close[price]="+3%"`, not `-3%`.
- `trigger=last` is the default and also governs the attached close. Index triggers can fall back to last price.
- Leverage defaults to none; omit `leverage` entirely for unlevered spot.
- `oflags=post,fciq` requests maker-only limit entry and quote-currency fees.
- `timeinforce=GTD` requires `expiretm`; relative expiration is at least five seconds, at most one month. An absolute Unix timestamp avoids relative-string encoding mistakes.
- `deadline` is a separate RFC3339 request-admission cutoff, two to sixty seconds ahead; it does not replace resting-order expiry.
- `validate=true` checks inputs without placing a matching-engine order.

[Current REST AddOrder](https://docs.kraken.com/api-reference/trading/add-order).

Kraken's official REST example demonstrates an unlevered buy with `close[ordertype]=stop-loss` and `close[price]=-5%`. The negative static-stop offset must not be confused with trailing-stop syntax. [Official API order examples](https://support.kraken.com/articles/360000920786-examples-of-placing-orders-with-different-parameters).

The explicit per-fill semantics are documented in current WebSocket v2: every primary fill generates an opposite-side secondary order equal to the executed quantity. Its `reduce_only` field describes reducing **margin positions**. A cash spot balance should not be treated as a margin position; do not rely on `reduce_only` as a spot inventory lock. WebSocket v2 also documents positive trailing offsets. [WebSocket v2 Add Order](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/add_order).

Kraken describes conditional closes as a single secondary order template, with no combined TP/SL OCO through that mechanism. Its newer Pro interface has a separate bracket-order feature; that UI feature is not evidence that REST `close[...]` accepts two children. [Conditional close](https://support.kraken.com/articles/360038640052-conditional-close), [Pro bracket orders](https://support.kraken.com/articles/bracket-orders-on-kraken-pro).

Engineering consequence: submit a protective conditional template **inside the entry request**. A host-side sequence of buy, wait for fill, then create stop introduces an avoidable power-loss gap. Leave the generated protection on Kraken when the host exits. Cancel only a known unfilled entry remainder, never indiscriminately cancel every order.

## What protection can and cannot guarantee

A trailing sell follows favorable peaks and submits a market sell after the configured retracement. Kraken currently permits **20 active trailing orders per account**. Fragmented entries can create multiple children, so the limit is relevant even with few economic positions. [Trailing-stop support](https://support.kraken.com/articles/trailing-stop-orders).

A stop-loss creates a market order on triggering and incurs taker fees. The stop is an independent order: closing the holding another way requires managing the old stop to prevent unintended later executions. Its fill price can be materially worse than the trigger in volatile, thin markets. [Stop-loss support](https://support.kraken.com/articles/7699391647892-stop-loss-orders).

Kraken Market Price Protection can cancel market orders when spreads exceed pair-specific thresholds. The documentation currently describes a 2.5–20% range. This mechanism means an active stop is not an unconditional liquidation guarantee. [Market Price Protection](https://support.kraken.com/articles/201648183-market-price-protection).

A stop-limit controls the minimum acceptable execution price but may remain unfilled after a fast fall. It is therefore a weaker choice for this bot's offline liquidation objective. [Stop-limit support](https://support.kraken.com/articles/9616163984020-stop-loss-limit-orders).

Kraken `CancelAllOrdersAfter` cancels **all client orders** when its timer expires. It has no documented protective-order exclusion. A timeout of zero disables it. Using a recurring dead-man timer for this long-only offline bot would remove its stops when the machine powers off. A different program sharing the account can also create a dangerous interaction. [CancelAllOrdersAfter reference](https://docs.kraken.com/api-reference/trading/cancel-all-orders-after-x).

Engineering limitations requiring explicit recognition:

- Native OTO removes the local submission gap, but cannot insure against exchange outage, rejected children, stop cancellation, insufficient free inventory, gaps, or MPP.
- The public documents inspected do not give a comprehensive guarantee for OTO children below normal minimum size, behavior at the 20-trailing-order limit, child identifier inheritance, or child expiry inheritance from a GTD parent. Do not invent these guarantees. Validate real returned children, their quantity and expiry, and obtain Kraken UAT evidence for these cases before unattended live use.
- FOK requires immediate complete execution and is incompatible with the intention to rest a maker order. It also does not establish a single execution: one fully filled order can match several counterparties. It therefore does not establish that only one OTO child will be generated. A static stop-loss avoids the specifically documented trailing-order count restriction, although the other child-creation cases still require validation.
- A powered-off host cannot send immediate custom Telegram alerts. Persist notifications while online and summarize unseen exchange fills at restart; an independent always-on monitor is needed for custom instantaneous overnight alerts.
- Discovered unprotected inventory is an emergency even if the strategy would currently prefer holding it. A policy that promises zero naked inventory under every possible external failure is not technically achievable.

## Durable IDs and ambiguous REST responses

Kraken's current API distinguishes `cl_ord_id`, unique across the client's **open** orders, from numeric `userref`, which deliberately need not be unique. The two cannot be supplied together. Client IDs accept UUID formats or free ASCII text up to 18 characters. Open-order uniqueness is not a historical idempotency guarantee. [Client identifiers in WebSocket v2](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/add_order).

`OpenOrders` and `ClosedOrders` can filter by `cl_ord_id`; the former returns current open orders and the latter returns filled or canceled orders. `QueryOrders` requires Kraken transaction IDs, up to 50, and does not document `cl_ord_id` as a query parameter. [OpenOrders](https://docs.kraken.com/api-reference/account-data/get-open-orders), [ClosedOrders](https://docs.kraken.com/api-reference/account-data/get-closed-orders), [QueryOrders](https://docs.kraken.com/api-reference/account-data/query-orders-info).

The expanded OpenOrders schema defines nullable `refid` as the transaction ID of the order that created this order: this is a documented parent relationship when populated. `userref` and `cl_ord_id` are nullable identifiers; their inheritance is not documented. Validate `refid` against a persisted parent transaction ID; do not infer ownership solely from a matching symbol or similar size. [OpenOrders schema](https://docs.kraken.com/api-reference/account-data/get-open-orders.md).

Recommended application protocol (engineering design, not a venue guarantee):

1. Commit the immutable order intent and fresh client ID to durable local storage before transmission.
2. Send once. A timeout, reset, malformed reply, or uncertain HTTP failure marks the result **unknown**, not rejected.
3. Search open and closed history using the persisted identity, and reconcile executions and balances. Never turn absence from a single snapshot into permission to retry a buy.
4. Preserve an unresolved intent across restart and stop new entries until its outcome is established. A new client ID would create a second financial instruction, not recover the first one.
5. Process fills idempotently using unique exchange trade IDs. Commit fill accounting, state updates and notification-outbox entries in one transaction.
6. Never assume the child shares the parent's client ID. Retain any exchange-provided relationship, require consistent asset/side/quantity, and halt on ambiguous ownership.

## History pagination and boot reconciliation

`ClosedOrders` returns 50 results per page, newest first by default, with `ofs` pagination and a total count. `start` is exclusive and `end` inclusive. `closetime=close` selects closure time. A transaction-ID time boundary refers to the order's opening time. `consolidate_taker` defaults true. [ClosedOrders contract](https://docs.kraken.com/api-reference/account-data/get-closed-orders).

`TradesHistory` currently accepts `limit` from 1 to 100 (default 50), `ofs`, exclusive `start` and inclusive `end`, with `consolidate_taker=true` by default. It can include ledger IDs. The new pair filter is optional; do not require it for recovering all account activity. [TradesHistory contract](https://docs.kraken.com/api-reference/account-data/get-trades-history).

Engineering consequence: freeze an upper timestamp for a complete pagination sweep, overlap the previously committed lower timestamp, and deduplicate by exchange IDs. Persist the new cursor only after all pages and accounting commit successfully. A canceled/expired order may have nonzero executions; derive holdings from executed quantity rather than treating cancellation as zero-fill. Reconcile actual total and available balances, active children, partially filled parents, and exits that happened during downtime before permitting a new entry. An incomplete history page is a failed reconciliation, not a flat account.

## EUR specialization universe observed live

The public `AssetPairs` endpoint was queried during this research, with TLS verification enabled. All four candidates were `online`, quoted in `ZEUR`:

| REST pair | WebSocket pair | Minimum base quantity | Price tick | Quantity decimals | Minimum cost |
|---|---|---:|---:|---:|---:|
| TAOEUR | TAO/EUR | 0.02 TAO | €0.0001 | 5 | €0.45 |
| RENDEREUR | RENDER/EUR | 2.5 RENDER | €0.001 | 5 | €0.45 |
| AKTEUR | AKT/EUR | 10 AKT | €0.0001 | 8 | €0.45 |
| FETEUR | FET/EUR | 18 FET | €0.0001 | 8 | €0.45 |

[Exact public query](https://api.kraken.com/0/public/AssetPairs?pair=TAOEUR,RENDEREUR,AKTEUR,FETEUR).

The response's public fee arrays were empty. TAO exposed margin availability, which must not be enabled. Pair existence is not proof of enough liquidity or account-level regional eligibility. Refresh metadata and evaluate actual EUR spread, depth, recent trade activity, volume and executable size at runtime. Do not convert via USD/USDT or replace a rejected niche asset with an unrelated token. A below-minimum risk-sized order should be skipped, not enlarged to force a trade.

## French / MiCA context

The AMF whitelist lists **Payward Europe Solutions Limited / Kraken Digital Asset Exchange**, licensed in Ireland and authorized to provide crypto-asset services in France through European passporting. The AMF page records **25 June 2025** as the intended France service start date, not a claim that France granted the underlying Irish license. Its listed services include crypto/funds exchange and execution of client orders. [AMF French register entry](https://www.amf-france.org/fr/espace-epargnants/proteger-son-epargne/listes-blanches/psanpsca/payward-europe-solutions-limited-kraken-digital-asset-exchange-kraken).

This supports choosing Kraken's eligible European service for the user's own spot account. It does not certify this bot, eliminate French reporting/tax obligations, or authorize operating portfolios for third parties. Account residency, contractual entity and product eligibility remain account-specific.

## Evidence boundary

These are source-based API findings and a public metadata observation. Authenticated execution, child-generation edge cases, user-account fees, access permissions, UAT behavior and the strategy's out-of-sample expectancy have **not** been demonstrated by this research. Kraken states that UAT access is available through support. [Official API infrastructure overview](https://blog.kraken.com/product/api/unlocked-1-strategies-infrastructure-and-where-to-start).

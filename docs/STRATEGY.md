# WildCat-3 strategy, evidence and limits

Research date: **13 September 2026, Europe/Paris**. The public snapshot was captured at **2026-09-12 22:53:58 UTC**. WildCat-3 implements a selective long-only continuation hypothesis for decentralized AI and compute assets on actual Kraken EUR books. It does **not** have a demonstrated positive expected return or a demonstrated optimal Sharpe ratio. The included short historical diagnostic lost money. Keeping the €200 in EUR is an intentional outcome when the cost, liquidity or regime filters fail.

## The specialized universe

| Kraken symbol | Economic connection to the niche | Important distinction |
|---|---|---|
| TAO/EUR | Bittensor coordinates compensated work in subnets, including inference and other machine-intelligence services. | TAO is the network asset; the bot does not buy individual subnet alpha tokens. |
| RENDER/EUR | Render connects GPU owners with creators requiring rendering compute. | GPU rendering demand and AI-inference demand are different businesses; a shared narrative is not a common cash flow. |
| AKT/EUR | Akash provides decentralized cloud deployments, including GPU workloads for training and inference. | Available or leased GPUs do not mechanically determine short-term AKT returns. |
| FET/EUR | Fetch.ai uses FET in its agent and decentralized-AI services ecosystem. | Agent infrastructure belongs to the mandate; it is not a pure GPU-capacity asset. |

These classifications follow primary protocol material: [Bittensor network architecture](https://preview.bittensor.com/docs/concepts/network), [Render documentation](https://know.rendernetwork.com/), [Akash GPU deployments](https://akash.network/docs/learn/core-concepts/gpu-deployments/), and [Fetch.ai FET documentation](https://network.fetch.ai/docs/guides/network/intro-fet-token). The allowlist is explicit. BTC/EUR provides public market context and is never an eligible investment. Exchange discovery can remove unavailable or non-online markets; it cannot broaden the mandate.

The working hypothesis is that sector attention can produce persistent relative strength over several days, while its EUR sub-books remain much smaller than BTC/EUR. That hypothesis motivates a filter, not a probability estimate. Token emissions, changing compute utilization, product launches and exchange liquidity can all affect prices, but this implementation does **not** claim to ingest or predict those events. It avoids pretending that an unvalidated news or language-model score is a trading edge.

[Liu and Tsyvinski's cryptocurrency research](https://www.nber.org/papers/w24877) documents momentum and attention effects in its historical large-asset sample. It does not establish an hourly TAO, RENDER, AKT or FET strategy on Kraken after 2026 fees. The paper motivates testing continuation; the test must still succeed in this distinct market.

## What the actual EUR books showed

The reproducible [raw responses](evidence/2026-09-12/snapshot.json), [human-readable snapshot](evidence/2026-09-12/snapshot.md) and [hourly CSV](evidence/2026-09-12/hourly.csv) are included with the code. These are serial public REST observations, not simultaneous tradable guarantees.

| Pair | Spread, bp | Bid depth within 50 bp, € | Ask depth within 50 bp, € | Prior 24h turnover, € | Median hourly true range, % | 7d hourly BTC correlation |
|---|---:|---:|---:|---:|---:|---:|
| TAO/EUR | 8.01 | 80,634 | 46,670 | 406,366 | 0.946 | 0.578 |
| RENDER/EUR | 8.39 | 11,755 | 1,413 | 26,380 | 0.865 | 0.676 |
| AKT/EUR | 17.52 | 997 | 1,420 | 7,569 | 0.749 | 0.523 |
| FET/EUR | 6.84 | 2,880 | 417 | 57,389 | 1.177 | 0.648 |
| BTC/EUR, context only | 0.015 | 3,095,269 | 6,447,384 | 7,333,376 | 0.319 | 1.000 |

Depth sums displayed `price × quantity` inside 0.5% of the midpoint; the book request includes up to 100 levels. Turnover uses Kraken's prior-24h base volume multiplied by its prior-24h VWAP. True range uses the median of the most recent 72 completed hourly true ranges divided by preceding close. Correlation uses aligned hourly log returns from the last 168 return observations. The raw OHLC window contains 720 completed hourly candles per pair in this capture, from 13 August through 12 September.

Three findings materially affect the design:

1. A narrow spread does not imply a reliable exit: FET's 6.84 bp spread coexisted with only €417 of displayed near-mid ask depth. Book-side imbalance and both sides' depth matter.
2. TAO's observed EUR turnover exceeded the other three combined, but its 72h return was -8.30%. Better liquidity is insufficient without a favorable regime.
3. Observed niche hourly ranges were roughly 2.4–3.7 times BTC's. Tight large-cap-style stops and frequent re-entry would be poorly matched to this snapshot.

These are observations and engineering inferences from one sample, not persistent statistical laws. The current screen returned **no eligible pair**: AKT/FET failed depth, RENDER failed extreme imbalance, TAO failed persistent leadership. The fixed €100,000 daily EUR-turnover threshold also excludes the three lower-turnover pairs at this observation. We did not relax that threshold to manufacture trades.

## Signal and execution rules

Every decision uses 168 consecutive completed hourly candles. Unfinished candles, stale data, invalid prices, missing intervals, mismatched BTC timestamps, stale/crossed/malformed books and unknown fee units fail closed. Kraken's current hourly candle is excluded. Runtime fee inputs are fractions obtained by the engine from the account's actual pairs.

The complete gates in `strategy.py` are:

1. **Market regime:** BTC's 24h return must be at least -1.5%, its 72h return at least -5%, and its latest hourly return at least -1.2%.
2. **Tradability:** online allowlisted EUR pair; book age at most 15 seconds; spread at most 25 bp; at least €1,200 on each side inside 50 bp; absolute depth imbalance `abs(bid_depth - ask_depth)/(bid_depth + ask_depth)` at most 0.65; completed 24h EUR turnover at least €100,000. Displayed imbalance is an adverse-selection exclusion, not a claimed direction predictor.
3. **Leadership:** asset 24h return at least +2%, 72h return positive, and 24h return at least one percentage point above BTC. Reject 24h gains over 25% or 72h gains over 45% as overextended. These are predeclared heuristic thresholds, not fitted coefficients.
4. **Controlled breakout:** latest close at or above the preceding 24 closes' maximum, but no more than 1.5 robust hourly ranges above it. The candle must close in its upper 40%, and its total range cannot exceed three robust hourly ranges.
5. **Participation:** latest volume between 1.4 and 5 times the median of the preceding 48 hourly volumes. This demands participation but rejects isolated volume climaxes. It is not a calibrated fill probability.
6. **No chase:** current midpoint must remain within `max(0.25%, 0.5 × robust_hourly_range)` of the confirmed close. Quote at the bid, rounded down to the exchange tick, using a post-only order. Expire the resting entry after 60 seconds; never chase a missed fill.
7. **Risk and cost feasibility:** the following mathematical hurdle must pass. A qualifying observation is still only a candidate; balance, venue minimum, exposure, cooldown and reconciliation gates can prevent entry.

The robust hourly range is:

```text
TR_t = max(high_t - low_t, |high_t - close_(t-1)|, |low_t - close_(t-1)|) / close_(t-1)
v = median(last 72 TR_t)
d = max(0.025, 2.8 v)
```

The native trailing distance `d` is rounded upward to 0.01 percentage point. If it exceeds 6%, skip the trade rather than compressing its protection to an unsuitable distance. The minimum is 2.5%. A native trailing market exit continues to ratchet on Kraken while the computer is off. It does not impose a bounded fill price in a gap or guarantee execution during a venue outage; see [Kraken execution research](KRAKEN_RESEARCH.md).

## Fee arithmetic and small-bankroll economics

The verified base tier is **0.40% maker / 0.80% taker**, following the July 2026 change. Older 0.25% / 0.40% examples are stale. The engine queries actual account fees; the research defaults use the base tier. [Kraken fee schedule](https://www.kraken.com/features/fee-schedule), [fee-tier announcement](https://blog.kraken.com/product/pro/new-kraken-pro-fee-tiers).

For entry notional `N`, maker fee `f_m`, exit gross return `r` and taker fee `f_t`:

```text
entry cash spent = N (1 + f_m)
exit cash received = N (1 + r) (1 - f_t)
fee-only break-even r = (1 + f_m) / (1 - f_t) - 1
                     = 1.209677% at the baseline tier
```

This is already much larger than the observed EUR spreads. Quoting passively saves the difference between maker and taker fees at entry, but a maker fill tends to occur as the market sells into the bid. Post-only does not eliminate adverse selection, queue risk or the taker fee on an emergency exit.

The eligibility budget, in basis points, is:

```text
C = 10,000 ((1 + f_m)/(1 - f_t) - 1)
    + observed spread_bp + 30 exit-slippage_bp
    + max(35 uncertainty_bp, 0.25 × 10,000 v)

O = 10,000 × min(observed 72h return, 2 sqrt(24) v)

require O >= max(2 C, 10,000 d + C, C + 100)
```

`O` is a bounded **historical movement scale**, not a forecast. Its square-root-time term is a conservative sizing heuristic with no claim that these returns are Gaussian, independent, or diffusive. Requiring a large scale excludes signals whose conceivable follow-through is too small to pay costs; it cannot establish positive expectancy. The field `Signal.opportunity_margin_bps` contains `O - C`, and its user-facing reason explicitly says “not a return forecast.” Ranking divides this margin by the planned risk plus costs, then multiplies by capped observed relative strength. No win probability is invented.

A trailing strategy needs enough follow-through to survive the later retracement. With a 2.5% trail, baseline fees and 30 bp adverse exit slippage, a peak roughly **4.12% above entry** is necessary for the later trailing-stop sale merely to break even:

```text
peak / entry > (1 + f_m) / ((1 - d)(1 - f_t)(1 - slippage))
```

That calculation explains why sub-percent scalping is a poor starting hypothesis for this bankroll. It does not say a 4.12% move will occur.

Production sizing budgets at most **0.5% of equity per entry**, including trail distance, both fees, spread and slippage. It also caps allocation at 30%, retains €20 EUR reserve, caps quantity to 1% of displayed near-bid exit depth, and permits one active position. At €200, 2.5% trailing risk, 1.2% approximate fees, 20 bp spread and 30 bp slippage, the risk cap yields approximately `€1/0.042 = €23.81` before lot rounding. A venue minimum larger than the risk-sized order means no trade. The €1 is a planned-loss budget, not insurance against a price gap.

Two entry attempts per UTC day and six hours between submitted intents bound turnover. The 2% daily-loss and 8% drawdown breakers latch off new entries while retaining exchange protection. These controls constrain compounding damage. They do not optimize or guarantee Sharpe.

## Reproducible historical diagnostic

The executable CSV simulator calls the **same strategy evaluator**, supplies only candles completed by decision time, and performs fixed-parameter chronological evaluation:

- 168 hours of initial warmup; first 60% of remaining history designated development; final 40% held out.
- Three contiguous held-out folds with expanding historical context. Parameters are **never refitted**. Each fold starts with a fresh €200, so fold results are diagnostic and must not be summed as one portfolio.
- Maker entry requires a strict trade-through beyond one tick, an opening price above the limit, and execution-bar turnover at least 20 times the ticket. Merely touching a limit does not fill it.
- Native trailing stops use the worse terminal-value outcome of `open → high → low → close` and `open → low → high → close`. An opening gap fills at the opening price with slippage. Entry-bar highs before a fill cannot tighten that new position's stop.
- Fees are debited on entry and exit. Equity marks include open positions at a conservative liquidation value; drawdown is measured from sampled equity including those positions. It does not capture every intrabar equity low.
- Daily returns use consecutive UTC-midnight liquidation-equity observations, excluding partial days. Annualized Sharpe is `sqrt(365) × mean(daily returns)/sample_std`; it is `null` below 30 daily returns or with zero variance. This conventional annualization can still be misleading with autocorrelation; it is not a significance test.
- Runtime reserve, fee, allocation, risk, entry-attempt, cooldown, daily-loss and drawdown defaults are matched. Offline market metadata is a dated assumption; production always discovers it from Kraken.

The included [report](evidence/2026-09-12/backtest-report.json) produced these results with the final production-aligned defaults:

| Period | Final marked equity, € | Return | Closed trades | Maximum sampled drawdown |
|---|---:|---:|---:|---:|
| Development, Aug 20 22:00–Sep 3 17:00 UTC | 196.8283 | -1.5858% | 6 | 1.8615% |
| Held out, Sep 3 17:00–Sep 12 22:00 UTC | 199.4217 | -0.2891% | 1 | 0.3292% |
| Held out, 1.5× spread / 2× slippage | 200.0000 | 0.0000% | 0 | 0.0000% |

The held-out trade lost €0.5783. There were only **8 complete daily returns**, so Sharpe is `null`. In the spread/slippage stress, the assumed 30 bp spread exceeds the 25 bp gate and shuts the strategy down. Its unchanged €200 is not proof of successful execution under stress.

**This report is an hourly sensitivity diagnostic, not a faithful live-fill backtest.** An hourly low cannot prove a post-only fill occurred within the live 60-second expiry. Hourly mode explicitly allows the entire next hour to trade through, which may overstate opportunities. Synthetic EUR 10,000 book-side depth and a fixed 20 bp spread also prevent validation of the actual historical liquidity filters. Execution-minute CSV mode restricts entry to the first minute, but still cannot reveal queue position, exact submission time, cancellations, displayed-depth changes, partial fills, or Kraken's market-price-protection behavior. Candle-path pessimism is local to a bar and is not a global worst-case guarantee.

The short sample, single held-out trade, current-universe survivorship, synthetic books and absent minute execution data make a profitability claim unjustified. The negative outcome was retained; thresholds were not tuned to erase the loss. [Bailey et al.](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) explain why repeated strategy selection using investment backtests can invalidate apparently held-out evidence.

## Commands and next evidence needed

From the repository root:

```bash
uv run python -m wildcat3.research --output data/research/latest
uv run python -m wildcat3.backtest docs/evidence/2026-09-12/hourly.csv --output data/reproduced-report.json
uv run python -m wildcat3.backtest data/history-hourly.csv --execution-csv data/history-minute.csv --output data/minute-report.json
uv run pytest tests/test_strategy.py tests/test_backtest.py
```

CSV columns are `timestamp,symbol,open,high,low,close,volume`. Timestamps are UTC epoch seconds or timezone-aware ISO-8601; they denote candle **open**. Signal candles must be consecutive hourly records and include BTC/EUR plus one or more allowlisted niche pairs. Optional execution records must be consecutive one-minute records for every tested niche pair and span the entire evaluation period. Volume is base-asset units. The parser rejects duplicate/missing intervals and malformed data. The public capture requires no keys; REST's recent OHLC window cannot supply a long historical archive.

[Evidence provenance and exact reproduction](evidence/2026-09-12/README.md) include hashes and the captured source data. Before interpreting this as an investable strategy, collect several market regimes of EUR order books, trades and minute candles; evaluate actual 60-second maker fills and partial fills; preserve a fresh chronological test period; and evaluate costs, overnight gap scenarios and serially correlated daily returns. Record every parameter trial. A positive average across enough independent periods with cost-stress survival would support further validation; it would still not establish guaranteed returns. The present build supplies the implementation and reproducible measurement tools, while the positive edge remains unproven.

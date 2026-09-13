# Kraken public EUR microstructure snapshot

Captured 2026-09-12T22:53:58.650894+00:00.

| Pair | Spread bp | Bid depth ±50bp € | Ask depth ±50bp € | 24h turnover € | Median hourly TR % | 7d BTC correlation |
|---|---:|---:|---:|---:|---:|---:|
| AKT/EUR | 17.52 | 997 | 1420 | 7569 | 0.749 | 0.523 |
| BTC/EUR | 0.02 | 3095269 | 6447384 | 7333376 | 0.319 | 1.000 |
| FET/EUR | 6.84 | 2880 | 417 | 57389 | 1.177 | 0.648 |
| RENDER/EUR | 8.39 | 11755 | 1413 | 26380 | 0.865 | 0.676 |
| TAO/EUR | 8.01 | 80634 | 46670 | 406366 | 0.946 | 0.578 |

One observation of public books, collected serially. Displayed orders can disappear. These are actual EUR books, not USD liquidity translated into euros. No statistical edge is established.

## Current screening result

- AKT/EUR: insufficient two-sided EUR depth inside 50 bp
- FET/EUR: insufficient two-sided EUR depth inside 50 bp
- RENDER/EUR: extreme book imbalance/adverse-selection risk
- TAO/EUR: no persistent sector leadership versus BTC

Sources: [Kraken Depth](https://docs.kraken.com/api/docs/rest-api/get-order-book/), [OHLC](https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/), [Ticker](https://docs.kraken.com/api/docs/rest-api/get-ticker-information/).

This capture contains 720 completed hourly bars per pair, covering roughly 30 days. Longer data and minute or trade-level execution records are needed; the snapshot is not an adequate backtest sample.

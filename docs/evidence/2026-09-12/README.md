# Preserved public-market evidence

Captured 2026-09-12T22:53:58Z from Kraken public REST endpoints. No account data or credentials are included. `snapshot.json` preserves the source metadata, books, ticker and completed OHLC responses; `snapshot.md` summarizes the sample. `hourly.csv` contains approximately 30 days of completed hourly EUR OHLC for the four niche assets and BTC/EUR. This is a single current universe, not a survivorship-free history of past listings.

Reproduce the stored diagnostic using:

```sh
uv run python -m wildcat3.backtest docs/evidence/2026-09-12/hourly.csv --output data/reproduced-report.json
```

`backtest-report.json` contains the preserved chronological holdout diagnostic with production-aligned fee and risk defaults. It is an **hourly fill sensitivity scenario** with synthetic book assumptions; it does not validate 60-second live order fills, queue priority, historical depth or exchange outages. The negative held-out result is retained, without tuning thresholds to improve it.

SHA-256 hashes of preserved artifacts:

```text
c142e2aa012ea6e4b7f83c6d9ee9586e6480a5f5d5bbee54734d1063daa5e5b2  backtest-report.json
a7ea027e6f233b5990068649923c56bdd29bc14b228d61b56bbad5c1c45fce03  hourly.csv
8cc2bd003c3fda05221bb7a6f80c87f30aa840547ad58100a2c97df44ae09097  snapshot.json
a9b916648fbd2715acd3d6576b81cfef40125bdbc3fc63aa2aabb0c08327a7c6  snapshot.md
```

# WildCat-3

A local Python trading agent specialized in Kraken EUR spot markets for decentralized AI and compute: **TAO, RENDER, AKT and FET**. BTC/EUR is a market-regime input only. The initial bankroll is exactly **€200**. No margin, borrowing, derivatives, conversion orders or general market scanner.

The default mode is paper trading against real public Kraken data. Live trading requires your own Kraken credentials and explicit configuration. This repository does not establish a profitable edge or guarantee that an exchange stop executes. Do not describe an indicator backtest or this implementation as validated production performance.

## Install and start

### Browser setup (recommended)

On your Mac, double-click **Open WildCat-3.command** in the project folder. It installs the locked dependencies with `uv` and opens the local control panel in your browser. Keep the launcher's Terminal window open. If `uv` is missing, install it once using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/).

1. Start **Paper trading** immediately, with a simulated €200 and no keys.
2. For Kraken, expand **Where do I get my API keys?**, create a Spot API key on Kraken, and paste the API key and private key into the form. Your Kraken password is never needed here.
3. Click **Save Kraken keys**, then **Check account**. The check authenticates and reconciles the dedicated account without placing or validating an order; success does not establish trading permissions, live execution safety or profitability.
4. Optionally enter a Telegram bot token, use **Find my chat** after messaging your bot, select your chat, and save. All credentials are entered in the browser.
5. Select **Live**, explicitly confirm real-money trading, then click **Start live trading**. This requires a successful account check within the past 15 minutes and still applies every existing engine safeguard.

The panel shows saved account snapshots, positions, orders, decisions and events. **Stop bot safely** requests graceful reconciliation and entry cancellation while retaining native protective exits. Closing the browser tab does not stop the bot. Double-click the launcher again to reopen the existing panel. No process starts trading just because the panel opens.

The panel only listens on `127.0.0.1`, requires a per-launch session token for its API, and rejects cross-origin writes. Credentials are saved in the gitignored `.env` file with owner-only permissions (`0600`), in **plaintext**, and never returned to the browser. They are accessible to programs running as your Mac user; this is not encrypted credential storage. Do not expose this panel through a tunnel or reverse proxy. The launcher URL is a local session credential—do not share it.

### Terminal alternative

Python 3.11+ on macOS or Linux (the process lock uses `fcntl`). With `uv` installed:

```sh
uv sync --frozen --no-editable --reinstall-package wildcat3
uv run --no-sync wildcat3 web
```

`web` opens the same browser panel; `--no-browser` prints its session URL and `--port 8766` selects a different local port. No `.env` editing is needed. The launcher uses a regular package installation to avoid macOS/Python ignoring hidden editable-install path files; `--no-sync` preserves that installation. For the original terminal dashboard and developer tools:

```sh
uv sync --extra dev
cp .env.example .env
uv run wildcat3 doctor
uv run wildcat3 run
```

A Rich terminal dashboard shows equity, free EUR, fee-adjusted position PnL, native order coverage, realized returns, closed-trade win/loss counts, drawdown, recent logs, API health and candidate rejection reasons. `Ctrl-C` cancels known pending entry orders and retains protective exits. `uv run wildcat3 status` reads the last stored view even while the runner is active; its timestamp identifies stale/offline data.

```sh
uv run wildcat3 run --once --no-ui
uv run pytest
uv run ruff check src tests
```

The first command performs one public-data paper cycle. It can legitimately produce no trade. Paper stops are simulated locally and cannot run while this process is offline; this is not evidence of live exchange behavior. Each mode uses a separate SQLite database under `data/`.

## Strategy and capital allocation

Read [the strategy study](docs/STRATEGY.md) for the mathematical rationale, measured market data, modeling limitations and research commands. This is a selective hourly trend-continuation hypothesis, with BTC shock filtering and niche relative strength. Entry requires adequate EUR turnover, narrow spreads, exit-side depth, volume expansion and a volatility-scaled opportunity hurdle after fees. It avoids indicator-driven churn and makes no statistically unsupported claim of positive expectancy.

Orders are short-lived post-only limit buys with a native **conditional trailing-stop market sell** attached in the same Kraken request. Stops follow favorable price moves on Kraken, including while the computer is powered off. There is no local take-profit order competing with the protective stop. The default trailing distance is volatility-dependent, bounded between 2.5% and 6%.

Risk sizing budgets at most 0.5% of current equity (initially €1) for the trail distance plus modeled fees, spread and slippage. Allocation is capped at 30%, with €20 EUR reserve, one active pair, a six-hour entry cooldown, and at most two entry attempts per UTC day. Exchange lot/cost minima are checked **after rounding down**; undersized signals are skipped. These are planned-loss limits, not guaranteed maximum losses. Daily marked-equity losses of 2% or drawdown of 8% latch a circuit breaker. Marked equity values open positions at bid less estimated taker fees and slippage.

Current verified starter-tier fees are **0.40% maker and 0.80% taker**, effective July 2026; maker-entry/taker-exit costs about 1.20% before spread and slippage. Live mode reads the account's pair-specific fee schedule and refreshes it periodically. See [Kraken research and sources](docs/KRAKEN_RESEARCH.md); the fee assumptions are not hardcoded as live truth.

## Live account setup

1. Use a **dedicated Kraken account/balance** with exactly **€200 EUR**, no other assets, open orders, margin positions, Earn holdings or external trading bots. Disable automatic Earn transfers. The runner rejects unexplained cash/asset movements and never treats your wider holdings as its bankroll. Do not run another instance with the same account/API key, including on another computer.
2. Create a Kraken API key with balance/funds queries, open and closed order/trade queries, create/modify orders and cancel orders permissions. **Do not grant withdrawals, deposits or transfers.** Use IP restrictions where practical. Store the key only in your local `.env`; never commit it. `TradeVolume` requires query funds permission.
3. Enter `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` in the browser setup form. Its **Live** start button enables real trading only for the explicitly launched process. For terminal-only use, set `WILDCAT_LIVE_ENABLED=true` in `.env` and pass `--live`. Saving settings in the browser resets that file flag to false; the browser still supports an explicit live launch. A paper process never uses the private key.
4. Set up Telegram as below, and enable Kraken's own execution notifications for periods when this computer is off.
5. Review [Kraken-specific unresolved acceptance tests](docs/KRAKEN_RESEARCH.md) and [operations](docs/OPERATIONS.md), including tiny partial fills, inherited fee currency, child expiry and trailing-order limits. Validate actual behavior using the minimum practical live size before trusting unattended operation. A REST `validate=true` response does not prove child creation or fills.

```sh
uv run wildcat3 preflight --live
uv run wildcat3 run --live
```

`preflight` authenticates, reconciles, and validates a risk-sized sample order with Kraken's `validate=true`. It places no order. The second command submits real orders when qualifying signals exist. No live orders were submitted while developing this repository.

## Persistence and offline operation

SQLite uses WAL, full synchronous commits, deduplicated execution IDs, durable order intents, persisted nonce and a notification outbox. Each database has an exclusive process lock. Intents are committed **before** the exchange request. Unknown outcomes are not blindly retried: they block entries until open/closed history identifies the original request or an operator records an authoritative rejection.

Startup and every trading cycle reconcile complete paginated history, known order IDs, native child links, balances and executed volumes. Protective quantities must equal owned inventory. Missing, expired, unexpected or insufficient stops halt entries and alert; only known entry parents may be canceled. Protective orders are never canceled automatically, including at shutdown or circuit-breaker activation. There is deliberately no cancel-all dead-man timer: it would remove the stops when the machine goes offline.

Native OTO removes the host-side gap between entry and protection submission. **Absolute “never naked” cannot be guaranteed:** exchange rejection/outage, market price protection, a stop gap, extreme partial-fill fragmentation or insufficient base inventory can still prevent liquidation. Current Kraken documentation limits active trailing orders to 20 and does not fully specify every conditional-child edge case. A local application cannot repair or send alerts while powered off. If those residual risks are unacceptable, do not enable live execution. Inspect Kraken directly after any critical coverage alert; the bot does not blindly create extra sell orders that could race existing stops.

## Telegram notifications

Create a bot using Telegram's BotFather, message the bot from your phone, and set `TELEGRAM_BOT_TOKEN` and your numeric `TELEGRAM_CHAT_ID`. The application sends startup/reconciliation, each observed execution (price, size, fees and realized EUR/% PnL), critical errors and shutdown notices. The outbox retries failures with bounded exponential backoff; deduplicated fills prevent duplicate event creation. Delivery is at least once, so a lost Telegram acknowledgment can cause a duplicate message. Alerts arrive after polling observes the event, and downtime fills are sent upon restart. Bot credentials and API secrets are not logged. No messages are sent unless these variables are configured and you run the application.

## Research, export and operations

[STRATEGY.md](docs/STRATEGY.md) explains public market snapshots and chronological backtesting with fees, adverse fills and minute execution data. Hourly candles alone cannot establish whether a 60-second order filled, and historical books/queue priority are not recoverable from OHLC. All backtest output must retain those limitations. The repository includes meaningful failure-path tests, not a claim of live acceptance.

`uv run wildcat3 export --output data/executions.csv` exports the ledger for review. It is not a French tax return. For a French resident, Kraken's Irish MiCA passport is recorded by the AMF, but authorization is not a capital guarantee or a certification of this bot, profitability, account eligibility or your personal tax compliance. Verify applicable obligations yourself using the official links in the research document.

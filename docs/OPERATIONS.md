# Operating WildCat-3

## Start with a public-data dry run

Run commands from the repository root; paths and `.env` are relative to the project. Python 3.11 or newer is required. `uv sync --frozen --extra dev` installs exact dependency versions from `uv.lock`. Copy `.env.example` to `.env`, set permissions with `chmod 600 .env`, and leave `WILDCAT_LIVE_ENABLED=false` initially.

```sh
uv run wildcat3 doctor
uv run wildcat3 run --once --no-ui
uv run wildcat3 status
uv run wildcat3 run
```

`doctor` reads Kraken's country-filtered EUR pair metadata and current books. It never authenticates. The paper process runs the actual strategy against public data, maintaining €200 initial simulated cash and local simulated conditional orders. Signals require seven days of completed hourly history, so an empty or discontinuous series cannot trigger a trade. No trade is a normal result, including in the recorded market snapshot.

Use a tall terminal for the full dashboard. `--no-ui` runs without an alternate-screen interface and prints an initial snapshot; use `status` in another terminal to inspect the latest persisted state. `Ctrl-C` or SIGTERM requests an orderly shutdown. Each SQLite path admits one running process; paper and live must use separate databases. The account/API key must also be exclusive across different database paths and machines, which a local file lock cannot enforce.

## Verify the exchange account before live execution

Use a dedicated spot balance holding exactly €200, with no other assets, orders, margin positions or Earn balances. Deposit EUR externally before setup; this bot has no funding, withdrawal or conversion functions. Do not fund with €200 equivalent in USD/stablecoins. Disable automatic Earn transfers and stop any other account automation. Once initialized, fees, trades and compounding change cash normally; the bot does not keep resetting the account to €200.

Create an API key with query funds/balances, open and closed orders/trades, create/modify orders and cancel orders. Never enable withdrawals or transfers. Use a separate key for this process and do not share its nonce stream. Set your credentials in `.env`, plus `WILDCAT_LIVE_ENABLED=true`, then run:

```sh
uv run wildcat3 preflight --live
```

Preflight uses private read endpoints, records the account inception, reconciles state and sends one **validate-only** native OTO payload. It never calls the matching engine with `validate=false`. Success confirms accepted parameters and read access, not that a real fill will create a valid child under every condition. If no risk-sized sample clears minima/depth, validation is refused. A preflight key needs the order-creation permission even though no order executes.

Before unattended use, verify actual conditional-child behavior with Kraken and a minimum practical controlled live trade: each partial fill gets a child; child `refid` links to the entry; child is immediately active and GTC even after the parent expires; relative trailing distance or absolute `stopprice` respects the intended loss bound; quote-fee preference (`fciq`) is present; actual settlement is in EUR; and partial fills below normal minimum/near the 20-trailing-order limit behave safely. The runner halts if any observable invariant fails. These cases are **not certified by this repository's mocked tests**. The current REST metadata does not provide a universal documented guarantee for every case.

```sh
uv run wildcat3 run --live
```

This enables real autonomous orders whenever the preconditions and strategy qualify. `--live` alone is insufficient unless the environment also enables live mode. A paper process never loads private Kraken credentials. There is no margin/leverage parameter, no sell-short route, and the adapter rejects arbitrary markets and unprotected buy requests.

## State machine and recovery

| Observation | Durable behavior | What remains on Kraken |
|---|---|---|
| New eligible signal | Commit `SUBMITTING` intent before HTTP | Nothing until Kraken accepts |
| Definitive acknowledgement | Save order ID and `ACKNOWLEDGED` | Short GTD passive buy, attached native trailing close template |
| Timeout / invalid acknowledgement | Save `UNCERTAIN`; block entries and repair retries | Possibly the original order and its child stops |
| Fill / partial fill | Deduplicate trade ID; commit fill and notification together | Matching native child, independently active |
| Parent canceled/expired | Mark intent `CLOSED`; reconcile actual executed volume | Existing child stops remain |
| Stop fill while offline | Rebuild cost, cash, realized PnL and holdings on restart | Remaining children, if any |
| Circuit breaker | Persist entry halt; cancel only owned entry parents | All protective stops remain |
| Confirmed missing coverage | Halt, alert, cancel parent remainder, attempt one scoped repair after terminal parent confirmation | One native GTC stop-loss for proven uncovered inventory if accepted |
| Unknown repair outcome | Persist uncertainty; never blindly resubmit | Possibly the original repair stop |

History is fully paginated with a fixed upper window, overlapping lower bounds, unique execution IDs and a cursor captured before the read sweep. Unresolved intents retain their earlier history window. Child metadata is queried directly if its execution appears between the closed-orders and trades-history reads. Nonces survive restarts. SQLite WAL plus `synchronous=FULL` makes the intent durable before network submission. Back up the database using SQLite's backup mechanism (see below), not by copying only the `.sqlite3` file while WAL writes are active.

Reconciliation does not suppress real executions merely because protection fails: the fill and holding are published first. A position without a current price is marked **awaiting price**, not hidden behind stale flat-equity figures. Private monitoring runs before public market-data retrieval so a quote outage cannot prevent observing executions. REST rate limiting can make a complete polling cycle longer than the configured 15-second inter-cycle delay; alerts are delivered when observed, not guaranteed within 15 seconds of an exchange fill.

### Critical missing protection

Inspect the exchange directly. The runner never cancels an existing child merely to place another stop. It can attempt one native stop-loss repair only after three consistent reconciliation attempts establish missing quantity, all relevant entry parents are terminal, balances match the ledger, all orders are known, and no submission is unresolved. Quantity is exact owned inventory less verified protection; it is never rounded up. The stop uses the original risk floor, or just below the current bid if that floor was already breached. This cannot undo a loss already incurred.

The repair has its own persisted intent and the same no-blind-retry rule. Insufficient minimum size, rejected repair, ambiguous response, unknown fee settlement, oversize/existing protection, missing market data or exchange unavailability requires operator action. Entry halt remains after successful repair. Stops can slip, be canceled by market price protection, fail when the exchange is unavailable or exceed the documented active trailing-order limit. Neither the bot nor a native stop guarantees a maximum loss.

### Entry halt / circuit breaker

First stop the runner and resolve the actual exchange condition. A circuit breaker retains stops instead of canceling them or forcing a racing market sell. Then:

```sh
uv run wildcat3 resume --live
uv run wildcat3 run --live
```

`resume` clears the administrative halt only. It cannot erase the high-water mark, reset the €200 initial floor, bypass daily loss limits, accept foreign inventory or resolve an unknown request. A still-active risk condition will halt again. An 8% drawdown requires strategy/account review; this CLI intentionally cannot reset historical losses just to continue trading.

### Ambiguous submission with no record

Normally the next authoritative open/closed/history query finds the original `userref`, including after a crash before its order ID was saved. An empty query is not proof that Kraken rejected it. Keep the bot blocked and obtain authoritative confirmation from Kraken before recording a non-acceptance:

```sh
uv run wildcat3 resolve-unknown --live --ref 123456 \
  --confirmed-not-accepted --reason 'Kraken support verification reference and date'
```

Use the actual reference from `intents` in the database, not the example. The command requires an unresolved intent, at least five minutes since submission, an exclusive database lock and an explicit audit note. It does not cancel anything or treat elapsed time as proof. Never use it merely to clear a warning. On next boot the account is still fully reconciled; an order that later appears is adopted and audited.

### Fee settlement and external activity

`fciq` is a fee preference, not a universal settlement guarantee. The bot verifies quote preference on protective orders and audits actual base/EUR balances against the fill ledger. It requests trade ledger references for diagnosis but does **not** automatically reinterpret base-paid fee transactions. A base-fee fallback, manual deposit/withdrawal, extra trade, staking transfer or changed API account halts new entries. Preserve the original database and inspect Kraken ledger records; do not delete history to make balances appear correct. A protective order whose quantity exceeds actual available base inventory may fail at trigger time, so this condition needs immediate exchange review.

## Mobile alerts and offline hours

Telegram is optional in paper mode but strongly advised for live operation. Create a bot with Telegram's official BotFather, start a conversation from your phone, obtain your numeric chat ID using Telegram's Bot API, and set both variables. See [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage). Avoid putting a real token into shared shell history or screenshots. The application uses HTTPS POST and does not print token-bearing URLs.

Events include every observed entry/exit (size, EUR price/fee and realized EUR/%), startup with reconciliation summary, emergency stops and critical errors. Notifications are persisted transactionally with fills and retried with exponential backoff. Delivery is at least once: an acknowledgment lost after successful delivery can produce a duplicate notification. Notifications cannot be generated by a powered-off host; Kraken's own mobile execution notifications are the independent channel during offline hours. The app reports downtime fills on restart.

Paper fills/stops use sampled quotes, not the exchange matching engine. Resting entry requires trade-through plus depth participation; observed stop exits walk available bids with an additional slippage penalty. Unobserved intrapoll peaks/reversals, queues, MPP and exact last-trade triggers cannot be reconstructed. Observation gaps over two minutes with working orders are durably flagged. Do not interpret gapped paper PnL as validated offline parity.

## Inspection, export, backup and tests

```sh
uv run wildcat3 status --live
uv run wildcat3 export --live --output data/executions.csv
uv run pytest -q
uv run ruff check src tests
```

Read a consistent copy with SQLite's backup facility:

```sh
sqlite3 data/live.sqlite3 '.backup data/live-backup.sqlite3'
```

Store backups privately. The database contains execution history, account metadata, event text and an API-key fingerprint, but no raw API secret/token. Keep `.env`, `data/` and backups out of source control. Do not restore a stale database while a newer process is operating. A restore must use the same account/key and reconcile all subsequent history before trading.

Tests cover signing against Kraken's published vector, strict EUR/spot payload boundaries, ambiguous acknowledgement handling, paginated history, incomplete candle exclusion, fee units, risk sizing, durable fill/outbox transactions, instance locks, partial fills, delayed children, offline fills, emergency protection, and strategy/backtest lookahead controls. CI runs offline tests on Python 3.11 and 3.13. Public smoke checks are separate; no live execution or Telegram delivery was used for repository validation.

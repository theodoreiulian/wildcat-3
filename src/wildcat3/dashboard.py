from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def money(value, signed=False):
    if value is None:
        return "awaiting price"
    try:
        d = Decimal(str(value))
        return f"€{d:+,.2f}" if signed else f"€{d:,.2f}"
    except (InvalidOperation, ValueError):
        return "—"


def render(view, events, notification_health=""):
    status = str(view.get("status", "STARTING"))
    color = "red" if status in {"HALTED", "UNCERTAIN", "DEGRADED"} else "green"
    title = Text(f"WILDCAT-3  ·  {view.get('mode', 'paper').upper()}  ·  EUR SPOT", style="bold cyan")
    title.append(f"    {status}", style=f"bold {color}")
    metrics = Table.grid(expand=True, padding=(0, 2))
    for _ in range(4):
        metrics.add_column(ratio=1)
    metrics.add_row("Liquidation equity", "Available EUR", "Realized / Unrealized", "Closed wins / losses")
    metrics.add_row(money(view.get("equity")), money(view.get("available")),
                    money(view.get("realized", 0), True) + " / " + money(view.get("unrealized"), True),
                    f"{view.get('wins', 0)} / {view.get('losses', 0)}")
    metrics.add_row(f"Cash {money(view.get('cash'))}", f"Fees {money(view.get('fees', 0))}",
                    f"Drawdown {float(view.get('drawdown', 0)):.2f}%", "Initial bankroll €200.00")
    positions = Table("Position", "Owned quantity", "Cost / unit", "Est. exit value", "Unrealized", box=box.SIMPLE, expand=True)
    for p in view.get("positions", []):
        positions.add_row(p["symbol"], p["quantity"], money(p["average"]), money(p.get("value")), money(p.get("pnl"), True))
    if not view.get("positions"):
        positions.add_row("No position", "—", "—", "—", "—")
    orders = Table("Kraken order", "Pair", "Side / type", "Remaining", "Price / stop", "Coverage", box=box.SIMPLE, expand=True)
    for order in view.get("orders", []):
        orders.add_row(order["id"], order["symbol"], order["side"] + " / " + order["type"], order["quantity"],
                       str(order["price"]), "Verified" if order.get("protected") else "Pending / unverified")
    if not view.get("orders"):
        orders.add_row("No working orders", "—", "—", "—", "—", "—")
    scan = Table.grid(padding=(0, 2))
    scan.add_column(style="cyan", no_wrap=True)
    scan.add_column()
    for symbol, reason in view.get("reasons", {}).items():
        if symbol != "BTC/EUR":
            scan.add_row(symbol, Text(reason))
    logs = Table.grid(padding=(0, 1))
    for event in reversed(events[:8]):
        stamp = datetime.fromtimestamp(event["time"], timezone.utc).strftime("%H:%M:%S")
        logs.add_row(stamp, event["level"], Text(event["message"]))
    stamp = view.get("last_reconciled")
    freshness = datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds") if stamp else "never"
    health = Text(f"{view.get('health', 'Starting')}\nLast reconciliation: {freshness}\n{notification_health}")
    if view.get("mode") == "paper":
        health.append("\nPaper: sampled local fills/stops; protection does not operate while offline.", style="yellow")
    return Group(title, Panel(metrics, border_style="cyan"), positions, orders,
                 Panel(scan, title="Candidate decisions"), Panel(health, title="Connection & runtime"),
                 Panel(logs, title="Recent events · UTC"))

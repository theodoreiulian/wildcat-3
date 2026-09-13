import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    db_path: Path = Path("data/paper.sqlite3")
    bankroll: Decimal = Decimal("200")
    risk_fraction: Decimal = Decimal("0.005")
    max_allocation: Decimal = Decimal("0.30")
    reserve_eur: Decimal = Decimal("20")
    maker_fee: Decimal = Decimal("0.004")
    taker_fee: Decimal = Decimal("0.008")
    slippage_bps: Decimal = Decimal("30")
    daily_loss_fraction: Decimal = Decimal("0.02")
    drawdown_fraction: Decimal = Decimal("0.08")
    poll_seconds: float = 15
    entry_ttl: int = 60
    cooldown_seconds: int = 6 * 3600
    max_entries_per_day: int = 2
    max_book_age: float = 15
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    telegram_chat: str = field(default="", repr=False)

    def __post_init__(self):
        if self.mode not in {"paper", "live"}:
            raise ValueError("mode must be paper or live")
        if self.bankroll != Decimal("200"):
            raise ValueError("WildCat-3 initial bankroll must be exactly EUR 200")
        if not Decimal("0") < self.risk_fraction <= Decimal("0.005"):
            raise ValueError("Risk may not exceed 0.5% per entry")
        if not Decimal("0") < self.max_allocation <= Decimal("0.30"):
            raise ValueError("Allocation may not exceed 30%")
        if self.mode == "live" and not (self.api_key and self.api_secret):
            raise ValueError("Live mode requires KRAKEN_API_KEY and KRAKEN_API_SECRET")
        if not (5 <= self.poll_seconds <= 60 and 10 <= self.entry_ttl <= 120):
            raise ValueError("Invalid polling interval or entry expiry")
        if not (Decimal("0") <= self.maker_fee <= Decimal("0.05")
                and Decimal("0") <= self.taker_fee <= Decimal("0.05")):
            raise ValueError("Invalid fee rates")

    @classmethod
    def from_env(cls, mode: str = "paper", db: str | None = None):
        if mode == "live" and os.getenv("WILDCAT_LIVE_ENABLED", "").lower() != "true":
            raise ValueError("Set WILDCAT_LIVE_ENABLED=true and pass --live to enable real orders")
        return cls(
            mode=mode, db_path=Path(db or f"data/{mode}.sqlite3"),
            api_key=os.getenv("KRAKEN_API_KEY", "") if mode == "live" else "",
            api_secret=os.getenv("KRAKEN_API_SECRET", "") if mode == "live" else "",
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat=os.getenv("TELEGRAM_CHAT_ID", ""),
            maker_fee=Decimal(os.getenv("WILDCAT_PAPER_MAKER_FEE", "0.004")),
            taker_fee=Decimal(os.getenv("WILDCAT_PAPER_TAKER_FEE", "0.008")),
        )

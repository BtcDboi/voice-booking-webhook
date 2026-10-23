"""Configuration is read at startup, never at import time."""

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    secret: str = field(repr=False)
    db_path: Path = Path("data/bookings.sqlite3")
    timezone: str = "America/New_York"
    opens: time = time(9)
    closes: time = time(17)
    slot_minutes: int = 30
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    secret_header: str = "X-Vapi-Secret"
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.secret.strip():
            raise ValueError("WEBHOOK_SECRET must be set")
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.secret_header):
            raise ValueError("SECRET_HEADER must be an HTTP header name")
        ZoneInfo(self.timezone)
        if self.opens >= self.closes or any(
            t.second or t.microsecond or t.tzinfo for t in (self.opens, self.closes)
        ):
            raise ValueError("Business hours must be same-day, minute-precision local times")
        if not 1 <= self.slot_minutes <= 1440:
            raise ValueError("SLOT_MINUTES must be between 1 and 1440")
        if not self.weekdays or any(day not in range(7) for day in self.weekdays):
            raise ValueError("BUSINESS_DAYS must contain weekdays from 0 to 6")
        if bool(self.telegram_token) != bool(self.telegram_chat_id):
            raise ValueError("Set both Telegram settings, or neither")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            secret=os.environ.get("WEBHOOK_SECRET", ""),
            db_path=Path(os.getenv("DB_PATH", "data/bookings.sqlite3")),
            timezone=os.getenv("BUSINESS_TIMEZONE", "America/New_York"),
            opens=time.fromisoformat(os.getenv("BUSINESS_OPEN", "09:00")),
            closes=time.fromisoformat(os.getenv("BUSINESS_CLOSE", "17:00")),
            slot_minutes=int(os.getenv("SLOT_MINUTES", "30")),
            weekdays=tuple(int(day) for day in os.getenv("BUSINESS_DAYS", "0,1,2,3,4").split(",")),
            secret_header=os.getenv("SECRET_HEADER", "X-Vapi-Secret"),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )

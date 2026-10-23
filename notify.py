"""Best-effort alerts carry booking metadata, never caller details."""

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from db import Booking

logger = logging.getLogger("voice_booking")


class Notifier(Protocol):
    def notify(self, booking: Booking) -> None: ...


class LogNotifier:
    def notify(self, booking: Booking) -> None:
        logger.info("new_booking booking_id=%s start=%s", booking["booking_id"], booking["start"])


@dataclass
class TelegramNotifier:
    token: str = field(repr=False)
    chat_id: str = field(repr=False)

    def notify(self, booking: Booking) -> None:
        # httpx's INFO request log includes Telegram's token-bearing URL.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        response = httpx.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": f"New booking #{booking['booking_id']} at {booking['start']}",
            },
            timeout=3.0,
        )
        response.raise_for_status()
        if response.json().get("ok") is not True:
            raise RuntimeError("Telegram did not accept the alert")


def notify_safely(notifier: Notifier, booking: Booking) -> None:
    try:
        notifier.notify(booking)
    except Exception:
        # Never log exception text: it may contain a token-bearing request URL.
        logger.warning("booking_notification_failed booking_id=%s", booking["booking_id"])

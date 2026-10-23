import json
import logging
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from db import Booking
from notify import LogNotifier, TelegramNotifier, notify_safely
from tests.conftest import HEADERS, tool_payload


def test_notifier_failure_does_not_break_booking(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingNotifier:
        def notify(self, booking: Booking) -> None:
            raise RuntimeError("private exception details must not appear in logs")

    with TestClient(create_app(settings, FailingNotifier())) as client:
        response = client.post("/vapi/tools", headers=HEADERS, json=tool_payload())
        assert response.status_code == 200
        assert json.loads(response.json()["results"][0]["result"])["booked"] is True
        assert len(client.get("/bookings?date=2030-01-07", headers=HEADERS).json()["bookings"]) == 1
    assert "booking_notification_failed" in caplog.text
    assert "private exception" not in caplog.text


def test_log_notification_omits_caller_details(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="voice_booking"):
        LogNotifier().notify(
            {
                "booking_id": 1,
                "start": "2030-01-07T09:00:00-05:00",
                "name": "Jordan Lee",
                "phone": "+1 555 0100",
            }
        )
    assert "new_booking booking_id=1" in caplog.text
    assert "Jordan" not in caplog.text and "555" not in caplog.text


@pytest.mark.parametrize(
    "status,body", [(200, {"ok": True}), (200, {"ok": False}), (503, {"ok": False})]
)
def test_telegram_is_mocked_and_failures_are_contained(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
    body: Any,
) -> None:
    def fake_post(url: str, *, json: Any, timeout: float) -> httpx.Response:
        assert url.endswith("/sendMessage") and timeout == 3.0
        assert json["text"] == "New booking #1 at 2030-01-07T09:00:00-05:00"
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    notify_safely(
        TelegramNotifier("test-only-bot-placeholder", "test-only-chat-placeholder"),
        {"booking_id": 1, "start": "2030-01-07T09:00:00-05:00"},
    )
    assert ("booking_notification_failed" in caplog.text) == (status != 200 or not body["ok"])
    assert "test-only-bot-placeholder" not in caplog.text


@pytest.mark.parametrize(
    "changes",
    [
        {"secret": ""},
        {"slot_minutes": 0},
        {"weekdays": (7,)},
        {"secret_header": "Bad Header"},
        {"telegram_token": "test-only-placeholder"},
    ],
)
def test_invalid_configuration_fails_at_startup(settings: Settings, changes: Any) -> None:
    with pytest.raises(ValueError):
        replace(settings, **changes)


def test_configuration_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "test-only-placeholder")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Europe/Berlin")
    monkeypatch.setenv("BUSINESS_OPEN", "08:30")
    monkeypatch.setenv("SLOT_MINUTES", "45")
    monkeypatch.setenv("BUSINESS_DAYS", "1,3")
    settings = Settings.from_env()
    assert settings.timezone == "Europe/Berlin" and settings.opens.isoformat() == "08:30:00"
    assert settings.slot_minutes == 45 and settings.weekdays == (1, 3)
    assert "test-only-placeholder" not in repr(settings)

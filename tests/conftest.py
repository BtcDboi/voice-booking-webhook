import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import create_app
from config import Settings
from db import Booking

# Deliberately public test fixture, never a deployment credential.
TEST_SECRET = "test-only-placeholder"
HEADERS = {"X-Vapi-Secret": TEST_SECRET}


class RecordingNotifier:
    def __init__(self) -> None:
        self.bookings: list[Booking] = []

    def notify(self, booking: Booking) -> None:
        self.bookings.append(booking)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(secret=TEST_SECRET, db_path=tmp_path / "bookings.sqlite3")


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def client(settings: Settings, notifier: RecordingNotifier) -> Iterator[TestClient]:
    with TestClient(create_app(settings, notifier)) as client:
        yield client


def tool_payload(
    name: str = "book_appointment",
    arguments: Any = None,
    call_id: str = "demo-tool-booking",
) -> dict[str, Any]:
    if arguments is None:
        arguments = {
            "start": "2030-01-07T09:00:00-05:00",
            "name": "Jordan Lee",
            "phone": "+1 555 0100",
            "reason": "Routine appointment",
        }
    return {
        "message": {
            "type": "tool-calls",
            "toolCallList": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            ],
        }
    }


def end_report(result: Any = None, summary: str = "The appointment was booked.") -> dict[str, Any]:
    messages = (
        []
        if result is None
        else [
            {
                "role": "tool_call_result",
                "name": "book_appointment",
                "toolCallId": "demo-tool-booking",
                "result": result,
                "time": 0,
                "secondsFromStart": 0,
            }
        ]
    )
    return {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "demo-call"},
            "analysis": {"summary": summary},
            "artifact": {"messages": messages},
        }
    }

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import create_app
from booking import BookingService
from config import Settings
from db import Database
from tests.conftest import HEADERS, RecordingNotifier, tool_payload


def invoke(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/vapi/tools", headers=HEADERS, json=payload)
    assert response.status_code == 200
    value = response.json()["results"][0]["result"]
    assert isinstance(value, str) and "\n" not in value
    return json.loads(value)


def test_availability_lists_only_full_slots_in_business_hours(client: TestClient) -> None:
    result = invoke(client, tool_payload("check_availability", {"date": "2030-01-07"}))
    assert result["timezone"] == "America/New_York"
    assert len(result["slots"]) == 16
    assert result["slots"][0] == "2030-01-07T09:00:00-05:00"
    assert result["slots"][-1] == "2030-01-07T16:30:00-05:00"


def test_closed_weekdays_have_no_slots(client: TestClient) -> None:
    assert invoke(client, tool_payload("check_availability", {"date": "2030-01-06"}))["slots"] == []


def test_booking_commits_and_disappears_from_availability(
    client: TestClient,
    notifier: RecordingNotifier,
) -> None:
    result = invoke(client, tool_payload())
    assert result == {"booked": True, "booking_id": 1, "start": "2030-01-07T09:00:00-05:00"}
    slots = invoke(client, tool_payload("check_availability", {"date": "2030-01-07"}))["slots"]
    assert len(slots) == 15 and result["start"] not in slots
    calendar = client.get("/bookings?date=2030-01-07", headers=HEADERS).json()
    assert calendar["bookings"] == [{"booking_id": 1, "start": result["start"]}]
    assert "Jordan" not in json.dumps(calendar)
    assert len(notifier.bookings) == 1


def test_double_booking_is_rejected(client: TestClient) -> None:
    assert invoke(client, tool_payload())["booked"] is True
    assert invoke(client, tool_payload(call_id="demo-other-tool")) == {
        "booked": False,
        "reason": "slot_taken",
    }


def test_identical_webhook_retry_returns_original_booking_without_second_alert(
    client: TestClient,
    notifier: RecordingNotifier,
) -> None:
    first = invoke(client, tool_payload())
    assert invoke(client, tool_payload()) == first
    assert len(notifier.bookings) == 1


def test_reusing_tool_id_with_different_arguments_is_an_error(client: TestClient) -> None:
    invoke(client, tool_payload())
    payload = tool_payload()
    payload["message"]["toolCallList"][0]["function"]["arguments"]["start"] = (
        "2030-01-07T10:00:00-05:00"
    )
    response = client.post("/vapi/tools", json=payload, headers=HEADERS)
    assert response.json()["results"][0]["error"] == "tool_call_id_reused"


@pytest.mark.parametrize(
    "start",
    [
        "2030-01-07T08:30:00-05:00",
        "2030-01-07T17:00:00-05:00",
        "2030-01-06T10:00:00-05:00",
        "2030-01-07T16:45:00-05:00",
    ],
)
def test_outside_hours_never_creates_a_booking(client: TestClient, start: str) -> None:
    result = invoke(
        client,
        tool_payload(arguments={"start": start, "name": "Jordan Lee", "phone": "+1 555 0100"}),
    )
    assert result == {"booked": False, "reason": "outside_hours"}


@pytest.mark.parametrize(
    "start",
    [
        "not-a-time",
        "2030-01-07T09:00:00",
        "2030-01-07T09:15:00-05:00",
        "2030-01-07T09:00:01-05:00",
        "2030-01-07T09:00:00-04:00",
        "2030-01-07",
    ],
)
def test_invalid_or_unaligned_time_is_rejected(client: TestClient, start: str) -> None:
    result = invoke(
        client,
        tool_payload(arguments={"start": start, "name": "Jordan Lee", "phone": "+1 555 0100"}),
    )
    assert result == {"booked": False, "reason": "invalid_time"}


def test_dst_spring_gap_is_not_offered_and_cannot_be_booked(settings: Settings) -> None:
    settings = replace(settings, opens=time(0), closes=time(4), weekdays=(6,))
    with TestClient(create_app(settings, RecordingNotifier())) as client:
        slots = invoke(client, tool_payload("check_availability", {"date": "2030-03-10"}))["slots"]
        assert len(slots) == 6
        assert all("T02:" not in slot for slot in slots)
        result = invoke(
            client,
            tool_payload(
                arguments={
                    "start": "2030-03-10T02:30:00-05:00",
                    "name": "Jordan Lee",
                    "phone": "+1 555 0100",
                }
            ),
        )
        assert result == {"booked": False, "reason": "invalid_time"}


def test_dst_fall_repeated_hour_has_two_distinct_bookable_instants(settings: Settings) -> None:
    settings = replace(settings, opens=time(0), closes=time(4), weekdays=(6,))
    with TestClient(create_app(settings, RecordingNotifier())) as client:
        slots = invoke(client, tool_payload("check_availability", {"date": "2030-11-03"}))["slots"]
        assert len(slots) == 10
        for index, offset in enumerate(["-04:00", "-05:00"]):
            start = f"2030-11-03T01:30:00{offset}"
            assert start in slots
            assert (
                invoke(
                    client,
                    tool_payload(
                        arguments={"start": start, "name": "Jordan Lee", "phone": "+1 555 0100"},
                        call_id=f"demo-fold-{index}",
                    ),
                )["booked"]
                is True
            )


def test_configured_slot_length_keeps_real_elapsed_duration_across_dst(settings: Settings) -> None:
    service = BookingService(
        replace(settings, opens=time(0), closes=time(4), weekdays=(6,), slot_minutes=45),
        Database(settings.db_path),
    )
    slots = service.slots(date(2030, 11, 3))
    instants = [slot.astimezone(UTC) for slot in slots]
    assert len(instants) == 6
    assert all(
        (later - earlier).total_seconds() == 2700
        for earlier, later in zip(instants, instants[1:], strict=False)
    )


def test_nonexistent_opening_time_closes_that_date(settings: Settings) -> None:
    service = BookingService(
        replace(settings, opens=time(2), closes=time(4), weekdays=(6,)), Database(settings.db_path)
    )
    assert service.slots(date(2030, 3, 10)) == []


def test_simultaneous_requests_cannot_double_book(settings: Settings) -> None:
    database = Database(settings.db_path)
    database.initialize()
    service = BookingService(settings, database)

    def attempt(index: int) -> bool:
        result, _ = service.book(
            tool_call_id=f"demo-race-{index}",
            start="2030-01-07T09:00:00-05:00",
            name="Jordan Lee",
            phone="+1 555 0100",
        )
        return result["booked"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(attempt, range(8))).count(True) == 1
    assert len(database.bookings_for_date("2030-01-07")) == 1


def test_bookings_survive_a_new_application_instance(settings: Settings) -> None:
    with TestClient(create_app(settings, RecordingNotifier())) as client:
        invoke(client, tool_payload())
    with TestClient(create_app(settings, RecordingNotifier())) as client:
        rows = client.get("/bookings?date=2030-01-07", headers=HEADERS).json()["bookings"]
        assert len(rows) == 1
        assert datetime.fromisoformat(rows[0]["start"]).utcoffset().total_seconds() == -18000

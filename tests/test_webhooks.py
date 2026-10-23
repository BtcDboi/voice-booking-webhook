import json
import logging
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import create_app
from classify import WARNING, classify_call
from config import Settings
from db import Database
from tests.conftest import HEADERS, RecordingNotifier, end_report, tool_payload


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/vapi/tools", tool_payload()),
        ("/vapi/events", end_report()),
        ("/bookings?date=2030-01-07", None),
    ],
)
@pytest.mark.parametrize("headers", [{}, {"X-Vapi-Secret": "wrong-placeholder"}])
def test_missing_or_wrong_secret_is_401(
    client: TestClient,
    path: str,
    payload: Any,
    headers: dict[str, str],
) -> None:
    response = (
        client.get(path, headers=headers)
        if payload is None
        else client.post(path, headers=headers, json=payload)
    )
    assert response.status_code == 401


def test_custom_secret_header_is_supported(settings: Settings) -> None:
    with TestClient(create_app(replace(settings, secret_header="X-Booking-Secret"))) as client:
        assert client.post("/vapi/tools", json=tool_payload(), headers=HEADERS).status_code == 401
        assert (
            client.post(
                "/vapi/tools", json=tool_payload(), headers={"X-Booking-Secret": settings.secret}
            ).status_code
            == 200
        )


def test_health_needs_no_secret_and_checks_the_database(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_unknown_tool_returns_a_correlated_error_with_http_200(client: TestClient) -> None:
    response = client.post("/vapi/tools", headers=HEADERS, json=tool_payload("unknown", {}))
    assert response.status_code == 200
    assert response.json() == {
        "results": [{"toolCallId": "demo-tool-booking", "error": "unknown_tool"}]
    }


def test_both_tool_lists_are_deduplicated_and_json_arguments_are_accepted(
    client: TestClient,
    notifier: RecordingNotifier,
) -> None:
    payload = tool_payload()
    call = payload["message"]["toolCallList"][0]
    payload["message"]["toolWithToolCallList"] = [
        {
            "toolCall": {
                **call,
                "function": {
                    **call["function"],
                    "arguments": json.dumps(call["function"]["arguments"]),
                },
            }
        }
    ]
    response = client.post("/vapi/tools", headers=HEADERS, json=payload)
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1
    assert len(notifier.bookings) == 1


def test_tool_with_tool_call_list_alone_is_supported(client: TestClient) -> None:
    payload = tool_payload()
    call = payload["message"].pop("toolCallList")[0]
    payload["message"]["toolWithToolCallList"] = [{"toolCall": call}]
    response = client.post("/vapi/tools", headers=HEADERS, json=payload)
    assert json.loads(response.json()["results"][0]["result"])["booked"] is True


def test_batch_returns_one_correlated_response_per_call(client: TestClient) -> None:
    payload = tool_payload()
    payload["message"]["toolCallList"] += tool_payload("unknown", {}, "demo-unknown")["message"][
        "toolCallList"
    ]
    response = client.post("/vapi/tools", headers=HEADERS, json=payload)
    assert [row["toolCallId"] for row in response.json()["results"]] == [
        "demo-tool-booking",
        "demo-unknown",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"message": None},
        {"message": {"type": "tool-calls"}},
        tool_payload(arguments="{broken"),
        tool_payload(arguments="[]"),
        tool_payload(arguments=42),
        {"message": {"type": "tool-calls", "toolCallList": [{"id": "demo-missing-function"}]}},
    ],
)
def test_malformed_tool_payload_is_422_not_500(client: TestClient, payload: Any) -> None:
    assert client.post("/vapi/tools", headers=HEADERS, json=payload).status_code == 422


def test_invalid_json_body_is_rejected(client: TestClient) -> None:
    assert (
        client.post(
            "/vapi/tools", headers={**HEADERS, "Content-Type": "application/json"}, content="{"
        ).status_code
        == 422
    )


def test_excessively_nested_argument_json_is_422_not_500(client: TestClient) -> None:
    payload = tool_payload(arguments="[" * 2000 + "0" + "]" * 2000)
    assert client.post("/vapi/tools", headers=HEADERS, json=payload).status_code == 422


def test_malformed_later_call_prevents_partial_execution(
    client: TestClient,
    notifier: RecordingNotifier,
) -> None:
    payload = tool_payload()
    payload["message"]["toolCallList"].append({"id": "demo-broken"})
    assert client.post("/vapi/tools", headers=HEADERS, json=payload).status_code == 422
    assert not notifier.bookings
    assert client.get("/bookings?date=2030-01-07", headers=HEADERS).json()["bookings"] == []


def test_conflicting_duplicate_tool_ids_are_rejected(client: TestClient) -> None:
    payload = tool_payload()
    payload["message"]["toolCallList"] += tool_payload("unknown", {})["message"]["toolCallList"]
    assert client.post("/vapi/tools", headers=HEADERS, json=payload).status_code == 422


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"start": "x", "name": " ", "phone": " "},
        {"start": "x", "name": 42, "phone": "+1 555 0100"},
    ],
)
def test_invalid_function_arguments_produce_a_tool_error(
    client: TestClient, arguments: Any
) -> None:
    response = client.post("/vapi/tools", headers=HEADERS, json=tool_payload(arguments=arguments))
    assert response.status_code == 200
    assert response.json()["results"][0]["error"] == "invalid_arguments"


@pytest.mark.parametrize("day", ["2030-02-30", "tomorrow", 1234567890, "20300107"])
def test_availability_requires_a_real_iso_date(client: TestClient, day: Any) -> None:
    response = client.post(
        "/vapi/tools", headers=HEADERS, json=tool_payload("check_availability", {"date": day})
    )
    assert response.json()["results"][0]["error"] == "invalid_arguments"


def test_summary_says_booked_but_tool_never_ran_is_not_booked(
    client: TestClient,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = end_report()
    assert classify_call(report["message"]) == "not_booked"
    with caplog.at_level(logging.WARNING):
        response = client.post("/vapi/events", json=report, headers=HEADERS)
    assert response.json() == {"classification": "not_booked", "warnings": [WARNING]}
    assert WARNING in caplog.text
    with Database(settings.db_path).connect() as connection:
        outcome = connection.execute("SELECT * FROM outcomes").fetchone()
    assert outcome["classification"] == "not_booked" and outcome["warning"] == WARNING


@pytest.mark.parametrize(
    "result",
    [
        {"booked": False},
        '{"booked": false}',
        {"booked": "true"},
        {"booked": 1},
        "Success.",
        "booked=true",
        [],
        None,
    ],
)
def test_only_boolean_true_in_a_tool_result_can_count_as_booked(result: Any) -> None:
    assert classify_call(end_report(result)["message"]) == "not_booked"


@pytest.mark.parametrize("result", [{"booked": True}, '{"booked":true,"booking_id":1}'])
def test_successful_booking_result_overrides_a_negative_summary(
    client: TestClient, result: Any
) -> None:
    response = client.post(
        "/vapi/events", headers=HEADERS, json=end_report(result, "No appointment was booked.")
    )
    assert response.json() == {"classification": "booked", "warnings": []}


@pytest.mark.parametrize(
    "change",
    [
        {"role": "assistant"},
        {"name": "check_availability"},
        {"toolCallId": ""},
        {"error": "failed"},
    ],
)
def test_assistant_text_other_tools_and_failed_messages_are_not_booking_evidence(
    change: Any,
) -> None:
    report = end_report({"booked": True})["message"]
    report["artifact"]["messages"][0].update(change)
    assert classify_call(report) == "not_booked"


def test_a_requested_tool_without_a_result_is_not_a_booking() -> None:
    report = end_report()["message"]
    report["artifact"]["messages"] = [
        {
            "role": "tool_calls",
            "toolCalls": [
                {
                    "function": {"name": "book_appointment", "arguments": "{}"},
                    "id": "demo-tool-booking",
                }
            ],
        }
    ]
    assert classify_call(report) == "not_booked"


def test_negative_summary_does_not_trigger_a_mismatch_warning(client: TestClient) -> None:
    response = client.post(
        "/vapi/events", headers=HEADERS, json=end_report(summary="The appointment was not booked.")
    )
    assert response.json() == {"classification": "not_booked", "warnings": []}


def test_event_retry_updates_one_outcome_without_creating_a_booking(
    client: TestClient,
    settings: Settings,
) -> None:
    for _ in range(2):
        assert (
            client.post(
                "/vapi/events", headers=HEADERS, json=end_report({"booked": True})
            ).status_code
            == 200
        )
    with Database(settings.db_path).connect() as connection:
        assert connection.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM bookings").fetchone()[0] == 0


@pytest.mark.parametrize(
    "payload",
    [{}, {"message": {"type": "end-of-call-report"}}, {"message": {"type": "status-update"}}],
)
def test_malformed_or_unsupported_event_is_rejected(client: TestClient, payload: Any) -> None:
    assert client.post("/vapi/events", headers=HEADERS, json=payload).status_code == 422


def test_actual_booking_response_round_trips_into_end_of_call_evidence(client: TestClient) -> None:
    result = client.post("/vapi/tools", json=tool_payload(), headers=HEADERS).json()["results"][0]
    response = client.post("/vapi/events", json=end_report(result["result"]), headers=HEADERS)
    assert response.json()["classification"] == "booked"

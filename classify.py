"""Tool evidence decides the outcome; summary text can only trigger a warning.

https://docs.vapi.ai/server-url/events
https://raw.githubusercontent.com/VapiAI/server-sdk-typescript/main/src/api/types/ToolCallResultMessage.ts
https://raw.githubusercontent.com/VapiAI/server-sdk-typescript/main/src/api/types/ServerMessageEndOfCallReport.ts
"""

import json
import re
from typing import Any, Literal

WARNING = "summary_claims_booking_but_tool_failed"


def classify_call(report: dict[str, Any]) -> Literal["booked", "not_booked"]:
    """Accept the inner Vapi message, using only native artifact.messages evidence."""
    artifact = report.get("artifact")
    messages = artifact.get("messages", []) if isinstance(artifact, dict) else []
    if not isinstance(messages, list):
        return "not_booked"
    for message in messages:
        if not isinstance(message, dict) or (
            message.get("role") != "tool_call_result"
            or message.get("name") != "book_appointment"
            or not isinstance(message.get("toolCallId"), str)
            or not message["toolCallId"].strip()
            or message.get("error")
        ):
            continue
        result = message.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, RecursionError):
                continue
        if isinstance(result, dict) and result.get("booked") is True:
            return "booked"
    return "not_booked"


def summary_claims_booking(report: dict[str, Any]) -> bool:
    """A deliberately limited English heuristic, never a source of booking truth."""
    analysis = report.get("analysis")
    summary = analysis.get("summary") if isinstance(analysis, dict) else None
    if not isinstance(summary, str):
        return False
    for sentence in re.split(r"[.!?;]", summary.lower()):
        affirmative = re.search(r"\b(booked|scheduled|confirmed)\b", sentence)
        negative = re.search(
            r"\b(not|no|never|unable|failed|couldn't|didn't|wasn't|isn't|hasn't|unconfirmed)\b",
            sentence,
        )
        if affirmative and not negative:
            return True
    return False

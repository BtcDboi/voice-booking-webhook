"""Validate the entire envelope before any tool can mutate the calendar.

Formats checked 2026-10-24:
https://docs.vapi.ai/server-url/events
https://docs.vapi.ai/tools/custom-tools
https://docs.vapi.ai/server-url/server-authentication
"""

import json
import re
from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Function(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]

    @field_validator("arguments", mode="before")
    @classmethod
    def decode_arguments(cls, value: Any) -> Any:
        # The docs show objects; accept JSON-encoded objects too.
        try:
            return json.loads(value) if isinstance(value, str) else value
        except RecursionError as error:
            raise ValueError("Arguments exceed JSON nesting limits") from error


class ToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    type: Literal["function"] = "function"
    function: Function


class ToolWithCall(BaseModel):
    toolCall: ToolCall


class ToolMessage(BaseModel):
    type: Literal["tool-calls"]
    toolCallList: list[ToolCall] = Field(default_factory=list, max_length=50)
    toolWithToolCallList: list[ToolWithCall] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def consistent_calls(self) -> Self:
        calls: dict[str, ToolCall] = {}
        for call in self.toolCallList + [item.toolCall for item in self.toolWithToolCallList]:
            if call.id in calls and calls[call.id] != call:
                raise ValueError("Conflicting definitions for a tool call ID")
            calls[call.id] = call
        if not calls or len(calls) > 50:
            raise ValueError("Expected between 1 and 50 distinct tool calls")
        return self

    def calls(self) -> list[ToolCall]:
        return list(
            {
                call.id: call
                for call in self.toolCallList
                + [item.toolCall for item in self.toolWithToolCallList]
            }.values()
        )


class ToolEnvelope(BaseModel):
    message: ToolMessage


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AvailabilityArguments(Arguments):
    date: date

    @field_validator("date", mode="before")
    @classmethod
    def iso_date_only(cls, value: Any) -> Any:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Use YYYY-MM-DD")
        return value


class BookingArguments(Arguments):
    start: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=1, max_length=40)
    reason: str | None = Field(default=None, max_length=500)


class CallReference(BaseModel):
    id: str = Field(min_length=1, max_length=200)


class Artifact(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)


class Analysis(BaseModel):
    summary: str | None = None


class EndOfCallReport(BaseModel):
    type: Literal["end-of-call-report"]
    call: CallReference
    artifact: Artifact = Field(default_factory=Artifact)
    analysis: Analysis = Field(default_factory=Analysis)


class EventEnvelope(BaseModel):
    message: EndOfCallReport


def result_for(call_id: str, result: dict[str, Any]) -> dict[str, str]:
    # Vapi requires a single-line string, not a nested object in `result`.
    return {"toolCallId": call_id, "result": json.dumps(result, separators=(",", ":"))}

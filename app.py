"""Run with: uvicorn app:create_app --factory --env-file .env"""

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from booking import BookingService
from classify import WARNING, classify_call, summary_claims_booking
from config import Settings
from db import Database, ReplayConflict
from notify import LogNotifier, Notifier, TelegramNotifier, notify_safely
from vapi import AvailabilityArguments, BookingArguments, EventEnvelope, ToolEnvelope, result_for

logger = logging.getLogger("voice_booking")


def create_app(settings: Settings | None = None, notifier: Notifier | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.db_path)
    booking = BookingService(settings, database)
    if notifier is None:
        notifier = (
            TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)
            if settings.telegram_token
            else LogNotifier()
        )
    logging.basicConfig(level=logging.INFO)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        yield

    app = FastAPI(title="Maple Street Dental booking sample", lifespan=lifespan)

    def require_secret(request: Request) -> None:
        supplied = request.headers.get(settings.secret_header, "")
        # Bytes also handle non-ASCII headers without compare_digest raising TypeError.
        if not secrets.compare_digest(supplied.encode(), settings.secret.encode()):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    protected = [Depends(require_secret)]

    @app.exception_handler(RequestValidationError)
    async def invalid_payload(_: Request, __: RequestValidationError) -> JSONResponse:
        # Do not reflect caller details or headers through validation error bodies.
        return JSONResponse(status_code=422, content={"detail": "Invalid request payload or query"})

    @app.get("/health")
    def health() -> dict[str, str]:
        with database.connect() as connection:
            connection.execute("SELECT 1 FROM bookings LIMIT 1")
        return {"status": "ok"}

    @app.get("/bookings", dependencies=protected)
    def bookings(day: Annotated[date, Query(alias="date")]) -> dict[str, Any]:
        rows = database.bookings_for_date(day.isoformat())
        # Only the fields needed to inspect the calendar are exposed here.
        return {
            "date": day.isoformat(),
            "timezone": settings.timezone,
            "bookings": [{"booking_id": row["booking_id"], "start": row["start"]} for row in rows],
        }

    @app.post("/vapi/tools", dependencies=protected)
    def tools(payload: ToolEnvelope, background: BackgroundTasks) -> dict[str, Any]:
        results = []
        for call in payload.message.calls():
            try:
                if call.function.name == "check_availability":
                    args = AvailabilityArguments.model_validate(call.function.arguments)
                    result = booking.availability(args.date)
                elif call.function.name == "book_appointment":
                    args = BookingArguments.model_validate(call.function.arguments)
                    result, created = booking.book(tool_call_id=call.id, **args.model_dump())
                    if created is not None:
                        background.add_task(notify_safely, notifier, created)
                else:
                    results.append({"toolCallId": call.id, "error": "unknown_tool"})
                    continue
            except ValidationError:
                results.append({"toolCallId": call.id, "error": "invalid_arguments"})
                continue
            except ReplayConflict:
                results.append({"toolCallId": call.id, "error": "tool_call_id_reused"})
                continue
            results.append(result_for(call.id, result))
        return {"results": results}

    @app.post("/vapi/events", dependencies=protected)
    def events(payload: EventEnvelope) -> dict[str, Any]:
        report = payload.message.model_dump()
        classification = classify_call(report)
        warning = (
            WARNING if classification == "not_booked" and summary_claims_booking(report) else None
        )
        database.store_outcome(payload.message.call.id, classification, warning)
        if warning:
            logger.warning(WARNING)
        return {"classification": classification, "warnings": [warning] if warning else []}

    return app

"""A single calendar with fixed-duration appointments and explicit timezone rules."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config import Settings
from db import Booking, Database, SlotTaken


def local_instants(wall: datetime, zone: ZoneInfo) -> list[datetime]:
    """Round trips reject nonexistent times; folds preserve both repeated times."""
    instants = set()
    for fold in (0, 1):
        instant = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == wall:
            instants.add(instant)
    return sorted(instants)


class BookingService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.zone = ZoneInfo(settings.timezone)

    def slots(self, day: date) -> list[datetime]:
        if day.weekday() not in self.settings.weekdays:
            return []
        opening = datetime.combine(day, self.settings.opens)
        closing = datetime.combine(day, self.settings.closes)
        starts = local_instants(opening, self.zone)
        ends = local_instants(closing, self.zone)
        if not starts or not ends:
            return []  # A nonexistent opening/closing boundary closes this date.
        cursor, end = starts[0], ends[-1]
        duration = timedelta(minutes=self.settings.slot_minutes)
        slots = []
        while cursor + duration <= end:
            # Check the whole interval so a rollback cannot cross closed wall-clock hours.
            within_hours = all(
                opening
                <= (cursor + timedelta(minutes=minute)).astimezone(self.zone).replace(tzinfo=None)
                < closing
                for minute in range(self.settings.slot_minutes)
            )
            if within_hours:
                slots.append(cursor.astimezone(self.zone))
            cursor += duration
        return slots

    def availability(self, day: date) -> dict[str, Any]:
        occupied = {row["start_utc"] for row in self.database.bookings_for_date(day.isoformat())}
        return {
            "date": day.isoformat(),
            "timezone": self.settings.timezone,
            "slots": [
                slot.isoformat()
                for slot in self.slots(day)
                if slot.astimezone(UTC).isoformat() not in occupied
            ],
        }

    def book(
        self,
        *,
        tool_call_id: str,
        start: str,
        name: str,
        phone: str,
        reason: str | None = None,
    ) -> tuple[dict[str, Any], Booking | None]:
        try:
            requested = datetime.fromisoformat(start)
            if requested.tzinfo is None:
                raise ValueError
            local = requested.astimezone(self.zone)
            # Require the business-local wall time AND its real UTC offset.
            if (
                requested.replace(tzinfo=None) != local.replace(tzinfo=None)
                or requested.utcoffset() != local.utcoffset()
            ):
                raise ValueError
            instant = local.astimezone(UTC)
            allowed = {slot.astimezone(UTC) for slot in self.slots(local.date())}
        except (ValueError, OverflowError):
            return {"booked": False, "reason": "invalid_time"}, None
        if instant not in allowed:
            outside = (
                local.weekday() not in self.settings.weekdays
                or not self.settings.opens <= local.time() < self.settings.closes
                or (instant + timedelta(minutes=self.settings.slot_minutes))
                .astimezone(self.zone)
                .replace(tzinfo=None)
                > datetime.combine(local.date(), self.settings.closes)
            )
            return {"booked": False, "reason": "outside_hours" if outside else "invalid_time"}, None
        try:
            row, created = self.database.reserve(
                start_utc=instant.isoformat(),
                start=local.isoformat(),
                local_date=local.date().isoformat(),
                tool_call_id=tool_call_id,
                name=name,
                phone=phone,
                reason=reason,
            )
        except SlotTaken:
            return {"booked": False, "reason": "slot_taken"}, None
        return {"booked": True, "booking_id": row["booking_id"], "start": row["start"]}, (
            row if created else None
        )

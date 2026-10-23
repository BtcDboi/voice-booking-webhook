"""Short transactions, one connection per operation, and database-enforced uniqueness."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

Booking = dict[str, Any]


class SlotTaken(Exception):
    pass


class ReplayConflict(Exception):
    """A tool call ID was reused with different booking arguments."""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS bookings (
                    booking_id INTEGER PRIMARY KEY,
                    start_utc TEXT NOT NULL UNIQUE,
                    start TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS bookings_date ON bookings(local_date);
                CREATE TABLE IF NOT EXISTS outcomes (
                    call_id TEXT PRIMARY KEY,
                    classification TEXT NOT NULL CHECK(classification IN ('booked', 'not_booked')),
                    warning TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def bookings_for_date(self, day: str) -> list[Booking]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bookings WHERE local_date = ? ORDER BY start_utc", (day,)
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve(
        self,
        *,
        start_utc: str,
        start: str,
        local_date: str,
        tool_call_id: str,
        name: str,
        phone: str,
        reason: str | None,
    ) -> tuple[Booking, bool]:
        with self.connect() as connection:
            try:
                cursor = connection.execute(
                    """INSERT INTO bookings
                       (start_utc, start, local_date, tool_call_id, name, phone, reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (start_utc, start, local_date, tool_call_id, name, phone, reason),
                )
            except sqlite3.IntegrityError:
                previous = connection.execute(
                    "SELECT * FROM bookings WHERE tool_call_id = ?", (tool_call_id,)
                ).fetchone()
                if previous is not None:
                    if (
                        previous["start_utc"],
                        previous["name"],
                        previous["phone"],
                        previous["reason"],
                    ) != (start_utc, name, phone, reason):
                        raise ReplayConflict from None
                    return dict(previous), False
                raise SlotTaken from None
            row = connection.execute(
                "SELECT * FROM bookings WHERE booking_id = ?", (cursor.lastrowid,)
            ).fetchone()
            return dict(row), True

    def store_outcome(self, call_id: str, classification: str, warning: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO outcomes (call_id, classification, warning) VALUES (?, ?, ?)
                   ON CONFLICT(call_id) DO UPDATE SET classification=excluded.classification,
                   warning=excluded.warning, updated_at=CURRENT_TIMESTAMP""",
                (call_id, classification, warning),
            )

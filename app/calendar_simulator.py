"""Independent HTTP calendar simulator with durable, versioned idempotent writes."""

import json
import os
import sqlite3
import time
from uuid import UUID

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

load_dotenv()
app = FastAPI(title="Huddle calendar simulator")


def db():
    connection = sqlite3.connect(os.getenv("CALENDAR_DB", "calendar-demo.sqlite"), timeout=10)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, version INTEGER, payload TEXT)"
    )
    connection.execute("CREATE TABLE IF NOT EXISTS faults (id TEXT PRIMARY KEY)")
    return connection


def authorize(authorization):
    if authorization != "Bearer " + os.getenv("CALENDAR_TOKEN", "local-demo-calendar-token"):
        raise HTTPException(401, "Unauthorized")


class CalendarEvent(BaseModel):
    id: UUID
    title: str
    room_id: str
    starts_at: str
    ends_at: str
    attendees: int
    status: str
    version: int = Field(ge=1)


@app.put("/events/{booking_id}")
def upsert_event(booking_id: UUID, event: CalendarEvent, authorization: str = Header()):
    authorize(authorization)
    if event.id != booking_id:
        raise HTTPException(422, "Event identity mismatch")
    connection = db()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT version,payload FROM events WHERE id=?", (str(booking_id),)
            ).fetchone()
            if (
                previous
                and previous[0] == event.version
                and json.loads(previous[1]) != event.model_dump(mode="json")
            ):
                raise HTTPException(409, "Same version with different payload")
            connection.execute(
                """INSERT INTO events VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET
                version=excluded.version,payload=excluded.payload WHERE excluded.version>events.version""",
                (str(booking_id), event.version, event.model_dump_json()),
            )
            result = json.loads(
                connection.execute(
                    "SELECT payload FROM events WHERE id=?", (str(booking_id),)
                ).fetchone()[0]
            )
            fault = connection.execute(
                "DELETE FROM faults WHERE id=? RETURNING id", (str(booking_id),)
            ).fetchone()
        if fault:
            time.sleep(7)  # Commit succeeded; the caller's 5-second timeout expires.
        return result
    finally:
        connection.close()


@app.post("/faults/{booking_id}")
def lose_next_response(booking_id: UUID, authorization: str = Header()):
    authorize(authorization)
    connection = db()
    try:
        with connection:
            connection.execute("INSERT OR IGNORE INTO faults VALUES (?)", (str(booking_id),))
    finally:
        connection.close()
    return {"armed": True}


@app.get("/events/{booking_id}")
def read_event(booking_id: UUID, authorization: str = Header()):
    authorize(authorization)
    connection = db()
    try:
        row = connection.execute(
            "SELECT payload FROM events WHERE id=?", (str(booking_id),)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Event not found")
        return json.loads(row[0])
    finally:
        connection.close()

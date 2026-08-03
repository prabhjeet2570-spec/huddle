from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.bookings import event, owned
from app.db import connect
from app.session import current_user

router = APIRouter(tags=["reliability"])


@router.get("/metrics")
def metrics(user=Depends(current_user)):
    with connect() as conn:
        totals = conn.execute(
            """SELECT count(*) FILTER (WHERE status='confirmed') AS confirmed,
            count(*) FILTER (WHERE status='cancelled') AS cancelled,
            count(*) FILTER (WHERE calendar_status='pending') AS pending,
            count(*) FILTER (WHERE calendar_status='needs_review') AS needs_review,
            count(*) FILTER (WHERE calendar_status='synced') AS synced FROM bookings WHERE owner_id=%s""",
            (user["id"],),
        ).fetchone()
        jobs = conn.execute(
            """SELECT count(*) AS jobs,count(*) FILTER(WHERE o.attempts>1) AS retried,
            percentile_cont(.95) WITHIN GROUP (ORDER BY extract(epoch FROM o.completed_at-o.created_at))
                FILTER(WHERE o.state='done') AS sync_p95_seconds,
            max(extract(epoch FROM now()-o.created_at)) FILTER(WHERE o.state IN ('pending','processing','needs_review')) AS oldest_pending_seconds
            FROM outbox o JOIN bookings b ON b.id=o.booking_id WHERE b.owner_id=%s""",
            (user["id"],),
        ).fetchone()
        heartbeat = conn.execute(
            "SELECT seen_at,seen_at>now()-interval '15 seconds' AS online FROM worker_heartbeats WHERE name='calendar'"
        ).fetchone()
        history = conn.execute(
            """SELECT e.kind,e.detail,e.created_at,b.title,b.room_id,b.id AS booking_id
            FROM booking_events e JOIN bookings b ON b.id=e.booking_id WHERE b.owner_id=%s ORDER BY e.id DESC LIMIT 30""",
            (user["id"],),
        ).fetchall()
    return {
        **totals,
        **jobs,
        "worker": heartbeat,
        "history": history,
        "scope": "Current workspace session; calendar simulator",
        "latency_definition": "p95 seconds from outbox enqueue to successful reconciliation, including retries; completed jobs only",
    }


@router.post("/bookings/{booking_id}/retry-sync")
def retry_sync(booking_id: UUID, user=Depends(current_user)):
    with connect() as conn:
        row = owned(conn, booking_id, user["id"])
        if row["calendar_status"] != "needs_review":
            raise HTTPException(409, "Only work requiring review can be retried manually")
        conn.execute(
            "UPDATE outbox SET state='pending',attempts=0,available_at=now(),lease_token=NULL WHERE booking_id=%s AND version=%s AND state='needs_review'",
            (booking_id, row["version"]),
        )
        conn.execute("UPDATE bookings SET calendar_status='pending' WHERE id=%s", (booking_id,))
        event(
            conn,
            booking_id,
            "manual_retry",
            "Calendar reconciliation requeued by the booking owner.",
        )
    return {"queued": True}

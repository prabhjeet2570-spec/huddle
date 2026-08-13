import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Cookie, HTTPException, Response
from pydantic import BaseModel, Field

from app.db import connect

router = APIRouter(tags=["session"])


class SessionInput(BaseModel):
    name: str = Field(default="Morgan", min_length=1, max_length=40)


def current_user(huddle_session: str | None = Cookie(default=None)):
    if not huddle_session:
        raise HTTPException(401, "Open a workspace first")
    with connect() as conn:
        user = conn.execute(
            "SELECT id,name FROM sessions WHERE token_hash=%s AND expires_at>now()",
            (hashlib.sha256(huddle_session.encode()).hexdigest(),),
        ).fetchone()
    if not user:
        raise HTTPException(401, "Workspace session expired")
    return user


@router.post("/session")
def start_session(body: SessionInput, response: Response):
    token = secrets.token_urlsafe(32)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Enter a display name")
    user_id = uuid4()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions VALUES (%s,%s,%s,%s)",
            (
                user_id,
                hashlib.sha256(token.encode()).hexdigest(),
                name,
                datetime.now(UTC) + timedelta(days=7),
            ),
        )
    response.set_cookie(
        "huddle_session",
        token,
        httponly=True,
        samesite="strict",
        max_age=604800,
        secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
    )
    return {"id": str(user_id), "name": name}


@router.delete("/session")
def end_session(response: Response, huddle_session: str | None = Cookie(default=None)):
    if huddle_session:
        with connect() as conn:
            conn.execute(
                "UPDATE sessions SET expires_at=now() WHERE token_hash=%s",
                (hashlib.sha256(huddle_session.encode()).hexdigest(),),
            )
    response.delete_cookie("huddle_session")
    return {"ok": True}


DEMO_NAMES = ("Blake", "Morgan", "Jake", "Ashley")


@router.get("/demo/profiles")
def demo_profiles():
    return {"names": DEMO_NAMES if os.getenv("HUDDLE_DEMO_PROFILES") == "true" else []}


@router.post("/demo/profiles/{name}")
def switch_demo_profile(name: str, response: Response):
    """Explicit local-only sample identities; this is not account authentication."""
    from uuid import NAMESPACE_URL, uuid5

    if os.getenv("HUDDLE_DEMO_PROFILES") != "true" or name not in DEMO_NAMES:
        raise HTTPException(404, "Demo profile unavailable")
    identity = uuid5(NAMESPACE_URL, "huddle-local-demo/" + name)
    token = secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute(
            """INSERT INTO sessions VALUES (%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash,expires_at=excluded.expires_at""",
            (
                identity,
                hashlib.sha256(token.encode()).hexdigest(),
                name,
                datetime.now(UTC) + timedelta(days=7),
            ),
        )
    response.set_cookie(
        "huddle_session",
        token,
        httponly=True,
        samesite="strict",
        max_age=604800,
        secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
    )
    return {"id": str(identity), "name": name}

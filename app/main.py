"""Huddle's HTTP application."""

from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.assistant import router as assistant_router
from app.availability import router as availability_router
from app.bookings import router as bookings_router
from app.db import connect
from app.demo import router as demo_router
from app.operations import router as operations_router
from app.rooms import router as rooms_router
from app.session import current_user
from app.session import router as session_router

app = FastAPI(title="Huddle", version="0.1.0")
app.include_router(assistant_router)
app.include_router(demo_router)
app.include_router(operations_router)
app.include_router(session_router)
app.include_router(rooms_router)
app.include_router(bookings_router)
app.include_router(availability_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the application can serve requests."""
    return {"status": "ok"}


@app.get("/session")
def session(user=Depends(current_user)):
    return user


static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(static_dir / "index.html")


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        if origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/ready")
def ready():
    try:
        with connect() as conn:
            conn.execute("SELECT id FROM outbox LIMIT 1")
        return {"status": "ready"}
    except psycopg.Error:
        return JSONResponse({"status": "unavailable"}, status_code=503)

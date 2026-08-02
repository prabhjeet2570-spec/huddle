"""Huddle's HTTP application."""

from fastapi import Depends, FastAPI
from app.session import current_user, router as session_router

from app.availability import router as availability_router
from app.bookings import router as bookings_router
from app.rooms import router as rooms_router

app = FastAPI(title="Huddle", version="0.1.0")
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

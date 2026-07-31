"""Huddle's HTTP application."""

from fastapi import FastAPI

from app.availability import router as availability_router
from app.bookings import router as bookings_router
from app.rooms import router as rooms_router

app = FastAPI(title="Huddle", version="0.1.0")
app.include_router(rooms_router)
app.include_router(bookings_router)
app.include_router(availability_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the application can serve requests."""
    return {"status": "ok"}

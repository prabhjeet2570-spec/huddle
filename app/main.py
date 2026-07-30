"""Huddle's HTTP application."""

from fastapi import FastAPI

from app.rooms import router as rooms_router

app = FastAPI(title="Huddle", version="0.1.0")
app.include_router(rooms_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the application can serve requests."""
    return {"status": "ok"}

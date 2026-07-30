"""Huddle's HTTP application."""

from fastapi import FastAPI

app = FastAPI(title="Huddle", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the application can serve requests."""
    return {"status": "ok"}

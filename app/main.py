"""Application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes.auth import router as auth_router
from app.api.routes.bookings import router as bookings_router
from app.config import settings
from app.domain.exceptions import DomainError, GuardrailError
from app.infrastructure.database import dispose_engine
from app.infrastructure.seed import seed
from app.observability.tracing import configure_tracing
from app.services.hold_expiry import sweeper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_tracing()
    if settings.seed_on_startup:
        await seed()

    # The sweeper is what makes hold TTLs visible in availability queries.
    await sweeper.start()
    try:
        yield
    finally:
        await sweeper.stop()
        await dispose_engine()


app = FastAPI(
    title="Huddle",
    version="1.0.0",
    description=(
        "A conversational meeting-room booking agent with the reliability "
        "engineering that makes autonomous writes to contended state safe."
    ),
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(bookings_router)


@app.exception_handler(GuardrailError)
async def guardrail_handler(_request: Request, error: GuardrailError) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": str(error)})


@app.exception_handler(DomainError)
async def domain_handler(_request: Request, error: DomainError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(error)})

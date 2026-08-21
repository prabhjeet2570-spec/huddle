"""Application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes.auth import router as auth_router
from app.api.routes.bookings import router as bookings_router
from app.api.routes.chat import router as chat_router
from app.api.routes.ops import router as ops_router
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

    if settings.llm_provider != "fake" and not settings.llm_api_key.get_secret_value():
        # Said once, loudly, at boot rather than only on the first chat request.
        logger.warning(
            "No HUDDLE_LLM_API_KEY set: /chat is disabled. The REST API, "
            "booking rules and metrics all work without one."
        )
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
    description="An LLM agent that books meeting rooms over an HTTP API.",
    lifespan=lifespan,
)

app.include_router(ops_router)
app.include_router(auth_router)
app.include_router(bookings_router)
app.include_router(chat_router)


@app.exception_handler(GuardrailError)
async def guardrail_handler(_request: Request, error: GuardrailError) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": str(error)})


@app.exception_handler(DomainError)
async def domain_handler(_request: Request, error: DomainError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(error)})


if settings.otel_enabled:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        logger.exception("FastAPI OTel instrumentation unavailable")

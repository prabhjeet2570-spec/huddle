FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY ui ./ui
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh \
    && useradd --create-home --uid 1000 huddle \
    && chown -R huddle:huddle /app
USER huddle

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

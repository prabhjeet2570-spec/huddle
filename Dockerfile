FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.9
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY migrations ./migrations
RUN useradd --create-home huddle && mkdir /data && chown huddle:huddle /data
USER huddle
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8010
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]

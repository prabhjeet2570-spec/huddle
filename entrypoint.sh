#!/usr/bin/env bash
# Migrate before serving. The schema carries the exclusion constraint the
# application's correctness depends on, so starting without it is never right.
set -euo pipefail

if [[ "${1:-}" == "uvicorn" ]]; then
  echo "waiting for the database..."
  for _ in $(seq 1 60); do
    if python -c "
import sys, psycopg
from app.config import settings
try:
    psycopg.connect(settings.sync_database_url.replace('postgresql+psycopg://','postgresql://'), connect_timeout=2).close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  echo "applying migrations..."
  alembic upgrade head
fi

exec "$@"

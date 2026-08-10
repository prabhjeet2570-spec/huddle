import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()


def connect(*, autocommit=False):
    return psycopg.connect(
        os.getenv("DATABASE_URL", "postgresql://huddle:huddle@localhost:5438/huddle"),
        row_factory=dict_row,
        connect_timeout=5,
        autocommit=autocommit,
    )


def migrate():
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(481920)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY)")
        for path in sorted((Path(__file__).parent.parent / "migrations").glob("*.sql")):
            if not conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name=%s", (path.name,)
            ).fetchone():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations VALUES (%s)", (path.name,))

    # The adapter owns its schema and uses concurrent indexes, requiring autocommit.
    from langgraph.checkpoint.postgres import PostgresSaver

    with connect(autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(481921)")
        try:
            PostgresSaver(conn).setup()
        finally:
            conn.execute("SELECT pg_advisory_unlock(481921)")


if __name__ == "__main__":
    migrate()

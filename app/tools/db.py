"""Database helpers for hide-my-list.

Provides connection management and migration runner.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import structlog

log = structlog.get_logger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


def get_connection_string() -> str:
    return os.environ["DATABASE_URL"]


def run_migrations() -> None:
    """Run all SQL migration files in order against the configured database.

    Called at app startup before any other DB operations.
    Files are applied in filename order; each is idempotent (IF NOT EXISTS).
    """
    conn_str = get_connection_string()
    migration_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))

    if not migration_files:
        log.info("db.migrations.none_found")
        return

    with psycopg.connect(conn_str) as conn:
        for mig_file in migration_files:
            log.info("db.migration.applying", file=mig_file.name)
            sql = mig_file.read_text(encoding="utf-8")
            conn.execute(sql)
            conn.commit()
            log.info("db.migration.done", file=mig_file.name)

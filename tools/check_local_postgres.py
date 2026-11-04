"""Fail closed if a local PostgreSQL cutover lost existing SQLite tasks."""

from __future__ import annotations

import os
import json
import sqlite3
import sys
from pathlib import Path

from redis import Redis
from sqlalchemy import create_engine, func, select, text

from excel_auditor.persistence import UserRow
from excel_auditor.workflow.repository import objects


def main(root: Path) -> None:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            actual = [connection.scalar(select(func.count()).select_from(UserRow.__table__)),
                      connection.scalar(select(func.count()).select_from(objects))]
            identity = {
                "system_identifier": str(connection.scalar(text("select system_identifier from pg_control_system()"))),
                "database_oid": int(connection.scalar(text("select oid from pg_database where datname=current_database()"))),
            }
        marker = root / "local-postgres-state.json"
        if marker.is_file():
            saved = json.loads(marker.read_text(encoding="utf-8"))
            if saved != identity:
                raise RuntimeError("PostgreSQL data volume differs from the migrated local database")
        else:
            # First local cutover only: verify that both old SQLite stores were
            # transferred before pinning this particular PostgreSQL volume.
            sources = [(root / "auth.sqlite3", "auth_users"),
                       (root / "workflow" / "workflow.sqlite3", "workflow_objects")]
            previous = []
            for path, table in sources:
                with sqlite3.connect(path) as connection:
                    previous.append(connection.execute(f"select count(*) from {table}").fetchone()[0])
            if any(now < old for now, old in zip(actual, previous)):
                raise RuntimeError("PostgreSQL is missing migrated local accounts or tasks")
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(json.dumps(identity), encoding="utf-8")
            temporary.replace(marker)
        if actual[0] < 1:
            raise RuntimeError("PostgreSQL has no local login account")
        if not Redis.from_url(os.environ["REDIS_URL"]).ping():
            raise RuntimeError("Redis is unavailable")
        print("PostgreSQL, Redis, and migrated data verified")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main(Path(sys.argv[1]))

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text


def test_existing_sqlite_comparison_table_gets_task_metadata_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE comparison_jobs (id VARCHAR(64) PRIMARY KEY)"))

    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{path}", create_schema=True)
    columns = {column["name"] for column in inspect(database.engine).get_columns("comparison_jobs")}
    assert {"queue_name", "attempt", "worker_id", "lease_expires_at", "idempotency_key"} <= columns

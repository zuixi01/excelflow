"""One-time, fail-closed migration of the local auth and workflow SQLite stores.

Stop the local API and workflow worker first. The source databases are backed up
and kept in place; the target must already have an empty Alembic schema.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, func, insert, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import Connection


def backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as reader, sqlite3.connect(destination) as writer:
        reader.backup(writer)


def source_tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def copy_store(source_path: Path, writer: Connection, target_meta: MetaData) -> dict[str, int]:
    source = create_engine(f"sqlite:///{source_path.as_posix()}")
    try:
        names = source_tables(source)
        missing = names - set(target_meta.tables)
        if missing:
            raise RuntimeError(f"PostgreSQL schema is missing tables: {', '.join(sorted(missing))}")
        result: dict[str, int] = {}
        with source.connect() as reader:
            for target_table in target_meta.sorted_tables:
                name = target_table.name
                if name not in names:
                    continue
                source_table = Table(name, MetaData(), autoload_with=source)
                source_columns = set(source_table.c.keys())
                target_columns = set(target_table.c.keys())
                if not source_columns <= target_columns:
                    raise RuntimeError(f"PostgreSQL table {name} is missing columns: {source_columns - target_columns}")
                if writer.scalar(select(func.count()).select_from(target_table)):
                    raise RuntimeError(f"PostgreSQL table {name} is not empty; migration will not overwrite it")
                batch = []
                count = 0
                for row in reader.execute(select(source_table)).mappings():
                    batch.append(dict(row))
                    if len(batch) == 100:
                        writer.execute(insert(target_table), batch)
                        count += len(batch)
                        batch.clear()
                if batch:
                    writer.execute(insert(target_table), batch)
                    count += len(batch)
                if writer.scalar(select(func.count()).select_from(target_table)) != count:
                    raise RuntimeError(f"PostgreSQL row count differs for {name}")
                result[name] = count
        return result
    finally:
        source.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("var"))
    args = parser.parse_args()
    root = args.data_root.resolve()
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql+"):
        parser.error("DATABASE_URL must point to the intended PostgreSQL database")
    sources = [root / "auth.sqlite3", root / "workflow" / "workflow.sqlite3"]
    if any(not path.is_file() for path in sources):
        parser.error("both local SQLite databases must exist")
    target = create_engine(url, pool_pre_ping=True)
    try:
        meta = MetaData()
        meta.reflect(bind=target)
        all_names = set()
        for path in sources:
            source = create_engine(f"sqlite:///{path.as_posix()}")
            try:
                all_names.update(source_tables(source))
            finally:
                source.dispose()
        if not all_names <= set(meta.tables):
            raise RuntimeError("PostgreSQL schema is incomplete; run Alembic upgrade first")
        with target.connect() as connection:
            occupied = [name for name in all_names if connection.scalar(select(func.count()).select_from(meta.tables[name]))]
        if occupied:
            raise RuntimeError("PostgreSQL already has business data; refusing migration: " + ", ".join(sorted(occupied)))
        backup_dir = root / "migration-backup" / datetime.now().strftime("%Y%m%d-%H%M%S")
        for path in sources:
            backup_sqlite(path, backup_dir / path.name)
        print(f"SQLite backups saved to {backup_dir}")
        copied: dict[str, int] = {}
        with target.begin() as writer:
            for path in sources:
                copied.update(copy_store(backup_dir / path.name, writer, meta))
            # SQLAlchemy inserts explicit IDs. Advance serial sequences, if any,
            # before a new record is created by the application.
            for name in copied:
                table = meta.tables[name]
                for column in table.primary_key.columns:
                    sequence = writer.scalar(text("SELECT pg_get_serial_sequence(:table, :column)"),
                                             {"table": name, "column": column.name})
                    if sequence:
                        quoted_table = writer.dialect.identifier_preparer.quote(name)
                        quoted_column = writer.dialect.identifier_preparer.quote(column.name)
                        maximum = writer.scalar(text(f"SELECT max({quoted_column}) FROM {quoted_table}"))
                        if maximum is not None:
                            writer.execute(text("SELECT setval(:sequence, :value, true)"),
                                           {"sequence": sequence, "value": maximum})
        print("Migrated tables and row counts:")
        for name, count in sorted(copied.items()):
            print(f"  {name}: {count}")
        print("Migration complete. Original SQLite databases remain unchanged.")
    finally:
        target.dispose()


if __name__ == "__main__":
    main()

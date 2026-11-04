import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url

from excel_auditor.workflow.repository import WorkflowStore, executions, objects
from excel_auditor.workflow.tasks import TaskService


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'migration.sqlite3'}"
        return
    database_url = os.environ.get("EXCELFLOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set isolated EXCELFLOW_TEST_DATABASE_URL for PostgreSQL migration rehearsal")
    schema = "migration_" + uuid4().hex
    admin = create_engine(database_url)
    with admin.begin() as con:
        con.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield make_url(database_url).update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    finally:
        with admin.begin() as con:
            con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_upgrade_backfills_legacy_creators_and_unicode_search_and_downgrades(tmp_path, monkeypatch, migration_url):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = migration_url
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "20261016_0008")
    engine = create_engine(url)
    with engine.begin() as con:
        con.execute(text("INSERT INTO workflow_objects (id,tenant_id,kind,status,revision,payload,created_at,updated_at) VALUES (:id,'local','task','ready',0,:payload,'2026','2026')"),
                    {"id": "legacy_task", "payload": '{"actor":"alice","name":"Straße","filename":"sample.xlsx"}'})
        con.execute(text("INSERT INTO workflow_objects (id,tenant_id,kind,status,revision,payload,created_at,updated_at) VALUES ('legacy_export','local','export','completed',0,:payload,'2026','2026')"),
                    {"payload": '{"task_id":"legacy_task"}'})
    command.upgrade(config, "head")
    store = WorkflowStore(tmp_path, engine)
    result = TaskService(store).list("local", "STRASSE", creator_id="alice")
    assert result["total"] == 1 and result["items"][0]["id"] == "legacy_task"
    assert TaskService(store).get("legacy_task", "local")["exports"][0]["id"] == "legacy_export"
    with engine.connect() as con:
        assert con.execute(select(executions)).first() is None
        assert con.execute(select(objects.c.parent_task_id).where(objects.c.id == "legacy_export")).scalar_one() == "legacy_task"
    command.downgrade(config, "20261016_0008")
    with engine.connect() as con:
        assert con.execute(text("SELECT id FROM workflow_objects WHERE kind='task'")).scalar_one() == "legacy_task"
    engine.dispose()


def test_local_database_upgrades_existing_file_without_rewriting_tasks(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'workflow.sqlite3'}")
    with engine.begin() as con:
        con.execute(text("CREATE TABLE workflow_objects (id VARCHAR(64) PRIMARY KEY,tenant_id VARCHAR(200),kind VARCHAR(32),status VARCHAR(32),revision INTEGER,payload JSON,created_at VARCHAR(40),updated_at VARCHAR(40))"))
        con.execute(text("INSERT INTO workflow_objects VALUES ('old','local','task','ready',7,'{\"actor\":\"alice\",\"name\":\"Straße\"}','2026','2026')"))
    engine.dispose()
    store = WorkflowStore(tmp_path)
    result = TaskService(store).list("local", "STRASSE", creator_id="alice")
    assert result["total"] == 1 and result["items"][0]["revision"] == 7
    store.engine.dispose()


def test_sql_pagination_has_stable_ties_and_filters_before_count(tmp_path):
    store = WorkflowStore(tmp_path)
    with store.engine.begin() as con:
        for index in range(75):
            store.create(con, "local", "task", {"actor": "alice" if index % 2 else "bob", "name": f"A_{index}", "filename": "input.xlsx"}, "ready", f"task_{index:03d}")
        con.execute(objects.update().values(updated_at="2026"))
    statements = []
    event.listen(store.engine, "before_cursor_execute", lambda con, cur, statement, *args: statements.append(statement))
    service = TaskService(store)
    pages = [service.list("local", offset=offset, limit=10, creator_id="alice") for offset in range(0, 40, 10)]
    ids = [item["id"] for page in pages for item in page["items"]]
    assert len(ids) == len(set(ids)) == 37
    assert ids == sorted(ids, reverse=True)
    assert all(page["total"] == 37 for page in pages)
    assert any("LIMIT" in statement and "OFFSET" in statement for statement in statements)
    assert service.list("local", "A_1", creator_id="alice")["total"] == 6
    store.engine.dispose()


def test_local_upgrade_backfills_parent_links_when_summary_columns_exist(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'workflow.sqlite3'}")
    with engine.begin() as con:
        con.execute(text("CREATE TABLE workflow_objects (id VARCHAR(64) PRIMARY KEY,tenant_id VARCHAR(200),kind VARCHAR(32),status VARCHAR(32),revision INTEGER,payload JSON,created_at VARCHAR(40),updated_at VARCHAR(40),creator_id VARCHAR(200),search_summary TEXT)"))
        con.execute(text("INSERT INTO workflow_objects VALUES ('old_export','local','export','completed',0,'{\"task_id\":\"old_task\"}','2026','2026',NULL,'')"))
    engine.dispose()
    store = WorkflowStore(tmp_path)
    with store.engine.connect() as con:
        assert con.execute(select(objects.c.parent_task_id)).scalar_one() == "old_task"
    store.engine.dispose()

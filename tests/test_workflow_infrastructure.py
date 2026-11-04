"""Opt-in PostgreSQL/Redis/RQ recovery tests, each in its own database schema."""
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url

from excel_auditor.queueing import RedisJobQueue
from excel_auditor.workflow.execution import ExecutionService, run_execution
from excel_auditor.workflow.models import WorkflowError, default_config
from excel_auditor.workflow.repository import WorkflowStore, executions, metadata
from excel_auditor.workflow.tasks import TaskService


def crash_execution(root, execution_id):
    def crash(*args):
        os._exit(42)
    TaskService.parse = crash
    run_execution(root, execution_id)


@pytest.fixture
def infrastructure(tmp_path, monkeypatch):
    database_url = os.environ.get("EXCELFLOW_TEST_DATABASE_URL")
    redis_url = os.environ.get("EXCELFLOW_TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("set isolated EXCELFLOW_TEST_DATABASE_URL and EXCELFLOW_TEST_REDIS_URL")
    schema = "wf_test_" + uuid4().hex
    admin = create_engine(database_url)
    with admin.begin() as con:
        con.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(database_url).update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    engine = create_engine(url)
    metadata.create_all(engine)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(Path("src").resolve()), str(Path("tests").resolve())]))
    store = WorkflowStore(tmp_path, engine)
    queue = RedisJobQueue(redis_url, queue_name="workflow")
    try:
        yield store, queue
    finally:
        engine.dispose()
        with admin.begin() as con:
            con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def create_task(store):
    book = Workbook()
    book.active.append(["商品名称", "货号", "销售价", "库存"])
    book.active.append(["合成商品", "0001", "10.00", "3"])
    output = BytesIO()
    book.save(output)
    with store.engine.begin() as con:
        obj = store.create(con, "local", "task", {"actor": "alice", "config": default_config().model_dump(mode="json")}, "processing")
        execution = dict(con.execute(select(executions).where(executions.c.object_id == obj["id"])).mappings().one())
    (store.directory(obj["id"]) / "source.xlsx").write_bytes(output.getvalue())
    return obj, execution


def burst_worker():
    return subprocess.run([sys.executable, "-c", "from rq.cli import main; main()", "worker", "-w", "rq.worker.SimpleWorker",
        "--url", os.environ["REDIS_URL"], "--name", "fault_" + uuid4().hex, "--burst", "workflow"],
        capture_output=True, timeout=60)


@pytest.mark.integration
def test_real_rq_worker_death_recovery_and_stale_fencing(infrastructure):
    store, queue = infrastructure
    obj, execution = create_task(store)
    queue._queue("workflow").enqueue(crash_execution, str(store.root), execution["id"], job_id="crash_" + execution["id"])
    result = burst_worker()
    assert result.returncode == 42, result.stdout + result.stderr
    with store.engine.connect() as con:
        lost = dict(con.execute(select(executions).where(executions.c.id == execution["id"])).mappings().one())
    assert lost["status"] == "running" and lost["attempts"] == 1
    with store.engine.begin() as con:
        con.execute(update(executions).where(executions.c.id == execution["id"]).values(lease_until="2000-01-01T00:00:00+00:00"))
    service = ExecutionService(store)
    assert service.recover() == 1
    store.execution = (lost["id"], lost["token"])
    with pytest.raises(WorkflowError, match="租约"), store.engine.begin() as con:
        store.save(con, obj)
    store.execution = None
    service.dispatch(queue)
    result = burst_worker()
    assert result.returncode == 0, result.stdout + result.stderr
    with store.engine.connect() as con:
        assert store.get(con, obj["id"], "local")["status"] == "mapping"
        current = con.execute(select(executions).where(executions.c.id == execution["id"])).mappings().one()
        assert current["status"] == "succeeded" and current["attempts"] == 2
    assert len(list(store.directory(obj["id"]).glob("source-data-*.json"))) == 1


@pytest.mark.integration
def test_actual_redis_connection_failure_and_dispatcher_restart(infrastructure):
    from redis import Redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    store, queue = infrastructure
    obj, execution = create_task(store)
    class Offline:
        def __init__(self):
            self.connection = Redis(host="127.0.0.1", port=1, socket_connect_timeout=.1, retry=Retry(NoBackoff(), 0))
        def enqueue_workflow(self, *args, **kwargs):
            self.connection.ping()
    ExecutionService(store).dispatch(Offline())
    with store.engine.connect() as con:
        assert store.get(con, obj["id"], "local")["status"] == "processing"
    restarted = ExecutionService(WorkflowStore(store.root, store.engine), clock=lambda: datetime.now(UTC) + timedelta(seconds=31))
    restarted.dispatch(queue)
    # Repeated scans and deliveries cannot publish a second result.
    restarted.dispatch(queue)
    result = burst_worker()
    assert result.returncode == 0, result.stdout + result.stderr
    run_execution(str(store.root), execution["id"])
    with store.engine.connect() as con:
        current = con.execute(select(executions).where(executions.c.id == execution["id"])).mappings().one()
        assert current["status"] == "succeeded" and current["attempts"] == 1
    assert len(list(store.directory(obj["id"]).glob("source-data-*.json"))) == 1

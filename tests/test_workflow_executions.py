from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from excel_auditor.workflow.repository import WorkflowStore, executions
from excel_auditor.workflow.models import WorkflowError


def setup_execution(tmp_path):
    store = WorkflowStore(tmp_path)
    with store.engine.begin() as con:
        obj = store.create(con, "local", "task", {"actor": "alice"}, "processing")
        execution = dict(con.execute(select(executions)).mappings().one())
    return store, obj, execution


def test_business_record_and_outbox_rollback_together(tmp_path):
    store = WorkflowStore(tmp_path)
    with pytest.raises(RuntimeError), store.engine.begin() as con:
        store.create(con, "local", "task", {}, "processing")
        raise RuntimeError("rollback")
    with store.engine.connect() as con:
        assert con.execute(select(executions)).first() is None


def test_duplicate_delivery_has_only_one_owner(tmp_path):
    from excel_auditor.workflow.execution import ExecutionService
    store, obj, execution = setup_execution(tmp_path)
    service = ExecutionService(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: service.claim(execution["id"], "test"), range(8)))
    assert sum(item is not None for item in claims) == 1


def test_lease_recovery_fences_previous_worker_and_bounds_retries(tmp_path):
    from excel_auditor.workflow.execution import ExecutionService
    store, obj, execution = setup_execution(tmp_path)
    current = datetime.now(UTC)
    service = ExecutionService(store, clock=lambda: current, lease_seconds=2)
    first = service.claim(execution["id"], "lost")
    store.execution = (first["id"], first["token"])
    for attempt in range(1, 4):
        current += timedelta(seconds=3)
        service.recover()
        if attempt == 1:
            with pytest.raises(WorkflowError, match="租约"), store.engine.begin() as con:
                store.save(con, obj)
        if attempt < 3:
            assert service.claim(execution["id"], "replacement")["attempts"] == attempt + 1
    with store.engine.connect() as con:
        row = con.execute(select(executions)).mappings().one()
        assert row["status"] == "failed"
        assert store.get(con, obj["id"], "local")["status"] == "failed"


def test_healthy_running_and_queued_work_is_not_recovered(tmp_path):
    from excel_auditor.workflow.execution import ExecutionService
    store, obj, execution = setup_execution(tmp_path)
    service = ExecutionService(store)
    assert service.claim(execution["id"], "healthy")
    assert service.recover() == 0


def test_queue_outage_preserves_outbox_for_replay(tmp_path):
    from excel_auditor.workflow.execution import ExecutionService
    store, obj, execution = setup_execution(tmp_path)
    instant = datetime.now(UTC)
    service = ExecutionService(store, clock=lambda: instant)
    class Offline:
        def enqueue_workflow(self, *args, **kwargs):
            raise ConnectionError("offline")
    service.dispatch(Offline())
    with store.engine.connect() as con:
        assert store.get(con, obj["id"], "local")["status"] == "processing"
        assert con.execute(select(executions.c.status)).scalar_one() == "queued"
    instant += timedelta(seconds=31)
    assert service.dispatch() == [execution["id"]]


@pytest.mark.parametrize("status,attempts", [(422, 1), (429, 3), (503, 3)])
def test_invalid_input_is_terminal_and_transient_errors_have_retry_limit(tmp_path, monkeypatch, status, attempts):
    from excel_auditor.workflow.execution import execute
    from excel_auditor.workflow.tasks import TaskService
    store, obj, execution = setup_execution(tmp_path)
    def failed(*args):
        raise WorkflowError("test failure", status)
    monkeypatch.setattr(TaskService, "parse", failed)
    for _ in range(attempts):
        with store.engine.begin() as con:
            con.execute(update(executions).values(available_at="2000-01-01T00:00:00+00:00"))
        execute(store, execution["id"])
    with store.engine.connect() as con:
        row = con.execute(select(executions)).mappings().one()
        assert row["status"] == "failed" and row["attempts"] == attempts
        assert store.get(con, obj["id"], "local")["status"] == "failed"


def test_process_death_recovers_without_a_finalizer(tmp_path):
    import os
    import subprocess
    import sys
    from excel_auditor.workflow.execution import ExecutionService
    store, obj, execution = setup_execution(tmp_path)
    code = """
import os, sys
from pathlib import Path
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.execution import ExecutionService
store = WorkflowStore(Path(sys.argv[1]))
assert ExecutionService(store, lease_seconds=1).claim(sys.argv[2], 'crashed-worker')
os._exit(42)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), execution["id"]],
                            env={**os.environ, "PYTHONPATH": str(__import__("pathlib").Path("src").resolve())}, timeout=30)
    assert result.returncode == 42
    service = ExecutionService(store, clock=lambda: datetime.now(UTC) + timedelta(seconds=2))
    assert service.recover() == 1
    assert service.claim(execution["id"], "replacement")["attempts"] == 2


def test_healthy_queued_work_does_not_starve_pending_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv('EXCEL_AUDITOR_WORKFLOW_MAX_WAITING', '1000')
    monkeypatch.setenv('EXCEL_AUDITOR_WORKFLOW_USER_WAITING', '1000')
    from excel_auditor.workflow.execution import ExecutionService
    store, obj, execution = setup_execution(tmp_path)
    with store.engine.begin() as con:
        for _ in range(110):
            store.create(con, "local", "task", {}, "processing")
    class HealthyQueue:
        def __init__(self):
            self.ids = []
        def workflow_job_active(self, job_id):
            return any(job_id.startswith(item) for item in self.ids)
        def enqueue_workflow(self, *args, execution_id, **kwargs):
            self.ids.append(execution_id)
    queue = HealthyQueue()
    service = ExecutionService(store)
    service.dispatch(queue)
    service.dispatch(queue)
    assert len(queue.ids) == len(set(queue.ids)) == 111


def test_deletion_fences_exports_and_prevents_reusing_retired_request_identity(tmp_path):
    from excel_auditor.workflow.execution import ExecutionService
    from excel_auditor.workflow.repository import objects
    from excel_auditor.workflow.tasks import TaskService

    store = WorkflowStore(tmp_path)
    with store.engine.begin() as con:
        task = store.create(con, "local", "task", {}, "processing", "task_stable_request")
        task["status"] = "ready"
        store.save(con, task)
        export = store.create(con, "local", "export", {"task_id": task["id"]}, "processing")
        execution_id = con.execute(select(executions.c.id).where(executions.c.object_id == export["id"])).scalar_one()
    held = ExecutionService(store).claim(execution_id, "old-worker")
    TaskService(store).delete("local", [task["id"]])
    store.execution = (held["id"], held["token"])
    with pytest.raises(WorkflowError, match="租约"), store.engine.begin() as con:
        store.save(con, export)
    store.execution = None
    with pytest.raises(WorkflowError, match="请求标识"), store.engine.begin() as con:
        store.create(con, "local", "task", {}, "processing", "task_stable_request")
    with store.engine.connect() as con:
        assert con.execute(select(objects)).first() is None
        assert set(con.execute(select(executions.c.status)).scalars()) == {"superseded"}


def test_slow_queue_publish_does_not_block_http_event_loop(tmp_path):
    import asyncio
    import threading
    from io import BytesIO

    import httpx
    from fastapi import FastAPI, Request
    from openpyxl import Workbook

    from excel_auditor.workflow.field_profiles import FieldProfile, FieldProfileService, ProfileEdit
    from excel_auditor.workflow.routes import build_router

    store = WorkflowStore(tmp_path / "workflow")
    profiles = FieldProfileService(store)
    previous = profiles.get("local")
    changed = profiles.save("local", ProfileEdit(base_revision=previous["revision"],
        profile=FieldProfile(name="Test", fixed_fields=[{"id": "name", "title": "Name"}])))
    profiles.publish("local", changed["revision"])
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    class Queue:
        def enqueue_workflow(self, *args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            returned.set()

    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.tenant_id = "local"
        request.state.user_id = "alice"
        request.state.role = "owner"
        request.state.authenticated = True
        return await call_next(request)

    @app.get("/health/live")
    async def health():
        return {"status": "ok"}

    app.include_router(build_router(tmp_path, task_queue=Queue()))
    book = Workbook()
    book.active.append(["Name"])
    book.active.append(["Item"])
    content = BytesIO()
    book.save(content)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            request = asyncio.create_task(client.post("/api/v1/standardization-tasks", data={"name": "Upload"},
                                                      files={"excel_file": ("input.xlsx", content.getvalue())}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert (await asyncio.wait_for(client.get("/health/live"), timeout=2)).status_code == 200
                assert not returned.is_set()
            finally:
                release.set()
                (await request).raise_for_status()

    asyncio.run(exercise())


def test_default_api_only_queues_work_until_worker_runs(tmp_path):
    from io import BytesIO
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from openpyxl import Workbook

    from excel_auditor.workflow.field_profiles import FieldProfile, FieldProfileService, ProfileEdit
    from excel_auditor.workflow.execution import run_execution
    from excel_auditor.workflow.routes import build_router

    store = WorkflowStore(tmp_path / 'workflow')
    profiles = FieldProfileService(store)
    original = profiles.get('local')
    draft = profiles.save('local', ProfileEdit(base_revision=original['revision'],
        profile=FieldProfile(name='Test', fixed_fields=[{'id': 'name', 'title': 'Name'}])))
    profiles.publish('local', draft['revision'])
    book = Workbook()
    book.active.append(['Name'])
    book.active.append(['Item'])
    content = BytesIO()
    book.save(content)
    book.close()
    app = FastAPI()

    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = 'local'
        request.state.user_id = 'alice'
        request.state.role = 'owner'
        request.state.authenticated = True
        return await call_next(request)

    app.include_router(build_router(tmp_path))
    with TestClient(app) as client:
        created = client.post('/api/v1/standardization-tasks',
            files={'excel_file': ('input.xlsx', content.getvalue())})
        assert created.status_code == 202, created.text
        task_id = created.json()['id']
        assert client.get(f'/api/v1/workflow-tasks/{task_id}').json()['status'] == 'processing'
        with store.engine.connect() as con:
            pending = con.execute(select(executions).where(executions.c.object_id == task_id)).mappings().one()
        assert pending['status'] == 'pending'
        run_execution(store.root, pending['id'])
        task = client.get(f'/api/v1/workflow-tasks/{task_id}').json()
        assert task['status'] == 'mapping'
        # Legacy clients also receive a queued operation when Prefer is absent.
        response = client.post(f'/api/v1/workflow-tasks/{task_id}/mapping/analyze',
            json={'base_revision': task['revision']})
        assert response.status_code == 202
        operation_id = response.json()['id']
        assert response.headers['Location'].endswith('/operations/' + operation_id)
        assert client.get(f'/api/v1/workflow-tasks/{task_id}/operations/{operation_id}').json()['status'] == 'processing'
        with store.engine.connect() as con:
            pending = con.execute(select(executions).where(executions.c.object_id == operation_id)).mappings().one()
        run_execution(store.root, pending['id'])
        assert client.get(f'/api/v1/workflow-tasks/{task_id}/operations/{operation_id}').json()['status'] == 'ready'
        analyzed = client.get(f'/api/v1/workflow-tasks/{task_id}').json()
        sheets = [{'sheet': sheet['sheet'], 'header_row': sheet['header_row'],
                   'header_depth': sheet['header_depth'], 'bindings': sheet['suggested_bindings']}
                  for sheet in analyzed['payload']['sheets']]
        confirmed = client.post(f'/api/v1/workflow-tasks/{task_id}/mapping',
            json={'base_revision': analyzed['revision'], 'sheets': sheets})
        assert confirmed.status_code == 202
        with store.engine.connect() as con:
            execution_id = con.execute(select(executions.c.id).where(
                executions.c.object_id == confirmed.json()['id'])).scalar_one()
        run_execution(store.root, execution_id)
        ready = client.get(f'/api/v1/workflow-tasks/{task_id}').json()
        assert ready['status'] == 'ready'
        export = client.post(f'/api/v1/workflow-tasks/{task_id}/exports',
            json={'base_revision': ready['revision']})
        assert export.status_code == 202
        with store.engine.connect() as con:
            execution_id = con.execute(select(executions.c.id).where(
                executions.c.object_id == export.json()['id'])).scalar_one()
        run_execution(store.root, execution_id)
        prepared = client.get(export.headers['Location'] + '/result').json()
        with store.engine.connect() as con:
            execution_id = con.execute(select(executions.c.id).where(
                executions.c.object_id == prepared['id'])).scalar_one()
        run_execution(store.root, execution_id)
        assert any(item['id'] == prepared['id'] and item['status'] == 'completed'
                   for item in client.get(f'/api/v1/workflow-tasks/{task_id}').json()['exports'])
    store.engine.dispose()


@pytest.mark.parametrize("fault,expected_status,attempts", [(429, "succeeded", 2), ("timeout", "succeeded", 2),
    (401, "failed", 1), ("invalid_draft", "failed", 1)])
def test_http_template_learning_faults_recover_but_invalid_requests_are_terminal(tmp_path, monkeypatch, fault, expected_status, attempts):
    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx

    from excel_auditor.workflow.ai import AIClient, AISettings
    from excel_auditor.workflow.execution import execute
    from excel_auditor.workflow.templates import TemplateService

    store = WorkflowStore(tmp_path)
    templates = TemplateService(store)
    obj = templates.create("local")
    obj = templates.ai_suggest(obj["id"], "local", obj["revision"], "Review fields")
    with store.engine.connect() as con:
        execution = dict(con.execute(select(executions)).mappings().one())
    calls = []

    class Model(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(self.path)
            first = len(calls) == 1
            if first and fault == "timeout":
                time.sleep(.3)
            draft = {"fields": [{"id": "unknown"}] if fault == "invalid_draft" else [], "columns": [], "notes": []}
            content = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(draft)}}]}).encode()
            self.send_response(fault if first and isinstance(fault, int) else 200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_):
            pass

    monkeypatch.setenv("EXCEL_AUDITOR_ENV", "test")
    monkeypatch.setenv("EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP", "1")
    original_client = httpx.Client

    class ShortTimeoutClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "timeout": .1})

    monkeypatch.setattr(httpx, "Client", ShortTimeoutClient)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = AIClient(store.root, "local")
    client.save(AISettings(base_url=f"http://127.0.0.1:{server.server_port}", model="synthetic", api_key="test-only"))

    try:
        for _ in range(attempts):
            with store.engine.begin() as con:
                con.execute(update(executions).values(available_at="2000-01-01T00:00:00+00:00"))
            execute(store, execution["id"])
        with store.engine.connect() as con:
            row = con.execute(select(executions)).mappings().one()
            assert row["status"] == expected_status and row["attempts"] == attempts
            assert store.get(con, obj["id"], "local")["status"] == ("draft" if expected_status == "succeeded" else "failed")
        assert len(calls) == attempts
        assert set(calls) == {"/chat/completions"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

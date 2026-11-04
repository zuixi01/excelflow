from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import func, select, update

from excel_auditor.workflow.capacity import ResourceLease
from excel_auditor.workflow.execution import ExecutionService, execute
from excel_auditor.workflow.models import WorkflowError
from excel_auditor.workflow.operations import OperationService
from excel_auditor.workflow.repository import WorkflowStore, objects, executions
from excel_auditor.workflow.uploads import digest, persist
from test_name_confirmation import case, workbook


def create(store, actor):
    with store.engine.begin() as con:
        obj = store.create(con, 'local', 'task', {'actor': actor}, 'processing')
        return obj, con.execute(select(executions.c.id).where(executions.c.object_id == obj['id'])).scalar_one()


def test_simultaneous_admission_is_atomic_and_rejection_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv('EXCEL_AUDITOR_WORKFLOW_MAX_WAITING', '3')
    monkeypatch.setenv('EXCEL_AUDITOR_WORKFLOW_USER_WAITING', '100')
    store = WorkflowStore(tmp_path)
    def submit(i):
        try:
            return create(store, str(i))[0]['id']
        except WorkflowError as exc:
            assert exc.code == 'WORKFLOW_CAPACITY_FULL'
    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = [result for result in pool.map(submit, range(20)) if result]
    assert len(accepted) == 5
    with store.engine.connect() as con:
        assert con.execute(select(func.count()).select_from(objects)).scalar_one() == 5
    store.engine.dispose()


def test_execution_global_and_per_user_limits_do_not_consume_retry(tmp_path):
    store = WorkflowStore(tmp_path)
    jobs = [create(store, actor)[1] for actor in ['alice', 'alice', 'bob', 'carol']]
    service = ExecutionService(store)
    first = service.claim(jobs[0], 'one')
    assert first
    assert service.claim(jobs[1], 'two') is None
    assert service.claim(jobs[2], 'three')
    assert service.claim(jobs[3], 'four') is None
    with store.engine.connect() as con:
        assert con.execute(select(executions.c.attempts).where(executions.c.id == jobs[3])).scalar_one() == 0
    service.finish(first)
    with store.engine.begin() as con:
        con.execute(update(executions).where(executions.c.id == jobs[1]).values(available_at=service.stamp()))
    assert service.claim(jobs[1], 'five')
    store.engine.dispose()


def test_upload_model_slots_shared_between_independent_engines(tmp_path):
    first, second = WorkflowStore(tmp_path), WorkflowStore(tmp_path)
    lease = ResourceLease(first, 'upload', 1).acquire()
    try:
        with pytest.raises(WorkflowError, match='名额'):
            ResourceLease(second, 'upload', 1).acquire()
    finally:
        lease.close()
    lease2 = ResourceLease(second, 'upload', 1).acquire()
    lease2.close()
    first.engine.dispose(); second.engine.dispose()


def run_operation(service, task, op, args=None, content=None):
    operations = OperationService(service.store)
    queued = operations.create(task['id'], 'local', 'local', op, args, content)
    with service.store.engine.connect() as con:
        execution = con.execute(select(executions.c.id).where(executions.c.object_id == queued['id'])).scalar_one()
    execute(service.store, execution)
    obj = operations.get(task['id'], 'local', queued['id'])
    assert obj['status'] == 'ready', obj
    return operations.result(obj), obj


def test_durable_name_return_download_and_final_preserve_layout(case, tmp_path):
    service, task, rows, _ = case
    original = service.maintenance_file(task['id'], 'local')
    queued_file, job = run_operation(service, task, 'maintenance_download')
    import re
    assert re.search(r'_维护表_\d{14}\.xlsx$', queued_file['filename'])
    assert '_download_' not in queued_file['filename']
    path = service.store.directory(job['id']) / queued_file['stored_file']
    a, b = load_workbook(original), load_workbook(path)
    assert list(a.active.merged_cells.ranges) == list(b.active.merged_cells.ranges)
    assert list(a.active.values) == list(b.active.values)
    a.close(); b.close()
    upload = tmp_path / 'confirmation.xlsx'
    upload.write_bytes(workbook([[row['id'], f'测试商品{i}'] for i, row in enumerate(rows)]))
    preview, _ = run_operation(service, task, 'name_preview', content=upload)
    result, _ = run_operation(service, task, 'name_apply', {'preview_id': preview['id']})
    assert result['payload']['error_count'] == 0
    exported, _ = run_operation(service, result, 'export_prepare', {'base_revision':result['revision']})
    with service.store.engine.connect() as con:
        execution = con.execute(select(executions.c.id).where(executions.c.object_id == exported['id'])).scalar_one()
    execute(service.store, execution)
    with service.store.engine.connect() as con:
        assert service.store.get(con, exported['id'], 'local')['status'] == 'completed'


def test_queued_operation_rejects_stale_revision_and_wrong_task(case):
    service, task, _, _ = case
    jobs = OperationService(service.store)
    op = jobs.create(task['id'], 'local', 'local', 'maintenance_download')
    with pytest.raises(WorkflowError):
        jobs.get('task_other', 'local', op['id'])
    with service.store.engine.begin() as con:
        con.execute(update(objects).where(objects.c.id == task['id']).values(revision=task['revision'] + 1))
        execution = con.execute(select(executions.c.id).where(executions.c.object_id == op['id'])).scalar_one()
    execute(service.store, execution)
    failed = jobs.get(task['id'], 'local', op['id'])
    assert failed['status'] == 'failed'
    assert failed['payload']['error_code'] == 'REVISION_CONFLICT'


def test_path_inputs_do_not_call_read_bytes(tmp_path, monkeypatch):
    path = tmp_path / 'input.xlsx'; path.write_bytes(b'large-payload' * 10000)
    expected = digest(path.read_bytes(), b'suffix')
    monkeypatch.setattr(type(path), 'read_bytes', lambda self: pytest.fail('whole-file read'))
    assert digest(path, b'suffix') == expected
    target = tmp_path / 'copy.xlsx'; persist(target, path)
    assert target.stat().st_size == path.stat().st_size


def test_production_secret_is_external_not_saved(case, monkeypatch, tmp_path):
    from excel_auditor.workflow.ai import AIClient, AISettings
    key = tmp_path / 'key'; key.write_text('test-production-key')
    monkeypatch.setenv('EXCEL_AUDITOR_ENV', 'production')
    monkeypatch.setenv('EXCEL_AUDITOR_MODEL_KEY_FILE', str(key))
    client = AIClient(case[0].store.root, 'local')
    client.save(AISettings(base_url='https://model.example/v1', model='test'))
    assert 'test-production-key' not in client.path.read_text('utf-8')
    assert client.settings(private=True)['api_key'] == 'test-production-key'


def test_http_download_returns_job_before_work_completes_and_can_resume(case, monkeypatch):
    import threading
    import time
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from excel_auditor.workflow.routes import build_router
    from excel_auditor.workflow.tasks import TaskService
    service, task, _, root = case
    released = threading.Event()
    original = TaskService.maintenance_file
    def slow(self, *args, **kwargs):
        assert released.wait(10)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(TaskService, 'maintenance_file', slow)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request, next):
        request.state.tenant_id='local'; request.state.user_id='local'; request.state.role='owner'; request.state.authenticated=True
        return await next(request)
    @app.exception_handler(WorkflowError)
    async def error(request, exc):
        return JSONResponse({'detail':str(exc)},status_code=exc.status)
    app.include_router(build_router(root, inline_executor_for_tests=True))
    with TestClient(app) as client:
        try:
            response = client.get(f'/api/v1/workflow-tasks/{task["id"]}/maintenance/download',headers={'Prefer':'respond-async'})
            assert response.status_code == 202
            location = response.headers['location']
            assert client.get(location).json()['status'] == 'processing'
            assert client.get(location+'/result').status_code == 409
        finally:
            released.set()
        for _ in range(100):
            if client.get(location).json()['status'] == 'ready': break
            time.sleep(.05)
        download = client.get(location+'/result')
        assert download.status_code == 200
        book=load_workbook(BytesIO(download.content)); assert book.active.cell(1,2).value; book.close()


def test_upload_guard_rejects_before_multipart_parser_and_releases_slot(tmp_path, monkeypatch):
    monkeypatch.setenv('EXCEL_AUDITOR_MAX_UPLOADS', '2')
    from fastapi import FastAPI, File, UploadFile
    from fastapi.testclient import TestClient
    from excel_auditor.workflow.upload_guard import guarded_request, guard_store
    app=FastAPI()
    @app.middleware('http')
    async def guard(request,next):
        return await guarded_request(request,next,tmp_path,None)
    @app.post('/upload')
    async def endpoint(file:UploadFile=File(...)):
        return {'ok':True}
    store=guard_store(tmp_path,None)
    leases=[ResourceLease(store,'upload',2).acquire() for _ in range(2)]
    with TestClient(app) as client:
        try:
            assert client.post('/upload',headers={'Content-Type':'multipart/form-data'},content=b'broken').status_code==429
        finally:
            for lease in leases:lease.close()
        assert client.post('/upload',headers={'Content-Type':'multipart/form-data'},content=b'broken').status_code==400
        lease=ResourceLease(store,'upload',1).acquire();lease.close()


def test_cleanup_does_not_delete_original_tasks_or_active_results(case):
    from excel_auditor.workflow.maintenance_ops import cleanup
    service,task,_,_=case
    result,op=run_operation(service,task,'maintenance_download')
    with service.store.engine.begin() as con:
        con.execute(update(objects).where(objects.c.id==op['id']).values(updated_at='2020-01-01T00:00:00+00:00'))
        # Directly seeded parse outbox is superseded by a later mapping revision.
        con.execute(update(executions).where(executions.c.object_id==task['id']).values(status='superseded'))
    candidates=cleanup(service.store)
    assert candidates['objects']==[op['id']]
    assert service.store.directory(op['id']).exists()
    cleanup(service.store,apply=True)
    assert not (service.store.root/'files'/op['id']).exists()
    assert service.get(task['id'],'local')['id']==task['id']


def test_production_compose_activates_workers_without_legacy_profile():
    from pathlib import Path
    import yaml
    config=yaml.safe_load(Path('deploy/docker-compose.prod.yaml').read_text('utf-8'))
    services=config['services']
    for name,queue in [('worker-workflow','workflow'),('worker-workflow-export','workflow-export')]:
        service=services[name]
        assert service['profiles']==[]
        assert 'extends' not in service  # Compose extends unions profiles even when child supplies [].
        assert service['environment']['EXCEL_AUDITOR_QUEUE_NAME']==queue
        assert service['environment']['EXCEL_AUDITOR_MODEL_KEY_FILE']=='/run/secrets/model-key'
        assert service['environment']['EXCEL_AUDITOR_WORKFLOW_MAX_RUNNING'].endswith(':-2}')
    assert services['workflow-recovery']['profiles']==[]

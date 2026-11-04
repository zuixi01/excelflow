"""Read production as SQLite mode=ro; exercise new async HTTP workflow in a separate root."""
from contextlib import ExitStack
from copy import deepcopy
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from excel_auditor.workflow.field_profiles import FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.models import WorkflowError
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.routes import build_router
from excel_auditor.workflow.tasks import TaskService
from excel_auditor.workflow.uploads import digest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'outputs/production-hardening-20261103/actual-11'
TARGET = 'task_91d9a3c4444f4eae96f0ca20ff6c981f'


def baseline():
    with sqlite3.connect((ROOT / 'var/workflow/workflow.sqlite3').as_uri() + '?mode=ro', uri=True) as con:
        row = con.execute('select payload from workflow_objects where id=?', (TARGET,)).fetchone()
        objects = con.execute('select id,revision,status,payload from workflow_objects order by id').fetchall()
        records = con.execute('select * from workflow_records where task_id=? order by ordinal', (TARGET,)).fetchall()
    return json.loads(row[0]), hashlib.sha256(json.dumps([objects, records], ensure_ascii=False).encode()).hexdigest()


def header(path):
    book = load_workbook(path); sheet = book.active
    result = {'values': [[c.value for c in row] for row in sheet.iter_rows(max_row=3)],
        'styles': [[str(c._style) for c in row] for row in sheet.iter_rows(max_row=3)],
        'merges': sorted(str(r) for r in sheet.merged_cells.ranges if r.max_row <= 3),
        'widths': {k:v.width for k,v in sheet.column_dimensions.items()}}
    book.close(); return result


def image_hashes(path):
    with zipfile.ZipFile(path) as archive:
        return {hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist() if n.startswith('xl/media/') and not n.endswith('/')}


def main():
    original, before = baseline()
    OUT.mkdir(parents=True, exist_ok=True)
    root = OUT / ('runtime-' + str(time.time_ns()))
    store = WorkflowStore(root / 'workflow'); tasks = TaskService(store)
    profiles = FieldProfileService(store)
    draft = profiles.get('local')
    draft = profiles.save('local', ProfileEdit(base_revision=draft['revision'], profile=FieldProfile.model_validate(original['field_snapshot']['profile'])))
    profiles.publish('local', draft['revision'])
    app = FastAPI()
    @app.middleware('http')
    async def identity(request, next):
        request.state.tenant_id = 'local'; request.state.user_id = 'acceptance'
        request.state.role = 'owner'; request.state.authenticated = True
        return await next(request)
    @app.exception_handler(WorkflowError)
    async def failure(request, exc):
        return JSONResponse({'detail':str(exc),'code':exc.code},status_code=exc.status)
    app.include_router(build_router(root))
    started = time.monotonic()
    operations = []
    with TestClient(app) as client:
        client.headers['Prefer'] = 'respond-async'
        def call(method, path, **kwargs):
            response = client.request(method, '/api/v1' + path, **kwargs)
            assert response.status_code < 300, response.text[:1000]
            if response.status_code == 202 and '/operations/' in response.headers.get('location', ''):
                location = response.headers['location']; operations.append(location)
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    status = client.get(location).json()
                    if status['status'] == 'failed': raise AssertionError(status)
                    if status['status'] == 'ready': return client.get(location + '/result')
                    time.sleep(.2)
                raise AssertionError('background operation timeout')
            return response
        source = ROOT / 'var/workflow/files' / TARGET
        with ExitStack() as stack:
            uploads = []
            for item in original['source_files']:
                path = source / item['storage_name']; assert digest(path) == item['sha256']
                uploads.append(('excel_file', (item['filename'], stack.enter_context(path.open('rb')), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')))
            task = call('POST', '/standardization-tasks', files=uploads, data={'name':'仅验收：生产改造 11 文件'}).json()
        tid = task['id']; task = call('GET', '/workflow-tasks/' + tid).json()
        assert task['status'] == 'mapping', task
        print('11 files uploaded and parsed', flush=True)
        task = call('POST', f'/workflow-tasks/{tid}/mapping', json={'base_revision':task['revision'],'sheets':deepcopy(original['mapping']['sheets'])}).json()
        rows = tasks.snapshot(tid,'local',task['revision'],include_rows=True)[1]
        missing = [r for r in rows if r['source'].get('kind') != 'context' and not r['values_json'].get('product_name')]
        if missing:
            book = Workbook(); sheet = book.active; sheet.append(['记录ID','确认商品名称'])
            for i,row in enumerate(missing,1): sheet.append([row['id'],f'仅验收商品{i}'])
            data = BytesIO(); book.save(data); book.close()
            preview = call('POST', f'/workflow-tasks/{tid}/maintenance/name-confirmations', files={'excel_file':('names.xlsx',data.getvalue())}).json()
            task = call('POST', f'/workflow-tasks/{tid}/maintenance/name-confirmations/apply', json={'preview_id':preview['id']}).json()
        assert task['payload']['error_count'] == 0, task['payload']['error_count']
        maintenance = OUT / '仅验收_维护表.xlsx'
        maintenance.write_bytes(call('GET', f'/workflow-tasks/{tid}/maintenance/download').content)
        expected = header(maintenance)
        print('async maintenance download complete; checking return', flush=True)
        with maintenance.open('rb') as handle:
            inspected = call('POST', f'/workflow-tasks/{tid}/maintenance/returns/inspect', files={'excel_file':('return.xlsx',handle)}).json()
        choices = [{k:c[k] for k in ('key','action','field_id','title','kind','type')} for c in inspected['payload']['columns']]
        preview = call('POST', f'/workflow-tasks/{tid}/maintenance/returns/{inspected["id"]}/preview', json={'columns':choices}).json()
        assert preview['status'] == 'ready', preview['payload'].get('row_review')
        assert preview['payload']['counts'] == {'changed':0,'added':0,'missing':0}
        task = call('POST', f'/workflow-tasks/{tid}/maintenance/returns/apply', json={'preview_id':preview['id']}).json()
        again = OUT / '仅验收_回传后维护表.xlsx'
        again.write_bytes(call('GET', f'/workflow-tasks/{tid}/maintenance/download').content)
        assert header(again) == expected
        export = call('POST', f'/workflow-tasks/{tid}/exports', json={'base_revision':task['revision']}).json()
        for _ in range(450):
            current = tasks.get(tid,'local'); run = next(r for r in current['exports'] if r['id'] == export['id'])
            if run['status'] != 'processing': break
            time.sleep(2)
        assert run['status'] == 'completed', run
        final = OUT / '仅验收_最终表.xlsx'
        final.write_bytes(call('GET',f'/workflow-tasks/{tid}/exports/{run["id"]}/files/标准商品表.xlsx').content)
        columns = run['payload']['config']['outputs'][0]['sheets'][0]['columns']
        assert sum(c['operation']=='key_value' for c in columns) == 2
        assert all(c['operation'] != 'key_value' for c in columns if not c['title'].startswith('动态字段/'))
        assert image_hashes(maintenance) == image_hashes(final)
        assert baseline()[1] == before
        report = {'passed':True,'source_files':len(original['source_files']),'records':len(rows),'test_names_only_in_copy':len(missing),
            'async_operations':len(operations),'header_layout_unchanged':True,'image_hashes':len(image_hashes(final)),
            'only_dynamic_fields_summarized':True,'production_unchanged':True,'elapsed_seconds':round(time.monotonic()-started,2),
            'scope':'isolated SQLite + local executor + actual files; not production Redis/PostgreSQL load test; original confirmed mappings reused',
            'runtime':str(root),'task_id':tid}
        (OUT / '验收结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    store.engine.dispose()


if __name__ == '__main__': main()

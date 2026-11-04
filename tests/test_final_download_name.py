from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from excel_auditor.workflow.models import WorkflowError
from excel_auditor.workflow.routes import build_router
from test_flexible_maintenance_return import runtime


def test_final_download_uses_generation_time_and_preserves_file_bytes(runtime, tmp_path):
    store, service, task = runtime
    run, _ = service.create_export(task['id'], 'local', task['revision'])
    service.generate(run['id'], 'local')
    original = service.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx').read_bytes()
    with store.engine.begin() as con:
        export = store.get(con, run['id'], 'local', 'export')
        export['payload']['completed_at'] = '2026-11-03T07:30:25+00:00'
        store.save(con, export)
        renamed = store.get(con, task['id'], 'local', 'task')
        renamed['payload']['name'] = '任务后来改名'
        store.save(con, renamed)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = request.state.user_id = 'local'
        request.state.authenticated = True
        request.state.role = 'admin'
        return await call_next(request)
    @app.exception_handler(WorkflowError)
    async def errors(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status)
    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    root = f"/api/v1/workflow-tasks/{task['id']}/exports/{run['id']}/files/"
    with TestClient(app) as client:
        first = client.get(root + '标准商品表.xlsx')
        again = client.get(root + '标准商品表.xlsx')
        assert first.status_code == 200
        assert first.content == again.content == original
        assert '自由回传测试_最终表_20261103153025.xlsx' in unquote(first.headers['content-disposition'])
        assert again.headers['content-disposition'] == first.headers['content-disposition']
        assert first.headers['cache-control'] == 'no-store'
        assert '最终表.zip' in unquote(client.get(root + '最终表.zip').headers['content-disposition'])
        assert client.get(root + '不存在.xlsx').status_code == 404


def test_old_final_export_supports_new_name_without_regenerating(runtime):
    store, service, task = runtime
    run, _ = service.create_export(task['id'], 'local', task['revision'])
    service.generate(run['id'], 'local')
    with store.engine.begin() as con:
        export = store.get(con, run['id'], 'local', 'export')
        export['payload'].pop('task_name')
        export['payload']['completed_at'] = '2026-11-03T07:30:25+00:00'
        store.save(con, export)
        current = store.get(con, task['id'], 'local', 'task')
        current['payload']['name'] = '供应商/报价:10?*'
        store.save(con, current)
    assert service.export_download_name(task['id'], run['id'], 'local', '标准商品表.xlsx') == '供应商_报价_10___最终表_20261103153025.xlsx'


def test_final_name_is_consistent_in_plan_history_preview_and_download(runtime, tmp_path):
    store, service, task = runtime
    before = service.get(task['id'], 'local')
    assert before['final_outputs'][0]['display_name'] == '自由回传测试_最终表_年月日时分秒.xlsx'
    run, _ = service.create_export(task['id'], 'local', task['revision'])
    service.generate(run['id'], 'local')
    with store.engine.begin() as con:
        export = store.get(con, run['id'], 'local', 'export')
        export['payload']['completed_at'] = '2026-11-03T07:30:25+00:00'
        store.save(con, export)
    detail = service.get(task['id'], 'local')
    expected = '自由回传测试_最终表_20261103153025.xlsx'
    assert detail['final_outputs'][0]['display_name'] == expected
    assert detail['exports'][0]['payload']['artifacts'][0]['download_name'] == expected
    assert service.export_download_name(task['id'], run['id'], 'local', '标准商品表.xlsx') == expected

    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = request.state.user_id = 'local'
        request.state.authenticated = True
        request.state.role = 'admin'
        return await call_next(request)
    @app.exception_handler(WorkflowError)
    async def errors(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status)
    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    with TestClient(app) as client:
        preview = client.get(f"/api/v1/workflow-tasks/{task['id']}/exports/{run['id']}/preview/标准商品表.xlsx")
        assert preview.status_code == 200
        assert preview.json()['name'] == expected

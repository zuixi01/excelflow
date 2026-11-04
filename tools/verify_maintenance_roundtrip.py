"""Isolated, real HTTP acceptance harness. Never points at the business data root."""
import argparse
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import uvicorn

from excel_auditor.workflow.routes import build_router
from excel_auditor.workflow.models import WorkflowError


def make_app(root):
    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = 'acceptance'
        request.state.user_id = 'acceptance-owner'
        request.state.role = 'owner'
        request.state.authenticated = True
        return await call_next(request)
    @app.exception_handler(WorkflowError)
    async def error(request, exc):
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=exc.status)
    @app.get('/api/v1/auth/session')
    def session():
        return {'authenticated': True, 'role': 'owner', 'username': '隔离验收', 'user_id': 'acceptance-owner'}
    @app.get('/api/v1/auth/csrf')
    def csrf():
        return {'csrf_token': 'isolated-acceptance'}
    app.include_router(build_router(root))
    return app


def prepare(root, url):
    client = httpx.Client(base_url=url, timeout=60)
    profile = {'name': '灯具回传验收字段', 'fixed_fields': [
        {'id': 'sku', 'title': '货号', 'type': 'id_code', 'required': True},
        {'id': 'sale_price', 'title': '售价', 'type': 'decimal', 'required': True},
    ], 'local_categories': [{'id': 'lighting', 'title': '灯具', 'attributes': [
        {'id': 'brand', 'title': '品牌', 'required': True},
        {'id': 'series', 'title': '系列'}, {'id': 'name', 'title': '名称', 'required': True},
        {'id': 'color', 'title': '颜色'}], 'specifications': [
        {'id': 'style', 'title': '款式'}, {'id': 'power', 'title': '功率', 'required': True}]}]}
    draft = client.get('/api/v1/field-profile').json()
    saved = client.put('/api/v1/field-profile', json={'base_revision': draft['revision'], 'profile': profile})
    saved.raise_for_status()
    published = client.post('/api/v1/field-profile/publish', json={'base_revision': saved.json()['revision']})
    published.raise_for_status()
    (root / 'field-profile.json').write_text(json.dumps(published.json(), ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'profile_published': True, 'url': url}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['serve', 'prepare'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8003)
    args = parser.parse_args()
    root = args.root.resolve()
    assert root.is_relative_to(Path.cwd().resolve() / 'outputs'), 'Acceptance root must be in workspace outputs'
    root.mkdir(exist_ok=True, parents=True)
    if args.operation == 'serve':
        uvicorn.run(make_app(root / '.runtime'), host='127.0.0.1', port=args.port)
    else:
        prepare(root, f'http://127.0.0.1:{args.port}')

import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from excel_auditor.standard_sources import ConnectionRegistry, ManagedHttpSource
from excel_auditor.workflow.models import WorkflowError
from excel_auditor.workflow.routes import build_router
from excel_auditor.workflow import platform_fields


def managed_connection(tmp_path, monkeypatch, payload=None, *, private=False):
    token = 'test-only-platform-token'
    monkeypatch.setenv('EXCEL_AUDITOR_SECRET_PRODUCT_FIELDS_TOKEN', token)
    config = {'connections': [{
        'id': 'product-platform', 'base_url': 'https://platform.example.com/',
        'allowed_paths': ['/fields'], 'auth_secret_ref': 'product_fields_token',
        'field_definition': {'title': '平台商品字段', 'path': '/fields', 'tenant_ids': ['local']},
    }]}
    path = tmp_path / 'connections.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    response = payload or {'data': {'schema_version': 'v2', 'categories': [
        {'id': 'lighting', 'title': '灯具',
         'attributes': [{'id': 'brand', 'title': '品牌', 'aliases': ['厂牌']}],
         'specifications': [{'id': 'power', 'title': '功率', 'type': 'string'}]},
    ]}}

    def respond(request):
        assert request.headers['Authorization'] == 'Bearer ' + token
        assert request.url.path == '/fields'
        return httpx.Response(200, json=response)

    return ManagedHttpSource(ConnectionRegistry(path), transport=httpx.MockTransport(respond),
                             resolver=lambda _host: ['127.0.0.1' if private else '93.184.216.34'])


def app_client(tmp_path):
    app = FastAPI()

    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = request.headers.get('X-Tenant', 'local')
        request.state.user_id = 'test-user'
        request.state.authenticated = True
        request.state.role = request.headers.get('X-Role', 'owner')
        return await call_next(request)

    @app.exception_handler(WorkflowError)
    async def error(_request, exc):
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=exc.status)

    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    return TestClient(app)


def test_admin_fetch_preview_import_publish_and_historical_snapshot(tmp_path, monkeypatch):
    managed = managed_connection(tmp_path, monkeypatch)
    monkeypatch.setattr(platform_fields, 'source', lambda: managed)
    with app_client(tmp_path) as client:
        assert client.get('/api/v1/field-profile/platform-connections', headers={'X-Role': 'operator'}).status_code == 403
        assert client.post('/api/v1/field-profile/platform-preview', json={'connection_id': 'product-platform'},
                           headers={'X-Tenant': 'another'}).status_code == 404
        connections = client.get('/api/v1/field-profile/platform-connections').json()
        assert connections == {'configured': True, 'items': [{'id': 'product-platform', 'title': '平台商品字段'}]}
        assert 'test-only-platform-token' not in json.dumps(connections)
        preview = client.post('/api/v1/field-profile/platform-preview', json={'connection_id': 'product-platform'}).json()
        assert preview['field_count'] == 2
        assert preview['categories'][0]['attributes'][0]['title'] == '品牌'
        assert 'test-only-platform-token' not in json.dumps(preview)

        initial = client.get('/api/v1/field-profile').json()
        earlier = client.put('/api/v1/field-profile', json={
            'base_revision': initial['revision'], 'profile': initial['payload']['draft']}).json()
        earlier = client.post('/api/v1/field-profile/publish', json={'base_revision': earlier['revision']}).json()
        old_published = earlier['payload']['published']
        old_version = earlier['payload']['version']
        draft = client.get('/api/v1/field-profile').json()['payload']['draft']
        draft['local_categories'] = preview['categories']
        draft['platform_source'] = {'connection_id': preview['connection_id'],
                                    'schema_version': preview['schema_version'],
                                    'fetched_at': preview['fetched_at']}
        saved = client.put('/api/v1/field-profile', json={'base_revision': earlier['revision'], 'profile': draft})
        assert saved.status_code == 200, saved.text
        # Editing a draft never changes the published definition used by tasks.
        assert saved.json()['payload']['published'] == old_published
        published = client.post('/api/v1/field-profile/publish', json={'base_revision': saved.json()['revision']})
        assert published.status_code == 200, published.text
        assert published.json()['payload']['version'] == old_version + 1
        structure = client.post('/api/v1/field-profile/preview', json=published.json()['payload']['published'])
        assert structure.status_code == 200, structure.text
        assert structure.json()['fields'] == len(draft['fixed_fields']) + 2
        assert published.json()['payload']['published']['platform_source']['connection_id'] == 'product-platform'


def test_invalid_contract_and_private_target_do_not_import(tmp_path, monkeypatch):
    malformed = managed_connection(tmp_path, monkeypatch, {'categories': [
        {'id': 'lighting', 'title': '灯具', 'attributes': [{'id': 'brand', 'title': '品牌'}],
         'specifications': []},
        {'id': 'lighting', 'title': '重复类目', 'attributes': [], 'specifications': []},
    ]})
    try:
        platform_fields.preview('local', 'product-platform', malformed)
        assert False, 'duplicate category IDs must be rejected'
    except WorkflowError as exc:
        assert exc.code == 'PLATFORM_SCHEMA_INVALID'

    private = managed_connection(tmp_path, monkeypatch, private=True)
    try:
        platform_fields.preview('local', 'product-platform', private)
        assert False, 'private destination must be rejected without an explicit managed exception'
    except WorkflowError as exc:
        assert exc.code == 'PLATFORM_FETCH_FAILED'


def test_direct_form_saves_secret_and_previews_without_exposing_key(tmp_path, monkeypatch):
    from excel_auditor.workflow.platform_fields import DirectPlatformConnection, DirectPlatformSettings
    monkeypatch.delenv('EXCEL_AUDITOR_ALLOW_PRIVATE_PLATFORM', raising=False)
    secret = 'test-only-direct-token'
    with app_client(tmp_path) as client:
        endpoint = '/api/v1/field-profile/platform-direct'
        assert client.get(endpoint, headers={'X-Role': 'operator'}).status_code == 403
        assert client.put(endpoint, json={'base_url': 'https://platform.example.com', 'path': '/fields',
                                          'api_key': secret}, headers={'X-Role': 'operator'}).status_code == 403
        saved = client.put(endpoint, json={'base_url': 'https://platform.example.com', 'path': '/fields',
                                           'method': 'GET', 'api_key': secret})
        assert saved.status_code == 200, saved.text
        assert saved.json()['has_key'] is True
        assert secret not in saved.text
        assert secret not in client.get(endpoint).text
        assert secret not in ''.join(path.read_text('utf-8') for path in (tmp_path / 'workflow' / 'settings').glob('platform-*.json'))
        assert client.get(endpoint, headers={'X-Tenant': 'other'}).json()['configured'] is False

    seen = []
    def respond(request):
        seen.append(request.headers.get('Authorization'))
        return httpx.Response(200, json={'categories': [{'id': 'bath', 'title': '卫浴',
            'attributes': [{'id': 'brand', 'title': '品牌'}], 'specifications': []}]})
    direct = DirectPlatformConnection(tmp_path / 'workflow', 'local')
    preview = direct.preview('local', transport=httpx.MockTransport(respond), resolver=lambda _: ['93.184.216.34'])
    assert preview['category_count'] == 1
    assert seen == ['Bearer ' + secret]
    try:
        direct.preview('local', transport=httpx.MockTransport(respond), resolver=lambda _: ['127.0.0.1'])
        assert False, 'loopback must be blocked'
    except WorkflowError as exc:
        assert exc.code == 'PLATFORM_FETCH_FAILED'
    direct.save(DirectPlatformSettings(base_url='https://new.example.com', path='/fields'))
    assert direct.public()['has_key'] is False
    direct.preview('local', transport=httpx.MockTransport(respond), resolver=lambda _: ['93.184.216.34'])
    assert seen[-1] is None


def test_direct_form_rejects_http_and_unsafe_path(tmp_path):
    with app_client(tmp_path) as client:
        endpoint = '/api/v1/field-profile/platform-direct'
        assert client.put(endpoint, json={'base_url': 'http://platform.example.com', 'path': '/fields'}).status_code == 422
        assert client.put(endpoint, json={'base_url': 'https://platform.example.com', 'path': '/../admin'}).status_code == 422

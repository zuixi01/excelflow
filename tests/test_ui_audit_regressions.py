import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('EXCEL_AUDITOR_DATA', str(tmp_path))
    monkeypatch.setenv('EXCEL_AUDITOR_REQUIRE_AUTH', '1')
    monkeypatch.setenv('EXCEL_AUDITOR_ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('EXCEL_AUDITOR_ADMIN_PASSWORD', 'audit-test-password')
    for key in ('DATABASE_URL','REDIS_URL','S3_BUCKET','EXCEL_AUDITOR_API_TOKEN','EXCEL_AUDITOR_API_TOKENS_JSON','EXCEL_AUDITOR_AUTH_DATABASE_URL','EXCEL_AUDITOR_CONNECTIONS'):
        monkeypatch.delenv(key, raising=False)
    import excel_auditor.api as module
    module = importlib.reload(module)
    with TestClient(module.app) as client:
        assert client.post('/api/v1/auth/login', json={'username':'admin','password':'audit-test-password'}).status_code == 200
        client.headers['X-CSRF-Token'] = client.get('/api/v1/auth/csrf').json()['csrf_token']
        yield client, module
    module.auth_database.engine.dispose()


def test_invalid_legacy_draft_publish_is_actionable_not_server_error(runtime):
    client, _ = runtime
    response = client.post('/api/v1/schemas/audit/drafts', json={'config': {'schema_id':'audit'}})
    assert response.status_code == 201
    result = client.post(f"/api/v1/schemas/audit/drafts/{response.json()['draft_id']}/publish")
    assert result.status_code == 422
    assert '规则校验失败' in result.json()['detail']


def test_settings_report_current_standardization_limits(runtime):
    client, _ = runtime
    limits = client.get('/api/v1/workflow-settings').json()['limits']
    assert (limits['upload_mib'], limits['total_upload_mib'], limits['source_files']) == (100, 500, 30)


def test_operator_cannot_write_admin_rule_aliases(runtime):
    client, module = runtime
    module.auth_database.create_operator(username='operator', password='audit-test-password', role='operator')
    client.post('/api/v1/auth/logout')
    assert client.post('/api/v1/auth/login', json={'username':'operator','password':'audit-test-password'}).status_code == 200
    client.headers['X-CSRF-Token'] = client.get('/api/v1/auth/csrf').json()['csrf_token']
    result = client.post('/api/v1/mappings/confirm', json={'schema_id':'audit','draft_id':'draft_example','raw_header':'厂牌','canonical_field':'brand'})
    assert result.status_code == 403

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def test_operator_accounts_use_workspace_quota_without_personal_limits(tmp_path):
    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    database.ensure_local_admin(username="admin", password="correct-password")
    operator = database.create_operator(username="Alice", password="operator-password", role="operator")

    for index in range(13):
        database.create_job(f"workspace_task_{index}", tenant_id="local", user_id=operator.id)


def test_operator_management_route_is_local_and_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_PASSWORD", "correct-password")
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKENS_JSON", raising=False)
    import excel_auditor.api as api_module

    api_module = importlib.reload(api_module)
    client = TestClient(api_module.app)
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"}).status_code == 200
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]

    created = client.post(
        "/api/v1/operators",
        json={"username": "alice", "password": "operator-password", "role": "operator", "max_running": 3, "max_queued": 12, "daily_limit": 50},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert "tenant_id" not in payload
    assert "quota" not in payload
    assert "password_hash" not in created.text

    listed = client.get("/api/v1/operators")
    assert listed.status_code == 200
    assert next(item for item in listed.json()["items"] if item["username"] == "alice")["role"] == "operator"

    events = api_module.auth_database.list_auth_events("local")
    assert any(event.action == "operator.created" for event in events)


def test_operator_route_rejects_admin_role_for_new_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_PASSWORD", "correct-password")
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKENS_JSON", raising=False)
    import excel_auditor.api as api_module

    api_module = importlib.reload(api_module)
    client = TestClient(api_module.app)
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"}).status_code == 200
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]

    response = client.post(
        "/api/v1/operators",
        json={"username": "second-admin", "password": "operator-password", "role": "admin"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422


def test_operator_job_quota_is_enforced_in_database_transaction(tmp_path):
    import pytest
    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    database.ensure_local_admin(username="admin", password="correct-password")
    operator = database.create_operator(username="alice", password="operator-password")
    for index in range(13):
        database.create_job(f"job_{index}", tenant_id="local", user_id=operator.id)


def test_operator_can_only_access_own_comparison_job(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_PASSWORD", "correct-password")
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKENS_JSON", raising=False)
    import excel_auditor.api as api_module

    api_module = importlib.reload(api_module)
    operator_a = api_module.auth_database.create_operator(username="alice", password="operator-password")
    operator_b = api_module.auth_database.create_operator(username="bob", password="operator-password")
    job_id = api_module.service.create_job(user_id=operator_a.id)
    client = TestClient(api_module.app)
    assert client.post("/api/v1/auth/login", json={"username": "bob", "password": "operator-password"}).status_code == 200
    assert client.get(f"/api/v1/comparisons/{job_id}").status_code == 404

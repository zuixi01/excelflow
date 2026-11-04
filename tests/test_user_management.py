from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _login(client: TestClient, tenant_id: str, username: str, password: str):
    response = client.post("/api/v1/auth/login", json={"tenant_id": tenant_id, "username": username, "password": password})
    assert response.status_code == 200, response.text
    csrf = client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    return csrf.json()["csrf_token"]


def test_admin_can_manage_same_tenant_users_and_disable_revokes_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKENS_JSON", raising=False)
    import excel_auditor.api as api_module

    api_module = importlib.reload(api_module)
    tenant = api_module.auth_database.create_tenant("Tenant", tenant_id="tenant-a")
    admin = api_module.auth_database.create_user(tenant_id=tenant.id, username="admin", password="admin-password", role="admin")
    api_module.auth_database.create_user(tenant_id=tenant.id, username="operator", password="operator-password", role="operator")
    client = TestClient(api_module.app)
    csrf = _login(client, tenant.id, "admin", "admin-password")

    created = client.post(
        "/api/v1/users",
        headers={"X-CSRF-Token": csrf},
        json={"username": "viewer", "password": "viewer-password", "role": "viewer"},
    )
    assert created.status_code == 201, created.text
    viewer_id = created.json()["id"]
    assert created.json()["password_hash"] if False else "password_hash" not in created.json()

    users = client.get("/api/v1/users")
    assert users.status_code == 200
    assert {item["username"] for item in users.json()["items"]} == {"admin", "operator", "viewer"}

    viewer_client = TestClient(api_module.app)
    viewer_csrf = _login(viewer_client, tenant.id, "viewer", "viewer-password")
    denied = viewer_client.get("/api/v1/users")
    assert denied.status_code == 403
    viewer_write = viewer_client.post(
        "/api/v1/schemas",
        headers={"X-CSRF-Token": viewer_csrf},
        json={"schema_id": "viewer-blocked", "schema_version": "1.0.0"},
    )
    assert viewer_write.status_code == 403

    disabled = client.post(f"/api/v1/users/{viewer_id}/disable", headers={"X-CSRF-Token": csrf})
    assert disabled.status_code == 200
    assert viewer_client.get("/api/v1/auth/session").status_code == 401


def test_users_are_tenant_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKENS_JSON", raising=False)
    import excel_auditor.api as api_module

    api_module = importlib.reload(api_module)
    first = api_module.auth_database.create_tenant("First", tenant_id="tenant-a")
    second = api_module.auth_database.create_tenant("Second", tenant_id="tenant-b")
    api_module.auth_database.create_user(tenant_id=first.id, username="admin", password="admin-password", role="admin")
    api_module.auth_database.create_user(tenant_id=second.id, username="admin", password="admin-password", role="admin")
    client = TestClient(api_module.app)
    _login(client, first.id, "admin", "admin-password")

    assert client.get("/api/v1/users").json()["items"]
    assert client.get(f"/api/v1/users?tenant_id={second.id}").status_code == 403


def test_bootstrap_owner_creates_tenant_and_hashed_password(tmp_path):
    from excel_auditor.admin_cli import bootstrap_owner
    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    user = bootstrap_owner(database, tenant_id="tenant-bootstrap", tenant_name="Bootstrap", username="owner", password="bootstrap-password")

    assert user.role == "owner"
    assert user.password_hash != "bootstrap-password"
    assert database.verify_user_password(user.id, "bootstrap-password") is True

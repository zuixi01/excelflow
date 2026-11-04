from __future__ import annotations

import importlib
import json
import pytest

from fastapi.testclient import TestClient


def test_cookie_login_session_logout_and_csrf_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKENS_JSON", raising=False)
    import excel_auditor.api as api_module

    api_module = importlib.reload(api_module)
    tenant = api_module.auth_database.create_tenant("Tenant", tenant_id="tenant-a")
    api_module.auth_database.create_user(tenant_id=tenant.id, username="alice", password="correct-password", role="owner")
    client = TestClient(api_module.app)

    denied = client.get("/api/v1/auth/session")
    assert denied.status_code == 401
    login = client.post("/api/v1/auth/login", json={"tenant_id": tenant.id, "username": "alice", "password": "correct-password"})
    assert login.status_code == 200, login.text
    assert login.json()["user_id"]
    assert "password" not in login.text
    assert "excel_flow_session" in client.cookies

    current = client.get("/api/v1/auth/session")
    assert current.status_code == 200
    assert current.json()["username"] == "alice"
    csrf = client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200 and len(csrf.json()["csrf_token"]) >= 32

    payload = {"schema_id": "blocked", "schema_version": "1.0.0", "name": "blocked"}
    without_csrf = client.post("/api/v1/schemas", json=payload)
    assert without_csrf.status_code == 403

    with_csrf = client.post("/api/v1/schemas", json=payload, headers={"X-CSRF-Token": csrf.json()["csrf_token"]})
    assert with_csrf.status_code != 403

    logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf.json()["csrf_token"]})
    assert logout.status_code == 204
    assert client.get("/api/v1/auth/session").status_code == 401


def test_login_failure_is_generic(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKENS_JSON", raising=False)
    import excel_auditor.api as api_module

    api_module = importlib.reload(api_module)
    tenant = api_module.auth_database.create_tenant("Tenant", tenant_id="tenant-a")
    api_module.auth_database.create_user(tenant_id=tenant.id, username="alice", password="correct-password", role="owner")
    client = TestClient(api_module.app)

    unknown = client.post("/api/v1/auth/login", json={"tenant_id": tenant.id, "username": "nobody", "password": "wrong-password"})
    wrong = client.post("/api/v1/auth/login", json={"tenant_id": tenant.id, "username": "alice", "password": "wrong-password"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"] == "invalid credentials"


@pytest.mark.parametrize("check", ["validate_session", "validate_csrf"])
def test_slow_auth_database_does_not_block_other_http_requests(tmp_path, monkeypatch, check):
    import asyncio
    import threading

    import httpx
    import excel_auditor.api as api_module

    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    api_module = importlib.reload(api_module)
    api_module.auth_database.ensure_local_admin(username="admin", password="test-password")
    login_client = TestClient(api_module.app)
    login_client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-password"}).raise_for_status()
    csrf = login_client.get("/api/v1/auth/csrf").json()["csrf_token"]
    cookie = login_client.cookies.get("excel_flow_session")
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    original = getattr(api_module.auth_service, check)

    def delayed(*args):
        entered.set()
        release.wait(timeout=5)
        returned.set()
        return original(*args)

    monkeypatch.setattr(api_module.auth_service, check, delayed)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url="http://testserver") as client:
            headers = {"Cookie": "excel_flow_session=" + cookie, "X-CSRF-Token": csrf}
            request = asyncio.create_task(client.get("/api/v1/auth/session", headers=headers) if check == "validate_session"
                                          else client.post("/api/v1/auth/logout", headers=headers))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                response = await asyncio.wait_for(client.get("/health/live"), timeout=2)
                assert response.status_code == 200 and not returned.is_set()
            finally:
                release.set()
                (await request).raise_for_status()

    asyncio.run(exercise())

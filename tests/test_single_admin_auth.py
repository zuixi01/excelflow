from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _load_api(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_USERNAME", "administrator")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_PASSWORD", "correct-password")
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKENS_JSON", raising=False)
    import excel_auditor.api as api_module

    return importlib.reload(api_module)


def test_admin_login_only_accepts_account_and_password(tmp_path, monkeypatch):
    api_module = _load_api(tmp_path, monkeypatch)
    client = TestClient(api_module.app)

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "administrator", "password": "correct-password"},
    )

    assert login.status_code == 200, login.text
    assert login.json()["username"] == "administrator"
    assert "tenant_id" not in login.json()



def test_admin_session_does_not_expose_tenant_concept(tmp_path, monkeypatch):
    api_module = _load_api(tmp_path, monkeypatch)
    client = TestClient(api_module.app)
    assert client.post(
        "/api/v1/auth/login",
        json={"username": "administrator", "password": "correct-password"},
    ).status_code == 200

    session = client.get("/api/v1/auth/session")
    assert session.status_code == 200
    assert session.json()["username"] == "administrator"
    assert "tenant_id" not in session.json()

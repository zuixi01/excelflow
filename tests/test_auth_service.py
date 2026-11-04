from __future__ import annotations

from datetime import timedelta

import pytest


def _database(tmp_path):
    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    database.create_tenant("Local", tenant_id="local")
    return database


def test_login_returns_server_session_and_csrf_credentials(tmp_path):
    from excel_auditor.auth_service import AuthenticationError, AuthService

    database = _database(tmp_path)
    user = database.create_user(tenant_id="local", username="alice", password="correct-password", role="owner")
    service = AuthService(database, session_ttl=timedelta(hours=8))

    identity, credentials = service.login("alice", "correct-password", ip="127.0.0.1", user_agent="test")

    assert identity.user_id == user.id
    assert identity.tenant_id == "local"
    assert identity.role == "owner"
    assert len(credentials.token) >= 32
    assert len(credentials.csrf_token) >= 32
    assert database.get_active_auth_session(service.token_digest(credentials.token)) is not None
    with pytest.raises(AuthenticationError):
        service.login("alice", "wrong-password", ip="127.0.0.1", user_agent="test")


def test_csrf_and_session_revocation_are_server_side(tmp_path):
    from excel_auditor.auth_service import AuthService

    database = _database(tmp_path)
    database.create_user(tenant_id="local", username="alice", password="correct-password", role="owner")
    service = AuthService(database)
    identity, credentials = service.login("alice", "correct-password", ip="127.0.0.1", user_agent="test")

    assert service.validate_session(credentials.token) is not None
    assert service.validate_csrf(identity.session_id, credentials.csrf_token) is True
    assert service.validate_csrf(identity.session_id, "wrong") is False
    service.logout(credentials.token)
    assert service.validate_session(credentials.token) is None


def test_repeated_failures_lock_account_without_revealing_username_state(tmp_path):
    from excel_auditor.auth_service import AuthenticationError, AuthService

    database = _database(tmp_path)
    database.create_user(tenant_id="local", username="alice", password="correct-password", role="owner")
    service = AuthService(database, max_failures=2, lock_seconds=300)

    for _ in range(2):
        with pytest.raises(AuthenticationError, match="invalid credentials"):
            service.login("alice", "wrong-password", ip="127.0.0.1", user_agent="test")
    with pytest.raises(AuthenticationError, match="invalid credentials"):
        service.login("alice", "correct-password", ip="127.0.0.1", user_agent="test")


def test_ip_failure_limit_and_auth_events_are_recorded(tmp_path):
    from excel_auditor.auth_service import AuthenticationError, AuthService

    database = _database(tmp_path)
    database.create_user(tenant_id="local", username="alice", password="correct-password", role="owner")
    service = AuthService(database, max_failures=5, ip_max_failures=2)

    for _ in range(2):
        with pytest.raises(AuthenticationError, match="invalid credentials"):
            service.login("alice", "wrong-password", ip="192.0.2.10", user_agent="test")
    with pytest.raises(AuthenticationError, match="invalid credentials"):
        service.login("alice", "correct-password", ip="192.0.2.10", user_agent="test")
    identity, _credentials = service.login("alice", "correct-password", ip="192.0.2.11", user_agent="test")

    assert identity.user_id
    assert {event.action for event in database.list_auth_events()} >= {"login_failed", "login_succeeded"}


def test_failure_window_expires_across_service_instances(tmp_path):
    from datetime import UTC, datetime
    from excel_auditor.auth_service import AuthenticationError, AuthService

    database = _database(tmp_path)
    database.create_user(tenant_id="local", username="alice", password="correct-password", role="owner")
    instant = [datetime.now(UTC)]
    first = AuthService(database, max_failures=2, lock_seconds=10, clock=lambda: instant[0])
    second = AuthService(database, max_failures=2, lock_seconds=10, clock=lambda: instant[0])
    for service in (first, second):
        with pytest.raises(AuthenticationError):
            service.login("alice", "wrong-password", ip="192.0.2.10")
    with pytest.raises(AuthenticationError):
        first.login("alice", "correct-password", ip="192.0.2.10")
    instant[0] += timedelta(seconds=11)
    assert second.login("alice", "correct-password", ip="192.0.2.10")[0].username == "alice"


def test_bootstrap_preserves_existing_credentials_and_status(tmp_path):
    database = _database(tmp_path)
    user = database.ensure_local_admin(username="admin", password="old-password")
    database.set_user_password(user.id, "new-password")
    database.set_user_status(user.id, "disabled")
    again = database.ensure_local_admin(username="admin", password="old-password")
    assert again.password_hash == database.get_user(user.id).password_hash
    assert again.status == "disabled"
    database.set_user_status(user.id, "active")
    assert database.verify_user_password(user.id, "new-password")
    assert not database.verify_user_password(user.id, "old-password")


def test_concurrent_bootstrap_creates_only_one_admin(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from excel_auditor.persistence import DatabaseRepository
    database = DatabaseRepository(f"sqlite:///{tmp_path / 'bootstrap.sqlite3'}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        users = list(pool.map(lambda _: database.ensure_local_admin(username="admin", password="bootstrap-password"), range(4)))
    assert {user.id for user in users} == {"admin"}
    assert len(database.list_users("local")) == 1


def test_explicit_cli_reset_revokes_sessions(tmp_path, monkeypatch):
    import sys
    from excel_auditor.admin_cli import main
    from excel_auditor.auth_service import AuthService
    from excel_auditor.persistence import DatabaseRepository
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path))
    monkeypatch.setenv("EXCEL_AUDITOR_BOOTSTRAP_PASSWORD", "new-admin-password")
    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    database.ensure_local_admin(username="admin", password="old-admin-password")
    service = AuthService(database)
    _, credentials = service.login("admin", "old-admin-password")
    monkeypatch.setattr(sys, "argv", ["excel-auditor-admin", "reset-password", "--username", "admin"])
    main()
    assert service.validate_session(credentials.token) is None
    assert service.login("admin", "new-admin-password")[0].username == "admin"

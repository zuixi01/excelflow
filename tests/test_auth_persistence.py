from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def test_user_repository_hashes_password_and_enforces_tenant_username_uniqueness(tmp_path):
    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    tenant = database.create_tenant("Tenant A")
    user = database.create_user(
        tenant_id=tenant.id,
        username="Alice",
        password="Correct Horse Battery Staple!",
        role="admin",
    )

    assert user.password_hash != "Correct Horse Battery Staple!"
    assert database.verify_user_password(user.id, "Correct Horse Battery Staple!") is True
    assert database.verify_user_password(user.id, "wrong") is False

    with pytest.raises(ValueError, match="username already exists"):
        database.create_user(tenant_id=tenant.id, username="alice", password="another-password", role="viewer")


def test_same_username_is_allowed_in_different_tenants_and_disabled_user_cannot_login(tmp_path):
    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    first = database.create_tenant("First")
    second = database.create_tenant("Second")
    first_user = database.create_user(tenant_id=first.id, username="operator", password="first-password", role="operator")
    second_user = database.create_user(tenant_id=second.id, username="operator", password="second-password", role="operator")

    assert first_user.tenant_id != second_user.tenant_id
    database.set_user_status(first_user.id, "disabled")
    assert database.get_user_by_username(first.id, "operator") .status == "disabled"
    assert database.get_user_by_username(second.id, "operator") .status == "active"


def test_session_can_be_created_and_revoked(tmp_path):
    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    tenant = database.create_tenant("Tenant")
    user = database.create_user(tenant_id=tenant.id, username="owner", password="password", role="owner")
    now = datetime.now(UTC)
    session = database.create_auth_session(
        user_id=user.id,
        token_hash="token-hash",
        csrf_hash="csrf-hash",
        created_at=now,
        expires_at=now + timedelta(hours=8),
    )

    assert database.get_active_auth_session("token-hash") is not None
    database.revoke_auth_session(session.id, now)
    assert database.get_active_auth_session("token-hash") is None

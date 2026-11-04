from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
import secrets
from datetime import UTC, datetime, timedelta

from .auth_models import AuthIdentity, SessionCredentials, normalize_username, verify_password
from .persistence import DatabaseRepository


class AuthenticationError(ValueError):
    """A deliberately non-specific authentication failure."""


LOCAL_TENANT_ID = "local"


class AuthService:
    def __init__(
        self,
        database: DatabaseRepository,
        *,
        session_ttl: timedelta = timedelta(hours=8),
        max_failures: int = 5,
        lock_seconds: int = 300,
        ip_max_failures: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.session_ttl = session_ttl
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.ip_max_failures = ip_max_failures or max(25, max_failures * 5)
        self.clock = clock or (lambda: datetime.now(UTC))
        from .auth_limits import LoginLimiter
        self.limiter = LoginLimiter(database, seconds=lock_seconds)

    @staticmethod
    def session_csrf(token: str) -> str:
        return hmac.new(token.encode(), b"excel-flow-csrf-v1", hashlib.sha256).hexdigest()

    @staticmethod
    def token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def csrf_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def login(self, username: str, password: str, *, tenant_id: str | None = None, ip: str = "", user_agent: str = "") -> tuple[AuthIdentity, SessionCredentials]:
        tenant_id = tenant_id or LOCAL_TENANT_ID
        try:
            normalized = normalize_username(username)
            user = self.database.get_user_by_username(tenant_id, normalized)
        except ValueError:
            user = None
        now = self.clock()
        ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest() if ip else "unknown"
        ip_key = f"source:{tenant_id}:{ip_hash}"
        if self.limiter.count(ip_key, now) >= self.ip_max_failures:
            self.database.record_auth_event("login_failed", tenant_id=tenant_id, user_id=user.id if user else None, metadata={"reason": "rate_limited"})
            raise AuthenticationError("invalid credentials")
        if user is None or user.status != "active":
            self.limiter.increment(ip_key, now)
            self.database.record_auth_event("login_failed", tenant_id=tenant_id, metadata={"reason": "invalid_credentials"})
            raise AuthenticationError("invalid credentials")
        if user.locked_until is not None and self._utc(user.locked_until) > now:
            self.database.record_auth_event("login_failed", tenant_id=tenant_id, user_id=user.id, metadata={"reason": "locked"})
            raise AuthenticationError("invalid credentials")
        if not verify_password(password, user.password_hash):
            self.limiter.increment(ip_key, now)
            account_key = f"account:{tenant_id}:{user.id}"
            failures = self.limiter.increment(account_key, now)
            locked_until = now + timedelta(seconds=self.lock_seconds) if failures >= self.max_failures else None
            self.database.update_login_failure(user.id, failures, locked_until)
            self.database.record_auth_event("login_failed", tenant_id=tenant_id, user_id=user.id, metadata={"reason": "invalid_credentials"})
            raise AuthenticationError("invalid credentials")

        self.limiter.clear(f"account:{tenant_id}:{user.id}")
        self.database.mark_login_success(user.id, now)
        token = secrets.token_urlsafe(48)
        csrf_token = self.session_csrf(token)
        expires_at = now + self.session_ttl
        session = self.database.create_auth_session(
            user_id=user.id,
            token_hash=self.token_digest(token),
            csrf_hash=self.csrf_digest(csrf_token),
            created_at=now,
            expires_at=expires_at,
        )
        identity = AuthIdentity(
            session_id=session.id,
            user_id=user.id,
            tenant_id=user.tenant_id,
            username=user.username,
            role=user.role,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )
        self.database.record_auth_event("login_succeeded", tenant_id=user.tenant_id, user_id=user.id, metadata={"session_id": session.id})
        return identity, SessionCredentials(session.id, token, csrf_token, expires_at)

    def validate_session(self, token: str) -> AuthIdentity | None:
        if not token:
            return None
        session = self.database.get_active_auth_session(self.token_digest(token))
        if session is None:
            return None
        user = self.database.get_user(session.user_id)
        if user is None or user.status != "active":
            return None
        return AuthIdentity(
            session_id=session.id,
            user_id=user.id,
            tenant_id=user.tenant_id,
            username=user.username,
            role=user.role,
            csrf_token="",
            expires_at=session.expires_at,
        )

    def validate_csrf(self, session_id: str, token: str) -> bool:
        if not session_id or not token:
            return False
        session = self.database.get_auth_session(session_id)
        if session is None or session.revoked_at is not None or self._utc(session.expires_at) <= datetime.now(UTC):
            return False
        return secrets.compare_digest(session.csrf_hash, self.csrf_digest(token))

    def issue_csrf(self, session_id: str, session_token: str) -> str:
        session = self.database.get_active_auth_session(self.token_digest(session_token))
        if session is None or session.id != session_id:
            raise AuthenticationError("invalid session")
        token = self.session_csrf(session_token)
        if not secrets.compare_digest(session.csrf_hash, self.csrf_digest(token)):
            self.database.rotate_auth_csrf(session_id, self.csrf_digest(token))
        return token

    def logout(self, token: str) -> None:
        session = self.database.get_active_auth_session(self.token_digest(token)) if token else None
        if session is not None:
            self.database.revoke_auth_session(session.id)
            user = self.database.get_user(session.user_id)
            self.database.record_auth_event("logout", tenant_id=user.tenant_id if user else None, user_id=session.user_id, metadata={"session_id": session.id})

    def revoke_all(self, user_id: str) -> int:
        return self.database.revoke_user_sessions(user_id)

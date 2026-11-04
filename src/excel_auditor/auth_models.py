from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError


class UserRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


PASSWORD_HASHER = PasswordHasher()


def normalize_username(value: str) -> str:
    normalized = " ".join(value.strip().split()).casefold()
    if not normalized or len(normalized) > 200:
        raise ValueError("username must contain 1 to 200 characters")
    return normalized


def hash_password(password: str) -> str:
    if len(password) < 8 or len(password) > 1024:
        raise ValueError("password must contain 8 to 1024 characters")
    return PASSWORD_HASHER.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(encoded_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


@dataclass(frozen=True)
class SessionCredentials:
    session_id: str
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthIdentity:
    session_id: str
    user_id: str
    tenant_id: str
    username: str
    role: str
    csrf_token: str
    expires_at: datetime

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .auth_models import UserRole
from .auth_service import AuthService
from .persistence import DatabaseRepository


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=1024)
    role: UserRole = UserRole.VIEWER


class UpdateUserRequest(BaseModel):
    role: UserRole | None = None


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=1024)


def _public_user(database: DatabaseRepository, user, *, include_tenant: bool = True, include_quota: bool = True) -> dict[str, str | int | None | dict[str, int]]:
    payload = {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "status": user.status,
        "failed_login_count": user.failed_login_count,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat(),
    }
    if include_tenant:
        payload["tenant_id"] = user.tenant_id
    if include_quota:
        payload["quota"] = database.get_user_quota(user.id)
    return payload


def build_router(database: DatabaseRepository, auth_service: AuthService, *, prefix: str = "/api/v1/users") -> APIRouter:
    router = APIRouter(prefix=prefix)

    def require_admin(request: Request) -> None:
        if not getattr(request.state, "authenticated", False) or request.state.auth_method != "session":
            raise HTTPException(status_code=403, detail="administrator role required")
        if request.state.role not in {UserRole.OWNER.value, UserRole.ADMIN.value}:
            raise HTTPException(status_code=403, detail="administrator role required")

    def target_user(request: Request, user_id: str):
        user = database.get_user(user_id)
        if user is None or user.tenant_id != request.state.tenant_id:
            raise HTTPException(status_code=404, detail="user not found")
        return user

    @router.get("")
    def list_users(request: Request, tenant_id: str | None = None):
        require_admin(request)
        if tenant_id is not None and tenant_id != request.state.tenant_id:
            raise HTTPException(status_code=403, detail="tenant access denied")
        users = database.list_users(request.state.tenant_id)
        if prefix.endswith("/operators"):
            users = [user for user in users if user.role in {UserRole.OPERATOR.value, UserRole.VIEWER.value}]
        return {"items": [_public_user(database, user, include_tenant=not prefix.endswith("/operators"), include_quota=not prefix.endswith("/operators")) for user in users]}

    @router.post("", status_code=201)
    def create_user(request: Request, body: CreateUserRequest):
        require_admin(request)
        if prefix.endswith("/operators") and body.role not in {UserRole.OPERATOR, UserRole.VIEWER}:
            raise HTTPException(status_code=422, detail="operator role must be operator or viewer")
        if body.role == UserRole.OWNER and request.state.role != UserRole.OWNER.value:
            raise HTTPException(status_code=403, detail="owner role required")
        try:
            if prefix.endswith("/operators"):
                user = database.create_operator(
                    username=body.username,
                    password=body.password,
                    role=body.role.value,
                )
            else:
                user = database.create_user(tenant_id=request.state.tenant_id, username=body.username, password=body.password, role=body.role.value)
        except ValueError as exc:
            if str(exc) == "username already exists":
                raise HTTPException(status_code=409, detail="username already exists") from exc
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _public_user(database, user, include_tenant=not prefix.endswith("/operators"), include_quota=not prefix.endswith("/operators"))

    @router.patch("/{user_id}")
    def update_user(request: Request, user_id: str, body: UpdateUserRequest):
        require_admin(request)
        target = target_user(request, user_id)
        if body.role is not None:
            if body.role == UserRole.OWNER and request.state.role != UserRole.OWNER.value:
                raise HTTPException(status_code=403, detail="owner role required")
            if prefix.endswith("/operators") and body.role not in {UserRole.OPERATOR, UserRole.VIEWER}:
                raise HTTPException(status_code=422, detail="operator role must be operator or viewer")
            database.update_user_role(user_id, body.role.value)
        updated = database.get_user(target.id)
        database.record_auth_event("operator.updated", tenant_id="local", user_id=target.id, metadata={"role": updated.role})
        return _public_user(database, updated, include_tenant=not prefix.endswith("/operators"), include_quota=not prefix.endswith("/operators"))

    @router.post("/{user_id}/reset-password")
    def reset_password(request: Request, user_id: str, body: ResetPasswordRequest):
        require_admin(request)
        target_user(request, user_id)
        database.set_user_password(user_id, body.password)
        auth_service.revoke_all(user_id)
        database.record_auth_event("operator.password_reset", tenant_id="local", user_id=user_id)
        return {"id": user_id, "status": "password_reset"}

    @router.post("/{user_id}/disable")
    def disable_user(request: Request, user_id: str):
        require_admin(request)
        target = target_user(request, user_id)
        if target.id == request.state.user_id:
            raise HTTPException(status_code=409, detail="cannot disable current user")
        database.set_user_status(user_id, "disabled")
        auth_service.revoke_all(user_id)
        database.record_auth_event("operator.disabled", tenant_id="local", user_id=user_id)
        return _public_user(database, database.get_user(user_id), include_tenant=not prefix.endswith("/operators"), include_quota=not prefix.endswith("/operators"))

    @router.post("/{user_id}/enable")
    def enable_user(request: Request, user_id: str):
        require_admin(request)
        target_user(request, user_id)
        database.set_user_status(user_id, "active")
        database.record_auth_event("operator.enabled", tenant_id="local", user_id=user_id)
        return _public_user(database, database.get_user(user_id), include_tenant=not prefix.endswith("/operators"), include_quota=not prefix.endswith("/operators"))

    return router

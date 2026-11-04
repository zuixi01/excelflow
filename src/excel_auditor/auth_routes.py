from __future__ import annotations

import os
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth_service import AuthenticationError, AuthService


class LoginRequest(BaseModel):
    # Accepted only as a compatibility shim for old API clients; the web UI never sends it.
    tenant_id: str | None = Field(default=None, min_length=1, max_length=200)
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


def build_router(auth_service: AuthService, *, auth_required: bool) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth")
    ttl = auth_service.session_ttl
    secure_cookie = os.environ.get("EXCEL_AUDITOR_ENV", "development").lower() in {"production", "prod"}

    def no_store(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"

    @router.post("/login")
    def login(body: LoginRequest, request: Request) -> JSONResponse:
        try:
            identity, credentials = auth_service.login(
                body.username,
                body.password,
                tenant_id=body.tenant_id,
                ip=request.client.host if request.client else "",
                user_agent=request.headers.get("user-agent", ""),
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail="invalid credentials") from exc
        response = JSONResponse(content={
            "authenticated": True,
            "auth_required": auth_required,
            "user_id": identity.user_id,
            "username": identity.username,
            "role": identity.role,
            "expires_at": identity.expires_at.isoformat(),
        })
        response.set_cookie(
            "excel_flow_session",
            credentials.token,
            max_age=int(ttl.total_seconds()),
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path="/",
        )
        no_store(response)
        return response

    @router.get("/session")
    def session(request: Request) -> JSONResponse:
        if not getattr(request.state, "authenticated", False):
            raise HTTPException(status_code=401, detail="authentication required")
        payload = {
            "authenticated": True,
            "auth_required": auth_required,
            "user_id": request.state.user_id,
            "username": request.state.username,
            "role": request.state.role,
            "auth_method": request.state.auth_method,
            "expires_at": request.state.session_expires_at.isoformat() if request.state.session_expires_at else None,
        }
        # Keep the service-to-service identity visible for legacy bearer clients;
        # browser sessions intentionally expose only the administrator account.
        if request.state.auth_method == "bearer":
            payload["tenant_id"] = request.state.tenant_id
        response = JSONResponse(content=payload)
        no_store(response)
        return response

    @router.get("/csrf")
    def csrf(request: Request) -> JSONResponse:
        if getattr(request.state, "auth_method", "") != "session":
            raise HTTPException(status_code=403, detail="browser session required")
        token = auth_service.issue_csrf(request.state.auth_session_id, request.state.session_token)
        response = JSONResponse(content={"csrf_token": token})
        no_store(response)
        return response

    @router.post("/logout", status_code=204)
    def logout(request: Request) -> Response:
        token = getattr(request.state, "session_token", "")
        auth_service.logout(token)
        response = Response(status_code=204)
        response.delete_cookie("excel_flow_session", path="/")
        no_store(response)
        return response

    return router

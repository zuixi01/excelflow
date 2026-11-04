from __future__ import annotations

import hashlib
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import Field

from .field_reconciliation import FieldReconciliationService, read_json, write_json
from .models import StrictModel
from .strict_serialization import dump_json_exact, load_json_strict
from .workflow.routes import guard_origin
from .permissions import creator_scope, with_creators

LiteralKind = Literal["excel", "report", "snapshot"]


@contextmanager
def mutation_lock(directory: Path):
    lock = directory / "field-mutation.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise HTTPException(409, "任务正在更新，请刷新后重试。")
    os.close(descriptor)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def archive_report(directory: Path, revision: int):
    for filename in ("field-reconciliation-report.json", "field-reconciliation-result.xlsx"):
        path = directory / filename
        if path.is_file():
            previous = directory / f"revision-{revision}-{filename}"
            if not previous.exists():
                shutil.copyfile(path, previous)


class MappingDecision(StrictModel):
    sheet: str = Field(min_length=1, max_length=128)
    column: int = Field(ge=1, le=200)
    field_id: str | None = None


class ConfirmMappings(StrictModel):
    base_revision: int = Field(ge=0)
    decisions: list[MappingDecision] = Field(min_length=1, max_length=1000)


def build_router(data_root: Path, get_service, get_managed, get_queue, *, auth_database=None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/field-reconciliations", dependencies=[Depends(guard_origin)])

    def authorized(request: Request, job_id: str):
        try:
            current = get_service().status(job_id)
        except (FileNotFoundError, OSError):
            raise HTTPException(404, "任务不存在。")
        if current.get("tenant_id") != request.state.tenant_id or current.get("deleted_at"):
            raise HTTPException(404, "任务不存在。")
        if getattr(request.state, "role", "") not in {"owner", "admin", "service"} and current.get("user_id") != request.state.user_id:
            raise HTTPException(404, "任务不存在。")
        if current.get("workflow") != "field_reconciliation":
            raise HTTPException(409, "任务类型不匹配。")
        return current

    def public(current):
        result = {key: value for key, value in current.items() if key not in {"tenant_id", "user_id", "trace_id", "object_keys"}}
        return {**result, "created_by_id": current.get("user_id")}

    def dispatch(background, current):
        service = get_service()
        directory = service.job_directory(current["job_id"])
        path = directory / current["input_name"]
        try:
            queue = get_queue()
            if queue:
                queue.enqueue_field_reconciliation(data_root, current["job_id"], path, current["connection_id"], current["scope"], current["tenant_id"], revision=current.get("revision", 0))
            else:
                background.add_task(FieldReconciliationService(service, get_managed(), data_root / "workflow").run,
                                    current["job_id"], path, current["connection_id"], current["scope"], current["tenant_id"])
        except Exception:
            service._write_status(current["job_id"], {**current, "status": "failed", "error_message_safe": "任务队列暂不可用，请重试。"})
            raise HTTPException(503, "任务队列暂不可用，请重试。")

    @router.get("/connections")
    def connections(request: Request):
        managed = get_managed()
        items = []
        for connection in managed.registry.connections.values() if managed else []:
            config = connection.field_reconciliation
            if config and request.state.tenant_id in config.tenant_ids:
                items.append({"id": connection.id, "title": config.title,
                              "scope_fields": sorted(set(config.parameter_mapping.values()))})
        return {"items": items}

    @router.get("")
    def tasks(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        items = []
        creator = creator_scope(request)
        for path in get_service().jobs.glob("job_*/status.json"):
            try:
                current = read_json(path)
            except (OSError, ValueError):
                continue
            if current.get("tenant_id") == request.state.tenant_id and current.get("workflow") == "field_reconciliation" and not current.get("deleted_at"):
                if creator is not None and current.get("user_id") != creator:
                    continue
                items.append(public(current))
        items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        start = (page - 1) * page_size
        return {"items": with_creators(items[start:start + page_size], request.state.tenant_id, auth_database), "total": len(items), "page": page}

    @router.post("", status_code=202)
    async def create(request: Request, background: BackgroundTasks, excel_file: UploadFile = File(...),
                     connection_id: str = Form(...), scope_json: str = Form("{}"),
                     idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
        suffix = Path(excel_file.filename or "input.xlsx").suffix.lower()
        if suffix not in {".xlsx", ".xlsm"}:
            raise HTTPException(415, "支持 .xlsx 或 .xlsm 文件。")
        managed = get_managed()
        if not managed:
            raise HTTPException(503, "尚未配置平台字段接口，请联系管理员。")
        try:
            connection = managed.registry.get(connection_id)
        except KeyError:
            raise HTTPException(422, "所选平台来源不可用。")
        config = connection.field_reconciliation
        if config is None or request.state.tenant_id not in config.tenant_ids:
            raise HTTPException(403, "无权使用所选平台来源。")
        try:
            if len(scope_json.encode()) > 32 * 1024:
                raise ValueError("scope too large")
            scope = load_json_strict(scope_json)
            allowed = set(config.parameter_mapping.values())
            if not isinstance(scope, dict) or set(scope) != allowed or any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in scope.values()):
                raise ValueError("invalid scope")
        except ValueError:
            raise HTTPException(422, "请填写所选来源要求的商户或业务范围。")
        content = bytearray()
        while chunk := await excel_file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > 50 * 1024 * 1024:
                raise HTTPException(413, "Excel 不能超过 50 MiB。")
        fingerprint = hashlib.sha256(b"field-reconciliation\0" + connection_id.encode() + dump_json_exact(scope, sort_keys=True).encode() + suffix.encode() + content).hexdigest()
        service = get_service()
        try:
            job_id, replayed = service.create_or_get_job(idempotency_key, fingerprint, request.state.tenant_id, request.state.user_id, request.state.trace_id)
        except FileExistsError:
            raise HTTPException(409, "提交标识已用于另一份文件，请重新提交。")
        except ValueError:
            raise HTTPException(429, "任务数量已达上限，请等待当前任务完成。")
        if replayed:
            return public(service.status(job_id))
        filename = (excel_file.filename or "supplier.xlsx").replace("\\", "/").split("/")[-1][:150]
        service.record_input_metadata(job_id, filename, len(content))
        input_name = "field-input" + suffix
        (service.job_directory(job_id) / input_name).write_bytes(content)
        current = {**service.status(job_id), "workflow": "field_reconciliation", "connection_id": connection_id,
                   "scope": scope, "revision": 0, "input_name": input_name}
        service._write_status(job_id, current)
        dispatch(background, current)
        return public(current)

    def report(request, job_id):
        current = authorized(request, job_id)
        if current["status"] not in {"completed", "manual_review"}:
            raise HTTPException(409, "任务尚未生成可用报告。")
        return read_json(get_service().artifact(job_id, "field_report"))

    @router.get("/{job_id}")
    def task(request: Request, job_id: str):
        return with_creators([public(authorized(request, job_id))], request.state.tenant_id, auth_database)[0]

    @router.get("/{job_id}/mappings")
    def mappings(request: Request, job_id: str):
        data = report(request, job_id)
        return {"items": data["mappings"], "fields": data["fields"]}

    @router.get("/{job_id}/issues")
    def issues(request: Request, job_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), severity: str = ""):
        data = report(request, job_id)
        items = [item for item in data["issues"] if not severity or item["severity"] == severity]
        start = (page - 1) * page_size
        return Response(dump_json_exact({"items": items[start:start + page_size], "total": len(items)}, ensure_ascii=False), media_type="application/json")

    @router.get("/{job_id}/files/{kind}")
    def file(request: Request, job_id: str, kind: LiteralKind):
        current = authorized(request, job_id)
        if kind != "snapshot" and current["status"] not in {"completed", "manual_review"}:
            raise HTTPException(409, "任务尚未生成当前版本的结果。")
        try:
            path = get_service().artifact(job_id, "field_" + kind)
        except FileNotFoundError:
            raise HTTPException(404, "文件尚未生成。")
        return FileResponse(path, filename=path.name)

    @router.post("/{job_id}/mappings/confirm", status_code=202)
    def confirm(request: Request, job_id: str, body: ConfirmMappings, background: BackgroundTasks):
        current = authorized(request, job_id)
        service = get_service()
        directory = service.job_directory(job_id)
        with mutation_lock(directory):
            current = authorized(request, job_id)
            if current.get("revision", 0) != body.base_revision or current["status"] not in {"completed", "manual_review"}:
                raise HTTPException(409, "任务版本已变化，请刷新后再确认。")
            data = report(request, job_id)
            field_ids = {field["id"] for field in data["fields"]}
            selected = {(item["sheet"], item["column"]): dict(item) for item in data["mappings"]}
            if len({(item.sheet, item.column) for item in body.decisions}) != len(body.decisions):
                raise HTTPException(422, "同一列不能重复确认。")
            for decision in body.decisions:
                key = (decision.sheet, decision.column)
                if key not in selected or decision.field_id is not None and decision.field_id not in field_ids:
                    raise HTTPException(422, "确认包含未知列或平台字段。")
                selected[key].update(field_id=decision.field_id, status="accepted" if decision.field_id else "ignored")
            occupied = [(item["sheet"], item["field_id"]) for item in selected.values() if item["status"] == "accepted"]
            if len(set(occupied)) != len(occupied):
                raise HTTPException(422, "同一工作表的多列不能指向同一平台字段。")
            previous = read_json(directory / "field-decisions.json") if (directory / "field-decisions.json").exists() else []
            decisions = {(item["sheet"], item["column"]): item for item in previous}
            for item in body.decisions:
                decisions[(item.sheet, item.column)] = {**item.model_dump(), "actor": request.state.user_id}
            archive_report(directory, body.base_revision)
            write_json(directory / "field-decisions.json", list(decisions.values()))
            current = {**current, "revision": body.base_revision + 1, "status": "queued", "progress": 0, "artifacts": {}, "object_keys": {}}
            service._write_status(job_id, current)
            dispatch(background, current)
            return public(current)

    @router.post("/{job_id}/retry", status_code=202)
    def retry(request: Request, job_id: str, background: BackgroundTasks):
        authorized(request, job_id)
        directory = get_service().job_directory(job_id)
        with mutation_lock(directory):
            current = authorized(request, job_id)
            if current["status"] not in {"failed", "cancelled", "manual_review"}:
                raise HTTPException(409, "当前任务无需重试。")
            archive_report(directory, current.get("revision", 0))
            (directory / "cancel.requested").unlink(missing_ok=True)
            current = {**current, "status": "queued", "progress": 0, "revision": current.get("revision", 0) + 1, "artifacts": {}, "object_keys": {}}
            get_service()._write_status(job_id, current)
            dispatch(background, current)
            return public(current)

    return router

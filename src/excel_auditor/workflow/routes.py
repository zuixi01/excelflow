from __future__ import annotations

import os
import re
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, Response, JSONResponse
from pydantic import Field
from sqlalchemy import select
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ..models import StrictModel
from ..permissions import can_manage, creator_scope
from .ai import AIClient, AISettings
from .excel import MAX_UPLOAD
from .limits import MAX_UPLOAD_MIB
from .models import EditRequest, MappingRequest, RevisionRequest, TemplateEdit, WorkflowError, FlexibleReturnRequest
from .repository import WorkflowStore, objects
from .uploads import receive
from .tasks import TaskService, maintenance_download_name
from .templates import TemplateService
from .field_profiles import FieldProfile, FieldProfileService, ProfileEdit, NewFieldProfile, compile_profile
from .platform_fields import PlatformFieldRequest, DirectPlatformSettings, DirectPlatformConnection


class NewTemplate(StrictModel):
    source_id: str | None = None
    name: str = Field(default="新业务模板", min_length=1, max_length=100)


class DeleteTemplates(StrictModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class DeleteTasks(StrictModel):
    ids: list[str] = Field(min_length=1, max_length=100)


class LearnRequest(RevisionRequest):
    instructions: str = Field(default="", max_length=6000)


class ReturnRequest(StrictModel):
    preview_id: str
    delete_missing: bool = True
    acknowledge_identity_warnings: bool = False
    confirm_structure: bool = False
    confirm_stale: bool = False


class ExportRequest(RevisionRequest):
    output_template_id: str | None = Field(default=None, min_length=1, max_length=64)


class NameConfirmationApply(StrictModel):
    preview_id: str = Field(min_length=1, max_length=64)


class AnalyzeMappingRequest(RevisionRequest):
    # Accepted for older clients; matching no longer depends on operator category selection.
    categories: dict[str, str] = Field(default_factory=dict, max_length=50)


def guard_origin(request: Request):
    if request.method not in {"GET", "HEAD", "OPTIONS"} and (origin := request.headers.get("origin")):
        origin_host = urlsplit(origin).hostname
        expected = request.url.hostname
        if origin_host != expected and not {origin_host, expected} <= {"localhost", "127.0.0.1", "::1"}:
            raise WorkflowError("请求来源不匹配，请从本项目页面操作。", 403)


def require_admin(request: Request):
    if not can_manage(request):
        raise WorkflowError("当前账户没有模板发布或模型配置权限。", 403)


def build_router(data_root: Path, database=None, task_queue=None, *, auth_database=None,
                 inline_executor_for_tests: bool = False) -> APIRouter:
    instance = None
    initialization_lock = threading.Lock()
    # Normal API processes only enqueue work. Tests may explicitly inject an
    # in-process executor to keep isolated ASGI fixtures self-contained.
    local_operations = (ThreadPoolExecutor(max_workers=2, thread_name_prefix='workflow-operation')
                        if inline_executor_for_tests else None)

    def store():
        nonlocal instance
        with initialization_lock:
            if instance is None:
                instance = WorkflowStore(data_root / "workflow", database.engine if database else None)
        return instance

    def tasks():
        return TaskService(store(), auth_database=auth_database or database)

    def templates():
        return TemplateService(store())

    def asynchronous(request):
        # The deployed API never performs expensive workflow work inline, even
        # when an older client omits Prefer: respond-async.
        return (not inline_executor_for_tests or os.environ.get('EXCEL_AUDITOR_ENV', '').lower() in {'production', 'prod'}
                or 'respond-async' in request.headers.get('prefer', ''))

    def operation(request, task_id, name, args=None, content=None):
        from .operations import OperationService
        from .execution import ExecutionService, execute_isolated
        result = OperationService(store()).create(task_id, request.state.tenant_id, request.state.user_id,
            name, args, content, request.headers.get('idempotency-key'))
        if task_queue is not None or local_operations is not None:
            for execution_id in ExecutionService(store()).dispatch(task_queue, object_id=result['id']):
                if local_operations is not None:
                    local_operations.submit(execute_isolated, store(), execution_id)
        return JSONResponse(result, status_code=202, headers={
            'Location': f'/api/v1/workflow-tasks/{task_id}/operations/{result["id"]}', 'Retry-After': '2'})

    def authorize_task(request: Request):
        task_id = request.path_params.get("task_id")
        if task_id is None:
            return
        with store().engine.connect() as con:
            task = store().get(con, task_id, request.state.tenant_id, "task")
        creator_id = creator_scope(request)
        if creator_id is not None and task["payload"].get("actor") != creator_id:
            raise WorkflowError("记录不存在或无权访问", 404, "NOT_FOUND")

    async def upload_context(request: Request):
        # Standalone router users (including isolated test apps) need the same staging lifecycle.
        if 'multipart/form-data' not in request.headers.get('content-type', '') or hasattr(request.state, 'upload_directory'):
            yield
            return
        import tempfile
        from .capacity import ResourceLease, disk_check, limit
        await run_in_threadpool(disk_check, store().root)
        lease = await run_in_threadpool(ResourceLease(store(), 'upload', limit('MAX_UPLOADS', 16)).acquire)
        try:
            with tempfile.TemporaryDirectory(prefix='upload-', dir=store().root) as directory:
                request.state.upload_directory = directory
                yield
        finally:
            await run_in_threadpool(lease.close)

    async def interactive_capacity(request: Request):
        # Legacy synchronous clients and template trials must not bypass memory admission.
        path = request.url.path
        task_heavy = ('/workflow-tasks/' in path and '/operations/' not in path and
            (request.method in {'POST', 'PATCH'} or path.endswith('/maintenance/download')))
        trial = '/workflow-templates/' in path and path.endswith('/trial')
        if not trial and (not task_heavy or asynchronous(request)):
            yield
            return
        from .capacity import ResourceLease, disk_check
        await run_in_threadpool(disk_check, store().root)
        lease = await run_in_threadpool(ResourceLease(store(), 'interactive', 1).acquire)
        try:
            yield
            lease.check()
        finally:
            await run_in_threadpool(lease.close)

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(guard_origin), Depends(authorize_task), Depends(upload_context), Depends(interactive_capacity)])

    @router.get("/field-profile")
    def field_profile(request: Request):
        return FieldProfileService(store()).get(request.state.tenant_id)

    @router.get('/field-profile/platform-connections', dependencies=[Depends(require_admin)])
    def platform_field_connections(request: Request):
        from .platform_fields import available
        return available(request.state.tenant_id)

    @router.post('/field-profile/platform-preview', dependencies=[Depends(require_admin)])
    def platform_field_preview(request: Request, body: PlatformFieldRequest):
        from .platform_fields import preview
        return preview(request.state.tenant_id, body.connection_id)

    @router.get('/field-profile/platform-direct', dependencies=[Depends(require_admin)])
    def platform_direct_settings(request: Request):
        return DirectPlatformConnection(store().root, request.state.tenant_id).public()

    @router.put('/field-profile/platform-direct', dependencies=[Depends(require_admin)])
    def save_platform_direct_settings(request: Request, body: DirectPlatformSettings):
        return DirectPlatformConnection(store().root, request.state.tenant_id).save(body)

    @router.post('/field-profile/platform-direct/preview', dependencies=[Depends(require_admin)])
    def preview_platform_direct_settings(request: Request):
        return DirectPlatformConnection(store().root, request.state.tenant_id).preview(request.state.tenant_id)

    @router.put("/field-profile", dependencies=[Depends(require_admin)])
    def save_profile(request: Request, body: ProfileEdit):
        return FieldProfileService(store()).save(request.state.tenant_id, body)

    @router.post("/field-profile/preview", dependencies=[Depends(require_admin)])
    def preview_profile(body: FieldProfile):
        config = compile_profile(body)
        return {"fields": sum(not field.hidden for field in config.fields), "outputs": [f.model_dump(mode="json") for f in config.outputs]}

    @router.post("/field-profile/publish", dependencies=[Depends(require_admin)])
    def publish_profile(request: Request, body: RevisionRequest):
        return FieldProfileService(store()).publish(request.state.tenant_id, body.base_revision)

    @router.get("/field-profiles")
    def list_field_profiles(request: Request):
        return FieldProfileService(store()).list(request.state.tenant_id)

    @router.post("/field-profiles", dependencies=[Depends(require_admin)], status_code=201)
    def create_field_profile(request: Request, body: NewFieldProfile):
        return FieldProfileService(store()).create(request.state.tenant_id, body)

    @router.get("/field-profiles/{profile_id}")
    def get_field_profile(request: Request, profile_id: str):
        return FieldProfileService(store()).get(request.state.tenant_id, profile_id)

    @router.put("/field-profiles/{profile_id}", dependencies=[Depends(require_admin)])
    def save_field_profile(request: Request, profile_id: str, body: ProfileEdit):
        return FieldProfileService(store()).save(request.state.tenant_id, body, profile_id)

    @router.post("/field-profiles/{profile_id}/publish", dependencies=[Depends(require_admin)])
    def publish_field_profile(request: Request, profile_id: str, body: RevisionRequest):
        return FieldProfileService(store()).publish(request.state.tenant_id, body.base_revision, profile_id)

    @router.post("/standardization-tasks", status_code=202)
    async def create_standardization(request: Request, background: BackgroundTasks, excel_file: list[UploadFile] = File(...),
                                     name: str = Form(""), field_profile_id: str | None = Form(None),
                                     idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
        from .source_files import MAX_SOURCE_FILES, clean_filename, validate_uploads
        if not 1 <= len(excel_file) <= MAX_SOURCE_FILES:
            raise WorkflowError(f"每个任务请选择 1 至 {MAX_SOURCE_FILES} 个 Excel 文件。")
        files = []
        for upload in excel_file:
            files.append((clean_filename(upload.filename or "供应商.xlsx"), await receive(upload, request)))
            validate_uploads(files)
        obj, created = await run_in_threadpool(tasks().create_standardization_files, request.state.tenant_id, request.state.user_id,
                                              name[:100], files, idempotency_key, field_profile_id)
        if created:
            await run_in_threadpool(dispatch, background, "parse_task", obj["id"], request.state.tenant_id)
        return obj

    @router.post("/workflow-tasks/{task_id}/mapping/analyze")
    def analyze_mapping(request: Request, task_id: str, body: AnalyzeMappingRequest):
        if asynchronous(request):
            return operation(request, task_id, "mapping_analyze", body.model_dump(mode="json"))
        return tasks().analyze_mapping(task_id, request.state.tenant_id, request.state.user_id, body.base_revision, body.categories)

    @router.post("/workflow-tasks/{task_id}/mapping/upgrade")
    def upgrade_mapping(request: Request, task_id: str, body: RevisionRequest):
        if asynchronous(request):
            return operation(request, task_id, "mapping_upgrade", body.model_dump(mode="json"))
        return tasks().analyze_mapping(task_id, request.state.tenant_id, request.state.user_id, body.base_revision, upgrade_snapshot=True)

    def dispatch(background: BackgroundTasks, operation: str, object_id: str, tenant: str):
        from .execution import ExecutionService, execute_isolated
        if task_queue is not None or inline_executor_for_tests:
            for execution_id in ExecutionService(store()).dispatch(task_queue, object_id=object_id):
                if inline_executor_for_tests and task_queue is None:
                    background.add_task(execute_isolated, store(), execution_id)

    @asynccontextmanager
    async def recovery_lifespan(app):
        if not inline_executor_for_tests:
            yield
            return
        from .execution import ExecutionService, execute_isolated
        stopped = threading.Event()
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="workflow-local")

        def scan():
            import logging
            while not stopped.is_set():
                try:
                    for execution_id in ExecutionService(store()).dispatch(task_queue):
                        pool.submit(execute_isolated, store(), execution_id)
                except Exception:
                    logging.getLogger(__name__).exception("Workflow recovery scan failed")
                stopped.wait(float(os.environ.get("EXCEL_AUDITOR_RECOVERY_INTERVAL", "30")))

        thread = threading.Thread(target=scan, daemon=True, name="workflow-recovery")
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=5)
            pool.shutdown(wait=True)
            if local_operations is not None:
                local_operations.shutdown(wait=True)

    router.lifespan_context = recovery_lifespan

    @router.get('/workflow-operations/health', dependencies=[Depends(require_admin)])
    def workflow_health():
        from .maintenance_ops import health
        return health(store())

    @router.get('/workflow-tasks/{task_id}/operations/{operation_id}')
    def operation_status(request: Request, task_id: str, operation_id: str):
        from .operations import OperationService
        service = OperationService(store())
        return service.status(service.get(task_id, request.state.tenant_id, operation_id))

    @router.get('/workflow-tasks/{task_id}/operations/{operation_id}/result')
    def operation_result(request: Request, task_id: str, operation_id: str):
        from .operations import OperationService
        service = OperationService(store())
        obj = service.get(task_id, request.state.tenant_id, operation_id)
        result = service.result(obj)
        if result.get('download'):
            return FileResponse(store().directory(operation_id) / result['stored_file'], filename=result['filename'], headers={'Cache-Control': 'no-store'})
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @router.get("/workflow-definitions")
    def definitions(request: Request):
        return templates().definitions(request.state.tenant_id)

    @router.get("/workflow-examples/source")
    def example():
        from io import BytesIO
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = "商家商品"
        sheet.append(["商品名称", "货号", "商品分类", "商品品牌", "销售价", "库存", "销售单位"])
        sheet.append(["示例台灯", "0001", "照明", "示例品牌", "129.50", "20", "盏"])
        sheet.append(["示例吊灯", "0002", "照明", "示例品牌", "299.00", "12", "盏"])
        output = BytesIO()
        book.save(output)
        book.close()
        return Response(output.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @router.get("/workflow-tasks")
    def list_tasks(request: Request, search: str = "", status: str = "", offset: int = 0, limit: int = 30):
        return tasks().list(request.state.tenant_id, search[:200], status, max(0, offset), max(1, min(100, limit)),
                            creator_id=creator_scope(request))

    @router.delete("/workflow-tasks")
    def delete_tasks(request: Request, body: DeleteTasks):
        return tasks().delete(request.state.tenant_id, body.ids, creator_id=creator_scope(request))

    @router.post("/workflow-tasks", status_code=202)
    async def create_task(request: Request, background: BackgroundTasks, excel_file: UploadFile = File(...),
                          template_id: str = Form(...), name: str = Form(""), idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
        content = await receive(excel_file, request)
        filename = re.split(r"[/\\]", excel_file.filename or "原表.xlsx")[-1][:150]
        obj, created = await run_in_threadpool(tasks().create, request.state.tenant_id, request.state.user_id,
                                              template_id, name[:100], filename, content, idempotency_key)
        if created:
            await run_in_threadpool(dispatch, background, "parse_task", obj["id"], request.state.tenant_id)
        return obj

    @router.get("/workflow-tasks/{task_id}")
    def task(request: Request, task_id: str):
        return tasks().get(task_id, request.state.tenant_id)

    @router.post("/workflow-tasks/{task_id}/mapping")
    def mapping(request: Request, task_id: str, body: MappingRequest):
        if asynchronous(request):
            return operation(request, task_id, "mapping_confirm", body.model_dump(mode="json"))
        return tasks().confirm_mapping(task_id, request.state.tenant_id, request.state.user_id, body)

    @router.post("/workflow-tasks/{task_id}/mapping/reopen")
    def reopen_mapping(request: Request, task_id: str, body: RevisionRequest):
        return tasks().reopen_mapping(task_id, request.state.tenant_id, request.state.user_id, body.base_revision)

    @router.get("/workflow-tasks/{task_id}/maintenance")
    def maintenance(request: Request, task_id: str, offset: int = 0, limit: int = 30, search: str = "", issues_only: bool = False, sort: str = "", descending: bool = False):
        return tasks().rows(task_id, request.state.tenant_id, max(0, offset), max(1, min(100, limit)), search[:200], issues_only, sort, descending)

    @router.patch("/workflow-tasks/{task_id}/maintenance")
    def edit(request: Request, task_id: str, body: EditRequest):
        if asynchronous(request):
            return operation(request, task_id, "edit", body.model_dump(mode="json"))
        return tasks().edit(task_id, request.state.tenant_id, request.state.user_id, body)

    @router.post("/workflow-tasks/{task_id}/maintenance/validate")
    def validate(request: Request, task_id: str, body: RevisionRequest):
        if asynchronous(request):
            return operation(request, task_id, "edit", body.model_dump(mode="json"))
        return tasks().edit(task_id, request.state.tenant_id, request.state.user_id, EditRequest(base_revision=body.base_revision))

    @router.get("/workflow-tasks/{task_id}/maintenance/download")
    def download_maintenance(request: Request, task_id: str):
        if asynchronous(request):
            return operation(request, task_id, "maintenance_download")
        path = tasks().maintenance_file(task_id, request.state.tenant_id)
        return FileResponse(path, filename=maintenance_download_name(path), headers={"Cache-Control": "no-store"},
                            background=BackgroundTask(path.unlink, missing_ok=True))

    @router.post("/workflow-tasks/{task_id}/maintenance/returns")
    async def preview_return(request: Request, task_id: str, excel_file: UploadFile = File(...)):
        content = await receive(excel_file, request)
        if asynchronous(request):
            return await run_in_threadpool(operation, request, task_id, "return_preview", None, content)
        return await run_in_threadpool(tasks().preview_return, task_id, request.state.tenant_id, content)

    @router.post('/workflow-tasks/{task_id}/maintenance/name-confirmations')
    async def preview_names(request: Request, task_id: str, excel_file: UploadFile = File(...)):
        from .name_confirmation import preview
        content = await receive(excel_file, request)
        if asynchronous(request):
            return await run_in_threadpool(operation, request, task_id, "name_preview", None, content)
        return await run_in_threadpool(preview, tasks(), task_id, request.state.tenant_id, content)

    @router.post('/workflow-tasks/{task_id}/maintenance/name-confirmations/apply')
    def apply_names(request: Request, task_id: str, body: NameConfirmationApply):
        if asynchronous(request):
            return operation(request, task_id, "name_apply", body.model_dump(mode="json"))
        from .name_confirmation import apply
        return apply(tasks(), task_id, request.state.tenant_id, request.state.user_id, body.preview_id)

    @router.post('/workflow-tasks/{task_id}/maintenance/returns/inspect')
    async def inspect_return(request: Request, task_id: str, excel_file: UploadFile = File(...)):
        content = await receive(excel_file, request)
        if asynchronous(request):
            return await run_in_threadpool(operation, request, task_id, "return_inspect", None, content)
        return await run_in_threadpool(tasks().inspect_return, task_id, request.state.tenant_id, content)

    @router.post('/workflow-tasks/{task_id}/maintenance/returns/{preview_id}/preview')
    def preview_flexible_return(request: Request, task_id: str, preview_id: str, body: FlexibleReturnRequest):
        if asynchronous(request):
            return operation(request, task_id, "return_flexible", {"preview_id": preview_id, "body": body.model_dump(mode="json")})
        return tasks().preview_flexible_return(task_id, request.state.tenant_id, preview_id, body)

    @router.post("/workflow-tasks/{task_id}/maintenance/returns/apply")
    def apply_return(request: Request, task_id: str, body: ReturnRequest):
        if asynchronous(request):
            return operation(request, task_id, "return_apply", body.model_dump(mode="json"))
        return tasks().apply_return(task_id, request.state.tenant_id, request.state.user_id, body.preview_id,
                                    body.delete_missing, body.acknowledge_identity_warnings, body.confirm_structure, body.confirm_stale)

    @router.get('/workflow-tasks/{task_id}/maintenance/returns/{preview_id}/details')
    def return_details(request: Request, task_id: str, preview_id: str, kind: str = 'changed', offset: int = 0, limit: int = 10):
        from .return_details import page_details
        with store().engine.connect() as con:
            preview = store().get(con, preview_id, request.state.tenant_id, 'return')
        if preview['payload'].get('task_id') != task_id or preview['status'] not in {'row_review', 'ready', 'applied'}:
            raise WorkflowError('此回传预览不适用于当前任务。', 404)
        manifest = preview['payload'].get('detail_manifest')
        if not manifest:
            raise WorkflowError('此旧预览没有完整明细，请重新上传维护表。', 409)
        return page_details(store().directory(preview_id), manifest, kind, offset, limit)

    @router.post("/workflow-tasks/{task_id}/exports", status_code=202)
    def create_export(request: Request, background: BackgroundTasks, task_id: str, body: ExportRequest,
                      idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
        if asynchronous(request):
            return operation(request, task_id, 'export_prepare', body.model_dump(mode='json'))
        obj, created = tasks().create_export(task_id, request.state.tenant_id, body.base_revision,
                                             idempotency=idempotency_key, output_template_id=body.output_template_id)
        if created:
            dispatch(background, "export", obj["id"], request.state.tenant_id)
        return obj

    @router.get("/workflow-tasks/{task_id}/exports/{run_id}/files/{filename}")
    def artifact(request: Request, task_id: str, run_id: str, filename: str):
        service = tasks()
        path = service.export_file(task_id, run_id, request.state.tenant_id, filename)
        download_name = service.export_download_name(task_id, run_id, request.state.tenant_id, filename)
        return FileResponse(path, filename=download_name, headers={'Cache-Control': 'no-store'})

    @router.get('/workflow-tasks/{task_id}/images/{digest}')
    def task_image(request: Request, task_id: str, digest: str):
        path, info = tasks().image_file(task_id, request.state.tenant_id, digest)
        return FileResponse(path, media_type=info['mime_type'], headers={'Cache-Control': 'private, no-cache', 'X-Content-Type-Options': 'nosniff'})

    @router.get("/workflow-tasks/{task_id}/exports/{run_id}/preview/{filename}")
    def preview_output(request: Request, task_id: str, run_id: str, filename: str):
        from openpyxl import load_workbook
        from .excel import cell_text
        path = tasks().export_file(task_id, run_id, request.state.tenant_id, filename)
        if path.suffix != ".xlsx":
            raise WorkflowError("仅支持预览 Excel 文件。")
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            return {"name": tasks().export_download_name(task_id, run_id, request.state.tenant_id, filename), "sheets": [{"name": sheet.title,
                "rows": [[cell_text(cell) for cell in row] for row in sheet.iter_rows(max_row=min(12, sheet.max_row), values_only=True)]}
                for sheet in book.worksheets if sheet.sheet_state == "visible"]}
        finally:
            book.close()

    @router.post("/workflow-tasks/{task_id}/cancel")
    def cancel(request: Request, task_id: str, body: RevisionRequest):
        return tasks().cancel(task_id, request.state.tenant_id, body.base_revision)

    @router.post("/workflow-tasks/{task_id}/retry", status_code=202)
    def retry(request: Request, background: BackgroundTasks, task_id: str, body: RevisionRequest):
        with store().engine.begin() as con:
            obj = store().claim(con, task_id, request.state.tenant_id, body.base_revision)
            if obj["status"] not in {"failed", "cancelled"} or obj["payload"].get("record_count"):
                raise WorkflowError("当前任务无需重新解析。", 409)
            obj["status"] = "processing"
            obj["payload"].pop("error", None)
            store().save(con, obj)
        dispatch(background, "parse_task", task_id, request.state.tenant_id)
        return store().public(obj)

    @router.get("/workflow-templates")
    def list_templates(request: Request):
        return templates().list(request.state.tenant_id)

    @router.delete("/workflow-templates", dependencies=[Depends(require_admin)])
    def delete_templates(request: Request, body: DeleteTemplates):
        return templates().delete(request.state.tenant_id, body.ids)

    @router.post("/workflow-templates", dependencies=[Depends(require_admin)], status_code=201)
    def create_template(request: Request, body: NewTemplate):
        return templates().create(request.state.tenant_id, body.source_id, body.name)

    @router.post("/workflow-templates/import", dependencies=[Depends(require_admin)], status_code=202)
    async def import_template(request: Request, background: BackgroundTasks, excel_file: UploadFile = File(...), name: str = Form("新商品模板"), purpose: str = Form("output")):
        obj = await run_in_threadpool(templates().upload, request.state.tenant_id, name[:100], await receive(excel_file, request), purpose)
        await run_in_threadpool(dispatch, background, "parse_template", obj["id"], request.state.tenant_id)
        return obj

    @router.post("/workflow-templates/{template_id}/sources", dependencies=[Depends(require_admin)], status_code=202)
    async def attach_template_source(request: Request, background: BackgroundTasks, template_id: str, excel_file: UploadFile = File(...), purpose: str = Form(...)):
        obj = await run_in_threadpool(templates().attach_source, template_id, request.state.tenant_id, await receive(excel_file, request), purpose)
        await run_in_threadpool(dispatch, background, "parse_template", obj["id"], request.state.tenant_id)
        return obj

    @router.post("/workflow-templates/{template_id}/retry", dependencies=[Depends(require_admin)], status_code=202)
    def retry_template(request: Request, background: BackgroundTasks, template_id: str, body: RevisionRequest):
        obj = templates().retry(template_id, request.state.tenant_id, body.base_revision)
        dispatch(background, "parse_template", obj["id"], request.state.tenant_id)
        return obj

    @router.post("/workflow-templates/{template_id}/reparse", dependencies=[Depends(require_admin)], status_code=202)
    def reparse_template(request: Request, background: BackgroundTasks, template_id: str, body: RevisionRequest):
        obj = templates().reparse(template_id, request.state.tenant_id, body.base_revision)
        dispatch(background, "parse_template", obj["id"], request.state.tenant_id)
        return obj

    @router.get("/workflow-templates/{template_id}")
    def template(request: Request, template_id: str):
        return templates().get(template_id, request.state.tenant_id)

    @router.put("/workflow-templates/{template_id}", dependencies=[Depends(require_admin)])
    def update_template(request: Request, template_id: str, body: TemplateEdit):
        return templates().update(template_id, request.state.tenant_id, body)

    @router.post("/workflow-templates/{template_id}/learn", dependencies=[Depends(require_admin)], status_code=202)
    def learn(request: Request, background: BackgroundTasks, template_id: str, body: LearnRequest):
        obj = templates().ai_suggest(template_id, request.state.tenant_id, body.base_revision, body.instructions)
        dispatch(background, "learn", template_id, request.state.tenant_id)
        return obj

    @router.post("/workflow-templates/{template_id}/trial", dependencies=[Depends(require_admin)])
    async def trial(request: Request, template_id: str, base_revision: int = Form(...), sample_file: UploadFile | None = File(None)):
        content = await receive(sample_file, request) if sample_file else None
        return await run_in_threadpool(templates().trial, template_id, request.state.tenant_id, base_revision, content)

    @router.get("/workflow-templates/{template_id}/trial/{index}")
    def trial_file(request: Request, template_id: str, index: int):
        obj = templates().get(template_id, request.state.tenant_id)
        artifacts = obj["payload"].get("test_result", {}).get("artifacts", [])
        if index < 0 or index >= len(artifacts):
            raise WorkflowError("试运行文件不存在。", 404)
        return FileResponse(store().directory(template_id) / f"trial-{index}.xlsx", filename=artifacts[index]["name"])

    @router.post("/workflow-templates/{template_id}/publish", dependencies=[Depends(require_admin)])
    def publish(request: Request, template_id: str, body: RevisionRequest):
        return templates().publish(template_id, request.state.tenant_id, body.base_revision)

    @router.get("/workflow-settings")
    def settings(request: Request):
        from .limits import MAX_TOTAL_UPLOAD_MIB
        from .source_files import MAX_SOURCE_FILES
        return {"ai": AIClient(store().root, request.state.tenant_id).settings(),
                "can_manage_templates": can_manage(request),
                "limits": {"upload_mib": MAX_UPLOAD_MIB, "total_upload_mib": MAX_TOTAL_UPLOAD_MIB,
                           "source_files": MAX_SOURCE_FILES, "rows": 20000, "columns": 200},
                "storage": "database" if database else "local"}

    @router.put("/workflow-settings/ai", dependencies=[Depends(require_admin)])
    def save_ai(request: Request, body: AISettings):
        return AIClient(store().root, request.state.tenant_id).save(body)

    @router.post("/workflow-settings/ai/models", dependencies=[Depends(require_admin)])
    def models(request: Request):
        return AIClient(store().root, request.state.tenant_id).models()

    return router


def run_background(root: str, operation: str, object_id: str, tenant: str):
    from sqlalchemy import create_engine
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True) if os.environ.get("DATABASE_URL") else None
    store = WorkflowStore(Path(root), engine)
    try:
        function = {"parse_task": TaskService(store).parse, "export": TaskService(store).generate,
                    "parse_template": TemplateService(store).parse, "learn": TemplateService(store).learn}.get(operation)
        if function is None:
            raise WorkflowError("不支持的后台任务。")
        function(object_id, tenant)
    finally:
        store.engine.dispose()

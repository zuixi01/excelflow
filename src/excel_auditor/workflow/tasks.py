from __future__ import annotations

import hashlib
import json
import re
import shutil
import os
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, insert, select, update

from .excel import MAX_ROWS, MAX_CELLS, cell_text, discover, literal, read_maintenance, transform, validate_records, write_maintenance, write_output
from .models import EditRequest, MappingRequest, WorkflowConfig, WorkflowError, content_hash
from .uploads import digest, size, persist
from .repository import WorkflowStore, executions, identifier, now, objects, records, revisions
from .retained_columns import is_sequence_header, retain_source_columns, standard_fields_only
from .image_assets import copy_assets, find_asset, record_image_ids, snapshot_assets, validate_image_refs


def maintenance_download_name(path: Path) -> str:
    """Readable filename; the internal suffix isolates concurrent downloads."""
    return re.sub(r'_download_[a-f0-9]{32}(?=\.xlsx$)', '', path.name)


def final_download_name(task_name: str, generated_at: str | None = None) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', task_name).strip(' .')[:60] or '商品'
    if generated_at is None:
        return f'{name}_最终表_年月日时分秒.xlsx'
    try:
        moment = datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        # Some migrated exports have no usable creation timestamp. Keep them
        # readable instead of making the task detail/download fail entirely.
        try:
            moment = (datetime(int(generated_at), 1, 1, tzinfo=timezone.utc)
                      if re.fullmatch(r'\d{4}', generated_at or '') else datetime.now(timezone.utc))
        except ValueError:
            moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M%S')
    return f'{name}_最终表_{stamp}.xlsx'


def write_json(path: Path, value):
    descriptor, name = tempfile.mkstemp(suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def suggest_bindings(sheets, config):
    from ..product_workflow.fixed_template import normalize_header
    for sheet in sheets:
        suggested = [{"field": column["field"], "columns": [column["column"]], "operation": "copy", "argument": "", "part": 0}
                     for column in sheet["columns"] if column["field"]]
        notes = []
        for rule in config.import_rules:
            suggested = [binding for binding in suggested if binding["field"] != rule.field]
            positions = [[column["column"] for column in sheet["columns"] if normalize_header(column["header"]) == normalize_header(header)] for header in rule.headers]
            if any(len(position) != 1 for position in positions):
                notes.append(f"导入规则 {rule.field} 的来源表头未能唯一匹配，请手动确认。")
            else:
                suggested.append({"field": rule.field, "columns": [position[0] for position in positions],
                                  "operation": rule.operation, "argument": rule.argument, "part": rule.part})
        sheet["suggested_bindings"] = suggested
        sheet["mapping_notes"] = notes


class TaskService:
    def __init__(self, store: WorkflowStore, *, auth_database=None):
        self.store = store
        self.auth_database = auth_database

    def _with_creators(self, items: list[dict], tenant: str) -> list[dict]:
        from ..permissions import with_creators
        return with_creators(items, tenant, self.auth_database)

    def snapshot(self, task_id, tenant, base_revision, *, include_rows=False):
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, "task")
            rows = [dict(row) for row in con.execute(select(records).where(records.c.task_id == task_id)
                    .order_by(records.c.ordinal)).mappings()] if include_rows else []
            revision = con.execute(select(objects.c.revision).where(objects.c.id == task_id)).scalar_one()
            if task["revision"] != base_revision or revision != base_revision:
                raise WorkflowError("数据已有新版本，请刷新后重新应用修改；本次修改未覆盖新数据。", 409, "REVISION_CONFLICT")
        return task, rows

    def get(self, task_id: str, tenant: str) -> dict:
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, "task")
            history = con.execute(select(revisions.c.revision, revisions.c.actor, revisions.c.created_at, revisions.c.detail)
                                  .where(revisions.c.task_id == task_id).order_by(revisions.c.revision.desc()).limit(20)).mappings().all()
            exports = con.execute(select(objects).where(objects.c.tenant_id == tenant, objects.c.kind == "export",
                                  objects.c.parent_task_id == task_id)
                                  .order_by(objects.c.created_at.desc(), objects.c.id.desc())).mappings().all()
            operations = con.execute(select(objects.c.id, objects.c.status, objects.c.created_at,
                objects.c.payload['operation'].as_string().label('operation'),
                objects.c.payload['error'].as_string().label('error')).where(objects.c.tenant_id == tenant,
                objects.c.kind == 'operation', objects.c.parent_task_id == task_id)
                .order_by(objects.c.created_at.desc()).limit(12)).mappings().all()
        result = self.store.public(task)
        if task['payload'].get('workflow') == 'standardization':
            from .final_output import final_output_config
            output = final_output_config(WorkflowConfig.model_validate(task['payload']['config']),
                                         task['payload']['field_snapshot']['profile'])
            result['final_outputs'] = [file.model_dump(mode='json') for file in output.outputs]
        result["history"] = [dict(item) for item in history]
        result['operations'] = [dict(item) for item in operations]
        result["exports"] = [self.store.public(dict(item)) for item in exports if item["payload"].get("task_id") == task_id]
        for export in result['exports']:
            if export['status'] != 'completed':
                continue
            payload = export['payload']
            display = final_download_name(payload.get('task_name', task['payload']['name']),
                                          payload.get('completed_at') or export['created_at'])
            for artifact in payload.get('artifacts', []):
                if artifact['name'].lower().endswith('.xlsx'):
                    artifact['download_name'] = display
        if result.get('final_outputs'):
            current_export = next((run for run in result['exports'] if run['status'] == 'completed'
                                   and run['payload']['revision'] == task['revision']), None)
            for file in result['final_outputs']:
                artifact = next((item for item in current_export['payload'].get('artifacts', [])
                                 if item['name'] == file['name']), None) if current_export else None
                file['display_name'] = artifact['download_name'] if artifact else final_download_name(task['payload']['name'])
        self.with_executions([result, *result["exports"]], tenant)
        return self._with_creators([result], tenant)[0]

    def with_executions(self, items, tenant):
        active = {item["id"]: item for item in items if item["status"] == "processing"}
        if not active:
            return items
        with self.store.engine.connect() as con:
            rows = con.execute(select(executions.c.object_id, executions.c.revision, executions.c.status, executions.c.attempts)
                .where(executions.c.tenant_id == tenant, executions.c.object_id.in_(active)).order_by(executions.c.revision)).mappings()
            for row in rows:
                item = active[row["object_id"]]
                if row["revision"] == item["revision"]:
                    item["execution"] = {"status": row["status"], "attempts": row["attempts"]}
        return items

    def list(self, tenant: str, search: str = "", status: str = "", offset: int = 0, limit: int = 30,
             *, creator_id: str | None = None):
        with self.store.engine.connect() as con:
            payload = objects.c.payload
            timing = payload["processing_progress"]
            timing_keys = ("status", "stage", "ai_started_at", "ai_finished_at", "finished_at")
            query = select(
                objects.c.id, objects.c.status, objects.c.revision, objects.c.updated_at,
                payload["name"].as_string().label("name"), payload["filename"].as_string().label("filename"),
                func.coalesce(payload["field_snapshot"]["template_name"].as_string(),
                              payload["config"]["name"].as_string()).label("template_name"),
                func.coalesce(payload["record_count"].as_integer(), 0).label("record_count"),
                func.coalesce(payload["error_count"].as_integer(), 0).label("error_count"),
                func.coalesce(payload["error"].as_string(), "").label("error"),
                payload["actor"].as_string().label("created_by_id"),
                *(timing[key].as_string().label(f"timing_{key}") for key in timing_keys),
            ).where(objects.c.tenant_id == tenant, objects.c.kind == "task")
            if status:
                query = query.where(objects.c.status == status)
            if creator_id is not None:
                query = query.where(objects.c.creator_id == creator_id)
            if search:
                query = query.where(objects.c.search_summary.contains(search.casefold(), autoescape=True))
            total = con.execute(select(func.count()).select_from(query.subquery())).scalar_one()
            items = [dict(row) for row in con.execute(query.order_by(objects.c.updated_at.desc(), objects.c.id.desc())
                                                    .offset(offset).limit(limit)).mappings()]
            for item in items:
                values = {key: item.pop(f"timing_{key}") for key in timing_keys}
                item["ai_timing"] = values if values["status"] else None
        return {"items": self._with_creators(self.with_executions(items, tenant), tenant), "total": total}

    def delete(self, tenant: str, task_ids: list[str], *, creator_id: str | None = None):
        unique_ids = list(dict.fromkeys(task_ids))
        if len(unique_ids) != len(task_ids):
            raise WorkflowError("删除列表包含重复处理记录，请刷新后重试。")
        with self.store.engine.begin() as con:
            tasks = [self.store.get(con, task_id, tenant, "task") for task_id in unique_ids]
            if creator_id is not None and any(item["payload"].get("actor") != creator_id for item in tasks):
                raise WorkflowError("记录不存在或无权访问", 404, "NOT_FOUND")
            processing = [item["payload"].get("name", item["id"]) for item in tasks if item["status"] == "processing"]
            if processing:
                raise WorkflowError(f"以下处理记录仍在导入，完成后再删除：{'、'.join(processing)}", 409)
            related = [dict(row) for row in con.execute(select(objects).where(
                objects.c.tenant_id == tenant, objects.c.kind.in_(["export", "return", "nameconfirmation", "operation"]),
                objects.c.parent_task_id.in_(unique_ids),
            )).mappings()]
            related_ids = [item["id"] for item in related]
            con.execute(update(executions).where(executions.c.tenant_id == tenant,
                executions.c.object_id.in_([*unique_ids, *related_ids]),
                executions.c.status.in_(["pending", "queued", "running"]),
            ).values(status="superseded", token=None, lease_until=None, finished_at=now(), updated_at=now()))
            con.execute(delete(records).where(records.c.task_id.in_(unique_ids)))
            con.execute(delete(revisions).where(revisions.c.task_id.in_(unique_ids)))
            if related_ids:
                con.execute(delete(objects).where(objects.c.id.in_(related_ids), objects.c.tenant_id == tenant))
            con.execute(delete(objects).where(objects.c.id.in_(unique_ids), objects.c.tenant_id == tenant, objects.c.kind == "task"))
        for object_id in [*unique_ids, *related_ids]:
            shutil.rmtree(self.store.root / "files" / object_id, ignore_errors=True)
        return {"deleted": unique_ids}

    def create(self, tenant: str, actor: str, template_id: str, name: str, filename: str, content: bytes, idempotency: str | None = None):
        with self.store.engine.begin() as con:
            template = self.store.get(con, template_id, tenant, "template")
            if template["status"] != "published":
                raise WorkflowError("请先发布模板。")
            config = WorkflowConfig.model_validate(template["payload"]["config"])
            fingerprint = digest(content, template_id.encode() + name.encode())
            if idempotency:
                task_id = "task_" + hashlib.sha256(f"{tenant}\0{actor}\0{idempotency}".encode()).hexdigest()[:32]
                previous = con.execute(select(objects).where(objects.c.id == task_id)).mappings().first()
                if previous:
                    if (previous["tenant_id"] != tenant or previous["payload"].get("actor") != actor
                            or previous["payload"].get("fingerprint") != fingerprint):
                        raise WorkflowError("相同请求标识对应不同输入，请重新提交。", 409)
                    return self.store.public(dict(previous)), False
            else:
                task_id = identifier("task")
            obj = self.store.create(con, tenant, "task", {
                "name": name or filename.rsplit(".", 1)[0], "filename": filename,
                "template_id": template_id, "template_version": template["payload"].get("version", 1),
                "template_hash": content_hash(config.model_dump(mode="json")), "config": config.model_dump(mode="json"),
                "actor": actor, "fingerprint": fingerprint, "input_sha256": digest(content),
                "stage": "import", "record_count": 0, "error_count": 0,
            }, "processing", task_id)
            persist(self.store.directory(task_id) / "source.xlsx", content)
        return self.store.public(obj), True

    def create_standardization(self, tenant: str, actor: str, name: str, filename: str, content: bytes,
                               idempotency: str | None = None, field_profile_id: str | None = None):
        return self.create_standardization_files(tenant, actor, name, [(filename, content)], idempotency, field_profile_id)

    def create_standardization_files(self, tenant: str, actor: str, name: str, files: list[tuple[str, bytes]],
                                     idempotency: str | None = None, field_profile_id: str | None = None):
        from .field_profiles import FieldProfileService
        from .source_files import clean_filename, validate_uploads
        validate_uploads(files)
        files = [(clean_filename(filename), content) for filename, content in files]
        config, snapshot = FieldProfileService(self.store).snapshot(tenant, field_profile_id)
        config.preserve_extras = True
        config.source_retention_version = 2
        config.source_header_layout = "grouped"
        config.preserve_source_merges = True
        config.exclude_source_sequence = True
        sources = [{"filename": filename, "storage_name": "source.xlsx" if len(files) == 1 else f"source-{index}.xlsx",
                    "sha256": digest(content), "size": size(content)}
                   for index, (filename, content) in enumerate(files, start=1)]
        filename = "；".join(source["filename"] for source in sources)
        input_hash = sources[0]["sha256"] if len(files) == 1 else content_hash(sources)
        # Preserve the existing single-file idempotency contract.
        fingerprint = (digest(files[0][1], content_hash(snapshot).encode() + name.encode())
                       if len(files) == 1 else content_hash({"sources": sources, "snapshot": snapshot, "name": name}))
        default_name = Path(files[0][0]).stem if len(files) == 1 else f"{Path(files[0][0]).stem[:70]}等 {len(files)} 个文件"
        task_id = "task_" + hashlib.sha256(f"{tenant}\0{actor}\0{idempotency}".encode()).hexdigest()[:32] if idempotency else identifier("task")
        # Copy large workbooks outside the database write transaction. A quick
        # directory rename publishes them with the admitted task, so concurrent
        # uploads do not hold the SQLite/PostgreSQL write lock during disk IO.
        staging_root = self.store.root / 'staging'
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='task-source-', dir=staging_root))
        target = self.store.root / 'files' / task_id
        published = False
        committed = False
        try:
            for source, (_, content) in zip(sources, files):
                persist(staging / source['storage_name'], content)
            with self.store.engine.begin() as con:
                previous = con.execute(select(objects).where(objects.c.id == task_id)).mappings().first()
                if previous:
                    if (previous["tenant_id"] != tenant or previous["payload"].get("actor") != actor
                            or previous["payload"].get("fingerprint") != fingerprint):
                        raise WorkflowError("相同请求标识对应不同输入，请重新提交。", 409)
                    return self.store.public(dict(previous)), False
                obj = self.store.create(con, tenant, "task", {
                    "name": name or default_name, "filename": filename, "workflow": "standardization",
                    "template_id": None, "template_version": snapshot["version"], "template_hash": content_hash(config.model_dump(mode="json")),
                    "field_profile_id": snapshot["profile_id"],
                    "config": config.model_dump(mode="json"), "field_snapshot": snapshot, "actor": actor,
                    "fingerprint": fingerprint, "input_sha256": input_hash, "source_files": sources,
                    "stage": "import", "record_count": 0, "error_count": 0,
                }, "processing", task_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise WorkflowError('任务文件目录已存在，请使用新的请求标识。', 409)
                staging.replace(target)
                published = True
            committed = True
            return self.store.public(obj), True
        finally:
            if not committed and published and target.is_relative_to(self.store.root / 'files'):
                shutil.rmtree(target, ignore_errors=True)
            if staging.exists() and staging.is_relative_to(staging_root):
                shutil.rmtree(staging, ignore_errors=True)

    def parse(self, task_id: str, tenant: str):
        progress = None
        try:
            with self.store.engine.connect() as con:
                task = self.store.get(con, task_id, tenant, "task")
                expected = task["revision"]
                if task["status"] != "processing":
                    return
            config = WorkflowConfig.model_validate(task["payload"]["config"])
            standardization = task["payload"].get("workflow") == "standardization"
            if standardization:
                from .progress import TaskProgress
                progress = TaskProgress(self.store, task_id, tenant, expected, 'processing')
                progress.update(stage='reading_excel')
            if standardization and task["payload"].get("source_files"):
                from .source_files import discover_sources
                sheets = discover_sources(self.store.directory(task_id), task["payload"]["source_files"], config)
            else:
                sheets = discover(self.store.directory(task_id) / "source.xlsx", config.fields, supplier=standardization,
                                  preserve_extras=standardization and config.preserve_extras)
            if standardization:
                from .semantic_mapping import suggest_semantic_bindings
                from .ai import AIClient
                categories = task["payload"]["field_snapshot"]["profile"]["local_categories"]
                for sheet in sheets:
                    sheet["category_id"] = next((c["id"] for c in categories if config.category_field and c["title"] == sheet["sheet"]), "unclassified")
                ai = AIClient(self.store.root, tenant)
                suggest_semantic_bindings(sheets, config, ai, categories, progress=progress.update)
            else:
                suggest_bindings(sheets, config)
            filename = f"source-data-{self.store.execution[1]}.json" if self.store.execution else "source-data.json"
            write_json(self.store.directory(task_id) / filename, sheets)
            with self.store.engine.begin() as con:
                current = self.store.get(con, task_id, tenant, "task")
                if current["status"] != "processing" or current["revision"] != expected:
                    return
                current["payload"]["sheets"] = [{key: value for key, value in sheet.items() if key != "rows"} for sheet in sheets]
                current["payload"]["stage"] = "mapping"
                current["payload"]["source_data_file"] = filename
                current['payload']['image_asset_ids'] = sorted(p.stem for p in (self.store.directory(task_id) / 'images').glob('*.json'))
                current["status"] = "mapping"
                if progress:
                    progress.finish(current)
                self.store.save(con, current)
        except Exception as exc:
            if progress:
                progress.update(stage='failed', status='failed', finished_at=now())
            self.fail(task_id, tenant, exc, "task")

    def analyze_mapping(self, task_id: str, tenant: str, actor: str, base_revision: int, categories: dict[str, str] | None = None, *, upgrade_snapshot=False):
        from .semantic_mapping import suggest_semantic_bindings
        from .ai import AIClient
        task = self.get(task_id, tenant)
        if task["status"] != "mapping" or task["payload"].get("workflow") != "standardization":
            raise WorkflowError("当前任务不能重新分析字段。", 409)
        if task["revision"] != base_revision:
            raise WorkflowError("数据已有新版本，请刷新。", 409)
        raw = json.loads((self.store.directory(task_id) / task["payload"].get("source_data_file", "source-data.json")).read_text(encoding="utf-8"))
        from .field_profiles import FieldProfile, compile_profile
        snapshot = dict(task["payload"]["field_snapshot"])
        profile = FieldProfile.model_validate(snapshot["profile"])
        config = compile_profile(profile)
        config.preserve_extras = task["payload"]["config"].get("preserve_extras", False)
        config.source_header_layout = task["payload"]["config"].get("source_header_layout", "provenance")
        config.preserve_source_merges = task["payload"]["config"].get("preserve_source_merges", False)
        config.exclude_source_sequence = task["payload"]["config"].get("exclude_source_sequence", False)
        config.source_retention_version = task['payload']['config'].get('source_retention_version',0)
        changes = []
        if upgrade_snapshot:
            from .specification_compatibility import upgraded_snapshot
            from .field_profiles import FieldProfileService
            from .source_files import discover_sources
            _, published = FieldProfileService(self.store).snapshot(tenant, snapshot.get('profile_id'))
            config, snapshot, changes = upgraded_snapshot(task, published)
            raw = discover_sources(self.store.directory(task_id), task['payload']['source_files'], config)
        definitions = snapshot["profile"]["local_categories"]
        for sheet in raw:
            sheet["category_id"] = "unclassified"
        from .progress import TaskProgress
        progress = TaskProgress(self.store, task_id, tenant, base_revision, 'mapping')
        progress.update(stage='reading_excel')
        try:
            ai = AIClient(self.store.root, tenant)
            suggest_semantic_bindings(raw, config, ai, definitions, progress=progress.update)
        except Exception:
            progress.update(stage='failed', status='failed', finished_at=now())
            raise
        # A distinct file prevents a stale analysis from overwriting the source
        # data used by a newer revision. The DB publishes its pointer atomically.
        filename = f'source-data-{identifier("analysis")}.json'
        write_json(self.store.directory(task_id) / filename, raw)
        with self.store.engine.begin() as con:
            current = self.store.claim(con, task_id, tenant, base_revision)
            if current["status"] != "mapping":
                raise WorkflowError("任务已离开字段确认步骤。", 409)
            # Keep row data immutable; publish only the new suggestions under revision control.
            current["payload"]["sheets"] = [{k: v for k, v in s.items() if k != "rows"} for s in raw]
            snapshot["matching_scope"] = "all_fields"
            current["payload"].update(config=config.model_dump(mode="json"), field_snapshot=snapshot,
                                      source_data_file=filename, template_version=snapshot['version'],
                                      template_hash=content_hash(config.model_dump(mode="json")))
            current["payload"].pop("mapping", None)
            progress.finish(current)
            self.store.save(con, current)
            self.store.audit(con, current, actor, {"action": "upgrade_field_snapshot" if upgrade_snapshot else "semantic_mapping", 'field_changes': changes,
                                                 'source_blocks': [s['sheet'] for s in raw if s.get('source_block')]})
        return self.get(task_id, tenant)

    def fail(self, object_id: str, tenant: str, exc: Exception, kind: str):
        import logging
        logging.getLogger(__name__).warning("Workflow failed: %s", object_id, exc_info=exc)
        if self.store.execution is not None:
            raise exc
        with self.store.engine.begin() as con:
            obj = self.store.get(con, object_id, tenant, kind)
            if obj["status"] == "cancelled":
                return
            obj["status"] = "failed"
            obj["payload"]["error"] = str(exc) if isinstance(exc, WorkflowError) else "处理失败，原始文件与已保存数据保持完整，请检查文件或重试。"
            self.store.save(con, obj)

    def confirm_mapping(self, task_id: str, tenant: str, actor: str, request: MappingRequest, *, _repair=False):
        task, old_rows = self.snapshot(task_id, tenant, request.base_revision,include_rows=_repair)
        if task["status"] != "mapping" and not (_repair and task['status'] in {'maintenance','ready','completed'}):
            raise WorkflowError("当前任务不在字段确认步骤。", 409)
        config = WorkflowConfig.model_validate(task["payload"]["config"])
        raw = json.loads((self.store.directory(task_id) / task["payload"].get("source_data_file", "source-data.json")).read_text(encoding="utf-8"))
        repair_filename = None
        if _repair:
            from .source_files import discover_sources
            previous={s['sheet']:s for s in raw}
            config.source_retention_version=2
            raw=discover_sources(self.store.directory(task_id),task['payload']['source_files'],config)
            fresh={s['sheet']:s for s in raw}
            for plan in request.sheets:
                prior=previous[plan.sheet];source=fresh[plan.sheet]
                if (plan.header_row,plan.header_depth)==(prior['header_row'],prior['header_depth']):
                    plan.header_row=source['header_row'];plan.header_depth=source['header_depth']
            repair_filename=f'source-data-{identifier("retention")}.json'
        by_sheet = {item["sheet"]: item for item in raw}
        if {sheet.sheet for sheet in request.sheets} != set(by_sheet) or len(request.sheets) != len(by_sheet):
            raise WorkflowError("请明确选择所有识别到的工作表，未使用的表可取消勾选。")
        preserve_extras = task["payload"].get("workflow") == "standardization" and config.preserve_extras
        if preserve_extras:
            config = standard_fields_only(config)
        fields = {field.rule.name: field for field in config.fields}
        new_records = []
        ignored = []
        for mapping in request.sheets:
            if not mapping.include:
                ignored.append({"sheet": mapping.sheet, "reason": "用户排除工作表"})
                continue
            source = by_sheet[mapping.sheet]
            if preserve_extras and config.source_retention_version >= 2:
                from .source_presentation import context_rows
                source['context_rows'] = context_rows(source,mapping)
            if config.category_field:
                categories = task["payload"]["field_snapshot"]["profile"]["local_categories"]
                if mapping.category_id not in {"unclassified", *(c["id"] for c in categories)}:
                    raise WorkflowError("来源类目无效。")
                if any(b.field == config.category_field or fields.get(b.field) and fields[b.field].category_id
                       and fields[b.field].category_id != mapping.category_id for b in mapping.bindings):
                    raise WorkflowError("字段映射包含其他类目的动态字段或系统字段。")
            count = len(source["columns"])
            bindings = mapping.bindings
            if len({binding.field for binding in bindings}) != len(bindings):
                raise WorkflowError(f"{mapping.sheet} 多个来源映射到同一字段，请改用一条拼接规则或选择正确来源。")
            if any(binding.field not in fields or any(c > count for c in binding.columns) for binding in bindings):
                raise WorkflowError("字段映射引用了不存在的字段或列。")
            if any(c < 1 or c > count for c in mapping.ignored_columns):
                raise WorkflowError("忽略列编号无效。")
            used = {column for binding in bindings if not preserve_extras or binding.operation != "constant" for column in binding.columns}
            sequence_columns = {column["column"] for column in source["columns"]
                                if config.exclude_source_sequence and is_sequence_header(column["header"])} - used
            if used & set(mapping.ignored_columns):
                raise WorkflowError("已映射列不能同时忽略。")
            for col in range(1, count + 1):
                if col in used or preserve_extras:
                    continue
                title = source["columns"][col - 1]["header"]
                reason = "用户忽略" if col in mapping.ignored_columns else "未包含于模板字段"
                ignored.append({"sheet": mapping.sheet, "column": title, "reason": reason})
            start = mapping.header_row + mapping.header_depth - 1
            if start >= len(source["rows"]):
                raise WorkflowError(f"{mapping.sheet} 数据起始位置之后没有记录。")
            for row_number, raw_values in enumerate(source["rows"][start:], start=start + 1):
                context_reason = source.get('context_rows',{}).get(row_number,'')
                retained_content = preserve_extras and any(value for col, value in enumerate(raw_values, start=1) if col not in used | sequence_columns)
                if config.source_retention_version < 2 and row_number in source.get("excluded_rows", []) and (source.get('source_block') or not retained_content):
                    continue
                nonempty = [v for col, v in enumerate(raw_values, start=1) if v and col not in sequence_columns]
                if not context_reason and (not nonempty or len(nonempty) == 1 and re_note(nonempty[0]) and not retained_content):
                    continue
                values = {key: field.default or "" for key, field in fields.items()}
                if context_reason:values={key:'' for key in fields}
                if config.category_field:
                    values[config.category_field] = mapping.category_id
                for binding in bindings:
                    if context_reason:
                        values[binding.field] = ''
                        continue
                    picked = [raw_values[c - 1] if c <= len(raw_values) else "" for c in binding.columns]
                    try:
                        values[binding.field] = transform(picked, binding.operation, binding.argument, binding.part)
                    except WorkflowError as exc:
                        raise WorkflowError(f"{mapping.sheet} 第 {row_number} 行：{exc}") from exc
                new_records.append({"task_id": task_id, "id": identifier("row"), "ordinal": len(new_records) + 1,
                                    "values_json": values, "original": dict(values),
                                    "source": {"sheet": mapping.sheet, "row": row_number,
                                               **({'kind':'context','context_reason':context_reason} if context_reason else {}),
                                               **({'source_block': source['source_block']} if source.get('source_block') else {}),
                                               **({"filename": source["source_filename"], "original_sheet": source["source_sheet"],
                                                   "file_index": source["source_file_index"]} if "source_filename" in source else {}),
                                               **({"mapping_revision": request.base_revision + 1} if task["payload"].get("workflow") == "standardization" else {})},
                                    "issues": [], "search_text": "", "issue_count": 0})
        if preserve_extras:
            config = retain_source_columns(config, raw, request.sheets)
            # Keep generated maintenance workbooks within the same round-trip cell budget.
            if (len(new_records) + 10) * (len(config.fields) + 1) > MAX_CELLS:
                raise WorkflowError("完整保留原表列后的维护区域超过 200 万单元格，请拆分工作表后导入；原表未修改。")
            extras = [field for field in config.fields if field.source_column is not None]
            for record in new_records:
                origin = record["source"]
                raw_values = by_sheet[origin["sheet"]]["rows"][origin["row"] - 1]
                for field in extras:
                    column = field.source_column
                    value = raw_values[column.column - 1] if (column.sheet == origin["sheet"] and column.column <= len(raw_values)
                                and (not column.context_only or origin.get('kind')=='context')) else ""
                    record["values_json"][field.rule.name] = value
                    record["original"][field.rule.name] = value
        if not new_records:
            raise WorkflowError("没有可导入的记录，请检查工作表选择和表头位置。")
        from .source_merges import attach_source_merges
        attach_source_merges(new_records, raw, request.sheets, config)
        if config.source_retention_version>=2:
            from .source_presentation import attach_retained_rectangles
            attach_retained_rectangles(new_records,raw,request.sheets,config)
        if _repair:
            prior_rows={(r['source'].get('sheet'),r['source'].get('row')):r for r in old_rows}
            for record in new_records:
                prior=prior_rows.get((record['source']['sheet'],record['source']['row']))
                if prior:
                    record['id']=prior['id']
                    # Preserve every existing product edit. Only positively
                    # identified source-context rows stop populating business fields.
                    if record['source'].get('kind')!='context':
                        for key,value in prior['values_json'].items():
                            if key in record['values_json']:record['values_json'][key]=value
            source_keys=set(by_sheet)
            for prior in old_rows:
                if prior['source'].get('sheet') not in source_keys:
                    manual=dict(prior)
                    manual['values_json']={f.rule.name:prior['values_json'].get(f.rule.name,f.default or '') for f in config.fields}
                    manual['ordinal']=len(new_records)+1
                    new_records.append(manual)
            write_json(self.store.directory(task_id)/repair_filename,raw)
        config = WorkflowConfig.model_validate(config.model_dump(mode="json"))
        errors, warnings = validate_records(new_records, config)
        for row in new_records:
            row["issue_count"] = len(row["issues"])
        with self.store.engine.begin() as con:
            self.store.claim(con, task_id, tenant, request.base_revision)
            task["revision"] = request.base_revision + 1
            if _repair:con.execute(delete(records).where(records.c.task_id==task_id))
            con.execute(insert(records), new_records)
            task["payload"].update(config=config.model_dump(mode="json"), record_count=len(new_records),
                                   error_count=errors, warning_count=warnings, stage="maintenance",
                                   mapping=request.model_dump(mode="json"), ignored=ignored)
            if config.source_retention_version >= 2:
                from .source_presentation import source_presentations
                task['payload']['source_presentation'] = source_presentations(raw, request.sheets)
                task['payload']['source_review'] = self.source_review(new_records,config)
            task["payload"]["template_hash"] = content_hash(config.model_dump(mode="json"))
            if repair_filename:task['payload']['source_data_file']=repair_filename
            task["status"] = "maintenance" if errors else "ready"
            self.store.save(con, task)
            self.store.audit(con, task, actor, {"action": "repair_source_retention" if _repair else "import", "records": len(new_records)})
        return self.get(task_id, tenant)

    def repair_source_retention(self,task_id,tenant,actor,base_revision):
        task,_=self.snapshot(task_id,tenant,base_revision)
        if task['payload'].get('workflow')!='standardization' or not task['payload'].get('mapping'):
            raise WorkflowError('请先确认字段后再修复原表还原。',409)
        with self.store.engine.connect() as con:
            audit=con.execute(select(revisions.c.detail).where(revisions.c.task_id==task_id)).scalars().all()
        if any(d.get('deleted',0) or d.get('delete_missing') and d.get('counts',{}).get('missing',0) for d in audit):
            raise WorkflowError('本任务已有人工删除记录，不能直接按原表补回；请先核对需要恢复的范围。',409)
        request=MappingRequest.model_validate({**task['payload']['mapping'],'base_revision':base_revision})
        return self.confirm_mapping(task_id,tenant,actor,request,_repair=True)

    @staticmethod
    def source_review(rows,config):
        from .source_presentation import is_context
        missing=[{'id':r['id'],'source':r['source']} for r in rows if not is_context(r,config) and not r['values_json'].get('product_name')]
        return {'missing_product_names':missing,'context_rows':sum(is_context(r,config) for r in rows),
                'empty_source_columns':[{'field':f.rule.name,'source':f.source_column.model_dump(mode='json')} for f in config.fields
                    if f.source_column and not f.source_column.context_only and not any(r['values_json'].get(f.rule.name) for r in rows)]}

    def reopen_mapping(self, task_id: str, tenant: str, actor: str, base_revision: int):
        """Return a task to field confirmation so its maintenance rows can be rebuilt."""
        with self.store.engine.begin() as con:
            task = self.store.claim(con, task_id, tenant, base_revision)
            if task["status"] not in {"maintenance", "ready", "completed"}:
                raise WorkflowError("当前任务不能重新确认字段。", 409)
            con.execute(delete(records).where(records.c.task_id == task_id))
            if task["payload"].get("workflow") == "standardization" and task["payload"]["config"].get("preserve_extras"):
                config = standard_fields_only(WorkflowConfig.model_validate(task["payload"]["config"]))
                task["payload"].update(config=config.model_dump(mode="json"), template_hash=content_hash(config.model_dump(mode="json")))
            task["payload"].update(record_count=0, error_count=0, warning_count=0, stage="mapping")
            task["status"] = "mapping"
            self.store.save(con, task)
            self.store.audit(con, task, actor, {"action": "reopen_mapping"})
        return self.get(task_id, tenant)

    def rows(self, task_id: str, tenant: str, offset: int, limit: int, search: str = "", issues_only: bool = False, sort: str = "", descending: bool = False):
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, "task")
            query = select(records).where(records.c.task_id == task_id)
            if issues_only:
                query = query.where(records.c.issue_count > 0)
            if search:
                query = query.where(records.c.search_text.contains(search.casefold(), autoescape=True))
            total = con.execute(select(func.count()).select_from(query.subquery())).scalar_one()
            if sort:
                if sort not in {field["rule"]["name"] for field in task["payload"]["config"]["fields"]}:
                    raise WorkflowError("排序字段不存在。")
                # Sort only IDs and the requested key, never every wide record/image metadata.
                keys = [(row.id, row.sort_value or '') for row in con.execute(query.with_only_columns(
                    records.c.id, records.c.values_json[sort].as_string().label('sort_value')).order_by(records.c.ordinal))]
                from decimal import Decimal, InvalidOperation
                rule = next(field["rule"] for field in task["payload"]["config"]["fields"] if field["rule"]["name"] == sort)
                def sort_key(row):
                    value = row[1]
                    if rule["type"] in {"integer", "decimal", "number", "float"}:
                        try:
                            number = Decimal(value)
                            if number.is_finite():
                                return (0, number)
                        except (InvalidOperation, ValueError):
                            pass
                        return (1, value)
                    return (0, value.casefold())
                keys.sort(key=sort_key, reverse=descending)
                page_ids = [key[0] for key in keys[offset:offset + limit]]
                page = {row['id']: dict(row) for row in con.execute(query.where(records.c.id.in_(page_ids))).mappings()}
                items = [page[key] for key in page_ids]
            else:
                items = [dict(row) for row in con.execute(query.order_by(records.c.ordinal).offset(offset).limit(limit)).mappings()]
        if task["payload"].get("workflow") == "standardization":
            mappings = {sheet["sheet"]: sheet["bindings"] for sheet in task["payload"].get("mapping", {}).get("sheets", [])}
            for row in items:
                if "mapping_revision" in row["source"] and "bindings" not in row["source"]:
                    row["source"]["bindings"] = mappings.get(row["source"]["sheet"], [])
        return {"items": items, "total": total, "revision": task["revision"]}

    def edit(self, task_id: str, tenant: str, actor: str, request: EditRequest, *, expand_shared=True, audit_action='edit'):
        task, all_rows = self.snapshot(task_id, tenant, request.base_revision, include_rows=True)
        if task["status"] not in {"maintenance", "ready", "completed"}:
            raise WorkflowError("当前任务不可编辑。", 409)
        config = WorkflowConfig.model_validate(task["payload"]["config"])
        fields = {field.rule.name: field for field in config.fields}
        by_id = {row["id"]: row for row in all_rows}
        if set(request.changes) - by_id.keys() or set(request.deletions) - by_id.keys():
            raise WorkflowError("修改包含未知记录。")
        if set(request.changes) & set(request.deletions):
            raise WorkflowError("不能同时修改和删除同一记录。")
        before = {row["id"]: content_hash(row) for row in all_rows}
        if config.preserve_source_merges and expand_shared:
            from .source_merges import expand_shared_changes
            request = request.model_copy(update={'changes': expand_shared_changes(all_rows, request.changes, request.deletions)})
        for row_id, changes in request.changes.items():
            for field_id, value in changes.items():
                if field_id not in fields:
                    raise WorkflowError("修改包含未知字段。")
                if not fields[field_id].editable and cell_text(value) != by_id[row_id]["values_json"].get(field_id, ""):
                    raise WorkflowError(f"{fields[field_id].rule.title} 为只读字段。")
                if len(value or "") > 32767:
                    raise WorkflowError("单元格内容过长。")
                by_id[row_id]["values_json"][field_id] = value or ""
        removed = set(request.deletions)
        all_rows = [row for row in all_rows if row["id"] not in removed]
        next_ordinal = max((row["ordinal"] for row in by_id.values()), default=0) + 1
        for i, supplied in enumerate(request.additions):
            if set(supplied) - fields.keys():
                raise WorkflowError("新增记录包含未知字段。")
            for field_id, value in supplied.items():
                if len(value or "") > 32767:
                    raise WorkflowError("单元格内容过长。")
                if not fields[field_id].editable and cell_text(value) != (fields[field_id].default or ""):
                    raise WorkflowError(f"{fields[field_id].rule.title} 为只读字段。")
            values = {key: supplied.get(key, field.default) or "" for key, field in fields.items()}
            all_rows.append({"task_id": task_id, "id": identifier("row"), "ordinal": next_ordinal + i,
                             "values_json": values, "original": {}, "source": {"sheet": "手工新增", "row": None},
                             "issues": [], "issue_count": 0, "search_text": ""})
        if not all_rows or len(all_rows) > MAX_ROWS:
            raise WorkflowError("维护数据需要保留 1 至 20,000 条记录。")
        if config.preserve_extras and (len(all_rows) + 10) * (len(config.fields) + 1) > MAX_CELLS:
            raise WorkflowError("维护区域超过 200 万单元格，请拆分任务后新增记录；已有数据未修改。")
        errors, warnings = validate_records(all_rows, config)
        validate_image_refs(all_rows, self.store.directory(task_id) / 'images')
        additions, changes = [], []
        for row in all_rows:
            row["issue_count"] = len(row["issues"])
            if row["id"] not in before:
                additions.append(row)
            elif content_hash(row) != before[row["id"]]:
                changes.append({"row_key": row["id"], "new_values": row["values_json"],
                                "new_issues": row["issues"], "new_count": row["issue_count"], "new_search": row["search_text"]})
        with self.store.engine.begin() as con:
            self.store.claim(con, task_id, tenant, request.base_revision)
            task["revision"] = request.base_revision + 1
            if additions:
                con.execute(insert(records), additions)
            if changes:
                from sqlalchemy import bindparam
                con.execute(update(records).where(records.c.task_id == task_id, records.c.id == bindparam("row_key")).values(
                    values_json=bindparam("new_values"), issues=bindparam("new_issues"),
                    issue_count=bindparam("new_count"), search_text=bindparam("new_search")), changes)
            if removed:
                con.execute(delete(records).where(records.c.task_id == task_id, records.c.id.in_(removed)))
            task["payload"].update(record_count=len(all_rows), error_count=errors, warning_count=warnings, stage="maintenance")
            if config.source_retention_version>=2:task['payload']['source_review']=self.source_review(all_rows,config)
            task['payload']['image_asset_ids'] = sorted(set(task['payload'].get('image_asset_ids', [])) | set(record_image_ids(all_rows)))
            task["status"] = "maintenance" if errors else "ready"
            self.store.save(con, task)
            self.store.audit(con, task, actor, {"action": audit_action, "changed_records": len(request.changes),
                                             "added": len(request.additions), "deleted": len(removed),
                                             "fields": sorted({field for change in request.changes.values() for field in change})})
        return self.get(task_id, tenant)

    def maintenance_file(self, task_id: str, tenant: str, *, table_mode=False):
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, "task")
            if task["revision"] < 1 or task["status"] in {"mapping", "processing", "failed", "cancelled"}:
                raise WorkflowError("请先生成维护表。")
            rows = [dict(row) for row in con.execute(select(records).where(records.c.task_id == task_id).order_by(records.c.ordinal)).mappings()]
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', task["payload"]["name"]).strip(' .')[:60] or "商品"
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M%S')
        path = self.store.directory(task_id) / f"{name}_维护表_{stamp}_{identifier('download')}.xlsx"
        write_maintenance(path, task, rows, table_mode=table_mode)
        return path

    def preview_return(self, task_id: str, tenant: str, content: bytes):
        with self.store.engine.begin() as con:
            task = self.store.get(con, task_id, tenant, "task")
            preview = self.store.create(con, tenant, "return", {"task_id": task_id, "base_revision": task["revision"]})
        path = self.store.directory(preview["id"]) / "return.xlsx"
        persist(path, content)
        _, source_records = self.snapshot(task_id, tenant, task['revision'], include_rows=True)
        incoming = read_maintenance(path, task, records=source_records)
        with self.store.engine.begin() as con:
            current = self.store.get(con, task_id, tenant, "task")
            if current["revision"] != task["revision"]:
                raise WorkflowError("数据已更新，请重新下载维护表。", 409, "REVISION_CONFLICT")
            rows = {row["id"]: dict(row) for row in con.execute(select(records).where(records.c.task_id == task_id)).mappings()}
            seen = set()
            changes, additions, samples = {}, [], []
            for item in incoming:
                row_id = item["id"]
                if not row_id:
                    additions.append(item["values"])
                    continue
                if row_id in seen:
                    raise WorkflowError("回传文件包含重复的记录 ID。")
                seen.add(row_id)
                if row_id not in rows:
                    raise WorkflowError("回传文件包含未知记录 ID。新增行请留空隐藏 ID 列。")
                delta = {key: value for key, value in item["values"].items() if value != rows[row_id]["values_json"].get(key, "")}
                if delta:
                    changes[row_id] = delta
                    if len(samples) < 100:
                        samples.append({"id": row_id, "source": rows[row_id]["source"], "before": {key: rows[row_id]["values_json"].get(key, "") for key in delta}, "after": delta})
            missing = [row_id for row_id in rows if row_id not in seen]
            # Ambiguous operations need an explicit second confirmation, never an automatic ID guess.
            warnings = []
            missing_values = {content_hash(rows[row_id]['values_json']) for row_id in missing}
            missing_skus = {rows[row_id]['values_json'].get('sku') for row_id in missing} - {None, ''}
            suspects = sum(content_hash(values) in missing_values or bool(values.get('sku') and values.get('sku') in missing_skus)
                           for values in additions)
            if suspects:
                warnings.append({'code': 'POSSIBLE_ID_LOSS', 'count': suspects,
                                 'message': f'{suspects} 条无 ID 的新增行与缺少记录的内容或货号一致，可能是误删了隐藏 ID。请核对新增和缺少明细。'})
            originals = {}
            for row_id, row in rows.items():
                originals.setdefault(content_hash(row['values_json']), set()).add(row_id)
            moved = sum(bool(originals.get(content_hash({**rows[row_id]['values_json'], **delta}), set()) - {row_id})
                        for row_id, delta in changes.items())
            if moved:
                warnings.append({'code': 'POSSIBLE_PARTIAL_SORT', 'count': moved,
                                 'message': f'{moved} 条记录修改后的内容与其他原记录一致，可能只排序了业务列而未移动 ID，请核对修改明细。'})
            fields = {field["rule"]["name"]: field for field in task["payload"]["config"]["fields"]}
            if any(not fields[key]["editable"] for delta in changes.values() for key in delta):
                raise WorkflowError("回传修改了只读字段，请恢复后重新上传。")
            preview["payload"].update(changes=changes, additions=additions, missing=missing, samples=samples,
                                      added_samples=[{'values': values} for values in additions[:20]],
                                      missing_samples=[{'id': row_id, 'values': rows[row_id]['values_json']} for row_id in missing[:20]],
                                      identity_warnings=warnings,
                                      counts={"changed": len(changes), "added": len(additions), "missing": len(missing)})
            from .return_details import write_details
            preview['payload']['detail_manifest'] = write_details(self.store.directory(preview['id']), rows,
                changes=changes, additions=additions, missing=missing)
            preview["status"] = "ready"
            self.store.save(con, preview)
        public = self.store.public(preview)
        public["payload"] = {key: value for key, value in public["payload"].items() if key not in {"changes", "additions"}}
        return public

    def inspect_return(self, task_id: str, tenant: str, content: bytes):
        from .return_import import inspect_book, field_options
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, 'task')
        if task['payload'].get('workflow') != 'standardization' or task['status'] not in {'maintenance', 'ready', 'completed'}:
            raise WorkflowError('当前任务不支持自由结构回传。', 409)
        file_hash = digest(content)
        # Only successful returns belonging to this tenant/task can be reused.
        # Legacy returns predate the digest field; compare their retained files.
        with self.store.engine.connect() as con:
            applied = con.execute(select(objects).where(objects.c.tenant_id == tenant,
                objects.c.kind == 'return', objects.c.status == 'applied',
                objects.c.payload['task_id'].as_string() == task_id).order_by(objects.c.updated_at.desc())).mappings().all()
        for previous in applied:
            previous_hash = previous['payload'].get('file_sha256')
            if not previous_hash:
                previous_file = self.store.directory(previous['id']) / 'return.xlsx'
                if previous_file.is_file():
                    previous_hash = digest(previous_file)
            if previous_hash == file_hash:
                self.snapshot(task_id, tenant, task['revision'])
                return {'id': previous['id'], 'status': 'already_applied', 'payload': {
                    'applied_revision': previous['payload'].get('applied_revision', previous['payload']['base_revision'] + 1),
                    'current_revision': task['revision']}}
        with self.store.engine.begin() as con:
            preview = self.store.create(con, tenant, 'return', {'task_id': task_id, 'base_revision': task['revision'], 'flexible': True, 'file_sha256': file_hash})
        path = self.store.directory(preview['id']) / 'return.xlsx'
        persist(path, content)
        document = inspect_book(path, task)
        self.snapshot(task_id, tenant, task['revision'])
        write_json(path.with_name('document.json'), document)
        config = WorkflowConfig.model_validate(task['payload']['config'])
        preview['payload'].update(columns=document['columns'], sheets=document['sheets'], record_count=len(document['rows']),
                                  field_options=field_options(config), source_revision=document['source_revision'],
                                  stale_revision=document['stale_revision'])
        preview['status'] = 'structure'
        with self.store.engine.begin() as con:
            self.store.save(con, preview)
        return self.store.public(preview)

    def preview_flexible_return(self, task_id, tenant, preview_id, request):
        from .return_import import reconcile
        with self.store.engine.connect() as con:
            preview = self.store.get(con, preview_id, tenant, 'return')
        if preview['payload'].get('task_id') != task_id or not preview['payload'].get('flexible') or preview['status'] not in {'structure', 'row_review', 'ready'}:
            raise WorkflowError('此回传文件不可预览，请重新上传。', 409)
        task, rows = self.snapshot(task_id, tenant, preview['payload']['base_revision'], include_rows=True)
        document = json.loads((self.store.directory(preview_id) / 'document.json').read_text(encoding='utf-8'))
        staged = reconcile(task, rows, document, request)
        write_json(self.store.directory(preview_id) / 'staged.json', staged)
        from .return_details import write_details, field_changes
        baseline_depth = document['sheets'][0]['header_depth']
        if document.get('stale_revision'):
            from .retained_columns import field_header_path
            current_config = WorkflowConfig.model_validate(task['payload']['config'])
            baseline_depth = max((len(field_header_path(f, layout=current_config.source_header_layout))
                                  for f in current_config.fields), default=1)
        preview['payload']['detail_manifest'] = write_details(self.store.directory(preview_id), rows,
            proposed=staged['proposed'], missing=staged['missing'], locations=staged['diff_locations'],
            columns=staged['diff_columns'], original_depth=baseline_depth)
        preview['payload']['field_changes'] = field_changes(WorkflowConfig.model_validate(task['payload']['config']),
            WorkflowConfig.model_validate(staged['config']), staged['diff_columns'])
        preview['payload'].update({k: v for k, v in staged.items() if k not in {'config', 'proposed', 'kept', 'diff_locations', 'diff_columns'}})
        preview['payload']['decisions'] = request.model_dump(mode='json')
        preview['payload']['sync_mode'] = 'replace'
        preview['payload']['record_options'] = ([{'id': row['id'], 'label': f"{row['values_json'].get('sku', '')} {row['values_json'].get('product_name') or row['values_json'].get('name', '')} · 下载时第 {index+document['sheets'][0]['header_depth']+1} 行".strip()} for index, row in enumerate(rows)] if staged['row_review'] else [])
        preview['status'] = 'row_review' if staged['row_review'] else 'ready'
        with self.store.engine.begin() as con:
            self.store.save(con, preview)
        public = self.store.public(preview)
        public['payload'] = {k: v for k, v in public['payload'].items() if k not in {'decisions', 'columns'}}
        return public

    def _apply_flexible_return(self, task_id, tenant, actor, preview, delete_missing, confirm_structure, confirm_stale=False):
        payload = preview['payload']
        if payload.get('sync_mode') != 'replace':
            raise WorkflowError('这份预览使用旧的保留规则，请重新上传维护表，核对同步删除的行列后保存。', 409, 'RETURN_REFRESH_REQUIRED')
        # The reviewed return workbook is authoritative. Old clients may still
        # send delete_missing=false; they must not silently restore missing rows.
        delete_missing = True
        task, _ = self.snapshot(task_id, tenant, payload['base_revision'])
        if payload.get('stale_revision') and not confirm_stale:
            raise WorkflowError('这是旧版本维护表，请核对与当前数据的差异，并确认覆盖和删除项后保存。', 422, 'RETURN_STALE_CONFIRMATION_REQUIRED')
        if payload.get('structure_summary') and not confirm_structure:
            raise WorkflowError('请确认字段对应、结构及合并关系变化后保存。', 422, 'RETURN_STRUCTURE_CONFIRMATION_REQUIRED')
        staged = json.loads((self.store.directory(preview['id']) / 'staged.json').read_text(encoding='utf-8'))
        config = WorkflowConfig.model_validate(staged['config'])
        missing = set(staged['missing'])
        rows = staged['proposed']
        if not rows or len(rows) > MAX_ROWS:
            raise WorkflowError('维护数据需要保留 1 至 20,000 条商品。')
        for ordinal, row in enumerate(rows):
            row['ordinal'] = ordinal
        copy_assets(self.store.directory(preview['id']) / 'images', self.store.directory(task_id) / 'images')
        validate_image_refs(rows, self.store.directory(task_id) / 'images')
        errors, warnings = validate_records(rows, config)
        for row in rows:
            row['issue_count'] = len(row['issues'])
        with self.store.engine.begin() as con:
            self.store.claim(con, task_id, tenant, payload['base_revision'])
            task['revision'] = payload['base_revision'] + 1
            con.execute(delete(records).where(records.c.task_id == task_id))
            con.execute(insert(records), rows)
            task['payload'].update(config=config.model_dump(mode='json'), template_hash=content_hash(config.model_dump(mode='json')),
                                   record_count=len(rows), error_count=errors, warning_count=warnings, stage='maintenance')
            if config.source_retention_version>=2:task['payload']['source_review']=self.source_review(rows,config)
            task['payload']['image_asset_ids'] = sorted(set(task['payload'].get('image_asset_ids', [])) | set(record_image_ids(rows)))
            task['status'] = 'maintenance' if errors else 'ready'
            self.store.save(con, task)
            self.store.audit(con, task, actor, {'action': 'flexible_return', 'preview_id': preview['id'],
                             'counts': staged['counts'], 'structure': staged['structure_summary'],
                             'changed_records': staged['counts']['changed'], 'added': staged['counts']['added'],
                             'deleted': len(missing) if delete_missing else 0,
                             'source_revision': payload.get('source_revision', payload['base_revision']),
                             'stale_confirmed': bool(payload.get('stale_revision') and confirm_stale),
                             'delete_missing_confirmed': bool(delete_missing)})
            preview['status'] = 'applied'
            preview['payload'].update(structure_confirmed=bool(confirm_structure), delete_missing_confirmed=bool(delete_missing),
                                      applied_revision=task['revision'], stale_confirmed=bool(payload.get('stale_revision') and confirm_stale))
            self.store.save(con, preview)
        return self.get(task_id, tenant)

    def apply_return(self, task_id: str, tenant: str, actor: str, preview_id: str, delete_missing: bool,
                     acknowledge_identity_warnings: bool = False, confirm_structure: bool = False, confirm_stale: bool = False):
        with self.store.engine.connect() as con:
            preview = self.store.get(con, preview_id, tenant, "return")
            if preview["payload"]["task_id"] != task_id or preview["status"] != "ready":
                raise WorkflowError("回传预览不适用于当前任务。")
        payload = preview["payload"]
        if payload.get('flexible'):
            return self._apply_flexible_return(task_id, tenant, actor, preview, delete_missing, confirm_structure, confirm_stale)
        if payload.get('identity_warnings') and not acknowledge_identity_warnings:
            raise WorkflowError('请先核对记录 ID 风险提示，并明确确认新增、缺少或错位疑点。', 422, 'RETURN_IDENTITY_CONFIRMATION_REQUIRED')
        copy_assets(self.store.directory(preview_id) / 'images', self.store.directory(task_id) / 'images')
        result = self.edit(task_id, tenant, actor, EditRequest(base_revision=payload["base_revision"], changes=payload["changes"],
                           additions=payload["additions"], deletions=payload["missing"] if delete_missing else []))
        with self.store.engine.begin() as con:
            preview["status"] = "applied"
            preview['payload']['identity_warnings_acknowledged'] = bool(acknowledge_identity_warnings)
            preview['payload']['delete_missing_confirmed'] = bool(delete_missing)
            self.store.save(con, preview)
        return result

    @staticmethod
    def _output_config(maintenance: WorkflowConfig, output: WorkflowConfig) -> WorkflowConfig:
        """Apply an output template to a task without changing its maintenance schema."""
        from ..product_workflow.fixed_template import normalize_header

        def names(field) -> set[str]:
            return {normalize_header(value) for value in [field.rule.title, *field.rule.aliases] if value}

        maintenance_fields = {field.rule.name: field for field in maintenance.fields}
        output_fields = {field.rule.name: field for field in output.fields}
        references = {
            reference
            for file in output.outputs
            for sheet in file.sheets
            for reference in [sheet.group_by, *sheet.sku_fields,
                              *(column.field for column in sheet.columns),
                              *(source for column in sheet.columns for source in column.sources)]
            if reference
        }
        field_map: dict[str, str] = {}
        for reference in references:
            source = output_fields.get(reference)
            if source is None:
                raise WorkflowError("最终输出模板包含不存在的字段，请重新发布该模板。")
            matched = [field.rule.name for field in maintenance_fields.values() if names(source) & names(field)]
            if not matched:
                raise WorkflowError(f"最终输出模板字段“{source.rule.title}”未在当前维护表中找到同名字段。")
            if len(matched) > 1:
                raise WorkflowError(f"最终输出模板字段“{source.rule.title}”匹配到多个维护字段，请调整字段名称或别名。")
            field_map[reference] = matched[0]

        result = maintenance.model_dump(mode="json")
        outputs = output.model_dump(mode="json")["outputs"]
        for file in outputs:
            for sheet in file["sheets"]:
                sheet["group_by"] = field_map.get(sheet["group_by"], sheet["group_by"])
                sheet["sku_fields"] = [field_map.get(field, field) for field in sheet["sku_fields"]]
                for column in sheet["columns"]:
                    column["field"] = field_map.get(column["field"], column["field"])
                    column["sources"] = [field_map.get(field, field) for field in column["sources"]]
        result["outputs"] = outputs
        return WorkflowConfig.model_validate(result)

    def create_export(self, task_id: str, tenant: str, base_revision: int, idempotency: str | None = None,
                      output_template_id: str | None = None):
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, "task")
            output_template_id = output_template_id or task["payload"]["template_id"]
            run_id = "export_" + hashlib.sha256(f"{tenant}:{task_id}:{idempotency}".encode()).hexdigest()[:32] if idempotency else identifier("export")
            previous = con.execute(select(objects).where(objects.c.id == run_id)).mappings().first()
            if previous:
                if (previous["payload"]["revision"] != base_revision
                        or previous["payload"].get("output_template_id") != output_template_id):
                    raise WorkflowError("重复生成请求的版本不一致。", 409)
                return self.store.public(dict(previous)), False
        task, rows = self.snapshot(task_id, tenant, base_revision, include_rows=True)
        if task["status"] not in {"ready", "completed"} or task["payload"].get("error_count"):
            raise WorkflowError("请先处理维护表中的阻断性问题。")
        config = WorkflowConfig.model_validate(task["payload"]["config"])
        errors, _ = validate_records(rows, config)
        if errors:
            raise WorkflowError("维护数据未通过生成前校验。")
        if task["payload"].get("workflow") == "standardization":
            if output_template_id:
                raise WorkflowError("标准化任务按创建时的字段快照输出，无需选择模板。")
            output_version = task["payload"]["field_snapshot"]["version"]
            from .final_output import final_output_config
            config = final_output_config(config, task['payload']['field_snapshot']['profile'])
        else:
            with self.store.engine.connect() as con:
                output_template = self.store.get(con, output_template_id, tenant, "template")
            if output_template["status"] != "published":
                raise WorkflowError("请选择已发布的最终输出模板。")
            config = self._output_config(config, WorkflowConfig.model_validate(output_template["payload"]["config"]))
            output_version = output_template["payload"].get("version", 1)
        output_fields = {col.field for file in config.outputs for sheet in file.sheets for col in sheet.columns}
        output_fields.update(name for file in config.outputs for sheet in file.sheets for col in sheet.columns for name in col.sources)
        payload = {
            "task_id": task_id, "revision": base_revision, "template_id": output_template_id,
            "task_name": task['payload']['name'],
            "maintenance_template_id": task["payload"]["template_id"], "output_template_id": output_template_id,
            "output_template_version": output_version, "workflow": task["payload"].get("workflow", "sop"),
            "field_snapshot": task["payload"].get("field_snapshot"), "mapping": task["payload"].get("mapping"),
            "source_files": task["payload"].get("source_files", []),
            'source_presentation':task['payload'].get('source_presentation',{}),
            "template_hash": content_hash(config.model_dump(mode="json")), "config": config.model_dump(mode="json"),
            "record_count": len(rows), "omitted_fields": [field.rule.title for field in config.fields if field.rule.name not in output_fields],
        }
        # Each submitter owns a separate snapshot even for the same idempotency key.
        snapshot_path = self.store.directory(run_id) / (identifier("snap") + ".json")
        write_json(snapshot_path, rows)
        payload["snapshot_file"] = snapshot_path.name
        image_ids = sorted(set(task['payload'].get('image_asset_ids', [])) | set(record_image_ids(rows)))
        payload['image_assets'] = snapshot_assets(self.store.directory(task_id) / 'images', self.store.directory(run_id) / 'images', image_ids)
        try:
            with self.store.engine.begin() as con:
                locked = con.execute(update(objects).where(objects.c.id == task_id, objects.c.tenant_id == tenant,
                    objects.c.revision == base_revision, objects.c.status.in_(["ready", "completed"])).values(updated_at=now()))
                if locked.rowcount != 1:
                    raise WorkflowError("维护数据已更新，请刷新后生成。", 409)
                previous = con.execute(select(objects).where(objects.c.id == run_id)).mappings().first()
                if previous:
                    if (previous["payload"]["revision"] != base_revision
                            or previous["payload"].get("output_template_id") != output_template_id):
                        raise WorkflowError("重复生成请求的版本不一致。", 409)
                    snapshot_path.unlink(missing_ok=True)
                    return self.store.public(dict(previous)), False
                run = self.store.create(con, tenant, "export", payload, "processing", run_id)
        except BaseException:
            snapshot_path.unlink(missing_ok=True)
            raise
        return self.store.public(run), True

    def generate(self, run_id: str, tenant: str):
        staging = None
        try:
            with self.store.engine.connect() as con:
                run = self.store.get(con, run_id, tenant, "export")
                if run["status"] != "processing":
                    return
                template = self.store.get(con, run["payload"]["template_id"], tenant, "template") if run["payload"]["template_id"] else None
            config = WorkflowConfig.model_validate(run["payload"]["config"])
            directory = self.store.directory(run_id)
            rows = json.loads((directory / run["payload"].get("snapshot_file", "snapshot.json")).read_text(encoding="utf-8"))
            suffix = self.store.execution[1] if self.store.execution else identifier("a")[2:]
            staging = directory / f"stg-{suffix}"
            staging.mkdir(exist_ok=True)
            artifacts = []
            for index, spec in enumerate(config.outputs):
                with self.store.engine.connect() as con:
                    if self.store.get(con, run_id, tenant, "export")["status"] == "cancelled":
                        return
                reference = self.store.directory(template["id"]) / "reference.xlsx" if template and index == 0 and template["payload"].get("purpose", "output") == "output" else None
                artifacts.append(write_output(staging / spec.name, spec, config, rows, reference, image_directory=directory / 'images',
                                              source_presentation=run['payload'].get('source_presentation')))
            manifest = {"revision": run["payload"]["revision"], "template_hash": run["payload"]["template_hash"],
                        "files": artifacts, "omitted_fields": run["payload"]["omitted_fields"],
                        "field_snapshot": run["payload"].get("field_snapshot"),
                        "source_files": run["payload"].get("source_files", []),
                        'image_assets': run['payload'].get('image_assets', [])}
            write_json(staging / "生成说明.json", manifest)
            with zipfile.ZipFile(staging / "最终表.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                for artifact in artifacts:
                    archive.write(staging / artifact["name"], artifact["name"])
                archive.write(staging / "生成说明.json", "生成说明.json")
                for info in run['payload'].get('image_assets', []):
                    image, _ = find_asset(directory / 'images', info['id'])
                    archive.write(image, '原始图片/' + image.name)
            with self.store.engine.begin() as con:
                current = self.store.get(con, run_id, tenant, "export")
                if current["status"] != "processing" or current["revision"] != run["revision"]:
                    return
                self.store.fence(con)
                final = directory / f"output-{suffix}"
                staging.replace(final)
                current["payload"].update(artifacts=artifacts, completed_at=now(), output_directory=final.name)
                current["status"] = "completed"
                self.store.save(con, current)
                task = self.store.get(con, run["payload"]["task_id"], tenant, "task")
                # Never label a newer maintenance revision as exported by an old run.
                if task["revision"] == run["payload"]["revision"] and task["status"] in {"ready", "completed"}:
                    task["status"] = "completed"
                    task["payload"]["stage"] = "output"
                    self.store.save(con, task)
        except Exception as exc:
            self.fail(run_id, tenant, exc, "export")
        finally:
            if staging is not None and staging.exists():
                # Delete only this generated staging directory, never task inputs or snapshots.
                shutil.rmtree(staging)

    def image_file(self, task_id, tenant, digest):
        with self.store.engine.connect() as con:
            self.store.get(con, task_id, tenant, 'task')
        return find_asset(self.store.directory(task_id) / 'images', digest)

    def export_download_name(self, task_id: str, run_id: str, tenant: str, filename: str) -> str:
        if not filename.lower().endswith('.xlsx'):
            return filename
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, 'task')
            run = self.store.get(con, run_id, tenant, 'export')
        if run['payload']['task_id'] != task_id or run['status'] != 'completed':
            raise WorkflowError('此生成批次尚未完整完成。', 409)
        return final_download_name(run['payload'].get('task_name', task['payload']['name']),
                                   run['payload'].get('completed_at') or run['created_at'])

    def export_file(self, task_id: str, run_id: str, tenant: str, filename: str):
        with self.store.engine.connect() as con:
            self.store.get(con, task_id, tenant, "task")
            run = self.store.get(con, run_id, tenant, "export")
            if run["payload"]["task_id"] != task_id or run["status"] != "completed":
                raise WorkflowError("此生成批次尚未完整完成。", 409)
            allowed = {item["name"] for item in run["payload"].get("artifacts", [])} | {"最终表.zip", "生成说明.json"}
            if filename not in allowed:
                raise WorkflowError("文件不存在", 404)
        return self.store.directory(run_id) / run["payload"].get("output_directory", "output") / filename

    def cancel(self, task_id: str, tenant: str, base_revision: int):
        with self.store.engine.begin() as con:
            task = self.store.claim(con, task_id, tenant, base_revision)
            if task["status"] not in {"processing", "mapping", "failed"}:
                raise WorkflowError("维护任务无需取消，已保存数据可稍后继续。")
            task["status"] = "cancelled"
            self.store.save(con, task)
        return self.get(task_id, tenant)


def re_note(value: str) -> bool:
    return value.strip().startswith(("备注", "说明", "注：", "注:"))

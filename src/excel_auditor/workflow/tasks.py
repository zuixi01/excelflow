from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

from sqlalchemy import delete, func, insert, select, update

from .excel import MAX_ROWS, cell_text, discover, literal, read_maintenance, transform, validate_records, write_maintenance, write_output
from .models import ColumnRule, EditRequest, MaintenanceField, MappingRequest, WorkflowConfig, WorkflowError, content_hash
from .repository import WorkflowStore, identifier, now, objects, records, revisions


def write_json(path: Path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


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
    def __init__(self, store: WorkflowStore):
        self.store = store

    def get(self, task_id: str, tenant: str) -> dict:
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, "task")
            history = con.execute(select(revisions.c.revision, revisions.c.actor, revisions.c.created_at, revisions.c.detail)
                                  .where(revisions.c.task_id == task_id).order_by(revisions.c.revision.desc()).limit(20)).mappings().all()
            exports = con.execute(select(objects).where(objects.c.tenant_id == tenant, objects.c.kind == "export")
                                  .order_by(objects.c.created_at.desc())).mappings().all()
        result = self.store.public(task)
        result["history"] = [dict(item) for item in history]
        result["exports"] = [self.store.public(dict(item)) for item in exports if item["payload"].get("task_id") == task_id]
        return result

    def list(self, tenant: str, search: str = "", status: str = "", offset: int = 0, limit: int = 30):
        with self.store.engine.connect() as con:
            query = select(objects).where(objects.c.tenant_id == tenant, objects.c.kind == "task")
            if status:
                query = query.where(objects.c.status == status)
            # Search titles without embedding SQL or depending on JSON dialect functions.
            items = [dict(row) for row in con.execute(query.order_by(objects.c.updated_at.desc())).mappings()]
        if search:
            items = [item for item in items if search.casefold() in (item["payload"].get("name", "") + item["payload"].get("filename", "")).casefold()]
        summary = []
        for item in items[offset:offset + limit]:
            payload = item["payload"]
            summary.append({"id": item["id"], "status": item["status"], "revision": item["revision"],
                            "name": payload["name"], "filename": payload["filename"], "template_name": payload["config"]["name"],
                            "record_count": payload.get("record_count", 0), "error_count": payload.get("error_count", 0),
                            "updated_at": item["updated_at"], "error": payload.get("error", "")})
        return {"items": summary, "total": len(items)}

    def create(self, tenant: str, actor: str, template_id: str, name: str, filename: str, content: bytes, idempotency: str | None = None):
        with self.store.engine.begin() as con:
            template = self.store.get(con, template_id, tenant, "template")
            if template["status"] != "published":
                raise WorkflowError("请先试运行并发布模板。")
            config = WorkflowConfig.model_validate(template["payload"]["config"])
            fingerprint = hashlib.sha256(content + template_id.encode() + name.encode()).hexdigest()
            if idempotency:
                task_id = "task_" + hashlib.sha256(f"{tenant}:{idempotency}".encode()).hexdigest()[:32]
                previous = con.execute(select(objects).where(objects.c.id == task_id)).mappings().first()
                if previous:
                    if previous["tenant_id"] != tenant or previous["payload"].get("fingerprint") != fingerprint:
                        raise WorkflowError("相同请求标识对应不同输入，请重新提交。", 409)
                    return self.store.public(dict(previous)), False
            else:
                task_id = identifier("task")
            obj = self.store.create(con, tenant, "task", {
                "name": name or filename.rsplit(".", 1)[0], "filename": filename,
                "template_id": template_id, "template_version": template["payload"].get("version", 1),
                "template_hash": content_hash(config.model_dump(mode="json")), "config": config.model_dump(mode="json"),
                "actor": actor, "fingerprint": fingerprint, "input_sha256": hashlib.sha256(content).hexdigest(),
                "stage": "import", "record_count": 0, "error_count": 0,
            }, "processing", task_id)
            (self.store.directory(task_id) / "source.xlsx").write_bytes(content)
        return self.store.public(obj), True

    def parse(self, task_id: str, tenant: str):
        try:
            with self.store.engine.connect() as con:
                task = self.store.get(con, task_id, tenant, "task")
                expected = task["revision"]
                if task["status"] != "processing":
                    return
            config = WorkflowConfig.model_validate(task["payload"]["config"])
            sheets = discover(self.store.directory(task_id) / "source.xlsx", config.fields)
            suggest_bindings(sheets, config)
            write_json(self.store.directory(task_id) / "source-data.json", sheets)
            with self.store.engine.begin() as con:
                current = self.store.get(con, task_id, tenant, "task")
                if current["status"] != "processing" or current["revision"] != expected:
                    return
                current["payload"]["sheets"] = [{key: value for key, value in sheet.items() if key != "rows"} for sheet in sheets]
                current["payload"]["stage"] = "mapping"
                current["status"] = "mapping"
                self.store.save(con, current)
        except Exception as exc:
            self.fail(task_id, tenant, exc, "task")

    def fail(self, object_id: str, tenant: str, exc: Exception, kind: str):
        with self.store.engine.begin() as con:
            obj = self.store.get(con, object_id, tenant, kind)
            if obj["status"] == "cancelled":
                return
            obj["status"] = "failed"
            obj["payload"]["error"] = str(exc) if isinstance(exc, WorkflowError) else "处理失败，原始文件与已保存数据保持完整，请检查文件或重试。"
            self.store.save(con, obj)

    def confirm_mapping(self, task_id: str, tenant: str, actor: str, request: MappingRequest):
        with self.store.engine.begin() as con:
            task = self.store.claim(con, task_id, tenant, request.base_revision)
            if task["status"] != "mapping":
                raise WorkflowError("当前任务不在字段确认步骤。", 409)
            config = WorkflowConfig.model_validate(task["payload"]["config"])
            raw = json.loads((self.store.directory(task_id) / "source-data.json").read_text(encoding="utf-8"))
            by_sheet = {item["sheet"]: item for item in raw}
            if {sheet.sheet for sheet in request.sheets} != set(by_sheet) or len(request.sheets) != len(by_sheet):
                raise WorkflowError("请明确选择所有识别到的工作表，未使用的表可取消勾选。")
            fields = {field.rule.name: field for field in config.fields}
            new_records = []
            ignored = []
            for mapping in request.sheets:
                if not mapping.include:
                    ignored.append({"sheet": mapping.sheet, "reason": "用户排除工作表"})
                    continue
                source = by_sheet[mapping.sheet]
                count = len(source["columns"])
                bindings = mapping.bindings
                if len({binding.field for binding in bindings}) != len(bindings):
                    raise WorkflowError(f"{mapping.sheet} 多个来源映射到同一字段，请改用一条拼接规则或选择正确来源。")
                if any(binding.field not in fields or any(c > count for c in binding.columns) for binding in bindings):
                    raise WorkflowError("字段映射引用了不存在的字段或列。")
                if any(c < 1 or c > count for c in mapping.ignored_columns):
                    raise WorkflowError("忽略列编号无效。")
                used = {column for binding in bindings for column in binding.columns}
                if used & set(mapping.ignored_columns):
                    raise WorkflowError("已映射列不能同时忽略。")
                extras = {}
                for col in range(1, count + 1):
                    if col in used:
                        continue
                    title = source["columns"][col - 1]["header"]
                    if col in mapping.ignored_columns or not config.preserve_extras:
                        ignored.append({"sheet": mapping.sheet, "column": title, "reason": "按配置不导入"})
                        continue
                    key = "extra_" + hashlib.sha256(f"{mapping.sheet}:{col}:{title}".encode()).hexdigest()[:12]
                    if key not in fields:
                        title = f"扩展/{mapping.sheet}/{title}"
                        taken = {field.rule.title for field in fields.values()}
                        if title in taken:
                            title += f" ({col})"
                        field = MaintenanceField(rule=ColumnRule(name=key, title=title[:255]))
                        config.fields.append(field)
                        fields[key] = field
                    extras[key] = col
                start = mapping.header_row + mapping.header_depth - 1
                if start >= len(source["rows"]):
                    raise WorkflowError(f"{mapping.sheet} 数据起始位置之后没有记录。")
                for row_number, raw_values in enumerate(source["rows"][start:], start=start + 1):
                    nonempty = [v for v in raw_values if v]
                    if not nonempty or len(nonempty) == 1 and re_note(nonempty[0]):
                        continue
                    values = {key: field.default or "" for key, field in fields.items()}
                    for binding in bindings:
                        picked = [raw_values[c - 1] if c <= len(raw_values) else "" for c in binding.columns]
                        try:
                            values[binding.field] = transform(picked, binding.operation, binding.argument, binding.part)
                        except WorkflowError as exc:
                            raise WorkflowError(f"{mapping.sheet} 第 {row_number} 行：{exc}") from exc
                    for key, col in extras.items():
                        values[key] = raw_values[col - 1] if col <= len(raw_values) else ""
                    new_records.append({"task_id": task_id, "id": identifier("row"), "ordinal": len(new_records) + 1,
                                        "values_json": values, "original": dict(values),
                                        "source": {"sheet": mapping.sheet, "row": row_number}, "issues": [], "search_text": "", "issue_count": 0})
            if not new_records:
                raise WorkflowError("没有可导入的记录，请检查工作表选择和表头位置。")
            config = WorkflowConfig.model_validate(config.model_dump(mode="json"))
            errors, warnings = validate_records(new_records, config)
            for row in new_records:
                row["issue_count"] = len(row["issues"])
            con.execute(insert(records), new_records)
            task["payload"].update(config=config.model_dump(mode="json"), record_count=len(new_records),
                                   error_count=errors, warning_count=warnings, stage="maintenance",
                                   mapping=request.model_dump(mode="json"), ignored=ignored)
            task["status"] = "maintenance" if errors else "ready"
            self.store.save(con, task)
            self.store.audit(con, task, actor, {"action": "import", "records": len(new_records)})
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
                items = [dict(row) for row in con.execute(query).mappings()]
                from decimal import Decimal, InvalidOperation
                rule = next(field["rule"] for field in task["payload"]["config"]["fields"] if field["rule"]["name"] == sort)
                def sort_key(row):
                    value = row["values_json"].get(sort, "")
                    if rule["type"] in {"integer", "decimal", "number", "float"}:
                        try:
                            number = Decimal(value)
                            if number.is_finite():
                                return (0, number)
                        except (InvalidOperation, ValueError):
                            pass
                        return (1, value)
                    return (0, value.casefold())
                items.sort(key=sort_key, reverse=descending)
                items = items[offset:offset + limit]
            else:
                items = [dict(row) for row in con.execute(query.order_by(records.c.ordinal).offset(offset).limit(limit)).mappings()]
        return {"items": items, "total": total, "revision": task["revision"]}

    def edit(self, task_id: str, tenant: str, actor: str, request: EditRequest):
        with self.store.engine.begin() as con:
            task = self.store.claim(con, task_id, tenant, request.base_revision)
            if task["status"] not in {"maintenance", "ready", "completed"}:
                raise WorkflowError("当前任务不可编辑。", 409)
            config = WorkflowConfig.model_validate(task["payload"]["config"])
            fields = {field.rule.name: field for field in config.fields}
            all_rows = [dict(row) for row in con.execute(select(records).where(records.c.task_id == task_id)).mappings()]
            by_id = {row["id"]: row for row in all_rows}
            if set(request.changes) - by_id.keys() or set(request.deletions) - by_id.keys():
                raise WorkflowError("修改包含未知记录。")
            if set(request.changes) & set(request.deletions):
                raise WorkflowError("不能同时修改和删除同一记录。")
            before = {row["id"]: content_hash(row) for row in all_rows}
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
            errors, warnings = validate_records(all_rows, config)
            for row in all_rows:
                row["issue_count"] = len(row["issues"])
                if row["id"] not in before:
                    con.execute(insert(records).values(**row))
                elif content_hash(row) != before[row["id"]]:
                    con.execute(update(records).where(records.c.task_id == task_id, records.c.id == row["id"]).values(
                        values_json=row["values_json"], issues=row["issues"], issue_count=row["issue_count"], search_text=row["search_text"]))
            if removed:
                con.execute(delete(records).where(records.c.task_id == task_id, records.c.id.in_(removed)))
            task["payload"].update(record_count=len(all_rows), error_count=errors, warning_count=warnings, stage="maintenance")
            task["status"] = "maintenance" if errors else "ready"
            self.store.save(con, task)
            self.store.audit(con, task, actor, {"action": "edit", "changed_records": len(request.changes),
                                             "added": len(request.additions), "deleted": len(removed),
                                             "fields": sorted({field for change in request.changes.values() for field in change})})
        return self.get(task_id, tenant)

    def maintenance_file(self, task_id: str, tenant: str):
        with self.store.engine.connect() as con:
            task = self.store.get(con, task_id, tenant, "task")
            if task["revision"] < 1 or task["status"] in {"mapping", "processing", "failed", "cancelled"}:
                raise WorkflowError("请先生成维护表。")
            rows = [dict(row) for row in con.execute(select(records).where(records.c.task_id == task_id).order_by(records.c.ordinal)).mappings()]
        path = self.store.directory(task_id) / f"maintenance-r{task['revision']}-{identifier('download')}.xlsx"
        write_maintenance(path, task, rows)
        return path

    def preview_return(self, task_id: str, tenant: str, content: bytes):
        with self.store.engine.begin() as con:
            task = self.store.get(con, task_id, tenant, "task")
            preview = self.store.create(con, tenant, "return", {"task_id": task_id, "base_revision": task["revision"]})
        path = self.store.directory(preview["id"]) / "return.xlsx"
        path.write_bytes(content)
        incoming = read_maintenance(path, task)
        with self.store.engine.begin() as con:
            current = self.store.get(con, task_id, tenant, "task")
            if current["revision"] != task["revision"]:
                raise WorkflowError("数据已更新，请重新下载维护表。", 409)
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
            fields = {field["rule"]["name"]: field for field in task["payload"]["config"]["fields"]}
            if any(not fields[key]["editable"] for delta in changes.values() for key in delta):
                raise WorkflowError("回传修改了只读字段，请恢复后重新上传。")
            preview["payload"].update(changes=changes, additions=additions, missing=missing, samples=samples,
                                      counts={"changed": len(changes), "added": len(additions), "missing": len(missing)})
            preview["status"] = "ready"
            self.store.save(con, preview)
        public = self.store.public(preview)
        public["payload"] = {key: value for key, value in public["payload"].items() if key not in {"changes", "additions"}}
        return public

    def apply_return(self, task_id: str, tenant: str, actor: str, preview_id: str, delete_missing: bool):
        with self.store.engine.connect() as con:
            preview = self.store.get(con, preview_id, tenant, "return")
            if preview["payload"]["task_id"] != task_id or preview["status"] != "ready":
                raise WorkflowError("回传预览不适用于当前任务。")
        payload = preview["payload"]
        result = self.edit(task_id, tenant, actor, EditRequest(base_revision=payload["base_revision"], changes=payload["changes"],
                           additions=payload["additions"], deletions=payload["missing"] if delete_missing else []))
        with self.store.engine.begin() as con:
            preview["status"] = "applied"
            self.store.save(con, preview)
        return result

    def create_export(self, task_id: str, tenant: str, base_revision: int, idempotency: str | None = None):
        with self.store.engine.begin() as con:
            task = self.store.get(con, task_id, tenant, "task")
            if idempotency:
                run_id = "export_" + hashlib.sha256(f"{tenant}:{task_id}:{idempotency}".encode()).hexdigest()[:32]
                previous = con.execute(select(objects).where(objects.c.id == run_id)).mappings().first()
                if previous:
                    if previous["payload"]["revision"] != base_revision:
                        raise WorkflowError("重复生成请求的版本不一致。", 409)
                    return self.store.public(dict(previous)), False
            else:
                run_id = identifier("export")
            # A conditional write serializes snapshot capture against maintenance edits.
            locked = con.execute(update(objects).where(objects.c.id == task_id, objects.c.tenant_id == tenant,
                              objects.c.revision == base_revision).values(updated_at=now()))
            if locked.rowcount != 1:
                raise WorkflowError("维护数据已更新，请刷新后生成。", 409)
            task = self.store.get(con, task_id, tenant, "task")
            if task["status"] not in {"ready", "completed"} or task["payload"].get("error_count"):
                raise WorkflowError("请先处理维护表中的阻断性问题。")
            rows = [dict(row) for row in con.execute(select(records).where(records.c.task_id == task_id).order_by(records.c.ordinal)).mappings()]
            config = WorkflowConfig.model_validate(task["payload"]["config"])
            errors, _ = validate_records(rows, config)
            if errors:
                raise WorkflowError("维护数据未通过生成前校验。")
            output_fields = {col.field for file in config.outputs for sheet in file.sheets for col in sheet.columns}
            output_fields.update(name for file in config.outputs for sheet in file.sheets for col in sheet.columns for name in col.sources)
            run = self.store.create(con, tenant, "export", {
                "task_id": task_id, "revision": base_revision, "template_id": task["payload"]["template_id"],
                "template_hash": task["payload"]["template_hash"], "config": config.model_dump(mode="json"),
                "record_count": len(rows), "omitted_fields": [field.rule.title for field in config.fields if field.rule.name not in output_fields],
            }, "processing", run_id)
            write_json(self.store.directory(run_id) / "snapshot.json", rows)
        return self.store.public(run), True

    def generate(self, run_id: str, tenant: str):
        try:
            with self.store.engine.connect() as con:
                run = self.store.get(con, run_id, tenant, "export")
                if run["status"] != "processing":
                    return
                template = self.store.get(con, run["payload"]["template_id"], tenant, "template")
            config = WorkflowConfig.model_validate(run["payload"]["config"])
            directory = self.store.directory(run_id)
            rows = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
            staging = directory / "staging"
            staging.mkdir(exist_ok=True)
            artifacts = []
            for index, spec in enumerate(config.outputs):
                with self.store.engine.connect() as con:
                    if self.store.get(con, run_id, tenant, "export")["status"] == "cancelled":
                        return
                reference = self.store.directory(template["id"]) / "reference.xlsx" if index == 0 and template["payload"].get("purpose", "output") == "output" else None
                artifacts.append(write_output(staging / spec.name, spec, config, rows, reference))
            manifest = {"revision": run["payload"]["revision"], "template_hash": run["payload"]["template_hash"],
                        "files": artifacts, "omitted_fields": run["payload"]["omitted_fields"]}
            write_json(staging / "生成说明.json", manifest)
            with zipfile.ZipFile(staging / "最终表.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                for artifact in artifacts:
                    archive.write(staging / artifact["name"], artifact["name"])
                archive.write(staging / "生成说明.json", "生成说明.json")
            with self.store.engine.begin() as con:
                current = self.store.get(con, run_id, tenant, "export")
                if current["status"] == "cancelled":
                    return
                final = directory / "output"
                staging.replace(final)
                current["payload"].update(artifacts=artifacts, completed_at=now())
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
            staging = self.store.directory(run_id) / "staging"
            if staging.exists():
                # Delete only this generated staging directory, never task inputs or snapshots.
                shutil.rmtree(staging)

    def export_file(self, task_id: str, run_id: str, tenant: str, filename: str):
        with self.store.engine.connect() as con:
            self.store.get(con, task_id, tenant, "task")
            run = self.store.get(con, run_id, tenant, "export")
            if run["payload"]["task_id"] != task_id or run["status"] != "completed":
                raise WorkflowError("此生成批次尚未完整完成。", 409)
            allowed = {item["name"] for item in run["payload"].get("artifacts", [])} | {"最终表.zip", "生成说明.json"}
            if filename not in allowed:
                raise WorkflowError("文件不存在", 404)
        return self.store.directory(run_id) / "output" / filename

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

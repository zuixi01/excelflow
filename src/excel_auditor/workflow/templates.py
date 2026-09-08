from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from .ai import AIClient
from .excel import cell_text, discover, field_matches, transform, validate_records, write_output
from .models import ColumnRule, MaintenanceField, OutputColumn, OutputFile, OutputSheet, TemplateEdit, WorkflowConfig, WorkflowError, content_hash, default_config
from .repository import WorkflowStore, identifier, objects
from .tasks import TaskService, suggest_bindings, write_json


class TemplateService:
    def __init__(self, store: WorkflowStore):
        self.store = store

    def seed(self, tenant: str):
        template_id = "template_" + hashlib.sha256(f"{tenant}:default-sop-v1".encode()).hexdigest()[:32]
        with self.store.engine.connect() as con:
            existing = con.execute(select(objects.c.id).where(objects.c.id == template_id)).first()
        if not existing:
            config = default_config().model_dump(mode="json")
            try:
                with self.store.engine.begin() as con:
                    self.store.create(con, tenant, "template", {"config": config, "version": 1, "family": template_id,
                        "confirmed": True, "builtin": True, "tested_hash": content_hash(config),
                        "notes": ["内置规则仅要求商品名称必填。请按业务需要补充唯一键、必填字段及输出规则。"]}, "published", template_id)
            except IntegrityError:
                pass  # Another request seeded the same immutable built-in template.
        return template_id

    def list(self, tenant: str):
        self.seed(tenant)
        with self.store.engine.connect() as con:
            items = [self.store.public(dict(row)) for row in con.execute(select(objects).where(
                objects.c.tenant_id == tenant, objects.c.kind == "template").order_by(objects.c.updated_at.desc())).mappings()]
        return {"items": items}

    def get(self, template_id: str, tenant: str):
        with self.store.engine.connect() as con:
            return self.store.public(self.store.get(con, template_id, tenant, "template"))

    def create(self, tenant: str, source_id: str | None = None, name: str = "新业务模板"):
        with self.store.engine.begin() as con:
            if source_id:
                source = self.store.get(con, source_id, tenant, "template")
                payload = {"config": source["payload"]["config"], "version": source["payload"].get("version", 1) + 1,
                           "family": source["payload"].get("family", source_id), "purpose": source["payload"].get("purpose", "output"), "notes": [], "confirmed": False}
            else:
                config = default_config()
                config.name = name
                payload = {"config": config.model_dump(mode="json"), "version": 1, "confirmed": False, "notes": []}
            obj = self.store.create(con, tenant, "template", payload)
            if source_id:
                for filename in ("reference.xlsx", "sample.json"):
                    source_path = self.store.directory(source_id) / filename
                    if source_path.exists():
                        shutil.copyfile(source_path, self.store.directory(obj["id"]) / filename)
        return self.store.public(obj)

    def upload(self, tenant: str, name: str, content: bytes, purpose: str = "output"):
        if purpose not in {"output", "maintenance", "import"}:
            raise WorkflowError("模板用途无效。")
        result = self.create(tenant, name=name)
        with self.store.engine.begin() as con:
            obj = self.store.get(con, result["id"], tenant, "template")
            obj["status"] = "processing"
            obj["payload"]["purpose"] = purpose
            self.store.save(con, obj)
        (self.store.directory(result["id"]) / "reference.xlsx").write_bytes(content)
        result["status"] = "processing"
        return result

    def parse(self, template_id: str, tenant: str):
        try:
            with self.store.engine.connect() as con:
                obj = self.store.get(con, template_id, tenant, "template")
                config = WorkflowConfig.model_validate(obj["payload"]["config"])
            sheets = discover(self.store.directory(template_id) / "reference.xlsx", config.fields, template=True)
            output_sheets = []
            examples = []
            notes = ["已直接读取工作表、表头及列宽。字段含义与业务约束请确认后发布。"]
            for sheet in sheets:
                columns = []
                for item in sheet["columns"]:
                    matches = field_matches(item["header"], config.fields)
                    if len(matches) == 1:
                        field_id = matches[0]
                    else:
                        field_id = "custom_" + hashlib.sha256(item["header"].encode()).hexdigest()[:12]
                        if field_id not in {f.rule.name for f in config.fields}:
                            title = item["header"]
                            if title in {f.rule.title for f in config.fields}:
                                title = "扩展/" + title
                            config.fields.append(MaintenanceField(rule=ColumnRule(name=field_id, title=title)))
                        notes.append(f"{sheet['sheet']} / {item['header']} 已作为扩展字段保留，请确认类型。")
                    operation, argument = "copy", ""
                    example = item["example"]
                    if example.startswith("="):
                        operation = "formula"
                        original_row = sheet["header_row"] + sheet["header_depth"]
                        import re
                        argument = re.sub(rf"(?<=[A-Z]){original_row}(?!\d)", "{row}", example)
                        notes.append(f"公式 {item['header']} 仅保留公式文本，最终计算由 Excel 完成。")
                    columns.append(OutputColumn(title=item["header"], field=field_id, operation=operation, argument=argument,
                                                width=max(4, min(80, sheet["widths"][item["column"] - 1]))))
                # Duplicate physical headers need manual resolution rather than silent merging.
                if len({c.title for c in columns}) != len(columns):
                    raise WorkflowError("模板存在重复表头，请使用不同字段名称后重新上传。")
                output_sheets.append(OutputSheet(name=sheet["sheet"], header_row=sheet["header_row"],
                                    data_start_row=sheet["header_row"] + sheet["header_depth"], columns=columns))
                for row in sheet["rows"][sheet["header_row"] + sheet["header_depth"] - 1:][:10]:
                    if any(row):
                        example_row = {column.field: row[index] for index, column in enumerate(columns)
                                       if index < len(row) and column.operation != "formula"}
                        examples.append(example_row)
            if obj["payload"].get("purpose", "output") == "output":
                config.outputs = [OutputFile(name="最终商品表.xlsx", sheets=output_sheets)]
            config = WorkflowConfig.model_validate(config.model_dump(mode="json"))
            write_json(self.store.directory(template_id) / "sample.json", examples)
            with self.store.engine.begin() as con:
                current = self.store.get(con, template_id, tenant, "template")
                current["payload"].update(config=config.model_dump(mode="json"), notes=notes, confirmed=False,
                                          sample_count=len(examples), structure=[{key: value for key, value in sheet.items() if key not in {"rows", "preview"}} for sheet in sheets])
                current["status"] = "draft"
                self.store.save(con, current)
        except Exception as exc:
            TaskService(self.store).fail(template_id, tenant, exc, "template")

    def update(self, template_id: str, tenant: str, request: TemplateEdit):
        with self.store.engine.begin() as con:
            obj = self.store.claim(con, template_id, tenant, request.base_revision, "template")
            if obj["status"] == "published":
                raise WorkflowError("已发布版本不可修改，请创建新版本。", 409)
            if obj["status"] in {"processing", "learning"}:
                raise WorkflowError("模板正在解析，请稍后编辑。", 409)
            new_config = request.config.model_dump(mode="json")
            if content_hash(new_config) != content_hash(obj["payload"]["config"]):
                obj["payload"].pop("tested_hash", None)
                obj["payload"].pop("test_result", None)
            obj["payload"].update(config=new_config, confirmed=request.confirmed)
            obj["payload"].pop("error", None)
            obj["status"] = "draft"
            self.store.save(con, obj)
        return self.get(template_id, tenant)

    def ai_suggest(self, template_id: str, tenant: str, base_revision: int, instructions: str):
        with self.store.engine.begin() as con:
            obj = self.store.claim(con, template_id, tenant, base_revision, "template")
            if obj["status"] not in {"draft", "failed"}:
                raise WorkflowError("仅草稿可进行 AI 学习。", 409)
            obj["status"] = "learning"
            obj["payload"]["instructions"] = instructions
            self.store.save(con, obj)
        return self.get(template_id, tenant)

    def learn(self, template_id: str, tenant: str):
        try:
            with self.store.engine.connect() as con:
                obj = self.store.get(con, template_id, tenant, "template")
                config = WorkflowConfig.model_validate(obj["payload"]["config"])
            sample_path = self.store.directory(template_id) / "sample.json"
            samples = json.loads(sample_path.read_text(encoding="utf-8")) if sample_path.exists() else []
            suggestion = AIClient(self.store.root, tenant).suggest(config, obj["payload"].get("instructions", ""), samples)
            changed = config.model_dump(mode="json")
            fields = {field["rule"]["name"]: field for field in changed["fields"]}
            notes = []
            for patch in suggestion.get("fields", []):
                if not isinstance(patch, dict) or set(patch) - {"id", "type", "required", "aliases", "reason"} or patch.get("id") not in fields:
                    raise WorkflowError("AI 字段建议包含未知字段或操作，请重试。", 502)
                rule = fields[patch["id"]]["rule"]
                for key in ("type", "required", "aliases"):
                    if key in patch:
                        rule[key] = patch[key]
                notes.append(f"AI 建议 / {rule['title']}：{str(patch.get('reason', '请确认字段规则'))[:500]}")
            for patch in suggestion.get("columns", []):
                if not isinstance(patch, dict) or set(patch) - {"file", "sheet", "column", "field", "operation", "argument", "sources", "part", "reason"}:
                    raise WorkflowError("AI 输出建议包含不支持的操作。", 502)
                indexes = [patch.get(key) for key in ("file", "sheet", "column")]
                if any(type(index) is not int or index < 0 for index in indexes):
                    raise WorkflowError("AI 输出建议索引无效。", 502)
                try:
                    column = changed["outputs"][indexes[0]]["sheets"][indexes[1]]["columns"][indexes[2]]
                except IndexError as exc:
                    raise WorkflowError("AI 输出建议引用了不存在的列。", 502) from exc
                if patch.get("operation") == "formula":
                    raise WorkflowError("AI 不能直接生成可执行公式，请在输出配置中明确设置。", 502)
                for key in ("field", "operation", "argument", "sources", "part"):
                    if key in patch:
                        column[key] = patch[key]
                notes.append(f"AI 建议 / {column['title']}：{str(patch.get('reason', '请确认输出规则'))[:500]}")
            config = WorkflowConfig.model_validate(changed)
            notes.extend(str(note)[:500] for note in suggestion.get("notes", [])[:30])
            with self.store.engine.begin() as con:
                current = self.store.get(con, template_id, tenant, "template")
                if current["revision"] != obj["revision"] or current["status"] != "learning":
                    return
                current["payload"].update(config=config.model_dump(mode="json"), notes=notes, confirmed=False,
                                          ai_model=AIClient(self.store.root, tenant).settings()["model"])
                current["payload"].pop("tested_hash", None)
                current["payload"].pop("test_result", None)
                current["payload"].pop("error", None)
                current["status"] = "draft"
                self.store.save(con, current)
        except Exception as exc:
            error = exc if isinstance(exc, WorkflowError) else WorkflowError("AI 草稿未通过规则校验，请人工检查模板或重试。", 502)
            TaskService(self.store).fail(template_id, tenant, error, "template")

    def trial(self, template_id: str, tenant: str, base_revision: int, sample_content: bytes | None = None):
        with self.store.engine.connect() as con:
            obj = self.store.get(con, template_id, tenant, "template")
            if obj["revision"] != base_revision or obj["status"] != "draft":
                raise WorkflowError("请保存当前草稿后再试运行。", 409)
        config = WorkflowConfig.model_validate(obj["payload"]["config"])
        directory = self.store.directory(template_id)
        # Invalidate prior evidence before reading or rendering a new sample.
        with self.store.engine.begin() as con:
            current = self.store.claim(con, template_id, tenant, base_revision, "template")
            current["payload"].pop("tested_hash", None)
            current["payload"]["test_result"] = {"passed": False, "error": "本次试运行尚未通过，请检查样例后重试。"}
            self.store.save(con, current)
            base_revision = current["revision"]
        if sample_content:
            sample_path = directory / f"sample-{identifier('trial')}.xlsx"
            sample_path.write_bytes(sample_content)
            discovered = discover(sample_path, config.fields)
            suggest_bindings(discovered, config)
            sample_rows = []
            for sheet in discovered:
                if sheet["mapping_notes"]:
                    raise WorkflowError("；".join(sheet["mapping_notes"]))
                field_ids = [item["field"] for item in sheet["suggested_bindings"]]
                if len(set(field_ids)) != len(field_ids):
                    raise WorkflowError("试运行样例存在重复字段映射，请先修正。")
                for row in sheet["rows"][sheet["header_row"] + sheet["header_depth"] - 1:]:
                    if any(row):
                        sample_rows.append({item["field"]: transform([row[c - 1] if c <= len(row) else "" for c in item["columns"]], item["operation"], item["argument"], item["part"]) for item in sheet["suggested_bindings"]})
        else:
            if obj["payload"].get("ai_model"):
                raise WorkflowError("AI 调整后请上传原始数据样例，以验证最新字段映射。")
            sample_path = directory / "sample.json"
            sample_rows = json.loads(sample_path.read_text(encoding="utf-8")) if sample_path.exists() else []
        if not sample_rows:
            raise WorkflowError("模板没有样例数据，请上传一份原始数据样例进行试运行。")
        rows = [{"id": f"sample-{index}", "values_json": {f.rule.name: cell_text(row.get(f.rule.name, f.default)) for f in config.fields},
                 "issues": []} for index, row in enumerate(sample_rows)]
        errors, _ = validate_records(rows, config)
        result = {"passed": False, "records": len(rows), "errors": errors,
                  "issues": [{"row": index + 1, **issue} for index, row in enumerate(rows) for issue in row["issues"]][:100]}
        if not errors:
            artifacts = []
            for index, spec in enumerate(config.outputs):
                reference = directory / "reference.xlsx" if index == 0 and obj["payload"].get("purpose", "output") == "output" else None
                path = directory / f"trial-{index}.xlsx"
                artifact = write_output(path, spec, config, rows, reference)
                artifact["name"] = spec.name
                artifacts.append(artifact)
            result.update(passed=True, artifacts=artifacts)
        with self.store.engine.begin() as con:
            current = self.store.claim(con, template_id, tenant, base_revision, "template")
            if current["status"] != "draft":
                raise WorkflowError("模板状态已变化，请刷新后重试。", 409)
            current["payload"]["test_result"] = result
            if result["passed"]:
                current["payload"]["tested_hash"] = content_hash(config.model_dump(mode="json"))
            else:
                current["payload"].pop("tested_hash", None)
            self.store.save(con, current)
        return self.get(template_id, tenant)

    def publish(self, template_id: str, tenant: str, base_revision: int):
        with self.store.engine.begin() as con:
            obj = self.store.claim(con, template_id, tenant, base_revision, "template")
            payload = obj["payload"]
            if obj["status"] != "draft" or not payload.get("confirmed"):
                raise WorkflowError("请确认字段和业务规则后再发布。")
            config = WorkflowConfig.model_validate(payload["config"])
            if payload.get("tested_hash") != content_hash(config.model_dump(mode="json")):
                raise WorkflowError("当前规则尚未通过样例试运行。")
            obj["status"] = "published"
            self.store.save(con, obj)
        return self.get(template_id, tenant)

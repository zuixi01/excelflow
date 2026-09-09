from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from sqlalchemy import delete, select

from .ai import AIClient
from .excel import cell_text, discover, field_matches, transform, validate_records, write_output
from .models import ColumnRule, MaintenanceField, OutputColumn, OutputFile, OutputSheet, TemplateEdit, WorkflowConfig, WorkflowError, content_hash, starter_config
from .repository import WorkflowStore, identifier, objects
from .tasks import TaskService, suggest_bindings, write_json
from ..product_workflow.fixed_template import normalize_header


class TemplateService:
    def __init__(self, store: WorkflowStore):
        self.store = store

    def list(self, tenant: str):
        with self.store.engine.connect() as con:
            items = [dict(row) for row in con.execute(select(objects).where(
                objects.c.tenant_id == tenant, objects.c.kind == "template").order_by(objects.c.updated_at.desc())).mappings()]
        return {"items": [self.store.public(item) for item in self._latest_versions(items)]}

    def definitions(self, tenant: str):
        with self.store.engine.connect() as con:
            items = [dict(row) for row in con.execute(select(objects).where(
                objects.c.tenant_id == tenant, objects.c.kind == "template", objects.c.status == "published"
            ).order_by(objects.c.updated_at.desc())).mappings()]
        return {"items": [self.store.public(item) for item in self._latest_versions(items)]}

    @staticmethod
    def _family_id(item: dict) -> str:
        return str(item["payload"].get("family") or item["id"])

    @classmethod
    def _latest_versions(cls, items: list[dict]) -> list[dict]:
        latest: dict[str, dict] = {}
        for item in items:
            family = cls._family_id(item)
            current = latest.get(family)
            version = int(item["payload"].get("version", 1))
            current_version = int(current["payload"].get("version", 1)) if current else -1
            if current is None or version > current_version or (version == current_version and item["updated_at"] > current["updated_at"]):
                latest[family] = item
        return sorted(latest.values(), key=lambda item: item["updated_at"], reverse=True)

    def get(self, template_id: str, tenant: str):
        with self.store.engine.connect() as con:
            return self.store.public(self.store.get(con, template_id, tenant, "template"))

    def delete(self, tenant: str, template_ids: list[str]):
        unique_ids = list(dict.fromkeys(template_ids))
        if len(unique_ids) != len(template_ids):
            raise WorkflowError("删除列表包含重复流程，请刷新后重试。")
        with self.store.engine.begin() as con:
            selected = [self.store.get(con, template_id, tenant, "template") for template_id in unique_ids]
            families = {self._family_id(item) for item in selected}
            templates = [dict(row) for row in con.execute(select(objects).where(
                objects.c.tenant_id == tenant, objects.c.kind == "template"
            )).mappings() if self._family_id(dict(row)) in families]
            active = [dict(row) for row in con.execute(select(objects.c.payload).where(
                objects.c.tenant_id == tenant, objects.c.kind == "task"
            )).mappings()]
            in_use = {item["payload"].get("template_id") for item in active}
            referenced = [item["payload"]["config"].get("name", item["id"]) for item in templates if item["id"] in in_use]
            if referenced:
                raise WorkflowError(f"以下流程已被任务引用，不能删除：{'、'.join(referenced)}", 409)
            deleted_ids = [item["id"] for item in templates]
            con.execute(delete(objects).where(objects.c.tenant_id == tenant, objects.c.kind == "template", objects.c.id.in_(deleted_ids)))
        for template_id in deleted_ids:
            shutil.rmtree(self.store.root / "files" / template_id, ignore_errors=True)
        return {"deleted": deleted_ids}

    def create(self, tenant: str, source_id: str | None = None, name: str = "新业务模板"):
        with self.store.engine.begin() as con:
            if source_id:
                source = self.store.get(con, source_id, tenant, "template")
                payload = {"config": source["payload"]["config"], "version": source["payload"].get("version", 1) + 1,
                           "family": source["payload"].get("family", source_id), "purpose": source["payload"].get("purpose", "output"), "notes": [], "confirmed": False}
            else:
                config = starter_config(name)
                payload = {"config": config.model_dump(mode="json"), "version": 1, "confirmed": False, "notes": []}
            obj = self.store.create(con, tenant, "template", payload)
            if source_id:
                for filename in ("reference.xlsx", "maintenance.xlsx", "output.xlsx", "import.xlsx", "sample.json"):
                    source_path = self.store.directory(source_id) / filename
                    if source_path.exists():
                        shutil.copyfile(source_path, self.store.directory(obj["id"]) / filename)
        return self.store.public(obj)

    def upload(self, tenant: str, name: str, content: bytes, purpose: str = "output"):
        if purpose not in {"output", "maintenance", "import"}:
            raise WorkflowError("模板用途无效。")
        result = self.create(tenant, name=name)
        return self._save_source(result["id"], tenant, content, purpose)

    def attach_source(self, template_id: str, tenant: str, content: bytes, purpose: str):
        if purpose not in {"output", "maintenance", "import"}:
            raise WorkflowError("模板用途无效。")
        return self._save_source(template_id, tenant, content, purpose)

    def _save_source(self, template_id: str, tenant: str, content: bytes, purpose: str):
        with self.store.engine.begin() as con:
            obj = self.store.get(con, template_id, tenant, "template")
            if obj["status"] == "published":
                raise WorkflowError("已发布版本不可修改，请先创建新版本。", 409)
            obj["status"] = "processing"
            obj["payload"].setdefault("purpose", purpose)
            obj["payload"]["parsing_purpose"] = purpose
            self.store.save(con, obj)
        directory = self.store.directory(template_id)
        (directory / f"{purpose}.xlsx").write_bytes(content)
        if purpose == "output":
            (directory / "reference.xlsx").write_bytes(content)
        return self.get(template_id, tenant)

    def retry(self, template_id: str, tenant: str, base_revision: int):
        with self.store.engine.begin() as con:
            obj = self.store.claim(con, template_id, tenant, base_revision, "template")
            if obj["status"] != "failed":
                raise WorkflowError("当前模板不需要重新解析。", 409)
            purpose = obj["payload"].get("parsing_purpose", obj["payload"].get("purpose", "output"))
            directory = self.store.directory(template_id)
            if not (directory / f"{purpose}.xlsx").exists() and not (directory / "reference.xlsx").exists():
                raise WorkflowError("找不到原始模板文件，请重新上传。", 404)
            obj["status"] = "processing"
            obj["payload"].pop("error", None)
            self.store.save(con, obj)
        return self.get(template_id, tenant)

    def reparse(self, template_id: str, tenant: str, base_revision: int):
        """Re-read an uploaded source file without requiring a new upload."""
        with self.store.engine.begin() as con:
            obj = self.store.claim(con, template_id, tenant, base_revision, "template")
            if obj["status"] == "published":
                raise WorkflowError("已发布版本不可重新解析，请先创建新版本。", 409)
            purpose = obj["payload"].get("parsing_purpose", obj["payload"].get("purpose", "output"))
            directory = self.store.directory(template_id)
            if not (directory / f"{purpose}.xlsx").exists() and not (directory / "reference.xlsx").exists():
                raise WorkflowError("找不到原始模板文件，请重新上传。", 404)
            obj["status"] = "processing"
            obj["payload"].pop("error", None)
            self.store.save(con, obj)
        return self.get(template_id, tenant)

    @staticmethod
    def _is_structural_template_column(header: str) -> bool:
        """Exclude labels used to describe a template layout, not product data."""
        return not header or header.startswith("未命名列 ") or normalize_header(header) == normalize_header("表头")

    @staticmethod
    def _maintenance_definition(sheets: list[dict]) -> tuple[list[MaintenanceField], list[dict], list[OutputSheet]]:
        """Build maintenance fields solely from the uploaded template structure."""
        entries = [
            (sheet["sheet"], item)
            for sheet in sheets
            for item in sheet["columns"]
            if not TemplateService._is_structural_template_column(item["header"])
        ]
        if not entries:
            raise WorkflowError("维护模板没有可用表头。请至少保留一列真实业务字段。")
        title_counts: dict[str, int] = {}
        for _, item in entries:
            title_counts[item["header"]] = title_counts.get(item["header"], 0) + 1

        fields: list[MaintenanceField] = []
        dynamic_by_sheet: dict[str, list[dict]] = {}
        output_columns: dict[str, list[OutputColumn]] = {}
        for sheet_name, item in entries:
            header = item["header"]
            field_id = "field_" + hashlib.sha256(f"{sheet_name}:{item['column']}:{header}".encode()).hexdigest()[:12]
            title = header if title_counts[header] == 1 else f"{sheet_name} / {header}"
            leaf_title = header.rsplit("/", 1)[-1].strip()
            aliases = []
            for alias in (header if title != header else "", leaf_title):
                if alias and normalize_header(alias) != normalize_header(title) and alias not in aliases:
                    aliases.append(alias)
            fields.append(MaintenanceField(rule=ColumnRule(
                name=field_id,
                title=title,
                aliases=aliases,
                type="string",
                normalize=["trim"],
            ), editable=True))
            output_columns.setdefault(sheet_name, []).append(OutputColumn(
                title=header, field=field_id,
            ))
            if any(normalize_header(part) == normalize_header("动态字段") for part in header.split("/")):
                dynamic_by_sheet.setdefault(sheet_name, []).append({"column": item["column"], "title": header, "field": field_id})

        dynamic_columns = [
            {"sheet": sheet_name, "columns": columns}
            for sheet_name, columns in dynamic_by_sheet.items()
        ]
        fallback_sheets = [
            OutputSheet(name=sheet_name, columns=columns)
            for sheet_name, columns in output_columns.items()
        ]
        return fields, dynamic_columns, fallback_sheets

    @staticmethod
    def _output_sheets(sheets: list[dict], config: WorkflowConfig, notes: list[str]) -> tuple[list[OutputSheet], list[dict]]:
        output_sheets: list[OutputSheet] = []
        examples: list[dict] = []
        for sheet in sheets:
            columns = []
            for item in sheet["columns"]:
                if TemplateService._is_structural_template_column(item["header"]):
                    continue
                matches = field_matches(item["header"], config.fields)
                field_id = matches[0] if len(matches) == 1 else ""
                if not field_id:
                    notes.append(f"{sheet['sheet']} / {item['header']} 未匹配维护字段，请在输出配置中确认。")
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
            if len({column.title for column in columns}) != len(columns):
                raise WorkflowError("模板存在重复表头，请使用不同字段名称后重新上传。")
            output_sheets.append(OutputSheet(name=sheet["sheet"], header_row=sheet["header_row"],
                                data_start_row=sheet["header_row"] + sheet["header_depth"], columns=columns))
            for row in sheet["rows"][sheet["header_row"] + sheet["header_depth"] - 1:][:10]:
                if any(row):
                    examples.append({column.field: row[index] for index, column in enumerate(columns)
                                     if column.field and index < len(row) and column.operation != "formula"})
        return output_sheets, examples

    def parse(self, template_id: str, tenant: str):
        try:
            with self.store.engine.connect() as con:
                obj = self.store.get(con, template_id, tenant, "template")
                config = WorkflowConfig.model_validate(obj["payload"]["config"])
                purpose = obj["payload"].get("parsing_purpose", obj["payload"].get("purpose", "output"))
            directory = self.store.directory(template_id)
            source_path = directory / f"{purpose}.xlsx"
            if not source_path.exists():
                source_path = directory / "reference.xlsx"
            sheets = discover(source_path, config.fields, template=True)
            output_sheets = []
            examples = []
            dynamic_columns = obj["payload"].get("dynamic_columns", [])
            notes = ["已直接读取工作表、表头及列宽。字段含义与业务约束请确认后发布。"]
            if purpose == "maintenance":
                config.fields, dynamic_columns, fallback_sheets = self._maintenance_definition(sheets)
                config.import_rules = []
                config.unique_key = []
                config.cross_checks = []
                config.outputs = [OutputFile(name="维护字段输出.xlsx", sheets=fallback_sheets)]
                notes.append(f"已按维护模板实际表头生成 {len(config.fields)} 个维护字段，字段顺序与 Excel 保持一致。")
                if dynamic_columns:
                    count = sum(len(item["columns"]) for item in dynamic_columns)
                    notes.append(f"其中 {count} 个字段位于“动态字段”分组，已保留为接口填充元数据。")
                output_path = directory / "output.xlsx"
                if output_path.exists():
                    output_source = discover(output_path, config.fields, template=True)
                    output_sheets, output_examples = self._output_sheets(output_source, config, notes)
                    config.outputs = [OutputFile(name="最终商品表.xlsx", sheets=output_sheets)]
                    examples.extend(output_examples)
            else:
                # An output template may be the first file a user imports.  In
                # that case its real headers are the only available field set.
                if purpose == "output" and not (directory / "maintenance.xlsx").exists():
                    config.fields, dynamic_columns, _ = self._maintenance_definition(sheets)
                    config.import_rules = []
                    config.unique_key = []
                    config.cross_checks = []
                    notes.append(f"尚未上传维护模板，已按输出模板实际表头生成 {len(config.fields)} 个维护字段。")
                output_sheets, examples = self._output_sheets(sheets, config, notes)
            if purpose == "output":
                config.outputs = [OutputFile(name="最终商品表.xlsx", sheets=output_sheets)]
            config = WorkflowConfig.model_validate(config.model_dump(mode="json"))
            write_json(self.store.directory(template_id) / "sample.json", examples)
            with self.store.engine.begin() as con:
                current = self.store.get(con, template_id, tenant, "template")
                sources = current["payload"].setdefault("sources", {})
                sources[purpose] = {"filename": source_path.name, "sheets": [{"name": sheet["sheet"], "columns": len(sheet["columns"])} for sheet in sheets]}
                current["payload"].update(config=config.model_dump(mode="json"), notes=notes, confirmed=False,
                                          sample_count=len(examples), dynamic_columns=dynamic_columns,
                                          structure=[{key: value for key, value in sheet.items() if key not in {"rows", "preview"}} for sheet in sheets])
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
            WorkflowConfig.model_validate(payload["config"])
            obj["status"] = "published"
            self.store.save(con, obj)
        return self.get(template_id, tenant)

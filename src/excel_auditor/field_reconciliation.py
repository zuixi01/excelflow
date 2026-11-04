from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pydantic import Field, model_validator
from rapidfuzz import fuzz

from .engine import _validate
from .models import ColumnRule, StrictModel, normalize_header
from .normalization import parse_value
from .product_workflow.sheet_discovery import _header_depth, flatten_headers, merged_value_map
from .service import AuditService, JobCancelled
from .standard_sources import ManagedHttpSource
from .strict_serialization import dump_json_exact, load_json_strict
from .workbook import _inspect_sheet_xml_structure, _validate_xml_part, sha256_file
from .workflow.ai import AIClient


MAX_ROWS = 20_000
MAX_COLUMNS = 200
MAX_CELLS = 2_000_000


class PlatformField(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=128)
    type: Literal["text", "string", "integer", "decimal", "date", "datetime", "boolean", "enum", "id_code"] = "text"
    required: bool = False
    sensitive: bool = False
    enum_values: list[str] = Field(default_factory=list, max_length=500)
    compare_mode: Literal["exact", "ignore_case", "numeric", "date", "semantic"] = "exact"

    @model_validator(mode="after")
    def contract(self):
        if self.compare_mode == "semantic" and self.type not in {"text", "string"}:
            raise ValueError("semantic comparison is restricted to text fields")
        if self.type == "enum" and not self.enum_values:
            raise ValueError("enum fields require enum_values")
        self.rule()
        return self

    def rule(self) -> ColumnRule:
        return ColumnRule(name=self.id, title=self.title, type="string" if self.type == "text" else self.type,
                          required=self.required, enum_values=self.enum_values, normalize=["trim"],
                          compare={"allow_naive_datetime": True} if self.type == "datetime" else {})


class PlatformEnvelope(StrictModel):
    schema_version: str = Field(min_length=1, max_length=128)
    primary_key: list[str] = Field(default_factory=list, max_length=20)
    fields: list[PlatformField] = Field(min_length=1, max_length=200)
    records: list[dict[str, Any]] | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def contract(self):
        ids = {field.id for field in self.fields}
        if len(ids) != len(self.fields) or len(set(self.primary_key)) != len(self.primary_key):
            raise ValueError("field ids and primary key fields must be unique")
        if not set(self.primary_key) <= ids or (self.records is not None and not self.primary_key):
            raise ValueError("record comparison requires declared primary key fields")
        seen = set()
        for record in self.records or []:
            key = record_key(record, self.primary_key)
            if any(not part for part in key) or key in seen:
                raise ValueError("platform records contain an empty or duplicate primary key")
            seen.add(key)
        return self


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def token(value: Any) -> str:
    return normalize_header(text(value)).casefold()


def record_key(record: dict, keys: list[str]) -> tuple[str, ...]:
    # Business identifiers retain punctuation, case and leading zeros.
    return tuple(text(record.get(key)) for key in keys)


def write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(dump_json_exact(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return load_json_strict(path.read_text(encoding="utf-8"), preserve_decimal=True)


def open_supplier_book(path: Path, *, max_upload_bytes: int = 50 * 1024 * 1024,
                       max_uncompressed_bytes: int = 100 * 1024 * 1024):
    if path.stat().st_size > max_upload_bytes or not zipfile.is_zipfile(path):
        raise ValueError(f"供应商文件不是有效的 Excel，或超过 {max_upload_bytes // (1024 * 1024)} MiB。")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [item.filename.replace("\\", "/").lower() for item in entries]
        if len(entries) > 4000 or len(set(names)) != len(names):
            raise ValueError("Excel 包含过多或重复的文件条目。")
        if any(name.startswith("/") or ".." in name.split("/") for name in names):
            raise ValueError("Excel 文件条目无效。")
        if sum(item.file_size for item in entries) > max_uncompressed_bytes:
            raise ValueError(f"Excel 解压后超过 {max_uncompressed_bytes // (1024 * 1024)} MiB，请拆分后上传。")
        cells = 0
        for entry in entries:
            if entry.flag_bits & 1:
                raise ValueError("请先解除 Excel 文件加密。")
            if entry.filename.lower().endswith((".xml", ".rels")):
                _validate_xml_part(archive, entry)
            if entry.filename.lower().startswith("xl/worksheets/") and entry.filename.lower().endswith(".xml"):
                _, (rows, columns) = _inspect_sheet_xml_structure(archive, entry)
                cells += rows * columns
                if rows > MAX_ROWS + 100 or columns > MAX_COLUMNS or cells > MAX_CELLS:
                    raise ValueError("Excel 超出 20,000 行、200 列或 200 万单元格容量，请拆分上传。")
    book = load_workbook(path, data_only=False, keep_links=False)
    if len(book.worksheets) > 50:
        book.close()
        raise ValueError("Excel 最多支持 50 个工作表。")
    return book


class FieldReconciliationService:
    def __init__(self, audit_service: AuditService, managed_http: ManagedHttpSource | None, ai_root: Path):
        self.audit_service, self.managed_http, self.ai_root = audit_service, managed_http, ai_root

    def run(self, job_id: str, excel_path: Path, connection_id: str, scope: dict[str, Any], tenant_id: str) -> None:
        book = None
        try:
            directory = self.audit_service.job_directory(job_id)
            self._status(job_id, status="loading_standard", progress=10, workflow="field_reconciliation",
                         error_code=None, error_message_safe=None, warnings=[])
            self.audit_service._check_cancelled(job_id)
            snapshot_path = directory / "field-standard-snapshot.json"
            if snapshot_path.exists():
                snapshot = read_json(snapshot_path)
                envelope = PlatformEnvelope.model_validate(snapshot["contract"])
                expected = hashlib.sha256(dump_json_exact(snapshot["contract"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if expected != snapshot["content_sha256"]:
                    raise ValueError("字段标准快照校验失败。")
            else:
                envelope, metadata = self._load_standard(connection_id, scope)
                contract = envelope.model_dump(mode="python")
                snapshot = {"captured_at": datetime.now(UTC).isoformat(), "connection_id": connection_id,
                            "scope": scope, "source": metadata, "contract": contract,
                            "content_sha256": hashlib.sha256(dump_json_exact(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
                write_json(snapshot_path, snapshot)
            self._status(job_id, status="reading_excel", progress=25, schema_version=envelope.schema_version,
                         schema_hash=snapshot["content_sha256"], input_sha256=sha256_file(excel_path),
                         comparison_mode="records" if envelope.records is not None else "fields")
            book = open_supplier_book(excel_path)
            sheets = self._read_sheets(book, envelope.fields)
            self.audit_service._check_cancelled(job_id)
            ai = AIClient(self.ai_root, tenant_id)
            settings = ai.settings()
            warnings: list[str] = []
            saved_mappings = directory / "field-input-mappings.json"
            mappings = read_json(saved_mappings) if saved_mappings.exists() else self._map_columns(sheets, envelope.fields, ai, settings, warnings)
            if not saved_mappings.exists():
                write_json(saved_mappings, mappings)
            decisions_path = directory / "field-decisions.json"
            if decisions_path.exists():
                for decision in read_json(decisions_path):
                    item = next(item for item in mappings if item["sheet"] == decision["sheet"] and item["column"] == decision["column"])
                    item.update(field_id=decision["field_id"], status="accepted" if decision["field_id"] else "ignored",
                                method="human", confidence=1.0, reason="运营人员确认。")
            self._status(job_id, status="comparing", progress=55)
            results, issues, evidence = self._compare(job_id, sheets, mappings, envelope, ai, settings, warnings)
            summary = {"source_records": len(results), "passed_records": sum(row["status"] == "passed" for row in results),
                       "failed_records": sum(row["status"] == "failed" for row in results),
                       "review_records": sum(row["status"] == "manual_review" for row in results),
                       "mapped_columns": sum(item["status"] == "accepted" for item in mappings),
                       "pending_columns": sum(item["status"] == "manual_review" for item in mappings)}
            report = {"schema_version": envelope.schema_version, "schema_hash": snapshot["content_sha256"],
                      "comparison_mode": "records" if envelope.records is not None else "fields",
                      "fields": [field.model_dump() for field in envelope.fields], "mappings": mappings,
                      "results": results, "issues": issues, "semantic_evidence": evidence, "summary": summary,
                      "warnings": warnings, "model": settings["model"] if settings["configured"] else None,
                      "prompt_version": "field-reconciliation-v1", "revision": self.audit_service.status(job_id).get("revision", 0)}
            self.audit_service._check_cancelled(job_id)
            write_json(directory / "field-reconciliation-report.json", report)
            self._write_excel(directory / "field-reconciliation-result.xlsx", report)
            artifacts = {"field_report": "field-reconciliation-report.json", "field_excel": "field-reconciliation-result.xlsx", "field_snapshot": snapshot_path.name}
            object_keys = self.audit_service._upload_artifacts(job_id, directory, list(artifacts.values()))
            needs_review = summary["pending_columns"] or any(item["severity"] == "review" for item in issues)
            self._status(job_id, status="manual_review" if needs_review else "completed", progress=100,
                         issue_count=len(issues), summary=summary, warnings=warnings, artifacts=artifacts,
                         object_keys=object_keys, completed_at=datetime.now(UTC).isoformat())
        except JobCancelled:
            self._status(job_id, status="cancelled", progress=100, completed_at=datetime.now(UTC).isoformat())
        except Exception as exc:
            write_json(self.audit_service.job_directory(job_id) / "field-reconciliation-diagnostic.json", {"exception_type": type(exc).__name__})
            safe_message = str(exc) if isinstance(exc, ValueError) and any(str(exc).startswith(prefix) for prefix in ("Excel", "供应商", "请先", "字段标准快照")) else "平台字段标准读取或文件核对失败，请检查连接配置与文件结构。"
            self._status(job_id, status="failed", progress=100, completed_at=datetime.now(UTC).isoformat(),
                         error_code="FIELD_RECONCILIATION_FAILED", error_message_safe=safe_message)
        finally:
            if book is not None:
                book.close()

    def _load_standard(self, connection_id: str, scope: dict) -> tuple[PlatformEnvelope, dict]:
        if self.managed_http is None:
            raise ValueError("platform connection is not configured")
        connection = self.managed_http.registry.get(connection_id)
        config = connection.field_reconciliation
        if config is None:
            raise ValueError("connection has no field standard endpoint")
        payload, metadata = self.managed_http.fetch_object(connection_id, config.path, config.method,
                                                         static_parameters=config.static_parameters,
                                                         parameter_mapping=config.parameter_mapping, parameters=scope)
        envelope = PlatformEnvelope.model_validate(payload.get("data", payload))
        if len(envelope.records or []) > connection.max_records:
            raise ValueError("platform record limit exceeded")
        return envelope, metadata

    def _read_sheets(self, book, fields: list[PlatformField]) -> list[dict]:
        choices = {token(value) for field in fields for value in [field.id, field.title, *field.aliases]}
        sheets = []
        count = 0
        for sheet in book.worksheets:
            if sheet.sheet_state != "visible":
                continue
            content = [cell for cell in sheet._cells.values() if cell.value is not None]
            if not content:
                continue
            last_row = max(cell.row for cell in content)
            last_column = max(cell.column for cell in content)
            if last_row < 2:
                continue
            if last_row > MAX_ROWS + 100 or last_column > MAX_COLUMNS:
                raise ValueError("Excel 工作表超出容量，请拆分上传。")
            scores = []
            for row in sheet.iter_rows(max_row=min(last_row - 1, 30), max_col=last_column):
                labels = [text(cell.value) for cell in row]
                exact = sum(token(value) in choices for value in labels if value)
                words = sum(bool(value) and not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value) for value in labels)
                scores.append((exact * 100 + min(words, 20), -row[0].row))
            header_row = -max(scores)[1]
            depth = _header_depth(sheet, header_row, last_row, last_column)
            merged = merged_value_map(sheet, last_row=last_row, last_column=last_column)
            headers = flatten_headers(sheet, merged, header_row=header_row, header_depth=depth, last_column=last_column)
            rows = []
            for row in sheet.iter_rows(min_row=header_row + depth, max_row=last_row, max_col=last_column):
                values = {}
                formulas = []
                for cell in row:
                    value = merged.get((cell.row, cell.column), cell.value)
                    if cell.data_type == "f":
                        formulas.append(cell.column)
                    if isinstance(value, int) and not isinstance(value, bool) and re.fullmatch(r"0{2,20}", cell.number_format or ""):
                        value = str(value).zfill(len(cell.number_format))
                    values[cell.column] = value.isoformat() if isinstance(value, (datetime, date)) else value
                if any(text(value) for value in values.values()):
                    rows.append({"sheet": sheet.title, "row": row[0].row, "values": values, "formulas": formulas})
            count += len(rows)
            if count > MAX_ROWS:
                raise ValueError("Excel 一批最多支持 20,000 条记录，请拆分上传。")
            if rows:
                sheets.append({"sheet": sheet.title, "header_row": header_row, "headers": headers, "rows": rows})
        if not sheets:
            raise ValueError("供应商 Excel 没有可识别的数据工作表。")
        return sheets

    def _map_columns(self, sheets: list[dict], fields: list[PlatformField], ai: AIClient, settings: dict, warnings: list[str]) -> list[dict]:
        owners: dict[str, set[str]] = {}
        for field in fields:
            for value in [field.id, field.title, *field.aliases]:
                owners.setdefault(token(value), set()).add(field.id)
        mappings = []
        for sheet in sheets:
            current = []
            for column, header in enumerate(sheet["headers"], 1):
                ids = owners.get(token(header), set())
                candidates = [{"field_id": field.id, "title": field.title,
                               "confidence": max(fuzz.ratio(token(header), token(value)) for value in [field.id, field.title, *field.aliases]) / 100}
                              for field in fields] if header else []
                candidates = sorted([c for c in candidates if c["confidence"] >= .6], key=lambda c: -c["confidence"])[:3]
                current.append({"sheet": sheet["sheet"], "column": column, "header": header or f"未命名列 {column}",
                                "field_id": next(iter(ids)) if len(ids) == 1 else None,
                                "status": "accepted" if len(ids) == 1 else "manual_review",
                                "method": "exact" if len(ids) == 1 else "unmapped", "confidence": 1.0 if len(ids) == 1 else 0,
                                "reason": "名称或平台别名精确匹配。" if len(ids) == 1 else "请选择对应字段或保留为附加列。", "candidates": candidates})
            pending = [item for item in current if item["status"] == "manual_review"]
            if pending and settings["configured"]:
                try:
                    for start in range(0, len(pending), 25):
                        columns = [{"column": item["column"], "header": item["header"]} for item in pending[start:start + 25]]
                        # Samples are opt-in; an unmapped column cannot yet be reliably classified as non-sensitive.
                        suggestions = ai.reconcile_field_mappings([field.model_dump() for field in fields], columns)
                        by_column = {item["column"]: item for item in pending}
                        for suggestion in suggestions:
                            item = by_column[suggestion["column"]]
                            item.update(field_id=suggestion["field_id"], method="semantic_model", confidence=suggestion["confidence"],
                                        reason=suggestion["reason"] + "（语义建议待确认）")
                except Exception:
                    warnings.append(f"{sheet['sheet']}：语义字段识别暂不可用，可手动确认字段。")
            elif pending:
                warnings.append(f"{sheet['sheet']}：尚未配置语义分析模型，请确认未识别的字段。")
            counts = Counter(item["field_id"] for item in current if item["field_id"])
            for item in current:
                if item["field_id"] and counts[item["field_id"]] > 1:
                    item.update(status="manual_review", reason="多列指向同一平台字段，请选择其中一列。")
            mappings.extend(current)
        return mappings

    def _compare(self, job_id: str, sheets: list[dict], mappings: list[dict], envelope: PlatformEnvelope, ai: AIClient, settings: dict, warnings: list[str]):
        fields = {field.id: field for field in envelope.fields}
        rules = {field.id: field.rule() for field in envelope.fields}
        platform = {record_key(record, envelope.primary_key): record for record in envelope.records or []}
        issues, results, evidence, requests, deferred = [], [], [], [], {}
        source_counts: Counter = Counter()
        for sheet in sheets:
            selected = {item["column"]: item["field_id"] for item in mappings if item["sheet"] == sheet["sheet"] and item["status"] == "accepted"}
            missing = [field for field in envelope.fields if (field.required or field.id in envelope.primary_key) and field.id not in selected.values()]
            pending = any(item["status"] == "manual_review" for item in mappings if item["sheet"] == sheet["sheet"])
            for field in missing:
                issues.append({"issue_type": "missing_field", "severity": "review" if pending else "error", "sheet": sheet["sheet"],
                               "field_id": field.id, "field_title": field.title, "message": f"缺少必需字段：{field.title}。"})
            for row in sheet["rows"]:
                source = {field_id: row["values"].get(column) for column, field_id in selected.items()}
                result = {"sheet": row["sheet"], "row": row["row"], "source": source, "standard": {},
                          "raw_source": {str(column): value for column, value in row["values"].items()},
                          "normalized_source": {},
                          "status": "manual_review" if pending else "failed" if missing else "passed", "differences": []}
                results.append(result)
                key = record_key(source, envelope.primary_key)
                if envelope.primary_key:
                    if any(not part for part in key):
                        self._issue(result, issues, None, "empty_primary_key", "业务主键缺失，未关联标准记录。")
                    else:
                        source_counts[key] += 1
                target = platform.get(key) if envelope.records is not None and all(key) else None
                if envelope.records is not None:
                    if target is None:
                        self._issue(result, issues, None, "standard_record_missing", "平台没有唯一对应的标准记录，未执行值比对。")
                    else:
                        result["standard"] = target
                for column, field_id in selected.items():
                    field, value = fields[field_id], source[field_id]
                    parsed = parse_value(value, rules[field_id])
                    error = _validate(parsed, rules[field_id]) if parsed.valid else self._type_error(field)
                    if column in row["formulas"]:
                        error = "包含公式，请在供应商表中粘贴为值后重新上传。"
                    if field.required and not text(value):
                        error = "必填字段为空。"
                    if error:
                        self._issue(result, issues, field, "invalid_value", str(error), value)
                        continue
                    result["normalized_source"][field_id] = parsed.normalized.isoformat() if isinstance(parsed.normalized, (datetime, date)) else parsed.normalized
                    if target is None or field_id in envelope.primary_key:
                        continue
                    standard = target.get(field_id)
                    if self._equal(value, standard, field):
                        continue
                    if field.compare_mode == "semantic" and text(value) and text(standard):
                        compare_id = f"{len(results)}:{field_id}"
                        if settings["configured"] and settings["include_samples"] and not field.sensitive and max(len(text(value)), len(text(standard))) <= 1000 and len(requests) < 1000:
                            requests.append({"id": compare_id, "field": field.title, "description": field.description,
                                             "supplier_value": text(value), "platform_value": text(standard)})
                            deferred[compare_id] = (result, field, value, standard)
                        else:
                            if field.sensitive:
                                reason = "敏感字段未发送给模型，请人工核实原值。"
                            elif not settings["configured"]:
                                reason = "尚未配置语义分析模型，请配置后重试。"
                            elif not settings["include_samples"]:
                                reason = "尚未授权发送非敏感字段值，请授权后重试。"
                            else:
                                reason = "超出语义分析容量，请拆分文件或人工核实原值。"
                            self._issue(result, issues, field, "semantic_pending", reason, value, standard, "review")
                    else:
                        self._issue(result, issues, field, "value_mismatch", "供应商值与平台值不同。", value, standard)
        for result in results:
            if envelope.primary_key and source_counts[record_key(result["source"], envelope.primary_key)] > 1:
                self._issue(result, issues, None, "duplicate_primary_key", "供应商表存在重复业务主键，请核实后重新上传。")
        for start in range(0, len(requests), 25):
            self.audit_service._check_cancelled(job_id)
            try:
                verdicts = ai.reconcile_semantic_values(requests[start:start + 25])
            except Exception:
                verdicts = {}
                warnings.append("部分文本语义比较暂不可用，相关值已列为待核实。")
            for request in requests[start:start + 25]:
                result, field, value, standard = deferred[request["id"]]
                verdict = verdicts.get(request["id"], {"verdict": "unknown", "reason": "模型未返回可验证的结论。"})
                evidence.append({"sheet": result["sheet"], "row": result["row"], "field_id": field.id, **verdict})
                if verdict["verdict"] != "equivalent":
                    unknown = verdict["verdict"] == "unknown"
                    self._issue(result, issues, field, "semantic_pending" if unknown else "semantic_mismatch", verdict["reason"], value, standard, "review" if unknown else "error")
        return results, issues, evidence

    @staticmethod
    def _type_error(field: PlatformField) -> str:
        expected = {"integer": "整数", "decimal": "数值", "date": "日期", "datetime": "日期时间",
                    "boolean": "布尔值", "enum": "平台允许的枚举值"}.get(field.type, "平台要求的格式")
        return f"字段格式不符合要求，应为{expected}。"

    @staticmethod
    def _equal(left: Any, right: Any, field: PlatformField) -> bool:
        parsed_left, parsed_right = parse_value(left, field.rule()), parse_value(right, field.rule())
        if not parsed_left.valid or not parsed_right.valid:
            return False
        if field.compare_mode == "numeric" and field.type in {"text", "string"}:
            numeric = ColumnRule(name=field.id, title=field.title, type="decimal", normalize=["trim"])
            parsed_left, parsed_right = parse_value(left, numeric), parse_value(right, numeric)
            return parsed_left.valid and parsed_right.valid and parsed_left.normalized == parsed_right.normalized
        if field.compare_mode == "date" and field.type in {"text", "string"}:
            date_rule = ColumnRule(name=field.id, title=field.title, type="date", normalize=["trim"])
            parsed_left, parsed_right = parse_value(left, date_rule), parse_value(right, date_rule)
            return parsed_left.valid and parsed_right.valid and parsed_left.normalized == parsed_right.normalized
        if field.compare_mode == "ignore_case":
            return text(parsed_left.normalized).casefold() == text(parsed_right.normalized).casefold()
        return parsed_left.normalized == parsed_right.normalized

    @staticmethod
    def _issue(result: dict, issues: list, field: PlatformField | None, kind: str, message: str, value=None, standard=None, severity="error"):
        if severity == "error" or result["status"] != "failed":
            result["status"] = "failed" if severity == "error" else "manual_review"
        if field and field.id not in result["differences"]:
            result["differences"].append(field.id)
        issues.append({"issue_type": kind, "severity": severity, "sheet": result["sheet"], "row": result["row"],
                       "field_id": field.id if field else None, "field_title": field.title if field else None,
                       "supplier_value": value, "platform_value": standard, "message": message})

    @staticmethod
    def _write_excel(path: Path, report: dict):
        book = Workbook()
        summary = book.active
        summary.title = "核对结果"
        summary.append(["状态", "工作表", "来源行", "差异字段"])
        for item in report["results"]:
            summary.append([{"passed": "通过", "failed": "不通过", "manual_review": "待确认"}[item["status"]], item["sheet"], item["row"], "、".join(item["differences"])])
        mapping = book.create_sheet("字段映射")
        mapping.append(["工作表", "列", "供应商字段", "平台字段", "状态", "方式", "依据"])
        for item in report["mappings"]:
            mapping.append([item["sheet"], item["column"], item["header"], item["field_id"], item["status"], item["method"], item["reason"]])
        issues = book.create_sheet("问题清单")
        issues.append(["工作表", "来源行", "字段", "级别", "供应商值", "平台值", "说明"])
        for item in report["issues"]:
            issues.append([item.get("sheet"), item.get("row"), item.get("field_title"), item["severity"], text(item.get("supplier_value")), text(item.get("platform_value")), item["message"]])
        values = book.create_sheet("标准化数据")
        values.append(["来源工作表", "来源行", *[field["title"] for field in report["fields"]]])
        for item in report["results"]:
            values.append([item["sheet"], item["row"], *[text(item["normalized_source"].get(field["id"], item["source"].get(field["id"]))) for field in report["fields"]]])
        raw = book.create_sheet("供应商原始数据")
        column_count = max((item["column"] for item in report["mappings"]), default=0)
        raw.append(["来源工作表", "来源行", *[f"第 {column} 列" for column in range(1, column_count + 1)]])
        for item in report["results"]:
            raw.append([item["sheet"], item["row"], *[item["raw_source"].get(str(column)) for column in range(1, column_count + 1)]])
        semantics = book.create_sheet("语义依据")
        semantics.append(["工作表", "来源行", "字段", "模型结论", "依据"])
        for item in report["semantic_evidence"]:
            semantics.append([item["sheet"], item["row"], item["field_id"], item["verdict"], item["reason"]])
        trace = book.create_sheet("_标准快照")
        trace.sheet_state = "hidden"
        trace.append(["schema_version", report["schema_version"]])
        trace.append(["content_sha256", report["schema_hash"]])
        for sheet in book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for row in sheet:
                for cell in row:
                    if isinstance(cell.value, str):
                        cell.value = ILLEGAL_CHARACTERS_RE.sub("", cell.value)[:32767]
                        cell.data_type = "s"
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="22664E")
            for index in range(1, sheet.max_column + 1):
                sheet.column_dimensions[get_column_letter(index)].width = 24 if index < sheet.max_column else 48
        temporary = path.with_suffix(".tmp.xlsx")
        book.save(temporary)
        book.close()
        temporary.replace(path)

    def _status(self, job_id: str, **changes):
        self.audit_service._write_status(job_id, {**self.audit_service.status(job_id), **changes})

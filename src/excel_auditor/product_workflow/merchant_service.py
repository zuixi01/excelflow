from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..service import AuditService, JobCancelled
from .merchant_extractor import extract_merchant_records
from .platform_product_source import EmptyPlatformProductSource, FixturePlatformProductSource
from .reconciler import reconcile_products
from .sheet_discovery import discover_product_sheets
from .workbook_writer import write_reconciliation_workbook


class MerchantReconciliationService:
    """Normalize multi-sheet merchant workbooks and optionally reconcile platform products."""

    def __init__(self, audit_service: AuditService) -> None:
        self.audit_service = audit_service

    def run(
        self,
        job_id: str,
        excel_path: Path,
        platform_path: Path | None = None,
        *,
        name_match_threshold: float = 90,
    ) -> None:
        directory = self.audit_service.job_directory(job_id)
        workbook = None
        try:
            self._status(job_id, status="discovering", progress=15, workflow="merchant_product_reconciliation")
            self.audit_service._check_cancelled(job_id)
            input_sha256 = _sha256_file(excel_path)
            workbook = load_workbook(excel_path, data_only=False, read_only=False, keep_links=False)
            discovered = discover_product_sheets(workbook)
            if not discovered:
                raise ValueError("MERCHANT_WORKBOOK_INVALID: no product worksheet was recognized")
            merchant_records = extract_merchant_records(workbook, discovered)
            if not merchant_records:
                raise ValueError("MERCHANT_WORKBOOK_INVALID: no product records were found")

            self._status(
                job_id,
                status="matching",
                progress=55,
                workflow="merchant_product_reconciliation",
                input_sha256=input_sha256,
                recognized_sheet_count=len(discovered),
                source_record_count=len(merchant_records),
            )
            self.audit_service._check_cancelled(job_id)
            source = FixturePlatformProductSource(platform_path) if platform_path else EmptyPlatformProductSource()
            platform_records, platform_extensions = source.load()
            result = reconcile_products(
                discovered,
                merchant_records,
                platform_records,
                platform_extensions,
                threshold=name_match_threshold,
                platform_available=platform_path is not None,
            )

            self._status(job_id, status="rendering", progress=80, workflow="merchant_product_reconciliation")
            output_path = directory / "product-result.xlsx"
            result_path = directory / "product-result.json"
            issues_path = directory / "product-issues.jsonl"
            write_reconciliation_workbook(output_path, result)
            summary = _summary(result)
            _write_json_atomic(result_path, _result_payload(
                result,
                summary=summary,
                input_sha256=input_sha256,
                name_match_threshold=name_match_threshold,
                platform_provided=platform_path is not None,
            ))
            _write_jsonl_atomic(issues_path, [asdict(issue) for issue in result.issues])
            artifacts = {
                "product_excel": output_path.name,
                "product_result": result_path.name,
                "product_issues": issues_path.name,
            }
            object_keys = self.audit_service._upload_artifacts(
                job_id,
                directory,
                [output_path.name, result_path.name, issues_path.name],
            )
            self._status(
                job_id,
                status="completed",
                progress=100,
                workflow="merchant_product_reconciliation",
                completed_at=datetime.now(UTC).isoformat(),
                input_sha256=input_sha256,
                output_sha256=_sha256_file(output_path),
                platform_provided=platform_path is not None,
                name_match_threshold=name_match_threshold,
                issue_count=len(result.issues),
                summary=summary,
                artifacts=artifacts,
                object_keys=object_keys,
            )
        except JobCancelled:
            self._status(
                job_id,
                status="cancelled",
                progress=100,
                workflow="merchant_product_reconciliation",
                completed_at=datetime.now(UTC).isoformat(),
            )
        except Exception as exc:
            diagnostic = directory / "merchant-reconciliation-diagnostic.json"
            _write_json_atomic(diagnostic, {"exception_type": type(exc).__name__, "message": str(exc)[:2000]})
            self._status(
                job_id,
                status="failed",
                workflow="merchant_product_reconciliation",
                completed_at=datetime.now(UTC).isoformat(),
                error_code="MERCHANT_RECONCILIATION_FAILED",
                error_message_safe="商品表处理失败，请检查工作表结构或平台测试数据。",
            )
        finally:
            if workbook is not None:
                workbook.close()

    def _status(self, job_id: str, **changes: Any) -> None:
        current = self.audit_service.status(job_id)
        self.audit_service._write_status(job_id, {**current, **changes})


def _summary(result: Any) -> dict[str, int]:
    return {
        "recognized_sheets": len(result.discovered_sheets),
        "source_records": len(result.merchant_records),
        "platform_records": len(result.platform_records),
        "matched_records": len(result.matches),
        "differences": result.difference_count,
        "merchant_only": result.merchant_only_count,
        "platform_only": result.platform_only_count,
        "output_records": len(result.output_records),
    }


def _result_payload(
    result: Any,
    *,
    summary: dict[str, int],
    input_sha256: str,
    name_match_threshold: float,
    platform_provided: bool,
) -> dict[str, Any]:
    return {
        "workflow": "merchant_product_reconciliation",
        "version": "1.0",
        "input_sha256": input_sha256,
        "platform_provided": platform_provided,
        "name_match_threshold": name_match_threshold,
        "summary": summary,
        "discovered_sheets": [asdict(sheet) for sheet in result.discovered_sheets],
        "records": [
            {
                "output_row": index,
                "status": record.row_status,
                "source_sheet": record.source_sheet,
                "source_row": record.source_row,
                "platform_record_id": record.platform_record_id,
                "match_score": record.match_score,
                "fixed": record.fixed,
                "platform_extensions": record.platform_extras,
                "difference_fields": sorted(record.difference_fields),
            }
            for index, record in enumerate(result.output_records, start=4)
        ],
        "issues": [asdict(issue) for issue in result.issues],
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default))
            handle.write("\n")
    temporary.replace(path)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

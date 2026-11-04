import importlib
from io import BytesIO
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
import pytest
from pydantic import ValidationError

from excel_auditor.field_reconciliation import FieldReconciliationService, PlatformEnvelope, read_json
from excel_auditor.service import AuditService
from excel_auditor.standard_sources import ConnectionRegistry, ManagedHttpSource
from excel_auditor.workflow.ai import AIClient, AISettings


def contract(records=False):
    result = {"schema_version": "2026.10", "primary_key": ["sku"], "fields": [
        {"id": "sku", "title": "SKU", "type": "string", "required": True},
        {"id": "name", "title": "Name", "compare_mode": "semantic", "required": True},
        {"id": "price", "title": "Price", "type": "decimal"},
    ]}
    if records:
        result["records"] = [{"sku": "001", "name": "Lamp", "price": "100.00"}]
    return result


def workbook(headers=None, rows=None, sheets=1):
    book = Workbook()
    for index in range(sheets):
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = f"Supplier-{index}"
        sheet.append(headers or ["SKU", "Name", "Price"])
        for row in rows or [["001", "Lamp", "99.50"]]:
            sheet.append(row)
    output = BytesIO()
    book.save(output); book.close()
    return output.getvalue()


def source(tmp_path, payload, observed=None):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": [{"id": "merchant-fields", "base_url": "https://platform.example/",
        "allowed_paths": ["/field-standards"], "field_reconciliation": {"path": "/field-standards", "tenant_ids": ["local", "a"],
        "parameter_mapping": {"merchant_id": "merchant_id"}}}]}), encoding="utf-8")
    def handle(request):
        if observed is not None:
            observed.append(dict(request.url.params))
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"data": payload})
    return ManagedHttpSource(ConnectionRegistry(path), httpx.MockTransport(handle), resolver=lambda _host: ["93.184.216.34"])


def run(tmp_path, payload, content=None, monkeypatch=None):
    audit = AuditService(tmp_path / "runtime")
    job_id = audit.create_job()
    path = tmp_path / "supplier.xlsx"
    path.write_bytes(content or workbook())
    runtime = FieldReconciliationService(audit, source(tmp_path, payload), tmp_path / "ai")
    runtime.run(job_id, path, "merchant-fields", {"merchant_id": "m-1"}, "local")
    status = audit.status(job_id)
    assert status["status"] != "failed", status
    return audit, job_id, read_json(audit.artifact(job_id, "field_report"))


def test_fields_only_and_multiple_sheets(tmp_path):
    payload = contract()
    payload["primary_key"] = []
    audit, job_id, report = run(tmp_path, payload, workbook(sheets=2))
    assert report["comparison_mode"] == "fields"
    assert report["summary"]["passed_records"] == 2
    assert audit.status(job_id)["status"] == "completed"


def test_record_comparison_keeps_values_and_identifiers_exact(tmp_path):
    audit, job_id, report = run(tmp_path, contract(True))
    assert report["results"][0]["source"]["sku"] == "001"
    assert any(issue["field_id"] == "price" and issue["issue_type"] == "value_mismatch" for issue in report["issues"])
    snapshot = read_json(audit.artifact(job_id, "field_snapshot"))
    assert snapshot["contract"]["schema_version"] == "2026.10"
    assert len(snapshot["content_sha256"]) == 64


def test_missing_required_and_duplicate_keys_never_pass(tmp_path):
    _, _, report = run(tmp_path, contract(), workbook(headers=["SKU", "Price"], rows=[["001", "100"], ["001", "100"]]))
    assert report["summary"]["passed_records"] == 0
    assert {issue["issue_type"] for issue in report["issues"]} >= {"missing_field", "duplicate_primary_key"}


@pytest.mark.parametrize("records", [[{"sku": "", "name": "A"}], [{"sku": "001"}, {"sku": "001"}]])
def test_invalid_platform_keys_rejected(records):
    payload = contract(True); payload["records"] = records
    with pytest.raises(ValidationError):
        PlatformEnvelope.model_validate(payload)


def test_model_unknown_or_unavailable_is_review_not_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(AIClient, "settings", lambda self: {"configured": True, "model": "test", "include_samples": True})
    monkeypatch.setattr(AIClient, "reconcile_semantic_values", lambda self, data: {item["id"]: {"verdict": "unknown", "reason": "Needs context"} for item in data})
    audit, job_id, report = run(tmp_path, contract(True), workbook(rows=[["001", "Desk light", "100"]]))
    assert report["summary"]["passed_records"] == 0
    assert report["semantic_evidence"][0]["verdict"] == "unknown"
    assert audit.status(job_id)["status"] == "manual_review"


def test_model_equivalent_evidence_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(AIClient, "settings", lambda self: {"configured": True, "model": "test", "include_samples": True})
    monkeypatch.setattr(AIClient, "reconcile_semantic_values", lambda self, data: {item["id"]: {"verdict": "equivalent", "reason": "Same material"} for item in data})
    _, _, report = run(tmp_path, contract(True), workbook(rows=[["001", "Desk light", "100"]]))
    assert report["summary"]["passed_records"] == 1
    assert report["semantic_evidence"][0]["reason"] == "Same material"


def test_model_self_confidence_alone_requires_review(tmp_path, monkeypatch):
    monkeypatch.setattr(AIClient, "settings", lambda self: {"configured": True, "model": "test", "include_samples": False})
    monkeypatch.setattr(AIClient, "reconcile_field_mappings", lambda self, fields, columns: [{"column": 2, "field_id": "name", "confidence": .99, "reason": "Similar meaning"}])
    _, _, report = run(tmp_path, contract(), workbook(headers=["SKU", "Item caption", "Price"]))
    assert report["summary"]["passed_records"] == 0
    assert next(item for item in report["mappings"] if item["column"] == 2)["status"] == "manual_review"


def test_exported_formula_like_text_is_literal(tmp_path):
    _, _, report = run(tmp_path, contract(), workbook(rows=[["001", "=1+1", "100"]]))
    assert any(issue["issue_type"] == "invalid_value" for issue in report["issues"])
    output = next((tmp_path / "runtime" / "jobs").glob("*/field-reconciliation-result.xlsx"))
    book = load_workbook(output)
    assert book["标准化数据"].cell(2, 4).data_type == "s"
    assert book["供应商原始数据"].cell(2, 4).data_type == "s"
    book.close()


def test_extra_columns_survive_manual_confirmation_and_export(tmp_path):
    audit, job_id, report = run(tmp_path, contract(), workbook(headers=["SKU", "Name", "Price", "Supplier note"], rows=[["001", "Lamp", "99.50", "keep this note"]]))
    from excel_auditor.field_reconciliation import write_json

    directory = audit.job_directory(job_id)
    write_json(directory / "field-decisions.json", [{"sheet": "Supplier-0", "column": 4, "field_id": None}])
    FieldReconciliationService(audit, source(tmp_path, contract()), tmp_path / "ai").run(job_id, tmp_path / "supplier.xlsx", "merchant-fields", {"merchant_id": "m-1"}, "local")
    report = read_json(audit.artifact(job_id, "field_report"))
    assert report["results"][0]["raw_source"]["4"] == "keep this note"
    assert next(item for item in report["mappings"] if item["column"] == 4)["status"] == "ignored"
    assert report["summary"]["passed_records"] == 1
    book = load_workbook(audit.artifact(job_id, "field_excel"))
    assert book["供应商原始数据"].cell(2, 6).value == "keep this note"
    book.close()


@pytest.mark.parametrize(("field_type", "value", "normalized"), [("date", "2026/10/14", "2026-10-14"), ("datetime", "2026-10-14T09:00:00", "2026-10-14T09:00:00"), ("boolean", "true", True), ("decimal", "99.50", 99.5)])
def test_normalized_values_remain_json_serializable(tmp_path, field_type, value, normalized):
    payload = {"schema_version": "1", "fields": [{"id": "value", "title": "Value", "type": field_type}]}
    _, _, report = run(tmp_path, payload, workbook(headers=["Value"], rows=[[value]]))
    assert report["results"][0]["normalized_source"]["value"] == normalized
    assert report["summary"]["passed_records"] == 1


def test_invalid_decimal_message_is_actionable(tmp_path):
    _, _, report = run(tmp_path, contract(), workbook(rows=[["001", "Lamp", "pending"]]))
    issue = next(item for item in report["issues"] if item["field_id"] == "price")
    assert issue["message"] == "字段格式不符合要求，应为数值。"


def test_sensitive_values_never_sent_to_model(tmp_path, monkeypatch):
    payload = contract(True)
    payload["fields"][1]["sensitive"] = True
    monkeypatch.setattr(AIClient, "settings", lambda self: {"configured": True, "model": "test", "include_samples": True})
    calls = []
    monkeypatch.setattr(AIClient, "reconcile_semantic_values", lambda self, data: calls.append(data))
    _, _, report = run(tmp_path, payload, workbook(rows=[["001", "Confidential", "100"]]))
    assert not calls
    assert report["summary"]["review_records"] == 1
    assert report["issues"][0]["message"] == "敏感字段未发送给模型，请人工核实原值。"


def test_empty_platform_records_are_not_fields_only(tmp_path):
    payload = contract(True)
    payload["records"] = []
    _, _, report = run(tmp_path, payload)
    assert report["comparison_mode"] == "records"
    assert report["summary"]["passed_records"] == 0
    assert any(item["issue_type"] == "standard_record_missing" for item in report["issues"])


@pytest.fixture
def api_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path / "api-data"))
    monkeypatch.delenv("EXCEL_AUDITOR_API_TOKEN", raising=False)
    monkeypatch.delenv("EXCEL_AUDITOR_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("EXCEL_AUDITOR_API_TOKENS_JSON", json.dumps({"token-a": {"tenant_id": "a", "user_id": "operator"}, "token-b": {"tenant_id": "b", "user_id": "operator"}}))
    import excel_auditor.api as api_module
    api_module = importlib.reload(api_module)
    yield api_module
    monkeypatch.undo()
    importlib.reload(api_module)


def test_api_upload_confirm_reuses_snapshot_and_checks_revision_and_tenant(tmp_path, api_runtime):
    api_module = api_runtime
    calls = []
    api_module.managed_http = source(tmp_path, contract(), calls)
    client = TestClient(api_module.app)
    headers = {"Authorization": "Bearer token-a", "Idempotency-Key": "upload-1"}
    body = workbook(headers=["SKU", "Item caption", "Price"])
    response = client.post("/api/v1/field-reconciliations", headers=headers, data={"connection_id": "merchant-fields", "scope_json": '{"merchant_id":"m-1"}'}, files={"excel_file": ("supplier.xlsx", body)})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    status = client.get(f"/api/v1/field-reconciliations/{job_id}", headers=headers).json()
    assert status["status"] == "manual_review", status
    snapshot_before = api_module.service.artifact(job_id, "field_snapshot").read_bytes()
    decisions = {"base_revision": 0, "decisions": [{"sheet": "Supplier-0", "column": 2, "field_id": "name"}]}
    confirmed = client.post(f"/api/v1/field-reconciliations/{job_id}/mappings/confirm", headers=headers, json=decisions)
    assert confirmed.status_code == 202, confirmed.text
    assert client.get(f"/api/v1/field-reconciliations/{job_id}", headers=headers).json()["status"] == "completed"
    assert len(calls) == 1
    assert api_module.service.artifact(job_id, "field_snapshot").read_bytes() == snapshot_before
    assert client.post(f"/api/v1/field-reconciliations/{job_id}/mappings/confirm", headers=headers, json=decisions).status_code == 409
    assert client.get(f"/api/v1/field-reconciliations/{job_id}", headers={"Authorization": "Bearer token-b"}).status_code == 404
    assert client.get("/api/v1/field-reconciliations/connections", headers={"Authorization": "Bearer token-b"}).json()["items"] == []
    assert client.get(f"/api/v1/field-reconciliations/{job_id}/files/excel", headers=headers).status_code == 200
    assert client.get("/api/v1/field-reconciliations", headers=headers).json()["total"] == 1
    replay = client.post("/api/v1/field-reconciliations", headers=headers, data={"connection_id": "merchant-fields", "scope_json": '{"merchant_id":"m-1"}'}, files={"excel_file": ("supplier.xlsx", body)})
    assert replay.status_code == 202
    assert replay.json()["job_id"] == job_id
    assert len(calls) == 1


def test_ai_duplicate_or_truncated_verdict_rejected(tmp_path):
    def handle(_request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"results":[]}'}}]})
    ai = AIClient(tmp_path, "local", httpx.MockTransport(handle))
    ai.save(AISettings(base_url="https://models.example/v1", model="test", api_key="test-key", include_samples=True))
    with pytest.raises(Exception, match="语义比对"):
        ai.reconcile_semantic_values([{"id": "1", "supplier_value": "A", "platform_value": "B"}])

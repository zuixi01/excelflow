"""Real model + synthetic Excel acceptance, isolated from business tasks and field profiles.

Run with PYTHONPATH=src and EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP=1 for the local HTTP model.
The saved model credentials are used internally by AIClient, never copied into the report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.excel import field_matches, read_maintenance
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, LocalCategory, ProfileEdit
from excel_auditor.workflow.mapping_runtime import mapping_runtime
from excel_auditor.workflow.models import EditRequest, MappingRequest
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.tasks import TaskService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings-root", type=Path, default=Path("var/workflow"))
    parser.add_argument("--output", type=Path, default=Path(".codex-artifacts") / ("retained-columns-live-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    settings_root = args.settings_root.resolve()
    calls = []
    report = {"status": "running", "business_tasks_modified": False, "row_values_sent_to_model": False, "calls": calls}
    store = WorkflowStore(out / "data" / "workflow")

    class ObservedAIClient(AIClient):
        def __init__(self, root, tenant):
            super().__init__(settings_root, tenant)

        def request(self, route, payload=None):
            assert self.transport is None, "Live test must use real HTTP"
            assert payload["model"] == "gpt-6.1-sol"
            prompt = json.loads(payload["messages"][1]["content"])
            assert set(prompt) == {"fields", "columns"}
            assert all(set(column) == {"column", "header"} for column in prompt["columns"])
            entry = {"route": route, "requested_model": payload["model"], "real_http": True}
            calls.append(entry)
            start = time.monotonic()
            response = super().request(route, payload)
            entry.update(http_status=200, response_model=response.get("model"), response_id=response.get("id"),
                         usage=response.get("usage"), seconds=round(time.monotonic() - start, 3))
            return response

    ai = ObservedAIClient(settings_root, "local")
    try:
        settings = ai.settings()
        assert settings["configured"] and settings["model"] == "gpt-6.1-sol", "Configure the real gpt-6.1-sol model first"
        settings_hash = hashlib.sha256(ai.path.read_bytes()).hexdigest()
        profiles = FieldProfileService(store)
        definition = FieldProfile(name="真实模型与原表保留列验收", fixed_fields=[
            ConfiguredField(id="test_code", title="商品编码", description="唯一货品编号，保留前导零"),
            ConfiguredField(id="test_name", title="商品名称", description="向顾客展示的商品品名"),
            ConfiguredField(id="test_price", title="销售单价", type="decimal", description="向顾客收取的每件人民币售价"),
            ConfiguredField(id="test_weight", title="商品净重千克", description="商品本体的重量，来源没有时不要匹配"),
        ], local_categories=[LocalCategory(id="test", title="示例平台", attributes=[
            ConfiguredField(id="material", title="材质", description="制造商品使用的主要材料"),
        ], specifications=[ConfiguredField(id="color", title="颜色", description="商品外观的颜色")])])
        draft = profiles.get("local")
        draft = profiles.save("local", ProfileEdit(base_revision=draft["revision"], profile=definition))
        profiles.publish("local", draft["revision"])
        profile_before = profiles.get("local")
        headers = ["货品识别号", "售卖品名", "向顾客收取的单件金额（元）", "制造主体用料", "外观呈现色调",
                   "供应商交付补记", "供应商交付补记", "箱内计数", "原样字符（测试）/附注"]
        values = [
            ["000007", "示例台灯甲", "129.50", "铝合金", "蓝色", "  首行\n第二行  ", "000001", "12", "ＮＡ / Ａ"],
            ["000008", "示例台灯乙", "88.00", "铜", "白色", "", "000002", "0", "末列内容"],
            ["000009", "示例台灯丙", "99.90", "木材", "黑色", "仅供验收", "000003", "24", ""]]
        book = Workbook()
        sheet = book.active
        sheet.title = "示例供应商"
        sheet.append(headers)
        for row in values:
            sheet.append(row)
        source = out / "示例原表.xlsx"
        book.save(source)
        book.close()
        config, _ = profiles.snapshot("local")
        assert all(not field_matches(header, config.fields) for header in headers), "This fixture must require semantic matching"
        tasks = TaskService(store)
        task, _ = tasks.create_standardization("local", "local", "真实模型保留列验收", source.name, source.read_bytes())
        print("Calling the real gpt-6.1-sol through the production import pipeline...", flush=True)
        with patch("excel_auditor.workflow.ai.AIClient", ObservedAIClient):
            tasks.parse(task["id"], "local")
        task = tasks.get(task["id"], "local")
        assert task["status"] == "mapping", task["payload"].get("error")
        source_sheet = task["payload"]["sheets"][0]
        expected = {"test_code": [1], "test_name": [2], "test_price": [3], "local.test.attribute.material": [4],
                    "local.test.specification.color": [5]}
        bindings = source_sheet["suggested_bindings"]
        assert {b["field"]: b["columns"] for b in bindings} == expected, bindings
        assert all(b["method"] == "semantic_model" for b in bindings)
        assert calls and all(c["response_model"] == "gpt-6.1-sol" for c in calls)
        report["mappings"] = bindings
        request = MappingRequest(base_revision=task["revision"], sheets=[dict(sheet=source_sheet["sheet"],
            header_row=source_sheet["header_row"], header_depth=source_sheet["header_depth"], bindings=bindings)])
        task = tasks.confirm_mapping(task["id"], "local", "local", request)
        fields = task["payload"]["config"]["fields"]
        retained = [field for field in fields if field.get("source_column")]
        assert len(fields) == 10 and len(retained) == 4 and fields[-4:] == retained
        assert [field["source_column"]["header"] for field in retained] == headers[5:]
        rows = tasks.rows(task["id"], "local", 0, 20)["items"]
        ids = [f["rule"]["name"] for f in retained]
        for row, original in zip(rows, values, strict=True):
            assert [row["values_json"][field_id] for field_id in ids] == original[5:]
            assert row["values_json"]["test_code"] == original[0]
            assert row["values_json"]["test_weight"] == ""
            assert row["values_json"]["local.test.attribute.material"] == original[3]
            assert row["values_json"]["local.test.specification.color"] == original[4]
        task = tasks.edit(task["id"], "local", "local", EditRequest(base_revision=task["revision"],
            changes={rows[0]["id"]: {ids[0]: "在线维护修改\n保留第二行"}}))
        maintenance = tasks.maintenance_file(task["id"], "local")
        shutil.copyfile(maintenance, out / "示例维护表.xlsx")
        transported = read_maintenance(maintenance, task)
        assert transported[0]["values"][ids[0]] == "在线维护修改\n保留第二行"
        book = load_workbook(maintenance)
        meta = dict(book["_workflow_meta"].values)
        sheet = book["维护表"]
        sheet.cell(meta["header_depth"] + 1, [f["rule"]["name"] for f in fields].index(ids[1]) + 2, "00000999")
        sheet.cell(meta["header_depth"] + 2, [f["rule"]["name"] for f in fields].index("local.test.specification.color") + 2, "米白色")
        returned = out / "示例回传表.xlsx"
        book.save(returned)
        book.close()
        preview = tasks.preview_return(task["id"], "local", returned.read_bytes())
        assert preview["payload"]["counts"] == {"changed": 2, "added": 0, "missing": 0}
        task = tasks.apply_return(task["id"], "local", "local", preview["id"], False)
        rows = tasks.rows(task["id"], "local", 0, 20)["items"]
        assert rows[0]["values_json"][ids[1]] == "00000999"
        assert rows[1]["values_json"]["local.test.specification.color"] == "米白色"
        run, _ = tasks.create_export(task["id"], "local", task["revision"])
        tasks.generate(run["id"], "local")
        export = tasks.export_file(task["id"], run["id"], "local", "标准商品表.xlsx")
        shutil.copyfile(export, out / "示例最终表.xlsx")
        book = load_workbook(export)
        sheet = book.active
        first_row = task["payload"]["config"]["outputs"][0]["sheets"][0]["data_start_row"]
        assert sheet.max_column == 10 and sheet.max_row == first_row + 2
        for index, row in enumerate(rows, start=first_row):
            for column, field in enumerate(fields, start=1):
                actual = sheet.cell(index, column).value
                expected_value = row["values_json"][field["rule"]["name"]]
                if field["rule"]["type"] == "decimal":
                    assert actual == float(expected_value)
                else:
                    assert (actual if actual is not None else "") == expected_value
        assert sheet.cell(2, 10).value == headers[-1]
        book.close()
        assert profiles.get("local") == profile_before
        assert hashlib.sha256(ai.path.read_bytes()).hexdigest() == settings_hash
        report.update(status="passed", task_id=task["id"], rows=3, matched_fields=5, missing_target_fields=1,
            fixed_fields=4, dynamic_fields=2, retained_columns=4, final_columns=10,
            checks={"semantic_match": True, "all_source_values_retained": True, "online_edit": True,
                    "maintenance_download": True, "excel_return": True, "final_export_every_cell": True,
                    "original_headers_preserved": True, "existing_settings_unchanged": True},
            retained=[field["source_column"] for field in retained])
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        mapping_runtime().shutdown()
        store.engine.dispose()
        (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
        print(str(out), flush=True)


if __name__ == "__main__":
    main()

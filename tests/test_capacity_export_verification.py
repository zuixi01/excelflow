"""The capacity gate must understand headers and detect corrupt exported cells."""
from io import BytesIO
from pathlib import Path
import runpy

import pytest
from openpyxl import Workbook


benchmark = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "benchmark_workflow_capacity.py"))
verify_large_export = benchmark["verify_large_export"]


def exported(*, corrupt=False, truncate=False):
    book = Workbook()
    sheet = book.active
    sheet.append(["固定字段", None])
    sheet.append(["Field 0", "Field 1"])
    sheet.append([None, None])
    sheet.append(["000000_0", "bad-value" if corrupt else "000000_1"])
    if not truncate:
        sheet.append(["000001_0", "000001_1"])
    buffer = BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


def test_capacity_export_uses_configured_data_start_after_hierarchical_headers():
    assert verify_large_export(exported(), 2, 2, 4) == 2


@pytest.mark.parametrize("problem", ["corrupt", "truncate"])
def test_capacity_export_rejects_corrupt_or_missing_data(problem):
    with pytest.raises(AssertionError):
        verify_large_export(exported(**{problem: True}), 2, 2, 4)


def test_large_fixture_can_reduce_active_fields_without_deleting_published_ids(tmp_path):
    from excel_auditor.workflow.field_profiles import FieldProfileService
    from excel_auditor.workflow.repository import WorkflowStore

    store = WorkflowStore(tmp_path)
    try:
        profiles = FieldProfileService(store)
        for count in (8, 100, 50):
            benchmark["set_benchmark_fields"](profiles, count)
            config, _ = profiles.snapshot("local")
            assert len(config.outputs[0].sheets[0].columns) == count
        published = profiles.get("local")["payload"]["published"]["fixed_fields"]
        assert len(published) == 100
        assert sum(field["enabled"] for field in published) == 50
    finally:
        store.engine.dispose()

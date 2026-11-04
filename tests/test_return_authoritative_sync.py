import pytest
from openpyxl import load_workbook

from excel_auditor.workflow.models import WorkflowError
from test_flexible_maintenance_return import runtime, inspect, preview, choices


def test_deleted_row_and_column_stay_deleted_through_maintenance_and_final_export(runtime):
    store, service, task = runtime
    def edit(book):
        sheet = book['维护表']
        sheet.delete_rows(5)  # Remove row_1 / SKU 0002.
        # Excel updates merged headers and named ranges on column deletion;
        # openpyxl does not, so emulate those native bookkeeping updates.
        for region in list(sheet.merged_cells.ranges):
            if region.max_col == 8:
                sheet.unmerge_cells(str(region))
                if region.min_col < 8 and (region.min_col < 7 or region.min_row < region.max_row):
                    sheet.merge_cells(start_row=region.min_row, end_row=region.max_row,
                                      start_column=region.min_col, end_column=7)
        for name in book.defined_names.values():
            if "!$H$" in name.attr_text:
                name.attr_text = '#REF!'
        sheet.delete_cols(8)  # Last dynamic column: 功率. Existing anchors do not shift.
        sheet['C4'] = '24'
        for column, cell in enumerate(list(sheet[4]), 1):
            sheet.cell(6, column).value = cell.value
        sheet['A6'] = sheet['B6'] = None
        sheet['F6'] = '新增商品'
    result = preview(runtime, inspect(runtime, edit))
    assert result['payload']['sync_mode'] == 'replace'
    assert result['payload']['counts'] == {'changed': 1, 'added': 1, 'missing': 1}
    assert any(c['kind'] == '删除列' and c['field'] == '功率' for c in result['payload']['field_changes'])
    # A stale client false flag cannot restore a row absent from this reviewed file.
    updated = service.apply_return(task['id'], 'local', 'operator', result['id'], False, confirm_structure=True)
    rows = service.rows(task['id'], 'local', 0, 30)['items']
    assert updated['revision'] == 2 and len(rows) == 3
    assert [r['id'] for r in rows[:2]] == ['row_0', 'row_2']
    assert all(r['id'] != 'row_1' and r['values_json']['sku'] != '0002' for r in rows)
    assert all('local.lighting.specification.power' not in r['values_json'] for r in rows)
    assert rows[0]['values_json']['price'] == '24'
    assert rows[2]['values_json']['local.lighting.attribute.name'] == '新增商品'
    assert not any(f['rule']['name'].endswith('.power') for f in updated['payload']['config']['fields'])
    maintenance = load_workbook(service.maintenance_file(task['id'], 'local'))
    sheet = maintenance['维护表']
    assert sheet.max_column == 7 and sheet.max_row == 6
    assert sheet['A4'].value == 'row_0' and sheet['A5'].value == 'row_2'
    assert sheet['A6'].value == rows[2]['id']
    assert sheet['D3'].value == '品牌' and sheet['F3'].value == '名称'
    maintenance.close()
    exported, _ = service.create_export(task['id'], 'local', updated['revision'])
    service.generate(exported['id'], 'local')
    final = load_workbook(service.export_file(task['id'], exported['id'], 'local', '标准商品表.xlsx'))
    sheet = final.active
    start = exported['payload']['config']['outputs'][0]['sheets'][0]['data_start_row']
    values = list(sheet.iter_rows(min_row=start, values_only=True))
    assert len(values) == 3 and [r[0] for r in values[:2]] == ['0001','0003']
    assert '名称：新增商品' in str(values[-1])
    assert '功率：' not in str(values) and '0002' not in str(values)
    assert '品牌：三雄极光' in str(values[0])
    final.close()
    # Re-downloading and returning without edits must not resurrect anything.
    again = preview(runtime, inspect(runtime))
    assert again['payload']['counts'] == {'changed': 0, 'added': 0, 'missing': 0}


def test_old_preserve_preview_must_be_reviewed_again_before_save(runtime):
    store, service, task = runtime
    result = preview(runtime, inspect(runtime))
    with store.engine.begin() as con:
        old = store.get(con, result['id'], 'local', 'return')
        old['payload'].pop('sync_mode')
        store.save(con, old)
    with pytest.raises(WorkflowError, match='旧的保留规则'):
        service.apply_return(task['id'], 'local', 'operator', result['id'], True, confirm_structure=True)
    assert service.get(task['id'], 'local')['revision'] == 1


def test_explicit_column_preserve_is_rejected_instead_of_silent_restoration(runtime):
    inspection = inspect(runtime)
    cols = choices(inspection)
    series = next(c for c in cols if c['field_id'].endswith('.series'))
    series['action'] = 'ignore'
    with pytest.raises(WorkflowError, match='以文件内容为准'):
        preview(runtime, inspection, cols, missing_fields={series['field_id']: 'preserve'})

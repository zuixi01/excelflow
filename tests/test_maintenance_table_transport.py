from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from openpyxl import Workbook, load_workbook

from excel_auditor.workflow.excel import read_maintenance
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.models import MappingRequest, WorkflowError
from excel_auditor.workflow.routes import build_router
from test_sop_workflow import ready_task, runtime


def saved(book):
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def table_file(service, task):
    return service.maintenance_file(task['id'], 'local', table_mode=True)


def test_grouped_maintenance_preserves_fixed_dynamic_and_source_header_levels(tmp_path):
    from excel_auditor.models import ColumnRule
    from excel_auditor.workflow.excel import write_maintenance
    from excel_auditor.workflow.field_profiles import LocalCategory, compile_profile
    from excel_auditor.workflow.models import MaintenanceField, SourceColumn, content_hash
    profile = FieldProfile(fixed_fields=[
        ConfiguredField(id='product_name', title='商品名称'),
        ConfiguredField(id='quantity', title='数值', group='包装量'),
        ConfiguredField(id='unit', title='单位', group='包装量'),
    ], local_categories=[LocalCategory(id='lighting', title='灯具', attributes=[
        ConfiguredField(id='brand', title='品牌'), ConfiguredField(id='series', title='系列'),
    ], specifications=[ConfiguredField(id='power', title='功率')])])
    config = compile_profile(profile)
    config.source_header_layout = 'grouped'
    config.fields.append(MaintenanceField(rule=ColumnRule(name='extra', title='原表保留列/封窗 · A/铝材壁厚', type='string'),
                                          source_column=SourceColumn(sheet='封窗', column=1, header='铝材壁厚')))
    task = {'id': 'task_grouped', 'revision': 1, 'payload': {'config': config.model_dump(mode='json'),
                                                         'template_hash': content_hash(config.model_dump(mode='json'))}}
    row = {'id': 'row_grouped', 'values_json': {field.rule.name: '' for field in config.fields}, 'issues': []}
    row['values_json']['product_name'] = '示例商品'
    path = tmp_path / 'grouped.xlsx'
    write_maintenance(path, task, [row])
    book = load_workbook(path)
    sheet = book['维护表']
    assert [sheet.cell(1, column).value for column in (2, 5, 8)] == ['固定字段', '动态字段', '封窗']
    assert sheet['C2'].value == '包装量' and sheet['E2'].value == '商品属性' and sheet['G2'].value == '商品规格'
    assert sheet['C3'].value == '数值' and sheet['E3'].value == '品牌' and sheet['G3'].value == '功率'
    assert {'B1:D1', 'E1:G1', 'C2:D2', 'E2:F2'} <= {str(region) for region in sheet.merged_cells.ranges}
    assert not sheet.tables and sheet.column_dimensions['A'].hidden
    assert dict(book['_workflow_meta'].values)['header_depth'] == 3
    book.close()
    assert read_maintenance(path, task) == [{'id': row['id'], 'values': row['values_json']}]


def test_web_download_preserves_hierarchical_headers_and_table_files_remain_supported(runtime, tmp_path):
    _, service, _ = runtime
    task = ready_task(runtime)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = request.state.user_id = 'local'
        return await call_next(request)
    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    with TestClient(app) as client:
        response = client.get(f"/api/v1/workflow-tasks/{task['id']}/maintenance/download")
        assert response.status_code == 200
    book = load_workbook(BytesIO(response.content))
    sheet = book['维护表']
    meta = dict(book['_workflow_meta'].values)
    assert meta['header_depth'] > 1
    assert not sheet.tables
    assert sheet.column_dimensions['A'].hidden
    assert sheet['A1'].value == '_workflow_record_id'
    assert sheet.cell(meta['header_depth'] + 1, 1).value.startswith('row_')
    assert any(region.min_row == 1 and region.max_row <= meta['header_depth'] for region in sheet.merged_cells.ranges)
    book.close()
    # Previously downloaded single-header Table files must still round-trip.
    explicit_table_path = table_file(service, task)
    book = load_workbook(explicit_table_path)
    sheet = book['维护表']
    table = sheet.tables['MaintenanceRecords']
    assert table.ref == f'A1:{sheet.cell(1,sheet.max_column).column_letter}{sheet.max_row}'
    assert table.autoFilter.ref == table.ref
    assert table.tableColumns[0].name == '_workflow_record_id'
    assert sheet.column_dimensions['A'].hidden and not sheet.protection.sheet
    assert not sheet.merged_cells.ranges and not book.security.lockStructure
    assert dict(book['_workflow_meta'].values)['transport_format'] == 'table_v1'
    assert len({column.name.casefold() for column in table.tableColumns}) == len(table.tableColumns)
    book.close()
    with ZipFile(explicit_table_path) as package:
        assert 'xl/tables/table1.xml' in package.namelist()


def test_complete_row_sort_has_no_changes_or_identity_warnings(runtime):
    _, service, _ = runtime
    task = ready_task(runtime)
    before = service.rows(task['id'], 'local', 0, 30)['items']
    book = load_workbook(table_file(service, task))
    sheet = book['维护表']
    values = list(sheet.iter_rows(min_row=2, values_only=True))
    for index, values in enumerate(reversed(values), start=2):
        for column, value in enumerate(values, start=1):
            sheet.cell(index, column).value = value
    preview = service.preview_return(task['id'], 'local', saved(book))
    assert preview['payload']['counts'] == {'changed': 0, 'added': 0, 'missing': 0}
    assert preview['payload']['identity_warnings'] == []
    service.apply_return(task['id'], 'local', 'local', preview['id'], False)
    assert service.rows(task['id'], 'local', 0, 30)['items'] == before


@pytest.mark.parametrize('delete_missing,expected_count', [(False, 3), (True, 2)])
def test_add_edit_delete_preview_and_apply_are_explicit(runtime, delete_missing, expected_count):
    _, service, _ = runtime
    task = ready_task(runtime)
    before = service.rows(task['id'], 'local', 0, 30)['items']
    book = load_workbook(table_file(service, task))
    sheet = book['维护表']
    names = {field['rule']['name']: i for i, field in enumerate(task['payload']['config']['fields'], start=2)}
    sheet.delete_rows(2)
    sheet.cell(2, names['sale_price'], '219.00')
    new = [None] * sheet.max_column
    new[names['product_name'] - 1] = '新增灯具'
    new[names['sku'] - 1] = '0003'
    sheet.append(new)
    preview = service.preview_return(task['id'], 'local', saved(book))
    assert preview['payload']['counts'] == {'changed': 1, 'added': 1, 'missing': 1}
    assert preview['payload']['added_samples'][0]['values']['sku'] == '0003'
    assert preview['payload']['missing_samples'][0]['id'] == before[0]['id']
    assert preview['payload']['identity_warnings'] == []
    # Preview alone never changes business data.
    assert service.rows(task['id'], 'local', 0, 30)['items'] == before
    result = service.apply_return(task['id'], 'local', 'local', preview['id'], delete_missing)
    assert result['payload']['record_count'] == expected_count
    after = service.rows(task['id'], 'local', 0, 30)['items']
    assert any(row['id'] == before[1]['id'] and row['values_json']['sale_price'] == '219.00' for row in after)
    assert (before[0]['id'] in {row['id'] for row in after}) is not delete_missing
    assert after[-1]['values_json']['sku'] == '0003' and after[-1]['id'] not in {row['id'] for row in before}


@pytest.mark.parametrize('damage,message', [('duplicate', '重复的记录 ID'), ('unknown', '未知记录 ID')])
def test_duplicate_or_unknown_ids_are_rejected(runtime, damage, message):
    _, service, _ = runtime
    task = ready_task(runtime)
    book = load_workbook(table_file(service, task))
    sheet = book['维护表']
    sheet.cell(3, 1, sheet.cell(2, 1).value if damage == 'duplicate' else 'row_not_in_this_task')
    with pytest.raises(WorkflowError, match=message):
        service.preview_return(task['id'], 'local', saved(book))


@pytest.mark.parametrize('damage,code', [('clear_id', 'POSSIBLE_ID_LOSS'), ('business_only_sort', 'POSSIBLE_PARTIAL_SORT')])
def test_suspected_id_loss_and_partial_sort_need_server_confirmation(runtime, damage, code):
    _, service, _ = runtime
    task = ready_task(runtime)
    before = service.rows(task['id'], 'local', 0, 30)['items']
    book = load_workbook(table_file(service, task))
    sheet = book['维护表']
    if damage == 'clear_id':
        sheet.cell(2, 1).value = None
    else:
        values = list(sheet.iter_rows(min_row=2, min_col=2, values_only=True))
        for index, row in enumerate(reversed(values), start=2):
            for column, value in enumerate(row, start=2):
                sheet.cell(index, column).value = value
    preview = service.preview_return(task['id'], 'local', saved(book))
    assert code in {warning['code'] for warning in preview['payload']['identity_warnings']}
    with pytest.raises(WorkflowError) as error:
        service.apply_return(task['id'], 'local', 'local', preview['id'], False)
    assert error.value.code == 'RETURN_IDENTITY_CONFIRMATION_REQUIRED'
    assert service.rows(task['id'], 'local', 0, 30)['items'] == before
    # Explicit acknowledgement is available for intentional operations, never inferred.
    service.apply_return(task['id'], 'local', 'local', preview['id'], False, True)


def test_merged_source_is_flat_in_table_and_shared_edits_still_export_correctly(runtime):
    store, service, _ = runtime
    profiles = FieldProfileService(store)
    profile = FieldProfile(fixed_fields=[ConfiguredField(id='product_name', title='商品名称'), ConfiguredField(id='sku', title='货号')])
    draft = profiles.get('local')
    saved_profile = profiles.save('local', ProfileEdit(base_revision=draft['revision'], profile=profile))
    profiles.publish('local', saved_profile['revision'])
    book = Workbook()
    sheet = book.active
    sheet.append(['商品名称', '货号'])
    sheet.append(['共享商品名', '0001'])
    sheet.append([None, '0002'])
    sheet.merge_cells('A2:A3')
    task, _ = service.create_standardization('local', 'local', '', 'source.xlsx', saved(book))
    service.parse(task['id'], 'local')
    task = service.get(task['id'], 'local')
    source = task['payload']['sheets'][0]
    task = service.confirm_mapping(task['id'], 'local', 'local', MappingRequest(base_revision=task['revision'], sheets=[{
        'sheet': source['sheet'], 'header_row': source['header_row'], 'header_depth': source['header_depth'],
        'bindings': source['suggested_bindings'],
    }]))
    path = table_file(service, task)
    book = load_workbook(path)
    sheet = book['维护表']
    assert not sheet.merged_cells.ranges
    assert sheet['B2'].value == sheet['B3'].value == '共享商品名'
    sheet['B2'] = '维护后的共享名称'
    preview = service.preview_return(task['id'], 'local', saved(book))
    task = service.apply_return(task['id'], 'local', 'local', preview['id'], False)
    assert [row['values_json']['product_name'] for row in service.rows(task['id'], 'local', 0, 30)['items']] == ['维护后的共享名称'] * 2
    run, _ = service.create_export(task['id'], 'local', task['revision'])
    service.generate(run['id'], 'local')
    book = load_workbook(service.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx'))
    assert 'A4:A5' in {str(region) for region in book.active.merged_cells.ranges}
    book.close()


def test_identity_acknowledgement_is_enforced_by_api(runtime, tmp_path):
    _, service, _ = runtime
    task = ready_task(runtime)
    book = load_workbook(table_file(service, task))
    book['维护表']['A2'] = None
    content = saved(book)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = request.state.user_id = 'local'
        return await call_next(request)
    @app.exception_handler(WorkflowError)
    async def errors(request, exc):
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=exc.status)
    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    root = f"/api/v1/workflow-tasks/{task['id']}/maintenance/returns"
    with TestClient(app) as client:
        response = client.post(root, files={'excel_file': ('edited.xlsx', content)})
        assert response.status_code == 200, response.text
        preview = response.json()
        rejected = client.post(root + '/apply', json={'preview_id': preview['id'], 'delete_missing': True})
        assert rejected.status_code == 422
        assert rejected.json()['code'] == 'RETURN_IDENTITY_CONFIRMATION_REQUIRED'
        accepted = client.post(root + '/apply', json={'preview_id': preview['id'], 'delete_missing': True,
                                                    'acknowledge_identity_warnings': True})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['revision'] == task['revision'] + 1

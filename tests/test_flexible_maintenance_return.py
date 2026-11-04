from copy import deepcopy
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import insert, select, update

from excel_auditor.workflow.excel import validate_records
from excel_auditor.workflow.field_profiles import compile_profile
from excel_auditor.workflow.models import FlexibleReturnRequest, WorkflowError, content_hash
from excel_auditor.workflow.repository import WorkflowStore, objects, records
from excel_auditor.workflow.tasks import TaskService
from test_dynamic_final_output import definition


@pytest.fixture
def runtime(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow')
    service = TaskService(store)
    profile = definition()
    config = compile_profile(profile)
    config.preserve_source_merges = True
    with store.engine.begin() as con:
        task = store.create(con, 'local', 'task', {'name': '自由回传测试', 'workflow': 'standardization',
            'config': config.model_dump(mode='json'), 'template_hash': content_hash(config.model_dump(mode='json')),
            'template_id': None, 'field_snapshot': {'version': 1, 'profile': profile.model_dump(mode='json')}, 'record_count': 3, 'error_count': 0}, 'ready')
        con.execute(update(objects).where(objects.c.id == task['id']).values(revision=1))
        rows = []
        for i in range(3):
            values = {f.rule.name: '' for f in config.fields}
            values.update(sku=f'000{i+1}', price=str(10+i))
            values['local.lighting.attribute.brand'] = '三雄极光'
            values['local.lighting.attribute.name'] = f'商品{i+1}'
            rows.append({'task_id': task['id'], 'id': f'row_{i}', 'ordinal': i, 'values_json': values,
                         'original': deepcopy(values), 'source': {'sheet': '来源', 'row': i+2}, 'issues': [], 'issue_count': 0, 'search_text': ''})
        validate_records(rows, config)
        con.execute(insert(records), rows)
    yield store, service, service.get(task['id'], 'local')
    store.engine.dispose()


def supplied(book):
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def choices(inspection):
    return [{k: col[k] for k in ('key', 'action', 'field_id', 'title', 'kind', 'type')} for col in inspection['payload']['columns']]


def inspect(runtime, edit=lambda book: None):
    _, service, task = runtime
    book = load_workbook(service.maintenance_file(task['id'], 'local'))
    edit(book)
    result = service.inspect_return(task['id'], 'local', supplied(book))
    book.close()
    return result


def preview(runtime, inspection, columns=None, **kwargs):
    _, service, task = runtime
    return service.preview_flexible_return(task['id'], 'local', inspection['id'], FlexibleReturnRequest(columns=columns or choices(inspection), **kwargs))


def test_new_renamed_reordered_deleted_columns_and_rows_roundtrip(runtime):
    store, service, task = runtime
    def edit(book):
        sheet = book['维护表']
        # A renamed existing field is identified by its invisible name.
        sheet['D3'] = '厂牌'
        # Add dynamic and ordinary columns in the data area, preserving 3 header rows.
        sheet['J1'], sheet['J2'], sheet['J3'] = '动态字段', '商品属性', '防护等级'
        sheet['K1'], sheet['K3'] = '新增字段', '运营备注'
        sheet['J4'], sheet['K4'] = 'IP65', '独立列原值'
        # Remove a record, add a blank-ID record and reorder complete rows.
        row0 = [c.value for c in sheet[4]]
        row2 = [c.value for c in sheet[6]]
        new = list(row0)
        new[0], new[1], new[2] = None, '0004', '99.5'
        for offset, values in enumerate([row2, new, row0], start=4):
            for col, value in enumerate(values, start=1):
                sheet.cell(offset, col).value = value
    inspection = inspect(runtime, edit)
    cols = choices(inspection)
    brand = next(c for c in cols if c['field_id'].endswith('.brand'))
    assert brand['title'] == '厂牌'
    # Treat missing source series column as removed rather than cleared.
    series = next(c for c in cols if c['field_id'].endswith('.series'))
    series['action'] = 'ignore'
    result = preview(runtime, inspection, cols, missing_fields={series['field_id']: 'delete'})
    assert result['status'] == 'ready'
    assert result['payload']['counts'] == {'changed': 1, 'added': 1, 'missing': 1}
    assert any('改名：品牌 → 厂牌' in s for s in result['payload']['structure_summary'])
    with pytest.raises(WorkflowError, match='确认字段对应'):
        service.apply_return(task['id'], 'local', 'operator', result['id'], True)
    assert service.get(task['id'], 'local')['revision'] == 1
    updated = service.apply_return(task['id'], 'local', 'operator', result['id'], True, confirm_structure=True)
    assert updated['revision'] == 2 and updated['payload']['record_count'] == 3
    rows = service.rows(task['id'], 'local', 0, 30)['items']
    assert [r['values_json']['sku'] for r in rows] == ['0003', '0004', '0001']
    assert rows[0]['id'] == 'row_2' and rows[2]['id'] == 'row_0'
    assert all(series['field_id'] not in r['values_json'] for r in rows)
    assert rows[2]['original']['sku'] == '0001'
    run, _ = service.create_export(task['id'], 'local', 2)
    service.generate(run['id'], 'local')
    final = load_workbook(service.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx'))
    sheet = final.active
    assert sheet.max_column == 5  # sku, price, attributes, specifications, ordinary
    start = run['payload']['config']['outputs'][0]['sheets'][0]['data_start_row']
    assert '厂牌：三雄极光' in sheet.cell(start+2, 3).value
    assert '防护等级：IP65' in sheet.cell(start+2, 3).value
    assert '系列：' not in sheet.cell(start+2, 3).value
    assert sheet.cell(start+2, 5).value == '独立列原值'
    assert sheet.cell(start+2, 1).value == '0001'
    final.close()
    # Applied preview cannot replay.
    with pytest.raises(WorkflowError):
        service.apply_return(task['id'], 'local', 'operator', result['id'], True, confirm_structure=True)


@pytest.mark.parametrize('damage', ['missing_id', 'duplicate_id', 'unknown_id', 'sku_mismatch'])
def test_uncertain_identity_requires_row_resolution(runtime, damage):
    _, service, task = runtime
    def edit(book):
        sheet = book['维护表']
        if damage == 'missing_id':
            sheet['A4'] = None
        elif damage == 'duplicate_id':
            sheet['A4'] = 'row_1'
        elif damage == 'unknown_id':
            sheet['A4'] = 'row_foreign'
        else:
            sheet['B4'], sheet['B5'] = '0002', '0001'
    inspection = inspect(runtime, edit)
    review = preview(runtime, inspection)
    assert review['status'] == 'row_review'
    assert service.get(task['id'], 'local')['revision'] == 1
    decisions = [{'key': r['key'], 'action': 'existing', 'record_id': 'row_' + str(r['row']-4)} for r in review['payload']['row_review']]
    ready = preview(runtime, inspection, rows=decisions)
    assert ready['status'] == 'ready'
    service.apply_return(task['id'], 'local', 'operator', ready['id'], False, confirm_structure=True)
    assert service.get(task['id'], 'local')['payload']['record_count'] == 3


def test_missing_required_column_is_not_silently_preserved(runtime):
    _, service, task = runtime
    inspection = inspect(runtime, lambda book: setattr(book['维护表']['D4'], 'value', None))
    cols = choices(inspection)
    brand = next(c for c in cols if c['field_id'].endswith('.brand'))
    brand['action'] = 'ignore'
    with pytest.raises(WorkflowError, match='不能直接删除'):
        preview(runtime, inspection, cols)
    assert service.get(task['id'], 'local')['revision'] == 1


def test_required_delete_duplicate_mapping_and_stale_file_rejected(runtime):
    _, service, task = runtime
    inspection = inspect(runtime)
    cols = choices(inspection)
    brand = next(c for c in cols if c['field_id'].endswith('.brand'))
    brand['action'] = 'ignore'
    with pytest.raises(WorkflowError, match='必填'):
        preview(runtime, inspection, cols, missing_fields={brand['field_id']: 'delete'})
    cols = choices(inspection)
    cols[1]['field_id'] = cols[0]['field_id']
    with pytest.raises(WorkflowError, match='不能重复关联'):
        preview(runtime, inspection, cols)
    valid = preview(runtime, inspection)
    # Concurrent revision must atomically prevent both schema and row replacement.
    from excel_auditor.workflow.models import EditRequest
    service.edit(task['id'], 'local', 'operator', EditRequest(base_revision=1, changes={'row_0': {'price': '88'}}))
    with pytest.raises(WorkflowError, match='新版本'):
        service.apply_return(task['id'], 'local', 'operator', valid['id'], False, confirm_structure=True)
    assert service.rows(task['id'], 'local', 0, 30)['items'][0]['values_json']['price'] == '88'


def test_merge_and_unmerge_are_reviewed_not_structural_rejection(runtime):
    _, service, task = runtime
    def edit(book):
        book['维护表'].merge_cells('D4:D5')
        book['维护表']['D4'] = '共享品牌'
    inspection = inspect(runtime, edit)
    result = preview(runtime, inspection)
    assert any('合并关系变化' in item for item in result['payload']['structure_summary'])
    service.apply_return(task['id'], 'local', 'operator', result['id'], False, confirm_structure=True)
    rows = service.rows(task['id'], 'local', 0, 30)['items']
    assert rows[0]['values_json']['local.lighting.attribute.brand'] == rows[1]['values_json']['local.lighting.attribute.brand'] == '共享品牌'
    assert rows[0]['source']['merged_fields'] == rows[1]['source']['merged_fields']
    runtime = (runtime[0], service, service.get(task['id'], 'local'))
    def unmerge(book):
        book['维护表'].unmerge_cells('D4:D5')
        book['维护表']['D5'] = '独立品牌'
    inspection = inspect(runtime, unmerge)
    result = preview(runtime, inspection)
    service.apply_return(task['id'], 'local', 'operator', result['id'], False, confirm_structure=True)
    rows = service.rows(task['id'], 'local', 0, 30)['items']
    assert rows[1]['values_json']['local.lighting.attribute.brand'] == '独立品牌'
    assert not rows[0]['source']['merged_fields']


def test_legacy_file_without_column_names_can_manually_map_renamed_field(runtime):
    def edit(book):
        book.defined_names.clear()
        book['维护表']['D3'] = '厂牌'
    inspection = inspect(runtime, edit)
    renamed = next(c for c in inspection['payload']['columns'] if c['title'] == '厂牌')
    assert renamed['action'] == 'new'
    cols = choices(inspection)
    column = next(c for c in cols if c['title'] == '厂牌')
    column.update(action='existing', field_id='local.lighting.attribute.brand', kind='attribute')
    assert preview(runtime, inspection, cols)['status'] == 'ready'


def test_clear_cell_is_a_real_change_and_required_validation_still_blocks_export(runtime):
    _, service, task = runtime
    inspection = inspect(runtime, lambda book: setattr(book['维护表']['D4'], 'value', None))
    result = preview(runtime, inspection)
    assert result['payload']['counts']['changed'] == 1
    updated = service.apply_return(task['id'], 'local', 'operator', result['id'], False, confirm_structure=True)
    assert updated['payload']['error_count'] == 1
    assert service.rows(task['id'], 'local', 0, 30)['items'][0]['values_json']['local.lighting.attribute.brand'] == ''
    with pytest.raises(WorkflowError, match='阻断性问题'):
        service.create_export(task['id'], 'local', updated['revision'])


def test_schema_and_records_rollback_together_on_commit_failure(runtime, monkeypatch):
    store, service, task = runtime
    inspection = inspect(runtime, lambda book: setattr(book['维护表']['D3'], 'value', '运营厂牌'))
    result = preview(runtime, inspection)
    original_rows = service.rows(task['id'], 'local', 0, 30)['items']
    original_save = store.save
    def fail_task_save(con, obj):
        if obj['id'] == task['id']:
            raise RuntimeError('simulated storage failure')
        return original_save(con, obj)
    monkeypatch.setattr(store, 'save', fail_task_save)
    with pytest.raises(RuntimeError, match='simulated storage failure'):
        service.apply_return(task['id'], 'local', 'operator', result['id'], False, confirm_structure=True)
    assert service.get(task['id'], 'local')['revision'] == 1
    assert service.get(task['id'], 'local')['payload']['config'] == task['payload']['config']
    assert service.rows(task['id'], 'local', 0, 30)['items'] == original_rows


@pytest.mark.parametrize('label',['厂牌/品牌','厂牌\n品牌'])
def test_literal_slash_label_keeps_header_hierarchy_and_dynamic_output_name(runtime,label):
    _, service, task = runtime
    inspection = inspect(runtime, lambda book: setattr(book['维护表']['D3'], 'value', label))
    result = preview(runtime, inspection)
    service.apply_return(task['id'], 'local', 'operator', result['id'], False, confirm_structure=True)
    maintenance = load_workbook(service.maintenance_file(task['id'], 'local'))
    assert maintenance['维护表']['D3'].value == label
    assert dict(maintenance['_workflow_meta'].values)['header_depth'] == 3
    maintenance.close()
    run, _ = service.create_export(task['id'], 'local', 2)
    service.generate(run['id'], 'local')
    final = load_workbook(service.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx'))
    start = run['payload']['config']['outputs'][0]['sheets'][0]['data_start_row']
    assert final.active.cell(start, 3).value.startswith(label+'：三雄极光')
    final.close()


def test_cross_task_and_tenant_cannot_use_inspection(runtime):
    store, service, task = runtime
    inspection = inspect(runtime)
    with pytest.raises(WorkflowError):
        service.preview_flexible_return('task_other', 'local', inspection['id'], FlexibleReturnRequest(columns=choices(inspection)))
    with pytest.raises(WorkflowError):
        service.preview_flexible_return(task['id'], 'another_tenant', inspection['id'], FlexibleReturnRequest(columns=choices(inspection)))


def test_old_flat_table_returns_do_not_rename_fields_to_full_path(runtime):
    _, service, task = runtime
    content = service.maintenance_file(task['id'], 'local', table_mode=True).read_bytes()
    inspection = service.inspect_return(task['id'], 'local', content)
    assert all('/' not in c['title'] for c in inspection['payload']['columns'])
    result = preview(runtime, inspection)
    assert result['payload']['counts'] == {'changed': 0, 'added': 0, 'missing': 0}
    assert result['payload']['structure_summary'] == []


def test_old_file_leaf_match_does_not_cross_dynamic_and_fixed_groups(runtime):
    def edit(book):
        book.defined_names.clear()
        book['维护表']['D3'] = '售价'
    inspection = inspect(runtime, edit)
    renamed = next(c for c in inspection['payload']['columns'] if c['column']==4)
    assert renamed['action'] == 'new' and renamed['field_id'] == '' and renamed['kind'] == 'attribute'


def test_return_keeps_column_widths_and_header_heights_after_values_shrink(runtime):
    _,service,task=runtime
    def edit(book):
        sheet=book['维护表'];sheet.column_dimensions['D'].width=55;sheet.row_dimensions[1].height=38
        sheet['D4']='短';sheet['D5']='短';sheet['D6']='短'
    inspection=inspect(runtime,edit);result=preview(runtime,inspection)
    service.apply_return(task['id'],'local','operator',result['id'],False,confirm_structure=True)
    book=load_workbook(service.maintenance_file(task['id'],'local'))
    assert book['维护表'].column_dimensions['D'].width==55
    assert book['维护表'].row_dimensions[1].height==38
    book.close()

from io import BytesIO
import json
import zipfile

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from pydantic import ValidationError

from excel_auditor.workflow.excel import write_output
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, LocalCategory, ProfileEdit, compile_profile
from excel_auditor.workflow.final_output import final_output_config
from excel_auditor.workflow.models import MappingRequest, OutputColumn, WorkflowError
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.routes import build_router
from excel_auditor.workflow.tasks import TaskService


def definition():
    return FieldProfile(fixed_fields=[
        ConfiguredField(id='sku', title='货号', type='id_code'),
        ConfiguredField(id='price', title='售价', type='decimal'),
    ], local_categories=[LocalCategory(id='lighting', title='灯具', attributes=[
        ConfiguredField(id='brand', title='品牌', required=True),
        ConfiguredField(id='series', title='系列'),
        ConfiguredField(id='name', title='名称'),
    ], specifications=[ConfiguredField(id='style', title='款式'), ConfiguredField(id='power', title='功率')])])


def test_returned_dynamic_values_export_as_two_columns_and_preview_matches(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow')
    tasks, profiles = TaskService(store), FieldProfileService(store)
    profile = definition()
    draft = profiles.get('local')
    draft = profiles.save('local', ProfileEdit(base_revision=draft['revision'], profile=profile))
    profiles.publish('local', draft['revision'])
    book = Workbook()
    sheet = book.active
    sheet.append(['货号', '售价', '品牌', '系列', '名称', '款式', '功率', '备注'])
    sheet.append(['0001', 8.5, '旧品牌', None, '防眩筒灯', 'PAM', '3W', '原样保留'])
    sheet.append(['0002', 9.5, None, None, None, 'PAM', '5W', '第二条备注'])
    sheet.merge_cells('C2:C3')
    sheet.merge_cells('E2:E3')
    content = BytesIO()
    book.save(content)
    book.close()
    task, _ = tasks.create_standardization('local', 'operator', '', '灯具.xlsx', content.getvalue())
    tasks.parse(task['id'], 'local')
    task = tasks.get(task['id'], 'local')
    source = task['payload']['sheets'][0]
    ids = [field['rule']['name'] for field in task['payload']['config']['fields']]
    task = tasks.confirm_mapping(task['id'], 'local', 'operator', MappingRequest(base_revision=task['revision'], sheets=[{
        'sheet': source['sheet'], 'header_row': source['header_row'], 'header_depth': source['header_depth'],
        'bindings': [{'field': name, 'columns': [index + 1]} for index, name in enumerate(ids)],
    }]))
    assert task['status'] == 'ready'
    original_config = task['payload']['config']
    first, _ = tasks.create_export(task['id'], 'local', task['revision'])
    tasks.generate(first['id'], 'local')
    first_path = tasks.export_file(task['id'], first['id'], 'local', '标准商品表.xlsx')
    first_bytes = first_path.read_bytes()

    # The maintenance workbook still has each dynamic field in its own column.
    maintenance = load_workbook(tasks.maintenance_file(task['id'], 'local'))
    metadata = dict((row[0], row[1]) for row in maintenance['_workflow_meta'].values)
    first_row = metadata['header_depth'] + 1
    maintenance['维护表'].cell(first_row, 4, '三雄极光')
    maintenance['维护表'].cell(first_row + 1, 7, 'D7')
    returned = BytesIO()
    maintenance.save(returned)
    maintenance.close()
    preview = tasks.preview_return(task['id'], 'local', returned.getvalue())
    assert preview['payload']['counts']['changed'] == 2
    task = tasks.apply_return(task['id'], 'local', 'operator', preview['id'], False)
    assert task['payload']['config'] == original_config
    assert len(task['payload']['config']['outputs'][0]['sheets'][0]['columns']) == 8

    # Subsequent global publishing must not rename/reorder fields in this task's export.
    profile.local_categories[0].attributes[0].title = '后来发布的品牌名'
    current = profiles.get('local')
    saved = profiles.save('local', ProfileEdit(base_revision=current['revision'], profile=profile))
    profiles.publish('local', saved['revision'])
    run, _ = tasks.create_export(task['id'], 'local', task['revision'])
    assert not run['payload']['omitted_fields']
    preview_outputs = tasks.get(task['id'], 'local')['final_outputs']
    assert all(output['display_name'].startswith('灯具_最终表_') for output in preview_outputs)
    assert run['payload']['config']['outputs'] == [
        {key: value for key, value in output.items() if key != 'display_name'}
        for output in preview_outputs
    ]
    tasks.generate(run['id'], 'local')
    path = tasks.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx')
    exported = load_workbook(path)
    start = run['payload']['config']['outputs'][0]['sheets'][0]['data_start_row']
    expected = '品牌：三雄极光、系列：、名称：防眩筒灯'
    try:
        sheet = exported.active
        assert sheet.max_column == 5
        assert sheet.cell(2, 3).value == '商品属性' and sheet.cell(2, 4).value == '商品规格'
        assert list(sheet.iter_rows(min_row=start, max_row=start + 1, values_only=True)) == [
            ('0001', 8.5, expected, '款式：PAM、功率：3W', '原样保留'),
            ('0002', 9.5, expected, '款式：D7、功率：5W', '第二条备注'),
        ]
        assert not any(region.max_row >= start and region.min_col <= 4 and region.max_col >= 3 for region in sheet.merged_cells.ranges)
        assert sheet.cell(start, 3).alignment.wrap_text
        assert sheet.row_dimensions[start].height >= 36
    finally:
        exported.close()
    assert first_path.read_bytes() == first_bytes
    with zipfile.ZipFile(tasks.export_file(task['id'], run['id'], 'local', '最终表.zip')) as bundle:
        assert bundle.read('标准商品表.xlsx') == path.read_bytes()
        assert json.loads(bundle.read('生成说明.json'))['files'][0]['sheets'][0]['columns'] == 5

    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = 'local'
        request.state.user_id = 'operator'
        request.state.role = 'owner'
        request.state.authenticated = True
        return await call_next(request)
    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    with TestClient(app) as client:
        response = client.get(f"/api/v1/workflow-tasks/{task['id']}/exports/{run['id']}/preview/标准商品表.xlsx")
        assert response.status_code == 200, response.text
        assert response.json()['sheets'][0]['rows'][start - 1][2:4] == [expected, '款式：PAM、功率：3W']
    store.engine.dispose()


def test_many_dynamic_fields_and_duplicate_names_keep_order_and_identity():
    profile = definition()
    profile.local_categories[0].attributes += [ConfiguredField(id=f'a{i}', title=f'字段{i}') for i in range(25)]
    profile.local_categories.append(LocalCategory(id='other', title='另一个字段组', attributes=[ConfiguredField(id='brand', title='品牌')]))
    config = compile_profile(profile)
    output = final_output_config(config, profile.model_dump(mode='json'))
    attributes = output.outputs[0].sheets[0].columns[2]
    assert len(attributes.sources) == 29
    assert attributes.source_labels[:3] == ['灯具·品牌', '系列', '名称']
    assert attributes.source_labels[-1] == '另一个字段组·品牌'
    assert len(config.outputs[0].sheets[0].columns) == 33
    assert final_output_config(output, profile.model_dump(mode='json')) == output


def test_no_dynamic_fields_leaves_output_unchanged():
    profile = FieldProfile(fixed_fields=[ConfiguredField(id='sku', title='货号')])
    config = compile_profile(profile)
    assert final_output_config(config, profile.model_dump(mode='json')) == config


def test_key_value_cells_preserve_zero_newlines_and_reject_excel_text_overflow(tmp_path):
    profile = definition()
    config = final_output_config(compile_profile(profile), profile.model_dump(mode='json'))
    values = {field.rule.name: '' for field in config.fields}
    values.update({'sku': '0001', 'price': '0', 'local.lighting.attribute.brand': '=literal',
                   'local.lighting.attribute.series': '0', 'local.lighting.attribute.name': '第一行\n第二行'})
    path = tmp_path / 'values.xlsx'
    write_output(path, config.outputs[0], config, [{'values_json': values}])
    book = load_workbook(path)
    cell = book.active.cell(config.outputs[0].sheets[0].data_start_row, 3)
    assert cell.value == '品牌：=literal、系列：0、名称：第一行\n第二行' and cell.data_type == 's'
    book.close()
    values['local.lighting.attribute.name'] = 'x' * 32767
    with pytest.raises(WorkflowError, match='32,767'):
        write_output(tmp_path / 'too-long.xlsx', config.outputs[0], config, [{'values_json': values}])
    with pytest.raises(ValidationError, match='一一对应'):
        OutputColumn(title='商品属性', operation='key_value', sources=['brand'], source_labels=[])

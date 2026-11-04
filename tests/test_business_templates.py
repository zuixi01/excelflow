"""Business-template isolation and the actual multi-upload/return/export API contract."""
from copy import deepcopy
from io import BytesIO

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from excel_auditor.workflow.field_profiles import (
    ConfiguredField, FieldProfile, FieldProfileService, LocalCategory, NewFieldProfile, ProfileEdit,
)
from excel_auditor.workflow.models import WorkflowError
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.routes import build_router
from excel_auditor.workflow.tasks import TaskService


def definition(name='灯具模板'):
    return FieldProfile(name=name, fixed_fields=[
        ConfiguredField(id='product_name', title='商品名称', required=True),
        ConfiguredField(id='sku', title='货号', type='id_code'),
        ConfiguredField(id='price', title='售价', type='decimal'),
    ], local_categories=[LocalCategory(id='lighting', title='灯具', attributes=[
        ConfiguredField(id='brand', title='品牌'),
    ], specifications=[ConfiguredField(id='power', title='功率')])])


def supplier(prefix):
    book = Workbook()
    book.active.title = '报价'
    book.active.append(['商品名称', '货号', '售价', '品牌', '功率', '供应商备注'])
    for index in (1, 2):
        book.active.append([f'筒灯{prefix}{index}', f'{prefix}00{index}', 10+index, '三雄极光', '3W', f'原文{prefix}{index}'])
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


@pytest.fixture
def store(tmp_path):
    value = WorkflowStore(tmp_path / 'workflow')
    yield value
    value.engine.dispose()


def publish(profiles, record, profile):
    saved = profiles.save('local', ProfileEdit(base_revision=record['revision'], profile=profile), record['id'])
    return profiles.publish('local', saved['revision'], record['id'])


def test_legacy_default_catalog_does_not_rewrite_profile_or_historical_snapshot(store):
    profiles, tasks = FieldProfileService(store), TaskService(store)
    record = publish(profiles, profiles.get('local'), definition())
    # Simulate the exact pre-template storage shape, including an old task without identity metadata.
    with store.engine.begin() as con:
        raw = store.get(con, record['id'], 'local')
        raw['payload'].pop('template_name')
        raw['payload'].pop('published_template_name')
        store.save(con, raw)
        raw = store.get(con, record['id'], 'local')
    task, _ = tasks.create_standardization('local', 'user', '旧任务', '原表.xlsx', supplier('A'))
    with store.engine.begin() as con:
        old = store.get(con, task['id'], 'local')
        old['payload'].pop('field_profile_id')
        old['payload']['field_snapshot'].pop('profile_id')
        old['payload']['field_snapshot'].pop('template_name')
        store.save(con, old)
        old = store.get(con, task['id'], 'local')
    assert profiles.list('local')['items'][0]['payload']['template_name'] == '通用商品模板'
    copy = profiles.create('local', NewFieldProfile(name='门窗模板', source_id=record['id']))
    changed = definition('门窗模板')
    changed.fixed_fields[2].title = '门窗售价'
    publish(profiles, copy, changed)
    with store.engine.connect() as con:
        assert store.get(con, record['id'], 'local') == raw
        assert store.get(con, task['id'], 'local') == old
    tasks.parse(task['id'], 'local')
    assert tasks.get(task['id'], 'local')['payload']['field_snapshot'] == old['payload']['field_snapshot']


def test_independent_drafts_versions_and_explicit_selection(store):
    profiles, tasks = FieldProfileService(store), TaskService(store)
    default = publish(profiles, profiles.get('local'), definition('通用商品模板'))
    default = profiles.get('local', default['id'])
    draft = profiles.create('local', NewFieldProfile(name='门窗模板', source_id=default['id']))
    with pytest.raises(WorkflowError, match='先发布'):
        tasks.create_standardization('local', 'user', '', '原表.xlsx', supplier('A'), field_profile_id=draft['id'])
    profile = FieldProfile.model_validate(draft['payload']['draft'])
    profile.fixed_fields[-1].enabled = False
    profile.fixed_fields.append(ConfiguredField(id='warranty', title='质保时间'))
    selected = publish(profiles, draft, profile)
    task, _ = tasks.create_standardization('local', 'user', '', '原表.xlsx', supplier('A'), 'same', selected['id'])
    original = deepcopy(task['payload']['field_snapshot'])
    assert task['payload']['template_id'] is None  # Legacy output templates are a different feature.
    assert task['payload']['field_profile_id'] == selected['id']
    assert [f['rule']['name'] for f in task['payload']['config']['fields']][:3] == ['product_name', 'sku', 'warranty']
    profile.name = '门窗模板新版'
    saved = profiles.save('local', ProfileEdit(base_revision=selected['revision'], profile=profile), selected['id'])
    assert profiles.snapshot('local', selected['id'])[1]['template_name'] == '门窗模板'
    profiles.publish('local', saved['revision'], selected['id'])
    assert tasks.get(task['id'], 'local')['payload']['field_snapshot'] == original
    assert profiles.snapshot('local', selected['id'])[1]['template_name'] == '门窗模板新版'
    assert profiles.get('local', default['id']) == default
    with pytest.raises(WorkflowError, match='新版本'):
        profiles.save('local', ProfileEdit(base_revision=selected['revision'], profile=profile), selected['id'])
    with pytest.raises(WorkflowError):
        profiles.create('another-tenant', NewFieldProfile(name='越权副本', source_id=selected['id']))
    with pytest.raises(WorkflowError, match='不同输入'):
        tasks.create_standardization('local', 'user', '', '原表.xlsx', supplier('A'), 'same', default['id'])


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = request.headers.get('X-Test-Tenant', 'local')
        request.state.user_id = 'user'
        request.state.authenticated = True
        request.state.role = request.headers.get('X-Test-Role', 'owner')
        return await call_next(request)
    @app.exception_handler(WorkflowError)
    async def errors(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status)
    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    with TestClient(app) as value:
        yield value


def test_catalog_api_permissions_and_invalid_selection(client):
    base = '/api/v1/field-profiles'
    created = client.post(base, json={'name': '测试模板'})
    assert created.status_code == 201
    item = created.json()
    assert len(client.get(base).json()['items']) == 2
    for method, path, body in [
        ('post', base, {'name': '禁止'}),
        ('put', f"{base}/{item['id']}", {'base_revision': 0, 'profile': definition().model_dump()}),
        ('post', f"{base}/{item['id']}/publish", {'base_revision': 0}),
    ]:
        assert getattr(client, method)(path, json=body, headers={'X-Test-Role': 'operator'}).status_code == 403
    assert client.get(f"{base}/{item['id']}", headers={'X-Test-Tenant': 'other'}).status_code == 404
    assert client.post(base, json={'name': '副本', 'source_id': item['id']}, headers={'X-Test-Tenant': 'other'}).status_code == 404
    assert client.post(base, json={'name': '   '}).status_code == 422
    for profile_id, status in [(item['id'], 409), ('fieldconfig_missing', 404)]:
        response = client.post('/api/v1/standardization-tasks', data={'field_profile_id': profile_id},
                               files={'excel_file': ('报价.xlsx', supplier('A'))})
        assert response.status_code == status, response.text


def header_layout(sheet, depth=3):
    return {
        'cells': [(c.coordinate, c.value, str(c._style)) for row in sheet.iter_rows(max_row=depth) for c in row],
        'merges': sorted(str(r) for r in sheet.merged_cells.ranges if r.max_row <= depth),
        'widths': {k: v.width for k, v in sheet.column_dimensions.items()},
        'heights': [sheet.row_dimensions[r].height for r in range(1, depth+1)],
        'freeze': sheet.freeze_panes,
    }


def test_selected_template_multi_upload_return_export_and_layout(client):
    def ok(response, status=200):
        assert response.status_code == status, response.text
        return response.json()
    base = '/api/v1/field-profiles'
    # Default stays unpublished; only the selected independent template can create this task.
    first = ok(client.post(base, json={'name': '灯具模板'}), 201)
    first = ok(client.put(f"{base}/{first['id']}", json={'base_revision': first['revision'], 'profile': definition().model_dump()}))
    first = ok(client.post(f"{base}/{first['id']}/publish", json={'base_revision': first['revision']}))
    created = ok(client.post('/api/v1/standardization-tasks', data={'name': '多模板全流程', 'field_profile_id': first['id']},
                            files=[('excel_file', ('供应商甲.xlsx', supplier('A'))), ('excel_file', ('供应商乙.xlsx', supplier('B')))]), 202)
    url = f"/api/v1/workflow-tasks/{created['id']}"
    task = ok(client.get(url))
    assert task['status'] == 'mapping', task['payload'].get('error')
    snapshot = deepcopy(task['payload']['field_snapshot'])
    changed = definition()
    changed.local_categories[0].attributes[0].title = '新版厂牌'
    newer = ok(client.put(f"{base}/{first['id']}", json={'base_revision': first['revision'], 'profile': changed.model_dump()}))
    ok(client.post(f"{base}/{first['id']}/publish", json={'base_revision': newer['revision']}))
    task = ok(client.post(url+'/mapping', json={'base_revision': task['revision'], 'sheets': [
        {k: s[k] for k in ('sheet', 'header_row', 'header_depth')} | {'bindings': s['suggested_bindings']}
        for s in task['payload']['sheets']
    ]}))
    assert task['status'] == 'ready', task['payload']
    assert task['payload']['field_snapshot'] == snapshot
    original_rows = ok(client.get(url+'/maintenance'))['items']
    response = client.get(url+'/maintenance/download')
    assert response.status_code == 200
    book = load_workbook(BytesIO(response.content))
    sheet = book['维护表']
    layout = header_layout(sheet)
    assert sheet.column_dimensions['A'].hidden and sheet['B1'].value == '固定字段'
    assert sheet['E1'].value == '动态字段' and sheet['E2'].value == '商品属性' and sheet['F2'].value == '商品规格'
    old_rows = [[c.value for c in row] for row in sheet.iter_rows(min_row=4)]
    assert len(old_rows) == 4
    added = list(old_rows[0])
    added[:4] = [None, '新增射灯', 'NEW001', 88]
    old_rows[0][4] = '回传品牌'
    for row_index, values in enumerate([old_rows[3], added, old_rows[0], old_rows[1]], 4):
        for col_index, value in enumerate(values, 1):
            sheet.cell(row_index, col_index).value = value
    assert header_layout(sheet) == layout
    stream = BytesIO()
    book.save(stream)
    book.close()
    inspection = ok(client.post(url+'/maintenance/returns/inspect', files={'excel_file': ('维护回传.xlsx', stream.getvalue())}))
    columns = [{k: c[k] for k in ('key', 'action', 'field_id', 'title', 'kind', 'type')} for c in inspection['payload']['columns']]
    preview = ok(client.post(url+f"/maintenance/returns/{inspection['id']}/preview", json={'columns': columns}))
    assert preview['status'] == 'ready', preview['payload']
    assert preview['payload']['counts'] == {'changed': 1, 'added': 1, 'missing': 1}
    task = ok(client.post(url+'/maintenance/returns/apply', json={'preview_id': preview['id'], 'delete_missing': True, 'confirm_structure': True}))
    assert task['payload']['field_snapshot'] == snapshot
    rows = ok(client.get(url+'/maintenance'))['items']
    assert [r['values_json']['sku'] for r in rows] == ['B002', 'NEW001', 'A001', 'A002']
    assert [rows[i]['id'] for i in (0, 2, 3)] == [original_rows[i]['id'] for i in (3, 0, 1)]
    returned = load_workbook(BytesIO(client.get(url+'/maintenance/download').content))
    assert header_layout(returned['维护表']) == layout
    returned.close()
    run = ok(client.post(url+'/exports', json={'base_revision': task['revision']}), 202)
    task = ok(client.get(url))
    assert task['exports'][0]['status'] == 'completed', task['exports'][0]['payload']
    response = client.get(url+f"/exports/{run['id']}/files/标准商品表.xlsx")
    assert response.status_code == 200, response.text[:500]
    final = load_workbook(BytesIO(response.content))
    sheet = final.active
    values = list(sheet.iter_rows(min_row=4, values_only=True))
    assert values[2][:5] == ('筒灯A1', 'A001', 11, '品牌：回传品牌', '功率：3W')
    assert values[0][:5] == ('筒灯B2', 'B002', 12, '品牌：三雄极光', '功率：3W')
    assert '原文A1' in values[2][5:] and '原文B2' in values[0][5:]
    assert not any('新版厂牌' in str(v) for row in values for v in row)
    assert sheet['A1'].value == '固定字段' and sheet['D1'].value == '动态字段'
    assert sheet['D2'].value == '商品属性' and sheet['E2'].value == '商品规格'
    final.close()

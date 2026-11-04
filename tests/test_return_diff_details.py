from copy import deepcopy

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from excel_auditor.workflow.models import WorkflowError
from excel_auditor.workflow.return_details import page_details, write_details
from excel_auditor.workflow.routes import build_router
from test_flexible_maintenance_return import runtime, inspect, preview
from openpyxl import load_workbook


def test_full_paging_beyond_previous_sample_limits(tmp_path):
    old = [{'id': f'row_{i}', 'values_json': {'product_name': f'灯具{i}', 'price': '10'},
            'source': {'filename': '灯具.xlsx', 'sheet': 'Sheet1', 'row': i+4}} for i in range(155)]
    proposed = deepcopy(old[:130])
    for row in proposed:
        row['values_json']['price'] = '12'
    proposed += [{'id': f'new_{i}', 'values_json': {'product_name': f'新增{i}'}, 'source': {}} for i in range(25)]
    manifest = write_details(tmp_path, old, proposed=proposed, missing=[r['id'] for r in old[130:]])
    assert manifest['counts'] == {'changed': 130, 'added': 25, 'missing': 25}
    # Page contents are immutable snapshots even when task data changes later.
    old[120]['values_json']['price'] = '999'
    result = page_details(tmp_path, manifest, 'changed', 120, 10)
    assert result['total'] == 130 and len(result['items']) == 10
    assert result['items'][0]['before'] == {'price': '10'}
    assert result['items'][0]['after'] == {'price': '12'}
    assert result['items'][0]['name'] == '灯具120'
    assert result['items'][0]['source']['row'] == 124
    assert page_details(tmp_path, manifest, 'added', 20)['items'][-1]['values']['product_name'] == '新增24'
    assert page_details(tmp_path, manifest, 'missing', 20)['items'][-1]['id'] == 'row_154'
    assert page_details(tmp_path, manifest, 'changed', 130)['items'] == []
    assert page_details(tmp_path, manifest, 'changed', -1, 100)['limit'] == 50
    with pytest.raises(WorkflowError):
        page_details(tmp_path, manifest, 'unknown')
    with pytest.raises(WorkflowError):
        page_details(tmp_path, {'token': '../outside'}, 'changed')


def test_legacy_snapshot_and_unchanged_rows(tmp_path):
    rows = {'r1': {'id': 'r1', 'values_json': {'sku': '001', 'price': '10'}, 'source': {}}}
    manifest = write_details(tmp_path, rows, changes={'r1': {'price': ''}}, additions=[{'sku': '002'}])
    assert page_details(tmp_path, manifest, 'changed')['items'][0]['after'] == {'price': ''}
    assert page_details(tmp_path, manifest, 'added')['items'][0]['sku'] == '002'
    unchanged = write_details(tmp_path, rows, proposed=list(rows.values()))
    assert unchanged['counts'] == {'changed': 0, 'added': 0, 'missing': 0}


def test_flexible_details_api_before_and_after_apply(runtime, tmp_path):
    store, service, task = runtime
    with store.engine.begin() as con:
        owned = store.get(con, task['id'], 'local', 'task')
        owned['payload']['actor'] = 'local'
        store.save(con, owned)
    before = service.rows(task['id'], 'local', 0, 30)['items']
    def edit(book):
        sheet = book['维护表']
        sheet['C4'] = '19.5'
        # Replace the last row with a new record: one add and one missing.
        sheet['A6'] = None
        sheet['B6'] = 'new-sku'
    result = preview(runtime, inspect(runtime, edit))
    counts = {'changed': 1, 'added': 1, 'missing': 1}
    assert result['payload']['counts'] == result['payload']['detail_manifest']['counts'] == counts
    assert service.rows(task['id'], 'local', 0, 30)['items'] == before
    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, call_next):
        request.state.tenant_id = request.headers.get('x-test-tenant', 'local')
        request.state.user_id = request.headers.get('x-test-user', 'local')
        return await call_next(request)
    @app.exception_handler(WorkflowError)
    async def errors(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status)
    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    root = f"/api/v1/workflow-tasks/{task['id']}/maintenance/returns/{result['id']}/details"
    with TestClient(app) as client:
        response = client.get(root)
        assert response.status_code == 200, response.text
        changed = response.json()
        assert changed['items'][0]['before'] == {'price': '10'}
        assert changed['items'][0]['after'] == {'price': '19.5'}
        assert changed['items'][0]['location'] == {'sheet': '维护表', 'row': 4}
        assert changed['items'][0]['cells']['price'] == {'sheet': '维护表', 'row': 4, 'column': 3, 'letter': 'C', 'address': 'C4'}
        assert client.get(root, params={'kind': 'added'}).json()['items'][0]['sku'] == 'new-sku'
        assert client.get(root, params={'kind': 'missing'}).json()['items'][0]['id'] == 'row_2'
        assert client.get(root, headers={'x-test-tenant': 'other'}).status_code == 404
        assert client.get(root, headers={'x-test-user': 'other'}).status_code == 404
        assert client.get(root.replace(task['id'], 'task_other')).status_code == 404
        service.apply_return(task['id'], 'local', 'operator', result['id'], False, confirm_structure=True)
        assert service.get(task['id'], 'local')['payload']['record_count'] == 3
        assert client.get(root).json() == changed


def test_return_coordinates_follow_sort_and_rename_not_source_row(runtime):
    store, service, task = runtime
    def edit(book):
        sheet = book['维护表']
        sheet['D3'] = '厂牌'
        first = [cell.value for cell in sheet[4]]
        second = [cell.value for cell in sheet[5]]
        for index, values in enumerate([second, first], 4):
            for column, value in enumerate(values, 1):
                sheet.cell(index, column).value = value
        sheet['C5'] = '21'
    result = preview(runtime, inspect(runtime, edit))
    assert {'kind': '改名', 'field': '厂牌', 'before': '品牌', 'after': '厂牌',
            'location': '维护表 · D 列', 'original_column': 'D'} in result['payload']['field_changes']
    item = page_details(store.directory(result['id']), result['payload']['detail_manifest'], 'changed')['items'][0]
    assert item['id'] == 'row_0'
    assert item['location']['row'] == 5
    assert item['original_location']['row'] == 4
    assert item['source']['row'] == 2  # Original supplier file, never displayed as return location.
    assert item['cells']['price']['address'] == 'C5'
    assert item['before']['price'] == '10' and item['after']['price'] == '21'


def test_insert_without_id_or_sku_gets_persistent_id_and_roundtrips(runtime):
    store, service, task = runtime
    def edit(book):
        sheet = book['维护表']
        for column, cell in enumerate(list(sheet[4]), 1):
            sheet.cell(7, column).value = cell.value
        sheet['A7'] = sheet['B7'] = None
        sheet['C7'] = '88'
        sheet['F7'] = '运营新建商品'
    result = preview(runtime, inspect(runtime, edit))
    assert result['status'] == 'ready' and result['payload']['row_review'] == []
    assert result['payload']['counts'] == {'changed': 0, 'added': 1, 'missing': 0}
    added = page_details(store.directory(result['id']), result['payload']['detail_manifest'], 'added')['items'][0]
    assert added['sku'] == '' and added['location']['row'] == 7
    assert added['id'].startswith('row_') and added['id'] not in {'row_0','row_1','row_2'}
    service.apply_return(task['id'], 'local', 'operator', result['id'], False, confirm_structure=True)
    rows = service.rows(task['id'], 'local', 0, 30)['items']
    assert len(rows) == 4 and rows[-1]['id'] == added['id']
    book = load_workbook(service.maintenance_file(task['id'], 'local'))
    assert book['维护表']['A7'].value == added['id'] and book['维护表'].column_dimensions['A'].hidden
    book.close()
    again = preview(runtime, inspect(runtime))
    assert again['status'] == 'ready' and again['payload']['counts'] == {'changed': 0, 'added': 0, 'missing': 0}


def test_identity_review_keeps_known_differences_visible_and_blocks_save(runtime):
    store, service, task = runtime
    def edit(book):
        sheet = book['维护表']
        sheet['A4'] = None  # Matches an existing SKU: still requires review.
        sheet['C5'] = '77'
        sheet['A6'] = sheet['B6'] = None
        sheet['F6'] = '新增无货号商品'
    inspection = inspect(runtime, edit)
    result = preview(runtime, inspection)
    assert result['status'] == 'row_review'
    assert result['payload']['counts']['changed'] == result['payload']['counts']['added'] == 1
    assert len(result['payload']['row_review']) == 1
    assert page_details(store.directory(result['id']), result['payload']['detail_manifest'], 'changed')['items'][0]['cells']['price']['address'] == 'C5'
    assert page_details(store.directory(result['id']), result['payload']['detail_manifest'], 'added')['items'][0]['location']['row'] == 6
    with pytest.raises(WorkflowError):
        service.apply_return(task['id'], 'local', 'operator', result['id'], True, confirm_structure=True)
    assert service.get(task['id'], 'local')['revision'] == 1
    pending = result['payload']['row_review'][0]
    resolved = preview(runtime, inspection, rows=[{'key':pending['key'],'action':'existing','record_id':'row_0'}])
    assert resolved['status'] == 'ready'
    assert resolved['payload']['counts'] == {'changed':1,'added':1,'missing':1}

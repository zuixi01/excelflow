from io import BytesIO
import json
import zipfile

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from excel_auditor.workflow import source_files
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.models import MappingRequest, WorkflowError
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.routes import build_router
from excel_auditor.workflow.tasks import TaskService


def supplier(code, note, *, formula=False):
    book = Workbook()
    sheet = book.active
    sheet.title = '商品'
    sheet.append(['商品名称', '货号', '销售价', '备注'])
    sheet.append(['同名商品', code + '1', '=1/0' if formula else 10, note])
    sheet.append([None, code + '2', 20, None])
    sheet.merge_cells('A2:A3')
    sheet.merge_cells('D2:D3')
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def publish(store):
    profiles = FieldProfileService(store)
    draft = profiles.get('local')
    config = FieldProfile(fixed_fields=[
        ConfiguredField(id='product_name', title='商品名称', required=True),
        ConfiguredField(id='sku', title='货号', type='id_code'),
        ConfiguredField(id='sale_price', title='销售价', type='decimal', min='0'),
    ])
    saved = profiles.save('local', ProfileEdit(base_revision=draft['revision'], profile=config))
    profiles.publish('local', saved['revision'])


@pytest.fixture
def runtime(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow')
    publish(store)
    yield store, TaskService(store)
    store.engine.dispose()


def create_and_parse(tasks, files):
    task, _ = tasks.create_standardization_files('local', 'operator', '', files)
    tasks.parse(task['id'], 'local')
    return tasks.get(task['id'], 'local')


def test_same_filename_and_sheet_keep_all_records_merges_and_roundtrip(runtime):
    store, tasks = runtime
    originals = [supplier('000', '供应商甲'), supplier('001', '供应商乙')]
    task = create_and_parse(tasks, [('报价.xlsx', content) for content in originals])
    assert task['status'] == 'mapping', task['payload'].get('error')
    sheets = task['payload']['sheets']
    assert len({s['sheet'] for s in sheets}) == 2
    assert [s['source_sheet'] for s in sheets] == ['商品', '商品']
    assert [s['source_file_index'] for s in sheets] == [1, 2]
    assert [(store.directory(task['id']) / source['storage_name']).read_bytes()
            for source in task['payload']['source_files']] == originals
    task = tasks.confirm_mapping(task['id'], 'local', 'operator', MappingRequest(base_revision=task['revision'], sheets=[
        {'sheet': s['sheet'], 'header_row': s['header_row'], 'header_depth': s['header_depth'], 'bindings': s['suggested_bindings']}
        for s in sheets
    ]))
    assert task['status'] == 'ready'
    rows = tasks.rows(task['id'], 'local', 0, 100)['items']
    assert [r['values_json']['sku'] for r in rows] == ['0001', '0002', '0011', '0012']
    assert [r['source']['file_index'] for r in rows] == [1, 1, 2, 2]
    assert all(r['source']['filename'] == '报价.xlsx' and r['source']['original_sheet'] == '商品' for r in rows)
    assert rows[0]['source']['merged_fields']['product_name'] != rows[2]['source']['merged_fields']['product_name']
    extras = [f['rule']['name'] for f in task['payload']['config']['fields'] if f.get('source_column')]
    assert len(extras) == 2
    assert [[r['values_json'][f] for f in extras] for r in rows] == [
        ['供应商甲', ''], ['供应商甲', ''], ['', '供应商乙'], ['', '供应商乙']]

    maintenance = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(maintenance)
    meta = dict((r[0], r[1]) for r in book['_workflow_meta'].values)
    # The stable record ID is column A; SKU is the second configured field.
    book['维护表'].cell(meta['header_depth'] + 3, 3, '0011-edited')
    content = BytesIO()
    book.save(content)
    book.close()
    preview = tasks.preview_return(task['id'], 'local', content.getvalue())
    assert preview['payload']['counts'] == {'changed': 1, 'added': 0, 'missing': 0}
    task = tasks.apply_return(task['id'], 'local', 'operator', preview['id'], False)
    assert tasks.rows(task['id'], 'local', 0, 100)['items'][2]['values_json']['sku'] == '0011-edited'
    run, _ = tasks.create_export(task['id'], 'local', task['revision'])
    tasks.generate(run['id'], 'local')
    result = tasks.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx')
    book = load_workbook(result)
    try:
        assert book.sheetnames == ['标准商品']
        start = task['payload']['config']['outputs'][0]['sheets'][0]['data_start_row']
        assert book.active.max_row == start + 3
        assert [book.active.cell(r, 2).value for r in range(start, start + 4)] == ['0001', '0002', '0011-edited', '0012']
        merges = [r for r in book.active.merged_cells.ranges if r.min_col == 1 and r.min_row >= start]
        assert sorted((r.min_row, r.max_row) for r in merges) == [(start, start + 1), (start + 2, start + 3)]
    finally:
        book.close()
    manifest = json.loads(result.with_name('生成说明.json').read_text(encoding='utf-8'))
    assert len(manifest['source_files']) == 2


def test_multi_upload_idempotency_includes_order_names_and_content(runtime):
    _, tasks = runtime
    files = [('甲.xlsx', supplier('000', '甲')), ('乙.xlsx', supplier('001', '乙'))]
    task, created = tasks.create_standardization_files('local', 'operator', '批量', files, 'request-1')
    again, repeated = tasks.create_standardization_files('local', 'operator', '批量', files, 'request-1')
    assert created and not repeated and again['id'] == task['id']
    for changed in [list(reversed(files)), [('新名称.xlsx', files[0][1]), files[1]], [files[0], ('乙.xlsx', supplier('002', '丙'))]]:
        with pytest.raises(WorkflowError) as conflict:
            tasks.create_standardization_files('local', 'operator', '批量', changed, 'request-1')
        assert conflict.value.status == 409


@pytest.mark.parametrize('invalid', [b'not excel', supplier('001', '公式', formula=True)])
def test_one_invalid_file_fails_whole_task_without_importing_partial_records(runtime, invalid):
    store, tasks = runtime
    task = create_and_parse(tasks, [('正常.xlsx', supplier('000', '正常')), ('有问题.xlsx', invalid)])
    assert task['status'] == 'failed'
    assert '有问题.xlsx' in task['payload']['error']
    assert tasks.rows(task['id'], 'local', 0, 100)['total'] == 0
    assert not (store.directory(task['id']) / 'source-data.json').exists()


@pytest.mark.parametrize('limit,value,message', [('MAX_ROWS', 3, '20,000'), ('MAX_CELLS', 20, '200 万'), ('MAX_SOURCE_SHEETS', 1, '50 个')])
def test_limits_apply_across_files(runtime, monkeypatch, limit, value, message):
    monkeypatch.setattr(source_files, limit, value)
    task = create_and_parse(runtime[1], [('甲.xlsx', supplier('000', '甲')), ('乙.xlsx', supplier('001', '乙'))])
    assert task['status'] == 'failed' and message in task['payload']['error']


def test_single_file_and_old_tasks_still_parse(runtime):
    store, tasks = runtime
    task, _ = tasks.create_standardization('local', 'operator', '', '旧表.xlsx', supplier('000', '备注'))
    assert (store.directory(task['id']) / 'source.xlsx').exists()
    with store.engine.begin() as con:
        saved = store.get(con, task['id'], 'local')
        saved['payload'].pop('source_files')
        store.save(con, saved)
    tasks.parse(task['id'], 'local')
    task = tasks.get(task['id'], 'local')
    assert task['status'] == 'mapping'
    assert task['payload']['sheets'][0]['sheet'] == '商品'


@pytest.fixture
def client(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow')
    publish(store)
    app = FastAPI()
    @app.middleware('http')
    async def identity(request: Request, next_call):
        request.state.tenant_id = 'local'
        request.state.user_id = 'operator'
        request.state.authenticated = True
        request.state.role = 'owner'
        return await next_call(request)
    @app.exception_handler(WorkflowError)
    async def error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status)
    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    with TestClient(app) as client:
        yield client
    store.engine.dispose()


def test_multipart_accepts_repeated_file_field_and_preserves_single_upload(client):
    files = [('excel_file', ('同名.xlsx', supplier('000', '甲'))), ('excel_file', ('同名.xlsx', supplier('001', '乙')))]
    created = client.post('/api/v1/standardization-tasks', files=files)
    assert created.status_code == 202, created.text
    task = client.get('/api/v1/workflow-tasks/' + created.json()['id']).json()
    assert task['status'] == 'mapping' and len(task['payload']['sheets']) == 2
    single = client.post('/api/v1/standardization-tasks', files={'excel_file': ('single.xlsx', supplier('000', '单个'))})
    assert single.status_code == 202, single.text
    task = client.get('/api/v1/workflow-tasks/' + single.json()['id']).json()
    assert task['payload']['sheets'][0]['sheet'] == '商品'


def test_upload_count_type_and_combined_size_rejected_before_task_creation(client, monkeypatch):
    endpoint = '/api/v1/standardization-tasks'
    assert client.post(endpoint).status_code == 422
    assert client.post(endpoint, files=[('excel_file', ('a.xlsx', b'a'))] * 31).status_code == 422
    assert client.post(endpoint, files=[('excel_file', ('a.xlsx', b'a')), ('excel_file', ('b.csv', b'b'))]).status_code == 415
    monkeypatch.setattr(source_files, 'MAX_TOTAL_UPLOAD', 3)
    assert client.post(endpoint, files=[('excel_file', ('a.xlsx', b'aa')), ('excel_file', ('b.xlsx', b'bb'))]).status_code == 413
    assert client.get('/api/v1/workflow-tasks').json()['total'] == 0


def test_thirty_files_can_be_parsed_in_one_task(client):
    content = supplier('000', '批量')
    response = client.post('/api/v1/standardization-tasks', files=[
        ('excel_file', (f'{i}.xlsx', content)) for i in range(30)
    ])
    assert response.status_code == 202, response.text
    task = client.get('/api/v1/workflow-tasks/' + response.json()['id']).json()
    assert task['status'] == 'mapping', task['payload'].get('error')
    assert len(task['payload']['sheets']) == 30
    assert len({s['sheet'] for s in task['payload']['sheets']}) == 30


def test_per_file_and_combined_limits_are_independent(monkeypatch):
    assert source_files.MAX_SOURCE_FILES == 30
    assert source_files.MAX_UPLOAD == 100 * 1024 * 1024
    assert source_files.MAX_TOTAL_UPLOAD == 500 * 1024 * 1024
    # Scale byte limits while exercising the exact boundary comparisons.
    monkeypatch.setattr(source_files, 'MAX_UPLOAD', 100)
    monkeypatch.setattr(source_files, 'MAX_TOTAL_UPLOAD', 500)
    source_files.validate_uploads([('a.xlsx', b'a' * 100)] * 5)
    with pytest.raises(WorkflowError, match='合计不能超过 500 MiB') as total_error:
        source_files.validate_uploads([('a.xlsx', b'a' * 100)] * 5 + [('b.xlsx', b'b')])
    assert total_error.value.status == 413
    with pytest.raises(WorkflowError, match='单个 Excel 文件不能超过 100 MiB') as file_error:
        source_files.validate_uploads([('a.xlsx', b'a' * 101)])
    assert file_error.value.status == 413


def test_upload_above_old_twenty_mib_total_is_accepted_and_parsed(client):
    book = BytesIO(supplier('000', '大批次'))
    # A stored inert part increases transport size without creating huge cell data.
    with zipfile.ZipFile(book, 'a') as archive:
        archive.writestr('upload-test-padding.bin', b'0' * (11 * 1024 * 1024), compress_type=zipfile.ZIP_STORED)
    content = book.getvalue()
    assert 20 * 1024 * 1024 < 2 * len(content) < source_files.MAX_UPLOAD < source_files.MAX_TOTAL_UPLOAD
    response = client.post('/api/v1/standardization-tasks', files=[
        ('excel_file', ('large-a.xlsx', content)), ('excel_file', ('large-b.xlsx', content))
    ])
    assert response.status_code == 202, response.text
    task = client.get('/api/v1/workflow-tasks/' + response.json()['id']).json()
    assert task['status'] == 'mapping', task['payload'].get('error')
    assert len(task['payload']['sheets']) == 2


def test_single_file_over_old_twenty_and_fifty_mib_and_total_over_old_hundred_are_parsed(client):
    book = BytesIO(supplier('100', '超过旧容量限制'))
    with zipfile.ZipFile(book, 'a') as archive:
        with archive.open('upload-test-padding.bin', 'w', force_zip64=True) as part:
            for _ in range(51):
                part.write(b'0' * (1024 * 1024))
    content = book.getvalue()
    assert 50 * 1024 * 1024 < len(content) < source_files.MAX_UPLOAD
    assert 100 * 1024 * 1024 < len(content) * 2 < source_files.MAX_TOTAL_UPLOAD
    response = client.post('/api/v1/standardization-tasks', files=[
        ('excel_file', ('51MiB-a.xlsx', content)), ('excel_file', ('51MiB-b.xlsx', content))
    ])
    assert response.status_code == 202, response.text
    task = client.get('/api/v1/workflow-tasks/' + response.json()['id']).json()
    assert task['status'] == 'mapping', task['payload'].get('error')
    assert len(task['payload']['sheets']) == 2


def test_workflow_uncompressed_limit_expanded_without_changing_legacy_supplier_limit(tmp_path):
    from excel_auditor.workflow.excel import safe_book
    from excel_auditor.field_reconciliation import open_supplier_book
    path = tmp_path / 'large-expanded.xlsx'
    path.write_bytes(supplier('200', '压缩后很小的有效Excel'))
    with zipfile.ZipFile(path, 'a', compression=zipfile.ZIP_DEFLATED) as archive:
        with archive.open('expanded-test-padding.bin', 'w', force_zip64=True) as part:
            for _ in range(101):
                part.write(b'0' * (1024 * 1024))
    for supplier_mode in (True, False):
        book = safe_book(path, supplier=supplier_mode)
        assert book['商品']['B2'].value == '2001'
        book.close()
    with pytest.raises(ValueError, match='解压后超过 100 MiB'):
        open_supplier_book(path)

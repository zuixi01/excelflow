from __future__ import annotations

import json
from io import BytesIO

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from pydantic import SecretStr, ValidationError
from excel_auditor.models import ColumnRule

from excel_auditor.workflow.ai import AIClient, AISettings
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, LocalCategory, ProfileEdit, compile_profile
from excel_auditor.workflow.models import EditRequest, MappingRequest, WorkflowError, content_hash, MaintenanceField
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.routes import build_router
from excel_auditor.workflow.tasks import TaskService
from excel_auditor.workflow.excel import read_maintenance


@pytest.fixture
def runtime(tmp_path):
    store = WorkflowStore(tmp_path / "workflow")
    yield store, TaskService(store), FieldProfileService(store)
    store.engine.dispose()


def profile():
    return FieldProfile(fixed_fields=[
        ConfiguredField(id="product_name", title="商品名称", aliases=["产品名称"], required=True),
        ConfiguredField(id="brand", title="商品品牌", aliases=["品牌", "产品品牌"]),
        ConfiguredField(id="sku", title="货号", aliases=["产品型号"], type="id_code"),
        ConfiguredField(id="sale_price", title="销售价", type="decimal", min="0"),
    ], local_categories=[
        LocalCategory(id="lighting", title="灯具", attributes=[ConfiguredField(id="brand", title="品牌", aliases=["产品品牌"], required=True)],
                      specifications=[ConfiguredField(id="power", title="功率", required=True)]),
        LocalCategory(id="tile", title="瓷砖", attributes=[ConfiguredField(id="origin", title="产地", required=True)]),
    ])


def publish(service, value=None):
    draft = service.get("local")
    draft = service.save("local", ProfileEdit(base_revision=draft["revision"], profile=value or profile()))
    return service.publish("local", draft["revision"])


def supplier():
    book = Workbook()
    lamps = book.active
    lamps.title = "灯具"
    lamps.append(["供应商报价"])
    lamps.append(["产品名称", "产品品牌", "产品型号", "功率", "销售价"])
    lamps.append(["筒灯", "示例品牌", "0001", "3W", 8.4])
    lamps.append(["备注：含运输，不含安装"])
    lamps.merge_cells("A4:E4")
    tiles = book.create_sheet("瓷砖")
    tiles.append(["商品名称", "属性", None, "销售价"])
    tiles.append([None, "产地", "厚度", None])
    tiles.merge_cells("A1:A2")
    tiles.merge_cells("B1:C1")
    tiles.merge_cells("D1:D2")
    tiles.append(["墙砖", "广州", "8mm", 19])
    windows = book.create_sheet("封窗")
    windows.append(["商品名称", "类别", "销售价"])
    windows.append(["外开窗", "固定位", 899])
    windows.append([None, "开启扇", 999])
    windows.merge_cells("A2:A3")
    output = book.create_sheet("输出说明")
    output.append(["固定字段", "动态字段（运营平台提供接口）"])
    output.append(["商品名称", "功率"])
    output.append(["举例", "3W"])
    output.append(['=DISPIMG("example",1)'])
    result = BytesIO()
    book.save(result)
    book.close()
    return result.getvalue()


def create(runtime, content=None, *, legacy=False):
    store, tasks, profiles = runtime
    publish(profiles)
    obj, _ = tasks.create_standardization("local", "local", "测试整理", "供应商.xlsx", content or supplier())
    if legacy:
        # Reconstruct a previously stored per-category snapshot, not the current public creation flow.
        with store.engine.begin() as con:
            saved = store.get(con, obj['id'], 'local')
            config = compile_profile(profile()).model_dump(mode='json')
            config['category_field'] = 'source_category'
            groups = {'lighting': '灯具', 'tile': '瓷砖'}
            for field in config['fields']:
                if field['rule']['name'].startswith('local.'):
                    group = field['rule']['name'].split('.')[1]
                    field['category_id'] = group
                    parts = field['rule']['title'].split('/')
                    field['rule']['title'] = '/'.join([groups[group], parts[1], parts[-1]])
            config['fields'].append(MaintenanceField(rule=ColumnRule(name='source_category', title='来源类目', type='string'),
                                                    editable=False, hidden=True, default='unclassified').model_dump(mode='json'))
            columns = config['outputs'][0]['sheets'][0]['columns']
            fixed = [c for c in columns if not c['field'].startswith('local.')]
            sheets = [{'name': '固定字段', 'columns': fixed, 'header_row': 1, 'data_start_row': 4,
                       'filter_field': 'source_category', 'filter_value': 'unclassified'}]
            for group, title in groups.items():
                dynamic = [{**c, 'title': '/'.join([*c['title'].split('/')[:2], c['title'].split('/')[-1]])}
                           for c in columns if c['field'].startswith(f'local.{group}.')]
                sheets.append({'name': title, 'columns': fixed + dynamic, 'header_row': 1, 'data_start_row': 4,
                               'filter_field': 'source_category', 'filter_value': group})
            config['outputs'][0]['sheets'] = sheets
            saved['payload'].update(config=config, template_hash=content_hash(config))
            saved['payload']['field_snapshot'].pop('matching_scope')
            store.save(con, saved)
    tasks.parse(obj["id"], "local")
    task = tasks.get(obj["id"], "local")
    assert task["status"] == "mapping", task["payload"].get("error")
    return task


def mapping(task):
    return MappingRequest(base_revision=task["revision"], sheets=[{
        "sheet": s["sheet"], "header_row": s["header_row"], "header_depth": s["header_depth"],
        "category_id": s.get("category_id", "unclassified"), "bindings": s["suggested_bindings"],
    } for s in task["payload"]["sheets"]])


def test_configuration_revision_publish_permissions_and_snapshot(runtime):
    _, tasks, profiles = runtime
    with pytest.raises(WorkflowError, match="先发布"):
        tasks.create_standardization("local", "local", "", "supplier.xlsx", supplier())
    task = create(runtime)
    old = task["payload"]["config"]
    current = profiles.get("local")
    updated = profile()
    updated.fixed_fields[0].title = "新商品名称"
    draft = profiles.save("local", ProfileEdit(base_revision=current["revision"], profile=updated))
    with pytest.raises(WorkflowError, match="新版本"):
        profiles.save("local", ProfileEdit(base_revision=current["revision"], profile=updated))
    profiles.publish("local", draft["revision"])
    assert tasks.get(task["id"], "local")["payload"]["config"] == old
    assert task["payload"]["field_snapshot"]["version"] == 1
    newer, _ = tasks.create_standardization("local", "local", "", "new.xlsx", supplier())
    assert newer["payload"]["field_snapshot"]["version"] == 2
    removed = profile()
    removed.fixed_fields.pop()
    with pytest.raises(WorkflowError, match="停用"):
        profiles.save("local", ProfileEdit(base_revision=profiles.get("local")["revision"], profile=removed))
    assert profiles.get("different")["payload"]["published"] is None


def test_all_fields_are_analyzed_when_supplier_sheet_names_do_not_match_groups(runtime):
    book = load_workbook(BytesIO(supplier()))
    book['灯具'].title = '供应商A'
    book['瓷砖'].title = '报价明细'
    content = BytesIO()
    book.save(content)
    book.close()
    task = create(runtime, content.getvalue())
    assert task['payload']['field_snapshot']['matching_scope'] == 'all_fields'
    config = task['payload']['config']
    assert not config['category_field'] and all(not field['category_id'] for field in config['fields'])
    assert len(config['outputs'][0]['sheets']) == 1
    assert len(config['outputs'][0]['sheets'][0]['columns']) == 7
    assert not config['outputs'][0]['sheets'][0]['filter_field']
    sheets = task['payload']['sheets']
    lamp_bindings = {b['field']: b for b in sheets[0]['suggested_bindings']}
    assert lamp_bindings['brand']['columns'] == lamp_bindings['local.lighting.attribute.brand']['columns'] == [2]
    assert 'local.tile.attribute.origin' in {b['field'] for b in sheets[1]['suggested_bindings']}
    assert all(s['mapping_summary']['total'] == 7 for s in sheets)
    assert 'local.tile.attribute.origin' in {f['field'] for f in sheets[0]['unmatched_fields']}
    assert not any('尚未选择动态字段类目' in note for s in sheets for note in s['mapping_notes'])


def test_model_receives_unmatched_dynamic_fields_from_all_groups_on_every_sheet(runtime, monkeypatch):
    seen = []
    monkeypatch.setattr(AIClient, 'settings', lambda self: {'configured': True})
    def reconcile(self, fields, columns, **kwargs):
        seen.append((fields, columns))
        assert all(set(c) == {'column','header','source_column','sheet_id','sheet_name'} for c in columns)
        return []
    monkeypatch.setattr(AIClient, 'reconcile_field_mappings', reconcile)
    task = create(runtime)
    assert len(seen) == 1
    fields, columns = seen[0]
    targets = [{f['id'].split('::',1)[1] for f in fields if f['sheet_name']==sheet['sheet']} for sheet in task['payload']['sheets']]
    assert 'local.tile.attribute.origin' in targets[0]
    assert 'local.lighting.specification.power' in targets[1]
    assert {'local.tile.attribute.origin', 'local.lighting.specification.power'} <= targets[2]
    assert all(s['mapping_summary']['model_status'] == 'completed' for s in task['payload']['sheets'])


@pytest.mark.parametrize('headers,expected', [
    (['动态字段 / 商品属性 / 灯具 / 材质'], {'local.lighting.attribute.material': [1]}),
    (['动态字段 / 商品属性 / 灯具 / 材质', '动态字段 / 商品属性 / 瓷砖 / 材质'],
     {'local.lighting.attribute.material': [1], 'local.tile.attribute.material': [2]}),
    (['材质'], {'local.lighting.attribute.material': [1], 'local.tile.attribute.material': [1]}),
])
def test_explicit_group_header_does_not_leak_to_another_groups_same_leaf(runtime, headers, expected):
    from excel_auditor.workflow.semantic_mapping import suggest_semantic_bindings
    definition = FieldProfile(fixed_fields=[ConfiguredField(id='product_name', title='商品名称')], local_categories=[
        LocalCategory(id='lighting', title='灯具', attributes=[ConfiguredField(id='material', title='材质')]),
        LocalCategory(id='tile', title='瓷砖', attributes=[ConfiguredField(id='material', title='材质')]),
    ])
    sheets = [{'sheet': '未知表', 'columns': [{'column': index + 1, 'header': header} for index, header in enumerate(headers)]}]
    suggest_semantic_bindings(sheets, compile_profile(definition), AIClient(runtime[0].root, 'local'), [])
    bindings = {b['field']: b['columns'] for b in sheets[0]['suggested_bindings']}
    assert bindings == expected


def test_combined_export_and_return_keep_all_fields_and_unmatched_values_empty(runtime):
    _, tasks, profiles = runtime
    definition = profile()
    for group in definition.local_categories:
        for field in [*group.attributes, *group.specifications]:
            field.required = False
    publish(profiles, definition)
    created, _ = tasks.create_standardization('local', 'local', '联合导出', '供应商.xlsx', supplier())
    tasks.parse(created['id'], 'local')
    task = tasks.get(created['id'], 'local')
    task = tasks.confirm_mapping(task['id'], 'local', 'local', mapping(task))
    assert task['status'] == 'ready'
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert rows[0]['values_json']['local.tile.attribute.origin'] == ''
    assert rows[0]['values_json']['local.lighting.attribute.brand'] == '示例品牌'
    assert rows[1]['source']['kind']=='context'
    products=[r for r in rows if r['source'].get('kind')!='context']
    assert products[1]['values_json']['local.tile.attribute.origin'] == '广州'
    assert products[1]['values_json']['local.lighting.specification.power'] == ''
    assert rows[0]['source']['bindings']
    with tasks.store.engine.connect() as con:
        from sqlalchemy import select
        from excel_auditor.workflow.repository import records
        stored = con.execute(select(records.c.source).where(records.c.task_id == task['id'])).scalars().all()
    assert all('bindings' not in source and source['mapping_revision'] == task['revision'] for source in stored)
    config = task['payload']['config']
    assert [c['field'] for c in config['outputs'][0]['sheets'][0]['columns']][:7] == [
        'product_name', 'brand', 'sku', 'sale_price', 'local.lighting.attribute.brand', 'local.tile.attribute.origin', 'local.lighting.specification.power']
    run, _ = tasks.create_export(task['id'], 'local', task['revision'])
    tasks.generate(run['id'], 'local')
    book = load_workbook(tasks.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx'))
    assert len(book.sheetnames) == 1
    sheet = book.active
    first_data = config['outputs'][0]['sheets'][0]['data_start_row']
    assert sheet.max_row == first_data + len(rows)-1
    output_columns=run['payload']['config']['outputs'][0]['sheets'][0]['columns']
    source_columns={f['source_column']['sheet']:i for i,c in enumerate(output_columns,1) for f in config['fields'] if f['rule']['name']==c['field'] and f.get('source_column')}
    assert sum(c.value=='备注：含运输，不含安装' for c in sheet[first_data+1])==1
    assert sheet.cell(first_data + 2, source_columns['瓷砖']).value == '8mm'
    assert sheet.cell(first_data + 3, source_columns['封窗']).value == '固定位'
    assert sheet.cell(first_data + 4, source_columns['封窗']).value == '开启扇'
    assert sheet.cell(first_data, 3).value == '0001'
    assert sheet.cell(first_data, 5).value == '品牌：示例品牌、产地：'
    assert sheet.cell(first_data + 2, 5).value == '品牌：、产地：广州'
    assert sheet.cell(first_data, 6).value == '功率：3W'
    book.close()
    path = tasks.maintenance_file(task['id'], 'local')
    transported = read_maintenance(path, tasks.get(task['id'], 'local'), records=rows)
    assert transported[0]['values']['local.lighting.specification.power'] == '3W'
    book = load_workbook(path)
    headers = {row[0]: row[1] for row in book['_workflow_meta'].values if row[0]}
    book['维护表'].cell(headers['header_depth'] + 1, 5, '9.50')
    edited = BytesIO()
    book.save(edited)
    book.close()
    preview = tasks.preview_return(task['id'], 'local', edited.getvalue())
    assert preview['payload']['counts']['changed'] == 1
    updated = tasks.apply_return(task['id'], 'local', 'local', preview['id'], False)
    assert updated['revision'] == 2
    assert tasks.rows(task['id'], 'local', 0, 20)['items'][0]['values_json']['local.lighting.specification.power'] == '3W'


def test_explicit_operator_default_stays_distinct_from_a_source_mapping(runtime):
    _, tasks, profiles = runtime
    definition = FieldProfile(fixed_fields=[ConfiguredField(id='product_name', title='商品名称', aliases=['产品名称'])],
                              local_categories=[LocalCategory(id='platform', title='模拟接口', attributes=[
                                  ConfiguredField(id='origin', title='默认发货地', default='国内')])])
    publish(profiles, definition)
    obj, _ = tasks.create_standardization('local', 'local', '显式默认值', '供应商.xlsx', supplier())
    tasks.parse(obj['id'], 'local')
    task = tasks.get(obj['id'], 'local')
    assert 'local.platform.attribute.origin' in {f['field'] for f in task['payload']['sheets'][0]['unmatched_fields']}
    tasks.confirm_mapping(task['id'], 'local', 'local', mapping(task))
    row = tasks.rows(task['id'], 'local', 0, 20)['items'][0]
    assert row['values_json']['local.platform.attribute.origin'] == '国内'
    assert not any(b['field'] == 'local.platform.attribute.origin' for b in row['source']['bindings'])


def test_legacy_multisheet_one_to_many_category_validation_and_export(runtime):
    store, tasks, _ = runtime
    task = create(runtime, legacy=True)
    sheets = task["payload"]["sheets"]
    assert [(s["sheet"], s["row_count"]) for s in sheets] == [("灯具", 1), ("瓷砖", 1), ("封窗", 2)]
    lamp = sheets[0]["suggested_bindings"]
    assert next(b for b in lamp if b["field"] == "brand")["columns"] == [2]
    assert next(b for b in lamp if b["field"] == "local.lighting.attribute.brand")["columns"] == [2]
    request = mapping(task)
    assert next(b for b in request.sheets[1].bindings if b.field == "local.tile.attribute.origin").columns == [2]
    task = tasks.confirm_mapping(task["id"], "local", "local", request)
    assert task["status"] == "ready", task
    rows = tasks.rows(task["id"], "local", 0, 20)["items"]
    assert len(rows) == 4
    assert rows[0]["values_json"]["brand"] == rows[0]["values_json"]["local.lighting.attribute.brand"] == "示例品牌"
    assert rows[0]["values_json"]["sku"] == "0001"
    assert rows[2]["values_json"]["product_name"] == rows[3]["values_json"]["product_name"] == "外开窗"
    assert rows[3]["values_json"]["sale_price"] == "999"
    assert not rows[1]["issues"], "other-category required fields must not block tiles"
    run, _ = tasks.create_export(task["id"], "local", task["revision"])
    tasks.generate(run["id"], "local")
    finished = tasks.get(task["id"], "local")
    assert finished["exports"][0]["status"] == "completed", finished["exports"]
    path = tasks.export_file(task["id"], run["id"], "local", "标准商品表.xlsx")
    book = load_workbook(path)
    assert book.sheetnames == ["固定字段", "灯具", "瓷砖"]
    assert book["灯具"].cell(4, 3).value == "0001"
    assert book["灯具"].cell(4, 5).value == "品牌：示例品牌"
    assert book["灯具"].cell(4, 6).value == "功率：3W"
    assert book["瓷砖"].cell(4, 5).value == "产地：广州"
    assert book["固定字段"].max_row == 5
    assert "A1:D1" in {str(m) for m in book["灯具"].merged_cells.ranges}
    book.close()
    manifest = json.loads(tasks.export_file(task["id"], run["id"], "local", "生成说明.json").read_text(encoding="utf-8"))
    assert manifest["field_snapshot"]["version"] == 1


def test_all_fields_required_missing_block_export_without_category_selection(runtime):
    _, tasks, _ = runtime
    task = create(runtime)
    analyzed = tasks.analyze_mapping(task["id"], "local", "local", task["revision"])
    assert all(s['mapping_summary']['total'] == 7 for s in analyzed['payload']['sheets'])
    req = mapping(analyzed)
    task = tasks.confirm_mapping(task["id"], "local", "local", req)
    assert task["status"] == "maintenance"
    with pytest.raises(WorkflowError, match="阻断"):
        tasks.create_export(task["id"], "local", task["revision"])


def test_legacy_foreign_category_mapping_rejected(runtime):
    _, tasks, _ = runtime
    task = create(runtime, legacy=True)
    req = mapping(task)
    req.sheets[0].bindings[0].field = "local.tile.attribute.origin"
    with pytest.raises(WorkflowError, match="其他类目"):
        tasks.confirm_mapping(task["id"], "local", "local", req)


def test_model_one_source_multiple_targets_and_fallback(runtime, monkeypatch):
    store, tasks, _ = runtime
    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"mappings": [
        {"column": 1, "field_id": "brand", "confidence": .9, "reason": "品牌"},
        {"column": 1, "field_id": "dynamic_brand", "confidence": .9, "reason": "品牌"},
    ]})}}]}
    ai = AIClient(store.root, "local", httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    ai.save(AISettings(base_url="https://model.example/v1", model="test", api_key=SecretStr("test-key")))
    result = ai.reconcile_field_mappings([{"id": "brand"}, {"id": "dynamic_brand"}], [{"column": 1}], multiple_targets=True)
    assert len(result) == 2
    monkeypatch.setattr(AIClient, "reconcile_field_mappings", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")))
    task = create(runtime)
    assert any("模型分析未完成" in n for s in task["payload"]["sheets"] for n in s["mapping_notes"])


def test_field_validation_and_idempotency(runtime):
    _, tasks, profiles = runtime
    with pytest.raises(ValidationError):
        FieldProfile(fixed_fields=[ConfiguredField(id="invalid", title="", type="enum")])
    publish(profiles)
    one, created = tasks.create_standardization("local", "local", "same", "a.xlsx", supplier(), "same-key")
    # Reuse the exact bytes; XLSX timestamps are not part of business identity.
    content = (runtime[0].directory(one["id"]) / "source.xlsx").read_bytes()
    two, repeated = tasks.create_standardization("local", "local", "same", "a.xlsx", content, "same-key")
    assert created and not repeated and one["id"] == two["id"]


def test_model_suggestions_flow_into_sop_without_changing_values(runtime, monkeypatch):
    book = load_workbook(BytesIO(supplier()))
    book["灯具"].cell(2, 2, "供应厂牌")
    data = BytesIO()
    book.save(data)
    book.close()
    monkeypatch.setattr(AIClient, "settings", lambda self: {"configured": True})
    def suggestions(self, fields, columns, **kwargs):
        assert kwargs["multiple_targets"]
        assert all(set(column) == {'column','header','source_column','sheet_id','sheet_name'} for column in columns), "no row values leave the process"
        column = next(c['column'] for c in columns if c['sheet_name']=='灯具' and c['source_column']==2)
        return [{"column": column, "field_id": field["id"], "confidence": .95, "reason": "厂牌表示品牌"}
                for field in fields if field['sheet_name']=='灯具' and field["id"].split('::',1)[1] in {"brand", "local.lighting.attribute.brand"}]
    monkeypatch.setattr(AIClient, "reconcile_field_mappings", suggestions)
    task = create(runtime, data.getvalue())
    bindings = task["payload"]["sheets"][0]["suggested_bindings"]
    assert len([b for b in bindings if b["method"] == "semantic_model"]) == 2
    task = runtime[1].confirm_mapping(task["id"], "local", "local", mapping(task))
    row = runtime[1].rows(task["id"], "local", 0, 10)["items"][0]
    assert row["values_json"]["sale_price"] == "8.4"
    assert row["values_json"]["brand"] == row["values_json"]["local.lighting.attribute.brand"] == "示例品牌"
    assert any(b["method"] == "semantic_model" for b in row["source"]["bindings"])


def test_legacy_standardization_maintenance_roundtrip_preserves_categories(runtime):
    _, tasks, _ = runtime
    task = create(runtime, legacy=True)
    task = tasks.confirm_mapping(task["id"], "local", "local", mapping(task))
    path = tasks.maintenance_file(task["id"], "local")
    transported = read_maintenance(path, task)
    assert transported[0]["values"]["source_category"] == "lighting"
    book = load_workbook(path)
    price_column = next(index + 2 for index, field in enumerate(task["payload"]["config"]["fields"]) if field["rule"]["name"] == "sale_price")
    book["维护表"].cell(4, price_column, "9.50")
    content = BytesIO()
    book.save(content)
    book.close()
    preview = tasks.preview_return(task["id"], "local", content.getvalue())
    assert preview["payload"]["counts"] == {"changed": 1, "added": 0, "missing": 0}
    updated = tasks.apply_return(task["id"], "local", "local", preview["id"], False)
    assert updated["revision"] == 2
    row = tasks.rows(task["id"], "local", 0, 10)["items"][0]
    assert row["values_json"]["source_category"] == "lighting" and row["values_json"]["sale_price"] == "9.50"


def test_explicit_reanalysis_migrates_legacy_mapping_using_its_own_field_snapshot(runtime):
    _, tasks, profiles = runtime
    task = create(runtime, legacy=True)
    old_version = task['payload']['field_snapshot']['version']
    old_profile = task['payload']['field_snapshot']['profile']
    changed = profile()
    changed.local_categories[0].attributes.append(ConfiguredField(id='newer', title='之后新增字段'))
    publish(profiles, changed)
    analyzed = tasks.analyze_mapping(task['id'], 'local', 'local', task['revision'])
    assert analyzed['revision'] == task['revision'] + 1
    assert not analyzed['payload']['config']['category_field']
    assert analyzed['payload']['field_snapshot']['matching_scope'] == 'all_fields'
    assert analyzed['payload']['field_snapshot']['profile'] == old_profile
    assert analyzed['payload']['field_snapshot']['version'] == old_version
    assert all(s['mapping_summary']['total'] == 7 for s in analyzed['payload']['sheets'])
    assert tasks.rows(task['id'], 'local', 0, 20)['total'] == 0


def test_profile_api_and_permission(tmp_path):
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.tenant_id = "local"
        request.state.user_id = request.headers.get("X-Test-Actor", "local")
        request.state.authenticated = True
        request.state.role = "owner" if request.state.user_id == "local" else "operator"
        return await call_next(request)

    @app.exception_handler(WorkflowError)
    async def errors(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    app.include_router(build_router(tmp_path, inline_executor_for_tests=True))
    with TestClient(app) as client:
        current = client.get("/api/v1/field-profile").json()
        assert client.post("/api/v1/field-profile/preview", json=profile().model_dump()).status_code == 200
        denied = client.put("/api/v1/field-profile", headers={"X-Test-Actor": "viewer"}, json={"base_revision": 0, "profile": profile().model_dump()})
        assert denied.status_code == 403
        saved = client.put("/api/v1/field-profile", json={"base_revision": current["revision"], "profile": profile().model_dump()})
        assert saved.status_code == 200, saved.text
        assert client.post("/api/v1/field-profile/publish", json={"base_revision": saved.json()["revision"]}).status_code == 200
        created = client.post("/api/v1/standardization-tasks", files={"excel_file": ("supplier.xlsx", supplier())})
        assert created.status_code == 202, created.text
        task = client.get(f"/api/v1/workflow-tasks/{created.json()['id']}").json()
        assert task["status"] == "mapping"
        analyzed = client.post(f"/api/v1/workflow-tasks/{task['id']}/mapping/analyze", json={'base_revision': task['revision']})
        assert analyzed.status_code == 200, analyzed.text
        assert client.get("/api/v1/field-profile").json()["payload"]["version"] == 1

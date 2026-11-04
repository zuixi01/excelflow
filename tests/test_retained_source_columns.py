from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook

from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.excel import read_maintenance
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, LocalCategory, ProfileEdit
from excel_auditor.workflow.models import Binding, EditRequest, MappingRequest, WorkflowError
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.tasks import TaskService


@pytest.fixture
def runtime(tmp_path):
    store = WorkflowStore(tmp_path / "workflow")
    profiles = FieldProfileService(store)
    profile = FieldProfile(fixed_fields=[ConfiguredField(id="sku", title="商品编码")], local_categories=[
        LocalCategory(id="test", title="测试", attributes=[ConfiguredField(id="material", title="材质")])])
    draft = profiles.get("local")
    saved = profiles.save("local", ProfileEdit(base_revision=draft["revision"], profile=profile))
    profiles.publish("local", saved["revision"])
    yield TaskService(store), profiles
    store.engine.dispose()


def workbook(sheets):
    book = Workbook()
    book.remove(book.active)
    for name, headers, rows in sheets:
        sheet = book.create_sheet(name)
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        # Text that looks like a formula must remain literal through every transport.
        for row in sheet:
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def create(tasks, content, *, historical=False, layout=None):
    task, _ = tasks.create_standardization("local", "test", "保留列测试", "source.xlsx", content)
    if historical or layout:
        with tasks.store.engine.begin() as con:
            saved = tasks.store.get(con, task["id"], "local")
            if historical:
                saved["payload"]["config"]["preserve_extras"] = False
            if layout:
                saved["payload"]["config"]["source_header_layout"] = layout
            tasks.store.save(con, saved)
    tasks.parse(task["id"], "local")
    task = tasks.get(task["id"], "local")
    assert task["status"] == "mapping", task["payload"].get("error")
    return task


def mapping(task):
    return MappingRequest(base_revision=task["revision"], sheets=[dict(sheet=s["sheet"], header_row=s["header_row"],
        header_depth=s["header_depth"], bindings=s["suggested_bindings"]) for s in task["payload"]["sheets"]])


def extras(task):
    return [f for f in task["payload"]["config"]["fields"] if f.get("source_column")]


def test_literal_duplicate_columns_survive_edit_return_export_and_remapping(runtime):
    tasks, profiles = runtime
    before_profile = profiles.get("local")
    headers = ["商品编码", "材质", "备注（原样）/详情\n第二行", "同名", "同名", None]
    source_values = ["0007", "铜", "  第一行\n第二行  ", "00000001", "=literal", "NA"]
    content = workbook([("甲表", headers, [source_values, ["0008", "铁", "", "  ", "尾行", "0"]]),
                        ("乙表", ["商品编码", "材质", "同名"], [["0009", "铝", "乙表独立值"]])])
    task = create(tasks, content)
    request = mapping(task)
    request.sheets.reverse()  # Caller order cannot reorder retained source columns.
    request.sheets[1].ignored_columns = [4]  # Old clients cannot silently discard extras.
    task = tasks.confirm_mapping(task["id"], "local", "test", request)
    assert task["status"] == "ready"
    retained = extras(task)
    assert [(f["source_column"]["sheet"], f["source_column"]["column"]) for f in retained] == [
        ("甲表", 3), ("甲表", 4), ("甲表", 5), ("甲表", 6), ("乙表", 3)]
    assert retained[0]["source_column"]["header"] == headers[2]
    assert len({f["rule"]["name"] for f in retained}) == 5
    assert not task["payload"]["ignored"]
    fields = task["payload"]["config"]["fields"]
    assert [f["rule"]["name"] for f in fields[:2]] == ["sku", "local.test.attribute.material"]
    rows = tasks.rows(task["id"], "local", 0, 20)["items"]
    row = next(r for r in rows if r["values_json"]["sku"] == "0007")
    ids = [f["rule"]["name"] for f in retained]
    assert [row["values_json"][key] for key in ids] == source_values[2:] + [""]
    blank = next(r for r in rows if r["values_json"]["sku"] == "0008")
    assert blank["values_json"][ids[1]] == "  "
    task = tasks.edit(task["id"], "local", "test", EditRequest(base_revision=task["revision"],
                      changes={row["id"]: {ids[0]: "在线修改\n第二行"}}))
    path = tasks.maintenance_file(task["id"], "local")
    incoming = read_maintenance(path, task)
    assert next(r for r in incoming if r["id"] == row["id"])["values"][ids[0]] == "在线修改\n第二行"
    book = load_workbook(path)
    meta = dict(book["_workflow_meta"].values)
    sheet = book["维护表"]
    assert sheet.cell(2, 4).value == headers[2]  # Original punctuation/slash/newline intact.
    row_number = next(i for i in range(meta["header_depth"] + 1, sheet.max_row + 1) if sheet.cell(i, 1).value == row["id"])
    sheet.cell(row_number, 5, "00000222")
    stream = BytesIO()
    book.save(stream)
    book.close()
    preview = tasks.preview_return(task["id"], "local", stream.getvalue())
    assert preview["payload"]["counts"] == {"changed": 1, "added": 0, "missing": 0}
    task = tasks.apply_return(task["id"], "local", "test", preview["id"], False)
    run, _ = tasks.create_export(task["id"], "local", task["revision"])
    tasks.generate(run["id"], "local")
    book = load_workbook(tasks.export_file(task["id"], run["id"], "local", "标准商品表.xlsx"))
    sheet = book.active
    assert sheet.max_column == 7
    assert sheet.cell(2, 3).value == headers[2]
    exported = next(list(values) for values in sheet.iter_rows(min_row=4, values_only=True) if values[0] == "0007")
    assert exported == ["0007", "材质：铜", "在线修改\n第二行", "00000222", "=literal", "NA", None]
    assert all(cell.data_type != "f" for row_cells in sheet for cell in row_cells)
    book.close()
    assert profiles.get("local") == before_profile
    task = tasks.reopen_mapping(task["id"], "local", "test", task["revision"])
    assert not extras(task)
    request = mapping(task)
    request.sheets[0].bindings = [Binding(field="sku", columns=[1]), Binding(field="local.test.attribute.material", columns=[3])]
    task = tasks.confirm_mapping(task["id"], "local", "test", request)
    assert [(f["source_column"]["sheet"], f["source_column"]["column"]) for f in extras(task)] == [
        ("甲表", 2), ("甲表", 4), ("甲表", 5), ("甲表", 6), ("乙表", 3)]


def test_wide_multi_sheet_import_roundtrips_more_than_200_columns(runtime):
    tasks, _ = runtime
    headers = ["商品编码", "材质", *[f"供应备注{i}" for i in range(198)]]
    content = workbook([(name, headers, [[name, "木", *[f"{name}-{i:04d}" for i in range(198)]]])
                        for name in ["甲", "乙", "丙", "丁"]])
    task = create(tasks, content)
    task = tasks.confirm_mapping(task["id"], "local", "test", mapping(task))
    assert len(extras(task)) == 792
    path = tasks.maintenance_file(task["id"], "local")
    rows = read_maintenance(path, task)
    assert len(rows) == 4 and len(rows[0]["values"]) == 794
    assert rows[3]["values"][extras(task)[-1]["rule"]["name"]] == "丁-0197"
    preview = tasks.preview_return(task["id"], "local", path.read_bytes())
    assert preview["payload"]["counts"] == {"changed": 0, "added": 0, "missing": 0}


@pytest.mark.parametrize("failure", [False, True])
def test_uncertain_or_failed_model_keeps_every_source_column(runtime, monkeypatch, failure):
    tasks, _ = runtime
    monkeypatch.setattr(AIClient, "settings", lambda self: {"configured": True})
    def reconcile(self, fields, columns, **kwargs):
        if failure:
            raise WorkflowError("模拟模型故障")
        return [{"field_id": "sku", "column": 1, "confidence": 0.5, "reason": "不确定"}]
    monkeypatch.setattr(AIClient, "reconcile_field_mappings", reconcile)
    task = create(tasks, workbook([("来源", ["货物标记", "补充内容"], [["0007", "完整内容"]])]))
    assert not task["payload"]["sheets"][0]["suggested_bindings"]
    task = tasks.confirm_mapping(task["id"], "local", "test", mapping(task))
    assert len(extras(task)) == 2
    row = tasks.rows(task["id"], "local", 0, 20)["items"][0]
    assert [row["values_json"][f["rule"]["name"]] for f in extras(task)] == ["0007", "完整内容"]


def test_historical_task_keeps_its_previous_schema(runtime):
    tasks, _ = runtime
    task = create(tasks, workbook([("来源", ["商品编码", "原表备注"], [["0007", "历史内容"]])]), historical=True)
    task = tasks.analyze_mapping(task["id"], "local", "test", task["revision"])
    assert task["payload"]["config"]["preserve_extras"] is False
    task = tasks.confirm_mapping(task["id"], "local", "test", mapping(task))
    assert not extras(task)
    assert len(task["payload"]["ignored"]) == 1


def test_return_rejects_removed_or_renamed_retained_header(runtime):
    tasks, _ = runtime
    task = create(tasks, workbook([("来源", ["商品编码", "补充"], [["0007", "内容"]])]))
    task = tasks.confirm_mapping(task["id"], "local", "test", mapping(task))
    path = tasks.maintenance_file(task["id"], "local")
    book = load_workbook(path)
    book["维护表"].cell(1, 4, "改名")
    stream = BytesIO()
    book.save(stream)
    book.close()
    with pytest.raises(WorkflowError, match="列结构"):
        tasks.preview_return(task["id"], "local", stream.getvalue())


def test_retained_only_note_row_and_constant_binding_do_not_discard_data(runtime):
    tasks, _ = runtime
    task = create(tasks, workbook([("来源", ["商品编码", "材质", "附加记录"], [
        ["0007", "铜", "正常"], [None, None, "备注：仅保留列有内容，也不能丢失"]])]))
    request = mapping(task)
    request.sheets[0].bindings[0] = Binding(field="sku", columns=[1], operation="constant", argument="统一编码")
    task = tasks.confirm_mapping(task["id"], "local", "test", request)
    assert [f["source_column"]["column"] for f in extras(task)] == [1, 3]
    rows = tasks.rows(task["id"], "local", 0, 20)["items"]
    assert len(rows) == 2
    assert rows[0]["values_json"][extras(task)[0]["rule"]["name"]] == "0007"
    assert rows[1]["values_json"][extras(task)[1]["rule"]["name"]] == "备注：仅保留列有内容，也不能丢失"


def test_long_original_header_and_cell_text_are_not_truncated(runtime):
    tasks, _ = runtime
    header, value = "原表完整长表头（Ａ）/" * 30, "完整长文本" * 6000
    task = create(tasks, workbook([("来源", ["商品编码", header], [["0007", value]])]))
    task = tasks.confirm_mapping(task["id"], "local", "test", mapping(task))
    retained = extras(task)[0]
    assert retained["source_column"]["header"] == header
    path = tasks.maintenance_file(task["id"], "local")
    rows = read_maintenance(path, task)
    assert rows[0]["values"][retained["rule"]["name"]] == value
    book = load_workbook(path)
    assert book["维护表"].cell(2, 4).value == header
    book.close()


def test_conflicting_target_bindings_fail_without_changing_task(runtime):
    tasks, _ = runtime
    task = create(tasks, workbook([("来源", ["商品编码", "货号补充"], [["0007", "另一个编码"]])]))
    request = mapping(task)
    request.sheets[0].bindings.append(Binding(field="sku", columns=[2]))
    with pytest.raises(WorkflowError, match="多个来源"):
        tasks.confirm_mapping(task["id"], "local", "test", request)
    current = tasks.get(task["id"], "local")
    assert current["status"] == "mapping" and current["revision"] == task["revision"]


def test_merged_zero_padded_source_and_formatted_return_keep_leading_zeroes(runtime):
    tasks, _ = runtime
    book = load_workbook(BytesIO(workbook([("来源", ["商品编码", "附加编号"], [["A1", 7], ["A2", None]])])))
    book.active['B2'].number_format = '000000'
    book.active.merge_cells('B2:B3')
    stream = BytesIO()
    book.save(stream)
    book.close()
    task = create(tasks, stream.getvalue())
    task = tasks.confirm_mapping(task['id'], 'local', 'test', mapping(task))
    key = extras(task)[0]['rule']['name']
    assert [row['values_json'][key] for row in tasks.rows(task['id'], 'local', 0, 20)['items']] == ['000007', '000007']
    path = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(path)
    book['维护表'].cell(4, 4, 8).number_format = '000000'
    book.save(path)
    book.close()
    assert read_maintenance(path, task, records=tasks.rows(task['id'], 'local', 0, 20)['items'])[0]['values'][key] == '000008'


def test_compact_headers_omit_sequence_and_keep_unmatched_values_on_the_same_row(runtime):
    tasks, _ = runtime
    content = workbook([('封窗', ['序号', '商品编码', '表面工艺', '气密性', '水密性', '抗风压'], [
        [1, 'QX3', '喷涂', '8级', '6级', '9级'], [2, 'QX7', '喷涂', '7级', '5级', '8级'], [3, None, None, None, None, None]])])
    task = create(tasks, content, layout="compact")
    task = tasks.confirm_mapping(task['id'], 'local', 'test', mapping(task))
    assert [f['source_column']['header'] for f in extras(task)] == ['表面工艺', '气密性', '水密性', '抗风压']
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert len(rows) == 2
    keys = [f['rule']['name'] for f in extras(task)]
    assert [(r['values_json']['sku'], *[r['values_json'][k] for k in keys]) for r in rows] == [
        ('QX3', '喷涂', '8级', '6级', '9级'), ('QX7', '喷涂', '7级', '5级', '8级')]
    path = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(path)
    sheet = book['维护表']
    assert [sheet.cell(1, i).value for i in range(4, 8)] == ['表面工艺', '气密性', '水密性', '抗风压']
    assert not any('封窗 ·' in str(cell.value) or cell.value == '序号' for row in sheet for cell in row)
    book.close()
    assert len(read_maintenance(path, task)) == 2


def test_duplicate_compact_headers_stay_separate_columns(runtime):
    tasks, _ = runtime
    task = create(tasks, workbook([('来源', ['商品编码', '说明', '说明'], [['0007', '左值', '右值']])]), layout="compact")
    task = tasks.confirm_mapping(task['id'], 'local', 'test', mapping(task))
    path = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(path)
    sheet = book['维护表']
    assert sheet.cell(1, 4).value == sheet.cell(1, 5).value == '说明'
    assert not any(region.min_col == 4 and region.max_col == 5 for region in sheet.merged_cells.ranges)
    book.close()
    row = read_maintenance(path, task)[0]
    assert [row['values'][f['rule']['name']] for f in extras(task)] == ['左值', '右值']


def test_grouped_headers_merge_only_categories_and_preserve_rows_through_return_and_export(runtime):
    tasks, _ = runtime
    task = create(tasks, workbook([
        ('封窗', ['序号', '商品编码', '表面工艺', '气密性', '说明', '说明'], [[1, 'QX3', '喷涂', '8级', '左', '右']]),
        ('瓷砖', ['序号', '商品编码', '表面工艺', '防滑等级'], [[1, 'C001', '哑光', 'R10']]),
    ]))
    assert task['payload']['config']['source_header_layout'] == 'grouped'
    task = tasks.confirm_mapping(task['id'], 'local', 'test', mapping(task))
    fields = extras(task)
    assert [f['source_column']['header'] for f in fields] == ['表面工艺', '气密性', '说明', '说明', '表面工艺', '防滑等级']
    before_rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert len(before_rows) == 2
    path = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(path)
    sheet = book['维护表']
    assert sheet['D1'].value == 'source · 封窗' and sheet['H1'].value == 'source · 瓷砖'
    assert {'D1:G1', 'H1:I1'} <= {str(region) for region in sheet.merged_cells.ranges}
    assert sheet['F2'].value == sheet['G2'].value == '说明'
    assert not any(region.min_col <= 6 < region.max_col and region.min_row > 1 for region in sheet.merged_cells.ranges)
    assert not any(' · ' in str(cell.value) or cell.value == '序号' for row in sheet.iter_rows(min_row=2) for cell in row)
    depth = dict(book['_workflow_meta'].values)['header_depth']
    assert all(region.max_row <= depth for region in sheet.merged_cells.ranges)
    assert sheet.cell(depth + 1, 4).value == '喷涂'
    assert sheet.cell(depth + 2, 8).value == '哑光'
    sheet.cell(depth + 1, 4, '氟碳喷涂')
    stream = BytesIO()
    book.save(stream)
    book.close()
    preview = tasks.preview_return(task['id'], 'local', stream.getvalue())
    assert preview['payload']['counts'] == {'changed': 1, 'added': 0, 'missing': 0}
    task = tasks.apply_return(task['id'], 'local', 'test', preview['id'], False)
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert rows[0]['id'] == before_rows[0]['id']
    assert rows[0]['values_json'][fields[0]['rule']['name']] == '氟碳喷涂'
    assert rows[1]['values_json'] == before_rows[1]['values_json']
    run, _ = tasks.create_export(task['id'], 'local', task['revision'])
    tasks.generate(run['id'], 'local')
    book = load_workbook(tasks.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx'))
    sheet = book.active
    assert {'C1:F1', 'G1:H1'} <= {str(region) for region in sheet.merged_cells.ranges}
    assert sheet['C1'].value == 'source · 封窗' and sheet['G1'].value == 'source · 瓷砖'
    assert sheet['E2'].value == sheet['F2'].value == '说明'
    assert sheet.cell(4, 3).value == '氟碳喷涂' and sheet.cell(5, 7).value == '哑光'
    assert not any('封窗 ·' in str(cell.value) or '瓷砖 ·' in str(cell.value) for row in sheet for cell in row)
    book.close()


def merged_source_task(tasks):
    content = workbook([('封窗', ['商品编码', '材质', '类别', '价格', '相同但未合并'], [
        ['QX7（QD7115）', '铝材', '固定位', '999', '同值'],
        [None, None, '开启扇', '1038', '同值'],
        ['QX7（QD7115）', '铝材', '独立商品', '1200', '同值'],
    ])])
    book = load_workbook(BytesIO(content))
    book.active.merge_cells('A2:A3')
    book.active.merge_cells('B2:B3')
    data = BytesIO()
    book.save(data)
    book.close()
    task = create(tasks, data.getvalue())
    assert task['payload']['config']['preserve_source_merges'] is True
    return tasks.confirm_mapping(task['id'], 'local', 'test', mapping(task))


def test_source_merges_survive_shared_edit_return_and_export(runtime):
    tasks, _ = runtime
    task = merged_source_task(tasks)
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert rows[0]['source']['merged_fields']['sku'] == rows[1]['source']['merged_fields']['sku']
    assert 'merged_fields' not in rows[2]['source']
    path = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(path)
    sheet = book['维护表']
    assert {'B4:B5', 'C4:C5'} <= {str(r) for r in sheet.merged_cells.ranges}
    assert sheet['B4'].value == 'QX7（QD7115）' and sheet['B5'].value is None
    assert [sheet.cell(i, 5).value for i in (4, 5)] == ['999', '1038']
    assert not any(r.max_row > 3 and r.min_col >= 4 for r in sheet.merged_cells.ranges)
    book.close()
    assert read_maintenance(path, task, records=rows) == [{'id': r['id'], 'values': r['values_json']} for r in rows]
    # Editing the second member, as on a filtered page, still updates the shared cell.
    task = tasks.edit(task['id'], 'local', 'test', EditRequest(base_revision=task['revision'], changes={rows[1]['id']: {'sku': '新版名称'}}))
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert [row['values_json']['sku'] for row in rows] == ['新版名称', '新版名称', 'QX7（QD7115）']
    path = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(path)
    book['维护表']['B4'] = '回传名称'
    book['维护表']['E5'] = '1080'
    data = BytesIO()
    book.save(data)
    book.close()
    preview = tasks.preview_return(task['id'], 'local', data.getvalue())
    assert preview['payload']['counts'] == {'changed': 2, 'added': 0, 'missing': 0}
    task = tasks.apply_return(task['id'], 'local', 'test', preview['id'], False)
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert [row['values_json']['sku'] for row in rows] == ['回传名称', '回传名称', 'QX7（QD7115）']
    price = next(f['rule']['name'] for f in extras(task) if f['source_column']['header'] == '价格')
    assert [row['values_json'][price] for row in rows] == ['999', '1080', '1200']
    run, _ = tasks.create_export(task['id'], 'local', task['revision'])
    tasks.generate(run['id'], 'local')
    book = load_workbook(tasks.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx'))
    sheet = book.active
    assert 'A4:A5' in {str(r) for r in sheet.merged_cells.ranges}
    assert 'B4:B5' not in {str(r) for r in sheet.merged_cells.ranges}
    assert sheet['A4'].value == '回传名称' and sheet['A6'].value == 'QX7（QD7115）'
    assert [sheet.cell(i, 4).value for i in (4, 5)] == ['999', '1080']
    book.close()


@pytest.mark.parametrize('operation', ['unmerge', 'merge_unrelated', 'merge_ids'])
def test_return_rejects_changed_data_merge_structure(runtime, operation):
    tasks, _ = runtime
    task = merged_source_task(tasks)
    path = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(path)
    sheet = book['维护表']
    if operation == 'unmerge':
        sheet.unmerge_cells('B4:B5')
    elif operation == 'merge_unrelated':
        sheet.merge_cells('E4:E5')
    else:
        sheet.merge_cells('A4:A5')
    data = BytesIO()
    book.save(data)
    book.close()
    with pytest.raises(WorkflowError, match='合并关系'):
        tasks.preview_return(task['id'], 'local', data.getvalue())
    assert tasks.get(task['id'], 'local')['revision'] == task['revision']


def test_shared_edits_reject_conflicts_and_deletion_preserves_remaining_values(runtime):
    tasks, _ = runtime
    task = merged_source_task(tasks)
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    with pytest.raises(WorkflowError, match='不同修改'):
        tasks.edit(task['id'], 'local', 'test', EditRequest(base_revision=task['revision'], changes={
            rows[0]['id']: {'sku': 'A'}, rows[1]['id']: {'sku': 'B'}}))
    assert tasks.rows(task['id'], 'local', 0, 20)['items'] == rows
    task = tasks.edit(task['id'], 'local', 'test', EditRequest(base_revision=task['revision'], deletions=[rows[0]['id']]))
    remaining = tasks.rows(task['id'], 'local', 0, 20)['items']
    path = tasks.maintenance_file(task['id'], 'local')
    assert read_maintenance(path, task, records=remaining) == [{'id': r['id'], 'values': r['values_json']} for r in remaining]
    book = load_workbook(path)
    assert not any(r.max_row > 3 for r in book['维护表'].merged_cells.ranges)
    book.close()


def test_unmapped_platform_cells_share_source_blocks_through_edit_return_export(runtime):
    tasks, profiles = runtime
    draft = profiles.get('local')
    profile = FieldProfile.model_validate(draft['payload']['draft'])
    profile.fixed_fields.extend([
        ConfiguredField(id='photo', title='商品图片'),
        ConfiguredField(id='stock', title='库存', type='integer', default='0'),
    ])
    saved = profiles.save('local', ProfileEdit(base_revision=draft['revision'], profile=profile))
    profiles.publish('local', saved['revision'])
    task = merged_source_task(tasks)
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    from excel_auditor.workflow.source_merges import vertical_spans
    assert list(vertical_spans(rows, 'photo')) == [(0, 1)]
    assert list(vertical_spans(rows, 'stock')) == [(0, 1)]
    # A shared edit from the second row updates the first as well.
    task = tasks.edit(task['id'], 'local', 'test', EditRequest(base_revision=task['revision'],
        changes={rows[1]['id']: {'photo': '商品图片.png'}}))
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert [r['values_json']['photo'] for r in rows] == ['商品图片.png', '商品图片.png', '']
    path = tasks.maintenance_file(task['id'], 'local')
    book = load_workbook(path)
    assert {'C4:C5', 'D4:D5'} <= {str(r) for r in book['维护表'].merged_cells.ranges}
    book['维护表']['D4'] = 12
    data = BytesIO()
    book.save(data)
    book.close()
    preview = tasks.preview_return(task['id'], 'local', data.getvalue())
    assert preview['payload']['counts'] == {'changed': 2, 'added': 0, 'missing': 0}
    task = tasks.apply_return(task['id'], 'local', 'test', preview['id'], False)
    rows = tasks.rows(task['id'], 'local', 0, 20)['items']
    assert [r['values_json']['stock'] for r in rows] == ['12', '12', '0']
    run, _ = tasks.create_export(task['id'], 'local', task['revision'])
    tasks.generate(run['id'], 'local')
    book = load_workbook(tasks.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx'))
    assert {'B4:B5', 'C4:C5'} <= {str(r) for r in book.active.merged_cells.ranges}
    assert book.active['C4'].value == 12
    book.close()


def test_shared_platform_blocks_follow_variable_nested_source_geometry():
    from types import SimpleNamespace as NS
    from excel_auditor.workflow.models import SheetMapping
    from excel_auditor.workflow.source_merges import attach_source_merges, vertical_spans
    # Source column positions and block sizes vary; a broad category merge
    # must not swallow the finer product boundaries in another column.
    rows = [{'id': str(i), 'source': {'sheet': '任意表', 'row': i},
             'values_json': {'name': '重复名称', 'price': '99', 'blank': '', 'constant': '默认'}}
            for i in range(2, 11)]
    fields = [NS(rule=NS(name=f), source_column=None) for f in ['name', 'price', 'blank', 'constant']]
    config = NS(preserve_source_merges=True, fields=fields)
    plan = SheetMapping(sheet='任意表', header_row=1, bindings=[
        Binding(field='name', columns=[4]), Binding(field='price', columns=[1]),
        Binding(field='constant', operation='constant', argument='默认')])
    sources = [{'sheet': '任意表', 'merged_ranges': [
        [1, 1, 1, 6], [2, 8, 6, 6], [2, 4, 4, 4], [5, 6, 4, 4], [7, 8, 4, 4],
    ]}]
    attach_source_merges(rows, sources, [plan], config)
    for field in ['name', 'blank', 'constant']:
        assert list(vertical_spans(rows, field)) == [(0, 2), (3, 4), (5, 6)]
    assert not list(vertical_spans(rows, 'price'))  # Equal unmerged prices stay separate.
    assert 'merged_fields' not in rows[-1]['source']  # Equal unmerged products stay separate.
    rows[1]['values_json']['blank'] = '历史独立编辑'
    assert list(vertical_spans(rows, 'blank')) == [(3, 4), (5, 6)]
    # A partially overlapping merge splits the inferred platform block, while
    # explicitly mapped source cells retain their original span.
    sources[0]['merged_ranges'].append([3, 5, 3, 3])
    rows = [dict(row, source={'sheet': '任意表', 'row': i}) for i, row in enumerate(rows, 2)]
    for row in rows:
        row['values_json']['blank'] = ''
    attach_source_merges(rows, sources, [plan], config)
    assert list(vertical_spans(rows, 'blank')) == [(1, 2), (5, 6)]
    assert list(vertical_spans(rows, 'name')) == [(0, 2), (3, 4), (5, 6)]

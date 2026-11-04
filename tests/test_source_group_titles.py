from io import BytesIO

from openpyxl import Workbook, load_workbook

from excel_auditor.workflow.models import MappingRequest, starter_config
from excel_auditor.workflow.source_files import discover_sources
from excel_auditor.workflow.excel import read_maintenance
from test_retained_source_columns import runtime


def workbook(titles=('Sheet1',)):
    book=Workbook();book.remove(book.active)
    for title in titles:
        sheet=book.create_sheet(title)
        sheet.append(['商品编码','规格尺寸卖点','备注1','备注2'])
        sheet.append(['A8','一级水效','含税','安装'])
    content=BytesIO();book.save(content);book.close()
    return content.getvalue()


def test_filename_group_in_maintenance_and_final_and_roundtrip(runtime):
    tasks,_=runtime
    task,_=tasks.create_standardization('local','test','来源标题测试','洁具-安华.xlsx',workbook())
    tasks.parse(task['id'],'local');task=tasks.get(task['id'],'local')
    source=task['payload']['sheets'][0]
    assert source['sheet']=='Sheet1'  # Display-name change must not rename source identity.
    task=tasks.confirm_mapping(task['id'],'local','test',MappingRequest(base_revision=task['revision'],sheets=[{
        'sheet':'Sheet1','header_row':1,'header_depth':1,'include':True,'bindings':[{'field':'sku','columns':[1]}]}]))
    path=tasks.maintenance_file(task['id'],'local')
    book=load_workbook(path);sheet=book.active
    assert '洁具-安华' in [cell.value for cell in sheet[1]]
    assert 'Sheet1' not in [cell.value for cell in sheet[1]]
    book.close()
    _,rows=tasks.snapshot(task['id'],'local',task['revision'],include_rows=True)
    incoming=read_maintenance(path,task,records=rows)
    assert incoming[0]['id']==rows[0]['id']
    assert incoming[0]['values']==rows[0]['values_json']
    export,_=tasks.create_export(task['id'],'local',task['revision']);tasks.generate(export['id'],'local')
    book=load_workbook(tasks.export_file(task['id'],export['id'],'local','标准商品表.xlsx'))
    assert '洁具-安华' in [cell.value for cell in book.active[1]]
    book.close()


def test_multiple_sheets_and_duplicate_filenames_remain_distinguishable(tmp_path):
    (tmp_path/'one.xlsx').write_bytes(workbook(('坐便器','花洒')))
    (tmp_path/'two.xlsx').write_bytes(workbook())
    sources=discover_sources(tmp_path,[{'filename':'洁具.xlsx','storage_name':'one.xlsx'},
        {'filename':'洁具.xlsx','storage_name':'two.xlsx'}],starter_config())
    assert [s['source_group_title'] for s in sources]==['1. 洁具 · 坐便器','1. 洁具 · 花洒','2. 洁具']
    assert len({s['sheet'] for s in sources})==3

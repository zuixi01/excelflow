"""Isolated actual-list acceptance harness. Never updates production names."""
from pathlib import Path
from copy import deepcopy
from io import BytesIO
import argparse
import hashlib
import json
import os
import shutil
import sqlite3

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/name-confirmation-return-20261102'
TARGET='task_91d9a3c4444f4eae96f0ca20ff6c981f'
LIST=ROOT/'outputs/power-name-confirmation-20261102/109条商品名称待确认.xlsx'


def original():
    with sqlite3.connect((ROOT/'var/workflow/workflow.sqlite3').as_uri()+'?mode=ro',uri=True) as con:
        task=con.execute('select payload,revision from workflow_objects where id=?',(TARGET,)).fetchone()
        rows=con.execute('select * from workflow_records where task_id=? order by ordinal',(TARGET,)).fetchall()
    return hashlib.sha256(json.dumps([task,rows],ensure_ascii=False).encode()).hexdigest()


def prepare():
    from sqlalchemy import insert, update
    from openpyxl import load_workbook
    from excel_auditor.workflow.repository import WorkflowStore,records,objects
    from excel_auditor.workflow.tasks import TaskService
    OUT.mkdir(parents=True,exist_ok=True)
    source=WorkflowStore(ROOT/'var/workflow'); service=TaskService(source)
    task=service.get(TARGET,'local'); task,rows=service.snapshot(TARGET,'local',task['revision'],include_rows=True)
    store=WorkflowStore(OUT/'.runtime/workflow')
    with store.engine.begin() as con:
        store.create(con,'local','task',deepcopy(task['payload']),task['status'],object_id=TARGET)
        con.execute(update(objects).where(objects.c.id==TARGET).values(revision=task['revision']))
        con.execute(insert(records),deepcopy(rows))
    shutil.copytree(source.directory(TARGET)/'images',store.directory(TARGET)/'images',dirs_exist_ok=True)
    book=load_workbook(LIST);sheet=book.active;expected={}
    for i in range(7,116):
        name=f'隔离验收名称{i-6:03d}'
        sheet.cell(i,12,name);expected[sheet.cell(i,14).value]=name
    # Reversed data order proves matching does not depend on the Excel row order.
    values=list(sheet.iter_rows(min_row=7,max_row=115,values_only=True))[::-1]
    for i,values_row in enumerate(values,7):
        for j,value in enumerate(values_row,1):sheet.cell(i,j).value=value
    book.save(OUT/'仅验收_109条已填写.xlsx')
    sheet.cell(8,14,sheet.cell(7,14).value);book.save(OUT/'仅验收_重复ID.xlsx');book.close()
    (OUT/'baseline.json').write_text(json.dumps({'sha256':original(),'task':task,'expected':expected},ensure_ascii=False),'utf-8')
    source.engine.dispose();store.engine.dispose()
    print('Prepared isolated 501-row task and reversed 109-row confirmation list.',flush=True)


def check():
    from openpyxl import load_workbook
    from excel_auditor.workflow.repository import WorkflowStore
    from excel_auditor.workflow.tasks import TaskService
    baseline=json.loads((OUT/'baseline.json').read_text('utf-8'))
    store=WorkflowStore(OUT/'.runtime/workflow');service=TaskService(store)
    task=service.get(TARGET,'local'); _,rows=service.snapshot(TARGET,'local',task['revision'],include_rows=True)
    assert task['payload']['error_count']==0 and task['payload']['record_count']==501
    assert task['payload']['config']==baseline['task']['payload']['config']
    assert task['payload']['field_snapshot']==baseline['task']['payload']['field_snapshot']
    source=WorkflowStore(ROOT/'var/workflow'); ss=TaskService(source)
    source_task=ss.get(TARGET,'local');_,prior=ss.snapshot(TARGET,'local',source_task['revision'],include_rows=True)
    for old,row in zip(prior,rows):
        values=deepcopy(old['values_json'])
        if row['id'] in baseline['expected']:values['product_name']=baseline['expected'][row['id']]
        assert row['id']==old['id'] and row['values_json']==values
    run=task['exports'][0]; assert run['status']=='completed',run
    path=service.export_file(TARGET,run['id'],'local','标准商品表.xlsx')
    book=load_workbook(path,read_only=True);sheet=book.active
    content=list(sheet.iter_rows(min_row=4,values_only=True));assert len(content)==501
    present={str(cell) for row in content for cell in row if cell is not None}
    assert set(baseline['expected'].values())<=present
    power_id=next(f['rule']['name'] for f in task['payload']['config']['fields'] if f['rule']['title'].endswith('/功率'))
    power_values={r['values_json'][power_id] for r in prior if r['values_json'].get(power_id)}
    assert power_values
    for power in power_values:
        assert any(f'功率：{power}' in value for value in present),repr(power)
    book.close()
    assert original()==baseline['sha256']
    result={'passed':True,'actual_confirmation_rows':109,'records_preserved':501,'only_product_names_changed':True,
            'config_and_layout_unchanged':True,'export_completed':True,'production_unchanged':True}
    (OUT/'验收结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    source.engine.dispose();store.engine.dispose()


def prepare_partial():
    from sqlalchemy import insert,update
    from openpyxl import load_workbook
    from excel_auditor.workflow.repository import WorkflowStore,records,objects
    from excel_auditor.workflow.tasks import TaskService
    source=WorkflowStore(ROOT/'var/workflow');service=TaskService(source)
    task=service.get(TARGET,'local');task,rows=service.snapshot(TARGET,'local',task['revision'],include_rows=True)
    store=WorkflowStore(OUT/'.runtime/workflow')
    payload=deepcopy(task['payload']);payload['name']='仅验收：确认清单填写3条'
    with store.engine.begin() as con:
        obj=store.create(con,'local','task',payload,task['status'])
        con.execute(update(objects).where(objects.c.id==obj['id']).values(revision=task['revision']))
        copied=deepcopy(rows)
        for row in copied:row['task_id']=obj['id']
        con.execute(insert(records),copied)
    shutil.copytree(source.directory(TARGET)/'images',store.directory(obj['id'])/'images')
    book=load_workbook(LIST);sheet=book.active;expected={}
    for i,name in zip((7,37,115),('测试商品A（仅验收）','测试商品B（仅验收）','测试商品C（仅验收）')):
        sheet.cell(i,12,name);expected[sheet.cell(i,14).value]=name
    book.save(OUT/'仅验收_填写3条.xlsx');book.close()
    (OUT/'partial-baseline.json').write_text(json.dumps({'id':obj['id'],'sha256':original(),'expected':expected},ensure_ascii=False),'utf-8')
    source.engine.dispose();store.engine.dispose()
    print('Prepared 3 confirmations in an isolated copy.',flush=True)


def check_partial():
    from excel_auditor.workflow.repository import WorkflowStore
    from excel_auditor.workflow.tasks import TaskService
    baseline=json.loads((OUT/'partial-baseline.json').read_text('utf-8'))
    store=WorkflowStore(OUT/'.runtime/workflow');service=TaskService(store)
    task=service.get(baseline['id'],'local');_,rows=service.snapshot(task['id'],'local',task['revision'],include_rows=True)
    source=WorkflowStore(ROOT/'var/workflow');ss=TaskService(source)
    original_task=ss.get(TARGET,'local');_,prior=ss.snapshot(TARGET,'local',original_task['revision'],include_rows=True)
    assert task['payload']['error_count']==106 and len(rows)==501
    for old,row in zip(prior,rows):
        values=deepcopy(old['values_json'])
        if row['id'] in baseline['expected']:values['product_name']=baseline['expected'][row['id']]
        assert old['id']==row['id'] and values==row['values_json']
    assert task['payload']['config']==original_task['payload']['config']
    assert original()==baseline['sha256']
    result={'passed':True,'updated_names':3,'blank_names_preserved':106,'records_preserved':501,'layout_unchanged':True,'production_unchanged':True}
    (OUT/'3条回传验收.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(result),flush=True)
    store.engine.dispose();source.engine.dispose()


def prepare_remaining():
    from openpyxl import load_workbook
    from excel_auditor.workflow.repository import WorkflowStore
    from excel_auditor.workflow.tasks import TaskService
    baseline=json.loads((OUT/'partial-baseline.json').read_text('utf-8'))
    store=WorkflowStore(OUT/'.runtime/workflow');service=TaskService(store)
    task=service.get(baseline['id'],'local');_,rows=service.snapshot(task['id'],'local',task['revision'],include_rows=True)
    assert task['payload']['error_count']==106
    by_id={r['id']:r for r in rows}
    book=load_workbook(LIST);sheet=book.active;expected=dict(baseline['expected']);added=0
    for i in range(7,116):
        row_id=sheet.cell(i,14).value
        if not by_id[row_id]['values_json'].get('product_name'):
            name=f'测试商品{i-6:03d}（仅验收）'
            sheet.cell(i,12,name);expected[row_id]=name;added+=1
    assert added==106 and len(expected)==109
    book.save(OUT/'仅验收_剩余106条.xlsx');book.close()
    (OUT/'complete-baseline.json').write_text(json.dumps({'id':task['id'],'task':task,'expected':expected,'sha256':original()},ensure_ascii=False),'utf-8')
    store.engine.dispose();print('Prepared remaining 106 names in the same test task.',flush=True)


def deliver_complete():
    import zipfile
    import xml.etree.ElementTree as ET
    from openpyxl import load_workbook
    from excel_auditor.workflow.repository import WorkflowStore
    from excel_auditor.workflow.tasks import TaskService
    baseline=json.loads((OUT/'complete-baseline.json').read_text('utf-8'))
    store=WorkflowStore(OUT/'.runtime/workflow');service=TaskService(store)
    task=service.get(baseline['id'],'local');_,rows=service.snapshot(task['id'],'local',task['revision'],include_rows=True)
    assert task['payload']['error_count']==0 and len(rows)==501
    assert task['payload']['config']==baseline['task']['payload']['config']
    assert task['payload']['field_snapshot']==baseline['task']['payload']['field_snapshot']
    source=WorkflowStore(ROOT/'var/workflow');ss=TaskService(source)
    production=ss.get(TARGET,'local');_,prior=ss.snapshot(TARGET,'local',production['revision'],include_rows=True)
    for old,row in zip(prior,rows):
        values=deepcopy(old['values_json'])
        if row['id'] in baseline['expected']:values['product_name']=baseline['expected'][row['id']]
        assert row['id']==old['id'] and row['values_json']==values
    run=task['exports'][0];assert run['status']=='completed'
    final=service.export_file(task['id'],run['id'],'local','标准商品表.xlsx')
    previous=service.get(TARGET,'local')['exports'][0]
    reference=service.export_file(TARGET,previous['id'],'local','标准商品表.xlsx')
    ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    def layout(path):
        with zipfile.ZipFile(path) as z:
            tree=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            return [ET.tostring(r) for r in tree.findall('x:sheetData/x:row',ns) if int(r.attrib['r'])<=3],ET.tostring(tree.find('x:cols',ns)),[ET.tostring(m) for m in tree.findall('x:mergeCells/x:mergeCell',ns)],z.read('xl/styles.xml')
    assert layout(final)==layout(reference),'Final layered headers/styles/merges/widths changed'
    with zipfile.ZipFile(final) as z:
        image_hashes={hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('xl/media/')}
    with zipfile.ZipFile(reference) as z:
        assert image_hashes=={hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('xl/media/')}
    book=load_workbook(final,read_only=True);sheet=book.active
    values=list(sheet.iter_rows(min_row=4,values_only=True));assert len(values)==501
    cells={str(v) for row in values for v in row if v is not None}
    assert set(baseline['expected'].values())<=cells
    assert any('功率：' in v for v in cells)
    assert not any(c.data_type=='e' for row in sheet.iter_rows() for c in row)
    metadata={'sheet':sheet.title,'columns':sheet.max_column,'rows':sheet.max_row,
              'headers':list(sheet.iter_rows(max_row=3,values_only=True))}
    book.close()
    assert original()==baseline['sha256']
    destination=OUT/'仅供测试_最终商品表_109条测试名称.xlsx'
    shutil.copy2(final,destination)
    report={'passed':True,'new_names':106,'previous_test_names_retained':3,'records':501,'errors':0,
            'header_style_merges_widths_unchanged':True,'distinct_images_retained':len(image_hashes),
            'production_unchanged':True,'file':str(destination),'metadata':metadata}
    (OUT/'完整测试最终表验收.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='metadata'},ensure_ascii=False),flush=True)
    store.engine.dispose();source.engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['prepare','api','check','prepare-partial','check-partial','prepare-remaining','deliver-complete']);args=parser.parse_args()
    if args.operation=='prepare':prepare()
    elif args.operation=='check':check()
    elif args.operation=='prepare-partial':prepare_partial()
    elif args.operation=='check-partial':check_partial()
    elif args.operation=='prepare-remaining':prepare_remaining()
    elif args.operation=='deliver-complete':deliver_complete()
    else:
        for key in ('DATABASE_URL','REDIS_URL','S3_BUCKET','EXCEL_AUDITOR_AUTH_DATABASE_URL','EXCEL_AUDITOR_API_TOKEN','EXCEL_AUDITOR_API_TOKENS_JSON','EXCEL_AUDITOR_CONNECTIONS'):
            os.environ.pop(key,None)
        os.environ.update(EXCEL_AUDITOR_DATA=str(OUT/'.runtime'),EXCEL_AUDITOR_REQUIRE_AUTH='1',
                          EXCEL_AUDITOR_ADMIN_USERNAME='audit',EXCEL_AUDITOR_ADMIN_PASSWORD='audit-test-password')
        import uvicorn
        from excel_auditor.api import app
        uvicorn.run(app,host='127.0.0.1',port=8005,log_level='warning')

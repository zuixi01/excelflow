from copy import deepcopy
from io import BytesIO

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import update

from excel_auditor.workflow.field_profiles import FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.models import EditRequest, MappingRequest, WorkflowError
from excel_auditor.workflow.name_confirmation import preview, apply
from excel_auditor.workflow.repository import WorkflowStore, records, objects
from excel_auditor.workflow.routes import build_router
from excel_auditor.workflow.tasks import TaskService


def workbook(rows, headers=None):
    book=Workbook(); sheet=book.active
    sheet.append(['商品名称确认清单'])
    sheet.append(headers or ['记录ID','确认商品名称','确认意见'])
    for row in rows: sheet.append(row)
    stream=BytesIO(); book.save(stream); book.close()
    return stream.getvalue()


@pytest.fixture
def case(tmp_path):
    store=WorkflowStore(tmp_path/'workflow'); tasks=TaskService(store); profiles=FieldProfileService(store)
    profile=FieldProfile.model_validate({'name':'确认名称测试','fixed_fields':[
        {'id':'product_name','title':'商品名称','required':True},{'id':'brand','title':'品牌'},{'id':'sku','title':'型号'}],
        'local_categories':[{'id':'lamp','title':'灯具','specifications':[{'id':'power','title':'功率'}]}]})
    draft=profiles.get('local'); draft=profiles.save('local',ProfileEdit(base_revision=draft['revision'],profile=profile)); profiles.publish('local',draft['revision'])
    book=Workbook();sheet=book.active;sheet.append(['商品名称','品牌','型号','功率'])
    for i in range(3): sheet.append([None,'原品牌',f'M{i}','3W'])
    stream=BytesIO();book.save(stream);book.close()
    task,_=tasks.create_standardization('local','local','名称确认测试','原表.xlsx',stream.getvalue())
    tasks.parse(task['id'],'local');task=tasks.get(task['id'],'local')
    sheet=task['payload']['sheets'][0]
    task=tasks.confirm_mapping(task['id'],'local','local',MappingRequest(base_revision=task['revision'],sheets=[{
        'sheet':sheet['sheet'],'include':True,'header_row':1,'header_depth':1,'bindings':[
            {'field':field,'columns':[index],'operation':'copy'} for index,field in enumerate(['product_name','brand','sku','local.lamp.specification.power'],1)]}]))
    rows=tasks.snapshot(task['id'],'local',task['revision'],include_rows=True)[1]
    yield tasks,task,rows,tmp_path
    store.engine.dispose()


def test_sorted_partial_confirmation_changes_only_named_ids_and_preserves_layout(case):
    tasks,task,rows,_=case
    before=deepcopy(rows)
    # Reverse order, omit one row, leave one blank; reference values are ignored.
    p=preview(tasks,task['id'],'local',workbook([[rows[2]['id'],'确认灯具','改品牌也不生效'],[rows[0]['id'],'','']]))
    assert p['payload']['counts']=={'changed':1,'blank':1,'unchanged':0,'overwritten':0}
    assert tasks.get(task['id'],'local')['revision']==task['revision']
    result=apply(tasks,task['id'],'local','operator',p['id'])
    actual=tasks.snapshot(task['id'],'local',result['revision'],include_rows=True)[1]
    before[2]['values_json']['product_name']='确认灯具'
    assert [r['values_json'] for r in actual]==[r['values_json'] for r in before]
    assert result['payload']['config']==task['payload']['config']
    assert result['payload']['field_snapshot']==task['payload']['field_snapshot']
    assert result['payload']['error_count']==2
    assert result['history'][0]['detail']['action']=='confirm_product_names'
    with pytest.raises(WorkflowError,match='新版本'):
        apply(tasks,task['id'],'local','operator',p['id'])


@pytest.mark.parametrize('bad', ['unknown','duplicate','missing','formula_name','formula_id','error','wrong_headers','context'])
def test_invalid_confirmation_rejected_without_changes(case,bad):
    tasks,task,rows,_=case
    supplied=[[rows[0]['id'],'名称']]; headers=None
    if bad=='unknown': supplied[0][0]='row_other_task'
    if bad=='duplicate': supplied*=2
    if bad=='missing': supplied[0][0]=''
    if bad=='formula_name': supplied[0][1]='=1+2'
    if bad=='formula_id': supplied[0][0]='=A1'
    if bad=='error': supplied[0][1]='#REF!'
    if bad=='wrong_headers': headers=['记录ID','商品名称']
    if bad=='context':
        with tasks.store.engine.begin() as con:
            con.execute(update(records).where(records.c.task_id==task['id'],records.c.id==rows[0]['id']).values(source={**rows[0]['source'],'kind':'context'}))
    with pytest.raises(WorkflowError): preview(tasks,task['id'],'local',workbook(supplied,headers))
    assert tasks.get(task['id'],'local')['revision']==task['revision']


def test_blank_and_same_name_do_not_clear_or_create_revision(case):
    tasks,task,rows,_=case
    task=tasks.edit(task['id'],'local','local',EditRequest(base_revision=task['revision'],changes={rows[0]['id']:{'product_name':'原名称'}}))
    p=preview(tasks,task['id'],'local',workbook([[rows[0]['id'],'原名称'],[rows[1]['id'],'  ']]))
    assert p['payload']['counts']=={'changed':0,'blank':1,'unchanged':1,'overwritten':0}
    with pytest.raises(WorkflowError,match='没有需要'): apply(tasks,task['id'],'local','local',p['id'])
    assert tasks.get(task['id'],'local')['revision']==task['revision']


def test_preview_conflict_and_wrong_task_or_tenant(case):
    tasks,task,rows,_=case
    p=preview(tasks,task['id'],'local',workbook([[rows[0]['id'],'确认名称']]))
    with pytest.raises(WorkflowError): apply(tasks,'other','local','local',p['id'])
    with pytest.raises(WorkflowError): apply(tasks,task['id'],'other','local',p['id'])
    tasks.edit(task['id'],'local','local',EditRequest(base_revision=task['revision'],changes={rows[0]['id']:{'product_name':'后来改的名称'}}))
    with pytest.raises(WorkflowError,match='新版本'): apply(tasks,task['id'],'local','local',p['id'])
    p=preview(tasks,task['id'],'local',workbook([[rows[0]['id'],'确认名称']]))
    assert p['payload']['counts']['overwritten']==1
    assert p['payload']['samples'][0]['before']=='后来改的名称'


def test_confirmation_previews_removed_with_task(case):
    tasks,task,rows,_=case
    p=preview(tasks,task['id'],'local',workbook([[rows[0]['id'],'确认名称']]))
    tasks.delete('local',[task['id']])
    with tasks.store.engine.connect() as con:
        with pytest.raises(WorkflowError): tasks.store.get(con,p['id'],'local')


def test_does_not_expand_shared_source_name_group(case):
    tasks,task,rows,_=case
    with tasks.store.engine.begin() as con:
        for row in rows:
            con.execute(update(records).where(records.c.task_id==task['id'],records.c.id==row['id']).values(source={**row['source'],'merged_fields':{'product_name':'shared-name'}}))
    p=preview(tasks,task['id'],'local',workbook([[rows[0]['id'],'仅这一条']]))
    result=apply(tasks,task['id'],'local','local',p['id'])
    actual=tasks.snapshot(task['id'],'local',result['revision'],include_rows=True)[1]
    assert [r['values_json']['product_name'] for r in actual]==['仅这一条','','']


def test_http_preview_apply_and_final_export(case):
    tasks,task,rows,root=case
    app=FastAPI()
    @app.middleware('http')
    async def identity(request:Request,call_next):
        request.state.tenant_id='local';request.state.user_id='local';request.state.role='owner'
        return await call_next(request)
    @app.exception_handler(WorkflowError)
    async def failure(request,exc): return JSONResponse({'detail':str(exc)},status_code=exc.status)
    app.include_router(build_router(root, inline_executor_for_tests=True))
    with TestClient(app) as client:
        url=f"/api/v1/workflow-tasks/{task['id']}/maintenance/name-confirmations"
        result=client.post(url,files={'excel_file':('确认.xlsx',workbook([[r['id'],f'确认名称{i}'] for i,r in enumerate(reversed(rows))]))})
        assert result.status_code==200,result.text
        result=client.post(url+'/apply',json={'preview_id':result.json()['id']})
        assert result.status_code==200,result.text
    task=result.json(); assert task['payload']['error_count']==0
    run,_=tasks.create_export(task['id'],'local',task['revision']);tasks.generate(run['id'],'local')
    path=tasks.export_file(task['id'],run['id'],'local','标准商品表.xlsx')
    book=load_workbook(path);sheet=book.active
    assert sheet.cell(4,1).value=='确认名称2'
    assert any('功率：3W' in str(c.value) for row in sheet.iter_rows(min_row=4) for c in row)
    book.close()

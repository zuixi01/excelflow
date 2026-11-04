from io import BytesIO
from copy import deepcopy
import pytest
from openpyxl import Workbook,load_workbook
from openpyxl.styles import Font,PatternFill,Alignment

from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.field_profiles import FieldProfileService,ProfileEdit,FieldProfile,ConfiguredField
from excel_auditor.workflow.models import MappingRequest,FlexibleReturnRequest,EditRequest
from excel_auditor.workflow.tasks import TaskService


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(AIClient,'settings',lambda self:{'configured':False})
    store=WorkflowStore(tmp_path/'workflow');profiles=FieldProfileService(store);state=profiles.get('local')
    state=profiles.save('local',ProfileEdit(base_revision=state['revision'],profile=FieldProfile(fixed_fields=[
        ConfiguredField(id='product_name',title='商品名称',group='基本',required=True),ConfiguredField(id='sku',title='型号'),ConfiguredField(id='brand',title='品牌'),
        ConfiguredField(id='other_description',title='备注',aliases=['备注：'])])))
    profiles.publish('local',state['revision'])
    yield store,TaskService(store)
    store.engine.dispose()


NOTE='电机价格包含电机、安装和遥控器，轨道超过五米需要按实际长度另行计价。'
def supplier():
    book=Workbook();s=book.active
    s.append(['商品名称','型号','品牌','定制说明','原表空列'])
    s.append(['电机','DT98','杜亚','两种定制方案',''])
    s.append([NOTE]);s.merge_cells('A3:C3')
    s['D2'].font=Font(name='宋体',color='1F497D',bold=True)
    s['D2'].fill=PatternFill('solid',fgColor='FFE699')
    s['A3'].alignment=Alignment(wrap_text=True,vertical='center')
    s.column_dimensions['D'].width=27;s.row_dimensions[3].height=72
    b=BytesIO();book.save(b);book.close();return b.getvalue()


def imported(runtime):
    _,service=runtime
    task,_=service.create_standardization('local','tester','','source.xlsx',supplier());service.parse(task['id'],'local')
    task=service.get(task['id'],'local')
    return service.confirm_mapping(task['id'],'local','tester',MappingRequest(base_revision=task['revision'],sheets=[
        dict(sheet=s['sheet'],header_row=s['header_row'],header_depth=s['header_depth'],bindings=s['suggested_bindings']) for s in task['payload']['sheets']]))


def roundtrip(service,task,content):
    inspected=service.inspect_return(task['id'],'local',content)
    choices=[{k:c[k] for k in ('key','action','field_id','title','kind','type')} for c in inspected['payload']['columns']]
    ready=service.preview_flexible_return(task['id'],'local',inspected['id'],FlexibleReturnRequest(columns=choices))
    assert ready['status']=='ready'
    return ready


def test_note_is_not_a_product_and_unmapped_cells_restore_as_one_rectangle(runtime):
    _,service=runtime;task=imported(runtime)
    assert task['status']=='ready' and task['payload']['error_count']==0
    rows=service.rows(task['id'],'local',0,100)['items'];assert len(rows)==2
    assert rows[1]['source']['kind']=='context'
    assert all(not rows[1]['values_json'][k] for k in ['product_name','sku','brand'])
    path=service.maintenance_file(task['id'],'local');book=load_workbook(path);s=book['维护表']
    fields=task['payload']['config']['fields'];extra={f['source_column']['column']:i for i,f in enumerate(fields,2) if f.get('source_column')}
    assert s.cell(4,extra[4]).value=='两种定制方案'
    assert s.cell(4,extra[4]).font.name=='宋体' and s.cell(4,extra[4]).fill.fgColor.rgb=='00FFE699'
    assert s.column_dimensions[s.cell(4,extra[4]).column_letter].width==27
    assert s.row_dimensions[5].height==72
    assert any(r.min_row==r.max_row==5 and r.min_col==extra[1] and r.max_col==extra[3] for r in s.merged_cells.ranges)
    assert sum(c.value==NOTE for c in s[5])==1
    book.close()
    returned=roundtrip(service,task,path.read_bytes())
    assert returned['payload']['counts']=={'changed':0,'added':0,'missing':0}
    assert returned['payload']['structure_summary']==[]
    task=service.apply_return(task['id'],'local','tester',returned['id'],False)
    checked=load_workbook(service.maintenance_file(task['id'],'local'))
    assert sum(c.value==NOTE for c in checked['维护表'][5])==1
    checked.close()
    run,_=service.create_export(task['id'],'local',task['revision']);service.generate(run['id'],'local')
    final=load_workbook(service.export_file(task['id'],run['id'],'local','标准商品表.xlsx'))
    assert sum(c.value==NOTE for c in final.active[5])==1
    final.close()


def test_unmerge_and_change_one_retained_cell_does_not_merge_away_the_edit(runtime):
    _,service=runtime;task=imported(runtime)
    book=load_workbook(service.maintenance_file(task['id'],'local'));s=book['维护表']
    region=next(r for r in s.merged_cells.ranges if r.min_row==r.max_row==5 and r.max_col>r.min_col)
    left,right=region.min_col,region.max_col;s.unmerge_cells(str(region))
    s.cell(5,left,'运营修改后的说明');s.cell(5,right,'独立补充文字')
    b=BytesIO();book.save(b);book.close()
    ready=roundtrip(service,task,b.getvalue())
    assert any('合并关系变化' in m for m in ready['payload']['structure_summary'])
    task=service.apply_return(task['id'],'local','tester',ready['id'],False,confirm_structure=True)
    book=load_workbook(service.maintenance_file(task['id'],'local'));s=book['维护表']
    assert s.cell(5,left).value=='运营修改后的说明' and s.cell(5,right).value=='独立补充文字'
    assert not any(r.min_row==r.max_row==5 and r.min_col<=left and r.max_col>=right for r in s.merged_cells.ranges)
    book.close()


def test_repair_reuses_record_ids_and_preserves_existing_product_edits(runtime):
    _,service=runtime;task=imported(runtime)
    rows=service.rows(task['id'],'local',0,100)['items'];ids=[r['id'] for r in rows]
    task=service.edit(task['id'],'local','tester',EditRequest(base_revision=task['revision'],changes={ids[0]:{'product_name':'运营维护的商品名称'}}))
    task=service.repair_source_retention(task['id'],'local','tester',task['revision'])
    rows=service.rows(task['id'],'local',0,100)['items']
    assert [r['id'] for r in rows]==ids and rows[0]['values_json']['product_name']=='运营维护的商品名称'
    assert not task['payload']['source_review']['missing_product_names']


def test_editing_one_merged_note_updates_its_logical_cell(runtime):
    _,service=runtime;task=imported(runtime);rows=service.rows(task['id'],'local',0,100)['items']
    note=rows[1];fields=note['source']['rectangular_merges'][0]['fields']
    service.edit(task['id'],'local','tester',EditRequest(base_revision=task['revision'],changes={note['id']:{fields[0]:'运营修订的完整说明'}}))
    updated=service.rows(task['id'],'local',0,100)['items'][1]
    assert all(updated['values_json'][fid]=='运营修订的完整说明' for fid in fields)
    assert not updated['values_json']['product_name']


def test_numbered_short_merged_notes_are_context_and_remark_does_not_win_header_detection(runtime):
    _,service=runtime
    book=Workbook();s=book.active;s.title='富轩吊装费用'
    s.append(['吊装费用价格表']);s.merge_cells('A1:F1')
    s.append(['玻璃面积',None,'楼层','结算价']);s.merge_cells('A2:B3');s.merge_cells('C2:C3');s.merge_cells('D2:F2')
    s.cell(3,4,'元/片')
    s.append(['3-5㎡',None,'2-29楼',800,'加一片',150])
    s.cell(30,1,'备注：');s.cell(30,2,'费用不含税')
    content=BytesIO();book.save(content);book.close()
    task,_=service.create_standardization('local','tester','','fee.xlsx',content.getvalue());service.parse(task['id'],'local')
    task=service.get(task['id'],'local');source=task['payload']['sheets'][0]
    assert (source['header_row'],source['header_depth'])==(2,2)
    task=service.confirm_mapping(task['id'],'local','tester',MappingRequest(base_revision=task['revision'],sheets=[
        dict(sheet=source['sheet'],header_row=2,header_depth=2,bindings=source['suggested_bindings'])]))
    assert task['status']=='ready' and task['payload']['record_count']==27
    assert all(r['source']['kind']=='context' for r in service.rows(task['id'],'local',0,100)['items'])
    from excel_auditor.workflow.source_presentation import context_rows
    from types import SimpleNamespace
    source=dict(sheet='门',rows=[['名称','型号','备注'],['3.锁具为铝材配套']*3],columns=[{'header':v} for v in ['名称','型号','备注']],merged_ranges=[[2,2,1,3]])
    assert context_rows(source,SimpleNamespace(header_row=1,header_depth=1,bindings=[]))[2]=='原表合并说明'


def test_subtotal_with_zero_and_remark_is_context_but_named_product_is_not():
    from types import SimpleNamespace
    from excel_auditor.workflow.models import Binding
    from excel_auditor.workflow.source_presentation import context_rows
    source=dict(sheet='套餐',columns=[{'header':v} for v in ['产品名称','型号','配置','供价','备注']],
                rows=[['产品名称','型号','配置','供价','备注'],['','','合计','0','不含安装'],['商品A','A1','合计','0','不含安装']],merged_ranges=[])
    mapping=SimpleNamespace(header_row=1,header_depth=1,bindings=[Binding(field='product_name',columns=[1]),Binding(field='sku',columns=[2])])
    result=context_rows(source,mapping)
    assert result[2]=='原表汇总/合计' and 3 not in result


def test_subtotal_repair_preserves_raw_notes_and_other_products(runtime):
    _,service=runtime
    book=Workbook();sheet=book.active
    sheet.append(['商品名称','型号','产品配置','供价','备注'])
    sheet.append(['套餐商品','P1','浴室柜和花洒',1200,'含安装'])
    sheet.append(['','','合计',0,'不含安装'])
    content=BytesIO();book.save(content);book.close()
    task,_=service.create_standardization('local','tester','','set.xlsx',content.getvalue());service.parse(task['id'],'local')
    task=service.get(task['id'],'local')
    task=service.confirm_mapping(task['id'],'local','tester',MappingRequest(base_revision=task['revision'],sheets=[
        dict(sheet=s['sheet'],header_row=s['header_row'],header_depth=s['header_depth'],bindings=s['suggested_bindings']) for s in task['payload']['sheets']]))
    assert task['status']=='ready' and task['payload']['source_review']['context_rows']==1
    rows=service.rows(task['id'],'local',0,100)['items']
    assert rows[0]['values_json']['product_name']=='套餐商品'
    assert rows[1]['source']['context_reason']=='原表汇总/合计'
    assert not rows[1]['values_json']['other_description']
    assert '不含安装' in rows[1]['values_json'].values() and '合计' in rows[1]['values_json'].values()

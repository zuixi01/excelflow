from copy import deepcopy
from io import BytesIO
import json

from openpyxl import Workbook
import pytest

from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.field_profiles import FieldProfileService, FieldProfile, LocalCategory, ConfiguredField, ProfileEdit
from excel_auditor.workflow.models import WorkflowError, MappingRequest
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.semantic_mapping import suggest_semantic_bindings
from excel_auditor.workflow.source_blocks import split_horizontal_tables
from excel_auditor.workflow.tasks import TaskService
from excel_auditor.workflow.specification_compatibility import text_specification_profile
from excel_auditor.workflow.field_profiles import compile_profile


def profile():
    return FieldProfile(fixed_fields=[ConfiguredField(id='product_name', title='商品名称', required=True),
        ConfiguredField(id='sale_price', title='销售价', type='decimal'),
        ConfiguredField(id='warranty_days', title='质保天数', aliases=['质保时间'], type='decimal')],
        local_categories=[LocalCategory(id='lighting', title='灯具', attributes=[
            ConfiguredField(id='dimensions', title='尺寸', type='decimal'),
            ConfiguredField(id='area', title='照射面积', type='decimal')])])


def workbook():
    book=Workbook();sheet=book.active
    sheet.append(['商品名称','销售价','尺寸','质保时间',None,None,'商品名称','销售价'])
    sheet.append(['窗帘',12,'600*1200','2年',None,None,'轨道',8])
    sheet.append(['布帘',15,'750*1500','16年',None,None,'备注：轨道费用按米计算，仅作说明'])
    sheet.merge_cells('C2:C3')
    content=BytesIO();book.save(content);book.close()
    return content.getvalue()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(AIClient, 'settings', lambda self: {'configured': False})
    store=WorkflowStore(tmp_path/'workflow');profiles=FieldProfileService(store)
    state=profiles.get('local')
    state=profiles.save('local', ProfileEdit(base_revision=state['revision'],profile=profile()))
    profiles.publish('local',state['revision'])
    yield store,TaskService(store),profiles
    store.engine.dispose()


def test_upgrade_changes_only_authorized_types_and_preserves_unpublished_draft(runtime):
    store,tasks,profiles=runtime
    task,_=tasks.create_standardization('local','test','','curtain.xlsx',workbook());tasks.parse(task['id'],'local')
    before=tasks.get(task['id'],'local');old_file=before['payload']['source_data_file']
    saved=profiles.get('local');draft=deepcopy(saved['payload']['draft']);draft['name']='未发布编辑'
    draft['fixed_fields'][0]['description']='保留未发布描述'
    saved=profiles.save('local',ProfileEdit(base_revision=saved['revision'],profile=draft))
    upgraded=profiles.upgrade_specification_types('local',saved['revision'])
    assert upgraded['status']=='draft'
    assert upgraded['payload']['published']['name']!='未发布编辑'
    assert upgraded['payload']['draft']['fixed_fields'][0]['description']=='保留未发布描述'
    result=tasks.analyze_mapping(task['id'],'local','test',before['revision'],upgrade_snapshot=True)
    assert result['revision']==before['revision']+1
    assert result['payload']['source_data_file']!=old_file
    assert (store.directory(task['id'])/old_file).is_file()
    assert result['payload']['field_snapshot']['version']==2
    config=result['payload']['config'];previous=before['payload']['config']
    assert [f['rule']['name'] for f in config['fields']]==[f['rule']['name'] for f in previous['fields']]
    assert [f['rule']['title'] for f in config['fields']]==[f['rule']['title'] for f in previous['fields']]
    for key in ('source_header_layout','preserve_source_merges','exclude_source_sequence'):
        assert config[key]==previous[key]
    assert all(f['rule']['type']=='string' for f in config['fields'] if f['rule']['name'].startswith('local.'))
    raw=json.loads((store.directory(task['id'])/result['payload']['source_data_file']).read_text('utf-8'))
    left,right=raw
    assert left['source_block']['range']=='A:D' and right['source_block']['range']=='G:H'
    assert right['columns'][0]['source_column']==7 and right['rows'][1]==['轨道','8']
    assert [s['source_sheet'] for s in raw]==['Sheet','Sheet']
    assert right['excluded_rows']==[3]
    assert left['merged_ranges']==[[2,3,3,3]] and not right['merged_ranges']
    for source in raw:
        assert len([b for b in source['suggested_bindings'] if b['field']=='product_name'])==1
        assert not any('多个可能来源' in note for note in source['mapping_notes'])
        assert not any(b['field']=='warranty_days' for b in source['suggested_bindings'])
    assert left['mapping_notices']
    result=tasks.confirm_mapping(task['id'],'local','test',MappingRequest(base_revision=result['revision'],sheets=[
        dict(sheet=s['sheet'],header_row=s['header_row'],header_depth=s['header_depth'],bindings=s['suggested_bindings']) for s in raw]))
    assert result['status']=='ready' and result['payload']['error_count']==0
    rows=tasks.rows(task['id'],'local',0,100)['items']
    assert [r['values_json']['product_name'] for r in rows]==['窗帘','布帘','轨道','']
    assert rows[-1]['source']['kind']=='context'
    assert rows[-1]['source']['source_block']['start_column']==7
    warranty=next(f for f in result['payload']['config']['fields'] if f.get('source_column') and '质保时间' in f['source_column']['header'])
    assert [r['values_json'][warranty['rule']['name']] for r in rows]==['2年','16年','','']
    with pytest.raises(WorkflowError):tasks.analyze_mapping(task['id'],'local','test',result['revision'],upgrade_snapshot=True)


@pytest.mark.parametrize('header,value,bind', [('质保时间','2年',False),('质保时间','16年',False),('质保时间','12个月',False),
    ('质保时间','365',False),('质保时间','365天',True),('质保天数','365',True)])
def test_warranty_exact_and_semantic_preserve_original(runtime,monkeypatch,header,value,bind):
    store,_,profiles=runtime
    config,_=profiles.snapshot('local')
    sheet=dict(sheet='test',header_row=1,header_depth=1,rows=[[header],[value]],columns=[dict(column=1,header=header)])
    suggest_semantic_bindings([sheet],config,AIClient(store.root,'local'),[])
    assert any(b['field']=='warranty_days' for b in sheet['suggested_bindings'])==bind
    assert sheet['verbatim_columns']==[1]
    if not bind:
        monkeypatch.setattr(AIClient,'settings',lambda self:{'configured':True,'model':'test'})
        monkeypatch.setattr(AIClient,'reconcile_field_mappings',lambda self,*a,**k:[dict(field_id='warranty_days',column=1,confidence=.99,reason='质保字段')])
        suggest_semantic_bindings([sheet],config,AIClient(store.root,'local'),[])
        assert not any(b['field']=='warranty_days' for b in sheet['suggested_bindings'])
        assert all('不符合目标字段类型' not in n for n in sheet['mapping_notes'])


def test_split_requires_empty_cells_and_projects_original_image_coordinates():
    columns=[dict(column=i,header=h) for i,h in enumerate(['商品名称','销售价','','商品名称','销售价'],1)]
    source=dict(sheet='商品',header_row=1,header_depth=1,columns=columns,rows=[['商品名称','销售价','','商品名称','销售价'],
        ['长商品名称绝不能因为只有名称就被当作备注删除',12,'','轨道',8]],merged_ranges=[],widths=[10]*5,
        image_locations=[{'cell':'E2','sheet':'商品','id':'asset'}])
    left,right=split_horizontal_tables(source)
    assert not left['excluded_rows']
    assert right['image_locations'][0]['cell']=='B2' and right['image_locations'][0]['source_cell']=='E2'
    source['rows'][1][2]='有数据，不是分隔列'
    assert split_horizontal_tables(source)==[source]


def test_snapshot_upgrade_conflict_and_wrong_tenant(runtime):
    store,tasks,_=runtime
    task,_=tasks.create_standardization('local','test','','curtain.xlsx',workbook());tasks.parse(task['id'],'local')
    before=tasks.get(task['id'],'local')
    with pytest.raises(WorkflowError):tasks.analyze_mapping(task['id'],'local','test',before['revision']+1,upgrade_snapshot=True)
    with pytest.raises(WorkflowError):tasks.analyze_mapping(task['id'],'another','test',before['revision'],upgrade_snapshot=True)
    assert tasks.get(task['id'],'local')['payload']['source_data_file']==before['payload']['source_data_file']


@pytest.mark.parametrize('header,value,title', [('规格','600*1200','尺寸'),('标准尺寸','2150*880*240','尺寸'),
    ('标准尺寸（门扇）','2340*900*55/57/60','尺寸'),('试用面积','20-30m²','照射面积')])
def test_text_specifications_do_not_depend_on_model_omissions(runtime,header,value,title):
    store,_,_=runtime
    converted,_=text_specification_profile(profile())
    config=compile_profile(converted)
    sheet=dict(sheet='test',header_row=1,header_depth=1,rows=[[header],[value]],columns=[dict(column=1,header=header)])
    suggest_semantic_bindings([sheet],config,AIClient(store.root,'local'),[])
    field=next(f for f in config.fields if f.rule.title.split('/')[-1]==title)
    assert any(b['field']==field.rule.name and b['columns']==[1] and b['method']=='header_rule' for b in sheet['suggested_bindings'])

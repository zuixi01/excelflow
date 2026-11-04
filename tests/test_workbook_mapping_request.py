import json

import httpx
import pytest

from excel_auditor.workflow.ai import AIClient, AISettings
from excel_auditor.workflow.models import WorkflowConfig, WorkflowError
from excel_auditor.workflow.semantic_mapping import suggest_semantic_bindings


def test_all_fields_from_all_sheets_are_sent_once_and_results_do_not_cross_sheets(tmp_path, monkeypatch):
    monkeypatch.delenv('REDIS_URL',raising=False)
    requests=[]
    def handle(request):
        body=json.loads(request.content)
        supplied=json.loads(body['messages'][1]['content'])
        requests.append(supplied)
        assert len(supplied['fields'])==300  # No 25/50 truncation or splitting.
        assert len(supplied['columns'])==4
        assert {f['sheet_name'] for f in supplied['fields']}=={'封窗','瓷砖'}
        assert 'private row value' not in request.content.decode()
        fields=supplied['fields']
        first=fields[0]['id']; second=fields[150]['id']
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'mappings':[
            {'field_id':first,'column':3,'confidence':1,'reason':'wrong sheet'},
            {'field_id':first,'column':2,'confidence':.95,'reason':'correct first sheet'},
            {'field_id':second,'column':4,'confidence':.95,'reason':'correct second sheet'},
        ]})}}]})
    ai=AIClient(tmp_path,'local',httpx.MockTransport(handle))
    ai.save(AISettings(base_url='https://models.example/v1',model='test',api_key='test-only'))
    config=WorkflowConfig(name='test',fields=[{'rule':{'name':f'f{i}','title':f'Target {i}'}} for i in range(150)],
        outputs=[{'name':'out.xlsx','sheets':[{'columns':[{'title':'Target 0','field':'f0'}]}]}])
    sheets=[{'sheet':name,'columns':[{'column':1,'header':'同名列'},{'column':2,'header':'同名列'}],
             'rows':[['private row value']]} for name in ['封窗','瓷砖']]
    events=[]
    suggest_semantic_bindings(sheets,config,ai,[],progress=events.append)
    assert len(requests)==1
    for sheet in sheets:
        assert [(b['field'],b['columns']) for b in sheet['suggested_bindings']]==[('f0',[2])]
        assert sheet['mapping_summary']['model_status']=='completed'
    assert max(event.get('total_batches',0) for event in events)==1


def test_truncated_model_response_is_not_accepted_as_complete(tmp_path):
    ai=AIClient(tmp_path,'local',httpx.MockTransport(lambda request:httpx.Response(200,json={
        'choices':[{'finish_reason':'length','message':{'content':'{"mappings":[]}'}}]})))
    ai.save(AISettings(base_url='https://models.example/v1',model='test',api_key='test-only'))
    with pytest.raises(WorkflowError,match='语义匹配结果'):
        ai.reconcile_field_mappings([{'id':'f0'}],[{'column':1,'header':'来源'}],multiple_targets=True)


def test_local_full_column_type_check_keeps_composite_dimensions_out_of_numbers():
    from excel_auditor.workflow.models import MaintenanceField
    from excel_auditor.workflow.semantic_mapping import source_type_compatible
    size=MaintenanceField(rule={'name':'size','title':'尺寸','type':'decimal'})
    sheet={'header_row':1,'header_depth':1,'rows':[['规格'],['600×600'],['800×800']]}
    assert not source_type_compatible(sheet,size,1)
    sheet['rows'].append(['600'])
    assert source_type_compatible(sheet,size,1)  # Mixed data remains reviewable.
    days=MaintenanceField(rule={'name':'warranty_days','title':'质保天数','type':'integer'})
    assert source_type_compatible({'rows':[['质保天数'],['5840天']]},days,1)

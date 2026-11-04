from io import BytesIO

from openpyxl import Workbook

from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.models import MappingRequest
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.tasks import TaskService


def test_same_upload_after_confirmation_and_reanalysis_each_call_model(tmp_path, monkeypatch):
    store=WorkflowStore(tmp_path/'workflow')
    service=TaskService(store)
    profiles=FieldProfileService(store)
    draft=profiles.get('local')
    saved=profiles.save('local',ProfileEdit(base_revision=draft['revision'],profile=FieldProfile(
        fixed_fields=[ConfiguredField(id='sku',title='商品编码')])) )
    profiles.publish('local',saved['revision'])
    book=Workbook()
    book.active.append(['来源字段'])
    book.active.append(['商品 A'])
    data=BytesIO()
    book.save(data)
    book.close()
    calls=[]
    monkeypatch.setattr(AIClient,'settings',lambda self:{'configured':True,'model':'test'})
    def respond(self, fields, columns, **kwargs):
        calls.append((fields,columns))
        return [{'field_id':'sku','column':1,'confidence':.95,'reason':f'fresh {len(calls)}'}]
    monkeypatch.setattr(AIClient,'reconcile_field_mappings',respond)
    # A Redis configuration must not introduce hidden cross-upload reuse.
    monkeypatch.setenv('REDIS_URL','redis://not-used:6379/0')
    from excel_auditor.workflow import mapping_runtime
    monkeypatch.setattr(mapping_runtime,'_shared_cache',lambda *_: (_ for _ in ()).throw(AssertionError('cache accessed')))
    try:
        def upload():
            task,_=service.create_standardization('local','tester','fresh','source.xlsx',data.getvalue())
            service.parse(task['id'],'local')
            task=service.get(task['id'],'local')
            assert task['status']=='mapping'
            return task
        first=upload()
        sheet=first['payload']['sheets'][0]
        service.confirm_mapping(first['id'],'local','tester',MappingRequest(base_revision=first['revision'],sheets=[
            dict(sheet=sheet['sheet'],header_row=sheet['header_row'],header_depth=sheet['header_depth'],bindings=sheet['suggested_bindings'])]))
        second=upload()
        assert len(calls)==2
        assert second['payload']['sheets'][0]['suggested_bindings'][0]['reason'].startswith('fresh 2')
        third=service.analyze_mapping(second['id'],'local','tester',second['revision'])
        assert len(calls)==3
        assert third['payload']['sheets'][0]['suggested_bindings'][0]['reason'].startswith('fresh 3')
        assert third['payload']['processing_progress']['total_batches']==1
    finally:
        store.engine.dispose()

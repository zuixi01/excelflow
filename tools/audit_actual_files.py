"""Exercise eleven original uploads in an isolated template; never edits production."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

from openpyxl import load_workbook
from excel_auditor.workflow.field_profiles import FieldProfileService, FieldProfile, NewFieldProfile, ProfileEdit
from excel_auditor.workflow.models import MappingRequest, FlexibleReturnRequest
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.tasks import TaskService

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/project-audit-20261102'
TARGET='task_91d9a3c4444f4eae96f0ca20ff6c981f'


def production_state():
    with sqlite3.connect((ROOT/'var/workflow/workflow.sqlite3').as_uri()+'?mode=ro',uri=True) as con:
        task=con.execute('select payload from workflow_objects where id=?',(TARGET,)).fetchone()[0]
        rows=con.execute('select id,values_json,source,issues from workflow_records where task_id=? order by ordinal',(TARGET,)).fetchall()
        all_tasks=con.execute("select id,revision,status,payload from workflow_objects where kind in ('task','fieldconfig') order by id").fetchall()
    return json.loads(task),hashlib.sha256(json.dumps([all_tasks,rows],ensure_ascii=False).encode()).hexdigest()


def headers(sheet):
    return {'values':[[c.value for c in row] for row in sheet.iter_rows(max_row=3)],
        'merges':sorted(str(r) for r in sheet.merged_cells.ranges if r.max_row<=3),
        'styles':[[str(c._style) for c in row] for row in sheet.iter_rows(max_row=3)],
        'widths':{k:v.width for k,v in sheet.column_dimensions.items()}}


def image_hashes(file):
    with zipfile.ZipFile(file) as z:
        return {hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('xl/media/') and not n.endswith('/')}


original,before=production_state()
store=WorkflowStore(OUT/'.eleven/workflow');tasks=TaskService(store);profiles=FieldProfileService(store)
profile=FieldProfile.model_validate(original['field_snapshot']['profile'])
profile.name='11文件保留原文验收（隔离模板）'
# Data validation is tested separately against the unchanged business template.
# This copy keeps all literal values, including absent names and unit-bearing power strings.
overrides=[]
for field in profile.fixed_fields:
    if field.id=='product_name':
        field.required=False;overrides.append('仅验收模板将商品名称设为非必填，空名称仍为空')
for group in profile.local_categories:
    for field in [*group.attributes,*group.specifications]:
        if field.title=='功率':
            field.type='string';field.min=None;field.max=None;overrides.append('仅验收模板将功率设为文本，保留原始 3W 等值')
draft=profiles.create('local',NewFieldProfile(name=profile.name))
draft=profiles.save('local',ProfileEdit(base_revision=draft['revision'],profile=profile),draft['id'])
published=profiles.publish('local',draft['revision'],draft['id'])
source=ROOT/'var/workflow/files'/TARGET
files=[]
for info in original['source_files']:
    data=(source/info['storage_name']).read_bytes();assert hashlib.sha256(data).hexdigest()==info['sha256']
    files.append((info['filename'],data))
task,_=tasks.create_standardization_files('local','audit','11个实际文件完整回归',files,field_profile_id=published['id'])
print('11 original uploads copied; parsing...',flush=True)
tasks.parse(task['id'],'local');task=tasks.get(task['id'],'local');assert task['status']=='mapping',task['payload'].get('error')
task=tasks.confirm_mapping(task['id'],'local','audit',MappingRequest(base_revision=task['revision'],sheets=deepcopy(original['mapping']['sheets'])))
assert task['payload']['error_count']==0,[(r['source'],r['issues']) for r in tasks.snapshot(task['id'],'local',task['revision'],include_rows=True)[1] if r['issues']][:5]
rows=tasks.snapshot(task['id'],'local',task['revision'],include_rows=True)[1]
missing=sum(r['source'].get('kind')!='context' and not r['values_json'].get('product_name') for r in rows)
path=tasks.maintenance_file(task['id'],'local')
maintenance=OUT/'11文件_隔离维护表.xlsx';maintenance.write_bytes(path.read_bytes())
book=load_workbook(maintenance);layout=headers(book['维护表']);assert book['维护表'].column_dimensions['A'].hidden;book.close()
print(json.dumps({'rows':len(rows),'missing_names_preserved':missing,'maintenance_bytes':maintenance.stat().st_size}),flush=True)
inspection=tasks.inspect_return(task['id'],'local',maintenance.read_bytes())
choices=[{k:c[k] for k in ('key','action','field_id','title','kind','type')} for c in inspection['payload']['columns']]
preview=tasks.preview_flexible_return(task['id'],'local',inspection['id'],FlexibleReturnRequest(columns=choices))
assert preview['status']=='ready',preview['payload'].get('row_review')
assert preview['payload']['counts']=={'changed':0,'added':0,'missing':0}
assert not preview['payload']['structure_summary']
task=tasks.apply_return(task['id'],'local','audit',preview['id'],False)
again=load_workbook(tasks.maintenance_file(task['id'],'local'));assert headers(again['维护表'])==layout;again.close()
run,_=tasks.create_export(task['id'],'local',task['revision']);tasks.generate(run['id'],'local')
task=tasks.get(task['id'],'local');assert task['exports'][0]['status']=='completed',task['exports'][0]['payload'].get('error')
final=OUT/'11文件_隔离最终表.xlsx';final.write_bytes(tasks.export_file(task['id'],run['id'],'local','标准商品表.xlsx').read_bytes())
book=load_workbook(final);sheet=book.active
dynamic_columns=[c for c in run['payload']['config']['outputs'][0]['sheets'][0]['columns'] if c['operation']=='key_value'];assert len(dynamic_columns)==2
assert all(c['operation']!='key_value' for c in run['payload']['config']['outputs'][0]['sheets'][0]['columns'] if not c['title'].startswith('动态字段/'))
final_rows=sheet.max_row-3;book.close()
assert image_hashes(maintenance)==image_hashes(final)
for info in original['source_files']:assert hashlib.sha256((source/info['storage_name']).read_bytes()).hexdigest()==info['sha256']
assert production_state()[1]==before
report={'passed':True,'source_files':len(files),'records':len(rows),'final_rows':final_rows,'missing_names_preserved':missing,
        'maintenance_return_counts':preview['payload']['counts'],'header_values_styles_merges_widths_unchanged':True,
        'image_hashes_preserved':len(image_hashes(final)),'only_dynamic_fields_summarized':True,'production_unchanged':True,
        'production_validation_errors':original['error_count'],'qa_template_overrides':overrides,'task_id':task['id']}
(OUT/'11文件验收结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False),flush=True)
store.engine.dispose()

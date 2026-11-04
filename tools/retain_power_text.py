"""Explicit, revision-checked power type migration; never fills product names."""
from copy import deepcopy
from pathlib import Path
import argparse
import hashlib
import json

from sqlalchemy import select, update
from excel_auditor.workflow.repository import WorkflowStore, objects, records
from excel_auditor.workflow.tasks import TaskService
from excel_auditor.workflow.field_profiles import FieldProfile, FieldProfileService, dynamic_id
from excel_auditor.workflow.models import WorkflowConfig, content_hash
from excel_auditor.workflow.excel import validate_records


def power_profile(raw):
    profile = FieldProfile.model_validate(raw).model_copy(deep=True)
    ids = set()
    for category in profile.local_categories:
        for member, kind in [('attributes', 'attribute'), ('specifications', 'specification')]:
            for field in getattr(category, member):
                if field.title == '功率':
                    field.type = 'string'
                    field.min = field.max = None
                    ids.add(dynamic_id(category, kind, field))
    return profile.model_dump(mode='json'), ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    out = Path('outputs/power-name-confirmation-20261102')
    out.mkdir(parents=True, exist_ok=True)
    store = WorkflowStore(Path('var/workflow'))
    service = TaskService(store)
    target = 'task_91d9a3c4444f4eae96f0ca20ff6c981f'
    task = service.get(target, 'local')
    task, rows = service.snapshot(target, 'local', task['revision'], include_rows=True)
    assert task['status'] in {'maintenance', 'ready'}
    template = FieldProfileService(store).get('local')
    with store.engine.connect() as con:
        template = store.get(con, template['id'], 'local', 'fieldconfig')
    before_task, before_rows = deepcopy(task), deepcopy(rows)
    before_template = deepcopy(template)
    snapshot = task['payload']['field_snapshot']
    profile, ids = power_profile(snapshot['profile'])
    assert len(ids) == 1
    power_id = next(iter(ids))
    snapshot.update(profile=profile, sha256=content_hash(profile))
    for member in ('draft', 'published'):
        if template['payload'].get(member):
            template['payload'][member], _ = power_profile(template['payload'][member])
    template['payload']['version'] += 1
    if content_hash(template['payload']['published']) == snapshot['sha256']:
        snapshot['version'] = template['payload']['version']
    config = WorkflowConfig.model_validate(task['payload']['config'])
    for field in config.fields:
        if field.rule.name in ids:
            field.rule.type = type(field.rule.type)('string')
            field.rule.validation.min = field.rule.validation.max = None
    old_config = deepcopy(before_task['payload']['config'])
    new_config = config.model_dump(mode='json')
    # Assert the entire layout and all other rules are unchanged.
    for f in old_config['fields']:
        if f['rule']['name'] in ids:
            f['rule']['type'] = 'string'
            f['rule']['validation']['min'] = f['rule']['validation']['max'] = None
    assert old_config == new_config
    errors, warnings = validate_records(rows, config)
    missing = {r['id'] for r in rows if r['source'].get('kind') != 'context' and not r['values_json'].get('product_name')}
    audit = json.loads(Path('outputs/name-audit-20261027/audit.json').read_text('utf-8'))
    assert missing == {r['record_id'] for r in audit['pending']} and len(missing) == 109
    for old, row in zip(before_rows, rows):
        assert all(old[k] == row[k] for k in ('id', 'ordinal', 'values_json', 'original', 'source'))
    power_errors_before = sum(i.get('field') == power_id for r in before_rows for i in r['issues'])
    assert errors == 109 and before_task['payload']['error_count'] == 149
    assert not any(i.get('field') == power_id for r in rows for i in r['issues'])
    for filename, digest in audit['input_hashes'].items():
        file = next(f for f in task['payload']['source_files'] if f['filename'] == filename)
        assert hashlib.sha256((store.directory(target)/file['storage_name']).read_bytes()).hexdigest() == digest
    task['payload'].update(config=new_config, error_count=errors, warning_count=warnings)
    task['status'] = 'maintenance'
    report = dict(task_id=target, before_revision=task['revision'], errors_before=149, errors_after=errors,
                  power_error_reduction=149-errors, missing_names=109, values_unchanged=True,
                  headers_layout_unchanged=True, source_files_unchanged=True, applied=args.apply)
    if args.apply:
        (out/'before.json').write_text(json.dumps({'task':before_task,'template':before_template,'rows':before_rows},ensure_ascii=False), 'utf-8')
        with store.engine.begin() as con:
            store.claim(con, target, 'local', task['revision'])
            store.claim(con, template['id'], 'local', template['revision'], 'fieldconfig')
            task['revision'] += 1
            template['revision'] += 1
            for row in rows:
                con.execute(update(records).where(records.c.task_id==target, records.c.id==row['id']).values(
                    issues=row['issues'], issue_count=len(row['issues']), search_text=row['search_text']))
            store.save(con, task)
            store.save(con, template)
            store.audit(con, task, before_task['payload']['actor'], {
                'action':'power_text_type_upgrade','fields':sorted(ids),'changed_records':0,
                'errors_before':149,'errors_after':109,'names_filled':0})
        report['revision'] = task['revision']
    (out/('applied.json' if args.apply else 'preview.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    (out/'confirmation-data.json').write_text(json.dumps({'task_id':target,'revision':task['revision'],
        'pending':audit['pending'],'counts_by_source':audit['counts_by_source']},ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))
    store.engine.dispose()


if __name__ == '__main__':
    main()

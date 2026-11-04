"""Exercise unified SOP in a temporary store, using only local header rules (no live model)."""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import urllib.request
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook

from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.field_profiles import FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.models import EditRequest, MappingRequest
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.tasks import TaskService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook', type=Path)
    parser.add_argument('--profile-url', default='http://127.0.0.1:8000/api/v1/field-profile')
    parser.add_argument('--output', type=Path, default=Path('var/acceptance/unified-sample/标准商品表.xlsx'))
    args = parser.parse_args()
    with urllib.request.urlopen(args.profile_url, timeout=10) as response:
        definition = FieldProfile.model_validate(json.load(response)['payload']['published'])
    with tempfile.TemporaryDirectory(prefix='excelflow-unified-') as directory:
        store = WorkflowStore(Path(directory) / 'workflow')
        try:
            assert not AIClient(store.root, 'local').settings()['configured']
            profiles, tasks = FieldProfileService(store), TaskService(store)
            draft = profiles.get('local')
            draft = profiles.save('local', ProfileEdit(base_revision=draft['revision'], profile=definition))
            profiles.publish('local', draft['revision'])
            created, _ = tasks.create_standardization('local', 'local', '隔离联合验收', args.workbook.name, args.workbook.read_bytes())
            tasks.parse(created['id'], 'local')
            task = tasks.get(created['id'], 'local')
            assert task['status'] == 'mapping', task['payload'].get('error')
            count = len(task['payload']['config']['fields'])
            assert task['payload']['field_snapshot']['matching_scope'] == 'all_fields'
            assert all(s['mapping_summary']['total'] == count for s in task['payload']['sheets'])
            request = MappingRequest(base_revision=task['revision'], sheets=[{
                'sheet': s['sheet'], 'header_row': s['header_row'], 'header_depth': s['header_depth'], 'bindings': s['suggested_bindings'],
            } for s in task['payload']['sheets']])
            task = tasks.confirm_mapping(task['id'], 'local', 'local', request)
            rows = tasks.rows(task['id'], 'local', 0, 100)['items']
            assert len(rows) == 77
            corrections = {}
            validation_notes = []
            rules = {field['rule']['name']: field['rule'] for field in task['payload']['config']['fields']}
            for row in rows:
                for issue in row['issues']:
                    field = issue['field']
                    value = row['values_json'][field]
                    if field == 'warranty_days':
                        assert value.endswith('天') and value[:-1].isdigit(), value
                        corrections.setdefault(row['id'], {})[field] = value[:-1]
                    else:
                        assert not rules[field]['required'] and rules[field]['type'] in {'decimal', 'integer'}, issue
                        # An explicit test-only operator edit: leave invalid optional numerics pending,
                        # never guess a dimension or introduce an automatic production conversion.
                        corrections.setdefault(row['id'], {})[field] = ''
                        validation_notes.append({'field': issue['title'], 'supplier_value': value, 'test_only_action': 'clear_invalid_optional_value'})
            if corrections:
                # Explicit test-only correction, not an automatic production conversion rule.
                task = tasks.edit(task['id'], 'local', 'local', EditRequest(base_revision=task['revision'], changes=corrections))
            assert task['status'] == 'ready'
            run, _ = tasks.create_export(task['id'], 'local', task['revision'])
            tasks.generate(run['id'], 'local')
            result = tasks.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx')
            book = load_workbook(result)
            plan = task['payload']['config']['outputs'][0]['sheets'][0]
            assert book.sheetnames == ['标准商品']
            assert book.active.max_column == count
            assert book.active.max_row - plan['data_start_row'] + 1 == 77
            book.close()
            dynamic_brand = next(field['rule']['name'] for field in task['payload']['config']['fields']
                                 if field['rule']['title'] == '动态字段/商品属性/品牌')
            brand_sheets = {row['source']['sheet'] for row in rows if any(b['field'] == dynamic_brand for b in row['source']['bindings'])}
            assert {'封窗', '瓷砖'} <= brand_sheets
            maintenance = tasks.maintenance_file(task['id'], 'local')
            book = load_workbook(maintenance)
            metadata = {row[0]: row[1] for row in book['_workflow_meta'].values if row[0]}
            dynamic_column = next(index + 2 for index, field in enumerate(task['payload']['config']['fields']) if field['rule']['name'] == dynamic_brand)
            book['维护表'].cell(metadata['header_depth'] + 1, dynamic_column, '隔离回传验收品牌')
            content = BytesIO()
            book.save(content)
            book.close()
            preview = tasks.preview_return(task['id'], 'local', content.getvalue())
            assert preview['payload']['counts']['changed'] == 1
            updated = tasks.apply_return(task['id'], 'local', 'local', preview['id'], False)
            assert updated['revision'] == task['revision'] + 1
            assert tasks.rows(task['id'], 'local', 0, 100)['items'][0]['values_json'][dynamic_brand] == '隔离回传验收品牌'
            args.output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(result, args.output)
            print(json.dumps({'records': 77, 'target_fields': count, 'matching_scope': 'all_fields',
                              'worksheets': ['标准商品'], 'dynamic_brand_matched_on': sorted(brand_sheets),
                              'roundtrip': 'passed', 'live_model_calls': 0, 'test_only_validation_edits': validation_notes,
                              'output': str(args.output.resolve())}, ensure_ascii=False))
        finally:
            store.engine.dispose()


if __name__ == '__main__':
    main()

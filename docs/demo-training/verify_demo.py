"""Read an existing synthetic fixture and rehearse the real service in an isolated store."""
from __future__ import annotations

import json
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from openpyxl import load_workbook
from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.models import EditRequest, MappingRequest, WorkflowConfig, WorkflowError
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.semantic_mapping import suggest_semantic_bindings
from excel_auditor.workflow.tasks import TaskService


def main():
    output = ROOT / 'outputs' / 'demo-training-20261026'
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT / 'var' / 'field-demo' / 'supplier-demo.xlsx'
    report = {'scope': 'isolated Python service rehearsal; no live AI, HTTP UI, or production infrastructure', 'steps': [],
              'source_sha256': {name: hashlib.sha256((ROOT / 'src' / 'excel_auditor' / 'workflow' / name).read_bytes()).hexdigest() for name in ['tasks.py', 'semantic_mapping.py', 'excel.py']}}
    with tempfile.TemporaryDirectory(prefix='excelflow-teaching-') as temp:
        store = WorkflowStore(Path(temp) / 'workflow')
        try:
            tasks, profiles = TaskService(store), FieldProfileService(store)
            assert not AIClient(store.root, 'local').settings()['configured']
            profile = FieldProfile(fixed_fields=[
                ConfiguredField(id='product_name', title='商品名称', aliases=['品名'], required=True),
                ConfiguredField(id='sku', title='商品编码', aliases=['货号'], type='id_code'),
                ConfiguredField(id='sale_price', title='销售价', aliases=['含税价格'], type='decimal', min='0'),
            ])
            draft = profiles.get('local')
            draft = profiles.save('local', ProfileEdit(base_revision=draft['revision'], profile=profile))
            profiles.publish('local', draft['revision'])
            task, _ = tasks.create_standardization('local', 'demo', '演示教学', source.name, source.read_bytes())
            report['steps'].append({'action': 'create', 'status': task['status']})
            tasks.parse(task['id'], 'local')
            task = tasks.get(task['id'], 'local')
            assert task['status'] == 'mapping', task['payload'].get('error')
            report['steps'].append({'action': 'parse', 'status': task['status'], 'sheets': task['payload']['sheets']})
            task = tasks.confirm_mapping(task['id'], 'local', 'demo', MappingRequest(base_revision=task['revision'], sheets=[
                {'sheet': s['sheet'], 'header_row': s['header_row'], 'header_depth': s['header_depth'], 'bindings': s['suggested_bindings']}
                for s in task['payload']['sheets']
            ]))
            rows = tasks.rows(task['id'], 'local', 0, 100)['items']
            assert len(rows) == 3 and rows[0]['values_json']['sku'] == '0001'
            assert task['status'] == 'maintenance'
            errors = [{'sku': r['values_json']['sku'], 'issues': r['issues']} for r in rows if r['issues']]
            report['steps'].append({'action': 'confirm', 'status': task['status'], 'errors': errors})
            try:
                tasks.create_export(task['id'], 'local', task['revision'])
            except WorkflowError as exc:
                report['blocked_export'] = str(exc)
            else:
                raise AssertionError('invalid data was exported')
            fixes = {r['id']: {'sale_price': '99.00'} for r in rows if r['issues']}
            task = tasks.edit(task['id'], 'local', 'demo', EditRequest(base_revision=task['revision'], changes=fixes))
            assert task['status'] == 'ready'
            report['steps'].append({'action': 'explicit_demo_edit', 'status': task['status'], 'note': '99.00 is a fictional operator-entered demonstration value, not inferred by AI'})
            maintenance = tasks.maintenance_file(task['id'], 'local')
            preview = tasks.preview_return(task['id'], 'local', maintenance.read_bytes())
            report['maintenance_roundtrip_preview'] = preview['payload']['counts']
            run, _ = tasks.create_export(task['id'], 'local', task['revision'])
            tasks.generate(run['id'], 'local')
            task = tasks.get(task['id'], 'local')
            assert task['status'] == 'completed'
            result = tasks.export_file(task['id'], run['id'], 'local', '标准商品表.xlsx')
            book = load_workbook(result)
            try:
                assert book.sheetnames == ['标准商品']
                assert book.active.max_column == 4
                retained = [f for f in task['payload']['config']['fields'] if f.get('source_column')]
                assert len(retained) == 1 and retained[0]['source_column']['header'] == '用料'
                start = task['payload']['config']['outputs'][0]['sheets'][0]['data_start_row']
                values = list(book.active.iter_rows(min_row=start, values_only=True))
                assert len(values) == 3 and values[0][1] == '0001'
                report['export'] = {'sheets': book.sheetnames, 'rows': values, 'retained_columns': ['用料'], 'status': task['status']}
            finally:
                book.close()
            shutil.copy2(source, output / '演示供应商原表.xlsx')
            shutil.copy2(result, output / '演示标准商品表.xlsx')
            shutil.copy2(maintenance, output / '隔离任务维护表.xlsx')
        finally:
            store.engine.dispose()

    # Verify the current workbook-level request and safe failure path.
    with tempfile.TemporaryDirectory(prefix='excelflow-batches-') as temp:
        config = WorkflowConfig(name='Workbook rehearsal', fields=[{'rule': {'name': f'field_{i}', 'title': f'Target {i}'}} for i in range(51)], outputs=[{'name': 'output.xlsx', 'sheets': [{'columns': [{'title': 'Target', 'field': 'field_0'}]}]}])
        sheets = [{'sheet': 'Input', 'columns': [{'column': 1, 'header': 'Target 0'}]}]
        def respond(self, fields, columns, **kwargs):
            raise ValueError('synthetic failure')
        with patch.object(AIClient, 'settings', return_value={'configured': True}), patch.object(AIClient, 'reconcile_field_mappings', respond):
            suggest_semantic_bindings(sheets, config, AIClient(Path(temp), 'local'), [])
        assert [b['field'] for b in sheets[0]['suggested_bindings']] == ['field_0']
        assert len(sheets[0]['model_errors']) == 1
        report['model_failure_fallback'] = {'target_count': 51, 'status': 'passed', 'exact_match_preserved': True}
        sheets = [{'sheet': name, 'columns': [{'column': 1, 'header': 'Other header'}]} for name in ['A', 'B']]
        calls = []
        def combined(self, fields, columns, **kwargs):
            calls.append({'fields': len(fields), 'columns': len(columns)})
            assert all('rows' not in c and 'example' not in c for c in columns)
            return [{'field_id': f['id'], 'column': next(c['column'] for c in columns if c['sheet_id'] == f['sheet_id']), 'confidence': .9, 'reason': 'Synthetic mapping'} for f in fields if f['id'].endswith('::field_0')]
        with patch.object(AIClient, 'settings', return_value={'configured': True}), patch.object(AIClient, 'reconcile_field_mappings', combined):
            suggest_semantic_bindings(sheets, config, AIClient(Path(temp), 'local'), [])
        assert len(calls) == 1 and calls[0]['fields'] == 102
        assert all(s['suggested_bindings'][0]['field'] == 'field_0' and s['suggested_bindings'][0]['columns'] == [1] for s in sheets)
        report['workbook_mapping_request'] = {'status': 'passed', 'calls': calls, 'sheet_local_columns_restored': True}
    (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'output': str(output), 'records': 3, 'model_requests_for_two_sheets': len(calls)}, ensure_ascii=False))


if __name__ == '__main__':
    main()

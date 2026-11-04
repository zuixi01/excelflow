import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from openpyxl import Workbook
import pytest

from excel_auditor.workflow.ai import AIClient
from excel_auditor.workflow.field_profiles import ConfiguredField, FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.progress import TaskProgress
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.tasks import TaskService


def create_task(tmp_path):
    store = WorkflowStore(tmp_path / 'workflow')
    service = TaskService(store)
    profiles = FieldProfileService(store)
    profile = FieldProfile(fixed_fields=[ConfiguredField(id=f'field_{i}', title=f'Target {i}') for i in range(51)])
    draft = profiles.get('local')
    saved = profiles.save('local', ProfileEdit(base_revision=draft['revision'], profile=profile))
    profiles.publish('local', saved['revision'])
    book = Workbook()
    book.remove(book.active)
    for title in ['Windows', 'Tiles']:
        sheet = book.create_sheet(title)
        sheet.append([f'Unknown {title} header'])
        sheet.append(['private row sample'])
    content = BytesIO()
    book.save(content)
    book.close()
    task, _ = service.create_standardization('local', 'test', 'Progress', 'source.xlsx', content.getvalue())
    return store, service, task


@pytest.mark.parametrize('reanalyze', [False, True])
def test_workbook_request_reports_progress_without_changing_revision(tmp_path, monkeypatch, reanalyze):
    store, service, task = create_task(tmp_path)
    monkeypatch.delenv('REDIS_URL', raising=False)
    if reanalyze:
        monkeypatch.setattr(AIClient, 'settings', lambda self: {'configured': False})
        service.parse(task['id'], 'local')
    monkeypatch.setattr(AIClient, 'settings', lambda self: {'configured': True, 'model': 'test-model'})
    started, release = threading.Event(), threading.Event()
    calls = []
    def respond(self, fields, columns, **kwargs):
        calls.append((fields, columns))
        assert len(fields) == 102 and len(columns) == 2
        started.set()
        assert release.wait(10)
        return []
    monkeypatch.setattr(AIClient, 'reconcile_field_mappings', respond)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = (pool.submit(service.analyze_mapping, task['id'], 'local', 'test', 0) if reanalyze
                       else pool.submit(service.parse, task['id'], 'local'))
            try:
                assert started.wait(10)
                deadline = time.monotonic() + 5
                while True:
                    partial = service.get(task['id'], 'local')
                    if all(s['status']=='running' for s in partial['payload']['processing_progress']['sheets']):
                        break
                    assert time.monotonic() < deadline
                    time.sleep(.01)
                progress = partial['payload']['processing_progress']
                assert progress['status'] == 'running' and progress['stage'] == 'ai_matching'
                assert progress['total_batches'] == 1 and progress['completed_batches'] == 0
                assert len(progress['sheets']) == 2 and all(s['status']=='running' for s in progress['sheets'])
                assert partial['revision'] == 0 and partial['status'] == ('mapping' if reanalyze else 'processing')
                summary = service.list('local')['items'][0]
                assert summary['ai_timing']['ai_started_at'] == progress['ai_started_at']
                assert summary['ai_timing']['status'] == 'running'
                assert summary['ai_timing']['ai_finished_at'] is None
                assert 'payload' not in summary and 'sheets' not in summary['ai_timing']
                assert not pending.done()
            finally:
                release.set()
            pending.result(10)
        final = service.get(task['id'], 'local')
        progress = final['payload']['processing_progress']
        assert final['status'] == 'mapping' and final['revision'] == (1 if reanalyze else 0)
        assert progress['status'] == 'completed' and progress['completed_batches'] == 1
        assert len(calls) == 1
        assert progress['finished_at'] and progress['ai_finished_at'] and progress['elapsed_seconds'] >= 0
        summary = service.list('local')['items'][0]['ai_timing']
        assert summary['ai_finished_at'] == progress['ai_finished_at']
        assert summary['status'] == 'completed'
        assert 'private row sample' not in json.dumps(calls)
    finally:
        store.engine.dispose()


def test_task_summary_does_not_invent_historical_ai_times(tmp_path):
    store, service, task = create_task(tmp_path)
    try:
        assert service.list('local')['items'][0]['ai_timing'] is None
        with store.engine.begin() as con:
            saved = store.get(con, task['id'], 'local', 'task')
            saved['payload']['processing_progress'] = {
                'status': 'failed', 'stage': 'failed',
                'ai_started_at': '2026-10-26T10:00:00+08:00',
                'finished_at': '2026-10-26T10:02:10+08:00',
            }
            store.save(con, saved)
        timing = service.list('local')['items'][0]['ai_timing']
        assert timing['status'] == 'failed'
        assert timing['ai_finished_at'] is None
        assert timing['finished_at'] == '2026-10-26T10:02:10+08:00'
    finally:
        store.engine.dispose()


def test_progress_cannot_overwrite_newer_analysis_or_completed_revision(tmp_path):
    store, service, task = create_task(tmp_path)
    try:
        first = TaskProgress(store, task['id'], 'local', 0, 'processing')
        first.update(stage='reading_excel')
        newer = TaskProgress(store, task['id'], 'local', 0, 'processing')
        newer.update(stage='ai_matching', total_batches=2)
        first.update(stage='assembling')
        current = service.get(task['id'], 'local')
        assert current['payload']['processing_progress']['run_id'] == newer.data['run_id']
        with store.engine.begin() as con:
            current = store.claim(con, task['id'], 'local', 0)
            current['status'] = 'mapping'
            newer.finish(current)
            store.save(con, current)
        newer.update(stage='ai_matching')
        final = service.get(task['id'], 'local')
        assert final['revision'] == 1 and final['payload']['processing_progress']['status'] == 'completed'
    finally:
        store.engine.dispose()


def test_failed_excel_parse_stops_the_processing_clock(tmp_path):
    store, service, task = create_task(tmp_path)
    try:
        (store.directory(task['id']) / 'source.xlsx').write_bytes(b'bad workbook')
        service.parse(task['id'], 'local')
        failed = service.get(task['id'], 'local')
        assert failed['status'] == 'failed'
        assert failed['payload']['processing_progress']['status'] == 'failed'
        assert failed['payload']['processing_progress']['finished_at']
    finally:
        store.engine.dispose()

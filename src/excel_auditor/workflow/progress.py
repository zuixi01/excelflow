"""Persist observable processing milestones without changing the task revision."""
from __future__ import annotations

from datetime import datetime

from .repository import identifier, now


class TaskProgress:
    def __init__(self, store, task_id, tenant, revision, expected_status):
        self.store, self.task_id, self.tenant = store, task_id, tenant
        self.revision, self.expected_status = revision, expected_status
        self.data = {'run_id': identifier('progress'), 'status': 'running', 'stage': 'reading_excel',
                     'started_at': now(), 'updated_at': now(), 'total_batches': 0,
                     'completed_batches': 0, 'failed_batches': 0, 'sheets': []}

    def update(self, event=None, **values):
        stamp = now()
        incoming = {**(event or {}), **values}
        if incoming.get('stage') == 'ai_matching' and not self.data.get('ai_started_at'):
            self.data['ai_started_at'] = stamp
        if incoming.get('stage') == 'assembling' and self.data.get('ai_started_at'):
            self.data.setdefault('ai_finished_at', stamp)
        self.data.update(incoming, updated_at=stamp)
        with self.store.engine.begin() as con:
            current = self.store.get(con, self.task_id, self.tenant, 'task')
            if current['revision'] != self.revision or current['status'] != self.expected_status:
                return
            previous = current['payload'].get('processing_progress')
            # Another reanalysis may have started while this run was waiting.
            if previous and previous.get('status') == 'running' and previous.get('run_id') != self.data['run_id']:
                if previous['started_at'] > self.data['started_at']:
                    return
            current['payload']['processing_progress'] = dict(self.data)
            self.store.save(con, current)

    def finish(self, current, *, failed=False):
        stamp = now()
        self.data.update(status='failed' if failed else 'completed', stage='failed' if failed else 'completed',
                         finished_at=stamp, updated_at=stamp)
        self.data['elapsed_seconds'] = max(0, (datetime.fromisoformat(stamp) - datetime.fromisoformat(self.data['started_at'])).total_seconds())
        if self.data.get('ai_started_at'):
            self.data.setdefault('ai_finished_at', stamp)
        current['payload']['processing_progress'] = dict(self.data)

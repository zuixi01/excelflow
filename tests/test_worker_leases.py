from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def test_worker_lease_rejects_stale_worker_and_requeues_expired_task(tmp_path):
    from excel_auditor.persistence import DatabaseRepository
    from excel_auditor.worker_control import WorkerControl

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'tasks.sqlite3'}")
    database.ensure_local_admin(username="admin", password="correct-password")
    job_id = "job_lease"
    database.create_job(job_id)
    control = WorkerControl(database, lease_seconds=1)
    claimed = control.start(job_id, "worker-a")
    assert claimed["status"] == "running"
    assert control.heartbeat(job_id, "worker-a") is True
    assert control.heartbeat(job_id, "worker-b") is False

    database.update_job({"job_id": job_id, "lease_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()})
    assert control.requeue_expired(now=datetime.now(UTC)) == [job_id]


def test_duplicate_claim_is_atomic_even_for_the_same_worker(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from excel_auditor.persistence import DatabaseRepository
    from excel_auditor.worker_control import WorkerControl
    database = DatabaseRepository(f"sqlite:///{tmp_path / 'atomic.sqlite3'}")
    database.create_job("job_atomic")
    control = WorkerControl(database)
    def claim(_):
        try:
            control.start("job_atomic", "same-worker")
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(4))) == 1

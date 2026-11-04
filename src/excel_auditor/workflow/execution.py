from __future__ import annotations

import logging
import os
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select, update

from .models import WorkflowError
from .repository import WorkflowStore, executions, objects

logger = logging.getLogger(__name__)


class ExecutionService:
    def __init__(self, store, *, clock=None, lease_seconds=None, max_attempts=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = lease_seconds if lease_seconds is not None else int(os.environ.get("EXCEL_AUDITOR_LEASE_SECONDS", "120"))
        self.max_attempts = max_attempts if max_attempts is not None else int(os.environ.get("EXCEL_AUDITOR_MAX_RETRIES", "3"))
        if self.lease_seconds <= 0 or self.max_attempts <= 0:
            raise ValueError("workflow lease and attempt limits must be positive")

    def stamp(self, seconds=0):
        return (self.clock() + timedelta(seconds=seconds)).isoformat()

    def claim(self, execution_id, worker):
        token = uuid4().hex
        with self.store.engine.begin() as con:
            from .capacity import can_run, serialize
            serialize(con, self.store.engine)
            candidate = con.execute(select(executions).where(executions.c.id == execution_id,
                executions.c.status.in_(['pending', 'queued']))).mappings().first()
            if candidate is None:
                return None
            candidate_obj = con.execute(select(objects).where(objects.c.id == candidate['object_id'])).mappings().first()
            if candidate_obj is not None and not can_run(con, self.store, dict(candidate_obj)):
                con.execute(update(executions).where(executions.c.id == execution_id).values(
                    status='pending', lease_until=None, available_at=self.stamp(5)))
                return None
            changed = con.execute(update(executions).where(
                executions.c.id == execution_id, executions.c.status.in_(["pending", "queued"]),
                executions.c.available_at <= self.stamp(), executions.c.attempts < self.max_attempts,
            ).values(status="running", token=token, worker=worker, attempts=executions.c.attempts + 1,
                     lease_until=self.stamp(self.lease_seconds), updated_at=self.stamp(), started_at=self.stamp()))
            if changed.rowcount != 1:
                return None
            row = dict(con.execute(select(executions).where(executions.c.id == execution_id)).mappings().one())
            obj = con.execute(select(objects).where(objects.c.id == row["object_id"], objects.c.tenant_id == row["tenant_id"])).mappings().first()
            expected_status = "learning" if row["operation"] == "learn" else "processing"
            if obj is None or obj["revision"] != row["revision"] or obj["status"] != expected_status:
                con.execute(update(executions).where(executions.c.id == execution_id).values(status="superseded", lease_until=None))
                return None
            return row

    def heartbeat(self, row):
        with self.store.engine.begin() as con:
            return con.execute(update(executions).where(
                executions.c.id == row["id"], executions.c.token == row["token"],
                executions.c.status == "running", executions.c.lease_until > self.stamp(),
            ).values(lease_until=self.stamp(self.lease_seconds), updated_at=self.stamp())).rowcount == 1

    def finish(self, row, error=None, *, retryable=False):
        with self.store.engine.begin() as con:
            changed = con.execute(update(executions).where(
                executions.c.id == row["id"], executions.c.token == row["token"],
                executions.c.status == "running", executions.c.lease_until > self.stamp(),
            ).values(status="pending" if error and retryable and row["attempts"] < self.max_attempts else "failed" if error else "succeeded",
                     lease_until=None, available_at=self.stamp(min(30, 2 ** row["attempts"])) if error and retryable else self.stamp(),
                     error=str(error)[:2000] if isinstance(error, WorkflowError) else "临时处理异常" if error else None,
                     updated_at=self.stamp(), finished_at=self.stamp()))
            if changed.rowcount != 1:
                return
            if error and (not retryable or row["attempts"] >= self.max_attempts):
                self._fail_object(con, row, str(error) if isinstance(error, WorkflowError) else "处理失败，请检查文件后重试。")

    def _fail_object(self, con, row, message):
        obj = con.execute(select(objects).where(objects.c.id == row["object_id"], objects.c.tenant_id == row["tenant_id"],
            objects.c.revision == row["revision"], objects.c.status.in_(["processing", "learning"])).with_for_update()).mappings().first()
        if obj:
            payload = dict(obj["payload"])
            payload["error"] = message
            payload['error_code'] = row.get('error_code')
            con.execute(update(objects).where(objects.c.id == obj["id"], objects.c.revision == row["revision"]).values(
                status="failed", payload=payload, updated_at=self.stamp()))

    def recover(self):
        recovered = 0
        with self.store.engine.begin() as con:
            rows = con.execute(select(executions).where(executions.c.status == "running", executions.c.lease_until <= self.stamp())).mappings().all()
            for row in rows:
                terminal = row["attempts"] >= self.max_attempts
                changed = con.execute(update(executions).where(executions.c.id == row["id"], executions.c.status == "running",
                    executions.c.token == row["token"], executions.c.lease_until <= self.stamp()).values(
                    status="failed" if terminal else "pending", token=None, worker=None, lease_until=None,
                    available_at=self.stamp(), updated_at=self.stamp(), error="Worker 失联，重试次数已用尽。" if terminal else "Worker 失联，等待重试。"))
                if changed.rowcount:
                    recovered += 1
                    if terminal:
                        self._fail_object(con, row, "Worker 失联，重试次数已用尽，请手动重试。")
        return recovered

    def dispatch(self, queue=None, *, object_id=None):
        self.recover()
        with self.store.engine.connect() as con:
            query = select(executions).where(executions.c.status.in_(["pending", "queued"]),
                executions.c.available_at <= self.stamp(),
                or_(executions.c.lease_until.is_(None), executions.c.lease_until <= self.stamp()))
            if object_id:
                query = query.where(executions.c.object_id == object_id)
            rows = [dict(row) for row in con.execute(query.order_by(executions.c.available_at).limit(100)).mappings()]
        dispatched = []
        for row in rows:
            if row["available_at"] > self.stamp():
                continue
            job_id = f"{row['id']}_{row['attempts'] + 1}"
            # A queued job can wait indefinitely while healthy. Missing/failed RQ jobs
            # are repaired after a short dispatch lease, using the same job identity.
            if row["status"] == "queued" and row["lease_until"] and row["lease_until"] > self.stamp():
                continue
            try:
                if queue is not None and hasattr(queue, "workflow_job_active") and queue.workflow_job_active(job_id):
                    with self.store.engine.begin() as con:
                        con.execute(update(executions).where(executions.c.id == row["id"], executions.c.status == "queued",
                            executions.c.attempts == row["attempts"]).values(lease_until=self.stamp(30)))
                    continue
                with self.store.engine.begin() as con:
                    changed = con.execute(update(executions).where(executions.c.id == row["id"], executions.c.status == row["status"],
                        executions.c.attempts == row["attempts"],
                        or_(executions.c.lease_until.is_(None), executions.c.lease_until <= self.stamp()),
                    ).values(status="queued", lease_until=self.stamp(30), updated_at=self.stamp()))
                if changed.rowcount != 1:
                    continue
                if queue is None:
                    dispatched.append(row["id"])
                else:
                    queue.enqueue_workflow(self.store.root, row["operation"], row["object_id"], row["tenant_id"],
                                           execution_id=row["id"], attempt=row["attempts"] + 1)
            except Exception:
                logger.warning("Workflow dispatch deferred: %s", row["id"], exc_info=True)
                # The durable outbox remains queued and will be retried after its lease.
        return dispatched


def is_retryable(error):
    import httpx
    return isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError)) or (
        isinstance(error, WorkflowError) and error.status in {429, 502, 503, 504}
        and error.code not in {"AI_INVALID_DRAFT", "AI_INVALID_MAPPING", "AI_INVALID_COMPARISON",
                               "AI_INVALID_RESPONSE", "AI_REQUEST_REJECTED", "AI_RESPONSE_TOO_LARGE", "EXECUTION_EXPIRED"})


def execute(store, execution_id):
    service = ExecutionService(store)
    row = service.claim(execution_id, f"{socket.gethostname()}:{os.getpid()}")
    if row is None:
        return
    store.execution = (row["id"], row["token"])
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(min(30, service.lease_seconds / 4)):
            try:
                if not service.heartbeat(row):
                    return
            except Exception:
                logger.warning("Workflow heartbeat failed: %s", row["id"], exc_info=True)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        from .tasks import TaskService
        from .templates import TemplateService
        from .operations import OperationService
        function = {"parse_task": TaskService(store).parse, "export": TaskService(store).generate,
                    "parse_template": TemplateService(store).parse, "learn": TemplateService(store).learn,
                    "maintenance_operation": OperationService(store).run}[row["operation"]]
        function(row["object_id"], row["tenant_id"])
        service.finish(row)
    except Exception as exc:
        row['error_code'] = getattr(exc, 'code', None)
        service.finish(row, exc, retryable=is_retryable(exc))
    finally:
        stopped.set()
        thread.join(timeout=5)
        store.execution = None


def open_store(root):
    from sqlalchemy import create_engine
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True) if os.environ.get("DATABASE_URL") else None
    return WorkflowStore(Path(root), engine)


def run_execution(root, execution_id):
    store = open_store(root)
    try:
        execute(store, execution_id)
    finally:
        store.engine.dispose()


def execute_isolated(store, execution_id):
    execute(WorkflowStore(store.root, store.engine), execution_id)


def run_recovery():
    from concurrent.futures import ThreadPoolExecutor
    from ..queueing import RedisJobQueue
    root = Path(os.environ.get("EXCEL_AUDITOR_DATA", "var")).resolve() / "workflow"
    store = open_store(root)
    queue = RedisJobQueue(os.environ["REDIS_URL"], queue_name="workflow") if os.environ.get("REDIS_URL") else None
    interval = max(0.2, float(os.environ.get('EXCEL_AUDITOR_RECOVERY_INTERVAL', '30')))
    stopped = threading.Event()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            while not stopped.is_set():
                try:
                    for execution_id in ExecutionService(store).dispatch(queue):
                        pool.submit(run_execution, str(root), execution_id)
                    (store.root / 'recovery.heartbeat').touch()
                except Exception:
                    logger.exception("Workflow recovery scan failed")
                stopped.wait(interval)
        finally:
            store.engine.dispose()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Run the independent workflow dispatcher/worker.')
    parser.add_argument('--data-root', help='Shared application data directory; defaults to EXCEL_AUDITOR_DATA.')
    arguments = parser.parse_args()
    if arguments.data_root:
        os.environ['EXCEL_AUDITOR_DATA'] = str(Path(arguments.data_root).resolve())
    run_recovery()

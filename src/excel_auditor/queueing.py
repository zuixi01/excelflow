from __future__ import annotations

from pathlib import Path
from typing import Any

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from rq import Queue

from .models import RuleSet


def select_queue(workload: str, *, estimated_bytes: int = 0) -> str:
    if workload == "workflow":
        return "workflow"
    if workload in {"managed_http", "network"}:
        return "network"
    if estimated_bytes >= 50 * 1024 * 1024 or workload in {"merchant", "heavy"}:
        return "heavy"
    return "normal"


class RedisJobQueue:
    def __init__(self, redis_url: str, queue_name: str = "excel-auditor") -> None:
        self.connection = Redis.from_url(redis_url, decode_responses=False, socket_connect_timeout=2, socket_timeout=5,
                                         retry=Retry(NoBackoff(), 0))
        self.queue = Queue(queue_name, connection=self.connection, default_timeout=900)
        self.queue_name = queue_name
        self._queues: dict[str, Queue] = {queue_name: self.queue}

    def _queue(self, queue_name: str) -> Queue:
        if queue_name not in {"normal", "heavy", "network", "workflow", "workflow-export", self.queue_name}:
            raise ValueError(f"unsupported queue: {queue_name}")
        selected = self._queues.get(queue_name)
        if selected is None:
            selected = Queue(queue_name, connection=self.connection, default_timeout=900)
            self._queues[queue_name] = selected
        return selected

    def enqueue_task(self, task_id: str, queue_name: str, payload: dict[str, Any], *, timeout: int = 900) -> None:
        selected = self.queue if queue_name == "normal" else self._queue(queue_name)
        selected.enqueue(
            "excel_auditor.worker.run_task",
            task_id,
            payload,
            job_id=f"rq_task_{task_id}",
            job_timeout=timeout,
            result_ttl=3600,
            failure_ttl=86400,
        )

    def workflow_job_active(self, job_id: str) -> bool:
        from rq.job import Job
        from rq.exceptions import NoSuchJobError
        try:
            job = Job.fetch(job_id, connection=self.connection)
            status = job.get_status(refresh=True)
            if status == "started":
                from datetime import UTC, datetime, timedelta
                heartbeat = job.last_heartbeat or job.started_at
                if heartbeat is None or heartbeat < datetime.now(UTC) - timedelta(seconds=120):
                    return False
            return status in {"queued", "started", "deferred", "scheduled"}
        except NoSuchJobError:
            return False

    def enqueue_workflow(self, data_root: Path, operation: str, object_id: str, tenant: str,
                         *, execution_id: str | None = None, attempt: int = 1) -> None:
        queue_name = "workflow-export" if operation in {"export", "maintenance_operation"} else "workflow"
        if execution_id:
            self._queue(queue_name).enqueue(
                "excel_auditor.workflow.execution.run_execution", str(data_root), execution_id,
                job_id=f"{execution_id}_{attempt}", job_timeout=900, result_ttl=3600, failure_ttl=86400,
            )
            return
        self._queue(queue_name).enqueue(
            "excel_auditor.workflow.routes.run_background",
            str(data_root), operation, object_id, tenant,
            job_timeout=300, result_ttl=3600, failure_ttl=86400,
        )

    def enqueue(self, data_root: Path, job_id: str, excel_path: Path, standard_path: Path | None, rules: RuleSet, parameters: dict[str, Any], queue_name: str = "normal") -> None:
        selected = self.queue if queue_name == "normal" else self._queue(queue_name)
        selected.enqueue(
            "excel_auditor.worker.run_job",
            str(data_root),
            job_id,
            str(excel_path),
            str(standard_path) if standard_path else None,
            rules.model_dump(mode="json"),
            parameters,
            job_id="rq_" + job_id,
            job_timeout=rules.workbook.processing_timeout_seconds,
            result_ttl=3600,
            failure_ttl=86400,
        )

    def enqueue_product(
        self,
        data_root: Path,
        job_id: str,
        excel_path: Path,
        rules: RuleSet,
        tenant_id: str,
        actor_id: str,
    ) -> None:
        self.queue.enqueue(
            "excel_auditor.worker.run_product_job",
            str(data_root),
            job_id,
            str(excel_path),
            rules.model_dump(mode="json"),
            tenant_id,
            actor_id,
            job_id="rq_product_" + job_id,
            job_timeout=rules.workbook.processing_timeout_seconds,
            result_ttl=3600,
            failure_ttl=86400,
        )

    def enqueue_product_revision(
        self,
        data_root: Path,
        job_id: str,
        rules: RuleSet,
        tenant_id: str,
        actor_id: str,
        parent_revision_number: int,
    ) -> None:
        self.queue.enqueue(
            "excel_auditor.worker.run_product_revision",
            str(data_root),
            job_id,
            rules.model_dump(mode="json"),
            tenant_id,
            actor_id,
            job_id=f"rq_product_revision_{job_id}_{parent_revision_number}",
            job_timeout=rules.workbook.processing_timeout_seconds,
            result_ttl=3600,
            failure_ttl=86400,
        )

    def enqueue_merchant_reconciliation(
        self,
        data_root: Path,
        job_id: str,
        excel_path: Path,
        platform_path: Path | None,
        name_match_threshold: float,
    ) -> None:
        self.queue.enqueue(
            "excel_auditor.worker.run_merchant_reconciliation_job",
            str(data_root),
            job_id,
            str(excel_path),
            str(platform_path) if platform_path else None,
            name_match_threshold,
            job_id="rq_merchant_" + job_id,
            job_timeout=900,
            result_ttl=3600,
            failure_ttl=86400,
        )

    def enqueue_field_reconciliation(
        self, data_root: Path, job_id: str, excel_path: Path, connection_id: str, scope: dict[str, Any], tenant_id: str,
        revision: int = 0,
    ) -> None:
        self.queue.enqueue(
            "excel_auditor.worker.run_field_reconciliation_job",
            str(data_root), job_id, str(excel_path), connection_id, scope, tenant_id,
            job_id=f"rq_field_{job_id}_{revision}", job_timeout=900, result_ttl=3600, failure_ttl=86400,
        )

    def ping(self) -> None:
        self.connection.ping()

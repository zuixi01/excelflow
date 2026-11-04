from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from .persistence import ComparisonJobRow, DatabaseRepository


class WorkerControl:
    """Database compare-and-set state transitions for concurrent workers."""

    def __init__(self, database: DatabaseRepository, *, lease_seconds: int = 120, max_attempts: int = 3) -> None:
        self.database = database
        self.lease_seconds = max(5, lease_seconds)
        self.max_attempts = max(1, max_attempts)

    def start(self, task_id: str, worker_id: str) -> dict[str, object]:
        now = datetime.now(UTC)
        with Session(self.database.engine) as session, session.begin():
            changed = session.execute(update(ComparisonJobRow).where(
                ComparisonJobRow.id == task_id, ComparisonJobRow.status == "queued",
                ComparisonJobRow.attempt < self.max_attempts,
            ).values(status="running", attempt=ComparisonJobRow.attempt + 1, worker_id=worker_id,
                     lease_expires_at=now + timedelta(seconds=self.lease_seconds)))
            row = session.get(ComparisonJobRow, task_id)
            if row is None:
                raise FileNotFoundError("task not found")
            if changed.rowcount != 1:
                raise ValueError("task is not runnable or its lease is already held")
            return self._payload(row)

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        with Session(self.database.engine) as session, session.begin():
            now = datetime.now(UTC)
            return session.execute(update(ComparisonJobRow).where(
                ComparisonJobRow.id == task_id, ComparisonJobRow.status == "running", ComparisonJobRow.worker_id == worker_id,
                ComparisonJobRow.lease_expires_at > now,
            ).values(lease_expires_at=now + timedelta(seconds=self.lease_seconds))).rowcount == 1

    def complete(self, task_id: str, worker_id: str) -> bool:
        return self._finish(task_id, worker_id, "completed")

    def fail(self, task_id: str, worker_id: str, *, terminal: bool = False) -> bool:
        target = "failed" if terminal else case((ComparisonJobRow.attempt >= self.max_attempts, "failed"), else_="queued")
        return self._finish(task_id, worker_id, target)

    def requeue_expired(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(UTC)
        with Session(self.database.engine) as session, session.begin():
            rows = session.scalars(select(ComparisonJobRow).where(
                ComparisonJobRow.status == "running",
                ComparisonJobRow.lease_expires_at.is_not(None),
                ComparisonJobRow.lease_expires_at < current,
            )).all()
            ids = []
            for row in rows:
                changed = session.execute(update(ComparisonJobRow).where(
                    ComparisonJobRow.id == row.id, ComparisonJobRow.status == "running",
                    ComparisonJobRow.lease_expires_at < current,
                ).values(status="failed" if row.attempt >= self.max_attempts else "queued", worker_id=None, lease_expires_at=None)
                    .execution_options(synchronize_session=False))
                if changed.rowcount:
                    ids.append(row.id)
            return ids

    def _finish(self, task_id: str, worker_id: str, status: str) -> bool:
        with Session(self.database.engine) as session, session.begin():
            return session.execute(update(ComparisonJobRow).where(
                ComparisonJobRow.id == task_id, ComparisonJobRow.status == "running", ComparisonJobRow.worker_id == worker_id,
                ComparisonJobRow.lease_expires_at > datetime.now(UTC),
            ).values(status=status, worker_id=None, lease_expires_at=None)).rowcount == 1

    @staticmethod
    def _payload(row: ComparisonJobRow) -> dict[str, object]:
        return {
            "job_id": row.id,
            "status": row.status,
            "attempt": row.attempt,
            "worker_id": row.worker_id,
            "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
        }

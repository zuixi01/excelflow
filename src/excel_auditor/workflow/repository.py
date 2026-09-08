from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Column, ForeignKey, Index, Integer, JSON, MetaData, String, Table, Text, create_engine, insert, select, update
from sqlalchemy.engine import Connection, Engine

from .models import WorkflowError


metadata = MetaData()
objects = Table("workflow_objects", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("revision", Integer, nullable=False, default=0),
    Column("payload", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
Index("ix_workflow_objects_tenant_kind", objects.c.tenant_id, objects.c.kind, objects.c.updated_at)
records = Table("workflow_records", metadata,
    Column("task_id", String(64), ForeignKey("workflow_objects.id", ondelete="CASCADE"), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("ordinal", Integer, nullable=False),
    Column("values_json", JSON, nullable=False),
    Column("original", JSON, nullable=False),
    Column("source", JSON, nullable=False),
    Column("issues", JSON, nullable=False),
    Column("issue_count", Integer, nullable=False, default=0),
    Column("search_text", Text, nullable=False),
)
Index("ix_workflow_records_task_ordinal", records.c.task_id, records.c.ordinal)
revisions = Table("workflow_revisions", metadata,
    Column("task_id", String(64), ForeignKey("workflow_objects.id", ondelete="CASCADE"), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("actor", String(200), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("detail", JSON, nullable=False),
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


class WorkflowStore:
    def __init__(self, root: Path, engine: Engine | None = None):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.engine = engine or create_engine(f"sqlite:///{(root / 'workflow.sqlite3').as_posix()}", connect_args={"timeout": 30})
        if engine is None:
            metadata.create_all(self.engine)

    def directory(self, object_id: str) -> Path:
        if not object_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in object_id):
            raise WorkflowError("记录不存在", 404)
        path = self.root / "files" / object_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get(self, connection: Connection, object_id: str, tenant: str, kind: str | None = None) -> dict:
        query = select(objects).where(objects.c.id == object_id, objects.c.tenant_id == tenant)
        if kind:
            query = query.where(objects.c.kind == kind)
        row = connection.execute(query).mappings().first()
        if row is None:
            raise WorkflowError("记录不存在或无权访问", 404, "NOT_FOUND")
        return dict(row)

    def create(self, connection: Connection, tenant: str, kind: str, payload: dict, status: str = "draft", object_id: str | None = None) -> dict:
        record = dict(id=object_id or identifier(kind), tenant_id=tenant, kind=kind, status=status,
                      revision=0, payload=payload, created_at=now(), updated_at=now())
        connection.execute(insert(objects).values(**record))
        return record

    def claim(self, connection: Connection, object_id: str, tenant: str, base_revision: int, kind: str = "task") -> dict:
        changed = connection.execute(update(objects).where(
            objects.c.id == object_id, objects.c.tenant_id == tenant,
            objects.c.kind == kind, objects.c.revision == base_revision,
        ).values(revision=base_revision + 1, updated_at=now()))
        if changed.rowcount != 1:
            self.get(connection, object_id, tenant, kind)
            raise WorkflowError("数据已有新版本，请刷新后重新应用修改；本次修改未覆盖新数据。", 409, "REVISION_CONFLICT")
        return self.get(connection, object_id, tenant, kind)

    def save(self, connection: Connection, obj: dict) -> None:
        connection.execute(update(objects).where(objects.c.id == obj["id"], objects.c.tenant_id == obj["tenant_id"]).values(
            payload=obj["payload"], status=obj["status"], updated_at=now(),
        ))

    def audit(self, connection: Connection, obj: dict, actor: str, detail: dict) -> None:
        connection.execute(insert(revisions).values(task_id=obj["id"], revision=obj["revision"], actor=actor, created_at=now(), detail=detail))

    @staticmethod
    def public(obj: dict) -> dict:
        return {key: value for key, value in obj.items() if key not in {"tenant_id", "kind"}}

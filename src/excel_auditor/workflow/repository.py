from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Column, ForeignKey, Index, Integer, JSON, MetaData, String, Table, Text, create_engine, insert, select, update
from sqlalchemy.engine import Connection, Engine

from .models import WorkflowError


metadata = MetaData()
capacity_locks = Table("workflow_capacity_locks", metadata,
    Column("name", String(64), primary_key=True), Column("version", Integer, nullable=False, default=0))
resource_leases = Table("workflow_resource_leases", metadata,
    Column("token", String(64), primary_key=True), Column("resource", String(32), nullable=False),
    Column("expires_at", String(40), nullable=False))
Index("ix_workflow_resource_expiry", resource_leases.c.resource, resource_leases.c.expires_at)
objects = Table("workflow_objects", metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("revision", Integer, nullable=False, default=0),
    Column("payload", JSON, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("creator_id", String(200)),
    Column("parent_task_id", String(64)),
    Column("search_summary", Text, nullable=False, server_default=""),
)
Index("ix_workflow_objects_tenant_kind", objects.c.tenant_id, objects.c.kind, objects.c.updated_at)
Index("ix_workflow_objects_creator_page", objects.c.tenant_id, objects.c.kind, objects.c.creator_id, objects.c.updated_at, objects.c.id)
Index("ix_workflow_objects_status_page", objects.c.tenant_id, objects.c.kind, objects.c.status, objects.c.updated_at, objects.c.id)
Index("ix_workflow_objects_parent_task", objects.c.tenant_id, objects.c.kind, objects.c.parent_task_id, objects.c.created_at, objects.c.id)
executions = Table("workflow_executions", metadata,
    Column("id", String(64), primary_key=True),
    Column("object_id", String(64), nullable=False),
    Column("tenant_id", String(200), nullable=False),
    Column("operation", String(32), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("worker", String(200)),
    Column("token", String(64)),
    Column("lease_until", String(40)),
    Column("available_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("error", Text),
    Column("created_at", String(40), nullable=False),
    Column("started_at", String(40)),
    Column("finished_at", String(40)),
)
Index("ix_workflow_executions_recovery", executions.c.status, executions.c.available_at, executions.c.lease_until)
Index("ix_workflow_executions_object", executions.c.tenant_id, executions.c.object_id, executions.c.revision)
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
        self.execution = None
        if engine is None:
            # Local files predate Alembic; upgrade only additive columns in place.
            from sqlalchemy import inspect, text
            if inspect(self.engine).has_table("workflow_objects"):
                columns = {column["name"] for column in inspect(self.engine).get_columns("workflow_objects")}
                with self.engine.begin() as con:
                    if "creator_id" not in columns:
                        con.execute(text("ALTER TABLE workflow_objects ADD COLUMN creator_id VARCHAR(200)"))
                    if "parent_task_id" not in columns:
                        con.execute(text("ALTER TABLE workflow_objects ADD COLUMN parent_task_id VARCHAR(64)"))
                    if "search_summary" not in columns:
                        con.execute(text("ALTER TABLE workflow_objects ADD COLUMN search_summary TEXT NOT NULL DEFAULT ''"))
                if not {"creator_id", "parent_task_id", "search_summary"} <= columns:
                    self.backfill_summaries()
            metadata.create_all(self.engine)
            for index in objects.indexes:
                index.create(self.engine, checkfirst=True)

    @staticmethod
    def summary(payload):
        return {"creator_id": payload.get("actor"), "parent_task_id": payload.get("task_id"),
                "search_summary": (str(payload.get("name") or "") + str(payload.get("filename") or "")).casefold()}

    def backfill_summaries(self):
        cursor = ""
        while True:
            with self.engine.begin() as con:
                batch = con.execute(select(objects.c.id, objects.c.payload).where(objects.c.id > cursor).order_by(objects.c.id).limit(500)).mappings().all()
                if not batch:
                    return
                for row in batch:
                    con.execute(update(objects).where(objects.c.id == row["id"]).values(**self.summary(row["payload"])))
                cursor = batch[-1]["id"]

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
        self.fence(connection)
        if object_id and connection.execute(select(executions.c.id).where(
            executions.c.tenant_id == tenant, executions.c.object_id == object_id,
        ).limit(1)).first():
            raise WorkflowError("原请求的处理记录已删除或已存在，请使用新的请求标识重新提交。", 409, "REQUEST_RETIRED")
        record = dict(id=object_id or identifier(kind), tenant_id=tenant, kind=kind, status=status,
                      revision=0, payload=payload, created_at=now(), updated_at=now(), **self.summary(payload))
        connection.execute(insert(objects).values(**record))
        self.schedule(connection, record)
        return record

    def schedule(self, connection, obj):
        operation = "learn" if obj["status"] == "learning" and obj["kind"] == "template" else {
            "task": "parse_task", "template": "parse_template", "export": "export", "operation": "maintenance_operation",
        }.get(obj["kind"]) if obj["status"] == "processing" else None
        if operation is None:
            return
        if self.execution is not None:
            running_object = connection.execute(select(executions.c.object_id).where(executions.c.id == self.execution[0])).scalar_one_or_none()
            if running_object == obj['id']:
                return
        from .capacity import admit
        admit(connection, self, obj, operation)
        from ..persistence import dialect_insert
        execution_id = "exec_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{obj['id']}:{obj['revision']}:{operation}").hex
        connection.execute(dialect_insert(self.engine, executions).values(
            id=execution_id, object_id=obj["id"], tenant_id=obj["tenant_id"], operation=operation,
            revision=obj["revision"], status="pending", attempts=0, available_at=now(), updated_at=now(), created_at=now(),
        ).on_conflict_do_nothing(index_elements=["id"]))

    def fence(self, connection):
        if self.execution is None:
            return
        execution_id, token = self.execution
        changed = connection.execute(update(executions).where(
            executions.c.id == execution_id, executions.c.token == token,
            executions.c.status == "running", executions.c.lease_until > now(),
        ).values(updated_at=now()))
        if changed.rowcount != 1:
            raise WorkflowError("执行租约已失效，已放弃旧执行结果。", 409, "EXECUTION_EXPIRED")

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
        self.fence(connection)
        changed = connection.execute(update(objects).where(objects.c.id == obj["id"], objects.c.tenant_id == obj["tenant_id"],
            objects.c.revision == obj["revision"]).values(
            payload=obj["payload"], status=obj["status"], updated_at=now(), **self.summary(obj["payload"]),
        ))
        if changed.rowcount != 1:
            raise WorkflowError("数据已有新版本，请刷新。", 409, "REVISION_CONFLICT")
        self.schedule(connection, obj)

    def audit(self, connection: Connection, obj: dict, actor: str, detail: dict) -> None:
        connection.execute(insert(revisions).values(task_id=obj["id"], revision=obj["revision"], actor=actor, created_at=now(), detail=detail))

    @staticmethod
    def public(obj: dict) -> dict:
        return {key: value for key, value in obj.items() if key not in {"tenant_id", "kind", "creator_id", "parent_task_id", "search_summary"}}

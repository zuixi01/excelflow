"""Database-coordinated capacity. Locks/counts share the caller's transaction."""
from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from ..persistence import dialect_insert
from .models import WorkflowError
from .repository import capacity_locks, executions, objects, resource_leases


def limit(name, default):
    value = int(os.environ.get('EXCEL_AUDITOR_' + name, str(default)))
    if value < 1:
        raise ValueError(name + ' must be positive')
    return value


def stamp(seconds=0):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def serialize(con, engine, name='workflow'):
    con.execute(dialect_insert(engine, capacity_locks).values(name=name, version=0).on_conflict_do_nothing(index_elements=['name']))
    con.execute(update(capacity_locks).where(capacity_locks.c.name == name).values(version=capacity_locks.c.version + 1))


def owner(con, obj):
    actor = obj.get('creator_id') or obj['payload'].get('actor')
    if not actor and obj['payload'].get('task_id'):
        actor = con.execute(select(objects.c.creator_id).where(objects.c.id == obj['payload']['task_id'],
            objects.c.tenant_id == obj['tenant_id'])).scalar_one_or_none()
    return actor or 'service'


def active_query():
    parent = objects.alias('parent_capacity')
    source = executions.join(objects, objects.c.id == executions.c.object_id).outerjoin(parent, parent.c.id == objects.c.parent_task_id)
    actor = func.coalesce(objects.c.creator_id, parent.c.creator_id, 'service')
    return select(func.count()).select_from(source).where(executions.c.revision == objects.c.revision,
        objects.c.status.in_(['processing', 'learning'])), actor


def admit(con, store, obj, operation):
    serialize(con, store.engine)
    # Scheduling the same outbox entry again must not consume another slot.
    if con.execute(select(executions.c.id).where(executions.c.object_id == obj['id'],
            executions.c.revision == obj['revision'], executions.c.operation == operation)).first():
        return
    query, actor = active_query()
    query = query.where(executions.c.status.in_(['pending', 'queued', 'running']))
    if store.execution is not None:
        # A durable operation can hand its admitted slot to a child export.
        query = query.where(executions.c.id != store.execution[0])
    if con.execute(query).scalar_one() >= limit('WORKFLOW_MAX_WAITING', 50) + limit('WORKFLOW_MAX_RUNNING', 2):
        raise WorkflowError('处理队列已满，请稍后提交。', 429, 'WORKFLOW_CAPACITY_FULL')
    if con.execute(query.where(objects.c.tenant_id == obj['tenant_id'], actor == owner(con, obj))).scalar_one() >= limit('WORKFLOW_USER_WAITING', 3) + limit('WORKFLOW_USER_RUNNING', 1):
        raise WorkflowError('您的待处理任务已达上限，请等待已有任务完成。', 429, 'USER_CAPACITY_FULL')


def can_run(con, store, obj):
    serialize(con, store.engine)
    query, actor = active_query()
    query = query.where(executions.c.status == 'running', executions.c.lease_until > stamp())
    interactive = con.execute(select(func.count()).select_from(resource_leases).where(
        resource_leases.c.resource == 'interactive', resource_leases.c.expires_at > stamp())).scalar_one()
    return (con.execute(query).scalar_one() + interactive < limit('WORKFLOW_MAX_RUNNING', 2)
        and con.execute(query.where(objects.c.tenant_id == obj['tenant_id'], actor == owner(con, obj))).scalar_one() < limit('WORKFLOW_USER_RUNNING', 1))


def disk_check(root):
    reserve = limit('MIN_FREE_DISK_MIB', 2048) * 1024 * 1024
    if shutil.disk_usage(root).free < reserve:
        raise WorkflowError('服务器剩余空间不足，已暂停接收新处理任务，请联系管理员。', 507, 'DISK_PRESSURE')


class ResourceLease:
    def __init__(self, store, resource, maximum, seconds=180):
        self.store, self.resource, self.maximum, self.seconds = store, resource, maximum, seconds
        self.token, self.stop = uuid4().hex, threading.Event()
        self.thread = None
        self.lost = False

    def acquire(self):
        with self.store.engine.begin() as con:
            if self.resource == 'interactive':
                serialize(con, self.store.engine)
            serialize(con, self.store.engine, 'resource:' + self.resource)
            con.execute(delete(resource_leases).where(resource_leases.c.expires_at <= stamp()))
            count = con.execute(select(func.count()).select_from(resource_leases).where(resource_leases.c.resource == self.resource)).scalar_one()
            if self.resource == 'interactive':
                query, _ = active_query()
                running = con.execute(query.where(executions.c.status == 'running', executions.c.lease_until > stamp())).scalar_one()
                if count + running >= limit('WORKFLOW_MAX_RUNNING', 2):
                    raise WorkflowError('服务器正在处理其他文件，请稍后重试。', 429, 'RESOURCE_BUSY')
            if count >= self.maximum:
                raise WorkflowError('当前处理名额已满，请稍后重试。', 429, 'RESOURCE_BUSY')
            con.execute(resource_leases.insert().values(token=self.token, resource=self.resource, expires_at=stamp(self.seconds)))
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def _heartbeat(self):
        while not self.stop.wait(min(30, self.seconds / 3)):
            try:
                with self.store.engine.begin() as con:
                    if con.execute(update(resource_leases).where(resource_leases.c.token == self.token,
                            resource_leases.c.expires_at > stamp()).values(expires_at=stamp(self.seconds))).rowcount != 1:
                        self.lost = True
                        return
            except Exception:
                self.lost = True
                return

    def check(self):
        if self.lost:
            raise WorkflowError('资源租约失效，请稍后重试。', 503, 'RESOURCE_LEASE_LOST')

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        with self.store.engine.begin() as con:
            con.execute(delete(resource_leases).where(resource_leases.c.token == self.token))


@contextmanager
def resource(store, name, maximum):
    lease = ResourceLease(store, name, maximum).acquire()
    try:
        yield lease
        lease.check()
    finally:
        lease.close()

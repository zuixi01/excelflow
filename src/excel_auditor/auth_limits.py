from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import case, delete, select

from .persistence import AuthRateLimitRow, dialect_insert


class LoginLimiter:
    def __init__(self, database, *, seconds: int):
        self.database = database
        self.seconds = max(1, seconds)
        url = os.environ.get("REDIS_URL")
        self.redis = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) if url else None

    @staticmethod
    def key(value: str) -> str:
        return "excel-auditor:login:" + hashlib.sha256(value.encode()).hexdigest()

    def count(self, value: str, now: datetime) -> int:
        key = self.key(value)
        if self.redis:
            try:
                return int(self.redis.get(key) or 0)
            except RedisError:
                pass
        table = AuthRateLimitRow.__table__
        with self.database.engine.connect() as con:
            return con.execute(select(table.c.failures).where(table.c.key == key, table.c.expires_at > now)).scalar_one_or_none() or 0

    def increment(self, value: str, now: datetime) -> int:
        key = self.key(value)
        if self.redis:
            try:
                return int(self.redis.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]); end; return n", 1, key, self.seconds))
            except RedisError:
                pass
        table = AuthRateLimitRow.__table__
        expiry = now + timedelta(seconds=self.seconds)
        statement = dialect_insert(self.database.engine, table).values(key=key, failures=1, expires_at=expiry)
        statement = statement.on_conflict_do_update(index_elements=[table.c.key], set_={
            "failures": case((table.c.expires_at <= now, 1), else_=table.c.failures + 1),
            "expires_at": case((table.c.expires_at <= now, expiry), else_=table.c.expires_at),
        }).returning(table.c.failures)
        with self.database.engine.begin() as con:
            con.execute(delete(table).where(table.c.expires_at <= now))
            return con.execute(statement).scalar_one()

    def clear(self, value: str) -> None:
        key = self.key(value)
        if self.redis:
            try:
                self.redis.delete(key)
            except RedisError:
                pass
        with self.database.engine.begin() as con:
            con.execute(delete(AuthRateLimitRow).where(AuthRateLimitRow.key == key))

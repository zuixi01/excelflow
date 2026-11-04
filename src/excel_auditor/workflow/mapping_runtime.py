from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from functools import lru_cache

from redis import Redis
from redis.exceptions import RedisError

from .ai import AIClient
from .models import WorkflowError


logger = logging.getLogger(__name__)


def _concurrency(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if not 1 <= value <= 32:
        raise ValueError(f"{name} must be between 1 and 32")
    return value


class MappingRuntime:
    """Bound model concurrency and share header-only suggestions between tasks."""

    def __init__(self, *, concurrency: int = 4, task_concurrency: int = 2,
                 cache_size: int = 256, cache_ttl: float = 300):
        self.executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="field-mapping")
        self.task_concurrency = min(concurrency, task_concurrency)
        self.cache_size = cache_size
        self.cache_ttl = cache_ttl
        self.lock = threading.Lock()
        self.entries: OrderedDict[str, tuple[Future, float]] = OrderedDict()

    def _completed(self, key: str, future: Future) -> None:
        with self.lock:
            if key not in self.entries or self.entries[key][0] is not future:
                return
            if future.cancelled() or future.exception() is not None:
                del self.entries[key]
            else:
                self.entries[key] = future, time.monotonic() + self.cache_ttl

    def submit(self, ai: AIClient, fields: list[dict], columns: list[dict], *, namespace: str, use_cache=True) -> Future:
        key = hashlib.sha256(json.dumps([namespace, fields, columns], ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not use_cache:
            return self.executor.submit(self._compute, ai, fields, columns, key, use_cache=False)
        with self.lock:
            for expired in [key for key, (future, deadline) in self.entries.items()
                            if future.done() and deadline <= time.monotonic()]:
                del self.entries[expired]
            if key in self.entries:
                previous = self.entries[key][0]
                if previous.done() and (previous.cancelled() or previous.exception() is not None):
                    del self.entries[key]
                else:
                    self.entries.move_to_end(key)
                    return previous
            # Keep pending requests shared; evict only completed suggestions.
            while len(self.entries) >= self.cache_size:
                completed = next((key for key, (future, _) in self.entries.items() if future.done()), None)
                if completed is None:
                    break
                del self.entries[completed]
            future = self.executor.submit(self._compute, ai, fields, columns, key)
            cached = len(self.entries) < self.cache_size
            if cached:
                self.entries[key] = future, float("inf")
        if cached:
            future.add_done_callback(lambda result: self._completed(key, result))
        return future

    def _compute(self, ai: AIClient, fields: list[dict], columns: list[dict], key: str, *, use_cache=True) -> list[dict]:
        def request():
            # A model may occasionally return malformed JSON for the same valid
            # headers. Retry once; authentication/configuration failures remain terminal.
            for attempt in range(1, 3):
                started = time.monotonic()
                try:
                    result = ai.reconcile_field_mappings(fields, columns, multiple_targets=True)
                    logger.info("mapping_batch_completed batch=%s attempt=%d fields=%d columns=%d elapsed=%.2f",
                                key[:12], attempt, len(fields), len(columns), time.monotonic() - started)
                    return result
                except Exception as exc:
                    transient = isinstance(exc, WorkflowError) and exc.code in {
                        "AI_TIMEOUT", "AI_SERVICE_ERROR", "AI_INVALID_MAPPING"}
                    retry = transient and attempt < 2
                    # Exception text, request headers and upstream bodies may contain secrets.
                    logger.warning("mapping_batch_failed batch=%s attempt=%d error=%s cause=%s retry=%s elapsed=%.2f",
                                   key[:12], attempt, exc.code if isinstance(exc, WorkflowError) else "AI_ANALYSIS_ERROR",
                                   type(exc.__cause__ or exc).__name__, retry, time.monotonic() - started)
                    if not retry:
                        raise
                    time.sleep(1)

        redis_url = os.environ.get("REDIS_URL")
        if not use_cache or not redis_url or ai.transport is not None:
            return request()
        cache = _shared_cache(redis_url)
        cache_key = "excel-auditor:mapping:v1:" + key
        # Cover both generation attempts, their connection/write budgets and backoff.
        lock = cache.lock(cache_key + ":lock", timeout=360, blocking_timeout=330)
        acquired = False
        try:
            cached = cache.get(cache_key)
            if cached is not None:
                return json.loads(cached)
            acquired = lock.acquire()
            if acquired:
                cached = cache.get(cache_key)
                if cached is not None:
                    return json.loads(cached)
            result = request()
            try:
                cache.set(cache_key, json.dumps(result, ensure_ascii=False), ex=max(1, int(self.cache_ttl)))
            except RedisError:
                pass
            return result
        except (RedisError, json.JSONDecodeError):
            return request()
        finally:
            if acquired:
                try:
                    lock.release()
                except RedisError:
                    pass

    def analyze(self, ai: AIClient, batches: list[tuple[list[dict], list[dict]]], *, namespace: str, progress=None, use_cache=True) -> list:
        remaining = deque(enumerate(batches))
        pending: dict[Future, list[int]] = {}
        results: list = [None] * len(batches)
        states = ['waiting'] * len(batches)
        def report():
            if progress:
                progress(list(states))
        report()
        while remaining or pending:
            # A large workbook cannot fill the executor ahead of every other task.
            while remaining and len(pending) < self.task_concurrency:
                index, (fields, columns) = remaining.popleft()
                future = self.submit(ai, fields, columns, namespace=namespace, use_cache=use_cache)
                pending.setdefault(future, []).append(index)
                states[index] = 'running'
            report()
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                try:
                    result = future.result()
                except Exception as exc:
                    result = exc
                for index in pending.pop(future):
                    results[index] = result
                    states[index] = 'failed' if isinstance(result, Exception) else 'completed'
            report()
        return results

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)


def mapping_namespace(ai: AIClient, settings: dict) -> str:
    revision = hashlib.sha256(ai.path.read_bytes()).hexdigest() if ai.path.exists() else ""
    return json.dumps([str(ai.path.resolve()), revision, settings], sort_keys=True)


@lru_cache(maxsize=4)
def _shared_cache(redis_url: str) -> Redis:
    return Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=2)


@lru_cache(maxsize=1)
def mapping_runtime() -> MappingRuntime:
    return MappingRuntime(
        concurrency=_concurrency("EXCEL_AUDITOR_MAPPING_CONCURRENCY", 4),
        task_concurrency=_concurrency("EXCEL_AUDITOR_MAPPING_TASK_CONCURRENCY", 2),
    )

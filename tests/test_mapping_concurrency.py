from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from excel_auditor.workflow import mapping_runtime as runtime_module
from excel_auditor.workflow.mapping_runtime import MappingRuntime, mapping_namespace


class Model:
    transport = None

    def __init__(self, callback):
        self.callback = callback

    def reconcile_field_mappings(self, fields, columns, **kwargs):
        assert kwargs == {"multiple_targets": True}
        return self.callback(fields, columns)


def batch(name):
    return [{"id": name}], [{"column": 1, "header": name}]


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    instance = MappingRuntime(concurrency=4, task_concurrency=2)
    yield instance
    instance.shutdown()


def test_thirty_simultaneous_tasks_share_identical_inflight_header_requests(runtime):
    barrier = threading.Barrier(30)
    calls = []
    model = Model(lambda fields, columns: calls.append(fields) or [{"field_id": fields[0]["id"]}])

    def submit(_):
        barrier.wait(timeout=10)
        return runtime.analyze(model, [batch("sku")], namespace="workspace-a")

    with ThreadPoolExecutor(max_workers=30) as callers:
        results = list(callers.map(submit, range(30)))
    assert calls == [[{"id": "sku"}]]
    assert all(result == [[{"field_id": "sku"}]] for result in results)


def test_thirty_distinct_tasks_run_in_parallel_with_a_global_bound(runtime):
    guard = threading.Lock()
    saturated, release = threading.Event(), threading.Event()
    active, peak, calls = 0, 0, 0

    def respond(fields, columns):
        nonlocal active, peak, calls
        with guard:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 4:
                saturated.set()
        try:
            assert release.wait(timeout=10)
            return [{"field_id": fields[0]["id"]}]
        finally:
            with guard:
                active -= 1

    model = Model(respond)
    with ThreadPoolExecutor(max_workers=30) as callers:
        pending = [callers.submit(runtime.analyze, model, [batch(f"{task}-{part}") for part in range(2)], namespace="workspace")
                   for task in range(30)]
        try:
            assert saturated.wait(timeout=10)
        finally:
            release.set()
        results = [future.result(timeout=10) for future in pending]
    assert peak == 4 and calls == 60
    assert all(result == [[{"field_id": f"{task}-0"}], [{"field_id": f"{task}-1"}]] for task, result in enumerate(results))


def test_large_task_leaves_capacity_for_another_task(runtime):
    first_started, second_started, release = threading.Event(), threading.Event(), threading.Event()
    calls = []
    guard = threading.Lock()

    def respond(fields, columns):
        with guard:
            calls.append(fields[0]["id"])
            if len(calls) == 2:
                first_started.set()
            if fields[0]["id"] == "small":
                second_started.set()
        assert release.wait(timeout=10)
        return []

    model = Model(respond)
    with ThreadPoolExecutor(max_workers=2) as callers:
        large = callers.submit(runtime.analyze, model, [batch(f"large-{i}") for i in range(20)], namespace="workspace")
        try:
            assert first_started.wait(timeout=10)
            small = callers.submit(runtime.analyze, model, [batch("small")], namespace="workspace")
            assert second_started.wait(timeout=10)
        finally:
            release.set()
        assert small.result(timeout=10) == [[]]
        assert len(large.result(timeout=10)) == 20


def test_cache_is_scoped_to_workspace_model_fields_and_headers(runtime):
    calls = []
    model = Model(lambda fields, columns: calls.append((fields, columns)) or [])
    for namespace, request in [("a", batch("sku")), ("a", batch("sku")), ("b", batch("sku")),
                               ("a", batch("name")), ("a", ([{"id": "sku"}], [{"column": 1, "header": "other"}]))]:
        assert runtime.analyze(model, [request], namespace=namespace) == [[]]
    assert len(calls) == 4


def test_failed_requests_are_retried_instead_of_cached(runtime):
    calls = []

    def respond(fields, columns):
        calls.append(True)
        if len(calls) == 1:
            raise ValueError("temporary failure")
        return []

    model = Model(respond)
    first = runtime.analyze(model, [batch("sku")], namespace="workspace")
    assert isinstance(first[0], ValueError)
    assert runtime.analyze(model, [batch("sku")], namespace="workspace") == [[]]
    assert len(calls) == 2


def test_batch_progress_counts_failures_and_shared_cached_results(runtime):
    calls, snapshots = [], []
    def respond(fields, columns):
        calls.append(fields[0]['id'])
        if fields[0]['id'] == 'bad':
            raise ValueError('private upstream details')
        return []
    model = Model(respond)
    runtime.analyze(model, [batch('good')], namespace='progress')
    result = runtime.analyze(model, [batch('good'), batch('good'), batch('bad')], namespace='progress', progress=snapshots.append)
    assert snapshots[0] == ['waiting'] * 3
    assert snapshots[-1] == ['completed', 'completed', 'failed']
    assert calls.count('good') == 1 and isinstance(result[-1], ValueError)
    assert 'private upstream details' not in str(snapshots)


def test_cache_expires_and_settings_changes_invalidate_namespace(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    calls = []
    model = Model(lambda fields, columns: calls.append(True) or [])
    model.path = tmp_path / "settings.json"
    namespace = mapping_namespace(model, {"model": "a"})
    model.path.write_text('{"secret":"first"}', encoding="utf-8")
    assert mapping_namespace(model, {"model": "a"}) != namespace
    first = mapping_namespace(model, {"model": "a"})
    model.path.write_text('{"secret":"second"}', encoding="utf-8")
    assert mapping_namespace(model, {"model": "a"}) != first
    assert "second" not in mapping_namespace(model, {"model": "a"})
    assert mapping_namespace(model, {"model": "b"}) != mapping_namespace(model, {"model": "a"})
    instance = MappingRuntime(cache_ttl=-1)
    try:
        instance.analyze(model, [batch("sku")], namespace=namespace)
        instance.analyze(model, [batch("sku")], namespace=namespace)
        assert len(calls) == 2
    finally:
        instance.shutdown()


def test_separate_worker_runtimes_share_redis_suggestions(monkeypatch):
    class Cache:
        def __init__(self):
            self.values = {}
            self.mutex = threading.Lock()

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, **kwargs):
            self.values[key] = value

        def lock(self, key, **kwargs):
            return self.mutex

    cache = Cache()
    monkeypatch.setenv("REDIS_URL", "redis://example")
    monkeypatch.setattr(runtime_module, "_shared_cache", lambda _: cache)
    calls = []
    model = Model(lambda fields, columns: calls.append(True) or [{"field_id": "sku"}])
    first, second = MappingRuntime(), MappingRuntime()
    try:
        assert first.analyze(model, [batch("sku")], namespace="workspace") == [[{"field_id": "sku"}]]
        assert second.analyze(model, [batch("sku")], namespace="workspace") == [[{"field_id": "sku"}]]
        assert len(calls) == 1
    finally:
        first.shutdown()
        second.shutdown()


def test_model_still_runs_if_shared_cache_is_unavailable(runtime, monkeypatch):
    class Cache:
        def lock(self, *args, **kwargs):
            return threading.Lock()

        def get(self, key):
            raise RedisConnectionError("offline")

    monkeypatch.setenv("REDIS_URL", "redis://example")
    monkeypatch.setattr(runtime_module, "_shared_cache", lambda _: Cache())
    assert runtime.analyze(Model(lambda *args: []), [batch("sku")], namespace="workspace") == [[]]


@pytest.mark.parametrize("error_code", [None, "AI_TIMEOUT", "AI_SERVICE_ERROR", "AI_INVALID_MAPPING"])
def test_workbook_failure_preserves_exact_matches(tmp_path, monkeypatch, error_code):
    from excel_auditor.workflow.ai import AIClient
    from excel_auditor.workflow.models import WorkflowConfig, WorkflowError
    from excel_auditor.workflow.semantic_mapping import suggest_semantic_bindings

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(AIClient, "settings", lambda self: {"configured": True})
    monkeypatch.setattr(runtime_module.time, "sleep", lambda _: None)

    def respond(self, fields, columns, **kwargs):
        if error_code:
            raise WorkflowError("private upstream details", 502, error_code)
        raise ValueError("private upstream details")

    monkeypatch.setattr(AIClient, "reconcile_field_mappings", respond)
    config = WorkflowConfig(name="Batch test", fields=[{"rule": {"name": f"field_{i}", "title": f"Target {i}"}} for i in range(51)],
                            outputs=[{"name": "output.xlsx", "sheets": [{"columns": [{"title": "Target 0", "field": "field_0"}]}]}])
    sheets = [{"sheet": "Input", "columns": [{"column": 1, "header": "Target 0"}]}]
    suggest_semantic_bindings(sheets, config, AIClient(tmp_path, "local"), [])
    assert sheets[0]["mapping_summary"]["model_status"] == "failed"
    assert [item["field"] for item in sheets[0]["suggested_bindings"]] == ["field_0"]
    assert sheets[0]["model_errors"][0]["code"] == (error_code or "AI_ANALYSIS_ERROR")
    assert sheets[0]["model_errors"][0]["batch"] == 1
    assert "1/1 批" in sheets[0]["mapping_notes"][0]
    assert "private upstream details" not in str(sheets)

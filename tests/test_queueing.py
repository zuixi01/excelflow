from pathlib import Path

from excel_auditor.queueing import RedisJobQueue, select_queue
from excel_auditor.rules import load_rules


def test_product_jobs_and_revisions_use_versioned_rq_payloads(monkeypatch):
    import excel_auditor.queueing as queueing

    calls = []

    class FakeQueue:
        def __init__(self, name, connection, default_timeout):
            calls.append(("init", name, connection, default_timeout))

        def enqueue(self, *args, **kwargs):
            calls.append(("enqueue", args, kwargs))

    connection = object()
    def connect(url, **options):
        assert options.pop("retry").get_retries() == 0
        assert options == {"decode_responses": False, "socket_connect_timeout": 2, "socket_timeout": 5}
        return connection
    monkeypatch.setattr(queueing.Redis, "from_url", staticmethod(connect))
    monkeypatch.setattr(queueing, "Queue", FakeQueue)
    rules = load_rules(Path("configs/examples/product-normalization.yaml"))
    queue = RedisJobQueue("redis://example/0")

    queue.enqueue_product(Path("runtime"), "job_1", Path("input.xlsx"), rules, "tenant-a", "operator")
    queue.enqueue_product_revision(Path("runtime"), "job_1", rules, "tenant-a", "operator", 3)

    initial = calls[1]
    assert initial[1][0] == "excel_auditor.worker.run_product_job"
    assert initial[2]["job_id"] == "rq_product_job_1"
    assert initial[2]["job_timeout"] == rules.workbook.processing_timeout_seconds
    revision = calls[2]
    assert revision[1][0] == "excel_auditor.worker.run_product_revision"
    assert revision[2]["job_id"] == "rq_product_revision_job_1_3"


def test_field_reconciliation_retries_have_distinct_queue_ids(monkeypatch):
    import excel_auditor.queueing as queueing

    calls = []
    class FakeQueue:
        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(queueing.Redis, "from_url", staticmethod(lambda *args, **kwargs: object()))
    monkeypatch.setattr(queueing, "Queue", FakeQueue)
    queue = RedisJobQueue("redis://example/0")
    for revision in (0, 1):
        queue.enqueue_field_reconciliation(Path("runtime"), "job_1", Path("input.xlsx"), "fields", {}, "tenant-a", revision=revision)
    assert calls[0][0] == ("excel_auditor.worker.run_field_reconciliation_job", "runtime", "job_1", "input.xlsx", "fields", {}, "tenant-a")
    assert calls[0][1]["job_id"] == "rq_field_job_1_0"
    assert calls[1][1]["job_id"] == "rq_field_job_1_1"


def test_generic_tasks_route_to_named_queue(monkeypatch):
    import excel_auditor.queueing as queueing

    queues = {}

    class FakeQueue:
        def __init__(self, name, connection, default_timeout):
            queues[name] = self
            self.name = name
            self.jobs = []

        def enqueue(self, *args, **kwargs):
            self.jobs.append((args, kwargs))

    monkeypatch.setattr(queueing.Redis, "from_url", staticmethod(lambda *args, **kwargs: object()))
    monkeypatch.setattr(queueing, "Queue", FakeQueue)
    queue = RedisJobQueue("redis://example/0")
    queue.enqueue_task("task-1", "heavy", {"kind": "comparison", "job_id": "job-1"})

    assert "heavy" in queues
    assert queues["heavy"].jobs[0][0] == ("excel_auditor.worker.run_task", "task-1", {"kind": "comparison", "job_id": "job-1"})


def test_workload_queue_selection_is_deterministic():
    assert select_queue("comparison", estimated_bytes=1_000_000) == "normal"
    assert select_queue("comparison", estimated_bytes=80_000_000) == "heavy"
    assert select_queue("managed_http") == "network"
    assert select_queue("workflow") == "workflow"


def test_exports_have_dedicated_workers_and_do_not_wait_behind_imports(monkeypatch):
    import excel_auditor.queueing as queueing

    calls = []

    class FakeQueue:
        def __init__(self, name, **kwargs):
            self.name = name

        def enqueue(self, *args, **kwargs):
            calls.append((self.name, args))

    monkeypatch.setattr(queueing.Redis, "from_url", staticmethod(lambda *args, **kwargs: object()))
    monkeypatch.setattr(queueing, "Queue", FakeQueue)
    queue = RedisJobQueue("redis://example/0")
    for operation in ("parse_task", "export", "parse_template", "learn"):
        queue.enqueue_workflow(Path("runtime"), operation, "task_1", "local")
    assert [name for name, _ in calls] == ["workflow", "workflow-export", "workflow", "workflow"]

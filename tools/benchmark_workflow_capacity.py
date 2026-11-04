"""Isolated HTTP + PostgreSQL + Redis/RQ benchmark. Never targets the live data root."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import platform
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
from alembic import command
from alembic.config import Config
from openpyxl import Workbook
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import make_url

from excel_auditor.persistence import DatabaseRepository
from excel_auditor.workflow.ai import AIClient, AISettings
from excel_auditor.workflow.field_profiles import FieldProfile, FieldProfileService, ProfileEdit
from excel_auditor.workflow.models import MappingRequest
from excel_auditor.workflow.repository import WorkflowStore, executions, now, objects
from excel_auditor.workflow.tasks import TaskService


def distribution(values):
    values = sorted(values)
    return {"p50_seconds": round(statistics.median(values), 4),
            "p95_seconds": round(values[min(len(values) - 1, int(len(values) * .95))], 4)} if values else {}


def workbook(rows, columns, variant=""):
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Products")
    sheet.append([f"supplier_{index}{variant}" for index in range(columns)])
    for index in range(rows):
        sheet.append([f"{index:06d}_{column}" for column in range(columns)])
    output = BytesIO()
    book.save(output)
    return output.getvalue()


class Model(BaseHTTPRequestHandler):
    calls = 0
    lock = threading.Lock()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        request = json.loads(body["messages"][-1]["content"])
        with self.lock:
            Model.calls += 1
        time.sleep(.1)
        mappings = [{"column": int(field["id"][1:]) + 1, "field_id": field["id"], "confidence": .99,
                     "reason": "Synthetic benchmark mapping"} for field in request["fields"]]
        result = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"mappings": mappings})}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, *_):
        pass


def wait_for(predicate, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise TimeoutError("benchmark condition did not complete")


def mapping(task):
    return {"base_revision": task["revision"], "sheets": [{
        "sheet": sheet["sheet"], "header_row": sheet["header_row"], "header_depth": sheet["header_depth"],
        "bindings": sheet["suggested_bindings"],
    } for sheet in task["payload"]["sheets"]]}


def verify_large_export(content, row_count, columns, data_start_row):
    """Check every synthetic data cell after the configured output headers."""
    from openpyxl import load_workbook
    book = load_workbook(BytesIO(content), read_only=True)
    try:
        sheet = book.active
        assert sheet.max_column == columns, (sheet.max_column, columns)
        written = 0
        for index, row in enumerate(sheet.iter_rows(min_row=data_start_row, values_only=True)):
            expected = tuple(f"{index:06d}_{column}" for column in range(columns))
            assert row == expected, f"export data mismatch at row {index + data_start_row}"
            written += 1
        assert written == row_count, (written, row_count)
        return written
    finally:
        book.close()


def set_benchmark_fields(profiles, count):
    current = profiles.get("local")
    published = current["payload"].get("published") or {}
    previous = published.get("fixed_fields", [])
    fields = [{"id": f"f{i}", "title": f"Field {i}", "enabled": i < count}
              for i in range(max(count, len(previous)))]
    profile = FieldProfile(name="Benchmark", fixed_fields=fields)
    changed = profiles.save("local", ProfileEdit(base_revision=current["revision"], profile=profile))
    profiles.publish("local", changed["revision"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tasks", type=int, nargs="+", default=[30, 60])
    parser.add_argument("--large", action="store_true")
    parser.add_argument("--large-shape", type=int, nargs=2, action="append", metavar=("ROWS", "COLUMNS"),
                        help="Override large sample shapes; may be repeated")
    parser.add_argument("--index", action="store_true")
    parser.add_argument("--skip-matrix", action="store_true", help="Run warmup and requested large/index checks only")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--export-workers", type=int, default=2)
    parser.add_argument("--accounts", nargs="+", choices=["shared", "multiple"], default=["shared", "multiple"])
    parser.add_argument("--cache", nargs="+", choices=["cold", "warm", "different_headers"], default=["cold", "warm", "different_headers"])
    parser.add_argument("--port", type=int, default=18089)
    args = parser.parse_args()
    if not (1 <= args.workers <= 32 and 1 <= args.export_workers <= 32):
        parser.error("worker counts must be between 1 and 32")
    if args.large_shape and (not args.large or any(not (1 <= rows <= 20000 and 8 <= columns <= 150) for rows, columns in args.large_shape)):
        parser.error("--large-shape requires --large, 1..20000 rows and 8..150 columns")
    if "warm" in args.cache and ("cold" not in args.cache or args.cache.index("cold") > args.cache.index("warm")):
        parser.error("warm cache measurement requires a preceding cold case")
    worker_class = "rq.worker.SimpleWorker" if os.name == "nt" else "rq.worker.Worker"
    database_url = os.environ["BENCHMARK_DATABASE_URL"]
    redis_url = os.environ["BENCHMARK_REDIS_URL"]
    tag = uuid4().hex[:8]
    schema = "benchmark_" + tag
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as con:
        con.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin_engine.dispose()
    database_url = make_url(database_url).update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    args.output.mkdir(parents=True, exist_ok=True)
    root = (args.output / ("runtime_" + uuid4().hex[:8])).resolve()
    root.mkdir()
    env = {**os.environ, "DATABASE_URL": database_url, "REDIS_URL": redis_url, "EXCEL_AUDITOR_DATA": str(root),
           "PYTHONPATH": str(Path("src").resolve()), "EXCEL_AUDITOR_REQUIRE_AUTH": "1", "EXCEL_AUDITOR_ENV": "development",
           "EXCEL_AUDITOR_ADMIN_USERNAME": "benchmark_admin", "EXCEL_AUDITOR_ADMIN_PASSWORD": "benchmark-admin-password",
           "EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP": "1", "EXCEL_AUDITOR_RECOVERY_INTERVAL": "1"}
    os.environ.update({key: env[key] for key in ["DATABASE_URL", "REDIS_URL", "EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP"]})
    command.upgrade(Config("alembic.ini"), "head")
    database = DatabaseRepository(database_url, create_schema=False)
    store = WorkflowStore(root / "workflow", database.engine)
    database.ensure_local_admin(username="benchmark_admin", password="benchmark-admin-password")
    users = [database.create_user(tenant_id="local", username=f"bench_{tag}_{i}", password="benchmark-operator-password", role="operator") for i in range(4)]
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    AIClient(store.root, "local").save(AISettings(base_url=f"http://127.0.0.1:{model.server_port}", model="synthetic", api_key="test-key"))
    profiles = FieldProfileService(store)

    def set_fields(count):
        set_benchmark_fields(profiles, count)

    set_fields(8)
    processes, logs, clients = [], [], []
    stats = {"peak_rss_mb": 0, "peak_cpu_percent": 0, "samples": 0, "lock_wait_samples": 0}
    stopped = threading.Event()

    def start_process(command, name, additions=None):
        log = (root / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(command, env={**env, **(additions or {})}, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    def observe():
        tracked = {}
        while not stopped.wait(.25):
            rss, cpu = 0, 0
            for process in processes:
                try:
                    parent = psutil.Process(process.pid)
                    for item in [parent, *parent.children(recursive=True)]:
                        known = tracked.setdefault(item.pid, item)
                        rss += known.memory_info().rss
                        cpu += known.cpu_percent()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            stats["peak_rss_mb"] = max(stats["peak_rss_mb"], round(rss / 1024 ** 2, 1))
            stats["peak_cpu_percent"] = max(stats["peak_cpu_percent"], cpu)
            stats["samples"] += 1
            with database.engine.connect() as con:
                stats["lock_wait_samples"] += con.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")).scalar_one()

    report = {"model": "synthetic HTTP, 100 ms/request; no production model or business files", "runtime": str(root),
              "schema": schema, "platform": platform.platform(), "python": platform.python_version(),
              "cpu_logical": psutil.cpu_count(), "host_memory_gb": round(psutil.virtual_memory().total / 1024 ** 3, 1),
              "import_workers": args.workers, "export_workers": args.export_workers, "api_workers": 2,
              "rq_worker_class": worker_class, "completed": False, "cases": []}

    def checkpoint():
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    try:
        for index in range(args.workers):
            start_process([sys.executable, "-c", "import excel_auditor.worker; from rq.cli import main; main()", "worker", "-w", worker_class, "--url", redis_url,
                           "--name", f"bench_{tag}_import_{index}", "workflow"], f"import_{index}")
        for index in range(args.export_workers):
            start_process([sys.executable, "-c", "import excel_auditor.worker; from rq.cli import main; main()", "worker", "-w", worker_class, "--url", redis_url,
                           "--name", f"bench_{tag}_export_{index}", "workflow-export"], f"export_{index}")
        start_process([sys.executable, "tools/benchmark_api_server.py"], "api", {"BENCHMARK_PORT": str(args.port)})
        base = f"http://127.0.0.1:{args.port}/api/v1"

        def ready():
            try:
                return httpx.get(base + "/auth/session", timeout=1, trust_env=False).status_code in {200, 401}
            except httpx.HTTPError:
                return False
        wait_for(ready, 90)
        monitor = threading.Thread(target=observe, daemon=True)
        monitor.start()

        def login(username, password):
            client = httpx.Client(base_url=base, timeout=120, trust_env=False, headers={"Connection": "close"})
            client.post("/auth/login", json={"username": username, "password": password}).raise_for_status()
            token = client.get("/auth/csrf").json()["csrf_token"]
            client.headers["X-CSRF-Token"] = token
            clients.append(client)
            return client

        admin = login("benchmark_admin", "benchmark-admin-password")

        def run_case(count, shared, cache):
            print(json.dumps({"starting": {"tasks": count, "shared": shared, "cache": cache}}), flush=True)
            sessions = [login(users[0 if shared else i % len(users)].username, "benchmark-operator-password") for i in range(count)]
            case_tag = f"{tag}_{uuid4().hex[:8]}"
            sample = workbook(100, 8)
            started = time.monotonic()
            calls_before = Model.calls
            query_times, save_times = [], []

            def submit(index):
                tick = time.monotonic()
                content = workbook(100, 8, f"_{case_tag}_{index}") if cache == "different_headers" else sample
                response = sessions[index].post("/standardization-tasks", data={"name": case_tag + f"_{index}"},
                    files={"excel_file": ("input.xlsx", content)}, headers={"Idempotency-Key": uuid4().hex})
                response.raise_for_status()
                return response.json()["id"], time.monotonic() - tick

            with ThreadPoolExecutor(max_workers=count) as pool:
                submitted = list(pool.map(submit, range(count)))
            ids = [item[0] for item in submitted]

            def one_task(index):
                client, task_id = sessions[index], ids[index]
                def parsed():
                    tick = time.monotonic()
                    response = client.get(f"/workflow-tasks/{task_id}")
                    response.raise_for_status()
                    query_times.append(time.monotonic() - tick)
                    task = response.json()
                    if task["status"] == "failed":
                        raise RuntimeError(task)
                    return task if task["status"] == "mapping" else None
                task = wait_for(parsed)
                task = client.post(f"/workflow-tasks/{task_id}/mapping", json=mapping(task)).raise_for_status().json()
                page = client.get(f"/workflow-tasks/{task_id}/maintenance").raise_for_status().json()
                assert page["total"] == 100
                tick = time.monotonic()
                task = client.patch(f"/workflow-tasks/{task_id}/maintenance", json={"base_revision": task["revision"],
                    "changes": {page["items"][0]["id"]: {"f0": "changed_" + str(index)}}}).raise_for_status().json()
                save_times.append(time.monotonic() - tick)
                run = client.post(f"/workflow-tasks/{task_id}/exports", json={"base_revision": task["revision"]}).raise_for_status().json()
                def exported():
                    task = client.get(f"/workflow-tasks/{task_id}").raise_for_status().json()
                    run = task["exports"][0]
                    if run["status"] == "failed":
                        raise RuntimeError(run)
                    return run if run["status"] == "completed" else None
                complete = wait_for(exported)
                artifact = complete["payload"]["artifacts"][0]["name"]
                response = client.get(f"/workflow-tasks/{task_id}/exports/{run['id']}/files/{artifact}")
                response.raise_for_status()
                from openpyxl import load_workbook
                book = load_workbook(BytesIO(response.content), read_only=True)
                output_rows = list(book.active.iter_rows(min_row=1, max_row=4, values_only=True))
                assert any(row[0] == "changed_" + str(index) for row in output_rows), output_rows
                book.close()
                return time.monotonic() - started, run["id"]

            with ThreadPoolExecutor(max_workers=count) as pool:
                finished = list(pool.map(one_task, range(count)))
            total_time = time.monotonic() - started
            all_ids = ids + [item[1] for item in finished]
            with store.engine.connect() as con:
                work = con.execute(select(executions).where(executions.c.object_id.in_(all_ids))).mappings().all()
            assert len(work) == count * 2 and all(row["status"] == "succeeded" and row["attempts"] == 1 for row in work)
            def delta(row, end, start):
                return (datetime.fromisoformat(row[end]) - datetime.fromisoformat(row[start])).total_seconds()
            own = sessions[0].get("/workflow-tasks", params={"search": case_tag}).raise_for_status().json()
            assert own["total"] == (count if shared else (count + 3) // 4)
            admin_items = admin.get("/workflow-tasks", params={"search": case_tag, "limit": 100}).raise_for_status().json()
            assert admin_items["total"] == count and all(item["created_by_username"] for item in admin_items["items"])
            if not shared:
                assert sessions[0].get(f"/workflow-tasks/{ids[1]}").status_code == 404
            case = {"tasks": count, "accounts": 1 if shared else 4, "cache": cache,
                    "submit": distribution([item[1] for item in submitted]), "query": distribution(query_times),
                    "save": distribution(save_times), "queue": distribution([delta(row, "started_at", "created_at") for row in work]),
                    "execution": distribution([delta(row, "finished_at", "started_at") for row in work]),
                    "end_to_end": distribution([item[0] for item in finished]), "batch_seconds": round(total_time, 3),
                    "tasks_per_minute": round(count / total_time * 60, 2), "model_calls": Model.calls - calls_before,
                    "model_429": 0, "cross_account_access": 0, "duplicate_results": 0}
            report["cases"].append(case)
            print(json.dumps(case), flush=True)
            checkpoint()
            for client in sessions:
                client.close()
                clients.remove(client)

        run_case(1, True, "cold")
        report["warmup"] = report["cases"].pop()
        for count in ([] if args.skip_matrix else args.tasks):
            for account_mode in args.accounts:
                shared = account_mode == "shared"
                # A new settings revision provides a fresh shared cache namespace.
                AIClient(store.root, "local").save(AISettings(base_url=f"http://127.0.0.1:{model.server_port}", model=f"synthetic-{uuid4().hex}", api_key="test-key"))
                for cache in args.cache:
                    run_case(count, shared, cache)

        service = TaskService(store)
        if args.large:
            report["large_samples"] = []
            for row_count, columns in (args.large_shape or [(10000, 100), (20000, 50)]):
                set_fields(columns)
                content = workbook(row_count, columns)
                tick = time.monotonic()
                task = admin.post("/standardization-tasks", data={"name": "large"}, files={"excel_file": ("large.xlsx", content)}).raise_for_status().json()
                task_id = task["id"]
                def parsed_large():
                    current = admin.get(f"/workflow-tasks/{task_id}").raise_for_status().json()
                    if current["status"] == "failed":
                        raise RuntimeError(current)
                    return current if current["status"] == "mapping" else None
                task = wait_for(parsed_large, 600)
                task = admin.post(f"/workflow-tasks/{task_id}/mapping", json=mapping(task)).raise_for_status().json()
                assert task["payload"]["record_count"] == row_count
                with store.engine.connect() as con:
                    size = con.execute(text("SELECT sum(octet_length(source::text)),pg_total_relation_size('workflow_records') FROM workflow_records WHERE task_id=:task"), {"task": task["id"]}).one()
                bindings = task["payload"]["mapping"]["sheets"][0]["bindings"]
                legacy_mapping_bytes = len(json.dumps(bindings, ensure_ascii=False).encode()) * row_count
                run = admin.post(f"/workflow-tasks/{task_id}/exports", json={"base_revision": task["revision"]}).raise_for_status().json()
                def exported_large():
                    current = admin.get(f"/workflow-tasks/{task_id}").raise_for_status().json()["exports"][0]
                    if current["status"] == "failed":
                        raise RuntimeError(current)
                    return current if current["status"] == "completed" else None
                complete = wait_for(exported_large, 600)
                artifact = complete["payload"]["artifacts"][0]["name"]
                response = admin.get(f"/workflow-tasks/{task_id}/exports/{run['id']}/files/{artifact}").raise_for_status()
                output_plan = task["payload"]["config"]["outputs"][0]["sheets"][0]
                verified_rows = verify_large_export(response.content, row_count, columns, output_plan["data_start_row"])
                report["large_samples"].append({"rows": row_count, "columns": columns, "seconds": round(time.monotonic() - tick, 3),
                    "source_bytes": size[0], "legacy_redundant_mapping_bytes": legacy_mapping_bytes, "records_table_bytes": size[1],
                    "export_verified": True, "verified_data_rows": verified_rows, "data_start_row": output_plan["data_start_row"]})
                checkpoint()
                print(json.dumps(report["large_samples"][-1]), flush=True)

        if args.index:
            report["pagination"] = []
            tenant = "index_" + tag
            for count in (10000, 100000):
                with store.engine.begin() as con:
                    start_index = 0 if count == 10000 else 10000
                    for start in range(start_index, count, 1000):
                        con.execute(insert(objects), [{"id": f"{tenant}_{i:06d}", "tenant_id": tenant, "kind": "task", "status": "ready",
                            "revision": 0, "payload": {"name": f"Item {i}", "filename": "source.xlsx", "actor": "alice"},
                            "creator_id": "alice", "search_summary": f"item {i}source.xlsx", "created_at": now(), "updated_at": now()} for i in range(start, min(count, start + 1000))])
                tick = time.monotonic()
                page = service.list(tenant, offset=count - 30, limit=30, creator_id="alice")
                elapsed = time.monotonic() - tick
                assert page["total"] == count and len(page["items"]) == 30
                with store.engine.connect() as con:
                    con.execute(text("ANALYZE workflow_objects"))
                    plan = con.execute(text("EXPLAIN (ANALYZE, BUFFERS) SELECT id FROM workflow_objects WHERE tenant_id=:tenant AND kind='task' AND creator_id='alice' ORDER BY updated_at DESC,id DESC LIMIT 30 OFFSET :offset"), {"tenant": tenant, "offset": count - 30}).scalars().all()
                report["pagination"].append({"tasks": count, "deep_page_seconds": round(elapsed, 4), "returned": 30, "explain": plan})
                checkpoint()
                print(json.dumps(report["pagination"][-1]), flush=True)
            with store.engine.connect() as con:
                task_id = con.execute(select(objects.c.id).where(objects.c.tenant_id == "local", objects.c.kind == "task").limit(1)).scalar_one()
                report["detail_query"] = {}
                for label, field in [("json_filter", "payload->>'task_id'"), ("parent_index", "parent_task_id")]:
                    plan = con.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM workflow_objects WHERE tenant_id='local' AND kind='export' AND {field}=:task ORDER BY created_at DESC,id DESC"), {"task": task_id}).scalar_one()
                    report["detail_query"][label] = plan[0]
                print(json.dumps({"detail_query_ms": {key: value["Execution Time"] for key, value in report["detail_query"].items()}}), flush=True)
        report["resources"] = stats
        report["completed"] = True
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        stopped.set()
        for client in clients:
            client.close()
        for process in reversed(processes):
            try:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for item in children:
                    item.terminate()
                parent.terminate()
                _, alive = psutil.wait_procs([*children, parent], timeout=8)
                for item in alive:
                    item.kill()
                process.wait(timeout=10)
            except (psutil.NoSuchProcess, subprocess.TimeoutExpired):
                pass
        for log in logs:
            log.close()
        model.shutdown()
        database.engine.dispose()
        report["resources"] = stats
        checkpoint()


if __name__ == "__main__":
    main()

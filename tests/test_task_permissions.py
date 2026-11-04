from __future__ import annotations

import importlib
import asyncio
from contextlib import ExitStack
from io import BytesIO

import pytest
import httpx
from fastapi.testclient import TestClient
from openpyxl import Workbook

from excel_auditor.workflow.field_profiles import FieldProfileService
from excel_auditor.workflow.models import TemplateEdit, default_config
from excel_auditor.workflow.repository import WorkflowStore
from excel_auditor.workflow.tasks import TaskService
from excel_auditor.workflow.templates import TemplateService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_DATA", str(tmp_path))
    monkeypatch.setenv("EXCEL_AUDITOR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("EXCEL_AUDITOR_ADMIN_PASSWORD", "test-password")
    for name in ("DATABASE_URL", "REDIS_URL", "S3_BUCKET", "EXCEL_AUDITOR_API_TOKEN", "EXCEL_AUDITOR_API_TOKENS_JSON", "EXCEL_AUDITOR_AUTH_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    import excel_auditor.api as api_module

    api = importlib.reload(api_module)
    users = {"admin": api.auth_database.get_user_by_username("local", "admin")}
    for username, role in (("alice", "operator"), ("bob", "operator"), ("viewer", "viewer")):
        users[username] = api.auth_database.create_operator(username=username, password="test-password", role=role)
    store = WorkflowStore(tmp_path / "workflow")
    tasks = TaskService(store, auth_database=api.auth_database)
    templates = TemplateService(store)
    draft = templates.create("local", name="Shared workflow")
    draft = templates.update(draft["id"], "local", TemplateEdit(base_revision=draft["revision"], config=default_config(), confirmed=True))
    template = templates.publish(draft["id"], "local", draft["revision"])
    profiles = FieldProfileService(store)
    draft = profiles.get("local")
    profiles.publish("local", draft["revision"])

    book = Workbook()
    book.active.append(["商品名称", "货号", "销售价", "库存"])
    book.active.append(["台灯", "0001", "29.50", "8"])
    content = BytesIO()
    book.save(content)
    book.close()
    uploads = {"excel_file": ("supplier.xlsx", content.getvalue())}
    seeded = {}
    for name, username in (("alice-first", "alice"), ("bob-task", "bob"), ("alice-second", "alice"), ("admin-task", "admin")):
        task, _ = tasks.create("local", users[username].id, template["id"], name, "supplier.xlsx", content.getvalue())
        tasks.parse(task["id"], "local")
        seeded[name] = tasks.get(task["id"], "local")
    with store.engine.begin() as con:
        payload = {"name": "Legacy task", "filename": "legacy.xlsx", "config": default_config().model_dump(mode="json")}
        seeded["legacy"] = store.create(con, "local", "task", payload, "mapping")
        seeded["foreign"] = store.create(con, "another-workspace", "task", {**payload, "actor": users["alice"].id}, "mapping")
    with ExitStack() as stack:
        clients = {}
        for username in users:
            client = stack.enter_context(TestClient(api.app))
            response = client.post("/api/v1/auth/login", json={"username": username, "password": "test-password"})
            assert response.status_code == 200, response.text
            client.headers["X-CSRF-Token"] = client.get("/api/v1/auth/csrf").json()["csrf_token"]
            clients[username] = client
        yield api, users, tasks, template, uploads, seeded, clients
    store.engine.dispose()
    api.auth_database.engine.dispose()


def test_task_lists_scope_search_totals_and_pagination_to_creator(workspace):
    _, _, _, _, _, seeded, clients = workspace
    alice = clients["alice"]
    listed = alice.get("/api/v1/workflow-tasks?limit=1").json()
    assert listed["total"] == 2
    second_page = alice.get("/api/v1/workflow-tasks?limit=1&offset=1").json()
    assert {listed["items"][0]["id"], second_page["items"][0]["id"]} == {seeded["alice-first"]["id"], seeded["alice-second"]["id"]}
    assert alice.get("/api/v1/workflow-tasks?search=bob").json() == {"items": [], "total": 0}
    assert alice.get("/api/v1/workflow-tasks?status=mapping").json()["total"] == 2
    assert alice.get("/api/v1/workflow-tasks?status=completed").json()["total"] == 0
    assert clients["bob"].get("/api/v1/workflow-tasks").json()["total"] == 1
    assert clients["viewer"].get("/api/v1/workflow-tasks").json() == {"items": [], "total": 0}


def test_admin_sees_all_tasks_and_creator_names_including_disabled_accounts(workspace):
    api, users, _, _, _, seeded, clients = workspace
    api.auth_database.set_user_status(users["bob"].id, "disabled")
    admin = clients["admin"]
    listed = admin.get("/api/v1/workflow-tasks").json()
    assert listed["total"] == 5
    by_id = {item["id"]: item for item in listed["items"]}
    for name, username in (("alice-first", "alice"), ("bob-task", "bob"), ("admin-task", "admin")):
        task_id = seeded[name]["id"]
        assert by_id[task_id]["created_by_username"] == username
        detail = admin.get(f"/api/v1/workflow-tasks/{task_id}").json()
        assert detail["created_by_id"] == users[username].id
        assert detail["created_by_username"] == username
    assert by_id[seeded["legacy"]["id"]]["created_by_id"] is None
    assert admin.get(f"/api/v1/workflow-tasks/{seeded['foreign']['id']}").status_code == 404
    api.auth_database.update_user_role(users["alice"].id, "admin")
    assert clients["alice"].get("/api/v1/workflow-tasks").json()["total"] == 5


def test_every_task_route_rejects_other_operators_before_reading_or_mutating(workspace):
    _, _, tasks, _, uploads, seeded, clients = workspace
    foreign = seeded["bob-task"]
    prefix = f"/api/v1/workflow-tasks/{foreign['id']}"
    attempts = [
        ("GET", "", {}),
        ("GET", "/maintenance", {}),
        ("GET", "/maintenance/download", {}),
        ("GET", "/exports/export_unknown/files/result.xlsx", {}),
        ("GET", "/exports/export_unknown/preview/result.xlsx", {}),
        ("POST", "/mapping/analyze", {"json": {"base_revision": 0}}),
        ("POST", "/mapping", {"json": {"base_revision": 0, "sheets": []}}),
        ("POST", "/mapping/reopen", {"json": {"base_revision": 0}}),
        ("PATCH", "/maintenance", {"json": {"base_revision": 0}}),
        ("POST", "/maintenance/validate", {"json": {"base_revision": 0}}),
        ("POST", "/maintenance/returns", {"files": uploads}),
        ("POST", "/maintenance/returns/apply", {"json": {"preview_id": "return_unknown"}}),
        ("POST", "/exports", {"json": {"base_revision": 0}}),
        ("POST", "/cancel", {"json": {"base_revision": 0}}),
        ("POST", "/retry", {"json": {"base_revision": 0}}),
    ]
    for method, suffix, kwargs in attempts:
        response = clients["alice"].request(method, prefix + suffix, **kwargs)
        assert response.status_code == 404, (method, suffix, response.text)
    assert tasks.get(foreign["id"], "local") == foreign
    for name in ("legacy", "foreign"):
        assert clients["alice"].get(f"/api/v1/workflow-tasks/{seeded[name]['id']}").status_code == 404
    assert clients["viewer"].get(prefix).status_code == 404
    assert clients["bob"].get(prefix).status_code == 200


def test_legacy_local_creator_displays_admin_without_changing_ownership(workspace):
    _, _, tasks, template, uploads, _, clients = workspace
    task, _ = tasks.create("local", "local", template["id"], "Before account login", "supplier.xlsx", uploads["excel_file"][1])
    task_id = task["id"]
    listed = clients["admin"].get("/api/v1/workflow-tasks").json()
    summary = next(item for item in listed["items"] if item["id"] == task_id)
    assert summary["created_by_username"] == "admin"
    assert summary["created_by_id"] == "local"
    detail = clients["admin"].get(f"/api/v1/workflow-tasks/{task_id}").json()
    assert detail["created_by_username"] == "admin"
    assert detail["payload"]["actor"] == "local"
    for username in ("alice", "bob"):
        assert clients[username].get(f"/api/v1/workflow-tasks/{task_id}").status_code == 404
        assert task_id not in {item["id"] for item in clients[username].get("/api/v1/workflow-tasks").json()["items"]}


def test_legacy_creator_uses_configured_admin_name_only_in_local_workspace(tmp_path):
    from excel_auditor.persistence import DatabaseRepository

    database = DatabaseRepository(f"sqlite:///{tmp_path / 'auth.sqlite3'}")
    database.ensure_local_admin(username="custom-admin", password="test-password")
    database.create_tenant("Another workspace", tenant_id="another-workspace")
    database.create_user(tenant_id="another-workspace", username="other-owner", password="test-password", role="owner")
    store = WorkflowStore(tmp_path / "workflow")
    tasks = TaskService(store, auth_database=database)
    try:
        with store.engine.begin() as con:
            payload = {"name": "Legacy", "filename": "supplier.xlsx", "actor": "local", "config": default_config().model_dump(mode="json")}
            local = store.create(con, "local", "task", payload)
            foreign = store.create(con, "another-workspace", "task", payload)
            unknown = store.create(con, "local", "task", {**payload, "actor": "unknown-user"})
        assert tasks.get(local["id"], "local")["created_by_username"] == "custom-admin"
        assert tasks.get(foreign["id"], "another-workspace")["created_by_username"] == "local"
        assert tasks.get(unknown["id"], "local")["created_by_username"] == "unknown-user"
    finally:
        store.engine.dispose()
        database.engine.dispose()


def test_bulk_delete_is_atomic_and_admin_can_delete_another_accounts_tasks(workspace):
    _, _, tasks, _, _, seeded, clients = workspace
    own_id, other_id = seeded["alice-first"]["id"], seeded["bob-task"]["id"]
    response = clients["alice"].request("DELETE", "/api/v1/workflow-tasks", json={"ids": [own_id, other_id]})
    assert response.status_code == 404
    assert tasks.get(own_id, "local") and tasks.get(other_id, "local")
    assert clients["alice"].request("DELETE", "/api/v1/workflow-tasks", json={"ids": [own_id]}).status_code == 200
    assert clients["admin"].request("DELETE", "/api/v1/workflow-tasks", json={"ids": [other_id]}).status_code == 200
    assert clients["bob"].get(f"/api/v1/workflow-tasks/{other_id}").status_code == 404


@pytest.mark.parametrize("endpoint", ["/workflow-tasks", "/standardization-tasks"])
def test_upload_replays_are_scoped_to_the_creating_account(workspace, endpoint):
    _, users, _, template, uploads, _, clients = workspace
    data = {"name": "Same input", "template_id": template["id"]} if endpoint == "/workflow-tasks" else {"name": "Same input"}
    task_ids = set()
    for username in ("alice", "bob"):
        first = clients[username].post("/api/v1" + endpoint, data=data, files=uploads, headers={"Idempotency-Key": "shared-key"})
        assert first.status_code == 202, first.text
        replay = clients[username].post("/api/v1" + endpoint, data=data, files=uploads, headers={"Idempotency-Key": "shared-key"})
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == first.json()["id"]
        assert replay.json()["payload"]["actor"] == users[username].id
        task_ids.add(first.json()["id"])
    assert len(task_ids) == 2


def test_admin_mapping_response_preserves_original_creator(workspace):
    from sqlalchemy import select
    from excel_auditor.workflow.execution import run_execution
    from excel_auditor.workflow.repository import executions

    _, users, tasks, _, _, seeded, clients = workspace
    task = seeded["alice-first"]
    response = clients["admin"].post(f"/api/v1/workflow-tasks/{task['id']}/mapping", json={
        "base_revision": task["revision"],
        "sheets": [{"sheet": sheet["sheet"], "header_row": sheet["header_row"], "header_depth": sheet["header_depth"],
                    "bindings": sheet["suggested_bindings"]} for sheet in task["payload"]["sheets"]],
    })
    assert response.status_code == 202, response.text
    with tasks.store.engine.connect() as con:
        execution_id = con.execute(select(executions.c.id).where(
            executions.c.object_id == response.json()['id'])).scalar_one()
    run_execution(tasks.store.root, execution_id)
    updated = clients['admin'].get(f"/api/v1/workflow-tasks/{task['id']}").json()
    assert updated["created_by_id"] == users["alice"].id
    assert updated["created_by_username"] == "alice"
    download = clients["alice"].get(f"/api/v1/workflow-tasks/{task['id']}/maintenance/download")
    assert download.status_code == 202
    with tasks.store.engine.connect() as con:
        execution_id = con.execute(select(executions.c.id).where(
            executions.c.object_id == download.json()['id'])).scalar_one()
    run_execution(tasks.store.root, execution_id)
    assert clients['alice'].get(download.headers['Location']).status_code == 200
    assert clients['alice'].get(download.headers['Location'] + '/result').status_code == 200
    assert clients["bob"].get(f"/api/v1/workflow-tasks/{task['id']}/maintenance/download").status_code == 404


def test_field_reconciliation_and_job_replays_respect_account_ownership(workspace):
    api, users, _, _, _, _, clients = workspace
    job_ids = []
    for username in ("alice", "bob"):
        job_id, replayed = api.service.create_or_get_job("same-key", "same-fingerprint", "local", users[username].id)
        assert replayed is False
        assert api.service.create_or_get_job("same-key", "same-fingerprint", "local", users[username].id) == (job_id, True)
        current = api.service.status(job_id)
        api.service._write_status(job_id, {**current, "workflow": "field_reconciliation"})
        job_ids.append(job_id)
    assert job_ids[0] != job_ids[1]
    for suffix in ("", "/mappings", "/issues", "/artifacts/result_xlsx"):
        assert clients["bob"].get(f"/api/v1/field-reconciliations/{job_ids[0]}{suffix}").status_code == 404
    assert clients["alice"].get(f"/api/v1/field-reconciliations/{job_ids[0]}").status_code == 200
    assert clients["admin"].get(f"/api/v1/field-reconciliations/{job_ids[0]}").status_code == 200


def test_thirty_parallel_submissions_from_shared_and_distinct_accounts(workspace, monkeypatch):
    # This scenario verifies ownership; production admission is exercised separately.
    monkeypatch.setenv('EXCEL_AUDITOR_MAX_UPLOADS', '30')
    monkeypatch.setenv('EXCEL_AUDITOR_WORKFLOW_USER_WAITING', '50')
    api, users, _, _, uploads, _, clients = workspace

    async def submit_all():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as caller:
            async def submit(index):
                username = "alice" if index < 20 else "bob"
                response = await caller.post("/api/v1/standardization-tasks", files=uploads,
                                             data={"name": f"Concurrent {index}"},
                                             headers={"Cookie": f"excel_flow_session={clients[username].cookies.get('excel_flow_session')}",
                                                      "X-CSRF-Token": clients[username].headers["X-CSRF-Token"]})
                assert response.status_code == 202, response.text
                assert response.json()["payload"]["actor"] == users[username].id
                return response.json()["id"]
            return await asyncio.gather(*(submit(index) for index in range(30)))

    task_ids = asyncio.run(submit_all())
    assert len(set(task_ids)) == 30
    for username, expected in (("alice", 22), ("bob", 11)):
        listed = clients[username].get("/api/v1/workflow-tasks?limit=100").json()
        assert listed["total"] == expected
        assert all(item["created_by_id"] == users[username].id for item in listed["items"])
        # Admission now queues excess work instead of executing all 30 in request threads.
        assert all(item["status"] in {"mapping", "processing"} for item in listed["items"])


def test_admin_configuration_permissions_match_role(workspace):
    _, _, _, _, _, _, clients = workspace
    assert clients["admin"].get("/api/v1/workflow-settings").json()["can_manage_templates"] is True
    assert clients["admin"].put("/api/v1/workflow-settings/ai", json={}).status_code == 200
    assert clients["alice"].get("/api/v1/workflow-settings").json()["can_manage_templates"] is False
    assert clients["alice"].put("/api/v1/workflow-settings/ai", json={}).status_code == 403


def test_field_lists_filter_before_counting_and_show_creators(workspace):
    api, users, _, _, _, _, clients = workspace
    ids = {}
    for name in ("alice", "bob"):
        job = api.service.create_job(tenant_id="local", user_id=users[name].id)
        api.service._write_status(job, {**api.service.status(job), "workflow": "field_reconciliation", "scope": {"merchant_id": name}})
        ids[name] = job
    own = clients["alice"].get("/api/v1/field-reconciliations?page_size=1").json()
    assert own["total"] == 1
    assert own["items"][0]["job_id"] == ids["alice"]
    all_tasks = clients["admin"].get("/api/v1/field-reconciliations").json()
    assert all_tasks["total"] == 2
    assert {item["created_by_username"] for item in all_tasks["items"]} == {"alice", "bob"}


def test_three_browser_tabs_keep_their_csrf_tokens_valid(workspace):
    _, _, _, _, _, _, clients = workspace
    client = clients["alice"]
    tokens = [client.get("/api/v1/auth/csrf").json()["csrf_token"] for _ in range(3)]
    assert len(set(tokens)) == 1
    for token in tokens:
        response = client.post("/api/v1/workflow-tasks/task_missing/cancel", json={"base_revision": 0}, headers={"X-CSRF-Token": token})
        assert response.status_code == 404

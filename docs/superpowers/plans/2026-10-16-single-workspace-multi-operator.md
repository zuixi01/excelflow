# Single-Workspace Multi-Operator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留单一 `local` 工作空间和唯一管理员账号的前提下，为每名运营人员提供独立账号、角色、配额、并发任务处理和可追溯操作审计。

**Architecture:** 认证层继续使用现有会话机制，但浏览器 API 去掉租户输入，所有数据归属固定为 `local`；管理员管理运营账号，任务记录保存创建者、状态、队列和 Worker 租约。任务状态与配额计数放入共享数据库，Redis/RQ 负责跨进程调度，Worker 通过租约、心跳、重试和幂等键保证并发安全。

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy/Alembic, SQLite（开发）/PostgreSQL（生产）, Redis, RQ, Vue 3, pytest, pnpm.

**Spec:** `docs/superpowers/specs/2026-10-16-single-workspace-multi-operator-design.md`

## Global Constraints

- 浏览器 API 不接受也不返回租户选择；内部兼容字段只允许固定值 `local`。
- 系统只能存在一个 `owner` 管理员；运营账号只能使用 `operator` 或 `viewer` 角色。
- 默认工作空间并发上限为 8 个运行任务、100 个排队任务。
- 默认单运营人员并发上限为 2 个运行任务、10 个排队任务。
- 所有任务状态变更和账号管理动作必须写入审计记录，且审计记录不能包含密码或令牌。
- SQLite 仅用于开发和单进程测试；生产部署必须使用 PostgreSQL、Redis 和对象存储。
- 文件上传必须限制大小并采用流式写入，任务载荷不能把整个文件内容放入 Redis。

## Review Focus

- 同一运营人员同时提交超过配额的任务时，只有配额内任务进入队列，其余请求得到稳定的 429 响应；测试归入 Task 3。
- 两个 API 进程同时创建任务时，数据库唯一键和事务计数不能造成重复任务或超额运行；测试归入 Task 3。
- Worker 在执行中断后重新领取任务时，旧租约不能覆盖新 Worker 的状态；测试归入 Task 4。
- 被禁用账号的现有会话和正在排队任务必须按规格失效或停止继续执行；测试归入 Task 2/4。
- 运营人员只能读取和操作自己有权限的任务，管理员可查看全局审计；测试归入 Task 2/5。

### Task 1: 固定单工作空间的身份与数据库边界

**Files:**
- Modify: `src/excel_auditor/auth_models.py`
- Modify: `src/excel_auditor/auth_service.py`
- Modify: `src/excel_auditor/auth_routes.py`
- Modify: `src/excel_auditor/persistence.py`
- Create: `migrations/versions/20261016_0007_local_workspace_operator_auth.py`
- Test: `tests/test_single_workspace_auth.py`

**Interfaces:**
- Produces `LOCAL_TENANT_ID = "local"`, `require_local_identity(request)`, and repository methods for operator listing, creation, role/status updates, session revocation, quota lookup, and audit-event insertion.

- [ ] **Step 1: Write failing tests** for login without `tenant_id`, rejection of non-`local` compatibility values, creation of only one owner, operator username uniqueness within `local`, and disabled-user session invalidation.
- [ ] **Step 2: Run** `pytest tests/test_single_workspace_auth.py -q`; verify the new endpoint/repository behavior fails before implementation.
- [ ] **Step 3: Implement** fixed-workspace identity normalization and repository constraints. Keep legacy storage columns only for migration compatibility; enforce `tenant_id == "local"` at the service boundary and add database checks/indexes for one owner and unique local usernames.
- [ ] **Step 4: Implement** the Alembic migration to backfill all existing tenants/users into `local`, retain the first owner as the unique administrator, disable duplicate owners, and preserve auth event history.
- [ ] **Step 5: Implement** operator management routes under `/api/v1/operators` with administrator-only create/update/disable/enable/reset-password/revoke-sessions actions. Responses must contain id, username, role, status, quota, timestamps, and never password hashes.
- [ ] **Step 6: Run** the focused auth tests and existing `tests/test_auth_*.py`; verify all pass.
- [ ] **Step 7: Commit** `feat: enforce local workspace operator accounts`.

### Task 2: 角色权限、会话和操作审计

**Files:**
- Modify: `src/excel_auditor/auth_service.py`
- Modify: `src/excel_auditor/api.py`
- Modify: `src/excel_auditor/user_routes.py`
- Modify: `src/excel_auditor/workflow/routes.py`
- Create: `src/excel_auditor/authorization.py`
- Test: `tests/test_operator_authorization.py`

**Interfaces:**
- Produces `require_role(*roles)`, `can_access_job(identity, job)`, and `record_operator_audit(action, actor_id, target_type, target_id, metadata)`.

- [ ] **Step 1: Write failing tests** for `admin/operator/viewer` permissions, task ownership filtering, audit records for account actions and task actions, and no secret values in audit metadata.
- [ ] **Step 2: Run** `pytest tests/test_operator_authorization.py -q`; verify expected failures.
- [ ] **Step 3: Implement** centralized authorization dependencies and replace route-local role/tenant checks. Operator may submit, view, cancel, and retry own tasks; viewer may view permitted tasks; only admin manages accounts, quotas, templates, publishing, and global audit.
- [ ] **Step 4: Add** session revocation checks to every authenticated request and record login/logout/revoke/password-reset events.
- [ ] **Step 5: Add** task audit events for create, enqueue, start, progress, cancel, retry, fail, complete, download, edit, and publish, with actor and worker identifiers where applicable.
- [ ] **Step 6: Run** focused authorization tests plus `tests/test_api.py`, `tests/test_sop_workflow.py`, and existing user-management tests; verify no regression in legacy compatibility paths.
- [ ] **Step 7: Commit** `feat: add operator authorization and audit trail`.

### Task 3: 数据库任务状态、幂等和配额服务

**Files:**
- Modify: `src/excel_auditor/persistence.py`
- Modify: `src/excel_auditor/service.py`
- Modify: `src/excel_auditor/api.py`
- Create: `src/excel_auditor/task_control.py`
- Create: `migrations/versions/20261016_0008_task_control_and_quotas.py`
- Test: `tests/test_task_control.py`

**Interfaces:**
- Produces `TaskControl.create_or_replay(...)`, `TaskControl.reserve(...)`, `TaskControl.release(...)`, `TaskControl.transition(...)`, and `TaskControl.cancel(...)`.
- `TaskControl` returns a task record containing `job_id`, `created_by`, `status`, `queue_name`, `attempt`, `worker_id`, `lease_expires_at`, `idempotency_key`, and quota counters.

- [ ] **Step 1: Write failing tests** for idempotent replay, per-user/workspace queued and running limits, daily quota, atomic reservation under concurrent calls, and release on failed enqueue.
- [ ] **Step 2: Run** `pytest tests/test_task_control.py -q`; verify failure.
- [ ] **Step 3: Add** database tables/indexes for `tasks`, `task_attempts`, `operator_quotas`, `quota_counters`, and `task_audit_events`; include uniqueness on `(workspace_id, idempotency_key)` and indexes by creator/status/updated time.
- [ ] **Step 4: Implement** transactional reservation and state transitions. A reservation must atomically check workspace and operator limits, write the task, increment counters, and append the create/enqueue audit event.
- [ ] **Step 5: Replace** file-registry idempotency and process-local active-job checks in comparison and workflow submission paths with `TaskControl`; keep the old file registry only as a migration fallback.
- [ ] **Step 6: Return** stable API errors: 409 for idempotency conflict with a different payload and 429 for quota exhaustion, including retry metadata but no internal database details.
- [ ] **Step 7: Run** focused tests plus `tests/test_api.py`, `tests/test_queueing.py`, and persistence tests.
- [ ] **Step 8: Commit** `feat: add transactional task control and quotas`.

### Task 4: Redis/RQ 多队列、Worker 租约和重试

**Files:**
- Modify: `src/excel_auditor/queueing.py`
- Modify: `src/excel_auditor/worker.py`
- Modify: `src/excel_auditor/api.py`
- Modify: `src/excel_auditor/workflow/routes.py`
- Create: `src/excel_auditor/worker_control.py`
- Test: `tests/test_concurrent_queueing.py`
- Test: `tests/test_worker_leases.py`

**Interfaces:**
- Produces `RedisJobQueue.enqueue_task(task_id, queue_name, payload)`, `WorkerControl.start(task_id, worker_id)`, `heartbeat(...)`, `complete(...)`, `fail(...)`, and `requeue_expired(...)`.

- [ ] **Step 1: Write failing tests** for queue selection (`normal`, `heavy`, `network`, `workflow`), one logical task per RQ job, retry backoff, heartbeat lease extension, expired-lease recovery, and stale-worker transition rejection.
- [ ] **Step 2: Run** the focused queue/lease tests; verify failure.
- [ ] **Step 3: Implement** queue selection based on workload type and `RQ` job metadata. Preserve a compatibility `excel-auditor` queue alias for existing deployments.
- [ ] **Step 4: Implement** WorkerControl lease transitions with database compare-and-set semantics, heartbeat interval shorter than the lease, bounded retries, exponential backoff, and terminal failure audit.
- [ ] **Step 5: Update** worker entrypoints so each worker processes one job at a time and reconstructs runtime from task metadata; do not pass file bytes through Redis.
- [ ] **Step 6: Update** API/workflow dispatch to reserve before enqueue, release on enqueue failure, and expose queue/attempt/progress fields in task status.
- [ ] **Step 7: Run** queue and worker tests with a fake Redis/database, then run the existing queue and workflow suites.
- [ ] **Step 8: Commit** `feat: add concurrent queues and worker leases`.

### Task 5: 流式上传、任务资源和并发 API

**Files:**
- Modify: `src/excel_auditor/api.py`
- Modify: `src/excel_auditor/storage.py`
- Modify: `src/excel_auditor/workflow/routes.py`
- Modify: `src/excel_auditor/workflow/repository.py`
- Test: `tests/test_concurrent_api.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces `stream_upload(upload_file, destination, max_bytes) -> UploadMetadata` and task-scoped artifact access that checks operator authorization before reading files.

- [ ] **Step 1: Write failing tests** for bounded streaming uploads, rejection of oversized files, simultaneous submissions from different operators, task visibility isolation, and artifact access after completion.
- [ ] **Step 2: Run** the focused tests; verify failure.
- [ ] **Step 3: Implement** chunked upload to local/object storage with incremental byte counting and SHA-256; persist only metadata in task rows and Redis payloads.
- [ ] **Step 4: Replace** process-local workflow initialization locks and background-only dispatch with database/task-control coordination while keeping synchronous development fallback when Redis is absent.
- [ ] **Step 5: Add** pagination/filtering for operator task history and admin global task/audit views; ensure all list queries filter by `local` and authorization.
- [ ] **Step 6: Run** focused concurrency/storage tests and the full API/workflow suites.
- [ ] **Step 7: Commit** `feat: support streamed uploads and concurrent task APIs`.

### Task 6: 前端运营账号管理、配额和任务状态

**Files:**
- Modify: `apps/web/src/workflow/api.ts`
- Modify: `apps/web/src/App.vue`
- Modify: `apps/web/src/workflow/Settings.vue`
- Modify: `apps/web/src/workflow/Workbench.vue`
- Create: `apps/web/src/workflow/OperatorManagement.vue`
- Modify: `apps/web/src/workflow/style.css`
- Test: `tests/test_frontend_contracts.py`

**Interfaces:**
- Produces typed API clients for `/operators`, `/tasks`, quota errors, queue progress, and audit summaries.

- [ ] **Step 1: Write failing frontend contract tests** in `tests/test_frontend_contracts.py` that inspect the built TypeScript/API contract for admin-only operator management, quota display, 429 handling, task queue/running status, and absence of tenant fields; run the repository's available Python test command and verify failure.
- [ ] **Step 3: Implement** operator list/create/disable/reset-password UI in Settings, with role and quota editing restricted to admin.
- [ ] **Step 4: Implement** Workbench task cards showing queued/running/progress/attempt/failure state and the current user’s task history.
- [ ] **Step 5: Handle** expired sessions, quota responses, stale task status, and permission errors with concise Chinese UI messages.
- [ ] **Step 6: Run** `pytest tests/test_frontend_contracts.py -q` and `pnpm build`; verify no tenant input or schema error appears on the login page.
- [ ] **Step 7: Commit** `feat: add operator management and task concurrency UI`.

### Task 7: 部署容量、迁移、初始化和文档

**Files:**
- Modify: `deploy/docker-compose.yaml`
- Modify: `deploy/docker-compose.prod.yaml`
- Modify: `deploy/env.example`
- Modify: `src/excel_auditor/admin_cli.py`
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `docs/project-development-guide.md`
- Create: `tests/test_deployment_capacity.py`

- [ ] **Step 1: Write failing configuration tests** for queue names, worker commands, default quota values, required production services, and single-admin bootstrap.
- [ ] **Step 2: Run** the configuration tests; verify failure.
- [ ] **Step 3: Configure** production workers for `normal`, `heavy`, `network`, and `workflow` queues, with initial capacity of 4 normal, 2 heavy, and 2 network/workflow workers; keep replica counts overridable by environment.
- [ ] **Step 4: Add** explicit environment settings for workspace/operator quotas, lease/retry timing, upload limits, and object storage; validate production startup refuses missing PostgreSQL/Redis/object storage.
- [ ] **Step 5: Extend** admin CLI to bootstrap the unique admin and create initial operators without accepting tenant input; make password changes explicit and auditable.
- [ ] **Step 6: Document** the capacity formula, concurrency tuning, migration procedure, operator lifecycle, audit retention, and rollback steps.
- [ ] **Step 7: Run** deployment/configuration tests, migration checks, the full pytest suite, and `pnpm build`.
- [ ] **Step 8: Commit** `feat: configure multi-operator production capacity`.

### Task 8: 集成验收和发布前验证

**Files:**
- Create: `tests/test_multi_operator_acceptance.py`
- Modify: `docs/implementation-status.md`

- [ ] **Step 1: Add** an end-to-end test that bootstraps `admin`, creates two operators, submits simultaneous normal/heavy/workflow jobs, verifies independent quotas and visibility, disables one operator, and checks audit records.
- [ ] **Step 2: Run** targeted acceptance tests against SQLite/fake Redis and record results.
- [ ] **Step 3: Run** the complete verification commands: `pytest -q`, `pnpm build`, migration upgrade/downgrade checks, and `git diff --check`.
- [ ] **Step 4: Update** implementation status with deployed defaults, known capacity assumptions, and operational commands.
- [ ] **Step 5: Commit** `test: verify multi-operator concurrent task flow`.

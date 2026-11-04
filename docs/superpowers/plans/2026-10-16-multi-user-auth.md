# ExcelFlow 单管理员账号认证 Implementation Plan

> This plan was narrowed to a single browser administrator account after review. The existing internal workspace field remains only for data compatibility.

**Goal:** 为 ExcelFlow 增加单管理员账号密码登录、服务端会话、CSRF 和防暴力破解能力，同时保留现有 Bearer Token 服务间认证。

**Architecture:** 浏览器使用服务端随机会话 Cookie，数据库只保存会话摘要和 CSRF 摘要；密码使用 Argon2id 哈希。API 中间件优先解析浏览器会话，未命中时继续支持现有 Bearer Token；所有业务资源继续执行租户过滤，写请求执行角色和 CSRF 校验。

**Tech Stack:** FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL/SQLite、Redis/RQ、Vue 3、TypeScript、Argon2id。

**Spec:** `docs/superpowers/specs/2026-10-16-multi-user-auth-design.md`

## Global Constraints

- 不保存明文密码、完整会话令牌、完整 CSRF Token 或完整 IP。
- 浏览器会话 Cookie 必须使用 HttpOnly、SameSite=Lax；生产环境使用 Secure。
- 所有 Cookie 会话下的 POST、PUT、PATCH、DELETE 请求必须验证 CSRF Token。
- 现有 Bearer Token 服务间认证和 Worker 流程必须保持兼容。
- 所有业务对象必须继续按 tenant_id 隔离；角色检查不能替代租户过滤。
- 认证失败消息不能区分用户不存在和密码错误。
- 先写失败测试，再写生产代码；每个任务完成后运行对应测试。

## Review Focus

- 会话 Cookie 被伪造或重放时，必须因服务端 token_hash/过期校验而失败；由认证服务测试覆盖。
- Cookie 会话写请求缺少或伪造 CSRF Token 时，必须返回 403；由 CSRF 测试覆盖。
- 禁用用户后已有会话必须立即失效；由用户管理测试覆盖。
- 租户 A 的用户访问租户 B 资源必须返回 404 或 403，不能泄露对象存在性；由权限测试覆盖。
- Bearer Token 调用仍应通过原有 API 和 Worker 测试；由回归测试覆盖。

---

### Task 1: 认证依赖和数据模型

**Files:**
- Modify: `pyproject.toml`，增加固定版本 `argon2-cffi`。
- Modify: `src/excel_auditor/persistence.py`，增加 Tenant、User、AuthSession、AuthEvent 表和访问方法。
- Create: `src/excel_auditor/auth_models.py`，定义角色、状态、请求/响应模型和安全常量。
- Create: `migrations/versions/<timestamp>_multi_user_auth.py`。
- Test: `tests/test_auth_persistence.py`。

**Interfaces:**
- Produces `UserRepository.create_user`, `UserRepository.get_by_username`, `UserRepository.set_status`, `SessionRepository.create`, `SessionRepository.get_active`, `SessionRepository.revoke`。
- 认证模型使用 `owner | admin | operator | viewer` 和 `active | disabled`。

- [x] 写测试：创建租户和用户；数据库只保存密码哈希；租户内用户名唯一；禁用用户不能创建新会话。
- [x] 运行 `pytest tests/test_auth_persistence.py -q`，确认在实现前失败。
- [x] 实现 SQLAlchemy 表、索引、租户过滤和 Alembic 迁移。
- [x] 使用 Argon2id 校验密码，不把密码放入模型序列化结果。
- [x] 再次运行测试确认通过。

### Task 2: 密码、会话和 CSRF 服务

**Files:**
- Create: `src/excel_auditor/auth_service.py`。
- Modify: `src/excel_auditor/security_config.py`，增加 Cookie、安全密钥、会话 TTL 和登录限流校验。
- Test: `tests/test_auth_service.py`。

**Interfaces:**
- `hash_password(password: str) -> str`。
- `verify_password(password: str, encoded_hash: str) -> bool`。
- `create_session(user_id: str, tenant_id: str, ip: str, user_agent: str) -> SessionCredentials`。
- `authenticate_session(token: str) -> AuthIdentity | None`。
- `validate_csrf(session_id: str, token: str) -> bool`。
- `revoke_session(session_id: str) -> None`。

- [x] 写测试：正确密码通过、错误密码失败、随机会话令牌不落库、过期/撤销会话失败、CSRF 错误失败。
- [x] 运行 `pytest tests/test_auth_service.py -q`，确认失败。
- [x] 实现 Argon2id、SHA-256 摘要、时间过期和服务端撤销。
- [x] 实现按用户名/IP 的失败计数和短期锁定。
- [x] 运行测试确认通过。

### Task 3: API 会话中间件和认证路由

**Files:**
- Modify: `src/excel_auditor/api.py`，接入 Cookie 会话、认证路由和 CSRF 校验。
- Create: `src/excel_auditor/auth_routes.py`，实现登录、退出、会话、CSRF 和会话撤销接口。
- Modify: `src/excel_auditor/workflow/routes.py` 和字段核对/通用 API 的权限依赖。
- Test: `tests/test_auth_api.py`。

**Interfaces:**
- `POST /api/v1/auth/login`。
- `POST /api/v1/auth/logout`。
- `GET /api/v1/auth/session`。
- `GET /api/v1/auth/csrf`。
- `POST /api/v1/auth/sessions/revoke-all`。
- `require_identity`, `require_roles(*roles)` 和 `require_csrf` 依赖。

- [x] 写测试：登录成功设置 HttpOnly/SameSite Cookie；错误凭据统一 401；退出使会话失效；无 CSRF 的写请求返回 403；Bearer Token 仍可用。
- [x] 运行 `pytest tests/test_auth_api.py -q`，确认失败。
- [x] 实现 Cookie 会话优先、Bearer Token 兼容的请求身份解析。
- [x] 对认证响应设置 `Cache-Control: no-store` 和 `Pragma: no-cache`。
- [x] 对状态变化接口执行 CSRF 校验和角色校验。
- [x] 运行测试确认通过。

### Task 4: 用户管理和初始化管理员

**Files:**
- Create: `src/excel_auditor/user_routes.py`。
- Modify: `src/excel_auditor/api.py`，注册用户管理路由。
- Modify: `src/excel_auditor/startup.py` 或新增 `src/excel_auditor/admin_cli.py`，提供初始化命令。
- Test: `tests/test_user_management.py`。

**Interfaces:**
- `GET /api/v1/users`。
- `POST /api/v1/users`。
- `PATCH /api/v1/users/{user_id}`。
- `POST /api/v1/users/{user_id}/reset-password`。
- `POST /api/v1/users/{user_id}/disable`。
- `POST /api/v1/users/{user_id}/enable`。
- `excel-auditor-admin create`。

- [x] 写测试：owner/admin 可管理同租户用户；operator/viewer 被拒绝；禁用用户撤销现有会话；不同租户不能查询用户。
- [x] 运行 `pytest tests/test_user_management.py -q`，确认失败。
- [x] 实现用户创建、角色修改、密码重置、启用/禁用和撤销会话。
- [x] 实现一次性管理员初始化，不把初始密码写入日志或数据库明文。
- [x] 运行测试确认通过。

### Task 5: 前端登录门禁和用户管理页面

**Files:**
- Create: `apps/web/src/auth/LoginPage.vue`。
- Create: `apps/web/src/auth/UserManagement.vue`。
- Modify: `apps/web/src/App.vue`，支持 `/login` 和登录后目标路由。
- Modify: `apps/web/src/workflow/api.ts`，Cookie 请求、CSRF Token 获取和 401 处理。
- Modify: `apps/web/src/workflow/style.css` 或新建 `apps/web/src/auth/style.css`。
- Test: `pnpm --dir apps/web build`。

**Interfaces:**
- 登录页面提交 `{username, password}`，成功后触发 `authenticated` 事件。
- API 请求使用 `credentials: "include"`，写请求自动携带 `X-CSRF-Token`。
- 401 自动清理内存状态并跳转 `/login`。

- [x] 先增加登录页面、会话状态和 API 客户端类型，运行构建确认缺失接口/类型失败或行为不完整。
- [x] 实现用户名密码表单、错误提示、登录中状态和退出按钮。
- [x] 禁止在 localStorage/sessionStorage 保存密码、会话令牌和 CSRF Token。
- [x] 实现角色控制：隐藏无权限入口，同时依赖后端权限拒绝作为最终边界。
- [x] 运行 `pnpm --dir apps/web build` 确认通过。

### Task 6: 安全配置、部署和回归验证

**Files:**
- Modify: `deploy/docker-compose.prod.yaml`，加入 Cookie 安全、初始化和会话配置。
- Modify: `deploy/nginx.conf`，确认 HTTPS 代理、CSP 和安全响应头。
- Modify: `docs/deployment.md` 和 `docs/project-development-guide.md`，补充账号初始化、Cookie 和回滚说明。
- Test: `tests/test_api.py`、`tests/test_infrastructure_integration.py`、完整 Pytest。

- [x] 写部署配置测试：生产环境缺少认证密钥、数据库或安全连接时启动失败。
- [x] 运行认证相关测试和现有基础设施测试，确认新配置前失败或覆盖缺口。
- [x] 实现生产 Cookie Secure、HTTPS、会话 TTL 和启动校验。
- [x] 运行完整 `pytest -q`。
- [x] 运行 `pnpm --dir apps/web build`。
- [x] 执行浏览器登录、退出、禁用用户、跨租户访问和旧 Bearer Token 流程验收。

### Task 7: 最终安全审查

**Files:**
- Review all changed authentication files and migration.
- Test: full backend suite, frontend build, API smoke checks.

- [x] 检查日志、错误响应、报告和审计中没有密码、Cookie、完整 CSRF Token 或完整 IP。
- [x] 检查所有写 API 的角色和 CSRF 依赖覆盖范围。
- [x] 检查用户、任务、模板、报告和文件下载都带租户过滤。
- [x] 检查生产配置默认拒绝不安全 Cookie 和明文外部连接。
- [x] 运行完整验证并记录结果。

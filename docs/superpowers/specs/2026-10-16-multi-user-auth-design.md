# ExcelFlow 单管理员账号登录设计

> 2026-10-16 复核后采用单管理员方案。下文保留的角色与会话安全细节仅作为实现背景；网页登录不要求租户标识，也不开放多用户注册。

## 目标

为 ExcelFlow 提供面向浏览器的单管理员账号登录。管理员通过账号和密码登录，业务数据继续使用内部 `local` 工作空间字段；现有 Bearer Token 继续用于服务间调用和自动化 API。

## 范围

本次包含：

- 用户、租户、角色和服务端会话。
- Argon2id 密码哈希，不保存明文密码。
- 登录、退出、当前会话查询和会话撤销。
- `owner`、`admin`、`operator`、`viewer` 四类角色。
- 用户所属租户隔离和资源访问校验。
- 登录失败限流、账号禁用和审计事件。
- HttpOnly 会话 Cookie、CSRF Token 和安全响应头。
- `/login` 网页入口、登录门禁、退出按钮和用户管理页面。
- Alembic 迁移、初始化管理员命令和认证回归测试。

本次不包含：

- OAuth/OIDC、LDAP 或企业身份平台联邦登录。
- 邮件找回密码、短信验证码和 MFA。
- 公共注册。
- 用浏览器账号会话替换服务间 Bearer Token。

## 认证架构

浏览器登录使用服务端会话：

1. 浏览器向 `POST /api/v1/auth/login` 提交用户名和密码。
2. API 使用 Argon2id 校验密码。
3. API 生成至少 32 字节随机会话令牌，只把令牌的 SHA-256 摘要保存到数据库或 Redis。
4. 浏览器收到 `HttpOnly` 会话 Cookie，不读取或保存令牌文本。
5. 后续请求由 API 根据 Cookie 查询会话、用户、租户和角色。
6. 退出或禁用用户时删除/撤销服务端会话。

Cookie 约束：

- 名称：`excel_flow_session`。
- `HttpOnly=true`。
- 生产环境 `Secure=true`。
- `SameSite=Lax`。
- `Path=/`。
- 默认有效期 8 小时，滑动续期不超过 24 小时。
- 登录、退出、当前会话接口都返回 `Cache-Control: no-store`。

## 数据模型

### tenants

- `id`
- `name`
- `status`：`active` 或 `disabled`
- `created_at`

### users

- `id`
- `tenant_id`
- `username`：租户内唯一、大小写折叠比较
- `password_hash`
- `role`
- `status`：`active` 或 `disabled`
- `failed_login_count`
- `locked_until`
- `last_login_at`
- `created_at`
- `updated_at`

### auth_sessions

- `id`
- `user_id`
- `token_hash`
- `csrf_hash`
- `created_at`
- `last_seen_at`
- `expires_at`
- `revoked_at`
- `ip_hash`
- `user_agent_hash`

### auth_events

记录登录成功、登录失败、退出、管理员创建用户、禁用用户、重置密码和撤销会话。事件中不保存密码、Cookie 或完整 IP。

## 角色权限

| 角色 | 权限 |
|---|---|
| owner | 当前租户全部管理能力，包括用户、字段配置、模板和任务 |
| admin | 用户管理、字段配置、模板和任务管理 |
| operator | 创建、处理、查看和下载业务任务 |
| viewer | 只读查看任务和报告，不允许上传、编辑、发布或下载敏感产物 |

所有资源查询仍必须同时校验 `tenant_id`。角色检查只作为权限层，不能替代租户过滤。

## API

### 浏览器认证

- `POST /api/v1/auth/login`
  - 请求：`username`、`password`。
  - 成功：设置会话 Cookie，返回脱敏用户、租户和角色。
  - 失败：统一返回 401，不区分“用户不存在”和“密码错误”。
- `POST /api/v1/auth/logout`
  - 撤销当前会话并清除 Cookie。
- `GET /api/v1/auth/session`
  - 返回当前用户、租户、角色和会话过期时间。
- `GET /api/v1/auth/csrf`
  - 返回当前会话绑定的 CSRF Token，不返回会话令牌。
- `POST /api/v1/auth/sessions/revoke-all`
  - 管理员撤销指定用户的全部会话。

### 用户管理

- `GET /api/v1/users`
- `POST /api/v1/users`
- `PATCH /api/v1/users/{id}`
- `POST /api/v1/users/{id}/reset-password`
- `POST /api/v1/users/{id}/disable`
- `POST /api/v1/users/{id}/enable`

用户管理接口只允许 `owner` 和 `admin`，并强制绑定当前租户。

### 服务间认证

现有 `Authorization: Bearer ...` 继续保留。服务间 Token 不创建浏览器 Cookie，不参与网页登录，也不在网页接口响应中返回。

## CSRF 和限流

- 所有 `POST`、`PUT`、`PATCH`、`DELETE` 请求在 Cookie 会话下必须提供 `X-CSRF-Token`。
- CSRF Token 与会话绑定，服务端只保存摘要。
- 登录接口按用户名和来源地址执行失败限流。
- 连续失败时短暂锁定账号，但响应仍使用统一的认证失败消息。
- 认证失败不记录密码、Cookie 或完整请求体。

## 前端行为

- `/login` 显示用户名和密码表单。
- 登录成功后进入原始目标页面或 `#workbench`。
- 页面启动先调用 `/api/v1/auth/session`。
- 未登录只能访问登录页。
- API 返回 401 时清除前端状态并跳回 `/login`。
- API 写请求自动获取并发送 `X-CSRF-Token`。
- 页面不使用 `localStorage` 保存密码、会话令牌或 CSRF Token。
- 顶部显示当前用户名和租户，提供退出按钮。

## 初始化和迁移

- 增加 Alembic 迁移创建认证表和索引。
- 增加 `excel-auditor-admin create` 管理命令。
- 初始化命令从交互式输入或一次性环境变量读取初始密码，写入 Argon2id 哈希后立即清除环境变量使用痕迹。
- 生产部署必须在 HTTPS 下运行，并通过安全的部署流程创建首个 `owner`。

## 安全验收标准

- 数据库中不存在明文密码。
- 登录失败不泄露用户是否存在。
- Cookie 带 `HttpOnly`、`SameSite=Lax`，生产带 `Secure`。
- 登录、退出和会话响应不可缓存。
- 无 CSRF Token 的写请求被拒绝。
- 禁用用户的现有会话立即失效。
- 租户 A 的用户无法读取或修改租户 B 的任务、模板、报告和用户。
- `viewer` 无法执行写操作或下载敏感产物。
- 现有 Bearer Token API 和 Worker 流程继续通过原有测试。

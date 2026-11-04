# 运营账号任务隔离与提交验收

日期：2026-10-16

本次接续会话中尚未完成的两个请求：运营账号只能查看自己的任务，以及使用 `operator_test` 实际提交测试任务。当前工作区已有权限实现，本次复核现有代码并完成本地验收，没有重新改写权限逻辑。

## 自动回归

执行：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_task_permissions.py tests/test_auth_api.py tests/test_operator_management.py tests/test_workflow_executions.py tests/test_workflow_migration_and_paging.py -o addopts='' -q
pnpm --dir apps/web build
```

结果：43 通过、1 跳过；前端类型检查和构建通过。覆盖列表筛选、搜索、分页计数、其他账号详情及文件访问、批量删除原子性、管理员全局访问、幂等请求账号隔离和持久后台执行。

## 真实本地 HTTP 与浏览器验收

- 服务：API `http://127.0.0.1:8000`，页面 `http://127.0.0.1:5173/`，启用账号认证。
- 使用已有 `operator_test` 登录；没有修改账号密码、角色或启停状态。
- 提交前列表为 0 条，不显示已有的其他账号任务；访问其他账号任务详情、维护数据和维护文件下载均返回 404；账号管理接口返回 403。
- 上传内置示例表，任务名称为“运营账号提交验收-20261016”，返回 HTTP 202 和 `processing` 状态。
- 任务 ID：`task_2deea751ea3fb09061bbd41976ed3c31`，创建者为当前运营账号。
- 持久执行记录 `parse_task` 为 `succeeded`，尝试 1 次，无错误；任务进入 `mapping`（待确认字段）。
- 提交后该运营账号列表仅 1 条，浏览器实际登录后显示同一测试任务。
- 保留该测试任务，方便用户继续检查字段确认流程。

原始 HTTP 验收结果及截图保存在本地忽略目录：`var/acceptance/operator-continuation-20261016.json` 和同名 `.png`。

本次运行使用本地 SQLite 与后台执行模式，没有配置 Redis；因此上述提交验证证明持久执行记录和本地处理正常，不代表完成真实 Redis/RQ 或生产容量验收。

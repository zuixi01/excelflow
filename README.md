# Excel Standard Auditor

配置驱动的 Excel 标准数据核验与受控修复平台。实现采用 Python/FastAPI 比对核心、PostgreSQL/Redis/RQ/MinIO 基础设施、Vue 3 前端，以及 .NET 8 Open XML SDK 高保真渲染器。

## 已实现能力

- 一个工作台内切换“SOP 整理”和“平台字段核对”：保留原 SOP 维护与导出流程，平台核对模式直接读取接口字段，无需选择模板。使用和接口接入见 [平台字段核对说明](docs/field-reconciliation.md)。
- 商品 SOP 工作台：导入原表、确认字段、维护校验、Excel 回传、生成与预览最终表；支持模板学习、可选样例试运行和不可变版本发布。操作与边界见 [SOP 使用说明](docs/sop-user-guide.md)。
- 严格、版本化的 YAML/JSON 规则；所有嵌套配置拒绝未知字段，发布版本不可变。
- XLSX/XLSM 安全预检、表头自动定位、工作表/表头别名、模糊候选人工审核。
- 单主键/联合主键匹配，空键、重复键、缺失/多余记录隔离。
- 字符串、整数、Decimal、日期时间、布尔、枚举、集合、JSON 的类型化比较，以及字段和跨字段质量规则。
- 日期/数值显示格式可按字段显式授权规范化；默认关闭，修复不改写单元格值或公式。
- 上传 JSON/CSV、请求内 JSON、受管 HTTP 分页数据源；固化、回读并校验 SHA-256 标准快照。
- 标色、批注、插列、填值、追加记录和内置报告；维护受支持的公式、Table、筛选、验证、合并区域、定义名称和宏部件，对复杂结构明确失败或转人工。
- JSON/HTML/Excel 报告、不可预测差异 ID、可复现语义投影、逐项修复结果与审计记录。
- Bearer 鉴权、工作空间级规则/草稿/任务/下载隔离、并发配额、软删除和延迟清理；网页端使用单个管理员账号密码登录。
- PostgreSQL、Redis/RQ、MinIO/S3 适配器、Alembic 迁移、健康检查和指标。
- 19 个固定 Golden 工作簿（含 10,000×20 大文件与固定标准快照）、属性/安全/可复现/性能测试，以及真实基础设施和 LibreOffice CI 作业。
- 锁定的 Python、Node 和 NuGet 依赖；许可证清单、SBOM、漏洞扫描和“扫描通过后才推送”的 SHA 镜像发布流程。

## Windows 双击启动

已安装项目依赖且 Docker Desktop 可用时，双击根目录的 `启动项目.cmd`，会启动本地 PostgreSQL、Redis、API、独立 RQ Worker、恢复进程和前端，并打开 <http://127.0.0.1:5173/>。重复双击会复用当前项目的服务，不会重复启动。PostgreSQL 和 Redis 只监听本机回环地址。

启动设置固定在 `tools/Start-ExcelFlow-Postgres.ps1`：本地开发环境、每次开启 `EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP=1`、使用现有 `var` 文件目录、PostgreSQL 端口 55432、Redis 端口 56379、API 端口 8000 和前端端口 5173。API 只接收和登记主工作流任务；RQ 的 `workflow` 和 `workflow-export` Worker 分别执行导入与维护/导出，恢复进程补发中断的任务。Docker Compose 文件是 `deploy/docker-compose.local-infra.yaml`，本地数据库凭据位于未纳入版本控制的 `var/local-infra.env`。模型配置和系统账户保护的密钥继续保存在原数据目录，不在脚本中填写密钥，也不重置账号或模型配置；更换模型地址时仍按页面要求重新填写密钥。HTTP 兼容仅用于本地开发，不改变生产环境的 HTTPS 策略。

日志在 `var/logs/`，其中 `rq-workflow-*.stderr.log`、`rq-workflow-export-*.stderr.log` 是独立 Worker 日志。命令行验证时可运行 `powershell -NoProfile -ExecutionPolicy Bypass -File tools/Start-ExcelFlow-Postgres.ps1 -NoBrowser`。Worker 暂停时已提交任务留在 PostgreSQL 执行表及 Redis 队列中，重新启动 Worker 后继续处理。本地 Windows 使用 RQ 的 `SimpleWorker`；生产环境使用 Linux 上的普通 RQ Worker。旧 SQLite 数据库保留在 `var/auth.sqlite3`、`var/workflow/workflow.sqlite3`，迁移前的一致性副本保留在 `var/migration-backup/`。

## 本地验证

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install uv==0.12.5
.\.venv\Scripts\uv sync --frozen --extra test --extra security

dotnet restore services\excel_renderer\ExcelRenderer.csproj --locked-mode
dotnet publish services\excel_renderer\ExcelRenderer.csproj -c Release -r win-x64 --self-contained true --no-restore -o .renderer
$env:EXCEL_RENDERER_COMMAND = (Resolve-Path .renderer\ExcelRenderer.exe)
.\.venv\Scripts\python -m pytest -q

pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web build
```

示例规则在 `configs/examples/employee-roster.yaml`。开发环境可使用 `deploy/docker-compose.yaml`。生产服务器按 [8 核 16 GiB 部署手册](docs/production-8c16g.md) 使用 `deploy/production.sh`：拉取通过 CI 的 Git 标签，预检密钥与证书，启动生产 Compose 和 HTTPS 入口；服务器不执行应用镜像构建。

## 商家商品合并

`POST /api/v1/merchant-product-reconciliations` 接收一个包含多个商家工作表的 `.xlsx` 或 `.xlsm` 文件。系统自动识别商品表、映射固定字段并生成新的 `商品汇总`、`问题清单` 和隐藏的 `_来源追踪` 工作表。接口不修改上传文件。

开发和验收阶段可通过可选表单字段 `platform_json` 传入平台商品 fixture，通过 `name_match_threshold` 调整商品名称匹配阈值，默认值为 `90`。真实平台接口接入后只需实现平台商品源适配器，抽取、匹配、比较和输出逻辑保持不变。

实现状态见 `docs/implementation-status.md`，逐项验收证据见 `docs/requirements-evidence-matrix.md`，构建与回滚规则见 `docs/deployment.md`。

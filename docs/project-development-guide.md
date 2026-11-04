# ExcelFlow 项目开发交接文档

> 文档用途：为后续开发、测试、部署和业务联调提供统一入口。
> 适用版本：当前工作区代码（2026-10-16）。本文档描述仓库中的实际实现，不代表所有功能都已经完成生产验收。

## 1. 项目定位

ExcelFlow 是一个配置驱动的 Excel 数据核验、整理、受控修复和结果导出平台。它把 Excel 处理拆成“规则/字段配置、文件安全预检、标准数据快照、字段映射、类型化比较、人工审核、Excel 渲染、报告和审计”几个可追踪阶段。

当前代码包含四条业务链路：

1. 通用标准核验：按版本化规则将供应商 Excel 与 JSON、CSV 或受管 HTTP 标准数据进行比较，并生成差异、报告和修复后的 Excel。
2. 商品 SOP 整理：上传供应商商品表，自动发现表头和字段，人工确认映射，在维护工作台中修改数据，最后生成标准商品表。
3. 平台字段核对：从平台接口读取动态字段标准，识别供应商字段，执行必填、类型、枚举和逐行标准值核对。
4. 商品/商家商品扩展：按商品目录、商家工作表和名称匹配规则生成商品汇总、问题清单和来源追踪数据。

项目的核心原则是：配置版本不可变、标准数据先快照、所有自动修复必须可解释、无法证明安全的结构进入人工审核、租户和文件访问严格隔离。

## 2. 总体架构

    flowchart LR
    User[运营人员] --> Web[Vue 3 工作台]
    Web --> API[FastAPI API]
    API --> Auth[Bearer 鉴权 / 租户隔离]
    API --> Queue{Redis + RQ}
    Queue --> Worker[Python Worker]
    API -.开发环境.-> Background[FastAPI BackgroundTasks]
    Worker --> Inspect[Excel 安全预检]
    Inspect --> Normalize[标准化 / 快照]
    Normalize --> Compare[Python 比对引擎]
    Compare --> Manifest[修复 Manifest]
    Manifest --> Renderer[.NET 8 OpenXML Renderer]
    Compare --> Reports[JSON / JSONL / HTML / Excel 报告]
    API --> DB[(PostgreSQL / SQLite)]
    API --> ObjectStore[(MinIO / S3)]
    Renderer --> Excel[结果 Excel]

后端总装配在 src/excel_auditor/api.py 中完成：

- 创建 AuditService、数据库、对象存储、受管 HTTP 标准源和任务队列。
- 注册通用核验路由。
- 注册 SOP/商品工作流路由。
- 注册平台字段核对路由。
- 统一处理鉴权、健康检查、指标和异常响应。

## 3. 技术栈和版本

| 层级 | 技术 | 主要用途 |
|---|---|---|
| 后端语言 | Python 3.12+ | API、工作流、规则、比对和报告 |
| Web API | FastAPI 0.141.1、Uvicorn 0.52.4 | REST API、文件上传、健康检查 |
| 数据模型 | Pydantic 2.13.4 | 严格配置、请求、响应和字段契约 |
| Excel 读取 | openpyxl 3.1.5 | 表头发现、普通单元格读取、SOP 维护 |
| 数据处理 | Polars、Pandera、DataComPy | 大数据量、校验和结构化比较扩展 |
| 类型解析 | Decimal、datetime、严格 JSON | 精确数值、日期、布尔、集合、JSON |
| 模糊匹配 | RapidFuzz 3.14.5 | 表头候选和字段候选匹配 |
| 数据库 | PostgreSQL 17；开发环境 SQLite | 任务、规则、快照、差异和审计 |
| ORM / 迁移 | SQLAlchemy 2.0.52、Alembic 1.19.1 | 数据访问和版本迁移 |
| 异步任务 | Redis 8、RQ 2.11.0 | API 与 Worker 解耦 |
| 对象存储 | MinIO / S3、boto3 | 结果文件和标准快照持久化 |
| 前端 | Vue 3.5.20、TypeScript 5.9.2、Vite 8.2.2 | 工作台和任务详情 |
| UI | lucide-vue-next | 图标 |
| Excel 生产渲染 | .NET 8、DocumentFormat.OpenXml 3.5.1 | 保留 OpenXML 结构并执行安全修复 |
| 部署 | Docker Compose、Nginx | API、Worker、Web 和基础设施 |
| 测试 | Pytest、Hypothesis、Golden Workbook、LibreOffice | 单元、属性、回归和真实 Excel 验收 |

依赖清单：

- Python：pyproject.toml
- Web：apps/web/package.json
- Renderer：services/excel_renderer/ExcelRenderer.csproj

## 4. 目录和模块职责

src/excel_auditor/
├── api.py                    # FastAPI 应用、鉴权、通用 API 和任务 API
├── models.py                 # RuleSet、SheetRule、ColumnRule、Difference 等核心模型
├── engine.py                 # 表头映射、主键匹配、字段比较和修复计划
├── workbook.py               # XLSX/XLSM 安全检查和工作簿快照
├── normalization.py          # 字符串、数字、日期、JSON、枚举等规范化
├── service.py                # 通用任务生命周期、快照、报告和渲染编排
├── rendering.py              # Python Renderer 抽象和 .NET Renderer 适配器
├── standard_sources.py       # 受管 HTTP 标准源、白名单和 SSRF 防护
├── persistence.py            # PostgreSQL/SQLAlchemy 持久化模型
├── queueing.py               # Redis/RQ 队列适配器
├── storage.py                # 本地和 S3/MinIO 文件存储
├── worker.py                 # RQ Worker 入口和各类后台任务
├── workflow/                 # 商品 SOP、模板、字段配置和维护任务
├── field_reconciliation.py   # 平台字段核对核心逻辑
├── field_reconciliation_routes.py
└── product_workflow/         # 商品目录、商家商品和商品标准化扩展

apps/web/src/
├── App.vue                   # Hash 路由和主布局
├── workflow/                 # SOP 工作台、任务详情、字段配置和模板中心
└── reconciliation/           # 平台字段核对工作台

services/excel_renderer/
└── Program.cs                # .NET OpenXML 渲染器和 Manifest 协议

deploy/                       # Dockerfile、Compose、Nginx 和生产配置
migrations/                   # Alembic 迁移
tests/                        # 单元、集成、Golden、性能和安全测试
tools/                        # 演示、浏览器验收和验证脚本
docs/                         # 设计、部署、验收和交接文档

## 5. 通用标准核验流程

### 5.1 业务流程图

    sequenceDiagram
    participant U as 用户
    participant W as Web
    participant A as FastAPI
    participant Q as Redis/RQ
    participant S as AuditService
    participant R as Renderer

    U->>W: 上传 Excel、规则版本和标准数据
    W->>A: POST /api/v1/comparisons
    A->>A: 校验租户、规则、大小和幂等键
    A->>Q: 入队 comparison job
    Q->>S: 执行后台任务
    S->>S: Excel 安全预检
    S->>S: 读取并固化标准快照
    S->>S: 表头映射、主键连接、字段比较
    S->>R: 提交渲染 Manifest
    R-->>S: 输出修复结果和操作结果
    S-->>A: 更新状态、报告、产物和审计
    W->>A: GET /api/v1/comparisons/{job_id}
    A-->>W: 状态、统计和下载地址

### 5.2 API 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | /api/v1/schemas/publish | 发布规则版本 |
| POST | /api/v1/schemas/import | 导入 JSON/YAML 规则 |
| POST | /api/v1/workbooks/precheck | 只做工作簿结构预检 |
| POST | /api/v1/comparisons | 创建标准核验任务 |
| GET | /api/v1/comparisons/{id} | 查询任务状态 |
| GET | /api/v1/comparisons/{id}/differences | 分页查询差异 |
| GET | /api/v1/comparisons/{id}/artifacts/{artifact} | 下载 Excel、报告和 Manifest |
| POST | /api/v1/comparisons/{id}/cancel | 请求取消 |
| DELETE | /api/v1/comparisons/{id} | 软删除任务 |

创建核验任务的入口是 src/excel_auditor/api.py 中的 create_comparison。它负责读取上传文件、解析参数、计算幂等指纹、创建任务目录，然后根据是否配置 REDIS_URL 选择 RQ 或 FastAPI 后台任务。

### 5.3 核心处理阶段

AuditService.run() 位于 src/excel_auditor/service.py:157，主流程如下：

workbook = inspect_workbook(input_path, rules)

standard, metadata = load_standard_data(...)
snapshot = create_snapshot(standard, job_directory, metadata)
standard = load_snapshot(snapshot, spill_after_records=threshold)

comparison = compare_workbook(
    workbook,
    standard,
    rules,
    job_id=job_id,
    spill_to_disk=True,
)

write_json_report(report, directory / "report.json")
write_differences_jsonl(report.differences, directory / "differences.jsonl")

render = renderer.render(
    input_path,
    output_path,
    workbook,
    rules,
    comparison,
    report_payload,
)

如果存在以下情况，任务会进入 manual_review 或 report_only，不会强行改写文件：

- 表头或工作表存在歧义。
- 工作簿包含不支持的复杂结构。
- 公式、宏、外部链接或日期时区无法安全处理。
- 数值无法在 Excel 单元格中精确往返。
- 差异数量或文件规模超过自动渲染门槛。

## 6. 商品 SOP 整理流程

### 6.1 用户流程图

    flowchart TD
    A[管理员配置固定字段] --> B[保存字段草稿]
    B --> C[预览并发布字段版本]
    C --> D[用户上传供应商 Excel]
    D --> E[识别工作表、多级表头和数据行]
    E --> F[自动建议字段映射]
    F --> G{人工确认映射}
    G -->|有问题| F
    G -->|确认| H[维护数据]
    H --> I[保存并校验]
    I --> J{是否还有阻断问题}
    J -->|有| H
    J -->|无| K[生成标准商品表]
    K --> L[预览 / 下载 / 审计]

### 6.2 API 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/v1/field-profile | 读取字段草稿和已发布版本 |
| PUT | /api/v1/field-profile | 保存字段配置 |
| POST | /api/v1/field-profile/preview | 校验字段配置和输出结构 |
| POST | /api/v1/field-profile/publish | 发布字段版本 |
| POST | /api/v1/standardization-tasks | 创建新式商品标准化任务 |
| POST | /api/v1/workflow-tasks/{id}/mapping/analyze | 重新分析字段 |
| POST | /api/v1/workflow-tasks/{id}/mapping | 确认映射 |
| GET | /api/v1/workflow-tasks/{id}/maintenance | 获取维护记录 |
| PATCH | /api/v1/workflow-tasks/{id}/maintenance | 编辑并校验记录 |
| POST | /api/v1/workflow-tasks/{id}/maintenance/returns | 预览 Excel 回传 |
| POST | /api/v1/workflow-tasks/{id}/maintenance/returns/apply | 应用回传结果 |
| POST | /api/v1/workflow-tasks/{id}/exports | 生成标准输出 |

### 6.3 SOP 状态机

    stateDiagram-v2
    [*] --> processing: 上传任务
    processing --> mapping: 解析完成
    processing --> failed: 解析失败
    mapping --> mapping: 重新分析 / 重开映射
    mapping --> maintenance: 映射确认且存在问题
    mapping --> ready: 映射确认且无问题
    maintenance --> maintenance: 编辑、回传、重新校验
    maintenance --> ready: 阻断问题清除
    ready --> processing: 创建导出任务
    processing --> completed: 导出完成
    failed --> processing: 重试
    processing --> cancelled: 取消
    cancelled --> processing: 重新处理

实现位置：

- 解析：src/excel_auditor/workflow/tasks.py:145
- 编辑和校验：src/excel_auditor/workflow/tasks.py:341
- 导出：src/excel_auditor/workflow/tasks.py:562
- 路由：src/excel_auditor/workflow/routes.py
- Excel 发现和转换：src/excel_auditor/workflow/excel.py

每个任务保存创建时的字段快照、模板版本和配置摘要。后续发布新配置不会改变进行中的任务。

## 7. 平台字段核对流程

### 7.1 接口流程图

    sequenceDiagram
    participant U as 运营人员
    participant W as 核对工作台
    participant A as FastAPI
    participant P as 平台字段接口
    participant F as FieldReconciliationService

    U->>W: 选择来源、填写范围、上传 Excel
    W->>A: POST /field-reconciliations
    A->>F: 创建 queued 任务
    F->>P: 请求字段契约和可选标准记录
    P-->>F: schema_version、fields、primary_key、records
    F->>F: 固化 field-standard-snapshot.json
    F->>F: 识别工作表和供应商列
    F->>F: 名称匹配 / AI 语义建议
    F->>F: 字段校验和逐行核对
    F-->>A: report.json、result.xlsx、snapshot
    A-->>W: completed 或 manual_review
    U->>W: 确认字段映射
    W->>A: POST /{id}/mappings/confirm
    A->>F: 复用同一标准快照重新处理

### 7.2 API 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /api/v1/field-reconciliations/connections | 获取当前租户可用平台来源 |
| POST | /api/v1/field-reconciliations | 上传 Excel 并创建核对任务 |
| GET | /api/v1/field-reconciliations | 查询任务列表 |
| GET | /api/v1/field-reconciliations/{id} | 获取状态和进度 |
| GET | /api/v1/field-reconciliations/{id}/mappings | 获取字段映射 |
| POST | /api/v1/field-reconciliations/{id}/mappings/confirm | 保存人工确认 |
| GET | /api/v1/field-reconciliations/{id}/issues | 分页查看问题 |
| GET | /api/v1/field-reconciliations/{id}/files/{kind} | 下载 Excel、报告、快照 |
| POST | /api/v1/field-reconciliations/{id}/retry | 重试任务 |

### 7.3 字段核对状态机

    stateDiagram-v2
    [*] --> queued
    queued --> loading_standard
    loading_standard --> reading_excel
    reading_excel --> comparing
    comparing --> completed: 无待确认问题
    comparing --> manual_review: 字段映射或结果需人工确认
    loading_standard --> failed
    reading_excel --> failed
    comparing --> failed
    queued --> cancelled
    loading_standard --> cancelled
    reading_excel --> cancelled
    comparing --> cancelled
    failed --> queued: retry
    manual_review --> queued: retry / 确认映射

实现位置：

- 核心服务：src/excel_auditor/field_reconciliation.py:144
- 平台契约模型：src/excel_auditor/field_reconciliation.py:61
- 路由：src/excel_auditor/field_reconciliation_routes.py:56
- 前端工作台：apps/web/src/reconciliation/ReconciliationWorkbench.vue

## 8. 商品标准化和商家商品合并扩展

商品标准化入口：

POST /api/v1/product-normalizations

它使用规则中的 product_workflow 配置，从平台目录适配器读取类目和商品字段，执行商品匹配、字段标准化、人工审核和修订。

商家商品合并入口：

POST /api/v1/merchant-product-reconciliations

处理步骤：

1. 识别多个商家工作表。
2. 提取商品名称、货号、品牌、价格、库存等字段。
3. 与平台商品源进行名称和标识匹配。
4. 生成 商品汇总、问题清单 和隐藏的 _来源追踪 工作表。
5. 保留来源工作表、来源行号和字段追踪信息。

主要代码位于 src/excel_auditor/product_workflow/。

## 9. 核心代码说明

### 9.1 FastAPI 应用装配

service = AuditService(
    DATA_ROOT,
    managed_http=managed_http,
    database=database,
    artifact_store=artifact_store,
)

app = FastAPI(title="Excel Standard Auditor", version="0.1.0")
app.include_router(build_workflow_router(DATA_ROOT, database, task_queue))
app.include_router(build_field_reconciliation_router(...))

位置：src/excel_auditor/api.py。

### 9.2 统一鉴权和租户隔离

API 中间件读取 Authorization: Bearer ...，将身份解析为：

request.state.tenant_id
request.state.user_id
request.state.trace_id

所有任务、规则、文件和报告查询都带 tenant_id 条件。生产环境必须设置：

EXCEL_AUDITOR_REQUIRE_AUTH=1
EXCEL_AUDITOR_API_TOKEN=...

多租户可以使用 EXCEL_AUDITOR_API_TOKENS_JSON 为不同令牌绑定不同租户和用户。

### 9.3 幂等任务创建

通用核验、SOP 和字段核对都支持 Idempotency-Key。服务会将租户、请求参数和文件内容生成指纹，避免同一请求重复创建任务。

核心实现：

- AuditService.create_or_get_job：src/excel_auditor/service.py
- WorkflowStore.claim：src/excel_auditor/workflow/repository.py
- RedisJobQueue：src/excel_auditor/queueing.py

### 9.4 规则和字段模型

核心模型在 src/excel_auditor/models.py：

- RuleSet：完整规则版本。
- SheetRule：工作表、表头、主键和动作配置。
- ColumnRule：字段类型、别名、校验、容差、格式和修复策略。
- Difference：单条差异和审计信息。
- AuditReport：任务级报告。

所有模型都使用严格字段策略，未知配置字段会直接拒绝。规则发布时会校验容差、日期格式、枚举别名、公式模板、工作表和字段冲突。

### 9.5 表头映射和主键比较

表头映射位于 src/excel_auditor/engine.py:74。匹配优先级：

1. 规范字段名。
2. 字段展示名。
3. 已确认别名。
4. RapidFuzz 模糊候选。
5. 无法唯一确认时进入人工审核。

主键匹配支持：

- 单字段主键。
- 联合主键。
- 行号主键。
- 空主键和重复主键隔离。
- 前导零编码保留。
- 日期和 Decimal 类型化比较。

### 9.6 受控 Excel 渲染

Python 比对引擎只生成修复计划，不直接承担复杂 OpenXML 写回。Manifest 经过严格校验后交给 .NET Renderer。

渲染器支持：

- 单元格和整行标色。
- 批注追加和审计说明。
- 类型化写值。
- 插列。
- 缺失记录追加。
- Table、筛选、数据验证和定义名称维护。
- 宏部件保留。
- 输出结构回读验证。

不支持或无法安全证明的结构必须返回结构化错误，例如 UNSUPPORTED_FEATURE，不能静默丢失数据。

## 10. 存储、任务和数据生命周期

### 10.1 本地开发

var/
└── jobs/{job_id}/
    ├── input.xlsx
    ├── standard-snapshot.json
    ├── report.json
    ├── differences.jsonl
    ├── report.html
    ├── render-manifest.json
    └── result.xlsx

SOP 工作流默认使用：

var/workflow/workflow.sqlite3

### 10.2 生产环境

- PostgreSQL 保存任务、规则、差异索引和审计。
- Redis 保存队列状态。
- MinIO/S3 保存标准快照和结果文件。
- 本地目录只作为临时工作区。
- 任务完成后按保留策略清理。

相关代码：

- 数据库：src/excel_auditor/persistence.py
- 文件存储：src/excel_auditor/storage.py
- 保留清理：src/excel_auditor/retention.py
- Worker：src/excel_auditor/worker.py

## 11. 安全和可靠性要求

### 文件安全

- 限制上传大小、行数、列数和单元格总数。
- 检查 ZIP 条目、路径穿越、重复条目和加密文件。
- 检查 XML 外部实体和危险 OpenXML 结构。
- 默认不执行用户公式、宏和外部连接。

### 数据安全

- Bearer 鉴权。
- 租户级对象过滤。
- 敏感字段脱敏到报告、日志和审计。
- 标准数据 SHA-256 快照。
- 上传和结果文件独立保存。

### 网络安全

- 受管 HTTP 连接使用路径白名单。
- 禁止未经授权的内网访问和 SSRF。
- 生产环境要求 HTTPS。
- 连接文件只保存 auth_secret_ref，密钥从受保护目录读取。

### 并发和一致性

- Idempotency-Key 避免重复任务。
- revision 乐观锁避免旧页面覆盖新数据。
- Redis/RQ 把长任务移出 API 请求。
- Worker 和 API 共享同一数据根目录或对象存储。

## 12. 本地开发和验证

### 安装依赖

py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install uv==0.12.5
.\.venv\Scripts\uv sync --frozen --extra test --extra security

### 构建 .NET Renderer

dotnet restore services\excel_renderer\ExcelRenderer.csproj --locked-mode
dotnet publish services\excel_renderer\ExcelRenderer.csproj -c Release -r win-x64 --self-contained true --no-restore -o .renderer
$env:EXCEL_RENDERER_COMMAND = (Resolve-Path .renderer\ExcelRenderer.exe)

### 启动后端

$env:PYTHONPATH = "src"
.\.venv\Scripts\python -m uvicorn excel_auditor.api:app --host 127.0.0.1 --port 8000

### 启动前端

pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web dev --host 127.0.0.1

### 验证

.\.venv\Scripts\python -m pytest -q
pnpm --dir apps/web build

重点测试：

- tests/test_api.py
- tests/test_end_to_end.py
- tests/test_workbook_safety.py
- tests/test_standardization_workflow.py
- tests/test_field_reconciliation.py
- tests/test_excel_acceptance.py
- tests/golden_files/

浏览器验收脚本位于 tools/verify_*.cjs，字段核对演示位于 tools/demo_field_platform.py。

## 13. 生产部署流程

生产部署文件：

- deploy/docker-compose.prod.yaml
- deploy/Dockerfile.api
- deploy/Dockerfile.web
- docs/deployment.md

标准发布流程：

1. CI 执行测试、依赖许可证检查、漏洞扫描和 SBOM 生成。
2. 按 Git SHA 构建 API/Worker 和 Web 镜像。
3. 读取镜像 registry digest。
4. 生成并校验 release-manifest.json。
5. 生产服务器只拉取固定 digest 镜像。
6. 执行数据库迁移。
7. 启动 API、Worker 和 Web。
8. 验证 /health/live、/health/ready、Worker 心跳和小型 smoke comparison。
9. 保留上一版 digest 和 previous-release.env 以便回滚。

生产环境不得在服务器执行 docker build、源码构建、latest 替换或未经确认的清理操作。

## 14. 后续开发建议

### 优先级高

- 与真实平台联调字段标准接口和鉴权。
- 完成真实商家文件验收，而不只依赖合成 Golden 文件。
- 建立生产 PostgreSQL、Redis、S3 的集成环境。
- 完成真实模型服务的 HTTPS、超时、限额和质量验收。

### 优先级中

- 增加任务监控和失败原因可视化。
- 为大文件增加更完整的磁盘溢写和性能基线。
- 增加字段配置导入导出和审计查询界面。
- 补充跨工作表引用和复杂 Excel 结构的明确支持边界。

### 开发注意事项

- 不要绕过 RuleSet 和严格模型直接读取用户配置。
- 不要让标准源请求发生在 Excel 安全预检之前。
- 不要使用最新发布的字段配置重建历史任务。
- 不要把人工审核问题静默降级为自动通过。
- 修改 Renderer 前必须补充 Golden/合约测试。
- 修改任务状态时同步更新 API、前端状态标签和测试。

## 15. 当前工作区交接说明

当前工作区在整理本文档前已经存在未提交修改，涉及 README、前端工作台、SOP 后端、队列、平台字段核对和相关测试。接手开发时先执行：

git status --short --branch
git diff --stat

确认这些修改属于当前迭代，再决定继续开发、提交或拆分分支。本文档记录的是当前工作区状态，认证功能新增和修改见第 16 节及对应源文件。

## 16. 单工作空间与运营账号

网页端在单一 `local` 工作空间内使用账号和密码登录。管理员账号唯一，运营人员由管理员创建并拥有独立角色、配额和审计身份：

    http://127.0.0.1:5173/login

登录后由服务端签发 HttpOnly 会话 Cookie。浏览器不会把密码、会话令牌或 CSRF Token 写入 localStorage/sessionStorage；写请求由前端自动获取会话绑定的 CSRF Token。登录失败返回统一的认证失败消息，并按账号和来源地址执行失败锁定。

认证 API：

- POST /api/v1/auth/login
- GET /api/v1/auth/session
- GET /api/v1/auth/csrf
- POST /api/v1/auth/logout
- GET /api/v1/users
- POST /api/v1/users
- PATCH /api/v1/users/{id}
- POST /api/v1/users/{id}/reset-password
- POST /api/v1/users/{id}/disable
- POST /api/v1/users/{id}/enable
- GET /api/v1/operators
- POST /api/v1/operators
- PATCH /api/v1/operators/{id}
- POST /api/v1/operators/{id}/reset-password
- POST /api/v1/operators/{id}/disable
- POST /api/v1/operators/{id}/enable

运营人员只能查看和操作自己创建的任务，管理员可查看全局任务和审计；`viewer` 只读。任务进入 Redis 的 `normal`、`heavy`、`network` 或 `workflow` 队列，状态、配额计数、Worker 租约和审计事件保存在共享数据库。服务间 Bearer Token 保持兼容，但不会作为网页端账号密码使用。

首次创建管理员：

    excel-auditor-admin create --username admin

密码从 EXCEL_AUDITOR_BOOTSTRAP_PASSWORD 读取或交互式输入；也可以通过 EXCEL_AUDITOR_ADMIN_USERNAME 和 EXCEL_AUDITOR_ADMIN_PASSWORD 在 API 启动时创建管理员。生产环境必须先执行 Alembic 迁移，并通过 HTTPS 访问网页端。

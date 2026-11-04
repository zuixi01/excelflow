# 平台字段标准驱动的 Excel 核对

默认入口为统一工作台 `#workbench`，同页切换“SOP 整理”和“平台字段核对”。原 SOP 的任务、维护、导出及模板编辑完整保留，模板管理通过 SOP 模式内的“流程配置”进入。平台字段核对无需选择模板，使用 `#workbench?mode=reconciliation`，兼容旧链接 `#reconciliation`。已有数据保存在原 `EXCEL_AUDITOR_DATA` 目录，演示目录独立，不代表原数据已删除。

## 接口契约

平台返回 JSON 对象，支持直接返回以下对象，也支持放在 `data` 中：

```json
{
  "schema_version": "2026.10",
  "fields": [
    {"id": "sku", "title": "商品编码", "aliases": ["货号"], "type": "string", "required": true},
    {"id": "material", "title": "材质", "description": "商品主要材料", "compare_mode": "semantic"},
    {"id": "price", "title": "销售价", "type": "decimal", "required": true}
  ]
}
```

- `fields` 为实际需要核对的动态字段，不使用硬编码商品字段。
- 仅提供字段时，系统执行字段对应、必填、类型和枚举检查，不报告“商品值已与平台一致”。
- 如需逐行标准值比对，额外提供 `primary_key: ["sku"]` 和 `records: [{"sku":"001","material":"纯棉","price":"100.00"}]`。省略 `records` 与 `records: []` 含义不同：前者只做字段校验，后者表示标准记录确实为空。
- 字段类型：`text/string/integer/decimal/date/datetime/boolean/enum/id_code`；枚举必须提供 `enum_values`。
- 比较方式：`exact/ignore_case/numeric/date/semantic`。数值、日期、布尔和枚举使用类型化确定性比较；`semantic` 仅允许文本字段。
- `sensitive: true` 的值不发送给模型。字段名、字段 ID、类型和描述供模型识别字段含义。
- 标准记录必须有完整且唯一的业务主键。业务主键保留大小写、标点和前导零，不进行名称模糊关联。
- 首版字段契约和标准记录需在一次响应中完整返回，不支持截断或分页 envelope。默认响应上限 16 MiB，记录上限另受连接配置约束。较大平台数据需由平台接口按商户或类目分段返回。

## 服务端连接

参考 `configs/connections.example.json` 中的 `merchant-field-standard`。使用 `EXCEL_AUDITOR_CONNECTIONS` 指向连接文件，密钥继续由 `auth_secret_ref` 与受保护的密钥目录或环境变量管理。

`field_reconciliation` 配置 `title`、`path`、`method`、`tenant_ids`、`parameter_mapping` 和可选 `static_parameters`。只有明确列出的租户能看到和使用该来源，接口 URL、路径和密钥不返回浏览器。`parameter_mapping` 指定运营需要填写的业务范围；固定商户或固定类目可通过 `static_parameters` 设置，此时日常操作只需上传。

生产连接应使用 HTTPS 和白名单路径。本地模拟配置 `configs/connections.field-demo.json` 允许 loopback HTTP，仅供开发演示。

## 模型与确认

在系统设置保存模型地址、密钥与模型名，兼容 JSON chat completions。名称和平台别名精确匹配可自动应用，其他字段由模型提供语义候选；首版模型自报高置信度仍需运营确认。附加列可选择“保留为附加列”，重复目标字段必须解决。

模型接口默认仅支持 HTTPS。仅在非生产环境中，可由管理员设置 `EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP=1` 后重启 API 和 worker，显式允许 HTTP；系统设置会显示明文传输警告。`EXCEL_AUDITOR_ENV=production/prod` 时始终禁止 HTTP，即使打开该开关也无效。关闭兼容后，已有 HTTP 配置也不能继续发送请求。生产环境应通过 HTTPS 网关接入，不要将开发兼容作为正式部署方案。

文本标准值比对需要开启“授权发送非敏感字段值进行语义比对”。模型返回 `equivalent/different/unknown`，结论与依据写入报告。服务不可用、响应截断、重复结论、超出单次容量或没有发送授权时列为待核实，不判通过。每批最多 25 个文本值，一次任务最多处理 1000 对，其余留待核实。未配置模型时仍可完成确定性核对及人工字段确认。

字段确认使用修订号防止旧页面覆盖新操作。确认与重试读取同一份字段快照；快照有内容哈希校验。旧修订的报告保留在任务目录，原始 Excel 不被改写。完整报告保留每行所有来源列的原值，结果 Excel 的“供应商原始数据”包含未匹配及附加列，可结合“字段映射”追溯各表列名。敏感原值仍存在结果文件中，下载需按租户控制权限。

## 任务 API

| 方法与路径（前缀 `/api/v1/field-reconciliations`） | 用途 |
| --- | --- |
| `GET /connections` | 返回当前租户可用的来源及所需业务范围 |
| `POST /` | multipart：`excel_file`、`connection_id`、`scope_json`，支持 `Idempotency-Key` |
| `GET /` | 当前租户任务列表，`page/page_size` 分页 |
| `GET /{id}` | 状态、进度与统计 |
| `GET /{id}/mappings` | 映射、语义依据及可用平台字段 |
| `POST /{id}/mappings/confirm` | `base_revision` 与 `decisions: [{sheet,column,field_id}]`；`field_id: null` 表示保留附加列 |
| `GET /{id}/issues` | 分页问题，支持 `severity` 筛选 |
| `GET /{id}/files/{excel,report,snapshot}` | 下载 Excel、完整报告或接口快照 |
| `POST /{id}/retry` | 失败或待确认任务重新处理，复用已有快照 |

后台处理复用 Redis/RQ；未配置队列时使用 FastAPI 开发后台任务。容量为 50 MiB、总计 20,000 条记录、每表 200 列、200 万单元格和 50 个工作表。首版要求普通列表数据区域；支持可见多工作表及常见合并表头，复杂跨表引用和公式值需先转换为值。生成独立结果文件，宏、图像及其他原文件结构不会复制到结果，但上传文件完整保留。

## 本地演示

```powershell
.\.venv\Scripts\python tools\demo_field_platform.py --generate
.\.venv\Scripts\python tools\demo_field_platform.py
```

另一个终端启动 API：

```powershell
$env:PYTHONPATH = "src"
$env:EXCEL_AUDITOR_CONNECTIONS = "configs/connections.field-demo.json"
$env:EXCEL_AUDITOR_DATA = "var/field-demo/runtime"
.\.venv\Scripts\python -m uvicorn excel_auditor.api:app --host 127.0.0.1 --port 8000
```

使用 `pnpm --dir apps/web dev --host 127.0.0.1` 启动前端，上传 `var/field-demo/supplier-demo.xlsx`。演示包含一列待确认字段和一项数值格式问题，平台响应是明确的模拟数据。真实平台地址、鉴权和字段响应尚需与对方联调；本地演示不验证真实模型质量。

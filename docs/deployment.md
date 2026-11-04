# 构建与部署

2026-11-03：50 人 / 8 核 16 GB 的当前部署参数、生产密钥、容量限制、后台接口与备份操作，以 [生产部署手册](production-8c16g.md) 为准。以下容量实验数据保留为历史证据，不代表新版本已通过生产压测。

## 不可变构建

所有测试和镜像构建在本地或 CI 完成。发布工作流使用 Git SHA 生成：

- `excel-auditor-api:<git-sha>`（包含已编译的 .NET 自包含渲染器）；
- `excel-auditor-web:<git-sha>`。

生产配置 `deploy/docker-compose.prod.yaml` 强制要求独立的完整 `API_IMAGE` 和 `WEB_IMAGE` 引用，不接受隐式默认值。发布工作流在镜像推送后读取两个 registry digest，生成经过 `excel_auditor.release_manifest` 严格校验的 `release-manifest.json` 与不含秘密的 `release.env`；两个引用均采用 `<git-sha>@sha256:<digest>`，因此 API/Worker 与 Web 可以分别精确锁定和回滚。
发布工作流先复用完整 CI，再本地构建、扫描、生成 SBOM 和执行容器冒烟；所有门禁通过后才登录仓库并推送镜像。

正式发布仅接受已有的 `v<major>.<minor>.<patch>`（可带预发布后缀）标签。手动触发也必须填写一个已存在且精确指向所选 Git SHA 的标签；不能从任意未标记分支绕过发布身份检查。

生产环境启动时执行 fail-fast 安全校验。至少要求：PostgreSQL URL 使用 `sslmode=require`、`verify-ca` 或 `verify-full`；Redis 使用 `rediss://`；对象存储启用 `AES256` 或 `aws:kms` 服务端加密，使用自定义 S3 endpoint 时必须为 HTTPS；鉴权必须开启且令牌长度至少 32 字符。服务间 API 可用 `EXCEL_AUDITOR_API_TOKENS_JSON` 配置长随机令牌；浏览器使用各自的管理员或运营账号登录。秘密只通过部署平台注入，不写入仓库、Compose 文件、日志或报告。

网页使用单个工作空间内的服务端会话。首次部署后运行 `excel-auditor-admin create --username <admin>` 初始化唯一管理员，再由管理员在“系统设置”创建运营账号；运营账号只有 `operator` 和 `viewer` 两种角色，任务容量与管理员共享工作空间级限制。运营账号仅能查看自己创建的任务，管理员可查看全部任务及创建账号；该隔离同时覆盖任务接口、下载和批量删除。历史任务缺少创建者时仅管理员可见。浏览器登录只需要账号和密码，浏览器 API 不接受租户选择。浏览器只接收 `HttpOnly`、`SameSite=Lax` 的会话 Cookie，生产环境由 `EXCEL_AUDITOR_ENV=production` 自动启用 `Secure`。Bearer Token 仍保留给 Worker、服务间调用和自动化脚本，不作为网页密码使用。

当前生产默认启动 workflow 导入与 workflow-export 导出/维护队列，各 1 个 Worker；normal/heavy/network 为按需开启的 legacy profile。主工作流共享执行上限 2、等待上限 50，每人执行上限 1、等待上限 3。旧比较模块的工作空间配额与主工作流配额是独立规则，不应混淆。

### 30 个以上同时提交

同一账号的多个浏览器会话共享账号容量，默认同时执行 1 个重任务，其余排队。不同账号共享全站执行名额。API 默认 2 个进程；Redis 与独立 Worker 用于生产。本地启动脚本使用独立的 SQLite 工作流 Worker 进程，API 不再执行主工作流重任务。

字段匹配先完成确定性的同名与别名匹配，再并行请求剩余字段。每进程的模型请求总并发由 `EXCEL_AUDITOR_MAPPING_CONCURRENCY` 控制，默认 4；每个任务最多提交 `EXCEL_AUDITOR_MAPPING_TASK_CONCURRENCY` 个尚未完成的请求，默认 2，避免大工作簿一次占满请求队列。多个 Worker 的总模型并发会累加，必须一起计入模型供应商的请求和 Token 额度。

主工作流的字段重新分析要求每次取得新建议，因此主动关闭跨任务缓存；重复上传相同表头仍会调用模型。底层映射运行时保留可选的短期缓存能力，但当前主工作流未启用，不能据此估算模型额度或处理时间。人工确认和版本冲突校验仍然执行。

以下为启动压测的配置示例，实际容量取决于表格大小、字段数及模型延迟：

| 资源参考 | API 进程 | 导入 Worker | 导出 Worker | 每任务模型并发 |
| --- | --- | --- | --- | --- |
| 8 核 / 32 GB | 2 | 4 | 2 | 2 |
| 16 核 / 64 GB | 2 | 8 | 4 | 2 |

其他队列的 Worker、数据库和操作系统也需要预留 CPU 与内存。提交数超过执行槽位时仍会短暂排队；减少等待应以任务完成速率、P95 等待时间、CPU/内存峰值及模型 429 频率为依据逐步增加 Worker。若每分钟到达任务数长期超过每分钟完成数，需要增加计算或模型容量，单纯增加排队上限不能缩短等待。

可以在管理机运行不消耗模型额度的调度基准：

```bash
PYTHONPATH=src python tools/benchmark_mapping_concurrency.py --tasks 30 --model-delay 0.05
```

该基准用固定模拟模型延迟比较原来的 2 个导入 Worker 逐批等待，与 4 个导入 Worker、并行请求和缓存的组合效果。它不测 Excel 解析、网络、真实模型速率限制或生产 Redis，因此不能把它的加速倍数当作真实业务承诺。发布后还需用实际大小的脱敏文件和实际模型服务做 30 个以上同时提交的完整压测。

2026-10-17 已在 Linux、真实 PostgreSQL/Redis/RQ 和 100ms 合成 HTTP 模型下完成完整流程补测，见 [容量收尾验收](superpowers/reports/2026-10-17-capacity-final.md)。同条件四账号、不同表头的 60 任务场景，导入 Worker 从 2 增到 4 后端到端 P95 从 86.3 秒降到 74.0 秒，但峰值进程 RSS 从约 2.0 GiB 增到 2.7 GiB；30 任务场景没有总耗时收益。继续将 4 导入 / 2 导出作为压测起点，不能据此承诺真实业务时延或直接推定 8 Worker 的收益。

上线新队列时须同时启动 `worker-workflow-export`，并让旧 `workflow` 队列中的导出任务自然完成。

## 任务恢复与本次升级

新增迁移为 `20261016_0009`（共享、可过期的登录失败计数）和 `20261016_0010`（任务执行记录、列表和父任务关联索引）。使用 `excel-auditor-server` 启动 API，先执行 Alembic 再启动多个 API 进程。单独运行 `uvicorn` 或旧 `excel-auditor-api` 入口前须手动执行 `alembic upgrade head`。本地 `workflow.sqlite3` 自动执行兼容旧文件的增量结构升级；正式环境仍使用 Alembic。

提交业务记录时同时写入待执行记录。Redis 暂时不可用时保留任务，独立恢复服务会补发，避免要求用户重复提交。API 不启动工作流扫描线程，也不执行主工作流重任务；生产部署单独的 `workflow-recovery` 与 RQ Worker，本地启动脚本单独启动 SQLite 工作流 Worker。每次执行最多尝试 3 次，默认租约 120 秒，每 30 秒执行心跳；恢复扫描间隔由 `EXCEL_AUDITOR_RECOVERY_INTERVAL` 设置。健康排队任务不会因等待超过租约而重新执行。无效文件直接失败；网络错误、服务临时不可用和 Worker 失联允许重试。故障发现时间取决于扫描间隔与租约，重跑开始时间取决于可用 Worker。

升级不会自动重跑没有执行记录的历史“处理中”任务。发布前排空旧队列并核对这类任务；确认旧 Worker 已停止后，逐项取消、重试。新的来源 JSON、样例、导出快照及输出目录通过数据库中的文件引用读取，旧格式仍可读。应同时备份业务数据库、认证数据库及 `/data` 文件，数据库备份与文件备份须来自同一暂停写入窗口。

管理员初始化只在不存在时创建账号，已有密码、角色和禁用状态保持不变。需要重置时，在受保护终端设置 `EXCEL_AUDITOR_BOOTSTRAP_PASSWORD`，显式执行 `excel-auditor-admin reset-password --username admin`；该操作撤销已有登录会话。旧会话首次获取 CSRF 时会转换为稳定令牌，升级时已打开页面应刷新一次。

反向代理部署须将 `EXCEL_AUDITOR_TRUSTED_PROXY_IPS` 配置为实际 Nginx 来源 IP 或专用代理网段。保持 API 不直接暴露公网；Nginx 覆写来源头，登录限流分别统计账号和来源 IP。共享出口默认允许每 5 分钟 25 次来源失败，账号默认连续窗口内 5 次失败后锁定 5 分钟。不要把未经可信代理处理的外部来源头直接用于限流。

回滚时先暂停提交、停止恢复扫描并排空 Worker。优先回滚到支持这些文件引用的兼容版本；回滚到旧版本必须一并恢复升级前的数据库和任务文件，不能仅回滚镜像，也不能在仍有新执行记录时直接降级结构。新增迁移的升降级仅在独立测试库演练，生产降级前须确认备份可恢复。

真实队列压测工具为 `tools/benchmark_workflow_capacity.py`。在独立 PostgreSQL 数据库、Redis 实例和本地空闲端口运行，设置 `BENCHMARK_DATABASE_URL`、`BENCHMARK_REDIS_URL`，执行 `PYTHONPATH=src python tools/benchmark_workflow_capacity.py --tasks 30 60 --large --index --output .codex-artifacts/capacity`。该工具启动真实 HTTP API、RQ Worker 和模拟模型，记录延迟、吞吐、进程资源及 PostgreSQL 锁等待采样，校验每个任务下载结果与账号权限。它创建合成测试账号和数据，不能指向业务数据库。真实模型额度、实际脱敏文件和正式服务器硬件的验收仍须单独执行。

仅补跑大文件与分页时可增加 `--skip-matrix --large --index`；`--large-shape 20000 50` 可单独指定样本，并可重复传入多个形状。每个已完成样本或分页检查会保存检查点；后续失败不会抹掉已完成指标，整体失败状态也不会被改成成功。大文件下载后的校验按配置的数据起始行逐单元格比对，不假定只有一行表头。

受管 HTTP 标准源使用 `STANDARD_CONNECTIONS_FILE` 挂载只读连接注册表；注册表只保存 `auth_secret_ref`，不保存密钥。`STANDARD_SECRETS_DIR` 指向受保护的宿主机目录，每个引用对应一个同名、只读的小文件。上传型标准源可继续使用仓库提供的空注册表和空秘密目录。不得把实际连接密钥写入 `env.example`、连接 JSON 或镜像。

## 服务器发布前只读检查

执行任何拉取或启动前，必须检查并记录：

```bash
uptime
free -h
swapon --show
df -h
df -ih
docker system df
docker compose --env-file /secure/production.env --env-file ./release.env -f deploy/docker-compose.prod.yaml ps
```

同时检查最近系统日志是否有 OOM、磁盘 I/O、文件系统或内核错误。资源达到项目 `AGENTS.md` 阈值时停止发布，不自动清理、重启或远程构建。

## 发布与验证

从对应 GitHub Actions 运行下载 `release-manifest-<git-sha>` 制品，在受信任管理机执行以下离线校验：

```bash
PYTHONPATH=src python -m excel_auditor.release_manifest release-manifest.json --env-output release.env
```

把当前已验证版本的 `release.env` 保留为精确回滚目标。服务器上的 `/secure/production.env` 只保存数据库、Redis、S3 和鉴权等秘密，不定义 `API_IMAGE` 或 `WEB_IMAGE`；命令中把已验证的 `release.env` 放在最后，使发布制品的镜像引用具有最终优先级。拉取前先核对 Compose 解析结果与 manifest 完全一致：

```bash
docker compose --env-file /secure/production.env --env-file ./release.env -f deploy/docker-compose.prod.yaml config --images
```

确认输出恰好包含 manifest 中的 API digest（API 与 Worker 两次）及 Web digest 后，仅在服务器执行：

```bash
docker compose --env-file /secure/production.env --env-file ./release.env -f deploy/docker-compose.prod.yaml pull
docker compose --env-file /secure/production.env --env-file ./release.env -f deploy/docker-compose.prod.yaml up -d --remove-orphans
docker compose --env-file /secure/production.env --env-file ./release.env -f deploy/docker-compose.prod.yaml ps
```

随后验证 `/health/live`、`/health/ready`、Worker 心跳和一份无敏感数据的小型 smoke comparison。日志只读取有限行数，禁止输出环境变量和秘密信息。

发布记录必须包含 Git SHA、API/Web 镜像完整 digest、SBOM/漏洞扫描制品、部署前旧 digest、健康检查结果和操作者/时间。没有这些记录时不得把部署标记为完成。

## 回滚

恢复发布前保留并已验证的 `previous-release.env`。先用 `config --images` 确认其中两个完整 digest 与发布前记录一致，再执行：

```bash
docker compose --env-file /secure/production.env --env-file ./previous-release.env -f deploy/docker-compose.prod.yaml config --images
docker compose --env-file /secure/production.env --env-file ./previous-release.env -f deploy/docker-compose.prod.yaml pull
docker compose --env-file /secure/production.env --env-file ./previous-release.env -f deploy/docker-compose.prod.yaml up -d --remove-orphans
```

不得在生产服务器运行 `docker build`、语言构建命令或未经确认的 prune/重启/清理操作。

若健康检查失败，停止继续发布并使用记录的旧 digest 回滚；不得临时改用 `latest`、现场修改源码或在服务器重建镜像。

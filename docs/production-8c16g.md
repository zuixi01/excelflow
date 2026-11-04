# ExcelFlow 内部生产部署：50 人 / 8 核 16 GB

本文件是部署与验收操作说明，不是容量保证。保持现有 Excel 输出逻辑。默认最多 2 个重任务同时执行、每人 1 个；最多 50 个等待任务、每人 3 个。上传最多 16 个并行接收，超出时工作台短暂等待并自动重试；模型调用跨进程最多 4 个。上传写入临时磁盘后再进入分析队列，较大的 Excel 文件复制不再占用数据库写事务。默认资源限制是验证起点，500 MiB 是上传上限，不是对任意内容的内存或处理时间承诺。

## 新服务器最短部署路径

以 Ubuntu 24.04 LTS x86-64、8 核 16 GiB 为目标。服务器先安装 Docker Engine、Compose 插件、Python 3、OpenSSL、curl，并可访问 GHCR。还须准备带 TLS 的 PostgreSQL、Redis、S3 兼容对象存储、有效的公司域名与证书，以及可用的 HTTPS 模型服务。可以部署在公司现有基础设施上；单独申请一台空服务器并不会自动提供这些服务。

发布先在 GitHub 的 `main` 分支运行一次成功的 `performance.yml` 基线，然后为通过 CI 的提交创建 `vX.Y.Z` 标签，等待 `release.yml` 完成测试、镜像扫描与发布。服务器拉取**该标签**，不要部署有未提交改动的工作目录：

```sh
git clone --branch vX.Y.Z --depth 1 https://github.com/zuixi01/excelflow.git
cd excelflow
python3 tools/production_init.py --directory "$HOME/.config/excelflow"
```

初始化只生成 API 令牌、管理员初始密码、平台字段加密密钥和私有配置文件，不会覆盖已有文件。编辑 `$HOME/.config/excelflow/production.env`，填写 PostgreSQL/Redis/S3 地址及凭据、实际域名；将证书及私钥放在该目录对应路径，把**真实模型密钥**写入 `model-key`。配置文件和 TLS 私钥设为 `600`；模型密钥与平台加密密钥须属于 `SECRET_READ_GID` 指定的主机组、权限为 `640`，这样非 root 的 API/Worker 容器才能只读挂载。初始化默认写入执行用户的组 ID。`SERVICE_CA_FILE` 指向供容器验证 PostgreSQL、Redis、S3 和模型 HTTPS 证书的 CA 包；公司内部 CA 应加入该包，Redis URL 如需显式指定可附加 `ssl_ca_certs=/run/secrets/service-ca.pem`。若公司 CA 尚未在服务器信任列表中，还应填写供部署脚本验证网页 HTTPS 的 `TLS_CA_FILE`。镜像仓库为私有时，先执行 `docker login ghcr.io`。证书须覆盖 `PUBLIC_HOSTNAME`，有效期至少剩余 30 天。不要将配置和密钥提交到 Git。

```sh
bash deploy/production.sh check "$HOME/.config/excelflow/production.env"
bash deploy/production.sh up "$HOME/.config/excelflow/production.env"
bash deploy/production.sh status "$HOME/.config/excelflow/production.env"
```

`up` 根据当前 Git 提交的 SHA 拉取发布流水线产出的 API/Web 镜像，运行容器烟测、迁移和就绪探针，并通过附加的 TLS Nginx 容器开放 443。内部网页 8080 仅绑定服务器回环地址，API/数据库/Redis 不映射主机端口。预检失败时不会开始部署。正式开放用户前，仍须完成本文件末尾的容量、备份恢复与回退验收。

首次登录后，管理员应在系统设置中保存 HTTPS 模型地址与模型名称；`model-key` 文件用于保存实际密钥，Windows 本地保存的 DPAPI 密钥不能直接迁到 Linux。随后在隔离任务中走完上传、字段确认、维护表回传和最终表下载，确认图片、表头和动态字段。真实平台字段接口还没给出时，本地模板字段照常使用，接入后再配置平台地址。

## 发布前

1. 为发布制作不可变镜像；依赖安全扫描、回归、前端构建及真实基础设施测试必须通过。
2. 准备 PostgreSQL（TLS）、Redis（TLS）、对象存储、HTTPS 入口及 HTTPS 模型。数据目录必须为持久卷。外部数据库、Redis 的内存也计入整机预算。
3. 复制 `deploy/env.example` 到受保护的配置文件，替换地址和凭据。生产必须开启登录，员工使用独立账号。不要将本地开发启动脚本用作生产启动入口。
4. 将模型密钥放在 `MODEL_KEY_FILE` 指定的文件中，仅允许部署管理员和容器运行账户读取。API 与 Worker 挂载同一个只读文件。应用配置只保存外部密钥标记，不再将 Linux 生产密钥写入普通 JSON。密钥备份通过公司的密钥管理系统完成。
5. 代理终止 HTTPS，配置可信代理 IP。不要暴露 API、PostgreSQL、Redis 端口。镜像内 Nginx 的 8080 是代理后的内部 HTTP，员工入口必须是 HTTPS。

## 服务

正常发布使用上面的 `deploy/production.sh`，它会合并生产 Compose 与 TLS 入口配置；不要只启动基础 Compose 而跳过 HTTPS 和证书预检。

默认启动 web、API、导入 Worker、导出/维护 Worker、恢复服务。旧 normal/heavy/network 服务属于 `legacy` profile，只有确实使用这些旧模块且预算允许时才显式启用。

API 为 2 个进程，上限 2 GiB；导入和导出 Worker 各 1 个、各上限 4 GiB。恢复服务 512 MiB，web 128 MiB。剩余内存给操作系统、数据库、Redis 和文件缓存。观察 OOM 及任务失败，不能仅靠提高 Worker 数量扩容。

API 启动会执行 Alembic，新增容量表迁移为 `20261103_0011`。Worker 应在 API 就绪后再启动。历史字段快照保持不变。

## 接口与客户端

生产重操作统一返回 HTTP 202，`Location` 指向任务内的 `/operations/{id}`；轮询到 `ready` 后读取 `/result`。失败返回明确错误，旧版本数据不覆盖新版本。前端已经处理这一协议。开发环境带 `Prefer: respond-async` 使用相同流程；旧开发客户端可继续同步调用。

包括维护表下载、回传解析、结构差异预览、名称确认、维护数据保存、字段确认及重新分析。最终生成继续使用原有持久执行队列。浏览器关闭不会取消后台任务，超过等待时间不要重复提交；在任务数据中检查结果。HTTP 429 为明确的容量拒绝，不应自动无休止重试。

## 备份与恢复

`tools/production_backup.py` 实现**停写备份**，不是在线一致性备份。它不会主动停止服务；`--services-stopped` 是运维确认，不能伪称它检测了所有写入者。备份前停止 API、全部 Worker 和恢复服务，确认没有其他写入程序。网页入口显示维护公告。工具需要与数据库主版本兼容的 `pg_dump`、`pg_restore` 和项目 Python 依赖。

```sh
# 在能访问实际持久数据目录的运维主机执行；DATABASE_URL 从受保护环境注入
python tools/production_backup.py backup --data-root /srv/excelflow/data --bundle /backup/excelflow/release-001 --services-stopped
python tools/production_backup.py verify --bundle /backup/excelflow/release-001
# 恢复到全新的空数据库（更改 DATABASE_URL）和空目录，不覆盖原环境
python tools/production_backup.py restore --data-root /srv/excelflow/recovery --bundle /backup/excelflow/release-001 --services-stopped
```

备份完成再恢复服务；备份失败不能标为成功。将备份复制到异机加密存储，保留校验清单。恢复后核对账号权限、任务/商品数量、图片和样例导出，再切流量。目标 RPO 1 小时 / RTO 4 小时尚需目标环境演练；该停写工具本身不证明能满足一小时 RPO。需要持续运行时，应另配置 PostgreSQL PITR 与文件版本/一致性快照，验证同一恢复点后再承诺指标。

Windows 开发数据迁移：先停止本地写入，复制认证 SQLite、业务 SQLite 和整个数据目录；数据库迁移应在空 PostgreSQL 中进行并核对逐表数量、任务归属和文件哈希。不可把本地 owner 身份直接作为员工权限。Windows DPAPI 密钥不能直接搬到 Linux，使用生产密钥文件重新保存模型配置。

## 监控与清理

- 管理员接口 `GET /api/v1/workflow-operations/health`：队列、过期执行租约、资源名额、磁盘余量。
- 已鉴权 `/metrics` 增加数据库共享的任务数量、最长等待秒数、磁盘余量与过期执行数；通过内部抓取配置访问 API，不暴露指标到公网。
- RQ Worker 健康检查针对当前容器自己的 Worker 心跳；恢复服务检查自己的扫描心跳。
- 主机/容器监控补充 CPU、内存、OOM、磁盘 IO 和 HTTP P95，模型 429/超时也应告警。这些需要监控平台接入，不能用应用健康接口替代。

```sh
python -m excel_auditor.workflow.maintenance_ops health
python -m excel_auditor.workflow.maintenance_ops cleanup --days 7
# 确认清理范围后由运维计划任务每天执行
python -m excel_auditor.workflow.maintenance_ops cleanup --days 7 --apply
```

清理仅涉及过期回传预览、名称确认预览、后台操作结果、残留上传目录，以及数据库中没有对应对象且已过保留期的失败提交目录，不清理正式任务及其原表、图片、快照或最终导出。存在待执行任务时暂缓清理。超过保留期的确认预览需重新上传。

## 容量验收与发布

在目标服务器上用 50 个独立账号验证混合查询/编辑与真实文件提交，逐级增加并发；触发限额时预期为 429。分别统计接受率、排队等待、执行时间、失败率和轻接口 P95。保留 20% 左右整机内存余量，不以持续 swap 通过验收。

使用 11 个实际文件的隔离副本完整验证，逐项核对表头、合并、图片、原始字段和最终动态字段。测试 Worker 死亡、Redis 断连、API 重启、模型超时，以及备份恢复。至少做 8 小时混合负载。先 5–10 人试运行 1–2 个工作日，再开放 50 人。

回退应同时考虑代码与数据库版本；新增容量表是增量迁移，旧代码与当前数据兼容性仍需演练。发生数据错误时停止新写入，不能只回退镜像后继续写库。未完成目标服务器压测和恢复演练前，不得标注“已满足生产容量”。

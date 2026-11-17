# GitHub Actions 发布及 SSH-JP 安全切换

`.github/workflows/production-docker.yml` 在 GitHub runner 完成验证、构建和 GHCR 发布；既有 `SSH-JP` 目标只拉取并运行已构建的镜像。不得在目标执行源码构建、依赖安装或完整应用测试。

`scripts/deploy_release.py` 接受 `DEPLOY_PATH`、`DEPLOY_COMPOSE_FILE`（默认 `docker-compose.yml`）、`DEPLOY_SERVICE`（默认 `codex2api`）、`DEPLOY_OPERATION`（`inspect` / `deploy`）、`RELEASE_SHA`（完整提交 SHA）、`RELEASE_IMAGE`（GHCR digest 引用）。私有镜像登录使用 `GHCR_USERNAME/TOKEN` 和临时 Docker 认证目录，不覆盖既有 Docker 登录配置。

先以 `inspect` 运行：只读检查既有容器、健康版本、数据库类型、队列、持久卷，以及命名空间防火墙权限；不会封锁请求、拉取镜像或重启。输出仅含脱敏审计摘要。管理密钥从既有容器 `ADMIN_SECRET` 获取；必要时由受保护环境提供 `CODEX_DEPLOY_ADMIN_SECRET`，禁止放在命令行。

部署需 Linux、Docker Compose、Python 3.9+、`nsenter`、`iptables`，启用 IPv6 时还需 `ip6tables`，执行用户需有相应权限。脚本只修改专属容器网络命名空间，拒绝 host/shared 网络；权限或状态无法核实即停止。不会尝试安装工具或自动提权。

切换顺序：

1. 限次拉取指定 digest，校验 Docker 的仓库 digest，保留旧镜像本地回滚标签。此目标沿用 dockerd 已有下载线路；脚本不以 CLI 的代理环境变量冒充 daemon 路线切换。3.5 pre 不属于该目标，其他服务器部署应另行比较其实际直连/10809线路。
2. 在旧容器命名空间短暂拒绝服务端口的新连接，保留已建立连接，等待 HTTP/WS 服务连接、账号在途请求、异步图片 queued/running 连续归零；默认最多等待 180 秒。超时解除封锁并保留原服务，绝不重启在途任务。
3. SQLite 使用 online backup API，包含已提交 WAL，并校验备份；同时备份已存在的 `response-context.db`。备份保存在部署目录 `.release-backups/`，权限受限。记录旧账号、API Key、请求历史、终态图片任务、资源和关键运营设置的稳定字段指纹；新版本启动后按原 ID 逐项核对，允许新记录和账号凭证/余额同步变化，摘要仅输出数量与核对结果。检测到 PostgreSQL 时先停止，需根据该目标既有 `pg_dump` 能力补充一致性备份程序，不能把 SQLite 备份当作 PostgreSQL 备份。
4. 保留原 compose、`.env`、全部持久卷及运行设置，仅使用独立 compose override 选择镜像。既有启动命令如与镜像默认值不同则拒绝切换，不能覆盖用户自定义启动参数。新容器先等待，安装其专属入口防护后才启动应用；健康接口必须返回准确提交 SHA，管理运行状态必须确认数据库健康及 `output_guard_ready=true`，才开放流量；不会提交付费生成请求。
5. 失败时回滚镜像并验证旧版本；**不自动恢复数据库**，避免覆盖迁移后或其他服务的新写入。若旧版本不能读取迁移后的数据库，会明确报告回滚失败；备份仅用于人工评估后的离线恢复。

所有入口防护均在 `finally` 清理，另有独立超时清理 watchdog，处理 SSH 会话异常断开。已有长时间保活或 WebSocket 连接也会阻止严格切换，需要它们自然结束；脚本不会强断这些连接。应用升级可能迁移数据，镜像回滚不能代替数据库兼容性核对。

防护使用持续持有的网络命名空间文件描述符，清理 watchdog 继承该描述符，避免原 PID 退出或复用后误操作其他命名空间。部署目录永久保留 `.codex2api-release.json` 发布回执，不修改应用 `.env`。之后手工操作应使用 `docker compose -f docker-compose.yml -f .codex2api-release.json up -d --no-build --no-deps codex2api`，实际文件名及服务名按原配置替换，确保继续使用已验证的 digest；单独使用原 compose 可能恢复其默认镜像标签。

旧版本 Grok 异步视频任务的账号绑定只保存在运行时缓存。若目标使用 memory 缓存且存在 Grok 账号，现有接口无法证明这些异步任务已结束，脚本会报告风险并拒绝自动切换；不能用 HTTP 在途为零代替这项核对。

脚本回归测试只使用合成 SQLite 和模拟命令，执行于 GitHub runner：`python -m unittest discover -s scripts -p 'test_deploy_release.py' -v`。

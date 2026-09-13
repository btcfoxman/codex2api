# 上游合并核对记录（2026-09-13）

## 来源与范围

- 本地仓库：`btcfoxman/codex2api`，分支 `main`。
- GitHub fork 的 parent/source 均为 `james-6-23/codex2api`，对应本地 remote `james`。
- 合并前提交：`e7ed93d743eab0a136e670f72510eee541419637`。
- 上次共同上游提交：`10d970cb3c3bf88ebb3fcbe966a6a2eed1eb282a`（v2.6.6 之后的 main）。
- 本次上游提交：`2386b6c39bb62c934bf0c31350d964fb569d7a41`（`v2.9.7`）。
- 本次纳入 811 个上游提交；上游相对于共同祖先修改 1,131 个文件。
- 合并前本地 `main` 与 `origin/main` 一致，子项目无未提交改动。
- 本地备份分支：`codex/backup-before-upstream-v2.9.7-20260913`。

这是 `codex2api` 独立 Git 仓库内的合并。父工作区中的其他子项目、已有未提交改动以及本地 `.env`、SQLite 数据库均不属于本次修改范围。

## 冲突处理与定制保留

| 文件或功能 | 处理结果 |
| --- | --- |
| `.github/workflows/production-docker.yml` | 完整保留现有 GHCR 构建、鉴权拉取和 `SSH-JP` 部署配置；与合并前内容一致。 |
| 上游 `docker-image.yml`、`pr-check.yml`、`release.yml` | 保持 fork 之前已删除这些工作流的约定；`render-deploy.yml` 同样保持删除状态。 |
| `.github/workflows/security-scan.yml` | 接收上游更新。 |
| `.gitignore` | 保留本地 SQLite 数据库及其辅助文件忽略规则，同时接收上游新增规则。 |
| `admin/handler.go` | 在新设置结构中保留 `image_s3_public_base_url` 的读取、更新及响应字段。 |
| `frontend/src/pages/Settings.tsx` | 接收上游设置页重构，将 CDN 公网地址输入项接回新的图片存储设置区域。 |
| `internal/imagestore/*`、`proxy/images.go` | 保留公网图片 URL、对象前缀去重及无公网地址时使用预签名 URL 的行为。 |
| `proxy/images_test.go` | 本地 URL 上传回归测试补齐上游新增的 `imageUpscalePlan` 参数，修复自动合并后才在编译阶段暴露的签名不兼容。 |
| `proxy/handler.go` | 将下载 407 后内联远程资源的重试合入新版 Responses、Compact、Chat 处理链，保留上游连续重试、Antigravity 401 刷新和 Grok 压缩状态判断。 |
| `auth/store.go` | 使用上游 `confirmResponsesAvailable(..., false)` 兼容入口，替代本地增加 1 纳秒的时钟精度补丁；带请求开始时间的入口继续保护新的限流证据。 |

新增 `admin/image_storage_public_url_test.go` 核对 CDN 配置的规范化、持久化、无关设置更新、重载、清空及实际 URL；新增 `proxy/remote_assets_handler_test.go` 核对三个接口下载 407 后的成功重试和重复失败时仅重试一次的行为。

## 验证中修复的上游问题

- `GetProxyRiskScoringProfile` 在 SQLite 下先执行 `$1` 查询，又覆盖为 `?` 查询，第一份未扫描的结果一直占用连接。改为先选择 SQL 再查询，避免连接泄漏和 Windows 下数据库文件无法清理；增加单连接池连续查询回归，覆盖其他平台上的连接耗尽风险。
- 连续重试的磁盘回放缓冲直接删除已打开文件，在 Windows 下失败。拆出平台文件创建函数：其他平台保留立即 unlink，Windows 使用 `FILE_FLAG_DELETE_ON_CLOSE`，在句柄关闭或进程退出时由系统删除；核对文件不能被普通方式重新打开、回放内容及关闭后的清理。使用现有版本的 `golang.org/x/sys`，将其标记为直接依赖。
- LRU 淘汰测试使用连续 `time.Now()` 采样，粗粒度时钟产生并列时间戳后会随机失败。测试显式设置访问时间，验证真实的淘汰顺序；调度器生产逻辑不变。

## 验证环境与范围

- Windows amd64；Go 自动选择项目要求的 `1.26.6`；Node.js `22.22.2`。
- 为控制本机内存占用，Go 检查使用 `GOMAXPROCS=2`、`GOMEMLIMIT=1GiB` 和 `-p 2`。
- 本次仅完成本地代码合并与验证，不触发远程部署。
- 本机未安装 Docker，未运行容器镜像构建和 PostgreSQL 容器兼容性测试。
- 前端 `audit:ci` 脚本在 Windows 直接启动 `npm` 时出现 `spawnSync npm ENOENT`；当前漏洞例外表为空，因此使用等价的 `npm audit --omit=dev --audit-level=high` 验证，结果为 0 漏洞。

## 最终验证结果

| 检查 | 结果 |
| --- | --- |
| `go test -p 2 -count=1 -timeout 8m ./...` | 16 个有测试的包全部通过，另 3 个包无测试文件；包含新增集成回归。 |
| 上述 SQLite、回放缓冲、LRU 问题定向测试 `-count=2` | 两轮全部通过。 |
| `go vet -p 1 ./...` | 通过。 |
| `go build -p 1 -o .git/codex2api-upstream-v2.9.7.exe .` | Windows amd64 后端构建通过。 |
| `go mod verify` | 所有模块完整性校验通过。 |
| `npm ci --no-audit --no-fund` | 锁定依赖安装通过。 |
| `npm test` | 308 项通过。 |
| `npm run typecheck` | 通过。 |
| `VITE_APP_VERSION=v2.9.7 npm run build` | 前端生产构建通过。 |
| `npm run test:svg` | 5 项通过；1 项需额外本地 HTML 样本的可选测试跳过。 |
| `npm audit --omit=dev --audit-level=high` | 0 漏洞。 |
| `git diff --check` / `git diff --cached --check` | 通过，无未解决冲突。 |

复核 `git ls-remote james refs/heads/main` 仍为本次合入的 `2386b6c39bb62c934bf0c31350d964fb569d7a41`。父工作区状态与操作前一致。未执行远程 push、生产部署、Docker 构建、PostgreSQL 外部数据库测试或 race 检查。

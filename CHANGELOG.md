# 更新日志 / Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.1] - 2026-09-23

安全与可靠性加固版本：视频号 MITM 最小权限、REST 认证边界、证书生命周期、
崩溃恢复、CI 与发布工程化。不新增平台与新功能。

### 安全（Security）

- **视频号 MITM 域名白名单（最小权限）**：新增 `channels/domains.py` 集中维护
  解密白名单（默认仅 `weixin.qq.com` 子域）。mitmproxy 侧通过 `allow_hosts`
  把非白名单连接作为 TCP 隧道转发——QQ 其它子域、CDN、无关网站不再被 HTTPS
  解密；响应级解析同步收敛到同一白名单。可用 `channels.intercept_domains`
  显式扩展，禁止无依据放宽。
- **REST API 认证边界**：新增 `server/auth.py`。默认 `127.0.0.1` 行为不变；
  非环回客户端必须携带 `X-Auth-Token`（或 `Authorization: Bearer`），token 来自
  `server.auth_token` 或环境变量 `DOWNLOADER_API_TOKEN`；未配置 token 时远程请求
  一律 403。`/api/*` 全部纳入（含 channels、config、download、任务控制），仅
  `/api/v1/health` 公开。`server.auth_token` 不可经 HTTP 写入、读取时脱敏。
- **视频号 feed 接口脱敏**：新增 `ChannelFeed.to_public_dict()`，`/api/v1/channels/feeds`
  不再返回直链、`decode_key`、BGM/图片直链、`source_api` 等敏感/内部字段；
  证书状态轮询不再返回本机路径（完整证书详情走专用端点）。
- **yt-dlp extra_options 白名单分级**：新增 `ytdlp/options_policy.py`。默认只放行
  无本地副作用的网络/性能参数；`outtmpl` / `exec_cmd` / `external_downloader` /
  `cookiefile` / `proxy` 等危险参数需显式 `ytdlp.unsafe_extra_options: true`；
  未知参数一律拒绝、全有或全无。Web API 的 `overrides.ytdlp` 不再允许携带
  `extra_options` / `unsafe_extra_options`，杜绝经任务接口构造任意命令执行。

### 新增（Added）

- **证书生命周期**：`CertificateManager.uninstall()`（按本机 CA SHA-1 指纹精确
  删除当前用户 Root 存储中的证书，绝不按名称模糊删除）、`certificate_info()`
  （生成/信任状态、SHA-256/SHA-1 指纹、subject、有效期）。CLI
  `--channels-uninstall-ca`、REST `POST /api/v1/channels/certificate/uninstall`、
  `GET /api/v1/channels/certificate` 详情，网页控制台配套按钮。
- **系统代理崩溃恢复**：新增 `channels/proxy_recovery.py`。开启代理前把原始
  设置持久化到应用数据目录；启动时自动检测上次异常退出（`taskkill /F`、崩溃、
  断电）残留并恢复；用户手动改过代理时不覆盖。CLI `--repair-network`、
  REST `POST /api/v1/channels/network/repair`、网页「恢复网络」按钮。
- **CI**：`.github/workflows/ci.yml`——核心矩阵（ubuntu/windows × Python
  3.9–3.12，`[dev]`）、channels/server 矩阵（3.10+，`[channels,server,dev]`）、
  `requirements.lock` 锁定复现作业（3.12）与 `python -m build` 构建门禁；
  附 `scripts/ci_smoke.py` 包导入冒烟。CI 不修改真实系统证书/代理（全部 mock）。
- **依赖一致性守护**：`tests/test_dependency_consistency.py` 校验 pyproject /
  requirements.txt / requirements.lock 三者不漂移；`requirements.lock` 重建为
  完整快照并注明版本策略（yt-dlp 保持无上界便于升级解析器，mitmproxy 增设
  `<13` 上界）。
- **发布工程化**：`SECURITY.md`（CA/系统代理知情、卸载与恢复方法、远程暴露
  警告、漏洞报告渠道）、`CONTRIBUTING.md`、本 CHANGELOG、tag 触发的 Release
  工作流（仅打 tag 才出 Release 草稿）。

### 变更（Changed）

- **FeedStore 严格有界**：`max_items` 成为真正上限——按 failed → skipped →
  done → pending 优先级淘汰最旧条目，`downloading` 尽量保留但绝不允许无限
  增长；淘汰时同步清理 `feed_id` / `object_id` 双索引，杜绝 stale index。
- **项目定位描述统一**：仓库已不止抖音下载器——pyproject 描述、FastAPI
  标题/版本、CLI 描述、README 定位统一为多平台表述；版本号统一升到 2.0.1。
- **README 如实表述视频号能力边界**：不依赖微信前端 DOM / JS bundle 注入、
  可显著降低前端改版失效概率；仍依赖视频号 API 数据结构（objectDesc /
  decodeKey / liveInfo 等）、ISAAC64 加密与 CDN 行为。

### 修复（Fixed）

- open-folder / 配置写入等 4 处本地写入路径增加防御性守卫（符号链接拒绝、
  路径规范化、项目目录边界校验）。

### 升级须知

- 从 2.0.0 升级：无需迁移。此前异常强杀遗留的系统代理会在首次启动时自动
  恢复；如需立即处理执行 `python run.py --repair-network`。
- 使用视频号嗅探后若不再需要，建议 `python run.py --channels-uninstall-ca`
  卸载根证书。

## [2.0.0]

初始公开发布：抖音批量下载（多模式）、B 站全链路、Web 控制台与 REST API、
yt-dlp 多平台引擎、微信视频号嗅探下载。

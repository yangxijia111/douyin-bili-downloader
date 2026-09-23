# 更新日志 / Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.3] - 2026-09-23

视频号「粘贴分享链接下载」。Windows 真机实测确认：微信内视频号页面数据经
**原生桥接**下发（不走 HTTP），被动嗅探与页面 fetch/XHR hook 均拿不到 feed；
而分享链接会在微信内置浏览器打开独立的**预览页**（finder-preview），该页
数据经页面自身 API 取回——把注入覆盖到预览页即可稳定捕获。

### 新增（Added）

- **分享链接下载**（`--channels-link <URL>` / 网页控制台「分享链接下载」输入框
  / `POST /api/v1/channels/link`）：粘贴微信「分享 → 复制链接」得到的链接
  （`https://weixin.qq.com/sph/<id>` 或全链），自动识别、启动嗅探会话（强制
  自动下载）、尝试唤起链接，并引导在微信中打开；预览页加载后自动捕获并下载
  目标视频，完成后自动结束会话。支持分享文案混排文本的链接抽取。
- **预览页注入**：注入白名单扩展到 `/finder-preview/pages/{sph,feed,live,home}`
  （分享链接在微信内置浏览器里的实际落点）；前端页面类型识别同步覆盖。
- **preview 页 sceneInfo 模式提取**（`channels/feed.py: extract_preview_feeds`）：
  预览页接口返回的对象特征是 `videoUrl` / `picInfo`（而非 `objectDesc`），
  新增专用提取器；`FeedCapturePipeline` 在 objectDesc 提不到时自动回退到该
  模式，被动嗅探的响应标记预检同步兼容 `videoUrl`。
- **运行时探针**（随前端心跳回传）：`hooked`（运行时 hook 安装情况）/
  `fetch_hooked` / `xhr_hooked` / `index_size` / window 上与 finder/feed 相关
  的属性名 / 当前 video 元素的 src 与 outerHTML——真机诊断「数据到底从哪来」
  的关键证据，只回传属性名与计数，不含用户正文。
- **诊断路径清单**：`candidate_paths` 记录最近命中的白名单域响应路径（去查询
  串，有界 32 条），`parsed_feeds` 长期为 0 时直接看出微信数据从哪些接口来。

### 修复（Fixed）

- **嵌入场景 mitmproxy ErrorCheck 崩溃（P1，真机实测发现）**：mitmproxy 的
  ErrorCheck 插件在生命周期内出现任何 ERROR 级日志时于关闭时 `sys.exit(1)`，
  与 FastAPI 共享事件循环时会炸掉整个服务进程（会话 stop 后服务端死亡、再次
  start 500）。嵌入启动时移除该插件（addons.get("errorcheck") + remove +
  finish），代理自身错误已由各 addon try/except 与诊断计数器覆盖。
- **bootstrap 模块基址**：`document.currentScript` 只在脚本执行期有效，异步
  加载页面模块时为 null 导致模块 URL 解析成页面相对路径（404、按钮不出现）；
  改为执行期捕获 + `/__cuin/assets/` 兜底。
- **Banner 版本号漂移**：CLI 横幅硬编码 v2.0.1，改为动态读取 `__init__`。

### 变更（Changed）

- `is_channels_url` 现在也识别分享链接（`weixin.qq.com/sph/<id>`）；
  `CHANNELS_URL_HINT` 增加 `--channels-link` 引导。
- 无 decodeKey 的预览页直链下载失败时给出可操作指引（提示改用微信内播放 +
  页面按钮/嗅探列表路径），而不是笼统的「解密校验失败」。
- 版本号 → 2.0.3。

### 已知边界（如实说明）

- 分享链接的数据捕获依赖「在微信内置浏览器里打开该链接」——工具会尝试自动
  唤起（`cmd /c start`），失败时按引导手动打开即可。
- 预览页不下发 `decodeKey`：若拿到的直链仍是加密流，会明确提示改用页面按钮
  模式（该路径可取得 decodeKey）。真机验证见
  `docs/testing/channels-windows-smoke.md` 第 13 项。

## [2.0.2] - 2026-09-23

视频号运行时捕获与页面内下载版本。修复 Windows 真机「嗅探会话正常但始终
捕获不到视频」的 P0 缺陷：页面注入成为主要捕获方案，微信页面内直接出现
可视化下载按钮；被动嗅探保留为兜底。新增链路诊断、四策略捕获流水线与
前端单测。**真机验收清单见 `docs/testing/channels-windows-smoke.md`——
本版本是否正式发布以 Windows 微信真机验收为准。**

### 修复（Fixed）

- **视频号无法捕获（P0）**：根因是 v2.0.1 唯一捕获路径（mitmproxy 被动
  响应 → body 含 `objectDesc` → `json.loads`）无法覆盖真实微信链路——
  页面数据可能只存在于前端运行时对象、或落在未解密域，且链路完全不可
  观测。v2.0.2 以页面注入为主策略：向 `channels.weixin.qq.com/web/pages/
  {home,feed,live,profile}` 注入 bootstrap，hook `fetch` / `XMLHttpRequest`
  与 finder 运行时函数，经同源虚拟接口 `/__cuin/feed` 回传 Python 侧。
- **诊断替代「暂无嗅探结果」**：新增 `channels/diagnostics.py`（11 个规范
  计数器 + 九级状态链 + A–F 分级诊断）。CLI 会话与 Web 控制台实时显示
  「代理连接 → 目标域命中 → HTML 拦截 → 脚本注入 → 前端心跳 → 页面按钮
  → Feed 获取 → 下载成功」，第一个 ✗ 即断点并附处置建议。

### 新增（Added）

- **微信页面内下载按钮**（`channels/inject/`，独立实现，不复制
  wx_channels_download 源码）：首页推荐流（对应当前播放视频，切换自动
  跟随）、详情页/作者主页（优先微信现有操作栏）、直播页（开始/停止录制）。
  选择器链 primary → fallback → 悬浮按钮，MutationObserver 处理 DOM 复用，
  `data-cuin-btn` 保证幂等。菜单按 feed 能力动态生成（下载 / 最高画质 /
  最低画质 / 封面）；未识别当前视频时给出引导提示，绝不无反应。
- **同源虚拟接口**（`channels/virtual_host.py`）：`/__cuin/assets/*` 虚拟
  静态资源、`/__cuin/feed` Feed 桥接、`/__cuin/heartbeat` 前端心跳、
  `/__cuin/task` 下载/录制任务与状态轮询。全部由 mitmproxy 本地响应、
  **永不转发腾讯服务器**；严格校验 body 大小 / Content-Type / 同源
  Origin/Referer / schema；非 `/__cuin/*` 请求零影响。
- **四策略捕获流水线**（`channels/pipeline.py`）：A 被动响应 / B 页面
  网络 hook / C 页面运行时 hook / D 兼容补丁，统一进 `FeedStore`，按策略
  统计供数占比。
- **Strategy D 补丁框架**（`channels/patches.py`）：特征检测命中才应用、
  失败自动 passthrough、命中/失败记诊断。**默认不登记任何补丁**——仅当
  真机证明 B/C 不稳定时按微信版本登记（附 regression fixture）。
- **页面按钮任务中心**（`channels/task_hub.py`）：复用 ChannelsDownloader /
  FeedStore / DownloadResult，任务状态轮询、直播录制取消（ffmpeg 优雅
  终止并保留部分文件）。
- **HTML 注入正确性**（`channels/injector.py`）：gzip/br 透明（解码 →
  改写 → 按原编码重编码并修正 Content-Length）、CSP 头与 meta 双形式
  放宽（补 `'self'`、复制 nonce，不引入 `'unsafe-inline'`）、幂等标记、
  任何失败原样放行。
- **前端单测**（`tests/frontend/`，`node --test`）：hook 语义保持
  （fetch 返回值完全不变 / XHR 行为不变 / hook 异常不影响微信）、
  objectDesc 收集、当前视频匹配、按钮模型。CI channels-server 矩阵新增
  该步骤。

### 变更（Changed）

- **`channels.auto_download` 默认改为 `false`**：页面按钮模式下「刷视频
  只捕获、点按钮才下载」才是合理语义，避免刷十几个视频全部自动保存；
  用户可在配置或网页控制台手动开启。
- 新增配置 `channels.inject_ui`（默认 true，false 退化为纯被动嗅探）与
  `channels.patch_js_bundles`（默认 false，Strategy D 开关）。
- Web 控制台「视频号」页新增「嗅探链路诊断」卡片（状态链 + 处置建议 +
  策略分布），捕获列表空态改为链路诊断提示。
- `/api/v1/channels/status` 增加 `diagnostics` 负载，`/feeds` 增加
  `strategy_stats`。

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

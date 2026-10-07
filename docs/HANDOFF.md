# 开发交接文档（HANDOFF）

> 生成日期：2026-10-04。撰写方式：基于当日对代码库、Git 状态与测试套件的实地核查（非聊天记录转述）。
> 目标读者：下一位接手的 AI Agent 或开发者。读完本文档 + 代码库即可继续开发，无需任何历史会话。
>
> 另有两份补充阅读：`AGENTS.md`（项目结构与 AI Agent 规则，内容详细）与 `CHANGELOG.md`（版本演进）。
> 各 Agent 留下的专题排查记录在 `docs/testing/`（5 份）。

---

## 1. 项目目标与现状

### 1.1 项目是什么

**多平台视频批量下载器**，Web 控制台 + CLI + REST API 三种入口。支持四个平台引擎：

| 引擎 | 目录 | 覆盖平台 |
|------|------|----------|
| Douyin（自研） | `core/` | 抖音：单视频、图文、作者主页（发布/点赞/合集/音乐）、收藏夹、直播 |
| Bilibili（自研） | `bilibili/` | B 站：单视频（含分 P）、UP 主投稿、合集/系列、收藏夹、`b23.tv` 短链 |
| yt-dlp（委托） | `ytdlp/` | 爱奇艺、腾讯视频、优酷、芒果 TV、快手、西瓜、头条、微博、小红书等 |
| Channels 视频号（自研嗅探） | `channels/` | 微信视频号：被动嗅探 + 页面注入 + MSE 分片捕获 + 分享链接 |

衍生自上游 `jiji262/douyin-downloader`（已保留 upstream remote），公开仓库为
`github.com/yangxijia111/douyin-bili-downloader`（origin）。

### 1.2 版本与完成度（2026-10-04 实测）

| 版本 | 状态 | 内容 |
|------|------|------|
| v2.0.1（commit `d95c6d7`） | **已发布**（CI 绿、Release 已发） | 安全加固：REST token 认证、配置写入白名单、凭据脱敏、MITM 最小权限 |
| v2.0.2（`f988997`） | 已推送 | 视频号页面注入捕获（hook fetch/XHR + 页面下载按钮 + `/__cuin/*` 虚拟接口 + 九级诊断链） |
| v2.0.3（`9d79c11`，**当前 HEAD**） | 已推送、未 tag | 视频号分享链接下载（preview 页捕获） |
| v2.0.4（**未提交，全部在工作区**） | 代码完成、测试全绿、**真机部分验收** | 见下 |

**工作区未提交的 v2.0.4 内容**（58 个修改文件 +5357/-754，24 个新增文件，`git status` 实测）：

1. **MSE 分片旁路捕获（策略 E）**：微信 4.x 把视频号页面数据改为原生桥接下发、播放改为 MSE
   （`video.src` 是 blob: URL），被动嗅探/页面 hook/运行时 hook 三条旧路全部失效。新方案 hook
   `SourceBuffer.appendBuffer` 拿已解密 fMP4 分片，经 `POST /__cuin/mse` 上传落盘。
2. **MSE 完整性修复（2026-09-26）**：修复"下载只有几秒/不能播放"——前端按缓冲区间+时长判完整，
   后端逐轨 FFmpeg 完整解码核对、合并分离的音视频轨、HEVC 转 H.264/AAC、输出 faststart MP4。
3. **Windows 代理同步修复（2026-10-03）**：微信 `WeChatAppEx.exe` 实际读 `InternetQueryOptionW(75)`
   的 LAN 代理（原来是 Clash 的 7897），下载器只写顶层注册表不生效。现同步全部三处 + 备份恢复。
   **已真机验收**：微信流量进 8899、捕获 2 条视频（241 秒长片两轨完整解码通过）。
4. **回放直接下载（2026-10-04，最新）**：长回放无需播放完毕，按钮直接提交 CDN 原文件下载
   （观察播放器 Worker `CUT` 消息拿完整签名地址 + Vue 渲染树定位 Feed）。**离线测试通过，
   真机最终验收未完成**（见 §7 待办）。
5. **UI 与全链路审计（2026-10-03）**：控制台改版（主题切换、步骤化建任务、多链接识别去重、
   弹窗焦点管理、配置分组折叠与草稿保护），修复约 20 项跨平台缺陷（详见
   `docs/testing/ui-and-crawler-audit-2026-10-03.md` 的缺陷表）。
6. **配置中心目录浏览**：`GET /api/v1/fs/dirs` 只读列子目录 + 前端「浏览…」弹层选择下载目录。

### 1.3 当前可运行状态（2026-10-04 全部实测）

```
全量 Python 测试：1449 passed, 1 skipped（约 100 秒）   ✅
前端测试：       98 passed（node --test）              ✅
Ruff：           All checks passed                     ✅
```

skipped 的 1 项是 `test_proxy_validator_parity.py` 对旧导出符号 `_PROXY_ALLOWED_SCHEMES` 的
兼容性测试（故意跳过，与视频号无关，勿"修复"）。

启动方式（开发环境）：

```bash
# REST 服务 + 网页控制台（浏览器打开 http://127.0.0.1:8000）
python run.py --serve --serve-port 8000
# 开发时建议用示例配置，避免污染真实 config.yml
python run.py --config config.example.yml --serve --serve-port 8010
# CLI 下载
python run.py --config config.yml <链接>
```

环境：Windows 10/11，Python **3.13.15**（项目 `.venv/` 已存在）。CI 矩阵跑 3.9–3.13。

---

## 2. 项目结构与核心模块

```
douyin-downloader/
├── run.py                  # 唯一入口：sys.path 引导 + chdir 后调 cli.main:main()
├── __init__.py             # __version__ = "2.0.4"（CLI 横幅动态读取，勿硬编码）
├── config.example.yml      # 示例配置（可提交）；config.yml 是真实凭证（不可提交）
├── cli/                    # 参数解析、下载编排、download_url 平台路由、--serve/--channels 会话
│   ├── main.py             #   路由顺序：Bilibili → ytdlp → Douyin 兜底；channels 链接只给嗅探引导
│   ├── channels_session.py #   CLI 视频号嗅探会话生命周期
│   └── link_input.py       # 【新】分享文案多链接离线抽取（CLI/Server 共用）
├── core/                   # 抖音引擎：API client、URL parser、各模式 downloader（策略模式 user_modes/）
├── bilibili/               # B 站引擎（与 core/ 平行实现，复用 storage/control/进度层）
├── ytdlp/                  # yt-dlp 引擎：域名注册表 detect_ytdlp_platform + downloader.py
├── channels/               # 视频号嗅探引擎（见 §4，最复杂、最易碎的模块）
├── server/                 # FastAPI REST + 网页控制台宿主
│   ├── app.py              #   build_app() 工厂，约 37 个端点（§3 有清单）；1406 行
│   ├── jobs.py             #   JobManager：并发/TTL/max_jobs；CURRENT_JOB ContextVar
│   ├── progress.py         #   core 进度回调 → job 对象桥接
│   ├── channels.py         #   服务端嗅探会话管理器（代理设置/恢复）
│   ├── auth.py             #   AuthPolicy：环回免 token，非环回必须 X-Auth-Token
│   └── input_validation.py # 【新】可变状态变更前的离线校验与可读中文报错
├── web/index.html          # 单文件控制台（2526 行，vanilla JS、离线、无 CDN）
├── auth/                   # Cookie / MS Token 管理
├── config/                 # ConfigLoader：YAML 加载、深合并 update、默认值
├── control/                # 并发控制：RateLimiter / RetryHandler / QueueManager
├── storage/                # Database（SQLite）+ FileManager + naming 模板
├── tests/                  # 107 个 pytest 模块 + tests/frontend/ 6 个 node --test 文件
├── docs/testing/           # 5 份专题排查/验收记录（其他 Agent 撰写，含复现命令）
├── scripts/ci_smoke.py
└── .verify/                # 【gitignore】真机验收材料、静态分析样本、pytest 输出（勿提交）
```

**未跟踪的新模块**（v2.0.4，尚未 commit）：

| 文件 | 职责 |
|------|------|
| `channels/mse_capture.py` | MSE 分轨上传拼装 → MP4 头校验 → 逐轨解码校验/时长核对 → 合并 → FeedStore/历史登记 |
| `channels/media_validation.py` | FFmpeg 验证与兼容转码（worker 线程；禁止网络协议输入） |
| `channels/replay_downloader.py` | **已停用的实验性回放拼接**，未接入 CLI/Server（保留作历史，见 §6 决策 3） |
| `channels/connection.py` | 只读 Windows 进程/TCP 证据：按 PID 判断微信是否真的走了嗅探代理 |
| `channels/inject/assets/media_source.js` | 观察原播放器 Worker `CUT` 消息（回放直接下载） |
| `server/input_validation.py` + `cli/link_input.py` | 入参校验、多链接抽取（两入口共用） |

---

## 3. REST API 与数据流

### 3.1 端点清单（server/app.py，行号为当前工作区版本）

- 基础：`GET /`（控制台）、`GET /api/v1/health`（**唯一公开端点**）
- 下载：`POST /api/v1/download`、`POST /api/v1/links/parse`（新：离线批量解析分享文案）
- 任务：`GET /api/v1/jobs[/{id}]`、`POST .../retry|cancel|pause|resume`、`DELETE ...[/...]`
- 配置：`GET|PUT /api/v1/config`、`GET /api/v1/cookies/status`、`GET /api/v1/fs/dirs`（新：目录浏览）
- 总览：`GET /api/v1/stats`、`GET /api/v1/history`、`GET /api/v1/manifest`
- 目录：`GET /api/v1/download-dir`、`POST /api/v1/open-folder`（限下载根目录内）
- 发现：`GET /api/v1/discovery/hot-board|search`
- 视频号：`GET /api/v1/channels/status`、证书 install/uninstall、start/stop、feeds、
  feeds/{id}/download、auto-download、network/repair、link/parse、`POST|DELETE /api/v1/channels/link`

### 3.2 关键调用链

**下载任务**：
`web/CLI → POST /download（extract_url_from_text 抠链接）→ JobManager.submit →
CURRENT_JOB(ContextVar) → _execute_download → cli.main.download_url 平台路由 →
各引擎 Downloader → FileManager（落盘）+ Database（aweme 表/历史）+ 进度回调 → progress.py → job`

**视频号嗅探会话**（`server/channels.py` + `channels/`）：
`POST /channels/start → ChannelsSessionManager → mitmproxy 程序化嵌入（interceptor.py）+
系统代理设置（proxy_recovery.py，Windows 注册表+WinINET，try/finally 恢复）→ 微信流量经过 →
四策略流水线（pipeline.py：A 被动嗅探/B 页面 fetch-XHR hook/C 运行时 hook/E MSE）→ FeedStore →
worker 自动下载 或 页面按钮 task_hub → 落盘/历史`

**页面注入链**：`injector.py 改写 HTML（gzip/br 透明+CSP 放宽）→ bootstrap.js（模块基址执行期捕获）
→ cuin_core.js（hook + MSE 分片收集 + base64 打包）→ /__cuin/feed|heartbeat|task|mse（virtual_host.py
本地响应，永不转发腾讯）→ Python 侧`

### 3.3 配置数据流（改动配置必须理解）

`GET /config` 下发 `_redacted_config()`（cookies/bilibili cookies/ytdlp cookies/cookie_file/
transcript.api_key/server.auth_token 全部 `***`）→ 前端表单渲染 → `PUT /config` 只接受
`_EDITABLE_CONFIG_KEYS` 白名单（server/app.py:78 起，**刻意不含任何凭据**；`server.auth_token`
在服务端被强制剔除防止自签发后门）→ `ConfigLoader.update()` 深合并 → `reload_runtime()` 热生效
→ `_write_config_file()` 写回 YAML。

---

## 4. 视频号引擎（channels/）——最重要也最易碎

微信没有免登录 Web API，整个模块是"嗅探形"架构，且微信 4.x 改版（原生桥接 + MSE）使
**MSE 分片旁路（策略 E）成为当前唯一可靠捕获路**。演进与实证全部记录在 `CHANGELOG.md`
v2.0.2–v2.0.4 与 `docs/testing/` 五份文档里，动这个模块前必须先读。

- **ISAAC64 解密**（`isaac64.py`）：seed = decodeKey（JSON 字符串形态的 uint64，**注意精度**），
  校验锚点：全零 seed 首输出必须为 `0x9d39247e33776d41`。仅 MP4 前 131072 字节被加密。
- **虚拟接口**（`virtual_host.py`）：`/__cuin/*` 全部本地响应、永不转发；严格校验同源
  Origin/Referer、body 大小（**4 MiB 上限**→前端按 **2 MiB** 分批 base64）、schema。
- **MSE 完整性门禁**（`mse_capture.py` + `media_validation.py`）：前端按「从零开始的连续缓冲
  覆盖 + 完整时长 + 加载结束」判定（跳播/切换/淘汰不算完整）；后端逐轨完整解码核对时长，
  合并音视频，HEVC→H.264/AAC，faststart MP4。**只有通过验证才登记 done**，失败不写去重标记。
- **单轨内存上限 384 MiB / 总量 512 MiB / 最多 4 链**：超限截断，长视频可能不完整（已知边界）。
- **页面注入资产**（`channels/inject/assets/`）：`bootstrap.js`（基址执行期捕获）、
  `cuin_core.js`（版本随主版本走，当前 2.0.4，纯逻辑函数 Node 可测）、`media_source.js`。
- **mitmproxy**：可选 extra `pip install ".[channels]"`，要求 Python ≥3.10、锁定 `<13`（大版本
  升级曾改内部 API）。嵌入时**移除 ErrorCheck 插件**（见 §6 决策 7）。

---

## 5. 环境、依赖、构建与测试

### 5.1 依赖要点（pyproject.toml）

- 运行核心：aiohttp / httpx / aiofiles / aiosqlite / rich / pyyaml / gmssl / yt-dlp≥2025.1.1 /
  **imageio-ffmpeg==0.6.0（精确锁定**，sidecar 构建可复现，勿随意 bump）
- 可选 extra：`server`（fastapi/uvicorn）、`browser`（playwright）、`channels`（mitmproxy≥10.1,<13），
  `transcribe`（openai-whisper）
- **Python 版本约束**：CI 全量矩阵 3.9–3.13；mitmproxy 限 3.10+；Release/lock 作业固定 3.13。
  代码里大量惰性 asyncio 原语垫片（见 §6 决策 8）——写新代码不要在模块 import 期构造
  Lock/Semaphore/Event/Queue。
- 修改依赖必须同步 `requirements.txt` / `requirements.lock`（有 `test_dependency_consistency.py` 看守）。

### 5.2 测试与验证命令（全部实测可用）

```bash
# 全量 Python 测试（约 100s；1449 passed, 1 skipped 为正常基线）
.venv/Scripts/python.exe -m pytest tests/ -q --no-header -p no:cacheprovider

# 前端测试（98 项）。【坑】必须用 glob 展开；写成 `node --test tests/frontend/`
# 这种目录形式在 Windows 上会直接失败。
node --test tests/frontend/*.test.js

# 静态检查
.venv/Scripts/python.exe -m ruff check .
git diff --check        # 空白检查

# 手动起服务验证控制台（用示例配置避免污染真实配置）
python run.py --config config.example.yml --serve --serve-port 8010
curl http://127.0.0.1:8010/api/v1/health
```

Windows 下若经 PowerShell 管道 pytest 输出丢失，用项目自带的 `_run_tests.py` 包装
（`--out=` 指定输出文件）。

### 5.3 Git 约定（重要）

- **commit 由用户手动执行**（历史惯例，Agent 只改代码不 commit）；push 可由 Agent 执行。
- HEAD 在 `9d79c11`（v2.0.3），`origin/main` 同步；v2.0.4 全部改动堆在工作区未提交。
- 行尾：仓库混 LF/CRLF，`git diff` 时出现大量 "LF will be replaced by CRLF" 警告是
  core.autocrlf 正常现象，不是错误。

---

## 6. 重要设计决策与理由（不可随意推翻）

1. **凭据永不通过 HTTP 读写**。`_EDITABLE_CONFIG_KEYS` 白名单不含 cookies/api_key/auth_token；
   `PUT /config` 服务端强制剔除 `server.auth_token`（否则远程匿名请求可自设 token 绕过认证）；
   `GET /config` 一律脱敏下发。改配置中心前端时不得把这些字段加回 schema。
2. **`/api/*` 统一认证边界**：环回免 token，非环回必须 `X-Auth-Token`（`server/auth.py`），
   仅 `/api/v1/health` 公开。新增端点自动被覆盖，**不要**给新端点单开白名单。
3. **回放头部拼接已停用**：`cipher XOR (plain XOR cipher) ≡ plain`，该"解密"数学上恒等于原文，
   只是覆盖文件头、破坏索引。`channels/replay_downloader.py` 保留但**未接入 CLI/Server**，
   旧入口会拒绝这种伪解密。勿复活；正路是 Worker `CUT` 消息的完整签名 CDN 地址（2026-10-04 方案）。
4. **MSE 保存是"边播边录"**：没有 decodeKey 时必须从头连续播放；完整性门禁宁可失败也不产出
   "几秒的成功文件"。这是 2026-09-26 修复的核心语义，放宽门槛会让垃圾文件再次冒充成功。
5. **`/__cuin/*` 永不转发腾讯**；所有请求严格校验大小/同源/schema，任何失败原样放行不影响
   微信正常使用。改 virtual_host 时保持该语义。
6. **系统代理必须可恢复**：`proxy_recovery.py` 用 `InternetSetOptionW(75)` 同步三处注册表
   （顶层 + DefaultConnectionSettings + SavedLegacySettings），设置前备份、停止/异常都恢复。
   只写顶层注册表是已证实无效的旧方案，勿回退。
7. **嵌入 mitmproxy 时移除 ErrorCheck 插件**：该插件有 ERROR 日志时关闭期 `sys.exit(1)`，
   会炸掉与 FastAPI 共享事件循环的整个服务进程（v2.0.3 真机 P1）。
8. **py≤3.10 兼容**：asyncio 原语一律惰性构造（历史 commit f5125d4/0abb026/1ca794c）。
   模块级 `asyncio.Lock()` 等会在 3.9/3.10 上因无运行时而崩溃。
9. **媒体请求 `trust_env=False`**：视频号的 aiohttp 会话不走环境代理（用户不用 Clash 下视频），
   直播 FFmpeg 子进程同样剥离 `HTTP_PROXY` 等变量。这是用户明确要求的行为（2026-10-03 文档）。
10. **目录浏览接口只读、只列子目录**：`GET /api/v1/fs/dirs` 不返回文件与内容，空路径返回盘符
    列表，非环回受 token 保护。不要扩展成文件读取。
11. **yt-dlp Cookie 临时文件放下载目录并清理、默认缓存关闭**（v2.0.3 UI 审计修复，避免写用户环境）。

---

## 7. 待办、已知问题与风险

### 7.1 立即待办

1. **提交 v2.0.4**：58 修改 + 24 新增文件全部未 commit。工作区混有多位 Agent 的成果
   （MSE/完整性/代理同步/回放/UI/目录浏览），建议按主题拆分或整体一个 v2.0.4 commit，
   由用户确认后提交。提交前跑 §5.2 三件套。
2. **回放直接下载真机验收**（未闭环）：`.verify/channels-replay-real-validation.json` 的
   `fully_decoded=true` 才算通过；验收脚本 `.verify/retry_requested_replay.py` +
   `.verify/check_replay_saved.py`（十分钟窗口内等待指定 53:32 回放）。目标视频是
   TRAE.ai「搞懂token和上下文让AI省钱」。
3. tag v2.0.4 + Release（CI 走 `.github/workflows/release.yml`，固定 py3.13）。

### 7.2 已知边界/技术债

- **MSE 单轨 384 MiB 内存上限**：超长视频无法保存完整；分片落盘化是明确的改进方向。
- **MSE 命名兜底**：MSE 链路拿不到元数据，落盘用时间戳命名；可从页面 DOM 补标题/作者。
- **微信版本升级随时破坏注入/hook**（选择器、Worker 消息格式、Vue 内部结构都是非公开契约）；
  `channels/patches.py` Strategy D 框架默认不登记补丁，仅在真机证明 B/C 不稳时按版本登记。
- **上游平台接口失效风险**：yt-dlp 平台随上游 extractor 走（`pip install -U yt-dlp` 可修）；
  爱奇艺大陆站曾失效（iq.com 需 PhantomJS）。
- **离线测试 ≠ 真实可下载**：UI/解析回归全部基于 mock，Cookie 有效性、风控、地区限制需真机验证。
- 依赖弃用警告（DeprecationWarning 若干）未处理，按项目惯例不通过升级依赖解决。

### 7.3 风险与红线（不能踩）

- **敏感文件绝不可提交**（已被 .gitignore 覆盖，实测 check-ignore 通过，但别用 `-f` 加）：
  `config.yml`（真实抖音凭证）、`.cookies.json`、`dy_downloader.db*`（历史库）、`.verify/`、`.test-tmp/`。
- **不要把凭据字段加进任何"可经网页修改"的白名单**（见 §6 决策 1）。
- **不要在 `/api/` 边界外新开公开端点**。
- `virtual_host.py` 校验逻辑、`proxy_recovery.py` 的备份恢复语义、MSE 完整性门禁——改动前先读
  对应 `docs/testing/` 文档里的故障实证，那些方案是踩坑后修出来的。

---

## 8. 前端控制台速记（web/index.html）

单文件 2526 行，vanilla JS，无构建步骤；`server/app.py` 直接 `FileResponse` 下发。

- 全局 `state` + `api(path, options)` 封装（自动带 `X-Auth-Token`，存 sessionStorage；API 基址
  默认同源，可在「连接设置」弹层改，存 localStorage `dy_console_api_base`）。
- 配置中心由 `settingsSchema()`（约 line 2185 起）驱动渲染；字段类型 `switch/select/modes/json/
  number/text/dir`。**`dir` 类型**=输入框+「浏览…」按钮 → `#dirModal` 弹层（目录选择器）。
- 弹层基础设施：`showModal(id, focusBox)`（line 2485）统一 Escape/焦点限制/返回原焦点；新弹层复用 `.modal-mask`。
- 配置保存：收集 `[data-path]` → diff 现值 → `PUT /api/v1/config`，只发变更键。
- 【坑】目录弹层的 `data.parent` 为**空字符串**表示"回到此电脑/盘符列表"，是有效值；
  写成 `data.parent || null` 会把它误吞成 null 导致"上一级"按钮失效。必须显式 `=== null` 判断。
- 【坑】配置里的下载目录可能是相对路径（`./Downloaded/`），打开目录弹层时前端已做回落
  （非 `/^[a-zA-Z]:[\\/]|[\\/]/` 开头 → 从盘符列表开始浏览），改代码时保持该回落。

---

## 9. 下一步推荐开发顺序

1. **先提交**（§7.1 第 1 条）——当前工作区承载了 5+ 主题的成果，任何后续开发都应基于干净基线。
2. 完成回放直接下载真机验收（§7.1 第 2 条），更新 `docs/testing/channels-replay-direct-20261004.md`。
3. tag + Release v2.0.4，写 CHANGELOG「未发布」段为正式版本号。
4. 视线可选增强（按价值排序）：
   - MSE 分片落盘化（解除 384 MiB 上限，支持长视频）；
   - MSE 元数据增强（从页面 DOM 提取标题/作者，替代时间戳命名）;
   - UI 审计文档中"离线通过但需真机验证"的清单逐平台真机回归；
   - 依赖弃用警告清理。

---

## 10. 不确定信息（如实标注）

- 本文档对 `web/index.html`、`server/app.py` 内部行号均基于 2026-10-04 工作区快照，commit 后会漂移。
- "v2.0.4 需拆分 commit 还是整体提交"是用户决策，本文档只给建议。
- `.verify/` 下各 JSON/日志为其他 Agent 的真机验收原始材料，本文档只转述其结论，未逐字节复核。
- 微信 4.1.15.13 的行为结论（原生桥接、MSE、Worker CUT 消息）来自 2026-09-24 ~ 2026-10-04
  真机实证记录，**对其他微信版本不保证成立**。

<p align="center">
  <img src="img/logo.png" width="120" alt="douyin-bili-downloader" />
</p>

# douyin-bili-downloader

[中文](#中文) | [English](#english)

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT" /></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python 3.9+" /></a>
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="Platform" />
</p>

多平台视频批量下载工具：抖音、哔哩哔哩、微信视频号，以及基于 yt-dlp 引擎的爱奇艺 / 腾讯视频 / 优酷等平台。支持命令行、REST API 与网页控制台三种使用方式。

A multi-platform batch downloader for Douyin, Bilibili, WeChat Channels, and yt-dlp-powered sites (iQIYI, Tencent Video, Youku, and more). Usable as a CLI, a REST API server, or a single-file web console.

---

## 中文

### 项目简介

`douyin-bili-downloader` 是一个基于 Python 的多平台视频批量下载工具，覆盖以下平台：

- **抖音**：单个视频 / 图文 / 合集 / 音乐、短链解析、作者主页批量下载（发布 / 喜欢 / 合集 / 音乐）、登录账号收藏夹、直播录制、评论采集、热搜榜与关键词搜索
- **哔哩哔哩**：单稿件（含分 P）、UP 主投稿、合集 / 系列、收藏夹、`b23.tv` 短链，DASH 音视频自动合并
- **微信视频号**：本地 MITM 嗅探模式，边刷边自动捕获并下载（视频为加密 MP4，自动 ISAAC64 解密；图文、直播回放同样支持）
- **其他平台**（yt-dlp 引擎）：爱奇艺、腾讯视频、优酷、芒果 TV、快手、西瓜视频、今日头条、微博、小红书

所有链接混用时按域名自动分流。提供命令行（CLI）、REST API 服务与单文件网页控制台三种使用方式。

### 功能特性

**平台能力**

- 抖音：无水印优先、自动选择最高码率、封面 / 音乐 / 头像 / JSON 元数据一并保存
- 哔哩哔哩：画质 / 编码 / 音质可配置，增量下载按 `bvid + 分 P` 粒度补齐
- 微信视频号：不注入、不改写微信前端（不依赖其 DOM / JS bundle），仅被动嗅探本机流量，显著降低前端改版导致的失效概率；仍依赖视频号 API 数据结构、字段、加密方式及 CDN 行为。视频仅前 128 KiB 加密，流式解密并做 MP4 魔数自校验。MITM 解密范围限定在 `weixin.qq.com` 域名白名单内，其余流量不解密
- 其他平台：单视频与剧集列表页均可，按平台配置 Cookie，画质可选，失败时分类提示（DRM / 需登录 / 地区限制 / 站方改版）

**工程能力**

- 并发下载（默认 5）、指数退避重试（1s / 2s / 5s）、默认 2 请求/秒限速
- SQLite 下载历史 + 本地文件双重判重，磁盘增量下载（`increase` 配置）
- 时间过滤（`start_time` / `end_time`）与数量限制（`number`，0 为不限）
- 翻页受限时浏览器兜底（Playwright，支持人工过验证码）
- 下载完整性校验（Content-Length 比对，不完整文件自动清理重试）
- Rich 进度条展示，支持 `progress.quiet_logs` 静默模式
- 完成通知推送：Bark / Telegram / Webhook（含企业微信、飞书、钉钉）
- 可选视频转写（OpenAI Transcriptions API，或本地 Whisper）
- Docker 部署（提供 Dockerfile）

### 环境要求

- Python 3.9 或更高版本（`pyproject.toml` 要求 `>=3.9`）
- 微信视频号嗅探为可选扩展，需要 Python 3.10+（mitmproxy 10+ 的要求）
- 可选：ffmpeg（HLS 直播流后处理；转写功能使用内置的 imageio-ffmpeg 静态二进制，无需单独安装）
- 可选：Playwright + Chromium（浏览器兜底、自动 Cookie 捕获）
- 操作系统：Windows / macOS / Linux

### 快速开始

#### 1) 获取代码并安装依赖

```bash
git clone https://github.com/yangxijia111/douyin-bili-downloader.git
cd douyin-bili-downloader
pip install -r requirements.txt
```

按需安装可选扩展（定义于 `pyproject.toml`）：

```bash
pip install ".[browser]"      # 浏览器兜底与自动 Cookie 捕获（Playwright）
pip install ".[channels]"     # 微信视频号嗅探（Python 3.10+）
pip install ".[server]"       # REST API 服务与网页控制台（FastAPI + Uvicorn）
pip install ".[transcribe]"   # 本地 Whisper 转写引擎（可选）
```

#### 2) 创建配置文件

```bash
cp config.example.yml config.yml
```

`config.example.yml` 内含全部配置项的说明与安全占位符（如 `YOUR_CSRF_TOKEN`），按需修改即可。

#### 3) 获取 Cookie（推荐自动方式）

```bash
python -m tools.cookie_fetcher --config config.yml
```

在弹出的浏览器中登录抖音，回到终端按回车，Cookie 会自动写入 `config.yml`。

#### 4) 运行

```bash
python run.py -c config.yml
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `-u, --url` | 追加下载链接，可重复传入 |
| `-c, --config` | 配置文件路径（默认 `config.yml`） |
| `-p, --path` | 下载目录 |
| `-t, --thread` | 并发下载数 |
| `--show-warnings` | 显示警告日志 |
| `-v, --verbose` | 显示详细日志 |
| `--hot-board [N]` | 拉取抖音热搜榜并导出 JSONL，可选上限 N |
| `--search KEYWORD` | 按关键词搜索作品并导出 JSONL |
| `--search-max N` | `--search` 最大拉取条数（默认 50） |
| `--serve` | 以 REST API 服务模式运行（需安装 `fastapi + uvicorn`） |
| `--serve-host HOST` | 服务监听地址（默认 `127.0.0.1`） |
| `--serve-port PORT` | 服务监听端口（默认 `8000`） |
| `--channels` | 进入微信视频号嗅探会话（需安装 `mitmproxy`，即 `pip install ".[channels]"`） |
| `--channels-port` | 嗅探代理端口（默认取 `channels.proxy_port` 配置，8899） |
| `--channels-uninstall-ca` | 卸载嗅探根证书（按本机 CA 指纹精确删除，不触碰其它证书） |
| `--repair-network` | 检查并恢复上次异常退出残留的系统代理（幂等；不覆盖你手动改过的设置） |
| `--version` | 显示版本号 |

### 典型用法

#### 下载单个视频 / 图文 / 合集 / 音乐

```yaml
link:
  - https://www.douyin.com/video/7604129988555574538
```

#### 批量下载作者主页作品

```yaml
link:
  - https://www.douyin.com/user/MS4wLjABAAAAxxxx
mode:
  - post        # 可选 post / like / mix / music，可多选
number:
  post: 50      # 0 表示不限数量
```

#### 下载登录账号的收藏夹

```yaml
link:
  - https://www.douyin.com/user/self?showTab=favorite_collection
mode:
  - collect     # collect / collectmix 需单独使用，不能与其它模式混用
```

#### 下载哔哩哔哩内容

```yaml
link:
  - https://www.bilibili.com/video/BV1xx411c7mD     # 单稿件，?p=N 指定分 P
  - https://space.bilibili.com/123456/video          # UP 主投稿
  - https://www.bilibili.com/bangumi/play/ep123456   # 合集 / 系列
  - https://space.bilibili.com/123456/favlist?fid=100  # 收藏夹（需登录 Cookie）

bilibili:
  quality: highest
```

#### 下载其他平台（爱奇艺 / 腾讯视频 / 优酷等）

```yaml
link:
  - https://www.iqiyi.com/v_xxxxx.html           # 爱奇艺
  - https://v.qq.com/x/cover/xxx/yyy.html        # 腾讯视频
  - https://v.youku.com/v_show/id_xxx.html       # 优酷
  - https://www.mgtv.com/b/xxx/yyy.html          # 芒果 TV
  - https://v.kuaishou.com/AbCd12                # 快手短链
  - https://www.ixigua.com/7123456789            # 西瓜视频
  - https://weibo.com/tv/show/1034:4xxxx         # 微博视频
  - https://www.xiaohongshu.com/explore/xxx      # 小红书

ytdlp:
  quality: highest
  number:
    video: 0        # 剧集列表页展开后的上限，0 为不限
```

也可通过环境变量 `YTDLP_COOKIE_FILE` 指定浏览器导出的 Netscape 格式 `cookies.txt`，对所有平台生效。

#### 微信视频号嗅探下载

视频号没有免登录的网页 API（登录态只存在于本机微信客户端内），因此采用嗅探模式：`--channels` 启动本机 MITM 代理，在本机微信里刷视频号即自动捕获并下载。

```bash
pip install ".[channels]"    # 一次性安装可选依赖（Python 3.10+）
python run.py --channels
```

首次使用需在 Windows 证书确认对话框中信任本地生成的根证书（每机唯一，存于 `~/.mitmproxy`；会话结束后自动恢复系统代理；异常强杀导致的代理残留会在下次启动时自动检测恢复，也可 `python run.py --repair-network` 手动恢复）。

**能力边界（如实说明）**：此方案不依赖微信前端 DOM 和 JS bundle 注入，可显著降低前端页面改版造成的失效概率；**仍依赖**视频号 API 数据结构（`objectDesc` / `media` / `mediaType` / `liveInfo`）、字段（`decodeKey`）、ISAAC64 加密方式及 CDN URL 行为——微信协议层改动仍可能需要适配更新。

**MITM 最小权限**：HTTPS 解密仅限 `weixin.qq.com` 域名白名单（视频号 API 所在域，见 `channels/domains.py`），QQ 其它子域、CDN 与无关网站一律隧道转发不解密；如需扩展须通过 `channels.intercept_domains` 配置并自担评估责任。

**证书生命周期**：`python run.py --channels-uninstall-ca` 可精确卸载根证书（按本机 CA 指纹删除，不触碰其它证书）；网页控制台「视频号」页提供安装 / 卸载 / 详情入口。详见 `SECURITY.md`。详见 `config.example.yml` 的 `channels` 配置段。

#### 直播录制（实验性）

```yaml
link:
  - https://live.douyin.com/123456789
live:
  max_duration_seconds: 3600   # 0 表示录到主播下播
```

#### 热搜榜 / 关键词搜索

```bash
python run.py --hot-board 30 -p ./Downloaded
python run.py --search "猫咪" --search-max 100 -p ./Downloaded
```

#### 重新下载已下载过的内容

增量下载基于「本地主文件是否存在」判断。想强制重新下载，需要删除本地文件（文件夹名含作品 ID）；仅删数据库不会触发重下。

```bash
rm -rf "Downloaded/AuthorName/post/2024-02-07_Title_<aweme_id>/"
```

### 网页控制台

项目内置单文件网页控制台（`web/index.html`，纯离线、无 CDN 依赖），由 REST 服务模式托管：

```bash
pip install fastapi uvicorn    # 一次性可选依赖
python run.py --serve --serve-port 8000
```

启动后访问 <http://127.0.0.1:8000/> ，提供总览、链接下载（批量提交、可直接粘贴 App 分享文案自动提取链接）、任务中心（实时进度条、暂停 / 继续 / 取消 / 重试）、数据发现、下载档案、配置中心与视频号嗅探等页签。下载任务后台异步执行；凭据字段不会下发到浏览器。

### REST API

> **远程访问与认证（2.0.1）**：服务默认监听 `127.0.0.1`，本机请求免认证，行为与旧版一致。若用 `--serve-host 0.0.0.0` / 局域网 IP / `::` 对外开放，远程请求必须携带令牌——先在 `config.yml` 配置 `server.auth_token`（或环境变量 `DOWNLOADER_API_TOKEN`），请求头带 `X-Auth-Token: <token>`（网页控制台会自动弹窗询问）。未配置令牌时远程请求一律 `403` 拒绝。**不建议把 REST 服务暴露到公网**，详见 `SECURITY.md`。

服务模式暴露的主要端点：

服务模式暴露的主要端点：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 网页控制台 |
| POST | `/api/v1/download` | 提交下载任务，返回 `{job_id, status}` |
| GET | `/api/v1/jobs/{job_id}` | 查询任务状态与实时进度 |
| GET | `/api/v1/jobs` | 列出近期任务 |
| POST | `/api/v1/jobs/{job_id}/retry` | 重试任务 |
| POST | `/api/v1/jobs/{job_id}/pause` | 暂停任务（当前项完成后、下次请求前生效） |
| POST | `/api/v1/jobs/{job_id}/resume` | 恢复暂停的任务 |
| POST | `/api/v1/jobs/{job_id}/cancel` | 取消任务（已下载文件保留） |
| GET | `/api/v1/config` | 读取配置（Cookie / API Key 脱敏） |
| PUT | `/api/v1/config` | 保存白名单内的配置键到 `config.yml` |
| GET | `/api/v1/history` | 分页查询 SQLite 下载历史 |
| GET | `/api/v1/stats` | 版本、任务分布、运行参数等统计 |
| GET | `/api/v1/health` | 健康检查 |

完成任务默认按 24 小时 TTL 与 500 条上限清理（进行中的任务不清理），可通过 `server.job_ttl_seconds` / `server.max_jobs` 调整。

### Docker 部署

```bash
docker build -t douyin-downloader .
docker run -v $(pwd)/config.yml:/app/config.yml -v $(pwd)/Downloaded:/app/Downloaded douyin-downloader
```

### 输出目录结构

默认配置（`folderstyle: true`）下的输出示例：

```text
Downloaded/
├── download_manifest.jsonl          # 追加式下载清单
├── hot_board/                       # --hot-board 输出
├── search/                          # --search 输出
└── AuthorName/
    ├── post/
    │   └── 2024-02-07_Title_aweme_id/
    │       ├── ...mp4
    │       ├── ..._cover.jpg
    │       ├── ..._music.mp3
    │       ├── ..._data.json
    │       ├── ..._comments.json    # comments.enabled 时
    │       └── ...transcript.txt    # transcript.enabled 时
    ├── like/  mix/  music/  collect/  collectmix/
    └── live/                        # 直播录制输出
```

### 项目结构

```text
douyin-downloader/
├── run.py                  # 入口脚本
├── config.example.yml      # 示例配置（复制为 config.yml 后编辑）
├── pyproject.toml          # 构建配置、依赖、CLI 入口（douyin-dl）
├── requirements.txt        # 运行依赖
├── Dockerfile              # Docker 部署
├── auth/                   # Cookie 与 msToken 管理
├── bilibili/               # 哔哩哔哩：URL 解析、WBI 签名、DASH 流、各类下载器
├── channels/               # 微信视频号：mitmproxy 嗅探、ISAAC64 解密、下载与直播录制
├── cli/                    # 命令行入口、进度展示、登录流程、转写
├── config/                 # 配置加载与默认值
├── control/                # 队列管理、限速器、重试处理器
├── core/                   # 抖音：API 客户端、各类下载器、评论、发现、ffmpeg
├── server/                 # REST API 服务（FastAPI）与任务管理
├── storage/                # SQLite 数据库、文件管理、元数据
├── tools/                  # cookie_fetcher、watch_server 等工具
├── utils/                  # Cookie 工具、命名、日志等
├── web/                    # 单文件网页控制台（index.html）
├── ytdlp/                  # 其他平台：yt-dlp 引擎封装
├── tests/                  # Pytest 测试套件
└── img/                    # 文档图片资源
```

### 运行测试

```bash
python -m pytest -q
ruff check .
```

### 已知限制

- 浏览器兜底目前仅对 `post` 模式完整验证，`like / mix / music` 主要依赖 API 正常分页
- `collect / collectmix` 仅支持当前已登录 Cookie 对应账号，且必须单独使用
- 直播录制 FLV 可直接播放；HLS 源只保存 playlist 文件（需 ffmpeg 后处理）；直播接口未覆盖所有场景，视为实验性
- 爱奇艺 / 腾讯视频 / 优酷 / 芒果 TV 的 VIP 专享内容受 DRM（ChinaDRM / Widevine）保护，任何工具都无法直接获取；免费内容、试看片段与登录后可看的非 DRM 内容可正常下载
- 各平台实测状态（yt-dlp 2026.08.19）：腾讯视频 / 微博 / 今日头条免登录可下载；西瓜视频需要配置 Cookie；**爱奇艺大陆站（iqiyi.com）解析器上游当前失效**（见 yt-dlp [issue #12226](https://github.com/yt-dlp/yt-dlp/issues/12226)），上游修复后 `pip install -U yt-dlp` 即可；国际站（iq.com）可用但需要将 [PhantomJS](https://phantomjs.org/download.html) 放入 PATH；快手没有 yt-dlp 专用解析器，属尽力而为
- 未登录时抖音翻页仅能稳定获取首页，完整批量下载建议配置 Cookie

### 免责声明

本项目仅用于技术研究、学习与个人数据管理。请合法、负责任地使用：

- 不得用于侵犯他人隐私、著作权等合法权益
- 不得用于任何违法用途
- 使用本软件产生的一切风险与责任由使用者自行承担
- 平台政策或接口变化导致功能失效属正常技术风险

### 贡献指南

欢迎提交 Issue 与 Pull Request：

1. Fork 本仓库并基于 `main` 创建分支
2. 安装依赖：`pip install -r requirements.txt`（开发工具：`pytest`、`ruff`）
3. 修改代码并补充测试
4. 本地通过测试与 lint：`pytest -q`、`ruff check .`
5. 提交 Pull Request 并清晰描述改动

提交 Issue 时请说明平台、Python 版本，并务必抹除日志中的真实 Cookie 与 Token。

### 路线图

- [x] 哔哩哔哩下载支持（单稿件、UP 主投稿、合集 / 系列、收藏夹、短链）
- [x] 基于 yt-dlp 的其他平台下载（爱奇艺、腾讯视频、优酷、芒果 TV、快手、西瓜、头条、微博、小红书）
- [x] 微信视频号嗅探下载（视频、图文、直播回放、直播录制）
- [x] 网页控制台：实时进度条、任务暂停 / 继续 / 取消、档案浏览
- [ ] `like / mix / music` 模式的浏览器兜底覆盖
- [ ] 收藏夹模式（`collect / collectmix`）的增量截断
- [ ] 直播 HLS 录制的可播放输出（当前 FLV 原生，HLS 仅保存 playlist）

### 致谢

- [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) — 上游项目，本项目基于其二次开发；抖音核心、CLI 框架与文档结构来自上游
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — 其他平台的下载引擎
- [mitmproxy](https://github.com/mitmproxy/mitmproxy) — 微信视频号嗅探引擎
- [Rich](https://github.com/Textualize/rich) — 终端进度展示
- [FastAPI](https://github.com/fastapi/fastapi) 与 [Uvicorn](https://github.com/encode/uvicorn) — REST API 服务
- [Playwright](https://github.com/microsoft/playwright) — 浏览器兜底与自动 Cookie 捕获
- [F2](https://github.com/Johnserf-Seed/f2) — msToken 生成配置来源
- [wx_channels_download](https://github.com/ltaoo/wx_channels_download) 与 [WechatSphDecrypt](https://github.com/Hanson/WechatSphDecrypt) — 视频号解密方案的思路参考

### 开源许可证

本项目基于 MIT 协议开源，详见 [LICENSE](LICENSE)。

---

## English

### Introduction

`douyin-bili-downloader` is a Python-based multi-platform batch downloader covering:

- **Douyin**: single videos / image-notes / collections / music, short-link parsing, profile batch downloads (posts / likes / mixes / music), logged-in favorites, live recording, comments collection, hot-search board and keyword search
- **Bilibili**: single videos (multi-page), user uploads, collections/series, favourites, `b23.tv` short links, with automatic DASH audio/video merging
- **WeChat Channels (Shipinhao)**: local MITM sniffer mode — captures and downloads while you browse; encrypted MP4s are decrypted on the fly (ISAAC64), image posts and live replays included
- **Other platforms** (yt-dlp engine): iQIYI, Tencent Video, Youku, Mango TV, Kuaishou, Xigua, Toutiao, Weibo, Xiaohongshu

Mixed links are auto-routed by domain. The tool runs as a CLI, a REST API server, or a single-file web console.

### Features

**Platform capabilities**

- Douyin: watermark-free sources preferred, highest bitrate auto-selected, covers / music / avatars / JSON metadata saved alongside
- Bilibili: configurable quality / codec / audio, incremental resume per `bvid + page`
- WeChat Channels: passive sniffing only — no injection or rewriting of WeChat's frontend; only the first 128 KiB of each video are encrypted, decrypted streaming with an MP4 magic-number self-check
- Other platforms: single videos and series pages, per-platform cookies, selectable quality, failures classified (DRM / login required / geo-restricted / site changed)

**Engineering capabilities**

- Concurrent downloads (default 5), exponential-backoff retries (1s / 2s / 5s), 2 req/s rate limit by default
- SQLite history + on-disk file dual check, disk-based incremental downloads (`increase` config)
- Time filters (`start_time` / `end_time`) and count limits (`number`, 0 = unlimited)
- Browser fallback when pagination is restricted (Playwright, manual CAPTCHA supported)
- Download integrity checks (Content-Length validation, incomplete files auto-cleaned and retried)
- Rich progress bars, with a `progress.quiet_logs` quiet mode
- Completion notifications: Bark / Telegram / Webhook (WeCom, Feishu, DingTalk included)
- Optional video transcription (OpenAI Transcriptions API, or local Whisper)
- Docker deployment (Dockerfile included)

### Requirements

- Python 3.9 or newer (`pyproject.toml` requires `>=3.9`)
- The WeChat Channels sniffer is an optional extra and needs Python 3.10+ (required by mitmproxy 10+)
- Optional: ffmpeg (post-processing HLS live streams; transcription uses the bundled imageio-ffmpeg static binary)
- Optional: Playwright + Chromium (browser fallback, automatic cookie capture)
- OS: Windows / macOS / Linux

### Quick Start

#### 1) Clone and install

```bash
git clone https://github.com/yangxijia111/douyin-bili-downloader.git
cd douyin-bili-downloader
pip install -r requirements.txt
```

Optional extras (defined in `pyproject.toml`):

```bash
pip install ".[browser]"      # browser fallback & automatic cookie capture (Playwright)
pip install ".[channels]"     # WeChat Channels sniffer (Python 3.10+)
pip install ".[server]"       # REST API server & web console (FastAPI + Uvicorn)
pip install ".[transcribe]"   # local Whisper transcription engine (optional)
```

#### 2) Create your config

```bash
cp config.example.yml config.yml
```

`config.example.yml` documents every option with safe placeholders (e.g. `YOUR_CSRF_TOKEN`).

#### 3) Get cookies (automatic, recommended)

```bash
python -m tools.cookie_fetcher --config config.yml
```

Log into Douyin in the browser popup, return to the terminal and press Enter — cookies are written to `config.yml` automatically.

#### 4) Run

```bash
python run.py -c config.yml
```

### CLI Arguments

| Argument | Description |
|----------|-------------|
| `-u, --url` | Append download link(s), repeatable |
| `-c, --config` | Config file path (default: `config.yml`) |
| `-p, --path` | Download directory |
| `-t, --thread` | Concurrent downloads |
| `--show-warnings` | Show warning logs |
| `-v, --verbose` | Show verbose logs |
| `--hot-board [N]` | Fetch the Douyin hot-search board and export JSONL, optional top-N |
| `--search KEYWORD` | Search works by keyword and export JSONL |
| `--search-max N` | Max items for `--search` (default 50) |
| `--serve` | Run as a REST API server (requires `fastapi + uvicorn`) |
| `--serve-host HOST` | Listen host (default `127.0.0.1`) |
| `--serve-port PORT` | Listen port (default `8000`) |
| `--channels` | Enter a WeChat Channels sniffing session (requires `mitmproxy`, i.e. `pip install ".[channels]"`) |
| `--channels-port` | Sniffer proxy port (default: `channels.proxy_port` config, 8899) |
| `--channels-uninstall-ca` | Uninstall the sniffer root CA (exact fingerprint match; never touches other certificates) |
| `--repair-network` | Detect and restore a system proxy left behind by an abnormal exit (idempotent; never overrides your manual changes) |
| `--version` | Show version |

### Typical Usage

#### Download a single video / image-note / collection / music

```yaml
link:
  - https://www.douyin.com/video/7604129988555574538
```

#### Batch download a creator's works

```yaml
link:
  - https://www.douyin.com/user/MS4wLjABAAAAxxxx
mode:
  - post        # post / like / mix / music, multiple allowed
number:
  post: 50      # 0 = unlimited
```

#### Download your logged-in favorites collection

```yaml
link:
  - https://www.douyin.com/user/self?showTab=favorite_collection
mode:
  - collect     # collect / collectmix must be used alone
```

#### Download from Bilibili

```yaml
link:
  - https://www.bilibili.com/video/BV1xx411c7mD     # single video, ?p=N selects a page
  - https://space.bilibili.com/123456/video          # user uploads
  - https://www.bilibili.com/bangumi/play/ep123456   # collection / series
  - https://space.bilibili.com/123456/favlist?fid=100  # favourites (login required)

bilibili:
  quality: highest
```

#### Download from other platforms (iQIYI / Tencent Video / Youku, etc.)

```yaml
link:
  - https://www.iqiyi.com/v_xxxxx.html           # iQIYI
  - https://v.qq.com/x/cover/xxx/yyy.html        # Tencent Video
  - https://v.youku.com/v_show/id_xxx.html       # Youku
  - https://www.mgtv.com/b/xxx/yyy.html          # Mango TV
  - https://v.kuaishou.com/AbCd12                # Kuaishou short link
  - https://www.ixigua.com/7123456789            # Xigua
  - https://weibo.com/tv/show/1034:4xxxx         # Weibo video
  - https://www.xiaohongshu.com/explore/xxx      # Xiaohongshu

ytdlp:
  quality: highest
  number:
    video: 0        # cap after expanding a series/list page, 0 = unlimited
```

A browser-exported Netscape-format `cookies.txt` can also be supplied via the `YTDLP_COOKIE_FILE` environment variable, applying to every platform.

#### WeChat Channels sniffing download

Shipinhao has no login-free web API (auth lives only inside the local WeChat client), so this platform uses a sniffer: `--channels` starts a local MITM proxy that captures and downloads automatically while you browse Shipinhao in WeChat.

```bash
pip install ".[channels]"    # one-time optional dependency (Python 3.10+)
python run.py --channels
```

On first use, trust the locally generated root certificate in the Windows confirmation dialog (per-machine unique, stored in `~/.mitmproxy`; the system proxy is restored automatically when the session ends). See the `channels` section in `config.example.yml` for options.

#### Record a live stream (experimental)

```yaml
link:
  - https://live.douyin.com/123456789
live:
  max_duration_seconds: 3600   # 0 = record until the broadcaster ends
```

#### Hot-search board / keyword search

```bash
python run.py --hot-board 30 -p ./Downloaded
python run.py --search "猫咪" --search-max 100 -p ./Downloaded
```

#### Re-downloading content

Incremental downloads are decided by whether the primary media file exists on disk. To force a re-download, delete the local files (the folder name contains the work ID); deleting the database alone will not trigger it.

```bash
rm -rf "Downloaded/AuthorName/post/2024-02-07_Title_<aweme_id>/"
```

### Web Console

The project ships a single-file web console (`web/index.html`, fully offline, no CDN dependency), served by the REST API mode:

```bash
pip install fastapi uvicorn    # one-time optional dependency
python run.py --serve --serve-port 8000
```

Then open <http://127.0.0.1:8000/> for six tabs: Overview, Link Download (batch submission — paste a whole App share message and the URL is extracted automatically), Jobs (live progress bars, pause / resume / cancel / retry), Discovery, Archive, and Settings. Downloads run asynchronously in the background; credential fields are never sent to the browser.

### REST API

Main endpoints exposed in server mode:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web console |
| POST | `/api/v1/download` | Submit a download job, returns `{job_id, status}` |
| GET | `/api/v1/jobs/{job_id}` | Job status and live progress |
| GET | `/api/v1/jobs` | List recent jobs |
| POST | `/api/v1/jobs/{job_id}/retry` | Re-queue a job |
| POST | `/api/v1/jobs/{job_id}/pause` | Pause a job (takes effect after the current item) |
| POST | `/api/v1/jobs/{job_id}/resume` | Resume a paused job |
| POST | `/api/v1/jobs/{job_id}/cancel` | Cancel a job (downloaded files are kept) |
| GET | `/api/v1/config` | Read config (cookies / API keys redacted) |
| PUT | `/api/v1/config` | Persist whitelisted config keys to `config.yml` |
| GET | `/api/v1/history` | Paginated SQLite download history |
| GET | `/api/v1/stats` | Version, job breakdown, runtime parameters |
| GET | `/api/v1/health` | Health probe |

Finished jobs are pruned by TTL (default 24h) and max-jobs (default 500); in-flight jobs are never pruned. Tune via `server.job_ttl_seconds` / `server.max_jobs`.

### Docker Deployment

```bash
docker build -t douyin-downloader .
docker run -v $(pwd)/config.yml:/app/config.yml -v $(pwd)/Downloaded:/app/Downloaded douyin-downloader
```

### Output Structure

Example output with default settings (`folderstyle: true`):

```text
Downloaded/
├── download_manifest.jsonl          # append-only download manifest
├── hot_board/                       # --hot-board output
├── search/                          # --search output
└── AuthorName/
    ├── post/
    │   └── 2024-02-07_Title_aweme_id/
    │       ├── ...mp4
    │       ├── ..._cover.jpg
    │       ├── ..._music.mp3
    │       ├── ..._data.json
    │       ├── ..._comments.json    # when comments.enabled
    │       └── ...transcript.txt    # when transcript.enabled
    ├── like/  mix/  music/  collect/  collectmix/
    └── live/                        # live recording output
```

### Project Structure

```text
douyin-downloader/
├── run.py                  # Entry script
├── config.example.yml      # Example config — copy to config.yml and edit
├── pyproject.toml          # Build config, dependencies, CLI entry (douyin-dl)
├── requirements.txt        # Runtime dependencies
├── Dockerfile              # Docker deployment
├── auth/                   # Cookie & msToken management
├── bilibili/               # Bilibili: URL parser, WBI signing, DASH streams, downloaders
├── channels/               # WeChat Channels: mitmproxy sniffer, ISAAC64 decryption, downloads & live recording
├── cli/                    # CLI entry, progress display, login flow, transcription
├── config/                 # Config loading & defaults
├── control/                # Queue manager, rate limiter, retry handler
├── core/                   # Douyin: API client, downloaders, comments, discovery, ffmpeg
├── server/                 # REST API server (FastAPI) & job management
├── storage/                # SQLite database, file manager, metadata
├── tools/                  # cookie_fetcher, watch_server, etc.
├── utils/                  # Cookie utils, naming, logging, etc.
├── web/                    # Single-file web console (index.html)
├── ytdlp/                  # Other platforms: yt-dlp engine wiring
├── tests/                  # Pytest suite
└── img/                    # Documentation image assets
```

### Running Tests

```bash
python -m pytest -q
ruff check .
```

### Known Limitations

- Browser fallback is fully validated for `post` only; `like / mix / music` rely on API pagination
- `collect / collectmix` work for the logged-in cookie's account only and must be used alone
- Live recording saves FLV natively; HLS sources only save the playlist (ffmpeg post-processing needed); the webcast endpoint is experimental
- VIP-only content on iQIYI / Tencent Video / Youku / Mango TV is DRM-protected (ChinaDRM / Widevine) and cannot be downloaded by any tool; free content, previews, and non-DRM content visible after login work fine
- Per-platform status as tested (yt-dlp 2026.08.19): Tencent Video / Weibo / Toutiao download without login; Xigua requires cookies; **the mainland iQIYI (iqiyi.com) extractor is currently broken upstream** (yt-dlp [issue #12226](https://github.com/yt-dlp/yt-dlp/issues/12226)) — once fixed upstream, `pip install -U yt-dlp` is all that's needed; the international site (iq.com) works but needs [PhantomJS](https://phantomjs.org/download.html) on PATH; Kuaishou has no dedicated yt-dlp extractor and is best-effort
- Without cookies, Douyin pagination only reliably returns the first page — configure cookies for full batch downloads

### Disclaimer

This project is for technical research, learning, and personal data management only. Please use it legally and responsibly:

- Do not use it to infringe others' privacy, copyright, or other legal rights
- Do not use it for any illegal purpose
- Users are solely responsible for all risks and liabilities arising from usage
- Feature breakage caused by platform policy or API changes is a normal technical risk

### Contributing

Issues and Pull Requests are welcome:

1. Fork this repository and create your branch from `main`
2. Install dependencies: `pip install -r requirements.txt` (dev tools: `pytest`, `ruff`)
3. Make your changes and add tests where applicable
4. Pass tests and lint locally: `pytest -q`, `ruff check .`
5. Submit a Pull Request with a clear description

When filing an issue, include your platform and Python version, and always redact real cookies and tokens from logs.

### Roadmap

- [x] Bilibili download support (single videos, user uploads, collections/series, favourites, short links)
- [x] Other platforms via yt-dlp (iQIYI, Tencent Video, Youku, Mango TV, Kuaishou, Xigua, Toutiao, Weibo, Xiaohongshu)
- [x] WeChat Channels sniffing download (videos, image posts, live replays, live recording)
- [x] Web console: live progress bars, per-job pause/resume/cancel, archive browser
- [ ] Browser fallback coverage for `like / mix / music` modes
- [ ] Incremental stop for favorites-collection modes (`collect / collectmix`)
- [ ] Playable output for live HLS recordings (currently FLV native; HLS saves the playlist only)

### Acknowledgements

- [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) — the upstream project this fork is built on; the Douyin core, CLI framework, and documentation structure originate from it
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — download engine for other platforms
- [mitmproxy](https://github.com/mitmproxy/mitmproxy) — WeChat Channels sniffer engine
- [Rich](https://github.com/Textualize/rich) — terminal progress display
- [FastAPI](https://github.com/fastapi/fastapi) & [Uvicorn](https://github.com/encode/uvicorn) — REST API server
- [Playwright](https://github.com/microsoft/playwright) — browser fallback and automatic cookie capture
- [F2](https://github.com/Johnserf-Seed/f2) — source of the msToken generation config
- [wx_channels_download](https://github.com/ltaoo/wx_channels_download) & [WechatSphDecrypt](https://github.com/Hanson/WechatSphDecrypt) — approach references for Shipinhao decryption

### License

Licensed under the MIT License. See [LICENSE](LICENSE) for details.

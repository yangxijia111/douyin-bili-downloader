<!-- Generated: 2026-03-27 | Updated: 2026-05-08 -->

# douyin-downloader

## Purpose
A Python-based Douyin (TikTok China) batch downloader that fetches videos, galleries, music, and user content without watermarks. Supports multiple download modes (user posts, likes, mixes, music), concurrent downloads with rate limiting, cookie-based authentication, and optional Whisper transcription. CLI-driven with YAML configuration.

## Key Files

| File | Description |
|------|-------------|
| `run.py` | Entry point — bootstraps `sys.path` and delegates to `cli.main:main()` |
| `__init__.py` | Package version (`2.0.0`) |
| `pyproject.toml` | Build config, dependencies, CLI entry point (`douyin-dl`), tool settings |
| `config.example.yml` | Example YAML config for users to copy and customize |
| `requirements.txt` | Pinned dependency list (mirrors pyproject.toml) |
| `Dockerfile` | Container build for the downloader |
| `README.md` | Bilingual project documentation (features, usage, structure) |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `auth/` | Cookie and MS token management (see `auth/AGENTS.md`) |
| `bilibili/` | Bilibili downloads (single video incl. multi-page, user uploads, collections/series, favourites, `b23.tv` short links). Parallel implementation to `core/` with its own domain model (bvid/cid/WBI signing/DASH) that reuses only the infra layer (`storage`, `control`, `cli` progress, `utils.naming`). Also hosts `security.py`: outbound-URL scheme whitelist + private/reserved-address blocklist applied to every stream/subtitle/short-link request. Platform routing happens in `cli.main.download_url` / `server.app._execute_download` before any client is built (`bilibili.url_parser.detect_platform`). |
| `ytdlp/` | Other major platforms (iQIYI, Tencent Video, Youku, Mango TV, Kuaishou, Xigua, Toutiao, Weibo, Xiaohongshu) delegated to the yt-dlp engine. Parallel to `core/` and `bilibili/`: `url_parser.py` holds the domain → platform registry (`SUPPORTED_PLATFORMS`, `detect_ytdlp_platform`), `downloader.py` wires yt-dlp's Python API (options, Netscape cookie file, ffmpeg, progress hooks bridged via `call_soon_threadsafe`) to the shared `FileManager` / `utils.naming` / `aweme` table (`aweme_type="ytdlp_<platform>"`, `aweme_id="<platform>_<id>"`). Errors are classified as `drm` / `login` / `geo` / `unsupported` / `generic` so CLI/Server can give actionable hints. Routing order in both entry points: Bilibili → ytdlp → Douyin fallback. Per-platform credentials live under `ytdlp.cookies.<platform>`; `_config_snapshot()` in `cli.main` / `server.app` strips them (and every other platform's) before history rows are written. |
| `channels/` | WeChat Channels (Shipinhao) — the fourth platform link, **sniffer-shaped** (no login-free web API exists). `interceptor.py` embeds mitmproxy programmatically (optional extra `pip install ".[channels]"`, Python 3.10+) and manages the per-machine root cert (`~/.mitmproxy`, install via `certutil -addstore -user Root`) plus the Windows system proxy (HKCU registry + WinINET refresh, always restored via try/finally). `SnifferAddon` passively parses API responses (recursive `objectDesc` scan — no path whitelist, resilient to WeChat revisions; unlike the reference project wx_channels_download we inject/rewrite nothing). `feed.py` models captures (kind: video/image/live; `decodeKey` is a uint64 shipped as a JSON string); `isaac64.py` ports the ISAAC-64 keystream (seed `Seed[0]=decodeKey`, verified against the standard all-zero-seed first output `0x9d39247e33776d41`); only the first 131072 bytes of each MP4 are XOR-encrypted and are decrypted streaming during download with an MP4 `ftyp` self-check. `downloader.py` reuses `FileManager`/`naming`/`aweme` table (`aweme_id="channels_<objectId>"`); `live.py` records live FLV via ffmpeg. `worker.py` is the shared auto-download consumer (CLI `--channels` session in `cli/channels_session.py` and `server/channels.py` session manager + `/api/v1/channels/*` endpoints + web console "Shipinhao" tab). Shipinhao links cannot be direct-downloaded (browser has no WeChat auth): both entry points route them to a sniff-mode hint via `channels.url_parser.is_channels_url`. |
| `cli/` | CLI argument parsing, main async loop, progress display (see `cli/AGENTS.md`) |
| `config/` | YAML config loading, env var overrides, defaults (see `config/AGENTS.md`) |
| `control/` | Concurrency control — rate limiter, retry handler, queue manager (see `control/AGENTS.md`) |
| `core/` | Business logic — API client, URL parser, downloaders, strategy pattern (see `core/AGENTS.md`) |
| `server/` | FastAPI REST API + optional web console host (`app.py`, `jobs.py`, `progress.py`). Serves `web/index.html` and exposes config / history / stats / discovery endpoints plus per-job pause/resume/cancel. `progress.py` bridges `core`'s progress callbacks into the job object; `jobs.py` sets `CURRENT_JOB` (ContextVar) so the executor can find its job without changing its signature. CLI-only; the desktop sibling ships a richer server. |
| `storage/` | SQLite database, file management, metadata handling (see `storage/AGENTS.md`) |
| `tests/` | Pytest test suite with 80 test modules (see `tests/AGENTS.md`) |
| `tools/` | Standalone utilities like browser-based cookie fetching (see `tools/AGENTS.md`) |
| `utils/` | Shared helpers — logging, validation, anti-bot signatures (see `utils/AGENTS.md`) |
| `web/` | Single-file web console (`index.html`) — offline, no CDN, vanilla JS. Talks to `server/app.py` over `/api/v1/*`. Not a Python package; served via `FileResponse`. |

## For AI Agents

### Working In This Directory
- Python 3.8+ compatibility required — avoid walrus operator, `match` statements, and `type` aliases
- All I/O is async (`aiohttp`, `aiofiles`, `aiosqlite`) — never use blocking I/O in core paths
- Entry point is `cli.main:main()` which calls `asyncio.run(main_async(args))`
- Config is YAML-based with env var overrides (`DOUYIN_*` prefix)
- The `mix`/`allmix` config alias system requires special handling (see `config/config_loader.py`)

### Testing Requirements
- Run: `python -m pytest tests/`
- Async tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- Linting: `ruff check .` (target Python 3.8, line-length 100)

### Common Patterns
- Factory pattern for downloaders (`DownloaderFactory.create()`)
- Strategy pattern for user download modes (`core/user_modes/`)
- Registry pattern for mode discovery (`UserModeRegistry`)
- All downloaders inherit from `BaseDownloader` with shared `_download_mode_items()`
- Logging via `utils.logger.setup_logger(name)` — one logger per module

## Dependencies

### External
- `aiohttp` — async HTTP client for API calls and downloads
- `aiofiles` — async file I/O
- `aiosqlite` — async SQLite for download history
- `rich` — terminal UI (progress bars, tables, styled output)
- `pyyaml` — YAML config parsing
- `python-dateutil` — date/time parsing for time-range filters
- `gmssl` — Chinese SM3/SM4 crypto for anti-bot signatures

### Optional
- `playwright` — browser automation for cookie fetching
- `mitmproxy` — WeChat Channels sniffer engine (`channels/`, extra `channels`, Python 3.10+)
- `openai-whisper` — audio transcription

<!-- MANUAL: -->

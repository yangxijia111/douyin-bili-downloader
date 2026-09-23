# 贡献指南 / Contributing

## 开发环境

```bash
git clone https://github.com/yangxijia111/douyin-bili-downloader.git
cd douyin-bili-downloader

python -m venv .venv
# Windows
.venv/Scripts/python.exe -m pip install -e ".[all]"
# macOS/Linux
.venv/bin/python -m pip install -e ".[all]"
```

`[all]` 包含全部可选 extras（browser / channels / transcribe / server / dev）。
仅开发核心功能可只装 `-e ".[dev]"`；动视频号相关代码再装 `[channels]`（要求 Python 3.10+）。

## 提交前必过

```bash
pytest -q          # 全量测试（Windows 代理/证书逻辑全部走 mock，不会改系统状态）
ruff check .       # Lint
python -m build    # 构建门禁（改过打包配置时）
```

CI（`.github/workflows/ci.yml`）在 push / pull_request 时自动跑：
核心矩阵（ubuntu + windows × Python 3.9–3.12）、channels/server 矩阵（3.10+）、
`requirements.lock` 锁定复现作业（3.12）与构建门禁。

## 约定

- **语言**：代码注释、commit message、文档一律中文（对外文档可中英双语）。
- **测试先行**：修 bug 先写复现测试；新功能带测试。不要为了让测试通过而
  删除断言、放大 skip、或吞异常。
- **视频号（channels/）改动**：
  - 先更新 `tests/fixtures/channels/` 的脱敏 fixture 再动 parser，让协议
    结构变化在 review 中可见；
  - 域名白名单（`channels/domains.py`）是安全边界：新增域名必须给出依据
    （哪个接口、什么响应），禁止直接放宽到 `.qq.com`；
  - 任何触碰系统代理 / 证书存储的逻辑必须有 mock 测试，且恢复路径幂等。
- **安全相关改动**：REST API 的新端点默认纳入 `/api/*` 认证边界；新增可
  配置项若属于凭据类，必须保证不可经 HTTP 写入、读取时脱敏。
- **依赖**：运行时依赖保持范围声明（`>=`），其中 yt-dlp 不设上界；影响
  行为的大版本（如 mitmproxy）需带上界并在跨过时先过测试。同步更新
  `requirements.txt` 与 `requirements.lock`（后者由 `pip freeze
  --exclude-editable` 重建，`tests/test_dependency_consistency.py` 会校验）。

## 提交与发布

- commit message 用 conventional 风格（`feat:` / `fix:` / `docs:` / `chore:` …）。
- 发布由维护者执行：打 `vX.Y.Z` tag 推送后，Release 工作流生成草稿 Release
  （不会自动发布未打 tag 的版本），Release Notes 写入 `CHANGELOG.md` 同款内容。

# v2.0.2 Release Notes（草稿 — 待 Windows 真机验收后发布）

> **状态：代码完成、自动测试全绿、已推送 main。尚未创建正式 Release / tag
> —— 按发布硬门禁要求，须先完成 `docs/testing/channels-windows-smoke.md`
> 的真机验收（首页普通视频 × 2、详情页普通视频 × 1、直播回放 × 1）。
> 验收通过后，把下方「发布检查清单」逐项打勾再执行 tag + GitHub Release。**

## v2.0.2 — WeChat Channels Runtime Capture & In-Page Download

### 修复（P0）

**Windows 微信中视频号始终无法捕获**。v2.0.1 唯一依赖「mitmproxy 被动
响应 → body 含 `objectDesc` → json.loads」这一假设，真机实测证明它不足
以覆盖真实微信链路，且断点完全不可观测（只能显示「暂无嗅探结果」）。
v2.0.2 起**页面注入成为主要捕获方案**：向视频号页面注入 bootstrap，
非侵入 hook `fetch` / `XMLHttpRequest` 与 finder 运行时函数，捕获数据经
同源虚拟接口 `/__cuin/feed` 回传 Python 侧；被动嗅探保留为兜底策略。

### 新增

- **微信页面内下载按钮**（首页 / 详情页 / 直播页，视觉融入微信 UI）：
  - 首页推荐流：对应当前播放视频，切换自动跟随；
  - 详情页 / 作者主页：优先插入微信现有操作栏，找不到时悬浮按钮兜底；
  - 直播页：开始录制 / 停止录制（停止时保留已录制部分）；
  - 菜单按 feed 能力动态生成：下载 / 最高画质 / 最低画质 / 封面；
  - 未识别当前视频时给出引导提示（「请播放视频 1–2 秒或切换一次视频」），
    绝不无反应。
- **同源虚拟接口** `/__cuin/*`（mitmproxy 本地响应，永不转发腾讯）：
  虚拟静态资源、Feed 桥接、前端心跳、下载/录制任务与状态轮询。
  无 CORS、无公网暴露，`decodeKey` 与直链不经任何腾讯接口外泄。
- **四策略捕获流水线**：A 被动响应 / B 页面网络 hook / C 页面运行时
  hook / D 兼容补丁（框架就绪、默认无补丁），统一进 FeedStore 并按策略
  统计供数占比。
- **九级链路诊断**（替代「暂无嗅探结果」）：代理连接 → 目标域命中 →
  HTML 拦截 → 脚本注入 → 前端心跳 → 页面按钮 → Feed 获取 → 下载成功，
  第一个 ✗ 即断点，附 A–F 六种故障的针对性处置建议。CLI 与 Web 控制台
  「视频号」页同步展示。
- **前端单测**（`node --test`，CI channels-server 矩阵执行）：hook 语义
  保持（fetch 返回值完全不变 / XHR 行为不变 / hook 异常不影响微信）、
  objectDesc 收集、当前视频匹配、按钮模型。

### 变更

- `channels.auto_download` **默认改为 `false`**：刷视频只捕获、点按钮才
  下载，避免刷十几个视频全部自动保存；可在配置 / 网页控制台手动开启。
- 新增 `channels.inject_ui`（默认 true）与 `channels.patch_js_bundles`
  （默认 false）配置。
- HTML 注入正确性：gzip/br 透明、CSP 头与 meta 双形式放宽（补 `'self'`
  + 复制 nonce）、幂等、任何失败原样放行。

### 兼容性与风险（如实说明）

- 仍依赖视频号 API 数据结构（`objectDesc` / `media` / `mediaType` /
  `liveInfo`）、`decodeKey`、ISAAC64 与 CDN 行为；微信协议层改动仍可能
  需要适配。
- 页面注入为**非侵入 hook + 同源桥接**，不改写微信任何 JS；选择器链
  （primary → fallback → 悬浮）覆盖当前已知结构，微信大改版时按钮可能
  退化为悬浮形态（诊断链会显示断点）。
- 直播录制依赖本机 ffmpeg。
- 本项目 MIT；参考项目 ltaoo/wx_channels_download 为 MIT + Commons
  Clause，仅参考行为与技术路线，**未复制其源码**（独立实现）。

### 升级提示

```bash
pip install -U "douyin-downloader[channels] @ git+https://github.com/yangxijia111/douyin-bili-downloader.git"
```

`auto_download` 默认值变化只影响新配置；已有 `config.yml` 里显式写过
`true` 的老行为不变（想恢复自动下载就保留 `true`）。

### 发布检查清单（真机验收后执行）

- [ ] `docs/testing/channels-windows-smoke.md` 全部 12 项通过
      （最少样本：首页普通视频 × 2、详情页 × 1、直播回放 × 1）
- [ ] CI 全绿（core / channels-server / pinned / build 四矩阵）
- [ ] `git tag v2.0.2 && git push origin v2.0.2`
- [ ] GitHub Release（正文用本文件，去掉顶部状态说明）

from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "path": "./Downloaded/",
    # 处理内容开关：默认只保存视频本体。封面 / 音乐 / 头像 / 作品 JSON 都是
    # 附带产物，绝大多数用户并不需要，默认全开会平白多出几倍文件和请求。
    # 只影响新配置；已有 config.yml 里显式写过的值不受影响。
    "video": True,
    "music": False,
    "cover": False,
    "avatar": False,
    "json": False,
    "start_time": "",
    "end_time": "",
    "folderstyle": True,
    # 命名模板：渲染时可用变量见 utils/naming.py:ALLOWED_VARIABLES。默认保持
    # 与历史行为一致（`{date}_{title}_{id}`），用户可在设置中改写。
    "filename_template": "{date}_{title}_{id}",
    "folder_template": "{date}_{title}_{id}",
    # 作者目录层命名方式：
    #   "nickname"    - 作者昵称（默认，最直观，但重名会合并、改名会分裂）
    #   "sec_uid"     - 作者 sec_uid（稳定唯一，但不直观）
    #   "nickname_uid" - 昵称_sec_uid（直观 + 唯一）
    # 切换只影响后续下载，不会迁移已存在的目录。
    "author_dir": "nickname",
    # 是否按下载模式（post / like / mix …）再分一层子文件夹。
    #   True  - 作者目录下再分 post/like/... （默认，与历史行为一致）
    #   False - 不分模式层，文件直接落在作者目录下（复刻 legacy 布局，无 POST 文件夹）
    "group_by_mode": True,
    "download_pinned": False,
    # 下载博主作品时，是否在作者根目录覆盖保存主页地址文本。
    "author_url": False,
    # 下载博主作品时，是否在作者根目录覆盖保存一张主页首屏截图。
    "homepage_screenshot": False,
    "mode": ["post"],
    "number": {
        "post": 0,
        "like": 0,
        "allmix": 0,
        "mix": 0,
        "music": 0,
        "collect": 0,
        "collectmix": 0,
    },
    # 增量下载首先检查磁盘主文件。磁盘缺失时，True 会重新下载；False 会在数据库
    # 存在有效下载记录（file_path 非空）时继续跳过。默认 True 保持历史行为。
    "redownload_missing_files": True,
    # 各模式是否启用增量下载；False 会强制重下并原子覆盖当前筛选范围。
    "increase": {
        "post": True,
        "like": True,
        "allmix": True,
        "mix": True,
        "music": True,
    },
    "thread": 5,
    "retry_times": 3,
    "rate_limit": 2,
    "proxy": "",
    # 视频下载画质。可选值：
    #   "original" - 原画：探测上传原片（ratio=default，转码档列表之外，可比
    #                最高转码档大数倍），比最高转码档大则优先下载；探测失败
    #                退回最高转码档。代价是每条作品多一次探测请求（超时 10s）
    #   "highest"  - 最高转码档（默认）：只在 bit_rate 阶梯里挑，不发探测请求
    #   "lowest"   - 最低可用档（省流量）
    #   "1440p" / "1080p" / "720p" / "540p" / "480p" / "360p"
    #              - 指定分辨率，匹配不到时自动降级到最接近的可用档
    # 注：实际可用档位取决于原视频上传质量；完整尺寸按短边匹配，仅有 width 时兼容旧响应。
    "video_quality": "highest",
    "database": True,
    "database_path": "dy_downloader.db",
    "progress": {
        "quiet_logs": True,
    },
    "transcript": {
        "enabled": False,
        "model": "gpt-4o-mini-transcribe",
        "output_dir": "",
        "response_formats": ["txt", "json"],
        "api_url": "https://api.openai.com/v1/audio/transcriptions",
        "api_key_env": "OPENAI_API_KEY",
        "api_key": "",
        # When true (default), the desktop sidecar runs the source video
        # through ffmpeg locally and uploads only the extracted mono mp3
        # to the transcription endpoint. Saves bandwidth and avoids the
        # OpenAI 25 MiB single-file ceiling. Set to false to fall back to
        # uploading the source video itself (legacy behaviour). The UI
        # deliberately does not surface this toggle — see
        # ``.kiro/specs/transcript-audio-extract-and-ui`` Requirement 1.
        "upload_audio_only": True,
    },
    "auto_cookie": False,
    "browser_fallback": {
        "enabled": True,
        "headless": False,
        "max_scrolls": 240,
        "idle_rounds": 8,
        "wait_timeout_seconds": 600,
    },
    # 下载完成通知（可选）。providers 支持 bark / telegram / webhook。
    "notifications": {
        "enabled": False,
        "on_success": True,
        "on_failure": True,
        "providers": [],
    },
    # 评论采集（可选）。启用后每个作品会额外生成 *_comments.json。
    "comments": {
        "enabled": False,
        "include_replies": False,
        "max_comments": 0,  # 0 = 不限
        "page_size": 20,
    },
    # 直播录制（可选）。由 live.douyin.com / /follow/live/ 链接触发。
    "live": {
        "max_duration_seconds": 0,  # 0 = 直到流结束
        "chunk_size": 65536,
        "idle_timeout_seconds": 30,
    },
    # REST API 服务模式（可选，需 fastapi + uvicorn）。
    "server": {
        "max_jobs": 500,  # 内存中保留的 job 条数上限（不含 in-flight）
        "job_ttl_seconds": 86400,  # 完成态 job 保留时间（秒）
    },
    # 哔哩哔哩下载（可选）。link 里出现 bilibili.com / b23.tv 链接或裸 BV 号时
    # 自动启用；抖音侧的所有开关（path / 命名模板 / 线程 / 代理 / 数据库 /
    # 时间范围 / 通知）对 B 站同样生效，这里只放 B 站独有的部分。
    "bilibili": {
        # 总开关。关闭后 B 站链接会被明确拒绝而不是静默跳过。
        "enabled": True,
        # 登录凭据。SESSDATA 是唯一必需项：没有它只能拿到 360P/480P，且收藏夹
        # 接口直接返回 -101。bili_jct / buvid3 可选（buvid3 缺失会自动领取）。
        # 两种写法等价：
        #   cookie: "SESSDATA=xxx; bili_jct=yyy"
        #   cookies: {SESSDATA: xxx, bili_jct: yyy}
        "cookie": "",
        "cookies": {},
        # 画质偏好：highest / lowest / 8k / dolby / hdr / 4k / 1080p60 /
        # 1080p+ / 1080p / 720p60 / 720p / 480p / 360p / 240p。
        # 指定档位不可用时自动降级到最接近的可用档；实际可用上限由账号权限
        # 决定（未登录 480P、登录 1080P、大会员 4K/8K/HDR/杜比）。
        "quality": "highest",
        # 音频轨偏好：highest（无损/杜比/192K 优先）或 lowest。
        "audio_quality": "highest",
        # 视频编码偏好：auto / avc / hevc / av1。auto 按 avc→hevc→av1 兼容性
        # 优先选择；指定编码不存在时自动退回可用编码。
        "codec": "auto",
        # 只下载音频轨（保存为 .m4a）。适合当播客/音乐收藏用。
        "audio_only": False,
        # 附带产物开关。默认全关，与抖音侧保持一致的「只拿主媒体」策略。
        "download_cover": False,
        "download_subtitle": False,
        "download_danmaku": False,
        "download_json": False,
        # UP 主投稿排序：pubdate（最新发布）/ click（最多播放）/ stow（最多收藏）。
        "user_order": "pubdate",
        # 接口之间的最小请求间隔（秒）。B 站风控对同账号高频请求敏感，调小会
        # 提高被拦截概率。
        "request_interval": 0.5,
        # ffmpeg 路径。留空则用打包内置的 ffmpeg，其次搜索 PATH。
        # DASH 音视频合并必须依赖 ffmpeg。
        "ffmpeg_path": "",
        # 各链接类型的数量上限，0 = 不限。
        "number": {
            "video": 0,
            "user": 0,
            "collection": 0,
            "series": 0,
            "favlist": 0,
        },
        # 各链接类型是否启用磁盘增量（按 bvid 判定，多 P 稿件按分 P 粒度补齐）。
        "increase": {
            "video": True,
            "user": True,
            "collection": True,
            "series": True,
            "favlist": True,
        },
    },
    # 其他知名视频平台（可选，yt-dlp 引擎）。link 里出现爱奇艺 / 腾讯视频 /
    # 优酷 / 芒果 TV / 快手 / 西瓜视频 / 今日头条 / 微博 / 小红书 链接时自动
    # 启用；需要 `pip install yt-dlp`。抖音侧的通用开关（path / 命名模板 /
    # 线程 / 代理 / 数据库 / 增量）同样生效，这里只放引擎独有的部分。
    #
    # 能力边界：VIP 专享内容受 DRM 保护，任何工具都无法直接下载；免费内容与
    # 登录后可看的非 DRM 内容可下载，配置对应平台的 Cookie 可提升清晰度上限。
    "ytdlp": {
        # 总开关。关闭后这些平台的链接会被明确拒绝而不是静默跳过。
        "enabled": True,
        # 按平台单独开关；缺省视为开启。键名见 ytdlp.url_parser.SUPPORTED_PLATFORMS：
        # iqiyi / tencent / youku / mgtv / kuaishou / xigua / toutiao / weibo / xiaohongshu
        "platforms": {},
        # 按平台配置登录 Cookie（浏览器地址栏 F12 → Network → 请求头 Cookie 整段
        # 复制）。写法：
        #   cookies:
        #     iqiyi: "P00001=xxx; QC005=yyy"
        #     tencent: {vqq_vuserid: xxx, vqq_access_token: yyy}
        "cookies": {},
        # 或者直接指定浏览器导出的 Netscape 格式 cookies.txt（对所有平台生效，
        # 优先级高于 cookies）。
        "cookie_file": "",
        # 画质偏好：highest / lowest / 4k / 2k / 1080p / 720p / 480p / 360p。
        # 指定档位不可用时自动降级；实际上限由账号权限与站方策略决定。
        "quality": "highest",
        # 只下载音频轨（保存为 .m4a）。
        "audio_only": False,
        # 附带产物开关。默认全关，与抖音 / B 站侧保持一致的「只拿主媒体」策略。
        "download_cover": False,
        "download_subtitle": False,
        "download_json": False,
        # ffmpeg 路径。留空则用打包内置的 ffmpeg，其次搜索 PATH。分离音视频轨
        # 的站点合并必须依赖 ffmpeg。
        "ffmpeg_path": "",
        # 逃生舱：直接透传给 yt-dlp 的额外选项字典（如 {"geo_bypass": true}）。
        "extra_options": {},
        # 数量上限，0 = 不限。剧集页 / 列表页展开后按此截断。
        "number": {
            "video": 0,
        },
        # 磁盘增量（按 <平台>_<视频ID> 判定）。
        "increase": {
            "video": True,
        },
    },
    # 微信视频号嗅探下载（python run.py --channels 或网页控制台「视频号」页）。
    # 视频号没有免登录 Web API，登录态只在本机微信客户端里；嗅探模式下
    # 工具启动本机 MITM 代理（mitmproxy），被动读取微信内嵌浏览器流量中的
    # 视频直链与解密密钥，捕获后下载。详见 channels/ 包 docstring。
    "channels": {
        # 嗅探开关（URL 直链下载不受此影响；视频号链接本身不支持直链）。
        "enabled": True,
        # 捕获到新视频后是否自动下载。v2.0.2 起默认 False：微信页面按钮
        # 模式下「刷视频只捕获、点按钮才下载」才是合理语义——避免用户刷
        # 十几个视频就全部自动保存。仍可在网页控制台 / 配置里手动开启。
        "auto_download": False,
        # 是否向微信视频号页面注入下载按钮（bootstrap + 虚拟资源）。
        # False 时退化为 v2.0.1 的纯被动嗅探（无页面 UI）。
        "inject_ui": True,
        # Strategy D：拦截 res.wx.qq.com 的 JS bundle 并应用兼容补丁。
        # 默认 False（当前无已登记补丁）；开启需同时在 intercept_domains
        # 增加 res.wx.qq.com，且仅在有真机证据时使用。
        "patch_js_bundles": False,
        # 画质偏好：highest / lowest / 1080p / 720p / 480p …（匹配不到自动回退）。
        "quality": "highest",
        # 本机嗅探代理端口（默认避开 wx_channels_download 的 2022）。
        "proxy_port": 8899,
        # 自动模式下是否录制正在直播的 FLV 流（时长无上界，默认关；列表里
        # 可随时手动开始录制）。
        "live_record": False,
        # 附带下载封面图。
        "download_cover": False,
        # 增量下载（按 channels_<objectId> 判重）。
        "increase": True,
    },
}

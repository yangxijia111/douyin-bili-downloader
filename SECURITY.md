# 安全策略 / Security Policy

[中文](#中文) · [English](#english)

---

## 中文

### 支持版本

| 版本 | 支持状态 |
| ---- | -------- |
| 2.0.x | ✅ 接受安全报告 |
| < 2.0 | ❌ 请先升级 |

### 你应该知道的安全边界（视频号嗅探模式）

微信视频号下载采用**本机 MITM 嗅探**实现，使用前请知情：

1. **会安装本地根证书**。首次运行会在本机生成唯一的 mitmproxy CA（`~/.mitmproxy`，不与任何其他用户共享），安装到**当前用户**的 Root 存储（Windows 会弹系统确认框，拒绝即中止）。
2. **会临时修改系统代理**。嗅探会话期间把系统代理指向 `127.0.0.1:8899`，会话正常结束时自动还原；若进程被强杀（`taskkill /F`、崩溃、断电），下次启动本程序时会自动检测并恢复，也可手动执行 `python run.py --repair-network`。
3. **HTTPS 解密范围限定在域名白名单**。只解密 `weixin.qq.com` 的子域（视频号 API 所在域）；QQ 其它子域、CDN 与无关网站一律隧道转发、不解密、不留副本。白名单见 `channels/domains.py`。
4. **如何卸载根证书**：`python run.py --channels-uninstall-ca`，或网页控制台「视频号」页 →「卸载证书」。卸载按本机 CA 的 SHA-1 指纹精确删除，不会触碰你的其它证书。
5. **不要把 REST 服务暴露到公网**。默认只监听 `127.0.0.1`，本机请求免认证。若确需局域网访问：配置 `server.auth_token`（或环境变量 `DOWNLOADER_API_TOKEN`），远程请求必须携带 `X-Auth-Token`；未配置 token 时远程请求一律被拒绝。
6. **凭据只在本地**：抖音 / B 站 Cookie、transcript API key 等凭据不会被 REST API 返回（已脱敏为 `***`），也不能通过 HTTP API 写入。

### 报告安全问题

**请不要在公开 Issue 中报告安全漏洞。**

- 私信联系仓库所有者：GitHub [@yangxijia111](https://github.com/yangxijia111)
- 或使用 [GitHub Private Vulnerability Reporting](https://github.com/yangxijia111/douyin-bili-downloader/security/advisories/new)
- 请包含：影响版本、复现步骤、影响评估；我们会在 72 小时内确认、修复后披露。

---

## English

### Supported Versions

| Version | Supported |
| ------- | --------- |
| 2.0.x   | ✅ Reports accepted |
| < 2.0   | ❌ Please upgrade first |

### Security Boundaries You Should Know (WeChat Channels sniffer mode)

1. **A local root CA is installed.** First run generates a machine-unique mitmproxy CA in `~/.mitmproxy` and installs it into the **current user** Root store (Windows shows a confirmation dialog).
2. **The system proxy is temporarily changed.** During a sniffing session the system proxy points to `127.0.0.1:8899` and is restored on normal exit. After a hard kill, the leftover proxy is detected and repaired on the next start, or manually via `python run.py --repair-network`.
3. **HTTPS interception is limited to a domain allowlist.** Only `weixin.qq.com` subdomains are decrypted; everything else is tunnelled without decryption. See `channels/domains.py`.
4. **Uninstalling the CA**: `python run.py --channels-uninstall-ca`, or the web console → Channels → "卸载证书". Deletion matches the exact SHA-1 fingerprint of this machine's CA only.
5. **Do not expose the REST service to the public internet.** It binds to `127.0.0.1` by default. For LAN access set `server.auth_token` (or `DOWNLOADER_API_TOKEN`); remote requests without a valid `X-Auth-Token` are rejected (403 when no token is configured, 401 on bad token).
6. **Credentials stay local**: platform cookies and API keys are redacted from every API response and are never writable via HTTP.

### Reporting a Vulnerability

**Please do not open public issues for security reports.**

- Contact the owner: GitHub [@yangxijia111](https://github.com/yangxijia111)
- Or use [GitHub Private Vulnerability Reporting](https://github.com/yangxijia111/douyin-bili-downloader/security/advisories/new)
- Include affected version, reproduction steps and impact. We aim to acknowledge within 72 hours and disclose after a fix ships.

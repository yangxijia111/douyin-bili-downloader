"""本地下载进度控制面板：stdlib-only HTTP 服务。

功能:
- 实时展示 Downloaded/ 下载进度与运行日志
- 输入链接（博主主页/作品/分享短链）启动批量下载
- 启动 / 停止下载进程（增量下载，停止后可随时继续）

用法:
    .venv/Scripts/python.exe tools/watch_server.py [--port 8765] [--log <外部日志路径>] [--open]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "Downloaded"
DEFAULT_CONFIG = PROJECT_ROOT / "config.yml"
MANAGED_LOG = PROJECT_ROOT / "logs" / "watch_run.log"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TARGET_RE = re.compile(r"number:\s*\n\s*post:\s*(\d+)")
_LINK_BLOCK_RE = re.compile(r"(?m)^link:[ \t]*\n(?:[ \t]+-[^\n]*\n?)*")
_URL_RE = re.compile(r"https?://[^\s\"'<>，,；;]+")
_DOUYIN_HOST_RE = re.compile(r"^https?://([\w-]+\.)*(douyin\.com|iesdouyin\.com|douyinpic\.com|douyinvod\.com)/", re.I)


def extract_urls(text: str) -> List[str]:
    """从自由文本中提取抖音相关链接（每行一个或混在分享文案里均可）。"""
    urls: List[str] = []
    seen = set()
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(").,，。；;")
        if not _DOUYIN_HOST_RE.match(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def update_config_links(urls: List[str]) -> Optional[str]:
    """把链接列表写入 config.yml 的 link 块，保留文件其余内容与注释。"""
    try:
        text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    except OSError as exc:
        return f"读取 config.yml 失败: {exc}"
    entries = "".join(f"  - {u}\n" for u in urls)
    new_text, replaced = _LINK_BLOCK_RE.subn(f"link:\n{entries}", text, count=1)
    if replaced != 1:
        new_text = f"link:\n{entries}" + text
    try:
        DEFAULT_CONFIG.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return f"写入 config.yml 失败: {exc}"
    return None


class DownloadManager:
    """管理由面板启动的下载子进程。"""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.log_path: Optional[Path] = None
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, urls: Optional[List[str]] = None) -> Dict[str, Any]:
        with self.lock:
            if self.running:
                return {"ok": False, "error": f"已有下载任务在运行（PID {self.proc.pid}），请先停止"}
            if urls:
                err = update_config_links(urls)
                if err:
                    return {"ok": False, "error": err}
            MANAGED_LOG.parent.mkdir(parents=True, exist_ok=True)
            self.log_path = MANAGED_LOG
            lf = open(MANAGED_LOG, "wb")
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "run.py"],
                    cwd=str(PROJECT_ROOT),
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                )
            finally:
                lf.close()  # Popen 已持有句柄
            return {"ok": True, "pid": self.proc.pid}

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            if not self.running:
                return {"ok": False, "error": "当前没有运行中的下载"}
            proc, self.proc = self.proc, None
        pid = proc.pid
        if os.name == "nt":
            # 杀掉整棵进程树，避免 ffmpeg 等子进程残留
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            return {"ok": True, "pid": pid, "note": "进程退出等待超时"}
        return {"ok": True, "pid": pid}

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.proc.pid if self.running else None,
            "log_path": str(self.log_path) if self.log_path else None,
        }


manager = DownloadManager()


def _scan_downloads(download_dir: Path) -> Dict[str, Any]:
    completed: List[Dict[str, Any]] = []
    active: List[Dict[str, Any]] = []
    completed_size = 0
    if download_dir.exists():
        for root, _dirs, files in os.walk(download_dir):
            for fname in files:
                path = Path(root) / fname
                try:
                    st = path.stat()
                except OSError:
                    continue
                if fname.endswith(".tmp"):
                    active.append({"name": fname[: -len(".tmp")], "size": st.st_size, "mtime": st.st_mtime})
                else:
                    rel = path.relative_to(download_dir)
                    completed.append({"rel": str(rel), "name": fname, "size": st.st_size, "mtime": st.st_mtime})
                    completed_size += st.st_size
    completed.sort(key=lambda x: -x["mtime"])
    active.sort(key=lambda x: -x["mtime"])
    return {
        "completed": completed[:200],
        "active": active[:50],
        "stats": {
            "completed_count": len(completed),
            "active_count": len(active),
            "completed_size": completed_size,
            "target": _read_target(),
        },
    }


def _read_target() -> Optional[int]:
    try:
        m = _TARGET_RE.search(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        return int(m.group(1)) if m else None
    except OSError:
        return None


def _read_log_tail(max_lines: int = 40) -> List[str]:
    log_path = manager.log_path or external_log_path
    if not log_path or not log_path.exists():
        return []
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 16384))
            raw = f.read().decode("utf-8", errors="replace")
        lines = [_ANSI_RE.sub("", ln).rstrip() for ln in raw.splitlines()]
        return [ln for ln in lines if ln.strip()][-max_lines:]
    except OSError:
        return []


external_log_path: Optional[Path] = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, _HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            payload = _scan_downloads(DEFAULT_DOWNLOAD_DIR)
            payload["log_tail"] = _read_log_tail()
            payload["download_dir"] = str(DEFAULT_DOWNLOAD_DIR)
            payload.update(manager.status())
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            self._send(400, json.dumps({"ok": False, "error": "请求体不是合法 JSON"}).encode("utf-8"), "application/json; charset=utf-8")
            return

        if self.path == "/api/start":
            raw_text = body.get("urls_text") or ""
            urls = extract_urls(raw_text)
            if raw_text.strip() and not urls:
                self._send(400, json.dumps({"ok": False, "error": "未识别到有效的抖音链接"}).encode("utf-8"), "application/json; charset=utf-8")
                return
            result = manager.start(urls or None)
            code = 200 if result.get("ok") else 409
            self._send(code, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        elif self.path == "/api/stop":
            result = manager.stop()
            code = 200 if result.get("ok") else 409
            self._send(code, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # 静默访问日志
        return


_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>抖音批量下载控制台</title>
<style>
  :root {
    --bg: #0f1115; --card: #171a21; --border: #262b36;
    --text: #e6e9ef; --muted: #8b93a3; --accent: #fe2c55; --ok: #2fbf71; --warn: #e0a800;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: "Segoe UI", "Microsoft YaHei", sans-serif; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: var(--muted); font-size: 12px; margin-bottom: 20px; }
  .sub .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--ok); margin-right: 6px; }
  .cards { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 20px; min-width: 140px; }
  .card .num { font-size: 26px; font-weight: 700; }
  .card .lbl { color: var(--muted); font-size: 12px; margin-top: 2px; }
  section { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 16px; }
  section h2 { font-size: 14px; color: var(--muted); margin-bottom: 10px; font-weight: 600; }
  textarea { width: 100%; min-height: 84px; background: #0d0f13; color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-size: 13px; font-family: Consolas, monospace; resize: vertical; }
  textarea:focus { outline: 1px solid var(--accent); }
  .btn-row { display: flex; gap: 10px; margin-top: 12px; align-items: center; flex-wrap: wrap; }
  button { border: none; border-radius: 8px; padding: 9px 22px; font-size: 14px; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .4; cursor: not-allowed; }
  #btn-start { background: var(--accent); color: #fff; }
  #btn-stop { background: #2a2f3a; color: var(--text); border: 1px solid var(--border); }
  .badge { font-size: 12px; padding: 4px 10px; border-radius: 20px; border: 1px solid var(--border); color: var(--muted); }
  .badge.on { color: var(--ok); border-color: var(--ok); }
  .hint { color: var(--muted); font-size: 12px; margin-top: 10px; line-height: 1.7; }
  #ctrl-msg { font-size: 13px; margin-left: 4px; }
  #ctrl-msg.ok { color: var(--ok); } #ctrl-msg.err { color: var(--accent); }
  .bar { height: 6px; background: #232833; border-radius: 3px; overflow: hidden; margin-top: 6px; }
  .bar > i { display: block; height: 100%; width: 40%; background: var(--accent); border-radius: 3px; animation: slide 1.2s ease-in-out infinite; }
  @keyframes slide { 0% { transform: translateX(-100%); } 100% { transform: translateX(350%); } }
  .item { padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
  .item:last-child { border-bottom: none; }
  .item .name { word-break: break-all; }
  .item .meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
  pre { font-family: Consolas, monospace; font-size: 12px; line-height: 1.55; color: #b7bfcc; white-space: pre-wrap; word-break: break-all; max-height: 320px; overflow-y: auto; }
</style>
</head>
<body>
  <h1>抖音批量下载控制台</h1>
  <div class="sub">数据目录：<span id="dir"></span> · 页面每 2 秒自动刷新</div>

  <section>
    <h2>下载控制</h2>
    <textarea id="urls" placeholder="粘贴链接，每行一个（支持博主主页 / 作品页 / 分享短链及带文案的分享内容）&#10;例如：https://www.douyin.com/user/MS4wLjAB...&#10;留空则按 config.yml 中现有链接开始下载"></textarea>
    <div class="btn-row">
      <button id="btn-start" onclick="ctrl('/api/start')">开始下载</button>
      <button id="btn-stop" onclick="ctrl('/api/stop')" disabled>停止下载</button>
      <span class="badge" id="run-badge">检查中...</span>
      <span id="ctrl-msg"></span>
    </div>
    <div class="hint">
      · 停止后再开始不会重复下载：已完成的作品自动增量跳过，未完成的重新下载<br>
      · 链接会写入 config.yml 的 link 列表（其余配置不变）
    </div>
  </section>

  <div class="cards">
    <div class="card"><div class="num" id="st-done">-</div><div class="lbl">已完成文件</div></div>
    <div class="card"><div class="num" id="st-active">-</div><div class="lbl">正在下载</div></div>
    <div class="card"><div class="num" id="st-size">-</div><div class="lbl">已完成总大小</div></div>
    <div class="card"><div class="num" id="st-target">-</div><div class="lbl">目标作品数</div></div>
  </div>

  <section id="sec-active">
    <h2>正在下载</h2>
    <div id="active"></div>
  </section>
  <section>
    <h2>已完成文件（最新在前）</h2>
    <div id="done"></div>
  </section>
  <section>
    <h2>运行日志（末尾 40 行）</h2>
    <pre id="log">加载中...</pre>
  </section>

<script>
function fmtSize(n) {
  if (n == null) return "-";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
let running = false;
async function refresh() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    document.getElementById("dir").textContent = d.download_dir;
    document.getElementById("st-done").textContent = d.stats.completed_count;
    document.getElementById("st-size").textContent = fmtSize(d.stats.completed_size);
    document.getElementById("st-target").textContent = d.stats.target ?? "-";
    const badge = document.getElementById("run-badge");
    running = !!d.running;
    badge.textContent = running ? `运行中 · PID ${d.pid}` : "已停止";
    badge.className = "badge" + (running ? " on" : "");
    document.getElementById("btn-stop").disabled = !running;
    document.getElementById("st-active").textContent = running ? d.stats.active_count : 0;
    const act = document.getElementById("active");
    const actTitle = document.querySelector("#sec-active h2");
    if (!d.active.length) {
      actTitle.textContent = "正在下载";
      act.innerHTML = '<div class="empty">暂无进行中的下载</div>';
    } else if (running) {
      actTitle.textContent = "正在下载";
      act.innerHTML = d.active.map(a => `<div class="item"><div class="name">${esc(a.name)}</div><div class="meta">${fmtSize(a.size)} · 写入中</div><div class="bar"><i></i></div></div>`).join("");
    } else {
      actTitle.textContent = `未完成残留（已停止，共 ${d.active.length} 个）`;
      act.innerHTML = d.active.map(a => `<div class="item"><div class="name">${esc(a.name)}</div><div class="meta">已停止于 ${fmtSize(a.size)} · 重新开始下载后自动重下</div></div>`).join("");
    }
    const done = document.getElementById("done");
    done.innerHTML = d.completed.length
      ? d.completed.map(c => `<div class="item"><div class="name">${esc(c.rel)}</div><div class="meta">${fmtSize(c.size)} · ${new Date(c.mtime * 1000).toLocaleTimeString()}</div></div>`).join("")
      : '<div class="empty">还没有已完成的文件</div>';
    document.getElementById("log").textContent = d.log_tail.join("\\n") || "（无日志）";
  } catch (e) {
    document.getElementById("log").textContent = "刷新失败: " + e;
  }
}
async function ctrl(action) {
  const msg = document.getElementById("ctrl-msg");
  msg.textContent = ""; msg.className = "";
  const opts = { method: "POST", headers: { "Content-Type": "application/json" } };
  if (action === "/api/start") {
    opts.body = JSON.stringify({ urls_text: document.getElementById("urls").value });
  }
  try {
    const r = await fetch(action, opts);
    const d = await r.json();
    msg.textContent = d.ok ? (action === "/api/start" ? "已启动" : "已停止") : (d.error || "操作失败");
    msg.className = d.ok ? "ok" : "err";
    if (d.ok && action === "/api/start") document.getElementById("urls").value = "";
  } catch (e) {
    msg.textContent = "请求失败: " + e; msg.className = "err";
  }
  refresh();
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


def main() -> None:
    global external_log_path
    parser = argparse.ArgumentParser(description="本地下载进度控制面板")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", type=Path, default=None, help="外部下载日志（面板未托管下载进程时展示用）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()
    external_log_path = args.log

    Handler_ = Handler
    server = ThreadingHTTPServer((args.host, args.port), Handler_)
    url = f"http://{args.host}:{args.port}"
    print(f"[INFO] 控制台已启动: {url}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

/**
 * bootstrap.js 初始化冒烟测试（最小 DOM 桩）。
 *
 * 验证：脚本在类浏览器环境里能完整启动（不抛异常）、安装 fetch/XHR hook、
 * 按页面类型拼出正确的模块 URL（/__cuin/assets/ 兜底）。
 *
 * 注意：bootstrap 内部有多个 setInterval 轮询（运行时 hook 补装 / 当前
 * 视频识别 / 心跳 / SPA 路由），测试结束必须清理，否则 node 进程不退出。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");

const ASSETS = path.join(__dirname, "..", "..", "channels", "inject", "assets");

function makeElement(tag) {
  const el = {
    tagName: tag,
    style: {},
    children: [],
    listeners: {},
    className: "",
    textContent: "",
    parentNode: null,
    isConnected: false,
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute() {},
    getAttribute() { return null; },
    appendChild(child) { el.children.push(child); child.parentNode = el; return child; },
    removeChild(child) { el.children = el.children.filter((c) => c !== child); return child; },
    remove() {},
    addEventListener(type, fn) { (el.listeners[type] = el.listeners[type] || []).push(fn); },
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, left: 0, bottom: 0, right: 0 }; },
  };
  return el;
}

function makeEnv(pathname) {
  const head = makeElement("head");
  const body = makeElement("body");
  const appendedScripts = [];
  const timers = new Set();
  const doc = {
    readyState: "complete",
    currentScript: { src: "https://channels.weixin.qq.com/__cuin/assets/bootstrap.js" },
    head,
    body,
    addEventListener() {},
    createElement: (tag) => makeElement(tag),
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  const originalHeadAppend = head.appendChild.bind(head);
  head.appendChild = (child) => {
    if (child.tagName === "script") appendedScripts.push(child.src);
    return originalHeadAppend(child);
  };
  const win = {
    document: doc,
    location: { pathname: pathname || "/web/pages/home" },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {},
    fetch: async () => ({
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: true }),
    }),
    XMLHttpRequest: function XMLHttpRequest() {},
    console,
  };
  win.window = win;

  // 受控定时器：测试结束后统一清理。
  const trackedSetInterval = (fn, ms) => {
    const id = setInterval(fn, ms);
    timers.add(id);
    return id;
  };
  const trackedSetTimeout = (fn, ms) => {
    const id = setTimeout(fn, ms);
    timers.add(id);
    return id;
  };
  const cleanup = () => {
    for (const id of timers) {
      clearInterval(id);
      clearTimeout(id);
    }
    timers.clear();
  };
  return { win, appendedScripts, cleanup, trackedSetInterval, trackedSetTimeout };
}

function loadCore(win) {
  const code = fs.readFileSync(path.join(ASSETS, "cuin_core.js"), "utf8");
  const fn = new Function("window", "globalThis", "module", "exports", "require", code);
  fn(win, win, { exports: {} }, {}, require);
  const sourceCode = fs.readFileSync(path.join(ASSETS, "media_source.js"), "utf8");
  new Function("window", "globalThis", "module", sourceCode)(win, win, undefined);
}

function loadBootstrap(env) {
  const code = fs.readFileSync(path.join(ASSETS, "bootstrap.js"), "utf8");
  const fn = new Function(
    "window", "document", "location", "history", "fetch", "XMLHttpRequest",
    "setInterval", "clearInterval", "setTimeout", "clearTimeout", "console",
    code
  );
  fn(
    env.win, env.win.document, env.win.location, env.win.history,
    env.win.fetch, env.win.XMLHttpRequest,
    env.trackedSetInterval, clearInterval, env.trackedSetTimeout, clearTimeout,
    console
  );
  return env;
}

test("bootstrap 在首页环境完整启动并安装 hook", (t) => {
  const env = makeEnv("/web/pages/home");
  t.after(env.cleanup);
  loadCore(env.win); // core 必须先于 bootstrap（注入顺序保证）
  loadBootstrap(env);
  const { win, appendedScripts } = env;
  assert.ok(win.__CUIN__, "bootstrap 未注册 window.__CUIN__");
  assert.equal(win.__CUIN__.version, "2.0.4");
  assert.equal(typeof win.fetch, "function");
  // 首页模块按虚拟路径加载（currentScript 绝对路径或 /__cuin/ 兜底都算），
  // 关键是不能是页面相对路径（/web/pages/channels_home.js）。
  assert.ok(
    appendedScripts.some((src) => src.endsWith("/__cuin/assets/channels_home.js")),
    `模块 URL 不正确: ${JSON.stringify(appendedScripts)}`
  );
});

test("bootstrap 幂等：重复执行不重复注册", (t) => {
  const env = makeEnv("/web/pages/home");
  t.after(env.cleanup);
  loadCore(env.win);
  loadBootstrap(env);
  const first = env.win.__CUIN__;
  loadBootstrap(env);
  assert.equal(env.win.__CUIN__, first);
});

test("bootstrap 在详情页 / 直播页 / 作者页加载对应模块", (t) => {
  const envs = [];
  t.after(() => envs.forEach((e) => e.cleanup()));
  for (const [pathname, module] of [
    ["/web/pages/feed/abc", "channels_feed.js"],
    ["/web/pages/live", "channels_live.js"],
    ["/web/pages/profile", "channels_feed.js"],
  ]) {
    const env = makeEnv(pathname);
    loadCore(env.win);
    loadBootstrap(env);
    envs.push(env);
    assert.ok(
      env.appendedScripts.some((src) => src.endsWith("/" + module)),
      `${pathname} 应加载 ${module}，实际: ${JSON.stringify(env.appendedScripts)}`
    );
  }
});

test("bootstrap 启动异常被吞掉（不影响微信页面）", (t) => {
  const env = makeEnv("/web/pages/home");
  t.after(env.cleanup);
  // 不加载 cuin_core.js：bootstrap 应安静返回而不抛。
  loadBootstrap(env);
  assert.equal(env.win.__CUIN__, undefined);
});

test("内嵌详情的 id-only feed 从运行时 hook 上传且保留策略与完整字符串", async (t) => {
  const env = makeEnv("/web/pages/home");
  t.after(env.cleanup);
  const id = "14990527163746486645";
  const node = { id, objectDesc: { mediaType: 4, media: [{ url: "https://cdn/video.mp4" }] } };
  env.win.finderPcFlow = () => ({ data: { object: [node] } });
  const calls = [];
  env.win.fetch = async (url, opts) => {
    calls.push({ url, body: opts && opts.body && JSON.parse(opts.body) });
    return { json: async () => ({ ok: true, feeds: [] }) };
  };
  loadCore(env.win);
  loadBootstrap(env);
  const result = env.win.finderPcFlow();
  assert.equal(result.data.object[0], node);
  await new Promise((resolve) => setTimeout(resolve, 340));
  const upload = calls.find((call) => call.url === "/__cuin/feed");
  assert.ok(upload, "运行时下发的 id-only feed 应进入桥接队列");
  assert.equal(upload.body.feeds[0].id, id);
  assert.equal(upload.body.strategy, "page_runtime_hook");
  assert.equal(env.win.__CUIN__.state.index[0].object_id, id);
});

// ----------------------------------------------------------------------
// v2.0.4 MSE 自动保存：播放结束/链淘汰时自动上传，不再依赖手点按钮。
// ----------------------------------------------------------------------

function makeMseEnv(pathname) {
  const env = makeEnv(pathname);
  const docListeners = {};
  env.win.document.addEventListener = (type, fn) => {
    (docListeners[type] = docListeners[type] || []).push(fn);
  };
  // 记录版 fetch：bootstrap 加载时以参数形式捕获（postJson 用闭包 fetch），
  // 必须在 loadBootstrap 之前替换 env.win.fetch。
  const fetchCalls = [];
  env.win.fetch = async (path, opts) => {
    let body = null;
    try { body = opts && opts.body ? JSON.parse(opts.body) : null; } catch (e) { /* ignore */ }
    fetchCalls.push({ path, op: body && body.op, body });
    return {
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: true }),
    };
  };
  class SourceBuffer {
    constructor() {
      this.listeners = {};
      this.buffered = { length: 1, start: () => 0, end: () => 10 };
    }
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    appendBuffer(buf) { this.appended = (this.appended || 0) + buf.length; }
  }
  class MediaSource {
    constructor() { this.listeners = {}; this.duration = 10; this.readyState = "ended"; }
    addSourceBuffer(mime) {
      const sb = new SourceBuffer();
      this.lastSb = sb;
      this.lastMime = mime;
      return sb;
    }
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
  }
  env.win.MediaSource = MediaSource;
  env.win.SourceBuffer = SourceBuffer;
  let blobIndex = 0;
  env.win.URL = { createObjectURL: () => "blob:video-" + (++blobIndex) };
  env.video = { currentSrc: "", duration: 10 };
  env.feed = { id: "mse-full-fixture", objectDesc: { mediaType: 4,
    description: "完整测试视频", media: [{ url: "https://finder.video.qq.com/full-fixture", videoPlayLen: 10 }] } };
  env.win.document.querySelector = (sel) => sel === "video" ? env.video : null;
  env.win.document.querySelectorAll = (sel) => sel === "video" ? [env.video] : sel === "#app, [data-v-app]" && env.feed ?
    [{ _vnode: { component: { props: { feed: env.feed }, subTree: { el: env.video } } } }] : [];
  env.docListeners = docListeners;
  env.fetchCalls = fetchCalls;
  return env;
}

function playVideo(env, { fill = 3, bytes = 300 * 1024 } = {}) {
  // 模拟播放器：new MediaSource → addSourceBuffer → appendBuffer(init+media)。
  const ms = new env.win.MediaSource();
  env.video.currentSrc = env.win.URL.createObjectURL(ms);
  const sb = ms.addSourceBuffer('video/mp4; codecs="avc1.42E01E"');
  sb.appendBuffer(new Uint8Array(64 * 1024).fill(7)); // init segment（指纹用）
  sb.appendBuffer(new Uint8Array(bytes).fill(fill)); // media 分片
  for (const fn of env.docListeners.loadeddata || []) fn({ target: env.video });
  return { ms, sb };
}

function mseUploadCalls(env) {
  return env.fetchCalls.filter((c) => c.path === "/__cuin/mse");
}

test("MSE 当前回放没有匹配链时不能回退下载旧短片", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win); loadBootstrap(env);
  const { ms, sb } = playVideo(env);
  ms.readyState = "open";
  for (const fn of sb.listeners.updateend || []) fn();
  ms.readyState = "ended";
  env.video.currentSrc = "blob:current-long-replay-without-capture";
  assert.equal((await env.win.__CUIN__.saveCurrentMse()).saved, false);
  assert.equal(mseUploadCalls(env).length, 0);
});

test("MSE 自动保存：播放结束（ended）自动上传 begin/chunk/end", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win);
  loadBootstrap(env);
  assert.ok(env.win.__CUIN__, "bootstrap 未启动");

  playVideo(env);
  // 触发 document capture 的 ended 监听（视频自然播完）。
  for (const fn of env.docListeners["ended"] || []) fn();
  await new Promise((r) => setTimeout(r, 80));

  const ops = mseUploadCalls(env).map((c) => c.op);
  assert.ok(ops.includes("begin"), `应发起 begin，实际: ${JSON.stringify(ops)}`);
  assert.ok(ops.includes("chunk"), "应上传分片");
  assert.ok(ops.includes("end"), "应结束上传");
  const beginIdx = ops.indexOf("begin");
  const endIdx = ops.indexOf("end");
  assert.ok(beginIdx < ops.lastIndexOf("chunk") && ops.lastIndexOf("chunk") < endIdx,
    "顺序应为 begin → chunk → end");

  // 同一链重复触发（切走时 sourceended 再来一次）：不得重复上传。
  const count = mseUploadCalls(env).length;
  for (const fn of env.docListeners["ended"] || []) fn();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(mseUploadCalls(env).length, count, "重复触发不得重复上传");
});

test("53分钟回放只播两秒时，按钮用播放器原始源参数提交完整下载", async (t) => {
  const env = makeMseEnv("/web/pages/feed");
  t.after(env.cleanup);
  const id = "14990527163746486645";
  const node = { id, objectNonceId: "replay-fixture", contact: { nickname: "TRAE.ai" },
    objectDesc: { description: "搞懂token和上下文让AI省钱", mediaType: 4,
      media: [{ url: "https://finder.video.qq.com/fixture?encfilekey=fixture", decodeKey: "", videoPlayLen: 3180 }] } };
  const app = { _vnode: { component: { props: { feed: node }, subTree: { el: env.video } } } };
  const hiddenPreview = { currentSrc: "blob:hidden", videoWidth: 720, paused: false,
    getBoundingClientRect: () => ({ width: 0, height: 0 }) };
  env.video.paused = true;
  env.video.getBoundingClientRect = () => ({ width: 1280, height: 720 });
  env.win.document.querySelectorAll = (sel) => sel === "video" ? [hiddenPreview, env.video] : sel === "#app, [data-v-app]" ? [app] : [];
  const blobFactory = env.win.URL.createObjectURL;
  env.win.URL = class extends URL {};
  env.win.URL.createObjectURL = blobFactory;
  env.win.Worker = class { postMessage() { return "original"; } };
  const calls = [];
  env.win.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url, body });
    const response = url === "/__cuin/feed" ? { ok: true, feeds: [{ object_id: id, feed_id: "replay-fixture", kind: "video", has_url: true, has_decode_key: true }] } :
      url === "/__cuin/task" ? { ok: true, task_id: "task-fixture" } : { ok: true, task: { status: "done" } };
    return { headers: { get: () => "application/json" }, json: async () => response };
  };
  loadCore(env.win); loadBootstrap(env);
  const { ms, sb } = playVideo(env);
  ms.duration = 3180; ms.readyState = "open";
  env.video.duration = 3180; env.video.currentTime = 2;
  sb.buffered = { length: 1, start: () => 0, end: () => 2 };
  for (const callback of env.docListeners.loadeddata || []) callback();
  const originalResult = new env.win.Worker().postMessage({ cmd: "CUT", url: node.objectDesc.media[0].url,
    seed: "18446744073709551615", first: true, timestampOffset: 0, contentLen: 104010497 });
  assert.equal(originalResult, "original");
  await new Promise(r => setTimeout(r, 340));
  for (const callback of env.docListeners.play || []) callback();
  const api = env.win.__CUIN__;
  assert.equal(api.getCurrentFeed().feed_id, "replay-fixture");
  const feedCall = calls.find(c => c.url === "/__cuin/feed");
  assert.equal(feedCall.body.feeds[0].objectDesc.media[0].decodeKey, "18446744073709551615");
  assert.equal(feedCall.body.feeds[0].objectDesc.media[0].fileSize, 104010497);
  const button = api.createButton({ getModel: () => env.win.CUIN_CORE.buildButtonModel(api.getCurrentFeed()),
    onAction: () => api.requestDownload(api.getCurrentFeed(), "download") });
  for (const callback of button.el.listeners.click || []) callback();
  await new Promise(r => setTimeout(r, 40));
  assert.deepEqual(calls.find(c => c.url === "/__cuin/task").body, { feed_id: "replay-fixture", action: "download" });
  assert.equal(calls.filter(c => c.url === "/__cuin/mse").length, 0,
    "整场下载不能等待MSE播放结束或上传两秒缓存");
});

test("当前 Feed 先到而解密参数迟到时，按钮能力必须刷新", async (t) => {
  const env = makeMseEnv("/web/pages/feed");
  t.after(env.cleanup);
  const blobFactory = env.win.URL.createObjectURL;
  env.win.URL = class extends URL {};
  env.win.URL.createObjectURL = blobFactory;
  env.win.Worker = class { postMessage() {} };
  let responses = 0;
  env.win.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    const feeds = url === "/__cuin/feed" ? [{ object_id: env.feed.id, feed_id: "known-replay",
      kind: "video", has_url: true, has_decode_key: !!body.feeds[0].objectDesc.media[0].decodeKey }] : [];
    if (url === "/__cuin/feed") responses++;
    return { headers: { get: () => "application/json" }, json: async () => ({ ok: true, feeds }) };
  };
  loadCore(env.win); loadBootstrap(env);
  playVideo(env);
  await new Promise(r => setTimeout(r, 340));
  assert.equal(env.win.__CUIN__.getCurrentFeed().has_decode_key, false);
  new env.win.Worker().postMessage({ cmd: "CUT", url: env.feed.objectDesc.media[0].url,
    seed: "18446744073709551615", first: true });
  await new Promise(r => setTimeout(r, 340));
  assert.equal(responses, 2);
  assert.equal(env.win.__CUIN__.getCurrentFeed().has_decode_key, true,
    "同一ID的新参数不能被首次空密钥能力视图盖住");
});

test("播放器画质规格与原始视频地址必须完整捕获，迟到档位需刷新", async (t) => {
  const env = makeMseEnv("/web/pages/feed");
  t.after(env.cleanup);
  const blobFactory = env.win.URL.createObjectURL;
  env.win.URL = class extends URL {};
  env.win.URL.createObjectURL = blobFactory;
  const calls = [];
  env.win.fetch = async (url, opts) => {
    if (url === "/__cuin/feed") calls.push(JSON.parse(opts.body));
    return { headers: { get: () => "application/json" }, json: async () => ({ ok: true, feeds: [] }) };
  };
  loadCore(env.win); loadBootstrap(env);
  playVideo(env);
  await new Promise(r => setTimeout(r, 340));
  const media = env.feed.objectDesc.media[0];
  media.fullUrl = "https://finder.video.qq.com/original?sign=fixture%2F%2B&token=fixture";
  media.width = 1280; media.height = 720;
  media.spec = [{ fileFormat: "hd", width: 1920, height: 1080, bitRate: 2400000, fileSize: 50000000 },
    { fileFormat: "sd", width: 1280, height: 720, bitRate: 600000, fileSize: 12000000 }];
  for (const fn of env.docListeners.play || []) fn({ target: env.video });
  await new Promise(r => setTimeout(r, 340));
  assert.equal(calls.length, 2, "画质列表迟到必须重新上传同一Feed");
  const captured = calls[1].feeds[0].objectDesc.media[0];
  assert.equal(captured.fullUrl, media.fullUrl);
  assert.deepEqual(captured.spec, media.spec);
  assert.equal(captured.width, 1280); assert.equal(captured.height, 720);
  captured.spec[0].width = 1;
  assert.equal(media.spec[0].width, 1920, "不能修改微信播放器原始对象");
});

test("video 元素复用时不能把旧短片缓存重命名为当前长回放", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win); loadBootstrap(env);
  playVideo(env);
  env.feed = { id: "new-long-replay", objectDesc: { mediaType: 4,
    media: [{ url: "https://finder.video.qq.com/new-replay", videoPlayLen: 3212 }] } };
  for (const fn of env.docListeners.play || []) fn({ target: env.video });
  assert.equal((await env.win.__CUIN__.saveCurrentMse()).saved, false);
  assert.equal(mseUploadCalls(env).length, 0);
});

test("较早的空密钥响应迟到时不能覆盖最新下载能力", async (t) => {
  const env = makeMseEnv("/web/pages/feed");
  t.after(env.cleanup);
  const blobFactory = env.win.URL.createObjectURL;
  env.win.URL = class extends URL {};
  env.win.URL.createObjectURL = blobFactory;
  env.win.Worker = class { postMessage() {} };
  let releaseOld;
  env.win.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    const hasKey = !!(body && body.feeds && body.feeds[0].objectDesc.media[0].decodeKey);
    return { headers: { get: () => "application/json" }, json: async () => {
      if (url === "/__cuin/feed" && !hasKey) await new Promise(resolve => { releaseOld = resolve; });
      return { ok: true, feeds: url === "/__cuin/feed" ? [{ object_id: env.feed.id,
        feed_id: "known-replay", kind: "video", has_url: true, has_decode_key: hasKey }] : [] };
    } };
  };
  loadCore(env.win); loadBootstrap(env);
  playVideo(env);
  await new Promise(r => setTimeout(r, 340));
  assert.equal(typeof releaseOld, "function");
  new env.win.Worker().postMessage({ cmd: "CUT", url: env.feed.objectDesc.media[0].url,
    seed: "18446744073709551615", first: true });
  await new Promise(r => setTimeout(r, 340));
  assert.equal(env.win.__CUIN__.getCurrentFeed().has_decode_key, true);
  releaseOld();
  await new Promise(r => setTimeout(r, 20));
  assert.equal(env.win.__CUIN__.getCurrentFeed().has_decode_key, true);
});

test("最初身份未知的旧缓存不能在切换后绑定到别的视频", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  env.feed = null;
  loadCore(env.win); loadBootstrap(env);
  playVideo(env);
  env.feed = { id: "unrelated-new-feed", objectDesc: { mediaType: 4,
    media: [{ url: "https://finder.video.qq.com/unrelated", videoPlayLen: 10 }] } };
  for (const fn of env.docListeners.play || []) fn({ target: env.video });
  assert.equal((await env.win.__CUIN__.saveCurrentMse()).saved, false);
  assert.equal(mseUploadCalls(env).length, 0);
});

test("MSE 自动保存：同一完整视频重复播放去重", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win);
  loadBootstrap(env);

  playVideo(env); // 第一次播放
  for (const fn of env.docListeners["ended"] || []) fn();
  await new Promise((r) => setTimeout(r, 80));
  const first = mseUploadCalls(env).filter((c) => c.op === "begin").length;
  assert.equal(first, 1);

  // 第二次播放：完整内容相同才去重。
  playVideo(env);
  for (const fn of env.docListeners["ended"] || []) fn();
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(
    mseUploadCalls(env).filter((c) => c.op === "begin").length,
    1,
    "同一视频重复播放不应再次上传"
  );
});

test("MSE 相同编码头的不同视频不能被误判为重复", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win); loadBootstrap(env);
  for (const fill of [3, 9]) {
    playVideo(env, { fill });
    for (const fn of env.docListeners.ended || []) fn();
    await new Promise((r) => setTimeout(r, 50));
  }
  assert.equal(mseUploadCalls(env).filter((c) => c.op === "end").length, 2);
});

test("MSE 自动保存：链被挤出（淘汰）前自动保存", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win);
  loadBootstrap(env);

  // 播 5 个视频（页面内存上限 4 条链）：第 5 条 addSourceBuffer 时最老的
  // 链被淘汰——淘汰前应自动上传。
  for (let i = 0; i < 5; i++) playVideo(env, { fill: i });
  await new Promise((r) => setTimeout(r, 80));
  const begins = mseUploadCalls(env).filter((c) => c.op === "begin");
  assert.equal(begins.length, 1, "被淘汰的链应恰好自动保存一次");
});

test("MSE 自动保存：完整的小视频也能保存", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win);
  loadBootstrap(env);

  playVideo(env, { bytes: 60 * 1024 }); // 总量 124KB < 256KB 阈值
  for (const fn of env.docListeners["ended"] || []) fn();
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(mseUploadCalls(env).filter((c) => c.op === "end").length, 1);
});

test("MSE 不得把切换时的两秒缓存当完整视频自动保存", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win);
  loadBootstrap(env);
  const { ms, sb } = playVideo(env);
  ms.duration = 60;
  sb.buffered = { length: 1, start: () => 0, end: () => 2 };
  for (const fn of ms.listeners.sourceended || []) fn();
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(mseUploadCalls(env).length, 0, "残缺缓存不能登记为下载成功");
});

test("MSE 切走时六秒临时时长不能自证完整，即使 sourceended", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  env.feed = null;
  t.after(env.cleanup);
  loadCore(env.win); loadBootstrap(env);
  const { ms, sb } = playVideo(env);
  ms.duration = 6; ms.readyState = "ended";
  env.video.duration = 6; env.video.currentTime = 2; env.video.ended = false;
  sb.buffered = { length: 1, start: () => 0, end: () => 6 };
  for (const fn of ms.listeners.sourceended || []) fn();
  await new Promise(r => setTimeout(r, 60));
  assert.equal(mseUploadCalls(env).length, 0, "未知原片时长时，六秒缓存不能登记为完整下载");
  assert.equal((await env.win.__CUIN__.saveCurrentMse()).saved, false);
});

test("MSE 必须核对页面真实 Feed 时长，不能保存23秒视频的六秒缓存", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  const feed = { id: "full-video-23", objectDesc: { mediaType: 4,
    description: "完整视频", media: [{ url: "https://finder.video.qq.com/full", videoPlayLen: 23 }] } };
  const app = { _vnode: { component: { props: { feed }, subTree: { el: env.video } } } };
  env.win.document.querySelectorAll = sel => sel === "video" ? [env.video] : sel === "#app, [data-v-app]" ? [app] : [];
  loadCore(env.win); loadBootstrap(env);
  const { ms, sb } = playVideo(env);
  ms.duration = 6; env.video.duration = 6;
  sb.buffered = { length: 1, start: () => 0, end: () => 6 };
  for (const fn of env.docListeners.loadeddata || []) fn();
  for (const fn of ms.listeners.sourceended || []) fn();
  await new Promise(r => setTimeout(r, 60));
  assert.equal(mseUploadCalls(env).length, 0, "原视频23秒，缓存6秒必须失败");
});

test("MSE append 自动增长的临时时长不代表全片已加载", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win); loadBootstrap(env);
  const { ms, sb } = playVideo(env);
  ms.readyState = "open";
  ms.duration = 2;
  env.video.duration = 2;
  sb.buffered.end = () => 2;
  for (const fn of sb.listeners.updateend || []) fn();
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(mseUploadCalls(env).length, 0);
  assert.equal((await env.win.__CUIN__.saveCurrentMse()).saved, false);
});

test("MSE 音视频分轨需一起完整，并合并为一次保存", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win);
  loadBootstrap(env);
  const { ms, sb } = playVideo(env);
  const audio = ms.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
  audio.appendBuffer(new Uint8Array(2000).fill(8));
  audio.buffered = { length: 1, start: () => 0, end: () => 2 };
  for (const fn of sb.listeners.updateend || []) fn();
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(mseUploadCalls(env).length, 0, "音轨未完成时不能提前保存");
  audio.buffered.end = () => 10;
  for (const fn of audio.listeners.updateend || []) fn();
  await new Promise((r) => setTimeout(r, 60));
  const calls = mseUploadCalls(env);
  assert.equal(calls.filter((c) => c.op === "begin").length, 2);
  const ends = calls.filter((c) => c.op === "end");
  assert.equal(ends.length, 1);
  assert.equal(ends[0].body.meta.companions.length, 1);
  assert.equal(ends[0].body.meta.duration, 10);
});

test("MSE 跳播留下时间缺口时不能保存", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  loadCore(env.win); loadBootstrap(env);
  const { ms, sb } = playVideo(env);
  sb.buffered = { length: 2, start: (i) => i === 0 ? 0 : 8, end: (i) => i === 0 ? 2 : 10 };
  for (const fn of ms.listeners.sourceended || []) fn();
  const saved = await env.win.__CUIN__.saveCurrentMse();
  assert.equal(saved.saved, false);
  assert.match(saved.error, /不完整/);
  assert.equal(mseUploadCalls(env).length, 0);
});

test("MSE 大 append 分片拆成有界请求，失败后允许重试", async (t) => {
  const env = makeMseEnv("/web/pages/feed/abc");
  t.after(env.cleanup);
  const original = env.win.fetch;
  let failOnce = true;
  env.win.fetch = async (path, opts) => {
    const response = await original(path, opts);
    if (path === "/__cuin/mse" && JSON.parse(opts.body).op === "chunk" && failOnce) {
      failOnce = false;
      response.json = async () => ({ ok: false, error: "temporary failure" });
    }
    return response;
  };
  loadCore(env.win); loadBootstrap(env);
  const { ms } = playVideo(env, { bytes: 5 * 1024 * 1024 });
  for (const fn of ms.listeners.sourceended || []) fn();
  await new Promise((r) => setTimeout(r, 80));
  assert.ok(mseUploadCalls(env).some((c) => c.op === "abort"));
  for (const fn of ms.listeners.sourceended || []) fn();
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(mseUploadCalls(env).filter((c) => c.op === "end").length, 1);
  for (const call of mseUploadCalls(env).filter((c) => c.op === "chunk")) {
    assert.ok(JSON.stringify(call.body).length < 4 * 1024 * 1024);
  }
});

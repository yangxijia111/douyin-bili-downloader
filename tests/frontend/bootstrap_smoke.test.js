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
  assert.equal(win.__CUIN__.version, "2.0.2");
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

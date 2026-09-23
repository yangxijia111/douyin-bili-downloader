/**
 * bootstrap.js — 视频号页面注入入口（随 HTML <head> 注入，早于页面脚本执行）。
 *
 * 职责划分：
 *   本文件 + cuin_core.js + 页面模块（channels_home/feed/live.js）
 *       → 当前播放内容识别 / Feed 捕获 / 用户交互 / 下载按钮
 *   Python 后端（/__cuin/* 虚拟接口）
 *       → 任务 / 下载 / ISAAC64 解密 / 落盘 / 数据库
 *
 * 前端**不**重新实现下载器与解密：按钮只负责把「当前 feed + 动作」经
 * POST /__cuin/task 交给后端，再轮询 GET /__cuin/task/{id} 展示状态。
 *
 * 铁律：任何自身异常都必须被吞掉——注入脚本出问题绝不能影响微信原页面。
 */
(function () {
  "use strict";

  var core = window.CUIN_CORE;
  if (!core) return; // cuin_core.js 必须先加载（注入顺序保证）
  if (window.__CUIN__ && window.__CUIN__.version) return; // 幂等

  var BRIDGE_FEED = "/__cuin/feed";
  var BRIDGE_TASK = "/__cuin/task";
  var BRIDGE_HEARTBEAT = "/__cuin/heartbeat";
  var HEARTBEAT_INTERVAL = 5000;
  var CAPTURE_DEBOUNCE = 300;
  var MAX_POLLS = 3600; // 任务轮询上限（直播录制可能很长）

  // 页面模块基址：脚本**执行期**的 currentScript 才有效（异步加载时为
  // null），兜底用注入 snippet 约定的虚拟路径。
  var BASE_DIR = "/__cuin/assets/";
  try {
    var bootScript = document.currentScript;
    if (bootScript && bootScript.src && bootScript.src.indexOf("/__cuin/") >= 0) {
      BASE_DIR = bootScript.src.replace(/[^/]*$/, "");
    }
  } catch (e) { /* ignore */ }

  var state = {
    version: core.VERSION,
    page: "",
    // 本地索引：hook 捕获的原始节点轻量项（含直链，仅本地匹配用）。
    index: [],
    indexKeys: {},
    // 桥接返回的 feed 能力视图（objectId → view）。
    views: {},
    viewOrder: [],
    current: null, // 当前识别到的 view
    pendingNodes: [],
    pendingKeys: {},
    captureTimer: null,
    buttons: 0,
    modules: {}, // type → factory
    loadedModules: {},
    loadingModules: {},
    currentModule: null,
    currentModuleType: "",
    runtimeHooked: {},
  };

  function log() {
    try { console.info.apply(console, arguments); } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------
  // 桥接客户端（同源虚拟接口，无 CORS / 无公网暴露）
  // ------------------------------------------------------------------

  function postJson(path, body) {
    try {
      return fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "omit",
      }).then(function (resp) {
        return resp.json();
      }).catch(function () { return null; });
    } catch (e) {
      return Promise.resolve(null);
    }
  }

  function getJson(path) {
    try {
      return fetch(path, { method: "GET", credentials: "omit" })
        .then(function (resp) { return resp.json(); })
        .catch(function () { return null; });
    } catch (e) {
      return Promise.resolve(null);
    }
  }

  // ------------------------------------------------------------------
  // Feed 捕获：hook → 本地索引 + 去重队列 → /__cuin/feed
  // ------------------------------------------------------------------

  function queueNodes(nodes, strategy) {
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var key = core.nodeKey(node);
      if (!key || state.pendingKeys[key]) continue;
      if (!state.indexKeys[key]) {
        state.indexKeys[key] = true;
        state.index.push(core.localIndexItem(node));
        if (state.index.length > 500) state.index.shift();
      }
      state.pendingKeys[key] = true;
      state.pendingNodes.push(node);
    }
    if (!state.pendingNodes.length) return;
    if (state.captureTimer) return;
    state.captureTimer = setTimeout(flushCapture, CAPTURE_DEBOUNCE);
  }

  function flushCapture() {
    state.captureTimer = null;
    var nodes = state.pendingNodes;
    state.pendingNodes = [];
    state.pendingKeys = {};
    if (!nodes.length) return;
    postJson(BRIDGE_FEED, {
      strategy: "page_network_hook",
      page: state.page,
      feeds: nodes,
    }).then(function (data) {
      if (!data || !data.ok || !Array.isArray(data.feeds)) return;
      for (var i = 0; i < data.feeds.length; i++) {
        var view = data.feeds[i];
        if (view && view.object_id && !state.views[view.object_id]) {
          state.views[view.object_id] = view;
          state.viewOrder.push(view.object_id);
        }
      }
      notifyCurrentFeed();
    });
  }

  /** Strategy C：运行时函数结果扫描。 */
  function onRuntimeValue(value, name) {
    try {
      var nodes = core.nodesFromValue(value);
      if (nodes.length) queueNodes(nodes, "page_runtime_hook");
    } catch (e) { /* ignore */ }
  }

  /** Strategy B：fetch/XHR 响应文本扫描。 */
  function onNetworkText(text, url) {
    try {
      var nodes = core.nodesFromText(text);
      if (nodes.length) queueNodes(nodes, "page_network_hook");
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------
  // 当前播放内容识别
  // ------------------------------------------------------------------

  var currentFeedListeners = [];

  function notifyCurrentFeed() {
    for (var i = 0; i < currentFeedListeners.length; i++) {
      try { currentFeedListeners[i](state.current); } catch (e) { /* ignore */ }
    }
  }

  function detectCurrent() {
    var video = null;
    try {
      var videos = document.querySelectorAll("video");
      for (var i = 0; i < videos.length; i++) {
        var v = videos[i];
        if (v.videoWidth > 0 && !v.paused) { video = v; break; }
        if (!video && v.currentSrc) video = v;
      }
    } catch (e) { /* ignore */ }
    if (!video) return;
    var src = "";
    try { src = video.currentSrc || video.src || ""; } catch (e) { /* ignore */ }
    var item = core.matchFeedBySrc(state.index, src);
    var next = null;
    if (item) {
      next = state.views[item.object_id] || {
        feed_id: item.nonce_id || item.object_id,
        object_id: item.object_id,
        kind: item.kind,
        title: "",
        has_url: true,
        has_decode_key: null,
        has_cover: !!item.cover_url,
        qualities: [],
      };
    }
    var changed = (next && next.object_id) !== (state.current && state.current.object_id);
    state.current = next;
    if (changed) notifyCurrentFeed();
  }

  // ------------------------------------------------------------------
  // 任务：按钮 → /__cuin/task → 轮询状态
  // ------------------------------------------------------------------

  function requestDownload(view, action, quality) {
    var body = { feed_id: view.feed_id, action: action || "download" };
    if (quality) body.quality = quality;
    return postJson(BRIDGE_TASK, body).then(function (data) {
      if (!data || !data.ok || !data.task_id) {
        throw new Error((data && data.error) || "后端拒绝任务");
      }
      return pollTask(data.task_id);
    });
  }

  function pollTask(taskId) {
    return new Promise(function (resolve) {
      var polls = 0;
      var timer = setInterval(function () {
        polls += 1;
        if (polls > MAX_POLLS) {
          clearInterval(timer);
          resolve({ status: "failed", error: "轮询超时" });
          return;
        }
        getJson(BRIDGE_TASK + "/" + encodeURIComponent(taskId)).then(function (data) {
          var task = data && data.task;
          if (!task) return;
          if (task.status === "done" || task.status === "failed" || task.status === "stopped") {
            clearInterval(timer);
            resolve(task);
          }
        });
      }, 1000);
    });
  }

  // ------------------------------------------------------------------
  // 按钮 UI（页面模块共用；样式在 channels.css）
  // ------------------------------------------------------------------

  function toast(message, ms) {
    try {
      var existing = document.querySelector(".__cuin-toast");
      if (existing) existing.remove();
      var el = document.createElement("div");
      el.className = "__cuin-toast";
      el.textContent = message;
      document.body.appendChild(el);
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, ms || 2600);
    } catch (e) { /* ignore */ }
  }

  function closeMenu(menu) {
    if (menu && menu.parentNode) menu.parentNode.removeChild(menu);
  }

  /**
   * 创建下载按钮。opts:
   *   { label, getModel, onAction(actionId, quality) → Promise, hint }
   * getModel() 返回 core.buildButtonModel(view) 的结果（动态能力）。
   */
  function createButton(opts) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "__cuin-btn";
    btn.setAttribute("data-cuin-btn", "1");
    btn.textContent = opts.label || "下载";
    var menu = null;
    var busy = false;

    function refresh() {
      if (busy) return;
      var model = opts.getModel ? opts.getModel() : { ready: false, label: opts.label, menu: [] };
      btn.textContent = model.ready ? model.label : (opts.label || "下载");
      btn.classList.toggle("__cuin-waiting", !model.ready);
      btn.__cuinModel = model;
    }

    function setBusy(text) {
      busy = true;
      btn.classList.add("__cuin-busy");
      btn.textContent = text;
    }

    function setIdle(text, ok) {
      busy = false;
      btn.classList.remove("__cuin-busy");
      btn.textContent = text || (opts.label || "下载");
      setTimeout(refresh, ok ? 2500 : 4000);
    }

    function runAction(actionId, quality) {
      if (busy) return;
      closeMenu(menu); menu = null;
      setBusy("准备中…");
      Promise.resolve()
        .then(function () { return opts.onAction(actionId, quality); })
        .then(function (task) {
          if (task && task.status === "done") {
            setIdle("✓ 已完成", true);
          } else if (task && task.status === "stopped") {
            setIdle("已停止", true);
          } else {
            setIdle("✗ 失败", false);
            toast("下载失败：" + ((task && task.error) || "未知错误"), 4000);
          }
        })
        .catch(function (err) {
          setIdle("✗ 失败", false);
          toast(String((err && err.message) || err), 4000);
        });
    }

    btn.addEventListener("click", function () {
      if (busy) return;
      // 每次点击都重新计算模型（用户可能刚切换了视频）。
      var model = opts.getModel ? opts.getModel() : { ready: false, menu: [] };
      if (!model.ready || !model.menu.length) {
        // Feed 尚未解析：点击不能无反应。
        toast(
          "正在获取当前视频信息…若长时间无响应：已注入下载按钮，但尚未识别当前视频。" +
          "请播放视频 1–2 秒或切换一次视频。",
          4000
        );
        return;
      }
      if (model.menu.length === 1) {
        runAction(model.menu[0].id);
        return;
      }
      if (menu) { closeMenu(menu); menu = null; return; }
      menu = document.createElement("div");
      menu.className = "__cuin-menu";
      model.menu.forEach(function (item) {
        var row = document.createElement("div");
        row.className = "__cuin-menu-item";
        row.textContent = item.label;
        row.addEventListener("click", function (ev) {
          ev.stopPropagation();
          runAction(item.id);
        });
        menu.appendChild(row);
      });
      document.body.appendChild(menu);
      var rect = btn.getBoundingClientRect();
      menu.style.top = (rect.bottom + 6) + "px";
      menu.style.left = Math.max(8, rect.left - 60) + "px";
      setTimeout(function () {
        document.addEventListener("click", function onDoc() {
          document.removeEventListener("click", onDoc);
          closeMenu(menu); menu = null;
        });
      }, 0);
    });

    refresh();
    return { el: btn, refresh: refresh };
  }

  // ------------------------------------------------------------------
  // 页面模块注册 / 加载 / SPA 路由切换
  // ------------------------------------------------------------------

  var MODULE_FILES = {
    home: "channels_home.js",
    feed: "channels_feed.js",
    profile: "channels_feed.js",
    live: "channels_live.js",
  };

  function loadScriptOnce(type) {
    if (state.loadedModules[type] || state.loadingModules[type]) return;
    state.loadingModules[type] = true;
    var s = document.createElement("script");
    s.src = BASE_DIR + (MODULE_FILES[type] || "channels_feed.js");
    s.async = false;
    s.onload = function () {
      state.loadedModules[type] = true;
      delete state.loadingModules[type];
      mountModule(type);
    };
    s.onerror = function () {
      delete state.loadingModules[type];
    };
    document.head.appendChild(s);
  }

  function unmountModule() {
    if (state.currentModule && typeof state.currentModule.unmount === "function") {
      try { state.currentModule.unmount(); } catch (e) { /* ignore */ }
    }
    state.currentModule = null;
    state.currentModuleType = "";
    state.buttons = 0;
  }

  function mountModule(type) {
    if (state.currentModuleType === type && state.currentModule) return;
    unmountModule();
    var factory = state.modules[type];
    if (!factory) return;
    try {
      state.currentModule = factory(api);
      state.currentModuleType = type;
    } catch (e) {
      log("[cuin] 页面模块挂载失败:", type, e);
    }
  }

  function switchPage() {
    var type = core.detectPageType(location.pathname);
    if (type === state.currentModuleType && state.currentModule) return;
    if (!type) { unmountModule(); return; }
    loadScriptOnce(type);
    if (state.loadedModules[type]) mountModule(type);
  }

  /**
   * 托管式按钮挂载（页面模块共用）：
   *   primary selector → fallback selectors → 最终悬浮按钮；
   *   MutationObserver 监听页面切换 / DOM 复用，按钮被移走就重新挂载；
   *   幂等：同一容器里已有 data-cuin-btn 不重复创建。
   */
  function mountManagedButton(opts) {
    var handle = null;
    var timer = null;

    function findContainer() {
      var selectors = opts.selectors || [];
      for (var i = 0; i < selectors.length; i++) {
        try {
          var found = document.querySelector(selectors[i]);
          if (found && found.offsetParent !== null) return found;
        } catch (e) { /* ignore */ }
      }
      return null;
    }

    function place() {
      if (handle && handle.el && handle.el.isConnected) {
        handle.refresh();
        api.setButtonCount(countButtons());
        return;
      }
      var container = findContainer();
      if (container) {
        var existing = container.querySelector("[data-cuin-btn]");
        if (existing) {
          handle = { el: existing, refresh: function () { /* 由新实例接管 */ } };
          return;
        }
        handle = createButton(opts);
        container.appendChild(handle.el);
        api.setButtonCount(countButtons());
        return;
      }
      if (opts.floating === false) return;
      // 最终兜底：右下悬浮按钮（不依赖微信任何结构）。
      var float = document.querySelector(".__cuin-float");
      if (float && float.querySelector("[data-cuin-btn]")) {
        handle = { el: float.querySelector("[data-cuin-btn]"), refresh: function () { /* noop */ } };
        return;
      }
      if (!float) {
        float = document.createElement("div");
        float.className = "__cuin-float";
        document.body.appendChild(float);
      }
      handle = createButton(opts);
      float.appendChild(handle.el);
      api.setButtonCount(countButtons());
    }

    function countButtons() {
      try { return document.querySelectorAll("[data-cuin-btn]").length; }
      catch (e) { return 0; }
    }

    function schedulePlace() {
      if (timer) return;
      timer = setTimeout(function () { timer = null; place(); }, 400);
    }

    var observer = null;
    try {
      observer = new MutationObserver(schedulePlace);
      observer.observe(document.body, { childList: true, subtree: true });
    } catch (e) { /* ignore */ }
    place();

    return {
      unmount: function () {
        if (observer) { try { observer.disconnect(); } catch (e) { /* ignore */ } }
        if (timer) { clearTimeout(timer); timer = null; }
        if (handle && handle.el && handle.el.parentNode) {
          handle.el.parentNode.removeChild(handle.el);
        }
        api.setButtonCount(countButtons());
      },
      refresh: function () { place(); },
    };
  }

  // ------------------------------------------------------------------
  // 对外 API（页面模块使用）
  // ------------------------------------------------------------------

  var api = {
    version: state.version,
    state: state,
    register: function (type, factory) {
      state.modules[type] = factory;
      if (type === state.currentModuleType && !state.currentModule) mountModule(type);
    },
    toast: toast,
    createButton: createButton,
    getCurrentFeed: function () { return state.current; },
    waitForCurrentFeed: function (timeoutMs) {
      return new Promise(function (resolve) {
        if (state.current) { resolve(state.current); return; }
        var done = false;
        var listener = function (feed) {
          if (done) return;
          if (feed) { done = true; resolve(feed); }
        };
        currentFeedListeners.push(listener);
        setTimeout(function () {
          if (done) return;
          done = true;
          var idx = currentFeedListeners.indexOf(listener);
          if (idx >= 0) currentFeedListeners.splice(idx, 1);
          resolve(state.current);
        }, timeoutMs || 4000);
      });
    },
    onCurrentFeed: function (listener) {
      currentFeedListeners.push(listener);
    },
    requestDownload: requestDownload,
    mountManagedButton: mountManagedButton,
    /** 页面模块挂载/卸载按钮时调用，维护心跳里的按钮计数。 */
    setButtonCount: function (n) {
      state.buttons = Math.max(0, n | 0);
    },
  };

  window.__CUIN__ = api;

  // ------------------------------------------------------------------
  // 启动：hooks → 心跳 → 路由
  // ------------------------------------------------------------------

  try {
    // Strategy B：fetch / XHR 非侵入 hook（保持原语义）。
    try {
      if (typeof window.fetch === "function") {
        window.fetch = core.installFetchHook(window.fetch, onNetworkText);
      }
    } catch (e) { /* ignore */ }
    try {
      if (typeof window.XMLHttpRequest === "function") {
        core.installXhrHook(window.XMLHttpRequest, onNetworkText);
      }
    } catch (e) { /* ignore */ }

    // Strategy C：finder 运行时函数（页面脚本可能后定义，轮询补装）。
    function hookRuntimeOnce() {
      try {
        var installed = core.installRuntimeHooks(window, onRuntimeValue);
        for (var i = 0; i < installed.length; i++) {
          state.runtimeHooked[installed[i]] = true;
        }
      } catch (e) { /* ignore */ }
    }
    hookRuntimeOnce();
    var runtimePoll = setInterval(function () {
      hookRuntimeOnce();
      if (Object.keys(state.runtimeHooked).length >= core.RUNTIME_HOOK_NAMES.length) {
        clearInterval(runtimePoll);
      }
    }, 800);

    // 当前播放识别：video 事件 + 轮询双保险。
    document.addEventListener("play", detectCurrent, true);
    document.addEventListener("loadeddata", detectCurrent, true);
    setInterval(detectCurrent, 1000);

    // 心跳：让后端知道脚本活着、页面类型、按钮数量。
    setInterval(function () {
      postJson(BRIDGE_HEARTBEAT, {
        page: state.page,
        url: location.pathname,
        buttons: state.buttons,
      });
    }, HEARTBEAT_INTERVAL);

    // SPA 路由：history 包装 + popstate + 轮询兜底。
    try {
      var pushState = history.pushState;
      if (typeof pushState === "function") {
        history.pushState = function () {
          var result = pushState.apply(this, arguments);
          setTimeout(switchPage, 0);
          return result;
        };
      }
      var replaceState = history.replaceState;
      if (typeof replaceState === "function") {
        history.replaceState = function () {
          var result = replaceState.apply(this, arguments);
          setTimeout(switchPage, 0);
          return result;
        };
      }
    } catch (e) { /* ignore */ }
    window.addEventListener("popstate", function () { setTimeout(switchPage, 0); });
    setInterval(switchPage, 600);

    function start() {
      state.page = core.detectPageType(location.pathname);
      switchPage();
      log("[cuin] bootstrap v" + core.VERSION + " 已注入（页面: " + (state.page || "未知") + "）");
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  } catch (e) {
    try { console.error("[cuin] bootstrap 启动失败（不影响微信页面）:", e); } catch (e2) { /* ignore */ }
  }
})();

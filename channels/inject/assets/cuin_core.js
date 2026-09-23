/**
 * cuin_core.js — 视频号页面注入的纯逻辑核心（无 DOM 依赖，可在 Node 下单测）。
 *
 * 只放「不碰浏览器也能验证」的逻辑：
 *   - objectDesc 节点递归收集（与 Python channels.feed.extract_feeds 同思路）
 *   - fetch / XHR 的非侵入 hook 工厂（保持原调用语义，异常静默回退）
 *   - finder 运行时函数的包装工厂
 *   - 当前播放视频识别（video src ↔ feed 直链匹配）
 *   - 按钮模型（按 feed 能力动态生成菜单）
 *
 * 浏览器胶水（心跳 / DOM / 按钮挂载）在 bootstrap.js 与各页面模块里。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.CUIN_CORE = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var VERSION = "2.0.2";

  // 递归收集含 objectDesc 的 dict 节点（含嵌套推荐）。depth 上限防止异常
  // 结构（自引用在 JSON.parse 结果里不可能出现，但防御性保留）。
  function collectObjectDescNodes(node, out, depth) {
    if (depth > 32 || node === null || typeof node !== "object") return;
    if (!Array.isArray(node)) {
      if (node.objectDesc && typeof node.objectDesc === "object") {
        out.push(node);
      }
      for (var key in node) {
        if (Object.prototype.hasOwnProperty.call(node, key)) {
          collectObjectDescNodes(node[key], out, depth + 1);
        }
      }
    } else {
      for (var i = 0; i < node.length; i++) {
        collectObjectDescNodes(node[i], out, depth + 1);
      }
    }
  }

  /** 从任意 JSON 文本提取 feed 节点数组；解析失败返回 []（静默）。 */
  function nodesFromText(text) {
    if (typeof text !== "string" || text.length < 16) return [];
    if (text.indexOf("objectDesc") < 0) return [];
    var data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      return [];
    }
    var out = [];
    collectObjectDescNodes(data, out, 0);
    return out;
  }

  /** 从运行时函数返回值（可能是 Promise）提取节点。 */
  function nodesFromValue(value) {
    if (!value || typeof value !== "object") return [];
    var out = [];
    collectObjectDescNodes(value, out, 0);
    return out;
  }

  /** 节点的稳定标识（objectId 优先，nonce 兜底）——与 Python dedup_key 对齐。 */
  function nodeKey(node) {
    if (!node || typeof node !== "object") return "";
    return String(node.objectId || node.objectNonceId || "");
  }

  /** 从原始节点提取「仅供本地匹配用」的轻量索引项（不上传、不外泄）。 */
  function localIndexItem(node) {
    var desc = (node && node.objectDesc) || {};
    var media = Array.isArray(desc.media) ? desc.media : [];
    var first = media[0] || {};
    var url = String(first.url || "") + String(first.urlToken || "");
    return {
      object_id: String(node.objectId || ""),
      nonce_id: String(node.objectNonceId || ""),
      kind: desc.mediaType === 2 ? "image" : desc.mediaType === 9 || node.liveInfo ? "live" : "video",
      url: url,
      cover_url: String(first.coverUrl || ""),
    };
  }

  /** URL 去参数路径（用于 video src 与 feed 直链的粗匹配）。 */
  function urlPath(url) {
    if (typeof url !== "string") return "";
    var cut = url.split("?")[0].split("#")[0];
    return cut;
  }

  /** 取 URL 查询参数（encfilekey / token 等）。 */
  function urlParam(url, name) {
    if (typeof url !== "string") return "";
    var q = url.split("?")[1];
    if (!q) return "";
    var parts = q.split("&");
    for (var i = 0; i < parts.length; i++) {
      var kv = parts[i].split("=");
      if (decodeURIComponent(kv[0]) === name) return decodeURIComponent(kv[1] || "");
    }
    return "";
  }

  /**
   * 用 video 元素 src 在本地索引里找当前 feed（最新优先）。
   * 路径相同优先；路径不同但 encfilekey 相同也认（CDN 可能换域名）。
   */
  function matchFeedBySrc(index, src) {
    if (!src || !Array.isArray(index)) return null;
    var srcPath = urlPath(src);
    var srcKey = urlParam(src, "encfilekey");
    var keyMatch = null;
    for (var i = index.length - 1; i >= 0; i--) {
      var item = index[i];
      if (!item || !item.url) continue;
      if (srcPath && urlPath(item.url) === srcPath) return item;
      if (!keyMatch && srcKey && urlParam(item.url, "encfilekey") === srcKey) {
        keyMatch = item;
      }
    }
    return keyMatch;
  }

  /** 按 kind / 能力生成按钮菜单（页面按钮「根据真实能力动态显示」）。 */
  function buildButtonModel(view) {
    if (!view) {
      return { ready: false, label: "下载", menu: [] };
    }
    var menu = [];
    if (view.kind === "live") {
      menu.push({ id: "record", label: "开始录制" });
    } else if (view.kind === "image") {
      menu.push({ id: "download", label: "下载图片" });
      if (view.has_cover) menu.push({ id: "cover", label: "下载封面" });
    } else {
      menu.push({ id: "download", label: "下载" });
      if (view.qualities && view.qualities.length) {
        menu.push({ id: "download:highest", label: "下载最高画质" });
        menu.push({ id: "download:lowest", label: "下载最低画质" });
      }
      if (view.has_cover) menu.push({ id: "cover", label: "下载封面" });
    }
    return { ready: true, label: view.kind === "live" ? "录制" : "下载", menu: menu };
  }

  /**
   * fetch hook 工厂：包装后的 fetch **返回值与原生完全一致**
   * （读取响应用 clone，原响应对象原样返回）；hook 自身异常全部静默。
   */
  function installFetchHook(originalFetch, onJson) {
    if (typeof originalFetch !== "function") return originalFetch;
    var wrapped = function () {
      var url = "";
      try {
        var input = arguments[0];
        url = String(input && input.url ? input.url : input || "");
      } catch (e) { /* ignore */ }
      var promise = originalFetch.apply(this, arguments);
      try {
        promise = Promise.resolve(promise).then(function (resp) {
          try {
            if (!resp || typeof resp.clone !== "function") return resp;
            var ct = "";
            if (resp.headers && typeof resp.headers.get === "function") {
              ct = resp.headers.get("content-type") || "";
            }
            if (ct.indexOf("json") < 0 && ct.indexOf("text") < 0) return resp;
            resp.clone().text().then(function (text) {
              try { onJson(text, url); } catch (e) { /* hook 异常不影响微信 */ }
            }, function () { /* clone 读取失败忽略 */ });
          } catch (e) { /* hook 异常不影响微信 */ }
          return resp;
        });
      } catch (e) { /* hook 异常不影响微信 */ }
      return promise;
    };
    wrapped.__cuinHooked = true;
    return wrapped;
  }

  /**
   * XHR hook 工厂：**原型级原位包装**——只给 open/send 加记录与 load 监听，
   * 不替换构造函数（instanceof / 静态常量 / 语义完全不变），异常静默。
   */
  function installXhrHook(XHRCtor, onJson) {
    if (typeof XHRCtor !== "function") return XHRCtor;
    var proto = XHRCtor.prototype;
    if (!proto || proto.__cuinHooked) return XHRCtor;
    var originalOpen = proto.open;
    var originalSend = proto.send;
    proto.open = function () {
      try { this.__cuinUrl = String(arguments[1] || ""); } catch (e) { /* ignore */ }
      return originalOpen.apply(this, arguments);
    };
    proto.send = function () {
      var self = this;
      try {
        self.addEventListener("load", function () {
          try {
            var rt = self.responseType;
            if (rt === "" || rt === "text") {
              var text = self.responseText;
              if (text && text.indexOf("objectDesc") >= 0) {
                onJson(text, self.__cuinUrl);
              }
            } else if (rt === "json" && self.response) {
              onJson(self.response, self.__cuinUrl);
            }
          } catch (e) { /* hook 异常不影响微信 */ }
        });
      } catch (e) { /* ignore */ }
      return originalSend.apply(this, arguments);
    };
    try { proto.__cuinHooked = true; } catch (e) { /* ignore */ }
    return XHRCtor;
  }

  // Strategy C：微信页面运行时可能持有的 finder 函数（非侵入包装）。
  var RUNTIME_HOOK_NAMES = [
    "finderPcFlow",
    "finderGetRecommend",
    "finderUserPage",
    "finderGetCommentDetail",
    "finderLiveUserPage",
    "finderLiveReplayList",
    "joinLive"
  ];

  /**
   * 运行时函数包装工厂：保持原函数返回值（含 Promise 原样透传），
   * 仅在结果里扫描 objectDesc；已包装过的跳过；异常静默。
   */
  function installRuntimeHooks(win, onValue) {
    var installed = [];
    if (!win) return installed;
    for (var i = 0; i < RUNTIME_HOOK_NAMES.length; i++) {
      // IIFE 逐迭代捕获 name/original：var 声明在循环外共享，直接闭包会
      // 让包装函数读到循环末尾的值（其余名字不存在时为 undefined）。
      (function (name, original) {
        try {
          if (typeof original !== "function" || original.__cuinHooked) return;
          var wrapped = function () {
            var result = original.apply(this, arguments);
            try {
              if (result && typeof result.then === "function") {
                result.then(
                  function (value) { try { onValue(value, name); } catch (e) { /* ignore */ } },
                  function () { /* 原 Promise 拒绝：不干预 */ }
                );
              } else {
                onValue(result, name);
              }
            } catch (e) { /* hook 异常不影响微信 */ }
            return result;
          };
          wrapped.__cuinHooked = true;
          try { Object.defineProperty(wrapped, "length", { value: original.length }); } catch (e) { /* ignore */ }
          try { Object.defineProperty(wrapped, "name", { value: original.name }); } catch (e) { /* ignore */ }
          win[name] = wrapped;
          installed.push(name);
        } catch (e) { /* 单个函数包装失败不影响其余 */ }
      })(RUNTIME_HOOK_NAMES[i], win[RUNTIME_HOOK_NAMES[i]]);
    }
    return installed;
  }

  /** 页面类型识别（与 Python CHANNELS_PAGE_PATH_RE 覆盖范围一致）。 */
  function detectPageType(pathname) {
    var path = String(pathname || "");
    var match = path.match(/\/web\/pages\/(home|feed|live|profile)/i);
    return match ? match[1].toLowerCase() : "";
  }

  return {
    VERSION: VERSION,
    collectObjectDescNodes: collectObjectDescNodes,
    nodesFromText: nodesFromText,
    nodesFromValue: nodesFromValue,
    nodeKey: nodeKey,
    localIndexItem: localIndexItem,
    urlPath: urlPath,
    urlParam: urlParam,
    matchFeedBySrc: matchFeedBySrc,
    buildButtonModel: buildButtonModel,
    installFetchHook: installFetchHook,
    installXhrHook: installXhrHook,
    installRuntimeHooks: installRuntimeHooks,
    detectPageType: detectPageType,
    RUNTIME_HOOK_NAMES: RUNTIME_HOOK_NAMES
  };
});

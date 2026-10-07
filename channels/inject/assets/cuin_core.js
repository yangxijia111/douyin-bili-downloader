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

  var VERSION = "2.0.4";

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

  /** 视频号内嵌详情/播放列表用 id 表示稳定标识；只对 objectDesc 节点启用。 */
  function objectIdFromNode(node) {
    if (!node || typeof node !== "object") return "";
    var alias = node.objectDesc && typeof node.objectDesc === "object" &&
      !Array.isArray(node.objectDesc) ? node.id : "";
    return String(node.objectId || alias || "");
  }

  /** 节点的稳定标识（objectId/id 优先，nonce 兜底）——与 Python dedup_key 对齐。 */
  function nodeKey(node) {
    if (!node || typeof node !== "object") return "";
    return objectIdFromNode(node) || String(node.objectNonceId || "");
  }

  /** 从原始节点提取「仅供本地匹配用」的轻量索引项（不上传、不外泄）。 */
  function localIndexItem(node) {
    var desc = (node && node.objectDesc) || {};
    var media = Array.isArray(desc.media) ? desc.media : [];
    var first = media[0] || {};
    var url = String(first.url || "") + String(first.urlToken || "");
    return {
      object_id: objectIdFromNode(node),
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
      if (Array.isArray(view.quality_options)) {
        var seen = Object.create(null);
        view.quality_options.forEach(function (option) {
          if (!option || typeof option.value !== "string" || seen[option.value]) return;
          if (!/^(?:highest|lowest|original|format:[A-Za-z0-9_.-]{1,128})$/.test(option.value)) return;
          if (option.value === "original" && !view.has_original) return;
          seen[option.value] = true;
          menu.push({ id: "download:" + option.value, label: "下载" + String(option.label || option.value) });
        });
        if (view.has_original && !seen.original) {
          menu.push({ id: "download:original", label: "下载原始源" });
        }
      } else if (view.qualities && view.qualities.length) {
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
            // preview 页 getFeedInfo 响应不含 objectDesc（sceneInfo 模式），
            // 但跳转逻辑需要它——按 URL 白名单放行给回调。
            var isPreviewApi = String(self.__cuinUrl || "").indexOf("get_feed_info") >= 0;
            if (rt === "" || rt === "text") {
              var text = self.responseText;
              if (text && (text.indexOf("objectDesc") >= 0 || isPreviewApi)) {
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

  var NATIVE_BODY_LIMIT = 8 * 1024 * 1024;

  function nativeBodyWithinLimit(text) {
    if (typeof text !== "string" || text.length > NATIVE_BODY_LIMIT) return false;
    var bytes = 0;
    for (var i = 0; i < text.length; i++) {
      var code = text.charCodeAt(i);
      if (code < 128) bytes += 1;
      else if (code < 2048) bytes += 2;
      else if (code >= 0xd800 && code <= 0xdbff && i + 1 < text.length &&
        text.charCodeAt(i + 1) >= 0xdc00 && text.charCodeAt(i + 1) <= 0xdfff) {
        bytes += 4;
        i += 1;
      } else bytes += 3;
      if (bytes > NATIVE_BODY_LIMIT) return false;
    }
    return true;
  }

  /** 原始桥 JSON 的 uint64 不能先转 Number；字符串 token 整段跳过。 */
  function preserveNativeIntegers(text) {
    var parts = [], copied = 0, quoted = false, escaped = false;
    // 单遍词法扫描，坏 JSON 的大量转义引号也不会触发正则回溯。
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      if (quoted) {
        if (escaped) escaped = false;
        else if (ch === "\\") escaped = true;
        else if (ch === '"') quoted = false;
        continue;
      }
      if (ch === '"') { quoted = true; continue; }
      if (ch !== "-" && (ch < "0" || ch > "9")) continue;
      var end = i + 1;
      while (end < text.length && /[0-9.eE+-]/.test(text.charAt(end))) end += 1;
      var token = text.slice(i, end);
      var next = end;
      while (next < text.length && /\s/.test(text.charAt(next))) next += 1;
      // 只保护整数值，保留非法数字/数字键让 JSON.parse 拒绝它们。
      if (!/^-?(?:0|[1-9]\d*)$/.test(token) || text.charAt(next) === ":") {
        i = end - 1;
        continue;
      }
      var digits = token.charAt(0) === "-" ? token.slice(1) : token;
      if (digits.length > 16 || (digits.length === 16 && digits > "9007199254740991")) {
        parts.push(text.slice(copied, i), '"' + token + '"');
        copied = end;
      }
      i = end - 1;
    }
    parts.push(text.slice(copied));
    return parts.join("");
  }

  /** 仅复制已确认的 feed 子树；其余桥响应和传给微信的原对象保持不变。 */
  function normalizeNativeFeed(node, depth) {
    if (depth > 32 || node === null || typeof node !== "object") return node;
    if (Array.isArray(node)) {
      return node.map(function (item) { return normalizeNativeFeed(item, depth + 1); });
    }
    var out = {};
    Object.keys(node).forEach(function (key) {
      var mapped = key.replace(/_([a-z])/g, function (match, letter) { return letter.toUpperCase(); });
      if (mapped !== key && Object.prototype.hasOwnProperty.call(node, mapped)) return;
      // defineProperty 也把 __proto__ 当作数据字段，不改变新对象原型。
      Object.defineProperty(out, mapped, {
        value: normalizeNativeFeed(node[key], depth + 1),
        enumerable: true, writable: true, configurable: true
      });
    });
    return out;
  }

  function nativeNodesFromText(text) {
    if (!nativeBodyWithinLimit(text) || !/objectDesc|object_desc/.test(text)) return [];
    var data;
    try { data = JSON.parse(preserveNativeIntegers(text)); } catch (e) { return []; }
    var out = [];
    (function walk(node, depth) {
      if (depth > 32 || !node || typeof node !== "object") return;
      if (!Array.isArray(node)) {
        var desc = node.objectDesc || node.object_desc;
        if (desc && typeof desc === "object" && !Array.isArray(desc)) {
          out.push(normalizeNativeFeed(node, 0));
        }
      }
      Object.keys(node).forEach(function (key) { walk(node[key], depth + 1); });
    })(data, 0);
    return out;
  }

  function nativeRequestPath(params) {
    if (!params || typeof params.url !== "string") return "";
    var match = params.url.match(/^(?:https?:\/\/[^/?#\s]+)?(\/[A-Za-z0-9_./-]*)(?:[?#]|$)/);
    return match && match[1].length <= 256 ? match[1] : "";
  }

  /**
   * 腾讯页面 FeedTransferService 通过 finderH5ExtTransfer 原生回调接收
   * jsapi_resp.resp_json，然后 jsonToHump。只观察这个已证实的 seam，不发请求。
   * 原 invoke 返回值及原 callback 的 this/参数/返回/异常均原样保留。
   */
  function installNativeBridgeHooks(win, onValue) {
    var installed = [];
    try {
      var bridge = win && win.WeixinJSBridge;
      var original = bridge && bridge.invoke;
      if (typeof original !== "function" || original.__cuinNativeBridgeHooked) return installed;
      var wrapped = function () {
        if (arguments[0] !== "finderH5ExtTransfer" || typeof arguments[2] !== "function") {
          return original.apply(this, arguments);
        }
        var args = Array.prototype.slice.call(arguments);
        var callback = args[2];
        var requestPath = "";
        try { requestPath = nativeRequestPath(args[1]); } catch (e) { /* ignore */ }
        args[2] = function () {
          try {
            var response = arguments[0];
            var text = response && response.jsapi_resp && response.jsapi_resp.resp_json;
            var nodes = nativeNodesFromText(text);
            if (nodes.length) onValue(nodes, "finderH5ExtTransfer", { request_path: requestPath });
          } catch (e) { /* 观察失败不干预微信回调 */ }
          return callback.apply(this, arguments);
        };
        return original.apply(this, args);
      };
      wrapped.__cuinNativeBridgeHooked = true;
      try { Object.defineProperty(wrapped, "length", { value: original.length }); } catch (e) { /* ignore */ }
      try { Object.defineProperty(wrapped, "name", { value: original.name }); } catch (e) { /* ignore */ }
      bridge.invoke = wrapped;
      if (bridge.invoke === wrapped) installed.push("WeixinJSBridge.invoke:finderH5ExtTransfer");
    } catch (e) { /* 桥尚未加载或只读：下一轮安装可重试 */ }
    return installed;
  }

  // Strategy E：MSE 分片旁路。新版微信 MSE 播放下页面网络层看不到 feed
  // JSON（原生桥接下发），但播放器喂给 SourceBuffer.appendBuffer 的必然是
  // 已解密、可直接解码的 fMP4 分片——hook 原型方法即可完整拿到视频数据。

  /** 拷贝 appendBuffer 入参为独立 Uint8Array（页面可能复用/脱离其 buffer）。 */
  function copyChunkBytes(arg) {
    try {
      if (arg === null || arg === undefined) return null;
      if (typeof ArrayBuffer !== "undefined" && arg instanceof ArrayBuffer) {
        return new Uint8Array(arg.slice(0));
      }
      if (typeof ArrayBuffer !== "undefined" && ArrayBuffer.isView(arg)) {
        var view = new Uint8Array(arg.buffer, arg.byteOffset, arg.byteLength);
        var out = new Uint8Array(view.length);
        out.set(view);
        return out;
      }
      // 跨 realm ArrayBuffer 兜底（有 byteLength 但不是视图）。
      if (typeof arg.byteLength === "number" && typeof arg.slice === "function") {
        return new Uint8Array(arg.slice(0));
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  /** Uint8Array → base64（0x8000 分片避免 String.fromCharCode 栈溢出）。 */
  function bytesToBase64(bytes) {
    var text = "";
    var step = 0x8000;
    for (var i = 0; i < bytes.length; i += step) {
      text += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }
    if (typeof btoa !== "function") {
      throw new Error("btoa unavailable");
    }
    return btoa(text);
  }

  /**
   * 把分片数组打包成上传批次（纯逻辑，Node 可测）。
   * 每批至少含一个分片；尽量凑到 limit 二进制字节以内。
   * 返回 [{ b64, bytes }]。
   */
  function packMseBatches(chunks, limit) {
    if (!Number.isFinite(limit) || limit < 1) throw new Error("invalid batch limit");
    limit = Math.floor(limit);
    var batches = [];
    var pending = [];
    var pendingBytes = 0;
    function flush() {
      if (!pending.length) return;
      var total = pendingBytes;
      var merged = new Uint8Array(total);
      var offset = 0;
      for (var i = 0; i < pending.length; i++) {
        merged.set(pending[i], offset);
        offset += pending[i].length;
      }
      batches.push({ b64: bytesToBase64(merged), bytes: total });
      pending = [];
      pendingBytes = 0;
    }
    for (var i = 0; i < chunks.length; i++) {
      var chunk = chunks[i];
      if (!chunk || !chunk.length) continue;
      for (var offset = 0; offset < chunk.length;) {
        var size = Math.min(limit - pendingBytes, chunk.length - offset);
        pending.push(chunk.subarray(offset, offset + size));
        pendingBytes += size;
        offset += size;
        if (pendingBytes >= limit) flush();
      }
    }
    flush();
    return batches;
  }

  /** 随机流 id（字母数字，8–64 位，满足后端 stream_id 校验）。 */
  function randomId() {
    return (
      Date.now().toString(36) + Math.random().toString(36).slice(2, 10)
    ).replace(/[^a-zA-Z0-9]/g, "");
  }

  /** Fingerprint sampled media bytes and length, independent of append boundaries.
   * Shared init segments describe codecs, not video identity.
   */
  function chainFingerprint(chunks) {
    if (!Array.isArray(chunks) || !chunks.length) return "";
    var hash = 5381;
    var total = 0;
    var sample = 0;
    for (var i = 0; i < chunks.length; i++) {
      var chunk = chunks[i];
      if (!chunk) continue;
      while (sample < total + chunk.length) {
        hash = ((hash * 33) ^ chunk[sample - total]) >>> 0;
        sample += 997;
      }
      total += chunk.length;
    }
    return total ? total.toString(36) + "-" + hash.toString(36) : "";
  }

  /**
   * MSE hook 工厂（非侵入，异常静默）：
   *   - MediaSource.prototype.addSourceBuffer → onSourceBuffer(sb, ms, mime)
   *   - SourceBuffer.prototype.appendBuffer   → onAppend(sb, chunkCopy)
   *   - URL.createObjectURL(MediaSource)      → onCreateObjectURL(url, ms)
   * 全部保持原返回值/语义；任一目标缺失时跳过该项；已安装过跳过。
   */
  function installMseHooks(win, handlers) {
    var installed = [];
    if (!win || !handlers) return installed;
    var MediaSourceCtor = win.MediaSource;
    var SourceBufferCtor = win.SourceBuffer;
    // appendBuffer 捕获是整个旁路的目的，两个类必须同时存在才安装。
    if (
      typeof MediaSourceCtor !== "function" ||
      typeof SourceBufferCtor !== "function" ||
      !MediaSourceCtor.prototype ||
      !SourceBufferCtor.prototype
    ) {
      return installed;
    }
    var proto = SourceBufferCtor.prototype;
    if (typeof proto.appendBuffer === "function" && !proto.__cuinHooked) {
      var originalAppend = proto.appendBuffer;
      proto.appendBuffer = function () {
        var chunkCopy = copyChunkBytes(arguments[0]);
        var result = originalAppend.apply(this, arguments);
        if (chunkCopy !== null) {
          try { handlers.onAppend(this, chunkCopy); } catch (e) { /* ignore */ }
        }
        return result;
      };
      try { proto.__cuinHooked = true; } catch (e) { /* ignore */ }
      installed.push("sourceBuffer.appendBuffer");
    }
    var msProto = MediaSourceCtor.prototype;
    if (
      typeof msProto.addSourceBuffer === "function" &&
      !msProto.__cuinHooked
    ) {
      var originalAdd = msProto.addSourceBuffer;
      msProto.addSourceBuffer = function () {
        var mime = typeof arguments[0] === "string" ? arguments[0] : "";
        var sb = originalAdd.apply(this, arguments);
        try { handlers.onSourceBuffer(sb, this, mime); } catch (e) { /* ignore */ }
        return sb;
      };
      try { msProto.__cuinHooked = true; } catch (e) { /* ignore */ }
      installed.push("mediaSource.addSourceBuffer");
    }
    try {
      if (
        win.URL &&
        typeof win.URL.createObjectURL === "function" &&
        !win.URL.createObjectURL.__cuinHooked
      ) {
        var originalCreate = win.URL.createObjectURL;
        var wrappedCreate = function (obj) {
          var url = originalCreate.apply(this, arguments);
          try {
            if (obj instanceof MediaSourceCtor) handlers.onCreateObjectURL(url, obj);
          } catch (e) { /* ignore */ }
          return url;
        };
        try { wrappedCreate.__cuinHooked = true; } catch (e) { /* ignore */ }
        try { win.URL.createObjectURL = wrappedCreate; } catch (e) { /* ignore */ }
        installed.push("url.createObjectURL");
      }
    } catch (e) { /* ignore */ }
    return installed;
  }

  /** 页面类型识别（与 Python CHANNELS_PAGE_PATH_RE 覆盖范围一致）。 */
  function detectPageType(pathname) {
    var path = String(pathname || "");
    // 分享链接预览页：页面本身没有播放器（bundle 实证），唯一职责是拿到
    // feed 标识后跳转 web 版详情页，单独成类型以免误挂 feed 页按钮。
    if (/\/finder-preview\/pages\//i.test(path)) return "preview";
    var match = path.match(/\/web\/pages\/(home|feed|live|profile)/i);
    return match ? match[1].toLowerCase() : "";
  }

  // Strategy F：分享链接 preview 页跳转。PC 微信打开分享链接后，preview 页
  // 在 visibilitychange 时经 WeixinJSBridge "openFinderView" 打开**原生**详情
  // 页（数据/播放全走原生栈，代理与页面 hook 均不可见）。而它 bundle 里的
  // 另一分支证明 openUrlWithExtraWebview 打开 `web/pages/feed?eid=<id>` 是
  // 合法路径——那是网页详情页，有页面内 MSE 播放器，分片旁路在那边生效。
  // 跳转 id 的来源：getFeedInfo 响应 data.sceneInfo.dynamicExportId
  // （兜底 exportId / objectId / id）。

  /**
   * 从 JSON 文本或已解析对象中递归提取 preview 跳转所需的 feed 标识。
   * 返回字符串（dynamicExportId 优先，exportId/objectId/id 兜底）；没有返回 ""。
   */
  function extractPreviewJumpTarget(data) {
    var text = null;
    if (typeof data === "string") text = data;
    else if (data && typeof data === "object") {
      try { text = JSON.stringify(data); } catch (e) { return ""; }
    }
    if (!text || text.length < 8 || text.indexOf("ceneInfo") < 0) return "";
    var parsed;
    try { parsed = JSON.parse(text); } catch (e) { return ""; }
    var found = { id: "" };
    (function walk(node, depth) {
      if (found.id || depth > 24 || !node || typeof node !== "object") return;
      if (!Array.isArray(node)) {
        if (typeof node.sceneInfo === "object" && node.sceneInfo) {
          var si = node.sceneInfo;
          var candidate =
            (typeof si.dynamicExportId === "string" && si.dynamicExportId) ||
            (typeof si.exportId === "string" && si.exportId) ||
            (typeof si.objectId === "string" && si.objectId) ||
            (typeof si.id === "string" && si.id) ||
            "";
          if (candidate && !found.id) { found.id = candidate; return; }
        }
        for (var key in node) {
          if (Object.prototype.hasOwnProperty.call(node, key)) walk(node[key], depth + 1);
        }
      } else {
        for (var i = 0; i < node.length; i++) walk(node[i], depth + 1);
      }
    })(parsed, 0);
    return found.id;
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
    installNativeBridgeHooks: installNativeBridgeHooks,
    installMseHooks: installMseHooks,
    copyChunkBytes: copyChunkBytes,
    bytesToBase64: bytesToBase64,
    packMseBatches: packMseBatches,
    randomId: randomId,
    chainFingerprint: chainFingerprint,
    detectPageType: detectPageType,
    extractPreviewJumpTarget: extractPreviewJumpTarget,
    RUNTIME_HOOK_NAMES: RUNTIME_HOOK_NAMES
  };
});

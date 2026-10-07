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
  var BRIDGE_MSE = "/__cuin/mse";
  var HEARTBEAT_INTERVAL = 5000;
  var CAPTURE_DEBOUNCE = 300;
  var MAX_POLLS = 3600; // 任务轮询上限（直播录制可能很长）

  // MSE 分片链的内存上限：播放时累积，用户点「下载」时上传。
  // 单链 384MB（≈40 分钟 1080p）、总计 512MB、最多 4 条链（新旧更替）。
  var MSE_MAX_CHAIN_BYTES = 384 * 1024 * 1024;
  var MSE_MAX_TOTAL_BYTES = 512 * 1024 * 1024;
  var MSE_MAX_CHAINS = 4;
  // 单次 POST 的二进制目标大小（base64 后 ~2.7MB，低于后端 4MB 上限）。
  var MSE_UPLOAD_BATCH_BYTES = 2 * 1024 * 1024;


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
    pageId: core.randomId(),
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
    pendingStrategies: {},
    nodeVersions: {},
    captureTimer: null,
    buttons: 0,
    modules: {}, // type → factory
    loadedModules: {},
    loadingModules: {},
    currentModule: null,
    currentModuleType: "",
    runtimeHooked: {},
    workerSources: [],
    pageNodeSignatures: {},
    // MSE 分片旁路（Strategy E）：sb/ms ↔ 链 的映射与链本身。
    mse: {
      chains: [], // { id, mime, ms, chunks: [Uint8Array], bytes, truncated }
      sbToChain: null, // WeakMap SourceBuffer → chain
      msToChains: null, // WeakMap MediaSource → chain[]
      blobUrlToMs: {}, // blob: URL → MediaSource（createObjectURL 时登记）
    },
    // MSE 是已识别原视频后的完整缓存保存通道；源文件下载由任务接口负责。
    // 播放结束/链淘汰只触发核验，缺少原视频身份或时长时不允许保存成功。
    mseAutoSaveEnabled: true,
    mseSavedFPs: {}, // 完整内容保存成功后去重，失败允许重试
    mseSaveQueue: [],
    mseSaving: false,
    // Strategy F（preview 页自动跳转）：getFeedInfo 提取到的 feed 标识。
    previewExportId: "",
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
      if (!key) continue;
      state.nodeVersions[key] = (state.nodeVersions[key] || 0) + 1;
      if (state.pendingKeys[key]) {
        // The worker can supply its seed between loadeddata and the debounced
        // bridge request. Send the latest parameters rather than an empty key.
        for (var p = 0; p < state.pendingNodes.length; p++) {
          if (core.nodeKey(state.pendingNodes[p]) === key) { state.pendingNodes[p] = node; break; }
        }
        if (strategy === "page_runtime_hook") state.pendingStrategies[key] = strategy;
        continue;
      }
      if (!state.indexKeys[key]) {
        state.indexKeys[key] = true;
        state.index.push(core.localIndexItem(node));
        if (state.index.length > 500) state.index.shift();
      }
      state.pendingKeys[key] = true;
      state.pendingStrategies[key] = strategy === "page_runtime_hook" ? strategy : "page_network_hook";
      state.pendingNodes.push(node);
    }
    if (!state.pendingNodes.length) return;
    if (state.captureTimer) return;
    state.captureTimer = setTimeout(flushCapture, CAPTURE_DEBOUNCE);
  }

  function flushCapture() {
    state.captureTimer = null;
    var nodes = state.pendingNodes;
    var strategies = state.pendingStrategies;
    state.pendingNodes = [];
    state.pendingKeys = {};
    state.pendingStrategies = {};
    if (!nodes.length) return;
    ["page_network_hook", "page_runtime_hook"].forEach(function (strategy) {
      var batch = nodes.filter(function (node) { return strategies[core.nodeKey(node)] === strategy; });
      if (!batch.length) return;
      var versions = {};
      batch.forEach(function (node) { var key = core.nodeKey(node); versions[key] = state.nodeVersions[key]; });
      postJson(BRIDGE_FEED, {
        strategy: strategy,
        page: state.page,
        feeds: batch,
      }).then(function (data) {
        if (!data || !data.ok || !Array.isArray(data.feeds)) return;
        for (var i = 0; i < data.feeds.length; i++) {
          var view = data.feeds[i];
          if (view && view.object_id) {
            var key = Object.prototype.hasOwnProperty.call(versions, view.object_id) ? view.object_id : view.feed_id;
            if (versions[key] !== undefined && versions[key] !== state.nodeVersions[key]) continue;
            if (!state.views[view.object_id]) state.viewOrder.push(view.object_id);
            state.views[view.object_id] = view;
          }
        }
        detectCurrent();
        notifyCurrentFeed();
      });
    });
  }

  /** Strategy C：运行时函数结果扫描。 */
  function onRuntimeValue(value, name) {
    try {
      var nodes = core.nodesFromValue(value);
      if (nodes.length) queueNodes(nodes, "page_runtime_hook");
    } catch (e) { /* ignore */ }
  }

  function onWorkerSource(source, worker) {
    var existing = state.workerSources.filter(function (item) { return item.worker === worker; })[0];
    if (existing) existing.source = source;
    else state.workerSources.push({ worker: worker, source: source });
    if (state.workerSources.length > 32) state.workerSources.shift();
    detectCurrent();
  }

  function pageFeedNode(video) {
    var runtime = window.CUIN_MEDIA_SOURCE;
    if (!runtime || typeof runtime.findCurrentFeedFromVue !== "function") return null;
    try {
      var feed = runtime.findCurrentFeedFromVue(video, document);
      if (!feed) return null;
      var media = feed.media || (feed.objectDesc && feed.objectDesc.media && feed.objectDesc.media[0]);
      if (!media || typeof media.url !== "string" || !media.url) return null;
      var url = media.url + (media.urlToken || "");
      var key = core.urlParam(url, "encfilekey");
      var source = null;
      for (var i = state.workerSources.length - 1; i >= 0; i--) {
        var candidate = state.workerSources[i].source;
        if ((key && key === core.urlParam(candidate.url, "encfilekey")) || candidate.url === url) {
          source = candidate; break;
        }
      }
      var node = {
        id: String(feed.id || feed.objectId || ""),
        objectNonceId: String(feed.objectNonceId || ""),
        createtime: feed.createtime || 0,
        contact: { nickname: String((feed.contact && feed.contact.nickname) || ""),
          username: String((feed.contact && feed.contact.username) || "") },
        objectDesc: { mediaType: 4,
          description: String((feed.objectDesc && feed.objectDesc.description) || ""),
          media: [{ url: source ? source.url : url, decodeKey: source ? source.seed : media.decodeKey,
            videoPlayLen: media.videoPlayLen || 0, fileSize: (source && source.content_length) || media.fileSize || 0,
            coverUrl: media.coverUrl || "" }] },
      };
      if (typeof runtime.snapshotQualityMetadata === "function") {
        var quality = runtime.snapshotQualityMetadata(media, window.URL || URL);
        var capturedMedia = node.objectDesc.media[0];
        Object.keys(quality).forEach(function (name) {
          // Worker contentLen is for the currently signed playback source.
          if (name !== "fileSize" || !capturedMedia.fileSize) capturedMedia[name] = quality[name];
        });
      }
      return node.id || node.objectNonceId ? node : null;
    } catch (e) { return null; }
  }

  /** Strategy B：fetch/XHR 响应文本扫描。 */
  function onNetworkText(text, url) {
    try {
      // Strategy F：preview 页 getFeedInfo 响应 → 提取 feed 标识 → 跳转
      // web 版详情页（text 可能是字符串也可能是 responseType:json 的对象）。
      if (
        state.page === "preview" &&
        typeof url === "string" && url.indexOf("get_feed_info") >= 0
      ) {
        var target = core.extractPreviewJumpTarget(text);
        if (target) {
          state.previewExportId = target;
          previewJumpToWebFeed(target);
        }
      }
    } catch (e) { /* ignore */ }
    try {
      var nodes = core.nodesFromText(text);
      if (nodes.length) queueNodes(nodes, "page_network_hook");
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------
  // Strategy F：分享链接 preview 页 → web 版详情页自动跳转。
  // PC 微信的 preview 页自身没有播放器，且会在页面可见时经原生桥
  // openFinderView 打开原生详情页（数据不经过代理与页面 hook，捕获
  // 结构性不可达）。改为抢先把用户带到 web/pages/feed——该页有页面内
  // MSE 播放器，分片旁路在那里完成自动保存。
  // ------------------------------------------------------------------

  var previewJumped = false;

  function previewJumpToWebFeed(exportId) {
    try {
      if (previewJumped || !exportId) return;
      var bridge = window.WeixinJSBridge;
      if (!bridge || typeof bridge.invoke !== "function") return; // 轮询补跳
      previewJumped = true;
      var feedUrl =
        "https://channels.weixin.qq.com/web/pages/feed?eid=" +
        encodeURIComponent(String(exportId)) +
        "&context_id=cuin-" + Date.now() + "&entrance_id=1019";
      bridge.invoke(
        "openUrlWithExtraWebview",
        { url: feedUrl, type: "FinderFeed", id: "FinderFeed" },
        function () {
          try { bridge.invoke("closeWindow", {}, function () {}); } catch (e) { /* ignore */ }
        }
      );
      log("[cuin] preview 页已跳转 web 版详情页:", feedUrl);
    } catch (e) { /* ignore */ }
  }

  function setupPreviewAutoJump() {
    try {
      // 拦下微信 bundle 的 visibilitychange → openFinderView（跳原生）：
      // 我们随 <head> 注入先注册，同元素同阶段按注册序执行，
      // stopImmediatePropagation 可让后注册的原生跳转不触发。
      document.addEventListener("visibilitychange", function (ev) {
        try {
          if (document.visibilityState === "visible" && ev.stopImmediatePropagation) {
            ev.stopImmediatePropagation();
          }
        } catch (e) { /* ignore */ }
      });
    } catch (e) { /* ignore */ }
    // WeixinJSBridge 由微信原生注入，出现时机不定；拿到标识后轮询补跳。
    var deadline = Date.now() + 30000;
    var poll = setInterval(function () {
      if (previewJumped || Date.now() > deadline) {
        clearInterval(poll);
        return;
      }
      if (state.previewExportId) previewJumpToWebFeed(state.previewExportId);
    }, 300);
    // 兜底：拿不到标识（接口失败/结构变化）时至少提示用户，不无声卡死。
    setTimeout(function () {
      if (!previewJumped && !state.previewExportId) {
        toast("未能自动获取该分享视频的详情标识，请从微信「发现→视频号」播放目标视频（播放后自动保存）。", 6000);
      }
    }, 12000);
  }

  // ------------------------------------------------------------------
  // Strategy E：MSE 完整缓存保存（绑定原视频身份和真实时长）
  // ------------------------------------------------------------------

  function mseNewChain(sb, ms, mime) {
    var mse = state.mse;
    if (!mse.sbToChain) {
      mse.sbToChain = new WeakMap();
      mse.msToChains = new WeakMap();
    }
    // 淘汰：链数超限或总字节超限时丢最老的链（新视频优先）。
    // 丢弃前尝试自动保存——这是该链数据的最后机会。
    var total = 0;
    for (var i = 0; i < mse.chains.length; i++) total += mse.chains[i].bytes;
    while (
      mse.chains.length >= MSE_MAX_CHAINS ||
      (total >= MSE_MAX_TOTAL_BYTES && mse.chains.length > 0)
    ) {
      var oldest = mse.chains.shift();
      total -= oldest.bytes;
      queueAutoSaveChain(oldest);
    }
    var chain = {
      id: core.randomId(),
      mime: String(mime || ""),
      ms: ms,
      sb: sb,
      ranges: [],
      duration: 0,
      chunks: [],
      bytes: 0,
      truncated: false,
    };
    mse.chains.push(chain);
    mse.sbToChain.set(sb, chain);
    var siblings = ms ? mse.msToChains.get(ms) : null;
    if (!siblings) {
      siblings = [];
      if (ms) mse.msToChains.set(ms, siblings);
    }
    siblings.push(chain);
    if (sb && typeof sb.addEventListener === "function") {
      sb.addEventListener("updateend", function () {
        rememberCoverage(chain);
        queueAutoSaveChain(chain);
      });
    }
    // sourceended alone is not proof of a complete video: validate all tracks
    // against their highest observed duration and continuous buffered ranges.
    try {
      if (
        ms && typeof ms.addEventListener === "function" && !ms.__cuinEndedHooked
      ) {
        ms.__cuinEndedHooked = true;
        ms.addEventListener("sourceended", function () {
          var group = state.mse.msToChains
            ? state.mse.msToChains.get(ms) || []
            : [];
          for (var i = 0; i < group.length; i++) group[i].sourceFinished = true;
          for (var j = 0; j < group.length; j++) queueAutoSaveChain(group[j]);
        });
      }
    } catch (e) { /* ignore */ }
    return chain;
  }

  function mseOnAppend(sb, chunk) {
    var mse = state.mse;
    var chain = mse.sbToChain && mse.sbToChain.get(sb);
    if (!chain) {
      // addSourceBuffer 早于 hook 安装（罕见）：补登记，mime 未知。
      chain = mseNewChain(sb, null, "");
    }
    if (chain.truncated) return;
    if (chain.bytes + chunk.length > MSE_MAX_CHAIN_BYTES) {
      chain.truncated = true; // 超限截断：不再收，保存时按已收分片处理
      return;
    }
    chain.chunks.push(chunk);
    chain.bytes += chunk.length;
  }

  function chainGroup(chain) {
    if (!chain) return [];
    return (chain.ms && state.mse.msToChains.get(chain.ms)) || [chain];
  }

  function bindMseFeed(chain, node) {
    var id = core.nodeKey(node);
    var media = node && node.objectDesc && node.objectDesc.media;
    var expected = Number((Array.isArray(media) && media[0] || {}).videoPlayLen);
    // A recycled video element must not rename an older captured stream.
    chainGroup(chain).forEach(function (item) {
      if (!item.identityObserved) {
        item.identityObserved = true;
        item.identityUnknown = !id;
        item.feedId = id;
      }
      // A late unrelated Vue Feed cannot establish the identity of an old
      // anonymous cache. Known IDs may still receive delayed duration data.
      if (item.identityUnknown || !id || item.feedId !== id) return;
      item.feedNode = node;
      if (Number.isFinite(expected) && expected > 0) {
        item.expectedDuration = Math.max(item.expectedDuration || 0, expected);
      }
    });
  }

  function rememberCoverage(chain) {
    try {
      var duration = chain.ms && chain.ms.duration;
      if (Number.isFinite(duration) && duration > 0) chain.duration = Math.max(chain.duration, duration);
      var videos = document.querySelectorAll("video");
      for (var i = 0; i < videos.length; i++) {
        var video = videos[i];
        if (state.mse.blobUrlToMs[video.currentSrc || video.src] === chain.ms &&
            Number.isFinite(video.duration) && video.duration > 0) {
          chain.duration = Math.max(chain.duration, video.duration);
          bindMseFeed(chain, pageFeedNode(video));
        }
      }
      var buffered = chain.sb && chain.sb.buffered;
      if (buffered) {
        for (var j = 0; j < buffered.length; j++) {
          chain.ranges.push([buffered.start(j), buffered.end(j)]);
        }
      }
      chain.ranges.sort(function (a, b) { return a[0] - b[0]; });
      var merged = [];
      chain.ranges.forEach(function (range) {
        var last = merged[merged.length - 1];
        if (last && range[0] <= last[1] + 0.05) last[1] = Math.max(last[1], range[1]);
        else merged.push(range.slice());
      });
      chain.ranges = merged;
    } catch (e) { /* detached SourceBuffer: retain the observed coverage */ }
  }

  function completeGroup(chain) {
    var report = mseCaptureReport(chain);
    return report.complete ? { chains: chainGroup(chain), duration: report.duration,
      feedId: chain.feedId, feedNode: chain.feedNode } : null;
  }

  function mseCaptureReport(chain) {
    var group = chainGroup(chain);
    var duration = 0;
    var tracks = group.map(function (item) {
      rememberCoverage(item);
      duration = Math.max(duration, item.duration, item.expectedDuration || 0);
      return { mime: item.mime, bytes: item.bytes, duration: item.duration, ranges: item.ranges,
        truncated: !!item.truncated, initialized: !!(item.ms && item.mime),
        source_finished: !!(item.sourceFinished || (item.ms && item.ms.readyState === "ended")) };
    });
    var reason = "complete";
    if (!tracks.length) reason = "no_video";
    else if (group.some(function (t) { return !t.feedId || !t.expectedDuration || t.feedId !== group[0].feedId; })) reason = "missing_feed_duration";
    else if (tracks.some(function (t) { return t.truncated; })) reason = "truncated";
    else if (tracks.some(function (t) { return !t.initialized || !t.bytes; })) reason = "missing_initialization";
    else if (!Number.isFinite(duration) || duration <= 0) reason = "missing_duration";
    else if (!tracks.some(function (t) { return t.mime.indexOf("video") >= 0; })) reason = "no_video";
    else if (tracks.some(function (t) { return !t.ranges.length || t.ranges[0][0] > 0.15; })) reason = "missing_start";
    else if (tracks.some(function (t) { return t.ranges.length !== 1; })) reason = "coverage_gap";
    else if (tracks.some(function (t) { return t.ranges[0][1] < duration - 0.25; })) reason = "missing_end";
    // appendBuffer itself may increase MediaSource.duration from 2s to 4s, ...
    // while the real video is still loading. Coverage matching that temporary
    // duration must never trigger an early save.
    if (reason === "complete" && !tracks.some(function (t) { return t.source_finished; })) reason = "pending_source_end";
    tracks.forEach(function (track) {
      track.complete = !track.truncated && track.initialized && track.bytes > 0 &&
        duration > 0 && track.ranges.length === 1 && track.ranges[0][0] <= 0.15 &&
        track.ranges[0][1] >= duration - 0.25;
    });
    return { complete: reason === "complete", reason_code: reason, duration: duration, tracks: tracks };
  }

  function mseOnCreateObjectURL(blobUrl, ms) {
    try {
      var map = state.mse.blobUrlToMs;
      map[String(blobUrl)] = ms;
      // 有界保护：blob URL 映射最多留 40 条（长期会话防无界增长）。
      var keys = Object.keys(map);
      for (var i = 0; i < keys.length - 40; i++) delete map[keys[i]];
    } catch (e) { /* ignore */ }
  }

  /** 当前 video（blob URL）对应的最佳链：优先含 video 的 mime、字节数大者。 */
  function currentMseChain(eventVideo) {
    var mse = state.mse;
    var usable = function (chain) {
      return chain && chain.bytes > 1024 && chain.chunks.length > 0;
    };
    var candidates = null;
    try {
      var video = eventVideo;
      if (!video) video = selectCurrentVideo();
      var src = video ? (video.currentSrc || video.src || "") : "";
      if (src && src.indexOf("blob:") === 0) {
        var ms = mse.blobUrlToMs[src];
        if (ms && mse.msToChains) {
          var siblings = mse.msToChains.get(ms) || [];
          candidates = siblings.filter(usable);
        }
      }
    } catch (e) { /* ignore */ }
    if (!candidates || !candidates.length) {
      // An unrecognized current player must never select a previous video's
      // cache, even when it happens to be the only remaining chain.
      return null;
    }
    if (!candidates.length) return null;
    var best = null;
    for (var i = 0; i < candidates.length; i++) {
      var chain = candidates[i];
      if (!best) { best = chain; continue; }
      var chainIsVideo = chain.mime.indexOf("video") >= 0;
      var bestIsVideo = best.mime.indexOf("video") >= 0;
      if (chainIsVideo !== bestIsVideo) {
        if (chainIsVideo) best = chain;
      } else if (chain.bytes > best.bytes) {
        best = chain;
      }
    }
    return best;
  }

  function hasMseForCurrentVideo() {
    return currentMseChain() !== null;
  }

  /**
   * 把一条链上传后端拼装保存（begin → chunk* → end 三段式）。
   * onProgress(percent) 汇报上传进度；resolve({saved, feed_id?, path?, error?})。
   */
  async function uploadChain(chain, onProgress) {
    var capture = completeGroup(chain);
    if (!capture) return {
      saved: false,
      error: "尚未取得完整源文件的下载参数。请重新打开这条回放以捕获源地址和解密参数；当前播放缓存不完整，无法保存完整回放",
    };
    var tracks = capture.chains.map(function (item) {
      return { chunks: item.chunks.slice(), bytes: item.bytes, mime: item.mime };
    });
    var ids = [];
    var totalBytes = tracks.reduce(function (sum, track) { return sum + track.bytes; }, 0);
    var sent = 0;
    try {
      // sourceended may precede the debounced metadata bridge request.
      var feedResponse = await postJson(BRIDGE_FEED, {
        strategy: "page_runtime_hook", page: state.page, feeds: [capture.feedNode],
      });
      if (!feedResponse || !feedResponse.ok) throw new Error("未能确认原视频信息，请重新打开目标视频");
      for (var t = 0; t < tracks.length; t++) {
        var streamId = core.randomId();
        ids.push(streamId);
        var begin = await postJson(BRIDGE_MSE, {
          op: "begin", stream_id: streamId, mime: tracks[t].mime, page: state.page,
        });
        if (!begin || !begin.ok) throw new Error((begin && begin.error) || "后端拒绝 MSE 保存");
        // Encode one bounded batch at a time, not the entire video in base64.
        var pending = [];
        var pendingBytes = 0;
        var seq = 0;
        async function flush() {
          if (!pendingBytes) return;
          var batch = core.packMseBatches(pending, MSE_UPLOAD_BATCH_BYTES)[0];
          var result = await postJson(BRIDGE_MSE, {
            op: "chunk", stream_id: streamId, seq: seq++, b64: batch.b64,
          });
          if (!result || !result.ok) throw new Error((result && result.error) || "分片上传失败");
          sent += batch.bytes;
          pending = [];
          pendingBytes = 0;
          if (typeof onProgress === "function") onProgress(Math.min(99, Math.round(sent * 100 / totalBytes)));
        }
        for (var c = 0; c < tracks[t].chunks.length; c++) {
          var chunk = tracks[t].chunks[c];
          for (var offset = 0; offset < chunk.length;) {
            var size = Math.min(MSE_UPLOAD_BATCH_BYTES - pendingBytes, chunk.length - offset);
            pending.push(chunk.subarray(offset, offset + size));
            pendingBytes += size;
            offset += size;
            if (pendingBytes === MSE_UPLOAD_BATCH_BYTES) await flush();
          }
        }
        await flush();
      }
      var end = await postJson(BRIDGE_MSE, {
        op: "end", stream_id: ids[0],
        meta: { title: "", author: "", page: state.page, complete: true,
          feed_id: capture.feedId, duration: capture.duration, companions: ids.slice(1) },
      });
      if (!end || !end.ok) throw new Error((end && end.error) || "后端校验或合并失败");
      return { saved: true, feed_id: end.feed_id, path: end.path, bytes: end.bytes };
    } catch (e) {
      for (var n = 0; n < ids.length; n++) await postJson(BRIDGE_MSE, { op: "abort", stream_id: ids[n] });
      return { saved: false, error: String(e.message || e) };
    }
  }

  /**
   * MSE 自动保存队列：自然结束、sourceended 或淘汰触发完整性核验。
   * 只有已绑定原视频且所有轨完整的链才能入队，串行上传——同一时间只有一条链在
   * 传，避免多路 4MB 批次把本地代理压垮。
   */
  function queueAutoSaveChain(chain) {
    try {
      if (!state.mseAutoSaveEnabled || !chain || chain.autoSaved || chain.saving) return;
      var capture = completeGroup(chain);
      if (!capture) return;
      var group = capture.chains;
      if (group.some(function (item) { return item.saving || item.autoSaved; })) return;
      var fp = group.map(function (item) { return core.chainFingerprint(item.chunks); }).join(":");
      if (state.mseSavedFPs[fp]) return;
      group.forEach(function (item) { item.saving = true; });
      state.mseSaveQueue.push({ chain: chain, group: group, fingerprint: fp });
      pumpMseSaveQueue();
    } catch (e) { /* ignore */ }
  }

  function pumpMseSaveQueue() {
    if (state.mseSaving || !state.mseSaveQueue.length) return;
    state.mseSaving = true;
    var entry = state.mseSaveQueue.shift();
    uploadChain(entry.chain, null).then(function (result) {
      entry.group.forEach(function (item) { item.saving = false; });
      state.mseSaving = false;
      if (result && result.saved) {
        entry.group.forEach(function (item) { item.autoSaved = true; });
        state.mseSavedFPs[entry.fingerprint] = true;
        log("[cuin] MSE 完整视频保存完成:", result.path);
      } else if (result && result.error) {
        log("[cuin] MSE 保存失败:", result.error);
        toast(result.error, 6000);
      }
      pumpMseSaveQueue();
    });
  }

  /** 手动按钮入口：保存当前播放视频的最佳链。 */
  function saveCurrentMse(onProgress) {
    var video = selectCurrentVideo();
    var chain = currentMseChain(video);
    var node = pageFeedNode(video);
    if (!chain || !node || (chain.feedId && chain.feedId !== core.nodeKey(node))) {
      return Promise.resolve({ saved: false, error: "尚未匹配到当前视频的完整缓存，请重新打开目标视频" });
    }
    return uploadChain(chain, onProgress);
  }

  // ------------------------------------------------------------------
  // 当前播放内容识别
  // ------------------------------------------------------------------

  var currentFeedListeners = [];

  function selectCurrentVideo() {
    var videos = document.querySelectorAll("video");
    var best = null;
    var bestScore = -1;
    for (var i = 0; i < videos.length; i++) {
      var video = videos[i];
      var score = video.paused ? 1 : 2;
      if (typeof video.getBoundingClientRect === "function") {
        var rect = video.getBoundingClientRect();
        if (!rect || rect.width <= 0 || rect.height <= 0) continue;
        var width = rect.width;
        var height = rect.height;
        if (window.innerWidth && window.innerHeight) {
          width = Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(0, rect.left));
          height = Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(0, rect.top));
        }
        if (!width || !height) continue;
        if (typeof window.getComputedStyle === "function") {
          var style = window.getComputedStyle(video);
          if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") continue;
        }
        score = width * height * (video.paused ? 1 : 1.05);
      }
      if (score > bestScore) { best = video; bestScore = score; }
    }
    return best;
  }

  function notifyCurrentFeed() {
    for (var i = 0; i < currentFeedListeners.length; i++) {
      try { currentFeedListeners[i](state.current); } catch (e) { /* ignore */ }
    }
  }

  function detectCurrent() {
    var video = null;
    try {
      video = selectCurrentVideo();
    } catch (e) { /* ignore */ }
    if (!video) { state.current = null; return; }
    var pageNode = pageFeedNode(video);
    var boundChain = currentMseChain(video);
    if (boundChain) bindMseFeed(boundChain, pageNode);
    if (pageNode) {
      var nodeId = core.nodeKey(pageNode);
      var firstMedia = pageNode.objectDesc.media[0];
      var signature = JSON.stringify(firstMedia);
      if (state.pageNodeSignatures[nodeId] !== signature) {
        state.pageNodeSignatures[nodeId] = signature;
        var signatureKeys = Object.keys(state.pageNodeSignatures);
        if (signatureKeys.length > 64) delete state.pageNodeSignatures[signatureKeys[0]];
        queueNodes([pageNode], "page_runtime_hook");
      }
    }
    var src = "";
    try { src = video.currentSrc || video.src || ""; } catch (e) { /* ignore */ }
    var item = pageNode ? core.localIndexItem(pageNode) : core.matchFeedBySrc(state.index, src);
    var next = null;
    if (item) {
      next = state.views[item.object_id] || null;
    }
    var changed = next !== state.current;
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
      detectCurrent();
      // 每次点击都重新计算模型（用户可能刚切换了视频）。
      var model = opts.getModel ? opts.getModel() : { ready: false, menu: [] };
      if (!model.ready || !model.menu.length) {
        // Feed 未解析：新版微信 MSE 播放下走分片旁路保存；没有分片才提示。
        if (hasMseForCurrentVideo()) {
          setBusy("准备中…");
          saveCurrentMse(function (percent) {
            btn.textContent = "保存中 " + percent + "%";
          }).then(function (result) {
            if (result && result.saved) {
              setIdle("✓ 已保存", true);
            } else {
              setIdle("✗ 失败", false);
              toast("保存失败：" + ((result && result.error) || "未知错误"), 4000);
            }
          });
          return;
        }
        // Feed 尚未解析且没有分片：点击不能无反应。
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
    // preview 页没有播放器（页面即将被自动跳走），不挂任何页面模块。
    if (type === "preview") { unmountModule(); return; }
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

  /**
   * 运行时探针（真机诊断用，随心跳回传）。
   * 当 Feed 长期捕获不到时，这份数据能直接回答：
   *   - 页面里到底有哪些 finder/feed 相关全局对象（hook 目标是否存在）
   *   - 我们的 hook 装上了没有
   *   - 本地索引里积累了多少节点（页面侧看到了多少数据）
   * 只回传**属性名与计数**，不回传任何用户数据。
   */
  function collectProbe() {
    var probe = {
      hooked: Object.keys(state.runtimeHooked),
      page_id: state.pageId,
      visibility: document.visibilityState || "visible",
      fetch_hooked: !!(window.fetch && window.fetch.__cuinHooked),
      xhr_hooked: !!(window.XMLHttpRequest && window.XMLHttpRequest.prototype.__cuinHooked),
      index_size: state.index.length,
      view_size: state.viewOrder.length,
      window_keys: [],
      video: null,
    };
    // MSE 分片旁路状态（只回传计数，不回传分片内容）。
    try {
      var mseTotal = 0;
      var mseTruncated = 0;
      for (var ci = 0; ci < state.mse.chains.length; ci++) {
        mseTotal += state.mse.chains[ci].bytes;
        if (state.mse.chains[ci].truncated) mseTruncated += 1;
      }
      var currentChain = currentMseChain();
      var report = mseCaptureReport(currentChain);
      probe.mse = {
        streams: state.mse.chains.length,
        total_bytes: mseTotal,
        truncated: mseTruncated,
        current_bytes: currentChain ? currentChain.bytes : 0,
        current_mime: currentChain ? currentChain.mime : "",
        complete: report.complete,
        reason_code: report.reason_code,
        tracks: report.tracks,
      };
    } catch (e) { /* ignore */ }
    try {
      var keys = Object.getOwnPropertyNames(window);
      var re = /finder|feed|channel|live|object|snss?/i;
      for (var i = 0; i < keys.length && probe.window_keys.length < 60; i++) {
        if (re.test(keys[i])) probe.window_keys.push(keys[i]);
      }
    } catch (e) { /* ignore */ }
    try {
      var v = selectCurrentVideo();
      if (v) {
        probe.video = {
          src_kind: String(v.currentSrc || v.src || "").indexOf("blob:") === 0 ? "blob" : v.currentSrc || v.src ? "http" : "none",
          current_time: v.currentTime || 0, duration: Number.isFinite(v.duration) ? v.duration : 0,
          ready_state: v.readyState || 0, network_state: v.networkState || 0,
          paused: !!v.paused, ended: !!v.ended, buffered: [],
        };
        for (var k = 0; v.buffered && k < v.buffered.length && k < 16; k++) {
          probe.video.buffered.push([v.buffered.start(k), v.buffered.end(k)]);
        }
      }
    } catch (e) { /* ignore */ }
    return probe;
  }

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
    /** MSE 分片旁路：当前视频是否有可保存分片 / 触发保存。 */
    hasMseForCurrentVideo: hasMseForCurrentVideo,
    saveCurrentMse: saveCurrentMse,
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

    // Strategy E：MSE 分片旁路（原型级 hook，保持原语义；缺 MediaSource 的
    // 环境里 installMseHooks 直接返回空，不影响其余启动流程）。
    try {
      var mseInstalled = core.installMseHooks(window, {
        onSourceBuffer: mseNewChain,
        onAppend: mseOnAppend,
        onCreateObjectURL: mseOnCreateObjectURL,
      });
      for (var mi = 0; mi < mseInstalled.length; mi++) {
        state.runtimeHooked[mseInstalled[mi]] = true;
      }
    } catch (e) { /* ignore */ }

    // Strategy C：finder 运行时函数（页面脚本可能后定义，轮询补装）。
    function hookRuntimeOnce() {
      try {
        var bridgeInstalled = core.installNativeBridgeHooks ? core.installNativeBridgeHooks(window, onRuntimeValue) : [];
        bridgeInstalled.forEach(function (name) { state.runtimeHooked[name] = true; });
        var mediaRuntime = window.CUIN_MEDIA_SOURCE;
        if (mediaRuntime && mediaRuntime.installWorkerSourceHook(window, onWorkerSource)) {
          state.runtimeHooked["worker.postMessage:CUT"] = true;
        }
        var installed = core.installRuntimeHooks(window, onRuntimeValue);
        for (var i = 0; i < installed.length; i++) {
          state.runtimeHooked[installed[i]] = true;
        }
      } catch (e) { /* ignore */ }
    }
    hookRuntimeOnce();
    var runtimePoll = setInterval(function () {
      hookRuntimeOnce();
      if (core.RUNTIME_HOOK_NAMES.every(function (name) { return state.runtimeHooked[name]; }) &&
          state.runtimeHooked["WeixinJSBridge.invoke:finderH5ExtTransfer"] &&
          state.runtimeHooked["worker.postMessage:CUT"]) {
        clearInterval(runtimePoll);
      }
    }, 800);

    // 当前播放识别：video 事件 + 轮询双保险。
    document.addEventListener("play", detectCurrent, true);
    document.addEventListener("loadeddata", detectCurrent, true);
    // MSE 自动保存触发之一：自然播完（此时分片已全部 append）。
    document.addEventListener("ended", function (event) {
      var chain = currentMseChain(event && event.target);
      chainGroup(chain).forEach(function (item) { item.sourceFinished = true; });
      queueAutoSaveChain(chain);
    }, true);
    setInterval(detectCurrent, 1000);

    // 心跳：让后端知道脚本活着、页面类型、按钮数量；响应顺带下发
    // mse_auto_save 开关（配置 channels.mse_auto_save，运行中可改）。
    setInterval(function () {
      postJson(BRIDGE_HEARTBEAT, {
        page: state.page,
        url: location.pathname,
        buttons: state.buttons,
        probe: collectProbe(),
      }).then(function (data) {
        if (data && typeof data.mse_auto_save === "boolean") {
          state.mseAutoSaveEnabled = data.mse_auto_save;
        }
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
      // Strategy F：分享链接 preview 页 → 自动跳转 web 版详情页（唯一职责）。
      if (state.page === "preview") setupPreviewAutoJump();
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

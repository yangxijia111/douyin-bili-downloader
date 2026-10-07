/** Capture the original media source before the player decrypts/remuxes it.
 * No network requests, logging, or player state changes belong in this module.
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CUIN_MEDIA_SOURCE = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  // Same evidence-based suffix as channels.domains.MEDIA_URL_SUFFIXES.
  var MEDIA_HOST = "finder.video.qq.com";
  var UINT64_MAX = "18446744073709551615";

  function validMediaUrl(value, URLClass) {
    if (typeof value !== "string" || !value || value.length > 4096 ||
        /[\\\s\x00-\x1f\x7f]/.test(value)) return "";
    try {
      var parsed = new URLClass(value);
      if ((parsed.protocol !== "https:" && parsed.protocol !== "http:") ||
          parsed.username || parsed.password || parsed.port) return "";
      var host = parsed.hostname.toLowerCase();
      if (host !== MEDIA_HOST && !host.endsWith("." + MEDIA_HOST)) return "";
      return value;
    } catch (e) { return ""; }
  }

  function uint64Seed(value) {
    // Never stringify an unsafe Number: that has already lost seed bits.
    if (typeof value === "number") {
      if (!Number.isSafeInteger(value) || value <= 0) return "";
      value = String(value);
    } else if (typeof value === "bigint") value = String(value);
    if (typeof value !== "string" || !/^[0-9]{1,20}$/.test(value)) return "";
    value = value.replace(/^0+/, "");
    if (!value || value.length > UINT64_MAX.length ||
        (value.length === UINT64_MAX.length && value > UINT64_MAX)) return "";
    return value;
  }

  function sourceFromMessage(message, URLClass) {
    if (!message || typeof message !== "object" || message.cmd !== "CUT") return null;
    var url = validMediaUrl(message.url, URLClass);
    var seed = uint64Seed(message.seed);
    if (!url || !seed || message.disableDecrypt === true) return null;
    var source = { url: url, seed: seed };
    if (typeof message.first === "boolean") source.first = message.first;
    if (typeof message.timestampOffset === "number" &&
        Number.isFinite(message.timestampOffset) && message.timestampOffset >= 0) {
      source.timestamp_offset = message.timestampOffset;
    }
    if (typeof message.contentLen === "number" &&
        Number.isSafeInteger(message.contentLen) && message.contentLen > 0) {
      source.content_length = message.contentLen;
    }
    return source;
  }

  function installWorkerSourceHook(win, onSource) {
    try {
      var proto = win && win.Worker && win.Worker.prototype;
      if (!proto || typeof proto.postMessage !== "function" ||
          proto.postMessage.__cuinMediaSourceHooked || typeof onSource !== "function") return false;
      var original = proto.postMessage;
      var URLClass = win.URL || (typeof URL !== "undefined" && URL);
      function wrapped() {
        var source = null;
        // Read scalar parameters before transfer can detach media buffers.
        try { source = sourceFromMessage(arguments[0], URLClass); } catch (e) { /* ignore */ }
        var result = original.apply(this, arguments);
        if (source) {
          try { onSource(source, this); } catch (e) { /* preserve player semantics */ }
        }
        return result;
      }
      wrapped.__cuinMediaSourceHooked = true;
      proto.postMessage = wrapped;
      return proto.postMessage === wrapped;
    } catch (e) { return false; }
  }

  function ownData(object, key) {
    if (!object || typeof object !== "object") return null;
    try {
      var descriptor = Object.getOwnPropertyDescriptor(object, key);
      return descriptor && Object.prototype.hasOwnProperty.call(descriptor, "value")
        ? descriptor.value : null;
    } catch (e) { return null; }
  }

  function snapshotQualityMetadata(media, URLClass) {
    var out = {};
    function field(object, camel, snake) {
      var value = ownData(object, camel);
      return value === null && snake ? ownData(object, snake) : value;
    }
    function numeric(object, target) {
      [["width"], ["height"], ["bitRate", "bit_rate"], ["fileSize", "file_size"],
        ["videoBitrate", "video_bitrate"], ["audioBitrate", "audio_bitrate"],
        ["frameRate", "frame_rate"]].forEach(function (names) {
        var raw = field(object, names[0], names[1]);
        var value = typeof raw === "number" || typeof raw === "string" ? Number(raw) : 0;
        if (Number.isFinite(value) && value > 0 && value <= Number.MAX_SAFE_INTEGER) target[names[0]] = value;
      });
    }
    numeric(media, out);
    var full = validMediaUrl(field(media, "fullUrl", "full_url") || ownData(media, "fullURL"), URLClass);
    if (full) out.fullUrl = full;
    var specs = ownData(media, "spec");
    if (Array.isArray(specs)) {
      out.spec = [];
      specs.slice(0, 64).forEach(function (spec) {
        var dynamicRange = field(spec, "dynamicRangeType", "dynamic_range_type");
        if (dynamicRange !== null && Number(dynamicRange) !== 0) return;
        var format = field(spec, "fileFormat", "file_format");
        if ((typeof format !== "string" && typeof format !== "number") ||
            !/^[A-Za-z0-9._-]{1,128}$/.test(String(format))) return;
        var item = { fileFormat: String(format) };
        numeric(spec, item);
        out.spec.push(item);
      });
    }
    return out;
  }

  function componentFeed(component) {
    for (var depth = 0; component && depth < 12; depth++) {
      var feed = ownData(ownData(component, "props"), "feed");
      if (feed && typeof feed === "object") return feed;
      component = ownData(component, "parent");
    }
    return null;
  }

  /** Production Vue omits __vueParentComponent, but keeps container._vnode.
   * Walk only the rendered VNode tree and match this video's DOM ancestry.
   * Return its nearest props.feed; the caller must clone only media fields.
   */
  function findCurrentFeedFromVue(video, doc) {
    if (!video || !doc) return null;
    try {
      var ancestors = [];
      var roots = [];
      var element = video;
      for (var a = 0; element && a < 16; a++, element = element.parentElement) {
        ancestors.push(element);
        var direct = ownData(element, "_vnode");
        if (direct) roots.push(direct);
        var component = ownData(element, "__vueParentComponent");
        // Dev builds can cheaply enter the same rendered tree from the video.
        if (component) {
          roots.push({ component: component });
          for (var p = 0; component && p < 12; p++) {
            component = ownData(component, "parent");
            if (component) roots.push({ component: component });
          }
        }
      }
      if (typeof doc.querySelectorAll === "function") {
        var containers = doc.querySelectorAll("#app, [data-v-app]");
        for (var c = 0; c < containers.length && c < 4; c++) {
          var rootNode = ownData(containers[c], "_vnode");
          if (rootNode) roots.push(rootNode);
        }
      }
      var visited = new WeakSet();
      var stack = roots.map(function (node) { return { node: node, feed: null, depth: 0 }; });
      var preferred = [];
      var count = 0;
      var best = null;
      var bestDistance = ancestors.length;
      while ((preferred.length || stack.length) && count < 1024) {
        var entry = (preferred.length ? preferred : stack).pop();
        var node = entry.node;
        if (!node || typeof node !== "object" || visited.has(node) || entry.depth > 64) continue;
        visited.add(node);
        count++;
        var currentComponent = ownData(node, "component");
        var feed = ownData(ownData(node, "props"), "feed") ||
          ownData(ownData(currentComponent, "props"), "feed") ||
          componentFeed(ownData(node, "ctx")) || entry.feed;
        if (!feed || typeof feed !== "object") feed = null;
        var distance = ancestors.indexOf(ownData(node, "el"));
        if (feed && distance >= 0 && distance < bestDistance) {
          best = feed;
          bestDistance = distance;
          if (distance === 0) return best;
        }
        function push(child) {
          if (!child || typeof child !== "object" || visited.has(child)) return;
          var subtree = ownData(ownData(child, "component"), "subTree");
          var isNearVideo = ancestors.indexOf(ownData(child, "el")) >= 0 ||
            ancestors.indexOf(ownData(subtree, "el")) >= 0;
          // A recommendation grid can precede the modal in render order. Visit
          // the actual video's DOM branch first without raising the node bound.
          if (stack.length + preferred.length >= 2048) {
            if (!isNearVideo || !stack.length) return;
            stack.pop();
          }
          (isNearVideo ? preferred : stack).push({ node: child, feed: feed, depth: entry.depth + 1 });
        }
        push(ownData(currentComponent, "subTree"));
        var suspense = ownData(node, "suspense");
        if (suspense) push(ownData(suspense, "activeBranch"));
        var children = ownData(node, "children");
        if (Array.isArray(children)) {
          // LIFO visits children in visual/source order, within the same bound.
          for (var i = Math.min(children.length, 1024 - count) - 1; i >= 0; i--) push(children[i]);
        }
      }
      return best;
    } catch (e) { return null; }
  }

  return {
    installWorkerSourceHook: installWorkerSourceHook,
    snapshotQualityMetadata: snapshotQualityMetadata,
    findCurrentFeedFromVue: findCurrentFeedFromVue,
  };
});

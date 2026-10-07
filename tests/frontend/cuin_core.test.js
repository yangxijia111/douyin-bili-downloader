/**
 * cuin_core.js 的 Node 单测（node --test tests/frontend/）。
 *
 * 重点验证 hook 的「非侵入」硬性要求：
 *   - fetch 包装后返回值与原生完全一致（原响应对象原样返回）；
 *   - XHR 原型级包装不改变 open/send 语义与实例行为；
 *   - hook 自身异常绝不外溢（不影响微信页面）。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const core = require(path.join(__dirname, "..", "..", "channels", "inject", "assets", "cuin_core.js"));

function feedNode(objectId, nonce) {
  return {
    objectId,
    objectNonceId: nonce || "n_" + objectId,
    contact: { nickname: "作者" },
    objectDesc: {
      mediaType: 4,
      description: "标题" + objectId,
      media: [{ url: "https://cdn/" + objectId + ".mp4", urlToken: "?tk=1", decodeKey: "123" }],
    },
  };
}

// ---------------------------------------------------------------- 收集与识别

test("nodesFromText 提取 objectDesc 节点", () => {
  const text = JSON.stringify({ data: [feedNode("a"), feedNode("b")] });
  const nodes = core.nodesFromText(text);
  assert.equal(nodes.length, 2);
  assert.equal(core.nodeKey(nodes[0]), "a");
});

test("nodesFromText 对无标记 / 坏 JSON 静默返回空", () => {
  assert.deepEqual(core.nodesFromText('{"data":[]}'), []);
  assert.deepEqual(core.nodesFromText("{not json"), []);
  assert.deepEqual(core.nodesFromText(""), []);
  assert.deepEqual(core.nodesFromText(null), []);
});

test("nodesFromText 处理嵌套结构", () => {
  const text = JSON.stringify({ a: { b: [feedNode("x"), { c: feedNode("y") }] } });
  assert.equal(core.nodesFromText(text).length, 2);
});

test("nodesFromValue 直接扫运行时对象", () => {
  const nodes = core.nodesFromValue({ result: [feedNode("a")] });
  assert.equal(nodes.length, 1);
  assert.deepEqual(core.nodesFromValue(null), []);
  assert.deepEqual(core.nodesFromValue(42), []);
});

test("nodeKey 与 localIndexItem", () => {
  const item = core.localIndexItem(feedNode("a"));
  assert.equal(item.object_id, "a");
  assert.equal(item.kind, "video");
  assert.ok(item.url.includes("cdn/a.mp4"));
  assert.equal(core.nodeKey(feedNode("a")), "a");
  assert.equal(core.nodeKey({}), "");
});

test("微信内嵌播放列表的 id 字符串保持 uint64 精度并与 objectId 共用索引", () => {
  const node = feedNode("14990527163746486645");
  node.id = node.objectId;
  delete node.objectId;
  delete node.objectNonceId;
  assert.equal(core.nodeKey(node), "14990527163746486645");
  assert.equal(core.localIndexItem(node).object_id, "14990527163746486645");
  assert.equal(core.nodeKey({ id: "14990527163746486645" }), "");
  assert.equal(core.localIndexItem({ id: "14990527163746486645" }).object_id, "");
  node.objectId = "legacy";
  assert.equal(core.nodeKey(node), "legacy");
});

test("matchFeedBySrc 路径匹配（最新优先）", () => {
  const index = [
    { object_id: "a", url: "https://cdn/a.mp4?x=1" },
    { object_id: "b", url: "https://cdn/b.mp4?x=2" },
  ];
  assert.equal(core.matchFeedBySrc(index, "https://cdn/b.mp4?x=9").object_id, "b");
  assert.equal(core.matchFeedBySrc(index, "https://cdn/a.mp4").object_id, "a");
  assert.equal(core.matchFeedBySrc(index, "https://cdn/c.mp4"), null);
  assert.equal(core.matchFeedBySrc(index, ""), null);
});

test("matchFeedBySrc encfilekey 兜底匹配", () => {
  const index = [{ object_id: "a", url: "https://old.cdn/a.mp4?encfilekey=K1" }];
  const hit = core.matchFeedBySrc(index, "https://new.cdn/a.mp4?encfilekey=K1&t=2");
  assert.equal(hit.object_id, "a");
});

test("buildButtonModel 按能力生成菜单", () => {
  const video = core.buildButtonModel({
    kind: "video", has_url: true, has_cover: true, qualities: ["highest", "lowest"],
  });
  assert.equal(video.ready, true);
  const ids = video.menu.map((m) => m.id);
  assert.ok(ids.includes("download:highest"));
  assert.ok(ids.includes("download:lowest"));
  assert.ok(ids.includes("cover"));

  const image = core.buildButtonModel({ kind: "image", has_url: true, has_cover: false });
  assert.deepEqual(image.menu.map((m) => m.id), ["download"]);

  const live = core.buildButtonModel({ kind: "live", has_url: true });
  assert.deepEqual(live.menu.map((m) => m.id), ["record"]);

  const none = core.buildButtonModel(null);
  assert.equal(none.ready, false);
  assert.deepEqual(none.menu, []);
});

test("buildButtonModel exposes captured format tiers without inventing resolutions", () => {
  const model = core.buildButtonModel({ kind: "video", has_original: true,
    qualities: ["legacy"], quality_options: [
      { value: "highest", label: "最高可用" },
      { value: "original", label: "原始源" },
      { value: "format:1080p60", label: "1080p60 · 1920×1080" },
      { value: "format:720p", label: "720p · 1280×720" },
    ] });
  assert.ok(model.menu.some((item) => item.id === "download:format:1080p60" && /1080p60/.test(item.label)));
  assert.ok(model.menu.some((item) => item.id === "download:original"));
  assert.equal(model.menu.filter((item) => item.id === "download:highest").length, 1);
  assert.ok(!model.menu.some((item) => item.id === "download:lowest"));
  const sourceOnly = core.buildButtonModel({ kind: "video", quality_options: [] });
  assert.ok(!sourceOnly.menu.some((item) => /1080/.test(item.label)));
});

test("detectPageType 覆盖四类页面", () => {
  assert.equal(core.detectPageType("/web/pages/home"), "home");
  assert.equal(core.detectPageType("/web/pages/feed/abc123"), "feed");
  assert.equal(core.detectPageType("/web/pages/live?x=1"), "live");
  assert.equal(core.detectPageType("/web/pages/profile"), "profile");
  assert.equal(core.detectPageType("/web/other"), "");
});

// ---------------------------------------------------------------- fetch hook

test("fetch hook：返回值与原生完全一致", async () => {
  const bodyText = JSON.stringify({ data: [feedNode("a")] });
  const original = async () => ({
    ok: true,
    status: 200,
    headers: { get: (k) => (k === "content-type" ? "application/json" : null) },
    text: async () => bodyText,
    clone() { return this; },
  });
  const seen = [];
  const wrapped = core.installFetchHook(original, (text, url) => seen.push([text, url]));
  const resp = await wrapped("https://channels.weixin.qq.com/api/feed", { method: "POST" });
  assert.equal(resp.status, 200);
  assert.equal(resp.ok, true);
  assert.equal(await resp.text(), bodyText); // 原响应对象原样返回
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(seen.length, 1);
  assert.equal(seen[0][0], bodyText);
  assert.ok(seen[0][1].includes("api/feed"));
});

test("fetch hook：非 JSON 响应不读取", async () => {
  let readCount = 0;
  const original = async () => ({
    headers: { get: () => "image/png" },
    clone() { readCount += 1; return this; },
  });
  const seen = [];
  const wrapped = core.installFetchHook(original, (t) => seen.push(t));
  await wrapped("https://channels.weixin.qq.com/img.png");
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(readCount, 0);
  assert.equal(seen.length, 0);
});

test("fetch hook：hook 异常不影响调用方", async () => {
  const original = async () => ({
    headers: { get: () => "application/json" },
    text: async () => "{}",
    clone() { return this; },
  });
  const wrapped = core.installFetchHook(original, () => {
    throw new Error("hook boom");
  });
  const resp = await wrapped("https://channels.weixin.qq.com/api");
  assert.ok(resp); // 调用方拿到正常响应
  await new Promise((r) => setTimeout(r, 10));
});

test("fetch hook：非函数入参原样返回", () => {
  assert.equal(core.installFetchHook(undefined, () => {}), undefined);
});

// ---------------------------------------------------------------- XHR hook

class FakeXHR {
  constructor() {
    this.responseType = "";
    this.responseText = "";
    this.listeners = {};
    this.opened = null;
    this.sent = null;
  }
  open(method, url) { this.opened = [method, url]; }
  send(body) { this.sent = body; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  emitLoad() { (this.listeners.load || []).forEach((fn) => fn()); }
}

test("XHR hook：open/send 语义不变，load 时捕获 objectDesc 响应", () => {
  const seen = [];
  const Ctor = core.installXhrHook(FakeXHR, (text, url) => seen.push([text, url]));
  assert.equal(Ctor, FakeXHR); // 构造函数不被替换（instanceof 不变）
  const xhr = new Ctor();
  xhr.open("POST", "https://channels.weixin.qq.com/api/finderPcFlow");
  xhr.send("{}");
  assert.deepEqual(xhr.opened, ["POST", "https://channels.weixin.qq.com/api/finderPcFlow"]);
  assert.equal(xhr.sent, "{}");
  xhr.responseText = JSON.stringify({ data: [feedNode("a")] });
  xhr.emitLoad();
  assert.equal(seen.length, 1);
  assert.ok(seen[0][0].includes("objectDesc"));
  assert.ok(seen[0][1].includes("finderPcFlow"));
});

test("XHR hook：无 objectDesc 的响应不捕获", () => {
  const seen = [];
  const Ctor = core.installXhrHook(FakeXHR, (t) => seen.push(t));
  const xhr = new Ctor();
  xhr.open("GET", "https://channels.weixin.qq.com/api/config");
  xhr.send();
  xhr.responseText = '{"config":1}';
  xhr.emitLoad();
  assert.equal(seen.length, 0);
});

test("XHR hook：responseType=json 直接透传对象", () => {
  // 原型级 hook 是单例语义（installXhrHook 幂等，__cuinHooked 会继承），
  // 用完全独立的假类隔离，避免复用已 hook 的原型。
  class JsonXHR {
    constructor() {
      this.responseType = "";
      this.response = null;
      this.listeners = {};
    }
    open() {}
    send() {}
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    emitLoad() { (this.listeners.load || []).forEach((fn) => fn()); }
  }
  const seen = [];
  const Ctor = core.installXhrHook(JsonXHR, (v) => seen.push(v));
  const xhr = new Ctor();
  xhr.responseType = "json";
  xhr.open("GET", "https://channels.weixin.qq.com/api");
  xhr.send();
  xhr.response = { data: [feedNode("a")] };
  xhr.emitLoad();
  assert.equal(seen.length, 1);
  assert.ok(seen[0].data);
});

test("XHR hook：重复安装幂等", () => {
  const once = core.installXhrHook(FakeXHR, () => {});
  const twice = core.installXhrHook(once, () => {});
  assert.equal(twice, FakeXHR);
  assert.equal(FakeXHR.prototype.__cuinHooked, true);
});

// ---------------------------------------------------------------- 运行时 hook

test("runtime hook：包装全局函数且返回值不变", () => {
  const win = {};
  win.finderPcFlow = function finderPcFlow(a, b) { return { code: 0, a, b }; };
  const seen = [];
  const installed = core.installRuntimeHooks(win, (value, name) => seen.push([value, name]));
  assert.deepEqual(installed, ["finderPcFlow"]);
  const result = win.finderPcFlow(1, 2);
  assert.deepEqual(result, { code: 0, a: 1, b: 2 });
  assert.equal(seen.length, 1);
  assert.equal(seen[0][1], "finderPcFlow");
});

test("runtime hook：Promise 结果透传且不改变链", async () => {
  const win = {};
  win.finderGetRecommend = () => Promise.resolve({ data: [feedNode("a")] });
  const seen = [];
  core.installRuntimeHooks(win, (v, n) => seen.push([v, n]));
  const promise = win.finderGetRecommend();
  const value = await promise;
  assert.ok(value.data);
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(seen.length, 1);
});

test("runtime hook：只包装存在的函数，跳过非函数", () => {
  const win = { finderPcFlow: 42, joinLive: function joinLive() { return 1; } };
  const installed = core.installRuntimeHooks(win, () => {});
  assert.deepEqual(installed, ["joinLive"]);
});

test("runtime hook：重复安装幂等", () => {
  const win = {};
  win.finderPcFlow = function () { return 1; };
  core.installRuntimeHooks(win, () => {});
  const installedAgain = core.installRuntimeHooks(win, () => {});
  assert.deepEqual(installedAgain, []);
});

test("runtime hook：hook 异常不影响原函数返回", () => {
  const win = {};
  win.finderPcFlow = function () { return "ok"; };
  core.installRuntimeHooks(win, () => { throw new Error("boom"); });
  assert.equal(win.finderPcFlow(), "ok");
});

test("runtime hook：原 Promise 拒绝不干预", async () => {
  const win = {};
  win.joinLive = () => Promise.reject(new Error("live failed"));
  core.installRuntimeHooks(win, () => {});
  await assert.rejects(() => win.joinLive(), /live failed/);
});

// ---------------------------------------------------------------- 原生桥 hook

test("native bridge hook：只捕获目标响应，保持 invoke 与 callback 的调用语义", () => {
  const response = { jsapi_resp: { resp_json: JSON.stringify({ data: feedNode("bridge") }) } };
  const callbackThis = { native: true };
  const invokeThis = { caller: true };
  const invokeResult = { nativeResult: true };
  const callbackResult = Promise.resolve("callback result");
  const request = { url: "https://channels.weixin.qq.com/cgi-bin/findergetcommentdetail?token=private", req_json: "unchanged" };
  const extra = { extra: true };
  let invokeCount = 0;
  let callbackCount = 0;
  const win = { WeixinJSBridge: { invoke: function (name, params, callback, fourth) {
    invokeCount += 1;
    assert.equal(this, invokeThis);
    assert.equal(name, "finderH5ExtTransfer");
    assert.equal(params, request);
    assert.equal(fourth, extra);
    assert.equal(callback.call(callbackThis, response, extra), callbackResult);
    return invokeResult;
  } } };
  const seen = [];
  assert.deepEqual(core.installNativeBridgeHooks(win, (...args) => seen.push(args)), ["WeixinJSBridge.invoke:finderH5ExtTransfer"]);
  assert.equal(invokeCount, 0); // 安装 hook 不主动请求微信接口。
  const result = win.WeixinJSBridge.invoke.call(invokeThis, "finderH5ExtTransfer", request, function (value, fourth) {
    callbackCount += 1;
    assert.equal(this, callbackThis);
    assert.equal(value, response);
    assert.equal(fourth, extra);
    return callbackResult;
  }, extra);
  assert.equal(result, invokeResult);
  assert.equal(invokeCount, 1);
  assert.equal(callbackCount, 1);
  assert.equal(seen.length, 1);
  assert.equal(core.nodeKey(seen[0][0][0]), "bridge");
  assert.equal(seen[0][1], "finderH5ExtTransfer");
  assert.deepEqual(seen[0][2], { request_path: "/cgi-bin/findergetcommentdetail" });
});

test("native bridge hook：snake_case feed 独立归一，uint64 原始数字与字符串均不丢精度", () => {
  const raw = '{"data":{"object":{"id":14990527163746486645,"object_nonce_id":"nonce",' +
    '"object_desc":{"media_type":4,"description":"14990527163746486645 remains text \\"id\\":18446744073709551615",' +
    '"media":[{"url":"https://example.invalid/video","url_token":"?fixture=1",' +
    '"decode_key":18446744073709551615,"video_play_len":3180,"file_size":9007199254740991}]},' +
    '"contact":{"head_url":"https://example.invalid/avatar","nickname":"作者"}},' +
    '"recommend":[{"id":"14990527163746486646","object_desc":{"media_type":4,' +
    '"media":[{"decode_key":"18446744073709551614","url":"https://example.invalid/next"}]}}]}}';
  const response = { jsapi_resp: { resp_json: raw } };
  const seen = [];
  let callbackCount = 0;
  const win = { WeixinJSBridge: { invoke(name, params, callback) { callback(response); } } };
  core.installNativeBridgeHooks(win, (value) => seen.push(value));
  win.WeixinJSBridge.invoke("finderH5ExtTransfer", { url: "/cgi-bin/detail?private=1#fragment" }, (value) => {
    callbackCount += 1;
    assert.equal(value, response);
    assert.equal(value.jsapi_resp.resp_json, raw);
  });
  assert.equal(callbackCount, 1);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].length, 2);
  const [first, next] = seen[0];
  assert.equal(core.nodeKey(first), "14990527163746486645");
  assert.equal(first.objectNonceId, "nonce");
  assert.equal(first.objectDesc.mediaType, 4);
  assert.equal(first.objectDesc.description, '14990527163746486645 remains text "id":18446744073709551615');
  assert.equal(first.objectDesc.media[0].decodeKey, "18446744073709551615");
  assert.equal(first.objectDesc.media[0].videoPlayLen, 3180);
  assert.equal(first.objectDesc.media[0].fileSize, 9007199254740991);
  assert.equal(first.objectDesc.media[0].urlToken, "?fixture=1");
  assert.equal(first.contact.headUrl, "https://example.invalid/avatar");
  assert.equal(core.nodeKey(next), "14990527163746486646");
  assert.equal(next.objectDesc.media[0].decodeKey, "18446744073709551614");
  assert.equal(first.object_desc, undefined);
});

test("native bridge hook：其他 API 与没有回调的请求参数原样透传", () => {
  const calls = [];
  const seen = [];
  const callback = () => {};
  const params = { url: "/cgi-bin/detail" };
  const win = { WeixinJSBridge: { invoke: function () { calls.push([...arguments]); return "native"; } } };
  core.installNativeBridgeHooks(win, (value) => seen.push(value));
  assert.equal(win.WeixinJSBridge.invoke("finderGetInitData", params, callback, 7), "native");
  assert.deepEqual(calls[0], ["finderGetInitData", params, callback, 7]);
  assert.equal(win.WeixinJSBridge.invoke("finderH5ExtTransfer", params), "native");
  assert.deepEqual(calls[1], ["finderH5ExtTransfer", params]);
  assert.deepEqual(seen, []);
});

test("native bridge hook：坏 JSON、超限文本与非 objectDesc 响应均忽略，原回调照常执行", () => {
  const inputs = [
    { jsapi_resp: { resp_json: '{"object_desc":broken}' } },
    { jsapi_resp: { resp_json: JSON.stringify({ id: "generic", object_desc: "invalid" }) } },
    { jsapi_resp: { resp_json: JSON.stringify({ objectDesc: [] }) } },
    { jsapi_resp: { resp_json: JSON.stringify({ id: "generic", code: 0 }) } },
    { jsapi_resp: { resp_json: '{"id":014990527163746486645,"object_desc":{}}' } },
    { jsapi_resp: { resp_json: '{14990527163746486645:1,"object_desc":{}}' } },
    { jsapi_resp: { resp_json: '{"object_desc":{"description":"' + '\\"'.repeat(20000) } },
    { jsapi_resp: { resp_json: { objectDesc: {} } } },
    { jsapi_resp: { resp_json: " ".repeat(8 * 1024 * 1024) + JSON.stringify(feedNode("too-large")) } },
    { jsapi_resp: { resp_json: JSON.stringify({ ...feedNode("too-many-utf8-bytes"), padding: "中".repeat(3 * 1024 * 1024) }) } },
    { get jsapi_resp() { throw new Error("host getter"); } },
    null,
  ];
  const win = { WeixinJSBridge: { invoke(name, params, callback) { return callback(params.response); } } };
  let callbackCount = 0;
  const seen = [];
  core.installNativeBridgeHooks(win, (value) => seen.push(value));
  for (const response of inputs) {
    assert.equal(win.WeixinJSBridge.invoke("finderH5ExtTransfer", { response }, (value) => {
      callbackCount += 1;
      assert.equal(value, response);
      return "ok";
    }), "ok");
  }
  assert.equal(callbackCount, inputs.length);
  assert.deepEqual(seen, []);
});

test("native bridge hook：observer 异常静默，原 invoke/callback 异常与 Promise 保留", async () => {
  const response = { jsapi_resp: { resp_json: JSON.stringify(feedNode("a")) } };
  const nativeError = new Error("native invoke error");
  const callbackError = new Error("original callback error");
  const nativePromise = Promise.reject(nativeError);
  await nativePromise.catch(() => {});
  const win = { WeixinJSBridge: { invoke(name, params, callback) {
    if (params.mode === "throw") throw nativeError;
    if (params.mode === "promise") return nativePromise;
    return callback(response);
  } } };
  let callbackCount = 0;
  core.installNativeBridgeHooks(win, () => { throw new Error("observer error"); });
  assert.equal(win.WeixinJSBridge.invoke("finderH5ExtTransfer", {}, () => { callbackCount += 1; return "ok"; }), "ok");
  assert.equal(callbackCount, 1);
  assert.throws(() => win.WeixinJSBridge.invoke("finderH5ExtTransfer", {}, () => { throw callbackError; }), (e) => e === callbackError);
  assert.throws(() => win.WeixinJSBridge.invoke("finderH5ExtTransfer", { mode: "throw" }, () => {}), (e) => e === nativeError);
  assert.equal(win.WeixinJSBridge.invoke("finderH5ExtTransfer", { mode: "promise" }, () => {}), nativePromise);
  await assert.rejects(nativePromise, (e) => e === nativeError);
});

test("native bridge hook：迟加载可重试，重复安装和只读 invoke 不伪报成功", () => {
  const win = {};
  assert.deepEqual(core.installNativeBridgeHooks(win, () => {}), []);
  let callbackCount = 0;
  win.WeixinJSBridge = { invoke(name, params, callback) { callback({ jsapi_resp: { resp_json: JSON.stringify(feedNode("a")) } }); } };
  const seen = [];
  const duplicateSeen = [];
  assert.equal(core.installNativeBridgeHooks(win, (value) => seen.push(value)).length, 1);
  const wrapped = win.WeixinJSBridge.invoke;
  assert.deepEqual(core.installNativeBridgeHooks(win, (value) => duplicateSeen.push(value)), []);
  assert.equal(win.WeixinJSBridge.invoke, wrapped);
  win.WeixinJSBridge.invoke("finderH5ExtTransfer", {}, () => { callbackCount += 1; });
  assert.equal(callbackCount, 1);
  assert.equal(seen.length, 1);
  assert.deepEqual(duplicateSeen, []);
  const readonly = { WeixinJSBridge: Object.freeze({ invoke() {} }) };
  assert.deepEqual(core.installNativeBridgeHooks(readonly, () => {}), []);
});

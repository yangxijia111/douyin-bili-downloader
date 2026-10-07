const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const source = require(path.join(__dirname, "..", "..", "channels", "inject", "assets", "media_source.js"));

test("quality metadata captures safe formats and original URL without unrelated fields", () => {
  const media = { full_url: "https://finder.video.qq.com/full?sign=fixture%2F%2B", width: "1280", height: 720,
    spec: [{ file_format: "xWT111", width: 1920, height: 1080, bit_rate: "2400000", file_size: 30000000,
      cookie: "must-not-copy", url: "https://untrusted.example/video" },
    { fileFormat: "bad&token=secret", width: 9999, height: 9999 }] };
  const result = source.snapshotQualityMetadata(media, URL);
  assert.deepEqual(result, { width: 1280, height: 720, fullUrl: media.full_url,
    spec: [{ fileFormat: "xWT111", width: 1920, height: 1080, bitRate: 2400000, fileSize: 30000000 }] });
  result.spec[0].width = 1;
  assert.equal(media.spec[0].width, 1920);
  assert.equal(source.snapshotQualityMetadata({ fullUrl: "https://127.0.0.1/private", spec: null }, URL).fullUrl, undefined);
});

function environment(callback) {
  const calls = [];
  class Worker {
    postMessage(...args) { calls.push({ self: this, args }); return this.result; }
  }
  const win = { Worker, URL };
  const installed = source.installWorkerSourceHook(win, callback);
  return { win, Worker, calls, installed };
}

const valid = (extra = {}) => ({ cmd: "CUT", url: "https://finder.video.qq.com/20302/video.mp4?x=fixture", seed: "18446744073709551615", first: true, timestampOffset: 0, contentLen: 104010497, ...extra });

test("worker source capture preserves original message, transfer list, this and result", () => {
  const captures = [];
  const env = environment((value, worker) => captures.push({ value, worker }));
  assert.equal(env.installed, true);
  const worker = new env.Worker();
  worker.result = { marker: true };
  const body = valid();
  const transfer = [new ArrayBuffer(8)];
  const result = worker.postMessage(body, transfer, "extra");
  assert.equal(result, worker.result);
  assert.equal(env.calls[0].self, worker);
  assert.deepEqual(env.calls[0].args, [body, transfer, "extra"]);
  assert.equal(env.calls[0].args[0], body);
  assert.equal(env.calls[0].args[1], transfer);
  assert.equal(captures[0].worker, worker);
  assert.deepEqual(captures[0].value, { url: body.url, seed: body.seed, first: true, timestamp_offset: 0, content_length: body.contentLen });
  captures[0].value.url = "changed";
  assert.equal(body.url, valid().url);
});

test("a throwing callback never affects the player and an original exception stays identical", () => {
  const env = environment(() => { throw new Error("callback"); });
  assert.doesNotThrow(() => new env.Worker().postMessage(valid()));
  const originalError = new Error("original");
  class Worker { postMessage() { throw originalError; } }
  const seen = [];
  source.installWorkerSourceHook({ Worker, URL }, value => seen.push(value));
  assert.throws(() => new Worker().postMessage(valid()), error => error === originalError);
  assert.equal(seen.length, 0);
});

test("duplicate installation does not wrap or notify twice", () => {
  let count = 0;
  const env = environment(() => count++);
  const first = env.Worker.prototype.postMessage;
  assert.equal(source.installWorkerSourceHook(env.win, () => count++), false);
  assert.equal(env.Worker.prototype.postMessage, first);
  new env.Worker().postMessage(valid());
  assert.equal(count, 1);
  assert.equal(source.installWorkerSourceHook({}, () => {}), false);
});

test("only exact CUT media commands with lossless nonzero uint64 seeds are captured", () => {
  const captured = [];
  const env = environment(value => captured.push(value));
  const worker = new env.Worker();
  const rejected = [
    null, "CUT", new ArrayBuffer(20), {}, valid({ cmd: "cut" }), valid({ cmd: "RESUME" }),
    valid({ seed: "0" }), valid({ seed: "-1" }), valid({ seed: "1.1" }), valid({ seed: "1e3" }),
    valid({ seed: "18446744073709551616" }), valid({ seed: Number.MAX_SAFE_INTEGER + 1 }),
    valid({ seed: Infinity }), valid({ seed: {} }), valid({ seed: " 123 " }),
  ];
  for (const message of rejected) worker.postMessage(message);
  assert.equal(captured.length, 0);
  worker.postMessage(valid({ seed: 18446744073709551615n }));
  worker.postMessage(valid({ seed: 123 }));
  worker.postMessage(valid({ seed: "00012" }));
  assert.deepEqual(captured.map(value => value.seed), ["18446744073709551615", "123", "12"]);
  assert.equal(env.calls.length, rejected.length + 3);
});

test("media host validation rejects private hosts, lookalikes, credentials and malformed URLs", () => {
  const captured = [];
  const env = environment(value => captured.push(value));
  const worker = new env.Worker();
  for (const url of [
    "http://127.0.0.1/video", "http://localhost/video", "http://[::1]/video",
    "https://finder.video.qq.com.evil.example/video", "https://evilfinder.video.qq.com/video",
    "https://finder.video.qq.com@127.0.0.1/video", "https://user:pw@finder.video.qq.com/video",
    "https://channels.weixin.qq.com/video", "https://qq.com/video", "file:///video",
    "https://finder.video.qq.com:8899/video", "https://finder.video.qq.com\\@evil.example/video",
    "https://finder.video.qq.com/\nvideo", "https://finder.video.qq.com/" + "a".repeat(4096),
  ]) worker.postMessage(valid({ url }));
  assert.equal(captured.length, 0);
  worker.postMessage(valid({ url: "https://a.finder.video.qq.com/video" }));
  assert.equal(captured.length, 1);
});

test("source snapshots omit arbitrary buffers, credentials and invalid numeric metadata", () => {
  const captured = [];
  const env = environment(value => captured.push(value));
  const body = valid({ first: "yes", timestampOffset: Infinity, contentLen: -2, firstRawBuf: new ArrayBuffer(1024), cookie: "fixture" });
  new env.Worker().postMessage(body);
  assert.deepEqual(captured[0], { url: body.url, seed: body.seed });
  assert.equal(body.firstRawBuf.byteLength, 1024);
});

test("hostile message property access does not throw into original postMessage", () => {
  const env = environment(() => {});
  const body = Object.defineProperty({}, "cmd", { get() { throw Error("getter"); } });
  assert.doesNotThrow(() => new env.Worker().postMessage(body));
  assert.equal(env.calls[0].args[0], body);
});

test("production Vue maps a video through container._vnode to its owning props.feed", () => {
  const feed = { id: "target", objectDesc: { media: [] } };
  const other = { id: "other" };
  const video = { parentElement: null };
  const otherVideo = {};
  const app = { _vnode: { component: { subTree: { children: [
    { component: { props: { feed: other }, subTree: { el: otherVideo } } },
    { component: { props: { feed }, subTree: { children: [{ el: video }] } } },
  ] } } } };
  const doc = { querySelectorAll: () => [app] };
  assert.equal(source.findCurrentFeedFromVue(video, doc), feed);
  assert.equal(source.findCurrentFeedFromVue({}, doc), null);
});

test("videojs generated video matches its actual owning DOM wrapper", () => {
  const feed = { id: "generated-video" };
  const wrapper = { parentElement: null };
  const video = { parentElement: wrapper };
  const app = { _vnode: { component: { props: { feed }, subTree: { el: wrapper } } } };
  assert.equal(source.findCurrentFeedFromVue(video, { querySelectorAll: () => [app] }), feed);
});

test("Vue lookup follows suspense active content, avoids cycles and never reads arbitrary getters", () => {
  const video = { parentElement: null };
  const feed = { id: "suspense" };
  const hidden = { id: "hidden" };
  let getterReads = 0;
  const props = Object.defineProperty({}, "feed", { get() { getterReads++; return hidden; } });
  const root = { children: [
    { props, el: video },
    { suspense: { activeBranch: { props: { feed }, el: video }, pendingBranch: { props: { feed: hidden }, el: video } } },
  ] };
  root.children.unshift(root);
  const app = { _vnode: root };
  assert.equal(source.findCurrentFeedFromVue(video, { querySelectorAll: () => [app] }), feed);
  assert.equal(getterReads, 0);
});

test("Vue tree lookup remains bounded with deep or enormous trees", () => {
  const video = { parentElement: null };
  let root = { props: { feed: { id: "too-deep" } }, el: video };
  for (let i = 0; i < 1000; i++) root = { component: { subTree: root } };
  assert.equal(source.findCurrentFeedFromVue(video, { querySelectorAll: () => [{ _vnode: root }] }), null);
  assert.equal(source.findCurrentFeedFromVue(video, { querySelectorAll() { throw Error("dom"); } }), null);
});

test("rendered video ctx.props.feed overrides an unrelated feed inherited from a root wrapper", () => {
  const target = { id: "current-replay", objectDesc: { media: [] } };
  const unrelated = { id: "home-preview" };
  const wrapper = { parentElement: null };
  const video = { parentElement: wrapper };
  const app = { _vnode: { props: { feed: unrelated }, el: wrapper, children: [
    { el: video, ctx: { props: { feed: target } } },
  ] } };
  assert.equal(source.findCurrentFeedFromVue(video, { querySelectorAll: () => [app] }), target);
});

test("nested video render ctx.parent leads to its player feed without inspecting component state", () => {
  const feed = { id: "parent-player" };
  const video = { parentElement: null };
  const player = { props: { feed } };
  const app = { _vnode: { children: [{ el: video, ctx: { parent: player } }] } };
  assert.equal(source.findCurrentFeedFromVue(video, { querySelectorAll: () => [app] }), feed);
});

test("visible video branch is visited ahead of a large home recommendation tree", () => {
  const feed = { id: "late-modal-replay" };
  const wrapper = { parentElement: null };
  const video = { parentElement: wrapper };
  const hiddenNodes = Array.from({ length: 2000 }, () => ({ el: {} }));
  const app = { _vnode: { children: [
    { el: {}, children: hiddenNodes },
    { el: wrapper, children: [{ el: video, ctx: { props: { feed } } }] },
  ] } };
  assert.equal(source.findCurrentFeedFromVue(video, { querySelectorAll: () => [app] }), feed);
});

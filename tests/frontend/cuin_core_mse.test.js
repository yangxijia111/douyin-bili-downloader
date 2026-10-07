/**
 * cuin_core.js MSE 分片旁路 hook 的 Node 单测（node --test tests/frontend/）。
 *
 * 重点验证 hook 的「非侵入」硬性要求（与 fetch/XHR hook 同一标准）：
 *   - appendBuffer 包装后原方法被调用、返回值不变、入参原样传递；
 *   - 分片拷贝独立于原 buffer（页面复用/脱开 buffer 不影响已捕获数据）；
 *   - 回调抛异常不外溢、不阻断原调用；
 *   - 重复安装幂等；缺 MediaSource/SourceBuffer 的环境直接跳过；
 *   - packMseBatches 打包边界（空分片 / 单片超限 / 精确凑批）。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const core = require(path.join(__dirname, "..", "..", "channels", "inject", "assets", "cuin_core.js"));

function makeClasses() {
  // 每个测试新建类：__cuinHooked 标记写在原型上，共享类会跨测试泄漏。
  class SourceBuffer {
    constructor() {
      this.appended = [];
    }
    appendBuffer(buf) {
      this.appended.push(buf);
      // 模拟真实 MSE：调用后可能脱离/复用原 buffer。
      return undefined;
    }
  }
  class MediaSource {
    constructor() {
      this.sourceBuffers = [];
    }
    addSourceBuffer(mime) {
      const sb = new SourceBuffer();
      this.sourceBuffers.push({ mime, sb });
      return sb;
    }
  }
  return { SourceBuffer, MediaSource };
}

function makeEnv() {
  const { SourceBuffer, MediaSource } = makeClasses();
  const events = { sourceBuffer: [], append: [], createObjectURL: [] };
  const createdUrls = [];
  const FakeURL = {
    createObjectURL(obj) {
      const url = "blob:fake-" + createdUrls.length;
      createdUrls.push({ url, obj });
      return url;
    },
  };
  const win = {
    MediaSource,
    SourceBuffer,
    URL: FakeURL,
  };
  const installed = core.installMseHooks(win, {
    onSourceBuffer: (sb, ms, mime) => events.sourceBuffer.push({ sb, ms, mime }),
    onAppend: (sb, chunk) => events.append.push({ sb, chunk }),
    onCreateObjectURL: (url, ms) => events.createObjectURL.push({ url, ms }),
  });
  return { win, events, FakeURL, installed, MediaSource, SourceBuffer };
}

test("installMseHooks 安装三个 hook", () => {
  const { installed } = makeEnv();
  assert.deepEqual(installed.sort(), [
    "mediaSource.addSourceBuffer",
    "sourceBuffer.appendBuffer",
    "url.createObjectURL",
  ]);
});

test("appendBuffer 保持原语义并回调拷贝分片", () => {
  const { win, events } = makeEnv();
  const ms = new win.MediaSource();
  const sb = ms.addSourceBuffer('video/mp4; codecs="avc1.640028"');
  assert.equal(sb.appended.length, 0);

  const raw = new Uint8Array([1, 2, 3, 4, 5]);
  const result = sb.appendBuffer(raw);
  assert.equal(result, undefined);
  assert.equal(sb.appended.length, 1);
  assert.equal(sb.appended[0], raw); // 原方法收到原入参

  assert.equal(events.append.length, 1);
  const chunk = events.append[0].chunk;
  assert.ok(chunk instanceof Uint8Array);
  assert.deepEqual(Array.from(chunk), [1, 2, 3, 4, 5]);
  // 拷贝独立：修改原 buffer 不影响已捕获数据。
  raw[0] = 99;
  assert.equal(chunk[0], 1);
});

test("appendBuffer 支持带 byteOffset 的视图", () => {
  const { win, events } = makeEnv();
  const ms = new win.MediaSource();
  const sb = ms.addSourceBuffer("video/mp4");
  const backing = new ArrayBuffer(16);
  const view = new Uint8Array(backing, 4, 8);
  for (let i = 0; i < 8; i++) view[i] = i + 1;
  sb.appendBuffer(view);
  assert.deepEqual(Array.from(events.append[0].chunk), [1, 2, 3, 4, 5, 6, 7, 8]);
});

test("onAppend 回调抛异常不外溢、不阻断原调用", () => {
  const events = { append: 0 };
  const { SourceBuffer, MediaSource } = makeClasses();
  const win = { MediaSource, SourceBuffer, URL: undefined };
  core.installMseHooks(win, {
    onSourceBuffer() {},
    onAppend() {
      events.append += 1;
      throw new Error("boom");
    },
    onCreateObjectURL() {},
  });
  const ms = new win.MediaSource();
  const sb = ms.addSourceBuffer("video/mp4");
  assert.doesNotThrow(() => sb.appendBuffer(new Uint8Array([1])));
  assert.equal(events.append, 1);
  assert.equal(sb.appended.length, 1);
});

test("重复安装幂等（原型只包一层）", () => {
  const calls = [];
  const { SourceBuffer, MediaSource } = makeClasses();
  const win = { MediaSource, SourceBuffer, URL: undefined };
  const handlers = { onSourceBuffer: () => calls.push("add"), onAppend: () => calls.push("append"), onCreateObjectURL: () => {} };
  core.installMseHooks(win, handlers);
  const first = win.MediaSource.prototype.addSourceBuffer;
  const firstAppend = win.SourceBuffer.prototype.appendBuffer;
  core.installMseHooks(win, handlers);
  assert.equal(win.MediaSource.prototype.addSourceBuffer, first);
  assert.equal(win.SourceBuffer.prototype.appendBuffer, firstAppend);

  const ms = new win.MediaSource();
  ms.addSourceBuffer("video/mp4");
  assert.equal(calls.filter((c) => c === "add").length, 1); // 只回调一次
});

test("缺 MediaSource / SourceBuffer 的环境直接跳过", () => {
  const installed1 = core.installMseHooks({}, {
    onSourceBuffer() {}, onAppend() {}, onCreateObjectURL() {},
  });
  assert.deepEqual(installed1, []);
  const installed2 = core.installMseHooks(
    { MediaSource: makeClasses().MediaSource },
    { onSourceBuffer() {}, onAppend() {}, onCreateObjectURL() {} }
  );
  assert.deepEqual(installed2, []);
});

test("createObjectURL 仅对 MediaSource 实例回调", () => {
  const { win, events } = makeEnv();
  const ms = new win.MediaSource();
  const url = win.URL.createObjectURL(ms);
  assert.match(url, /^blob:fake-/);
  assert.equal(events.createObjectURL.length, 1);
  assert.equal(events.createObjectURL[0].ms, ms);
  win.URL.createObjectURL(new Uint8Array([1])); // 非 MediaSource：不回调
  assert.equal(events.createObjectURL.length, 1);
});

test("packMseBatches 打包边界", () => {
  const bytes = (n, fill) => new Uint8Array(n).fill(fill);
  // 空输入
  assert.deepEqual(core.packMseBatches([], 100), []);
  // 单批合并
  const one = core.packMseBatches([bytes(10, 1), bytes(10, 2)], 100);
  assert.equal(one.length, 1);
  assert.equal(one[0].bytes, 20);
  // 解码后字节序正确
  const decoded = Buffer.from(one[0].b64, "base64");
  assert.deepEqual(Array.from(decoded.subarray(0, 10)), Array(10).fill(1));
  assert.deepEqual(Array.from(decoded.subarray(10)), Array(10).fill(2));
  // 超限切批（每批至少一个分片）
  const multi = core.packMseBatches([bytes(60, 1), bytes(60, 2), bytes(60, 3)], 100);
  assert.equal(multi.length, 2);
  assert.deepEqual(multi.map((b) => b.bytes), [100, 80]);
  // 单片超过 limit 也不丢
  const over = core.packMseBatches([bytes(500, 7)], 100);
  assert.equal(over.length, 5);
  assert.ok(over.every((b) => b.bytes === 100));
  // 空分片被跳过
  const skipEmpty = core.packMseBatches([bytes(0, 0), bytes(5, 9)], 100);
  assert.equal(skipEmpty.length, 1);
  assert.equal(skipEmpty[0].bytes, 5);
});

test("bytesToBase64 与 Buffer 往返一致", () => {
  const data = new Uint8Array(1024);
  for (let i = 0; i < data.length; i++) data[i] = i % 251;
  const b64 = core.bytesToBase64(data);
  assert.deepEqual(Array.from(Buffer.from(b64, "base64")), Array.from(data));
});

test("randomId 满足后端 stream_id 校验", () => {
  for (let i = 0; i < 50; i++) {
    const id = core.randomId();
    assert.match(id, /^[A-Za-z0-9]{8,64}$/);
  }
});

test("chainFingerprint：同视频重复播放同指纹、不同视频不同指纹", () => {
  // 同一视频两次播放：init segment（首个分片）字节恒定。
  const initSeg = new Uint8Array(70 * 1024);
  for (let i = 0; i < initSeg.length; i++) initSeg[i] = i % 251;
  const play1 = [initSeg, new Uint8Array(1024).fill(3)];
  const play2 = [initSeg, new Uint8Array(2048).fill(9)]; // media 分片不同
  assert.notEqual(core.chainFingerprint(play1), core.chainFingerprint(play2));
  assert.equal(core.chainFingerprint(play1), core.chainFingerprint([initSeg, new Uint8Array(1024).fill(3)]));

  // init segment 不同的视频 → 指纹不同。
  const otherInit = new Uint8Array(70 * 1024);
  for (let i = 0; i < otherInit.length; i++) otherInit[i] = (i + 1) % 241;
  assert.notEqual(
    core.chainFingerprint(play1),
    core.chainFingerprint([otherInit, new Uint8Array(1024).fill(3)])
  );

  // 边界：空输入返回空串。
  assert.equal(core.chainFingerprint([]), "");
  assert.equal(core.chainFingerprint(null), "");
});

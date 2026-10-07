/**
 * cuin_core.js Strategy F（preview 页跳转）的 Node 单测。
 *
 * 覆盖：
 *   - detectPageType：finder-preview 页返回 "preview"（不再与 feed 混用）
 *   - extractPreviewJumpTarget：字符串/对象输入、sceneInfo 嵌套取
 *     dynamicExportId、兜底字段序、无 sceneInfo 返回空、坏 JSON 容错
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const core = require(path.join(__dirname, "..", "..", "channels", "inject", "assets", "cuin_core.js"));

test("detectPageType: finder-preview 页返回 preview", () => {
  assert.equal(core.detectPageType("/finder-preview/pages/sph"), "preview");
  assert.equal(core.detectPageType("/finder-preview/pages/feed"), "preview");
});

test("detectPageType: web pages 类型不受影响", () => {
  assert.equal(core.detectPageType("/web/pages/feed"), "feed");
  assert.equal(core.detectPageType("/web/pages/live"), "live");
  assert.equal(core.detectPageType("/other/page"), "");
});

test("extractPreviewJumpTarget: 从 JSON 文本取 sceneInfo.dynamicExportId", () => {
  const text = JSON.stringify({
    errCode: 0,
    data: { sceneInfo: { dynamicExportId: "export_123", nickname: "作者" } },
  });
  assert.equal(core.extractPreviewJumpTarget(text), "export_123");
});

test("extractPreviewJumpTarget: 对象输入（XHR responseType:json）", () => {
  const obj = { data: { sceneInfo: { dynamicExportId: "obj_456" } } };
  assert.equal(core.extractPreviewJumpTarget(obj), "obj_456");
});

test("extractPreviewJumpTarget: 字段兜底序 exportId→objectId→id", () => {
  assert.equal(
    core.extractPreviewJumpTarget({ sceneInfo: { exportId: "e1", id: "i1" } }),
    "e1"
  );
  assert.equal(
    core.extractPreviewJumpTarget({ sceneInfo: { objectId: "o1", id: "i1" } }),
    "o1"
  );
  assert.equal(core.extractPreviewJumpTarget({ sceneInfo: { id: "i2" } }), "i2");
});

test("extractPreviewJumpTarget: 无 sceneInfo 返回空串", () => {
  assert.equal(core.extractPreviewJumpTarget({ data: { foo: 1 } }), "");
  assert.equal(core.extractPreviewJumpTarget("not json at all"), "");
  assert.equal(core.extractPreviewJumpTarget(null), "");
  assert.equal(core.extractPreviewJumpTarget(undefined), "");
});

test("extractPreviewJumpTarget: 嵌套数组内也能找到", () => {
  const text = JSON.stringify({
    list: [{ sceneInfo: { dynamicExportId: "deep_789" } }],
  });
  assert.equal(core.extractPreviewJumpTarget(text), "deep_789");
});

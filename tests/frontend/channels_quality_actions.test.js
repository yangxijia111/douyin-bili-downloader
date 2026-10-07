"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

for (const [file, pages] of [["channels_home.js", ["home"]], ["channels_feed.js", ["feed", "profile"]]]) {
  test(file + " preserves the full captured format selector", async () => {
    const registered = {}, calls = [], view = { feed_id: "lecture" };
    const api = {
      register(page, fn) { registered[page] = fn; },
      mountManagedButton(options) { return options; },
      getCurrentFeed() { return view; },
      waitForCurrentFeed() { return Promise.resolve(view); },
      requestDownload(...args) { calls.push(args); return Promise.resolve({ queued: true }); },
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../channels/inject/assets", file), "utf8"),
      { window: { __CUIN__: api, CUIN_CORE: { buildButtonModel() {} } } });
    for (const page of pages) await registered[page]().onAction("download:format:1080p60");
    assert.deepEqual(calls, pages.map(() => [view, "download", "format:1080p60"]));
  });
}

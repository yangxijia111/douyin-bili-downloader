"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../../web/index.html"), "utf8");
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
function harness() {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", checked: false, hidden: false, disabled: false, dataset: {}, style: {},
      textContent: "", innerHTML: "", listeners: {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      addEventListener(kind, fn) { this.listeners[kind] = fn; },
      setAttribute() {}, removeAttribute() {}, focus() {}, dispatchEvent() {},
    });
    return elements.get(id);
  };
  const content = [{ dataset: { content: "video" }, checked: true }, { dataset: { content: "cover" }, checked: false }];
  const numbers = [{ value: "20", dataset: { num: "post" } }];
  const storage = { getItem() { return null; }, setItem() {} };
  const document = {
    getElementById: element, documentElement: { dataset: { theme: "light" } },
    addEventListener() {}, querySelector() { return null; },
    querySelectorAll(selector) {
      if (selector === "#dlContent input[data-content]") return content;
      if (selector === "#dlNumbers input") return numbers;
      return [];
    },
  };
  const context = vm.createContext({ document, window: { addEventListener() {} },
    localStorage: storage, sessionStorage: storage, URL, AbortController,
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() {}, clearInterval() {},
  });
  vm.runInContext(script.replace(/initDownloadTab\(\);\s*activateTab\("overview"\);\s*$/, "") +
    "\nglobalThis.ui = {parseLinks,urlsOf,linkInfo,buildOverrides,submissionProblems,filterJobs,renderJob,jobBadge,apiErrorMessage,chConnectionText,chQualityOptions,chAbsoluteFilePath,chRenderFeed,chDownload,loadChannelsFeeds,settingsSchema,state};", context);
  element("dlQuality").value = "highest";
  element("dlBatchLimit").value = "20";
  element("dlCommentsMax").value = "500";
  element("dlBatchField").hidden = true;
  return { ui: context.ui, element, numbers, content, context };
}
const plain = (value) => JSON.parse(JSON.stringify(value));

test("Channels shows every saved file address and copies or opens its exact location", () => {
  const { ui, element, context } = harness();
  const mp4 = 'D:\\Downloads\\讲座 "A"\\视频.mp4';
  const jpg = "D:\\Downloads\\讲座\\封面.jpg";
  const rendered = ui.chRenderFeed({ feed_id: "saved", kind: "video", status: "done",
    downloaded_paths: [mp4, jpg] }, "highest");
  assert.ok(rendered.includes('D:\\Downloads\\讲座 &quot;A&quot;\\视频.mp4'));
  assert.ok(rendered.includes(jpg));
  assert.equal((rendered.match(/复制文件路径/g) || []).length, 2);
  assert.equal((rendered.match(/打开文件夹/g) || []).length, 2);
  assert.ok(!rendered.includes('讲座 "A"'));
  const calls = [];
  context.copyText = (value) => calls.push(["copy", value]);
  context.openFolder = (value) => calls.push(["open", value]);
  const click = element("chFeeds").listeners.click;
  click({ target: { closest: (selector) => selector === "button[data-ch-copy]" ? { dataset: { chCopy: mp4 } } : null } });
  click({ target: { closest: (selector) => selector === "button[data-ch-open]" ? { dataset: { chOpen: "D:\\Downloads\\讲座\\" } } : null } });
  assert.deepEqual(calls, [["copy", mp4], ["open", "D:\\Downloads\\讲座\\"]]);
});

test("Channels keeps the earlier saved file accessible when a quality upgrade fails", () => {
  const { ui } = harness();
  assert.ok(ui.chRenderFeed({ feed_id: "saved", kind: "video", status: "failed",
    downloaded_paths: ["/downloads/earlier.mp4"], error: "CDN error" }, "highest")
    .includes('data-ch-copy="/downloads/earlier.mp4"'));
});

test("Channels resolves saved relative paths against the configured directory without doubling it", () => {
  const { ui } = harness();
  ui.state.downloadDirAbs = "D:\\项目\\save\\Downloaded";
  ui.state.downloadDirConfigured = "./save/Downloaded/";
  assert.equal(ui.chAbsoluteFilePath("save\\Downloaded\\TRAE.ai\\视频.mp4"), "D:\\项目\\save\\Downloaded\\TRAE.ai\\视频.mp4");
  assert.equal(ui.chAbsoluteFilePath("D:\\已有文件\\视频.mp4"), "D:\\已有文件\\视频.mp4");
  const rendered = ui.chRenderFeed({ feed_id: "relative", kind: "video", status: "done",
    downloaded_paths: ["save/Downloaded/TRAE.ai/视频.mp4"] }, "highest");
  assert.ok(rendered.includes('data-ch-copy="D:\\项目\\save\\Downloaded\\TRAE.ai\\视频.mp4"'));
  assert.ok(rendered.includes('data-ch-open="D:\\项目\\save\\Downloaded\\TRAE.ai\\"'));
  ui.state.downloadDirAbs = "/home/me/Downloaded";
  ui.state.downloadDirConfigured = "./Downloaded";
  assert.equal(ui.chAbsoluteFilePath("Downloaded/讲座/视频.mp4"), "/home/me/Downloaded/讲座/视频.mp4");
});

test("Channels keeps an older completed download visible after more than 100 captures", async () => {
  const { ui, element, context } = harness();
  const feeds = Array.from({ length: 321 }, (_, i) => ({ feed_id: "feed" + i, title: "Captured " + i,
    kind: "video", status: "pending", quality_options: [] }));
  feeds[144] = { ...feeds[144], title: "Completed replay", status: "done", downloaded_paths: ["Downloaded/replay.mp4"] };
  ui.state.downloadDirAbs = "D:\\Downloads";
  ui.state.downloadDirConfigured = "Downloaded";
  const calls = [];
  context.api = async (path) => { calls.push(path); return { running: true, total: 321, feeds, default_quality: "highest" }; };
  await ui.loadChannelsFeeds();
  assert.equal(calls[0], "/api/v1/channels/feeds?limit=2000");
  const rendered = element("chFeeds").innerHTML;
  assert.ok(rendered.includes("Completed replay"));
  assert.ok(rendered.includes('data-ch-copy="D:\\Downloads\\replay.mp4"'));
  assert.ok(rendered.indexOf("Completed replay") < rendered.indexOf("Captured 0"));
  assert.ok(!rendered.includes("Captured 200"));
  assert.match(element("chFeedCount").textContent, /321.*101/);
});

test("Channels lists actual captured tiers, preserves per-video choice, and enables quality redownload", () => {
  const { ui } = harness();
  const feed = { feed_id: "video1", kind: "video", status: "done", title: "Lecture",
    downloaded_paths: ["/saved/lecture.mp4"], downloaded_quality: { width: 1920, height: 1080 },
    has_original: true, quality_options: [
      { value: "format:1080p60", label: "1080p60 · 1920×1080", width: 1920, height: 1080 },
      { value: "format:720p", label: "720p · 1280×720", width: 1280, height: 720 },
    ] };
  const options = plain(ui.chQualityOptions(feed));
  assert.ok(options.some((option) => option.value === "original"));
  assert.ok(options.some((option) => option.value === "format:1080p60"));
  ui.state.chQualities.video1 = "format:1080p60";
  ui.state.chExplicitQualities.video1 = true;
  const rendered = ui.chRenderFeed(feed, "720p");
  assert.ok(rendered.includes('value="format:1080p60" selected'));
  assert.ok(rendered.includes("重新下载"));
  assert.ok(rendered.includes("lecture.mp4"));
  assert.ok(rendered.includes("已下载 1920×1080"));
  assert.ok(!rendered.includes('value="1080p"'));
  const source = ui.chRenderFeed({ feed_id: "source", kind: "video", status: "pending", quality_options: [] }, "highest");
  assert.ok(source.includes("当前源"));
  assert.ok(source.includes("未捕获其他档位"));
  assert.ok(!source.includes("1080p"));
});

test("Channels follows an updated default until the user chooses a specific per-video tier", () => {
  const { ui } = harness();
  const feed = { feed_id: "video1", kind: "video", status: "pending", quality_options: [
    { value: "format:FHD", label: "1080p", width: 1920, height: 1080 },
    { value: "format:HD", label: "720p", width: 1280, height: 720 },
  ] };
  assert.ok(ui.chRenderFeed({ ...feed, quality_options: [] }, "720p").includes('value="highest" selected'));
  assert.ok(ui.chRenderFeed(feed, "720p").includes('value="format:HD" selected'));
  assert.ok(ui.chRenderFeed(feed, "1080p").includes('value="format:FHD" selected'));
  ui.state.chExplicitQualities.video1 = true;
  assert.ok(ui.chRenderFeed(feed, "720p").includes('value="format:FHD" selected'));
});

test("Channels numeric defaults do not infer a resolution from one unknown image dimension", () => {
  const { ui } = harness();
  const feed = { feed_id: "partial", kind: "video", status: "pending", quality_options: [
    { value: "format:partialHD", label: "高度 720（宽度未知）", width: 0, height: 720 },
    { value: "format:FHD", label: "1080p · 1920×1080", width: 1920, height: 1080 },
  ] };
  const options = plain(ui.chQualityOptions(feed));
  assert.equal(options.find((option) => option.value === "format:partialHD").resolution, 0);
  assert.ok(ui.chRenderFeed(feed, "720p").includes('value="format:FHD" selected'));
});

test("Channels config uses its own quality preference", () => {
  const { ui } = harness();
  const field = ui.settingsSchema().flatMap((section) => section.fields).find((item) => item.p === "channels.quality");
  assert.ok(field);
  assert.ok(field.opts.some((option) => option[0] === "highest"));
  assert.ok(field.opts.some((option) => option[0] === "1080p"));
});

test("Channels download sends selected opaque format in JSON", async () => {
  const { ui, context } = harness();
  const calls = [];
  context.api = async (path, options) => { calls.push({ path, options: plain(options) }); return {}; };
  context.loadChannelsAll = () => {};
  context.toast = () => {};
  ui.state.chQualities.video1 = "format:1080p60";
  await ui.chDownload("video1");
  assert.deepEqual(calls[0], { path: "/api/v1/channels/feeds/video1/download",
    options: { method: "POST", body: { quality: "format:1080p60" } } });
});

test("Channels never treats other applications' proxy traffic as WeChat connected", () => {
  const { ui } = harness();
  const status = { running: true, diagnostics: { counters: { proxy_connections: 100, target_domain_connections: 0 } },
    wechat_connection: { supported: true, wechat_running: true, proxy_connected: false, other_proxy_ports: [7897] } };
  assert.match(ui.chConnectionText(status), /7897/);
  assert.doesNotMatch(ui.chConnectionText(status), /微信已连接/);
  status.wechat_connection.connection_proxy_matches = false;
  assert.match(ui.chConnectionText(status), /尚未同步/);
  status.wechat_connection.connection_proxy_matches = true;
  status.wechat_connection.proxy_connected = true;
  assert.match(ui.chConnectionText(status), /微信已连接/);
});

test("all inline JavaScript compiles and event handlers bind without a browser", () => {
  for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
  assert.ok(harness().ui);
  const markup = html.replace(/<script>[\s\S]*?<\/script>/g, "");
  const ids = [...markup.matchAll(/(?<![\w-])id="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(new Set(ids).size, ids.length, "static IDs must be unique");
});
test("share text extracts every link on the same line and keeps signed query parameters", () => {
  const { ui } = harness();
  const items = ui.parseLinks("作品 https://v.douyin.com/one/ 和 https://www.xiaohongshu.com/explore/123?xsec_token=abc%2Bdef&xsec_source=pc，");
  assert.deepEqual(plain(ui.urlsOf(items)), ["https://v.douyin.com/one/", "https://www.xiaohongshu.com/explore/123?xsec_token=abc%2Bdef&xsec_source=pc"]);
});
test("duplicates do not create multiple jobs and arbitrary share prose is ignored", () => {
  const { ui } = harness();
  const items = ui.parseLinks("分享文案\nhttps://v.douyin.com/a/\nv.douyin.com/a/\n# https://v.douyin.com/ignored/");
  assert.equal(ui.urlsOf(items).length, 1);
  assert.equal(items.filter((i) => i.duplicate).length, 1);
});
test("bare third-party domains and bare BV IDs are recognized", () => {
  const { ui } = harness();
  const urls = ui.urlsOf(ui.parseLinks("xhslink.com/a/b v.kuaishou.com/abc BV1xx411c7mD b23.tv/abc"));
  assert.equal(urls.length, 4);
  assert.equal(ui.linkInfo(urls[0]).key, "xiaohongshu");
  assert.equal(ui.linkInfo(urls[1]).key, "kuaishou");
  assert.equal(ui.linkInfo(urls[3]).platform, "bilibili");
});
test("host matching rejects lookalike and credential-bearing URLs", () => {
  const { ui } = harness();
  for (const url of ["https://evilbilibili.com/video/BV1xx411c7mD", "https://bilibili.com.evil.test/video/a", "https://douyin.com@evil.test/video/a", "https://user:pass@douyin.com/video/a", "ftp://douyin.com/video/a"]) {
    assert.equal(ui.linkInfo(url).supported, false, url);
    assert.equal(ui.urlsOf(ui.parseLinks(url)).length, 0, url);
  }
});
test("all nine registered external platforms match domain suffixes", () => {
  const { ui } = harness();
  for (const [domain, key] of [["iqiyi.com", "iqiyi"], ["v.qq.com", "tencent"], ["youku.com", "youku"], ["mgtv.com", "mgtv"], ["kuaishou.com", "kuaishou"], ["ixigua.com", "xigua"], ["toutiao.com", "toutiao"], ["weibo.cn", "weibo"], ["xhslink.com", "xiaohongshu"]]) {
    assert.equal(ui.linkInfo("https://" + domain + "/v/123").key, key);
  }
});
test("video Channels and unsupported Bilibili courses stay out of the direct queue", () => {
  const { ui } = harness();
  assert.equal(ui.linkInfo("https://weixin.qq.com/sph/a").platform, "channels");
  assert.equal(ui.urlsOf(ui.parseLinks("https://weixin.qq.com/sph/a https://www.bilibili.com/bangumi/play/ep1")).length, 0);
});
test("modal video takes precedence over homepage batch classification", () => {
  const { ui } = harness();
  const info = ui.linkInfo("https://www.douyin.com/user/abc?modal_id=123456");
  assert.equal(info.type, "video");
  assert.equal(info.batch, false);
});
test("Douyin author uses 20 per mode and a single video does not override modes", () => {
  const { ui } = harness();
  const author = plain(ui.buildOverrides("https://www.douyin.com/user/abc"));
  assert.deepEqual(author.mode, ["post"]);
  assert.equal(author.number.post, 20);
  assert.equal(author.video, true);
  assert.equal("mode" in ui.buildOverrides("https://www.douyin.com/video/123"), false);
});
test("Bilibili and yt-dlp overrides use their own number and quality keys", () => {
  const { ui, element } = harness();
  element("dlBatchLimit").value = "7";
  element("dlQuality").value = "720p";
  const bili = plain(ui.buildOverrides("https://space.bilibili.com/123"));
  assert.equal(bili.bilibili.number.user, 7);
  assert.equal(bili.bilibili.number.favlist, 7);
  assert.equal(bili.bilibili.quality, "720p");
  assert.equal("mode" in bili, false);
  const ytdlp = plain(ui.buildOverrides("https://www.iqiyi.com/v_abc.html"));
  assert.equal(ytdlp.ytdlp.number.video, 7);
  assert.equal(ytdlp.ytdlp.quality, "720p");
  assert.equal("comments" in ytdlp, false);
});
test("zero really means all; original and 1440p map to external supported qualities", () => {
  const { ui, element } = harness();
  element("dlBatchLimit").value = "0";
  element("dlQuality").value = "original";
  assert.equal(ui.buildOverrides("https://youku.com/v/a").ytdlp.number.video, 0);
  assert.equal(ui.buildOverrides("https://youku.com/v/a").ytdlp.quality, "highest");
  element("dlQuality").value = "1440p";
  assert.equal(ui.buildOverrides("https://youku.com/v/a").ytdlp.quality, "2k");
});
test("reversed dates are rejected only for applicable Douyin tasks", () => {
  const { ui, element } = harness();
  element("dlLinks").value = "https://douyin.com/user/abc";
  element("dlStart").value = "2026-10-03";
  element("dlEnd").value = "2026-09-01";
  assert.ok(ui.submissionProblems().some((p) => p.includes("日期")));
  element("dlLinks").value = "https://www.bilibili.com/video/BV1xx411c7mD";
  assert.equal(ui.submissionProblems().length, 0);
});
test("batch counts reject empty, negative, fractional and nonfinite values", () => {
  const { ui, element } = harness();
  element("dlLinks").value = "https://iqiyi.com/v/123";
  element("dlBatchField").hidden = false;
  for (const value of ["", "-1", "1.5", "Infinity", "not a number"]) {
    element("dlBatchLimit").value = value;
    assert.ok(ui.submissionProblems().some((p) => p.includes("数量")), value);
  }
});
test("missing homepage mode and collection on someone else's profile are rejected", () => {
  const { ui, element } = harness();
  element("dlLinks").value = "https://douyin.com/user/abc";
  ui.state.modes = [];
  assert.ok(ui.submissionProblems().length);
  ui.state.modes = ["collect"];
  assert.ok(ui.submissionProblems().some((p) => p.includes("自己的收藏")));
});
test("partial failures are visible in failed filter and excluded from completed", () => {
  const { ui } = harness();
  const jobs = [{ status: "success", failed: 1, success: 2, url: "partial" }, { status: "success", failed: 0, url: "good" }, { status: "pending", paused: true, url: "pause" }];
  assert.deepEqual(plain(ui.filterJobs(jobs, "failed", "")), [jobs[0]]);
  assert.deepEqual(plain(ui.filterJobs(jobs, "success", "")), [jobs[1]]);
  assert.deepEqual(plain(ui.filterJobs(jobs, "paused", "")), [jobs[2]]);
  assert.ok(ui.jobBadge(jobs[0]).includes("部分失败"));
  assert.ok(ui.jobBadge({ status: "failed", failed: 1, success: 2 }).includes("部分失败"));
});
test("legacy Bilibili av IDs and missing quality tiers retain the expected platform limits", () => {
  const { ui, element } = harness();
  assert.deepEqual(plain(ui.urlsOf(ui.parseLinks("av123456"))), ["av123456"]);
  assert.equal(ui.linkInfo("https://acg.tv/abc").platform, "bilibili");
  element("dlQuality").value = "540p";
  assert.equal(ui.buildOverrides("av123456").bilibili.quality, "480p");
  element("dlQuality").value = "1440p";
  assert.equal(ui.buildOverrides("av123456").bilibili.quality, "1080p");
});
test("task search covers author and title, case-insensitively", () => {
  const { ui } = harness();
  const jobs = [{ status: "running", author_nickname: "Alice", last_item: "Cats", url: "a" }];
  assert.equal(ui.filterJobs(jobs, "active", "alice").length, 1);
  assert.equal(ui.filterJobs(jobs, "all", "cats").length, 1);
});
test("current media identifiers and error messages are HTML-escaped", () => {
  const { ui } = harness();
  const rendered = ui.renderJob({ status: "running", url: "https://douyin.com/video/123", current: { aweme_id: "<img src=x onerror=alert(1)>", bytes_total: 100, bytes_read: 20 }, error: "<script>bad()</script>", output_dirs: [] });
  assert.ok(!rendered.includes("<img src=x"));
  assert.ok(rendered.includes("&lt;img"));
  assert.ok(!rendered.includes("<script>"));
});
test("API validation arrays produce readable text instead of object Object", () => {
  const { ui } = harness();
  const message = ui.apiErrorMessage({ detail: [{ loc: ["body", "overrides", "number"], msg: "must be nonnegative" }] }, 422);
  assert.ok(message.includes("number"));
  assert.ok(!message.includes("[object Object]"));
});

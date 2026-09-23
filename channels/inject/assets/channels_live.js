/**
 * channels_live.js — 视频号「直播页」页面模块。
 *
 * 直播是 FLV 流、不加密，由后端 ffmpeg 录制（channels.live.LiveRecorder）。
 * 按钮两态：
 *   开始录制 → POST /__cuin/task {action:"record"} → 轮询任务
 *   停止录制 → POST /__cuin/task {action:"stop"}   → 取消录制任务
 *            （ffmpeg 优雅终止，已录制部分文件保留并登记）
 */
(function () {
  "use strict";
  var api = window.__CUIN__;
  var core = window.CUIN_CORE;
  if (!api || !core) return;

  var recording = false;
  var mount = null;

  function getModel() {
    if (recording) {
      return { ready: true, label: "停止录制", menu: [{ id: "stop", label: "停止录制" }] };
    }
    var view = api.getCurrentFeed();
    var model = core.buildButtonModel(view);
    if (model.ready && view.kind === "live") {
      return { ready: true, label: "开始录制", menu: [{ id: "record", label: "开始录制" }] };
    }
    // 直播 feed 尚未解析：仍然显示按钮（点击给出引导提示）。
    return { ready: false, label: "开始录制", menu: [] };
  }

  function onAction(actionId) {
    if (actionId === "stop") {
      var current = api.getCurrentFeed();
      if (!current) return Promise.reject(new Error("没有进行中的录制任务"));
      recording = false;
      if (mount) mount.refresh();
      return api.requestDownload(current, "stop");
    }
    return api.waitForCurrentFeed(4000).then(function (view) {
      if (!view) {
        throw new Error("尚未识别当前直播，请进入直播页面等待 1–2 秒");
      }
      recording = true;
      if (mount) mount.refresh();
      return api.requestDownload(view, "record").then(
        function (task) { recording = false; if (mount) mount.refresh(); return task; },
        function (err) { recording = false; if (mount) mount.refresh(); throw err; }
      );
    });
  }

  api.register("live", function () {
    mount = api.mountManagedButton({
      label: "开始录制",
      selectors: [
        ".op-item",
        "[class*='op-item']",
        ".live-op",
        ".bottom-bar",
        ".toolbar",
      ],
      floating: true,
      getModel: getModel,
      onAction: onAction,
    });
    return {
      unmount: function () {
        recording = false;
        if (mount) mount.unmount();
        mount = null;
      },
    };
  });
})();

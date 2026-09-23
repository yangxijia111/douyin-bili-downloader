/**
 * channels_feed.js — 视频号「视频详情页 / 作者主页」页面模块。
 *
 * 详情页优先插入到微信现有的操作栏（点赞 / 评论 / 分享那一行）；找不到时
 * 由 bootstrap 的悬浮按钮兜底。选择器链与首页模块同样遵循
 * primary → fallback → floating 的降级顺序。
 */
(function () {
  "use strict";
  var api = window.__CUIN__;
  var core = window.CUIN_CORE;
  if (!api || !core) return;

  function getModel() {
    return core.buildButtonModel(api.getCurrentFeed());
  }

  function onAction(actionId) {
    return api.waitForCurrentFeed(4000).then(function (view) {
      if (!view) {
        throw new Error("尚未识别当前视频，请播放视频 1–2 秒或切换一次视频");
      }
      var parts = String(actionId).split(":");
      return api.requestDownload(view, parts[0], parts[1] || null);
    });
  }

  api.register("feed", function () {
    return api.mountManagedButton({
      label: "下载",
      selectors: [
        ".op-item",
        "[class*='op-item']",
        ".feed-op",
        ".bottom-bar",
        ".toolbar",
      ],
      floating: true,
      getModel: getModel,
      onAction: onAction,
    });
  });

  // 作者主页（profile）与详情页共用同一套交互。
  api.register("profile", function () {
    return api.mountManagedButton({
      label: "下载",
      selectors: [
        ".op-item",
        "[class*='op-item']",
        ".feed-op",
        ".bottom-bar",
        ".toolbar",
      ],
      floating: true,
      getModel: getModel,
      onAction: onAction,
    });
  });
})();

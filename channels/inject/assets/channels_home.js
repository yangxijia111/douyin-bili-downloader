/**
 * channels_home.js — 视频号「首页推荐流」页面模块。
 *
 * 按钮挂载策略（与规范一致，不依赖单一 selector）：
 *   primary  .slides-item .click-box.op-item   （当前视频的操作按钮区域）
 *   fallback .slides-item .op-item / .op-item / [class*="op-item"]
 *   最终     右下悬浮按钮（bootstrap.mountManagedButton 的 floating 兜底）
 *
 * MutationObserver 监听页面切换与 DOM 复用（微信 SPA 会回收幻灯片节点），
 * 按钮被移走时自动重新挂载；幂等由 data-cuin-btn 标记保证。
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

  api.register("home", function () {
    return api.mountManagedButton({
      label: "下载",
      selectors: [
        ".slides-item .click-box.op-item",
        ".slides-item .op-item",
        ".slides-item [class*='op-item']",
        ".op-item",
        "[class*='op-item']",
      ],
      floating: true,
      getModel: getModel,
      onAction: onAction,
    });
  });
})();

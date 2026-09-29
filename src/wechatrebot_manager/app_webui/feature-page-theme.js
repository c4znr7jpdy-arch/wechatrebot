"use strict";

(() => {
  window.sendFeaturePageContext = function (frame) {
    if (!frame?.contentWindow || !frame.src) return;
    const origin = new URL(frame.src, window.location.href).origin;
    if (origin !== window.location.origin) return;
    frame.contentWindow.postMessage({channel: "astrbot-plugin-page", kind: "context", context: {
      locale: "zh-CN", isDark: document.documentElement.dataset.theme === "dark",
    }}, origin);
  };
  window.syncFeaturePageTheme = function (frame, theme, onStylesReady = null) {
    if (!frame?.contentWindow) return;
    theme = theme === "dark" ? "dark" : "light";
    let ready = false;
    const finishStyles = () => {
      if (ready) return;
      ready = true;
      onStylesReady?.();
    };
    try {
      const frameDocument = frame.contentDocument;
      const root = frameDocument?.documentElement;
      if (root) {
        root.dataset.theme = theme;
        if (frameDocument.querySelector(".studio-shell")) {
          root.dataset.managerFeaturePage = "mimo";
          const styleId = "wechatrebot-feature-page-overrides";
          let stylesheet = frameDocument.getElementById(styleId);
          if (!stylesheet && frameDocument.head) {
            stylesheet = frameDocument.createElement("link");
            stylesheet.id = styleId;
            stylesheet.rel = "stylesheet";
            stylesheet.href = new URL("/feature-page-overrides.css", window.location.href).href;
            stylesheet.addEventListener("load", finishStyles, {once: true});
            stylesheet.addEventListener("error", finishStyles, {once: true});
            frameDocument.head.appendChild(stylesheet);
          } else if (stylesheet?.sheet) {
            finishStyles();
          } else if (stylesheet) {
            stylesheet.addEventListener("load", finishStyles, {once: true});
            stylesheet.addEventListener("error", finishStyles, {once: true});
          } else {
            finishStyles();
          }
        } else {
          delete root.dataset.managerFeaturePage;
          finishStyles();
        }
      } else {
        finishStyles();
      }
    } catch (_error) {
      finishStyles();
    }
    frame.contentWindow.postMessage({channel: "wechatrebot-theme", theme}, "*");
    window.sendFeaturePageContext(frame);
  };
})();

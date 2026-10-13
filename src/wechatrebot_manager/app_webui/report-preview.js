"use strict";

// Reuse the manager's native dialog pattern for Escape and focus restoration.
const openImagePreview = (() => {
  let dialog, image, viewport, status, zoom, fit = true, scale = 1, message = false;
  function layout() {
    if (!dialog.open || !image.naturalWidth) return;
    const fitWidth = Math.min(image.naturalWidth, viewport.clientWidth - 32,
      message ? (viewport.clientHeight - 32) * image.naturalWidth / image.naturalHeight : Infinity);
    const width = fit ? fitWidth : image.naturalWidth * scale;
    image.style.width = `${Math.max(1, width)}px`;
    zoom.textContent = `${Math.round(width / image.naturalWidth * 100)}%`;
  }
  function changeZoom(factor) {
    if (image.hidden || !image.naturalWidth) return;
    const minimum = Math.min(.2, (viewport.clientWidth - 32) / image.naturalWidth,
      message ? (viewport.clientHeight - 32) / image.naturalHeight : Infinity);
    scale = Math.max(minimum, Math.min(3, image.getBoundingClientRect().width / image.naturalWidth * factor));
    fit = false;
    layout();
    if (message) viewport.scrollTo((viewport.scrollWidth - viewport.clientWidth) / 2,
      (viewport.scrollHeight - viewport.clientHeight) / 2);
  }
  function initialize() {
    dialog = document.createElement("dialog");
    dialog.className = "report-preview-dialog";
    dialog.setAttribute("aria-labelledby", "reportPreviewTitle");
    dialog.innerHTML = `<header class="report-preview-header"><div><h2 id="reportPreviewTitle">报告样式预览</h2><p>示例数据 · 向下滚动查看完整报告</p></div><button type="button" class="button ghost report-preview-close" data-action="close" aria-label="关闭预览" title="关闭预览" autofocus><svg aria-hidden="true" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header>
      <p class="report-preview-status" role="status"></p><div class="report-preview-viewport" tabindex="0" aria-label="完整报告，可滚动查看"><img alt="完整报告样式预览（示例数据）" hidden></div>
      <div class="report-preview-tools" aria-label="图片显示控制"><button type="button" class="button ghost small" data-action="out" aria-label="缩小预览">−</button><output aria-live="polite">100%</output><button type="button" class="button ghost small" data-action="in" aria-label="放大预览">＋</button></div>`;
    document.body.append(dialog);
    image = dialog.querySelector("img");
    viewport = dialog.querySelector(".report-preview-viewport");
    status = dialog.querySelector(".report-preview-status");
    zoom = dialog.querySelector("output");
    image.addEventListener("load", () => {
      image.hidden = false;
      status.hidden = true;
      layout();
      viewport.scrollTo(0, 0);
    });
    image.addEventListener("error", () => {
      image.hidden = true;
      status.hidden = false;
      status.textContent = "预览图片加载失败，请关闭后重新打开。";
    });
    viewport.addEventListener("wheel", (event) => {
      // Long reports retain native vertical scrolling.
      if (!message || image.hidden || !event.deltaY) return;
      event.preventDefault();
      changeZoom(event.deltaY < 0 ? 1.25 : .8);
    }, { passive: false });
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const targets = [...dialog.querySelectorAll('button, [tabindex="0"]')];
      const first = targets[0], last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    });
    dialog.addEventListener("click", (event) => {
      const action = event.target.closest("button")?.dataset.action;
      if (action === "close") dialog.close();
      if (action === "in" || action === "out") changeZoom(action === "in" ? 1.25 : .8);
      if (event.target === dialog) {
        const rect = dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
      }
      layout();
    });
    dialog.addEventListener("close", () => {
      image.removeAttribute("src");
      image.hidden = true;
    });
    new ResizeObserver(layout).observe(viewport);
  }
  return (src, name, isMessage = false) => {
    if (isMessage ? !(typeof src === "string" && src.length <= 88000 && /^data:image\/webp;base64,[A-Za-z0-9+/=]+$/.test(src))
      : !/^\/assets\/report-preview-[\w-]+\.webp$/.test(src)) return;
    if (!dialog) initialize();
    message = isMessage;
    fit = true;
    dialog.classList.toggle("is-message-preview", message);
    dialog.querySelector("h2").textContent = message ? (name || "消息图片") : `${name || "报告样式"} · 完整预览`;
    dialog.querySelector("header p").hidden = message;
    viewport.setAttribute("aria-label", message ? "消息图片，可缩放和滚动查看" : "完整报告，可滚动查看");
    image.alt = message ? (name || "消息图片") : "完整报告样式预览（示例数据）";
    status.textContent = "正在加载完整预览…";
    status.hidden = false;
    image.hidden = true;
    zoom.textContent = "—";
    dialog.showModal();
    image.src = src;
  };
})();
window.openReportPreview = (src, name) => openImagePreview(src, name);
window.openMessageImagePreview = (src, name) => openImagePreview(src, name, true);

"use strict";

// Reuse the manager's native dialog pattern for Escape and focus restoration.
const openImagePreview = (() => {
  let dialog, image, viewport, status, statusSlot, zoom, copyMenu, opener, fit = true, scale = 1, message = false;
  let loadToken = 0, imageMode = "thumb", statusSticky = false, enlargeToFit = false;
  let statusTimer = 0;
  let statusFadeTimer = 0;
  const STATUS_AUTO_HIDE_MS = 3600;
  const STATUS_FADE_MS = 180;

  function clearStatusTimer() {
    window.clearTimeout(statusTimer);
    window.clearTimeout(statusFadeTimer);
    statusTimer = 0;
    statusFadeTimer = 0;
  }

  function showStatus(text, {keepOnLoad = false} = {}) {
    clearStatusTimer();
    statusSticky = keepOnLoad;
    status.textContent = text;
    status.hidden = false;
    status.classList.remove("is-hiding");
    // Commit the transparent starting style before revealing a newly shown notice.
    // Keep the tallest notice slot until closing, so dismissal never resizes the image.
    const height = statusSlot.getBoundingClientRect().height;
    if (message) statusSlot.style.minHeight = `${height}px`;
    status.classList.add("is-visible");
    statusTimer = window.setTimeout(hideStatus, STATUS_AUTO_HIDE_MS);
  }

  function hideStatus() {
    if (status.hidden || status.classList.contains("is-hiding")) return;
    clearStatusTimer();
    statusSticky = false;
    if (message) statusSlot.style.minHeight = `${statusSlot.getBoundingClientRect().height}px`;
    status.classList.remove("is-visible");
    status.classList.add("is-hiding");
    statusFadeTimer = window.setTimeout(() => {
      status.hidden = true;
      statusSticky = false;
      status.classList.remove("is-hiding");
      statusFadeTimer = 0;
    }, window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : STATUS_FADE_MS);
  }

  function resetStatus() {
    clearStatusTimer();
    status.hidden = true;
    statusSticky = false;
    status.classList.remove("is-visible", "is-hiding");
  }

  function validImageSource(src, kind = "report") {
    if (typeof src !== "string") return false;
    if (kind === "report") return /^\/assets\/report-preview-[\w-]+\.webp$/.test(src);
    const maxLength = kind === "qrcode" ? 2 * 1024 * 1024 + 64 : 28 * 1024 * 1024;
    if (src.length > maxLength) return false;
    const formats = kind === "qrcode" ? "jpeg|png" : "jpeg|png|webp|gif";
    return new RegExp(`^data:image\\/(?:${formats});base64,[A-Za-z0-9+/=]+$`).test(src);
  }
  async function copyImage() {
    copyMenu.hidden = true;
    const source = image.src;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      canvas.getContext("2d").drawImage(image, 0, 0);
      const png = new Promise((resolve, reject) => canvas.toBlob(
        (blob) => blob ? resolve(blob) : reject(new Error("无法转换图片")), "image/png"));
      await navigator.clipboard.write([new ClipboardItem({"image/png": png})]);
      if (!dialog.open || image.src !== source) return;
      showStatus("图片已复制");
    } catch (_error) {
      if (!dialog.open || image.src !== source) return;
      showStatus("复制失败，请检查剪贴板权限后重试。");
    }
  }
  function fittedWidth() {
    return Math.max(1, Math.min(enlargeToFit ? Infinity : image.naturalWidth, viewport.clientWidth - 32,
      message ? (viewport.clientHeight - 32) * image.naturalWidth / image.naturalHeight : Infinity));
  }
  function layout() {
    if (!dialog.open || !image.naturalWidth) return;
    const width = fit ? fittedWidth() : image.naturalWidth * scale;
    image.style.width = `${Math.max(1, width)}px`;
    zoom.textContent = `${Math.round(width / image.naturalWidth * 100)}%`;
  }
  function changeZoom(factor) {
    if (image.hidden || !image.naturalWidth) return;
    const minimum = Math.min(.2, (viewport.clientWidth - 32) / image.naturalWidth,
      message ? (viewport.clientHeight - 32) / image.naturalHeight : Infinity);
    const maximum = enlargeToFit ? Math.max(3, fittedWidth() / image.naturalWidth * 4) : 3;
    scale = Math.max(minimum, Math.min(maximum, image.getBoundingClientRect().width / image.naturalWidth * factor));
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
      <div class="report-preview-status-slot"><p class="report-preview-status" role="status" hidden></p></div><div class="report-preview-viewport" tabindex="0" aria-label="完整报告，可滚动查看"><img alt="完整报告样式预览（示例数据）" hidden></div>
      <div class="report-preview-tools" aria-label="图片显示控制"><button type="button" class="button ghost small" data-action="out" aria-label="缩小预览">−</button><output aria-live="polite">100%</output><button type="button" class="button ghost small" data-action="in" aria-label="放大预览">＋</button></div>`;
    document.body.append(dialog);
    image = dialog.querySelector("img");
    viewport = dialog.querySelector(".report-preview-viewport");
    status = dialog.querySelector(".report-preview-status");
    statusSlot = dialog.querySelector(".report-preview-status-slot");
    zoom = dialog.querySelector("output");
    image.addEventListener("load", () => {
      image.hidden = false;
      if (!statusSticky) hideStatus();
      layout();
      viewport.scrollTo(0, 0);
    });
    image.addEventListener("error", () => {
      if (message && imageMode === "original") {
        imageMode = "thumb";
        showStatus("原图加载失败，已显示缩略图。", {keepOnLoad: true});
        image.src = image.dataset.thumbnailSource || "";
        return;
      }
      image.hidden = true;
      showStatus("预览图片加载失败，请关闭后重新打开。", {keepOnLoad: true});
    });
    viewport.addEventListener("wheel", (event) => {
      // Long reports retain native vertical scrolling.
      if (!message || image.hidden || !event.deltaY) return;
      event.preventDefault();
      changeZoom(event.deltaY < 0 ? 1.25 : .8);
    }, { passive: false });
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const targets = [...dialog.querySelectorAll('button, [tabindex="0"]')].filter((node) => node.getClientRects().length);
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
      if (action === "copy") copyImage();
      copyMenu.hidden = true;
      if (message && event.target === viewport) dialog.close();
      if (event.target === dialog) {
        const rect = dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
      }
      layout();
    });
    dialog.addEventListener("close", () => {
      if (dialog.open) return;
      loadToken += 1;
      imageMode = "thumb";
      resetStatus();
      copyMenu.hidden = true;
      image.removeAttribute("src");
      image.removeAttribute("data-thumbnail-source");
      image.style.removeProperty("width");
      image.hidden = true;
      // Polling may have replaced the QR button while the modal was open.
      if (!opener?.isConnected && opener?.dataset.qrKey) {
        const current = document.querySelector("#conversationInspector .group-qrcode");
        if (current?.dataset.qrKey === opener.dataset.qrKey) current.focus();
      }
    });
    copyMenu = document.createElement("button");
    copyMenu.type = "button";
    copyMenu.className = "preview-copy-menu";
    copyMenu.dataset.action = "copy";
    copyMenu.textContent = "复制图片";
    copyMenu.hidden = true;
    dialog.append(copyMenu);
    image.addEventListener("contextmenu", (event) => {
      if (!message || image.hidden) return;
      event.preventDefault();
      copyMenu.hidden = false;
      copyMenu.style.left = `${Math.max(0, Math.min(event.clientX, innerWidth - copyMenu.offsetWidth - 8))}px`;
      copyMenu.style.top = `${Math.max(0, Math.min(event.clientY, innerHeight - copyMenu.offsetHeight - 8))}px`;
      copyMenu.focus();
    });
    new ResizeObserver(layout).observe(viewport);
  }
  return (src, name, kind = "report", options = {}) => {
    const isMessage = kind !== "report";
    if (!validImageSource(src, kind) || (isMessage && kind !== "qrcode" && src.length > 88000)) return;
    if (!dialog) initialize();
    resetStatus();
    statusSlot.style.removeProperty("min-height");
    const token = ++loadToken;
    message = isMessage;
    enlargeToFit = kind === "message";
    const originalPending = typeof options.loadOriginal === "function";
    imageMode = "thumb";
    statusSticky = originalPending;
    fit = true;
    scale = 1;
    dialog.classList.toggle("is-message-preview", message);
    dialog.setAttribute("aria-label", name || (message ? "消息图片" : "报告样式预览"));
    dialog.querySelector("h2").textContent = message ? (name || "消息图片") : `${name || "报告样式"} · 完整预览`;
    dialog.querySelector("header p").hidden = message;
    viewport.setAttribute("aria-label", message ? "消息图片，可缩放和滚动查看" : "完整报告，可滚动查看");
    image.alt = message ? (name || "消息图片") : "完整报告样式预览（示例数据）";
    image.hidden = true;
    image.dataset.thumbnailSource = src;
    copyMenu.hidden = true;
    zoom.textContent = "—";
    opener = document.activeElement;
    dialog.showModal();
    showStatus(originalPending ? "正在获取原图…" : "正在加载完整预览…", {keepOnLoad: originalPending});
    image.src = src;
    if (originalPending) {
      const stillCurrent = () => token === loadToken && dialog.open && options.isCurrent?.() !== false;
      Promise.resolve().then(() => stillCurrent() ? options.loadOriginal() : null).then((original) => {
        if (!stillCurrent()) return;
        const candidate = typeof original === "string" ? original : original?.src;
        if (!validImageSource(candidate, "message")) {
          throw new Error(original?.message || "原图暂不可用，已显示缩略图。");
        }
        statusSticky = original?.quality === "cached";
        imageMode = "original";
        if (original?.quality === "cached") {
          showStatus("当前为已缓存图片，原图不可用。", {keepOnLoad: true});
        }
        fit = true;
        image.src = candidate;
      }).catch((error) => {
        if (!stillCurrent()) return;
        showStatus(error?.message || "原图暂不可用，已显示缩略图。", {keepOnLoad: true});
      });
    }
  };
})();
window.openReportPreview = (src, name) => openImagePreview(src, name);
window.openMessageImagePreview = (src, name, options = {}) => openImagePreview(src, name, "message", options);
window.openGroupQrPreview = (src, name) => openImagePreview(src, name, "qrcode");
window.closeMessageImagePreview = () => document.querySelector(".is-message-preview[open]")?.close();

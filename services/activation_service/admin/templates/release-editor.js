"use strict";

(() => {
  window.addEventListener("pageshow", () => {
    document.querySelectorAll('form[action$="/releases/publish"] button, #release-editor button')
      .forEach(button => { button.disabled = false; });
  });
  const fields = document.getElementById("release-editor-fields");
  if (!fields) {
    // Only lock the final confirmation after native required-field validation succeeds.
    document.querySelector('form[action$="/releases/publish"]')?.addEventListener("submit", event => {
      event.currentTarget.querySelector('button[type="submit"]').disabled = true;
      event.currentTarget.querySelector('button[type="submit"]').textContent = "正在发布，请等待…";
    });
    return;
  }
  const form = fields.form;
  const select = form.elements.namedItem("version");
  const title = form.elements.namedItem("title");
  const notes = form.elements.namedItem("notes");
  const digest = form.elements.namedItem("manifest_digest");
  const status = document.getElementById("release-editor-status");
  let loaded = "", dirty = false, sequence = 0, ready = false;
  function setEnabled(usable) {
    fields.disabled = !ready;
    for (const control of [title, notes, ...form.querySelectorAll('button[type="submit"]')]) {
      control.disabled = !ready || !usable;
    }
  }
  title.addEventListener("input", () => { dirty = true; });
  notes.addEventListener("input", () => { dirty = true; });

  async function loadDraft() {
    const version = select.value;
    const attempt = ++sequence;
    digest.value = "";
    setEnabled(false);
    status.textContent = "正在读取更新说明草稿…";
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(`/wechatrebot-admin/releases/editor?version=${encodeURIComponent(version)}`,
        {cache: "no-store", credentials: "same-origin", signal: controller.signal});
      if (response.redirected || !response.ok) throw new Error("草稿读取失败，请刷新版本信息后重试；登录过期时请重新登录。");
      const value = await response.json();
      if (attempt !== sequence) return;
      if (value.version !== version) throw new Error("候选版本已变化，请刷新页面。");
      title.value = value.title;
      notes.value = value.notes;
      digest.value = value.manifest_sha256;
      loaded = version;
      dirty = false;
      status.textContent = "填写完成后可保存草稿，或预览并进入二次确认。保存草稿不会通知客户端。";
    } catch (error) {
      if (attempt === sequence) status.textContent = error.name === "AbortError"
        ? "草稿读取超时，请点击刷新版本信息重试。" : error.message;
    } finally {
      clearTimeout(timeout);
      if (attempt === sequence) setEnabled(Boolean(digest.value));
    }
  }

  select.addEventListener("change", () => {
    if (dirty && !window.confirm("当前修改还没有保存，确定切换版本吗？")) {
      select.value = loaded;
      return;
    }
    loadDraft();
  });
  document.addEventListener("releases-unavailable", () => {
    ready = false;
    setEnabled(false);
    status.textContent = "版本信息暂不可用。已保留你填写的说明，请刷新版本信息后重试。";
  });
  document.addEventListener("releases-loaded", event => {
    const {stable, listing} = event.detail;
    if (!listing) return;
    ready = Boolean(stable.value?.verified && listing.value && !stable.error && !listing.error
      && !stable.loading && !listing.loading);
    if (!ready) { setEnabled(false); return; }
    const versions = listing.value.filter(item => (item.available ?? item.verified) && item.pending);
    if (!versions.length) {
      ready = false;
      setEnabled(false);
      if (!dirty) select.replaceChildren(new Option("暂无待发布版本", ""));
      status.textContent = "暂无待发布版本，请先让 Codex 准备好更新包。";
      return;
    }
    const selected = select.value;
    if (!loaded || versions.some(item => item.version === selected)) {
      select.replaceChildren(...versions.map(item => new Option(item.version, item.version)));
      if (versions.some(item => item.version === selected)) select.value = selected;
    } else {
      ready = false;
      setEnabled(false);
      status.textContent = "待发布版本已变化，已保留草稿内容。请刷新页面后重新核对。";
      return;
    }
    if (loaded !== select.value || !digest.value) loadDraft();
    else setEnabled(true);
  });
  form.addEventListener("submit", () => {
    dirty = false;
    form.querySelectorAll('button[type="submit"]').forEach(button => { button.disabled = true; });
  });
})();

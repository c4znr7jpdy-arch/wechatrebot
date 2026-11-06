"use strict";

(() => {
  const root = document.getElementById("release-data");
  if (!root) return;
  const scope = root.dataset.releaseScope;
  const refresh = document.getElementById("refresh-releases");
  const stableVersion = document.getElementById("stable-version");
  const stableStatus = document.getElementById("stable-status");
  const listingStatus = document.getElementById("release-listing-status");
  const rows = document.getElementById("release-rows");
  const fields = document.getElementById("release-switch-fields");
  const form = fields?.form;
  let timer, controller, stopped = false, busy = false, resumePending = false;
  let rowsKey = "", formKey = "", edited = false, stableLoaded = false;
  form?.addEventListener("input", () => { edited = true; });
  form?.addEventListener("change", () => { edited = true; });

  function statusText(state, label, previous = false) {
    const saved = state.value !== null || previous;
    const age = state.age_seconds !== null ? `（${state.age_seconds} 秒前读取）` : "";
    if (state.error) return `${state.error}${saved ? ` 显示上次验证结果${age}。` : ""}`;
    if (state.loading) return saved ? `正在更新${label}，显示上次验证结果${age}。` : `正在读取${label}，可继续使用其他功能。`;
    return `${label}已读取${age}。`;
  }

  function renderRows(listing) {
    const key = JSON.stringify(listing);
    if (key === rowsKey) return;
    rowsKey = key;
    const fragment = document.createDocumentFragment();
    for (const item of listing) {
      const row = document.createElement("tr");
      for (const value of [item.version, item.published_at]) {
        const cell = row.insertCell();
        cell.textContent = value;
      }
      const assets = row.insertCell();
      for (const asset of item.assets) {
        const line = document.createElement("div");
        line.textContent = `${asset.name} · ${Number(asset.size).toLocaleString()} 字节`;
        assets.append(line);
      }
      row.insertCell().textContent = item.verified ? "签名通过" : `不可选：${item.error}`;
      fragment.append(row);
    }
    rows.replaceChildren(fragment);
  }

  function render(data) {
    const stable = data.stable;
    if (stable.value) {
      stableVersion.textContent = `${stable.value.version}（签名已验证）`;
      stableLoaded = true;
    } else if (!stableLoaded && !stable.loading) stableVersion.textContent = "暂不可用";
    stableStatus.textContent = statusText(stable, "稳定版本", stableLoaded);
    if (!fields) return;
    const listing = data.listing;
    if (listing.value) renderRows(listing.value);
    listingStatus.textContent = statusText(listing, "发布记录", Boolean(rowsKey));
    const verified = (listing.value || []).filter(item => item.verified);
    const ready = stable.value && stable.value.verified && verified.length > 0;
    if (ready) {
      const key = stable.value.digest + JSON.stringify(verified.map(item => item.version));
      if (!formKey || (!edited && key !== formKey)) {
        const select = form.elements.namedItem("version");
        const selected = select.value;
        select.replaceChildren(...verified.map(item => new Option(item.version, item.version)));
        if (verified.some(item => item.version === selected)) select.value = selected;
        form.elements.namedItem("stable_digest").value = stable.value.digest;
        formKey = key;
        fields.disabled = false;
      } else if (key !== formKey) {
        listingStatus.textContent += " 已保留当前选择和动态码；提交时会重新核验，若提示清单已变更，请刷新页面后重新确认。";
      }
    }
    fields.querySelector('button[type="submit"]').disabled = !ready || stable.loading || listing.loading || Boolean(stable.error || listing.error);
    if (!listing.loading && listing.value && verified.length === 0 && !listing.error) {
      listingStatus.textContent += " 暂无签名通过的可选版本。";
    }
  }

  async function load(force = false) {
    if (busy || stopped) return;
    busy = true;
    refresh.disabled = true;
    clearTimeout(timer);
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    let loading = false;
    try {
      const options = {signal: controller.signal, cache: "no-store", credentials: "same-origin"};
      let url = `/wechatrebot-admin/releases/status?scope=${encodeURIComponent(scope)}`;
      if (force) {
        url = "/wechatrebot-admin/releases/refresh";
        options.method = "POST";
        options.body = new URLSearchParams({scope, csrf: refresh.dataset.csrf});
      }
      const response = await fetch(url, options);
      if (response.redirected) throw new Error("登录已失效，请重新登录后查看版本信息。");
      if (!response.ok) throw new Error("版本信息读取失败，请稍后点击刷新重试。");
      const data = await response.json();
      if (stopped) return;
      render(data);
      loading = Object.values(data).some(state => state.loading);
    } catch (error) {
      if (!stopped) {
        stableStatus.textContent = error.name === "AbortError" ? "版本信息读取超时，请稍后点击刷新重试。" : error.message;
        if (fields) fields.querySelector('button[type="submit"]').disabled = true;
      }
    } finally {
      clearTimeout(timeout);
      busy = false;
      refresh.disabled = false;
      const resuming = resumePending;
      resumePending = false;
      if ((loading || resuming) && !stopped) timer = setTimeout(() => load(), resuming ? 0 : 1000);
    }
  }

  refresh.addEventListener("click", () => load(true));
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      stopped = false;
      if (busy) resumePending = true;
      else load();
    }
  });
  load();
})();

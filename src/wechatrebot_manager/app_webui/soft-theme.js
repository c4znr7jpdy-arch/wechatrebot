/* Skin selection is independent of light/dark mode. The server supplies the
 * bundled catalog and saved preference before the first paint. */
(() => {
  "use strict";
  const KEY = "wxrebot-skin";
  const root = document.documentElement;
  const catalogNode = document.getElementById("managerSkinCatalog");
  const catalog = catalogNode ? JSON.parse(catalogNode.textContent) : [
    {id: "default", name: "默认", description: "经典界面", stylesheet: ""},
  ];
  const entries = Object.freeze(catalog.map((entry) => Object.freeze(entry)));
  const find = (id) => entries.find((entry) => entry.id === id);
  const saved = find(root.dataset.savedSkin)?.id;
  const requested = find(new URLSearchParams(window.location.search).get("style"))?.id;
  let remembered;
  try { remembered = find(window.localStorage.getItem(KEY))?.id; } catch (_error) { /* Optional cache. */ }
  let current = requested || saved || remembered || "default";
  let failure = "";

  function available(id) {
    const entry = find(id);
    if (!entry) return false;
    if (!entry.stylesheet) return true;
    try {
      return Boolean(document.querySelector(`link[data-manager-skin="${id}"]`)?.sheet?.cssRules.length);
    } catch (_error) { return false; /* Only bundled same-origin styles are usable. */ }
  }

  function apply(id) {
    current = find(id)?.id || "default";
    if (current === "default") delete root.dataset.skin;
    else root.dataset.skin = current;
  }

  function verifyStyles() {
    if (available(current)) return;
    failure = "皮肤资源未能加载，已使用默认皮肤。请重新打开管理器后重试。";
    apply("default");
    window.dispatchEvent(new Event("manager-skin-change"));
  }

  async function save(id, savePreference) {
    if (!available(id)) throw new Error("皮肤资源未能加载，请重新打开管理器后重试。");
    await savePreference(id);
    root.dataset.savedSkin = id;
    apply(id);
    failure = "";
    try { window.localStorage.setItem(KEY, id); } catch (_error) { /* Saved by the manager. */ }
    const url = new URL(window.location.href);
    if (url.searchParams.has("style")) {
      url.searchParams.delete("style");
      window.history.replaceState(window.history.state, "", url);
    }
    window.dispatchEvent(new Event("manager-skin-change"));
  }

  async function initialize(savePreference) {
    verifyStyles();
    // Migrate the earlier URL/localStorage opt-in into the durable preference.
    if (!failure && (requested || (!saved && remembered))) {
      await save(current, savePreference);
    }
  }

  apply(current);
  window.addEventListener("DOMContentLoaded", verifyStyles, {once: true});
  window.managerSkins = Object.freeze({
    entries, available, save, initialize,
    get current() { return current; },
    get failure() { return failure; },
  });
})();

/* Opt-in switch for the soft UI skin.
 *
 * Runs from <head> before the first paint so the skin stylesheet never applies a
 * frame late. The default product look stays untouched unless a skin is asked for:
 *   ?style=soft     -> soft skin for this load, and remembered
 *   ?style=default  -> back to the shipped look, and remembered
 *
 * Remembered choice is stored per browser profile, so an installed window can be
 * flipped once and keeps the skin. Clearing site data (or ?style=default) restores.
 */
(() => {
  "use strict";
  const KEY = "wxrebot-skin";
  const root = document.documentElement;

  function read() {
    let requested = null;
    try {
      requested = new URLSearchParams(window.location.search).get("style");
    } catch (_error) {
      requested = null;
    }
    if (requested === "soft" || requested === "default") {
      try {
        if (requested === "default") {
          window.localStorage.removeItem(KEY);
        } else {
          window.localStorage.setItem(KEY, "soft");
        }
      } catch (_error) {
        /* Storage may be unavailable; the query parameter still applies. */
      }
      return requested === "soft" ? "soft" : "";
    }
    try {
      return window.localStorage.getItem(KEY) === "soft" ? "soft" : "";
    } catch (_error) {
      return "";
    }
  }

  const skin = read();
  if (skin) {
    root.dataset.skin = skin;
  } else {
    delete root.dataset.skin;
  }
})();

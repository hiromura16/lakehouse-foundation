"use strict";

(() => {
  const key = "chaoslab-app-theme";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = null;
  try { const saved = localStorage.getItem(key); if (["light", "dark"].includes(saved)) preference = saved; } catch {}

  function apply() {
    const dark = (preference || (system.matches ? "dark" : "light")) === "dark";
    root.dataset.theme = dark ? "dark" : "light";
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = dark ? "#101722" : "#f4f6fa";
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.setAttribute("aria-pressed", String(dark));
      button.title = dark ? "ライトテーマに切り替える" : "ダークテーマに切り替える";
    }
  }

  // Apply before the stylesheet and page content load, including on a reload.
  apply();
  function connect() {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    apply();
    button.addEventListener("click", () => {
      preference = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem(key, preference); } catch {}
      apply();
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", connect, { once: true });
  else connect();
  system.addEventListener("change", () => { if (!preference) apply(); });
  window.addEventListener("storage", event => {
    if (event.key !== key && event.key !== null) return;
    preference = ["light", "dark"].includes(event.newValue) ? event.newValue : null;
    apply();
  });
})();

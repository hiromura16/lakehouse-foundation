"use strict";

const environments = [["all", "すべて", "▦"], ["foundation", "Foundation", "F"], ["vision", "Vision", "V"], ["truenas", "TrueNAS", "T"], ["shared", "共通管理", "◇"]];
const categories = [["all", "すべての用途", "▦"], ["monitoring", "監視・ログ", "◴"], ["compute", "分析・計算", "⌘"], ["automation", "処理・連携", "⇄"], ["factory", "工場データ", "⌁"], ["database", "データベース", "▤"], ["network", "ネットワーク", "◇"], ["management", "管理", "⚙"]];
const scopeNames = { public: "Cloudflare", lan: "LAN直通", tailscale: "Tailscale", local: "NAS本体のみ", internal: "内部接続" };
const envNames = { foundation: "Foundation", vision: "Vision", truenas: "TrueNAS", shared: "共通管理" };
const byId = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function browserUrl(endpoint) {
  if (!endpoint || ["local", "internal"].includes(endpoint.scope) || !["web", "api", "metrics"].includes(endpoint.kind)) return null;
  try { const url = new URL(endpoint.url); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch { return null; }
}
let catalog;
let environment = "all";
let category = "all";
let query = "";
let scope = ["apps.chaoslab.fyi", ].includes(location.hostname) ? "public" : "lan";
try { const saved = localStorage.getItem("chaoslab-app-scope"); if (["public", "lan", "tailscale"].includes(saved)) scope = saved; } catch {}
const primary = (app) => app.endpoints.find(e => e.scope === scope && e.kind === "web" && browserUrl(e))
  || app.endpoints.find(e => e.scope === scope && browserUrl(e))
  || app.endpoints.find(e => e.scope === "public" && e.kind === "web" && browserUrl(e))
  || app.endpoints.find(e => e.scope === "public" && browserUrl(e));
const visibleApps = () => catalog.apps.filter(app => (environment === "all" || app.environment === environment)
  && (category === "all" || app.category === category)
  && [app.name, app.id, app.description, envNames[app.environment], ...app.endpoints.map(e => e.url)].join(" ").toLocaleLowerCase().includes(query));

function navButton(id, name, icon, active, count, attribute) {
  return `<button type="button" class="nav-item ${active ? "active" : ""}" ${attribute}="${id}" aria-pressed="${active}"><span class="nav-icon" aria-hidden="true">${icon}</span><span class="nav-name">${escapeHtml(name)}</span><span class="nav-count">${count}</span></button>`;
}
function renderNavigation() {
  byId("environments").innerHTML = environments.map(([id, name, icon]) => navButton(id, name, icon, environment === id, id === "all" ? catalog.apps.length : catalog.apps.filter(a => a.environment === id).length, "data-environment")).join("");
  byId("categories").innerHTML = categories.filter(([id]) => id === "all" || catalog.apps.some(a => a.category === id)).map(([id, name, icon]) => navButton(id, name, icon, category === id, id === "all" ? catalog.apps.length : catalog.apps.filter(a => a.category === id).length, "data-category")).join("");
}
function renderFavorites() {
  const preferred = ["foundation-portal", "vision-portal", "grafana", "pgadmin", "prometheus", "uptime-kuma"];
  const apps = preferred.map(id => catalog.apps.find(a => a.id === id)).filter(a => a && primary(a));
  byId("favorites").innerHTML = apps.map(app => `<a class="quick-link" href="${escapeHtml(primary(app).url)}" target="_blank" rel="noopener noreferrer"><span class="quick-icon" aria-hidden="true">${escapeHtml(app.icon)}</span><span class="quick-name">${escapeHtml(app.name.replace(" · ", " "))}</span><span aria-hidden="true">↗</span></a>`).join("");
  byId("favorites-section").hidden = apps.length === 0;
}
function connectionRow(endpoint) {
  const value = escapeHtml(endpoint.url);
  return `<div class="endpoint"><div class="endpoint-heading"><span>${escapeHtml(endpoint.label)} · ${scopeNames[endpoint.scope] || endpoint.scope}</span><button type="button" class="copy" data-copy="${value}" aria-label="${escapeHtml(endpoint.label)}をコピー">コピー</button></div>${browserUrl(endpoint) ? `<a class="route-url" href="${value}" target="_blank" rel="noopener noreferrer">${value} ↗</a>` : `<code class="route-url">${value}</code>`}</div>`;
}
function appCard(app) {
  const route = primary(app);
  const selected = route || app.endpoints.find(e => e.scope === scope && !["local", "internal"].includes(e.scope));
  const relatedApp = catalog.apps.find(other => other.id === app.related);
  const related = relatedApp && primary(relatedApp);
  const date = app.checked_at || catalog.updated_at;
  const stateLabel = (app.state === "RUNNING" ? "確認時点：稼働中" : "稼働状態：" + app.state) + (date ? " · " + new Date(date).toLocaleDateString("ja-JP", { timeZone: "Asia/Tokyo" }) : "");
  const routeContent = selected ? `<div class="primary-route"><div class="route-label"><span>${escapeHtml(selected.label)}</span><strong>${scopeNames[selected.scope] || selected.scope}</strong></div><code class="route-url">${escapeHtml(selected.url)}</code></div>`
    : `<div class="no-web">${app.endpoints.some(e => browserUrl(e)) ? "この接続方法のURLは未登録です。" : "専用のWeb画面はありません。"}</div>`;
  const action = route ? `<a class="open-link" href="${escapeHtml(route.url)}" target="_blank" rel="noopener noreferrer">${route.kind === "web" ? "アプリを開く" : "接続先を開く"}<span aria-hidden="true">↗</span></a>` : '<span class="web-none">接続情報</span>';
  return `<article class="app-card environment-${escapeHtml(app.environment)}" data-app-id="${escapeHtml(app.id)}"><div class="card-heading"><span class="app-icon" aria-hidden="true">${escapeHtml(app.icon)}</span><div class="card-titles"><h3>${escapeHtml(app.name)}</h3><div class="card-meta"><span class="environment-tag">${envNames[app.environment] || "共通"}</span>${app.endpoints.some(e => e.kind === "api") ? "<span>API</span>" : ""}</div></div><span class="state ${app.state === "RUNNING" ? "" : "off"}" title="${escapeHtml(stateLabel)}" role="img" aria-label="${escapeHtml(stateLabel)}"></span></div><p class="description">${escapeHtml(app.description)}</p>${routeContent}<div class="card-action">${action}${selected ? `<button type="button" class="copy" data-copy="${escapeHtml(selected.url)}" aria-label="${escapeHtml(app.name)}の接続先をコピー">URLをコピー</button>` : ""}</div>${app.endpoints.length ? `<details class="connections"><summary>すべての接続先 · ${app.endpoints.length}</summary>${app.endpoints.map(connectionRow).join("")}</details>` : ""}${app.note ? `<p class="app-note">${escapeHtml(app.note)}</p>` : ""}${related ? `<a class="related-link" href="${escapeHtml(related.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(relatedApp.name)}を開く ↗</a>` : ""}</article>`;
}
function render() {
  renderNavigation(); renderFavorites();
  const apps = visibleApps();
  byId("apps").innerHTML = apps.map(appCard).join("");
  byId("empty").hidden = apps.length !== 0;
  const envLabel = environments.find(([id]) => id === environment)?.[1];
  byId("directory-heading").textContent = environment === "all" ? categories.find(([id]) => id === category)?.[1] || "すべてのアプリ" : envLabel;
  byId("result-count").textContent = `${apps.length}件`;
  document.querySelectorAll("[data-scope]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.scope === scope)));
  byId("scope-hint").textContent = { public: "メール認証後に各アプリへ接続します。", lan: "LAN URLを優先。未登録の画面はCloudflareへ接続します。", tailscale: "Tailscale URLを優先。未登録の画面はCloudflareへ接続します。" }[scope];
  byId("nas-link").href = scope === "public" ? catalog.nas_public_url : `https://${scope === "tailscale" ? catalog.tailscale_host : catalog.lan_host}/`;
  byId("lan-guide").href = catalog.lakehouse_lan_guide_url;
}
function toast(message) {
  byId("toast").textContent = message; byId("toast").hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { byId("toast").hidden = true; }, 2300);
}
async function copyText(value) {
  try {
    if (window.isSecureContext && navigator.clipboard) await navigator.clipboard.writeText(value);
    else {
      const field = document.createElement("textarea"); field.value = value; field.className = "clipboard-fallback"; field.setAttribute("readonly", ""); document.body.appendChild(field); field.select();
      let copied; try { copied = document.execCommand("copy"); } finally { field.remove(); }
      if (!copied) throw new Error("Clipboard unavailable");
    }
    toast("接続先をコピーしました");
  } catch { toast("コピーできませんでした。URLを選択してコピーしてください。"); }
}
document.addEventListener("click", event => {
  const button = event.target.closest("button"); if (!button) return;
  if (button.dataset.copy) void copyText(button.dataset.copy);
  if (button.dataset.scope) { scope = button.dataset.scope; try { localStorage.setItem("chaoslab-app-scope", scope); } catch {} if (catalog) render(); }
  if (button.dataset.environment) { environment = button.dataset.environment; category = "all"; if (catalog) render(); }
  if (button.dataset.category) { category = button.dataset.category; environment = "all"; if (catalog) render(); }
  if (button.id === "reset") { environment = "all"; category = "all"; query = ""; byId("search").value = ""; if (catalog) render(); }
});
byId("search").addEventListener("input", event => { query = event.target.value.trim().toLocaleLowerCase(); if (catalog) render(); });
document.addEventListener("keydown", event => { if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) { event.preventDefault(); byId("search").focus(); } });
byId("export").addEventListener("click", () => {
  const rows = [["アプリ名", "登録名", "環境", "用途", "接続方法", "種類", "URL・接続先", "状態", "状態の確認日時"]];
  for (const app of visibleApps()) for (const endpoint of app.endpoints.length ? app.endpoints : [{ scope: "", label: "専用Web画面なし", url: "" }]) rows.push([app.name, app.id, envNames[app.environment], app.description, scopeNames[endpoint.scope] || "", endpoint.label, endpoint.url, app.state, app.checked_at || catalog.updated_at]);
  const cell = value => '"' + String(/^[=+@-]/.test(value) ? "'" + value : value).replace(/"/g, '""') + '"';
  const blob = new Blob(["\ufeff" + rows.map(row => row.map(cell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = "chaoslab-apps.csv"; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
(async () => {
  try {
    const response = await fetch("/catalog.json", { cache: "no-store" }); if (!response.ok) throw new Error("Catalog unavailable"); catalog = await response.json();
    if (!Array.isArray(catalog.apps)) throw new Error("Invalid catalog");
    const web = catalog.apps.filter(app => app.endpoints.some(e => browserUrl(e))).length;
    byId("summary").innerHTML = `<span><strong>${catalog.apps.length}</strong> 登録</span><span><strong>${web}</strong> Web・API入口</span>`;
    const updated = new Intl.DateTimeFormat("ja-JP", { timeZone: "Asia/Tokyo", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(catalog.updated_at));
    byId("updated").textContent = `一覧更新 ${updated} JST · 稼働状態は各アプリの確認時点の情報です。`;
    byId("export").disabled = false; render();
  } catch {
    byId("summary").textContent = "一覧を読み込めませんでした。";
    byId("apps").textContent = "ページを再読み込みしてください。";
  }
})();

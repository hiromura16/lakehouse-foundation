import html from "./site/index.html";
import css from "./site/style.css";
import theme from "./site/theme.js";
import javascript from "./site/app.js";
import catalog from "./site/catalog.json";

const files = Object.freeze({
  "/": [html, "text/html; charset=utf-8"],
  "/index.html": [html, "text/html; charset=utf-8"],
  "/style.css": [css, "text/css; charset=utf-8"],
  "/app.js": [javascript, "application/javascript; charset=utf-8"],
  "/theme.js": [theme, "application/javascript; charset=utf-8"],
  "/catalog.json": [catalog, "application/json; charset=utf-8"],
});

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (!["GET", "HEAD"].includes(request.method)) return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
    if (url.hostname !== "apps.chaoslab.fyi") return new Response("Not found", { status: 404 });
    const file = files[url.pathname];
    if (!file) return new Response("Not found", { status: 404 });
    return new Response(request.method === "HEAD" ? null : file[0], {
      headers: {
        "Content-Type": file[1], "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'none'",
      },
    });
  },
};

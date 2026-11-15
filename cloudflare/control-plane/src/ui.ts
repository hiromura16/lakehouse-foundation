// 静的ファイルも同じWorkerから配信し、すべてAccess認証を通す。
import html from "./ui/index.html?raw";
import css from "./ui/style.css?raw";
import client from "./ui/client.js?raw";

export { html, css, client };

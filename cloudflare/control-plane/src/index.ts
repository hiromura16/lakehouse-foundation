import { authenticate } from "./auth";
import { contractSchema, sourceSchema, sourceUpdateSchema } from "./schema";
import {
  listAudit,
  listContracts,
  listSources,
  saveContract,
  saveSource,
} from "./store";
import { html, css, client } from "./ui";

const MAX_BODY_BYTES = 32768;
class HttpError extends Error {
  constructor(
    public status: number,
    public code: string,
    public message: string,
  ) {
    super(message);
  }
}

/** Content-Lengthだけに依存せず、チャンク読み込み中も上限を強制する。 */
async function readJson(request: Request): Promise<unknown> {
  if (
    request.headers.get("content-type")?.split(";")[0]?.trim() !==
    "application/json"
  ) {
    throw new HttpError(415, "json_required", "JSON形式で送信してください。");
  }
  if (!request.body)
    throw new HttpError(400, "body_required", "入力がありません。");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) {
        await reader.cancel();
        throw new HttpError(
          413,
          "body_too_large",
          "入力は32KiB以内にしてください。",
        );
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    try {
      return JSON.parse(
        new TextDecoder("utf-8", { fatal: true }).decode(bytes),
      );
    } catch {
      throw new HttpError(400, "invalid_json", "JSONを読み取れません。");
    }
  } finally {
    reader.releaseLock();
  }
}
function json(value: unknown, status = 200) {
  return Response.json(value, { status });
}
async function route(
  request: Request,
  env: Env,
  actor: string,
  requestId: string,
): Promise<Response> {
  const path = new URL(request.url).pathname;
  if (request.method === "GET") {
    if (path === "/")
      return new Response(html, {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    if (path === "/app.css")
      return new Response(css, {
        headers: { "content-type": "text/css; charset=utf-8" },
      });
    if (path === "/app.js")
      return new Response(client, {
        headers: { "content-type": "text/javascript; charset=utf-8" },
      });
    if (path === "/api/sources")
      return json({ items: await listSources(env.CONTROL_DB) });
    if (path === "/api/contracts")
      return json({ items: await listContracts(env.CONTROL_DB) });
    if (path === "/api/audit")
      return json({ items: await listAudit(env.CONTROL_DB) });
    if (path === "/api/status") {
      await env.CONTROL_DB.prepare(
        "SELECT revision FROM sources LIMIT 1",
      ).all();
      return json({
        controlPlane: "ready",
        metadataStore: "ready",
        dataPlane: "not_enabled",
        contractApplication: "not_implemented",
        billingChanges: false,
      });
    }
  }
  if (
    (request.method === "POST" &&
      ["/api/sources", "/api/contracts"].includes(path)) ||
    (request.method === "PUT" && path.startsWith("/api/sources/"))
  ) {
    if (
      request.headers.get("origin") !== env.PUBLIC_ORIGIN ||
      request.headers.get("X-Lakehouse-Request") !== "1"
    ) {
      throw new HttpError(
        403,
        "same_origin_required",
        "同じ管理画面から操作してください。",
      );
    }
    const body = await readJson(request);
    const schema =
      path === "/api/contracts"
        ? contractSchema
        : request.method === "PUT"
          ? sourceUpdateSchema
          : sourceSchema;
    const parsed = schema.safeParse(body);
    if (!parsed.success)
      throw new HttpError(
        422,
        "invalid_definition",
        "論理識別子・必須項目・列の重複・主キー・入力上限を確認してください。接続先や認証情報は登録できません。",
      );
    const audit = { actor, requestId, now: new Date().toISOString() };
    let saved: boolean;
    if (path === "/api/contracts") {
      saved = await saveContract(
        env.CONTROL_DB,
        contractSchema.parse(body),
        audit,
      );
    } else if (request.method === "PUT") {
      const value = sourceUpdateSchema.parse(body);
      if (path !== `/api/sources/${value.id}`)
        throw new HttpError(422, "id_mismatch", "識別子が一致しません。");
      saved = await saveSource(
        env.CONTROL_DB,
        value,
        audit,
        value.expectedRevision,
      );
    } else {
      saved = await saveSource(env.CONTROL_DB, sourceSchema.parse(body), audit);
    }
    if (!saved)
      throw new HttpError(
        409,
        "definition_conflict",
        "他の変更・識別子の重複・上限・ソース状態を確認し、再読込してください。",
      );
    return json(
      { saved: true, requestId, applied: false },
      request.method === "POST" ? 201 : 200,
    );
  }
  throw new HttpError(404, "not_found", "ページまたは操作が見つかりません。");
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const requestId = crypto.randomUUID();
    let response: Response;
    try {
      if (new URL(request.url).origin !== env.PUBLIC_ORIGIN) {
        throw new HttpError(
          403,
          "origin_not_configured",
          "このホストでは利用できません。",
        );
      }
      let actor: string;
      try {
        actor = await authenticate(request, env);
      } catch {
        throw new HttpError(
          401,
          "access_required",
          "Cloudflare Accessへのログインが必要です。",
        );
      }
      response = await route(request, env, actor, requestId);
    } catch (error) {
      const known = error instanceof HttpError;
      response = json(
        {
          error: known ? error.code : "internal_error",
          message: known
            ? error.message
            : "処理に失敗しました。操作IDを添えて管理者に連絡してください。",
          requestId,
        },
        known ? error.status : 500,
      );
    }
    // パス・入力・JWT・メールをログに含めない。
    if (response.status >= 400 || request.method !== "GET") {
      console.log(
        JSON.stringify({
          event: "control_request",
          requestId,
          method: request.method,
          status: response.status,
        }),
      );
    }
    const headers = new Headers(response.headers);
    headers.set("Cache-Control", "no-store");
    headers.set(
      "Content-Security-Policy",
      "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    );
    headers.set("X-Content-Type-Options", "nosniff");
    headers.set("Referrer-Policy", "no-referrer");
    headers.set(
      "Permissions-Policy",
      "camera=(), microphone=(), geolocation=()",
    );
    headers.set("X-Request-Id", requestId);
    return new Response(response.body, { status: response.status, headers });
  },
} satisfies ExportedHandler<Env>;

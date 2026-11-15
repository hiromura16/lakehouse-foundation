import { createLocalJWKSet, jwtVerify } from "jose";

/** Accessの署名・発行元・対象アプリ・期限を確認し、監査用の匿名識別子を返す。 */
export async function authenticate(
  request: Request,
  env: Env,
): Promise<string> {
  const issuer = env.ACCESS_TEAM_DOMAIN;
  if (
    !/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(issuer) ||
    !env.ACCESS_AUD
  ) {
    throw new Error("access_configuration");
  }
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token || token.length > 16384) throw new Error("access_required");
  // 宛先は配備設定の固定値。受信ヘッダーやJWT内のURLへ通信しない。
  const response = await fetch(`${issuer}/cdn-cgi/access/certs`, {
    redirect: "error",
    signal: AbortSignal.timeout(4000),
    cf: { cacheEverything: true, cacheTtl: 300 },
  });
  if (!response.ok) throw new Error("access_keys_unavailable");
  if (!response.body) throw new Error("access_keys_invalid");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 65536) {
        await reader.cancel();
        throw new Error("access_keys_invalid");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  const raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  const keys = createLocalJWKSet(JSON.parse(raw));
  const { payload } = await jwtVerify(token, keys, {
    issuer,
    audience: env.ACCESS_AUD,
    algorithms: ["RS256"],
    requiredClaims: ["sub", "iat", "exp"],
    clockTolerance: 5,
  });
  if (!payload.sub || payload.type !== "app")
    throw new Error("access_identity_invalid");
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(`${issuer}:${payload.sub}`),
  );
  return Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
}

import { env } from "cloudflare:workers";
import { applyD1Migrations, reset } from "cloudflare:test";
import type { D1Migration } from "@cloudflare/vitest-plugin";
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import worker from "../src/index";

const testEnv = env as Env & { TEST_MIGRATIONS: D1Migration[] };
const origin = "https://control.example.invalid";
let pair: Awaited<ReturnType<typeof generateKeyPair>>;
let keys: { keys: object[] };
let jwt: string;
const source = {
  id: "synthetic-source",
  displayName: "合成メタデータ",
  kind: "sqlserver",
  ownerTeam: "demo-team",
  status: "draft",
};
const contract = {
  id: "synthetic-contract",
  sourceId: source.id,
  datasetId: "synthetic-events",
  columns: [
    {
      name: "event_id",
      dataType: "string",
      classification: "internal",
      primaryKey: true,
    },
  ],
  freshnessMinutes: 60,
  deletePolicy: "retain-tombstone",
  expectedRevision: 0,
};

async function sign(
  options: {
    audience?: string;
    issuer?: string;
    expired?: boolean;
    type?: string;
  } = {},
) {
  return new SignJWT({ type: options.type ?? "app" })
    .setProtectedHeader({ alg: "RS256", kid: "synthetic-key" })
    .setSubject("synthetic-user")
    .setIssuedAt()
    .setIssuer(options.issuer ?? env.ACCESS_TEAM_DOMAIN)
    .setAudience(options.audience ?? env.ACCESS_AUD)
    .setExpirationTime(
      options.expired ? Math.floor(Date.now() / 1000) - 60 : "5m",
    )
    .sign(pair.privateKey);
}
async function request(
  path: string,
  method = "GET",
  body?: unknown,
  headers: Record<string, string> = {},
) {
  return worker.fetch(
    new Request(origin + path, {
      method,
      headers: {
        "Cf-Access-Jwt-Assertion": jwt,
        Origin: origin,
        "X-Lakehouse-Request": "1",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    env,
  );
}
async function rowCount(table: "sources" | "contracts" | "audit_events") {
  return (
    await env.CONTROL_DB.prepare(`SELECT count(*) AS n FROM ${table}`).first<{
      n: number;
    }>()
  )?.n;
}
beforeAll(async () => {
  pair = await generateKeyPair("RS256", { extractable: true });
  keys = {
    keys: [
      {
        ...(await exportJWK(pair.publicKey)),
        alg: "RS256",
        kid: "synthetic-key",
        use: "sig",
      },
    ],
  };
  jwt = await sign();
});
beforeEach(async () => {
  await reset();
  await applyD1Migrations(env.CONTROL_DB, testEnv.TEST_MIGRATIONS);
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    expect(String(input)).toBe(
      env.ACCESS_TEAM_DOMAIN + "/cdn-cgi/access/certs",
    );
    return Response.json(keys);
  });
  vi.spyOn(console, "log").mockImplementation(() => undefined);
});
afterEach(() => vi.restoreAllMocks());

describe("Accessの検証と同一オリジン制限", () => {
  it("JWTなし・メールヘッダーだけの要求を拒否し、D1を変更しない", async () => {
    const response = await request("/api/sources", "POST", source, {
      "Cf-Access-Jwt-Assertion": "",
      "Cf-Access-Authenticated-User-Email": "synthetic@example.invalid",
    });
    expect(response.status).toBe(401);
    expect(await rowCount("sources")).toBe(0);
    expect(await rowCount("audit_events")).toBe(0);
  });
  it.each(["audience", "issuer", "expired", "type"])(
    "%sの異なるJWTを拒否する",
    async (field) => {
      const options =
        field === "expired"
          ? { expired: true }
          : { [field]: "synthetic-other" };
      expect(
        (
          await request("/api/status", "GET", undefined, {
            "Cf-Access-Jwt-Assertion": await sign(options),
          })
        ).status,
      ).toBe(401);
    },
  );
  it("別の鍵で偽造された署名を拒否する", async () => {
    const other = await generateKeyPair("RS256");
    const forged = await new SignJWT({ type: "app" })
      .setProtectedHeader({ alg: "RS256", kid: "synthetic-key" })
      .setSubject("synthetic-user")
      .setIssuedAt()
      .setExpirationTime("5m")
      .setIssuer(env.ACCESS_TEAM_DOMAIN)
      .setAudience(env.ACCESS_AUD)
      .sign(other.privateKey);
    expect(
      (
        await request("/api/sources", "POST", source, {
          "Cf-Access-Jwt-Assertion": forged,
        })
      ).status,
    ).toBe(401);
    expect(await rowCount("sources")).toBe(0);
  });
  it("公開鍵本文が上限を超えた場合に認証を通さない", async () => {
    vi.mocked(fetch).mockImplementation(
      async () => new Response("x".repeat(65537)),
    );
    expect((await request("/api/status")).status).toBe(401);
  });
  it("公開鍵取得の失敗時に認証を通さない", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("synthetic-failure"));
    expect((await request("/api/status")).status).toBe(401);
  });
  it.each<Record<string, string>>([
    { Origin: "https://example.invalid" },
    { Origin: "" },
    { "X-Lakehouse-Request": "" },
  ])("別オリジンや操作ヘッダーなしの更新を拒否する", async (headers) => {
    expect(
      (await request("/api/sources", "POST", source, headers)).status,
    ).toBe(403);
    expect(await rowCount("sources")).toBe(0);
  });
  it("別ホストとOPTIONSを許可しない", async () => {
    expect(
      (await worker.fetch(new Request("https://example.invalid/"), env)).status,
    ).toBe(403);
    expect((await request("/api/sources", "OPTIONS")).status).toBe(404);
  });
});
describe("実際のD1による管理データの保存", () => {
  it("登録・一覧・版比較更新と匿名監査記録を同時に保存する", async () => {
    expect((await request("/api/sources", "POST", source)).status).toBe(201);
    const items = (
      (await (await request("/api/sources")).json()) as {
        items: { revision: number }[];
      }
    ).items;
    expect(items[0]?.revision).toBe(1);
    expect(
      (
        await request("/api/sources/" + source.id, "PUT", {
          ...source,
          displayName: "更新済み定義",
          expectedRevision: 1,
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await request("/api/sources/" + source.id, "PUT", {
          ...source,
          expectedRevision: 1,
        })
      ).status,
    ).toBe(409);
    expect(await rowCount("audit_events")).toBe(2);
    const audit = await env.CONTROL_DB.prepare(
      "SELECT * FROM audit_events ORDER BY sequence",
    ).all<{ actor_hash: string }>();
    expect(audit.results[0]?.actor_hash).toMatch(/^[a-f0-9]{64}$/);
    const response = await request("/api/audit");
    expect(await response.text()).not.toContain("actor_hash");
  });
  it("契約を下書きとして追記し、過去版とソース参照を維持する", async () => {
    await request("/api/sources", "POST", source);
    expect((await request("/api/contracts", "POST", contract)).status).toBe(
      201,
    );
    expect(
      (
        await request("/api/contracts", "POST", {
          ...contract,
          freshnessMinutes: 30,
          expectedRevision: 1,
        })
      ).status,
    ).toBe(201);
    expect(
      (
        await request("/api/contracts", "POST", {
          ...contract,
          expectedRevision: 1,
        })
      ).status,
    ).toBe(409);
    expect(
      (
        await request("/api/contracts", "POST", {
          ...contract,
          datasetId: "another-dataset",
          expectedRevision: 2,
        })
      ).status,
    ).toBe(409);
    const result = (await (await request("/api/contracts")).json()) as {
      items: { revision: number; status: string; freshnessMinutes: number }[];
    };
    expect(result.items).toHaveLength(1);
    expect(result.items[0]).toMatchObject({
      revision: 2,
      status: "draft",
      freshnessMinutes: 30,
    });
    expect(await rowCount("contracts")).toBe(2);
    expect(await rowCount("audit_events")).toBe(3);
  });
  it("同じ版を元にした並行更新は1件だけ成功する", async () => {
    await request("/api/sources", "POST", source);
    const results = await Promise.all([
      request("/api/contracts", "POST", contract),
      request("/api/contracts", "POST", contract),
    ]);
    expect(results.map((result) => result.status).sort()).toEqual([201, 409]);
    expect(await rowCount("contracts")).toBe(1);
  });
  it("存在しないソースや保留ソースの契約を保存しない", async () => {
    expect((await request("/api/contracts", "POST", contract)).status).toBe(
      409,
    );
    await request("/api/sources", "POST", { ...source, status: "paused" });
    expect((await request("/api/contracts", "POST", contract)).status).toBe(
      409,
    );
  });
  it("監査の保存に失敗したらソース変更もロールバックする", async () => {
    await env.CONTROL_DB.prepare(
      "CREATE TRIGGER synthetic_audit_failure BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT, 'synthetic'); END",
    ).run();
    const response = await request("/api/sources", "POST", source);
    expect(response.status).toBe(500);
    expect(await rowCount("sources")).toBe(0);
    expect(await response.text()).not.toContain("synthetic_audit_failure");
  });
  it("ソース100件と履歴1000件の上限を守る", async () => {
    await env.CONTROL_DB.prepare(
      `WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100)
      INSERT INTO sources SELECT 'source-'||x,'合成定義','file','demo-team','draft',1,'2026-11-15','synthetic','synthetic' FROM n`,
    ).run();
    expect((await request("/api/sources", "POST", source)).status).toBe(409);
    await env.CONTROL_DB.prepare(
      `WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1005)
      INSERT INTO audit_events(action,target_id,revision,actor_hash,request_id,created_at)
      SELECT 'synthetic','synthetic',1,'synthetic','synthetic','2026-11-15' FROM n`,
    ).run();
    expect(await rowCount("audit_events")).toBe(1000);
  });
});
describe("入力上限と画面の安全な配信", () => {
  it.each([
    { ...source, host: "synthetic.invalid" },
    { ...source, id: "https://example.invalid" },
    { ...source, displayName: "synthetic.invalid" },
    { ...source, password: "synthetic-only" },
  ])("接続項目や論理識別子でない入力を拒否する", async (body) => {
    expect((await request("/api/sources", "POST", body)).status).toBe(422);
    expect(await rowCount("sources")).toBe(0);
  });
  it.each([
    { ...contract, columns: [] },
    { ...contract, columns: [{ ...contract.columns[0], primaryKey: false }] },
    { ...contract, columns: [contract.columns[0], contract.columns[0]] },
    { ...contract, freshnessMinutes: 0 },
  ])("不正な列定義と鮮度条件を拒否する", async (body) => {
    expect((await request("/api/contracts", "POST", body)).status).toBe(422);
  });
  it("32KiB超過とJSON以外を拒否する", async () => {
    expect(
      (
        await request("/api/sources", "POST", {
          ...source,
          displayName: "x".repeat(32768),
        })
      ).status,
    ).toBe(413);
    expect(
      (
        await request("/api/sources", "POST", source, {
          "Content-Type": "text/plain",
        })
      ).status,
    ).toBe(415);
  });
  it("日本語画面・CSP・キャッシュ無効と正直な稼働状態を返す", async () => {
    const response = await request("/");
    expect(response.status).toBe(200);
    expect(await response.text()).toContain('lang="ja"');
    expect(response.headers.get("Content-Security-Policy")).toContain(
      "frame-ancestors 'none'",
    );
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(await (await request("/api/status")).json()).toMatchObject({
      dataPlane: "not_enabled",
      billingChanges: false,
    });
    expect((await request("/app.js")).status).toBe(200);
    expect((await request("/app.css")).status).toBe(200);
  });
});

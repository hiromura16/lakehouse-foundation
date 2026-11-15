import { bindings, defineConfig } from "cf/config";
import * as entrypoint from "./src/index.ts" with { type: "cf-worker" };

// 実環境の識別子は公開せず、配備時に専用の値を注入する。
export default defineConfig({
  worker: {
    name: "lakehouse-vision-control",
    entrypoint,
    compatibilityDate: "2026-11-15",
    workersDev: false,
    env: {
      CONTROL_DB: bindings.d1({ name: "lakehouse-vision-control" }),
      ACCESS_TEAM_DOMAIN: bindings.text(
        "https://example.cloudflareaccess.com" as string,
      ),
      ACCESS_AUD: bindings.text("local-test-audience" as string),
      PUBLIC_ORIGIN: bindings.text("https://control.example.invalid" as string),
    },
    observability: {
      enabled: true,
      redactQueryString: true,
      logs: { enabled: true, headSamplingRate: 0.1, invocationLogs: false },
      traces: { enabled: true, headSamplingRate: 0.01 },
    },
  },
});

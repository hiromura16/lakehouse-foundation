import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "browser.spec.ts",
  workers: 1,
  use: { headless: true, browserName: "chromium" },
  reporter: "list",
});

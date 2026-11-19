import { defineConfig } from '@playwright/test';

/** Explicit opt-in UI contract checks. No live API, credentials, or database. */
export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  timeout: 20_000,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    browserName: 'chromium',
    // Never disable the Chromium sandbox to make a restricted runner pass.
    launchOptions: { chromiumSandbox: true },
    trace: 'off',
  },
  webServer: {
    command: 'npm exec vite preview -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
    timeout: 10_000,
  },
});

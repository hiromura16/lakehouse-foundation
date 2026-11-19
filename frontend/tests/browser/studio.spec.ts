/** Prepared synthetic browser acceptance. Runtime execution is a separate gate. */
import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  let authenticated = false;
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/session') {
      if (request.method() === 'POST') authenticated = true;
      if (request.method() === 'DELETE') authenticated = false;
      await route.fulfill({ json: { authenticated } });
      return;
    }
    if (path === '/api/control/services') {
      await route.fulfill({
        json: {
          pending_restart: false,
          services: [
            {
              id: 'grafana',
              title: 'Grafana',
              description: 'Synthetic internal health',
              profile: 'monitoring',
              profiles: ['monitoring'],
              enabled: true,
              configured: true,
              state: 'available',
              url: '',
              link_mode: 'public',
              link_configured: false,
              missing_settings: [],
            },
          ],
        },
      });
      return;
    }
    if (path === '/api/control/catalog') {
      await route.fulfill({ json: { version: 1, datasets: [] } });
      return;
    }
    if (path === '/api/control/jobs') {
      await route.fulfill({ json: { jobs: [], persistent: false, history_limit: 20 } });
      return;
    }
    // An unexpected operation must fail rather than reaching any real API.
    await route.fulfill({ status: 503, json: { code: 'SYNTHETIC_NOT_CONFIGURED' } });
  });
});

async function connect(page: import('@playwright/test').Page) {
  await page.goto('/');
  await page.getByLabel('接続token', { exact: true }).fill('synthetic'.repeat(6));
  await page.getByRole('button', { name: '接続する', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'データの現在地を、ひと目で。' })).toBeVisible();
}

test('owner connection clears the token without browser storage', async ({ page }) => {
  await connect(page);
  expect(
    await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })),
  ).toEqual({ local: 0, session: 0 });
  await page.getByRole('button', { name: 'sessionを終了', exact: true }).click();
  await expect(page.getByLabel('接続token', { exact: true })).toHaveValue('');
});

test('public cards with no configured origin do not produce localhost anchors', async ({
  page,
}) => {
  await connect(page);
  await page.getByRole('button', { name: 'サービス', exact: true }).click();
  await expect(page.getByText('外部URL未設定', { exact: true }).first()).toBeVisible();
  await expect(page.locator('a[href*="localhost"]')).toHaveCount(0);
  await expect(page.locator('a[href*="127.0.0.1"]')).toHaveCount(0);
});

test('metadata page explains that a review memo is not database application', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: 'データ連携を組み立てる', exact: true }).first().click();
  await expect(page.getByRole('heading', { name: 'データ連携を、組み立てる。' })).toBeVisible();
  await expect(page.locator('button').filter({ hasText: /^適用$/ })).toHaveCount(0);
});

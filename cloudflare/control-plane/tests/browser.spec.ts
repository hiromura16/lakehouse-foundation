import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

// 画面操作のみを合成APIで検証する。実D1・JWTの試験はcontrol.test.tsが担当する。
test("日本語画面でソース登録、契約の版追加、再読込ができる", async ({
  page,
}) => {
  const sources: Record<string, unknown>[] = [];
  const contracts: Record<string, unknown>[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("https://control.example.invalid/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const files: Record<string, [string, string]> = {
      "/": ["index.html", "text/html"],
      "/app.css": ["style.css", "text/css"],
      "/app.js": ["client.js", "text/javascript"],
    };
    const file = files[path];
    if (file) {
      return route.fulfill({
        contentType: file[1],
        body: readFileSync(
          new URL("../src/ui/" + file[0], import.meta.url),
          "utf8",
        ),
      });
    }
    if (request.method() === "POST") {
      expect(request.headers()["x-lakehouse-request"]).toBe("1");
      const body = request.postDataJSON();
      if (path === "/api/sources") sources.push({ ...body, revision: 1 });
      if (path === "/api/contracts") {
        const index = contracts.findIndex((item) => item.id === body.id);
        const value = {
          ...body,
          revision: body.expectedRevision + 1,
          status: "draft",
        };
        if (index < 0) contracts.push(value);
        else contracts[index] = value;
      }
      return route.fulfill({
        status: 201,
        json: { saved: true, applied: false },
      });
    }
    const values: Record<string, unknown> = {
      "/api/sources": { items: sources },
      "/api/contracts": { items: contracts },
      "/api/audit": { items: [] },
      "/api/status": {
        controlPlane: "ready",
        metadataStore: "ready",
        dataPlane: "not_enabled",
      },
    };
    return route.fulfill({ json: values[path] ?? {} });
  });
  await page.goto("https://control.example.invalid/");
  await expect(
    page.getByText("まだソースがありません。", { exact: false }),
  ).toBeVisible();
  const sourceForm = page.locator("#source-form");
  await sourceForm.getByLabel("論理識別子").fill("synthetic-source");
  await sourceForm.getByLabel("表示名").fill("合成ソース");
  await sourceForm.getByLabel("担当チームの識別子").fill("demo-team");
  await sourceForm
    .getByRole("button", { name: "下書きを保存", exact: true })
    .click();
  await expect(
    page.locator("#source-list").getByText("合成ソース", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "データ契約", exact: true }).click();
  const form = page.locator("#contract-form");
  await form.getByLabel("契約の識別子").fill("synthetic-contract");
  await form.getByLabel("データセットの識別子").fill("synthetic-events");
  await form.getByLabel("列の論理名").fill("event_id");
  await form.getByLabel("主キーに含める").check();
  await form.getByRole("button", { name: "新しい版の下書きを保存" }).click();
  await expect(page.locator("#contract-list")).toContainText("第1版");
  await page.getByRole("button", { name: "次の版を作成" }).click();
  await form.getByLabel("鮮度の目標（分）").fill("30");
  await form.getByRole("button", { name: "新しい版の下書きを保存" }).click();
  await expect(page.locator("#contract-list")).toContainText("第2版");
  await page.reload();
  await page.getByRole("button", { name: "データ契約", exact: true }).click();
  await expect(page.locator("#contract-list")).toContainText("鮮度 30分");
  await page.getByRole("button", { name: "稼働状況・履歴" }).click();
  await expect(page.locator("#service-status")).toContainText("未有効化");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

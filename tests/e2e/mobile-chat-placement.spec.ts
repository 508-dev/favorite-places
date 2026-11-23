import { expect, test } from "@playwright/test";

test.use({ isMobile: true, hasTouch: true });

for (const viewport of [
  { width: 360, height: 740 },
  { width: 390, height: 844 },
  { width: 430, height: 932 },
  { width: 844, height: 390 },
]) {
  test(`chat stays clear of mobile corners at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    for (const path of ["/", "/guides/tokyo-japan/"]) {
      await page.goto(path);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        viewport.width,
      );
      const toggle = page.locator("[data-place-chat-toggle]");
      const box = (await toggle.boundingBox())!;
      expect(box.height).toBeCloseTo(48, 0);
      expect(viewport.width - box.x - box.width).toBeCloseTo(20, 0);
      expect(viewport.height - box.y - box.height).toBeCloseTo(8, 0);
      await toggle.tap();
      const panel = (await page.locator("[data-place-chat-panel]").boundingBox())!;
      expect(panel.x).toBeGreaterThanOrEqual(19);
      expect(viewport.width - panel.x - panel.width).toBeGreaterThanOrEqual(19);
      expect(panel.y).toBeGreaterThanOrEqual(0);
    }
  });
}

test("chat adds safe-area insets and keeps its panel inside the usable viewport", async ({
  page,
  context,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const session = await context.newCDPSession(page);
  await session.send("Emulation.setSafeAreaInsetsOverride", {
    insets: { top: 44, bottom: 34, left: 6, right: 6 },
  });
  await page.goto("/guides/tokyo-japan/");
  const toggle = page.locator("[data-place-chat-toggle]");
  const box = (await toggle.boundingBox())!;
  expect(390 - box.x - box.width).toBeCloseTo(26, 0);
  expect(844 - box.y - box.height).toBeCloseTo(42, 0);
  if ((await page.locator("[data-map-panel]").getAttribute("data-collapsed")) !== "true") {
    await page.locator("[data-map-toggle]").tap();
  }
  await expect(page.locator(".floating-scroll-controls")).toHaveAttribute("data-anchor", "right");
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const topButton = page.getByRole("button", { name: "Scroll to top" });
  await expect(topButton).toBeVisible();
  const topBox = (await topButton.boundingBox())!;
  expect(box.y - topBox.y - topBox.height).toBeGreaterThanOrEqual(12);
  expect(390 - topBox.x - topBox.width).toBeCloseTo(26, 0);
  await page.screenshot({ path: ".context/qa/mobile-chat-scroll-controls.png" });
  await topButton.tap();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThan(80);
  await page.locator("[data-map-toggle]").tap();
  await expect(page.locator(".floating-scroll-controls")).toHaveAttribute("data-anchor", "left");
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(topButton).toBeVisible();
  const leftTopBox = (await topButton.boundingBox())!;
  expect(leftTopBox.height).toBeCloseTo(box.height, 0);
  expect(leftTopBox.x).toBeCloseTo(26, 0);
  expect(leftTopBox.y + leftTopBox.height).toBeCloseTo(box.y + box.height, 0);
  await page.screenshot({ path: ".context/qa/mobile-chat-aligned-controls.png" });
  await topButton.tap();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThan(80);
  await toggle.tap();
  await page.locator("[data-place-chat]").evaluate((element) => {
    (element as HTMLElement).dataset.hasResults = "true";
  });
  const panel = (await page.locator("[data-place-chat-panel]").boundingBox())!;
  expect(panel.x).toBeGreaterThanOrEqual(25);
  expect(390 - panel.x - panel.width).toBeGreaterThanOrEqual(25);
  expect(panel.y).toBeGreaterThanOrEqual(44);
  await page.screenshot({ path: ".context/qa/mobile-chat-safe-area.png" });
});

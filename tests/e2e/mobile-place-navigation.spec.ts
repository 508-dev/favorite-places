import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

test("mobile place links keep the card title visible after the keyboard viewport settles", async ({
  page,
}) => {
  await page.addInitScript(() => {
    document.addEventListener("DOMContentLoaded", () => {
      const style = document.createElement("style");
      style.textContent = '[data-search-highlight="true"] { min-height: 1200px; }';
      document.head.append(style);
    });
  });
  await page.goto("/guides/taipei-taiwan/");
  const id = await page.locator("[data-place-card]").last().getAttribute("data-place-id");
  await page.goto(`/guides/taipei-taiwan/?place=${encodeURIComponent(id || "")}`, {
    waitUntil: "domcontentloaded",
  });
  const target = page.locator('[data-place-card][data-search-highlight="true"]');
  await expect(target).toBeFocused();
  // Model keyboard dismissal interrupting the first scroll on Android Chrome.
  await page.setViewportSize({ width: 390, height: 530 });
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => (await target.boundingBox())?.y).toBeGreaterThanOrEqual(-1);
  await expect.poll(async () => (await target.boundingBox())?.y).toBeLessThan(24);
  await expect(target).toBeFocused();
});

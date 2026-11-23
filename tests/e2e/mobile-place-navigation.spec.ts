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

test("touching a guide chat result closes chat and reveals its card without opening the mobile map", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("favoritePlacesMapCollapsedMobile", "true"));
  await page.goto("/guides/taipei-taiwan/");
  const id = await page.locator("[data-place-card]").last().getAttribute("data-place-id");
  await page.route("**/api/chat", (route) =>
    route.fulfill({
      json: {
        reply: "Try this place.",
        places: [
          {
            id,
            name: "Recommended place",
            url: `/guides/taipei-taiwan/?place=${encodeURIComponent(id || "")}`,
          },
        ],
      },
    }),
  );
  await page.evaluate(() => {
    (window as unknown as { navigationMarker: boolean }).navigationMarker = true;
  });
  await page.locator("[data-place-chat-toggle]").tap();
  await page.locator("[data-place-chat-input]").fill("Find me a place");
  await page.locator("[data-place-chat-submit]").tap();
  await page.locator(".place-chat-card").tap();
  await expect(page.locator("[data-place-chat-panel]")).toBeHidden();
  await expect(page.locator("[data-map-panel]")).toHaveAttribute("data-collapsed", "true");
  const target = page.locator('[data-place-card][data-search-highlight="true"]');
  await expect(target).toHaveAttribute("data-place-id", id || "");
  await expect(target).toBeFocused();
  await expect.poll(async () => (await target.boundingBox())?.y).toBeGreaterThanOrEqual(-1);
  await expect.poll(async () => (await target.boundingBox())?.y).toBeLessThan(24);
  expect(
    await page.evaluate(
      () => (window as unknown as { navigationMarker: boolean }).navigationMarker,
    ),
  ).toBe(true);
  await expect(page).toHaveURL(/\/guides\/taipei-taiwan\/$/);
});

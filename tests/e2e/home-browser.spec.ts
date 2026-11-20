import { expect, test } from "@playwright/test";

test("filters guides by country and renders global place search results", async ({ page }) => {
  await page.goto("/");

  await expect(page.locator("[data-home-results-count]")).toHaveText("3 guides across 3 countries");
  await expect(page.locator('[data-guide-card][data-guide-slug="tokyo-japan"]')).toBeVisible();
  await expect(page.locator('[data-guide-card][data-guide-slug="taipei-taiwan"]')).toBeVisible();
  await expect(
    page.locator('[data-guide-card][data-guide-slug="hong-kong-wanderlog-example"]'),
  ).toBeVisible();

  await page.locator("[data-home-search-input]").fill("bar");
  await expect(page.locator("[data-home-results-count]")).toHaveText("3 guides across 3 countries");
  await expect(page.locator('[data-guide-card][data-guide-slug="tokyo-japan"]')).toBeVisible();
  await expect(page.locator('[data-guide-card][data-guide-slug="taipei-taiwan"]')).toBeVisible();
  await expect(
    page.locator('[data-guide-card][data-guide-slug="hong-kong-wanderlog-example"]'),
  ).toBeVisible();
  await page.locator("[data-home-search-input]").clear();

  await page.locator("[data-country-select]").selectOption("taiwan");

  await expect(page.locator("[data-home-results-count]")).toHaveText("1 guide across 1 country");
  await expect(page.locator('[data-guide-card][data-guide-slug="taipei-taiwan"]')).toBeVisible();
  await expect(page.locator('[data-guide-card][data-guide-slug="tokyo-japan"]')).toBeHidden();

  await page.locator("[data-home-search-input]").fill("bar");
  await expect(page.locator("[data-home-results-count]")).toHaveText("1 guide across 1 country");
  await expect(page.locator('[data-guide-card][data-guide-slug="taipei-taiwan"]')).toBeVisible();

  await page.locator("[data-home-search-input]").fill("museum");
  await expect(page.locator("[data-global-search-results]")).toBeVisible();
  await expect(page.locator("[data-global-search-title]")).toContainText("matching");
  await expect(page.locator("[data-global-search-summary]")).toContainText(
    "Results are matched from saved place details and tags.",
  );
  await expect(page.locator("[data-global-search-suggestions] button").first()).toBeVisible();
  await expect(page.locator("[data-global-search-disclosure]")).toHaveCount(0);
  await expect(page.locator("[data-grouped-search-list]")).toBeVisible();
  await expect(page.locator("[data-grouped-search-list]")).toContainText("Taipei");
  await expect(page.locator("[data-grouped-search-list]")).toContainText(
    "Museum of Contemporary Art Taipei",
  );

  await page.locator('[data-search-view-toggle][data-search-view="individual"]').click();

  await expect(
    page.locator("[data-global-search-list] .search-result-card").filter({
      hasText: "Museum of Contemporary Art Taipei",
    }),
  ).toBeVisible();
  await expect(
    page.locator('[data-search-view-toggle][data-search-view="individual"]'),
  ).toHaveAttribute("aria-pressed", "true");
});

test("featured postcards and the country dropdown share a filter", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  const taiwan = page.getByRole("link", { name: "Taiwan 1 guide" });
  await expect(taiwan.locator("img")).toBeVisible();
  expect(
    await taiwan.locator("img").evaluate((img: HTMLImageElement) => img.naturalWidth),
  ).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "Show map" })).toHaveAttribute(
    "aria-expanded",
    "false",
  );

  await taiwan.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#browse-guides")).toBeFocused();
  await expect(page.locator("[data-country-select]")).toHaveValue("taiwan");
  await expect(taiwan).toHaveAttribute("aria-current", "true");
  await expect(page).toHaveURL(/#taiwan$/);
  await expect(page.locator("[data-country-block]:visible")).toHaveCount(1);

  await page.reload();
  await expect(page.locator("[data-country-select]")).toHaveValue("taiwan");
  await page.getByRole("button", { name: "Show map" }).click();
  await expect(page.locator("[data-home-guide-map]")).toBeVisible();
  await expect(page.locator("[data-home-map-summary]")).toContainText("1 guide in Taiwan");

  await page.locator("[data-country-select]").selectOption("");
  await expect(page.locator("[data-country-block]:visible")).toHaveCount(3);
  await expect(taiwan).not.toHaveAttribute("aria-current");
});

import { expect, test } from "@playwright/test";

test("filters guides by country and renders global place search results", async ({ page }) => {
  await page.goto("/");

  await expect(page.locator("[data-home-results-count]")).toHaveText("3 guides across 3 countries");
  await expect(page.locator('[data-guide-card][data-guide-slug="tokyo-japan"]')).toBeVisible();
  await expect(page.locator('[data-guide-card][data-guide-slug="taipei-taiwan"]')).toBeVisible();
  await expect(
    page.locator('[data-guide-card][data-guide-slug="hong-kong-wanderlog-example"]'),
  ).toBeVisible();

  await page.locator('[data-country-filter][data-country="taiwan"]').click();

  await expect(page.locator("[data-home-results-count]")).toHaveText("1 guide across 1 country");
  await expect(page.locator('[data-guide-card][data-guide-slug="taipei-taiwan"]')).toBeVisible();
  await expect(page.locator('[data-guide-card][data-guide-slug="tokyo-japan"]')).toBeHidden();

  await page.locator("[data-home-search-input]").fill("bar");
  await expect(page.locator("[data-home-results-count]")).toHaveText("1 guide across 1 country");
  await expect(page.locator('[data-guide-card][data-guide-slug="taipei-taiwan"]')).toBeVisible();

  await page.locator("[data-home-search-input]").fill("museum");
  await expect(page.locator("[data-global-search-results]")).toBeVisible();
  await expect(page.locator("[data-global-search-title]")).toContainText("matching");
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

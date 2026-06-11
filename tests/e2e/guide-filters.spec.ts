import { expect, test } from "@playwright/test";

const visiblePlaceCards = "[data-place-card]:not([hidden])";

test("filters, tags, and sorts places on a guide page", async ({ page }) => {
  await page.goto("/guides/taipei-taiwan/");

  const resultsCount = page.locator("[data-results-count]");
  const searchInput = page.locator("[data-search-input]");

  await expect(resultsCount).toHaveText("11 places");
  await expect(page.locator(visiblePlaceCards)).toHaveCount(11);

  await searchInput.fill("museum");

  await expect(resultsCount).toHaveText("2 places");
  await expect(page.locator(visiblePlaceCards)).toHaveCount(2);
  await expect(
    page.locator(visiblePlaceCards).filter({ hasText: "National Palace Museum" }),
  ).toBeVisible();
  await expect(
    page.locator(visiblePlaceCards).filter({ hasText: "Museum of Contemporary Art Taipei" }),
  ).toBeVisible();
  await expect(page.locator('[data-place-card][data-name="taipei 101"]')).toBeHidden();

  await searchInput.fill("");
  await page.locator('[data-type-filter][data-type="zoo"]').click();

  await expect(resultsCount).toHaveText("1 place");
  await expect(page.locator(visiblePlaceCards)).toHaveCount(1);
  await expect(page.locator(visiblePlaceCards).filter({ hasText: "Taipei Zoo" })).toBeVisible();
  await expect(page.locator('[data-type-filter][data-type="zoo"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await page.locator('[data-type-filter][data-type="zoo"]').click();
  await searchInput.fill("#rain");
  await expect(page.locator('[data-autocomplete-tag="rainy-day"]')).toBeVisible();
  await searchInput.press("Enter");

  await expect(page.locator("[data-selected-tags]")).toContainText("#rainy-day");
  await expect(resultsCount).toHaveText("2 places");
  await expect(page.locator(visiblePlaceCards)).toHaveCount(2);

  await page.locator('[data-remove-tag="rainy-day"]').click();
  await expect(resultsCount).toHaveText("11 places");

  await page.locator("[data-sort-select]").selectOption("name");

  await expect
    .poll(async () =>
      page
        .locator(visiblePlaceCards)
        .evaluateAll((cards) =>
          cards.slice(0, 3).map((card) => (card as HTMLElement).dataset.name),
        ),
    )
    .toEqual(["ad astra", "addiction aquatic development", "museum of contemporary art taipei"]);
});

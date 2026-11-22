import { expect, test } from "@playwright/test";

test("filters guides by country and renders global place search results", async ({ page }) => {
  await page.goto("/");

  await expect(page.locator("[data-home-results-count]")).toHaveText("3 guides across 3 countries");
  await expect(page.locator('[data-guide-card][data-guide-slug="tokyo-japan"]')).toBeVisible();
  await expect(page.locator('[data-guide-card][data-guide-slug="taipei-taiwan"]')).toBeVisible();
  await expect(
    page.locator('[data-guide-card][data-guide-slug="hong-kong-wanderlog-example"]'),
  ).toBeVisible();

  const taiwanOption = page.locator('[data-country-select] option[value="taiwan"]');
  await expect(taiwanOption).toHaveText("Taiwan 🇹🇼 (1 guide)");
  await page.locator("[data-home-search-input]").fill("no matching destination");
  await expect(taiwanOption).toHaveText("Taiwan 🇹🇼 (0 guides)");

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

test("opens the full guide from a grouped search header", async ({ page }) => {
  await page.goto("/");
  await page.locator("[data-country-select]").selectOption("taiwan");
  await page.locator("[data-home-search-input]").fill("museum");

  const groups = page.locator("[data-grouped-search-list]");
  const place = groups.getByRole("link", { name: /Museum of Contemporary Art Taipei/ });
  await expect(place).toHaveAttribute("href", /^\/guides\/taipei-taiwan\/\?place=[^&]+&q=museum$/);

  const header = groups.getByRole("link", { name: /Taipei.*match/ });
  await expect(header).toHaveAttribute("href", "/guides/taipei-taiwan/");
  await header.focus();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/\/guides\/taipei-taiwan\/$/);
});

test("featured postcards and the country dropdown share a filter", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  const taiwan = page.getByRole("link", { name: "Taiwan 1 guide" });
  await expect(taiwan.locator("img")).toBeVisible();
  expect(
    await taiwan.locator("img").evaluate((img: HTMLImageElement) => img.naturalWidth),
  ).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "Hide map" })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(page.locator("[data-home-guide-map]")).toBeVisible();

  await taiwan.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#browse-guides")).toBeFocused();
  await expect(page.locator("[data-country-select]")).toHaveValue("taiwan");
  await expect(taiwan).toHaveAttribute("aria-current", "true");
  await expect(page).toHaveURL(/#taiwan$/);
  await expect(page.locator("[data-country-block]:visible")).toHaveCount(1);

  await page.getByRole("button", { name: "Hide map" }).click();
  await page.reload();
  await expect(page.locator("[data-country-select]")).toHaveValue("taiwan");
  await expect(page.locator("[data-home-guide-map]")).toBeHidden();
  await page.getByRole("button", { name: "Show map" }).click();
  await expect(page.locator("[data-home-guide-map]")).toBeVisible();
  await expect(page.locator("[data-home-map-summary]")).toContainText("1 guide in Taiwan");

  await page.locator("[data-country-select]").selectOption("");
  await expect(page.locator("[data-country-block]:visible")).toHaveCount(3);
  await expect(taiwan).not.toHaveAttribute("aria-current");
});

test("keeps contributor footers aligned when guide descriptions have different lengths", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");

  // The sample site has one guide per country. Put two cards in one row for this layout check.
  const cards = page.locator("[data-guide-card]");
  await cards.evaluateAll(([first, second]) => {
    first.parentElement?.append(second);
    const description = second.querySelector(".ui-copy");
    if (description) description.textContent = "A longer guide description. ".repeat(18);
  });

  const firstGrid = page.locator("[data-country-block] .card-grid").first();
  const footerBottoms = await firstGrid
    .locator(".guide-card-footer")
    .evaluateAll((footers) =>
      footers.map((footer) => Math.round(footer.getBoundingClientRect().bottom)),
    );
  expect(footerBottoms).toHaveLength(2);
  expect(new Set(footerBottoms).size).toBe(1);
});

test("uses the header brand as the way back to the guide collection", async ({ page }) => {
  await page.goto("/guides/tokyo-japan/");

  const header = page.getByRole("banner");
  const brand = header.getByRole("link", { name: "Favorite Places Demo", exact: true });
  await expect(header.locator('a[href="/"]')).toHaveCount(1);
  await expect(header.getByRole("link", { name: "About", exact: true })).toBeVisible();
  await brand.click();

  await expect(page).toHaveURL("/");
  await expect(header.locator('a[href="/"]')).toHaveCount(1);
});

test("shares the homepage theme with individual guides", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const homeFont = await page
    .getByRole("heading", { level: 1 })
    .evaluate((heading) => getComputedStyle(heading).fontFamily);
  expect(homeFont).toContain("Source Serif 4");
  const homeBackground = await page
    .locator("body")
    .evaluate((body) => getComputedStyle(body).backgroundColor);

  await page.goto("/guides/tokyo-japan/");
  await expect(page.getByRole("banner")).toHaveCSS("background-color", "rgb(53, 62, 73)");
  await expect(page.getByRole("heading", { level: 1 })).toHaveCSS("font-family", homeFont);
  await expect(page.locator("body")).toHaveCSS("background-color", homeBackground);
  await expect(page.locator("[data-place-card]").first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

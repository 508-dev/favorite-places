import { expect, test } from "@playwright/test";

test("chat cards show place details and reveal the place without reloading", async ({ page }) => {
  await page.goto("/guides/tokyo-japan/");
  const target = page.locator("[data-place-card]").first();
  const id = await target.getAttribute("data-place-id");
  const name = await target.getAttribute("data-name");
  await page.route("**/chat-test-photo.png", (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>',
    }),
  );
  await page.route("**/api/chat", (route) =>
    route.fulfill({
      json: {
        reply: "Here are a few places.",
        places: [
          {
            id,
            name,
            url: `/guides/tokyo-japan/?place=${encodeURIComponent(id || "")}`,
            photo_url: "/chat-test-photo.png",
            rating: 4.8,
            user_rating_count: 321,
            price_range: "¥¥",
            note: "A lovely place.",
          },
        ],
      },
    }),
  );
  await page.locator("[data-search-input]").fill("unlikely matching search zzzzz");
  await expect(target).toBeHidden();
  await page.locator("[data-place-chat-toggle]").click();
  await expect(page.getByText("Your saved-place guide")).toHaveCount(0);
  await page.locator("[data-place-chat-input]").fill("Find a place");
  await page.locator("[data-place-chat-submit]").click();
  const result = page.locator(".place-chat-card");
  await expect(result).toContainText("4.8 ★ (321)");
  await expect(result).toContainText("A lovely place.");
  await expect(result.locator("img")).toHaveCount(1);
  await page.evaluate(() => {
    (window as unknown as { chatTestMarker: boolean }).chatTestMarker = true;
  });
  await page.locator("[data-guide-map]").evaluate((element) => {
    const map = (
      element as HTMLElement & { guideMap: { fitBounds: (...args: unknown[]) => unknown } }
    ).guideMap;
    const original = map.fitBounds.bind(map);
    (window as unknown as { chatRefits: number }).chatRefits = 0;
    map.fitBounds = (...args: unknown[]) => {
      (window as unknown as { chatRefits: number }).chatRefits++;
      return original(...args);
    };
  });
  await result.click();
  expect(await page.evaluate(() => (window as unknown as { chatRefits: number }).chatRefits)).toBe(
    0,
  );
  await page.evaluate(() => {
    document.querySelector("[data-guide-root]")?.dispatchEvent(
      new CustomEvent("guide:places-updated", {
        bubbles: true,
        detail: {
          source: "search-index",
          mapFrameActive: false,
          visiblePlaceIds: Array.from(
            document.querySelectorAll<HTMLElement>("[data-place-card]:not([hidden])"),
          ).map((card) => card.dataset.placeId),
        },
      }),
    );
  });
  expect(await page.evaluate(() => (window as unknown as { chatRefits: number }).chatRefits)).toBe(
    0,
  );
  await expect(target).toBeVisible();
  await expect(target).toHaveAttribute("data-search-highlight", "true");
  await expect(target).toBeFocused();
  await expect(target).toHaveAttribute("data-map-active", "true");
  await expect(page.locator(".leaflet-popup")).toBeVisible();
  expect(
    await page
      .locator("[data-guide-map]")
      .evaluate((element) =>
        (element as HTMLElement & { guideMap: { getZoom(): number } }).guideMap.getZoom(),
      ),
  ).toBeGreaterThanOrEqual(13);
  expect(
    await page.evaluate(() => (window as unknown as { chatTestMarker: boolean }).chatTestMarker),
  ).toBe(true);
  await expect(page.locator("[data-place-chat-panel]")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await result.click();
  await expect(page.locator("[data-place-chat-panel]")).toBeHidden();
});

test("chat proposes filters explicitly, applies them to the guide and supports undo", async ({
  page,
}) => {
  await page.goto("/guides/tokyo-japan/");
  const name =
    (await page.locator("[data-place-card]").first().getAttribute("data-name")) || "coffee";
  const scopeId = await page.locator("[data-place-card]").first().getAttribute("data-place-id");
  const originalCount = await page.locator("[data-place-card]:not([hidden])").count();
  await page.route("**/api/chat", (route) =>
    route.fulfill({
      json: {
        reply: "Here are some ideas.",
        places: [],
        actions: [
          {
            id: "guide.apply-filters",
            version: 1,
            guide_slug: "tokyo-japan",
            filters: { query: name, tags: [], min_rating: 0 },
            scope: { label: "Selected area", place_ids: [scopeId] },
          },
        ],
      },
    }),
  );
  await page.locator("[data-place-chat-toggle]").click();
  const panel = page.locator("[data-place-chat-panel]");
  const initialHeight = (await panel.boundingBox())!.height;
  await page.locator("[data-place-chat-input]").fill("Find a place like this");
  await page.locator("[data-place-chat-submit]").click();
  const apply = page.getByRole("button", { name: "Apply filters", exact: true });
  await expect(apply).toBeVisible();
  await expect(page.locator("[data-search-input]")).toHaveValue("");
  expect((await panel.boundingBox())!.height).toBeGreaterThanOrEqual(initialHeight);
  await apply.click();
  await expect(page.locator("[data-search-input]")).toHaveValue(name);
  await expect(page.locator(".guide-chat-filters")).toBeVisible();
  await expect(panel).toBeVisible();
  await expect(page.locator("[data-sort-select]")).toHaveValue("rating");
  await page.locator("[data-search-input]").fill("");
  await expect(page.locator("[data-place-card]:not([hidden])")).toHaveCount(1);
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(page.locator("[data-search-input]")).toHaveValue("");
  await expect(page.locator(".guide-chat-filters")).toBeHidden();
  await expect(page.locator("[data-sort-select]")).toHaveValue("curated");
  await expect(page.locator("[data-place-card]:not([hidden])")).toHaveCount(originalCount);
  await page.getByRole("button", { name: "Minimize chat", exact: true }).click();
  await expect(panel).toBeHidden();
  await page.locator("[data-place-chat-toggle]").click();
  await expect(apply).toBeVisible();
  const send = (await page.locator("[data-place-chat-submit]").boundingBox())!;
  const input = (await page.locator("[data-place-chat-input]").boundingBox())!;
  expect(send.height).toBeLessThanOrEqual(44);
  expect(Math.abs(send.y + send.height / 2 - input.y - input.height / 2)).toBeLessThan(2);
});

test("quota exhaustion explains the pause, retains the question, and leaves normal search available", async ({
  page,
}) => {
  await page.clock.install();
  await page.goto("/guides/tokyo-japan/");
  const resetAt = await page.evaluate(() => new Date(Date.now() + 3600000).toISOString());
  let requests = 0;
  await page.route("**/api/chat", (route) => {
    requests++;
    return route.fulfill({
      status: 429,
      json: {
        code: "ai_daily_quota_exhausted",
        error:
          "We've used today's AI search allowance. You can still search and filter places normally.",
        resets_at: resetAt,
      },
    });
  });
  await page.locator("[data-place-chat-toggle]").click();
  await page.locator("[data-place-chat-input]").fill("Find a quiet cafe");
  await page.locator("[data-place-chat-submit]").click();
  await expect(page.locator("[data-place-chat-thread]")).toContainText(
    "today's AI search allowance",
  );
  await expect(page.locator("[data-place-chat-input]")).toHaveValue("Find a quiet cafe");
  await expect(page.locator("[data-place-chat-submit]")).toBeDisabled();
  await expect(page.locator("[data-place-chat-status]")).toContainText(
    "Search and filters still work",
  );
  await page.getByRole("button", { name: "Minimize chat", exact: true }).click();
  await page.locator("[data-search-input]").fill("coffee");
  await expect
    .poll(() => page.locator("[data-place-card]:not([hidden])").count())
    .toBeGreaterThan(0);
  expect(requests).toBe(1);
  await page.clock.fastForward(3601000);
  await expect(page.locator("[data-place-chat-submit]")).toBeEnabled();
});

test("an unrelated turn leaves the active search intact for the next refinement", async ({
  page,
}) => {
  const active = {
    version: 1,
    query: "cafes in Tokyo",
    focus: "cafes",
    alternatives: ["coffee"],
    location: "Tokyo",
    preferences: [],
    min_rating: 0,
    place_ids: [],
  };
  const received: Record<string, unknown>[] = [];
  await page.route("**/api/chat", async (route) => {
    received.push(route.request().postDataJSON());
    const step = received.length;
    await route.fulfill({
      json:
        step === 2
          ? {
              outcome: "redirect",
              reply: "I can help find saved places or narrow filters.",
              places: [],
              guides: [],
              actions: [],
            }
          : {
              outcome: "search",
              reply: step === 1 ? "Search started." : "Search refined.",
              places: [],
              search_context: active,
            },
    });
  });
  await page.goto("/guides/tokyo-japan/");
  await page.locator("[data-place-chat-toggle]").click();
  for (const [message, reply] of [
    ["cafes", "Search started."],
    ["Write a poem", "I can help find saved places or narrow filters."],
    ["somewhere quieter", "Search refined."],
  ]) {
    await page.locator("[data-place-chat-input]").fill(message);
    await page.locator("[data-place-chat-submit]").click();
    await expect(page.getByText(reply, { exact: true })).toBeVisible();
  }
  expect(received).toHaveLength(3);
  expect(received[1].search_context).toEqual(active);
  expect(received[2].search_context).toEqual(active);
});

test("chat and native controls share type, rating and tag chips, with undo", async ({ page }) => {
  await page.goto("/guides/tokyo-japan/");
  const originalCount = await page.locator("[data-place-card]:not([hidden])").count();
  await page.locator('[data-type-filter][data-type="bar"]').click();
  await page.route("**/api/chat", (route) =>
    route.fulfill({
      json: {
        reply: "Here are some ideas.",
        places: [],
        actions: [
          {
            id: "guide.apply-filters",
            version: 1,
            guide_slug: "tokyo-japan",
            filters: { query: "gardens", tags: ["quiet"], min_rating: 4.5 },
          },
        ],
      },
    }),
  );
  await page.locator("[data-place-chat-toggle]").click();
  await page.locator("[data-place-chat-input]").fill("Quiet gardens with 4.5 stars");
  await page.locator("[data-place-chat-submit]").click();
  const apply = page.getByRole("button", { name: "Apply filters", exact: true });
  await apply.click();
  const chips = page.locator("[data-selected-tags]");
  await expect(page.locator('[data-type-filter][data-type="garden"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.locator("[data-search-input]")).toHaveValue("");
  await expect(page.locator("[data-rating-summary]")).toHaveText("4.5+");
  await expect(chips).toContainText("Type: Garden");
  await expect(chips).toContainText("4.5+ stars");
  await expect(chips).toContainText("#quiet");
  await expect(page.locator("[data-place-card]:not([hidden])")).toHaveCount(1);
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(page.locator('[data-type-filter][data-type="bar"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(chips).not.toContainText("#quiet");
  await apply.click();
  await chips.getByRole("button", { name: "Remove 4.5+ stars", exact: true }).click();
  await expect(page.locator("[data-rating-summary]")).toHaveText("");
  await chips.locator('[data-remove-filter="type"]').click();
  await expect(page.locator('[data-type-filter][data-type=""]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await chips.getByRole("button", { name: "Remove #quiet", exact: true }).click();
  await expect(chips).toBeHidden();
  await expect(page.locator("[data-place-card]:not([hidden])")).toHaveCount(originalCount);
  await apply.click();
  await expect(page).toHaveURL(/[?&]type=garden\b/);
  await page.reload();
  await expect(page.locator('[data-type-filter][data-type="garden"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(chips).toContainText("Type: Garden");
});

test("unsupported chat criteria stay editable and native areas replace precise chat scopes", async ({
  page,
}) => {
  await page.goto("/guides/tokyo-japan/");
  const first = page.locator("[data-place-card]").first();
  const id = (await first.getAttribute("data-place-id"))!;
  const name = (await first.getAttribute("data-name"))!;
  const originalCount = await page.locator("[data-place-card]:not([hidden])").count();
  await page.evaluate(
    ({ id, name }) => {
      document.querySelector("[data-guide-root]")!.dispatchEvent(
        new CustomEvent("guide:apply-filters", {
          detail: {
            action: {
              id: "guide.apply-filters",
              version: 1,
              guide_slug: "tokyo-japan",
              filters: { query: name, tags: [], min_rating: 0 },
              scope: { label: "Station walking distance", place_ids: [id] },
            },
          },
        }),
      );
    },
    { id, name },
  );
  const chips = page.locator("[data-selected-tags]");
  await expect(page.locator("[data-search-input]")).toHaveValue(name);
  await expect(chips).toContainText(`Search: ${name}`);
  await expect(chips).toContainText("Near: Station walking distance");
  await chips.locator('[data-remove-filter="query"]').click();
  await expect(page.locator("[data-search-input]")).toHaveValue("");
  await expect(page.locator("[data-place-card]:not([hidden])")).toHaveCount(1);
  // Another area must remain selectable, even outside the precise scope.
  const area = page.locator('[data-area-filter]:not([data-area=""])').first();
  await expect(area).toBeEnabled();
  await area.click();
  await expect(area).toHaveAttribute("aria-pressed", "true");
  await expect(chips).not.toContainText("Near:");
  await chips.locator('[data-remove-filter="area"]').click();
  await expect(page.locator("[data-place-card]:not([hidden])")).toHaveCount(originalCount);
  // Exact saved-place membership can use the existing area control instead.
  const scope = await area.evaluate((element) => {
    const button = element as HTMLElement;
    const ids = Array.from(document.querySelectorAll<HTMLElement>("[data-place-card]"))
      .filter((card) => card.dataset.neighborhoodToken === button.dataset.area)
      .map((card) => card.dataset.placeId!);
    return { label: button.dataset.areaLabel!, place_ids: ids };
  });
  expect(scope.place_ids.length).toBeGreaterThan(0);
  await page.evaluate((scope) => {
    document.querySelector("[data-guide-root]")!.dispatchEvent(
      new CustomEvent("guide:apply-filters", {
        detail: {
          action: {
            id: "guide.apply-filters",
            version: 1,
            guide_slug: "tokyo-japan",
            filters: { query: "", tags: [], min_rating: 0 },
            scope,
          },
        },
      }),
    );
  }, scope);
  await expect(area).toHaveAttribute("aria-pressed", "true");
  await expect(chips).toContainText(`Area: ${scope.label}`);
  await expect(chips).not.toContainText("Near:");
});

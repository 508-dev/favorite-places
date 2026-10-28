import { afterEach, describe, expect, it, vi } from "vitest";

import {
  loadSearchIndex,
  prepareSearchIndex,
  searchGuides,
  searchPlaces,
} from "../../public/scripts/place-search.js";

const index = prepareSearchIndex({
  version: 1,
  guides: [
    {
      slug: "tokyo-japan",
      title: "Tokyo, Japan",
      city: "Tokyo",
      country: "Japan",
      country_code: "JP",
      tags: ["coffee"],
      place_count: 2,
      top_categories: ["Coffee shop"],
      featured_names: ["Quiet Coffee"],
      url: "/guides/tokyo-japan/",
      search_text: "tokyo japan coffee",
    },
    {
      slug: "san-francisco-california-usa",
      title: "San Francisco, California, USA",
      city: "San Francisco",
      country: "United States",
      country_code: "US",
      tags: ["restaurants"],
      place_count: 1,
      top_categories: ["Restaurant"],
      featured_names: ["Buzzy Dinner"],
      url: "/guides/san-francisco-california-usa/",
      search_text: "san francisco california sf restaurants",
    },
    {
      slug: "new-york-new-york-usa",
      title: "New York, New York, USA",
      city: "New York",
      country: "United States",
      country_code: "US",
      tags: ["coffee"],
      place_count: 1,
      top_categories: ["Coffee shop"],
      featured_names: ["NY Coffee"],
      url: "/guides/new-york-new-york-usa/",
      search_text: "new york nyc united states us coffee",
    },
  ],
  entries: [
    {
      id: "tokyo-coffee",
      guide_slug: "tokyo-japan",
      guide_title: "Tokyo, Japan",
      city: "Tokyo",
      country: "Japan",
      name: "Quiet Coffee",
      category: "Coffee shop",
      neighborhood: "Shibuya",
      tags: ["coffee-shop", "shibuya"],
      vibe_tags: ["quiet", "laptop-friendly", "cozy"],
      note: "Quiet coffee shop with wifi.",
      top_pick: true,
      manual_rank: 2,
      maps_url: "https://maps.example/tokyo-coffee",
      url: "/guides/tokyo-japan/?place=tokyo-coffee",
      search_text: "quiet coffee shop wifi shibuya tokyo japan",
    },
    {
      id: "tokyo-burger",
      guide_slug: "tokyo-japan",
      guide_title: "Tokyo, Japan",
      city: "Tokyo",
      country: "Japan",
      name: "Henry's Burger Harajuku",
      category: "Burger restaurant",
      neighborhood: "Harajuku",
      tags: ["burger", "harajuku"],
      vibe_tags: ["casual", "quick-stop"],
      note: "Compact burger spot.",
      top_pick: false,
      manual_rank: 0,
      maps_url: "https://maps.example/tokyo-burger",
      url: "/guides/tokyo-japan/?place=tokyo-burger",
      search_text: "henrys burger harajuku casual quick stop tokyo japan",
    },
    {
      id: "sf-dinner",
      guide_slug: "san-francisco-california-usa",
      guide_title: "San Francisco, California, USA",
      city: "San Francisco",
      country: "United States",
      name: "Buzzy Dinner",
      category: "Restaurant",
      neighborhood: "Mission",
      tags: ["restaurant", "mission"],
      vibe_tags: ["date-night", "lively"],
      note: "Date night restaurant.",
      top_pick: false,
      manual_rank: 0,
      maps_url: "https://maps.example/sf-dinner",
      url: "/guides/san-francisco-california-usa/?place=sf-dinner",
      search_text: "date night restaurant mission san francisco sf",
    },
    {
      id: "ny-coffee",
      guide_slug: "new-york-new-york-usa",
      guide_title: "New York, New York, USA",
      city: "New York",
      country: "United States",
      name: "NY Coffee",
      category: "Coffee shop",
      neighborhood: "SoHo",
      tags: ["coffee-shop", "soho"],
      vibe_tags: ["quiet"],
      note: "Quiet coffee shop.",
      top_pick: false,
      manual_rank: 0,
      maps_url: "https://maps.example/ny-coffee",
      url: "/guides/new-york-new-york-usa/?place=ny-coffee",
      search_text: "quiet coffee shop soho new york united states us",
    },
  ],
});

describe("place search", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("parses natural global queries into location, category, and vibe constraints", () => {
    const state = searchPlaces("quiet coffee in tokyo", { index, scope: "all" });

    expect(state.results.map((result) => result.entry.id)).toEqual(["tokyo-coffee"]);
    expect(state.parsed.guideSlugs).toEqual(["tokyo-japan"]);
    expect(state.parsed.categories).toEqual(["cafe"]);
    expect(state.parsed.vibes).toContain("quiet");
  });

  it("keeps explicit location matches above unrelated cities", () => {
    const state = searchPlaces("date night restaurants in sf", { index, scope: "all" });

    expect(state.results[0].entry.id).toBe("sf-dinner");
    expect(
      state.results.every((result) => result.entry.guide_slug === "san-francisco-california-usa"),
    ).toBe(true);
  });

  it("supports guide-local queries without repeating the city name", () => {
    const state = searchPlaces("laptop friendly coffee", {
      index,
      scope: "guide",
      guideSlug: "tokyo-japan",
    });

    expect(state.results.map((result) => result.entry.id)).toEqual(["tokyo-coffee"]);
  });

  it("supports two-character prefix matches in guide-local search", () => {
    const state = searchPlaces("ha", {
      index,
      scope: "guide",
      guideSlug: "tokyo-japan",
      activeFilters: { tag: "casual" },
    });

    expect(state.results.map((result) => result.entry.id)).toEqual(["tokyo-burger"]);
  });

  it("filters unmatched guide-local queries after the index loads", () => {
    const state = searchPlaces("volcanic bookstore", {
      index,
      scope: "guide",
      guideSlug: "tokyo-japan",
    });

    expect(state.results).toEqual([]);
  });

  it("requires all meaningful terms in multi-word guide-local searches to match", () => {
    const state = searchPlaces("har cafe", {
      index,
      scope: "guide",
      guideSlug: "tokyo-japan",
    });

    expect(state.results).toEqual([]);
  });

  it("treats shared vibe aliases as alternatives instead of requiring every mapped vibe", () => {
    const state = searchPlaces("good vibes", {
      index,
      scope: "guide",
      guideSlug: "tokyo-japan",
    });

    expect(state.results.map((result) => result.entry.id)).toEqual(["tokyo-coffee"]);
    expect(state.parsed.vibes).toEqual(
      expect.arrayContaining(["cozy", "design-forward", "lively", "local-favorite"]),
    );
  });

  it("ignores cross-guide location aliases in guide-scoped searches", () => {
    const state = searchPlaces("quiet coffee in united states", {
      index,
      scope: "guide",
      guideSlug: "tokyo-japan",
    });

    expect(state.results.map((result) => result.entry.id)).toEqual(["tokyo-coffee"]);
    expect(state.parsed.guideSlugs).toEqual([]);
  });

  it("returns guide matches for broad home-page discovery", () => {
    expect(searchGuides("sf restaurants", { index })[0].guide.slug).toBe(
      "san-francisco-california-usa",
    );
  });

  it("does not return curated places for unrelated global searches", () => {
    const state = searchPlaces("volcanic bookstore in sf", { index, scope: "all" });

    expect(state.results).toEqual([]);
  });

  it("caches index fetches per URL and allows failed URLs to retry", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ version: 1, guides: [], entries: [] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            version: 1,
            guides: [{ slug: "tokyo-japan", title: "Tokyo", city: "Tokyo" }],
            entries: [],
          }),
      });
    vi.stubGlobal("fetch", fetchMock);

    await expect(loadSearchIndex("/data/search-index-failed.json")).rejects.toThrow("offline");
    await expect(loadSearchIndex("/data/search-index-failed.json")).resolves.toMatchObject({
      entries: [],
      guides: [],
    });
    await expect(loadSearchIndex("/data/search-index-other.json")).resolves.toMatchObject({
      guides: [expect.objectContaining({ slug: "tokyo-japan" })],
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/data/search-index-failed.json");
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/data/search-index-failed.json");
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/data/search-index-other.json");
  });
});

describe("review evidence retrieval", () => {
  it("finds review-only dishes and atmosphere without outranking curated matches", () => {
    const entries = [
      {
        id: "review",
        name: "Cafe One",
        category: "Cafe",
        guide_slug: "tokyo-japan",
        review_evidence: ["Quiet upstairs seating with sesame pudding."],
      },
      {
        id: "curated",
        name: "Cafe Two",
        category: "Cafe",
        guide_slug: "tokyo-japan",
        note: "Quiet upstairs seating with sesame pudding.",
        vibe_tags: ["quiet"],
      },
      { id: "unrelated", name: "Cafe Three", category: "Cafe", guide_slug: "tokyo-japan" },
    ];
    const prepared = prepareSearchIndex({ entries });
    for (const query of ["sesame pudding", "quiet cafe", "upstairs seating"]) {
      const result = searchPlaces(query, {
        index: prepared,
        scope: "guide",
        guideSlug: "tokyo-japan",
      });
      expect(result.results.map(({ entry }) => entry.id)).toEqual(["curated", "review"]);
    }
  });
});

it("searches CJK phrases embedded in cached reviews", () => {
  const prepared = prepareSearchIndex({
    entries: [
      { id: "match", name: "Shop One", review_evidence: ["這裡的小籠湯包很好吃"] },
      { id: "other", name: "Shop Two", review_evidence: ["牛肉麵很好吃"] },
    ],
  });
  expect(
    searchPlaces("小籠湯包", { index: prepared }).results.map(({ entry }) => entry.id),
  ).toEqual(["match"]);
});

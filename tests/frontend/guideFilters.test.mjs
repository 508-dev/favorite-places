import { describe, expect, it } from "vitest";

import {
  buildAreaFilterStatusMessage,
  buildEmptyStateMessage,
  buildNearbyDistanceMap,
  cardHasTag,
  cardMatchesBudget,
  cardMatchesType,
  compareCardsByCurated,
  compareCardsByNearby,
  compareCardsByNeighborhood,
  countAreaOptionCards,
  countBudgetOptionCards,
  countMatchingCards,
  countTagOptionCards,
  countTypeOptionCards,
  getInitialSelectedTags,
  hasAdditionalGuideFilters,
  locationFallbackMessage,
  normalizeUserLocationDetail,
  resolveLocationSortState,
  sortFilterOptions,
} from "../../public/scripts/guide-filters.js";

const makeCard = ({
  bestHit = "false",
  category = "",
  budgetKind = "",
  budgetTier = "",
  featured = "false",
  lat = "",
  lng = "",
  localityPath = "",
  name = "",
  neighborhood = "",
  neighborhoodToken = "",
  placeId,
  rank = "0",
  search = "",
  tags = "",
  topPick = "false",
  vibeTags = "",
} = {}) => ({
  dataset: {
    bestHit,
    budgetKind,
    budgetTier,
    category,
    featured,
    lat,
    lng,
    localityPath,
    name,
    neighborhood,
    neighborhoodToken,
    placeId,
    rank,
    search,
    tags,
    topPick,
    vibeTags,
  },
});

describe("guide filters", () => {
  it("counts vibe matches within the active area", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "south brisbane",
        search: "coffee brunch",
        vibeTags: "date-night scenic",
      }),
      makeCard({
        placeId: "2",
        neighborhood: "south brisbane",
        search: "rain museum",
        vibeTags: "rainy-day",
      }),
      makeCard({
        placeId: "3",
        neighborhood: "west end",
        search: "sunset drinks",
        vibeTags: "date-night scenic",
      }),
    ];

    expect(
      countMatchingCards(cards, {
        activeArea: "south brisbane",
        normalizedQuery: "",
        tag: "date-night",
      }),
    ).toBe(1);
    expect(
      countMatchingCards(cards, {
        activeArea: "south brisbane",
        normalizedQuery: "",
        tag: "",
      }),
    ).toBe(2);
  });

  it("matches tags across place and vibe tag fields", () => {
    const card = makeCard({
      placeId: "1",
      tags: JSON.stringify(["coffee", "bakery"]),
      vibeTags: JSON.stringify(["Date Night", "scenic"]),
    });

    expect(cardHasTag(card, "bakery")).toBe(true);
    expect(cardHasTag(card, "scenic")).toBe(true);
    expect(cardHasTag(card, "date-night")).toBe(true);
  });

  it("normalizes multi-word categories before type matching", () => {
    const card = makeCard({ placeId: "1" });
    card.dataset.category = "Apartment Building";

    expect(
      cardMatchesType(card, {
        activeTypeValue: "apartment-building",
        activeTypeSeedValues: [],
      }),
    ).toBe(true);
  });

  it("matches explicit budget kind and tier filters", () => {
    const card = makeCard({
      budgetKind: "restaurant_per_person",
      budgetTier: "2",
      placeId: "1",
    });

    expect(cardMatchesBudget(card)).toBe(true);
    expect(
      cardMatchesBudget(card, {
        activeBudgetSelections: ["restaurant_per_person:2"],
      }),
    ).toBe(true);
    expect(
      cardMatchesBudget(card, {
        activeBudgetSelections: ["hotel_per_night:2"],
      }),
    ).toBe(false);
  });

  it("matches any selected budget tier for range-style filtering", () => {
    const cards = [
      makeCard({
        budgetKind: "restaurant_per_person",
        budgetTier: "1",
        placeId: "cheap",
      }),
      makeCard({
        budgetKind: "restaurant_per_person",
        budgetTier: "2",
        placeId: "moderate",
      }),
      makeCard({
        budgetKind: "restaurant_per_person",
        budgetTier: "3",
        placeId: "splurge",
      }),
    ];

    expect(
      countMatchingCards(cards, {
        activeBudgetSelections: ["restaurant_per_person:2", "restaurant_per_person:3"],
      }),
    ).toBe(2);
  });

  it("counts budget options with other filters applied", () => {
    const cards = [
      makeCard({
        budgetKind: "restaurant_per_person",
        budgetTier: "2",
        neighborhood: "ginza",
        placeId: "1",
      }),
      makeCard({
        budgetKind: "restaurant_per_person",
        budgetTier: "3",
        neighborhood: "ginza",
        placeId: "2",
      }),
      makeCard({
        budgetKind: "restaurant_per_person",
        budgetTier: "2",
        neighborhood: "shibuya",
        placeId: "3",
      }),
    ];

    expect(
      countBudgetOptionCards(
        cards,
        {
          activeArea: "ginza",
        },
        {
          budgetKind: "restaurant_per_person",
          budgetTier: "2",
        },
      ),
    ).toBe(1);
  });

  it("builds area-aware status and empty messages when broader matches exist", () => {
    expect(
      buildAreaFilterStatusMessage({
        activeAreaLabel: "South Brisbane",
        hasAdditionalFilters: true,
        visibleCount: 1,
        overflowCount: 3,
      }),
    ).toBe("Showing 1 place in South Brisbane. 3 more matches elsewhere in this guide.");

    expect(
      buildEmptyStateMessage({
        activeAreaLabel: "South Brisbane",
        overflowCount: 2,
        query: "",
      }),
    ).toBe(
      "No matches in South Brisbane. 2 more matches elsewhere in this guide. Try another area or clear the area filter.",
    );
  });

  it("keeps generic empty-state copy when no area filter is active", () => {
    expect(
      buildEmptyStateMessage({
        activeAreaLabel: "",
        overflowCount: 2,
        query: "",
      }),
    ).toBe("No matches. Try a broader search or clear the tag filter.");
  });

  it("uses singular overflow grammar for a single broader result", () => {
    expect(
      buildAreaFilterStatusMessage({
        activeAreaLabel: "South Brisbane",
        hasAdditionalFilters: true,
        visibleCount: 1,
        overflowCount: 1,
      }),
    ).toBe("Showing 1 place in South Brisbane. 1 more match elsewhere in this guide.");

    expect(
      buildEmptyStateMessage({
        activeAreaLabel: "South Brisbane",
        overflowCount: 1,
        query: "coffee",
      }),
    ).toBe(
      'No places matched "coffee" in South Brisbane. 1 more match elsewhere in this guide. Try another area or clear the area filter.',
    );
  });

  it("suppresses the area status line when no places are visible in the selected area", () => {
    expect(
      buildAreaFilterStatusMessage({
        activeAreaLabel: "South Brisbane",
        hasAdditionalFilters: true,
        visibleCount: 0,
        overflowCount: 2,
      }),
    ).toBe("");
  });

  it("suppresses the area status line when area is the only active filter", () => {
    expect(
      buildAreaFilterStatusMessage({
        activeAreaLabel: "Zhongshan",
        hasAdditionalFilters: false,
        visibleCount: 3,
        overflowCount: 8,
      }),
    ).toBe("");
  });

  it("treats a full-map frame as no additional guide filter", () => {
    expect(
      hasAdditionalGuideFilters({
        mapFramePlaceIds: new Set(["1", "2", "3"]),
        totalCardCount: 3,
      }),
    ).toBe(false);
  });

  it("treats a narrowed map frame as an additional guide filter", () => {
    expect(
      hasAdditionalGuideFilters({
        mapFramePlaceIds: new Set(["1", "2"]),
        totalCardCount: 3,
      }),
    ).toBe(true);
  });

  it("treats an empty active map frame as an additional guide filter", () => {
    expect(
      hasAdditionalGuideFilters({
        mapFramePlaceIds: new Set(),
        totalCardCount: 3,
      }),
    ).toBe(true);
  });

  it("accepts the module's active filter argument names", () => {
    expect(
      hasAdditionalGuideFilters({
        activeTypeValue: "restaurant",
        selectedTagValues: ["date-night"],
      }),
    ).toBe(true);
  });

  it("hydrates initial selected tags from repeated tag params", () => {
    expect(
      getInitialSelectedTags({
        allTags: ["seafood", "date-night", "quiet"],
        params: new URLSearchParams("tag=seafood&tag=date-night&tag=unknown&tag=seafood"),
      }),
    ).toEqual(["seafood", "date-night"]);
  });

  it("keeps the area label in query empty-state copy even with no overflow matches", () => {
    expect(
      buildEmptyStateMessage({
        activeAreaLabel: "South Brisbane",
        overflowCount: 0,
        query: "coffee",
      }),
    ).toBe(
      'No places matched "coffee" in South Brisbane. Try another area or clear the area filter.',
    );
  });

  it("lets guide-wide overflow ignore the current map frame", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "south brisbane",
        search: "brunch",
        vibeTags: "date-night",
      }),
      makeCard({
        placeId: "2",
        neighborhood: "west end",
        search: "brunch",
        vibeTags: "date-night",
      }),
    ];

    expect(
      countMatchingCards(cards, {
        activeArea: "south brisbane",
        mapFramePlaceIds: new Set(["1"]),
        normalizedQuery: "brunch",
        searchResultIds: new Set(["1", "2"]),
        tag: "date-night",
      }),
    ).toBe(1);

    expect(
      countMatchingCards(cards, {
        normalizedQuery: "brunch",
        searchResultIds: new Set(["1", "2"]),
        tag: "date-night",
      }),
    ).toBe(2);
  });

  it("does not count off-screen matches in the same area as guide-wide overflow", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "south brisbane",
        search: "brunch",
        vibeTags: "date-night",
      }),
      makeCard({
        placeId: "2",
        neighborhood: "south brisbane",
        search: "brunch",
        vibeTags: "date-night",
      }),
    ];

    const broaderAreaCount = countMatchingCards(cards, {
      normalizedQuery: "brunch",
      searchResultIds: new Set(["1", "2"]),
      tag: "date-night",
    });
    const areaMatchCount = countMatchingCards(cards, {
      activeArea: "south brisbane",
      normalizedQuery: "brunch",
      searchResultIds: new Set(["1", "2"]),
      tag: "date-night",
    });
    const visibleCountInMapFrame = countMatchingCards(cards, {
      activeArea: "south brisbane",
      mapFramePlaceIds: new Set(["1"]),
      normalizedQuery: "brunch",
      searchResultIds: new Set(["1", "2"]),
      tag: "date-night",
    });

    expect(broaderAreaCount).toBe(2);
    expect(areaMatchCount).toBe(2);
    expect(visibleCountInMapFrame).toBe(1);
    expect(Math.max(0, broaderAreaCount - areaMatchCount)).toBe(0);
  });

  it("normalizes accented and slugged area filters before matching cards", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "São Paulo",
        search: "coffee",
        vibeTags: "date-night",
      }),
      makeCard({
        placeId: "2",
        neighborhood: "Rio de Janeiro",
        search: "coffee",
        vibeTags: "date-night",
      }),
    ];

    expect(
      countMatchingCards(cards, {
        activeArea: "sao-paulo",
        normalizedQuery: "coffee",
        tag: "date-night",
      }),
    ).toBe(1);
  });

  it("matches normalized neighborhood dataset values against district-style area filters", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "Da’an District",
        neighborhoodToken: "da-an-district",
        search: "dessert",
      }),
    ];

    expect(
      countMatchingCards(cards, {
        activeArea: "da-an",
        normalizedQuery: "dessert",
      }),
    ).toBe(1);
  });

  it("keeps neighborhood sorting on the human-readable label", () => {
    const cards = [
      makeCard({
        placeId: "1",
        name: "Cafe B",
        neighborhood: "Évora",
        neighborhoodToken: "evora",
      }),
      makeCard({
        placeId: "2",
        name: "Cafe A",
        neighborhood: "Zurich",
        neighborhoodToken: "zurich",
      }),
    ];

    expect([...cards].sort(compareCardsByNeighborhood).map((card) => card.dataset.placeId)).toEqual(
      ["1", "2"],
    );
  });

  it("matches broader-area filters against any locality in the card path", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "Jingumae",
        localityPath: JSON.stringify(["Jingumae", "Shibuya City"]),
        search: "coffee",
      }),
      makeCard({
        placeId: "2",
        neighborhood: "Nishiazabu",
        localityPath: JSON.stringify(["Nishiazabu", "Minato City"]),
        search: "coffee",
      }),
    ];

    expect(
      countMatchingCards(cards, {
        activeArea: "broader-shibuya",
        normalizedQuery: "coffee",
      }),
    ).toBe(1);
  });

  it("matches primary areas across district suffix variants without collapsing broader levels", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "Zhongshan District",
        localityPath: JSON.stringify(["Zhongshan District"]),
        search: "dumplings",
      }),
      makeCard({
        placeId: "2",
        neighborhood: "Meguro",
        localityPath: JSON.stringify(["Meguro", "Meguro City"]),
        search: "coffee",
      }),
    ];

    expect(
      countMatchingCards(cards, {
        activeArea: "zhongshan",
        normalizedQuery: "dumplings",
      }),
    ).toBe(1);

    expect(
      countMatchingCards(cards, {
        activeArea: "broader-meguro",
        normalizedQuery: "coffee",
      }),
    ).toBe(1);

    expect(
      countMatchingCards(cards, {
        activeArea: "meguro",
        normalizedQuery: "coffee",
      }),
    ).toBe(1);
  });

  it("counts area options against the current non-area filters", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "lastarria",
        category: "cafe",
        vibeTags: JSON.stringify(["cozy"]),
      }),
      makeCard({
        placeId: "2",
        neighborhood: "providencia",
        category: "restaurant",
        vibeTags: JSON.stringify(["cozy"]),
      }),
      makeCard({
        placeId: "3",
        neighborhood: "bellavista",
        category: "cafe",
        vibeTags: JSON.stringify(["lively"]),
      }),
    ];

    expect(
      countAreaOptionCards(
        cards,
        {
          activeTypeValue: "cafe",
          activeTypeSeedValues: [],
          selectedTagValues: ["cozy"],
        },
        "lastarria",
      ),
    ).toBe(1);
    expect(
      countAreaOptionCards(cards, {
        activeTypeValue: "cafe",
        activeTypeSeedValues: [],
        selectedTagValues: ["cozy"],
      }),
    ).toBe(1);
  });

  it("counts type and tag options within the selected area", () => {
    const cards = [
      makeCard({
        placeId: "1",
        neighborhood: "lastarria",
        category: "cafe",
        vibeTags: JSON.stringify(["cozy"]),
      }),
      makeCard({
        placeId: "2",
        neighborhood: "lastarria",
        category: "restaurant",
        vibeTags: JSON.stringify(["date-night"]),
      }),
      makeCard({
        placeId: "3",
        neighborhood: "providencia",
        category: "restaurant",
        vibeTags: JSON.stringify(["date-night"]),
      }),
    ];

    expect(
      countTypeOptionCards(
        cards,
        {
          activeArea: "lastarria",
        },
        {
          typeValue: "restaurant",
          typeSeedValues: [],
        },
      ),
    ).toBe(1);
    expect(
      countTagOptionCards(
        cards,
        {
          activeArea: "lastarria",
          activeTypeValue: "restaurant",
          activeTypeSeedValues: [],
        },
        "cozy",
      ),
    ).toBe(0);
  });

  it("sorts matching filter options ahead of zero-count options while keeping pinned items first", () => {
    expect(
      sortFilterOptions([
        { pinned: false, active: false, count: 0, originalIndex: 3, id: "zero" },
        { pinned: false, active: false, count: 2, originalIndex: 2, id: "two" },
        { pinned: true, active: false, count: 1, originalIndex: 0, id: "all" },
        { pinned: false, active: false, count: 5, originalIndex: 1, id: "five" },
      ]).map((option) => option.id),
    ).toEqual(["all", "five", "two", "zero"]);
  });

  it("keeps the original order within equal-count groups even when one option is active", () => {
    expect(
      sortFilterOptions([
        { pinned: true, active: false, count: 11, originalIndex: 0, id: "all" },
        { pinned: false, active: false, count: 2, originalIndex: 1, id: "da-an" },
        { pinned: false, active: false, count: 2, originalIndex: 2, id: "taipei-city" },
        { pinned: false, active: false, count: 2, originalIndex: 3, id: "zhongshan" },
        { pinned: false, active: true, count: 1, originalIndex: 4, id: "datong" },
        { pinned: false, active: false, count: 1, originalIndex: 5, id: "shilin" },
        { pinned: false, active: false, count: 1, originalIndex: 6, id: "wenshan" },
      ]).map((option) => option.id),
    ).toEqual(["all", "da-an", "taipei-city", "zhongshan", "datong", "shilin", "wenshan"]);
  });

  it("sorts nearby cards by cached distance and keeps cards without coordinates last", () => {
    const currentLocation = { lat: 35.6812, lng: 139.7671 };
    const cards = [
      makeCard({ placeId: "far", lat: "35.6895", lng: "139.6917", name: "Far", rank: "1" }),
      makeCard({ placeId: "missing", name: "Missing", rank: "99", topPick: "true" }),
      makeCard({ placeId: "near", lat: "35.6814", lng: "139.7673", name: "Near", rank: "2" }),
    ];
    const distanceByPlaceId = buildNearbyDistanceMap(cards, currentLocation);

    const sortedCards = [...cards].sort((left, right) =>
      compareCardsByNearby(left, right, {
        currentLocation,
        distanceByPlaceId,
      }),
    );

    expect(Array.from(distanceByPlaceId.keys())).toEqual(["far", "near"]);
    expect(sortedCards.map((card) => card.dataset.placeId)).toEqual(["near", "far", "missing"]);
  });

  it("falls back to curated sorting when nearby sorting has no current location", () => {
    const cards = [
      makeCard({ placeId: "rank-1", name: "Bravo", rank: "1" }),
      makeCard({ placeId: "rank-3", name: "Alpha", rank: "3" }),
      makeCard({ placeId: "top-pick", name: "Cafe", rank: "2", topPick: "true" }),
    ];

    const nearbySorted = [...cards].sort((left, right) =>
      compareCardsByNearby(left, right, {
        currentLocation: null,
        distanceByPlaceId: new Map(),
      }),
    );
    const curatedSorted = [...cards].sort(compareCardsByCurated);

    expect(nearbySorted.map((card) => card.dataset.placeId)).toEqual(
      curatedSorted.map((card) => card.dataset.placeId),
    );
  });

  it("prioritizes featured and best-hit cards in curated sorting", () => {
    const cards = [
      makeCard({ placeId: "rank-10", name: "Bravo", rank: "10" }),
      makeCard({ placeId: "best-hit", name: "Alpha", rank: "1", bestHit: "true" }),
      makeCard({ placeId: "top-pick", name: "Cafe", rank: "99", topPick: "true" }),
      makeCard({ placeId: "featured", name: "Delta", rank: "0", featured: "true" }),
    ];

    expect([...cards].sort(compareCardsByCurated).map((card) => card.dataset.placeId)).toEqual([
      "featured",
      "best-hit",
      "top-pick",
      "rank-10",
    ]);
  });
  it("resets nearby sorting to curated when location is denied or unavailable", () => {
    expect(
      resolveLocationSortState({
        currentLocation: null,
        currentLocationStatus: "denied",
        sortValue: "nearby",
      }),
    ).toEqual({
      message: "Location unavailable. Showing curated order instead.",
      shouldFallback: true,
      sortValue: "curated",
    });

    expect(
      resolveLocationSortState({
        currentLocation: null,
        currentLocationStatus: "unavailable",
        fallbackMessage: "Location denied.",
        fallbackSortValue: "rating",
        sortValue: "nearby",
      }),
    ).toEqual({
      message: "Location denied.",
      shouldFallback: true,
      sortValue: "rating",
    });
  });

  it("uses the guide-area fallback copy when the user is outside the guide area", () => {
    expect(
      resolveLocationSortState({
        currentLocation: null,
        currentLocationStatus: "far",
        fallbackMessage: locationFallbackMessage(
          "far",
          "Location unavailable. Showing curated order instead.",
        ),
        sortValue: "nearby",
      }),
    ).toEqual({
      message: "You're outside this guide area. Showing curated order instead.",
      shouldFallback: true,
      sortValue: "curated",
    });

    expect(
      resolveLocationSortState({
        currentLocation: { lat: 25.033, lng: 121.5654 },
        currentLocationStatus: "far",
        fallbackMessage: locationFallbackMessage(
          "far",
          "Location unavailable. Showing curated order instead.",
        ),
        sortValue: "nearby",
      }),
    ).toEqual({
      message: "You're outside this guide area. Showing curated order instead.",
      shouldFallback: true,
      sortValue: "curated",
    });
  });

  it("keeps direct geolocation coordinates even when the user is outside the guide area", () => {
    expect(
      normalizeUserLocationDetail({
        coordinates: { lat: "35.6812", lng: "139.7671" },
        nearGuide: false,
        status: "available",
      }),
    ).toEqual({
      lat: 35.6812,
      lng: 139.7671,
    });

    expect(
      normalizeUserLocationDetail({
        coordinates: { lat: "35.6812", lng: "139.7671" },
        nearGuide: false,
        status: "far",
      }),
    ).toEqual({
      lat: 35.6812,
      lng: 139.7671,
    });
  });
  it("does not reset nearby sorting while location is still idle, checking, or already available", () => {
    expect(
      resolveLocationSortState({
        currentLocation: null,
        currentLocationStatus: "idle",
        sortValue: "nearby",
      }),
    ).toEqual({
      message: "",
      shouldFallback: false,
      sortValue: "nearby",
    });

    expect(
      resolveLocationSortState({
        currentLocation: null,
        currentLocationStatus: "checking",
        sortValue: "nearby",
      }),
    ).toEqual({
      message: "",
      shouldFallback: false,
      sortValue: "nearby",
    });

    expect(
      resolveLocationSortState({
        currentLocation: { lat: 35.6812, lng: 139.7671 },
        currentLocationStatus: "available",
        sortValue: "nearby",
      }),
    ).toEqual({
      message: "",
      shouldFallback: false,
      sortValue: "nearby",
    });
  });
});

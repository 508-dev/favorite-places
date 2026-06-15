import { describe, expect, it } from "vitest";

import {
  buildGuideJsonLd,
  buildGuideMetaDescription,
  buildHomeJsonLd,
  buildHomeMetaDescription,
  serializeJsonLdForHtml,
  toPlainText,
} from "../../src/lib/seo";
import type { Guide, GuideManifest, Place } from "../../src/lib/types";

function createGuideManifest(overrides: Partial<GuideManifest> = {}): GuideManifest {
  return {
    slug: "tokyo-japan",
    title: "Tokyo, Japan",
    description: null,
    country_name: "Japan",
    country_code: "JP",
    center_lat: 35.6764,
    center_lng: 139.65,
    city_name: "Tokyo",
    list_tags: [],
    place_count: 3,
    featured_names: [],
    top_categories: ["restaurant"],
    ...overrides,
  };
}

function createPlace(overrides: Partial<Place> = {}): Place {
  return {
    id: "place-1",
    name: "Coffee Spot",
    address: "1 Example St",
    lat: 35.0,
    lng: 139.0,
    maps_url: "https://maps.example/place-1",
    cid: null,
    google_id: null,
    google_place_id: null,
    google_place_resource_name: null,
    rating: 4.5,
    user_rating_count: 120,
    primary_category: "cafe",
    primary_category_localized: null,
    marker_icon: "cafe",
    tags: ["coffee"],
    visible_tags: ["coffee"],
    vibe_tags: ["quiet"],
    price_range: null,
    budget_kind: null,
    budget_tier: null,
    budget_label: null,
    locality_path: [],
    neighborhood: "Shibuya",
    note: "Great espresso",
    why_recommended: null,
    main_photo_path: null,
    photo_url: null,
    top_pick: false,
    hidden: false,
    manual_rank: 0,
    status: "open",
    provenance: {
      tags: [],
    },
    ...overrides,
  };
}

function createGuide(overrides: Partial<Guide> = {}): Guide {
  return {
    slug: "tokyo-japan",
    title: "Tokyo, Japan",
    description: null,
    source_url: null,
    list_id: null,
    place_photo_mode: "local_cache",
    country_name: "Japan",
    country_code: "JP",
    city_name: "Tokyo",
    list_tags: ["coffee", "late-night"],
    featured_place_ids: [],
    best_hit_place_ids: [],
    best_hit_min_rating: null,
    best_hit_min_reviews: null,
    top_categories: ["coffee-shop", "restaurant"],
    generated_at: "2026-04-28T00:00:00.000Z",
    place_count: 3,
    center_lat: 35.6764,
    center_lng: 139.65,
    places: [],
    ...overrides,
  };
}

describe("seo helpers", () => {
  it("normalizes markdown links, raw urls, and whitespace to plain text", () => {
    expect(
      toPlainText("  Start [Example](https://example.com) and https://foo.test  \n  End  "),
    ).toBe("Start Example and End");
  });

  it("builds a home meta description from intro text and counters", () => {
    expect(
      buildHomeMetaDescription({
        intro: " Saved places for trip planning. ",
        guideCount: 12,
        countryCount: 5,
        placeCount: 320,
      }),
    ).toBe(
      "Saved places for trip planning. Browse 12 guides across 5 countries and 320 saved places.",
    );
  });

  it("uses explicit guide descriptions after plain-text normalization", () => {
    const guide = createGuide({
      description: "A [great](https://example.com) food guide for Tokyo.",
    });

    expect(
      buildGuideMetaDescription({
        guide,
        countryName: "Japan",
        featuredPlaceNames: ["Sushi Saito"],
      }),
    ).toBe("A great food guide for Tokyo.");
  });

  it("builds a fallback guide description from location and featured places", () => {
    const guide = createGuide({
      description: null,
      place_count: 4,
      top_categories: ["coffee-shop", "bakery"],
    });

    expect(
      buildGuideMetaDescription({
        guide,
        countryName: "Japan",
        featuredPlaceNames: ["Glitch Coffee", "Fuglen Tokyo"],
      }),
    ).toBe("4 saved places in Tokyo, Japan. Includes Glitch Coffee and Fuglen Tokyo.");
  });

  it("escapes opening angle brackets when serializing JSON-LD for html", () => {
    expect(
      serializeJsonLdForHtml({
        "@context": "https://schema.org",
        description: "</script><script>alert(1)</script>",
      }),
    ).toContain("\\u003c/script>\\u003cscript>alert(1)\\u003c/script>");
  });

  it("keeps home JSON-LD list counts aligned with the emitted guide list", () => {
    const guides = Array.from({ length: 30 }, (_, index) =>
      createGuideManifest({
        slug: `guide-${index + 1}`,
        title: `Guide ${index + 1}`,
      }),
    );

    const jsonLd = buildHomeJsonLd({
      siteUrl: "https://favorite-places.pages.dev/",
      pageUrl: "https://favorite-places.pages.dev/",
      description: "Home page description",
      guides,
    });

    const itemList = jsonLd.mainEntity as {
      numberOfItems: number;
      itemListElement: unknown[];
    };

    expect(itemList.numberOfItems).toBe(24);
    expect(itemList.itemListElement).toHaveLength(24);
  });

  it("keeps guide JSON-LD list counts aligned with the emitted place list", () => {
    const visiblePlaces = Array.from({ length: 28 }, (_, index) =>
      createPlace({
        id: `place-${index + 1}`,
        name: `Place ${index + 1}`,
      }),
    );
    const guide = createGuide({
      place_count: visiblePlaces.length,
      places: visiblePlaces,
    });

    const [, collectionPage] = buildGuideJsonLd({
      siteUrl: "https://favorite-places.pages.dev/",
      pageUrl: "https://favorite-places.pages.dev/guides/tokyo-japan/",
      description: "Guide description",
      guide,
      countryName: "Japan",
      visiblePlaces,
    });

    const itemList = collectionPage.mainEntity as {
      numberOfItems: number;
      itemListElement: unknown[];
    };

    expect(itemList.numberOfItems).toBe(25);
    expect(itemList.itemListElement).toHaveLength(25);
  });

  it("includes guide author metadata in guide JSON-LD when available", () => {
    const [, collectionPage] = buildGuideJsonLd({
      siteUrl: "https://favorite-places.pages.dev/",
      pageUrl: "https://favorite-places.pages.dev/guides/tokyo-japan/",
      description: "Guide description",
      guide: createGuide({
        author: {
          name: "Curator Name",
          photo_path: "/author-photos/curator.webp",
          photo_url: "https://example.com/curator.jpg",
        },
      }),
      countryName: "Japan",
      visiblePlaces: [],
    });

    expect((collectionPage as Record<string, unknown>).author).toEqual({
      "@type": "Person",
      name: "Curator Name",
      image: "https://favorite-places.pages.dev/author-photos/curator.webp",
    });
  });

  it("prefers why_recommended over note for place JSON-LD descriptions", () => {
    const [, collectionJsonLd] = buildGuideJsonLd({
      siteUrl: "https://favorite-places.pages.dev/",
      pageUrl: "https://favorite-places.pages.dev/guides/tokyo-japan/",
      description: "Guide description",
      guide: createGuide(),
      countryName: "Japan",
      visiblePlaces: [
        createPlace({
          note: "Raw saved-list note",
          why_recommended: "Rendered recommendation copy",
        }),
      ],
    });

    expect(JSON.stringify(collectionJsonLd)).toContain("Rendered recommendation copy");
    expect(JSON.stringify(collectionJsonLd)).not.toContain("Raw saved-list note");
  });
});

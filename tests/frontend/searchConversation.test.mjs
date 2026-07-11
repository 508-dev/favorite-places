import { describe, expect, it } from "vitest";

import {
  buildSearchConversation,
  buildSearchSuggestions,
} from "../../public/scripts/search-conversation.js";

describe("search conversation", () => {
  it("turns deterministic parser state into a scoped result summary", () => {
    expect(
      buildSearchConversation({
        count: 2,
        parsed: { categories: ["cafe"], vibes: ["quiet"] },
        scopeLabel: "Tokyo",
      }),
    ).toBe(
      "I found 2 places for quiet and cafe in Tokyo. Results are matched from saved place details and tags.",
    );
  });

  it("explains empty results without implying a model answered", () => {
    expect(
      buildSearchConversation({
        count: 0,
        parsed: { categories: [], vibes: ["date-night"] },
        scopeLabel: "this guide",
      }),
    ).toBe(
      "No places matched for date night in this guide. Try removing a term or choosing a suggested refinement.",
    );
  });

  it("suggests frequent unused categories and vibe tags as refinements", () => {
    expect(
      buildSearchSuggestions({
        parsed: { categories: ["cafe"], vibes: ["quiet"] },
        results: [
          {
            entry: {
              category: "Coffee shop",
              vibe_tags: ["quiet", "laptop-friendly", "cozy"],
            },
          },
          {
            entry: {
              category: "Coffee shop",
              vibe_tags: ["laptop-friendly"],
            },
          },
        ],
      }),
    ).toEqual([
      { label: "Coffee Shop", query: "coffee shop" },
      { label: "Laptop Friendly", query: "laptop friendly" },
      { label: "Cozy", query: "cozy" },
    ]);
  });
});

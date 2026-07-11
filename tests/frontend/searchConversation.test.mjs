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

  it("includes literal query terms that the parser did not classify", () => {
    expect(
      buildSearchConversation({
        count: 1,
        parsed: { categories: [], unmatchedTerms: ["shibuya"], vibes: ["quiet"] },
        scopeLabel: "this guide",
      }),
    ).toBe(
      "I found 1 place for quiet and shibuya in this guide. Results are matched from saved place details and tags.",
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
      { action: "append", label: "Coffee Shop", query: "coffee shop" },
      { action: "append", label: "Laptop Friendly", query: "laptop friendly" },
      { action: "append", label: "Cozy", query: "cozy" },
    ]);
  });

  it("offers a deterministic broadening action when there are no result signals", () => {
    expect(
      buildSearchSuggestions({
        parsed: { categories: ["cafe"], vibes: [] },
        results: [],
      }),
    ).toEqual([{ action: "clear", label: "Clear search", query: "" }]);
  });
});

import { describe, expect, it, vi } from "vitest";
import { onRequestPost } from "../../functions/api/chat.js";
import { resolveSearchIntent } from "../../functions/lib/chat-provider.js";
import { searchIntent } from "../helpers/search-intent.mjs";

const guide = { slug: "tokyo-japan", title: "Tokyo, Japan 🇯🇵", city: "Tokyo", country: "Japan" };
const oldState = {
  version: 1,
  query: "ramen in Tokyo Uni place",
  focus: "ramen",
  alternatives: ["ramen noodles"],
  location: "Tokyo",
  preferences: ["Uni place", "quiet"],
  min_rating: 0,
  place_ids: [],
};

describe("recovery from misclassified food preferences", () => {
  it.each([
    ["And Uni place", "sea urchin", ["uni", "ウニ"]],
    ["Uni place!", "sea urchin", ["uni", "ウニ"]],
    ["Uni is my favorite food", "sea urchin", ["uni"]],
    ["Seafood place", "seafood", ["海鮮"]],
    ["Sea urchin restaurant", "sea urchin", ["uni"]],
    ["Kaiseki", "kaiseki", ["懐石料理"]],
    ["Japanese restauran", "Japanese restaurant", ["和食"]],
  ])("replaces ramen for %s without preserving polluted preferences", async (message, focus, alternatives) => {
    const run = vi.fn(async (_model, request) => {
      const input = JSON.parse(request.messages[1].content);
      expect(input.current_guide).toMatchObject({ city: "Tokyo", country: "Japan" });
      expect(input.previous_state.preferences).toEqual(["quiet"]);
      expect(input.previous_query).not.toContain("Uni place");
      return {
        response: JSON.stringify(
          searchIntent({
            route: "search",
            query: `${focus} in Tokyo`,
            focus,
            alternatives,
            location: "Tokyo",
            preferences: ["quiet"],
          }),
        ),
      };
    });
    const result = await resolveSearchIntent(
      { AI: { run } },
      [{ role: "user", content: message }],
      oldState,
      [],
      guide,
    );
    expect(result.focus).toBe(focus);
    expect(result.preferences).toEqual(["quiet"]);
    expect(result.location).toBe("Tokyo");
    expect(result.query).not.toMatch(/ramen|Uni place/);
    expect(oldState.preferences).toEqual(["Uni place", "quiet"]);
    expect(run).toHaveBeenCalledOnce();
  });
  it("still preserves the food and area for a mood-only follow-up", async () => {
    const env = {
      AI: {
        run: async () => ({
          response: JSON.stringify(
            searchIntent({
              route: "search",
              turn_type: "refinement",
              query: "quiet restaurants",
              focus: "ramen",
              alternatives: [],
              location: "Tokyo",
              preferences: ["quiet", "date night"],
            }),
          ),
        }),
      },
    };
    const result = await resolveSearchIntent(
      env,
      [{ role: "user", content: "somewhere quieter" }],
      oldState,
    );
    expect(result).toMatchObject({
      focus: "ramen",
      location: "Tokyo",
      preferences: ["quiet", "date-night"],
    });
  });
  it("allows an explicit whole-guide recovery without silently broadening an area", async () => {
    const response = (turn_type, location) => ({
      response: JSON.stringify(
        searchIntent({ route: "search", focus: "ramen", turn_type, location }),
      ),
    });
    const env = {
      AI: {
        run: vi
          .fn()
          .mockResolvedValueOnce(response("retry", "Ximen"))
          .mockResolvedValueOnce(response("refinement", "")),
      },
    };
    const previous = { ...oldState, location: "Ximen" };
    expect(
      (await resolveSearchIntent(env, [{ role: "user", content: "Try again" }], previous)).location,
    ).toBe("Ximen");
    expect(
      (
        await resolveSearchIntent(
          env,
          [{ role: "user", content: "Search the whole guide" }],
          previous,
        )
      ).location,
    ).toBe("");
  });
});

it("does not clear a named area in an anywhere-in-that-area request", async () => {
  const env = {
    AI: {
      run: async () => ({
        response: JSON.stringify(
          searchIntent({
            route: "search",
            query: "ramen in Shibuya",
            focus: "ramen",
            alternatives: [],
            location: "Shibuya",
            preferences: [],
          }),
        ),
      }),
    },
  };
  expect(
    (await resolveSearchIntent(env, [{ role: "user", content: "Anywhere in Shibuya" }], oldState))
      .location,
  ).toBe("Shibuya");
});

describe("correlated turn diagnostics", () => {
  it("identifies an empty geographic scope without logging visitor text", async () => {
    const log = vi.spyOn(console, "info").mockImplementation(() => {});
    try {
      const response = await onRequestPost({
        request: new Request("https://example.com/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            mode: "guide",
            guide_slug: guide.slug,
            messages: [{ role: "user", content: "uni in SecretMissingArea" }],
          }),
        }),
        env: {
          CHAT_ENABLED: "true",
          AI: {
            run: async () => ({
              response: JSON.stringify(
                searchIntent({
                  route: "search",
                  query: "uni in SecretMissingArea",
                  focus: "uni",
                  alternatives: [],
                  location: "SecretMissingArea",
                  preferences: [],
                }),
              ),
            }),
          },
          ASSETS: {
            fetch: async () =>
              Response.json({
                guides: [guide],
                entries: [
                  {
                    id: "one",
                    guide_slug: guide.slug,
                    name: "Uni Counter",
                    category: "Japanese restaurant",
                    neighborhood: "Ebisu",
                  },
                ],
              }),
          },
        },
      });
      const result = await response.json();
      expect(result.diagnostics).toMatchObject({
        version: 1,
        stage: "retrieve",
        outcome: "no_matches",
        no_match_reason: "area_scope_empty",
        model_calls: 1,
        counts: { guide_entries: 1, scoped_entries: 0, candidates: 0, results: 0 },
      });
      expect(result.diagnostics.request_id).toBe(response.headers.get("X-Chat-Request-Id"));
      expect(result.reply).toContain("area");
      expect(JSON.stringify(log.mock.calls)).not.toMatch(/SecretMissingArea|Uni Counter|uni in/);
    } finally {
      log.mockRestore();
    }
  });
});

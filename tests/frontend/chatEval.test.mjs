import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { SEARCH_SCOPE_REPLY } from "../../functions/lib/chat-scope.js";
import { evaluateTurn } from "../../scripts/evaluate_chat.mjs";

describe("chat trajectory assertions", () => {
  it("checks redirects without requiring or accepting replacement search context", () => {
    const redirect = {
      outcome: "redirect",
      reply: SEARCH_SCOPE_REPLY,
      places: [],
      guides: [],
      actions: [],
    };
    expect(evaluateTurn(redirect, { outcome: "redirect" })).toEqual([]);
    expect(
      evaluateTurn({ ...redirect, reply: "Here is a poem" }, { outcome: "redirect" }),
    ).toContain("redirect leaked arbitrary assistant prose");
    expect(
      evaluateTurn({ ...redirect, search_context: { query: "poems" } }, { outcome: "redirect" }),
    ).toContain("redirect returned artifacts or replaced search state");
  });

  it("rejects redirects for every existing search trajectory", () => {
    const fixtures = JSON.parse(
      readFileSync(new URL("../fixtures/chat-evals.json", import.meta.url), "utf8"),
    );
    const redirect = { outcome: "redirect", reply: SEARCH_SCOPE_REPLY };
    expect(evaluateTurn(redirect)).toContain("expected search outcome");
    for (const fixture of fixtures) {
      for (const turn of fixture.turns) {
        if (turn.expect?.outcome === "redirect") continue;
        expect(evaluateTurn(redirect, turn.expect)).toContain("expected search outcome");
      }
    }
  });

  it("rejects the original stale beef-noodle follow-up", () => {
    const failures = evaluateTurn(
      {
        outcome: "search",
        reply: "Here are some places",
        search_context: {
          version: 1,
          turn_type: "new_search",
          query: "beef noodles and soup dumplings in Ximen",
        },
        places: [{ id: "beef", name: "Beef Noodles", url: "/guides/taipei-taiwan/?place=beef" }],
      },
      {
        query_includes: ["soup dumpling", "ximen"],
        query_excludes: ["beef noodle"],
        names_exclude: "beef noodle",
      },
    );
    expect(failures).toHaveLength(2);
  });
  it("accepts preserved context with a replaced food type", () => {
    expect(
      evaluateTurn(
        {
          outcome: "search",
          reply: "Here are some places",
          search_context: { version: 1, turn_type: "new_search", query: "soup dumplings in Ximen" },
          places: [
            {
              id: "dumplings",
              name: "Soup Dumplings",
              url: "/guides/taipei-taiwan/?place=dumplings",
            },
          ],
        },
        {
          query_includes: ["soup dumpling", "ximen"],
          query_excludes: ["beef noodle"],
          results_include: "dumpling",
        },
      ),
    ).toEqual([]);
  });
});

it("fails a food-switch trajectory for empty results, stale focus or food preferences", () => {
  const failures = evaluateTurn(
    {
      outcome: "search",
      reply: "No matches",
      places: [],
      search_context: {
        version: 1,
        turn_type: "new_search",
        query: "sea urchin in Tokyo",
        focus: "ramen",
        preferences: ["Uni place"],
      },
    },
    { min_results: 1, focus_includes: "uni|urchin", preferences_exclude: "uni|urchin" },
  );
  expect(failures).toContain("fewer than 1 results");
  expect(failures).toContain("unexpected focus ramen");
  expect(failures).toContain("food leaked into preference state");
});

it("requires the expected model-owned transition", () => {
  const result = {
    outcome: "search",
    reply: "Found places",
    places: [],
    search_context: { version: 1, query: "sea urchin in Tokyo", turn_type: "retry" },
  };
  expect(evaluateTurn(result, { turn_types: ["correction"] })).toContain(
    "unexpected turn type retry",
  );
  delete result.search_context.turn_type;
  expect(evaluateTurn(result)).toContain("missing valid search turn type");
});

it("rejects extra guide filters even if the recommendation cards look relevant", () => {
  const result = {
    outcome: "search",
    reply: "Found ramen",
    places: [],
    search_context: {
      version: 1,
      turn_type: "new_search",
      query: "ramen in Tokyo",
      preferences: [],
      min_rating: 0,
    },
    actions: [
      { id: "guide.apply-filters", filters: { query: "ramen", tags: ["quiet"], min_rating: 4.5 } },
    ],
  };
  const expected = { guide_filters: { query: "ramen", tags: [], min_rating: 0 } };
  expect(evaluateTurn(result, expected)).toContain(
    "guide filters differ from the requested broad search",
  );
  result.actions[0].filters = expected.guide_filters;
  expect(evaluateTurn(result, expected)).toEqual([]);
  result.search_context.preferences = ["quiet"];
  expect(evaluateTurn(result, expected)).toContain(
    "intent invented unrequested filter constraints",
  );
});

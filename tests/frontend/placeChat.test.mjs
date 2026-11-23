import { describe, expect, it, vi } from "vitest";
import { onRequestPost } from "../../functions/api/chat.js";
import { completeChat, resolveSearchIntent } from "../../functions/lib/chat-provider.js";
import { searchIntent } from "../helpers/search-intent.mjs";

const index = {
  guides: [
    {
      slug: "tokyo-japan",
      title: "Tokyo",
      city: "Tokyo",
      country: "Japan",
      search_text: "tokyo japan",
    },
    {
      slug: "kyoto-japan",
      title: "Kyoto",
      city: "Kyoto",
      country: "Japan",
      search_text: "kyoto japan temples",
    },
  ],
  entries: [
    {
      id: "quiet-cafe",
      guide_slug: "tokyo-japan",
      guide_title: "Tokyo",
      name: "Quiet Cafe",
      city: "Tokyo",
      country: "Japan",
      category: "Cafe",
      neighborhood: "Shibuya",
      tags: ["coffee"],
      vibe_tags: ["quiet"],
      note: "A calm coffee shop.",
      photo_url: "/place-photos/quiet.webp",
      rating: 4.7,
      user_rating_count: 120,
      price_range: "¥¥",
      url: "/guides/tokyo-japan/?place=quiet-cafe",
      search_text: "quiet cafe coffee tokyo shibuya japan",
    },
    {
      id: "busy-cafe",
      guide_slug: "tokyo-japan",
      guide_title: "Tokyo",
      name: "Busy Cafe",
      city: "Tokyo",
      country: "Japan",
      category: "Cafe",
      tags: ["coffee"],
      vibe_tags: ["lively"],
      url: "/guides/tokyo-japan/?place=busy-cafe",
      search_text: "busy cafe coffee tokyo japan",
    },
    {
      id: "kyoto-temple",
      guide_slug: "kyoto-japan",
      guide_title: "Kyoto",
      name: "Kyoto Temple",
      city: "Kyoto",
      country: "Japan",
      category: "Temple",
      tags: [],
      vibe_tags: ["quiet"],
      url: "/guides/kyoto-japan/?place=kyoto-temple",
      search_text: "quiet temple kyoto japan",
    },
  ],
};

function context(messages, ai = vi.fn(), options = {}) {
  return {
    request: new Request("https://example.com/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages, ...options }),
    }),
    env: {
      CHAT_ENABLED: "true",
      ASSETS: { fetch: vi.fn(async () => Response.json(index)) },
      AI: {
        run: vi.fn(async (model, request) => {
          if (request.messages?.[0].content.startsWith("Resolve the current place-search intent")) {
            const state = JSON.parse(request.messages[1].content);
            if (!state.previous_query && request.messages.length === 3)
              return {
                response: JSON.stringify(
                  searchIntent({
                    route: "search",
                    query: request.messages.at(-1).content,
                    focus: request.messages.at(-1).content,
                    alternatives: [],
                  }),
                ),
              };
          }
          return ai(model, request);
        }),
      },
    },
  };
}

describe("place chat", () => {
  it("returns a friendly quota error and stops inference when classification exhausts the allowance", async () => {
    const ai = vi.fn(async () => {
      throw Object.assign(new Error("Daily allocation exhausted"), { code: 3036 });
    });
    const input = context([{ role: "user", content: "cafes" }], ai, {
      mode: "guide",
      guide_slug: "tokyo-japan",
    });
    input.env.CHAT_ALERTS_ENABLED = "true";
    input.env.CHAT_ALERTS = { fetch: vi.fn(async () => Response.json({ status: "sent" })) };
    input.waitUntil = vi.fn();
    const response = await onRequestPost(input);
    expect(response.status).toBe(429);
    expect(Number(response.headers.get("Retry-After"))).toBeGreaterThan(0);
    expect(await response.json()).toMatchObject({
      code: "ai_daily_quota_exhausted",
      error: expect.stringContaining("search and filter places normally"),
      resets_at: expect.any(String),
    });
    expect(ai).toHaveBeenCalledOnce();
    expect(input.waitUntil).toHaveBeenCalledOnce();
    await input.waitUntil.mock.calls[0][0];
    expect(input.env.CHAT_ALERTS.fetch).toHaveBeenCalledOnce();
  });

  it("retrieves a review-only match beyond the candidate cap and supplies bounded evidence", async () => {
    const ai = vi.fn(async (_model, request) => {
      if (request.questions?.place) {
        expect(JSON.stringify(request.questions.place.criteria)).toContain(
          "Sesame pudding upstairs",
        );
        expect(JSON.stringify(request.questions.place.criteria)).not.toContain("unrelated review");
        return { answers: { place: { type: "choice", probabilities: { place_0: 1 } } } };
      }
      const catalog = request.messages.find((message) =>
        message.content.startsWith("Candidate places:"),
      );
      expect(catalog.content).toContain('"review_evidence":["Sesame pudding upstairs"]');
      expect(catalog.content).not.toContain("unrelated review");
      return {
        response: JSON.stringify({
          reply: "A possible match.",
          place_ids: ["tokyo-japan:review-only"],
        }),
      };
    });
    const input = context([{ role: "user", content: "sesame pudding" }], ai, {
      mode: "guide",
      guide_slug: "tokyo-japan",
    });
    input.env.ASSETS.fetch = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [
          ...Array.from({ length: 30 }, (_, i) => ({ ...index.entries[0], id: `filler-${i}` })),
          {
            ...index.entries[1],
            id: "review-only",
            review_evidence: ["unrelated review", "Sesame pudding upstairs"],
          },
        ],
      }),
    );
    const response = await onRequestPost(input);
    expect(response.status).toBe(200);
    expect((await response.json()).places.map((place) => place.id)).toEqual(["review-only"]);
  });

  it("does not force review-only matches after the model rejects a negative review", async () => {
    const input = context(
      [{ role: "user", content: "sesame pudding" }],
      async (_model, request) => {
        if (request.questions?.place)
          return { answers: { place: { type: "choice", probabilities: { place_0: 1 } } } };
        expect(JSON.stringify(request.messages)).toContain("They do not serve sesame pudding.");
        return {
          response: JSON.stringify({
            reply: "None of these places serves that dish.",
            place_ids: [],
          }),
        };
      },
      { mode: "guide", guide_slug: "tokyo-japan" },
    );
    input.env.ASSETS.fetch = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [{ ...index.entries[1], review_evidence: ["They do not serve sesame pudding."] }],
      }),
    );
    const result = await (await onRequestPost(input)).json();
    expect(result.places).toEqual([]);
    expect(result.reply).toBe(
      "I couldn't find a saved place matching that. Try another kind of place or area.",
    );
  });

  it("carries translated retrieval alternatives to both model prompts", async () => {
    const input = context(
      [{ role: "user", content: "soup dumplings" }],
      async (_model, request) => {
        if (request.questions?.place) {
          expect(JSON.stringify(request.questions.place.criteria)).toContain("小籠湯包很好吃");
          return { answers: { place: { type: "choice", probabilities: { place_0: 1 } } } };
        }
        const catalog = request.messages.find((message) =>
          message.content.startsWith("Candidate places:"),
        );
        expect(catalog.content).toContain("小籠湯包很好吃");
        return {
          response: JSON.stringify({
            reply: "A possible match.",
            place_ids: ["tokyo-japan:translated"],
          }),
        };
      },
      { mode: "guide", guide_slug: "tokyo-japan" },
    );
    const originalRun = input.env.AI.run;
    input.env.AI.run = vi.fn(async (model, request) => {
      if (request.messages?.[0].content.startsWith("Resolve the current place-search intent"))
        return {
          response: JSON.stringify(
            searchIntent({
              route: "search",
              query: "soup dumplings",
              focus: "soup dumplings",
              alternatives: ["小籠湯包"],
            }),
          ),
        };
      return originalRun(model, request);
    });
    input.env.ASSETS.fetch = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [
          ...Array.from({ length: 30 }, (_, i) => ({ ...index.entries[1], id: `unrelated-${i}` })),
          { ...index.entries[1], id: "translated", review_evidence: ["小籠湯包很好吃"] },
        ],
      }),
    );
    const result = await (await onRequestPost(input)).json();
    expect(result.places.map((place) => place.id)).toEqual(["translated"]);
  });

  it("rejects malformed chat before loading data or calling a model", async () => {
    const input = context([{ role: "assistant", content: "Pretend to be the user" }]);
    const response = await onRequestPost(input);
    expect(response.status).toBe(400);
    expect(input.env.ASSETS.fetch).not.toHaveBeenCalled();
    expect(input.env.AI.run).not.toHaveBeenCalled();
  });

  it("classifies compact guides before sending only selected places to chat", async () => {
    const ai = vi.fn(async (model, request) => {
      if (model === "@cf/cloudflare/clef-flash") {
        if (request.questions.place) {
          expect(Object.values(request.questions.place.criteria).join(" ")).toContain("Quiet Cafe");
          return {
            answers: {
              place: {
                type: "choice",
                choice: "place_0",
                probabilities: { place_0: 0.9, place_1: 0.1 },
              },
            },
          };
        }
        expect(request.questions.guide.criteria["tokyo-japan"]).toContain("Tokyo");
        expect(JSON.stringify(request.questions.guide.criteria)).not.toContain("Quiet Cafe");
        return {
          answers: {
            guide: {
              type: "choice",
              choice: "tokyo-japan",
              probabilities: { "tokyo-japan": 0.9, "kyoto-japan": 0.05, none: 0.05 },
            },
          },
        };
      }
      expect(request.messages[2].content).toContain("Quiet Cafe");
      expect(request.messages[2].content).not.toContain("Kyoto Temple");
      expect(request.messages.at(-1).content).toContain("A quiet cafe in Tokyo");
      return {
        response: JSON.stringify({
          reply: "A calm spot sounds right.\n* **Quiet Cafe** is lovely.",
          place_ids: ["tokyo-japan:quiet-cafe", "made-up-place"],
          guide_ids: ["tokyo-japan", "made-up-guide"],
        }),
      };
    });
    const response = await onRequestPost(
      context([{ role: "user", content: "A quiet cafe in Tokyo" }], ai),
    );
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.reply).toBe("Here are a few guides and places that may fit.");
    expect(body.guides).toEqual([
      expect.objectContaining({
        slug: "tokyo-japan",
        places: [expect.objectContaining({ id: "quiet-cafe" })],
      }),
    ]);
    expect(ai).toHaveBeenCalledTimes(3);
  });

  it("scopes guide chat to its guide without a classifier call", async () => {
    const ai = vi.fn(async (_model, request) => {
      if (request.questions?.place) {
        return {
          answers: {
            place: {
              type: "choice",
              choice: "place_0",
              probabilities: { place_0: 0.9, place_1: 0.1 },
            },
          },
        };
      }
      expect(request.messages[2].content).toContain("Quiet Cafe");
      expect(request.messages[2].content).not.toContain("Kyoto Temple");
      return {
        response:
          '{"reply":"Here is one option.","place_ids":["tokyo-japan:quiet-cafe","kyoto-japan:kyoto-temple"]}',
      };
    });
    const response = await onRequestPost(
      context([{ role: "user", content: "Cozy cafe in Tokyo" }], ai, {
        mode: "guide",
        guide_slug: "tokyo-japan",
      }),
    );
    expect(response.status).toBe(200);
    expect(ai).toHaveBeenCalledTimes(2);
    expect((await response.json()).places.map((place) => place.id)).toEqual(["quiet-cafe"]);
  });

  it("returns up to six validated cards with their available place details", async () => {
    const entries = Array.from({ length: 8 }, (_, number) => ({
      ...index.entries[0],
      id: `cafe-${number}`,
      name: `Cafe ${number}`,
    }));
    const ai = vi.fn(async (_model, request) => {
      if (request.questions?.place) {
        return {
          answers: {
            place: {
              type: "choice",
              probabilities: Object.fromEntries(entries.map((_, i) => [`place_${i}`, 1 / (i + 1)])),
            },
          },
        };
      }
      return {
        response: JSON.stringify({
          reply: "Here are some ideas.",
          place_ids: [...entries.map((entry) => `tokyo-japan:${entry.id}`), "unknown"],
        }),
      };
    });
    const input = context([{ role: "user", content: "Tokyo cafes" }], ai, {
      mode: "guide",
      guide_slug: "tokyo-japan",
    });
    input.env.ASSETS.fetch = vi.fn(async () => Response.json({ guides: index.guides, entries }));
    const result = await (await onRequestPost(input)).json();
    expect(result.places).toHaveLength(6);
    expect(result.places[0]).toMatchObject({
      photo_url: "/place-photos/quiet.webp",
      rating: 4.7,
      user_rating_count: 120,
      price_range: "¥¥",
      note: "A calm coffee shop.",
    });
  });

  it("rejects unknown guide before inference", async () => {
    const input = context([{ role: "user", content: "Cafe" }], vi.fn(), {
      mode: "guide",
      guide_slug: "missing",
    });
    expect((await onRequestPost(input)).status).toBe(400);
    expect(input.env.AI.run).not.toHaveBeenCalled();
  });

  it("uses a fixed search clarification when no places are selected", async () => {
    const ai = vi.fn(async (_model, request) => {
      if (request.questions?.place) {
        return {
          answers: { place: { type: "choice", probabilities: { place_0: 0.9, place_1: 0.1 } } },
        };
      }
      return {
        response: JSON.stringify({ reply: "What neighborhood do you prefer?", place_ids: [] }),
      };
    });
    const response = await onRequestPost(
      context([{ role: "user", content: "Somewhere to eat" }], ai, {
        mode: "guide",
        guide_slug: "tokyo-japan",
      }),
    );
    expect(await response.json()).toEqual({
      diagnostics: expect.objectContaining({
        outcome: "no_matches",
        no_match_reason: "selection_empty",
        stage: "validate_results",
      }),
      outcome: "search",
      reply: "I couldn't find a saved place matching that. Try another kind of place or area.",
      places: [],
      search_context: {
        version: 1,
        turn_type: "new_search",
        query: "Somewhere to eat",
        focus: "Somewhere to eat",
        alternatives: [],
        location: "",
        location_alternatives: [],
        preferences: [],
        min_rating: 0,
        place_ids: [],
      },
    });
  });

  it("resolves a changed food type before retrieving and ranking a follow-up", async () => {
    const ai = vi.fn(async (_model, request) => {
      if (request.messages?.[0].content.startsWith("Resolve the current place-search intent")) {
        expect(JSON.parse(request.messages[1].content)).toMatchObject({
          previous_query: "beef noodles in Ximen",
        });
        expect(request.messages.at(-1)).toEqual({
          role: "user",
          content: "how about soup dumplings",
        });
        return {
          response: JSON.stringify(
            searchIntent({
              route: "search",
              query: "soup dumplings in Ximen",
              focus: "soup dumplings",
              location: "Ximen",
              alternatives: ["xiaolongbao"],
            }),
          ),
        };
      }
      if (request.questions?.place) {
        expect(request.state).toBe("soup dumplings in Ximen");
        expect(JSON.stringify(request.questions.place.criteria)).toContain("Xiao Long Bao");
        return { answers: { place: { type: "choice", probabilities: { place_0: 1 } } } };
      }
      expect(
        request.messages.some((message) =>
          message.content.includes("Current resolved search: soup dumplings in Ximen"),
        ),
      ).toBe(true);
      return { response: '{"reply":"Here are some ideas.","place_ids":[]}' };
    });
    const input = context(
      [
        { role: "user", content: "Find beef noodles in Ximen" },
        { role: "assistant", content: "Here are some places." },
        { role: "user", content: "how about soup dumplings" },
      ],
      ai,
      {
        mode: "guide",
        guide_slug: "tokyo-japan",
        search_context: { version: 1, query: "beef noodles in Ximen" },
      },
    );
    input.env.ASSETS.fetch = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [
          ...Array.from({ length: 30 }, (_, i) => ({
            ...index.entries[0],
            id: `other-${i}`,
            name: `Ximen Restaurant ${i}`,
            note: "An unrelated Ximen place.",
            search_text: "ximen restaurant",
            lat: 25.043,
            lng: 121.507,
          })),
          {
            ...index.entries[0],
            id: "dumplings",
            name: "Xiao Long Bao",
            note: "Juicy xiaolongbao.",
            search_text: "xiaolongbao dumplings",
            lat: 25.042,
            lng: 121.509,
          },
        ],
      }),
    );
    const response = await onRequestPost(input);
    expect(response.status).toBe(200);
    expect((await response.json()).search_context.query).toBe("soup dumplings in Ximen");
  });

  it.each([
    {
      query: "ramen in Tokyo",
      tags: ["quiet", "date-night", "group-friendly", "solo-friendly"],
      min_rating: 4.5,
    },
    { query: "https://attacker.example", tags: ["invented-tag"], min_rating: 99 },
    null,
  ])("builds broad guide filters from the intent instead of the recommendation: %j", async (filters) => {
    const input = context([{ role: "user", content: "looking for ramen" }], undefined, {
      mode: "guide",
      guide_slug: "tokyo-japan",
    });
    input.env.ASSETS.fetch = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [
          ...["quiet", "date-night", "group-friendly", "solo-friendly"].map((tag, n) => ({
            ...index.entries[0],
            id: `ramen-${n}`,
            name: `Ramen ${n}`,
            category: "Ramen restaurant",
            search_text: "ramen tokyo",
            vibe_tags: [tag],
            rating: n ? 4.2 : 4.7,
          })),
        ],
      }),
    );
    input.env.AI.run = vi.fn(async (_model, request) => {
      if (request.messages?.[0].content.startsWith("Resolve"))
        return {
          response: JSON.stringify(
            searchIntent({ route: "search", focus: "ramen", location: "Tokyo" }),
          ),
        };
      if (request.questions?.place)
        return {
          answers: { place: { type: "choice", probabilities: { place_0: 1, place_1: 0.9 } } },
        };
      expect(request.response_format.json_schema.schema.properties).not.toHaveProperty("filters");
      return {
        response: JSON.stringify({
          reply: "Here are some ideas.",
          place_ids: ["tokyo-japan:ramen-0", "tokyo-japan:ramen-1"],
          filters,
        }),
      };
    });
    const body = await (await onRequestPost(input)).json();
    expect(body.actions).toEqual([
      {
        id: "guide.apply-filters",
        version: 1,
        guide_slug: "tokyo-japan",
        filters: { query: "ramen", tags: [], min_rating: 0 },
      },
    ]);
    expect(body.places.map((place) => place.id)).toEqual(["ramen-0", "ramen-1"]);
    expect(body.places[1].rating).toBe(4.2);
  });

  it("preserves explicitly requested guide preferences and rating", async () => {
    const input = context(
      [{ role: "user", content: "quiet cafes rated at least 4.5" }],
      undefined,
      { mode: "guide", guide_slug: "tokyo-japan" },
    );
    input.env.AI.run = vi.fn(async (_model, request) => {
      if (request.messages?.[0].content.startsWith("Resolve"))
        return {
          response: JSON.stringify(
            searchIntent({
              route: "search",
              focus: "cafe",
              location: "Tokyo",
              preferences: ["quiet"],
              min_rating: 4.5,
            }),
          ),
        };
      if (request.questions?.place)
        return { answers: { place: { type: "choice", probabilities: { place_0: 1 } } } };
      return {
        response: JSON.stringify({
          reply: "Ideas",
          place_ids: ["tokyo-japan:quiet-cafe"],
          filters: null,
        }),
      };
    });
    const body = await (await onRequestPost(input)).json();
    expect(body.actions[0].filters).toEqual({ query: "cafe", tags: ["quiet"], min_rating: 4.5 });
  });

  it("applies the model-resolved rating refinement with its food and area", async () => {
    const run = vi.fn(async () => ({
      response: JSON.stringify(
        searchIntent({
          route: "search",
          turn_type: "refinement",
          query: "high rated restaurants in Ximen",
          focus: "beef noodles",
          location: "Ximen",
          alternatives: [],
          min_rating: 4.5,
        }),
      ),
    }));
    const result = await resolveSearchIntent(
      { AI: { run } },
      [{ role: "user", content: "only places rated 4.5 stars and up" }],
      {
        query: "beef noodles in Ximen",
        focus: "beef noodles",
        alternatives: ["beef noodle"],
        location: "Ximen",
        preferences: [],
        min_rating: 0,
      },
    );
    expect(result).toMatchObject({ focus: "beef noodles", location: "Ximen", min_rating: 4.5 });
    expect(result.query).toContain("beef noodles");
    expect(result.query).not.toContain("restaurants");
  });

  it("accepts the model-resolved replacement city", async () => {
    const result = await resolveSearchIntent(
      {
        AI: {
          run: async () => ({
            response: JSON.stringify(
              searchIntent({
                route: "search",
                query: "cafes in Osaka",
                focus: "cafes",
                alternatives: [],
                location: "Osaka",
              }),
            ),
          }),
        },
      },
      [{ role: "user", content: "actually Osaka instead" }],
      { query: "cafes in Tokyo", focus: "cafes", alternatives: [], location: "Tokyo" },
    );
    expect(result.location).toBe("Osaka");
    expect(result.query).not.toContain("Tokyo");
  });

  it("keeps a local-language lexical match when classification ranks it below the chat budget", async () => {
    const entries = [
      {
        ...index.entries[0],
        id: "dumplings",
        name: "湯包樂 小籠湯包",
        category: "Dumpling restaurant",
        search_text: "dumpling restaurant",
        note: null,
      },
      ...Array.from({ length: 15 }, (_, i) => ({
        ...index.entries[1],
        id: `other-${i}`,
        name: `Restaurant ${i}`,
        category: "Restaurant",
        search_text: "restaurant",
      })),
    ];
    const input = context(
      [{ role: "user", content: "dumpling" }],
      async (_model, request) => {
        if (request.questions?.place)
          return {
            answers: {
              place: {
                type: "choice",
                probabilities: Object.fromEntries(
                  entries.map((entry, i) => [`place_${i}`, entry.id === "dumplings" ? 0.001 : 0.5]),
                ),
              },
            },
          };
        const catalog = JSON.parse(request.messages[2].content.replace("Candidate places: ", ""));
        expect(catalog).toHaveLength(12);
        expect(catalog.some((place) => place.name === "湯包樂 小籠湯包")).toBe(true);
        return {
          response: JSON.stringify({
            reply: "Try this dumpling shop.",
            place_ids: ["tokyo-japan:dumplings"],
          }),
        };
      },
      { mode: "guide", guide_slug: "tokyo-japan" },
    );
    input.env.ASSETS.fetch = vi.fn(async () => Response.json({ guides: index.guides, entries }));
    const result = await (await onRequestPost(input)).json();
    expect(result.places[0].id).toBe("dumplings");
  });

  it("recognizes a replacement area through a local-language alias without model evidence", async () => {
    const result = await resolveSearchIntent(
      {
        AI: {
          run: async () => ({
            response: JSON.stringify(
              searchIntent({
                route: "search",
                query: "beef noodles in 大安",
                focus: "beef noodles",
                alternatives: [],
                location: "大安",
                location_alternatives: ["Daan"],
              }),
            ),
          }),
        },
      },
      [{ role: "user", content: "what about in Da'an instead" }],
      {
        query: "beef noodles in Ximen",
        focus: "beef noodles",
        alternatives: [],
        location: "Ximen",
      },
    );
    expect(result.location).toBe("大安");
  });

  it("rejects model suggestions of unrelated dishes when saved data has an explicit food match", async () => {
    const input = context(
      [{ role: "user", content: "soup dumplings" }],
      async (_model, request) => {
        if (request.questions?.place)
          return {
            answers: { place: { type: "choice", probabilities: { place_0: 0.5, place_1: 0.5 } } },
          };
        return {
          response: JSON.stringify({ reply: "Try this.", place_ids: ["tokyo-japan:pork-rice"] }),
        };
      },
      { mode: "guide", guide_slug: "tokyo-japan" },
    );
    input.env.AI.run = vi.fn(async (_model, request) => {
      if (request.messages?.[0].content.startsWith("Resolve"))
        return {
          response: JSON.stringify(
            searchIntent({
              route: "search",
              query: "soup dumplings",
              focus: "soup dumplings",
              alternatives: ["小籠湯包", "dumpling"],
            }),
          ),
        };
      if (request.questions?.place)
        return {
          answers: { place: { type: "choice", probabilities: { place_0: 0.1, place_1: 0.9 } } },
        };
      return {
        response: JSON.stringify({ reply: "Try this.", place_ids: ["tokyo-japan:pork-rice"] }),
      };
    });
    input.env.ASSETS.fetch = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [
          {
            ...index.entries[0],
            id: "dumplings",
            name: "湯包樂 小籠湯包",
            category: "Dumpling restaurant",
          },
          { ...index.entries[1], id: "pork-rice", name: "Pork Rice", category: "Restaurant" },
        ],
      }),
    );
    const result = await (await onRequestPost(input)).json();
    expect(result.places.map((place) => place.id)).toEqual(["dumplings"]);
  });

  it("rejects calls while chat is disabled", async () => {
    const input = context([{ role: "user", content: "Cafe" }]);
    input.env.CHAT_ENABLED = "false";
    const response = await onRequestPost(input);
    expect(response.status).toBe(503);
    expect(input.env.AI.run).not.toHaveBeenCalled();
  });

  it("enforces minimum ratings in discovery mode even when the model selects a low-rated place", async () => {
    const input = context([{ role: "user", content: "Tokyo cafes rated 4.8 stars and up" }]);
    input.env.AI.run = vi.fn(async (_model, request) => {
      if (request.messages?.[0].content.startsWith("Resolve"))
        return {
          response: JSON.stringify(
            searchIntent({
              route: "search",
              query: "Tokyo cafes rated 4.8 stars and up",
              focus: "cafes",
              location: "Tokyo",
              alternatives: ["coffee"],
              min_rating: 4.8,
            }),
          ),
        };
      if (request.questions?.guide)
        return { answers: { guide: { type: "choice", probabilities: { "tokyo-japan": 1 } } } };
      if (request.questions?.place)
        return {
          answers: { place: { type: "choice", probabilities: { place_0: 0.5, place_1: 0.5 } } },
        };
      return {
        response: JSON.stringify({
          reply: "Try these.",
          place_ids: ["tokyo-japan:quiet-cafe", "tokyo-japan:busy-cafe"],
          guide_ids: ["tokyo-japan"],
        }),
      };
    });
    input.env.ASSETS.fetch = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [
          { ...index.entries[0], rating: 3.2 },
          { ...index.entries[1], rating: 4.9 },
        ],
      }),
    );
    const result = await (await onRequestPost(input)).json();
    expect(result.guides[0].places.map((place) => place.id)).toEqual(["busy-cafe"]);
    expect(result.search_context.place_ids).toEqual(["tokyo-japan:busy-cafe"]);
  });

  it("preserves displayed order for first-one references and uses ratings within equally relevant matches", async () => {
    const ai = vi.fn(async (_model, request) => {
      if (request.messages?.[0].content.startsWith("Resolve")) {
        expect(JSON.parse(request.messages[1].content).previous_places[0].name).toBe("Busy Cafe");
        return {
          response: JSON.stringify(
            searchIntent({
              route: "search",
              query: "cafes",
              focus: "cafes",
              alternatives: [],
            }),
          ),
        };
      }
      if (request.questions?.place)
        return {
          answers: { place: { type: "choice", probabilities: { place_0: 0.5, place_1: 0.5 } } },
        };
      return {
        response: JSON.stringify({
          reply: "Try these.",
          place_ids: ["tokyo-japan:quiet-cafe", "tokyo-japan:busy-cafe"],
        }),
      };
    });
    const input = context([{ role: "user", content: "cafes" }], ai, {
      mode: "guide",
      guide_slug: "tokyo-japan",
    });
    const assets = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [
          { ...index.entries[0], rating: 4.7 },
          { ...index.entries[1], rating: 4.9 },
        ],
      }),
    );
    input.env.ASSETS.fetch = assets;
    const first = await (await onRequestPost(input)).json();
    expect(first.places[0].id).toBe("busy-cafe");
    expect(first.search_context.place_ids).toEqual([
      "tokyo-japan:busy-cafe",
      "tokyo-japan:quiet-cafe",
    ]);
    const next = context(
      [
        { role: "user", content: "cafes" },
        { role: "assistant", content: first.reply },
        { role: "user", content: "tell me about the first one" },
      ],
      ai,
      { mode: "guide", guide_slug: "tokyo-japan", search_context: first.search_context },
    );
    next.env.ASSETS.fetch = assets;
    const second = await (await onRequestPost(next)).json();
    expect(second.places.map((place) => place.id)).toEqual(["busy-cafe"]);
  });

  it("prefers relevance over a higher rating, but uses rating between comparable matches", async () => {
    const input = context(
      [{ role: "user", content: "cafes" }],
      async (_model, request) => {
        if (request.questions?.place)
          return {
            answers: {
              place: {
                type: "choice",
                probabilities: { place_0: 0.9, place_1: 0.85, place_2: 0.3 },
              },
            },
          };
        return {
          response: JSON.stringify({
            reply: "Try these.",
            place_ids: [
              "tokyo-japan:quiet-cafe",
              "tokyo-japan:busy-cafe",
              "tokyo-japan:weak-match",
            ],
          }),
        };
      },
      { mode: "guide", guide_slug: "tokyo-japan" },
    );
    input.env.ASSETS.fetch = vi.fn(async () =>
      Response.json({
        guides: index.guides,
        entries: [
          { ...index.entries[0], rating: 4.5 },
          { ...index.entries[1], rating: 4.7 },
          { ...index.entries[1], id: "weak-match", name: "Weak Match Cafe", rating: 4.9 },
        ],
      }),
    );
    const result = await (await onRequestPost(input)).json();
    expect(result.places.map((place) => place.id)).toEqual([
      "busy-cafe",
      "quiet-cafe",
      "weak-match",
    ]);
  });

  it("accepts a full-length multibyte conversation without trapping retries at the byte limit", async () => {
    const messages = Array.from({ length: 7 }, (_, i) => ({
      role: i % 2 ? "assistant" : "user",
      content: "珈".repeat(i % 2 ? 260 : 500),
    }));
    const input = context(
      messages,
      vi.fn(async (_model, request) => {
        if (request.messages?.[0].content.startsWith("Resolve"))
          return {
            response: JSON.stringify(
              searchIntent({
                route: "search",
                query: "cafes",
                focus: "cafes",
                alternatives: [],
              }),
            ),
          };
        if (request.questions?.place)
          return { answers: { place: { type: "choice", probabilities: { place_0: 1 } } } };
        return { response: JSON.stringify({ reply: "Coffee ideas", place_ids: [] }) };
      }),
      { mode: "guide", guide_slug: "tokyo-japan" },
    );
    expect(new TextEncoder().encode(await input.request.clone().text()).length).toBeGreaterThan(
      6000,
    );
    expect((await onRequestPost(input)).status).toBe(200);
  });

  it("rejects cross-origin and non-JSON requests before inference", async () => {
    const crossOrigin = context([{ role: "user", content: "Cafe" }]);
    crossOrigin.request.headers.set("Origin", "https://another.example");
    expect((await onRequestPost(crossOrigin)).status).toBe(403);
    expect(crossOrigin.env.AI.run).not.toHaveBeenCalled();

    const wrongType = context([{ role: "user", content: "Cafe" }]);
    wrongType.request.headers.set("Content-Type", "text/plain");
    expect((await onRequestPost(wrongType)).status).toBe(415);
    expect(wrongType.env.AI.run).not.toHaveBeenCalled();
  });

  it("accepts structured Cloudflare responses", async () => {
    const result = await completeChat(
      { AI: { run: vi.fn(async () => ({ response: { reply: "Hello", place_ids: [] } })) } },
      [{ role: "user", content: "Hi" }],
    );
    expect(JSON.parse(result)).toEqual({ reply: "Hello", place_ids: [] });
  });

  it("supports an OpenAI-compatible provider without changing the app contract", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        Response.json({ choices: [{ message: { content: '{"reply":"Hello","place_ids":[]}' } }] }),
      );
    try {
      const content = await completeChat(
        {
          CHAT_PROVIDER: "openai-compatible",
          CHAT_MODEL: "small-model",
          CHAT_API_BASE_URL: "https://provider.example/v1",
          CHAT_API_KEY: "test-key",
        },
        [{ role: "user", content: "Hi" }],
      );
      expect(content).toContain("Hello");
      expect(fetchMock).toHaveBeenCalledWith(
        new URL("https://provider.example/v1/chat/completions"),
        expect.objectContaining({ method: "POST" }),
      );
    } finally {
      fetchMock.mockRestore();
    }
  });
});

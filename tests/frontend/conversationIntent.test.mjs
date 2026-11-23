import { describe, expect, it, vi } from "vitest";
import { onRequestPost } from "../../functions/api/chat.js";
import { completeChat, resolveSearchIntent } from "../../functions/lib/chat-provider.js";
import { searchIntent } from "../helpers/search-intent.mjs";

const previous = {
  version: 1,
  focus: "ramen",
  alternatives: ["ramen noodles"],
  location: "Tokyo",
  location_alternatives: [],
  preferences: ["quiet"],
  min_rating: 0,
};
const user = (content) => ({ role: "user", content });
const assistant = (content) => ({ role: "assistant", content });
function model(proposal) {
  const run = vi.fn(async () => ({ response: JSON.stringify(proposal) }));
  return { env: { AI: { run } }, run };
}

describe("model-owned conversation search intent", () => {
  it("resolves an implied correction from an earlier turn without requiring words in the latest message", async () => {
    const conversation = [
      user("Find ramen in Tokyo"),
      assistant("Ramen options."),
      user("And uni places"),
      assistant("More ramen."),
      user("No, I meant that last ingredient."),
    ];
    const proposal = searchIntent({
      route: "search",
      turn_type: "correction",
      focus: "sea urchin",
      alternatives: ["uni", "ウニ"],
      location: "Tokyo",
      preferences: ["quiet"],
    });
    const { env, run } = model(proposal);
    const result = await resolveSearchIntent(env, conversation, {
      ...previous,
      preferences: ["Uni place", "quiet"],
      turn_type: "correction",
      extra_instruction: "SECRET_UNUSED_STATE",
    });
    const payload = JSON.parse(run.mock.calls[0][1].messages[1].content);
    expect(run.mock.calls[0][1].messages.slice(2)).toEqual(conversation);
    expect(payload.previous_state.preferences).toEqual(["quiet"]);
    expect(payload.previous_state).not.toHaveProperty("turn_type");
    expect(JSON.stringify(payload)).not.toContain("SECRET_UNUSED_STATE");
    expect(result).toMatchObject({
      turn_type: "correction",
      focus: "sea urchin",
      location: "Tokyo",
    });
    expect(result.query).not.toContain("ramen");
    expect(run).toHaveBeenCalledOnce();
  });

  it.each([
    [
      "back to the first neighborhood",
      { turn_type: "correction", location: "Ximen", location_alternatives: ["西門町"] },
      { location: "Ximen" },
    ],
    [
      "four and a half stars or better",
      { turn_type: "refinement", min_rating: 4.5 },
      { min_rating: 4.5 },
    ],
    [
      "I don't care about stars anymore",
      { turn_type: "refinement", min_rating: 0 },
      { min_rating: 0 },
    ],
    [
      "let's look across the city instead",
      { turn_type: "refinement", location: "" },
      { location: "" },
    ],
    [
      "more relaxed",
      { turn_type: "refinement", preferences: ["quiet", "cozy"] },
      { focus: "ramen", preferences: ["quiet", "cozy"] },
    ],
    ["try again", { turn_type: "retry" }, { focus: "ramen", location: "Da'an", min_rating: 4.8 }],
    [
      "time for something different",
      { turn_type: "new_search", focus: "sushi", location: "Tokyo", min_rating: 0 },
      { focus: "sushi", min_rating: 0 },
    ],
  ])("applies the validated snapshot for %s", async (latest, change, expected) => {
    const state = { ...previous, location: "Da'an", min_rating: 4.8 };
    const { env } = model(searchIntent({ route: "search", ...state, ...change }));
    const result = await resolveSearchIntent(
      env,
      [
        user("Ramen in Ximen"),
        assistant("Places."),
        user("Da'an instead"),
        assistant("More places."),
        user(latest),
      ],
      state,
    );
    expect(result).toMatchObject({ ...expected, turn_type: change.turn_type });
    expect(state).toMatchObject({ location: "Da'an", min_rating: 4.8 });
  });

  it.each([
    { turn_type: undefined },
    { turn_type: "guess_from_keywords" },
    { location: undefined },
    { location: 42 },
    { location_alternatives: undefined },
    { preferences: ["invented-filter"] },
    { min_rating: 99 },
    { min_rating: undefined },
    { focus: "a".repeat(81) },
    { alternatives: ["one", "two", "three", "four"] },
  ])("rejects an invalid full snapshot instead of substituting old slots: %j", async (invalid) => {
    const { env } = model(searchIntent({ route: "search", focus: "sushi", ...invalid }));
    await expect(resolveSearchIntent(env, [user("that other thing")], previous)).rejects.toThrow(
      /Invalid/,
    );
  });

  it("bounds conversational turns without granting them system roles", async () => {
    const conversation = Array.from({ length: 11 }, (_, i) =>
      i % 2 ? assistant(`assistant-${i}`) : user(`user-${i} ${"x".repeat(600)}`),
    );
    const { env, run } = model(searchIntent({ route: "search", focus: "ramen" }));
    await resolveSearchIntent(env, conversation, previous);
    const request = run.mock.calls[0][1];
    expect(request.messages.map((message) => message.role)).toEqual([
      "system",
      "user",
      ...conversation.slice(-7).map((message) => message.role),
    ]);
    const history = request.messages.slice(2);
    expect(history).toHaveLength(7);
    expect(history[0].content).toContain("user-4");
    expect(history.every((turn) => turn.content.length <= 500)).toBe(true);
    expect(request.messages[0].content).toContain("untrusted data");
  });
});

describe("provider output contracts", () => {
  it("requests a strict intent schema from the default Cloudflare model", async () => {
    const { env, run } = model(searchIntent({ route: "search", focus: "ramen" }));
    await resolveSearchIntent(env, [user("ramen")], null);
    expect(run.mock.calls[0][1].response_format).toMatchObject({
      type: "json_schema",
      json_schema: { name: "resolve_place_search", strict: true },
    });
    const schema = run.mock.calls[0][1].response_format.json_schema.schema;
    expect(schema.type).toBe("object");
    expect(schema.anyOf).toBeUndefined();
    expect(schema.additionalProperties).toBe(false);
    expect(schema.required).toEqual(Object.keys(schema.properties));
    expect(schema.properties.route.enum).toEqual(["search", "redirect"]);
  });
  it("uses an object-root schema for opted-in OpenAI providers and validates search slots", async () => {
    const fetchMock = vi.fn(async (_url, request) => {
      const format = JSON.parse(request.body).response_format;
      expect(format.type).toBe("json_schema");
      expect(format.json_schema.strict).toBe(true);
      expect(format.json_schema.schema.type).toBe("object");
      expect(format.json_schema.schema.anyOf).toBeUndefined();
      return Response.json({ choices: [{ message: { content: JSON.stringify(reply) } }] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const env = {
      CHAT_PROVIDER: "openai-compatible",
      CHAT_API_BASE_URL: "https://api.openai.com/v1",
      CHAT_API_KEY: "test-key",
      CHAT_MODEL: "test-model",
      CHAT_JSON_SCHEMA: "true",
    };
    let reply = searchIntent({ route: "search", focus: "ramen" });
    try {
      expect(await resolveSearchIntent(env, [user("ramen")], null)).toMatchObject({
        focus: "ramen",
      });
      reply = Object.fromEntries(
        Object.keys(reply).map((key) => [key, key === "route" ? "redirect" : null]),
      );
      expect(await resolveSearchIntent(env, [user("write a poem")], null)).toEqual({
        route: "redirect",
      });
      reply = searchIntent({ route: "search", focus: null });
      await expect(resolveSearchIntent(env, [user("ramen")], null)).rejects.toThrow(
        "Invalid resolved search slots",
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it("keeps JSON mode for other models unless schema support is explicitly enabled", async () => {
    const { env, run } = model({ reply: "ok", place_ids: [] });
    const format = { name: "test_format", schema: { type: "object" } };
    await completeChat({ ...env, CHAT_MODEL: "another-model" }, [], format);
    expect(run.mock.calls[0][1].response_format).toEqual({ type: "json_object" });
    await completeChat(
      { ...env, CHAT_MODEL: "another-model", CHAT_JSON_SCHEMA: "true" },
      [],
      format,
    );
    expect(run.mock.calls[1][1].response_format).toMatchObject({
      type: "json_schema",
      json_schema: { name: "test_format" },
    });
    expect(run).toHaveBeenCalledTimes(2);
  });
});

it("returns no matches without asking for recommendations when every candidate was excluded", async () => {
  const run = vi.fn(async () => ({
    response: JSON.stringify(
      searchIntent({ route: "search", turn_type: "retry", focus: "ramen", location: "Tokyo" }),
    ),
  }));
  const response = await onRequestPost({
    request: new Request("https://example.com/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: "guide",
        guide_slug: "tokyo-japan",
        messages: [user("others")],
        search_context: { ...previous, place_ids: ["tokyo-japan:one"] },
      }),
    }),
    env: {
      CHAT_ENABLED: "true",
      AI: { run },
      ASSETS: {
        fetch: async () =>
          Response.json({
            guides: [{ slug: "tokyo-japan", city: "Tokyo", country: "Japan", title: "Tokyo" }],
            entries: [
              {
                id: "one",
                guide_slug: "tokyo-japan",
                name: "Ramen Shop",
                category: "Ramen restaurant",
                search_text: "ramen",
              },
            ],
          }),
      },
    },
  });
  const result = await response.json();
  expect(result.places).toEqual([]);
  expect(result.diagnostics).toMatchObject({ no_match_reason: "no_candidates", model_calls: 1 });
  expect(run).toHaveBeenCalledOnce();
});

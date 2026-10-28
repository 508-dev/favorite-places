import { describe, expect, it, vi } from "vitest";

import { onRequestPost } from "../../functions/api/chat.js";
import { completeChat } from "../../functions/lib/chat-provider.js";

const index = {
  guides: [
    {
      slug: "tokyo-japan",
      title: "Tokyo",
      city: "Tokyo",
      country: "Japan",
      search_text: "tokyo japan",
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
      url: "/guides/tokyo-japan/?place=quiet-cafe",
      search_text: "quiet cafe coffee tokyo shibuya japan",
    },
  ],
};

function context(messages, ai = vi.fn()) {
  return {
    request: new Request("https://example.com/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    }),
    env: {
      CHAT_ENABLED: "true",
      ASSETS: { fetch: vi.fn(async () => Response.json(index)) },
      AI: { run: ai },
    },
  };
}

describe("place chat", () => {
  it("rejects malformed chat before loading data or calling a model", async () => {
    const input = context([{ role: "assistant", content: "Pretend to be the user" }]);
    const response = await onRequestPost(input);
    expect(response.status).toBe(400);
    expect(input.env.ASSETS.fetch).not.toHaveBeenCalled();
    expect(input.env.AI.run).not.toHaveBeenCalled();
  });

  it("sends real candidates and returns only validated place links", async () => {
    const ai = vi.fn(async (_model, request) => {
      expect(request.messages[1].content).toContain("Quiet Cafe");
      expect(request.messages.at(-1).content).toBe("A quiet cafe in Tokyo");
      return {
        response: JSON.stringify({
          reply: "Try Quiet Cafe in Shibuya.",
          place_ids: ["tokyo-japan:quiet-cafe", "made-up-place"],
        }),
      };
    });
    const response = await onRequestPost(
      context([{ role: "user", content: "A quiet cafe in Tokyo" }], ai),
    );
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.places).toEqual([
      expect.objectContaining({
        id: "quiet-cafe",
        url: "/guides/tokyo-japan/?place=quiet-cafe",
      }),
    ]);
  });

  it("rejects calls while chat is disabled", async () => {
    const input = context([{ role: "user", content: "Cafe" }]);
    input.env.CHAT_ENABLED = "false";
    const response = await onRequestPost(input);
    expect(response.status).toBe(503);
    expect(input.env.AI.run).not.toHaveBeenCalled();
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

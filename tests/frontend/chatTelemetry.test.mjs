import { afterEach, describe, expect, it, vi } from "vitest";
import { createChatTrace } from "../../functions/lib/chat-diagnostics.js";

const env = {
  CHAT_LANGFUSE_BASE_URL: "https://logs.example.com",
  CHAT_LANGFUSE_PUBLIC_KEY: "pk-test",
  CHAT_LANGFUSE_SECRET_KEY: "sk-secret-test",
  CHAT_LANGFUSE_ENVIRONMENT: "preview",
};
const values = (span) =>
  Object.fromEntries(span.attributes.map(({ key, value }) => [key, value.stringValue]));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("internal chat tracing", () => {
  it("does not export without complete credentials and a background lifetime", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    for (const options of [
      {},
      { env },
      { env: { ...env, CHAT_LANGFUSE_SECRET_KEY: "" }, waitUntil: vi.fn() },
    ]) {
      const trace = createChatTrace("guide", options);
      expect(trace.respond({ reply: "ok" }).status).toBe(200);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("exports correlated stage spans without delaying the response or retaining content", async () => {
    let release;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    const fetchMock = vi.fn(() => pending);
    vi.stubGlobal("fetch", fetchMock);
    const waitUntil = vi.fn();
    const trace = createChatTrace("guide", {
      env,
      waitUntil,
      input: { latest_request: "private request" },
    });
    trace.details.guide_slug = "tokyo-japan";
    await trace.model("resolve_intent", async () => ({ focus: "private focus" }));
    await trace.model("rank_places", async () => [], false);
    const response = trace.respond({
      places: [{ id: "one" }],
      reply: "private reply",
      search_context: { focus: "private focus" },
    });
    expect(response.status).toBe(200);
    expect(waitUntil).toHaveBeenCalledTimes(1);
    const [url, request] = fetchMock.mock.calls[0];
    expect(url.href).toBe("https://logs.example.com/api/public/otel/v1/traces");
    expect(request.redirect).toBe("manual");
    const payload = JSON.parse(request.body);
    const spans = payload.resourceSpans[0].scopeSpans[0].spans;
    expect(spans.map((span) => span.name)).toEqual([
      "guide.search",
      "resolve_intent",
      "rank_places",
    ]);
    expect(spans[0].traceId).toBe(response.headers.get("X-Chat-Request-Id").replaceAll("-", ""));
    expect(spans[1].parentSpanId).toBe(spans[0].spanId);
    expect(BigInt(spans[1].endTimeUnixNano)).toBeGreaterThanOrEqual(
      BigInt(spans[1].startTimeUnixNano),
    );
    expect(values(spans[0])["langfuse.environment"]).toBe("preview");
    expect(request.body).not.toMatch(/private|sk-secret-test|pk-test/);
    expect(JSON.stringify(await response.json())).not.toContain("langfuse");
    release(Response.json({}));
    await waitUntil.mock.calls[0][0];
  });

  it("captures only the explicitly enabled bounded input and structured output", async () => {
    const fetchMock = vi.fn(async () => Response.json({}));
    vi.stubGlobal("fetch", fetchMock);
    const waitUntil = vi.fn();
    const trace = createChatTrace("discover", {
      env: { ...env, CHAT_LANGFUSE_CAPTURE_CONTENT: "true" },
      waitUntil,
      input: { latest_request: "quiet cafes" },
    });
    trace.respond({
      reply: "unlogged assistant prose",
      guides: [{ places: [{ id: "one" }] }],
      search_context: { focus: "cafe" },
      actions: [{ id: "guide.apply-filters" }],
    });
    await waitUntil.mock.calls[0][0];
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    const root = values(body.resourceSpans[0].scopeSpans[0].spans[0]);
    expect(JSON.parse(root["langfuse.observation.input"])).toEqual({
      latest_request: "quiet cafes",
    });
    expect(JSON.parse(root["langfuse.observation.output"])).toEqual({
      search_context: { focus: "cafe" },
      place_ids: ["one"],
      action_ids: ["guide.apply-filters"],
    });
    expect(JSON.stringify(body)).not.toContain("unlogged assistant prose");
  });

  it.each([
    "timeout",
    "http",
    "partial",
    "redirect",
  ])("isolates %s export failures from chat and redacts error details", async (failure) => {
    const fetchMock = vi.fn(async () => {
      if (failure === "timeout") throw new Error("secret upstream response");
      if (failure === "redirect")
        return new Response(null, {
          status: 302,
          headers: { Location: "https://other.example.com" },
        });
      return failure === "http"
        ? new Response("secret upstream response", { status: 403 })
        : Response.json({
            partialSuccess: { rejectedSpans: "1", errorMessage: "secret upstream response" },
          });
    });
    vi.stubGlobal("fetch", fetchMock);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const waitUntil = vi.fn();
    const trace = createChatTrace("guide", { env, waitUntil });
    await expect(
      trace.model("recommend", async () => {
        throw new Error("secret model response");
      }),
    ).rejects.toThrow("secret model response");
    const response = trace.respond({ error: "Friendly error", code: "ai_unavailable" }, 503);
    await expect(waitUntil.mock.calls[0][0]).resolves.toBeUndefined();
    expect(response.status).toBe(503);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(JSON.parse(warn.mock.calls[0][0])).toMatchObject({
      event: "chat.telemetry_failed",
      reason:
        failure === "partial"
          ? "rejected_spans"
          : failure === "timeout"
            ? "export_failed"
            : "http_error",
    });
    expect(JSON.stringify(warn.mock.calls)).not.toContain("secret");
    const spans = JSON.parse(fetchMock.mock.calls[0][1].body).resourceSpans[0].scopeSpans[0].spans;
    expect(spans[0].status.code).toBe(2);
    expect(spans[1].status.code).toBe(2);
  });
});

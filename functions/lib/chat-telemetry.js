// OTLP/HTTP JSON keeps this exporter usable in Pages without a Node SDK/runtime.
const attribute = (key, value) => ({ key, value: { stringValue: String(value) } });
const nanos = (milliseconds) => String(BigInt(milliseconds) * 1_000_000n);
const spanId = () => crypto.randomUUID().replaceAll("-", "").slice(0, 16);
const exportError = (reason, status) =>
  Object.assign(new Error("Chat telemetry export failed"), {
    telemetryReason: reason,
    telemetryStatus: status,
  });

export async function exportChatTrace(env, diagnostics, stages, started, content = null) {
  const endpoint = new URL("/api/public/otel/v1/traces", env.CHAT_LANGFUSE_BASE_URL);
  if (endpoint.protocol !== "https:") throw exportError("invalid_endpoint");
  const traceId = diagnostics.request_id.replaceAll("-", "");
  const rootId = spanId();
  const name = diagnostics.mode === "guide" ? "guide.search" : "discover.search";
  const shared = [
    attribute("langfuse.trace.name", name),
    attribute("langfuse.environment", env.CHAT_LANGFUSE_ENVIRONMENT || "production"),
    attribute("langfuse.trace.metadata.request_id", JSON.stringify(diagnostics.request_id)),
    attribute("langfuse.trace.metadata.contract_version", "1"),
    attribute("langfuse.trace.metadata.runtime_owner", JSON.stringify("pages")),
  ];
  const rootAttributes = [
    ...shared,
    attribute("langfuse.observation.type", "span"),
    attribute("langfuse.observation.metadata", JSON.stringify(diagnostics)),
    attribute("langfuse.trace.metadata", JSON.stringify(diagnostics)),
  ];
  if (content) {
    rootAttributes.push(
      attribute("langfuse.observation.input", JSON.stringify(content.input)),
      attribute("langfuse.observation.output", JSON.stringify(content.output)),
    );
  }
  const spans = [
    {
      traceId,
      spanId: rootId,
      name,
      kind: 1,
      startTimeUnixNano: nanos(started),
      endTimeUnixNano: nanos(started + diagnostics.elapsed_ms),
      attributes: rootAttributes,
      status: { code: diagnostics.outcome === "error" ? 2 : 1 },
    },
    ...stages.map((stage) => ({
      traceId,
      spanId: spanId(),
      parentSpanId: rootId,
      name: stage.name,
      kind: 1,
      startTimeUnixNano: nanos(stage.started),
      endTimeUnixNano: nanos(stage.ended),
      attributes: [
        ...shared,
        attribute("langfuse.observation.type", "span"),
        attribute("langfuse.observation.metadata.model_call", stage.model_call),
        attribute("langfuse.observation.metadata.provider", env.CHAT_PROVIDER || "cloudflare"),
      ],
      status: { code: stage.failed ? 2 : 1 },
    })),
  ];
  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      Authorization: `Basic ${btoa(`${env.CHAT_LANGFUSE_PUBLIC_KEY}:${env.CHAT_LANGFUSE_SECRET_KEY}`)}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      resourceSpans: [
        {
          resource: { attributes: [attribute("service.name", "favorite-places-chat")] },
          scopeSpans: [{ scope: { name: "favorite-places.chat", version: "1" }, spans }],
        },
      ],
    }),
    signal: AbortSignal.timeout(5000),
    redirect: "manual",
  });
  if (!response.ok) throw exportError("http_error", response.status);
  // OTLP can acknowledge the request while rejecting individual spans.
  const acknowledgement = await response.json();
  if (Number(acknowledgement?.partialSuccess?.rejectedSpans || 0) > 0)
    throw exportError("rejected_spans");
}

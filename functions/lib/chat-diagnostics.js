import { exportChatTrace } from "./chat-telemetry.js";

// Console traces contain metadata only; search text is omitted.
export function createChatTrace(mode, options = {}) {
  const start = Date.now();
  const stages = [];
  const { env = {}, waitUntil, input = null } = options;
  const details = {
    version: 1,
    request_id: crypto.randomUUID(),
    mode,
    stage: "index",
    guide_slug: null,
    counts: {
      guide_entries: 0,
      scoped_entries: 0,
      shortlist: 0,
      candidates: 0,
      results: 0,
      actions: 0,
    },
    timings_ms: {},
    model_calls: 0,
  };
  return {
    details,
    async model(stage, call, usesModel = true) {
      details.stage = stage;
      if (usesModel) details.model_calls += 1;
      const started = Date.now();
      let failed = false;
      try {
        return await call();
      } catch (error) {
        failed = true;
        throw error;
      } finally {
        const ended = Date.now();
        details.timings_ms[stage] = ended - started;
        stages.push({ name: stage, started, ended, failed, model_call: usesModel });
      }
    },
    respond(data, status = 200) {
      const results = data.guides
        ? data.guides.flatMap((guide) => guide.places || [])
        : data.places || [];
      details.counts.results = results.length;
      details.counts.actions = data.actions?.length || 0;
      const diagnostics = {
        ...details,
        outcome:
          status >= 400
            ? "error"
            : data.outcome === "redirect"
              ? "redirect"
              : results.length
                ? "results"
                : data.actions?.length
                  ? "filters"
                  : "no_matches",
        elapsed_ms: Date.now() - start,
        ...(data.code ? { code: data.code } : {}),
      };
      console.info(
        JSON.stringify({
          event: "chat.turn",
          runtime_owner: "pages",
          contract_version: 1,
          ...diagnostics,
        }),
      );
      if (
        env.CHAT_LANGFUSE_BASE_URL &&
        env.CHAT_LANGFUSE_PUBLIC_KEY &&
        env.CHAT_LANGFUSE_SECRET_KEY &&
        waitUntil
      ) {
        const content =
          env.CHAT_LANGFUSE_CAPTURE_CONTENT === "true"
            ? {
                input,
                output: {
                  search_context: data.search_context || null,
                  place_ids: results.map((place) => place.id),
                  action_ids: (data.actions || []).map((action) => action.id),
                },
              }
            : null;
        const exporting = exportChatTrace(env, diagnostics, stages, start, content).catch(
          (error) => {
            // Do not print provider bodies, credentials or request content on export failure.
            console.warn(
              JSON.stringify({
                event: "chat.telemetry_failed",
                request_id: details.request_id,
                reason:
                  error.name === "TimeoutError"
                    ? "timeout"
                    : error.telemetryReason || "export_failed",
                ...(Number.isInteger(error.telemetryStatus)
                  ? { http_status: error.telemetryStatus }
                  : {}),
              }),
            );
          },
        );
        waitUntil(exporting);
      }
      return Response.json(
        { ...data, diagnostics },
        {
          status,
          headers: { "Cache-Control": "no-store", "X-Chat-Request-Id": details.request_id },
        },
      );
    },
  };
}

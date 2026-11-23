import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";
import { SEARCH_TURN_TYPES } from "../functions/lib/chat-provider.js";
import { SEARCH_SCOPE_REPLY } from "../functions/lib/chat-scope.js";

export function evaluateTurn(result, expected = {}) {
  const failures = [];
  const expectedOutcome = expected.outcome || "search";
  if (result.outcome !== expectedOutcome) failures.push(`expected ${expectedOutcome} outcome`);
  if (result.outcome === "redirect") {
    if (result.reply !== SEARCH_SCOPE_REPLY)
      failures.push("redirect leaked arbitrary assistant prose");
    if (
      result.places?.length ||
      result.guides?.length ||
      result.actions?.length ||
      result.search_context
    )
      failures.push("redirect returned artifacts or replaced search state");
    return failures;
  }
  if (result.outcome !== "search") failures.push("missing search outcome");
  const query = result.search_context?.query || "";
  const places = [
    ...(result.places || []),
    ...(result.guides || []).flatMap((guide) => guide.places || []),
  ];
  const matches = (pattern, value) => new RegExp(pattern, "iu").test(value);
  if (typeof result.reply !== "string" || !query || result.search_context?.version !== 1)
    failures.push("missing reply or canonical search context");
  if (expected.min_results && places.length < expected.min_results)
    failures.push(`fewer than ${expected.min_results} results`);
  const turnType = result.search_context?.turn_type;
  if (!SEARCH_TURN_TYPES.includes(turnType)) failures.push("missing valid search turn type");
  if (expected.turn_types && !expected.turn_types.includes(turnType))
    failures.push(`unexpected turn type ${turnType}`);
  const focus = result.search_context?.focus || "";
  if (expected.focus_includes && !matches(expected.focus_includes, focus))
    failures.push(`unexpected focus ${focus}`);
  if (
    expected.preferences_exclude &&
    matches(expected.preferences_exclude, (result.search_context?.preferences || []).join(" "))
  )
    failures.push("food leaked into preference state");
  if (places.length > 6) failures.push("more than six results");
  if (places.some((place) => typeof place.id !== "string" || !place.url?.startsWith("/guides/")))
    failures.push("invalid place link");
  for (const pattern of expected.query_includes || [])
    if (!matches(pattern, query)) failures.push(`query missing ${pattern}`);
  for (const pattern of expected.query_excludes || [])
    if (matches(pattern, query)) failures.push(`query retains ${pattern}`);
  if (
    expected.results_include &&
    !places.some((place) =>
      matches(expected.results_include, `${place.name} ${place.category} ${place.note}`),
    )
  )
    failures.push(`no result matching ${expected.results_include}`);
  if (expected.names_exclude && places.some((place) => matches(expected.names_exclude, place.name)))
    failures.push(`stale result matching ${expected.names_exclude}`);
  if (
    expected.max_distance_km &&
    places.some(
      (place) =>
        typeof place.distance_km !== "number" || place.distance_km > expected.max_distance_km,
    )
  )
    failures.push("result outside the requested area radius");
  if (
    expected.neighborhood_excludes &&
    places.some((place) => matches(expected.neighborhood_excludes, place.neighborhood || ""))
  )
    failures.push("result in an explicitly excluded neighborhood");
  if (expected.guide_slug && !result.guides?.some((guide) => guide.slug === expected.guide_slug))
    failures.push(`missing guide ${expected.guide_slug}`);
  if (expected.guide_filters) {
    const action = result.actions?.find((action) => action.id === "guide.apply-filters");
    const actual = action?.filters;
    const wanted = expected.guide_filters;
    if (
      !actual ||
      actual.query !== wanted.query ||
      actual.min_rating !== wanted.min_rating ||
      JSON.stringify(actual.tags) !== JSON.stringify(wanted.tags)
    )
      failures.push("guide filters differ from the requested broad search");
    const preferences = result.search_context?.preferences || [];
    if (
      JSON.stringify(preferences) !== JSON.stringify(wanted.tags) ||
      result.search_context?.min_rating !== wanted.min_rating
    )
      failures.push("intent invented unrequested filter constraints");
  }
  if (expected.min_rating) {
    if (
      !places.length ||
      places.some((place) => typeof place.rating !== "number" || place.rating < expected.min_rating)
    )
      failures.push(`results below ${expected.min_rating} stars`);
    if (
      !result.actions?.some(
        (action) =>
          action.id === "guide.apply-filters" && action.filters?.min_rating === expected.min_rating,
      )
    )
      failures.push("missing requested rating filter action");
  }
  return failures;
}

async function main() {
  const args = process.argv.slice(2);
  const option = (name) => args[args.indexOf(name) + 1];
  const base = args.includes("--base-url") ? option("--base-url") : process.env.CHAT_EVAL_BASE_URL;
  if (!base)
    throw new Error(
      "Set CHAT_EVAL_BASE_URL or pass --base-url pointing to a chat-enabled deployment. This runs live inference and consumes its AI allocation.",
    );
  const origin = new URL(base);
  if (!/^https?:$/.test(origin.protocol)) throw new Error("Expected an HTTP(S) deployment URL");
  const fixtures = JSON.parse(
    await readFile(new URL("../tests/fixtures/chat-evals.json", import.meta.url), "utf8"),
  );
  const cases = args.includes("--case")
    ? fixtures.filter((fixture) => fixture.id === option("--case"))
    : fixtures;
  if (!cases.length) throw new Error("No matching eval cases");
  const reports = [];
  for (const fixture of cases) {
    const messages = [...(fixture.history || [])];
    let context = fixture.initial_context;
    const turns = [];
    for (const turn of fixture.turns) {
      messages.push({ role: "user", content: turn.message });
      const start = Date.now();
      try {
        const response = await fetch(new URL("/api/chat", origin), {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "User-Agent":
              "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
          },
          body: JSON.stringify({
            mode: fixture.mode,
            guide_slug: fixture.guide_slug,
            messages: messages.slice(-7),
            search_context: context,
          }),
          signal: AbortSignal.timeout(60000),
        });
        if (!response.ok) {
          const body = await response.text();
          const description = response.headers.get("content-type")?.includes("json")
            ? body
            : body.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
          throw new Error(`HTTP ${response.status}: ${description.slice(0, 1500)}`);
        }
        const result = await response.json();
        if (result.outcome !== "redirect") context = result.search_context;
        messages.push({ role: "assistant", content: result.reply });
        const failures = evaluateTurn(result, turn.expect);
        turns.push({
          message: turn.message,
          query: context?.query,
          search_context: context,
          elapsed_ms: Date.now() - start,
          diagnostics: result.diagnostics,
          failures,
          place_names: [
            ...(result.places || []),
            ...(result.guides || []).flatMap((guide) => guide.places || []),
          ].map((place) => place.name),
        });
      } catch (error) {
        turns.push({
          message: turn.message,
          elapsed_ms: Date.now() - start,
          failures: [error.message],
        });
        break;
      }
    }
    const passed =
      turns.length === fixture.turns.length && turns.every((turn) => !turn.failures.length);
    reports.push({ id: fixture.id, passed, turns });
    console.log(`${passed ? "PASS" : "FAIL"} ${fixture.id}`);
    for (const turn of turns)
      for (const failure of turn.failures)
        console.log(`  ${turn.message}: ${failure} (query: ${turn.query || "unavailable"})`);
  }
  const summary = {
    timestamp: new Date().toISOString(),
    deployment: origin.origin,
    passed: reports.filter((report) => report.passed).length,
    total: reports.length,
    cases: reports,
  };
  if (args.includes("--report")) {
    const path = option("--report");
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, `${JSON.stringify(summary, null, 2)}\n`);
  }
  console.log(`${summary.passed}/${summary.total} trajectories passed.`);
  if (summary.passed !== summary.total) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

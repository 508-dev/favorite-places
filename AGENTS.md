# AGENTS.md

## Project Summary

`favorite-places` is a static-first Astro site that turns personal Google Maps saved lists into cleaner shareable guides.

- Frontend: Astro + TypeScript
- Data pipeline: Python + `uv`
- Source scraper: vendored git subtree at `vendor/gmaps-scraper/` from `https://github.com/508-dev/gmaps-scraper`
- Hosting target: static deploys on Cloudflare Pages or GitHub Pages

## Working Rules

- Prefer `rg` for search.
- Keep the root [`.python-version`](.python-version) aligned with `pyproject.toml` because Cloudflare Pages otherwise falls back to its default Python runtime.
- For Cloudflare Pages deploys, set `SKIP_DEPENDENCY_INSTALL=true` and `BUN_VERSION=1.3.12`, then use a custom build command that runs `bun ci`, installs `uv==0.11.6`, runs `uv sync`, then `bun run build:data` before `bun run build`.
- Use `uv sync` for Python dependencies.
- Use `bun install` for frontend dependencies.
- Use `bun run check` and `bun run build` before closing out frontend changes.
- Use `.venv/bin/python` if `uv run` hits sandbox cache issues in Codex.

## Agent Workflow

- State assumptions before acting when a request has more than one reasonable interpretation. If an assumption would materially change the result, ask instead of guessing.
- Read before you write. Before adding or replacing code, inspect the relevant exports, immediate callers, and obvious shared utilities.
- Keep changes surgical. Touch only what the task requires; do not refactor adjacent code or add speculative abstractions.
- Match the existing codebase conventions for naming, structure, error handling, and tests. If two patterns conflict, pick one, say why, and do not blend them silently.
- Use code for deterministic decisions and the model for judgment calls. Do not push routing, retries, status handling, or other rule-based transforms into prompts or model output when code can decide directly.
- For non-trivial changes, reason through state ownership, observability, blast radius, and timing before shipping. Surface risks instead of hand-waving them away.
- Verify intent, not just motion. "Done" is wrong if important work was skipped silently, and "tests pass" is not enough if the tests would still pass after breaking the intended behavior.
- Checkpoint multi-step work. After each significant step, be able to say what changed, what was verified, and what remains.

## Self-Hosted Refresh Runner

The `data-refresh` workflow is intentionally tied to a self-hosted Linux runner. Treat the runner image or host provisioning as the contract for OS-level tools and browser libraries; do not install them ad hoc inside the workflow.

- Keep upstream runner labels generic. The workflow defaults to `["self-hosted","Linux"]`; site repos can pin private pools with the GitHub Actions repository variable `DATA_REFRESH_RUNNER_LABELS`, for example `["self-hosted","Linux","residential"]`.
- `unzip` must be present on `PATH` because `oven-sh/setup-bun` downloads a `.zip` release archive.
- GNU `timeout` must be present on `PATH`; the workflow stops the refresh command after its soft timeout, defaults to 150 minutes, caps manual values below the 180-minute job timeout, then commits any completed partial data changes.
- `cloakbrowser` downloads its own Chromium binary, but the host still needs Playwright/Chromium system libraries installed.
- Provision the runner with the equivalent of Playwright's Chromium Linux dependencies, for example the packages behind `playwright install-deps chromium`, instead of trying to `apt install` during the workflow run.
- The workflow includes a preflight check for `unzip` and a small set of required shared libraries (`libnspr4`, `libnss3`, GTK, GBM, X11, and related browser deps) so runner drift fails fast with a clear error.
- For Ubuntu-based runners, the concrete package fix for the current preflight is `sudo apt-get install -y libnspr4 libnss3 libgbm1 libxcb1 libxkbcommon0`, then `sudo ldconfig`. Verify with `ldconfig -p | grep -E 'libnspr4\.so|libnss3\.so|libgbm\.so\.1|libxcb\.so\.1|libxkbcommon\.so\.0'`. Prefer baking the full Playwright Chromium dependency set into the runner image with `sudo npx playwright install-deps chromium`.
- Set the repo Actions secret `GH_AUTOMATION_TOKEN` to a token with the minimum permissions needed for this automation: `workflows: write` to update refs that include `.github/workflows/**`, `contents: write` to push `automation/data-refresh-*`, and `pull-requests: write` to create or update the refresh PRs. For a classic PAT, include the `workflow` scope in addition to normal `repo` write access. The default `GITHUB_TOKEN` cannot reliably create or advance these automation branches after `.github/workflows/**` changes.

## Environment Variables

- `GOOGLE_MAPS_JS_API_KEY` is read by Astro during render/build and emitted into the page only when the Google map provider is active. Treat it as the browser Google Maps display key: production usage should be on a separate key restricted by HTTP referrer and limited to `Maps JavaScript API`.
- `GOOGLE_PLACES_API_KEY` is the optional server/build-time fallback key for Places enrichment when Google Maps place-page scraping cannot recover enough data. Do not expose it to the browser.
- `PUBLIC_MAP_PROVIDER=leaflet` forces the Leaflet/OpenStreetMap fallback even when `GOOGLE_MAPS_JS_API_KEY` is present.
- `GMAPS_SCRAPER_PROXY` optionally routes scraper traffic through a proxy. The pipeline keeps proxy-specific scraper sessions under `.context/gmaps-scraper/` by default, rotates them when they go stale or get blocked, and can be pointed at a shared absolute path with `FAVORITE_PLACES_GMAPS_SCRAPER_STATE_DIR` when you want reuse across worktrees or the main checkout.
- `FAVORITE_PLACES_GMAPS_SCRAPER_STATE_DIR` optionally overrides the scraper browser-profile and HTTP-cookie-jar root. Use the same absolute path from multiple worktrees if you want to share scraper trust state. A headed run with that same path may still be needed to establish consent or signed-in browser state; the browser profile and curl cookie jar are persisted together under this root but are separate artifacts.
- `FAVORITE_PLACES_SITE_DIR` points both Astro and the Python data pipeline at the site pack. It defaults to `./site`, with `./site.example` as the fresh-checkout fallback.
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and optional `LANGFUSE_BASE_URL` enable Langfuse generation logging for uncached scraper repair and semantic enrichment LLM calls. The pipeline keeps Langfuse off the critical path with short export defaults: `LANGFUSE_TIMEOUT=2`, `LANGFUSE_FLUSH_AT=8`, and `LANGFUSE_FLUSH_INTERVAL=1.0`; override them only when intentionally trading refresh latency for telemetry durability.
- `GMAPS_SCRAPER_LANGFUSE_FULL_CAPTURE=true` opts scraper repair logging into full request/response payload capture. Leave unset by default so scraper repair traces use URL hashes and metadata only.

## Data Practices

This repo deliberately avoids committing generated build artifacts.

- `site/data/raw/` may be committed when you want reproducible scraped snapshots in git.
- `site/data/cache/places.sqlite` is the canonical enrichment cache artifact when you want reproducible enrichment snapshots in git.
- `src/data/generated/` is local-only generated site input data.
- `public/data/search-index.json` is local-only generated browser search data.
- `site/overrides/` is the source-controlled layer for handwritten curation.
- `site/list_sources.json` is safe to commit if it only contains public list URLs you are comfortable sharing.
- In `site/list_sources.json`, `slug` is required. `type` is inferred for supported Google Maps `url` sources and local CSV `path` sources; explicit `type` is allowed but must match the configured `url` or `path`. Google My Maps URLs are not supported yet. `google_export_csv` sources still require `title`; `title` is optional for Google Maps URL sources as a fallback if the source data cannot recover the real title.

Merge precedence:

1. Manual overrides
2. Google Places enrichment cache
3. Raw scraped list data

## Common Commands

Install dependencies:

```bash
uv sync
bun install
```

Populate local raw data from public Google Maps lists:

```bash
bun run sync:sources
```

This refreshes every configured source and then rebuilds generated site data. URL sources skip network refreshes until their `refresh_after` window expires unless the source config changes; CSV sources skip rewrites when their input hash is unchanged.

Force-refresh all configured raw source imports:

```bash
bun run sync:sources:force
```

Refresh one configured raw source by slug, source URL, or source path:

```bash
bun run sync:source -- tokyo-japan
```

Build generated JSON and search index data from local raw data:

```bash
bun run build:data
```

Use this when raw snapshots are already current and you only need to regenerate site inputs.
Configured local CSV sources are auto-imported into `site/data/raw/<slug>.json` before rebuild. Public URL sources are not refreshed here.

Fill or refresh Google Places enrichment cache:

```bash
GOOGLE_PLACES_API_KEY=... bun run enrich:data
GOOGLE_PLACES_API_KEY=... bun run fill:gaps
GOOGLE_PLACES_API_KEY=... bun run refresh:enrichment
```

Optional debug export of per-guide cache JSON from SQLite:

```bash
bun run export:cache:json
```

Run the site:

```bash
bun run dev
```

Guide pages default to Google Maps when `GOOGLE_MAPS_JS_API_KEY` is present at build/render time. Without that key, or when `PUBLIC_MAP_PROVIDER=leaflet`, they fall back to Leaflet.

## Notes For Future Agents

- Cloudflare Pages should not use its default `pip install .` auto-detection for this repo; the data pipeline depends on `uv` to resolve the vendored scraper from `vendor/gmaps-scraper`.
- Raw place `note` should flow through as the default fallback recommendation copy.
- Raw place `is_favorite` should flow through as the default top-pick signal.
- Manual `note` is the only handwritten recommendation override and still wins over generated copy.
- Manual `top_pick` overrides still win.
- Manual `vibe_tags` overrides win over rule-derived browser search vibe tags.
- Place enrichment cache entries now carry `input_signature` and `refresh_after`; invalidation is not a single global TTL anymore.
- Raw saved-list snapshots now carry `fetched_at`, `refresh_after`, and `source_signature`; URL sources skip network refreshes until the refresh window expires unless the source config changes, while CSV sources can skip rewrites when the input hash is unchanged.
- The raw-list scraper now uses `gmaps-scraper`, which defaults to `curl_cffi` with browser fallback and supports place-page scraping. The pipeline reuses worktree-local scraper sessions by default, can share them across worktrees when `FAVORITE_PLACES_GMAPS_SCRAPER_STATE_DIR` is set, rotates to a fresh identity when `GMAPS_SCRAPER_PROXY` changes, clears sessions after obvious block/cookie-jar failures, and expires idle sessions after 14 days.
- Enrichment now prefers `gmaps-scraper` place-page scraping and uses `GOOGLE_PLACES_API_KEY` only as a fallback when a place page is blocked, limited, or too thin to trust.
- Do not reintroduce tracked generated JSON unless the user explicitly asks for fixture-style examples in git.

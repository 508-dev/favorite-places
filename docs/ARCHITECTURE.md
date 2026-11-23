# Architecture

This document covers the implementation and operational details behind Favorite Places. The README is intentionally kept focused on what the project is and how to use it.

## Stack

- Astro for the static site
- TypeScript for frontend data loading and UI behavior
- Python + `uv` for scraping, normalization, enrichment, and generated JSON
- `bun` for frontend package management and scripts

The repo pins Python via [../.python-version](../.python-version) so local `uv` usage and Cloudflare Pages builds resolve the intended runtime instead of Cloudflare's default.

## Site Pack Contract

The reusable app reads user-owned content from a site pack.

Resolution order:

1. `FAVORITE_PLACES_SITE_DIR`, when set
2. `./site`, when present
3. `./site.example`

Expected shape:

```txt
site/
  config.ts
  theme.css
  list_sources.json
  data/raw/
  data/cache/places.sqlite
  public/
  content/templates/
  overrides/
```

Site-owned content:

- `site/config.ts`: site identity, nav, map provider, home copy, guide labels, section visibility, and card display fields
- `site/theme.css`: CSS custom-property overrides and targeted site-specific styling
- `site/list_sources.json`: public Google Maps list URLs or local Google Takeout CSV source definitions
- `site/data/raw/`: raw source snapshots
- `site/data/cache/`: reproducible enrichment cache artifacts
- `site/overrides/`: manual curation for lists and places
- `site/content/templates/`: trusted HTML insertion points
- `site/public/`: copied into static builds and served during local dev

App-owned code:

- `src/pages/`
- `src/components/`
- `src/lib/`
- `src/styles/`
- `scripts/`
- `functions/`: optional Cloudflare Pages chat endpoint

## Template Surface

The template API is layered:

1. `site/config.ts` for copy, labels, behavior, and card display toggles
2. CSS custom properties in `site/theme.css`
3. Known HTML insertion points in `site/content/templates/`
4. App-owned components for interactive cards, maps, search, and filtering

Supported template blocks:

```txt
site/content/templates/
  home-aside.html
  home-before-guides.html
  home-after-guides.html
  guide-aside/default.html
  guide-aside/<guide-slug>.html
  guide-before-places/default.html
  guide-before-places/<guide-slug>.html
  guide-after-places/default.html
  guide-after-places/<guide-slug>.html
```

Card markup stays app-owned because search, filtering, maps, and highlighting depend on stable `data-*` attributes. Customize what cards show through config, then customize the look through CSS.

## Source Definitions

`site/list_sources.json` is a list of source definitions. Each source needs a stable `slug`.

- `url` sources infer `type: "google_list_url"` for supported Google Maps links.
- `path` sources infer `type: "google_export_csv"` and require `title`.
- `type` can be included explicitly, but it must match the configured `url` or `path`.
- `title` is optional for Google Maps URL sources and acts as a fallback title.
- Google My Maps URLs such as `https://www.google.com/maps/d/...` are not supported.

Example:

```json
[
  {
    "slug": "tokyo-japan",
    "url": "https://maps.app.goo.gl/your-public-list"
  },
  {
    "slug": "taipei-taiwan",
    "path": "data/imports/taipei-taiwan.csv",
    "title": "Taipei, Taiwan"
  }
]
```

## Data Workflow

Refresh all configured sources:

```bash
bun run sync:sources
```

Force-refresh all configured sources:

```bash
bun run sync:sources:force
```

Refresh one source by slug, source URL, or source path:

```bash
bun run sync:source -- tokyo-japan
bun run sync:source -- https://maps.app.goo.gl/your-public-list
bun run sync:source -- data/imports/taipei-taiwan.csv
```

Build generated data from current raw snapshots:

```bash
bun run build:data
```

Generated outputs:

- `src/data/generated/`: local-only JSON read by Astro
- `public/data/search-index.json`: local-only browser search index

These generated outputs are intentionally gitignored.

## Refresh Profiles

The profile wrapper is the preferred interface for recurring refresh automation:

```bash
bun run refresh:balanced
bun run refresh:backfill
bun run refresh:sweep
```

- `refresh:balanced`: refreshes URL-backed raw sources so saved-list changes are pulled in promptly, keeps CSV sources on content-signature skips unless explicitly selected or force-refreshed, then runs normal incremental enrichment. Missing places and raw-place changes go first; stale cache entries are refreshed afterward according to their TTLs.
- `refresh:backfill`: refreshes due raw sources, then fills only missing enrichment and missing photos.
- `refresh:sweep`: refreshes due raw sources, then force-refreshes every enrichment entry as a periodic consistency sweep.

Suggested self-hosted schedule:

```cron
17 3 * * * cd /srv/favorite-places && bun run refresh:balanced
43 5 1 * * cd /srv/favorite-places && bun run refresh:sweep -- --refresh-workers 1
```

GitHub Actions schedules use UTC.

## Self-Hosted Refresh Runner

The repo includes `.github/workflows/data-refresh.yml` for refresh automation on a self-hosted Linux runner.

The workflow intentionally keeps the upstream runner selection generic. It defaults to:

```json
["self-hosted", "Linux"]
```

Site repos that need a specific private runner pool should set the GitHub Actions repository variable `DATA_REFRESH_RUNNER_LABELS` to a JSON array, for example:

```json
["self-hosted", "Linux", "residential"]
```

Use repository variables for installation-specific runner labels instead of committing private labels to the upstream template.

The Actions job has a 180-minute hard timeout and runs the refresh command with a shorter soft timeout. The default soft timeout is 150 minutes, and manual dispatch values are capped at 150 minutes so setup, the 5-minute kill-after window, and summary, commit, and PR steps still fit under the job timeout. When the soft timeout expires, the workflow interrupts the refresh command, writes a partial summary, and continues to the commit and PR steps so completed raw snapshots, enrichment cache rows, and downloaded photos are not lost.

Rating-bearing place enrichment uses longer TTLs than raw saved-list snapshots because small review-count and rating movements usually do not change the guide experience. Normal rating-bearing places refresh after 15 days; places with at least 1,000 reviews refresh after 30 days. Non-operational, error, low-confidence, unmatched, and missing-photo retry entries keep shorter retry windows.

Recommended boundary:

- Do not run Google Maps list scraping or enrichment refreshes on GitHub-hosted runners.
- Keep `.github/workflows/ci.yml` validation-only.
- Keep `.github/workflows/data-refresh.yml` self-hosted only.
- Treat scraper session state, proxy routing, and anti-bot handling as infrastructure concerns.
- If the self-hosted runner is unavailable, skip the refresh rather than falling back to GitHub-hosted infrastructure.

Required repo secrets:

- `GOOGLE_PLACES_API_KEY`, optional for enrichment
- `GMAPS_SCRAPER_PROXY`, optional for scraper traffic
- `GH_AUTOMATION_TOKEN`, optional but recommended for pushing `automation/data-refresh-*` branches and opening/updating PRs

Minimum `GH_AUTOMATION_TOKEN` permissions:

- Classic PAT: `repo` and `workflow`
- Fine-grained PAT: repository access to this repo with `Contents: Read and write`, `Pull requests: Read and write`, and `Workflows: Read and write`

Runner provisioning:

- `unzip` must be present on `PATH` for `oven-sh/setup-bun`.
- GNU `timeout` must be present on `PATH` for the soft-timeout partial commit path.
- `cloakbrowser` downloads Chromium, but the host still needs Chromium system libraries.
- Prefer provisioning the full Playwright Chromium dependency set:

```bash
sudo npx playwright install-deps chromium
```

Partial Ubuntu fix for common missing libraries:

```bash
sudo apt-get update
sudo apt-get install -y libnspr4 libnss3 libgbm1 libxcb1 libxkbcommon0
sudo ldconfig
```

Verify:

```bash
ldconfig -p | grep -E 'libnspr4\.so|libnss3\.so|libgbm\.so\.1|libxcb\.so\.1|libxkbcommon\.so\.0'
```

## Cloudflare Pages

Cloudflare Pages should not auto-install Python dependencies for this repo. The root `pyproject.toml` exists for the local data pipeline, and Cloudflare's default `pip` path does not understand the vendored scraper declared in `[tool.uv.sources]`.

Use these Pages settings:

- Environment variable: `SKIP_DEPENDENCY_INSTALL=true`
- Environment variable: `BUN_VERSION=1.3.12`
- Python version: keep [../.python-version](../.python-version) aligned with `pyproject.toml`
- Build command:

```bash
bun ci && pipx install uv==0.11.6 && export PATH="$HOME/.local/bin:$PATH" && uv sync && bun run build:data && bun run build
```

Why:

- `bun ci` installs frontend dependencies from `bun.lock`
- `pipx install uv==0.11.6` makes `uv` available in the Pages build image
- `uv sync` installs Python dependencies, including the vendored scraper from `vendor/gmaps-scraper`
- `bun run build:data` generates Astro input JSON
- `bun run build` builds the static site

### Optional place chat

Place chat is kept in this repository because it consumes this site's generated place index and displays this site's place cards. It adds one Pages Function at `/api/chat`; the rest of the site remains static. Other static hosts can still build the site with chat disabled. A separate backend repository would be useful only if several sites need the same deployed service or independently managed releases.

Set `PUBLIC_CHAT_ENABLED=true` during the Astro build to show the chat bubble on home and guide pages, and set `CHAT_ENABLED=true` in Pages runtime variables to accept requests. Add an `AI` Workers AI binding in your Pages project. Store provider keys in Pages secrets. The chat model is `@cf/zai-org/glm-4.7-flash`; override it with `CHAT_MODEL`. Home-page guide classification uses `@cf/cloudflare/clef-flash`. To use another provider, set `CHAT_PROVIDER=openai-compatible`, `CHAT_MODEL`, `CHAT_API_BASE_URL`, and an encrypted `CHAT_API_KEY` secret; home-page guide ranking then falls back to built-in search. The API request and response stay the same across providers.

The Function loads `/data/chat/guides.json` and only the selected guides’ `/data/chat/<slug>.json` place indexes from Pages assets, avoiding the full browser index on Workers Free. For home chat it classifies compact guide summaries, selects at most three guides, then ranks at most 12 places per guide with Clef Flash and sends up to six per guide to chat. For guide chat it ranks up to 24 places in that guide. The chat model sees at most 12 places and can select up to six. Classification failures use search order; the two strongest lexical candidates remain in the bounded catalog even if classification overlooks their local-language names. The Function validates returned guide and place IDs, and returns separate text and card data. The browser renders linked cards with available place details without parsing assistant prose. A card for the current guide selects its existing card and zooms the map without navigating. Requests are limited to seven short messages and model replies to 640 output tokens. The endpoint returns a service error if the chat model or index is unavailable; visitors can still use regular search.

The shared browser/guide/chat index also includes `review_evidence` from the existing enrichment cache: up to 12 review-topic labels and five review snippets, each at most 180 characters, deduplicated with whitespace normalized. No reviewer identity, profile, or review metadata is exported. Index generation needs no new scraping or LLM calls. Review matches have lower lexical weight than curated descriptions and tags, including atmosphere queries whose terms are otherwise consumed as vibe filters. Search normalization preserves Unicode text and supports embedded Chinese, Japanese, and Korean phrase matches. Explicit tag filters still use the curated/generated tags. Review evidence is not included in home guide summaries; guide classification remains based on compact guide metadata.

After guide and geographic scoping, the existing candidate limits still apply. Ranking and recommendation prompts receive at most two query-matching review fragments per candidate (360 characters total), selected deterministically by term overlap with the resolved query and its retrieval alternatives. Review-only focus matches survive the final focus check when accepted by the model, but cannot trigger the deterministic fallback after model rejection. Reviews remain anecdotal evidence, not verified amenities, and lexical retrieval can match negative statements or miss translated/paraphrased concepts. The model is instructed to account for that context. Only cached samples are searchable; this is not an index of every Google Maps review.

Each turn uses the existing intent model to resolve a complete food/place focus, location, preferences and minimum rating from the last seven conversation turns, guide context, and prior versioned `search_context`. The model returns `new_search`, `refinement`, `correction` or `retry`; the API validates the full snapshot and applies it without requiring literal words in the latest message. Retrieval prioritizes the focus and its synonyms; ranking uses the complete resolved request. Area requests are constrained before retrieval using saved coordinates within 1.5 km of matching local place/address anchors, with equivalent local names supported. Unknown areas return no matches rather than silently expanding across the city. When the bounded catalog contains explicit focus/synonym matches, final cards must match that focus; unrelated model suggestions are rejected, with matching saved candidates used as fallback. Selected places are ordered by relative relevance bands, then rating and review count within each band. The final recommendation model receives only that request, with previous recommendations included only for explicit references such as “those”. Previous result IDs are validated against the index before they are supplied as context. Guide mode may return a typed `guide.apply-filters` action with query, tag IDs, minimum rating, and an optional validated area scope of saved place IDs. Applying it preserves the area boundary, selects rating sort for the filtered list, and supports Undo. The API validates proposals and rejects proposals with no matching saved data; the guide validates them against its controls before applying. Exact chat place types use the native type control; area scopes use a native area only when both label and complete saved-place membership match. Other queries stay in the search box and narrower or unmatched scopes remain removable “Near” chips. One active-filter row reflects search, area, type, budget, rating, review count, map scope and tags regardless of their source; chip removal updates the same control state. Choosing a native area replaces a chat area scope. The chat notice confirms the application and offers Undo without duplicating criteria. The guide owns filter state, match counting and Undo; the chat UI renders proposals and dispatches actions. Filters require an explicit click. Desktop place selection keeps chat open; narrow mobile screens minimize it.

For a public rollout with a hard zero-dollar inference ceiling, keep Workers AI on the free plan: requests stop when its daily free allocation is exhausted. On a paid plan or with another provider, configure a provider or gateway spend limit and an edge request limit before enabling `CHAT_ENABLED`. The prototype itself does not store a shared spend counter. Local Pages Function testing uses `wrangler pages dev dist --ai AI`; an AI binding calls the live service even in local development. `bun run dev` serves only the static Astro site and does not run Pages Functions.

### AI quota failures

The Pages runtime recognizes Cloudflare's documented account-quota code `3036` and explicit daily-allocation errors, including legacy messages. A bare HTTP 429 or error `4006` does not establish daily exhaustion. Quota failures stop further model calls in the turn and return `ai_daily_quota_exhausted`, the next UTC reset timestamp, and `Retry-After`. The browser explains the pause, retains the visitor's question, and disables new AI submissions until the reset; native search/filter controls remain available. Other provider failures keep the generic error message.

### Search-only chat scope

The existing intent-resolution call owns a typed `search`/`redirect` decision before guide/place ranking. Pure unrelated requests and role/prompt hijacking attempts receive a fixed search-only redirect, no cards or filter actions, and no new search context. Missing or unknown routes fail closed to that redirect. Mixed requests extract only the legitimate saved-place search. This adds no classifier call; redirected turns stop after intent resolution.

The API renders fixed text for results, filters, redirects, and no-match clarifications. Model prose is never returned to the visitor. Place/guide IDs and filter proposals still pass the existing catalog and schema validation. Scope instructions apply to intent, guide/place ranking, and recommendations; user text, previous state, place notes and review evidence remain untrusted data. The browser and eval runner preserve active search state on a typed redirect. This confines possible UI output even if the model misclassifies an attack as a search; semantic classification itself remains model judgment.

Regression coverage includes pure hijacks, unrelated requests, missing/unknown scope routes, mixed legitimate search plus override instructions, poisoned reviews, forged IDs/actions, empty-result prose, and a browser trajectory that resumes the active search after a redirect. The live eval suite has dedicated scope trajectories.


### Chat state recovery and diagnostics

Intent resolution owns semantic interpretation and receives up to seven role-labelled recent turns (each capped at 500 characters), the current guide’s city/title, a sanitized prior search snapshot and up to six previously displayed places. Guide and prior-state reference data use a user-role JSON payload, followed by the bounded conversation as user/assistant messages with the latest user request last. The fixed scope instructions remain the sole system message; history stays untrusted and gains no system role. The model classifies the latest turn as `new_search`, `refinement`, `correction` or `retry` and returns a complete snapshot of the intended search. The previous turn type is omitted from semantic memory so it does not anchor the next classification. An ambiguous request to try again after a correction may be classified as either retry or correction; the accepted search criteria must still remain correct. It can repair a stale stored food from an earlier user turn, preserve food/area during a mood refinement, resolve natural-language ratings, and clear constraints from contextual requests.

The runtime validates the turn type, required slots, string/alias bounds, supported preference IDs and rating thresholds, then builds the query deterministically from the accepted snapshot. It does not override focus, area or rating based on phrase matching or quoted evidence. Missing/invalid snapshot fields fail the turn without replacing the browser’s prior state. Legacy unsupported preferences are removed before the prior snapshot reaches the model. Canonical `search_context` stays at version 1 with an additive `turn_type`, also recorded as `diagnostics.transition.type`. Saved-place IDs, filter actions and geographic result boundaries retain their separate deterministic validation. Interpretation uses the same model call, with bounded extra conversation input. The default Cloudflare model receives JSON output schemas for intent and recommendation responses; other models/providers keep JSON mode unless `CHAT_JSON_SCHEMA=true` opts into schema support. Runtime validation runs for every provider and adds no automatic model retries.

Each validated API turn returns `diagnostics` plus `X-Chat-Request-Id`. A `chat.turn` console event records the same request ID, runtime owner, stage, model-call count, stage timings, slot-change flags and catalog/scoped/shortlist/candidate/result/action counts. Empty results distinguish `area_scope_empty`, `no_candidates` and `selection_empty`. Server trace events omit prompts, resolved search text, reviews and model output. Open your Pages deployment’s Functions logs to inspect these events.

The `tokyo-ramen-to-uni-recovery` live eval covers the reported sequence and requires actual results and the correct focus on each turn; local tests cover polluted-state migration and precise-area preservation. Diagnostics remain in the API response and internal console logs; the chat does not add report controls or browser storage.


### Persistent chat traces

Pages can optionally export each completed validated chat turn to Langfuse using OTLP/HTTP JSON. Set `CHAT_LANGFUSE_BASE_URL`, `CHAT_LANGFUSE_PUBLIC_KEY` and `CHAT_LANGFUSE_SECRET_KEY` as server-side Pages secrets. These are separate from the data pipeline's `LANGFUSE_*` settings. `CHAT_LANGFUSE_ENVIRONMENT` labels traces (default `production`); configure preview credentials separately if preview logging is desired.

The API owns the trace: its ID matches `X-Chat-Request-Id`, its name is `guide.search` or `discover.search`, and child spans record intent/classification/recommendation timings and failures. Metadata includes stage, outcome/error code, transition flags and retrieval/result counts. No model retries or inference calls are added. Export runs in `waitUntil` with a five-second timeout, no redirects or retries; export failures emit a redacted `chat.telemetry_failed` console event and do not change the chat response. Delivery is best effort rather than a durable audit log.

Content capture is off by default. `CHAT_LANGFUSE_CAPTURE_CONTENT=true` adds only the latest user request (up to 500 characters), validated returned search state and result/action IDs. It does not export the full conversation, catalog, review evidence, model prompts or raw completions. Console logs remain metadata only regardless of this setting. Set retention in your telemetry service.


### Guide filters versus chat recommendations

The intent resolver owns the requested food/type, area, preferences and rating. The recommendation model selects a small set of cards; it does not propose guide filters. The runtime constructs `guide.apply-filters` from the validated intent: food/type as the query, supported explicitly requested preference IDs as tags, the requested rating threshold (otherwise zero), and independently validated area scope. The current guide city is not appended to its own search query. Guide filters cover the matching guide catalog, independently of the recommended card IDs, and candidate attributes cannot introduce mood/rating restrictions. Alias fallback preserves requested tags and rating and never drops the food constraint to match an entire area.

## Environment Variables

Common variables:

- `GOOGLE_MAPS_JS_API_KEY`: browser Google Maps display key, read by Astro during render/build and embedded only when the Google map provider is active
- `GOOGLE_PLACES_API_KEY`: server/build-time key for API-based enrichment
- `GOOGLE_PLACES_ENRICHMENT_STRATEGY`: choose `scrape`, `api`, or `scrape_then_api` for place enrichment
- `GOOGLE_MAPS_PLACES_LLM_REPAIR`: override site scraper repair policy with `off`, `dom`, or `dom_then_translation`
- `GOOGLE_MAPS_PLACES_COLLECT_REVIEWS` / `GOOGLE_MAPS_PLACES_COLLECT_ABOUT`: override optional review/About panel collection
- `GOOGLE_MAPS_PLACES_SEMANTIC_LLM`: override optional LLM semantic enrichment for neighborhood, type tags, and vibe tags
- `GOOGLE_MAPS_PLACES_SEMANTIC_DESCRIPTIONS`: override optional LLM-generated card descriptions
- `GOOGLE_MAPS_PLACES_SEMANTIC_DESCRIPTION_FORCE_REFRESH`: force description regeneration instead of signature reuse
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`: optionally log uncached scraper repair and semantic enrichment LLM generations to Langfuse
- `LANGFUSE_TIMEOUT` / `LANGFUSE_FLUSH_AT` / `LANGFUSE_FLUSH_INTERVAL`: tune Langfuse export behavior; the pipeline defaults these to `2`, `8`, and `1.0` so telemetry remains bounded during long refresh jobs
- `GMAPS_SCRAPER_LANGFUSE_FULL_CAPTURE`: opt scraper repair traces into full request/response payload capture; unset by default so scraper repair traces use URL hashes and metadata only
- `PUBLIC_MAP_PROVIDER=leaflet`: force Leaflet/OpenStreetMap rendering
- `PUBLIC_PLACE_PHOTOS=off`: hide place photos in the UI
- `FAVORITE_PLACES_SITE_DIR`: point the app and Python pipeline at a non-default site pack
- `GMAPS_SCRAPER_PROXY`: route scraper traffic through a proxy
- `FAVORITE_PLACES_GMAPS_SCRAPER_STATE_DIR`: optionally override the scraper browser-profile and HTTP-cookie-jar root; point multiple worktrees at the same absolute path to reuse scraper session state
- `BRAVE_SEARCH_API_KEY` / `BRAVE_API_KEY`: enable Brave Search for optional trust-signal refreshes
- `FAVORITE_PLACES_TRUST_STORE_URL`: override trust-signal storage with an explicit SQLAlchemy URL, including local SQLite file URLs
- `FAVORITE_PLACES_TRUST_GOOGLE_FALLBACK`: allow Google Search HTML fallback for trust-signal refreshes when set to `true`
- `FAVORITE_PLACES_MICHELIN_REGION_URLS`: JSON object of `"country/city"` to MICHELIN region URL overrides for full-guide snapshots

Use a restricted browser key for `GOOGLE_MAPS_JS_API_KEY`. Do not expose `GOOGLE_PLACES_API_KEY` to the browser.

## Data Model

The project keeps these layers separate:

1. `site/data/raw/`: raw scraper or CSV import snapshots
2. `site/data/cache/places.sqlite`: cached Google Places lookups keyed by guide slug and stable place id
3. `~/.cache/favorite-places/trust-signals/trust.sqlite`: optional user-shared trust-signal cache for MICHELIN guide snapshots, MICHELIN restaurant detail-year snapshots, Wikipedia Michelin-star history, Tabelog, Time Out, and blog/search mentions
4. `site/data/cache/google-places/`: optional debug export directory, gitignored
5. `site/overrides/`: handwritten metadata, tags, notes, visibility, attribution, and ranking
6. `src/data/generated/`: static JSON that Astro reads at build time, gitignored
7. `public/data/search-index.json`: browser search index, gitignored

Merge precedence:

1. Manual overrides
2. Google Places enrichment cache and optional trust-signal cache
3. Raw scraped list data

Manual overrides always win over machine-enriched fields.

Google Maps list imports may attach `added_by` to individual raw places. The build preserves that attribution into generated place JSON, and place overrides can replace it with an author object or suppress it with `null`. The frontend default is to show individual place attribution only when it is not the effective guide author; `site/config.ts` can disable all card attributions with `placeCard.showAttribution: false`.

## Google Places Enrichment

Enrichment is optional and cached. A normal build can run without enrichment, but the resulting guides are intentionally sparse: they mostly reflect the raw saved-list data plus manual overrides. Running enrichment is recommended when you want stable categories, Maps links, status, ratings, and photos.

- `bun run enrich:data`: normal incremental enrichment; missing places go first, then stale entries
- `bun run fill:enrichment`: only places with no cache entry
- `bun run fill:gaps`: missing enrichment plus missing photos
- `bun run refresh:enrichment`: force-refresh every entry
- `bun run refresh:semantic-enrichment`: generate or refresh LLM semantic neighborhoods, type tags, and vibe tags from existing cache evidence only, without scraping or Places API calls
- `bun run refresh:semantic-descriptions`: generate or refresh LLM semantic descriptions from existing cache evidence only, without scraping or Places API calls
- `bun run export:cache:json`: optional debug export of per-guide cache JSON from SQLite

`GOOGLE_PLACES_ENRICHMENT_STRATEGY` controls source selection:

- `scrape`: Google Maps place-page scraping only
- `api`: Google Places Text Search only
- `scrape_then_api`: Google Maps place-page scraping first, then Google Places Text Search when the scraped result is blocked, limited, unmatched, or too sparse to trust

The default strategy is `scrape_then_api`. The scraper path does not require a Places API key. `api` mode and the fallback leg of `scrape_then_api` require `GOOGLE_PLACES_API_KEY`.

`site/enrichment.json` owns site-level scraper policy. The `google_maps_places.llm_repair` value supports `off`, `dom`, and `dom_then_translation`; the default is `dom`, which lets the scraper use optional LLM repair only for thin DOM extraction. Display translation repair is only run by `dom_then_translation`, after reusable prior display fields are applied. `google_maps_places.collect_reviews` and `collect_about` default to `false`; set them to `true` only for sites that want the heavier cache payload.

`google_maps_places.semantic_llm` defaults to `false`. When enabled, the pipeline sends compact cache-only evidence to the configured OpenAI-compatible model to infer better neighborhood labels, type tags, and vibe tags. Inputs include category, address, price range, review topics, compact review signals, and About labels; generated public JSON still does not expose reviews or About sections directly. `google_maps_places.semantic_descriptions` separately enables generated card descriptions. Description reuse is keyed by a coarse semantic signature over identity and enrichment quality signals, not volatile review text; `semantic_description_force_refresh` bypasses reuse and the semantic LLM cache for intentional regeneration. If credentials are absent or the LLM call fails, deterministic locality/category/vibe rules remain the fallback.

When Langfuse credentials are configured, each uncached OpenAI-compatible LLM request is recorded as a Langfuse generation with token usage when the provider returns it and metadata such as prompt version, evidence hash, and model. Semantic enrichment logging includes the compact semantic request payload and parsed output. Scraper repair logging redacts URLs to hashes and omits request/response payloads by default; `GMAPS_SCRAPER_LANGFUSE_FULL_CAPTURE=true` opts scraper repair traces into full payload capture. Both Langfuse clients use the SDK's async batch exporter with refresh-friendly defaults (`LANGFUSE_TIMEOUT=2`, `LANGFUSE_FLUSH_AT=8`, `LANGFUSE_FLUSH_INTERVAL=1.0`) and avoid adding extra process-exit flush hooks, so a slow Langfuse server does not become the main refresh bottleneck. Semantic and scraper cache hits intentionally skip Langfuse generation logging because no model call occurred.

`google_maps_places.price_display` controls the card-facing price label without changing raw scraper cache fields. `source_order` can prefer `price_range`, `admission_price`, or `room_price`. Numeric `price_range` values are shown conservatively for food/drink/shopping-style categories; attraction tickets and lodging quotes should use the separate `admission_price` or `room_price` fields. `currency_mode` defaults to `raw`; `guide_local` converts to the guide country currency, and `target` converts to `target_currency`. Symbolic values preserve the price tier while numeric prices use daily cached USD exchange rates from `api.fxratesapi.com` with jsDelivr currency-api fallback; failures degrade to the raw scraper price. `max_numeric_by_source` optionally hides converted numeric values above a per-source/per-currency ceiling so reseller bundles or bad localized offers do not appear on cards as normal admission prices.

`google_maps_places.neighborhood_mappings` is an ordered site-level cleanup layer for local naming conventions after deterministic and semantic neighborhood selection. Rules can match `from`, scope by `city`/`country`, optionally require `when_address_contains` or `when_candidate`, and emit `to`; per-place neighborhood overrides still take precedence.

The example site opts into review and About collection in `site.example/enrichment.json` so the fixture cache exercises those fields.

The API path uses Google Places Text Search with a narrow field mask and location bias around scraped coordinates.

Enrichment fills useful metadata such as category, Maps URI, rating, business status, and photos without making the rendered site depend on live Google calls.

Cache invalidation is field-aware: raw input changes force refresh, operational places refresh more slowly, and volatile states like ratings, closures, unmatched results, and API errors refresh sooner.

For CSV imports, the pipeline trusts each place's Google Maps URL more than the exported title. It derives stable place IDs from the Maps place token when needed and prefers Google Places display names during normalization so export mojibake does not leak into the final guide.

## Template Repo Workflow

The reusable application lives at the repository root in both
[`508-dev/favorite-places`](https://github.com/508-dev/favorite-places) and
[`michaelmwu/demflyers-favorites`](https://github.com/michaelmwu/demflyers-favorites).
Use upstream for shared features and dependency upgrades. When a shared fix
starts downstream, open a companion upstream PR and carry over the code,
tests, and template documentation.

Keep the demflyers site pack under `site/`, its Pages and quota-alert
deployment settings, and its site-specific build-hook tests in the demflyers
repo. Use `site.example/` to verify reusable changes. Upstream leaves chat
disabled until a deployment opts in.

Sync the complete `vendor/gmaps-scraper/` tree and refresh the root `uv.lock`
in both repositories. Record the scraper commit and any carried patches in the
vendor update commit. Check Python and frontend tests, the example data build,
and browser tests when changing shared code.

### Review evidence search

The shared browser/guide/chat index includes `review_evidence` from the enrichment cache: up to 12 review-topic labels and five review snippets, each at most 180 characters, deduplicated with whitespace normalized. Reviewer identity and profile metadata are excluded. Index generation needs no new scraping or LLM calls. Review text is stored once and joined into search text at runtime. Review matches have lower lexical weight than curated descriptions/tags; Unicode text and embedded Chinese, Japanese, and Korean phrase matches are supported. Explicit tag filters still use curated/generated tags.

The chat prototype retrieves at most 12 candidates and sends at most two query-matching review fragments per candidate (360 characters), selected using the latest and preceding user requests, consistently with shortlisting. The shared selector also accepts retrieval alternatives for runtimes that resolve structured intent. Only model-selected place IDs become recommendations; an empty selection stays empty, including when a review mentions the requested item negatively. Reviews are untrusted anecdotal evidence, not verified amenities. Only cached samples are searchable; lexical matching can find negative statements or miss paraphrases/translations.

Review snippet selection shares lexical normalization, token matching, and atmosphere aliases with search, so synonym matches such as “calm” → “quiet” retain their evidence.

Review evidence is exported only when the cached enrichment identity is compatible with the saved place, using the same identity guard as recommendation copy. Single-character CJK queries are retained during evidence selection.

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

- `refresh:balanced`: refreshes due raw sources, then runs normal incremental enrichment. Missing places and raw-place changes go first; stale cache entries are refreshed afterward according to their TTLs.
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
- `FAVORITE_PLACES_TRUST_CACHE_PATH`: override the default SQLite trust-signal cache path
- `FAVORITE_PLACES_TRUST_STORE_URL`: override trust-signal storage with an explicit SQLAlchemy URL
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

The intended public upstream template repo is [`508-dev/favorite-places`](https://github.com/508-dev/favorite-places).

This repo currently uses a transitional layout where the reusable app is at the repo root and the private site pack lives in `site/`. A later private deployment layout can move the reusable app under `app/` and keep `site/` beside it.

Add the upstream remote:

```bash
git remote add upstream git@github.com:508-dev/favorite-places.git
```

For an `app/ + site/` private deployment repo, point the build at the sibling private site pack:

```bash
cd app
FAVORITE_PLACES_SITE_DIR=../site bun run build:data
FAVORITE_PLACES_SITE_DIR=../site bun run build
```

Pull public app updates into the private deployment repo:

```bash
git subtree pull --prefix app upstream main --squash
```

Contribute reusable app changes back to the public template:

```bash
git subtree push --prefix app upstream my-app-change
```

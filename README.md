# Favorite Places

Static-first travel guides built from Google Maps saved lists.

Favorite Places turns personal saved lists into a small shareable site: guide pages, searchable place cards, map views, top picks, tags, notes, and lightweight theming. It is designed as a template app with user-owned content kept in a separate `site/` pack.

## What You Get

- An Astro static site for browsing guides and places
- A Python + `uv` data pipeline for importing Google Maps and Wanderlog lists
- A `site/` overlay for your config, theme, source lists, raw data, overrides, templates, and assets
- Optional Google Places enrichment for categories, status, Maps links, ratings, and photos
- A tiny committed `site.example/` pack that works as a demo

## Quick Start

Install dependencies:

```bash
bun install
uv sync
```

Optional local pre-commit hook setup:

```bash
uv tool install prek
prek install
prek run --all-files
```

This installs a `prek`-managed pre-commit hook that runs Biome checks on staged frontend files, `ruff` on staged Python files under `scripts/` and `tests/python/`, and `mypy` on the typed pipeline helper modules under `scripts/`.

Keep editor- or agent-specific launch configs local. Files under `.claude/` are not part of the repo contract and should remain untracked.

Preview the committed example site pack:

```bash
FAVORITE_PLACES_SITE_DIR=site.example bun run build:data
FAVORITE_PLACES_SITE_DIR=site.example bun run dev
```

Then open the local URL printed by Astro.

The example pack uses two public Google Maps example lists, one public Wanderlog guide example, committed raw snapshots, manual overrides, tags, top picks, and template aside blocks. It does not need API keys to render.

## Create Your Site

Copy the example pack into the default `site/` location:

```bash
cp -R site.example site
```

Edit:

- `site/config.ts` for site name, nav, copy, labels, map provider, and display options
- `site/theme.css` for colors, fonts, spacing, and custom styling
- `site/list_sources.json` for your Google Maps or Wanderlog list sources
- `site/overrides/` for handwritten notes, tags, top picks, and ranking
- `site/content/templates/` for optional trusted HTML insertion points
- `site/public/` for logos, favicons, and site-owned assets

After `site/` exists, it is the default. You only need `FAVORITE_PLACES_SITE_DIR` when previewing `site.example`, using a private sibling site pack, or using a later `app/ + site/` layout.

## Add Sources

Each source in `site/list_sources.json` needs a stable `slug`.

- `url` sources infer `type: "google_list_url"` for supported Google Maps links, including `https://maps.app.goo.gl/...` shortlinks and `https://www.google.com/maps/...` share links.
- `url` sources also infer `type: "wanderlog_view_url"` for supported Wanderlog guide URLs like `https://wanderlog.com/view/...`.
- `path` sources infer `type: "google_export_csv"` and require `title`.
- `type` can still be included explicitly, but it must match the configured `url` or `path`.
- `title` is optional for Google Maps URL sources and acts as a fallback list title.
- `title` is optional for Wanderlog URL sources and acts as a fallback guide title.
- Google My Maps URLs such as `https://www.google.com/maps/d/...` are not supported yet.

```json
[
  {
    "slug": "tokyo-japan",
    "url": "https://maps.app.goo.gl/your-public-list"
  },
  {
    "slug": "hong-kong-wanderlog-example",
    "type": "wanderlog_view_url",
    "url": "https://wanderlog.com/view/your-guide/hong-kong-recommendations",
    "title": "Hong Kong Recommendations (Wanderlog example)"
  },
  {
    "slug": "taipei-taiwan",
    "path": "data/imports/taipei-taiwan.csv",
    "title": "Taipei, Taiwan"
  }
]
```

Supported sources:

- Public Google Maps saved-list URLs, including `https://maps.app.goo.gl/...` shortlinks
- Public Wanderlog guide URLs, including `https://wanderlog.com/view/...`
- Google Takeout saved-list CSV exports

Google My Maps URLs are not supported yet.

For Google Maps URL sources, raw snapshots preserve the list `owner` object, including `name`, `photo_url`, `photo_path`, `avatar_mode`, and `profile_id`, plus any `collaborators` the scraper can recover. Individual places can also carry an `added_by` author with `name` and `profile_id`. When the scraped owner is the effective published author, source refresh downloads a square local author image into `site/public/author-photos/` and stores its `photo_path`. Guide list overrides can optionally set, replace, or suppress the generated guide `author`; use `photo_path` to point at a site-owned image under `site/public/`, or set `avatar_mode` to `photo`, `initials`, or `icon`.

Guide list overrides can also set `"place_photo_mode": "remote_url"` to force a guide to skip local `site/public/place-photos/` assets and render cards from direct `photo_url` values instead. The default is `"local_cache"`.

Place-level `added_by` metadata is preserved in generated place data. Cards show it by default only when it differs from the guide author, so collaborator additions are visible without repeating the guide owner on every card. Override a place with `"added_by": {"name": "Name", "avatar_mode": "initials"}` to set it manually, or `"added_by": null` to suppress it for that place. Set `placeCard.showAttribution: false` in `site/config.ts` to hide all individual place attributions, or `placeCard.showGuideAuthorAttribution: true` to also show places added by the guide author.

## Build Your Data

Refresh configured sources and rebuild generated site data:

```bash
bun run sync:sources
```

Rebuild generated site data from existing raw snapshots:

```bash
bun run build:data
```

Run the site:

```bash
bun run dev
```

Verify before shipping:

```bash
bun run check
bun run build
```

## Optional Enrichment

The site works without enrichment, and normal builds never call Google. When you run an enrichment command, the pipeline first tries to scrape each place's Google Maps page from the saved-list URL. If that page scrape is blocked, limited, unmatched, or too sparse to trust, the pipeline can optionally fall back to the Google Places API.

Add a server/build-time Places key only if you want that API fallback:

```bash
GOOGLE_PLACES_API_KEY=...
```

Then run one of the enrichment commands:

```bash
bun run fill:gaps
bun run enrich:data
bun run refresh:enrichment
```

Generate LLM semantic descriptions from existing cached enrichment without rescraping or using the Places API:

```bash
bun run refresh:semantic-enrichment
bun run refresh:semantic-enrichment:force
bun run refresh:semantic-descriptions
bun run refresh:semantic-descriptions:force
```

Compare LLM model choices for semantic enrichment and scraper DOM repair with the eval harness documented in [`docs/llm-evals.md`](docs/llm-evals.md). The package aliases are `bun run eval:llm`, `bun run eval:llm:semantic`, and `bun run eval:llm:dom-repair`.

The behavior is configurable in a few places:

- `GOOGLE_PLACES_ENRICHMENT_STRATEGY` controls enrichment source selection. Use `scrape` for scraper-only, `api` for API-only, or `scrape_then_api` for scraper first with API fallback. The default is `scrape_then_api`.
- `GOOGLE_PLACES_API_KEY` enables API-based enrichment. It is required for `api` mode and for the fallback leg of `scrape_then_api`.
- `site/enrichment.json` controls site-owned scraper policy. `google_maps_places.llm_repair` defaults to `dom`, while `collect_reviews` and `collect_about` default to `false` so enrichment stays compact unless a site opts into those heavier panels.
- `GOOGLE_MAPS_PLACES_LLM_REPAIR`, `GOOGLE_MAPS_PLACES_COLLECT_REVIEWS`, and `GOOGLE_MAPS_PLACES_COLLECT_ABOUT` override the site enrichment config for automation or one-off refreshes.
- `GMAPS_SCRAPER_PROXY` routes Google Maps list and place-page scraping through a proxy.
- `FAVORITE_PLACES_GMAPS_SCRAPER_STATE_DIR` optionally overrides where scraper browser profiles and HTTP cookie jars are stored. Point multiple worktrees at the same absolute path when you want to reuse scraper session state across them.
- The command controls refresh scope: `fill:gaps` fills missing enrichment and photos, `enrich:data` fills missing or stale cache entries, `refresh:enrichment` refreshes every entry, `refresh:semantic-enrichment` updates cached semantic neighborhoods/tags from already cached evidence, and `refresh:semantic-descriptions` only updates cached semantic descriptions from already cached enrichment evidence.

### Optional Trust Signals

Trust signals are third-party mentions such as MICHELIN Guide, Tabelog Awards for Japan guides, Time Out, and blog mentions. Normal builds only read the local trust cache; they do not call search providers.

MICHELIN regions are fetched as full guide snapshots when a configured guide maps to a known region. Tokyo and Kyoto, Japan are built in; add more regions with `FAVORITE_PLACES_MICHELIN_REGION_URLS` as a JSON object keyed by `"country/city"`. Wikipedia is used as a supplemental Michelin-star history source where explicitly mapped, such as Taiwan; it is not treated as a Bib Gourmand source. MICHELIN and Tabelog signals are year-aware: explicit years are preserved when the source exposes them, and live MICHELIN region rows can fill missing current guide years from cached official restaurant detail pages.

Refresh trust signals with Brave Search:

```bash
BRAVE_SEARCH_API_KEY=... bun run refresh:trust
```

Allow the brittle Google Search HTML fallback only when you explicitly want it:

```bash
bun run refresh:trust:google-fallback
```

The default trust cache is outside the repo and shared across checkouts/repos for the current user:

```txt
~/.cache/favorite-places/trust-signals/trust.sqlite
```

Set `XDG_CACHE_HOME` to move that default root. Override it with `FAVORITE_PLACES_TRUST_STORE_URL` for a SQLAlchemy URL, including local SQLite file URLs.

Example `site/enrichment.json`:

```json
{
  "google_maps_places": {
    "llm_repair": "dom",
    "collect_reviews": false,
    "collect_about": false,
    "semantic_llm": false,
    "semantic_descriptions": false,
    "semantic_description_force_refresh": false,
    "price_display": {
      "currency_mode": "guide_local",
      "source_order": ["price_range", "admission_price", "room_price"],
      "max_numeric_by_source": {
        "admission_price": {
          "JPY": 5000,
          "TWD": 1000
        }
      }
    },
    "neighborhood_mappings": [
      {
        "city": "Taipei",
        "country": "Taiwan",
        "from": "Wanhua District",
        "to": "Wanhua",
        "when_address_contains": "Wanhua District"
      }
    ]
  }
}
```

When `semantic_llm` is enabled and LLM credentials are configured, the pipeline uses compact cache-only evidence from price range, review topics, review snippets, and About labels to infer neighborhood, type tags, and vibe tags. `semantic_descriptions` separately enables generated card descriptions. Descriptions are reused while the semantic description signature remains stable; the signature tracks major quality changes such as name/address/category changes, review topics appearing, About sections changing, price range, and coarse rating/review-count buckets. Set `semantic_description_force_refresh` when you intentionally want to regenerate descriptions even if the signature is unchanged. If the LLM is unavailable or errors, deterministic category, locality, and vibe rules still produce the guide data.

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to log uncached LLM calls to Langfuse. The optional `LANGFUSE_BASE_URL` selects a non-default Langfuse region or self-hosted instance. Langfuse logging covers scraper place repair and semantic enrichment/description generations; cache hits are not logged as model calls. Langfuse export uses short, async-friendly defaults (`LANGFUSE_TIMEOUT=2`, `LANGFUSE_FLUSH_AT=8`, and `LANGFUSE_FLUSH_INTERVAL=1.0`) so observability does not dominate long refresh jobs; override those SDK settings if needed. Scraper repair logs redact URLs and omit full request/response payloads by default; set `GMAPS_SCRAPER_LANGFUSE_FULL_CAPTURE=true` only when you explicitly want full scraper repair payload capture.

`price_display` controls the card-facing price label while keeping raw scraper fields in the enrichment cache. `source_order` chooses which scraper field to display first: `price_range`, `admission_price`, or `room_price`. Numeric `price_range` values are displayed conservatively for food/drink/shopping-style categories; attraction tickets and lodging quotes should come through the separate `admission_price` or `room_price` fields. `currency_mode` supports `raw`, `guide_local`, or `target`; `target` also requires `target_currency`, such as `USD`. Symbol-only values like `$$` keep the same tier and swap the symbol, while numeric prices use cached daily USD exchange rates from `api.fxratesapi.com` with jsDelivr currency-api fallback. If rates are unavailable, the raw price is used. `max_numeric_by_source` can hide implausibly large converted values by source field and display currency, which is useful when Google surfaces reseller bundles instead of a simple admission ticket.

`neighborhood_mappings` is an ordered site-level cleanup layer for local naming conventions. Each rule can scope by `city` and `country`, match a current `from` neighborhood, optionally require `when_address_contains` or `when_candidate`, and then emit `to`. Per-place `neighborhood` overrides still win over these mappings.

Manual overrides always win over machine-enriched fields.

## Common Environment Variables

```bash
# Browser Google Maps display key.
GOOGLE_MAPS_JS_API_KEY=...

# Server/build-time key for API-based enrichment.
GOOGLE_PLACES_API_KEY=...

# Enrichment source strategy: scrape, api, or scrape_then_api.
GOOGLE_PLACES_ENRICHMENT_STRATEGY=scrape_then_api

# Scraper LLM repair policy: off, dom, or dom_then_translation.
GOOGLE_MAPS_PLACES_LLM_REPAIR=dom

# Optional LLM semantic tags/neighborhoods from enriched cache evidence.
GOOGLE_MAPS_PLACES_SEMANTIC_LLM=false

# Optional LLM-generated card descriptions from enriched cache evidence.
GOOGLE_MAPS_PLACES_SEMANTIC_DESCRIPTIONS=false

# Force LLM-generated card description regeneration.
GOOGLE_MAPS_PLACES_SEMANTIC_DESCRIPTION_FORCE_REFRESH=false

# Optional LLM observability for uncached generations.
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TIMEOUT=2
LANGFUSE_FLUSH_AT=8
LANGFUSE_FLUSH_INTERVAL=1.0
GMAPS_SCRAPER_LANGFUSE_FULL_CAPTURE=false

# Optional proxy for Google Maps list and place-page scraping.
GMAPS_SCRAPER_PROXY=...

# Optional shared scraper state root for browser profiles and curl cookies.
FAVORITE_PLACES_GMAPS_SCRAPER_STATE_DIR=/absolute/path/to/.context/gmaps-scraper

# Optional trust-signal cache and search provider settings.
BRAVE_SEARCH_API_KEY=...
FAVORITE_PLACES_TRUST_STORE_URL=sqlite:////absolute/path/to/trust.sqlite
FAVORITE_PLACES_TRUST_GOOGLE_FALLBACK=false
FAVORITE_PLACES_MICHELIN_REGION_URLS='{"japan/kyoto":"https://guide.michelin.com/en/jp/kyoto-region/restaurants"}'

# Force Leaflet/OpenStreetMap rendering.
PUBLIC_MAP_PROVIDER=leaflet

# Hide place photos in the UI.
PUBLIC_PLACE_PHOTOS=off

# Point at a non-default site pack.
FAVORITE_PLACES_SITE_DIR=../site
```

To share scraper state across Git worktrees and the main checkout, a practical choice is:

```bash
FAVORITE_PLACES_GMAPS_SCRAPER_STATE_DIR="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.context/gmaps-scraper"
```

A fresh persistent profile does not automatically fix limited-view responses on its own. It gives the scraper a durable browser profile and a durable curl cookie jar, but you may still need to run a headed scrape once against that same path to establish consent or trust state.

Use a restricted browser key for `GOOGLE_MAPS_JS_API_KEY`. Do not expose `GOOGLE_PLACES_API_KEY` to the browser.

## Deploy

This is a static Astro site, so it can deploy to Cloudflare Pages, GitHub Pages, or similar static hosts.

For Cloudflare Pages, use the custom build path documented in [Architecture](docs/ARCHITECTURE.md#cloudflare-pages). Do not rely on Cloudflare's automatic Python dependency detection for this repo.

## More Docs

- [Architecture](docs/ARCHITECTURE.md): site-pack contract, Cloudflare Pages, refresh automation, data model, and enrichment internals
- [Design System](docs/design-system.md): current visual system notes

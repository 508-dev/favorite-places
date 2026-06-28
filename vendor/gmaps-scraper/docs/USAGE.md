# Usage Guide

This guide covers the CLI and library flows that are too detailed for the
README.

## CLI Basics

Scrape a saved list:

```bash
uv run gmaps-scraper "https://maps.app.goo.gl/MG2Vd5pWBkL7hXL18"
```

Scrape a place page:

```bash
uv run gmaps-scraper \
  "https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z" \
  --kind place
```

Write JSON to a file:

```bash
uv run gmaps-scraper \
  "https://maps.app.goo.gl/MG2Vd5pWBkL7hXL18" \
  --output saved-list.json
```

Download place images:

```bash
uv run gmaps-scraper URL --kind place --download-photo den-photo.jpg
uv run gmaps-scraper URL --kind place --download-main-photo den-main-photo.jpg
```

Run with a visible browser for debugging:

```bash
uv run gmaps-scraper URL --kind place --headed
```

## Fetch Modes

```bash
uv run gmaps-scraper URL --fetch-mode auto
uv run gmaps-scraper URL --fetch-mode curl
uv run gmaps-scraper URL --fetch-mode browser
```

- `auto` uses `curl_cffi` first and falls back to the browser when parsing fails.
- `curl` uses only the HTTP path.
- `browser` uses only the browser path.

Place scraping currently uses the browser path.

## Batch Place Scraping

`places.txt` is newline-delimited. Blank lines and comments are ignored:

```text
https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z
# comments are ignored
https://www.google.com/maps/place/Narisawa/@35.6724929,139.7111143,17z
```

Batch scrape with retries, warmed browser contexts, staggered starts, and
screenshots:

```bash
uv run gmaps-scraper \
  --kind place \
  --input places.txt \
  --session-dir "$CONDUCTOR_ROOT_PATH/.gmaps-scraper/session" \
  --max-concurrency 1 \
  --max-retries 2 \
  --retry-backoff-ms 2000 \
  --stagger-ms 500 \
  --screenshot-output-dir .context/places/screenshots \
  --output place-results.json
```

Browser sessions use a weighted random desktop viewport by default. Persistent
profile sessions keep a stable viewport for that profile path. Use
`--disable-random-window-size` when you need CloakBrowser's default viewport,
and `--human-mouse` when you want CloakBrowser to add humanized mouse, keyboard,
and scroll behavior despite the extra latency.

You can also pass multiple URLs directly or pipe them on stdin:

```bash
uv run gmaps-scraper --kind place URL1 URL2 URL3
printf '%s\n' URL1 URL2 URL3 | uv run gmaps-scraper --kind place --input -
```

Parallel workers get separate browser profile subdirectories under
`--session-dir` and separate HTTP cookie jar paths. Use `--max-concurrency 1`
when the goal is a single long-lived browser identity. Use higher concurrency
when separate worker session state is acceptable.

## Debug Artifacts

Write place debug artifacts and a compact selector recipe:

```bash
uv run gmaps-scraper URL \
  --kind place \
  --debug-output-dir .context/places/example
```

`--screenshot-output-dir` writes overview and reviews screenshots. Debug dumps
also include raw investigation artifacts under `artifacts/` and a compact
`selector-recipe.json`. Reuse selector recipes across sessions, not full DOM
snapshots.

Reviews and About collection are enabled by default for place scraping. Use
`--skip-reviews` when you only need overview facts and do not want to open the
Reviews tab. Use `--skip-about` when About-panel attributes are not needed.
Skipping Reviews still preserves any review-count/topic evidence present on the
overview page, but it will not expand the Reviews tab for more topics or visible
review snippets.

## Optional LLM Repair

LLM repair is opt-in. Deterministic DOM and preview extraction always run first.

```bash
OPENAI_API_KEY=...
LLM_MODEL=gpt-5-mini

uv run gmaps-scraper \
  URL \
  --kind place \
  --llm-repair
```

The LLM path is split into two generic tasks:

- `dom_repair`: repair missing or suspicious Google Maps facts from sanitized
  DOM evidence.
- `display_translation`: produce English-readable `address_display_en` and
  `category_display_en` when raw Google fields contain non-Latin display text.

By default, `--llm-repair` enables both tasks. Restrict work with repeated
`--llm-task` flags:

```bash
uv run gmaps-scraper \
  URL \
  --kind place \
  --llm-repair \
  --llm-task display_translation
```

Configuration precedence is:

1. Checked-in app defaults
2. Worktree-local `llm.local.json`
3. Environment variables

Built-in aliases currently include `gpt-5-mini`, `gpt-4o-mini`, and
`gpt-4.1-mini`. Checked-in defaults intentionally target OpenAI-compatible chat
completions endpoints only. Add other OpenAI-compatible providers through
worktree-local `llm.local.json`.

Example worktree-local `llm.local.json` for Fireworks:

```json
{
  "models": {
    "kimi-k2p6": {
      "provider": "fireworks",
      "model": "accounts/fireworks/models/kimi-k2p6",
      "omit_temperature": true,
      "request_options": {
        "reasoning_effort": "low",
        "max_tokens": 512
      }
    }
  }
}
```

Useful environment variables include `LLM_MODEL`, `LLM_PROVIDER`,
`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`,
`LLM_REASONING_EFFORT`, and `LLM_OMIT_TEMPERATURE`.

## LLM Cache And Translation Memory

Cache optional LLM repairs so unchanged evidence does not call the model again:

```bash
uv run gmaps-scraper \
  --kind place \
  --input places.txt \
  --llm-repair \
  --llm-cache-dir "$CONDUCTOR_ROOT_PATH/.gmaps-scraper/llm-cache" \
  --session-dir "$CONDUCTOR_ROOT_PATH/.gmaps-scraper/session"
```

When `--llm-cache-dir` is used, the scraper also stores exact typed
translation-memory entries learned from LLM `address_display_en` and
`category_display_en` repairs in `translation-memory.learned.json`. Those
entries are reused from the same cache directory on later runs before calling
the LLM.

Learned memory is limited to category labels, city/country/neighborhood
components, floor/building suffixes, and known address tokens. Reviews and
review topics are never translated or learned into this cache.

Approved memory also supports typed pattern entries for structural address
tokens. Pattern templates intentionally support only numbered capture
substitution such as `{1}` and `{2}`:

```json
{
  "kind": "pattern",
  "source_pattern": "(?<![A-Za-z0-9])(\\d+)\\s*樓\\s*之\\s*(\\d+)",
  "target_template": "{1}F-{2}",
  "field_kinds": ["address_component"],
  "source_method": "approved",
  "confidence": "high"
}
```

See [Contributing](../CONTRIBUTING.md) for promotion rules.

## Downstream Refreshes

Recommended low-cost refresh flow:

1. Scrape with `llm_policy="never"` to get fresh deterministic facts.
2. Reuse prior `address_display_en` and `category_display_en` only when the raw
   `address` or `category` is unchanged and the prior display value no longer
   needs English normalization.
3. If a raw field changed, or there is no reusable display value, check
   `needs_display_en(raw_value)` or diagnostics flags such as
   `needs_address_display_en` / `needs_category_display_en`.
4. Only then run optional LLM repair with a stable cache directory.

```python
from pathlib import Path

from gmaps_scraper import (
    cached_place_repairer,
    llm_cache_namespace_from_env,
    needs_display_en,
    openai_compatible_place_repairer_from_env,
    repair_place_display_fields,
    reuse_place_display_fields,
    scrape_place,
)

fresh = scrape_place(place_url, llm_policy="never")
fresh = reuse_place_display_fields(fresh, previous_place)

needs_translation = (
    needs_display_en(fresh.address) and fresh.address_display_en is None
) or (
    needs_display_en(fresh.category) and fresh.category_display_en is None
)

if needs_translation:
    fresh = repair_place_display_fields(
        fresh,
        repairer=cached_place_repairer(
            openai_compatible_place_repairer_from_env(),
            cache_dir=Path(".gmaps-scraper/llm-cache"),
            cache_namespace=llm_cache_namespace_from_env(),
        ),
        evidence={"city": "Singapore", "country": "Singapore"},
    )
```

For mapping-based caches, use
`reusable_place_display_fields(current_fields, previous_fields)` and merge the
returned keys into the refreshed record.

`repair_place_display_fields()` is a no-scrape helper. It sends only the current
place fields and optional caller evidence to the display-translation repairer.
Useful caller evidence includes guide/list city, country, or region. Reviews and
review topics should not be used for display translation.

### Display Repair Without Scraping

Use `repair_place_display_fields()` when a downstream cache already has fresh
place facts and only needs English-readable display fields. This avoids opening
Google Maps again.

```python
from pathlib import Path

from gmaps_scraper import (
    PlaceDetails,
    cached_place_repairer,
    llm_cache_namespace_from_env,
    openai_compatible_place_repairer_from_env,
    repair_place_display_fields,
)

repairer = cached_place_repairer(
    openai_compatible_place_repairer_from_env(),
    cache_dir=Path(".gmaps-scraper/llm-cache"),
    cache_namespace=llm_cache_namespace_from_env(),
)

place = PlaceDetails(
    source_url="https://www.google.com/maps/place/Fiamma",
    resolved_url="https://www.google.com/maps/place/Fiamma",
    name="Fiamma",
    category="イタリア料理店",
    rating=None,
    review_count=None,
    address="1 The Knolls, シンガポール 098297",
    located_in="Capella Singapore",
)

place = repair_place_display_fields(
    place,
    repairer=repairer,
    evidence={
        "city": "Singapore",
        "country": "Singapore",
        "source": "downstream guide metadata",
    },
)

print(place.category_display_en)  # "Italian restaurant"
print(place.address_display_en)   # "1 The Knolls, Singapore 098297"
```

The repair request includes raw `name`, `secondary_name`, `category`, `address`,
`located_in`, `address_parts`, and `google_place_id` when present. The model can
only return display translation fields; venue names, reviews, review topics,
tags, neighborhoods, and ranking data are ignored by this helper.

## Maps Search URL Helper

`gmaps-scraper` does not know a downstream guide's region. The caller should
build a specific query from its own context, then pass the resulting URL to the
scraper.

```python
from gmaps_scraper import build_maps_search_url, localize_maps_url, scrape_place

url = build_maps_search_url("Analogue, Singapore")
place = scrape_place(url)
```

For ambiguous names, include the most specific caller-known context. Prefer a
place ID when available, then full address, then city/country:

```python
build_maps_search_url("Analogue, Singapore", gl="sg")
build_maps_search_url("Analogue, 30 Victoria Street, Singapore", gl="sg")
```

If the caller has a Google place ID, include it:

```python
url = build_maps_search_url(
    "Ad Astra, Taipei",
    place_id="ChIJHeQU2UCpQjQRhNcDeQ1fUMI",
    gl="tw",
)
```

The helper defaults to `hl="en"` and `gl="us"`. Keeping `hl=en` reduces
localized UI surprises for the scraper and usually gives English-readable
labels. Override `gl` when the downstream app has a regional bias such as `sg`,
`tw`, `au`, or `uk`. Override `hl` only when you intentionally want Google Maps
to render in another language and can tolerate more localized output.

`scrape_place()` itself preserves the URL it receives. For direct place or CID
URLs, call `localize_maps_url(url, hl="en", gl="us")` before scraping when you
want the same English-readable bias explicitly:

```python
place = scrape_place(localize_maps_url("https://www.google.co.jp/maps/place/Tokyo+Tower"))
```

`gl` is a regional search bias, not proof of location. Downstream consumers that
have expected city/country context should still validate the resolved place
address or coordinates before accepting a refresh.

## Public API

Common top-level imports:

- `build_maps_search_url`
- `scrape_saved_list`
- `scrape_place`
- `scrape_places`
- `collect_place_snapshot`
- `cached_place_repairer`
- `llm_cache_namespace_from_env`
- `openai_compatible_place_repairer_from_env`
- `needs_display_en`
- `reusable_place_display_fields`
- `reuse_place_display_fields`
- `BrowserSessionConfig`
- `HttpSessionConfig`
- `PlaceDetails`
- `PlaceScrapeResult`
- `PlaceExtractionDiagnostics`

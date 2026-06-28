# Google Places and Saved Lists Scraper

Extract structured data from Google Maps saved lists and individual place pages.

`gmaps-scraper` reads Google Maps runtime data and rendered place-page DOM,
then returns typed Python objects or JSON. It is designed for cache refreshes,
place enrichment, and debugging when Google Maps' page structure changes.

## What It Extracts

- Saved list metadata, owner/collaborators, and place entries
- Place facts such as name, category, rating, review count, address, website,
  phone, plus code, coordinates, Google price range, summarized admission or
  lodging quotes, and photos
- Review topic chips with Google-provided mention counts
- Visible review snippets
- About-tab sections such as `Accessibility` and `Service options`
- Optional English-readable display fields for non-Latin address/category text
- Diagnostics, evidence hashes, and optional debug artifacts

## Requirements

- Python `3.14`
- `uv`
- `curl_cffi` for the primary saved-list fetch path
- `cloakbrowser` for browser-backed place scraping and fallback

## Install

This project is intended to be consumed directly from source rather than from
PyPI.

```bash
uv add git+https://github.com/michaelmwu/gmaps-scraper.git
```

If you vendor the package, also add the runtime dependencies:

```bash
uv add curl-cffi cloakbrowser
```

## Quickstart

Scrape a saved list:

```bash
uv run gmaps-scraper "https://maps.app.goo.gl/MG2Vd5pWBkL7hXL18"
```

Scrape one place:

```bash
uv run gmaps-scraper \
  "https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z" \
  --kind place
```

Batch scrape places:

```bash
uv run gmaps-scraper \
  --kind place \
  --input places.txt \
  --session-dir .gmaps-scraper/session \
  --max-retries 2 \
  --stagger-ms 500 \
  --output place-results.json
```

Build a Maps search URL from downstream context:

```python
from gmaps_scraper import build_maps_search_url

place_url = build_maps_search_url("Analogue, Singapore", gl="sg")
```

`gmaps-scraper` does not infer guide region. If your downstream app knows the
place should be in Singapore, Taipei, Hanoi, or another region, put that context
in the query or pass a Google place ID.

Enable optional LLM repair only when deterministic extraction is thin or needs
English-readable display normalization:

```bash
OPENAI_API_KEY=...
LLM_MODEL=gpt-5-mini

uv run gmaps-scraper \
  --kind place \
  --input places.txt \
  --llm-repair \
  --llm-cache-dir .gmaps-scraper/llm-cache
```

## Library Usage

```python
from pathlib import Path

from gmaps_scraper import (
    BrowserSessionConfig,
    build_maps_search_url,
    cached_place_repairer,
    llm_cache_namespace_from_env,
    openai_compatible_place_repairer_from_env,
    localize_maps_url,
    scrape_place,
    scrape_saved_list,
)

saved_list = scrape_saved_list("https://maps.app.goo.gl/MG2Vd5pWBkL7hXL18")

place_url = build_maps_search_url("Analogue, Singapore")
place = scrape_place(
    place_url,
    browser_session=BrowserSessionConfig(
        profile_dir=Path(".gmaps-scraper/session"),
        human_mouse=True,
    ),
)

direct_place_url = localize_maps_url("https://www.google.co.jp/maps/place/Tokyo+Tower")
direct_place = scrape_place(direct_place_url)

repaired_place = scrape_place(
    place.source_url,
    llm_fallback=cached_place_repairer(
        openai_compatible_place_repairer_from_env(),
        cache_dir=Path(".gmaps-scraper/llm-cache"),
        cache_namespace=llm_cache_namespace_from_env(),
    ),
    llm_tasks=("dom_repair", "display_translation"),
)
```

## Downstream Refresh Pattern

For low-cost refreshes, downstream consumers should scrape deterministically
first, reuse prior English display fields when raw fields are unchanged, and only
then call optional LLM repair if a raw non-Latin address/category still needs
normalization.

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
    repairer = cached_place_repairer(
        openai_compatible_place_repairer_from_env(),
        cache_dir=Path(".gmaps-scraper/llm-cache"),
        cache_namespace=llm_cache_namespace_from_env(),
    )
    translated = repair_place_display_fields(
        fresh,
        repairer=repairer,
        evidence={"city": "Singapore", "country": "Singapore"},
    )
    fresh = translated
```

The downstream app owns the spend policy. `gmaps-scraper` owns the generic
Google Maps repair prompt, evidence shape, script detection, translation memory,
and cache mechanics.

`repair_place_display_fields` does not scrape Google Maps. It only repairs
English-readable display fields from an existing `PlaceDetails`; optional
`evidence` is caller-provided context such as guide city/country.

Reviews and About attributes are collected by default. Use `collect_reviews=False`
or `collect_about=False` in library code, or `--skip-reviews` / `--skip-about` in
the CLI, when a refresh only needs overview facts.

## Documentation

- [Usage Guide](docs/USAGE.md): CLI options, batch scraping, debug artifacts,
  LLM setup, cache behavior, and downstream refresh examples
- [Architecture](docs/ARCHITECTURE.md): extraction layers, diagnostics, LLM repair,
  translation memory, and session/concurrency design
- [Supply Chain](docs/supply-chain.md): dependency cooldowns, locked installs,
  and local artifact handling
- [Contributing](CONTRIBUTING.md): local development, tests, PR expectations,
  and translation-memory promotion
- [Security](SECURITY.md): vulnerability reporting, secret handling, and
  dependency policy

## Development

```bash
uv sync --dev
./scripts/lint.sh
./scripts/typecheck.sh
./scripts/test.sh
```

Run the complete local gate with:

```bash
./scripts/check-all.sh
```

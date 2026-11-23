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

## CLI Reference

Run `uv run gmaps-scraper --help` to see the options available in your checkout.
Pass URLs as positional arguments, through `--input`, or both.

| Option | Default | Description |
| --- | --- | --- |
| `-h`, `--help` | Off | Print CLI help and exit. |
| `--kind {list,place}` | `list` | Select saved-list or place scraping. |
| `--output PATH` | Standard output | Write JSON to a file. |
| `--log-level {DEBUG,INFO,WARNING,ERROR}` | Off | Write scraper logs at the selected level to standard error. |
| `--download-photo PATH` | Off | Download the representative photo for a single place. |
| `--download-main-photo PATH` | Off | Download the main photo for a single place. |
| `--show-browser-window`, `--headed` | Off | Show the browser window. |
| `--timeout-ms MS` | `30000` | Set time limits for fetch operations in milliseconds. |
| `--settle-ms MS` | `3000` | Wait this long after browser navigation before extraction. |
| `--fetch-mode {auto,curl,browser}` | `auto` | Select a fetch mode. Saved lists support all three. Places use the browser with `auto` or `browser`. |
| `--session-dir PATH` | Unset | Store and reuse a persistent browser profile. |
| `--disable-random-window-size` | Off | Use CloakBrowser's default viewport. |
| `--human-mouse` | Off | Enable CloakBrowser's humanized mouse, keyboard, and scroll behavior. |
| `--proxy URL` | `GMAPS_SCRAPER_PROXY`, if set | Set the proxy for HTTP and browser requests. |
| `--http-cookie-jar PATH` | Unset | Persist HTTP cookies in a Netscape-format file. |
| `--debug-output-dir PATH` | Off | Write raw extraction artifacts for a single URL to this directory. |
| `--screenshot-output-dir PATH` | Off | Write place-page screenshots to this directory. |
| `--input PATH` | Unset | Append URLs from a newline-delimited file, or use `-` for standard input. |
| `--max-concurrency N` | `1` | Set the number of batch workers. |
| `--max-retries N` | `0` for lists, `1` for places | Set the number of retries per URL in a batch. |
| `--retry-backoff-ms MS` | `2000` | Set the initial batch retry delay in milliseconds. Successive delays increase linearly. |
| `--stagger-ms MS` | `0` | Delay starts between batch URLs or place workers. |
| `--fail-on-error` | Off | Write batch results, then exit with status `1` if any URL failed. |
| `--llm-repair` | Off | Enable optional OpenAI-compatible LLM repair for places. |
| `--llm-policy {never,on_quality_failure,always}` | `on_quality_failure` | Control when the optional LLM repair runs. |
| `--llm-task {dom_repair,display_translation}` | Both tasks | Select LLM tasks. Repeat to select multiple tasks. Requires `--llm-repair`. |
| `--llm-env-file PATH` | `.env` | Read optional LLM settings from this file. |
| `--llm-cache-dir PATH` | Unset | Cache optional LLM place repairs. Requires `--llm-repair`. |
| `--skip-reviews` | Off | Skip opening a place's Reviews tab. |
| `--skip-about` | Off | Skip opening a place's About tab. |
| `--skip-preview-if-dom-complete` | Off | Skip the HTTP preview when a full place overview supplies the core facts. |
| `--dump-debug-output` | Off | Write debug artifacts for a single URL under `.gmaps-debug/`. |

### Proxy, Timeouts, and Cookies

Use `--proxy` to override `GMAPS_SCRAPER_PROXY`. For an authenticated proxy,
set the environment variable to keep credentials out of command arguments.
Use `--session-dir` to retain browser cookies and `--http-cookie-jar` to retain
cookies used by the HTTP client.

`--timeout-ms` controls operations such as browser navigation and HTTP requests.
`--settle-ms` adds a browser-only wait. Batch runs also include any delays from
`--stagger-ms` and retries.

## Fetch Modes

```bash
uv run gmaps-scraper URL --fetch-mode auto
uv run gmaps-scraper URL --fetch-mode curl
uv run gmaps-scraper URL --fetch-mode browser
```

- `auto` uses `curl_cffi` first and falls back to the browser if HTTP collection
  or parsing fails.
- `curl` uses only the HTTP path.
- `browser` uses only the browser path.

Use `auto` or `browser` for place scraping. Both use the browser. The `curl`
mode is available for saved lists only.

HTTP requests for saved lists and place previews make up to three attempts on
transient transport errors, HTTP 429, and HTTP 500/502/503/504. Each GET shares
`--timeout-ms` across its attempts and delays, with a one-second minimum.
Saved-list continuation and capped expansion share a single completion budget.
Retries stop if a delay would exhaust the available budget or if
`Retry-After` exceeds 30 seconds.

Saved-list JSON includes `diagnostics` with the collector and source artifact,
browser fallback status, and whether the HTTP preload was expanded or
paginated. When Google reports a row total, `loaded_rows` and `expected_rows`
count raw saved-list entries before parsing and deduplication. Check
`quality_flags` for `partial_payload` before replacing previously saved results.
`fewer_places_than_loaded_rows` marks a difference between the raw and parsed
counts, including when deduplication merges rows.
Missing addresses, CIDs, or a list ID are reported separately.

## Batch Saved-List Scraping

Pass multiple saved-list URLs or use `--input` with a newline-delimited file.
Use `--input -` for stdin. Blank lines and comments in the input are ignored.

```bash
uv run gmaps-scraper --kind list --input lists.txt --fail-on-error --output list-results.json
```

Batch results are returned as `{"results": [...]}` in input order. Each result
contains its `source_url` and either a `saved_list` object or an `error` message.
A single positional URL still returns the saved-list object directly. Either
kind of batch writes all its results before returning status `1` when
`--fail-on-error` is set and any scrape fails. By default, the status is `0`
when the batch finishes and writes its results.

Saved-list batches default to one worker, using the configured browser profile
and HTTP cookie jar for each URL. With `--max-concurrency` above `1`, workers
use separate profile subdirectories and cookie jars. `--stagger-ms` adds a
delay before each worker starts and between URLs within each worker. Debug
dumps are available for single-list runs.

Use `--max-retries` to retry failures per URL. Saved-list batches default to
zero retries. `--retry-backoff-ms` sets the initial delay, which increases
linearly on successive retries. Results include `attempts` for URLs tried
more than once.

The Python library exposes the same workflow through `scrape_saved_lists(urls)`,
which returns `SavedListScrapeResult` objects with `saved_list` and `error`
attributes.

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

For overview refreshes, `--skip-preview-if-dom-complete` avoids an extra HTTP
request when a full place view already supplies a name, address, phone, website,
and coordinates from the view or the canonical URL. Preview-only fields can be
absent in that mode. A batch can accept such a result with quality flags that
identify missing fields. The Python functions `scrape_place()`,
`scrape_places()`, and `collect_place_snapshot()` accept
`skip_preview_if_dom_complete=True`.

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

## Logs and Debug Artifacts

Use `--log-level INFO` to see scraper activity on stderr while stdout remains
JSON. `DEBUG` adds details about browser operations and consent handling.
URL hashes correlate events for the same input and resolved URL.
Python callers can configure loggers under `gmaps_scraper` with standard logging.

Use `--debug-output-dir` to write place debug artifacts to a specific directory.

```bash
uv run gmaps-scraper URL \
  --kind place \
  --debug-output-dir .context/places/example
```

`--screenshot-output-dir` writes overview and reviews screenshots. Debug dumps
include extraction snapshots under `artifacts/`, the final place output,
diagnostics, and LLM evidence.

Current selectors live in the overview extractor and panel helpers.
[Overview extractor](../src/gmaps_scraper/data/place_extractor.js) and
[panel helpers](../src/gmaps_scraper/place_scraper.py).

The `selector-recipe.json` artifact and its manifest entry have been retired.
The deprecated selector recipe helpers still read and write legacy recipes for
offline use. For tools that expect the archived v1 file, call
`write_default_place_selector_recipe(path)`.

Reviews and About collection are enabled by default for place scraping. Use
`--skip-reviews` when you only need overview facts and do not want to open the
Reviews tab. Use `--skip-about` when About-panel attributes are not needed.
Skipping Reviews still preserves any review-count/topic evidence present on the
overview page, but it will not expand the Reviews tab for more topics or visible
review snippets.

## Optional LLM Repair

LLM repair is opt-in. Deterministic DOM and preview extraction always run first.

### LLM Setup

Export the API key and model before running the CLI. You can also put the
settings in `.env` or pass a different file with `--llm-env-file PATH`. Use
`OPENAI_BASE_URL` to select a different OpenAI-compatible endpoint.

```bash
export OPENAI_API_KEY=...
export LLM_MODEL=gpt-5-mini

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

`PlaceDetails.cid` contains the numeric Google Maps CID as a decimal string
when it can be recovered from the resolved Maps URL, the place preview, or
the input Maps URL. Optional DOM repair can supply a missing CID from the
evidence. JSON omits `cid` when it is unavailable.

`gl` is a regional search bias, not proof of location. Downstream consumers that
have expected city/country context should still validate the resolved place
address or coordinates before accepting a refresh.

## Weekly opening hours

Place output includes `opening_hours` when Google Maps supplies a complete
seven-day schedule in the Overview panel. Each entry has a localized `day` and
an `hours` array that preserves each displayed interval or closure label. The
scraper includes `is_open_24h` for recognized all-day or closed labels.
The schedule follows Google's display order and can include holiday changes.
The `status` field still reports the current open or closed message. The
`opening_hours_incomplete` quality flag indicates that the scraper found
schedule rows but could not validate all seven days.

## Public API

Common top-level imports:

- `build_maps_search_url`
- `scrape_saved_list`
- `scrape_saved_lists`
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

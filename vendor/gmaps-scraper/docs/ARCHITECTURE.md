# gmaps-scraper Architecture

## Two Extraction Modes

`gmaps-scraper` has two related but different scraping surfaces:

- Saved lists: parse list metadata and saved place entries from Google Maps
  runtime/preloaded payloads.
- Place pages: parse detailed place facts from the rendered Google Maps place
  panel, preview payloads, and conservative fallbacks.

Saved-list scraping is primarily a structured payload problem. Place-page
scraping is primarily a browser DOM problem. Keep the two paths separate unless
a helper is genuinely generic, such as URL parsing or data models.

## Saved List Extraction Strategy

Saved-list URLs usually resolve to Google Maps pages containing placelist
runtime data. The scraper prefers the HTTP/preloaded payload path because it is
faster and easier to reproduce than a browser session. Browser artifacts remain
available as fallback and for debugging.

The parser treats the explicit placelist ID as the strongest signal:

1. Extract the list ID from the resolved URL `!2s...` segment when available.
2. Prefer runtime strings that contain that exact list ID.
3. Fall back to strings containing `maps/placelists/list/`.
4. Treat the placelist URL marker as a locator, not proof that the surrounding
   node is the final parse target.

Place entries inside saved lists are detected structurally. The coordinate tuple
pattern `[null, null, lat, lng]` is the strongest signal for a saved place
record. The parser then uses surrounding parent structures to recover name,
address, note, favorite status, Google identifiers, ownership metadata, and a
direct CID URL for addressless records when available, or a Maps search URL
otherwise.

Saved-list output preserves:

- `source_url`: the caller-provided URL
- `resolved_url`: the final URL after redirects
- `list_id`, `title`, and `description`
- owner and collaborator metadata
- saved places with name, address, note, favorite status, coordinates, URL, and
  optional identifiers

Saved-list parsing should stay defensive. Google can reorder or extend runtime
arrays without notice, so avoid hardcoded deep indexes unless a surrounding
structural check makes them safe.

## Place Page Extraction Strategy

`gmaps-scraper` treats Google Maps as an unstable HTML application, not a
stable data API. The extractor therefore prioritizes provenance over simply
finding text that looks right.

The preferred extraction order is:

1. Structured DOM rows from the loaded Google Maps place page.
2. Internal preview payloads when the DOM is incomplete or unavailable.
3. Plain-text fallback from page snapshots as a last resort.

Each lower layer must be more conservative than the layer above it. A fallback
should prevent obvious bad data from being stored; it should not infer business
facts from nearby prose.

## Structured DOM Rows

The browser extractor runs in the page and first targets Google Maps detail
rows. For example, address and website usually appear as `button` or `a`
elements with `data-item-id` values such as `address`, `authority`, `phone:*`,
or `oloc`.

This is the strongest source because the row shape says what the value means.
The extractor may also use stable row structure plus the Google Maps icon glyph
when labels are localized. It should not depend on English-only `aria-label`
prefixes such as `Address:` because pages can render localized labels even when
the requested URL includes `hl=en`.

## Preview Payloads

Preview payloads are Google Maps internal `/maps/preview/place` responses or
embedded data blobs discovered while loading a place page. They are nested
JSON-like arrays, not a public schema.

These payloads can contain useful values when the rendered DOM is thin, blocked,
or only partially available. Because their shape is undocumented, extraction
from preview payloads should favor strongly typed signals: coordinates, plus
codes, phone-looking fields, postal addresses, and compact address arrays.
When the final canonical `/maps/place/` URL encodes explicit `!3d`/`!4d` pin
coordinates, those coordinates take precedence: preview payloads can include
unrelated nearby entities or booking offers alongside the selected place. An
`@lat,lng` viewport center remains a fallback only when structured coordinates
are unavailable.

## Legacy Payloads

Legacy payloads are older scraper/cache records or broad page snapshots that do
not preserve DOM-row provenance. In practice this means fields such as raw body
text, line lists, or historical `address` strings that were already extracted
before the current structured row strategy existed.

The sanitizer handles these records so existing downstream caches can be
refreshed safely. It must be stricter than the structured extractor because it
cannot tell whether a short line came from an address row, a review, a category
chip, or a service-option row.

## Address Policy

Address extraction should prefer explicit address rows. A value from a
structured `data-item-id="address"` row can be accepted even when it is
locality-only, such as `Baku, Azerbaijan`, because the DOM row provides the
address meaning.

Plain-text address fallback is intentionally narrow:

- Accept postal-code, plus-code, digit-plus-locality, and street-keyword
  addresses.
- Accept locality-only strings only when they are compact and have a geographic
  signal, such as a country/subdivision name or region abbreviation.
- Reject URLs, entity tokens, ratings/review counts, status text, phone numbers,
  service-option rows, categories, and review snippets.
- Keep plus codes separate from formatted addresses. A plus code can be useful
  location metadata, but it should not overwrite a real address unless the
  caller deliberately chooses that fallback.

## Adding Fallbacks

Before adding a regex or denylist entry, prefer a structural extractor change.
A fallback is appropriate only when there is a fixture showing why structured
extraction cannot see the value.

New fallback behavior should include:

- A fixture or unit test with the real Google Maps text/HTML shape.
- A comment explaining which layer it protects and why structural extraction is
  insufficient.
- A negative test for nearby UI text that could otherwise be misclassified.

Avoid broad heuristics such as "any short comma-separated phrase is an address".
Those rules tend to convert reviews like `good food, friendly owner` or service
options like `Dine-in, Takeout, Delivery` into addresses.

## Review Topics, Reviews, And About Sections

Review topic chips are Google-provided filter chips, not scraper-derived tags.
The browser extractor collects chips from the visible place page, opens the
Reviews tab, and expands `+N` groups when Google renders them. A topic count is
the number of reviews Google says mention that topic.

Visible review snippets are returned as evidence-backed snippets only. They are
not a full review crawl, and LLM repair is not allowed to synthesize review text.

About-tab attributes are grouped by Google section title, such as
`Accessibility` or `Service options`. These are factual place attributes from
the rendered About panel. They should stay separate from downstream guide tags
or editorial categories.

## Place Diagnostics And Quality Gates

Every `PlaceDetails` result can include `PlaceExtractionDiagnostics`. Public
diagnostics are intentionally compact:

- `quality_flags` tells the caller why the result may need retry or repair.
- `confidence` is a coarse deterministic extraction score.
- `llm_used` indicates whether a fresh LLM-derived repair was applied.
- `repair_source` distinguishes `llm`, `cache`, and `translation_memory`.
- `evidence_hash` and `prompt_version` make repair cache entries reproducible.

Debug dumps include fuller field-source and missing-field metadata. That detail
is useful for scraper maintenance, but most downstream consumers should use the
public diagnostics plus the structured fields.

Important quality flags include:

- `limited_view`: Google rendered a thin or blocked place view.
- `search_result_page`: the page looks like a search result rather than a full
  place result.
- `thin_place_result`: several core facts are missing.
- `needs_address_display_en`: the raw address contains non-Latin script and has
  no English-readable display field.
- `needs_category_display_en`: the raw category contains non-Latin script and
  has no English-readable display field.

## LLM Repair

LLMs are optional repair tools, not the primary extraction path. Deterministic
DOM and preview extraction always run first. A caller may provide an LLM repair
callback for thin or suspicious place results, but the caller owns the spend
policy: model choice, credentials, cache directory, per-run budget, and whether
a particular refresh is allowed to call a model.

The scraper owns generic Google Maps repair mechanics:

- compact sanitized DOM evidence
- quality flags and evidence hashes
- prompt/schema shape for Google Maps facts
- approved and learned translation memory
- repair cache keys and provenance

Downstream consumers own product policy:

- whether a refresh should use LLM at all
- whether prior display fields can be reused
- how to merge old cache records with new scrape results
- product-specific tags, neighborhoods, guide keywords, and ranking semantics

The default policy is `llm_policy="on_quality_failure"`. The repair path runs
only for critical quality failures, missing names, low confidence, or fields that
need English-readable display normalization. `llm_policy="never"` disables model
repair, and `llm_policy="always"` is intended for debugging or controlled
backfills.

LLM work is task-scoped. `dom_repair` allows the model to repair generic Google
Maps facts when extraction is thin or suspicious. `display_translation` allows
English-readable display normalization for raw address/category fields. Callers
can pass `llm_tasks=("display_translation",)` to avoid DOM repair, or
`llm_tasks=("dom_repair",)` to avoid display translation. The CLI exposes the
same split with repeated `--llm-task` flags.

The LLM is allowed to backfill bounded Google Maps place fields, but it must only
return values supported by the sanitized evidence. Review text is not accepted
through the LLM path. Review topics and About sections are accepted only when the
returned labels and counts are present in the deterministic evidence.

## LLM Cache And Translation Memory

`cached_place_repairer` wraps any provider-neutral repair callback with a disk
cache. Cache keys include the source URL, resolved URL, Google place ID when
known, evidence hash, prompt version, and model namespace. The namespace should
come from `llm_cache_namespace_from_env()` so `.env` and `llm.local.json` model
settings are included before cache keys are generated.

When `--llm-cache-dir` is used, the scraper also stores exact typed
translation-memory entries learned from successful `address_display_en` and
`category_display_en` repairs in `translation-memory.learned.json`. Learned
memory is checked before the repair cache and before any model call. This lets
later runs reuse known category labels and address components without spending
LLM tokens.

Translation memory is intentionally narrow. It applies to:

- category labels
- city/country/neighborhood components
- floor/building suffixes
- known address tokens

It must not learn or translate:

- reviews
- review topics
- venue names by default
- editorial descriptions
- broad full-address rewrites that are not safe component replacements

Approved memory lives in the package data. Learned memory is local to the cache
directory until a downstream user or maintainer promotes stable entries through a
normal PR with tests.

## Downstream Refresh Strategy

For refreshes, downstream consumers should first scrape with `llm_policy="never"`
and reuse previous `address_display_en` / `category_display_en` when the raw
`address` or `category` is unchanged and the prior display value does not still
need English normalization. The public helpers `needs_display_en`,
`reusable_place_display_fields`, and `reuse_place_display_fields` exist so
consumers can apply this policy without copying script detection or translation
memory logic.

Only when the raw field changed, the prior display field is missing/stale, and
the raw value still needs English display normalization should a downstream
consumer run `repair_place_display_fields()` with a stable cache-backed repairer.
That helper does not scrape the page again; it builds a
`display_translation`-only repair request from an existing `PlaceDetails`.
Callers may pass generic evidence such as guide/list city, country, or region.
`located_in` remains the Google Maps containing-place field, such as a hotel,
mall, building, or complex, not city/country. This keeps the spend decision in
the product while keeping Google Maps-specific translation memory and prompt
behavior in `gmaps-scraper`.

## Optional Panel Collection

Place scraping always starts with the overview panel. Reviews and About are
additional panel collection steps:

- Reviews collection clicks the Reviews tab, expands visible topic chips, and
  captures visible review snippets.
- About collection clicks the About tab and captures attribute sections such as
  Accessibility or Service options.

Both are enabled by default because they are part of the full place output
contract. Callers can pass `collect_reviews=False` or `collect_about=False`, and
the CLI exposes `--skip-reviews` and `--skip-about`, when a refresh only needs
core overview facts. Skipping Reviews does not delete overview review counts or
topic chips already present in the initial DOM; it only avoids the extra tab
interaction and review screenshot.

## Search URL Construction

`gmaps-scraper` should not infer a caller's guide region. If a downstream guide
knows that a place belongs to Singapore, Taipei, Hanoi, or another region, that
context should be encoded in the Maps search URL before scraping. For example,
the caller should pass a query like `Analogue, Singapore`, not just `Analogue`.

The public `build_maps_search_url()` helper only formats a caller-provided query
and optional `query_place_id`. It defaults to `hl=en` because English-rendered
Maps pages reduce localized UI surprises and usually produce English-readable
labels. Callers may override `gl` for regional bias. Callers may override `hl`,
but non-English UI can increase selector and normalization drift.

`gl` is a bias, not validation. A downstream consumer with expected guide region
or city/country context should validate the resolved place after scraping. The
scraper should not silently reject region mismatches because it does not know the
product's intended geography.

## Batch Scraping And Session Reuse

`scrape_places` reuses browser contexts within a sequential worker and supports
parallel workers with `max_concurrency`. `stagger_ms` delays worker starts so a
large batch does not hit Google all at once.

Persistent browser profiles are scoped per worker in parallel mode. HTTP cookie
jar paths are also scoped per worker so concurrent preview enrichment cannot
race on the same cookie file. Use a stable `session_dir` and low concurrency
when the goal is long-lived session reuse; use higher concurrency only when the
caller accepts separate worker session state.

LLMs may also be useful offline for proposing selectors when Google changes the
page. Durable improvements should still become deterministic extraction rules
plus fixtures that can be reviewed, tested, and rerun without model cost or
variability.

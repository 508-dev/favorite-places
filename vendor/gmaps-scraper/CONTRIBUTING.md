# Contributing

Thanks for improving `gmaps-scraper`. The project supports two main surfaces:
saved-list parsing and individual place-page extraction. Changes should keep
those boundaries clear and include tests that make the expected scraper behavior
easy to review.

## Development Setup

```bash
uv sync --dev
uv run prek install
```

`uv sync --dev` installs the project and development dependencies. `uv run prek
install` installs the repository's git hooks so lint and type checks run before
commits.

Run the local gates before opening or updating a PR:

```bash
./scripts/lint.sh
./scripts/typecheck.sh
./scripts/test.sh
./scripts/check-all.sh
```

`./scripts/check-all.sh` is the same local gate in one command. It runs linting,
type checking, and unit tests.

Dependency changes should follow [docs/supply-chain.md](docs/supply-chain.md):
keep `uv.lock` committed and prefer locked installs.

## Pull Request Expectations

- Keep changes scoped to the scraper behavior being fixed.
- Add focused tests for parser, scraper, CLI, or public API changes.
- Prefer structural DOM extraction over regex fallback.
- Add negative tests for UI text that could be misclassified.
- Keep raw Google fields intact when adding normalized/display fields.
- Do not add downstream product semantics such as guide tags, neighborhoods, or
  ranking logic to this package.

## Adding Place Extraction Fallbacks

Before adding a new fallback, confirm that the structured DOM or preview payload
cannot provide the field. If a fallback is still needed, include:

- a fixture or unit test with the real Google Maps text/HTML shape
- a negative case for nearby review/category/service-option text
- a narrow rule that rejects URLs, page chrome, ratings, phone numbers, and
  review snippets when extracting addresses

## LLM Repair Changes

LLM repair is optional and downstream-controlled. Deterministic extraction must
continue to work without model credentials.

When changing LLM repair behavior:

- keep the allowed output fields bounded to Google Maps facts
- preserve raw scraped fields as evidence
- do not synthesize or translate review text
- require evidence matches for LLM-provided review topics and About attributes
- update prompt-versioned tests when prompt behavior changes
- keep cache namespace and evidence hashing stable unless intentionally changed

## Translation Memory Promotion

Production or enrichment runs can create local learned entries at:

```text
<llm-cache-dir>/translation-memory.learned.json
```

These entries are local until promoted. Promotion should happen through a normal
PR that updates the bundled approved memory and tests.

Inspect learned entries first:

```bash
uv run python scripts/promote_translation_memory.py \
  "$CONDUCTOR_ROOT_PATH/.gmaps-scraper/llm-cache/translation-memory.learned.json" \
  --dry-run
```

Then promote selected safe entries:

```bash
uv run python scripts/promote_translation_memory.py \
  "$CONDUCTOR_ROOT_PATH/.gmaps-scraper/llm-cache/translation-memory.learned.json"
```

Promotion is appropriate for:

- category labels
- city, country, and neighborhood components
- floor or building suffixes
- known address tokens
- structural address patterns with conservative capture templates

Do not promote:

- reviews
- review topics
- venue names by default
- editorial descriptions
- broad full-address rewrites that are not safe component replacements

Pattern entries must be typed and conservative. Capture templates support only
numbered substitutions such as `{1}` and `{2}`:

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

Add tests for promoted mappings, especially when they involve non-Latin scripts
or floor/unit syntax.

## Documentation

Keep the top-level README short: overview, quickstart, and links. Put detailed
usage in [docs/USAGE.md](docs/USAGE.md) and design rationale in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

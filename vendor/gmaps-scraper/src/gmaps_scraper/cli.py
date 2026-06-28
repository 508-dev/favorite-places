"""Command-line interface for the GMaps scraper."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from gmaps_scraper.debug_dump import write_debug_dump, write_place_debug_dump
from gmaps_scraper.llm import (
    LLMRepairError,
    cached_place_repairer,
    llm_cache_namespace_from_env,
    openai_compatible_place_repairer_from_env,
)
from gmaps_scraper.models import PlaceDetails, PlaceLLMRepairRequest
from gmaps_scraper.place_scraper import (
    _PLACE_LLM_PROMPT_VERSION,
    PlaceLLMRepairer,
    PlaceLLMTask,
    _build_place_details,
    _build_place_diagnostics,
    _build_place_llm_evidence,
    _diagnostics_for_llm_tasks,
    _extract_llm_repair_source,
    _hash_evidence,
    _merge_llm_place_fields,
    _merge_place_sources,
    _place_detail_values,
    _repair_source_used_llm,
    _should_use_llm_repair,
    collect_place_snapshot,
    scrape_place,
    scrape_places,
)
from gmaps_scraper.scraper import (
    _HTTP_IMPERSONATE,
    DEFAULT_COLLECTION_MODE,
    BrowserSessionConfig,
    HttpSessionConfig,
    ScrapeError,
    _import_curl_requests,
    _raise_for_status,
    collect_saved_list_result,
)
from gmaps_scraper.url_tools import extract_list_id

_DEFAULT_DEBUG_DIR_NAME = ".gmaps-debug"


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="Google Maps list or place URL(s)")
    parser.add_argument(
        "--kind",
        choices=["list", "place"],
        default="list",
        help="Scrape a list or an individual place page.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON output path")
    parser.add_argument(
        "--download-photo",
        type=Path,
        help="For place scraping, download the representative photo to this file path.",
    )
    parser.add_argument(
        "--download-main-photo",
        type=Path,
        help="For place scraping, download the main place photo to this file path.",
    )
    parser.add_argument(
        "--show-browser-window",
        "--headed",
        dest="show_browser_window",
        action="store_true",
        help="Show the browser window while scraping for debugging.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=30_000,
        help="Overall fetch timeout in milliseconds.",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=3_000,
        help="Extra browser-only wait time after the page loads.",
    )
    parser.add_argument(
        "--fetch-mode",
        dest="collection_mode",
        choices=["auto", "curl", "browser"],
        default=DEFAULT_COLLECTION_MODE,
        help=(
            "Fetch mode: auto (curl_cffi with browser fallback), curl, or "
            "browser."
        ),
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="Reuse a persistent browser profile stored in this directory.",
    )
    parser.add_argument(
        "--disable-random-window-size",
        action="store_true",
        help="Use CloakBrowser's default viewport instead of a weighted random desktop size.",
    )
    parser.add_argument(
        "--human-mouse",
        action="store_true",
        help="Enable CloakBrowser humanized mouse, keyboard, and scroll behavior.",
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("GMAPS_SCRAPER_PROXY"),
        help=(
            "Proxy URL passed through to curl_cffi and the browser. Prefer "
            "GMAPS_SCRAPER_PROXY for authenticated proxies so credentials "
            "do not appear in shell history or process listings."
        ),
    )
    parser.add_argument(
        "--http-cookie-jar",
        type=Path,
        help="Persist curl_cffi cookies in this Netscape-format cookie jar file.",
    )
    parser.add_argument(
        "--debug-output-dir",
        type=Path,
        help=(
            "Directory for raw runtime artifacts, ranked candidate payloads, "
            "and per-place debug dumps."
        ),
    )
    parser.add_argument(
        "--screenshot-output-dir",
        type=Path,
        help="For place scraping, write page screenshots to this directory.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help=(
            "For place scraping, read additional place URLs from a newline-delimited "
            "file, or use '-' for stdin."
        ),
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=1,
        help="For batch place scraping, number of concurrent workers. Defaults to 1.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=1,
        help=(
            "For batch place scraping, per-place retries after failures or "
            "retryable quality flags."
        ),
    )
    parser.add_argument(
        "--retry-backoff-ms",
        type=int,
        default=2_000,
        help="For batch place scraping, linear retry backoff in milliseconds.",
    )
    parser.add_argument(
        "--stagger-ms",
        type=int,
        default=0,
        help="For batch place scraping, delay starts between URLs or workers.",
    )
    parser.add_argument(
        "--llm-repair",
        action="store_true",
        help=(
            "For place scraping, enable optional OpenAI-compatible LLM repair "
            "from OPENAI_API_KEY, OPENAI_BASE_URL, and LLM_MODEL."
        ),
    )
    parser.add_argument(
        "--llm-policy",
        choices=["never", "on_quality_failure", "always"],
        default="on_quality_failure",
        help="When to call the optional LLM repair callback for place scraping.",
    )
    parser.add_argument(
        "--llm-task",
        action="append",
        choices=["dom_repair", "display_translation"],
        help=(
            "Restrict optional LLM work for place scraping. Repeat to enable "
            "multiple tasks. Defaults to both DOM repair and display translation."
        ),
    )
    parser.add_argument(
        "--llm-env-file",
        type=Path,
        default=Path(".env"),
        help="Env file to read for optional LLM repair settings.",
    )
    parser.add_argument(
        "--llm-cache-dir",
        type=Path,
        help="Cache optional LLM place repairs in this directory.",
    )
    parser.add_argument(
        "--skip-reviews",
        action="store_true",
        help="For place scraping, skip opening the Reviews tab.",
    )
    parser.add_argument(
        "--skip-about",
        action="store_true",
        help="For place scraping, skip opening the About tab.",
    )
    parser.add_argument(
        "--dump-debug-output",
        action="store_true",
        help=(
            "Write debug artifacts to a default hidden directory in the current "
            f"working directory: `{_DEFAULT_DEBUG_DIR_NAME}`."
        ),
    )
    return parser


def main() -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args()
    if not args.urls and args.input is None:
        parser.error("at least one URL or --input is required.")
    browser_session = None
    if (
        args.session_dir is not None
        or args.proxy is not None
        or args.disable_random_window_size
        or args.human_mouse
    ):
        browser_session = BrowserSessionConfig(
            profile_dir=args.session_dir,
            proxy=args.proxy,
            window_size=None if args.disable_random_window_size else "random",
            human_mouse=args.human_mouse,
        )
    http_session = None
    if args.http_cookie_jar is not None or args.proxy is not None:
        http_session = HttpSessionConfig(
            cookie_jar_path=args.http_cookie_jar,
            proxy=args.proxy,
        )

    if args.kind == "place":
        if args.collection_mode == "curl":
            parser.error(
                "Place scraping currently requires browser mode. "
                "Use `--fetch-mode browser`."
            )
        if args.llm_cache_dir is not None and not args.llm_repair:
            parser.error("`--llm-cache-dir` requires `--llm-repair`.")
        if args.llm_task is not None and not args.llm_repair:
            parser.error("`--llm-task` requires `--llm-repair`.")
        llm_tasks = cast(
            tuple[PlaceLLMTask, ...],
            tuple(args.llm_task or ("dom_repair", "display_translation")),
        )
        collect_reviews = not args.skip_reviews
        collect_about = not args.skip_about
        llm_fallback = None
        if args.llm_repair:
            try:
                llm_fallback = openai_compatible_place_repairer_from_env(
                    env_file=args.llm_env_file,
                )
                if args.llm_cache_dir is not None:
                    cache_namespace = llm_cache_namespace_from_env(
                        env_file=args.llm_env_file,
                    )
                    llm_fallback = cached_place_repairer(
                        llm_fallback,
                        cache_dir=args.llm_cache_dir,
                        cache_namespace=cache_namespace,
                    )
            except LLMRepairError as exc:
                parser.exit(1, f"{parser.prog}: error: {exc}\n")
        place_urls = _read_place_urls(args.urls, args.input)
        is_batch = len(place_urls) > 1 or args.input is not None
        if is_batch:
            if args.download_photo is not None or args.download_main_photo is not None:
                parser.error("place photo downloads are supported only for one URL.")
            if args.dump_debug_output or args.debug_output_dir is not None:
                parser.error("place debug dumps are supported only for one URL.")
            if args.screenshot_output_dir is None:
                batch_results = scrape_places(
                    place_urls,
                    headless=not args.show_browser_window,
                    timeout_ms=args.timeout_ms,
                    settle_time_ms=args.settle_ms,
                    browser_session=browser_session,
                    http_session=http_session,
                    llm_fallback=llm_fallback,
                    llm_policy=args.llm_policy,
                    llm_tasks=llm_tasks,
                    collect_reviews=collect_reviews,
                    collect_about=collect_about,
                    max_concurrency=args.max_concurrency,
                    max_retries=args.max_retries,
                    retry_backoff_ms=args.retry_backoff_ms,
                    stagger_ms=args.stagger_ms,
                )
            else:
                batch_results = scrape_places(
                    place_urls,
                    headless=not args.show_browser_window,
                    timeout_ms=args.timeout_ms,
                    settle_time_ms=args.settle_ms,
                    browser_session=browser_session,
                    http_session=http_session,
                    llm_fallback=llm_fallback,
                    llm_policy=args.llm_policy,
                    llm_tasks=llm_tasks,
                    collect_reviews=collect_reviews,
                    collect_about=collect_about,
                    max_concurrency=args.max_concurrency,
                    max_retries=args.max_retries,
                    retry_backoff_ms=args.retry_backoff_ms,
                    stagger_ms=args.stagger_ms,
                    screenshot_output_dir=args.screenshot_output_dir,
                )
            payload = json.dumps(
                {"results": [result.to_dict() for result in batch_results]},
                indent=2,
                ensure_ascii=False,
            )
            if args.output is not None:
                args.output.write_text(f"{payload}\n", encoding="utf-8")
            else:
                print(payload)
            return 0
        if (
            args.download_photo is not None
            and args.output is not None
            and args.download_photo == args.output
        ):
            parser.error("`--download-photo` must be different from `--output`.")
        if (
            args.download_main_photo is not None
            and args.output is not None
            and args.download_main_photo == args.output
        ):
            parser.error("`--download-main-photo` must be different from `--output`.")
        if (
            args.download_photo is not None
            and args.download_main_photo is not None
            and args.download_photo == args.download_main_photo
        ):
            parser.error(
                "`--download-photo` and `--download-main-photo` must be different paths."
            )
        place_debug_output_dir = _resolve_place_debug_output_dir(
            place_url=place_urls[0],
            dump_debug_output=args.dump_debug_output,
            debug_output_dir=args.debug_output_dir,
        )
        if place_debug_output_dir is None:
            screenshot_path = _place_screenshot_path(
                args.screenshot_output_dir,
                place_urls[0],
                stage="reviews",
            )
            overview_screenshot_path = _place_screenshot_path(
                args.screenshot_output_dir,
                place_urls[0],
                stage="overview",
            )
            if screenshot_path is None:
                place_result = scrape_place(
                    place_urls[0],
                    headless=not args.show_browser_window,
                    timeout_ms=args.timeout_ms,
                    settle_time_ms=args.settle_ms,
                    browser_session=browser_session,
                    http_session=http_session,
                    llm_fallback=llm_fallback,
                    llm_policy=args.llm_policy,
                    llm_tasks=llm_tasks,
                    collect_reviews=collect_reviews,
                    collect_about=collect_about,
                )
            else:
                place_result = scrape_place(
                    place_urls[0],
                    headless=not args.show_browser_window,
                    timeout_ms=args.timeout_ms,
                    settle_time_ms=args.settle_ms,
                    browser_session=browser_session,
                    http_session=http_session,
                    llm_fallback=llm_fallback,
                    llm_policy=args.llm_policy,
                    llm_tasks=llm_tasks,
                    collect_reviews=collect_reviews,
                    collect_about=collect_about,
                    screenshot_path=screenshot_path,
                    overview_screenshot_path=overview_screenshot_path,
                )
        else:
            place_result, snapshot, merged_snapshot, evidence = _scrape_place_for_debug(
                place_urls[0],
                headless=not args.show_browser_window,
                timeout_ms=args.timeout_ms,
                settle_time_ms=args.settle_ms,
                browser_session=browser_session,
                http_session=http_session,
                llm_fallback=llm_fallback,
                llm_policy=args.llm_policy,
                llm_tasks=llm_tasks,
                collect_reviews=collect_reviews,
                collect_about=collect_about,
                screenshot_path=_place_screenshot_path(
                    args.screenshot_output_dir or (place_debug_output_dir / "artifacts"),
                    place_urls[0],
                    stage="reviews",
                ),
                overview_screenshot_path=_place_screenshot_path(
                    args.screenshot_output_dir or (place_debug_output_dir / "artifacts"),
                    place_urls[0],
                    stage="overview",
                ),
            )
            assert place_result.diagnostics is not None
            write_place_debug_dump(
                place_urls[0],
                resolved_url=place_result.resolved_url,
                snapshot=snapshot,
                merged_snapshot=merged_snapshot,
                details=place_result,
                evidence=evidence,
                diagnostics=place_result.diagnostics,
                output_dir=place_debug_output_dir,
            )
        if args.download_photo is not None:
            try:
                _download_place_photo(
                    place_result,
                    output_path=args.download_photo,
                    http_session=http_session,
                )
            except RuntimeError as exc:
                parser.exit(1, f"{parser.prog}: error: {exc}\n")
        if args.download_main_photo is not None:
            try:
                _download_place_image(
                    place_result.main_photo_url,
                    output_path=args.download_main_photo,
                    http_session=http_session,
                    referer=place_result.resolved_url or place_result.source_url,
                    missing_message="No main photo URL was found for this place.",
                )
            except RuntimeError as exc:
                parser.exit(1, f"{parser.prog}: error: {exc}\n")
        payload = json.dumps(place_result.to_dict(), indent=2, ensure_ascii=False)
        if args.output is not None:
            args.output.write_text(f"{payload}\n", encoding="utf-8")
        else:
            print(payload)
        return 0
    if args.download_photo is not None or args.download_main_photo is not None:
        parser.error(
            "`--download-photo` and `--download-main-photo` are supported only with `--kind place`."
        )
    if args.llm_repair:
        parser.error("`--llm-repair` is supported only with `--kind place`.")
    if args.llm_task is not None:
        parser.error("`--llm-task` is supported only with `--kind place`.")
    if args.llm_cache_dir is not None:
        parser.error("`--llm-cache-dir` is supported only with `--kind place`.")
    if args.skip_reviews:
        parser.error("`--skip-reviews` is supported only with `--kind place`.")
    if args.skip_about:
        parser.error("`--skip-about` is supported only with `--kind place`.")
    if args.screenshot_output_dir is not None:
        parser.error("`--screenshot-output-dir` is supported only with `--kind place`.")
    if args.input is not None:
        parser.error("`--input` is supported only with `--kind place`.")
    if len(args.urls) != 1:
        parser.error("list scraping requires exactly one URL.")

    artifacts, result = collect_saved_list_result(
        cast(str, args.urls[0]),
        headless=not args.show_browser_window,
        timeout_ms=args.timeout_ms,
        settle_time_ms=args.settle_ms,
        collection_mode=args.collection_mode,
        browser_session=browser_session,
        http_session=http_session,
    )
    debug_output_dir = _resolve_debug_output_dir(
        list_url=cast(str, args.urls[0]),
        resolved_url=artifacts.resolved_url,
        dump_debug_output=args.dump_debug_output,
        debug_output_dir=args.debug_output_dir,
    )
    if debug_output_dir is not None:
        write_debug_dump(
            args.urls[0],
            resolved_url=artifacts.resolved_url,
            runtime_state=artifacts.runtime_state,
            script_texts=artifacts.script_texts,
            html=artifacts.html,
            output_dir=debug_output_dir,
        )
    payload = json.dumps(result.to_dict(), indent=2, ensure_ascii=False)

    if args.output is not None:
        args.output.write_text(f"{payload}\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def _download_place_photo(
    place_result: PlaceDetails,
    *,
    output_path: Path,
    http_session: HttpSessionConfig | None,
) -> None:
    _download_place_image(
        place_result.photo_url,
        output_path=output_path,
        http_session=http_session,
        referer=place_result.resolved_url or place_result.source_url,
        missing_message="No representative photo URL was found for this place.",
    )


def _download_place_image(
    photo_url: str | None,
    *,
    output_path: Path,
    http_session: HttpSessionConfig | None,
    referer: str,
    missing_message: str,
) -> None:
    if photo_url is None:
        raise RuntimeError(missing_message)
    try:
        curl_requests = _import_curl_requests()
        session_kwargs: dict[str, object] = {
            "impersonate": _HTTP_IMPERSONATE,
            "allow_redirects": True,
            "default_headers": True,
            "timeout": 30,
        }
        if http_session is not None and http_session.proxy is not None:
            session_kwargs["proxy"] = http_session.proxy

        with curl_requests.Session(**session_kwargs) as session:
            response = session.get(
                photo_url,
                referer=referer,
            )
            _raise_for_status(response)
            content = response.content

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to download place photo: {exc}") from exc


def _read_place_urls(urls: list[str], input_path: Path | None) -> list[str]:
    result = list(urls)
    if input_path is not None:
        text = (
            sys.stdin.read()
            if str(input_path) == "-"
            else input_path.read_text(encoding="utf-8")
        )
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                result.append(stripped)
    return result


def _place_screenshot_path(
    output_dir: Path | None,
    place_url: str,
    *,
    stage: str,
) -> Path | None:
    if output_dir is None:
        return None
    slug = "".join(character.lower() if character.isalnum() else "-" for character in place_url)
    slug = "-".join(part for part in slug.split("-") if part)
    digest = sha256(place_url.encode("utf-8")).hexdigest()[:8]
    return output_dir / f"{slug[:80] or 'place'}-{digest}-{stage}.png"


def _scrape_place_for_debug(
    place_url: str,
    *,
    headless: bool,
    timeout_ms: int,
    settle_time_ms: int,
    browser_session: BrowserSessionConfig | None,
    http_session: HttpSessionConfig | None,
    llm_fallback: PlaceLLMRepairer | None,
    llm_policy: str,
    llm_tasks: tuple[PlaceLLMTask, ...] = ("dom_repair", "display_translation"),
    collect_reviews: bool = True,
    collect_about: bool = True,
    screenshot_path: Path | None = None,
    overview_screenshot_path: Path | None = None,
) -> tuple[PlaceDetails, dict[str, object], dict[str, object], dict[str, object]]:
    snapshot = collect_place_snapshot(
        place_url,
        headless=headless,
        timeout_ms=timeout_ms,
        settle_time_ms=settle_time_ms,
        browser_session=browser_session,
        http_session=http_session,
        collect_reviews=collect_reviews,
        collect_about=collect_about,
        screenshot_path=screenshot_path,
        overview_screenshot_path=overview_screenshot_path,
    )
    raw_resolved_url = snapshot.get("resolved_url")
    resolved_url = raw_resolved_url if isinstance(raw_resolved_url, str) else None
    if resolved_url is not None and extract_list_id(resolved_url) is not None:
        raise ScrapeError(
            "Place URL resolved to a Google Maps saved list. "
            "Use `--kind list` for saved-list URLs or pass an individual place URL."
        )
    dom_snapshot = cast(
        Mapping[str, object],
        snapshot["dom"] if isinstance(snapshot.get("dom"), dict) else {},
    )
    preview_snapshot = cast(
        Mapping[str, object],
        snapshot["preview"] if isinstance(snapshot.get("preview"), dict) else {},
    )
    merged_snapshot = _merge_place_sources(dom_snapshot, preview_snapshot)
    details = _build_place_details(place_url, resolved_url=resolved_url, snapshot=merged_snapshot)
    evidence = _build_place_llm_evidence(merged_snapshot)
    evidence_hash = _hash_evidence(evidence)
    details.diagnostics = _build_place_diagnostics(
        details,
        merged_snapshot,
        evidence_hash=evidence_hash,
    )
    if llm_fallback is None or not _should_use_llm_repair(
        cast(Literal["never", "on_quality_failure", "always"], llm_policy),
        details.diagnostics,
        tasks=llm_tasks,
    ):
        return details, snapshot, merged_snapshot, evidence
    if llm_policy not in {"always", "on_quality_failure"}:
        raise ValueError(f"Unsupported llm_policy: {llm_policy}")
    details.diagnostics.prompt_version = _PLACE_LLM_PROMPT_VERSION
    request_diagnostics = _diagnostics_for_llm_tasks(details.diagnostics, llm_tasks)
    try:
        repair = llm_fallback(
            PlaceLLMRepairRequest(
                source_url=place_url,
                resolved_url=resolved_url,
                current_fields=_place_detail_values(details),
                diagnostics=request_diagnostics,
                evidence=evidence,
                tasks=list(llm_tasks),
            )
        )
    except Exception as exc:
        details.diagnostics.llm_error = str(exc)
        return details, snapshot, merged_snapshot, evidence
    if repair is None:
        return details, snapshot, merged_snapshot, evidence
    repair_source = _extract_llm_repair_source(repair)
    repaired_snapshot = _merge_llm_place_fields(
        merged_snapshot,
        repair,
        current_fields=_place_detail_values(details),
        llm_tasks=llm_tasks,
    )
    repaired_details = _build_place_details(
        place_url,
        resolved_url=resolved_url,
        snapshot=repaired_snapshot,
    )
    repaired_details.diagnostics = _build_place_diagnostics(
        repaired_details,
        repaired_snapshot,
        evidence_hash=evidence_hash,
        llm_used=_repair_source_used_llm(repair_source),
        repair_source=repair_source,
        prompt_version=_PLACE_LLM_PROMPT_VERSION,
    )
    return repaired_details, snapshot, repaired_snapshot, evidence


def _resolve_debug_output_dir(
    *,
    list_url: str,
    resolved_url: str | None,
    dump_debug_output: bool,
    debug_output_dir: Path | None,
) -> Path | None:
    if debug_output_dir is not None:
        return debug_output_dir
    if not dump_debug_output:
        return None
    list_id = extract_list_id(resolved_url or "") or extract_list_id(list_url) or "unknown-list"
    return Path(os.getcwd()) / _DEFAULT_DEBUG_DIR_NAME / list_id


def _resolve_place_debug_output_dir(
    *,
    place_url: str,
    dump_debug_output: bool,
    debug_output_dir: Path | None,
) -> Path | None:
    if debug_output_dir is not None:
        return debug_output_dir
    if not dump_debug_output:
        return None
    return Path(os.getcwd()) / _DEFAULT_DEBUG_DIR_NAME / _slugify_debug_name(place_url)


def _slugify_debug_name(value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "-" for character in value)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:80] or "place"

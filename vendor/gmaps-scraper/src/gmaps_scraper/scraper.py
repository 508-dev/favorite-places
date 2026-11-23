"""Google Maps saved-list scraper with HTTP and browser collectors."""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookiejar import LoadError, MozillaCookieJar
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal, Required, TypedDict, cast
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from gmaps_scraper._logging import url_log_fields
from gmaps_scraper.browser_humanization import (
    BrowserWindowSize,
    resolve_browser_window_size,
)
from gmaps_scraper.models import SavedList, SavedListCollectionSource, SavedListScrapeResult
from gmaps_scraper.parser import (
    JSONValue,
    ParseError,
    _entitylist_row_counts,
    _extract_script_texts_from_html,
    parse_saved_list_artifacts,
)
from gmaps_scraper.url_tools import extract_list_id_from_text

_logger = logging.getLogger(__name__)


type CollectionMode = Literal["auto", "curl", "browser"]

_CONSENT_URL_MARKERS = ("consent.google", "consent.youtube")
_HTTP_IMPERSONATE = "chrome"
_HTTP_GET_MAX_ATTEMPTS = 3
_HTTP_GET_BACKOFF_BASE_SECONDS = 1.0
_HTTP_GET_RETRY_AFTER_MAX_SECONDS = 30.0
_HTTP_GET_MIN_TIMEOUT_SECONDS = 0.001
_HTTP_GET_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_COLLECTION_MODE: CollectionMode = "auto"
_LINK_TAG_PATTERN = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HTML_ATTRIBUTE_PATTERN = re.compile(
    r"""\b([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(['"])(.*?)\2""",
    re.DOTALL,
)
_ENTITYLIST_PAGE_SIZE_PATTERN = re.compile(r"(%214i|!4i)(\d+)")
_ENTITYLIST_PAGE_SIZE_LIMIT = 500
_ENTITYLIST_CONTINUATION_ANCHOR = re.compile(r"(!3e\d+!4i)(\d+)(?=!|$)")
_ENTITYLIST_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9+/_-]+={0,2}")
_ENTITYLIST_CURSOR_TRANSLATION = str.maketrans("+/", "-_")
_CONSENT_TEXT_MARKERS = (
    "before you continue to google",
    "prima di continuare su google",
    "avant de continuer sur google",
    "bevor sie zu google weitergehen",
    "antes de continuar en google",
    "voordat je doorgaat naar google",
    "g.co/privacytools",
)
_REJECT_BUTTON_LABELS = (
    "Reject all",
    "Rifiuta tutto",
    "Tout refuser",
    "Alle ablehnen",
    "Rechazar todo",
    "Tudo recusar",
    "Alles afwijzen",
    "Odrzuć wszystko",
    "Avvisa allt",
    "Afvis alle",
    "Hylää kaikki",
)
_MORE_OPTIONS_BUTTON_LABELS = (
    "More options",
    "Altre opzioni",
    "Plus d'options",
    "Weitere Optionen",
    "Más opciones",
    "Mais opções",
    "Meer opties",
    "Więcej opcji",
    "Fler alternativ",
    "Flere valgmuligheder",
    "Lisää vaihtoehtoja",
)


@dataclass(slots=True)
class BrowserArtifacts:
    """Artifacts collected over HTTP or in a browser."""

    resolved_url: str | None
    runtime_state: JSONValue | None
    script_texts: list[str]
    html: str
    preload_expanded: bool = False
    preload_paginated: bool = False


@dataclass(slots=True)
class _EntitylistPage:
    payload: list[JSONValue]
    header: list[JSONValue]
    rows: list[JSONValue]
    list_id: str
    total_rows: int
    cursor: str | None


class BrowserProxyConfig(TypedDict, total=False):
    """Playwright-compatible proxy configuration."""

    server: Required[str]
    bypass: str
    username: str
    password: str


@dataclass(slots=True, frozen=True)
class BrowserSessionConfig:
    """Controls browser profile reuse and network identity."""

    profile_dir: Path | None = None
    proxy: str | BrowserProxyConfig | None = None
    window_size: BrowserWindowSize = "random"
    human_mouse: bool = False


@dataclass(slots=True, frozen=True)
class HttpSessionConfig:
    """Controls curl-based cookie persistence and network identity."""

    cookie_jar_path: Path | None = None
    proxy: str | None = None


def _browser_session_for_parallel_worker(
    browser_session: BrowserSessionConfig | None,
    *,
    worker_index: int,
) -> BrowserSessionConfig | None:
    if browser_session is None or browser_session.profile_dir is None:
        return browser_session
    return BrowserSessionConfig(
        profile_dir=browser_session.profile_dir / f"worker-{worker_index + 1}",
        proxy=browser_session.proxy,
        window_size=browser_session.window_size,
        human_mouse=browser_session.human_mouse,
    )


def _http_session_for_parallel_worker(
    http_session: HttpSessionConfig | None,
    *,
    worker_index: int,
) -> HttpSessionConfig | None:
    if http_session is None or http_session.cookie_jar_path is None:
        return http_session
    cookie_jar_path = http_session.cookie_jar_path
    return HttpSessionConfig(
        cookie_jar_path=(
            cookie_jar_path.parent
            / f"{cookie_jar_path.stem}.worker-{worker_index + 1}{cookie_jar_path.suffix}"
        ),
        proxy=http_session.proxy,
    )


class ScrapeError(RuntimeError):
    """Raised when browser automation fails."""


def scrape_saved_list(
    list_url: str,
    *,
    headless: bool = True,
    timeout_ms: int = 30_000,
    settle_time_ms: int = 3_000,
    collection_mode: CollectionMode = DEFAULT_COLLECTION_MODE,
    browser_session: BrowserSessionConfig | None = None,
    http_session: HttpSessionConfig | None = None,
) -> SavedList:
    """Scrape and parse a Google Maps saved list."""
    _, result = collect_saved_list_result(
        list_url,
        headless=headless,
        timeout_ms=timeout_ms,
        settle_time_ms=settle_time_ms,
        collection_mode=collection_mode,
        browser_session=browser_session,
        http_session=http_session,
    )
    return result


def scrape_saved_lists(
    list_urls: Sequence[str],
    *,
    headless: bool = True,
    timeout_ms: int = 30_000,
    settle_time_ms: int = 3_000,
    collection_mode: CollectionMode = DEFAULT_COLLECTION_MODE,
    browser_session: BrowserSessionConfig | None = None,
    http_session: HttpSessionConfig | None = None,
    max_concurrency: int = 1,
    max_retries: int = 0,
    retry_backoff_ms: int = 2_000,
    stagger_ms: int = 0,
) -> list[SavedListScrapeResult]:
    """Scrape saved lists, returning per-URL results in input order.

    Parallel workers keep separate persistent browser profiles and cookie jars.
    Each retry reruns collection and parsing for its own URL.
    """
    urls = [url.strip() for url in list_urls if url.strip()]
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1.")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative.")
    if not urls:
        return []
    worker_count = min(max_concurrency, len(urls))

    def scrape_one(
        list_url: str,
        worker_browser_session: BrowserSessionConfig | None,
        worker_http_session: HttpSessionConfig | None,
    ) -> SavedListScrapeResult:
        last_error = "Saved-list scrape failed."
        total_attempts = max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                saved_list = scrape_saved_list(
                    list_url,
                    headless=headless,
                    timeout_ms=timeout_ms,
                    settle_time_ms=settle_time_ms,
                    collection_mode=collection_mode,
                    browser_session=worker_browser_session,
                    http_session=worker_http_session,
                )
            except Exception as exc:
                last_error = str(exc) or "Saved-list scrape failed."
            else:
                return SavedListScrapeResult(list_url, saved_list=saved_list, attempts=attempt)
            if attempt < total_attempts and retry_backoff_ms > 0:
                time.sleep(attempt * retry_backoff_ms / 1000)
        return SavedListScrapeResult(list_url, error=last_error, attempts=total_attempts)

    def scrape_worker(worker_index: int) -> list[SavedListScrapeResult]:
        worker_browser_session = browser_session
        worker_http_session = http_session
        if max_concurrency > 1:
            worker_browser_session = _browser_session_for_parallel_worker(
                browser_session, worker_index=worker_index
            )
            worker_http_session = _http_session_for_parallel_worker(
                http_session, worker_index=worker_index
            )
        if worker_index > 0 and stagger_ms > 0:
            time.sleep(worker_index * stagger_ms / 1000)

        results: list[SavedListScrapeResult] = []
        worker_urls = urls[worker_index::worker_count]
        for index, list_url in enumerate(worker_urls):
            if index > 0 and stagger_ms > 0:
                time.sleep(stagger_ms / 1000)
            results.append(scrape_one(list_url, worker_browser_session, worker_http_session))
        return results

    if worker_count == 1:
        return scrape_worker(0)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        worker_results = executor.map(scrape_worker, range(worker_count))
        # Each worker processes every nth URL, so interleave its results to restore input order.
        return [
            result for row in zip_longest(*worker_results) for result in row if result is not None
        ]


def collect_saved_list_result(
    list_url: str,
    *,
    headless: bool = True,
    timeout_ms: int = 30_000,
    settle_time_ms: int = 3_000,
    collection_mode: CollectionMode = DEFAULT_COLLECTION_MODE,
    browser_session: BrowserSessionConfig | None = None,
    http_session: HttpSessionConfig | None = None,
) -> tuple[BrowserArtifacts, SavedList]:
    """Collect artifacts and parse a Google Maps saved list."""
    normalized_mode = _normalize_collection_mode(collection_mode)
    _logger.info(
        "Starting saved-list collection mode=%s timeout_ms=%s",
        normalized_mode,
        timeout_ms,
        extra=url_log_fields(list_url),
    )

    if normalized_mode == "browser":
        artifacts = collect_browser_artifacts(
            list_url,
            headless=headless,
            timeout_ms=timeout_ms,
            settle_time_ms=settle_time_ms,
            browser_session=browser_session,
        )
        return artifacts, _parse_saved_list(
            list_url,
            artifacts=artifacts,
            collection_source="browser",
        )

    if normalized_mode == "curl":
        artifacts = collect_http_artifacts(
            list_url,
            timeout_ms=timeout_ms,
            http_session=http_session,
        )
        return artifacts, _parse_saved_list(
            list_url,
            artifacts=artifacts,
            collection_source="http",
        )

    try:
        artifacts = collect_http_artifacts(
            list_url,
            timeout_ms=timeout_ms,
            http_session=http_session,
        )
        return artifacts, _parse_saved_list(
            list_url,
            artifacts=artifacts,
            collection_source="http",
        )
    except (ParseError, ScrapeError) as exc:
        _logger.warning(
            "Falling back to saved-list browser collection error_type=%s",
            type(exc).__name__,
            extra=url_log_fields(list_url),
        )
        artifacts = collect_browser_artifacts(
            list_url,
            headless=headless,
            timeout_ms=timeout_ms,
            settle_time_ms=settle_time_ms,
            browser_session=browser_session,
        )
        return artifacts, _parse_saved_list(
            list_url,
            artifacts=artifacts,
            collection_source="browser",
            used_browser_fallback=True,
        )


def _parse_saved_list(
    list_url: str,
    *,
    artifacts: BrowserArtifacts,
    collection_source: SavedListCollectionSource,
    used_browser_fallback: bool = False,
) -> SavedList:
    parsed = parse_saved_list_artifacts(
        list_url,
        resolved_url=artifacts.resolved_url,
        runtime_state=artifacts.runtime_state,
        script_texts=artifacts.script_texts,
        html=artifacts.html,
    )
    if parsed.diagnostics is not None:
        parsed.diagnostics.collection_source = collection_source
        parsed.diagnostics.used_browser_fallback = used_browser_fallback
        parsed.diagnostics.preload_expanded = artifacts.preload_expanded
        parsed.diagnostics.preload_paginated = artifacts.preload_paginated
    return parsed


def collect_http_artifacts(
    list_url: str,
    *,
    timeout_ms: int,
    http_session: HttpSessionConfig | None = None,
) -> BrowserArtifacts:
    """Load a page over HTTP and collect runtime artifacts from preload responses."""
    curl_requests = _import_curl_requests()
    timeout_seconds = max(timeout_ms / 1_000, 1.0)
    session_kwargs: dict[str, Any] = {
        "impersonate": _HTTP_IMPERSONATE,
        "allow_redirects": True,
        "default_headers": True,
        "headers": {"Accept-Language": "en-US,en;q=0.9"},
        "timeout": timeout_seconds,
    }
    cookie_jar = _load_http_cookie_jar(http_session)
    if cookie_jar is not None:
        session_kwargs["cookies"] = cookie_jar
    if http_session is not None and http_session.proxy is not None:
        session_kwargs["proxy"] = http_session.proxy

    preload_expanded = False
    preload_paginated = False
    try:
        with curl_requests.Session(**session_kwargs) as session:
            response = _get_with_retry(session, list_url, timeout=timeout_seconds)
            _raise_for_status(response)
            resolved_url = _normalize_response_url(getattr(response, "url", None))
            if resolved_url is not None and urlsplit(resolved_url).hostname == "maps.app.goo.gl":
                canonical_url = _resolve_saved_list_short_link(session, resolved_url)
                if canonical_url is not None:
                    response = _get_with_retry(session, canonical_url, timeout=timeout_seconds)
                    _raise_for_status(response)
                    resolved_url = _normalize_response_url(getattr(response, "url", None))
            page_html = _response_text(response)
            script_texts = _extract_script_texts_from_html(page_html)
            preload_url = _extract_preloaded_fetch_url(
                page_html,
                base_url=resolved_url or list_url,
                preferred_path_markers=("entitylist/getlist",),
            )
            if preload_url is not None:
                try:
                    preload_response = _get_with_retry(
                        session,
                        preload_url,
                        timeout=timeout_seconds,
                        referer=resolved_url or list_url,
                    )
                    _raise_for_status(preload_response)
                except Exception as exc:
                    _logger.warning(
                        "Saved-list preload fetch failed error_type=%s",
                        type(exc).__name__,
                        extra=url_log_fields(list_url, resolved_url),
                    )
                else:
                    preload_text = _response_text(preload_response)
                    if preload_text.strip():
                        preload_text, preload_expanded, preload_paginated = (
                            _complete_entitylist_preload(
                                session,
                                preload_url=preload_url,
                                preload_text=preload_text,
                                referer=resolved_url or list_url,
                                timeout=timeout_seconds,
                            )
                        )
                        script_texts.append(preload_text)
    except Exception as exc:
        _logger.warning(
            "Saved-list HTTP collection failed error_type=%s",
            type(exc).__name__,
            extra=url_log_fields(list_url),
        )
        raise ScrapeError(f"Failed to collect HTTP artifacts: {exc}") from exc
    finally:
        _save_http_cookie_jar(http_session, cookie_jar)

    _logger.debug(
        "Collected saved-list HTTP artifacts", extra=url_log_fields(list_url, resolved_url)
    )
    return BrowserArtifacts(
        resolved_url=resolved_url,
        runtime_state=None,
        script_texts=script_texts,
        html=page_html,
        preload_expanded=preload_expanded,
        preload_paginated=preload_paginated,
    )


def collect_browser_artifacts(
    list_url: str,
    *,
    headless: bool,
    timeout_ms: int,
    settle_time_ms: int,
    browser_session: BrowserSessionConfig | None = None,
) -> BrowserArtifacts:
    """Load a page in CloakBrowser and collect runtime artifacts."""
    context = _launch_browser_context(
        headless=headless,
        browser_session=browser_session,
    )
    try:
        page = context.new_page()
        page.goto(list_url, wait_until="domcontentloaded", timeout=timeout_ms)
        _handle_google_consent(page, timeout_ms=timeout_ms)
        try:
            page.wait_for_load_state("load", timeout=min(timeout_ms, 10_000))
        except Exception:
            pass
        _handle_google_consent(page, timeout_ms=timeout_ms)
        page.wait_for_timeout(settle_time_ms)

        resolved_url = _read_resolved_url(page)
        runtime_state = _read_runtime_state(page, timeout_ms=timeout_ms)
        script_texts = _read_script_texts(page)
        html = page.content()
    except Exception as exc:  # pragma: no cover - browser error path
        raise ScrapeError(f"Failed to collect browser artifacts: {exc}") from exc
    finally:
        context.close()

    _logger.debug(
        "Collected saved-list browser artifacts", extra=url_log_fields(list_url, resolved_url)
    )
    return BrowserArtifacts(
        resolved_url=resolved_url,
        runtime_state=runtime_state,
        script_texts=script_texts,
        html=html,
    )


def _launch_browser_context(
    *,
    headless: bool,
    browser_session: BrowserSessionConfig | None,
) -> Any:
    try:
        from cloakbrowser import (  # type: ignore[import-untyped]
            launch_context,
            launch_persistent_context,
        )
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise ScrapeError("CloakBrowser is not installed. Run `uv sync`.") from exc

    session = browser_session or BrowserSessionConfig()
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "humanize": session.human_mouse,
        "locale": "en-US",
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }
    window_size = resolve_browser_window_size(
        session.window_size,
        profile_dir=session.profile_dir,
    )
    if window_size is not None:
        width, height = window_size
        launch_kwargs["args"] = [f"--window-size={width},{height}"]
        launch_kwargs["viewport"] = {"width": width, "height": height}
    if session.proxy is not None:
        launch_kwargs["proxy"] = session.proxy
    if session.profile_dir is None:
        return launch_context(**launch_kwargs)

    session.profile_dir.mkdir(parents=True, exist_ok=True)
    return launch_persistent_context(session.profile_dir, **launch_kwargs)


def _read_resolved_url(page: Any) -> str | None:
    value = getattr(page, "url", None)
    return _normalize_response_url(value)


def _read_runtime_state(page: Any, *, timeout_ms: int) -> JSONValue | None:
    attempts = max(1, timeout_ms // 1_000)
    for _ in range(attempts):
        runtime_state = page.evaluate(
            "() => globalThis.APP_INITIALIZATION_STATE ?? window.APP_INITIALIZATION_STATE ?? null"
        )
        if isinstance(runtime_state, (list, dict)):
            return runtime_state
        page.wait_for_timeout(1_000)
    return None


def _read_script_texts(page: Any) -> list[str]:
    script_texts = page.evaluate(
        "() => Array.from(document.scripts, (script) => script.textContent || '')"
    )
    if not isinstance(script_texts, list):
        return []
    return [text for text in script_texts if isinstance(text, str)]


def _handle_google_consent(page: Any, *, timeout_ms: int) -> None:
    for _ in range(2):
        if not _has_google_consent_screen(page):
            return
        _logger.debug("Detected Google consent screen")
        if _click_button_in_contexts(page, _REJECT_BUTTON_LABELS):
            _logger.debug("Selected Reject all on Google consent screen")
            _settle_after_consent(page, timeout_ms=timeout_ms)
            continue
        if _click_button_in_contexts(page, _MORE_OPTIONS_BUTTON_LABELS):
            _logger.debug("Opened More options on Google consent screen")
            page.wait_for_timeout(500)
            if _click_button_in_contexts(page, _REJECT_BUTTON_LABELS):
                _logger.debug("Selected Reject all from Google consent options")
                _settle_after_consent(page, timeout_ms=timeout_ms)
                continue
        break

    if not _has_google_consent_screen(page):
        return

    _logger.error("Could not dismiss Google consent screen")
    diagnostics = _capture_consent_diagnostics(page)
    details = ", ".join(str(path) for path in diagnostics)
    raise ScrapeError(
        f"Detected a Google consent screen but could not reject cookies automatically. "
        f"Saved diagnostics: {details}"
    )


def _settle_after_consent(page: Any, *, timeout_ms: int) -> None:
    try:
        page.wait_for_load_state("load", timeout=min(timeout_ms, 10_000))
    except Exception:
        pass
    page.wait_for_timeout(1_000)


def _has_google_consent_screen(page: Any) -> bool:
    url = str(getattr(page, "url", "")).lower()
    if any(marker in url for marker in _CONSENT_URL_MARKERS):
        return True

    for context in _iter_contexts(page):
        body_text = _read_body_text(context).lower()
        if any(marker in body_text for marker in _CONSENT_TEXT_MARKERS):
            return True
    return False


def _click_button_in_contexts(page: Any, labels: tuple[str, ...]) -> bool:
    pattern = _button_label_pattern(labels)
    for context in _iter_contexts(page):
        if _click_button(context, pattern=pattern, labels=labels):
            return True
    return False


def _click_button(context: Any, *, pattern: re.Pattern[str], labels: tuple[str, ...]) -> bool:
    try:
        context.get_by_role("button", name=pattern).first.click(timeout=1_500)
    except Exception:
        return _click_button_with_dom(context, labels)
    return True


def _button_label_pattern(labels: tuple[str, ...]) -> re.Pattern[str]:
    escaped_labels = [re.escape(label) for label in labels]
    return re.compile(rf"^\s*(?:{'|'.join(escaped_labels)})\s*$", re.IGNORECASE)


def _iter_contexts(page: Any) -> list[Any]:
    contexts = [page]
    for frame in getattr(page, "frames", []):
        if frame is page:
            continue
        contexts.append(frame)
    return contexts


def _read_body_text(context: Any) -> str:
    try:
        value = context.evaluate("() => document.body?.innerText ?? ''")
    except Exception:
        return ""
    if not isinstance(value, str):
        return ""
    return value


def _click_button_with_dom(context: Any, labels: tuple[str, ...]) -> bool:
    script = """
    (labels) => {
      const normalize = (value) => value.trim().replace(/\\s+/g, " ").toLowerCase();
      const expected = new Set(labels.map(normalize));
      const selector = [
        "button",
        '[role="button"]',
        'input[type="button"]',
        'input[type="submit"]'
      ].join(", ");
      const candidates = Array.from(document.querySelectorAll(selector));

      for (const element of candidates) {
        const text = normalize(element.innerText || element.textContent || element.value || "");
        if (!expected.has(text)) {
          continue;
        }
        element.click();
        return true;
      }
      return false;
    }
    """
    try:
        clicked = context.evaluate(script, list(labels))
    except Exception:
        return False
    return clicked is True


def _capture_consent_diagnostics(page: Any) -> list[Path]:
    diagnostics_dir = Path(".context/diagnostics")
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    saved_paths: list[Path] = []
    html_path = diagnostics_dir / f"google-consent-{timestamp}.html"
    text_path = diagnostics_dir / f"google-consent-{timestamp}.txt"
    screenshot_path = diagnostics_dir / f"google-consent-{timestamp}.png"

    try:
        html_path.write_text(page.content(), encoding="utf-8")
        saved_paths.append(html_path)
    except Exception:
        pass

    try:
        text_path.write_text(_read_body_text(page), encoding="utf-8")
        saved_paths.append(text_path)
    except Exception:
        pass

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        saved_paths.append(screenshot_path)
    except Exception:
        pass

    return saved_paths


def _import_curl_requests() -> Any:
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise ScrapeError("curl_cffi is not installed. Run `uv sync`.") from exc
    return curl_requests


def _load_http_cookie_jar(
    http_session: HttpSessionConfig | None,
) -> MozillaCookieJar | None:
    if http_session is None or http_session.cookie_jar_path is None:
        return None
    http_session.cookie_jar_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_jar = MozillaCookieJar(str(http_session.cookie_jar_path))
    if http_session.cookie_jar_path.exists():
        try:
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
        except LoadError as exc:
            _logger.warning("Failed to load HTTP cookie jar error_type=%s", type(exc).__name__)
            raise ScrapeError(
                f"Failed to load HTTP cookie jar: {http_session.cookie_jar_path}"
            ) from exc
    return cookie_jar


def _save_http_cookie_jar(
    http_session: HttpSessionConfig | None,
    cookie_jar: MozillaCookieJar | None,
) -> None:
    if (
        http_session is None
        or http_session.cookie_jar_path is None
        or cookie_jar is None
    ):
        return
    cookie_jar.save(ignore_discard=True, ignore_expires=True)


def _resolve_saved_list_short_link(session: Any, url: str) -> str | None:
    # Some short links return a JavaScript interstitial to browser-identified GET requests.
    try:
        response = session.head(url, headers={"User-Agent": "gmaps-scraper"})
        _raise_for_status(response)
    except Exception:
        return None
    target = _normalize_response_url(getattr(response, "url", None))
    if target is not None:
        parts = urlsplit(target)
        if (
            parts.scheme == "https"
            and parts.hostname in {"www.google.com", "maps.google.com"}
            and extract_list_id_from_text(target) is not None
        ):
            return target
    return None


def _extract_preloaded_fetch_url(
    page_html: str,
    *,
    base_url: str,
    preferred_path_markers: tuple[str, ...] = (),
) -> str | None:
    candidates: list[str] = []
    for match in _LINK_TAG_PATTERN.finditer(page_html):
        attributes = _extract_html_attributes(match.group(0))
        if attributes.get("as", "").strip().lower() != "fetch":
            continue
        href = html.unescape(attributes.get("href", ""))
        if not href.strip() or "/maps/preview/" not in href:
            continue
        candidates.append(urljoin(base_url, href))
    if not candidates:
        return None
    for marker in preferred_path_markers:
        for candidate in candidates:
            if marker in candidate:
                return candidate
    return candidates[0]


def _complete_entitylist_preload(
    session: Any,
    *,
    preload_url: str,
    preload_text: str,
    referer: str,
    timeout: float,
) -> tuple[str, bool, bool]:
    first_page = _parse_entitylist_page(preload_text)
    if first_page is None or len(first_page.rows) >= first_page.total_rows:
        return preload_text, False, False

    deadline = time.monotonic() + timeout
    attempted_urls: set[str] = set()
    paginated_text = _paginate_entitylist_preload(
        session,
        preload_url=preload_url,
        first_page=first_page,
        referer=referer,
        attempted_urls=attempted_urls,
        deadline=deadline,
    )
    if paginated_text is not None:
        return paginated_text, False, True

    expanded = _expand_entitylist_preload(
        session,
        preload_url=preload_url,
        first_page=first_page,
        referer=referer,
        deadline=deadline,
    )
    if expanded is None:
        return preload_text, False, False

    expanded_url, expanded_text, expanded_page = expanded
    paginated_text = _paginate_entitylist_preload(
        session,
        preload_url=expanded_url,
        first_page=expanded_page,
        referer=referer,
        attempted_urls=attempted_urls,
        deadline=deadline,
    )
    return paginated_text or expanded_text, True, paginated_text is not None


def _paginate_entitylist_preload(
    session: Any,
    *,
    preload_url: str,
    first_page: _EntitylistPage,
    referer: str,
    attempted_urls: set[str],
    deadline: float,
) -> str | None:
    if first_page.cursor is None or len(first_page.rows) >= first_page.total_rows:
        return None
    rows = list(first_page.rows)
    seen_rows = {_entitylist_row_key(row) for row in rows}
    cursor = first_page.cursor
    while cursor is not None and len(rows) < first_page.total_rows:
        next_url = _entitylist_continuation_url(preload_url, cursor)
        if next_url is None or next_url in attempted_urls:
            break
        attempted_urls.add(next_url)

        text = _fetch_entitylist_text(session, url=next_url, referer=referer, deadline=deadline)
        page = _parse_entitylist_page(text) if text is not None else None
        if (
            page is None
            or page.list_id != first_page.list_id
            or page.total_rows != first_page.total_rows
            or not page.rows
            or len(rows) + len(page.rows) > first_page.total_rows
        ):
            break

        page_keys = {_entitylist_row_key(row) for row in page.rows}
        if seen_rows & page_keys:
            break
        rows.extend(page.rows)
        seen_rows.update(page_keys)
        cursor = page.cursor

    if len(rows) == len(first_page.rows):
        return None
    # The parser selects one artifact. Present the combined rows under the original header.
    first_page.header[8] = rows
    first_page.payload[1] = cursor or ""
    return ")]}'\n" + json.dumps(first_page.payload, ensure_ascii=False, separators=(",", ":"))


def _expand_entitylist_preload(
    session: Any,
    *,
    preload_url: str,
    first_page: _EntitylistPage,
    referer: str,
    deadline: float,
) -> tuple[str, str, _EntitylistPage] | None:
    page_size = min(first_page.total_rows, _ENTITYLIST_PAGE_SIZE_LIMIT)
    if page_size <= len(first_page.rows):
        return None
    expanded_url = _replace_entitylist_page_size(preload_url, page_size)
    if expanded_url is None or expanded_url == preload_url:
        return None
    text = _fetch_entitylist_text(session, url=expanded_url, referer=referer, deadline=deadline)
    if text is None:
        return None
    expanded = _parse_entitylist_page(text)
    if (
        expanded is None
        or expanded.list_id != first_page.list_id
        or expanded.total_rows != first_page.total_rows
        or len(expanded.rows) <= len(first_page.rows)
        or not {_entitylist_row_key(row) for row in first_page.rows}.issubset(
            _entitylist_row_key(row) for row in expanded.rows
        )
    ):
        return None
    return expanded_url, text, expanded


def _fetch_entitylist_text(
    session: Any, *, url: str, referer: str, deadline: float
) -> str | None:
    try:
        response = _get_with_retry(
            session, url, timeout=deadline - time.monotonic(), referer=referer
        )
        _raise_for_status(response)
    except Exception:
        return None
    return _response_text(response)


def _entitylist_continuation_url(preload_url: str, cursor: str) -> str | None:
    parts = urlsplit(preload_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    pb_indexes = [index for index, (name, _) in enumerate(query) if name == "pb"]
    if len(pb_indexes) != 1:
        return None
    index = pb_indexes[0]
    _, pb = query[index]
    if "!5B" in pb:
        return None
    # Maps sends the response cursor as field 5B in URL-safe base64.
    cursor = cursor.translate(_ENTITYLIST_CURSOR_TRANSLATION).rstrip("=")
    if (size := _ENTITYLIST_CONTINUATION_ANCHOR.search(pb)) is None:
        return None
    page_size = min(int(size[2]), _ENTITYLIST_PAGE_SIZE_LIMIT)
    query[index] = ("pb", f"{pb[:size.start()]}{size[1]}{page_size}!5B{cursor}{pb[size.end():]}")
    return urlunsplit(parts._replace(query=urlencode(query)))


def _parse_entitylist_page(preload_text: str) -> _EntitylistPage | None:
    normalized = preload_text.strip()
    if normalized.startswith(")]}'"):
        normalized = normalized[4:].lstrip()
    try:
        payload = cast(JSONValue, json.loads(normalized))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None
    header = payload[0]
    counts = _entitylist_row_counts(header)
    if counts is None or not isinstance(header, list):
        return None
    identity = header[0]
    rows = header[8]
    if (
        not isinstance(identity, list)
        or not identity
        or not isinstance(identity[0], str)
        or not identity[0]
        or not isinstance(rows, list)
    ):
        return None
    cursor = payload[1] if len(payload) > 1 else None
    if not isinstance(cursor, str) or _ENTITYLIST_CURSOR_PATTERN.fullmatch(cursor) is None:
        cursor = None
    return _EntitylistPage(payload, header, rows, identity[0], counts[1], cursor)


def _entitylist_row_key(row: JSONValue) -> str:
    # The saved item identity is independent of display strings and user notes on the row.
    if isinstance(row, list) and len(row) > 8 and isinstance(row[8], list) and row[8]:
        row = row[8]
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _replace_entitylist_page_size(preload_url: str, page_size: int) -> str | None:
    if page_size < 1:
        return None

    def _replacement(match: re.Match[str]) -> str:
        prefix = match.group(1)
        return f"{prefix}{page_size}"

    expanded_url, replacement_count = _ENTITYLIST_PAGE_SIZE_PATTERN.subn(
        _replacement,
        preload_url,
        count=1,
    )
    if replacement_count == 0:
        return None
    return expanded_url


def _extract_html_attributes(tag_html: str) -> dict[str, str]:
    return {
        name.lower(): value
        for name, _quote, value in _HTML_ATTRIBUTE_PATTERN.findall(tag_html)
    }


def _normalize_response_url(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized


def _response_text(response: Any) -> str:
    value = getattr(response, "text", "")
    if isinstance(value, str):
        return value
    return str(value)


def _get_with_retry(session: Any, url: str, *, timeout: float, **kwargs: Any) -> Any:
    """GET with bounded retries on transient transport errors, 429, and 5xx.

    The timeout in seconds includes all attempts and retry delays for this GET.
    The last response or exception is passed through unchanged.
    """
    if timeout < _HTTP_GET_MIN_TIMEOUT_SECONDS:
        raise TimeoutError("Insufficient time to attempt HTTP GET.")
    deadline = time.monotonic() + timeout
    remaining = timeout
    attempt = 1
    while True:
        try:
            response = session.get(url, timeout=remaining, **kwargs)
        except OSError as exc:
            if attempt == _HTTP_GET_MAX_ATTEMPTS or not _is_transient_transport_error(exc):
                raise
            remaining = _wait_for_http_retry(
                deadline,
                _http_get_backoff_seconds(attempt),
                url=url,
                next_attempt=attempt + 1,
                reason=type(exc).__name__,
            )
            if remaining < _HTTP_GET_MIN_TIMEOUT_SECONDS:
                raise
        else:
            status_code = getattr(response, "status_code", None)
            if (
                attempt == _HTTP_GET_MAX_ATTEMPTS
                or status_code not in _HTTP_GET_RETRYABLE_STATUS_CODES
            ):
                return response
            retry_after = _parse_retry_after_seconds(response)
            if retry_after is not None and retry_after > _HTTP_GET_RETRY_AFTER_MAX_SECONDS:
                return response
            delay = _http_get_backoff_seconds(attempt) if retry_after is None else retry_after
            remaining = _wait_for_http_retry(
                deadline, delay, url=url, next_attempt=attempt + 1, reason=f"HTTP {status_code}",
            )
            if remaining < _HTTP_GET_MIN_TIMEOUT_SECONDS:
                return response
        attempt += 1


def _wait_for_http_retry(
    deadline: float, delay: float, *, url: str, next_attempt: int, reason: str,
) -> float:
    # curl_cffi converts seconds to integer milliseconds. Zero means no timeout.
    if deadline - time.monotonic() - delay < _HTTP_GET_MIN_TIMEOUT_SECONDS:
        return 0.0
    if delay:
        time.sleep(delay)
    if deadline - time.monotonic() >= _HTTP_GET_MIN_TIMEOUT_SECONDS:
        _logger.warning(
            "Retrying HTTP GET attempt=%d max_attempts=%d delay_seconds=%.3f reason=%s",
            next_attempt,
            _HTTP_GET_MAX_ATTEMPTS,
            delay,
            reason,
            extra=url_log_fields(url),
        )
    return deadline - time.monotonic()


def _is_transient_transport_error(exc: OSError) -> bool:
    # curl_cffi errors subclass OSError; these ones fail the same way on every
    # attempt (bad URL, TLS verification, redirect loop, proxy config).
    if isinstance(exc, ValueError):
        return False
    try:
        from curl_cffi import CurlECode
        from curl_cffi.requests import exceptions as curl_exceptions
    except ImportError:  # pragma: no cover - dependency error path
        return True
    if isinstance(exc, curl_exceptions.ProxyError):
        # A CONNECT rejection, such as 407, can carry the same receive error code.
        return exc.code == CurlECode.RECV_ERROR and "proxy connect aborted" in str(exc).lower()
    permanent = (
        curl_exceptions.CertificateVerifyError,
        curl_exceptions.TooManyRedirects,
        curl_exceptions.ImpersonateError,
        curl_exceptions.SessionClosed,
    )
    return not isinstance(exc, permanent)


def _http_get_backoff_seconds(attempt: int) -> float:
    ceiling = _HTTP_GET_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    return random.uniform(ceiling / 2, ceiling)


def _parse_retry_after_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)


def _raise_for_status(response: Any) -> None:
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()


def _normalize_collection_mode(collection_mode: CollectionMode) -> CollectionMode:
    return collection_mode

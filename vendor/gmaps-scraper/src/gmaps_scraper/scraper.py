"""Google Maps saved-list scraper with HTTP and browser collectors."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from typing import Any, Literal, Required, TypedDict
from urllib.parse import urljoin

from gmaps_scraper.browser_humanization import (
    BrowserWindowSize,
    resolve_browser_window_size,
)
from gmaps_scraper.models import SavedList
from gmaps_scraper.parser import JSONValue, ParseError, parse_saved_list_artifacts

type CollectionMode = Literal["auto", "curl", "browser"]

_CONSENT_URL_MARKERS = ("consent.google", "consent.youtube")
_HTTP_IMPERSONATE = "chrome"
DEFAULT_COLLECTION_MODE: CollectionMode = "auto"
_LINK_TAG_PATTERN = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HTML_ATTRIBUTE_PATTERN = re.compile(
    r"""\b([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(['"])(.*?)\2""",
    re.DOTALL,
)
_SCRIPT_TEXT_PATTERN = re.compile(
    r"<script\b[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_ENTITYLIST_PAGE_SIZE_PATTERN = re.compile(r"(%214i|!4i)(\d+)")
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
    """Artifacts collected from a browser session."""

    resolved_url: str | None
    runtime_state: JSONValue | None
    script_texts: list[str]
    html: str


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
        )
    except (ParseError, ScrapeError):
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
        )


def _parse_saved_list(
    list_url: str,
    *,
    artifacts: BrowserArtifacts,
) -> SavedList:
    return parse_saved_list_artifacts(
        list_url,
        resolved_url=artifacts.resolved_url,
        runtime_state=artifacts.runtime_state,
        script_texts=artifacts.script_texts,
        html=artifacts.html,
    )


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
        "timeout": timeout_seconds,
    }
    cookie_jar = _load_http_cookie_jar(http_session)
    if cookie_jar is not None:
        session_kwargs["cookies"] = cookie_jar
    if http_session is not None and http_session.proxy is not None:
        session_kwargs["proxy"] = http_session.proxy

    try:
        with curl_requests.Session(**session_kwargs) as session:
            response = session.get(list_url)
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
                    preload_response = session.get(
                        preload_url,
                        referer=resolved_url or list_url,
                    )
                    _raise_for_status(preload_response)
                except Exception:
                    pass
                else:
                    preload_text = _response_text(preload_response)
                    if preload_text.strip():
                        expanded_preload_text = _expand_entitylist_preload_text(
                            session,
                            preload_url=preload_url,
                            preload_text=preload_text,
                            referer=resolved_url or list_url,
                        )
                        if expanded_preload_text is not None:
                            preload_text = expanded_preload_text
                        script_texts.append(preload_text)
    except Exception as exc:  # pragma: no cover - network error path
        raise ScrapeError(f"Failed to collect HTTP artifacts: {exc}") from exc
    finally:
        _save_http_cookie_jar(http_session, cookie_jar)

    return BrowserArtifacts(
        resolved_url=resolved_url,
        runtime_state=None,
        script_texts=script_texts,
        html=page_html,
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
        if _click_button_in_contexts(page, _REJECT_BUTTON_LABELS):
            _settle_after_consent(page, timeout_ms=timeout_ms)
            continue
        if _click_button_in_contexts(page, _MORE_OPTIONS_BUTTON_LABELS):
            page.wait_for_timeout(500)
            if _click_button_in_contexts(page, _REJECT_BUTTON_LABELS):
                _settle_after_consent(page, timeout_ms=timeout_ms)
                continue
        break

    if not _has_google_consent_screen(page):
        return

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


def _extract_script_texts_from_html(page_html: str) -> list[str]:
    return [
        match.group(1)
        for match in _SCRIPT_TEXT_PATTERN.finditer(page_html)
        if match.group(1).strip()
    ]


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


def _expand_entitylist_preload_text(
    session: Any,
    *,
    preload_url: str,
    preload_text: str,
    referer: str,
) -> str | None:
    response_counts = _extract_entitylist_response_counts(preload_text)
    if response_counts is None:
        return None

    loaded_rows, total_rows = response_counts
    if total_rows <= loaded_rows:
        return None

    expanded_preload_url = _replace_entitylist_page_size(preload_url, total_rows)
    if expanded_preload_url is None or expanded_preload_url == preload_url:
        return None

    try:
        expanded_response = session.get(
            expanded_preload_url,
            referer=referer,
        )
        _raise_for_status(expanded_response)
    except Exception:
        return None

    expanded_text = _response_text(expanded_response)
    if not expanded_text.strip():
        return None
    return expanded_text


def _extract_entitylist_response_counts(preload_text: str) -> tuple[int, int] | None:
    candidate = _extract_entitylist_payload(preload_text)
    if candidate is None:
        return None

    rows = candidate[8]
    total = candidate[12]
    if not isinstance(rows, list) or not isinstance(total, int):
        return None
    return len(rows), total


def _extract_entitylist_payload(preload_text: str) -> list[Any] | None:
    normalized = preload_text.strip()
    if normalized.startswith(")]}'"):
        normalized = normalized[4:].lstrip()
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None
    candidate = payload[0]
    if not isinstance(candidate, list) or len(candidate) <= 12:
        return None
    return candidate


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


def _raise_for_status(response: Any) -> None:
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()


def _normalize_collection_mode(collection_mode: CollectionMode) -> CollectionMode:
    return collection_mode

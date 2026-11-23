from __future__ import annotations

import json
import re
import tempfile
import unittest
from hashlib import sha256
from http.cookiejar import Cookie, LoadError
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlsplit

from gmaps_scraper.models import SavedList, SavedListExtractionDiagnostics
from gmaps_scraper.parser import ParseError
from gmaps_scraper.scraper import (
    BrowserArtifacts,
    BrowserSessionConfig,
    HttpSessionConfig,
    ScrapeError,
    _handle_google_consent,
    _has_google_consent_screen,
    _launch_browser_context,
    _read_resolved_url,
    collect_http_artifacts,
    collect_saved_list_result,
    scrape_saved_lists,
)


class _FakeLocator:
    def __init__(self, context: _FakeContext, pattern: re.Pattern[str]) -> None:
        self._context = context
        self._pattern = pattern
        self.first = self

    def click(self, timeout: int) -> None:
        del timeout
        if not self._context.allow_role_click:
            raise RuntimeError("Role-based click disabled")
        for label in self._context.buttons:
            if self._pattern.search(label):
                self._context.clicked.append(label)
                self._context.dismiss_consent()
                return
        raise RuntimeError("No matching button")


class _FakeContext:
    def __init__(
        self,
        *,
        text: str,
        buttons: list[str] | None = None,
        url: str = "",
        frames: list[_FakeContext] | None = None,
        allow_role_click: bool = True,
        dismiss_on_click: bool = True,
    ) -> None:
        self._text = text
        self.buttons = buttons or []
        self.url = url
        self.frames = frames or []
        self.allow_role_click = allow_role_click
        self.dismiss_on_click = dismiss_on_click
        self.clicked: list[str] = []
        self.timeouts: list[int] = []

    def evaluate(self, script: str, argument: Any = None) -> Any:
        if "querySelectorAll" in script and isinstance(argument, list):
            normalized_labels = {
                label.strip().lower()
                for label in argument
                if isinstance(label, str)
            }
            for label in self.buttons:
                if label.strip().lower() in normalized_labels:
                    self.clicked.append(label)
                    self.dismiss_consent()
                    return True
            return False
        if "innerText" in script:
            return self._text
        return None

    def get_by_role(self, role: str, name: re.Pattern[str]) -> _FakeLocator:
        if role != "button":
            raise RuntimeError("Unexpected role")
        return _FakeLocator(self, name)

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.timeouts.append(milliseconds)

    def wait_for_load_state(self, state: str, timeout: int) -> None:
        del state, timeout

    def content(self) -> str:
        return f"<html><body>{self._text}</body></html>"

    def screenshot(self, *, path: str, full_page: bool) -> None:
        del path, full_page

    def dismiss_consent(self) -> None:
        if not self.dismiss_on_click:
            return
        self._text = ""
        self.buttons = []
        self.url = ""


class _FakeHttpResponse:
    def __init__(self, *, text: str, url: str) -> None:
        self.text = text
        self.url = url

    def raise_for_status(self) -> None:
        return None


class _FakeHttpSession:
    def __init__(self, responses: list[_FakeHttpResponse], **kwargs: Any) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.kwargs = kwargs

    def __enter__(self) -> _FakeHttpSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def get(self, url: str, **kwargs: Any) -> _FakeHttpResponse:
        self.calls.append((url, kwargs))
        cookie_jar = self.kwargs.get("cookies")
        if cookie_jar is not None:
            cookie_jar.set_cookie(
                Cookie(
                    version=0,
                    name="sid",
                    value="123",
                    port=None,
                    port_specified=False,
                    domain="www.google.com",
                    domain_specified=True,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=False,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
        return self._responses.pop(0)


class _FakeCurlRequests:
    def __init__(self, responses: list[_FakeHttpResponse]) -> None:
        self._responses = responses
        self.sessions: list[_FakeHttpSession] = []

    def Session(self, **kwargs: Any) -> _FakeHttpSession:
        session = _FakeHttpSession(self._responses, **kwargs)
        self.sessions.append(session)
        return session


class BatchSavedListTests(unittest.TestCase):
    def test_parallel_batch_isolates_worker_sessions_and_preserves_order(self) -> None:
        urls = [f"https://maps.app.goo.gl/example-{index}" for index in range(5)]
        browser_session = BrowserSessionConfig(
            profile_dir=Path("profile"),
            proxy="http://proxy.example:8080",
            window_size=None,
            human_mouse=True,
        )
        http_session = HttpSessionConfig(
            cookie_jar_path=Path("cookies.txt"), proxy="http://proxy.example:8080"
        )
        two_workers_started = Barrier(2, timeout=3)

        def scrape_one(url: str, **_kwargs: object) -> SavedList:
            if url in urls[:2]:
                two_workers_started.wait()
            if url == urls[2]:
                raise ScrapeError("unavailable")
            return SavedList(url, None, None, url, None, [])

        with patch("gmaps_scraper.scraper.scrape_saved_list", side_effect=scrape_one) as scrape:
            results = scrape_saved_lists(
                urls,
                browser_session=browser_session,
                http_session=http_session,
                max_concurrency=2,
            )

        self.assertEqual([result.source_url for result in results], urls)
        self.assertEqual(
            [result.error for result in results], [None, None, "unavailable", None, None]
        )
        self.assertEqual(scrape.call_count, len(urls))
        scrape_kwargs = {args.args[0]: args.kwargs for args in scrape.call_args_list}
        for index, url in enumerate(urls):
            worker = index % 2 + 1
            self.assertEqual(
                scrape_kwargs[url]["browser_session"],
                BrowserSessionConfig(
                    profile_dir=Path("profile") / f"worker-{worker}",
                    proxy=browser_session.proxy,
                    window_size=None,
                    human_mouse=True,
                ),
            )
            self.assertEqual(
                scrape_kwargs[url]["http_session"],
                HttpSessionConfig(
                    cookie_jar_path=Path(f"cookies.worker-{worker}.txt"),
                    proxy=http_session.proxy,
                ),
            )

    def test_batch_retries_each_url_with_linear_backoff(self) -> None:
        urls = ["https://maps.app.goo.gl/example-a", "https://maps.app.goo.gl/example-b"]
        saved_list = SavedList(urls[0], None, None, "Coffee stops", None, [])
        with (
            patch(
                "gmaps_scraper.scraper.scrape_saved_list",
                side_effect=[
                    ScrapeError("temporarily unavailable"),
                    saved_list,
                    ScrapeError("connection reset"),
                    ScrapeError("timed out"),
                    ParseError("payload unavailable"),
                ],
            ) as scrape_one,
            patch("gmaps_scraper.scraper.time.sleep") as sleep,
        ):
            results = scrape_saved_lists(urls, max_retries=2, retry_backoff_ms=500)

        self.assertEqual(
            [args.args[0] for args in scrape_one.call_args_list],
            [urls[0], urls[0], urls[1], urls[1], urls[1]],
        )
        self.assertEqual(sleep.call_args_list, [call(0.5), call(0.5), call(1.0)])
        self.assertEqual(
            [result.to_dict() for result in results],
            [
                {"source_url": urls[0], "saved_list": saved_list.to_dict(), "attempts": 2},
                {"source_url": urls[1], "error": "payload unavailable", "attempts": 3},
            ],
        )

    def test_batch_continues_after_browser_fallback_lifecycle_errors(self) -> None:
        urls = [f"https://maps.app.goo.gl/example-{index}" for index in range(3)]
        first = SavedList(urls[0], None, None, "Coffee stops", None, [])
        last = SavedList(urls[2], None, None, "Dinner spots", None, [])
        for failure_stage in ("launch", "close"):
            with self.subTest(failure_stage=failure_stage):
                error = OSError(f"browser {failure_stage} failed")
                context = Mock()
                page = context.new_page.return_value
                page.url = urls[1]
                page.frames = []
                page.evaluate.return_value = []
                page.content.return_value = "<html></html>"
                if failure_stage == "close":
                    context.close.side_effect = error

                with (
                    patch(
                        "gmaps_scraper.scraper.collect_http_artifacts",
                        side_effect=[
                            BrowserArtifacts(urls[0], None, [], ""),
                            ScrapeError("HTTP unavailable"),
                            BrowserArtifacts(urls[2], None, [], ""),
                        ],
                    ) as collect_http,
                    patch(
                        "gmaps_scraper.scraper.parse_saved_list_artifacts",
                        side_effect=[first, last],
                    ),
                    patch(
                        "gmaps_scraper.scraper._launch_browser_context",
                        side_effect=error if failure_stage == "launch" else None,
                        return_value=context,
                    ) as launch_browser,
                ):
                    results = scrape_saved_lists(urls)

                self.assertEqual([args.args[0] for args in collect_http.call_args_list], urls)
                launch_browser.assert_called_once()
                if failure_stage == "close":
                    context.close.assert_called_once_with()
                    page.content.assert_called_once_with()
                self.assertEqual(
                    [result.to_dict() for result in results],
                    [
                        {"source_url": urls[0], "saved_list": first.to_dict()},
                        {"source_url": urls[1], "error": str(error)},
                        {"source_url": urls[2], "saved_list": last.to_dict()},
                    ],
                )

    def test_batch_continues_after_collection_and_parsing_errors(self) -> None:
        urls = [f"https://maps.app.goo.gl/example-{index}" for index in range(3)]
        saved_list = SavedList(urls[2], None, None, "Sample Coffee Stops", None, [])
        browser_session = BrowserSessionConfig(profile_dir=Path("profile"))
        http_session = HttpSessionConfig(cookie_jar_path=Path("cookies.txt"))

        with (
            patch(
                "gmaps_scraper.scraper.scrape_saved_list",
                side_effect=[ScrapeError("unavailable"), ParseError("no payload"), saved_list],
            ) as scrape_one,
            patch("gmaps_scraper.scraper.time.sleep") as sleep,
        ):
            results = scrape_saved_lists(
                urls,
                headless=False,
                timeout_ms=12_000,
                settle_time_ms=1_000,
                collection_mode="browser",
                browser_session=browser_session,
                http_session=http_session,
                stagger_ms=250,
            )

        self.assertEqual(
            scrape_one.call_args_list,
            [
                call(
                    url,
                    headless=False,
                    timeout_ms=12_000,
                    settle_time_ms=1_000,
                    collection_mode="browser",
                    browser_session=browser_session,
                    http_session=http_session,
                )
                for url in urls
            ],
        )
        self.assertEqual(sleep.call_args_list, [call(0.25), call(0.25)])
        self.assertEqual(
            [result.to_dict() for result in results],
            [
                {"source_url": urls[0], "error": "unavailable"},
                {"source_url": urls[1], "error": "no payload"},
                {"source_url": urls[2], "saved_list": saved_list.to_dict()},
            ],
        )


class ScraperConsentTests(unittest.TestCase):
    def test_launches_ephemeral_context_without_profile_dir(self) -> None:
        launched: list[dict[str, Any]] = []
        expected_context = object()

        def fake_launch_context(**kwargs: Any) -> object:
            launched.append(kwargs)
            return expected_context

        fake_module = SimpleNamespace(
            launch_context=fake_launch_context,
            launch_persistent_context=lambda *_args, **_kwargs: None,
        )

        with (
            patch.dict("sys.modules", {"cloakbrowser": fake_module}),
            patch(
                "gmaps_scraper.scraper.resolve_browser_window_size",
                return_value=(1366, 768),
            ),
        ):
            context = _launch_browser_context(
                headless=False,
                browser_session=BrowserSessionConfig(),
            )

        self.assertIs(context, expected_context)
        self.assertEqual(
            launched,
            [
                {
                    "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
                    "args": ["--window-size=1366,768"],
                    "headless": False,
                    "humanize": False,
                    "locale": "en-US",
                    "viewport": {"width": 1366, "height": 768},
                }
            ],
        )

    def test_launches_persistent_context_with_profile_dir_and_proxy(self) -> None:
        launched: list[tuple[Path, dict[str, Any]]] = []
        expected_context = object()

        def fake_launch_persistent_context(profile_dir: Path, **kwargs: Any) -> object:
            launched.append((profile_dir, kwargs))
            return expected_context

        fake_module = SimpleNamespace(
            launch_context=lambda **_kwargs: None,
            launch_persistent_context=fake_launch_persistent_context,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir) / "session"
            with (
                patch.dict("sys.modules", {"cloakbrowser": fake_module}),
                patch(
                    "gmaps_scraper.scraper.resolve_browser_window_size",
                    return_value=(1536, 864),
                ),
            ):
                context = _launch_browser_context(
                    headless=True,
                    browser_session=BrowserSessionConfig(
                        profile_dir=profile_dir,
                        proxy="http://proxy.example:8080",
                        human_mouse=True,
                    ),
                )
            self.assertTrue(profile_dir.is_dir())

        self.assertIs(context, expected_context)
        self.assertEqual(
            launched,
            [
                (
                    profile_dir,
                    {
                        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
                        "args": ["--window-size=1536,864"],
                        "headless": True,
                        "humanize": True,
                        "locale": "en-US",
                        "proxy": "http://proxy.example:8080",
                        "viewport": {"width": 1536, "height": 864},
                    },
                )
            ],
        )

    def test_reads_resolved_url_from_page(self) -> None:
        page = _FakeContext(
            text="",
            url="https://www.google.com/maps/@30.5370705,125.4120472,6z/data=!4m3!11m2!2sTESTLISTABC123456789!3e3?entry=ttu",
        )

        self.assertEqual(_read_resolved_url(page), page.url)

    def test_detects_italian_consent_screen(self) -> None:
        page = _FakeContext(
            text="Google\nPrima di continuare su Google\nRifiuta tutto\nAccetta tutto",
            buttons=["Rifiuta tutto", "Accetta tutto"],
        )

        self.assertTrue(_has_google_consent_screen(page))

    def test_rejects_cookies_from_main_page(self) -> None:
        page = _FakeContext(
            text="Google\nPrima di continuare su Google\nRifiuta tutto\nAccetta tutto",
            buttons=["Rifiuta tutto", "Accetta tutto", "Altre opzioni"],
        )

        _handle_google_consent(page, timeout_ms=5_000)

        self.assertEqual(page.clicked, ["Rifiuta tutto"])

    def test_rejects_cookies_after_opening_more_options(self) -> None:
        class _TwoStepConsent(_FakeContext):
            def dismiss_consent(self) -> None:
                if self.clicked[-1] == "More options":
                    self.buttons = ["Reject all", "Accept all"]
                else:
                    super().dismiss_consent()

        page = _TwoStepConsent(
            text="Before you continue to Google",
            buttons=["More options", "Accept all"],
        )
        with self.assertLogs("gmaps_scraper.scraper", level="DEBUG") as logs:
            _handle_google_consent(page, timeout_ms=5_000)

        self.assertEqual(page.clicked, ["More options", "Reject all"])
        self.assertEqual(page.timeouts[0], 500)
        self.assertFalse(_has_google_consent_screen(page))
        self.assertIn("Opened More options", " ".join(logs.output))

    def test_rejects_cookies_from_iframe(self) -> None:
        frame = _FakeContext(
            text="Google\nPrima di continuare su Google\nRifiuta tutto\nAccetta tutto",
            buttons=["Rifiuta tutto", "Accetta tutto"],
        )
        page = _FakeContext(text="", frames=[frame])

        _handle_google_consent(page, timeout_ms=5_000)

        self.assertEqual(frame.clicked, ["Rifiuta tutto"])

    def test_falls_back_to_dom_click_when_role_click_fails(self) -> None:
        page = _FakeContext(
            text="Google\nPrima di continuare su Google\nRifiuta tutto\nAccetta tutto",
            buttons=["Rifiuta tutto", "Accetta tutto"],
            allow_role_click=False,
        )

        _handle_google_consent(page, timeout_ms=5_000)

        self.assertEqual(page.clicked, ["Rifiuta tutto"])

    def test_raises_when_reject_button_is_missing(self) -> None:
        page = _FakeContext(
            text="Google\nPrima di continuare su Google\nAccetta tutto",
            buttons=["Accetta tutto", "Altre opzioni"],
            dismiss_on_click=False,
        )

        with patch("gmaps_scraper.scraper._capture_consent_diagnostics", return_value=[]):
            with self.assertRaises(ScrapeError):
                _handle_google_consent(page, timeout_ms=5_000)


class HttpArtifactTests(unittest.TestCase):
    def test_primary_http_failure_is_wrapped_and_logs_only_safe_context(self) -> None:
        source_url = "https://maps.app.goo.gl/example?token=private-value"
        failure = ConnectionError("connection failed with private-value")
        curl_requests = unittest.mock.MagicMock()
        session = curl_requests.Session.return_value.__enter__.return_value
        session.get.side_effect = failure
        with (
            patch("gmaps_scraper.scraper._import_curl_requests", return_value=curl_requests),
            patch("gmaps_scraper.scraper.time.sleep"),
            self.assertLogs("gmaps_scraper.scraper", level="WARNING") as logs,
            self.assertRaisesRegex(ScrapeError, "Failed to collect HTTP artifacts") as raised,
        ):
            collect_http_artifacts(source_url, timeout_ms=3000)

        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual([call.args for call in session.get.call_args_list], [(source_url,)] * 3)
        self.assertEqual(len(logs.records), 3)
        for record in logs.records:
            self.assertIn("ConnectionError", record.getMessage())
            self.assertNotIn("private-value", record.getMessage())
            self.assertEqual(record.source_url_hash, sha256(source_url.encode()).hexdigest())

    def test_invalid_cookie_jar_stops_collection_with_a_load_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar_path = Path(directory) / "cookies.txt"
            jar_path.write_text("This is not a Netscape cookie jar.", encoding="utf-8")
            with (
                patch("gmaps_scraper.scraper._import_curl_requests") as curl_requests,
                self.assertLogs("gmaps_scraper.scraper", level="WARNING") as logs,
                self.assertRaisesRegex(ScrapeError, "Failed to load HTTP cookie jar") as raised,
            ):
                collect_http_artifacts(
                    "https://maps.app.goo.gl/example",
                    timeout_ms=3000,
                    http_session=HttpSessionConfig(cookie_jar_path=jar_path),
                )

            curl_requests.return_value.Session.assert_not_called()
        self.assertIsInstance(raised.exception.__cause__, LoadError)
        self.assertNotIn(str(jar_path), logs.output[0])

    def test_collect_http_artifacts_fetches_preloaded_payload(self) -> None:
        script_text = '\n  window.APP_INITIALIZATION_STATE=["A &amp; B"];\n'
        fake_requests = _FakeCurlRequests(
            responses=[
                _FakeHttpResponse(
                    text=(
                        "<html><head>"
                        '<link href="/maps/preview/entitylist/getlist?pb=123" '
                        'as="fetch" rel="preload">'
                        '<!-- Example: <script>document.title = "Map";</script> -->'
                        f'<SCRIPT data-label="2 > 1">{script_text}</SCRIPT>'
                        "<script> \n </script>"
                        "</head></html>"
                    ),
                    url="https://www.google.com/maps/@/data=!3m1!4b1",
                ),
                _FakeHttpResponse(
                    text=")]}'\n[[\"payload\"]]",
                    url="https://www.google.com/maps/preview/entitylist/getlist?pb=123",
                ),
            ]
        )

        with patch(
            "gmaps_scraper.scraper._import_curl_requests",
            return_value=fake_requests,
        ):
            artifacts = collect_http_artifacts(
                "https://maps.app.goo.gl/example",
                timeout_ms=15_000,
                http_session=None,
            )

        self.assertEqual(artifacts.resolved_url, "https://www.google.com/maps/@/data=!3m1!4b1")
        self.assertEqual(
            artifacts.script_texts,
            [script_text, ")]}'\n[[\"payload\"]]"],
        )
        self.assertIn("/maps/preview/entitylist/getlist?pb=123", artifacts.html)

        session = fake_requests.sessions[0]
        self.assertEqual(session.kwargs["impersonate"], "chrome")
        self.assertEqual(session.kwargs["headers"], {"Accept-Language": "en-US,en;q=0.9"})
        self.assertEqual(session.calls[0][0], "https://maps.app.goo.gl/example")
        self.assertEqual(
            session.calls[1][0],
            "https://www.google.com/maps/preview/entitylist/getlist?pb=123",
        )

    @staticmethod
    def _make_entitylist_payload(
        indexes: range, total_count: int, *, cursor: str = "", list_id: str = "list-id",
    ) -> str:
        rows = []
        for index in indexes:
            ids = [str(7451636382641713350 + index), str(9055794338847426964 + index)]
            rows.append([
                None,
                [
                    None,
                    None,
                    "",
                    None,
                    f"Address {index}",
                    [None, None, 35.0 + index, 139.0 + index],
                    ids,
                    f"/g/place-{index}",
                ],
                f"Place {index}",
                None,
                None,
                None,
                None,
                None,
                [[1], ids],
            ])
        candidate = [
            [list_id, 3, [[1], [9]], 1, 1],
            4,
            [2, 1, f"https://www.google.com/maps/placelists/list/{list_id}"],
            ["Owner", "https://example.com/avatar.jpg", "104356373423434804635"],
            "Big list",
            "",
            None,
            None,
            rows,
            [None, None, None, [11, "11"]],
            [0, 0],
            [0, 0],
            total_count,
        ]
        return ")]}'\n" + json.dumps([candidate, cursor, None])

    def test_collect_http_artifacts_expands_entitylist_payload_when_total_exceeds_page(
        self,
    ) -> None:
        fake_requests = _FakeCurlRequests(
            responses=[
                _FakeHttpResponse(
                    text=(
                        "<html><head>"
                        '<link href="/maps/preview/entitylist/getlist?pb=%214i500" '
                        'as="fetch" rel="preload">'
                        "</head></html>"
                    ),
                    url="https://www.google.com/maps/@/data=!3m1!4b1",
                ),
                _FakeHttpResponse(
                    text=self._make_entitylist_payload(range(2), total_count=4),
                    url="https://www.google.com/maps/preview/entitylist/getlist?pb=%214i500",
                ),
                _FakeHttpResponse(
                    text=self._make_entitylist_payload(range(4), total_count=4),
                    url="https://www.google.com/maps/preview/entitylist/getlist?pb=%214i4",
                ),
            ]
        )

        with patch(
            "gmaps_scraper.scraper._import_curl_requests",
            return_value=fake_requests,
        ):
            artifacts = collect_http_artifacts(
                "https://maps.app.goo.gl/example",
                timeout_ms=15_000,
                http_session=None,
            )

        self.assertEqual(len(artifacts.script_texts), 1)
        self.assertIn('"Place 3"', artifacts.script_texts[0])
        self.assertTrue(artifacts.preload_expanded)
        self.assertFalse(artifacts.preload_paginated)

        session = fake_requests.sessions[0]
        self.assertEqual(
            session.calls[2][0],
            "https://www.google.com/maps/preview/entitylist/getlist?pb=%214i4",
        )

    def _scrape_entitylist_pages(
        self, *pages: str, page_size: int = 500,
    ) -> tuple[SavedList, list[tuple[str, dict[str, Any]]]]:
        source_url = "https://www.google.com/maps/placelists/list/list-id"
        endpoint = "/maps/preview/entitylist/getlist"
        preload_path = (
            f"{endpoint}?hl=en&amp;pb=%211m1%211slist-id%213e2%214i{page_size}%2116b1"
        )
        fake_requests = _FakeCurlRequests([
            _FakeHttpResponse(
                text=f'<link rel="preload" as="fetch" href="{preload_path}">',
                url=source_url,
            ),
            *[
                _FakeHttpResponse(text=page, url=f"https://www.google.com{endpoint}")
                for page in pages
            ],
        ])
        with patch("gmaps_scraper.scraper._import_curl_requests", return_value=fake_requests):
            _, result = collect_saved_list_result(source_url, collection_mode="curl")
        return result, fake_requests.sessions[0].calls

    def test_paginates_a_saved_list_using_each_cursor_and_preserves_order(self) -> None:
        result, calls = self._scrape_entitylist_pages(
            self._make_entitylist_payload(range(0, 2), 5, cursor="next+p/1="),
            self._make_entitylist_payload(range(2, 4), 5, cursor="next+p/2="),
            self._make_entitylist_payload(range(4, 5), 5),
            page_size=2,
        )

        self.assertEqual([place.name for place in result.places], [
            "Place 0", "Place 1", "Place 2", "Place 3", "Place 4",
        ])
        self.assertEqual(result.title, "Big list")
        self.assertEqual(result.to_dict()["diagnostics"], {
            "artifact_source": "script_texts",
            "runtime_state_present": False,
            "collection_source": "http",
            "used_browser_fallback": False,
            "preload_expanded": False,
            "preload_paginated": True,
            "loaded_rows": 5,
            "expected_rows": 5,
            "quality_flags": [],
        })
        self.assertEqual(len(calls), 4)
        for page, (url, kwargs) in enumerate(calls[2:], start=1):
            self.assertEqual(parse_qs(urlsplit(url).query), {
                "hl": ["en"],
                "pb": [f"!1m1!1slist-id!3e2!4i2!5Bnext-p_{page}!16b1"],
            })
            self.assertEqual(kwargs["referer"], result.resolved_url)

    def test_preserves_collected_rows_when_pagination_stops_early(self) -> None:
        bad_pages = {
            "repeated_cursor": self._make_entitylist_payload(range(2, 3), 600, cursor="next"),
            "overlap": self._make_entitylist_payload(range(1, 3), 600, cursor="next"),
            "different_list": self._make_entitylist_payload(range(2, 3), 600, list_id="other"),
            "changed_total": self._make_entitylist_payload(range(2, 3), 601),
            "invalid_response": "<html>Try again</html>",
        }
        for reason, bad_page in bad_pages.items():
            with self.subTest(reason=reason):
                repeated_cursor = reason == "repeated_cursor"
                result, calls = self._scrape_entitylist_pages(
                    self._make_entitylist_payload(range(2), 600, cursor="next"), bad_page,
                )
                self.assertEqual(len(calls), 3)
                expected_rows = 3 if repeated_cursor else 2
                self.assertEqual(len(result.places), expected_rows)
                assert result.diagnostics is not None
                self.assertEqual(result.diagnostics.preload_paginated, repeated_cursor)
                self.assertEqual(result.diagnostics.loaded_rows, expected_rows)
                self.assertEqual(result.diagnostics.expected_rows, 600)
                self.assertIn("partial_payload", result.diagnostics.quality_flags)

    def test_preserves_first_page_when_bounded_expansion_returns_invalid_content(self) -> None:
        result, calls = self._scrape_entitylist_pages(
            self._make_entitylist_payload(range(2), 600), "<html>Try again</html>",
            page_size=2,
        )

        self.assertEqual([place.name for place in result.places], ["Place 0", "Place 1"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            parse_qs(urlsplit(calls[2][0]).query)["pb"], ["!1m1!1slist-id!3e2!4i500!16b1"],
        )
        assert result.diagnostics is not None
        self.assertFalse(result.diagnostics.preload_expanded)
        self.assertIn("partial_payload", result.diagnostics.quality_flags)

    def test_paginates_when_a_partial_expanded_preload_introduces_a_cursor(self) -> None:
        result, calls = self._scrape_entitylist_pages(
            self._make_entitylist_payload(range(2), 5),
            self._make_entitylist_payload(range(4), 5, cursor="next"),
            self._make_entitylist_payload(range(4, 5), 5),
            page_size=2,
        )

        self.assertEqual([place.name for place in result.places], [
            "Place 0", "Place 1", "Place 2", "Place 3", "Place 4",
        ])
        self.assertEqual([parse_qs(urlsplit(url).query)["pb"] for url, _ in calls[2:]], [
            ["!1m1!1slist-id!3e2!4i5!16b1"],
            ["!1m1!1slist-id!3e2!4i5!5Bnext!16b1"],
        ])
        assert result.diagnostics is not None
        self.assertTrue(result.diagnostics.preload_expanded)
        self.assertTrue(result.diagnostics.preload_paginated)
        self.assertEqual(result.diagnostics.loaded_rows, 5)
        self.assertEqual(result.diagnostics.quality_flags, [])

    def test_does_not_repeat_a_continuation_request_after_preload_expansion(self) -> None:
        result, calls = self._scrape_entitylist_pages(
            self._make_entitylist_payload(range(2), 700, cursor="next"),
            "<html>Try again</html>",
            self._make_entitylist_payload(range(3), 700, cursor="next"),
            page_size=600,
        )

        self.assertEqual([parse_qs(urlsplit(url).query)["pb"] for url, _ in calls[2:]], [
            ["!1m1!1slist-id!3e2!4i500!5Bnext!16b1"],
            ["!1m1!1slist-id!3e2!4i500!16b1"],
        ])
        self.assertEqual(len(result.places), 3)
        assert result.diagnostics is not None
        self.assertTrue(result.diagnostics.preload_expanded)
        self.assertFalse(result.diagnostics.preload_paginated)
        self.assertIn("partial_payload", result.diagnostics.quality_flags)

    def test_resolves_short_link_when_the_first_request_gets_an_interstitial(self) -> None:
        source_url = "https://maps.app.goo.gl/example"
        canonical_url = "https://www.google.com/maps/placelists/list/list-id"
        preload_path = "/maps/preview/entitylist/getlist?pb=%213e2%214i500"
        fake_requests = _FakeCurlRequests([
            _FakeHttpResponse(text="<html>Opening Maps</html>", url=source_url),
            _FakeHttpResponse(
                text=f'<link rel="preload" as="fetch" href="{preload_path}">',
                url=canonical_url,
            ),
            _FakeHttpResponse(
                text=self._make_entitylist_payload(range(2), 2),
                url=f"https://www.google.com{preload_path}",
            ),
        ])

        with (
            patch("gmaps_scraper.scraper._import_curl_requests", return_value=fake_requests),
            patch.object(
                _FakeHttpSession,
                "head",
                create=True,
                return_value=_FakeHttpResponse(text="", url=canonical_url),
            ) as head,
        ):
            _, result = collect_saved_list_result(source_url, collection_mode="curl")

        head.assert_called_once_with(source_url, headers={"User-Agent": "gmaps-scraper"})
        self.assertEqual(result.source_url, source_url)
        self.assertEqual(result.resolved_url, canonical_url)
        self.assertEqual(len(result.places), 2)
        self.assertEqual(fake_requests.sessions[0].calls[-1], (
            f"https://www.google.com{preload_path}",
            {"referer": canonical_url, "timeout": 30.0},
        ))

    def test_collect_http_artifacts_returns_html_without_preload(self) -> None:
        fake_requests = _FakeCurlRequests(
            responses=[
                _FakeHttpResponse(
                    text=(
                        "<html><body>No preload here</body>"
                        "<script>const value = 1;</script></html>"
                    ),
                    url="https://www.google.com/maps",
                )
            ]
        )

        with patch(
            "gmaps_scraper.scraper._import_curl_requests",
            return_value=fake_requests,
        ):
            artifacts = collect_http_artifacts(
                "https://maps.app.goo.gl/example",
                timeout_ms=15_000,
                http_session=None,
            )

        self.assertEqual(artifacts.resolved_url, "https://www.google.com/maps")
        self.assertEqual(artifacts.script_texts, ["const value = 1;"])

    def test_collect_http_artifacts_fetches_preload_with_attributes_in_any_order(self) -> None:
        fake_requests = _FakeCurlRequests(
            responses=[
                _FakeHttpResponse(
                    text=(
                        "<html><head>"
                        "<link rel='preload' as='fetch' "
                        "href='/maps/preview/entitylist/getlist?pb=123'>"
                        "</head></html>"
                    ),
                    url="https://www.google.com/maps/@/data=!3m1!4b1",
                ),
                _FakeHttpResponse(
                    text=")]}'\n[[\"payload\"]]",
                    url="https://www.google.com/maps/preview/entitylist/getlist?pb=123",
                ),
            ]
        )

        with patch(
            "gmaps_scraper.scraper._import_curl_requests",
            return_value=fake_requests,
        ):
            artifacts = collect_http_artifacts(
                "https://maps.app.goo.gl/example",
                timeout_ms=15_000,
                http_session=None,
            )

        self.assertEqual(artifacts.script_texts, [")]}'\n[[\"payload\"]]"])
        session = fake_requests.sessions[0]
        self.assertEqual(
            session.calls[1][0],
            "https://www.google.com/maps/preview/entitylist/getlist?pb=123",
        )

    def test_collect_http_artifacts_prefers_entitylist_over_other_preview_links(self) -> None:
        fake_requests = _FakeCurlRequests(
            responses=[
                _FakeHttpResponse(
                    text=(
                        "<html><head>"
                        '<link rel="preload" as="fetch" href="/maps/preview/log204?foo=1">'
                        '<link rel="preload" as="fetch" '
                        'href="/maps/preview/entitylist/getlist?pb=123">'
                        "</head></html>"
                    ),
                    url="https://www.google.com/maps/@/data=!3m1!4b1",
                ),
                _FakeHttpResponse(
                    text=")]}'\n[[\"payload\"]]",
                    url="https://www.google.com/maps/preview/entitylist/getlist?pb=123",
                ),
            ]
        )

        with patch(
            "gmaps_scraper.scraper._import_curl_requests",
            return_value=fake_requests,
        ):
            artifacts = collect_http_artifacts(
                "https://maps.app.goo.gl/example",
                timeout_ms=15_000,
                http_session=None,
            )

        self.assertEqual(artifacts.script_texts, [")]}'\n[[\"payload\"]]"])
        session = fake_requests.sessions[0]
        self.assertEqual(
            session.calls[1][0],
            "https://www.google.com/maps/preview/entitylist/getlist?pb=123",
        )

    def test_collect_http_artifacts_uses_http_session_proxy_and_cookie_jar(self) -> None:
        fake_requests = _FakeCurlRequests(
            responses=[
                _FakeHttpResponse(
                    text="<html><body>No preload here</body></html>",
                    url="https://www.google.com/maps",
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            cookie_jar_path = Path(tmp_dir) / "cookies.txt"
            with patch(
                "gmaps_scraper.scraper._import_curl_requests",
                return_value=fake_requests,
            ):
                collect_http_artifacts(
                    "https://maps.app.goo.gl/example",
                    timeout_ms=15_000,
                    http_session=HttpSessionConfig(
                        cookie_jar_path=cookie_jar_path,
                        proxy="http://proxy.example:8080",
                    ),
                )

            session = fake_requests.sessions[0]
            self.assertEqual(session.kwargs["proxy"], "http://proxy.example:8080")
            self.assertTrue(cookie_jar_path.is_file())
            self.assertIn("sid", cookie_jar_path.read_text(encoding="utf-8"))


class SavedListFallbackTests(unittest.TestCase):
    def test_collect_saved_list_result_prefers_http_when_parse_succeeds(self) -> None:
        http_artifacts = BrowserArtifacts(
            resolved_url="https://www.google.com/maps/@/data=!3m1!4b1",
            runtime_state=None,
            script_texts=["http-script"],
            html="<html></html>",
        )
        parsed = Mock()

        with (
            patch("gmaps_scraper.scraper.collect_http_artifacts", return_value=http_artifacts),
            patch(
                "gmaps_scraper.scraper.collect_browser_artifacts"
            ) as collect_browser_artifacts,
            patch("gmaps_scraper.scraper.parse_saved_list_artifacts", return_value=parsed),
        ):
            artifacts, result = collect_saved_list_result("https://maps.app.goo.gl/example")

        self.assertIs(artifacts, http_artifacts)
        self.assertIs(result, parsed)
        collect_browser_artifacts.assert_not_called()

    def test_collect_saved_list_result_uses_http_only_mode(self) -> None:
        http_artifacts = BrowserArtifacts(
            resolved_url="https://www.google.com/maps/@/data=!3m1!4b1",
            runtime_state=None,
            script_texts=["http-script"],
            html="<html></html>",
        )
        parsed = Mock()

        with (
            patch("gmaps_scraper.scraper.collect_http_artifacts", return_value=http_artifacts),
            patch(
                "gmaps_scraper.scraper.collect_browser_artifacts"
            ) as collect_browser_artifacts,
            patch("gmaps_scraper.scraper.parse_saved_list_artifacts", return_value=parsed),
        ):
            artifacts, result = collect_saved_list_result(
                "https://maps.app.goo.gl/example",
                collection_mode="curl",
            )

        self.assertIs(artifacts, http_artifacts)
        self.assertIs(result, parsed)
        collect_browser_artifacts.assert_not_called()

    def test_collect_saved_list_result_falls_back_to_browser_after_http_parse_error(self) -> None:
        http_artifacts = BrowserArtifacts(
            resolved_url="https://www.google.com/maps/@/data=!3m1!4b1",
            runtime_state=None,
            script_texts=["http-script"],
            html="<html></html>",
        )
        browser_artifacts = BrowserArtifacts(
            resolved_url="https://www.google.com/maps/@/data=!3m1!4b1",
            runtime_state=["browser-runtime"],
            script_texts=["browser-script"],
            html="<html></html>",
        )
        parsed = Mock(diagnostics=SavedListExtractionDiagnostics("runtime_state", True))

        with (
            patch("gmaps_scraper.scraper.collect_http_artifacts", return_value=http_artifacts),
            patch(
                "gmaps_scraper.scraper.collect_browser_artifacts",
                return_value=browser_artifacts,
            ) as collect_browser_artifacts,
            patch(
                "gmaps_scraper.scraper.parse_saved_list_artifacts",
                side_effect=[ParseError("bad http payload"), parsed],
            ),
        ):
            artifacts, result = collect_saved_list_result("https://maps.app.goo.gl/example")

        self.assertIs(artifacts, browser_artifacts)
        self.assertIs(result, parsed)
        self.assertEqual(result.diagnostics.collection_source, "browser")
        self.assertTrue(result.diagnostics.used_browser_fallback)
        collect_browser_artifacts.assert_called_once()

    def test_collect_saved_list_result_uses_browser_only_mode(self) -> None:
        browser_artifacts = BrowserArtifacts(
            resolved_url="https://www.google.com/maps/@/data=!3m1!4b1",
            runtime_state=["browser-runtime"],
            script_texts=["browser-script"],
            html="<html></html>",
        )
        parsed = Mock()

        with (
            patch("gmaps_scraper.scraper.collect_http_artifacts") as collect_http_artifacts,
            patch(
                "gmaps_scraper.scraper.collect_browser_artifacts",
                return_value=browser_artifacts,
            ) as collect_browser_artifacts,
            patch("gmaps_scraper.scraper.parse_saved_list_artifacts", return_value=parsed),
        ):
            artifacts, result = collect_saved_list_result(
                "https://maps.app.goo.gl/example",
                collection_mode="browser",
            )

        self.assertIs(artifacts, browser_artifacts)
        self.assertIs(result, parsed)
        collect_http_artifacts.assert_not_called()
        collect_browser_artifacts.assert_called_once()

    def test_browser_only_mode_preserves_headless_setting(self) -> None:
        browser_artifacts = BrowserArtifacts(
            resolved_url="https://www.google.com/maps/@/data=!3m1!4b1",
            runtime_state=["browser-runtime"],
            script_texts=["browser-script"],
            html="<html></html>",
        )
        parsed = Mock()

        with (
            patch("gmaps_scraper.scraper.collect_http_artifacts") as collect_http_artifacts,
            patch(
                "gmaps_scraper.scraper.collect_browser_artifacts",
                return_value=browser_artifacts,
            ) as collect_browser_artifacts,
            patch("gmaps_scraper.scraper.parse_saved_list_artifacts", return_value=parsed),
        ):
            artifacts, result = collect_saved_list_result(
                "https://maps.app.goo.gl/example",
                headless=False,
                collection_mode="browser",
            )

        self.assertIs(artifacts, browser_artifacts)
        self.assertIs(result, parsed)
        collect_http_artifacts.assert_not_called()
        collect_browser_artifacts.assert_called_once_with(
            "https://maps.app.goo.gl/example",
            headless=False,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            browser_session=None,
        )

    def test_browser_session_reaches_browser_fallback_path(self) -> None:
        browser_artifacts = BrowserArtifacts(
            resolved_url="https://www.google.com/maps/@/data=!3m1!4b1",
            runtime_state=["browser-runtime"],
            script_texts=["browser-script"],
            html="<html></html>",
        )
        parsed = Mock()
        browser_session = BrowserSessionConfig(
            profile_dir=Path("/tmp/example-session"),
            proxy="http://proxy.example:8080",
        )

        with (
            patch(
                "gmaps_scraper.scraper.collect_http_artifacts",
                side_effect=ScrapeError("bad http"),
            ),
            patch(
                "gmaps_scraper.scraper.collect_browser_artifacts",
                return_value=browser_artifacts,
            ) as collect_browser_artifacts,
            patch("gmaps_scraper.scraper.parse_saved_list_artifacts", return_value=parsed),
        ):
            artifacts, result = collect_saved_list_result(
                "https://maps.app.goo.gl/example",
                collection_mode="auto",
                browser_session=browser_session,
            )

        self.assertIs(artifacts, browser_artifacts)
        self.assertIs(result, parsed)
        collect_browser_artifacts.assert_called_once_with(
            "https://maps.app.goo.gl/example",
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            browser_session=browser_session,
        )

    def test_http_session_reaches_http_collectors(self) -> None:
        http_artifacts = BrowserArtifacts(
            resolved_url="https://www.google.com/maps/@/data=!3m1!4b1",
            runtime_state=None,
            script_texts=["http-script"],
            html="<html></html>",
        )
        parsed = Mock()
        http_session = HttpSessionConfig(
            cookie_jar_path=Path("/tmp/http-cookies.txt"),
            proxy="http://proxy.example:8080",
        )

        with (
            patch("gmaps_scraper.scraper.collect_http_artifacts", return_value=http_artifacts)
            as collect_http_artifacts_mock,
            patch("gmaps_scraper.scraper.parse_saved_list_artifacts", return_value=parsed),
        ):
            artifacts, result = collect_saved_list_result(
                "https://maps.app.goo.gl/example",
                collection_mode="curl",
                http_session=http_session,
            )

        self.assertIs(artifacts, http_artifacts)
        self.assertIs(result, parsed)
        collect_http_artifacts_mock.assert_called_once_with(
            "https://maps.app.goo.gl/example",
            timeout_ms=30_000,
            http_session=http_session,
        )

if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gmaps_scraper.models import (
    PlaceExtractionDiagnostics,
    PlaceLLMRepairRequest,
    PlaceScrapeResult,
)
from gmaps_scraper.place_scraper import (
    _PLACE_ABOUT_PANEL_JS,
    _PLACE_ABOUT_TAB_CLICK_JS,
    _PLACE_DETAIL_READY_JS,
    _PLACE_JS_EXTRACTOR,
    _PLACE_RESERVATION_BUTTON_CLICK_JS,
    _PLACE_RESERVATION_DIALOG_JS,
    _PLACE_REVIEW_SNIPPET_JS,
    _PLACE_REVIEW_TAB_CLICK_JS,
    _PLACE_REVIEW_TOPIC_JS,
    _PLACE_SEARCH_RESULT_CLICK_JS,
    _PLACE_SEARCH_RESULT_OPEN_JS,
    _build_place_details,
    _build_place_details_from_snapshot,
    _build_place_diagnostics,
    _build_place_llm_evidence,
    _clean_category_text,
    _clean_description_text,
    _clean_name_text,
    _collect_reservation_dialog_snapshot,
    _extract_address_from_lines,
    _extract_admission_price_from_lines,
    _extract_preview_address,
    _extract_preview_coordinates,
    _extract_preview_description,
    _extract_preview_phone,
    _extract_preview_place_enrichment,
    _extract_price_range_from_lines,
    _extract_review_count_from_lines,
    _extract_secondary_name,
    _hash_evidence,
    _looks_like_google_maps_place_url,
    _merge_llm_place_fields,
    _merge_place_sources,
    _merge_reservation_links,
    _normalize_google_place_id,
    _normalize_phone_candidate,
    _normalize_photo_url,
    _normalize_preview_website,
    _normalize_reservation_links,
    _normalize_review_topics,
    _normalize_reviews,
    _normalize_website,
    _open_place_result_from_search_page,
    _parse_price_amount,
    _parse_review_count,
    _search_result_candidate_url,
    _search_result_snapshot,
    _seed_google_consent_cookies,
    _should_use_llm_repair,
    collect_place_snapshot,
    scrape_places,
)
from gmaps_scraper.scraper import (
    BrowserSessionConfig,
    HttpSessionConfig,
    ScrapeError,
    _launch_browser_context,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "place_pages"
_PREVIEW_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "preview_payloads"
_SKIP_BROWSER_FIXTURE_TESTS_ENV = "GMAPS_SCRAPER_SKIP_BROWSER_FIXTURE_TESTS"


class PlaceJsExtractorFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get(_SKIP_BROWSER_FIXTURE_TESTS_ENV):
            raise unittest.SkipTest(
                f"{_SKIP_BROWSER_FIXTURE_TESTS_ENV} is set; skipping browser fixture tests."
            )
        try:
            cls.context = _launch_browser_context(
                headless=True,
                browser_session=BrowserSessionConfig(window_size=(1280, 720)),
            )
        except Exception as exc:
            raise unittest.SkipTest(
                f"Browser fixture tests require a launchable browser: {exc}"
            ) from exc

    @classmethod
    def tearDownClass(cls) -> None:
        context = getattr(cls, "context", None)
        if context is not None:
            context.close()

    def _evaluate_fixture(self, fixture_name: str, script: str = _PLACE_JS_EXTRACTOR) -> object:
        page = self.context.new_page()
        try:
            page.goto(
                (_FIXTURE_DIR / fixture_name).as_uri(),
                wait_until="domcontentloaded",
                timeout=5_000,
            )
            return page.evaluate(script)
        finally:
            page.close()

    def test_overview_fixture_extracts_core_place_fields(self) -> None:
        snapshot = self._evaluate_fixture("overview.html")

        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["name"], "Fixture Cafe")
        self.assertEqual(snapshot["rating"], "4.6")
        self.assertEqual(snapshot["review_count"], "1,234")
        self.assertEqual(snapshot["review_count_source"], "f7nice")
        self.assertEqual(snapshot["category"], "Coffee shop")
        self.assertEqual(
            snapshot["description"],
            "Compact fixture description for the place overview.",
        )
        self.assertEqual(snapshot["address"], "123 Test Street, Fixture City")
        self.assertEqual(snapshot["website"], "https://fixture.example/")
        self.assertEqual(snapshot["phone"], "+1 555-0100")
        self.assertEqual(snapshot["plus_code"], "7FG8+22 Fixture City")
        self.assertEqual(
            snapshot["main_photo_url"],
            "https://lh3.googleusercontent.com/place-overview.jpg",
        )
        self.assertFalse(snapshot["limited_view"])

    def test_reviews_fixture_keeps_reviewer_photo_out_of_place_photo(self) -> None:
        snapshot = self._evaluate_fixture("reviews.html")
        reviews = self._evaluate_fixture("reviews.html", _PLACE_REVIEW_SNIPPET_JS)

        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["name"], "Fixture Reviews Cafe")
        self.assertEqual(
            snapshot["main_photo_url"],
            "https://lh3.googleusercontent.com/place-review-hero.jpg",
        )
        self.assertNotEqual(
            snapshot["main_photo_url"],
            "https://lh3.googleusercontent.com/reviewer-placeholder.jpg",
        )
        self.assertEqual(
            [(topic["text"], topic["aria_label"]) for topic in snapshot["review_topics"]],
            [
                ("coffee 19", "coffee, mentioned in 19 reviews"),
                ("service 12", "service, mentioned in 12 reviews"),
            ],
        )
        self.assertIsInstance(reviews, list)
        self.assertEqual(reviews[0]["author"], "Reviewer Placeholder")
        self.assertEqual(reviews[0]["text"], "Useful visible review snippet.")

    def test_about_fixture_preserves_core_fields_and_reads_about_sections(self) -> None:
        snapshot = self._evaluate_fixture("about.html")
        about_sections = self._evaluate_fixture("about.html", _PLACE_ABOUT_PANEL_JS)

        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["name"], "Fixture About Bakery")
        self.assertEqual(snapshot["category"], "Bakery")
        self.assertEqual(snapshot["address"], "9 About Avenue, Fixture City")
        self.assertEqual(snapshot["phone"], "+1 555-0102")
        self.assertEqual(
            about_sections,
            [
                {
                    "title": "Service options",
                    "items": [
                        {
                            "label": "Takeaway",
                            "aria_label": "Offers takeaway",
                            "source": "about_panel",
                        },
                        {
                            "label": "Delivery",
                            "aria_label": "Offers delivery",
                            "source": "about_panel",
                        },
                    ],
                }
            ],
        )

    def test_limited_view_fixture_sets_limited_view_flag(self) -> None:
        snapshot = self._evaluate_fixture("limited_view.html")

        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["name"], "Limited Fixture Place")
        self.assertTrue(snapshot["limited_view"])

    def test_search_result_fixture_is_not_detail_ready(self) -> None:
        snapshot = self._evaluate_fixture("search_result.html")
        detail_ready = self._evaluate_fixture("search_result.html", _PLACE_DETAIL_READY_JS)
        search_candidate = self._evaluate_fixture(
            "search_result.html",
            _PLACE_SEARCH_RESULT_CLICK_JS,
        )

        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["name"], "Results")
        self.assertFalse(detail_ready)
        self.assertIs(search_candidate, False)


class PlaceScraperTests(unittest.TestCase):
    def test_build_place_details_from_snapshot_rejects_saved_list_resolution(self) -> None:
        with self.assertRaisesRegex(ScrapeError, "saved list"):
            _build_place_details_from_snapshot(
                "https://maps.app.goo.gl/example",
                snapshot={
                    "resolved_url": (
                        "https://www.google.com/maps/@1,2,14z/"
                        "data=!4m3!11m2!2sShpCfVAkTaGQFUSz8UklcQ!3e3"
                    ),
                    "dom": {"name": "Singapore"},
                },
                llm_fallback=None,
                llm_policy="on_quality_failure",
            )

    def test_place_js_extractor_avoids_broad_photo_selectors(self) -> None:
        self.assertNotIn("button[jsaction*='image'] img", _PLACE_JS_EXTRACTOR)
        self.assertNotIn("button[jsaction*='photo'] img", _PLACE_JS_EXTRACTOR)
        self.assertNotIn("button[aria-label^='Photo of'] img", _PLACE_JS_EXTRACTOR)
        self.assertNotIn("], document)\n    || firstBackgroundImageUrl", _PLACE_JS_EXTRACTOR)

    def test_collect_place_snapshot_can_skip_reviews_and_about_tabs(self) -> None:
        class _FakePage:
            url = "https://www.google.com/maps/place/Den"

            def __init__(self) -> None:
                self.evaluate_calls = 0

            def goto(self, *_args: object, **_kwargs: object) -> None:
                pass

            def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
                pass

            def wait_for_selector(self, *_args: object, **_kwargs: object) -> None:
                pass

            def wait_for_timeout(self, *_args: object, **_kwargs: object) -> None:
                pass

            def reload(self, *_args: object, **_kwargs: object) -> None:
                pass

            def screenshot(self, *, path: str, **_kwargs: object) -> None:
                Path(path).write_bytes(b"screenshot")

            def evaluate(self, script: object) -> object:
                self.evaluate_calls += 1
                if script == _PLACE_JS_EXTRACTOR:
                    return {"name": "Den"}
                return True

            def close(self) -> None:
                pass

        class _FakeContext:
            def __init__(self) -> None:
                self.page = _FakePage()
                self.closed = False

            def new_page(self) -> _FakePage:
                return self.page

            def close(self) -> None:
                self.closed = True

        context = _FakeContext()
        with tempfile.TemporaryDirectory() as tmp_dir:
            screenshot_path = Path(tmp_dir) / "overview-only.png"
            with (
                patch(
                    "gmaps_scraper.place_scraper._launch_browser_context",
                    return_value=context,
                ),
                patch("gmaps_scraper.place_scraper._handle_google_consent"),
                patch("gmaps_scraper.place_scraper._ensure_review_signal") as review_signal,
                patch(
                    "gmaps_scraper.place_scraper._collect_preview_place_enrichment",
                    return_value={},
                ),
            ):
                snapshot = collect_place_snapshot(
                    "https://www.google.com/maps/place/Den",
                    collect_reviews=False,
                    collect_about=False,
                    screenshot_path=screenshot_path,
                )

            self.assertTrue(context.closed)
            self.assertEqual(snapshot["dom"], {"name": "Den"})
            self.assertGreaterEqual(context.page.evaluate_calls, 2)
            review_signal.assert_not_called()
            self.assertEqual(screenshot_path.read_bytes(), b"screenshot")

    def test_collect_reservation_dialog_snapshot_clicks_and_reads_provider_links(self) -> None:
        class _FakePage:
            def __init__(self) -> None:
                self.waited: list[int] = []

            def evaluate(self, script: object) -> object:
                if script == _PLACE_RESERVATION_BUTTON_CLICK_JS:
                    return True
                if script == _PLACE_RESERVATION_DIALOG_JS:
                    return [{"label": "TableCheck", "url": "https://www.tablecheck.com/example"}]
                return None

            def wait_for_timeout(self, value: int) -> None:
                self.waited.append(value)

        page = _FakePage()

        self.assertEqual(
            _collect_reservation_dialog_snapshot(page, timeout_ms=30_000),
            {"reservation_links": [{"label": "TableCheck", "url": "https://www.tablecheck.com/example"}]},
        )
        self.assertEqual(page.waited, [1_500])

    def test_reservation_dialog_extractor_trusts_provider_popup_without_dialog_role(self) -> None:
        self.assertIn("continue with", _PLACE_RESERVATION_DIALOG_JS)
        self.assertIn("about these providers", _PLACE_RESERVATION_DIALOG_JS)
        self.assertIn("hasTrustedProviderRoot", _PLACE_RESERVATION_DIALOG_JS)
        self.assertIn("closeProviderRoot", _PLACE_RESERVATION_DIALOG_JS)
        self.assertIn(
            "return (rightRect.width * rightRect.height) - (leftRect.width * leftRect.height);",
            _PLACE_RESERVATION_DIALOG_JS,
        )
        self.assertIn("KeyboardEvent", _PLACE_RESERVATION_DIALOG_JS)

    def test_reservation_extractors_match_provider_hosts_with_boundaries(self) -> None:
        self.assertNotIn("providerHostMatches", _PLACE_JS_EXTRACTOR)
        self.assertIn("if (!reservationPattern.test(evidence))", _PLACE_JS_EXTRACTOR)
        self.assertIn("providerHostMatches", _PLACE_RESERVATION_DIALOG_JS)
        self.assertIn("(^|[.-])(?:opentable|resy|sevenrooms", _PLACE_RESERVATION_DIALOG_JS)
        self.assertNotIn("providerHostPattern.test(evidence)", _PLACE_RESERVATION_DIALOG_JS)

    def test_reservation_extractors_do_not_match_generic_booking_copy(self) -> None:
        for script in (
            _PLACE_JS_EXTRACTOR,
            _PLACE_RESERVATION_BUTTON_CLICK_JS,
            _PLACE_RESERVATION_DIALOG_JS,
        ):
            self.assertIn("book a table", script)
            self.assertNotIn("|booking", script)
            self.assertNotIn("book(?: a table)?", script)

    def test_collect_reservation_dialog_snapshot_skips_when_no_button(self) -> None:
        class _FakePage:
            def __init__(self) -> None:
                self.waited: list[int] = []

            def evaluate(self, script: object) -> object:
                if script == _PLACE_RESERVATION_BUTTON_CLICK_JS:
                    return False
                raise AssertionError("dialog should not be read")

            def wait_for_timeout(self, value: int) -> None:
                self.waited.append(value)

        page = _FakePage()

        self.assertEqual(_collect_reservation_dialog_snapshot(page, timeout_ms=30_000), {})
        self.assertEqual(page.waited, [])

    def test_merge_reservation_links_dedupes_overview_and_dialog_links(self) -> None:
        merged = _merge_reservation_links(
            {"reservation_links": [{"label": "Resy", "url": "https://resy.com/example"}]},
            {
                "reservation_links": [
                    {"label": "Resy duplicate", "url": "https://resy.com/example"},
                    {"label": "TableCheck", "url": "https://www.tablecheck.com/example"},
                ]
            },
        )

        self.assertEqual(
            merged["reservation_links"],
            [
                {"label": "Resy", "url": "https://resy.com/example"},
                {"label": "TableCheck", "url": "https://www.tablecheck.com/example"},
            ],
        )

    def test_merge_reservation_links_stores_normalized_redirect_urls(self) -> None:
        merged = _merge_reservation_links(
            {
                "reservation_links": [
                    {
                        "label": "Inline",
                        "url": "https://www.google.com:443/url?q=https%3A%2F%2Finline.app%2Fbooking%2Ffoo",
                    }
                ]
            },
            {"reservation_links": []},
        )

        self.assertEqual(
            merged["reservation_links"],
            [{"label": "Inline", "url": "https://inline.app/booking/foo"}],
        )

    def test_scrape_places_reuses_context_and_retries_quality_flags(self) -> None:
        class _FakeContext:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        context = _FakeContext()
        snapshots = [
            {
                "resolved_url": "https://www.google.com/maps/place/Den",
                "dom": {
                    "name": "Den",
                    "address": "Tokyo, Japan",
                    "rating": "4.4",
                    "review_count": "324",
                    "limited_view": True,
                },
                "preview": {},
            },
            {
                "resolved_url": "https://www.google.com/maps/place/Den",
                "dom": {
                    "name": "Den",
                    "address": "Tokyo, Japan",
                    "rating": "4.4",
                    "review_count": "324",
                },
                "preview": {},
            },
        ]

        with (
            patch(
                "gmaps_scraper.place_scraper._launch_browser_context",
                return_value=context,
            ) as launch_context,
            patch(
                "gmaps_scraper.place_scraper._collect_place_snapshot_with_context",
                side_effect=snapshots,
            ) as collect_snapshot,
            patch("gmaps_scraper.place_scraper.time.sleep") as sleep,
        ):
            results = scrape_places(
                ["https://www.google.com/maps/place/Den"],
                max_retries=1,
                retry_backoff_ms=500,
            )

        self.assertTrue(context.closed)
        launch_context.assert_called_once()
        self.assertEqual(collect_snapshot.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].attempts, 2)
        self.assertIsNone(results[0].error)
        self.assertIsNotNone(results[0].place)
        self.assertFalse(results[0].place.limited_view)

    def test_scrape_places_parallel_uses_worker_scoped_session_paths(self) -> None:
        seen_profile_dirs: list[Path | None] = []
        seen_cookie_jar_paths: list[Path | None] = []
        seen_human_mouse: list[bool] = []
        seen_window_sizes: list[object] = []

        def fake_scrape_places_sequential(
            place_urls: list[str],
            **kwargs: object,
        ) -> list[PlaceScrapeResult]:
            browser_session = kwargs["browser_session"]
            http_session = kwargs["http_session"]
            self.assertIsInstance(browser_session, BrowserSessionConfig)
            self.assertIsInstance(http_session, HttpSessionConfig)
            seen_profile_dirs.append(browser_session.profile_dir)
            seen_human_mouse.append(browser_session.human_mouse)
            seen_window_sizes.append(browser_session.window_size)
            seen_cookie_jar_paths.append(http_session.cookie_jar_path)
            return [
                PlaceScrapeResult(source_url=place_url, attempts=1)
                for place_url in place_urls
            ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_dir = Path(tmp_dir) / "session"
            cookie_jar_path = Path(tmp_dir) / "cookies.txt"
            with (
                patch(
                    "gmaps_scraper.place_scraper._scrape_places_sequential",
                    side_effect=fake_scrape_places_sequential,
                ),
                patch("gmaps_scraper.place_scraper.time.sleep"),
            ):
                results = scrape_places(
                    ["url-1", "url-2", "url-3"],
                    browser_session=BrowserSessionConfig(
                        profile_dir=profile_dir,
                        window_size=None,
                        human_mouse=True,
                    ),
                    http_session=HttpSessionConfig(cookie_jar_path=cookie_jar_path),
                    max_concurrency=2,
                    stagger_ms=10,
                )

        self.assertEqual(
            sorted(path for path in seen_profile_dirs if path is not None),
            sorted([profile_dir / "worker-1", profile_dir / "worker-2"]),
        )
        self.assertEqual(
            sorted(path for path in seen_cookie_jar_paths if path is not None),
            sorted(
                [
                    cookie_jar_path.parent / "cookies.worker-1.txt",
                    cookie_jar_path.parent / "cookies.worker-2.txt",
                ]
            ),
        )
        self.assertEqual(seen_human_mouse, [True, True])
        self.assertEqual(seen_window_sizes, [None, None])
        self.assertEqual([result.source_url for result in results], ["url-1", "url-2", "url-3"])

    def test_scrape_places_parallel_returns_worker_errors_per_url(self) -> None:
        def fake_scrape_places_sequential(
            place_urls: list[str],
            **kwargs: object,
        ) -> list[PlaceScrapeResult]:
            del kwargs
            if "bad-url" in place_urls:
                raise RuntimeError("context launch failed")
            return [
                PlaceScrapeResult(source_url=place_url, attempts=1)
                for place_url in place_urls
            ]

        with (
            patch(
                "gmaps_scraper.place_scraper._scrape_places_sequential",
                side_effect=fake_scrape_places_sequential,
            ),
            patch("gmaps_scraper.place_scraper.time.sleep"),
        ):
            results = scrape_places(
                ["ok-url", "bad-url"],
                max_concurrency=2,
            )

        self.assertEqual(results[0].source_url, "ok-url")
        self.assertIsNone(results[0].error)
        self.assertEqual(results[1].source_url, "bad-url")
        self.assertEqual(results[1].attempts, 0)
        self.assertIn("context launch failed", results[1].error or "")

    def test_scrape_places_strips_input_urls_before_scraping(self) -> None:
        seen_urls: list[str] = []

        def fake_scrape_places_sequential(
            place_urls: list[str],
            **kwargs: object,
        ) -> list[PlaceScrapeResult]:
            del kwargs
            seen_urls.extend(place_urls)
            return [
                PlaceScrapeResult(source_url=place_url, attempts=1)
                for place_url in place_urls
            ]

        with patch(
            "gmaps_scraper.place_scraper._scrape_places_sequential",
            side_effect=fake_scrape_places_sequential,
        ):
            results = scrape_places(["  https://www.google.com/maps/place/Den  ", "  "])

        self.assertEqual(seen_urls, ["https://www.google.com/maps/place/Den"])
        self.assertEqual(
            [result.source_url for result in results],
            ["https://www.google.com/maps/place/Den"],
        )

    def test_build_place_details_preserves_raw_price_range_and_about_sections(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Fiamma",
            resolved_url="https://www.google.com/maps/place/Fiamma",
            snapshot={
                "name": "Fiamma",
                "category": "Italian restaurant",
                "rating": "4.8",
                "review_count": "832",
                "price_range": "SGD 100+",
                "address": "1 The Knolls, Singapore 098297",
                "about_sections": [
                    {
                        "title": "Accessibility",
                        "items": [
                            {
                                "label": "Wheelchair accessible entrance",
                                "aria_label": "Has wheelchair accessible entrance",
                                "source": "about_panel",
                            },
                            {"label": "Wheelchair accessible parking lot"},
                        ],
                    }
                ],
            },
        )

        self.assertEqual(details.price_range, "SGD 100+")
        self.assertEqual(
            [section.to_dict() for section in details.about_sections],
            [
                {
                    "title": "Accessibility",
                    "items": [
                        {
                            "label": "Wheelchair accessible entrance",
                            "aria_label": "Has wheelchair accessible entrance",
                        },
                        {"label": "Wheelchair accessible parking lot"},
                    ],
                }
            ],
        )

    def test_build_place_details_accepts_symbolic_price_range(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Japan",
            resolved_url="https://www.google.com/maps/place/Japan",
            snapshot={
                "name": "Japan Place",
                "category": "Restaurant",
                "rating": "4.5",
                "review_count": "120",
                "price_range": "$$",
                "address": "Tokyo, Japan",
            },
        )

        self.assertEqual(details.price_range, "$$")

    def test_extract_price_range_from_lines_rejects_offer_quote_rows(self) -> None:
        self.assertIsNone(
            _extract_price_range_from_lines(
                [
                    "Admission · NT$100",
                    "2 options · NT$5,293",
                ]
            )
        )

    def test_extract_price_range_from_lines_accepts_place_summary_rows(self) -> None:
        self.assertEqual(
            _extract_price_range_from_lines(
                ["4.8 · (326) · NT$2,000+ · Fine dining restaurant"]
            ),
            "NT$2,000+",
        )

    def test_extract_admission_price_from_lines_accepts_localized_headings(self) -> None:
        self.assertEqual(
            _extract_admission_price_from_lines(
                [
                    "門票",
                    "官方網站",
                    "NT$320",
                    "Klook",
                    "NT$320",
                ]
            ),
            "NT$320",
        )

    def test_build_place_details_summarizes_admission_prices_separately(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Shinjuku+Gyoen",
            resolved_url="https://www.google.com/maps/place/Shinjuku+Gyoen",
            snapshot={
                "name": "Shinjuku Gyoen National Garden",
                "category": "National park",
                "rating": "4.6",
                "review_count": "12,340",
                "address": "11 Naitomachi, Shinjuku City, Tokyo 160-0014, Japan",
                "admission_prices": ["NT$100.40", "NT$101.00", "NT$101.00"],
                "body_text": "\n".join(
                    [
                        "Admission",
                        "Official site",
                        "NT$100.40",
                        "Klook",
                        "NT$101.00",
                    ]
                ),
            },
        )

        self.assertIsNone(details.price_range)
        self.assertEqual(details.admission_price, "NT$101.00")
        self.assertIsNone(details.room_price)

    def test_build_place_details_moves_localized_admission_price_out_of_price_range(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Tokyo+Tower",
            resolved_url="https://www.google.com/maps/place/Tokyo+Tower",
            snapshot={
                "name": "Tokyo Tower",
                "category": "Observation deck",
                "rating": "4.5",
                "review_count": "40,001",
                "price_range": "NT$320",
                "admission_prices": ["NT$320"],
                "address": "4 Chome-2-8 Shibakoen, Minato City, Tokyo 105-0011, Japan",
                "body_text": "\n".join(
                    [
                        "門票",
                        "官方網站",
                        "NT$320",
                        "Klook",
                        "NT$320",
                    ]
                ),
            },
        )

        self.assertIsNone(details.price_range)
        self.assertEqual(details.admission_price, "NT$320")

    def test_build_place_details_uses_structural_admission_offers_before_address(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Tokyo+Tower",
            resolved_url="https://www.google.com/maps/place/Tokyo+Tower",
            snapshot={
                "name": "Tokyo Tower",
                "category": "Observation deck",
                "rating": "4.5",
                "review_count": "40,001",
                "structural_offer_kind": "admission",
                "structural_offer_prices": ["NT$320", "NT$320", "NT$420"],
                "address": "4 Chome-2-8 Shibakoen, Minato City, Tokyo 105-0011, Japan",
            },
        )

        self.assertEqual(details.admission_price, "NT$320")
        self.assertIsNone(details.room_price)

    def test_build_place_details_keeps_distinct_price_range_when_admission_differs(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Museum",
            resolved_url="https://www.google.com/maps/place/Museum",
            snapshot={
                "name": "Museum",
                "category": "Art museum",
                "rating": "4.4",
                "review_count": "1,234",
                "price_range": "¥1,000–2,000",
                "admission_prices": ["¥320", "¥320"],
                "address": "Example address",
            },
        )

        self.assertEqual(details.price_range, "¥1,000–2,000")
        self.assertEqual(details.admission_price, "¥320")

    def test_build_place_details_summarizes_room_prices_separately(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Tokyo+Prince+Hotel",
            resolved_url="https://www.google.com/maps/place/Tokyo+Prince+Hotel",
            snapshot={
                "name": "Tokyo Prince Hotel",
                "category": "Hotel",
                "rating": "4.2",
                "review_count": "5,481",
                "address": "3 Chome-3-1 Shibakoen, Minato City, Tokyo 105-8560, Japan",
                "room_prices": [
                    "NT$5,960",
                    "NT$6,473",
                    "NT$7,299",
                    "NT$7,355",
                ],
                "room_price_overlay": "NT$5,293",
                "body_text": "\n".join(
                    [
                        "Compare prices",
                        "Agoda",
                        "NT$5,960",
                        "Priceline",
                        "NT$5,293",
                    ]
                ),
            },
        )

        self.assertIsNone(details.price_range)
        self.assertIsNone(details.admission_price)
        self.assertEqual(details.room_price, "NT$6,473")

    def test_build_place_details_uses_structural_room_offers_before_address(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Tokyo+Prince+Hotel",
            resolved_url="https://www.google.com/maps/place/Tokyo+Prince+Hotel",
            snapshot={
                "name": "Tokyo Prince Hotel",
                "category": "Hotel",
                "rating": "4.2",
                "review_count": "5,481",
                "structural_offer_kind": "room",
                "structural_offer_prices": ["NT$5,960", "NT$6,473", "NT$7,299"],
                "address": "3 Chome-3-1 Shibakoen, Minato City, Tokyo 105-8560, Japan",
            },
        )

        self.assertIsNone(details.admission_price)
        self.assertEqual(details.room_price, "NT$6,473")

    def test_build_place_details_orders_comma_decimal_offer_prices(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Hotel",
            resolved_url="https://www.google.com/maps/place/Hotel",
            snapshot={
                "name": "Hotel",
                "category": "Hotel",
                "rating": "4.2",
                "review_count": "500",
                "address": "Example address",
                "room_prices": ["€999,00", "€1.234,56", "€2.000,00"],
            },
        )

        self.assertEqual(details.room_price, "€1.234,56")

    def test_parse_price_amount_handles_localized_grouping(self) -> None:
        self.assertEqual(_parse_price_amount("1.234"), 1234.0)
        self.assertEqual(_parse_price_amount("1.234.567"), 1234567.0)
        self.assertEqual(_parse_price_amount("1.234,56"), 1234.56)
        self.assertEqual(_parse_price_amount("1,234.56"), 1234.56)

    def test_place_js_extractor_falls_back_to_address_icon_rows(self) -> None:
        self.assertIn('const isAddressIcon = (icon) => {', _PLACE_JS_EXTRACTOR)
        self.assertIn('glyph === ""', _PLACE_JS_EXTRACTOR)
        self.assertIn('panel.querySelectorAll(".google-symbols, [role=', _PLACE_JS_EXTRACTOR)
        self.assertIn('icon.closest(".LCF4w', _PLACE_JS_EXTRACTOR)
        self.assertIn('const rowValue = (row) => {', _PLACE_JS_EXTRACTOR)

    def test_place_js_extractor_collects_quote_sections_separately(self) -> None:
        self.assertLess(
            _PLACE_JS_EXTRACTOR.index("const collectLeafPrices ="),
            _PLACE_JS_EXTRACTOR.index("const priceRangeValue ="),
        )
        self.assertLess(
            _PLACE_JS_EXTRACTOR.index("const roomOverlayPrice ="),
            _PLACE_JS_EXTRACTOR.index("const priceRangeValue ="),
        )
        self.assertIn(
            "const headingAliases = (value) => Array.isArray(value) ? value : [value];",
            _PLACE_JS_EXTRACTOR,
        )
        self.assertIn(
            '"門票"',
            _PLACE_JS_EXTRACTOR,
        )
        self.assertIn(
            '"料金を比較"',
            _PLACE_JS_EXTRACTOR,
        )
        self.assertNotIn('"overview"', _PLACE_ABOUT_TAB_CLICK_JS.lower())
        self.assertIn("const isSearchPage =", _PLACE_SEARCH_RESULT_CLICK_JS)
        self.assertIn(
            "new URL(hrefValue, window.location.href).href",
            _PLACE_SEARCH_RESULT_CLICK_JS,
        )
        self.assertIn("searchResultTitleLabels", _PLACE_SEARCH_RESULT_CLICK_JS)
        self.assertIn("parseCardReviewCount", _PLACE_SEARCH_RESULT_CLICK_JS)
        self.assertIn(
            "const cardDescription = (article, excludedValues) => {",
            _PLACE_SEARCH_RESULT_CLICK_JS,
        )
        self.assertIn('article.querySelectorAll("div.W4Efsd")', _PLACE_SEARCH_RESULT_CLICK_JS)
        self.assertNotIn("findDescriptionLine", _PLACE_SEARCH_RESULT_CLICK_JS)
        self.assertIn("getBoundingClientRect()", _PLACE_SEARCH_RESULT_OPEN_JS)
        self.assertIn("const placePanelRoot = () => {", _PLACE_JS_EXTRACTOR)
        self.assertIn("visibleArea", _PLACE_JS_EXTRACTOR)
        self.assertIn("articleCount >= 2", _PLACE_JS_EXTRACTOR)
        self.assertIn("searchResultTitleLabels", _PLACE_DETAIL_READY_JS)
        self.assertIn("placePanelRoot", _PLACE_REVIEW_TAB_CLICK_JS)
        self.assertIn("const detailsBoundaryTop = () => {", _PLACE_JS_EXTRACTOR)
        self.assertIn('structural_offer_kind: structuralOffers.kind,', _PLACE_JS_EXTRACTOR)
        self.assertIn(
            'panel.querySelector(`[data-item-id="place-info-links:"]`)',
            _PLACE_JS_EXTRACTOR,
        )
        self.assertIn('button[aria-label*=\'per night\' i]', _PLACE_JS_EXTRACTOR)

    def test_review_topic_collection_can_click_review_tab_and_read_chips(self) -> None:
        self.assertIn("button[role='tab']", _PLACE_REVIEW_TAB_CLICK_JS)
        self.assertIn("(review|reviews|評論|クチコミ)", _PLACE_REVIEW_TAB_CLICK_JS)
        self.assertIn("/^\\+\\d+$/.test(text)", _PLACE_REVIEW_TOPIC_JS)
        self.assertIn("button[role='radio']", _PLACE_REVIEW_TOPIC_JS)
        self.assertIn("button[aria-pressed]", _PLACE_REVIEW_TOPIC_JS)

    def test_parse_review_count_handles_suffixes(self) -> None:
        self.assertEqual(_parse_review_count("324"), 324)
        self.assertEqual(_parse_review_count("1,296"), 1296)
        self.assertEqual(_parse_review_count("1.296"), 1296)
        self.assertEqual(_parse_review_count("3.6K"), 3600)
        self.assertEqual(_parse_review_count("9.4万"), 94000)

    def test_extract_review_count_from_lines_prefers_place_panel_over_related_cards(
        self,
    ) -> None:
        self.assertEqual(
            _extract_review_count_from_lines(
                [
                    "Ad Astra",
                    "4.8",
                    "(326)·NT$2,000+",
                    "Fine dining restaurant",
                    "Review summary",
                    "326 reviews",
                    "People also search for",
                    "WOW Bistro",
                    "4.6(5,590)",
                ]
            ),
            326,
        )

    def test_build_place_details_prefers_structured_review_count_over_text_fallback(
        self,
    ) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den",
            snapshot={
                "name": "Den",
                "category": "Restaurant",
                "review_count": "324",
                "body_text": "\n".join(
                    [
                        "People also search for",
                        "WOW Bistro",
                        "4.6",
                        "5,590 reviews",
                        "Den",
                        "324 reviews",
                    ]
                ),
            },
        )

        self.assertEqual(details.review_count, 324)

    def test_normalize_review_topics_extracts_filter_chips(self) -> None:
        topics = _normalize_review_topics(
            [
                {"text": "pho 501", "source": "button[jsaction*='review']"},
                {"text": "bun bo nam bo 623"},
                {"aria_label": "Mentioned in 29 reviews: banh xeo"},
                {"text": "sushi", "aria_label": "sushi, mentioned in 115 reviews"},
                {"text": "Most relevant"},
                {"text": "5 stars 89"},
                {"text": "like 1"},
                {"text": "michelin one-star 34"},
            ]
        )

        self.assertEqual(
            [topic.to_dict() for topic in topics],
            [
                {
                    "label": "pho",
                    "count": 501,
                },
                {"label": "bun bo nam bo", "count": 623},
                {"label": "banh xeo", "count": 29},
                {"label": "sushi", "count": 115},
                {"label": "michelin one-star", "count": 34},
            ],
        )

    def test_normalize_reviews_extracts_visible_review_snippets(self) -> None:
        reviews = _normalize_reviews(
            [
                {
                    "rating": "5 stars",
                    "relative_time": "3 months ago",
                    "text": "Spectacular food. More",
                    "like_count": "Like",
                    "source": "dom",
                },
                {
                    "author": "Michael Pinkerton",
                    "source": "dom",
                },
                {
                    "author": "Gustavo Montez",
                    "rating": "5 stars",
                    "relative_time": "7 months ago",
                    "text": "Nice~~~",
                    "like_count": "1",
                    "source": "dom",
                },
            ]
        )

        self.assertEqual(
            [review.to_dict() for review in reviews],
            [
                {
                    "author": "Michael Pinkerton",
                    "rating": 5.0,
                    "relative_time": "3 months ago",
                    "text": "Spectacular food.",
                },
                {
                    "author": "Gustavo Montez",
                    "rating": 5.0,
                    "relative_time": "7 months ago",
                    "text": "Nice~~~",
                    "like_count": 1,
                },
            ],
        )

    def test_llm_policy_uses_repair_only_for_quality_failures(self) -> None:
        self.assertFalse(
            _should_use_llm_repair(
                "on_quality_failure",
                PlaceExtractionDiagnostics(confidence=0.95),
            )
        )
        self.assertTrue(
            _should_use_llm_repair(
                "on_quality_failure",
                PlaceExtractionDiagnostics(
                    quality_flags=["thin_place_result"],
                    confidence=0.52,
                ),
            )
        )

    def test_llm_tasks_scope_quality_gate_and_request(self) -> None:
        calls: list[PlaceLLMRepairRequest] = []
        snapshot = {
            "resolved_url": "https://www.google.com/maps/place/Den",
            "dom": {
                "name": "Den",
                "category": "테스트카테고리",
                "rating": "4.4",
                "review_count": "324",
                "address": "Tokyo, Japan",
            },
            "preview": {},
        }

        details = _build_place_details_from_snapshot(
            "https://www.google.com/maps/place/Den",
            snapshot=snapshot,
            llm_fallback=lambda request: calls.append(request)
            or {"category_display_en": "Test Category"},
            llm_policy="on_quality_failure",
            llm_tasks=("dom_repair",),
        )

        self.assertNotEqual(details.category_display_en, "Test Category")
        self.assertEqual(calls, [])

        details = _build_place_details_from_snapshot(
            "https://www.google.com/maps/place/Den",
            snapshot=snapshot,
            llm_fallback=lambda request: calls.append(request)
            or {"category_display_en": "Test Category"},
            llm_policy="on_quality_failure",
            llm_tasks=("display_translation",),
        )

        self.assertEqual(details.category_display_en, "Test Category")
        request = calls[-1]
        self.assertEqual(request.tasks, ["display_translation"])
        self.assertEqual(request.diagnostics.quality_flags, ["needs_category_display_en"])

    def test_build_place_details_marks_cached_repair_without_llm_use(self) -> None:
        details = _build_place_details_from_snapshot(
            "https://www.google.com/maps/place/Den",
            snapshot={
                "resolved_url": "https://www.google.com/maps/place/Den",
                "dom": {
                    "name": "Den",
                    "category": "Restaurant",
                    "rating": "4.4",
                    "review_count": "324",
                    "address": "Tokyo, Japan",
                },
                "preview": {},
            },
            llm_fallback=lambda _request: {
                "fields": {"website": "https://example.com"},
                "_repair_source": "cache",
            },
            llm_policy="always",
        )

        self.assertEqual(details.website, "https://example.com")
        self.assertIsNotNone(details.diagnostics)
        assert details.diagnostics is not None
        self.assertFalse(details.diagnostics.llm_used)
        self.assertEqual(details.diagnostics.repair_source, "cache")

    def test_build_place_details_backfills_from_search_result_card(self) -> None:
        details = _build_place_details_from_snapshot(
            "https://www.google.com/maps/search/?api=1&query=Lola+Underground",
            snapshot={
                "resolved_url": "https://www.google.com/maps/place/Pooles+Temple",
                "dom": {
                    "name": "Pooles Temple",
                    "category": "Event venue",
                    "rating": "4.6",
                    "address": "Hay St &, Cathedral Ave",
                },
                "search_result": {
                    "name": "Pooles Temple",
                    "rating": "4.6",
                    "review_count": "16",
                    "category": "Event venue",
                    "address": "Hay St &, Cathedral Ave",
                },
                "preview": {},
            },
            llm_fallback=None,
            llm_policy="never",
        )

        self.assertEqual(details.name, "Pooles Temple")
        self.assertEqual(details.review_count, 16)
        assert details.diagnostics is not None
        self.assertEqual(
            details.diagnostics.field_sources.get("review_count"),
            "search_result",
        )

    def test_build_place_details_prefers_preview_over_search_card_fallback(self) -> None:
        details = _build_place_details_from_snapshot(
            "https://www.google.com/maps/search/?api=1&query=Lola+Underground",
            snapshot={
                "resolved_url": "https://www.google.com/maps/place/Pooles+Temple",
                "dom": {"name": "Pooles Temple"},
                "search_result": {
                    "name": "Pooles Temple",
                    "category": "Event venue",
                    "review_count": "16",
                    "address": "Hay St &, Cathedral Ave",
                    "opened_from_search_result": True,
                },
                "preview": {
                    "category": "Bar",
                    "review_count": "32",
                    "address": (
                        "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, "
                        "2 Chome−3−18 建築家会館ＪＩＡ館"
                    ),
                },
            },
            llm_fallback=None,
            llm_policy="never",
        )

        self.assertEqual(details.category, "Bar")
        self.assertEqual(details.review_count, 32)
        self.assertEqual(
            details.address,
            "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
        )
        assert details.diagnostics is not None
        self.assertEqual(details.diagnostics.field_sources.get("category"), "preview")

    def test_build_place_details_backfills_preview_rating_summary_sources(self) -> None:
        preview = _extract_preview_place_enrichment(
            (_PREVIEW_FIXTURE_DIR / "rating_review_count.txt").read_text(encoding="utf-8")
        )
        details = _build_place_details_from_snapshot(
            "https://www.google.com/maps/place/Limited+Fixture",
            snapshot={
                "resolved_url": "https://www.google.com/maps/place/Limited+Fixture",
                "dom": {
                    "name": "Limited Fixture",
                    "limited_view": True,
                },
                "search_result": {},
                "preview": preview,
            },
            llm_fallback=None,
            llm_policy="never",
        )

        self.assertEqual(details.rating, 4.7)
        self.assertEqual(details.review_count, 832)
        assert details.diagnostics is not None
        self.assertEqual(details.diagnostics.field_sources.get("rating"), "preview")
        self.assertEqual(details.diagnostics.field_sources.get("review_count"), "preview")

    def test_build_place_details_keeps_dom_rating_summary_over_preview(self) -> None:
        details = _build_place_details_from_snapshot(
            "https://www.google.com/maps/place/Fixture",
            snapshot={
                "resolved_url": "https://www.google.com/maps/place/Fixture",
                "dom": {
                    "name": "Fixture",
                    "rating": "4.9",
                    "review_count": "1,234",
                },
                "search_result": {},
                "preview": {
                    "rating": 4.7,
                    "review_count": 832,
                },
            },
            llm_fallback=None,
            llm_policy="never",
        )

        self.assertEqual(details.rating, 4.9)
        self.assertEqual(details.review_count, 1234)
        assert details.diagnostics is not None
        self.assertEqual(details.diagnostics.field_sources.get("rating"), "dom")
        self.assertEqual(details.diagnostics.field_sources.get("review_count"), "dom")

    def test_build_place_details_prefers_selected_card_when_search_open_fails(self) -> None:
        details = _build_place_details_from_snapshot(
            "https://www.google.com/maps/search/?api=1&query=Lola+Underground",
            snapshot={
                "resolved_url": "https://www.google.com/maps/search/?api=1&query=Lola+Underground",
                "dom": {
                    "name": "Wrong Visible Result",
                    "category": "Restaurant",
                    "review_count": "999",
                    "address": "Wrong Address",
                },
                "search_result": {
                    "name": "Pooles Temple",
                    "category": "Event venue",
                    "review_count": "16",
                    "address": "Hay St &, Cathedral Ave",
                },
                "preview": {},
            },
            llm_fallback=None,
            llm_policy="never",
        )

        self.assertEqual(details.name, "Pooles Temple")
        self.assertEqual(details.category, "Event venue")
        self.assertEqual(details.review_count, 16)
        self.assertEqual(details.address, "Hay St &, Cathedral Ave")
        assert details.diagnostics is not None
        self.assertEqual(details.diagnostics.field_sources.get("name"), "search_result")

    def test_build_place_details_uses_structured_dom_fields(self) -> None:
        description = (
            "Seasonal menus of strikingly presented contemporary dishes, with wine "
            "pairings, in a stylish space."
        )
        details = _build_place_details(
            "https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            snapshot={
                "name": "Den",
                "secondary_name": "傳",
                "rating": "4.4",
                "review_count": "324",
                "category": "Japanese restaurant",
                "address": (
                    "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 "
                    "建築家会館ＪＩＡ館"
                ),
                "located_in": "Floor 1 · 日本建築家協会",
                "status": "Closed · Opens 6 PM",
                "website": "http://www.jimbochoden.com/",
                "phone": "+81 3-6455-5433",
                "plus_code": "MPF7+73 Shibuya, Tokyo, Japan",
                "description": description,
                "limited_view": True,
                "body_text": "\n".join(
                    [
                        "Den",
                        "傳",
                        "4.4",
                        "Japanese restaurant·",
                        description,
                    ]
                ),
            },
        )

        self.assertEqual(details.name, "Den")
        self.assertEqual(details.secondary_name, "傳")
        self.assertEqual(details.category, "Japanese restaurant")
        self.assertEqual(details.rating, 4.4)
        self.assertEqual(details.review_count, 324)
        self.assertEqual(details.description, description)
        self.assertEqual(details.lat, 35.6731762)
        self.assertEqual(details.lng, 139.7127216)
        self.assertTrue(details.limited_view)

    def test_build_place_details_does_not_promote_body_text_to_description(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den",
            snapshot={
                "name": "Den",
                "category": "Japanese restaurant",
                "body_text": "\n".join(
                    [
                        "Share",
                        (
                            "Seasonal menus of strikingly presented contemporary dishes, "
                            "with wine pairings, in a stylish space."
                        ),
                    ]
                ),
            },
        )

        self.assertIsNone(details.description)

    def test_build_place_details_does_not_promote_share_adjacent_panel_text(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/CANNES+sign",
            resolved_url="https://www.google.com/maps/place/CANNES+sign",
            snapshot={
                "name": "CANNES sign",
                "category": "Tourist attraction",
                "panel_text": "\n".join(
                    [
                        "Share",
                        (
                            "We took a very long cruise last summer from Venice to Portugal. "
                            "One stop was Cannes, and this sign was on the walking tour."
                        ),
                    ]
                ),
            },
        )

        self.assertIsNone(details.description)

    def test_build_place_details_preserves_zero_coordinates(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Null+Island",
            resolved_url="https://www.google.com/maps/place/Null+Island",
            snapshot={
                "name": "Null Island",
                "category": "Tourist attraction",
                "lat": 0.0,
                "lng": 0.0,
                "body_text": "Null Island\nTourist attraction",
            },
        )

        self.assertEqual(details.lat, 0.0)
        self.assertEqual(details.lng, 0.0)

    def test_build_place_details_rejects_fixaddress_url_addresses(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Nizami+Street",
            resolved_url="https://www.google.com/maps/place/Nizami+Street",
            snapshot={
                "name": "Nizami Street",
                "category": "Transportation",
                "address": (
                    "Address https://www.google.com/local/place/rap/fixaddress?"
                    "g2lb=72971417,73155522,100805691&hl=en-CA&gl=ca"
                ),
                "body_text": "Nizami Street\nTransportation",
            },
        )

        self.assertIsNone(details.address)

    def test_build_place_details_rejects_relative_search_url_addresses(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/search/?api=1&query=Sendlinger+Tor",
            resolved_url="https://www.google.com/maps/search/?api=1&query=Sendlinger+Tor",
            snapshot={
                "name": "Sendlinger-Tor-Platz 1",
                "category": "Kebab shop",
                "rating": "4.4",
                "review_count": "8862",
                "address": (
                    "/search?sca_esv=6ce6d4092249d8a7&authuser=0&hl=en&gl=tw"
                    "&output=search&tbm=map&q=Haferkater,+Sendlinger+Tor,+M%C3%BCnchen"
                    "&ludocid=16588126363784805389"
                ),
                "body_text": "Sendlinger-Tor-Platz 1\nKebab shop",
            },
        )

        self.assertIsNone(details.address)

    def test_build_place_details_rejects_travel_product_address(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/search/?api=1&query=Nalati+Grassland",
            resolved_url="https://www.google.com/maps/search/?api=1&query=Nalati+Grassland",
            snapshot={
                "name": "Nalati Grassland",
                "category": "National park",
                "rating": "4.5",
                "review_count": "133",
                "address": "8-Day Ili Pastoral RV Adventure (Duku Highway Crossing)",
                "body_text": "Nalati Grassland\nNational park",
            },
        )

        self.assertIsNone(details.address)

    def test_build_place_details_accepts_locality_only_address(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Nizami+Street",
            resolved_url="https://www.google.com/maps/place/Nizami+Street",
            snapshot={
                "name": "Nizami St",
                "category": "Notable street",
                "address": "Baku, Azerbaijan",
                "body_text": "Nizami St\n4.7\n1,842 reviews\nNotable street",
            },
        )

        self.assertEqual(details.address, "Baku, Azerbaijan")

    def test_build_place_details_adds_english_display_address_for_known_non_latin_parts(
        self,
    ) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Capella",
            resolved_url="https://www.google.com/maps/place/Capella",
            snapshot={
                "name": "Capella Singapore",
                "address": "1 The Knolls, シンガポール 098297",
                "body_text": "Capella Singapore\nHotel",
            },
        )

        self.assertEqual(details.address, "1 The Knolls, シンガポール 098297")
        self.assertEqual(details.address_display_en, "1 The Knolls, Singapore 098297")
        self.assertEqual(details.address_display_en_source, "translation_memory")
        self.assertEqual(details.address_display_en_confidence, "high")

    def test_build_place_details_does_not_duplicate_latin_address_display(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/BunBo",
            resolved_url="https://www.google.com/maps/place/BunBo",
            snapshot={
                "name": "Bun Bo",
                "address": "73-75 Hàng Điếu, Phố cổ Hà Nội, Hoàn Kiếm, Hà Nội, Vietnam",
                "body_text": "Bun Bo\nNoodle shop",
            },
        )

        self.assertIsNone(details.address_display_en)

    def test_build_place_details_adds_english_display_category_for_known_non_latin_category(
        self,
    ) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Fiamma",
            resolved_url="https://www.google.com/maps/place/Fiamma",
            snapshot={
                "name": "Fiamma",
                "category": "イタリア料理店",
                "address": "1 The Knolls, Singapore 098297",
                "body_text": "Fiamma\nイタリア料理店",
            },
        )

        self.assertEqual(details.category, "イタリア料理店")
        self.assertEqual(details.category_display_en, "Italian restaurant")
        self.assertEqual(details.category_display_en_source, "translation_memory")
        self.assertEqual(details.category_display_en_confidence, "high")

    def test_build_place_details_translates_known_non_latin_address_components(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Bada",
            resolved_url="https://www.google.com/maps/place/Bada",
            snapshot={
                "name": "Bada Sikdang",
                "address": "245 2층 Itaewon-ro, 한남동 Yongsan District, Seoul, South Korea",
                "body_text": "Bada Sikdang\nRestaurant",
            },
        )

        self.assertEqual(
            details.address_display_en,
            "245 2F Itaewon-ro, Hannam-dong Yongsan District, Seoul, South Korea",
        )

    def test_build_place_details_translates_basement_address_marker_in_place(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Analogue",
            resolved_url="https://www.google.com/maps/place/Analogue",
            snapshot={
                "name": "Analogue",
                "address": "Hong Kong, Central, Lyndhurst Terrace, 48地下",
                "body_text": "Analogue\nCocktail bar",
            },
        )

        self.assertEqual(
            details.address_display_en,
            "Hong Kong, Central, Lyndhurst Terrace, Basement #48",
        )

    def test_extract_address_from_lines_supports_non_japanese_addresses(self) -> None:
        self.assertEqual(
            _extract_address_from_lines(
                [
                    "Coffee shop",
                    "Open ⋅ Closes 8 PM",
                    "1600 Amphitheatre Parkway, Mountain View, CA 94043",
                ]
            ),
            "1600 Amphitheatre Parkway, Mountain View, CA 94043",
        )
        self.assertEqual(
            _extract_address_from_lines(
                [
                    "Noodle shop",
                    "73-75 Hàng Điếu, Phố cổ Hà Nội, Hoàn Kiếm, Hà Nội, Vietnam",
                ]
            ),
            "73-75 Hàng Điếu, Phố cổ Hà Nội, Hoàn Kiếm, Hà Nội, Vietnam",
        )

    def test_clean_name_text_preserves_names_that_start_with_open_or_closed(self) -> None:
        self.assertEqual(_clean_name_text("Open Kitchen"), "Open Kitchen")
        self.assertEqual(_clean_name_text("Closed Loop Coffee"), "Closed Loop Coffee")
        self.assertEqual(_clean_name_text("Open Now Cafe"), "Open Now Cafe")
        self.assertIsNone(_clean_name_text("Open ⋅ Closes 8 PM"))
        self.assertIsNone(_clean_name_text("Open now"))

    def test_clean_category_text_rejects_search_result_labels(self) -> None:
        self.assertIsNone(_clean_category_text("share"))
        self.assertIsNone(_clean_category_text("結果"))
        self.assertEqual(_clean_category_text("Japanese restaurant"), "Japanese restaurant")

    def test_clean_name_text_rejects_ui_action_labels(self) -> None:
        for value in ("Call", "Directions", "Save", "Saved", "Share", "Website"):
            with self.subTest(value=value):
                self.assertIsNone(_clean_name_text(value))

    def test_extract_preview_place_enrichment_backfills_core_fields(self) -> None:
        payload_data = [
            None,
            None,
            None,
            None,
            None,
            None,
            [
                "token",
                "meta",
                [
                    "Japan",
                    "〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18",
                    "建築家会館ＪＩＡ館",
                ],
                None,
                [None, None, None, None, None, None, None, 4.4],
                None,
                None,
                ["http://www.jimbochoden.com/", "jimbochoden.com"],
                None,
                [None, None, 35.6731762, 139.7127216],
                "0x60188c981788132b:0x6ef132909b155a88",
                "Den",
                None,
                ["Japanese restaurant", "Kaiseki restaurant", "Restaurant"],
                "2 Chome Jingumae",
                None,
                None,
                None,
                "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 Den, 建築家会館ＪＩＡ館",
                None,
                None,
                None,
                [
                    [
                        "0x60188c981788132b:0x6ef132909b155a88",
                        None,
                        None,
                        "/m/0131whcb",
                        "ChIJ8T36HxCLGGARvpARPDyaKLA",
                    ]
                ],
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                ["Modern setting for fine dining menus", "SearchResult.TYPE_JAPANESE_RESTAURANT"],
                "/g/11c5s9cpnk",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [["+81 3-6455-5433", [["03-6455-5433", 1], ["+81 3-6455-5433", 2]]]],
                None,
                None,
                None,
                None,
                [
                    [
                        [
                            "2 Chome Jingumae",
                            "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                            "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                            "Shibuya",
                            "150-0001",
                            "Tokyo",
                            "JP",
                            ["Floor 1"],
                        ],
                        ["0ahUKE", "8Q7XMPF7+73", ["MPF7+73 Shibuya, Tokyo, Japan"], 3],
                    ]
                ],
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [[None, None, 35.6731762, 139.7127216]],
            ],
        ]
        payload = ")]}'\n" + json.dumps(payload_data, ensure_ascii=False)
        enrichment = _extract_preview_place_enrichment(payload)

        self.assertEqual(enrichment["website"], "http://www.jimbochoden.com/")
        self.assertEqual(enrichment["phone"], "+81 3-6455-5433")
        self.assertEqual(enrichment["plus_code"], "MPF7+73 Shibuya, Tokyo, Japan")
        self.assertEqual(
            enrichment["address_parts"],
            [
                "2 Chome Jingumae",
                "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                "Shibuya",
                "150-0001",
                "Tokyo",
                "JP",
                ["Floor 1"],
            ],
        )
        self.assertEqual(
            enrichment["address"],
            "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 Den, 建築家会館ＪＩＡ館",
        )
        self.assertEqual(enrichment["category"], "Japanese restaurant")
        self.assertEqual(enrichment["description"], "Modern setting for fine dining menus")
        self.assertEqual(enrichment["lat"], 35.6731762)
        self.assertEqual(enrichment["lng"], 139.7127216)
        self.assertEqual(enrichment["google_place_id"], "ChIJ8T36HxCLGGARvpARPDyaKLA")

    def test_extract_preview_place_enrichment_reads_rating_summary_fixture(self) -> None:
        enrichment = _extract_preview_place_enrichment(
            (_PREVIEW_FIXTURE_DIR / "rating_review_count.txt").read_text(encoding="utf-8")
        )

        self.assertEqual(enrichment["rating"], 4.7)
        self.assertEqual(enrichment["review_count"], 832)

    def test_extract_preview_place_enrichment_accepts_year_range_review_count(self) -> None:
        payload = ")]}'\n" + json.dumps([["rating-summary", 4.7, 2000]])
        enrichment = _extract_preview_place_enrichment(payload)

        self.assertEqual(enrichment["rating"], 4.7)
        self.assertEqual(enrichment["review_count"], 2000)

    def test_extract_preview_place_enrichment_rejects_ambiguous_rating_counts(self) -> None:
        enrichment = _extract_preview_place_enrichment(
            (_PREVIEW_FIXTURE_DIR / "ambiguous_rating_review_count.txt").read_text(
                encoding="utf-8"
            )
        )

        self.assertNotIn("rating", enrichment)
        self.assertNotIn("review_count", enrichment)

    def test_extract_preview_description_preserves_text_starting_with_open(self) -> None:
        description = _extract_preview_description(
            [
                "Open fire cooking over binchotan.",
                "Open ⋅ Closes 10 PM",
                "SearchResult.TYPE_RESTAURANT",
            ]
        )

        self.assertEqual(
            description,
            "Open fire cooking over binchotan.",
        )

    def test_extract_preview_description_preserves_open_now_prose(self) -> None:
        description = _extract_preview_description(
            [
                "Open now for lunch and dinner service.",
                "Open now ⋅ Closes 10 PM",
            ]
        )

        self.assertEqual(description, "Open now for lunch and dinner service.")

    def test_clean_description_text_rejects_sponsored_label(self) -> None:
        self.assertIsNone(_clean_description_text("Sponsored"))

    def test_clean_description_text_rejects_place_tab_strip(self) -> None:
        self.assertIsNone(_clean_description_text("Overview Reviews About"))

    def test_clean_description_text_keeps_structured_marketing_summary(self) -> None:
        self.assertEqual(
            _clean_description_text(
                "Modern restaurant serving delicious food for lunch and dinner in a "
                "friendly, relaxed setting."
            ),
            (
                "Modern restaurant serving delicious food for lunch and dinner in a "
                "friendly, relaxed setting."
            ),
        )
        self.assertEqual(
            _clean_description_text(
                "Casual cafe serving coffee all day in a sunny room near downtown."
            ),
            "Casual cafe serving coffee all day in a sunny room near downtown.",
        )
        self.assertEqual(
            _clean_description_text(
                "Museum exhibits artifacts found across the US, with archival photos, "
                "oral histories, rotating galleries, and educational programs."
            ),
            (
                "Museum exhibits artifacts found across the US, with archival photos, "
                "oral histories, rotating galleries, and educational programs."
            ),
        )
        self.assertEqual(
            _clean_description_text(
                "Instagram-worthy dessert cafe serving colorful macarons, "
                "house-roasted coffee, brunch plates, and seasonal pastries daily."
            ),
            (
                "Instagram-worthy dessert cafe serving colorful macarons, "
                "house-roasted coffee, brunch plates, and seasonal pastries daily."
            ),
        )
        self.assertEqual(
            _clean_description_text(
                "A must visit destination for families, with interactive science "
                "exhibits, live demonstrations, and workshops."
            ),
            (
                "A must visit destination for families, with interactive science "
                "exhibits, live demonstrations, and workshops."
            ),
        )

    def test_clean_description_text_rejects_first_person_review_prose(self) -> None:
        self.assertIsNone(
            _clean_description_text(
                "This garden is absolutely beautiful and highly fragrant. "
                "We visited in mid-April, and many of the roses were in bloom."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "Amazing quality loved the Kusama exhibit; I’ve seen her work elsewhere, "
                "but the way it’s displayed here takes it to the next level."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "There are many Sichuan Opera Theatre Houses but so far this is the most "
                "professional show I have attended. Was a bit skeptical at first."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "Beautiful view from the cellar and there’s access to the gallery below. "
                "We arrived late so couldn’t do the tasting."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "After six p.m. You can reserve this onsen privately for one hour at a time. "
                "It cost ¥2000 yen to reserve it. I think during normal hours it is cheaper."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "Huge Waves - they were really fun but you should not lie in the front rows "
                "because an unexpectedly large wave hit our towel."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "This pond is a scenic and tranquil spot, just a five-minute bike "
                "ride from the main street in Karuizawa. "
                "It offers a peaceful retreat with serene water and lush surroundings. "
                "The area is equipped with toilets and is perfect for taking your "
                "children for a relaxing outing. "
                "A wonderful place to unwind and enjoy nature. Highly recommended for families."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "Just an overrated place. Tiktok and instagram made it famous but I didn’t "
                "feel like it is a must visit spot."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "Best ramen we've ever had. It helps that you get to make it yourself "
                "(the noodles at least). Everything tastes better when you do it yourself! "
                "Date day for a Saturday morning class. Great experience overall."
            )
        )
        self.assertIsNone(
            _clean_description_text(
                "That’s gotta be one of the best hot chocolate drink I’ve tasted in my life! "
                "The long wait was definitely worth it. There was quite a line before we got "
                "a seat, but boy was it worth every minute. Definitely recommend this place."
            )
        )
        for review_description in (
            "It was my first attempt to eat mukhata. The food was so delicious and "
            "the clerks were very kind.",
            "Best place to stay in Hanoi. I’d just finished Ha Giang loop and "
            "needed a place to rest.",
            "The katsu burger!!! Omfg!!! So yummy and the young man with blonde "
            "curly hair from England.",
            "What a great hotel! The rooms were huge and have balconies with a seating area.",
            "Had a great time here with my friends! The barkeeper made us feel "
            "welcomed and we had a lot of fun.",
            "The lady was just so so lovely. My feet are just gorgeous. "
            "Would recommend to everyone.",
            "My stay in Alila was wonderful. Special shout out to the staff for "
            "making it memorable.",
            "The hotel have a sense of peace and tranquility once step in. "
            "The personal service was delicate.",
            "The staffs also offered great recommendation for drinks based on your preference.",
            "Directions Save Nearby Send to phone Share About this data "
            "Get the most out of Google Maps Sign in",
            "\ue52e Directions \ue866 Save \uf05f Nearby \ue702 Send to phone \ue80d Share",
        ):
            self.assertIsNone(_clean_description_text(review_description))

    def test_clean_description_text_keeps_first_person_business_summary(self) -> None:
        self.assertEqual(
            _clean_description_text(
                "We serve seasonal Italian dishes and cocktails in a relaxed dining room."
            ),
            "We serve seasonal Italian dishes and cocktails in a relaxed dining room.",
        )

    def test_clean_description_text_keeps_first_person_summary_with_word_prefixes(self) -> None:
        self.assertEqual(
            _clean_description_text(
                "We are a family-owned restaurant founded in 1978, serving regional dishes "
                "with local ingredients and warm hospitality."
            ),
            (
                "We are a family-owned restaurant founded in 1978, serving regional dishes "
                "with local ingredients and warm hospitality."
            ),
        )

    def test_clean_description_text_keeps_owner_style_marketing_summary(self) -> None:
        self.assertEqual(
            _clean_description_text(
                "Our team provides thoughtful care and we strive to make every visit feel easy."
            ),
            "Our team provides thoughtful care and we strive to make every visit feel easy.",
        )

    def test_clean_description_text_keeps_contact_us_business_summary(self) -> None:
        self.assertEqual(
            _clean_description_text(
                "Contact us for reservations, catering, private events, seasonal menus, "
                "and group dining in our relaxed neighborhood restaurant."
            ),
            (
                "Contact us for reservations, catering, private events, seasonal menus, "
                "and group dining in our relaxed neighborhood restaurant."
            ),
        )

    def test_clean_description_text_keeps_first_person_words_containing_markers(self) -> None:
        self.assertEqual(
            _clean_description_text(
                "Our costume museum preserves regional theatre clothing and performance "
                "objects with guided exhibits, public workshops, archival research, "
                "and rotating displays for visitors."
            ),
            (
                "Our costume museum preserves regional theatre clothing and performance "
                "objects with guided exhibits, public workshops, archival research, "
                "and rotating displays for visitors."
            ),
        )

    def test_clean_description_text_rejects_option_only_amenity_labels(self) -> None:
        self.assertIsNone(_clean_description_text("· \ue5ca Dogs allowed \ue5cc"))
        self.assertIsNone(_clean_description_text("Onsite services"))
        self.assertIsNone(_clean_description_text("Restaurant · Dine-in · Takeout"))
        self.assertEqual(
            _clean_description_text("Dine-in · Takeout · Cozy bistro with seasonal plates."),
            "Cozy bistro with seasonal plates",
        )

    def test_clean_description_text_rejects_policy_prompt_without_period(self) -> None:
        self.assertIsNone(
            _clean_description_text(
                "Our policies do not permit contributions to this type of place"
            )
        )

    def test_clean_description_text_strips_trailing_google_maps_glyph(self) -> None:
        self.assertEqual(
            _clean_description_text(
                "Large museum showcasing ancient pottery, Buddhist art & exhibits. \ue5cc"
            ),
            "Large museum showcasing ancient pottery, Buddhist art & exhibits.",
        )

    def test_extract_preview_place_enrichment_rejects_invalid_address_parts(self) -> None:
        payload_data = [
            [
                [
                    [
                        "2 Chome Jingumae",
                        "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                        "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                        "Shibuya",
                        "150-0001",
                        "Tokyo",
                        "JP",
                        ["Floor 1", 3],
                    ],
                    ["0ahUKE", "8Q7XMPF7+73", ["MPF7+73 Shibuya, Tokyo, Japan"], 3],
                ]
            ]
        ]
        payload = ")]}'\n" + json.dumps(payload_data, ensure_ascii=False)
        enrichment = _extract_preview_place_enrichment(payload)

        self.assertNotIn("address_parts", enrichment)

    def test_extract_preview_coordinates_ignores_short_integer_pairs(self) -> None:
        root = [
            [1, 2],
            ["noise", [None, None, 35.6731762, 139.7127216]],
        ]

        self.assertEqual(
            _extract_preview_coordinates(root),
            (35.6731762, 139.7127216),
        )

    def test_extract_preview_phone_rejects_cid_like_values(self) -> None:
        self.assertEqual(
            _extract_preview_phone(["5180951040094558101", "1776609428996", "+33 1 42 00 00 00"]),
            "+33 1 42 00 00 00",
        )

    def test_extract_preview_address_rejects_map_urls_and_prefers_postal_address(self) -> None:
        self.assertEqual(
            _extract_preview_address(
                [
                    "https://www.google.com/maps/place/Test/@48.8814703,2.340862,17z/data=!3m1!4b1",
                    "26-28 Cotham Rd, Kew VIC 3101, Australia",
                ]
            ),
            "26-28 Cotham Rd, Kew VIC 3101, Australia",
        )
        self.assertEqual(
            _extract_preview_address(
                [
                    "/maps/place/Foo · 123 Main St, Springfield, IL 62701",
                ]
            ),
            "123 Main St, Springfield, IL 62701",
        )

    def test_extract_preview_address_uses_cleaned_segment_from_compound_value(self) -> None:
        self.assertEqual(
            _extract_preview_address(
                [
                    "Cafe · 1600 Amphitheatre Parkway, Mountain View, CA 94043",
                    "Cafe",
                ]
            ),
            "1600 Amphitheatre Parkway, Mountain View, CA 94043",
        )
        self.assertEqual(
            _extract_preview_address(["8-Day Ili Pastoral RV Adventure · 123 Main St"]),
            "123 Main St",
        )
        self.assertEqual(
            _extract_preview_address(["8 Day Street, Boston, MA 02111"]),
            "8 Day Street, Boston, MA 02111",
        )

    def test_extract_preview_address_rejects_review_snippets(self) -> None:
        self.assertIsNone(
            _extract_preview_address(
                [
                    (
                        "The best takeout or eat in I recommend this place. We dropped in "
                        "5 minutes "
                        "before closing time and the owner took the initiative to cook us More"
                    ),
                    (
                        "Fascinating 2 hours session introducing Tonga culture and history, "
                        "way of life, using plants as herbal cues, medicine and food, "
                        "traditional weapons and utensils, "
                        "and more."
                    ),
                    (
                        "The nuggets are massive, good size burgers and probably the best "
                        "for value in town"
                    ),
                    (
                        "This place has great food, good service, friendly owner, and "
                        "delicious burgers"
                    ),
                    "good food, friendly owner",
                ]
            )
        )

    def test_extract_preview_address_rejects_service_option_lists(self) -> None:
        self.assertIsNone(_extract_preview_address(["Dine-in, Takeout, Delivery"]))
        self.assertIsNone(_extract_preview_address(["Dine-in, Takeout, Delivery."]))
        self.assertIsNone(_extract_preview_address(["Dine-in, Takeout, Reservations"]))
        self.assertIsNone(_extract_preview_address(["Takeout, Delivery, Curbside pickup"]))
        self.assertIsNone(
            _extract_preview_address(["Wheelchair accessible entrance, Dine-in, Takeout"])
        )
        self.assertIsNone(_extract_preview_address(["Museum, Art gallery"]))
        self.assertIsNone(_extract_preview_address(["Friendly staff, good coffee."]))
        self.assertIsNone(_extract_preview_address(["Great food at St. James, highly recommend."]))

    def test_extract_preview_address_keeps_locality_abbreviations(self) -> None:
        self.assertEqual(_extract_preview_address(["St. Louis, MO"]), "St. Louis, MO")
        self.assertEqual(_extract_preview_address(["St. John's, NL"]), "St. John's, NL")
        self.assertEqual(_extract_preview_address(["Washington, D.C."]), "Washington, D.C.")
        self.assertEqual(_extract_preview_address(["Bar, Montenegro"]), "Bar, Montenegro")
        self.assertEqual(_extract_preview_address(["Bar, Bar, Montenegro"]), "Bar, Bar, Montenegro")
        self.assertEqual(
            _extract_preview_address(["Friendly, Coffee Springs"]),
            "Friendly, Coffee Springs",
        )

    def test_extract_preview_address_keeps_addresses_with_prose_words(self) -> None:
        self.assertEqual(
            _extract_preview_address(
                [
                    "Good Burger, 1 Main St, New York, NY 10001",
                    (
                        "The nuggets are massive, good size burgers and probably the best "
                        "for value in town"
                    ),
                ]
            ),
            "Good Burger, 1 Main St, New York, NY 10001",
        )
        self.assertEqual(
            _extract_preview_address(["Session Road, Baguio, Benguet 2600, Philippines"]),
            "Session Road, Baguio, Benguet 2600, Philippines",
        )
        self.assertEqual(
            _extract_preview_address(["Best Avenue, Oakland, CA 94611"]),
            "Best Avenue, Oakland, CA 94611",
        )
        self.assertEqual(
            _extract_preview_address(["Dinner Plain, Victoria, Australia"]),
            "Dinner Plain, Victoria, Australia",
        )
        self.assertEqual(
            _extract_preview_address(["Port of Spain, Trinidad & Tobago"]),
            "Port of Spain, Trinidad & Tobago",
        )

    def test_normalize_phone_candidate_accepts_long_unformatted_international_numbers(self) -> None:
        self.assertEqual(_normalize_phone_candidate("442071838750"), "442071838750")

    def test_normalize_phone_candidate_rejects_numeric_preview_entity_ids(self) -> None:
        self.assertIsNone(_normalize_phone_candidate("1777026232472"))

    def test_build_place_details_ignores_placeholder_name_invalid_phone_and_status_description(
        self,
    ) -> None:
        details = _build_place_details(
            "https://maps.google.com/?cid=5180951040094558101",
            resolved_url="https://www.google.com/maps/place//@48.8814703,2.340862,17z/data=!3m1!4b1",
            snapshot={
                "name": "",
                "secondary_name": "",
                "phone": "5180951040094558101",
                "status": "営業時間外 · 営業開始: 18:00\uFF08火\uFF09",
                "description": "営業時間外 · 営業開始: 18:00\uFF08火\uFF09",
                "lat": 48.8814703,
                "lng": 2.340862,
                "body_text": "\n".join(["", "", "営業時間外 · 営業開始: 18:00\uFF08火\uFF09"]),
            },
        )

        self.assertIsNone(details.name)
        self.assertIsNone(details.secondary_name)
        self.assertIsNone(details.phone)
        self.assertIsNone(details.description)

    def test_build_place_details_rejects_placeholder_description_direct_value(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Bianchetto",
            resolved_url="https://www.google.com/maps/place/Bianchetto",
            snapshot={
                "name": "Bianchetto",
                "description": "Share",
                "body_text": "Bianchetto\nRestaurant",
            },
        )

        self.assertIsNone(details.description)

    def test_build_place_details_rejects_icon_only_description_direct_value(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Bianchetto",
            resolved_url="https://www.google.com/maps/place/Bianchetto",
            snapshot={
                "name": "Bianchetto",
                "description": "\uea74",
                "body_text": "Bianchetto\nRestaurant",
            },
        )

        self.assertIsNone(details.description)

    def test_build_place_details_rejects_locality_description_direct_value(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Ad+Astra",
            resolved_url="https://www.google.com/maps/place/Ad+Astra",
            snapshot={
                "name": "Ad Astra",
                "description": "Taipei City, Zhongshan District",
                "body_text": "Ad Astra\nRestaurant",
            },
        )

        self.assertIsNone(details.description)

    def test_build_place_details_rejects_moderation_prompt_description(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/La+Quintessence",
            resolved_url="https://www.google.com/maps/place/La+Quintessence",
            snapshot={
                "name": "La Quintessence Cannes",
                "description": (
                    "Mark as temporarily closed, or remove this place; "
                    "report a legal problem"
                ),
                "body_text": "La Quintessence Cannes\nRestaurant",
            },
        )

        self.assertIsNone(details.description)

    def test_build_place_details_rejects_service_option_only_description(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/La+Prosciutteria",
            resolved_url="https://www.google.com/maps/place/La+Prosciutteria",
            snapshot={
                "name": "La Prosciutteria Bologna",
                "description": (
                    "· \ue5ca Dine-in · \ue5ca Curbside pickup · "
                    "\ue5ca Delivery \ue5cc"
                ),
                "body_text": "La Prosciutteria Bologna\nRestaurant",
            },
        )

        self.assertIsNone(details.description)

    def test_build_place_details_strips_service_option_suffix_from_description(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Rare+Steakhouse",
            resolved_url="https://www.google.com/maps/place/Rare+Steakhouse",
            snapshot={
                "name": "Rare Steakhouse",
                "description": (
                    "Polished white-tablecloth operation dishing up traditional & "
                    "Japanese-style cuts, plus cocktails. · \ue5ca Dine-in · "
                    "\ue5ca Takeout · \ue5cd Delivery \ue5cc"
                ),
                "body_text": "Rare Steakhouse\nSteak house",
            },
        )

        self.assertEqual(
            details.description,
            (
                "Polished white-tablecloth operation dishing up "
                "traditional & Japanese-style cuts, plus cocktails"
            ),
        )

    def test_build_place_details_rejects_first_person_review_description(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/CANNES+sign",
            resolved_url="https://www.google.com/maps/place/CANNES+sign",
            snapshot={
                "name": "CANNES sign",
                "description": (
                    "We took a very long cruise last summer from Venice to Portugal. On NCL. "
                    "One stop was Cannes. We got off the ship and took a self guided walking "
                    "tour using google maps and my research."
                ),
                "body_text": "CANNES sign\nTourist attraction",
            },
        )

        self.assertIsNone(details.description)

    def test_build_place_details_rejects_search_results_labels_and_rating_categories(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/search/?api=1&query=Bianchetto",
            resolved_url="https://www.google.com/maps/search/?api=1&query=Bianchetto",
            snapshot={
                "name": "結果",
                "category": "5.0(8)",
                "address": "バー · 26-28 Cotham Rd",
                "body_text": "\n".join(["結果", "5.0(8)", "バー · 26-28 Cotham Rd"]),
            },
        )

        self.assertIsNone(details.name)

    def test_build_place_details_rejects_sponsored_name_label(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/search/?api=1&query=Nakameguro+Iguchi",
            resolved_url="https://www.google.com/maps/search/?api=1&query=Nakameguro+Iguchi",
            snapshot={
                "name": "Sponsored \ue5d4",
                "category": "Restaurant",
                "rating": "3.7",
                "review_count": "186",
                "body_text": "\n".join(["Sponsored \ue5d4", "Restaurant", "3.7"]),
            },
        )

        self.assertIsNone(details.name)

    def test_search_result_snapshot_preserves_panel_text_for_fallback_parsing(self) -> None:
        snapshot = _search_result_snapshot(
            {
                "href": "https://www.google.com/maps/place/Open+Kitchen",
                "name": "Open Kitchen",
                "panel_text": "\n".join(
                    [
                        "Open Kitchen",
                        "Restaurant · MPF7+73 Shibuya, Tokyo, Japan",
                    ]
                ),
            }
        )

        details = _build_place_details(
            "https://www.google.com/maps/place/Open+Kitchen",
            resolved_url="https://www.google.com/maps/place/Open+Kitchen",
            snapshot=snapshot,
        )

        self.assertEqual(
            snapshot["panel_text"],
            "Open Kitchen\nRestaurant · MPF7+73 Shibuya, Tokyo, Japan",
        )
        self.assertEqual(details.category, "Restaurant")
        self.assertEqual(details.plus_code, "MPF7+73 Shibuya, Tokyo, Japan")

    def test_build_place_details_rejects_ui_action_fallback_name_and_marks_diagnostics(
        self,
    ) -> None:
        snapshot = {
            "category": "Restaurant",
            "rating": "4.5",
            "review_count": "100",
            "address": "Taipei City, Taiwan",
            "body_text": "\n".join(["Share", "Saved", "Directions", "Restaurant"]),
        }
        details = _build_place_details(
            "https://www.google.com/maps/place/Share",
            resolved_url="https://www.google.com/maps/place/Share",
            snapshot=snapshot,
        )
        evidence = _build_place_llm_evidence(snapshot)
        details.diagnostics = _build_place_diagnostics(
            details,
            snapshot,
            evidence_hash=_hash_evidence(evidence),
        )

        self.assertIsNone(details.name)
        self.assertIsNotNone(details.diagnostics)
        assert details.diagnostics is not None
        self.assertIn("missing_name", details.diagnostics.quality_flags)

    def test_build_place_details_rejects_structured_name_that_matches_action_label(
        self,
    ) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Pooles+Temple",
            resolved_url="https://www.google.com/maps/place/Pooles+Temple",
            snapshot={
                "name": "Share",
                "category": "Event venue",
                "panel_text": "\n".join(
                    ["Share", "Saved", "Directions", "Pooles Temple", "Event venue"]
                ),
            },
        )

        self.assertEqual(details.name, "Pooles Temple")

    def test_build_place_details_prefers_structured_title_over_action_lines(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Taipei+Zoo",
            resolved_url="https://www.google.com/maps/place/Taipei+Zoo",
            snapshot={
                "name": "Taipei Zoo",
                "category": "Zoo",
                "body_text": "\n".join(["Share", "Save", "Directions", "Taipei Zoo", "Zoo"]),
            },
        )

        self.assertEqual(details.name, "Taipei Zoo")

    def test_build_place_details_preserves_numeric_only_name(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/404",
            resolved_url="https://www.google.com/maps/place/404",
            snapshot={
                "name": "404",
                "body_text": "\n".join(["404", "Bar"]),
            },
        )

        self.assertEqual(details.name, "404")

    def test_build_place_details_preserves_slashed_numeric_name(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/24-7",
            resolved_url="https://www.google.com/maps/place/24-7",
            snapshot={
                "name": "24/7",
                "body_text": "\n".join(["24/7", "Diner"]),
            },
        )

        self.assertEqual(details.name, "24/7")

    def test_build_place_details_preserves_open_prefixed_name_and_description(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Open+Kitchen",
            resolved_url="https://www.google.com/maps/place/Open+Kitchen",
            snapshot={
                "name": "Open Kitchen",
                "description": "Open fire cooking in a bright room.",
                "body_text": "\n".join(
                    [
                        "Open Kitchen",
                        "Restaurant",
                        "Open fire cooking in a bright room.",
                    ]
                ),
            },
        )

        self.assertEqual(details.name, "Open Kitchen")
        self.assertEqual(details.description, "Open fire cooking in a bright room.")
        self.assertIsNone(details.status)

    def test_build_place_details_preserves_photo_url(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Open+Kitchen",
            resolved_url="https://www.google.com/maps/place/Open+Kitchen",
            snapshot={
                "name": "Open Kitchen",
                "main_photo_url": "https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
                "photo_url": "https://lh3.googleusercontent.com/p/example=s680-w680-h510",
                "body_text": "Open Kitchen",
            },
        )

        self.assertEqual(
            details.main_photo_url,
            "https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
        )
        self.assertEqual(
            details.photo_url,
            "https://lh3.googleusercontent.com/p/example=s680-w680-h510",
        )

    def test_build_place_details_preserves_reservation_links(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Open+Kitchen",
            resolved_url="https://www.google.com/maps/place/Open+Kitchen",
            snapshot={
                "name": "Open Kitchen",
                "reservation_links": [
                    {"label": "TableCheck", "url": "https://www.tablecheck.com/example"},
                    {"label": "Bad", "url": "javascript:alert(1)"},
                ],
                "body_text": "Open Kitchen",
            },
        )

        self.assertEqual(
            [link.to_dict() for link in details.reservation_links],
            [{"label": "TableCheck", "url": "https://www.tablecheck.com/example"}],
        )
        self.assertEqual(
            details.to_dict()["reservation_links"],
            [{"label": "TableCheck", "url": "https://www.tablecheck.com/example"}],
        )

    def test_build_place_details_preserves_google_place_id(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            snapshot={
                "name": "Den",
                "google_place_id": "ChIJ8T36HxCLGGARvpARPDyaKLA",
                "address_parts": [
                    "2 Chome Jingumae",
                    "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                    "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                    "Shibuya",
                    "150-0001",
                    "Tokyo",
                    "JP",
                    ["Floor 1"],
                ],
                "body_text": "Den\nJapanese restaurant",
            },
        )

        self.assertEqual(details.google_place_id, "ChIJ8T36HxCLGGARvpARPDyaKLA")
        self.assertEqual(
            details.address_parts,
            [
                "2 Chome Jingumae",
                "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                "Shibuya",
                "150-0001",
                "Tokyo",
                "JP",
                ["Floor 1"],
            ],
        )

    def test_build_place_details_preserves_search_result_fields(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/search/?api=1&query=Taipei+Zoo",
            resolved_url="https://www.google.com/maps/place/Taipei+Zoo",
            snapshot={
                "name": "Taipei Zoo",
                "search_result_description": "Sizable zoo with a gondola & kids' area",
                "search_result_url": "https://www.google.com/maps/place/Taipei+Zoo",
                "body_text": "Taipei Zoo",
            },
        )

        self.assertEqual(
            details.search_result_description,
            "Sizable zoo with a gondola & kids' area",
        )
        self.assertEqual(
            details.search_result_url,
            "https://www.google.com/maps/place/Taipei+Zoo",
        )
        self.assertEqual(
            details.to_dict()["search_result_description"],
            "Sizable zoo with a gondola & kids' area",
        )

    def test_build_place_details_rejects_invalid_address_parts(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            snapshot={
                "name": "Den",
                "address_parts": [
                    "2 Chome Jingumae",
                    "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                    "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                    "Shibuya",
                    "150-0001",
                    "Tokyo",
                    "JP",
                    ["Floor 1", 3],
                ],
                "body_text": "Den\nJapanese restaurant",
            },
        )

        self.assertIsNone(details.address_parts)

    def test_build_place_details_rejects_page_chrome_address_and_falls_back_to_panel_text(
        self,
    ) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Bianchetto",
            resolved_url="https://www.google.com/maps/place/Bianchetto",
            snapshot={
                "name": "Bianchetto",
                "address": "Imagery © 2026 Google TermsPrivacySend Product Feedback",
                "panel_text": "\n".join(
                    [
                        "Bianchetto",
                        "Restaurant",
                        "26-28 Cotham Rd, Kew VIC 3101, Australia",
                    ]
                ),
            },
        )

        self.assertEqual(details.address, "26-28 Cotham Rd, Kew VIC 3101, Australia")

    def test_build_place_details_rejects_compacted_page_chrome_address_and_policy_description(
        self,
    ) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/BunBo",
            resolved_url="https://www.google.com/maps/place/BunBo",
            snapshot={
                "name": "Bun Bo",
                "address": "Imagery ©2026 , Map data ©2026 JapanTermsPrivacySend Product Feedback",
                "description": "Our policies do not permit contributions to this type of place.",
                "body_text": "\n".join(
                    [
                        "Bun Bo",
                        "Noodle shop",
                        "Imagery ©2026 , Map data ©2026 JapanTermsPrivacySend Product Feedback",
                    ]
                ),
            },
        )

        self.assertIsNone(details.address)
        self.assertIsNone(details.description)

    def test_build_place_details_rejects_invalid_snapshot_plus_code_and_falls_back_to_lines(
        self,
    ) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den",
            snapshot={
                "name": "Den",
                "plus_code": "https://www.google.com/maps/place/Den",
                "panel_text": "\n".join(
                    [
                        "Den",
                        "Japanese restaurant",
                        "MPF7+73 Shibuya, Tokyo, Japan",
                    ]
                ),
            },
        )

        self.assertEqual(details.plus_code, "MPF7+73 Shibuya, Tokyo, Japan")

    def test_normalize_google_place_id_accepts_trailing_hyphen(self) -> None:
        self.assertEqual(
            _normalize_google_place_id("ChIJabcdefghij-"),
            "ChIJabcdefghij-",
        )

    def test_build_place_details_rejects_street_view_as_photo(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Open+Kitchen",
            resolved_url="https://www.google.com/maps/place/Open+Kitchen",
            snapshot={
                "name": "Open Kitchen",
                "main_photo_url": (
                    "https://streetviewpixels-pa.googleapis.com/v1/thumbnail?panoid=abc"
                ),
                "photo_url": (
                    "https://streetviewpixels-pa.googleapis.com/v1/thumbnail?panoid=abc"
                ),
                "body_text": "Open Kitchen",
            },
        )

        self.assertIsNone(details.main_photo_url)
        self.assertIsNone(details.photo_url)

    def test_build_place_details_rejects_google_avatar_as_photo(self) -> None:
        details = _build_place_details(
            "https://www.google.com/maps/place/Fa+Burger",
            resolved_url="https://www.google.com/maps/place/Fa+Burger",
            snapshot={
                "name": "Fa Burger",
                "main_photo_url": "https://lh3.googleusercontent.com/a-/ALV-UjW_avatar",
                "photo_url": "https://lh3.googleusercontent.com/a-/ALV-UjW_avatar",
                "body_text": "Fa Burger",
            },
        )

        self.assertIsNone(details.main_photo_url)
        self.assertIsNone(details.photo_url)

    def test_open_place_result_from_search_page_waits_for_place_title(self) -> None:
        class _FakePage:
            def __init__(self) -> None:
                self.waited: list[object] = []
                self.visited: list[tuple[str, str, int]] = []
                self.clicked: list[tuple[float, float]] = []
                self.detail_checks = 0
                self.mouse = self

            def evaluate(self, script: object, *args: object) -> object:
                if script == _PLACE_SEARCH_RESULT_CLICK_JS:
                    return "https://www.google.co.jp/maps/place/National+Azabu?hl=ja&gl=jp"
                if script == _PLACE_SEARCH_RESULT_OPEN_JS:
                    assert args[0] == (
                        "https://www.google.co.jp/maps/place/National+Azabu?hl=ja&gl=jp"
                    )
                    return {"x": 20, "y": 40}
                if script == _PLACE_DETAIL_READY_JS:
                    self.detail_checks += 1
                    return True
                return None

            def click(self, x: float, y: float) -> None:
                self.clicked.append((x, y))

            def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                self.visited.append((url, wait_until, timeout))

            def wait_for_load_state(self, state: str, *, timeout: int) -> None:
                self.waited.append(("load_state", state, timeout))

            def wait_for_selector(self, selector: str, *, timeout: int, state: str) -> None:
                self.waited.append(("selector", selector, timeout, state))

            def wait_for_timeout(self, value: int) -> None:
                self.waited.append(("timeout", value))

        page = _FakePage()
        with patch("gmaps_scraper.place_scraper._handle_google_consent") as consent_mock:
            self.assertEqual(
                _open_place_result_from_search_page(page, timeout_ms=30_000),
                {"opened_from_search_result": True},
            )
        self.assertEqual(page.visited, [])
        self.assertEqual(page.clicked, [(20, 40)])
        self.assertEqual(page.detail_checks, 2)
        self.assertIn(("load_state", "load", 10_000), page.waited)
        self.assertEqual(consent_mock.call_count, 2)

    def test_open_place_result_from_search_page_falls_back_to_goto_when_click_fails(
        self,
    ) -> None:
        class _FakePage:
            def __init__(self) -> None:
                self.visited: list[tuple[str, str, int]] = []
                self.detail_checks = 0

            def evaluate(self, script: object, *_args: object) -> object:
                if script == _PLACE_SEARCH_RESULT_CLICK_JS:
                    return "https://www.google.co.jp/maps/place/National+Azabu?hl=ja&gl=jp"
                if script == _PLACE_SEARCH_RESULT_OPEN_JS:
                    return {}
                if script == _PLACE_DETAIL_READY_JS:
                    self.detail_checks += 1
                    return True
                return None

            def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                self.visited.append((url, wait_until, timeout))

            def wait_for_load_state(self, _state: str, *, timeout: int) -> None:
                assert timeout == 10_000

            def wait_for_selector(self, _selector: str, *, timeout: int, state: str) -> None:
                assert timeout == 10_000
                assert state == "attached"

        page = _FakePage()
        with patch("gmaps_scraper.place_scraper._handle_google_consent") as consent_mock:
            self.assertEqual(
                _open_place_result_from_search_page(page, timeout_ms=30_000),
                {"opened_from_search_result": True},
            )
        self.assertEqual(
            page.visited,
            [
                (
                    "https://www.google.co.jp/maps/place/National+Azabu?hl=ja&gl=jp",
                    "domcontentloaded",
                    30_000,
                )
            ],
        )
        self.assertEqual(page.detail_checks, 1)
        self.assertEqual(consent_mock.call_count, 2)

    def test_open_place_result_from_search_page_preserves_card_details_on_goto_failure(
        self,
    ) -> None:
        class _FakePage:
            def evaluate(self, script: object, *_args: object) -> object:
                if script == _PLACE_SEARCH_RESULT_CLICK_JS:
                    return {
                        "href": "https://www.google.com/maps/place/Taipei+Zoo",
                        "name": "Taipei Zoo",
                        "review_count": "76,998",
                        "search_result_description": "Sizable zoo with a gondola & kids' area",
                    }
                if script == _PLACE_SEARCH_RESULT_OPEN_JS:
                    return {}
                return None

            def goto(self, _url: str, *, wait_until: str, timeout: int) -> None:
                assert wait_until == "domcontentloaded"
                assert timeout == 30_000
                raise RuntimeError("navigation blocked")

            def wait_for_timeout(self, _value: int) -> None:
                pass

        self.assertEqual(
            _open_place_result_from_search_page(_FakePage(), timeout_ms=30_000),
            {
                "search_result_url": "https://www.google.com/maps/place/Taipei+Zoo",
                "name": "Taipei Zoo",
                "review_count": "76,998",
                "search_result_description": "Sizable zoo with a gondola & kids' area",
            },
        )

    def test_open_place_result_from_search_page_rejects_non_google_place_urls(self) -> None:
        class _FakePage:
            def __init__(self) -> None:
                self.visited: list[str] = []

            def evaluate(self, script: object, *_args: object) -> object:
                if script == _PLACE_SEARCH_RESULT_CLICK_JS:
                    return "https://example.com/maps/place/National+Azabu"
                return False

            def wait_for_timeout(self, _value: int) -> None:
                pass

            def goto(self, url: str, **_kwargs: object) -> None:
                self.visited.append(url)

        page = _FakePage()
        self.assertFalse(_open_place_result_from_search_page(page, timeout_ms=30_000))
        self.assertEqual(page.visited, [])

    def test_search_result_candidate_js_decodes_place_id_safely(self) -> None:
        self.assertIn("safeDecodeURIComponent", _PLACE_SEARCH_RESULT_CLICK_JS)
        self.assertNotIn(
            "decodeURIComponent(placeIdMatch[1])",
            _PLACE_SEARCH_RESULT_CLICK_JS,
        )

    def test_looks_like_google_maps_place_url_accepts_google_tlds_only(self) -> None:
        self.assertTrue(
            _looks_like_google_maps_place_url(
                "https://www.google.co.jp/maps/place/National+Azabu"
            )
        )
        self.assertTrue(
            _looks_like_google_maps_place_url(
                "https://maps.google.com/maps/place/National+Azabu"
            )
        )
        self.assertFalse(
            _looks_like_google_maps_place_url(
                "https://example.com/maps/place/National+Azabu"
            )
        )
        self.assertFalse(
            _looks_like_google_maps_place_url(
                "https://www.google.com.example.com/maps/place/National+Azabu"
            )
        )

    def test_search_result_candidate_url_stops_polling_on_place_page_sentinel(self) -> None:
        class _FakePage:
            def __init__(self) -> None:
                self.evaluate_calls = 0
                self.wait_calls = 0
                self.evaluated_scripts: list[object] = []

            def evaluate(self, script: object) -> object:
                self.evaluate_calls += 1
                self.evaluated_scripts.append(script)
                return False

            def wait_for_timeout(self, _value: int) -> None:
                self.wait_calls += 1

        page = _FakePage()
        self.assertIsNone(_search_result_candidate_url(page, timeout_ms=30_000))
        self.assertEqual(page.evaluate_calls, 1)
        self.assertEqual(page.evaluated_scripts, [_PLACE_SEARCH_RESULT_CLICK_JS])
        self.assertEqual(page.wait_calls, 0)

    def test_search_result_candidate_url_accepts_card_details_object(self) -> None:
        class _FakePage:
            def evaluate(self, script: object) -> object:
                assert script == _PLACE_SEARCH_RESULT_CLICK_JS
                return {
                    "href": "https://www.google.com/maps/place/Pooles+Temple",
                    "name": "Pooles Temple",
                    "review_count": "16",
                }

            def wait_for_timeout(self, _value: int) -> None:
                raise AssertionError("should not poll after a card candidate is found")

        self.assertEqual(
            _search_result_candidate_url(_FakePage(), timeout_ms=30_000),
            "https://www.google.com/maps/place/Pooles+Temple",
        )

    def test_extract_secondary_name_aborts_when_rating_line_follows_name(self) -> None:
        self.assertIsNone(
            _extract_secondary_name(
                ["Den", "4.4", "傳"],
                name="Den",
            )
        )

    def test_normalize_photo_url_rejects_google_avatar_urls(self) -> None:
        self.assertIsNone(
            _normalize_photo_url("https://lh3.googleusercontent.com/a-/ALV-UjW_avatar")
        )
        self.assertIsNone(_normalize_photo_url("https://lh5.ggpht.com/a/example-avatar"))
        self.assertIsNone(
            _normalize_photo_url("https://lh3.googleusercontent.com:443/a-/ALV-UjW_avatar")
        )
        self.assertEqual(
            _normalize_photo_url("https://lh3.googleusercontent.com/p/example=s680-w680-h510"),
            "https://lh3.googleusercontent.com/p/example=s680-w680-h510",
        )
        self.assertEqual(
            _normalize_photo_url(
                "https://lh3.googleusercontent.com/gps-cs-s/example=w408-h306-k-no"
            ),
            "https://lh3.googleusercontent.com/gps-cs-s/example=w408-h306-k-no",
        )

    def test_normalize_photo_url_rejects_ad_thumbnails_and_unshaped_hosts(self) -> None:
        for photo_url in (
            "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcExample&s=3",
            "https://www.gstatic.com/faviconV2?url=https://example.com",
            "https://www.gstatic.com/ads-travel/example.png",
            "https://lh3.googleusercontent.com/p/example",
            "https://lh3.googleusercontent.com.example/p/example=w680-h510-k-no",
        ):
            with self.subTest(photo_url=photo_url):
                self.assertIsNone(_normalize_photo_url(photo_url))

    def test_normalize_photo_url_rejects_google_static_map_urls(self) -> None:
        self.assertIsNone(
            _normalize_photo_url(
                "https://maps.google.com/maps/api/staticmap?center=35.6530036,139.7223467"
            )
        )
        self.assertIsNone(
            _normalize_photo_url(
                "https://www.google.com/maps/api/staticmap?center=35.6530036,139.7223467"
            )
        )
        self.assertIsNone(
            _normalize_photo_url(
                "https://maps.googleapis.com/maps/api/staticmap?center=35.6530036,139.7223467"
            )
        )

    def test_normalize_preview_website_rejects_streetview_thumbnail_urls(self) -> None:
        self.assertIsNone(
            _normalize_preview_website(
                "https://streetviewpixels-pa.googleapis.com/v1/thumbnail?panoid=abc"
            )
        )
        self.assertIsNone(
            _normalize_preview_website(
                "https://inline.app/booking/foo?utm_source=ig"
            )
        )

    def test_normalize_website_rejects_non_http_urls(self) -> None:
        self.assertEqual(_normalize_website("https://example.com"), "https://example.com")
        self.assertEqual(_normalize_website("http://example.com"), "http://example.com")
        self.assertIsNone(_normalize_website("javascript:alert(1)"))
        self.assertIsNone(_normalize_website("mailto:test@example.com"))
        self.assertIsNone(_normalize_website("example.com"))

    def test_normalize_reservation_links_keeps_http_provider_links(self) -> None:
        links = _normalize_reservation_links(
            [
                {"label": "Resy", "url": "https://resy.com/cities/lisbon/example"},
                {"label": "Duplicate", "url": "https://resy.com/cities/lisbon/example"},
                {"label": "Bad", "url": "javascript:alert(1)"},
            ]
        )

        self.assertEqual(
            [link.to_dict() for link in links],
            [{"label": "Resy", "url": "https://resy.com/cities/lisbon/example"}],
        )

    def test_normalize_reservation_links_cleans_google_dialog_labels(self) -> None:
        links = _normalize_reservation_links(
            [
                {
                    "label": "\ue878 Find a table tablecheck.com",
                    "url": "https://www.tablecheck.com/markstokyo/reserve",
                },
                {
                    "label": "ikyu.com ikyu.com ikyu.com",
                    "url": "https://restaurant.ikyu.com/112767/?ikgo=2",
                },
                {
                    "label": "autoreserve.com autoreserve.com autoreserve.com",
                    "url": "https://autoreserve.com/restaurants/example",
                },
                {
                    "label": "\ue157 sg-management.jp \ue157sg-management.jp sg-management.jp",
                    "url": "https://sg-management.jp/reserve/",
                },
                {
                    "label": "Reserve a table",
                    "url": "https://www.google.com/maps/reserve/v/dine/c/example",
                },
            ]
        )

        self.assertEqual(
            [link.to_dict() for link in links],
            [
                {"label": "TableCheck", "url": "https://www.tablecheck.com/markstokyo/reserve"},
                {"label": "Ikyu", "url": "https://restaurant.ikyu.com/112767/?ikgo=2"},
                {"label": "AutoReserve", "url": "https://autoreserve.com/restaurants/example"},
                {"label": "SG Management", "url": "https://sg-management.jp/reserve/"},
            ],
        )

    def test_normalize_reservation_links_unwraps_provider_redirects(self) -> None:
        links = _normalize_reservation_links(
            [
                {
                    "label": "Find a table Inline",
                    "url": "https://www.google.com:443/url?q=https%3A%2F%2Finline.app%2Fbooking%2Ffoo",
                }
            ]
        )

        self.assertEqual(
            [link.to_dict() for link in links],
            [{"label": "Inline", "url": "https://inline.app/booking/foo"}],
        )

    def test_normalize_reservation_links_drops_google_reserve_cctld(self) -> None:
        links = _normalize_reservation_links(
            [
                {
                    "label": "Reserve a table",
                    "url": "https://www.google.com.sg/maps/reserve/v/dine/c/example",
                },
                {
                    "label": "TableCheck",
                    "url": "https://www.tablecheck.com/example",
                },
            ]
        )

        self.assertEqual(
            [link.to_dict() for link in links],
            [{"label": "TableCheck", "url": "https://www.tablecheck.com/example"}],
        )

    def test_normalize_reservation_links_ignores_google_substring_hosts(self) -> None:
        links = _normalize_reservation_links(
            [
                {
                    "label": "Reserve",
                    "url": "https://evilgoogle.com/maps/reserve/v/dine/c/example",
                },
                {
                    "label": "TableCheck",
                    "url": "https://www.tablecheck.com/example",
                },
            ]
        )

        self.assertEqual(
            [link.url for link in links],
            [
                "https://evilgoogle.com/maps/reserve/v/dine/c/example",
                "https://www.tablecheck.com/example",
            ],
        )

    def test_merge_place_sources_only_backfills_missing_fields(self) -> None:
        merged = _merge_place_sources(
            {
                "name": "Den",
                "category": "",
                "website": None,
                "phone": "+81 3-6455-5433",
                "limited_view": False,
            },
            {
                "category": "Japanese restaurant",
                "website": "http://www.jimbochoden.com/",
                "phone": "03-6455-5433",
                "limited_view": True,
            },
        )

        self.assertEqual(merged["name"], "Den")
        self.assertEqual(merged["category"], "Japanese restaurant")
        self.assertEqual(merged["website"], "http://www.jimbochoden.com/")
        self.assertEqual(merged["phone"], "+81 3-6455-5433")
        self.assertTrue(merged["limited_view"])

    def test_merge_llm_place_fields_only_backfills_cleaned_missing_fields(self) -> None:
        merged = _merge_llm_place_fields(
            {
                "name": "Den",
                "website": "https://example.com",
                "address": "bad page chrome",
                "field_sources": {"name": "dom", "website": "dom", "address": "dom"},
                "review_topics": [{"text": "sushi, mentioned in 115 reviews"}],
                "about_sections": [
                    {
                        "title": "Service options",
                        "items": [{"label": "Dine-in"}],
                    }
                ],
            },
            {
                "name": "Other Den",
                "website": "example.com",
                "address": "2 Chome Jingumae, Tokyo, Japan",
                "reviews": [{"author": "Fake", "text": "Invented"}],
                "review_topics": [
                    {"label": "sushi", "count": 115},
                    {"label": "sushi", "count": 999},
                    {"label": "ramen", "count": 20},
                ],
                "about_sections": [
                    {
                        "title": "Service options",
                        "items": [
                            {"label": "Dine-in"},
                            {"label": "Delivery"},
                        ],
                    }
                ],
            },
            current_fields={
                "name": "Den",
                "website": "https://example.com",
                "address": None,
                "reviews": [],
                "review_topics": [],
                "about_sections": [],
            },
        )

        self.assertEqual(merged["name"], "Den")
        self.assertEqual(merged["website"], "https://example.com")
        self.assertEqual(merged["address"], "2 Chome Jingumae, Tokyo, Japan")
        self.assertNotIn("reviews", merged)
        self.assertEqual(merged["review_topics"], [{"label": "sushi", "count": 115}])
        self.assertEqual(
            merged["about_sections"],
            [{"title": "Service options", "items": [{"label": "Dine-in"}]}],
        )
        self.assertEqual(merged["field_sources"]["address"], "llm")
        self.assertEqual(merged["field_sources"]["review_topics"], "llm")

    def test_seed_google_consent_cookies_uses_page_context(self) -> None:
        class _FakeContext:
            def __init__(self) -> None:
                self.cookies: list[object] = []

            def add_cookies(self, cookies: list[object]) -> None:
                self.cookies.extend(cookies)

        class _FakePage:
            def __init__(self) -> None:
                self.context = _FakeContext()

        page = _FakePage()
        _seed_google_consent_cookies(
            page,
            source_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
        )

        self.assertGreaterEqual(len(page.context.cookies), 1)
        self.assertEqual(page.context.cookies[0]["name"], "CONSENT")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from gmaps_scraper.cli import (
    _download_place_photo,
    _place_screenshot_path,
    _scrape_place_for_debug,
    main,
)
from gmaps_scraper.models import PlaceDetails, PlaceScrapeResult
from gmaps_scraper.scraper import (
    BrowserArtifacts,
    BrowserSessionConfig,
    HttpSessionConfig,
    ScrapeError,
)


def _artifacts() -> BrowserArtifacts:
    return BrowserArtifacts(
        resolved_url=(
            "https://www.google.com/maps/@30.5370705,125.4120472,6z/"
            "data=!4m3!11m2!2sTESTLISTABC123456789!3e3?entry=ttu"
        ),
        runtime_state=["runtime"],
        script_texts=["script"],
        html="<html></html>",
    )


def _parsed_payload() -> dict[str, object]:
    return {
        "source_url": "https://maps.app.goo.gl/TestSavedListShortUrl",
        "resolved_url": (
            "https://www.google.com/maps/@30.5370705,125.4120472,6z/"
            "data=!4m3!11m2!2sTESTLISTABC123456789!3e3?entry=ttu"
        ),
        "list_id": "TESTLISTABC123456789",
        "title": "Sample Coffee Stops",
        "description": "Curated fixture data for parser tests",
        "owner": {
            "name": "Fixture Owner",
            "photo_url": "https://lh3.googleusercontent.com/a-/fixture-owner",
            "profile_id": "104356373423434804635",
        },
        "collaborators": [
            {
                "name": "Fixture Collaborator",
                "photo_url": "https://lh3.googleusercontent.com/a-/fixture-collaborator",
                "profile_id": "205678901234567890123",
            }
        ],
        "places": [],
    }


def _result(payload: dict[str, object]) -> Mock:
    result = Mock()
    result.to_dict.return_value = payload
    return result


class CliTests(unittest.TestCase):
    def test_place_screenshot_path_includes_digest_to_avoid_slug_collisions(self) -> None:
        path_a = _place_screenshot_path(
            Path("screenshots"),
            "https://maps.app.goo.gl/example-a",
            stage="overview",
        )
        path_b = _place_screenshot_path(
            Path("screenshots"),
            "https://maps.app.goo.gl/example_a",
            stage="overview",
        )

        self.assertIsNotNone(path_a)
        self.assertIsNotNone(path_b)
        assert path_a is not None
        assert path_b is not None
        self.assertNotEqual(path_a.name, path_b.name)
        self.assertRegex(path_a.name, r"-[0-9a-f]{8}-overview[.]png$")

    def test_prints_json_to_stdout(self) -> None:
        stdout = io.StringIO()
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with (
            patch(
                "sys.argv",
                ["gmaps-scraper", "https://maps.app.goo.gl/TestSavedListShortUrl"],
            ),
            patch(
                "gmaps_scraper.cli.collect_saved_list_result",
                return_value=(artifacts, result),
            ) as collect_saved_list_result,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), parsed_payload)
        collect_saved_list_result.assert_called_once_with(
            "https://maps.app.goo.gl/TestSavedListShortUrl",
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            collection_mode="auto",
            browser_session=None,
            http_session=None,
        )

    def test_writes_output_file_and_forwards_cli_flags(self) -> None:
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "saved-list.json"
            with (
                patch(
                        "sys.argv",
                        [
                            "gmaps-scraper",
                            "https://maps.app.goo.gl/TestSavedListShortUrl",
                            "--output",
                            str(output_path),
                        "--headed",
                        "--timeout-ms",
                        "45000",
                        "--settle-ms",
                        "5000",
                        "--session-dir",
                        str(Path(tmp_dir) / "session"),
                        "--proxy",
                        "http://proxy.example:8080",
                        "--http-cookie-jar",
                        str(Path(tmp_dir) / "cookies.txt"),
                    ],
                ),
                patch(
                    "gmaps_scraper.cli.collect_saved_list_result",
                    return_value=(artifacts, result),
                ) as collect_saved_list_result,
            ):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8")),
                parsed_payload,
            )
            collect_saved_list_result.assert_called_once_with(
                "https://maps.app.goo.gl/TestSavedListShortUrl",
                headless=False,
                timeout_ms=45_000,
                settle_time_ms=5_000,
                collection_mode="auto",
                browser_session=BrowserSessionConfig(
                    profile_dir=Path(tmp_dir) / "session",
                    proxy="http://proxy.example:8080",
                ),
                http_session=HttpSessionConfig(
                    cookie_jar_path=Path(tmp_dir) / "cookies.txt",
                    proxy="http://proxy.example:8080",
                ),
            )

    def test_forwards_explicit_collection_mode(self) -> None:
        stdout = io.StringIO()
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "https://maps.app.goo.gl/TestSavedListShortUrl",
                    "--fetch-mode",
                    "browser",
                ],
            ),
            patch(
                "gmaps_scraper.cli.collect_saved_list_result",
                return_value=(artifacts, result),
            ) as collect_saved_list_result,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), parsed_payload)
        collect_saved_list_result.assert_called_once_with(
            "https://maps.app.goo.gl/TestSavedListShortUrl",
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            collection_mode="browser",
            browser_session=None,
            http_session=None,
        )

    def test_forwards_browser_humanization_flags(self) -> None:
        stdout = io.StringIO()
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "https://maps.app.goo.gl/TestSavedListShortUrl",
                    "--human-mouse",
                    "--disable-random-window-size",
                ],
            ),
            patch(
                "gmaps_scraper.cli.collect_saved_list_result",
                return_value=(artifacts, result),
            ) as collect_saved_list_result,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), parsed_payload)
        collect_saved_list_result.assert_called_once_with(
            "https://maps.app.goo.gl/TestSavedListShortUrl",
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            collection_mode="auto",
            browser_session=BrowserSessionConfig(window_size=None, human_mouse=True),
            http_session=None,
        )

    def test_place_kind_calls_place_scraper(self) -> None:
        stdout = io.StringIO()
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            name="Den",
            secondary_name="傳",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
            status="Closed · Opens 6 PM",
            website="http://www.jimbochoden.com/",
            phone="+81 3-6455-5433",
            plus_code="MPF7+73 Shibuya, Tokyo, Japan",
            lat=35.6731762,
            lng=139.7127216,
            limited_view=True,
        )

        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "https://www.google.com/maps/place/Den",
                    "--kind",
                    "place",
                ],
            ),
            patch("gmaps_scraper.cli.scrape_place", return_value=details) as scrape_place,
            patch("gmaps_scraper.cli.collect_saved_list_result") as collect_saved_list_result,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), details.to_dict())
        scrape_place.assert_called_once_with(
            "https://www.google.com/maps/place/Den",
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            browser_session=None,
            http_session=None,
            llm_fallback=None,
            llm_policy="on_quality_failure",
            llm_tasks=("dom_repair", "display_translation"),
            collect_reviews=True,
            collect_about=True,
        )
        collect_saved_list_result.assert_not_called()

    def test_place_kind_downloads_photo_when_requested(self) -> None:
        stdout = io.StringIO()
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome\u22123\u221218",
            main_photo_url="https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
            photo_url="https://lh3.googleusercontent.com/p/example=s680-w680-h510",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            photo_path = Path(tmp_dir) / "den.jpg"
            with (
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://www.google.com/maps/place/Den",
                        "--kind",
                        "place",
                        "--download-photo",
                        str(photo_path),
                    ],
                ),
                patch("gmaps_scraper.cli.scrape_place", return_value=details),
                patch("gmaps_scraper.cli._download_place_photo") as download_photo,
                redirect_stdout(stdout),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        download_photo.assert_called_once_with(
            details,
            output_path=photo_path,
            http_session=None,
        )

    def test_place_kind_downloads_main_photo_when_requested(self) -> None:
        stdout = io.StringIO()
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome\u22123\u221218",
            main_photo_url="https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
            photo_url="https://lh3.googleusercontent.com/p/example=s680-w680-h510",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            photo_path = Path(tmp_dir) / "den-main.jpg"
            with (
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://www.google.com/maps/place/Den",
                        "--kind",
                        "place",
                        "--download-main-photo",
                        str(photo_path),
                    ],
                ),
                patch("gmaps_scraper.cli.scrape_place", return_value=details),
                patch("gmaps_scraper.cli._download_place_image") as download_photo,
                redirect_stdout(stdout),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        download_photo.assert_called_once_with(
            details.main_photo_url,
            output_path=photo_path,
            http_session=None,
            referer=details.resolved_url or details.source_url,
            missing_message="No main photo URL was found for this place.",
        )

    def test_download_photo_requires_place_kind(self) -> None:
        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "https://maps.app.goo.gl/TestSavedListShortUrl",
                    "--download-photo",
                    "photo.jpg",
                ],
            ),
            self.assertRaises(SystemExit),
        ):
            main()

    def test_download_main_photo_requires_place_kind(self) -> None:
        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "https://maps.app.goo.gl/TestSavedListShortUrl",
                    "--download-main-photo",
                    "main-photo.jpg",
                ],
            ),
            self.assertRaises(SystemExit),
        ):
            main()

    def test_place_kind_reports_missing_photo_when_download_requested(self) -> None:
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome\u22123\u221218",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://www.google.com/maps/place/Den",
                        "--kind",
                        "place",
                        "--download-photo",
                        str(Path(tmp_dir) / "den.jpg"),
                    ],
                ),
                patch("gmaps_scraper.cli.scrape_place", return_value=details),
                self.assertRaises(SystemExit),
            ):
                main()

    def test_place_kind_reports_missing_main_photo_when_download_requested(self) -> None:
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome\u22123\u221218",
            photo_url="https://lh3.googleusercontent.com/p/example=s680-w680-h510",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://www.google.com/maps/place/Den",
                        "--kind",
                        "place",
                        "--download-main-photo",
                        str(Path(tmp_dir) / "den-main.jpg"),
                    ],
                ),
                patch("gmaps_scraper.cli.scrape_place", return_value=details),
                self.assertRaises(SystemExit),
            ):
                main()

    def test_uses_proxy_from_environment_for_list_scrapes(self) -> None:
        stdout = io.StringIO()
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with (
            patch(
                "sys.argv",
                ["gmaps-scraper", "https://maps.app.goo.gl/TestSavedListShortUrl"],
            ),
            patch.dict("os.environ", {"GMAPS_SCRAPER_PROXY": "http://proxy.example:8080"}),
            patch(
                "gmaps_scraper.cli.collect_saved_list_result",
                return_value=(artifacts, result),
            ) as collect_saved_list_result,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        collect_saved_list_result.assert_called_once_with(
            "https://maps.app.goo.gl/TestSavedListShortUrl",
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            collection_mode="auto",
            browser_session=BrowserSessionConfig(
                profile_dir=None,
                proxy="http://proxy.example:8080",
            ),
            http_session=HttpSessionConfig(
                cookie_jar_path=None,
                proxy="http://proxy.example:8080",
            ),
        )

    def test_place_kind_forwards_http_cookie_jar_to_preview_enrichment(self) -> None:
        stdout = io.StringIO()
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome\u22123\u221218",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://www.google.com/maps/place/Den",
                        "--kind",
                        "place",
                        "--http-cookie-jar",
                        str(Path(tmp_dir) / "cookies.txt"),
                    ],
                ),
                patch("gmaps_scraper.cli.scrape_place", return_value=details) as scrape_place,
                redirect_stdout(stdout),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        scrape_place.assert_called_once_with(
            "https://www.google.com/maps/place/Den",
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            browser_session=None,
            http_session=HttpSessionConfig(
                cookie_jar_path=Path(tmp_dir) / "cookies.txt",
                proxy=None,
            ),
            llm_fallback=None,
            llm_policy="on_quality_failure",
            llm_tasks=("dom_repair", "display_translation"),
            collect_reviews=True,
            collect_about=True,
        )

    def test_place_kind_can_enable_llm_repair_from_env(self) -> None:
        stdout = io.StringIO()
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Tokyo, Japan",
        )
        llm_fallback = Mock()

        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "https://www.google.com/maps/place/Den",
                    "--kind",
                    "place",
                    "--llm-repair",
                    "--llm-policy",
                    "always",
                    "--llm-env-file",
                    ".env.test",
                ],
            ),
            patch(
                "gmaps_scraper.cli.openai_compatible_place_repairer_from_env",
                return_value=llm_fallback,
            ) as repairer_from_env,
            patch("gmaps_scraper.cli.scrape_place", return_value=details) as scrape_place,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        repairer_from_env.assert_called_once_with(env_file=Path(".env.test"))
        scrape_place.assert_called_once_with(
            "https://www.google.com/maps/place/Den",
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            browser_session=None,
            http_session=None,
            llm_fallback=llm_fallback,
            llm_policy="always",
            llm_tasks=("dom_repair", "display_translation"),
            collect_reviews=True,
            collect_about=True,
        )

    def test_place_kind_can_scope_llm_tasks_and_skip_tabs(self) -> None:
        stdout = io.StringIO()
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Tokyo, Japan",
        )
        llm_fallback = Mock()

        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "https://www.google.com/maps/place/Den",
                    "--kind",
                    "place",
                    "--llm-repair",
                    "--llm-task",
                    "display_translation",
                    "--skip-reviews",
                    "--skip-about",
                ],
            ),
            patch(
                "gmaps_scraper.cli.openai_compatible_place_repairer_from_env",
                return_value=llm_fallback,
            ),
            patch("gmaps_scraper.cli.scrape_place", return_value=details) as scrape_place,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            scrape_place.call_args.kwargs["llm_tasks"],
            ("display_translation",),
        )
        self.assertFalse(scrape_place.call_args.kwargs["collect_reviews"])
        self.assertFalse(scrape_place.call_args.kwargs["collect_about"])

    def test_place_kind_uses_env_file_model_for_llm_cache_namespace(self) -> None:
        stdout = io.StringIO()
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Tokyo, Japan",
        )
        llm_fallback = Mock()
        cached_fallback = Mock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "LLM_MODEL=gpt-5-mini\nOPENAI_API_KEY=test-key\n",
                encoding="utf-8",
            )
            cache_dir = Path(tmp_dir) / "llm-cache"
            with (
                patch.dict("os.environ", {}, clear=True),
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://www.google.com/maps/place/Den",
                        "--kind",
                        "place",
                        "--llm-repair",
                        "--llm-env-file",
                        str(env_path),
                        "--llm-cache-dir",
                        str(cache_dir),
                    ],
                ),
                patch(
                    "gmaps_scraper.cli.openai_compatible_place_repairer_from_env",
                    return_value=llm_fallback,
                ),
                patch(
                    "gmaps_scraper.cli.cached_place_repairer",
                    return_value=cached_fallback,
                ) as cached_repairer,
                patch("gmaps_scraper.cli.scrape_place", return_value=details) as scrape_place,
                redirect_stdout(stdout),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        cached_repairer.assert_called_once_with(
            llm_fallback,
            cache_dir=cache_dir,
            cache_namespace="openai:gpt-5-mini",
        )
        scrape_place.assert_called_once()
        self.assertIs(scrape_place.call_args.kwargs["llm_fallback"], cached_fallback)

    def test_debug_place_scrape_rejects_saved_list_resolution(self) -> None:
        snapshot = {
            "resolved_url": (
                "https://www.google.com/maps/@1,2,14z/"
                "data=!4m3!11m2!2sShpCfVAkTaGQFUSz8UklcQ!3e3"
            ),
            "dom": {"name": "Singapore"},
            "preview": {},
        }

        with patch("gmaps_scraper.cli.collect_place_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(ScrapeError, "saved list"):
                _scrape_place_for_debug(
                    "https://maps.app.goo.gl/example",
                    headless=True,
                    timeout_ms=30_000,
                    settle_time_ms=3_000,
                    browser_session=None,
                    http_session=None,
                    llm_fallback=None,
                    llm_policy="on_quality_failure",
                )

    def test_debug_place_scrape_uses_production_llm_quality_gate(self) -> None:
        llm_fallback = Mock(return_value={"fields": {"rating": 4.8}})
        snapshot = {
            "resolved_url": "https://www.google.com/maps/place/Den",
            "dom": {
                "name": "Den",
                "category": "Japanese restaurant",
                "address": "Tokyo, Japan",
            },
            "preview": {},
        }

        with patch("gmaps_scraper.cli.collect_place_snapshot", return_value=snapshot):
            details, _snapshot, _merged_snapshot, _evidence = _scrape_place_for_debug(
                "https://www.google.com/maps/place/Den",
                headless=True,
                timeout_ms=30_000,
                settle_time_ms=3_000,
                browser_session=None,
                http_session=None,
                llm_fallback=llm_fallback,
                llm_policy="on_quality_failure",
            )

        llm_fallback.assert_not_called()
        self.assertIsNotNone(details.diagnostics)
        assert details.diagnostics is not None
        self.assertIn("no_reputation_or_contact", details.diagnostics.quality_flags)

    def test_debug_place_scrape_passes_unstripped_current_fields_to_llm(self) -> None:
        llm_fallback = Mock(return_value=None)
        snapshot = {
            "resolved_url": "https://www.google.com/maps/place/Den",
            "dom": {
                "name": "Den",
                "category": "Japanese restaurant",
                "address": "Tokyo, Japan",
            },
            "preview": {},
        }

        with patch("gmaps_scraper.cli.collect_place_snapshot", return_value=snapshot):
            _scrape_place_for_debug(
                "https://www.google.com/maps/place/Den",
                headless=True,
                timeout_ms=30_000,
                settle_time_ms=3_000,
                browser_session=None,
                http_session=None,
                llm_fallback=llm_fallback,
                llm_policy="always",
            )

        request = llm_fallback.call_args.args[0]
        self.assertIn("rating", request.current_fields)
        self.assertIsNone(request.current_fields["rating"])

    def test_place_kind_can_scrape_batch_from_input_file(self) -> None:
        stdout = io.StringIO()
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Tokyo, Japan",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            input_path = Path(tmp_dir) / "places.txt"
            input_path.write_text(
                "\n".join(
                    [
                        "https://www.google.com/maps/place/Den",
                        "# skipped",
                        "https://www.google.com/maps/place/Narisawa",
                    ]
                ),
                encoding="utf-8",
            )
            with (
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "--kind",
                        "place",
                        "--input",
                        str(input_path),
                        "--session-dir",
                        str(Path(tmp_dir) / "session"),
                        "--max-concurrency",
                        "1",
                        "--max-retries",
                        "2",
                        "--retry-backoff-ms",
                        "750",
                        "--stagger-ms",
                        "250",
                    ],
                ),
                patch(
                    "gmaps_scraper.cli.scrape_places",
                    return_value=[
                        PlaceScrapeResult(
                            source_url="https://www.google.com/maps/place/Den",
                            place=details,
                            attempts=1,
                        ),
                        PlaceScrapeResult(
                            source_url="https://www.google.com/maps/place/Narisawa",
                            error="blocked",
                            attempts=3,
                        ),
                    ],
                ) as scrape_places,
                redirect_stdout(stdout),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        scrape_places.assert_called_once_with(
            [
                "https://www.google.com/maps/place/Den",
                "https://www.google.com/maps/place/Narisawa",
            ],
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            browser_session=BrowserSessionConfig(
                profile_dir=Path(tmp_dir) / "session",
                proxy=None,
            ),
            http_session=None,
            llm_fallback=None,
            llm_policy="on_quality_failure",
            llm_tasks=("dom_repair", "display_translation"),
            collect_reviews=True,
            collect_about=True,
            max_concurrency=1,
            max_retries=2,
            retry_backoff_ms=750,
            stagger_ms=250,
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["results"][0]["place"]["name"], "Den")
        self.assertEqual(payload["results"][1]["error"], "blocked")

    def test_place_kind_can_scrape_batch_from_command_line_urls(self) -> None:
        stdout = io.StringIO()
        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "--kind",
                    "place",
                    "https://www.google.com/maps/place/Den",
                    "https://www.google.com/maps/place/Narisawa",
                ],
            ),
            patch(
                "gmaps_scraper.cli.scrape_places",
                return_value=[
                    PlaceScrapeResult(
                        source_url="https://www.google.com/maps/place/Den",
                        attempts=1,
                    ),
                    PlaceScrapeResult(
                        source_url="https://www.google.com/maps/place/Narisawa",
                        attempts=1,
                    ),
                ],
            ) as scrape_places,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        scrape_places.assert_called_once_with(
            [
                "https://www.google.com/maps/place/Den",
                "https://www.google.com/maps/place/Narisawa",
            ],
            headless=True,
            timeout_ms=30_000,
            settle_time_ms=3_000,
            browser_session=None,
            http_session=None,
            llm_fallback=None,
            llm_policy="on_quality_failure",
            llm_tasks=("dom_repair", "display_translation"),
            collect_reviews=True,
            collect_about=True,
            max_concurrency=1,
            max_retries=1,
            retry_backoff_ms=2_000,
            stagger_ms=0,
        )

    def test_place_kind_can_scrape_batch_from_stdin(self) -> None:
        stdout = io.StringIO()
        stdin = io.StringIO(
            "\n".join(
                [
                    "https://www.google.com/maps/place/Den",
                    "# skipped",
                    "https://www.google.com/maps/place/Narisawa",
                ]
            )
        )
        with (
            patch("sys.stdin", stdin),
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "--kind",
                    "place",
                    "--input",
                    "-",
                ],
            ),
            patch(
                "gmaps_scraper.cli.scrape_places",
                return_value=[
                    PlaceScrapeResult(
                        source_url="https://www.google.com/maps/place/Den",
                        attempts=1,
                    ),
                    PlaceScrapeResult(
                        source_url="https://www.google.com/maps/place/Narisawa",
                        attempts=1,
                    ),
                ],
            ) as scrape_places,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        scrape_places.assert_called_once()
        self.assertEqual(
            scrape_places.call_args.args[0],
            [
                "https://www.google.com/maps/place/Den",
                "https://www.google.com/maps/place/Narisawa",
            ],
        )

    def test_download_place_photo_writes_bytes(self) -> None:
        details = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            name="Den",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome\u22123\u221218",
            main_photo_url="https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
            photo_url="https://lh3.googleusercontent.com/p/example=s680-w680-h510",
        )

        class _FakeResponse:
            content = b"photo-bytes"

        class _FakeSession:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def __enter__(self) -> _FakeSession:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return None

            def get(self, url: str, referer: str) -> _FakeResponse:
                self.url = url
                self.referer = referer
                return _FakeResponse()

        class _FakeRequests:
            Session = _FakeSession

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "photo.jpg"
            with (
                patch("gmaps_scraper.cli._import_curl_requests", return_value=_FakeRequests),
                patch("gmaps_scraper.cli._raise_for_status") as raise_for_status,
            ):
                _download_place_photo(
                    details,
                    output_path=output_path,
                    http_session=HttpSessionConfig(
                        cookie_jar_path=None,
                        proxy="http://proxy.example:8080",
                    ),
                )

            self.assertEqual(output_path.read_bytes(), b"photo-bytes")
            raise_for_status.assert_called_once()

    def test_download_place_photo_wraps_network_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "photo.jpg"
            with self.assertRaisesRegex(RuntimeError, "Failed to download place photo: boom"):
                with patch(
                    "gmaps_scraper.cli._import_curl_requests",
                    side_effect=Exception("boom"),
                ):
                    _download_place_photo(
                        PlaceDetails(
                            source_url="https://www.google.com/maps/place/Den",
                            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
                            name="Den",
                            category="Japanese restaurant",
                            rating=4.4,
                            review_count=324,
                            address=(
                                "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, "
                                "2 Chome\u22123\u221218"
                            ),
                            photo_url="https://lh3.googleusercontent.com/p/example=s680-w680-h510",
                        ),
                        output_path=output_path,
                        http_session=None,
                    )

    def test_debug_output_dir_writes_dump_and_stdout_payload(self) -> None:
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with (
                patch(
                    "gmaps_scraper.cli.collect_saved_list_result",
                    return_value=(artifacts, result),
                ),
                patch("gmaps_scraper.cli.write_debug_dump") as write_debug_dump,
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://maps.app.goo.gl/TestSavedListShortUrl",
                        "--debug-output-dir",
                        tmp_dir,
                    ],
                ),
                redirect_stdout(stdout),
            ):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            write_debug_dump.assert_called_once_with(
                "https://maps.app.goo.gl/TestSavedListShortUrl",
                resolved_url=artifacts.resolved_url,
                runtime_state=artifacts.runtime_state,
                script_texts=artifacts.script_texts,
                html=artifacts.html,
                output_dir=Path(tmp_dir),
            )
            self.assertEqual(json.loads(stdout.getvalue()), parsed_payload)

    def test_dump_debug_output_uses_default_hidden_directory(self) -> None:
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with (
                patch(
                    "gmaps_scraper.cli.collect_saved_list_result",
                    return_value=(artifacts, result),
                ),
                patch("gmaps_scraper.cli.write_debug_dump") as write_debug_dump,
                patch("gmaps_scraper.cli.os.getcwd", return_value=tmp_dir),
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://maps.app.goo.gl/TestSavedListShortUrl",
                        "--dump-debug-output",
                    ],
                ),
                redirect_stdout(stdout),
            ):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            write_debug_dump.assert_called_once_with(
                "https://maps.app.goo.gl/TestSavedListShortUrl",
                resolved_url=artifacts.resolved_url,
                runtime_state=artifacts.runtime_state,
                script_texts=artifacts.script_texts,
                html=artifacts.html,
                output_dir=Path(tmp_dir) / ".gmaps-debug" / "TESTLISTABC123456789",
            )
            self.assertEqual(json.loads(stdout.getvalue()), parsed_payload)

    def test_debug_output_dir_overrides_default_dump_directory(self) -> None:
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            explicit_dir = Path(tmp_dir) / "custom-debug"
            with (
                patch(
                    "gmaps_scraper.cli.collect_saved_list_result",
                    return_value=(artifacts, result),
                ),
                patch("gmaps_scraper.cli.write_debug_dump") as write_debug_dump,
                patch("gmaps_scraper.cli.os.getcwd", return_value=tmp_dir),
                patch(
                    "sys.argv",
                    [
                        "gmaps-scraper",
                        "https://maps.app.goo.gl/TestSavedListShortUrl",
                        "--dump-debug-output",
                        "--debug-output-dir",
                        str(explicit_dir),
                    ],
                ),
                redirect_stdout(stdout),
            ):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            write_debug_dump.assert_called_once_with(
                "https://maps.app.goo.gl/TestSavedListShortUrl",
                resolved_url=artifacts.resolved_url,
                runtime_state=artifacts.runtime_state,
                script_texts=artifacts.script_texts,
                html=artifacts.html,
                output_dir=explicit_dir,
            )
            self.assertEqual(json.loads(stdout.getvalue()), parsed_payload)

    def test_list_kind_is_accepted(self) -> None:
        stdout = io.StringIO()
        artifacts = _artifacts()
        parsed_payload = _parsed_payload()
        result = _result(parsed_payload)

        with (
            patch(
                "sys.argv",
                [
                    "gmaps-scraper",
                    "https://maps.app.goo.gl/TestSavedListShortUrl",
                    "--kind",
                    "list",
                ],
            ),
            patch(
                "gmaps_scraper.cli.collect_saved_list_result",
                return_value=(artifacts, result),
            ) as collect_saved_list_result,
            redirect_stdout(stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), parsed_payload)
        collect_saved_list_result.assert_called_once()


if __name__ == "__main__":
    unittest.main()

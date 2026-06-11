from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from PIL import Image

from scripts import build_data
from scripts.pipeline_models import (
    EnrichmentCacheEntry,
    EnrichmentPlace,
    Guide,
    ListAuthor,
    NormalizedPlace,
    PlaceReservationLink,
    RawPlace,
    RawSavedList,
    SourceConfig,
)


class BuildDataTests(unittest.TestCase):
    def test_scraper_session_reset_detects_browser_launch_failures(self) -> None:
        cases = (
            "Failed to launch browser context: Timeout 180000ms exceeded",
            "BrowserType.launch_persistent_context: Timeout 180000ms exceeded",
        )
        for message in cases:
            with self.subTest(message=message):
                self.assertTrue(
                    build_data.should_reset_scraper_session(
                        build_data.ScrapeError(message)
                    )
                )

    def test_raw_saved_list_keeps_owner_metadata_from_json(self) -> None:
        saved_list = RawSavedList.model_validate(
            {
                "title": "Tokyo, Japan",
                "owner": {
                    "name": "Curator Name",
                    "photo_url": "https://example.com/curator.jpg",
                    "photo_path": "/author-photos/curator.webp",
                    "avatar_mode": "photo",
                    "profile_id": "curator-id",
                },
                "collaborators": [
                    {
                        "name": "Second Curator",
                        "photo_url": "https://example.com/second.jpg",
                    }
                ],
                "places": [],
            }
        )

        self.assertIsNotNone(saved_list.owner)
        assert saved_list.owner is not None
        self.assertEqual(saved_list.owner.name, "Curator Name")
        self.assertEqual(saved_list.owner.photo_url, "https://example.com/curator.jpg")
        self.assertEqual(saved_list.owner.photo_path, "/author-photos/curator.webp")
        self.assertEqual(saved_list.owner.avatar_mode, "photo")
        self.assertEqual(saved_list.owner.profile_id, "curator-id")
        self.assertEqual(len(saved_list.collaborators), 1)
        self.assertEqual(saved_list.collaborators[0].name, "Second Curator")
        self.assertEqual(saved_list.collaborators[0].photo_url, "https://example.com/second.jpg")

    def test_raw_place_keeps_added_by_metadata_from_json(self) -> None:
        place = RawPlace.model_validate(
            {
                "name": "Coffee Spot",
                "maps_url": "https://maps.example/coffee",
                "added_by": {
                    "name": "Second Curator",
                    "profile_id": "second-curator-id",
                },
            }
        )

        self.assertIsNotNone(place.added_by)
        assert place.added_by is not None
        self.assertEqual(place.added_by.name, "Second Curator")
        self.assertEqual(place.added_by.profile_id, "second-curator-id")

    def test_refresh_trust_signals_passes_override_location_context(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            list_overrides_dir = tmpdir_path / "lists"
            raw_dir.mkdir()
            list_overrides_dir.mkdir()
            raw = RawSavedList(
                title="Restaurants",
                places=[
                    RawPlace(
                        name="Coffee Spot",
                        maps_url="https://maps.example/coffee",
                    )
                ],
            )
            (raw_dir / "dinner-list.json").write_text(raw.model_dump_json(), encoding="utf-8")
            (list_overrides_dir / "dinner-list.json").write_text(
                json.dumps({"city_name": "Tokyo", "country_name": "Japan"}),
                encoding="utf-8",
            )
            summary = SimpleNamespace(
                michelin_regions_refreshed=0,
                michelin_details_refreshed=0,
                searched_places=0,
                skipped_places=0,
                signals_written=0,
                provider_failures=0,
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "sync_local_csv_sources"),
                patch.object(build_data, "load_places_cache", return_value={}),
                patch.object(build_data, "refresh_trust_signals_for_raw_guides", return_value=summary) as refresh,
            ):
                build_data.refresh_trust_signals(include_google_fallback=False)

        self.assertEqual(
            refresh.call_args.kwargs["guide_location_contexts"],
            {"dinner-list": ("Tokyo", "Japan")},
        )

    def test_normalize_guide_uses_raw_owner_as_author(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            owner=ListAuthor(
                name="Curator Name",
                photo_url="https://example.com/curator.jpg",
                profile_id="curator-id",
            ),
            places=[
                RawPlace(
                    name="Coffee Spot",
                    maps_url="https://maps.example/coffee",
                )
            ],
        )

        def read_json_side_effect(path: Path) -> dict[str, object]:
            if path == build_data.LIST_OVERRIDES_DIR / "tokyo-japan.json":
                return {}
            if path == build_data.PLACE_OVERRIDES_DIR / "tokyo-japan.json":
                return {}
            return {}

        with patch.object(build_data, "read_json", side_effect=read_json_side_effect):
            guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})

        self.assertIsNotNone(guide.author)
        assert guide.author is not None
        self.assertEqual(guide.author.name, "Curator Name")
        self.assertEqual(guide.author.photo_url, "https://example.com/curator.jpg")
        self.assertEqual(guide.author.profile_id, "curator-id")

    def test_normalize_guide_exposes_website_and_reservation_links(self) -> None:
        raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Lisbon, Portugal",
            places=[
                RawPlace(
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0], source_type=raw.configured_source_type)
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-20T00:00:00+00:00",
                source="google_maps_page",
                query="Open Kitchen, Lisbon, Portugal",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Open Kitchen",
                    google_maps_uri="https://www.google.com/maps/place/Open+Kitchen",
                    website="https://openkitchen.example/",
                    reservation_links=[
                        PlaceReservationLink(
                            label="Resy",
                            url="https://resy.com/cities/lisbon/open-kitchen",
                        )
                    ],
                ),
            )
        }

        with patch.object(build_data, "read_json", return_value={}):
            guide = build_data.normalize_guide("lisbon-portugal", raw, enrichment_cache=enrichment_cache)

        self.assertEqual(guide.places[0].website, "https://openkitchen.example/")
        self.assertEqual(
            [link.model_dump(mode="json") for link in guide.places[0].reservation_links],
            [{"label": "Resy", "url": "https://resy.com/cities/lisbon/open-kitchen"}],
        )
        self.assertIsNotNone(guide.places[0].provenance.website)
        self.assertIsNotNone(guide.places[0].provenance.reservation_links)

    def test_normalize_guide_allows_author_override(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            owner=ListAuthor(name="Original Curator"),
            places=[
                RawPlace(
                    name="Coffee Spot",
                    maps_url="https://maps.example/coffee",
                )
            ],
        )

        def read_json_side_effect(path: Path) -> dict[str, object]:
            if path == build_data.LIST_OVERRIDES_DIR / "tokyo-japan.json":
                return {
                    "author": {
                        "name": "Override Curator",
                        "photo_path": "/author-photos/override.svg",
                        "avatar_mode": "photo",
                    }
                }
            if path == build_data.PLACE_OVERRIDES_DIR / "tokyo-japan.json":
                return {}
            return {}

        with patch.object(build_data, "read_json", side_effect=read_json_side_effect):
            guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})

        self.assertIsNotNone(guide.author)
        assert guide.author is not None
        self.assertEqual(guide.author.name, "Override Curator")
        self.assertEqual(guide.author.photo_path, "/author-photos/override.svg")
        self.assertEqual(guide.author.avatar_mode, "photo")

    def test_normalize_guide_author_override_does_not_merge_raw_photo(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            owner=ListAuthor(
                name="Original Curator",
                photo_url="https://example.com/original.jpg",
                photo_path="/author-photos/original.webp",
            ),
            places=[
                RawPlace(
                    name="Coffee Spot",
                    maps_url="https://maps.example/coffee",
                )
            ],
        )

        def read_json_side_effect(path: Path) -> dict[str, object]:
            if path == build_data.LIST_OVERRIDES_DIR / "tokyo-japan.json":
                return {"author": {"name": "Override Curator", "avatar_mode": "initials"}}
            if path == build_data.PLACE_OVERRIDES_DIR / "tokyo-japan.json":
                return {}
            return {}

        with patch.object(build_data, "read_json", side_effect=read_json_side_effect):
            guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})

        self.assertIsNotNone(guide.author)
        assert guide.author is not None
        self.assertEqual(guide.author.name, "Override Curator")
        self.assertEqual(guide.author.avatar_mode, "initials")
        self.assertIsNone(guide.author.photo_url)
        self.assertIsNone(guide.author.photo_path)

    def test_normalize_guide_allows_remote_photo_mode_override(self) -> None:
        raw = RawSavedList(
            title="Hong Kong",
            places=[
                RawPlace(
                    name="Hotel",
                    maps_url="https://maps.example/hotel",
                )
            ],
        )

        def read_json_side_effect(path: Path) -> dict[str, object]:
            if path == build_data.LIST_OVERRIDES_DIR / "hong-kong.json":
                return {"place_photo_mode": "remote_url"}
            if path == build_data.PLACE_OVERRIDES_DIR / "hong-kong.json":
                return {}
            return {}

        with patch.object(build_data, "read_json", side_effect=read_json_side_effect):
            guide = build_data.normalize_guide("hong-kong", raw, enrichment_cache={})

        self.assertEqual(guide.place_photo_mode, "remote_url")

    def test_normalize_guide_preserves_place_added_by(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            owner=ListAuthor(name="Guide Owner", profile_id="owner-id"),
            places=[
                RawPlace(
                    name="Coffee Spot",
                    maps_url="https://maps.example/coffee",
                    added_by=ListAuthor(name="Second Curator", profile_id="second-id"),
                )
            ],
        )

        def read_json_side_effect(path: Path) -> dict[str, object]:
            if path == build_data.LIST_OVERRIDES_DIR / "tokyo-japan.json":
                return {}
            if path == build_data.PLACE_OVERRIDES_DIR / "tokyo-japan.json":
                return {}
            return {}

        with patch.object(build_data, "read_json", side_effect=read_json_side_effect):
            guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})

        self.assertEqual(guide.places[0].added_by, ListAuthor(name="Second Curator", profile_id="second-id"))
        self.assertIsNotNone(guide.places[0].provenance.added_by)
        assert guide.places[0].provenance.added_by is not None
        self.assertEqual(guide.places[0].provenance.added_by.source, "google_list")

    def test_normalize_guide_allows_place_added_by_override_and_suppression(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Coffee Spot",
                    maps_url="https://maps.example/coffee",
                    cid="1",
                    added_by=ListAuthor(name="Raw Curator"),
                ),
                RawPlace(
                    name="Tea Spot",
                    maps_url="https://maps.example/tea",
                    cid="2",
                    added_by=ListAuthor(name="Raw Curator"),
                ),
            ],
        )

        def read_json_side_effect(path: Path) -> dict[str, object]:
            if path == build_data.LIST_OVERRIDES_DIR / "tokyo-japan.json":
                return {}
            if path == build_data.PLACE_OVERRIDES_DIR / "tokyo-japan.json":
                return {
                    "cid:1": {"added_by": {"name": "Manual Curator", "avatar_mode": "initials"}},
                    "cid:2": {"added_by": None},
                }
            return {}

        with patch.object(build_data, "read_json", side_effect=read_json_side_effect):
            guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})

        places_by_id = {place.id: place for place in guide.places}
        self.assertEqual(
            places_by_id["cid:1"].added_by,
            ListAuthor(name="Manual Curator", avatar_mode="initials"),
        )
        self.assertIsNotNone(places_by_id["cid:1"].provenance.added_by)
        assert places_by_id["cid:1"].provenance.added_by is not None
        self.assertEqual(places_by_id["cid:1"].provenance.added_by.source, "manual")
        self.assertIsNone(places_by_id["cid:2"].added_by)
        self.assertIsNone(places_by_id["cid:2"].provenance.added_by)

    def test_sync_list_author_photo_downloads_local_photo_path(self) -> None:
        author = ListAuthor(
            name="Curator Name",
            photo_url="https://example.com/curator.jpg",
            profile_id="curator-id",
        )

        with patch.object(
            build_data,
            "download_list_author_photo",
            return_value="/author-photos/tokyo-japan-example.webp",
        ) as download_photo:
            synced = build_data.sync_list_author_photo("tokyo-japan", author)

        self.assertIsNotNone(synced)
        assert synced is not None
        self.assertEqual(synced.photo_path, "/author-photos/tokyo-japan-example.webp")
        download_photo.assert_called_once_with(
            "tokyo-japan",
            "https://example.com/curator.jpg",
            profile_id="curator-id",
        )

    def test_download_list_author_photo_uses_optimizer_extension(self) -> None:
        class FakeHeaders:
            def get_content_type(self) -> str:
                return "image/png"

        class FakeResponse:
            headers = FakeHeaders()

            def read(self) -> bytes:
                return b"source-image"

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        photo_url = "https://example.com/curator.jpg"
        photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]
        with TemporaryDirectory() as tmpdir:
            author_photo_dir = Path(tmpdir)
            expected_path = author_photo_dir / f"profile-curator-id--{photo_hash}.jpg"
            with (
                patch.object(build_data, "AUTHOR_PHOTOS_DIR", author_photo_dir),
                patch.object(build_data, "urlopen", return_value=FakeResponse()),
                patch.object(build_data, "optimize_author_photo_asset", return_value=(b"optimized", ".jpg")),
            ):
                result = build_data.download_list_author_photo("tokyo", photo_url, profile_id="curator-id")

            self.assertEqual(result, f"/author-photos/{expected_path.name}")
            self.assertEqual(expected_path.read_bytes(), b"optimized")

    def test_download_list_author_photo_falls_back_to_photo_hash_without_profile_id(self) -> None:
        class FakeHeaders:
            def get_content_type(self) -> str:
                return "image/png"

        class FakeResponse:
            headers = FakeHeaders()

            def read(self) -> bytes:
                return b"source-image"

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        photo_url = "https://example.com/curator.jpg"
        photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]
        with TemporaryDirectory() as tmpdir:
            author_photo_dir = Path(tmpdir)
            expected_path = author_photo_dir / f"photo-{photo_hash}.jpg"
            with (
                patch.object(build_data, "AUTHOR_PHOTOS_DIR", author_photo_dir),
                patch.object(build_data, "urlopen", return_value=FakeResponse()),
                patch.object(build_data, "optimize_author_photo_asset", return_value=(b"optimized", ".jpg")),
            ):
                result = build_data.download_list_author_photo("tokyo", photo_url)

            self.assertEqual(result, f"/author-photos/{expected_path.name}")
            self.assertEqual(expected_path.read_bytes(), b"optimized")

    def test_author_photo_temp_path_is_unique_per_invocation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            author_photo_dir = Path(tmpdir)
            first_uuid = SimpleNamespace(hex="first")
            second_uuid = SimpleNamespace(hex="second")
            with (
                patch.object(build_data, "AUTHOR_PHOTOS_DIR", author_photo_dir),
                patch.object(build_data.uuid, "uuid4", side_effect=[first_uuid, second_uuid]),
            ):
                first_path = build_data.author_photo_temp_path("profile-curator--abc.webp")
                second_path = build_data.author_photo_temp_path("profile-curator--abc.webp")

        self.assertEqual(first_path.name, ".profile-curator--abc.webp.first.tmp")
        self.assertEqual(second_path.name, ".profile-curator--abc.webp.second.tmp")
        self.assertNotEqual(first_path, second_path)

    def test_author_photo_legacy_stale_cleanup_does_not_match_slug_prefixes_or_shared_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            author_photo_dir = Path(tmpdir)
            keep_path = author_photo_dir / "tokyo--keep.webp"
            same_slug_current = author_photo_dir / "tokyo--stale.webp"
            same_slug_current_jpg = author_photo_dir / "tokyo--stale.jpg"
            same_slug_legacy = author_photo_dir / "tokyo-123456789abc.webp"
            prefixed_slug_current = author_photo_dir / "tokyo-japan--stale.webp"
            prefixed_slug_legacy = author_photo_dir / "tokyo-japan-123456789abc.webp"
            shared_profile = author_photo_dir / "profile-curator-id--123456789abc.webp"
            shared_photo_hash = author_photo_dir / "photo-123456789abc.webp"
            for path in [
                keep_path,
                same_slug_current,
                same_slug_current_jpg,
                same_slug_legacy,
                prefixed_slug_current,
                prefixed_slug_legacy,
                shared_profile,
                shared_photo_hash,
            ]:
                path.write_bytes(b"image")

            with patch.object(build_data, "AUTHOR_PHOTOS_DIR", author_photo_dir):
                stale_names = {
                    path.name
                    for path in build_data.stale_legacy_author_photo_paths("tokyo", keep_filename=keep_path.name)
                }

        self.assertEqual(
            stale_names,
            {
                same_slug_current.name,
                same_slug_current_jpg.name,
                same_slug_legacy.name,
            },
        )

    def test_optimize_author_photo_asset_falls_back_to_jpeg_without_webp(self) -> None:
        source = BytesIO()
        Image.new("RGB", (500, 300), color=(160, 80, 40)).save(source, format="PNG")

        with patch.object(build_data, "image_supports_webp", return_value=False):
            optimized_content, extension = build_data.optimize_author_photo_asset(
                source.getvalue(),
                content_type="image/png",
            )

        self.assertIsNotNone(optimized_content)
        self.assertEqual(extension, ".jpg")
        assert optimized_content is not None
        with Image.open(BytesIO(optimized_content)) as optimized_image:
            self.assertEqual(
                optimized_image.size,
                (build_data.AUTHOR_PHOTO_SIZE, build_data.AUTHOR_PHOTO_SIZE),
            )

    def test_list_author_override_skips_scraped_owner_photo_download(self) -> None:
        def read_json_side_effect(path: Path) -> dict[str, object]:
            if path == build_data.LIST_OVERRIDES_DIR / "tokyo-japan.json":
                return {"author": "Example Curator"}
            return {}

        with patch.object(build_data, "read_json", side_effect=read_json_side_effect):
            self.assertTrue(build_data.list_author_is_overridden("tokyo-japan"))

    def test_format_duration_seconds_formats_short_and_long_values(self) -> None:
        self.assertEqual(build_data.format_duration_seconds(9.34), "9.3s")
        self.assertEqual(build_data.format_duration_seconds(68.25), "1m 08.3s")
        self.assertEqual(build_data.format_duration_seconds(59.95), "1m 00.0s")

    def test_percentile_sorts_float_values_and_clamps_rank(self) -> None:
        percentile_value = build_data.percentile([4.8, 4.2, 4.5], 0.8)
        self.assertIsNotNone(percentile_value)
        self.assertAlmostEqual(percentile_value, 4.68)
        self.assertEqual(build_data.percentile([10.0, 20.0], -1.0), 10.0)
        self.assertEqual(build_data.percentile([10.0, 20.0], 2.0), 20.0)

    def test_percentile_returns_none_for_empty_values(self) -> None:
        self.assertIsNone(build_data.percentile([], 0.5))

    def test_default_refresh_workers_scales_down_to_cpu_count(self) -> None:
        with patch.object(build_data.os, "cpu_count", return_value=2):
            self.assertEqual(build_data.default_refresh_workers(), 2)

    def test_default_refresh_workers_caps_at_four(self) -> None:
        with patch.object(build_data.os, "cpu_count", return_value=16):
            self.assertEqual(build_data.default_refresh_workers(), 4)

    def test_resolve_scraper_state_dir_prefers_explicit_env(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {build_data.SCRAPER_STATE_DIR_ENV: tmpdir},
                clear=False,
            ):
                self.assertEqual(build_data.resolve_scraper_state_dir(), Path(tmpdir).resolve())

    def test_parser_uses_auto_refresh_worker_default(self) -> None:
        parser = build_data.build_parser()
        args = parser.parse_args([])

        self.assertEqual(args.refresh_workers, build_data.DEFAULT_REFRESH_WORKERS)

    def test_main_uses_photo_only_fast_path_for_refresh_photos(self) -> None:
        with (
            patch.object(sys, "argv", ["build_data.py", "--refresh-photos"]),
            patch.object(build_data, "refresh_generated_guide_photos", return_value=True) as refresh_photos,
            patch.object(build_data, "rebuild_generated_data") as rebuild_generated_data,
        ):
            result = build_data.main()

        self.assertEqual(result, 0)
        refresh_photos.assert_called_once_with(
            photo_workers=build_data.DEFAULT_REFRESH_WORKERS,
            startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )
        rebuild_generated_data.assert_not_called()

    def test_main_rejects_google_fallback_without_trust_refresh(self) -> None:
        stderr = StringIO()
        with (
            patch.object(sys, "argv", ["build_data.py", "--trust-google-fallback"]),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            build_data.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--trust-google-fallback requires --refresh-trust-signals", stderr.getvalue())

    def test_main_falls_back_to_full_rebuild_when_photo_only_fast_path_is_unavailable(self) -> None:
        with (
            patch.object(sys, "argv", ["build_data.py", "--refresh-photos"]),
            patch.object(build_data, "refresh_generated_guide_photos", return_value=False) as refresh_photos,
            patch.object(build_data, "rebuild_generated_data") as rebuild_generated_data,
        ):
            result = build_data.main()

        self.assertEqual(result, 0)
        refresh_photos.assert_called_once_with(
            photo_workers=build_data.DEFAULT_REFRESH_WORKERS,
            startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )
        rebuild_generated_data.assert_called_once_with(
            refresh_photos=True,
            photo_workers=build_data.DEFAULT_REFRESH_WORKERS,
            startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )

    def test_main_treats_enrich_place_as_targeted_refresh(self) -> None:
        with (
            patch.object(sys, "argv", ["build_data.py", "--enrich-place", "cid:123"]),
            patch.object(build_data, "enrichment_source_refresh_lists", return_value=[]),
            patch.object(build_data, "refresh_raw_sources") as refresh_raw_sources,
            patch.object(build_data, "sync_local_csv_sources"),
            patch.object(build_data, "enrich_raw_sources") as enrich_raw_sources,
            patch.object(build_data, "rebuild_generated_data") as rebuild_generated_data,
        ):
            result = build_data.main()

        self.assertEqual(result, 0)
        refresh_raw_sources.assert_not_called()
        enrich_raw_sources.assert_called_once_with(
            force_refresh=True,
            missing_only=False,
            place_selectors=["cid:123"],
            refresh_workers=build_data.DEFAULT_REFRESH_WORKERS,
            refresh_startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )
        rebuild_generated_data.assert_called_once_with(
            refresh_photos=False,
            photo_workers=build_data.DEFAULT_REFRESH_WORKERS,
            startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )

    def test_main_refreshes_affected_source_before_targeted_enrichment(self) -> None:
        with (
            patch.object(sys, "argv", ["build_data.py", "--enrich-place", "guide-slug:tokyo-japan"]),
            patch.object(build_data, "enrichment_source_refresh_lists", return_value=["tokyo-japan"]),
            patch.object(build_data, "refresh_raw_sources") as refresh_raw_sources,
            patch.object(build_data, "sync_local_csv_sources"),
            patch.object(build_data, "enrich_raw_sources") as enrich_raw_sources,
            patch.object(build_data, "rebuild_generated_data"),
        ):
            result = build_data.main()

        self.assertEqual(result, 0)
        refresh_raw_sources.assert_called_once_with(
            headed=False,
            force_refresh=True,
            refresh_lists=["tokyo-japan"],
            refresh_workers=build_data.DEFAULT_REFRESH_WORKERS,
            refresh_retries=build_data.DEFAULT_REFRESH_RETRIES,
            refresh_retry_backoff_seconds=build_data.DEFAULT_REFRESH_RETRY_BACKOFF_SECONDS,
            refresh_startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )
        enrich_raw_sources.assert_called_once()

    def test_main_can_skip_source_refresh_before_targeted_enrichment(self) -> None:
        with (
            patch.object(
                sys,
                "argv",
                [
                    "build_data.py",
                    "--skip-enrichment-source-refresh",
                    "--enrich-place",
                    "guide-slug:tokyo-japan",
                ],
            ),
            patch.object(build_data, "enrichment_source_refresh_lists") as refresh_lists,
            patch.object(build_data, "refresh_raw_sources") as refresh_raw_sources,
            patch.object(build_data, "sync_local_csv_sources"),
            patch.object(build_data, "enrich_raw_sources"),
            patch.object(build_data, "rebuild_generated_data"),
        ):
            result = build_data.main()

        self.assertEqual(result, 0)
        refresh_lists.assert_not_called()
        refresh_raw_sources.assert_not_called()

    def test_main_force_semantic_descriptions_runs_cache_only_pass(self) -> None:
        with (
            patch.object(sys, "argv", ["build_data.py", "--force-semantic-descriptions", "--enrich-place", "cid:123"]),
            patch.object(build_data, "sync_local_csv_sources") as sync_local_csv_sources,
            patch.object(build_data, "enrich_raw_sources") as enrich_raw_sources,
            patch.object(build_data, "refresh_cached_semantic_enrichment") as refresh_semantics,
            patch.object(build_data, "rebuild_generated_data") as rebuild_generated_data,
        ):
            result = build_data.main()

        self.assertEqual(result, 0)
        sync_local_csv_sources.assert_called_once()
        enrich_raw_sources.assert_not_called()
        refresh_semantics.assert_called_once_with(
            enable_semantics=False,
            enable_description=True,
            force_semantics=False,
            force_description=True,
            place_selectors=["cid:123"],
        )
        rebuild_generated_data.assert_called_once_with(
            refresh_photos=False,
            photo_workers=build_data.DEFAULT_REFRESH_WORKERS,
            startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )

    def test_main_semantic_refresh_with_photos_runs_full_rebuild(self) -> None:
        with (
            patch.object(sys, "argv", ["build_data.py", "--refresh-semantic-descriptions", "--refresh-photos"]),
            patch.object(build_data, "sync_local_csv_sources"),
            patch.object(build_data, "refresh_cached_semantic_enrichment"),
            patch.object(build_data, "refresh_generated_guide_photos", return_value=True) as refresh_photos,
            patch.object(build_data, "rebuild_generated_data") as rebuild_generated_data,
        ):
            result = build_data.main()

        self.assertEqual(result, 0)
        refresh_photos.assert_not_called()
        rebuild_generated_data.assert_called_once_with(
            refresh_photos=True,
            photo_workers=build_data.DEFAULT_REFRESH_WORKERS,
            startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )

    def test_refresh_generated_guide_photos_skips_artifact_rewrite_when_photo_state_is_unchanged(self) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at=build_data.STABLE_GENERATED_AT_FALLBACK,
            place_count=1,
            places=[
                NormalizedPlace(
                    id="cid:123",
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=123",
                    main_photo_path="/place-photos/cid-123-existing.webp",
                    status="active",
                )
            ],
        )
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=123",
                    cid="123",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            generated_lists_dir = root / "generated" / "lists"
            raw_dir = root / "raw"
            generated_dir = root / "generated"
            public_data_dir = root / "public-data"
            place_photos_dir = root / "place-photos"
            generated_lists_dir.mkdir(parents=True, exist_ok=True)
            raw_dir.mkdir(parents=True, exist_ok=True)

            build_data.write_json(generated_lists_dir / "tokyo-japan.json", guide)
            build_data.write_json(raw_dir / "tokyo-japan.json", raw)

            with (
                patch.object(build_data, "GENERATED_LISTS_DIR", generated_lists_dir),
                patch.object(build_data, "GENERATED_DIR", generated_dir),
                patch.object(build_data, "PUBLIC_DATA_DIR", public_data_dir),
                patch.object(build_data, "PLACE_PHOTOS_DIR", place_photos_dir),
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "load_places_cache", return_value={}),
                patch.object(build_data, "populate_place_photos_for_guides"),
                patch.object(build_data, "rebuild_places_sqlite") as rebuild_places_sqlite,
            ):
                result = build_data.refresh_generated_guide_photos(
                    photo_workers=1,
                    startup_jitter_seconds=0,
                )

        self.assertTrue(result)
        rebuild_places_sqlite.assert_not_called()

    def test_refresh_generated_guide_photos_falls_back_when_raw_and_generated_slugs_diverge(self) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at=build_data.STABLE_GENERATED_AT_FALLBACK,
            place_count=1,
            places=[
                NormalizedPlace(
                    id="cid:123",
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=123",
                    main_photo_path="/place-photos/cid-123-existing.webp",
                    status="active",
                )
            ],
        )
        raw_tokyo = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=123",
                    cid="123",
                )
            ],
        )
        raw_osaka = RawSavedList(
            title="Osaka, Japan",
            places=[
                RawPlace(
                    name="Coffee Shop",
                    maps_url="https://maps.google.com/?cid=456",
                    cid="456",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            generated_lists_dir = root / "generated" / "lists"
            raw_dir = root / "raw"
            generated_dir = root / "generated"
            public_data_dir = root / "public-data"
            place_photos_dir = root / "place-photos"
            generated_lists_dir.mkdir(parents=True, exist_ok=True)
            raw_dir.mkdir(parents=True, exist_ok=True)

            build_data.write_json(generated_lists_dir / "tokyo-japan.json", guide)
            build_data.write_json(raw_dir / "tokyo-japan.json", raw_tokyo)
            build_data.write_json(raw_dir / "osaka-japan.json", raw_osaka)

            with (
                patch("builtins.print") as print_mock,
                patch.object(build_data, "GENERATED_LISTS_DIR", generated_lists_dir),
                patch.object(build_data, "GENERATED_DIR", generated_dir),
                patch.object(build_data, "PUBLIC_DATA_DIR", public_data_dir),
                patch.object(build_data, "PLACE_PHOTOS_DIR", place_photos_dir),
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "populate_place_photos_for_guides") as populate_place_photos,
            ):
                result = build_data.refresh_generated_guide_photos(
                    photo_workers=1,
                    startup_jitter_seconds=0,
                )

        self.assertFalse(result)
        populate_place_photos.assert_not_called()
        print_mock.assert_called_once_with(
            "WARNING: Generated and raw guide sets differ; falling back to a full rebuild.",
            flush=True,
        )

    def test_parser_rejects_negative_refresh_retry_values(self) -> None:
        invalid_args = [
            ["--refresh-retries", "-1"],
            ["--refresh-retry-backoff-seconds", "-0.1"],
            ["--refresh-startup-jitter-seconds", "-0.1"],
        ]

        for args in invalid_args:
            with self.subTest(args=args):
                parser = build_data.build_parser()
                stderr = StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    parser.parse_args(args)

                self.assertEqual(raised.exception.code, 2)
                self.assertIn("must be >= 0", stderr.getvalue())

    def test_source_config_infers_google_list_url_from_maps_shortlink(self) -> None:
        source = SourceConfig(slug="florence-italy", url="https://maps.app.goo.gl/mXQoUYRRjWuj6HNw8")

        self.assertEqual(source.type, "google_list_url")

    def test_source_config_accepts_matching_explicit_type(self) -> None:
        source = SourceConfig(
            slug="florence-italy",
            type="google_list_url",
            url="https://maps.app.goo.gl/mXQoUYRRjWuj6HNw8",
        )

        self.assertEqual(source.type, "google_list_url")

    def test_source_config_infers_google_list_url_from_google_maps_share_url(self) -> None:
        source = SourceConfig(
            slug="florence-italy",
            url=(
                "https://www.google.com/maps/@43.7704158,11.2457711,15z/"
                "data=!4m6!1m2!10m1!1e1!11m2!2sMc3UGukaEH6YkxkW4hLKEg!3e3"
            ),
        )

        self.assertEqual(source.type, "google_list_url")

    def test_source_config_infers_google_export_csv_from_path(self) -> None:
        source = SourceConfig(
            slug="taipei-taiwan",
            path="data/imports/taipei-taiwan.csv",
            title="Taipei, Taiwan",
        )

        self.assertEqual(source.type, "google_export_csv")

    def test_source_config_infers_wanderlog_view_url_from_url(self) -> None:
        source = SourceConfig(
            slug="hong-kong",
            url="https://wanderlog.com/view/otooauorfe/hong-kong-recommendations",
        )

        self.assertEqual(source.type, "wanderlog_view_url")

    def test_source_config_rejects_type_that_disagrees_with_maps_url(self) -> None:
        with self.assertRaises(ValidationError) as context:
            SourceConfig(
                slug="florence-italy",
                type="google_export_csv",
                url="https://maps.app.goo.gl/mXQoUYRRjWuj6HNw8",
            )

        self.assertIn("does not match configured source fields", str(context.exception))

    def test_source_config_rejects_type_that_disagrees_with_csv_path(self) -> None:
        with self.assertRaises(ValidationError) as context:
            SourceConfig(
                slug="taipei-taiwan",
                type="google_list_url",
                path="data/imports/taipei-taiwan.csv",
                title="Taipei, Taiwan",
            )

        self.assertIn("does not match configured source fields", str(context.exception))

    def test_source_config_rejects_non_google_url_for_google_list_url(self) -> None:
        with self.assertRaises(ValidationError) as context:
            SourceConfig(
                slug="example",
                type="google_list_url",
                url="https://example.com/list",
            )

        self.assertIn("supported Google Maps URL", str(context.exception))

    def test_source_config_rejects_non_wanderlog_url_for_wanderlog_view_url(self) -> None:
        with self.assertRaises(ValidationError) as context:
            SourceConfig(
                slug="example",
                type="wanderlog_view_url",
                url="https://example.com/list",
            )

        self.assertIn("supported Wanderlog view URL", str(context.exception))

    def test_parse_wanderlog_view_html_extracts_places(self) -> None:
        mobx_state = {
            "tripPlanStore": {
                "data": {
                    "seoDescription": "Michael Wu's travel guide for Hong Kong with 2 favorite places to visit",
                    "tripPlan": {
                        "name": "DEM Flyers Hong Kong recommendations",
                        "itinerary": {
                            "sections": [
                                {
                                    "heading": "Restaurants",
                                    "blocks": [
                                        {
                                            "type": "place",
                                            "rating": "mustGo",
                                            "text": {
                                                "ops": [
                                                    {
                                                        "insert": (
                                                            "Order the pineapple bun and milk tea.\n"
                                                        )
                                                    }
                                                ]
                                            },
                                            "reactions": {"❤️": [1667], "🍵": [1667]},
                                            "imageKeys": ["sample-image-key"],
                                            "place": {
                                                "name": "Kam Wah Cafe",
                                                "place_id": "ChIJ1234567890",
                                                "formatted_address": "47 Bute St, Mong Kok, Hong Kong",
                                                "rating": 4.2,
                                                "user_ratings_total": 1234,
                                                "price_level": 1,
                                                "types": ["restaurant", "chinese_tea_house"],
                                                "business_status": "OPERATIONAL",
                                                "geometry": {
                                                    "location": {
                                                        "lat": 22.3201,
                                                        "lng": 114.1702,
                                                    }
                                                },
                                                "url": "https://maps.google.com/?cid=1234567890",
                                            },
                                        },
                                        {
                                            "type": "place",
                                            "place": {
                                                "name": "Nan Lian Garden",
                                                "place_id": "ChIJABCDEFGHIJ",
                                                "formatted_address": "60 Fung Tak Rd, Diamond Hill, Hong Kong",
                                                "geometry": {
                                                    "location": {
                                                        "lat": 22.339611,
                                                        "lng": 114.204515,
                                                    }
                                                },
                                            },
                                        },
                                    ],
                                }
                            ]
                        },
                    },
                }
            }
        }
        html = (
            "<html><head>"
            "<title>DEM Flyers Hong Kong recommendations – Wanderlog</title>"
            "<meta name=\"description\" "
            "content=\"Michael Wu&#x27;s travel guide for Hong Kong with 2 favorite places to visit\">"
            "</head><body><script>"
            f"window.__MOBX_STATE__ = {json.dumps(mobx_state)};\n"
            "window.__CONFIG__ = {};\n"
            "</script></body></html>"
        )

        payload = build_data.parse_wanderlog_view_html(
            "https://wanderlog.com/view/otooauorfe/hong-kong-recommendations",
            html,
        )

        self.assertEqual(payload.source_url, "https://wanderlog.com/view/otooauorfe/hong-kong-recommendations")
        self.assertEqual(payload.list_id, "otooauorfe")
        self.assertEqual(payload.title, "DEM Flyers Hong Kong recommendations")
        self.assertEqual(
            payload.description,
            "Michael Wu's travel guide for Hong Kong with 2 favorite places to visit",
        )
        self.assertEqual(len(payload.places), 2)
        self.assertEqual(payload.places[0].name, "Kam Wah Cafe")
        self.assertEqual(payload.places[0].google_id, "ChIJ1234567890")
        self.assertEqual(payload.places[0].maps_url, "https://maps.google.com/?cid=1234567890")
        self.assertTrue(payload.places[0].is_favorite)
        self.assertEqual(payload.places[0].rating, 4.2)
        self.assertEqual(payload.places[0].user_rating_count, 1234)
        self.assertEqual(payload.places[0].price_level, 1)
        self.assertEqual(payload.places[0].types, ["restaurant", "chinese_tea_house"])
        self.assertEqual(payload.places[0].business_status, "OPERATIONAL")
        self.assertEqual(
            payload.places[0].photo_url,
            "https://itin-dev.wanderlogstatic.com/freeImageMedium/sample-image-key",
        )
        self.assertEqual(payload.places[0].note, "Order the pineapple bun and milk tea.\n❤️ 🍵")
        self.assertEqual(payload.places[1].name, "Nan Lian Garden")
        self.assertEqual(payload.places[1].google_id, "ChIJABCDEFGHIJ")
        self.assertEqual(
            payload.places[1].maps_url,
            (
                "https://www.google.com/maps/search/?api=1&query="
                "Nan+Lian+Garden%2C+60+Fung+Tak+Rd%2C+Diamond+Hill%2C+Hong+Kong&"
                "query_place_id=ChIJABCDEFGHIJ"
            ),
        )

    def test_parse_wanderlog_view_html_preserves_reactions_without_text_note(self) -> None:
        mobx_state = {
            "tripPlanStore": {
                "data": {
                    "tripPlan": {
                        "name": "Hong Kong reactions",
                        "itinerary": {
                            "sections": [
                                {
                                    "blocks": [
                                        {
                                            "type": "place",
                                            "text": {"ops": [{"insert": "\n"}]},
                                            "rating": "mustGo",
                                            "reactions": {"⭐": [1]},
                                            "place": {
                                                "name": "Australia Dairy Company",
                                                "place_id": "ChIJREACTIONONLY",
                                                "formatted_address": "47 Parkes St, Jordan, Hong Kong",
                                                "url": "https://maps.google.com/?cid=18038164721409490895",
                                            },
                                        }
                                    ]
                                }
                            ]
                        },
                    }
                }
            }
        }
        html = (
            "<html><head><title>Hong Kong reactions – Wanderlog</title></head><body><script>"
            f"window.__MOBX_STATE__ = {json.dumps(mobx_state, ensure_ascii=False)};\n"
            "window.__CONFIG__ = {};\n"
            "</script></body></html>"
        )

        payload = build_data.parse_wanderlog_view_html(
            "https://wanderlog.com/view/reactions/hong-kong-reactions",
            html,
        )

        self.assertEqual(payload.places[0].note, "⭐")
        self.assertTrue(payload.places[0].is_favorite)

    def test_parse_wanderlog_view_html_strips_from_the_web_prefix(self) -> None:
        mobx_state = {
            "tripPlanStore": {
                "data": {
                    "tripPlan": {
                        "name": "Hong Kong notes",
                        "itinerary": {
                            "sections": [
                                {
                                    "blocks": [
                                        {
                                            "type": "place",
                                            "text": {
                                                "ops": [
                                                    {
                                                        "insert": (
                                                            "From the web: Historic district of narrow streets "
                                                            "& alleys lined with restaurants.\n"
                                                        )
                                                    }
                                                ]
                                            },
                                            "place": {
                                                "name": "Lan Kwai Fong",
                                                "place_id": "ChIJLAN",
                                                "formatted_address": "Central, Hong Kong",
                                                "types": ["tourist_attraction", "point_of_interest"],
                                                "url": "https://maps.google.com/?cid=1",
                                            },
                                        }
                                    ]
                                }
                            ]
                        },
                    }
                }
            }
        }
        html = (
            "<html><head><title>Hong Kong notes – Wanderlog</title></head><body><script>"
            f"window.__MOBX_STATE__ = {json.dumps(mobx_state, ensure_ascii=False)};\n"
            "window.__CONFIG__ = {};\n"
            "</script></body></html>"
        )

        payload = build_data.parse_wanderlog_view_html(
            "https://wanderlog.com/view/notes/hong-kong-notes",
            html,
        )

        self.assertEqual(
            payload.places[0].note,
            "Historic district of narrow streets & alleys lined with restaurants.",
        )

    def test_parse_wanderlog_view_html_drops_route_note_without_place_name(self) -> None:
        mobx_state = {
            "tripPlanStore": {
                "data": {
                    "tripPlan": {
                        "name": "Hong Kong route notes",
                        "itinerary": {
                            "sections": [
                                {
                                    "blocks": [
                                        {
                                            "type": "place",
                                            "text": {
                                                "ops": [
                                                    {
                                                        "insert": (
                                                            "From the web: Central is Hong Kong's business and "
                                                            "retail heart with nightlife in nearby SoHo.\n"
                                                        )
                                                    }
                                                ]
                                            },
                                            "place": {
                                                "name": "Stanley Street",
                                                "place_id": "ChIJSTANLEY",
                                                "formatted_address": "Stanley St, Central, Hong Kong",
                                                "types": ["route"],
                                                "url": "https://maps.google.com/?cid=2",
                                            },
                                        }
                                    ]
                                }
                            ]
                        },
                    }
                }
            }
        }
        html = (
            "<html><head><title>Hong Kong route notes – Wanderlog</title></head><body><script>"
            f"window.__MOBX_STATE__ = {json.dumps(mobx_state, ensure_ascii=False)};\n"
            "window.__CONFIG__ = {};\n"
            "</script></body></html>"
        )

        payload = build_data.parse_wanderlog_view_html(
            "https://wanderlog.com/view/routes/hong-kong-route-notes",
            html,
        )

        self.assertIsNone(payload.places[0].note)

    def test_source_config_rejects_google_mymaps_url(self) -> None:
        with self.assertRaises(ValidationError) as context:
            SourceConfig(
                slug="valencia-spain",
                url=(
                    "https://www.google.com/maps/d/u/0/viewer?hl=en"
                    "&mid=1OiyJQ0xcbnanQ2QJsTavzetf5b_D_s4"
                ),
            )

        self.assertIn("Google My Maps URLs are not supported", str(context.exception))

    def test_preserve_existing_raw_saved_list_keeps_stronger_prior_place_fields(self) -> None:
        existing_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="Duke's Waikiki",
                    address="2335 Kalākaua Ave #116, Honolulu, HI 96815, United States",
                    is_favorite=True,
                    lat=21.2769032,
                    lng=-157.8278887,
                    maps_url="https://www.google.com/maps/search/?api=1&query=Duke%27s+Waikiki",
                    cid="8935267511126082507",
                    google_id="/g/1tdwf48w",
                    maps_place_token="0xdeadbeef:0x1",
                    added_by=ListAuthor(name="Second Curator", profile_id="second-id"),
                )
            ],
        )
        refreshed_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="Duke's Waikiki",
                    address=None,
                    is_favorite=False,
                    lat=21.2769032,
                    lng=-157.8278887,
                    maps_url="https://www.google.com/maps/search/?api=1&query=Duke%27s+Waikiki",
                    cid="8935267511126082507",
                    google_id=None,
                    maps_place_token=None,
                    added_by=None,
                )
            ],
        )

        merged = build_data.preserve_existing_raw_saved_list(
            slug="oahu-hawaii-usa",
            existing_payload=existing_payload,
            refreshed_payload=refreshed_payload,
        )

        self.assertEqual(
            merged.places[0].address,
            "2335 Kalākaua Ave #116, Honolulu, HI 96815, United States",
        )
        self.assertEqual(merged.places[0].google_id, "/g/1tdwf48w")
        self.assertEqual(merged.places[0].maps_place_token, "0xdeadbeef:0x1")
        self.assertEqual(merged.places[0].added_by, ListAuthor(name="Second Curator", profile_id="second-id"))
        self.assertTrue(merged.places[0].is_favorite)

    def test_preserve_existing_raw_saved_list_does_not_apply_to_non_matching_place(self) -> None:
        existing_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="Old Place",
                    address="1 Example St",
                    lat=35.0,
                    lng=139.0,
                    maps_url="https://www.google.com/maps/search/?api=1&query=Old+Place",
                    cid="111",
                    google_id="/g/old",
                )
            ],
        )
        refreshed_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="New Place",
                    address=None,
                    lat=36.0,
                    lng=140.0,
                    maps_url="https://www.google.com/maps/search/?api=1&query=New+Place",
                    cid="222",
                    google_id=None,
                )
            ],
        )

        merged = build_data.preserve_existing_raw_saved_list(
            slug="tokyo-japan",
            existing_payload=existing_payload,
            refreshed_payload=refreshed_payload,
        )

        self.assertIsNone(merged.places[0].address)
        self.assertIsNone(merged.places[0].google_id)

    def test_preserve_existing_raw_saved_list_skips_fields_when_names_do_not_match(self) -> None:
        existing_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="Michael's list for Seoul, Korea",
                    address="BONTÉ 본태",
                    lat=37.0,
                    lng=127.0,
                    maps_url="https://www.google.com/maps/search/?api=1&query=BONT%C3%89",
                    cid="333",
                )
            ],
        )
        refreshed_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="BONTÉ 본태",
                    address=None,
                    lat=37.0,
                    lng=127.0,
                    maps_url="https://www.google.com/maps/search/?api=1&query=BONT%C3%89",
                    cid="333",
                )
            ],
        )

        merged = build_data.preserve_existing_raw_saved_list(
            slug="seoul-korea",
            existing_payload=existing_payload,
            refreshed_payload=refreshed_payload,
        )

        self.assertIsNone(merged.places[0].address)

    def test_preserve_existing_raw_saved_list_skips_note_like_address_restore(self) -> None:
        existing_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="Gorat's Steakhouse",
                    address="Warren Buffet's favorite steakhouse",
                    note="Warren Buffet's favorite steakhouse",
                    lat=41.2412347,
                    lng=-95.9886973,
                    maps_url="https://www.google.com/maps/search/?api=1&query=Gorat%27s+Steakhouse",
                    cid="10697208099166303129",
                )
            ],
        )
        refreshed_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="Gorat's Steakhouse",
                    address=None,
                    note="Warren Buffet's favorite steakhouse",
                    lat=41.2412347,
                    lng=-95.9886973,
                    maps_url="https://www.google.com/maps/search/?api=1&query=Gorat%27s+Steakhouse",
                    cid="10697208099166303129",
                )
            ],
        )

        merged = build_data.preserve_existing_raw_saved_list(
            slug="omaha-nebraska-usa",
            existing_payload=existing_payload,
            refreshed_payload=refreshed_payload,
        )

        self.assertIsNone(merged.places[0].address)

    def test_preserve_existing_raw_saved_list_skips_generic_locality_address_restore(self) -> None:
        existing_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="青湾情人海灘",
                    address="台湾 澎湖縣馬公市嵵裡里",
                    lat=23.5319641,
                    lng=119.553573,
                    maps_url="https://www.google.com/maps/search/?api=1&query=%E9%9D%92%E6%B9%BE%E6%83%85%E4%BA%BA%E6%B5%B7%E7%81%98",
                    cid="3777482742712049795",
                    google_id="/g/11x9_8ngd",
                )
            ],
        )
        refreshed_payload = RawSavedList(
            configured_source_type="google_list_url",
            places=[
                RawPlace(
                    name="青湾情人海灘",
                    address=None,
                    lat=23.5319641,
                    lng=119.553573,
                    maps_url="https://www.google.com/maps/search/?api=1&query=%E9%9D%92%E6%B9%BE%E6%83%85%E4%BA%BA%E6%B5%B7%E7%81%98",
                    cid="3777482742712049795",
                    google_id="/g/11x9_8ngd",
                )
            ],
        )

        merged = build_data.preserve_existing_raw_saved_list(
            slug="penghu-taiwan",
            existing_payload=existing_payload,
            refreshed_payload=refreshed_payload,
        )

        self.assertIsNone(merged.places[0].address)
        self.assertEqual(merged.places[0].google_id, "/g/11x9_8ngd")

    def test_preserve_existing_raw_saved_list_skips_cross_source_signature_reuse(self) -> None:
        previous_source = SourceConfig(
            slug="los-angeles-california-usa",
            url="https://maps.app.goo.gl/old-list",
        )
        current_source = SourceConfig(
            slug="los-angeles-california-usa",
            url="https://maps.app.goo.gl/new-list",
        )
        existing_payload = RawSavedList(
            configured_source_type="google_list_url",
            source_signature=build_data.raw_source_signature(previous_source),
            places=[
                RawPlace(
                    name="Global Village",
                    address="38C5+F57 - Wadi Al Safa 4 - Dubai - United Arab Emirates",
                    is_favorite=True,
                    lat=25.0716887,
                    lng=55.3084347,
                    maps_url="https://www.google.com/maps/search/?api=1&query=Global+Village",
                    cid="4494433615859482133",
                    google_id="/g/11oldsource",
                    maps_place_token="0xold:0x1",
                )
            ],
        )
        refreshed_payload = RawSavedList(
            configured_source_type="google_list_url",
            source_signature=build_data.raw_source_signature(current_source),
            places=[
                RawPlace(
                    name="Global Village",
                    address=None,
                    is_favorite=False,
                    lat=25.0716887,
                    lng=55.3084347,
                    maps_url="https://www.google.com/maps/search/?api=1&query=Global+Village",
                    cid=None,
                    google_id=None,
                    maps_place_token=None,
                )
            ],
        )

        merged = build_data.preserve_existing_raw_saved_list(
            source=current_source,
            slug="los-angeles-california-usa",
            existing_payload=existing_payload,
            refreshed_payload=refreshed_payload,
        )

        self.assertIsNone(merged.places[0].address)
        self.assertIsNone(merged.places[0].google_id)
        self.assertIsNone(merged.places[0].cid)
        self.assertIsNone(merged.places[0].maps_place_token)
        self.assertFalse(merged.places[0].is_favorite)

    def test_build_place_page_candidate_urls_prefers_search_for_cid_inputs(self) -> None:
        place = RawPlace(
            name="Sister Midnight",
            address="4 Rue Viollet-le-Duc, 75009 Paris, France",
            maps_url="https://maps.google.com/?cid=5180951040094558101",
        )

        self.assertEqual(
            build_data.build_place_page_candidate_urls(place),
            [
                (
                    "https://www.google.com/maps/search/?api=1&query="
                    "Sister+Midnight%2C+4+Rue+Viollet-le-Duc%2C+75009+Paris%2C+France"
                    "&hl=en&gl=us"
                ),
                "https://maps.google.com/?cid=5180951040094558101&hl=en&gl=us",
            ],
        )

    def test_build_place_page_candidate_urls_adds_cid_fallback_for_search_urls(self) -> None:
        place = RawPlace(
            name="Swagat Wine & Dine",
            address="Bau St, Suva, Fiji",
            maps_url=(
                "https://www.google.com/maps/search/?api=1"
                "&query=Swagat+Wine+%26+Dine%2C+Bau+St%2C+Suva%2C+Fiji"
            ),
            cid="15264186070483398692",
        )

        self.assertEqual(
            build_data.build_place_page_candidate_urls(place),
            [
                (
                    "https://www.google.com/maps/search/?api=1"
                    "&query=Swagat+Wine+%26+Dine%2C+Bau+St%2C+Suva%2C+Fiji&hl=en&gl=us"
                ),
                "https://maps.google.com/?cid=15264186070483398692&hl=en&gl=us",
            ],
        )

    def test_build_place_page_candidate_urls_replaces_name_only_search_urls_with_city_country_bias(self) -> None:
        place = RawPlace(
            name="Locale",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Locale",
            cid="6924437521980544303",
            lat=35.636208,
            lng=139.709467,
        )

        self.assertEqual(
            build_data.build_place_page_candidate_urls(
                place,
                city_name="Tokyo",
                country_name="Japan",
            ),
            [
                "https://maps.google.com/?cid=6924437521980544303&hl=en&gl=us",
                (
                    "https://www.google.com/maps/search/?api=1"
                    "&query=Locale%2C+Tokyo%2C+Japan&hl=en&gl=us"
                ),
                "https://www.google.com/maps/search/?api=1&query=Locale&hl=en&gl=us",
            ],
        )

    def test_build_place_page_candidate_urls_keeps_fallbacks_when_google_place_id_is_present(self) -> None:
        place = RawPlace(
            name="Cantina OK!",
            address="Council Pl, Sydney NSW 2000, Australia",
            maps_url="https://maps.google.com/?cid=7715422616180689913",
            cid="7715422616180689913",
        )

        self.assertEqual(
            build_data.build_place_page_candidate_urls(
                place,
                google_place_id="ChIJGcmcg7ZC1moRAOacd3HoEwM",
            ),
            [
                (
                    "https://www.google.com/maps/search/?api=1"
                    "&query=Cantina+OK%21%2C+Council+Pl%2C+Sydney+NSW+2000%2C+Australia"
                    "&query_place_id=ChIJGcmcg7ZC1moRAOacd3HoEwM&hl=en&gl=us"
                ),
                (
                    "https://www.google.com/maps/search/?api=1"
                    "&query=Cantina+OK%21%2C+Council+Pl%2C+Sydney+NSW+2000%2C+Australia&hl=en&gl=us"
                ),
                "https://maps.google.com/?cid=7715422616180689913&hl=en&gl=us",
            ],
        )

    def test_localize_google_maps_scrape_url_forces_english_locale(self) -> None:
        self.assertEqual(
            build_data.localize_google_maps_scrape_url(
                "https://www.google.com/maps/place/Test+Place?entry=ttu&hl=ja"
            ),
            "https://www.google.com/maps/place/Test+Place?entry=ttu&hl=en&gl=us",
        )
        self.assertEqual(
            build_data.localize_google_maps_scrape_url("https://example.com/place/Test+Place?hl=ja"),
            "https://example.com/place/Test+Place?hl=ja",
        )

    def test_fetch_place_page_enrichment_prefers_search_candidate_for_cid_inputs(self) -> None:
        place = RawPlace(
            name="Sister Midnight",
            address="4 Rue Viollet-le-Duc, 75009 Paris, France",
            maps_url="https://maps.google.com/?cid=5180951040094558101",
            lat=48.8814703,
            lng=2.340862,
        )
        called_urls: list[str] = []

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            called_urls.append(url)
            return SimpleNamespace(
                source_url=url,
                resolved_url=(
                    "https://www.google.com/maps/place/Sister+Midnight/"
                    "@48.8814703,2.340862,17z/data=!3m1!4b1"
                ),
                name="Sister Midnight",
                category="Cocktail bar",
                rating=4.7,
                review_count=321,
                address="4 Rue Viollet-le-Duc, 75009 Paris, France",
                located_in=None,
                status=None,
                website="https://sistermidnightparis.com/",
                phone="+33 1 42 00 00 00",
                plus_code=None,
                description=None,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertEqual(
            called_urls,
            [
                (
                    "https://www.google.com/maps/search/?api=1&query="
                    "Sister+Midnight%2C+4+Rue+Viollet-le-Duc%2C+75009+Paris%2C+France"
                    "&hl=en&gl=us"
                )
            ],
        )
        self.assertTrue(entry.matched)
        self.assertIsNotNone(entry.place)
        assert entry.place is not None
        self.assertEqual(entry.place.display_name, "Sister Midnight")
        self.assertEqual(entry.place.primary_type_display_name, "Cocktail bar")

    def test_fetch_place_page_enrichment_uses_city_country_bias_for_name_only_search_urls(self) -> None:
        place = RawPlace(
            name="Locale",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Locale",
            cid="6924437521980544303",
            lat=35.636208,
            lng=139.709467,
        )
        called_urls: list[str] = []

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            called_urls.append(url)
            return SimpleNamespace(
                source_url=url,
                resolved_url="https://www.google.com/maps/place/Locale/@35.636208,139.709467,17z",
                name="Locale",
                category="Cafe",
                rating=4.4,
                review_count=215,
                address="1 Chome-19-14 Aobadai, Meguro City, Tokyo 153-0042, Japan",
                located_in=None,
                status=None,
                website="https://locale.jp/",
                phone="+81 3-1234-5678",
                plus_code=None,
                description=None,
                lat=35.636208,
                lng=139.709467,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Tokyo",
                country_name="Japan",
            )

        self.assertEqual(
            called_urls,
            ["https://maps.google.com/?cid=6924437521980544303&hl=en&gl=us"],
        )
        self.assertTrue(entry.matched)
        self.assertIsNotNone(entry.place)
        self.assertEqual(entry.query, "Locale, Tokyo, Japan")

    def test_fetch_place_page_enrichment_scopes_scraper_session_by_locality(self) -> None:
        place = RawPlace(
            name="Tokyo Tower",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Tokyo+Tower",
        )

        details = SimpleNamespace(
            source_url=(
                "https://www.google.com/maps/search/?api=1"
                "&query=Tokyo+Tower%2C+Tokyo%2C+Japan&hl=en&gl=us"
            ),
            resolved_url="https://www.google.com/maps/place/Tokyo+Tower/@35.6585805,139.7454329,17z",
            name="Tokyo Tower",
            category="Observation deck",
            rating=4.5,
            review_count=1000,
            address="4 Chome-2-8 Shibakoen, Minato City, Tokyo 105-0011, Japan",
            status=None,
            website=None,
            phone=None,
            plus_code=None,
            description=None,
            lat=35.6585805,
            lng=139.7454329,
            limited_view=False,
        )

        with (
            patch.object(build_data, "current_scraper_proxy", return_value=None),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ) as build_sessions,
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
            patch.object(build_data, "scrape_place", return_value=details),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Tokyo",
                country_name="Japan",
            )

        self.assertTrue(entry.matched)
        build_sessions.assert_called_once_with(
            None,
            session_scope="place-Japan-Tokyo",
        )

    def test_fetch_place_page_enrichment_uses_dom_repair_without_optional_panels(self) -> None:
        place = RawPlace(
            name="Locale",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Locale",
        )
        repairer = object()
        captured_kwargs: dict[str, object] = {}

        def fake_scrape_place(url: str, **kwargs: object) -> SimpleNamespace:
            captured_kwargs.update(kwargs)
            return SimpleNamespace(
                source_url=url,
                resolved_url="https://www.google.com/maps/place/Locale/@35.636208,139.709467,17z",
                name="Locale",
                category="Cafe",
                rating=4.4,
                review_count=215,
                address="1 Chome-19-14 Aobadai, Meguro City, Tokyo 153-0042, Japan",
                status=None,
                website="https://locale.jp/",
                phone="+81 3-1234-5678",
                plus_code=None,
                description=None,
                lat=35.636208,
                lng=139.709467,
                limited_view=False,
            )

        with (
            patch.object(build_data, "google_maps_place_llm_repair_mode", return_value="dom"),
            patch.object(build_data, "build_place_llm_repairer", return_value=repairer),
            patch.object(build_data, "google_maps_place_collect_reviews", return_value=False),
            patch.object(build_data, "google_maps_place_collect_about", return_value=False),
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Tokyo",
                country_name="Japan",
            )

        self.assertTrue(entry.matched)
        self.assertIs(captured_kwargs["llm_fallback"], repairer)
        self.assertEqual(captured_kwargs["llm_tasks"], ("dom_repair",))
        self.assertFalse(captured_kwargs["collect_reviews"])
        self.assertFalse(captured_kwargs["collect_about"])

    def test_fetch_place_page_enrichment_repairs_display_fields_without_rescraping(self) -> None:
        place = RawPlace(
            name="Tea House",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Tea+House",
        )
        repairer = object()
        captured_tasks: list[object] = []
        repair_evidence: dict[str, object] = {}

        def fake_scrape_place(url: str, **kwargs: object) -> SimpleNamespace:
            captured_tasks.append(kwargs["llm_tasks"])
            return SimpleNamespace(
                source_url=url,
                resolved_url="https://www.google.com/maps/place/Tea+House/@25.1,121.5,17z",
                name="Tea House",
                category="茶館",
                rating=4.5,
                review_count=110,
                address="No. 12號, Songgao Rd, Xinyi District, Taipei City",
                status=None,
                website="https://tea.example/",
                phone=None,
                plus_code=None,
                description=None,
                lat=25.1,
                lng=121.5,
                limited_view=False,
            )

        def fake_repair_place_display_fields(
            details: SimpleNamespace,
            *,
            repairer: object,
            evidence: dict[str, object],
        ) -> SimpleNamespace:
            repair_evidence.update(evidence)
            details.category_display_en = "Tea house"
            details.category_display_en_source = "llm"
            details.category_display_en_confidence = "high"
            details.address_display_en = "No. 12, Songgao Rd, Xinyi District, Taipei City"
            details.address_display_en_source = "llm"
            details.address_display_en_confidence = "high"
            return details

        with (
            patch.object(build_data, "google_maps_place_llm_repair_mode", return_value="dom_then_translation"),
            patch.object(build_data, "build_place_llm_repairer", return_value=repairer),
            patch.object(
                build_data,
                "repair_place_display_fields",
                side_effect=fake_repair_place_display_fields,
            ) as repair_display,
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Taipei",
                country_name="Taiwan",
            )

        self.assertEqual(captured_tasks, [("dom_repair",)])
        repair_display.assert_called_once()
        self.assertIs(repair_display.call_args.kwargs["repairer"], repairer)
        self.assertEqual(
            repair_evidence,
            {"city": "Taipei", "country": "Taiwan", "query": "Tea House, Taipei, Taiwan"},
        )
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(entry.place.address_display_en, "No. 12, Songgao Rd, Xinyi District, Taipei City")
        self.assertEqual(entry.place.primary_type_display_name, "Tea house")
        self.assertEqual(entry.place.primary_type_display_name_localized, "茶館")

    def test_fetch_place_page_enrichment_repairs_display_fields_on_sparse_retry(self) -> None:
        place = RawPlace(
            name="Tea House",
            address="Taipei, Taiwan",
            maps_url="https://www.google.com/maps/place/Tea+House",
        )
        repairer = object()
        first = SimpleNamespace(
            source_url=place.maps_url,
            resolved_url=place.maps_url,
            name="Tea House",
            category="Cafe",
            rating=None,
            review_count=None,
            address="Taipei, Taiwan",
            status=None,
            website=None,
            phone=None,
            plus_code=None,
            description=None,
            limited_view=True,
        )
        retry = SimpleNamespace(
            source_url=place.maps_url,
            resolved_url="https://www.google.com/maps/place/Tea+House/@25.1,121.5,17z",
            name="Tea House",
            category="茶館",
            rating=4.5,
            review_count=110,
            address="No. 12號, Songgao Rd, Xinyi District, Taipei City",
            status=None,
            website="https://tea.example/",
            phone=None,
            plus_code=None,
            description=None,
            limited_view=False,
        )

        def fake_repair_place_display_fields(
            details: SimpleNamespace,
            *,
            repairer: object,
            evidence: dict[str, object],
        ) -> SimpleNamespace:
            details.category_display_en = "Tea house"
            details.category_display_en_source = "llm"
            details.category_display_en_confidence = "high"
            details.address_display_en = "No. 12, Songgao Rd, Xinyi District, Taipei City"
            details.address_display_en_source = "llm"
            details.address_display_en_confidence = "high"
            return details

        with (
            patch.object(build_data, "google_maps_place_llm_repair_mode", return_value="dom_then_translation"),
            patch.object(build_data, "build_place_llm_repairer", return_value=repairer),
            patch.object(build_data, "needs_display_en", side_effect=lambda value: "號" in (value or "")),
            patch.object(
                build_data,
                "repair_place_display_fields",
                side_effect=fake_repair_place_display_fields,
            ) as repair_display,
            patch.object(build_data, "scrape_place", side_effect=[first, retry]),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "build_scraper_configs", return_value=(None, None)),
            patch.object(build_data, "clear_scraper_session_state"),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Taipei",
                country_name="Taiwan",
            )

        repair_display.assert_called_once()
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(entry.place.address_display_en, "No. 12, Songgao Rd, Xinyi District, Taipei City")
        self.assertEqual(entry.place.primary_type_display_name, "Tea house")
        self.assertEqual(entry.place.primary_type_display_name_localized, "茶館")

    def test_fetch_place_page_enrichment_reuses_previous_display_fields(self) -> None:
        place = RawPlace(
            name="Tea House",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Tea+House",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Tea House, Taipei, Taiwan",
            matched=True,
            place=EnrichmentPlace(
                display_name="Tea House",
                formatted_address="No. 12號, Songgao Rd, Xinyi District, Taipei City",
                address_display_en="No. 12, Songgao Rd, Xinyi District, Taipei City",
                address_display_en_source="llm",
                address_display_en_confidence="high",
                primary_type_display_name="Tea house",
                primary_type_display_name_localized="茶館",
                category_display_en="Tea house",
                category_display_en_source="llm",
                category_display_en_confidence="high",
            ),
        )
        captured_tasks: list[object] = []

        def fake_scrape_place(url: str, **kwargs: object) -> SimpleNamespace:
            captured_tasks.append(kwargs["llm_tasks"])
            return SimpleNamespace(
                source_url=url,
                resolved_url="https://www.google.com/maps/place/Tea+House/@25.1,121.5,17z",
                name="Tea House",
                category="茶館",
                rating=4.5,
                review_count=110,
                address="No. 12號, Songgao Rd, Xinyi District, Taipei City",
                status=None,
                website="https://tea.example/",
                phone=None,
                plus_code=None,
                description=None,
                lat=25.1,
                lng=121.5,
                limited_view=False,
            )

        with (
            patch.object(build_data, "google_maps_place_llm_repair_mode", return_value="dom_then_translation"),
            patch.object(build_data, "build_place_llm_repairer", return_value=object()),
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Taipei",
                country_name="Taiwan",
                existing_entry=existing_entry,
            )

        self.assertEqual(captured_tasks, [("dom_repair",)])
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(entry.place.address_display_en, "No. 12, Songgao Rd, Xinyi District, Taipei City")
        self.assertEqual(entry.place.category_display_en, "Tea house")

    def test_fetch_places_api_enrichment_uses_city_country_bias_for_name_only_search_urls(self) -> None:
        place = RawPlace(
            name="Bilmonte",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Bilmonte",
            cid="1343378048703211865",
            lat=41.3894089,
            lng=2.1636435,
        )
        captured_request_body: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return b'{"places":[]}'

        def fake_urlopen(request, timeout: int = 20):  # type: ignore[no-untyped-def]
            self.assertEqual(timeout, 20)
            captured_request_body.update(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with patch.object(build_data, "urlopen", side_effect=fake_urlopen):
            entry = build_data.fetch_places_api_enrichment(
                place,
                city_name="Barcelona",
                country_name="Spain",
                api_key="test-key",
            )

        self.assertEqual(
            captured_request_body["textQuery"],
            "Bilmonte, Barcelona, Spain",
        )
        self.assertEqual(entry.query, "Bilmonte, Barcelona, Spain")
        self.assertFalse(entry.matched)

    def test_fetch_place_page_enrichment_falls_back_to_canonical_url_for_weak_search_match(self) -> None:
        place = RawPlace(
            name="Sister Midnight",
            address="4 Rue Viollet-le-Duc, 75009 Paris, France",
            maps_url="https://maps.google.com/?cid=5180951040094558101",
            lat=48.8814703,
            lng=2.340862,
        )
        called_urls: list[str] = []

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            called_urls.append(url)
            if "/maps/search/" in url:
                return SimpleNamespace(
                    source_url=url,
                    resolved_url="https://www.google.com/maps/place/Another+Bar/@48.88,2.34,17z",
                    name="Another Bar",
                    category="Cocktail bar",
                    rating=4.7,
                    review_count=321,
                    address="1 Different Street, 75009 Paris, France",
                    located_in=None,
                    status=None,
                    website="https://wrong.example/",
                    phone="+33 1 42 00 00 00",
                    plus_code=None,
                    description=None,
                    lat=48.8905,
                    lng=2.3305,
                    limited_view=False,
                )
            return SimpleNamespace(
                source_url=url,
                resolved_url=(
                    "https://www.google.com/maps/place/Sister+Midnight/"
                    "@48.8814703,2.340862,17z/data=!3m1!4b1"
                ),
                name="Sister Midnight",
                category="Cocktail bar",
                rating=4.7,
                review_count=321,
                address="4 Rue Viollet-le-Duc, 75009 Paris, France",
                located_in=None,
                status=None,
                website="https://sistermidnightparis.com/",
                phone="+33 1 42 00 00 00",
                plus_code=None,
                description=None,
                lat=48.8814703,
                lng=2.340862,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertEqual(
            called_urls,
            [
                (
                    "https://www.google.com/maps/search/?api=1&query="
                    "Sister+Midnight%2C+4+Rue+Viollet-le-Duc%2C+75009+Paris%2C+France"
                    "&hl=en&gl=us"
                ),
                "https://maps.google.com/?cid=5180951040094558101&hl=en&gl=us",
            ],
        )
        self.assertTrue(entry.matched)
        self.assertIsNotNone(entry.place)
        assert entry.place is not None
        self.assertEqual(entry.place.display_name, "Sister Midnight")

    def test_fetch_place_page_enrichment_skips_semantics_for_rejected_candidates(self) -> None:
        place = RawPlace(
            name="Sister Midnight",
            address="4 Rue Viollet-le-Duc, 75009 Paris, France",
            maps_url="https://maps.google.com/?cid=5180951040094558101",
            lat=48.8814703,
            lng=2.340862,
        )

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            if "/maps/search/" in url:
                return SimpleNamespace(
                    source_url=url,
                    resolved_url="https://www.google.com/maps/place/Another+Bar/@48.88,2.34,17z",
                    name="Another Bar",
                    category="Cocktail bar",
                    rating=4.7,
                    review_count=321,
                    address="1 Different Street, 75009 Paris, France",
                    located_in=None,
                    status=None,
                    website="https://wrong.example/",
                    phone="+33 1 42 00 00 00",
                    plus_code=None,
                    description=None,
                    lat=48.8905,
                    lng=2.3305,
                    limited_view=False,
                )
            return SimpleNamespace(
                source_url=url,
                resolved_url=(
                    "https://www.google.com/maps/place/Sister+Midnight/"
                    "@48.8814703,2.340862,17z/data=!3m1!4b1"
                ),
                name="Sister Midnight",
                category="Cocktail bar",
                rating=4.7,
                review_count=321,
                address="4 Rue Viollet-le-Duc, 75009 Paris, France",
                located_in=None,
                status=None,
                website="https://sistermidnightparis.com/",
                phone="+33 1 42 00 00 00",
                plus_code=None,
                description=None,
                lat=48.8814703,
                lng=2.340862,
                limited_view=False,
            )

        semantic_calls: list[str | None] = []

        def fake_apply_semantic_enrichment(enrichment_place, **_: object) -> None:
            semantic_calls.append(enrichment_place.display_name)

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
            patch.object(build_data, "apply_semantic_enrichment", side_effect=fake_apply_semantic_enrichment),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertTrue(entry.matched)
        self.assertEqual(semantic_calls, ["Sister Midnight"])

    def test_fetch_place_page_enrichment_skips_search_result_with_junk_address(self) -> None:
        place = RawPlace(
            name="Global Village",
            address="Dubai",
            maps_url="https://maps.google.com/?cid=123456789",
            lat=25.0715,
            lng=55.3086,
        )
        called_urls: list[str] = []

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            called_urls.append(url)
            if "/maps/search/" in url:
                return SimpleNamespace(
                    source_url=url,
                    resolved_url="https://www.google.com/maps/search/?api=1&query=Global+Village",
                    name="Global Village",
                    category="Theme park",
                    rating=4.6,
                    review_count=100,
                    address=(
                        "Imagery ©2026 , Map data ©2026 "
                        "United StatesTermsPrivacySend Product Feedback"
                    ),
                    located_in=None,
                    status=None,
                    website=None,
                    phone=None,
                    plus_code=None,
                    description=None,
                    lat=25.0715,
                    lng=55.3086,
                    limited_view=False,
                )
            return SimpleNamespace(
                source_url=url,
                resolved_url="https://www.google.com/maps/place/Global+Village/@25.0715,55.3086,17z",
                name="Global Village",
                category="Theme park",
                rating=4.6,
                review_count=100,
                address="38C5+F57 - Wadi Al Safa 4 - Dubai - United Arab Emirates",
                located_in=None,
                status=None,
                website="https://www.globalvillage.ae/",
                phone=None,
                plus_code="38C5+F57 Dubai",
                description=None,
                lat=25.0715,
                lng=55.3086,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertEqual(
            called_urls,
            [
                "https://www.google.com/maps/search/?api=1&query=Global+Village%2C+Dubai&hl=en&gl=us",
                "https://maps.google.com/?cid=123456789&hl=en&gl=us",
            ],
        )
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(
            entry.place.formatted_address,
            "38C5+F57 - Wadi Al Safa 4 - Dubai - United Arab Emirates",
        )

    def test_fetch_place_page_enrichment_rejects_search_artifact_partial_name_match(self) -> None:
        place = RawPlace(
            name="Sendlinger Tor",
            address="Sendlinger-Tor-Platz 1, 80336 München, Germany",
            maps_url=(
                "https://www.google.com/maps/search/?api=1&query="
                "Sendlinger+Tor%2C+Sendlinger-Tor-Platz+1%2C+80336+M%C3%BCnchen%2C+Germany"
            ),
            lat=48.1340387,
            lng=11.5676369,
        )

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                source_url=url,
                resolved_url=url,
                name="Sendlinger-Tor-Platz 1",
                category="Kebab shop",
                rating=4.4,
                review_count=8862,
                address=(
                    "/search?sca_esv=6ce6d4092249d8a7&authuser=0&hl=en&gl=tw"
                    "&output=search&tbm=map&q=Haferkater,+Sendlinger+Tor,+M%C3%BCnchen"
                    "&ludocid=16588126363784805389"
                ),
                located_in=None,
                status=None,
                website=None,
                phone=None,
                plus_code=None,
                description="Kreissparkasse München Starnberg Ebersberg - BaufinanzierungsCenter",
                lat=48.1340387,
                lng=11.5676369,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertFalse(entry.matched)
        self.assertIsNone(entry.place)

    def test_fetch_place_page_enrichment_accepts_scored_search_partial_name_match(self) -> None:
        place = RawPlace(
            name="McDonald's",
            maps_url="https://www.google.com/maps/search/?api=1&query=McDonald%27s%2C+Shibuya",
            lat=35.6581,
            lng=139.7017,
        )

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                source_url=url,
                resolved_url=url,
                name="McDonald's Shibuya",
                category="Fast food restaurant",
                rating=3.8,
                review_count=1200,
                address=None,
                located_in=None,
                status=None,
                website=None,
                phone=None,
                plus_code=None,
                description=None,
                lat=35.6581,
                lng=139.7017,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(entry.place.display_name, "McDonald's Shibuya")

    def test_fetch_place_page_enrichment_rejects_sparse_search_match_without_location_evidence(self) -> None:
        place = RawPlace(
            name="McDonald's",
            maps_url="https://www.google.com/maps/search/?api=1&query=McDonald%27s",
        )

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                source_url=url,
                resolved_url=url,
                name="McDonald's",
                category="Fast food restaurant",
                rating=3.8,
                review_count=1200,
                address=None,
                located_in=None,
                status=None,
                website=None,
                phone=None,
                plus_code=None,
                description=None,
                lat=None,
                lng=None,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertFalse(entry.matched)
        self.assertIsNone(entry.place)

    def test_fetch_place_page_enrichment_rejects_addressless_search_match_with_loose_distance(self) -> None:
        place = RawPlace(
            name="McDonald's",
            maps_url="https://www.google.com/maps/search/?api=1&query=McDonald%27s",
            lat=35.6581,
            lng=139.7017,
        )

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                source_url=url,
                resolved_url=url,
                name="McDonald's",
                category="Fast food restaurant",
                rating=3.8,
                review_count=1200,
                address=None,
                located_in=None,
                status=None,
                website=None,
                phone=None,
                plus_code=None,
                description=None,
                lat=35.7481,
                lng=139.7017,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertFalse(entry.matched)
        self.assertIsNone(entry.place)

    def test_normalize_place_page_enrichment_rejects_ui_display_name_and_review_description(self) -> None:
        details = SimpleNamespace(
            source_url="https://www.google.com/maps/search/?api=1&query=Onsen",
            resolved_url=None,
            name="Share",
            category="Outdoor bath",
            rating=4.1,
            review_count=265,
            address="20 Obamacho Marina, Unzen, Nagasaki 854-0517, Japan",
            located_in=None,
            status=None,
            website=None,
            phone=None,
            plus_code=None,
            description=(
                "After six p.m. You can reserve this onsen privately for one hour at a time. "
                "It cost ¥2000 yen to reserve it. I think during normal hours it is cheaper."
            ),
            lat=32.728,
            lng=130.207,
            limited_view=False,
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertIsNone(enrichment.display_name)
        self.assertIsNone(enrichment.description)

        details.name = "Tamariz Beach"
        details.description = (
            "Huge Waves - they were really fun but you should not lie in the front rows "
            "because an unexpectedly large wave hit our towel."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.display_name, "Tamariz Beach")
        self.assertIsNone(enrichment.description)

        details.name = "Kumoba Pond"
        details.description = (
            "This pond is a scenic and tranquil spot, just a five-minute bike ride from the main street in Karuizawa. "
            "It offers a peaceful retreat with serene water and lush surroundings. "
            "The area is equipped with toilets and is perfect for taking your children for a relaxing outing. "
            "A wonderful place to unwind and enjoy nature. Highly recommended for families."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.display_name, "Kumoba Pond")
        self.assertIsNone(enrichment.description)

        details.name = "Pink Street"
        details.description = (
            "Just an overrated place. Tiktok and instagram made it famous but I didn't "
            "feel like it is a must visit spot."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.display_name, "Pink Street")
        self.assertIsNone(enrichment.description)

        details.name = "Sushi Bar"
        details.description = "Highly recommended sushi spot"

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.display_name, "Sushi Bar")
        self.assertIsNone(enrichment.description)

        details.description = "We came for brunch and loved it"

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertIsNone(enrichment.description)

        details.description = (
            "Best ramen we've ever had. It helps that you get to make it yourself "
            "(the noodles at least). Everything tastes better when you do it yourself! "
            "Date day for a Saturday morning class. Great experience overall."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertIsNone(enrichment.description)

        details.description = (
            "That’s gotta be one of the best hot chocolate drink I’ve tasted in my life! "
            "The long wait was definitely worth it. There was quite a line before we got "
            "a seat, but boy was it worth every minute. Definitely recommend this place."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertIsNone(enrichment.description)

        for review_description in (
            "It was my first attempt to eat mukhata. The food was so delicious and the clerks were very kind.",
            "Best place to stay in Hanoi. I’d just finished Ha Giang loop and needed a place to rest.",
            "The katsu burger!!! Omfg!!! So yummy and the young man with blonde curly hair from England.",
            "What a great hotel! The rooms were huge and have balconies with a seating area.",
            "Had a great time here with my friends! The barkeeper made us feel welcomed and we had a lot of fun.",
            "The lady was just so so lovely. My feet are just gorgeous. Would recommend to everyone.",
            "My stay in Alila was wonderful. Special shout out to the staff for making it memorable.",
            "The hotel have a sense of peace and tranquility once step in. The personal service was delicate.",
            "The staffs also offered great recommendation for drinks based on your preference.",
            "Directions Save Nearby Send to phone Share About this data Get the most out of Google Maps Sign in",
            "\ue52e Directions \ue866 Save \uf05f Nearby \ue702 Send to phone \ue80d Share",
        ):
            details.description = review_description

            enrichment = build_data.normalize_place_page_enrichment(details)

            self.assertIsNone(enrichment.description)

        details.source_url = "https://www.google.com/maps/place/Modern+Restaurant"
        details.resolved_url = "https://www.google.com/maps/place/Modern+Restaurant"
        details.description = (
            "Modern restaurant serving delicious food for lunch and dinner in a "
            "friendly, relaxed setting."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.description, details.description)

        details.description = "We celebrate local producers with seasonal cooking."

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.description, details.description)

        details.description = (
            "A must visit destination for families, with interactive science exhibits, "
            "live demonstrations, and workshops."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.description, details.description)

        details.source_url = "https://www.google.com/maps/place/Costume+Museum"
        details.resolved_url = "https://www.google.com/maps/place/Costume+Museum"
        details.name = "Costume Museum"
        details.description = (
            "Our costume museum presents rotating textile exhibitions with archival research, "
            "studio workshops, and guided tours for visitors interested in design history."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.display_name, "Costume Museum")
        self.assertEqual(enrichment.description, details.description)

        details.name = "Low Cost Cafe"
        details.description = (
            "Our low-cost community studio offers affordable workshops, repair clinics, "
            "shared tools, and training programs for young makers."
        )

        enrichment = build_data.normalize_place_page_enrichment(details)

        self.assertEqual(enrichment.display_name, "Low Cost Cafe")
        self.assertEqual(enrichment.description, details.description)

    def test_fetch_place_page_enrichment_retries_direct_place_url_when_search_match_lacks_description(self) -> None:
        place = RawPlace(
            name="Taipei 101",
            address="Taipei, Taiwan",
            maps_url="https://www.google.com/maps/place/Taipei+101/@25.0341222,121.5640212,17z",
        )
        called_urls: list[str] = []

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            called_urls.append(url)
            if "/maps/search/" in url:
                return SimpleNamespace(
                    source_url=url,
                    resolved_url="https://www.google.com/maps/place/Taipei+101/@25.0341222,121.5640212,17z",
                    name="Taipei 101",
                    category="Shopping center",
                    rating=4.4,
                    review_count=37946,
                    address="No. 45, City Hall Rd, Taipei City, Taiwan 110",
                    located_in=None,
                    status=None,
                    website="https://www.taipei-101.com.tw/",
                    phone="+886 2 8101 8899",
                    plus_code=None,
                    description=None,
                    lat=25.0341222,
                    lng=121.5640212,
                    limited_view=False,
                )
            return SimpleNamespace(
                source_url=url,
                resolved_url="https://www.google.com/maps/place/Taipei+101/@25.0341222,121.5640212,17z",
                name="Taipei 101",
                category="Shopping center",
                rating=4.4,
                review_count=37946,
                address="No. 45, City Hall Rd, Taipei City, Taiwan 110",
                located_in=None,
                status=None,
                website="https://www.taipei-101.com.tw/",
                phone="+886 2 8101 8899",
                plus_code=None,
                description="Iconic skyscraper with shopping, dining, and an observatory on the 89th floor.",
                lat=25.0341222,
                lng=121.5640212,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Taipei",
                country_name="Taiwan",
                google_place_id="ChIJraeA2rarQjQRPBBjyR3RxKw",
            )

        self.assertEqual(
            called_urls,
            [
                "https://www.google.com/maps/search/?api=1&query=Taipei+101%2C+Taipei%2C+Taiwan&query_place_id=ChIJraeA2rarQjQRPBBjyR3RxKw&hl=en&gl=us",
                "https://www.google.com/maps/place/Taipei+101/@25.0341222,121.5640212,17z?hl=en&gl=us",
            ],
        )
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(
            entry.place.description,
            "Iconic skyscraper with shopping, dining, and an observatory on the 89th floor.",
        )

    def test_fetch_place_page_enrichment_retries_direct_place_url_when_search_page_stays_search_and_lacks_description(self) -> None:
        place = RawPlace(
            name="Taipei Zoo",
            address="Taipei, Taiwan",
            maps_url="https://www.google.com/maps/place/Taipei+Zoo/@24.9982415,121.5807219,17z",
        )
        called_urls: list[str] = []

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            called_urls.append(url)
            if len(called_urls) == 1:
                return SimpleNamespace(
                    source_url=url,
                    resolved_url=url,
                    name="Taipei Zoo",
                    category="Zoo",
                    rating=4.6,
                    review_count=76998,
                    address="No. 30號, Section 2, Xinguang Rd",
                    located_in=None,
                    status=None,
                    website="https://www.zoo.gov.taipei/",
                    phone="02 2938 2300",
                    plus_code="XHXJ+C9 Wanxing Village, Wenshan District, Taipei City",
                    description=None,
                    lat=24.9982415,
                    lng=121.5807219,
                    limited_view=False,
                )
            return SimpleNamespace(
                source_url=url,
                resolved_url="https://www.google.com/maps/place/Taipei+Zoo/@24.9982415,121.5807219,17z",
                name="Taipei Zoo",
                category="Zoo",
                rating=4.6,
                review_count=76998,
                address="No. 30號, Section 2, Xinguang Rd, Wanxing Village, Wenshan District, Taipei City, 116",
                located_in=None,
                status=None,
                website="https://www.zoo.gov.taipei/",
                phone="02 2938 2300",
                plus_code="XHXJ+C9 Wanxing Village, Wenshan District, Taipei City",
                description="Large indoor-outdoor zoo in a scenic setting with a children’s area, gondola & shuttle train.",
                lat=24.9982415,
                lng=121.5807219,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Taipei",
                country_name="Taiwan",
                google_place_id="ChIJj4EySmCqQjQRZte0Cf0G_qo",
            )

        self.assertEqual(
            called_urls,
            [
                "https://www.google.com/maps/search/?api=1&query=Taipei+Zoo%2C+Taipei%2C+Taiwan&query_place_id=ChIJj4EySmCqQjQRZte0Cf0G_qo&hl=en&gl=us",
                "https://www.google.com/maps/place/Taipei+Zoo/@24.9982415,121.5807219,17z?hl=en&gl=us",
            ],
        )
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(
            entry.place.description,
            "Large indoor-outdoor zoo in a scenic setting with a children’s area, gondola & shuttle train.",
        )

    def test_fetch_place_page_enrichment_retries_captured_search_result_url_when_clickthrough_is_thin(self) -> None:
        place = RawPlace(
            name="Taipei Zoo",
            maps_url="https://www.google.com/maps/search/?api=1&query=Taipei+Zoo",
        )
        called_urls: list[str] = []

        def fake_scrape_place(url: str, **_: object) -> SimpleNamespace:
            called_urls.append(url)
            if len(called_urls) == 1:
                return SimpleNamespace(
                    source_url=url,
                    resolved_url=url,
                    name="Taipei Zoo",
                    category="Zoo",
                    rating=4.6,
                    review_count=76998,
                    address="No. 30號, Section 2, Xinguang Rd",
                    status=None,
                    website="https://www.zoo.gov.taipei/",
                    phone="02 2938 2300",
                    plus_code="XHXJ+C9 Wanxing Village, Wenshan District, Taipei City",
                    description=None,
                    search_result_description="Sizable zoo with a gondola & kids' area",
                    search_result_url="https://www.google.com/maps/place/Taipei+Zoo/data=!4m7!3m6",
                    lat=24.9985635,
                    lng=121.5809857,
                    limited_view=False,
                )
            return SimpleNamespace(
                source_url=url,
                resolved_url=url,
                name="Taipei Zoo",
                category="Zoo",
                rating=4.6,
                review_count=76998,
                address="No. 30號, Section 2, Xinguang Rd, Wanxing Village, Wenshan District, Taipei City, 116",
                status=None,
                website="https://www.zoo.gov.taipei/",
                phone="02 2938 2300",
                plus_code="XHXJ+C9 Wanxing Village, Wenshan District, Taipei City",
                description="Large indoor-outdoor zoo in a scenic setting with a children’s area, gondola & shuttle train.",
                lat=24.9985635,
                lng=121.5809857,
                limited_view=False,
            )

        with (
            patch.object(build_data, "scrape_place", side_effect=fake_scrape_place),
            patch.object(
                build_data,
                "build_scraper_sessions",
                return_value=(SimpleNamespace(), None, None),
            ),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
        ):
            entry = build_data.fetch_place_page_enrichment(
                place,
                city_name="Taipei",
                country_name="Taiwan",
            )

        self.assertEqual(
            called_urls,
            [
                "https://www.google.com/maps/search/?api=1&query=Taipei+Zoo%2C+Taipei%2C+Taiwan&hl=en&gl=us",
                "https://www.google.com/maps/place/Taipei+Zoo/data=!4m7!3m6?hl=en&gl=us",
            ],
        )
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(
            entry.place.description,
            "Large indoor-outdoor zoo in a scenic setting with a children’s area, gondola & shuttle train.",
        )

    def test_fetch_place_page_enrichment_retries_limited_view_without_review_count_after_candidate_queue(self) -> None:
        place = RawPlace(
            name="Cantina OK!",
            address="Council Pl, Sydney NSW 2000, Australia",
            maps_url="https://www.google.com/maps/place/Cantina+OK!",
        )
        scrape_urls = ["https://example.com/first", "https://example.com/second"]
        scrape_attempts: list[str] = []

        def scrape_side_effect(
            scrape_url: str,
            *,
            headless: bool,
            browser_session: object,
            http_session: object,
            llm_fallback: object,
            llm_tasks: tuple[str, ...],
            collect_reviews: bool,
            collect_about: bool,
        ) -> SimpleNamespace:
            scrape_attempts.append(scrape_url)
            if len(scrape_attempts) <= len(scrape_urls):
                return SimpleNamespace(
                    source_url=scrape_url,
                    resolved_url=scrape_url,
                    name="Cantina OK!",
                    category="Cocktail bar",
                    address="Council Pl, Sydney NSW 2000, Australia",
                    limited_view=True,
                )
            return SimpleNamespace(
                source_url=scrape_url,
                resolved_url=scrape_url,
                name="Cantina OK!",
                category="Cocktail bar",
                address="Council Pl, Sydney NSW 2000, Australia",
                rating=4.8,
                review_count=512,
                limited_view=False,
            )

        with (
            patch.object(build_data, "current_scraper_proxy", return_value=None),
            patch.object(build_data, "build_place_page_candidate_urls", return_value=scrape_urls),
            patch.object(build_data, "build_scraper_sessions", return_value=(SimpleNamespace(), None, None)),
            patch.object(build_data, "build_scraper_configs", return_value=(None, None)),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "clear_scraper_session_state") as clear_session,
            patch.object(build_data, "release_scraper_session_lock"),
            patch.object(build_data, "scrape_place", side_effect=scrape_side_effect),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertEqual(
            scrape_attempts,
            ["https://example.com/first", "https://example.com/second", "https://example.com/first"],
        )
        clear_session.assert_called_once()
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(entry.place.user_rating_count, 512)

    def test_fetch_place_page_enrichment_returns_sparse_limited_view_when_retry_stays_sparse(self) -> None:
        place = RawPlace(
            name="Cantina OK!",
            address="Council Pl, Sydney NSW 2000, Australia",
            maps_url="https://www.google.com/maps/place/Cantina+OK!",
        )
        scrape_attempts: list[str] = []

        def scrape_side_effect(scrape_url: str, **_: object) -> SimpleNamespace:
            scrape_attempts.append(scrape_url)
            return SimpleNamespace(
                source_url="https://www.google.com/maps/search/?api=1&query=Cantina+OK",
                resolved_url=scrape_url,
                name="Cantina OK!",
                category="Cocktail bar",
                address="Council Pl, Sydney NSW 2000, Australia",
                rating=4.4,
                limited_view=True,
            )

        with (
            patch.object(build_data, "current_scraper_proxy", return_value=None),
            patch.object(build_data, "build_place_page_candidate_urls", return_value=["https://example.com/first"]),
            patch.object(build_data, "build_scraper_sessions", return_value=(SimpleNamespace(), None, None)),
            patch.object(build_data, "build_scraper_configs", return_value=(None, None)),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "clear_scraper_session_state"),
            patch.object(build_data, "release_scraper_session_lock"),
            patch.object(build_data, "scrape_place", side_effect=scrape_side_effect),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertEqual(scrape_attempts, ["https://example.com/first", "https://example.com/first"])
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(entry.place.display_name, "Cantina OK!")
        self.assertEqual(entry.place.rating, 4.4)
        self.assertIsNone(entry.place.user_rating_count)
        self.assertTrue(entry.place.limited_view)
        self.assertTrue(build_data.should_fallback_to_places_api(entry))

    def test_fetch_place_page_enrichment_rejects_wrong_sparse_search_result(self) -> None:
        place = RawPlace(
            name="Cantina OK!",
            address="Council Pl, Sydney NSW 2000, Australia",
            maps_url="https://www.google.com/maps/search/?api=1&query=Cantina+OK",
        )
        scrape_attempts: list[str] = []

        def scrape_side_effect(scrape_url: str, **_: object) -> SimpleNamespace:
            scrape_attempts.append(scrape_url)
            return SimpleNamespace(
                source_url="https://www.google.com/maps/search/?api=1&query=Cantina+OK",
                resolved_url=scrape_url,
                name="Wrong Cafe",
                category="Cafe",
                address=None,
                rating=4.4,
                limited_view=True,
            )

        with (
            patch.object(build_data, "current_scraper_proxy", return_value=None),
            patch.object(build_data, "build_place_page_candidate_urls", return_value=["https://example.com/search"]),
            patch.object(build_data, "build_scraper_sessions", return_value=(SimpleNamespace(), None, None)),
            patch.object(build_data, "build_scraper_configs", return_value=(None, None)),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "clear_scraper_session_state") as clear_session,
            patch.object(build_data, "release_scraper_session_lock"),
            patch.object(build_data, "scrape_place", side_effect=scrape_side_effect),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertEqual(scrape_attempts, ["https://example.com/search"])
        clear_session.assert_not_called()
        self.assertFalse(entry.matched)
        self.assertIsNone(entry.place)
        self.assertIsNone(entry.error)

    def test_fetch_place_page_enrichment_rejects_far_exact_name_candidate(self) -> None:
        place = RawPlace(
            name="Casa Montaña",
            maps_url="https://www.google.com/maps/search/?api=1&query=Casa+Monta%C3%B1a",
            lat=39.465568,
            lng=-0.3308894,
        )
        scrape_attempts: list[str] = []

        def scrape_side_effect(scrape_url: str, **_: object) -> SimpleNamespace:
            scrape_attempts.append(scrape_url)
            if scrape_url == "https://example.com/wrong":
                return SimpleNamespace(
                    source_url="https://www.google.com/maps/place/Casa+Monta%C3%B1a",
                    resolved_url=scrape_url,
                    name="Casa Montaña",
                    category="Villa",
                    address="Rincón, Puerto Rico",
                    rating=4.8,
                    review_count=10,
                    lat=18.3546206,
                    lng=-67.2517407,
                    limited_view=False,
                )
            return SimpleNamespace(
                source_url="https://maps.google.com/?cid=963849929162476527",
                resolved_url=scrape_url,
                name="Casa Montaña",
                category="Restaurant",
                address="Carrer de Josep Benlliure, 69, Valencia, Spain",
                rating=4.6,
                review_count=4812,
                lat=39.465568,
                lng=-0.3308894,
                limited_view=False,
            )

        with (
            patch.object(build_data, "current_scraper_proxy", return_value=None),
            patch.object(
                build_data,
                "build_place_page_candidate_urls",
                return_value=["https://example.com/wrong", "https://example.com/correct"],
            ),
            patch.object(build_data, "build_scraper_sessions", return_value=(SimpleNamespace(), None, None)),
            patch.object(build_data, "build_scraper_configs", return_value=(None, None)),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "clear_scraper_session_state"),
            patch.object(build_data, "release_scraper_session_lock"),
            patch.object(build_data, "scrape_place", side_effect=scrape_side_effect),
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        self.assertEqual(scrape_attempts, ["https://example.com/wrong", "https://example.com/correct"])
        self.assertTrue(entry.matched)
        assert entry.place is not None
        self.assertEqual(entry.place.primary_type_display_name, "Restaurant")
        self.assertEqual(entry.place.formatted_address, "Carrer de Josep Benlliure, 69, Valencia, Spain")

    def test_preserve_existing_enrichment_does_not_keep_incompatible_stronger_maps_url(self) -> None:
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Casa Montaña, Valencia, Spain",
            matched=True,
            place=EnrichmentPlace(
                display_name="Casa Montaña",
                formatted_address="Carr. 413 KM 5.8 Interior Road Sec La Joya BO, Puerto Rico",
                google_maps_uri="https://www.google.com/maps/place/Casa+Monta%C3%B1a/@18.3546206,-67.2517407,17z/",
                google_place_id="stale-place-id",
                google_place_resource_name="places/stale-place-id",
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="Casa Montaña, Valencia, Spain",
            matched=True,
            place=EnrichmentPlace(
                display_name="Casa Montaña",
                formatted_address="C/ de Josep Benlliure, 69, Poblats Marítims, València, Spain",
                google_maps_uri="https://maps.google.com/?cid=963849929162476527",
            ),
        )

        merged, warning = build_data.preserve_existing_enrichment(
            slug="valencia-spain",
            place_id="cid:963849929162476527",
            place_name="Casa Montaña",
            existing_entry=existing_entry,
            refreshed_entry=refreshed_entry,
        )

        self.assertIsNone(warning)
        assert merged.place is not None
        self.assertEqual(merged.place.google_maps_uri, "https://maps.google.com/?cid=963849929162476527")
        self.assertIsNone(merged.place.google_place_id)
        self.assertIsNone(merged.place.google_place_resource_name)

    def test_preserve_existing_enrichment_keeps_compatible_stronger_maps_url(self) -> None:
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Casa Montaña, Valencia, Spain",
            matched=True,
            place=EnrichmentPlace(
                display_name="Casa Montaña",
                formatted_address="C/ de Josep Benlliure, 69, Poblats Marítims, València, Spain",
                google_maps_uri="https://maps.google.com/?cid=963849929162476527",
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="Casa Montaña, Valencia, Spain",
            matched=True,
            place=EnrichmentPlace(
                display_name="Casa Montaña",
                formatted_address="C/ de Josep Benlliure, 69, Poblats Marítims, València, Spain",
                google_maps_uri="https://www.google.com/maps/search/?api=1&query=Casa+Monta%C3%B1a",
            ),
        )

        merged, warning = build_data.preserve_existing_enrichment(
            slug="valencia-spain",
            place_id="cid:963849929162476527",
            place_name="Casa Montaña",
            existing_entry=existing_entry,
            refreshed_entry=refreshed_entry,
        )

        self.assertIsNotNone(warning)
        assert merged.place is not None
        self.assertEqual(merged.place.google_maps_uri, "https://maps.google.com/?cid=963849929162476527")

    def test_preserve_existing_enrichment_keeps_thinner_successful_refresh_fields(self) -> None:
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="The Bob Hawke Beer and Leisure Centre, Sydney",
            matched=True,
            place=EnrichmentPlace(
                display_name="The Bob Hawke Beer and Leisure Centre",
                formatted_address="8-12 Sydney St, Marrickville NSW 2204, Australia",
                review_topics=[{"label": "beer", "count": 10}],
                reviews=[{"text": "Great pub"}],
                about_sections=[
                    {
                        "title": "Offerings",
                        "items": [{"label": "Beer", "aria_label": "Serves beer"}],
                    }
                ],
                semantic_description="A brewery-backed leisure centre with pub food and a retro Australian beer-hall feel.",
                semantic_description_signature="old-signature",
                semantic_source="llm",
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="The Bob Hawke Beer and Leisure Centre, Sydney",
            matched=True,
            place=EnrichmentPlace(
                display_name="The Bob Hawke Beer and Leisure Centre",
                formatted_address="8-12 Sydney St, Marrickville NSW 2204, Australia",
                rating=4.5,
            ),
        )

        merged, warning = build_data.preserve_existing_enrichment(
            slug="sydney-australia",
            place_id="cid:4697974207037271879",
            place_name="The Bob Hawke Beer and Leisure Centre",
            existing_entry=existing_entry,
            refreshed_entry=refreshed_entry,
        )

        self.assertIsNotNone(warning)
        assert merged.place is not None
        self.assertEqual(merged.place.review_topics, [{"label": "beer", "count": 10}])
        self.assertEqual(merged.place.reviews, [{"text": "Great pub"}])
        self.assertEqual(
            merged.place.about_sections,
            [
                {
                    "title": "Offerings",
                    "items": [{"label": "Beer", "aria_label": "Serves beer"}],
                }
            ],
        )
        self.assertEqual(
            merged.place.semantic_description,
            "A brewery-backed leisure centre with pub food and a retro Australian beer-hall feel.",
        )
        self.assertEqual(merged.place.semantic_description_signature, "old-signature")
        self.assertEqual(merged.place.semantic_source, "llm")
        self.assertIn("about", warning or "")
        self.assertIn("semantic_description", warning or "")

    def test_preserve_existing_enrichment_skips_invalid_cached_semantic_description(self) -> None:
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Sushi Okeya Kyujiro, Montreal",
            matched=True,
            place=EnrichmentPlace(
                display_name="Sushi Okeya Kyujiro",
                formatted_address="1227 Rue de la Montagne, Montréal, QC H3G 1Z2, Canada",
                semantic_description=(
                    "$180 lunch omakase; food is top notch, though it doesn’t feel that "
                    "elevated at this price point (dishware feels kinda cheap and "
                    "servers are a bit clueless about alcohol options)"
                ),
                semantic_description_signature="old-signature",
                semantic_source="llm",
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="Sushi Okeya Kyujiro, Montreal",
            matched=True,
            place=EnrichmentPlace(
                display_name="Sushi Okeya Kyujiro",
                formatted_address="1227 Rue de la Montagne, Montréal, QC H3G 1Z2, Canada",
                rating=4.6,
            ),
        )
        raw_place = RawPlace(
            name="Sushi Okeya Kyujiro",
            address="1227 Rue de la Montagne, Montréal, QC H3G 1Z2, Canada",
            maps_url="https://maps.google.com/?cid=123",
            cid="123",
        )

        merged, warning = build_data.preserve_existing_enrichment(
            slug="montreal-canada",
            place_id="cid:123",
            place_name="Sushi Okeya Kyujiro",
            existing_entry=existing_entry,
            refreshed_entry=refreshed_entry,
            raw_place=raw_place,
        )

        self.assertIsNone(warning)
        assert merged.place is not None
        self.assertIsNone(merged.place.semantic_description)
        self.assertIsNone(merged.place.semantic_description_signature)
        self.assertIsNone(merged.place.semantic_source)

    def test_uncertain_place_page_identity_suppresses_publishable_identity_fields(self) -> None:
        raw_place = RawPlace(
            name="Lola Underground",
            address="Hay St &, Cathedral Ave, Perth WA 6000, Australia",
            lat=-31.9549688,
            lng=115.8609673,
            maps_url=(
                "https://www.google.com/maps/search/?api=1&query=Lola+Underground%2C+"
                "Hay+St+%26%2C+Cathedral+Ave%2C+Perth+WA+6000%2C+Australia"
            ),
            cid="3040698308894550531",
            google_id="/g/11wqqpwh8_",
        )
        enrichment_place = EnrichmentPlace(
            display_name="Pooles Temple",
            formatted_address="Hay St &, Cathedral Ave, Perth WA 6000, Australia",
            google_maps_uri=(
                "https://www.google.com/maps/place/Pooles+Temple/@-31.9549688,115.8609673,17z/"
                "data=!3m1!4b1!4m6!3m5!1s0x2a32bb006aca6a03:0xe5ba64f4222eb894!"
                "8m2!3d-31.9549688!4d115.8609673!16s%2Fg%2F11wqqpwh8_"
            ),
            google_place_id="ChIJA2rKagC7MioRlLguIvRkuuU",
            google_place_resource_name="places/ChIJA2rKagC7MioRlLguIvRkuuU",
            website="https://statebuildings.com/functions/pooles-temple",
            phone="+61 8 1234 5678",
            primary_type_display_name="Event venue",
            review_topics=[{"label": "cocktails"}],
        )

        suppressed_fields = build_data.suppress_uncertain_place_page_identity_fields(
            raw_place,
            enrichment_place,
        )

        self.assertIn("display_name", suppressed_fields)
        self.assertIn("google_maps_uri", suppressed_fields)
        self.assertIsNone(enrichment_place.display_name)
        self.assertIsNone(enrichment_place.google_maps_uri)
        self.assertIsNone(enrichment_place.google_place_id)
        self.assertIsNone(enrichment_place.google_place_resource_name)
        self.assertIsNone(enrichment_place.website)
        self.assertIsNone(enrichment_place.phone)
        self.assertEqual(enrichment_place.formatted_address, "Hay St &, Cathedral Ave, Perth WA 6000, Australia")
        self.assertEqual(enrichment_place.primary_type_display_name, "Event venue")
        self.assertEqual(enrichment_place.review_topics, [{"label": "cocktails"}])

    def test_uncertain_place_page_identity_keeps_partial_name_matches(self) -> None:
        raw_place = RawPlace(
            name="McDonald's",
            address=None,
            lat=35.6595,
            lng=139.7005,
            maps_url="https://www.google.com/maps/search/?api=1&query=McDonald%27s",
        )
        enrichment_place = EnrichmentPlace(
            display_name="McDonald's Shibuya",
            google_maps_uri="https://www.google.com/maps/place/McDonald%27s+Shibuya/",
            google_place_id="compatible-place-id",
            website="https://www.mcdonalds.co.jp/",
        )

        suppressed_fields = build_data.suppress_uncertain_place_page_identity_fields(
            raw_place,
            enrichment_place,
        )

        self.assertEqual(suppressed_fields, [])
        self.assertEqual(enrichment_place.display_name, "McDonald's Shibuya")
        self.assertEqual(enrichment_place.google_place_id, "compatible-place-id")

    def test_google_place_id_refresh_seed_ignores_incompatible_existing_name(self) -> None:
        raw_place = RawPlace(
            name="Lola Underground",
            maps_url="https://www.google.com/maps/search/?api=1&query=Lola+Underground",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="Lola Underground, Perth",
            matched=True,
            place=EnrichmentPlace(
                display_name="Pooles Temple",
                google_place_id="ChIJA2rKagC7MioRlLguIvRkuuU",
            ),
        )

        self.assertIsNone(build_data.google_place_id_for_refresh_seed(raw_place, existing_entry))

        self.assertEqual(
            build_data.google_place_id_for_refresh_seed(
                raw_place,
                existing_entry,
                allow_identity_mismatch=True,
            ),
            "ChIJA2rKagC7MioRlLguIvRkuuU",
        )

    def test_google_place_id_refresh_seed_ignores_incompatible_existing_url_name(self) -> None:
        raw_place = RawPlace(
            name="Lola Underground",
            maps_url="https://www.google.com/maps/search/?api=1&query=Lola+Underground",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="Lola Underground, Perth",
            matched=True,
            place=EnrichmentPlace(
                display_name=None,
                google_maps_uri="https://www.google.com/maps/place/Pooles+Temple/@-31.9549688,115.8609673,17z/",
                google_place_id="ChIJA2rKagC7MioRlLguIvRkuuU",
            ),
        )

        self.assertIsNone(build_data.google_place_id_for_refresh_seed(raw_place, existing_entry))

    def test_google_place_id_refresh_seed_keeps_compatible_existing_name(self) -> None:
        raw_place = RawPlace(
            name="McDonald's",
            maps_url="https://www.google.com/maps/search/?api=1&query=McDonald%27s",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="McDonald's",
            matched=True,
            place=EnrichmentPlace(
                display_name="McDonald's Shibuya",
                google_place_id="compatible-place-id",
            ),
        )

        self.assertEqual(build_data.google_place_id_for_refresh_seed(raw_place, existing_entry), "compatible-place-id")

    def test_preserve_existing_enrichment_does_not_restore_incompatible_previous_identity(self) -> None:
        raw_place = RawPlace(
            name="Lola Underground",
            address="Hay St &, Cathedral Ave, Perth WA 6000, Australia",
            maps_url="https://www.google.com/maps/search/?api=1&query=Lola+Underground",
            cid="3040698308894550531",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Lola Underground, Perth",
            matched=True,
            place=EnrichmentPlace(
                display_name=None,
                formatted_address="Hay St &, Cathedral Ave, Perth WA 6000, Australia",
                google_maps_uri="https://www.google.com/maps/place/Pooles+Temple/@-31.9549688,115.8609673,17z/",
                google_place_id="ChIJA2rKagC7MioRlLguIvRkuuU",
                business_status="OPERATIONAL",
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="Lola Underground, Perth",
            matched=True,
            place=EnrichmentPlace(
                display_name=None,
                formatted_address="Hay St &, Cathedral Ave, Perth WA 6000, Australia",
                google_maps_uri=None,
                google_place_id=None,
            ),
        )

        merged, warning = build_data.preserve_existing_enrichment(
            slug="perth-and-fremantle-australia",
            place_id="cid:3040698308894550531",
            place_name="Lola Underground",
            existing_entry=existing_entry,
            refreshed_entry=refreshed_entry,
            raw_place=raw_place,
        )

        self.assertIsNotNone(warning)
        assert merged.place is not None
        self.assertIsNone(merged.place.google_maps_uri)
        self.assertIsNone(merged.place.google_place_id)
        self.assertEqual(merged.place.business_status, "OPERATIONAL")

        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-02T00:00:00+00:00",
            source="google_maps_page",
            query="Lola Underground, Perth",
            matched=True,
            place=EnrichmentPlace(
                display_name=None,
                formatted_address="Hay St &, Cathedral Ave, Perth WA 6000, Australia",
                google_maps_uri=None,
                google_place_id=None,
            ),
        )
        merged, warning = build_data.preserve_existing_enrichment(
            slug="perth-and-fremantle-australia",
            place_id="cid:3040698308894550531",
            place_name="Lola Underground",
            existing_entry=existing_entry,
            refreshed_entry=refreshed_entry,
            raw_place=raw_place,
            allow_identity_mismatch=True,
        )

        self.assertIsNotNone(warning)
        assert merged.place is not None
        self.assertEqual(
            merged.place.google_maps_uri,
            "https://www.google.com/maps/place/Pooles+Temple/@-31.9549688,115.8609673,17z/",
        )
        self.assertEqual(merged.place.google_place_id, "ChIJA2rKagC7MioRlLguIvRkuuU")

    def test_place_page_has_meaningful_enrichment_rejects_limited_view_without_review_count(self) -> None:
        details = SimpleNamespace(limited_view=True)
        enrichment_place = EnrichmentPlace(
            display_name="Bianchetto",
            formatted_address="26-28 Cotham Rd, Kew VIC 3101, Australia",
            primary_type_display_name="Bar",
            rating=4.4,
        )

        self.assertTrue(build_data.should_retry_limited_place_page_result(details, enrichment_place))
        self.assertFalse(build_data.place_page_has_meaningful_enrichment(details, enrichment_place))

    def test_normalize_place_page_enrichment_prefers_stable_search_source_url(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url=(
                    "https://www.google.com/maps/search/?api=1&query="
                    "Sister+Midnight%2C+4+Rue+Viollet-le-Duc%2C+75009+Paris%2C+France"
                ),
                resolved_url=(
                    "https://www.google.com/maps/search/?api=1&query="
                    "Sister+Midnight%2C+4+Rue+Viollet-le-Duc%2C+75009+Paris%2C+France"
                ),
                name="Sister Midnight",
                category="Cocktail bar",
                rating=4.7,
                review_count=288,
                address="4 Rue Viollet-le-Duc, 75009 Paris, France",
                website="https://sistermidnightparis.com/",
                phone=None,
                plus_code=None,
                description="Volatile review text",
                limited_view=False,
                status=None,
            )
        )

        self.assertEqual(
            enrichment.google_maps_uri,
            (
                "https://www.google.com/maps/search/?api=1&query="
                "Sister+Midnight%2C+4+Rue+Viollet-le-Duc%2C+75009+Paris%2C+France"
            ),
        )
        self.assertIsNone(enrichment.description)

    def test_normalize_place_page_enrichment_rejects_relative_search_address(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/search/?api=1&query=Sendlinger+Tor",
                resolved_url="https://www.google.com/maps/search/?api=1&query=Sendlinger+Tor",
                name="Sendlinger-Tor-Platz 1",
                category="Kebab shop",
                rating=4.4,
                review_count=8862,
                address=(
                    "/search?sca_esv=6ce6d4092249d8a7&authuser=0&hl=en&gl=tw"
                    "&output=search&tbm=map&q=Haferkater,+Sendlinger+Tor,+M%C3%BCnchen"
                    "&ludocid=16588126363784805389"
                ),
                limited_view=False,
            )
        )

        self.assertIsNone(enrichment.formatted_address)

    def test_normalize_place_page_enrichment_rejects_travel_product_address(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/search/?api=1&query=Nalati+Grassland",
                resolved_url="https://www.google.com/maps/search/?api=1&query=Nalati+Grassland",
                name="Nalati Grassland",
                category="National park",
                rating=4.5,
                review_count=133,
                address="8-Day Ili Pastoral RV Adventure (Duku Highway Crossing)",
                limited_view=False,
            )
        )

        self.assertIsNone(enrichment.formatted_address)

    def test_normalize_place_page_enrichment_keeps_description_after_search_resolves_to_place(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url=(
                    "https://www.google.com/maps/search/?api=1&query="
                    "Taipei+101%2C+Taipei%2C+Taiwan&query_place_id=ChIJraeA2rarQjQRPBBjyR3RxKw"
                ),
                resolved_url=(
                    "https://www.google.com/maps/place/Taipei+101/"
                    "@25.0341222,121.5640212,17z/data=!3m1!4b1"
                ),
                name="Taipei 101",
                category="Shopping center",
                rating=4.4,
                review_count=37946,
                address="No. 45, City Hall Rd, Xinyi District, Taipei City, Taiwan 110",
                website="https://www.taipei-101.com.tw/",
                phone="02 8101 8899",
                plus_code=None,
                description="Iconic skyscraper with shopping, dining, and an observatory on the 89th floor.",
                limited_view=False,
                status=None,
            )
        )

        self.assertEqual(
            enrichment.google_maps_uri,
            (
                "https://www.google.com/maps/search/?api=1&query="
                "Taipei+101%2C+Taipei%2C+Taiwan&query_place_id=ChIJraeA2rarQjQRPBBjyR3RxKw"
            ),
        )
        self.assertEqual(
            enrichment.description,
            "Iconic skyscraper with shopping, dining, and an observatory on the 89th floor.",
        )

    def test_normalize_place_page_enrichment_keeps_search_result_description_separate(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url=(
                    "https://www.google.com/maps/search/?api=1&query="
                    "Taipei+Zoo%2C+Taipei%2C+Taiwan"
                ),
                resolved_url="https://www.google.com/maps/place/Taipei+Zoo/@24.9982415,121.5807219,17z",
                name="Taipei Zoo",
                category="Zoo",
                rating=4.6,
                review_count=76998,
                address="No. 30號, Section 2, Xinguang Rd, Taipei City",
                website="https://www.zoo.gov.taipei/",
                phone="02 2938 2300",
                plus_code=None,
                description="Large indoor-outdoor zoo in a scenic setting with a children's area.",
                search_result_description="Sizable zoo with a gondola & kids' area",
                limited_view=False,
                status=None,
            )
        )

        self.assertEqual(
            enrichment.description,
            "Large indoor-outdoor zoo in a scenic setting with a children's area.",
        )
        self.assertEqual(
            enrichment.search_result_description,
            "Sizable zoo with a gondola & kids' area",
        )

    def test_normalize_place_page_enrichment_carries_photo_urls(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Open+Kitchen",
                resolved_url="https://www.google.com/maps/place/Open+Kitchen",
                name="Open Kitchen",
                category="Restaurant",
                rating=4.7,
                review_count=120,
                address="1 Example St, Tokyo",
                website="https://openkitchen.example/",
                phone="+81 3-1111-2222",
                plus_code=None,
                description=None,
                main_photo_url="https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
                photo_url="https://lh3.googleusercontent.com/p/example=s680-w680-h510",
                limited_view=False,
                status=None,
            )
        )

        self.assertEqual(
            enrichment.main_photo_url,
            "https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
        )
        self.assertEqual(
            enrichment.photo_url,
            "https://lh3.googleusercontent.com/p/example=s680-w680-h510",
        )

    def test_normalize_place_page_enrichment_carries_reservation_links(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Open+Kitchen",
                resolved_url="https://www.google.com/maps/place/Open+Kitchen",
                name="Open Kitchen",
                category="Restaurant",
                rating=4.7,
                review_count=120,
                address="1 Example St, Lisbon",
                website="https://openkitchen.example/",
                reservation_links=[
                    {"label": "Resy", "url": "https://resy.com/cities/lisbon/open-kitchen"},
                    {"label": "Bad", "url": "javascript:alert(1)"},
                ],
                phone="+351 21 000 0000",
                plus_code=None,
                description=None,
                limited_view=False,
                status=None,
            )
        )

        self.assertEqual(enrichment.website, "https://openkitchen.example/")
        self.assertEqual(
            [link.model_dump(mode="json") for link in enrichment.reservation_links],
            [{"label": "Resy", "url": "https://resy.com/cities/lisbon/open-kitchen"}],
        )

    def test_normalize_place_page_enrichment_carries_optional_panel_cache_fields(self) -> None:
        class PanelItem:
            def to_dict(self) -> dict[str, object]:
                return {"label": "Cozy", "count": 12, "empty": None}

        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Tea+House",
                resolved_url="https://www.google.com/maps/place/Tea+House",
                name="Tea House",
                category="Tea house",
                rating=4.5,
                review_count=110,
                price_range="$200-400",
                address="No. 12, Songgao Rd, Taipei City",
                status=None,
                website=None,
                phone=None,
                plus_code=None,
                description=None,
                review_topics=[PanelItem()],
                reviews=[{"author": "A", "text": "Great tea.", "empty": ""}],
                about_sections=[{"title": "Service options", "items": [{"label": "Dine-in"}]}],
                limited_view=False,
            )
        )

        self.assertEqual(enrichment.price_range, "$200-400")
        self.assertEqual(enrichment.review_topics, [{"label": "Cozy", "count": 12}])
        self.assertEqual(enrichment.reviews, [{"author": "A", "text": "Great tea."}])
        self.assertEqual(
            enrichment.about_sections,
            [{"title": "Service options", "items": [{"label": "Dine-in"}]}],
        )

    def test_normalize_place_page_enrichment_drops_suspicious_photo_urls(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Open+Kitchen",
                resolved_url="https://www.google.com/maps/place/Open+Kitchen",
                name="Open Kitchen",
                category="Restaurant",
                address="1 Example St, Tokyo",
                main_photo_url=(
                    "https://maps.google.com/maps/api/staticmap?center=35.0%2C139.0"
                    "&zoom=17&size=900x900"
                ),
                photo_url="https://lh3.googleusercontent.com/a-/ALV-UjW_avatar=w36-h36-p-rp-mo-br100",
                limited_view=False,
            )
        )

        self.assertIsNone(enrichment.main_photo_url)
        self.assertIsNone(enrichment.photo_url)

    def test_normalize_place_page_enrichment_drops_tiny_photo_urls(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Open+Kitchen",
                resolved_url="https://www.google.com/maps/place/Open+Kitchen",
                name="Open Kitchen",
                category="Restaurant",
                address="1 Example St, Tokyo",
                photo_url="https://lh3.googleusercontent.com/gps-cs-s/example=w122-h92-k-no",
                limited_view=False,
            )
        )

        self.assertIsNone(enrichment.photo_url)

    def test_sanitize_place_photo_url_drops_any_tiny_source_dimension(self) -> None:
        for photo_url in (
            "https://lh3.googleusercontent.com/p/example=s680-w120-h800",
            "https://lh3.googleusercontent.com/p/example=s680-w800-h120",
            "https://lh3.googleusercontent.com/p/example=s680-w199-h400",
        ):
            with self.subTest(photo_url=photo_url):
                self.assertIsNone(build_data.sanitize_place_photo_url(photo_url))

        self.assertEqual(
            build_data.sanitize_place_photo_url(
                "https://lh3.googleusercontent.com/p/example=s680-w200-h200"
            ),
            "https://lh3.googleusercontent.com/p/example=s680-w200-h200",
        )
        self.assertEqual(
            build_data.sanitize_place_photo_url(
                "https://lh3.googleusercontent.com/p/example=s680-w800-h600"
            ),
            "https://lh3.googleusercontent.com/p/example=s680-w800-h600",
        )

    def test_sanitize_place_photo_url_rejects_static_map_and_avatar_path_variants(self) -> None:
        self.assertIsNone(
            build_data.sanitize_place_photo_url(
                "https://maps.google.com/maps/api/staticmap?center=35.0%2C139.0&zoom=17&size=900x900"
            )
        )
        self.assertIsNone(
            build_data.sanitize_place_photo_url(
                "https://lh3.googleusercontent.com:443/a-/ALV-UjW_avatar=w680-h680-p-rp-mo-br100"
            )
        )
        self.assertIsNone(
            build_data.sanitize_place_photo_url(
                "https://lh5.ggpht.com:443/a/example-avatar=w680-h680-p-rp-mo-br100"
            )
        )
        for photo_url in (
            "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcExample&s=3",
            "https://www.gstatic.com/faviconV2?url=https://example.com",
            "https://www.gstatic.com/ads-travel/example.png",
        ):
            with self.subTest(photo_url=photo_url):
                self.assertIsNone(build_data.sanitize_place_photo_url(photo_url))

    def test_sanitize_place_photo_url_uses_hostname_suffix_for_avatar_hosts(self) -> None:
        self.assertEqual(
            build_data.sanitize_place_photo_url(
                "https://evil-googleusercontent.com.example/a-/ALV-UjW_avatar=w680-h680-p-rp-mo-br100"
            ),
            "https://evil-googleusercontent.com.example/a-/ALV-UjW_avatar=w680-h680-p-rp-mo-br100",
        )
        self.assertIsNone(
            build_data.sanitize_place_photo_url(
                "https://lh3.googleusercontent.com:443/a-/ALV-UjW_avatar=w680-h680-p-rp-mo-br100"
            )
        )

    def test_normalize_place_page_enrichment_preserves_google_place_id_and_address_parts(self) -> None:
        enrichment = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Elephant+Mountain",
                resolved_url="https://www.google.com/maps/place/Elephant+Mountain",
                google_place_id="ChIJ8T36HxCLGGARvpARPDyaKLA",
                name="象山",
                category="Trailhead",
                address="2HGG+M6",
                address_parts=[
                    "Trailhead",
                    "Xinyi District, Taipei City",
                    "Xinyi District, Taipei City",
                    "Xinyi District",
                    "110",
                    "Taipei City",
                    "TW",
                    ["Taiwan"],
                ],
                limited_view=False,
            )
        )

        self.assertEqual(enrichment.google_place_id, "ChIJ8T36HxCLGGARvpARPDyaKLA")
        self.assertEqual(
            enrichment.address_parts,
            [
                "Trailhead",
                "Xinyi District, Taipei City",
                "Xinyi District, Taipei City",
                "Xinyi District",
                "110",
                "Taipei City",
                "TW",
                ["Taiwan"],
            ],
        )

    def test_sync_guide_place_photos_downloads_local_copy(self) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id="cid:123",
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=123",
                    status="active",
                )
            ],
        )
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Open Kitchen",
            matched=True,
            place=EnrichmentPlace(
                display_name="Open Kitchen",
                formatted_address="1 Example St, Tokyo",
                main_photo_url="https://lh3.googleusercontent.com/p/example=s680-w680-h510",
            ),
        )

        image_buffer = BytesIO()
        Image.new("RGB", (1600, 900), color=(200, 120, 80)).save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()

        class FakeHeaders:
            def get_content_type(self) -> str:
                return "image/png"

        class FakeResponse:
            headers = FakeHeaders()

            def read(self) -> bytes:
                return image_bytes

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        with TemporaryDirectory() as tmpdir:
            photo_dir = Path(tmpdir)
            photo_url = cache_entry.place.main_photo_url
            assert photo_url is not None
            photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]
            expected_extension = ".webp" if build_data.image_supports_webp() else ".jpg"
            expected_filename = f"cid-123-{photo_hash}{expected_extension}"
            expected_path = photo_dir / expected_filename
            with (
                patch.object(build_data, "PLACE_PHOTOS_DIR", photo_dir),
                patch.object(build_data, "urlopen", return_value=FakeResponse()),
            ):
                build_data.sync_guide_place_photos(
                    guide,
                    enrichment_cache={"cid:123": cache_entry},
                )

            self.assertEqual(
                guide.places[0].main_photo_path,
                f"/place-photos/{expected_filename}",
            )
            self.assertTrue(expected_path.exists())
            with Image.open(expected_path) as generated_image:
                self.assertEqual(
                    generated_image.size,
                    (build_data.PHOTO_CARD_WIDTH, build_data.PHOTO_CARD_HEIGHT),
                )

    def test_sync_place_photo_reuses_existing_hashed_file(self) -> None:
        photo_url = "https://lh3.googleusercontent.com/p/example=s680-w680-h510"
        photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]

        with TemporaryDirectory() as tmpdir:
            photo_dir = Path(tmpdir)
            existing_path = photo_dir / f"cid-123-{photo_hash}.jpg"
            legacy_path = photo_dir / "tokyo-japan" / existing_path.name
            existing_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            existing_path.write_bytes(b"existing-image")
            legacy_path.write_bytes(b"stale-legacy-image")

            with (
                patch.object(build_data, "PLACE_PHOTOS_DIR", photo_dir),
                patch.object(build_data, "urlopen", side_effect=AssertionError("should not download")),
            ):
                result = build_data.sync_place_photo(
                    "tokyo-japan",
                    "cid:123",
                    photo_url=photo_url,
                )

        self.assertEqual(
            result,
            f"/place-photos/cid-123-{photo_hash}.jpg",
        )
        self.assertFalse(legacy_path.exists())
        self.assertFalse(legacy_path.parent.exists())

    def test_sync_place_photo_migrates_legacy_guide_scoped_file_to_flat_storage(self) -> None:
        photo_url = "https://lh3.googleusercontent.com/p/example=s680-w680-h510"
        photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]

        with TemporaryDirectory() as tmpdir:
            photo_dir = Path(tmpdir)
            legacy_path = photo_dir / "tokyo-japan" / f"cid-123-{photo_hash}.jpg"
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_bytes(b"existing-image")

            with (
                patch.object(build_data, "PLACE_PHOTOS_DIR", photo_dir),
                patch.object(build_data, "urlopen", side_effect=AssertionError("should not download")),
            ):
                result = build_data.sync_place_photo(
                    "tokyo-japan",
                    "cid:123",
                    photo_url=photo_url,
                )

            canonical_path = photo_dir / f"cid-123-{photo_hash}.jpg"
            self.assertEqual(result, f"/place-photos/cid-123-{photo_hash}.jpg")
            self.assertTrue(canonical_path.exists())
            self.assertFalse(legacy_path.exists())

    def test_resolve_existing_place_photo_path_with_flat_index_cleans_legacy_duplicate(self) -> None:
        photo_url = "https://lh3.googleusercontent.com/p/example=s680-w680-h510"
        photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]
        filename = f"cid-123-{photo_hash}.jpg"

        with TemporaryDirectory() as tmpdir:
            photo_dir = Path(tmpdir)
            canonical_path = photo_dir / filename
            legacy_path = photo_dir / "tokyo-japan" / filename
            canonical_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            canonical_path.write_bytes(b"canonical-image")
            legacy_path.write_bytes(b"stale-legacy-image")

            with patch.object(build_data, "PLACE_PHOTOS_DIR", photo_dir):
                path, fallback_reason = build_data.resolve_existing_place_photo_path(
                    "tokyo-japan",
                    "cid:123",
                    photo_url=photo_url,
                    flat_index=build_data.build_place_photo_flat_index(),
                )

            self.assertEqual(path, f"/place-photos/{filename}")
            self.assertIsNone(fallback_reason)
            self.assertTrue(canonical_path.exists())
            self.assertFalse(legacy_path.exists())
            self.assertFalse(legacy_path.parent.exists())

    def test_populate_place_photos_for_guides_parallelizes_refresh_jobs(self) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=2,
            places=[
                NormalizedPlace(
                    id="cid:111",
                    name="First Place",
                    maps_url="https://maps.google.com/?cid=111",
                    status="active",
                ),
                NormalizedPlace(
                    id="cid:222",
                    name="Second Place",
                    maps_url="https://maps.google.com/?cid=222",
                    status="active",
                ),
            ],
        )
        enrichment_caches = {
            "tokyo-japan": {
                "cid:111": EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query="First Place",
                    matched=True,
                    place=EnrichmentPlace(main_photo_url="https://example.com/first.jpg"),
                ),
                "cid:222": EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query="Second Place",
                    matched=True,
                    place=EnrichmentPlace(main_photo_url="https://example.com/second.jpg"),
                ),
            }
        }
        executor_holder: dict[str, object] = {}
        calls: list[tuple[str, str, str | None, float]] = []

        class FakeFuture:
            def __init__(self, value: str | None):
                self._value = value

            def result(self) -> str | None:
                return self._value

        class FakeExecutor:
            def __init__(self, max_workers: int):
                self.max_workers = max_workers
                executor_holder["executor"] = self

            def __enter__(self) -> "FakeExecutor":
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def submit(self, fn, slug, place_id, *, photo_url, startup_jitter_seconds):
                value = fn(
                    slug,
                    place_id,
                    photo_url=photo_url,
                    startup_jitter_seconds=startup_jitter_seconds,
                )
                return FakeFuture(value)

        def fake_sync_place_photo(
            slug: str,
            place_id: str,
            *,
            photo_url: str | None,
            startup_jitter_seconds: float = 0,
        ) -> str:
            calls.append((slug, place_id, photo_url, startup_jitter_seconds))
            return f"/place-photos/{place_id}.webp"

        with (
            patch("builtins.print") as print_mock,
            patch.object(build_data, "ThreadPoolExecutor", FakeExecutor),
            patch.object(build_data, "as_completed", side_effect=lambda futures: list(futures)),
            patch.object(build_data, "sync_place_photo", side_effect=fake_sync_place_photo),
        ):
            build_data.populate_place_photos_for_guides(
                [guide],
                enrichment_caches=enrichment_caches,
                refresh_photos=True,
                photo_workers=4,
                startup_jitter_seconds=8,
            )

        executor = executor_holder["executor"]
        assert isinstance(executor, FakeExecutor)
        self.assertEqual(executor.max_workers, 2)
        self.assertEqual(
            calls,
            [
                ("tokyo-japan", "cid:111", "https://example.com/first.jpg", 8),
                ("tokyo-japan", "cid:222", "https://example.com/second.jpg", 8),
            ],
        )
        self.assertEqual(guide.places[0].main_photo_path, "/place-photos/cid:111.webp")
        self.assertEqual(guide.places[1].main_photo_path, "/place-photos/cid:222.webp")
        self.assertEqual(
            [args[0] for args, _kwargs in print_mock.call_args_list],
            [
                "Downloading 2 place photos with 2 workers (0 existing, 0 without photo URLs)",
                "[photos 1/2] downloaded: tokyo-japan / First Place",
                "[photos 2/2] downloaded: tokyo-japan / Second Place",
            ],
        )

    def test_populate_place_photos_for_guides_uses_existing_local_files_without_refresh(self) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id="cid:123",
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=123",
                    status="active",
                )
            ],
        )
        photo_url = "https://lh3.googleusercontent.com/p/example=s680-w680-h510"
        photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]

        with TemporaryDirectory() as tmpdir:
            photo_dir = Path(tmpdir)
            existing_path = photo_dir / f"cid-123-{photo_hash}.jpg"
            existing_path.parent.mkdir(parents=True, exist_ok=True)
            existing_path.write_bytes(b"existing-image")

            with (
                patch.object(build_data, "PLACE_PHOTOS_DIR", photo_dir),
                patch.object(build_data, "sync_place_photo", side_effect=AssertionError("should not refresh")),
            ):
                build_data.populate_place_photos_for_guides(
                    [guide],
                    enrichment_caches={
                        "tokyo-japan": {
                            "cid:123": EnrichmentCacheEntry(
                                fetched_at="2026-04-20T00:00:00+00:00",
                                query="Open Kitchen",
                                matched=True,
                                place=EnrichmentPlace(main_photo_url=photo_url),
                            )
                        }
                    },
                    refresh_photos=False,
                    photo_workers=4,
                    startup_jitter_seconds=8,
                )

        self.assertEqual(
            guide.places[0].main_photo_path,
            f"/place-photos/cid-123-{photo_hash}.jpg",
        )

    def test_populate_place_photos_for_guides_keeps_existing_photo_when_url_changes_and_refresh_is_disabled(
        self,
    ) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id="cid:123",
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=123",
                    status="active",
                )
            ],
        )
        old_photo_url = "https://lh3.googleusercontent.com/p/old-token=s680-w680-h510"
        new_photo_url = "https://lh3.googleusercontent.com/p/new-token=s680-w680-h510"
        old_photo_hash = hashlib.sha256(old_photo_url.encode("utf-8")).hexdigest()[:12]

        with TemporaryDirectory() as tmpdir:
            photo_dir = Path(tmpdir)
            existing_path = photo_dir / f"cid-123-{old_photo_hash}.jpg"
            existing_path.write_bytes(b"existing-image")

            with (
                patch.object(build_data, "PLACE_PHOTOS_DIR", photo_dir),
                patch.object(build_data, "sync_place_photo", side_effect=AssertionError("should not refresh")),
            ):
                build_data.populate_place_photos_for_guides(
                    [guide],
                    enrichment_caches={
                        "tokyo-japan": {
                            "cid:123": EnrichmentCacheEntry(
                                fetched_at="2026-04-20T00:00:00+00:00",
                                query="Open Kitchen",
                                matched=True,
                                place=EnrichmentPlace(main_photo_url=new_photo_url),
                            )
                        }
                    },
                    refresh_photos=False,
                    photo_workers=4,
                    startup_jitter_seconds=8,
                )

        self.assertEqual(
            guide.places[0].main_photo_path,
            f"/place-photos/cid-123-{old_photo_hash}.jpg",
        )

    def test_populate_place_photos_for_guides_warns_when_reusing_photo_because_enrichment_lost_photo_url(
        self,
    ) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id="cid:123",
                    name="Open Kitchen",
                    maps_url="https://maps.google.com/?cid=123",
                    status="active",
                )
            ],
        )
        old_photo_url = "https://lh3.googleusercontent.com/p/old-token=s680-w680-h510"
        old_photo_hash = hashlib.sha256(old_photo_url.encode("utf-8")).hexdigest()[:12]

        with TemporaryDirectory() as tmpdir:
            photo_dir = Path(tmpdir)
            existing_path = photo_dir / f"cid-123-{old_photo_hash}.jpg"
            existing_path.write_bytes(b"existing-image")

            stdout = StringIO()
            with (
                patch.object(build_data, "PLACE_PHOTOS_DIR", photo_dir),
                patch.object(build_data, "sync_place_photo", side_effect=AssertionError("should not refresh")),
                redirect_stdout(stdout),
            ):
                build_data.populate_place_photos_for_guides(
                    [guide],
                    enrichment_caches={
                        "tokyo-japan": {
                            "cid:123": EnrichmentCacheEntry(
                                fetched_at="2026-04-20T00:00:00+00:00",
                                query="Open Kitchen",
                                matched=True,
                                place=EnrichmentPlace(),
                            )
                        }
                    },
                    refresh_photos=False,
                    photo_workers=4,
                    startup_jitter_seconds=8,
                )

        self.assertEqual(
            guide.places[0].main_photo_path,
            f"/place-photos/cid-123-{old_photo_hash}.jpg",
        )
        self.assertIn("Photo extraction may be failing on this runner.", stdout.getvalue())

    def test_populate_place_photos_for_guides_skips_remote_url_mode(self) -> None:
        guide = Guide(
            slug="hong-kong",
            title="Hong Kong",
            place_photo_mode="remote_url",
            country_name="Hong Kong",
            city_name="Hong Kong",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id="cid:123",
                    name="Hotel",
                    maps_url="https://maps.google.com/?cid=123",
                    main_photo_path="/place-photos/existing.webp",
                    status="active",
                )
            ],
        )

        with patch.object(build_data, "sync_place_photo", side_effect=AssertionError("should not refresh")):
            build_data.populate_place_photos_for_guides(
                [guide],
                enrichment_caches={
                    "hong-kong": {
                        "cid:123": EnrichmentCacheEntry(
                            fetched_at="2026-04-20T00:00:00+00:00",
                            query="Hotel",
                            matched=True,
                            place=EnrichmentPlace(main_photo_url="https://example.com/hotel.jpg"),
                        )
                    }
                },
                refresh_photos=True,
                photo_workers=4,
                startup_jitter_seconds=8,
            )

        self.assertIsNone(guide.places[0].main_photo_path)

    def test_optimize_place_photo_asset_resizes_to_card_ratio(self) -> None:
        source = BytesIO()
        Image.new("RGB", (1400, 1000), color=(10, 50, 200)).save(source, format="PNG")

        optimized_content, extension = build_data.optimize_place_photo_asset(
            source.getvalue(),
            content_type="image/png",
        )

        self.assertIsNotNone(optimized_content)
        self.assertIn(extension, {".webp", ".jpg"})
        assert optimized_content is not None
        with Image.open(BytesIO(optimized_content)) as optimized_image:
            self.assertEqual(
                optimized_image.size,
                (build_data.PHOTO_CARD_WIDTH, build_data.PHOTO_CARD_HEIGHT),
            )

    def test_place_page_has_meaningful_enrichment_rejects_address_only_false_positive(self) -> None:
        self.assertFalse(
            build_data.place_page_has_meaningful_enrichment(
                SimpleNamespace(limited_view=False),
                EnrichmentPlace(
                    display_name=None,
                    formatted_address="バー · 26-28 Cotham Rd",
                ),
            )
        )

    def test_place_page_has_meaningful_enrichment_accepts_name_address_and_reputation(self) -> None:
        self.assertTrue(
            build_data.place_page_has_meaningful_enrichment(
                SimpleNamespace(limited_view=False),
                EnrichmentPlace(
                    display_name="Bianchetto",
                    formatted_address="26-28 Cotham Rd, Kew VIC 3101, Australia",
                    rating=5.0,
                    user_rating_count=8,
                ),
            )
        )

    def test_place_page_has_meaningful_enrichment_rejects_ui_label_false_positive(self) -> None:
        self.assertFalse(
            build_data.place_page_has_meaningful_enrichment(
                SimpleNamespace(limited_view=False),
                EnrichmentPlace(
                    display_name=None,
                    formatted_address="バー · 26-28 Cotham Rd",
                    primary_type_display_name="バー",
                ),
            )
        )

    def test_should_fallback_to_places_api_for_sparse_search_result(self) -> None:
        entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Bianchetto, 26-28 Cotham Rd, Kew VIC 3101, Australia",
            source="google_maps_page",
            matched=True,
            score=45,
            place=EnrichmentPlace(
                display_name="Bianchetto",
                formatted_address="バー · 26-28 Cotham Rd",
                google_maps_uri=(
                    "https://www.google.com/maps/search/?api=1&query="
                    "Bianchetto%2C+26-28+Cotham+Rd%2C+Kew+VIC+3101%2C+Australia"
                ),
                primary_type_display_name="バー",
            ),
        )

        self.assertTrue(build_data.should_fallback_to_places_api(entry))

    def test_should_not_fallback_to_places_api_for_rich_search_result(self) -> None:
        entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Sister Midnight, 4 Rue Viollet-le-Duc, 75009 Paris, France",
            source="google_maps_page",
            matched=True,
            score=45,
            place=EnrichmentPlace(
                display_name="Sister Midnight",
                formatted_address="4 Rue Viollet-le-Duc, 75009 Paris, France",
                google_maps_uri=(
                    "https://www.google.com/maps/search/?api=1&query="
                    "Sister+Midnight%2C+4+Rue+Viollet-le-Duc%2C+75009+Paris%2C+France"
                ),
                rating=4.7,
                user_rating_count=288,
                website="https://sistermidnightparis.com/",
                primary_type_display_name="Cocktail bar",
            ),
        )

        self.assertFalse(build_data.should_fallback_to_places_api(entry))

    def test_extract_hashtags_normalizes_accented_and_malformed_location_tags(self) -> None:
        tags = build_data.extract_hashtags("Great spots #genève #gen-ve #park")
        self.assertEqual(tags, ["geneve", "park"])

    def test_stable_place_id_fallback_uses_plain_slugify_output(self) -> None:
        place = RawPlace(
            name="Gen-ve Cafe",
            address="1 Example St, Geneva, Switzerland",
            maps_url="https://maps.google.com/?q=Gen-ve+Cafe",
            lat=46.2044,
            lng=6.1432,
        )

        self.assertEqual(
            build_data.stable_place_id(place),
            "slug:gen-ve-cafe-46-2044-6-1432",
        )

    def test_stable_place_id_fallback_does_not_fold_accents(self) -> None:
        place = RawPlace(
            name="Genève Cafe",
            address="1 Example St, Geneva, Switzerland",
            maps_url="https://maps.google.com/?q=Gen%C3%A8ve+Cafe",
            lat=46.2044,
            lng=6.1432,
        )

        self.assertEqual(
            build_data.stable_place_id(place),
            "slug:gen-ve-cafe-46-2044-6-1432",
        )

    def test_normalize_guide_expands_location_aliases_in_list_tags(self) -> None:
        raw = RawSavedList(
            title="Genève, Switzerland",
            description="Walkable guide #genève #park",
            places=[
                RawPlace(
                    name="Parc de La Perle du Lac",
                    address="Rue de Lausanne 120B, 1202 Genève, Switzerland",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            (list_overrides_dir / "geneve-switzerland.json").write_text(
                json.dumps(
                    {
                        "city_name": "Genève",
                        "country_name": "Switzerland",
                        "country_code": "CH",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide("geneve-switzerland", raw, enrichment_cache={})

        self.assertEqual(guide.list_tags, ["geneva", "geneve", "park"])

    def test_normalize_guide_applies_site_build_hook_to_description(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            description="Original description",
            places=[
                RawPlace(
                    name="Koffee Mameya",
                    address="4-15-3 Jingumae, Shibuya City, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="1",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            hooks_path = tmpdir_path / "build_hooks.py"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()
            hooks_path.write_text(
                "\n".join(
                    [
                        "def transform_guide_description(description, *, slug, raw, list_override):",
                        "    return f\"{slug}: {description}\"",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            build_data._load_site_build_hooks_module.cache_clear()
            try:
                with (
                    patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                    patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                    patch.object(build_data, "SITE_BUILD_HOOKS_PATH", hooks_path),
                ):
                    guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})
            finally:
                build_data._load_site_build_hooks_module.cache_clear()

        self.assertEqual(guide.description, "tokyo-japan: Original description")

    def test_normalize_guide_collapses_blank_lines_after_site_hook_changes(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            description="\n\nOriginal description\n\n",
            places=[
                RawPlace(
                    name="Coffee Supreme",
                    address="Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="1",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            hooks_path = tmpdir_path / "build_hooks.py"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()
            hooks_path.write_text(
                "\n".join(
                    [
                        "def transform_guide_description(description, *, slug, raw, list_override):",
                        "    return f'\\n\\n{description}\\n\\n\\nExtra note\\n\\n'",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            build_data._load_site_build_hooks_module.cache_clear()
            try:
                with (
                    patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                    patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                    patch.object(build_data, "SITE_BUILD_HOOKS_PATH", hooks_path),
                ):
                    guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})
            finally:
                build_data._load_site_build_hooks_module.cache_clear()

        self.assertEqual(guide.description, "Original description\n\nExtra note")

    def test_load_site_build_hooks_module_does_not_cache_missing_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            hooks_path = Path(tmpdir) / "build_hooks.py"

            self.assertIsNone(build_data.load_site_build_hooks_module(hooks_path))
            hooks_path.write_text("hook_loaded = True\n", encoding="utf-8")

            try:
                module = build_data.load_site_build_hooks_module(hooks_path)
            finally:
                build_data._load_site_build_hooks_module.cache_clear()

        self.assertIsNotNone(module)
        self.assertTrue(module.hook_loaded)

    def test_normalize_text_blocks_strips_outer_blank_lines_and_collapses_internal_runs(self) -> None:
        self.assertEqual(
            build_data.normalize_text_blocks("\n\nAlpha  \n\n\nBeta\n \n\nGamma\n\n"),
            "Alpha\n\nBeta\n\nGamma",
        )

    def test_normalize_guide_normalizes_accented_override_list_tags(self) -> None:
        raw = RawSavedList(
            title="Genève, Switzerland",
            places=[
                RawPlace(
                    name="Parc de La Perle du Lac",
                    address="Rue de Lausanne 120B, 1202 Genève, Switzerland",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            (list_overrides_dir / "geneve-switzerland.json").write_text(
                json.dumps({"list_tags": ["Genève", "Park"]}),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide("geneve-switzerland", raw, enrichment_cache={})

        self.assertEqual(guide.list_tags, ["geneva", "geneve", "park"])

    def test_normalize_guide_skips_placeholder_override_list_tags(self) -> None:
        raw = RawSavedList(
            title="Genève, Switzerland",
            places=[
                RawPlace(
                    name="Parc de La Perle du Lac",
                    address="Rue de Lausanne 120B, 1202 Genève, Switzerland",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            (list_overrides_dir / "geneve-switzerland.json").write_text(
                json.dumps({"list_tags": ["🌯", "東京", "Park"]}),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide("geneve-switzerland", raw, enrichment_cache={})

        self.assertEqual(guide.list_tags, ["park"])

    def test_normalize_guide_applies_overrides_and_hides_places_from_counts(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            description="Local favorites. #food #coffee",
            source_url="https://maps.app.goo.gl/tokyo",
            places=[
                RawPlace(
                    name="Coffee House",
                    address="1 Shibuya, Tokyo, Japan",
                    note="Original note",
                    is_favorite=False,
                    lat=35.65,
                    lng=139.7,
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                ),
                RawPlace(
                    name="Noodle Shop",
                    address="2 Shinjuku, Tokyo, Japan",
                    note="Best at lunch",
                    is_favorite=True,
                    lat=35.66,
                    lng=139.71,
                    maps_url="https://maps.google.com/?cid=2",
                    cid="222",
                ),
                RawPlace(
                    name="Hidden Bar",
                    address="3 Ebisu, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=3",
                    cid="333",
                ),
            ],
        )
        first_place_id = build_data.stable_place_id(raw.places[0])
        second_place_id = build_data.stable_place_id(raw.places[1])
        third_place_id = build_data.stable_place_id(raw.places[2])

        enrichment_cache = {
            first_place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-01T00:00:00+00:00",
                query="Coffee House, Tokyo",
                matched=True,
                score=88,
                place=EnrichmentPlace(
                    google_maps_uri="https://maps.google.com/?cid=override",
                    primary_type="cafe",
                    primary_type_display_name="Coffee shop",
                    formatted_address="1 Shibuya, Tokyo, Japan",
                    types=["cafe", "food"],
                    business_status="OPERATIONAL",
                ),
            )
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            (list_overrides_dir / "tokyo-japan.json").write_text(
                json.dumps(
                    {
                        "title": "Michael's Tokyo",
                        "city_name": "Tokyo",
                        "country_name": "Japan",
                        "country_code": "JP",
                        "list_tags": ["manual-tag"],
                        "featured_place_ids": [second_place_id],
                    }
                ),
                encoding="utf-8",
            )
            (place_overrides_dir / "tokyo-japan.json").write_text(
                json.dumps(
                    {
                        first_place_id: {
                            "top_pick": True,
                            "note": "Manual note",
                            "primary_category": "Bakery",
                            "tags": ["specialty"],
                            "manual_rank": 3,
                        },
                        third_place_id: {"hidden": True},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide(
                    "tokyo-japan",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.title, "Michael's Tokyo")
        self.assertEqual(guide.country_name, "Japan")
        self.assertEqual(guide.city_name, "Tokyo")
        self.assertEqual(guide.list_tags, ["coffee", "food", "manual-tag"])
        self.assertEqual(guide.place_count, 2)
        self.assertEqual(guide.featured_place_ids, [second_place_id, first_place_id])
        self.assertAlmostEqual(guide.center_lat or 0, 35.655, places=3)
        self.assertAlmostEqual(guide.center_lng or 0, 139.705, places=3)

        first_place = guide.places[0]
        hidden_place = next(place for place in guide.places if place.id == third_place_id)

        self.assertEqual(first_place.id, first_place_id)
        self.assertEqual(first_place.primary_category, "Bakery")
        self.assertEqual(first_place.marker_icon, "bakery")
        self.assertEqual(first_place.note, "Manual note")
        self.assertEqual(first_place.why_recommended, "Manual note")
        self.assertEqual(
            first_place.maps_url,
            "https://www.google.com/maps/search/?api=1&query=Coffee+House%2C+1+Shibuya%2C+Tokyo%2C+Japan",
        )
        self.assertEqual(first_place.neighborhood, "Shibuya")
        self.assertTrue(first_place.top_pick)
        self.assertIn("local-favorite", first_place.vibe_tags)
        self.assertIn("quick-stop", first_place.vibe_tags)
        self.assertEqual(first_place.status, "active")
        self.assertIn("bakery", first_place.tags)
        self.assertIn("shibuya", first_place.tags)
        self.assertIn("specialty", first_place.tags)
        self.assertIn("tokyo", first_place.tags)
        self.assertEqual(first_place.provenance.name.source, "google_list")
        self.assertEqual(first_place.provenance.address.source, "google_places")
        self.assertEqual(first_place.provenance.maps_url.source, "google_places")
        self.assertEqual(first_place.provenance.primary_category.source, "manual")
        self.assertEqual(first_place.provenance.note.source, "manual")
        self.assertEqual(first_place.provenance.why_recommended.source, "manual")
        self.assertEqual(first_place.provenance.top_pick.source, "manual")
        self.assertEqual(first_place.provenance.status.source, "google_places")
        self.assertEqual(
            {field.value: field.source for field in first_place.provenance.tags},
            {
                "specialty": "manual",
                "tokyo": "google_list",
                "shibuya": "google_list",
                "bakery": "manual",
                "cafe": "google_places",
                "food": "google_places",
            },
        )
        self.assertTrue(hidden_place.hidden)

    def test_normalize_guide_combines_google_description_and_note_without_semantic_copy(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Coffee House",
                    note="Saved-list note from the curator.",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-01T00:00:00+00:00",
                query="Coffee House, Tokyo",
                place=EnrichmentPlace(
                    description="Google place description.",
                    primary_type_display_name="Coffee shop",
                ),
            )
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
                patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            ):
                guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache=enrichment_cache)

        self.assertEqual(
            guide.places[0].why_recommended,
            "Google place description.\n\nSaved-list note from the curator.",
        )
        self.assertEqual(guide.places[0].note, "Saved-list note from the curator.")

    def test_normalize_guide_uses_search_result_description_as_description_backup(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Coffee House",
                    note="Saved-list note from the curator.",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-01T00:00:00+00:00",
                query="Coffee House, Tokyo",
                place=EnrichmentPlace(
                    search_result_description="Search-result place description.",
                    primary_type_display_name="Coffee shop",
                ),
            )
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
                patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            ):
                guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache=enrichment_cache)

        self.assertEqual(
            guide.places[0].why_recommended,
            "Search-result place description.\n\nSaved-list note from the curator.",
        )

    def test_normalize_guide_prefers_semantic_description_over_deterministic_description_fallback(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Coffee House",
                    note="Saved-list note from the curator.",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-01T00:00:00+00:00",
                query="Coffee House, Tokyo",
                place=EnrichmentPlace(
                    description="Google place description.",
                    search_result_description="Search-result place description.",
                    semantic_description="LLM semantic description.",
                    primary_type_display_name="Coffee shop",
                ),
            )
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
                patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            ):
                guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache=enrichment_cache)

        self.assertEqual(guide.places[0].why_recommended, "LLM semantic description.")

    def test_normalize_guide_rejects_manual_why_recommended_override(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Coffee House",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            place_overrides_dir = tmpdir_path / "places"
            place_overrides_dir.mkdir()
            (place_overrides_dir / "tokyo-japan.json").write_text(
                json.dumps(
                    {
                        place_id: {
                            "why_recommended": "Unsupported handwritten description.",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir):
                with self.assertRaisesRegex(ValueError, "use 'note' for handwritten recommendation copy"):
                    build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})

    def test_normalize_guide_combines_google_description_and_saved_note_without_semantic_description(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[
                RawPlace(
                    name="Taipei Zoo",
                    note="Go early for the panda house.",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                source="google_maps_page",
                query="Taipei Zoo, Taipei, Taiwan",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Taipei Zoo",
                    description="Large indoor-outdoor zoo in a scenic setting with a children's area.",
                    semantic_description="A semantic description should be ignored when disabled.",
                ),
            )
        }

        with (
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
        ):
            guide = build_data.normalize_guide(
                "taipei-taiwan",
                raw,
                enrichment_cache=enrichment_cache,
            )

        self.assertEqual(
            guide.places[0].why_recommended,
            "Large indoor-outdoor zoo in a scenic setting with a children's area.\n\n"
            "Go early for the panda house.",
        )

    def test_normalize_guide_uses_semantic_description_before_google_description_and_saved_note(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[
                RawPlace(
                    name="Taipei Zoo",
                    note="Go early for the panda house.",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                source="google_maps_page",
                query="Taipei Zoo, Taipei, Taiwan",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Taipei Zoo",
                    description="Large indoor-outdoor zoo in a scenic setting with a children's area.",
                    semantic_description="A concise LLM description grounded in the zoo summary and panda note.",
                ),
            )
        }

        with (
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=True),
        ):
            guide = build_data.normalize_guide(
                "taipei-taiwan",
                raw,
                enrichment_cache=enrichment_cache,
            )

        self.assertEqual(
            guide.places[0].why_recommended,
            "A concise LLM description grounded in the zoo summary and panda note.",
        )

    def test_normalize_guide_rejects_recommendation_copy_from_mismatched_enrichment_identity(self) -> None:
        raw = RawSavedList(
            title="Las Vegas, Nevada",
            places=[
                RawPlace(
                    name="Niu-Gu Noodle House",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                source="google_maps_page",
                query="Niu-Gu Noodle House, Las Vegas, Nevada",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Michael's",
                    description="Long-running steakhouse with fine dining and a polished bar.",
                    search_result_description="Classic steakhouse in Las Vegas.",
                    semantic_description="An enduring Henderson steakhouse offering fine dining with classic cuts.",
                ),
            )
        }

        with (
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=True),
        ):
            guide = build_data.normalize_guide(
                "las-vegas-nevada-usa",
                raw,
                enrichment_cache=enrichment_cache,
            )

        self.assertIsNone(guide.places[0].why_recommended)

    def test_normalize_guide_uses_search_result_description_when_full_description_missing(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[
                RawPlace(
                    name="Taipei Zoo",
                    note="Go early for the panda house.",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                source="google_maps_page",
                query="Taipei Zoo, Taipei, Taiwan",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Taipei Zoo",
                    search_result_description="Sizable zoo with a gondola & kids' area",
                ),
            )
        }

        with (
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
        ):
            guide = build_data.normalize_guide(
                "taipei-taiwan",
                raw,
                enrichment_cache=enrichment_cache,
            )

        self.assertEqual(
            guide.places[0].why_recommended,
            "Sizable zoo with a gondola & kids' area\n\nGo early for the panda house.",
        )

    def test_normalize_guide_rejects_low_information_enrichment_description(self) -> None:
        raw = RawSavedList(
            title="Hong Kong",
            places=[
                RawPlace(
                    name="Star Ferry Pier",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-15T00:00:00+00:00",
                source="google_maps_page",
                query="Star Ferry Pier, Hong Kong",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Star Ferry Pier",
                    description="Building",
                    primary_type_display_name="Restaurant",
                ),
            )
        }

        with (
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
        ):
            guide = build_data.normalize_guide(
                "hong-kong",
                raw,
                enrichment_cache=enrichment_cache,
            )

        self.assertIsNone(guide.places[0].why_recommended)

    def test_normalize_guide_rejects_enrichment_category_for_ambiguous_location_types(self) -> None:
        raw = RawSavedList(
            title="Hong Kong",
            places=[
                RawPlace(
                    name="Star Ferry Pier",
                    maps_url="https://maps.google.com/?cid=111",
                    google_id="ChIJSTARFERRY",
                    types=["premise"],
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-15T00:00:00+00:00",
                source="google_maps_page",
                query="Star Ferry Pier, Hong Kong",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Star Ferry Pier",
                    description="Building",
                    primary_type="restaurant",
                    primary_type_display_name="Restaurant",
                    types=["restaurant"],
                ),
            )
        }

        with (
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
        ):
            guide = build_data.normalize_guide(
                "hong-kong",
                raw,
                enrichment_cache=enrichment_cache,
            )

        self.assertIsNone(guide.places[0].primary_category)

    def test_raw_place_primary_category_prefers_lodging_over_leading_cafe_tag(self) -> None:
        place = RawPlace(
            name="Harbour View Hotel",
            maps_url="https://maps.google.com/?cid=111",
            types=["cafe", "lodging", "restaurant", "food", "point_of_interest", "establishment"],
        )

        self.assertEqual(build_data.raw_place_primary_category(place), "Lodging")

    def test_normalize_guide_prefers_raw_note_when_enrichment_description_is_only_local_name(self) -> None:
        raw = RawSavedList(
            title="Hong Kong",
            places=[
                RawPlace(
                    name="Tai O",
                    note="From the web: Fishing town on western Lantau known for stilt houses and seafood.",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-15T00:00:00+00:00",
                source="google_maps_page",
                query="Tai O, Hong Kong",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Tai O",
                    description="大澳",
                    formatted_address="Tai O, Hong Kong",
                ),
            )
        }

        with (
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
        ):
            guide = build_data.normalize_guide(
                "hong-kong",
                raw,
                enrichment_cache=enrichment_cache,
            )

        self.assertEqual(
            guide.places[0].why_recommended,
            "From the web: Fishing town on western Lantau known for stilt houses and seafood.",
        )

    def test_normalize_guide_prefers_display_address_over_raw_saved_list_address(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[
                RawPlace(
                    name="Tea House",
                    address="110台灣台北市信義區松高路12號",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                source="google_maps_page",
                query="Tea House, Taipei, Taiwan",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Tea House",
                    formatted_address="110台灣台北市信義區松高路12號",
                    address_display_en="No. 12, Songgao Rd, Xinyi District, Taipei City",
                ),
            )
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()
            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide(
                    "taipei-taiwan",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(
            guide.places[0].address,
            "No. 12, Songgao Rd, Xinyi District, Taipei City",
        )
        self.assertIsNotNone(guide.places[0].provenance.address)
        assert guide.places[0].provenance.address is not None
        self.assertEqual(guide.places[0].provenance.address.source, "google_maps_page")
        self.assertIn("No.+12%2C+Songgao+Rd", guide.places[0].maps_url)

    def test_normalize_guide_prefers_enriched_formatted_address_over_raw_address(self) -> None:
        raw = RawSavedList(
            title="Yilan, Taiwan",
            places=[
                RawPlace(
                    name="Coming Home Bar",
                    address="260, Taiwan, Yilan County, Yilan City, Nongquan Rd, 110號1F",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                source="google_maps_page",
                query="Coming Home Bar, Yilan, Taiwan",
                matched=True,
                place=EnrichmentPlace(
                    display_name="Coming Home Bar",
                    formatted_address=(
                        "260, Taiwan, Yilan County, Yilan City, "
                        "Minquan Village, Nongquan Rd, 110號1F"
                    ),
                ),
            )
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()
            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide(
                    "yilan-taiwan",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(
            guide.places[0].address,
            "No. 110, 1F, Nongquan Rd, Minquan Village, Yilan City, Yilan County, Taiwan 260",
        )
        self.assertIsNotNone(guide.places[0].provenance.address)
        assert guide.places[0].provenance.address is not None
        self.assertEqual(guide.places[0].provenance.address.source, "google_maps_page")

    def test_place_vibe_tags_can_be_manually_overridden(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Quiet Coffee",
                    address="1 Shibuya, Tokyo, Japan",
                    note="Quiet cafe with wifi and outlets.",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()
            (place_overrides_dir / "tokyo-japan.json").write_text(
                json.dumps({place_id: {"vibe_tags": ["date-night"]}}),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide(
                    "tokyo-japan",
                    raw,
                    enrichment_cache={},
                )

        self.assertEqual(guide.places[0].vibe_tags, ["date-night"])

    def test_normalize_guide_marks_enriched_neighborhood_provenance_when_raw_address_missing(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[
                RawPlace(
                    name="象山",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-26T00:00:00+00:00",
                query="象山, Taipei, Taiwan",
                matched=True,
                source="google_maps_page",
                place=EnrichmentPlace(
                    formatted_address="2HGG+M6",
                    primary_type="trailhead",
                    primary_type_display_name="Trailhead",
                    address_parts=[
                        "Trailhead",
                        "Xinyi District, Taipei City",
                        "Xinyi District, Taipei City",
                        "Xinyi District",
                        "110",
                        "Taipei City",
                        "TW",
                        ["Taiwan"],
                    ],
                ),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide(
                    "taipei-taiwan",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.places[0].neighborhood, "Xinyi District")
        self.assertIsNotNone(guide.places[0].provenance.neighborhood)
        assert guide.places[0].provenance.neighborhood is not None
        self.assertEqual(guide.places[0].provenance.neighborhood.source, "google_maps_page")

    def test_normalize_guide_prefers_specific_address_locality_over_broad_semantic_parent(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Higashiazabu Amamoto",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                query="Higashiazabu Amamoto, Tokyo, Japan",
                matched=True,
                source="google_maps_page",
                place=EnrichmentPlace(
                    display_name="Higashiazabu Amamoto",
                    formatted_address="1 Chome-7-9 Higashiazabu, Minato City, Tokyo 106-0044, Japan",
                    primary_type_display_name="Sushi restaurant",
                    semantic_neighborhood="Azabu",
                ),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=True),
            ):
                guide = build_data.normalize_guide(
                    "tokyo-japan",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.places[0].neighborhood, "Higashiazabu")

    def test_normalize_guide_applies_site_neighborhood_mapping_rules(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[
                RawPlace(
                    name="Wang’s Broth",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                query="Wang’s Broth, Taipei, Taiwan",
                matched=True,
                source="google_maps_page",
                place=EnrichmentPlace(
                    display_name="Wang’s Broth",
                    formatted_address="No. 17-4, Huaxi St, Wanhua District, Taipei City, Taiwan 108",
                    primary_type_display_name="Deli",
                    semantic_neighborhood="Wanhua District",
                ),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(
                    build_data,
                    "google_maps_place_neighborhood_mappings",
                    return_value=[
                        {
                            "city": "Taipei",
                            "country": "Taiwan",
                            "from": "Wanhua District",
                            "to": "Wanhua",
                            "when_address_contains": "Wanhua District",
                        }
                    ],
                ),
            ):
                guide = build_data.normalize_guide(
                    "taipei-taiwan",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.places[0].neighborhood, "Wanhua")

    def test_normalize_guide_applies_site_neighborhood_mapping_alias_lists(self) -> None:
        raw = RawSavedList(
            title="Montreal, Canada",
            places=[
                RawPlace(
                    name="Plateau Cafe",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                query="Plateau Cafe, Montreal, Canada",
                matched=True,
                source="google_maps_page",
                place=EnrichmentPlace(
                    display_name="Plateau Cafe",
                    formatted_address="100 Mont-Royal Ave E, Montréal, QC, Canada",
                    primary_type_display_name="Cafe",
                    semantic_neighborhood="Le Plateau",
                ),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=True),
                patch.object(
                    build_data,
                    "google_maps_place_neighborhood_mappings",
                    return_value=[
                        {
                            "city": "Montreal",
                            "country": "Canada",
                            "from": ["The Plateau", "Le Plateau"],
                            "to": "Plateau",
                        }
                    ],
                ),
            ):
                guide = build_data.normalize_guide(
                    "montreal-canada",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.places[0].neighborhood, "Plateau")

    def test_normalize_guide_applies_country_level_site_neighborhood_mapping_rules(self) -> None:
        raw = RawSavedList(
            title="Busan, Korea",
            places=[
                RawPlace(
                    name="Seomyeon Cafe",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                query="Seomyeon Cafe, Busan, Korea",
                matched=True,
                source="google_maps_page",
                place=EnrichmentPlace(
                    display_name="Seomyeon Cafe",
                    formatted_address="22-9 Seojeon-ro 10beon-gil, Busanjin District, Busan, South Korea",
                    primary_type_display_name="Cafe",
                    semantic_neighborhood="Busanjin District",
                ),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(
                    build_data,
                    "google_maps_place_neighborhood_mappings",
                    return_value=[
                        {
                            "country": "Korea",
                            "from": "Busanjin District",
                            "to": "Busanjin",
                        }
                    ],
                ),
            ):
                guide = build_data.normalize_guide(
                    "busan-korea",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.places[0].neighborhood, "Busanjin")

    def test_apply_site_neighborhood_mappings_expands_exact_observed_labels(self) -> None:
        rules = [
            {
                "city": "Mexico City",
                "country": "Mexico",
                "from": "Roma Nte",
                "to": "Roma Norte",
            },
            {
                "city": "Mexico City",
                "country": "Mexico",
                "from": "Col del Valle Nte",
                "to": "Del Valle Norte",
            },
            {
                "city": "Mexico City",
                "country": "Mexico",
                "from": "Balderas S/N",
                "to": "Balderas",
            },
            {
                "city": "Guadalajara",
                "country": "Mexico",
                "from": "Vallarta Nte",
                "to": "Vallarta Norte",
            },
            {
                "city": "Montreal",
                "country": "Canada",
                "from": "Vieux-Montréal",
                "to": "Old Montreal",
            },
            {
                "city": "Luxembourg",
                "country": "Luxembourg",
                "from": ["Ville Haute Luxembourg", "Ville-Haute Luxembourg"],
                "to": "Ville Haute",
            },
            {
                "city": "Luxembourg",
                "country": "Luxembourg",
                "from": ["Pafendall", "Pafendall Luxembourg"],
                "to": "Pfaffenthal",
            },
            {
                "city": "Luxembourg",
                "country": "Luxembourg",
                "from": "Cote d'Eich",
                "to": "Côte d'Eich",
            },
            {
                "city": "Paris",
                "country": "France",
                "from": "Bd Saint-Germain",
                "to": "Boulevard Saint-Germain",
            },
            {
                "city": "Paris",
                "country": "France",
                "from": "Gal de Valois",
                "to": "Galerie de Valois",
            },
            {
                "city": "Cannes",
                "country": "France",
                "from": "Bd de la République",
                "to": "Boulevard de la République",
            },
            {
                "city": "Cannes",
                "country": "France",
                "from": "Jetée Albert Edouard",
                "to": "Jetée Albert Édouard",
            },
            {
                "city": "Cannes",
                "country": "France",
                "from": "Musée de La Castre",
                "to": "Musée de la Castre",
            },
            {
                "city": "Lyon",
                "country": "France",
                "from": "Cr Lafayette F",
                "to": "Cours Lafayette",
            },
            {
                "city": "Metz",
                "country": "France",
                "from": "Parv. des Droits de l'Homme CS",
                "to": "Parvis des Droits de l'Homme",
            },
            {
                "city": "Sydney",
                "country": "Australia",
                "from": "The Rocks NSW オーストラリア",
                "to": "The Rocks",
            },
            {
                "city": "Bologna",
                "country": "Italy",
                "from": "Bologna Centro Storico",
                "to": "Centro Storico",
            },
            {
                "city": "Milan",
                "country": "Italy",
                "from": "P.za del Duomo",
                "to": "Piazza del Duomo",
            },
            {
                "city": "Madrid",
                "country": "Spain",
                "from": "P.º del Prado",
                "to": "Paseo del Prado",
            },
        ]

        cases = [
            ("Roma Nte", "Mexico City", "Mexico", "Roma Norte"),
            ("Col del Valle Nte", "Mexico City", "Mexico", "Del Valle Norte"),
            ("Balderas S/N", "Mexico City", "Mexico", "Balderas"),
            ("Vallarta Nte", "Guadalajara", "Mexico", "Vallarta Norte"),
            ("Vieux-Montréal", "Montreal", "Canada", "Old Montreal"),
            ("Ville-Haute Luxembourg", "Luxembourg", "Luxembourg", "Ville Haute"),
            ("Pafendall Luxembourg", "Luxembourg", "Luxembourg", "Pfaffenthal"),
            ("Cote d'Eich", "Luxembourg", "Luxembourg", "Côte d'Eich"),
            ("Bd Saint-Germain", "Paris", "France", "Boulevard Saint-Germain"),
            ("Gal de Valois", "Paris", "France", "Galerie de Valois"),
            ("Bd de la République", "Cannes", "France", "Boulevard de la République"),
            ("Jetée Albert Edouard", "Cannes", "France", "Jetée Albert Édouard"),
            ("Musée de La Castre", "Cannes", "France", "Musée de la Castre"),
            ("Cr Lafayette F", "Lyon", "France", "Cours Lafayette"),
            ("Parv. des Droits de l'Homme CS", "Metz", "France", "Parvis des Droits de l'Homme"),
            ("The Rocks NSW オーストラリア", "Sydney", "Australia", "The Rocks"),
            ("Bologna Centro Storico", "Bologna", "Italy", "Centro Storico"),
            ("P.za del Duomo", "Milan", "Italy", "Piazza del Duomo"),
            ("P.º del Prado", "Madrid", "Spain", "Paseo del Prado"),
        ]

        with patch.object(build_data, "google_maps_place_neighborhood_mappings", return_value=rules):
            for neighborhood, city_name, country_name, expected in cases:
                with self.subTest(neighborhood=neighborhood):
                    self.assertEqual(
                        build_data.apply_site_neighborhood_mappings(
                            neighborhood,
                            city_name=city_name,
                            country_name=country_name,
                            address_values=[],
                            locality_candidates=[neighborhood],
                        ),
                        expected,
                    )

    def test_normalize_guide_applies_site_category_mapping_rules(self) -> None:
        raw = RawSavedList(
            title="Montreal, Canada",
            places=[
                RawPlace(
                    name="Kitano Shokudo",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                query="Kitano Shokudo, Montreal, Canada",
                matched=True,
                source="google_maps_page",
                place=EnrichmentPlace(
                    display_name="Kitano Shokudo",
                    primary_type_display_name="Authentic Japanese restaurant",
                ),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(
                    build_data,
                    "google_maps_place_category_mappings",
                    return_value=[
                        {
                            "city": "Montreal",
                            "country": "Canada",
                            "from": "Authentic Japanese restaurant",
                            "to": "Japanese restaurant",
                        }
                    ],
                ),
            ):
                guide = build_data.normalize_guide(
                    "montreal-canada",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.places[0].primary_category, "Japanese restaurant")
        self.assertIn("japanese-restaurant", guide.places[0].tags)
        self.assertNotIn("authentic-japanese-restaurant", guide.places[0].tags)

    def test_normalize_guide_keeps_category_tag_when_mapping_preserves_slug(self) -> None:
        raw = RawSavedList(
            title="Montreal, Canada",
            places=[
                RawPlace(
                    name="Cafe",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                query="Cafe, Montreal, Canada",
                matched=True,
                source="google_maps_page",
                place=EnrichmentPlace(
                    display_name="Cafe",
                    primary_type_display_name="coffee shop",
                ),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(
                    build_data,
                    "google_maps_place_category_mappings",
                    return_value=[
                        {
                            "city": "Montreal",
                            "country": "Canada",
                            "from": "coffee shop",
                            "to": "Coffee Shop",
                        }
                    ],
                ),
            ):
                guide = build_data.normalize_guide(
                    "montreal-canada",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.places[0].primary_category, "Coffee Shop")
        self.assertIn("coffee-shop", guide.places[0].tags)

    def test_normalize_guide_preserves_manual_tags_when_category_mapping_prunes_source_tag(self) -> None:
        raw = RawSavedList(
            title="Montreal, Canada",
            places=[
                RawPlace(
                    name="Kitano Shokudo",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                query="Kitano Shokudo, Montreal, Canada",
                matched=True,
                source="google_maps_page",
                place=EnrichmentPlace(
                    display_name="Kitano Shokudo",
                    primary_type_display_name="Authentic Japanese restaurant",
                ),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()
            (place_overrides_dir / "montreal-canada.json").write_text(
                json.dumps(
                    {
                        place_id: {
                            "tags": ["authentic-japanese-restaurant", "izakaya"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
                patch.object(
                    build_data,
                    "google_maps_place_category_mappings",
                    return_value=[
                        {
                            "city": "Montreal",
                            "country": "Canada",
                            "from": "Authentic Japanese restaurant",
                            "to": "Japanese restaurant",
                        }
                    ],
                ),
            ):
                guide = build_data.normalize_guide(
                    "montreal-canada",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.places[0].primary_category, "Japanese restaurant")
        self.assertIn("japanese-restaurant", guide.places[0].tags)
        self.assertIn("authentic-japanese-restaurant", guide.places[0].tags)
        self.assertIn("izakaya", guide.places[0].tags)

    def test_normalize_guide_excludes_permanently_closed_places_from_ui_counts(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Active Coffee",
                    address="1 Shibuya, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                    lat=35.66,
                    lng=139.7,
                ),
                RawPlace(
                    name="Closed Cafe",
                    address="2 Shibuya, Tokyo, Japan",
                    is_favorite=True,
                    maps_url="https://maps.google.com/?cid=2",
                    cid="222",
                    lat=40.0,
                    lng=140.0,
                ),
                RawPlace(
                    name="Temporary Tea",
                    address="3 Shibuya, Tokyo, Japan",
                    is_favorite=True,
                    maps_url="https://maps.google.com/?cid=3",
                    cid="333",
                    lat=35.68,
                    lng=139.72,
                ),
            ],
        )
        active_place_id = build_data.stable_place_id(raw.places[0])
        closed_place_id = build_data.stable_place_id(raw.places[1])
        temporary_place_id = build_data.stable_place_id(raw.places[2])
        enrichment_cache = {
            active_place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-01T00:00:00+00:00",
                query="Active Coffee",
                matched=True,
                place=EnrichmentPlace(business_status="OPERATIONAL"),
            ),
            closed_place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-01T00:00:00+00:00",
                query="Closed Cafe",
                matched=True,
                place=EnrichmentPlace(business_status="CLOSED_PERMANENTLY"),
            ),
            temporary_place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-01T00:00:00+00:00",
                query="Temporary Tea",
                matched=True,
                place=EnrichmentPlace(business_status="CLOSED_TEMPORARILY"),
            ),
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()
            (list_overrides_dir / "tokyo-japan.json").write_text(
                json.dumps({"featured_place_ids": [closed_place_id, temporary_place_id]}),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide(
                    "tokyo-japan",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        self.assertEqual(guide.place_count, 2)
        self.assertEqual(guide.featured_place_ids, [temporary_place_id])
        self.assertAlmostEqual(guide.center_lat or 0, 35.67, places=2)
        self.assertAlmostEqual(guide.center_lng or 0, 139.71, places=2)
        self.assertEqual(
            {place.id: place.status for place in guide.places},
            {
                active_place_id: "active",
                closed_place_id: "closed-permanently",
                temporary_place_id: "temporarily-closed",
            },
        )

    def test_place_vibe_tags_can_be_manually_cleared(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Quiet Coffee",
                    address="1 Shibuya, Tokyo, Japan",
                    note="Quiet cafe with wifi and outlets.",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()
            (place_overrides_dir / "tokyo-japan.json").write_text(
                json.dumps({place_id: {"vibe_tags": []}}),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide(
                    "tokyo-japan",
                    raw,
                    enrichment_cache={},
                )

        self.assertEqual(guide.places[0].vibe_tags, [])

    def test_page_backed_enrichment_uses_google_maps_page_provenance(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Coffee House",
                    address="1 Shibuya, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-01T00:00:00+00:00",
                refresh_after="2026-04-08T00:00:00+00:00",
                source="google_maps_page",
                query="Coffee House, Tokyo",
                matched=True,
                score=45,
                place=EnrichmentPlace(
                    display_name="Coffee House",
                    formatted_address="1 Shibuya, Tokyo, Japan",
                    google_maps_uri="https://www.google.com/maps/place/Coffee+House",
                    primary_type="coffee_shop",
                    primary_type_display_name="Coffee shop",
                    types=["coffee_shop"],
                ),
            )
        }

        guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache=enrichment_cache)
        place = guide.places[0]

        self.assertEqual(place.maps_url, "https://www.google.com/maps/place/Coffee+House")
        self.assertEqual(place.provenance.maps_url.source, "google_maps_page")
        self.assertEqual(place.provenance.primary_category.source, "google_maps_page")
        self.assertIn(
            "google_maps_page",
            {field.source for field in place.provenance.tags},
        )

    def test_vibe_tags_match_snake_case_enrichment_types(self) -> None:
        vibes = build_data.derive_vibe_tags(
            RawPlace(
                name="Plain Place",
                address="1 Shibuya, Tokyo, Japan",
                maps_url="https://maps.google.com/?cid=1",
            ),
            enrichment=EnrichmentPlace(primary_type="coffee_shop", types=["coffee_shop"]),
            category="coffee_shop",
            tags=[],
            note=None,
            why_recommended=None,
            top_pick=False,
        )

        self.assertIn("cozy", vibes)
        self.assertIn("slow-afternoon", vibes)
        self.assertIn("solo-friendly", vibes)

    def test_derive_marker_icon_prefers_specific_type_matches(self) -> None:
        test_cases = [
            (
                "coffee-shop",
                EnrichmentPlace(primary_type="restaurant", types=["coffee_shop", "restaurant"]),
                "cafe",
            ),
            (
                "art-gallery",
                EnrichmentPlace(types=["art_gallery"]),
                "museum",
            ),
            (
                None,
                EnrichmentPlace(types=["historical_landmark"]),
                "attraction",
            ),
            (
                None,
                EnrichmentPlace(types=["spa"]),
                "spa",
            ),
            (
                "night market",
                EnrichmentPlace(),
                "shopping",
            ),
        ]

        for category, enrichment, expected_icon in test_cases:
            with self.subTest(category=category, expected_icon=expected_icon):
                self.assertEqual(
                    build_data.derive_marker_icon(
                        RawPlace(name="Plain Place", maps_url="https://maps.google.com/?cid=1"),
                        enrichment=enrichment,
                        category=category,
                        note=None,
                        why_recommended=None,
                    ),
                    expected_icon,
                )

    def test_derive_marker_icon_falls_back_to_default_without_type_signal(self) -> None:
        self.assertEqual(
            build_data.derive_marker_icon(
                RawPlace(name="Plain Place", maps_url="https://maps.google.com/?cid=1"),
                enrichment=EnrichmentPlace(),
                category=None,
                note=None,
                why_recommended=None,
            ),
            "default",
        )

    def test_derive_enrichment_type_tags_expands_parent_category_tags(self) -> None:
        self.assertEqual(
            build_data.derive_enrichment_type_tags(
                EnrichmentPlace(primary_type="peruvian_restaurant", types=["peruvian_restaurant"])
            ),
            ["restaurant", "peruvian-restaurant"],
        )
        self.assertEqual(
            build_data.derive_enrichment_type_tags(
                EnrichmentPlace(primary_type="coffee_shop", types=["coffee_shop"])
            ),
            ["cafe", "coffee-shop"],
        )
        self.assertEqual(
            build_data.derive_enrichment_type_tags(
                EnrichmentPlace(primary_type="art_gallery", types=["art_gallery"])
            ),
            ["museum", "art-gallery"],
        )
        self.assertEqual(
            build_data.derive_enrichment_type_tags(
                EnrichmentPlace(primary_type="udon_noodle_restaurant", types=["udon_noodle_restaurant"])
            ),
            ["restaurant", "japanese-restaurant", "udon-noodle-restaurant"],
        )
        self.assertEqual(
            build_data.derive_enrichment_type_tags(
                EnrichmentPlace(primary_type="steak_house", types=["steak_house"])
            ),
            ["restaurant", "western-restaurant", "steak-house"],
        )

    def test_derive_visible_place_tags_prioritizes_semantic_and_category_tags(self) -> None:
        self.assertEqual(
            build_data.derive_visible_place_tags(
                tags=["tokyo", "restaurant", "food", "ginza", "date-night"],
                semantic_tags=["omakase", "date-night"],
                category="Sushi restaurant",
            ),
            ["omakase", "date-night", "sushi-restaurant", "tokyo", "restaurant", "food", "ginza"],
        )

    def test_normalize_price_text_to_currency_preserves_symbolic_tier(self) -> None:
        self.assertEqual(
            build_data.normalize_price_text_to_currency("$$$", target_currency="JPY"),
            "¥¥¥",
        )
        self.assertEqual(
            build_data.normalize_price_text_to_currency("$$$", target_currency="TWD"),
            "NT$$$",
        )

    def test_normalize_price_text_to_currency_converts_numeric_ranges(self) -> None:
        with patch.object(
            build_data,
            "load_usd_exchange_rates",
            return_value={"USD": 1.0, "TWD": 30.0, "JPY": 150.0},
        ):
            self.assertEqual(
                build_data.normalize_price_text_to_currency("NT$1–200", target_currency="TWD"),
                "NT$1–200",
            )
            self.assertEqual(
                build_data.normalize_price_text_to_currency("NT$200–400", target_currency="JPY"),
                "¥1,000–2,000",
            )

    def test_display_price_range_for_place_uses_configured_source_order_and_guide_currency(self) -> None:
        enrichment = EnrichmentPlace(
            price_range=None,
            admission_price="NT$100",
            room_price="NT$5,293",
        )

        with (
            patch.object(
                build_data,
                "google_maps_place_price_display_config",
                return_value={
                    "currency_mode": "guide_local",
                    "source_order": ["price_range", "admission_price", "room_price"],
                },
            ),
            patch.object(
                build_data,
                "load_usd_exchange_rates",
                return_value={"USD": 1.0, "TWD": 30.0, "JPY": 150.0},
            ),
        ):
            self.assertEqual(
                build_data.display_price_range_for_place(enrichment, country_name="Japan"),
                "¥500",
            )

    def test_display_price_range_for_place_skips_admission_above_configured_cap(self) -> None:
        enrichment = EnrichmentPlace(
            price_range=None,
            admission_price="NT$3,007",
            room_price=None,
        )

        with (
            patch.object(
                build_data,
                "google_maps_place_price_display_config",
                return_value={
                    "currency_mode": "guide_local",
                    "source_order": ["admission_price"],
                    "max_numeric_by_source": {"admission_price": {"JPY": 5000}},
                },
            ),
            patch.object(
                build_data,
                "load_usd_exchange_rates",
                return_value={"USD": 1.0, "TWD": 30.0, "JPY": 150.0},
            ),
        ):
            self.assertIsNone(
                build_data.display_price_range_for_place(enrichment, country_name="Japan")
            )

    def test_display_price_range_for_place_skips_numeric_attraction_price_range(self) -> None:
        enrichment = EnrichmentPlace(
            price_range="NT$4,909",
            admission_price=None,
            primary_type_display_name="Observation deck",
            types=["tourist_attraction"],
        )

        with patch.object(
            build_data,
            "google_maps_place_price_display_config",
            return_value={"currency_mode": "guide_local"},
        ):
            self.assertIsNone(
                build_data.display_price_range_for_place(enrichment, country_name="Japan")
            )

    def test_display_price_range_for_place_skips_symbolic_unknown_price_range(self) -> None:
        enrichment = EnrichmentPlace(
            price_range="$",
            admission_price=None,
            primary_type_display_name=None,
            types=[],
        )

        with patch.object(
            build_data,
            "google_maps_place_price_display_config",
            return_value={"currency_mode": "guide_local"},
        ):
            self.assertIsNone(
                build_data.display_price_range_for_place(enrichment, country_name="Japan")
            )

    def test_display_price_range_for_place_keeps_numeric_restaurant_price_range(self) -> None:
        enrichment = EnrichmentPlace(
            price_range="NT$200–400",
            primary_type_display_name="Restaurant",
            types=["restaurant"],
        )

        with (
            patch.object(
                build_data,
                "google_maps_place_price_display_config",
                return_value={"currency_mode": "guide_local"},
            ),
            patch.object(
                build_data,
                "load_usd_exchange_rates",
                return_value={"USD": 1.0, "TWD": 30.0, "JPY": 150.0},
            ),
        ):
            self.assertEqual(
                build_data.display_price_range_for_place(enrichment, country_name="Japan"),
                "¥1,000–2,000",
            )

    def test_apply_semantic_enrichment_uses_optional_llm_response(self) -> None:
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            price_range="NT$200-400",
            review_topics=[{"label": "oolong", "count": 12}],
            about_sections=[{"title": "Service options", "items": [{"label": "Dine-in"}]}],
        )
        raw_place = RawPlace(name="Tea House", maps_url="https://maps.example/tea")

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=False),
            patch.object(
                build_data,
                "repair_semantic_enrichment_with_llm",
                return_value={
                    "neighborhood": "Xinyi District",
                    "tags": ["tea-house", "Taipei Tea"],
                    "vibe_tags": ["quiet", "date night"],
                    "types": ["specialty-cafe"],
                },
            ) as repair,
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            )

        repair.assert_called_once()
        self.assertIs(repair.call_args.kwargs["include_semantics"], True)
        self.assertIs(repair.call_args.kwargs["include_description"], False)
        self.assertIs(repair.call_args.kwargs["bypass_cache"], False)
        self.assertEqual(enrichment.semantic_neighborhood, "Xinyi")
        self.assertEqual(enrichment.semantic_tags, ["tea-house", "taipei-tea"])
        self.assertEqual(enrichment.semantic_vibe_tags, ["quiet", "date-night"])
        self.assertEqual(enrichment.semantic_types, ["specialty-cafe"])
        self.assertEqual(enrichment.semantic_source, "llm")

    def test_apply_semantic_enrichment_drops_conflicting_llm_description_location(self) -> None:
        raw_place = RawPlace(
            name="Casa de Norte",
            address="Japan, 〒040-0065 Hokkaido, Hakodate, Toyokawacho, 12-6",
            maps_url="https://maps.example/casa-de-norte",
        )
        enrichment = EnrichmentPlace(
            display_name="Casa de Norte",
            formatted_address="Japan, 〒040-0065 Hokkaido, Hakodate, Toyokawacho, 12-6",
            primary_type_display_name="Western restaurant",
            plus_code="QP99+4H Hakodate, Hokkaido, Japan",
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=True),
            patch.object(
                build_data,
                "repair_semantic_enrichment_with_llm",
                return_value={
                    "description": "Open-24-hours Japanese restaurant in Nishi-Shinjuku's business district with a relaxed atmosphere."
                },
            ),
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Hakodate",
                country_name="Japan",
            )

        self.assertIsNone(enrichment.semantic_description)
        self.assertIsNone(enrichment.semantic_description_signature)
        self.assertIsNone(enrichment.semantic_source)

    def test_semantic_description_location_conflict_allows_named_venue_references(self) -> None:
        raw_place = RawPlace(
            name="Hugo",
            address="Budapest, Hungary",
            maps_url="https://maps.example/hugo",
        )
        enrichment = EnrichmentPlace(
            display_name="Hugo",
            formatted_address="Budapest, Hungary",
            primary_type_display_name="Espresso bar",
        )

        self.assertFalse(
            build_data.semantic_description_has_conflicting_location(
                "Small espresso bar near the New York Café famous for traditional Hungarian strudels.",
                enrichment_place=enrichment,
                raw_place=raw_place,
                city_name="Budapest",
                country_name="Hungary",
            )
        )
        self.assertTrue(
            build_data.semantic_description_has_conflicting_location(
                "Major international airport serving Tianjin and the Beijing-Tianjin-Hebei region.",
                enrichment_place=enrichment,
                raw_place=raw_place,
                city_name="Takamatsu",
                country_name="Japan",
            )
        )
        self.assertFalse(
            build_data.semantic_description_has_conflicting_location(
                "Compact ramen counter serving Tokyo-style ramen with house noodles.",
                enrichment_place=enrichment,
                raw_place=raw_place,
                city_name="New York",
                country_name="United States",
            )
        )
        self.assertTrue(
            build_data.semantic_description_has_conflicting_location(
                "Paris Baguette is a bakery in Paris with coffee and pastries.",
                enrichment_place=EnrichmentPlace(
                    display_name="Paris Baguette",
                    formatted_address="Seoul, South Korea",
                    primary_type_display_name="Bakery",
                ),
                raw_place=RawPlace(
                    name="Paris Baguette",
                    address="Seoul, South Korea",
                    maps_url="https://maps.example/paris-baguette",
                ),
                city_name="Seoul",
                country_name="South Korea",
            )
        )

    def test_repair_semantic_enrichment_logs_langfuse_generation(self) -> None:
        class FakeResponse:
            def read(self) -> bytes:
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "neighborhood": "Xinyi District",
                                            "tags": ["tea-house"],
                                            "description": "A quiet tea stop.",
                                        }
                                    )
                                }
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 7,
                            "total_tokens": 18,
                        },
                    }
                ).encode("utf-8")

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        class FakeObservation:
            def __init__(self) -> None:
                self.updates: list[dict[str, object]] = []

            def update(self, **kwargs: object) -> None:
                self.updates.append(kwargs)

        class FakeManager:
            def __init__(self, observation: FakeObservation) -> None:
                self.observation = observation
                self.closed = False

            def __enter__(self) -> FakeObservation:
                return self.observation

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                self.closed = True
                return False

        class FakeClient:
            def __init__(self) -> None:
                self.observation = FakeObservation()
                self.manager = FakeManager(self.observation)
                self.started: dict[str, object] | None = None

            def start_as_current_observation(self, **kwargs: object) -> FakeManager:
                self.started = kwargs
                return self.manager

        fake_client = FakeClient()
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
        )
        raw_place = RawPlace(name="Tea House", maps_url="https://maps.example/tea")

        with TemporaryDirectory() as tmp_dir:
            with (
                patch.object(
                    build_data,
                    "semantic_llm_config",
                    return_value={
                        "model": "gpt-test",
                        "api_key": "test-key",
                        "base_url": "https://llm.example/v1",
                        "namespace": "test-namespace",
                    },
                ),
                patch.object(
                    build_data,
                    "google_maps_place_semantic_cache_dir",
                    return_value=Path(tmp_dir),
                ),
                patch.object(build_data, "configured_langfuse_client", return_value=fake_client),
                patch.object(build_data, "urlopen", return_value=FakeResponse()),
            ):
                result = build_data.repair_semantic_enrichment_with_llm(
                    enrichment,
                    raw_place=raw_place,
                    city_name="Taipei",
                    country_name="Taiwan",
                    include_semantics=True,
                    include_description=True,
                    bypass_cache=True,
                )

        self.assertEqual(
            result,
            {
                "neighborhood": "Xinyi District",
                "tags": ["tea-house"],
                "description": "A quiet tea stop.",
            },
        )
        self.assertIsNotNone(fake_client.started)
        assert fake_client.started is not None
        self.assertEqual(fake_client.started["as_type"], "generation")
        self.assertEqual(fake_client.started["name"], "favorite-places.semantic-enrichment")
        self.assertEqual(fake_client.started["model"], "gpt-test")
        self.assertEqual(fake_client.started["metadata"]["prompt_version"], "favorite-places-semantic-v11")
        self.assertTrue(fake_client.manager.closed)
        self.assertEqual(
            fake_client.observation.updates[-1]["usage_details"],
            {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        )
        self.assertEqual(
            fake_client.observation.updates[-1]["metadata"],
            {**fake_client.started["metadata"], "status": "success"},
        )

    def test_langfuse_client_does_not_cache_disabled_env(self) -> None:
        class FakeLangfuse:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        fake_module = ModuleType("langfuse")
        fake_module.Langfuse = FakeLangfuse  # type: ignore[attr-defined]
        build_data.clear_langfuse_client_cache()
        self.addCleanup(build_data.clear_langfuse_client_cache)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(build_data, "load_dotenv_values", return_value={}),
        ):
            self.assertIsNone(build_data.configured_langfuse_client())

        with (
            patch.dict(
                os.environ,
                {
                    "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
                    "LANGFUSE_SECRET_KEY": "sk-lf-test",
                    "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com",
                },
                clear=True,
            ),
            patch.dict(sys.modules, {"langfuse": fake_module}),
            patch.object(build_data, "load_dotenv_values", return_value={}),
        ):
            client = build_data.configured_langfuse_client()

        self.assertIsInstance(client, FakeLangfuse)
        self.assertEqual(
            client.kwargs,
            {
                "public_key": "pk-lf-test",
                "secret_key": "sk-lf-test",
                "base_url": "https://us.cloud.langfuse.com",
                "timeout": 2,
                "flush_at": 8,
                "flush_interval": 1.0,
            },
        )

    def test_langfuse_client_uses_export_options_from_env(self) -> None:
        class FakeLangfuse:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        fake_module = ModuleType("langfuse")
        fake_module.Langfuse = FakeLangfuse  # type: ignore[attr-defined]
        build_data.clear_langfuse_client_cache()
        self.addCleanup(build_data.clear_langfuse_client_cache)

        with (
            patch.dict(
                os.environ,
                {
                    "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
                    "LANGFUSE_SECRET_KEY": "sk-lf-test",
                    "LANGFUSE_TIMEOUT": "4",
                    "LANGFUSE_FLUSH_AT": "3",
                    "LANGFUSE_FLUSH_INTERVAL": "0.5",
                },
                clear=True,
            ),
            patch.dict(sys.modules, {"langfuse": fake_module}),
            patch.object(build_data, "load_dotenv_values", return_value={}),
        ):
            client = build_data.configured_langfuse_client()

        self.assertIsInstance(client, FakeLangfuse)
        self.assertEqual(client.kwargs["timeout"], 4)
        self.assertEqual(client.kwargs["flush_at"], 3)
        self.assertEqual(client.kwargs["flush_interval"], 0.5)

    def test_normalize_semantic_neighborhood_display_cases_slug_outputs(self) -> None:
        cases = {
            "perth-cbd": "Perth CBD",
            "perth_cbd": "Perth CBD",
            "margaret-river": "Margaret River",
            "northbridge": "Northbridge",
            "PERTH CBD": "Perth CBD",
            "CBD": "CBD",
            "da-an": "Da'an",
            "st kilda": "St Kilda",
            "St. Pauli": "St. Pauli",
            "st johns wood": "St Johns Wood",
            "District 1": "District 1",
            "1st arrondissement": "1st Arrondissement",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    build_data.normalize_semantic_neighborhood_label(
                        value,
                        city_name="Perth",
                        country_name="Australia",
                    ),
                    expected,
                )

    def test_normalize_semantic_neighborhood_preserves_site_mapping_sources(self) -> None:
        rules = [
            {
                "city": "Madrid",
                "country": "Spain",
                "from": "P.º del Prado",
                "to": "Paseo del Prado",
            }
        ]

        with patch.object(build_data, "google_maps_place_neighborhood_mappings", return_value=rules):
            self.assertEqual(
                build_data.normalize_semantic_neighborhood_label(
                    "P.º del Prado",
                    city_name="Madrid",
                    country_name="Spain",
                ),
                "P.º del Prado",
            )

    def test_normalize_semantic_neighborhood_rejects_country_city_and_street_like_values(self) -> None:
        cases = [
            ("モナコ", "Monaco", "Monaco"),
            ("フランス", "Nice", "France"),
            ("Commercial Ct", "Belfast", "Northern Ireland"),
            ("Rte de la Piscine", "Monaco", "Monaco"),
            ("County Antrim", "Belfast", "Northern Ireland"),
            ("TX", "Austin", "United States"),
            ("Xinjiang", "Urumqi", "China"),
            ("新疆维吾尔自治区", "Urumqi", "China"),
            ("Sichuan", "Chengdu", "China"),
            ("四川省", "Chengdu", "China"),
            ("Donostia", "Bilbao and San Sebastian", "Spain"),
            ("Donostia / San Sebastián", "Bilbao and San Sebastian", "Spain"),
        ]
        for value, city_name, country_name in cases:
            with self.subTest(value=value):
                self.assertIsNone(
                    build_data.normalize_semantic_neighborhood_label(
                        value,
                        city_name=city_name,
                        country_name=country_name,
                    )
                )

    def test_refine_semantic_neighborhood_prefers_address_candidate_casing(self) -> None:
        self.assertEqual(
            build_data.refine_semantic_neighborhood_with_address_localities(
                "perth-cbd",
                ["Perth CBD"],
            ),
            "Perth CBD",
        )
        self.assertEqual(
            build_data.refine_semantic_neighborhood_with_address_localities(
                "phố cổ hà nội",
                ["Phố cổ Hà Nội"],
            ),
            "Phố cổ Hà Nội",
        )

    def test_apply_semantic_enrichment_display_cases_llm_neighborhood_slug(self) -> None:
        enrichment = EnrichmentPlace(
            display_name="Wine Bar",
            formatted_address="Perth CBD, Perth WA, Australia",
            primary_type_display_name="Wine bar",
        )
        raw_place = RawPlace(name="Wine Bar", maps_url="https://maps.example/wine")

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=False),
            patch.object(
                build_data,
                "repair_semantic_enrichment_with_llm",
                return_value={
                    "neighborhood": "perth-cbd",
                    "tags": ["wine-bar"],
                    "vibe_tags": ["date-night"],
                    "types": ["bar"],
                },
            ),
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Perth",
                country_name="Australia",
            )

        self.assertEqual(enrichment.semantic_neighborhood, "Perth CBD")

    def test_normalize_semantic_tag_list_drops_noise_tags(self) -> None:
        self.assertEqual(
            build_data.normalize_semantic_tag_list(
                ["scallion pancake", "q", "蔥油餅", "street food"],
                limit=8,
            ),
            ["scallion-pancake", "street-food"],
        )

    def test_apply_semantic_enrichment_reuses_description_when_signature_matches(self) -> None:
        raw_place = RawPlace(
            name="Tea House",
            note="Order the house oolong and stay for a quiet reset.",
            maps_url="https://maps.example/tea",
        )
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            review_topics=[{"label": "oolong", "count": 12}],
            about_sections=[{"title": "Service options", "items": [{"label": "Dine-in"}]}],
        )
        signature = build_data.semantic_description_signature(
            enrichment,
            raw_place=raw_place,
            city_name="Taipei",
            country_name="Taiwan",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Tea House, Taipei, Taiwan",
            matched=True,
            place=EnrichmentPlace(
                semantic_description="A calm tea stop with oolong-focused review signals.",
                semantic_description_signature=signature,
                semantic_source="llm",
            ),
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=False),
            patch.object(build_data, "repair_semantic_enrichment_with_llm") as repair,
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
                existing_entry=existing_entry,
            )

        repair.assert_not_called()
        self.assertEqual(
            enrichment.semantic_description,
            "A calm tea stop with oolong-focused review signals.",
        )
        self.assertEqual(enrichment.semantic_description_signature, signature)

    def test_apply_semantic_enrichment_does_not_reuse_low_information_matching_signature(self) -> None:
        raw_place = RawPlace(
            name="Tea House",
            maps_url="https://maps.example/tea",
        )
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
        )
        signature = build_data.semantic_description_signature(
            enrichment,
            raw_place=raw_place,
            city_name="Taipei",
            country_name="Taiwan",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Tea House, Taipei, Taiwan",
            matched=True,
            place=EnrichmentPlace(
                semantic_description="Tea House is a tea house in Taipei.",
                semantic_description_signature=signature,
                semantic_source="llm",
            ),
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=False),
            patch.object(build_data, "repair_semantic_enrichment_with_llm", return_value=None) as repair,
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
                existing_entry=existing_entry,
            )

        repair.assert_called_once()
        self.assertIsNone(enrichment.semantic_description)
        self.assertIsNone(enrichment.semantic_description_signature)
        self.assertIsNone(enrichment.semantic_source)

    def test_apply_semantic_enrichment_clears_stale_description_on_signature_mismatch(self) -> None:
        raw_place = RawPlace(
            name="Tea House",
            note="Order the house oolong and stay for a quiet reset.",
            maps_url="https://maps.example/tea",
        )
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            review_topics=[{"label": "oolong", "count": 12}],
            semantic_description="A stale generated description.",
            semantic_description_signature="old-signature",
            semantic_source="llm",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Tea House, Taipei, Taiwan",
            matched=True,
            place=enrichment,
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=False),
            patch.object(build_data, "repair_semantic_enrichment_with_llm", return_value=None),
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
                existing_entry=existing_entry,
            )

        self.assertIsNone(enrichment.semantic_description)
        self.assertIsNone(enrichment.semantic_description_signature)
        self.assertIsNone(enrichment.semantic_source)

    def test_apply_semantic_enrichment_omits_description_when_llm_description_missing(self) -> None:
        raw_place = RawPlace(
            name="Rua Augusta Arch",
            address="R. Augusta 2, 1100-053 Lisboa, Portugal",
            maps_url="https://maps.example/arch",
        )
        enrichment = EnrichmentPlace(
            display_name="Rua Augusta Arch",
            formatted_address="R. Augusta 2, 1100-053 Lisboa, Portugal",
            primary_type_display_name="Historical landmark",
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=True),
            patch.object(build_data, "repair_semantic_enrichment_with_llm", return_value={"description": None}),
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Lisbon",
                country_name="Portugal",
            )

        self.assertIsNone(enrichment.semantic_description)
        self.assertIsNone(enrichment.semantic_description_signature)
        self.assertIsNone(enrichment.semantic_source)

    def test_apply_semantic_enrichment_rejects_low_information_llm_description(self) -> None:
        raw_place = RawPlace(
            name="'Olu 'Olu Poké Sherbrooke West",
            address="250 Sherbrooke St W #101, Montreal, Quebec H2X 1X9, Canada",
            maps_url="https://maps.example/poke",
        )
        enrichment = EnrichmentPlace(
            display_name="'Olu 'Olu Poké Sherbrooke West",
            formatted_address="250 Sherbrooke St W #101, Montreal, Quebec H2X 1X9, Canada",
            primary_type="poke_bar",
            primary_type_display_name="Poke bar",
            types=["poke_bar", "restaurant"],
            review_topics=[{"label": "limu poke", "count": 2}],
            about_sections=[{"title": "Service options", "items": [{"label": "Dine-in"}]}],
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=True),
            patch.object(
                build_data,
                "repair_semantic_enrichment_with_llm",
                return_value={"description": "'Olu 'Olu Poké Sherbrooke West is a poke bar in Montreal."},
            ),
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Montreal",
                country_name="Canada",
            )

        self.assertIsNone(enrichment.semantic_description)
        self.assertIsNone(enrichment.semantic_description_signature)
        self.assertIsNone(enrichment.semantic_source)

    def test_semantic_description_accepts_distinctive_review_topic_context(self) -> None:
        raw_place = RawPlace(
            name="Pizzeria Zac",
            address="8 Av. Duluth E, Montréal, QC H2W 1G6, Canada",
            maps_url="https://maps.example/zac",
        )
        enrichment = EnrichmentPlace(
            display_name="Pizzeria Zac | Plant-Based Pizza & Eats",
            formatted_address="8 Av. Duluth E, Montréal, QC H2W 1G6, Canada",
            primary_type="pizza_restaurant",
            primary_type_display_name="Pizza restaurant",
            types=["pizza_restaurant", "restaurant"],
            review_topics=[{"label": "vegan pizza", "count": 136}],
        )

        self.assertFalse(
            build_data.semantic_description_is_low_information(
                "Plant-based pizzeria known for vegan pizza and casual counter-service meals.",
                enrichment_place=enrichment,
                raw_place=raw_place,
                city_name="Montreal",
                country_name="Canada",
            )
        )

    def test_semantic_description_accepts_longer_single_sentence(self) -> None:
        description = (
            "Plant-based pizzeria known for vegan pizza, dairy-free cheese, and casual counter-service meals "
            "near Duluth, useful when the guide needs a relaxed vegan option."
        )

        self.assertEqual(build_data.sanitize_semantic_description(description), description)

    def test_semantic_description_accepts_valid_english_words_that_overlap_french_leak_terms(
        self,
    ) -> None:
        for description in (
            "A convivial neighborhood wine bar with a compact list and relaxed counter seating.",
            "Contemporary bistro menus propose a focused set of seasonal plates for sharing.",
        ):
            with self.subTest(description=description):
                self.assertEqual(build_data.sanitize_semantic_description(description), description)

    def test_semantic_description_accepts_common_phrases_that_overlap_chat_leaks(self) -> None:
        for description in (
            "A top notch omakase counter with seasonal nigiri and polished service.",
            "Kinda hidden cocktail bar with a compact menu and relaxed late-night energy.",
            "Great spot for a banh mi and don’t miss the whelk papaya salad.",
        ):
            with self.subTest(description=description):
                self.assertEqual(build_data.sanitize_semantic_description(description), description)

    def test_semantic_description_accepts_common_recommendation_openers(self) -> None:
        for description in (
            "Great spot for handmade soba with a quiet counter and seasonal sides.",
            "Amazing spot for seasonal gelato and espresso near the old market.",
            "Nice spot for small plates, natural wine, and a low-key patio dinner.",
        ):
            with self.subTest(description=description):
                self.assertEqual(build_data.sanitize_semantic_description(description), description)

    def test_semantic_description_accepts_slash_abbreviations(self) -> None:
        description = "Counter-service breakfast/lunch cafe with vegetarian/vegan bowls and house coffee."

        self.assertEqual(build_data.sanitize_semantic_description(description), description)

    def test_semantic_description_rejects_paragraph_length_text(self) -> None:
        self.assertIsNone(build_data.sanitize_semantic_description("x" * 321))

    def test_semantic_description_rejects_review_source_leaks(self) -> None:
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "Budget mineral hot springs with free lockers, though mixed reviews note lukewarm water."
            )
        )
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "Beloved trattoria praised for warm service and handmade pasta."
            )
        )
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "A hidden speakeasy bar with an intimate atmosphere and highly praised cocktails."
            )
        )
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "Historic villa widely hailed as one of the world's most beautiful views."
            )
        )
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "A medieval church considered one of the most photographed landmarks on the coast."
            )
        )
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "Gelato shop with wheelchair-accessible seating and LGBTQ+-friendly service."
            )
        )
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "Terrace with the coast's most photographed garden view."
            )
        )
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "Quiet spa hotel by the marina; just expect spa access to be a separate charge."
            )
        )
        self.assertIsNone(
            build_data.sanitize_semantic_description(
                "A moody cocktail bar with a romantic atmosphere, though food options are minimal."
            )
        )

    def test_semantic_description_rejects_chat_history_leaky_examples(self) -> None:
        for description in (
            (
                "$180 lunch omakase; food is top notch, though it doesn’t feel that "
                "elevated at this price point (dishware feels kinda cheap and "
                "servers are a bit clueless about alcohol options)"
            ),
            (
                "Annette partage / Annette est festive / prendre un verre dans un "
                "esprit convivial et chaleureux/ Annette vous propose des petits "
                "plats à partager, des cocktails raffinés et une sélection de vins "
                "méticuleusement choisis / Annette reçoit dans un décor tendance et feutré"
            ),
        ):
            with self.subTest(description=description):
                self.assertIsNone(build_data.sanitize_semantic_description(description))

    def test_semantic_description_rejects_curation_meta_copy(self) -> None:
        for description in (
            "A favorite-worthy dinner stop, so it should surface without extra manual curation.",
            "Feels more like a personal recommendation than a tourist checklist stop.",
            "Saved-list metadata for a top pick in the guide.",
        ):
            with self.subTest(description=description):
                self.assertIsNone(build_data.sanitize_semantic_description(description))

    def test_semantic_description_rejects_attribute_boilerplate(self) -> None:
        for description in (
            "Upscale restaurant in De Pijp with full wheelchair accessibility and a refined dining experience.",
            "A scenic waterfall with suspension bridges and observation decks, featuring free admission and accessible facilities.",
            "Women-owned taquería offering fast service, great cocktails, and accessible amenities.",
            "High-end Hawaiian fusion dishes served in a contemporary, accessible setting.",
            "This accessible izakaya offers counter and outdoor seating with accommodating staff.",
            "A cozy spot drawing solo diners and accessibility-conscious visitors alike.",
            "Museum showcasing vintage cars with a multilingual audio guide and family-friendly accessibility.",
            "Hand-made dumplings served in a cozy setting with notable accessibility.",
        ):
            with self.subTest(description=description):
                self.assertIsNone(build_data.sanitize_semantic_description(description))

    def test_apply_semantic_enrichment_preserves_semantic_source_when_semantics_remain_populated(self) -> None:
        raw_place = RawPlace(
            name="Tea House",
            note="Order the house oolong and stay for a quiet reset.",
            maps_url="https://maps.example/tea",
        )
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            review_topics=[{"label": "oolong", "count": 12}],
            semantic_neighborhood="Xinyi",
            semantic_tags=["tea-house"],
            semantic_description="A stale generated description.",
            semantic_description_signature="old-signature",
            semantic_source="llm",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Tea House, Taipei, Taiwan",
            matched=True,
            place=enrichment,
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=False),
            patch.object(build_data, "repair_semantic_enrichment_with_llm", return_value=None),
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
                existing_entry=existing_entry,
            )

        self.assertEqual(enrichment.semantic_neighborhood, "Xinyi")
        self.assertEqual(enrichment.semantic_tags, ["tea-house"])
        self.assertIsNone(enrichment.semantic_description)
        self.assertIsNone(enrichment.semantic_description_signature)
        self.assertEqual(enrichment.semantic_source, "llm")

    def test_apply_semantic_enrichment_force_refreshes_description(self) -> None:
        raw_place = RawPlace(
            name="Tea House",
            note="Order the house oolong and stay for a quiet reset.",
            maps_url="https://maps.example/tea",
        )
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            review_topics=[{"label": "oolong", "count": 12}],
        )
        signature = build_data.semantic_description_signature(
            enrichment,
            raw_place=raw_place,
            city_name="Taipei",
            country_name="Taiwan",
        )
        existing_entry = EnrichmentCacheEntry(
            fetched_at="2026-05-01T00:00:00+00:00",
            source="google_maps_page",
            query="Tea House, Taipei, Taiwan",
            matched=True,
            place=EnrichmentPlace(
                semantic_description="A previous generated description.",
                semantic_description_signature=signature,
                semantic_source="llm",
            ),
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "google_maps_place_semantic_description_force_refresh", return_value=True),
            patch.object(
                build_data,
                "repair_semantic_enrichment_with_llm",
                return_value={"description": "A fresh generated description."},
            ) as repair,
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
                existing_entry=existing_entry,
            )

        repair.assert_called_once()
        self.assertIs(repair.call_args.kwargs["include_semantics"], False)
        self.assertIs(repair.call_args.kwargs["include_description"], True)
        self.assertIs(repair.call_args.kwargs["bypass_cache"], True)
        self.assertEqual(enrichment.semantic_description, "A fresh generated description.")
        self.assertEqual(enrichment.semantic_description_signature, signature)
        self.assertEqual(enrichment.semantic_source, "llm")

    def test_apply_semantic_enrichment_suppresses_description_for_manual_override(self) -> None:
        raw_place = RawPlace(
            name="Tea House",
            note="Order the house oolong and stay for a quiet reset.",
            maps_url="https://maps.example/tea",
        )
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            semantic_description="A stale generated description.",
            semantic_description_signature="old-signature",
            semantic_source="llm",
        )

        with (
            patch.object(build_data, "google_maps_place_semantic_llm_enabled", return_value=False),
            patch.object(build_data, "google_maps_place_semantic_descriptions_enabled", return_value=True),
            patch.object(build_data, "repair_semantic_enrichment_with_llm") as repair,
        ):
            build_data.apply_semantic_enrichment(
                enrichment,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
                suppress_description=True,
            )

        repair.assert_not_called()
        self.assertIsNone(enrichment.semantic_description)
        self.assertIsNone(enrichment.semantic_description_signature)
        self.assertIsNone(enrichment.semantic_source)

    def test_semantic_description_signature_ignores_review_snippet_churn(self) -> None:
        raw_place = RawPlace(name="Tea House", maps_url="https://maps.example/tea")
        first = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            review_topics=[{"label": "oolong", "count": 12}],
            reviews=[{"text": "Volatile review text A"}],
        )
        second = first.model_copy(deep=True)
        second.reviews = [{"text": "Volatile review text B"}]

        self.assertEqual(
            build_data.semantic_description_signature(
                first,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
            build_data.semantic_description_signature(
                second,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
        )
        without_topics = first.model_copy(deep=True)
        without_topics.review_topics = []
        self.assertNotEqual(
            build_data.semantic_description_signature(
                first,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
            build_data.semantic_description_signature(
                without_topics,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
        )
        second.about_sections = [{"title": "Service options", "items": [{"label": "Dine-in"}]}]
        self.assertNotEqual(
            build_data.semantic_description_signature(
                first,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
            build_data.semantic_description_signature(
                second,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
        )

    def test_semantic_description_signature_changes_when_raw_note_changes(self) -> None:
        first = RawPlace(
            name="Tea House",
            note="Order the house oolong and stay for a quiet reset.",
            maps_url="https://maps.example/tea",
        )
        second = RawPlace(
            name="Tea House",
            note="Good for a quick tea break before shopping.",
            maps_url="https://maps.example/tea",
        )
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            review_topics=[{"label": "oolong", "count": 12}],
        )

        self.assertNotEqual(
            build_data.semantic_description_signature(
                enrichment,
                raw_place=first,
                city_name="Taipei",
                country_name="Taiwan",
            ),
            build_data.semantic_description_signature(
                enrichment,
                raw_place=second,
                city_name="Taipei",
                country_name="Taiwan",
            ),
        )

    def test_semantic_description_signature_changes_when_google_description_changes(self) -> None:
        raw_place = RawPlace(name="Tea House", maps_url="https://maps.example/tea")
        first = EnrichmentPlace(
            display_name="Tea House",
            description="Polished tea room with quiet seating and house oolong.",
            review_topics=[{"label": "oolong", "count": 12}],
        )
        second = first.model_copy(deep=True)
        second.description = "Compact tea counter known for fast tastings."

        self.assertNotEqual(
            build_data.semantic_description_signature(
                first,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
            build_data.semantic_description_signature(
                second,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
        )

    def test_semantic_description_signature_changes_when_search_result_description_changes(self) -> None:
        raw_place = RawPlace(name="Tea House", maps_url="https://maps.example/tea")
        first = EnrichmentPlace(
            display_name="Tea House",
            search_result_description="Quiet tea room near the station.",
            review_topics=[{"label": "oolong", "count": 12}],
        )
        second = first.model_copy(deep=True)
        second.search_result_description = "Fast tea counter near the station."

        self.assertNotEqual(
            build_data.semantic_description_signature(
                first,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
            build_data.semantic_description_signature(
                second,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
        )

    def test_semantic_enrichment_evidence_includes_raw_note_only_for_descriptions(self) -> None:
        raw_place = RawPlace(
            name="Tea House",
            note="Order the house oolong and stay for a quiet reset.",
            maps_url="https://maps.example/tea",
        )
        enrichment = EnrichmentPlace(
            display_name="Tea House",
            description="Polished tea room with quiet seating and house oolong.",
            search_result_description="Quiet tea room near the station.",
        )

        description_evidence = build_data.semantic_enrichment_evidence(
            enrichment,
            raw_place=raw_place,
            city_name="Taipei",
            country_name="Taiwan",
            include_raw_note=True,
        )
        semantic_only_evidence = build_data.semantic_enrichment_evidence(
            enrichment,
            raw_place=raw_place,
            city_name="Taipei",
            country_name="Taiwan",
            include_raw_note=False,
        )

        self.assertEqual(
            description_evidence["raw_note"],
            "Order the house oolong and stay for a quiet reset.",
        )
        self.assertEqual(
            description_evidence["google_maps_description"],
            "Polished tea room with quiet seating and house oolong.",
        )
        self.assertEqual(
            semantic_only_evidence["google_maps_description"],
            "Polished tea room with quiet seating and house oolong.",
        )
        self.assertEqual(
            description_evidence["search_result_description"],
            "Quiet tea room near the station.",
        )
        self.assertEqual(
            semantic_only_evidence["search_result_description"],
            "Quiet tea room near the station.",
        )
        self.assertNotIn("raw_note", semantic_only_evidence)

    def test_semantic_description_signature_normalizes_minor_text_churn(self) -> None:
        raw_place = RawPlace(name="Tea House", maps_url="https://maps.example/tea")
        first = EnrichmentPlace(
            display_name="Tea House",
            formatted_address="No. 12, Songgao Rd, Taipei City",
            primary_type_display_name="Tea house",
            price_range="NT$200-400",
            review_topics=[{"label": "Oolong Tea", "count": 12}],
            about_sections=[{"title": "Service options", "items": [{"label": "Dine-in"}]}],
        )
        second = EnrichmentPlace(
            display_name="tea  house",
            formatted_address="No 12 Songgao Rd Taipei City",
            primary_type_display_name="tea-house",
            price_range="NT 200 400",
            review_topics=[{"label": "oolong-tea", "count": 14}],
            about_sections=[{"title": "service options", "items": [{"label": "Dine in"}]}],
        )

        self.assertEqual(
            build_data.semantic_description_signature(
                first,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
            build_data.semantic_description_signature(
                second,
                raw_place=raw_place,
                city_name="Taipei",
                country_name="Taiwan",
            ),
        )

    def test_normalize_place_page_enrichment_infers_localized_category_types(self) -> None:
        steakhouse = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Test",
                resolved_url="https://www.google.com/maps/place/Test",
                name="Test Steakhouse",
                category="和風ステーキハウス",
                rating=4.6,
                review_count=128,
                address="Kyoto",
                limited_view=False,
            )
        )
        self.assertEqual(steakhouse.primary_type, "steak_house")
        self.assertEqual(steakhouse.primary_type_display_name, "Steak house")
        self.assertEqual(steakhouse.primary_type_display_name_localized, "和風ステーキハウス")
        self.assertEqual(steakhouse.types, ["restaurant", "western_restaurant", "steak_house"])

        museum = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Test",
                resolved_url="https://www.google.com/maps/place/Test",
                name="Museum",
                category="博物館",
                rating=4.8,
                review_count=240,
                address="Taoyuan",
                limited_view=False,
            )
        )
        self.assertEqual(museum.primary_type, "museum")
        self.assertEqual(museum.primary_type_display_name, "Museum")
        self.assertEqual(museum.primary_type_display_name_localized, "博物館")
        self.assertEqual(museum.types, ["museum"])

    def test_normalize_place_page_enrichment_drops_suspicious_category_labels(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Test",
                resolved_url="https://www.google.com/maps/place/Test",
                name="Test Place",
                category="Floor 1",
                rating=4.2,
                review_count=17,
                address="Osaka",
                limited_view=False,
            )
        )

        self.assertIsNone(place.primary_type)
        self.assertIsNone(place.primary_type_display_name)
        self.assertIsNone(place.primary_type_display_name_localized)
        self.assertEqual(place.types, [])

    def test_normalize_place_page_enrichment_preserves_localized_only_category(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Test",
                resolved_url="https://www.google.com/maps/place/Test",
                name="Test Place",
                category="麺類専門店",
                rating=4.2,
                review_count=17,
                address="Fukuoka",
                limited_view=False,
            )
        )

        self.assertIsNone(place.primary_type)
        self.assertIsNone(place.primary_type_display_name)
        self.assertEqual(place.primary_type_display_name_localized, "麺類専門店")
        self.assertEqual(place.types, [])

    def test_normalize_enrichment_match_preserves_localized_category_with_generic_type(self) -> None:
        place = build_data.normalize_enrichment_match(
            {
                "id": "places/abc",
                "displayName": {"text": "Test Place"},
                "primaryType": "point_of_interest",
                "primaryTypeDisplayName": {"text": "もつ鍋料理店"},
                "types": ["point_of_interest", "establishment"],
            }
        )

        self.assertIsNone(place.primary_type)
        self.assertIsNone(place.primary_type_display_name)
        self.assertEqual(place.primary_type_display_name_localized, "もつ鍋料理店")
        self.assertEqual(place.types, [])

    def test_normalize_place_page_enrichment_drops_ui_action_display_names(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/search/?api=1&query=Taipei+Zoo",
                resolved_url="https://www.google.com/maps/search/?api=1&query=Taipei+Zoo",
                name="Share",
                category="Zoo",
                rating=4.6,
                review_count=76982,
                address="No. 30號, Section 2, Xinguang Rd",
                limited_view=False,
            )
        )

        self.assertIsNone(place.display_name)
        self.assertEqual(place.primary_type_display_name, "Zoo")
        self.assertEqual(place.formatted_address, "No. 30號, Section 2, Xinguang Rd")

    def test_normalize_place_page_enrichment_drops_sponsored_display_name(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/search/?api=1&query=Restaurant",
                resolved_url="https://www.google.com/maps/search/?api=1&query=Restaurant",
                name="Sponsored \ue5d4",
                category="Restaurant",
                rating=3.7,
                review_count=186,
                address=None,
                limited_view=False,
            )
        )

        self.assertIsNone(place.display_name)
        self.assertEqual(place.primary_type_display_name, "Restaurant")

    def test_normalize_guide_display_address_formats_taiwan_postal_prefix(self) -> None:
        self.assertEqual(
            build_data.normalize_guide_display_address(
                "260, Taiwan, Yilan County, Yilan City, Minquan Village, Nongquan Rd, 110號1F",
                country_name="Taiwan",
            ),
            "No. 110, 1F, Nongquan Rd, Minquan Village, Yilan City, Yilan County, Taiwan 260",
        )

    def test_normalize_guide_display_address_formats_localized_taiwan_postal_prefix(self) -> None:
        self.assertEqual(
            build_data.normalize_guide_display_address(
                "260, 台灣, Yilan County, Yilan City, Minquan Village, Nongquan Rd, 110號1F",
                country_name="Taiwan",
            ),
            "No. 110, 1F, Nongquan Rd, Minquan Village, Yilan City, Yilan County, Taiwan 260",
        )
        self.assertEqual(
            build_data.normalize_guide_display_address(
                "260, 台湾, Yilan County, Yilan City, Minquan Village, Nongquan Rd, 110號1F",
                country_name="Taiwan",
            ),
            "No. 110, 1F, Nongquan Rd, Minquan Village, Yilan City, Yilan County, Taiwan 260",
        )

    def test_normalize_guide_display_address_collapses_taiwan_duplicate_number(self) -> None:
        self.assertEqual(
            build_data.normalize_guide_display_address(
                "No. 6 No. 6號, Lane 47, Section 3, Dafu Rd, Zhuangwei Township, Yilan County, 263",
                country_name="Taiwan",
            ),
            "No. 6, Lane 47, Section 3, Dafu Rd, Zhuangwei Township, Yilan County, 263",
        )

    def test_normalize_place_page_enrichment_sanitizes_suspicious_addresses(self) -> None:
        chrome = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Dubai+Frame",
                resolved_url="https://www.google.com/maps/place/Dubai+Frame",
                name="Dubai Frame",
                category="Tourist attraction",
                rating=4.6,
                review_count=100,
                address=(
                    "Imagery ©2026 , Map data ©2026 "
                    "United StatesTermsPrivacySend Product Feedback"
                ),
                limited_view=False,
            )
        )
        token = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Jumeirah+Beach",
                resolved_url="https://www.google.com/maps/place/Jumeirah+Beach",
                name="Jumeirah Beach",
                category="Beach",
                rating=4.4,
                review_count=20,
                address="/m/0cnyfm6",
                limited_view=False,
            )
        )
        street_view = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Jumeirah+Beach",
                resolved_url="https://www.google.com/maps/place/Jumeirah+Beach",
                name="Jumeirah Beach",
                category="Beach",
                rating=4.4,
                review_count=20,
                address="Street View & 360°",
                limited_view=False,
            )
        )
        prefixed = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Global+Village",
                resolved_url="https://www.google.com/maps/place/Global+Village",
                name="Global Village",
                category="Theme park",
                rating=4.5,
                review_count=20,
                address="企業のオフィス ·  · Exit 37 - Sheikh Mohammed Bin Zayed Rd",
                limited_view=False,
            )
        )

        self.assertIsNone(chrome.formatted_address)
        self.assertIsNone(token.formatted_address)
        self.assertIsNone(street_view.formatted_address)
        self.assertEqual(prefixed.formatted_address, "Exit 37 - Sheikh Mohammed Bin Zayed Rd")

    def test_normalize_place_page_enrichment_rejects_fixaddress_url_addresses(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Nizami+Street",
                resolved_url="https://www.google.com/maps/place/Nizami+Street",
                name="Nizami Street",
                category="Transportation",
                rating=4.7,
                review_count=1825,
                address=(
                    "Address https://www.google.com/local/place/rap/fixaddress?"
                    "g2lb=72971417,73155522,100805691&hl=en-CA&gl=ca"
                ),
                limited_view=False,
            )
        )

        self.assertIsNone(place.formatted_address)

    def test_normalize_place_page_enrichment_rejects_review_prose_as_address(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Nizami+Street",
                resolved_url="https://www.google.com/maps/place/Nizami+Street",
                name="Nizami Street",
                category="Transportation",
                rating=4.7,
                review_count=1842,
                address=(
                    "It was amazing street walk experience i hade ever before. "
                    "Shops and natural bueaty is next level experience 💖"
                ),
                limited_view=False,
            )
        )

        self.assertIsNone(place.formatted_address)

    def test_normalize_place_page_enrichment_rejects_long_review_snippet_as_address(self) -> None:
        for address in (
            (
                "The best takeout or eat in I recommend this place. We dropped in 5 minutes "
                "before closing time and the owner took the initiative to cook us More"
            ),
            "The nuggets are massive, good size burgers and probably the best for value in town",
            "This place has great food, good service, friendly owner, and delicious burgers",
        ):
            with self.subTest(address=address):
                place = build_data.normalize_place_page_enrichment(
                    SimpleNamespace(
                        source_url="https://www.google.com/maps/place/Tropical+Taste",
                        resolved_url="https://www.google.com/maps/place/Tropical+Taste",
                        name="Tropical Taste",
                        category="Restaurant",
                        rating=4.6,
                        review_count=101,
                        address=address,
                        limited_view=False,
                    )
                )

                self.assertIsNone(place.formatted_address)

    def test_normalize_place_page_enrichment_keeps_addresses_with_prose_words(self) -> None:
        for address in (
            "Good Burger, 1 Main St, New York, NY 10001",
            "Session Road, Baguio, Benguet 2600, Philippines",
            "Best Avenue, Oakland, CA 94611",
            "Dinner Plain, Victoria, Australia",
            "Port of Spain, Trinidad & Tobago",
        ):
            with self.subTest(address=address):
                place = build_data.normalize_place_page_enrichment(
                    SimpleNamespace(
                        source_url="https://www.google.com/maps/place/Test",
                        resolved_url="https://www.google.com/maps/place/Test",
                        name="Test",
                        category="Restaurant",
                        rating=4.6,
                        review_count=101,
                        address=address,
                        limited_view=False,
                    )
                )

                self.assertEqual(place.formatted_address, address)

    def test_normalize_place_page_enrichment_accepts_locality_only_address(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Nizami+Street",
                resolved_url="https://www.google.com/maps/place/Nizami+Street",
                name="Nizami St",
                category="Notable street",
                rating=4.7,
                review_count=1842,
                address="Baku, Azerbaijan",
                limited_view=False,
            )
        )

        self.assertEqual(place.formatted_address, "Baku, Azerbaijan")

    def test_normalize_place_page_enrichment_rejects_service_options_as_address(self) -> None:
        for address in (
            "Dine-in, Takeout, Delivery",
            "Dine-in, Takeout, Delivery.",
            "Dine-in, Takeout, Reservations",
            "Takeout, Delivery, Curbside pickup",
            "Wheelchair accessible entrance, Dine-in, Takeout",
            "Museum, Art gallery",
            "Museum, Montenegro",
            "Hotel, Spain",
            "Friendly, XYZ",
            "good food, friendly owner",
            "Friendly staff, good coffee.",
            "Great food at St. James, highly recommend.",
        ):
            with self.subTest(address=address):
                place = build_data.normalize_place_page_enrichment(
                    SimpleNamespace(
                        source_url="https://www.google.com/maps/place/Test",
                        resolved_url="https://www.google.com/maps/place/Test",
                        name="Test",
                        category="Restaurant",
                        rating=4.6,
                        review_count=101,
                        address=address,
                        limited_view=False,
                    )
                )

                self.assertIsNone(place.formatted_address)

    def test_normalize_place_page_enrichment_keeps_locality_abbreviations(self) -> None:
        for address in (
            "St. Louis, MO",
            "St. John's, NL",
            "Washington, D.C.",
            "Bar, Montenegro",
            "Bar, Bar, Montenegro",
            "東京, 日本",
            "Port of Spain, Trinidad & Tobago",
            "Jumeirah Beach - Jumeirah - Jumeira Third - Dubai - United Arab Emirates",
        ):
            with self.subTest(address=address):
                place = build_data.normalize_place_page_enrichment(
                    SimpleNamespace(
                        source_url="https://www.google.com/maps/place/Test",
                        resolved_url="https://www.google.com/maps/place/Test",
                        name="Test",
                        category="Locality",
                        rating=4.6,
                        review_count=101,
                        address=address,
                        limited_view=False,
                    )
                )

                self.assertEqual(place.formatted_address, address)

    def test_normalize_place_page_enrichment_rejects_saved_list_row_as_address(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Nizami+Street",
                resolved_url="https://www.google.com/maps/place/Nizami+Street",
                name="Nizami St",
                category="Notable street",
                rating=4.7,
                review_count=1842,
                address="Saved in Favorites & Baku, Azerbaijan 🇦🇿",
                limited_view=False,
            )
        )

        self.assertIsNone(place.formatted_address)

    def test_normalize_place_page_enrichment_falls_back_to_compound_plus_code(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Global+Village",
                resolved_url="https://www.google.com/maps/place/Global+Village",
                name="Global Village",
                category="Theme park",
                rating=4.5,
                review_count=20,
                address="Street View & 360°",
                plus_code="38C5+F57 - Wadi Al Safa 4 - Dubai - United Arab Emirates",
                limited_view=False,
            )
        )

        self.assertEqual(
            place.formatted_address,
            "38C5+F57 - Wadi Al Safa 4 - Dubai - United Arab Emirates",
        )
        self.assertEqual(place.plus_code, "38C5+F57 - Wadi Al Safa 4 - Dubai - United Arab Emirates")

    def test_normalize_place_page_enrichment_sanitizes_phone_and_plus_code(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Nar+%26+Sharab",
                resolved_url="https://www.google.com/maps/place/Nar+%26+Sharab",
                name="Nar & Sharab",
                category="Restaurant",
                rating=4.4,
                review_count=51,
                address="Namiq Quliyev, Baku, Azerbaijan",
                phone="+994 50 241 01 01",
                plus_code="Plus code: 8R3F+XV Baku, Azerbaijan",
                limited_view=False,
            )
        )

        self.assertEqual(place.phone, "+994 50 241 01 01")
        self.assertEqual(place.plus_code, "8R3F+XV Baku, Azerbaijan")

    def test_normalize_place_page_enrichment_rejects_invalid_phone_and_plus_code(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/place/Nizami+Street",
                resolved_url="https://www.google.com/maps/place/Nizami+Street",
                name="Nizami St",
                category="Notable street",
                rating=4.7,
                review_count=1842,
                address="Baku, Azerbaijan",
                phone="1776616236980",
                plus_code="Your Maps history",
                limited_view=False,
            )
        )

        self.assertIsNone(place.phone)
        self.assertIsNone(place.plus_code)

    def test_normalize_place_page_enrichment_uses_address_like_description_when_address_missing(self) -> None:
        place = build_data.normalize_place_page_enrichment(
            SimpleNamespace(
                source_url="https://www.google.com/maps/search/?api=1&query=Jumeirah+Beach",
                resolved_url="https://www.google.com/maps/place/Jumeirah+Beach",
                name="Jumeirah Beach",
                category="Beach",
                rating=4.6,
                review_count=None,
                address=None,
                plus_code=None,
                description="Jumeirah Beach - Jumeirah - Jumeira Third - Dubai - United Arab Emirates",
                limited_view=True,
            )
        )

        self.assertEqual(
            place.formatted_address,
            "Jumeirah Beach - Jumeirah - Jumeira Third - Dubai - United Arab Emirates",
        )
        self.assertIsNone(place.description)

    def test_normalize_guide_keeps_english_primary_category_and_stores_localized_variant(self) -> None:
        raw = RawSavedList(
            title="Fukuoka",
            places=[
                RawPlace(
                    name="やまや別邸西中洲",
                    address="Fukuoka",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-23T00:00:00+00:00",
                query="やまや別邸西中洲, Fukuoka",
                matched=True,
                score=45,
                place=EnrichmentPlace(
                    primary_type="restaurant",
                    primary_type_display_name="もつ鍋料理店",
                ),
            )
        }

        guide = build_data.normalize_guide("fukuoka-japan", raw, enrichment_cache=enrichment_cache)
        place = guide.places[0]

        self.assertEqual(place.primary_category, "Restaurant")
        self.assertEqual(place.primary_category_localized, "もつ鍋料理店")
        self.assertEqual(place.provenance.primary_category.value, "Restaurant")
        self.assertEqual(place.provenance.primary_category_localized.value, "もつ鍋料理店")

    def test_normalize_enrichment_match_expands_parent_restaurant_types(self) -> None:
        place = build_data.normalize_enrichment_match(
            {
                "id": "test-id",
                "name": "places/test-id",
                "displayName": {"text": "Test Udon"},
                "formattedAddress": "Sapporo",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "primaryType": "udon_noodle_restaurant",
                "primaryTypeDisplayName": {"text": "Udon noodle restaurant"},
                "types": ["udon_noodle_restaurant"],
            }
        )

        self.assertEqual(place.primary_type, "udon_noodle_restaurant")
        self.assertEqual(
            place.types,
            ["restaurant", "japanese_restaurant", "udon_noodle_restaurant"],
        )

    def test_normalize_enrichment_match_drops_generic_item_type(self) -> None:
        place = build_data.normalize_enrichment_match(
            {
                "id": "test-id",
                "name": "places/test-id",
                "displayName": {"text": "Deenyana - Little Switzerland - Palms Village"},
                "formattedAddress": "Alishan Township",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "primaryType": "item",
                "primaryTypeDisplayName": {"text": "公共住宅"},
                "types": ["item"],
            }
        )

        self.assertIsNone(place.primary_type)
        self.assertIsNone(place.primary_type_display_name)
        self.assertIsNone(place.primary_type_display_name_localized)
        self.assertEqual(place.types, [])

    def test_canonicalize_enrichment_place_drops_cached_generic_item_type(self) -> None:
        place = build_data.canonicalize_enrichment_place(
            EnrichmentPlace(
                display_name="Al Marmoom Heritage Village",
                primary_type="item",
                types=["item"],
            )
        )

        assert place is not None
        self.assertIsNone(place.primary_type)
        self.assertEqual(place.types, [])

    def test_canonicalize_enrichment_place_keeps_usable_types_from_invalid_cached_primary_type(self) -> None:
        place = build_data.canonicalize_enrichment_place(
            EnrichmentPlace(
                display_name="Russafa",
                primary_type="adults_only_boutique_hotel",
                primary_type_display_name="Adults Only Boutique Hotel",
                types=["shopping", "adults_only_boutique_hotel"],
            )
        )

        assert place is not None
        self.assertEqual(place.primary_type, "shopping")
        self.assertIsNone(place.primary_type_display_name)
        self.assertEqual(place.types, ["shopping"])

    def test_canonicalize_enrichment_place_recovers_category_from_types_when_primary_is_generic(self) -> None:
        place = build_data.canonicalize_enrichment_place(
            EnrichmentPlace(
                display_name="Cafe North",
                primary_type="point_of_interest",
                primary_type_display_name="Point of interest",
                types=["restaurant", "food", "point_of_interest"],
            )
        )

        assert place is not None
        self.assertEqual(place.primary_type, "restaurant")
        self.assertIsNone(place.primary_type_display_name)
        self.assertEqual(place.types, ["restaurant", "food"])

    def test_canonicalize_enrichment_place_preserves_localized_category_when_primary_is_generic(self) -> None:
        place = build_data.canonicalize_enrichment_place(
            EnrichmentPlace(
                display_name="Motsu Pot",
                primary_type="point_of_interest",
                primary_type_display_name="もつ鍋料理店",
                types=["point_of_interest"],
            )
        )

        assert place is not None
        self.assertIsNone(place.primary_type)
        self.assertIsNone(place.primary_type_display_name)
        self.assertEqual(place.primary_type_display_name_localized, "もつ鍋料理店")
        self.assertEqual(place.types, [])

    def test_humanize_type_id_rejects_generic_item(self) -> None:
        self.assertIsNone(build_data.humanize_type_id("item"))

    def test_humanize_type_id_rejects_weather_and_amenity_noise(self) -> None:
        self.assertIsNone(build_data.humanize_type_id("light_rain"))
        self.assertIsNone(build_data.humanize_type_id("clear_with_periodic_clouds"))
        self.assertIsNone(build_data.humanize_type_id("adults_only_boutique_hotel"))
        self.assertIsNone(build_data.humanize_type_id("beer"))
        self.assertIsNone(build_data.humanize_type_id("free_breakfast"))
        self.assertIsNone(build_data.humanize_type_id("transportation"))
        self.assertIsNone(build_data.humanize_type_id("free_wi_fi"))

    def test_normalize_enrichment_match_drops_weather_category(self) -> None:
        place = build_data.normalize_enrichment_match(
            {
                "id": "test-id",
                "name": "places/test-id",
                "displayName": {"text": "Christianshavn"},
                "formattedAddress": "Copenhagen, Denmark",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "primaryType": "clear_with_periodic_clouds",
                "primaryTypeDisplayName": {"text": "Clear with periodic clouds"},
                "types": ["clear_with_periodic_clouds"],
            }
        )

        self.assertIsNone(place.primary_type)
        self.assertIsNone(place.primary_type_display_name)
        self.assertIsNone(place.primary_type_display_name_localized)
        self.assertEqual(place.types, [])

    def test_fallback_semantic_description_trims_seo_stuffed_cjk_place_name(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="山之埕-嘉義景點 親子旅遊園區 阿里山景點 好評 打卡景點 伴手禮推薦 2025評價",
                primary_type_display_name="Tourist attraction",
                formatted_address="Chukou Village, Taiwan",
            ),
            raw_place=RawPlace(
                name="山之埕-嘉義景點 親子旅遊園區 阿里山景點 好評 打卡景點 伴手禮推薦 2024評價",
                maps_url="https://maps.example/shan",
                address=None,
            ),
            city_name="Alishan",
        )

        self.assertEqual(description, "山之埕 is a tourist attraction in Chukou Village.")

    def test_semantic_enrichment_evidence_trims_seo_stuffed_name(self) -> None:
        evidence = build_data.semantic_enrichment_evidence(
            EnrichmentPlace(
                display_name="鄒族 逐鹿文創園區-嘉義阿里山 原住民餐廳 IG PTT Dcard",
                primary_type_display_name="Restaurant",
            ),
            raw_place=RawPlace(
                name="鄒族 逐鹿文創園區-嘉義阿里山 原住民餐廳 IG PTT Dcard",
                maps_url="https://maps.example/veoveoana",
                address=None,
            ),
            city_name="Alishan",
            country_name="Taiwan",
        )

        self.assertEqual(evidence["name"], "鄒族 逐鹿文創園區")

    def test_normalize_enrichment_match_drops_beer_category(self) -> None:
        place = build_data.normalize_enrichment_match(
            {
                "id": "test-id",
                "name": "places/test-id",
                "displayName": {"text": "Made in Belfast Talbot Street"},
                "formattedAddress": "25 Talbot St, Belfast BT1 2LD, UK",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "primaryType": "beer",
                "primaryTypeDisplayName": {"text": "Beer"},
                "types": ["beer"],
            }
        )

        self.assertIsNone(place.primary_type)
        self.assertIsNone(place.primary_type_display_name)
        self.assertIsNone(place.primary_type_display_name_localized)
        self.assertEqual(place.types, [])

    def test_normalize_enrichment_match_drops_transportation_category(self) -> None:
        place = build_data.normalize_enrichment_match(
            {
                "id": "test-id",
                "name": "places/test-id",
                "displayName": {"text": "Jægersborggade"},
                "formattedAddress": "2200 Copenhagen, Denmark",
                "googleMapsUri": "https://maps.google.com/?cid=1",
                "primaryType": "transportation",
                "primaryTypeDisplayName": {"text": "Transportation"},
                "types": ["transportation"],
            }
        )

        self.assertIsNone(place.primary_type)
        self.assertIsNone(place.primary_type_display_name)
        self.assertIsNone(place.primary_type_display_name_localized)
        self.assertEqual(place.types, [])

    def test_fallback_semantic_description_uses_city_for_ui_address_fragments(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Al Seef St",
                formatted_address="10382+ Photos",
            ),
            raw_place=RawPlace(name="Al Seef", maps_url="https://maps.example/al-seef", address=None),
            city_name="Dubai",
        )

        self.assertEqual(description, "Al Seef St is a saved place in Dubai.")

    def test_fallback_semantic_description_uses_city_for_lodging_listing_address(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Russafa",
                formatted_address="VG Centrico, 2 habitaciones, confort ascensor,wifi AC",
            ),
            raw_place=RawPlace(name="Russafa", maps_url="https://maps.example/russafa", address=None),
            city_name="Valencia",
        )

        self.assertEqual(description, "Russafa is a saved place in Valencia.")

    def test_fallback_semantic_description_does_not_use_place_name_as_location(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Russafa",
                primary_type_display_name="Neighborhood",
                formatted_address="Russafa, Valencia, Spain",
            ),
            raw_place=RawPlace(name="Russafa", maps_url="https://maps.example/russafa", address=None),
            city_name="Valencia",
        )

        self.assertEqual(description, "Russafa is a neighborhood in Valencia.")

    def test_fallback_semantic_description_uses_city_for_full_address_locality(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Kite Beach",
                primary_type_display_name="Beach",
                formatted_address="Kite beach - Dubai - United Arab Emirates",
            ),
            raw_place=RawPlace(name="Kite Beach", maps_url="https://maps.example/kite-beach", address=None),
            city_name="Dubai",
        )

        self.assertEqual(description, "Kite Beach is a beach in Dubai.")

    def test_fallback_semantic_description_strips_leading_location_preposition(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Coco Grill & Bar",
                primary_type_display_name="Grill",
                formatted_address="at Paradeplatz, Bleicherweg 1, 8001 Zurich, Switzerland",
            ),
            raw_place=RawPlace(name="Coco Grill & Bar", maps_url="https://maps.example/coco", address=None),
            city_name="Zurich",
        )

        self.assertEqual(description, "Coco Grill & Bar is a grill in Paradeplatz.")

    def test_fallback_semantic_description_uses_natural_articles(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Didi's Frieden",
                primary_type_display_name="European restaurant",
                formatted_address="Old Town, Zurich, Switzerland",
            ),
            raw_place=RawPlace(name="Didi's Frieden", maps_url="https://maps.example/didis", address=None),
            city_name="Zurich",
        )

        self.assertEqual(description, "Didi's Frieden is a european restaurant in Old Town.")

    def test_fallback_semantic_description_uses_city_for_street_like_locality_fragments(self) -> None:
        cases = [
            ("Moe's Original BBQ", "Barbecue restaurant", "650 Broadway, Bangor, ME 04401", "Bangor"),
            ("Berghain | Panorama Bar", "Night club", "Am Wriezener bhf, 10243 Berlin, Germany", "Berlin"),
            ("DDR Museum", "Museum", "Vera Britain Ufer, Karl-Liebknecht-Str. 1, 10178 Berlin, Germany", "Berlin"),
            ("Exotic Garden of Monaco", "Garden", "62 Bd du Jardin Exotique, 98000 Monaco", "Monaco"),
            ("gerhard's café monaco", "Bar", "42 Quai Jean-Charles Rey, 98000 Monaco", "Monaco"),
            ("Basilica di San Petronio", "Basilica", "Piazza Maggiore, 1/e, 40124 Bologna BO, Italy", "Bologna"),
            ("Garisenda", "Tourist attraction", "P.za di Porta Ravegnana, 40126 Bologna BO, Italy", "Bologna"),
            ("Museo Civico Archeologico Bologna", "Archaeological museum", "V. dell'Archiginnasio, 2, 40124 Bologna BO, Italy", "Bologna"),
            ("IGY Vieux-Port de Cannes", "Marina", "Jetée Albert Edouard, 06400 Cannes, France", "Cannes"),
            (
                "Palace of Festivals and Congresses of Cannes",
                "Exhibition and trade center",
                "Palais des Festivals et des Congrès, 1 Bd de la Croisette, 06400 Cannes, France",
                "Cannes",
            ),
        ]

        for name, category, address, city_name in cases:
            with self.subTest(name=name):
                description = build_data.fallback_semantic_description(
                    EnrichmentPlace(
                        display_name=name,
                        primary_type_display_name=category,
                        formatted_address=address,
                    ),
                    raw_place=RawPlace(name=name, maps_url="https://maps.example/place", address=None),
                    city_name=city_name,
                )

                self.assertTrue(description.endswith(f" in {city_name}."))

    def test_fallback_semantic_description_strips_short_region_code_from_locality(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Faro di Punta Carena",
                primary_type_display_name="Historical landmark",
                formatted_address="Str. Faro di Carena, 80071 Anacapri NA, Italy",
            ),
            raw_place=RawPlace(name="Faro di Punta Carena", maps_url="https://maps.example/faro", address=None),
            city_name="Capri",
        )

        self.assertEqual(description, "Faro di Punta Carena is a historical landmark in Anacapri.")

    def test_fallback_semantic_description_prefers_plus_code_locality_for_bad_address(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Scala Fenicia",
                primary_type_display_name="Tourist attraction",
                formatted_address="plattl, 7c, 39054 Renon BZ, Italy",
                plus_code="H64H+H7 Anacapri, Metropolitan City of Naples, Italy",
            ),
            raw_place=RawPlace(name="Scala Fenicia", maps_url="https://maps.example/scala", address=None),
            city_name="Capri",
        )

        self.assertEqual(description, "Scala Fenicia is a tourist attraction in Anacapri.")

    def test_fallback_semantic_description_does_not_use_broader_plus_code_region(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Belvedere Tragara",
                primary_type_display_name="Observation deck",
                formatted_address="Via Tragara, 80073 Capri NA, Italy",
                plus_code="G7W2+42 Capri, Metropolitan City of Naples, Italy",
            ),
            raw_place=RawPlace(name="Belvedere Tragara", maps_url="https://maps.example/belvedere", address=None),
            city_name="Capri",
        )

        self.assertEqual(description, "Belvedere Tragara is an observation deck in Capri.")

    def test_normalize_address_locality_part_strips_localized_prefix_before_english(self) -> None:
        self.assertEqual(
            build_data.normalize_address_locality_part("青羊宫商圈 Qingyang District"),
            "Qingyang District",
        )

    def test_normalize_address_locality_part_keeps_localities_with_admin_words(self) -> None:
        self.assertEqual(build_data.normalize_address_locality_part("State College"), "State College")
        self.assertEqual(build_data.normalize_address_locality_part("County Line"), "County Line")
        self.assertIsNone(build_data.normalize_address_locality_part("Burqin County"))

    def test_normalize_semantic_neighborhood_part_keeps_near_prefixed_neighborhood(self) -> None:
        self.assertEqual(
            build_data.normalize_semantic_neighborhood_part("Near North Side"),
            "Near North Side",
        )

    def test_fallback_semantic_description_uses_explicit_region_when_no_locality(self) -> None:
        description = build_data.fallback_semantic_description(
            EnrichmentPlace(
                display_name="Ka Nasi Lake",
                primary_type_display_name="Lake",
                formatted_address="Burqin County, Altay Prefecture, China",
            ),
            raw_place=RawPlace(name="Ka Nasi Lake", maps_url="https://maps.example/kanas", address=None),
            city_name="Urumqi",
        )

        self.assertEqual(description, "Ka Nasi Lake is a lake in Burqin County.")

    def test_derive_marker_icon_uses_place_name_keyword_fallback_without_enrichment(self) -> None:
        test_cases = [
            ("Bar Rooster", "bar"),
            ("Bear Pond Espresso", "cafe"),
            ("Ameyoko Shopping District", "shopping"),
            ("Pizza Strada", "restaurant"),
        ]

        for place_name, expected_icon in test_cases:
            with self.subTest(place_name=place_name):
                self.assertEqual(
                    build_data.derive_marker_icon(
                        RawPlace(name=place_name, maps_url="https://maps.google.com/?cid=1"),
                        enrichment=EnrichmentPlace(),
                        category=None,
                        note=None,
                        why_recommended=None,
                    ),
                    expected_icon,
                )

    def test_derive_marker_icon_does_not_use_locality_tags_as_type_fallback(self) -> None:
        self.assertEqual(
            build_data.derive_marker_icon(
                RawPlace(
                    name="Plain Place",
                    address="1 Ocean Drive, Miami Beach, Park City",
                    maps_url="https://maps.google.com/?cid=1",
                ),
                enrichment=EnrichmentPlace(),
                category=None,
                note=None,
                why_recommended=None,
            ),
            "default",
        )

    def test_derive_place_tags_prefers_curated_enrichment_tags(self) -> None:
        tags = build_data.derive_place_tags(
            RawPlace(
                name="Coffee Counter",
                address="123 Main St, Williamsburg, Brooklyn, United States",
                maps_url="https://maps.google.com/?cid=1",
            ),
            "New York",
            enrichment=EnrichmentPlace(
                primary_type="coffee_shop",
                primary_type_display_name="Coffee shop",
                types=["coffee_shop", "food", "point_of_interest", "establishment"],
            ),
            category="Coffee shop",
        )

        self.assertIn("cafe", tags)
        self.assertIn("coffee-shop", tags)
        self.assertIn("food", tags)
        self.assertNotIn("point-of-interest", tags)
        self.assertNotIn("establishment", tags)

    def test_derive_place_tags_downweights_broad_localities_when_enrichment_is_specific(self) -> None:
        tags = build_data.derive_place_tags(
            RawPlace(
                name="Neighborhood Cafe",
                address="123 Main St, Williamsburg, Brooklyn, United States",
                maps_url="https://maps.google.com/?cid=1",
            ),
            "New York",
            enrichment=EnrichmentPlace(primary_type="cafe", types=["cafe", "food"]),
            category="Cafe",
        )

        self.assertIn("williamsburg", tags)
        self.assertNotIn("brooklyn", tags)
        self.assertIn("cafe", tags)

    def test_vibe_keyword_matching_uses_token_boundaries(self) -> None:
        self.assertFalse(build_data.vibe_keyword_matches("barcelona cafe", "bar"))
        self.assertTrue(build_data.vibe_keyword_matches("quiet bar seating", "bar"))

    def test_build_search_index_contains_compact_place_context(self) -> None:
        guide = build_data.normalize_guide(
            "tokyo-japan",
            RawSavedList(
                title="Tokyo, Japan",
                places=[
                    RawPlace(
                        name="Quiet Coffee",
                        address="1 Shibuya, Tokyo, Japan",
                        note="Quiet cafe with wifi.",
                        is_favorite=True,
                        maps_url="https://maps.google.com/?cid=1",
                        cid="111",
                    ),
                ],
            ),
            enrichment_cache={},
        )

        index = build_data.build_search_index([guide])

        self.assertEqual(index["version"], 1)
        self.assertEqual(index["generated_at"], build_data.STABLE_GENERATED_AT_FALLBACK)
        self.assertEqual(index["guides"][0]["slug"], "tokyo-japan")
        self.assertEqual(index["entries"][0]["guide_slug"], "tokyo-japan")
        self.assertEqual(index["entries"][0]["name"], "Quiet Coffee")
        self.assertIn("quiet", index["entries"][0]["vibe_tags"])
        self.assertIn("laptop-friendly", index["entries"][0]["vibe_tags"])
        self.assertIn("tokyo", index["entries"][0]["search_text"])

    def test_normalize_guide_uses_stable_generated_at_from_inputs(self) -> None:
        raw = RawSavedList(
            fetched_at="2026-04-18T00:00:00+00:00",
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Quiet Coffee",
                    address="1 Shibuya, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=1",
                    cid="111",
                ),
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0], source_type=raw.configured_source_type)
        guide = build_data.normalize_guide(
            "tokyo-japan",
            raw,
            enrichment_cache={
                place_id: EnrichmentCacheEntry(
                    fetched_at="2026-04-19T00:00:00+00:00",
                    last_verified_at="2026-04-20T00:00:00+00:00",
                    query="Quiet Coffee, 1 Shibuya, Tokyo, Japan",
                ),
            },
        )

        self.assertEqual(guide.generated_at, "2026-04-20T00:00:00+00:00")

    def test_build_search_index_includes_rating_fields_when_present_or_missing(self) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=2,
            places=[
                NormalizedPlace(
                    id="rated",
                    name="Rated Coffee",
                    maps_url="https://maps.example/rated",
                    rating=4.8,
                    user_rating_count=321,
                    status="active",
                ),
                NormalizedPlace(
                    id="unrated",
                    name="Unrated Tea",
                    maps_url="https://maps.example/unrated",
                    rating=None,
                    user_rating_count=None,
                    status="active",
                ),
            ],
        )

        index = build_data.build_search_index([guide])
        entries = {entry["id"]: entry for entry in index["entries"]}

        self.assertEqual(entries["rated"]["rating"], 4.8)
        self.assertEqual(entries["rated"]["user_rating_count"], 321)
        self.assertIn("rating", entries["unrated"])
        self.assertIsNone(entries["unrated"]["rating"])
        self.assertIn("user_rating_count", entries["unrated"])
        self.assertIsNone(entries["unrated"]["user_rating_count"])

    def test_search_index_and_manifest_skip_permanently_closed_places(self) -> None:
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo, Japan",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=2,
            featured_place_ids=["closed", "temporary"],
            places=[
                NormalizedPlace(
                    id="closed",
                    name="Closed Cafe",
                    maps_url="https://maps.example/closed",
                    status="closed-permanently",
                ),
                NormalizedPlace(
                    id="temporary",
                    name="Temporary Tea",
                    maps_url="https://maps.example/temporary",
                    status="temporarily-closed",
                ),
                NormalizedPlace(
                    id="active",
                    name="Active Coffee",
                    maps_url="https://maps.example/active",
                    status="active",
                ),
            ],
        )

        manifest = build_data.summarize_guide(guide)
        index = build_data.build_search_index([guide])

        self.assertEqual(manifest.featured_names, ["Temporary Tea"])
        self.assertEqual(index["guides"][0]["featured_names"], ["Temporary Tea"])
        self.assertEqual([entry["id"] for entry in index["entries"]], ["temporary", "active"])

    def test_guide_location_center_excludes_far_outliers(self) -> None:
        places = [
            NormalizedPlace(
                id="tokyo-1",
                name="Tokyo 1",
                lat=35.66,
                lng=139.7,
                maps_url="https://maps.example/tokyo-1",
                status="active",
            ),
            NormalizedPlace(
                id="tokyo-2",
                name="Tokyo 2",
                lat=35.67,
                lng=139.71,
                maps_url="https://maps.example/tokyo-2",
                status="active",
            ),
            NormalizedPlace(
                id="tokyo-3",
                name="Tokyo 3",
                lat=35.65,
                lng=139.69,
                maps_url="https://maps.example/tokyo-3",
                status="active",
            ),
            NormalizedPlace(
                id="bad-import",
                name="Bad import",
                lat=48.86,
                lng=2.35,
                maps_url="https://maps.example/bad-import",
                status="active",
            ),
        ]

        center_lat, center_lng = build_data.guide_location_center(places)

        self.assertAlmostEqual(center_lat or 0, 35.66, places=3)
        self.assertAlmostEqual(center_lng or 0, 139.7, places=3)

    def test_warn_far_map_pins_prints_cli_warning_for_distant_places(self) -> None:
        places = [
            NormalizedPlace(
                id="tokyo-1",
                name="Tokyo 1",
                lat=35.66,
                lng=139.7,
                maps_url="https://maps.example/tokyo-1",
                status="active",
            ),
            NormalizedPlace(
                id="bad-import",
                name="Bad import",
                lat=48.86,
                lng=2.35,
                maps_url="https://maps.example/bad-import",
                status="active",
            ),
        ]

        with patch("builtins.print") as print_mock:
            build_data.warn_far_map_pins("tokyo-japan", places, (35.665, 139.705))

        self.assertEqual(print_mock.call_count, 1)
        warning = print_mock.call_args.args[0]
        self.assertIn("WARNING: tokyo-japan:bad-import", warning)
        self.assertIn("check whether it belongs in this city/country", warning)

    def test_suppress_far_map_pins_clears_extreme_outlier_coordinates(self) -> None:
        places = [
            NormalizedPlace(
                id="tokyo-1",
                name="Tokyo 1",
                lat=35.66,
                lng=139.7,
                maps_url="https://maps.example/tokyo-1",
                status="active",
            ),
            NormalizedPlace(
                id="tokyo-2",
                name="Tokyo 2",
                lat=35.67,
                lng=139.71,
                maps_url="https://maps.example/tokyo-2",
                status="active",
            ),
            NormalizedPlace(
                id="tokyo-3",
                name="Tokyo 3",
                lat=35.65,
                lng=139.69,
                maps_url="https://maps.example/tokyo-3",
                status="active",
            ),
            NormalizedPlace(
                id="bad-import",
                name="Bad import",
                lat=48.86,
                lng=2.35,
                maps_url="https://maps.example/bad-import",
                status="active",
            ),
        ]

        with patch("builtins.print") as print_mock:
            build_data.suppress_far_map_pins("tokyo-japan", places, (35.665, 139.705))

        self.assertEqual((places[-1].lat, places[-1].lng), (None, None))
        suppression_warning = print_mock.call_args.args[0]
        self.assertIn("Suppressing map pin for tokyo-japan:bad-import", suppression_warning)

    def test_suppress_far_map_pins_keeps_non_extreme_outlier_coordinates(self) -> None:
        places = [
            NormalizedPlace(
                id="la-1",
                name="Los Angeles 1",
                lat=34.0522,
                lng=-118.2437,
                maps_url="https://maps.example/la-1",
                status="active",
            ),
            NormalizedPlace(
                id="la-2",
                name="Los Angeles 2",
                lat=34.0622,
                lng=-118.2537,
                maps_url="https://maps.example/la-2",
                status="active",
            ),
            NormalizedPlace(
                id="la-3",
                name="Los Angeles 3",
                lat=34.0722,
                lng=-118.2637,
                maps_url="https://maps.example/la-3",
                status="active",
            ),
            NormalizedPlace(
                id="far-but-possible",
                name="Far but possible",
                lat=34.0522,
                lng=-119.3537,
                maps_url="https://maps.example/far-but-possible",
                status="active",
            ),
        ]

        with patch("builtins.print") as print_mock:
            build_data.suppress_far_map_pins("los-angeles-california-usa", places, (34.0622, -118.2537))

        self.assertIsNotNone(places[-1].lat)
        self.assertIsNotNone(places[-1].lng)
        print_mock.assert_not_called()

    def test_suppress_far_map_pins_skips_sparse_guides(self) -> None:
        places = [
            NormalizedPlace(
                id="tokyo-1",
                name="Tokyo 1",
                lat=35.66,
                lng=139.7,
                maps_url="https://maps.example/tokyo-1",
                status="active",
            ),
            NormalizedPlace(
                id="tokyo-2",
                name="Tokyo 2",
                lat=35.67,
                lng=139.71,
                maps_url="https://maps.example/tokyo-2",
                status="active",
            ),
            NormalizedPlace(
                id="bad-import",
                name="Bad import",
                lat=48.86,
                lng=2.35,
                maps_url="https://maps.example/bad-import",
                status="active",
            ),
        ]

        with patch("builtins.print") as print_mock:
            build_data.suppress_far_map_pins(
                "tokyo-japan",
                places,
                build_data.guide_location_center(places),
            )

        self.assertEqual((places[-1].lat, places[-1].lng), (48.86, 2.35))
        print_mock.assert_not_called()

    def test_guide_map_pin_warning_distance_uses_inlier_radius_plus_buffer(self) -> None:
        places = [
            NormalizedPlace(
                id="urumqi-1",
                name="Urumqi 1",
                lat=43.811,
                lng=87.606,
                maps_url="https://maps.example/urumqi-1",
                status="active",
            ),
            NormalizedPlace(
                id="urumqi-2",
                name="Urumqi 2",
                lat=43.825,
                lng=87.615,
                maps_url="https://maps.example/urumqi-2",
                status="active",
            ),
            NormalizedPlace(
                id="urumqi-3",
                name="Urumqi 3",
                lat=43.801,
                lng=87.649,
                maps_url="https://maps.example/urumqi-3",
                status="active",
            ),
            NormalizedPlace(
                id="urumqi-4",
                name="Urumqi 4",
                lat=43.789,
                lng=87.632,
                maps_url="https://maps.example/urumqi-4",
                status="active",
            ),
            NormalizedPlace(
                id="nalati",
                name="Nalati Grassland",
                lat=43.292522,
                lng=84.229038,
                maps_url="https://maps.example/nalati",
                status="active",
            ),
        ]

        center = build_data.guide_location_center(places)
        threshold = build_data.guide_map_pin_warning_distance_meters(places, center)

        self.assertIsNotNone(threshold)
        self.assertEqual(threshold, build_data.MAP_PIN_DISTANCE_WARNING_MIN_METERS)

    def test_warn_far_map_pins_warns_for_medium_distance_bad_imports(self) -> None:
        places = [
            NormalizedPlace(
                id="tokyo-1",
                name="Tokyo 1",
                lat=35.6600,
                lng=139.7000,
                maps_url="https://maps.example/tokyo-1",
                status="active",
            ),
            NormalizedPlace(
                id="tokyo-2",
                name="Tokyo 2",
                lat=35.6760,
                lng=139.6990,
                maps_url="https://maps.example/tokyo-2",
                status="active",
            ),
            NormalizedPlace(
                id="tokyo-3",
                name="Tokyo 3",
                lat=35.6700,
                lng=139.7350,
                maps_url="https://maps.example/tokyo-3",
                status="active",
            ),
            NormalizedPlace(
                id="tokyo-4",
                name="Tokyo 4",
                lat=35.6890,
                lng=139.6910,
                maps_url="https://maps.example/tokyo-4",
                status="active",
            ),
            NormalizedPlace(
                id="beaumont-import",
                name="Kasama Restaurant",
                lat=33.9290518,
                lng=-116.9776347,
                maps_url="https://maps.example/beaumont-import",
                status="active",
            ),
        ]

        center = build_data.guide_location_center(places)

        with patch("builtins.print") as print_mock:
            build_data.warn_far_map_pins("tokyo-japan", places, center)

        self.assertEqual(print_mock.call_count, 1)
        warning = print_mock.call_args.args[0]
        self.assertIn("WARNING: tokyo-japan:beaumont-import", warning)
        self.assertIn("Kasama Restaurant", warning)

    def test_country_inference_keeps_monaco_english_with_localized_address_tails(self) -> None:
        raw = RawSavedList(
            title="Monaco 🇲🇨",
            places=[
                RawPlace(
                    name="Cafe",
                    address="42 Quai Jean-Charles Rey, 98000 Monaco, モナコ",
                    maps_url="https://maps.google.com/?cid=1",
                ),
                RawPlace(
                    name="Cathedral",
                    address="4 Rue Colonel Bellando de Castro, 98000 Monaco, モナコ",
                    maps_url="https://maps.google.com/?cid=2",
                ),
                RawPlace(
                    name="Garden",
                    address="モナコ 〒98000 モナコ",
                    maps_url="https://maps.google.com/?cid=3",
                ),
            ],
        )

        self.assertEqual(build_data.infer_country_name(raw.title or "", raw), "Monaco")
        self.assertEqual(
            build_data.infer_country_from_address("42 Quai Jean-Charles Rey, 98000 Monaco, モナコ"),
            "Monaco",
        )
        self.assertEqual(build_data.infer_country_code("Monaco"), "MC")

    def test_country_inference_uses_flag_before_address_fragments(self) -> None:
        raw = RawSavedList(
            title="Okinawa Main Island 🇯🇵",
            description="Michael's list for Okinawa, Japan 🇯🇵",
            places=[
                RawPlace(
                    name="Street Fragment",
                    address="〒900-0015 Okinawa, Naha, Kumoji, 2 Chome−19−17 RS-ONE 1F",
                    maps_url="https://maps.google.com/?cid=1",
                ),
                RawPlace(
                    name="Postal Fragment",
                    address="3 Chome-9-26 Makishi, Naha, Okinawa 900-0013",
                    maps_url="https://maps.google.com/?cid=2",
                ),
            ],
        )

        self.assertEqual(build_data.infer_country_name(raw.title or "", raw), "Japan")
        self.assertIsNone(build_data.infer_country_from_address(raw.places[0].address))
        self.assertIsNone(build_data.infer_country_from_address(raw.places[1].address))

    def test_country_inference_ignores_parenthetical_title_suffix(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan (Example) 🇹🇼",
            places=[],
        )

        self.assertEqual(build_data.infer_city_name(raw.title or ""), "Taipei")
        self.assertEqual(build_data.infer_country_name(raw.title or "", raw), "Taiwan")

    def test_guide_location_context_uses_list_overrides_before_raw_title(self) -> None:
        raw = RawSavedList(
            title="Alamaty, Kazakhstan 🇰🇿",
            places=[],
        )

        with TemporaryDirectory() as tmpdir:
            list_overrides_dir = Path(tmpdir)
            (list_overrides_dir / "almaty-kazakhstan.json").write_text(
                json.dumps({"title": "Almaty, Kazakhstan 🇰🇿", "city_name": "Almaty"}),
                encoding="utf-8",
            )

            with patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir):
                city_name, country_name = build_data.guide_location_context("almaty-kazakhstan", raw)

        self.assertEqual(city_name, "Almaty")
        self.assertEqual(country_name, "Kazakhstan")

    def test_address_locality_tags_exclude_buildings_blocks_and_postal_fragments(self) -> None:
        enrichment = EnrichmentPlace()
        addresses = [
            (
                "5 Chome-13-14 Jingumae, Shibuya City, Tokyo 150-0001, Japan",
                "Tokyo",
                {"jingumae", "shibuya-city", "tokyo"},
                {"chome-13-14-jingumae", "tokyo-150-0001"},
                "Jingumae",
            ),
            (
                "〒104-0045 Tokyo, Chuo City, Tsukiji, 3 Chome−16−9 アーバンメイツビル １F",
                "Tokyo",
                {"chuo-city", "tsukiji", "tokyo"},
                {"tokyo-104-0045", "chome-16-9", "urbanmeitsubiru"},
                "Tsukiji",
            ),
            (
                "Japan, 〒105-0004 Tokyo, Minato City, Shinbashi, 3 Chome−8−5, Le Gratteciel, 号 B1",
                "Tokyo",
                {"minato-city", "shinbashi", "tokyo"},
                {"le-gratteciel", "chome-8-5"},
                "Shinbashi",
            ),
            (
                "123 Main St, Williamsburg, Brooklyn, United States",
                "New York",
                {"williamsburg", "brooklyn", "new-york"},
                {"main-st", "united-states"},
                "Williamsburg",
            ),
            (
                "10 Rue de Rivoli, Le Marais, Paris, France",
                "Paris",
                {"le-marais", "paris"},
                {"rue-de-rivoli", "france"},
                "Le Marais",
            ),
            (
                "1 Via Roma, Brera, Milan, Italy",
                "Milan",
                {"brera", "milan"},
                {"via-roma", "italy"},
                "Brera",
            ),
            (
                "1 Oxford St, Soho, London, United Kingdom",
                "London",
                {"soho", "london"},
                {"oxford-st", "united-kingdom"},
                "Soho",
            ),
            (
                "106 台湾 Taipei City, Da’an District, Section 4, Zhongxiao E Rd, 250-4號1F",
                "Taipei",
                {"da-an-district", "taipei"},
                {"taiwan", "taipei-city", "zhongxiao-e-rd"},
                "Da’an District",
            ),
            (
                "No, No. 60, Section 2, Hankou St, Wanhua District, Taipei City, 108",
                "Taipei",
                {"wanhua-district", "taipei"},
                {"no", "taipei-city", "hankou-st"},
                "Wanhua District",
            ),
            (
                "Japan, 044-0081 Hokkaido, Abuta District, Kutchan, Yamada, 132-26",
                "Niseko",
                {"kutchan", "yamada", "niseko"},
                {"hokkaido"},
                "Kutchan",
            ),
            (
                "Karuizawa, Kitasaku District, Nagano 389-0102, Japan",
                "Karuizawa",
                {"karuizawa"},
                {"kitasaku-district", "nagano"},
                None,
            ),
            (
                "2 Chome-7-22 Asato, Naha, Okinawa 902-0067, Japan",
                "Okinawa Main Island",
                {"asato", "naha", "okinawa-main-island"},
                {"okinawa"},
                "Asato",
            ),
            (
                "C/ de Manso, 22, L'Eixample, 08015 Barcelona, Spain",
                "Barcelona",
                {"l-eixample", "barcelona"},
                {"c-de-manso"},
                "L'Eixample",
            ),
            (
                "Carrer de Blai, 47, Sants-Montjuïc, 08004 Barcelona, Spain",
                "Barcelona",
                {"sants-montjuic", "barcelona"},
                {"carrer-de-blai"},
                "Sants-Montjuïc",
            ),
            (
                "Plaça Comercial, 10, Ciutat Vella, 08003 Barcelona, Spain",
                "Barcelona",
                {"ciutat-vella", "barcelona"},
                {"placa-comercial"},
                "Ciutat Vella",
            ),
            (
                "La Rambla, 124, Ciutat Vella, 08002 Barcelona, スペイン",
                "Barcelona",
                {"ciutat-vella", "barcelona"},
                {"スペイン"},
                "Ciutat Vella",
            ),
            (
                "Av. del Paral·lel, 126 bis, Eixample, 08015 Barcelona, Spain",
                "Barcelona",
                {"eixample", "barcelona"},
                {"bis", "av-del-parallel"},
                "Eixample",
            ),
            (
                "Pl. de Carles Buïgas, 1, Sants-Montjuïc, 08038 Barcelona, Spain",
                "Barcelona",
                {"sants-montjuic", "barcelona"},
                {"pl-de-carles-buigas"},
                "Sants-Montjuïc",
            ),
            (
                "Moll dels Pescadors, 1, Barceloneta, Ciutat Vella, 08003 Barcelona, Spain",
                "Barcelona",
                {"barceloneta", "ciutat-vella", "barcelona"},
                {"moll-dels-pescadors"},
                "Barceloneta",
            ),
            (
                "Dentro del restaurante The Lobster Roll, Carrer del Rec Comtal, 12, Eixample, 08003 Barcelona, Spain",
                "Barcelona",
                {"eixample", "barcelona"},
                {"dentro-del-restaurante-the-lobster-roll"},
                "Eixample",
            ),
            (
                "Carrer de Marià Labèrnia, s/n, El Carmel, Horta-Guinardó, 08032 Barcelona, Spain",
                "Barcelona",
                {"el-carmel", "horta-guinardo", "barcelona"},
                {"s-n"},
                "El Carmel",
            ),
            (
                "144 Elizabeth St, Hobart TAS 7000, Australia",
                "Tasmania",
                {"hobart", "tasmania"},
                {"tas"},
                "Hobart",
            ),
            (
                "Galerie de la Reine 5, 1000 Brussel, Belgium",
                "Brussels",
                {"brussels"},
                {"brussel", "belgium"},
                None,
            ),
            (
                "26-28 Cotham Rd, Kew VIC 3101, Australia",
                "Melbourne",
                {"kew", "melbourne"},
                {"vic"},
                "Kew",
            ),
            (
                "Council Pl, Sydney NSW 2000, Australia",
                "Sydney",
                {"sydney"},
                {"nsw", "council-pl"},
                None,
            ),
            (
                "Boston, MA 02108, United States",
                "Boston",
                {"boston"},
                {"ma"},
                None,
            ),
            (
                "Belfast, County Antrim, Northern Ireland",
                "Belfast",
                {"belfast"},
                {"county-antrim"},
                None,
            ),
            (
                "3 Prom. des Anglais, 06000 Nice, France",
                "Nice",
                {"nice"},
                {"prom-des-anglais"},
                None,
            ),
            (
                "P.º del Tránsito, s/n, 45002 Toledo, Spain",
                "Toledo",
                {"toledo"},
                {"paseo-del-transito"},
                None,
            ),
            (
                "Rathauspl. 1, 90403 Nürnberg, Germany",
                "Nuremberg",
                {"nuremberg"},
                {"nurnberg", "nürnberg", "germany"},
                None,
            ),
            (
                "Pilestræde 39, 1112 København, Denmark",
                "Copenhagen",
                {"copenhagen"},
                {"kobenhavn", "københavn", "denmark"},
                None,
            ),
            (
                "Barer Str. 27, 80333 München, Germany",
                "Munich",
                {"munich"},
                {"munchen", "münchen", "germany"},
                None,
            ),
        ]

        for address, city_name, expected_tags, rejected_tags, expected_neighborhood in addresses:
            with self.subTest(address=address):
                place = RawPlace(
                    name="Test Place",
                    address=address,
                    maps_url="https://maps.google.com/?cid=1",
                )
                tags = set(
                    build_data.derive_place_tags(
                        place,
                        city_name,
                        enrichment=enrichment,
                        category=None,
                    )
                )

                self.assertEqual(
                    build_data.infer_neighborhood(address, city_name=city_name),
                    expected_neighborhood,
                )
                self.assertTrue(expected_tags.issubset(tags))
                self.assertTrue(tags.isdisjoint(rejected_tags))

    def test_import_saved_list_csv_reads_description_notes_and_maps_tokens(self) -> None:
        with TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "alishan.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "A foggy mountain weekend",
                        "",
                        "Title,Note,URL,Tags,Comment",
                        (
                            "Legacy Tea Stop,Best at sunrise,"
                            "https://www.google.com/maps/place/Tea+House/data=!4m2!3m1!1s0xabc123:0xdef456,,Order the oolong"
                        ),
                        "Fallback Name,,https://maps.app.goo.gl/short-link,,",
                    ]
                ),
                encoding="utf-8",
            )
            source = SourceConfig(
                slug="alishan-taiwan",
                type="google_export_csv",
                path=str(csv_path),
                title="Alishan, Taiwan",
            )

            saved_list = build_data.import_saved_list_csv(source)

        self.assertEqual(saved_list.title, "Alishan, Taiwan")
        self.assertEqual(saved_list.description, "A foggy mountain weekend")
        self.assertEqual(len(saved_list.places), 2)

        first_place = saved_list.places[0]
        second_place = saved_list.places[1]

        self.assertEqual(first_place.name, "Tea House")
        self.assertEqual(first_place.note, "Best at sunrise\n\nOrder the oolong")
        self.assertEqual(first_place.maps_place_token, "0xabc123:0xdef456")
        self.assertEqual(
            build_data.stable_place_id(first_place, source_type="google_export_csv"),
            "gms:0xabc123:0xdef456",
        )
        self.assertEqual(second_place.name, "Fallback Name")
        self.assertTrue(
            build_data.stable_place_id(second_place, source_type="google_export_csv").startswith("url:")
        )

    def test_normalize_guide_prefers_enrichment_name_for_csv_sources_and_tracks_provenance(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            configured_source_type="google_export_csv",
            fetched_at="2026-04-15T00:00:00+00:00",
            refresh_after="2026-04-29T00:00:00+00:00",
            places=[
                RawPlace(
                    name="Legacy Tea Stop",
                    address=None,
                    maps_url="https://maps.google.com/?cid=1",
                    maps_place_token="0xabc123:0xdef456",
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0], source_type=raw.configured_source_type)
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-16T00:00:00+00:00",
                refresh_after="2026-04-23T00:00:00+00:00",
                query="Legacy Tea Stop",
                matched=True,
                score=80,
                place=EnrichmentPlace(
                    display_name="Modern Tea House",
                    formatted_address="1 Songshan, Taipei, Taiwan",
                    google_maps_uri="https://maps.google.com/?cid=override",
                    primary_type_display_name="Tea house",
                    types=["tea_house"],
                ),
            )
        }

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            list_overrides_dir = tmpdir_path / "lists"
            place_overrides_dir = tmpdir_path / "places"
            list_overrides_dir.mkdir()
            place_overrides_dir.mkdir()

            with (
                patch.object(build_data, "LIST_OVERRIDES_DIR", list_overrides_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", place_overrides_dir),
            ):
                guide = build_data.normalize_guide(
                    "taipei-taiwan",
                    raw,
                    enrichment_cache=enrichment_cache,
                )

        place = guide.places[0]
        self.assertEqual(place.name, "Modern Tea House")
        self.assertEqual(place.address, "1 Songshan, Taipei, Taiwan")
        self.assertEqual(
            place.maps_url,
            "https://www.google.com/maps/search/?api=1&query=Modern+Tea+House%2C+1+Songshan%2C+Taipei%2C+Taiwan",
        )
        self.assertEqual(place.provenance.name.source, "google_places")
        self.assertEqual(place.provenance.name.fetched_at, "2026-04-16T00:00:00+00:00")
        self.assertEqual(place.provenance.address.source, "google_places")
        self.assertEqual(place.provenance.maps_url.source, "google_places")
        self.assertEqual(place.provenance.primary_category.source, "google_places")
        self.assertEqual(
            {field.value: field.source for field in place.provenance.tags},
            {
                "cafe": "google_places",
                "taipei": "google_list",
                "tea-house": "google_places",
            },
        )

    def test_normalize_guide_prefers_place_id_search_url_when_available(self) -> None:
        raw = RawSavedList(
            title="Sydney, Australia",
            places=[
                RawPlace(
                    name="Cantina OK!",
                    address="Council Pl, Sydney NSW 2000, Australia",
                    lat=-33.8702175,
                    lng=151.2051413,
                    maps_url="https://maps.google.com/?cid=7715422616180689913",
                    cid="7715422616180689913",
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0])
        enrichment_cache = {
            place_id: EnrichmentCacheEntry(
                fetched_at="2026-04-16T00:00:00+00:00",
                query="Cantina OK!, Sydney",
                matched=True,
                score=88,
                place=EnrichmentPlace(
                    display_name="Cantina OK!",
                    formatted_address="Council Pl, Sydney NSW 2000, Australia",
                    google_maps_uri="https://maps.google.com/?cid=7715422616180689913",
                    google_place_id="ChIJGcmcg7ZC1moRAOacd3HoEwM",
                ),
            )
        }

        guide = build_data.normalize_guide("sydney-australia", raw, enrichment_cache=enrichment_cache)

        self.assertEqual(
            guide.places[0].maps_url,
            (
                "https://www.google.com/maps/search/?api=1"
                "&query=Cantina+OK%21%2C+Council+Pl%2C+Sydney+NSW+2000%2C+Australia"
                "&query_place_id=ChIJGcmcg7ZC1moRAOacd3HoEwM"
            ),
        )

    def test_normalize_guide_recomputes_center_after_pin_suppression(self) -> None:
        raw = RawSavedList(
            title="Tokyo, Japan",
            places=[
                RawPlace(
                    name="Tokyo 1",
                    address="1 Shibuya, Tokyo, Japan",
                    lat=35.66,
                    lng=139.70,
                    maps_url="https://maps.google.com/?cid=1",
                    cid="1",
                ),
                RawPlace(
                    name="Tokyo 2",
                    address="2 Shibuya, Tokyo, Japan",
                    lat=35.67,
                    lng=139.71,
                    maps_url="https://maps.google.com/?cid=2",
                    cid="2",
                ),
            ],
        )

        def suppress_and_mutate(
            _slug: str,
            places: list[NormalizedPlace],
            _center: tuple[float | None, float | None],
        ) -> None:
            places[-1].lat = None
            places[-1].lng = None

        with (
            patch.object(build_data, "suppress_far_map_pins", side_effect=suppress_and_mutate),
            patch.object(build_data, "warn_far_map_pins") as warn_mock,
        ):
            guide = build_data.normalize_guide("tokyo-japan", raw, enrichment_cache={})

        self.assertAlmostEqual(guide.center_lat or 0, 35.66, places=3)
        self.assertAlmostEqual(guide.center_lng or 0, 139.70, places=3)
        self.assertEqual(warn_mock.call_args.args[2], (guide.center_lat, guide.center_lng))

    def test_resolve_refresh_sources_matches_csv_path_selector(self) -> None:
        sources = [
            SourceConfig(
                slug="tokyo-japan",
                type="google_list_url",
                url="https://maps.app.goo.gl/tokyo",
            ),
            SourceConfig(
                slug="alishan-taiwan",
                type="google_export_csv",
                path="data/raw/alishan-taiwan.csv",
                title="Alishan, Taiwan",
            ),
        ]

        selected_sources = build_data.resolve_refresh_sources(sources, ["data/raw/alishan-taiwan.csv"])

        self.assertEqual([source.slug for source in selected_sources], ["alishan-taiwan"])

    def test_raw_source_refresh_reason_respects_future_refresh_after(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )
        saved_list = RawSavedList(
            fetched_at=datetime.now(UTC).isoformat(),
            refresh_after=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            source_signature=build_data.raw_source_signature(source),
        )

        self.assertIsNone(build_data.raw_source_refresh_reason(source, saved_list))

    def test_raw_source_refresh_reason_detects_config_change_despite_future_refresh_after(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )
        saved_list = RawSavedList(
            fetched_at=datetime.now(UTC).isoformat(),
            refresh_after=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            source_signature="old-signature",
        )

        self.assertEqual(build_data.raw_source_refresh_reason(source, saved_list), "source-config-changed")

    def test_raw_source_refresh_reason_accepts_legacy_url_source_signature(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )
        saved_list = RawSavedList(
            fetched_at=datetime.now(UTC).isoformat(),
            refresh_after=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            source_signature=build_data.legacy_google_list_source_signature(source),
        )

        self.assertIsNone(build_data.raw_source_refresh_reason(source, saved_list))

    def test_refresh_raw_sources_skips_fresh_url_snapshot(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        with TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir)
            raw_path = raw_dir / "tokyo-japan.json"
            build_data.write_json(
                raw_path,
                RawSavedList(
                    fetched_at=datetime.now(UTC).isoformat(),
                    refresh_after=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    source_signature=build_data.raw_source_signature(source),
                    places=[],
                ),
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "load_sources", return_value=[source]),
                patch.object(build_data, "scrape_google_list_url") as scrape,
            ):
                build_data.refresh_raw_sources(
                    headed=False,
                    force_refresh=False,
                    refresh_lists=[],
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

        scrape.assert_not_called()

    def test_refresh_raw_sources_force_bypasses_fresh_url_snapshot(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        with TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir)
            raw_path = raw_dir / "tokyo-japan.json"
            build_data.write_json(
                raw_path,
                RawSavedList(
                    fetched_at=datetime.now(UTC).isoformat(),
                    refresh_after=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    source_signature=build_data.raw_source_signature(source),
                    places=[],
                ),
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "load_sources", return_value=[source]),
                patch.object(
                    build_data,
                    "scrape_google_list_url",
                    return_value=RawSavedList(title="Fresh", places=[]),
                ) as scrape,
            ):
                build_data.refresh_raw_sources(
                    headed=False,
                    force_refresh=True,
                    refresh_lists=[],
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

        scrape.assert_called_once_with(source, headed=False)

    def test_scraper_session_identity_key_changes_with_proxy(self) -> None:
        self.assertEqual(build_data.scraper_session_identity_key(None), "direct")
        self.assertNotEqual(
            build_data.scraper_session_identity_key("http://proxy-a.example:8080"),
            build_data.scraper_session_identity_key("http://proxy-b.example:8080"),
        )
        self.assertEqual(
            build_data.scraper_session_identity_key(None, session_scope="place-Japan-Tokyo"),
            "direct-place-japan-tokyo",
        )

    def test_ensure_scraper_session_state_expires_idle_sessions(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            stale_now = datetime(2026, 4, 1, tzinfo=UTC)
            fresh_now = stale_now + build_data.SCRAPER_SESSION_MAX_AGE + timedelta(minutes=1)

            with patch.object(build_data, "SCRAPER_STATE_DIR", state_dir):
                state = build_data.ensure_scraper_session_state(None, now=stale_now)
                state.browser_profile_dir.mkdir(parents=True, exist_ok=True)
                state.http_cookie_jar_path.write_text("cookie", encoding="utf-8")
                build_data.record_scraper_session_use(state, proxy=None, now=stale_now)
                build_data.release_scraper_session_lock(state)

                refreshed = build_data.ensure_scraper_session_state(None, now=fresh_now)

            self.assertTrue(refreshed.identity_dir.is_dir())
            self.assertFalse(refreshed.http_cookie_jar_path.exists())
            self.assertFalse(refreshed.browser_profile_dir.exists())
            build_data.release_scraper_session_lock(refreshed)

    def test_scraper_session_is_stale_without_metadata_when_artifacts_exist(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with patch.object(build_data, "SCRAPER_STATE_DIR", state_dir):
                state = build_data.build_scraper_session_state(None)
                state.browser_profile_dir.mkdir(parents=True, exist_ok=True)

            self.assertTrue(build_data.scraper_session_is_stale(state))

    def test_build_scraper_session_state_uses_stable_slot_state_files(self) -> None:
        state = build_data.build_scraper_session_state("http://proxy.example:8080")
        self.assertEqual(state.slot_key, "slot-0")
        self.assertEqual(state.browser_profile_dir.name, "slot-0")
        self.assertEqual(state.http_cookie_jar_path.name, "http-cookies.slot-0.txt")
        self.assertEqual(state.metadata_path.name, "metadata.slot-0.json")

    def test_ensure_scraper_session_state_reuses_stable_slot_files_after_lock_release(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            now = datetime(2026, 4, 1, tzinfo=UTC)

            with patch.object(build_data, "SCRAPER_STATE_DIR", state_dir):
                first = build_data.ensure_scraper_session_state(None, now=now)
                build_data.record_scraper_session_use(first, proxy=None, now=now)
                build_data.release_scraper_session_lock(first)

                second = build_data.ensure_scraper_session_state(None, now=now + timedelta(hours=1))

            self.assertEqual(second.slot_key, first.slot_key)
            self.assertEqual(second.browser_profile_dir, first.browser_profile_dir)
            self.assertEqual(second.http_cookie_jar_path, first.http_cookie_jar_path)
            self.assertEqual(second.metadata_path, first.metadata_path)
            build_data.release_scraper_session_lock(second)

    def test_clear_scraper_session_state_keeps_other_slot_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with patch.object(build_data, "SCRAPER_STATE_DIR", state_dir):
                state = build_data.build_scraper_session_state(None)
                sibling_browser_dir = state.identity_dir / "browser" / "slot-1"
                sibling_cookie_jar_path = state.identity_dir / "http-cookies.slot-1.txt"
                sibling_metadata_path = state.identity_dir / "metadata.slot-1.json"

                state.browser_profile_dir.mkdir(parents=True, exist_ok=True)
                state.http_cookie_jar_path.write_text("current", encoding="utf-8")
                state.metadata_path.write_text("{}", encoding="utf-8")

                sibling_browser_dir.mkdir(parents=True, exist_ok=True)
                sibling_cookie_jar_path.write_text("sibling", encoding="utf-8")
                sibling_metadata_path.write_text("{}", encoding="utf-8")

                build_data.clear_scraper_session_state(state)

            self.assertFalse(state.browser_profile_dir.exists())
            self.assertFalse(state.http_cookie_jar_path.exists())
            self.assertFalse(state.metadata_path.exists())
            self.assertTrue(sibling_browser_dir.exists())
            self.assertTrue(sibling_cookie_jar_path.exists())
            self.assertTrue(sibling_metadata_path.exists())

    def test_ensure_scraper_session_state_sweeps_stale_sibling_slot_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            stale_now = datetime(2026, 4, 1, tzinfo=UTC)
            fresh_now = stale_now + build_data.SCRAPER_SESSION_MAX_AGE + timedelta(minutes=1)

            with patch.object(build_data, "SCRAPER_STATE_DIR", state_dir):
                stale_state = build_data.build_scraper_session_state(None, slot_key="slot-1")
                stale_state.browser_profile_dir.mkdir(parents=True, exist_ok=True)
                stale_state.http_cookie_jar_path.write_text("cookie", encoding="utf-8")
                build_data.record_scraper_session_use(stale_state, proxy=None, now=stale_now)

                current_state = build_data.ensure_scraper_session_state(None, now=fresh_now)

            self.assertFalse(stale_state.browser_profile_dir.exists())
            self.assertFalse(stale_state.http_cookie_jar_path.exists())
            self.assertFalse(stale_state.metadata_path.exists())
            build_data.release_scraper_session_lock(current_state)

    def test_scraper_session_lock_is_active_for_new_unreadable_lock(self) -> None:
        with TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "slot-0.lock"
            lock_path.write_text("", encoding="utf-8")

            self.assertTrue(build_data.scraper_session_lock_is_active(lock_path))

    def test_scraper_session_lock_is_inactive_for_old_unreadable_lock(self) -> None:
        with TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "slot-0.lock"
            lock_path.write_text("", encoding="utf-8")
            old_timestamp = (
                datetime.now(UTC) - build_data.SCRAPER_SESSION_LOCK_WRITE_GRACE - timedelta(seconds=1)
            ).timestamp()
            os.utime(lock_path, (old_timestamp, old_timestamp))

            self.assertFalse(build_data.scraper_session_lock_is_active(lock_path))

    def test_scrape_google_list_url_passes_persistent_sessions_to_scraper(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        class DummySavedListResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "source_url": source.url,
                    "resolved_url": source.url,
                    "list_id": "tokyo",
                    "title": "Tokyo",
                    "description": None,
                    "places": [],
                }

        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                patch.object(build_data, "SCRAPER_STATE_DIR", state_dir),
                patch.dict("os.environ", {"GMAPS_SCRAPER_PROXY": "http://proxy.example:8080"}, clear=False),
                patch.object(build_data, "scrape_saved_list", return_value=DummySavedListResult()) as scrape,
            ):
                payload = build_data.scrape_google_list_url(source, headed=False)

        self.assertEqual(payload.title, "Tokyo")
        kwargs = scrape.call_args.kwargs
        self.assertTrue(kwargs["headless"])
        self.assertEqual(kwargs["collection_mode"], "curl")
        self.assertEqual(kwargs["browser_session"].proxy, "http://proxy.example:8080")
        self.assertEqual(kwargs["http_session"].proxy, "http://proxy.example:8080")
        self.assertIn("proxy-", str(kwargs["browser_session"].profile_dir))
        self.assertIn("proxy-", str(kwargs["http_session"].cookie_jar_path))

    def test_scrape_google_list_url_clears_session_after_curl_block_and_falls_back_to_browser(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        class DummySavedListResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "source_url": source.url,
                    "resolved_url": source.url,
                    "list_id": "tokyo",
                    "title": "Tokyo",
                    "description": None,
                    "places": [],
                }

        with TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                patch.object(build_data, "SCRAPER_STATE_DIR", state_dir),
                patch.object(
                    build_data,
                    "scrape_saved_list",
                    side_effect=[
                        build_data.ScrapeError("HTTP 429 Too Many Requests"),
                        DummySavedListResult(),
                    ],
                ) as scrape,
                patch.object(
                    build_data,
                    "clear_scraper_session_state",
                    wraps=build_data.clear_scraper_session_state,
                ) as clear_state,
            ):
                payload = build_data.scrape_google_list_url(source, headed=False)

        self.assertEqual(payload.title, "Tokyo")
        self.assertEqual(scrape.call_count, 2)
        self.assertEqual(scrape.call_args_list[0].kwargs["collection_mode"], "curl")
        self.assertEqual(scrape.call_args_list[1].kwargs["collection_mode"], "browser")
        clear_state.assert_called_once()

    def test_fetch_places_enrichment_uses_place_page_without_api_key(self) -> None:
        place = RawPlace(
            name="Jimbocho Den",
            address="Tokyo, Japan",
            maps_url="https://www.google.com/maps/place/Jimbocho+Den",
        )
        details = SimpleNamespace(
            source_url=place.maps_url,
            resolved_url="https://www.google.com/maps/place/Jimbocho+Den/@35.67,139.71,17z",
            name="Jimbocho Den",
            category="Japanese restaurant",
            rating=4.8,
            review_count=324,
            address="Tokyo, Japan",
            status="Closed · Opens 6 PM",
            website="http://www.jimbochoden.com/",
            phone="+81 3-6455-5433",
            plus_code="MPF7+73 Shibuya, Tokyo, Japan",
            description="Modern kaiseki.",
            limited_view=False,
        )

        with patch.object(build_data, "scrape_place", return_value=details) as scrape:
            entry = build_data.fetch_places_enrichment(
                place,
                api_key=None,
                strategy="scrape_then_api",
            )

        scrape.assert_called_once()
        self.assertEqual(entry.source, "google_maps_page")
        self.assertTrue(entry.matched)
        self.assertEqual(entry.place.display_name, "Jimbocho Den")
        self.assertEqual(entry.place.primary_type, "japanese_restaurant")
        self.assertEqual(entry.place.user_rating_count, 324)
        self.assertEqual(entry.place.business_status, "OPERATIONAL")

    def test_fetch_place_page_enrichment_rewrites_cid_url_before_scraping(self) -> None:
        place = RawPlace(
            name="Cantina OK!",
            address="Council Pl, Sydney NSW 2000, Australia",
            lat=-33.8702175,
            lng=151.2051413,
            maps_url="https://maps.google.com/?cid=7715422616180689913",
            cid="7715422616180689913",
        )
        details = SimpleNamespace(
            source_url=(
                "https://www.google.com/maps/search/?api=1"
                "&query=Cantina+OK%21%2C+Council+Pl%2C+Sydney+NSW+2000%2C+Australia"
            ),
            resolved_url="https://www.google.com/maps/place/Cantina+OK!/@-33.8702175,151.2051413,17z",
            name="Cantina OK!",
            category="Cocktail bar",
            address="Council Pl, Sydney NSW 2000, Australia",
            status="Open",
            limited_view=False,
        )

        with (
            patch.object(build_data, "current_scraper_proxy", return_value=None),
            patch.object(build_data, "build_scraper_sessions", return_value=(SimpleNamespace(), None, None)),
            patch.object(build_data, "record_scraper_session_use"),
            patch.object(build_data, "release_scraper_session_lock"),
            patch.object(build_data, "google_maps_place_collect_reviews", return_value=True),
            patch.object(build_data, "google_maps_place_collect_about", return_value=True),
            patch.object(build_data, "scrape_place", return_value=details) as scrape,
        ):
            entry = build_data.fetch_place_page_enrichment(place)

        scrape.assert_called_once_with(
            (
                "https://www.google.com/maps/search/?api=1"
                "&query=Cantina+OK%21%2C+Council+Pl%2C+Sydney+NSW+2000%2C+Australia"
                "&hl=en&gl=us"
            ),
            headless=True,
            browser_session=None,
            http_session=None,
            llm_fallback=build_data.build_place_llm_repairer(),
            llm_tasks=("dom_repair",),
            collect_reviews=True,
            collect_about=True,
        )
        self.assertTrue(entry.matched)
        self.assertEqual(entry.place.display_name, "Cantina OK!")

    def test_fetch_places_enrichment_uses_scrape_strategy_without_api_fallback(self) -> None:
        place = RawPlace(
            name="Jimbocho Den",
            address="Tokyo, Japan",
            maps_url="https://www.google.com/maps/place/Jimbocho+Den",
        )
        page_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-16T00:00:00+00:00",
            refresh_after="2026-04-19T00:00:00+00:00",
            source="google_maps_page",
            query="Jimbocho Den, Tokyo, Japan",
            matched=True,
            score=45,
            place=EnrichmentPlace(
                display_name="Jimbocho Den",
                formatted_address="Tokyo, Japan",
                google_maps_uri=place.maps_url,
                limited_view=True,
            ),
        )

        with (
            patch.object(build_data, "fetch_place_page_enrichment", return_value=page_entry) as page_fetch,
            patch.object(build_data, "fetch_places_api_enrichment") as api_fetch,
        ):
            entry = build_data.fetch_places_enrichment(
                place,
                api_key="test-key",
                strategy="scrape",
            )

        page_fetch.assert_called_once_with(
            place,
            city_name=None,
            country_name=None,
            google_place_id=None,
            signature_google_place_id=None,
            existing_entry=None,
            suppress_description=False,
            allow_identity_mismatch=False,
        )
        api_fetch.assert_not_called()
        self.assertIs(entry, page_entry)

    def test_fetch_places_enrichment_uses_api_strategy_without_scraping(self) -> None:
        place = RawPlace(
            name="Jimbocho Den",
            address="Tokyo, Japan",
            maps_url="https://www.google.com/maps/place/Jimbocho+Den",
        )
        api_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-16T00:00:00+00:00",
            refresh_after="2026-04-23T00:00:00+00:00",
            source="google_places_api",
            query="Jimbocho Den, Tokyo, Japan",
            matched=True,
            score=88,
            place=EnrichmentPlace(
                display_name="Jimbocho Den",
                formatted_address="Tokyo, Japan",
                google_maps_uri="https://maps.google.com/?cid=1",
                primary_type="restaurant",
                primary_type_display_name="Restaurant",
            ),
        )

        with (
            patch.object(build_data, "fetch_place_page_enrichment") as page_fetch,
            patch.object(build_data, "fetch_places_api_enrichment", return_value=api_entry) as api_fetch,
        ):
            entry = build_data.fetch_places_enrichment(
                place,
                api_key="test-key",
                strategy="api",
            )

        page_fetch.assert_not_called()
        api_fetch.assert_called_once_with(
            place,
            city_name=None,
            country_name=None,
            signature_google_place_id=None,
            api_key="test-key",
        )
        self.assertIs(entry, api_entry)

    def test_fetch_places_enrichment_fails_for_api_strategy_without_key(self) -> None:
        place = RawPlace(
            name="Jimbocho Den",
            address="Tokyo, Japan",
            maps_url="https://www.google.com/maps/place/Jimbocho+Den",
        )

        with (
            patch.object(build_data, "fetch_place_page_enrichment") as page_fetch,
            patch.object(build_data, "fetch_places_api_enrichment") as api_fetch,
        ):
            with self.assertRaisesRegex(RuntimeError, "GOOGLE_PLACES_API_KEY is required"):
                build_data.fetch_places_enrichment(
                    place,
                    api_key=None,
                    strategy="api",
                )

        page_fetch.assert_not_called()
        api_fetch.assert_not_called()

    def test_fetch_places_enrichment_falls_back_to_api_when_page_is_limited(self) -> None:
        place = RawPlace(
            name="Jimbocho Den",
            address="Tokyo, Japan",
            maps_url="https://www.google.com/maps/place/Jimbocho+Den",
        )
        page_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-16T00:00:00+00:00",
            refresh_after="2026-04-19T00:00:00+00:00",
            source="google_maps_page",
            query="Jimbocho Den, Tokyo, Japan",
            matched=True,
            score=45,
            place=EnrichmentPlace(
                display_name="Jimbocho Den",
                formatted_address="Tokyo, Japan",
                google_maps_uri=place.maps_url,
                limited_view=True,
                description="A polished kaiseki counter with seasonal menus.",
                about_sections=[{"title": "Amenities", "items": [{"label": "Restroom"}]}],
            ),
        )
        api_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-16T00:00:00+00:00",
            refresh_after="2026-04-23T00:00:00+00:00",
            source="google_places_api",
            query="Jimbocho Den, Tokyo, Japan",
            matched=True,
            score=88,
            place=EnrichmentPlace(
                display_name="Jimbocho Den",
                formatted_address="Tokyo, Japan",
                google_maps_uri="https://maps.google.com/?cid=1",
                primary_type="restaurant",
                primary_type_display_name="Restaurant",
            ),
        )

        with (
            patch.object(build_data, "fetch_place_page_enrichment", return_value=page_entry) as page_fetch,
            patch.object(build_data, "fetch_places_api_enrichment", return_value=api_entry) as api_fetch,
        ):
            entry = build_data.fetch_places_enrichment(
                place,
                api_key="test-key",
                strategy="scrape_then_api",
            )

        page_fetch.assert_called_once_with(
            place,
            city_name=None,
            country_name=None,
            google_place_id=None,
            signature_google_place_id=None,
            existing_entry=None,
            suppress_description=False,
            allow_identity_mismatch=False,
        )
        api_fetch.assert_called_once_with(
            place,
            city_name=None,
            country_name=None,
            signature_google_place_id=None,
            api_key="test-key",
        )
        self.assertIs(entry, api_entry)
        assert entry.place is not None
        self.assertEqual(
            entry.place.description,
            "A polished kaiseki counter with seasonal menus.",
        )
        self.assertEqual(
            entry.place.about_sections,
            [{"title": "Amenities", "items": [{"label": "Restroom"}]}],
        )
        self.assertEqual(entry.place.google_maps_uri, "https://maps.google.com/?cid=1")
        self.assertEqual(entry.merged_sources, ["google_maps_page", "google_places_api"])

    def test_fetch_places_enrichment_keeps_page_result_when_api_fallback_fails(self) -> None:
        place = RawPlace(
            name="Jimbocho Den",
            address="Tokyo, Japan",
            maps_url="https://www.google.com/maps/place/Jimbocho+Den",
        )
        page_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-16T00:00:00+00:00",
            refresh_after="2026-04-19T00:00:00+00:00",
            source="google_maps_page",
            query="Jimbocho Den, Tokyo, Japan",
            matched=True,
            score=45,
            place=EnrichmentPlace(
                display_name="Jimbocho Den",
                formatted_address="Tokyo, Japan",
                google_maps_uri=place.maps_url,
                limited_view=True,
            ),
        )
        api_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-16T00:00:00+00:00",
            refresh_after="2026-04-17T00:00:00+00:00",
            source="google_places_api",
            query="Jimbocho Den, Tokyo, Japan",
            matched=False,
            error="http_error:403",
        )

        with (
            patch.object(build_data, "fetch_place_page_enrichment", return_value=page_entry) as page_fetch,
            patch.object(build_data, "fetch_places_api_enrichment", return_value=api_entry) as api_fetch,
        ):
            entry = build_data.fetch_places_enrichment(
                place,
                api_key="test-key",
                strategy="scrape_then_api",
            )

        page_fetch.assert_called_once_with(
            place,
            city_name=None,
            country_name=None,
            google_place_id=None,
            signature_google_place_id=None,
            existing_entry=None,
            suppress_description=False,
            allow_identity_mismatch=False,
        )
        api_fetch.assert_called_once_with(
            place,
            city_name=None,
            country_name=None,
            signature_google_place_id=None,
            api_key="test-key",
        )
        self.assertIs(entry, page_entry)

    def test_normalize_place_page_enrichment_omits_maps_uri_for_url_only_result(self) -> None:
        details = SimpleNamespace(
            source_url="https://www.google.com/maps/place/Jimbocho+Den",
            resolved_url="https://www.google.com/maps/place/Jimbocho+Den/@35.67,139.71,17z",
            limited_view=False,
        )

        enrichment_place = build_data.normalize_place_page_enrichment(details)

        self.assertIsNone(enrichment_place.google_maps_uri)
        self.assertFalse(build_data.place_page_has_meaningful_enrichment(details, enrichment_place))

    def test_parallel_refresh_writes_each_snapshot_as_it_finishes(self) -> None:
        first_source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )
        second_source = SourceConfig(
            slug="madrid-spain",
            type="google_list_url",
            url="https://maps.app.goo.gl/madrid",
        )

        with TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir)
            first_raw_path = raw_dir / "tokyo-japan.json"

            class FakeFuture:
                def __init__(self, result):
                    self._result = result

                def result(self):
                    return self._result()

            first_future = FakeFuture(lambda: RawSavedList(title="Tokyo", places=[]))

            def second_result() -> RawSavedList:
                self.assertTrue(first_raw_path.exists())
                return RawSavedList(title="Madrid", places=[])

            second_future = FakeFuture(second_result)
            futures_by_slug = {
                first_source.slug: first_future,
                second_source.slug: second_future,
            }
            test_case = self

            class FakeExecutor:
                def __init__(self, max_workers):
                    self.max_workers = max_workers

                def submit(self, _fn, source, **kwargs):
                    test_case.assertEqual(kwargs["refresh_retries"], build_data.DEFAULT_REFRESH_RETRIES)
                    test_case.assertFalse(kwargs["headed"])
                    return futures_by_slug[source.slug]

                def shutdown(self, wait=True, cancel_futures=False):
                    return None

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "load_sources", return_value=[first_source, second_source]),
                patch.object(build_data, "ThreadPoolExecutor", FakeExecutor),
                patch.object(build_data, "as_completed", return_value=[first_future, second_future]),
            ):
                build_data.refresh_raw_sources(
                    headed=False,
                    force_refresh=True,
                    refresh_lists=[],
                    refresh_workers=2,
                    refresh_startup_jitter_seconds=0,
                )

            first_payload = RawSavedList.model_validate_json(first_raw_path.read_text(encoding="utf-8"))
            second_payload = RawSavedList.model_validate_json(
                (raw_dir / "madrid-spain.json").read_text(encoding="utf-8")
            )

        self.assertEqual(first_payload.title, "Tokyo")
        self.assertEqual(second_payload.title, "Madrid")

    def test_parallel_refresh_terminates_workers_on_interrupt(self) -> None:
        first_source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )
        second_source = SourceConfig(
            slug="madrid-spain",
            type="google_list_url",
            url="https://maps.app.goo.gl/madrid",
        )

        class FakeFuture:
            def result(self):
                raise KeyboardInterrupt()

        future = FakeFuture()
        executor_holder: dict[str, object] = {}

        class FakeExecutor:
            def __init__(self, max_workers):
                self.max_workers = max_workers
                self.terminated = False
                self.shutdown_calls: list[tuple[bool, bool]] = []
                executor_holder["executor"] = self

            def submit(self, _fn, source, **kwargs):
                return future

            def terminate_workers(self):
                self.terminated = True

            def shutdown(self, wait=True, cancel_futures=False):
                self.shutdown_calls.append((wait, cancel_futures))

        with (
            patch.object(build_data, "load_sources", return_value=[first_source, second_source]),
            patch.object(build_data, "cache_refresh_reason", return_value="forced"),
            patch.object(build_data, "ThreadPoolExecutor", FakeExecutor),
            patch.object(build_data, "as_completed", return_value=[future]),
            patch("builtins.print"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                build_data.refresh_raw_sources(
                    headed=False,
                    force_refresh=True,
                    refresh_lists=[],
                    refresh_workers=2,
                    refresh_startup_jitter_seconds=0,
                )

        executor = executor_holder["executor"]
        assert isinstance(executor, FakeExecutor)
        self.assertTrue(executor.terminated)
        self.assertIn((True, True), executor.shutdown_calls)

    def test_refresh_retries_transient_scrape_failure(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter") as startup_sleep,
            patch.object(build_data.time, "sleep") as retry_sleep,
            patch.object(
                build_data,
                "scrape_google_list_url",
                side_effect=[build_data.ScrapeError("timeout"), RawSavedList(title="Tokyo", places=[])],
            ) as scrape,
        ):
            payload = build_data.scrape_google_list_url_with_retries(
                source,
                headed=False,
                refresh_retries=2,
                refresh_retry_backoff_seconds=10,
                refresh_startup_jitter_seconds=8,
            )

        self.assertEqual(payload.title, "Tokyo")
        self.assertEqual(scrape.call_count, 2)
        self.assertEqual(startup_sleep.call_count, 2)
        retry_sleep.assert_called_once_with(10)

    def test_enrich_raw_sources_missing_only_skips_expired_entries(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[
                RawPlace(name="Expired Place", maps_url="https://maps.google.com/?cid=111", cid="111"),
                RawPlace(name="Missing Place", maps_url="https://maps.google.com/?cid=222", cid="222"),
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            seen_reasons: list[str] = []

            def fake_enrich_place_job(_slug, _place_id, _place_name, refresh_reason, _place_payload, **_kwargs):
                seen_reasons.append(refresh_reason)
                return EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query="Missing Place, Tokyo",
                    matched=True,
                    place=EnrichmentPlace(display_name="Missing Place"),
                )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(
                    build_data,
                    "cache_refresh_reason",
                    side_effect=lambda place, _cache_entry, **_kwargs: {
                        "Expired Place": "refresh-window-expired",
                        "Missing Place": "missing-cache-entry",
                    }[place.name],
                ),
                patch.object(build_data, "enrich_place_job", side_effect=fake_enrich_place_job),
            ):
                build_data.enrich_raw_sources(
                    force_refresh=False,
                    missing_only=True,
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

            self.assertEqual(seen_reasons, ["missing-cache-entry"])

    def test_refresh_cached_semantic_descriptions_updates_existing_cache_without_scrape(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[RawPlace(name="Tea House", maps_url="https://maps.google.com/?cid=111", cid="111")],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            (raw_dir / "taipei-taiwan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            cache_payload = {
                "cid:111": EnrichmentCacheEntry(
                    fetched_at="2026-05-01T00:00:00+00:00",
                    source="google_maps_page",
                    query="Tea House, Taipei, Taiwan",
                    matched=True,
                    place=EnrichmentPlace(
                        display_name="Tea House",
                        formatted_address="No. 12, Songgao Rd, Taipei City",
                        primary_type_display_name="Tea house",
                        review_topics=[{"label": "oolong", "count": 12}],
                    ),
                )
            }

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
            ):
                build_data.save_places_cache("taipei-taiwan", cache_payload)

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "fetch_places_enrichment") as fetch_places_enrichment,
                patch.object(
                    build_data,
                    "repair_semantic_enrichment_with_llm",
                    return_value={"description": "A quiet tea stop focused on oolong."},
                ) as repair,
            ):
                updated_count, reused_count, skipped_count = build_data.refresh_cached_semantic_descriptions()
                refreshed_cache = build_data.load_places_cache("taipei-taiwan")

        fetch_places_enrichment.assert_not_called()
        repair.assert_called_once()
        self.assertEqual((updated_count, reused_count, skipped_count), (1, 0, 0))
        self.assertEqual(
            refreshed_cache["cid:111"].place.semantic_description,
            "A quiet tea stop focused on oolong.",
        )
        self.assertIsNotNone(refreshed_cache["cid:111"].place.semantic_description_signature)

    def test_refresh_cached_semantic_descriptions_persists_stale_description_clear(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[RawPlace(name="Tea House", maps_url="https://maps.google.com/?cid=111", cid="111")],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            (raw_dir / "taipei-taiwan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            cache_payload = {
                "cid:111": EnrichmentCacheEntry(
                    fetched_at="2026-05-01T00:00:00+00:00",
                    source="google_maps_page",
                    query="Tea House, Taipei, Taiwan",
                    matched=True,
                    place=EnrichmentPlace(
                        display_name="Tea House",
                        formatted_address="No. 12, Songgao Rd, Taipei City",
                        primary_type_display_name="Tea house",
                        review_topics=[{"label": "oolong", "count": 12}],
                        semantic_description="A stale generated description.",
                        semantic_description_signature="old-signature",
                        semantic_source="llm",
                    ),
                )
            }

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
            ):
                build_data.save_places_cache("taipei-taiwan", cache_payload)

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "fetch_places_enrichment") as fetch_places_enrichment,
                patch.object(
                    build_data,
                    "repair_semantic_enrichment_with_llm",
                    return_value={},
                ) as repair,
            ):
                updated_count, reused_count, skipped_count = build_data.refresh_cached_semantic_descriptions()
                refreshed_cache = build_data.load_places_cache("taipei-taiwan")

        fetch_places_enrichment.assert_not_called()
        repair.assert_called_once()
        self.assertEqual((updated_count, reused_count, skipped_count), (1, 0, 0))
        refreshed_place = refreshed_cache["cid:111"].place
        assert refreshed_place is not None
        self.assertIsNone(refreshed_place.semantic_description)
        self.assertIsNone(refreshed_place.semantic_description_signature)
        self.assertIsNone(refreshed_place.semantic_source)

    def test_refresh_cached_semantic_descriptions_skips_places_with_manual_note(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[
                RawPlace(
                    name="Tea House",
                    note="Saved-list note that should not matter once a handwritten note exists.",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            overrides_dir = tmpdir_path / "overrides" / "places"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir(parents=True)
            overrides_dir.mkdir(parents=True)
            (raw_dir / "taipei-taiwan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            (overrides_dir / "taipei-taiwan.json").write_text(
                json.dumps(
                    {
                        "cid:111": {
                            "note": "Handwritten editor description.",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cache_payload = {
                "cid:111": EnrichmentCacheEntry(
                    fetched_at="2026-05-01T00:00:00+00:00",
                    source="google_maps_page",
                    query="Tea House, Taipei, Taiwan",
                    matched=True,
                    place=EnrichmentPlace(
                        display_name="Tea House",
                        formatted_address="No. 12, Songgao Rd, Taipei City",
                        primary_type_display_name="Tea house",
                        semantic_description="A stale generated description.",
                        semantic_description_signature="old-signature",
                        semantic_source="llm",
                    ),
                )
            }

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", overrides_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
            ):
                build_data.save_places_cache("taipei-taiwan", cache_payload)

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", overrides_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "fetch_places_enrichment") as fetch_places_enrichment,
                patch.object(build_data, "repair_semantic_enrichment_with_llm") as repair,
            ):
                updated_count, reused_count, skipped_count = build_data.refresh_cached_semantic_descriptions()
                refreshed_cache = build_data.load_places_cache("taipei-taiwan")

        fetch_places_enrichment.assert_not_called()
        repair.assert_not_called()
        self.assertEqual((updated_count, reused_count, skipped_count), (1, 0, 0))
        refreshed_place = refreshed_cache["cid:111"].place
        assert refreshed_place is not None
        self.assertIsNone(refreshed_place.semantic_description)
        self.assertIsNone(refreshed_place.semantic_description_signature)
        self.assertIsNone(refreshed_place.semantic_source)

    def test_refresh_cached_semantic_enrichment_updates_neighborhood_without_scrape(self) -> None:
        raw = RawSavedList(
            title="Taipei, Taiwan",
            places=[RawPlace(name="Ad Astra", maps_url="https://maps.google.com/?cid=111", cid="111")],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            (raw_dir / "taipei-taiwan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            cache_payload = {
                "cid:111": EnrichmentCacheEntry(
                    fetched_at="2026-05-01T00:00:00+00:00",
                    source="google_maps_page",
                    query="Ad Astra, Taipei, Taiwan",
                    matched=True,
                    place=EnrichmentPlace(
                        display_name="Ad Astra",
                        formatted_address=(
                            "No. 23, Lane 45, Section 2, Zhongshan N Rd, "
                            "Kangle Village, Zhongshan District, Taipei City, Taiwan 104"
                        ),
                        primary_type_display_name="Fine dining restaurant",
                    ),
                )
            }

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
            ):
                build_data.save_places_cache("taipei-taiwan", cache_payload)

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "fetch_places_enrichment") as fetch_places_enrichment,
                patch.object(
                    build_data,
                    "repair_semantic_enrichment_with_llm",
                    return_value={
                        "neighborhood": "Zhongshan",
                        "tags": ["fine-dining"],
                        "vibe_tags": ["special-occasion"],
                        "types": ["tasting-menu"],
                    },
                ) as repair,
            ):
                updated_count, reused_count, skipped_count = build_data.refresh_cached_semantic_enrichment(
                    enable_semantics=True,
                    enable_description=False,
                )
                refreshed_cache = build_data.load_places_cache("taipei-taiwan")

        fetch_places_enrichment.assert_not_called()
        repair.assert_called_once()
        self.assertEqual((updated_count, reused_count, skipped_count), (1, 0, 0))
        self.assertEqual(refreshed_cache["cid:111"].place.semantic_neighborhood, "Zhongshan")
        self.assertEqual(refreshed_cache["cid:111"].place.semantic_tags, ["fine-dining"])

    def test_refresh_cached_semantic_enrichment_with_place_selector_preserves_unselected_guide_cache(self) -> None:
        tokyo_raw = RawSavedList(
            title="Tokyo",
            places=[RawPlace(name="Current Tokyo Place", maps_url="https://maps.google.com/?cid=111", cid="111")],
        )
        taipei_raw = RawSavedList(
            title="Taipei",
            places=[RawPlace(name="Selected Taipei Place", maps_url="https://maps.google.com/?cid=222", cid="222")],
        )
        stale_tokyo_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Deleted Tokyo Place",
            matched=True,
            place=EnrichmentPlace(display_name="Deleted Tokyo Place"),
        )
        taipei_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Selected Taipei Place",
            matched=True,
            place=EnrichmentPlace(display_name="Selected Taipei Place"),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(tokyo_raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            (raw_dir / "taipei-taiwan.json").write_text(
                json.dumps(taipei_raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
            ):
                build_data.save_places_cache("tokyo-japan", {"cid:999": stale_tokyo_entry})
                build_data.save_places_cache("taipei-taiwan", {"cid:222": taipei_entry})
                updated_count, reused_count, skipped_count = build_data.refresh_cached_semantic_enrichment(
                    enable_semantics=False,
                    enable_description=False,
                    place_selectors=["taipei-taiwan:cid:222"],
                )
                tokyo_cache = build_data.load_places_cache("tokyo-japan")

        self.assertEqual((updated_count, reused_count, skipped_count), (0, 1, 0))
        self.assertEqual(list(tokyo_cache), ["cid:999"])
        self.assertEqual(tokyo_cache["cid:999"].query, "Deleted Tokyo Place")

    def test_enrich_raw_sources_prioritizes_missing_entries_before_expired_refreshes(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[
                RawPlace(name="Expired Place", maps_url="https://maps.google.com/?cid=111", cid="111"),
                RawPlace(name="Missing Place", maps_url="https://maps.google.com/?cid=222", cid="222"),
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            seen_reasons: list[str] = []

            def fake_enrich_place_job(_slug, _place_id, _place_name, refresh_reason, _place_payload, **_kwargs):
                seen_reasons.append(refresh_reason)
                return EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query="Tokyo",
                    matched=True,
                    place=EnrichmentPlace(display_name="Tokyo"),
                )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(
                    build_data,
                    "cache_refresh_reason",
                    side_effect=lambda place, _cache_entry, **_kwargs: {
                        "Expired Place": "refresh-window-expired",
                        "Missing Place": "missing-cache-entry",
                    }[place.name],
                ),
                patch.object(build_data, "enrich_place_job", side_effect=fake_enrich_place_job),
            ):
                build_data.enrich_raw_sources(
                    force_refresh=False,
                    missing_only=False,
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

            self.assertEqual(
                seen_reasons,
                ["missing-cache-entry", "refresh-window-expired"],
            )

    def test_enrich_raw_sources_refreshes_when_google_place_id_override_changes(self) -> None:
        place = RawPlace(
            name="Taipei 101",
            maps_url="https://maps.google.com/?cid=3765761221328423815",
            cid="3765761221328423815",
        )
        raw = RawSavedList(title="Taipei, Taiwan", places=[place])

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            overrides_dir = tmpdir_path / "overrides" / "places"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir(parents=True)
            overrides_dir.mkdir(parents=True)
            cache_dir.mkdir()
            (raw_dir / "taipei-taiwan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            (overrides_dir / "taipei-taiwan.json").write_text(
                json.dumps(
                    {
                        "cid:3765761221328423815": {
                            "google_place_id": "ChIJraeA2rarQjQRPBBjyR3RxKw",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stale_signature_entry = EnrichmentCacheEntry(
                fetched_at="2026-05-01T00:00:00+00:00",
                refresh_after="2099-01-01T00:00:00+00:00",
                source="google_maps_page",
                query="Taipei 101, Taipei, Taiwan",
                input_signature=build_data.enrichment_input_signature(
                    place,
                    city_name="Taipei",
                    country_name="Taiwan",
                ),
                matched=True,
                score=build_data.STRONG_MATCH_SCORE,
                place=EnrichmentPlace(display_name="Taipei 101"),
            )

            seen_reasons: list[str] = []

            def fake_enrich_place_job(_slug, _place_id, _place_name, refresh_reason, _place_payload, **_kwargs):
                seen_reasons.append(refresh_reason)
                return stale_signature_entry

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", overrides_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
            ):
                build_data.save_places_cache("taipei-taiwan", {"cid:3765761221328423815": stale_signature_entry})

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACE_OVERRIDES_DIR", overrides_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(build_data, "enrich_place_job", side_effect=fake_enrich_place_job),
            ):
                build_data.enrich_raw_sources(
                    force_refresh=False,
                    missing_only=False,
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

        self.assertEqual(seen_reasons, ["raw-place-changed"])

    def test_enrich_raw_sources_filters_to_selected_place(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[
                RawPlace(name="First Place", maps_url="https://maps.google.com/?cid=111", cid="111"),
                RawPlace(name="Second Place", maps_url="https://maps.google.com/?cid=222", cid="222"),
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            seen_places: list[str] = []

            def fake_enrich_place_job(_slug, _place_id, place_name, _refresh_reason, _place_payload, **_kwargs):
                seen_places.append(place_name)
                return EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query=place_name,
                    matched=True,
                    place=EnrichmentPlace(display_name=place_name),
                )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(build_data, "enrich_place_job", side_effect=fake_enrich_place_job),
            ):
                build_data.enrich_raw_sources(
                    force_refresh=True,
                    place_selectors=["222"],
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

            self.assertEqual(seen_places, ["Second Place"])

    def test_enrich_raw_sources_with_place_selector_preserves_unselected_guide_cache(self) -> None:
        tokyo_raw = RawSavedList(
            title="Tokyo",
            places=[RawPlace(name="Current Tokyo Place", maps_url="https://maps.google.com/?cid=111", cid="111")],
        )
        taipei_raw = RawSavedList(
            title="Taipei",
            places=[RawPlace(name="Selected Taipei Place", maps_url="https://maps.google.com/?cid=222", cid="222")],
        )
        stale_tokyo_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Deleted Tokyo Place",
            matched=True,
            place=EnrichmentPlace(display_name="Deleted Tokyo Place"),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(tokyo_raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            (raw_dir / "taipei-taiwan.json").write_text(
                json.dumps(taipei_raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            seen_places: list[str] = []

            def fake_enrich_place_job(_slug, _place_id, place_name, _refresh_reason, _place_payload, **_kwargs):
                seen_places.append(place_name)
                return EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query=place_name,
                    matched=True,
                    place=EnrichmentPlace(display_name=place_name),
                )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(build_data, "enrich_place_job", side_effect=fake_enrich_place_job),
            ):
                build_data.save_places_cache("tokyo-japan", {"cid:999": stale_tokyo_entry})
                build_data.enrich_raw_sources(
                    force_refresh=True,
                    place_selectors=["taipei-taiwan:cid:222"],
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )
                tokyo_cache = build_data.load_places_cache("tokyo-japan")

        self.assertEqual(seen_places, ["Selected Taipei Place"])
        self.assertEqual(list(tokyo_cache), ["cid:999"])
        self.assertEqual(tokyo_cache["cid:999"].query, "Deleted Tokyo Place")

    def test_enrich_raw_sources_matches_cid_derived_from_maps_url(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[
                RawPlace(name="First Place", maps_url="https://maps.google.com/?cid=111"),
                RawPlace(
                    name="Second Place",
                    google_id="/g/second",
                    maps_url="https://maps.google.com/?cid=222",
                ),
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            seen_places: list[str] = []

            def fake_enrich_place_job(_slug, _place_id, place_name, _refresh_reason, _place_payload, **_kwargs):
                seen_places.append(place_name)
                return EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query=place_name,
                    matched=True,
                    place=EnrichmentPlace(display_name=place_name),
                )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(build_data, "enrich_place_job", side_effect=fake_enrich_place_job),
            ):
                build_data.enrich_raw_sources(
                    force_refresh=True,
                    place_selectors=["cid:222"],
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

            self.assertEqual(seen_places, ["Second Place"])

    def test_enrich_raw_sources_matches_maps_place_token_derived_from_maps_url(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[
                RawPlace(
                    name="First Place",
                    google_id="/g/first",
                    maps_url="https://maps.google.com/?q=0x111:0xaaa",
                ),
                RawPlace(
                    name="Second Place",
                    google_id="/g/second",
                    maps_url="https://maps.google.com/?q=0x222:0xbbb",
                ),
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            seen_places: list[str] = []

            def fake_enrich_place_job(_slug, _place_id, place_name, _refresh_reason, _place_payload, **_kwargs):
                seen_places.append(place_name)
                return EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query=place_name,
                    matched=True,
                    place=EnrichmentPlace(display_name=place_name),
                )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(build_data, "enrich_place_job", side_effect=fake_enrich_place_job),
            ):
                build_data.enrich_raw_sources(
                    force_refresh=True,
                    place_selectors=["gms:0x222:0xbbb"],
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

            self.assertEqual(seen_places, ["Second Place"])

    def test_enrich_raw_sources_does_not_match_bare_guide_slug_as_place(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[RawPlace(name="First Place", maps_url="https://maps.google.com/?cid=111", cid="111")],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "No configured place matched: tokyo-japan"):
                    build_data.enrich_raw_sources(
                        force_refresh=True,
                        place_selectors=["tokyo-japan"],
                        refresh_workers=1,
                        refresh_startup_jitter_seconds=0,
                    )

    def test_enrich_raw_sources_matches_explicit_guide_slug_selector(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[
                RawPlace(name="First Place", maps_url="https://maps.google.com/?cid=111", cid="111"),
                RawPlace(name="Second Place", maps_url="https://maps.google.com/?cid=222", cid="222"),
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            seen_places: list[str] = []

            def fake_enrich_place_job(_slug, _place_id, place_name, _refresh_reason, _place_payload, **_kwargs):
                seen_places.append(place_name)
                return EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query=place_name,
                    matched=True,
                    place=EnrichmentPlace(display_name=place_name),
                )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(build_data, "enrich_place_job", side_effect=fake_enrich_place_job),
            ):
                build_data.enrich_raw_sources(
                    force_refresh=True,
                    place_selectors=["guide-slug:tokyo-japan"],
                    refresh_workers=1,
                    refresh_startup_jitter_seconds=0,
                )

            self.assertEqual(seen_places, ["First Place", "Second Place"])

    def test_enrich_raw_sources_parallel_writes_cache_incrementally(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[
                RawPlace(
                    name="First Place",
                    address="1 Example St, Tokyo",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
                RawPlace(
                    name="Second Place",
                    address="2 Example St, Tokyo",
                    maps_url="https://maps.google.com/?cid=222",
                    cid="222",
                ),
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            first_place_id = build_data.stable_place_id(raw.places[0])
            second_place_id = build_data.stable_place_id(raw.places[1])

            class FakeFuture:
                def __init__(self, result):
                    self._result = result

                def result(self):
                    return self._result()

            first_future = FakeFuture(
                lambda: EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:00+00:00",
                    query="First Place, Tokyo",
                    matched=True,
                    place=EnrichmentPlace(display_name="First Place"),
                )
            )

            def second_result() -> EnrichmentCacheEntry:
                cache_payload = build_data.load_places_cache("tokyo-japan")
                self.assertIn(first_place_id, cache_payload)
                self.assertNotIn(second_place_id, cache_payload)
                return EnrichmentCacheEntry(
                    fetched_at="2026-04-20T00:00:01+00:00",
                    query="Second Place, Tokyo",
                    matched=True,
                    place=EnrichmentPlace(display_name="Second Place"),
                )

            second_future = FakeFuture(second_result)
            futures_by_cid = {
                "111": first_future,
                "222": second_future,
            }
            test_case = self

            class FakeExecutor:
                def __init__(self, max_workers):
                    test_case.assertEqual(max_workers, 2)

                def submit(self, _fn, _slug, _place_id, _place_name, _refresh_reason, place_payload, **kwargs):
                    test_case.assertIsNone(kwargs["api_key"])
                    test_case.assertEqual(kwargs["refresh_startup_jitter_seconds"], 0)
                    return futures_by_cid[place_payload["cid"]]

                def shutdown(self, wait=True, cancel_futures=False):
                    return None

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(build_data, "ThreadPoolExecutor", FakeExecutor),
                patch.object(build_data, "as_completed", return_value=[first_future, second_future]),
            ):
                build_data.enrich_raw_sources(
                    force_refresh=True,
                    refresh_workers=2,
                    refresh_startup_jitter_seconds=0,
                )

                cache_payload = build_data.load_places_cache("tokyo-japan")
            self.assertIn(first_place_id, cache_payload)
            self.assertIn(second_place_id, cache_payload)
            self.assertEqual(cache_payload[first_place_id].place.display_name, "First Place")
            self.assertEqual(cache_payload[second_place_id].place.display_name, "Second Place")

    def test_place_input_signature_prefers_stable_identifiers(self) -> None:
        first_place = RawPlace(
            name="Old Name",
            address="Old Address",
            maps_url="https://maps.google.com/?cid=111",
            cid="111",
            lat=1.0,
            lng=2.0,
        )
        second_place = RawPlace(
            name="New Name",
            address="New Address",
            maps_url="https://www.google.com/maps/search/?api=1&query=New+Name",
            cid="111",
            lat=9.0,
            lng=10.0,
        )

        self.assertEqual(
            build_data.place_input_signature(first_place),
            build_data.place_input_signature(second_place),
        )

    def test_enrichment_input_signature_includes_locality_bias_context(self) -> None:
        place = RawPlace(
            name="Bilmonte",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Bilmonte",
            cid="1343378048703211865",
            lat=41.3894089,
            lng=2.1636435,
        )

        generic_signature = build_data.enrichment_input_signature(place)
        barcelona_signature = build_data.enrichment_input_signature(
            place,
            city_name="Barcelona",
            country_name="Spain",
        )
        madrid_signature = build_data.enrichment_input_signature(
            place,
            city_name="Madrid",
            country_name="Spain",
        )

        self.assertNotEqual(generic_signature, barcelona_signature)
        self.assertNotEqual(barcelona_signature, madrid_signature)

    def test_enrichment_input_signature_includes_scraper_policy(self) -> None:
        place = RawPlace(
            name="Bilmonte",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Bilmonte",
            cid="1343378048703211865",
            lat=41.3894089,
            lng=2.1636435,
        )

        with (
            patch.object(build_data, "google_maps_place_llm_repair_mode", return_value="off"),
            patch.object(build_data, "google_maps_place_collect_reviews", return_value=False),
            patch.object(build_data, "google_maps_place_collect_about", return_value=False),
        ):
            minimal_signature = build_data.enrichment_input_signature(
                place,
                city_name="Barcelona",
                country_name="Spain",
            )

        with (
            patch.object(build_data, "google_maps_place_llm_repair_mode", return_value="dom_then_translation"),
            patch.object(build_data, "google_maps_place_collect_reviews", return_value=True),
            patch.object(build_data, "google_maps_place_collect_about", return_value=True),
        ):
            richer_signature = build_data.enrichment_input_signature(
                place,
                city_name="Barcelona",
                country_name="Spain",
            )

        self.assertNotEqual(minimal_signature, richer_signature)

    def test_cache_refresh_reason_invalidates_legacy_unbiased_name_only_search_entry(self) -> None:
        place = RawPlace(
            name="Bilmonte",
            address=None,
            maps_url="https://www.google.com/maps/search/?api=1&query=Bilmonte",
            cid="1343378048703211865",
            lat=41.3894089,
            lng=2.1636435,
        )
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-19T16:47:26.628975+00:00",
            refresh_after="2026-05-19T16:47:26.628975+00:00",
            source="google_maps_page",
            query="Bilmonte",
            input_signature=build_data.place_input_signature(place),
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                display_name="Bilmonte",
                formatted_address="30 Great Windmill St, London W1D 7LW, United Kingdom",
            ),
        )

        refresh_reason = build_data.cache_refresh_reason(
            place,
            cache_entry,
            city_name="Barcelona",
            country_name="Spain",
        )

        self.assertEqual(refresh_reason, "raw-place-changed")

    def test_cache_refresh_reason_retries_old_real_place_without_photo_url(self) -> None:
        place = RawPlace(
            name="Tokyo Metropolitan Government Building",
            address=None,
            maps_url="https://maps.google.com/?cid=6924439272315041697",
            cid="6924439272315041697",
            lat=35.6894807,
            lng=139.6916863,
        )
        cache_entry = EnrichmentCacheEntry(
            fetched_at=(
                datetime.now(UTC)
                - build_data.PHOTOLESS_REAL_PLACE_CACHE_TTL
                - timedelta(hours=1)
            ).isoformat(),
            refresh_after=(datetime.now(UTC) + timedelta(days=6)).isoformat(),
            source="google_maps_page",
            query="Tokyo Metropolitan Government Building, Tokyo, Japan",
            input_signature=build_data.enrichment_input_signature(
                place,
                city_name="Tokyo",
                country_name="Japan",
            ),
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                display_name="Tokyo Metropolitan Government Building",
                formatted_address="2 Chome-8-1 Nishishinjuku, Shinjuku City, Tokyo 160-0023, Japan",
                google_place_id="ChIJ-d5eaQCNGGARG9gXAP513iw",
                google_maps_uri=(
                    "https://www.google.com/maps/place/Tokyo+Metropolitan+Government+Building/"
                    "@35.6895569,139.6919911,17z"
                ),
            ),
        )

        refresh_reason = build_data.cache_refresh_reason(
            place,
            cache_entry,
            city_name="Tokyo",
            country_name="Japan",
        )

        self.assertEqual(refresh_reason, "missing-photo-url")

    def test_cache_refresh_reason_keeps_fresh_real_place_without_photo_url(self) -> None:
        place = RawPlace(
            name="Zass",
            address=None,
            maps_url="https://maps.google.com/?cid=1385867432453075065",
            cid="1385867432453075065",
            lat=40.6231077,
            lng=14.5037538,
        )
        cache_entry = EnrichmentCacheEntry(
            fetched_at=datetime.now(UTC).isoformat(),
            refresh_after=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            source="google_maps_page",
            query="Zass, Amalfi Coast, Italy",
            input_signature=build_data.enrichment_input_signature(
                place,
                city_name="Amalfi Coast",
                country_name="Italy",
            ),
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                display_name="Zass",
                formatted_address="Via Laurito, 2, 84017 Positano SA, Italy",
                google_place_id="ChIJTTegVVqXOxMRX4nA4MSAv-I",
            ),
        )

        refresh_reason = build_data.cache_refresh_reason(
            place,
            cache_entry,
            city_name="Amalfi Coast",
            country_name="Italy",
        )

        self.assertIsNone(refresh_reason)

    def test_cache_refresh_reason_handles_naive_photo_retry_timestamp(self) -> None:
        place = RawPlace(
            name="Tokyo Metropolitan Government Building",
            address=None,
            maps_url="https://maps.google.com/?cid=6924439272315041697",
            cid="6924439272315041697",
            lat=35.6894807,
            lng=139.6916863,
        )
        naive_fetched_at = (
            datetime.now(UTC)
            - build_data.PHOTOLESS_REAL_PLACE_CACHE_TTL
            - timedelta(hours=1)
        ).replace(tzinfo=None)
        cache_entry = EnrichmentCacheEntry(
            fetched_at=naive_fetched_at.isoformat(),
            refresh_after=(datetime.now(UTC) + timedelta(days=6)).isoformat(),
            source="google_maps_page",
            query="Tokyo Metropolitan Government Building, Tokyo, Japan",
            input_signature=build_data.enrichment_input_signature(
                place,
                city_name="Tokyo",
                country_name="Japan",
            ),
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                display_name="Tokyo Metropolitan Government Building",
                google_place_id="ChIJ-d5eaQCNGGARG9gXAP513iw",
            ),
        )

        refresh_reason = build_data.cache_refresh_reason(
            place,
            cache_entry,
            city_name="Tokyo",
            country_name="Japan",
        )

        self.assertEqual(refresh_reason, "missing-photo-url")

    def test_cache_refresh_reason_does_not_photo_retry_api_only_entry(self) -> None:
        place = RawPlace(
            name="Michael's",
            address="9777 Las Vegas Blvd S, Las Vegas, NV 89183, USA",
            maps_url="https://maps.google.com/?cid=3558656038997305861",
            cid="3558656038997305861",
            lat=36.012782,
            lng=-115.175648,
        )
        cache_entry = EnrichmentCacheEntry(
            fetched_at=(
                datetime.now(UTC)
                - build_data.PHOTOLESS_REAL_PLACE_CACHE_TTL
                - timedelta(hours=1)
            ).isoformat(),
            refresh_after=(datetime.now(UTC) + timedelta(days=6)).isoformat(),
            source="google_places_api",
            query="Michael's, Las Vegas",
            input_signature=build_data.enrichment_input_signature(
                place,
                city_name="Las Vegas",
                country_name="USA",
            ),
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                display_name="Michael's",
                formatted_address="9777 Las Vegas Blvd S, Las Vegas, NV 89183, USA",
                google_place_id="ChIJwZX119rOyIAR5Tx-nEKkSgE",
                google_place_resource_name="places/ChIJwZX119rOyIAR5Tx-nEKkSgE",
            ),
        )

        refresh_reason = build_data.cache_refresh_reason(
            place,
            cache_entry,
            city_name="Las Vegas",
            country_name="USA",
        )

        self.assertIsNone(refresh_reason)

    def test_cache_refresh_reason_retries_merged_api_page_entry_without_photo_url(self) -> None:
        place = RawPlace(
            name="Tokyo Metropolitan Government Building",
            address=None,
            maps_url="https://maps.google.com/?cid=6924439272315041697",
            cid="6924439272315041697",
            lat=35.6894807,
            lng=139.6916863,
        )
        cache_entry = EnrichmentCacheEntry(
            fetched_at=(
                datetime.now(UTC)
                - build_data.PHOTOLESS_REAL_PLACE_CACHE_TTL
                - timedelta(hours=1)
            ).isoformat(),
            refresh_after=(datetime.now(UTC) + timedelta(days=6)).isoformat(),
            source="google_places_api",
            merged_sources=["google_maps_page", "google_places_api"],
            query="Tokyo Metropolitan Government Building, Tokyo, Japan",
            input_signature=build_data.enrichment_input_signature(
                place,
                city_name="Tokyo",
                country_name="Japan",
            ),
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                display_name="Tokyo Metropolitan Government Building",
                formatted_address="2 Chome-8-1 Nishishinjuku, Shinjuku City, Tokyo 160-0023, Japan",
                google_place_id="ChIJ-d5eaQCNGGARG9gXAP513iw",
                google_place_resource_name="places/ChIJ-d5eaQCNGGARG9gXAP513iw",
                google_maps_uri=(
                    "https://www.google.com/maps/place/Tokyo+Metropolitan+Government+Building/"
                    "@35.6895569,139.6919911,17z"
                ),
            ),
        )

        refresh_reason = build_data.cache_refresh_reason(
            place,
            cache_entry,
            city_name="Tokyo",
            country_name="Japan",
        )

        self.assertEqual(refresh_reason, "missing-photo-url")

    def test_cache_refresh_ttl_uses_short_retry_for_real_place_without_photo_url(self) -> None:
        cache_entry = EnrichmentCacheEntry(
            fetched_at=datetime.now(UTC).isoformat(),
            source="google_maps_page",
            query="Tokyo Metropolitan Government Building, Tokyo, Japan",
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                display_name="Tokyo Metropolitan Government Building",
                google_place_id="ChIJ-d5eaQCNGGARG9gXAP513iw",
            ),
        )

        self.assertEqual(
            build_data.cache_refresh_ttl(cache_entry),
            build_data.PHOTOLESS_REAL_PLACE_CACHE_TTL,
        )

    def test_build_places_sqlite_signature_changes_when_version_or_schema_changes(self) -> None:
        raw = RawSavedList(
            configured_source_type="google_list_url",
            fetched_at="2026-04-20T00:00:00+00:00",
            title="Tokyo",
            places=[
                RawPlace(
                    name="Coffee House",
                    address="1 Shibuya, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0], source_type=raw.configured_source_type)
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id=place_id,
                    name="Coffee House",
                    maps_url="https://www.google.com/maps/search/?api=1&query=Coffee+House",
                    status="active",
                )
            ],
        )
        baseline = build_data.build_places_sqlite_signature(
            raw_lists={"tokyo-japan": raw},
            guides=[guide],
            enrichment_caches={"tokyo-japan": {}},
        )
        changed_guide = guide.model_copy(update={"title": "Tokyo Updated"})
        changed_raw = raw.model_copy(update={"title": "Tokyo Updated"})
        unchanged_cache_signature = build_data.build_places_sqlite_signature(
            raw_lists={"tokyo-japan": changed_raw},
            guides=[changed_guide],
            enrichment_caches={"tokyo-japan": {}},
        )

        with patch.object(build_data, "PLACES_SQLITE_SIGNATURE_VERSION", build_data.PLACES_SQLITE_SIGNATURE_VERSION + 1):
            bumped_version = build_data.build_places_sqlite_signature(
                raw_lists={"tokyo-japan": raw},
                guides=[guide],
                enrichment_caches={"tokyo-japan": {}},
            )

        with patch.object(build_data, "PLACES_SQLITE_SCHEMA_SQL", build_data.PLACES_SQLITE_SCHEMA_SQL + "\n-- test change"):
            changed_schema = build_data.build_places_sqlite_signature(
                raw_lists={"tokyo-japan": raw},
                guides=[guide],
                enrichment_caches={"tokyo-japan": {}},
            )

        self.assertEqual(baseline, unchanged_cache_signature)
        self.assertNotEqual(baseline, bumped_version)
        self.assertNotEqual(baseline, changed_schema)

    def test_place_photo_signature_state_tracks_photo_digest_not_mtime(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            photo_path = tmpdir_path / "public" / "place-photos" / "cid-111-photo.webp"
            photo_path.parent.mkdir(parents=True, exist_ok=True)
            first_bytes = b"first-photo"
            second_bytes = b"other-photo"
            fixed_mtime = 1_700_000_000

            photo_path.write_bytes(first_bytes)
            os.utime(photo_path, (fixed_mtime, fixed_mtime))

            with patch.object(build_data, "ROOT", tmpdir_path):
                first_state = build_data.place_photo_signature_state(
                    "/place-photos/cid-111-photo.webp",
                    source_photo_url="https://images.example/coffee-house.webp",
                    metadata_cache={},
                )

                photo_path.write_bytes(second_bytes)
                os.utime(photo_path, (fixed_mtime, fixed_mtime))

                second_state = build_data.place_photo_signature_state(
                    "/place-photos/cid-111-photo.webp",
                    source_photo_url="https://images.example/coffee-house.webp",
                    metadata_cache={},
                )

            self.assertNotEqual(first_state, second_state)
            self.assertEqual(first_state[1], hashlib.sha256(first_bytes).hexdigest())
            self.assertEqual(second_state[1], hashlib.sha256(second_bytes).hexdigest())

    def test_rebuild_places_sqlite_writes_compact_cache_artifact(self) -> None:
        raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Tokyo",
            places=[
                RawPlace(
                    name="Coffee House",
                    address="1 Shibuya, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                    lat=35.6595,
                    lng=139.7005,
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0], source_type=raw.configured_source_type)
        photo_public_path = "/place-photos/cid-111-photo.webp"
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            last_verified_at="2026-04-20T00:00:00+00:00",
            refresh_after="2026-04-27T00:00:00+00:00",
            source="google_maps_page",
            query="Coffee House, 1 Shibuya, Tokyo, Japan",
            input_signature=build_data.place_input_signature(raw.places[0]),
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                google_place_id="ChIJ123",
                display_name="Coffee House",
                formatted_address="1 Shibuya, Tokyo, Japan",
                google_maps_uri="https://www.google.com/maps/place/Coffee+House",
                rating=4.8,
                user_rating_count=240,
                primary_type="cafe",
                primary_type_display_name="Cafe",
                types=["cafe", "food"],
                main_photo_url="https://images.example/coffee-house.webp",
                photo_url="https://images.example/coffee-house.webp",
                reviews=[
                    {
                        "author": "A",
                        "rating": 5,
                        "relative_time": "1 week ago",
                        "text": "Careful espresso and a quiet room.",
                    }
                ],
                about_sections=[
                    {
                        "title": "Service options",
                        "items": [{"label": "Dine-in", "aria_label": "Serves dine-in"}],
                    }
                ],
            ),
        )
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            featured_place_ids=[place_id],
            best_hit_place_ids=[place_id],
            top_categories=["cafe"],
            places=[
                NormalizedPlace(
                    id=place_id,
                    name="Coffee House",
                    address="1 Shibuya, Tokyo, Japan",
                    lat=35.6595,
                    lng=139.7005,
                    maps_url="https://www.google.com/maps/search/?api=1&query=Coffee+House",
                    cid="111",
                    google_place_id="ChIJ123",
                    rating=4.8,
                    user_rating_count=240,
                    primary_category="cafe",
                    tags=["coffee"],
                    vibe_tags=["cozy"],
                    neighborhood="Shibuya",
                    note="Great espresso",
                    why_recommended="Morning stop",
                    main_photo_path=photo_public_path,
                    top_pick=True,
                    hidden=False,
                    manual_rank=3,
                    status="active",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "cache" / "places.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)

            with (
                patch.object(build_data, "ROOT", tmpdir_path),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
            ):
                build_data.rebuild_places_sqlite(
                    raw_lists={"tokyo-japan": raw},
                    guides=[guide],
                    enrichment_caches={"tokyo-japan": {place_id: cache_entry}},
                )

            connection = sqlite3.connect(db_path)
            try:
                tables = {
                    name
                    for (name,) in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertEqual(tables, {"build_metadata", "guide_enrichment_cache"})

                indexes = {
                    name
                    for (name,) in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'"
                    )
                }
                self.assertEqual(indexes, {"idx_guide_enrichment_cache_place_id"})

                sqlite_cache_entry = connection.execute(
                    """
                    SELECT query, input_signature, cache_json
                    FROM guide_enrichment_cache
                    WHERE guide_slug = ? AND place_id = ?
                    """,
                    ("tokyo-japan", place_id),
                ).fetchone()
                self.assertIsNotNone(sqlite_cache_entry)
                assert sqlite_cache_entry is not None
                self.assertEqual(
                    sqlite_cache_entry[:2],
                    (
                        "Coffee House, 1 Shibuya, Tokyo, Japan",
                        build_data.place_input_signature(raw.places[0]),
                    ),
                )
                cache_payload = json.loads(sqlite_cache_entry[2])
                place_payload = cache_payload["place"]
                self.assertEqual(place_payload["display_name"], "Coffee House")
                self.assertEqual(place_payload["reviews"], cache_entry.place.reviews)
                self.assertEqual(place_payload["about_sections"], cache_entry.place.about_sections)
                self.assertNotIn("error", cache_payload)
                self.assertNotIn("error_body", cache_payload)
                self.assertNotIn("address_parts", place_payload)
                build_metadata = connection.execute(
                    "SELECT value FROM build_metadata WHERE key = ?",
                    (build_data.PLACES_SQLITE_BUILD_METADATA_KEY,),
                ).fetchone()
                self.assertIsNotNone(build_metadata)
                assert build_metadata is not None
                self.assertRegex(build_metadata[0], r"^[0-9a-f]{64}$")
            finally:
                connection.close()

    def test_rebuild_places_sqlite_rejects_empty_payload_over_existing_nonempty_guide(self) -> None:
        raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Tokyo",
            places=[
                RawPlace(
                    name="Coffee House",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0], source_type=raw.configured_source_type)
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id=place_id,
                    name="Coffee House",
                    maps_url="https://www.google.com/maps/search/?api=1&query=Coffee+House",
                    status="active",
                )
            ],
        )
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Coffee House",
            matched=True,
            place=EnrichmentPlace(display_name="Coffee House"),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "cache" / "places.sqlite"

            with patch.object(build_data, "PLACES_SQLITE_PATH", db_path):
                build_data.save_places_cache("tokyo-japan", {place_id: cache_entry})
                with self.assertRaisesRegex(RuntimeError, "tokyo-japan"):
                    build_data.rebuild_places_sqlite(
                        raw_lists={"tokyo-japan": raw},
                        guides=[guide],
                        enrichment_caches={"tokyo-japan": {}},
                    )
                loaded_payload = build_data.load_places_cache("tokyo-japan")

        self.assertEqual(list(loaded_payload), [place_id])

    def test_rebuild_places_sqlite_rejects_empty_payload_after_pruning_stale_ids(self) -> None:
        stale_raw = RawPlace(
            name="Old Coffee House",
            maps_url="https://maps.google.com/?cid=111",
            cid="111",
        )
        current_raw = RawPlace(
            name="Coffee House",
            maps_url="https://maps.google.com/?cid=222",
            cid="222",
        )
        raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Tokyo",
            places=[current_raw],
        )
        stale_place_id = build_data.stable_place_id(
            stale_raw,
            source_type=raw.configured_source_type,
        )
        current_place_id = build_data.stable_place_id(
            current_raw,
            source_type=raw.configured_source_type,
        )
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id=current_place_id,
                    name="Coffee House",
                    maps_url="https://www.google.com/maps/search/?api=1&query=Coffee+House",
                    status="active",
                )
            ],
        )
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Old Coffee House",
            matched=True,
            place=EnrichmentPlace(display_name="Old Coffee House"),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "cache" / "places.sqlite"

            with patch.object(build_data, "PLACES_SQLITE_PATH", db_path):
                build_data.save_places_cache("tokyo-japan", {stale_place_id: cache_entry})
                with self.assertRaisesRegex(RuntimeError, "tokyo-japan"):
                    build_data.rebuild_places_sqlite(
                        raw_lists={"tokyo-japan": raw},
                        guides=[guide],
                        enrichment_caches={"tokyo-japan": {stale_place_id: cache_entry}},
                    )
                loaded_payload = build_data.load_places_cache("tokyo-japan")

        self.assertEqual(list(loaded_payload), [stale_place_id])

    def test_rebuild_places_sqlite_hydrates_matching_guide_cache_photo_urls(self) -> None:
        shared_place_id = "cid:111"
        photo_url = "https://images.example/shared-photo.webp"
        kanazawa_raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Kanazawa",
            places=[
                RawPlace(
                    name="Shared Place",
                    address="1 Kanazawa, Japan",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                )
            ],
        )
        toyama_raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Toyama",
            places=[
                RawPlace(
                    name="Shared Place",
                    address="1 Toyama, Japan",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                )
            ],
        )
        kanazawa_guide = Guide(
            slug="kanazawa-japan",
            title="Kanazawa",
            country_name="Japan",
            city_name="Kanazawa",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id=shared_place_id,
                    name="Shared Place",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                    google_place_id="place-123",
                    google_place_resource_name="places/place-123",
                    status="active",
                )
            ],
        )
        toyama_guide = Guide(
            slug="toyama-japan",
            title="Toyama",
            country_name="Japan",
            city_name="Toyama",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id=shared_place_id,
                    name="Shared Place",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                    google_place_id="place-123",
                    google_place_resource_name="places/place-123",
                    main_photo_path="/place-photos/shared-photo.webp",
                    status="active",
                )
            ],
        )
        kanazawa_cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Shared Place, Kanazawa",
            matched=True,
            place=EnrichmentPlace(
                google_place_id="place-123",
                google_place_resource_name="places/place-123",
                display_name="Shared Place",
            ),
        )
        toyama_cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-21T00:00:00+00:00",
            query="Shared Place, Toyama",
            matched=True,
            place=EnrichmentPlace(
                display_name="Shared Place",
                main_photo_url=photo_url,
                photo_url=photo_url,
            ),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "cache" / "places.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)

            with (
                patch.object(build_data, "ROOT", tmpdir_path),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
            ):
                build_data.rebuild_places_sqlite(
                    raw_lists={
                        "kanazawa-japan": kanazawa_raw,
                        "toyama-japan": toyama_raw,
                    },
                    guides=[kanazawa_guide, toyama_guide],
                    enrichment_caches={
                        "kanazawa-japan": {shared_place_id: kanazawa_cache_entry},
                        "toyama-japan": {shared_place_id: toyama_cache_entry},
                    },
                )

            connection = sqlite3.connect(db_path)
            try:
                sqlite_cache_entry = connection.execute(
                    """
                    SELECT cache_json
                    FROM guide_enrichment_cache
                    WHERE guide_slug = ? AND place_id = ?
                    """,
                    ("kanazawa-japan", shared_place_id),
                ).fetchone()
                self.assertIsNotNone(sqlite_cache_entry)
                assert sqlite_cache_entry is not None
                place_payload = json.loads(sqlite_cache_entry[0])["place"]
                self.assertEqual(place_payload["main_photo_url"], photo_url)
                self.assertEqual(place_payload["photo_url"], photo_url)
            finally:
                connection.close()

    def test_build_places_sqlite_rows_only_contains_enrichment_cache_rows(self) -> None:
        shared_place_id = "cid:222"
        oita_raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Oita",
            places=[
                RawPlace(
                    name="Milch",
                    address="1 Oita, Japan",
                    maps_url="https://maps.google.com/?cid=222",
                    cid="222",
                )
            ],
        )
        yufuin_raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Yufuin",
            places=[
                RawPlace(
                    name="Share",
                    address="1 Yufuin, Japan",
                    maps_url="https://maps.google.com/?cid=222",
                    cid="222",
                )
            ],
        )
        oita_guide = Guide(
            slug="oita-japan",
            title="Oita",
            country_name="Japan",
            city_name="Oita",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id=shared_place_id,
                    name="Milch",
                    maps_url="https://maps.google.com/?cid=222",
                    cid="222",
                    google_place_id="place-milch",
                    google_place_resource_name="places/place-milch",
                    status="active",
                )
            ],
        )
        yufuin_guide = Guide(
            slug="yufuin-japan",
            title="Yufuin",
            country_name="Japan",
            city_name="Yufuin",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id=shared_place_id,
                    name="Share",
                    maps_url="https://maps.google.com/?cid=222",
                    cid="222",
                    main_photo_path="/place-photos/share.webp",
                    user_rating_count=100,
                    status="active",
                )
            ],
        )
        oita_cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Milch, Oita",
            matched=True,
            place=EnrichmentPlace(
                google_place_id="place-milch",
                google_place_resource_name="places/place-milch",
                display_name="Milch",
            ),
        )
        yufuin_cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-21T00:00:00+00:00",
            query="Share, Yufuin",
            matched=True,
            place=EnrichmentPlace(
                display_name="Share",
                photo_url="https://images.example/share.webp",
            ),
        )

        rows = build_data.build_places_sqlite_rows(
            raw_lists={
                "oita-japan": oita_raw,
                "yufuin-japan": yufuin_raw,
            },
            guides=[oita_guide, yufuin_guide],
            enrichment_caches={
                "oita-japan": {shared_place_id: oita_cache_entry},
                "yufuin-japan": {shared_place_id: yufuin_cache_entry},
            },
        )

        self.assertEqual(len(rows.guide_cache_rows), 2)
        cache_rows_by_guide = {row[0]: row for row in rows.guide_cache_rows}
        self.assertEqual(cache_rows_by_guide["oita-japan"][1], shared_place_id)
        self.assertEqual(cache_rows_by_guide["yufuin-japan"][1], shared_place_id)
        self.assertEqual(
            json.loads(cache_rows_by_guide["oita-japan"][12])["place"]["google_place_id"],
            "place-milch",
        )

    def test_rebuild_places_sqlite_skips_rewrite_when_signature_matches(self) -> None:
        raw = RawSavedList(
            configured_source_type="google_list_url",
            fetched_at="2026-04-20T00:00:00+00:00",
            title="Tokyo",
            places=[
                RawPlace(
                    name="Coffee House",
                    address="1 Shibuya, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                    lat=35.6595,
                    lng=139.7005,
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0], source_type=raw.configured_source_type)
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            last_verified_at="2026-04-20T00:00:00+00:00",
            refresh_after="2026-04-27T00:00:00+00:00",
            source="google_maps_page",
            query="Coffee House, 1 Shibuya, Tokyo, Japan",
            input_signature=build_data.place_input_signature(raw.places[0]),
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(display_name="Coffee House"),
        )
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            featured_place_ids=[place_id],
            best_hit_place_ids=[place_id],
            top_categories=["cafe"],
            places=[
                NormalizedPlace(
                    id=place_id,
                    name="Coffee House",
                    address="1 Shibuya, Tokyo, Japan",
                    maps_url="https://www.google.com/maps/search/?api=1&query=Coffee+House",
                    status="active",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "cache" / "places.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)

            with patch.object(build_data, "PLACES_SQLITE_PATH", db_path):
                build_data.rebuild_places_sqlite(
                    raw_lists={"tokyo-japan": raw},
                    guides=[guide],
                    enrichment_caches={"tokyo-japan": {place_id: cache_entry}},
                )
                first_stat = db_path.stat()
                build_data.rebuild_places_sqlite(
                    raw_lists={"tokyo-japan": raw},
                    guides=[guide],
                    enrichment_caches={"tokyo-japan": {place_id: cache_entry}},
                )
                second_stat = db_path.stat()

            self.assertEqual(first_stat.st_mtime_ns, second_stat.st_mtime_ns)

    def test_rebuild_places_sqlite_recovers_from_invalid_existing_db(self) -> None:
        raw = RawSavedList(
            configured_source_type="google_list_url",
            fetched_at="2026-04-20T00:00:00+00:00",
            title="Tokyo",
            places=[
                RawPlace(
                    name="Coffee House",
                    address="1 Shibuya, Tokyo, Japan",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                )
            ],
        )
        place_id = build_data.stable_place_id(raw.places[0], source_type=raw.configured_source_type)
        guide = Guide(
            slug="tokyo-japan",
            title="Tokyo",
            country_name="Japan",
            city_name="Tokyo",
            generated_at="2026-04-20T00:00:00+00:00",
            place_count=1,
            places=[
                NormalizedPlace(
                    id=place_id,
                    name="Coffee House",
                    maps_url="https://www.google.com/maps/search/?api=1&query=Coffee+House",
                    status="active",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "cache" / "places.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("not a sqlite file", encoding="utf-8")

            with patch.object(build_data, "PLACES_SQLITE_PATH", db_path):
                self.assertIsNone(build_data.load_places_cache_from_sqlite("tokyo-japan"))
                self.assertIsNone(build_data.load_places_sqlite_build_signature())

                build_data.rebuild_places_sqlite(
                    raw_lists={"tokyo-japan": raw},
                    guides=[guide],
                    enrichment_caches={"tokyo-japan": {}},
                )

            connection = sqlite3.connect(db_path)
            try:
                build_metadata = connection.execute(
                    "SELECT value FROM build_metadata WHERE key = ?",
                    (build_data.PLACES_SQLITE_BUILD_METADATA_KEY,),
                ).fetchone()
                self.assertIsNotNone(build_metadata)
            finally:
                connection.close()

    def test_load_places_cache_from_sqlite_raises_operational_error(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "cache" / "places.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.touch()

            with (
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "sqlite_table_exists", side_effect=sqlite3.OperationalError("database is locked")),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    build_data.load_places_cache_from_sqlite("tokyo-japan")

    def test_load_places_sqlite_build_signature_raises_operational_error(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "cache" / "places.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.touch()

            with (
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "sqlite_table_exists", side_effect=sqlite3.OperationalError("database is locked")),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    build_data.load_places_sqlite_build_signature()

    def test_save_places_cache_writes_sqlite_only_by_default(self) -> None:
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            refresh_after="2026-04-27T00:00:00+00:00",
            query="Coffee House, 1 Shibuya, Tokyo, Japan",
            input_signature="signature-v1",
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(
                display_name="Coffee House",
                formatted_address="1 Shibuya, Tokyo, Japan",
            ),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            cache_dir = tmpdir_path / "cache" / "google-places"
            db_path = tmpdir_path / "cache" / "places.sqlite"

            with (
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
            ):
                build_data.save_places_cache("tokyo-japan", {"cid:111": cache_entry})

                self.assertFalse((cache_dir / "tokyo-japan.json").exists())

                build_data.write_json(
                    cache_dir / "tokyo-japan.json",
                    {
                        "cid:111": {
                            "fetched_at": "2020-01-01T00:00:00+00:00",
                            "query": "stale json payload",
                            "matched": False,
                        }
                    },
                )

                loaded_payload = build_data.load_places_cache("tokyo-japan")

            self.assertEqual(loaded_payload["cid:111"].query, cache_entry.query)
            self.assertTrue(loaded_payload["cid:111"].matched)

    def test_save_places_cache_to_sqlite_canonicalizes_without_mutating_payload(self) -> None:
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Open Kitchen",
            matched=True,
            place=EnrichmentPlace(
                primary_type="restaurant",
                primary_type_display_name="Restaurant",
                primary_type_display_name_localized="レストラン",
                photo_url="https://lh3.googleusercontent.com:443/a-/ALV-UjW_avatar=w680-h680-p-rp-mo-br100",
            ),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "places.sqlite"

            with patch.object(build_data, "PLACES_SQLITE_PATH", db_path):
                build_data.save_places_cache_to_sqlite("tokyo-japan", {"cid:111": cache_entry})
                loaded_payload = build_data.load_places_cache("tokyo-japan")

        assert cache_entry.place is not None
        self.assertEqual(
            cache_entry.place.primary_type_display_name,
            "Restaurant",
        )
        self.assertEqual(
            cache_entry.place.primary_type_display_name_localized,
            "レストラン",
        )
        self.assertEqual(
            cache_entry.place.photo_url,
            "https://lh3.googleusercontent.com:443/a-/ALV-UjW_avatar=w680-h680-p-rp-mo-br100",
        )
        assert loaded_payload["cid:111"].place is not None
        self.assertEqual(loaded_payload["cid:111"].place.primary_type_display_name, "Restaurant")
        self.assertEqual(loaded_payload["cid:111"].place.primary_type_display_name_localized, "レストラン")
        self.assertIsNone(loaded_payload["cid:111"].place.photo_url)

    def test_save_places_cache_to_sqlite_rejects_empty_overwrite_of_existing_cache(self) -> None:
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Open Kitchen",
            matched=True,
            place=EnrichmentPlace(display_name="Open Kitchen"),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "places.sqlite"

            with patch.object(build_data, "PLACES_SQLITE_PATH", db_path):
                build_data.save_places_cache_to_sqlite("tokyo-japan", {"cid:111": cache_entry})
                with self.assertRaisesRegex(RuntimeError, "Refusing to replace non-empty enrichment cache"):
                    build_data.save_places_cache_to_sqlite("tokyo-japan", {})
                loaded_payload = build_data.load_places_cache("tokyo-japan")

        self.assertEqual(list(loaded_payload), ["cid:111"])
        self.assertEqual(loaded_payload["cid:111"].query, "Open Kitchen")

    def test_save_places_cache_to_sqlite_allows_explicit_empty_prune(self) -> None:
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Open Kitchen",
            matched=True,
            place=EnrichmentPlace(display_name="Open Kitchen"),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            db_path = tmpdir_path / "places.sqlite"

            with patch.object(build_data, "PLACES_SQLITE_PATH", db_path):
                build_data.save_places_cache_to_sqlite("tokyo-japan", {"cid:111": cache_entry})
                build_data.save_places_cache_to_sqlite("tokyo-japan", {}, allow_empty_overwrite=True)
                loaded_payload = build_data.load_places_cache("tokyo-japan")

        self.assertEqual(loaded_payload, {})

    def test_prune_places_cache_to_raw_places_drops_stale_place_ids(self) -> None:
        raw = RawSavedList(
            title="Aguas Calientes",
            places=[
                RawPlace(
                    name="Mapacho Craft Beer Restaurant",
                    maps_url="https://maps.google.com/?cid=14063537238082844765",
                    cid="14063537238082844765",
                    google_id="/g/11c59xr4t8",
                )
            ],
        )
        current_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Mapacho current",
            matched=True,
        )
        stale_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-01T00:00:00+00:00",
            query="Mapacho stale gid",
            matched=True,
        )

        pruned_payload, pruned_count = build_data.prune_places_cache_to_raw_places(
            {
                "cid:14063537238082844765": current_entry,
                "gid:g-11c59xr4t8": stale_entry,
            },
            raw,
        )

        self.assertEqual(pruned_count, 1)
        self.assertEqual(list(pruned_payload), ["cid:14063537238082844765"])
        self.assertIs(pruned_payload["cid:14063537238082844765"], current_entry)

    def test_prune_places_cache_to_raw_places_drops_all_rows_for_empty_guide(self) -> None:
        raw = RawSavedList(title="Empty", places=[])
        stale_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-01T00:00:00+00:00",
            query="stale",
            matched=True,
        )

        pruned_payload, pruned_count = build_data.prune_places_cache_to_raw_places(
            {"cid:111": stale_entry},
            raw,
        )

        self.assertEqual(pruned_payload, {})
        self.assertEqual(pruned_count, 1)

    def test_export_places_cache_json_writes_debug_output(self) -> None:
        cache_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            refresh_after="2026-04-27T00:00:00+00:00",
            query="Coffee House, 1 Shibuya, Tokyo, Japan",
            input_signature="signature-v1",
            matched=True,
            score=build_data.STRONG_MATCH_SCORE,
            place=EnrichmentPlace(display_name="Coffee House"),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            cache_dir = tmpdir_path / "cache" / "google-places"

            with patch.object(build_data, "PLACES_CACHE_DIR", cache_dir):
                build_data.export_places_cache_json("tokyo-japan", {"cid:111": cache_entry})

            exported = json.loads((cache_dir / "tokyo-japan.json").read_text(encoding="utf-8"))

        self.assertEqual(exported["cid:111"]["query"], cache_entry.query)

    def test_parallel_enrichment_terminates_workers_on_interrupt(self) -> None:
        raw = RawSavedList(
            title="Tokyo",
            places=[
                RawPlace(
                    name="First Place",
                    address="1 Example St, Tokyo",
                    maps_url="https://maps.google.com/?cid=111",
                    cid="111",
                ),
                RawPlace(
                    name="Second Place",
                    address="2 Example St, Tokyo",
                    maps_url="https://maps.google.com/?cid=222",
                    cid="222",
                )
            ],
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            raw_dir = tmpdir_path / "raw"
            cache_dir = tmpdir_path / "cache"
            db_path = tmpdir_path / "places.sqlite"
            raw_dir.mkdir()
            cache_dir.mkdir()
            (raw_dir / "tokyo-japan.json").write_text(
                json.dumps(raw.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )

            class FakeFuture:
                def result(self):
                    raise KeyboardInterrupt()

            future = FakeFuture()
            executor_holder: dict[str, object] = {}

            class FakeExecutor:
                def __init__(self, max_workers):
                    self.max_workers = max_workers
                    self.terminated = False
                    self.shutdown_calls: list[tuple[bool, bool]] = []
                    executor_holder["executor"] = self

                def submit(self, _fn, _slug, _place_id, _place_name, _refresh_reason, place_payload, **kwargs):
                    return future

                def terminate_workers(self):
                    self.terminated = True

                def shutdown(self, wait=True, cancel_futures=False):
                    self.shutdown_calls.append((wait, cancel_futures))

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "PLACES_CACHE_DIR", cache_dir),
                patch.object(build_data, "PLACES_SQLITE_PATH", db_path),
                patch.object(build_data, "google_places_api_key", return_value=None),
                patch.object(build_data, "ThreadPoolExecutor", FakeExecutor),
                patch.object(build_data, "as_completed", return_value=[future]),
                patch("builtins.print"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    build_data.enrich_raw_sources(
                        force_refresh=True,
                        refresh_workers=2,
                        refresh_startup_jitter_seconds=0,
                    )

        executor = executor_holder["executor"]
        assert isinstance(executor, FakeExecutor)
        self.assertTrue(executor.terminated)
        self.assertIn((True, True), executor.shutdown_calls)

    def test_enrich_place_job_logs_when_worker_starts(self) -> None:
        entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="First Place, Tokyo",
            matched=True,
            place=EnrichmentPlace(display_name="First Place"),
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter") as jitter_sleep,
            patch.object(build_data, "fetch_places_enrichment", return_value=entry) as fetch_enrichment,
            patch("builtins.print") as print_mock,
        ):
            result = build_data.enrich_place_job(
                "tokyo-japan",
                "cid:111",
                "First Place",
                "forced",
                {
                    "name": "First Place",
                    "address": "1 Example St, Tokyo",
                    "maps_url": "https://maps.google.com/?cid=111",
                    "cid": "111",
                },
                api_key=None,
                refresh_startup_jitter_seconds=3,
            )

        self.assertIs(result, entry)
        print_mock.assert_called_once_with(
            "Enriching tokyo-japan:cid:111 [First Place] (forced)",
            flush=True,
        )
        jitter_sleep.assert_called_once_with(3)
        fetch_enrichment.assert_called_once()

    def test_enrich_place_job_suppresses_semantic_description_for_handwritten_note(self) -> None:
        entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="First Place, Tokyo",
            matched=True,
            place=EnrichmentPlace(display_name="First Place"),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            overrides_dir = tmpdir_path / "overrides" / "places"
            overrides_dir.mkdir(parents=True)
            (overrides_dir / "tokyo-japan.json").write_text(
                json.dumps(
                    {
                        "cid:111": {
                            "note": "Handwritten editor description.",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "PLACE_OVERRIDES_DIR", overrides_dir),
                patch.object(build_data, "sleep_for_refresh_startup_jitter"),
                patch.object(build_data, "fetch_places_enrichment", return_value=entry) as fetch_enrichment,
                patch("builtins.print"),
            ):
                build_data.enrich_place_job(
                    "tokyo-japan",
                    "cid:111",
                    "First Place",
                    "forced",
                    {
                        "name": "First Place",
                        "address": "1 Example St, Tokyo",
                        "maps_url": "https://maps.google.com/?cid=111",
                        "cid": "111",
                    },
                    api_key=None,
                    refresh_startup_jitter_seconds=0,
                )

        self.assertTrue(fetch_enrichment.call_args.kwargs["suppress_description"])

    def test_enrich_place_job_uses_override_google_place_id(self) -> None:
        entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Taipei 101, Taipei",
            matched=True,
            place=EnrichmentPlace(display_name="Taipei 101"),
        )
        previous_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-19T00:00:00+00:00",
            query="Taipei 101, Taipei",
            matched=True,
            place=EnrichmentPlace(
                display_name="Taipei 101 Offices",
                google_place_id="stale-place-id",
            ),
        )

        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            overrides_dir = tmpdir_path / "overrides" / "places"
            overrides_dir.mkdir(parents=True)
            (overrides_dir / "taipei-taiwan.json").write_text(
                json.dumps(
                    {
                        "cid:3765761221328423815": {
                            "google_place_id": "ChIJraeA2rarQjQRPBBjyR3RxKw",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(build_data, "PLACE_OVERRIDES_DIR", overrides_dir),
                patch.object(build_data, "sleep_for_refresh_startup_jitter"),
                patch.object(build_data, "fetch_places_enrichment", return_value=entry) as fetch_enrichment,
                patch("builtins.print"),
            ):
                build_data.enrich_place_job(
                    "taipei-taiwan",
                    "cid:3765761221328423815",
                    "Taipei 101",
                    "forced",
                    {
                        "name": "Taipei 101",
                        "address": "No. 45, City Hall Rd, Taipei",
                        "maps_url": "https://www.google.com/maps?cid=3765761221328423815",
                        "cid": "3765761221328423815",
                    },
                    city_name="Taipei",
                    country_name="Taiwan",
                    api_key=None,
                    refresh_startup_jitter_seconds=0,
                    existing_entry=previous_entry,
                )

        self.assertEqual(
            fetch_enrichment.call_args.kwargs["google_place_id"],
            "ChIJraeA2rarQjQRPBBjyR3RxKw",
        )

    def test_enrich_place_job_preserves_previous_entry_when_refresh_degrades_to_error(self) -> None:
        previous_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="First Place, Tokyo",
            matched=True,
            place=EnrichmentPlace(
                display_name="First Place",
                rating=4.7,
                user_rating_count=321,
                primary_type="restaurant",
                primary_type_display_name="Restaurant",
                business_status="CLOSED_TEMPORARILY",
                google_maps_uri="https://www.google.com/maps/search/?api=1&query=First+Place&query_place_id=place123",
                google_place_id="place123",
                photo_url="https://photos.example/old.jpg",
            ),
        )
        degraded_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-23T00:00:00+00:00",
            query="First Place, Tokyo",
            source="google_places_api",
            error="http_403",
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter"),
            patch.object(build_data, "fetch_places_enrichment", return_value=degraded_entry),
            patch("builtins.print") as print_mock,
        ):
            result = build_data.enrich_place_job(
                "tokyo-japan",
                "cid:111",
                "First Place",
                "refresh-window-expired",
                {
                    "name": "First Place",
                    "address": "1 Example St, Tokyo",
                    "maps_url": "https://maps.google.com/?cid=111",
                    "cid": "111",
                },
                api_key="test-key",
                refresh_startup_jitter_seconds=0,
                existing_entry=previous_entry,
            )

        self.assertIs(result, previous_entry)
        self.assertEqual(print_mock.call_count, 2)
        self.assertEqual(
            print_mock.call_args_list[1].args[0],
            "WARNING: Preserving previous enrichment for tokyo-japan:cid:111 [First Place] "
            "because refresh returned degraded result (http_403).",
        )

    def test_enrich_place_job_scrubs_suspicious_photo_when_refresh_degrades(self) -> None:
        previous_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="First Place, Tokyo",
            matched=True,
            place=EnrichmentPlace(
                display_name="First Place",
                rating=4.7,
                google_maps_uri="https://www.google.com/maps/search/?api=1&query=First+Place&query_place_id=place123",
                google_place_id="place123",
                photo_url="https://lh3.googleusercontent.com/a-/ALV-UjW_avatar=w36-h36-p-rp-mo-br100",
            ),
        )
        degraded_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-23T00:00:00+00:00",
            query="First Place, Tokyo",
            source="google_places_api",
            error="http_403",
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter"),
            patch.object(build_data, "fetch_places_enrichment", return_value=degraded_entry),
            patch("builtins.print"),
        ):
            result = build_data.enrich_place_job(
                "tokyo-japan",
                "cid:111",
                "First Place",
                "refresh-window-expired",
                {
                    "name": "First Place",
                    "address": "1 Example St, Tokyo",
                    "maps_url": "https://maps.google.com/?cid=111",
                    "cid": "111",
                },
                api_key="test-key",
                refresh_startup_jitter_seconds=0,
                existing_entry=previous_entry,
            )

        self.assertIs(result, previous_entry)
        self.assertIsNone(result.place.photo_url)

    def test_enrich_place_job_preserves_previous_fields_when_refresh_loses_metadata(self) -> None:
        previous_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="First Place, Tokyo",
            matched=True,
            place=EnrichmentPlace(
                display_name="First Place",
                rating=4.7,
                user_rating_count=321,
                primary_type="restaurant",
                primary_type_display_name="Restaurant",
                business_status="CLOSED_TEMPORARILY",
                google_maps_uri="https://www.google.com/maps/search/?api=1&query=First+Place&query_place_id=place123",
                google_place_id="place123",
                google_place_resource_name="places/place123",
                photo_url="https://photos.example/old.jpg",
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-23T00:00:00+00:00",
            query="First Place, Tokyo",
            matched=True,
            place=EnrichmentPlace(
                display_name="First Place",
                google_maps_uri="https://www.google.com/maps/search/?api=1&query=First+Place",
            ),
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter"),
            patch.object(build_data, "fetch_places_enrichment", return_value=refreshed_entry),
            patch("builtins.print") as print_mock,
        ):
            result = build_data.enrich_place_job(
                "tokyo-japan",
                "cid:111",
                "First Place",
                "refresh-window-expired",
                {
                    "name": "First Place",
                    "address": "1 Example St, Tokyo",
                    "maps_url": "https://maps.google.com/?cid=111",
                    "cid": "111",
                },
                api_key=None,
                refresh_startup_jitter_seconds=0,
                existing_entry=previous_entry,
            )

        self.assertIs(result, refreshed_entry)
        self.assertEqual(result.place.rating, 4.7)
        self.assertEqual(result.place.user_rating_count, 321)
        self.assertEqual(result.place.primary_type_display_name, "Restaurant")
        self.assertEqual(result.place.business_status, "CLOSED_TEMPORARILY")
        self.assertEqual(
            result.place.google_maps_uri,
            "https://www.google.com/maps/search/?api=1&query=First+Place&query_place_id=place123",
        )
        self.assertEqual(result.place.google_place_id, "place123")
        self.assertEqual(result.place.photo_url, "https://photos.example/old.jpg")
        self.assertEqual(print_mock.call_count, 2)
        self.assertEqual(
            print_mock.call_args_list[1].args[0],
            "WARNING: Preserving previous enrichment fields for tokyo-japan:cid:111 [First Place]: "
            "rating, user_rating_count, primary_category, status, maps_url, photo_url.",
        )

    def test_enrich_place_job_preserves_valid_previous_address_when_refresh_loses_it(self) -> None:
        previous_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Tropical Taste, Tonga",
            matched=True,
            place=EnrichmentPlace(
                display_name="Tropical Taste",
                formatted_address="VQ4V+29M, Nuku'alofa, Tonga",
                rating=4.6,
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-23T00:00:00+00:00",
            query="Tropical Taste, Tonga",
            matched=True,
            place=EnrichmentPlace(
                display_name="Tropical Taste",
                primary_type_display_name="Restaurant",
            ),
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter"),
            patch.object(build_data, "fetch_places_enrichment", return_value=refreshed_entry),
            patch("builtins.print") as print_mock,
        ):
            result = build_data.enrich_place_job(
                "tonga",
                "cid:111",
                "Tropical Taste",
                "forced",
                {
                    "name": "Tropical Taste",
                    "address": "VQ4V+29M, Nuku'alofa, Tonga",
                    "maps_url": "https://maps.google.com/?cid=111",
                    "cid": "111",
                },
                api_key=None,
                refresh_startup_jitter_seconds=0,
                existing_entry=previous_entry,
            )

        self.assertIs(result, refreshed_entry)
        assert result.place is not None
        self.assertEqual(result.place.formatted_address, "VQ4V+29M, Nuku'alofa, Tonga")
        self.assertEqual(print_mock.call_count, 2)
        self.assertEqual(
            print_mock.call_args_list[1].args[0],
            "WARNING: Preserving previous enrichment fields for tonga:cid:111 [Tropical Taste]: "
            "rating, address.",
        )

    def test_enrich_place_job_does_not_preserve_suspicious_previous_address(self) -> None:
        previous_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="Tropical Taste, Tonga",
            matched=True,
            place=EnrichmentPlace(
                display_name="Tropical Taste",
                formatted_address=(
                    "The best takeout or eat in I recommend this place. We dropped in 5 minutes "
                    "before closing time and the owner took the initiative to cook us More"
                ),
                rating=4.6,
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-23T00:00:00+00:00",
            query="Tropical Taste, Tonga",
            matched=True,
            place=EnrichmentPlace(
                display_name="Tropical Taste",
                primary_type_display_name="Restaurant",
            ),
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter"),
            patch.object(build_data, "fetch_places_enrichment", return_value=refreshed_entry),
            patch("builtins.print"),
        ):
            result = build_data.enrich_place_job(
                "tonga",
                "cid:111",
                "Tropical Taste",
                "forced",
                {
                    "name": "Tropical Taste",
                    "address": "VQ4V+29M, Nuku'alofa, Tonga",
                    "maps_url": "https://maps.google.com/?cid=111",
                    "cid": "111",
                },
                api_key=None,
                refresh_startup_jitter_seconds=0,
                existing_entry=previous_entry,
            )

        assert result.place is not None
        self.assertIsNone(result.place.formatted_address)

    def test_enrich_place_job_does_not_preserve_suspicious_old_photo_urls(self) -> None:
        previous_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-20T00:00:00+00:00",
            query="First Place, Tokyo",
            matched=True,
            place=EnrichmentPlace(
                display_name="First Place",
                rating=4.7,
                user_rating_count=321,
                google_maps_uri="https://www.google.com/maps/search/?api=1&query=First+Place&query_place_id=place123",
                google_place_id="place123",
                photo_url="https://lh3.googleusercontent.com/a-/ALV-UjW_avatar=w36-h36-p-rp-mo-br100",
            ),
        )
        refreshed_entry = EnrichmentCacheEntry(
            fetched_at="2026-04-23T00:00:00+00:00",
            query="First Place, Tokyo",
            matched=True,
            place=EnrichmentPlace(
                display_name="First Place",
                google_maps_uri="https://www.google.com/maps/search/?api=1&query=First+Place",
            ),
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter"),
            patch.object(build_data, "fetch_places_enrichment", return_value=refreshed_entry),
            patch("builtins.print") as print_mock,
        ):
            result = build_data.enrich_place_job(
                "tokyo-japan",
                "cid:111",
                "First Place",
                "refresh-window-expired",
                {
                    "name": "First Place",
                    "address": "1 Example St, Tokyo",
                    "maps_url": "https://maps.google.com/?cid=111",
                    "cid": "111",
                },
                api_key=None,
                refresh_startup_jitter_seconds=0,
                existing_entry=previous_entry,
            )

        self.assertIs(result, refreshed_entry)
        self.assertIsNone(result.place.photo_url)
        self.assertEqual(print_mock.call_count, 2)
        self.assertEqual(
            print_mock.call_args_list[1].args[0],
            "WARNING: Preserving previous enrichment fields for tokyo-japan:cid:111 [First Place]: "
            "rating, user_rating_count, maps_url.",
        )

    def test_refresh_retries_transient_parse_failure(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        with (
            patch.object(build_data, "sleep_for_refresh_startup_jitter"),
            patch.object(build_data.time, "sleep") as retry_sleep,
            patch.object(
                build_data,
                "scrape_google_list_url",
                side_effect=[build_data.ParseError("missing list payload"), RawSavedList(title="Tokyo", places=[])],
            ) as scrape,
        ):
            payload = build_data.scrape_google_list_url_with_retries(
                source,
                headed=False,
                refresh_retries=2,
                refresh_retry_backoff_seconds=10,
                refresh_startup_jitter_seconds=8,
            )

        self.assertEqual(payload.title, "Tokyo")
        self.assertEqual(scrape.call_count, 2)
        retry_sleep.assert_called_once_with(10)

    def test_refresh_raw_sources_keeps_existing_snapshot_when_refresh_fails(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        with TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir)
            raw_path = raw_dir / "tokyo-japan.json"
            build_data.write_json(
                raw_path,
                RawSavedList(
                    title="Backup",
                    fetched_at=datetime.now(UTC).isoformat(),
                    refresh_after=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    source_signature=build_data.raw_source_signature(source),
                    places=[],
                ),
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "load_sources", return_value=[source]),
                patch.object(build_data, "scrape_google_list_url", side_effect=build_data.ScrapeError("timeout")),
            ):
                build_data.refresh_raw_sources(
                    headed=False,
                    force_refresh=False,
                    refresh_lists=[],
                    refresh_workers=1,
                    refresh_retries=0,
                    refresh_startup_jitter_seconds=0,
                )

            payload = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))

        self.assertEqual(payload.title, "Backup")

    def test_refresh_raw_sources_keeps_existing_snapshot_when_parse_fails(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        with TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir)
            raw_path = raw_dir / "tokyo-japan.json"
            build_data.write_json(
                raw_path,
                RawSavedList(
                    title="Backup",
                    fetched_at=datetime.now(UTC).isoformat(),
                    refresh_after=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    source_signature=build_data.raw_source_signature(source),
                    places=[],
                ),
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "load_sources", return_value=[source]),
                patch.object(
                    build_data,
                    "scrape_google_list_url",
                    side_effect=build_data.ParseError("missing list payload"),
                ),
            ):
                build_data.refresh_raw_sources(
                    headed=False,
                    force_refresh=False,
                    refresh_lists=[],
                    refresh_workers=1,
                    refresh_retries=0,
                    refresh_startup_jitter_seconds=0,
                )

            payload = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))

        self.assertEqual(payload.title, "Backup")

    def test_refresh_raw_sources_does_not_hide_unexpected_refresh_errors(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )

        with TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir)
            raw_path = raw_dir / "tokyo-japan.json"
            build_data.write_json(
                raw_path,
                RawSavedList(
                    title="Backup",
                    fetched_at=datetime.now(UTC).isoformat(),
                    refresh_after=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    source_signature=build_data.raw_source_signature(source),
                    places=[],
                ),
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "load_sources", return_value=[source]),
                patch.object(build_data, "scrape_google_list_url", side_effect=ValueError("bad data")),
            ):
                with self.assertRaisesRegex(ValueError, "bad data"):
                    build_data.refresh_raw_sources(
                        headed=False,
                        force_refresh=False,
                        refresh_lists=[],
                        refresh_workers=1,
                        refresh_retries=0,
                        refresh_startup_jitter_seconds=0,
                    )

            payload = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))

        self.assertEqual(payload.title, "Backup")

    def test_refresh_raw_sources_rejects_backup_when_source_config_changed(self) -> None:
        current_source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/current",
        )
        previous_source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/previous",
        )

        with TemporaryDirectory() as tmpdir:
            raw_dir = Path(tmpdir)
            raw_path = raw_dir / "tokyo-japan.json"
            build_data.write_json(
                raw_path,
                RawSavedList(
                    title="Previous List",
                    fetched_at=datetime.now(UTC).isoformat(),
                    refresh_after=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    source_signature=build_data.raw_source_signature(previous_source),
                    places=[],
                ),
            )

            with (
                patch.object(build_data, "RAW_DIR", raw_dir),
                patch.object(build_data, "load_sources", return_value=[current_source]),
                patch.object(build_data, "scrape_google_list_url", side_effect=build_data.ScrapeError("timeout")),
            ):
                with self.assertRaisesRegex(RuntimeError, "Raw refresh failed for 1 source"):
                    build_data.refresh_raw_sources(
                        headed=False,
                        force_refresh=False,
                        refresh_lists=[],
                        refresh_workers=1,
                        refresh_retries=0,
                        refresh_startup_jitter_seconds=0,
                    )

            payload = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))

        self.assertEqual(payload.title, "Previous List")

    def test_google_list_refresh_after_uses_stable_source_jitter(self) -> None:
        source = SourceConfig(
            slug="tokyo-japan",
            type="google_list_url",
            url="https://maps.app.goo.gl/tokyo",
        )
        fetched_at = datetime(2026, 4, 15, tzinfo=UTC)
        source_signature = build_data.raw_source_signature(source)

        refresh_after = build_data.raw_source_refresh_after(
            fetched_at,
            source,
            source_signature=source_signature,
        )
        repeated_refresh_after = build_data.raw_source_refresh_after(
            fetched_at,
            source,
            source_signature=source_signature,
        )
        lower_bound = fetched_at + build_data.RAW_SOURCE_CACHE_TTL - build_data.RAW_SOURCE_REFRESH_JITTER
        upper_bound = fetched_at + build_data.RAW_SOURCE_CACHE_TTL + build_data.RAW_SOURCE_REFRESH_JITTER

        self.assertEqual(refresh_after, repeated_refresh_after)
        self.assertGreaterEqual(refresh_after, lower_bound)
        self.assertLessEqual(refresh_after, upper_bound)


if __name__ == "__main__":
    unittest.main()

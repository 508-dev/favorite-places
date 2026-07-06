from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import build_data
from scripts import refresh_profiles


class SelfHostedRefreshTests(unittest.TestCase):
    def test_parser_defaults_to_balanced_profile(self) -> None:
        parser = refresh_profiles.build_parser()
        args = parser.parse_args([])

        self.assertEqual(args.profile, "balanced")
        self.assertEqual(args.refresh_workers, build_data.DEFAULT_REFRESH_WORKERS)

    def test_balanced_profile_runs_incremental_refresh(self) -> None:
        with (
            patch.object(refresh_profiles.build_data, "refresh_raw_sources") as refresh_raw_sources,
            patch.object(refresh_profiles.build_data, "sync_local_csv_sources") as sync_local_csv_sources,
            patch.object(refresh_profiles.build_data, "enrich_raw_sources") as enrich_raw_sources,
            patch.object(refresh_profiles.build_data, "rebuild_generated_data") as rebuild_generated_data,
        ):
            result = refresh_profiles.main(["balanced"])

        self.assertEqual(result, 0)
        refresh_raw_sources.assert_called_once_with(
            headed=False,
            force_refresh=False,
            refresh_lists=[],
            refresh_workers=build_data.DEFAULT_REFRESH_WORKERS,
            force_url_refresh=True,
            refresh_retries=build_data.DEFAULT_REFRESH_RETRIES,
            refresh_retry_backoff_seconds=build_data.DEFAULT_REFRESH_RETRY_BACKOFF_SECONDS,
            refresh_startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )
        sync_local_csv_sources.assert_called_once_with()
        enrich_raw_sources.assert_called_once_with(
            force_refresh=False,
            missing_only=False,
            refresh_workers=build_data.DEFAULT_REFRESH_WORKERS,
            refresh_startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )
        rebuild_generated_data.assert_called_once_with(
            refresh_photos=True,
            photo_workers=build_data.DEFAULT_REFRESH_WORKERS,
            startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )

    def test_backfill_profile_only_fills_missing_enrichment(self) -> None:
        with (
            patch.object(refresh_profiles.build_data, "refresh_raw_sources"),
            patch.object(refresh_profiles.build_data, "sync_local_csv_sources"),
            patch.object(refresh_profiles.build_data, "enrich_raw_sources") as enrich_raw_sources,
            patch.object(refresh_profiles.build_data, "rebuild_generated_data"),
        ):
            refresh_profiles.main(["backfill"])

        enrich_raw_sources.assert_called_once_with(
            force_refresh=False,
            missing_only=True,
            refresh_workers=build_data.DEFAULT_REFRESH_WORKERS,
            refresh_startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )

    def test_sweep_profile_forces_enrichment_refresh_and_allows_photo_skip(self) -> None:
        with (
            patch.object(refresh_profiles.build_data, "refresh_raw_sources") as refresh_raw_sources,
            patch.object(refresh_profiles.build_data, "sync_local_csv_sources"),
            patch.object(refresh_profiles.build_data, "enrich_raw_sources") as enrich_raw_sources,
            patch.object(refresh_profiles.build_data, "rebuild_generated_data") as rebuild_generated_data,
        ):
            refresh_profiles.main(
                [
                    "sweep",
                    "--refresh-force",
                    "--refresh-list",
                    "tokyo-japan",
                    "--skip-photos",
                ]
            )

        refresh_raw_sources.assert_called_once_with(
            headed=False,
            force_refresh=True,
            refresh_lists=["tokyo-japan"],
            refresh_workers=build_data.DEFAULT_REFRESH_WORKERS,
            force_url_refresh=False,
            refresh_retries=build_data.DEFAULT_REFRESH_RETRIES,
            refresh_retry_backoff_seconds=build_data.DEFAULT_REFRESH_RETRY_BACKOFF_SECONDS,
            refresh_startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )
        enrich_raw_sources.assert_called_once_with(
            force_refresh=True,
            missing_only=False,
            refresh_workers=build_data.DEFAULT_REFRESH_WORKERS,
            refresh_startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )
        rebuild_generated_data.assert_called_once_with(
            refresh_photos=False,
            photo_workers=build_data.DEFAULT_REFRESH_WORKERS,
            startup_jitter_seconds=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        )

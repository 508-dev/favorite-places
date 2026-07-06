from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts import build_data as build_data
else:
    try:
        from scripts import build_data as build_data
    except ImportError:
        import build_data as build_data


@dataclass(frozen=True)
class RefreshProfile:
    description: str
    force_raw_refresh: bool = False
    enrich_force_refresh: bool = False
    enrich_missing_only: bool = False
    refresh_photos: bool = True


REFRESH_PROFILES: dict[str, RefreshProfile] = {
    "balanced": RefreshProfile(
        description=(
            "Refresh URL-backed raw sources to pull in fresh list changes, then run incremental enrichment. "
            "CSV sources still use content signatures to skip unchanged imports. "
            "Missing and changed places go first; stale entries are refreshed by TTL afterward."
        ),
        force_raw_refresh=True,
    ),
    "backfill": RefreshProfile(
        description=(
            "Refresh due raw sources, then fill only missing enrichment and missing photos. "
            "Use this when you want the cheapest possible recurring pass."
        ),
        enrich_missing_only=True,
    ),
    "sweep": RefreshProfile(
        description=(
            "Refresh due raw sources, then force-refresh every enrichment cache entry as a periodic backstop."
        ),
        enrich_force_refresh=True,
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        nargs="?",
        choices=sorted(REFRESH_PROFILES),
        default="balanced",
        help="Refresh profile to run. Defaults to the normal balanced incremental pass.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run the Google list scraper in headed mode.",
    )
    parser.add_argument(
        "--refresh-force",
        action="store_true",
        help="Force raw source refreshes even if the source refresh window has not expired.",
    )
    parser.add_argument(
        "--refresh-list",
        action="append",
        default=[],
        help="Refresh only the configured source matching this slug, URL, or path. Repeat to target multiple sources.",
    )
    parser.add_argument(
        "--refresh-workers",
        type=int,
        default=build_data.DEFAULT_REFRESH_WORKERS,
        help="Maximum parallel workers for raw refreshes, enrichment jobs, and optional photo refreshes.",
    )
    parser.add_argument(
        "--refresh-retries",
        type=build_data.non_negative_int,
        default=build_data.DEFAULT_REFRESH_RETRIES,
        help="Retry each Google list refresh this many times after the first failed attempt.",
    )
    parser.add_argument(
        "--refresh-retry-backoff-seconds",
        type=build_data.non_negative_float,
        default=build_data.DEFAULT_REFRESH_RETRY_BACKOFF_SECONDS,
        help="Initial delay before retrying a failed Google list refresh.",
    )
    parser.add_argument(
        "--refresh-startup-jitter-seconds",
        type=build_data.non_negative_float,
        default=build_data.DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        help="Maximum randomized delay before each raw refresh, enrichment job, or photo refresh starts.",
    )
    parser.add_argument(
        "--skip-photos",
        action="store_true",
        help="Skip refreshing missing local photo derivatives during the final rebuild step.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    profile = REFRESH_PROFILES[args.profile]

    print(f"Running refresh profile '{args.profile}': {profile.description}")
    if args.refresh_list:
        print(f"Selected source filter(s): {', '.join(args.refresh_list)}")
    if args.refresh_force:
        print("Raw source refreshes are forced for this run.")
    if profile.force_raw_refresh and not args.refresh_force:
        print("Profile forces raw source refreshes so list changes are discovered promptly.")

    build_data.refresh_raw_sources(
        headed=args.headed,
        force_refresh=args.refresh_force,
        refresh_lists=args.refresh_list,
        refresh_workers=args.refresh_workers,
        force_url_refresh=profile.force_raw_refresh,
        refresh_retries=args.refresh_retries,
        refresh_retry_backoff_seconds=args.refresh_retry_backoff_seconds,
        refresh_startup_jitter_seconds=args.refresh_startup_jitter_seconds,
    )

    build_data.sync_local_csv_sources()
    build_data.enrich_raw_sources(
        force_refresh=profile.enrich_force_refresh,
        missing_only=profile.enrich_missing_only,
        refresh_workers=args.refresh_workers,
        refresh_startup_jitter_seconds=args.refresh_startup_jitter_seconds,
    )

    build_data.rebuild_generated_data(
        refresh_photos=profile.refresh_photos and not args.skip_photos,
        photo_workers=args.refresh_workers,
        startup_jitter_seconds=args.refresh_startup_jitter_seconds,
    )
    print("Refresh complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

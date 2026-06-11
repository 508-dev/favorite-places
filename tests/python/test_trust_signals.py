from __future__ import annotations

from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import os
import unittest

from scripts.pipeline_models import TrustSignal
from scripts.pipeline_models import RawPlace, RawSavedList
from scripts import build_data
from scripts.trust_signals import (
    MichelinRegionSource,
    MichelinRestaurant,
    MichelinMatchContext,
    TabelogRestaurant,
    TabelogSource,
    SearchResult,
    TrustSignalStore,
    default_trust_cache_path,
    michelin_next_page_url,
    michelin_region_sources_for_guides,
    parse_michelin_full_list_article_page,
    parse_michelin_detail_page_award_year,
    parse_michelin_region_page,
    parse_tabelog_award_page,
    parse_tabelog_hyakumeiten_page,
    parse_tabelog_search_results,
    parse_wikipedia_michelin_starred_page,
    parse_google_search_results,
    refresh_trust_signals_for_raw_guides,
    scrape_official_michelin_region,
    enrich_michelin_signal_award_years,
    infer_award_year,
    signals_from_michelin_region,
    signals_from_tabelog_restaurants,
    signals_from_tabelog_search_results,
    signals_from_search_results,
    sqlite_url_for_path,
    tabelog_hyakumeiten_category_urls,
    tabelog_sources_for_guides,
    trust_match_signature,
)


class TrustSignalsTest(unittest.TestCase):
    def test_default_trust_cache_path_is_user_level_shared_cache(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache_path = default_trust_cache_path(Path("/repo"), env={"XDG_CACHE_HOME": tmpdir})

        self.assertEqual(
            cache_path,
            Path(tmpdir) / "favorite-places" / "trust-signals" / "trust.sqlite",
        )

    def test_sqlite_url_for_path_does_not_create_parent_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing" / "trust.sqlite"
            store_url = sqlite_url_for_path(db_path)

            self.assertFalse(db_path.parent.exists())

            TrustSignalStore(store_url)

            self.assertTrue(db_path.parent.exists())

    def test_store_can_skip_schema_initialization_for_read_only_loads(self) -> None:
        with (
            patch("scripts.trust_signals.ensure_sqlite_store_parent_dir") as ensure_parent_dir,
            patch("scripts.trust_signals.metadata.create_all") as create_all,
            patch("scripts.trust_signals.ensure_trust_schema") as ensure_schema,
        ):
            TrustSignalStore("sqlite:///:memory:", initialize=False)

        ensure_parent_dir.assert_not_called()
        create_all.assert_not_called()
        ensure_schema.assert_not_called()

    def test_store_round_trips_place_signals(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.sqlite"
            store = TrustSignalStore(sqlite_url_for_path(db_path))
            fetched_at = datetime(2026, 1, 15, tzinfo=UTC)
            signal = TrustSignal(
                source="michelin",
                label="MICHELIN Guide",
                tier="Bib Gourmand",
                url="https://guide.michelin.com/en/jp/tokyo-region/restaurant/coffee-house",
                title="Coffee House - Tokyo - a MICHELIN Guide Restaurant",
                fetched_at=fetched_at.isoformat(),
                confidence="high",
                match_reason="name plus source/location match",
            )

            store.replace_search_signals(
                "cid:111",
                [signal],
                match_signature=trust_match_signature("Coffee House", "Tokyo", "Japan"),
                now=fetched_at,
            )

            loaded = store.load_signals_for_place_keys(["cid:111"])

        self.assertEqual(loaded, {"cid:111": [signal]})

    def test_store_refresh_replaces_stale_michelin_rows_for_place(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.sqlite"
            store = TrustSignalStore(sqlite_url_for_path(db_path))
            fetched_at = datetime(2026, 1, 15, tzinfo=UTC)
            match_signature = trust_match_signature("Quintessence", "Tokyo", "Japan")
            stale_signal = TrustSignal(
                source="michelin",
                label="MICHELIN Guide",
                tier="Selected",
                url="https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/quintessence-1193907",
                title="Quintessence",
                fetched_at=fetched_at.isoformat(),
                confidence="high",
                match_reason="Michelin name exact match",
            )
            current_signal = TrustSignal(
                source="michelin",
                label="MICHELIN Guide",
                tier="3 stars",
                award_year=2026,
                is_current=True,
                url="https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/quintessence-1193907",
                title="Quintessence",
                fetched_at=fetched_at.isoformat(),
                confidence="high",
                match_reason="Michelin name exact match",
            )

            store.replace_search_signals("cid:111", [stale_signal], match_signature=match_signature, now=fetched_at)
            store.replace_search_signals("cid:111", [current_signal], match_signature=match_signature, now=fetched_at)

            loaded = store.load_signals_for_place_keys(["cid:111"])

        self.assertEqual(loaded, {"cid:111": [current_signal]})

    def test_refresh_clears_stale_michelin_rows_without_search_provider(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.sqlite"
            now = datetime(2026, 1, 15, tzinfo=UTC)
            match_signature = trust_match_signature("Former Star", "Tokyo", "Japan")
            stale_michelin = TrustSignal(
                source="michelin",
                label="MICHELIN Guide",
                tier="1 star",
                award_year=2025,
                is_current=True,
                url="https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/former-star",
                title="Former Star",
                fetched_at=now.isoformat(),
                confidence="high",
                match_reason="Michelin name exact match",
            )
            tabelog_signal = TrustSignal(
                source="tabelog",
                label="Tabelog Award",
                tier="Bronze",
                award_year=2025,
                url="https://award.tabelog.com/en/restaurants/former-star",
                title="Former Star - Tabelog Award",
                fetched_at=now.isoformat(),
                confidence="high",
                match_reason="name plus source/location match",
            )
            store = TrustSignalStore(sqlite_url_for_path(db_path))
            store.replace_search_signals("place-1", [stale_michelin, tabelog_signal], match_signature=match_signature, now=now)
            raw_lists = {
                "tokyo-japan": RawSavedList(
                    title="Tokyo, Japan",
                    places=[
                        RawPlace(
                            name="Former Star",
                            address="1 Shibuya, Tokyo, Japan",
                            maps_url="https://maps.google.com/?q=former-star",
                        )
                    ],
                )
            }

            with (
                patch.dict(
                    os.environ,
                    {
                        "FAVORITE_PLACES_TRUST_CACHE_PATH": str(db_path),
                        "BRAVE_SEARCH_API_KEY": "",
                        "BRAVE_API_KEY": "",
                        "FAVORITE_PLACES_TRUST_GOOGLE_FALLBACK": "",
                    },
                ),
                patch(
                    "scripts.trust_signals.scrape_michelin_region_source",
                    return_value=[
                        MichelinRestaurant(
                            name="Other Restaurant",
                            url="https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/other",
                            tier="1 star",
                            is_current=True,
                            region_key="japan/tokyo",
                        )
                    ],
                ),
                patch("scripts.trust_signals.tabelog_sources_for_guides", return_value={}),
            ):
                summary = refresh_trust_signals_for_raw_guides(
                    root=Path(tmpdir),
                    raw_lists=raw_lists,
                    enrichment_caches={"tokyo-japan": {}},
                    stable_place_ids={("tokyo-japan", 0): "place-1"},
                )

            loaded = store.load_signals_for_place_keys(["place-1"])

        self.assertEqual(summary.searched_places, 1)
        self.assertEqual(loaded, {"place-1": [tabelog_signal]})

    def test_refresh_skips_search_michelin_signal_when_region_signal_has_same_url(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.sqlite"
            raw_lists = {
                "tokyo-japan": RawSavedList(
                    title="Tokyo, Japan",
                    places=[
                        RawPlace(
                            name="Coffee House",
                            address="1 Shibuya, Tokyo, Japan",
                            maps_url="https://maps.google.com/?q=coffee-house",
                        )
                    ],
                )
            }

            with (
                patch.dict(
                    os.environ,
                    {
                        "FAVORITE_PLACES_TRUST_CACHE_PATH": str(db_path),
                        "BRAVE_SEARCH_API_KEY": "brave-key",
                        "BRAVE_API_KEY": "",
                        "FAVORITE_PLACES_TRUST_GOOGLE_FALLBACK": "",
                    },
                ),
                patch(
                    "scripts.trust_signals.scrape_michelin_region_source",
                    return_value=[
                        MichelinRestaurant(
                            name="Coffee House",
                            url="https://guide.michelin.com/en/tokyo-region/restaurant/coffee-house",
                            tier="Selected",
                            is_current=True,
                            region_key="japan/tokyo",
                        )
                    ],
                ),
                patch(
                    "scripts.trust_signals.brave_search",
                    return_value=[
                        SearchResult(
                            title="Coffee House - Tokyo - MICHELIN Guide",
                            url="https://guide.michelin.com/en/jp/tokyo-region/restaurant/coffee-house",
                            snippet="Selected restaurant in Tokyo.",
                        ),
                        SearchResult(
                            title="The Tabelog Award 2026 Bronze Coffee House",
                            url="https://award.tabelog.com/en/restaurants/coffee-house",
                            snippet="Restaurant award winner.",
                        ),
                    ],
                ),
                patch("scripts.trust_signals.tabelog_sources_for_guides", return_value={}),
            ):
                refresh_trust_signals_for_raw_guides(
                    root=Path(tmpdir),
                    raw_lists=raw_lists,
                    enrichment_caches={"tokyo-japan": {}},
                    stable_place_ids={("tokyo-japan", 0): "place-1"},
                )

            loaded = TrustSignalStore(sqlite_url_for_path(db_path)).load_signals_for_place_keys(["place-1"])

        self.assertEqual([signal.source for signal in loaded["place-1"]], ["michelin", "tabelog"])
        self.assertEqual(loaded["place-1"][0].title, "Coffee House")

    def test_refresh_writes_tabelog_source_signals_for_japan_without_search_provider(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.sqlite"
            raw_lists = {
                "tokyo-japan": RawSavedList(
                    title="Tokyo, Japan",
                    places=[
                        RawPlace(
                            name="GINZA KOKORO",
                            address="Ginza, Tokyo, Japan",
                            maps_url="https://maps.google.com/?q=ginza-kokoro",
                        )
                    ],
                )
            }

            with (
                patch.dict(
                    os.environ,
                    {
                        "FAVORITE_PLACES_TRUST_CACHE_PATH": str(db_path),
                        "FAVORITE_PLACES_TABELOG_SOURCE_URLS": '{"award":"https://award.tabelog.com/en/restaurants"}',
                        "BRAVE_SEARCH_API_KEY": "",
                        "BRAVE_API_KEY": "",
                        "FAVORITE_PLACES_TRUST_GOOGLE_FALLBACK": "",
                    },
                ),
                patch("scripts.trust_signals.scrape_michelin_region_source", return_value=[]),
                patch(
                    "scripts.trust_signals.scrape_tabelog_source",
                    return_value=[
                        TabelogRestaurant(
                            name="GINZA KOKORO",
                            url="https://tabelog.com/tokyo/A1302/A130202/13249117/",
                            label="The Tabelog Award",
                            tier="Gold",
                            award_year=2026,
                            is_current=True,
                            region_key="japan",
                        )
                    ],
                ),
                patch("scripts.trust_signals.tabelog_search", return_value=[]),
            ):
                summary = refresh_trust_signals_for_raw_guides(
                    root=Path(tmpdir),
                    raw_lists=raw_lists,
                    enrichment_caches={"tokyo-japan": {}},
                    stable_place_ids={("tokyo-japan", 0): "place-1"},
                )

            loaded = TrustSignalStore(sqlite_url_for_path(db_path)).load_signals_for_place_keys(["place-1"])

        self.assertEqual(summary.tabelog_sources_refreshed, 1)
        self.assertEqual(len(loaded["place-1"]), 1)
        self.assertEqual(loaded["place-1"][0].source, "tabelog")
        self.assertEqual(loaded["place-1"][0].label, "The Tabelog Award")
        self.assertEqual(loaded["place-1"][0].tier, "Gold")
        self.assertEqual(loaded["place-1"][0].award_year, 2026)

    def test_refresh_matches_tabelog_hyakumeiten_by_direct_tabelog_search_url(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.sqlite"
            raw_lists = {
                "tokyo-japan": RawSavedList(
                    title="Tokyo, Japan",
                    places=[
                        RawPlace(
                            name="Higashiazabu Amamoto",
                            address="Akabanebashi, Tokyo, Japan",
                            maps_url="https://maps.google.com/?q=higashiazabu-amamoto",
                        )
                    ],
                )
            }

            with (
                patch.dict(
                    os.environ,
                    {
                        "FAVORITE_PLACES_TRUST_CACHE_PATH": str(db_path),
                        "FAVORITE_PLACES_TABELOG_SOURCE_URLS": '{"hyakumeiten":"https://award.tabelog.com/hyakumeiten"}',
                        "BRAVE_SEARCH_API_KEY": "",
                        "BRAVE_API_KEY": "",
                        "FAVORITE_PLACES_TRUST_GOOGLE_FALLBACK": "",
                    },
                ),
                patch("scripts.trust_signals.scrape_michelin_region_source", return_value=[]),
                patch(
                    "scripts.trust_signals.scrape_tabelog_source",
                    return_value=[
                        TabelogRestaurant(
                            name="東麻布 天本",
                            url="https://tabelog.com/tokyo/A1314/A131401/13196420/",
                            label="Tabelog Hyakumeiten",
                            tier="寿司 TOKYO 百名店",
                            award_year=2025,
                            is_current=True,
                            region_key="japan",
                        )
                    ],
                ),
                patch(
                    "scripts.trust_signals.tabelog_search",
                    return_value=[
                        SearchResult(
                            title="Higashiazabu Amamoto",
                            url="https://tabelog.com/en/tokyo/A1314/A131401/13196420/",
                        )
                    ],
                ),
            ):
                summary = refresh_trust_signals_for_raw_guides(
                    root=Path(tmpdir),
                    raw_lists=raw_lists,
                    enrichment_caches={"tokyo-japan": {}},
                    stable_place_ids={("tokyo-japan", 0): "place-1"},
                )

            loaded = TrustSignalStore(sqlite_url_for_path(db_path)).load_signals_for_place_keys(["place-1"])

        self.assertEqual(summary.tabelog_sources_refreshed, 1)
        self.assertEqual(len(loaded["place-1"]), 1)
        self.assertEqual(loaded["place-1"][0].label, "Tabelog Hyakumeiten")
        self.assertEqual(loaded["place-1"][0].tier, "寿司 TOKYO 百名店")
        self.assertEqual(loaded["place-1"][0].match_reason, "Tabelog direct search URL match")

    def test_refresh_preserves_tabelog_rows_when_tabelog_search_fails(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.sqlite"
            now = datetime(2026, 1, 15, tzinfo=UTC)
            existing_signal = TrustSignal(
                source="tabelog",
                label="Tabelog Hyakumeiten",
                tier="寿司 TOKYO 百名店",
                award_year=2025,
                is_current=True,
                url="https://tabelog.com/tokyo/A1314/A131401/13196420/",
                title="東麻布 天本",
                fetched_at=now.isoformat(),
                confidence="high",
                match_reason="Tabelog direct search URL match",
            )
            store = TrustSignalStore(sqlite_url_for_path(db_path))
            store.replace_search_signals(
                "place-1",
                [existing_signal],
                match_signature=trust_match_signature("Higashiazabu Amamoto", "Tokyo", "Japan"),
                now=now,
            )
            raw_lists = {
                "tokyo-japan": RawSavedList(
                    title="Tokyo, Japan",
                    places=[
                        RawPlace(
                            name="Higashiazabu Amamoto",
                            address="Akabanebashi, Tokyo, Japan",
                            maps_url="https://maps.google.com/?q=higashiazabu-amamoto",
                        )
                    ],
                )
            }

            with (
                patch.dict(
                    os.environ,
                    {
                        "FAVORITE_PLACES_TRUST_CACHE_PATH": str(db_path),
                        "FAVORITE_PLACES_TABELOG_SOURCE_URLS": '{"hyakumeiten":"https://award.tabelog.com/hyakumeiten"}',
                        "BRAVE_SEARCH_API_KEY": "",
                        "BRAVE_API_KEY": "",
                        "FAVORITE_PLACES_TRUST_GOOGLE_FALLBACK": "",
                    },
                ),
                patch("scripts.trust_signals.scrape_michelin_region_source", return_value=[]),
                patch(
                    "scripts.trust_signals.scrape_tabelog_source",
                    return_value=[
                        TabelogRestaurant(
                            name="東麻布 天本",
                            url="https://tabelog.com/tokyo/A1314/A131401/13196420/",
                            label="Tabelog Hyakumeiten",
                            tier="寿司 TOKYO 百名店",
                            award_year=2025,
                            is_current=True,
                            region_key="japan",
                        )
                    ],
                ),
                patch("scripts.trust_signals.tabelog_search", side_effect=OSError("blocked")),
            ):
                summary = refresh_trust_signals_for_raw_guides(
                    root=Path(tmpdir),
                    raw_lists=raw_lists,
                    enrichment_caches={"tokyo-japan": {}},
                    stable_place_ids={("tokyo-japan", 0): "place-1"},
                    force_refresh=True,
                )

            loaded = store.load_signals_for_place_keys(["place-1"])

        self.assertEqual(summary.provider_failures, 1)
        self.assertEqual(loaded, {"place-1": [existing_signal]})

    def test_normalize_guide_includes_cached_trust_signals(self) -> None:
        raw = RawSavedList(
            configured_source_type="google_list_url",
            title="Tokyo, Japan",
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
        signal = TrustSignal(
            source="michelin",
            label="MICHELIN Guide",
            tier="Selected",
            url="https://guide.michelin.com/example",
            title="Coffee House - Tokyo - MICHELIN Guide",
            fetched_at="2026-01-15T00:00:00+00:00",
            confidence="high",
            match_reason="name plus source/location match",
        )

        guide = build_data.normalize_guide(
            "tokyo-japan",
            raw,
            enrichment_cache={},
            trust_signals={place_id: [signal]},
        )

        self.assertEqual(guide.places[0].trust_signals, [signal])
        self.assertIsNotNone(guide.places[0].provenance.trust_signals)
        assert guide.places[0].provenance.trust_signals is not None
        self.assertEqual(guide.places[0].provenance.trust_signals.source, "trust_signal")

    def test_search_results_classify_authoritative_trust_sources(self) -> None:
        fetched_at = datetime(2026, 1, 15, tzinfo=UTC)
        results = [
            SearchResult(
                title="Coffee House - Tokyo - a MICHELIN Guide Restaurant",
                url="https://guide.michelin.com/en/jp/tokyo-region/restaurant/coffee-house",
                snippet="Bib Gourmand restaurant in Tokyo.",
            ),
            SearchResult(
                title="The Tabelog Award 2026 Gold Coffee House",
                url="https://award.tabelog.com/en/restaurants/coffee-house",
                snippet="Tokyo restaurant award winner.",
            ),
            SearchResult(
                title="The best cafes in Tokyo",
                url="https://www.timeout.com/tokyo/restaurants/coffee-house",
                snippet="Coffee House is a quiet cafe in Tokyo.",
            ),
        ]

        signals = signals_from_search_results(
            results,
            place_name="Coffee House",
            city_name="Tokyo",
            country_name="Japan",
            fetched_at=fetched_at,
        )

        self.assertEqual([signal.source for signal in signals], ["michelin", "tabelog", "timeout"])
        self.assertEqual(signals[0].tier, "Bib Gourmand")
        self.assertEqual(signals[1].tier, "Gold")
        self.assertEqual(signals[1].award_year, 2026)
        self.assertTrue(all(signal.confidence == "high" for signal in signals))

    def test_tabelog_sources_only_include_japan_guides(self) -> None:
        sources = tabelog_sources_for_guides(
            {
                "tokyo-japan": RawSavedList(title="Tokyo, Japan", places=[]),
                "taipei-taiwan": RawSavedList(title="Taipei, Taiwan", places=[]),
            }
        )

        self.assertEqual(
            sources,
            {
                "tokyo-japan": [
                    TabelogSource(
                        source_type="award",
                        region_key="japan",
                        url="https://award.tabelog.com/en/restaurants",
                    ),
                    TabelogSource(
                        source_type="hyakumeiten",
                        region_key="japan",
                        url="https://award.tabelog.com/hyakumeiten",
                    ),
                ]
            },
        )

    def test_parse_tabelog_award_page_extracts_year_tiers_and_restaurants(self) -> None:
        body = """
        <html><head><title>The list of award winning stores｜The Tabelog Award 2026 [Tabelog]</title></head>
        <body>
          <ul class="award-rstlst__list">
            <li class="award-rstlst__item js-cassette-4row">
              <a class="award-rstlst__target" href="https://tabelog.com/en/tokyo/A1302/A130202/13249117/">
                <span class="award-rstlst__award-label is-gold"><span>GOLD</span></span>
                <div class="award-rstlst__rst-name">GINZA KOKORO</div>
              </a>
            </li>
            <li class="award-rstlst__item">
              <a class="award-rstlst__target" href="https://tabelog.com/en/hyogo/A2803/A280302/28000052/">
                <span class="award-rstlst__award-label is-silver"><span>SILVER</span></span>
                <span class="award-rstlst__award-label is-regional"><span>BEST REGIONAL RESTAURANTS</span></span>
                <div class="award-rstlst__rst-name">Kobe Beef House</div>
              </a>
            </li>
          </ul>
        </body></html>
        """

        restaurants = parse_tabelog_award_page(
            body,
            region_key="japan",
            page_url="https://award.tabelog.com/en/restaurants",
        )

        self.assertEqual(
            [(restaurant.name, restaurant.tier, restaurant.award_year) for restaurant in restaurants],
            [
                ("GINZA KOKORO", "Gold", 2026),
                ("Kobe Beef House", "Silver", 2026),
                ("Kobe Beef House", "Best Regional Restaurants", 2026),
            ],
        )
        self.assertEqual(restaurants[0].label, "The Tabelog Award")
        self.assertEqual(
            restaurants[0].url,
            "https://tabelog.com/tokyo/A1302/A130202/13249117/",
        )

    def test_parse_tabelog_hyakumeiten_page_extracts_category_year_and_restaurants(self) -> None:
        body = """
        <html><head><title>食べログ カレー TOKYO 百名店 2026 [食べログ]</title></head>
        <body>
          <div class="hyakumeiten-shop__item">
            <a class="hyakumeiten-shop__target" href="https://tabelog.com/tokyo/A1302/A130203/13003029/">
              <div class="hyakumeiten-shop__name">Delhi</div>
            </a>
          </div>
        </body></html>
        """

        restaurants = parse_tabelog_hyakumeiten_page(
            body,
            region_key="japan",
            page_url="https://award.tabelog.com/hyakumeiten/curry_tokyo",
        )

        self.assertEqual(len(restaurants), 1)
        self.assertEqual(restaurants[0].name, "Delhi")
        self.assertEqual(restaurants[0].label, "Tabelog Hyakumeiten")
        self.assertEqual(restaurants[0].tier, "カレー TOKYO 百名店")
        self.assertEqual(restaurants[0].award_year, 2026)

    def test_parse_tabelog_search_results_extracts_english_restaurant_urls(self) -> None:
        body = """
        <div class="list-rst" data-detail-url="https://tabelog.com/en/tokyo/A1314/A131401/13196420/">
          <h3 class="list-rst__rst-name">
            <a class="list-rst__rst-name-target" href="https://tabelog.com/en/tokyo/A1314/A131401/13196420/">
              Higashiazabu Amamoto
            </a>
          </h3>
        </div>
        """

        results = parse_tabelog_search_results(body, page_url="https://tabelog.com/en/rstLst/")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Higashiazabu Amamoto")
        self.assertEqual(results[0].url, "https://tabelog.com/tokyo/A1314/A131401/13196420/")

    def test_tabelog_hyakumeiten_category_urls_extracts_unique_category_pages(self) -> None:
        body = """
        <a href="/hyakumeiten">Top</a>
        <a href="/hyakumeiten/curry_tokyo">TOKYO</a>
        <a href="/hyakumeiten/curry_tokyo">Duplicate</a>
        <a href="/hyakumeiten/ramen_osaka">OSAKA</a>
        """

        self.assertEqual(
            tabelog_hyakumeiten_category_urls(
                body,
                page_url="https://award.tabelog.com/hyakumeiten",
            ),
            [
                "https://award.tabelog.com/hyakumeiten/curry_tokyo",
                "https://award.tabelog.com/hyakumeiten/ramen_osaka",
            ],
        )

    def test_tabelog_restaurants_match_japan_places(self) -> None:
        signals = signals_from_tabelog_restaurants(
            [
                TabelogRestaurant(
                    name="GINZA KOKORO",
                    url="https://tabelog.com/tokyo/A1302/A130202/13249117/",
                    label="The Tabelog Award",
                    tier="Gold",
                    award_year=2026,
                    is_current=True,
                    region_key="japan",
                )
            ],
            context=MichelinMatchContext(
                place_name="GINZA KOKORO",
                city_name="Tokyo",
                country_name="Japan",
                address="Ginza, Tokyo, Japan",
            ),
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "tabelog")
        self.assertEqual(signals[0].label, "The Tabelog Award")
        self.assertEqual(signals[0].tier, "Gold")
        self.assertEqual(signals[0].award_year, 2026)
        self.assertTrue(signals[0].is_current)

    def test_tabelog_search_results_match_snapshot_urls(self) -> None:
        signals = signals_from_tabelog_search_results(
            [
                TabelogRestaurant(
                    name="東麻布 天本",
                    url="https://tabelog.com/tokyo/A1314/A131401/13196420/",
                    label="Tabelog Hyakumeiten",
                    tier="寿司 TOKYO 百名店",
                    award_year=2025,
                    is_current=True,
                    region_key="japan",
                )
            ],
            [
                SearchResult(
                    title="Higashiazabu Amamoto",
                    url="https://tabelog.com/en/tokyo/A1314/A131401/13196420/",
                )
            ],
            place_name="Higashiazabu Amamoto",
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].label, "Tabelog Hyakumeiten")
        self.assertEqual(signals[0].tier, "寿司 TOKYO 百名店")
        self.assertEqual(signals[0].award_year, 2025)
        self.assertEqual(signals[0].match_reason, "Tabelog direct search URL match")

    def test_tabelog_restaurants_do_not_fuzzy_match_short_names(self) -> None:
        signals = signals_from_tabelog_restaurants(
            [
                TabelogRestaurant(
                    name="KAI",
                    url="https://tabelog.com/kagoshima/A4602/A460204/46016438/",
                    label="The Tabelog Award",
                    tier="Bronze",
                    award_year=2026,
                    is_current=True,
                    region_key="japan",
                )
            ],
            context=MichelinMatchContext(
                place_name="Kabi",
                city_name="Tokyo",
                country_name="Japan",
                address="Meguro, Tokyo, Japan",
            ),
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(signals, [])

    def test_tabelog_restaurants_require_real_location_evidence_for_fuzzy_matches(self) -> None:
        restaurant = TabelogRestaurant(
            name="Ginza Kokolo",
            url="https://tabelog.com/osaka/A2701/A270101/27000001/",
            label="The Tabelog Award",
            tier="Bronze",
            award_year=2026,
            is_current=True,
            region_key="japan",
        )

        signals = signals_from_tabelog_restaurants(
            [restaurant],
            context=MichelinMatchContext(
                place_name="GINZA KOKORO",
                city_name="Tokyo",
                country_name="Japan",
                address="Ginza, Tokyo, Japan",
            ),
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(signals, [])

        signals_with_location = signals_from_tabelog_restaurants(
            [restaurant.model_copy(update={"url": "https://tabelog.com/tokyo/A1302/A130202/13249117/"})],
            context=MichelinMatchContext(
                place_name="GINZA KOKORO",
                city_name="Tokyo",
                country_name="Japan",
                address="Ginza, Tokyo, Japan",
            ),
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(len(signals_with_location), 1)
        self.assertEqual(signals_with_location[0].confidence, "medium")

    def test_michelin_region_sources_only_include_relevant_guides(self) -> None:
        sources = michelin_region_sources_for_guides(
            {
                "tokyo-japan": RawSavedList(title="Tokyo, Japan", places=[]),
                "taipei-taiwan": RawSavedList(title="Taipei, Taiwan", places=[]),
                "hong-kong-wanderlog-example": RawSavedList(title="DEM Flyers Hong Kong recommendations", places=[]),
            }
        )

        self.assertEqual(
            sources,
            {
                "tokyo-japan": [
                    MichelinRegionSource(
                        source_type="official",
                        region_key="japan/tokyo",
                        url="https://guide.michelin.com/en/jp/tokyo-region/restaurants",
                    )
                ],
                "taipei-taiwan": [
                    MichelinRegionSource(
                        source_type="official",
                        region_key="taiwan/taipei",
                        url="https://guide.michelin.com/tw/en/article/michelin-guide-ceremony/taiwan-full-list",
                    ),
                    MichelinRegionSource(
                        source_type="wikipedia",
                        region_key="taiwan/wikipedia",
                        url="https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Taiwan",
                    )
                ],
                "hong-kong-wanderlog-example": [
                    MichelinRegionSource(
                        source_type="official",
                        region_key="hong kong/hong kong",
                        url="https://guide.michelin.com/en/hk/hong-kong-region/hong-kong/restaurants",
                    ),
                    MichelinRegionSource(
                        source_type="wikipedia",
                        region_key="hong kong/wikipedia",
                        url="https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Hong_Kong_and_Macau",
                    ),
                ],
            },
        )

    def test_region_sources_use_override_location_context(self) -> None:
        raw_lists = {
            "dinner-list": RawSavedList(title="Restaurants", places=[]),
        }
        guide_location_contexts = {
            "dinner-list": ("Tokyo", "Japan"),
        }

        michelin_sources = michelin_region_sources_for_guides(
            raw_lists,
            guide_location_contexts=guide_location_contexts,
        )
        tabelog_sources = tabelog_sources_for_guides(
            raw_lists,
            guide_location_contexts=guide_location_contexts,
        )

        self.assertEqual(
            michelin_sources,
            {
                "dinner-list": [
                    MichelinRegionSource(
                        source_type="official",
                        region_key="japan/tokyo",
                        url="https://guide.michelin.com/en/jp/tokyo-region/restaurants",
                    )
                ],
            },
        )
        self.assertEqual(
            [source.source_type for source in tabelog_sources["dinner-list"]],
            ["award", "hyakumeiten"],
        )

    def test_parse_wikipedia_michelin_starred_page_extracts_yeared_stars(self) -> None:
        body = """
        <table class="wikitable sortable plainrowheaders">
        <caption>Michelin-starred restaurants</caption>
        <tbody>
        <tr><th>Name</th><th>Cuisine</th><th>Location</th><th>2024</th><th>2025</th></tr>
        <tr>
          <th scope="row"><a href="/wiki/Lazy_Bear">Lazy Bear</a></th>
          <td>Modern</td><td>Taipei</td>
          <td><img alt="1 Michelin star" /></td>
          <td><img alt="2 Michelin stars" /></td>
        </tr>
        <tr>
          <th scope="row">No Star Cafe</th>
          <td>Cafe</td><td>Taipei</td>
          <td>—</td><td>—</td>
        </tr>
        <tr>
          <th scope="row"><a href="/w/index.php?title=Redlink_Cafe&amp;action=edit&amp;redlink=1">Redlink Cafe</a></th>
          <td>Cafe</td><td>Taipei</td>
          <td><img alt="1 Michelin star" /></td><td>—</td>
        </tr>
        </tbody>
        </table>
        """

        restaurants = parse_wikipedia_michelin_starred_page(
            body,
            region_key="taiwan/wikipedia",
            page_url="https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Taiwan",
        )

        self.assertEqual([restaurant.award_year for restaurant in restaurants], [2024, 2025, 2024])
        self.assertEqual([restaurant.tier for restaurant in restaurants], ["1 star", "2 stars", "1 star"])
        self.assertEqual(restaurants[0].name, "Lazy Bear")
        self.assertFalse(restaurants[0].is_current)
        self.assertEqual(restaurants[0].url, "https://en.wikipedia.org/wiki/Lazy_Bear")
        self.assertEqual(
            restaurants[2].url,
            "https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Taiwan",
        )

    def test_parse_michelin_full_list_article_extracts_current_yeared_awards(self) -> None:
        body = """
        <meta itemprop="name" content="The Full List of the MICHELIN Guide Taiwan 2025">
        <h2><b>43 One-MICHELIN-Star Restaurants:</b></h2>
        <h3>Taipei</h3>
        <p>
          A Cut <br>
          Ad Astra <br>
          <strong><a href="https://guide.michelin.com/tw/en/taipei-region/taipei/restaurant/amaze">aMaze</a> (NEW)</strong>
        </p>
        <h2><b>6 MICHELIN Green Star Restaurants:</b></h2>
        <h3>Taipei</h3>
        <p>EMBERS <br> Hosu </p>
        <h2><b>222 Selected Restaurants</b></h2>
        <h3>Taipei</h3>
        <p>Longtail <br> Mume </p>
        """

        restaurants = parse_michelin_full_list_article_page(
            body,
            region_key="taiwan/taipei",
            page_url="https://guide.michelin.com/tw/en/article/michelin-guide-ceremony/taiwan-full-list",
        )

        ad_astra = next(restaurant for restaurant in restaurants if restaurant.name == "Ad Astra")
        amaze = next(restaurant for restaurant in restaurants if restaurant.name == "aMaze")
        self.assertEqual(ad_astra.tier, "1 star")
        self.assertEqual(ad_astra.award_year, 2025)
        self.assertTrue(ad_astra.is_current)
        self.assertEqual(
            ad_astra.url,
            "https://guide.michelin.com/tw/en/article/michelin-guide-ceremony/taiwan-full-list",
        )
        self.assertEqual(
            amaze.url,
            "https://guide.michelin.com/en/taipei-region/taipei/restaurant/amaze",
        )
        embers = next(restaurant for restaurant in restaurants if restaurant.name == "EMBERS")
        self.assertEqual(embers.tier, "Green Star")
        self.assertIn("Longtail", [restaurant.name for restaurant in restaurants])

    def test_parse_michelin_region_page_extracts_restaurants(self) -> None:
        body = """
        <div class="card__menu">
          <div data-dtm-distinction="1 star" data-green-star="true" data-restaurant-name="ESTERRE by Alain Ducasse"></div>
          <a href="/en/jp/tokyo-region/restaurant/esterre-by-alain-ducasse">Open</a>
        </div>
        <div class="card__menu">
          <div data-dtm-distinction="" data-restaurant-name="Chez Olivier"></div>
          <a href="/en/jp/tokyo-region/restaurant/chez-olivier">Open</a>
        </div>
        """

        restaurants = parse_michelin_region_page(
            body,
            region_key="japan/tokyo",
            page_url="https://guide.michelin.com/en/jp/tokyo-region/restaurants",
        )

        self.assertEqual(
            [(restaurant.name, restaurant.tier) for restaurant in restaurants],
            [
                ("ESTERRE by Alain Ducasse", "1 star"),
                ("ESTERRE by Alain Ducasse", "Green Star"),
                ("Chez Olivier", "Selected"),
            ],
        )
        esterre_star = next(
            restaurant
            for restaurant in restaurants
            if restaurant.name == "ESTERRE by Alain Ducasse" and restaurant.tier == "1 star"
        )
        self.assertIsNone(esterre_star.award_year)
        self.assertTrue(esterre_star.is_current)
        self.assertEqual(
            esterre_star.url,
            "https://guide.michelin.com/en/tokyo-region/restaurant/esterre-by-alain-ducasse",
        )

    def test_parse_michelin_region_page_binds_preceding_href_to_same_card(self) -> None:
        body = """
        <div class="card__menu">
          <a href="/en/tokyo-region/restaurant/previous">Open</a>
          <div data-dtm-distinction="1 star" data-restaurant-name="Previous"></div>
        </div>
        <div class="card__menu">
          <a href="/en/tokyo-region/restaurant/current">Open</a>
          <div data-dtm-distinction="Bib Gourmand" data-restaurant-name="Current"></div>
        </div>
        """

        restaurants = parse_michelin_region_page(
            body,
            region_key="japan/tokyo",
            page_url="https://guide.michelin.com/en/jp/tokyo-region/restaurants",
        )

        current = next(restaurant for restaurant in restaurants if restaurant.name == "Current")
        self.assertEqual(current.url, "https://guide.michelin.com/en/tokyo-region/restaurant/current")
        self.assertEqual(current.tier, "Bib Gourmand")

    def test_scrape_official_michelin_region_preserves_co_awards(self) -> None:
        body = """
        <div class="card__menu">
          <div data-dtm-distinction="1 star" data-green-star="true" data-restaurant-name="ESTERRE by Alain Ducasse"></div>
          <a href="/en/jp/tokyo-region/restaurant/esterre-by-alain-ducasse">Open</a>
        </div>
        """
        source = MichelinRegionSource(
            source_type="official",
            region_key="japan/tokyo",
            url="https://guide.michelin.com/en/jp/tokyo-region/restaurants",
        )

        with (
            patch("scripts.trust_signals.fetch_text", return_value=body),
            patch("scripts.trust_signals.michelin_next_page_url", return_value=None),
        ):
            restaurants = scrape_official_michelin_region(source)

        self.assertEqual(
            sorted(restaurant.tier for restaurant in restaurants),
            ["1 star", "Green Star"],
        )

    def test_parse_michelin_region_page_extracts_singular_three_star_distinction(self) -> None:
        body = """
        <div class="card__menu">
          <div data-dtm-distinction="3 star" data-restaurant-name="Quintessence"></div>
          <a href="/en/tokyo-region/tokyo/restaurant/quintessence-1193907">Open</a>
        </div>
        """

        restaurants = parse_michelin_region_page(
            body,
            region_key="japan/tokyo",
            page_url="https://guide.michelin.com/en/jp/tokyo-region/restaurants/3-stars-michelin",
        )

        self.assertEqual(len(restaurants), 1)
        self.assertEqual(restaurants[0].name, "Quintessence")
        self.assertEqual(restaurants[0].tier, "3 stars")

    def test_parse_michelin_detail_page_extracts_date_awarded_year(self) -> None:
        body = """
        <script>
        dLayer['distinction'] = '3 star';
        dLayer['dateAwarded'] = '2026';
        </script>
        <span>Three Stars: Exceptional cuisine</span>
        """

        self.assertEqual(
            parse_michelin_detail_page_award_year(
                body,
                page_url="https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/quintessence-1193907",
            ),
            2026,
        )

    def test_michelin_signal_award_year_enrichment_fetches_once_per_matched_url(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.sqlite"
            store = TrustSignalStore(sqlite_url_for_path(db_path))
            restaurant_url = "https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/quintessence-1193907"
            signals = [
                TrustSignal(
                    source="michelin",
                    label="MICHELIN Guide",
                    tier="3 stars",
                    url=restaurant_url,
                    title="Quintessence",
                    fetched_at=datetime(2026, 1, 15, tzinfo=UTC).isoformat(),
                    confidence="high",
                    match_reason="Michelin name exact match",
                    is_current=True,
                ),
                TrustSignal(
                    source="michelin",
                    label="MICHELIN Guide",
                    tier="Green Star",
                    url=restaurant_url,
                    title="Quintessence",
                    fetched_at=datetime(2026, 1, 15, tzinfo=UTC).isoformat(),
                    confidence="high",
                    match_reason="Michelin name exact match",
                    is_current=True,
                ),
            ]

            with patch(
                "scripts.trust_signals.fetch_text",
                return_value="<script>dLayer['dateAwarded'] = '2026';</script>",
            ) as fetch_text:
                result = enrich_michelin_signal_award_years(
                    store,
                    signals,
                    now=datetime(2026, 1, 15, tzinfo=UTC),
                )

            self.assertEqual(fetch_text.call_count, 1)
            self.assertEqual(result.fetched_details, 1)
            self.assertEqual(result.changed_signals, 2)
            self.assertEqual([signal.award_year for signal in result.signals], [2026, 2026])

            with patch("scripts.trust_signals.fetch_text") as fetch_text:
                cached_result = enrich_michelin_signal_award_years(
                    store,
                    signals,
                    now=datetime(2026, 1, 16, tzinfo=UTC),
                )

            fetch_text.assert_not_called()
            self.assertEqual(cached_result.fetched_details, 0)
            self.assertEqual([signal.award_year for signal in cached_result.signals], [2026, 2026])

    def test_parse_michelin_region_page_dedupes_localized_restaurant_urls(self) -> None:
        body = """
        <div class="card__menu">
          <div data-dtm-distinction="1 star" data-restaurant-name="Kabi"></div>
          <a href="/at/de/tokyo-region/tokyo/restaurant/kabi">Open</a>
          <a href="/ae-az/en/tokyo-region/tokyo/restaurant/kabi">Open</a>
          <a href="/en/tokyo-region/tokyo/restaurant/kabi">Open</a>
        </div>
        """

        restaurants = parse_michelin_region_page(
            body,
            region_key="japan/tokyo",
            page_url="https://guide.michelin.com/en/jp/tokyo-region/restaurants",
        )

        self.assertEqual(len(restaurants), 1)
        self.assertEqual(
            restaurants[0].url,
            "https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/kabi",
        )

    def test_trust_store_url_has_readable_cache_uses_sqlalchemy_sqlite_paths(self) -> None:
        previous_cwd = Path.cwd()
        with TemporaryDirectory() as tmpdir:
            try:
                os.chdir(tmpdir)
                Path("relative trust.sqlite").touch()

                self.assertTrue(
                    build_data.trust_store_url_has_readable_cache(
                        "sqlite:///relative%20trust.sqlite?mode=ro"
                    )
                )
                self.assertFalse(
                    build_data.trust_store_url_has_readable_cache(
                        "sqlite:///missing%20trust.sqlite?mode=ro"
                    )
                )
            finally:
                os.chdir(previous_cwd)

    def test_michelin_region_restaurants_match_places(self) -> None:
        restaurants = parse_michelin_region_page(
            """
            <div data-dtm-distinction="bib gourmand" data-restaurant-name="Coffee House"></div>
            <a href="/en/jp/tokyo-region/restaurant/coffee-house">Open</a>
            """,
            region_key="japan/tokyo",
            page_url="https://guide.michelin.com/en/jp/tokyo-region/restaurants",
        )

        signals = signals_from_michelin_region(
            restaurants,
            context="Coffee House",
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "michelin")
        self.assertEqual(signals[0].tier, "Bib Gourmand")
        self.assertEqual(signals[0].confidence, "high")
        self.assertTrue(signals[0].is_current)

    def test_michelin_similarity_matches_restaurant_alias_with_location(self) -> None:
        restaurants = [
            MichelinRestaurant(
                name="A Cut",
                url="https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Taiwan",
                tier="1 star",
                award_year=2025,
                is_current=False,
                region_key="taiwan/wikipedia",
            )
        ]

        signals = signals_from_michelin_region(
            restaurants,
            context=MichelinMatchContext(
                place_name="A Cut Steakhouse",
                city_name="Taipei",
                country_name="Taiwan",
                address="Taipei, Taiwan",
            ),
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].confidence, "medium")
        self.assertEqual(signals[0].match_reason, "Michelin name alias plus location match")

    def test_michelin_similarity_rejects_single_token_alias(self) -> None:
        restaurants = [
            MichelinRestaurant(
                name="A",
                url="https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Taiwan",
                tier="1 star",
                award_year=2025,
                is_current=False,
                region_key="taiwan/wikipedia",
            )
        ]

        signals = signals_from_michelin_region(
            restaurants,
            context=MichelinMatchContext(
                place_name="A Cut Steakhouse",
                city_name="Taipei",
                country_name="Taiwan",
            ),
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(signals, [])

    def test_current_michelin_signal_sorts_before_previous_history(self) -> None:
        restaurants = [
            MichelinRestaurant(
                name="Ad Astra",
                url="https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Taiwan",
                tier="1 star",
                award_year=2023,
                is_current=False,
                region_key="taiwan/wikipedia",
            ),
            MichelinRestaurant(
                name="Ad Astra",
                url="https://guide.michelin.com/tw/en/article/michelin-guide-ceremony/taiwan-full-list",
                tier="1 star",
                award_year=2025,
                is_current=True,
                region_key="taiwan/taipei",
            ),
        ]

        signals = signals_from_michelin_region(
            restaurants,
            context=MichelinMatchContext(
                place_name="Ad Astra",
                city_name="Taipei",
                country_name="Taiwan",
            ),
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(signals[0].award_year, 2025)
        self.assertTrue(signals[0].is_current)
        self.assertEqual(signals[1].award_year, 2023)
        self.assertFalse(signals[1].is_current)

    def test_higher_current_michelin_tier_sorts_before_selected(self) -> None:
        restaurants = [
            MichelinRestaurant(
                name="Quintessence",
                url="https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/quintessence-1193907",
                tier="Selected",
                is_current=True,
                region_key="japan/tokyo",
            ),
            MichelinRestaurant(
                name="Quintessence",
                url="https://guide.michelin.com/en/tokyo-region/tokyo/restaurant/quintessence-1193907",
                tier="3 stars",
                award_year=2026,
                is_current=True,
                region_key="japan/tokyo",
            ),
        ]

        signals = signals_from_michelin_region(
            restaurants,
            context=MichelinMatchContext(
                place_name="Quintessence",
                city_name="Tokyo",
                country_name="Japan",
            ),
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(signals[0].tier, "3 stars")
        self.assertEqual(signals[0].award_year, 2026)
        self.assertEqual(signals[1].tier, "Selected")

    def test_michelin_next_page_url_extracts_region_pagination(self) -> None:
        body = '<a href="/en/jp/tokyo-region/restaurants/page/2" class="btn">Next</a>'

        self.assertEqual(
            michelin_next_page_url(
                body,
                page_url="https://guide.michelin.com/en/jp/tokyo-region/restaurants",
            ),
            "https://guide.michelin.com/en/jp/tokyo-region/restaurants/page/2",
        )

    def test_google_search_parser_extracts_redirect_result_urls(self) -> None:
        body = """
        <html><body>
          <a href="/url?q=https%3A%2F%2Fguide.michelin.com%2Fen%2Fjp%2Ftokyo-region%2Frestaurant%2Fcoffee-house&sa=U">
            <h3>Coffee House - Tokyo - MICHELIN Guide</h3>
          </a>
          <a href="/search?q=Coffee+House">Google internal</a>
        </body></html>
        """

        results = parse_google_search_results(body)

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].url,
            "https://guide.michelin.com/en/jp/tokyo-region/restaurant/coffee-house",
        )
        self.assertEqual(results[0].title, "Coffee House - Tokyo - MICHELIN Guide")

    def test_tabelog_signals_are_japan_only(self) -> None:
        signals = signals_from_search_results(
            [
                SearchResult(
                    title="The Tabelog Award 2026 Gold Coffee House",
                    url="https://award.tabelog.com/en/restaurants/coffee-house",
                    snippet="Restaurant award winner.",
                )
            ],
            place_name="Coffee House",
            city_name="Taipei",
            country_name="Taiwan",
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

        self.assertEqual(signals, [])

    def test_infer_award_year_from_url_or_title(self) -> None:
        self.assertEqual(infer_award_year("The Tabelog Award 2026 Gold"), 2026)
        self.assertEqual(infer_award_year("https://award.tabelog.com/en/2025"), 2025)
        self.assertIsNone(infer_award_year("MICHELIN Guide current selection"))

    def test_trust_signal_recommendation_copy_describes_awards(self) -> None:
        self.assertEqual(
            build_data.trust_signal_recommendation_copy(
                [
                    TrustSignal(
                        source="michelin",
                        label="MICHELIN Guide",
                        tier="1 star",
                        award_year=2025,
                        is_current=False,
                        fetched_at="2026-01-15T00:00:00+00:00",
                        confidence="high",
                        match_reason="Michelin name exact match",
                    )
                ]
            ),
            "Previously recognized by MICHELIN Guide 1 star 2025.",
        )

    def test_tabelog_hyakumeiten_display_uses_english_copy(self) -> None:
        signal = build_data.display_trust_signal(
            TrustSignal(
                source="tabelog",
                label="Tabelog Hyakumeiten",
                tier="寿司 TOKYO 百名店",
                award_year=2025,
                is_current=True,
                fetched_at="2026-01-15T00:00:00+00:00",
                confidence="high",
                match_reason="Tabelog direct search URL match",
            )
        )

        self.assertEqual(signal.label, "Tabelog Hyakumeiten")
        self.assertEqual(signal.tier, "寿司 TOKYO 百名店")
        self.assertEqual(signal.display_label, "Tabelog 100")
        self.assertEqual(signal.display_tier, "Sushi")
        self.assertEqual(
            build_data.trust_signal_recommendation_copy([signal]),
            "Recognized by Tabelog 100 Sushi 2025.",
        )

    def test_tabelog_award_display_removes_leading_the(self) -> None:
        signal = build_data.display_trust_signal(
            TrustSignal(
                source="tabelog",
                label="The Tabelog Award",
                tier="Gold",
                award_year=2026,
                is_current=True,
                fetched_at="2026-01-15T00:00:00+00:00",
                confidence="high",
                match_reason="Tabelog name exact match",
            )
        )

        self.assertEqual(signal.label, "The Tabelog Award")
        self.assertEqual(signal.display_label, "Tabelog")
        self.assertEqual(
            build_data.trust_signal_recommendation_copy([signal]),
            "Recognized by Tabelog Gold 2026.",
        )

    def test_trust_signal_recommendation_copy_skips_previous_when_current_exists(self) -> None:
        self.assertEqual(
            build_data.trust_signal_recommendation_copy(
                [
                    TrustSignal(
                        source="michelin",
                        label="MICHELIN Guide",
                        tier="1 star",
                        award_year=2025,
                        is_current=True,
                        fetched_at="2026-01-15T00:00:00+00:00",
                        confidence="high",
                        match_reason="Michelin name exact match",
                    ),
                    TrustSignal(
                        source="michelin",
                        label="MICHELIN Guide",
                        tier="1 star",
                        award_year=2023,
                        is_current=False,
                        fetched_at="2026-01-15T00:00:00+00:00",
                        confidence="high",
                        match_reason="Michelin name exact match",
                    ),
                ]
            ),
            "Recognized by MICHELIN Guide 1 star 2025.",
        )

    def test_trust_signal_recommendation_copy_describes_mixed_current_and_previous_awards(self) -> None:
        self.assertEqual(
            build_data.trust_signal_recommendation_copy(
                [
                    TrustSignal(
                        source="tabelog",
                        label="The Tabelog Award",
                        tier="Bronze",
                        award_year=2025,
                        is_current=True,
                        fetched_at="2026-01-15T00:00:00+00:00",
                        confidence="high",
                        match_reason="Tabelog name exact match",
                    ),
                    TrustSignal(
                        source="michelin",
                        label="MICHELIN Guide",
                        tier="1 star",
                        award_year=2023,
                        is_current=False,
                        fetched_at="2026-01-15T00:00:00+00:00",
                        confidence="high",
                        match_reason="Michelin name exact match",
                    ),
                ]
            ),
            "Recognized by Tabelog Bronze 2025 and previously by MICHELIN Guide 1 star 2023.",
        )

    def test_trust_signal_recommendation_copy_preserves_previous_michelin_star_when_current_tier_differs(self) -> None:
        self.assertEqual(
            build_data.trust_signal_recommendation_copy(
                [
                    TrustSignal(
                        source="michelin",
                        label="MICHELIN Guide",
                        tier="Selected",
                        award_year=2026,
                        is_current=True,
                        fetched_at="2026-01-15T00:00:00+00:00",
                        confidence="high",
                        match_reason="Michelin name exact match",
                    ),
                    TrustSignal(
                        source="michelin",
                        label="MICHELIN Guide",
                        tier="1 star",
                        award_year=2024,
                        is_current=False,
                        fetched_at="2026-01-15T00:00:00+00:00",
                        confidence="high",
                        match_reason="Michelin name exact match",
                    ),
                ]
            ),
            "Recognized by MICHELIN Guide Selected 2026 and previously by MICHELIN Guide 1 star 2024.",
        )


if __name__ == "__main__":
    unittest.main()

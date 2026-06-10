from __future__ import annotations

from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import unittest

from scripts.pipeline_models import TrustSignal
from scripts.pipeline_models import RawPlace, RawSavedList
from scripts import build_data
from scripts.trust_signals import (
    MichelinRegionSource,
    MichelinRestaurant,
    MichelinMatchContext,
    SearchResult,
    TrustSignalStore,
    default_trust_cache_path,
    michelin_next_page_url,
    michelin_region_sources_for_guides,
    parse_michelin_full_list_article_page,
    parse_michelin_detail_page_award_year,
    parse_michelin_region_page,
    parse_wikipedia_michelin_starred_page,
    parse_google_search_results,
    scrape_official_michelin_region,
    enrich_michelin_signal_award_years,
    infer_award_year,
    signals_from_michelin_region,
    signals_from_search_results,
    sqlite_url_for_path,
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
            "Recognized by The Tabelog Award Bronze 2025 and previously by MICHELIN Guide 1 star 2023.",
        )


if __name__ == "__main__":
    unittest.main()

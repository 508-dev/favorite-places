from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gmaps_scraper import (
    BrowserProxyConfig,
    BrowserSessionConfig,
    HttpSessionConfig,
    ListOwner,
    LLMRepairError,
    ParseError,
    Place,
    PlaceAboutItem,
    PlaceAboutSection,
    PlaceDetails,
    PlaceExtractionDiagnostics,
    PlaceLLMRepairRequest,
    PlaceOpeningHours,
    PlaceReview,
    PlaceScrapeResult,
    ReviewTopic,
    SavedList,
    SavedListExtractionDiagnostics,
    SavedListScrapeResult,
    ScrapeError,
    build_maps_search_url,
    cached_place_repairer,
    collect_place_snapshot,
    default_place_selector_recipe,
    llm_cache_namespace_from_env,
    load_place_selector_recipe,
    needs_display_en,
    openai_compatible_place_repairer_from_env,
    repair_place_display_fields,
    reusable_place_display_fields,
    reuse_place_display_fields,
    scrape_place,
    scrape_places,
    scrape_saved_list,
    scrape_saved_lists,
    write_default_place_selector_recipe,
)


class PublicApiTests(unittest.TestCase):
    def test_legacy_place_selector_recipe_can_still_be_read_and_written(self) -> None:
        recipe = default_place_selector_recipe()
        self.assertEqual(recipe["version"], 1)
        self.assertEqual(
            recipe["selectors"]["name"], ["h1.DUwDvf", "h1.lfPIob", "div[role='main'] h1"]
        )
        self.assertIn("review_count", recipe["selectors"])
        self.assertIn("review_topic_chip", recipe["selectors"])

        recipe["selectors"]["name"].clear()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "archive" / "selector-recipe.json"
            self.assertEqual(write_default_place_selector_recipe(path), path)
            self.assertEqual(load_place_selector_recipe(path), default_place_selector_recipe())
            self.assertTrue(default_place_selector_recipe()["selectors"]["name"])

    def test_top_level_exports_are_importable(self) -> None:
        self.assertTrue(callable(scrape_saved_list))
        self.assertTrue(callable(scrape_saved_lists))
        self.assertTrue(callable(scrape_place))
        self.assertTrue(callable(scrape_places))
        self.assertEqual(BrowserSessionConfig.__name__, "BrowserSessionConfig")
        self.assertEqual(BrowserProxyConfig.__name__, "BrowserProxyConfig")
        self.assertEqual(HttpSessionConfig.__name__, "HttpSessionConfig")
        self.assertEqual(LLMRepairError.__name__, "LLMRepairError")
        self.assertEqual(ListOwner.__name__, "ListOwner")
        self.assertEqual(PlaceAboutItem.__name__, "PlaceAboutItem")
        self.assertEqual(PlaceAboutSection.__name__, "PlaceAboutSection")
        self.assertEqual(PlaceExtractionDiagnostics.__name__, "PlaceExtractionDiagnostics")
        self.assertEqual(SavedListExtractionDiagnostics.__name__, "SavedListExtractionDiagnostics")
        self.assertEqual(PlaceLLMRepairRequest.__name__, "PlaceLLMRepairRequest")
        self.assertEqual(PlaceOpeningHours.__name__, "PlaceOpeningHours")
        self.assertEqual(PlaceReview.__name__, "PlaceReview")
        self.assertEqual(PlaceScrapeResult.__name__, "PlaceScrapeResult")
        self.assertEqual(SavedListScrapeResult.__name__, "SavedListScrapeResult")
        self.assertEqual(ReviewTopic.__name__, "ReviewTopic")
        self.assertTrue(issubclass(ParseError, RuntimeError))
        self.assertTrue(issubclass(ScrapeError, RuntimeError))
        self.assertTrue(callable(build_maps_search_url))
        self.assertTrue(callable(collect_place_snapshot))
        self.assertTrue(callable(cached_place_repairer))
        self.assertTrue(callable(default_place_selector_recipe))
        self.assertTrue(callable(load_place_selector_recipe))
        self.assertTrue(callable(llm_cache_namespace_from_env))
        self.assertTrue(callable(needs_display_en))
        self.assertTrue(callable(openai_compatible_place_repairer_from_env))
        self.assertTrue(callable(repair_place_display_fields))
        self.assertTrue(callable(reusable_place_display_fields))
        self.assertTrue(callable(reuse_place_display_fields))
        self.assertTrue(callable(write_default_place_selector_recipe))

    def test_saved_list_serializes_library_shape(self) -> None:
        place = Place(
            name="Northwind Cafe",
            address="Example District",
            note="Try the seasonal sampler.",
            lat=35.6501307,
            lng=139.6868459,
            maps_url=(
                "https://www.google.com/maps/search/"
                "?api=1&query=Northwind+Cafe%2C+Example+District"
            ),
            is_favorite=True,
            added_by=ListOwner(
                name="Fixture Owner",
                photo_url="https://lh3.googleusercontent.com/a-/fixture-owner",
                profile_id="104356373423434804635",
            ),
        )
        saved_list = SavedList(
            source_url="https://maps.app.goo.gl/TestSavedListShortUrl",
            resolved_url=(
                "https://www.google.com/maps/@30.5370705,125.4120472,6z/"
                "data=!4m3!11m2!2sTESTLISTABC123456789!3e3?entry=ttu"
            ),
            list_id="TESTLISTABC123456789",
            title="Sample Coffee Stops",
            description="Curated fixture data for parser tests",
            places=[place],
            owner=ListOwner(
                name="Fixture Owner",
                photo_url="https://lh3.googleusercontent.com/a-/fixture-owner",
                profile_id="104356373423434804635",
            ),
            collaborators=[
                ListOwner(
                    name="Fixture Collaborator",
                    photo_url="https://lh3.googleusercontent.com/a-/fixture-collaborator",
                    profile_id="205678901234567890123",
                )
            ],
        )

        self.assertEqual(
            saved_list.to_dict(),
            {
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
                "places": [
                    {
                        "name": "Northwind Cafe",
                        "address": "Example District",
                        "note": "Try the seasonal sampler.",
                        "is_favorite": True,
                        "lat": 35.6501307,
                        "lng": 139.6868459,
                        "maps_url": (
                            "https://www.google.com/maps/search/"
                            "?api=1&query=Northwind+Cafe%2C+Example+District"
                        ),
                        "added_by": {
                            "name": "Fixture Owner",
                            "profile_id": "104356373423434804635",
                        },
                    }
                ],
            },
        )

    def test_place_details_omit_missing_fields(self) -> None:
        place = PlaceDetails(
            source_url="https://www.google.com/maps/place/Den",
            resolved_url="https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z",
            google_place_id="ChIJ8T36HxCLGGARvpARPDyaKLA",
            name="Den",
            secondary_name="傳",
            category="Japanese restaurant",
            rating=4.4,
            review_count=324,
            address="Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
            address_display_en=(
                "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 "
                "建築家会館JIA館"
            ),
            address_display_en_source="llm",
            address_display_en_confidence="high",
            status="Closed · Opens 6 PM",
            website="http://www.jimbochoden.com/",
            phone="+81 3-6455-5433",
            plus_code="MPF7+73 Shibuya, Tokyo, Japan",
            address_parts=[
                "2 Chome Jingumae",
                "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                "Jingumae, 2 Chome−3−18 建築家会館ＪＩＡ館",
                "Shibuya",
                "150-0001",
                "Tokyo",
                "JP",
                ["Floor 1"],
            ],
            main_photo_url="https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
            photo_url="https://lh3.googleusercontent.com/p/example=s680-w680-h510",
            lat=35.6731762,
            lng=139.7127216,
            limited_view=True,
            review_topics=[
                ReviewTopic(label="pho", count=501, source="dom"),
                ReviewTopic(label="bun bo nam bo", count=623),
            ],
            reviews=[
                PlaceReview(
                    author="Fixture Reviewer",
                    rating=5.0,
                    relative_time="2 months ago",
                    text="Excellent broth.",
                    source="dom",
                )
            ],
            diagnostics=PlaceExtractionDiagnostics(
                field_sources={"name": "dom", "review_topics": "dom"},
                missing_fields=[],
                quality_flags=[],
                confidence=1.0,
                evidence_hash="fixture",
            ),
        )

        self.assertEqual(
            place.to_dict(),
            {
                "source_url": "https://www.google.com/maps/place/Den",
                "resolved_url": (
                    "https://www.google.com/maps/place/Den/@35.6731762,139.7127216,17z"
                ),
                "google_place_id": "ChIJ8T36HxCLGGARvpARPDyaKLA",
                "name": "Den",
                "category": "Japanese restaurant",
                "rating": 4.4,
                "review_count": 324,
                "address": (
                    "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 "
                    "建築家会館ＪＩＡ館"
                ),
                "address_display_en": (
                    "Japan, 〒150-0001 Tokyo, Shibuya, Jingumae, 2 Chome−3−18 "
                    "建築家会館JIA館"
                ),
                "address_display_en_source": "llm",
                "address_display_en_confidence": "high",
                "status": "Closed · Opens 6 PM",
                "website": "http://www.jimbochoden.com/",
                "phone": "+81 3-6455-5433",
                "plus_code": "MPF7+73 Shibuya, Tokyo, Japan",
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
                "main_photo_url": "https://lh3.googleusercontent.com/p/main-example=s680-w680-h510",
                "photo_url": "https://lh3.googleusercontent.com/p/example=s680-w680-h510",
                "secondary_name": "傳",
                "lat": 35.6731762,
                "lng": 139.7127216,
                "limited_view": True,
                "review_topics": [
                    {"label": "pho", "count": 501},
                    {"label": "bun bo nam bo", "count": 623},
                ],
                "reviews": [
                    {
                        "author": "Fixture Reviewer",
                        "rating": 5.0,
                        "relative_time": "2 months ago",
                        "text": "Excellent broth.",
                    }
                ],
                "diagnostics": {
                    "quality_flags": [],
                    "llm_used": False,
                    "confidence": 1.0,
                    "evidence_hash": "fixture",
                },
            },
        )


if __name__ == "__main__":
    unittest.main()

"""Google Maps scraping helpers."""

import logging

from gmaps_scraper.display_fields import (
    PlaceDisplayFieldRepairer,
    repair_place_display_fields,
    reusable_place_display_fields,
    reuse_place_display_fields,
)
from gmaps_scraper.llm import (
    LLMRepairError,
    cached_place_repairer,
    llm_cache_namespace_from_env,
    openai_compatible_place_repairer_from_env,
)
from gmaps_scraper.models import (
    ListOwner,
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
)
from gmaps_scraper.parser import ParseError, parse_saved_list_artifacts
from gmaps_scraper.place_scraper import (
    PlaceLLMRepairer,
    PlaceLLMTask,
    collect_place_snapshot,
    scrape_place,
    scrape_places,
)
from gmaps_scraper.scraper import (
    BrowserProxyConfig,
    BrowserSessionConfig,
    HttpSessionConfig,
    ScrapeError,
    scrape_saved_list,
    scrape_saved_lists,
)
from gmaps_scraper.selector_recipes import (
    default_place_selector_recipe,
    load_place_selector_recipe,
    write_default_place_selector_recipe,
)
from gmaps_scraper.translation_memory import needs_display_en
from gmaps_scraper.url_tools import (
    PLACELIST_URL_MARKER,
    build_maps_search_url,
    extract_list_id,
    extract_list_id_from_text,
    has_placelist_marker,
    localize_maps_url,
)

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "PLACELIST_URL_MARKER",
    "BrowserProxyConfig",
    "BrowserSessionConfig",
    "HttpSessionConfig",
    "LLMRepairError",
    "ListOwner",
    "ParseError",
    "Place",
    "PlaceAboutItem",
    "PlaceAboutSection",
    "PlaceDetails",
    "PlaceDisplayFieldRepairer",
    "PlaceExtractionDiagnostics",
    "PlaceLLMRepairRequest",
    "PlaceLLMRepairer",
    "PlaceLLMTask",
    "PlaceOpeningHours",
    "PlaceReview",
    "PlaceScrapeResult",
    "ReviewTopic",
    "SavedList",
    "SavedListExtractionDiagnostics",
    "SavedListScrapeResult",
    "ScrapeError",
    "cached_place_repairer",
    "build_maps_search_url",
    "collect_place_snapshot",
    "default_place_selector_recipe",
    "extract_list_id",
    "extract_list_id_from_text",
    "has_placelist_marker",
    "localize_maps_url",
    "load_place_selector_recipe",
    "llm_cache_namespace_from_env",
    "needs_display_en",
    "parse_saved_list_artifacts",
    "openai_compatible_place_repairer_from_env",
    "repair_place_display_fields",
    "reusable_place_display_fields",
    "reuse_place_display_fields",
    "scrape_place",
    "scrape_places",
    "scrape_saved_list",
    "scrape_saved_lists",
    "write_default_place_selector_recipe",
]

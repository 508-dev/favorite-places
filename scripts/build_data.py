from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import importlib.util
import io
import json
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from statistics import median
from threading import Lock
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pycountry
from pydantic import TypeAdapter
from PIL import Image, ImageOps, UnidentifiedImageError, features
from sqlalchemy.engine import make_url

try:
    from scripts.pipeline_models import (
        AddressParts,
        EnrichmentCacheEntry,
        EnrichmentPlace,
        Guide,
        GuideManifest,
        ListAuthor,
        MarkerIcon,
        NormalizedPlace,
        PlaceField,
        PlaceProvenance,
        PlaceReservationLink,
        PlacesSettings,
        RawPlace,
        RawSavedList,
        SourceConfig,
        TrustSignal,
    )
    from scripts.trust_signals import (
        TrustSignalStore,
        load_trust_signals_for_places,
        refresh_trust_signals_for_raw_guides,
        resolve_trust_store_url,
    )
except ModuleNotFoundError:
    from pipeline_models import (
        AddressParts,
        EnrichmentCacheEntry,
        EnrichmentPlace,
        Guide,
        GuideManifest,
        ListAuthor,
        MarkerIcon,
        NormalizedPlace,
        PlaceField,
        PlaceProvenance,
        PlaceReservationLink,
        PlacesSettings,
        RawPlace,
        RawSavedList,
        SourceConfig,
        TrustSignal,
    )
    from trust_signals import (
        TrustSignalStore,
        load_trust_signals_for_places,
        refresh_trust_signals_for_raw_guides,
        resolve_trust_store_url,
    )

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR_ENV = "FAVORITE_PLACES_SITE_DIR"
SCRAPER_STATE_DIR_ENV = "FAVORITE_PLACES_GMAPS_SCRAPER_STATE_DIR"


def resolve_site_dir() -> Path:
    configured_site_dir = os.environ.get(SITE_DIR_ENV)
    if configured_site_dir:
        path = Path(configured_site_dir).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        return path.resolve()

    default_site_dir = ROOT / "site"
    if default_site_dir.exists():
        return default_site_dir

    return ROOT / "site.example"


def resolve_scraper_state_dir() -> Path:
    configured_state_dir = os.environ.get(SCRAPER_STATE_DIR_ENV)
    if configured_state_dir:
        path = Path(configured_state_dir).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        return path.resolve()

    return ROOT / ".context" / "gmaps-scraper"


SITE_DIR = resolve_site_dir()
CONFIG_PATH = SITE_DIR / "list_sources.json"
RAW_DIR = SITE_DIR / "data" / "raw"
PLACES_CACHE_DIR = SITE_DIR / "data" / "cache" / "google-places"
PLACES_SQLITE_PATH = SITE_DIR / "data" / "cache" / "places.sqlite"
GENERATED_DIR = ROOT / "src" / "data" / "generated"
GENERATED_LISTS_DIR = GENERATED_DIR / "lists"
PUBLIC_DATA_DIR = ROOT / "public" / "data"
PLACE_PHOTOS_DIR = SITE_DIR / "public" / "place-photos"
AUTHOR_PHOTOS_DIR = SITE_DIR / "public" / "author-photos"
SITE_BUILD_HOOKS_PATH = SITE_DIR / "build_hooks.py"
SITE_ENRICHMENT_CONFIG_PATH = SITE_DIR / "enrichment.json"
LIST_OVERRIDES_DIR = SITE_DIR / "overrides" / "lists"
PLACE_OVERRIDES_DIR = SITE_DIR / "overrides" / "places"
SCRAPER_STATE_DIR = resolve_scraper_state_dir()
PRICE_RATE_CACHE_DIR = ROOT / ".context" / "currency-rates"
AUTO_REFRESH_WORKER_CAP = 4
DEFAULT_REFRESH_RETRIES = 2
DEFAULT_REFRESH_RETRY_BACKOFF_SECONDS = 10.0
DEFAULT_REFRESH_STARTUP_JITTER_SECONDS = 8.0


class _NoopLangfuseObservation:
    def update(self, **_: Any) -> None:
        return None


SCRAPER_SESSION_SLOT_COUNT = 8
SCRAPER_SESSION_LOCK_WRITE_GRACE = timedelta(seconds=5)
SCRAPER_SESSION_MAX_AGE = timedelta(days=14)
ERROR_CACHE_TTL = timedelta(days=1)
UNMATCHED_CACHE_TTL = timedelta(days=3)
LOW_CONFIDENCE_CACHE_TTL = timedelta(days=3)
RATINGS_CACHE_TTL = timedelta(days=7)
NON_OPERATIONAL_CACHE_TTL = timedelta(days=3)
OPERATIONAL_CACHE_TTL = timedelta(days=14)
PHOTOLESS_REAL_PLACE_CACHE_TTL = timedelta(days=1)
RAW_SOURCE_CACHE_TTL = timedelta(days=14)
RAW_SOURCE_REFRESH_JITTER = timedelta(days=3)
SOURCE_HTTP_TIMEOUT_SECONDS = 30
SOURCE_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
WANDERLOG_MOBX_STATE_MARKER = "window.__MOBX_STATE__ = "
WANDERLOG_CONFIG_MARKER = "window.__CONFIG__ = "
STRONG_MATCH_SCORE = 45
PLACE_PAGE_COORDINATE_MISMATCH_REJECT_METERS = 25_000.0
PLACE_PAGE_ADDRESSLESS_SEARCH_COORDINATE_MATCH_METERS = 400.0
MAP_PIN_DISTANCE_WARNING_MIN_METERS = 100_000.0
MAP_PIN_DISTANCE_WARNING_BUFFER_METERS = 50_000.0
MAP_PIN_DISTANCE_SUPPRESSION_MIN_METERS = 1_000_000.0
MAP_PIN_DISTANCE_SUPPRESSION_MULTIPLIER = 5.0
BEST_HIT_MAX_COUNT = 6
BEST_HIT_MIN_RATING = 4.5
BEST_HIT_MAX_RATING = 4.8
BEST_HIT_RATING_PERCENTILE = 0.8
BEST_HIT_MIN_CANDIDATE_COUNT = 3
BEST_HIT_REVIEW_THRESHOLD_MULTIPLIER = 0.35
BEST_HIT_MIN_REVIEW_THRESHOLD = 10
BEST_HIT_MAX_REVIEW_THRESHOLD = 2_000
BEST_HIT_RELAXED_REVIEW_THRESHOLD_MULTIPLIER = 0.65
PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PHOTO_DOWNLOAD_TIMEOUT_SECONDS = 20
PHOTO_CARD_WIDTH = 800
PHOTO_CARD_HEIGHT = 600
PHOTO_CARD_QUALITY = 78
AUTHOR_PHOTO_SIZE = 160
AUTHOR_PHOTO_QUALITY = 82
MIN_PLACE_PHOTO_SOURCE_DIMENSION = 200
PLACE_PHOTO_SOURCE_DIMENSION_RE = re.compile(r"w(?P<width>\d+)-h(?P<height>\d+)")
PRICE_DISPLAY_SOURCE_FIELDS = ("price_range", "admission_price", "room_price")
DEFAULT_PRICE_DISPLAY_SOURCE_ORDER = ("price_range", "admission_price", "room_price")
NUMERIC_PRICE_RANGE_CATEGORY_HINTS = (
    "bar",
    "bakery",
    "bistro",
    "brunch",
    "cafe",
    "coffee",
    "deli",
    "dessert",
    "drink",
    "food",
    "izakaya",
    "market",
    "pub",
    "ramen",
    "restaurant",
    "shop",
    "soba",
    "steak",
    "store",
    "sushi",
    "tea",
    "tonkatsu",
    "udon",
    "unagi",
    "yakiniku",
    "yakitori",
)
PRICE_BUDGET_KINDS = (
    "restaurant_per_person",
    "hotel_per_night",
    "admission_per_person",
)
RESTAURANT_BUDGET_CATEGORY_HINTS = (
    "bar",
    "bakery",
    "bistro",
    "brunch",
    "cafe",
    "coffee",
    "deli",
    "dessert",
    "diner",
    "drink",
    "food",
    "izakaya",
    "pub",
    "ramen",
    "restaurant",
    "soba",
    "steak",
    "sushi",
    "tea",
    "tonkatsu",
    "udon",
    "unagi",
    "yakiniku",
    "yakitori",
)
HOTEL_BUDGET_CATEGORY_HINTS = (
    "hotel",
    "hostel",
    "inn",
    "lodging",
    "resort",
    "ryokan",
)
ADMISSION_BUDGET_CATEGORY_HINTS = (
    "aquarium",
    "attraction",
    "botanical",
    "castle",
    "forest",
    "gallery",
    "garden",
    "historic",
    "landmark",
    "museum",
    "nature",
    "observation",
    "palace",
    "park",
    "recreation",
    "reserve",
    "shrine",
    "temple",
    "ticket",
    "tourist",
    "zoo",
)
PRICE_BUDGET_TIER_THRESHOLDS = {
    "restaurant_per_person": {
        "USD": (20, 60, 130),
        "JPY": (2_999, 8_999, 9_999),
        "TWD": (600, 1_800, 3_800),
        "HKD": (160, 480, 1_040),
        "SGD": (27, 80, 170),
        "KRW": (28_000, 84_000, 182_000),
        "EUR": (20, 60, 130),
        "GBP": (18, 55, 120),
        "CNY": (145, 435, 940),
        "THB": (720, 2_200, 4_700),
        "PHP": (1_150, 3_500, 7_500),
        "VND": (510_000, 1_520_000, 3_300_000),
        "INR": (1_700, 5_100, 11_000),
        "AUD": (32, 95, 205),
        "CAD": (28, 85, 180),
    },
    "hotel_per_night": {
        "USD": (120, 250, 500),
        "JPY": (12_000, 30_000, 60_000),
        "TWD": (3_600, 7_500, 15_000),
        "HKD": (960, 2_000, 4_000),
        "SGD": (160, 330, 660),
        "KRW": (168_000, 350_000, 700_000),
        "EUR": (120, 250, 500),
        "GBP": (100, 220, 450),
        "CNY": (870, 1_800, 3_600),
        "THB": (4_300, 9_000, 18_000),
        "PHP": (6_800, 14_000, 28_000),
        "VND": (3_000_000, 6_300_000, 12_700_000),
        "INR": (10_000, 21_000, 42_000),
        "AUD": (180, 380, 750),
        "CAD": (165, 345, 690),
    },
    "admission_per_person": {
        "USD": (15, 40, 80),
        "JPY": (999, 2_999, 5_999),
        "TWD": (450, 1_200, 2_400),
        "HKD": (120, 320, 640),
        "SGD": (20, 55, 110),
        "KRW": (20_000, 55_000, 110_000),
        "EUR": (15, 40, 80),
        "GBP": (12, 35, 70),
        "CNY": (110, 290, 580),
        "THB": (540, 1_450, 2_900),
        "PHP": (850, 2_300, 4_600),
        "VND": (380_000, 1_000_000, 2_000_000),
        "INR": (1_250, 3_350, 6_700),
        "AUD": (24, 65, 130),
        "CAD": (21, 55, 110),
    },
}
PRICE_RATE_CACHE_TTL = timedelta(days=1)
FX_RATES_API_URL = "https://api.fxratesapi.com/latest"
FAWAZ_CURRENCY_API_URL = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
PRICE_CURRENCY_SYMBOLS = {
    "USD": "$",
    "TWD": "NT$",
    "JPY": "¥",
    "SGD": "S$",
    "HKD": "HK$",
    "EUR": "€",
    "GBP": "£",
    "KRW": "₩",
    "CNY": "¥",
    "THB": "฿",
    "PHP": "₱",
    "VND": "₫",
    "INR": "₹",
    "AUD": "A$",
    "CAD": "CA$",
}
PRICE_TOKEN_CURRENCIES = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "NT$": "TWD",
    "TWD": "TWD",
    "¥": "JPY",
    "JPY": "JPY",
    "円": "JPY",
    "SGD": "SGD",
    "S$": "SGD",
    "HK$": "HKD",
    "HKD": "HKD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "₩": "KRW",
    "KRW": "KRW",
    "CNY": "CNY",
    "RMB": "CNY",
    "฿": "THB",
    "THB": "THB",
    "₱": "PHP",
    "PHP": "PHP",
    "₫": "VND",
    "VND": "VND",
    "₹": "INR",
    "INR": "INR",
    "A$": "AUD",
    "AUD": "AUD",
    "CA$": "CAD",
    "CAD": "CAD",
}
COUNTRY_CURRENCY_CODES = {
    "australia": "AUD",
    "canada": "CAD",
    "china": "CNY",
    "france": "EUR",
    "germany": "EUR",
    "hong kong": "HKD",
    "india": "INR",
    "italy": "EUR",
    "japan": "JPY",
    "philippines": "PHP",
    "singapore": "SGD",
    "south korea": "KRW",
    "spain": "EUR",
    "taiwan": "TWD",
    "thailand": "THB",
    "united kingdom": "GBP",
    "uk": "GBP",
    "united states": "USD",
    "united states of america": "USD",
    "usa": "USD",
    "vietnam": "VND",
}
PRICE_AMOUNT_RE = re.compile(
    r"(?P<currency>NT\$|HK\$|CA\$|A\$|S\$|US\$|USD|TWD|JPY|SGD|HKD|EUR|GBP|KRW|CNY|RMB|"
    r"THB|PHP|VND|INR|AUD|CAD|[$€£¥₩₹₫฿₱円])\s*"
    r"(?P<amount>[0-9][0-9,.\s\u00a0]*)(?P<plus>\+)?",
    flags=re.IGNORECASE,
)
# Bump this for any SQLite schema or row-derivation semantic change that must force a rebuild.
PLACES_SQLITE_SIGNATURE_VERSION = 4
PLACES_SQLITE_BUILD_METADATA_KEY = "build_signature"
STABLE_GENERATED_AT_FALLBACK = "1970-01-01T00:00:00+00:00"
SQLITE_UNREADABLE_ERROR_MARKERS = (
    "database disk image is malformed",
    "file is not a database",
    "malformed",
    "not a database",
)
PLACES_SQLITE_SCHEMA_SQL = """
CREATE TABLE build_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE guide_enrichment_cache (
    guide_slug TEXT NOT NULL,
    place_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    last_verified_at TEXT,
    refresh_after TEXT,
    source TEXT,
    query TEXT NOT NULL,
    input_signature TEXT,
    matched INTEGER,
    score INTEGER,
    error TEXT,
    error_body TEXT,
    cache_json TEXT NOT NULL,
    PRIMARY KEY (guide_slug, place_id)
);

CREATE INDEX idx_guide_enrichment_cache_place_id
    ON guide_enrichment_cache(place_id);
"""
SCRAPER_BLOCK_ERROR_MARKERS = (
    "429",
    "403",
    "automated queries",
    "blocked",
    "captcha",
    "challenge",
    "denied",
    "forbidden",
    "rate limit",
    "rate-limit",
    "recaptcha",
    "robot",
    "sorry",
    "too many requests",
    "unusual traffic",
)


@dataclass(frozen=True)
class PendingPhotoJob:
    guide_slug: str
    place_id: str
    place_name: str
    photo_url: str


@dataclass(frozen=True)
class PlacePhotoFlatIndex:
    exact_by_stem: dict[str, str]
    latest_by_place_prefix: dict[str, str]


@dataclass(frozen=True)
class PlacesSqliteRows:
    guide_cache_rows: list[tuple[Any, ...]]


SCRAPER_SESSION_RESET_ERROR_MARKERS = SCRAPER_BLOCK_ERROR_MARKERS + (
    "failed to load http cookie jar",
    "failed to launch browser context",
    "launch_persistent_context",
)
COUNTRY_LOCALITY_ALIASES = (
    "England",
    "Scotland",
    "Wales",
    "Northern Ireland",
    "UK",
    "UAE",
    "USA",
    "Korea",
    "Taiwan",
    "台灣",
    "台湾",
    "Trinidad & Tobago",
    "Spain",
    "España",
    "Espanya",
    "スペイン",
    "France",
    "フランス",
    "Monaco",
    "モナコ",
    "Japan",
    "日本",
    "China",
    "中国",
    "South Korea",
    "Korea",
    "韓国",
    "Vatican City",
    "Ivory Coast",
)
AUSTRALIAN_SUBNATIONAL_LOCALITY_ALIASES = (
    "NSW",
    "VIC",
    "QLD",
    "SA",
    "WA",
    "TAS",
    "NT",
    "ACT",
)
SUBNATIONAL_LOCALITY_ALIASES = AUSTRALIAN_SUBNATIONAL_LOCALITY_ALIASES + (
    "Xinjiang",
    "Xinjiang Uygur Autonomous Region",
    "Xinjiang Uyghur Autonomous Region",
    "新疆",
    "新疆维吾尔自治区",
    "新疆維吾爾自治區",
    "Sichuan",
    "四川",
    "四川省",
    "Kitaazumi District",
    "Kitasaku District",
)
SEMANTIC_NEIGHBORHOOD_DISPLAY_ALIASES = {
    "da an": "Da'an",
    "daan": "Da'an",
    "datong": "Datong",
    "dazhi": "Dazhi",
    "longshan temple": "Longshan Temple",
    "marunouchi": "Marunouchi",
    "ximen": "Ximen",
    "ximending": "Ximending",
    "xinyi": "Xinyi",
    "zhongshan": "Zhongshan",
    "zhishan": "Zhishan",
}
SEMANTIC_NEIGHBORHOOD_UPPERCASE_TOKENS = {
    "cbd",
    "dc",
    "dtla",
    "la",
    "nyc",
}
COUNTRY_LOCALITY_KEYS: set[str] | None = None
SUBNATIONAL_LOCALITY_KEYS: set[str] | None = None
SUBNATIONAL_LOCALITY_CODE_KEYS: set[str] | None = None
SUBNATIONAL_LOCALITY_ABBREVIATIONS = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    }
)
LOCATION_TAG_ALIASES: dict[str, tuple[str, ...]] = {
    "geneve": ("geneva",),
    "geneva": ("geneve",),
}
LOCALITY_EQUIVALENCE_ALIASES: dict[str, str] = {
    "brussel": "brussels",
    "bruxelles": "brussels",
    "donostia": "san sebastian",
    "donostia san sebastián": "san sebastian",
    "donostia san sebastian": "san sebastian",
    "genève": "geneva",
    "geneve": "geneva",
    "københavn": "copenhagen",
    "münchen": "munich",
    "nürnberg": "nuremberg",
    "san sebastián": "san sebastian",
}
BROKEN_TAG_NORMALIZATION_MAP: dict[str, str] = {
    "gen-ve": "geneve",
}


def default_refresh_workers() -> int:
    cpu_count = os.cpu_count() or AUTO_REFRESH_WORKER_CAP
    return max(1, min(AUTO_REFRESH_WORKER_CAP, SCRAPER_SESSION_SLOT_COUNT, cpu_count))


DEFAULT_REFRESH_WORKERS = default_refresh_workers()


ENRICHMENT_CONTACT_FIELDS_VERSION = 1


PLACES_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.name",
        "places.displayName",
        "places.formattedAddress",
        "places.googleMapsUri",
        "places.rating",
        "places.userRatingCount",
        "places.location",
        "places.primaryType",
        "places.primaryTypeDisplayName",
        "places.types",
        "places.businessStatus",
        "places.websiteUri",
    ]
)
VIBE_TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cozy": (
        "cozy",
        "cosy",
        "warm",
        "intimate",
        "comfort",
        "homey",
        "home-y",
        "fireside",
    ),
    "quiet": (
        "quiet",
        "calm",
        "peaceful",
        "serene",
        "relaxing",
        "chill",
        "low-key",
        "low key",
        "library",
    ),
    "lively": (
        "lively",
        "buzzy",
        "busy",
        "fun",
        "energetic",
        "scene",
        "nightlife",
        "live music",
    ),
    "date-night": (
        "date night",
        "romantic",
        "intimate",
        "cocktail",
        "wine bar",
        "special occasion",
    ),
    "solo-friendly": (
        "solo",
        "counter",
        "bar seating",
        "quick bite",
        "book",
        "people watching",
    ),
    "group-friendly": (
        "group",
        "friends",
        "share",
        "shared",
        "family style",
        "large table",
    ),
    "laptop-friendly": (
        "laptop",
        "wifi",
        "wi-fi",
        "work",
        "cowork",
        "outlet",
        "outlets",
    ),
    "scenic": (
        "view",
        "views",
        "scenic",
        "waterfront",
        "beach",
        "mountain",
        "sunset",
        "rooftop",
        "garden",
        "park",
    ),
    "design-forward": (
        "design",
        "beautiful",
        "stylish",
        "interior",
        "architecture",
        "gallery",
        "aesthetic",
    ),
    "local-favorite": (
        "local favorite",
        "favorite",
        "institution",
        "neighborhood",
        "beloved",
        "regulars",
    ),
    "classic": (
        "classic",
        "old-school",
        "old school",
        "historic",
        "traditional",
        "since ",
        "heritage",
    ),
    "hidden-gem": (
        "hidden gem",
        "hidden",
        "underrated",
        "hole in the wall",
        "off the beaten",
        "tucked",
    ),
    "cheap-eats": (
        "cheap",
        "inexpensive",
        "affordable",
        "budget",
        "value",
        "cash only",
    ),
    "splurge": (
        "splurge",
        "expensive",
        "fine dining",
        "michelin",
        "omakase",
        "tasting menu",
        "luxury",
    ),
    "quick-stop": (
        "quick",
        "grab",
        "takeout",
        "take away",
        "snack",
        "bakery",
        "stand",
        "stall",
    ),
    "slow-afternoon": (
        "afternoon",
        "linger",
        "slow",
        "tea",
        "cafe",
        "coffee",
        "bookstore",
    ),
    "late-night": (
        "late night",
        "late-night",
        "24 hour",
        "24-hour",
        "bar",
        "pub",
        "izakaya",
        "night market",
    ),
    "outdoor-seating": (
        "outdoor",
        "patio",
        "terrace",
        "sidewalk",
        "courtyard",
        "garden",
    ),
    "rainy-day": (
        "rainy",
        "rain",
        "museum",
        "gallery",
        "bookstore",
        "indoor",
        "indoors",
    ),
    "family-friendly": (
        "family",
        "kids",
        "children",
        "playground",
        "stroller",
    ),
    "touristy-but-worth-it": (
        "touristy",
        "worth it",
        "iconic",
        "famous",
        "landmark",
        "must",
    ),
}


def terminate_executor(executor: Any) -> None:
    terminate_workers = getattr(executor, "terminate_workers", None)
    if callable(terminate_workers):
        terminate_workers()
        return

    kill_workers = getattr(executor, "kill_workers", None)
    if callable(kill_workers):
        kill_workers()
        return

    executor.shutdown(wait=False, cancel_futures=True)
VIBE_CATEGORY_RULES: dict[str, tuple[str, ...]] = {
    "cafe": ("cozy", "solo-friendly", "slow-afternoon"),
    "coffee": ("cozy", "solo-friendly", "slow-afternoon"),
    "coffee-shop": ("cozy", "solo-friendly", "slow-afternoon"),
    "tea-house": ("quiet", "slow-afternoon"),
    "bakery": ("quick-stop", "slow-afternoon"),
    "bar": ("date-night", "late-night", "lively"),
    "pub": ("group-friendly", "late-night", "lively"),
    "night-club": ("late-night", "lively"),
    "restaurant": ("date-night", "group-friendly"),
    "fine-dining-restaurant": ("date-night", "splurge"),
    "museum": ("rainy-day", "solo-friendly"),
    "art-gallery": ("design-forward", "rainy-day"),
    "book-store": ("quiet", "rainy-day", "solo-friendly"),
    "park": ("scenic", "family-friendly"),
    "tourist-attraction": ("touristy-but-worth-it", "scenic"),
}
MARKER_ICON_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "cafe",
        (
            "cafe",
            "coffee",
            "coffee-shop",
            "coffee-roasters",
            "espresso-bar",
            "tea",
            "tea-house",
            "tea-room",
            "bubble-tea",
        ),
    ),
    (
        "bakery",
        (
            "bakery",
            "patisserie",
            "pastry",
            "pastry-shop",
            "dessert",
            "dessert-shop",
            "confectionery",
            "chocolate-shop",
            "ice-cream",
            "donut-shop",
        ),
    ),
    (
        "bar",
        (
            "bar",
            "pub",
            "cocktail-bar",
            "wine-bar",
            "beer-hall",
            "brewery",
            "sports-bar",
            "night-club",
            "izakaya",
            "karaoke",
            "live-music-venue",
        ),
    ),
    (
        "restaurant",
        (
            "restaurant",
            "food",
            "food-court",
            "meal-takeaway",
            "meal-delivery",
            "pizza",
            "ramen",
            "sushi",
            "noodle",
            "steakhouse",
            "barbecue",
            "brunch",
            "diner",
            "bistro",
            "cafeteria",
            "fast-food",
            "seafood",
            "tapas",
            "grill",
        ),
    ),
    (
        "museum",
        (
            "museum",
            "gallery",
            "library",
            "archive",
            "planetarium",
            "cultural-center",
        ),
    ),
    (
        "attraction",
        (
            "tourist-attraction",
            "landmark",
            "monument",
            "historical-landmark",
            "observation-deck",
            "visitor-center",
            "church",
            "cathedral",
            "basilica",
            "temple",
            "shrine",
            "mosque",
            "synagogue",
            "castle",
            "palace",
            "aquarium",
            "zoo",
            "amusement-park",
            "ferris-wheel",
        ),
    ),
    (
        "park",
        (
            "park",
            "botanical-garden",
            "garden",
            "hiking-area",
            "campground",
            "picnic-ground",
            "nature-preserve",
            "national-park",
            "trailhead",
        ),
    ),
    (
        "beach",
        (
            "beach",
            "marina",
            "waterfront",
            "pier",
            "swimming",
            "surf",
        ),
    ),
    (
        "shopping",
        (
            "store",
            "market",
            "shopping-mall",
            "gift-shop",
            "book-store",
            "clothing-store",
            "department-store",
            "antique-store",
        ),
    ),
    (
        "hotel",
        (
            "lodging",
            "hotel",
            "hostel",
            "resort",
            "inn",
            "ryokan",
        ),
    ),
    (
        "spa",
        (
            "spa",
            "sauna",
            "onsen",
            "massage",
            "hot-spring",
        ),
    ),
)
GENERIC_ENRICHMENT_TYPE_TAGS = frozenset(
    {
        "establishment",
        "point-of-interest",
        "store",
        "premise",
        "subpremise",
        "street-address",
        "route",
        "political",
        "item",
        "local-guide",
    }
)
INVALID_ENRICHMENT_TYPE_TAG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d+-reviews?$"),
    re.compile(r"^(?:open|closed|clear|cloudy|sunny|rain|snow)$"),
    re.compile(
        r"^(?:clear|cloudy|sunny|rain|snow|showers?|storms?|thunderstorms?|fog|haze|mist|overcast)"
        r"(?:[_-](?:with|and|periodic|scattered|mostly|partly|light|heavy|clouds?|sun|rain|snow|"
        r"showers?|storms?|thunderstorms?|fog|haze|mist|overcast))*$"
    ),
    re.compile(r"^adults(?:_|-)only(?:_|-).*$"),
    re.compile(r"^beer$"),
    re.compile(r"^transportation$"),
    re.compile(r"^(?:light|heavy|mostly|partly)(?:_|-)(?:rain|snow|cloudy|sunny)$"),
    re.compile(r"^free(?:_|-)breakfast$"),
    re.compile(r"^free(?:_|-)?wi(?:_|-)?fi$"),
    re.compile(r"^breakfast(?:_|-)included$"),
    re.compile(r"^free(?:_|-)cancellation(?:_|-).*$"),
    re.compile(r"^floor(?:_|-)\d+$"),
)
INVALID_ENRICHMENT_PRIMARY_CATEGORY_DISPLAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d+\s+reviews?$", re.IGNORECASE),
    re.compile(r"^floor\s+\d+$", re.IGNORECASE),
    re.compile(r"^(?:open|closed|clear|cloudy|sunny|rain|snow)$", re.IGNORECASE),
    re.compile(
        r"^(?:clear|cloudy|sunny|rain|snow|showers?|storms?|thunderstorms?|fog|haze|mist|overcast)"
        r"(?:\s+(?:with|and|periodic|scattered|mostly|partly|light|heavy|clouds?|sun|rain|snow|"
        r"showers?|storms?|thunderstorms?|fog|haze|mist|overcast))*$",
        re.IGNORECASE,
    ),
    re.compile(r"^adults\s+only\b", re.IGNORECASE),
    re.compile(r"^beer$", re.IGNORECASE),
    re.compile(r"^transportation$", re.IGNORECASE),
    re.compile(r"^(?:light|heavy|mostly|partly)\s+(?:rain|snow|cloudy|sunny)$", re.IGNORECASE),
    re.compile(r"^free\s+breakfast$", re.IGNORECASE),
    re.compile(r"^free\s+wi-?fi$", re.IGNORECASE),
    re.compile(r"^breakfast\s+included$", re.IGNORECASE),
    re.compile(r"^free cancellation\b", re.IGNORECASE),
)
PARENT_TYPE_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("restaurant", ("restaurant", "bistro", "diner", "grill", "eatery")),
    ("bar", ("bar", "pub", "brewery", "izakaya", "tavern", "speakeasy")),
    ("cafe", ("cafe", "coffee", "coffee-shop", "espresso-bar", "tea-house")),
    ("bakery", ("bakery", "patisserie", "pastry", "dessert", "donut", "ice-cream")),
    ("museum", ("museum", "art-gallery", "library", "archive", "planetarium")),
    ("attraction", ("tourist-attraction", "historical-landmark", "monument", "temple", "shrine", "church", "mosque", "synagogue")),
    ("park", ("park", "botanical-garden", "garden", "trailhead", "campground", "nature-preserve")),
    ("shopping", ("market", "boutique", "mall", "shopping-center")),
)
INFERRED_PARENT_TYPE_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "japanese-restaurant",
        (
            "japanese-restaurant",
            "sushi-restaurant",
            "ramen-restaurant",
            "udon-noodle-restaurant",
            "soba-noodle-restaurant",
            "izakaya-restaurant",
            "yakitori-restaurant",
            "yakiniku-restaurant",
            "kaiseki-restaurant",
            "tempura-restaurant",
            "teppanyaki-restaurant",
            "tonkatsu-restaurant",
            "okonomiyaki-restaurant",
            "japanese-curry-restaurant",
            "shabu-shabu-restaurant",
            "sukiyaki-restaurant",
            "ramen-restaurant",
            "kaiseki-restaurant",
            "curry-restaurant",
        ),
    ),
    (
        "western-restaurant",
        (
            "western-restaurant",
            "steak-house",
            "chophouse-restaurant",
            "continental-restaurant",
        ),
    ),
    (
        "chinese-restaurant",
        (
            "chinese-restaurant",
            "cantonese-restaurant",
            "sichuan-restaurant",
            "hot-pot-restaurant",
            "dumpling-restaurant",
            "chinese-noodle-restaurant",
            "dim-sum-restaurant",
            "hakka-restaurant",
        ),
    ),
    (
        "italian-restaurant",
        (
            "italian-restaurant",
            "pizza-restaurant",
            "pasta-shop",
            "trattoria",
            "osteria",
        ),
    ),
    ("mexican-restaurant", ("mexican-restaurant", "taco-restaurant", "taqueria")),
    ("korean-restaurant", ("korean-restaurant", "korean-barbecue-restaurant")),
)
DISPLAY_NAME_TYPE_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cocktail-bar", ("cocktail bar", "雞尾酒酒吧", "鸡尾酒酒吧", "칵테일바", "カクテル バー")),
    ("beer-hall", ("beer hall", "啤酒館", "啤酒馆")),
    ("steak-house", ("steak house", "steakhouse", "ステーキハウス", "牛排館", "牛排馆", "스테이크하우스")),
    ("izakaya-restaurant", ("izakaya", "居酒屋")),
    ("ramen-restaurant", ("ramen", "ラーメン屋", "拉麵店")),
    ("chinese-noodle-restaurant", ("麵店", "冷麵店", "ร้านก๋วยเตี๋ยว")),
    ("sushi-restaurant", ("sushi", "壽司店", "寿司店", "스시집")),
    ("yakitori-restaurant", ("yakitori", "焼き鳥店", "串燒店", "串烧店")),
    ("soba-noodle-restaurant", ("soba", "蕎麦店", "荞麦面店")),
    ("udon-noodle-restaurant", ("udon", "うどん店", "乌冬面店", "烏龍麵店")),
    (
        "hot-pot-restaurant",
        ("hot pot", "火鍋餐廳", "火锅店", "鍋料理店", "涮涮鍋餐廳", "しゃぶしゃぶ店", "すき焼き/しゃぶしゃぶ店"),
    ),
    ("yakiniku-restaurant", ("yakiniku", "焼肉店", "燒肉餐廳", "烧肉店", "日式燒肉餐廳", "日式烧肉餐厅")),
    ("kaiseki-restaurant", ("kaiseki", "会席・懐石料理店")),
    ("teppanyaki-restaurant", ("teppanyaki", "鉄板焼き店")),
    ("curry-restaurant", ("curry restaurant", "カレー店")),
    ("barbecue-restaurant", ("barbecue restaurant", "燒烤", "숯불구이/바베큐전문점")),
    ("pizza-restaurant", ("pizza", "ピザ店")),
    ("hamburger-restaurant", ("hamburger", "burger", "ハンバーガー店", "漢堡店", "汉堡店")),
    ("dumpling-restaurant", ("dumpling", "餃子店", "饺子馆")),
    ("indian-restaurant", ("indian restaurant", "インド料理店")),
    ("american-restaurant", ("american restaurant", "アメリカ料理店")),
    ("italian-restaurant", ("italian restaurant", "イタリア料理店", "義大利餐廳", "意大利餐廳", "意大利餐厅")),
    ("french-restaurant", ("french restaurant", "フランス料理店", "法國餐廳", "法国餐厅")),
    ("vietnamese-restaurant", ("vietnamese restaurant", "ベトナム料理店")),
    ("thai-restaurant", ("thai restaurant", "タイ料理店", "ภัตตาคารอาหารไทย")),
    ("mediterranean-restaurant", ("mediterranean restaurant", "地中海料理店")),
    ("japanese-restaurant", ("japanese restaurant", "日本餐廳", "日本餐厅", "日本料理店", "和食店", "和食レストラン", "うなぎ料理店", "郷土料理店", "もんじゃ焼き屋", "定食屋")),
    ("taiwanese-restaurant", ("taiwanese restaurant", "台灣餐廳", "台湾餐厅", "台菜餐廳", "台菜餐厅", "台灣菜", "早餐店")),
    ("cantonese-restaurant", ("cantonese restaurant", "粵菜館", "粤菜馆")),
    ("sichuan-restaurant", ("sichuan restaurant", "四川酒家", "四川菜館", "四川菜馆")),
    ("chinese-restaurant", ("chinese restaurant", "中菜館", "中菜馆", "中餐館", "中餐馆", "中式麵食店", "中式面食店", "中華料理店", "酒樓", "浙菜/浙江菜館", "中式包點店")),
    ("seafood-restaurant", ("seafood restaurant", "海鮮餐廳", "海鲜餐厅", "シーフード・海鮮料理店", "海鲜馆", "ภัตตาคารอาหารทะเล")),
    ("fine-dining-restaurant", ("fine dining restaurant", "ภัตตาคารอาหารแบบหรูหรา")),
    ("mexican-restaurant", ("mexican restaurant", "墨西哥餐廳", "墨西哥餐厅", "メキシコ料理店")),
    ("vegetarian-restaurant", ("vegetarian restaurant", "素食餐廳", "素食餐厅")),
    ("asian-restaurant", ("asian restaurant", "亞洲菜餐廳", "亚洲菜餐厅")),
    ("hakka-restaurant", ("hakka restaurant", "客家菜館", "客家菜馆")),
    ("museum", ("museum", "博物館", "博物馆", "박물관")),
    ("art-gallery", ("art gallery", "art museum", "美術館", "美术馆", "畫廊", "画廊", "미술관", "アート ギャラリー")),
    ("aquarium", ("aquarium", "水族館")),
    ("cafe", ("coffee shop", "cafe", "咖啡店", "咖啡館", "咖啡馆", "カフェ・喫茶", "カフェ", "喫茶", "카페", "커피숍/커피 전문점")),
    ("tea-house", ("tea house", "茶藝館", "傳統茶館", "전통 찻집")),
    ("tea-store", ("tea store", "茶葉店")),
    ("bakery", ("bakery", "ベーカリー", "餅店", "糖果糕餅店", "和菓子屋", "甜品店")),
    ("ice-cream-shop", ("ice cream", "アイスクリーム店", "雪糕店")),
    ("bubble-tea-store", ("bubble tea", "珍珠奶茶店")),
    ("wine-bar", ("wine bar", "ワインバー", "紅酒吧", "红酒吧", "와인 바")),
    ("pub", ("pub", "パブ")),
    ("bar", ("bar", "酒吧", "バー", "술집", "บาร์")),
    ("restaurant", ("restaurant", "餐廳", "餐厅", "餐館", "餐馆", "レストラン", "음식점", "식당", "小餐館", "小餐馆", "ร้านอาหาร")),
    ("shopping-center", ("shopping center", "百貨公司")),
    ("market", ("market", "マーケット", "夜市")),
    ("park", ("park", "公園", "城市公園", "庭園")),
    ("garden", ("garden", "庭園")),
    ("trailhead", ("trail", "行山徑")),
    ("historical-landmark", ("historical landmark", "歷史建築", "史跡")),
    ("cathedral", ("cathedral", "カトリック大聖堂")),
    ("shrine", ("shrine", "神社")),
    ("tourist-attraction", ("tourist attraction", "旅遊景點", "旅游胜地", "観光名所", "관광 명소", "景勝地")),
    ("cultural-center", ("cultural center", "藝術中心", "艺术中心", "文化中心")),
)
MARKER_ICON_TEXT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "cafe",
        (
            "cafe",
            "coffee",
            "espresso",
            "kissaten",
            "tea",
            "matcha",
            "roastery",
        ),
    ),
    (
        "bakery",
        (
            "bakery",
            "baker",
            "bread",
            "pastry",
            "patisserie",
            "viennoiserie",
            "dessert",
            "gelato",
            "ice cream",
        ),
    ),
    (
        "bar",
        (
            "bar",
            "cocktail",
            "wine",
            "pub",
            "taproom",
            "speakeasy",
            "izakaya",
            "nightclub",
            "karaoke",
            "beer hall",
        ),
    ),
    (
        "restaurant",
        (
            "restaurant",
            "pizza",
            "ramen",
            "sushi",
            "udon",
            "soba",
            "yakitori",
            "yakiniku",
            "grill",
            "kitchen",
            "diner",
            "bistro",
            "trattoria",
            "osteria",
            "taqueria",
            "bbq",
            "burger",
            "curry",
            "noodle",
        ),
    ),
    (
        "museum",
        (
            "museum",
            "gallery",
            "archive",
            "exhibit",
            "library",
        ),
    ),
    (
        "attraction",
        (
            "temple",
            "shrine",
            "church",
            "cathedral",
            "mosque",
            "synagogue",
            "castle",
            "palace",
            "tower",
            "landmark",
            "monument",
            "observatory",
            "observation deck",
            "viewpoint",
            "park entrance",
        ),
    ),
    (
        "park",
        (
            "park",
            "garden",
            "forest",
            "hike",
            "trail",
        ),
    ),
    (
        "beach",
        (
            "beach",
            "coast",
            "bay",
            "surf",
            "island",
        ),
    ),
    (
        "shopping",
        (
            "shopping",
            "market",
            "store",
            "district",
            "shotengai",
            "mall",
            "bookstore",
        ),
    ),
    (
        "hotel",
        (
            "hotel",
            "hostel",
            "ryokan",
            "resort",
            "inn",
        ),
    ),
    (
        "spa",
        (
            "spa",
            "onsen",
            "sauna",
            "bathhouse",
            "massage",
        ),
    ),
)

try:
    from gmaps_scraper import (
        BrowserSessionConfig,
        HttpSessionConfig,
        LLMRepairError,
        ParseError,
        ScrapeError,
        build_maps_search_url as build_scraper_maps_search_url,
        cached_place_repairer,
        llm_cache_namespace_from_env,
        needs_display_en,
        openai_compatible_place_repairer_from_env,
        repair_place_display_fields,
        reusable_place_display_fields,
        scrape_place,
        scrape_saved_list,
    )
except ImportError:
    class ParseError(RuntimeError):
        pass

    class ScrapeError(RuntimeError):
        pass

    BrowserSessionConfig = None
    HttpSessionConfig = None
    LLMRepairError = RuntimeError
    build_scraper_maps_search_url = None
    cached_place_repairer = None
    llm_cache_namespace_from_env = None
    needs_display_en = None
    openai_compatible_place_repairer_from_env = None
    repair_place_display_fields = None
    reusable_place_display_fields = None
    scrape_place = None
    scrape_saved_list = None

try:
    from gmaps_scraper import localize_maps_url as localize_scraper_maps_url
except ImportError:
    localize_scraper_maps_url = None

RECOVERABLE_REFRESH_ERRORS = (ScrapeError, ParseError)


@dataclass(slots=True, frozen=True)
class ScraperSessionState:
    identity_key: str
    identity_dir: Path
    slot_key: str
    browser_profile_dir: Path
    http_cookie_jar_path: Path
    metadata_path: Path
    lock_path: Path


def current_scraper_proxy() -> str | None:
    value = os.environ.get("GMAPS_SCRAPER_PROXY")
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def scraper_session_identity_key(proxy: str | None, *, session_scope: str | None = None) -> str:
    if proxy is None:
        base_key = "direct"
    else:
        digest = hashlib.sha256(proxy.encode("utf-8")).hexdigest()[:16]
        base_key = f"proxy-{digest}"
    scope_key = scraper_session_scope_key(session_scope)
    return f"{base_key}-{scope_key}" if scope_key else base_key


def scraper_session_scope_key(session_scope: str | None) -> str | None:
    normalized = as_string(session_scope)
    if normalized is None:
        return None
    scope_key = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    return scope_key[:80] or None


def scraper_place_session_scope(
    *,
    city_name: str | None,
    country_name: str | None,
) -> str:
    locality_parts = [
        part
        for part in (country_name, city_name)
        if as_string(part) is not None
    ]
    locality = "-".join(str(part) for part in locality_parts)
    return f"place-{locality}" if locality else "place"


def build_scraper_session_state(
    proxy: str | None,
    *,
    session_scope: str | None = None,
    slot_key: str = "slot-0",
    lock_path: Path | None = None,
) -> ScraperSessionState:
    identity_key = scraper_session_identity_key(proxy, session_scope=session_scope)
    identity_dir = SCRAPER_STATE_DIR / identity_key
    return ScraperSessionState(
        identity_key=identity_key,
        identity_dir=identity_dir,
        slot_key=slot_key,
        browser_profile_dir=identity_dir / "browser" / slot_key,
        http_cookie_jar_path=identity_dir / f"http-cookies.{slot_key}.txt",
        metadata_path=identity_dir / f"metadata.{slot_key}.json",
        lock_path=lock_path or identity_dir / "locks" / f"{slot_key}.lock",
    )


def ensure_scraper_session_state(
    proxy: str | None,
    *,
    session_scope: str | None = None,
    now: datetime | None = None,
) -> ScraperSessionState:
    reference_time = now or datetime.now(UTC)
    identity_key = scraper_session_identity_key(proxy, session_scope=session_scope)
    identity_dir = SCRAPER_STATE_DIR / identity_key
    sweep_stale_scraper_session_states(
        proxy,
        session_scope=session_scope,
        identity_dir=identity_dir,
        now=reference_time,
    )
    slot_key, lock_path = acquire_scraper_session_slot(identity_dir)
    state = build_scraper_session_state(
        proxy,
        session_scope=session_scope,
        slot_key=slot_key,
        lock_path=lock_path,
    )
    if scraper_session_is_stale(state, now=reference_time):
        clear_scraper_session_state(state)
    state.identity_dir.mkdir(parents=True, exist_ok=True)
    return state


def load_scraper_session_metadata(state: ScraperSessionState) -> dict[str, str] | None:
    if not state.metadata_path.exists():
        return None
    try:
        payload = json.loads(state.metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    metadata = {key: value for key, value in payload.items() if isinstance(key, str) and isinstance(value, str)}
    return metadata or None


def scraper_session_is_stale(
    state: ScraperSessionState,
    *,
    now: datetime | None = None,
) -> bool:
    metadata = load_scraper_session_metadata(state)
    if metadata is None:
        return (
            state.metadata_path.exists()
            or state.browser_profile_dir.exists()
            or state.http_cookie_jar_path.exists()
        )
    timestamp = metadata.get("last_used_at") or metadata.get("created_at")
    if timestamp is None:
        return True
    try:
        last_used_at = parse_metadata_datetime(timestamp)
    except ValueError:
        return True
    reference_time = now or datetime.now(UTC)
    return reference_time - last_used_at >= SCRAPER_SESSION_MAX_AGE


def record_scraper_session_use(
    state: ScraperSessionState,
    *,
    proxy: str | None,
    now: datetime | None = None,
) -> None:
    state.identity_dir.mkdir(parents=True, exist_ok=True)
    existing = load_scraper_session_metadata(state) or {}
    timestamp = (now or datetime.now(UTC)).isoformat()
    payload = {
        "identity_key": state.identity_key,
        "created_at": existing.get("created_at", timestamp),
        "last_used_at": timestamp,
        "proxy_kind": "proxy" if proxy is not None else "direct",
    }
    state.metadata_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def clear_scraper_session_state(state: ScraperSessionState) -> None:
    if state.browser_profile_dir.exists() and state.browser_profile_dir.is_dir():
        shutil.rmtree(state.browser_profile_dir, ignore_errors=True)

    for path in (state.http_cookie_jar_path, state.metadata_path):
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except OSError:
            pass

    for directory in (state.browser_profile_dir.parent, state.identity_dir):
        try:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def release_scraper_session_lock(state: ScraperSessionState) -> None:
    try:
        if state.lock_path.exists() or state.lock_path.is_symlink():
            state.lock_path.unlink()
    except OSError:
        pass

    for directory in (state.lock_path.parent, state.identity_dir):
        try:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def should_reset_scraper_session(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in SCRAPER_SESSION_RESET_ERROR_MARKERS)


def sweep_stale_scraper_session_states(
    proxy: str | None,
    *,
    session_scope: str | None = None,
    identity_dir: Path,
    now: datetime,
) -> None:
    for metadata_path in sorted(identity_dir.glob("metadata.*.json")):
        slot_key = metadata_path.stem.removeprefix("metadata.")
        state = build_scraper_session_state(proxy, session_scope=session_scope, slot_key=slot_key)
        if scraper_session_lock_is_active(state.lock_path):
            continue
        if scraper_session_is_stale(state, now=now):
            clear_scraper_session_state(state)
            release_scraper_session_lock(state)


def acquire_scraper_session_slot(identity_dir: Path) -> tuple[str, Path]:
    locks_dir = identity_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    for index in range(SCRAPER_SESSION_SLOT_COUNT):
        slot_key = f"slot-{index}"
        lock_path = locks_dir / f"{slot_key}.lock"
        if try_acquire_scraper_session_lock(lock_path):
            return slot_key, lock_path

    slot_key = f"slot-overflow-{os.getpid()}"
    lock_path = locks_dir / f"{slot_key}.lock"
    if try_acquire_scraper_session_lock(lock_path):
        return slot_key, lock_path
    raise RuntimeError(f"Could not allocate a scraper session slot under {identity_dir}.")


def try_acquire_scraper_session_lock(lock_path: Path) -> bool:
    payload = f"{os.getpid()}\n"
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if not scraper_session_lock_is_active(lock_path):
                try:
                    lock_path.unlink()
                except OSError:
                    return False
                continue
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except Exception:
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise
        return True
    return False


def scraper_session_lock_is_active(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    try:
        owner_text = lock_path.read_text(encoding="utf-8").strip()
        owner_pid = int(owner_text)
    except OSError:
        return True
    except ValueError:
        try:
            modified_at = datetime.fromtimestamp(lock_path.stat().st_mtime, tz=UTC)
        except OSError:
            return True
        return datetime.now(UTC) - modified_at < SCRAPER_SESSION_LOCK_WRITE_GRACE
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def build_scraper_sessions(
    proxy: str | None,
    *,
    session_scope: str | None = None,
    now: datetime | None = None,
) -> tuple[ScraperSessionState, Any, Any]:
    session_state = ensure_scraper_session_state(proxy, session_scope=session_scope, now=now)
    return session_state, *build_scraper_configs(session_state, proxy)


def build_scraper_configs(
    session_state: ScraperSessionState,
    proxy: str | None,
) -> tuple[Any, Any]:
    browser_session = None
    http_session = None
    if BrowserSessionConfig is not None:
        browser_session = BrowserSessionConfig(
            profile_dir=session_state.browser_profile_dir,
            proxy=proxy,
        )
    if HttpSessionConfig is not None:
        http_session = HttpSessionConfig(
            cookie_jar_path=session_state.http_cookie_jar_path,
            proxy=proxy,
        )
    return browser_session, http_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh every configured source before rebuilding generated JSON.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run the scraper in headed mode.",
    )
    parser.add_argument(
        "--refresh-force",
        action="store_true",
        help="Force-refresh raw sources even if a local file import is unchanged.",
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
        default=DEFAULT_REFRESH_WORKERS,
        help=(
            "Maximum parallel workers for headless refreshes, enrichment jobs, and optional photo refresh jobs. "
            "Defaults to an auto-derived value capped at 4; headed raw list refreshes stay single-worker."
        ),
    )
    parser.add_argument(
        "--refresh-retries",
        type=non_negative_int,
        default=DEFAULT_REFRESH_RETRIES,
        help="Retry each Google list refresh this many times after the first failed attempt.",
    )
    parser.add_argument(
        "--refresh-retry-backoff-seconds",
        type=non_negative_float,
        default=DEFAULT_REFRESH_RETRY_BACKOFF_SECONDS,
        help="Initial delay before retrying a failed Google list refresh. Later retries use exponential backoff.",
    )
    parser.add_argument(
        "--refresh-startup-jitter-seconds",
        type=non_negative_float,
        default=DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
        help="Maximum randomized delay before each Google list scrape, enrichment job, or optional photo refresh job starts.",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Fill missing or stale place enrichment cache entries from Google Maps place pages, prioritizing totally missing places first.",
    )
    parser.add_argument(
        "--enrich-missing",
        action="store_true",
        help="Fill only missing place enrichment cache entries, skipping expiry-based refreshes.",
    )
    parser.add_argument(
        "--refresh-enrichment",
        action="store_true",
        help="Force-refresh place enrichment cache entries for every place.",
    )
    parser.add_argument(
        "--refresh-semantic-descriptions",
        action="store_true",
        help=(
            "Generate or refresh LLM semantic descriptions from existing enrichment cache entries only; "
            "does not scrape Google Maps or call the Places API."
        ),
    )
    parser.add_argument(
        "--refresh-semantic-enrichment",
        action="store_true",
        help=(
            "Generate or refresh LLM semantic neighborhoods, tags, vibe tags, and types from existing enrichment "
            "cache entries only; does not scrape Google Maps or call the Places API."
        ),
    )
    parser.add_argument(
        "--force-semantic-descriptions",
        action="store_true",
        help=(
            "Force-regenerate cached LLM semantic descriptions even when their semantic signatures still match. "
            "Implies --refresh-semantic-descriptions."
        ),
    )
    parser.add_argument(
        "--force-semantic-enrichment",
        action="store_true",
        help=(
            "Force-regenerate cached LLM semantic neighborhoods and tags even when a semantic LLM cache entry exists. "
            "Implies --refresh-semantic-enrichment."
        ),
    )
    parser.add_argument(
        "--enrich-place",
        action="append",
        default=[],
        help=(
            "Force-refresh enrichment, or refresh semantic descriptions, for a specific place selector unless combined "
            "with --enrich or --enrich-missing. Repeat to target multiple places. "
            "Matches exact place ids, CIDs, names, Maps URLs, `cid:<value>`/`gms:<value>` "
            "aliases, and `guide-slug:<selector>` forms."
        ),
    )
    parser.add_argument(
        "--skip-enrichment-source-refresh",
        action="store_true",
        help=(
            "Do not refresh affected raw Google Maps source lists before place-page enrichment. "
            "Use this when you explicitly want enrichment to run against the existing raw snapshot."
        ),
    )
    parser.add_argument(
        "--refresh-photos",
        action="store_true",
        help="Download missing local optimized place photos from cached enrichment photo URLs.",
    )
    parser.add_argument(
        "--refresh-trust-signals",
        action="store_true",
        help=(
            "Refresh cached trust signals from configured/search providers. Normal builds only read the local "
            "trust cache and never perform live trust-signal searches."
        ),
    )
    parser.add_argument(
        "--trust-google-fallback",
        action="store_true",
        help="Allow the trust-signal resolver to fall back to parsing Google Search HTML when Brave Search is unavailable or empty.",
    )
    parser.add_argument(
        "--export-cache-json",
        action="store_true",
        help="Export the current SQLite-backed enrichment cache into per-guide JSON files for debugging.",
    )
    return parser


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def load_sources() -> list[SourceConfig]:
    if not CONFIG_PATH.exists():
        return []
    sources = TypeAdapter(list[SourceConfig]).validate_json(CONFIG_PATH.read_text(encoding="utf-8"))
    validate_unique_source_slugs(sources)
    return sources


def validate_unique_source_slugs(sources: list[SourceConfig]) -> None:
    slug_counts = Counter(source.slug for source in sources)
    duplicate_slugs = sorted(slug for slug, count in slug_counts.items() if count > 1)
    if not duplicate_slugs:
        return

    duplicate_text = ", ".join(duplicate_slugs)
    raise RuntimeError(f"Duplicate source slug(s) in {CONFIG_PATH}: {duplicate_text}")


def refresh_raw_sources(
    *,
    headed: bool,
    force_refresh: bool,
    refresh_lists: list[str],
    refresh_workers: int,
    refresh_retries: int = DEFAULT_REFRESH_RETRIES,
    refresh_retry_backoff_seconds: float = DEFAULT_REFRESH_RETRY_BACKOFF_SECONDS,
    refresh_startup_jitter_seconds: float = DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()
    selected_sources = resolve_refresh_sources(sources, refresh_lists)
    selected_slugs = {source.slug for source in selected_sources}
    refresh_jobs: list[tuple[SourceConfig, Path, bool, RawSavedList | None]] = []

    for source in sources:
        if selected_sources and source.slug not in selected_slugs:
            continue

        raw_path = RAW_DIR / f"{source.slug}.json"
        existing_payload = load_raw_saved_list(raw_path)

        if source.type == "google_export_csv":
            payload = refresh_google_export_csv(
                source,
                existing_payload=existing_payload,
                force_refresh=force_refresh or bool(selected_sources),
            )
            if payload is not None:
                write_json(raw_path, payload)
            continue

        source_url = source.url
        if not source_url:
            raise RuntimeError(f"Configured source {source.slug} is missing a URL.")

        refresh_reason = (
            None
            if force_refresh or bool(selected_sources)
            else raw_source_refresh_reason(source, existing_payload)
        )
        if not force_refresh and not bool(selected_sources) and refresh_reason is None:
            print(f"Skipping {source.slug} (raw snapshot fresh)")
            continue

        if force_refresh:
            print(f"Refreshing {source.slug} from {source_url} (forced)")
        elif selected_sources:
            print(f"Refreshing {source.slug} from {source_url} (selected)")
        else:
            print(f"Refreshing {source.slug} from {source_url} ({refresh_reason})")
        backup_available = existing_payload is not None and raw_source_signature_matches(
            source,
            existing_payload.source_signature,
        )
        refresh_jobs.append((source, raw_path, backup_available, existing_payload))

    if not refresh_jobs:
        return

    effective_startup_jitter_seconds = (
        refresh_startup_jitter_seconds if not headed and len(refresh_jobs) > 1 else 0
    )
    max_workers = max(1, refresh_workers)
    if headed or len(refresh_jobs) == 1 or max_workers == 1:
        failures: list[str] = []
        for source, raw_path, backup_available, existing_payload in refresh_jobs:
            try:
                payload = scrape_url_source_with_retries(
                    source,
                    headed=headed,
                    refresh_retries=refresh_retries,
                    refresh_retry_backoff_seconds=refresh_retry_backoff_seconds,
                    refresh_startup_jitter_seconds=effective_startup_jitter_seconds,
                )
            except RECOVERABLE_REFRESH_ERRORS as exc:
                if backup_available:
                    print(f"Keeping existing raw snapshot for {source.slug} after refresh failure: {exc}")
                    continue
                failures.append(f"{source.slug}: {exc}")
                continue
            payload = preserve_existing_raw_saved_list(
                source=source,
                slug=source.slug,
                existing_payload=existing_payload,
                refreshed_payload=payload,
            )
            write_json(raw_path, payload)
        if failures:
            failure_text = "\n".join(failures)
            raise RuntimeError(f"Raw refresh failed for {len(failures)} source(s):\n{failure_text}")
        return

    max_workers = min(max_workers, len(refresh_jobs))
    print(f"Running {len(refresh_jobs)} headless refresh jobs with {max_workers} workers")
    failures: list[str] = []

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {
            executor.submit(
                scrape_url_source_with_retries,
                source,
                headed=False,
                refresh_retries=refresh_retries,
                refresh_retry_backoff_seconds=refresh_retry_backoff_seconds,
                refresh_startup_jitter_seconds=effective_startup_jitter_seconds,
            ): (source, raw_path, backup_available, existing_payload)
            for source, raw_path, backup_available, existing_payload in refresh_jobs
        }
        for future in as_completed(future_map):
            source, raw_path, backup_available, existing_payload = future_map[future]
            try:
                payload = future.result()
            except RECOVERABLE_REFRESH_ERRORS as exc:
                if backup_available:
                    print(f"Keeping existing raw snapshot for {source.slug} after refresh failure: {exc}")
                else:
                    failures.append(f"{source.slug}: {exc}")
                continue
            payload = preserve_existing_raw_saved_list(
                source=source,
                slug=source.slug,
                existing_payload=existing_payload,
                refreshed_payload=payload,
            )
            write_json(raw_path, payload)
    except KeyboardInterrupt:
        print("Interrupt received; terminating refresh workers.", flush=True)
        terminate_executor(executor)
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    if failures:
        failure_text = "\n".join(failures)
        raise RuntimeError(f"Raw refresh failed for {len(failures)} source(s):\n{failure_text}")


def scrape_url_source_with_retries(
    source: SourceConfig,
    *,
    headed: bool,
    refresh_retries: int,
    refresh_retry_backoff_seconds: float,
    refresh_startup_jitter_seconds: float,
) -> RawSavedList:
    if source.type == "wanderlog_view_url":
        return scrape_wanderlog_view_url_with_retries(
            source,
            refresh_retries=refresh_retries,
            refresh_retry_backoff_seconds=refresh_retry_backoff_seconds,
            refresh_startup_jitter_seconds=refresh_startup_jitter_seconds,
        )
    return scrape_google_list_url_with_retries(
        source,
        headed=headed,
        refresh_retries=refresh_retries,
        refresh_retry_backoff_seconds=refresh_retry_backoff_seconds,
        refresh_startup_jitter_seconds=refresh_startup_jitter_seconds,
    )


def scrape_google_list_url(source: SourceConfig, *, headed: bool) -> RawSavedList:
    if scrape_saved_list is None:
        raise RuntimeError(
            "Could not import gmaps_scraper. "
            "Run `uv sync` to install the scraper dependency before using --refresh."
        )

    source_url = source.url
    if not source_url:
        raise RuntimeError(f"Configured source {source.slug} is missing a URL.")

    proxy = current_scraper_proxy()
    session_state, browser_session, http_session = build_scraper_sessions(proxy)
    try:
        try:
            result = scrape_saved_list(
                source_url,
                headless=not headed,
                collection_mode="curl",
                browser_session=browser_session,
                http_session=http_session,
            )
        except RECOVERABLE_REFRESH_ERRORS as exc:
            if should_reset_scraper_session(exc):
                clear_scraper_session_state(session_state)
                browser_session, http_session = build_scraper_configs(session_state, proxy)
            result = scrape_saved_list(
                source_url,
                headless=not headed,
                collection_mode="browser",
                browser_session=browser_session,
                http_session=http_session,
            )

        record_scraper_session_use(session_state, proxy=proxy)
        payload = RawSavedList.model_validate(result.to_dict())
        if source.title and not payload.title:
            payload.title = source.title
        stamp_raw_saved_list(payload, source, source_signature=raw_source_signature(source))
        if not list_author_is_overridden(source.slug):
            payload.owner = sync_list_author_photo(source.slug, payload.owner)
        return payload
    finally:
        release_scraper_session_lock(session_state)


def scrape_wanderlog_view_url(source: SourceConfig) -> RawSavedList:
    source_url = source.url
    if not source_url:
        raise RuntimeError(f"Configured source {source.slug} is missing a URL.")

    request = Request(
        source_url,
        headers={
            "User-Agent": SOURCE_HTTP_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urlopen(request, timeout=SOURCE_HTTP_TIMEOUT_SECONDS) as response:
            resolved_url = response.geturl() or source_url
            page_html = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError) as exc:
        raise ScrapeError(f"Failed to fetch Wanderlog view {source_url}: {exc}") from exc

    payload = parse_wanderlog_view_html(resolved_url, page_html)
    if source.title and not payload.title:
        payload.title = source.title
    stamp_raw_saved_list(payload, source, source_signature=raw_source_signature(source))
    return payload


def parse_wanderlog_view_html(source_url: str, page_html: str) -> RawSavedList:
    state = extract_wanderlog_mobx_state(page_html)
    trip_store = state.get("tripPlanStore")
    if not isinstance(trip_store, dict):
        raise ParseError("Wanderlog payload missing tripPlanStore")
    data = trip_store.get("data")
    if not isinstance(data, dict):
        raise ParseError("Wanderlog payload missing trip data")
    trip_plan = data.get("tripPlan")
    if not isinstance(trip_plan, dict):
        raise ParseError("Wanderlog payload missing tripPlan")
    itinerary = trip_plan.get("itinerary")
    if not isinstance(itinerary, dict):
        raise ParseError("Wanderlog payload missing itinerary")
    sections = itinerary.get("sections")
    if not isinstance(sections, list):
        raise ParseError("Wanderlog payload missing itinerary sections")

    places: list[RawPlace] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        blocks = section.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "place":
                continue
            place_payload = block.get("place")
            if not isinstance(place_payload, dict):
                continue

            name = as_string(place_payload.get("name")) or "Saved place"
            place_id = as_string(place_payload.get("place_id"))
            geometry = place_payload.get("geometry")
            location = geometry.get("location") if isinstance(geometry, dict) else None
            lat = as_float(location.get("lat")) if isinstance(location, dict) else None
            lng = as_float(location.get("lng")) if isinstance(location, dict) else None
            address = as_string(place_payload.get("formatted_address"))
            maps_url = build_wanderlog_place_maps_url(
                name=name,
                address=address,
                lat=lat,
                lng=lng,
                place_id=place_id,
                place_payload=place_payload,
            )
            if maps_url is None:
                continue

            places.append(
                RawPlace(
                    name=name,
                    address=address,
                    note=extract_wanderlog_block_note(block),
                    is_favorite=wanderlog_block_is_favorite(block),
                    lat=lat,
                    lng=lng,
                    maps_url=maps_url,
                    cid=extract_maps_cid(maps_url),
                    google_id=place_id,
                    maps_place_token=extract_maps_place_token(maps_url),
                    rating=as_float(place_payload.get("rating")),
                    user_rating_count=as_int(place_payload.get("user_ratings_total")),
                    price_level=as_int(place_payload.get("price_level")),
                    types=coerce_string_list(place_payload.get("types")),
                    business_status=extract_wanderlog_business_status(place_payload),
                    photo_url=build_wanderlog_place_photo_url(block),
                )
            )

    title = (
        as_string(trip_plan.get("name"))
        or as_string(trip_plan.get("title"))
        or extract_html_title(page_html)
        or fallback_source_title(SourceConfig(slug=extract_wanderlog_view_key(source_url) or "wanderlog", url=source_url))
    )
    description = (
        as_string(trip_plan.get("description"))
        or as_string(data.get("seoDescription"))
        or extract_wanderlog_meta_description(page_html)
    )
    return RawSavedList(
        source_url=source_url,
        list_id=extract_wanderlog_view_key(source_url),
        title=title,
        description=description,
        places=places,
    )


def extract_wanderlog_mobx_state(page_html: str) -> dict[str, Any]:
    start = page_html.find(WANDERLOG_MOBX_STATE_MARKER)
    if start < 0:
        raise ParseError("Wanderlog page missing MOBX state marker")
    start += len(WANDERLOG_MOBX_STATE_MARKER)
    end = page_html.find(WANDERLOG_CONFIG_MARKER, start)
    if end < 0:
        raise ParseError("Wanderlog page missing config marker")

    serialized = page_html[start:end].strip()
    if serialized.endswith(";"):
        serialized = serialized[:-1].rstrip()

    try:
        state = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise ParseError("Could not decode Wanderlog MOBX state") from exc
    if not isinstance(state, dict):
        raise ParseError("Unexpected Wanderlog MOBX state payload")
    return state


def build_wanderlog_place_maps_url(
    *,
    name: str,
    address: str | None,
    lat: float | None,
    lng: float | None,
    place_id: str | None,
    place_payload: dict[str, Any],
) -> str | None:
    direct_url = as_string(place_payload.get("url"))
    if direct_url:
        return direct_url

    query = build_maps_link_query(name=name, address=address, lat=lat, lng=lng)
    if not query:
        return None
    return build_google_maps_search_url(query, google_place_id=place_id)


def extract_wanderlog_block_note(block: dict[str, Any]) -> str | None:
    fragments: list[str] = []
    text_payload = block.get("text")
    if isinstance(text_payload, dict):
        ops = text_payload.get("ops")
        if isinstance(ops, list):
            for op in ops:
                if not isinstance(op, dict):
                    continue
                insert = op.get("insert")
                if isinstance(insert, str):
                    fragments.append(insert)

    note = "".join(fragments).strip()
    reactions = extract_wanderlog_block_reactions(block)
    place_payload = block.get("place")
    place_name = as_string(place_payload.get("name")) if isinstance(place_payload, dict) else None
    place_types = coerce_string_list(place_payload.get("types")) if isinstance(place_payload, dict) else []
    if reactions:
        note = f"{note}\n{reactions}" if note else reactions
    return sanitize_wanderlog_imported_note(
        note or None,
        place_name=place_name,
        place_types=place_types,
    )


def wanderlog_block_is_favorite(block: dict[str, Any]) -> bool:
    return as_string(block.get("rating")) == "mustGo"


WANDERLOG_IMPORTED_WEB_NOTE_PREFIX_RE = re.compile(r"^\s*from the web:\s*", re.IGNORECASE)
WANDERLOG_LOCATION_STYLE_RAW_TYPE_TAGS = frozenset({"route", "premise", "political", "neighborhood"})


def sanitize_wanderlog_imported_note(
    value: str | None,
    *,
    place_name: str | None,
    place_types: list[str],
) -> str | None:
    note = normalize_text_blocks(value)
    if note is None:
        return None

    had_from_web_prefix = WANDERLOG_IMPORTED_WEB_NOTE_PREFIX_RE.match(note) is not None
    note = WANDERLOG_IMPORTED_WEB_NOTE_PREFIX_RE.sub("", note).strip()
    if not note:
        return None

    raw_type_tags = {
        normalized
        for raw_type in place_types
        if (normalized := slugify(raw_type.replace("_", "-")))
    }
    if (
        had_from_web_prefix
        and raw_type_tags & WANDERLOG_LOCATION_STYLE_RAW_TYPE_TAGS
        and not text_mentions_place_name(note, place_name)
    ):
        return None

    return note


def text_mentions_place_name(value: str, place_name: str | None) -> bool:
    normalized_text = normalize_text(value)
    normalized_name = normalize_text(place_name)
    if not normalized_text or not normalized_name:
        return False
    return normalized_name in normalized_text


def extract_wanderlog_block_reactions(block: dict[str, Any]) -> str | None:
    reactions_payload = block.get("reactions")
    if not isinstance(reactions_payload, dict):
        return None

    reactions: list[str] = []
    for key in reactions_payload:
        reaction = as_string(key)
        if reaction:
            reactions.append(reaction)
    if not reactions:
        return None
    return " ".join(reactions)


def extract_wanderlog_business_status(place_payload: dict[str, Any]) -> str | None:
    if place_payload.get("permanently_closed") is True:
        return "CLOSED_PERMANENTLY"
    status = as_string(place_payload.get("business_status")) or as_string(place_payload.get("status"))
    if status in {"OPERATIONAL", "CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY", "FUTURE_OPENING"}:
        return status
    return normalize_place_page_business_status(status)


def build_wanderlog_place_photo_url(block: dict[str, Any]) -> str | None:
    image_keys = block.get("imageKeys")
    if not isinstance(image_keys, list):
        return None
    for image_key in image_keys:
        normalized = as_string(image_key)
        if normalized:
            return f"https://itin-dev.wanderlogstatic.com/freeImageMedium/{normalized}"
    return None


def extract_wanderlog_view_key(source_url: str) -> str | None:
    path_parts = [part for part in urlsplit(source_url).path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0] != "view":
        return None
    return path_parts[1]


def extract_html_title(page_html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = html_lib.unescape(match.group(1)).strip()
    if title.endswith("– Wanderlog"):
        title = title[: -len("– Wanderlog")].rstrip()
    return title or None


def extract_wanderlog_meta_description(page_html: str) -> str | None:
    patterns = (
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:description["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE)
        if match:
            description = html_lib.unescape(match.group(1)).strip()
            if description:
                return description
    return None


def scrape_google_list_url_with_retries(
    source: SourceConfig,
    *,
    headed: bool,
    refresh_retries: int,
    refresh_retry_backoff_seconds: float,
    refresh_startup_jitter_seconds: float,
) -> RawSavedList:
    attempts = max(1, refresh_retries + 1)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        sleep_for_refresh_startup_jitter(refresh_startup_jitter_seconds)
        try:
            return scrape_google_list_url(source, headed=headed)
        except RECOVERABLE_REFRESH_ERRORS as exc:
            last_error = exc
            if attempt >= attempts:
                break

            backoff_seconds = max(0.0, refresh_retry_backoff_seconds) * (2 ** (attempt - 1))
            print(
                f"Retrying {source.slug} after refresh failure "
                f"({attempt}/{attempts - 1} retries used): {exc}"
            )
            if backoff_seconds > 0:
                time.sleep(backoff_seconds)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Refresh did not produce a result for {source.slug}.")


def scrape_wanderlog_view_url_with_retries(
    source: SourceConfig,
    *,
    refresh_retries: int,
    refresh_retry_backoff_seconds: float,
    refresh_startup_jitter_seconds: float,
) -> RawSavedList:
    attempts = max(1, refresh_retries + 1)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        sleep_for_refresh_startup_jitter(refresh_startup_jitter_seconds)
        try:
            return scrape_wanderlog_view_url(source)
        except RECOVERABLE_REFRESH_ERRORS as exc:
            last_error = exc
            if attempt >= attempts:
                break

            backoff_seconds = max(0.0, refresh_retry_backoff_seconds) * (2 ** (attempt - 1))
            print(
                f"Retrying {source.slug} after refresh failure "
                f"({attempt}/{attempts - 1} retries used): {exc}"
            )
            if backoff_seconds > 0:
                time.sleep(backoff_seconds)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Refresh did not produce a result for {source.slug}.")


def sleep_for_refresh_startup_jitter(max_seconds: float) -> None:
    if max_seconds <= 0:
        return

    delay = random.uniform(0, max_seconds)
    if delay > 0:
        time.sleep(delay)


def refresh_google_export_csv(
    source: SourceConfig,
    *,
    existing_payload: RawSavedList | None,
    force_refresh: bool,
) -> RawSavedList | None:
    source_signature = raw_source_signature(source)
    if not force_refresh and existing_payload is not None and existing_payload.source_signature == source_signature:
        print(f"Skipping {source.slug} (csv unchanged)")
        return None

    csv_path = configured_source_path(source)
    action = "Re-importing" if force_refresh else "Importing"
    print(f"{action} {source.slug} from {csv_path}")
    payload = import_saved_list_csv(source)
    stamp_raw_saved_list(payload, source, source_signature=source_signature)
    return payload


def metadata_datetime_or_none(value: str | None) -> datetime | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    try:
        return parse_metadata_datetime(normalized)
    except ValueError:
        return None


def stable_generated_at(
    raw: RawSavedList,
    enrichment_cache: dict[str, EnrichmentCacheEntry],
    *,
    photo_paths: list[str | None] | None = None,
    trust_signals: Iterable[TrustSignal] | None = None,
) -> str:
    candidates: list[datetime] = []

    raw_fetched_at = metadata_datetime_or_none(raw.fetched_at)
    if raw_fetched_at is not None:
        candidates.append(raw_fetched_at)

    for entry in enrichment_cache.values():
        verified_at = metadata_datetime_or_none(entry.last_verified_at)
        if verified_at is not None:
            candidates.append(verified_at)
            continue

        fetched_at = metadata_datetime_or_none(entry.fetched_at)
        if fetched_at is not None:
            candidates.append(fetched_at)

    for public_path in photo_paths or []:
        absolute_path = public_asset_absolute_path(public_path)
        if absolute_path is None or not absolute_path.exists():
            continue
        candidates.append(datetime.fromtimestamp(absolute_path.stat().st_mtime, tz=UTC))

    for signal in trust_signals or []:
        fetched_at = metadata_datetime_or_none(signal.fetched_at)
        if fetched_at is not None:
            candidates.append(fetched_at)

    if not candidates:
        return STABLE_GENERATED_AT_FALLBACK
    return max(candidates).isoformat()


def rebuild_generated_data(
    *,
    refresh_photos: bool = False,
    photo_workers: int = DEFAULT_REFRESH_WORKERS,
    startup_jitter_seconds: float = DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
) -> None:
    sync_local_csv_sources()
    if GENERATED_LISTS_DIR.exists():
        shutil.rmtree(GENERATED_LISTS_DIR)
    GENERATED_LISTS_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLACE_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    guides: list[Guide] = []
    raw_lists: dict[str, RawSavedList] = {}
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]] = {}

    for raw_path in sorted(RAW_DIR.glob("*.json")):
        raw = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))
        raw_lists[raw_path.stem] = raw
        enrichment_cache = load_places_cache(raw_path.stem)
        enrichment_caches[raw_path.stem] = enrichment_cache

    hydrate_shared_enrichment_photo_urls(enrichment_caches)
    stable_place_ids = build_stable_place_id_index(raw_lists)
    trust_signal_maps = load_all_trust_signal_maps(
        raw_lists=raw_lists,
        enrichment_caches=enrichment_caches,
        stable_place_ids=stable_place_ids,
    )

    for raw_path in sorted(RAW_DIR.glob("*.json")):
        raw = raw_lists[raw_path.stem]
        guide = normalize_guide(
            raw_path.stem,
            raw,
            enrichment_cache=enrichment_caches[raw_path.stem],
            trust_signals=trust_signal_maps.get(raw_path.stem, {}),
        )
        guides.append(guide)

    populate_place_photos_for_guides(
        guides,
        enrichment_caches=enrichment_caches,
        refresh_photos=refresh_photos,
        photo_workers=photo_workers,
        startup_jitter_seconds=startup_jitter_seconds,
    )
    for guide in guides:
        guide.generated_at = stable_generated_at(
            raw_lists[guide.slug],
            enrichment_caches.get(guide.slug, {}),
            photo_paths=[place.main_photo_path for place in guide.places],
            trust_signals=[
                signal
                for place in guide.places
                for signal in place.trust_signals
            ],
        )
    rebuild_places_sqlite(
        raw_lists=raw_lists,
        guides=guides,
        enrichment_caches=enrichment_caches,
    )

    for guide in guides:
        write_json(GENERATED_LISTS_DIR / f"{guide.slug}.json", guide)

    guides.sort(key=lambda guide: (guide.country_name, guide.city_name, guide.title))
    manifests = [summarize_guide(guide) for guide in guides]
    search_index = build_search_index(guides)
    write_json(GENERATED_DIR / "manifests.json", manifests)
    write_json(GENERATED_DIR / "search-index.json", search_index)
    write_json(PUBLIC_DATA_DIR / "search-index.json", search_index, compact=True)


def build_stable_place_id_index(raw_lists: Mapping[str, RawSavedList]) -> dict[tuple[str, int], str]:
    result: dict[tuple[str, int], str] = {}
    for slug, raw in raw_lists.items():
        for index, place in enumerate(raw.places):
            result[(slug, index)] = stable_place_id(place, source_type=raw.configured_source_type)
    return result


def trust_store_url_has_readable_cache(store_url: str) -> bool:
    parsed_url = make_url(store_url)
    if parsed_url.drivername.split("+", 1)[0] != "sqlite":
        return True
    sqlite_database = parsed_url.database
    if not sqlite_database or sqlite_database == ":memory:":
        return True
    if str(parsed_url.query.get("uri", "")).lower() == "true" and sqlite_database.startswith("file:"):
        sqlite_database = sqlite_database.removeprefix("file:")
    if sqlite_database == ":memory:":
        return True
    sqlite_path = Path(unquote(sqlite_database))
    return sqlite_path.exists()


def load_all_trust_signal_maps(
    *,
    raw_lists: dict[str, RawSavedList],
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]],
    stable_place_ids: dict[tuple[str, int], str],
) -> dict[str, dict[str, list[TrustSignal]]]:
    store_url = resolve_trust_store_url(ROOT)
    if not trust_store_url_has_readable_cache(store_url):
        return {}

    store = TrustSignalStore(store_url, initialize=False)
    trust_signal_maps: dict[str, dict[str, list[TrustSignal]]] = {}
    guide_location_contexts = {
        slug: guide_location_context(slug, raw)
        for slug, raw in raw_lists.items()
    }
    for slug, raw in raw_lists.items():
        trust_signal_maps[slug] = load_trust_signals_for_places(
            store,
            slug,
            raw,
            enrichment_caches.get(slug, {}),
            stable_place_ids=stable_place_ids,
            location_context=guide_location_contexts.get(slug),
        )
    return trust_signal_maps


def refresh_trust_signals(
    *,
    include_google_fallback: bool,
) -> None:
    sync_local_csv_sources()
    raw_lists: dict[str, RawSavedList] = {}
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]] = {}
    for raw_path in sorted(RAW_DIR.glob("*.json")):
        raw = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))
        raw_lists[raw_path.stem] = raw
        enrichment_caches[raw_path.stem] = load_places_cache(raw_path.stem)

    stable_place_ids = build_stable_place_id_index(raw_lists)
    guide_location_contexts = {
        slug: guide_location_context(slug, raw)
        for slug, raw in raw_lists.items()
    }
    summary = refresh_trust_signals_for_raw_guides(
        root=ROOT,
        raw_lists=raw_lists,
        enrichment_caches=enrichment_caches,
        stable_place_ids=stable_place_ids,
        guide_location_contexts=guide_location_contexts,
        include_google_fallback=include_google_fallback,
    )
    print(
        "Refreshed trust signals: "
        f"Michelin regions {summary.michelin_regions_refreshed}, "
        f"Michelin details {summary.michelin_details_refreshed}, "
        f"searched {summary.searched_places}, skipped {summary.skipped_places}, "
        f"wrote {summary.signals_written}, provider failures {summary.provider_failures}.",
        flush=True,
    )


def refresh_generated_guide_photos(
    *,
    photo_workers: int = DEFAULT_REFRESH_WORKERS,
    startup_jitter_seconds: float = DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
) -> bool:
    if not GENERATED_LISTS_DIR.exists():
        return False

    guide_paths = sorted(GENERATED_LISTS_DIR.glob("*.json"))
    if not guide_paths:
        return False

    raw_paths = sorted(RAW_DIR.glob("*.json"))
    generated_slugs = {path.stem for path in guide_paths}
    raw_slugs = {path.stem for path in raw_paths}
    if generated_slugs != raw_slugs:
        print(
            "WARNING: Generated and raw guide sets differ; falling back to a full rebuild.",
            flush=True,
        )
        return False

    PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLACE_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

    guides: list[Guide] = []
    raw_lists: dict[str, RawSavedList] = {}
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]] = {}
    original_photo_paths: dict[str, tuple[str | None, ...]] = {}
    original_generated_at: dict[str, str] = {}

    for guide_path in guide_paths:
        guide = Guide.model_validate_json(guide_path.read_text(encoding="utf-8"))
        raw_path = RAW_DIR / f"{guide.slug}.json"
        if not raw_path.exists():
            print(
                f"WARNING: Missing raw data for {guide.slug}; falling back to a full rebuild.",
                flush=True,
            )
            return False

        guides.append(guide)
        original_photo_paths[guide.slug] = tuple(place.main_photo_path for place in guide.places)
        original_generated_at[guide.slug] = guide.generated_at
        raw_lists[guide.slug] = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))
        enrichment_caches[guide.slug] = load_places_cache(guide.slug)

    hydrate_shared_enrichment_photo_urls(enrichment_caches)
    populate_place_photos_for_guides(
        guides,
        enrichment_caches=enrichment_caches,
        refresh_photos=True,
        photo_workers=photo_workers,
        startup_jitter_seconds=startup_jitter_seconds,
    )

    changed = False
    for guide in guides:
        guide.generated_at = stable_generated_at(
            raw_lists[guide.slug],
            enrichment_caches.get(guide.slug, {}),
            photo_paths=[place.main_photo_path for place in guide.places],
        )
        current_photo_paths = tuple(place.main_photo_path for place in guide.places)
        if (
            current_photo_paths != original_photo_paths.get(guide.slug)
            or guide.generated_at != original_generated_at.get(guide.slug)
        ):
            changed = True

    if not changed:
        print("Place photos already current; skipped generated artifact rewrite.", flush=True)
        return True

    for guide in guides:
        write_json(GENERATED_LISTS_DIR / f"{guide.slug}.json", guide)

    rebuild_places_sqlite(
        raw_lists=raw_lists,
        guides=guides,
        enrichment_caches=enrichment_caches,
    )

    sorted_guides = sorted(
        guides,
        key=lambda guide: (guide.country_name, guide.city_name, guide.title),
    )
    manifests = [summarize_guide(guide) for guide in sorted_guides]
    search_index = build_search_index(sorted_guides)
    write_json(GENERATED_DIR / "manifests.json", manifests)
    write_json(GENERATED_DIR / "search-index.json", search_index)
    write_json(PUBLIC_DATA_DIR / "search-index.json", search_index, compact=True)
    return True


def sync_local_csv_sources() -> None:
    for source in load_sources():
        if source.type != "google_export_csv":
            continue

        raw_path = RAW_DIR / f"{source.slug}.json"
        existing_payload = load_raw_saved_list(raw_path)
        payload = refresh_google_export_csv(
            source,
            existing_payload=existing_payload,
            force_refresh=False,
        )
        if payload is None:
            continue

        write_json(raw_path, payload)


def import_saved_list_csv(source: SourceConfig) -> RawSavedList:
    csv_path = configured_source_path(source)
    rows = parse_google_export_rows(csv_path)
    header_index = find_google_export_header_index(rows)
    if header_index is None:
        raise RuntimeError(f"Could not find Google export headers in {csv_path}")

    headers = [normalize_csv_header(cell) for cell in rows[header_index]]
    description = csv_preamble_description(rows[:header_index])
    places: list[RawPlace] = []

    for row in rows[header_index + 1 :]:
        if not any(cell.strip() for cell in row):
            continue
        row_values = {header: value.strip() for header, value in zip(headers, row, strict=False)}
        maps_url = as_string(row_values.get("url"))
        if maps_url is None:
            continue

        url_name = name_from_maps_url(maps_url)
        raw_title = as_string(row_values.get("title"))
        note = combine_place_note(row_values.get("note"), row_values.get("comment"))
        places.append(
            RawPlace(
                name=url_name or raw_title or "Saved place",
                note=note,
                maps_url=maps_url,
                maps_place_token=extract_maps_place_token(maps_url),
            )
        )

    return RawSavedList(
        title=source.title or fallback_source_title(source),
        description=description,
        places=places,
    )


def parse_google_export_rows(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    for delimiter in ("\t", ",", ";"):
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        if find_google_export_header_index(rows) is not None:
            return rows
    raise RuntimeError(f"Could not detect a supported delimiter for {path}")


def find_google_export_header_index(rows: list[list[str]]) -> int | None:
    for index, row in enumerate(rows):
        normalized_row = [normalize_csv_header(cell) for cell in row]
        if normalized_row[:5] == ["title", "note", "url", "tags", "comment"]:
            return index
        if normalized_row[:3] == ["title", "note", "url"]:
            return index
    return None


def normalize_csv_header(value: str) -> str:
    return value.strip().lower()


def csv_preamble_description(rows: list[list[str]]) -> str | None:
    lines = [" ".join(cell.strip() for cell in row if cell.strip()) for row in rows]
    lines = [line for line in lines if line]
    if not lines:
        return None
    return "\n".join(lines)


def combine_place_note(note: Any, comment: Any) -> str | None:
    note_parts = [as_string(note), as_string(comment)]
    combined = [part for part in note_parts if part]
    if not combined:
        return None
    return "\n\n".join(combined)


def name_from_maps_url(maps_url: str) -> str | None:
    match = re.search(r"/place/([^/?#]+)", maps_url)
    if not match:
        return None
    candidate = unquote(match.group(1).replace("+", " "))
    return as_string(candidate)


def extract_maps_cid(maps_url: str | None) -> str | None:
    if maps_url is None:
        return None
    match = re.search(r"[?&]cid=(\d+)", maps_url)
    if not match:
        return None
    return match.group(1)


def extract_maps_place_token(maps_url: str | None) -> str | None:
    if maps_url is None:
        return None
    match = re.search(r"(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", maps_url)
    if not match:
        return None
    return match.group(1).lower()


def expand_location_tag_aliases(tags: set[str]) -> set[str]:
    expanded = set(tags)
    for tag in list(tags):
        expanded.update(LOCATION_TAG_ALIASES.get(tag, ()))
    return expanded


def place_override_for_ui_copy(slug: str, place_id: str, override: dict[str, Any]) -> dict[str, Any]:
    if "why_recommended" in override:
        raise ValueError(
            f"{PLACE_OVERRIDES_DIR / f'{slug}.json'} {place_id} uses unsupported "
            "'why_recommended'; use 'note' for handwritten recommendation copy"
        )
    return override


def coerce_guide_place_photo_mode(value: Any) -> Literal["local_cache", "remote_url"]:
    normalized = as_string(value)
    if normalized is None:
        return "local_cache"
    if normalized not in {"local_cache", "remote_url"}:
        raise ValueError(
            f"Unsupported place_photo_mode {normalized!r}; expected 'local_cache' or 'remote_url'."
        )
    return normalized


def normalize_guide(
    slug: str,
    raw: RawSavedList,
    *,
    enrichment_cache: dict[str, EnrichmentCacheEntry],
    trust_signals: dict[str, list[TrustSignal]] | None = None,
) -> Guide:
    list_override = read_json(LIST_OVERRIDES_DIR / f"{slug}.json")
    place_override_map = read_json(PLACE_OVERRIDES_DIR / f"{slug}.json")

    title = as_string(list_override.get("title")) or raw.title or slug.replace("-", " ").title()
    description = as_string(list_override.get("description")) or raw.description
    author = guide_author_for_ui(raw, list_override=list_override)
    place_photo_mode = coerce_guide_place_photo_mode(list_override.get("place_photo_mode"))
    description = transform_guide_description_with_site_hook(
        description,
        slug=slug,
        raw=raw,
        list_override=list_override,
    )

    city_name = as_string(list_override.get("city_name")) or infer_city_name(title)
    country_name = as_string(list_override.get("country_name")) or infer_country_name(title, raw)
    country_code = as_string(list_override.get("country_code")) or infer_country_code(country_name)

    description_tags = extract_hashtags(description)
    override_tags = [
        normalized
        for tag in coerce_string_list(list_override.get("list_tags"))
        if (normalized := normalize_list_tag(tag)) != "item"
    ]
    list_tags = sorted(expand_location_tag_aliases({*description_tags, *override_tags}))

    normalized_places: list[NormalizedPlace] = []
    category_counter: Counter[str] = Counter()
    prefer_enrichment_names = raw.configured_source_type == "google_export_csv"

    for place in raw.places:
        place_id = stable_place_id(place, source_type=raw.configured_source_type)
        place_trust_signals = (
            [display_trust_signal(signal) for signal in trust_signals.get(place_id, [])]
            if trust_signals is not None
            else []
        )
        override = place_override_for_ui_copy(slug, place_id, place_override_map.get(place_id, {}))
        enrichment_cache_entry = enrichment_cache.get(place_id)
        enrichment = coerce_enrichment_place(enrichment_cache_entry)
        usable_enrichment_category = usable_enrichment_primary_category(
            enrichment,
            raw_place=place,
        )
        manual_primary_category = as_string(override.get("primary_category"))
        machine_primary_category = (
            usable_enrichment_category
            or raw_place_primary_category(place)
        )
        mapped_primary_category = apply_site_category_mappings(
            machine_primary_category,
            city_name=city_name,
            country_name=country_name,
        )
        primary_category = manual_primary_category or mapped_primary_category
        machine_primary_category_tag = (
            normalize_tag_slug(machine_primary_category) if machine_primary_category else None
        )
        mapped_primary_category_tag = (
            normalize_tag_slug(mapped_primary_category) if mapped_primary_category else None
        )
        category_mapping_replaces_tag = bool(
            not manual_primary_category
            and machine_primary_category_tag
            and mapped_primary_category_tag
            and mapped_primary_category_tag != machine_primary_category_tag
        )
        mapped_primary_category_source_tag = (
            machine_primary_category_tag
            if category_mapping_replaces_tag
            else None
        )
        primary_category_localized = (
            None
            if manual_primary_category or category_mapping_replaces_tag
            else usable_enrichment_primary_category_localized(
                enrichment,
                raw_place=place,
                canonical_primary_category=usable_enrichment_category,
            )
        )
        use_semantic_enrichment = google_maps_place_semantic_llm_enabled()
        use_semantic_descriptions = google_maps_place_semantic_descriptions_enabled()
        semantic_tags = [
            tag
            for tag in [
                *(enrichment.semantic_tags if use_semantic_enrichment else []),
                *(enrichment.semantic_types if use_semantic_enrichment else []),
            ]
            if semantic_tag_slug_is_usable(tag) and tag != mapped_primary_category_source_tag
        ]
        place_override_tags = coerce_string_list(override.get("tags"))
        tags = sorted(
            {
                *place_override_tags,
                *semantic_tags,
                *derive_place_tags(place, city_name, enrichment=enrichment, category=primary_category),
            }
        )
        if mapped_primary_category_source_tag:
            tags = sorted(
                {
                    *place_override_tags,
                    *(tag for tag in tags if tag != mapped_primary_category_source_tag),
                }
            )
        raw_locality_candidates = infer_address_localities(
            place.address,
            city_name=city_name,
        )
        enrichment_locality_candidates = merge_inferred_localities(
            infer_address_localities(
                enrichment.formatted_address,
                city_name=city_name,
            ),
            infer_address_parts_localities(
                enrichment.address_parts,
                city_name=city_name,
            ),
        )
        locality_candidates = merge_inferred_localities(
            raw_locality_candidates,
            enrichment_locality_candidates,
        )
        semantic_neighborhood = (
            normalize_semantic_neighborhood_label(
                enrichment.semantic_neighborhood,
                city_name=city_name,
                country_name=country_name,
            )
            if use_semantic_enrichment and enrichment.semantic_neighborhood
            else None
        )
        semantic_neighborhood = refine_semantic_neighborhood_with_address_localities(
            semantic_neighborhood,
            locality_candidates,
        )
        manual_neighborhood = as_string(override.get("neighborhood"))
        machine_neighborhood = semantic_neighborhood or (locality_candidates[0] if locality_candidates else None)
        mapped_neighborhood = apply_site_neighborhood_mappings(
            machine_neighborhood,
            city_name=city_name,
            country_name=country_name,
            address_values=[
                place.address,
                enrichment.formatted_address,
                *flatten_address_parts(enrichment.address_parts),
            ],
            locality_candidates=locality_candidates,
        )
        neighborhood = manual_neighborhood or mapped_neighborhood
        neighborhood_uses_enrichment = bool(
            neighborhood
            and (
                machine_neighborhood in enrichment_locality_candidates
                or machine_neighborhood == semantic_neighborhood
            )
            and neighborhood not in raw_locality_candidates
        )
        hidden = bool(override.get("hidden", False))
        top_pick_override = as_bool(override.get("top_pick"))
        top_pick = top_pick_override if top_pick_override is not None else place.is_favorite
        manual_note = as_string(override.get("note"))
        note = manual_note or place.note
        added_by = place_added_by_for_ui(place, override=override)
        enrichment_identity_compatible = enrichment_identity_is_compatible_with_raw(place, enrichment)
        enrichment_recommendation_copy = (
            usable_enrichment_recommendation_copy(
                enrichment.description or enrichment.search_result_description,
                enrichment_place=enrichment,
                raw_place=place,
                city_name=city_name,
                country_name=country_name,
            )
            if enrichment_identity_compatible
            else None
        )
        base_recommendation = combine_recommendation_copy(
            enrichment_recommendation_copy,
            place.note,
        )
        semantic_description = (
            usable_semantic_description(
                enrichment.semantic_description,
                enrichment_place=enrichment,
                raw_place=place,
                city_name=city_name,
                country_name=country_name,
            )
            if use_semantic_descriptions
            else None
        )
        award_recommendation_copy = trust_signal_recommendation_copy(place_trust_signals)
        base_why_recommended = semantic_description or base_recommendation
        why_recommended = manual_note or combine_recommendation_copy(
            award_recommendation_copy,
            base_why_recommended,
        )
        if "vibe_tags" in override:
            override_vibe_tags = coerce_string_list(override.get("vibe_tags"))
            vibe_tags = sorted({slugify(tag) for tag in override_vibe_tags if slugify(tag)})
        else:
            vibe_tags = derive_vibe_tags(
                place,
                enrichment=enrichment,
                category=primary_category,
                tags=tags,
                note=note,
                why_recommended=why_recommended,
                top_pick=top_pick,
            )
            if use_semantic_enrichment:
                vibe_tags = sorted({*vibe_tags, *enrichment.semantic_vibe_tags})
        marker_icon = derive_marker_icon(
            place,
            enrichment=enrichment,
            category=primary_category,
            note=note,
            why_recommended=why_recommended,
        )
        manual_rank = as_int(override.get("manual_rank")) or 0
        status = (
            as_string(override.get("status"))
            or normalize_business_status(enrichment.business_status)
            or normalize_business_status(place.business_status)
            or "active"
        )
        preferred_name = place.name
        if prefer_enrichment_names and enrichment.display_name:
            preferred_name = enrichment.display_name
        normalized_name = as_string(override.get("name")) or preferred_name
        normalized_address = normalize_guide_display_address(
            enrichment.address_display_en or enrichment.formatted_address or place.address,
            country_name=country_name,
        )
        maps_url = build_public_google_maps_url(
            name=normalized_name,
            address=normalized_address,
            lat=place.lat,
            lng=place.lng,
            raw_maps_url=place.maps_url,
            google_maps_uri=enrichment.google_maps_uri,
            google_place_id=enrichment.google_place_id,
        )
        enrichment_display_price = display_price_source_for_place(
            enrichment,
            country_name=country_name,
        )
        raw_display_price = display_price_range_for_raw_place(place)
        price_source = (
            enrichment_display_price[0]
            if enrichment_display_price
            else "raw_price_level"
            if raw_display_price
            else None
        )
        display_price = (
            enrichment_display_price[1] if enrichment_display_price else raw_display_price
        )
        budget_fields = derive_place_budget_fields(
            enrichment_place=enrichment,
            raw_place=place,
            price_source=price_source,
            display_price=display_price,
            primary_category=primary_category,
            tags=tags,
            country_name=country_name,
        )

        normalized = NormalizedPlace(
            id=place_id,
            name=normalized_name,
            address=normalized_address,
            lat=place.lat,
            lng=place.lng,
            maps_url=maps_url,
            cid=place.cid,
            google_id=place.google_id,
            google_place_id=enrichment.google_place_id,
            google_place_resource_name=enrichment.google_place_resource_name,
            rating=enrichment.rating if enrichment.rating is not None else place.rating,
            user_rating_count=(
                enrichment.user_rating_count
                if enrichment.user_rating_count is not None
                else place.user_rating_count
            ),
            primary_category=primary_category,
            primary_category_localized=primary_category_localized,
            marker_icon=marker_icon,
            tags=tags,
            visible_tags=derive_visible_place_tags(
                tags=tags,
                semantic_tags=semantic_tags,
                category=primary_category,
            ),
            vibe_tags=vibe_tags,
            price_range=display_price,
            budget_kind=budget_fields["budget_kind"],
            budget_tier=budget_fields["budget_tier"],
            budget_label=budget_fields["budget_label"],
            website=as_http_url(enrichment.website),
            reservation_links=enrichment.reservation_links,
            neighborhood=neighborhood,
            note=note,
            why_recommended=why_recommended,
            added_by=added_by,
            main_photo_path=None,
            photo_url=cached_place_photo_url(enrichment_cache_entry) or place.photo_url,
            top_pick=top_pick,
            hidden=hidden,
            manual_rank=manual_rank,
            status=status,
            trust_signals=place_trust_signals,
        )
        normalized.provenance = build_place_provenance(
            raw=raw,
            raw_place=place,
            override=override,
            normalized=normalized,
            enrichment_cache_entry=enrichment_cache_entry,
            enrichment=enrichment,
            primary_category=primary_category,
            primary_category_localized=primary_category_localized,
            tags=tags,
            city_name=city_name,
            country_name=country_name,
            neighborhood_uses_enrichment=neighborhood_uses_enrichment,
            top_pick_override=top_pick_override,
            status=status,
            prefer_enrichment_names=prefer_enrichment_names,
            trust_signals=place_trust_signals,
        )
        normalized_places.append(normalized)
        if primary_category and not place_is_permanently_closed(normalized):
            category_counter[primary_category] += 1

    normalized_places.sort(
        key=lambda place: (
            place.hidden,
            not place.top_pick,
            -place.manual_rank,
            place.name.lower(),
        )
    )

    display_places = [place for place in normalized_places if place_is_visible_in_ui(place)]

    featured_place_ids = [
        place_id
        for place_id in coerce_string_list(list_override.get("featured_place_ids"))
        if any(place.id == place_id for place in display_places)
    ]
    auto_featured_ids = [place.id for place in display_places if place.top_pick]
    if featured_place_ids:
        featured_place_ids = list(dict.fromkeys([*featured_place_ids, *auto_featured_ids]))[:3]
    else:
        featured_place_ids = auto_featured_ids[:3]
    best_hit_place_ids, best_hit_min_rating, best_hit_min_reviews = select_best_hit_place_ids(
        display_places,
        featured_place_ids=featured_place_ids,
    )

    top_categories = [name for name, _count in category_counter.most_common(4)]
    generated_at = stable_generated_at(raw, enrichment_cache)
    guide_center = guide_location_center(display_places)
    suppress_far_map_pins(slug, display_places, guide_center)
    guide_center = guide_location_center(display_places)
    warn_far_map_pins(slug, display_places, guide_center)

    return Guide(
        slug=slug,
        title=title,
        description=description,
        author=author,
        place_photo_mode=place_photo_mode,
        source_url=raw.source_url,
        list_id=raw.list_id,
        country_name=country_name or "Unknown",
        country_code=country_code,
        city_name=city_name or title,
        list_tags=list_tags,
        featured_place_ids=featured_place_ids,
        best_hit_place_ids=best_hit_place_ids,
        best_hit_min_rating=best_hit_min_rating,
        best_hit_min_reviews=best_hit_min_reviews,
        top_categories=top_categories,
        generated_at=generated_at,
        place_count=len(display_places),
        center_lat=guide_center[0],
        center_lng=guide_center[1],
        places=normalized_places,
    )


def build_place_provenance(
    *,
    raw: RawSavedList,
    raw_place: RawPlace,
    override: dict[str, Any],
    normalized: NormalizedPlace,
    enrichment_cache_entry: EnrichmentCacheEntry | None,
    enrichment: EnrichmentPlace,
    primary_category: str | None,
    primary_category_localized: str | None,
    tags: list[str],
    city_name: str | None,
    country_name: str | None,
    neighborhood_uses_enrichment: bool,
    top_pick_override: bool | None,
    status: str,
    prefer_enrichment_names: bool,
    trust_signals: list[TrustSignal],
) -> PlaceProvenance:
    manual_name = as_string(override.get("name"))
    manual_category = as_string(override.get("primary_category"))
    manual_note = as_string(override.get("note"))
    manual_neighborhood = as_string(override.get("neighborhood"))
    manual_status = as_string(override.get("status"))

    provenance = PlaceProvenance()
    provenance.name = (
        manual_place_field(normalized.name)
        if manual_name
        else google_places_field(normalized.name, enrichment_cache_entry)
        if prefer_enrichment_names and enrichment.display_name
        else google_list_field(normalized.name, raw)
    )
    if normalized.address:
        enrichment_display_address = normalize_guide_display_address(
            enrichment.address_display_en,
            country_name=country_name,
        )
        enrichment_formatted_address = normalize_guide_display_address(
            enrichment.formatted_address,
            country_name=country_name,
        )
        provenance.address = (
            google_places_field(normalized.address, enrichment_cache_entry)
            if (
                normalized.address == enrichment_display_address
                or normalized.address == enrichment_formatted_address
            )
            else google_list_field(normalized.address, raw)
        )
    if normalized.lat is not None:
        provenance.lat = google_list_field(normalized.lat, raw)
    if normalized.lng is not None:
        provenance.lng = google_list_field(normalized.lng, raw)
    provenance.maps_url = (
        google_places_field(normalized.maps_url, enrichment_cache_entry)
        if enrichment.google_maps_uri
        else google_list_field(normalized.maps_url, raw)
    )
    if normalized.cid:
        provenance.cid = google_list_field(normalized.cid, raw)
    if normalized.google_id:
        provenance.google_id = google_list_field(normalized.google_id, raw)
    if normalized.google_place_id:
        provenance.google_place_id = google_places_field(normalized.google_place_id, enrichment_cache_entry)
    if normalized.google_place_resource_name:
        provenance.google_place_resource_name = google_places_field(
            normalized.google_place_resource_name,
            enrichment_cache_entry,
        )
    if normalized.rating is not None:
        provenance.rating = (
            google_places_field(normalized.rating, enrichment_cache_entry)
            if enrichment.rating is not None
            else google_list_field(normalized.rating, raw)
        )
    if normalized.user_rating_count is not None:
        provenance.user_rating_count = (
            google_places_field(normalized.user_rating_count, enrichment_cache_entry)
            if enrichment.user_rating_count is not None
            else google_list_field(normalized.user_rating_count, raw)
        )
    if primary_category:
        provenance.primary_category = (
            manual_place_field(primary_category)
            if manual_category
            else google_places_field(primary_category, enrichment_cache_entry)
            if enrichment.primary_type_display_name or enrichment.primary_type
            else google_list_field(primary_category, raw)
        )
    if primary_category_localized:
        provenance.primary_category_localized = google_places_field(primary_category_localized, enrichment_cache_entry)
    if normalized.website:
        provenance.website = (
            google_maps_page_field(normalized.website, enrichment_cache_entry)
            if cache_entry_has_merged_page_source(enrichment_cache_entry)
            else google_places_field(normalized.website, enrichment_cache_entry)
        )
    if normalized.reservation_links:
        reservation_link_values = [link.model_dump(mode="json") for link in normalized.reservation_links]
        if cache_entry_has_merged_page_source(enrichment_cache_entry):
            provenance.reservation_links = google_maps_page_field(
                reservation_link_values,
                enrichment_cache_entry,
            )
        else:
            provenance.reservation_links = google_places_field(
                reservation_link_values,
                enrichment_cache_entry,
            )
    provenance.tags = build_tag_provenance(
        raw=raw,
        raw_place=raw_place,
        override=override,
        enrichment_cache_entry=enrichment_cache_entry,
        enrichment=enrichment,
        primary_category=primary_category,
        primary_category_field=provenance.primary_category,
        city_name=city_name,
        tags=tags,
    )
    if normalized.neighborhood:
        provenance.neighborhood = (
            manual_place_field(normalized.neighborhood)
            if manual_neighborhood
            else google_places_field(normalized.neighborhood, enrichment_cache_entry)
            if neighborhood_uses_enrichment
            else google_list_field(normalized.neighborhood, raw)
        )
    if normalized.note:
        provenance.note = manual_place_field(normalized.note) if manual_note else google_list_field(normalized.note, raw)
    if normalized.why_recommended:
        provenance.why_recommended = (
            manual_place_field(normalized.why_recommended)
            if manual_note
            else None
        )
    if normalized.added_by:
        provenance.added_by = (
            manual_place_field(normalized.added_by)
            if "added_by" in override
            else google_list_field(normalized.added_by, raw)
        )
    provenance.top_pick = (
        manual_place_field(normalized.top_pick)
        if top_pick_override is not None
        else google_list_field(normalized.top_pick, raw)
    )
    if "hidden" in override:
        provenance.hidden = manual_place_field(normalized.hidden)
    if "manual_rank" in override:
        provenance.manual_rank = manual_place_field(normalized.manual_rank)
    if manual_status:
        provenance.status = manual_place_field(status)
    elif enrichment.business_status:
        provenance.status = google_places_field(status, enrichment_cache_entry)
    elif raw_place.business_status:
        provenance.status = google_list_field(status, raw)
    if trust_signals:
        provenance.trust_signals = trust_signal_field(trust_signals)
    return provenance


def build_tag_provenance(
    *,
    raw: RawSavedList,
    raw_place: RawPlace,
    override: dict[str, Any],
    enrichment_cache_entry: EnrichmentCacheEntry | None,
    enrichment: EnrichmentPlace,
    primary_category: str | None,
    primary_category_field: PlaceField | None,
    city_name: str | None,
    tags: list[str],
) -> list[PlaceField]:
    ranked_fields: dict[str, tuple[int, PlaceField]] = {}

    def put_tag(value: str | None, field: PlaceField | None, *, priority: int) -> None:
        tag = as_string(value)
        if tag is None or field is None:
            return
        existing = ranked_fields.get(tag)
        if existing is None or existing[0] < priority:
            ranked_fields[tag] = (priority, field)

    for tag in coerce_string_list(override.get("tags")):
        put_tag(tag, manual_place_field(tag), priority=30)
    if city_name:
        tag = normalize_tag_slug(city_name)
        put_tag(tag, google_list_field(tag, raw), priority=10)
    for tag in derive_locality_tags(
        raw_place.address,
        raw_place=raw_place,
        city_name=city_name,
        enrichment=enrichment,
        category=primary_category,
    ):
        put_tag(tag, google_list_field(tag, raw), priority=10)
    if primary_category:
        tag = normalize_tag_slug(primary_category)
        put_tag(
            tag,
            source_place_field(tag, primary_category_field),
            priority=30 if primary_category_field and primary_category_field.source == "manual" else 20,
        )
    for tag in derive_enrichment_type_tags(enrichment, category=primary_category):
        put_tag(tag, google_places_field(tag, enrichment_cache_entry), priority=20)
    for tag in derive_raw_place_type_tags(raw_place, category=primary_category):
        put_tag(tag, google_list_field(tag, raw), priority=15)

    return [ranked_fields[tag][1] for tag in tags if tag in ranked_fields]


def source_place_field(value: Any, source_field: PlaceField | None) -> PlaceField | None:
    if source_field is None:
        return None
    return PlaceField(
        value=value,
        source=source_field.source,
        fetched_at=source_field.fetched_at,
        expires_at=source_field.expires_at,
    )


def manual_place_field(value: Any) -> PlaceField:
    return PlaceField(value=value, source="manual")


def trust_signal_field(signals: list[TrustSignal]) -> PlaceField:
    fetched_dates = [
        parsed
        for signal in signals
        if (parsed := metadata_datetime_or_none(signal.fetched_at)) is not None
    ]
    fetched_at = max(fetched_dates).isoformat() if fetched_dates else None
    return PlaceField(value=signals, source="trust_signal", fetched_at=fetched_at)


TABELOG_HYAKUMEITEN_GENRE_DISPLAY = {
    "うどん": "Udon",
    "うなぎ": "Unagi",
    "お好み焼き": "Okonomiyaki",
    "すき焼き・しゃぶしゃぶ": "Sukiyaki and Shabu-Shabu",
    "そば": "Soba",
    "とんかつ": "Tonkatsu",
    "アイス・ジェラート": "Ice Cream and Gelato",
    "アジア・エスニック": "Asian and Ethnic",
    "イタリアン": "Italian",
    "カフェ": "Cafe",
    "カレー": "Curry",
    "スイーツ": "Sweets",
    "ステーキ・鉄板焼き": "Steak and Teppanyaki",
    "スペイン料理": "Spanish",
    "ハンバーガー": "Hamburger",
    "バー": "Bar",
    "パン": "Bakery",
    "ピザ": "Pizza",
    "フレンチ": "French",
    "ラーメン": "Ramen",
    "中国料理": "Chinese",
    "創作料理・イノベーティブ": "Creative and Innovative",
    "和菓子・甘味処": "Wagashi and Sweets",
    "喫茶店": "Kissaten",
    "天ぷら": "Tempura",
    "寿司": "Sushi",
    "居酒屋": "Izakaya",
    "日本料理": "Japanese",
    "洋食": "Yoshoku",
    "焼き鳥": "Yakitori",
    "焼肉": "Yakiniku",
    "立ち飲み": "Standing Bar",
    "食堂": "Shokudo",
    "餃子": "Gyoza",
    "鳥料理": "Chicken",
}

def display_trust_signal(signal: TrustSignal) -> TrustSignal:
    display_label = trust_signal_display_label(signal)
    display_tier = trust_signal_display_tier(signal)
    if display_label == signal.label and display_tier == signal.tier:
        return signal
    return signal.model_copy(
        update={
            "display_label": display_label,
            "display_tier": display_tier,
        }
    )


def trust_signal_display_label(signal: TrustSignal) -> str:
    if signal.source == "tabelog" and signal.label == "The Tabelog Award":
        return "Tabelog"
    if signal.source == "tabelog" and signal.label == "Tabelog Hyakumeiten":
        return "Tabelog 100"
    return signal.label


def trust_signal_display_tier(signal: TrustSignal) -> str | None:
    if signal.source == "tabelog" and signal.label == "Tabelog Hyakumeiten":
        return display_tabelog_hyakumeiten_tier(signal.tier)
    return signal.tier


def display_tabelog_hyakumeiten_tier(tier: str | None) -> str | None:
    if not tier:
        return None
    cleaned = re.sub(r"\s*百名店\s*$", "", tier).strip()
    if not cleaned:
        return "Selected Restaurants"
    parts = cleaned.split()
    if len(parts) >= 2 and parts[-1].isupper():
        genre = " ".join(parts[:-1])
    else:
        genre = cleaned
    display_genre = TABELOG_HYAKUMEITEN_GENRE_DISPLAY.get(genre, genre)
    return display_genre


def google_list_field(value: Any, raw: RawSavedList) -> PlaceField:
    return PlaceField(
        value=value,
        source="google_list",
        fetched_at=raw.fetched_at,
        expires_at=raw.refresh_after,
    )


def google_places_field(value: Any, cache_entry: EnrichmentCacheEntry | None) -> PlaceField:
    source = "google_places"
    if cache_entry is not None and cache_entry.source == "google_maps_page":
        source = "google_maps_page"
    return PlaceField(
        value=value,
        source=source,
        fetched_at=cache_entry.fetched_at if cache_entry else None,
        expires_at=cache_entry.refresh_after if cache_entry else None,
    )


def google_maps_page_field(value: Any, cache_entry: EnrichmentCacheEntry | None) -> PlaceField:
    return PlaceField(
        value=value,
        source="google_maps_page",
        fetched_at=cache_entry.fetched_at if cache_entry else None,
        expires_at=cache_entry.refresh_after if cache_entry else None,
    )


def cache_entry_has_merged_page_source(cache_entry: EnrichmentCacheEntry | None) -> bool:
    return bool(
        cache_entry is not None
        and (
            cache_entry.source == "google_maps_page"
            or "google_maps_page" in cache_entry.merged_sources
        )
    )


def place_is_permanently_closed(place: NormalizedPlace) -> bool:
    return place.status == "closed-permanently"


def place_is_visible_in_ui(place: NormalizedPlace) -> bool:
    return not place.hidden and not place_is_permanently_closed(place)


def percentile(values: Sequence[float | int], rank: float) -> float | None:
    if not values:
        return None

    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])

    clamped_rank = min(max(rank, 0.0), 1.0)
    index = (len(sorted_values) - 1) * clamped_rank
    lower_index = math.floor(index)
    upper_index = math.ceil(index)
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    if lower_index == upper_index:
        return float(lower_value)

    fraction = index - lower_index
    return lower_value + (upper_value - lower_value) * fraction


def round_review_threshold(value: float) -> int:
    if value <= 25:
        step = 5
    elif value <= 100:
        step = 10
    elif value <= 500:
        step = 25
    elif value <= 2_000:
        step = 50
    else:
        step = 100
    return max(step, int(math.ceil(value / step) * step))


def round_rating_floor(value: float) -> float:
    clamped_value = min(BEST_HIT_MAX_RATING, max(BEST_HIT_MIN_RATING, value))
    return round(math.floor((clamped_value + 1e-9) * 10) / 10, 1)


def guide_best_hit_review_threshold(places: list[NormalizedPlace]) -> int | None:
    review_counts = sorted(
        place.user_rating_count
        for place in places
        if place.rating is not None and place.user_rating_count is not None and place.user_rating_count > 0
    )
    if not review_counts:
        return None

    review_count_percentile = percentile(review_counts, 0.75)
    if review_count_percentile is None:
        return None

    adaptive_threshold = review_count_percentile * BEST_HIT_REVIEW_THRESHOLD_MULTIPLIER
    clamped_threshold = min(
        BEST_HIT_MAX_REVIEW_THRESHOLD,
        max(BEST_HIT_MIN_REVIEW_THRESHOLD, adaptive_threshold),
    )
    return round_review_threshold(clamped_threshold)


def guide_best_hit_score(
    place: NormalizedPlace,
    *,
    baseline_rating: float,
    prior_weight: int,
) -> float:
    rating = place.rating
    review_count = place.user_rating_count
    if rating is None or review_count is None or review_count <= 0:
        return 0.0

    return ((review_count * rating) + (prior_weight * baseline_rating)) / (review_count + prior_weight)


def guide_best_hit_rating_floor(places: list[NormalizedPlace]) -> float:
    ratings = sorted(
        place.rating
        for place in places
        if place.rating is not None and place.user_rating_count is not None and place.user_rating_count > 0
    )
    if not ratings:
        return BEST_HIT_MIN_RATING

    percentile_rating = percentile(ratings, BEST_HIT_RATING_PERCENTILE)
    if percentile_rating is None:
        return BEST_HIT_MIN_RATING

    return round_rating_floor(percentile_rating)


def select_best_hit_place_ids(
    places: list[NormalizedPlace],
    *,
    featured_place_ids: list[str],
) -> tuple[list[str], float | None, int | None]:
    featured_ids = set(featured_place_ids)
    rated_places = [
        place
        for place in places
        if place.rating is not None and place.user_rating_count is not None and place.user_rating_count > 0
    ]
    if not rated_places:
        return ([], None, None)

    review_threshold = guide_best_hit_review_threshold(rated_places)
    baseline_rating = sum(place.rating for place in rated_places if place.rating is not None) / len(rated_places)

    def matching_places(min_reviews: int, rating_floor: float) -> list[NormalizedPlace]:
        return [
            place
            for place in rated_places
            if place.id not in featured_ids
            and place.rating is not None
            and place.rating >= rating_floor
            and (place.user_rating_count or 0) >= min_reviews
        ]

    applied_review_threshold = review_threshold or BEST_HIT_MIN_REVIEW_THRESHOLD
    relaxed_review_threshold = applied_review_threshold
    if review_threshold is not None:
        relaxed_review_threshold = max(
            BEST_HIT_MIN_REVIEW_THRESHOLD,
            round_review_threshold(review_threshold * BEST_HIT_RELAXED_REVIEW_THRESHOLD_MULTIPLIER),
        )

    review_qualified_places = matching_places(applied_review_threshold, BEST_HIT_MIN_RATING)
    if len(review_qualified_places) < BEST_HIT_MIN_CANDIDATE_COUNT and relaxed_review_threshold < applied_review_threshold:
        applied_review_threshold = relaxed_review_threshold
        review_qualified_places = matching_places(applied_review_threshold, BEST_HIT_MIN_RATING)

    if not review_qualified_places:
        return ([], None, None)

    rating_floor = guide_best_hit_rating_floor(review_qualified_places)
    candidates = matching_places(applied_review_threshold, rating_floor)
    while (
        rating_floor > BEST_HIT_MIN_RATING
        and len(candidates) < BEST_HIT_MIN_CANDIDATE_COUNT
    ):
        rating_floor = round_rating_floor(rating_floor - 0.1)
        candidates = matching_places(applied_review_threshold, rating_floor)

    if len(candidates) < BEST_HIT_MAX_COUNT and review_threshold is not None:
        if relaxed_review_threshold < applied_review_threshold:
            relaxed_candidates = matching_places(relaxed_review_threshold, rating_floor)
            if len(relaxed_candidates) > len(candidates):
                candidates = relaxed_candidates
                applied_review_threshold = relaxed_review_threshold

    if not candidates:
        return ([], None, None)

    scored_candidates = sorted(
        candidates,
        key=lambda place: (
            -(place.rating or 0.0),
            -guide_best_hit_score(place, baseline_rating=baseline_rating, prior_weight=applied_review_threshold),
            -(place.user_rating_count or 0),
            -place.manual_rank,
            place.name.lower(),
        ),
    )
    return (
        [place.id for place in scored_candidates[:BEST_HIT_MAX_COUNT]],
        rating_floor,
        applied_review_threshold,
    )


def summarize_guide(guide: Guide) -> GuideManifest:
    featured_names = [
        place.name
        for place in guide.places
        if place.id in set(guide.featured_place_ids) and place_is_visible_in_ui(place)
    ]
    return GuideManifest(
        slug=guide.slug,
        title=guide.title,
        description=guide.description,
        author=guide.author,
        country_name=guide.country_name,
        country_code=guide.country_code,
        center_lat=guide.center_lat,
        center_lng=guide.center_lng,
        city_name=guide.city_name,
        list_tags=guide.list_tags,
        place_count=guide.place_count,
        featured_names=featured_names[:3],
        top_categories=guide.top_categories,
    )


def guide_location_center(places: list[NormalizedPlace]) -> tuple[float | None, float | None]:
    coordinates = guide_place_coordinates(places)
    if not coordinates:
        return (None, None)

    inlier_coordinates = guide_location_inlier_coordinates(coordinates)
    lat = sum(latitude for latitude, _longitude in inlier_coordinates) / len(inlier_coordinates)
    lng = sum(longitude for _latitude, longitude in inlier_coordinates) / len(inlier_coordinates)
    return (lat, lng)


def guide_geocoded_coordinate_count(places: list[NormalizedPlace]) -> int:
    return sum(1 for place in places if place.lat is not None and place.lng is not None)


def warn_far_map_pins(
    slug: str,
    places: list[NormalizedPlace],
    center: tuple[float | None, float | None],
) -> None:
    warning_distance_meters = guide_map_pin_warning_distance_meters(places, center)
    center_lat, center_lng = center
    if center_lat is None or center_lng is None or warning_distance_meters is None:
        return

    for place in places:
        if place.lat is None or place.lng is None:
            continue

        distance_meters = haversine_meters(center_lat, center_lng, place.lat, place.lng)
        if distance_meters < warning_distance_meters:
            continue

        print(
            "WARNING: "
            f"{slug}:{place.id} map pin for {place.name!r} is "
            f"{distance_meters / 1000:.0f} km from the guide center; "
            "check whether it belongs in this city/country."
        )


def suppress_far_map_pins(
    slug: str,
    places: list[NormalizedPlace],
    center: tuple[float | None, float | None],
) -> None:
    if guide_geocoded_coordinate_count(places) < 4:
        return

    suppression_distance_meters = guide_map_pin_suppression_distance_meters(places, center)
    center_lat, center_lng = center
    if center_lat is None or center_lng is None or suppression_distance_meters is None:
        return

    for place in places:
        if place.lat is None or place.lng is None:
            continue

        distance_meters = haversine_meters(center_lat, center_lng, place.lat, place.lng)
        if distance_meters < suppression_distance_meters:
            continue

        print(
            "WARNING: "
            f"Suppressing map pin for {slug}:{place.id} [{place.name}] because it is "
            f"{distance_meters / 1000:.0f} km from the guide center.",
            flush=True,
        )
        place.lat = None
        place.lng = None


def guide_map_pin_warning_distance_meters(
    places: list[NormalizedPlace],
    center: tuple[float | None, float | None],
) -> float | None:
    center_lat, center_lng = center
    if center_lat is None or center_lng is None:
        return None

    coordinates = guide_place_coordinates(places)
    if not coordinates:
        return None
    if len(coordinates) < 4:
        return MAP_PIN_DISTANCE_WARNING_MIN_METERS

    return _guide_map_pin_warning_distance_meters_cached(
        coordinates,
        center_lat,
        center_lng,
    )


def guide_map_pin_suppression_distance_meters(
    places: list[NormalizedPlace],
    center: tuple[float | None, float | None],
) -> float | None:
    warning_distance_meters = guide_map_pin_warning_distance_meters(places, center)
    if warning_distance_meters is None:
        return None
    return max(
        MAP_PIN_DISTANCE_SUPPRESSION_MIN_METERS,
        warning_distance_meters * MAP_PIN_DISTANCE_SUPPRESSION_MULTIPLIER,
    )


def guide_location_inliers(coordinates: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return list(guide_location_inlier_coordinates(tuple(coordinates)))


def guide_place_coordinates(places: list[NormalizedPlace]) -> tuple[tuple[float, float], ...]:
    return tuple(
        (place.lat, place.lng)
        for place in places
        if place.lat is not None and place.lng is not None
    )


def guide_location_inlier_coordinates(
    coordinates: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    return _guide_location_inliers_cached(coordinates)


@lru_cache(maxsize=None)
def _guide_map_pin_warning_distance_meters_cached(
    coordinates: tuple[tuple[float, float], ...],
    center_lat: float,
    center_lng: float,
) -> float:
    inlier_coordinates = guide_location_inlier_coordinates(coordinates)
    inlier_radius_meters = max(
        haversine_meters(center_lat, center_lng, latitude, longitude)
        for latitude, longitude in inlier_coordinates
    )
    return max(
        MAP_PIN_DISTANCE_WARNING_MIN_METERS,
        inlier_radius_meters + MAP_PIN_DISTANCE_WARNING_BUFFER_METERS,
    )


@lru_cache(maxsize=None)
def _guide_location_inliers_cached(
    coordinates: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    if len(coordinates) < 4:
        return coordinates

    medoid_lat, medoid_lng = min(
        coordinates,
        key=lambda coordinate: sum(
            haversine_meters(coordinate[0], coordinate[1], latitude, longitude)
            for latitude, longitude in coordinates
        ),
    )
    distances = [
        haversine_meters(medoid_lat, medoid_lng, latitude, longitude)
        for latitude, longitude in coordinates
    ]
    median_distance = median(distances)
    median_absolute_deviation = median(abs(distance - median_distance) for distance in distances)
    outlier_threshold = max(50_000.0, median_distance + 6 * median_absolute_deviation)

    inliers = [
        coordinate
        for coordinate, distance in zip(coordinates, distances, strict=True)
        if distance <= outlier_threshold
    ]
    return tuple(inliers) or coordinates


def enrich_raw_sources(
    *,
    force_refresh: bool,
    missing_only: bool = False,
    place_selectors: list[str] | None = None,
    refresh_workers: int = DEFAULT_REFRESH_WORKERS,
    refresh_startup_jitter_seconds: float = DEFAULT_REFRESH_STARTUP_JITTER_SECONDS,
) -> None:
    api_key = google_places_api_key()
    cache_payloads: dict[str, dict[str, EnrichmentCacheEntry]] = {}
    enrich_jobs: list[tuple[str, str, str, str, dict[str, Any], str | None, str | None]] = []
    normalized_place_selectors = normalize_place_selectors(place_selectors or [])
    matched_place_selectors: set[str] = set()

    for raw_path in sorted(RAW_DIR.glob("*.json")):
        raw = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))
        slug = raw_path.stem
        city_name, country_name = guide_location_context(slug, raw)
        place_override_map = read_json(PLACE_OVERRIDES_DIR / f"{slug}.json")
        cache_payload = load_places_cache(slug)
        if not normalized_place_selectors:
            cache_payload, pruned_count = prune_places_cache_to_raw_places(cache_payload, raw)
            if pruned_count:
                save_places_cache(slug, cache_payload, allow_empty_overwrite=True)
        cache_payloads[slug] = cache_payload
        for place in raw.places:
            place_id = stable_place_id(place, source_type=raw.configured_source_type)
            if normalized_place_selectors:
                selector_matches = place_selector_matches(
                    slug,
                    place,
                    place_id=place_id,
                    selectors=normalized_place_selectors,
                )
                matched_place_selectors.update(selector_matches)
                if not selector_matches:
                    continue
            override = place_override_for_ui_copy(slug, place_id, place_override_map.get(place_id, {}))
            override_google_place_id = as_string(override.get("google_place_id"))
            refresh_reason = enrichment_refresh_reason(
                place,
                cache_payload.get(place_id),
                city_name=city_name,
                country_name=country_name,
                signature_google_place_id=override_google_place_id,
                force_refresh=force_refresh,
                missing_only=missing_only,
            )
            if refresh_reason is None:
                continue

            enrich_jobs.append(
                (
                    slug,
                    place_id,
                    place.name,
                    refresh_reason,
                    place.model_dump(mode="json"),
                    city_name,
                    country_name,
                )
            )

    if normalized_place_selectors:
        missing_selectors = sorted(normalized_place_selectors - matched_place_selectors)
        if missing_selectors:
            missing_text = ", ".join(missing_selectors)
            raise RuntimeError(f"No configured place matched: {missing_text}")

    if not enrich_jobs:
        return

    enrich_jobs.sort(key=enrichment_job_priority)

    effective_startup_jitter_seconds = (
        refresh_startup_jitter_seconds if len(enrich_jobs) > 1 else 0
    )
    max_workers = max(1, min(refresh_workers, len(enrich_jobs)))
    if max_workers == 1 or len(enrich_jobs) == 1:
        for slug, place_id, place_name, refresh_reason, place_payload, city_name, country_name in enrich_jobs:
            entry = enrich_place_job(
                slug,
                place_id,
                place_name,
                refresh_reason,
                place_payload,
                city_name=city_name,
                country_name=country_name,
                api_key=api_key,
                refresh_startup_jitter_seconds=effective_startup_jitter_seconds,
                existing_entry=cache_payloads[slug].get(place_id),
            )
            cache_payloads[slug][place_id] = entry
            save_places_cache(slug, cache_payloads[slug])
        return

    print(f"Running {len(enrich_jobs)} enrichment jobs with {max_workers} workers")
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {
            executor.submit(
                enrich_place_job,
                slug,
                place_id,
                place_name,
                refresh_reason,
                place_payload,
                city_name=city_name,
                country_name=country_name,
                api_key=api_key,
                refresh_startup_jitter_seconds=effective_startup_jitter_seconds,
                existing_entry=cache_payloads[slug].get(place_id),
            ): (slug, place_id)
            for slug, place_id, place_name, refresh_reason, place_payload, city_name, country_name in enrich_jobs
        }
        for future in as_completed(future_map):
            slug, place_id = future_map[future]
            cache_payloads[slug][place_id] = future.result()
            save_places_cache(slug, cache_payloads[slug])
    except KeyboardInterrupt:
        print("Interrupt received; terminating enrichment workers.", flush=True)
        terminate_executor(executor)
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def enrichment_source_refresh_lists(place_selectors: Sequence[str] | None) -> list[str]:
    normalized_place_selectors = normalize_place_selectors(list(place_selectors or []))
    if not normalized_place_selectors:
        return []

    refresh_slugs = {
        selector.removeprefix("guide-slug:")
        for selector in normalized_place_selectors
        if selector.startswith("guide-slug:") and selector.removeprefix("guide-slug:")
    }
    for raw_path in sorted(RAW_DIR.glob("*.json")):
        raw = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))
        for place in raw.places:
            place_id = stable_place_id(place, source_type=raw.configured_source_type)
            if place_selector_matches(
                raw_path.stem,
                place,
                place_id=place_id,
                selectors=normalized_place_selectors,
            ):
                refresh_slugs.add(raw_path.stem)
                break
    return sorted(refresh_slugs)


def refresh_cached_semantic_descriptions(
    *,
    force_refresh: bool = False,
    place_selectors: Sequence[str] | None = None,
) -> tuple[int, int, int]:
    return refresh_cached_semantic_enrichment(
        enable_semantics=False,
        enable_description=True,
        force_description=force_refresh,
        place_selectors=place_selectors,
    )


def refresh_cached_semantic_enrichment(
    *,
    enable_semantics: bool,
    enable_description: bool,
    force_semantics: bool = False,
    force_description: bool = False,
    place_selectors: Sequence[str] | None = None,
) -> tuple[int, int, int]:
    normalized_place_selectors = normalize_place_selectors(place_selectors or [])
    matched_place_selectors: set[str] = set()
    cache_payloads: dict[str, dict[str, EnrichmentCacheEntry]] = {}
    semantic_jobs: list[tuple[str, str, RawPlace, EnrichmentCacheEntry, str | None, str | None, bool]] = []
    updated_count = 0
    reused_count = 0
    skipped_count = 0

    for raw_path in sorted(RAW_DIR.glob("*.json")):
        raw = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))
        slug = raw_path.stem
        city_name, country_name = guide_location_context(slug, raw)
        place_override_map = read_json(PLACE_OVERRIDES_DIR / f"{slug}.json")
        cache_payload = load_places_cache(slug)
        if not normalized_place_selectors:
            cache_payload, pruned_count = prune_places_cache_to_raw_places(cache_payload, raw)
            if pruned_count:
                save_places_cache(slug, cache_payload, allow_empty_overwrite=True)
        cache_payloads[slug] = cache_payload

        for place in raw.places:
            place_id = stable_place_id(place, source_type=raw.configured_source_type)
            if normalized_place_selectors:
                selector_matches = place_selector_matches(
                    slug,
                    place,
                    place_id=place_id,
                    selectors=normalized_place_selectors,
                )
                matched_place_selectors.update(selector_matches)
                if not selector_matches:
                    continue

            entry = cache_payload.get(place_id)
            if not cache_entry_has_publishable_enrichment(entry) or entry is None or entry.place is None:
                skipped_count += 1
                continue
            override = place_override_for_ui_copy(slug, place_id, place_override_map.get(place_id, {}))
            suppress_description = bool(as_string(override.get("note")))
            semantic_jobs.append((slug, place_id, place, entry, city_name, country_name, suppress_description))

    if normalized_place_selectors:
        missing_selectors = sorted(normalized_place_selectors - matched_place_selectors)
        if missing_selectors:
            missing_text = ", ".join(missing_selectors)
            raise RuntimeError(f"No configured place matched: {missing_text}")

    pending_save_slug: str | None = None
    for slug, place_id, place, entry, city_name, country_name, suppress_description in semantic_jobs:
        if pending_save_slug is not None and slug != pending_save_slug:
            save_places_cache(pending_save_slug, cache_payloads[pending_save_slug])
            pending_save_slug = None

        assert entry.place is not None
        previous_semantics = semantic_enrichment_state(entry.place)
        previous_description = entry.place.semantic_description
        previous_signature = entry.place.semantic_description_signature
        apply_semantic_enrichment(
            entry.place,
            raw_place=place,
            city_name=city_name,
            country_name=country_name,
            existing_entry=entry,
            raw_note=place.note,
            enable_semantics=enable_semantics,
            enable_description=enable_description,
            force_semantics=force_semantics,
            force_description=force_description,
            suppress_description=suppress_description,
        )
        current_semantics = semantic_enrichment_state(entry.place)
        current_description = entry.place.semantic_description
        current_signature = entry.place.semantic_description_signature
        semantic_changed = enable_semantics and current_semantics != previous_semantics
        description_changed = enable_description and (
            current_description != previous_description
            or current_signature != previous_signature
        )
        if semantic_changed or description_changed:
            requested = []
            if semantic_changed:
                requested.append("semantic enrichment")
            if description_changed:
                requested.append("semantic description")
            requested_label = " and ".join(requested)
            print(f"Generated {requested_label} for {slug}:{place_id} [{place.name}]", flush=True)
            updated_count += 1
            pending_save_slug = slug
        elif (
            (not enable_semantics or semantic_enrichment_state_is_populated(current_semantics))
            and (not enable_description or current_description)
        ):
            reused_count += 1
        else:
            skipped_count += 1

    if pending_save_slug is not None:
        save_places_cache(pending_save_slug, cache_payloads[pending_save_slug])

    print(
        "Semantic enrichment refresh complete: "
        f"{updated_count} generated, {reused_count} reused, {skipped_count} skipped.",
        flush=True,
    )
    return updated_count, reused_count, skipped_count


def semantic_enrichment_state(place: EnrichmentPlace) -> tuple[str | None, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        place.semantic_neighborhood,
        tuple(place.semantic_tags),
        tuple(place.semantic_vibe_tags),
        tuple(place.semantic_types),
    )


def semantic_enrichment_state_is_populated(
    state: tuple[str | None, tuple[str, ...], tuple[str, ...], tuple[str, ...]],
) -> bool:
    neighborhood, tags, vibe_tags, types = state
    return bool(neighborhood or tags or vibe_tags or types)


def enrich_place_job(
    slug: str,
    place_id: str,
    place_name: str,
    refresh_reason: str,
    place_payload: dict[str, Any],
    *,
    city_name: str | None = None,
    country_name: str | None = None,
    api_key: str | None,
    refresh_startup_jitter_seconds: float,
    existing_entry: EnrichmentCacheEntry | None = None,
) -> EnrichmentCacheEntry:
    print(
        f"Enriching {slug}:{place_id} [{place_name}] ({refresh_reason})",
        flush=True,
    )
    sleep_for_refresh_startup_jitter(refresh_startup_jitter_seconds)
    place = RawPlace.model_validate(place_payload)
    place_override_map = read_json(PLACE_OVERRIDES_DIR / f"{slug}.json")
    override = place_override_for_ui_copy(slug, place_id, place_override_map.get(place_id, {}))
    suppress_description = bool(as_string(override.get("note")))
    override_google_place_id = as_string(override.get("google_place_id"))
    allow_identity_mismatch = as_bool(override.get("allow_enrichment_identity_mismatch")) is True
    existing_google_place_id = google_place_id_for_refresh_seed(
        place,
        existing_entry,
        allow_identity_mismatch=allow_identity_mismatch,
    )
    refreshed_entry = fetch_places_enrichment(
        place,
        city_name=city_name,
        country_name=country_name,
        google_place_id=override_google_place_id
        or existing_google_place_id,
        signature_google_place_id=override_google_place_id,
        api_key=api_key,
        existing_entry=existing_entry,
        suppress_description=suppress_description,
        allow_identity_mismatch=allow_identity_mismatch,
    )
    merged_entry, warning = preserve_existing_enrichment(
        slug=slug,
        place_id=place_id,
        place_name=place_name,
        existing_entry=existing_entry,
        refreshed_entry=refreshed_entry,
        raw_place=place,
        allow_identity_mismatch=allow_identity_mismatch,
    )
    if warning is not None:
        print(warning, flush=True)
    return merged_entry


def google_place_id_for_refresh_seed(
    raw_place: RawPlace,
    existing_entry: EnrichmentCacheEntry | None,
    *,
    allow_identity_mismatch: bool = False,
) -> str | None:
    existing_place = existing_entry.place if existing_entry else None
    if existing_place is None:
        return None
    google_place_id = as_string(existing_place.google_place_id)
    if google_place_id is None:
        return None
    if not allow_identity_mismatch and not enrichment_identity_is_compatible_with_raw(raw_place, existing_place):
        return None
    return google_place_id


def stable_place_id(place: RawPlace, *, source_type: str | None = None) -> str:
    cid = place.cid
    if cid:
        return f"cid:{cid}"

    google_id = place.google_id
    if google_id:
        normalized = google_id.strip("/").replace("/", "-")
        return f"gid:{normalized}"

    cid_from_url = extract_maps_cid(place.maps_url)
    if cid_from_url:
        return f"cid:{cid_from_url}"

    maps_place_token = place.maps_place_token or extract_maps_place_token(place.maps_url)
    if maps_place_token:
        return f"gms:{maps_place_token}"

    if source_type == "google_export_csv":
        maps_url_id = short_maps_url_id(place.maps_url)
        if maps_url_id:
            return maps_url_id

    fallback = f"{place.name or 'place'}-{place.lat}-{place.lng}"
    return f"slug:{slugify(fallback)}"


def short_maps_url_id(maps_url: str | None) -> str | None:
    normalized_url = as_string(maps_url)
    if normalized_url is None:
        return None
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return f"url:{digest}"


def derive_place_tags(
    place: RawPlace,
    city_name: str | None,
    *,
    enrichment: EnrichmentPlace,
    category: str | None,
) -> list[str]:
    tags: set[str] = set()
    if city_name:
        tags.add(normalize_tag_slug(city_name))
    tags.update(
        derive_locality_tags(
            place.address,
            raw_place=place,
            city_name=city_name,
            enrichment=enrichment,
            category=category,
        )
    )
    if category:
        tags.add(normalize_tag_slug(category))
    tags.update(derive_enrichment_type_tags(enrichment, category=category))
    tags.update(derive_raw_place_type_tags(place, category=category))
    return sorted(tag for tag in tags if tag)


def derive_visible_place_tags(
    *,
    tags: list[str],
    semantic_tags: list[str],
    category: str | None,
) -> list[str]:
    visible: list[str] = []
    for tag in semantic_tags:
        append_unique_tag(visible, tag)
    if category:
        category_tag = normalize_tag_slug(category)
        if category_tag:
            append_unique_tag(visible, category_tag)
    for tag in tags:
        append_unique_tag(visible, tag)
    return visible


def derive_locality_tags(
    address: str | None,
    *,
    raw_place: RawPlace,
    city_name: str | None,
    enrichment: EnrichmentPlace,
    category: str | None,
) -> list[str]:
    locality_tags: list[str] = []
    for locality in infer_address_localities(address, city_name=city_name):
        tag = normalize_tag_slug(locality)
        if tag:
            append_unique_tag(locality_tags, tag)

    if not locality_tags:
        return []

    has_specific_enrichment = bool(
        category
        or derive_enrichment_type_tags(enrichment, category=category)
        or derive_raw_place_type_tags(raw_place, category=category)
    )
    if has_specific_enrichment:
        return locality_tags[:1]
    return locality_tags[:2]


def derive_enrichment_type_tags(enrichment: EnrichmentPlace, *, category: str | None = None) -> list[str]:
    _primary_type, type_ids = normalized_enrichment_type_ids(
        enrichment.primary_type,
        category or enrichment.primary_type_display_name,
        enrichment.types,
    )
    return [type_id.replace("_", "-") for type_id in type_ids]


def derive_raw_place_type_tags(place: RawPlace, *, category: str | None = None) -> list[str]:
    _primary_type, type_ids = normalized_enrichment_type_ids(
        None,
        category,
        place.types,
    )
    return [type_id.replace("_", "-") for type_id in type_ids]


def raw_place_primary_category(place: RawPlace) -> str | None:
    raw_type_tags = derive_raw_place_type_tags(place)
    if not raw_type_tags:
        return None
    if "lodging" in raw_type_tags:
        return humanize_type_id("lodging")
    return humanize_type_id(raw_type_tags[0].replace("-", "_"))


RAW_PLACE_AMBIGUOUS_LOCATION_TYPE_TAGS = frozenset({"route", "premise", "political", "neighborhood"})


def raw_place_has_ambiguous_location_types(place: RawPlace) -> bool:
    raw_type_tags = {
        normalized
        for raw_type in place.types
        if (normalized := slugify(raw_type.replace("_", "-")))
    }
    return bool(raw_type_tags & RAW_PLACE_AMBIGUOUS_LOCATION_TYPE_TAGS)


def enrichment_category_looks_like_place_name(
    label: str | None,
    *,
    enrichment_place: EnrichmentPlace,
    raw_place: RawPlace,
) -> bool:
    normalized_label = normalize_type_lookup_text(as_string(label))
    if not normalized_label:
        return False
    candidate_names = {
        normalized
        for candidate in (enrichment_place.display_name, raw_place.name)
        if (normalized := normalize_type_lookup_text(as_string(candidate)))
    }
    return normalized_label in candidate_names


def usable_enrichment_primary_category(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
) -> str | None:
    candidate = enrichment_place.primary_type_display_name or humanize_type_id(enrichment_place.primary_type)
    if candidate is None:
        return None
    if enrichment_category_looks_like_place_name(
        candidate,
        enrichment_place=enrichment_place,
        raw_place=raw_place,
    ):
        return None
    if raw_place_has_ambiguous_location_types(raw_place):
        enrichment_type_tags = {
            normalized
            for raw_type in enrichment_place.types
            if (normalized := slugify(raw_type.replace("_", "-")))
        }
        if not enrichment_type_tags & RAW_PLACE_AMBIGUOUS_LOCATION_TYPE_TAGS:
            return None
    return candidate


def usable_enrichment_primary_category_localized(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
    canonical_primary_category: str | None,
) -> str | None:
    localized = enrichment_place.primary_type_display_name_localized
    if localized is None or canonical_primary_category is None:
        return None
    if enrichment_category_looks_like_place_name(
        localized,
        enrichment_place=enrichment_place,
        raw_place=raw_place,
    ):
        return None
    return localized


def normalize_enrichment_type_tag(value: str | None) -> str | None:
    if not value:
        return None
    tag = slugify(value.replace("_", "-"))
    if (
        not tag
        or tag in GENERIC_ENRICHMENT_TYPE_TAGS
        or any(pattern.match(tag) for pattern in INVALID_ENRICHMENT_TYPE_TAG_PATTERNS)
    ):
        return None
    return tag


def sanitize_enrichment_primary_category(value: str | None) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    normalized = " ".join(normalized.split())
    tag = slugify(normalized)
    has_ascii_alnum = bool(re.search(r"[a-zA-Z0-9]", normalized))
    if (
        any(pattern.match(normalized) for pattern in INVALID_ENRICHMENT_PRIMARY_CATEGORY_DISPLAY_PATTERNS)
        or (has_ascii_alnum and tag in GENERIC_ENRICHMENT_TYPE_TAGS)
        or (has_ascii_alnum and any(pattern.match(tag) for pattern in INVALID_ENRICHMENT_TYPE_TAG_PATTERNS))
    ):
        return None
    return normalized


def looks_english_category_label(value: str | None) -> bool:
    return bool(value and value.isascii() and re.search(r"[A-Za-z]", value))


def humanize_type_id(value: str | None) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    tag = slugify(normalized.replace("_", "-"))
    if (
        not tag
        or tag in GENERIC_ENRICHMENT_TYPE_TAGS
        or any(pattern.match(tag) for pattern in INVALID_ENRICHMENT_TYPE_TAG_PATTERNS)
    ):
        return None
    phrase = normalized.replace("_", " ").replace("-", " ").strip().lower()
    if not phrase:
        return None
    return phrase[:1].upper() + phrase[1:]


def canonical_primary_category_label(
    *,
    primary_type: str | None,
    display_name: str | None,
) -> str | None:
    sanitized_display_name = sanitize_enrichment_primary_category(display_name)
    if looks_english_category_label(sanitized_display_name):
        return sanitized_display_name
    return humanize_type_id(primary_type)


def localized_primary_category_label(
    *,
    raw_display_name: str | None,
    canonical_display_name: str | None,
) -> str | None:
    sanitized_display_name = sanitize_enrichment_primary_category(raw_display_name)
    if (
        sanitized_display_name is None
        or looks_english_category_label(sanitized_display_name)
    ):
        return None
    if canonical_display_name is None:
        return sanitized_display_name
    if (
        canonical_display_name
        and normalize_type_lookup_text(sanitized_display_name) == normalize_type_lookup_text(canonical_display_name)
    ):
        return None
    return sanitized_display_name


def canonicalize_enrichment_place(place: EnrichmentPlace | None) -> EnrichmentPlace | None:
    if place is None:
        return None

    raw_display_name = place.primary_type_display_name_localized or place.primary_type_display_name
    if place.primary_type is not None and normalize_enrichment_type_tag(place.primary_type) is None:
        if normalize_type_lookup_text(place.primary_type) == "item":
            raw_display_name = None
        place.primary_type = None
    canonical_display_name = canonical_primary_category_label(
        primary_type=place.primary_type,
        display_name=place.primary_type_display_name,
    )
    localized_display_name = localized_primary_category_label(
        raw_display_name=raw_display_name,
        canonical_display_name=canonical_display_name,
    )

    place.primary_type_display_name = canonical_display_name
    place.primary_type_display_name_localized = localized_display_name
    place.primary_type, place.types = normalize_enrichment_place_type_fields(place)
    place.display_name = sanitize_place_page_display_name(place.display_name)
    place.formatted_address = sanitize_place_page_formatted_address(place.formatted_address)
    place.address_display_en = sanitize_place_page_formatted_address(place.address_display_en)
    place.category_display_en = sanitize_enrichment_primary_category(place.category_display_en)
    place.description = sanitize_place_page_description(place.description)
    place.search_result_description = sanitize_place_page_description(place.search_result_description)
    place.website = as_http_url(place.website)
    place.reservation_links = coerce_place_reservation_links(place.reservation_links)
    place.phone = sanitize_place_page_phone(place.phone)
    place.plus_code = sanitize_place_page_plus_code(place.plus_code)
    place.main_photo_url = sanitize_place_photo_url(place.main_photo_url)
    place.photo_url = sanitize_place_photo_url(place.photo_url)
    return place


def canonicalize_enrichment_cache_entry(entry: EnrichmentCacheEntry | None) -> EnrichmentCacheEntry | None:
    if entry is None:
        return None
    canonicalize_enrichment_place(entry.place)
    return entry


def canonicalized_enrichment_cache_entry_copy(entry: EnrichmentCacheEntry | None) -> EnrichmentCacheEntry | None:
    if entry is None:
        return None
    return entry.model_copy(
        update={
            "place": canonicalize_enrichment_place(entry.place.model_copy()) if entry.place is not None else None
        }
    )


def enrichment_cache_entry_payload(entry: EnrichmentCacheEntry) -> dict[str, Any]:
    canonicalized_entry = canonicalized_enrichment_cache_entry_copy(entry)
    assert canonicalized_entry is not None
    payload = canonicalized_entry.model_dump(mode="json", exclude_none=True)
    if not payload.get("merged_sources"):
        payload.pop("merged_sources", None)
    return payload


def sanitize_place_photo_url(value: str | None) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None

    url_parts = urlsplit(normalized)
    host = (url_parts.hostname or "").lower()
    path = url_parts.path.lower()
    query = url_parts.query.lower()
    if "staticmap" in path:
        return None
    if host.endswith("gstatic.com") and (
        host.startswith("encrypted-tbn")
        or path.startswith(("/faviconv2", "/ads-travel/"))
        or "tbn:" in query
    ):
        return None
    if (
        (host.endswith("googleusercontent.com") or host.endswith("ggpht.com"))
        and path.startswith(("/a-", "/a/"))
    ):
        return None

    dimensions = extract_place_photo_source_dimensions(normalized)
    if (
        dimensions is not None
        and (
            dimensions[0] < MIN_PLACE_PHOTO_SOURCE_DIMENSION
            or dimensions[1] < MIN_PLACE_PHOTO_SOURCE_DIMENSION
        )
    ):
        return None

    return normalized


def extract_place_photo_source_dimensions(value: str) -> tuple[int, int] | None:
    match = PLACE_PHOTO_SOURCE_DIMENSION_RE.search(value)
    if match is None:
        return None
    return int(match.group("width")), int(match.group("height"))


def normalized_enrichment_type_ids(
    primary_type: str | None,
    primary_type_display_name: str | None,
    raw_types: list[str] | None,
) -> tuple[str | None, list[str]]:
    specific_tags: list[str] = []
    raw_type_ids = normalize_enrichment_type_ids_with_generic_fallback(raw_types or [])
    normalized_primary_type = normalize_enrichment_type_tag(primary_type)
    if normalized_primary_type:
        append_unique_tag(specific_tags, normalized_primary_type)
    for raw_value in raw_types or []:
        normalized_type_tag = normalize_enrichment_type_tag(raw_value)
        if normalized_type_tag:
            append_unique_tag(specific_tags, normalized_type_tag)
    for inferred_tag in infer_display_name_type_tags(primary_type_display_name):
        append_unique_tag(specific_tags, inferred_tag)

    if not specific_tags:
        fallback_primary = normalize_enrichment_type_id_with_generic_fallback(primary_type)
        fallback_types = raw_type_ids[:]
        if fallback_primary and fallback_primary not in fallback_types:
            fallback_types.insert(0, fallback_primary)
        return fallback_primary, fallback_types

    expanded_type_tags: list[str] = []
    for tag in specific_tags:
        for parent_tag in expanded_parent_type_tags(tag):
            append_unique_tag(expanded_type_tags, parent_tag)
        append_unique_tag(expanded_type_tags, tag)

    return (
        specific_tags[0].replace("-", "_"),
        [tag.replace("-", "_") for tag in expanded_type_tags],
    )


def normalize_enrichment_place_type_fields(place: EnrichmentPlace) -> tuple[str | None, list[str]]:
    return normalized_enrichment_type_ids(
        place.primary_type,
        place.primary_type_display_name,
        place.types,
    )


def normalize_enrichment_type_id_with_generic_fallback(value: str | None) -> str | None:
    if not value:
        return None
    normalized = slugify(value.replace("_", "-"))
    if (
        not normalized
        or normalized in GENERIC_ENRICHMENT_TYPE_TAGS
        or any(pattern.match(normalized) for pattern in INVALID_ENRICHMENT_TYPE_TAG_PATTERNS)
    ):
        return None
    return normalized.replace("-", "_")


def normalize_enrichment_type_ids_with_generic_fallback(values: list[str]) -> list[str]:
    normalized_ids: list[str] = []
    for value in values:
        normalized = normalize_enrichment_type_id_with_generic_fallback(value)
        if normalized and normalized not in normalized_ids:
            normalized_ids.append(normalized)
    return normalized_ids


def append_unique_tag(tags: list[str], candidate: str) -> None:
    if candidate and candidate not in tags:
        tags.append(candidate)


def expanded_parent_type_tags(tag: str) -> list[str]:
    parents: list[str] = []
    for parent_tag, phrases in PARENT_TYPE_TAG_RULES:
        if any(slug_phrase_matches(tag, phrase) for phrase in phrases):
            append_unique_tag(parents, parent_tag)
    if tag.endswith("-restaurant"):
        append_unique_tag(parents, "restaurant")
    for parent_tag, phrases in INFERRED_PARENT_TYPE_TAG_RULES:
        if any(slug_phrase_matches(tag, phrase) for phrase in phrases):
            if parent_tag.endswith("-restaurant"):
                append_unique_tag(parents, "restaurant")
            append_unique_tag(parents, parent_tag)
    return parents


def infer_display_name_type_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return list(_infer_display_name_type_tags_cached(value))


@lru_cache(maxsize=None)
def _infer_display_name_type_tags_cached(value: str) -> tuple[str, ...]:
    tags: list[str] = []
    for tag, aliases in DISPLAY_NAME_TYPE_TAG_RULES:
        if any(type_alias_matches(value, alias) for alias in aliases):
            append_unique_tag(tags, tag)
    return tuple(tags)


@lru_cache(maxsize=None)
def type_alias_matches(label: str, alias: str) -> bool:
    if any(ord(char) > 127 for char in alias):
        return normalize_type_lookup_text(alias) in normalize_type_lookup_text(label)
    return slug_phrase_matches(slugify(label.replace("_", "-")), alias)


@lru_cache(maxsize=None)
def normalize_type_lookup_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("_", " ").replace("-", " ").lower()).strip()


def derive_marker_icon(
    place: RawPlace,
    *,
    enrichment: EnrichmentPlace,
    category: str | None,
    note: str | None,
    why_recommended: str | None,
) -> MarkerIcon:
    candidate_slugs = [
        slugify(term.replace("_", "-"))
        for term in [
            category,
            enrichment.primary_type,
            enrichment.primary_type_display_name,
            *enrichment.types,
            *place.types,
        ]
        if term
    ]

    for candidate_slug in candidate_slugs:
        for marker_icon, phrases in MARKER_ICON_RULES:
            if any(slug_phrase_matches(candidate_slug, phrase) for phrase in phrases):
                return marker_icon

    lookup_text = normalize_vibe_lookup_text(
        " ".join(
            filter(
                None,
                [
                    place.name,
                    place.note,
                    note,
                    why_recommended,
                    category,
                ],
            )
        )
    )
    for marker_icon, keywords in MARKER_ICON_TEXT_RULES:
        if any(vibe_keyword_matches(lookup_text, keyword) for keyword in keywords):
            return marker_icon

    return "default"


def derive_vibe_tags(
    place: RawPlace,
    *,
    enrichment: EnrichmentPlace,
    category: str | None,
    tags: list[str],
    note: str | None,
    why_recommended: str | None,
    top_pick: bool,
) -> list[str]:
    vibes: set[str] = set()
    category_terms = [
        category,
        enrichment.primary_type,
        enrichment.primary_type_display_name,
        *enrichment.types,
        *place.types,
        *tags,
    ]
    category_slugs = {slugify(term.replace("_", "-")) for term in category_terms if term}

    for category_slug in category_slugs:
        if category_slug in VIBE_CATEGORY_RULES:
            vibes.update(VIBE_CATEGORY_RULES[category_slug])

    lookup_text = normalize_vibe_lookup_text(
        " ".join(
            filter(
                None,
                [
                    place.name,
                    place.address,
                    place.note,
                    note,
                    why_recommended,
                    category,
                    enrichment.primary_type,
                    enrichment.primary_type_display_name,
                    " ".join(enrichment.types),
                    " ".join(tags),
                ],
            )
        )
    )
    for vibe_tag, keywords in VIBE_TAG_KEYWORDS.items():
        if any(vibe_keyword_matches(lookup_text, keyword) for keyword in keywords):
            vibes.add(vibe_tag)

    if top_pick:
        vibes.add("local-favorite")

    return sorted(vibes)


@lru_cache(maxsize=None)
def normalize_vibe_lookup_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("_", " ").replace("-", " ").lower()).strip()


@lru_cache(maxsize=None)
def slug_phrase_matches(candidate_slug: str, phrase: str) -> bool:
    normalized_phrase = slugify(phrase.replace("_", "-"))
    if not candidate_slug or not normalized_phrase:
        return False
    return (
        candidate_slug == normalized_phrase
        or candidate_slug.startswith(f"{normalized_phrase}-")
        or candidate_slug.endswith(f"-{normalized_phrase}")
        or f"-{normalized_phrase}-" in candidate_slug
    )


def vibe_keyword_matches(lookup_text: str, keyword: str) -> bool:
    pattern = vibe_keyword_pattern(keyword)
    if pattern is None:
        return False
    return pattern.search(lookup_text) is not None


@lru_cache(maxsize=None)
def vibe_keyword_pattern(keyword: str) -> re.Pattern[str] | None:
    normalized_keyword = normalize_vibe_lookup_text(keyword)
    if not normalized_keyword:
        return None
    pattern = r"(?<![a-z0-9])" + re.escape(normalized_keyword).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
    return re.compile(pattern)


def build_search_index(guides: list[Guide]) -> dict[str, Any]:
    generated_at = max(
        (guide.generated_at for guide in guides if as_string(guide.generated_at) is not None),
        default=STABLE_GENERATED_AT_FALLBACK,
    )
    return {
        "version": 1,
        "generated_at": generated_at,
        "guides": [search_index_guide_entry(guide) for guide in guides],
        "entries": [
            search_index_place_entry(guide, place)
            for guide in guides
            for place in guide.places
            if place_is_visible_in_ui(place)
        ],
    }


def search_index_guide_entry(guide: Guide) -> dict[str, Any]:
    featured_ids = set(guide.featured_place_ids)
    return {
        "slug": guide.slug,
        "title": guide.title,
        "description": guide.description,
        "city": guide.city_name,
        "country": guide.country_name,
        "country_code": guide.country_code,
        "tags": guide.list_tags,
        "place_count": guide.place_count,
        "top_categories": guide.top_categories,
        "featured_names": [
            place.name
            for place in guide.places
            if place.id in featured_ids and place_is_visible_in_ui(place)
        ][:3],
        "url": f"/guides/{guide.slug}/",
        "search_text": compact_search_text(
            [
                guide.title,
                guide.description,
                guide.city_name,
                guide.country_name,
                guide.country_code,
                " ".join(guide.list_tags),
                " ".join(guide.top_categories),
            ]
        ),
    }


def search_index_place_entry(guide: Guide, place: NormalizedPlace) -> dict[str, Any]:
    trust_signal_labels = [
        " ".join(
            part
            for part in [
                signal.display_label or signal.label,
                signal.display_tier or signal.tier,
                signal.title,
            ]
            if part
        )
        for signal in place.trust_signals
        if signal.confidence != "low"
    ]
    return {
        "id": place.id,
        "guide_slug": guide.slug,
        "guide_title": guide.title,
        "city": guide.city_name,
        "country": guide.country_name,
        "country_code": guide.country_code,
        "name": place.name,
        "category": place.primary_category,
        "neighborhood": place.neighborhood,
        "tags": place.tags,
        "vibe_tags": place.vibe_tags,
        "note": place.note,
        "why_recommended": place.why_recommended,
        "rating": place.rating,
        "user_rating_count": place.user_rating_count,
        "top_pick": place.top_pick,
        "manual_rank": place.manual_rank,
        "trust_signals": [
            {
                "label": signal.label,
                "tier": signal.tier,
                "display_label": signal.display_label,
                "display_tier": signal.display_tier,
                "award_year": signal.award_year,
                "is_current": signal.is_current,
                "source": signal.source,
            }
            for signal in place.trust_signals
            if signal.confidence != "low"
        ],
        "maps_url": place.maps_url,
        "url": f"/guides/{guide.slug}/?place={quote_query_value(place.id)}",
        "search_text": compact_search_text(
            [
                place.name,
                place.address,
                place.note,
                place.why_recommended,
                place.primary_category,
                place.neighborhood,
                " ".join(place.tags),
                " ".join(place.vibe_tags),
                " ".join(trust_signal_labels),
                guide.title,
                guide.city_name,
                guide.country_name,
                guide.country_code,
            ]
        ),
    }


def compact_search_text(parts: list[str | None]) -> str:
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part).lower()).strip()


def quote_query_value(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def infer_city_name(title: str) -> str | None:
    parts = split_title_parts(title)
    if not parts:
        return None
    return parts[0]


def guide_location_context(slug: str, raw: RawSavedList) -> tuple[str | None, str | None]:
    list_override = read_json(LIST_OVERRIDES_DIR / f"{slug}.json")
    title = as_string(list_override.get("title")) or raw.title or slug.replace("-", " ").title()
    city_name = as_string(list_override.get("city_name")) or infer_city_name(title)
    country_name = as_string(list_override.get("country_name")) or infer_country_name(title, raw)
    return city_name, country_name


def infer_country_name(title: str, raw: RawSavedList) -> str | None:
    parts = split_title_parts(title)
    if len(parts) > 1:
        return parts[-1]
    if len(parts) == 1 and known_country_name(parts[0]):
        return parts[0]
    flag_country_name = infer_country_from_flag(title) or infer_country_from_flag(raw.description)
    if flag_country_name:
        return flag_country_name

    place_countries = [infer_country_from_address(place.address) for place in raw.places]
    place_countries = [country for country in place_countries if country]
    if not place_countries:
        return None
    return Counter(place_countries).most_common(1)[0][0]


def infer_country_code(country_name: str | None) -> str | None:
    if country_name is None:
        return None
    mapping = {
        "Japan": "JP",
        "South Korea": "KR",
        "Korea": "KR",
        "Taiwan": "TW",
        "United States": "US",
    }
    mapped_code = mapping.get(country_name)
    if mapped_code:
        return mapped_code
    try:
        country = pycountry.countries.lookup(country_name)
    except LookupError:
        return None
    return country.alpha_2


def infer_country_from_address(address: str | None) -> str | None:
    if address is None:
        return None
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if not parts:
        return None

    for part in reversed(parts):
        country_name = known_country_name(normalize_country_candidate(part))
        if country_name:
            return country_name

    return None


def normalize_country_candidate(value: str) -> str:
    candidate = re.sub(r"[^\w\s-]", " ", value).strip()
    candidate = re.sub(r"\b\d{3,6}(?:[-−ー－]\d{4})?\b", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" -−ー－")
    return candidate


def known_country_name(candidate: str | None) -> str | None:
    if candidate is None:
        return None
    candidate = candidate.strip()
    if not candidate:
        return None

    aliases = {
        "england": "England",
        "scotland": "Scotland",
        "wales": "Wales",
        "northern ireland": "Northern Ireland",
        "uk": "UK",
        "uae": "UAE",
        "usa": "USA",
        "korea": "Korea",
        "taiwan": "Taiwan",
        "vatican city": "Vatican City",
        "ivory coast": "Ivory Coast",
    }
    alias = aliases.get(normalize_locality_key(candidate))
    if alias:
        return alias

    try:
        pycountry.countries.lookup(candidate)
    except LookupError:
        return None
    return candidate


def infer_country_from_flag(text: str | None) -> str | None:
    if text is None:
        return None

    flag_country_names = {
        "AE": "United Arab Emirates",
        "GB": "United Kingdom",
        "KR": "South Korea",
        "TW": "Taiwan",
        "US": "United States",
    }
    regional_indicator_start = 0x1F1E6
    regional_indicator_end = 0x1F1FF
    regional_indicators = [
        char for char in text if regional_indicator_start <= ord(char) <= regional_indicator_end
    ]
    for index in range(len(regional_indicators) - 1):
        first = ord(regional_indicators[index]) - regional_indicator_start
        second = ord(regional_indicators[index + 1]) - regional_indicator_start
        country_code = f"{chr(ord('A') + first)}{chr(ord('A') + second)}"
        mapped_name = flag_country_names.get(country_code)
        if mapped_name:
            return mapped_name
        country = pycountry.countries.get(alpha_2=country_code)
        if country:
            return country.name
    return None


def infer_neighborhood(address: str | None, *, city_name: str | None = None) -> str | None:
    localities = infer_address_localities(address, city_name=city_name)
    if localities:
        return localities[0]
    return None


def merge_inferred_localities(*locality_lists: list[str]) -> list[str]:
    merged: list[str] = []
    for locality_list in locality_lists:
        for locality in locality_list:
            append_unique_locality(merged, locality)
    return merged


def infer_address_parts_localities(
    address_parts: AddressParts | None,
    *,
    city_name: str | None = None,
) -> list[str]:
    if not address_parts:
        return []

    candidates: list[str] = []
    for index in (1, 2, 3, 5):
        if index >= len(address_parts):
            continue
        value = address_parts[index]
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    if len(address_parts) >= 8 and isinstance(address_parts[7], list):
        candidates.extend(value.strip() for value in address_parts[7] if isinstance(value, str) and value.strip())

    localities: list[str] = []
    for candidate in candidates:
        localities = merge_inferred_localities(
            localities,
            infer_address_localities(candidate, city_name=city_name),
        )
        normalized_candidate = normalize_address_locality_part(candidate)
        if normalized_candidate is None:
            continue
        candidate_key = normalize_locality_key(normalized_candidate)
        city_key = normalize_locality_key(city_name)
        city_equivalence_keys = normalize_locality_scope_equivalence_keys(city_name)
        candidate_equivalence_key = normalize_locality_equivalence_key(normalized_candidate)
        if (
            not candidate_key
            or candidate_key == city_key
            or candidate_equivalence_key in city_equivalence_keys
            or is_subnational_locality(normalized_candidate)
        ):
            continue
        append_unique_locality(localities, normalized_candidate)

    return localities


def infer_address_localities(address: str | None, *, city_name: str | None = None) -> list[str]:
    if address is None:
        return []
    return list(_infer_address_localities_cached(address, city_name))


@lru_cache(maxsize=None)
def _infer_address_localities_cached(address: str, city_name: str | None) -> tuple[str, ...]:
    city_key = normalize_locality_key(city_name)
    city_equivalence_keys = normalize_locality_scope_equivalence_keys(city_name)
    neighborhoods: list[str] = []
    subcities: list[str] = []

    for raw_part in re.split(r"[,、]", address):
        candidate = normalize_address_locality_part(raw_part)
        if candidate is None:
            continue
        key = normalize_locality_key(candidate)
        equivalent_key = normalize_locality_equivalence_key(candidate)
        if not key or key == city_key or equivalent_key in city_equivalence_keys:
            continue
        if is_subnational_locality(candidate):
            continue
        if is_subcity_locality(candidate):
            append_unique_locality(subcities, candidate)
        else:
            append_unique_locality(neighborhoods, candidate)

    return tuple([*neighborhoods, *subcities])


@lru_cache(maxsize=None)
def normalize_address_locality_part(
    part: str,
    *,
    allow_explicit_subnational_label: bool = False,
    strip_leading_preposition: bool = True,
) -> str | None:
    candidate = part.strip()
    if not candidate:
        return None

    candidate = re.sub(r"^(?:japan|日本)\s*", "", candidate, flags=re.IGNORECASE).strip()
    candidate = re.sub(r"〒\s*\d{3}[-−ー－]?\d{4}\s*", "", candidate).strip()
    candidate = re.sub(r"\b\d{3}[-−ー－]\d{4}\b", "", candidate).strip()
    candidate = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", candidate).strip()
    candidate = re.sub(r"\s+\d{4}\b", "", candidate).strip()
    candidate = candidate.strip(" -−ー－/()[]{}.")
    candidate = strip_leading_numeric_locality_prefix(candidate)
    if strip_leading_preposition:
        candidate = strip_leading_locality_preposition(candidate)
    candidate = strip_country_locality_prefix(candidate)
    candidate = strip_trailing_short_region_code(candidate)
    candidate = strip_subnational_locality_suffix(candidate)
    candidate = strip_leading_localized_locality_prefix(candidate)

    if not candidate or is_country_locality(candidate):
        return None
    if is_subnational_locality(candidate):
        return None
    if is_subnational_locality_abbreviation(candidate):
        return None
    if is_explicit_subnational_locality_label(candidate) and not allow_explicit_subnational_label:
        return None

    if is_building_or_unit_part(candidate):
        return None

    trailing_locality = extract_trailing_locality(candidate)
    if trailing_locality is not None:
        candidate = trailing_locality

    if is_street_or_block_part(candidate) or is_building_or_unit_part(candidate):
        return None

    candidate = re.sub(r"\s+", " ", candidate).strip(" -−ー－/()[]{}.")
    if not candidate:
        return None
    if re.search(r"\d", candidate):
        return None
    if len(candidate) <= 1:
        return None
    return candidate


def strip_country_locality_prefix(candidate: str) -> str:
    words = candidate.split()
    max_prefix_words = min(len(words) - 1, 4)
    for word_count in range(max_prefix_words, 0, -1):
        prefix = " ".join(words[:word_count])
        if is_country_locality(prefix):
            return " ".join(words[word_count:]).strip(" -−ー－/()[]{}.")
    return candidate


def strip_leading_numeric_locality_prefix(candidate: str) -> str:
    return re.sub(
        r"^\d+[A-Za-z]?(?:[-−ー－/]\d+[A-Za-z]?)*\s+(?!chome\b|丁目)",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip()


def strip_leading_locality_preposition(candidate: str) -> str:
    return re.sub(r"^(?:at|near)\s+", "", candidate, flags=re.IGNORECASE).strip()


def strip_trailing_short_region_code(candidate: str) -> str:
    return re.sub(r"\s+[A-Z]{2}$", "", candidate).strip(" -−ー－/()[]{}.")


def strip_leading_localized_locality_prefix(candidate: str) -> str:
    return re.sub(
        r"^[\u3040-\u30ff\u3400-\u9fff][\u3040-\u30ff\u3400-\u9fff\s]*\s+(?=[A-Za-z])",
        "",
        candidate,
    ).strip()


def strip_subnational_locality_suffix(candidate: str) -> str:
    return re.sub(
        r"\s+(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\b$",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip(" -−ー－/()[]{}.")


def extract_trailing_locality(candidate: str) -> str | None:
    patterns = [
        r"^\d+\s*-?\s*chome(?:[-−ー－]\d+)*\s+([A-Za-z][A-Za-z' -]+)$",
        r"^\d+\s*丁目(?:[-−ー－]\d+)*\s+([A-Za-z][A-Za-z' -]+)$",
        r"^(?:\d+[Ff]?\s*,?\s*)?(?:\d+[-−ー－/])+\d+\s+([A-Za-z][A-Za-z' -]+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, candidate, flags=re.IGNORECASE)
        if match:
            locality = match.group(1).strip(" -−ー－/()[]{}.")
            if locality and not is_building_or_unit_part(locality):
                return locality
    return None


def is_country_locality(candidate: str) -> bool:
    return normalize_locality_key(candidate) in get_country_locality_keys()


def is_subnational_locality_abbreviation(candidate: str) -> bool:
    compact = re.sub(r"[^A-Za-z]", "", candidate).upper()
    return compact in SUBNATIONAL_LOCALITY_ABBREVIATIONS


def is_explicit_subnational_locality_label(candidate: str) -> bool:
    candidate_key = normalize_locality_key(candidate)
    if candidate_key in {"county line", "state college"}:
        return False
    return bool(
        re.match(
            r"^(?:state|county|province|prefecture|region)\s+of\s+\S",
            candidate,
            flags=re.IGNORECASE,
        )
        or re.match(
            r"^(?:county|province|prefecture|region)\s+\S",
            candidate,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:county|province|prefecture|state|region)$",
            candidate,
            flags=re.IGNORECASE,
        )
    )


def get_country_locality_keys() -> set[str]:
    global COUNTRY_LOCALITY_KEYS
    if COUNTRY_LOCALITY_KEYS is not None:
        return COUNTRY_LOCALITY_KEYS

    country_names = set(COUNTRY_LOCALITY_ALIASES)
    for country in pycountry.countries:
        for attribute in ("name", "official_name", "common_name"):
            value = getattr(country, attribute, None)
            if value:
                country_names.add(value)

    COUNTRY_LOCALITY_KEYS = {normalize_locality_key(name) for name in country_names}
    return COUNTRY_LOCALITY_KEYS


def is_subnational_locality(candidate: str) -> bool:
    return normalize_locality_key(candidate) in get_subnational_locality_keys()


def is_subnational_code_locality(candidate: str) -> bool:
    return candidate.strip().casefold() in get_subnational_locality_code_keys()


def get_subnational_locality_keys() -> set[str]:
    global SUBNATIONAL_LOCALITY_KEYS
    if SUBNATIONAL_LOCALITY_KEYS is not None:
        return SUBNATIONAL_LOCALITY_KEYS

    subdivision_names: set[str] = set()
    for subdivision in pycountry.subdivisions:
        value = getattr(subdivision, "name", None)
        if value:
            subdivision_names.add(value)
    subdivision_names.update(SUBNATIONAL_LOCALITY_ALIASES)

    SUBNATIONAL_LOCALITY_KEYS = {
        normalize_locality_key(name)
        for name in subdivision_names
        if normalize_locality_key(name)
    }
    return SUBNATIONAL_LOCALITY_KEYS


def get_subnational_locality_code_keys() -> set[str]:
    global SUBNATIONAL_LOCALITY_CODE_KEYS
    if SUBNATIONAL_LOCALITY_CODE_KEYS is not None:
        return SUBNATIONAL_LOCALITY_CODE_KEYS

    code_keys: set[str] = set()
    for subdivision in pycountry.subdivisions:
        code = getattr(subdivision, "code", None)
        if not code or "-" not in code:
            continue
        suffix = code.rsplit("-", 1)[-1]
        if 2 <= len(suffix) <= 3 and suffix.isalpha():
            code_keys.add(suffix.casefold())
    code_keys.update(code.casefold() for code in AUSTRALIAN_SUBNATIONAL_LOCALITY_ALIASES)

    SUBNATIONAL_LOCALITY_CODE_KEYS = code_keys
    return SUBNATIONAL_LOCALITY_CODE_KEYS


def is_subcity_locality(candidate: str) -> bool:
    return bool(
        re.search(
            r"\b(?:city|ward|district|borough|county|prefecture|province|gu|ku)\b",
            candidate,
            flags=re.IGNORECASE,
        )
    )


def is_building_or_unit_part(candidate: str) -> bool:
    return bool(
        re.search(
            (
                r"\b(?:bldg|building|tower|plaza|terrace|floor|mall|hotel|mansion|palace|garden|stream|works|"
                r"center|centre|place|v-city|gratteciel|gems|bis|sn)\b"
                r"|^(?:s/n|sn)$"
                r"|^(?:no|no\.)$"
                r"|^dentro\s+del\b"
                r"|ビル|階|号|館"
            ),
            candidate,
            flags=re.IGNORECASE,
        )
    )


def is_street_or_block_part(candidate: str) -> bool:
    return bool(
        re.search(
            (
                r"\b(?:chome|丁目|st|str|street|rd|road|rte|route|ct|court|ln|lane|ave|avenue|dr|drive|blvd|boulevard|"
                r"prom|promenade|rue|via|carrer|calle|avinguda|avenida|av|rambla|ronda|rda|passeig|paseo|pg|bd|quai|piazza|"
                r"placa|plaça|pl|bajada|jet[eé]e|palais|"
                r"passatge|travessera|moll|jalan|soi|gil|ro|daero)\b"
                r"|^(?:c/|c\.|v\.|pg\.|av\.|bd\.|rda\.|pl\.|p\.za|p\.\s*º)\s"
            ),
            candidate,
            flags=re.IGNORECASE,
        )
    )


def append_unique_locality(localities: list[str], candidate: str) -> None:
    key = normalize_locality_key(candidate)
    if key and all(normalize_locality_key(existing) != key for existing in localities):
        localities.append(candidate)


def normalize_locality_key(value: str | None) -> str:
    if value is None:
        return ""
    cleaned = re.sub(r"[^\w\s-]", " ", value.strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_locality_equivalence_key(value: str | None) -> str:
    key = normalize_locality_key(value)
    if not key:
        return ""
    key = re.sub(
        r"\s+(?:main\s+island|islands|island|city|ward|district|borough|county|prefecture|province|state|region|gu|ku)$",
        "",
        key,
    ).strip()
    return LOCALITY_EQUIVALENCE_ALIASES.get(key, key)


def normalize_locality_scope_equivalence_keys(value: str | None) -> set[str]:
    normalized = as_string(value)
    if normalized is None:
        return set()
    keys = {key for key in [normalize_locality_equivalence_key(normalized)] if key}
    title_without_parentheticals = re.sub(r"\([^)]*\)", " ", normalized)
    for part in re.split(r"\s+(?:and|&)\s+|/", title_without_parentheticals, flags=re.IGNORECASE):
        key = normalize_locality_equivalence_key(part)
        if key:
            keys.add(key)
    return keys


def split_title_parts(title: str) -> list[str]:
    title_without_parentheticals = re.sub(r"\([^)]*\)", " ", title)
    cleaned = re.sub(r"[^\w\s,&/-]", "", title_without_parentheticals)
    return [part.strip() for part in cleaned.split(",") if part.strip()]


def extract_hashtags(text: str | None) -> list[str]:
    if text is None:
        return []
    normalized_tags = {
        normalize_list_tag(match.group(1))
        for match in re.finditer(r"#([^\s#]+)", text)
    }
    return sorted(tag for tag in normalized_tags if tag and tag != "item")


def normalize_list_tag(value: str) -> str:
    return normalize_tag_slug(value)


def normalize_tag_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    slug = slugify(ascii_value)
    return BROKEN_TAG_NORMALIZATION_MAP.get(slug, slug)


@lru_cache(maxsize=None)
def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def load_site_build_hooks_module(path: Path) -> Any | None:
    try:
        resolved_path = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    return _load_site_build_hooks_module(resolved_path, resolved_path.stat().st_mtime_ns)


@lru_cache(maxsize=None)
def _load_site_build_hooks_module(path: Path, mtime_ns: int) -> Any:
    module_identity = f"{path}:{mtime_ns}"
    module_name = f"favorite_places_site_build_hooks_{hashlib.sha1(module_identity.encode('utf-8')).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load site build hooks from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def transform_guide_description_with_site_hook(
    description: str | None,
    *,
    slug: str,
    raw: RawSavedList,
    list_override: dict[str, Any],
) -> str | None:
    normalized_description = normalize_text_blocks(description)
    module = load_site_build_hooks_module(SITE_BUILD_HOOKS_PATH)
    if module is None:
        return normalized_description
    hook = getattr(module, "transform_guide_description", None)
    if hook is None:
        return normalized_description
    if not callable(hook):
        raise TypeError(f"{SITE_BUILD_HOOKS_PATH} transform_guide_description must be callable")
    transformed = hook(
        normalized_description,
        slug=slug,
        raw=raw,
        list_override=list_override,
    )
    if transformed is None:
        return None
    if not isinstance(transformed, str):
        raise TypeError(f"{SITE_BUILD_HOOKS_PATH} transform_guide_description must return str | None")
    return normalize_text_blocks(transformed)


def normalize_text_blocks(value: str | None) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", normalized)
    normalized = normalized.strip()
    return normalized or None


def combine_recommendation_copy(*values: str | None) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_text_blocks(value)
        if normalized is None:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        parts.append(normalized)
    return "\n\n".join(parts) if parts else None


def trust_signal_recommendation_copy(signals: list[TrustSignal]) -> str | None:
    award_entries: list[tuple[str, bool]] = []
    seen_award_labels: set[str] = set()
    current_award_tiers = {
        (signal.source, trust_signal_award_tier(signal))
        for signal in signals
        if signal.confidence != "low"
        and signal.source in {"michelin", "tabelog"}
        and signal.is_current is True
    }
    for signal in signals:
        if signal.confidence == "low":
            continue
        if signal.source not in {"michelin", "tabelog"}:
            continue
        if (
            (signal.source, trust_signal_award_tier(signal)) in current_award_tiers
            and signal.is_current is False
        ):
            continue
        label_parts = [
            signal.display_label or trust_signal_display_label(signal),
            signal.display_tier or trust_signal_display_tier(signal),
        ]
        if signal.award_year:
            label_parts.append(str(signal.award_year))
        label = " ".join(part for part in label_parts if part)
        if label and label not in seen_award_labels:
            seen_award_labels.add(label)
            award_entries.append((label, signal.is_current is False))
        if len(award_entries) >= 2:
            break
    if not award_entries:
        return None
    if len(award_entries) == 1:
        label, is_previous = award_entries[0]
        prefix = "Previously recognized by" if is_previous else "Recognized by"
        return f"{prefix} {label}."
    first_label, first_is_previous = award_entries[0]
    second_label, second_is_previous = award_entries[1]
    if not first_is_previous and second_is_previous:
        return f"Recognized by {first_label} and previously by {second_label}."
    if first_is_previous and not second_is_previous:
        return f"Recognized by {second_label} and previously by {first_label}."
    prefix = "Previously recognized by" if first_is_previous and second_is_previous else "Recognized by"
    return f"{prefix} {first_label} and {second_label}."


def trust_signal_award_tier(signal: TrustSignal) -> str:
    return signal.tier or ""


def usable_enrichment_recommendation_copy(
    value: Any,
    *,
    enrichment_place: EnrichmentPlace,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
) -> str | None:
    description = sanitize_place_page_description(value)
    if description is None:
        return None
    if not enrichment_identity_is_compatible_with_raw(raw_place, enrichment_place):
        return None
    if semantic_description_has_conflicting_location(
        description,
        enrichment_place=enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
        country_name=country_name,
    ):
        return None
    if semantic_description_is_low_information(
        description,
        enrichment_place=enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
        country_name=country_name,
    ):
        return None
    return description


def coerce_list_author(value: Any) -> ListAuthor | None:
    if value is None:
        return None
    if isinstance(value, ListAuthor):
        return value
    if isinstance(value, str):
        normalized = as_string(value)
        return ListAuthor(name=normalized) if normalized else None
    if isinstance(value, dict):
        name = as_string(value.get("name"))
        if not name:
            return None
        return ListAuthor(
            name=name,
            photo_url=as_string(value.get("photo_url")),
            photo_path=as_string(value.get("photo_path")),
            avatar_mode=normalize_author_avatar_mode(value.get("avatar_mode")),
            profile_id=as_string(value.get("profile_id")),
        )
    return None


def normalize_author_avatar_mode(value: Any) -> Literal["photo", "initials", "icon"] | None:
    normalized = as_string(value)
    if normalized in {"photo", "initials", "icon"}:
        return normalized
    return None


def list_author_is_overridden(slug: str) -> bool:
    list_override = read_json(LIST_OVERRIDES_DIR / f"{slug}.json")
    return "author" in list_override or "owner" in list_override


def guide_author_for_ui(raw: RawSavedList, *, list_override: dict[str, Any]) -> ListAuthor | None:
    if "author" in list_override:
        return coerce_list_author(list_override.get("author"))
    if "owner" in list_override:
        return coerce_list_author(list_override.get("owner"))
    return raw.owner


def place_added_by_for_ui(place: RawPlace, *, override: dict[str, Any]) -> ListAuthor | None:
    if "added_by" in override:
        return coerce_list_author(override.get("added_by"))
    return place.added_by


def load_raw_saved_list(path: Path) -> RawSavedList | None:
    if not path.exists():
        return None
    return RawSavedList.model_validate_json(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = normalize_json_payload(payload)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    serialized = f"{text}\n"
    if path.exists() and path.read_text(encoding="utf-8") == serialized:
        return
    tmp_path.write_text(serialized, encoding="utf-8")
    tmp_path.replace(path)


def normalize_json_payload(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        return normalize_json_payload(payload.model_dump(mode="json"))
    if isinstance(payload, dict):
        return {key: normalize_json_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [normalize_json_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return [normalize_json_payload(item) for item in payload]
    return payload


def as_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def as_http_url(value: Any) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    parts = urlsplit(normalized)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return urlunsplit(parts)


def coerce_place_reservation_links(value: Any) -> list[PlaceReservationLink]:
    if not isinstance(value, list):
        return []

    links: list[PlaceReservationLink] = []
    seen_urls: set[str] = set()
    for item in value:
        if isinstance(item, PlaceReservationLink):
            label = as_string(item.label)
            url = as_http_url(item.url)
        elif isinstance(item, dict):
            label = as_string(item.get("label")) or as_string(item.get("provider"))
            url = as_http_url(item.get("url"))
        elif hasattr(item, "label") and hasattr(item, "url"):
            label = as_string(getattr(item, "label", None))
            url = as_http_url(getattr(item, "url", None))
        else:
            continue

        if label is None or url is None or url in seen_urls:
            continue
        links.append(PlaceReservationLink(label=label[:80], url=url))
        seen_urls.add(url)

    if any(not place_reservation_link_is_google_reserve(link) for link in links):
        links = [link for link in links if not place_reservation_link_is_google_reserve(link)]

    return links


def place_reservation_link_is_google_reserve(link: PlaceReservationLink) -> bool:
    parts = urlsplit(link.url)
    host = parts.netloc.lower()
    return host in {"www.google.com", "google.com", "maps.google.com"} and parts.path.startswith("/maps/reserve")


def as_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def google_places_api_key() -> str | None:
    return PlacesSettings().google_places_api_key


def google_places_enrichment_strategy() -> Literal["scrape", "api", "scrape_then_api"]:
    return PlacesSettings().google_places_enrichment_strategy


@lru_cache(maxsize=1)
def site_enrichment_config() -> dict[str, Any]:
    return read_json(SITE_ENRICHMENT_CONFIG_PATH)


def site_google_maps_place_config_value(key: str) -> Any:
    config = site_enrichment_config()
    google_maps_places = config.get("google_maps_places")
    if isinstance(google_maps_places, dict) and key in google_maps_places:
        return google_maps_places[key]
    place_scraping = config.get("place_scraping")
    if isinstance(place_scraping, dict) and key in place_scraping:
        return place_scraping[key]
    return config.get(key)


def google_maps_place_llm_repair_mode() -> Literal["off", "dom", "dom_then_translation"]:
    configured = PlacesSettings().google_maps_place_llm_repair
    if configured is not None:
        return configured
    site_value = as_string(site_google_maps_place_config_value("llm_repair"))
    if site_value is not None:
        if site_value not in {"off", "dom", "dom_then_translation"}:
            raise RuntimeError(
                f"{SITE_ENRICHMENT_CONFIG_PATH} google_maps_places.llm_repair must be "
                "`off`, `dom`, or `dom_then_translation`."
            )
        return site_value
    return "dom"


def google_maps_place_llm_cache_dir() -> Path:
    configured = PlacesSettings().google_maps_place_llm_cache_dir
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else ROOT / path
    return SCRAPER_STATE_DIR / "llm-cache"


def google_maps_place_collect_reviews() -> bool:
    configured = PlacesSettings().google_maps_place_collect_reviews
    if configured is not None:
        return configured
    site_value = as_bool(site_google_maps_place_config_value("collect_reviews"))
    return bool(site_value)


def google_maps_place_collect_about() -> bool:
    configured = PlacesSettings().google_maps_place_collect_about
    if configured is not None:
        return configured
    site_value = as_bool(site_google_maps_place_config_value("collect_about"))
    return bool(site_value)


def google_maps_place_semantic_llm_enabled() -> bool:
    configured = PlacesSettings().google_maps_place_semantic_llm
    if configured is not None:
        return configured
    site_value = as_bool(site_google_maps_place_config_value("semantic_llm"))
    return bool(site_value)


def google_maps_place_semantic_descriptions_enabled() -> bool:
    configured = PlacesSettings().google_maps_place_semantic_descriptions
    if configured is not None:
        return configured
    site_value = as_bool(site_google_maps_place_config_value("semantic_descriptions"))
    return bool(site_value)


def google_maps_place_semantic_description_force_refresh() -> bool:
    configured = PlacesSettings().google_maps_place_semantic_description_force_refresh
    if configured is not None:
        return configured
    site_value = as_bool(site_google_maps_place_config_value("semantic_description_force_refresh"))
    return bool(site_value)


def google_maps_place_neighborhood_mappings() -> list[dict[str, Any]]:
    configured = site_google_maps_place_config_value("neighborhood_mappings")
    if isinstance(configured, dict):
        return [
            {"from": source, "to": target}
            for source, target in configured.items()
            if isinstance(source, str) and isinstance(target, str)
        ]
    if isinstance(configured, list):
        return [item for item in configured if isinstance(item, dict)]
    return []


def google_maps_place_category_mappings() -> list[dict[str, Any]]:
    configured = site_google_maps_place_config_value("category_mappings")
    if isinstance(configured, dict):
        return [
            {"from": source, "to": target}
            for source, target in configured.items()
            if isinstance(source, str) and isinstance(target, str)
        ]
    if isinstance(configured, list):
        return [item for item in configured if isinstance(item, dict)]
    return []


def google_maps_place_price_display_config() -> dict[str, Any]:
    configured = site_google_maps_place_config_value("price_display")
    return configured if isinstance(configured, dict) else {}


@lru_cache(maxsize=1)
def build_place_llm_repairer() -> Any | None:
    if (
        cached_place_repairer is None
        or llm_cache_namespace_from_env is None
        or openai_compatible_place_repairer_from_env is None
    ):
        return None

    try:
        repairer = openai_compatible_place_repairer_from_env(
            default_config_file=ROOT / "vendor" / "gmaps-scraper" / "llm.json",
            local_config_file=ROOT / "llm.local.json",
        )
        cache_namespace = llm_cache_namespace_from_env(
            default_config_file=ROOT / "vendor" / "gmaps-scraper" / "llm.json",
            local_config_file=ROOT / "llm.local.json",
        )
    except LLMRepairError:
        return None

    return cached_place_repairer(
        repairer,
        cache_dir=google_maps_place_llm_cache_dir(),
        cache_namespace=cache_namespace,
    )


def configured_source_path(source: SourceConfig) -> Path:
    if not source.path:
        raise RuntimeError(f"Configured source {source.slug} is missing a path.")
    path = Path(source.path).expanduser()
    if not path.is_absolute():
        site_relative_path = SITE_DIR / path
        root_relative_path = ROOT / path
        path = root_relative_path if root_relative_path.exists() and not site_relative_path.exists() else site_relative_path
    return path


def fallback_source_title(source: SourceConfig) -> str:
    if source.title:
        return source.title
    if source.path:
        stem = Path(source.path).stem
    elif source.url:
        stem = source.slug
    else:
        stem = "saved-list"
    return stem.replace("-", " ").replace("_", " ").title()


def raw_source_signature(source: SourceConfig) -> str:
    payload = {
        "slug": source.slug,
        "type": source.type,
        "url": source.url,
        "path": source.path,
        "title": source.title,
    }
    if source.type == "google_export_csv":
        path = configured_source_path(source)
        payload["content_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def stamp_raw_saved_list(payload: RawSavedList, source: SourceConfig, *, source_signature: str) -> None:
    fetched_at = datetime.now(UTC)
    payload.fetched_at = fetched_at.isoformat()
    payload.refresh_after = raw_source_refresh_after(
        fetched_at,
        source,
        source_signature=source_signature,
    ).isoformat()
    payload.source_signature = source_signature
    payload.configured_source_type = source.type
    payload.configured_source_url = source.url
    payload.configured_source_path = source.path


def raw_place_match_keys(place: RawPlace, *, source_type: str | None = None) -> list[str]:
    keys: list[str] = []

    cid = as_string(place.cid) or extract_maps_cid(place.maps_url)
    if cid:
        keys.append(f"cid:{cid}")

    google_id = as_string(place.google_id)
    if google_id:
        keys.append(f"gid:{google_id.strip('/').replace('/', '-')}")

    maps_place_token = place.maps_place_token or extract_maps_place_token(place.maps_url)
    if maps_place_token:
        keys.append(f"gms:{maps_place_token}")

    name = as_string(place.name)
    lat = as_float(place.lat)
    lng = as_float(place.lng)
    if name and lat is not None and lng is not None:
        keys.append(f"name-ll:{slugify(name)}:{lat:.6f}:{lng:.6f}")

    address = as_string(place.address)
    if name and address:
        keys.append(f"name-address:{slugify(name)}:{slugify(address)}")

    if source_type == "google_export_csv":
        maps_url_id = short_maps_url_id(place.maps_url)
        if maps_url_id:
            keys.append(maps_url_id)

    keys.append(stable_place_id(place, source_type=source_type))
    return list(dict.fromkeys(keys))


def build_raw_place_preservation_index(
    raw: RawSavedList,
    *,
    source_type: str | None = None,
) -> dict[str, RawPlace]:
    index: dict[str, RawPlace] = {}
    for place in raw.places:
        for key in raw_place_match_keys(place, source_type=source_type):
            index.setdefault(key, place)
    return index


def preserve_existing_raw_place(
    *,
    existing_place: RawPlace,
    refreshed_place: RawPlace,
) -> tuple[RawPlace, list[str]]:
    preserved_fields: list[str] = []
    updates: dict[str, Any] = {}
    existing_name = as_string(existing_place.name)
    refreshed_name = as_string(refreshed_place.name)
    names_compatible = raw_place_names_are_compatible(existing_name, refreshed_name)

    if (
        names_compatible
        and not refreshed_place.address
        and raw_place_address_is_preservable(existing_place, refreshed_place)
    ):
        updates["address"] = existing_place.address
        preserved_fields.append("address")
    if names_compatible and not refreshed_place.google_id and existing_place.google_id:
        updates["google_id"] = existing_place.google_id
        preserved_fields.append("google_id")
    if names_compatible and not refreshed_place.cid and existing_place.cid:
        updates["cid"] = existing_place.cid
        preserved_fields.append("cid")
    if names_compatible and not refreshed_place.maps_place_token and existing_place.maps_place_token:
        updates["maps_place_token"] = existing_place.maps_place_token
        preserved_fields.append("maps_place_token")
    if names_compatible and not refreshed_place.is_favorite and existing_place.is_favorite:
        updates["is_favorite"] = True
        preserved_fields.append("is_favorite")
    if names_compatible and refreshed_place.added_by is None and existing_place.added_by is not None:
        updates["added_by"] = existing_place.added_by
        preserved_fields.append("added_by")

    if not updates:
        return refreshed_place, preserved_fields

    return refreshed_place.model_copy(update=updates), preserved_fields


def raw_place_names_are_compatible(existing_name: str | None, refreshed_name: str | None) -> bool:
    if not existing_name or not refreshed_name:
        return True

    existing_slug = slugify(existing_name)
    refreshed_slug = slugify(refreshed_name)
    if existing_slug == refreshed_slug:
        return True

    if existing_slug in refreshed_slug or refreshed_slug in existing_slug:
        shorter = min(len(existing_slug), len(refreshed_slug))
        return shorter >= 8

    return False


def raw_place_address_is_preservable(existing_place: RawPlace, refreshed_place: RawPlace) -> bool:
    address = as_string(existing_place.address)
    if not address:
        return False

    note = as_string(existing_place.note)
    if note and normalize_text(note) == normalize_text(address):
        return False

    existing_name = as_string(existing_place.name)
    refreshed_name = as_string(refreshed_place.name)
    normalized_address = normalize_text(address)
    if normalized_address is None:
        return False
    if existing_name and normalize_text(existing_name) == normalized_address:
        return False
    if refreshed_name and normalize_text(refreshed_name) == normalized_address:
        return False

    # Preserve only address-like strings, not generic locality labels or notes.
    return (
        bool(re.search(r"\d", address))
        or "," in address
        or "、" in address
        or bool(re.search(r"\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|place|pl|court|ct|suite|ste|unit|floor|fl)\b", address, re.IGNORECASE))
        or "〒" in address
        or "號" in address
    )


def preserve_existing_raw_saved_list(
    *,
    source: SourceConfig | None = None,
    slug: str,
    existing_payload: RawSavedList | None,
    refreshed_payload: RawSavedList,
) -> RawSavedList:
    if existing_payload is None:
        return refreshed_payload
    if source is not None:
        if not raw_source_signature_matches(source, existing_payload.source_signature):
            return refreshed_payload
    elif (
        existing_payload.source_signature
        and refreshed_payload.source_signature
        and existing_payload.source_signature != refreshed_payload.source_signature
    ):
        return refreshed_payload

    source_type = refreshed_payload.configured_source_type or existing_payload.configured_source_type
    existing_index = build_raw_place_preservation_index(existing_payload, source_type=source_type)
    updated_places: list[RawPlace] = []

    for refreshed_place in refreshed_payload.places:
        existing_place = None
        for key in raw_place_match_keys(refreshed_place, source_type=source_type):
            existing_place = existing_index.get(key)
            if existing_place is not None:
                break

        if existing_place is None:
            updated_places.append(refreshed_place)
            continue

        merged_place, preserved_fields = preserve_existing_raw_place(
            existing_place=existing_place,
            refreshed_place=refreshed_place,
        )
        if preserved_fields:
            place_id = stable_place_id(merged_place, source_type=source_type)
            print(
                f"WARNING: Preserving previous raw fields for {slug}:{place_id} "
                f"[{merged_place.name}]: {', '.join(preserved_fields)}.",
                flush=True,
            )
        updated_places.append(merged_place)

    return refreshed_payload.model_copy(update={"places": updated_places})


def raw_source_refresh_after(fetched_at: datetime, source: SourceConfig, *, source_signature: str) -> datetime:
    if source.type == "google_export_csv":
        return fetched_at + RAW_SOURCE_CACHE_TTL
    return fetched_at + RAW_SOURCE_CACHE_TTL + stable_timedelta_jitter(
        source_signature,
        max_jitter=RAW_SOURCE_REFRESH_JITTER,
    )


def stable_timedelta_jitter(key: str, *, max_jitter: timedelta) -> timedelta:
    max_seconds = int(max_jitter.total_seconds())
    if max_seconds <= 0:
        return timedelta()

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    offset_seconds = int(digest[:12], 16) % (max_seconds * 2 + 1)
    return timedelta(seconds=offset_seconds - max_seconds)


def raw_source_refresh_reason(source: SourceConfig, existing_payload: RawSavedList | None) -> str | None:
    if existing_payload is None:
        return "missing-raw-snapshot"

    if not raw_source_signature_matches(source, existing_payload.source_signature):
        return "source-config-changed"

    if existing_payload.refresh_after:
        try:
            refresh_after_dt = parse_metadata_datetime(existing_payload.refresh_after)
        except ValueError:
            return "invalid-refresh-after"
        if datetime.now(UTC) >= refresh_after_dt:
            return "refresh-window-expired"
        return None

    if existing_payload.fetched_at:
        try:
            fetched_at_dt = parse_metadata_datetime(existing_payload.fetched_at)
        except ValueError:
            return "invalid-fetched-at"
        if datetime.now(UTC) - fetched_at_dt >= RAW_SOURCE_CACHE_TTL:
            return "legacy-refresh-window-expired"
        return None

    return "missing-refresh-metadata"


def parse_metadata_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def raw_source_signature_matches(source: SourceConfig, signature: str | None) -> bool:
    return signature in raw_source_signature_candidates(source)


def raw_source_signature_candidates(source: SourceConfig) -> set[str]:
    signatures = {raw_source_signature(source)}
    if source.type == "google_list_url":
        signatures.add(legacy_google_list_source_signature(source))
    return signatures


def legacy_google_list_source_signature(source: SourceConfig) -> str:
    payload = {
        "slug": source.slug,
        "url": source.url,
        "title": source.title,
        "refresh_days": RAW_SOURCE_CACHE_TTL.days,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def resolve_refresh_sources(sources: list[SourceConfig], selectors: list[str]) -> list[SourceConfig]:
    if not selectors:
        return []

    normalized_selectors = {selector.strip() for selector in selectors if selector.strip()}
    matched_sources: list[SourceConfig] = []
    missing_selectors = set(normalized_selectors)

    for source in sources:
        if (
            source.slug in normalized_selectors
            or source.url in normalized_selectors
            or source.path in normalized_selectors
        ):
            matched_sources.append(source)
            missing_selectors.discard(source.slug)
            missing_selectors.discard(source.url)
            missing_selectors.discard(source.path)

    if missing_selectors:
        missing_text = ", ".join(sorted(missing_selectors))
        raise RuntimeError(f"No configured list matched: {missing_text}")

    return matched_sources


def cache_refresh_reason(
    place: RawPlace,
    cache_entry: EnrichmentCacheEntry | None,
    *,
    city_name: str | None = None,
    country_name: str | None = None,
    signature_google_place_id: str | None = None,
) -> str | None:
    if cache_entry is None:
        return "missing-cache-entry"

    expected_signature = enrichment_input_signature(
        place,
        city_name=city_name,
        country_name=country_name,
        signature_google_place_id=signature_google_place_id,
    )
    if cache_entry.input_signature != expected_signature:
        return "raw-place-changed"

    if cache_entry_needs_photo_url_retry(cache_entry):
        try:
            fetched_at_dt = parse_metadata_datetime(cache_entry.fetched_at)
        except ValueError:
            return "invalid-fetched-at"
        if datetime.now(UTC) - fetched_at_dt >= PHOTOLESS_REAL_PLACE_CACHE_TTL:
            return "missing-photo-url"

    if cache_entry.refresh_after:
        try:
            refresh_after_dt = parse_metadata_datetime(cache_entry.refresh_after)
        except ValueError:
            return "invalid-refresh-after"
        if datetime.now(UTC) >= refresh_after_dt:
            return "refresh-window-expired"
        return None

    try:
        fetched_at_dt = parse_metadata_datetime(cache_entry.fetched_at)
    except ValueError:
        return "invalid-fetched-at"
    if datetime.now(UTC) - fetched_at_dt > OPERATIONAL_CACHE_TTL:
        return "legacy-cache-entry"
    if cache_entry.input_signature is None:
        return "missing-input-signature"
    return None


ENRICHMENT_REFRESH_REASON_PRIORITY = {
    "missing-cache-entry": 0,
    "raw-place-changed": 1,
    "invalid-refresh-after": 2,
    "invalid-fetched-at": 2,
    "missing-input-signature": 2,
    "missing-photo-url": 3,
    "legacy-cache-entry": 3,
    "refresh-window-expired": 4,
    "forced": 5,
}


def enrichment_refresh_reason(
    place: RawPlace,
    cache_entry: EnrichmentCacheEntry | None,
    *,
    city_name: str | None = None,
    country_name: str | None = None,
    signature_google_place_id: str | None = None,
    force_refresh: bool,
    missing_only: bool,
) -> str | None:
    if force_refresh:
        return "forced"

    refresh_reason = cache_refresh_reason(
        place,
        cache_entry,
        city_name=city_name,
        country_name=country_name,
        signature_google_place_id=signature_google_place_id,
    )
    if missing_only and refresh_reason != "missing-cache-entry":
        return None
    return refresh_reason


def enrichment_job_priority(
    job: tuple[str, str, str, str, dict[str, Any], str | None, str | None]
) -> tuple[int, str, str]:
    slug, place_id, _place_name, refresh_reason, _place_payload, _city_name, _country_name = job
    return (
        ENRICHMENT_REFRESH_REASON_PRIORITY.get(refresh_reason, 99),
        slug,
        place_id,
    )
def place_input_signature(place: RawPlace) -> str:
    payload = structured_place_identity_payload(place)
    if payload is None:
        payload = {
            "name": place.name,
            "address": place.address,
            "lat": place.lat,
            "lng": place.lng,
        }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def enrichment_input_signature(
    place: RawPlace,
    *,
    city_name: str | None = None,
    country_name: str | None = None,
    signature_google_place_id: str | None = None,
) -> str:
    payload = {
        "place": structured_place_identity_payload(place)
        or {
            "name": place.name,
            "address": place.address,
            "lat": place.lat,
            "lng": place.lng,
        },
        "city_name": as_string(city_name),
        "country_name": as_string(country_name),
        "google_place_id_override": as_string(signature_google_place_id),
        "google_maps_places": google_maps_place_scraper_policy_payload(),
        "contact_fields_version": ENRICHMENT_CONTACT_FIELDS_VERSION,
        "search_query_version": 2,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def google_maps_place_scraper_policy_payload() -> dict[str, Any]:
    return {
        "llm_repair": google_maps_place_llm_repair_mode(),
        "collect_reviews": google_maps_place_collect_reviews(),
        "collect_about": google_maps_place_collect_about(),
    }


def structured_place_identity_payload(place: RawPlace) -> dict[str, str] | None:
    cid = place.cid or extract_maps_cid(place.maps_url)
    if cid:
        return {"cid": cid}

    google_id = as_string(place.google_id)
    if google_id:
        return {"google_id": google_id.strip("/").replace("/", "-")}

    maps_place_token = place.maps_place_token or extract_maps_place_token(place.maps_url)
    if maps_place_token:
        return {"maps_place_token": maps_place_token}

    return None


def cache_refresh_ttl(cache_entry: EnrichmentCacheEntry) -> timedelta:
    if cache_entry.error:
        return ERROR_CACHE_TTL
    if cache_entry.matched is False or cache_entry.place is None:
        return UNMATCHED_CACHE_TTL
    if cache_entry.score is None or cache_entry.score < STRONG_MATCH_SCORE:
        return LOW_CONFIDENCE_CACHE_TTL

    place = cache_entry.place
    if cache_entry_needs_photo_url_retry(cache_entry):
        return PHOTOLESS_REAL_PLACE_CACHE_TTL
    if place.business_status and place.business_status != "OPERATIONAL":
        return NON_OPERATIONAL_CACHE_TTL
    if place.rating is not None or place.user_rating_count is not None:
        return RATINGS_CACHE_TTL
    return OPERATIONAL_CACHE_TTL


def build_cache_entry(
    place: RawPlace,
    *,
    source: str | None = None,
    query: str,
    city_name: str | None = None,
    country_name: str | None = None,
    signature_google_place_id: str | None = None,
    matched: bool | None = None,
    score: int | None = None,
    error: str | None = None,
    error_body: str | None = None,
    enrichment_place: EnrichmentPlace | None = None,
) -> EnrichmentCacheEntry:
    fetched_at = datetime.now(UTC)
    cache_entry = EnrichmentCacheEntry(
        fetched_at=fetched_at.isoformat(),
        last_verified_at=fetched_at.isoformat(),
        source=source,
        query=query,
        input_signature=enrichment_input_signature(
            place,
            city_name=city_name,
            country_name=country_name,
            signature_google_place_id=signature_google_place_id,
        ),
        matched=matched,
        score=score,
        error=error,
        error_body=error_body,
        place=enrichment_place,
    )
    cache_entry.refresh_after = (fetched_at + cache_refresh_ttl(cache_entry)).isoformat()
    return cache_entry


def cache_entry_has_publishable_enrichment(entry: EnrichmentCacheEntry | None) -> bool:
    return bool(entry and entry.error is None and entry.matched is True and entry.place is not None)


def cache_entry_needs_photo_url_retry(entry: EnrichmentCacheEntry) -> bool:
    place = entry.place
    return bool(
        entry.error is None
        and entry.matched is True
        and cache_entry_has_page_scrape_enrichment(entry)
        and place is not None
        and not place.main_photo_url
        and not place.photo_url
        and enrichment_place_has_real_place_identity(place)
    )


def cache_entry_has_page_scrape_enrichment(entry: EnrichmentCacheEntry) -> bool:
    if entry.source == "google_maps_page":
        return True
    if "google_maps_page" in entry.merged_sources:
        return True
    place = entry.place
    return bool(
        entry.source == "google_places_api"
        and place is not None
        and (
            place.address_display_en
            or place.address_display_en_source
            or place.category_display_en
            or place.category_display_en_source
            or place.plus_code
            or place.description
            or place.search_result_description
            or place.search_result_url
            or place.review_topics
            or place.reviews
            or place.about_sections
        )
    )


def enrichment_place_has_real_place_identity(place: EnrichmentPlace) -> bool:
    return bool(
        place.google_place_id
        or place.google_place_resource_name
        or resolved_google_maps_url_is_place_page(place.google_maps_uri)
    )


def append_unique_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def google_maps_uri_strength(url: str | None) -> int:
    normalized = as_string(url)
    if normalized is None:
        return 0
    lowered = normalized.lower()
    if "query_place_id=" in lowered:
        return 4
    if "/maps/place/" in lowered or re.search(r"[?&]cid=[^&#]+", lowered):
        return 3
    if "/maps/search/" in lowered:
        return 1
    return 2


def resolved_google_maps_url_is_place_page(url: str | None) -> bool:
    normalized = as_string(url)
    if normalized is None:
        return False
    lowered = normalized.lower()
    return ("/maps/place/" in lowered or re.search(r"[?&]cid=[^&#]+", lowered) is not None) and (
        "/maps/search/" not in lowered
    )


def merge_page_place_into_api_entry(
    page_entry: EnrichmentCacheEntry,
    api_entry: EnrichmentCacheEntry,
) -> EnrichmentCacheEntry:
    page_place = page_entry.place
    api_place = api_entry.place
    if page_place is None or api_place is None:
        return api_entry

    for field_name in (
        "address_display_en",
        "address_display_en_source",
        "address_display_en_confidence",
        "primary_type_display_name_localized",
        "category_display_en",
        "category_display_en_source",
        "category_display_en_confidence",
        "plus_code",
        "description",
        "search_result_description",
        "search_result_url",
        "main_photo_url",
        "photo_url",
        "website",
    ):
        if getattr(api_place, field_name) in (None, "", []):
            setattr(api_place, field_name, getattr(page_place, field_name))

    if not api_place.reservation_links:
        api_place.reservation_links = page_place.reservation_links
    if not api_place.review_topics:
        api_place.review_topics = page_place.review_topics
    if not api_place.reviews:
        api_place.reviews = page_place.reviews
    if not api_place.about_sections:
        api_place.about_sections = page_place.about_sections

    if google_maps_uri_strength(page_place.google_maps_uri) > google_maps_uri_strength(api_place.google_maps_uri):
        api_place.google_maps_uri = page_place.google_maps_uri

    api_entry.merged_sources = merged_enrichment_sources(api_entry, page_entry)
    return api_entry


def merged_enrichment_sources(*entries: EnrichmentCacheEntry) -> list[Literal["google_maps_page", "google_places_api"]]:
    sources: list[Literal["google_maps_page", "google_places_api"]] = []
    for source in ("google_maps_page", "google_places_api"):
        if any(entry.source == source or source in entry.merged_sources for entry in entries):
            sources.append(source)
    return sources


def preserve_existing_enrichment(
    *,
    slug: str,
    place_id: str,
    place_name: str,
    existing_entry: EnrichmentCacheEntry | None,
    refreshed_entry: EnrichmentCacheEntry,
    raw_place: RawPlace | None = None,
    allow_identity_mismatch: bool = False,
) -> tuple[EnrichmentCacheEntry, str | None]:
    canonicalize_enrichment_cache_entry(existing_entry)
    canonicalize_enrichment_cache_entry(refreshed_entry)

    if not cache_entry_has_publishable_enrichment(existing_entry):
        return refreshed_entry, None

    if not cache_entry_has_publishable_enrichment(refreshed_entry):
        reason = refreshed_entry.error or "no matched enrichment"
        return (
            existing_entry,
            f"WARNING: Preserving previous enrichment for {slug}:{place_id} [{place_name}] "
            f"because refresh returned degraded result ({reason}).",
        )

    previous_place = existing_entry.place
    refreshed_place = refreshed_entry.place
    assert previous_place is not None
    assert refreshed_place is not None
    can_preserve_previous_identity = (
        allow_identity_mismatch
        or raw_place is None
        or enrichment_identity_is_compatible_with_raw(raw_place, previous_place)
    )

    preserved_fields: list[str] = []

    if refreshed_place.rating is None and previous_place.rating is not None:
        refreshed_place.rating = previous_place.rating
        append_unique_reason(preserved_fields, "rating")
    if refreshed_place.user_rating_count is None and previous_place.user_rating_count is not None:
        refreshed_place.user_rating_count = previous_place.user_rating_count
        append_unique_reason(preserved_fields, "user_rating_count")
    if not refreshed_place.formatted_address and previous_place.formatted_address:
        refreshed_place.formatted_address = previous_place.formatted_address
        append_unique_reason(preserved_fields, "address")
    if not refreshed_place.address_display_en and previous_place.address_display_en:
        refreshed_place.address_display_en = previous_place.address_display_en
        refreshed_place.address_display_en_source = previous_place.address_display_en_source
        refreshed_place.address_display_en_confidence = previous_place.address_display_en_confidence

    if not refreshed_place.primary_type_display_name and previous_place.primary_type_display_name:
        refreshed_place.primary_type_display_name = previous_place.primary_type_display_name
        if not refreshed_place.primary_type and previous_place.primary_type:
            refreshed_place.primary_type = previous_place.primary_type
        if not refreshed_place.types and previous_place.types:
            refreshed_place.types = previous_place.types[:]
        append_unique_reason(preserved_fields, "primary_category")
    elif (
        refreshed_place.primary_type_display_name == previous_place.primary_type_display_name
        and not refreshed_place.primary_type
        and previous_place.primary_type
    ):
        refreshed_place.primary_type = previous_place.primary_type
        if not refreshed_place.types and previous_place.types:
            refreshed_place.types = previous_place.types[:]

    if not refreshed_place.primary_type_display_name_localized and previous_place.primary_type_display_name_localized:
        refreshed_place.primary_type_display_name_localized = previous_place.primary_type_display_name_localized
    if not refreshed_place.category_display_en and previous_place.category_display_en:
        refreshed_place.category_display_en = previous_place.category_display_en
        refreshed_place.category_display_en_source = previous_place.category_display_en_source
        refreshed_place.category_display_en_confidence = previous_place.category_display_en_confidence

    if not refreshed_place.business_status and previous_place.business_status:
        refreshed_place.business_status = previous_place.business_status
        append_unique_reason(preserved_fields, "status")

    if (
        can_preserve_previous_identity
        and google_maps_uri_strength(refreshed_place.google_maps_uri)
        < google_maps_uri_strength(previous_place.google_maps_uri)
        and google_maps_uri_is_compatible_for_preservation(previous_place, refreshed_place)
    ):
        refreshed_place.google_maps_uri = previous_place.google_maps_uri
        if not refreshed_place.google_place_id and previous_place.google_place_id:
            refreshed_place.google_place_id = previous_place.google_place_id
        if not refreshed_place.google_place_resource_name and previous_place.google_place_resource_name:
            refreshed_place.google_place_resource_name = previous_place.google_place_resource_name
        append_unique_reason(preserved_fields, "maps_url")
    elif can_preserve_previous_identity and google_maps_uri_is_compatible_for_preservation(previous_place, refreshed_place):
        if not refreshed_place.google_place_id and previous_place.google_place_id:
            refreshed_place.google_place_id = previous_place.google_place_id
        if not refreshed_place.google_place_resource_name and previous_place.google_place_resource_name:
            refreshed_place.google_place_resource_name = previous_place.google_place_resource_name

    if not refreshed_place.main_photo_url and previous_place.main_photo_url:
        refreshed_place.main_photo_url = previous_place.main_photo_url
        append_unique_reason(preserved_fields, "photo_url")
    if not refreshed_place.photo_url and previous_place.photo_url:
        refreshed_place.photo_url = previous_place.photo_url
        append_unique_reason(preserved_fields, "photo_url")

    if can_preserve_previous_identity:
        if not refreshed_place.review_topics and previous_place.review_topics:
            refreshed_place.review_topics = previous_place.review_topics[:]
            append_unique_reason(preserved_fields, "review_topics")
        if not refreshed_place.reviews and previous_place.reviews:
            refreshed_place.reviews = previous_place.reviews[:]
            append_unique_reason(preserved_fields, "reviews")
        if not refreshed_place.about_sections and previous_place.about_sections:
            refreshed_place.about_sections = previous_place.about_sections[:]
            append_unique_reason(preserved_fields, "about")
        preserved_semantic_description = semantic_description_for_preservation(
            previous_place,
            refreshed_place,
            raw_place=raw_place,
        )
        if not refreshed_place.semantic_description and preserved_semantic_description:
            refreshed_place.semantic_description = preserved_semantic_description
            refreshed_place.semantic_description_signature = previous_place.semantic_description_signature
            refreshed_place.semantic_source = previous_place.semantic_source
            append_unique_reason(preserved_fields, "semantic_description")

    if not preserved_fields:
        return refreshed_entry, None

    return (
        refreshed_entry,
        f"WARNING: Preserving previous enrichment fields for {slug}:{place_id} [{place_name}]: "
        f"{', '.join(preserved_fields)}.",
    )


def semantic_description_for_preservation(
    previous_place: EnrichmentPlace,
    refreshed_place: EnrichmentPlace,
    *,
    raw_place: RawPlace | None,
) -> str | None:
    if not previous_place.semantic_description:
        return None
    if raw_place is None:
        return sanitize_semantic_description(previous_place.semantic_description)
    return usable_semantic_description(
        previous_place.semantic_description,
        enrichment_place=refreshed_place,
        raw_place=raw_place,
        city_name=None,
        country_name=None,
    )


def google_maps_uri_is_compatible_for_preservation(
    previous_place: EnrichmentPlace,
    refreshed_place: EnrichmentPlace,
) -> bool:
    previous_address = normalize_text(previous_place.formatted_address)
    refreshed_address = normalize_text(refreshed_place.formatted_address)
    if previous_address and refreshed_address and token_overlap_score(previous_address, refreshed_address) == 0:
        return False
    return True


def load_places_cache(slug: str) -> dict[str, EnrichmentCacheEntry]:
    return load_places_cache_from_sqlite(slug) or {}


def save_places_cache(
    slug: str,
    payload: dict[str, EnrichmentCacheEntry],
    *,
    allow_empty_overwrite: bool = False,
) -> None:
    save_places_cache_to_sqlite(slug, payload, allow_empty_overwrite=allow_empty_overwrite)


def prune_places_cache_to_raw_places(
    payload: dict[str, EnrichmentCacheEntry],
    raw: RawSavedList,
) -> tuple[dict[str, EnrichmentCacheEntry], int]:
    current_place_ids = {
        stable_place_id(place, source_type=raw.configured_source_type)
        for place in raw.places
    }
    if not current_place_ids:
        return {}, len(payload)
    pruned_payload = {
        place_id: entry
        for place_id, entry in payload.items()
        if place_id in current_place_ids
    }
    return pruned_payload, len(payload) - len(pruned_payload)


def export_places_cache_json(slug: str, payload: dict[str, EnrichmentCacheEntry]) -> None:
    write_json(PLACES_CACHE_DIR / f"{slug}.json", payload)


def sqlite_table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def sqlite_bool_or_none(value: bool | None) -> int | None:
    if value is None:
        return None
    return sqlite_bool(value)


def serialized_enrichment_cache_entry(entry: EnrichmentCacheEntry) -> str:
    return json.dumps(
        enrichment_cache_entry_payload(entry),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def sqlite_error_is_unreadable_database(error: sqlite3.Error) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in SQLITE_UNREADABLE_ERROR_MARKERS)


def ensure_guide_enrichment_cache_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS guide_enrichment_cache (
            guide_slug TEXT NOT NULL,
            place_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            last_verified_at TEXT,
            refresh_after TEXT,
            source TEXT,
            query TEXT NOT NULL,
            input_signature TEXT,
            matched INTEGER,
            score INTEGER,
            error TEXT,
            error_body TEXT,
            cache_json TEXT NOT NULL,
            PRIMARY KEY (guide_slug, place_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_guide_enrichment_cache_place_id
            ON guide_enrichment_cache(place_id)
        """
    )


def load_places_cache_from_sqlite(slug: str) -> dict[str, EnrichmentCacheEntry] | None:
    if not PLACES_SQLITE_PATH.exists():
        return None

    connection = sqlite3.connect(PLACES_SQLITE_PATH)
    try:
        try:
            if not sqlite_table_exists(connection, "guide_enrichment_cache"):
                return None

            rows = connection.execute(
                """
                SELECT place_id, cache_json
                FROM guide_enrichment_cache
                WHERE guide_slug = ?
                ORDER BY place_id
                """,
                (slug,),
            ).fetchall()
        except sqlite3.DatabaseError as error:
            if not sqlite_error_is_unreadable_database(error):
                raise
            return None
    finally:
        connection.close()

    result: dict[str, EnrichmentCacheEntry] = {}
    for place_id, cache_json in rows:
        if not isinstance(place_id, str) or not isinstance(cache_json, str):
            continue
        entry = EnrichmentCacheEntry.model_validate_json(cache_json)
        canonicalized_entry = canonicalized_enrichment_cache_entry_copy(entry)
        assert canonicalized_entry is not None
        result[place_id] = canonicalized_entry
    return result


def save_places_cache_to_sqlite(
    slug: str,
    payload: dict[str, EnrichmentCacheEntry],
    *,
    allow_empty_overwrite: bool = False,
) -> None:
    PLACES_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(PLACES_SQLITE_PATH)
    try:
        with connection:
            ensure_guide_enrichment_cache_table(connection)
            if not payload and not allow_empty_overwrite:
                existing_count = connection.execute(
                    "SELECT COUNT(*) FROM guide_enrichment_cache WHERE guide_slug = ?",
                    (slug,),
                ).fetchone()[0]
                if existing_count:
                    raise RuntimeError(
                        f"Refusing to replace non-empty enrichment cache for {slug} "
                        "with an empty payload."
                    )
            connection.execute(
                "DELETE FROM guide_enrichment_cache WHERE guide_slug = ?",
                (slug,),
            )
            if payload:
                serialized_rows = []
                for place_id, entry in sorted(payload.items()):
                    serialized_rows.append(
                        (
                            slug,
                            place_id,
                            entry.fetched_at,
                            entry.last_verified_at,
                            entry.refresh_after,
                            entry.source,
                            entry.query,
                            entry.input_signature,
                            sqlite_bool_or_none(entry.matched),
                            entry.score,
                            entry.error,
                            entry.error_body,
                            serialized_enrichment_cache_entry(entry),
                        )
                    )
                connection.executemany(
                    """
                    INSERT INTO guide_enrichment_cache (
                        guide_slug, place_id, fetched_at, last_verified_at, refresh_after, source,
                        query, input_signature, matched, score, error, error_body, cache_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    serialized_rows,
                )
    finally:
        connection.close()


def existing_places_cache_sqlite_row_counts(slugs: Iterable[str]) -> dict[str, int]:
    if not PLACES_SQLITE_PATH.exists():
        return {}
    connection = sqlite3.connect(PLACES_SQLITE_PATH)
    try:
        if not sqlite_table_exists(connection, "guide_enrichment_cache"):
            return {}
        return {
            slug: connection.execute(
                "SELECT COUNT(*) FROM guide_enrichment_cache WHERE guide_slug = ?",
                (slug,),
            ).fetchone()[0]
            for slug in slugs
        }
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def export_all_places_cache_json() -> int:
    PLACES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    exported = 0
    for path in sorted(PLACES_CACHE_DIR.glob("*.json")):
        path.unlink()

    for raw_path in sorted(RAW_DIR.glob("*.json")):
        slug = raw_path.stem
        payload = load_places_cache(slug)
        if not payload:
            continue
        export_places_cache_json(slug, payload)
        exported += 1

    return exported


def migrate_cache_to_sqlite(*, rewrite_raw_maps_urls: bool = True) -> tuple[int, int, int, int]:
    sync_local_csv_sources()
    migrated_guides = 0
    raw_places_rewritten = 0
    cache_entries_rewritten = 0
    sqlite_cache_rows = 0

    for raw_path in sorted(RAW_DIR.glob("*.json")):
        slug = raw_path.stem
        raw = RawSavedList.model_validate_json(raw_path.read_text(encoding="utf-8"))
        raw_changed = False
        cache_changed = False
        updated_places: list[RawPlace] = []

        cache_payload = load_places_cache(slug)

        for place in raw.places:
            updated_place = place
            prior_place_id = stable_place_id(place, source_type=raw.configured_source_type)
            if rewrite_raw_maps_urls:
                public_maps_url = build_public_google_maps_url(
                    name=place.name,
                    address=place.address,
                    lat=place.lat,
                    lng=place.lng,
                    raw_maps_url=place.maps_url,
                )
                if public_maps_url != place.maps_url:
                    updated_place = updated_place.model_copy(update={"maps_url": public_maps_url})
                    raw_changed = True
                    raw_places_rewritten += 1

            place_id = stable_place_id(updated_place, source_type=raw.configured_source_type)
            cache_entry = cache_payload.get(place_id)
            if cache_entry is None and prior_place_id != place_id:
                cache_entry = cache_payload.pop(prior_place_id, None)
            if cache_entry is not None and place_id not in cache_payload:
                cache_payload[place_id] = cache_entry
                cache_changed = True
                cache_entries_rewritten += 1

            updated_places.append(updated_place)

        if raw_changed:
            raw = raw.model_copy(update={"places": updated_places})
            write_json(raw_path, raw)

        save_places_cache(slug, cache_payload)
        sqlite_cache_rows += len(cache_payload)
        if raw_changed or cache_changed:
            migrated_guides += 1

    return migrated_guides, raw_places_rewritten, cache_entries_rewritten, sqlite_cache_rows


def place_photo_signature_state(
    public_path: str | None,
    *,
    source_photo_url: str | None,
    metadata_cache: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...]:
    cache_key = public_path or f"missing:{source_photo_url or 'none'}"
    cached = metadata_cache.get(cache_key)
    if cached is not None:
        return cached

    absolute_path = public_asset_absolute_path(public_path)
    if absolute_path and absolute_path.exists():
        stat_result = absolute_path.stat()
        state = (
            "downloaded",
            sha256_file(absolute_path),
            stat_result.st_size,
            source_photo_url,
            public_path,
        )
    elif source_photo_url:
        state = ("missing_file", source_photo_url, public_path)
    else:
        state = ("missing_url", public_path)

    metadata_cache[cache_key] = state
    return state


def build_places_sqlite_signature(
    *,
    raw_lists: dict[str, RawSavedList],
    guides: list[Guide],
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]],
) -> str:
    _ = raw_lists, guides

    payload = {
        "signature_version": PLACES_SQLITE_SIGNATURE_VERSION,
        "schema_sha256": hashlib.sha256(PLACES_SQLITE_SCHEMA_SQL.encode("utf-8")).hexdigest(),
        "enrichment_caches": {
            slug: {
                place_id: enrichment_cache_entry_payload(entry)
                for place_id, entry in sorted(cache_payload.items())
            }
            for slug, cache_payload in sorted(enrichment_caches.items())
        },
    }
    serialized = json.dumps(
        normalize_json_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_places_sqlite_build_signature() -> str | None:
    if not PLACES_SQLITE_PATH.exists():
        return None

    connection = sqlite3.connect(PLACES_SQLITE_PATH)
    try:
        try:
            if not sqlite_table_exists(connection, "build_metadata"):
                return None
            row = connection.execute(
                "SELECT value FROM build_metadata WHERE key = ?",
                (PLACES_SQLITE_BUILD_METADATA_KEY,),
            ).fetchone()
        except sqlite3.DatabaseError as error:
            if not sqlite_error_is_unreadable_database(error):
                raise
            return None
    finally:
        connection.close()

    if row is None or not isinstance(row[0], str):
        return None
    return row[0]


def enrichment_place_identity_key(place: EnrichmentPlace | None) -> str | None:
    if place is None:
        return None
    if place.google_place_id:
        return f"google_place_id:{place.google_place_id}"
    if place.google_place_resource_name:
        return f"google_place_resource_name:{place.google_place_resource_name}"
    return None


def enrichment_photo_donor_score(entry: EnrichmentCacheEntry) -> tuple[int, int, str]:
    place = entry.place or EnrichmentPlace()
    return (
        1 if place.main_photo_url else 0,
        1 if place.photo_url else 0,
        entry.fetched_at,
    )


def shared_enrichment_photo_identity_keys(
    place_id: str,
    entry: EnrichmentCacheEntry,
) -> list[str]:
    keys: list[str] = []
    place = entry.place
    identity_key = enrichment_place_identity_key(place)
    if identity_key:
        keys.append(identity_key)
    display_name = normalize_text(place.display_name) if place and place.display_name else ""
    if display_name:
        keys.append(f"place_id_name:{place_id}:{display_name}")
    return keys


def hydrate_shared_enrichment_photo_urls(
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]],
) -> int:
    photo_donors: dict[str, tuple[tuple[int, int, str], str | None, str | None]] = {}
    hydrated_count = 0

    for payload in enrichment_caches.values():
        for place_id, entry in payload.items():
            place = entry.place
            if (
                place is None
                or (not place.main_photo_url and not place.photo_url)
            ):
                continue
            candidate = (
                enrichment_photo_donor_score(entry),
                place.main_photo_url,
                place.photo_url,
            )
            for identity_key in shared_enrichment_photo_identity_keys(place_id, entry):
                donor = photo_donors.get(identity_key)
                if donor is None or candidate[0] > donor[0]:
                    photo_donors[identity_key] = candidate

    for payload in enrichment_caches.values():
        for place_id, entry in payload.items():
            place = entry.place
            if place is None:
                continue
            donor: tuple[tuple[int, int, str], str | None, str | None] | None = None
            for identity_key in shared_enrichment_photo_identity_keys(place_id, entry):
                donor = photo_donors.get(identity_key)
                if donor is not None:
                    break
            if donor is None:
                continue
            _, donor_main_photo_url, donor_photo_url = donor
            if not place.main_photo_url and donor_main_photo_url:
                place.main_photo_url = donor_main_photo_url
                hydrated_count += 1
            if not place.photo_url and donor_photo_url:
                place.photo_url = donor_photo_url
                hydrated_count += 1
    return hydrated_count


def build_places_sqlite_rows(
    *,
    raw_lists: dict[str, RawSavedList],
    guides: list[Guide],
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]],
) -> PlacesSqliteRows:
    _ = raw_lists, guides

    guide_cache_rows = [
        (
            guide_slug,
            place_id,
            entry.fetched_at,
            entry.last_verified_at,
            entry.refresh_after,
            entry.source,
            entry.query,
            entry.input_signature,
            sqlite_bool_or_none(entry.matched),
            entry.score,
            entry.error,
            entry.error_body,
            serialized_enrichment_cache_entry(entry),
        )
        for guide_slug, payload in sorted(enrichment_caches.items())
        for place_id, entry in sorted(payload.items())
    ]

    return PlacesSqliteRows(
        guide_cache_rows=guide_cache_rows,
    )


def write_places_sqlite(
    rows: PlacesSqliteRows,
    *,
    build_signature: str,
) -> None:
    PLACES_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = PLACES_SQLITE_PATH.with_suffix(".sqlite.tmp")
    tmp_path.unlink(missing_ok=True)

    connection = sqlite3.connect(tmp_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(PLACES_SQLITE_SCHEMA_SQL)

        with connection:
            connection.execute(
                "INSERT INTO build_metadata (key, value) VALUES (?, ?)",
                (PLACES_SQLITE_BUILD_METADATA_KEY, build_signature),
            )
            connection.executemany(
                """
                INSERT INTO guide_enrichment_cache (
                    guide_slug, place_id, fetched_at, last_verified_at, refresh_after, source,
                    query, input_signature, matched, score, error, error_body, cache_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows.guide_cache_rows,
            )
    finally:
        connection.close()

    tmp_path.replace(PLACES_SQLITE_PATH)


def rebuild_places_sqlite(
    *,
    raw_lists: dict[str, RawSavedList],
    guides: list[Guide],
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]],
) -> None:
    existing_row_counts = existing_places_cache_sqlite_row_counts(raw_lists.keys())
    pruned_any = False
    for slug, raw in raw_lists.items():
        cache_payload = enrichment_caches.get(slug)
        if cache_payload is None:
            continue
        pruned_payload, pruned_count = prune_places_cache_to_raw_places(cache_payload, raw)
        if pruned_count:
            enrichment_caches[slug] = pruned_payload
            pruned_any = True
    empty_replacements = [
        slug
        for slug, raw in raw_lists.items()
        if raw.places
        and not enrichment_caches.get(slug)
        and existing_row_counts.get(slug, 0) > 0
    ]
    if empty_replacements:
        joined_slugs = ", ".join(sorted(empty_replacements))
        raise RuntimeError(
            "Refusing to rebuild enrichment cache with empty payloads for "
            f"non-empty guide(s): {joined_slugs}."
        )
    hydrated_count = hydrate_shared_enrichment_photo_urls(enrichment_caches)
    build_signature = build_places_sqlite_signature(
        raw_lists=raw_lists,
        guides=guides,
        enrichment_caches=enrichment_caches,
    )
    if not pruned_any and not hydrated_count and load_places_sqlite_build_signature() == build_signature:
        return

    rows = build_places_sqlite_rows(
        raw_lists=raw_lists,
        guides=guides,
        enrichment_caches=enrichment_caches,
    )
    write_places_sqlite(rows, build_signature=build_signature)


def sqlite_bool(value: bool) -> int:
    return 1 if value else 0


def public_asset_absolute_path(public_path: str | None) -> Path | None:
    normalized = as_string(public_path)
    if normalized is None:
        return None
    relative_path = normalized.lstrip("/")
    site_public_path = SITE_DIR / "public" / relative_path
    if site_public_path.exists():
        return site_public_path
    return ROOT / "public" / relative_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sync_guide_place_photos(
    guide: Guide,
    *,
    enrichment_cache: dict[str, EnrichmentCacheEntry],
    startup_jitter_seconds: float = 0,
) -> None:
    for place in guide.places:
        photo_url = cached_place_photo_url(enrichment_cache.get(place.id))
        place.main_photo_path = sync_place_photo(
            guide.slug,
            place.id,
            photo_url=photo_url,
            startup_jitter_seconds=startup_jitter_seconds,
        )


def cached_place_photo_url(cache_entry: EnrichmentCacheEntry | None) -> str | None:
    if cache_entry is None or cache_entry.place is None:
        return None
    return cache_entry.place.main_photo_url or cache_entry.place.photo_url


def sync_list_author_photo(slug: str, author: ListAuthor | None) -> ListAuthor | None:
    if author is None or not author.photo_url:
        return author
    photo_path = download_list_author_photo(
        slug,
        author.photo_url,
        profile_id=author.profile_id,
    )
    if photo_path is None:
        return author
    if photo_path == author.photo_path:
        return author
    return author.model_copy(update={"photo_path": photo_path})


def download_list_author_photo(
    slug: str,
    photo_url: str,
    *,
    profile_id: str | None = None,
) -> str | None:
    AUTHOR_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    photo_stem = canonical_author_photo_stem(profile_id=profile_id, photo_url=photo_url)
    existing_path = existing_author_photo_path(photo_stem)
    if existing_path is not None:
        return public_author_photo_path(existing_path.name)

    try:
        request = Request(
            photo_url,
            headers={"User-Agent": "Mozilla/5.0 favorite-places author photo fetch"},
        )
        with urlopen(request, timeout=PHOTO_DOWNLOAD_TIMEOUT_SECONDS) as response:
            content = response.read()
            if not content:
                return None
            content_type = response_content_type(response)
    except (HTTPError, URLError, OSError) as exc:
        print(f"WARNING: Failed to download author photo for {slug} from {photo_url}: {exc}", flush=True)
        return None

    optimized_content, extension = optimize_author_photo_asset(content, content_type=content_type)
    if optimized_content is None or extension is None:
        return None

    filename = f"{photo_stem}{extension}"
    output_path = AUTHOR_PHOTOS_DIR / filename
    temp_path = author_photo_temp_path(filename)
    try:
        temp_path.write_bytes(optimized_content)
        temp_path.replace(output_path)
    finally:
        temp_path.unlink(missing_ok=True)

    for stale_path in stale_legacy_author_photo_paths(slug, keep_filename=filename):
        stale_path.unlink(missing_ok=True)

    return public_author_photo_path(filename)


def existing_author_photo_path(photo_stem: str) -> Path | None:
    for extension in (".webp", ".jpg"):
        photo_path = AUTHOR_PHOTOS_DIR / f"{photo_stem}{extension}"
        if photo_path.exists():
            return photo_path
    return None


def author_photo_temp_path(filename: str) -> Path:
    return AUTHOR_PHOTOS_DIR / f".{filename}.{uuid.uuid4().hex}.tmp"


def stale_legacy_author_photo_paths(slug: str, *, keep_filename: str) -> list[Path]:
    if not AUTHOR_PHOTOS_DIR.exists():
        return []

    safe_slug = safe_author_photo_stem(slug)
    current_prefix = f"{safe_slug}--"
    legacy_pattern = re.compile(rf"^{re.escape(safe_slug)}-[0-9a-f]{{12}}\.(?:webp|jpg)$")
    stale_paths: list[Path] = []
    for stale_path in AUTHOR_PHOTOS_DIR.iterdir():
        if stale_path.name == keep_filename or stale_path.name.startswith(".") or not stale_path.is_file():
            continue
        if stale_path.name.startswith(current_prefix) or legacy_pattern.fullmatch(stale_path.name):
            stale_paths.append(stale_path)
    return stale_paths


def safe_author_photo_stem(slug: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-._")
    return sanitized or "author"


def canonical_author_photo_stem(*, profile_id: str | None, photo_url: str) -> str:
    photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]
    if profile_id:
        return f"profile-{safe_author_photo_stem(profile_id)}--{photo_hash}"
    return f"photo-{photo_hash}"


def populate_place_photos_for_guides(
    guides: list[Guide],
    *,
    enrichment_caches: dict[str, dict[str, EnrichmentCacheEntry]],
    refresh_photos: bool,
    photo_workers: int,
    startup_jitter_seconds: float,
) -> None:
    flat_index = build_place_photo_flat_index()
    if not refresh_photos:
        for guide in guides:
            if guide.place_photo_mode == "remote_url":
                for place in guide.places:
                    place.main_photo_path = None
                continue
            enrichment_cache = enrichment_caches.get(guide.slug, {})
            for place in guide.places:
                photo_url = cached_place_photo_url(enrichment_cache.get(place.id))
                place.main_photo_path, fallback_reason = resolve_existing_place_photo_path(
                    guide.slug,
                    place.id,
                    photo_url=photo_url,
                    allow_stale_place_id_fallback=True,
                    flat_index=flat_index,
                )
                if fallback_reason == "missing-photo-url":
                    print(
                        f"WARNING: Reusing existing local photo for {guide.slug}:{place.id} "
                        "because current enrichment did not yield a photo URL. "
                        "Photo extraction may be failing on this runner.",
                        flush=True,
                    )
        return

    pending_jobs: list[PendingPhotoJob] = []
    reused_count = 0
    missing_photo_url_count = 0
    for guide in guides:
        if guide.place_photo_mode == "remote_url":
            for place in guide.places:
                place.main_photo_path = None
            continue
        enrichment_cache = enrichment_caches.get(guide.slug, {})
        for place in guide.places:
            photo_url = cached_place_photo_url(enrichment_cache.get(place.id))
            if not photo_url:
                place.main_photo_path = None
                missing_photo_url_count += 1
                continue

            existing_path = existing_place_photo_path(
                guide.slug,
                place.id,
                photo_url=photo_url,
                flat_index=flat_index,
            )
            if existing_path is not None:
                place.main_photo_path = existing_path
                reused_count += 1
                continue

            pending_jobs.append(
                PendingPhotoJob(
                    guide_slug=guide.slug,
                    place_id=place.id,
                    place_name=place.name,
                    photo_url=photo_url,
                )
            )

    if not pending_jobs:
        print(
            "No new place photos to download"
            f" ({reused_count} existing, {missing_photo_url_count} without photo URLs)",
            flush=True,
        )
        return

    place_by_key = {
        (guide.slug, place.id): place
        for guide in guides
        for place in guide.places
    }

    max_workers = max(1, min(photo_workers, len(pending_jobs)))
    effective_startup_jitter_seconds = startup_jitter_seconds if len(pending_jobs) > 1 else 0
    print(
        "Downloading"
        f" {len(pending_jobs)} place photos with {max_workers} workers"
        f" ({reused_count} existing, {missing_photo_url_count} without photo URLs)",
        flush=True,
    )

    if max_workers == 1 or len(pending_jobs) == 1:
        for index, job in enumerate(pending_jobs, start=1):
            photo_path = sync_place_photo(
                job.guide_slug,
                job.place_id,
                photo_url=job.photo_url,
                startup_jitter_seconds=effective_startup_jitter_seconds,
            )
            place_by_key[(job.guide_slug, job.place_id)].main_photo_path = photo_path
            print(photo_progress_line(index, len(pending_jobs), job, photo_path), flush=True)
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                sync_place_photo,
                job.guide_slug,
                job.place_id,
                photo_url=job.photo_url,
                startup_jitter_seconds=effective_startup_jitter_seconds,
            ): job
            for job in pending_jobs
        }
        for index, future in enumerate(as_completed(future_map), start=1):
            job = future_map[future]
            photo_path = future.result()
            place_by_key[(job.guide_slug, job.place_id)].main_photo_path = photo_path
            print(photo_progress_line(index, len(pending_jobs), job, photo_path), flush=True)


def photo_progress_line(
    completed_count: int,
    total_count: int,
    job: PendingPhotoJob,
    photo_path: str | None,
) -> str:
    outcome = "downloaded" if photo_path else "failed"
    return (
        f"[photos {completed_count}/{total_count}] {outcome}: "
        f"{job.guide_slug} / {job.place_name}"
    )


def resolve_existing_place_photo_path(
    slug: str,
    place_id: str,
    *,
    photo_url: str | None,
    allow_stale_place_id_fallback: bool = False,
    flat_index: PlacePhotoFlatIndex | None = None,
) -> tuple[str | None, str | None]:
    if photo_url:
        filename_glob = canonical_place_photo_glob(place_id, photo_url)
        if flat_index is not None:
            canonical_match = flat_index.exact_by_stem.get(canonical_place_photo_stem(place_id, photo_url))
            if canonical_match:
                remove_legacy_place_photo_matches(slug, filename_glob=filename_glob)
                return public_photo_path(canonical_match), None
        else:
            canonical_matches = sorted(PLACE_PHOTOS_DIR.glob(filename_glob))
            if canonical_matches:
                remove_legacy_place_photo_matches(slug, filename_glob=filename_glob)
                return public_photo_path(canonical_matches[0].name), None

        legacy_matches = sorted((PLACE_PHOTOS_DIR / slug).glob(filename_glob))
        if legacy_matches:
            migrated_path = migrate_legacy_place_photo_to_flat_dir(
                slug,
                filename_glob=filename_glob,
                legacy_path=legacy_matches[0],
            )
            return public_photo_path(migrated_path.name), None

    if not allow_stale_place_id_fallback:
        return None, None

    place_prefix = f"{safe_place_photo_stem(place_id)}-"
    if flat_index is not None:
        fallback_match = flat_index.latest_by_place_prefix.get(place_prefix)
        if fallback_match:
            fallback_reason = "stale-photo-url" if photo_url else "missing-photo-url"
            return public_photo_path(fallback_match), fallback_reason
    else:
        fallback_matches = sorted(
            (
                path
                for path in PLACE_PHOTOS_DIR.glob(f"{place_prefix}*.*")
                if path.is_file() and not path.name.startswith(".")
            ),
            key=lambda path: (path.stat().st_mtime, path.name),
            reverse=True,
        )
        if fallback_matches:
            fallback_reason = "stale-photo-url" if photo_url else "missing-photo-url"
            return public_photo_path(fallback_matches[0].name), fallback_reason

    legacy_dir = PLACE_PHOTOS_DIR / slug
    legacy_fallback_matches = sorted(
        (
            path
            for path in legacy_dir.glob(f"{place_prefix}*.*")
            if path.is_file() and not path.name.startswith(".")
        ),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    if not legacy_fallback_matches:
        return None, None

    migrated_path = migrate_legacy_place_photo_to_flat_dir(
        slug,
        filename_glob=legacy_fallback_matches[0].name,
        legacy_path=legacy_fallback_matches[0],
    )
    fallback_reason = "stale-photo-url" if photo_url else "missing-photo-url"
    return public_photo_path(migrated_path.name), fallback_reason


def existing_place_photo_path(
    slug: str,
    place_id: str,
    *,
    photo_url: str | None,
    allow_stale_place_id_fallback: bool = False,
    flat_index: PlacePhotoFlatIndex | None = None,
) -> str | None:
    path, _fallback_reason = resolve_existing_place_photo_path(
        slug,
        place_id,
        photo_url=photo_url,
        allow_stale_place_id_fallback=allow_stale_place_id_fallback,
        flat_index=flat_index,
    )
    return path


def build_place_photo_flat_index() -> PlacePhotoFlatIndex:
    exact_by_stem: dict[str, str] = {}
    latest_by_place_prefix: dict[str, tuple[float, str]] = {}
    if not PLACE_PHOTOS_DIR.exists():
        return PlacePhotoFlatIndex(exact_by_stem={}, latest_by_place_prefix={})

    for path in PLACE_PHOTOS_DIR.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue

        current_name = exact_by_stem.get(path.stem)
        if current_name is None or path.name < current_name:
            exact_by_stem[path.stem] = path.name

        place_prefix = place_photo_prefix_from_stem(path.stem)
        if place_prefix is None:
            continue

        candidate = (path.stat().st_mtime, path.name)
        existing = latest_by_place_prefix.get(place_prefix)
        if existing is None or candidate > existing:
            latest_by_place_prefix[place_prefix] = candidate

    return PlacePhotoFlatIndex(
        exact_by_stem=exact_by_stem,
        latest_by_place_prefix={
            place_prefix: filename
            for place_prefix, (_mtime, filename) in latest_by_place_prefix.items()
        },
    )


def place_photo_prefix_from_stem(stem: str) -> str | None:
    match = re.match(r"^(.+)-[0-9a-f]{12}$", stem)
    if match is None:
        return None
    return f"{match.group(1)}-"


def sync_place_photo(
    slug: str,
    place_id: str,
    *,
    photo_url: str | None,
    startup_jitter_seconds: float = 0,
) -> str | None:
    if not photo_url:
        return None

    photo_dir = PLACE_PHOTOS_DIR
    photo_dir.mkdir(parents=True, exist_ok=True)

    existing_path = existing_place_photo_path(slug, place_id, photo_url=photo_url)
    if existing_path is not None:
        return existing_path

    try:
        sleep_for_refresh_startup_jitter(startup_jitter_seconds)
        request = Request(
            photo_url,
            headers={"User-Agent": "Mozilla/5.0 favorite-places photo fetch"},
        )
        with urlopen(request, timeout=PHOTO_DOWNLOAD_TIMEOUT_SECONDS) as response:
            content = response.read()
            if not content:
                return None
            content_type = response_content_type(response)
    except (HTTPError, URLError, OSError) as exc:
        print(
            f"WARNING: Failed to download photo for {slug}:{place_id} from {photo_url}: {exc}",
            flush=True,
        )
        return None

    optimized_content, extension = optimize_place_photo_asset(
        content,
        content_type=content_type,
    )
    if optimized_content is None:
        return None

    place_prefix = canonical_place_photo_stem(place_id, photo_url)
    filename = canonical_place_photo_filename(place_id, photo_url, extension=extension)
    output_path = photo_dir / filename
    temp_path = photo_dir / f".{filename}.tmp"
    temp_path.write_bytes(optimized_content)
    temp_path.replace(output_path)

    for stale_path in photo_dir.glob(f"{place_prefix}-*"):
        if stale_path.name == filename or stale_path.name.startswith("."):
            continue
        stale_path.unlink(missing_ok=True)

    remove_legacy_place_photo_matches(slug, filename_glob=canonical_place_photo_glob(place_id, photo_url))
    return public_photo_path(filename)


def safe_place_photo_stem(place_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", place_id).strip("-._")
    return sanitized or "place"


def canonical_place_photo_glob(place_id: str, photo_url: str) -> str:
    return f"{canonical_place_photo_stem(place_id, photo_url)}.*"


def canonical_place_photo_filename(place_id: str, photo_url: str, *, extension: str) -> str:
    return f"{canonical_place_photo_stem(place_id, photo_url)}{extension}"


def canonical_place_photo_stem(place_id: str, photo_url: str) -> str:
    place_prefix = safe_place_photo_stem(place_id)
    photo_hash = hashlib.sha256(photo_url.encode("utf-8")).hexdigest()[:12]
    return f"{place_prefix}-{photo_hash}"


def migrate_legacy_place_photo_to_flat_dir(
    slug: str,
    *,
    filename_glob: str,
    legacy_path: Path,
) -> Path:
    canonical_path = PLACE_PHOTOS_DIR / legacy_path.name
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    if canonical_path.exists():
        legacy_path.unlink(missing_ok=True)
    else:
        legacy_path.replace(canonical_path)

    remove_legacy_place_photo_matches(slug, filename_glob=filename_glob)
    return canonical_path


def remove_legacy_place_photo_matches(slug: str, *, filename_glob: str) -> None:
    legacy_dir = PLACE_PHOTOS_DIR / slug
    if not legacy_dir.exists():
        return

    for stale_path in legacy_dir.glob(filename_glob):
        stale_path.unlink(missing_ok=True)

    try:
        next(legacy_dir.iterdir())
    except StopIteration:
        legacy_dir.rmdir()
    except FileNotFoundError:
        return


def response_content_type(response: Any) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    get_content_type = getattr(headers, "get_content_type", None)
    if callable(get_content_type):
        return get_content_type()
    get = getattr(headers, "get", None)
    if callable(get):
        return as_string(get("Content-Type"))
    return None


def optimize_place_photo_asset(
    content: bytes,
    *,
    content_type: str | None,
) -> tuple[bytes, str] | tuple[None, None]:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            working = ImageOps.exif_transpose(image)
            resized = ImageOps.fit(
                working,
                (PHOTO_CARD_WIDTH, PHOTO_CARD_HEIGHT),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )

            output = io.BytesIO()
            if image_supports_webp():
                if resized.mode not in {"RGB", "RGBA"}:
                    resized = resized.convert("RGB")
                resized.save(
                    output,
                    format="WEBP",
                    quality=PHOTO_CARD_QUALITY,
                    method=6,
                )
                return output.getvalue(), ".webp"

            if resized.mode != "RGB":
                resized = resized.convert("RGB")
            resized.save(
                output,
                format="JPEG",
                quality=PHOTO_CARD_QUALITY,
                optimize=True,
            )
            return output.getvalue(), ".jpg"
    except (OSError, UnidentifiedImageError) as exc:
        print(
            f"WARNING: Failed to optimize downloaded photo ({content_type or 'unknown'}): {exc}",
            flush=True,
        )
        return None, None


def optimize_author_photo_asset(
    content: bytes,
    *,
    content_type: str | None,
) -> tuple[bytes, str] | tuple[None, None]:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            working = ImageOps.exif_transpose(image)
            resized = ImageOps.fit(
                working,
                (AUTHOR_PHOTO_SIZE, AUTHOR_PHOTO_SIZE),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )

            output = io.BytesIO()
            if image_supports_webp():
                if resized.mode not in {"RGB", "RGBA"}:
                    resized = resized.convert("RGB")
                resized.save(
                    output,
                    format="WEBP",
                    quality=AUTHOR_PHOTO_QUALITY,
                    method=6,
                )
                return output.getvalue(), ".webp"

            if resized.mode != "RGB":
                resized = resized.convert("RGB")
            resized.save(
                output,
                format="JPEG",
                quality=AUTHOR_PHOTO_QUALITY,
                optimize=True,
            )
            return output.getvalue(), ".jpg"
    except (OSError, UnidentifiedImageError) as exc:
        print(
            f"WARNING: Failed to optimize downloaded author photo ({content_type or 'unknown'}): {exc}",
            flush=True,
        )
        return None, None


def image_supports_webp() -> bool:
    return bool(features.check("webp"))


def public_photo_path(filename: str) -> str:
    return f"/place-photos/{filename}"


def public_author_photo_path(filename: str) -> str:
    return f"/author-photos/{filename}"


def place_details_display_field_map(details: Any) -> dict[str, object]:
    return {
        "category": as_string(getattr(details, "category", None)),
        "category_display_en": as_string(getattr(details, "category_display_en", None)),
        "category_display_en_source": as_string(getattr(details, "category_display_en_source", None)),
        "category_display_en_confidence": as_string(getattr(details, "category_display_en_confidence", None)),
        "address": as_string(getattr(details, "address", None)),
        "address_display_en": as_string(getattr(details, "address_display_en", None)),
        "address_display_en_source": as_string(getattr(details, "address_display_en_source", None)),
        "address_display_en_confidence": as_string(getattr(details, "address_display_en_confidence", None)),
    }


def enrichment_place_display_field_map(place: EnrichmentPlace | None) -> dict[str, object]:
    if place is None:
        return {}
    return {
        "category": place.primary_type_display_name_localized or place.primary_type_display_name,
        "category_display_en": place.category_display_en,
        "category_display_en_source": place.category_display_en_source,
        "category_display_en_confidence": place.category_display_en_confidence,
        "address": place.formatted_address,
        "address_display_en": place.address_display_en,
        "address_display_en_source": place.address_display_en_source,
        "address_display_en_confidence": place.address_display_en_confidence,
    }


def apply_reusable_place_display_fields(
    details: Any,
    existing_entry: EnrichmentCacheEntry | None,
) -> None:
    if reusable_place_display_fields is None or existing_entry is None:
        return
    reusable = reusable_place_display_fields(
        place_details_display_field_map(details),
        enrichment_place_display_field_map(existing_entry.place),
    )
    for key, value in reusable.items():
        setattr(details, key, value)


def place_details_need_display_translation(details: Any) -> bool:
    if needs_display_en is None:
        return False
    return (
        bool(needs_display_en(as_string(getattr(details, "address", None))))
        and as_string(getattr(details, "address_display_en", None)) is None
    ) or (
        bool(needs_display_en(as_string(getattr(details, "category", None))))
        and as_string(getattr(details, "category_display_en", None)) is None
    )


def fetch_places_enrichment(
    place: RawPlace,
    *,
    city_name: str | None = None,
    country_name: str | None = None,
    google_place_id: str | None = None,
    signature_google_place_id: str | None = None,
    api_key: str | None,
    strategy: Literal["scrape", "api", "scrape_then_api"] | None = None,
    existing_entry: EnrichmentCacheEntry | None = None,
    suppress_description: bool = False,
    allow_identity_mismatch: bool = False,
) -> EnrichmentCacheEntry:
    if strategy is None:
        strategy = google_places_enrichment_strategy()

    if strategy == "scrape":
        return fetch_place_page_enrichment(
            place,
            city_name=city_name,
            country_name=country_name,
            google_place_id=google_place_id,
            signature_google_place_id=signature_google_place_id,
            existing_entry=existing_entry,
            suppress_description=suppress_description,
            allow_identity_mismatch=allow_identity_mismatch,
        )

    if strategy == "api":
        if api_key is None:
            raise RuntimeError(
                "GOOGLE_PLACES_API_KEY is required when GOOGLE_PLACES_ENRICHMENT_STRATEGY=api."
            )
        return fetch_places_api_enrichment(
            place,
            city_name=city_name,
            country_name=country_name,
            signature_google_place_id=signature_google_place_id,
            api_key=api_key,
        )

    page_entry = fetch_place_page_enrichment(
        place,
        city_name=city_name,
        country_name=country_name,
        google_place_id=google_place_id,
        signature_google_place_id=signature_google_place_id,
        existing_entry=existing_entry,
        suppress_description=suppress_description,
        allow_identity_mismatch=allow_identity_mismatch,
    )
    if page_entry.error is None and page_entry.place is not None:
        if api_key is None or not should_fallback_to_places_api(page_entry):
            return page_entry

    if api_key is not None:
        api_entry = fetch_places_api_enrichment(
            place,
            city_name=city_name,
            country_name=country_name,
            signature_google_place_id=signature_google_place_id,
            api_key=api_key,
        )
        if (
            page_entry.error is None
            and page_entry.place is not None
            and (api_entry.error is not None or api_entry.matched is not True or api_entry.place is None)
        ):
            return page_entry
        if page_entry.error is None and page_entry.place is not None and api_entry.place is not None:
            return merge_page_place_into_api_entry(page_entry, api_entry)
        return api_entry

    return page_entry


def fetch_place_page_enrichment(
    place: RawPlace,
    *,
    city_name: str | None = None,
    country_name: str | None = None,
    google_place_id: str | None = None,
    signature_google_place_id: str | None = None,
    existing_entry: EnrichmentCacheEntry | None = None,
    suppress_description: bool = False,
    allow_identity_mismatch: bool = False,
) -> EnrichmentCacheEntry:
    query = build_place_page_search_query(
        place,
        city_name=city_name,
        country_name=country_name,
    )
    if scrape_place is None:
        return build_cache_entry(
            place,
            source="google_maps_page",
            query=query,
            city_name=city_name,
            country_name=country_name,
            signature_google_place_id=signature_google_place_id,
            error="gmaps_scraper_unavailable",
        )

    proxy = current_scraper_proxy()
    session_state, browser_session, http_session = build_scraper_sessions(
        proxy,
        session_scope=scraper_place_session_scope(
            city_name=city_name,
            country_name=country_name,
        ),
    )
    llm_mode = google_maps_place_llm_repair_mode()
    llm_repairer = None if llm_mode == "off" else build_place_llm_repairer()
    collect_reviews = google_maps_place_collect_reviews()
    collect_about = google_maps_place_collect_about()

    def scrape_for_enrichment(scrape_url: str, *, llm_tasks: Sequence[Any]) -> Any:
        return scrape_place(
            scrape_url,
            headless=True,
            browser_session=browser_session,
            http_session=http_session,
            llm_fallback=llm_repairer,
            llm_tasks=llm_tasks,
            collect_reviews=collect_reviews,
            collect_about=collect_about,
        )

    try:
        last_error: str | None = None
        saw_non_error_result = False
        deferred_matched_entry: EnrichmentCacheEntry | None = None
        retry_urls: list[str] = []
        candidate_urls = build_place_page_candidate_urls(
            place,
            city_name=city_name,
            country_name=country_name,
            google_place_id=google_place_id,
        )

        def defer_sparse_retry_candidate(
            details: Any,
            enrichment_place: EnrichmentPlace,
            scrape_url: str,
        ) -> None:
            nonlocal deferred_matched_entry
            if not place_page_candidate_is_confident_match(place, details, enrichment_place):
                return
            deferred_matched_entry = build_cache_entry(
                place,
                source="google_maps_page",
                query=query,
                city_name=city_name,
                country_name=country_name,
                signature_google_place_id=signature_google_place_id,
                matched=True,
                score=STRONG_MATCH_SCORE,
                enrichment_place=enrichment_place,
            )
            retry_urls.append(scrape_url)

        def prepare_details_for_normalization(details: Any) -> Any:
            nonlocal last_error
            apply_reusable_place_display_fields(details, existing_entry)
            if (
                llm_mode == "dom_then_translation"
                and llm_repairer is not None
                and repair_place_display_fields is not None
                and place_details_need_display_translation(details)
            ):
                try:
                    return repair_place_display_fields(
                        details,
                        repairer=llm_repairer,
                        evidence={
                            "city": city_name,
                            "country": country_name,
                            "query": query,
                        },
                    )
                except Exception as exc:
                    last_error = f"display_repair_error:{exc}"
            return details

        for index, scrape_url in enumerate(candidate_urls):
            try:
                details = scrape_for_enrichment(scrape_url, llm_tasks=("dom_repair",))
            except RECOVERABLE_REFRESH_ERRORS as exc:
                if should_reset_scraper_session(exc):
                    clear_scraper_session_state(session_state)
                    browser_session, http_session = build_scraper_configs(session_state, proxy)
                    try:
                        details = scrape_for_enrichment(scrape_url, llm_tasks=("dom_repair",))
                    except RECOVERABLE_REFRESH_ERRORS as retry_exc:
                        last_error = f"scrape_error:{retry_exc}"
                        continue
                else:
                    last_error = f"scrape_error:{exc}"
                    continue

            saw_non_error_result = True
            record_scraper_session_use(session_state, proxy=proxy)
            details = prepare_details_for_normalization(details)
            enrichment_place = normalize_place_page_enrichment(details)
            should_retry = should_retry_limited_place_page_result(details, enrichment_place)
            source_url = as_string(getattr(details, "source_url", None)) or enrichment_place.google_maps_uri
            if source_url and "/maps/search/" in source_url and not enrichment_place.formatted_address:
                usable_without_address = place_page_search_result_without_address_is_usable(
                    place,
                    details,
                    enrichment_place,
                )
                search_result_url = localize_google_maps_scrape_url(
                    as_string(getattr(details, "search_result_url", None)) or ""
                )
                if search_result_url and search_result_url not in candidate_urls:
                    if usable_without_address:
                        deferred_matched_entry = build_cache_entry(
                            place,
                            source="google_maps_page",
                            query=query,
                            city_name=city_name,
                            country_name=country_name,
                            signature_google_place_id=signature_google_place_id,
                            matched=True,
                            score=STRONG_MATCH_SCORE,
                            enrichment_place=enrichment_place,
                        )
                    candidate_urls.insert(index + 1, search_result_url)
                    continue
                if index + 1 < len(candidate_urls):
                    if usable_without_address:
                        deferred_matched_entry = build_cache_entry(
                            place,
                            source="google_maps_page",
                            query=query,
                            city_name=city_name,
                            country_name=country_name,
                            signature_google_place_id=signature_google_place_id,
                            matched=True,
                            score=STRONG_MATCH_SCORE,
                            enrichment_place=enrichment_place,
                        )
                    elif should_retry:
                        defer_sparse_retry_candidate(details, enrichment_place, scrape_url)
                    continue
                if not usable_without_address:
                    if should_retry:
                        defer_sparse_retry_candidate(details, enrichment_place, scrape_url)
                    continue
            matched = place_page_has_meaningful_enrichment(details, enrichment_place)
            if not matched:
                if should_retry:
                    defer_sparse_retry_candidate(details, enrichment_place, scrape_url)
                continue
            if matched and not place_page_candidate_is_confident_match(place, details, enrichment_place):
                continue
            deferred_entry = build_cache_entry(
                place,
                source="google_maps_page",
                query=query,
                city_name=city_name,
                country_name=country_name,
                signature_google_place_id=signature_google_place_id,
                matched=True,
                score=STRONG_MATCH_SCORE,
                enrichment_place=enrichment_place,
            )
            search_result_url = localize_google_maps_scrape_url(
                as_string(getattr(details, "search_result_url", None)) or ""
            )
            if (
                search_result_url
                and search_result_url not in candidate_urls
                and not enrichment_place.description
            ):
                candidate_urls.insert(index + 1, search_result_url)
                deferred_matched_entry = deferred_entry
                continue
            if (
                as_string(getattr(details, "source_url", None))
                and "/maps/search/" in as_string(getattr(details, "source_url", None))
                and not enrichment_place.description
                and index + 1 < len(candidate_urls)
                and "/maps/place/" in candidate_urls[index + 1]
            ):
                deferred_matched_entry = deferred_entry
                continue
            if not allow_identity_mismatch:
                suppress_uncertain_place_page_identity_fields(place, enrichment_place)
            apply_semantic_enrichment(
                enrichment_place,
                raw_place=place,
                city_name=city_name,
                country_name=country_name,
                existing_entry=existing_entry,
                raw_note=place.note,
                suppress_description=suppress_description,
            )
            return deferred_entry
        if retry_urls:
            clear_scraper_session_state(session_state)
            browser_session, http_session = build_scraper_configs(session_state, proxy)
            for scrape_url in dedupe_urls(retry_urls):
                try:
                    details = scrape_for_enrichment(scrape_url, llm_tasks=("dom_repair",))
                except RECOVERABLE_REFRESH_ERRORS as exc:
                    last_error = f"scrape_error:{exc}"
                    continue
                saw_non_error_result = True
                record_scraper_session_use(session_state, proxy=proxy)
                details = prepare_details_for_normalization(details)
                enrichment_place = normalize_place_page_enrichment(details)
                if should_retry_limited_place_page_result(details, enrichment_place):
                    continue
                source_url = as_string(getattr(details, "source_url", None)) or enrichment_place.google_maps_uri
                if (
                    source_url
                    and "/maps/search/" in source_url
                    and not enrichment_place.formatted_address
                    and not place_page_search_result_without_address_is_usable(place, details, enrichment_place)
                ):
                    continue
                matched = place_page_has_meaningful_enrichment(details, enrichment_place)
                if matched and not place_page_candidate_is_confident_match(place, details, enrichment_place):
                    continue
                if not matched:
                    continue
                if not allow_identity_mismatch:
                    suppress_uncertain_place_page_identity_fields(place, enrichment_place)
                apply_semantic_enrichment(
                    enrichment_place,
                    raw_place=place,
                    city_name=city_name,
                    country_name=country_name,
                    existing_entry=existing_entry,
                    raw_note=place.note,
                    suppress_description=suppress_description,
                )
                return build_cache_entry(
                    place,
                    source="google_maps_page",
                    query=query,
                    city_name=city_name,
                    country_name=country_name,
                    signature_google_place_id=signature_google_place_id,
                    matched=True,
                    score=STRONG_MATCH_SCORE,
                    enrichment_place=enrichment_place,
                )
        if deferred_matched_entry is not None and deferred_matched_entry.place is not None:
            if not allow_identity_mismatch:
                suppress_uncertain_place_page_identity_fields(place, deferred_matched_entry.place)
            apply_semantic_enrichment(
                deferred_matched_entry.place,
                raw_place=place,
                city_name=city_name,
                country_name=country_name,
                existing_entry=existing_entry,
                raw_note=place.note,
                suppress_description=suppress_description,
            )
            return deferred_matched_entry
        if saw_non_error_result:
            return build_cache_entry(
                place,
                source="google_maps_page",
                query=query,
                city_name=city_name,
                country_name=country_name,
                signature_google_place_id=signature_google_place_id,
                matched=False,
            )
        if last_error is not None:
            return build_cache_entry(
                place,
                source="google_maps_page",
                query=query,
                city_name=city_name,
                country_name=country_name,
                signature_google_place_id=signature_google_place_id,
                error=last_error,
            )
        return build_cache_entry(
            place,
            source="google_maps_page",
            query=query,
            city_name=city_name,
            country_name=country_name,
            signature_google_place_id=signature_google_place_id,
            matched=False,
        )
    finally:
        release_scraper_session_lock(session_state)


def should_fallback_to_places_api(entry: EnrichmentCacheEntry) -> bool:
    place = entry.place
    if place is None:
        return True
    if entry.source != "google_maps_page":
        return False
    if place.limited_view:
        return True
    is_search_result = bool(place.google_maps_uri and "/maps/search/" in place.google_maps_uri)
    has_reputation = place.rating is not None or place.user_rating_count is not None
    has_contact = bool(place.website or place.phone)
    if is_search_result and not has_reputation and not has_contact:
        return True
    if place.business_status is None and place.rating is None and place.user_rating_count is None:
        if not place.primary_type_display_name:
            return True
    return False


def place_page_has_meaningful_enrichment(
    details: Any,
    enrichment_place: EnrichmentPlace,
) -> bool:
    has_identity = bool(enrichment_place.display_name and enrichment_place.formatted_address)
    has_reputation = (
        enrichment_place.rating is not None
        or enrichment_place.user_rating_count is not None
    )
    has_contact_or_context = bool(
        enrichment_place.primary_type_display_name
        or enrichment_place.website
        or enrichment_place.phone
        or enrichment_place.plus_code
        or enrichment_place.description
    )
    if should_retry_limited_place_page_result(details, enrichment_place):
        return False
    if has_identity and (has_reputation or has_contact_or_context):
        return True
    if (
        enrichment_place.display_name
        and has_contact_or_context
        and not bool(getattr(details, "limited_view", False))
    ):
        return True
    return not bool(getattr(details, "limited_view", False)) and bool(
        has_reputation and enrichment_place.formatted_address
    )


def should_retry_limited_place_page_result(
    details: Any,
    enrichment_place: EnrichmentPlace,
) -> bool:
    if not bool(getattr(details, "limited_view", False)):
        return False
    return enrichment_place.user_rating_count is None


def place_page_search_result_without_address_is_usable(
    raw_place: RawPlace,
    details: Any,
    enrichment_place: EnrichmentPlace,
) -> bool:
    return (
        place_page_has_meaningful_enrichment(details, enrichment_place)
        and place_page_candidate_is_confident_match(raw_place, details, enrichment_place)
        and place_page_search_result_without_address_has_location_evidence(raw_place, details, enrichment_place)
    )


def place_page_search_result_without_address_has_location_evidence(
    raw_place: RawPlace,
    details: Any,
    enrichment_place: EnrichmentPlace,
) -> bool:
    _ = enrichment_place
    distance_m = place_page_candidate_coordinate_distance_meters(raw_place, details)
    if distance_m is None:
        return False
    return distance_m <= PLACE_PAGE_ADDRESSLESS_SEARCH_COORDINATE_MATCH_METERS


def place_page_candidate_is_confident_match(
    raw_place: RawPlace,
    details: Any,
    enrichment_place: EnrichmentPlace,
) -> bool:
    distance_m = place_page_candidate_coordinate_distance_meters(raw_place, details)
    if (
        distance_m is not None
        and distance_m > PLACE_PAGE_COORDINATE_MISMATCH_REJECT_METERS
    ):
        return False

    source_url = as_string(getattr(details, "source_url", None)) or enrichment_place.google_maps_uri
    if not source_url or "/maps/search/" not in source_url:
        return True
    score = score_place_page_candidate(raw_place, details, enrichment_place)
    if enrichment_place.formatted_address is None:
        raw_name = normalize_text(raw_place.name)
        candidate_name = normalize_text(enrichment_place.display_name)
        if (
            raw_name
            and candidate_name
            and raw_name != candidate_name
            and place_page_candidate_name_is_address_artifact(raw_place, enrichment_place.display_name)
        ):
            return False
        if raw_name and candidate_name and raw_name != candidate_name:
            return score >= 60
    return score >= 25


def suppress_uncertain_place_page_identity_fields(
    raw_place: RawPlace,
    enrichment_place: EnrichmentPlace,
) -> list[str]:
    if enrichment_identity_is_compatible_with_raw(raw_place, enrichment_place):
        return []

    suppressed_fields: list[str] = []
    for field_name in (
        "display_name",
        "google_place_id",
        "google_place_resource_name",
        "website",
        "phone",
    ):
        if as_string(getattr(enrichment_place, field_name)) is not None:
            setattr(enrichment_place, field_name, None)
            suppressed_fields.append(field_name)
    if enrichment_place.reservation_links:
        enrichment_place.reservation_links = []
        suppressed_fields.append("reservation_links")

    if resolved_google_maps_url_is_place_page(enrichment_place.google_maps_uri):
        enrichment_place.google_maps_uri = None
        suppressed_fields.append("google_maps_uri")
    if resolved_google_maps_url_is_place_page(enrichment_place.search_result_url):
        enrichment_place.search_result_url = None
        suppressed_fields.append("search_result_url")

    return suppressed_fields


def enrichment_identity_is_compatible_with_raw(
    raw_place: RawPlace,
    enrichment_place: EnrichmentPlace,
) -> bool:
    raw_name = as_string(raw_place.name)
    if raw_name is None:
        return True

    identity_names = [
        as_string(enrichment_place.display_name),
        name_from_maps_url(enrichment_place.google_maps_uri or ""),
        name_from_maps_url(enrichment_place.search_result_url or ""),
    ]
    return all(
        raw_place_names_are_compatible(raw_name, identity_name)
        for identity_name in identity_names
        if identity_name is not None
    )


def place_page_candidate_name_is_address_artifact(
    raw_place: RawPlace,
    candidate_name: str | None,
) -> bool:
    candidate = normalize_text(candidate_name)
    if not candidate:
        return False
    raw_address = normalize_text(raw_place.address)
    if raw_address and (candidate in raw_address or raw_address in candidate):
        return True
    return bool(candidate_name and re.search(r"\d", candidate_name) and is_street_or_block_part(candidate_name))


def place_page_candidate_coordinate_distance_meters(
    raw_place: RawPlace,
    details: Any,
) -> float | None:
    raw_lat = raw_place.lat
    raw_lng = raw_place.lng
    candidate_lat = as_float(getattr(details, "lat", None))
    candidate_lng = as_float(getattr(details, "lng", None))
    if raw_lat is None or raw_lng is None or candidate_lat is None or candidate_lng is None:
        return None
    return haversine_meters(raw_lat, raw_lng, candidate_lat, candidate_lng)


def score_place_page_candidate(
    raw_place: RawPlace,
    details: Any,
    enrichment_place: EnrichmentPlace,
) -> int:
    score = 0
    raw_name = normalize_text(raw_place.name)
    candidate_name = normalize_text(enrichment_place.display_name)
    raw_address = normalize_text(raw_place.address)
    candidate_address = normalize_text(
        enrichment_place.formatted_address or as_string(getattr(details, "address", None))
    )

    if raw_name and candidate_name:
        if raw_name == candidate_name:
            score += 50
        elif raw_name in candidate_name or candidate_name in raw_name:
            score += 30
        else:
            score += token_overlap_score(raw_name, candidate_name)

    if raw_address and candidate_address:
        score += token_overlap_score(raw_address, candidate_address)

    distance_m = place_page_candidate_coordinate_distance_meters(raw_place, details)
    if distance_m is not None:
        if distance_m <= 100:
            score += 40
        elif distance_m <= 400:
            score += 25
        elif distance_m <= 1200:
            score += 10

    return score


def build_place_page_candidate_urls(
    place: RawPlace,
    *,
    city_name: str | None = None,
    country_name: str | None = None,
    google_place_id: str | None = None,
) -> list[str]:
    maps_url = as_string(place.maps_url)
    if maps_url is None:
        return []

    query = build_place_page_search_query(
        place,
        city_name=city_name,
        country_name=country_name,
    )
    search_url = build_google_maps_scrape_search_url(query) if query else None
    place_id_search_url = (
        build_google_maps_scrape_search_url(query, google_place_id=google_place_id)
        if query and google_place_id
        else None
    )
    localized_maps_url = localize_google_maps_scrape_url(maps_url)
    cid = as_string(place.cid) or extract_maps_cid(maps_url)
    cid_url = localize_google_maps_scrape_url(f"https://maps.google.com/?cid={cid}") if cid else None
    priority_candidates = [place_id_search_url] if place_id_search_url is not None else []

    if should_replace_raw_search_candidates_with_locality_bias(
        place,
        localized_maps_url=localized_maps_url,
        search_url=search_url,
    ):
        candidates = [cid_url, search_url, localized_maps_url]
        return dedupe_urls([*priority_candidates, *[url for url in candidates if url is not None]])

    candidates: list[str] = []
    if should_prefer_search_place_url(maps_url) and search_url is not None:
        candidates.append(search_url)
    candidates.append(localized_maps_url)
    if cid_url is not None:
        candidates.append(cid_url)
    if not should_prefer_search_place_url(maps_url) and search_url is not None:
        candidates.append(search_url)
    return dedupe_urls([*priority_candidates, *candidates])


def localize_google_maps_scrape_url(url: str) -> str:
    if localize_scraper_maps_url is not None:
        return localize_scraper_maps_url(url, hl="en", gl="us")

    split = urlsplit(url)
    host = split.netloc.lower()
    if "google." not in host:
        return url

    query_pairs = [(key, value) for key, value in parse_qsl(split.query, keep_blank_values=True) if key not in {"hl", "gl"}]
    query_pairs.extend([("hl", "en"), ("gl", "us")])
    return urlunsplit(split._replace(query=urlencode(query_pairs)))


def build_google_maps_scrape_search_url(
    query: str,
    *,
    google_place_id: str | None = None,
) -> str:
    if build_scraper_maps_search_url is not None:
        return build_scraper_maps_search_url(
            query,
            place_id=google_place_id,
            hl="en",
            gl="us",
        )
    return localize_google_maps_scrape_url(
        build_google_maps_search_url(query, google_place_id=google_place_id)
    )


def should_prefer_search_place_url(maps_url: str) -> bool:
    normalized = maps_url.strip().lower()
    if "/maps/place/" in normalized or "/maps/search/" in normalized:
        return False
    return "?cid=" in normalized or "?q=" in normalized or "&cid=" in normalized or "&q=" in normalized


def dedupe_urls(urls: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        deduped.append(url)
        seen.add(url)
    return deduped


def build_place_page_search_query(
    place: RawPlace,
    *,
    city_name: str | None = None,
    country_name: str | None = None,
) -> str:
    query = build_text_query(place)
    if not query:
        return query

    if place.address:
        return query

    locality_parts = [
        part.strip()
        for part in (city_name, country_name)
        if isinstance(part, str) and part.strip()
    ]
    if locality_parts:
        deduped_parts = list(dict.fromkeys(locality_parts))
        return f"{query}, {', '.join(deduped_parts)}"

    return query


def should_replace_raw_search_candidates_with_locality_bias(
    place: RawPlace,
    *,
    localized_maps_url: str,
    search_url: str | None,
) -> bool:
    if search_url is None:
        return False
    if place.address:
        return False
    if "/maps/search/" not in localized_maps_url:
        return False
    return search_url != localized_maps_url


PLACE_PAGE_ADDRESS_REJECT_SUBSTRINGS = (
    "about this data",
    "faviconv2",
    "imagery ©",
    "map data ©",
    "saved in",
    "send product feedback",
    "street view",
    "termsprivacy",
)
PLACE_PAGE_ADDRESS_REJECT_HOST_FRAGMENTS = ("gstatic.com", "googleusercontent.com")
PLACE_PAGE_ADDRESS_ENTITY_TOKEN_RE = re.compile(r"^/(?:g|m)/[A-Za-z0-9_-]+$")
PLACE_PAGE_URL_LIKE_RE = re.compile(
    r"(?:https?://|www\.|/(?:search|maps|url|local)(?:[/?#]|$))",
    re.IGNORECASE,
)
PLACE_PAGE_TRAVEL_PRODUCT_ADDRESS_RE = re.compile(r"\b\d+\s*[-–]\s*day\b", re.IGNORECASE)
PLACE_PAGE_LOCALITY_ABBREVIATION_PERIOD_RE = re.compile(r"(?:\bSt\.|\b[A-Z]\.(?:[A-Z]\.)+)")
PLACE_PAGE_REGION_CODE_RE = re.compile(r"[A-Z]{2,3}")
PLACE_PAGE_LOCALITY_ADDRESS_REJECT_VALUES = {
    "art gallery",
    "bakery",
    "bar",
    "cafe",
    "coffee shop",
    "curbside pickup",
    "delivery",
    "dessert shop",
    "dine-in",
    "dine in",
    "drive-through",
    "drive through",
    "hotel",
    "ice cream shop",
    "kerbside pickup",
    "museum",
    "no-contact delivery",
    "outdoor seating",
    "reservations",
    "restaurant",
    "shop",
    "shopping mall",
    "store",
    "takeaway",
    "takeout",
    "tourist attraction",
    "wheelchair accessible entrance",
}
PLACE_PAGE_PROSE_TERM_RE = re.compile(
    r"\b(?:best|good|great|delicious|dropped|experience|lunch|dinner|"
    r"burger|burgers|coffee|food|friendly|nugget|nuggets|owner|recommend|recommended|session)\b",
    re.IGNORECASE,
)
PLACE_PAGE_PLUS_CODE_RE = re.compile(
    r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}"
    r"(?:\s+[^\n]+)?\b"
)
PLACE_PAGE_PHONE_RE = re.compile(r"^\+?[0-9][0-9()\-\s]{7,}$")
PLACE_PAGE_POSTAL_CODE_RE = re.compile(
    r"\b(?:\d{5}(?:-\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})\b",
    re.IGNORECASE,
)
PLACE_PAGE_ADDRESS_KEYWORD_RE = re.compile(
    r"\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|place|pl|"
    r"court|ct|square|sq|suite|ste|unit|floor|fl|plaza|parkway|pkwy|highway|hwy)\b",
    re.IGNORECASE,
)
PLACE_PAGE_STRONG_ADDRESS_KEYWORD_RE = re.compile(
    r"\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|"
    r"square|sq|suite|ste|unit|floor|fl|plaza|parkway|pkwy|highway|hwy)\b",
    re.IGNORECASE,
)
PLACE_PAGE_DISPLAY_NAME_REJECT_VALUES = {
    "call",
    "directions",
    "save",
    "saved",
    "share",
    "sponsored",
    "website",
}


def sanitize_place_page_display_name(value: Any) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    normalized_lookup = normalize_text(normalized)
    if normalized_lookup in PLACE_PAGE_DISPLAY_NAME_REJECT_VALUES:
        return None
    if normalized_lookup.startswith("sponsored "):
        return None
    return normalized


PLACE_PAGE_FIRST_PERSON_DESCRIPTION_PRONOUN_RE = re.compile(
    r"\b(?:[Ii]|[Ii]['’](?:m|d|ve)|[Mm]y|[Mm]e|[Ww]e|[Ww]e['’](?:re|d|ve)|[Oo]ur|[Uu]s)\b",
)
PLACE_PAGE_FIRST_PERSON_DESCRIPTION_MARKERS = (
    "arrived",
    "attended",
    "came",
    "chose",
    "cost",
    "enjoyed",
    "felt",
    "found",
    "hit",
    "ordered",
    "return",
    "returned",
    "saw",
    "seen",
    "spent",
    "stopped by",
    "think",
    "tried",
    "visited",
    "went",
)
PLACE_PAGE_REVIEW_DESCRIPTION_MARKERS = (
    "best place to stay",
    "boy was it worth",
    "definitely recommend this place",
    "great experience overall",
    "had a great time",
    "hidden gem-literally",
    "highly recommended",
    "i forgot his name",
    "i'd just finished",
    "i've tasted",
    "i’d just finished",
    "i’ve tasted",
    "it was my first attempt",
    "my stay in",
    "once step in",
    "offered great recommendation",
    "omfg",
    "overrated",
    "so yummy",
    "the katsu burger",
    "the rooms were huge",
    "we have ever had",
    "we've ever had",
    "what a great hotel",
    "would recommend to everyone",
    "about this data",
    "get the most out of google maps",
    "your children",
    "your kids",
    "you should",
)


def phrase_marker_pattern(marker: str) -> str:
    return re.escape(marker).replace(r"\ ", r"\s+")


PLACE_PAGE_REVIEW_DESCRIPTION_MARKER_RE = re.compile(
    "|".join(
        rf"(?<![\w-]){phrase_marker_pattern(marker)}(?![\w-])"
        for marker in PLACE_PAGE_REVIEW_DESCRIPTION_MARKERS
    ),
    re.IGNORECASE,
)
PLACE_PAGE_FIRST_PERSON_DESCRIPTION_MARKER_RE = re.compile(
    "|".join(
        rf"(?<![\w-]){phrase_marker_pattern(marker)}(?![\w-])"
        for marker in PLACE_PAGE_FIRST_PERSON_DESCRIPTION_MARKERS
    ),
    re.IGNORECASE,
)
PLACE_PAGE_UI_ACTION_CLUSTER_LABEL_PATTERN = (
    r"(?:call|directions|save|saved|nearby|send to phone|share|website|sign in)"
)
PLACE_PAGE_UI_ACTION_CLUSTER_RE = re.compile(
    rf"^{PLACE_PAGE_UI_ACTION_CLUSTER_LABEL_PATTERN}"
    rf"(?:\s+{PLACE_PAGE_UI_ACTION_CLUSTER_LABEL_PATTERN}){{2,}}$",
    re.IGNORECASE,
)


def sanitize_place_page_description(value: Any) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    if looks_like_place_page_ui_action_cluster(normalized):
        return None
    if looks_like_place_page_review_description(normalized):
        return None
    if looks_like_place_page_first_person_description(normalized):
        return None
    return normalized


def looks_like_place_page_ui_action_cluster(value: str) -> bool:
    normalized = re.sub(r"[\ue000-\uf8ff]", " ", value)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    return PLACE_PAGE_UI_ACTION_CLUSTER_RE.fullmatch(normalized) is not None


def looks_like_place_page_review_description(value: str) -> bool:
    return PLACE_PAGE_REVIEW_DESCRIPTION_MARKER_RE.search(value) is not None


def looks_like_place_page_first_person_description(value: str) -> bool:
    if PLACE_PAGE_FIRST_PERSON_DESCRIPTION_PRONOUN_RE.search(value) is None:
        return False
    return PLACE_PAGE_FIRST_PERSON_DESCRIPTION_MARKER_RE.search(value) is not None


def sanitize_place_page_formatted_address(value: Any) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None

    lowered = normalized.lower()
    if PLACE_PAGE_URL_LIKE_RE.search(normalized) is not None:
        return None
    if any(fragment in lowered for fragment in PLACE_PAGE_ADDRESS_REJECT_SUBSTRINGS):
        return None
    if any(fragment in lowered for fragment in PLACE_PAGE_ADDRESS_REJECT_HOST_FRAGMENTS):
        return None
    if PLACE_PAGE_TRAVEL_PRODUCT_ADDRESS_RE.search(normalized) is not None:
        return None
    if looks_like_place_page_review_snippet(normalized):
        return None
    if PLACE_PAGE_ADDRESS_ENTITY_TOKEN_RE.fullmatch(normalized):
        return None
    if re.search(r"\d", normalized) and re.search(r"\breviews?\b", lowered):
        return None
    if normalized.endswith(" More"):
        return None
    if (
        normalized.endswith(".")
        and re.search(r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b", normalized) is None
        and not looks_like_place_page_locality_address(normalized)
    ):
        return None

    if "·" in normalized:
        segments = [segment.strip() for segment in normalized.split("·") if segment.strip()]
        for candidate in reversed(segments):
            cleaned = sanitize_place_page_formatted_address(candidate)
            if cleaned is not None and cleaned != normalized:
                return cleaned
        return None

    return normalized if looks_like_place_page_formatted_address(normalized) else None


def normalize_guide_display_address(value: Any, *, country_name: str | None) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    country_key = normalize_locality_key(country_name)
    if country_key == "taiwan" or re.search(r"\bTaiwan\b|台灣|台湾", normalized, flags=re.IGNORECASE):
        normalized = normalize_taiwan_guide_display_address(normalized)
    return re.sub(r"\s+", " ", normalized).strip() or None


def normalize_taiwan_guide_display_address(address: str) -> str:
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) >= 3 and re.fullmatch(r"\d{3}", parts[0]) and is_taiwan_country_token(parts[1]):
        postal_code = parts[0]
        parts = [
            normalize_taiwan_street_number_part(part)
            for part in [*reversed(parts[2:]), f"Taiwan {postal_code}"]
        ]
    else:
        parts = [normalize_taiwan_street_number_part(part) for part in parts]
    return ", ".join(part for part in parts if part)


def is_taiwan_country_token(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalize_locality_key(normalized) == "taiwan" or normalized in {"台灣", "台湾"}


def normalize_taiwan_street_number_part(part: str) -> str:
    normalized = re.sub(r"\s+", " ", part).strip()
    duplicate_match = re.fullmatch(
        r"No\.?\s*(?P<number>\d+[A-Za-z-]*)\s+No\.?\s*(?P=number)號(?P<suffix>[A-Za-z0-9-]*)",
        normalized,
        flags=re.IGNORECASE,
    )
    if duplicate_match:
        return format_taiwan_street_number(
            duplicate_match.group("number"),
            duplicate_match.group("suffix"),
        )
    number_match = re.fullmatch(
        r"(?:No\.?\s*)?(?P<number>\d+[A-Za-z-]*)號(?P<suffix>[A-Za-z0-9-]*)",
        normalized,
        flags=re.IGNORECASE,
    )
    if number_match:
        return format_taiwan_street_number(number_match.group("number"), number_match.group("suffix"))
    return normalized


def format_taiwan_street_number(number: str, suffix: str | None = None) -> str:
    normalized_number = number.strip()
    normalized_suffix = (suffix or "").strip()
    if normalized_suffix:
        return f"No. {normalized_number}, {normalized_suffix}"
    return f"No. {normalized_number}"


def looks_like_place_page_review_snippet(value: str) -> bool:
    if has_place_page_address_marker(value):
        return False
    if value.endswith(" More"):
        return True
    terms = PLACE_PAGE_PROSE_TERM_RE.findall(value)
    word_count = len(value.split())
    if "," in value and len(terms) >= 2:
        return True
    if word_count >= 10 and len(terms) >= 2:
        return True
    if word_count >= 10 and re.search(r"[.!?]", value) and terms:
        return True
    return False


def sanitize_place_page_phone(value: Any) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    if PLACE_PAGE_PHONE_RE.fullmatch(normalized) is None:
        return None
    digit_count = sum(character.isdigit() for character in normalized)
    if digit_count < 8 or digit_count > 15:
        return None
    if normalized.isdigit() and digit_count == 13 and normalized.startswith("17"):
        return None
    return normalized


def sanitize_place_page_plus_code(value: Any) -> str | None:
    normalized = as_string(value)
    if normalized is None:
        return None
    match = PLACE_PAGE_PLUS_CODE_RE.search(normalized)
    if match is None:
        return None
    return match.group(0).strip()


def looks_like_place_page_formatted_address(value: str) -> bool:
    if len(value) < 5:
        return False
    if PLACE_PAGE_PLUS_CODE_RE.search(value) is not None:
        return True
    if "〒" in value or value.startswith("Japan, "):
        return True
    if PLACE_PAGE_POSTAL_CODE_RE.search(value) is not None and any(character.isalpha() for character in value):
        return True

    has_digits = re.search(r"\d", value) is not None
    has_letters = any(character.isalpha() for character in value)
    if has_digits and has_letters and ("," in value or PLACE_PAGE_ADDRESS_KEYWORD_RE.search(value)):
        if "," in value and PLACE_PAGE_ADDRESS_KEYWORD_RE.search(value) is None and len(value.split()) > 10:
            return False
        return True
    if PLACE_PAGE_ADDRESS_KEYWORD_RE.search(value) and len(value.split()) <= 5:
        return True

    return looks_like_place_page_locality_address(value)


def looks_like_place_page_locality_address(value: str) -> bool:
    """Accept locality-only addresses only when they have a geographic anchor.

    The scraper uses structured Google Maps address rows first. This sanitizer
    also sees legacy/plain-text payloads, so locality-only fallbacks must be
    narrower than "short comma-separated phrase"; otherwise review snippets and
    service-option rows can become cached addresses.
    """
    if re.search(r"[!?]", value):
        return False
    locality_separator = " - " if " - " in value else ","
    locality_parts = [part.strip() for part in value.split(locality_separator) if part.strip()]
    max_words = 12 if locality_separator == " - " else 8
    if sum(len(part.split()) for part in locality_parts) > max_words:
        return False
    max_parts = 6 if locality_separator == " - " else 4
    if not 2 <= len(locality_parts) <= max_parts:
        return False
    if not all(place_page_locality_part_allows_period(part) for part in locality_parts):
        return False
    reject_keys = {
        key
        for part in locality_parts
        if (key := place_page_locality_address_reject_key(part)) in PLACE_PAGE_LOCALITY_ADDRESS_REJECT_VALUES
    }
    # A reject word can also be a real locality ("Bar, Montenegro"). Keep it
    # only when that same segment is a known geography; otherwise category rows
    # like "Museum, Montenegro" or "Hotel, Spain" are still UI text, not
    # addresses.
    geography_reject_keys = {
        place_page_locality_address_reject_key(part)
        for part in locality_parts
        if place_page_locality_address_reject_key(part) in reject_keys
        and has_place_page_locality_geo_signal_part(part)
    }
    if reject_keys - geography_reject_keys:
        return False
    if not has_place_page_locality_geo_signal(locality_parts):
        return False
    return all(any(character.isalpha() for character in part) and len(part) <= 60 for part in locality_parts)


def has_place_page_locality_geo_signal(parts: Sequence[str]) -> bool:
    """Return True when the trailing locality chain names a known geography."""
    for part in parts[1:]:
        if has_place_page_locality_geo_signal_part(part):
            return True
    return False


def has_place_page_locality_geo_signal_part(part: str) -> bool:
    if is_country_locality(part) or is_subnational_locality(part):
        return True
    if PLACE_PAGE_LOCALITY_ABBREVIATION_PERIOD_RE.fullmatch(part):
        return True
    if PLACE_PAGE_REGION_CODE_RE.fullmatch(part.strip()) and is_subnational_code_locality(part):
        return True
    # Google can still return localized country/subdivision names despite
    # hl=en. Non-ASCII trailing geography text is a safer signal than accepting
    # arbitrary ASCII prose or unknown uppercase tokens such as "XYZ".
    return any(character.isalpha() and not character.isascii() for character in part)


def place_page_locality_part_allows_period(part: str) -> bool:
    if "." not in part:
        return True
    if PLACE_PAGE_LOCALITY_ABBREVIATION_PERIOD_RE.fullmatch(part):
        return True
    return part.startswith("St. ") and len(part.split()) <= 3


def place_page_locality_address_reject_key(part: str) -> str:
    return part.casefold().strip(" .")


def has_place_page_address_marker(value: str) -> bool:
    return (
        PLACE_PAGE_PLUS_CODE_RE.search(value) is not None
        or PLACE_PAGE_POSTAL_CODE_RE.search(value) is not None
        or PLACE_PAGE_STRONG_ADDRESS_KEYWORD_RE.search(value) is not None
        or "〒" in value
        or value.startswith("Japan, ")
    )


def coerce_enrichment_address_parts(value: Any) -> AddressParts | None:
    if not isinstance(value, list):
        return None
    if len(value) < 7 or len(value) > 8:
        return None
    if not all(isinstance(item, str) for item in value[:7]):
        return None

    normalized: AddressParts = [str(item).strip() for item in value[:7]]
    if any(not item for item in normalized[:7]):
        return None

    if len(value) == 8:
        extra = value[7]
        if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
            return None
        normalized.append([str(item).strip() for item in extra if str(item).strip()])

    return normalized


def normalize_place_page_enrichment(details: Any) -> EnrichmentPlace:
    raw_category = sanitize_enrichment_primary_category(as_string(getattr(details, "category", None)))
    category_display_en = sanitize_enrichment_primary_category(
        as_string(getattr(details, "category_display_en", None))
    )
    category_basis = category_display_en or raw_category
    primary_type = None
    types: list[str] = []
    if category_basis is not None:
        primary_type, types = normalized_enrichment_type_ids(
            slugify(category_basis).replace("-", "_"),
            category_basis,
            [slugify(category_basis).replace("-", "_")],
        )
    category = canonical_primary_category_label(primary_type=primary_type, display_name=category_basis)
    category_localized = localized_primary_category_label(
        raw_display_name=raw_category,
        canonical_display_name=category,
    )

    source_url = as_string(getattr(details, "source_url", None))
    resolved_url = as_string(getattr(details, "resolved_url", None))
    from_search_url = bool(source_url and "/maps/search/" in source_url)
    display_name = sanitize_place_page_display_name(getattr(details, "name", None))
    formatted_address = sanitize_place_page_formatted_address(getattr(details, "address", None))
    address_display_en = sanitize_place_page_formatted_address(
        getattr(details, "address_display_en", None)
    )
    rating = as_float(getattr(details, "rating", None))
    user_rating_count = as_int(getattr(details, "review_count", None))
    website = as_string(getattr(details, "website", None))
    reservation_links = coerce_place_reservation_links(getattr(details, "reservation_links", None))
    phone = sanitize_place_page_phone(getattr(details, "phone", None))
    plus_code = sanitize_place_page_plus_code(getattr(details, "plus_code", None))
    if formatted_address is None and plus_code and any(
        separator in plus_code for separator in (" - ", ",", " ")
    ):
        formatted_address = sanitize_place_page_formatted_address(plus_code)
    description = as_string(getattr(details, "description", None))
    search_result_description = sanitize_place_page_description(
        getattr(details, "search_result_description", None)
    )
    search_result_url = as_string(getattr(details, "search_result_url", None))
    if formatted_address is None:
        description_address = sanitize_place_page_formatted_address(description)
        if description_address is not None:
            formatted_address = description_address
            description = None
    description = sanitize_place_page_description(description)
    main_photo_url = sanitize_place_photo_url(getattr(details, "main_photo_url", None))
    photo_url = sanitize_place_photo_url(getattr(details, "photo_url", None))
    if from_search_url and not resolved_google_maps_url_is_place_page(resolved_url):
        description = None
    limited_view = bool(getattr(details, "limited_view", False))
    has_meaningful_fields = any(
        (
            display_name,
            formatted_address,
            primary_type,
            category,
            rating is not None,
            user_rating_count is not None,
            website,
            reservation_links,
            phone,
            plus_code,
            description,
            search_result_description,
            main_photo_url,
            photo_url,
        )
    )
    maps_uri = None
    if limited_view or has_meaningful_fields:
        maps_uri = source_url if from_search_url else resolved_url or source_url
    return EnrichmentPlace(
        google_place_id=as_string(getattr(details, "google_place_id", None)),
        display_name=display_name,
        formatted_address=formatted_address,
        address_display_en=address_display_en,
        address_display_en_source=as_string(getattr(details, "address_display_en_source", None)),
        address_display_en_confidence=as_string(getattr(details, "address_display_en_confidence", None)),
        google_maps_uri=maps_uri,
        rating=rating,
        user_rating_count=user_rating_count,
        price_range=as_string(getattr(details, "price_range", None)),
        admission_price=as_string(getattr(details, "admission_price", None)),
        room_price=as_string(getattr(details, "room_price", None)),
        primary_type=primary_type,
        primary_type_display_name=category,
        primary_type_display_name_localized=category_localized,
        category_display_en=category_display_en,
        category_display_en_source=as_string(getattr(details, "category_display_en_source", None)),
        category_display_en_confidence=as_string(getattr(details, "category_display_en_confidence", None)),
        types=types,
        business_status=normalize_place_page_business_status(as_string(getattr(details, "status", None))),
        website=as_http_url(website),
        reservation_links=reservation_links,
        phone=phone,
        plus_code=plus_code,
        address_parts=coerce_enrichment_address_parts(getattr(details, "address_parts", None)),
        description=description,
        search_result_description=search_result_description,
        search_result_url=search_result_url,
        main_photo_url=main_photo_url,
        photo_url=photo_url,
        review_topics=coerce_json_object_list(getattr(details, "review_topics", None)),
        reviews=coerce_json_object_list(getattr(details, "reviews", None)),
        about_sections=coerce_json_object_list(getattr(details, "about_sections", None)),
        limited_view=limited_view,
    )


def coerce_json_object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if hasattr(item, "to_dict") and callable(item.to_dict):
            payload = item.to_dict()
        elif isinstance(item, dict):
            payload = item
        else:
            continue
        if isinstance(payload, dict):
            compact = {str(key): val for key, val in payload.items() if val not in (None, "", [])}
            if compact:
                items.append(compact)
    return items


SEMANTIC_LLM_PROMPT_VERSION = "favorite-places-semantic-v11"
SEMANTIC_ENRICHMENT_SYSTEM_PROMPT = """
You classify saved places for a travel guide. Return only compact JSON.

Neighborhood:
- Infer the guide-facing neighborhood only from address/geography evidence.
- Prefer common local area names over the smallest administrative unit.
- Return a human-facing display label, not a tag or slug.
- Use normal local display casing such as Perth CBD, Northbridge, or Margaret River.
- Preserve acronyms like CBD.
- Never return kebab-case, snake_case, all-lowercase labels, or labels with stray spaces inside words.
- For English-language guides, return the common English-facing neighborhood label when one exists.
- Use established English names rather than mechanically preserving local-language administrative labels.
- For Taiwan, village/li labels such as Kangle Village or Tonghua Village are usually too granular; prefer the district such as Zhongshan or Da'an unless the evidence explicitly names a more recognizable local area as the neighborhood.
- Do not infer Ximen, Neihu, Longshan, or similar local areas from map knowledge alone; if the address only says Wanhua District or a street/night-market name, return Wanhua.

Tags:
- Generate broad, reusable kebab-case tags, vibe_tags, and types when requested.
- Prefer 3-6 tags and 2-4 vibe_tags.

Description:
- Generate one concise, polished English place description when requested.
- Prefer a description under 180 characters.
- Use natural grammar and canonical capitalization for dish names, place types, and proper nouns when possible.
- Return null rather than generic card-metadata prose.
- Do not write descriptions whose only facts are the place name, category/type, address, city, or neighborhood, such as "X is a Y in Z."
- A description must add a distinctive, guide-useful fact grounded in the official description, raw_note, review topics, review-signal patterns, about sections, or concrete page evidence; otherwise return null.
- Treat google_maps_description and raw_note as the strongest description inputs, with raw_note as first-party saved-list context.
- Treat search_result_description as lower-priority supporting context.
- You may synthesize aggregate review-topic, review-signal, and about-section evidence into concise factual phrases such as popular dishes, format, setting, service style, or use case.
- Do not quote individual reviews.
- Do not mention reviewers, reviews, review counts, or source language such as "reviews say" or "customers praise."
- Use direct factual wording instead of review/source phrasing: write "with warm service" rather than "praised for warm service."
- Avoid hype and ranking claims such as "one of the best," "one of the most," "most photographed," "top-rated," "widely hailed," or "regarded as."
- Do not write curation-meta copy about the saved list, favorite-worthiness, surfacing, ranking, manual curation, or tourist checklists.
- Do not write accessibility or amenities boilerplate such as wheelchair accessibility, accessible facilities, accessible amenities, parking, restrooms, or service options unless the place is primarily about accessibility.
- Do not preserve individual complaints.
- Do not include one-off caveats or reviewer-style warnings such as "just expect..." or "separate charge."
- Do not repeat raw UI labels, badges, or attributes like Accessibility, Service options, wheelchair-accessible, or LGBTQ+-friendly unless they are central to the place identity.
- Do not copy raw_note verbatim; rewrite it as neutral, well-edited guide copy.
""".strip()


def apply_semantic_enrichment(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
    existing_entry: EnrichmentCacheEntry | None = None,
    raw_note: str | None = None,
    enable_semantics: bool | None = None,
    enable_description: bool | None = None,
    force_semantics: bool | None = None,
    force_description: bool | None = None,
    suppress_description: bool = False,
) -> None:
    include_semantics = google_maps_place_semantic_llm_enabled() if enable_semantics is None else enable_semantics
    include_description = (
        google_maps_place_semantic_descriptions_enabled() if enable_description is None else enable_description
    )
    force_semantics = False if force_semantics is None else force_semantics
    force_description = (
        google_maps_place_semantic_description_force_refresh()
        if force_description is None
        else force_description
    )
    requested_description = include_description
    include_description = include_description and not suppress_description
    if suppress_description and requested_description:
        enrichment_place.semantic_description = None
        enrichment_place.semantic_description_signature = None
        if not semantic_enrichment_state_is_populated(semantic_enrichment_state(enrichment_place)):
            enrichment_place.semantic_source = None
    if not include_semantics and not include_description:
        return
    description_signature = semantic_description_signature(
        enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
        country_name=country_name,
        raw_note=raw_note,
    )
    description_reused = False
    previous_place = existing_entry.place if existing_entry else None
    if (
        include_description
        and enrichment_place.semantic_description
        and enrichment_place.semantic_description_signature != description_signature
    ):
        enrichment_place.semantic_description = None
        enrichment_place.semantic_description_signature = None
        if not semantic_enrichment_state_is_populated(semantic_enrichment_state(enrichment_place)):
            enrichment_place.semantic_source = None
    if include_description and enrichment_place.semantic_description:
        accepted_description = usable_semantic_description(
            enrichment_place.semantic_description,
            enrichment_place=enrichment_place,
            raw_place=raw_place,
            city_name=city_name,
            country_name=country_name,
        )
        if accepted_description is None:
            enrichment_place.semantic_description = None
            enrichment_place.semantic_description_signature = None
            if not semantic_enrichment_state_is_populated(semantic_enrichment_state(enrichment_place)):
                enrichment_place.semantic_source = None
        elif accepted_description != enrichment_place.semantic_description:
            enrichment_place.semantic_description = accepted_description
    if (
        include_description
        and not force_description
        and previous_place is not None
        and previous_place.semantic_description
        and previous_place.semantic_description_signature == description_signature
    ):
        previous_description = usable_semantic_description(
            previous_place.semantic_description,
            enrichment_place=enrichment_place,
            raw_place=raw_place,
            city_name=city_name,
            country_name=country_name,
        )
        if previous_description is not None:
            enrichment_place.semantic_description = previous_description
            enrichment_place.semantic_description_signature = previous_place.semantic_description_signature
            enrichment_place.semantic_source = previous_place.semantic_source or "llm"
            description_reused = True
    if description_reused and not include_semantics:
        return
    repair = repair_semantic_enrichment_with_llm(
        enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
        country_name=country_name,
        raw_note=raw_note,
        include_semantics=include_semantics,
        include_description=include_description and not description_reused,
        bypass_cache=(include_semantics and force_semantics) or (include_description and force_description),
    )
    if repair is None:
        return
    if include_semantics:
        neighborhood = normalize_semantic_neighborhood_label(
            repair.get("neighborhood"),
            city_name=city_name,
            country_name=country_name,
        )
        if neighborhood is not None:
            enrichment_place.semantic_neighborhood = neighborhood
        enrichment_place.semantic_tags = normalize_semantic_tag_list(repair.get("tags"), limit=8)
        enrichment_place.semantic_vibe_tags = normalize_semantic_tag_list(repair.get("vibe_tags"), limit=8)
        enrichment_place.semantic_types = normalize_semantic_tag_list(repair.get("types"), limit=8)
    description_source: str | None = None
    if include_description and not description_reused:
        description = usable_semantic_description(
            repair.get("description"),
            enrichment_place=enrichment_place,
            raw_place=raw_place,
            city_name=city_name,
            country_name=country_name,
        )
        if description is not None:
            description_source = "llm"
            enrichment_place.semantic_description = description
            enrichment_place.semantic_description_signature = description_signature
    if any(
        (
            enrichment_place.semantic_neighborhood,
            enrichment_place.semantic_tags,
            enrichment_place.semantic_vibe_tags,
            enrichment_place.semantic_types,
            enrichment_place.semantic_description,
        )
    ):
        if (
            description_source == "fallback"
            and not enrichment_place.semantic_neighborhood
            and not enrichment_place.semantic_tags
            and not enrichment_place.semantic_vibe_tags
            and not enrichment_place.semantic_types
        ):
            enrichment_place.semantic_source = "fallback"
        else:
            enrichment_place.semantic_source = "llm"


def fallback_semantic_description(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
    city_name: str | None,
) -> str | None:
    name = semantic_place_name(enrichment_place, raw_place=raw_place)
    if name is None:
        return None
    category = as_string(enrichment_place.primary_type_display_name)
    location = fallback_semantic_description_location(
        enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
    )
    if normalize_text(location) == normalize_text(name):
        location = as_string(city_name)
    if category and location:
        description = f"{name} is {indefinite_article(category)} {category.lower()} in {location}."
    elif category:
        description = f"{name} is {indefinite_article(category)} {category.lower()}."
    elif location:
        description = f"{name} is a saved place in {location}."
    else:
        return None
    return sanitize_semantic_description(description)


SEMANTIC_NAME_SEO_MARKER_RE = re.compile(
    r"(?:\b(?:ig|ptt|dcard)\b|景點|好評|打卡|推薦|評價|必去|必吃|人氣|網美)",
    re.IGNORECASE,
)
SEMANTIC_NAME_SEO_SPLIT_RE = re.compile(r"\s*[-－–—|｜:：]\s*")


def semantic_place_name(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
) -> str | None:
    return sanitize_semantic_place_name(enrichment_place.display_name) or sanitize_semantic_place_name(raw_place.name)


def sanitize_semantic_place_name(value: Any) -> str | None:
    name = as_string(value)
    if name is None:
        return None
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return None
    parts = SEMANTIC_NAME_SEO_SPLIT_RE.split(name, maxsplit=1)
    if (
        len(parts) == 2
        and 1 < len(parts[0]) <= 40
        and SEMANTIC_NAME_SEO_MARKER_RE.search(parts[1])
    ):
        return parts[0].strip() or name
    return name


def fallback_semantic_description_location(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
    city_name: str | None,
) -> str | None:
    for locality in infer_plus_code_localities(enrichment_place.plus_code, city_name=city_name):
        locality_key = normalize_locality_key(locality)
        if (
            locality_key
            and locality_key != normalize_locality_key(city_name)
            and fallback_semantic_description_locality_is_usable(locality, city_name=city_name)
        ):
            return locality
    address = enrichment_place.address_display_en or enrichment_place.formatted_address or raw_place.address
    for locality in infer_address_localities(address, city_name=city_name):
        locality_key = normalize_locality_key(locality)
        if (
            locality_key
            and locality_key != normalize_locality_key(city_name)
            and fallback_semantic_description_locality_is_usable(locality, city_name=city_name)
        ):
            return locality
    for locality in infer_address_region_localities(address, city_name=city_name):
        locality_key = normalize_locality_key(locality)
        if (
            locality_key
            and locality_key != normalize_locality_key(city_name)
            and fallback_semantic_description_locality_is_usable(locality, city_name=city_name)
        ):
            return locality
    return as_string(city_name)


def infer_address_region_localities(address: str | None, *, city_name: str | None = None) -> list[str]:
    if address is None:
        return []
    city_key = normalize_locality_key(city_name)
    localities: list[str] = []
    for raw_part in re.split(r"[,、]", address):
        candidate = normalize_address_locality_part(
            raw_part,
            allow_explicit_subnational_label=True,
        )
        if candidate is None:
            continue
        key = normalize_locality_key(candidate)
        if not key or key == city_key:
            continue
        if is_explicit_subnational_locality_label(candidate):
            append_unique_locality(localities, candidate)
    return localities


def infer_plus_code_localities(plus_code: str | None, *, city_name: str | None) -> list[str]:
    normalized = as_string(plus_code)
    if normalized is None:
        return []
    match = re.match(r"^[A-Z0-9]{4,}\+[A-Z0-9]{2,}\s+(.+)$", normalized.strip(), flags=re.IGNORECASE)
    if match is None:
        return []
    first_part = match.group(1).split(",", 1)[0]
    locality = normalize_address_locality_part(first_part)
    return [locality] if locality else []


def fallback_semantic_description_locality_is_usable(
    locality: str,
    *,
    city_name: str | None,
) -> bool:
    normalized = " ".join(locality.split())
    if not normalized:
        return False
    if re.search(r"^\+?\s*photos?\b", normalized, flags=re.IGNORECASE):
        return False
    if re.search(r"\b(?:centrico|habitaciones?|bedrooms?|ascensor|wifi|air\s*conditioning|\bac\b)\b", normalized, flags=re.IGNORECASE):
        return False
    if normalize_locality_key(normalized) == "plattl":
        return False
    if re.search(r"\b(?:broadway|bhf|bahnhof|ufer)\b", normalized, flags=re.IGNORECASE):
        return False
    if " - " in normalized and city_name and normalize_locality_key(city_name) in normalize_locality_key(normalized):
        return False
    return True


def indefinite_article(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith(("eu", "uni", "use", "one")):
        return "a"
    if normalized.startswith(("honest", "hour", "heir")):
        return "an"
    return "an" if normalized[:1] in {"a", "e", "i", "o", "u"} else "a"


def repair_semantic_enrichment_with_llm(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
    raw_note: str | None = None,
    include_semantics: bool,
    include_description: bool,
    bypass_cache: bool = False,
) -> dict[str, Any] | None:
    if not include_semantics and not include_description:
        return None
    config = semantic_llm_config()
    if config is None:
        return None
    evidence = semantic_enrichment_evidence(
        enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
        country_name=country_name,
        raw_note=raw_note,
        include_review_signals=include_semantics or include_description,
        include_raw_note=include_description,
    )
    evidence["requested_outputs"] = {
        "semantic_tags": include_semantics,
        "description": include_description,
    }
    evidence_hash = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    cache_path = google_maps_place_semantic_cache_dir() / f"{config['namespace']}-{evidence_hash}.json"
    if not bypass_cache:
        cached = read_semantic_llm_cache(cache_path)
        if cached is not None:
            return cached

    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": [
            {
                "role": "system",
                "content": SEMANTIC_ENRICHMENT_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "schema": {
                            "neighborhood": "string or null",
                            "tags": ["major visible/search tags, kebab-case"],
                            "vibe_tags": ["atmosphere/use-case tags, kebab-case"],
                            "types": ["specific place type tags, kebab-case"],
                            "description": "one concise sentence or null",
                        },
                        "evidence": evidence,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }
    langfuse_metadata = {
        "base_url": config["base_url"],
        "cache_namespace": config["namespace"],
        "evidence_hash": evidence_hash,
        "prompt_version": SEMANTIC_LLM_PROMPT_VERSION,
        "include_semantics": include_semantics,
        "include_description": include_description,
    }
    manager, generation = open_langfuse_generation(
        name="favorite-places.semantic-enrichment",
        model=config["model"],
        input_payload=payload,
        metadata=langfuse_metadata,
    )
    exc_info: tuple[Any, Any, Any] = (None, None, None)
    try:
        try:
            request = Request(
                f"{config['base_url'].rstrip('/')}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request, timeout=45) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            exc_info = sys.exc_info()
            update_langfuse_generation(
                generation,
                metadata={**langfuse_metadata, "status": "error", "error": str(exc_info[1])},
            )
            return None

        content = extract_chat_message_content(response_payload)
        if content is None:
            update_langfuse_generation(
                generation,
                metadata={**langfuse_metadata, "status": "missing_content"},
            )
            return None
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            exc_info = sys.exc_info()
            update_langfuse_generation(
                generation,
                output=content,
                metadata={**langfuse_metadata, "status": "invalid_json"},
                usage_details=openai_usage_details(response_payload),
            )
            return None
        if not isinstance(decoded, dict):
            update_langfuse_generation(
                generation,
                output=decoded,
                metadata={**langfuse_metadata, "status": "invalid_schema"},
                usage_details=openai_usage_details(response_payload),
            )
            return None
        update_langfuse_generation(
            generation,
            output=decoded,
            metadata={**langfuse_metadata, "status": "success"},
            usage_details=openai_usage_details(response_payload),
        )
        write_semantic_llm_cache(cache_path, decoded)
        return decoded
    finally:
        current_exc_info = sys.exc_info()
        if exc_info[0] is None and current_exc_info[0] is not None:
            exc_info = current_exc_info
        close_langfuse_generation(manager, exc_info)


def semantic_enrichment_evidence(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
    raw_note: str | None = None,
    include_review_signals: bool = False,
    include_raw_note: bool = False,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "prompt_version": SEMANTIC_LLM_PROMPT_VERSION,
        "city": city_name,
        "country": country_name,
        "name": semantic_place_name(enrichment_place, raw_place=raw_place),
        "address": enrichment_place.address_display_en or enrichment_place.formatted_address or raw_place.address,
        "raw_address": enrichment_place.formatted_address or raw_place.address,
        "category": enrichment_place.primary_type_display_name,
        "category_localized": enrichment_place.primary_type_display_name_localized,
        "google_maps_description": enrichment_place.description,
        "search_result_description": enrichment_place.search_result_description,
        "types": enrichment_place.types[:8],
        "price_range": enrichment_place.price_range,
        "admission_price": enrichment_place.admission_price,
        "room_price": enrichment_place.room_price,
        "review_topics": enrichment_place.review_topics[:12],
        "about_sections": compact_about_sections(enrichment_place.about_sections),
    }
    if include_review_signals:
        evidence["review_signals"] = compact_review_signals(enrichment_place.reviews)
    if include_raw_note:
        evidence["raw_note"] = as_string(raw_note) or as_string(raw_place.note)
    return evidence


def semantic_description_signature(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
    raw_note: str | None = None,
) -> str:
    payload = {
        "prompt_version": SEMANTIC_LLM_PROMPT_VERSION,
        "city": city_name,
        "country": country_name,
        "name": semantic_signature_text(semantic_place_name(enrichment_place, raw_place=raw_place)),
        "address": semantic_signature_text(
            enrichment_place.address_display_en or enrichment_place.formatted_address or raw_place.address
        ),
        "category": semantic_signature_text(enrichment_place.primary_type_display_name),
        "google_maps_description": semantic_signature_text(enrichment_place.description),
        "search_result_description": semantic_signature_text(enrichment_place.search_result_description),
        "types": [value for value in (semantic_signature_text(item) for item in enrichment_place.types[:8]) if value],
        "price_range": semantic_signature_text(enrichment_place.price_range),
        "admission_price": semantic_signature_text(enrichment_place.admission_price),
        "room_price": semantic_signature_text(enrichment_place.room_price),
        "rating_bucket": rating_bucket(enrichment_place.rating),
        "review_count_bucket": review_count_bucket(enrichment_place.user_rating_count),
        "review_topics": compact_review_topics_for_signature(enrichment_place.review_topics),
        "about_sections": compact_about_sections_for_signature(enrichment_place.about_sections),
        "raw_note": semantic_signature_text(raw_note or raw_place.note),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def semantic_signature_text(value: Any) -> str | None:
    text = as_string(value)
    if text is None:
        return None
    normalized = unicodedata.normalize("NFKC", text).casefold()
    signature_chars: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        signature_chars.append(char if category[0] in {"L", "N"} else " ")
    compact = re.sub(r"\s+", " ", "".join(signature_chars)).strip()
    return compact or None


def rating_bucket(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 2) / 2


def review_count_bucket(value: int | None) -> str | None:
    if value is None:
        return None
    if value < 100:
        return str((value // 25) * 25)
    if value < 1000:
        return str((value // 100) * 100)
    return str((value // 1000) * 1000)


def compact_review_topics_for_signature(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for topic in topics[:12]:
        label = semantic_signature_text(topic.get("label"))
        if not label:
            continue
        count = as_int(topic.get("count"))
        compact.append({"label": label, "count_bucket": review_count_bucket(count)})
    return sorted(compact, key=lambda item: item["label"])


def compact_about_sections_for_signature(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for section in sections[:8]:
        title = semantic_signature_text(section.get("title"))
        items = section.get("items")
        labels: list[str] = []
        if isinstance(items, list):
            for item in items[:10]:
                if isinstance(item, dict):
                    label = semantic_signature_text(item.get("label"))
                    if label:
                        labels.append(label)
        if title or labels:
            compact.append({"title": title, "items": sorted(set(labels))})
    return sorted(compact, key=lambda item: (item["title"] or "", item["items"]))


def compact_about_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for section in sections[:8]:
        title = as_string(section.get("title"))
        items = section.get("items")
        labels: list[str] = []
        if isinstance(items, list):
            for item in items[:10]:
                if isinstance(item, dict):
                    label = as_string(item.get("label"))
                    if label:
                        labels.append(label)
        if title or labels:
            compact.append({"title": title, "items": labels})
    return compact


def compact_review_signals(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for review in reviews[:5]:
        text = as_string(review.get("text") or review.get("snippet"))
        rating = review.get("rating")
        if text:
            text = text[:180]
        if text or rating is not None:
            compact.append({"rating": rating, "snippet": text})
    return compact


def sanitize_semantic_label(value: Any, *, max_length: int) -> str | None:
    label = as_string(value)
    if label is None or len(label) > max_length:
        return None
    if re.search(r"\d{3,}", label):
        return None
    return label


def normalize_semantic_neighborhood_label(
    value: Any,
    *,
    city_name: str | None,
    country_name: str | None,
) -> str | None:
    label = sanitize_semantic_label(value, max_length=64)
    if label is None:
        return None
    if semantic_neighborhood_label_has_site_mapping_source(
        label,
        city_name=city_name,
        country_name=country_name,
    ):
        label = re.sub(r"\s+", " ", label.strip()).strip(" -−ー－/()[]{}.")
    else:
        label = normalize_semantic_neighborhood_part(label)
    if label is None:
        return None
    country_key = normalize_locality_key(country_name)
    city_key = normalize_locality_key(city_name)
    label_key = normalize_locality_key(label)
    label_equivalence_key = normalize_locality_equivalence_key(label)
    city_equivalence_keys = normalize_locality_scope_equivalence_keys(city_name)
    country_equivalence_key = normalize_locality_equivalence_key(country_name)
    if (
        label_key == country_key
        or label_key == city_key
        or label_equivalence_key in city_equivalence_keys
        or (country_equivalence_key and label_equivalence_key == country_equivalence_key)
    ):
        return None
    if country_key == "taiwan" or city_key in {"taipei", "taipei city"}:
        shortened = re.sub(r"\s+District$", "", label, flags=re.IGNORECASE).strip()
        if shortened:
            label = shortened
    alias = SEMANTIC_NEIGHBORHOOD_DISPLAY_ALIASES.get(semantic_neighborhood_comparison_key(label))
    if alias:
        return alias
    label = display_case_semantic_neighborhood_label(label)
    alias = SEMANTIC_NEIGHBORHOOD_DISPLAY_ALIASES.get(semantic_neighborhood_comparison_key(label))
    if alias:
        return alias
    return label


def normalize_semantic_neighborhood_part(label: str) -> str | None:
    normalized = normalize_address_locality_part(label, strip_leading_preposition=False)
    if normalized is not None:
        return normalized

    candidate = re.sub(r"\s+", " ", label.strip()).strip(" -−ー－/()[]{}.")
    if not candidate:
        return None
    if is_country_locality(candidate):
        return None
    if is_subnational_locality(candidate) or is_subnational_locality_abbreviation(candidate):
        return None
    if is_explicit_subnational_locality_label(candidate):
        return None
    if is_building_or_unit_part(candidate):
        return None
    if re.search(r"\d{3,}", candidate):
        return None
    if semantic_neighborhood_has_non_saint_street_marker(candidate):
        return None
    return candidate


def semantic_neighborhood_label_has_site_mapping_source(
    label: str,
    *,
    city_name: str | None,
    country_name: str | None,
) -> bool:
    for rule in google_maps_place_neighborhood_mappings():
        if not site_mapping_context_matches(rule.get("city"), city_name):
            continue
        if not site_mapping_context_matches(rule.get("country"), country_name):
            continue
        if site_mapping_values_match(rule.get("from"), [label]):
            return True
    return False


def semantic_neighborhood_has_non_saint_street_marker(candidate: str) -> bool:
    return bool(
        re.search(
            (
                r"\b(?:chome|丁目|str|street|rd|road|rte|route|ct|court|ln|lane|ave|avenue|dr|drive|blvd|boulevard|"
                r"prom|promenade|rue|via|carrer|calle|avinguda|avenida|av|rambla|ronda|rda|passeig|paseo|pg|bd|quai|piazza|"
                r"placa|plaça|pl|bajada|jet[eé]e|"
                r"passatge|travessera|moll|jalan|soi|gil|ro|daero)\b"
                r"|^(?:c/|c\.|v\.|pg\.|av\.|bd\.|rda\.|pl\.|p\.za|p\.\s*º)\s"
            ),
            candidate,
            flags=re.IGNORECASE,
        )
    )


def semantic_neighborhood_comparison_key(value: str | None) -> str:
    return normalize_locality_key(value).replace("-", " ")


def display_case_semantic_neighborhood_label(label: str) -> str:
    normalized = re.sub(r"\s+", " ", label.replace("_", " ").strip())
    slug_like = bool(re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)+", label.strip()))
    if slug_like:
        normalized = re.sub(r"[-_]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or not normalized.isascii():
        return normalized
    if not slug_like and normalized != normalized.lower() and normalized != normalized.upper():
        return normalized
    return " ".join(display_case_semantic_neighborhood_word(word) for word in normalized.split(" "))


def display_case_semantic_neighborhood_word(word: str) -> str:
    lower_word = word.lower()
    if lower_word in SEMANTIC_NEIGHBORHOOD_UPPERCASE_TOKENS:
        return lower_word.upper()
    if not lower_word:
        return word
    return f"{lower_word[0].upper()}{lower_word[1:]}"


def refine_semantic_neighborhood_with_address_localities(
    semantic_neighborhood: str | None,
    locality_candidates: Sequence[str],
) -> str | None:
    if semantic_neighborhood is None:
        return None
    semantic_key = semantic_neighborhood_comparison_key(semantic_neighborhood)
    if len(semantic_key) < 4:
        return semantic_neighborhood
    for candidate in locality_candidates:
        candidate_key = semantic_neighborhood_comparison_key(candidate)
        if not candidate_key:
            continue
        if candidate_key == semantic_key:
            return candidate
        if (
            candidate_key.endswith(semantic_key)
            or candidate_key.startswith(f"{semantic_key} ")
            or f" {semantic_key} " in f" {candidate_key} "
        ):
            return candidate
    return semantic_neighborhood


def apply_site_neighborhood_mappings(
    neighborhood: str | None,
    *,
    city_name: str | None,
    country_name: str | None,
    address_values: Sequence[str | None],
    locality_candidates: Sequence[str],
) -> str | None:
    if neighborhood is None:
        return None
    for rule in google_maps_place_neighborhood_mappings():
        mapped = apply_site_neighborhood_mapping_rule(
            neighborhood,
            rule,
            city_name=city_name,
            country_name=country_name,
            address_values=address_values,
            locality_candidates=locality_candidates,
        )
        if mapped is not None:
            return mapped
    return neighborhood


def apply_site_neighborhood_mapping_rule(
    neighborhood: str,
    rule: dict[str, Any],
    *,
    city_name: str | None,
    country_name: str | None,
    address_values: Sequence[str | None],
    locality_candidates: Sequence[str],
) -> str | None:
    target = sanitize_semantic_label(rule.get("to"), max_length=64)
    if target is None:
        return None
    if not site_mapping_context_matches(rule.get("city"), city_name):
        return None
    if not site_mapping_context_matches(rule.get("country"), country_name):
        return None
    if not site_mapping_values_match(rule.get("from"), [neighborhood]):
        return None
    if "when_address_contains" in rule and not site_mapping_contains(
        rule.get("when_address_contains"),
        address_values,
    ):
        return None
    if "when_candidate" in rule and not site_mapping_values_match(
        rule.get("when_candidate"),
        locality_candidates,
    ):
        return None
    return target


def apply_site_category_mappings(
    category: str | None,
    *,
    city_name: str | None,
    country_name: str | None,
) -> str | None:
    if category is None:
        return None
    for rule in google_maps_place_category_mappings():
        mapped = apply_site_category_mapping_rule(
            category,
            rule,
            city_name=city_name,
            country_name=country_name,
        )
        if mapped is not None:
            return mapped
    return category


def apply_site_category_mapping_rule(
    category: str,
    rule: dict[str, Any],
    *,
    city_name: str | None,
    country_name: str | None,
) -> str | None:
    target = sanitize_enrichment_primary_category(rule.get("to"))
    if target is None:
        return None
    if not site_mapping_context_matches(rule.get("city"), city_name):
        return None
    if not site_mapping_context_matches(rule.get("country"), country_name):
        return None
    if not site_mapping_values_match(rule.get("from"), [category]):
        return None
    return target


def site_mapping_context_matches(configured: Any, actual: str | None) -> bool:
    values = coerce_mapping_string_list(configured)
    if not values:
        return True
    actual_key = normalize_locality_key(actual)
    return bool(actual_key and any(normalize_locality_key(value) == actual_key for value in values))


def site_mapping_values_match(configured: Any, values: Sequence[str]) -> bool:
    configured_values = coerce_mapping_string_list(configured)
    if not configured_values:
        return False
    value_keys = {normalize_locality_key(value) for value in values if normalize_locality_key(value)}
    return any(normalize_locality_key(value) in value_keys for value in configured_values)


def site_mapping_contains(configured: Any, values: Sequence[str | None]) -> bool:
    configured_values = coerce_mapping_string_list(configured)
    if not configured_values:
        return False
    haystack = "\n".join(value for value in values if isinstance(value, str)).casefold()
    return any(value.casefold() in haystack for value in configured_values)


def coerce_mapping_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def flatten_address_parts(address_parts: AddressParts | None) -> list[str]:
    if not address_parts:
        return []
    flattened: list[str] = []
    for part in address_parts:
        if isinstance(part, str):
            flattened.append(part)
        elif isinstance(part, list):
            flattened.extend(item for item in part if isinstance(item, str))
    return flattened


def display_price_range_for_place(
    enrichment_place: EnrichmentPlace,
    *,
    country_name: str | None,
) -> str | None:
    selected_display_price = display_price_source_for_place(
        enrichment_place,
        country_name=country_name,
    )
    return selected_display_price[1] if selected_display_price else None


def display_price_source_for_place(
    enrichment_place: EnrichmentPlace,
    *,
    country_name: str | None,
) -> tuple[str, str] | None:
    selected_price = select_place_display_price(enrichment_place)
    if selected_price is None:
        return None
    source, raw_price = selected_price
    config = google_maps_place_price_display_config()
    currency_mode = as_string(config.get("currency_mode")) or "raw"
    if currency_mode == "raw":
        display_price = raw_price
        target_currency = None
    elif currency_mode == "guide_local":
        target_currency = country_currency_code(country_name)
        display_price = (
            normalize_price_text_to_currency(raw_price, target_currency=target_currency)
            if target_currency is not None
            else None
        ) or raw_price
    elif currency_mode == "target":
        target_currency = as_string(config.get("target_currency"))
        display_price = (
            normalize_price_text_to_currency(raw_price, target_currency=target_currency)
            if target_currency is not None
            else None
        ) or raw_price
    else:
        display_price = raw_price
        target_currency = None
    if not price_display_amount_is_allowed(source, display_price, target_currency=target_currency):
        return None
    return source, display_price


def display_price_range_for_raw_place(place: RawPlace) -> str | None:
    price_level = place.price_level
    if price_level is None or price_level < 1:
        return None
    return "$" * min(price_level, 4)


def derive_place_budget_fields(
    *,
    enrichment_place: EnrichmentPlace,
    raw_place: RawPlace,
    price_source: str | None,
    display_price: str | None,
    primary_category: str | None,
    tags: list[str],
    country_name: str | None,
) -> dict[str, str | int | None]:
    budget_kind = derive_price_budget_kind(
        enrichment_place=enrichment_place,
        raw_place=raw_place,
        price_source=price_source,
        primary_category=primary_category,
        tags=tags,
    )
    if budget_kind is None:
        return {"budget_kind": None, "budget_tier": None, "budget_label": None}

    budget_tier = derive_budget_tier(
        display_price,
        budget_kind=budget_kind,
        country_name=country_name,
    )
    if budget_tier is None:
        return {"budget_kind": None, "budget_tier": None, "budget_label": None}

    return {
        "budget_kind": budget_kind,
        "budget_tier": budget_tier,
        "budget_label": "$" * budget_tier,
    }


def derive_price_budget_kind(
    *,
    enrichment_place: EnrichmentPlace,
    raw_place: RawPlace,
    price_source: str | None,
    primary_category: str | None,
    tags: list[str],
) -> str | None:
    category_values = [
        primary_category,
        enrichment_place.primary_type,
        enrichment_place.primary_type_display_name,
        enrichment_place.primary_type_display_name_localized,
        *enrichment_place.types,
        *raw_place.types,
        *tags,
    ]
    category_text = normalize_budget_category_text(category_values)
    has_hotel_hint = budget_category_has_hint(category_text, HOTEL_BUDGET_CATEGORY_HINTS)
    has_restaurant_hint = budget_category_has_hint(category_text, RESTAURANT_BUDGET_CATEGORY_HINTS)
    has_admission_hint = budget_category_has_hint(category_text, ADMISSION_BUDGET_CATEGORY_HINTS)

    if price_source == "room_price":
        return "hotel_per_night" if has_hotel_hint and not has_restaurant_hint else None
    if price_source == "admission_price":
        return "admission_per_person" if has_admission_hint else None
    if has_restaurant_hint:
        return "restaurant_per_person"
    if has_hotel_hint:
        return "hotel_per_night"
    if has_admission_hint:
        return "admission_per_person"
    return None


def normalize_budget_category_text(values: Iterable[str | None]) -> str:
    return " ".join(slugify(value) for value in values if value)


def budget_category_has_hint(category_text: str, hints: Iterable[str]) -> bool:
    category_tokens = {
        token
        for phrase in category_text.split()
        for token in phrase.split("-")
        if token
    }
    category_phrases = set(category_text.split())
    for hint in hints:
        normalized_hint = slugify(hint)
        if normalized_hint in category_tokens or normalized_hint in category_phrases:
            return True
    return False


def derive_budget_tier(
    display_price: str | None,
    *,
    budget_kind: str,
    country_name: str | None,
) -> int | None:
    if display_price is None or budget_kind not in PRICE_BUDGET_KINDS:
        return None

    symbolic_tier = symbolic_price_tier(display_price)
    if symbolic_tier is not None:
        return symbolic_tier

    parsed_price = parse_price_text(display_price)
    if parsed_price is None:
        return None

    currency, amounts, _suffix = parsed_price
    expected_currency = budget_tier_currency_code(country_name)
    if expected_currency and expected_currency != currency:
        return None

    thresholds = PRICE_BUDGET_TIER_THRESHOLDS.get(budget_kind, {}).get(currency)
    if thresholds is None:
        return None

    reference_amount = (
        max(amounts)
        if budget_kind == "admission_per_person"
        else sum(amounts) / len(amounts)
    )
    for index, threshold in enumerate(thresholds, start=1):
        if reference_amount <= threshold:
            return index
    return 4


def symbolic_price_tier(value: str) -> int | None:
    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        return None
    if re.fullmatch(r"\${1,4}", text):
        return len(text)
    for prefix in ("NT$", "HK$", "S$", "US$", "A$", "CA$"):
        if text.startswith(prefix):
            suffix = text.removeprefix(prefix)
            if suffix and not re.fullmatch(r"\$*", suffix):
                return None
            count = 1 + len(suffix)
            return count if 1 <= count <= 4 else None
    for symbol in ("€", "£", "¥", "₩", "฿", "₱", "₫", "₹"):
        if re.fullmatch(re.escape(symbol) + r"{1,4}", text):
            return len(text)
    return None


def select_place_display_price(enrichment_place: EnrichmentPlace) -> tuple[str, str] | None:
    config = google_maps_place_price_display_config()
    configured_order = coerce_string_list(config.get("source_order"))
    source_order = [
        source for source in configured_order if source in PRICE_DISPLAY_SOURCE_FIELDS
    ] or list(DEFAULT_PRICE_DISPLAY_SOURCE_ORDER)
    values = {
        "price_range": enrichment_place.price_range,
        "admission_price": enrichment_place.admission_price,
        "room_price": enrichment_place.room_price,
    }
    for source in source_order:
        value = as_string(values.get(source))
        if value is not None:
            if source == "price_range" and not price_range_source_is_eligible(enrichment_place, value):
                continue
            if source in {"admission_price", "room_price"} and not price_source_is_eligible_for_category(
                enrichment_place,
                source,
            ):
                continue
            return source, value
    return None


def price_display_amount_is_allowed(
    source: str,
    value: str,
    *,
    target_currency: str | None,
) -> bool:
    parsed = parse_price_text(value)
    if parsed is None:
        return True
    parsed_currency, amounts, _suffix = parsed
    currency = (target_currency or parsed_currency).strip().upper()
    max_amount = price_display_max_numeric_amount(source, currency)
    if max_amount is None:
        return True
    return max(amounts) <= max_amount


def price_display_max_numeric_amount(source: str, currency: str) -> float | None:
    config = google_maps_place_price_display_config()
    configured = config.get("max_numeric_by_source")
    if not isinstance(configured, dict):
        return None
    source_config = configured.get(source)
    if not isinstance(source_config, dict):
        return None
    amount = source_config.get(currency.strip().upper())
    return as_float(amount)


def price_range_source_is_eligible(enrichment_place: EnrichmentPlace, value: str) -> bool:
    category_values = [
        enrichment_place.primary_type,
        enrichment_place.primary_type_display_name,
        enrichment_place.primary_type_display_name_localized,
        *enrichment_place.types,
    ]
    category_text = normalize_budget_category_text(category_values)
    if re.fullmatch(r"\${1,4}", value.strip()):
        return budget_category_has_hint(category_text, NUMERIC_PRICE_RANGE_CATEGORY_HINTS)
    if parse_price_text(value) is None:
        return True
    return budget_category_has_hint(category_text, NUMERIC_PRICE_RANGE_CATEGORY_HINTS)


def price_source_is_eligible_for_category(enrichment_place: EnrichmentPlace, source: str) -> bool:
    category_values = [
        enrichment_place.primary_type,
        enrichment_place.primary_type_display_name,
        enrichment_place.primary_type_display_name_localized,
        *enrichment_place.types,
    ]
    category_text = normalize_budget_category_text(category_values)
    if source == "room_price":
        return budget_category_has_hint(
            category_text,
            HOTEL_BUDGET_CATEGORY_HINTS,
        ) and not budget_category_has_hint(category_text, RESTAURANT_BUDGET_CATEGORY_HINTS)
    if source == "admission_price":
        return budget_category_has_hint(category_text, ADMISSION_BUDGET_CATEGORY_HINTS)
    return True


def country_currency_code(country_name: str | None) -> str | None:
    return COUNTRY_CURRENCY_CODES.get(normalize_locality_key(country_name))


def budget_tier_currency_code(country_name: str | None) -> str | None:
    config = google_maps_place_price_display_config()
    currency_mode = as_string(config.get("currency_mode")) or "raw"
    if currency_mode == "target":
        target_currency = as_string(config.get("target_currency"))
        return target_currency.upper() if target_currency else None
    return country_currency_code(country_name)


def normalize_price_text_to_currency(value: str, *, target_currency: str) -> str | None:
    target = target_currency.strip().upper()
    if target not in PRICE_CURRENCY_SYMBOLS:
        return None
    symbolic = re.fullmatch(r"\${1,4}", value.strip())
    if symbolic:
        return format_symbolic_price_tier(len(symbolic.group(0)), currency=target)
    parsed = parse_price_text(value)
    if parsed is None:
        return None
    source_currency, amounts, suffix = parsed
    if source_currency == target:
        converted = amounts
    else:
        rates = load_usd_exchange_rates()
        if rates is None:
            return None
        source_rate = rates.get(source_currency)
        target_rate = rates.get(target)
        if not source_rate or not target_rate:
            return None
        converted = [amount / source_rate * target_rate for amount in amounts]
    return format_price_amounts(converted, currency=target, suffix=suffix)


def parse_price_text(value: str) -> tuple[str, list[float], str] | None:
    text = unicodedata.normalize("NFKC", value).replace("\u00a0", " ").strip()
    if not text:
        return None
    matches = list(PRICE_AMOUNT_RE.finditer(text))
    if not matches:
        return None
    first_currency = normalize_price_currency_token(matches[0].group("currency"))
    if first_currency is None:
        return None
    amounts: list[float] = []
    for match in matches:
        currency = normalize_price_currency_token(match.group("currency")) or first_currency
        if currency != first_currency:
            return None
        amount = parse_price_amount(match.group("amount"))
        if amount is None:
            return None
        amounts.append(amount)
        if len(amounts) >= 2:
            break
    if len(amounts) == 1:
        tail = text[matches[0].end() :]
        tail_match = re.search(r"^\s*[-–]\s*(?P<amount>[0-9][0-9,.\s\u00a0]*)(?P<plus>\+)?", tail)
        if tail_match:
            amount = parse_price_amount(tail_match.group("amount"))
            if amount is not None:
                amounts.append(amount)
    return (first_currency, amounts, "+" if text.endswith("+") or matches[-1].group("plus") else "")


def normalize_price_currency_token(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value).strip().upper().replace(" ", "")
    return PRICE_TOKEN_CURRENCIES.get(normalized)


def parse_price_amount(value: str) -> float | None:
    normalized = value.replace("\u00a0", "").replace(" ", "")
    if not normalized:
        return None
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif normalized.count(",") == 1 and "." not in normalized:
        whole, fractional = normalized.split(",", 1)
        normalized = f"{whole}.{fractional}" if len(fractional) in {1, 2} else normalized.replace(",", "")
    elif "," in normalized and "." not in normalized:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", normalized):
            normalized = normalized.replace(",", "")
        else:
            return None
    elif "." in normalized and "," not in normalized and re.fullmatch(r"\d{1,3}(?:[.]\d{3})+", normalized):
        normalized = normalized.replace(".", "")
    try:
        return float(normalized)
    except ValueError:
        return None


def format_price_amounts(amounts: list[float], *, currency: str, suffix: str) -> str | None:
    if not amounts:
        return None
    symbol = PRICE_CURRENCY_SYMBOLS[currency]
    rounded = [round_display_price_amount(amount, currency=currency) for amount in amounts]
    if len(rounded) >= 2 and rounded[0] != rounded[1]:
        return f"{symbol}{format_price_number(rounded[0])}–{format_price_number(rounded[1])}{suffix}"
    return f"{symbol}{format_price_number(rounded[0])}{suffix}"


def format_symbolic_price_tier(count: int, *, currency: str) -> str:
    symbol = PRICE_CURRENCY_SYMBOLS[currency]
    if len(symbol) == 1:
        return symbol * count
    if symbol.endswith("$"):
        return f"{symbol}{'$' * max(0, count - 1)}"
    return " ".join(symbol for _ in range(count))


def round_display_price_amount(amount: float, *, currency: str) -> int:
    absolute = abs(amount)
    if currency in {"JPY", "KRW", "TWD"} and absolute >= 1_000:
        step = 100
    elif absolute >= 1_000:
        step = 100
    elif absolute >= 100:
        step = 10
    else:
        step = 1
    return int(round(amount / step) * step)


def format_price_number(value: int) -> str:
    return f"{value:,}"


def load_usd_exchange_rates() -> dict[str, float] | None:
    cached = read_cached_usd_exchange_rates()
    if cached is not None:
        return cached
    rates = fetch_usd_exchange_rates()
    if rates is None:
        return None
    write_cached_usd_exchange_rates(rates)
    return rates


def read_cached_usd_exchange_rates() -> dict[str, float] | None:
    cache_path = PRICE_RATE_CACHE_DIR / "usd.json"
    try:
        stat_result = cache_path.stat()
    except FileNotFoundError:
        return None
    if datetime.now(UTC) - datetime.fromtimestamp(stat_result.st_mtime, tz=UTC) > PRICE_RATE_CACHE_TTL:
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rates = payload.get("rates")
    if not isinstance(rates, dict):
        return None
    return normalize_exchange_rate_payload(rates)


def write_cached_usd_exchange_rates(rates: dict[str, float]) -> None:
    PRICE_RATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    write_json(
        PRICE_RATE_CACHE_DIR / "usd.json",
        {"fetched_at": datetime.now(UTC).isoformat(), "base": "USD", "rates": rates},
        compact=True,
    )


def fetch_usd_exchange_rates() -> dict[str, float] | None:
    for url in (FX_RATES_API_URL, FAWAZ_CURRENCY_API_URL):
        try:
            with urlopen(url, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rates_payload = payload.get("rates")
        if isinstance(rates_payload, dict):
            rates = normalize_exchange_rate_payload(rates_payload)
        else:
            usd_payload = payload.get("usd")
            rates = normalize_exchange_rate_payload(usd_payload) if isinstance(usd_payload, dict) else None
        if rates:
            rates["USD"] = 1.0
            return rates
    return None


def normalize_exchange_rate_payload(payload: dict[Any, Any]) -> dict[str, float]:
    rates: dict[str, float] = {"USD": 1.0}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, int | float):
            continue
        currency = key.strip().upper()
        if len(currency) == 3 and value > 0:
            rates[currency] = float(value)
    return rates


SEMANTIC_DESCRIPTION_MAX_CHARS = 320


def sanitize_semantic_description(value: Any) -> str | None:
    description = as_string(value)
    if description is None:
        return None
    description = re.sub(r"\s+", " ", description).strip()
    if not description or len(description) > SEMANTIC_DESCRIPTION_MAX_CHARS:
        return None
    if looks_like_malformed_semantic_description(description):
        return None
    if looks_like_semantic_description_review_source_leak(description):
        return None
    if looks_like_semantic_description_curation_meta(description):
        return None
    if looks_like_semantic_description_attribute_boilerplate(description):
        return None
    return description


SEMANTIC_DESCRIPTION_MALFORMED_RE = re.compile(
    r"\b(?:"
    r"top\s+notch|kinda|at\s+this\s+price\s+point|doesn[’']?t\s+feel|"
    r"feels?\s+(?:kinda\s+)?cheap|clueless)\b",
    re.IGNORECASE,
)


def looks_like_malformed_semantic_description(value: str) -> bool:
    if looks_like_slash_delimited_phrase_leak(value):
        return True
    malformed_markers = SEMANTIC_DESCRIPTION_MALFORMED_RE.findall(value)
    has_chatty_structure = any(marker in value for marker in ("$", ";", "(", ")"))
    return has_chatty_structure and len(malformed_markers) >= 2


SEMANTIC_DESCRIPTION_SLASH_SEPARATOR_RE = re.compile(r"(?:(?<=\s)/|/(?=\s))")


def looks_like_slash_delimited_phrase_leak(value: str) -> bool:
    if len(SEMANTIC_DESCRIPTION_SLASH_SEPARATOR_RE.findall(value)) < 2:
        return False
    segments = [segment.strip() for segment in re.split(r"\s*/\s*", value) if segment.strip()]
    phrase_like_segments = 0
    for segment in segments:
        words = SEMANTIC_DESCRIPTION_TOKEN_RE.findall(segment)
        if len(words) >= 3 or len(segment) >= 35:
            phrase_like_segments += 1
    return phrase_like_segments >= 2


SEMANTIC_DESCRIPTION_REVIEW_SOURCE_LEAK_RE = re.compile(
    r"\b(?:"
    r"(?:mixed\s+)?reviews?\s+(?:note|notes|say|says|mention|mentions|praise|praises|call|calls)|"
    r"(?:reviewers?|customers?|users?|people)\s+(?:note|notes|say|says|mention|mentions|praise|praises|call|calls)|"
    r"(?:highly\s+)?(?:praised|lauded|commended|celebrated|famed)\s+(?:for|as|\w+)|"
    r"(?:widely\s+)?(?:hailed|regarded|considered)\s+as|"
    r"one\s+of\s+(?:the\s+)?(?:(?:world'?s|italy'?s|city'?s|region'?s|area'?s)\s+)?(?:most|best|finest|top)|"
    r"(?:most\s+photographed|top[-\s]?rated|number\s+one|\#1)|"
    r"(?:just\s+expect|separate\s+charge|though\s+[^.?!]{0,80}\b(?:minimal|limited|expensive|pricey|crowded|slow|rude|disappointing))|"
    r"(?:wheelchair[-\s]?accessible|lgbtq\+?[-\s]?friendly)|"
    r"according\s+to\s+(?:reviews?|reviewers?|customers?|users?)"
    r")\b",
    re.IGNORECASE,
)


def looks_like_semantic_description_review_source_leak(value: str) -> bool:
    return SEMANTIC_DESCRIPTION_REVIEW_SOURCE_LEAK_RE.search(value) is not None


SEMANTIC_DESCRIPTION_CURATION_META_RE = re.compile(
    r"\b(?:"
    r"favorite[-\s]?worthy|"
    r"surface(?:s|d|ing)?\s+(?:without|in|on|as|for)\b|"
    r"(?:extra\s+)?manual\s+curation|"
    r"tourist\s+checklists?|"
    r"personal\s+recommendation|"
    r"saved[-\s]?list\s+(?:context|copy|curation|metadata|note)|"
    r"curation\s+(?:copy|metadata|note)"
    r")",
    re.IGNORECASE,
)


def looks_like_semantic_description_curation_meta(value: str) -> bool:
    return SEMANTIC_DESCRIPTION_CURATION_META_RE.search(value) is not None


SEMANTIC_DESCRIPTION_ATTRIBUTE_BOILERPLATE_RE = re.compile(
    r"\b(?:"
    r"(?:full|easy)?\s*wheelchair\s+accessibility|"
    r"wheelchair[-\s]?(?:friendly|access|accessible|amenit(?:y|ies))|"
    r"(?:family[-\s]?friendly|notable|comprehensive|visitor|onsite)?\s*accessibility(?:[-\s]?(?:conscious|features?|amenit(?:y|ies)))?|"
    r"accessible\s+(?:amenit(?:y|ies)|facilit(?:y|ies)|parking|restrooms?|seating|setting|green\s+space)|"
    r"(?:with|featuring|offering|and)\s+(?:accessible|full accessibility)\b|"
    r"full\s+accessibility(?:\s+features?)?|"
    r"\bthis\s+accessible\s+\w+"
    r")",
    re.IGNORECASE,
)


def looks_like_semantic_description_attribute_boilerplate(value: str) -> bool:
    return SEMANTIC_DESCRIPTION_ATTRIBUTE_BOILERPLATE_RE.search(value) is not None


def usable_semantic_description(
    value: Any,
    *,
    enrichment_place: EnrichmentPlace,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
) -> str | None:
    description = sanitize_semantic_description(value)
    if description is None:
        return None
    if not enrichment_identity_is_compatible_with_raw(raw_place, enrichment_place):
        return None
    if semantic_description_has_conflicting_location(
        description,
        enrichment_place=enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
        country_name=country_name,
    ):
        return None
    if semantic_description_is_low_information(
        description,
        enrichment_place=enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
        country_name=country_name,
    ):
        return None
    return description


LOW_INFORMATION_SEMANTIC_DESCRIPTION_TEMPLATE_RE = re.compile(
    r"^.{1,120}\s+is\s+(?:an?|the)\s+.{1,120}(?:\s+(?:in|near|around|at)\s+.{1,120})?\.$",
    re.IGNORECASE,
)
LOW_INFORMATION_SEMANTIC_DESCRIPTION_LOCATED_TEMPLATE_RE = re.compile(
    r"^(?:located|set|based)\s+(?:in|near|around|at)\s+.{1,120},\s+.{1,120}\s+is\s+(?:an?|the)\s+.{1,120}\.$",
    re.IGNORECASE,
)
SEMANTIC_DESCRIPTION_RICH_CONTEXT_RE = re.compile(
    r"\b(?:known for|famous for|specializing in|focused on|serving|serves|features|featuring|offers|offering|"
    r"with|housed in|set in|built|dating|founded|opened|showcasing|highlighting)\b",
    re.IGNORECASE,
)
SEMANTIC_DESCRIPTION_TOKEN_RE = re.compile(r"[\w]+(?:[-'][\w]+)*", re.UNICODE)
SEMANTIC_DESCRIPTION_CONTEXT_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "around",
    "as",
    "at",
    "based",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "its",
    "near",
    "of",
    "on",
    "or",
    "place",
    "saved",
    "set",
    "the",
    "this",
    "to",
    "with",
}


def semantic_description_is_low_information(
    description: str,
    *,
    enrichment_place: EnrichmentPlace,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
) -> bool:
    if (
        LOW_INFORMATION_SEMANTIC_DESCRIPTION_TEMPLATE_RE.match(description)
        or LOW_INFORMATION_SEMANTIC_DESCRIPTION_LOCATED_TEMPLATE_RE.match(description)
    ) and not SEMANTIC_DESCRIPTION_RICH_CONTEXT_RE.search(description):
        return True

    description_tokens = semantic_description_meaningful_tokens(description)
    if not description_tokens:
        return True
    context_tokens = semantic_description_card_context_tokens(
        enrichment_place,
        raw_place=raw_place,
        city_name=city_name,
        country_name=country_name,
    )
    if description_tokens and description_tokens <= context_tokens:
        return True

    distinctive_tokens = description_tokens - context_tokens
    if len(distinctive_tokens) <= 1 and not SEMANTIC_DESCRIPTION_RICH_CONTEXT_RE.search(description):
        return True
    return False


def semantic_description_card_context_tokens(
    enrichment_place: EnrichmentPlace,
    *,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
) -> set[str]:
    values: list[Any] = [
        semantic_place_name(enrichment_place, raw_place=raw_place),
        enrichment_place.display_name,
        raw_place.name,
        enrichment_place.primary_type,
        enrichment_place.primary_type_display_name,
        enrichment_place.primary_type_display_name_localized,
        enrichment_place.category_display_en,
        enrichment_place.formatted_address,
        enrichment_place.address_display_en,
        raw_place.address,
        enrichment_place.plus_code,
        city_name,
        country_name,
        *enrichment_place.types,
    ]
    tokens: set[str] = set()
    for value in values:
        tokens.update(semantic_description_meaningful_tokens(value))
    return tokens


def semantic_description_meaningful_tokens(value: Any) -> set[str]:
    text = as_string(value)
    if text is None:
        return set()
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("_", " ").replace("-", " ")
    tokens = {
        token.strip("'")
        for token in SEMANTIC_DESCRIPTION_TOKEN_RE.findall(normalized)
        if len(token.strip("'")) >= 2 and not token.strip("'").isdigit()
    }
    return {token for token in tokens if token not in SEMANTIC_DESCRIPTION_CONTEXT_STOPWORDS}


SEMANTIC_DESCRIPTION_CONFLICT_LOCATION_TERMS: tuple[str, ...] = (
    "nishi-shinjuku",
    "shinjuku",
    "shibuya",
    "ginza",
    "roppongi",
    "tokyo",
    "osaka",
    "kyoto",
    "sapporo",
    "seoul",
    "taipei",
    "bangkok",
    "singapore",
    "paris",
    "london",
    "new york",
    "los angeles",
    "san francisco",
    "shanghai",
    "beijing",
    "tianjin",
    "beijing-tianjin-hebei",
    "hong kong",
)
SEMANTIC_DESCRIPTION_LOCATION_NAME_SUFFIX_RE = re.compile(
    r"^\s+(?:cafe|café|restaurant|bar|hotel|museum|market|gallery|theatre|theater|station|park|square)\b",
    re.IGNORECASE,
)
SEMANTIC_DESCRIPTION_LOCATION_STYLE_SUFFIX_RE = re.compile(
    r"^-(?:style|inspired|brest)\b",
    re.IGNORECASE,
)


def semantic_description_has_conflicting_location(
    description: str,
    *,
    enrichment_place: EnrichmentPlace,
    raw_place: RawPlace,
    city_name: str | None,
    country_name: str | None,
) -> bool:
    description_key = normalize_locality_key(description)
    if not description_key:
        return False
    allowed_location_key = normalize_locality_key(
        " ".join(
            candidate
            for candidate in (
                city_name,
                country_name,
                enrichment_place.formatted_address,
                enrichment_place.address_display_en,
                enrichment_place.plus_code,
                raw_place.address,
            )
            if candidate
        )
    )
    for term in SEMANTIC_DESCRIPTION_CONFLICT_LOCATION_TERMS:
        term_key = normalize_locality_key(term)
        if not term_key or term_key in allowed_location_key:
            continue
        term_pattern = re.escape(term_key).replace(r"\-", r"[- ]")
        match = re.search(
            rf"\b(?:in|near|around|at|from|serving)\s+(?:the\s+)?{term_pattern}(?:\s+s)?\b",
            description_key,
        )
        if match and not SEMANTIC_DESCRIPTION_LOCATION_NAME_SUFFIX_RE.match(description_key[match.end() :]):
            suffix = description_key[match.end() :]
            if not SEMANTIC_DESCRIPTION_LOCATION_STYLE_SUFFIX_RE.match(suffix):
                return True
    return False


def normalize_semantic_tag_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        item_text = as_string(item)
        if item_text is None:
            continue
        tag = normalize_tag_slug(item_text)
        if semantic_tag_slug_is_usable(tag) and tag not in tags:
            tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def semantic_tag_slug_is_usable(tag: str | None) -> bool:
    if not tag or tag == "item":
        return False
    if len(tag) == 1 and tag.isalpha():
        return False
    return True


@lru_cache(maxsize=1)
def semantic_llm_config() -> dict[str, str] | None:
    env = load_dotenv_values(ROOT / ".env")
    model = os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") or env.get("LLM_MODEL") or env.get("OPENAI_MODEL")
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY")
    base_url = (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or env.get("LLM_BASE_URL")
        or env.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    )
    if not model or not api_key:
        return None
    namespace = hashlib.sha256(f"{base_url}:{model}:{SEMANTIC_LLM_PROMPT_VERSION}".encode("utf-8")).hexdigest()[:16]
    return {"model": model, "api_key": api_key, "base_url": base_url, "namespace": namespace}


def load_dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        values[key.strip()] = raw_value.strip().strip("\"'")
    return values


LANGFUSE_DEFAULT_TIMEOUT_SECONDS = 2
LANGFUSE_DEFAULT_FLUSH_AT = 8
LANGFUSE_DEFAULT_FLUSH_INTERVAL_SECONDS = 1.0
LANGFUSE_CLIENT_CACHE: dict[tuple[str, str, str | None, int, int, float], Any] = {}
LANGFUSE_CLIENT_CACHE_LOCK = Lock()


def configured_langfuse_client() -> Any | None:
    env = load_dotenv_values(ROOT / ".env")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY") or env.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY") or env.get("LANGFUSE_SECRET_KEY")
    base_url = normalize_langfuse_base_url(
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or env.get("LANGFUSE_BASE_URL")
        or env.get("LANGFUSE_HOST")
    )
    if not public_key or not secret_key:
        return None
    options = langfuse_client_options(env)
    return langfuse_client_for_config(public_key, secret_key, base_url, **options)


def langfuse_client_options(env: dict[str, str]) -> dict[str, int | float]:
    return {
        "timeout": langfuse_int_option(
            "LANGFUSE_TIMEOUT",
            env,
            default=LANGFUSE_DEFAULT_TIMEOUT_SECONDS,
        ),
        "flush_at": langfuse_int_option(
            "LANGFUSE_FLUSH_AT",
            env,
            default=LANGFUSE_DEFAULT_FLUSH_AT,
        ),
        "flush_interval": langfuse_float_option(
            "LANGFUSE_FLUSH_INTERVAL",
            env,
            default=LANGFUSE_DEFAULT_FLUSH_INTERVAL_SECONDS,
        ),
    }


def langfuse_int_option(name: str, env: dict[str, str], *, default: int) -> int:
    raw_value = os.environ.get(name) or env.get(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def langfuse_float_option(name: str, env: dict[str, str], *, default: float) -> float:
    raw_value = os.environ.get(name) or env.get(name)
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def langfuse_client_for_config(
    public_key: str,
    secret_key: str,
    base_url: str | None,
    *,
    timeout: int,
    flush_at: int,
    flush_interval: float,
) -> Any | None:
    cache_key = (public_key, secret_key, base_url, timeout, flush_at, flush_interval)
    cached = LANGFUSE_CLIENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    with LANGFUSE_CLIENT_CACHE_LOCK:
        cached = LANGFUSE_CLIENT_CACHE.get(cache_key)
        if cached is not None:
            return cached
        try:
            from langfuse import Langfuse
        except ImportError:
            return None
        kwargs = {
            "public_key": public_key,
            "secret_key": secret_key,
            "timeout": timeout,
            "flush_at": flush_at,
            "flush_interval": flush_interval,
        }
        if base_url:
            kwargs["base_url"] = base_url
        try:
            client = Langfuse(**kwargs)
        except Exception:
            return None
        LANGFUSE_CLIENT_CACHE[cache_key] = client
        return client


def clear_langfuse_client_cache() -> None:
    with LANGFUSE_CLIENT_CACHE_LOCK:
        LANGFUSE_CLIENT_CACHE.clear()


def normalize_langfuse_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().rstrip("/")
    if not stripped:
        return None
    if "://" in stripped:
        return stripped
    return f"https://{stripped}"


def flush_langfuse_client(client: Any) -> None:
    try:
        client.flush()
    except Exception:
        return


def flush_langfuse() -> None:
    for client in list(LANGFUSE_CLIENT_CACHE.values()):
        flush_langfuse_client(client)


def open_langfuse_generation(
    *,
    name: str,
    model: str,
    input_payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    client = configured_langfuse_client()
    if client is None:
        return None, _NoopLangfuseObservation()
    try:
        manager = client.start_as_current_observation(
            as_type="generation",
            name=name,
            model=model,
            input=input_payload,
            metadata=metadata,
        )
        observation = manager.__enter__()
    except Exception:
        return None, _NoopLangfuseObservation()
    return manager, observation


def close_langfuse_generation(manager: Any, exc_info: tuple[Any, Any, Any]) -> None:
    if manager is None:
        return
    try:
        manager.__exit__(*exc_info)
    except Exception:
        return


def update_langfuse_generation(observation: Any, **kwargs: Any) -> None:
    try:
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        observation.update(**kwargs)
    except Exception:
        return


def openai_usage_details(response_payload: Any) -> dict[str, int] | None:
    if not isinstance(response_payload, dict):
        return None
    usage = response_payload.get("usage")
    if not isinstance(usage, dict):
        return None
    details: dict[str, int] = {}
    for langfuse_key, openai_key in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = usage.get(openai_key)
        if isinstance(value, int):
            details[langfuse_key] = value
    return details or None


def google_maps_place_semantic_cache_dir() -> Path:
    return SCRAPER_STATE_DIR / "semantic-cache"


def read_semantic_llm_cache(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    repair = payload.get("repair") if isinstance(payload, dict) else None
    return repair if isinstance(repair, dict) else None


def write_semantic_llm_cache(path: Path, repair: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"repair": repair}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def extract_chat_message_content(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    return as_string(message.get("content"))


def normalize_place_page_business_status(status: str | None) -> str | None:
    normalized = as_string(status)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if "permanently closed" in lowered:
        return "CLOSED_PERMANENTLY"
    if "temporarily closed" in lowered:
        return "CLOSED_TEMPORARILY"
    if lowered.startswith("open") or lowered.startswith("closed") or "opens " in lowered:
        return "OPERATIONAL"
    return None


def fetch_places_api_enrichment(
    place: RawPlace,
    *,
    city_name: str | None = None,
    country_name: str | None = None,
    signature_google_place_id: str | None = None,
    api_key: str,
) -> EnrichmentCacheEntry:
    query = build_place_page_search_query(
        place,
        city_name=city_name,
        country_name=country_name,
    )
    request_body = {
        "textQuery": query,
        "pageSize": 3,
        "languageCode": "en",
    }

    lat = place.lat
    lng = place.lng
    if lat is not None and lng is not None:
        request_body["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": 600.0,
            }
        }

    request = Request(
        PLACES_TEXT_SEARCH_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": PLACES_FIELD_MASK,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return build_cache_entry(
            place,
            source="google_places_api",
            query=query,
            city_name=city_name,
            country_name=country_name,
            signature_google_place_id=signature_google_place_id,
            error=f"http_{exc.code}",
            error_body=body[:500],
        )
    except URLError as exc:
        return build_cache_entry(
            place,
            source="google_places_api",
            query=query,
            city_name=city_name,
            country_name=country_name,
            signature_google_place_id=signature_google_place_id,
            error=f"url_error:{exc.reason}",
        )

    matches = payload.get("places")
    if not isinstance(matches, list) or not matches:
        return build_cache_entry(
            place,
            source="google_places_api",
            query=query,
            city_name=city_name,
            country_name=country_name,
            signature_google_place_id=signature_google_place_id,
            matched=False,
        )

    scored_matches = [
        (score_text_search_candidate(place, candidate), candidate)
        for candidate in matches
        if isinstance(candidate, dict)
    ]
    scored_matches.sort(key=lambda item: item[0], reverse=True)
    best_score, best_match = scored_matches[0]

    result = build_cache_entry(
        place,
        source="google_places_api",
        query=query,
        city_name=city_name,
        country_name=country_name,
        signature_google_place_id=signature_google_place_id,
        matched=best_score >= 25,
        score=best_score,
        enrichment_place=normalize_enrichment_match(best_match) if best_score >= 25 else None,
    )
    return result


def build_text_query(place: RawPlace) -> str:
    name = place.name
    address = place.address or ""
    query = f"{name}, {address}".strip(", ")
    return query or name or address


def build_maps_link_query(
    *,
    name: str | None,
    address: str | None,
    lat: float | None,
    lng: float | None,
) -> str | None:
    text_parts = [part.strip() for part in (name, address) if isinstance(part, str) and part.strip()]
    if text_parts:
        return ", ".join(dict.fromkeys(text_parts))
    if lat is not None and lng is not None:
        return f"{lat:.7f},{lng:.7f}"
    return None


def build_google_maps_search_url(query: str, *, google_place_id: str | None = None) -> str:
    params = {"api": "1", "query": query}
    if google_place_id:
        params["query_place_id"] = google_place_id
    return f"https://www.google.com/maps/search/?{urlencode(params)}"


def should_rebuild_google_maps_url(maps_url: str | None) -> bool:
    normalized_url = as_string(maps_url)
    if normalized_url is None:
        return True
    if re.search(r"[?&]cid=[^&#]+", normalized_url):
        return True
    if "google." not in normalized_url:
        return False
    return bool(re.search(r"[?&]q=-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?(?:[&#]|$)", normalized_url))


def build_public_google_maps_url(
    *,
    name: str | None,
    address: str | None,
    lat: float | None,
    lng: float | None,
    raw_maps_url: str | None,
    google_maps_uri: str | None = None,
    google_place_id: str | None = None,
) -> str:
    query = build_maps_link_query(name=name, address=address, lat=lat, lng=lng)
    if query and google_place_id:
        return build_google_maps_search_url(query, google_place_id=google_place_id)

    preferred_url = as_string(google_maps_uri) or as_string(raw_maps_url)
    if preferred_url and not should_rebuild_google_maps_url(preferred_url):
        return preferred_url

    if query:
        return build_google_maps_search_url(query)

    return preferred_url or raw_maps_url or "https://www.google.com/maps"


def score_text_search_candidate(raw_place: RawPlace, candidate: dict[str, Any]) -> int:
    score = 0
    raw_name = normalize_text(raw_place.name)
    candidate_name = normalize_text(display_name_text(candidate.get("displayName")))
    raw_address = normalize_text(raw_place.address)
    candidate_address = normalize_text(as_string(candidate.get("formattedAddress")))

    if raw_name and candidate_name:
        if raw_name == candidate_name:
            score += 50
        elif raw_name in candidate_name or candidate_name in raw_name:
            score += 30
        else:
            overlap = token_overlap_score(raw_name, candidate_name)
            score += overlap

    if raw_address and candidate_address:
        score += token_overlap_score(raw_address, candidate_address)

    raw_lat = raw_place.lat
    raw_lng = raw_place.lng
    candidate_location = candidate.get("location")
    if (
        raw_lat is not None
        and raw_lng is not None
        and isinstance(candidate_location, dict)
        and as_float(candidate_location.get("latitude")) is not None
        and as_float(candidate_location.get("longitude")) is not None
    ):
        distance_m = haversine_meters(
            raw_lat,
            raw_lng,
            as_float(candidate_location.get("latitude")) or raw_lat,
            as_float(candidate_location.get("longitude")) or raw_lng,
        )
        if distance_m <= 100:
            score += 40
        elif distance_m <= 400:
            score += 25
        elif distance_m <= 1200:
            score += 10

    return score


def normalize_enrichment_match(candidate: dict[str, Any]) -> EnrichmentPlace:
    display_name = candidate.get("displayName")
    raw_primary_type_display_name = sanitize_enrichment_primary_category(
        display_name_text(candidate.get("primaryTypeDisplayName"))
    )
    primary_type = as_string(candidate.get("primaryType"))
    raw_localized_display_name = (
        None if normalize_type_lookup_text(primary_type) == "item" else raw_primary_type_display_name
    )
    primary_type_display_name = canonical_primary_category_label(
        primary_type=primary_type,
        display_name=raw_primary_type_display_name,
    )
    primary_type_display_name_localized = localized_primary_category_label(
        raw_display_name=raw_localized_display_name,
        canonical_display_name=primary_type_display_name,
    )
    primary_type, types = normalized_enrichment_type_ids(
        primary_type,
        primary_type_display_name,
        coerce_string_list(candidate.get("types")),
    )
    return EnrichmentPlace(
        google_place_id=as_string(candidate.get("id")),
        google_place_resource_name=as_string(candidate.get("name")),
        display_name=display_name_text(display_name),
        formatted_address=as_string(candidate.get("formattedAddress")),
        google_maps_uri=as_string(candidate.get("googleMapsUri")),
        rating=as_float(candidate.get("rating")),
        user_rating_count=as_int(candidate.get("userRatingCount")),
        primary_type=primary_type,
        primary_type_display_name=primary_type_display_name,
        primary_type_display_name_localized=primary_type_display_name_localized,
        types=types,
        business_status=as_string(candidate.get("businessStatus")),
        website=as_http_url(candidate.get("websiteUri")),
    )


def coerce_enrichment_place(value: EnrichmentCacheEntry | None) -> EnrichmentPlace:
    if value is None or value.place is None:
        return EnrichmentPlace()
    return canonicalize_enrichment_place(value.place) or EnrichmentPlace()


def display_name_text(value: Any) -> str | None:
    if isinstance(value, dict):
        return as_string(value.get("text"))
    if isinstance(value, str):
        return as_string(value)
    return None


def normalize_business_status(value: str | None) -> str | None:
    mapping = {
        "OPERATIONAL": "active",
        "CLOSED_TEMPORARILY": "temporarily-closed",
        "CLOSED_PERMANENTLY": "closed-permanently",
        "FUTURE_OPENING": "future-opening",
    }
    if value is None:
        return None
    return mapping.get(value, slugify(value))


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9\s]", " ", value.lower()).strip()


def normalize_place_selectors(selectors: list[str]) -> set[str]:
    return {
        normalized
        for selector in selectors
        if (normalized := as_string(selector.casefold() if isinstance(selector, str) else selector))
    }


def place_selector_matches(
    slug: str,
    place: RawPlace,
    *,
    place_id: str,
    selectors: set[str],
) -> set[str]:
    if not selectors:
        return set()

    values = {
        place_id.casefold(),
        f"{slug}:{place_id}".casefold(),
        f"guide-slug:{slug}".casefold(),
    }
    for candidate in (
        place.name,
        place.maps_url,
        place.cid,
        place.google_id,
        place.maps_place_token,
    ):
        normalized = as_string(candidate.casefold() if isinstance(candidate, str) else candidate)
        if normalized is None:
            continue
        values.add(normalized)
        values.add(f"{slug}:{normalized}".casefold())
    for prefix, candidate in (
        ("cid", place.cid),
        ("cid", extract_maps_cid(place.maps_url)),
        ("gms", place.maps_place_token),
        ("gms", extract_maps_place_token(place.maps_url)),
    ):
        normalized = as_string(candidate.casefold() if isinstance(candidate, str) else candidate)
        if normalized is None:
            continue
        values.add(normalized)
        values.add(f"{prefix}:{normalized}".casefold())
        values.add(f"{slug}:{normalized}".casefold())
        values.add(f"{slug}:{prefix}:{normalized}".casefold())
    return selectors & values


def token_overlap_score(left: str, right: str) -> int:
    left_tokens = {token for token in left.split() if len(token) > 2}
    right_tokens = {token for token in right.split() if len(token) > 2}
    if not left_tokens or not right_tokens:
        return 0
    overlap = len(left_tokens & right_tokens)
    return overlap * 5


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def format_duration_seconds(duration_seconds: float) -> str:
    total_tenths = max(0, int(max(0.0, duration_seconds) * 10 + 0.5))
    minutes, remaining_tenths = divmod(total_tenths, 600)
    seconds, tenths = divmod(remaining_tenths, 10)
    if minutes >= 1:
        return f"{minutes}m {seconds:02d}.{tenths}s"
    return f"{seconds}.{tenths}s"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.trust_google_fallback and not args.refresh_trust_signals:
        parser.error("--trust-google-fallback requires --refresh-trust-signals")

    if (args.refresh_list or args.refresh_force) and not args.refresh:
        args.refresh = True
    if args.force_semantic_descriptions and not args.refresh_semantic_descriptions:
        args.refresh_semantic_descriptions = True
    if args.force_semantic_enrichment and not args.refresh_semantic_enrichment:
        args.refresh_semantic_enrichment = True
    if args.enrich_place and not (
        args.enrich
        or args.enrich_missing
        or args.refresh_enrichment
        or args.refresh_semantic_descriptions
        or args.refresh_semantic_enrichment
    ):
        args.refresh_enrichment = True

    if args.refresh:
        refresh_raw_sources(
            headed=args.headed,
            force_refresh=args.refresh_force,
            refresh_lists=args.refresh_list,
            refresh_workers=args.refresh_workers,
            refresh_retries=args.refresh_retries,
            refresh_retry_backoff_seconds=args.refresh_retry_backoff_seconds,
            refresh_startup_jitter_seconds=args.refresh_startup_jitter_seconds,
        )

    if (
        (args.enrich or args.enrich_missing or args.refresh_enrichment)
        and not args.refresh
        and not args.skip_enrichment_source_refresh
    ):
        enrichment_refresh_lists = enrichment_source_refresh_lists(args.enrich_place)
        if enrichment_refresh_lists or (args.refresh_enrichment and not args.enrich_place):
            refresh_raw_sources(
                headed=args.headed,
                force_refresh=bool(enrichment_refresh_lists),
                refresh_lists=enrichment_refresh_lists,
                refresh_workers=args.refresh_workers,
                refresh_retries=args.refresh_retries,
                refresh_retry_backoff_seconds=args.refresh_retry_backoff_seconds,
                refresh_startup_jitter_seconds=args.refresh_startup_jitter_seconds,
            )

    if args.enrich or args.enrich_missing or args.refresh_enrichment:
        sync_local_csv_sources()
        enrich_raw_sources(
            force_refresh=args.refresh_enrichment,
            missing_only=args.enrich_missing,
            place_selectors=args.enrich_place,
            refresh_workers=args.refresh_workers,
            refresh_startup_jitter_seconds=args.refresh_startup_jitter_seconds,
        )

    if args.refresh_semantic_descriptions or args.refresh_semantic_enrichment:
        sync_local_csv_sources()
        refresh_cached_semantic_enrichment(
            enable_semantics=args.refresh_semantic_enrichment,
            enable_description=args.refresh_semantic_descriptions,
            force_semantics=args.force_semantic_enrichment,
            force_description=args.force_semantic_descriptions,
            place_selectors=args.enrich_place,
        )

    if args.refresh_trust_signals:
        refresh_trust_signals(include_google_fallback=args.trust_google_fallback)

    if args.export_cache_json:
        exported_guides = export_all_places_cache_json()
        print(f"Exported JSON cache debug files for {exported_guides} guide(s).")

    build_started_at = time.perf_counter()
    photo_only_refresh = (
        args.refresh_photos
        and not args.refresh
        and not args.enrich
        and not args.enrich_missing
        and not args.refresh_enrichment
        and not args.refresh_semantic_descriptions
        and not args.refresh_semantic_enrichment
        and not args.refresh_trust_signals
        and not args.export_cache_json
    )
    if photo_only_refresh and refresh_generated_guide_photos(
        photo_workers=args.refresh_workers,
        startup_jitter_seconds=args.refresh_startup_jitter_seconds,
    ):
        build_duration = time.perf_counter() - build_started_at
        print(f"Refreshed place photos in {format_duration_seconds(build_duration)}.")
        return 0

    rebuild_generated_data(
        refresh_photos=args.refresh_photos,
        photo_workers=args.refresh_workers,
        startup_jitter_seconds=args.refresh_startup_jitter_seconds,
    )
    build_duration = time.perf_counter() - build_started_at
    print(f"Generated list data, manifests, and search index in {format_duration_seconds(build_duration)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

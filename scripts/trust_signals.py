from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    insert,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url

try:
    from scripts.pipeline_models import EnrichmentCacheEntry, RawPlace, RawSavedList, TrustSignal
except ModuleNotFoundError:
    from pipeline_models import EnrichmentCacheEntry, RawPlace, RawSavedList, TrustSignal

TRUST_STORE_URL_ENV = "FAVORITE_PLACES_TRUST_STORE_URL"
BRAVE_SEARCH_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
BRAVE_API_KEY_ENV = "BRAVE_API_KEY"
GOOGLE_FALLBACK_ENV = "FAVORITE_PLACES_TRUST_GOOGLE_FALLBACK"
MICHELIN_REGION_URLS_ENV = "FAVORITE_PLACES_MICHELIN_REGION_URLS"
TABELOG_SOURCE_URLS_ENV = "FAVORITE_PLACES_TABELOG_SOURCE_URLS"
TRUST_SIGNAL_REFRESH_TTL = timedelta(days=90)
MICHELIN_REGION_REFRESH_TTL = timedelta(days=365)
TABELOG_AWARD_REFRESH_STAGGER = timedelta(days=180)
TABELOG_HYAKUMEITEN_REFRESH_TTL = timedelta(days=30)
TABELOG_HYAKUMEITEN_REFRESH_STAGGER = timedelta(days=14)
TABELOG_SEARCH_REFRESH_STAGGER = timedelta(days=180)
MICHELIN_SOURCE_CACHE_VERSION = 3
MICHELIN_DETAIL_CACHE_VERSION = 2
TABELOG_SOURCE_CACHE_VERSION = 1
TRUST_SIGNAL_HTTP_TIMEOUT_SECONDS = 20
TRUST_SIGNAL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
TRUST_SIGNAL_SOURCE_PRIORITY = {
    "michelin": 0,
    "tabelog": 1,
    "timeout": 2,
    "blog": 3,
    "web": 4,
    "brave_search": 5,
    "google_search": 6,
}
TRUST_SIGNAL_REPLACEABLE_SOURCES = tuple(TRUST_SIGNAL_SOURCE_PRIORITY)
TRUST_SIGNAL_SEARCH_RESULT_SOURCES = ("timeout", "blog", "web", "brave_search", "google_search")
TRUST_SIGNAL_CONFIDENCE_PRIORITY = {
    "high": 0,
    "medium": 1,
    "low": 2,
}
MICHELIN_TIER_PRIORITY = {
    "3 stars": 0,
    "2 stars": 1,
    "1 star": 2,
    "Bib Gourmand": 3,
    "Green Star": 4,
    "Selected": 5,
}
MICHELIN_REGION_URLS = {
    ("hong kong", "hong kong"): "https://guide.michelin.com/en/hk/hong-kong-region/hong-kong/restaurants",
    ("japan", "tokyo"): "https://guide.michelin.com/en/jp/tokyo-region/restaurants",
    ("japan", "kyoto"): "https://guide.michelin.com/en/jp/kyoto-region/restaurants",
    ("taiwan", "taipei"): "https://guide.michelin.com/tw/en/article/michelin-guide-ceremony/taiwan-full-list",
}
WIKIPEDIA_MICHELIN_REGION_URLS = {
    ("taiwan", "*"): "https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Taiwan",
    ("hong kong", "*"): "https://en.wikipedia.org/wiki/List_of_Michelin-starred_restaurants_in_Hong_Kong_and_Macau",
}
MICHELIN_BASE_URL = "https://guide.michelin.com"
TABELOG_AWARD_RESTAURANTS_URL = "https://award.tabelog.com/en/restaurants"
TABELOG_HYAKUMEITEN_URL = "https://award.tabelog.com/hyakumeiten"
TABELOG_SEARCH_BASE_URL = "https://tabelog.com/en/rstLst/"
RESTAURANT_NAME_STOPWORDS = {
    "bar",
    "bistro",
    "cafe",
    "café",
    "coffee",
    "grill",
    "kitchen",
    "ramen",
    "restaurant",
    "steakhouse",
    "sushi",
}
TABELOG_ELIGIBLE_CATEGORY_TERMS = (
    "bar",
    "bakery",
    "bistro",
    "brewery",
    "cafe",
    "café",
    "coffee",
    "confectionery",
    "dessert",
    "dining",
    "drink",
    "food",
    "izakaya",
    "kitchen",
    "pub",
    "ramen",
    "restaurant",
    "sake",
    "soba",
    "steak",
    "sushi",
    "tea",
    "tempura",
    "udon",
    "wine",
    "yakitori",
    "yakiniku",
)
TABELOG_INELIGIBLE_CATEGORY_TERMS = (
    "airport",
    "aquarium",
    "art gallery",
    "attraction",
    "beach",
    "bridge",
    "bus station",
    "castle",
    "church",
    "department store",
    "garden",
    "guest house",
    "historic",
    "hostel",
    "hotel",
    "inn",
    "landmark",
    "lodging",
    "mall",
    "market",
    "monument",
    "museum",
    "observation",
    "onsen",
    "park",
    "resort",
    "river",
    "scenic",
    "shopping",
    "shrine",
    "spa",
    "station",
    "store",
    "temple",
    "theme park",
    "tourist",
    "trail",
    "train",
    "villa",
    "zoo",
)

metadata = MetaData()

trust_source_snapshots = Table(
    "trust_source_snapshots",
    metadata,
    Column("source_key", String, primary_key=True),
    Column("source_type", String, nullable=False),
    Column("region", String),
    Column("url", Text),
    Column("query", Text),
    Column("fetched_at", String, nullable=False),
    Column("refresh_after", String),
    Column("payload_json", Text, nullable=False),
)

trust_place_signals = Table(
    "trust_place_signals",
    metadata,
    Column("place_key", String, primary_key=True),
    Column("signal_key", String, primary_key=True),
    Column("source", String, nullable=False),
    Column("label", String, nullable=False),
    Column("tier", String),
    Column("award_year", Integer),
    Column("is_current", Integer),
    Column("url", Text),
    Column("title", Text),
    Column("published_at", String),
    Column("fetched_at", String, nullable=False),
    Column("refresh_after", String),
    Column("confidence", String, nullable=False),
    Column("match_reason", Text, nullable=False),
    Column("match_signature", String, nullable=False),
    Column("signal_json", Text, nullable=False),
)

trust_place_source_urls = Table(
    "trust_place_source_urls",
    metadata,
    Column("place_key", String, primary_key=True),
    Column("source", String, primary_key=True),
    Column("url", Text, primary_key=True),
    Column("title", Text),
    Column("fetched_at", String, nullable=False),
    Column("refresh_after", String),
    Column("confidence", String, nullable=False),
    Column("match_reason", Text, nullable=False),
    Column("match_signature", String, nullable=False),
)

class SearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    url: str
    snippet: str | None = None
    published_at: str | None = None


class MichelinRestaurant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    url: str
    tier: str | None = None
    award_year: int | None = None
    is_current: bool | None = True
    region_key: str


class MichelinRegionSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_type: Literal["official", "wikipedia"]
    region_key: str
    url: str


class TabelogRestaurant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    url: str
    label: str
    tier: str | None = None
    award_year: int | None = None
    is_current: bool | None = True
    region_key: str


class TabelogSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_type: Literal["award", "hyakumeiten"]
    region_key: str
    url: str


class PlaceSourceUrl(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: str
    url: str
    title: str | None = None
    fetched_at: str
    refresh_after: str | None = None
    confidence: str
    match_reason: str


class MichelinDetailSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    award_year: int | None = None


@dataclass(frozen=True)
class MichelinMatchContext:
    place_name: str
    city_name: str | None = None
    country_name: str | None = None
    address: str | None = None


class TrustSignalRefreshSummary(BaseModel):
    searched_places: int = 0
    skipped_places: int = 0
    signals_written: int = 0
    provider_failures: int = 0
    michelin_regions_refreshed: int = 0
    michelin_details_refreshed: int = 0
    tabelog_sources_refreshed: int = 0


class TrustSignalStore:
    def __init__(self, store_url: str, *, initialize: bool = True) -> None:
        self.store_url = store_url
        if initialize:
            ensure_sqlite_store_parent_dir(store_url)
        self.engine = create_engine(store_url, future=True)
        if initialize:
            metadata.create_all(self.engine)
            ensure_trust_schema(self.engine)

    def load_signals_for_place_keys(
        self,
        place_keys: Iterable[str],
        *,
        include_low_confidence: bool = False,
        match_signatures_by_key: Mapping[str, set[str]] | None = None,
        now: datetime | None = None,
    ) -> dict[str, list[TrustSignal]]:
        keys = sorted({key for key in place_keys if key})
        if not keys:
            return {}

        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    trust_place_signals.c.place_key,
                    trust_place_signals.c.refresh_after,
                    trust_place_signals.c.match_signature,
                    trust_place_signals.c.signal_json,
                )
                .where(trust_place_signals.c.place_key.in_(keys))
                .order_by(
                    trust_place_signals.c.source,
                    trust_place_signals.c.label,
                    trust_place_signals.c.title,
                )
            ).fetchall()

        signals_by_key: dict[str, list[TrustSignal]] = {}
        for place_key, refresh_after, match_signature, signal_json in rows:
            if not isinstance(place_key, str) or not isinstance(signal_json, str):
                continue
            if now is not None:
                parsed_refresh_after = metadata_datetime_or_none(refresh_after)
                if parsed_refresh_after is not None and parsed_refresh_after <= now:
                    continue
            expected_match_signatures = match_signatures_by_key.get(place_key) if match_signatures_by_key is not None else None
            if expected_match_signatures is not None and match_signature not in expected_match_signatures:
                continue
            signal = TrustSignal.model_validate_json(signal_json)
            if signal.confidence == "low" and not include_low_confidence:
                continue
            signals_by_key.setdefault(place_key, []).append(signal)
        for key, signals in signals_by_key.items():
            signals_by_key[key] = sort_trust_signals(dedupe_trust_signals(signals))
        return signals_by_key

    def place_has_fresh_search_snapshot(self, query: str, *, now: datetime) -> bool:
        source_keys = [
            source_snapshot_key("brave_search", query),
            source_snapshot_key("google_search", query),
        ]
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(trust_source_snapshots.c.refresh_after)
                .where(trust_source_snapshots.c.source_key.in_(source_keys))
            ).fetchall()
        for (refresh_after,) in rows:
            parsed = metadata_datetime_or_none(refresh_after)
            if parsed is not None and parsed > now:
                return True
        return False

    def cached_search_results(self, provider: str, query: str, *, now: datetime) -> list[SearchResult] | None:
        source_key = source_snapshot_key(provider, query)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    trust_source_snapshots.c.fetched_at,
                    trust_source_snapshots.c.refresh_after,
                    trust_source_snapshots.c.payload_json,
                ).where(trust_source_snapshots.c.source_key == source_key)
            ).fetchone()
        if row is None:
            return None
        fetched_at, refresh_after, payload_json = row
        parsed_refresh_after = metadata_datetime_or_none(refresh_after)
        fetched_at_dt = metadata_datetime_or_none(fetched_at)
        policy_refresh_after = search_snapshot_refresh_after(
            provider,
            query,
            now=fetched_at_dt or now,
        )
        if provider == "tabelog_search" and parsed_refresh_after != policy_refresh_after:
            parsed_refresh_after = policy_refresh_after
            with self.engine.begin() as connection:
                connection.execute(
                    trust_source_snapshots.update()
                    .where(trust_source_snapshots.c.source_key == source_key)
                    .values(refresh_after=policy_refresh_after.isoformat())
                )
        if parsed_refresh_after is None or parsed_refresh_after <= now:
            return None
        payload = json.loads(payload_json)
        if not isinstance(payload, list):
            return []
        return [SearchResult.model_validate(item) for item in payload if isinstance(item, dict)]

    def save_search_results(
        self,
        provider: str,
        query: str,
        results: list[SearchResult],
        *,
        now: datetime,
    ) -> None:
        refresh_after = search_snapshot_refresh_after(provider, query, now=now)
        row = {
            "source_key": source_snapshot_key(provider, query),
            "source_type": provider,
            "region": None,
            "url": None,
            "query": query,
            "fetched_at": now.isoformat(),
            "refresh_after": refresh_after.isoformat(),
            "payload_json": json.dumps([result.model_dump(mode="json") for result in results], ensure_ascii=False),
        }
        upsert_row(self.engine, trust_source_snapshots, row, ["source_key"])

    def cached_michelin_restaurants(
        self,
        source: MichelinRegionSource,
        *,
        now: datetime,
    ) -> list[MichelinRestaurant] | None:
        source_key = michelin_source_key(source)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    trust_source_snapshots.c.refresh_after,
                    trust_source_snapshots.c.payload_json,
                ).where(trust_source_snapshots.c.source_key == source_key)
            ).fetchone()
        if row is None:
            return None
        refresh_after, payload_json = row
        parsed_refresh_after = metadata_datetime_or_none(refresh_after)
        if parsed_refresh_after is None or parsed_refresh_after <= now:
            return None
        payload = json.loads(payload_json)
        if not isinstance(payload, list):
            return []
        return [MichelinRestaurant.model_validate(item) for item in payload if isinstance(item, dict)]

    def save_michelin_restaurants(
        self,
        source: MichelinRegionSource,
        restaurants: list[MichelinRestaurant],
        *,
        now: datetime,
    ) -> None:
        refresh_after = now + MICHELIN_REGION_REFRESH_TTL
        row = {
            "source_key": michelin_source_key(source),
            "source_type": f"michelin_{source.source_type}",
            "region": source.region_key,
            "url": source.url,
            "query": None,
            "fetched_at": now.isoformat(),
            "refresh_after": refresh_after.isoformat(),
            "payload_json": json.dumps(
                [restaurant.model_dump(mode="json") for restaurant in restaurants],
                ensure_ascii=False,
            ),
        }
        upsert_row(self.engine, trust_source_snapshots, row, ["source_key"])

    def cached_tabelog_restaurants(
        self,
        source: TabelogSource,
        *,
        now: datetime,
    ) -> list[TabelogRestaurant] | None:
        source_key = tabelog_source_key(source)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    trust_source_snapshots.c.fetched_at,
                    trust_source_snapshots.c.refresh_after,
                    trust_source_snapshots.c.payload_json,
                ).where(trust_source_snapshots.c.source_key == source_key)
            ).fetchone()
        if row is None:
            return None
        fetched_at, refresh_after, payload_json = row
        parsed_refresh_after = metadata_datetime_or_none(refresh_after)
        fetched_at_dt = metadata_datetime_or_none(fetched_at)
        policy_refresh_after = tabelog_source_refresh_after(source, now=fetched_at_dt or now)
        if parsed_refresh_after != policy_refresh_after:
            parsed_refresh_after = policy_refresh_after
            with self.engine.begin() as connection:
                connection.execute(
                    trust_source_snapshots.update()
                    .where(trust_source_snapshots.c.source_key == source_key)
                    .values(refresh_after=policy_refresh_after.isoformat())
                )
        if parsed_refresh_after is None or parsed_refresh_after <= now:
            return None
        payload = json.loads(payload_json)
        if not isinstance(payload, list):
            return []
        return [TabelogRestaurant.model_validate(item) for item in payload if isinstance(item, dict)]

    def save_tabelog_restaurants(
        self,
        source: TabelogSource,
        restaurants: list[TabelogRestaurant],
        *,
        now: datetime,
    ) -> None:
        refresh_after = tabelog_source_refresh_after(source, now=now)
        row = {
            "source_key": tabelog_source_key(source),
            "source_type": f"tabelog_{source.source_type}",
            "region": source.region_key,
            "url": source.url,
            "query": None,
            "fetched_at": now.isoformat(),
            "refresh_after": refresh_after.isoformat(),
            "payload_json": json.dumps(
                [restaurant.model_dump(mode="json") for restaurant in restaurants],
                ensure_ascii=False,
            ),
        }
        upsert_row(self.engine, trust_source_snapshots, row, ["source_key"])

    def save_place_source_urls(
        self,
        place_key: str,
        source_urls: list[PlaceSourceUrl],
        *,
        match_signature: str,
    ) -> None:
        rows = [
            {
                "place_key": place_key,
                "source": source_url.source,
                "url": source_url.url,
                "title": source_url.title,
                "fetched_at": source_url.fetched_at,
                "refresh_after": source_url.refresh_after,
                "confidence": source_url.confidence,
                "match_reason": source_url.match_reason,
                "match_signature": match_signature,
            }
            for source_url in dedupe_place_source_urls(source_urls)
        ]
        with self.engine.begin() as connection:
            upsert_rows(connection, trust_place_source_urls, rows, ["place_key", "source", "url"])

    def load_place_source_urls(
        self,
        place_keys: Iterable[str],
        *,
        source: str,
        match_signature: str,
        now: datetime,
    ) -> list[PlaceSourceUrl]:
        keys = sorted({key for key in place_keys if key})
        if not keys:
            return []

        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    trust_place_source_urls.c.source,
                    trust_place_source_urls.c.url,
                    trust_place_source_urls.c.title,
                    trust_place_source_urls.c.fetched_at,
                    trust_place_source_urls.c.refresh_after,
                    trust_place_source_urls.c.confidence,
                    trust_place_source_urls.c.match_reason,
                )
                .where(
                    and_(
                        trust_place_source_urls.c.place_key.in_(keys),
                        trust_place_source_urls.c.source == source,
                        trust_place_source_urls.c.match_signature == match_signature,
                    )
                )
                .order_by(
                    trust_place_source_urls.c.source,
                    trust_place_source_urls.c.url,
                )
            ).fetchall()

        source_urls: list[PlaceSourceUrl] = []
        for (
            row_source,
            url,
            title,
            fetched_at,
            refresh_after,
            confidence,
            match_reason,
        ) in rows:
            parsed_refresh_after = metadata_datetime_or_none(refresh_after)
            if parsed_refresh_after is not None and parsed_refresh_after <= now:
                continue
            if not isinstance(row_source, str) or not isinstance(url, str):
                continue
            if not isinstance(fetched_at, str) or not isinstance(confidence, str) or not isinstance(match_reason, str):
                continue
            source_urls.append(
                PlaceSourceUrl(
                    source=row_source,
                    url=url,
                    title=title if isinstance(title, str) else None,
                    fetched_at=fetched_at,
                    refresh_after=refresh_after if isinstance(refresh_after, str) else None,
                    confidence=confidence,
                    match_reason=match_reason,
                )
            )
        return dedupe_place_source_urls(source_urls)

    def cached_michelin_detail_snapshot(
        self,
        restaurant_url: str,
        *,
        now: datetime,
    ) -> MichelinDetailSnapshot | None:
        source_key = michelin_detail_source_key(restaurant_url)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    trust_source_snapshots.c.refresh_after,
                    trust_source_snapshots.c.payload_json,
                ).where(trust_source_snapshots.c.source_key == source_key)
            ).fetchone()
        if row is None:
            return None
        refresh_after, payload_json = row
        parsed_refresh_after = metadata_datetime_or_none(refresh_after)
        if parsed_refresh_after is None or parsed_refresh_after <= now:
            return None
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            payload = {}
        return MichelinDetailSnapshot.model_validate(payload)

    def save_michelin_detail_snapshot(
        self,
        restaurant_url: str,
        snapshot: MichelinDetailSnapshot,
        *,
        now: datetime,
    ) -> None:
        refresh_after = now + MICHELIN_REGION_REFRESH_TTL
        row = {
            "source_key": michelin_detail_source_key(restaurant_url),
            "source_type": "michelin_detail",
            "region": None,
            "url": restaurant_url,
            "query": None,
            "fetched_at": now.isoformat(),
            "refresh_after": refresh_after.isoformat(),
            "payload_json": snapshot.model_dump_json(),
        }
        upsert_row(self.engine, trust_source_snapshots, row, ["source_key"])

    def replace_search_signals(
        self,
        place_key: str,
        signals: list[TrustSignal],
        *,
        match_signature: str,
        now: datetime,
        replaceable_sources: Iterable[str] = TRUST_SIGNAL_REPLACEABLE_SOURCES,
    ) -> None:
        refresh_after = now + TRUST_SIGNAL_REFRESH_TTL
        sources = tuple(replaceable_sources)
        rows = [
            trust_signal_row(
                place_key,
                signal,
                match_signature=match_signature,
                refresh_after=refresh_after,
            )
            for signal in dedupe_trust_signals(signals)
        ]
        with self.engine.begin() as connection:
            connection.execute(
                delete(trust_place_signals).where(
                    and_(
                        trust_place_signals.c.place_key == place_key,
                        trust_place_signals.c.source.in_(sources),
                    )
                )
            )
            upsert_rows(connection, trust_place_signals, rows, ["place_key", "signal_key"])


def upsert_row(engine: Engine, table: Table, row: dict[str, Any], primary_key_columns: list[str]) -> None:
    with engine.begin() as connection:
        upsert_rows(connection, table, [row], primary_key_columns)


def upsert_rows(connection: Any, table: Table, rows: list[dict[str, Any]], primary_key_columns: list[str]) -> None:
    if not rows:
        return

    dialect_name = connection.engine.dialect.name
    update_columns = [
        key
        for key in rows[0]
        if key not in primary_key_columns
    ]
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        statement = sqlite_insert(table).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=primary_key_columns,
            set_={
                column_name: statement.excluded[column_name]
                for column_name in update_columns
            },
        )
        connection.execute(statement)
        return

    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as postgres_insert

        statement = postgres_insert(table).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=primary_key_columns,
            set_={
                column_name: statement.excluded[column_name]
                for column_name in update_columns
            },
        )
        connection.execute(statement)
        return

    for row in rows:
        clauses = [
            table.c[column_name] == row[column_name]
            for column_name in primary_key_columns
        ]
        connection.execute(delete(table).where(and_(*clauses)))
        connection.execute(insert(table).values(row))


def ensure_trust_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        rows = connection.exec_driver_sql("PRAGMA table_info(trust_place_signals)").fetchall()
        column_names = {row[1] for row in rows}
        if "award_year" not in column_names:
            connection.exec_driver_sql("ALTER TABLE trust_place_signals ADD COLUMN award_year INTEGER")
        if "is_current" not in column_names:
            connection.exec_driver_sql("ALTER TABLE trust_place_signals ADD COLUMN is_current INTEGER")


def sqlite_bool_or_none(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def resolve_trust_store_url(root: Path, *, env: Mapping[str, str] | None = None) -> str:
    env = env or os.environ
    configured_store_url = env.get(TRUST_STORE_URL_ENV)
    if configured_store_url:
        return configured_store_url

    return sqlite_url_for_path(default_trust_cache_path(root, env=env))


def default_trust_cache_path(root: Path, *, env: Mapping[str, str] | None = None) -> Path:
    env = env or os.environ
    cache_root = env.get("XDG_CACHE_HOME")
    if cache_root:
        return Path(cache_root).expanduser() / "favorite-places" / "trust-signals" / "trust.sqlite"
    return Path.home() / ".cache" / "favorite-places" / "trust-signals" / "trust.sqlite"


def canonical_repo_root(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    common_dir_text = result.stdout.strip()
    if not common_dir_text:
        return None
    common_dir = Path(common_dir_text)
    return common_dir.parent if common_dir.name == ".git" else None


def resolve_path(value: str, *, root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def sqlite_url_for_path(path: Path) -> str:
    return f"sqlite:///{path}"


def ensure_sqlite_store_parent_dir(store_url: str) -> None:
    parsed_url = make_url(store_url)
    if parsed_url.drivername.split("+", 1)[0] != "sqlite":
        return
    sqlite_database = parsed_url.database
    if not sqlite_database or sqlite_database == ":memory:":
        return
    if str(parsed_url.query.get("uri", "")).lower() == "true" and sqlite_database.startswith("file:"):
        sqlite_database = sqlite_database.removeprefix("file:")
    if sqlite_database == ":memory:":
        return
    Path(sqlite_database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def load_trust_signals_for_places(
    store: TrustSignalStore,
    slug: str,
    raw: RawSavedList,
    enrichment_cache: Mapping[str, EnrichmentCacheEntry],
    *,
    stable_place_ids: Mapping[tuple[str, int], str],
    location_context: tuple[str | None, str | None] | None = None,
    now: datetime | None = None,
) -> dict[str, list[TrustSignal]]:
    lookup_keys: dict[str, list[str]] = {}
    match_signatures_by_key: dict[str, set[str]] = {}
    all_keys: set[str] = set()
    city_name = (
        location_context[0]
        if location_context is not None and location_context[0]
        else infer_city_name(raw.title or slug)
    )
    country_name = (
        location_context[1]
        if location_context is not None and location_context[1]
        else infer_country_name(raw.title or slug)
    )
    blocked_cid_alias_keys = raw_saved_list_current_cid_keys(raw)
    for index, place in enumerate(raw.places):
        place_id = stable_place_ids.get((slug, index))
        if place_id is None:
            continue
        match_signature = trust_match_signature(place.name, city_name, country_name)
        keys = trust_place_keys(
            place,
            place_id=place_id,
            enrichment_entry=enrichment_cache.get(place_id),
            blocked_cid_alias_keys=blocked_cid_alias_keys,
        )
        lookup_keys[place_id] = keys
        all_keys.update(keys)
        for key in keys:
            match_signatures_by_key.setdefault(key, set()).add(match_signature)

    signals_by_key = store.load_signals_for_place_keys(
        all_keys,
        match_signatures_by_key=match_signatures_by_key,
        now=now or datetime.now(UTC),
    )
    signals_by_place_id: dict[str, list[TrustSignal]] = {}
    for place_id, keys in lookup_keys.items():
        signals: list[TrustSignal] = []
        for key in keys:
            signals.extend(signals_by_key.get(key, []))
        signals_by_place_id[place_id] = sort_trust_signals(dedupe_trust_signals(signals))
    return signals_by_place_id


def refresh_trust_signals_for_raw_guides(
    *,
    root: Path,
    raw_lists: Mapping[str, RawSavedList],
    enrichment_caches: Mapping[str, Mapping[str, EnrichmentCacheEntry]],
    stable_place_ids: Mapping[tuple[str, int], str],
    guide_location_contexts: Mapping[str, tuple[str | None, str | None]] | None = None,
    force_refresh: bool = False,
    include_google_fallback: bool = False,
) -> TrustSignalRefreshSummary:
    store = TrustSignalStore(resolve_trust_store_url(root))
    now = datetime.now(UTC)
    summary = TrustSignalRefreshSummary()
    brave_api_key = os.environ.get(BRAVE_SEARCH_API_KEY_ENV) or os.environ.get(BRAVE_API_KEY_ENV)
    google_fallback_enabled = include_google_fallback or env_flag_enabled(GOOGLE_FALLBACK_ENV)
    michelin_sources_by_guide = michelin_region_sources_for_guides(
        raw_lists,
        guide_location_contexts=guide_location_contexts,
    )
    tabelog_sources_by_guide = tabelog_sources_for_guides(
        raw_lists,
        guide_location_contexts=guide_location_contexts,
    )
    michelin_restaurants_by_source: dict[tuple[str, str], list[MichelinRestaurant]] = {}
    tabelog_restaurants_by_source: dict[tuple[str, str], list[TabelogRestaurant]] = {}
    unique_sources = {
        (source.source_type, source.region_key, source.url): source
        for sources in michelin_sources_by_guide.values()
        for source in sources
    }
    for source in sorted(unique_sources.values(), key=lambda value: (value.source_type, value.region_key, value.url)):
        restaurants = store.cached_michelin_restaurants(source, now=now)
        if restaurants is None or force_refresh:
            restaurants = scrape_michelin_region_source(source)
            store.save_michelin_restaurants(source, restaurants, now=now)
            summary.michelin_regions_refreshed += 1
        michelin_restaurants_by_source[(source.source_type, source.region_key)] = restaurants

    unique_tabelog_sources = {
        (source.source_type, source.region_key, source.url): source
        for sources in tabelog_sources_by_guide.values()
        for source in sources
    }
    for source in sorted(
        unique_tabelog_sources.values(),
        key=lambda value: (value.source_type, value.region_key, value.url),
    ):
        restaurants = store.cached_tabelog_restaurants(source, now=now)
        if restaurants is None or force_refresh:
            restaurants = scrape_tabelog_source(source)
            store.save_tabelog_restaurants(source, restaurants, now=now)
            summary.tabelog_sources_refreshed += 1
        tabelog_restaurants_by_source[(source.source_type, source.region_key)] = restaurants

    for slug, raw in sorted(raw_lists.items()):
        location_context = guide_location_contexts.get(slug) if guide_location_contexts is not None else None
        city_name = (
            location_context[0]
            if location_context is not None and location_context[0]
            else infer_city_name(raw.title or slug)
        )
        country_name = (
            location_context[1]
            if location_context is not None and location_context[1]
            else infer_country_name(raw.title or slug)
        )
        enrichment_cache = enrichment_caches.get(slug, {})
        michelin_restaurants = [
            restaurant
            for source in michelin_sources_by_guide.get(slug, [])
            for restaurant in michelin_restaurants_by_source.get((source.source_type, source.region_key), [])
        ]
        tabelog_restaurants = [
            restaurant
            for source in tabelog_sources_by_guide.get(slug, [])
            for restaurant in tabelog_restaurants_by_source.get((source.source_type, source.region_key), [])
        ]
        guide_has_michelin_sources = bool(michelin_sources_by_guide.get(slug))
        guide_has_tabelog_sources = bool(tabelog_sources_by_guide.get(slug))
        blocked_cid_alias_keys = raw_saved_list_current_cid_keys(raw)
        for index, place in enumerate(raw.places):
            place_id = stable_place_ids.get((slug, index))
            if place_id is None:
                continue
            keys = trust_place_keys(
                place,
                place_id=place_id,
                enrichment_entry=enrichment_cache.get(place_id),
                blocked_cid_alias_keys=blocked_cid_alias_keys,
            )
            query = trust_search_query(
                place.name,
                city_name=city_name,
                country_name=country_name,
            )

            results: list[SearchResult] = []
            provider_failures = 0
            failed_replaceable_sources: set[str] = set()
            search_snapshot_is_fresh = store.place_has_fresh_search_snapshot(query, now=now)
            if search_snapshot_is_fresh and not force_refresh:
                results = (
                    store.cached_search_results("brave_search", query, now=now)
                    or store.cached_search_results("google_search", query, now=now)
                    or []
                )
            elif brave_api_key:
                try:
                    results = cached_or_fetch_search_results(
                        store,
                        "brave_search",
                        query,
                        now=now,
                        fetch=lambda: brave_search(query, api_key=brave_api_key),
                    )
                except (HTTPError, URLError, OSError, ValueError) as exc:
                    provider_failures += 1
                    failed_replaceable_sources.update(TRUST_SIGNAL_SEARCH_RESULT_SOURCES)
                    print(f"WARNING: Brave trust search failed for {place.name}: {exc}", flush=True)
            if not results and google_fallback_enabled:
                try:
                    results = cached_or_fetch_search_results(
                        store,
                        "google_search",
                        query,
                        now=now,
                        fetch=lambda: google_search_html(query),
                    )
                except (HTTPError, URLError, OSError, ValueError) as exc:
                    provider_failures += 1
                    failed_replaceable_sources.update(TRUST_SIGNAL_SEARCH_RESULT_SOURCES)
                    print(f"WARNING: Google fallback trust search failed for {place.name}: {exc}", flush=True)
            michelin_signals = signals_from_michelin_region(
                michelin_restaurants,
                context=MichelinMatchContext(
                    place_name=place.name,
                    city_name=city_name,
                    country_name=country_name,
                    address=place.address,
                ),
                fetched_at=now,
            )
            detail_result = enrich_michelin_signal_award_years(
                store,
                michelin_signals,
                now=now,
                force_refresh=force_refresh,
            )
            michelin_signals = detail_result.signals
            summary.michelin_details_refreshed += detail_result.fetched_details
            summary.provider_failures += detail_result.provider_failures
            tabelog_signals = signals_from_tabelog_restaurants(
                tabelog_restaurants,
                context=MichelinMatchContext(
                    place_name=place.name,
                    city_name=city_name,
                    country_name=country_name,
                    address=place.address,
                ),
                fetched_at=now,
            )
            match_signature = trust_match_signature(place.name, city_name, country_name)
            existing_signals_by_key = store.load_signals_for_place_keys(
                keys,
                include_low_confidence=True,
                match_signatures_by_key={key: {match_signature} for key in keys},
                now=now,
            )
            existing_tabelog_direct_signals = sort_trust_signals(
                dedupe_trust_signals(
                    signal
                    for signals in existing_signals_by_key.values()
                    for signal in signals
                    if signal.source == "tabelog"
                    and signal.match_reason == "Tabelog direct search URL match"
                )
            )
            saved_tabelog_source_urls = store.load_place_source_urls(
                keys,
                source="tabelog",
                match_signature=match_signature,
                now=now,
            )
            legacy_tabelog_source_urls = place_source_urls_from_tabelog_signals(
                existing_tabelog_direct_signals,
                fetched_at=now,
                refresh_after=search_snapshot_refresh_after(
                    "tabelog_search",
                    tabelog_search_query(place.name),
                    now=now,
                ),
            )
            if legacy_tabelog_source_urls:
                for key in keys:
                    store.save_place_source_urls(
                        key,
                        legacy_tabelog_source_urls,
                        match_signature=match_signature,
                    )
                saved_tabelog_source_urls = dedupe_place_source_urls(
                    [*saved_tabelog_source_urls, *legacy_tabelog_source_urls]
                )
            saved_tabelog_url_signals = signals_from_tabelog_source_urls(
                tabelog_restaurants,
                saved_tabelog_source_urls,
                fetched_at=now,
            )
            tabelog_search_signals: list[TrustSignal] = []
            should_search_tabelog = tabelog_search_place_is_eligible(
                place,
                enrichment_entry=enrichment_cache.get(place_id),
                has_tabelog_source_match=bool(
                    tabelog_signals
                    or saved_tabelog_source_urls
                    or existing_tabelog_direct_signals
                ),
            )
            if (
                guide_has_tabelog_sources
                and tabelog_restaurants
                and should_search_tabelog
                and not saved_tabelog_url_signals
                and not existing_tabelog_direct_signals
            ):
                tabelog_query = tabelog_search_query(place.name)
                try:
                    tabelog_results = cached_or_fetch_search_results(
                        store,
                        "tabelog_search",
                        tabelog_query,
                        now=now,
                        fetch=lambda: tabelog_search(tabelog_query),
                    )
                except (HTTPError, URLError, OSError, ValueError) as exc:
                    provider_failures += 1
                    failed_replaceable_sources.add("tabelog")
                    print(f"WARNING: Tabelog trust search failed for {place.name}: {exc}", flush=True)
                else:
                    tabelog_search_signals = signals_from_tabelog_search_results(
                        tabelog_restaurants,
                        tabelog_results,
                        place_name=place.name,
                        fetched_at=now,
                    )
                    tabelog_source_urls = place_source_urls_from_tabelog_search_results(
                        tabelog_results,
                        place_name=place.name,
                        fetched_at=now,
                        refresh_after=search_snapshot_refresh_after(
                            "tabelog_search",
                            tabelog_query,
                            now=now,
                        ),
                    )
                    if tabelog_source_urls:
                        for key in keys:
                            store.save_place_source_urls(
                                key,
                                tabelog_source_urls,
                                match_signature=match_signature,
                            )
            if (
                not results
                and not michelin_signals
                and not tabelog_signals
                and not saved_tabelog_url_signals
                and not existing_tabelog_direct_signals
                and not tabelog_search_signals
                and not brave_api_key
                and not google_fallback_enabled
            ):
                replaceable_sources = [
                    source
                    for source, enabled in (
                        ("michelin", guide_has_michelin_sources),
                        ("tabelog", guide_has_tabelog_sources),
                    )
                    if enabled and source not in failed_replaceable_sources
                ]
                if replaceable_sources:
                    for key in keys:
                        store.replace_search_signals(
                            key,
                            [],
                            match_signature=match_signature,
                            now=now,
                            replaceable_sources=replaceable_sources,
                        )
                    summary.searched_places += 1
                    summary.provider_failures += provider_failures
                    continue
                summary.skipped_places += 1
                continue

            signals = [
                *michelin_signals,
                *tabelog_signals,
                *saved_tabelog_url_signals,
                *existing_tabelog_direct_signals,
                *tabelog_search_signals,
                *search_signals_without_duplicate_award_urls(
                    [
                        *michelin_signals,
                        *tabelog_signals,
                        *saved_tabelog_url_signals,
                        *existing_tabelog_direct_signals,
                        *tabelog_search_signals,
                    ],
                    signals_from_search_results(
                        results,
                        place_name=place.name,
                        city_name=city_name,
                        country_name=country_name,
                        fetched_at=now,
                    ),
                ),
            ]
            for key in keys:
                store.replace_search_signals(
                    key,
                    signals,
                    match_signature=match_signature,
                    now=now,
                    replaceable_sources=[
                        source
                        for source in TRUST_SIGNAL_REPLACEABLE_SOURCES
                        if source not in failed_replaceable_sources
                    ],
                )
            summary.searched_places += 1
            summary.signals_written += len(signals) * len(keys)
            summary.provider_failures += provider_failures

    return summary


def cached_or_fetch_search_results(
    store: TrustSignalStore,
    provider: str,
    query: str,
    *,
    now: datetime,
    fetch: Any,
) -> list[SearchResult]:
    cached = store.cached_search_results(provider, query, now=now)
    if cached is not None:
        return cached
    results = fetch()
    store.save_search_results(provider, query, results, now=now)
    return results


def michelin_region_sources_for_guides(
    raw_lists: Mapping[str, RawSavedList],
    *,
    guide_location_contexts: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> dict[str, list[MichelinRegionSource]]:
    configured_urls = configured_michelin_region_urls()
    sources: dict[str, list[MichelinRegionSource]] = {}
    for slug, raw in raw_lists.items():
        country_name, city_name = michelin_region_lookup_key(
            slug,
            raw,
            location_context=guide_location_contexts.get(slug) if guide_location_contexts is not None else None,
        )
        key = (country_name, city_name)
        guide_sources: list[MichelinRegionSource] = []
        official_url = configured_urls.get(key) or MICHELIN_REGION_URLS.get(key)
        if official_url is not None:
            guide_sources.append(
                MichelinRegionSource(
                    source_type="official",
                    region_key=f"{country_name}/{city_name}",
                    url=official_url,
                )
            )
        wikipedia_url = (
            WIKIPEDIA_MICHELIN_REGION_URLS.get(key)
            or WIKIPEDIA_MICHELIN_REGION_URLS.get((country_name, "*"))
        )
        if wikipedia_url is not None:
            guide_sources.append(
                MichelinRegionSource(
                    source_type="wikipedia",
                    region_key=f"{country_name}/wikipedia",
                    url=wikipedia_url,
                )
            )
        if guide_sources:
            sources[slug] = guide_sources
    return sources


def michelin_region_lookup_key(
    slug: str,
    raw: RawSavedList,
    *,
    location_context: tuple[str | None, str | None] | None = None,
) -> tuple[str, str]:
    context_city, context_country = location_context or (None, None)
    text = normalize_search_text(
        " ".join(
            value
            for value in [
                slug,
                raw.title or "",
                raw.description or "",
                context_city or "",
                context_country or "",
            ]
            if value
        )
    )
    if "hong kong" in text:
        return "hong kong", "hong kong"
    if "taipei" in text or "taiwan" in text:
        return "taiwan", "taipei"
    if "tokyo" in text:
        return "japan", "tokyo"
    if "kyoto" in text:
        return "japan", "kyoto"
    city_name = normalize_region_token(context_city or infer_city_name(raw.title or slug) or "")
    country_name = normalize_region_token(context_country or infer_country_name(raw.title or slug) or "")
    return country_name, city_name


def normalize_region_token(value: str) -> str:
    normalized = re.sub(r"\([^)]*\)", " ", value)
    normalized = normalize_search_text(normalized)
    for suffix in (" example", " recommendations", " guide"):
        normalized = normalized.removesuffix(suffix)
    return normalized.strip()


def configured_michelin_region_urls() -> dict[tuple[str, str], str]:
    raw_value = os.environ.get(MICHELIN_REGION_URLS_ENV)
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[tuple[str, str], str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        parts = [normalize_search_text(part) for part in key.split("/") if normalize_search_text(part)]
        if len(parts) != 2:
            continue
        result[(parts[0], parts[1])] = value
    return result


def tabelog_sources_for_guides(
    raw_lists: Mapping[str, RawSavedList],
    *,
    guide_location_contexts: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> dict[str, list[TabelogSource]]:
    configured_urls = configured_tabelog_source_urls()
    sources: dict[str, list[TabelogSource]] = {}
    for slug, raw in raw_lists.items():
        location_context = guide_location_contexts.get(slug) if guide_location_contexts is not None else None
        context_country = location_context[1] if location_context is not None else None
        country_name = normalize_region_token(context_country or infer_country_name(raw.title or slug) or "")
        lookup_country, _ = michelin_region_lookup_key(slug, raw, location_context=location_context)
        if country_name != "japan" and lookup_country != "japan":
            continue
        urls = configured_urls or {
            "award": TABELOG_AWARD_RESTAURANTS_URL,
            "hyakumeiten": TABELOG_HYAKUMEITEN_URL,
        }
        sources[slug] = [
            TabelogSource(source_type=source_type, region_key="japan", url=url)
            for source_type, url in urls.items()
        ]
    return sources


def configured_tabelog_source_urls() -> dict[Literal["award", "hyakumeiten"], str]:
    raw_value = os.environ.get(TABELOG_SOURCE_URLS_ENV)
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[Literal["award", "hyakumeiten"], str] = {}
    for key, value in payload.items():
        if key not in {"award", "hyakumeiten"} or not isinstance(value, str):
            continue
        result[key] = value
    return result


def scrape_michelin_region_source(source: MichelinRegionSource) -> list[MichelinRestaurant]:
    if source.source_type == "wikipedia":
        return scrape_wikipedia_michelin_region(source)
    return scrape_official_michelin_region(source)


def scrape_official_michelin_region(source: MichelinRegionSource) -> list[MichelinRestaurant]:
    if "/article/" in source.url:
        body = fetch_text(source.url)
        return parse_michelin_full_list_article_page(
            body,
            region_key=source.region_key,
            page_url=source.url,
        )

    restaurants: dict[tuple[str, str, str], MichelinRestaurant] = {}
    next_url: str | None = source.url
    seen_pages: set[str] = set()
    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        body = fetch_text(next_url)
        for restaurant in parse_michelin_region_page(body, region_key=source.region_key, page_url=next_url):
            restaurants[(normalize_search_text(restaurant.name), restaurant.url, restaurant.tier or "")] = restaurant
        next_url = michelin_next_page_url(body, page_url=next_url)
    return sorted(restaurants.values(), key=lambda restaurant: normalize_search_text(restaurant.name))


def scrape_tabelog_source(source: TabelogSource) -> list[TabelogRestaurant]:
    if source.source_type == "hyakumeiten":
        return scrape_tabelog_hyakumeiten(source)
    return scrape_tabelog_awards(source)


def scrape_tabelog_awards(source: TabelogSource) -> list[TabelogRestaurant]:
    restaurants: dict[tuple[str, str, str, int | None], TabelogRestaurant] = {}
    next_url: str | None = source.url
    seen_pages: set[str] = set()
    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        body = fetch_text(next_url)
        for restaurant in parse_tabelog_award_page(body, region_key=source.region_key, page_url=next_url):
            restaurants[
                (
                    normalize_search_text(restaurant.name),
                    canonical_tabelog_url(restaurant.url),
                    restaurant.tier or "",
                    restaurant.award_year,
                )
            ] = restaurant
        next_url = tabelog_next_page_url(body, page_url=next_url)
    return sorted(restaurants.values(), key=lambda restaurant: normalize_search_text(restaurant.name))


def scrape_tabelog_hyakumeiten(source: TabelogSource) -> list[TabelogRestaurant]:
    index_body = fetch_text(source.url)
    page_urls = tabelog_hyakumeiten_category_urls(index_body, page_url=source.url)
    restaurants: dict[tuple[str, str, str, int | None], TabelogRestaurant] = {}
    for page_url in page_urls:
        body = fetch_text(page_url)
        for restaurant in parse_tabelog_hyakumeiten_page(body, region_key=source.region_key, page_url=page_url):
            restaurants[
                (
                    normalize_search_text(restaurant.name),
                    canonical_tabelog_url(restaurant.url),
                    restaurant.tier or "",
                    restaurant.award_year,
                )
            ] = restaurant
    return sorted(restaurants.values(), key=lambda restaurant: normalize_search_text(restaurant.name))


def scrape_wikipedia_michelin_region(source: MichelinRegionSource) -> list[MichelinRestaurant]:
    body = fetch_text(source.url)
    return parse_wikipedia_michelin_starred_page(
        body,
        region_key=source.region_key,
        page_url=source.url,
    )


class MichelinSignalDetailEnrichmentResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signals: list[TrustSignal]
    fetched_details: int = 0
    changed_signals: int = 0
    provider_failures: int = 0


def enrich_michelin_signal_award_years(
    store: TrustSignalStore,
    signals: list[TrustSignal],
    *,
    now: datetime,
    force_refresh: bool = False,
) -> MichelinSignalDetailEnrichmentResult:
    detail_urls = sorted(
        {
            signal.url
            for signal in signals
            if signal.source == "michelin"
            and signal.award_year is None
            and signal.is_current is True
            and signal.url is not None
            and is_official_michelin_restaurant_url(signal.url)
        }
    )
    years_by_url: dict[str, int | None] = {}
    fetched_details = 0
    provider_failures = 0
    for restaurant_url in detail_urls:
        snapshot = None if force_refresh else store.cached_michelin_detail_snapshot(restaurant_url, now=now)
        if snapshot is None:
            try:
                body = fetch_text(restaurant_url)
            except (HTTPError, URLError, OSError, ValueError) as exc:
                provider_failures += 1
                print(f"WARNING: MICHELIN detail-year fetch failed for {restaurant_url}: {exc}", flush=True)
                continue
            snapshot = MichelinDetailSnapshot(
                award_year=parse_michelin_detail_page_award_year(body, page_url=restaurant_url)
            )
            store.save_michelin_detail_snapshot(restaurant_url, snapshot, now=now)
            fetched_details += 1
        years_by_url[restaurant_url] = snapshot.award_year

    if not years_by_url:
        return MichelinSignalDetailEnrichmentResult(
            signals=signals,
            fetched_details=fetched_details,
            provider_failures=provider_failures,
        )

    enriched: list[TrustSignal] = []
    changed_signals = 0
    for signal in signals:
        award_year = years_by_url.get(signal.url or "")
        if signal.award_year is None and award_year is not None:
            enriched.append(signal.model_copy(update={"award_year": award_year}))
            changed_signals += 1
        else:
            enriched.append(signal)
    return MichelinSignalDetailEnrichmentResult(
        signals=sort_trust_signals(dedupe_trust_signals(enriched)),
        fetched_details=fetched_details,
        changed_signals=changed_signals,
        provider_failures=provider_failures,
    )


def is_official_michelin_restaurant_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() == "guide.michelin.com" and "/restaurant/" in parsed.path


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": TRUST_SIGNAL_USER_AGENT})
    with urlopen(request, timeout=TRUST_SIGNAL_HTTP_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


MICHELIN_REGION_CARD_RE = re.compile(
    r"<(?:article|div|li)\b[^>]*class=[\"'](?:[^\"']*\s)?card__menu(?:\s[^\"']*)?[\"'][^>]*>",
    flags=re.IGNORECASE,
)


def michelin_region_card_window(body: str, *, name_start: int, name_end: int) -> str:
    card_start: int | None = None
    for card_match in MICHELIN_REGION_CARD_RE.finditer(
        body,
        max(0, name_start - 5000),
        name_start,
    ):
        card_start = card_match.start()
    if card_start is not None:
        next_card = MICHELIN_REGION_CARD_RE.search(body, name_end)
        card_end = next_card.start() if next_card else min(len(body), card_start + 6000)
        return body[card_start:card_end]

    next_name = body.find("data-restaurant-name=", name_end)
    after_name = body[name_end : next_name if next_name >= 0 else min(len(body), name_end + 4000)]
    if re.search(r'href="[^"]*/restaurant/[^"]+"', after_name):
        return after_name

    previous_name = body.rfind("data-restaurant-name=", max(0, name_start - 1200), name_start)
    before_name = body[(previous_name if previous_name >= 0 else max(0, name_start - 1200)) : name_start]
    href_matches = list(re.finditer(r'href="[^"]*/restaurant/[^"]+"', before_name))
    if href_matches:
        return href_matches[-1].group(0)
    return body[max(0, name_start - 1200) : min(len(body), name_end + 4000)]


def parse_michelin_region_page(
    body: str,
    *,
    region_key: str,
    page_url: str,
) -> list[MichelinRestaurant]:
    restaurants: dict[tuple[str, str, str], MichelinRestaurant] = {}
    for match in re.finditer(r'data-restaurant-name="(?P<name>[^"]+)"', body):
        name = strip_html(match.group("name"))
        if not name:
            continue
        window = michelin_region_card_window(body, name_start=match.start(), name_end=match.end())
        href_match = re.search(r'href="(?P<href>[^"]*/restaurant/[^"]+)"', window)
        if href_match is None:
            continue
        url = canonical_michelin_restaurant_url(urljoin(page_url, html.unescape(href_match.group("href"))))
        tag_start = body.rfind("<", 0, match.start())
        tag_end = body.find(">", match.end())
        tag = body[tag_start : tag_end + 1] if tag_start >= 0 and tag_end >= 0 else ""
        distinction_match = re.search(r'data-dtm-distinction="(?P<distinction>[^"]*)"', tag)
        if distinction_match is None:
            distinction_match = re.search(r'data-dtm-distinction="(?P<distinction>[^"]*)"', window)
        distinction = strip_html(distinction_match.group("distinction")) if distinction_match else None
        normalized_name = normalize_search_text(name)
        tier = michelin_tier(normalize_search_text(distinction or "")) or "Selected"
        restaurants[(normalized_name, url, tier)] = MichelinRestaurant(
            name=name,
            url=url,
            tier=tier,
            award_year=infer_award_year(" ".join([name, url, distinction or ""])),
            is_current=True,
            region_key=region_key,
        )
        if re.search(r'data-green-star="(?:true|1|green[^"]*)"', tag, flags=re.IGNORECASE):
            restaurants[(normalized_name, url, "Green Star")] = MichelinRestaurant(
                name=name,
                url=url,
                tier="Green Star",
                award_year=infer_award_year(" ".join([name, url, distinction or ""])),
                is_current=True,
                region_key=region_key,
            )
    return list(restaurants.values())


def parse_tabelog_award_page(
    body: str,
    *,
    region_key: str,
    page_url: str,
) -> list[TabelogRestaurant]:
    award_year = infer_award_year(tabelog_page_title(body) or page_url)
    restaurants: dict[tuple[str, str, str], TabelogRestaurant] = {}
    for item_html in html_blocks_by_class(body, "li", "award-rstlst__item"):
        href = first_href(item_html)
        name = first_class_text(item_html, "award-rstlst__rst-name")
        if not href or not name:
            continue
        url = canonical_tabelog_url(urljoin(page_url, html.unescape(href)))
        tiers = tabelog_award_tiers(item_html)
        for tier in tiers or [None]:
            restaurants[(normalize_search_text(name), url, tier or "")] = TabelogRestaurant(
                name=name,
                url=url,
                label="The Tabelog Award",
                tier=tier,
                award_year=award_year,
                is_current=True,
                region_key=region_key,
            )
    return list(restaurants.values())


def parse_tabelog_hyakumeiten_page(
    body: str,
    *,
    region_key: str,
    page_url: str,
) -> list[TabelogRestaurant]:
    page_title = tabelog_page_title(body) or ""
    award_year = infer_award_year(page_title or page_url)
    tier = tabelog_hyakumeiten_tier(page_title)
    restaurants: dict[tuple[str, str], TabelogRestaurant] = {}
    for item_html in html_blocks_by_class(body, "div", "hyakumeiten-shop__item"):
        href = first_href(item_html)
        name = first_class_text(item_html, "hyakumeiten-shop__name")
        if not href or not name:
            continue
        url = canonical_tabelog_url(urljoin(page_url, html.unescape(href)))
        restaurants[(normalize_search_text(name), url)] = TabelogRestaurant(
            name=name,
            url=url,
            label="Tabelog Hyakumeiten",
            tier=tier,
            award_year=award_year,
            is_current=True,
            region_key=region_key,
        )
    return list(restaurants.values())


def tabelog_hyakumeiten_category_urls(body: str, *, page_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'href="(?P<href>/hyakumeiten/[^"#?]+)"', body):
        url = urljoin(page_url, html.unescape(match.group("href")))
        if url == page_url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def tabelog_next_page_url(body: str, *, page_url: str) -> str | None:
    current_page = tabelog_current_page_number(page_url)
    candidates: list[tuple[int, str]] = []
    for match in re.finditer(r'href="(?P<href>[^"]*restaurants[^"]*page=(?P<page>[0-9]+)[^"]*)"', body):
        page = int(match.group("page"))
        if page <= current_page:
            continue
        candidates.append((page, urljoin(page_url, html.unescape(match.group("href")))))
    return min(candidates)[1] if candidates else None


def tabelog_current_page_number(page_url: str) -> int:
    values = parse_qs(urlparse(page_url).query).get("page")
    if not values:
        return 1
    try:
        return int(values[0])
    except ValueError:
        return 1


def tabelog_page_title(body: str) -> str | None:
    match = re.search(r"<title\b[^>]*>(?P<title>.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    return strip_html(match.group("title"))


def tabelog_award_tiers(item_html: str) -> list[str]:
    tiers: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'<span\b[^>]*class="[^"]*award-rstlst__award-label[^"]*"[^>]*>(?P<label>.*?)</span>',
        item_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        tier = tabelog_award_tier_from_label(strip_html(match.group("label")))
        if tier and tier not in seen:
            seen.add(tier)
            tiers.append(tier)
    return tiers


def tabelog_award_tier_from_label(value: str) -> str | None:
    normalized = normalize_search_text(value)
    if "chef" in normalized and "gold" in normalized:
        return "Chefs' Gold"
    if "regional" in normalized:
        return "Best Regional Restaurants"
    if "new" in normalized:
        return "Best New Entry"
    return tabelog_tier(normalized)


def tabelog_hyakumeiten_tier(page_title: str) -> str:
    title = re.sub(r"\s*\[[^\]]+\]\s*$", "", page_title)
    title = re.sub(r"^食べログ\s*", "", title)
    title = re.sub(r"\s*20[0-9]{2}\s*$", "", title)
    title = strip_html(title)
    if not title:
        return "Hyakumeiten"
    return title


def html_blocks_by_class(body: str, tag_name: str, class_name: str) -> list[str]:
    blocks: list[str] = []
    pattern = re.compile(
        rf"<{tag_name}\b[^>]*class=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'][^>]*>",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(body):
        end = find_matching_html_end(body, match.start(), tag_name)
        if end is not None:
            blocks.append(body[match.start():end])
    return blocks


def first_class_text(body: str, class_name: str) -> str | None:
    pattern = re.compile(
        rf"<[^>]*class=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'][^>]*>(?P<value>.*?)</[^>]+>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(body)
    if match is None:
        return None
    value = strip_html(match.group("value"))
    return value or None


def first_href(body: str) -> str | None:
    match = re.search(r'<a\b[^>]*href="(?P<href>[^"]+)"', body, flags=re.IGNORECASE)
    return html.unescape(match.group("href")) if match else None


def first_attr_value(body: str, attr_name: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(attr_name)}=[\"'](?P<value>[^\"']+)[\"']",
        body,
        flags=re.IGNORECASE,
    )
    return html.unescape(match.group("value")) if match else None


def parse_michelin_full_list_article_page(
    body: str,
    *,
    region_key: str,
    page_url: str,
) -> list[MichelinRestaurant]:
    award_year = infer_award_year(" ".join([page_url, body[:4000]]))
    restaurants: dict[tuple[str, str], MichelinRestaurant] = {}
    for section_html, tier in michelin_full_list_sections(body):
        for paragraph_match in re.finditer(r"<p\b[^>]*>(?P<paragraph>.*?)</p>", section_html, flags=re.IGNORECASE | re.DOTALL):
            paragraph = paragraph_match.group("paragraph")
            for segment_html in re.split(r"<br\s*/?>", paragraph, flags=re.IGNORECASE):
                name = clean_michelin_full_list_name(strip_html(segment_html))
                if not name:
                    continue
                url = michelin_full_list_segment_url(segment_html, page_url=page_url) or page_url
                restaurants[(normalize_search_text(name), tier, url)] = MichelinRestaurant(
                    name=name,
                    url=url,
                    tier=tier,
                    award_year=award_year,
                    is_current=True,
                    region_key=region_key,
                )
    return sorted(
        restaurants.values(),
        key=lambda restaurant: (
            normalize_search_text(restaurant.name),
            restaurant.tier or "",
        ),
    )


def parse_michelin_detail_page_award_year(body: str, *, page_url: str) -> int | None:
    del page_url
    candidates: list[int] = []
    for pattern in (
        r'["\']dateAwarded["\']\s*[:=]\s*["\']?(20[0-9]{2})',
        r"\bdateAwarded\b[^0-9]{0,40}(20[0-9]{2})",
        r"\bMICHELIN Guide\b[^<\n\r]{0,120}\b(20[0-9]{2})\b",
        r"\b(20[0-9]{2})\b[^<\n\r]{0,120}\bMICHELIN Guide\b",
    ):
        candidates.extend(int(match) for match in re.findall(pattern, body, flags=re.IGNORECASE))
    if candidates:
        return max(candidates)
    return None


def michelin_full_list_sections(body: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    h2_pattern = re.compile(r"<h2\b[^>]*>.*?</h2>", flags=re.IGNORECASE | re.DOTALL)
    h2_matches = list(h2_pattern.finditer(body))
    for index, match in enumerate(h2_matches):
        heading = normalize_search_text(strip_html(match.group(0)))
        tier = michelin_full_list_heading_tier(heading)
        if tier is None:
            continue
        section_end = h2_matches[index + 1].start() if index + 1 < len(h2_matches) else len(body)
        sections.append((body[match.end() : section_end], tier))
    return sections


def michelin_full_list_heading_tier(normalized_heading: str) -> str | None:
    if "three michelin starred" in normalized_heading or "three michelin star" in normalized_heading:
        return "3 stars"
    if "two michelin star" in normalized_heading:
        return "2 stars"
    if "one michelin star" in normalized_heading:
        return "1 star"
    if "bib gourmand" in normalized_heading:
        return "Bib Gourmand"
    if "michelin green star" in normalized_heading or "green star" in normalized_heading:
        return "Green Star"
    if "selected restaurants" in normalized_heading:
        return "Selected"
    return None


def clean_michelin_full_list_name(value: str) -> str | None:
    value = re.sub(r"\((NEW|PROMOTED)\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n:")
    if not value:
        return None
    normalized = normalize_search_text(value)
    if normalized in {"taipei", "taichung", "kaohsiung", "tainan", "new taipei", "hsinchu"}:
        return None
    if normalized.startswith("michelin "):
        return None
    return value


def michelin_full_list_segment_url(segment_html: str, *, page_url: str) -> str | None:
    match = re.search(r'<a\b[^>]*href="(?P<href>[^"]+)"', segment_html, flags=re.IGNORECASE)
    if match is None:
        return None
    return canonical_michelin_restaurant_url(urljoin(page_url, html.unescape(match.group("href"))))


def canonical_michelin_restaurant_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "guide.michelin.com":
        return url
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) >= 4
        and parts[0] != "en"
        and looks_like_locale_path(parts[0], parts[1])
        and "/restaurant/" in parsed.path
    ):
        parts = ["en", *parts[2:]]
    elif len(parts) >= 4 and parts[0] == "en" and parts[1] in {"jp", "hk", "mo", "tw"}:
        parts = ["en", *parts[2:]]
    return parsed._replace(path="/" + "/".join(parts), query="", fragment="").geturl()


def canonical_tabelog_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("tabelog.com"):
        return url
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] == "en":
        parts = parts[1:]
    path = "/" + "/".join(parts) + ("/" if parts else "")
    return parsed._replace(path=path, query="", fragment="").geturl()


def looks_like_locale_path(country_part: str, language_part: str) -> bool:
    return bool(
        re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", country_part)
        and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", language_part)
    )


def michelin_next_page_url(body: str, *, page_url: str) -> str | None:
    matches = re.findall(r'href="(?P<href>[^"]*/restaurants/page/\d+)"', body)
    for href in matches:
        candidate = urljoin(page_url, html.unescape(href))
        if candidate != page_url:
            return candidate
    return None


def parse_wikipedia_michelin_starred_page(
    body: str,
    *,
    region_key: str,
    page_url: str,
) -> list[MichelinRestaurant]:
    table = wikipedia_table_with_caption(body, "Michelin-starred restaurants")
    if table is None:
        return []

    rows = wikipedia_table_rows(table)
    if not rows:
        return []
    headers = [normalize_wikipedia_cell_text(cell) for cell in rows[0]]
    year_columns = [
        (index, int(header))
        for index, header in enumerate(headers)
        if re.fullmatch(r"20[0-9]{2}", header)
    ]
    if not year_columns:
        return []

    restaurants: dict[tuple[str, str, int, str], MichelinRestaurant] = {}
    for row in rows[1:]:
        if not row:
            continue
        name = normalize_wikipedia_cell_text(row[0])
        if not name:
            continue
        url = wikipedia_cell_first_link(row[0], page_url=page_url) or page_url
        for column_index, year in year_columns:
            if column_index >= len(row):
                continue
            tier = wikipedia_michelin_star_tier(row[column_index])
            if tier is None:
                continue
            restaurants[(normalize_search_text(name), url, year, tier)] = MichelinRestaurant(
                name=name,
                url=url,
                tier=tier,
                award_year=year,
                is_current=False,
                region_key=region_key,
            )
    return sorted(
        restaurants.values(),
        key=lambda restaurant: (
            normalize_search_text(restaurant.name),
            restaurant.award_year or 0,
            restaurant.tier or "",
        ),
    )


def wikipedia_table_with_caption(body: str, caption: str) -> str | None:
    for match in re.finditer(r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>', body):
        table_start = match.start()
        table_end = find_matching_html_end(body, table_start, "table")
        if table_end is None:
            continue
        table = body[table_start:table_end]
        if caption.casefold() in strip_html(table).casefold():
            return table
    return None


def find_matching_html_end(body: str, start: int, tag_name: str) -> int | None:
    pattern = re.compile(rf"</?{tag_name}\b[^>]*>", flags=re.IGNORECASE)
    depth = 0
    for match in pattern.finditer(body, start):
        token = match.group(0)
        if token.startswith("</"):
            depth -= 1
            if depth == 0:
                return match.end()
        else:
            depth += 1
    return None


def wikipedia_table_rows(table_html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_match in re.finditer(r"<tr\b[^>]*>(?P<row>.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL):
        row_html = row_match.group("row")
        cells = [
            cell_match.group("cell")
            for cell_match in re.finditer(
                r"<t[dh]\b[^>]*>(?P<cell>.*?)</t[dh]>",
                row_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
        ]
        if cells:
            rows.append(cells)
    return rows


def normalize_wikipedia_cell_text(cell_html: str) -> str:
    text = strip_html(cell_html)
    text = re.sub(r"\[[0-9a-zA-Z]+\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def wikipedia_cell_first_link(cell_html: str, *, page_url: str) -> str | None:
    match = re.search(r'<a\b[^>]*href="(?P<href>[^"]+)"', cell_html, flags=re.IGNORECASE)
    if match is None:
        return None
    href = html.unescape(match.group("href"))
    if href.startswith("#"):
        return None
    if "redlink=1" in urlparse(href).query:
        return None
    return urljoin(page_url, href)


def wikipedia_michelin_star_tier(cell_html: str) -> str | None:
    normalized = normalize_search_text(cell_html)
    if "3 michelin stars" in normalized or "etoile michelin 3" in normalized:
        return "3 stars"
    if "2 michelin stars" in normalized or "etoile michelin 2" in normalized:
        return "2 stars"
    if "1 michelin star" in normalized or "etoile michelin 1" in normalized:
        return "1 star"
    return None


def signals_from_michelin_region(
    restaurants: list[MichelinRestaurant],
    *,
    context: MichelinMatchContext | str,
    fetched_at: datetime,
) -> list[TrustSignal]:
    match_context = (
        MichelinMatchContext(place_name=context)
        if isinstance(context, str)
        else context
    )
    signals: list[TrustSignal] = []
    for restaurant in restaurants:
        match = michelin_restaurant_match(restaurant, match_context)
        if match is None:
            continue
        confidence, match_reason = match
        signals.append(
            TrustSignal(
                source="michelin",
                label="MICHELIN Guide",
                tier=restaurant.tier,
                award_year=restaurant.award_year,
                is_current=restaurant.is_current,
                url=restaurant.url,
                title=restaurant.name,
                fetched_at=fetched_at.isoformat(),
                confidence=confidence,
                match_reason=match_reason,
            )
        )
    return sort_trust_signals(dedupe_trust_signals(signals))


def signals_from_tabelog_restaurants(
    restaurants: list[TabelogRestaurant],
    *,
    context: MichelinMatchContext | str,
    fetched_at: datetime,
) -> list[TrustSignal]:
    match_context = (
        MichelinMatchContext(place_name=context)
        if isinstance(context, str)
        else context
    )
    signals: list[TrustSignal] = []
    for restaurant in restaurants:
        match = tabelog_restaurant_match(restaurant, match_context)
        if match is None:
            continue
        confidence, match_reason = match
        signals.append(
            TrustSignal(
                source="tabelog",
                label=restaurant.label,
                tier=restaurant.tier,
                award_year=restaurant.award_year,
                is_current=restaurant.is_current,
                url=restaurant.url,
                title=restaurant.name,
                fetched_at=fetched_at.isoformat(),
                confidence=confidence,
                match_reason=match_reason,
            )
        )
    return sort_trust_signals(dedupe_trust_signals(signals))


def signals_from_tabelog_search_results(
    restaurants: list[TabelogRestaurant],
    results: list[SearchResult],
    *,
    place_name: str,
    fetched_at: datetime,
) -> list[TrustSignal]:
    restaurants_by_url: dict[str, list[TabelogRestaurant]] = {}
    for restaurant in restaurants:
        restaurants_by_url.setdefault(canonical_tabelog_url(restaurant.url), []).append(restaurant)

    signals: list[TrustSignal] = []
    matched_urls: set[str] = set()
    normalized_place_name = normalize_restaurant_name_for_match(place_name)
    for result in results:
        url = canonical_tabelog_url(result.url)
        matched_restaurants = restaurants_by_url.get(url)
        if not matched_restaurants or url in matched_urls:
            continue
        if not tabelog_search_result_matches_place(result, normalized_place_name):
            continue
        matched_urls.add(url)
        for restaurant in matched_restaurants:
            signals.append(
                TrustSignal(
                    source="tabelog",
                    label=restaurant.label,
                    tier=restaurant.tier,
                    award_year=restaurant.award_year,
                    is_current=restaurant.is_current,
                    url=restaurant.url,
                    title=restaurant.name,
                    fetched_at=fetched_at.isoformat(),
                    confidence="high",
                    match_reason="Tabelog direct search URL match",
                )
            )
    return sort_trust_signals(dedupe_trust_signals(signals))


def signals_from_tabelog_source_urls(
    restaurants: list[TabelogRestaurant],
    source_urls: list[PlaceSourceUrl],
    *,
    fetched_at: datetime,
) -> list[TrustSignal]:
    restaurants_by_url: dict[str, list[TabelogRestaurant]] = {}
    for restaurant in restaurants:
        restaurants_by_url.setdefault(canonical_tabelog_url(restaurant.url), []).append(restaurant)

    signals: list[TrustSignal] = []
    matched_urls: set[str] = set()
    for source_url in source_urls:
        if source_url.source != "tabelog":
            continue
        url = canonical_tabelog_url(source_url.url)
        matched_restaurants = restaurants_by_url.get(url)
        if not matched_restaurants or url in matched_urls:
            continue
        matched_urls.add(url)
        for restaurant in matched_restaurants:
            signals.append(
                TrustSignal(
                    source="tabelog",
                    label=restaurant.label,
                    tier=restaurant.tier,
                    award_year=restaurant.award_year,
                    is_current=restaurant.is_current,
                    url=restaurant.url,
                    title=restaurant.name,
                    fetched_at=fetched_at.isoformat(),
                    confidence=source_url.confidence,
                    match_reason="Tabelog saved source URL match",
                )
            )
    return sort_trust_signals(dedupe_trust_signals(signals))


def place_source_urls_from_tabelog_signals(
    signals: list[TrustSignal],
    *,
    fetched_at: datetime,
    refresh_after: datetime,
) -> list[PlaceSourceUrl]:
    source_urls: list[PlaceSourceUrl] = []
    for signal in signals:
        if signal.source != "tabelog" or not signal.url:
            continue
        source_urls.append(
            PlaceSourceUrl(
                source="tabelog",
                url=canonical_tabelog_url(signal.url),
                title=signal.title,
                fetched_at=fetched_at.isoformat(),
                refresh_after=refresh_after.isoformat(),
                confidence=signal.confidence,
                match_reason="Existing Tabelog direct signal",
            )
        )
    return dedupe_place_source_urls(source_urls)


def place_source_urls_from_tabelog_search_results(
    results: list[SearchResult],
    *,
    place_name: str,
    fetched_at: datetime,
    refresh_after: datetime,
) -> list[PlaceSourceUrl]:
    source_urls: list[PlaceSourceUrl] = []
    matched_urls: set[str] = set()
    normalized_place_name = normalize_restaurant_name_for_match(place_name)
    for result in results:
        url = canonical_tabelog_url(result.url)
        if url in matched_urls:
            continue
        if not tabelog_search_result_matches_place(result, normalized_place_name):
            continue
        matched_urls.add(url)
        source_urls.append(
            PlaceSourceUrl(
                source="tabelog",
                url=url,
                title=result.title,
                fetched_at=fetched_at.isoformat(),
                refresh_after=refresh_after.isoformat(),
                confidence="medium",
                match_reason="Tabelog search result name match",
            )
        )
    return dedupe_place_source_urls(source_urls)


def tabelog_search_result_matches_place(result: SearchResult, normalized_place_name: str) -> bool:
    result_name = normalize_restaurant_name_for_match(result.title)
    if not normalized_place_name or not result_name:
        return False
    if result_name == normalized_place_name:
        return True
    if min(len(normalized_place_name), len(result_name)) <= 4:
        return False
    return restaurant_name_similarity(normalized_place_name, result_name) >= 0.92


def michelin_restaurant_match(
    restaurant: MichelinRestaurant,
    context: MichelinMatchContext,
) -> tuple[Literal["high", "medium"], str] | None:
    place_name = normalize_restaurant_name_for_match(context.place_name)
    restaurant_name = normalize_restaurant_name_for_match(restaurant.name)
    if not place_name or not restaurant_name:
        return None
    if restaurant_name == place_name:
        return "high", "Michelin name exact match"

    ratio = restaurant_name_similarity(place_name, restaurant_name)
    containment = restaurant_name_contains_alias(place_name, restaurant_name)
    location_bonus = michelin_match_has_location_context(restaurant, context)
    if ratio >= 0.92:
        return "high", "Michelin name similarity match"
    if containment and location_bonus:
        return "medium", "Michelin name alias plus location match"
    if ratio >= 0.84 and location_bonus:
        return "medium", "Michelin name similarity plus location match"
    return None


def tabelog_restaurant_match(
    restaurant: TabelogRestaurant,
    context: MichelinMatchContext,
) -> tuple[Literal["high", "medium"], str] | None:
    place_name = normalize_restaurant_name_for_match(context.place_name)
    restaurant_name = normalize_restaurant_name_for_match(restaurant.name)
    if not place_name or not restaurant_name:
        return None
    if restaurant_name == place_name:
        return "high", "Tabelog name exact match"
    if min(len(place_name), len(restaurant_name)) <= 4:
        return None

    ratio = restaurant_name_similarity(place_name, restaurant_name)
    containment = restaurant_name_contains_alias(place_name, restaurant_name)
    location_bonus = tabelog_match_has_location_context(restaurant, context)
    if ratio >= 0.92:
        return "high", "Tabelog name similarity match"
    if containment and location_bonus:
        return "medium", "Tabelog name alias plus location match"
    if ratio >= 0.84 and location_bonus:
        return "medium", "Tabelog name similarity plus location match"
    return None


def normalize_restaurant_name_for_match(value: str) -> str:
    normalized = normalize_search_text(value)
    normalized = re.sub(r"\b(the|restaurant|ristorante|le|la|les|de|by)\b", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def restaurant_name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def restaurant_name_contains_alias(left: str, right: str) -> bool:
    left_tokens = [token for token in left.split() if token not in RESTAURANT_NAME_STOPWORDS]
    right_tokens = [token for token in right.split() if token not in RESTAURANT_NAME_STOPWORDS]
    if not left_tokens or not right_tokens:
        return False
    shorter, longer = (
        (left_tokens, right_tokens)
        if len(left_tokens) <= len(right_tokens)
        else (right_tokens, left_tokens)
    )
    if len(shorter) == 1 and len(longer) > 1:
        return False
    return all(token in longer for token in shorter)


def michelin_match_has_location_context(
    restaurant: MichelinRestaurant,
    context: MichelinMatchContext,
) -> bool:
    restaurant_location_text = normalize_search_text(
        " ".join(
            value
            for value in [
                restaurant.region_key,
                restaurant.url,
            ]
            if value
        )
    )
    normalized_city = normalize_search_text(context.city_name or "")
    return bool(normalized_city and normalized_city in restaurant_location_text)


def tabelog_match_has_location_context(
    restaurant: TabelogRestaurant,
    context: MichelinMatchContext,
) -> bool:
    restaurant_location_text = normalize_search_text(
        " ".join(
            value
            for value in [
                restaurant.region_key,
                restaurant.url,
            ]
            if value
        )
    )
    normalized_city = normalize_search_text(context.city_name or "")
    return bool(normalized_city and normalized_city in restaurant_location_text)


def brave_search(query: str, *, api_key: str) -> list[SearchResult]:
    params = urlencode({"q": query, "count": "10"})
    request = Request(
        f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
            "User-Agent": TRUST_SIGNAL_USER_AGENT,
        },
    )
    with urlopen(request, timeout=TRUST_SIGNAL_HTTP_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    web_payload = payload.get("web") if isinstance(payload, dict) else None
    raw_results = web_payload.get("results") if isinstance(web_payload, dict) else []
    results: list[SearchResult] = []
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict):
            continue
        title = as_nonempty_string(item.get("title"))
        url = as_nonempty_string(item.get("url"))
        if not title or not url:
            continue
        results.append(
            SearchResult(
                title=strip_html(title),
                url=url,
                snippet=strip_html(as_nonempty_string(item.get("description")) or ""),
                published_at=as_nonempty_string(item.get("age")),
            )
        )
    return results


def google_search_html(query: str) -> list[SearchResult]:
    params = urlencode({"q": query, "num": "10", "hl": "en"})
    request = Request(
        f"https://www.google.com/search?{params}",
        headers={"User-Agent": TRUST_SIGNAL_USER_AGENT},
    )
    with urlopen(request, timeout=TRUST_SIGNAL_HTTP_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8", errors="replace")
    return parse_google_search_results(body)


def tabelog_search(query: str) -> list[SearchResult]:
    params = urlencode({"sw": query})
    body = fetch_text(f"{TABELOG_SEARCH_BASE_URL}?{params}")
    return parse_tabelog_search_results(body, page_url=TABELOG_SEARCH_BASE_URL)


def parse_tabelog_search_results(body: str, *, page_url: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for block in html_blocks_by_class(body, "div", "list-rst"):
        url = first_attr_value(block, "data-detail-url")
        if url is None:
            url = first_href(block)
        if url is None:
            continue
        canonical_url = canonical_tabelog_url(urljoin(page_url, html.unescape(url)))
        if canonical_url in seen_urls:
            continue
        title = first_class_text(block, "list-rst__rst-name-target") or first_class_text(
            block,
            "cpy-rst-name",
        )
        if not title:
            title = urlparse(canonical_url).netloc
        seen_urls.add(canonical_url)
        results.append(SearchResult(title=title, url=canonical_url))
        if len(results) >= 10:
            break
    if results:
        return results

    item_list_match = re.search(
        r'<script\b[^>]*type="application/ld\+json"[^>]*>\s*(?P<payload>\{.*?"@type"\s*:\s*"ItemList".*?\})\s*</script>',
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if item_list_match is None:
        return []
    try:
        payload = json.loads(html.unescape(item_list_match.group("payload")))
    except json.JSONDecodeError:
        return []
    items = payload.get("itemListElement") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = as_nonempty_string(item.get("url"))
        if not url:
            continue
        canonical_url = canonical_tabelog_url(urljoin(page_url, url))
        if canonical_url in seen_urls:
            continue
        seen_urls.add(canonical_url)
        results.append(SearchResult(title=urlparse(canonical_url).netloc, url=canonical_url))
        if len(results) >= 10:
            break
    return results


def parse_google_search_results(body: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for match in re.finditer(r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<body>.*?)</a>', body, flags=re.DOTALL):
        href = html.unescape(match.group("href"))
        url = google_result_url(href)
        if url is None or url in seen_urls:
            continue
        seen_urls.add(url)
        title = strip_html(match.group("body"))
        if not title:
            title = urlparse(url).netloc
        results.append(SearchResult(title=title, url=url))
        if len(results) >= 10:
            break
    return results


def google_result_url(href: str) -> str | None:
    if href.startswith("/url?"):
        query = parse_qs(urlparse(href).query)
        candidate = query.get("q", [None])[0]
    else:
        candidate = href if href.startswith("http") else None
    if candidate is None:
        return None
    host = urlparse(candidate).netloc.lower()
    if not host or "google." in host:
        return None
    return candidate


def signals_from_search_results(
    results: list[SearchResult],
    *,
    place_name: str,
    city_name: str | None,
    country_name: str | None,
    fetched_at: datetime,
) -> list[TrustSignal]:
    signals: list[TrustSignal] = []
    for result in results:
        source, label, tier, award_year = classify_search_result(result)
        if source is None:
            continue
        if source == "tabelog" and normalize_search_text(country_name or "") != "japan":
            continue
        confidence, match_reason = trust_match_confidence(
            result,
            place_name=place_name,
            city_name=city_name,
            country_name=country_name,
        )
        signals.append(
            TrustSignal(
                source=source,
                label=label,
                tier=tier,
                award_year=award_year,
                is_current=None,
                url=result.url,
                title=result.title,
                published_at=result.published_at,
                fetched_at=fetched_at.isoformat(),
                confidence=confidence,
                match_reason=match_reason,
            )
        )
    return sort_trust_signals(dedupe_trust_signals(signals))


def search_signals_without_duplicate_award_urls(
    existing_signals: Iterable[TrustSignal],
    search_signals: Iterable[TrustSignal],
) -> list[TrustSignal]:
    michelin_urls = {
        canonical_michelin_restaurant_url(signal.url)
        for signal in existing_signals
        if signal.source == "michelin" and signal.url
    }
    tabelog_urls = {
        canonical_tabelog_url(signal.url)
        for signal in existing_signals
        if signal.source == "tabelog" and signal.url
    }
    filtered = [
        signal
        for signal in search_signals
        if not trust_signal_duplicates_existing_award_url(signal, michelin_urls=michelin_urls, tabelog_urls=tabelog_urls)
    ]
    return sort_trust_signals(dedupe_trust_signals(filtered))


def trust_signal_duplicates_existing_award_url(
    signal: TrustSignal,
    *,
    michelin_urls: set[str],
    tabelog_urls: set[str],
) -> bool:
    if not signal.url:
        return False
    if signal.source == "michelin":
        return canonical_michelin_restaurant_url(signal.url) in michelin_urls
    if signal.source == "tabelog":
        return canonical_tabelog_url(signal.url) in tabelog_urls
    return False


def classify_search_result(
    result: SearchResult,
) -> tuple[Literal["michelin", "tabelog", "timeout", "blog", "web"] | None, str, str | None, int | None]:
    host = urlparse(result.url).netloc.lower()
    title_text = f"{result.title} {result.snippet or ''}"
    award_year = infer_award_year(" ".join([result.title, result.snippet or "", result.url]))
    normalized = normalize_search_text(title_text)
    if "guide.michelin.com" in host:
        tier = michelin_tier(normalized)
        return "michelin", "MICHELIN Guide", tier, award_year
    if "tabelog.com" in host or "award.tabelog.com" in host:
        tier = tabelog_tier(normalized)
        return "tabelog", "Tabelog Award", tier, award_year
    if "timeout.com" in host:
        return "timeout", "Time Out", None, award_year
    if is_likely_blog_host(host):
        return "blog", "Blog mention", None, award_year
    return None, "", None, None


def michelin_tier(normalized_text: str) -> str | None:
    if "three stars" in normalized_text or "3 stars" in normalized_text or "3 star" in normalized_text:
        return "3 stars"
    if "two stars" in normalized_text or "2 stars" in normalized_text or "2 star" in normalized_text:
        return "2 stars"
    if "one star" in normalized_text or "1 star" in normalized_text:
        return "1 star"
    if "bib gourmand" in normalized_text:
        return "Bib Gourmand"
    if "green star" in normalized_text:
        return "Green Star"
    if "selected" in normalized_text:
        return "Selected"
    return None


def tabelog_tier(normalized_text: str) -> str | None:
    if "chef" in normalized_text and "gold" in normalized_text:
        return "Chefs' Gold"
    if "regional" in normalized_text:
        return "Best Regional Restaurants"
    if "new" in normalized_text:
        return "Best New Entry"
    for tier in ("gold", "silver", "bronze"):
        if tier in normalized_text:
            return tier.title()
    if "award" in normalized_text:
        return "Award"
    return None


def trust_match_confidence(
    result: SearchResult,
    *,
    place_name: str,
    city_name: str | None,
    country_name: str | None,
) -> tuple[Literal["high", "medium", "low"], str]:
    haystack = normalize_search_text(" ".join([result.title, result.snippet or "", result.url]))
    name = normalize_search_text(place_name)
    city = normalize_search_text(city_name or "")
    country = normalize_search_text(country_name or "")
    host = urlparse(result.url).netloc.lower()
    exact_name = bool(name and name in haystack)
    location_match = bool((city and city in haystack) or (country and country in haystack))
    authoritative = any(domain in host for domain in ("guide.michelin.com", "tabelog.com", "award.tabelog.com"))
    if exact_name and (location_match or authoritative):
        return "high", "name plus source/location match"
    if exact_name:
        return "medium", "name match"
    if token_overlap(name, haystack) >= 2 and location_match:
        return "medium", "partial name plus location match"
    return "low", "weak search-result match"


def trust_place_keys(
    place: RawPlace,
    *,
    place_id: str,
    enrichment_entry: EnrichmentCacheEntry | None,
    blocked_cid_alias_keys: set[str] | None = None,
) -> list[str]:
    keys: list[str] = []
    enrichment_place = enrichment_entry.place if enrichment_entry is not None else None
    if enrichment_place is not None:
        google_place_id = as_nonempty_string(enrichment_place.google_place_id)
        if google_place_id:
            keys.append(f"gpid:{google_place_id}")
    if place.cid:
        keys.append(f"cid:{place.cid}")
    for cid_alias in place.cid_aliases:
        cid_alias_key = f"cid:{cid_alias}" if cid_alias else None
        if cid_alias_key and (blocked_cid_alias_keys is None or cid_alias_key not in blocked_cid_alias_keys):
            keys.append(cid_alias_key)
    if place.google_id:
        keys.append(f"gid:{place.google_id.strip('/').replace('/', '-')}")
    if place.maps_place_token:
        keys.append(f"gms:{place.maps_place_token}")
    keys.append(place_id)
    return list(dict.fromkeys(keys))


def raw_saved_list_current_cid_keys(raw: RawSavedList) -> set[str]:
    return {f"cid:{place.cid}" for place in raw.places if place.cid}


def trust_search_query(place_name: str, *, city_name: str | None, country_name: str | None) -> str:
    terms = [
        f'"{place_name}"',
        city_name,
        country_name,
        "restaurant",
        "MICHELIN OR Tabelog OR Time Out OR blog",
    ]
    return " ".join(term for term in terms if term)


def tabelog_search_query(place_name: str) -> str:
    return place_name.strip()


def tabelog_search_place_is_eligible(
    place: RawPlace,
    *,
    enrichment_entry: EnrichmentCacheEntry | None,
    has_tabelog_source_match: bool = False,
) -> bool:
    if has_tabelog_source_match:
        return True
    category_terms = tabelog_category_terms(place, enrichment_entry=enrichment_entry)
    if not category_terms:
        return True
    normalized_terms = [normalize_search_text(term) for term in category_terms]
    if any(term_contains_any(normalized, TABELOG_ELIGIBLE_CATEGORY_TERMS) for normalized in normalized_terms):
        return True
    if any(term_contains_any(normalized, TABELOG_INELIGIBLE_CATEGORY_TERMS) for normalized in normalized_terms):
        return False
    return True


def tabelog_category_terms(
    place: RawPlace,
    *,
    enrichment_entry: EnrichmentCacheEntry | None,
) -> list[str]:
    terms: list[str] = []
    terms.extend(place.types)
    enrichment_place = enrichment_entry.place if enrichment_entry is not None else None
    if enrichment_place is not None:
        terms.extend(
            term
            for term in (
                enrichment_place.primary_type,
                enrichment_place.primary_type_display_name,
                enrichment_place.primary_type_display_name_localized,
                enrichment_place.category_display_en,
            )
            if term
        )
        terms.extend(enrichment_place.types)
        terms.extend(enrichment_place.semantic_types)
    return [term for term in terms if as_nonempty_string(term)]


def term_contains_any(value: str, needles: Iterable[str]) -> bool:
    tokens = value.split()
    token_set = set(tokens)
    for needle in needles:
        normalized_needle = normalize_search_text(needle)
        needle_tokens = normalized_needle.split()
        if not needle_tokens:
            continue
        if len(needle_tokens) == 1:
            if needle_tokens[0] in token_set:
                return True
            continue
        for index in range(0, len(tokens) - len(needle_tokens) + 1):
            if tokens[index:index + len(needle_tokens)] == needle_tokens:
                return True
    return False


def search_snapshot_refresh_after(provider: str, query: str, *, now: datetime) -> datetime:
    if provider != "tabelog_search":
        return now + TRUST_SIGNAL_REFRESH_TTL
    stagger_seconds = stable_stagger_seconds(
        f"{provider}:{query}",
        max_offset=TABELOG_SEARCH_REFRESH_STAGGER,
    )
    return next_tabelog_award_refresh_base(now) + timedelta(seconds=stagger_seconds)


def tabelog_source_refresh_after(source: TabelogSource, *, now: datetime) -> datetime:
    if source.source_type == "award":
        stagger_seconds = stable_stagger_seconds(
            tabelog_source_key(source),
            max_offset=TABELOG_AWARD_REFRESH_STAGGER,
        )
        return next_tabelog_award_refresh_base(now) + timedelta(seconds=stagger_seconds)
    stagger_seconds = stable_stagger_seconds(
        tabelog_source_key(source),
        max_offset=TABELOG_HYAKUMEITEN_REFRESH_STAGGER,
    )
    return now + TABELOG_HYAKUMEITEN_REFRESH_TTL + timedelta(seconds=stagger_seconds)


def next_tabelog_award_refresh_base(now: datetime) -> datetime:
    base = datetime(now.year, 2, 1, tzinfo=UTC)
    if now >= base:
        base = datetime(now.year + 1, 2, 1, tzinfo=UTC)
    return base


def stable_stagger_seconds(value: str, *, max_offset: timedelta) -> int:
    max_seconds = int(max_offset.total_seconds())
    if max_seconds <= 0:
        return 0
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (max_seconds + 1)


def trust_signal_row(
    place_key: str,
    signal: TrustSignal,
    *,
    match_signature: str,
    refresh_after: datetime,
) -> dict[str, Any]:
    signal_payload = signal.model_dump(mode="json")
    return {
        "place_key": place_key,
        "signal_key": trust_signal_key(signal),
        "source": signal.source,
        "label": signal.label,
        "tier": signal.tier,
        "award_year": signal.award_year,
        "is_current": sqlite_bool_or_none(signal.is_current),
        "url": signal.url,
        "title": signal.title,
        "published_at": signal.published_at,
        "fetched_at": signal.fetched_at,
        "refresh_after": refresh_after.isoformat(),
        "confidence": signal.confidence,
        "match_reason": signal.match_reason,
        "match_signature": match_signature,
        "signal_json": json.dumps(signal_payload, ensure_ascii=False, separators=(",", ":")),
    }


def trust_signal_key(signal: TrustSignal) -> str:
    payload = {
        "source": signal.source,
        "label": signal.label,
        "tier": signal.tier,
        "url": signal.url,
        "title": signal.title,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def infer_award_year(value: str) -> int | None:
    years = [int(match) for match in re.findall(r"\b(20[0-9]{2})\b", value)]
    return max(years) if years else None


def source_snapshot_key(provider: str, query: str) -> str:
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return f"search:{provider}:{digest}"


def michelin_source_key(source: MichelinRegionSource) -> str:
    digest = hashlib.sha256(source.url.encode("utf-8")).hexdigest()
    return f"michelin:v{MICHELIN_SOURCE_CACHE_VERSION}:{source.source_type}:{source.region_key}:{digest}"


def michelin_detail_source_key(restaurant_url: str) -> str:
    digest = hashlib.sha256(canonical_michelin_restaurant_url(restaurant_url).encode("utf-8")).hexdigest()
    return f"michelin-detail:v{MICHELIN_DETAIL_CACHE_VERSION}:{digest}"


def tabelog_source_key(source: TabelogSource) -> str:
    digest = hashlib.sha256(source.url.encode("utf-8")).hexdigest()
    return f"tabelog:v{TABELOG_SOURCE_CACHE_VERSION}:{source.source_type}:{source.region_key}:{digest}"


def trust_match_signature(place_name: str, city_name: str | None, country_name: str | None) -> str:
    payload = {
        "place_name": normalize_search_text(place_name),
        "city_name": normalize_search_text(city_name or ""),
        "country_name": normalize_search_text(country_name or ""),
        "version": 1,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def dedupe_trust_signals(signals: Iterable[TrustSignal]) -> list[TrustSignal]:
    by_key: dict[str, TrustSignal] = {}
    for signal in signals:
        key = trust_signal_key(signal)
        existing = by_key.get(key)
        if existing is None or trust_signal_sort_key(signal) < trust_signal_sort_key(existing):
            by_key[key] = signal
    return list(by_key.values())


def dedupe_place_source_urls(source_urls: Iterable[PlaceSourceUrl]) -> list[PlaceSourceUrl]:
    by_key: dict[tuple[str, str], PlaceSourceUrl] = {}
    for source_url in source_urls:
        key = (source_url.source, source_url.url)
        existing = by_key.get(key)
        if existing is None or TRUST_SIGNAL_CONFIDENCE_PRIORITY.get(
            source_url.confidence, 99
        ) < TRUST_SIGNAL_CONFIDENCE_PRIORITY.get(existing.confidence, 99):
            by_key[key] = source_url
    return sorted(by_key.values(), key=lambda value: (value.source, value.url))


def sort_trust_signals(signals: Iterable[TrustSignal]) -> list[TrustSignal]:
    return sorted(signals, key=trust_signal_sort_key)


def trust_signal_sort_key(signal: TrustSignal) -> tuple[int, int, int, int, int, str, str]:
    current_rank = 0 if signal.is_current is True else 1 if signal.is_current is None else 2
    year_rank = -(signal.award_year or 0)
    tier_rank = (
        MICHELIN_TIER_PRIORITY.get(signal.tier or "", 99)
        if signal.source == "michelin"
        else 0
    )
    return (
        TRUST_SIGNAL_SOURCE_PRIORITY.get(signal.source, 99),
        TRUST_SIGNAL_CONFIDENCE_PRIORITY.get(signal.confidence, 99),
        current_rank,
        tier_rank,
        year_rank,
        signal.label,
        signal.tier or "",
    )


def strip_html(value: str) -> str:
    stripped = re.sub(r"<[^>]+>", " ", value)
    stripped = html.unescape(stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def normalize_search_text(value: str) -> str:
    normalized = html.unescape(value).casefold()
    normalized = re.sub(r"https?://", " ", normalized)
    normalized = re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def token_overlap(left: str, right: str) -> int:
    left_tokens = {token for token in left.split() if len(token) > 2}
    right_tokens = {token for token in right.split() if len(token) > 2}
    return len(left_tokens & right_tokens)


def is_likely_blog_host(host: str) -> bool:
    if not host:
        return False
    excluded = (
        "google.",
        "instagram.com",
        "facebook.com",
        "youtube.com",
        "tiktok.com",
        "tripadvisor.",
        "yelp.",
        "opentable.",
    )
    if any(marker in host for marker in excluded):
        return False
    return any(marker in host for marker in ("blog", "substack", "medium.com", "wordpress", "note.com"))


def metadata_datetime_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def infer_city_name(title: str) -> str | None:
    parts = [part.strip() for part in re.split(r"[,|/]", title) if part.strip()]
    return parts[0] if parts else None


def infer_country_name(title: str) -> str | None:
    parts = [part.strip() for part in re.split(r"[,|/]", title) if part.strip()]
    return parts[-1] if len(parts) > 1 else None


def env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def as_nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None

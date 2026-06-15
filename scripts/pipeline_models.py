from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

type AddressParts = list[str | list[str]]


class PipelineModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PlacesSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    google_places_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_PLACES_API_KEY", "GOOGLE_MAPS_API_KEY"),
    )
    google_places_enrichment_strategy: Literal["scrape", "api", "scrape_then_api"] = Field(
        default="scrape_then_api",
        validation_alias=AliasChoices("GOOGLE_PLACES_ENRICHMENT_STRATEGY"),
    )
    google_maps_place_llm_repair: Literal["off", "dom", "dom_then_translation"] | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_MAPS_PLACES_LLM_REPAIR", "GMAPS_PLACES_LLM_REPAIR"),
    )
    google_maps_place_llm_cache_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_MAPS_PLACES_LLM_CACHE_DIR", "GMAPS_PLACES_LLM_CACHE_DIR"),
    )
    google_maps_place_collect_reviews: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_MAPS_PLACES_COLLECT_REVIEWS", "GMAPS_PLACES_COLLECT_REVIEWS"),
    )
    google_maps_place_collect_about: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_MAPS_PLACES_COLLECT_ABOUT", "GMAPS_PLACES_COLLECT_ABOUT"),
    )
    google_maps_place_semantic_llm: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_MAPS_PLACES_SEMANTIC_LLM", "GMAPS_PLACES_SEMANTIC_LLM"),
    )
    google_maps_place_semantic_descriptions: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_MAPS_PLACES_SEMANTIC_DESCRIPTIONS",
            "GMAPS_PLACES_SEMANTIC_DESCRIPTIONS",
        ),
    )
    google_maps_place_semantic_description_force_refresh: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GOOGLE_MAPS_PLACES_SEMANTIC_DESCRIPTION_FORCE_REFRESH",
            "GMAPS_PLACES_SEMANTIC_DESCRIPTION_FORCE_REFRESH",
        ),
    )


class SourceConfig(PipelineModel):
    slug: str
    type: Literal["google_list_url", "wanderlog_view_url", "google_export_csv"] | None = None
    url: str | None = None
    path: str | None = None
    title: str | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_source_type(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        inferred = dict(data)
        explicit_type = as_nonempty_string(inferred.get("type"))
        url = as_nonempty_string(inferred.get("url"))
        path = as_nonempty_string(inferred.get("path"))
        field_type = infer_source_type_from_fields(url=url, path=path)

        if explicit_type and field_type and explicit_type != field_type:
            raise ValueError(f"Source type `{explicit_type}` does not match configured source fields.")
        if explicit_type:
            return inferred
        if field_type:
            inferred["type"] = field_type
        else:
            raise ValueError("Could not infer source type. Provide `type`, `url`, or `path`.")

        return inferred

    @model_validator(mode="after")
    def validate_source_location(self) -> "SourceConfig":
        if not self.type:
            raise ValueError("Source type is required.")
        if self.url and is_unsupported_google_mymaps_url(self.url):
            raise ValueError("Google My Maps URLs are not supported as list sources.")
        if self.type == "google_list_url" and self.url and not is_supported_google_maps_source_url(self.url):
            raise ValueError("google_list_url sources require a supported Google Maps URL.")
        if self.type == "google_list_url" and not self.url:
            raise ValueError("google_list_url sources require `url`")
        if self.type == "wanderlog_view_url" and self.url and not is_supported_wanderlog_source_url(self.url):
            raise ValueError("wanderlog_view_url sources require a supported Wanderlog view URL.")
        if self.type == "wanderlog_view_url" and not self.url:
            raise ValueError("wanderlog_view_url sources require `url`")
        if self.type == "google_export_csv" and not self.path:
            raise ValueError("google_export_csv sources require `path`")
        if self.type == "google_export_csv" and not self.title:
            raise ValueError("google_export_csv sources require `title`")
        return self


def as_nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def infer_source_type_from_fields(*, url: str | None, path: str | None) -> str | None:
    if url and is_unsupported_google_mymaps_url(url):
        raise ValueError("Google My Maps URLs are not supported as list sources.")
    if url and is_supported_google_maps_source_url(url):
        return "google_list_url"
    if url and is_supported_wanderlog_source_url(url):
        return "wanderlog_view_url"
    if path:
        return "google_export_csv"
    return None


def is_supported_google_maps_source_url(url: str) -> bool:
    normalized = url.strip().lower()
    return normalized.startswith("https://maps.app.goo.gl/") or (
        normalized.startswith("https://www.google.com/maps/")
        and not is_unsupported_google_mymaps_url(normalized)
    )


def is_unsupported_google_mymaps_url(url: str) -> bool:
    normalized = url.strip().lower()
    return normalized.startswith("https://www.google.com/maps/d/")


def is_supported_wanderlog_source_url(url: str) -> bool:
    normalized = url.strip().lower()
    return normalized.startswith("https://wanderlog.com/view/") or normalized.startswith(
        "https://www.wanderlog.com/view/"
    )


class RawPlace(PipelineModel):
    name: str
    address: str | None = None
    note: str | None = None
    is_favorite: bool = False
    added_by: ListAuthor | None = None
    lat: float | None = None
    lng: float | None = None
    maps_url: str
    cid: str | None = None
    google_id: str | None = None
    maps_place_token: str | None = None
    rating: float | None = None
    user_rating_count: int | None = None
    price_level: int | None = None
    types: list[str] = Field(default_factory=list)
    business_status: str | None = None
    photo_url: str | None = None


class ListAuthor(PipelineModel):
    name: str
    photo_url: str | None = None
    photo_path: str | None = None
    avatar_mode: Literal["photo", "initials", "icon"] | None = None
    profile_id: str | None = None


class RawSavedList(PipelineModel):
    fetched_at: str | None = None
    refresh_after: str | None = None
    source_signature: str | None = None
    configured_source_type: str | None = None
    configured_source_url: str | None = None
    configured_source_path: str | None = None
    source_url: str | None = None
    list_id: str | None = None
    title: str | None = None
    description: str | None = None
    owner: ListAuthor | None = None
    collaborators: list[ListAuthor] = Field(default_factory=list)
    places: list[RawPlace] = Field(default_factory=list)


class PlaceReservationLink(PipelineModel):
    label: str
    url: str


class EnrichmentPlace(PipelineModel):
    google_place_id: str | None = None
    google_place_resource_name: str | None = None
    display_name: str | None = None
    formatted_address: str | None = None
    address_display_en: str | None = None
    address_display_en_source: str | None = None
    address_display_en_confidence: str | None = None
    google_maps_uri: str | None = None
    rating: float | None = None
    user_rating_count: int | None = None
    price_range: str | None = None
    admission_price: str | None = None
    room_price: str | None = None
    primary_type: str | None = None
    primary_type_display_name: str | None = None
    primary_type_display_name_localized: str | None = None
    category_display_en: str | None = None
    category_display_en_source: str | None = None
    category_display_en_confidence: str | None = None
    types: list[str] = Field(default_factory=list)
    business_status: str | None = None
    website: str | None = None
    reservation_links: list[PlaceReservationLink] = Field(default_factory=list)
    phone: str | None = None
    plus_code: str | None = None
    address_parts: AddressParts | None = None
    description: str | None = None
    search_result_description: str | None = None
    search_result_url: str | None = None
    main_photo_url: str | None = None
    photo_url: str | None = None
    review_topics: list[dict[str, Any]] = Field(default_factory=list)
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    about_sections: list[dict[str, Any]] = Field(default_factory=list)
    semantic_neighborhood: str | None = None
    semantic_tags: list[str] = Field(default_factory=list)
    semantic_vibe_tags: list[str] = Field(default_factory=list)
    semantic_types: list[str] = Field(default_factory=list)
    semantic_description: str | None = None
    semantic_description_signature: str | None = None
    semantic_source: str | None = None
    limited_view: bool = False


class EnrichmentCacheEntry(PipelineModel):
    fetched_at: str
    last_verified_at: str | None = None
    refresh_after: str | None = None
    source: Literal["google_maps_page", "google_places_api"] | None = None
    merged_sources: list[Literal["google_maps_page", "google_places_api"]] = Field(default_factory=list)
    query: str
    input_signature: str | None = None
    matched: bool | None = None
    score: int | None = None
    error: str | None = None
    error_body: str | None = None
    place: EnrichmentPlace | None = None


TrustSignalSource = Literal[
    "michelin",
    "tabelog",
    "timeout",
    "blog",
    "web",
    "brave_search",
    "google_search",
]

TrustSignalConfidence = Literal["high", "medium", "low"]


class TrustSignal(PipelineModel):
    source: TrustSignalSource
    label: str
    tier: str | None = None
    display_label: str | None = None
    display_tier: str | None = None
    award_year: int | None = None
    is_current: bool | None = None
    url: str | None = None
    title: str | None = None
    published_at: str | None = None
    fetched_at: str
    confidence: TrustSignalConfidence
    match_reason: str


FieldSource = Literal[
    "manual",
    "google_list",
    "google_places",
    "google_maps_page",
    "osm",
    "wikidata",
    "website",
    "trust_signal",
]

MarkerIcon = Literal[
    "default",
    "cafe",
    "restaurant",
    "bar",
    "bakery",
    "museum",
    "attraction",
    "park",
    "beach",
    "shopping",
    "hotel",
    "spa",
]


class PlaceField(PipelineModel):
    value: Any
    source: FieldSource
    fetched_at: str | None = None
    expires_at: str | None = None


class PlaceProvenance(PipelineModel):
    name: PlaceField | None = None
    address: PlaceField | None = None
    lat: PlaceField | None = None
    lng: PlaceField | None = None
    maps_url: PlaceField | None = None
    cid: PlaceField | None = None
    google_id: PlaceField | None = None
    google_place_id: PlaceField | None = None
    google_place_resource_name: PlaceField | None = None
    rating: PlaceField | None = None
    user_rating_count: PlaceField | None = None
    primary_category: PlaceField | None = None
    primary_category_localized: PlaceField | None = None
    website: PlaceField | None = None
    reservation_links: PlaceField | None = None
    tags: list[PlaceField] = Field(default_factory=list)
    neighborhood: PlaceField | None = None
    note: PlaceField | None = None
    why_recommended: PlaceField | None = None
    added_by: PlaceField | None = None
    top_pick: PlaceField | None = None
    hidden: PlaceField | None = None
    manual_rank: PlaceField | None = None
    status: PlaceField | None = None
    trust_signals: PlaceField | None = None


class NormalizedPlace(PipelineModel):
    id: str
    name: str
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    maps_url: str
    cid: str | None = None
    google_id: str | None = None
    google_place_id: str | None = None
    google_place_resource_name: str | None = None
    rating: float | None = None
    user_rating_count: int | None = None
    primary_category: str | None = None
    primary_category_localized: str | None = None
    marker_icon: MarkerIcon = "default"
    tags: list[str] = Field(default_factory=list)
    visible_tags: list[str] = Field(default_factory=list)
    vibe_tags: list[str] = Field(default_factory=list)
    price_range: str | None = None
    budget_kind: Literal[
        "restaurant_per_person",
        "hotel_per_night",
        "admission_per_person",
    ] | None = None
    budget_tier: int | None = None
    budget_label: str | None = None
    website: str | None = None
    reservation_links: list[PlaceReservationLink] = Field(default_factory=list)
    neighborhood: str | None = None
    note: str | None = None
    why_recommended: str | None = None
    added_by: ListAuthor | None = None
    main_photo_path: str | None = None
    photo_url: str | None = None
    top_pick: bool = False
    hidden: bool = False
    manual_rank: int = 0
    status: str
    trust_signals: list[TrustSignal] = Field(default_factory=list)
    provenance: PlaceProvenance = Field(default_factory=PlaceProvenance)


class Guide(PipelineModel):
    slug: str
    title: str
    description: str | None = None
    author: ListAuthor | None = None
    place_photo_mode: Literal["local_cache", "remote_url"] = "local_cache"
    source_url: str | None = None
    list_id: str | None = None
    country_name: str
    country_code: str | None = None
    city_name: str
    list_tags: list[str] = Field(default_factory=list)
    featured_place_ids: list[str] = Field(default_factory=list)
    best_hit_place_ids: list[str] = Field(default_factory=list)
    best_hit_min_rating: float | None = None
    best_hit_min_reviews: int | None = None
    top_categories: list[str] = Field(default_factory=list)
    generated_at: str
    place_count: int
    center_lat: float | None = None
    center_lng: float | None = None
    places: list[NormalizedPlace] = Field(default_factory=list)


class GuideManifest(PipelineModel):
    slug: str
    title: str
    description: str | None = None
    author: ListAuthor | None = None
    country_name: str
    country_code: str | None = None
    center_lat: float | None = None
    center_lng: float | None = None
    city_name: str
    list_tags: list[str] = Field(default_factory=list)
    place_count: int
    featured_names: list[str] = Field(default_factory=list)
    top_categories: list[str] = Field(default_factory=list)

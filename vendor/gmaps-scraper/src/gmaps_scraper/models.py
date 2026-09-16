"""Data models for parsed Google Maps results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

type AddressParts = list[str | list[str]]

PLACE_LLM_REPAIR_FIELDS: tuple[str, ...] = (
    "name",
    "secondary_name",
    "category",
    "category_display_en",
    "category_display_en_source",
    "category_display_en_confidence",
    "rating",
    "review_count",
    "price_range",
    "address",
    "address_display_en",
    "address_display_en_source",
    "address_display_en_confidence",
    "located_in",
    "status",
    "website",
    "phone",
    "plus_code",
    "address_parts",
    "description",
    "main_photo_url",
    "photo_url",
    "lat",
    "lng",
    "limited_view",
    "google_place_id",
    "review_topics",
    "about_sections",
)

PLACE_LLM_DISPLAY_TRANSLATION_FIELDS: tuple[str, ...] = (
    "category_display_en",
    "category_display_en_source",
    "category_display_en_confidence",
    "address_display_en",
    "address_display_en_source",
    "address_display_en_confidence",
)

PLACE_LLM_DOM_REPAIR_FIELDS: tuple[str, ...] = tuple(
    field for field in PLACE_LLM_REPAIR_FIELDS if field not in PLACE_LLM_DISPLAY_TRANSLATION_FIELDS
)


@dataclass(slots=True)
class ListOwner:
    """Owner or collaborator metadata attached to a saved list."""

    name: str
    photo_url: str | None = None
    profile_id: str | None = None

    def to_dict(self, *, include_photo_url: bool = True) -> dict[str, object]:
        """Convert owner metadata into a JSON-serializable dictionary."""
        result: dict[str, object] = {"name": self.name}
        if include_photo_url and self.photo_url is not None:
            result["photo_url"] = self.photo_url
        if self.profile_id is not None:
            result["profile_id"] = self.profile_id
        return result


@dataclass(slots=True)
class Place:
    """A single saved place extracted from a Google Maps list."""

    name: str
    address: str | None
    note: str | None
    lat: float
    lng: float
    maps_url: str
    cid: str | None = None
    google_id: str | None = None
    is_favorite: bool = False
    added_by: ListOwner | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert a place into a JSON-serializable dictionary."""
        result: dict[str, object] = {
            "name": self.name,
            "address": self.address,
            "note": self.note,
            "is_favorite": self.is_favorite,
            "lat": self.lat,
            "lng": self.lng,
            "maps_url": self.maps_url,
        }
        if self.note is None:
            del result["note"]
        if self.cid is not None:
            result["cid"] = self.cid
        if self.google_id is not None:
            result["google_id"] = self.google_id
        if self.added_by is not None:
            result["added_by"] = self.added_by.to_dict(include_photo_url=False)
        return result


@dataclass(slots=True)
class SavedList:
    """A parsed Google Maps saved list."""

    source_url: str
    resolved_url: str | None
    list_id: str | None
    title: str | None
    description: str | None
    places: list[Place]
    owner: ListOwner | None = None
    collaborators: list[ListOwner] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Convert a saved list into a JSON-serializable dictionary."""
        return {
            "source_url": self.source_url,
            "resolved_url": self.resolved_url,
            "list_id": self.list_id,
            "title": self.title,
            "description": self.description,
            "owner": self.owner.to_dict() if self.owner is not None else None,
            "collaborators": [
                collaborator.to_dict() for collaborator in self.collaborators
            ],
            "places": [place.to_dict() for place in self.places],
        }


@dataclass(slots=True)
class ReviewTopic:
    """A Google Maps review topic/filter chip."""

    label: str
    # Number of reviews Google says mention this topic.
    count: int | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert a review topic into a JSON-serializable dictionary."""
        result: dict[str, object] = {"label": self.label}
        if self.count is not None:
            result["count"] = self.count
        return result


@dataclass(slots=True)
class PlaceReview:
    """A visible Google Maps review snippet."""

    author: str | None = None
    rating: float | None = None
    relative_time: str | None = None
    text: str | None = None
    like_count: int | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert a review snippet into a JSON-serializable dictionary."""
        result: dict[str, object] = {
            "author": self.author,
            "rating": self.rating,
            "relative_time": self.relative_time,
            "text": self.text,
            "like_count": self.like_count,
        }
        return {
            key: value
            for key, value in result.items()
            if value is not None and value != ""
        }


@dataclass(slots=True)
class PlaceAboutItem:
    """A visible Google Maps About-panel attribute."""

    label: str
    aria_label: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert an About-panel attribute into a JSON-serializable dictionary."""
        result: dict[str, object] = {"label": self.label}
        if self.aria_label is not None:
            result["aria_label"] = self.aria_label
        return result


@dataclass(slots=True)
class PlaceAboutSection:
    """A Google Maps About-panel attribute section."""

    title: str
    items: list[PlaceAboutItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Convert an About-panel section into a JSON-serializable dictionary."""
        return {
            "title": self.title,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(slots=True)
class PlaceExtractionDiagnostics:
    """Diagnostics for deterministic and optional repaired place extraction."""

    field_sources: dict[str, str] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    confidence: float | None = None
    llm_used: bool = False
    repair_source: str | None = None
    llm_error: str | None = None
    evidence_hash: str | None = None
    prompt_version: str | None = None

    def to_dict(self, *, include_debug: bool = False) -> dict[str, object]:
        """Convert diagnostics into a JSON-serializable dictionary."""
        result: dict[str, object] = {
            "quality_flags": self.quality_flags,
            "llm_used": self.llm_used,
        }
        if include_debug:
            result["field_sources"] = self.field_sources
            result["missing_fields"] = self.missing_fields
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.repair_source is not None:
            result["repair_source"] = self.repair_source
        if self.evidence_hash is not None:
            result["evidence_hash"] = self.evidence_hash
        if self.llm_error is not None:
            result["llm_error"] = self.llm_error
        if self.prompt_version is not None:
            result["prompt_version"] = self.prompt_version
        return result


@dataclass(slots=True)
class PlaceLLMRepairRequest:
    """Provider-neutral request passed to an optional place repair callback."""

    source_url: str
    resolved_url: str | None
    current_fields: dict[str, Any]
    diagnostics: PlaceExtractionDiagnostics
    evidence: dict[str, Any]
    tasks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Convert the repair request into a JSON-serializable dictionary."""
        return {
            "source_url": self.source_url,
            "resolved_url": self.resolved_url,
            "current_fields": self.current_fields,
            "diagnostics": self.diagnostics.to_dict(),
            "evidence": self.evidence,
            "tasks": self.tasks,
        }


@dataclass(slots=True)
class PlaceReservationLink:
    """A visible booking or reservation provider link from a place page."""

    label: str
    url: str

    def to_dict(self) -> dict[str, object]:
        """Convert a reservation link into a JSON-serializable dictionary."""
        return {
            "label": self.label,
            "url": self.url,
        }


@dataclass(slots=True)
class PlaceDetails:
    """A parsed Google Maps place page."""

    source_url: str
    resolved_url: str | None
    name: str | None
    category: str | None
    rating: float | None
    review_count: int | None
    address: str | None
    price_range: str | None = None
    admission_price: str | None = None
    room_price: str | None = None
    category_display_en: str | None = None
    category_display_en_source: str | None = None
    category_display_en_confidence: str | None = None
    address_display_en: str | None = None
    address_display_en_source: str | None = None
    address_display_en_confidence: str | None = None
    located_in: str | None = None
    status: str | None = None
    website: str | None = None
    reservation_links: list[PlaceReservationLink] = field(default_factory=list)
    phone: str | None = None
    plus_code: str | None = None
    address_parts: AddressParts | None = None
    description: str | None = None
    secondary_name: str | None = None
    lat: float | None = None
    lng: float | None = None
    limited_view: bool = False
    main_photo_url: str | None = None
    photo_url: str | None = None
    google_place_id: str | None = None
    review_topics: list[ReviewTopic] = field(default_factory=list)
    reviews: list[PlaceReview] = field(default_factory=list)
    about_sections: list[PlaceAboutSection] = field(default_factory=list)
    diagnostics: PlaceExtractionDiagnostics | None = None
    search_result_description: str | None = None
    search_result_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert place details into a JSON-serializable dictionary."""
        result: dict[str, object] = {
            "source_url": self.source_url,
            "resolved_url": self.resolved_url,
            "google_place_id": self.google_place_id,
            "name": self.name,
            "category": self.category,
            "category_display_en": self.category_display_en,
            "category_display_en_source": self.category_display_en_source,
            "category_display_en_confidence": self.category_display_en_confidence,
            "rating": self.rating,
            "review_count": self.review_count,
            "price_range": self.price_range,
            "admission_price": self.admission_price,
            "room_price": self.room_price,
            "address": self.address,
            "address_display_en": self.address_display_en,
            "address_display_en_source": self.address_display_en_source,
            "address_display_en_confidence": self.address_display_en_confidence,
            "located_in": self.located_in,
            "status": self.status,
            "website": self.website,
            "reservation_links": [link.to_dict() for link in self.reservation_links],
            "phone": self.phone,
            "plus_code": self.plus_code,
            "address_parts": self.address_parts,
            "description": self.description,
            "search_result_description": self.search_result_description,
            "search_result_url": self.search_result_url,
            "main_photo_url": self.main_photo_url,
            "photo_url": self.photo_url,
            "secondary_name": self.secondary_name,
            "lat": self.lat,
            "lng": self.lng,
            "limited_view": self.limited_view,
            "review_topics": [topic.to_dict() for topic in self.review_topics],
            "reviews": [review.to_dict() for review in self.reviews],
            "about_sections": [section.to_dict() for section in self.about_sections],
            "diagnostics": (
                self.diagnostics.to_dict() if self.diagnostics is not None else None
            ),
        }
        return {
            key: value
            for key, value in result.items()
            if value is not None and value != "" and value != []
        }


@dataclass(slots=True)
class PlaceScrapeResult:
    """Per-URL result for a batch place scrape."""

    source_url: str
    place: PlaceDetails | None = None
    error: str | None = None
    attempts: int = 1

    def to_dict(self) -> dict[str, object]:
        """Convert a batch result into a JSON-serializable dictionary."""
        result: dict[str, object] = {
            "source_url": self.source_url,
            "attempts": self.attempts,
        }
        if self.place is not None:
            result["place"] = self.place.to_dict()
        if self.error is not None:
            result["error"] = self.error
        return result

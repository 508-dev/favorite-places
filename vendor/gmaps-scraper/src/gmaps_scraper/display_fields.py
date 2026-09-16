"""Helpers for reusing English-readable display fields across refreshes."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from hashlib import sha256
from typing import cast

from gmaps_scraper.models import (
    PlaceDetails,
    PlaceExtractionDiagnostics,
    PlaceLLMRepairRequest,
)
from gmaps_scraper.translation_memory import needs_display_en

type PlaceDisplayFieldRepairer = Callable[
    [PlaceLLMRepairRequest],
    Mapping[str, object] | None,
]

_DISPLAY_REPAIR_PROMPT_VERSION = "gmaps-display-translation-v1"
_DISPLAY_FIELD_GROUPS = (
    (
        "category",
        "category_display_en",
        "category_display_en_source",
        "category_display_en_confidence",
    ),
    (
        "address",
        "address_display_en",
        "address_display_en_source",
        "address_display_en_confidence",
    ),
)
_DISPLAY_REPAIR_FIELDS = {
    "category_display_en",
    "category_display_en_source",
    "category_display_en_confidence",
    "address_display_en",
    "address_display_en_source",
    "address_display_en_confidence",
}


def reusable_place_display_fields(
    current_fields: Mapping[str, object],
    previous_fields: Mapping[str, object],
    *,
    accepted_sources: Collection[str] | None = None,
    accepted_confidences: Collection[str] | None = None,
) -> dict[str, object]:
    """Return prior display fields that are safe to reuse for refreshed place data."""
    reusable: dict[str, object] = {}
    for raw_key, display_key, source_key, confidence_key in _DISPLAY_FIELD_GROUPS:
        current_raw = _clean_display_value(current_fields.get(raw_key))
        previous_raw = _clean_display_value(previous_fields.get(raw_key))
        if current_raw is None or current_raw != previous_raw:
            continue

        previous_display = _clean_display_value(previous_fields.get(display_key))
        if previous_display is None or needs_display_en(previous_display):
            continue

        current_display = _clean_display_value(current_fields.get(display_key))
        if current_display is not None and not needs_display_en(current_display):
            continue

        previous_source = _clean_display_value(previous_fields.get(source_key))
        if accepted_sources is not None and previous_source not in accepted_sources:
            continue

        previous_confidence = _clean_display_value(previous_fields.get(confidence_key))
        if (
            accepted_confidences is not None
            and previous_confidence not in accepted_confidences
        ):
            continue

        reusable[display_key] = previous_display
        if previous_source is not None:
            reusable[source_key] = previous_source
        if previous_confidence is not None:
            reusable[confidence_key] = previous_confidence
    return reusable


def reuse_place_display_fields(
    current: PlaceDetails,
    previous: PlaceDetails,
    *,
    accepted_sources: Collection[str] | None = None,
    accepted_confidences: Collection[str] | None = None,
) -> PlaceDetails:
    """Return a copy of ``current`` with reusable prior display fields applied."""
    reusable = reusable_place_display_fields(
        _place_display_values(current),
        _place_display_values(previous),
        accepted_sources=accepted_sources,
        accepted_confidences=accepted_confidences,
    )
    if not reusable:
        return current
    return replace(
        current,
        category_display_en=cast(
            str | None,
            reusable.get("category_display_en", current.category_display_en),
        ),
        category_display_en_source=cast(
            str | None,
            reusable.get(
                "category_display_en_source",
                current.category_display_en_source,
            ),
        ),
        category_display_en_confidence=cast(
            str | None,
            reusable.get(
                "category_display_en_confidence",
                current.category_display_en_confidence,
            ),
        ),
        address_display_en=cast(
            str | None,
            reusable.get("address_display_en", current.address_display_en),
        ),
        address_display_en_source=cast(
            str | None,
            reusable.get("address_display_en_source", current.address_display_en_source),
        ),
        address_display_en_confidence=cast(
            str | None,
            reusable.get(
                "address_display_en_confidence",
                current.address_display_en_confidence,
            ),
        ),
    )


def repair_place_display_fields(
    place: PlaceDetails,
    *,
    repairer: PlaceDisplayFieldRepairer,
    evidence: Mapping[str, object] | None = None,
) -> PlaceDetails:
    """Return a copy of ``place`` with repaired English-readable display fields.

    This helper does not scrape Google Maps. It builds a display-translation-only
    repair request from the already-known raw place fields.
    """
    current_fields = _place_display_values(place)
    quality_flags = _display_translation_quality_flags(current_fields)
    if not quality_flags:
        return place

    request_evidence: dict[str, object] = {
        "prompt_version": _DISPLAY_REPAIR_PROMPT_VERSION,
        "current_fields": current_fields,
    }
    if evidence is not None:
        request_evidence["caller_evidence"] = dict(evidence)
    diagnostics = PlaceExtractionDiagnostics(
        quality_flags=quality_flags,
        evidence_hash=_hash_display_repair_evidence(request_evidence),
        prompt_version=_DISPLAY_REPAIR_PROMPT_VERSION,
    )
    repair = repairer(
        PlaceLLMRepairRequest(
            source_url=place.source_url,
            resolved_url=place.resolved_url,
            current_fields=current_fields,
            diagnostics=diagnostics,
            evidence=request_evidence,
            tasks=["display_translation"],
        )
    )
    if repair is None:
        return place
    fields = repair.get("fields") if isinstance(repair.get("fields"), Mapping) else repair
    if not isinstance(fields, Mapping):
        return place
    repaired = _clean_repaired_display_fields(fields)
    if not repaired:
        return place
    repair_source = _repair_source(repair)
    return replace(
        place,
        category_display_en=cast(
            str | None,
            repaired.get("category_display_en", place.category_display_en),
        ),
        category_display_en_source=cast(
            str | None,
            repaired.get(
                "category_display_en_source",
                place.category_display_en_source,
            ),
        ),
        category_display_en_confidence=cast(
            str | None,
            repaired.get(
                "category_display_en_confidence",
                place.category_display_en_confidence,
            ),
        ),
        address_display_en=cast(
            str | None,
            repaired.get("address_display_en", place.address_display_en),
        ),
        address_display_en_source=cast(
            str | None,
            repaired.get("address_display_en_source", place.address_display_en_source),
        ),
        address_display_en_confidence=cast(
            str | None,
            repaired.get(
                "address_display_en_confidence",
                place.address_display_en_confidence,
            ),
        ),
        diagnostics=_repaired_display_diagnostics(
            place,
            repaired_fields=repaired,
            request_diagnostics=diagnostics,
            repair_source=repair_source,
        ),
    )


def _place_display_values(place: PlaceDetails) -> dict[str, object]:
    return {
        "name": place.name,
        "secondary_name": place.secondary_name,
        "category": place.category,
        "category_display_en": place.category_display_en,
        "category_display_en_source": place.category_display_en_source,
        "category_display_en_confidence": place.category_display_en_confidence,
        "address": place.address,
        "address_display_en": place.address_display_en,
        "address_display_en_source": place.address_display_en_source,
        "address_display_en_confidence": place.address_display_en_confidence,
        "located_in": place.located_in,
        "address_parts": place.address_parts,
        "google_place_id": place.google_place_id,
    }


def _display_translation_quality_flags(fields: Mapping[str, object]) -> list[str]:
    quality_flags: list[str] = []
    category = _clean_display_value(fields.get("category"))
    category_display = _clean_display_value(fields.get("category_display_en"))
    if (
        category is not None
        and needs_display_en(category)
        and (category_display is None or needs_display_en(category_display))
    ):
        quality_flags.append("needs_category_display_en")
    address = _clean_display_value(fields.get("address"))
    address_display = _clean_display_value(fields.get("address_display_en"))
    if (
        address is not None
        and needs_display_en(address)
        and (address_display is None or needs_display_en(address_display))
    ):
        quality_flags.append("needs_address_display_en")
    return quality_flags


def _clean_repaired_display_fields(fields: Mapping[str, object]) -> dict[str, object]:
    repaired: dict[str, object] = {}
    for key, value in fields.items():
        if key not in _DISPLAY_REPAIR_FIELDS:
            continue
        normalized = _clean_display_value(value)
        if normalized is None:
            continue
        if key.endswith("_display_en") and needs_display_en(normalized):
            continue
        repaired[key] = normalized
    return repaired


def _repair_source(repair: Mapping[str, object]) -> str:
    source = repair.get("_repair_source")
    if isinstance(source, str) and source.strip():
        return source.strip()
    return "llm"


def _repaired_display_diagnostics(
    place: PlaceDetails,
    *,
    repaired_fields: Mapping[str, object],
    request_diagnostics: PlaceExtractionDiagnostics,
    repair_source: str,
) -> PlaceExtractionDiagnostics:
    existing = place.diagnostics
    quality_flags: list[str] = (
        list(existing.quality_flags) if existing is not None else []
    )
    if "category_display_en" in repaired_fields:
        quality_flags = [
            flag for flag in quality_flags if flag != "needs_category_display_en"
        ]
    if "address_display_en" in repaired_fields:
        quality_flags = [
            flag for flag in quality_flags if flag != "needs_address_display_en"
        ]
    field_sources = dict(existing.field_sources) if existing is not None else {}
    for key in (
        "category_display_en",
        "address_display_en",
    ):
        if key in repaired_fields:
            source_key = f"{key}_source"
            field_sources[key] = (
                _clean_display_value(repaired_fields.get(source_key)) or repair_source
            )
    return PlaceExtractionDiagnostics(
        field_sources=field_sources,
        missing_fields=list(existing.missing_fields) if existing is not None else [],
        quality_flags=quality_flags,
        confidence=existing.confidence if existing is not None else None,
        llm_used=repair_source not in {"cache", "translation_memory"},
        repair_source=repair_source,
        evidence_hash=request_diagnostics.evidence_hash,
        prompt_version=request_diagnostics.prompt_version,
    )


def _hash_display_repair_evidence(evidence: Mapping[str, object]) -> str:
    payload = json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _clean_display_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None

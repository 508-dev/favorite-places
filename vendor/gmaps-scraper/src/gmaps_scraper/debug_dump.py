"""Debug helpers for inspecting Google Maps saved-list payloads."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gmaps_scraper._logging import url_log_fields
from gmaps_scraper.models import PlaceDetails, PlaceExtractionDiagnostics
from gmaps_scraper.parser import (
    JSONValue,
    _candidate_nodes,
    _collect_roots,
    _extract_address,
    _find_place_metadata,
    _find_place_name,
    _is_coordinate_tuple,
    _parse_candidate_node,
    _walk_json,
)
from gmaps_scraper.url_tools import extract_list_id


@dataclass(slots=True)
class _RankedCandidate:
    root_index: int
    signal_score: int
    ranking_score: int
    place_count: int
    node: JSONValue
    parsed: dict[str, object]


@dataclass(slots=True)
class _PlaceEntry:
    node: JSONValue
    name: str | None
    address: str | None
    lat: float
    lng: float
    strings: list[str]


_logger = logging.getLogger(__name__)


def write_debug_dump(
    list_url: str,
    *,
    resolved_url: str | None = None,
    runtime_state: JSONValue | None,
    script_texts: Sequence[str],
    html: str | None,
    output_dir: Path,
    max_candidates: int = 5,
) -> Path:
    """Write raw artifacts and ranked candidate nodes for manual inspection."""
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = output_dir / "artifacts"
    candidates_dir = output_dir / "candidates"
    places_dir = output_dir / "places"
    artifacts_dir.mkdir(exist_ok=True)
    candidates_dir.mkdir(exist_ok=True)
    places_dir.mkdir(exist_ok=True)

    _write_json(artifacts_dir / "runtime_state.json", runtime_state)
    _write_json(artifacts_dir / "script_texts.json", list(script_texts))
    (artifacts_dir / "page.html").write_text(html or "", encoding="utf-8")

    list_id = extract_list_id(resolved_url or list_url)
    roots = _collect_roots(runtime_state=runtime_state, script_texts=script_texts, html=html)
    ranked_candidates = _rank_candidates(
        list_url,
        resolved_url=resolved_url,
        roots=[node for _, node in roots],
        list_id=list_id,
    )

    manifest_candidates: list[dict[str, object]] = []
    for index, candidate in enumerate(ranked_candidates[:max_candidates], start=1):
        candidate_stem = f"candidate_{index:02d}_score_{candidate.ranking_score}"
        candidate_path = candidates_dir / f"{candidate_stem}.json"
        candidate_summary_path = candidates_dir / f"{candidate_stem}.summary.json"
        _write_json(candidate_path, candidate.node)
        _write_json(
            candidate_summary_path,
            {
                "root_index": candidate.root_index,
                "signal_score": candidate.signal_score,
                "ranking_score": candidate.ranking_score,
                "parsed": candidate.parsed,
            },
        )
        manifest_candidates.append(
            {
                "file": str(candidate_path.relative_to(output_dir)),
                "summary_file": str(candidate_summary_path.relative_to(output_dir)),
                "root_index": candidate.root_index,
                "signal_score": candidate.signal_score,
                "ranking_score": candidate.ranking_score,
                "parsed": candidate.parsed,
            }
        )

    place_entries: list[dict[str, object]] = []
    if ranked_candidates:
        for index, place_entry in enumerate(
            _collect_place_entries(ranked_candidates[0].node),
            start=1,
        ):
            slug = _slugify(place_entry.name or f"place-{index}")
            place_stem = f"place_{index:02d}_{slug}"
            place_path = places_dir / f"{place_stem}.json"
            place_summary_path = places_dir / f"{place_stem}.summary.json"
            _write_json(place_path, place_entry.node)
            _write_json(
                place_summary_path,
                {
                    "name": place_entry.name,
                    "address": place_entry.address,
                    "lat": place_entry.lat,
                    "lng": place_entry.lng,
                    "strings": place_entry.strings,
                },
            )
            place_entries.append(
                {
                    "file": str(place_path.relative_to(output_dir)),
                    "summary_file": str(place_summary_path.relative_to(output_dir)),
                    "name": place_entry.name,
                    "address": place_entry.address,
                    "lat": place_entry.lat,
                    "lng": place_entry.lng,
                    "strings": place_entry.strings,
                }
            )

    summary = {
        "source_url": list_url,
        "resolved_url": resolved_url,
        "list_id": list_id,
        "root_count": len(roots),
        "candidate_count": len(ranked_candidates),
        "artifact_files": {
            "runtime_state": "artifacts/runtime_state.json",
            "script_texts": "artifacts/script_texts.json",
            "page_html": "artifacts/page.html",
        },
        "candidates": manifest_candidates,
        "places": place_entries,
    }
    summary_path = output_dir / "summary.json"
    _write_json(summary_path, summary)
    _logger.debug("Wrote saved-list debug artifacts", extra=url_log_fields(list_url, resolved_url))
    return summary_path


def write_place_debug_dump(
    place_url: str,
    *,
    resolved_url: str | None,
    snapshot: dict[str, object],
    merged_snapshot: dict[str, object],
    details: PlaceDetails,
    evidence: dict[str, object],
    diagnostics: PlaceExtractionDiagnostics,
    output_dir: Path,
) -> Path:
    """Write place snapshots, output, diagnostics, and repair evidence."""
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)

    _write_json(output_dir / "place-output.json", details.to_dict())
    _write_json(output_dir / "diagnostics.json", diagnostics.to_dict(include_debug=True))
    _write_json(output_dir / "llm-evidence.json", evidence)
    _write_json(artifacts_dir / "snapshot.json", snapshot)
    _write_json(artifacts_dir / "merged-snapshot.json", merged_snapshot)

    summary = {
        "source_url": place_url,
        "resolved_url": resolved_url,
        "place": details.to_dict(),
        "artifact_files": {
            "place_output": "place-output.json",
            "diagnostics": "diagnostics.json",
            "llm_evidence": "llm-evidence.json",
            "snapshot": "artifacts/snapshot.json",
            "merged_snapshot": "artifacts/merged-snapshot.json",
        },
        "evidence_counts": {
            "text_lines": _list_len(evidence.get("text_lines")),
            "dom_candidates": _list_len(evidence.get("dom_candidates")),
            "review_topic_candidates": _list_len(evidence.get("review_topic_candidates")),
            "review_candidates": _list_len(evidence.get("review_candidates")),
        },
    }
    summary_path = output_dir / "summary.json"
    _write_json(summary_path, summary)
    _logger.debug("Wrote place debug artifacts", extra=url_log_fields(place_url, resolved_url))
    return summary_path


def _rank_candidates(
    list_url: str,
    *,
    resolved_url: str | None,
    roots: Sequence[JSONValue],
    list_id: str | None,
) -> list[_RankedCandidate]:
    ranked: list[_RankedCandidate] = []
    seen: set[str] = set()

    for root_index, root in enumerate(roots, start=1):
        for candidate in _candidate_nodes(root, list_id=list_id):
            serialized = _serialize(candidate.node)
            if serialized in seen:
                continue
            seen.add(serialized)
            parsed = _parse_candidate_node(
                list_url,
                candidate.node,
                resolved_url=resolved_url,
                list_id=list_id,
            )
            ranking_score = candidate.signal_score + len(parsed.places) * 10
            if parsed.title is not None:
                ranking_score += 2
            if parsed.description is not None:
                ranking_score += 1
            ranked.append(
                _RankedCandidate(
                    root_index=root_index,
                    signal_score=candidate.signal_score,
                    ranking_score=ranking_score,
                    place_count=len(parsed.places),
                    node=candidate.node,
                    parsed=parsed.to_dict(),
                )
            )

    ranked.sort(
        key=lambda candidate: (
            candidate.ranking_score,
            candidate.place_count,
        ),
        reverse=True,
    )
    return ranked


def _collect_place_entries(node: JSONValue) -> list[_PlaceEntry]:
    entries: list[_PlaceEntry] = []
    seen: set[str] = set()

    for current, ancestors in _walk_json(node):
        if not _is_coordinate_tuple(current):
            continue
        lat_value = current[2]
        lng_value = current[3]
        assert isinstance(lat_value, (int, float))
        assert isinstance(lng_value, (int, float))
        metadata_node = _find_place_metadata(ancestors)
        raw_entry = _find_place_entry(ancestors) or metadata_node or current
        serialized = _serialize(raw_entry)
        if serialized in seen:
            continue
        seen.add(serialized)
        address = _extract_address(metadata_node)
        name = _guess_place_name(raw_entry, ancestors=ancestors, address=address)
        entries.append(
            _PlaceEntry(
                node=raw_entry,
                name=name,
                address=address,
                lat=float(lat_value),
                lng=float(lng_value),
                strings=_collect_strings(raw_entry),
            )
        )

    return entries


def _find_place_entry(ancestors: Sequence[JSONValue]) -> JSONValue | None:
    list_ancestors = [ancestor for ancestor in ancestors if isinstance(ancestor, list)]
    if len(list_ancestors) >= 2:
        return list_ancestors[-2]
    if list_ancestors:
        return list_ancestors[-1]
    return None


def _guess_place_name(
    raw_entry: JSONValue,
    *,
    ancestors: Sequence[JSONValue],
    address: str | None,
) -> str | None:
    direct_name = _find_direct_name(raw_entry, address=address)
    if direct_name is not None:
        return direct_name
    return _find_place_name(ancestors, address=address)


def _find_direct_name(node: JSONValue, *, address: str | None) -> str | None:
    if not isinstance(node, list):
        return None
    for value in reversed(node):
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not candidate or candidate == address:
            continue
        if candidate.startswith(("http://", "https://", "/g/")):
            continue
        return candidate
    return None


def _collect_strings(node: JSONValue) -> list[str]:
    strings: list[str] = []
    seen: set[str] = set()
    for current, _ in _walk_json(node):
        if not isinstance(current, str):
            continue
        value = current.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        strings.append(value)
    return strings


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _list_len(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _serialize(node: JSONValue) -> str:
    return json.dumps(node, ensure_ascii=False, sort_keys=True)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"

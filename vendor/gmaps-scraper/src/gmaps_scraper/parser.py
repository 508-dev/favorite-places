"""Parser for Google Maps saved-list runtime data."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import TypeGuard, cast
from urllib.parse import urlencode

from gmaps_scraper.models import ListOwner, Place, SavedList
from gmaps_scraper.url_tools import (
    extract_list_id,
    extract_list_id_from_text,
    has_placelist_marker,
)

type JSONScalar = None | bool | int | float | str
type JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_XSSI_PREFIX = ")]}'"
_APP_STATE_PATTERN = re.compile(r"APP_INITIALIZATION_STATE\s*=\s*", re.MULTILINE)
_FAVORITE_MARKERS = frozenset({"❤", "♥", "♥️", "❤️"})
_LONG_INTEGER_PATTERN = re.compile(r"-?\d{10,}")
_ADDRESS_HINT_PATTERN = re.compile(
    r"(?:"
    r"\d"
    r"|,"
    r"|〒"
    r"|号"
    r"|\bstreet\b"
    r"|\bst\b\.?"
    r"|\bavenue\b"
    r"|\bave\b\.?"
    r"|\broad\b"
    r"|\brd\b\.?"
    r"|\bboulevard\b"
    r"|\bblvd\b\.?"
    r"|\blane\b"
    r"|\bln\b\.?"
    r"|\bdrive\b"
    r"|\bdr\b\.?"
    r"|\bway\b"
    r"|\bplace\b"
    r"|\bpl\b\.?"
    r"|\bcourt\b"
    r"|\bct\b\.?"
    r"|\bsquare\b"
    r"|\bsq\b\.?"
    r"|\bsuite\b"
    r"|\bste\b\.?"
    r"|\bunit\b"
    r"|\bfloor\b"
    r"|\bfl\b\.?"
    r"|\bbuilding\b"
    r"|\bcity\b"
    r"|\bdistrict\b"
    r"|\bward\b"
    r"|\bprefecture\b"
    r"|\bprovince\b"
    r"|\bstate\b"
    r")",
    re.IGNORECASE,
)


@dataclass(slots=True)
class _Candidate:
    node: JSONValue
    signal_score: int


class ParseError(RuntimeError):
    """Raised when a saved list cannot be parsed from the supplied artifacts."""


def parse_saved_list_artifacts(
    list_url: str,
    *,
    resolved_url: str | None = None,
    runtime_state: JSONValue | None = None,
    script_texts: Sequence[str] = (),
    html: str | None = None,
) -> SavedList:
    """Parse a saved list from browser artifacts."""
    list_id = extract_list_id(resolved_url or "") or extract_list_id(list_url)
    roots = _collect_roots(runtime_state=runtime_state, script_texts=script_texts, html=html)
    best_result: SavedList | None = None
    best_score = -1

    for root in roots:
        for candidate in _candidate_nodes(root, list_id=list_id):
            parsed = _parse_candidate_node(
                list_url,
                candidate.node,
                resolved_url=resolved_url,
                list_id=list_id,
            )
            score = candidate.signal_score + len(parsed.places) * 10
            if parsed.title is not None:
                score += 2
            if parsed.description is not None:
                score += 1
            if score > best_score:
                best_result = parsed
                best_score = score

    if best_result is None or not best_result.places:
        raise ParseError("Could not locate a placelist node with parsable places.")
    return best_result


def _collect_roots(
    *,
    runtime_state: JSONValue | None,
    script_texts: Sequence[str],
    html: str | None,
) -> list[JSONValue]:
    roots: list[JSONValue] = []
    if isinstance(runtime_state, (list, dict)):
        roots.append(runtime_state)
        for value in _iter_strings(runtime_state):
            roots.extend(_decode_embedded_json(value))
    elif isinstance(runtime_state, str):
        roots.extend(_decode_embedded_json(runtime_state))

    for text in script_texts:
        roots.extend(_decode_embedded_json(text))

    if html is not None:
        roots.extend(_decode_embedded_json(html))

    return _dedupe_roots(roots)


def _dedupe_roots(roots: Iterable[JSONValue]) -> list[JSONValue]:
    deduped: list[JSONValue] = []
    seen_serialized: set[str] = set()
    for root in roots:
        try:
            serialized = json.dumps(root, sort_keys=True)
        except TypeError:
            serialized = repr(root)
        if serialized in seen_serialized:
            continue
        deduped.append(root)
        seen_serialized.add(serialized)
    return deduped


def _decode_embedded_json(text: str) -> list[JSONValue]:
    roots: list[JSONValue] = []
    for candidate in _json_text_candidates(text):
        parsed = _load_json_candidate(candidate)
        if isinstance(parsed, (list, dict)):
            roots.append(parsed)
    return roots


def _json_text_candidates(text: str) -> Iterator[str]:
    stripped = text.strip()
    if not stripped:
        return

    seen: set[str] = set()
    queue = [stripped]
    app_state_match = _APP_STATE_PATTERN.search(stripped)
    if app_state_match is not None:
        queue.append(stripped[app_state_match.end() :].lstrip())

    xssi_index = stripped.find(_XSSI_PREFIX)
    if xssi_index >= 0:
        queue.append(stripped[xssi_index:])
        queue.append(stripped[xssi_index + len(_XSSI_PREFIX) :].lstrip())

    first_array = stripped.find("[")
    first_object = stripped.find("{")
    starts = [index for index in (first_array, first_object) if index >= 0]
    if starts:
        queue.append(stripped[min(starts) :].lstrip())

    for candidate in queue:
        normalized = candidate.strip().removesuffix(";")
        normalized = _strip_xssi_prefix(normalized)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        yield normalized

        try:
            unescaped = bytes(normalized, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            continue
        unescaped = unescaped.strip().removesuffix(";")
        unescaped = _strip_xssi_prefix(unescaped)
        if unescaped and unescaped not in seen:
            seen.add(unescaped)
            yield unescaped


def _strip_xssi_prefix(text: str) -> str:
    if text.startswith(_XSSI_PREFIX):
        return text[len(_XSSI_PREFIX) :].lstrip()
    return text


def _load_json_candidate(text: str) -> JSONValue | None:
    working = text
    while working:
        try:
            parsed = json.loads(working)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, str) and parsed != working:
            working = parsed
            continue
        return cast(JSONValue, parsed)
    return None


def _candidate_nodes(root: JSONValue, *, list_id: str | None) -> list[_Candidate]:
    node_by_id: dict[int, JSONValue] = {id(root): root}
    scores: dict[int, int] = {id(root): 1}

    for node, ancestors in _walk_json(root):
        if not isinstance(node, str):
            continue
        signal = _signal_score(node, list_id=list_id)
        if signal == 0:
            continue
        list_ancestors = [ancestor for ancestor in ancestors if isinstance(ancestor, list)]
        for depth, ancestor in enumerate(reversed(list_ancestors[-6:]), start=1):
            key = id(ancestor)
            node_by_id[key] = ancestor
            scores[key] = max(scores.get(key, 0), signal * 10 - depth)

    candidates = [
        _Candidate(node=node_by_id[key], signal_score=score)
        for key, score in scores.items()
    ]
    candidates.sort(key=lambda candidate: candidate.signal_score, reverse=True)
    return candidates


def _signal_score(value: str, *, list_id: str | None) -> int:
    score = 0
    if list_id is not None and list_id in value:
        score += 3
    if has_placelist_marker(value):
        score += 1
    return score


def _parse_candidate_node(
    list_url: str,
    node: JSONValue,
    *,
    resolved_url: str | None,
    list_id: str | None,
) -> SavedList:
    places = _extract_places(node)
    title, description, owner, collaborators = _extract_metadata(node, places=places)
    resolved_list_id = list_id or _find_list_id_in_node(node)
    return SavedList(
        source_url=list_url,
        resolved_url=resolved_url,
        list_id=resolved_list_id,
        title=title,
        description=description,
        places=places,
        owner=owner,
        collaborators=collaborators,
    )


def _extract_metadata(
    node: JSONValue,
    *,
    places: Sequence[Place],
) -> tuple[str | None, str | None, ListOwner | None, list[ListOwner]]:
    metadata_node = _find_metadata_node(node)
    if metadata_node is None:
        return None, None, None, _collect_place_owners(places)

    title = _clean_text(_safe_index(metadata_node, 4))
    description = _clean_text(_safe_index(metadata_node, 5))
    if description == title:
        description = None
    owner = _parse_list_owner(_safe_index(metadata_node, 3))
    collaborators = _merge_owner_lists(
        _extract_additional_list_header_owners(metadata_node),
        _collect_place_owners(places),
    )
    if owner is not None:
        collaborators = _merge_owner_lists(collaborators, [])
        collaborators = [
            collaborator
            for collaborator in collaborators
            if not _owners_refer_to_same_person(collaborator, owner)
        ]
    return title, description, owner, collaborators


def _find_metadata_node(node: JSONValue) -> list[JSONValue] | None:
    for current, _ in _walk_json(node):
        if not isinstance(current, list):
            continue
        if len(current) < 6:
            continue
        if _contains_placelist_signal(current):
            return current
    if isinstance(node, list):
        return node
    return None


def _contains_placelist_signal(node: list[JSONValue]) -> bool:
    for value in _iter_strings(node):
        if has_placelist_marker(value):
            return True
    return False


def _extract_additional_list_header_owners(node: list[JSONValue]) -> list[ListOwner]:
    owners: list[ListOwner] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    for index, value in enumerate(node):
        if index in {3, 8}:
            continue
        for current, _ in _walk_json(value):
            owner = _parse_list_owner(current)
            if owner is None:
                continue
            key = (owner.name, owner.photo_url, owner.profile_id)
            if key in seen:
                continue
            seen.add(key)
            owners.append(owner)

    return owners


def _collect_place_owners(places: Sequence[Place]) -> list[ListOwner]:
    return _merge_owner_lists(
        [place.added_by for place in places if place.added_by is not None],
        [],
    )


def _merge_owner_lists(
    primary: Sequence[ListOwner | None],
    secondary: Sequence[ListOwner | None],
) -> list[ListOwner]:
    owners: list[ListOwner] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    for owner in [*primary, *secondary]:
        if owner is None:
            continue
        key = (owner.name, owner.photo_url, owner.profile_id)
        if key in seen:
            continue
        seen.add(key)
        owners.append(owner)

    return owners


def _parse_list_owner(node: JSONValue | None) -> ListOwner | None:
    if not isinstance(node, list) or len(node) < 1 or len(node) > 3:
        return None

    name = _clean_text(_safe_index(node, 0))
    photo_url = _clean_text(_safe_index(node, 1))
    profile_id = _clean_text(_safe_index(node, 2))

    if name is None:
        return None
    if photo_url is not None and not photo_url.startswith(("http://", "https://")):
        return None
    if profile_id is not None and not _looks_like_profile_id(profile_id):
        return None

    return ListOwner(name=name, photo_url=photo_url, profile_id=profile_id)


def _extract_places(node: JSONValue) -> list[Place]:
    places: list[Place] = []
    seen: set[str] = set()

    for current, ancestors in _walk_json(node):
        if not _is_coordinate_tuple(current):
            continue
        lat_value = current[2]
        lng_value = current[3]
        assert isinstance(lat_value, (int, float))
        assert isinstance(lng_value, (int, float))
        lat = float(lat_value)
        lng = float(lng_value)
        place_record = _find_place_record(ancestors, coordinate_tuple=current)
        metadata_node = _place_metadata_from_record(place_record)
        if metadata_node is None:
            metadata_node = _find_place_metadata(ancestors)
        address = _extract_address(metadata_node)
        cid = _find_cid(metadata_node)
        google_id = _find_google_id(metadata_node)
        name = _find_place_name(ancestors, address=address, place_record=place_record)
        note = _find_place_note(place_record, name=name, address=address)
        is_favorite = _find_place_is_favorite(place_record)
        place = Place(
            name=name or address or f"{lat:.6f},{lng:.6f}",
            address=address,
            note=note,
            lat=lat,
            lng=lng,
            maps_url=_build_maps_url(
                name=name,
                address=address,
                lat=lat,
                lng=lng,
            ),
            cid=cid,
            google_id=google_id,
            is_favorite=is_favorite,
            added_by=_find_place_added_by(place_record),
        )
        dedupe_key = (
            google_id
            or (f"{cid}:{lat:.6f}:{lng:.6f}" if cid is not None else None)
            or f"{place.name}:{lat:.6f}:{lng:.6f}"
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        places.append(place)

    return places


def _find_place_metadata(ancestors: Sequence[JSONValue]) -> list[JSONValue] | None:
    place_record = _find_place_record(ancestors)
    metadata_node = _place_metadata_from_record(place_record)
    if metadata_node is not None:
        return metadata_node
    for ancestor in reversed(ancestors):
        if not isinstance(ancestor, list):
            continue
        if _contains_coordinate_tuple_direct(ancestor):
            return ancestor
    return None


def _contains_place_metadata_signal(node: list[JSONValue]) -> bool:
    return _contains_coordinate_tuple_direct(node)


def _find_place_name(
    ancestors: Sequence[JSONValue],
    *,
    address: str | None,
    place_record: list[JSONValue] | None = None,
) -> str | None:
    if place_record is None:
        place_record = _find_place_record(ancestors)
    if place_record is None:
        return None

    scoped_nodes = [place_record]
    metadata_node = _safe_index(place_record, 1)
    if isinstance(metadata_node, list):
        scoped_nodes.append(metadata_node)

    # Prefer the enclosing place record over its nested metadata payload.
    for node in scoped_nodes:
        preferred = _clean_text(_safe_index(node, 2))
        if _is_name_candidate(preferred, address=address):
            return preferred

    for node in scoped_nodes:
        for value in reversed(node):
            candidate = _clean_text(value)
            if _is_name_candidate(candidate, address=address):
                return candidate
    return None


def _find_place_note(
    place_record: list[JSONValue] | None,
    *,
    name: str | None,
    address: str | None,
) -> str | None:
    if place_record is None:
        return None
    preferred = _clean_text(_safe_index(place_record, 3))
    if _is_note_candidate(preferred, name=name, address=address):
        return preferred
    return None


def _find_place_is_favorite(
    place_record: list[JSONValue] | None,
) -> bool:
    if place_record is None:
        return False
    favorite_payload = _safe_index(place_record, 7)
    if _contains_favorite_marker(favorite_payload):
        return True
    for index, value in enumerate(place_record):
        if index == 3:
            continue
        if _contains_favorite_marker(value):
            return True
    return False


def _find_place_added_by(place_record: list[JSONValue] | None) -> ListOwner | None:
    if place_record is None:
        return None

    preferred = _parse_list_owner(_safe_index(place_record, 12))
    if preferred is not None:
        return preferred

    for value in reversed(place_record):
        owner = _parse_list_owner(value)
        if owner is not None:
            return owner
    return None


def _find_place_record(
    ancestors: Sequence[JSONValue],
    *,
    coordinate_tuple: list[JSONValue] | None = None,
) -> list[JSONValue] | None:
    for ancestor in reversed(ancestors):
        if not isinstance(ancestor, list):
            continue
        metadata_node = _place_metadata_from_record(ancestor)
        if metadata_node is None:
            continue
        if coordinate_tuple is not None and not _metadata_matches_coordinate(
            metadata_node,
            coordinate_tuple,
        ):
            continue
        return ancestor
    return None


def _extract_address(node: list[JSONValue] | None) -> str | None:
    if node is None:
        return None

    preferred = _clean_text(_safe_index(node, 4))
    if _looks_like_address(preferred):
        return preferred

    candidates = [_clean_text(value) for value in node]
    address_candidates = [
        candidate
        for candidate in candidates
        if candidate is not None and _looks_like_address(candidate)
    ]
    if not address_candidates:
        return None
    return max(address_candidates, key=len)


def _looks_like_address(value: str | None) -> bool:
    if value is None:
        return False
    if not _is_plain_text(value):
        return False
    if value.startswith("/g/"):
        return False
    if value.isdigit():
        return False
    if len(value) < 5:
        return False
    return _ADDRESS_HINT_PATTERN.search(value) is not None


def _find_cid(node: list[JSONValue] | None) -> str | None:
    if node is None:
        return None
    structured = _find_cid_in_structured_slot(_safe_index(node, 6))
    if structured is not None:
        return structured
    for value in node:
        candidate = _find_cid_in_fallback_value(value)
        if candidate is not None:
            return candidate
    return None


def _find_google_id(node: list[JSONValue] | None) -> str | None:
    if node is None:
        return None
    preferred = _clean_text(_safe_index(node, 7))
    if preferred is not None and preferred.startswith("/g/"):
        return preferred
    for value in node:
        candidate = _clean_text(value)
        if candidate is None:
            continue
        if candidate.startswith("/g/"):
            return candidate
    return None


def _place_metadata_from_record(node: JSONValue | None) -> list[JSONValue] | None:
    if not isinstance(node, list):
        return None
    metadata_node = _safe_index(node, 1)
    if not isinstance(metadata_node, list):
        return None
    if not _contains_coordinate_tuple_direct(metadata_node):
        return None
    return metadata_node


def _contains_coordinate_tuple_direct(node: list[JSONValue]) -> bool:
    return any(_is_coordinate_tuple(value) for value in node)


def _metadata_matches_coordinate(
    metadata_node: list[JSONValue],
    coordinate_tuple: list[JSONValue],
) -> bool:
    return any(
        isinstance(value, list) and value == coordinate_tuple
        for value in metadata_node
    )


def _find_cid_in_structured_slot(value: JSONValue | None) -> str | None:
    if isinstance(value, int):
        return _normalize_cid_token(str(value))
    if isinstance(value, str):
        return _normalize_cid_token(value)
    if not isinstance(value, list):
        return None
    owner = _parse_list_owner(value)
    if owner is not None and (owner.photo_url is not None or owner.profile_id is not None):
        return None

    numeric_texts = [
        text
        for text in (_clean_text(item) for item in value)
        if text is not None and _LONG_INTEGER_PATTERN.fullmatch(text) is not None
    ]
    if not numeric_texts:
        return None
    if len(numeric_texts) >= 2:
        return _normalize_cid_token(numeric_texts[1])
    return _normalize_cid_token(numeric_texts[0])


def _find_cid_in_fallback_value(value: JSONValue | None) -> str | None:
    if isinstance(value, int | str):
        return _normalize_fallback_cid_token(value)
    if not isinstance(value, list):
        return None
    owner = _parse_list_owner(value)
    if owner is not None and (owner.photo_url is not None or owner.profile_id is not None):
        return None
    numeric_texts = [
        text
        for text in (_clean_text(item) for item in value)
        if text is not None and _LONG_INTEGER_PATTERN.fullmatch(text) is not None
    ]
    if len(numeric_texts) == 1:
        return _normalize_fallback_cid_token(numeric_texts[0])
    return None


def _normalize_fallback_cid_token(value: JSONValue | None) -> str | None:
    text = str(value) if isinstance(value, int) else _clean_text(value)
    if text is None or _LONG_INTEGER_PATTERN.fullmatch(text) is None:
        return None
    if len(text.removeprefix("-")) < 15:
        return None
    return _normalize_cid_token(text)


def _normalize_cid_token(value: JSONValue | None) -> str | None:
    text = _clean_text(value)
    if text is None or _LONG_INTEGER_PATTERN.fullmatch(text) is None:
        return None
    number = int(text)
    if number < 0:
        number += 1 << 64
    return str(number)


def _is_place_record_node(node: list[JSONValue]) -> bool:
    return _place_metadata_from_record(node) is not None


def _contains_favorite_marker(node: JSONValue) -> bool:
    return any(value in _FAVORITE_MARKERS for value in _iter_strings(node))


def _find_list_id_in_node(node: JSONValue) -> str | None:
    for value in _iter_strings(node):
        candidate = extract_list_id_from_text(value)
        if candidate is not None:
            return candidate
    return None


def _build_maps_url(
    *,
    name: str | None,
    address: str | None,
    lat: float,
    lng: float,
) -> str:
    query = _build_maps_query(name=name, address=address, lat=lat, lng=lng)
    return f"https://www.google.com/maps/search/?{urlencode({'api': '1', 'query': query})}"


def _build_maps_query(
    *,
    name: str | None,
    address: str | None,
    lat: float,
    lng: float,
) -> str:
    if name is not None and address is not None:
        return f"{name}, {address}"
    if name is not None:
        return name
    if address is not None:
        return address
    return f"{lat:.7f},{lng:.7f}"


def _walk_json(
    node: JSONValue,
    ancestors: tuple[JSONValue, ...] = (),
) -> Iterator[tuple[JSONValue, tuple[JSONValue, ...]]]:
    yield node, ancestors
    if isinstance(node, list):
        for child in node:
            yield from _walk_json(child, ancestors + (node,))
    elif isinstance(node, dict):
        for child in node.values():
            yield from _walk_json(child, ancestors + (node,))


def _iter_strings(node: JSONValue) -> Iterator[str]:
    for current, _ in _walk_json(node):
        if isinstance(current, str):
            candidate = _clean_text(current)
            if candidate is not None:
                yield candidate


def _is_coordinate_tuple(node: JSONValue) -> TypeGuard[list[JSONValue]]:
    if not isinstance(node, list) or len(node) < 4:
        return False
    if node[0] is not None or node[1] is not None:
        return False
    return isinstance(node[2], (int, float)) and isinstance(node[3], (int, float))


def _safe_index(node: Sequence[JSONValue], index: int) -> JSONValue | None:
    if index >= len(node):
        return None
    return node[index]


def _clean_text(value: JSONValue | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


def _is_name_candidate(value: str | None, *, address: str | None) -> bool:
    if value is None:
        return False
    if not _is_plain_text(value):
        return False
    if address is not None and value == address:
        return False
    return True


def _is_note_candidate(
    value: str | None,
    *,
    name: str | None,
    address: str | None,
) -> bool:
    if value is None:
        return False
    if not _is_note_text(value):
        return False
    if name is not None and value == name:
        return False
    if address is not None and value == address:
        return False
    return True


def _is_plain_text(value: str) -> bool:
    if value.startswith(("http://", "https://", "/g/")):
        return False
    if "http://" in value or "https://" in value:
        return False
    if has_placelist_marker(value):
        return False
    if value.startswith(_XSSI_PREFIX):
        return False
    return True


def _is_note_text(value: str) -> bool:
    if value.startswith(("http://", "https://", "/g/")):
        return False
    if value.startswith(_XSSI_PREFIX):
        return False
    return True


def _owners_refer_to_same_person(left: ListOwner, right: ListOwner) -> bool:
    if left.profile_id is not None and right.profile_id is not None:
        return left.profile_id == right.profile_id

    left_name = _normalize_owner_name(left.name)
    right_name = _normalize_owner_name(right.name)
    if left_name != right_name:
        return False

    if left.photo_url is not None and right.photo_url is not None:
        return left.photo_url == right.photo_url
    return False


def _normalize_owner_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _looks_like_cid_candidate(value: str) -> bool:
    normalized = value.removeprefix("-")
    return normalized.isdigit() and len(normalized) >= 10


def _looks_like_profile_id(value: str) -> bool:
    return value.isdigit() and len(value) >= 10

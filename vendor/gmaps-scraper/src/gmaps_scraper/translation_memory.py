"""Conservative English-readable normalization for place metadata."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

_NON_LATIN_SCRIPT_PATTERN = re.compile(
    r"[\u0370-\u03ff\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff"
    r"\u0900-\u097f\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff]"
)
_URL_LIKE_PATTERN = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_ADDRESS_REJECT_SUBSTRINGS = (
    "about this data",
    "faviconv2",
    "imagery ©",
    "imagery©",
    "map data ©",
    "map data©",
    "saved in",
    "send product feedback",
    "street view",
    "termsprivacy",
)
_ADDRESS_FIELD_KINDS = {"address", "address_component", "city", "country", "neighborhood"}
_CATEGORY_FIELD_KINDS = {"category"}
_LEARNED_MEMORY_LOCKS: dict[Path, threading.Lock] = {}
_LEARNED_MEMORY_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class TranslationMemoryEntry:
    """A reusable, typed phrase mapping."""

    source: str
    target: str
    field_kinds: tuple[str, ...]
    source_method: str = "approved"
    confidence: str = "high"


@dataclass(frozen=True, slots=True)
class TranslationPatternEntry:
    """A reusable, typed regex mapping with capture templates."""

    source_pattern: str
    target_template: str
    field_kinds: tuple[str, ...]
    source_method: str = "approved"
    confidence: str = "high"
    compiled_pattern: re.Pattern[str] | None = None


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """A normalized display value plus provenance."""

    text: str
    source: str
    confidence: str


class TranslationMemory:
    """Applies typed, approved translations to bounded place fields only."""

    def __init__(
        self,
        entries: Sequence[TranslationMemoryEntry | TranslationPatternEntry] = (),
    ) -> None:
        self._category_entries = [
            entry
            for entry in entries
            if isinstance(entry, TranslationMemoryEntry)
            and _CATEGORY_FIELD_KINDS.intersection(entry.field_kinds)
        ]
        self._address_entries = [
            entry
            for entry in entries
            if isinstance(entry, TranslationMemoryEntry)
            and _ADDRESS_FIELD_KINDS.intersection(entry.field_kinds)
        ]
        self._address_pattern_entries = [
            entry
            for entry in entries
            if isinstance(entry, TranslationPatternEntry)
            and _ADDRESS_FIELD_KINDS.intersection(entry.field_kinds)
            and entry.compiled_pattern is not None
        ]

    @classmethod
    def default(cls) -> TranslationMemory:
        """Build the bundled approved translation memory."""
        return cls(_approved_entries())

    @classmethod
    def from_file(cls, path: Path) -> TranslationMemory:
        """Load approved or locally learned entries from JSON, plus bundled entries."""
        return cls([*_approved_entries(), *_read_entries(path)])

    def normalize_category(self, category: object) -> TranslationResult | None:
        """Return an English-readable category label when an approved entry exists."""
        normalized = _clean_display_text(category)
        if normalized is None or not needs_display_en(normalized):
            return None
        for entry in self._category_entries:
            if normalized == entry.source:
                return TranslationResult(
                    text=entry.target,
                    source="translation_memory",
                    confidence=entry.confidence,
                )
        return None

    def normalize_address(self, address: object) -> TranslationResult | None:
        """Return an English-readable display address using conservative replacements."""
        normalized = _clean_address_text(address)
        if normalized is None or not needs_display_en(normalized):
            return None

        exact = _exact_address_translation(normalized, self._address_entries)
        if exact is not None:
            return exact

        translated_parts: list[str] = []
        confidence = "high"
        for part in normalized.split(","):
            translated_part = part.strip()
            if not translated_part:
                continue
            translated_part, confidence = _translate_address_component(
                translated_part,
                pattern_entries=self._address_pattern_entries,
                phrase_entries=self._address_entries,
                confidence=confidence,
            )
            translated_parts.append(translated_part)
        translated = ", ".join(translated_parts)
        translated = _clean_translated_address(translated)
        if translated == normalized or needs_display_en(translated):
            return None
        return TranslationResult(
            text=translated,
            source="translation_memory",
            confidence=confidence,
        )


def _exact_address_translation(
    normalized: str,
    entries: Sequence[TranslationMemoryEntry],
) -> TranslationResult | None:
    for entry in entries:
        if normalized != entry.source:
            continue
        translated = _clean_translated_address(entry.target)
        if translated == normalized or needs_display_en(translated):
            return None
        return TranslationResult(
            text=translated,
            source="translation_memory",
            confidence=entry.confidence,
        )
    return None


def _translate_address_component(
    value: str,
    *,
    pattern_entries: Sequence[TranslationPatternEntry],
    phrase_entries: Sequence[TranslationMemoryEntry],
    confidence: str,
) -> tuple[str, str]:
    translated = value
    next_confidence = confidence
    for pattern_entry in pattern_entries:
        if pattern_entry.compiled_pattern is None:
            continue
        next_translated = _apply_pattern_entry(pattern_entry, translated)
        if next_translated != translated and pattern_entry.confidence != "high":
            next_confidence = pattern_entry.confidence
        translated = next_translated
    for phrase_entry in phrase_entries:
        if phrase_entry.source in translated and phrase_entry.confidence != "high":
            next_confidence = phrase_entry.confidence
        translated = translated.replace(phrase_entry.source, phrase_entry.target)
    return translated, next_confidence


def needs_display_en(value: str | None) -> bool:
    """Return true when the value contains scripts needing English display text."""
    return value is not None and _NON_LATIN_SCRIPT_PATTERN.search(value) is not None


def write_learned_translation_memory(
    path: Path,
    *,
    current_fields: Mapping[str, object],
    repair: Mapping[str, object],
) -> None:
    """Append exact, typed translation-memory entries learned from LLM output."""
    candidates = _pending_entries_from_repair(current_fields=current_fields, repair=repair)
    if not candidates:
        return

    with _learned_memory_lock(path):
        _merge_learned_translation_memory(path, candidates)


def _merge_learned_translation_memory(
    path: Path,
    candidates: list[dict[str, object]],
) -> None:
    existing = _read_pending_payload(path)
    raw_entries = existing.setdefault("entries", [])
    if isinstance(raw_entries, list):
        entries: list[object] = raw_entries
    else:
        entries = []
        existing["entries"] = entries
    index: dict[tuple[object, object, tuple[object, ...]], dict[object, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_field_kinds = entry.get("field_kinds")
        if isinstance(raw_field_kinds, list):
            field_kinds: list[object] = raw_field_kinds
        else:
            field_kinds = []
        index[(entry.get("source"), entry.get("target"), tuple(field_kinds))] = entry
    for candidate in candidates:
        key = _memory_entry_key(candidate)
        existing_entry = index.get(key)
        if isinstance(existing_entry, dict):
            count = existing_entry.get("observed_count")
            existing_entry["observed_count"] = count + 1 if isinstance(count, int) else 2
            continue
        entries.append(candidate)

    _write_json_atomic(path, existing)


def _learned_memory_lock(path: Path) -> threading.Lock:
    lock_path = path.expanduser().resolve()
    with _LEARNED_MEMORY_LOCKS_GUARD:
        lock = _LEARNED_MEMORY_LOCKS.get(lock_path)
        if lock is None:
            lock = threading.Lock()
            _LEARNED_MEMORY_LOCKS[lock_path] = lock
        return lock


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _memory_entry_key(entry: Mapping[str, object]) -> tuple[object, object, tuple[object, ...]]:
    raw_field_kinds = entry.get("field_kinds")
    if isinstance(raw_field_kinds, list):
        field_kinds: list[object] = raw_field_kinds
    else:
        field_kinds = []
    return (entry.get("source"), entry.get("target"), tuple(field_kinds))


def _pending_entries_from_repair(
    *,
    current_fields: Mapping[str, object],
    repair: Mapping[str, object],
) -> list[dict[str, object]]:
    fields = repair.get("fields")
    repair_fields = fields if isinstance(fields, Mapping) else repair
    pending: list[dict[str, object]] = []

    raw_category = _clean_display_text(current_fields.get("category"))
    display_category = _clean_display_text(repair_fields.get("category_display_en"))
    if _is_learnable_pair(raw_category, display_category):
        assert raw_category is not None
        assert display_category is not None
        pending.append(
            _pending_entry(
                source=raw_category,
                target=display_category,
                field_kinds=["category"],
            )
        )

    raw_address = _clean_address_text(current_fields.get("address"))
    display_address = _clean_address_text(repair_fields.get("address_display_en"))
    if raw_address is not None and display_address is not None:
        pending.extend(_pending_address_component_entries(raw_address, display_address))
    return pending


def _pending_address_component_entries(
    raw_address: str,
    display_address: str,
) -> list[dict[str, object]]:
    raw_parts = [part.strip() for part in raw_address.split(",")]
    display_parts = [part.strip() for part in display_address.split(",")]
    if len(raw_parts) != len(display_parts):
        return []
    pending: list[dict[str, object]] = []
    for raw_part, display_part in zip(raw_parts, display_parts, strict=True):
        if _is_learnable_pair(raw_part, display_part):
            pending.append(
                _pending_entry(
                    source=raw_part,
                    target=display_part,
                    field_kinds=["address_component"],
                )
            )
    return pending


def _is_learnable_pair(source: str | None, target: str | None) -> bool:
    if source is None or target is None or source == target:
        return False
    if not needs_display_en(source) or needs_display_en(target):
        return False
    if _URL_LIKE_PATTERN.search(source) is not None or _URL_LIKE_PATTERN.search(target) is not None:
        return False
    return len(source) <= 160 and len(target) <= 160


def _apply_pattern_entry(entry: TranslationPatternEntry, value: str) -> str:
    if entry.compiled_pattern is None:
        return value

    def replace_match(match: re.Match[str]) -> str:
        return _render_pattern_template(entry.target_template, match)

    return entry.compiled_pattern.sub(replace_match, value)


def _pending_entry(
    *,
    source: str,
    target: str,
    field_kinds: list[str],
) -> dict[str, object]:
    return {
        "source": source,
        "target": target,
        "field_kinds": field_kinds,
        "source_method": "llm",
        "confidence": "medium",
        "status": "learned",
        "observed_count": 1,
    }


def _clean_display_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _clean_address_text(value: object) -> str | None:
    normalized = _clean_display_text(value)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if _URL_LIKE_PATTERN.search(normalized) is not None:
        return None
    if any(fragment in lowered for fragment in _ADDRESS_REJECT_SUBSTRINGS):
        return None
    return normalized


def _clean_translated_address(value: str) -> str:
    translated = value
    translated = re.sub(r"\s+,", ",", translated)
    translated = re.sub(r",\s*,+", ",", translated)
    translated = re.sub(r"\s{2,}", " ", translated)
    return translated.strip(" ,")


def _read_entries(path: Path) -> list[TranslationMemoryEntry | TranslationPatternEntry]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    return _parse_entries_payload(payload)


def _parse_entry(value: object) -> TranslationMemoryEntry | TranslationPatternEntry | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("kind") == "pattern":
        return _parse_pattern_entry(value)
    return _parse_phrase_entry(value)


def _parse_phrase_entry(value: Mapping[object, object]) -> TranslationMemoryEntry | None:
    source = _clean_display_text(value.get("source"))
    target = _clean_display_text(value.get("target"))
    field_kinds = value.get("field_kinds")
    if source is None or target is None or not isinstance(field_kinds, list):
        return None
    kinds = tuple(kind for kind in field_kinds if isinstance(kind, str) and kind)
    if not kinds:
        return None
    confidence = _clean_display_text(value.get("confidence")) or "medium"
    source_method = _clean_display_text(value.get("source_method")) or "approved"
    return TranslationMemoryEntry(
        source=source,
        target=target,
        field_kinds=kinds,
        source_method=source_method,
        confidence=confidence,
    )


def _parse_pattern_entry(value: Mapping[object, object]) -> TranslationPatternEntry | None:
    source_pattern = _clean_display_text(value.get("source_pattern"))
    target_template = _clean_template_text(value.get("target_template"))
    field_kinds = value.get("field_kinds")
    if source_pattern is None or target_template is None or not isinstance(field_kinds, list):
        return None
    kinds = tuple(kind for kind in field_kinds if isinstance(kind, str) and kind)
    if not kinds:
        return None
    try:
        compiled_pattern = re.compile(source_pattern)
    except re.error:
        return None
    confidence = _clean_display_text(value.get("confidence")) or "medium"
    source_method = _clean_display_text(value.get("source_method")) or "approved"
    return TranslationPatternEntry(
        source_pattern=source_pattern,
        target_template=target_template,
        field_kinds=kinds,
        source_method=source_method,
        confidence=confidence,
        compiled_pattern=compiled_pattern,
    )


def _clean_template_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value


def _render_pattern_template(template: str, match: re.Match[str]) -> str:
    def replace_group(group_match: re.Match[str]) -> str:
        group_index = int(group_match.group(1))
        try:
            return match.group(group_index) or ""
        except IndexError:
            return ""

    return re.sub(r"\{(\d+)\}", replace_group, template)


def _read_pending_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"entries": []}
    return dict(payload) if isinstance(payload, Mapping) else {"entries": []}


def _approved_entries() -> list[TranslationMemoryEntry | TranslationPatternEntry]:
    try:
        payload = json.loads(
            resources.files("gmaps_scraper.data")
            .joinpath("translation-memory.approved.json")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError, json.JSONDecodeError):
        return list(_FALLBACK_APPROVED_ENTRIES)
    entries = _parse_entries_payload(payload)
    return entries or list(_FALLBACK_APPROVED_ENTRIES)


def _parse_entries_payload(payload: object) -> list[
    TranslationMemoryEntry | TranslationPatternEntry
]:
    entries = payload.get("entries") if isinstance(payload, Mapping) else payload
    if not isinstance(entries, list):
        return []
    parsed: list[TranslationMemoryEntry | TranslationPatternEntry] = []
    for entry in entries:
        parsed_entry = _parse_entry(entry)
        if parsed_entry is not None:
            parsed.append(parsed_entry)
    return parsed


_FALLBACK_APPROVED_ENTRIES: tuple[TranslationMemoryEntry | TranslationPatternEntry, ...] = (
    TranslationMemoryEntry("シンガポール", "Singapore", ("country", "city")),
    TranslationMemoryEntry("台北市", "Taipei City", ("city",)),
    TranslationMemoryEntry("中山區", "Zhongshan District", ("neighborhood",)),
    TranslationMemoryEntry("中山区", "Zhongshan District", ("neighborhood",)),
    TranslationMemoryEntry("康樂里", "Kangle Village", ("neighborhood",)),
    TranslationMemoryEntry("康楽里", "Kangle Village", ("neighborhood",)),
    TranslationMemoryEntry("台湾", "Taiwan", ("country",)),
    TranslationMemoryEntry("臺灣", "Taiwan", ("country",)),
    TranslationMemoryEntry("日本", "Japan", ("country",)),
    TranslationMemoryEntry("東京都", "Tokyo", ("city",)),
    TranslationMemoryEntry("東京", "Tokyo", ("city",)),
    TranslationMemoryEntry("大阪府", "Osaka Prefecture", ("city",)),
    TranslationMemoryEntry("大阪市", "Osaka", ("city",)),
    TranslationMemoryEntry("京都府", "Kyoto Prefecture", ("city",)),
    TranslationMemoryEntry("京都市", "Kyoto", ("city",)),
    TranslationMemoryEntry("韓国", "South Korea", ("country",)),
    TranslationMemoryEntry("대한민국", "South Korea", ("country",)),
    TranslationMemoryEntry("한남동", "Hannam-dong", ("neighborhood",)),
    TranslationMemoryEntry("롯데월드타워", "Lotte World Tower", ("address_component",)),
    TranslationPatternEntry(
        r"(?<![A-Za-z0-9])(\d+)\s*지하\s*(\d+)\s*층",
        "{1} B{2}",
        ("address_component",),
        compiled_pattern=re.compile(r"(?<![A-Za-z0-9])(\d+)\s*지하\s*(\d+)\s*층"),
    ),
    TranslationPatternEntry(
        r"지하\s*(\d+)\s*층",
        "B{1}",
        ("address_component",),
        compiled_pattern=re.compile(r"지하\s*(\d+)\s*층"),
    ),
    TranslationPatternEntry(
        r"(?<![A-Za-z0-9])(\d+)\s*층",
        "{1}F",
        ("address_component",),
        compiled_pattern=re.compile(r"(?<![A-Za-z0-9])(\d+)\s*층"),
    ),
    TranslationPatternEntry(
        r"(?<![A-Za-z0-9])(\d+)\s*地下",
        "Basement #{1}",
        ("address_component",),
        compiled_pattern=re.compile(r"(?<![A-Za-z0-9])(\d+)\s*地下"),
    ),
    TranslationPatternEntry(
        r"\bNo\.\s*(\d+)\s*之\s*(\d+)\s*號",
        "No. {1}-{2}",
        ("address_component",),
        compiled_pattern=re.compile(r"\bNo\.\s*(\d+)\s*之\s*(\d+)\s*號"),
    ),
    TranslationPatternEntry(
        r"(?<![A-Za-z0-9])(\d+)\s*樓\s*之\s*(\d+)",
        "{1}F-{2}",
        ("address_component",),
        compiled_pattern=re.compile(r"(?<![A-Za-z0-9])(\d+)\s*樓\s*之\s*(\d+)"),
    ),
    TranslationPatternEntry(
        r"(?<![A-Za-z0-9])(\d+)\s*樓",
        "{1}F",
        ("address_component",),
        compiled_pattern=re.compile(r"(?<![A-Za-z0-9])(\d+)\s*樓"),
    ),
    TranslationPatternEntry(
        r"(?<=\d)\s*號\b",
        "",
        ("address_component",),
        compiled_pattern=re.compile(r"(?<=\d)\s*號\b"),
    ),
    TranslationMemoryEntry("タイ", "Thailand", ("country",)),
    TranslationMemoryEntry("ベトナム", "Vietnam", ("country",)),
    TranslationMemoryEntry("イタリア料理店", "Italian restaurant", ("category",)),
    TranslationMemoryEntry("イベント会場", "Event venue", ("category",)),
    TranslationMemoryEntry("タイ料理店", "Thai restaurant", ("category",)),
    TranslationMemoryEntry("高級料理レストラン", "Fine dining restaurant", ("category",)),
    TranslationMemoryEntry("レストラン", "Restaurant", ("category",)),
    TranslationMemoryEntry("ラーメン屋", "Ramen restaurant", ("category",)),
    TranslationMemoryEntry("和食店", "Japanese restaurant", ("category",)),
    TranslationMemoryEntry("寿司店", "Sushi restaurant", ("category",)),
    TranslationMemoryEntry("カフェ", "Cafe", ("category",)),
    TranslationMemoryEntry("ホテル", "Hotel", ("category",)),
    TranslationMemoryEntry("칵테일바", "Cocktail bar", ("category",)),
    TranslationMemoryEntry("한식당", "Korean restaurant", ("category",)),
    TranslationMemoryEntry("酒吧", "Bar", ("category",)),
    TranslationMemoryEntry("中菜館", "Chinese restaurant", ("category",)),
)

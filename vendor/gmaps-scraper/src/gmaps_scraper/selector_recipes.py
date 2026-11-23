"""Deprecated compatibility helpers for the archived place selector recipe."""

from __future__ import annotations

import json
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

# Frozen v1 debug format. Update selectors in the live extractor.
DEFAULT_PLACE_SELECTOR_RECIPE: dict[str, Any] = json.loads(
    resources.files("gmaps_scraper.data")
    .joinpath("legacy-place-selector-recipe-v1.json")
    .read_text(encoding="utf-8")
)


def default_place_selector_recipe() -> dict[str, Any]:
    """Return a copy of the archived v1 recipe for legacy debug tooling."""
    return deepcopy(DEFAULT_PLACE_SELECTOR_RECIPE)


def load_place_selector_recipe(path: Path) -> dict[str, Any]:
    """Read a legacy recipe for inspection. Extraction uses its own selectors."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Place selector recipe must be a JSON object.")
    version = payload.get("version")
    if not isinstance(version, int):
        raise ValueError("Place selector recipe must include an integer version.")
    selectors = payload.get("selectors")
    if not isinstance(selectors, dict):
        raise ValueError("Place selector recipe must include selector mappings.")
    return payload


def write_default_place_selector_recipe(path: Path) -> Path:
    """Write the archived v1 recipe for legacy debug tooling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(default_place_selector_recipe(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path

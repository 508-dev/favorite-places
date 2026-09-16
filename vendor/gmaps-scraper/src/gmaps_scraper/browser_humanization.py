"""Browser launch randomization helpers."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Literal

type BrowserWindowSize = Literal["random"] | tuple[int, int] | None

_WEIGHTED_DESKTOP_WINDOW_SIZES: tuple[tuple[int, int], ...] = (
    *((1920, 1080),) * 35,
    *((1366, 768),) * 26,
    *((1536, 864),) * 16,
    *((1280, 720),) * 9,
    *((1440, 900),) * 9,
    *((1600, 900),) * 5,
)


def resolve_browser_window_size(
    window_size: BrowserWindowSize,
    *,
    profile_dir: Path | None,
) -> tuple[int, int] | None:
    """Resolve a configured browser window size."""
    if window_size is None:
        return None
    if window_size == "random":
        if profile_dir is None:
            return random.choice(_WEIGHTED_DESKTOP_WINDOW_SIZES)
        return _WEIGHTED_DESKTOP_WINDOW_SIZES[
            _stable_hash(str(profile_dir)) % len(_WEIGHTED_DESKTOP_WINDOW_SIZES)
        ]

    width, height = window_size
    if width <= 0 or height <= 0:
        raise ValueError("Browser window dimensions must be positive.")
    return width, height


def _stable_hash(text: str) -> int:
    value = 0
    for character in text:
        value = (value * 281 ^ ord(character) * 997) & 0xFFFFFFFF
    return value

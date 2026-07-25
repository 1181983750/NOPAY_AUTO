"""Text and path helpers shared by vision, browser and page modules."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]


def resolve_path(value: str | Path, base: Path = ROOT) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    return path if path.is_absolute() else (base / path).resolve()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def split_terms(value: Iterable[str] | str | None) -> list[str]:
    if value is None:
        return []
    raw = re.split(r"[,，|;；\n]+", value) if isinstance(value, str) else list(value)
    return [normalize_text(item) for item in raw if normalize_text(item)]


def text_has_any(text: str, terms: Iterable[str] | str | None) -> bool:
    normalized = normalize_text(text)
    return any(term in normalized for term in split_terms(terms))


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))

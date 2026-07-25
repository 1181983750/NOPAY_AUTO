"""Immutable input passed from the router to one page or dialog handler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PageContext:
    """One OCR scan and the browser objects needed to act on it."""

    browser: Any
    rect: Any
    image: Any
    boxes: list[Any]

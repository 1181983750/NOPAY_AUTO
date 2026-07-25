"""Small immutable-ish data structures passed between automation layers."""

from __future__ import annotations

from dataclasses import dataclass

from .text import normalize_text


@dataclass
class Rect:
    left: float
    top: float
    width: float
    height: float
    source: str = "unknown"

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def point(self, rx: float, ry: float) -> tuple[float, float]:
        return self.left + self.width * rx, self.top + self.height * ry


@dataclass
class TextBox:
    text: str
    confidence: float
    left: float
    top: float
    right: float
    bottom: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.left + self.right) / 2, (self.top + self.bottom) / 2

    @property
    def normalized(self) -> str:
        return normalize_text(self.text)


@dataclass
class TemplateMatch:
    name: str
    confidence: float
    left: float
    top: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return self.left + self.width / 2, self.top + self.height / 2

"""Shared helpers for editable payment keyword options."""

from __future__ import annotations

import re
from typing import Any


def split_payment_keywords(value: Any) -> list[str]:
    """Split legacy keyword input while preserving the entered order."""
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = re.split(r"[,，;；\n]+", str(value or ""))
    return [str(item).strip() for item in values if str(item).strip()]


def normalize_payment_keyword_options(value: Any, fallback: Any = "") -> list[dict[str, Any]]:
    """Normalize checkbox options and keep compatibility with string lists."""
    raw_options = value if value is not None else fallback
    if isinstance(raw_options, dict):
        raw_options = [{"keyword": keyword, "enabled": enabled} for keyword, enabled in raw_options.items()]

    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw_options, (list, tuple)):
        entries = raw_options
    else:
        entries = split_payment_keywords(raw_options)

    for entry in entries:
        if isinstance(entry, dict):
            keyword = str(entry.get("keyword") or entry.get("name") or "").strip()
            enabled = entry.get("enabled", True)
        else:
            keyword = str(entry).strip()
            enabled = True
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        options.append({"keyword": keyword, "enabled": enabled is True or str(enabled).lower() in {"1", "true", "on", "yes"}})
    return options


def enabled_payment_keywords(options: list[dict[str, Any]]) -> list[str]:
    """Return enabled keywords in their configured order."""
    return [str(option["keyword"]).strip() for option in options if option.get("enabled") and str(option.get("keyword") or "").strip()]

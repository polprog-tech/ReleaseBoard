"""Internationalization (i18n) module for ReleaseBoard.

Provides a lightweight, JSON-based translation system supporting:
- Translation catalogs per locale (EN, PL)
- Locale detection from Accept-Language headers
- Fallback to English when a key is missing in the target locale
- Interpolation via Python str.format()
- Basic pluralization (key.one / key.few / key.other for Polish)
- Async-safe locale context via contextvars for request-scoped translations
"""

from __future__ import annotations

import contextvars
import json
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_LOCALES_DIR = Path(__file__).parent / "locales"
_DEFAULT_LOCALE = "en"
_SUPPORTED_LOCALES = ("en", "pl")

# Loaded translation catalogs: {locale: {key: value}}
_catalogs: dict[str, dict[str, str]] = {}

# Context-var storage for per-request locale (async-safe replacement for threading.local)
_locale_var: contextvars.ContextVar[str] = contextvars.ContextVar("locale", default=_DEFAULT_LOCALE)


def _load_catalog(locale: str) -> dict[str, str]:
    """Load a locale catalog from its JSON file."""
    path = _LOCALES_DIR / f"{locale}.json"
    if not path.exists():
        _log.warning("Locale catalog not found: %s", path)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.error("Failed to load locale catalog %s: %s", locale, exc)
        return {}


def get_catalog(locale: str) -> dict[str, str]:
    """Get (or lazily load) the catalog for a locale."""
    if locale not in _catalogs:
        _catalogs[locale] = _load_catalog(locale)
    return _catalogs[locale]


def reload_catalogs() -> None:
    """Force-reload all catalogs from disk (useful for tests)."""
    _catalogs.clear()


def supported_locales() -> tuple[str, ...]:
    """Return the tuple of supported locale codes."""
    return _SUPPORTED_LOCALES


def default_locale() -> str:
    """Return the default locale code."""
    return _DEFAULT_LOCALE


def set_locale(locale: str) -> None:
    """Set the active locale for the current context (async-safe)."""
    _locale_var.set(locale if locale in _SUPPORTED_LOCALES else _DEFAULT_LOCALE)


def get_locale() -> str:
    """Get the active locale for the current context (async-safe)."""
    return _locale_var.get()


def t(key: str, locale: str | None = None, count: int | None = None, **kwargs: Any) -> str:
    """Translate a key to the given (or current) locale.

    Falls back to the default locale (English) if the key is missing.
    Supports interpolation: t("greeting", name="World") → "Hello, World!"
    Supports pluralization: t("items", count=5) looks up items.one/items.few/items.other.

    Args:
        key: Dot-separated translation key (e.g. "status.ready").
        locale: Override locale. If None, uses the thread-local locale.
        count: If provided, select plural form via _plural_key().
        **kwargs: Interpolation variables for str.format().

    Returns:
        Translated string, or the key itself if not found in any catalog.
    """
    loc = locale or get_locale()

    if count is not None:
        plural_suffix = _plural_key(loc, count)
        plural_key = f"{key}.{plural_suffix}"
        result = _lookup(plural_key, loc)
        if result is not None:
            kwargs.setdefault("count", count)
            if kwargs:
                try:
                    return result.format(**kwargs)
                except (KeyError, IndexError):
                    return result
            return result

    value = _lookup(key, loc)
    if value is None:
        return key

    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError):
            return value

    return value


def _lookup(key: str, locale: str) -> str | None:
    """Look up a key in the given locale catalog, falling back to default."""
    catalog = get_catalog(locale)
    value = catalog.get(key)
    if value is None and locale != _DEFAULT_LOCALE:
        value = get_catalog(_DEFAULT_LOCALE).get(key)
    return value


def _plural_key(locale: str, count: int) -> str:
    """Return the plural form suffix for a locale and count.

    English: one (1), other (everything else)
    Polish: one (1), few (2-4 except 12-14), other (rest)
    """
    if locale == "pl":
        if count == 1:
            return "one"
        last_two = count % 100
        last_one = count % 10
        if 2 <= last_one <= 4 and not (12 <= last_two <= 14):
            return "few"
        return "other"
    # Default (English) plural rules
    return "one" if count == 1 else "other"


def detect_locale_from_header(accept_language: str | None) -> str:
    """Parse an Accept-Language header and return the best supported locale.

    Handles formats like:
        pl,en-US;q=0.9,en;q=0.8
        en-GB,en;q=0.9,pl;q=0.8

    Returns the default locale if no match found.
    """
    if not accept_language:
        return _DEFAULT_LOCALE

    # Parse quality-weighted language tags
    candidates: list[tuple[float, str]] = []
    for part in accept_language.split(","):
        part = part.strip()
        if not part:
            continue
        if ";q=" in part:
            tag, q_str = part.split(";q=", 1)
            try:
                q = float(q_str.strip())
            except ValueError:
                q = 0.0
        else:
            tag = part
            q = 1.0
        candidates.append((q, tag.strip().lower()))

    # Sort by quality descending
    candidates.sort(key=lambda x: -x[0])

    for _, tag in candidates:
        # Exact match
        if tag in _SUPPORTED_LOCALES:
            return tag
        # Language prefix match (e.g. "pl-PL" → "pl")
        lang = tag.split("-")[0]
        if lang in _SUPPORTED_LOCALES:
            return lang

    return _DEFAULT_LOCALE

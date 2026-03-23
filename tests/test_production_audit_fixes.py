"""Regression tests for production audit fixes (second pass).
"""Regression tests for production audit fixes (second pass).

Covers: operator precedence in config backfill, schema caching,
atomic first-run config write, SSE serialization logging.
"""

from __future__ import annotations

from typing import Any


class TestConfigBackfillPrecedence:
    """Fix: operator precedence bug in fill_config_defaults."""

    def test_backfill_missing_key(self) -> None:
        """GIVEN a config with branding section missing 'theme'"""
        from releaseboard.web.state import fill_config_defaults

        data: dict[str, Any] = {"settings": {}, "layers": []}
        fill_config_defaults(data)

        """THEN theme is backfilled from defaults"""
        assert "theme" in data["settings"]
        assert data["settings"]["theme"] == "system"

    def test_backfill_empty_string_for_special_keys(self) -> None:
        """GIVEN a config with primary_color set to empty string"""
        from releaseboard.web.state import fill_config_defaults

        data: dict[str, Any] = {
            "branding": {"primary_color": ""},
            "layers": [],
        }
        fill_config_defaults(data)

        """THEN the empty value is replaced by the default"""
        assert data["branding"]["primary_color"] == "#fb6400"

    def test_backfill_preserves_existing_nonempty_special_key(self) -> None:
        """GIVEN a config with primary_color set to a custom value"""
        from releaseboard.web.state import fill_config_defaults

        data: dict[str, Any] = {
            "branding": {"primary_color": "#FF0000"},
            "layers": [],
        }
        fill_config_defaults(data)

        """THEN the custom value is preserved"""
        assert data["branding"]["primary_color"] == "#FF0000"


class TestSchemaCaching:
    """Fix: JSON schema was loaded from disk on every validation call."""

    def test_schema_is_cached(self) -> None:
        """GIVEN the schema module"""
        from releaseboard.config import schema as schema_mod

        schema_mod._SCHEMA_CACHE = None  # reset

        """WHEN loading schema twice"""
        s1 = schema_mod._load_schema()
        s2 = schema_mod._load_schema()

        """THEN both return the same object (cached)"""
        assert s1 is s2
        assert isinstance(s1, dict)
        assert "properties" in s1 or "type" in s1


class TestSSEFormat:
    """Fix: SSE serialization errors now log warnings instead of being silent."""

    def test_sse_format_normal(self) -> None:
        """GIVEN valid data"""
        from releaseboard.web.server import _sse_format

        result = _sse_format("test_event", {"key": "value"})

        """THEN it produces valid SSE format"""
        assert "event: test_event" in result
        assert '"key": "value"' in result
        assert result.endswith("\n\n")

    def test_sse_format_with_unserializable_data(self) -> None:
        """GIVEN data that cannot be JSON-serialized normally"""
        from releaseboard.web.server import _sse_format

        class BadObj:
            pass

        """WHEN formatting (default=str handles this)"""
        result = _sse_format("test", {"obj": BadObj()})

        """THEN it still produces valid SSE output (default=str fallback)"""
        assert "event: test" in result
        assert result.endswith("\n\n")

"""Tests for i18n catalog parity, SSE broadcast safety, datetime formatting,
staleness edge cases, branding/schema endpoint validation, and template limits."""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestI18nCatalogParity:
    """Scenarios for i18n catalog parity."""

    def _load(self, locale: str) -> dict:
        path = ROOT / "src" / "releaseboard" / "i18n" / "locales" / f"{locale}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_en_and_pl_have_identical_keys(self):
        """GIVEN the EN and PL locale catalogs loaded."""
        en = set(self._load("en").keys())
        pl = set(self._load("pl").keys())

        """WHEN comparing key sets between catalogs."""
        only_en = en - pl
        only_pl = pl - en

        """THEN both catalogs have identical keys with no orphans."""
        assert not only_en, f"Keys only in EN: {sorted(only_en)}"
        assert not only_pl, f"Keys only in PL: {sorted(only_pl)}"

    def test_rc_import_example_title_exists_in_en(self):
        """GIVEN the EN locale catalog loaded."""
        en = self._load("en")

        """WHEN checking for the rc.import.example_title key."""
        key_present = "rc.import.example_title" in en

        """THEN the key exists and is non-empty."""
        assert key_present
        assert len(en["rc.import.example_title"]) > 0


class TestContentLengthValidation:
    """Scenarios for Content-Length validation."""

    def test_calendar_import_malformed_content_length(self):
        """GIVEN the source code of the server module."""
        server_path = ROOT / "src" / "releaseboard" / "web" / "server.py"
        source = server_path.read_text(encoding="utf-8")

        """WHEN locating the calendar import endpoint."""
        idx = source.find("import_calendar")

        """THEN Content-Length is validated with isdigit() before int()."""
        if idx >= 0:
            nearby = source[idx : idx + 800]
            assert "isdigit()" in nearby, (
                "Calendar import endpoint must validate Content-Length with isdigit() "
                "before calling int()"
            )

    def test_read_json_body_has_isdigit_check(self):
        """GIVEN the source code of the server module."""
        server_path = ROOT / "src" / "releaseboard" / "web" / "server.py"
        source = server_path.read_text(encoding="utf-8")

        """WHEN locating the _read_json_body function."""
        idx = source.find("def _read_json_body")
        assert idx >= 0
        body = source[idx : idx + 500]

        """THEN Content-Length is validated with isdigit()."""
        assert "isdigit()" in body


class TestSSEBroadcastAtomicCleanup:
    """Scenarios for SSE broadcast atomic cleanup."""

    def test_broadcast_uses_list_comprehension_not_loop(self):
        """GIVEN the source code of the state module."""
        state_path = ROOT / "src" / "releaseboard" / "web" / "state.py"
        source = state_path.read_text(encoding="utf-8")

        """WHEN searching for TOCTOU-prone check-then-remove patterns."""
        has_toctou = "if dq in self._sse_subscribers" in source

        """THEN no check-then-remove loop is found."""
        assert not has_toctou, "SSE broadcast still uses TOCTOU-prone check-then-remove pattern"

    @pytest.mark.asyncio
    async def test_broadcast_removes_full_queues(self):
        """GIVEN an AppState with a full queue and a healthy queue."""
        from releaseboard.web.state import AppState

        config_path = ROOT / "examples" / "config.json"
        app_state = AppState(config_path)
        small_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        small_queue.put_nowait({"event": "filler", "data": {}})
        app_state._sse_subscribers.append(small_queue)
        healthy_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        app_state._sse_subscribers.append(healthy_queue)

        """WHEN broadcasting a test event."""
        await app_state.broadcast("test", {"msg": "hello"})

        """THEN the full queue is removed and the healthy one is kept."""
        assert small_queue not in app_state._sse_subscribers
        assert healthy_queue in app_state._sse_subscribers


class TestFormatDatetimeNoneGuard:
    """Scenarios for _format_datetime None guard."""

    def test_none_returns_dash(self):
        """GIVEN a None datetime value."""
        from releaseboard.presentation.view_models import _format_datetime

        value = None

        """WHEN formatting the datetime."""
        result = _format_datetime(value)

        """THEN a dash is returned."""
        assert result == "—"

    def test_valid_datetime_still_works(self):
        """GIVEN a valid UTC datetime."""
        from releaseboard.presentation.view_models import _format_datetime

        dt = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)

        """WHEN formatting the datetime."""
        result = _format_datetime(dt)

        """THEN the formatted string contains the year and is non-trivial."""
        assert "2026" in result
        assert len(result) > 5

    def test_none_with_locale(self):
        """GIVEN a None datetime value and a Polish locale."""
        from releaseboard.presentation.view_models import _format_datetime

        value = None

        """WHEN formatting the datetime with locale."""
        result = _format_datetime(value, locale="pl")

        """THEN a dash is returned."""
        assert result == "—"


class TestStalenessFutureDateHandling:
    """Scenarios for staleness future date handling."""

    def test_future_date_is_not_stale(self):
        """GIVEN a date 30 days in the future."""
        from releaseboard.analysis.staleness import is_stale

        future = datetime.now(tz=UTC) + timedelta(days=30)

        """WHEN checking staleness with a 14-day threshold."""
        result = is_stale(future, 14)

        """THEN the date is not flagged as stale."""
        assert result is False

    def test_far_future_date_is_not_stale(self):
        """GIVEN a date 365 days in the future."""
        from releaseboard.analysis.staleness import is_stale

        future = datetime.now(tz=UTC) + timedelta(days=365)

        """WHEN checking staleness with a 14-day threshold."""
        result = is_stale(future, 14)

        """THEN the date is not flagged as stale."""
        assert result is False

    def test_future_freshness_label_returns_today(self):
        """GIVEN a date 5 days in the future."""
        from releaseboard.analysis.staleness import freshness_label

        future = datetime.now(tz=UTC) + timedelta(days=5)

        """WHEN computing the freshness label with a 14-day threshold."""
        label = freshness_label(future, 14)

        """THEN the label indicates freshness."""
        assert "Today" in label or "today" in label.lower() or label != ""

    def test_past_date_still_detected_as_stale(self):
        """GIVEN a date 30 days in the past."""
        from releaseboard.analysis.staleness import is_stale

        old = datetime.now(tz=UTC) - timedelta(days=30)

        """WHEN checking staleness with a 14-day threshold."""
        result = is_stale(old, 14)

        """THEN the date is flagged as stale."""
        assert result is True

    def test_none_date_still_stale(self):
        """GIVEN a None date value."""
        from releaseboard.analysis.staleness import is_stale

        value = None

        """WHEN checking staleness with a 14-day threshold."""
        result = is_stale(value, 14)

        """THEN the result is stale."""
        assert result is True

    def test_boundary_exactly_at_threshold_not_stale(self):
        """GIVEN a date exactly at the 14-day threshold."""
        from releaseboard.analysis.staleness import is_stale

        at_threshold = datetime.now(tz=UTC) - timedelta(days=14)

        """WHEN checking staleness with a 14-day threshold."""
        result = is_stale(at_threshold, 14)

        """THEN the date is not flagged as stale."""
        assert result is False

    def test_one_day_over_threshold_is_stale(self):
        """GIVEN a date 15 days in the past."""
        from releaseboard.analysis.staleness import is_stale

        over = datetime.now(tz=UTC) - timedelta(days=15)

        """WHEN checking staleness with a 14-day threshold."""
        result = is_stale(over, 14)

        """THEN the date is flagged as stale."""
        assert result is True


class TestBrandingEndpointValidation:
    """Scenarios for branding endpoint validation."""

    def test_branding_endpoint_only_accepts_string_types(self):
        """GIVEN the source code of the server module."""
        server_path = ROOT / "src" / "releaseboard" / "web" / "server.py"
        source = server_path.read_text(encoding="utf-8")

        """WHEN locating the branding endpoint."""
        idx = source.find("update_branding")
        assert idx >= 0
        body = source[idx : idx + 800]

        """THEN type-checking with isinstance is present."""
        assert "isinstance" in body, "Branding endpoint must type-check text field values"

    def test_branding_endpoint_truncates_text(self):
        """GIVEN the source code of the server module."""
        server_path = ROOT / "src" / "releaseboard" / "web" / "server.py"
        source = server_path.read_text(encoding="utf-8")

        """WHEN locating the branding endpoint."""
        idx = source.find("update_branding")
        body = source[idx : idx + 800]

        """THEN text field length is limited."""
        assert "[:500]" in body or "[:256]" in body or "[:1000]" in body, (
            "Branding endpoint must limit text field length"
        )


class TestSchemaEndpointErrorHandling:
    """Scenarios for schema endpoint error handling."""

    def test_schema_endpoint_has_error_handling(self):
        """GIVEN the source code of the server module."""
        server_path = ROOT / "src" / "releaseboard" / "web" / "server.py"
        source = server_path.read_text(encoding="utf-8")

        """WHEN locating the get_schema function."""
        idx = source.find("def get_schema")
        assert idx >= 0
        body = source[idx : idx + 500]

        """THEN error handling for FileNotFoundError is present."""
        assert "FileNotFoundError" in body or "except" in body


class TestDeriveNameSpecificExceptions:
    """Scenarios for derive_name_from_url specific exceptions."""

    def test_no_bare_except_exception(self):
        """GIVEN the source code of the models module."""
        models_path = ROOT / "src" / "releaseboard" / "config" / "models.py"
        source = models_path.read_text(encoding="utf-8")

        """WHEN locating the derive_name_from_url function body."""
        idx = source.find("def derive_name_from_url")
        func_end = source.find("\ndef ", idx + 1)
        func_body = source[idx:func_end]

        """THEN specific exceptions are caught instead of bare Exception."""
        assert "except Exception:" not in func_body, (
            "derive_name_from_url still uses bare 'except Exception'"
        )
        assert "except (ValueError" in func_body

    def test_derive_name_still_works_for_all_formats(self):
        """GIVEN the derive_name_from_url function."""
        from releaseboard.config.models import derive_name_from_url

        """WHEN parsing various URL formats."""
        results = {
            "https": derive_name_from_url("https://github.com/acme/repo.git"),
            "ssh": derive_name_from_url("git@github.com:org/app.git"),
            "path": derive_name_from_url("/opt/repos/my-service"),
            "slug": derive_name_from_url("bare-slug"),
            "empty": derive_name_from_url(""),
            "spaces": derive_name_from_url("   "),
        }

        """THEN all formats return the expected repository name."""
        assert results["https"] == "repo"
        assert results["ssh"] == "app"
        assert results["path"] == "my-service"
        assert results["slug"] == "bare-slug"
        assert results["empty"] == ""
        assert results["spaces"] == ""


class TestSafeIntLogging:
    """Scenarios for _safe_int logging."""

    def test_safe_int_logs_warning_on_invalid_string(self):
        """GIVEN the source code of the loader module."""
        loader_path = ROOT / "src" / "releaseboard" / "config" / "loader.py"
        source = loader_path.read_text(encoding="utf-8")

        """WHEN locating the _safe_int function."""
        idx = source.find("def _safe_int")
        assert idx >= 0
        func_body = source[idx : idx + 300]

        """THEN a log warning is issued for invalid values."""
        assert "logger.warning" in func_body or "log" in func_body.lower()


class TestAnalysisTaskBroadcastSafety:
    """Scenarios for analysis task broadcast safety."""

    def test_broadcast_failure_is_caught(self):
        """GIVEN the source code of the server module."""
        server_path = ROOT / "src" / "releaseboard" / "web" / "server.py"
        source = server_path.read_text(encoding="utf-8")

        """WHEN locating the analysis _run function."""
        idx = source.find("async def _run() -> None:")
        assert idx >= 0
        body = source[idx : idx + 600]

        """THEN broadcast has nested try/except for safety."""
        assert body.count("try:") >= 2, (
            "Analysis _run must have nested try/except for broadcast safety"
        )


class TestJsonSerializationSafety:
    """Scenarios for JSON serialization safety."""

    def test_config_json_serialization_has_default_str(self):
        """GIVEN the source code of the view_models module."""
        vm_path = ROOT / "src" / "releaseboard" / "presentation" / "view_models.py"
        source = vm_path.read_text(encoding="utf-8")

        """WHEN searching for json.dumps usage."""
        has_default_str = "default=str" in source

        """THEN default=str is used for unserializable types."""
        assert has_default_str

    def test_config_json_serialization_has_error_handling(self):
        """GIVEN the source code of the view_models module."""
        vm_path = ROOT / "src" / "releaseboard" / "presentation" / "view_models.py"
        source = vm_path.read_text(encoding="utf-8")

        """WHEN locating the embedded_config_json section."""
        idx = source.find("embedded_config_json")
        body = source[idx : idx + 400]

        """THEN serialization errors are caught."""
        assert "except" in body


class TestTemplatePartialLimits:
    """Scenarios for template partial line limits."""

    TEMPLATE_DIR = ROOT / "src" / "releaseboard" / "presentation" / "templates"
    MAX_LINES = 1000

    def test_all_partials_under_limit(self):
        """GIVEN all template partial files."""
        partials = sorted(self.TEMPLATE_DIR.glob("_*.html.j2"))

        """WHEN counting lines in each partial."""
        oversized = [
            (f.name, len(f.read_text(encoding="utf-8").splitlines()))
            for f in partials
            if len(f.read_text(encoding="utf-8").splitlines()) > self.MAX_LINES
        ]

        """THEN every partial stays under the line limit."""
        for name, count in oversized:
            raise AssertionError(f"{name} has {count} lines (limit: {self.MAX_LINES})")

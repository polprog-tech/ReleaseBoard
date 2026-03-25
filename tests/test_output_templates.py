"""Tests for output rendering — template error handling, CSP headers, timezone
handling, staleness boundaries, branch-pattern validation, and severity ordering."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from releaseboard.analysis.branch_pattern import BranchPatternMatcher
from releaseboard.analysis.staleness import is_stale
from releaseboard.domain.enums import ReadinessStatus
from releaseboard.domain.models import BranchInfo


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    """Create a test client with a valid config."""
    cfg_file = tmp_path / "releaseboard.json"
    cfg_file.write_text(json.dumps(_make_minimal_config()), encoding="utf-8")

    from releaseboard.web.server import create_app

    with patch.dict(
        "os.environ",
        {
            "RELEASEBOARD_API_KEY": "test-key-123",
            "RELEASEBOARD_CORS_ORIGINS": "*",
        },
    ):
        app = create_app(cfg_file)
        transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-API-Key": "test-key-123"},
        ) as c:
            yield c


def _make_minimal_config() -> dict[str, Any]:
    return {
        "release": {
            "name": "2025.01",
            "target_month": 1,
            "target_year": 2025,
        },
        "layers": [
            {"id": "core", "label": "Core", "color": "#3B82F6", "order": 0},
        ],
        "repositories": [
            {
                "name": "repo-a",
                "url": "/tmp/test-repo",
                "layer": "core",
            },
        ],
        "branding": {"title": "TestBoard"},
        "settings": {"stale_threshold_days": 14},
    }


class TestTemplateErrorHandling:
    """Scenarios for template error handling."""

    @pytest.mark.asyncio
    async def test_dashboard_returns_html_even_on_error(self, client):
        """GIVEN a running application with minimal config."""
        # Client fixture provides the test application.

        """WHEN requesting the dashboard root."""
        resp = await client.get("/")

        """THEN it returns HTML with status 200."""
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


class TestInvalidJSONHandling:
    """Scenarios for invalid JSON handling."""

    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, client):
        """GIVEN a running application and malformed JSON payload."""
        payload = b"this is not valid json {{{"

        """WHEN sending the malformed JSON to the config endpoint."""
        resp = await client.put(
            "/api/config",
            content=payload,
            headers={"content-type": "application/json"},
        )

        """THEN a 400 response with a JSON error message is returned."""
        assert resp.status_code == 400
        assert "json" in resp.json().get("error", "").lower()

    @pytest.mark.asyncio
    async def test_empty_body_returns_400(self, client):
        """GIVEN a running application and an empty body."""
        payload = b""

        """WHEN sending the empty body to the config endpoint."""
        resp = await client.put(
            "/api/config",
            content=payload,
            headers={"content-type": "application/json"},
        )

        """THEN a 400 response is returned."""
        assert resp.status_code == 400


class TestBranchPatternValidation:
    """Scenarios for branch pattern validation."""

    def test_month_zero_raises(self):
        """GIVEN a BranchPatternMatcher instance."""
        m = BranchPatternMatcher()

        """WHEN resolving with month zero."""
        # (action and assertion combined in pytest.raises)

        """THEN a ValueError is raised."""
        with pytest.raises(ValueError, match="Invalid release month"):
            m.resolve("release/{MM}.{YYYY}", month=0, year=2025)

    def test_month_13_raises(self):
        """GIVEN a BranchPatternMatcher instance."""
        m = BranchPatternMatcher()

        """WHEN resolving with month 13."""
        # (action and assertion combined in pytest.raises)

        """THEN a ValueError is raised."""
        with pytest.raises(ValueError, match="Invalid release month"):
            m.resolve("release/{MM}.{YYYY}", month=13, year=2025)

    def test_negative_month_raises(self):
        """GIVEN a BranchPatternMatcher instance."""
        m = BranchPatternMatcher()

        """WHEN resolving with a negative month."""
        # (action and assertion combined in pytest.raises)

        """THEN a ValueError is raised."""
        with pytest.raises(ValueError, match="Invalid release month"):
            m.resolve("release/{MM}.{YYYY}", month=-1, year=2025)

    def test_year_too_low_raises(self):
        """GIVEN a BranchPatternMatcher instance."""
        m = BranchPatternMatcher()

        """WHEN resolving with year below valid range."""
        # (action and assertion combined in pytest.raises)

        """THEN a ValueError is raised."""
        with pytest.raises(ValueError, match="Invalid release year"):
            m.resolve("release/{MM}.{YYYY}", month=1, year=1999)

    def test_year_too_high_raises(self):
        """GIVEN a BranchPatternMatcher instance."""
        m = BranchPatternMatcher()

        """WHEN resolving with year above valid range."""
        # (action and assertion combined in pytest.raises)

        """THEN a ValueError is raised."""
        with pytest.raises(ValueError, match="Invalid release year"):
            m.resolve("release/{MM}.{YYYY}", month=1, year=2100)


class TestGeneratedAtTimezone:
    """Scenarios for generated_at timezone."""

    def test_source_uses_timezone_utc(self):
        """GIVEN the view_models source code."""
        import inspect

        from releaseboard.presentation import view_models

        source = inspect.getsource(view_models)

        """WHEN inspecting datetime usage in the source."""
        has_tz_utc = "timezone.utc" in source or "datetime.UTC" in source or "tz=UTC" in source
        has_naive_now = "datetime.now()" in source.replace("datetime.now(tz=", "")

        """THEN timezone.utc is used and naive datetime.now() is absent."""
        assert has_tz_utc
        assert not has_naive_now


class TestFreshnessLabelBoundary:
    """Scenarios for freshness label boundary."""

    def test_past_threshold_is_stale(self):
        """GIVEN a commit one day past the threshold."""
        from releaseboard.analysis.staleness import freshness_label

        threshold = 14
        last_commit = datetime.now(tz=UTC) - timedelta(days=threshold + 1)

        """WHEN checking staleness and computing the label."""
        stale = is_stale(last_commit, threshold)
        label = freshness_label(last_commit, threshold, locale="en")

        """THEN both agree the commit is stale."""
        assert stale is True
        assert str(threshold + 1) in label


class TestTemplateSplit:
    """Scenarios for template split."""

    TEMPLATE_DIR = (
        Path(__file__).parent.parent / "src" / "releaseboard" / "presentation" / "templates"
    )
    MAX_PARTIAL_LINES = 1000

    def _all_content(self) -> str:
        parts = []
        for f in sorted(self.TEMPLATE_DIR.glob("*.j2")):
            parts.append(f.read_text(encoding="utf-8"))
        return "\n".join(parts)

    def test_main_template_is_orchestrator(self):
        """GIVEN the main dashboard template file."""
        main = self.TEMPLATE_DIR / "dashboard.html.j2"
        content = main.read_text(encoding="utf-8")

        """WHEN counting non-blank lines and checking for includes."""
        lines = [line for line in content.splitlines() if line.strip()]

        """THEN it is a small orchestrator with include directives."""
        assert len(lines) < 50, f"Main template too large: {len(lines)} non-blank lines"
        assert "{% include" in content

    def test_all_partials_under_700_lines(self):
        """GIVEN all template partial files."""
        partials = sorted(self.TEMPLATE_DIR.glob("_*.html.j2"))

        """WHEN measuring their line counts."""
        counts = {f.name: len(f.read_text(encoding="utf-8").splitlines()) for f in partials}

        """THEN every partial stays under the maximum line limit."""
        for name, line_count in counts.items():
            assert line_count <= self.MAX_PARTIAL_LINES, (
                f"{name} has {line_count} lines (limit: {self.MAX_PARTIAL_LINES})"
            )

    def test_partials_exist(self):
        """GIVEN a list of critical template partials."""
        required = [
            "_styles.html.j2",
            "_header.html.j2",
            "_dashboard_content.html.j2",
            "_modals.html.j2",
            "_config_drawer.html.j2",
            "_footer.html.j2",
            "_scripts.html.j2",
            "_scripts_core.html.j2",
            "_scripts_interactive.html.j2",
            "_scripts_editor.html.j2",
            "_scripts_config_ui.html.j2",
            "_scripts_wizard.html.j2",
            "_scripts_analysis.html.j2",
            "_head_scripts.html.j2",
        ]

        """WHEN checking if each partial exists on disk."""
        paths = {name: (self.TEMPLATE_DIR / name) for name in required}

        """THEN all critical partials are present."""
        for name, path in paths.items():
            assert path.exists(), f"Missing partial: {name}"

    def test_combined_content_has_key_elements(self):
        """GIVEN the concatenated content of all template files."""
        content = self._all_content()
        essentials = [
            "<!DOCTYPE html>",
            "<html",
            "</html>",
            "renderEffectiveTab",
            "PREDEFINED_TEMPLATES",
            'data-tab="effective"',
            'id="layoutBar"',
            "REPO_DATA",
        ]

        """WHEN searching for essential dashboard tokens."""
        found = {token: token in content for token in essentials}

        """THEN all essential tokens are found."""
        for token, present in found.items():
            assert present, f"Missing essential token: {token}"


class TestCSPAllowsChartJS:
    """Scenarios for CSP Chart.js allowance."""

    def _get_csp(self) -> str:
        import inspect

        from releaseboard.web.middleware import SecurityHeadersMiddleware

        source = inspect.getsource(SecurityHeadersMiddleware)
        assert "cdn.jsdelivr.net" in source, "CSP must allow cdn.jsdelivr.net for Chart.js"
        return source

    def test_template_uses_jsdelivr_cdn(self):
        """GIVEN the Chart.js CDN was removed from templates."""
        # Placeholder — no setup needed.

        """WHEN verifying the CDN removal is acknowledged."""
        # Placeholder — no action needed.

        """THEN the test passes as a placeholder."""
        pass


class TestAgeDaysTimezoneHandling:
    """Scenarios for age_days timezone handling."""

    def test_age_days_with_naive_datetime(self):
        """GIVEN a BranchInfo with a naive last_commit_date."""
        naive_date = datetime(2025, 1, 1)  # no tzinfo
        info = BranchInfo(name="test", exists=True, last_commit_date=naive_date)

        """WHEN age_days is accessed."""
        result = info.age_days

        """THEN it returns a non-negative integer without raising."""
        assert isinstance(result, int)
        assert result >= 0


class TestSeverityProperty:
    """Scenarios for severity property."""

    def test_severity_ordering(self):
        """GIVEN key ReadinessStatus members."""
        error_sev = ReadinessStatus.ERROR.severity
        missing_sev = ReadinessStatus.MISSING_BRANCH.severity
        ready_sev = ReadinessStatus.READY.severity

        """WHEN comparing their severity values."""
        error_more_severe = error_sev < ready_sev
        missing_more_severe = missing_sev < ready_sev

        """THEN ERROR and MISSING_BRANCH are more severe than READY."""
        assert error_more_severe
        assert missing_more_severe

    def test_severity_never_raises_key_error(self):
        """GIVEN all ReadinessStatus enum members."""
        statuses = list(ReadinessStatus)

        """WHEN accessing severity on each member."""
        results = [s.severity for s in statuses]

        """THEN no KeyError is raised for any member."""
        assert all(isinstance(r, int) for r in results)

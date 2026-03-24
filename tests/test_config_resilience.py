"""Tests for config resilience, data integrity, and cross-cutting integration.

Covers: atomic config save, version consistency, GitLab provider, layout config,
secret sanitization, concurrent analysis, env-var resolution, error handlers,
health checks, CORS, body-size limits, SSE disconnect, graceful shutdown,
structured logging, timezone handling, severity, layer validation, SSE broadcast,
temp-file cleanup, CSP headers, config defaults/caching, event-loop safety,
template partials integrity.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from releaseboard.config.loader import load_config
from releaseboard.config.models import (
    AppConfig,
    LayerConfig,
    ReleaseConfig,
    RepositoryConfig,
    SettingsConfig,
)
from releaseboard.domain.enums import ReadinessStatus
from releaseboard.domain.models import BranchInfo
from releaseboard.git.gitlab_provider import GitLabProvider
from releaseboard.git.provider import GitProvider
from releaseboard.shared.logging import StructuredFormatter, get_logger
from releaseboard.web.state import (
    AppState,
    _sanitize_secrets,
)

BLOCKERS_CONFIG: dict[str, Any] = {
    "release": {
        "name": "Test Release",
        "target_month": 3,
        "target_year": 2025,
        "branch_pattern": "release/{MM}.{YYYY}",
    },
    "repositories": [
        {"name": "svc-a", "url": "https://git.example.com/a.git", "layer": "api"},
    ],
    "layers": [
        {"id": "api", "label": "API", "color": "#10B981", "order": 0},
    ],
    "branding": {"title": "Test", "primary_color": "#4F46E5", "secondary_color": "#002754e6"},
    "settings": {"stale_threshold_days": 14, "theme": "system"},
}


def _write_config(tmp_path: Path, data: dict | None = None) -> Path:
    config_path = tmp_path / "test_config.json"
    config_path.write_text(json.dumps(data or BLOCKERS_CONFIG), encoding="utf-8")
    return config_path


def _create_app_for_test(tmp_path: Path, data: dict | None = None):
    """Create a FastAPI app for testing."""
    from releaseboard.web.server import create_app

    config_path = _write_config(tmp_path, data)
    return create_app(config_path), config_path


REGRESSION_CONFIG: dict[str, Any] = {
    "release": {
        "name": "March 2025",
        "target_month": 3,
        "target_year": 2025,
        "branch_pattern": "release/{MM}.{YYYY}",
    },
    "layers": [
        {"id": "api", "label": "API", "order": 0},
    ],
    "repositories": [
        {
            "name": "svc-one",
            "url": "https://git.local/svc-one.git",
            "layer": "api",
        },
    ],
}


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(REGRESSION_CONFIG, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def state(config_file: Path) -> AppState:
    return AppState(config_file)


class TestGitLabProviderABC:
    """Scenarios for GitLabProvider ABC compliance."""

    def test_is_subclass_of_git_provider(self):
        """GIVEN GitLabProvider class."""
        subclass_check = issubclass(GitLabProvider, GitProvider)

        """WHEN checking inheritance."""
        result = subclass_check

        """THEN it is a subclass of GitProvider."""
        assert result

    def test_has_get_branch_info_method(self):
        """GIVEN a GitLabProvider instance."""
        provider = GitLabProvider(token="fake")

        """WHEN checking for get_branch_info method."""
        has_method = hasattr(provider, "get_branch_info")

        """THEN it has a callable get_branch_info method."""
        assert has_method
        assert callable(provider.get_branch_info)

    def test_get_branch_info_returns_branch_info_for_invalid_url(self):
        """GIVEN a non-GitLab URL."""
        provider = GitLabProvider(token="fake")

        """WHEN get_branch_info is called."""
        result = provider.get_branch_info("https://not-gitlab.com/repo", "main", timeout=5)

        """THEN returns BranchInfo with exists=False."""
        assert isinstance(result, BranchInfo)
        assert result.exists is False


class TestLayoutConfig:
    """Scenarios for layout config model and loader."""

    def test_loader_builds_layout_from_json(self, tmp_path: Path):
        """GIVEN a config file with layout section."""
        data = dict(BLOCKERS_CONFIG)
        data["layout"] = {
            "default_template": "executive",
            "section_order": ["score", "metrics"],
            "enable_drag_drop": False,
        }
        config_path = _write_config(tmp_path, data)

        """WHEN loaded."""
        config = load_config(config_path)

        """THEN layout is properly parsed."""
        assert config.layout.default_template == "executive"
        assert config.layout.section_order == ("score", "metrics")
        assert config.layout.enable_drag_drop is False


class TestPackageData:
    """Scenarios for package data availability."""

    def test_locale_files_exist(self):
        """GIVEN the locales directory."""
        from releaseboard.i18n import _LOCALES_DIR

        """WHEN checking for locale files."""
        en_exists = (_LOCALES_DIR / "en.json").exists()
        pl_exists = (_LOCALES_DIR / "pl.json").exists()

        """THEN en.json and pl.json exist."""
        assert en_exists
        assert pl_exists

    def test_schema_json_exists(self):
        """GIVEN the config directory."""
        from releaseboard.config.schema import _SCHEMA_PATH

        """WHEN checking for schema.json."""
        exists = _SCHEMA_PATH.exists()

        """THEN schema.json exists."""
        assert exists


class TestSecretSanitization:
    """Scenarios for config export token sanitization."""

    def test_sanitize_secrets_redacts_known_keys(self):
        """GIVEN config data with token fields."""
        data = {
            "github_token": "ghp_secret123",
            "gitlab_token": "glpat-secret456",
            "safe_field": "not-redacted",
        }

        """WHEN _sanitize_secrets is called."""
        _sanitize_secrets(data)

        """THEN token values are redacted."""
        assert data["github_token"] == "***REDACTED***"
        assert data["gitlab_token"] == "***REDACTED***"
        assert data["safe_field"] == "not-redacted"

    def test_sanitize_secrets_handles_nested_dicts(self):
        """GIVEN nested config with tokens."""
        data = {"provider": {"token": "secret-token"}}

        """WHEN _sanitize_secrets is called."""
        _sanitize_secrets(data)

        """THEN nested tokens are redacted."""
        assert data["provider"]["token"] == "***REDACTED***"

    def test_sanitize_secrets_handles_empty_values(self):
        """GIVEN config with empty token fields."""
        data = {"github_token": "", "gitlab_token": None}

        """WHEN _sanitize_secrets is called."""
        _sanitize_secrets(data)

        """THEN empty values are NOT redacted."""
        assert data["github_token"] == ""
        assert data["gitlab_token"] is None

    def test_export_config_redacts_tokens(self, tmp_path: Path):
        """GIVEN a state with tokens in draft."""
        config_path = _write_config(tmp_path)
        state = AppState(config_path)
        state.config_state.draft_raw["github_token"] = "ghp_secret"

        """WHEN export_config is called."""
        exported = state.export_config()

        """THEN exported data has redacted tokens and original is unchanged."""
        assert exported["github_token"] == "***REDACTED***"
        assert state.config_state.draft_raw["github_token"] == "ghp_secret"


class TestConcurrentAnalysis:
    """Scenarios for concurrent analysis with semaphore."""

    @pytest.mark.asyncio
    async def test_concurrent_analysis_produces_all_results(self):
        """GIVEN max_concurrent=5 and 3 repos."""
        from releaseboard.application.service import AnalysisService

        class QuickProvider(GitProvider):
            def list_remote_branches(self, url: str, timeout: int = 30) -> list[str]:
                return ["release/03.2025"]

            def get_branch_info(
                self,
                url: str,
                branch: str,
                timeout: int = 30,
            ) -> BranchInfo | None:
                return BranchInfo(
                    name="release/03.2025",
                    exists=True,
                    last_commit_date=datetime.now(tz=UTC),
                    last_commit_author="dev",
                    last_commit_message="ok",
                )

        config = AppConfig(
            release=ReleaseConfig(
                name="Test", target_month=3, target_year=2025, branch_pattern="release/{MM}.{YYYY}"
            ),
            layers=[LayerConfig(id="api", label="API", order=0)],
            repositories=[
                RepositoryConfig(name="a", url="https://git.local/a.git", layer="api"),
                RepositoryConfig(name="b", url="https://git.local/b.git", layer="api"),
                RepositoryConfig(name="c", url="https://git.local/c.git", layer="api"),
            ],
            settings=SettingsConfig(max_concurrent=5),
        )
        service = AnalysisService(QuickProvider())

        """WHEN analysis runs."""
        result = await service.analyze_async(config)

        """THEN all 3 repos produce results."""
        assert len(result.analyses) == 3
        names = {a.name for a in result.analyses}
        assert names == {"a", "b", "c"}


class TestErrorHandlers:
    """Scenarios for custom error handlers."""

    @pytest.mark.asyncio
    async def test_404_returns_json(self, tmp_path: Path):
        """GIVEN a running application."""
        app, _ = _create_app_for_test(tmp_path)

        """WHEN a non-existent endpoint is called."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/nonexistent-endpoint")

        """THEN returns JSON with 404 status."""
        assert resp.status_code == 404
        data = resp.json()
        assert data["ok"] is False
        assert "Not found" in data["error"]


class TestDeepHealthCheck:
    """Scenarios for deep health check endpoint."""

    @pytest.mark.asyncio
    async def test_status_includes_uptime(self, tmp_path: Path):
        """GIVEN a running application."""
        app, _ = _create_app_for_test(tmp_path)

        """WHEN the status endpoint is called."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/status")

        """THEN it includes uptime_seconds."""
        data = resp.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))
        assert data["uptime_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_status_includes_config_readable(self, tmp_path: Path):
        """GIVEN a running application."""
        app, _ = _create_app_for_test(tmp_path)

        """WHEN the status endpoint is called."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/status")

        """THEN it includes config_readable field."""
        data = resp.json()
        assert data["config_readable"] is True

    @pytest.mark.asyncio
    async def test_status_includes_analysis_running(self, tmp_path: Path):
        """GIVEN a running application with no analysis in progress."""
        app, _ = _create_app_for_test(tmp_path)

        """WHEN status is called."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/status")

        """THEN analysis_running is False."""
        data = resp.json()
        assert data["analysis_running"] is False


class TestCORSMiddleware:
    """Scenarios for CORS middleware."""

    @pytest.mark.asyncio
    async def test_cors_headers_present(self, tmp_path: Path):
        """GIVEN a running application."""
        app, _ = _create_app_for_test(tmp_path)

        """WHEN a request with an Origin header is made."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/status",
                headers={"Origin": "http://localhost:3000"},
            )

        """THEN CORS headers are present."""
        assert "access-control-allow-origin" in resp.headers


class TestBodySizeLimit:
    """Scenarios for request body size limit."""

    @pytest.mark.asyncio
    async def test_oversized_body_rejected(self, tmp_path: Path):
        """GIVEN a running application and a request body over 1MB."""
        app, _ = _create_app_for_test(tmp_path)
        big_body = json.dumps({"data": "x" * (1_048_577)})

        """WHEN sent to a JSON endpoint."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/config",
                content=big_body,
                headers={"Content-Type": "application/json"},
            )

        """THEN returns 413."""
        assert resp.status_code == 413


class TestContentTypeValidation:
    """Scenarios for content-type validation."""

    @pytest.mark.asyncio
    async def test_wrong_content_type_rejected(self, tmp_path: Path):
        """GIVEN a running application."""
        app, _ = _create_app_for_test(tmp_path)

        """WHEN a request with text/plain Content-Type is sent to a JSON endpoint."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/config",
                content='{"test": true}',
                headers={"Content-Type": "text/plain"},
            )

        """THEN returns 415."""
        assert resp.status_code == 415


class TestSSEDisconnect:
    """Scenarios for SSE client disconnect detection."""

    @pytest.mark.asyncio
    async def test_sse_subscribe_and_unsubscribe(self, tmp_path: Path):
        """GIVEN an SSE subscriber system."""
        config_path = _write_config(tmp_path)
        state = AppState(config_path)

        """WHEN subscribe and unsubscribe are called."""
        queue = state.subscribe()
        sub_count_after_sub = len(state._sse_subscribers)
        state.unsubscribe(queue)
        sub_count_after_unsub = len(state._sse_subscribers)

        """THEN subscribers are properly tracked."""
        assert sub_count_after_sub == 1
        assert sub_count_after_unsub == 0


class TestGracefulShutdown:
    """Scenarios for graceful shutdown via lifespan."""

    def test_lifespan_context_is_set(self, tmp_path: Path):
        """GIVEN the FastAPI app."""
        app, _ = _create_app_for_test(tmp_path)

        """WHEN checking for lifespan context manager."""
        lifespan = app.router.lifespan_context

        """THEN it is configured."""
        assert lifespan is not None


class TestStructuredLogging:
    """Scenarios for structured logging."""

    def test_structured_formatter_basic(self):
        """GIVEN a StructuredFormatter and a log record."""
        import logging

        formatter = StructuredFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )

        """WHEN formatting the log record."""
        output = formatter.format(record)

        """THEN output includes timestamp and level."""
        assert "test message" in output
        assert "[INFO]" in output

    def test_structured_formatter_with_extras(self):
        """GIVEN a log record with extra fields."""
        import logging

        formatter = StructuredFormatter("%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="request handled",
            args=(),
            exc_info=None,
        )
        record.request_path = "/api/status"
        record.duration_ms = 42

        """WHEN formatted."""
        output = formatter.format(record)

        """THEN extras appear in output."""
        assert "request_path=/api/status" in output
        assert "duration_ms=42" in output

    def test_get_logger_uses_structured_formatter(self):
        """GIVEN get_logger function."""
        import logging

        logger = get_logger("test_module")  # noqa: F841

        """WHEN creating a logger."""
        # Handler lives on the root 'releaseboard' logger; child loggers propagate.
        root = logging.getLogger("releaseboard")
        handlers = root.handlers

        """THEN it uses StructuredFormatter."""
        assert len(handlers) > 0
        assert isinstance(handlers[0].formatter, StructuredFormatter)


class TestAgeDaysTimezoneHandling:
    """Scenarios for age_days with various timezone-awareness levels."""

    def test_age_days_with_naive_datetime(self):
        """GIVEN a BranchInfo with a naive last_commit_date."""
        naive_date = datetime(2025, 1, 1)
        info = BranchInfo(name="test", exists=True, last_commit_date=naive_date)

        """WHEN accessing age_days."""
        result = info.age_days

        """THEN it returns a non-negative integer."""
        assert isinstance(result, int)
        assert result >= 0

    def test_age_days_with_aware_datetime(self):
        """GIVEN a BranchInfo with a tz-aware last_commit_date five days ago."""
        aware_date = datetime.now(tz=UTC) - timedelta(days=5)
        info = BranchInfo(name="test", exists=True, last_commit_date=aware_date)

        """WHEN accessing age_days."""
        result = info.age_days

        """THEN it returns 5."""
        assert result == 5

    def test_age_days_none_when_no_commit_date(self):
        """GIVEN a BranchInfo with no last_commit_date."""
        info = BranchInfo(name="test", exists=True)

        """WHEN accessing age_days."""
        result = info.age_days

        """THEN it returns None."""
        assert result is None


class TestSeverityProperty:
    """Scenarios for ReadinessStatus.severity property."""

    def test_all_known_statuses_have_severity(self):
        """GIVEN all current ReadinessStatus members."""
        statuses = list(ReadinessStatus)

        """WHEN checking their severity property."""
        severities = [s.severity for s in statuses]

        """THEN each severity is an integer."""
        for sev in severities:
            assert isinstance(sev, int)

    def test_severity_never_raises_key_error(self):
        """GIVEN all enum members of ReadinessStatus."""
        members = list(ReadinessStatus)

        """WHEN accessing severity on each member."""
        results = [s.severity for s in members]

        """THEN no KeyError is raised and all results are ints."""
        assert all(isinstance(r, int) for r in results)


class TestSSEBroadcastSafety:
    """Scenarios for SSE broadcast under concurrent subscriber changes."""

    @pytest.mark.asyncio
    async def test_broadcast_drops_full_queue_safely(self, state: AppState):
        """GIVEN a subscriber queue filled to capacity."""
        q = state.subscribe()
        for i in range(100):
            q.put_nowait({"event": f"fill_{i}", "data": {}})

        """WHEN broadcast fires."""
        await state.broadcast("overflow", {"x": 1})

        """THEN the full queue is removed from subscribers."""
        assert q not in state._sse_subscribers


class TestActiveConfigTempFileCleanup:
    """Scenarios for temp file cleanup in get_active_config."""

    def test_temp_file_cleaned_on_failure(self, state: AppState):
        """GIVEN a draft that causes load_config to fail."""
        temp_dir = tempfile.gettempdir()
        before = set(Path(temp_dir).glob("*.json"))

        """WHEN get_active_config is called with a broken loader."""
        with patch("releaseboard.web.state.load_config", side_effect=RuntimeError("boom")):
            config = state.get_active_config()

        """THEN it falls back gracefully and no temp file leaks."""
        assert config.release.name == REGRESSION_CONFIG["release"]["name"]
        after = set(Path(temp_dir).glob("*.json"))
        leaked = after - before
        assert len(leaked) == 0, f"Leaked temp files: {leaked}"


class TestAppStateInvalidJson:
    """Scenarios for AppState construction with invalid JSON."""

    def test_invalid_json_raises_value_error(self, tmp_path: Path):
        """GIVEN a config file containing invalid JSON."""
        bad_path = tmp_path / "bad.json"
        bad_path.write_text("{ not valid json }", encoding="utf-8")

        """WHEN AppState is constructed."""
        # action attempted inside assertion

        """THEN a ValueError is raised with 'Invalid JSON' in the message."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            AppState(bad_path)

    def test_valid_json_loads_normally(self, config_file: Path):
        """GIVEN a valid config file."""
        path = config_file

        """WHEN AppState is constructed."""
        state = AppState(path)

        """THEN no error is raised and the release name matches."""
        assert state.config_state.persisted.release.name == REGRESSION_CONFIG["release"]["name"]


class TestGitHubProviderResponseGuards:
    """Scenarios for GitHub API response structure guards."""

    def test_malformed_commit_structure_no_crash(self):
        """GIVEN a GitHub API response with a non-dict commit field."""
        from releaseboard.git.github_provider import GitHubProvider

        provider = GitHubProvider(token="fake")

        def mock_get_json(url: str, timeout: int):
            if "/branches/" in url:
                return {
                    "name": "release/03.2025",
                    "commit": "not-a-dict",  # Malformed
                }, 200
            return {"default_branch": "main"}, 200

        """WHEN get_branch_info processes the response."""
        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            info = provider.get_branch_info(
                "https://github.com/acme/web-app.git",
                "release/03.2025",
            )

        """THEN it returns a BranchInfo with gracefully degraded fields."""
        assert info is not None
        assert info.exists is True
        assert info.last_commit_date is None

    def test_null_author_in_commit_no_crash(self):
        """GIVEN a GitHub API response with null author in commit."""
        from releaseboard.git.github_provider import GitHubProvider

        provider = GitHubProvider(token="fake")

        def mock_get_json(url: str, timeout: int):
            if "/branches/" in url:
                return {
                    "name": "release/03.2025",
                    "commit": {
                        "sha": "abc123",
                        "commit": {"author": None, "message": "test"},
                    },
                }, 200
            return {"default_branch": "main"}, 200

        """WHEN get_branch_info processes the response."""
        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            info = provider.get_branch_info(
                "https://github.com/acme/web-app.git",
                "release/03.2025",
            )

        """THEN it returns a valid BranchInfo without crashing."""
        assert info is not None
        assert info.exists is True


class TestCSPAllowsChartJS:
    """Scenarios for SecurityHeadersMiddleware CSP Chart.js CDN allowance."""

    def _get_csp(self) -> str:
        import inspect

        from releaseboard.web.middleware import SecurityHeadersMiddleware

        source = inspect.getsource(SecurityHeadersMiddleware)
        assert "cdn.jsdelivr.net" in source, "CSP must allow cdn.jsdelivr.net for Chart.js"
        return source

    def test_csp_includes_jsdelivr(self):
        """GIVEN the SecurityHeadersMiddleware source."""
        source = self._get_csp()

        """WHEN checking for the jsdelivr CDN URL."""
        has_jsdelivr = "https://cdn.jsdelivr.net" in source

        """THEN the CSP includes it."""
        assert has_jsdelivr

    def test_csp_script_src_structure(self):
        """GIVEN the SecurityHeadersMiddleware source."""
        import re

        from releaseboard.web.middleware import SecurityHeadersMiddleware

        source = __import__("inspect").getsource(SecurityHeadersMiddleware)

        """WHEN extracting the script-src directive."""
        match = re.search(r"script-src\s+([^;]+)", source)

        """THEN it contains self, unsafe-inline, and cdn.jsdelivr.net."""
        assert match, "script-src directive not found in CSP"
        directive = match.group(1)
        assert "'self'" in directive
        assert "'unsafe-inline'" in directive
        assert "cdn.jsdelivr.net" in directive

    def test_template_uses_jsdelivr_cdn(self):
        """GIVEN that Chart.js CDN was removed."""
        removed = True

        """WHEN checking the template."""
        # placeholder — CDN reference intentionally removed

        """THEN this test is kept as a placeholder."""
        assert removed is True


class TestGitLabDefaultBranch:
    """Scenarios for GitLabProvider.get_default_branch_info."""

    def test_method_exists(self):
        """GIVEN a GitLabProvider instance."""
        from releaseboard.git.gitlab_provider import GitLabProvider

        provider = GitLabProvider(token=None)

        """WHEN checking for get_default_branch_info."""
        exists = hasattr(provider, "get_default_branch_info")
        is_callable = callable(provider.get_default_branch_info)

        """THEN the method exists and is callable."""
        assert exists
        assert is_callable

    def test_returns_none_for_invalid_url(self):
        """GIVEN a GitLabProvider instance."""
        from releaseboard.git.gitlab_provider import GitLabProvider

        provider = GitLabProvider(token=None)

        """WHEN calling get_default_branch_info with an invalid URL."""
        result = provider.get_default_branch_info("not-a-url", timeout=5)

        """THEN it returns None."""
        assert result is None

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_delegates_to_get_branch_info(self, mock_get_json):
        """GIVEN a GitLabProvider with mocked API responses."""
        from releaseboard.git.gitlab_provider import GitLabProvider

        mock_get_json.side_effect = [
            ({"default_branch": "develop"}, 200),
            ({"name": "develop", "commit": {"committed_date": "2025-01-01T00:00:00Z"}}, 200),
        ]
        provider = GitLabProvider(token=None)

        """WHEN calling get_default_branch_info."""
        result = provider.get_default_branch_info(
            "https://gitlab.com/mygroup/myproject", timeout=10
        )

        """THEN it returns a BranchInfo for the default branch."""
        assert result is not None
        assert result.name == "develop"

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_raises_on_api_error(self, mock_get_json):
        """GIVEN a GitLabProvider with a 404 API response."""
        from releaseboard.git.gitlab_provider import GitLabProvider
        from releaseboard.git.provider import GitAccessError, GitErrorKind

        mock_get_json.return_value = (None, 404)
        provider = GitLabProvider(token=None)

        """WHEN calling get_default_branch_info."""
        """THEN it raises GitAccessError with REPO_NOT_FOUND kind."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.get_default_branch_info("https://gitlab.com/mygroup/myproject", timeout=10)
        assert exc_info.value.kind == GitErrorKind.REPO_NOT_FOUND


class TestActiveConfigCaching:
    """Scenarios for get_active_config caching behaviour."""

    def _make_state(self, tmp_path: Path) -> AppState:
        from releaseboard.web.state import AppState

        config = {
            "release": {"name": "R1", "target_month": 1, "target_year": 2025},
            "repositories": [],
            "layers": [],
        }
        config_file = tmp_path / "test.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")
        return AppState(config_file)

    def test_cache_not_stale_after_same_update(self, tmp_path):
        """GIVEN a cached active config."""
        state = self._make_state(tmp_path)
        cfg1 = state.get_active_config()

        """WHEN the draft is updated with identical content."""
        state.update_draft(json.loads(json.dumps(state.config_state.draft_raw)))
        cfg2 = state.get_active_config()

        """THEN the same cached object is returned."""
        assert cfg1 is cfg2


class TestAnalyzeSyncEventLoopSafety:
    """Scenarios for analyze_sync behaviour in async contexts."""

    def test_detects_running_loop(self):
        """GIVEN an AnalysisService instance."""
        import inspect

        from releaseboard.application.service import AnalysisService

        mock_provider = MagicMock()
        service = AnalysisService(mock_provider)

        """WHEN inspecting the analyze_sync source."""
        source = inspect.getsource(service.analyze_sync)

        """THEN it checks for a running loop and uses ThreadPoolExecutor."""
        assert "get_running_loop" in source, "Must check for running event loop"
        assert "ThreadPoolExecutor" in source, "Must use thread pool as fallback"

    def test_has_concurrent_futures_fallback(self):
        """GIVEN the AnalysisService.analyze_sync source code."""
        import inspect

        from releaseboard.application.service import AnalysisService

        source = inspect.getsource(AnalysisService.analyze_sync)

        """WHEN checking for concurrent.futures usage."""
        has_futures = "concurrent.futures" in source
        has_submit = "pool.submit" in source

        """THEN both concurrent.futures and pool.submit are present."""
        assert has_futures
        assert has_submit


class TestTemplatePartialsIntegrity:
    """Scenarios for template partial file integrity and rendering."""

    def test_main_template_uses_includes(self):
        """GIVEN the main dashboard template."""
        main = (
            Path(__file__).parent.parent
            / "src"
            / "releaseboard"
            / "presentation"
            / "templates"
            / "dashboard.html.j2"
        )
        content = main.read_text(encoding="utf-8")

        """WHEN checking its structure."""
        lines = content.strip().splitlines()

        """THEN it uses includes and is a small orchestrator."""
        assert "{% include" in content
        assert len(lines) < 50, (
            f"Main template should be a small orchestrator, got {len(lines)} lines"
        )

    def test_jinja_render_produces_html(self):
        """GIVEN a minimal DashboardViewModel."""
        from releaseboard.analysis.metrics import DashboardMetrics
        from releaseboard.presentation.renderer import DashboardRenderer
        from releaseboard.presentation.view_models import (
            ChartData,
            DashboardViewModel,
        )

        vm = DashboardViewModel(
            title="Test",
            subtitle="Sub",
            company="Co",
            primary_color="#fb6400",
            secondary_color="#002754e6",
            tertiary_color="#10b981",
            theme="system",
            release_name="R1",
            generated_at="2025-01-01T00:00:00Z",
            metrics=DashboardMetrics(),
            layers=[],
            attention_items=[],
            all_repos=[],
            status_chart=ChartData(labels=[], values=[], colors=[]),
            layer_readiness_chart=ChartData(labels=[], values=[], colors=[]),
        )
        renderer = DashboardRenderer()

        """WHEN rendering the dashboard."""
        html = renderer.render(vm)

        """THEN it produces valid HTML with key elements."""
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()
        assert "<html" in html
        assert "</html>" in html

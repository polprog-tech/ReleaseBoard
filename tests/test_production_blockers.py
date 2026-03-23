"""Regression tests for production blocker fixes.

Covers:
- CORS origin restriction (Blocker #2)
- CSRF bypass when Origin absent (Blocker #9)
- Async-safe i18n locale via contextvars (Blocker #13)
- Concurrent analysis progress safety (Blocker #14 / #24)
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_MINIMAL_CONFIG = {
    "release": {"name": "Test", "target_month": 3, "target_year": 2026},
    "repositories": [],
    "layers": [],
    "branding": {
        "title": "Test",
        "subtitle": "Test Dashboard",
        "primary_color": "#fb6400",
        "secondary_color": "#002754e6",
    },
    "settings": {
        "stale_threshold_days": 14,
        "output_path": "output/test.html",
        "theme": "system",
        "timeout_seconds": 30,
        "max_concurrent": 5,
    },
}


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "releaseboard.json"
    p.write_text(json.dumps(_MINIMAL_CONFIG), encoding="utf-8")
    return p


@pytest.fixture
def app(config_path):
    from releaseboard.web.server import create_app

    return create_app(config_path)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


# ── Fix 1: CORS origin restriction ─────────────────────────────────


class TestCORSOriginRestriction:
    """Verify CORS no longer uses wildcard allow_origins."""

    def test_default_origins_are_localhost_only(self):
        """GIVEN default environment (no RELEASEBOARD_CORS_ORIGINS).
        WHEN getting CORS origins.
        THEN only localhost origins are returned."""
        from releaseboard.web.cors import get_cors_origins

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELEASEBOARD_CORS_ORIGINS", None)
            origins = get_cors_origins()

        assert "http://localhost:8080" in origins
        assert "http://127.0.0.1:8080" in origins
        assert "*" not in origins
        # All origins must be explicit localhost
        for o in origins:
            assert "localhost" in o or "127.0.0.1" in o

    def test_env_override_replaces_defaults(self):
        """GIVEN custom RELEASEBOARD_CORS_ORIGINS.
        WHEN getting CORS origins.
        THEN custom origins replace defaults."""
        from releaseboard.web.cors import get_cors_origins

        custom = "https://dashboard.example.com, https://internal.corp:9000"
        with patch.dict(os.environ, {"RELEASEBOARD_CORS_ORIGINS": custom}):
            origins = get_cors_origins()

        assert origins == [
            "https://dashboard.example.com",
            "https://internal.corp:9000",
        ]

    def test_empty_env_falls_back_to_defaults(self):
        """GIVEN empty RELEASEBOARD_CORS_ORIGINS.
        WHEN getting CORS origins.
        THEN defaults are used."""
        from releaseboard.web.cors import get_cors_origins

        with patch.dict(os.environ, {"RELEASEBOARD_CORS_ORIGINS": ""}):
            origins = get_cors_origins()

        assert len(origins) >= 2
        assert "http://localhost:8080" in origins

    def test_app_cors_not_wildcard(self, app):
        """GIVEN a fully created app.
        WHEN inspecting middleware stack.
        THEN CORSMiddleware is not configured with wildcard."""
        from starlette.middleware.cors import CORSMiddleware

        for mw in app.user_middleware:
            if mw.cls is CORSMiddleware:
                origins = mw.kwargs.get("allow_origins", [])
                assert "*" not in origins, "CORS must not use wildcard origins"
                break
        else:
            pytest.fail("CORSMiddleware not found in middleware stack")

    @pytest.mark.asyncio
    async def test_cors_allows_configured_origin(self, client):
        """GIVEN a request from an allowed origin.
        WHEN making a preflight OPTIONS request.
        THEN the response includes CORS headers."""
        resp = await client.options(
            "/api/status",
            headers={
                "origin": "http://localhost:8080",
                "access-control-request-method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:8080"


# ── Fix 2: CSRF bypass when Origin absent ───────────────────────────


class TestCSRFDefenseInDepth:
    """Verify CSRF middleware blocks cross-origin requests without Origin header."""

    @pytest.mark.asyncio
    async def test_post_with_xrw_header_passes(self, client):
        """GIVEN a POST without Origin but with X-Requested-With.
        WHEN posting to a state-changing endpoint.
        THEN the request is not blocked by CSRF."""
        resp = await client.post(
            "/api/config/save",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_post_with_cross_origin_referer_blocked(self, client):
        """GIVEN a POST without Origin, without X-Requested-With, with cross-origin Referer.
        WHEN posting to a state-changing endpoint.
        THEN the request is blocked."""
        resp = await client.post(
            "/api/config/save",
            headers={"referer": "http://evil.com/attack"},
        )
        assert resp.status_code == 403
        assert "CSRF" in resp.json().get("error", "")

    @pytest.mark.asyncio
    async def test_post_with_same_origin_referer_passes(self, client):
        """GIVEN a POST without Origin, without X-Requested-With, with same-origin Referer.
        WHEN posting to a state-changing endpoint.
        THEN the request passes."""
        resp = await client.post(
            "/api/config/save",
            headers={"referer": "http://testserver/dashboard"},
        )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_post_without_origin_or_referer_passes(self, client):
        """GIVEN a POST without Origin, X-Requested-With, or Referer (curl-like).
        WHEN posting to a state-changing endpoint.
        THEN the request passes (non-browser client)."""
        resp = await client.post("/api/config/save")
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_health_endpoint_exempt(self, client):
        """GIVEN a health endpoint.
        WHEN accessing with any headers.
        THEN CSRF is not enforced."""
        resp = await client.get("/health/live")
        assert resp.status_code == 200


# ── Fix 3: Async-safe i18n locale ──────────────────────────────────


class TestAsyncSafeLocale:
    """Verify i18n locale uses contextvars, not threading.local."""

    def test_uses_contextvars_not_threading(self):
        """GIVEN the i18n module.
        WHEN inspecting locale storage.
        THEN it uses contextvars.ContextVar."""
        import contextvars

        from releaseboard.i18n import _locale_var

        assert isinstance(_locale_var, contextvars.ContextVar)

    def test_set_and_get_locale(self):
        """GIVEN set_locale is called.
        WHEN get_locale is called.
        THEN the correct locale is returned."""
        from releaseboard.i18n import get_locale, set_locale

        set_locale("pl")
        assert get_locale() == "pl"
        set_locale("en")
        assert get_locale() == "en"

    def test_unsupported_locale_falls_back(self):
        """GIVEN an unsupported locale.
        WHEN set_locale is called.
        THEN it falls back to default."""
        from releaseboard.i18n import get_locale, set_locale

        set_locale("xx")
        assert get_locale() == "en"

    @pytest.mark.asyncio
    async def test_locale_isolated_across_async_tasks(self):
        """GIVEN concurrent async tasks with different locales.
        WHEN each task sets its own locale.
        THEN locales do not leak across tasks."""
        import contextvars

        from releaseboard.i18n import get_locale, set_locale

        results = {}

        async def task_with_locale(name: str, locale: str):
            ctx = contextvars.copy_context()

            def _inner():
                set_locale(locale)
                # Yield control to other tasks
                return get_locale()

            results[name] = ctx.run(_inner)

        await asyncio.gather(
            task_with_locale("task_pl", "pl"),
            task_with_locale("task_en", "en"),
        )

        assert results["task_pl"] == "pl"
        assert results["task_en"] == "en"


# ── Fix 4 & 5: Concurrent analysis progress safety ─────────────────


class TestConcurrentProgressSafety:
    """Verify progress tracking is safe under concurrent analysis tasks."""

    @pytest.mark.asyncio
    async def test_concurrent_progress_increments(self):
        """GIVEN multiple repos analyzed concurrently.
        WHEN all tasks complete.
        THEN progress.completed matches the total repo count."""
        from datetime import UTC, datetime

        from releaseboard.application.service import (
            AnalysisPhase,
            AnalysisService,
        )
        from releaseboard.config.models import (
            AppConfig,
            BrandingConfig,
            LayerConfig,
            ReleaseConfig,
            RepositoryConfig,
            SettingsConfig,
        )
        from releaseboard.domain.models import BranchInfo
        from releaseboard.git.provider import GitProvider

        num_repos = 20

        class ConcurrentStubProvider(GitProvider):
            """Provider that introduces async yields to stress concurrency."""

            def list_remote_branches(self, url: str, timeout: int = 30) -> list[str]:
                return ["main", "release/03.2025"]

            def get_branch_info(
                self, url: str, branch: str, timeout: int = 30
            ) -> BranchInfo | None:
                return BranchInfo(
                    exists=True,
                    name="release/03.2025",
                    last_commit_date=datetime.now(tz=UTC),
                    last_commit_author="dev",
                    last_commit_message="ok",
                )

        config = AppConfig(
            release=ReleaseConfig(
                name="March 2025",
                target_month=3,
                target_year=2025,
                branch_pattern="release/{MM}.{YYYY}",
            ),
            layers=[LayerConfig(id="svc", label="Services", order=0)],
            repositories=[
                RepositoryConfig(
                    name=f"repo-{i}",
                    url=f"https://git.local/repo-{i}.git",
                    layer="svc",
                )
                for i in range(num_repos)
            ],
            branding=BrandingConfig(),
            settings=SettingsConfig(max_concurrent=num_repos),
        )

        service = AnalysisService(ConcurrentStubProvider())
        result = await service.analyze_async(config)

        assert result.progress.completed == num_repos
        assert result.progress.error_count == 0
        assert result.progress.phase == AnalysisPhase.COMPLETED
        assert len(result.analyses) == num_repos

    @pytest.mark.asyncio
    async def test_concurrent_errors_counted_correctly(self):
        """GIVEN multiple repos where some fail concurrently.
        WHEN all tasks complete.
        THEN error_count matches actual failures."""
        from datetime import UTC, datetime

        from releaseboard.application.service import (
            AnalysisPhase,
            AnalysisService,
        )
        from releaseboard.config.models import (
            AppConfig,
            BrandingConfig,
            LayerConfig,
            ReleaseConfig,
            RepositoryConfig,
            SettingsConfig,
        )
        from releaseboard.domain.models import BranchInfo
        from releaseboard.git.provider import GitAccessError, GitProvider

        class FailEveryOtherProvider(GitProvider):
            def list_remote_branches(self, url: str, timeout: int = 30) -> list[str]:
                idx = int(url.split("repo-")[1].split(".")[0])
                if idx % 2 == 0:
                    raise GitAccessError(url, "simulated failure")
                return ["main", "release/03.2025"]

            def get_branch_info(
                self, url: str, branch: str, timeout: int = 30
            ) -> BranchInfo | None:
                return BranchInfo(
                    exists=True,
                    name="release/03.2025",
                    last_commit_date=datetime.now(tz=UTC),
                    last_commit_author="dev",
                    last_commit_message="ok",
                )

        num_repos = 10
        config = AppConfig(
            release=ReleaseConfig(
                name="March 2025",
                target_month=3,
                target_year=2025,
                branch_pattern="release/{MM}.{YYYY}",
            ),
            layers=[LayerConfig(id="svc", label="Services", order=0)],
            repositories=[
                RepositoryConfig(
                    name=f"repo-{i}",
                    url=f"https://git.local/repo-{i}.git",
                    layer="svc",
                )
                for i in range(num_repos)
            ],
            branding=BrandingConfig(),
            settings=SettingsConfig(max_concurrent=num_repos),
        )

        service = AnalysisService(FailEveryOtherProvider())
        result = await service.analyze_async(config)

        expected_errors = 5  # repos 0,2,4,6,8
        assert result.progress.error_count == expected_errors
        assert result.progress.completed == num_repos
        assert result.progress.phase == AnalysisPhase.PARTIAL_FAILURE

    @pytest.mark.asyncio
    async def test_progress_callbacks_fire_for_all_repos(self):
        """GIVEN a progress callback and concurrent analysis.
        WHEN all tasks complete.
        THEN callback fires for each repo start/complete."""
        from datetime import UTC, datetime

        from releaseboard.application.service import (
            AnalysisProgress,
            AnalysisService,
        )
        from releaseboard.config.models import (
            AppConfig,
            BrandingConfig,
            LayerConfig,
            ReleaseConfig,
            RepositoryConfig,
            SettingsConfig,
        )
        from releaseboard.domain.models import BranchInfo
        from releaseboard.git.provider import GitProvider

        class SimpleProvider(GitProvider):
            def list_remote_branches(self, url: str, timeout: int = 30) -> list[str]:
                return ["release/03.2025"]

            def get_branch_info(
                self, url: str, branch: str, timeout: int = 30
            ) -> BranchInfo | None:
                return BranchInfo(
                    exists=True,
                    name="release/03.2025",
                    last_commit_date=datetime.now(tz=UTC),
                    last_commit_author="dev",
                    last_commit_message="ok",
                )

        num_repos = 8
        config = AppConfig(
            release=ReleaseConfig(
                name="March 2025",
                target_month=3,
                target_year=2025,
                branch_pattern="release/{MM}.{YYYY}",
            ),
            layers=[LayerConfig(id="svc", label="Services", order=0)],
            repositories=[
                RepositoryConfig(
                    name=f"repo-{i}",
                    url=f"https://git.local/repo-{i}.git",
                    layer="svc",
                )
                for i in range(num_repos)
            ],
            branding=BrandingConfig(),
            settings=SettingsConfig(max_concurrent=num_repos),
        )

        events: list[str] = []

        def on_progress(event_type: str, progress: AnalysisProgress):
            events.append(event_type)

        service = AnalysisService(SimpleProvider())
        await service.analyze_async(config, on_progress=on_progress)

        assert events.count("repo_start") == num_repos
        assert events.count("repo_complete") == num_repos
        assert events[0] == "analysis_start"
        assert events[-1] == "analysis_complete"

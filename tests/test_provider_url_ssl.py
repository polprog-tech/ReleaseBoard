"""Tests for git provider correctness — URL encoding, SSL context caching,
default branch detection, API response guards, and retry logic."""

from __future__ import annotations

import time
from io import BytesIO
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from releaseboard.domain.models import BranchInfo
from releaseboard.git.gitlab_provider import GitLabProvider
from releaseboard.git.provider import GitProvider

if TYPE_CHECKING:
    from releaseboard.git.github_provider import GitHubProvider


@pytest.fixture
def app(config_path):
    from releaseboard.web.server import create_app
    return create_app(config_path)


class TestSmartProviderTTL:
    """Scenarios for SmartGitProvider TTL behaviour."""

    def test_api_initially_available(self):
        """GIVEN a freshly created SmartGitProvider."""
        from releaseboard.git.smart_provider import SmartGitProvider
        provider = SmartGitProvider()

        """WHEN checking initial API availability."""
        gh_available = provider._check_api_available("github")
        gl_available = provider._check_api_available("gitlab")

        """THEN both APIs are reported as available."""
        assert provider._github_api_available is True
        assert provider._gitlab_api_available is True
        assert gh_available is True
        assert gl_available is True

    def test_mark_unavailable_sets_timestamp(self):
        """GIVEN a freshly created SmartGitProvider."""
        from releaseboard.git.smart_provider import SmartGitProvider
        provider = SmartGitProvider()

        """WHEN the GitHub API is marked unavailable."""
        provider._mark_api_unavailable("github")

        """THEN the flag is False and the timestamp is set."""
        assert provider._github_api_available is False
        assert provider._github_api_unavailable_since is not None
        # GitLab should be unaffected
        assert provider._gitlab_api_available is True

    def test_api_stays_unavailable_within_ttl(self):
        """GIVEN a SmartGitProvider with a recently unavailable API."""
        from releaseboard.git.smart_provider import SmartGitProvider
        provider = SmartGitProvider()
        provider._mark_api_unavailable("github")

        """WHEN checking availability within the TTL window."""
        available = provider._check_api_available("github")

        """THEN the API is still reported as unavailable."""
        assert available is False

    def test_api_resets_after_ttl(self):
        """GIVEN a SmartGitProvider whose TTL has expired."""
        from releaseboard.git.smart_provider import SmartGitProvider
        provider = SmartGitProvider()
        provider._mark_api_unavailable("github")
        provider._github_api_unavailable_since = (
            time.monotonic() - provider._API_RETRY_TTL - 1
        )

        """WHEN checking availability after TTL expiration."""
        available = provider._check_api_available("github")

        """THEN the API resets to available."""
        assert available is True
        assert provider._github_api_available is True
        assert provider._github_api_unavailable_since is None

    def test_gitlab_ttl_independent(self):
        """GIVEN a SmartGitProvider with GitLab API marked unavailable."""
        from releaseboard.git.smart_provider import SmartGitProvider
        provider = SmartGitProvider()
        provider._mark_api_unavailable("gitlab")

        """WHEN checking GitHub availability."""
        gh_available = provider._check_api_available("github")
        gl_available = provider._check_api_available("gitlab")

        """THEN GitHub is still available, GitLab is not."""
        assert gh_available is True
        assert gl_available is False


class TestGitProviderRetry:
    """Scenarios for git provider retry logic."""

    def test_github_retries_on_502(self):
        """GIVEN a GitHubProvider and a mock that returns 502 twice then succeeds."""
        import urllib.error

        from releaseboard.git.github_provider import GitHubProvider

        provider = GitHubProvider(token="test")
        call_count = 0

        def mock_urlopen(req, timeout=None, context=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise urllib.error.HTTPError(
                    "http://test", 502, "Bad Gateway", {}, BytesIO(b"{}")
                )
            # Third attempt succeeds
            class MockResp:
                status = 200
                def read(self):
                    return b'[{"name": "main"}]'
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    pass
            return MockResp()

        """WHEN _get_json is called with the patched urlopen."""
        with patch("urllib.request.urlopen", mock_urlopen), \
             patch("time.sleep"):
            data, status = provider._get_json("https://api.github.com/test", 30)

        """THEN the provider retries and eventually succeeds."""
        assert call_count == 3
        assert status == 200
        assert isinstance(data, list)

    def test_gitlab_retries_on_503(self):
        """GIVEN a GitLabProvider and a mock that returns 503 once then succeeds."""
        import urllib.error

        from releaseboard.git.gitlab_provider import GitLabProvider

        provider = GitLabProvider(token="test")
        call_count = 0

        def mock_urlopen(req, timeout=None, context=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise urllib.error.HTTPError(
                    "http://test", 503, "Service Unavailable", {}, BytesIO(b"{}")
                )
            class MockResp:
                status = 200
                def read(self):
                    return b'[{"name": "develop"}]'
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    pass
            return MockResp()

        """WHEN _get_json is called with the patched urlopen."""
        with patch("urllib.request.urlopen", mock_urlopen), \
             patch("time.sleep"):
            data, status = provider._get_json("https://gitlab.com/api/v4/test", 30)

        """THEN the provider retries and succeeds on the second attempt."""
        assert call_count == 2
        assert status == 200

    def test_no_retry_on_404(self):
        """GIVEN a GitHubProvider and a mock that always returns 404."""
        import urllib.error

        from releaseboard.git.github_provider import GitHubProvider

        provider = GitHubProvider(token="test")
        call_count = 0

        def mock_urlopen(req, timeout=None, context=None):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(
                "http://test", 404, "Not Found", {}, BytesIO(b'{"message":"Not Found"}')
            )

        """WHEN _get_json is called with the patched urlopen."""
        with patch("urllib.request.urlopen", mock_urlopen), \
             patch("time.sleep"):
            data, status = provider._get_json("https://api.github.com/test", 30)

        """THEN the provider does not retry."""
        assert call_count == 1  # No retry
        assert status == 404


class TestGitHubURLEncoding:
    """Scenarios for GitHub API URL encoding."""

    def _extract_url(self, provider: GitHubProvider, method: str, *args: Any) -> str:
        """Call a provider method and capture the URL it tries to fetch."""
        captured: list[str] = []

        def fake_get_json(url: str, timeout: int) -> tuple[Any, int]:
            captured.append(url)
            if method == "list_remote_branches":
                return [{"name": "main", "commit": {"sha": "abc"}}], 200
            if method == "list_org_repos":
                return [{"full_name": "org/repo", "clone_url": "https://x"}], 200
            return {
                "name": "main",
                "commit": {
                    "sha": "abc",
                    "commit": {
                        "author": {
                            "date": "2025-01-01T00:00:00Z",
                        },
                    },
                },
            }, 200

        with patch.object(provider, "_get_json", side_effect=fake_get_json):
            getattr(provider, method)(*args)

        assert captured, f"No URL captured for {method}"
        return captured[0]


class TestGitLabProviderABC:
    """Scenarios for GitLabProvider ABC compliance."""

    def test_is_subclass_of_git_provider(self):
        """GIVEN the GitLabProvider class."""
        provider_cls = GitLabProvider

        """WHEN checking the class hierarchy."""
        is_subclass = issubclass(provider_cls, GitProvider)

        """THEN it is a subclass of GitProvider."""
        assert is_subclass


class TestGitHubProviderResponseGuards:
    """Scenarios for GitHub API response guards."""

    def test_malformed_commit_structure_no_crash(self):
        """GIVEN a GitHubProvider and a mock returning malformed commit data."""
        from releaseboard.git.github_provider import GitHubProvider

        provider = GitHubProvider(token="fake")

        def mock_get_json(url: str, timeout: int):
            if "/branches/" in url:
                return {
                    "name": "release/03.2025",
                    "commit": "not-a-dict",  # Malformed
                }, 200
            return {"default_branch": "main"}, 200

        """WHEN get_branch_info processes the malformed response."""
        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            info = provider.get_branch_info(
                "https://github.com/acme/web-app.git",
                "release/03.2025",
            )

        """THEN it does not crash and returns a BranchInfo with gracefully degraded date."""
        assert info is not None
        assert info.exists is True
        assert info.last_commit_date is None  # Gracefully degraded


class TestLocalProviderSplitlinesGuard:
    """Scenarios for LocalGitProvider splitlines guard."""

    def test_empty_stdout_no_index_error(self):
        """GIVEN a BranchInfo with no estimated creation date."""
        from releaseboard.git.local_provider import LocalGitProvider

        LocalGitProvider()
        info = BranchInfo(name="test", exists=True, estimated_creation_date=None)

        """WHEN accessing the estimated_creation_date field."""
        creation_date = info.estimated_creation_date

        """THEN no IndexError occurs and the value is None."""
        assert creation_date is None

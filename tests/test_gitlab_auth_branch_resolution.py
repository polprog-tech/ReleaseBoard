"""Tests for GitLab authentication flow, branch resolution, and smart provider routing.

Covers:
- GitLabProvider error classification (401, 403, 404, network errors)
- Branch names with slashes (release/2026.04) URL encoding
- SmartGitProvider routing for GitLab URLs
- SmartGitProvider fallback from GitLab API to git CLI
- Token propagation from SmartGitProvider to tag enrichment
- Authenticated GitLab provider reuse in AnalysisService
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from releaseboard.domain.models import BranchInfo
from releaseboard.git.gitlab_provider import GitLabProvider, is_gitlab_url, parse_gitlab_url
from releaseboard.git.provider import GitAccessError, GitErrorKind
from releaseboard.git.smart_provider import SmartGitProvider

# ---------------------------------------------------------------------------
# GitLabProvider: error classification on branch / project lookups
# ---------------------------------------------------------------------------


class TestGitLabProviderErrorClassification:
    """GitLab API should raise classified errors, not return silent failures."""

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_list_branches_raises_on_401(self, mock_get_json):
        """GIVEN a private repo returning HTTP 401."""
        mock_get_json.return_value = ({"message": "401 Unauthorized"}, 401)
        provider = GitLabProvider(token="bad-token")

        """WHEN listing branches."""
        """THEN it raises GitAccessError with AUTH_REQUIRED kind."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.list_remote_branches(
                "https://gitlab.com/myorg/private-repo", timeout=5
            )
        assert exc_info.value.kind == GitErrorKind.AUTH_REQUIRED

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_list_branches_raises_on_403(self, mock_get_json):
        """GIVEN a repo with insufficient permissions returning HTTP 403."""
        mock_get_json.return_value = ({"message": "403 Forbidden"}, 403)
        provider = GitLabProvider(token="limited-token")

        """WHEN listing branches."""
        """THEN it raises GitAccessError with ACCESS_DENIED kind."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.list_remote_branches(
                "https://gitlab.com/myorg/restricted-repo", timeout=5
            )
        assert exc_info.value.kind == GitErrorKind.ACCESS_DENIED

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_list_branches_raises_on_404(self, mock_get_json):
        """GIVEN a non-existent repo returning HTTP 404."""
        mock_get_json.return_value = ({"message": "404 Project Not Found"}, 404)
        provider = GitLabProvider(token="valid-token")

        """WHEN listing branches."""
        """THEN it raises GitAccessError with REPO_NOT_FOUND kind."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.list_remote_branches(
                "https://gitlab.com/myorg/nonexistent", timeout=5
            )
        assert exc_info.value.kind == GitErrorKind.REPO_NOT_FOUND

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_list_branches_raises_on_network_error(self, mock_get_json):
        """GIVEN a network error (status 0)."""
        mock_get_json.return_value = (None, 0)
        provider = GitLabProvider(token="valid-token")

        """WHEN listing branches."""
        """THEN it raises GitAccessError with NETWORK_ERROR kind."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.list_remote_branches(
                "https://gitlab.internal.company.com/team/repo", timeout=5
            )
        assert exc_info.value.kind == GitErrorKind.NETWORK_ERROR

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_get_branch_info_raises_on_401(self, mock_get_json):
        """GIVEN a private repo returning HTTP 401 on branch lookup."""
        mock_get_json.return_value = ({"message": "401 Unauthorized"}, 401)
        provider = GitLabProvider(token="expired-token")

        """WHEN getting branch info."""
        """THEN it raises GitAccessError with AUTH_REQUIRED kind."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.get_branch_info(
                "https://gitlab.com/myorg/private-repo",
                "release/2026.04",
                timeout=5,
            )
        assert exc_info.value.kind == GitErrorKind.AUTH_REQUIRED

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_get_branch_info_returns_not_found_on_404(self, mock_get_json):
        """GIVEN a branch that doesn't exist (HTTP 404)."""
        mock_get_json.return_value = ({"message": "404 Branch Not Found"}, 404)
        provider = GitLabProvider(token="valid-token")

        """WHEN getting branch info."""
        result = provider.get_branch_info(
            "https://gitlab.com/myorg/repo",
            "release/2026.99",
            timeout=5,
        )

        """THEN it returns BranchInfo with exists=False (not an error)."""
        assert result is not None
        assert result.exists is False
        assert result.data_source == "gitlab_api"

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_get_branch_info_raises_on_403(self, mock_get_json):
        """GIVEN a repo with 403 on branch lookup."""
        mock_get_json.return_value = ({"message": "403 Forbidden"}, 403)
        provider = GitLabProvider(token="limited-token")

        """WHEN getting branch info."""
        """THEN it raises GitAccessError with ACCESS_DENIED kind."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.get_branch_info(
                "https://gitlab.com/myorg/restricted-repo",
                "release/2026.04",
                timeout=5,
            )
        assert exc_info.value.kind == GitErrorKind.ACCESS_DENIED

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_get_default_branch_raises_on_401(self, mock_get_json):
        """GIVEN a 401 on the project info endpoint."""
        mock_get_json.return_value = ({"message": "401 Unauthorized"}, 401)
        provider = GitLabProvider(token="bad-token")

        """WHEN getting default branch info."""
        """THEN it raises GitAccessError with AUTH_REQUIRED kind."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.get_default_branch_info(
                "https://gitlab.com/myorg/private-repo", timeout=5
            )
        assert exc_info.value.kind == GitErrorKind.AUTH_REQUIRED


# ---------------------------------------------------------------------------
# GitLabProvider: branch resolution with slashes
# ---------------------------------------------------------------------------


class TestGitLabBranchSlashEncoding:
    """Branch names with slashes must be properly URL-encoded."""

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_branch_with_slash_encoded_correctly(self, mock_get_json):
        """GIVEN a branch name with a slash like release/2026.04."""
        mock_get_json.return_value = (
            {
                "name": "release/2026.04",
                "commit": {
                    "id": "abc123",
                    "committed_date": "2026-03-15T10:00:00+00:00",
                    "author_name": "Dev",
                    "message": "fix: stuff",
                },
            },
            200,
        )
        provider = GitLabProvider(token="valid-token")

        """WHEN getting branch info."""
        result = provider.get_branch_info(
            "https://gitlab.com/myorg/myrepo",
            "release/2026.04",
            timeout=5,
        )

        """THEN the API URL uses %2F encoding for the slash."""
        call_url = mock_get_json.call_args[0][0]
        assert "release%2F2026.04" in call_url
        assert result is not None
        assert result.exists is True
        assert result.name == "release/2026.04"
        assert result.last_commit_sha == "abc123"

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_branch_with_multiple_slashes(self, mock_get_json):
        """GIVEN a branch name with multiple slashes."""
        mock_get_json.return_value = (
            {
                "name": "feature/team/JIRA-123",
                "commit": {
                    "id": "ghi789",
                    "committed_date": "2026-03-10T08:00:00+00:00",
                    "author_name": "Dev",
                    "message": "feat: new feature",
                },
            },
            200,
        )
        provider = GitLabProvider(token="valid-token")

        """WHEN getting branch info."""
        result = provider.get_branch_info(
            "https://gitlab.com/myorg/myrepo",
            "feature/team/JIRA-123",
            timeout=5,
        )

        """THEN all slashes are encoded."""
        call_url = mock_get_json.call_args[0][0]
        assert "feature%2Fteam%2FJIRA-123" in call_url
        assert result.exists is True


# ---------------------------------------------------------------------------
# GitLabProvider: authentication header attachment
# ---------------------------------------------------------------------------


class TestGitLabAuthHeaders:
    """Token must be attached to every request from the start."""

    def test_headers_include_token_when_set(self):
        """GIVEN a GitLabProvider with a token."""
        provider = GitLabProvider(token="glpat-secret-token")

        """WHEN building request headers."""
        headers = provider._headers()

        """THEN PRIVATE-TOKEN header is present."""
        assert headers["PRIVATE-TOKEN"] == "glpat-secret-token"

    def test_headers_omit_token_when_empty(self):
        """GIVEN a GitLabProvider without a token."""
        provider = GitLabProvider(token=None)
        # Clear env var fallback for test isolation
        provider._token = ""

        """WHEN building request headers."""
        headers = provider._headers()

        """THEN PRIVATE-TOKEN header is NOT present."""
        assert "PRIVATE-TOKEN" not in headers

    def test_token_exposed_via_property(self):
        """GIVEN a GitLabProvider with a token."""
        provider = GitLabProvider(token="my-token-value")

        """WHEN reading the token property."""
        """THEN it returns the token."""
        assert provider.token == "my-token-value"


# ---------------------------------------------------------------------------
# GitLab URL detection
# ---------------------------------------------------------------------------


class TestGitLabURLDetection:
    """is_gitlab_url must distinguish GitLab from GitHub and other hosts."""

    def test_gitlab_com_detected(self):
        assert is_gitlab_url("https://gitlab.com/group/project") is True

    def test_github_com_rejected(self):
        assert is_gitlab_url("https://github.com/owner/repo") is False

    def test_simple_path_not_gitlab(self):
        assert is_gitlab_url("my-repo") is False

    def test_self_hosted_gitlab_detected(self):
        assert is_gitlab_url(
            "https://gitlab.internal.company.com/EMEA/GAD/OPS/UI/reports"
        ) is True

    def test_nested_namespace_parsed(self):
        result = parse_gitlab_url(
            "https://gitlab.internal.company.com/EMEA/GAD/OPS/UI/reports"
        )
        assert result is not None
        host, namespace, project = result
        assert host == "gitlab.internal.company.com"
        assert project == "reports"
        assert "EMEA" in namespace


# ---------------------------------------------------------------------------
# SmartGitProvider: GitLab routing with authentication
# ---------------------------------------------------------------------------


class TestSmartProviderGitLabRouting:
    """SmartGitProvider must route GitLab URLs to GitLabProvider with token."""

    @patch("releaseboard.git.gitlab_provider.GitLabProvider.list_remote_branches")
    def test_gitlab_url_uses_gitlab_provider(self, mock_list):
        """GIVEN a SmartGitProvider with gitlab_token."""
        mock_list.return_value = ["main", "release/2026.04"]
        provider = SmartGitProvider(gitlab_token="glpat-test-token")

        """WHEN listing branches for a GitLab URL."""
        branches = provider.list_remote_branches(
            "https://gitlab.com/myorg/myrepo", timeout=10
        )

        """THEN GitLabProvider is used (not LocalGitProvider)."""
        mock_list.assert_called_once()
        assert "release/2026.04" in branches

    @patch("releaseboard.git.gitlab_provider.GitLabProvider.get_branch_info")
    def test_gitlab_branch_info_uses_gitlab_provider(self, mock_info):
        """GIVEN a SmartGitProvider with gitlab_token."""
        mock_info.return_value = BranchInfo(
            name="release/2026.04", exists=True, data_source="gitlab_api"
        )
        provider = SmartGitProvider(gitlab_token="glpat-test-token")

        """WHEN getting branch info for a GitLab URL."""
        result = provider.get_branch_info(
            "https://gitlab.com/myorg/myrepo",
            "release/2026.04",
            timeout=10,
        )

        """THEN GitLabProvider is used."""
        mock_info.assert_called_once()
        assert result.exists is True
        assert result.data_source == "gitlab_api"

    @patch("releaseboard.git.gitlab_provider.GitLabProvider.get_default_branch_info")
    def test_gitlab_default_branch_uses_gitlab_provider(self, mock_default):
        """GIVEN a SmartGitProvider with gitlab_token."""
        mock_default.return_value = BranchInfo(
            name="main", exists=True, repo_default_branch="main",
            data_source="gitlab_api",
        )
        provider = SmartGitProvider(gitlab_token="glpat-test-token")

        """WHEN getting default branch info for a GitLab URL."""
        result = provider.get_default_branch_info(
            "https://gitlab.com/myorg/myrepo", timeout=10
        )

        """THEN GitLabProvider is used."""
        mock_default.assert_called_once()
        assert result.exists is True

    @patch("releaseboard.git.gitlab_provider.GitLabProvider.list_remote_branches")
    def test_gitlab_auth_error_not_swallowed(self, mock_list):
        """GIVEN a GitLab API returning 401 (auth failure)."""
        mock_list.side_effect = GitAccessError(
            "https://gitlab.com/myorg/private-repo",
            "Authentication required (HTTP 401).",
            kind=GitErrorKind.AUTH_REQUIRED,
        )
        provider = SmartGitProvider(gitlab_token="bad-token")

        """WHEN listing branches."""
        """THEN the auth error propagates (no fallback to git CLI)."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.list_remote_branches(
                "https://gitlab.com/myorg/private-repo", timeout=5
            )
        assert exc_info.value.kind == GitErrorKind.AUTH_REQUIRED

    @patch("releaseboard.git.gitlab_provider.GitLabProvider.list_remote_branches")
    def test_gitlab_access_denied_not_swallowed(self, mock_list):
        """GIVEN a GitLab API returning 403 (access denied)."""
        mock_list.side_effect = GitAccessError(
            "https://gitlab.com/myorg/restricted-repo",
            "Access denied (HTTP 403).",
            kind=GitErrorKind.ACCESS_DENIED,
        )
        provider = SmartGitProvider(gitlab_token="limited-token")

        """WHEN listing branches."""
        """THEN the access error propagates (no fallback)."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.list_remote_branches(
                "https://gitlab.com/myorg/restricted-repo", timeout=5
            )
        assert exc_info.value.kind == GitErrorKind.ACCESS_DENIED

    @patch("releaseboard.git.local_provider.LocalGitProvider.list_remote_branches")
    @patch("releaseboard.git.gitlab_provider.GitLabProvider.list_remote_branches")
    def test_gitlab_network_error_falls_back_to_cli(
        self, mock_gl_list, mock_local_list
    ):
        """GIVEN a GitLab API that's unreachable (network error)."""
        mock_gl_list.side_effect = GitAccessError(
            "https://gitlab.com/myorg/repo",
            "Cannot connect to GitLab API.",
            kind=GitErrorKind.NETWORK_ERROR,
        )
        mock_local_list.return_value = ["main", "release/2026.04"]
        provider = SmartGitProvider(gitlab_token="valid-token")

        """WHEN listing branches."""
        branches = provider.list_remote_branches(
            "https://gitlab.com/myorg/repo", timeout=5
        )

        """THEN it falls back to git CLI."""
        assert "release/2026.04" in branches
        mock_local_list.assert_called_once()

    @patch("releaseboard.git.gitlab_provider.GitLabProvider.get_branch_info")
    def test_gitlab_branch_info_auth_error_propagates(self, mock_info):
        """GIVEN a 401 on branch info lookup."""
        mock_info.side_effect = GitAccessError(
            "https://gitlab.com/myorg/private-repo",
            "Authentication required (HTTP 401).",
            kind=GitErrorKind.AUTH_REQUIRED,
        )
        provider = SmartGitProvider(gitlab_token="expired-token")

        """WHEN getting branch info."""
        """THEN the auth error propagates (not swallowed as exists=False)."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.get_branch_info(
                "https://gitlab.com/myorg/private-repo",
                "release/2026.04",
                timeout=5,
            )
        assert exc_info.value.kind == GitErrorKind.AUTH_REQUIRED


# ---------------------------------------------------------------------------
# AnalysisService: token propagation for tag enrichment
# ---------------------------------------------------------------------------


class TestServiceTokenPropagation:
    """Tag enrichment must reuse the authenticated GitLab provider."""

    def test_service_reuses_authenticated_provider(self):
        """GIVEN an AnalysisService using SmartGitProvider with gitlab_token."""
        from releaseboard.application.service import AnalysisService

        smart = SmartGitProvider(gitlab_token="glpat-enrichment-token")
        service = AnalysisService(smart)

        """WHEN the service checks the git provider."""
        """THEN it can access the authenticated GitLab provider."""
        assert isinstance(service.git_provider, SmartGitProvider)
        assert service.git_provider.gitlab_provider.token == "glpat-enrichment-token"


# ---------------------------------------------------------------------------
# GitLabProvider: successful authenticated branch lookup
# ---------------------------------------------------------------------------

class TestGitLabAuthenticatedBranchLookup:
    """Full scenario: token + private repo + branch with slashes = success."""

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_authenticated_branch_found(self, mock_get_json):
        """GIVEN a valid token and a private GitLab repo with release/2026.04."""

        def route_response(url, timeout):
            if "/repository/branches/release%2F2026.04" in url:
                return (
                    {
                        "name": "release/2026.04",
                        "commit": {
                            "id": "a1b2c3d4e5f6",
                            "committed_date": "2026-03-20T10:30:00+00:00",
                            "author_name": "Engineer",
                            "message": "chore: prepare release 2026.04",
                        },
                    },
                    200,
                )
            if "/repository/branches?" in url:
                return (
                    [
                        {"name": "main"},
                        {"name": "develop"},
                        {"name": "release/2026.04"},
                    ],
                    200,
                )
            return (None, 404)

        mock_get_json.side_effect = route_response
        provider = GitLabProvider(token="glpat-valid-token")

        """WHEN listing branches and getting branch info."""
        branches = provider.list_remote_branches(
            "https://gitlab.internal.company.com/EMEA/GAD/OPS/UI/reports",
            timeout=10,
        )
        branch_info = provider.get_branch_info(
            "https://gitlab.internal.company.com/EMEA/GAD/OPS/UI/reports",
            "release/2026.04",
            timeout=10,
        )

        """THEN branches are found and branch info is populated."""
        assert "release/2026.04" in branches
        assert branch_info.exists is True
        assert branch_info.name == "release/2026.04"
        assert branch_info.last_commit_sha == "a1b2c3d4e5f6"
        assert branch_info.last_commit_author == "Engineer"
        assert branch_info.data_source == "gitlab_api"

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_authenticated_branch_genuinely_missing(self, mock_get_json):
        """GIVEN a valid token, repo exists, but branch doesn't."""

        def route_response(url, timeout):
            if "/repository/branches/release%2F2026.99" in url:
                return ({"message": "404 Branch Not Found"}, 404)
            if "/repository/branches?" in url:
                return ([{"name": "main"}, {"name": "develop"}], 200)
            return (None, 404)

        mock_get_json.side_effect = route_response
        provider = GitLabProvider(token="glpat-valid-token")

        """WHEN looking up a non-existent branch."""
        branches = provider.list_remote_branches(
            "https://gitlab.com/myorg/myrepo", timeout=10
        )
        branch_info = provider.get_branch_info(
            "https://gitlab.com/myorg/myrepo", "release/2026.99", timeout=10
        )

        """THEN branches list doesn't contain it and branch_info shows not found."""
        assert "release/2026.99" not in branches
        assert branch_info.exists is False
        assert branch_info.data_source == "gitlab_api"

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_unauthenticated_private_repo_raises_auth_error(self, mock_get_json):
        """GIVEN no token and a private/internal repo."""
        mock_get_json.return_value = (
            {"message": "401 Unauthorized"},
            401,
        )
        provider = GitLabProvider(token=None)
        provider._token = ""  # Ensure no env var fallback

        """WHEN listing branches."""
        """THEN it raises AUTH_REQUIRED (not silent empty list)."""
        with pytest.raises(GitAccessError) as exc_info:
            provider.list_remote_branches(
                "https://gitlab.internal.company.com/team/private-repo",
                timeout=5,
            )
        assert exc_info.value.kind == GitErrorKind.AUTH_REQUIRED


# ---------------------------------------------------------------------------
# GitLabProvider: get_default_branch_info enriched metadata
# ---------------------------------------------------------------------------


class TestGitLabDefaultBranchEnriched:
    """get_default_branch_info should return repo metadata."""

    @patch("releaseboard.git.gitlab_provider.GitLabProvider._get_json")
    def test_default_branch_includes_repo_metadata(self, mock_get_json):
        """GIVEN a GitLab project API returning full metadata."""

        def route_response(url, timeout):
            if "/repository/branches/" in url:
                return (
                    {
                        "name": "main",
                        "commit": {
                            "id": "abc123",
                            "committed_date": "2026-03-15T10:00:00+00:00",
                            "author_name": "Dev",
                            "message": "latest commit",
                        },
                    },
                    200,
                )
            # Project info endpoint
            return (
                {
                    "default_branch": "main",
                    "visibility": "internal",
                    "description": "Internal service",
                    "web_url": "https://gitlab.com/myorg/myrepo",
                },
                200,
            )

        mock_get_json.side_effect = route_response
        provider = GitLabProvider(token="valid-token")

        """WHEN getting default branch info."""
        result = provider.get_default_branch_info(
            "https://gitlab.com/myorg/myrepo", timeout=10
        )

        """THEN repo metadata is included in the response."""
        assert result is not None
        assert result.exists is True
        assert result.repo_default_branch == "main"
        assert result.repo_visibility == "internal"
        assert result.data_source == "gitlab_api"


# ---------------------------------------------------------------------------
# SmartGitProvider: runtime token update
# ---------------------------------------------------------------------------


class TestSmartProviderUpdateTokens:
    """update_tokens() must replace provider instances and reset availability."""

    def test_update_gitlab_token_creates_new_provider(self):
        """GIVEN a SmartGitProvider with no initial token."""
        provider = SmartGitProvider()
        assert provider.gitlab_provider.token == ""

        """WHEN updating the GitLab token."""
        provider.update_tokens(gitlab_token="glpat-new-token")

        """THEN the provider uses the new token."""
        assert provider.gitlab_provider.token == "glpat-new-token"

    def test_update_gitlab_token_resets_availability(self):
        """GIVEN a SmartGitProvider with GitLab API marked unavailable."""
        provider = SmartGitProvider()
        provider._mark_api_unavailable("gitlab")
        assert provider._gitlab_api_available is False

        """WHEN updating the GitLab token."""
        provider.update_tokens(gitlab_token="glpat-fresh-token")

        """THEN availability is reset so the new token gets a fresh chance."""
        assert provider._gitlab_api_available is True
        assert provider._gitlab_api_unavailable_since is None

    def test_update_github_token_only(self):
        """GIVEN a SmartGitProvider with a GitLab token."""
        provider = SmartGitProvider(gitlab_token="glpat-original")

        """WHEN updating only the GitHub token."""
        provider.update_tokens(github_token="ghp_new")

        """THEN the GitLab token is unchanged."""
        assert provider.gitlab_provider.token == "glpat-original"

    def test_update_none_leaves_existing(self):
        """GIVEN a SmartGitProvider with tokens."""
        provider = SmartGitProvider(
            github_token="ghp_existing", gitlab_token="glpat-existing"
        )

        """WHEN calling update_tokens with no arguments."""
        provider.update_tokens()

        """THEN existing tokens are unchanged."""
        assert provider.gitlab_provider.token == "glpat-existing"

    def test_update_both_tokens(self):
        """GIVEN a SmartGitProvider with no tokens."""
        provider = SmartGitProvider()

        """WHEN updating both tokens."""
        provider.update_tokens(
            github_token="ghp_both", gitlab_token="glpat-both"
        )

        """THEN both providers have new tokens."""
        assert provider._github._token == "ghp_both"
        assert provider.gitlab_provider.token == "glpat-both"

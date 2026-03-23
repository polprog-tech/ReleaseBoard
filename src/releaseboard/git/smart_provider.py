"""Smart git provider — dispatches to GitHub/GitLab API with git CLI fallback."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from releaseboard.git.github_provider import GitHubProvider, parse_github_url
from releaseboard.git.gitlab_provider import GitLabProvider, is_gitlab_url
from releaseboard.git.local_provider import LocalGitProvider
from releaseboard.git.provider import GitAccessError, GitErrorKind, GitProvider

if TYPE_CHECKING:
    from releaseboard.domain.models import BranchInfo

logger = logging.getLogger(__name__)

# Error kinds that indicate API-level failure (not repository-specific)
_API_FAILURE_KINDS = frozenset({
    GitErrorKind.RATE_LIMITED,
    GitErrorKind.NETWORK_ERROR,
    GitErrorKind.PROVIDER_UNAVAILABLE,
    GitErrorKind.DNS_RESOLUTION,
    GitErrorKind.TIMEOUT,
})


class SmartGitProvider(GitProvider):
    """Routes requests to GitHubProvider/GitLabProvider for known hosts, else LocalGitProvider.

    When the REST API is unavailable (rate-limited, network error, etc.),
    automatically falls back to direct git CLI inspection (``git ls-remote``)
    for public repositories. This ensures repos remain analyzable even
    without API access.

    After ``_API_RETRY_TTL`` seconds, the provider re-attempts the REST API
    instead of staying permanently degraded.
    """

    _API_RETRY_TTL = 300  # Re-check API availability after 5 minutes

    def __init__(
        self,
        github_token: str | None = None,
        gitlab_token: str | None = None,
    ) -> None:
        self._github = GitHubProvider(token=github_token)
        self._gitlab = GitLabProvider(token=gitlab_token)
        self._local = LocalGitProvider()
        # Separate availability tracking for GitHub and GitLab APIs
        self._github_api_available = True
        self._github_api_unavailable_since: float | None = None
        self._gitlab_api_available = True
        self._gitlab_api_unavailable_since: float | None = None

    @property
    def gitlab_provider(self) -> GitLabProvider:
        """Expose the authenticated GitLab provider for reuse (e.g. tag enrichment)."""
        return self._gitlab

    def get_token_for_url(self, url: str) -> str:
        """Return the best available token for a given repository URL."""
        low = url.lower()
        if "github" in low:
            return getattr(self._github, "token", "") or ""
        if "gitlab" in low:
            return self._gitlab.token
        return ""

    def _auth_url(self, repo_url: str) -> str:
        """Inject auth credentials into a URL for git CLI operations.

        GitHub:  https://TOKEN@github.com/org/repo
        GitLab:  https://oauth2:TOKEN@gitlab.example.com/group/project
        """
        token = self.get_token_for_url(repo_url)
        if not token:
            return repo_url
        parsed = urlparse(repo_url)
        if not parsed.scheme or not parsed.hostname:
            return repo_url
        if self._is_gitlab(repo_url):
            netloc = f"oauth2:{token}@{parsed.hostname}"
        else:
            netloc = f"{token}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))

    def update_tokens(
        self,
        *,
        github_token: str | None = None,
        gitlab_token: str | None = None,
    ) -> None:
        """Update provider tokens at runtime (e.g. when user supplies them via UI).

        Only non-None values are applied; passing None leaves the existing token
        unchanged.  Tokens are held in memory only — never persisted to disk.
        """
        if github_token is not None:
            self._github = GitHubProvider(token=github_token or None)
            self._github_api_available = True
            self._github_api_unavailable_since = None
            logger.info("GitHub token updated (token %s)", "set" if github_token else "cleared")
        if gitlab_token is not None:
            self._gitlab = GitLabProvider(token=gitlab_token or None)
            self._gitlab_api_available = True
            self._gitlab_api_unavailable_since = None
            logger.info("GitLab token updated (token %s)", "set" if gitlab_token else "cleared")

    def _check_api_available(self, provider: str) -> bool:
        """Check if an API should be attempted (with TTL-based reset)."""
        if provider == "github":
            available = self._github_api_available
            since = self._github_api_unavailable_since
        else:
            available = self._gitlab_api_available
            since = self._gitlab_api_unavailable_since

        if available:
            return True
        if since is not None:
            elapsed = time.monotonic() - since
            if elapsed >= self._API_RETRY_TTL:
                if provider == "github":
                    self._github_api_available = True
                    self._github_api_unavailable_since = None
                else:
                    self._gitlab_api_available = True
                    self._gitlab_api_unavailable_since = None
                logger.info(
                    "Re-enabling %s API after %.0fs cooldown", provider, elapsed,
                )
                return True
        return False

    def _mark_api_unavailable(self, provider: str) -> None:
        """Record that an API is temporarily unavailable."""
        if provider == "github":
            self._github_api_available = False
            self._github_api_unavailable_since = time.monotonic()
        else:
            self._gitlab_api_available = False
            self._gitlab_api_unavailable_since = time.monotonic()

    def _is_github(self, repo_url: str) -> bool:
        return parse_github_url(repo_url) is not None

    def _is_gitlab(self, repo_url: str) -> bool:
        return is_gitlab_url(repo_url)

    def list_remote_branches(self, repo_url: str, timeout: int = 30) -> list[str]:
        if self._is_github(repo_url):
            return self._list_branches_github(repo_url, timeout)
        if self._is_gitlab(repo_url):
            return self._list_branches_gitlab(repo_url, timeout)
        return self._local.list_remote_branches(repo_url, timeout)

    def _list_branches_github(self, repo_url: str, timeout: int) -> list[str]:
        api_error: GitAccessError | None = None
        if self._check_api_available("github"):
            try:
                return self._github.list_remote_branches(repo_url, timeout)
            except GitAccessError as exc:
                if exc.kind not in _API_FAILURE_KINDS:
                    raise
                api_error = exc
                self._mark_api_unavailable("github")
                logger.info(
                    "GitHub API unavailable (%s) for %s, falling back to git CLI",
                    exc.kind.value, repo_url,
                )

        try:
            return self._local.list_remote_branches(self._auth_url(repo_url), timeout)
        except GitAccessError as local_exc:
            api_detail = api_error.detail if api_error else "skipped (previously failed)"
            msg = f"GitHub API: {api_detail}; Git CLI: {local_exc.detail}"
            raise GitAccessError(repo_url, msg, kind=local_exc.kind) from local_exc

    def _list_branches_gitlab(self, repo_url: str, timeout: int) -> list[str]:
        api_error: GitAccessError | None = None
        if self._check_api_available("gitlab"):
            try:
                return self._gitlab.list_remote_branches(repo_url, timeout)
            except GitAccessError as exc:
                if exc.kind not in _API_FAILURE_KINDS:
                    raise  # Repo-specific error (auth, not found) — don't fall back
                api_error = exc
                self._mark_api_unavailable("gitlab")
                logger.info(
                    "GitLab API unavailable (%s) for %s, falling back to git CLI",
                    exc.kind.value, repo_url,
                )

        try:
            return self._local.list_remote_branches(self._auth_url(repo_url), timeout)
        except GitAccessError as local_exc:
            api_detail = api_error.detail if api_error else "skipped (previously failed)"
            msg = f"GitLab API: {api_detail}; Git CLI: {local_exc.detail}"
            raise GitAccessError(repo_url, msg, kind=local_exc.kind) from local_exc

    def get_branch_info(
        self, repo_url: str, branch_name: str, timeout: int = 30
    ) -> BranchInfo | None:
        if self._is_github(repo_url):
            return self._get_branch_info_github(repo_url, branch_name, timeout)
        if self._is_gitlab(repo_url):
            return self._get_branch_info_gitlab(repo_url, branch_name, timeout)
        return self._local.get_branch_info(repo_url, branch_name, timeout)

    def _get_branch_info_github(
        self, repo_url: str, branch_name: str, timeout: int
    ) -> BranchInfo | None:
        if self._check_api_available("github"):
            try:
                return self._github.get_branch_info(repo_url, branch_name, timeout)
            except GitAccessError as exc:
                if exc.kind not in _API_FAILURE_KINDS:
                    raise
                self._mark_api_unavailable("github")
                logger.info(
                    "GitHub API unavailable (%s) for branch info, using git CLI",
                    exc.kind.value,
                )
        return self._local.get_branch_info(self._auth_url(repo_url), branch_name, timeout)

    def _get_branch_info_gitlab(
        self, repo_url: str, branch_name: str, timeout: int
    ) -> BranchInfo | None:
        if self._check_api_available("gitlab"):
            try:
                return self._gitlab.get_branch_info(repo_url, branch_name, timeout)
            except GitAccessError as exc:
                if exc.kind not in _API_FAILURE_KINDS:
                    raise  # Auth/access/not-found — surface to caller
                self._mark_api_unavailable("gitlab")
                logger.info(
                    "GitLab API unavailable (%s) for branch info, using git CLI",
                    exc.kind.value,
                )
        return self._local.get_branch_info(self._auth_url(repo_url), branch_name, timeout)

    def get_default_branch_info(
        self, repo_url: str, timeout: int = 30
    ) -> BranchInfo | None:
        """Get default branch info — tries REST API, then git CLI fallback."""
        if self._is_github(repo_url):
            return self._get_default_branch_github(repo_url, timeout)
        if self._is_gitlab(repo_url):
            return self._get_default_branch_gitlab(repo_url, timeout)
        return self._local.get_default_branch_info(repo_url, timeout)

    def _get_default_branch_github(
        self, repo_url: str, timeout: int
    ) -> BranchInfo | None:
        if self._check_api_available("github"):
            try:
                return self._github.get_default_branch_info(repo_url, timeout)
            except GitAccessError as exc:
                if exc.kind not in _API_FAILURE_KINDS:
                    raise
                self._mark_api_unavailable("github")
                logger.info(
                    "GitHub API unavailable (%s) for default branch, using git CLI",
                    exc.kind.value,
                )
        return self._local.get_default_branch_info(self._auth_url(repo_url), timeout)

    def _get_default_branch_gitlab(
        self, repo_url: str, timeout: int
    ) -> BranchInfo | None:
        if self._check_api_available("gitlab"):
            try:
                return self._gitlab.get_default_branch_info(repo_url, timeout)
            except GitAccessError as exc:
                if exc.kind not in _API_FAILURE_KINDS:
                    raise
                self._mark_api_unavailable("gitlab")
                logger.info(
                    "GitLab API unavailable (%s) for default branch, using git CLI",
                    exc.kind.value,
                )
        return self._local.get_default_branch_info(self._auth_url(repo_url), timeout)

"""Abstract git provider protocol and error classification."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ssl

    from releaseboard.domain.models import BranchInfo

logger = logging.getLogger(__name__)


class GitProvider(ABC):
    """Abstract interface for accessing git repository data.

    Implementations may use local git CLI, GitHub API, GitLab API, etc.
    """

    @abstractmethod
    def list_remote_branches(self, repo_url: str, timeout: int = 30) -> list[str]:
        """List all branch names in a remote repository.

        Args:
            repo_url: Repository URL or local path.
            timeout: Operation timeout in seconds.

        Returns:
            List of branch names (without refs/heads/ prefix).

        Raises:
            GitAccessError: If the repository cannot be accessed.
        """

    @abstractmethod
    def get_branch_info(
        self, repo_url: str, branch_name: str, timeout: int = 30
    ) -> BranchInfo | None:
        """Get metadata for a specific branch.

        Args:
            repo_url: Repository URL or local path.
            branch_name: Branch name to inspect.
            timeout: Operation timeout in seconds.

        Returns:
            BranchInfo if the branch exists, None otherwise.
        """


class GitErrorKind(StrEnum):
    """Classified categories of git access failures."""

    DNS_RESOLUTION = "dns_resolution"
    AUTH_REQUIRED = "auth_required"
    REPO_NOT_FOUND = "repo_not_found"
    ACCESS_DENIED = "access_denied"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    INVALID_URL = "invalid_url"
    LOCAL_PATH_MISSING = "local_path_missing"
    GIT_CLI_MISSING = "git_cli_missing"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    PLACEHOLDER_URL = "placeholder_url"
    UNKNOWN = "unknown"

    @property
    def user_message(self) -> str:
        """Short human-readable label (locale-aware via i18n catalog)."""
        return self.localized_message()

    def localized_message(self, locale: str | None = None) -> str:
        """Return a locale-aware user message."""
        from releaseboard.i18n import t

        _key_map = {
            "dns_resolution": "error.host_not_found",
            "network_error": "error.network",
        }
        key = _key_map.get(self.value, f"error.{self.value}")
        return t(key, locale=locale)


class GitAccessError(Exception):
    """Raised when a git operation fails, with classified error kind."""

    def __init__(self, repo_url: str, message: str, kind: GitErrorKind | None = None) -> None:
        self.repo_url = repo_url
        self.kind = kind or classify_git_error(message, repo_url)
        self.detail = message
        super().__init__(f"Git access error for '{repo_url}': {message}")

    @property
    def user_message(self) -> str:
        """Concise user-facing message."""
        return self.kind.user_message


# --- Example / Placeholder URL Detection ---

_EXAMPLE_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "git.example.com",
        "git.example.org",
        "git.example.net",
        "localhost.example",
        "test.example",
        "placeholder.com",
        "placeholder.org",
    }
)

_EXAMPLE_DOMAIN_PATTERNS = re.compile(
    r"(?:^|\.)(?:example|placeholder|test|invalid|localhost)\."
    r"(?:com|org|net|local)$",
    re.IGNORECASE,
)


def is_placeholder_url(url: str) -> bool:
    """Check if a URL uses known placeholder/example domains.

    Returns True for domains like git.example.com, *.placeholder.org, etc.
    """
    url = url.strip()
    if not url:
        return True

    # Extract hostname from various URL formats
    hostname = _extract_hostname(url)
    if not hostname:
        return False

    hostname_lower = hostname.lower()

    if hostname_lower in _EXAMPLE_DOMAINS:
        return True
    return bool(_EXAMPLE_DOMAIN_PATTERNS.search(hostname_lower))


def _extract_hostname(url: str) -> str | None:
    """Extract the hostname from a git URL (HTTPS, SSH, git@, etc)."""
    # git@host:org/repo
    ssh_short = re.match(r"^git@([^:]+):", url)
    if ssh_short:
        return ssh_short.group(1)

    # ssh://git@host/...
    ssh_full = re.match(r"^ssh://[^@]*@([^/:]+)", url)
    if ssh_full:
        return ssh_full.group(1)

    # https://host/... or http://host/...
    http_match = re.match(r"^https?://([^/:]+)", url)
    if http_match:
        return http_match.group(1)

    # git://host/...
    git_match = re.match(r"^git://([^/:]+)", url)
    if git_match:
        return git_match.group(1)

    return None


def classify_git_error(message: str, repo_url: str = "") -> GitErrorKind:
    """Classify a raw git/network error message into a structured kind."""
    msg = message.lower()

    if repo_url and is_placeholder_url(repo_url):
        return GitErrorKind.PLACEHOLDER_URL

    if "could not resolve host" in msg or "name or service not known" in msg:
        return GitErrorKind.DNS_RESOLUTION
    if "connection timed out" in msg or "timed out" in msg:
        return GitErrorKind.TIMEOUT
    if "timeout after" in msg:
        return GitErrorKind.TIMEOUT
    if "rate limit" in msg or "api rate" in msg:
        return GitErrorKind.RATE_LIMITED
    if "authentication" in msg or "could not read username" in msg:
        return GitErrorKind.AUTH_REQUIRED
    if "repository not found" in msg or "does not exist" in msg or "404" in msg:
        return GitErrorKind.REPO_NOT_FOUND
    if "permission denied" in msg or "access denied" in msg:
        return GitErrorKind.ACCESS_DENIED
    if "403" in msg:
        return GitErrorKind.RATE_LIMITED
    if "unable to access" in msg and "could not resolve" in msg:
        return GitErrorKind.DNS_RESOLUTION
    if "unable to access" in msg:
        return GitErrorKind.NETWORK_ERROR
    if "cannot access" in msg and ("github" in msg or "repo" in msg):
        return GitErrorKind.PROVIDER_UNAVAILABLE
    if "not found" in msg and "git" in msg:
        return GitErrorKind.GIT_CLI_MISSING
    if "no such file or directory" in msg:
        return GitErrorKind.LOCAL_PATH_MISSING
    if "invalid" in msg and "url" in msg:
        return GitErrorKind.INVALID_URL

    return GitErrorKind.UNKNOWN


def make_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that works in corporate proxy environments.

    .. deprecated::
        Use :func:`releaseboard.shared.network.make_ssl_context` directly.
        This re-export is kept for backward compatibility.
    """
    from releaseboard.shared.network import make_ssl_context as _make

    return _make()

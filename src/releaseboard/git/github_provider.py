"""GitHub REST API provider for repository and branch metadata."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

from releaseboard.domain.models import BranchInfo
from releaseboard.git.provider import GitAccessError, GitErrorKind, GitProvider

if TYPE_CHECKING:
    import ssl

logger = logging.getLogger(__name__)

_GITHUB_URL_RE = re.compile(
    r"(?:https?://github\.com/|git@github\.com:)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?/?$"
)


def parse_github_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL. Returns None if not GitHub."""
    m = _GITHUB_URL_RE.match(url)
    if m:
        return m.group("owner"), m.group("repo")
    parsed = urlparse(url)
    if parsed.hostname and "github.com" in parsed.hostname:
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) >= 2:
            repo = parts[1].removesuffix(".git")
            return parts[0], repo
    return None


def parse_github_owner(url: str) -> str | None:
    """Extract owner/org from a GitHub URL that points to an org or user page.

    Accepts:
      - https://github.com/owner
      - https://github.com/owner/
      - https://github.com/owner/repo  (returns owner only)
    """
    parsed = urlparse(url.strip().rstrip("/"))
    if not parsed.hostname or "github.com" not in parsed.hostname:
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if parts:
        return parts[0]
    return None


class GitHubProvider(GitProvider):
    """Git provider that uses the GitHub REST API for richer metadata.

    Requires either:
    - GITHUB_TOKEN environment variable, or
    - token passed at construction time.

    Falls back gracefully when unauthenticated (public repos only, rate-limited).
    """

    def __init__(self, token: str | None = None, timeout: int = 30) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._default_timeout = timeout
        self._session = None
        self._ssl_ctx: ssl.SSLContext | None = None

    @property
    def token(self) -> str:
        """Return the current authentication token (may be empty)."""
        return self._token

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get_json(self, url: str, timeout: int) -> tuple[dict | list | None, int]:
        """Make a GET request with retry for transient server errors.

        Returns (parsed_json, http_status). On network errors returns (None, 0).
        Retries up to 2 times on HTTP 502/503/504 or network failures.
        """
        import json
        import time as _time
        import urllib.error
        import urllib.request

        ctx = self._get_ssl_context()
        last_data, last_status = None, 0

        for attempt in range(3):
            req = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    return json.loads(resp.read().decode()), resp.status
            except urllib.error.HTTPError as exc:
                logger.debug("GitHub API HTTP %d for %s: %s", exc.code, url, exc.reason)
                try:
                    body = json.loads(exc.read().decode())
                    last_data, last_status = body, exc.code
                except Exception:
                    last_data, last_status = None, exc.code
                # Retry on transient server errors
                if exc.code in (502, 503, 504) and attempt < 2:
                    _time.sleep(0.5 * (2 ** attempt))
                    continue
                return last_data, last_status
            except Exception as exc:
                logger.debug("GitHub API request failed for %s: %s", url, exc)
                last_data, last_status = None, 0
                if attempt < 2:
                    _time.sleep(0.5 * (2 ** attempt))
                    continue
                return last_data, last_status

        return last_data, last_status

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Return a cached SSL context (created once per provider instance)."""
        if self._ssl_ctx is None:
            self._ssl_ctx = self._ssl_context()
        return self._ssl_ctx

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        """Build an SSL context using certifi bundle when available.

        macOS Python often lacks proper default CA certs; certifi provides them.
        """
        import ssl

        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            return ssl.create_default_context()

    def _raise_for_status(
        self,
        repo_url: str,
        owner: str,
        repo: str,
        status: int,
        body: dict | list | None,
    ) -> None:
        """Raise a properly classified GitAccessError from an HTTP failure."""
        msg_detail = ""
        if isinstance(body, dict) and "message" in body:
            msg_detail = body["message"]

        if status == 404:
            raise GitAccessError(
                repo_url,
                f"Repository not found: {owner}/{repo}",
                kind=GitErrorKind.REPO_NOT_FOUND,
            )
        if status == 403:
            if "rate limit" in msg_detail.lower():
                raise GitAccessError(
                    repo_url,
                    f"GitHub API rate limit exceeded for {owner}/{repo}. "
                    "Set GITHUB_TOKEN for higher limits.",
                    kind=GitErrorKind.RATE_LIMITED,
                )
            raise GitAccessError(
                repo_url,
                f"Access denied to {owner}/{repo}: {msg_detail}",
                kind=GitErrorKind.ACCESS_DENIED,
            )
        if status == 401:
            raise GitAccessError(
                repo_url,
                f"Authentication required for {owner}/{repo}",
                kind=GitErrorKind.AUTH_REQUIRED,
            )
        if status == 0:
            raise GitAccessError(
                repo_url,
                f"Cannot connect to GitHub API for {owner}/{repo}",
                kind=GitErrorKind.NETWORK_ERROR,
            )
        raise GitAccessError(
            repo_url,
            f"GitHub API error (HTTP {status}) for {owner}/{repo}: {msg_detail}",
            kind=GitErrorKind.PROVIDER_UNAVAILABLE,
        )

    def list_remote_branches(self, repo_url: str, timeout: int = 30) -> list[str]:
        """List branch names via GitHub API."""
        parsed = parse_github_url(repo_url)
        if not parsed:
            raise GitAccessError(repo_url, "Not a recognized GitHub URL")

        owner, repo = parsed
        branches: list[str] = []
        page = 1
        # URL-encode owner/repo to handle special characters safely
        safe_owner = quote(owner, safe="")
        safe_repo = quote(repo, safe="")
        while True:
            url = (
                f"https://api.github.com/repos/{safe_owner}/{safe_repo}"
                f"/branches?per_page=100&page={page}"
            )
            data, status = self._get_json(url, timeout)
            if status >= 400 or (page == 1 and data is None):
                self._raise_for_status(repo_url, owner, repo, status, data)
            if not data or not isinstance(data, list):
                break
            branches.extend(b["name"] for b in data if "name" in b)
            if len(data) < 100:
                break
            page += 1
        return branches

    def get_branch_info(
        self, repo_url: str, branch_name: str, timeout: int = 30
    ) -> BranchInfo | None:
        """Get rich branch metadata from GitHub API."""
        parsed = parse_github_url(repo_url)
        if not parsed:
            return BranchInfo(name=branch_name, exists=False)

        owner, repo = parsed

        # URL-encode to handle special characters safely
        safe_owner = quote(owner, safe="")
        safe_repo = quote(repo, safe="")

        # Fetch repo metadata
        repo_data, repo_status = self._get_json(
            f"https://api.github.com/repos/{safe_owner}/{safe_repo}", timeout
        )
        if repo_status >= 400:
            repo_data = None

        # Fetch branch metadata (branch names may contain slashes)
        safe_branch = quote(branch_name, safe="")
        branch_url = (
            f"https://api.github.com/repos/{safe_owner}/{safe_repo}"
            f"/branches/{safe_branch}"
        )
        branch_data, branch_status = self._get_json(branch_url, timeout)

        if not branch_data or not isinstance(branch_data, dict) or "name" not in branch_data:
            return BranchInfo(
                name=branch_name,
                exists=False,
                repo_description=(
                    _safe_str(repo_data, "description")
                    if isinstance(repo_data, dict) else None
                ),
                repo_default_branch=(
                    _safe_str(repo_data, "default_branch")
                    if isinstance(repo_data, dict) else None
                ),
                repo_visibility=(
                    _safe_str(repo_data, "visibility")
                    if isinstance(repo_data, dict) else None
                ),
                repo_owner=owner,
                repo_archived=(
                    repo_data.get("archived")
                    if isinstance(repo_data, dict) else None
                ),
                repo_web_url=(
                    _safe_str(repo_data, "html_url")
                    if isinstance(repo_data, dict) else None
                ),
                data_source="github_api",
            )

        # Extract commit info from branch response
        commit = (
            branch_data.get("commit")
            if isinstance(branch_data.get("commit"), dict) else {}
        )
        commit_detail = (
            commit.get("commit")
            if isinstance(commit.get("commit"), dict) else {}
        )
        author_info = (
            commit_detail.get("author")
            if isinstance(commit_detail.get("author"), dict) else {}
        )

        commit_date = None
        if author_info.get("date"):
            with contextlib.suppress(ValueError, TypeError):
                commit_date = datetime.fromisoformat(
                    author_info["date"].replace("Z", "+00:00")
                )

        provider_updated = None
        if isinstance(repo_data, dict) and repo_data.get("updated_at"):
            with contextlib.suppress(ValueError, TypeError):
                provider_updated = datetime.fromisoformat(
                    repo_data["updated_at"].replace("Z", "+00:00")
                )

        return BranchInfo(
            name=branch_name,
            exists=True,
            last_commit_date=commit_date,
            last_commit_author=author_info.get("name"),
            last_commit_message=_truncate(commit_detail.get("message", ""), 120),
            last_commit_sha=commit.get("sha"),
            repo_description=(
                _safe_str(repo_data, "description")
                if isinstance(repo_data, dict) else None
            ),
            repo_default_branch=(
                _safe_str(repo_data, "default_branch")
                if isinstance(repo_data, dict) else None
            ),
            repo_visibility=(
                _safe_str(repo_data, "visibility")
                if isinstance(repo_data, dict) else None
            ),
            repo_owner=owner,
            repo_archived=(
                repo_data.get("archived")
                if isinstance(repo_data, dict) else None
            ),
            repo_web_url=(
                _safe_str(repo_data, "html_url")
                if isinstance(repo_data, dict) else None
            ),
            provider_updated_at=provider_updated,
            data_source="github_api",
        )

    def get_default_branch_info(
        self, repo_url: str, timeout: int = 30
    ) -> BranchInfo | None:
        """Get metadata for the repository's default branch.

        Useful as a fallback when the expected release branch is missing but
        the repository is reachable.
        """
        parsed = parse_github_url(repo_url)
        if not parsed:
            return None

        owner, repo = parsed
        safe_owner = quote(owner, safe="")
        safe_repo = quote(repo, safe="")
        repo_data, repo_status = self._get_json(
            f"https://api.github.com/repos/{safe_owner}/{safe_repo}", timeout
        )
        if repo_status >= 400 or not isinstance(repo_data, dict):
            return None

        default_branch = repo_data.get("default_branch", "main")
        return self.get_branch_info(repo_url, default_branch, timeout)

    def list_org_repos(
        self, owner: str, timeout: int = 30
    ) -> list[dict[str, Any]]:
        """List repositories for a GitHub user or organisation.

        Returns a list of dicts with keys: name, url, default_branch,
        description, archived, visibility.
        """
        repos: list[dict[str, Any]] = []
        page = 1
        safe_owner = quote(owner, safe="")
        # Try /orgs first; remember which prefix worked for pagination
        prefix = "orgs"
        while True:
            url = (
                f"https://api.github.com/{prefix}/{safe_owner}"
                f"/repos?per_page=100&page={page}&sort=name"
            )
            data, status = self._get_json(url, timeout)

            # On first page 404 from /orgs, retry with /users
            if page == 1 and prefix == "orgs" and status == 404:
                prefix = "users"
                continue

            if status >= 400 or not isinstance(data, list):
                if page == 1:
                    detail = ""
                    if isinstance(data, dict) and "message" in data:
                        detail = data["message"]
                    raise GitAccessError(
                        f"https://github.com/{owner}",
                        f"Cannot list repos for '{owner}' "
                        f"(HTTP {status}). {detail}".strip(),
                        kind=GitErrorKind.REPO_NOT_FOUND
                        if status == 404
                        else GitErrorKind.NETWORK_ERROR,
                    )
                break

            if not data:
                break

            for r in data:
                if not isinstance(r, dict):
                    continue
                if r.get("archived"):
                    continue
                repos.append({
                    "name": r.get("name", ""),
                    "url": r.get("html_url", ""),
                    "default_branch": r.get("default_branch", "main"),
                    "description": r.get("description") or "",
                    "visibility": r.get("visibility", ""),
                })

            if len(data) < 100:
                break
            page += 1

        return repos


def _safe_str(data: dict, key: str) -> str | None:
    val = data.get(key)
    return str(val) if val is not None else None


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."

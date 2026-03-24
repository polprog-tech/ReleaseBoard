"""GitLab REST API provider for repository and branch metadata."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from releaseboard.domain.models import BranchInfo, TagInfo
from releaseboard.git.provider import GitAccessError, GitErrorKind, GitProvider
from releaseboard.shared.network import make_ssl_context

if TYPE_CHECKING:
    import ssl

logger = logging.getLogger(__name__)


def parse_gitlab_url(url: str) -> tuple[str, str, str] | None:
    """Extract (host, namespace, project) from a GitLab URL.

    Returns None if the URL doesn't look like a GitLab repo.
    Supports gitlab.com and self-hosted instances.
    """
    parsed = urlparse(url.strip().rstrip("/"))
    if not parsed.hostname:
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2:
        project = parts[-1].removesuffix(".git")
        namespace = "/".join(parts[:-1])
        return parsed.hostname, namespace, project
    return None


def is_gitlab_url(url: str) -> bool:
    """Check if a URL is likely a GitLab repository (not GitHub).

    parse_gitlab_url() is intentionally permissive (supports self-hosted
    instances), so any URL with ≥2 path parts matches.  This helper
    additionally rejects known non-GitLab hosts like github.com.
    """
    if parse_gitlab_url(url) is None:
        return False
    hostname = urlparse(url.strip()).hostname or ""
    return "github.com" not in hostname.lower()


def parse_gitlab_group(url: str) -> tuple[str, str] | None:
    """Extract (api_base, group_path) from a GitLab group/user URL.

    Accepts:
      - https://gitlab.com/my-group
      - https://gitlab.com/my-group/subgroup
      - https://git.company.com/my-team
    """
    parsed = urlparse(url.strip().rstrip("/"))
    if not parsed.hostname:
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        return None
    scheme = parsed.scheme or "https"
    api_base = f"{scheme}://{parsed.hostname}/api/v4"
    group_path = "/".join(parts)
    return api_base, group_path


class GitLabProvider(GitProvider):
    """Read-only GitLab REST API v4 client for repository discovery.

    Supports gitlab.com and self-hosted instances.
    Implements the GitProvider ABC for branch listing and branch info.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("GITLAB_TOKEN", "")
        self._ssl_ctx: ssl.SSLContext | None = None

    @property
    def token(self) -> str:
        """Return the current authentication token (may be empty)."""
        return self._token

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token
        return headers

    def _api_base(self, host: str) -> str:
        """Build the API base URL.

        Authentication is handled via the ``PRIVATE-TOKEN`` header
        (see ``_headers()``).  Embedded URL credentials
        (``oauth2:TOKEN@host``) are not used because Python 3.13+
        rejects them as invalid URLs.
        """
        return f"https://{host}/api/v4"

    def _raise_for_status(
        self,
        repo_url: str,
        status: int,
        body: Any,
    ) -> None:
        """Raise a properly classified GitAccessError from an HTTP failure."""
        detail = ""
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error") or ""
            if isinstance(detail, dict):
                detail = str(detail)

        if status == 404:
            raise GitAccessError(
                repo_url,
                f"Repository not found (HTTP 404). {detail}".strip(),
                kind=GitErrorKind.REPO_NOT_FOUND,
            )
        if status == 401:
            raise GitAccessError(
                repo_url,
                f"Authentication required (HTTP 401). {detail}".strip(),
                kind=GitErrorKind.AUTH_REQUIRED,
            )
        if status == 403:
            hint = " Ensure the token has read access (Reporter role or higher) to this project."
            raise GitAccessError(
                repo_url,
                f"Access denied (HTTP 403). {detail}{hint}".strip(),
                kind=GitErrorKind.ACCESS_DENIED,
            )
        if status == 0:
            raise GitAccessError(
                repo_url,
                f"Cannot connect to GitLab API. {detail}".strip(),
                kind=GitErrorKind.NETWORK_ERROR,
            )
        raise GitAccessError(
            repo_url,
            f"GitLab API error (HTTP {status}). {detail}".strip(),
            kind=GitErrorKind.PROVIDER_UNAVAILABLE,
        )

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        return make_ssl_context()

    def _get_ssl_context(self) -> ssl.SSLContext:
        """Return a cached SSL context (created once per provider instance)."""
        if self._ssl_ctx is None:
            self._ssl_ctx = self._ssl_context()
        return self._ssl_ctx

    def _get_json(self, url: str, timeout: int) -> tuple[Any, int]:
        """GET request with retry for transient server errors.

        Returns (parsed_json, http_status). (None, 0) on network error.
        Retries up to 1 time on HTTP 502/503/504 (gateway errors).
        Non-transient failures (DNS, SSL, timeout, 4xx) fail immediately.
        """
        import time as _time

        ctx = self._get_ssl_context()

        for attempt in range(2):
            req = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    return json.loads(resp.read().decode()), resp.status
            except urllib.error.HTTPError as exc:
                logger.debug("GitLab API HTTP %d for %s: %s", exc.code, url, exc.reason)
                try:
                    body = json.loads(exc.read().decode())
                    last_data, last_status = body, exc.code
                except Exception:
                    last_data, last_status = None, exc.code
                # Only retry on transient gateway errors
                if exc.code in (502, 503, 504) and attempt < 1:
                    _time.sleep(0.5)
                    continue
                return last_data, last_status
            except Exception as exc:
                logger.debug("GitLab API request failed for %s: %s", url, exc)
                # Network errors (DNS, SSL, timeout) won't resolve with
                # a quick retry — fail immediately to avoid wasted time.
                return None, 0

        return None, 0

    def list_group_repos(
        self, api_base: str, group_path: str, timeout: int = 30
    ) -> list[dict[str, Any]]:
        """List projects in a GitLab group (or user namespace).

        Returns list of dicts: name, url, default_branch, description, visibility.
        """
        # Re-derive api_base with embedded token credentials so
        # that auth works even through proxies that strip headers.
        try:
            _host = urllib.parse.urlparse(api_base).hostname or ""
            if _host:
                api_base = self._api_base(_host)
        except Exception:
            pass
        encoded = urllib.parse.quote(group_path, safe="")
        repos: list[dict[str, Any]] = []
        page = 1

        # Try /groups/ first, fallback to /users/ (for personal namespaces)
        prefix = "groups"
        while True:
            url = (
                f"{api_base}/{prefix}/{encoded}"
                f"/projects?per_page=100&page={page}"
                f"&include_subgroups=true&archived=false&order_by=name&sort=asc"
            )
            data, status = self._get_json(url, timeout)

            if page == 1 and prefix == "groups" and status == 404:
                prefix = "users"
                continue

            if status >= 400 or not isinstance(data, list):
                if page == 1:
                    detail = ""
                    if isinstance(data, dict):
                        detail = data.get("message") or data.get("error") or ""
                    raise GitAccessError(
                        f"{api_base.replace('/api/v4', '')}/{group_path}",
                        f"Cannot list repos for '{group_path}' (HTTP {status}). {detail}".strip(),
                        kind=GitErrorKind.REPO_NOT_FOUND
                        if status == 404
                        else GitErrorKind.NETWORK_ERROR,
                    )
                break

            if not data:
                break

            for p in data:
                if not isinstance(p, dict):
                    continue
                repos.append(
                    {
                        "name": p.get("path", p.get("name", "")),
                        "url": p.get("web_url", ""),
                        "default_branch": p.get("default_branch", "main"),
                        "description": p.get("description") or "",
                        "visibility": p.get("visibility", ""),
                    }
                )

            if len(data) < 100:
                break
            page += 1

        return repos

    def list_remote_branches(self, repo_url: str, timeout: int = 30) -> list[str]:
        """List branch names for a GitLab project."""
        parsed = parse_gitlab_url(repo_url)
        if not parsed:
            return []
        host, namespace, project = parsed
        encoded = urllib.parse.quote(f"{namespace}/{project}", safe="")
        api_base = self._api_base(host)

        branches: list[str] = []
        page = 1
        while True:
            url = f"{api_base}/projects/{encoded}/repository/branches?per_page=100&page={page}"
            data, status = self._get_json(url, timeout)
            if status >= 400 or not isinstance(data, list):
                if page == 1:
                    self._raise_for_status(repo_url, status, data)
                break
            if not data:
                break
            branches.extend(b.get("name", "") for b in data if isinstance(b, dict))
            if len(data) < 100:
                break
            page += 1
        return branches

    def get_branch_info(
        self, repo_url: str, branch_name: str, timeout: int = 30
    ) -> BranchInfo | None:
        """Get metadata for a specific branch via GitLab API."""
        parsed = parse_gitlab_url(repo_url)
        if not parsed:
            return BranchInfo(name=branch_name, exists=False)

        host, namespace, project = parsed
        encoded_project = urllib.parse.quote(f"{namespace}/{project}", safe="")
        encoded_branch = urllib.parse.quote(branch_name, safe="")
        api_base = self._api_base(host)

        url = f"{api_base}/projects/{encoded_project}/repository/branches/{encoded_branch}"
        data, status = self._get_json(url, timeout)

        if status == 404:
            # Branch genuinely not found — not an auth/access error
            return BranchInfo(name=branch_name, exists=False, data_source="gitlab_api")

        if status >= 400 or not isinstance(data, dict):
            # Auth, access, or server error — raise so callers can distinguish
            self._raise_for_status(repo_url, status, data)

        # Extract commit info
        commit = data.get("commit") if isinstance(data.get("commit"), dict) else {}
        commit_date = None
        if commit.get("committed_date"):
            try:
                from datetime import datetime

                commit_date = datetime.fromisoformat(
                    commit["committed_date"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        return BranchInfo(
            name=branch_name,
            exists=True,
            last_commit_date=commit_date,
            last_commit_author=commit.get("author_name"),
            last_commit_message=commit.get("message", "")[:120] if commit.get("message") else None,
            last_commit_sha=commit.get("id"),
            data_source="gitlab_api",
        )

    def get_default_branch_info(self, repo_url: str, timeout: int = 30) -> BranchInfo | None:
        """Get metadata for the repository's default branch via GitLab API."""
        parsed = parse_gitlab_url(repo_url)
        if not parsed:
            return None

        host, namespace, project = parsed
        encoded_project = urllib.parse.quote(f"{namespace}/{project}", safe="")
        api_base = self._api_base(host)

        # Get project info to find default branch
        project_url = f"{api_base}/projects/{encoded_project}"
        data, status = self._get_json(project_url, timeout)
        if status >= 400 or not isinstance(data, dict):
            self._raise_for_status(repo_url, status, data)

        default_branch = data.get("default_branch", "main")
        repo_visibility = data.get("visibility", "")
        repo_description = data.get("description") or ""
        repo_web_url = data.get("web_url", "")

        branch_info = self.get_branch_info(repo_url, default_branch, timeout)
        if branch_info and branch_info.exists:
            return BranchInfo(
                name=branch_info.name,
                exists=True,
                last_commit_date=branch_info.last_commit_date,
                last_commit_author=branch_info.last_commit_author,
                last_commit_message=branch_info.last_commit_message,
                last_commit_sha=branch_info.last_commit_sha,
                repo_default_branch=default_branch,
                repo_visibility=repo_visibility,
                repo_description=repo_description,
                repo_web_url=repo_web_url,
                data_source="gitlab_api",
            )
        return branch_info

    # ------------------------------------------------------------------
    # Tag enrichment — latest tag relevant to a specific branch
    # ------------------------------------------------------------------

    def get_latest_branch_tag(
        self, repo_url: str, branch_name: str, timeout: int = 30
    ) -> TagInfo | None:
        """Return the latest tag whose target commit is reachable from *branch_name*.

        Performance: fetches 1 page of 10 tags (newest first) and checks
        branch reachability for up to 5 candidates.  Uses a shorter timeout
        for the refs check (non-critical enrichment).  Stops on first match.

        Returns ``None`` when no matching tag is found or the API is
        unreachable.
        """
        parsed = parse_gitlab_url(repo_url)
        if not parsed:
            return None

        host, namespace, project = parsed
        encoded_project = urllib.parse.quote(f"{namespace}/{project}", safe="")
        api_base = self._api_base(host)

        # Fetch only 1 page of 10 tags — enough for most projects
        url = (
            f"{api_base}/projects/{encoded_project}"
            f"/repository/tags?order_by=updated&sort=desc"
            f"&per_page=10&page=1"
        )
        tags_data, status = self._get_json(url, timeout)
        if status >= 400 or not isinstance(tags_data, list) or not tags_data:
            if not tags_data:
                logger.debug("No tags found for %s/%s", namespace, project)
            return None

        # Use a shorter timeout for refs checks (non-critical enrichment)
        refs_timeout = min(timeout, 8)
        max_checks = 5  # Stop after checking 5 tags

        checked = 0
        for tag in tags_data:
            if not isinstance(tag, dict):
                continue
            tag_name = tag.get("name", "")
            commit_obj = tag.get("commit") if isinstance(tag.get("commit"), dict) else {}
            target_sha = commit_obj.get("id") or tag.get("target", "")
            if not target_sha:
                continue

            checked += 1
            if checked > max_checks:
                break

            # Ask GitLab which branches contain this commit
            refs_url = (
                f"{api_base}/projects/{encoded_project}"
                f"/repository/commits/{target_sha}/refs?type=branch"
            )
            refs_data, refs_status = self._get_json(refs_url, refs_timeout)
            if refs_status >= 400 or not isinstance(refs_data, list):
                logger.debug(
                    "Cannot check refs for tag %s (SHA %s): HTTP %d",
                    tag_name,
                    target_sha,
                    refs_status,
                )
                continue

            branch_names = {
                ref.get("name", "")
                for ref in refs_data
                if isinstance(ref, dict) and ref.get("type") == "branch"
            }
            if branch_name in branch_names:
                committed_date = None
                raw_date = commit_obj.get("committed_date") or commit_obj.get("created_at")
                if raw_date:
                    try:
                        from datetime import datetime

                        committed_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

                logger.info(
                    "Latest tag for %s/%s branch '%s': %s (%s)",
                    namespace,
                    project,
                    branch_name,
                    tag_name,
                    target_sha[:8],
                )
                return TagInfo(
                    name=tag_name,
                    target_sha=target_sha,
                    committed_date=committed_date,
                    message=(tag.get("message") or "").strip() or None,
                )

        logger.debug(
            "No tags reachable from branch '%s' in %s/%s (checked %d tags)",
            branch_name,
            namespace,
            project,
            checked,
        )
        return None

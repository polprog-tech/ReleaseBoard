"""ReleasePilot adapter — thin bridge between ReleaseBoard and ReleasePilot.

All release-notes generation logic lives in the ReleasePilot library.
This adapter only:
  1. Translates ReleaseBoard models → ReleasePilot Settings
  2. Clones remote repositories to a temporary directory when needed
  3. Calls ``releasepilot.pipeline.orchestrator.generate()``
  4. Wraps the result into ``ReleasePrepResult`` for the wizard

No rendering, no classification, no git collection is duplicated here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

try:
    from releasepilot import __version__ as rp_version
    from releasepilot.config.settings import RenderConfig, Settings
    from releasepilot.domain.enums import Audience
    from releasepilot.domain.enums import OutputFormat as RPOutputFormat
    from releasepilot.pipeline.orchestrator import generate

    _RELEASEPILOT_AVAILABLE = True
except ImportError:
    _RELEASEPILOT_AVAILABLE = False
    rp_version = "0.0.0"
    RenderConfig = None  # type: ignore[assignment,misc]
    Settings = None  # type: ignore[assignment,misc]
    Audience = None  # type: ignore[assignment,misc]
    RPOutputFormat = None  # type: ignore[assignment,misc]
    generate = None  # type: ignore[assignment,misc]

from releaseboard.integrations.releasepilot.models import (
    SUPPORTED_LANGUAGES,
    ReleasePrepRequest,
    ReleasePrepResult,
)
from releaseboard.integrations.releasepilot.validation import validate_prep_request

logger = logging.getLogger(__name__)

_CLONE_TIMEOUT = 120  # seconds for git clone


def _is_remote_url(url: str) -> bool:
    """Return True if *url* looks like a remote git URL (not a local path)."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https", "ssh", "git")


def _auth_clone_url(repo_url: str, token: str) -> str:
    """Embed *token* into the clone URL for authenticated HTTPS access.

    GitHub:  https://<token>@github.com/owner/repo
    GitLab:  https://oauth2:<token>@gitlab.example.com/group/repo
    """
    if not token:
        return repo_url
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("http", "https"):
        return repo_url
    host = parsed.hostname or ""
    if "github" in host:
        netloc = f"{token}@{parsed.hostname}"
    else:
        netloc = f"oauth2:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def _shallow_clone(
    repo_url: str,
    dest: str,
    *,
    token: str = "",
    branch: str = "",
) -> str:
    """Shallow-clone a remote repository into *dest* and return the clone path.

    Uses ``--filter=blob:none`` for a treeless clone (fast, keeps full commit
    history for log/tag traversal but skips file content until checked out).
    Falls back to ``--depth 200`` if the server doesn't support partial clone.
    """
    clone_url = _auth_clone_url(repo_url, token)
    clone_dir = os.path.join(dest, "repo")

    cmd_base = ["git", "clone", "--quiet", "--no-checkout"]
    if branch:
        cmd_base += ["--branch", branch]
    cmd_base += ["--filter=blob:none", clone_url, clone_dir]

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    try:
        result = subprocess.run(
            cmd_base,
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            return clone_dir

        stderr = result.stderr.strip()
        # Filter not supported → fallback to depth-limited clone
        if "filter" in stderr.lower() or "unknown option" in stderr.lower():
            logger.debug("Partial clone not supported, falling back to --depth 200")
            cmd_fallback = ["git", "clone", "--quiet", "--no-checkout", "--depth", "200"]
            if branch:
                cmd_fallback += ["--branch", branch]
            cmd_fallback += [clone_url, clone_dir]
            # Remove partial clone dir if it was created
            if os.path.isdir(clone_dir):
                shutil.rmtree(clone_dir, ignore_errors=True)
            subprocess.run(
                cmd_fallback,
                capture_output=True,
                text=True,
                timeout=_CLONE_TIMEOUT,
                check=True,
                env=env,
            )
            return clone_dir

        # Classify the error for a clear user-facing message
        _raise_clone_error(stderr, repo_url)

    except subprocess.TimeoutExpired as exc:
        raise _CloneError(
            f"Clone timed out after {_CLONE_TIMEOUT}s — the repository may be very "
            f"large or the network is slow: {repo_url}",
            error_code="clone_timeout",
        ) from exc
    except subprocess.CalledProcessError as exc:
        _raise_clone_error(exc.stderr.strip() if exc.stderr else str(exc), repo_url)

    return clone_dir  # unreachable, but keeps mypy happy


class _CloneError(Exception):
    """Internal error with a user-facing message and error code."""

    def __init__(self, message: str, error_code: str = "clone_failed") -> None:
        super().__init__(message)
        self.error_code = error_code


def _raise_clone_error(stderr: str, repo_url: str) -> None:
    """Classify git clone stderr and raise a descriptive ``_CloneError``."""
    low = stderr.lower()
    if "authentication" in low or "401" in low or "could not read" in low:
        raise _CloneError(
            f"Authentication failed for {repo_url} — check your token/credentials.",
            error_code="auth_failed",
        )
    if "403" in low or "forbidden" in low:
        raise _CloneError(
            f"Access denied to {repo_url} — the token may lack permissions.",
            error_code="access_denied",
        )
    if "not found" in low or "404" in low or "does not exist" in low:
        raise _CloneError(
            f"Repository not found: {repo_url}",
            error_code="repo_not_found",
        )
    if "could not resolve" in low or "name or service" in low:
        raise _CloneError(
            f"Cannot resolve host for {repo_url} — check the URL or network.",
            error_code="network_error",
        )
    if "ssl" in low or "certificate" in low:
        raise _CloneError(
            f"SSL/TLS error connecting to {repo_url} — possible proxy or cert issue.",
            error_code="ssl_error",
        )
    raise _CloneError(
        f"git clone failed for {repo_url}: {stderr}",
        error_code="clone_failed",
    )


@dataclass
class ReleasePilotCapabilities:
    """Describes what the integration can do in the current environment."""

    available: bool
    mode: str  # always "library" now
    version: str
    supported_audiences: tuple[str, ...]
    supported_formats: tuple[str, ...]
    export_formats_available: bool = True
    supported_languages: tuple[tuple[str, str], ...] = SUPPORTED_LANGUAGES

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "mode": self.mode,
            "version": self.version,
            "supported_audiences": list(self.supported_audiences),
            "supported_formats": list(self.supported_formats),
            "export_formats_available": self.export_formats_available,
            "supported_languages": [
                {"code": code, "label": label}
                for code, label in self.supported_languages
            ],
        }


def _detect_capabilities() -> ReleasePilotCapabilities:
    """Detect ReleasePilot capabilities from the installed library."""
    if not _RELEASEPILOT_AVAILABLE:
        return ReleasePilotCapabilities(
            available=False,
            mode="not_installed",
            version="0.0.0",
            supported_audiences=(),
            supported_formats=(),
            export_formats_available=False,
        )
    audiences = tuple(a.value for a in Audience)
    formats = tuple(f.value for f in RPOutputFormat)
    return ReleasePilotCapabilities(
        available=True,
        mode="library",
        version=rp_version,
        supported_audiences=audiences,
        supported_formats=formats,
        export_formats_available=True,
    )


def _request_to_settings(
    request: ReleasePrepRequest,
    *,
    repo_path: str = "",
) -> Settings:
    """Convert a ReleaseBoard prep request into ReleasePilot Settings.

    Parameters
    ----------
    repo_path:
        Override for the repository path.  When the original ``repo_url`` is
        a remote URL, the caller clones it first and passes the local clone
        path here.  Falls back to ``request.repo_url`` (works for local paths).
    """
    render = RenderConfig(
        show_authors=request.include_authors,
        show_commit_hashes=request.include_hashes,
        show_pr_links=request.show_pr_links,
        show_scope=request.show_scope,
        group_by_scope=request.group_by_scope,
        language=request.language,
        accent_color=request.accent_color,
    )
    return Settings(
        repo_path=repo_path or request.repo_url,
        from_ref=request.from_ref,
        to_ref=request.to_ref,
        branch=request.branch,
        since_date=request.since_date,
        audience=Audience(str(request.audience)),
        output_format=RPOutputFormat(str(request.output_format)),
        version=request.release_version,
        title=request.release_title,
        app_name=request.app_name or request.repo_name,
        language=request.language,
        render=render,
    )


class ReleasePilotAdapter:
    """Service adapter for ReleasePilot integration.

    Thread-safe.  One instance can be shared across requests.
    """

    def __init__(self) -> None:
        self._capabilities: ReleasePilotCapabilities | None = None

    @property
    def is_available(self) -> bool:
        """Return True if ReleasePilot is installed and usable."""
        return self.capabilities.available

    @property
    def capabilities(self) -> ReleasePilotCapabilities:
        if self._capabilities is None:
            self._capabilities = _detect_capabilities()
        return self._capabilities

    def validate(self, data: dict[str, Any]) -> list[str]:
        return validate_prep_request(data)

    async def prepare_release(self, request: ReleasePrepRequest) -> ReleasePrepResult:
        """Execute a release preparation run via ReleasePilot library.

        If ``request.repo_url`` is a remote URL (HTTPS/SSH), the repository is
        shallow-cloned to a temporary directory first.  The clone is cleaned up
        automatically after generation finishes.
        """
        if not self.is_available:
            return ReleasePrepResult(
                success=False,
                repo_name=request.repo_name,
                release_title=request.release_title,
                release_version=request.release_version,
                audience=str(request.audience),
                output_format=str(request.output_format),
                error_message="ReleasePilot is not installed",
                error_code="integration_unavailable",
            )
        try:
            if _is_remote_url(request.repo_url):
                return await self._prepare_from_remote(request)
            return await self._generate(request, repo_path=request.repo_url)
        except _CloneError as exc:
            logger.error("Clone failed for %s: %s", request.repo_name, exc)
            return ReleasePrepResult(
                success=False,
                repo_name=request.repo_name,
                release_title=request.release_title,
                release_version=request.release_version,
                audience=str(request.audience),
                output_format=str(request.output_format),
                error_message=str(exc),
                error_code=exc.error_code,
            )
        except Exception as exc:
            logger.error("Release preparation failed for %s: %s", request.repo_name, exc)
            return ReleasePrepResult(
                success=False,
                repo_name=request.repo_name,
                release_title=request.release_title,
                release_version=request.release_version,
                audience=str(request.audience),
                output_format=str(request.output_format),
                error_message=str(exc),
                error_code="preparation_failed",
            )

    async def _prepare_from_remote(self, request: ReleasePrepRequest) -> ReleasePrepResult:
        """Clone a remote repo to a temp dir, generate, then clean up."""
        tmpdir = tempfile.mkdtemp(prefix="rb_rp_")
        try:
            clone_path = await asyncio.to_thread(
                _shallow_clone,
                request.repo_url,
                tmpdir,
                token=request.git_token,
                branch=request.branch,
            )
            return await self._generate(request, repo_path=clone_path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def _generate(
        self,
        request: ReleasePrepRequest,
        *,
        repo_path: str,
    ) -> ReleasePrepResult:
        """Run the ReleasePilot pipeline and wrap the result."""
        settings = _request_to_settings(request, repo_path=repo_path)
        content = await asyncio.to_thread(generate, settings)

        total = content.count("\n- ") if content else 0

        return ReleasePrepResult(
            success=True,
            repo_name=request.repo_name,
            release_title=request.release_title,
            release_version=request.release_version,
            audience=str(request.audience),
            output_format=str(request.output_format),
            content=content,
            total_changes=total,
            metadata={"mode": "library", "version": rp_version},
        )


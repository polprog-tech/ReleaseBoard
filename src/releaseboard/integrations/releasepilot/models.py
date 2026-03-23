"""Data models for the ReleasePilot integration.

Thin wrappers around ReleasePilot's own domain types, plus ReleaseBoard-
specific request/result models.  All release-notes logic lives in the
ReleasePilot library — these models only add UI-integration helpers
(i18n label keys) and the request/result envelope for the wizard flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

try:
    from releasepilot.domain.enums import Audience
    from releasepilot.domain.enums import OutputFormat as RPOutputFormat
    _RP_ENUMS_AVAILABLE = True
except ImportError:
    _RP_ENUMS_AVAILABLE = False
    Audience = None  # type: ignore[assignment,misc]
    RPOutputFormat = None  # type: ignore[assignment,misc]

# ── Re-export ReleasePilot enums with UI helpers ────────────────────────────


class AudienceMode(str):
    """Thin wrapper around ``releasepilot.domain.enums.Audience``.

    Behaves like a plain string (the enum *value*) so it can be used in JSON
    payloads directly.  Provides ``label_key`` for i18n lookup in the wizard.
    """

    _enum = Audience

    def __new__(cls, value: str) -> AudienceMode:
        if _RP_ENUMS_AVAILABLE and Audience is not None:
            Audience(value)  # validate against real enum
        return super().__new__(cls, value)

    @property
    def label_key(self) -> str:
        safe = self.replace("-", "_")
        return f"rp.audience.{safe}"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        if _RP_ENUMS_AVAILABLE and Audience is not None:
            return tuple(a.value for a in Audience)
        return ()


class OutputFormat(str):
    """Thin wrapper around ``releasepilot.domain.enums.OutputFormat``."""

    _enum = RPOutputFormat

    def __new__(cls, value: str) -> OutputFormat:
        if _RP_ENUMS_AVAILABLE and RPOutputFormat is not None:
            RPOutputFormat(value)  # validate against real enum
        return super().__new__(cls, value)

    @property
    def label_key(self) -> str:
        return f"rp.format.{self}"

    @property
    def requires_export_deps(self) -> bool:
        return self in ("pdf", "docx")

    @classmethod
    def values(cls) -> tuple[str, ...]:
        if _RP_ENUMS_AVAILABLE and RPOutputFormat is not None:
            return tuple(f.value for f in RPOutputFormat)
        return ()


# Languages supported by ReleasePilot (code, native label).
SUPPORTED_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("pl", "Polski"),
    ("de", "Deutsch"),
    ("fr", "Français"),
    ("es", "Español"),
    ("it", "Italiano"),
    ("pt", "Português"),
    ("nl", "Nederlands"),
    ("uk", "Українська"),
    ("cs", "Čeština"),
)


# ── ReleaseBoard-specific models ────────────────────────────────────────────


@dataclass(frozen=True)
class RepoContext:
    """Repository context passed from ReleaseBoard into the wizard."""

    name: str
    url: str
    layer: str
    layer_label: str
    default_branch: str = "main"
    expected_branch: str = ""
    actual_branch: str = ""
    branch_exists: bool = False
    repo_description: str = ""
    repo_visibility: str = ""
    repo_web_url: str = ""
    repo_owner: str = ""
    release_name: str = ""
    release_month: int = 0
    release_year: int = 0
    branch_pattern: str = ""


@dataclass(frozen=True)
class ReleasePrepRequest:
    """Request payload for a release preparation run.

    Maps 1-to-1 onto ``releasepilot.config.settings.Settings`` so the full
    capability set is exposed without any translation gap.
    """

    repo_name: str
    repo_url: str
    release_title: str
    release_version: str
    from_ref: str = ""
    to_ref: str = "HEAD"
    audience: AudienceMode = AudienceMode("changelog")
    output_format: OutputFormat = OutputFormat("markdown")
    app_name: str = ""
    # Render options (forwarded to ReleasePilot RenderConfig)
    include_authors: bool = True
    include_hashes: bool = False
    show_scope: bool = True
    show_pr_links: bool = True
    group_by_scope: bool = False
    language: str = "en"
    accent_color: str = "#FB6400"
    # Source options
    branch: str = ""
    since_date: str = ""
    additional_notes: str = ""
    # Authentication (optional — injected by server from git provider)
    git_token: str = ""


@dataclass(frozen=True)
class ReleasePrepResult:
    """Result of a release preparation run."""

    success: bool
    repo_name: str
    release_title: str
    release_version: str
    audience: str
    output_format: str
    content: str = ""
    total_changes: int = 0
    highlights: tuple[str, ...] = ()
    breaking_changes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error_message: str = ""
    error_code: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "repo_name": self.repo_name,
            "release_title": self.release_title,
            "release_version": self.release_version,
            "audience": self.audience,
            "output_format": self.output_format,
            "content": self.content,
            "total_changes": self.total_changes,
            "highlights": list(self.highlights),
            "breaking_changes": list(self.breaking_changes),
            "warnings": list(self.warnings),
            "error_message": self.error_message,
            "error_code": self.error_code,
            "generated_at": self.generated_at.isoformat(),
            "metadata": dict(self.metadata),
        }

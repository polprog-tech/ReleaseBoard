"""View models — presentation-layer data structures for the HTML template."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, Any

from releaseboard.analysis.metrics import DashboardMetrics, LayerMetrics
from releaseboard.analysis.staleness import freshness_label
from releaseboard.domain.enums import ReadinessStatus
from releaseboard.git.gitlab_provider import is_gitlab_url
from releaseboard.i18n import default_locale, get_catalog, supported_locales
from releaseboard.presentation.theme import CHART_COLORS, STATUS_COLORS
from releaseboard.shared.logging import get_logger

if TYPE_CHECKING:
    from releaseboard.config.models import AppConfig
    from releaseboard.domain.models import RepositoryAnalysis

logger = get_logger("presentation.view_models")


# Locale-aware date formats: Polish uses day.month.year
_DATE_FORMATS: dict[str, str] = {
    "en": "%Y-%m-%d %H:%M",
    "pl": "%d.%m.%Y %H:%M",
}

_GENERATED_AT_FORMATS: dict[str, str] = {
    "en": "%Y-%m-%d %H:%M:%S",
    "pl": "%d.%m.%Y %H:%M:%S",
}


def _format_datetime(dt: datetime | None, locale: str | None = None) -> str:
    """Format a datetime using locale-appropriate pattern."""
    if dt is None:
        return "—"
    loc = locale or default_locale()
    fmt = _DATE_FORMATS.get(loc, _DATE_FORMATS["en"])
    return dt.strftime(fmt)


def _get_version() -> str:
    """Get installed package version, with graceful fallback."""
    try:
        return pkg_version("releaseboard")
    except Exception:
        return "dev"


def _get_rp_version() -> str:
    """Get installed ReleasePilot version, with graceful fallback."""
    try:
        from releaseboard.integrations.releasepilot.adapter import _detect_capabilities
        return _detect_capabilities().version
    except Exception:
        return "0.0.0"


@dataclass
class RepoViewModel:
    """Presentation model for a single repository."""

    name: str
    url: str
    layer: str
    layer_label: str
    status: str
    status_label: str
    status_color_bg: str
    status_color_fg: str
    status_badge_bg: str
    status_badge_fg: str
    expected_branch: str
    actual_branch: str
    naming_valid: bool
    is_stale: bool
    last_activity: str
    last_activity_raw: str
    first_activity: str
    last_author: str
    last_message: str
    commit_count: str
    freshness: str
    warnings: list[str]
    notes: list[str]
    error_message: str
    error_kind: str
    error_detail: str
    branch_exists: bool
    status_severity: int = 99
    # Provider metadata (available even when release branch is missing)
    repo_default_branch: str = ""
    repo_visibility: str = ""
    repo_description: str = ""
    repo_web_url: str = ""
    repo_owner: str = ""
    data_source: str = ""
    # GitLab tag enrichment
    latest_tag: str = ""
    latest_tag_sha: str = ""
    latest_tag_date: str = ""
    latest_tag_message: str = ""
    is_gitlab: bool = False


@dataclass
class LayerViewModel:
    """Presentation model for a layer section."""

    id: str
    label: str
    color: str
    total: int
    ready: int
    readiness_pct: float
    problem_count: int
    repos: list[RepoViewModel]
    metrics: LayerMetrics


@dataclass
class ChartData:
    """Data for a single chart."""

    labels: list[str]
    values: list[int | float]
    colors: list[str]


@dataclass
class DashboardViewModel:
    """Complete view model for the HTML dashboard."""

    title: str
    subtitle: str
    company: str
    primary_color: str
    secondary_color: str
    tertiary_color: str
    theme: str
    release_name: str
    generated_at: str
    metrics: DashboardMetrics
    layers: list[LayerViewModel]
    attention_items: list[RepoViewModel]
    all_repos: list[RepoViewModel]
    status_chart: ChartData
    layer_readiness_chart: ChartData
    layer_colors: dict[str, str] = field(default_factory=dict)
    interactive: bool = False
    release_month: int = 1
    release_year: int = 2025
    config_json: str = "{}"
    version: str = ""
    rp_version: str = ""
    author_name: str = ""
    author_role: str = ""
    author_url: str = ""
    author_tagline: str = ""
    author_copyright: str = ""
    locale: str = "en"
    translations_json: str = "{}"
    supported_locales: list[dict[str, str]] = field(default_factory=list)


def build_repo_view_model(
    analysis: RepositoryAnalysis,
    layer_label: str,
    stale_threshold: int,
    locale: str | None = None,
) -> RepoViewModel:
    """Convert a domain RepositoryAnalysis to a RepoViewModel."""
    colors = STATUS_COLORS.get(analysis.status, STATUS_COLORS[ReadinessStatus.UNKNOWN])

    actual_branch = ""
    last_author = ""
    last_message = ""
    commit_count = "—"
    if analysis.branch and analysis.branch.exists:
        actual_branch = analysis.branch.name
        last_author = analysis.branch.last_commit_author or ""
        last_message = analysis.branch.last_commit_message or ""
        if analysis.branch.commit_count is not None:
            commit_count = str(analysis.branch.commit_count)

    last_activity_raw = ""
    last_activity_display = "—"
    if analysis.last_activity:
        last_activity_raw = analysis.last_activity.isoformat()
        last_activity_display = _format_datetime(analysis.last_activity, locale)

    first_activity_display = "—"
    if analysis.first_activity:
        first_activity_display = _format_datetime(analysis.first_activity, locale)

    freshness = freshness_label(analysis.last_activity, stale_threshold, locale=locale)

    # Provider-level repo metadata (available even when release branch is missing)
    repo_default_branch = ""
    repo_visibility = ""
    repo_description = ""
    repo_web_url = ""
    repo_owner = ""
    data_source = ""
    if analysis.branch:
        repo_default_branch = analysis.branch.repo_default_branch or ""
        repo_visibility = analysis.branch.repo_visibility or ""
        repo_description = analysis.branch.repo_description or ""
        repo_web_url = analysis.branch.repo_web_url or ""
        repo_owner = analysis.branch.repo_owner or ""
        data_source = analysis.branch.data_source or ""

    # GitLab tag enrichment
    is_gitlab = is_gitlab_url(analysis.url)
    latest_tag = ""
    latest_tag_sha = ""
    latest_tag_date = ""
    latest_tag_message = ""
    if analysis.latest_tag:
        latest_tag = analysis.latest_tag.name
        latest_tag_sha = analysis.latest_tag.target_sha or ""
        latest_tag_message = analysis.latest_tag.message or ""
        if analysis.latest_tag.committed_date:
            latest_tag_date = _format_datetime(analysis.latest_tag.committed_date, locale)

    return RepoViewModel(
        name=analysis.name,
        url=analysis.url,
        layer=analysis.layer,
        layer_label=layer_label,
        status=analysis.status.value,
        status_label=analysis.status.localized_label(locale),
        status_color_bg=colors["bg"],
        status_color_fg=colors["fg"],
        status_badge_bg=colors["light_bg"],
        status_badge_fg=colors["light_fg"],
        expected_branch=analysis.expected_branch_name,
        actual_branch=actual_branch,
        naming_valid=analysis.naming_valid,
        is_stale=analysis.is_stale,
        last_activity=last_activity_display,
        last_activity_raw=last_activity_raw,
        first_activity=first_activity_display,
        last_author=last_author,
        last_message=last_message,
        commit_count=commit_count,
        freshness=freshness,
        warnings=list(analysis.warnings),
        notes=list(analysis.notes),
        error_message=analysis.error_message or "",
        error_kind=analysis.error_kind or "",
        error_detail=analysis.error_detail or "",
        branch_exists=analysis.branch_exists,
        status_severity=analysis.status.severity,
        repo_default_branch=repo_default_branch,
        repo_visibility=repo_visibility,
        repo_description=repo_description,
        repo_web_url=repo_web_url,
        repo_owner=repo_owner,
        data_source=data_source,
        latest_tag=latest_tag,
        latest_tag_sha=latest_tag_sha,
        latest_tag_date=latest_tag_date,
        latest_tag_message=latest_tag_message,
        is_gitlab=is_gitlab,
    )


def build_dashboard_view_model(
    config: AppConfig,
    analyses: list[RepositoryAnalysis],
    metrics: DashboardMetrics,
    locale: str | None = None,
    config_raw: dict[str, Any] | None = None,
) -> DashboardViewModel:
    """Build the complete dashboard view model from analysis results.

    Args:
        config_raw: Optional raw config dict. When provided, its JSON
            representation is embedded as ``config_json`` so that
            client-side scripts (e.g. the milestone timeline) can
            read release_calendar data even in static HTML exports.
    """
    active_locale = locale or default_locale()
    layer_labels = {layer.id: layer.label for layer in config.layers}
    layer_colors = {
        layer.id: layer.color or config.branding.primary_color
        for layer in config.layers
    }

    # Build per-repo view models
    all_repos: list[RepoViewModel] = []
    for a in analyses:
        label = layer_labels.get(a.layer, a.layer)
        vm = build_repo_view_model(
            a, label, config.settings.stale_threshold_days, locale=active_locale,
        )
        all_repos.append(vm)

    # Sort: problems first (by severity), then alphabetically
    all_repos.sort(key=lambda r: (
        ReadinessStatus(r.status).severity,
        r.layer,
        r.name,
    ))

    # Build layer view models
    layers: list[LayerViewModel] = []
    for layer_config in sorted(config.layers, key=lambda layer: layer.order):
        lid = layer_config.id
        layer_repos = [r for r in all_repos if r.layer == lid]
        lm = metrics.layer_metrics.get(
            lid,
            LayerMetrics(
                layer_id=lid, layer_label=layer_config.label,
            ),
        )
        layers.append(LayerViewModel(
            id=lid,
            label=layer_config.label,
            color=layer_config.color or config.branding.primary_color,
            total=lm.total,
            ready=lm.ready,
            readiness_pct=lm.readiness_pct,
            problem_count=lm.problem_count,
            repos=layer_repos,
            metrics=lm,
        ))

    # Attention items
    attention = [r for r in all_repos if ReadinessStatus(r.status).is_problem]

    # Status distribution chart
    status_chart = _build_status_chart(metrics, active_locale)

    # Layer readiness chart
    layer_readiness_chart = _build_layer_readiness_chart(layers)

    catalog = get_catalog(active_locale)
    # Build supported locales list for the language switcher
    locale_list = []
    for loc in supported_locales():
        loc_catalog = get_catalog(loc)
        meta = loc_catalog.get("_meta", {}) if isinstance(loc_catalog.get("_meta"), dict) else {}
        locale_list.append({
            "code": loc,
            "name": meta.get("name", loc.upper()),
            "direction": meta.get("direction", "ltr"),
        })

    # Embed raw config JSON so client-side scripts can access release_calendar
    embedded_config_json = "{}"
    if config_raw and isinstance(config_raw, dict):
        try:
            embedded_config_json = json.dumps(config_raw, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("Failed to serialize config for template embedding: %s", exc)
            embedded_config_json = "{}"

    return DashboardViewModel(
        title=config.branding.title,
        subtitle=config.branding.subtitle,
        company=config.branding.company,
        primary_color=config.branding.primary_color,
        secondary_color=config.branding.secondary_color,
        tertiary_color=config.branding.tertiary_color,
        theme=config.settings.theme,
        release_name=config.release.name,
        generated_at=datetime.now(tz=UTC).strftime(
            _GENERATED_AT_FORMATS.get(active_locale, _GENERATED_AT_FORMATS["en"])
        ),
        metrics=metrics,
        layers=layers,
        attention_items=attention[:10],
        all_repos=all_repos,
        status_chart=status_chart,
        layer_readiness_chart=layer_readiness_chart,
        layer_colors=layer_colors,
        release_month=config.release.target_month,
        release_year=config.release.target_year,
        version=_get_version(),
        rp_version=_get_rp_version(),
        author_name=config.author.name,
        author_role=config.author.role,
        author_url=config.author.url,
        author_tagline=config.author.tagline,
        author_copyright=config.author.copyright,
        locale=active_locale,
        translations_json=json.dumps(catalog, ensure_ascii=False),
        supported_locales=locale_list,
        config_json=embedded_config_json,
    )


def _build_status_chart(metrics: DashboardMetrics, locale: str | None = None) -> ChartData:
    """Build chart data for status distribution."""
    statuses = [
        ReadinessStatus.READY,
        ReadinessStatus.MISSING_BRANCH,
        ReadinessStatus.INVALID_NAMING,
        ReadinessStatus.STALE,
        ReadinessStatus.WARNING,
        ReadinessStatus.INACTIVE,
        ReadinessStatus.ERROR,
        ReadinessStatus.UNKNOWN,
    ]
    labels: list[str] = []
    values: list[int] = []
    colors: list[str] = []
    for s in statuses:
        count = metrics.status_counts.get(s.value, 0)
        if count > 0:
            labels.append(s.localized_label(locale))
            values.append(count)
            colors.append(CHART_COLORS[s])
    return ChartData(labels=labels, values=values, colors=colors)


def _build_layer_readiness_chart(layers: list[LayerViewModel]) -> ChartData:
    """Build chart data for per-layer readiness percentage."""
    return ChartData(
        labels=[layer.label for layer in layers],
        values=[round(layer.readiness_pct, 1) for layer in layers],
        colors=[layer.color for layer in layers],
    )

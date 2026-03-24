"""Application service — orchestrates analysis pipeline.

Shared by CLI and web. Neither should duplicate business logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from releaseboard.analysis.branch_pattern import BranchPatternMatcher
from releaseboard.analysis.metrics import DashboardMetrics, compute_dashboard_metrics
from releaseboard.analysis.readiness import ReadinessAnalyzer
from releaseboard.git.gitlab_provider import GitLabProvider, is_gitlab_url
from releaseboard.git.provider import GitAccessError, GitErrorKind, GitProvider, is_placeholder_url
from releaseboard.git.smart_provider import SmartGitProvider
from releaseboard.shared.logging import get_logger

if TYPE_CHECKING:
    from releaseboard.config.models import AppConfig
    from releaseboard.domain.models import RepositoryAnalysis

logger = get_logger("service")


class AnalysisPhase(StrEnum):
    """Front-end–safe analysis state model."""

    IDLE = "idle"
    QUEUED = "queued"
    STARTING = "starting"
    ANALYZING = "analyzing"
    COMPLETING = "completing"
    STOPPING = "stopping"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL_FAILURE = "partial_failure"


@dataclass
class RepoProgress:
    """Progress for a single repository analysis."""

    name: str
    status: str = "pending"  # pending | analyzing | done | error | skipped
    readiness: str | None = None
    error: str | None = None
    elapsed_ms: float = 0


@dataclass
class AnalysisProgress:
    """Real-time analysis progress state."""

    phase: AnalysisPhase = AnalysisPhase.IDLE
    total: int = 0
    completed: int = 0
    current_repo: str | None = None
    repos: list[RepoProgress] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None
    error_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.monotonic()
        return round(end - self.started_at, 1)

    @property
    def progress_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(self.completed / self.total * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "total": self.total,
            "completed": self.completed,
            "current_repo": self.current_repo,
            "elapsed_seconds": self.elapsed_seconds,
            "progress_pct": self.progress_pct,
            "error_count": self.error_count,
            "warnings": self.warnings,
            "repos": [
                {
                    "name": r.name,
                    "status": r.status,
                    "readiness": r.readiness,
                    "error": r.error,
                }
                for r in self.repos
            ],
        }


# Type for progress callback: called with (event_type, progress_snapshot)
ProgressCallback = Callable[[str, AnalysisProgress], Any]


@dataclass
class AnalysisResult:
    """Complete result of an analysis run."""

    config: AppConfig
    analyses: list[RepositoryAnalysis]
    metrics: DashboardMetrics
    progress: AnalysisProgress
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class AnalysisService:
    """Orchestrates the full analysis pipeline.

    Used by both CLI (`analyze_sync`) and web (`analyze_async`).
    Supports cooperative cancellation via an asyncio.Event.
    """

    def __init__(self, git_provider: GitProvider) -> None:
        self.git_provider = git_provider
        self._cancel_event: asyncio.Event | None = None

    def request_cancel(self) -> None:
        """Signal the running analysis to stop after the current repo."""
        if self._cancel_event is not None:
            self._cancel_event.set()

    @property
    def is_cancelling(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def analyze_sync(
        self,
        config: AppConfig,
        on_progress: ProgressCallback | None = None,
    ) -> AnalysisResult:
        """Run analysis synchronously (for CLI).

        Safe to call regardless of whether an event loop is already running.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # Already in an async context — run in a new thread with its own loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self.analyze_async(config, on_progress))
                return future.result()
        return asyncio.run(self.analyze_async(config, on_progress))

    async def analyze_async(
        self,
        config: AppConfig,
        on_progress: ProgressCallback | None = None,
    ) -> AnalysisResult:
        """Run analysis asynchronously (for web).

        Supports cooperative cancellation: call ``request_cancel()`` to stop
        after the currently-processing repository completes.
        """
        self._cancel_event = asyncio.Event()

        analyzer = ReadinessAnalyzer(config)
        matcher = BranchPatternMatcher()

        progress = AnalysisProgress(
            phase=AnalysisPhase.STARTING,
            total=len(config.repositories),
            repos=[RepoProgress(name=r.name) for r in config.repositories],
            started_at=time.monotonic(),
        )

        await self._emit(on_progress, "analysis_start", progress)

        progress.phase = AnalysisPhase.ANALYZING
        analyses: list[RepositoryAnalysis] = []
        cancelled = False

        # Use semaphore for concurrent analysis (honors max_concurrent)
        max_concurrent = max(1, config.settings.max_concurrent)
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _analyze_one(i: int, repo_config) -> tuple[int, RepositoryAnalysis]:
            """Analyze a single repo with semaphore-limited concurrency.

            NOTE: progress mutations are safe in asyncio's single-threaded event loop.
            The GIL + no ``await`` between read-increment-write ensures atomicity.
            If this code ever moves to threads, use asyncio.Lock or per-task aggregation.
            """
            async with semaphore:
                if self._cancel_event.is_set():
                    progress.repos[i].status = "skipped"
                    return i, analyzer.analyze_error(repo_config, "Cancelled")

                progress.current_repo = repo_config.name
                progress.repos[i].status = "analyzing"
                await self._emit(on_progress, "repo_start", progress)

                repo_start = time.monotonic()
                try:
                    url = config.resolve_repo_url(repo_config)

                    if is_placeholder_url(url):
                        analysis = analyzer.analyze_error(
                            repo_config,
                            f"Placeholder URL skipped: {url}",
                        )
                        analysis.error_kind = GitErrorKind.PLACEHOLDER_URL.value
                        progress.repos[i].status = "skipped"
                        progress.repos[i].error = (
                            "Placeholder URL — configure a real"
                            " repository URL"
                        )
                        progress.warnings.append(
                            f"{repo_config.name}: placeholder URL skipped"
                        )
                        logger.info(
                            "Skipping placeholder URL for %s: %s",
                            repo_config.name, url,
                        )
                    else:
                        branches = await asyncio.to_thread(
                            self.git_provider.list_remote_branches,
                            url,
                            config.settings.timeout_seconds,
                        )

                        pattern = config.resolve_branch_pattern(repo_config)
                        resolved = matcher.resolve(
                            pattern, config.release.target_month, config.release.target_year
                        )

                        branch_info = await asyncio.to_thread(
                            self.git_provider.get_branch_info,
                            url,
                            resolved.resolved_name,
                            config.settings.timeout_seconds,
                        )

                        default_branch_info = None
                        matching = matcher.find_matching(branches, resolved)
                        if not matching and hasattr(self.git_provider, 'get_default_branch_info'):
                            try:
                                default_branch_info = await asyncio.to_thread(
                                    self.git_provider.get_default_branch_info,
                                    url,
                                    config.settings.timeout_seconds,
                                )
                            except Exception as default_exc:
                                logger.debug(
                                    "Default branch lookup failed for %s: %s",
                                    repo_config.name, default_exc,
                                )

                        analysis = analyzer.analyze(
                            repo_config, branches,
                            branch_info, default_branch_info,
                        )

                        # GitLab tag enrichment — additive, failures are non-fatal
                        if analysis.branch_exists and is_gitlab_url(url):
                            analyzed_branch = (
                                analysis.branch.name
                                if analysis.branch
                                else resolved.resolved_name
                            )
                            try:
                                # Reuse the authenticated GitLab provider from
                                # SmartGitProvider to avoid creating a bare one
                                # that lacks the user-supplied token.
                                gl: GitLabProvider
                                if isinstance(self.git_provider, SmartGitProvider):
                                    gl = self.git_provider.gitlab_provider
                                else:
                                    gl = GitLabProvider()
                                tag_info = await asyncio.to_thread(
                                    gl.get_latest_branch_tag,
                                    url,
                                    analyzed_branch,
                                    config.settings.timeout_seconds,
                                )
                                analysis.latest_tag = tag_info
                            except Exception as tag_exc:
                                logger.debug(
                                    "GitLab tag enrichment failed for %s: %s",
                                    repo_config.name, tag_exc,
                                )

                        progress.repos[i].status = "done"
                        progress.repos[i].readiness = analysis.status.value

                except GitAccessError as exc:
                    analysis = analyzer.analyze_error(repo_config, exc.user_message)
                    analysis.error_kind = exc.kind.value
                    analysis.error_detail = exc.detail
                    progress.repos[i].status = "error"
                    progress.repos[i].error = exc.user_message
                    progress.error_count += 1
                    logger.warning(
                        "Git access error for %s: [%s] %s",
                        repo_config.name, exc.kind.value, exc,
                    )

                except Exception as exc:
                    analysis = analyzer.analyze_error(repo_config, f"Unexpected: {exc}")
                    progress.repos[i].status = "error"
                    progress.repos[i].error = str(exc)
                    progress.error_count += 1
                    logger.error("Unexpected error for %s: %s", repo_config.name, exc)

                progress.repos[i].elapsed_ms = round((time.monotonic() - repo_start) * 1000, 1)
                progress.completed += 1
                await self._emit(on_progress, "repo_complete", progress)
                return i, analysis

        # Launch all tasks concurrently (semaphore limits actual parallelism)
        tasks = [
            _analyze_one(i, repo_config)
            for i, repo_config in enumerate(config.repositories)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Unexpected analysis task error: %s", result)
                progress.error_count += 1
                continue
            idx, analysis = result
            # Skip cancelled repos (status="skipped" from cancel check)
            if progress.repos[idx].status == "skipped" and analysis.error_message == "Cancelled":
                continue
            analyses.append(analysis)

        # Sort analyses to match original repo order
        analyses.sort(
            key=lambda a: next(
                (i for i, r in enumerate(config.repositories) if r.name == a.name), 0
            )
        )

        if self._cancel_event.is_set():
            cancelled = True
            # Emit stopping event
            progress.phase = AnalysisPhase.STOPPING
            await self._emit(on_progress, "analysis_stopping", progress)

        # Determine final phase
        progress.finished_at = time.monotonic()
        progress.current_repo = None

        if cancelled:
            progress.phase = AnalysisPhase.CANCELLED
        elif progress.error_count > 0 and progress.error_count < progress.total:
            progress.phase = AnalysisPhase.PARTIAL_FAILURE
        elif progress.error_count == progress.total:
            progress.phase = AnalysisPhase.FAILED
        else:
            progress.phase = AnalysisPhase.COMPLETED

        layer_labels = {layer.id: layer.label for layer in config.layers}
        metrics = compute_dashboard_metrics(analyses, layer_labels)

        await self._emit(on_progress, "analysis_complete", progress)
        self._cancel_event = None

        return AnalysisResult(
            config=config,
            analyses=analyses,
            metrics=metrics,
            progress=progress,
        )

    @staticmethod
    async def _emit(
        callback: ProgressCallback | None,
        event_type: str,
        progress: AnalysisProgress,
    ) -> None:
        if callback is None:
            return
        result = callback(event_type, progress)
        if asyncio.iscoroutine(result):
            await result

    async def analyze_single_repo(
        self,
        config: AppConfig,
        repo_name: str,
    ) -> RepositoryAnalysis | None:
        """Re-analyze a single repository and return updated analysis.

        Returns ``None`` if the repo name is not found in config.
        """
        repo_config = next(
            (r for r in config.repositories if r.name == repo_name), None,
        )
        if repo_config is None:
            return None

        analyzer = ReadinessAnalyzer(config)
        matcher = BranchPatternMatcher()

        url = config.resolve_repo_url(repo_config)

        if is_placeholder_url(url):
            analysis = analyzer.analyze_error(
                repo_config, f"Placeholder URL skipped: {url}",
            )
            analysis.error_kind = GitErrorKind.PLACEHOLDER_URL.value
            return analysis

        try:
            branches = await asyncio.to_thread(
                self.git_provider.list_remote_branches,
                url, config.settings.timeout_seconds,
            )

            pattern = config.resolve_branch_pattern(repo_config)
            resolved = matcher.resolve(
                pattern,
                config.release.target_month,
                config.release.target_year,
            )

            branch_info = await asyncio.to_thread(
                self.git_provider.get_branch_info,
                url, resolved.resolved_name,
                config.settings.timeout_seconds,
            )

            default_branch_info = None
            matching = matcher.find_matching(branches, resolved)
            if not matching and hasattr(self.git_provider, "get_default_branch_info"):
                with contextlib.suppress(Exception):
                    default_branch_info = await asyncio.to_thread(
                        self.git_provider.get_default_branch_info,
                        url, config.settings.timeout_seconds,
                    )

            analysis = analyzer.analyze(
                repo_config, branches, branch_info, default_branch_info,
            )

            # GitLab tag enrichment
            if analysis.branch_exists and is_gitlab_url(url):
                analyzed_branch = (
                    analysis.branch.name
                    if analysis.branch
                    else resolved.resolved_name
                )
                try:
                    gl: GitLabProvider
                    if isinstance(self.git_provider, SmartGitProvider):
                        gl = self.git_provider.gitlab_provider
                    else:
                        gl = GitLabProvider()
                    tag_info = await asyncio.to_thread(
                        gl.get_latest_branch_tag,
                        url, analyzed_branch,
                        config.settings.timeout_seconds,
                    )
                    analysis.latest_tag = tag_info
                except Exception:
                    pass

            return analysis

        except GitAccessError as exc:
            analysis = analyzer.analyze_error(
                repo_config, exc.user_message,
            )
            analysis.error_kind = exc.kind.value
            analysis.error_detail = exc.detail
            return analysis

        except Exception as exc:
            return analyzer.analyze_error(
                repo_config, f"Unexpected: {exc}",
            )

"""FastAPI web server — routes for dashboard, config CRUD, analysis, and export."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from releaseboard import __version__
from releaseboard.analysis.metrics import compute_dashboard_metrics
from releaseboard.application.service import AnalysisPhase, AnalysisService
from releaseboard.config.schema import validate_config, validate_layer_references
from releaseboard.git.github_provider import GitHubProvider, parse_github_owner
from releaseboard.git.gitlab_provider import GitLabProvider, parse_gitlab_group
from releaseboard.git.provider import GitAccessError, is_placeholder_url
from releaseboard.git.smart_provider import SmartGitProvider
from releaseboard.i18n import detect_locale_from_header, get_catalog, supported_locales, t
from releaseboard.integrations.releasepilot.adapter import ReleasePilotAdapter
from releaseboard.integrations.releasepilot.models import (
    AudienceMode,
    OutputFormat,
    ReleasePrepRequest,
)
from releaseboard.presentation.renderer import DashboardRenderer
from releaseboard.presentation.view_models import build_dashboard_view_model
from releaseboard.shared.logging import get_logger
from releaseboard.web.cors import get_cors_origins
from releaseboard.web.middleware import (
    APIKeyMiddleware,
    CSRFMiddleware,
    RateLimitMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from releaseboard.web.state import AppState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger("web.server")

# Maximum request body size (1 MB)
_MAX_BODY_BYTES = 1_048_576


class _InvalidContentTypeError(Exception):
    """Raised when a request has an unexpected Content-Type."""
    def __init__(self, content_type: str) -> None:
        self.content_type = content_type
        super().__init__(f"Expected application/json, got: {content_type}")


class _BodyTooLargeError(Exception):
    """Raised when a request body exceeds the size limit."""
    def __init__(self, size: int) -> None:
        self.size = size
        super().__init__(f"Request body too large: {size} bytes (max {_MAX_BODY_BYTES})")


class _InvalidJSONError(Exception):
    """Raised when a request body contains malformed JSON."""
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Invalid JSON: {detail}")


def create_app(
    config_path: Path,
    *,
    first_run: bool = False,
    root_path: str = "",
) -> FastAPI:
    """Create the FastAPI application with all routes.

    Parameters
    ----------
    config_path:
        Path to the ``releaseboard.json`` configuration file.
    first_run:
        When *True*, start with the first-run setup wizard.
    root_path:
        ASGI ``root_path`` for mounting behind a reverse proxy or inside
        a portal shell (e.g. ``/tools/releaseboard``).
    """

    app = FastAPI(
        title="ReleaseBoard",
        description="Release Readiness Dashboard",
        version=__version__,
        root_path=root_path,
    )

    # --- Middleware Stack ---
    # Order of execution (outermost → innermost):
    # SecurityHeaders → Logging → RateLimit → CORS → CSRF → APIKey → route
    # (last added = outermost in Starlette)
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_cors_origins(),
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*", "X-API-Key", "X-Requested-With"],
        expose_headers=["Content-Disposition", "ETag"],
    )
    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=120,
        analysis_per_minute=5,
    )
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    # --- State ---
    if first_run:
        state: AppState | None = None
    else:
        state = AppState(config_path)
    git_provider = SmartGitProvider(
        github_token=os.environ.get("GITHUB_TOKEN") or None,
        gitlab_token=os.environ.get("GITLAB_TOKEN") or None,
    )
    service = AnalysisService(git_provider)
    release_pilot = ReleasePilotAdapter()
    _start_time = time.monotonic()
    _background_tasks: set[asyncio.Task[None]] = set()

    # --- Lifespan (startup/shutdown) ---

    @asynccontextmanager
    async def _lifespan(app_: FastAPI) -> AsyncIterator[None]:
        logger.info("ReleaseBoard v%s starting", __version__)
        _gh = "set" if git_provider.get_token_for_url("https://github.com") else "not set"
        _gl = "set" if git_provider.get_token_for_url("https://gitlab.com") else "not set"
        logger.info("Auth tokens: GITHUB_TOKEN=%s, GITLAB_TOKEN=%s", _gh, _gl)
        yield
        if state is not None and state.analysis_lock.locked():
            service.request_cancel()
            logger.info("Shutdown requested — cancelling running analysis")
        # Wait for background tasks to finish (with timeout)
        if _background_tasks:
            logger.info("Waiting for %d background task(s) to finish…", len(_background_tasks))
            done, pending = await asyncio.wait(_background_tasks, timeout=10)
            for t_pending in pending:
                t_pending.cancel()
        if state is not None:
            await state.broadcast("server_shutdown", {"reason": "Server is shutting down"})
        logger.info("Server shutdown complete")

    app.router.lifespan_context = _lifespan

    # --- Error Handlers ---

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": "Not found", "path": str(request.url.path)},
            status_code=404,
        )

    @app.exception_handler(500)
    async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Internal server error on %s: %s", request.url.path, exc)
        return JSONResponse(
            {"ok": False, "error": "Internal server error"},
            status_code=500,
        )

    @app.exception_handler(_InvalidContentTypeError)
    async def invalid_content_type_handler(
        request: Request, exc: _InvalidContentTypeError,
    ) -> JSONResponse:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Unsupported Content-Type: "
                    f"{exc.content_type}. "
                    f"Expected application/json"
                ),
            },
            status_code=415,
        )

    @app.exception_handler(_BodyTooLargeError)
    async def body_too_large_handler(request: Request, exc: _BodyTooLargeError) -> JSONResponse:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Request body too large "
                    f"({exc.size} bytes). "
                    f"Max: {_MAX_BODY_BYTES}"
                ),
            },
            status_code=413,
        )

    @app.exception_handler(_InvalidJSONError)
    async def invalid_json_handler(request: Request, exc: _InvalidJSONError) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": "Invalid JSON in request body"},
            status_code=400,
        )

    def _req_locale(request: Request) -> str:
        """Extract locale from request: ?lang= param > Accept-Language header."""
        lang_param = request.query_params.get("lang")
        if lang_param and lang_param in supported_locales():
            return lang_param
        return detect_locale_from_header(request.headers.get("accept-language"))

    def _translate_validation_error(error: str, locale: str) -> str:
        """Translate jsonschema validation error messages using i18n catalog."""
        import re

        def _friendly_path(raw_path: str) -> str:
            key = f"validation.path.{raw_path.replace('.', '_').replace('[', '').replace(']', '')}"
            translated = t(key, locale=locale)
            return translated if translated != key else raw_path

        # Pattern: "(path): 'X' is a required property"
        m = re.match(r"^(.+?): '(.+?)' is a required property$", error)
        if m:
            return t(
                "validation.required_property",
                locale=locale,
                path=_friendly_path(m.group(1)),
                prop=m.group(2),
            )

        # Pattern: "(path): Additional properties are not allowed (...)"
        m = re.match(
            r"^(.+?): Additional properties are not allowed"
            r" \((.+?) (?:was|were) unexpected\)$",
            error,
        )
        if m:
            return t(
                "validation.additional_properties",
                locale=locale,
                path=_friendly_path(m.group(1)),
                props=m.group(2),
            )

        # Pattern: "(path): X is not of type 'Y'"
        m = re.match(r"^(.+?): (.+?) is not of type '(.+?)'$", error)
        if m:
            return t(
                "validation.wrong_type",
                locale=locale,
                path=_friendly_path(m.group(1)),
                value=m.group(2),
                expected=m.group(3),
            )

        # Pattern: "(path): X is not valid under any of the given schemas"
        m = re.match(r"^(.+?): .+ is not valid under any of the given schemas$", error)
        if m:
            return t("validation.invalid_value", locale=locale, path=_friendly_path(m.group(1)))

        # Pattern: "(path): X is less than the minimum of Y" / "greater than maximum"
        m = re.match(
            r"^(.+?): (.+?) is (?:less than the minimum"
            r"|greater than the maximum) of (.+)$",
            error,
        )
        if m:
            return t(
                "validation.out_of_range",
                locale=locale,
                path=_friendly_path(m.group(1)),
                value=m.group(2),
                limit=m.group(3),
            )

        # Fallback: return original
        return error

    async def _read_json_body(request: Request) -> dict[str, Any]:
        """Read and validate a JSON request body with size and content-type checks."""
        content_type = request.headers.get("content-type", "")
        if content_type and "json" not in content_type:
            raise _InvalidContentTypeError(content_type)
        # Early rejection based on Content-Length header (before reading body)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
            raise _BodyTooLargeError(int(cl))
        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            raise _BodyTooLargeError(len(body))
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise _InvalidJSONError(str(exc)) from exc

    # --- Dashboard ---

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        """Serve the interactive dashboard."""
        locale = _req_locale(request)

        if state is None:
            # First-run mode — serve setup wizard
            return await _serve_first_run(request)

        config = state.get_active_config()
        renderer = DashboardRenderer()

        if state.analysis_result:
            vm = build_dashboard_view_model(
                config, state.analysis_result.analyses, state.analysis_result.metrics,
                locale=locale,
                config_raw=state.config_state.draft_raw,
            )
        else:
            from releaseboard.analysis.metrics import DashboardMetrics
            empty_metrics = DashboardMetrics()
            empty_metrics.total = len(config.repositories)
            vm = build_dashboard_view_model(
                config, [], empty_metrics, locale=locale,
                config_raw=state.config_state.draft_raw,
            )

        vm.interactive = True
        try:
            html = renderer.render(vm)
        except Exception as exc:
            logger.error("Dashboard template rendering failed: %s", exc)
            _title = t("error.page_title", locale=locale) or "ReleaseBoard Error"
            _heading = t("error.dashboard_rendering", locale=locale) or "Dashboard Rendering Error"
            _body = (
                t("error.check_logs", locale=locale)
                or "The dashboard could not be rendered."
                " Please check the server logs for details."
            )
            html = (
                f"<!DOCTYPE html><html><head><title>{_title}</title></head>"
                f"<body><h1>{_heading}</h1>"
                f"<p>{_body}</p></body></html>"
            )
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Content-Language": locale,
        })

    # --- First-Run Setup ---

    async def _serve_first_run(request: Request) -> HTMLResponse:
        locale = _req_locale(request)
        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale=locale, config_path=str(config_path))
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store",
            "Content-Language": locale,
        })

    @app.get("/api/examples")
    async def list_examples() -> JSONResponse:
        """List available example configurations."""
        examples_dir = Path(__file__).resolve().parent.parent.parent.parent / "examples"
        examples: list[dict[str, Any]] = []
        if examples_dir.exists():
            for f in sorted(examples_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    examples.append({
                        "name": f.name,
                        "title": data.get("release", {}).get("name", f.stem),
                        "repos": len(data.get("repositories", [])),
                        "layers": len(data.get("layers", [])),
                    })
                except (json.JSONDecodeError, KeyError):
                    pass
        return JSONResponse({"ok": True, "examples": examples})

    @app.post("/api/config/create")
    async def create_initial_config(request: Request) -> JSONResponse:
        """Create the initial configuration file from the first-run wizard."""
        nonlocal state
        body = await _read_json_body(request)

        mode = body.get("mode", "empty")

        if mode == "empty":
            from datetime import datetime as _dt
            now = _dt.now()
            config_data = {
                "release": {
                    "name": body.get("release_name", f"{now.strftime('%B %Y')} Release"),
                    "target_month": int(body.get("target_month", now.month)),
                    "target_year": int(body.get("target_year", now.year)),
                    "branch_pattern": body.get("branch_pattern", "release/{YYYY}.{MM}"),
                },
                "repositories": [],
            }
        elif mode == "example":
            example_name = body.get("example", "config.json")
            safe_name = Path(example_name).name
            example_path = (
                Path(__file__).resolve().parent.parent.parent.parent
                / "examples"
                / safe_name
            )
            if not example_path.exists():
                return JSONResponse(
                    {"ok": False, "error": f"Example '{safe_name}' not found"},
                    status_code=404,
                )
            config_data = json.loads(example_path.read_text(encoding="utf-8"))
        elif mode == "import":
            config_data = body.get("config", {})
        else:
            return JSONResponse({"ok": False, "error": f"Unknown mode: {mode}"}, status_code=400)

        # Fill defaults and validate
        from releaseboard.web.state import fill_config_defaults
        fill_config_defaults(config_data)
        errors = validate_config(config_data) + validate_layer_references(config_data)
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=422)

        # Write config file (atomic: temp + rename to avoid corruption on crash)
        import tempfile as _tmpmod
        json_text = json.dumps(config_data, indent=2, ensure_ascii=False) + "\n"
        tmp_fd, tmp_name = _tmpmod.mkstemp(
            dir=str(config_path.parent), suffix=".tmp",
        )
        try:
            with open(tmp_fd, "w", encoding="utf-8") as tmp_f:
                tmp_f.write(json_text)
            Path(tmp_name).replace(config_path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

        # Initialize app state and switch to normal mode
        state = AppState(config_path)

        return JSONResponse({"ok": True, "message": "Configuration created", "redirect": "/"})

    # --- Config API ---

    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        """Get current config state (draft + persisted)."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        data = state.config_state.to_api_dict()
        data["etag"] = state.config_state.config_etag
        return JSONResponse(
            data, headers={"ETag": f'"{state.config_state.config_etag}"'}
        )

    @app.put("/api/config")
    async def update_config(request: Request) -> JSONResponse:
        """Update draft config from UI."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        body = await _read_json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "errors": ["Invalid payload: expected a JSON object"]},
                status_code=400,
            )
        errors = state.update_draft(body)
        return JSONResponse({
            "ok": len(errors) == 0,
            "errors": errors,
            "has_unsaved_changes": state.config_state.has_unsaved_changes,
        })

    @app.post("/api/config/save")
    async def save_config(request: Request) -> JSONResponse:
        """Persist draft config to disk and clear stale analysis data."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        # Optimistic concurrency: reject if ETag doesn't match
        if_match = request.headers.get("if-match", "").strip('"')
        if if_match and if_match != state.config_state.config_etag:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Config was modified by another client. Refresh and retry.",
                },
                status_code=409,
            )
        errors = state.save_config()
        if not errors:
            state.analysis_result = None
        etag = state.config_state.config_etag
        return JSONResponse(
            {
                "ok": len(errors) == 0,
                "errors": errors,
                "has_unsaved_changes": state.config_state.has_unsaved_changes,
                "etag": etag,
            },
            headers={"ETag": f'"{etag}"'},
        )

    @app.post("/api/config/reset")
    async def reset_config() -> JSONResponse:
        """Reset draft to last persisted version."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        state.reset_draft()
        return JSONResponse({
            "ok": True,
            "draft": state.get_draft(),
            "has_unsaved_changes": False,
        })

    @app.post("/api/config/validate")
    async def validate_config_endpoint(request: Request) -> JSONResponse:
        """Validate config data against schema."""
        locale = _req_locale(request)
        body = await _read_json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "errors": ["Invalid payload: expected a JSON object"]},
                status_code=400,
            )
        errors = validate_config(body)
        errors += validate_layer_references(body)
        translated = [_translate_validation_error(e, locale) for e in errors]
        return JSONResponse({"ok": len(errors) == 0, "errors": translated})

    @app.get("/api/config/schema")
    async def get_schema() -> JSONResponse:
        """Get the JSON Schema for config validation with example."""
        schema_path = Path(__file__).parent.parent / "config" / "schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("Failed to load config schema: %s", exc)
            return JSONResponse(
                {"ok": False, "error": "Config schema unavailable"},
                status_code=503,
            )
        example = {
            "release": {
                "name": "March 2026 Release",
                "target_month": 3,
                "target_year": 2026,
                "branch_pattern": "release/{YYYY}.{MM}",
            },
            "layers": [
                {"id": "frontend", "label": "Frontend", "color": "#3b82f6"},
                {"id": "backend", "label": "Backend", "color": "#10b981"},
            ],
            "repositories": [
                {
                    "name": "web-app",
                    "url": "https://github.com/org/web-app",
                    "layer": "frontend",
                },
                {
                    "name": "api-service",
                    "url": "https://github.com/org/api-service",
                    "layer": "backend",
                },
            ],
        }
        return JSONResponse({"ok": True, "schema": schema, "example": example})

    @app.put("/api/config/branding")
    async def update_branding(request: Request) -> JSONResponse:
        """Quick-update branding."""
        if not state:
            return JSONResponse({"ok": False}, 503)
        body = await _read_json_body(request)
        import re as _re
        _hex_re = _re.compile(r"^#[0-9a-fA-F]{6,8}$")
        draft = state.config_state.draft_raw
        branding = draft.setdefault("branding", {})
        changed = False
        for key in ("primary_color", "secondary_color", "tertiary_color"):
            val = body.get(key)
            if val and _hex_re.match(str(val)):
                branding[key] = str(val)
                changed = True
        for key in ("title", "subtitle", "company"):
            if key in body and isinstance(body[key], str):
                branding[key] = body[key][:500]
                changed = True
        # Also save theme into settings when changed via quick-settings
        theme_val = body.get("theme")
        if theme_val in ("light", "dark", "midnight", "system"):
            settings = draft.setdefault("settings", {})
            settings["theme"] = theme_val
            changed = True
        if changed:
            state.config_state.draft_raw = draft
            errors = state.save_config()
            if errors:
                return JSONResponse({"ok": False, "errors": errors})
        return JSONResponse({"ok": True})

    @app.get("/api/config/export")
    async def export_config() -> JSONResponse:
        """Export current draft config."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        return JSONResponse(
            state.export_config(),
            headers={"Content-Disposition": "attachment; filename=releaseboard.json"},
        )

    @app.post("/api/config/import")
    async def import_config(request: Request) -> JSONResponse:
        """Import config from uploaded JSON."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        body = await _read_json_body(request)
        errors = state.import_config(body)
        return JSONResponse({
            "ok": len(errors) == 0,
            "errors": errors,
            "draft": state.get_draft(),
            "has_unsaved_changes": state.config_state.has_unsaved_changes,
        })

    # --- Analysis API ---

    @app.post("/api/analyze")
    async def trigger_analysis(request: Request) -> JSONResponse:
        """Trigger a new analysis run.

        Accepts optional ``github_token`` / ``gitlab_token`` in the JSON body.
        When provided, the tokens are applied to the server-level git provider
        (in memory only) so the analysis uses authenticated access.
        """
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        locale = _req_locale(request)
        if state.analysis_lock.locked():
            return JSONResponse(
                {"ok": False, "error": t("api.analysis_already_running", locale=locale)},
                status_code=409,
            )

        # Accept optional tokens from the request body
        body: dict[str, Any] = {}
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                body = await _read_json_body(request)
            except Exception:
                body = {}

        if isinstance(git_provider, SmartGitProvider) and body:
            gh_tok = (body.get("github_token") or "").strip() or None
            gl_tok = (body.get("gitlab_token") or "").strip() or None
            if gh_tok or gl_tok:
                git_provider.update_tokens(
                    github_token=gh_tok, gitlab_token=gl_tok,
                )

        async def _run() -> None:
            async with state.analysis_lock:
                try:
                    config = state.get_active_config()
                    result = await service.analyze_async(
                        config, on_progress=state.on_analysis_progress
                    )
                    state.analysis_result = result
                except Exception as exc:
                    logger.error("Analysis task failed: %s", exc, exc_info=True)
                    state.analysis_progress.phase = AnalysisPhase.FAILED
                    try:
                        await state.broadcast(
                            "analysis_complete",
                            state.analysis_progress.to_dict(),
                        )
                    except Exception as bcast_exc:
                        logger.error("Failed to broadcast analysis failure: %s", bcast_exc)

        task = asyncio.create_task(_run(), name="releaseboard-analysis")
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return JSONResponse({"ok": True, "message": t("api.analysis_started", locale=locale)})

    @app.post("/api/analyze/repo")
    async def analyze_single_repo(request: Request) -> JSONResponse:
        """Re-analyze a single repository by name.

        Expects JSON body ``{"repo": "<name>"}``.  Updates the
        existing analysis result in-place without a full re-run.
        """
        if state is None:
            return JSONResponse(
                {"ok": False, "error": "No configuration loaded"},
                status_code=503,
            )
        body = await _read_json_body(request)
        repo_name = (body.get("repo") or "").strip()
        if not repo_name:
            return JSONResponse(
                {"ok": False, "error": "Missing 'repo' in body"},
                status_code=400,
            )

        config = state.get_active_config()
        analysis = await service.analyze_single_repo(config, repo_name)
        if analysis is None:
            return JSONResponse(
                {"ok": False, "error": f"Repository '{repo_name}' not found in config"},
                status_code=404,
            )

        # Merge result into existing analysis result
        if state.analysis_result:
            new_list = [
                analysis if a.name == repo_name else a
                for a in state.analysis_result.analyses
            ]
            # If the repo wasn't in the existing list, append it
            if not any(a.name == repo_name for a in state.analysis_result.analyses):
                new_list.append(analysis)
            layer_labels = {layer.id: layer.label for layer in config.layers}
            state.analysis_result.analyses = new_list
            state.analysis_result.metrics = compute_dashboard_metrics(
                new_list, layer_labels,
            )
            import datetime as _dtmod
            state.analysis_result.timestamp = _dtmod.datetime.now(
                tz=_dtmod.UTC,
            )

        return JSONResponse({
            "ok": True,
            "name": analysis.name,
            "status": analysis.status.value,
            "branch_exists": analysis.branch_exists,
            "error_message": analysis.error_message or "",
        })

    @app.post("/api/analyze/cancel")
    async def cancel_analysis(request: Request) -> JSONResponse:
        """Request cancellation of the running analysis."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        locale = _req_locale(request)
        if not state.analysis_lock.locked():
            return JSONResponse(
                {"ok": False, "error": t("api.no_analysis_running", locale=locale)},
                status_code=409,
            )
        service.request_cancel()
        return JSONResponse({"ok": True, "message": t("api.cancellation_requested", locale=locale)})

    @app.get("/api/analyze/stream")
    async def analysis_stream(request: Request) -> StreamingResponse:
        """SSE endpoint for real-time analysis progress."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        queue = state.subscribe()

        async def event_generator():
            try:
                # Send current state immediately
                yield _sse_format("current_state", state.analysis_progress.to_dict())

                while True:
                    # Check for client disconnect
                    if await request.is_disconnected():
                        logger.debug("SSE client disconnected")
                        break

                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield _sse_format(message["event"], message["data"])

                        if message["event"] in ("analysis_complete", "server_shutdown"):
                            break
                    except TimeoutError:
                        # Send keepalive
                        yield ": keepalive\n\n"
            finally:
                state.unsubscribe(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/analyze/results")
    async def get_results(request: Request) -> JSONResponse:
        """Get latest analysis results."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        locale = _req_locale(request)
        if not state.analysis_result:
            return JSONResponse(
                {"ok": False, "error": t("api.no_results", locale=locale)},
                status_code=404,
            )

        result = state.analysis_result
        return JSONResponse({
            "ok": True,
            "timestamp": result.timestamp.isoformat(),
            "progress": result.progress.to_dict(),
            "metrics": {
                "total": result.metrics.total,
                "ready": result.metrics.ready,
                "missing": result.metrics.missing,
                "invalid_naming": result.metrics.invalid_naming,
                "stale": result.metrics.stale,
                "error": result.metrics.error,
                "warning": result.metrics.warning,
                "readiness_pct": result.metrics.readiness_pct,
            },
            "analyses": [
                {
                    "name": a.name,
                    "layer": a.layer,
                    "status": a.status.value,
                    "status_label": a.status.localized_label(locale),
                    "expected_branch": a.expected_branch_name,
                    "branch_exists": a.branch_exists,
                    "actual_branch": a.branch.name if a.branch and a.branch.exists else None,
                    "naming_valid": a.naming_valid,
                    "is_stale": a.is_stale,
                    "error_message": a.error_message,
                    "error_kind": a.error_kind,
                    "error_detail": a.error_detail,
                    "last_commit_sha": a.branch.last_commit_sha if a.branch else None,
                    "last_commit_author": a.branch.last_commit_author if a.branch else None,
                    "last_commit_message": a.branch.last_commit_message if a.branch else None,
                    "last_commit_date": (
                        a.branch.last_commit_date.isoformat()
                        if a.branch and a.branch.last_commit_date
                        else None
                    ),
                    "repo_description": a.branch.repo_description if a.branch else None,
                    "repo_visibility": a.branch.repo_visibility if a.branch else None,
                    "repo_web_url": a.branch.repo_web_url if a.branch else None,
                    "repo_owner": a.branch.repo_owner if a.branch else None,
                    "repo_default_branch": a.branch.repo_default_branch if a.branch else None,
                    "data_source": a.branch.data_source if a.branch else None,
                    "warnings": list(a.warnings),
                    "notes": list(a.notes),
                }
                for a in result.analyses
            ],
        })

    @app.get("/api/export/html")
    async def export_html(request: Request) -> HTMLResponse:
        """Export a static (self-contained) HTML dashboard."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        locale = _req_locale(request)

        config = state.get_active_config()
        renderer = DashboardRenderer()

        if state.analysis_result:
            vm = build_dashboard_view_model(
                config, state.analysis_result.analyses, state.analysis_result.metrics,
                locale=locale,
                config_raw=state.config_state.draft_raw,
            )
        else:
            from releaseboard.analysis.metrics import DashboardMetrics
            empty_metrics = DashboardMetrics()
            empty_metrics.total = len(config.repositories)
            vm = build_dashboard_view_model(
                config, [], empty_metrics, locale=locale,
                config_raw=state.config_state.draft_raw,
            )

        vm.interactive = False
        try:
            html = renderer.render(vm)
        except Exception as exc:
            logger.error("Export HTML template rendering failed: %s", exc)
            _title = t("error.page_title", locale=locale) or "ReleaseBoard Error"
            _heading = t("error.export_rendering", locale=locale) or "Export Rendering Error"
            _body = (
                t("error.export_check_logs", locale=locale)
                or "The dashboard could not be rendered for export."
            )
            html = (
                f"<!DOCTYPE html><html><head><title>{_title}</title></head>"
                f"<body><h1>{_heading}</h1>"
                f"<p>{_body}</p></body></html>"
            )
        return HTMLResponse(
            html,
            headers={"Content-Disposition": "attachment; filename=dashboard.html"},
        )

    @app.get("/api/status")
    async def app_status() -> JSONResponse:
        """Deep application health status."""
        if state is None:
            return JSONResponse({
                "ok": True,
                "version": __version__,
                "uptime_seconds": round(time.monotonic() - _start_time, 1),
                "first_run": True,
                "config_readable": False,
            })
        config_readable = False
        try:
            config_path.read_text(encoding="utf-8")
            config_readable = True
        except Exception:
            pass

        return JSONResponse({
            "ok": True,
            "version": __version__,
            "uptime_seconds": round(time.monotonic() - _start_time, 1),
            "analysis_phase": state.analysis_progress.phase.value,
            "analysis_running": state.analysis_lock.locked(),
            "has_results": state.analysis_result is not None,
            "has_unsaved_changes": state.config_state.has_unsaved_changes,
            "config_readable": config_readable,
            "sse_subscribers": len(state._sse_subscribers),
        })

    @app.get("/health/live")
    async def health_live() -> JSONResponse:
        """Liveness probe — confirms the process is running."""
        return JSONResponse({"status": "alive"})

    @app.get("/api/browse/dirs")
    async def browse_dirs(path: str = "") -> JSONResponse:
        """Browse local directories and detect git repos."""
        import os

        base = path.strip() or os.path.expanduser("~")
        base = os.path.expanduser(base)
        if not os.path.isdir(base):
            return JSONResponse({"ok": False, "error": "Not a directory"})

        items: list[dict] = []
        parent = os.path.dirname(base)
        if parent != base:
            items.append({"name": "..", "path": parent, "is_git": False})

        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
        except PermissionError:
            return JSONResponse({"ok": False, "error": "Permission denied"})

        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.startswith("."):
                continue
            ep = entry.path
            is_git = os.path.isdir(os.path.join(ep, ".git")) or (
                os.path.isfile(os.path.join(ep, "HEAD"))
                and os.path.isdir(os.path.join(ep, "refs"))
            )
            items.append({"name": entry.name, "path": ep, "is_git": is_git})

        cur_is_git = os.path.isdir(os.path.join(base, ".git")) or (
            os.path.isfile(os.path.join(base, "HEAD"))
            and os.path.isdir(os.path.join(base, "refs"))
        )

        return JSONResponse({
            "ok": True,
            "current": base,
            "is_git": cur_is_git,
            "items": items,
        })

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        """Readiness probe — confirms the app can serve requests."""
        if state is None:
            return JSONResponse(
                {"status": "not_ready", "config_readable": False, "first_run": True},
                status_code=503,
            )
        config_ok = False
        try:
            config_path.read_text(encoding="utf-8")
            config_ok = True
        except Exception:
            pass
        ready = config_ok and not state.analysis_lock.locked()
        status_code = 200 if ready else 503
        return JSONResponse(
            {
                "status": "ready" if ready else "not_ready",
                "config_readable": config_ok,
                "analysis_running": state.analysis_lock.locked(),
            },
            status_code=status_code,
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        """Return an empty favicon to suppress browser 404 errors."""
        # 1x1 transparent PNG — avoids 404 without needing a real icon file
        import base64
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQAB"
            "Nl7BcQAAAABJRU5ErkJggg=="
        )
        return Response(content=pixel, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/i18n/{locale}")
    async def get_translations(locale: str) -> JSONResponse:
        """Get translation catalog for a locale."""
        if locale not in supported_locales():
            return JSONResponse(
                {"ok": False, "error": f"Unsupported locale: {locale}"},
                status_code=404,
            )
        catalog = get_catalog(locale)
        return JSONResponse({"ok": True, "locale": locale, "catalog": catalog})

    @app.post("/api/config/check-urls")
    async def check_urls(request: Request) -> JSONResponse:
        """Validate repository URLs without running full analysis."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        locale = _req_locale(request)
        body = await _read_json_body(request)
        repos = body.get("repositories", [])
        results = []
        for r in repos:
            url = (r.get("url") or "").strip()
            name = r.get("name", url)
            if not url:
                results.append({
                    "name": name,
                    "status": "empty",
                    "message": t("api.url_empty", locale=locale),
                })
            elif is_placeholder_url(url):
                results.append({
                    "name": name,
                    "status": "placeholder",
                    "message": t(
                        "api.url_placeholder", locale=locale
                    ),
                })
            elif not any(
                url.startswith(s)
                for s in (
                    "http://", "https://", "git@",
                    "ssh://", "git://", "/",
                )
            ):
                results.append({
                    "name": name,
                    "status": "relative",
                    "message": t(
                        "api.url_relative", locale=locale
                    ),
                })
            else:
                results.append({
                    "name": name,
                    "status": "ok",
                    "message": t("api.url_valid", locale=locale),
                })
        return JSONResponse({"ok": True, "results": results})

    # --- Discovery API ---

    @app.post("/api/discover")
    async def discover_repos(request: Request) -> JSONResponse:
        """Scan root URLs and discover repositories for each layer."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        locale = _req_locale(request)
        body = await _read_json_body(request)
        layers_input = body.get("layers", [])
        global_branch_pattern = body.get("branch_pattern", "release/{YYYY}.{MM}")
        provider_type = body.get("provider", "github")
        github_token = (body.get("github_token") or "").strip()
        gitlab_token = (body.get("gitlab_token") or "").strip()
        warnings: list[str] = []

        # Persist tokens in-memory for subsequent analysis runs
        if isinstance(git_provider, SmartGitProvider) and (github_token or gitlab_token):
            git_provider.update_tokens(
                github_token=github_token or None,
                gitlab_token=gitlab_token or None,
            )

        github = GitHubProvider(token=github_token or None)
        gitlab = GitLabProvider(token=gitlab_token or None)
        result_layers: list[dict[str, Any]] = []

        for layer_def in layers_input:
            layer_id = layer_def.get("id", "")
            layer_label = layer_def.get("label", layer_id)
            root_url = (layer_def.get("root_url") or "").strip().rstrip("/")
            color = layer_def.get("color", "#6366F1")

            layer_result: dict[str, Any] = {
                "id": layer_id,
                "label": layer_label,
                "root_url": root_url,
                "color": color,
                "repos": [],
            }

            if not root_url:
                warnings.append(t("api.layer_no_root", locale=locale, label=layer_label))
                result_layers.append(layer_result)
                continue

            # Route to the right provider — auto-detect from URL,
            # falling back to the global provider_type hint.
            raw_repos: list[dict[str, Any]] = []
            branch_lister = None  # callable(repo_url, timeout) -> list[str]

            # Auto-detect: check hostname to decide provider.
            # "github.com" → GitHub; "gitlab" in hostname → GitLab;
            # otherwise use the global provider_type hint.
            _host = (urlparse(root_url).hostname or "").lower()
            url_is_github = "github.com" in _host
            url_is_gitlab = "gitlab" in _host
            use_gitlab = url_is_gitlab or (
                provider_type == "gitlab" and not url_is_github
            )

            if use_gitlab:
                parsed_gl = parse_gitlab_group(root_url)
                if not parsed_gl:
                    warnings.append(
                        t("api.layer_not_gitlab", locale=locale, label=layer_label, url=root_url)
                    )
                    result_layers.append(layer_result)
                    continue
                api_base, group_path = parsed_gl
                try:
                    raw_repos = await asyncio.to_thread(
                        gitlab.list_group_repos, api_base, group_path, timeout=30
                    )
                except GitAccessError as exc:
                    warnings.append(f"Layer '{layer_label}': {exc}")
                    result_layers.append(layer_result)
                    continue
                except Exception as exc:
                    warnings.append(f"Layer '{layer_label}': Unexpected error — {exc}")
                    result_layers.append(layer_result)
                    continue
                branch_lister = gitlab.list_remote_branches
            else:
                owner = parse_github_owner(root_url)
                if not owner:
                    warnings.append(
                        t("api.layer_not_github", locale=locale, label=layer_label, url=root_url)
                    )
                    result_layers.append(layer_result)
                    continue
                try:
                    raw_repos = await asyncio.to_thread(
                        github.list_org_repos, owner, timeout=30
                    )
                except GitAccessError as exc:
                    warnings.append(f"Layer '{layer_label}': {exc}")
                    result_layers.append(layer_result)
                    continue
                except Exception as exc:
                    warnings.append(f"Layer '{layer_label}': Unexpected error — {exc}")
                    result_layers.append(layer_result)
                    continue
                branch_lister = github.list_remote_branches

            # For each repo, try to detect branches
            for repo_info in raw_repos:
                repo_url = repo_info["url"]
                repo_name = repo_info["name"]

                branches: list[str] = []
                if branch_lister:
                    try:
                        branches = await asyncio.to_thread(
                            branch_lister, repo_url, 15
                        )
                    except Exception as exc:
                        logger.warning("Branch listing failed for %s: %s", repo_url, exc)

                release_branch = None
                for b in branches:
                    if b.startswith("release/") or b.startswith("rel/"):
                        release_branch = b
                        break

                layer_result["repos"].append({
                    "name": repo_name,
                    "url": repo_url,
                    "full_url": repo_url,
                    "default_branch": repo_info["default_branch"],
                    "description": repo_info["description"],
                    "branch_count": len(branches),
                    "release_branch": release_branch,
                    "included": True,
                })

            result_layers.append(layer_result)

        return JSONResponse({
            "ok": True,
            "layers": result_layers,
            "branch_pattern": global_branch_pattern,
            "warnings": warnings,
        })

    # --- ReleasePilot Integration API ---

    @app.get("/api/release-pilot/capabilities")
    async def release_pilot_capabilities() -> JSONResponse:
        """Check ReleasePilot integration availability and capabilities."""
        caps = release_pilot.capabilities
        return JSONResponse({"ok": True, **caps.to_dict()})

    @app.post("/api/release-pilot/validate")
    async def release_pilot_validate(request: Request) -> JSONResponse:
        """Validate release preparation wizard inputs."""
        if not release_pilot.is_available:
            return JSONResponse(
                {"ok": False, "error": "ReleasePilot integration is not installed"},
                status_code=503,
            )
        locale = _req_locale(request)
        body = await _read_json_body(request)
        error_keys = release_pilot.validate(body)
        errors = [t(key, locale=locale) for key in error_keys]
        return JSONResponse({
            "ok": len(errors) == 0,
            "errors": errors,
            "error_keys": error_keys,
        })

    @app.post("/api/release-pilot/prepare")
    async def release_pilot_prepare(request: Request) -> JSONResponse:
        """Execute a release preparation run."""
        if not release_pilot.is_available:
            return JSONResponse(
                {"ok": False, "error": "ReleasePilot integration is not installed"},
                status_code=503,
            )
        locale = _req_locale(request)
        body = await _read_json_body(request)

        # Validate first
        error_keys = release_pilot.validate(body)
        if error_keys:
            errors = [t(key, locale=locale) for key in error_keys]
            return JSONResponse({
                "ok": False,
                "errors": errors,
                "error_keys": error_keys,
            }, status_code=422)

        # Build request
        try:
            repo_url = body["repo_url"].strip()
            _git_token = git_provider.get_token_for_url(repo_url)
            prep_request = ReleasePrepRequest(
                repo_name=body["repo_name"].strip(),
                repo_url=repo_url,
                release_title=body["release_title"].strip(),
                release_version=body["release_version"].strip(),
                from_ref=body.get("from_ref", "").strip(),
                to_ref=body.get("to_ref", "HEAD").strip() or "HEAD",
                audience=AudienceMode(body.get("audience", "changelog")),
                output_format=OutputFormat(body.get("output_format", "markdown")),
                app_name=body.get("app_name", "").strip(),
                include_authors=body.get("include_authors", True),
                include_hashes=body.get("include_hashes", False),
                show_scope=body.get("show_scope", True),
                show_pr_links=body.get("show_pr_links", True),
                group_by_scope=body.get("group_by_scope", False),
                language=body.get("language", locale),
                accent_color=body.get("accent_color", "#FB6400"),
                branch=body.get("branch", "").strip(),
                since_date=body.get("since_date", "").strip(),
                additional_notes=body.get("additional_notes", "").strip(),
                git_token=_git_token,
            )
        except (KeyError, ValueError) as exc:
            return JSONResponse({
                "ok": False,
                "errors": [f"Invalid request: {exc}"],
            }, status_code=400)

        # Execute
        result = await release_pilot.prepare_release(prep_request)
        return JSONResponse({"ok": result.success, **result.to_dict()})

    @app.get("/api/release-pilot/repo-context/{repo_name}")
    async def release_pilot_repo_context(repo_name: str, request: Request) -> JSONResponse:
        """Get repository context data for the release wizard."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        locale = _req_locale(request)
        config = state.get_active_config()

        # Find the repository in config
        repo_config = next((r for r in config.repositories if r.name == repo_name), None)
        if not repo_config:
            return JSONResponse(
                {
                    "ok": False,
                    "error": t(
                        "ui.error.repo_not_in_config",
                        locale=locale,
                        name=repo_name,
                    ),
                },
                status_code=404,
            )

        # Resolve URLs and patterns
        resolved_url = config.resolve_repo_url(repo_config)
        pattern = config.resolve_branch_pattern(repo_config)
        layer = config.get_layer(repo_config.layer)
        layer_label = layer.label if layer else repo_config.layer

        # Get analysis data if available
        analysis_data: dict[str, Any] = {}
        if state.analysis_result:
            analysis = next(
                (a for a in state.analysis_result.analyses if a.name == repo_name), None
            )
            if analysis:
                analysis_data = {
                    "status": analysis.status.value,
                    "status_label": analysis.status.localized_label(locale),
                    "expected_branch": analysis.expected_branch_name,
                    "actual_branch": (
                        analysis.branch.name
                        if analysis.branch and analysis.branch.exists
                        else ""
                    ),
                    "branch_exists": analysis.branch_exists,
                    "repo_default_branch": (
                        analysis.branch.repo_default_branch
                        if analysis.branch
                        else repo_config.default_branch
                    ),
                    "repo_description": analysis.branch.repo_description if analysis.branch else "",
                    "repo_visibility": analysis.branch.repo_visibility if analysis.branch else "",
                    "repo_web_url": analysis.branch.repo_web_url if analysis.branch else "",
                    "repo_owner": analysis.branch.repo_owner if analysis.branch else "",
                }

        context = {
            "ok": True,
            "name": repo_config.name,
            "url": resolved_url,
            "layer": repo_config.layer,
            "layer_label": layer_label,
            "default_branch": repo_config.default_branch,
            "branch_pattern": pattern,
            "release_name": config.release.name,
            "release_month": config.release.target_month,
            "release_year": config.release.target_year,
            "notes": repo_config.notes or "",
            **analysis_data,
        }

        return JSONResponse(context)

    # --- Release Calendar API ---

    @app.get("/api/release-calendar")
    async def get_release_calendar() -> JSONResponse:
        """Get release calendar data from draft config."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        draft = state.get_draft()
        cal = draft.get("release_calendar", {})
        return JSONResponse({"ok": True, "release_calendar": cal})

    @app.put("/api/release-calendar")
    async def update_release_calendar(request: Request) -> JSONResponse:
        """Save release calendar data into draft config and persist."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        body = await _read_json_body(request)
        cal_data = body.get("release_calendar") or body
        # Merge into draft config
        draft = state.get_draft()
        draft["release_calendar"] = cal_data
        errors = state.update_draft(draft)
        if errors:
            return JSONResponse({"ok": False, "errors": errors})
        save_errors = state.save_config()
        if save_errors:
            return JSONResponse({"ok": False, "errors": save_errors})
        return JSONResponse({
            "ok": True,
            "has_unsaved_changes": False,
            "etag": state.config_state.config_etag,
        })

    @app.post("/api/release-calendar/import")
    async def import_release_calendar(request: Request) -> JSONResponse:
        """Import a release calendar from uploaded JSON with strict validation.

        Validates the import payload against the calendar schema before applying.
        If a calendar already exists, requires explicit confirmation via 'confirm_replace'.
        """
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        from releaseboard.calendar.validator import (
            MAX_IMPORT_SIZE_BYTES,
            calendar_has_data,
            validate_calendar_import,
        )

        # Check payload size before parsing
        content_length = request.headers.get("content-length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > MAX_IMPORT_SIZE_BYTES
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "errors": [
                        f"Import payload too large (max {MAX_IMPORT_SIZE_BYTES // 1024}KB)"
                    ],
                },
                status_code=413,
            )

        body = await _read_json_body(request)
        cal_data = body.get("release_calendar") or body
        confirm = body.get("confirm_replace", False)

        # Strict validation
        errors = validate_calendar_import(cal_data)
        if errors:
            return JSONResponse(
                {"ok": False, "errors": errors, "validation_failed": True},
                status_code=422,
            )

        # Check for existing calendar
        draft = state.get_draft()
        existing_cal = draft.get("release_calendar", {})
        if calendar_has_data(existing_cal) and not confirm:
            return JSONResponse({
                "ok": False,
                "needs_confirmation": True,
                "message": (
                    "A release calendar already exists."
                    " Set 'confirm_replace' to true to replace it."
                ),
            })

        # Apply import
        # Ensure defaults for missing optional fields
        if "display" not in cal_data:
            cal_data["display"] = {
                "show_notes": True,
                "show_weekdays": True,
                "show_quarter_headers": True,
            }
        if "events" not in cal_data:
            cal_data["events"] = []
        if "months" not in cal_data:
            cal_data["months"] = []

        draft["release_calendar"] = cal_data
        schema_errors = state.update_draft(draft)
        if schema_errors:
            return JSONResponse(
                {"ok": False, "errors": schema_errors},
                status_code=422,
            )
        save_errors = state.save_config()
        if save_errors:
            return JSONResponse({"ok": False, "errors": save_errors})

        return JSONResponse({
            "ok": True,
            "has_unsaved_changes": False,
            "etag": state.config_state.config_etag,
            "imported_events": len(cal_data.get("events", [])),
            "imported_months": len(cal_data.get("months", [])),
        })

    @app.get("/api/release-calendar/schema")
    async def get_calendar_schema() -> JSONResponse:
        """Return the calendar import schema definition and example for in-app guidance."""
        from releaseboard.calendar.validator import (
            get_import_schema_definition,
            get_import_schema_example,
        )

        return JSONResponse({
            "ok": True,
            "schema": get_import_schema_definition(),
            "example": get_import_schema_example(),
        })

    @app.get("/api/release-calendar/milestones")
    async def get_calendar_milestones() -> JSONResponse:
        """Return upcoming milestone dates with days remaining for dashboard display."""
        if state is None:
            return JSONResponse({"ok": False, "error": "No configuration loaded"}, status_code=503)
        from releaseboard.calendar.validator import get_upcoming_milestones

        draft = state.get_draft()
        cal = draft.get("release_calendar", {})
        milestones = get_upcoming_milestones(cal)
        return JSONResponse({"ok": True, "milestones": milestones})

    return app


_sse_event_counter = itertools.count(1)


def _sse_format(event: str, data: Any) -> str:
    """Format a Server-Sent Event message with unique event ID."""
    event_id = next(_sse_event_counter)
    try:
        json_data = json.dumps(data, default=str)
    except Exception:
        logger.warning("SSE data serialization failed for event '%s'", event, exc_info=True)
        json_data = json.dumps({"error": "serialization_failed"})
    return f"id: {event_id}\nevent: {event}\ndata: {json_data}\n\n"

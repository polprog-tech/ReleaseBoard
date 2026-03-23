"""Application state management for the web layer.

Manages the three config tiers:
- persisted: saved to disk
- active: used for the last analysis
- draft: current unsaved UI edits

And analysis state:
- progress: real-time analysis progress
- result: latest analysis result
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from releaseboard.application.service import AnalysisProgress, AnalysisResult
from releaseboard.config.loader import load_config
from releaseboard.config.schema import (
    validate_config,
    validate_layer_references,
)
from releaseboard.shared.logging import get_logger

if TYPE_CHECKING:
    from releaseboard.config.models import AppConfig

logger = get_logger("web.state")

# Fields that must be integers in the config schema
_INTEGER_PATHS: list[tuple[str, ...]] = [
    ("release", "target_month"),
    ("release", "target_year"),
    ("settings", "stale_threshold_days"),
    ("settings", "timeout_seconds"),
    ("settings", "max_concurrent"),
]


_SECTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "branding": {
        "title": "ReleaseBoard",
        "subtitle": "Release Readiness Dashboard",
        "company": "",
        "primary_color": "#fb6400",
        "secondary_color": "#002754e6",
        "tertiary_color": "#10b981",
        "logo_path": None,
    },
    "settings": {
        "stale_threshold_days": 14,
        "output_path": "output/dashboard.html",
        "theme": "system",
        "verbose": False,
        "timeout_seconds": 30,
        "max_concurrent": 5,
        "repository_root_url": "",
    },
    "layout": {
        "default_template": "default",
        "section_order": [
            "score", "metrics", "charts", "filters",
            "attention", "layer-*", "summary",
        ],
        "enable_drag_drop": True,
    },
}


def fill_config_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure optional config sections have proper defaults.

    Fills in missing top-level sections (branding, settings, layout)
    and backfills missing fields within existing sections so that schema
    validation never fails due to absent defaults.
    """
    if not isinstance(data, dict):
        return data
    for section, defaults in _SECTION_DEFAULTS.items():
        if section not in data:
            data[section] = dict(defaults)
        else:
            existing = data[section]
            if isinstance(existing, dict):
                for key, default_val in defaults.items():
                    # Only backfill truly empty values for fields with
                    # schema constraints (pattern, minimum, enum).
                    # Note: 0 is a valid value for numeric fields — only
                    # backfill None and empty strings.
                    if (key in (
                        "secondary_color", "primary_color", "tertiary_color",
                        "stale_threshold_days", "timeout_seconds",
                        "max_concurrent", "theme",
                    ) and existing.get(key) in ("", None)) or key not in existing:
                        existing[key] = default_val
    # Ensure layers is at least an empty list
    if "layers" not in data:
        data["layers"] = []
    # Ensure release_calendar has defaults
    if "release_calendar" not in data:
        from datetime import datetime as _dt

        data["release_calendar"] = {
            "name": "",
            "year": _dt.now().year,
            "notes": "",
            "months": [],
            "events": [],
            "display": {
                "show_notes": True,
                "show_weekdays": True,
                "show_quarter_headers": True,
            },
        }
    else:
        cal = data["release_calendar"]
        if "events" not in cal:
            cal["events"] = []
        if "months" not in cal:
            cal["months"] = []
        if "display" not in cal:
            cal["display"] = {
                "show_notes": True,
                "show_weekdays": True,
                "show_quarter_headers": True,
            }
    # Auto-generate layer definitions from repo references if layers is empty
    if not data["layers"]:
        _auto_generate_layers(data)
    return data


_DEFAULT_LAYER_COLORS = {
    "ui": "#7C5CFC",
    "api": "#0EA47A",
    "db": "#E08C00",
    "infra": "#E0544E",
    "mobile": "#A855F7",
    "web": "#2B8DE6",
    "backend": "#0D9488",
    "frontend": "#E55DA2",
}

_FALLBACK_COLORS = [
    "#7C5CFC", "#0EA47A", "#E08C00", "#E0544E",
    "#A855F7", "#2B8DE6", "#0D9488", "#E55DA2",
]

# Keys whose values should be redacted on export
_SENSITIVE_KEYS = frozenset({
    "github_token", "gitlab_token", "token", "secret", "password",
    "api_key", "private_key", "access_token",
})


def _sanitize_secrets(data: Any) -> None:
    """Recursively redact sensitive values in a config dict (in-place)."""
    if isinstance(data, dict):
        for key in data:
            if key.lower() in _SENSITIVE_KEYS and isinstance(data[key], str) and data[key]:
                data[key] = "***REDACTED***"
            else:
                _sanitize_secrets(data[key])
    elif isinstance(data, list):
        for item in data:
            _sanitize_secrets(item)


def _auto_generate_layers(data: dict[str, Any]) -> None:
    """Create layer definitions from unique layer IDs referenced by repos."""
    existing_ids = {
        layer["id"]
        for layer in data.get("layers", [])
        if isinstance(layer, dict) and "id" in layer
    }
    seen: dict[str, None] = {}
    for repo in data.get("repositories", []):
        lid = repo.get("layer", "")
        if lid and lid not in seen and lid not in existing_ids:
            seen[lid] = None
    for i, lid in enumerate(seen):
        color = _DEFAULT_LAYER_COLORS.get(lid, _FALLBACK_COLORS[i % len(_FALLBACK_COLORS)])
        data["layers"].append({
            "id": lid,
            "label": lid.upper(),
            "color": color,
            "order": i,
        })


def normalize_config_types(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce string-typed numbers to ints where the schema expects integers.

    HTML forms and JSON editors sometimes produce strings for numeric fields.
    This normalizes them before validation to prevent type errors.
    """
    if not isinstance(data, dict):
        return data or {}
    for path in _INTEGER_PATHS:
        obj = data
        for key in path[:-1]:
            if not isinstance(obj, dict) or key not in obj:
                break
            obj = obj[key]
        else:
            final_key = path[-1]
            if isinstance(obj, dict) and final_key in obj:
                val = obj[final_key]
                if isinstance(val, str):
                    with contextlib.suppress(ValueError, TypeError):
                        obj[final_key] = int(val)
    # Layer order fields
    for layer in data.get("layers", []):
        if isinstance(layer, dict) and "order" in layer:
            val = layer["order"]
            if isinstance(val, str):
                with contextlib.suppress(ValueError, TypeError):
                    layer["order"] = int(val)
    return data


@dataclass
class ConfigState:
    """Three-tier config state: persisted → active → draft."""

    persisted_raw: dict[str, Any]
    draft_raw: dict[str, Any]
    persisted: AppConfig
    active: AppConfig | None = None
    config_path: Path | None = None

    @property
    def has_unsaved_changes(self) -> bool:
        return json.dumps(self.draft_raw, sort_keys=True) != json.dumps(
            self.persisted_raw, sort_keys=True
        )

    @property
    def config_etag(self) -> str:
        """Compute ETag from persisted config for optimistic concurrency control."""
        raw = json.dumps(self.persisted_raw, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "persisted": self.persisted_raw,
            "draft": self.draft_raw,
            "has_unsaved_changes": self.has_unsaved_changes,
        }


class AppState:
    """Mutable application state for the web server."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in config file {config_path}: {exc}"
            ) from exc
        fill_config_defaults(raw)

        self.config_state = ConfigState(
            persisted_raw=json.loads(json.dumps(raw)),
            draft_raw=json.loads(json.dumps(raw)),
            persisted=load_config(config_path),
            config_path=config_path,
        )
        self.config_state.active = self.config_state.persisted

        self.analysis_progress = AnalysisProgress()
        self.analysis_result: AnalysisResult | None = None
        self.analysis_lock = asyncio.Lock()
        self._active_draft_hash: str | None = None

        # SSE: subscribers receive events via their own queue
        self._sse_subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    # --- Config Operations ---

    def get_draft(self) -> dict[str, Any]:
        return self.config_state.draft_raw

    def update_draft(self, data: dict[str, Any]) -> list[str]:
        """Update draft config. Returns validation errors (empty = valid)."""
        data = normalize_config_types(data)
        fill_config_defaults(data)
        errors = validate_config(data)
        ref_errors = validate_layer_references(data)
        all_errors = errors + ref_errors
        # Always update the draft even if invalid (so UI reflects what user typed)
        self.config_state.draft_raw = data
        return all_errors

    def validate_draft(self) -> list[str]:
        errors = validate_config(self.config_state.draft_raw)
        errors += validate_layer_references(self.config_state.draft_raw)
        return errors

    def save_config(self) -> list[str]:
        """Persist draft to disk atomically. Returns errors if draft is invalid."""
        errors = self.validate_draft()
        if errors:
            return errors

        json_text = json.dumps(self.config_state.draft_raw, indent=2, ensure_ascii=False)

        # Create backup of current config before overwriting
        if self.config_path and self.config_path.exists():
            backup_path = self.config_path.with_suffix(".json.bak")
            try:
                import shutil
                shutil.copy2(self.config_path, backup_path)
            except OSError as exc:
                logger.warning("Failed to create config backup: %s", exc)

        # Atomic write: write to temp file in same directory, then rename
        import os as _os
        import tempfile

        tmp_path = None
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=self.config_path.parent,
                prefix=".releaseboard_",
                suffix=".tmp",
            )
            # Close the fd immediately — we use Path.write_text below
            _os.close(tmp_fd)
            tmp_path = Path(tmp_name)
            tmp_path.write_text(json_text, encoding="utf-8")
            tmp_path.replace(self.config_path)
            tmp_path = None  # rename succeeded, no cleanup needed
        except Exception as exc:
            logger.error("Failed to save config atomically: %s", exc)
            return ["Failed to write config file. Check server logs for details."]
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        self.config_state.persisted_raw = json.loads(json.dumps(self.config_state.draft_raw))
        self.config_state.persisted = load_config(self.config_path)
        self.config_state.active = self.config_state.persisted
        logger.info("Config saved to %s", self.config_path)
        return []

    def reset_draft(self) -> None:
        """Reset draft to last persisted version."""
        self.config_state.draft_raw = json.loads(json.dumps(self.config_state.persisted_raw))

    def import_config(self, data: dict[str, Any]) -> list[str]:
        """Import config from uploaded JSON."""
        return self.update_draft(data)

    def export_config(self) -> dict[str, Any]:
        """Export current draft config with sensitive values redacted."""
        import copy
        data = copy.deepcopy(self.config_state.draft_raw)
        _sanitize_secrets(data)
        return data

    def get_active_config(self) -> AppConfig:
        """Get the config to use for analysis. Uses draft if valid, else persisted.

        Caches the built config and only rebuilds when the draft changes.
        """
        errors = self.validate_draft()
        if not errors:
            draft_json = json.dumps(self.config_state.draft_raw, sort_keys=True)
            draft_hash = hashlib.sha256(draft_json.encode()).hexdigest()[:16]
            if (
                self._active_draft_hash == draft_hash
                and self.config_state.active is not None
            ):
                return self.config_state.active
            # Build AppConfig from draft via temp file
            temp_path: Path | None = None
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False, encoding="utf-8"
                ) as f:
                    json.dump(self.config_state.draft_raw, f)
                    temp_path = Path(f.name)
                config = load_config(temp_path)
                self.config_state.active = config
                self._active_draft_hash = draft_hash
                return config
            except Exception as exc:
                logger.warning("Failed to build config from draft, using persisted: %s", exc)
                return self.config_state.persisted
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
        return self.config_state.persisted

    # --- SSE Operations ---

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Create a new SSE subscriber queue."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._sse_subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an SSE subscriber."""
        with contextlib.suppress(ValueError):
            self._sse_subscribers.remove(queue)

    async def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Broadcast an event to all SSE subscribers."""
        message = {"event": event_type, "data": data}
        dead_queues: list[asyncio.Queue[dict[str, Any]]] = []
        # Snapshot subscriber list to avoid concurrent-modification issues
        for queue in list(self._sse_subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                dead_queues.append(queue)
                logger.warning(
                    "Dropping SSE subscriber (queue full, %d events buffered)",
                    queue.maxsize,
                )
        if dead_queues:
            dead_set = set(id(q) for q in dead_queues)
            self._sse_subscribers = [
                q for q in self._sse_subscribers if id(q) not in dead_set
            ]

    async def on_analysis_progress(
        self, event_type: str, progress: AnalysisProgress
    ) -> None:
        """Callback for AnalysisService — broadcasts progress via SSE."""
        self.analysis_progress = progress
        await self.broadcast(event_type, progress.to_dict())

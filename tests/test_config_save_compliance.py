"""Tests for config save correctness — ETag conflict detection, save compliance,
and configuration persistence integrity."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from releaseboard.config.loader import _resolve_env_vars
from releaseboard.config.models import (
    LayoutConfig,
)
from releaseboard.git.gitlab_provider import GitLabProvider
from releaseboard.git.provider import GitProvider
from releaseboard.shared.logging import StructuredFormatter, get_logger
from releaseboard.web.state import (
    AppState,
)

if TYPE_CHECKING:
    from pathlib import Path

MINIMAL_CONFIG: dict[str, Any] = {
    "release": {
        "name": "Test Release",
        "target_month": 3,
        "target_year": 2025,
        "branch_pattern": "release/{MM}.{YYYY}",
    },
    "repositories": [
        {"name": "svc-a", "url": "https://git.example.com/a.git", "layer": "api"},
    ],
    "layers": [
        {"id": "api", "label": "API", "color": "#10B981", "order": 0},
    ],
    "branding": {"title": "Test", "primary_color": "#4F46E5", "secondary_color": "#002754e6"},
    "settings": {"stale_threshold_days": 14, "theme": "system"},
}


def _write_config(tmp_path: Path, data: dict | None = None) -> Path:
    config_path = tmp_path / "test_config.json"
    config_path.write_text(json.dumps(data or MINIMAL_CONFIG), encoding="utf-8")
    return config_path


def _create_app_for_test(tmp_path: Path, data: dict | None = None):
    """Create a FastAPI app for testing."""
    from releaseboard.web.server import create_app

    config_path = _write_config(tmp_path, data)
    return create_app(config_path), config_path


class TestGitLabProviderABC:
    """Scenarios for GitLabProvider ABC compliance."""

    def test_is_subclass_of_git_provider(self):
        """GIVEN GitLabProvider class."""
        cls = GitLabProvider

        """WHEN checking class hierarchy."""
        result = issubclass(cls, GitProvider)

        """THEN it is a subclass of GitProvider."""
        assert result

    def test_has_get_branch_info_method(self):
        """GIVEN a GitLabProvider instance."""
        provider = GitLabProvider(token="fake")

        """WHEN inspecting its methods."""
        has_method = hasattr(provider, "get_branch_info")
        is_callable = callable(provider.get_branch_info)

        """THEN it has a callable get_branch_info method."""
        assert has_method
        assert is_callable


class TestLayoutConfig:
    """Scenarios for layout config loading."""

    def test_layout_config_model_defaults(self):
        """GIVEN default LayoutConfig."""
        layout = LayoutConfig()

        """WHEN inspecting default values."""
        template = layout.default_template
        order = layout.section_order
        drag = layout.enable_drag_drop

        """THEN it has sensible defaults."""
        assert template == "default"
        assert isinstance(order, tuple)
        assert drag is True


class TestEnvVarResolution:
    """Scenarios for env var resolution."""

    def test_resolved_env_var_replaced(self):
        """GIVEN a string with ${VAR} and VAR is set."""
        with patch.dict(os.environ, {"TEST_HOST_XYZ": "git.example.com"}):
            """WHEN _resolve_env_vars is called."""
            result = _resolve_env_vars("https://${TEST_HOST_XYZ}/repo")

            """THEN the value is substituted."""
            assert result == "https://git.example.com/repo"


class TestSSEDisconnect:
    """Scenarios for SSE disconnect handling."""

    @pytest.mark.asyncio
    async def test_sse_subscribe_and_unsubscribe(self, tmp_path: Path):
        """GIVEN SSE subscriber system."""
        config_path = _write_config(tmp_path)
        state = AppState(config_path)

        """WHEN subscribe/unsubscribe is called."""
        queue = state.subscribe()
        initial_count = len(state._sse_subscribers)
        state.unsubscribe(queue)

        """THEN subscribers are properly tracked."""
        assert initial_count == 1
        assert len(state._sse_subscribers) == 0


class TestStructuredLogging:
    """Scenarios for structured logging."""

    def test_get_logger_uses_structured_formatter(self):
        """GIVEN get_logger function."""
        import logging

        logger = get_logger("test_module")  # noqa: F841

        """WHEN creating a logger."""
        # Handler lives on the root 'releaseboard' logger; child loggers propagate.
        root = logging.getLogger("releaseboard")
        handlers = root.handlers

        """THEN it uses StructuredFormatter."""
        assert len(handlers) > 0
        assert isinstance(handlers[0].formatter, StructuredFormatter)

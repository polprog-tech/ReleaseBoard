"""CORS origin configuration

Provides restricted localhost origins by default (since ReleaseBoard is a
local dashboard tool), with an environment variable override for custom
deployments.
"""

from __future__ import annotations

import os

_DEFAULT_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def get_cors_origins() -> list[str]:
    """Return the list of allowed CORS origins.

    Reads ``RELEASEBOARD_CORS_ORIGINS`` (comma-separated) from the environment.
    Falls back to localhost defaults when the variable is unset or empty.
    """
    env_value = os.environ.get("RELEASEBOARD_CORS_ORIGINS", "")
    origins = [o.strip() for o in env_value.split(",") if o.strip()]
    return origins or list(_DEFAULT_ORIGINS)

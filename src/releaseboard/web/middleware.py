"""HTTP middleware for security, observability, and reliability.

All middleware use the pure ASGI interface (not BaseHTTPMiddleware) to ensure
full compatibility with streaming responses (SSE) and avoid response buffering.
"""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

from releaseboard.shared.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = get_logger("web.middleware")


def _get_header(scope: Scope, name: str) -> str:
    """Extract a header value from ASGI scope (case-insensitive)."""
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == target:
            return value.decode("latin-1")
    return ""


def _get_client_ip(scope: Scope) -> str:
    """Extract client IP from ASGI scope."""
    client = scope.get("client")
    return client[0] if client else "unknown"


class SecurityHeadersMiddleware:
    """Add security response headers to all HTTP responses.

    Adds: X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
    Permissions-Policy, Content-Security-Policy.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # Allow embedding in iframes when integrated into a portal shell.
        # Set RELEASEBOARD_ALLOW_FRAMING=true to relax frame-ancestors.
        self._allow_framing = os.getenv("RELEASEBOARD_ALLOW_FRAMING", "").lower() in (
            "1", "true", "yes",
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def add_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["x-content-type-options"] = "nosniff"
                if self._allow_framing:
                    headers["x-frame-options"] = "SAMEORIGIN"
                else:
                    headers["x-frame-options"] = "DENY"
                headers["referrer-policy"] = "strict-origin-when-cross-origin"
                headers["permissions-policy"] = (
                    "camera=(), microphone=(), geolocation=()"
                )
                headers["x-xss-protection"] = "1; mode=block"
                if "content-security-policy" not in headers:
                    frame_policy = "'self'" if self._allow_framing else "'none'"
                    headers["content-security-policy"] = (
                        "default-src 'self'; "
                        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                        "img-src 'self' data:; "
                        "connect-src 'self' https://cdn.jsdelivr.net "
                        "https://fonts.googleapis.com https://fonts.gstatic.com; "
                        "font-src 'self' https://fonts.gstatic.com data:; "
                        f"frame-ancestors {frame_policy}"
                    )
            await send(message)

        await self.app(scope, receive, add_headers)


class RequestLoggingMiddleware:
    """Log HTTP requests with method, path, status code, and duration."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        status_code = 0

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            method = scope.get("method", "-")
            path = scope.get("path", "-")
            logger.info(
                "%s %s %d %.1fms", method, path, status_code, duration_ms,
            )


class RateLimitMiddleware:
    """Per-IP rate limiting using a fixed-window counter.

    Default: 120 requests/minute general, 5 requests/minute for analysis.
    """

    def __init__(
        self,
        app: ASGIApp,
        requests_per_minute: int = 120,
        analysis_per_minute: int = 5,
    ) -> None:
        self.app = app
        self._rpm = requests_per_minute
        self._analysis_rpm = analysis_per_minute
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._analysis_windows: dict[str, list[float]] = defaultdict(list)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_ip = _get_client_ip(scope)
        path = scope.get("path", "")
        method = scope.get("method", "")
        now = time.monotonic()

        # Stricter limit for analysis trigger
        if path == "/api/analyze" and method == "POST":
            w = self._analysis_windows[client_ip]
            w[:] = [t for t in w if now - t < 60]
            if len(w) >= self._analysis_rpm:
                resp = JSONResponse(
                    {"ok": False, "error": "Rate limit exceeded for analysis"},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
                await resp(scope, receive, send)
                return
            w.append(now)

        # General rate limit
        w = self._windows[client_ip]
        w[:] = [t for t in w if now - t < 60]
        if len(w) >= self._rpm:
            resp = JSONResponse(
                {"ok": False, "error": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
            await resp(scope, receive, send)
            return
        w.append(now)

        # Prevent unbounded memory growth from tracking too many IPs
        if len(self._windows) > 10_000:
            cutoff = now - 60
            stale_ips = [
                ip for ip, times in self._windows.items()
                if not times or times[-1] < cutoff
            ]
            for ip in stale_ips:
                del self._windows[ip]
                self._analysis_windows.pop(ip, None)
            # If pruning didn't free enough, force-clear as last resort
            if len(self._windows) > 10_000:
                self._windows.clear()
                self._analysis_windows.clear()

        await self.app(scope, receive, send)


class CSRFMiddleware:
    """CSRF protection via Origin header validation with defense-in-depth.

    For state-changing requests (POST/PUT/DELETE):
    - If Origin is present: validates it matches the request Host.
    - If Origin is absent: requires ``X-Requested-With: XMLHttpRequest``
      header (which cannot be sent cross-origin without CORS preflight),
      or validates the Referer header against the Host.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        # Exempt health and SSE endpoints
        path = scope.get("path", "")
        if path.startswith("/health/") or path.endswith("/stream"):
            await self.app(scope, receive, send)
            return

        origin = _get_header(scope, "origin")
        host = _get_header(scope, "host")

        # If Origin is present and Host is known, validate
        if origin and host:
            origin_host = urlparse(origin).netloc
            if origin_host != host:
                resp = JSONResponse(
                    {"ok": False, "error": "CSRF validation failed: origin mismatch"},
                    status_code=403,
                )
                await resp(scope, receive, send)
                return
        elif not origin:
            # Defense-in-depth: require X-Requested-With for requests without Origin.
            # Browsers cannot send this header cross-origin without CORS preflight.
            xrw = _get_header(scope, "x-requested-with")
            if xrw != "XMLHttpRequest" and not path.startswith("/api/health"):
                # Fall back to Referer check for same-origin form submissions
                referer = _get_header(scope, "referer")
                if referer and host and (
                    not referer.startswith(f"http://{host}")
                    and not referer.startswith(f"https://{host}")
                ):
                        resp = JSONResponse(
                            {"ok": False, "error": "CSRF validation failed"},
                            status_code=403,
                        )
                        await resp(scope, receive, send)
                        return

        await self.app(scope, receive, send)


class APIKeyMiddleware:
    """Optional API key authentication for state-changing endpoints.

    Enabled when RELEASEBOARD_API_KEY environment variable is set.
    GET requests and health endpoints are always allowed without a key.
    """

    def __init__(self, app: ASGIApp, api_key: str | None = None) -> None:
        self.app = app
        self._api_key = api_key or os.environ.get("RELEASEBOARD_API_KEY", "")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self._api_key:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "")

        # Allow read-only and health endpoints without key
        if method == "GET" or path.startswith("/health/"):
            await self.app(scope, receive, send)
            return

        provided = _get_header(scope, "x-api-key")
        if not hmac.compare_digest(provided.encode(), self._api_key.encode()):
            resp = JSONResponse(
                {"ok": False, "error": "Invalid or missing API key"},
                status_code=401,
            )
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)

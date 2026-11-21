import hmac
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import config as cfgmod
from .routes import register_routes

# State-changing methods get the CSRF origin check (defect D28).
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
# Cookie that keeps a browser logged in after an explicit ?token=/header
# validation (defect D28).
_TOKEN_COOKIE = "bm_token"


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def _origin_matches_host(request: Request, header_value: str) -> bool:
    """True when an Origin/Referer URL points at this server's own Host."""
    try:
        origin_netloc = (urlparse(header_value).netloc or "").lower()
    except ValueError:
        return False
    host = (request.headers.get("host") or "").lower()
    return bool(origin_netloc) and origin_netloc == host


def _security_middleware(app: FastAPI, token: str | None) -> None:
    """CSRF guard (always on) + bearer/page token auth (when web.token is set).

    CSRF (defect D28): a state-changing request (POST/PUT/DELETE/PATCH) that
    carries a cross-site Origin/Referer or ``Sec-Fetch-Site: cross-site`` is
    refused with 403. A browser always sends one of those headers on a
    cross-site request, so this kills drive-by POSTs from web pages; curl /
    CLI / server-to-server callers send none and stay unaffected. The check
    applies with or without a configured token, because the token-less
    dashboard is deliberately reachable and its no-body POSTs were fully
    drive-by-able (report_security CSRF class 1).

    Token (defect D28): /api/* keeps the exact 'Bearer ' scheme, now with a
    timing-safe comparison. When a token IS configured, every other route
    except /static is gated too — the HTML pages used to serve full data
    with a token armed (report_security SEC-10). Pages accept ``?token=``,
    an ``X-BackupMe-Token`` header, or the ``bm_token`` cookie that is set
    (HttpOnly, SameSite=Strict, Path=/) after a successful query/header
    validation so a browser stays logged in. Without a configured token,
    pages remain open (unchanged behavior).
    """

    @app.middleware("http")
    async def security_guard(request: Request, call_next):
        # CSRF: cross-site state-changing requests never reach a handler.
        if request.method in _STATE_CHANGING_METHODS:
            sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
            if sec_fetch_site == "cross-site":
                return JSONResponse(
                    {"detail": "cross-site request rejected"}, status_code=403)
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and not _origin_matches_host(request, origin):
                return JSONResponse(
                    {"detail": "cross-site request rejected"}, status_code=403)

        if not token:
            return await call_next(request)

        supplied = None
        via_query_or_header = False
        path = request.url.path
        if path.startswith("/api/"):
            # API: exact 'Bearer ' scheme only (qa auth matrix).
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[len("Bearer "):]
        elif path.startswith("/static"):
            return await call_next(request)
        else:
            # Pages: query param, dedicated header, or the session cookie.
            query_token = request.query_params.get("token")
            header_token = request.headers.get("x-backupme-token")
            if query_token or header_token:
                supplied = query_token or header_token
                via_query_or_header = True
            else:
                supplied = request.cookies.get(_TOKEN_COOKIE)

        # compare_digest on bytes: the str form raises on non-ASCII input.
        if supplied is None or not hmac.compare_digest(
                supplied.encode("utf-8"), token.encode("utf-8")):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        response: Response = await call_next(request)
        if via_query_or_header:
            # Persist a validated browser session (never set for the API,
            # which stays bearer-only).
            response.set_cookie(_TOKEN_COOKIE, token, httponly=True,
                                samesite="strict", path="/")
        return response


def create_app(config: cfgmod.Config) -> FastAPI:
    app = FastAPI(title="BackupMe Dashboard", version="2.0.0")
    app.add_middleware(NoCacheMiddleware)
    register_routes(app, config)
    _security_middleware(app, getattr(config.web, "token", "") or None)
    # Pre-compute the dashboard dedup-savings aggregate so the first visitor
    # never pays the minutes-class cold scan on the live store (no-op under
    # pytest). Must run AFTER register_routes, which resets the caches.
    from .routes.common import warm_dedup_savings_cache
    warm_dedup_savings_cache()
    return app


def start_web(config: cfgmod.Config, host: str = "127.0.0.1", port: int = 8080):
    app = create_app(config)
    uvicorn.run(app, host=host, port=port, log_level="info")

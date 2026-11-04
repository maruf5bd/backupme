import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import config as cfgmod
from .routes import register_routes


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def _auth_middleware(app: FastAPI, token: str | None) -> None:
    """Bearer-token guard over the admin API when a web.token is configured.

    Token-less mode (default) keeps the dashboard open — that remains safe
    today because the CLI binds loopback-only by default and warns when
    bound to a non-loopback address. The middleware is registered last so
    it runs before any route handler.
    """
    if not token:
        return

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != token:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


def create_app(config: cfgmod.Config) -> FastAPI:
    app = FastAPI(title="BackupMe Dashboard", version="2.0.0")
    app.add_middleware(NoCacheMiddleware)
    register_routes(app, config)
    _auth_middleware(app, getattr(config.web, "token", "") or None)
    return app


def start_web(config: cfgmod.Config, host: str = "127.0.0.1", port: int = 8080):
    app = create_app(config)
    uvicorn.run(app, host=host, port=port, log_level="info")
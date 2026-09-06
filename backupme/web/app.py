import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .. import config as cfgmod
from .routes import register_routes


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def create_app(config: cfgmod.Config) -> FastAPI:
    app = FastAPI(title="BackupMe Dashboard", version="2.0.0")
    app.add_middleware(NoCacheMiddleware)
    register_routes(app, config)
    return app


def start_web(config: cfgmod.Config, host: str = "127.0.0.1", port: int = 8080):
    app = create_app(config)
    uvicorn.run(app, host=host, port=port, log_level="info")
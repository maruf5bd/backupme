import uvicorn
from fastapi import FastAPI

from .. import config as cfgmod
from .routes import register_routes


def create_app(config: cfgmod.Config) -> FastAPI:
    app = FastAPI(title="BackupMe Dashboard", version="2.0.0")
    register_routes(app, config)
    return app


def start_web(config: cfgmod.Config, host: str = "127.0.0.1", port: int = 8080):
    app = create_app(config)
    uvicorn.run(app, host=host, port=port, log_level="info")
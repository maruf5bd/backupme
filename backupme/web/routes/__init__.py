"""BackupMe dashboard routes (backlog B05 package split — complete).

`register_routes(app, config)` remains the single public entry point: it
builds the shared RouteContext (config + templates) and calls each domain
module's register(app, ctx):

- pages      dashboard/settings/database/reports/health pages
- files      browse/search/download/restore/delete/verify
- backends   backend list/add/remove/usage/rescan/rebalance/drain
- dbops      db check/snapshot/restore/rebuild/oplog
- ingest     cPanel ingest drop/status/stream
- backup     backup start/scan (+SSE)
- config_api config get/set/import/export/sources
- scheduler  daemon start/stop/status, scheduler config, server restart
- health     Data Health page + /api/health/* family (scan, purge)
- misc       status/logs/report/search/encrypt/activity/stats/convert/...

Shared plumbing lives in common.py (RouteContext, config/backend/search
helpers, the one background-task runner, op_log helpers).

Compatibility note: tests patch ``backupme.web.routes.recover_mod`` (the
shared core module object), so the package re-exports it under the same
name the old monolith used.
"""
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backupme.core import recover as recover_mod  # monkeypatch surface (see docstring)

from .common import RouteContext, reset_background_state
from . import pages
from . import files
from . import backends
from . import dbops
from . import ingest
from . import backup
from . import config_api
from . import scheduler
from . import health
from . import misc


def register_routes(app, config):
    templates_dir = Path(__file__).resolve().parent.parent / "templates"
    static_dir = Path(__file__).resolve().parent.parent / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Fresh app instance = fresh background-task state (the old monolith
    # scoped these dicts inside register_routes; tests pin that isolation).
    reset_background_state()

    ctx = RouteContext(app=app, config=config, templates=templates)
    pages.register(app, ctx)
    files.register(app, ctx)
    backends.register(app, ctx)
    dbops.register(app, ctx)
    ingest.register(app, ctx)
    backup.register(app, ctx)
    config_api.register(app, ctx)
    scheduler.register(app, ctx)
    health.register(app, ctx)
    misc.register(app, ctx)

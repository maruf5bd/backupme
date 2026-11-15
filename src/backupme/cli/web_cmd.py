import click

from .base import cli


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8080, help="Port")
@click.option("--token", default=None, help="Bearer token for /api/* endpoints "
                   "(defaults to config web.token; empty = open dashboard)")
@click.pass_context
def web(ctx, host, port, token):
    cfg = ctx.obj["config"]
    if token is not None:
        cfg.web.token = token
    if host not in ("127.0.0.1", "localhost", "::1"):
        if cfg.web.token:
            click.echo(f"WARNING: binding to a non-loopback address ({host}); the "
                       "dashboard is token-protected but still exposed on the "
                       "network. Prefer an SSH tunnel: "
                       "ssh -L 8080:127.0.0.1:8080 <server>", err=True)
        else:
            click.echo("WARNING: binding the dashboard to a non-loopback address "
                       f"({host}) exposes an UNAUTHENTICATED admin UI that can read "
                       "vault data and write to server paths. Prefer an SSH tunnel: "
                       "ssh -L 8080:127.0.0.1:8080 <server>", err=True)
    from ..web.app import start_web
    start_web(cfg, host=host, port=port)

import click

from .base import cli


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8080, help="Port")
@click.pass_context
def web(ctx, host, port):
    cfg = ctx.obj["config"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        click.echo("WARNING: binding the dashboard to a non-loopback address "
                   f"({host}) exposes an UNAUTHENTICATED admin UI that can read "
                   "vault data and write to server paths. Prefer an SSH tunnel: "
                   "ssh -L 8080:127.0.0.1:8080 <server>", err=True)
    from ..web.app import start_web
    start_web(cfg, host=host, port=port)

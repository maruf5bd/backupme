import click

from .base import cli


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8080, help="Port")
@click.pass_context
def web(ctx, host, port):
    cfg = ctx.obj["config"]
    from ..web.app import start_web
    start_web(cfg, host=host, port=port)
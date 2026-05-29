import click


@click.group()
@click.option("--config", "-c", default="config.yaml", help="Config file path")
@click.option("--db-path", "--db", default="backupme.db", help="Database path")
@click.pass_context
def cli(ctx, config, db_path):
    ctx.ensure_object(dict)
    from .. import config as cfgmod
    cfg = cfgmod.load_config(config)
    ctx.obj["config"] = cfg
    ctx.obj["config_path"] = config
    import pathlib
    from ..core import database as dbmod
    dbmod.DB_PATH = pathlib.Path(db_path)
    dbmod.initialize()
import json
import click

from .base import cli


@cli.group()
def report():
    """Integrity report commands"""


@report.command("list")
@click.pass_context
def report_list(ctx):
    """List past integrity check reports (id, type, status, started)."""
    from ..core import verify as verify_mod
    reports = verify_mod.list_reports()
    for r in reports:
        click.echo(f"#{r['id']}  {r['check_type']:<20}  {r['status']:<10}  {r['started_at']}")


@report.command("get")
@click.argument("report_id", type=int)
@click.pass_context
def report_get(ctx, report_id):
    """Print one integrity report (by id) as JSON."""
    from ..core import database as dbmod
    row = dbmod.execute("SELECT * FROM integrity_checks WHERE id = ?", (report_id,)).fetchone()
    if row:
        click.echo(json.dumps(dict(row), indent=2, default=str))
    else:
        click.echo("Report not found")
        raise SystemExit(1)


@report.command()
@click.option("--count", type=int, default=5)
@click.option("--backend", help="Specific backend")
@click.pass_context
def verify(ctx, count, backend):
    """Run a small random-sample integrity check now and print the result as JSON."""
    from ..core import verify as verify_mod
    result = verify_mod.run_integrity_check("random", count=count, backend_name=backend)
    click.echo(json.dumps(result, indent=2))


@report.command()
@click.pass_context
def integrity(ctx):
    """Print the latest integrity report as JSON."""
    from ..core import verify as verify_mod
    result = verify_mod.get_report()
    click.echo(json.dumps(result, indent=2, default=str))
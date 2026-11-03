"""Shared --json emit helper for CLI commands.

Dependency-free (click + stdlib json only): safe to import at module load,
keeps lazy-import-heavy command modules fast.
"""
import json

import click


def emit(data, as_json: bool, human=None):
    """Implement the --json pattern for a command's result payload.

    When ``as_json`` is true, prints ``data`` to stdout as indented JSON.
    Otherwise calls ``human(data)`` if given, or renders a simple default
    table: dicts as ``key: value`` lines, lists of dicts as aligned columns,
    lists of scalars one per line, anything empty as ``(none)``.
    """
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return
    if callable(human):
        human(data)
        return
    _render_default(data)


def _render_default(data):
    if not data:
        click.echo("(none)")
        return
    if isinstance(data, dict):
        for key, value in data.items():
            click.echo(f"{key}: {value}")
        return
    if isinstance(data, list) and data and all(isinstance(row, dict) for row in data):
        _render_table(data)
        return
    for item in data:
        click.echo(item)


def _render_table(rows):
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    formatted = [
        {key: _shorten(row.get(key)) for key in columns}
        for row in rows
    ]
    widths = {
        key: max(len(key), *(len(row[key]) for row in formatted))
        for key in columns
    }
    header = "  ".join(key.ljust(widths[key]) for key in columns)
    click.echo(header)
    click.echo("  ".join("-" * widths[key] for key in columns))
    for row in formatted:
        click.echo("  ".join(row[key].ljust(widths[key]) for key in columns))


def _shorten(value) -> str:
    text = str(value)
    if len(text) > 40:
        return text[:40]
    return text

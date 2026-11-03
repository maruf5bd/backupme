import os, yaml

import click

from .base import cli
from ..cli.output import emit

_BACKEND_FIELDS = {
    "local": [
        ("name", str, "backend_disk"),
        ("path", str, "D:\\backupme"),
        ("priority", int, 100),
        ("weight", int, 10),
    ],
    "ftp": [
        ("name", str, "ftp_server"),
        ("host", str, "ftp.example.com"),
        ("port", int, 21),
        ("username", str, "user"),
        ("password", str, ""),
        ("path", str, "/backups"),
        ("quota", str, ""),
        ("priority", int, 10),
    ],
    "sftp": [
        ("name", str, "sftp_server"),
        ("host", str, "sftp.example.com"),
        ("port", int, 22),
        ("username", str, "user"),
        ("key_file", str, "~/.ssh/id_rsa"),
        ("path", str, "/mnt/backup"),
        ("quota", str, ""),
        ("priority", int, 10),
    ],
    "smb": [
        ("name", str, "nas"),
        ("host", str, "192.168.1.50"),
        ("share", str, "backups"),
        ("username", str, "user"),
        ("password", str, ""),
        ("path", str, "backupme"),
        ("priority", int, 10),
    ],
    "ssh": [
        ("name", str, "linux_box"),
        ("host", str, "192.168.1.100"),
        ("port", int, 22),
        ("username", str, "user"),
        ("key_file", str, "~/.ssh/id_rsa"),
        ("path", str, "/mnt/backup"),
        ("quota", str, ""),
        ("priority", int, 10),
    ],
    "rclone": [
        ("name", str, "gdrive"),
        ("remote", str, "gdrive:/backupme"),
        ("bwlimit", str, ""),
        ("quota", str, ""),
        ("priority", int, 50),
    ],
    "ninedrive": [
        ("name", str, "ninedrive_main"),
        ("base_url", str, "https://9drive.example.com"),
        ("api_key", str, "9d_live_..."),
        ("email", str, ""),
        ("password", str, ""),
        ("refresh_token", str, ""),
        ("folder", str, "backupme"),
        ("quota", str, ""),
        ("priority", int, 50),
    ],
}


@cli.group()
def backend():
    """Manage storage backends interactively"""


@backend.command("add")
@click.argument("type", type=click.Choice(list(_BACKEND_FIELDS)), required=False)
@click.pass_context
def backend_add(ctx, backend_type):
    """Add a storage backend interactively"""
    if not backend_type:
        click.echo("Available types:")
        for t in _BACKEND_FIELDS:
            click.echo(f"  {t}")
        backend_type = click.prompt("Backend type", type=click.Choice(list(_BACKEND_FIELDS)))

    fields = _BACKEND_FIELDS[backend_type]
    entry = {}
    for name, typ, default in fields:
        default_str = str(default) if default else ""
        val = click.prompt(f"  {name}", default=default_str)
        if not val and typ == int:
            val = default
        else:
            val = typ(val) if val else typ()
        if name in ("password",) and val and (not isinstance(val, str) or not val.startswith("{{")):
            env_key = f"{backend_type.upper()}_{val.upper()}"
            click.echo(f"  Tip: use $env:{env_key} instead of plaintext password")
            use_env = click.confirm(f"  Use env var {env_key}?", default=True)
            if use_env:
                os.environ[env_key] = str(val)
                val = f"{{{{ {env_key} }}}}"
            elif click.confirm("  Save plaintext?", default=False):
                pass
        if isinstance(val, str) and not val.strip():
            continue
        if not val:
            continue
        entry[name] = val

    cfg_path = ctx.obj.get("config_path", "config.yaml")
    if not os.path.exists(cfg_path):
        click.echo(f"Config not found: {cfg_path}")
        return

    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    raw.setdefault("backends", {}).setdefault(backend_type, [])

    existing = [b for b in raw["backends"][backend_type] if b.get("name") == entry.get("name")]
    if existing:
        if not click.confirm(f"Backend '{entry['name']}' already exists. Overwrite?", default=False):
            click.echo("Cancelled.")
            return
        raw["backends"][backend_type] = [b for b in raw["backends"][backend_type] if b.get("name") != entry["name"]]

    raw["backends"][backend_type].append(entry)

    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    click.echo(f"Backend '{entry['name']}' ({backend_type}) added to {cfg_path}")
    if click.confirm("Test connection now?", default=True):
        _test_backend(entry, backend_type)


@backend.command("list")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backend_list(ctx, as_json):
    """List all configured backends"""
    from ..cli.output import emit
    from ..core import rebalance as rb
    cfg = ctx.obj.get("config")
    if not cfg or not hasattr(cfg, "backends"):
        emit({"backends": [], "count": 0}, as_json=as_json, human=lambda d: click.echo("No backends configured"))
        return
    usage = {u["backend_name"]: u for u in rb.backend_usage()}
    rows = []
    for btype, blist in cfg.backends.items():
        for b in blist:
            u = usage.get(b.name, {})
            rows.append({"name": b.name, "type": btype, "host": getattr(b, "host", None), "path": getattr(b, "path", None),
                         "stored_bytes": u.get("stored_bytes", 0), "blobs": u.get("blobs", 0)})
    emit({"backends": rows, "count": len(rows)}, as_json=as_json, human=lambda d: [click.echo(f"  {r['name']:20s}  {r['type']:8s}  host={r.get('host','-'):20s}  path={r.get('path','-'):20s}  stored={r.get('stored_bytes', 0)} B, blobs={r.get('blobs', 0)}") for r in d["backends"]])


@backend.command("remove")
@click.argument("name")
@click.pass_context
def backend_remove(ctx, name):
    """Remove a backend by name"""
    cfg_path = ctx.obj.get("config_path", "config.yaml")
    if not os.path.exists(cfg_path):
        click.echo("Config not found")
        return
    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    found = False
    for btype in list(raw.get("backends", {})):
        raw["backends"][btype] = [b for b in raw["backends"][btype] if b.get("name") != name]
        if not raw["backends"][btype]:
            del raw["backends"][btype]
        else:
            found = True
    if not found:
        click.echo(f"No backend named '{name}'")
        return
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    click.echo(f"Removed backend '{name}'")


@backend.command("test")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backend_test(ctx, name, as_json):
    """Test connectivity to a backend"""
    from ..backends import get_backend
    from ..config import load_config
    from ..cli.output import emit
    cfg_path = ctx.obj.get("config_path", "config.yaml")
    cfg = load_config(cfg_path)

    raw = {}
    for btype, blist in cfg.backends.items():
        raw[btype] = []
        for b in blist:
            d = {k: getattr(b, k) for k in ("type", "name", "path", "host", "port", "username",
                                             "password", "key_file", "share", "remote", "bwlimit",
                                             "quota", "priority", "weight") if getattr(b, k, None) is not None}
            d["type"] = btype
            raw[btype].append(d)

    bk = get_backend(name, raw)
    if bk is None:
        emit({"connected": False, "error": f"backend '{name}' not found"}, as_json=as_json,
             human=lambda d: click.echo(f"Backend '{name}' not found"))
        return
    try:
        ok = bk.connect()
        free = bk.free_space() if ok else None
        total = bk.total_space() if ok else None
        if ok:
            bk.disconnect()
        data = {"name": name, "connected": bool(ok), "free_space": free, "total_space": total}
        emit(data, as_json=as_json, human=lambda d: (click.echo(f"Connecting to {name}... OK"), click.echo(f"  Space: {free} / {total}") if total else None))
    except Exception as e:
        emit({"name": name, "connected": False, "error": str(e)}, as_json=as_json,
             human=lambda d: click.echo(f"Connecting to {name}... ERROR: {e}"))


def _test_backend(entry: dict, btype: str):
    from ..backends import get_backend
    raw = {btype: [entry]}
    bk = get_backend(entry.get("name", "?"), raw)
    if bk:
        _test_connection(bk, entry.get("name", "?"))


def _test_connection(bk, name: str):
    click.echo(f"Connecting to {name}...", nl=False)
    try:
        ok = bk.connect()
        if ok:
            free = bk.free_space()
            total = bk.total_space()
            click.echo(" OK")
            if total:
                used = total - (free or 0)
                pct = used / total * 100 if total else 0
                click.echo(f"  Space: {_fmt_bytes(used)} / {_fmt_bytes(total)} ({pct:.0f}% used)")
            bk.disconnect()
        else:
            click.echo(" FAILED")
    except Exception as e:
        click.echo(f" ERROR: {e}")


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@backend.command("onboard")
@click.argument("name")
@click.option("--rebalance", is_flag=True, help="Backfill all missing blobs after connection test")
@click.option("--dry-run", is_flag=True, help="Show what would be copied without copying")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backend_onboard(ctx, name, rebalance, dry_run, as_json):
    """Test-connect to a newly configured backend NAME and optionally backfill it."""
    from ..backends import get_all_backends, get_backend
    from ..core import rebalance as rb
    from ..config import load_config

    cfg = load_config(ctx.obj.get("config_path", "config.yaml"))
    bk = get_backend(name, cfg)
    if bk is None:
        emit({"status": "error", "error": f"backend '{name}' not found or not connectable"},
             as_json=as_json, human=lambda d: click.echo(d["error"]))
        raise SystemExit(2)

    free = total = None
    try:
        free, total = bk.free_space(), bk.total_space()
    except Exception:
        pass
    result = {"status": "ok", "name": name, "type": bk.type,
              "connected": True, "free_space": free, "total_space": total}

    if rebalance:
        backends = get_all_backends(cfg)
        plan = rb.plan_rebalance(name)
        result["plan"] = plan
        if not dry_run and plan["blobs"]:
            out = rb.rebalance_to(name, backends, cfg)
            result["rebalance"] = out
            if out.get("status") not in ("ok",):
                result["status"] = "partial"
    elif dry_run:
        result["plan"] = rb.plan_rebalance(name)

    def human(d):
        click.echo(f"onboard {d['name']} ({d['type']}): connected")
        if d.get("total_space"):
            click.echo(f"  space: {_fmt_bytes(d['free_space'] or 0)} free / {_fmt_bytes(d['total_space'])}")
        if d.get("plan"):
            p = d["plan"]
            click.echo(f"  to backfill: {p['blobs']} blob(s), {_fmt_bytes(p['bytes'])}")
        if d.get("rebalance"):
            r = d["rebalance"]
            click.echo(f"  rebalanced: {r.get('copied', 0)} blob(s), {_fmt_bytes(r.get('bytes', 0))}"
                       f" (failed: {len(r.get('failed', []))}, remaining: {r.get('remaining_blobs', 0)})")

    emit(result, as_json=as_json, human=human)
    if result.get("status") == "partial":
        raise SystemExit(1)


@backend.command("rebalance")
@click.argument("name")
@click.option("--limit", type=int, default=None, help="Copy at most N blobs this run")
@click.option("--dry-run", is_flag=True, help="Plan only")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backend_rebalance(ctx, name, limit, dry_run, as_json):
    """Backfill blobs missing on backend NAME (resumable; checkpointed)."""
    from ..backends import get_all_backends
    from ..core import rebalance as rb
    from ..config import load_config

    cfg = load_config(ctx.obj.get("config_path", "config.yaml"))
    if dry_run:
        plan = rb.plan_rebalance(name)
        emit({"status": "dry-run", **plan}, as_json=as_json,
             human=lambda d: click.echo(f"{d['blobs']} blob(s), {_fmt_bytes(d['bytes'])} would be copied to {name}"))
        return
    backends = get_all_backends(cfg)
    out = rb.rebalance_to(name, backends, cfg, limit=limit)

    def human(d):
        if d.get("status") == "error":
            click.echo(d["error"])
            return
        click.echo(f"rebalance to {d['target']}: copied {d['copied']} blob(s), "
                   f"{_fmt_bytes(d['bytes'])}, failed {len(d['failed'])}, "
                   f"remaining {d['remaining_blobs']}")

    emit(out, as_json=as_json, human=human)
    if out.get("status") != "ok":
        raise SystemExit(1)


@backend.command("drain")
@click.argument("name")
@click.option("--dry-run", is_flag=True, help="Report what would be copied without copying")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backend_drain(ctx, name, dry_run, as_json):
    """Copy everything off backend NAME, then print the GC/removal runbook."""
    from ..backends import get_all_backends
    from ..core import rebalance as rb
    from ..config import load_config

    cfg = load_config(ctx.obj.get("config_path", "config.yaml"))
    backends = get_all_backends(cfg)
    out = rb.drain_backend(name, backends, cfg, dry_run=dry_run)

    def human(d):
        if d.get("status") == "error":
            click.echo(d["error"])
            return
        if d["status"] == "dry-run":
            click.echo(f"drain {d['backend']}: {d['blobs']} blob(s) total, "
                       f"would copy {d['blobs_to_copy']} blob(s) / {_fmt_bytes(d['bytes_to_copy'])}")
            return
        click.echo(f"drain {d['backend']}: copied {d['copied']}, failed {len(d['failed'])}, "
                   f"GC-eligible blobs {d['gc_eligible_blobs']}, "
                   f"still only here: {d['remaining_only_here_blobs']} blob(s) / "
                   f"{_fmt_bytes(d['remaining_only_here_bytes'])}")
        for step in d.get("runbook", []):
            click.echo(f"  {step}")

    emit(out, as_json=as_json, human=human)
    if out.get("status") in ("error", "partial"):
        raise SystemExit(1)
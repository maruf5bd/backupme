import os, sys, yaml

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
@click.argument("backend_type", type=click.Choice(list(_BACKEND_FIELDS)), required=False)
@click.option("--name", help="Backend name")
@click.option("--host", help="Host address")
@click.option("--port", type=int, help="Port number")
@click.option("--username", help="Username")
@click.option("--password", help="Password")
@click.option("--key-file", help="SSH key file path")
@click.option("--path", help="Remote path")
@click.option("--share", help="SMB share name")
@click.option("--remote", help="rclone remote name")
@click.option("--base-url", help="9drive base URL")
@click.option("--api-key", help="9drive API key")
@click.option("--email", help="Email address")
@click.option("--refresh-token", help="Refresh token")
@click.option("--folder", help="Folder name")
@click.option("--bwlimit", help="Bandwidth limit")
@click.option("--quota", help="Quota")
@click.option("--priority", type=int, help="Priority")
@click.option("--weight", type=int, help="Weight")
@click.option("--test/--no-test", default=True, help="Test connection after adding")
@click.pass_context
def backend_add(ctx, backend_type, name, host, port, username, password, key_file,
                path, share, remote, base_url, api_key, email, refresh_token,
                folder, bwlimit, quota, priority, weight, test):
    """Add a storage backend. With --name/--host/--port/etc. it runs
    non-interactively; otherwise it prompts for each field."""
    if not backend_type:
        click.echo("Available types:")
        for t in _BACKEND_FIELDS:
            click.echo(f"  {t}")
        backend_type = click.prompt("Backend type", type=click.Choice(list(_BACKEND_FIELDS)))

    fields = _BACKEND_FIELDS[backend_type]
    field_map = {
        "name": name, "host": host, "port": port, "username": username,
        "password": password, "key_file": key_file, "path": path, "share": share,
        "remote": remote, "base_url": base_url, "api_key": api_key,
        "email": email, "refresh_token": refresh_token, "folder": folder,
        "bwlimit": bwlimit, "quota": quota, "priority": priority, "weight": weight,
    }

    entry = {}
    interactive = all(v is None for v in field_map.values())

    for field_name, typ, default in fields:
        val = field_map.get(field_name)
        if val is None:
            if interactive:
                default_str = str(default) if default else ""
                val = click.prompt(f"  {field_name}", default=default_str)
            else:
                val = default
        if not val and typ == int:
            val = default
        else:
            val = typ(val) if val else typ()
        if field_name == "password" and val and (not isinstance(val, str) or not val.startswith("{{")):
            env_key = f"{backend_type.upper()}_{val.upper()}"
            if interactive:
                click.echo(f"  Tip: use $env:{env_key} instead of plaintext password")
                if click.confirm(f"  Use env var {env_key}?", default=True):
                    os.environ[env_key] = str(val)
                    val = f"{{{{ {env_key} }}}}"
            else:
                # Never prompt in non-interactive mode (scripts); just warn.
                click.echo("  Warning: storing plaintext password in config.yaml")
        if isinstance(val, str) and not val.strip():
            continue
        if not val:
            continue
        entry[field_name] = val

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
    # --no-test is honored: non-interactive runs test by default (no prompt),
    # interactive runs ask first (qa P1: the confirm blocked scripts and the
    # flag was ignored).
    if test and (not interactive or click.confirm("Test connection now?", default=True)):
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
    exists = any(b.get("name") == name
                 for blist in raw.get("backends", {}).values() for b in blist)
    if not exists:
        click.echo(f"No backend named '{name}'")
        raise SystemExit(1)
    for btype in list(raw.get("backends", {})):
        raw["backends"][btype] = [b for b in raw["backends"][btype] if b.get("name") != name]
        if not raw["backends"][btype]:
            del raw["backends"][btype]
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
        raise SystemExit(1)
    try:
        ok = bk.connect()
        free = bk.free_space() if ok else None
        total = bk.total_space() if ok else None
        if ok:
            bk.disconnect()
        data = {"name": name, "connected": bool(ok), "free_space": free, "total_space": total}
        emit(data, as_json=as_json, human=lambda d: (click.echo(f"Connecting to {name}... OK"), click.echo(f"  Space: {free} / {total}") if total else None))
        if not ok:
            raise SystemExit(1)
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
    # P2: an unknown name used to fall through to an empty dry-run plan
    # (silently planning 0 blobs). Fail like `backend onboard` does, but with
    # exit 1 (documented in MANUAL.md section 10).
    if not any(b.name == name for b in backends):
        out = {"status": "error", "error": f"backend '{name}' not found"}
        emit(out, as_json=as_json, human=lambda d: click.echo(d["error"]))
        raise SystemExit(1)
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


@backend.command("rescan")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backend_rescan(ctx, name, as_json):
    """Walk backend NAME and diff its real contents against the index."""
    from ..backends import get_all_backends
    from ..core.backend_scan import rescan_backend

    bk = next((b for b in get_all_backends(ctx.obj["config"]) if b.name == name), None)
    if bk is None:
        out = {"status": "error", "error": f"no backend named {name!r}"}
        emit(out, as_json=as_json, human=lambda d: click.echo(d["error"]))
        raise SystemExit(1)

    if not bk.connected and not bk.connect():
        out = {"status": "error", "error": f"backend {name!r} failed to connect"}
        emit(out, as_json=as_json, human=lambda d: click.echo(d["error"]))
        raise SystemExit(1)

    result = rescan_backend(bk)
    result["status"] = "ok"

    def human(d):
        click.echo(f"{d['backend']}: walk={d['walk_files']} file(s) "
                   f"(walk_errors={d['walk_errors']}), db={d['db_paths']} path(s)")
        click.echo(f"missing on box : {len(d['missing_on_box'])} "
                   f"({_fmt_bytes(d['missing_bytes'])})")
        click.echo(f"on box not in DB: {len(d['extra_on_box'])}")
        click.echo(f"size mismatches: {len(d['size_mismatch'])}")
        for p in d["missing_on_box"][:10]:
            click.echo(f"  missing: {p}")
        for p in d["extra_on_box"][:10]:
            click.echo(f"  extra  : {p}")
        for m in d["size_mismatch"][:10]:
            click.echo(f"  size   : {m['path']} db={m['db_size']} box={m['box_size']}")

    emit(result, as_json=as_json, human=human)
    if result["missing_on_box"] or result["size_mismatch"]:
        raise SystemExit(1)


@backend.command("sync-namespace")
@click.argument("target")
@click.option("--source", "sources", multiple=True,
              help="Restrict source roots to these local backend names "
                   "(default: all local backend roots)")
@click.option("--namespace", "namespaces", multiple=True,
              type=click.Choice(["manifests", "tree", "__backupme_db__"]),
              help="Restrict to these namespaces (default: all three)")
@click.option("--dry-run", is_flag=True,
              help="Scan and report without uploading anything")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
@click.pass_context
def backend_sync_namespace(ctx, target, sources, namespaces, dry_run, as_json):
    """Copy restore-map namespaces (manifests/tree/__backupme_db__) from
    local backend roots to backend TARGET so it alone can drive restore."""
    from pathlib import Path as _Path

    from ..backends import get_backend
    from ..config import load_config
    from ..core.namespace_sync import DEFAULT_NAMESPACES, sync_namespace

    cfg = load_config(ctx.obj.get("config_path", "config.yaml"))
    if not hasattr(cfg, "backends"):
        out = {"status": "error", "error": "config has no backends section"}
        emit(out, as_json=as_json, human=lambda d: click.echo(d["error"]))
        raise SystemExit(1)

    tgt = get_backend(target, cfg)
    if tgt is None:
        out = {"status": "error",
               "error": f"backend '{target}' not found or not connectable"}
        emit(out, as_json=as_json, human=lambda d: click.echo(d["error"]))
        raise SystemExit(1)

    local_entries = [b for b in cfg.backends.get("local", []) if getattr(b, "path", None)]
    known = {b.name: b for b in local_entries}
    if sources:
        unknown = [s for s in sources if s not in known]
        if unknown:
            out = {"status": "error",
                   "error": f"unknown source backend(s): {', '.join(unknown)} "
                            f"(sources must be local backends with a path)"}
            emit(out, as_json=as_json, human=lambda d: click.echo(d["error"]))
            raise SystemExit(1)
        chosen = [known[s] for s in sources]
    else:
        chosen = local_entries
    source_roots = [_Path(b.path) for b in chosen]
    if not source_roots:
        out = {"status": "error",
               "error": "no local source roots resolved from config"}
        emit(out, as_json=as_json, human=lambda d: click.echo(d["error"]))
        raise SystemExit(1)

    def _progress(done, total, rel):
        # Human-mode heartbeat on stderr only; --json stdout stays clean.
        if total and done % 200 == 0:
            click.echo(f"  namespace sync {done}/{total}", err=True)

    result = sync_namespace(tgt, source_roots,
                            namespaces=list(namespaces) or None,
                            dry_run=dry_run,
                            progress_cb=None if as_json else _progress)
    result["target"] = target
    result["sources"] = [str(p) for p in source_roots]
    result["namespaces"] = list(namespaces) or list(DEFAULT_NAMESPACES)
    result["dry_run"] = bool(dry_run)

    def human(d):
        if d.get("status") == "error":
            click.echo(d["error"])
            return
        if d.get("dry_run"):
            click.echo(f"sync-namespace -> {d['target']}: scanned {d['scanned']}, "
                       f"would upload {d.get('would_upload', 0)} "
                       f"({_fmt_bytes(d.get('would_upload_bytes', 0))}), "
                       f"skipped {d['skipped_existing']}, "
                       f"failed {len(d['failed'])}")
        else:
            click.echo(f"sync-namespace -> {d['target']}: scanned {d['scanned']}, "
                       f"uploaded {d['uploaded']} ({_fmt_bytes(d['bytes_uploaded'])}), "
                       f"skipped {d['skipped_existing']}, failed {len(d['failed'])}")
        if d["conflicts"]["count"]:
            click.echo(f"  conflicts: {d['conflicts']['count']} (first source wins)")
            for ex in d["conflicts"]["examples"]:
                click.echo(f"    {ex}")
        for f in d["failed"][:10]:
            click.echo(f"  failed: {f['path']}: {f['error']}")
        if len(d["failed"]) > 10:
            click.echo(f"  ... and {len(d['failed']) - 10} more")

    emit(result, as_json=as_json, human=human)
    if result.get("status") != "ok":
        raise SystemExit(1)
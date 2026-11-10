"""Tests for core/namespace_sync.py and the `backend sync-namespace` CLI.

Scratch dirs only (env fixture: isolated config + DB + local backend in the
pytest tmp tree) — never the live config.yaml, backupme.db, or real backends
(storagebox/K/H). A second local backend "sync_target" is added per-test as
the copy destination.
"""
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from backupme.backends.local import LocalBackend
from backupme.cli import cli
from backupme.core import namespace_sync
from backupme.core.namespace_sync import sync_namespace


@pytest.fixture()
def runner():
    return CliRunner()


def _invoke(runner, env, *args, **kw):
    """Invoke the CLI pinned to the isolated env's config and DB."""
    return runner.invoke(
        cli,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"), *args],
        **kw,
    )


def _add_second_backend(env, name="sync_target", subdir="sync_target_storage"):
    """Register a second local backend in the scratch config; returns its root."""
    cfg_path = env / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    root = env.parent / subdir
    raw.setdefault("backends", {}).setdefault("local", []).append(
        {"name": name, "path": root.as_posix(), "priority": 90})
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return root


def _source_root(env):
    """The test_local backend root (conftest points it at tmp/storage)."""
    return env.parent / "storage"


def _target(env):
    from backupme.backends import get_backend
    from backupme.config import load_config
    bk = get_backend("sync_target", load_config())
    assert bk is not None, "sync_target backend failed to connect"
    return bk


def _write_layout(root: Path, tag: bytes = b"a") -> dict[str, bytes]:
    """Create a synthetic manifests/tree/__backupme_db__ layout under root.

    Receipt names are 32 hex chars, sidecars 64 hex, tree pointers sit three
    levels deep — mirroring the real on-disk namespace layout.
    Returns {rel_path: content}.
    """
    files = {
        "manifests/ab/cd/" + "11" * 16 + ".manifest.json":
            b'{"kind":"receipt","tag":"%s"}' % tag,
        "manifests/ab/cd/" + "22" * 32 + ".json":
            b'{"kind":"sidecar","tag":"%s"}' % tag,
        "tree/9f/2c/docs-report.txt.backupme": b"TREEPOINTER:" + tag,
        "__backupme_db__/snapshots/snapshot-20261101.json":
            b'{"snap":true,"tag":"%s"}' % tag,
        "__backupme_db__/wal/wal-000001.zst": b"WALBLOB:" + tag,
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return files


def _target_files(target_root: Path) -> list[str]:
    return sorted(
        p.relative_to(target_root).as_posix()
        for p in target_root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# (a) full sync of a synthetic layout
# ---------------------------------------------------------------------------

def test_full_sync_uploads_all_namespaces(env):
    _add_second_backend(env)
    files = _write_layout(_source_root(env))
    tgt = _target(env)

    seen = []
    res = sync_namespace(tgt, [_source_root(env)],
                         progress_cb=lambda done, total, rel: seen.append((done, total, rel)))

    assert res["status"] == "ok"
    assert res["scanned"] == len(files)
    assert res["uploaded"] == len(files)
    assert res["skipped_existing"] == 0
    assert res["failed"] == []
    assert res["conflicts"]["count"] == 0
    assert res["bytes_uploaded"] == sum(len(v) for v in files.values())
    # progress callback fires once per processed file
    assert [d for d, _t, _r in seen] == list(range(1, len(files) + 1))
    # every file landed on the target with identical bytes
    target_root = Path(tgt.base_path)
    assert _target_files(target_root) == sorted(files)
    for rel, data in files.items():
        assert tgt.hash(rel) == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# (b) idempotent second run: everything already on the target is skipped
# ---------------------------------------------------------------------------

def test_second_run_is_fully_idempotent(env):
    _add_second_backend(env)
    files = _write_layout(_source_root(env))
    tgt = _target(env)

    r1 = sync_namespace(tgt, [_source_root(env)])
    assert r1["status"] == "ok"
    assert r1["uploaded"] == len(files)

    r2 = sync_namespace(tgt, [_source_root(env)])
    assert r2["status"] == "ok"
    assert r2["scanned"] == len(files)
    assert r2["uploaded"] == 0
    assert r2["skipped_existing"] == len(files)
    assert r2["bytes_uploaded"] == 0
    assert r2["failed"] == []


# ---------------------------------------------------------------------------
# (c) one unreadable file fails alone; the rest still sync
# ---------------------------------------------------------------------------

def test_per_file_failure_isolated(env, monkeypatch):
    _add_second_backend(env)
    files = _write_layout(_source_root(env))
    tgt = _target(env)

    boom_rel = "tree/9f/2c/docs-report.txt.backupme"
    original = namespace_sync._sha256_file

    def fake_sha(path):
        if str(path).replace("\\", "/").endswith(boom_rel):
            raise PermissionError("planted unreadable file")
        return original(path)

    monkeypatch.setattr(namespace_sync, "_sha256_file", fake_sha)
    res = sync_namespace(tgt, [_source_root(env)])

    assert res["status"] == "partial"
    assert res["uploaded"] == len(files) - 1
    assert len(res["failed"]) == 1
    assert res["failed"][0]["path"] == boom_rel
    assert "local read failed" in res["failed"][0]["error"]
    # the healthy files still made it; the broken one did not
    assert tgt.exists("manifests/ab/cd/" + "11" * 16 + ".manifest.json")
    assert not tgt.exists(boom_rel)


# ---------------------------------------------------------------------------
# (d) size mismatch on the target after upload is recorded as failed
# ---------------------------------------------------------------------------

def test_size_mismatch_verification_recorded_as_failed(env):
    class LyingListingBackend(LocalBackend):
        """Reports every remote size 7 bytes larger than reality."""

        def list_files(self, prefix=""):
            rows = super().list_files(prefix)
            return [{**r, "size": r["size"] + 7} for r in rows]

    files = _write_layout(_source_root(env))
    lying = LyingListingBackend(
        "lying_target", {"path": str(env.parent / "lying_target_storage")})
    assert lying.connect()

    res = sync_namespace(lying, [_source_root(env)])

    assert res["status"] == "partial"
    assert res["uploaded"] == 0
    assert res["bytes_uploaded"] == 0
    assert len(res["failed"]) == len(files)
    assert all("size mismatch on target" in f["error"] for f in res["failed"])
    assert all(f["path"] in files for f in res["failed"])
    # additive-only rule: the bytes were written despite the failed check
    for rel in files:
        assert lying.exists(rel)


# ---------------------------------------------------------------------------
# (e) dry_run scans but uploads nothing
# ---------------------------------------------------------------------------

def test_dry_run_uploads_nothing(env):
    _add_second_backend(env)
    files = _write_layout(_source_root(env))
    tgt = _target(env)

    res = sync_namespace(tgt, [_source_root(env)], dry_run=True)

    assert res["status"] == "ok"
    assert res["scanned"] == len(files)
    assert res["uploaded"] == 0
    assert res["bytes_uploaded"] == 0
    assert res["skipped_existing"] == 0
    assert res["would_upload"] == len(files)
    assert res["would_upload_bytes"] == sum(len(v) for v in files.values())
    assert res["failed"] == []
    assert _target_files(Path(tgt.base_path)) == []


# ---------------------------------------------------------------------------
# (f) conflicting rel paths across sources: first source wins, conflict kept
# ---------------------------------------------------------------------------

def test_conflict_first_source_wins(env):
    root_a = env.parent / "src_a"
    root_b = env.parent / "src_b"
    files_a = _write_layout(root_a, tag=b"A")
    _write_layout(root_b, tag=b"BB")          # same rel paths, different size
    # a sixth rel path where the sources also disagree (drives the examples cap)
    extra = "manifests/ef/01/" + "33" * 16 + ".manifest.json"
    (root_a / extra).parent.mkdir(parents=True)
    (root_a / extra).write_bytes(b"from-a")
    (root_b / extra).parent.mkdir(parents=True)
    (root_b / extra).write_bytes(b"from-b-but-longer")
    # and one identical-size duplicate across sources: must NOT count as conflict
    same = "tree/aa/bb/same.pointer.backupme"
    (root_a / same).parent.mkdir(parents=True)
    (root_a / same).write_bytes(b"identical-pointer")
    (root_b / same).parent.mkdir(parents=True)
    (root_b / same).write_bytes(b"identical-pointer")

    _add_second_backend(env)
    tgt = _target(env)
    res = sync_namespace(tgt, [root_a, root_b])

    assert res["status"] == "ok"
    assert res["scanned"] == len(files_a) + 2   # unique rel paths across sources
    assert res["uploaded"] == len(files_a) + 2
    assert res["conflicts"]["count"] == len(files_a) + 1  # 6 disagreements
    assert len(res["conflicts"]["examples"]) == 5         # example cap
    assert all(ex.startswith("manifests/") or ex.startswith("tree/") or
               ex.startswith("__backupme_db__/")
               for ex in res["conflicts"]["examples"])
    # first-source-wins: the target holds root_a's bytes for every conflict
    target_root = Path(tgt.base_path)
    assert _target_files(target_root) == sorted(list(files_a) + [extra, same])
    for rel, data in files_a.items():
        assert tgt.hash(rel) == hashlib.sha256(data).hexdigest()
    assert tgt.hash(extra) == hashlib.sha256(b"from-a").hexdigest()


# ---------------------------------------------------------------------------
# (g) CLI: happy path, unknown target, unknown source
# ---------------------------------------------------------------------------

def test_cli_sync_namespace_happy_path(runner, env):
    _add_second_backend(env)
    files = _write_layout(_source_root(env))

    r = _invoke(runner, env, "backend", "sync-namespace", "sync_target", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["target"] == "sync_target"
    assert payload["uploaded"] == len(files)
    assert payload["scanned"] == len(files)
    assert payload["failed"] == []
    assert payload["namespaces"] == ["manifests", "tree", "__backupme_db__"]

    target_root = env.parent / "sync_target_storage"
    assert _target_files(target_root) == sorted(files)


def test_cli_sync_namespace_namespace_filter(runner, env):
    _add_second_backend(env)
    files = _write_layout(_source_root(env))
    manifest_rels = [rel for rel in files if rel.startswith("manifests/")]

    r = _invoke(runner, env, "backend", "sync-namespace", "sync_target",
                "--namespace", "manifests", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "ok"
    assert payload["uploaded"] == len(manifest_rels)
    assert payload["namespaces"] == ["manifests"]
    # untouched namespaces did not leak onto the target
    target_root = env.parent / "sync_target_storage"
    assert _target_files(target_root) == sorted(manifest_rels)


def test_cli_sync_namespace_unknown_target_exit_1(runner, env):
    _add_second_backend(env)
    _write_layout(_source_root(env))

    r = _invoke(runner, env, "backend", "sync-namespace", "ghost_backend", "--json")
    assert r.exit_code == 1, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert "ghost_backend" in payload["error"]


def test_cli_sync_namespace_unknown_source_exit_1(runner, env):
    _add_second_backend(env)

    r = _invoke(runner, env, "backend", "sync-namespace", "sync_target",
                "--source", "no_such_local", "--json")
    assert r.exit_code == 1, r.output
    payload = json.loads(r.stdout)
    assert payload["status"] == "error"
    assert "no_such_local" in payload["error"]

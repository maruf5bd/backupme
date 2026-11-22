"""Regression tests for the 2026-11-20 correctness fixes.

- D31: `backend add sftp|ssh --password X` used to silently DROP the password
  (the field table had no password row) while printing success. The saved
  config.yaml entry must now contain the password.
- D32: `upload_bundle` returns results NOT in input order (inline-processed
  chunked/donor/error files append during its loop, batched small files are
  flushed last). Consumers matched results POSITIONALLY, rotating
  archive_members.file_id / cpanel member file_ids for any mixed batch. The
  consumers now match by local_path; these tests force a mixed batch through
  both consumer paths and assert each member's file_id maps to content whose
  sha equals the member's own sha.
- D33: a vanished file during `_run_backups` used to abort the whole backups
  tick (and every later tick in the pass). The tick now guards per source.
  Tick guards additionally report through notify.notify_failure, and a
  notify failure itself must not break the guard.
"""
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from backupme.cli import cli as cli_group
from backupme.core import archives as archives_mod
from backupme.core import cpanel_ingest
from backupme.core.database import execute
from tests.test_archives import build_tar_gz


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add_chunking(env, min_size: int = 2048, chunk_size: int = 65536) -> None:
    """Tiny min_size so one member takes the chunked (inline) path while the
    rest stay bundled — the mixed batch that exposed D32."""
    cfg = env / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    text += (
        "chunking:\n"
        f"  min_size: {min_size}\n"
        f"  chunk_size: {chunk_size}\n"
    )
    cfg.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# D31: backend add sftp|ssh --password must be saved
# ---------------------------------------------------------------------------

def test_d31_backend_add_saves_sftp_password(env):
    runner = CliRunner()
    result = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "backend", "add", "sftp",
         "--name", "sftp_pw_test",
         "--host", "127.0.0.1",
         "--port", "22",
         "--username", "user",
         "--password", "S3cret-PW",
         "--no-test"],
    )
    assert result.exit_code == 0, result.output
    raw = yaml.safe_load((env / "config.yaml").read_text(encoding="utf-8"))
    entries = raw["backends"]["sftp"]
    assert len(entries) == 1, entries
    assert entries[0]["name"] == "sftp_pw_test"
    # Before D31 the password row was missing from the field table, so the
    # saved entry had no password key at all.
    assert entries[0].get("password") == "S3cret-PW", entries[0]


def test_d31_backend_add_ssh_password_refused(env):
    """SSHBackend is CLI-based and key-auth-only: --password must be refused
    loudly instead of being stored as an inert credential (D31 follow-up)."""
    runner = CliRunner()
    result = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "backend", "add", "ssh",
         "--name", "ssh_pw_test",
         "--host", "127.0.0.1",
         "--port", "22",
         "--username", "user",
         "--password", "S3cret-PW",
         "--no-test"],
    )
    assert result.exit_code == 1, result.output
    assert "--key_file" in result.output and "--password" in result.output
    raw = yaml.safe_load((env / "config.yaml").read_text(encoding="utf-8"))
    assert not (raw.get("backends") or {}).get("ssh"), raw.get("backends")


def test_d31_saved_sftp_password_reaches_backend_config(env):
    """End-to-end: backend add saves the password, load_config parses it, and
    the backend object built from the config sees it."""
    from backupme.config import load_config
    from backupme.backends import get_backend

    runner = CliRunner()
    result = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "backend", "add", "sftp",
         "--name", "sftp_pw_chain",
         "--host", "127.0.0.1",
         "--username", "user",
         "--password", "S3cret-PW",
         "--no-test"],
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(str(env / "config.yaml"))
    entry = cfg.backends["sftp"][0]
    assert entry.password == "S3cret-PW"
    # get_backend connect()s to a dead host and returns None — build the
    # backend the same way the factory does, WITHOUT connecting, to check
    # the constructor actually consumes the password.
    from backupme.backends.ftp_sftp import SFTPBackend
    bk = SFTPBackend("sftp_pw_chain", {"host": "127.0.0.1", "username": "user",
                                       "password": "S3cret-PW"})
    assert bk.password == "S3cret-PW"


# ---------------------------------------------------------------------------
# D32: archive mixed batch — member file_ids must match member content
# ---------------------------------------------------------------------------

def test_d32_archive_mixed_batch_file_ids_match_content(env, backends):
    _add_chunking(env)
    from backupme.config import load_config

    members = {
        "m_a.txt": b"alpha member content",   # small -> bundled (flushed last)
        "big/blob.bin": b"B" * 8192,          # > min_size -> chunked inline
        "m_b.txt": b"beta member content",
        "m_c.txt": b"gamma member content",
    }
    src = env / "src.tar.gz"
    src.write_bytes(build_tar_gz(members))
    result = archives_mod.ingest_archive_file(str(src), backends, load_config())
    assert result["status"] == "archive_ingested", result
    fp = result["fingerprint"]

    for path, content in members.items():
        sha = _sha(content)
        row = execute(
            "SELECT am.file_id, am.member_sha256, f.sha256 AS file_sha "
            "FROM archive_members am "
            "JOIN files f ON f.id = am.file_id "
            "JOIN archives a ON a.id = am.archive_id "
            "WHERE a.fingerprint = ? AND am.relative_path = ?",
            (fp, path),
        ).fetchone()
        assert row is not None, f"no archive_members row for {path}"
        assert row["member_sha256"] == sha, path
        assert row["file_sha"] == sha, (
            f"{path}: archive_members.file_id -> files.sha256="
            f"{row['file_sha']}, expected {sha} (rotated file id?)")
        # Each chunk row must reference a blob whose content-addressed tail
        # sha equals the member sha (bundle member ids end with the member
        # sha; a single-chunk file's blob id IS its content sha).
        chunks = execute(
            "SELECT blob_id FROM file_chunks WHERE file_id = ? ORDER BY seq",
            (row["file_id"],),
        ).fetchall()
        assert chunks, f"no chunk rows for {path}"
        for c in chunks:
            assert c["blob_id"].split("/")[-1] == sha, (
                f"{path}: chunk blob {c['blob_id']} does not carry the "
                f"member content sha")


def test_d32_cpanel_mixed_batch_file_ids_match_content(env, backends):
    _add_chunking(env)
    from backupme.config import load_config

    members = {
        "homedir/small1.php": b"<?php small one;",
        "homedir/big.bin": b"B" * 8192,        # > min_size -> chunked inline
        "mysql/small2.sql": b"SELECT 2;",
        "homedir/small3.php": b"<?php small three;",
    }
    drop = env / "drop" / "acct"
    drop.mkdir(parents=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    (drop / "cpmove-acct.tar.gz").write_bytes(buf.getvalue())

    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    out = cpanel_ingest.ingest_tree(str(env / "drop"), backends, cfg)
    assert out["status"] == "ok", out
    r = out["results"][0]
    assert r["status"] == "ok", r

    # The generation manifest records (name, sha256, file_id) per member;
    # every file_id must map to a files row whose sha equals the member's
    # own sha (the positional zip used to rotate them).
    manifests = list((env / "manifest").glob("*.json"))
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert sorted(m["name"] for m in payload["members"]) == sorted(members)
    for m in payload["members"]:
        sha = _sha(members[m["name"]])
        assert m["sha256"] == sha, m["name"]
        row = execute(
            "SELECT sha256 FROM files WHERE id = ?", (m["file_id"],),
        ).fetchone()
        assert row is not None, f"{m['name']}: file_id {m['file_id']} missing"
        assert row["sha256"] == sha, (
            f"{m['name']}: manifest file_id -> files.sha256={row['sha256']}, "
            f"expected {sha} (rotated file id?)")


def test_d32_upload_bundle_covers_every_input_path(env, backends):
    """The contract the consumers rely on after D32: every input file gets
    exactly one result entry carrying its local_path (order not asserted —
    that ordering is what broke the consumers)."""
    _add_chunking(env)
    from backupme.config import load_config
    from backupme.core import uploader as uploader_mod

    src = env / "mixed"
    src.mkdir()
    contents = {
        "a_small.txt": b"alpha batched",
        "big.bin": b"B" * 8192,          # chunked inline
        "b_small.txt": b"beta batched",
        "c_small.txt": b"gamma batched",
    }
    items = []
    for name, content in contents.items():
        p = src / name
        p.write_bytes(content)
        items.append({"local_path": str(p), "source_root": str(src),
                      "relative_path": name, "version_seq": 1})
    results = uploader_mod.upload_bundle(items, backends, load_config(), 1)

    by_path = {}
    for r in results:
        by_path.setdefault(r.get("local_path"), []).append(r)
    assert set(by_path) == {str(src / name) for name in contents}
    for path, rs in by_path.items():
        assert len(rs) == 1, (path, rs)
        assert rs[0]["status"] in ("ok", "partial", "dedup"), (path, rs)


def test_d32_missing_result_is_error_not_misattribution(env, backends,
                                                        monkeypatch):
    """A member whose local_path has no matching upload result must become an
    error (never silently paired with some other member's result)."""
    _add_chunking(env)
    from backupme.config import load_config

    src = env / "src2.tar.gz"
    src.write_bytes(build_tar_gz({"m_a.txt": b"alpha", "m_b.txt": b"beta"}))

    def bogus_upload(items, bks, cfg, redundancy=1):
        # No local_path at all — nothing can be matched.
        return [{"status": "error", "error": "simulated backend outage"}]

    monkeypatch.setattr(archives_mod, "upload_bundle", bogus_upload)
    result = archives_mod.ingest_archive_file(str(src), backends, load_config())
    # The member failure raises inside the upload pass, which the archive
    # engine converts into the whole-blob fallback with the reason attached.
    assert result["status"] == "stored_whole", result
    assert "no upload result" in result["reason"], result


# ---------------------------------------------------------------------------
# D33: backups tick guards + notify wiring
# ---------------------------------------------------------------------------

def test_d33_vanished_file_does_not_kill_other_sources(env, backends,
                                                       monkeypatch):
    from backupme import scheduler as scheduler_mod

    bad = env / "bad_src"
    bad.mkdir()
    (bad / "g.txt").write_bytes(b"bad file content")
    good = env / "good_src"
    good.mkdir()
    (good / "f.txt").write_bytes(b"good file content")

    config = {
        "backends": {"local": [{"name": "test_local",
                                "path": str(env / "storage"),
                                "priority": 100}]},
        "backups": [
            {"source": str(bad), "backend": "test_local"},
            {"source": str(good), "backend": "test_local"},
        ],
    }
    sched = scheduler_mod.BackupScheduler(config)

    real_collect = scheduler_mod.collect_changes

    def flaky_collect(source_root, excludes, force=False, no_skip=False):
        if str(bad) in source_root:
            raise FileNotFoundError("vanished between walk and stat")
        return real_collect(source_root, excludes, force=force,
                            no_skip=no_skip)

    monkeypatch.setattr(scheduler_mod, "collect_changes", flaky_collect)
    # Before D33 this raised out of _run_backups and aborted the whole pass.
    sched._run_backups()

    rows = execute(
        "SELECT relative_path FROM files WHERE source_root = ?",
        (str(good),),
    ).fetchall()
    assert [r["relative_path"] for r in rows] == ["f.txt"]
    bad_rows = execute(
        "SELECT COUNT(*) FROM files WHERE source_root = ?", (str(bad),)
    ).fetchone()[0]
    assert bad_rows == 0


def test_d33_backups_tick_guard_notifies(env, monkeypatch):
    import backupme.core.notify as notify_mod
    from backupme import scheduler as scheduler_mod

    calls = []

    def fake_notify(title, detail, config=None):
        calls.append((title, detail))
        return {"logged": True}

    monkeypatch.setattr(notify_mod, "notify_failure", fake_notify)
    sched = scheduler_mod.BackupScheduler({})

    def boom():
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(sched, "_backups_pass", boom)
    sched._run_backups()          # must not raise

    assert len(calls) == 1, calls
    assert calls[0][0] == "backups tick failed"
    assert "tick blew up" in calls[0][1]


def test_d33_retention_tick_failure_notifies(env, monkeypatch):
    import backupme.core.notify as notify_mod
    from backupme import scheduler as scheduler_mod

    calls = []

    def fake_notify(title, detail, config=None):
        calls.append((title, detail))
        return {"logged": True}

    monkeypatch.setattr(notify_mod, "notify_failure", fake_notify)

    def boom(config):
        raise RuntimeError("retention blew up")

    monkeypatch.setattr(scheduler_mod, "apply_retention", boom)
    sched = scheduler_mod.BackupScheduler({})
    sched._run_retention()

    assert len(calls) == 1, calls
    assert calls[0][0] == "retention tick failed"
    assert "retention blew up" in calls[0][1]


def test_d33_notify_failure_cannot_break_guard(env, monkeypatch):
    """A raising notification must not escape the tick guard it was called
    from (D33 wiring rule)."""
    import backupme.core.notify as notify_mod
    from backupme import scheduler as scheduler_mod

    def exploding_notify(title, detail, config=None):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(notify_mod, "notify_failure", exploding_notify)
    sched = scheduler_mod.BackupScheduler({})

    def boom():
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(sched, "_backups_pass", boom)
    sched._run_backups()          # notify raising must not escape

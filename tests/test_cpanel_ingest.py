"""Tests for backupme.core.cpanel_ingest — the cPanel cpmove ingest engine."""
import io
import json
import re
import tarfile
from pathlib import Path

import pytest

from backupme.core import cpanel_ingest
from backupme.core.database import execute


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_MEMBERS = [
    ("homedir/index.php", b"<?php echo 1;"),
    ("homedir/config.php", b"<?php 1234567890"),
    ("mysql/db.sql", b"INSERT INTO x VALUES (1);"),
]


def _build_tar_bytes(with_symlink: bool = True, with_traversal: bool = True,
                     override_content: dict[str, bytes] | None = None) -> bytes:
    """Build a cpmove-style tar.gz in memory. Plain files, one symlink, one
    traversal member — the engine must skip the last two."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in _MEMBERS:
            content = (override_content or {}).get(name, data)
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        if with_symlink:
            info = tarfile.TarInfo(name="homedir/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "index.php"
            tar.addfile(info)
        if with_traversal:
            info = tarfile.TarInfo(name="../evil.txt")
            info.size = 5
            tar.addfile(info, io.BytesIO(b"EVIL!"))
    return buf.getvalue()


def make_cpmove(root: Path, account: str = "acct", name: str | None = None,
                override_content: dict[str, bytes] | None = None) -> Path:
    """Write a cpmove tar.gz at drop_root/<account>/<name>."""
    name = name or f"cpmove-{account}.tar.gz"
    dest = root / "drop" / account / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_build_tar_bytes(override_content=override_content))
    return dest


def _cp_archive(env: Path, name: str, blob: bytes) -> Path:
    dest = env / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return dest


@pytest.fixture()
def drop_root(env):
    """A drop_root holding one valid cpmove archive beneath account dir acct."""
    make_cpmove(env, "acct")
    return env / "drop"


@pytest.fixture()
def ingest_cfg(env):
    from backupme.config import load_config
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(env / "drop")
    return cfg


# ---------------------------------------------------------------------------
# 1. sanitize_member
# ---------------------------------------------------------------------------

def test_sanitize_member_rejects_traversal_absolute_and_empty():
    assert cpanel_ingest.sanitize_member("../evil") is None
    assert cpanel_ingest.sanitize_member("/abs/path") is None
    assert cpanel_ingest.sanitize_member("C:\\Windows") is None
    assert cpanel_ingest.sanitize_member("") is None
    assert cpanel_ingest.sanitize_member("a/../../etc/passwd") is None


def test_sanitize_member_passes_and_normalizes():
    assert cpanel_ingest.sanitize_member("homedir/index.php") == "homedir/index.php"
    assert cpanel_ingest.sanitize_member("mysql\\db.sql") == "mysql/db.sql"
    assert cpanel_ingest.sanitize_member("./ok.txt") == "ok.txt"


# ---------------------------------------------------------------------------
# 2. discover
# ---------------------------------------------------------------------------

def test_discover_finds_archive_and_derives_account(drop_root):
    items = cpanel_ingest.discover(str(drop_root))
    archives = [i for i in items if i["type"] == "archive"]
    assert len(archives) == 1
    assert archives[0]["account"] == "acct"
    assert archives[0]["path"].endswith("cpmove-acct.tar.gz")


def test_discover_ignores_dotdirs_and_done(drop_root, env):
    dropped = _cp_archive(env, "drop/.hidden/acct/cpmove-acct.tar.gz",
                          _build_tar_bytes(with_symlink=False, with_traversal=False))
    assert dropped.exists()
    items = cpanel_ingest.discover(str(drop_root))
    assert all(".hidden" not in i["path"] for i in items)
    # .done contents never listed
    done_dir = drop_root / ".done" / "acct"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "cpmove-old.tar.gz").write_bytes(b"old")
    items2 = cpanel_ingest.discover(str(drop_root))
    assert all(".done" not in i["path"] for i in items2)


def test_discover_classifies_plain_files(drop_root):
    (drop_root / "acct" / "notes.txt").write_text("hello")
    items = cpanel_ingest.discover(str(drop_root))
    types = {i["type"] for i in items}
    assert types == {"archive", "file"}


# ---------------------------------------------------------------------------
# 3. estimate_archive
# ---------------------------------------------------------------------------

def test_estimate_archive_counts_without_extraction(drop_root, env):
    archive = make_cpmove(env, "acct2")
    est = cpanel_ingest.estimate_archive(str(archive))
    assert est["members"] == 3
    total = sum(len(d) for _, d in _MEMBERS)
    assert est["bytes"] == total
    assert est["skipped"] == 1  # the symlink
    # No extraction happened.
    assert not (env / "staging").exists()


# ---------------------------------------------------------------------------
# 4. gen1 ingest_tree
# ---------------------------------------------------------------------------

def test_ingest_tree_end_to_end(drop_root, env, backends, ingest_cfg):
    from backupme.core import state as state_mod
    archive = drop_root / "acct" / "cpmove-acct.tar.gz"
    archive_sha = cpanel_ingest.hasher_mod.sha256_file(str(archive))

    out = cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)

    assert out["status"] == "ok"
    assert out["archives"] == 1
    assert out["files_ignored"] == 0
    r = out["results"][0]
    assert r["status"] == "ok"
    assert r["members"] == 3

    # 3 member rows indexed under cpanel/acct; symlink+traversal not indexed.
    rows = execute(
        "SELECT relative_path FROM files WHERE source_root = ? ORDER BY relative_path",
        ("cpanel/acct",),
    ).fetchall()
    names = sorted(row[0] for row in rows)
    assert names == ["homedir/config.php", "homedir/index.php", "mysql/db.sql"]

    # op_log committed row with the idempotency key.
    op = execute(
        "SELECT status, idempotency_key FROM op_log WHERE op_type = 'cpanel_ingest'",
    ).fetchone()
    assert op is not None
    assert op[0] == "committed"
    assert op[1] == f"cpanel_ingest:{archive_sha}"

    # content_text NULL (no FTS extraction for ingest members).
    nulls = execute(
        "SELECT COUNT(*) FROM files f LEFT JOIN files_fts t ON f.id = t.rowid "
        "WHERE f.source_root = 'cpanel/acct' AND t.content_text IS NULL",
    ).fetchone()[0]
    assert nulls == 3

    # manifest json with 3 members.
    manifests = list((env / "manifest").glob("*.json"))
    assert len(manifests) == 1
    mdata = json.loads(manifests[0].read_text())
    assert mdata["account"] == "acct"
    assert mdata["archive_sha256"] == archive_sha
    assert len(mdata["members"]) == 3

    # archive moved to .done
    assert not archive.exists()
    assert (drop_root / ".done" / "acct" / "cpmove-acct.tar.gz").exists()

    # staging cleaned: only the empty per-archive staging dir remains.
    staging = Path(ingest_cfg.cpanel_ingest.staging_dir)
    assert staging.exists()
    assert all(not p.is_file() for p in staging.rglob("*") if p.is_file())

    # checkpoint recorded.
    ck = state_mod.get_last_checkpoint()
    assert ck is not None and ck["type"] == "cpanel_ingest"
    assert ck["data"]["total"] == 3


# ---------------------------------------------------------------------------
# 5. gen2: idempotent skip, then changed archive dedup
# ---------------------------------------------------------------------------

def test_gen2_same_archive_is_skipped_by_idempotency(drop_root, env, backends,
                                                     ingest_cfg):
    cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    done_copy = drop_root / ".done" / "acct" / "cpmove-acct.tar.gz"
    same = done_copy.read_bytes()  # identical archive placed again
    fresh = _cp_archive(env, "drop/acct/cpmove-acct.tar.gz", same)
    assert fresh.exists()

    out = cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["status"] == "skipped"
    assert r["reason"] == "already ingested"
    # Old copy still in .done; nothing re-ingested (still 3 rows).
    n = execute(
        "SELECT COUNT(*) FROM files WHERE source_root = 'cpanel/acct' "
        "AND version_seq = 1",
    ).fetchone()[0]
    assert n == 3


def test_gen2_changed_archive_dedups_unchanged_members(drop_root, env, backends,
                                                       ingest_cfg):
    from backupme.core.database import get_connection, commit
    cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)

    blobs_before = execute("SELECT COUNT(*) FROM blobs").fetchone()[0]

    changed = _build_tar_bytes(override_content={
        "homedir/index.php": b"<?php echo 2; // changed",
    })
    _cp_archive(env, "drop/acct/cpmove-acct.tar.gz", changed)

    out = cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    r = out["results"][0]
    assert r["status"] == "ok"
    assert r["members"] == 3
    # config.php + db.sql must have been dedup hits; only index.php content
    # created new blob rows.
    assert r["dupe"] == 2

    blobs_after = execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    # One changed member's content arrives via its own upload_bundle call:
    # a bundle blob plus its member blob. Dedup hits create no blob rows.
    assert blobs_after - blobs_before == 2

    # All three members now have version_seq=2; v1 rows still alive.
    v2 = execute(
        "SELECT COUNT(*) FROM files WHERE source_root = 'cpanel/acct' AND version_seq = 2",
    ).fetchone()[0]
    assert v2 == 3
    v1 = execute(
        "SELECT COUNT(*) FROM files WHERE source_root = 'cpanel/acct' "
        "AND version_seq = 1 AND deleted_at IS NULL",
    ).fetchone()[0]
    assert v1 == 3


# ---------------------------------------------------------------------------
# 6. checkpoint
# ---------------------------------------------------------------------------

def test_checkpoint_type_and_total(drop_root, env, backends, ingest_cfg):
    """Beyond type/total (pinned in the e2e test): the checkpoint identifies
    the archive and its last processed member."""
    from backupme.core import state as state_mod
    archive = drop_root / "acct" / "cpmove-acct.tar.gz"
    archive_sha = cpanel_ingest.hasher_mod.sha256_file(str(archive))
    cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    ck = state_mod.get_last_checkpoint()
    assert ck["type"] == "cpanel_ingest"
    assert ck["data"]["total"] == 3
    assert ck["data"]["done"] == 3
    assert ck["data"]["account"] == "acct"
    assert ck["data"]["archive"].endswith("cpmove-acct.tar.gz")
    assert ck["data"]["sha256"] == archive_sha
    # The tar's last file member, not the symlink/traversal skips.
    assert ck["data"]["members"][0]["name"] == "mysql/db.sql"


# ---------------------------------------------------------------------------
# 7. progress callback
# ---------------------------------------------------------------------------

def test_progress_cb_called_monotonically(drop_root, env, backends, ingest_cfg):
    calls: list[tuple[str, int, int]] = []
    out = cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg,
                                    progress_cb=lambda name, done, total:
                                    calls.append((name, done, total)))
    assert out["status"] == "ok"
    dones = [c[1] for c in calls]
    assert dones == [1, 2, 3]
    assert all(c[2] == 3 for c in calls)


# ---------------------------------------------------------------------------
# 8/9. restore_cpanel roundtrip + retar
# ---------------------------------------------------------------------------

def test_restore_roundtrip_with_filter(drop_root, env, backends, ingest_cfg):
    cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    out = cpanel_ingest.restore_cpanel("acct", 1, member_filter="homedir/*",
                                       out=str(env / "restore_out"),
                                       backends=backends, config=ingest_cfg)
    assert out["status"] == "ok"
    assert out["members"] == 2
    base = env / "restore_out" / "acct"
    assert (base / "homedir" / "index.php").read_bytes() == b"<?php echo 1;"
    assert (base / "homedir" / "config.php").read_bytes() == b"<?php 1234567890"
    assert not (base / "mysql").exists()


def test_restore_retar_lists_filtered_members(drop_root, env, backends,
                                              ingest_cfg):
    cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    retar_path = env / "out.tar.gz"
    out = cpanel_ingest.restore_cpanel("acct", 1, member_filter="homedir/*",
                                       out=str(env / "restore_out"),
                                       retar=str(retar_path),
                                       backends=backends, config=ingest_cfg)
    assert out["status"] == "ok"
    assert out["retar"] == str(retar_path)
    assert retar_path.exists()
    with tarfile.open(retar_path, "r:gz") as tar:
        names = sorted(tar.getnames())
    assert names == ["homedir/config.php", "homedir/index.php"]


# ---------------------------------------------------------------------------
# 10. failure path
# ---------------------------------------------------------------------------

def test_corrupt_archive_fails_without_breaking_others(env, backends, ingest_cfg):
    from backupme.core.database import execute as q
    good = make_cpmove(env, "goodacct")
    bad = _cp_archive(env, "drop/badacct/cpmove-bad.tar.gz", b"THIS IS NOT A TAR")
    out = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)

    assert out["status"] == "partial"
    by_account = {r.get("account"): r for r in out["results"]}
    assert by_account["badacct"]["status"] == "failed"
    assert by_account["goodacct"]["status"] == "ok"
    assert "error" in by_account["badacct"]

    # op_log: bad archive has a failed row; good one committed.
    rows = execute(
        "SELECT status FROM op_log WHERE op_type = 'cpanel_ingest' ORDER BY id",
    ).fetchall()
    statuses = {row[0] for row in rows}
    assert statuses == {"committed", "failed"}

    # Bad archive stays in place (no .done move on failure).
    assert bad.exists()
    assert (env / "drop" / ".done" / "goodacct" / good.name).exists()


# ---------------------------------------------------------------------------
# 11. keep_raw
# ---------------------------------------------------------------------------

def test_keep_raw_uploads_raw_archive(drop_root, env, backends):
    cfg_path = env / "config.yaml"
    text = cfg_path.read_text()
    text += (
        "cpanel_ingest:\n"
        "  enabled: true\n"
        f"  drop_root: {(env / 'drop').as_posix()}\n"
        "  keep_raw: 1\n"
    )
    cfg_path.write_text(text)
    from backupme.backends import invalidate_cache
    invalidate_cache()
    from backupme.config import load_config
    ingest_cfg = load_config()
    assert ingest_cfg.cpanel_ingest.keep_raw == 1

    out = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)
    assert out["status"] == "ok"
    rows = execute(
        "SELECT relative_path FROM files WHERE source_root = 'cpanel-raw/acct'",
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "cpmove-acct.tar.gz"


def test_keep_raw_zero_no_raw_rows(drop_root, env, backends, ingest_cfg):
    cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    rows = execute(
        "SELECT COUNT(*) FROM files WHERE source_root LIKE 'cpanel-raw/%'",
    ).fetchone()[0]
    assert rows == 0


# ---------------------------------------------------------------------------
# 12. encrypted ingest end-to-end (QA coverage H1)
# ---------------------------------------------------------------------------

def test_ingest_tree_end_to_end_encrypted(enc_env):
    """Full path staging -> upload_bundle -> AES-GCM blob -> restore with
    encryption enabled. Guards the quiet-plaintext failure: _encrypt_data
    falls back to plaintext when the key file is missing, so a mis-wired
    config would store readable member content while the operator believes
    the vault is encrypted."""
    from backupme.config import load_config
    from backupme.backends import get_all_backends

    make_cpmove(enc_env)
    cfg = load_config()
    cfg.cpanel_ingest.enabled = True
    cfg.cpanel_ingest.drop_root = str(enc_env / "drop")
    backends = get_all_backends(cfg)

    out = cpanel_ingest.ingest_tree(str(enc_env / "drop"), backends, cfg)
    assert out["status"] == "ok"
    assert all(r["status"] == "ok" for r in out["results"])

    # Every storage row is recorded as encrypted.
    n_enc = execute("SELECT COUNT(*) FROM blob_storage WHERE encrypted = 1").fetchone()[0]
    n_all = execute("SELECT COUNT(*) FROM blob_storage").fetchone()[0]
    assert n_all >= 1
    assert n_enc == n_all

    # Nothing on disk under the storage root carries the plaintext marker.
    marker = b"<?php echo 1;"
    storage_root = enc_env.parent / "storage"
    leaked = [
        f for f in storage_root.rglob("*")
        if f.is_file() and f.suffix != ".json"
        and marker in f.read_bytes()
    ]
    assert leaked == [], f"plaintext member content stored at: {leaked}"

    # Restore roundtrip decrypts: plaintext comes back byte-for-byte.
    r = cpanel_ingest.restore_cpanel("acct", 1, out=str(enc_env / "restore_out"),
                                     backends=backends, config=cfg)
    assert r["status"] == "ok"
    assert r["members"] == 3
    base = enc_env / "restore_out" / "acct"
    assert (base / "homedir" / "index.php").read_bytes() == b"<?php echo 1;"
    assert (base / "mysql" / "db.sql").read_bytes() == b"INSERT INTO x VALUES (1);"


# ---------------------------------------------------------------------------
# 13. skip counters: max_member_size_gb + exclude_members (QA coverage H2/M2)
# ---------------------------------------------------------------------------

def test_max_member_size_skips_large_member_and_reports_it(env, backends, ingest_cfg):
    """A member over max_member_size_gb is skipped, counted in the result
    payload AND the generation manifest — never silently missing."""
    ingest_cfg.cpanel_ingest.max_member_size_gb = 0.0000005   # ~0.5 KB
    make_cpmove(env, "acct", override_content={"homedir/config.php": b"x" * 4096})
    out = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["status"] == "ok"
    assert r["members"] == 2            # config.php skipped
    assert r["skipped_oversize"] == 1
    assert r["excluded"] == 0

    # The skipped member is not in the vault.
    assert not execute(
        "SELECT 1 FROM files WHERE source_root='cpanel/acct' "
        "AND relative_path='homedir/config.php'"
    ).fetchone()

    # Manifest carries the counters.
    manifests = list((env / "manifest").glob("*.json"))
    mdata = json.loads(manifests[0].read_text())
    assert mdata["skipped_oversize"] == 1
    assert mdata["excluded"] == 0
    assert mdata["indexed"] == 2 and mdata["estimated"] == 3


def test_exclude_members_globs_skip_matching_members(env, backends, ingest_cfg):
    make_cpmove(env)
    ingest_cfg.cpanel_ingest.exclude_members = ["mysql/*", "*/config.php"]
    out = cpanel_ingest.ingest_tree(str(env / "drop"), backends, ingest_cfg)
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["members"] == 1            # only homedir/index.php survives
    assert r["excluded"] == 2
    assert r["skipped_oversize"] == 0
    n = execute(
        "SELECT COUNT(*) FROM files WHERE source_root='cpanel/acct'").fetchone()[0]
    assert n == 1
    manifests = list((env / "manifest").glob("*.json"))
    mdata = json.loads(manifests[0].read_text())
    assert mdata["excluded"] == 2


# ---------------------------------------------------------------------------
# 14. restore edge cases (QA coverage M5)
# ---------------------------------------------------------------------------

def test_restore_unknown_account_is_error_not_ok(env, backends, ingest_cfg):
    """QA M5: a restore of something that was never ingested must be
    detectably failed — status 'error', not ok/0."""
    r = cpanel_ingest.restore_cpanel("ghost", 99, out=str(env / "restore_out"),
                                     backends=backends, config=ingest_cfg)
    assert r["status"] == "error"
    assert "no ingested generation 99 for account 'ghost'" in r["error"]
    assert r["members"] == 0

    # Unknown generation of a REAL account is the same error.
    r2 = cpanel_ingest.restore_cpanel("acct", 42, out=str(env / "restore_out"),
                                      backends=backends, config=ingest_cfg)
    assert r2["status"] == "error"


def test_restore_filter_matching_nothing_is_ok_zero(drop_root, env, backends, ingest_cfg):
    """The generation exists; the filter simply selects nothing — a
    legitimate empty result, not an error."""
    cpanel_ingest.ingest_tree(str(drop_root), backends, ingest_cfg)
    r = cpanel_ingest.restore_cpanel("acct", 1, member_filter="nomatch/*",
                                     out=str(env / "restore_out"),
                                     backends=backends, config=ingest_cfg)
    assert r["status"] == "ok"
    assert r["members"] == 0

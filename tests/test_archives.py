"""Extract-first archive ingest (Phase 1): tests.

Covers the 11 agreed scenarios: nested-archive ingest + fingerprint identity,
archive-level and member-level dedup, whole-blob fallbacks (corrupt zip,
unsupported rar, size cap), zip-slip safety, manifest sidecars, restore, the
backup CLI wiring, and the schema/migration.
"""
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli as cli_group
from backupme.core import archives as archives_mod
from backupme.core.database import commit, execute
from tests.helpers import load_cfg

INNER_MEMBERS = {
    "inner/one.txt": b"inner file one",
    "inner/two.bin": bytes(range(256)),
}


# ---------------------------------------------------------------------------
# Archive builders / helpers
# ---------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def build_tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 1_700_000_000
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def write_file(root: Path, rel: str, data: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def expected_fingerprint(member_items: list[tuple[str, str, int]]) -> str:
    digest = hashlib.sha256()
    for path, sha, size in sorted(member_items):
        digest.update(f"{path}\0{sha}\0{size}\n".encode("utf-8"))
    return digest.hexdigest()


def ingest_bytes(root: Path, name: str, data: bytes, backends) -> dict:
    path = write_file(root, name, data)
    return archives_mod.ingest_archive_file(str(path), backends, load_cfg())


def archive_row(fp: str):
    return execute(
        "SELECT * FROM archives WHERE fingerprint = ?", (fp,)
    ).fetchone()


def member_rows(fp: str):
    return execute(
        "SELECT relative_path, sha256, size FROM files "
        "WHERE source_root = ? ORDER BY relative_path",
        (f"archive/{fp}",),
    ).fetchall()


def backend_storage_root(env: Path) -> Path:
    return Path(load_cfg().backends["local"][0].path)


def member_paths(fp: str) -> set[str]:
    return {r["relative_path"] for r in member_rows(fp)}


# ---------------------------------------------------------------------------
# 1. tar.gz with nested zip: rows, fingerprint identity, member roots
# ---------------------------------------------------------------------------

def test_tar_gz_with_nested_zip_ingests_members(env, backends):
    inner_zip = build_zip_bytes(INNER_MEMBERS)
    members = {
        "docs/readme.txt": b"hello archive",
        "data/blob.bin": bytes(range(256)) * 4,
        "notes/plain.txt": b"plain text member",
        "bundles/inner.zip": inner_zip,
        "empty/zero.txt": b"",
    }
    result = ingest_bytes(Path(env), "src/backup.tar.gz",
                          build_tar_gz(members), backends)

    assert result["status"] == "archive_ingested"
    assert result["stored_whole"] is False
    assert result["unsafe_members"] == 0
    fp = result["fingerprint"]

    # Fingerprint is exactly the manifest hash over the sorted members.
    items = [(path, _sha(content), len(content))
             for path, content in members.items()]
    assert fp == expected_fingerprint(items)

    # The archives row describes the virtual manifest.
    row = archive_row(fp)
    assert row is not None
    assert row["format"] == "tar.gz"
    assert row["original_name"] == "backup.tar.gz"
    assert row["member_count"] == 5
    assert row["total_uncompressed"] == sum(len(c) for c in members.values())
    assert row["original_size"] == len(build_tar_gz(members))

    # Members are files rows under archive/<fp>/ with correct relative paths.
    got = {r["relative_path"]: (r["sha256"], r["size"]) for r in member_rows(fp)}
    assert set(got) == set(members)
    for path, content in members.items():
        assert got[path] == (_sha(content), len(content))

    # The nested zip got its OWN archive row + members under its own root.
    inner_fp = expected_fingerprint(
        [(p, _sha(c), len(c)) for p, c in INNER_MEMBERS.items()])
    inner_row = archive_row(inner_fp)
    assert inner_row is not None
    assert inner_row["format"] == "zip"
    assert {r["relative_path"] for r in member_rows(inner_fp)} == set(INNER_MEMBERS)

    # The parent's member entry for the zip carries nested_archive=<inner fp>.
    link = execute(
        "SELECT am.nested_archive AS na FROM archive_members am "
        "JOIN archives a ON a.id = am.archive_id "
        "WHERE a.fingerprint = ? AND am.relative_path = 'bundles/inner.zip'",
        (fp,),
    ).fetchone()
    assert link is not None and link["na"] == inner_fp


def test_nested_plain_tar_member_gets_own_fingerprint(env, backends):
    # A nested member in PLAIN tar format: its magic ("ustar") sits at offset
    # 257, so the plan pass must peek deep enough to recurse into it.
    inner_tar = io.BytesIO()
    with tarfile.open(fileobj=inner_tar, mode="w") as tar:
        payload = b"deep inside a plain tar"
        info = tarfile.TarInfo(name="deep/file.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    members = {"outer.txt": b"outer", "wrapped.tar": inner_tar.getvalue()}
    result = ingest_bytes(Path(env), "wrapping.tar.gz",
                          build_tar_gz(members), backends)

    assert result["status"] == "archive_ingested"
    inner_fp = expected_fingerprint(
        [("deep/file.txt", _sha(payload), len(payload))])
    inner_row = archive_row(inner_fp)
    assert inner_row is not None
    assert inner_row["format"] == "tar"
    assert member_paths(inner_fp) == {"deep/file.txt"}
    outer_fp = result["fingerprint"]
    link = execute(
        "SELECT am.nested_archive AS na FROM archive_members am "
        "JOIN archives a ON a.id = am.archive_id "
        "WHERE a.fingerprint = ? AND am.relative_path = 'wrapped.tar'",
        (outer_fp,),
    ).fetchone()
    assert link is not None and link["na"] == inner_fp


# ---------------------------------------------------------------------------
# 2. Archive-level dedup: the same archive again is a pure no-op
# ---------------------------------------------------------------------------

def test_same_archive_twice_is_archive_dedup(env, backends):
    members = {"a.txt": b"alpha", "sub/b.txt": b"bravo"}
    data = build_tar_gz(members)
    r1 = ingest_bytes(Path(env), "one/first.tar.gz", data, backends)
    assert r1["status"] == "archive_ingested"

    members_before = execute("SELECT COUNT(*) FROM archive_members").fetchone()[0]
    blobs_before = execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stored_size), 0) AS b FROM blob_storage"
    ).fetchone()
    files_before = sorted(
        str(p) for p in backend_storage_root(Path(env)).rglob("*") if p.is_file())

    # Same content at a DIFFERENT path: identity is the fingerprint, not the file.
    r2 = ingest_bytes(Path(env), "other/second.tar.gz", data, backends)

    assert r2["status"] == "archive_ingested"
    assert r2["dedup"] is True
    assert r2["stored_whole"] is False
    assert r2["fingerprint"] == r1["fingerprint"]

    members_after = execute("SELECT COUNT(*) FROM archive_members").fetchone()[0]
    assert members_after == members_before
    blobs_after = execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stored_size), 0) AS b FROM blob_storage"
    ).fetchone()
    assert (blobs_after["n"], blobs_after["b"]) == (blobs_before["n"], blobs_before["b"])
    files_after = sorted(
        str(p) for p in backend_storage_root(Path(env)).rglob("*") if p.is_file())
    assert files_after == files_before


# ---------------------------------------------------------------------------
# 3. Day-2 dedup: changed member uploads, unchanged members dedup
# ---------------------------------------------------------------------------

def test_changed_archive_dedups_unchanged_members(env, backends):
    members_a = {"a.txt": b"AAA", "b.txt": b"BBB", "c.txt": b"CCC"}
    r1 = ingest_bytes(Path(env), "gen1.tar.gz", build_tar_gz(members_a), backends)
    assert r1["status"] == "archive_ingested"
    blobs_before = {r["blob_id"] for r in
                    execute("SELECT blob_id FROM blobs").fetchall()}

    members_b = dict(members_a)
    members_b["b.txt"] = b"CHANGED CONTENT"
    r2 = ingest_bytes(Path(env), "gen2.tar.gz", build_tar_gz(members_b), backends)

    assert r2["status"] == "archive_ingested"
    fp_a, fp_b = r1["fingerprint"], r2["fingerprint"]
    assert fp_b != fp_a
    assert archive_row(fp_b) is not None

    blobs_after = {r["blob_id"] for r in
                   execute("SELECT blob_id FROM blobs").fetchall()}
    new_blobs = blobs_after - blobs_before
    # Only the changed member brings new content: its bundle blob + member blob
    # (same accounting the cpanel ingest pins in test_cpanel_ingest).
    assert len(new_blobs) == 2

    # Every unchanged member dedups: its chunk mapping points at a blob that
    # already existed before this ingest.
    for path in ("a.txt", "c.txt"):
        chunk = execute(
            "SELECT fc.blob_id AS b FROM file_chunks fc "
            "JOIN files f ON f.id = fc.file_id "
            "WHERE f.source_root = ? AND f.relative_path = ?",
            (f"archive/{fp_b}", path),
        ).fetchone()
        assert chunk is not None
        assert chunk["b"] in blobs_before

    # The changed member carries the new content under the new fingerprint.
    changed = execute(
        "SELECT sha256 FROM files WHERE source_root = ? AND relative_path = 'b.txt'",
        (f"archive/{fp_b}",),
    ).fetchone()
    assert changed["sha256"] == _sha(b"CHANGED CONTENT")


# ---------------------------------------------------------------------------
# 4. Whole-file fallback: corrupt (truncated) zip
# ---------------------------------------------------------------------------

def test_corrupt_zip_falls_back_to_whole(env, backends):
    good = build_zip_bytes({"a.txt": b"hello"})
    truncated = good[: len(good) // 2]
    path = write_file(Path(env), "broken.zip", truncated)

    result = archives_mod.ingest_archive_file(
        str(path), backends, load_cfg(),
        source_root=str(env), relative_path="broken.zip")

    assert result["status"] == "stored_whole"
    assert result["stored_whole"] is True
    assert result["reason"]
    # No virtual manifest, and the file went through the existing whole-blob
    # path (a files row for the archive itself exists).
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0
    assert execute("SELECT COUNT(*) FROM archive_members").fetchone()[0] == 0
    whole = execute(
        "SELECT id FROM files WHERE relative_path = 'broken.zip' "
        "AND deleted_at IS NULL").fetchone()
    assert whole is not None


# ---------------------------------------------------------------------------
# 5. Whole-file fallback: unsupported magic (.rar) via the backup CLI
# ---------------------------------------------------------------------------

def test_rar_named_file_stored_whole_via_cli(env):
    rar_path = env / "photos.rar"
    rar_path.write_bytes(b"Rar!\x1a\x07\x00" + b"fake rar payload" * 10)
    runner = CliRunner()
    result = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "backup", str(rar_path), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    source = payload["sources"][0]
    assert source["status"] == "stored_whole"
    assert "rar" in source["reason"]
    # It still took the normal whole-blob path (per-file entry as usual).
    assert payload["files"], "expected the normal upload entry"
    assert payload["files"][0]["status"] == "ok"
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 6. Zip-slip safety: unsafe member names are rejected, the rest is ingested
# ---------------------------------------------------------------------------

def test_zip_slip_members_are_skipped(env, backends):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", b"evil")
        zf.writestr("/absolute.txt", b"absolute")
        zf.writestr("safe/ok.txt", b"safe content")
    path = write_file(Path(env), "sneaky.zip", buf.getvalue())

    result = archives_mod.ingest_archive_file(str(path), backends, load_cfg())

    # Documented choice: unsafe members are skipped (mirroring cpanel ingest),
    # the archive is still ingested with the surviving members.
    assert result["status"] == "archive_ingested"
    assert result["unsafe_members"] == 2
    fp = result["fingerprint"]
    assert member_paths(fp) == {"safe/ok.txt"}
    # Nothing escaped the virtual root: no files row outside archive/<fp>.
    escaped = execute(
        "SELECT COUNT(*) FROM files WHERE relative_path IN "
        "('../evil.txt', 'absolute.txt', 'evil.txt')").fetchone()[0]
    assert escaped == 0


# ---------------------------------------------------------------------------
# 7. Caps: a member blowing the per-archive uncompressed cap -> whole fallback
# ---------------------------------------------------------------------------

def test_size_cap_falls_back_whole(env, backends, monkeypatch):
    monkeypatch.setattr(archives_mod, "MAX_TOTAL_UNCOMPRESSED", 1024)
    members = {"big.bin": b"x" * 2048}
    path = write_file(Path(env), "big.tar.gz", build_tar_gz(members))

    result = archives_mod.ingest_archive_file(str(path), backends, load_cfg())

    assert result["status"] == "stored_whole"
    assert result["stored_whole"] is True
    assert "cap" in result["reason"]
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0


def test_member_cap_falls_back_whole(env, backends, monkeypatch):
    monkeypatch.setattr(archives_mod, "MAX_MEMBERS", 2)
    members = {"a.txt": b"1", "b.txt": b"2", "c.txt": b"3"}
    path = write_file(Path(env), "many.tar.gz", build_tar_gz(members))

    result = archives_mod.ingest_archive_file(str(path), backends, load_cfg())

    assert result["status"] == "stored_whole"
    assert "cap" in result["reason"]


# ---------------------------------------------------------------------------
# 8. Manifest sidecar on the backend
# ---------------------------------------------------------------------------

def test_manifest_sidecar_written_to_backend(env, backends):
    inner_zip = build_zip_bytes(INNER_MEMBERS)
    members = {"m/a.txt": b"AAA", "m/b.txt": b"BBB", "nested.zip": inner_zip}
    result = ingest_bytes(Path(env), "withsidecar.tar.gz",
                          build_tar_gz(members), backends)
    fp = result["fingerprint"]
    inner_fp = expected_fingerprint(
        [(p, _sha(c), len(c)) for p, c in INNER_MEMBERS.items()])

    sidecar = (backend_storage_root(Path(env)) / "manifests" / fp[:2]
               / fp[2:4] / f"{fp}.json")
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["fingerprint"] == fp
    assert payload["format"] == "tar.gz"
    assert payload["member_count"] == len(members)
    assert payload["total_uncompressed"] == sum(len(c) for c in members.values())

    by_path = {m["path"]: m for m in payload["members"]}
    nested_entry = by_path["nested.zip"]
    assert nested_entry["nested_archive"] == inner_fp
    assert "chunks" not in nested_entry

    for path in ("m/a.txt", "m/b.txt"):
        entry = by_path[path]
        assert entry["chunks"], "non-nested member must carry a chunk recipe"
        file_id = execute(
            "SELECT id FROM files WHERE source_root = ? AND relative_path = ?",
            (f"archive/{fp}", path),
        ).fetchone()["id"]
        db_chunks = execute(
            "SELECT blob_id, pt_offset, pt_size FROM file_chunks "
            "WHERE file_id = ? ORDER BY seq",
            (file_id,),
        ).fetchall()
        assert [(c["blob_id"], c["offset"], c["length"]) for c in entry["chunks"]] == [
            (r["blob_id"], r["pt_offset"], r["pt_size"]) for r in db_chunks]


def test_manifest_sidecar_skipped_when_encrypted(enc_env):
    from backupme.backends import get_all_backends
    backends = get_all_backends(load_cfg())
    members = {"secret.txt": b"classified payload"}
    path = write_file(Path(enc_env), "secret.tar.gz",
                      build_tar_gz(members), )
    result = archives_mod.ingest_archive_file(str(path), backends, load_cfg())
    assert result["status"] == "archive_ingested"
    assert result["manifest_backends"] == []
    fp = result["fingerprint"]
    sidecar = (backend_storage_root(Path(enc_env)) / "manifests" / fp[:2]
               / fp[2:4] / f"{fp}.json")
    assert not sidecar.exists()
    # The DB manifest still records everything.
    assert archive_row(fp) is not None


# ---------------------------------------------------------------------------
# 9. Restore via the CLI (prefix fingerprint accepted, structure preserved)
# ---------------------------------------------------------------------------

def test_archive_restore_via_cli(env, backends):
    inner_zip = build_zip_bytes(INNER_MEMBERS)
    members = {
        "docs/readme.txt": b"restore me",
        "data/blob.bin": bytes(range(256)) * 3,
        "bundles/inner.zip": inner_zip,
    }
    result = ingest_bytes(Path(env), "restorable.tar.gz",
                          build_tar_gz(members), backends)
    fp = result["fingerprint"]
    out = env / "restore_out"

    runner = CliRunner()
    r_restore = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "archive", "restore", fp[:12], "--output", str(out), "--json"],
    )
    assert r_restore.exit_code == 0, r_restore.output
    payload = json.loads(r_restore.stdout)
    assert payload["status"] == "ok"
    assert payload["members"] == 3
    assert payload["fingerprint"] == fp

    # Every regular member is written and hashes to its manifest sha.
    for path, content in members.items():
        if path == "bundles/inner.zip":
            continue
        restored = out / path
        assert restored.is_file(), f"missing restored member {path}"
        assert _sha(restored.read_bytes()) == _sha(content)

    # The nested member is restored as a directory holding the nested
    # archive's own members (nested structure preserved).
    nested_one = out / "bundles" / "inner.zip" / "inner" / "one.txt"
    nested_two = out / "bundles" / "inner.zip" / "inner" / "two.bin"
    assert nested_one.read_bytes() == INNER_MEMBERS["inner/one.txt"]
    assert nested_two.read_bytes() == INNER_MEMBERS["inner/two.bin"]


def test_archive_show_and_list_via_cli(env, backends):
    members = {"x.txt": b"xmember", "y/y.txt": b"ymember"}
    result = ingest_bytes(Path(env), "showable.tar.gz",
                          build_tar_gz(members), backends)
    fp = result["fingerprint"]
    runner = CliRunner()

    r_list = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "archive", "list", "--json"],
    )
    assert r_list.exit_code == 0, r_list.output
    listed = json.loads(r_list.stdout)
    assert listed["count"] == 1
    row = listed["archives"][0]
    assert row["fingerprint"] == fp
    assert row["member_count"] == 2
    assert row["format"] == "tar.gz"
    assert row["original_name"] == "showable.tar.gz"

    r_show = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "archive", "show", fp[:10], "--json"],
    )
    assert r_show.exit_code == 0, r_show.output
    shown = json.loads(r_show.stdout)
    assert shown["archive"]["fingerprint"] == fp
    assert {m["relative_path"] for m in shown["members"]} == set(members)

    # Unknown fingerprint exits 1 with a clean error.
    r_missing = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "archive", "show", "f" * 64, "--json"],
    )
    assert r_missing.exit_code == 1
    assert json.loads(r_missing.stdout)["status"] == "error"


# ---------------------------------------------------------------------------
# 10. backup CLI wiring
# ---------------------------------------------------------------------------

def test_backup_recursive_mixes_archive_and_plain_file(env):
    mix = env / "mix"
    mix.mkdir()
    (mix / "bundle.tar.gz").write_bytes(
        build_tar_gz({"x.txt": b"xxx", "y.bin": b"yyy"}))
    (mix / "plain.txt").write_bytes(b"not an archive")

    runner = CliRunner()
    result = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "backup", str(mix), "--recursive", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    statuses = {f["relative_path"]: f for f in payload["files"]}
    assert set(statuses) == {"bundle.tar.gz", "plain.txt"}

    archived = statuses["bundle.tar.gz"]
    assert archived["status"] == "archive_ingested"
    assert archived["stored_whole"] is False
    assert archived["member_count"] == 2
    assert archived["fingerprint"]

    # The non-archive file in the same run takes the normal path, untouched.
    assert statuses["plain.txt"]["status"] == "ok"

    # The archive is recorded as a virtual manifest (never stored whole).
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1
    assert execute(
        "SELECT COUNT(*) FROM files WHERE relative_path = 'bundle.tar.gz'"
    ).fetchone()[0] == 0


def test_backup_direct_archive_source_reports_per_source(env):
    src = env / "single.zip"
    src.write_bytes(build_zip_bytes({"deep/file.txt": b"zipped"}))

    runner = CliRunner()
    result = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "backup", str(src), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    source = payload["sources"][0]
    assert source["status"] == "archive_ingested"
    assert source["stored_whole"] is False
    assert source["member_count"] == 1
    assert source["fingerprint"]


# ---------------------------------------------------------------------------
# 11. Schema + migration
# ---------------------------------------------------------------------------

def test_fresh_env_creates_archive_tables(env):
    names = {r["name"] for r in execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert {"archives", "archive_members"} <= names

    archive_cols = [c["name"] for c in execute("PRAGMA table_info(archives)").fetchall()]
    for col in ("id", "fingerprint", "format", "original_name", "original_size",
                "member_count", "total_uncompressed", "created_at"):
        assert col in archive_cols
    member_cols = [c["name"] for c in
                   execute("PRAGMA table_info(archive_members)").fetchall()]
    for col in ("id", "archive_id", "relative_path", "file_id", "member_sha256",
                "member_size", "mtime", "nested_archive"):
        assert col in member_cols

    indexes = {r["name"] for r in execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    assert "idx_archives_fp" in indexes
    assert "idx_arch_members_arc" in indexes


def test_migration_3_upgrades_existing_db(env):
    # Simulate a pre-archives database: drop the tables, rewind the version.
    execute("DROP TABLE archive_members")
    execute("DROP TABLE archives")
    execute("DELETE FROM schema_version WHERE version = 3")
    commit()

    from backupme.core import database as dbmod
    dbmod.initialize()

    names = {r[0] for r in execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert {"archives", "archive_members"} <= names

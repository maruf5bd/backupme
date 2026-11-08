"""Extract-first archive ingest (Phase 2a): 7z and rar format tests.

7z goes through py7zr (library, lazily imported); rar through an external
extractor (unrar/7z) when one exists on the machine. Same fixture and
ingest patterns as tests/test_archives.py: every format must produce the
same member model (files rows under archive/<fp>/), archive-level dedup,
caps-before-extraction, and the never-lose-data whole-blob fallback.

Real-rar tests skip with a reason when the machine has no extractor tool;
fixture CREATION additionally needs WinRAR's Rar.exe (never installed by
these tests).
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from backupme.cli import cli as cli_group
from backupme.core import archives as archives_mod
from backupme.core.database import execute
from tests.helpers import load_cfg


# ---------------------------------------------------------------------------
# Helpers (same shape as tests/test_archives.py)
# ---------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def build_7z_bytes(members: dict[str, bytes], password: str | None = None,
                   encrypted_header: bool = False) -> bytes:
    """Build a real .7z with py7zr (skips the test when py7zr is absent)."""
    py7zr = pytest.importorskip("py7zr")
    tmp = Path(tempfile.mkdtemp(prefix="arch2-7z-"))
    try:
        out = tmp / "out.7z"
        payload = tmp / "payload.bin"
        with py7zr.SevenZipFile(out, "w", password=password) as sz:
            if encrypted_header:
                sz.set_encrypted_header(True)
            for name, data in members.items():
                payload.write_bytes(data)
                sz.write(payload, arcname=name)
        return out.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_file(root: Path, rel: str, data: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def ingest_bytes(root: Path, name: str, data: bytes, backends) -> dict:
    path = write_file(root, name, data)
    return archives_mod.ingest_archive_file(str(path), backends, load_cfg())


def expected_fingerprint(member_items: list[tuple[str, str, int]]) -> str:
    digest = hashlib.sha256()
    for path, sha, size in sorted(member_items):
        digest.update(f"{path}\0{sha}\0{size}\n".encode("utf-8"))
    return digest.hexdigest()


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


def member_paths(fp: str) -> set[str]:
    return {r["relative_path"] for r in member_rows(fp)}


def backend_storage_root(env: Path) -> Path:
    return Path(load_cfg().backends["local"][0].path)


def run_cli(env: Path, *args) -> dict:
    runner = CliRunner()
    result = runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         *args],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# rar tooling on this machine (checked once, never installed by tests)
# ---------------------------------------------------------------------------

def _rar_creator_exe() -> str | None:
    """WinRAR's Rar.exe can CREATE rar fixtures; unrar/7z cannot."""
    for cand in (r"C:\Program Files\WinRAR\Rar.exe",
                 r"C:\Program Files (x86)\WinRAR\Rar.exe"):
        if os.path.isfile(cand):
            return cand
    return None


RAR_CREATOR = _rar_creator_exe()
HAS_RAR_TOOL = archives_mod.find_rar_tool() is not None

needs_rar_tool = pytest.mark.skipif(
    not HAS_RAR_TOOL, reason="no rar extractor (unrar/7z) on this machine")
needs_rar_creator = pytest.mark.skipif(
    RAR_CREATOR is None, reason="no Rar.exe available to create rar fixtures")


def build_real_rar(root: Path, members: dict[str, bytes], name: str = "t.rar",
                   password: str | None = None) -> Path:
    """Create a REAL rar archive with WinRAR's Rar.exe (rar5 format)."""
    assert RAR_CREATOR, "fixture requires Rar.exe"
    srcdir = root / "_rarsrc"
    for rel, data in members.items():
        p = srcdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    rar_path = root / name
    argv = [RAR_CREATOR, "a", "-ep1", "-r", "-y"]
    if password:
        argv.append(f"-p{password}")
    argv += [str(rar_path), str(srcdir) + os.sep]
    proc = subprocess.run(argv, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return rar_path


# ---------------------------------------------------------------------------
# 7z: ingest roundtrip + sha-verified restore + manifest sidecar
# ---------------------------------------------------------------------------

SEVENZ_MEMBERS = {
    "docs/readme.txt": b"hello seven",
    "data/blob.bin": bytes(range(256)) * 4,
    "empty/zero.txt": b"",
}


def test_7z_roundtrip_ingests_and_restores(env, backends):
    result = ingest_bytes(Path(env), "src/backup.7z",
                          build_7z_bytes(SEVENZ_MEMBERS), backends)

    assert result["status"] == "archive_ingested"
    assert result["stored_whole"] is False
    assert result["unsafe_members"] == 0
    fp = result["fingerprint"]

    # Fingerprint is exactly the manifest hash over the sorted members.
    items = [(path, _sha(content), len(content))
             for path, content in SEVENZ_MEMBERS.items()]
    assert fp == expected_fingerprint(items)

    # The archives row describes the virtual manifest.
    row = archive_row(fp)
    assert row is not None
    assert row["format"] == "7z"
    assert row["original_name"] == "backup.7z"
    assert row["member_count"] == 3
    assert row["total_uncompressed"] == sum(
        len(c) for c in SEVENZ_MEMBERS.values())
    assert row["original_size"] > 0

    # Members are files rows under archive/<fp>/ with correct relative paths.
    got = {r["relative_path"]: (r["sha256"], r["size"])
           for r in member_rows(fp)}
    assert got == {p: (_sha(c), len(c)) for p, c in SEVENZ_MEMBERS.items()}

    # Manifest sidecar landed on the backend with format=7z.
    sidecar = (backend_storage_root(Path(env)) / "manifests" / fp[:2]
               / fp[2:4] / f"{fp}.json")
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["format"] == "7z"
    assert payload["member_count"] == 3
    assert {m["path"] for m in payload["members"]} == set(SEVENZ_MEMBERS)

    # sha-verified restore via the existing restore path (CLI drives
    # restore_archive): every member comes back byte-identical.
    out = env / "restore_out"
    restored = run_cli(env, "archive", "restore", fp[:12],
                       "--output", str(out), "--json")
    assert restored["status"] == "ok"
    assert restored["members"] == 3
    assert restored["format"] == "7z"
    for path, content in SEVENZ_MEMBERS.items():
        assert (out / path).is_file(), f"missing restored member {path}"
        assert _sha((out / path).read_bytes()) == _sha(content)


def test_7z_with_nested_zip_gets_own_fingerprint(env, backends):
    inner_members = {"inner/one.txt": b"inner file one"}
    members = {
        "docs/a.txt": b"outer member",
        "bundles/inner.zip": build_zip_bytes(inner_members),
    }
    result = ingest_bytes(Path(env), "wrapping.7z",
                          build_7z_bytes(members), backends)

    assert result["status"] == "archive_ingested"
    assert result["nested_archives"] == 1
    outer_fp = result["fingerprint"]

    inner_fp = expected_fingerprint(
        [(p, _sha(c), len(c)) for p, c in inner_members.items()])
    inner_row = archive_row(inner_fp)
    assert inner_row is not None
    assert inner_row["format"] == "zip"
    assert member_paths(inner_fp) == set(inner_members)

    link = execute(
        "SELECT am.nested_archive AS na FROM archive_members am "
        "JOIN archives a ON a.id = am.archive_id "
        "WHERE a.fingerprint = ? AND am.relative_path = 'bundles/inner.zip'",
        (outer_fp,),
    ).fetchone()
    assert link is not None and link["na"] == inner_fp


# ---------------------------------------------------------------------------
# 7z: archive-level dedup — same content twice, zero new bytes
# ---------------------------------------------------------------------------

def test_7z_ingested_twice_same_fingerprint_zero_new_bytes(env, backends):
    data = build_7z_bytes({"a.txt": b"alpha", "sub/b.txt": b"bravo"})
    r1 = ingest_bytes(Path(env), "one/first.7z", data, backends)
    assert r1["status"] == "archive_ingested"

    members_before = execute(
        "SELECT COUNT(*) FROM archive_members").fetchone()[0]
    blobs_before = execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stored_size), 0) AS b "
        "FROM blob_storage").fetchone()
    files_before = sorted(
        str(p) for p in backend_storage_root(Path(env)).rglob("*")
        if p.is_file())

    # Same content at a DIFFERENT path: identity is the fingerprint.
    r2 = ingest_bytes(Path(env), "other/second.7z", data, backends)

    assert r2["status"] == "archive_ingested"
    assert r2["dedup"] is True
    assert r2["stored_whole"] is False
    assert r2["fingerprint"] == r1["fingerprint"]

    members_after = execute(
        "SELECT COUNT(*) FROM archive_members").fetchone()[0]
    assert members_after == members_before
    blobs_after = execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stored_size), 0) AS b "
        "FROM blob_storage").fetchone()
    assert (blobs_after["n"], blobs_after["b"]) == (blobs_before["n"],
                                                    blobs_before["b"])
    files_after = sorted(
        str(p) for p in backend_storage_root(Path(env)).rglob("*")
        if p.is_file())
    assert files_after == files_before


# ---------------------------------------------------------------------------
# 7z: whole-blob fallbacks (never lose data)
# ---------------------------------------------------------------------------

def test_corrupt_7z_stored_whole(env, backends):
    good = build_7z_bytes({"a.txt": b"hello"})
    truncated = good[: len(good) // 2]
    path = write_file(Path(env), "broken.7z", truncated)

    result = archives_mod.ingest_archive_file(
        str(path), backends, load_cfg(),
        source_root=str(env), relative_path="broken.7z")

    assert result["status"] == "stored_whole"
    assert result["stored_whole"] is True
    assert "7z" in result["reason"]
    # No virtual manifest; the file went through the existing whole-blob path.
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0
    assert execute("SELECT COUNT(*) FROM archive_members").fetchone()[0] == 0
    whole = execute(
        "SELECT id FROM files WHERE relative_path = 'broken.7z' "
        "AND deleted_at IS NULL").fetchone()
    assert whole is not None


def test_encrypted_header_7z_stored_whole(env, backends):
    result = ingest_bytes(Path(env), "locked.7z",
                          build_7z_bytes({"secret.txt": b"classified"},
                                         password="pw12345",
                                         encrypted_header=True),
                          backends)
    assert result["status"] == "stored_whole"
    assert result["stored_whole"] is True
    assert "password" in result["reason"]
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0


def test_encrypted_content_7z_stored_whole(env, backends):
    # Password on the content only (plain header): the archive OPENS, so the
    # fallback must come from the needs_password() check.
    result = ingest_bytes(Path(env), "locked2.7z",
                          build_7z_bytes({"secret.txt": b"classified"},
                                         password="pw12345"),
                          backends)
    assert result["status"] == "stored_whole"
    assert result["stored_whole"] is True
    assert "encrypted" in result["reason"]
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 7z: caps fire BEFORE extraction (list() metadata, no extracted bytes)
# ---------------------------------------------------------------------------

def test_7z_size_cap_fires_before_extraction(env, backends, monkeypatch):
    py7zr = pytest.importorskip("py7zr")
    monkeypatch.setattr(archives_mod, "MAX_TOTAL_UNCOMPRESSED", 16)

    def _must_not_extract(self, *args, **kwargs):
        raise AssertionError("caps must abort before any extraction")

    monkeypatch.setattr(py7zr.SevenZipFile, "extract", _must_not_extract)

    result = ingest_bytes(Path(env), "big.7z",
                          build_7z_bytes({"big.bin": b"x" * 64}), backends)

    assert result["status"] == "stored_whole"
    assert "cap" in result["reason"]
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0


def test_7z_member_cap_falls_back_whole(env, backends, monkeypatch):
    monkeypatch.setattr(archives_mod, "MAX_MEMBERS", 2)
    result = ingest_bytes(Path(env), "many.7z",
                          build_7z_bytes({"a.txt": b"1", "b.txt": b"2",
                                          "c.txt": b"3"}), backends)
    assert result["status"] == "stored_whole"
    assert "cap" in result["reason"]


# ---------------------------------------------------------------------------
# 7z: member-name safety (zip-slip) and detection without py7zr
# ---------------------------------------------------------------------------

def test_7z_zip_slip_members_are_skipped(env, backends):
    # py7zr happily STORES a traversal arcname; the plan pass must reject it
    # before it ever becomes an extraction target.
    result = ingest_bytes(Path(env), "sneaky.7z",
                          build_7z_bytes({"../evil.txt": b"evil",
                                          "safe/ok.txt": b"safe content"}),
                          backends)

    assert result["status"] == "archive_ingested"
    assert result["unsafe_members"] == 1
    fp = result["fingerprint"]
    assert member_paths(fp) == {"safe/ok.txt"}
    escaped = execute(
        "SELECT COUNT(*) FROM files WHERE relative_path IN "
        "('../evil.txt', 'absolute.txt', 'evil.txt')").fetchone()[0]
    assert escaped == 0


def test_7z_detection_toggles_with_py7zr(monkeypatch, env):
    sevenz_file = write_file(
        Path(env), "x.7z", b"7z\xbc\xaf\x27\x1c" + b"\x00" * 40)

    # With py7zr importable (default on this install): a supported format.
    assert archives_mod.detect_archive_format(str(sevenz_file)) == "7z"
    assert archives_mod.unsupported_archive_format(str(sevenz_file)) is None

    # Simulate a machine without py7zr: detection drops the format and the
    # unsupported path names the missing dependency.
    monkeypatch.setattr(archives_mod, "_py7zr_checked", True)
    monkeypatch.setattr(archives_mod, "_py7zr_available", False)
    assert archives_mod.detect_archive_format(str(sevenz_file)) is None
    reason = archives_mod.unsupported_archive_format(str(sevenz_file))
    assert reason is not None
    assert "7z" in reason and "py7zr" in reason


# ---------------------------------------------------------------------------
# rar: tool detection (fake tool on PATH, no-tool unsupported)
# ---------------------------------------------------------------------------

def test_rar_tool_found_on_path(monkeypatch, tmp_path):
    fake_dir = tmp_path / "faketools"
    fake_dir.mkdir()
    fake_tool = fake_dir / "unrar.exe"
    fake_tool.write_bytes(b"")
    fake_tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_dir) + os.pathsep
                       + os.environ.get("PATH", ""))

    monkeypatch.setattr(archives_mod, "_RAR_TOOL", None)
    monkeypatch.setattr(archives_mod, "_rar_tool_resolved", False)
    try:
        tool = archives_mod.find_rar_tool()
    finally:
        archives_mod.reset_rar_tool_cache()
    assert tool is not None
    assert tool["kind"] == "unrar"
    assert Path(tool["path"]) == fake_tool


def test_rar_no_tool_stays_unsupported(monkeypatch, env, backends):
    rar_file = write_file(
        Path(env), "photos.rar", b"Rar!\x1a\x07\x00" + b"payload" * 10)

    monkeypatch.setattr(archives_mod, "_RAR_TOOL", None)
    monkeypatch.setattr(archives_mod, "_rar_tool_resolved", True)
    try:
        assert archives_mod.detect_archive_format(str(rar_file)) is None
        reason = archives_mod.unsupported_archive_format(str(rar_file))
        assert reason is not None
        assert "rar" in reason
        assert "unrar" in reason or "7z" in reason

        # A full ingest without a tool stores the rar whole, naming the tool.
        result = archives_mod.ingest_archive_file(
            str(rar_file), backends, load_cfg(),
            source_root=str(env), relative_path="photos.rar")
        assert result["status"] == "stored_whole"
        assert "rar" in result["reason"]
        assert ("unrar" in result["reason"] or "7z" in result["reason"])
    finally:
        archives_mod.reset_rar_tool_cache()
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0


def test_rar_magic_detected_when_tool_exists(monkeypatch, env):
    rar_file = write_file(
        Path(env), "some.rar", b"Rar!\x1a\x07\x01\x00" + b"payload" * 10)
    # rar5 and rar4 share the first 6 bytes: both must classify as rar.
    monkeypatch.setattr(archives_mod, "_RAR_TOOL",
                        {"kind": "7z", "path": "unused-for-detection"})
    monkeypatch.setattr(archives_mod, "_rar_tool_resolved", True)
    try:
        assert archives_mod.detect_archive_format(str(rar_file)) == "rar"
        assert archives_mod.unsupported_archive_format(str(rar_file)) is None
    finally:
        archives_mod.reset_rar_tool_cache()


# ---------------------------------------------------------------------------
# rar: real roundtrips (skip cleanly when the machine lacks the tools)
# ---------------------------------------------------------------------------

RAR_MEMBERS = {
    "one.txt": b"rar member one",
    "sub/two.txt": b"rar member two",
}


@needs_rar_tool
@needs_rar_creator
def test_rar_real_roundtrip_ingests_and_restores(env, backends):
    rar_path = build_real_rar(Path(env), RAR_MEMBERS)
    result = archives_mod.ingest_archive_file(str(rar_path), backends,
                                              load_cfg())

    assert result["status"] == "archive_ingested"
    assert result["stored_whole"] is False
    fp = result["fingerprint"]

    row = archive_row(fp)
    assert row is not None
    assert row["format"] == "rar"
    assert row["original_name"] == "t.rar"
    assert row["member_count"] == 2

    got = {r["relative_path"]: (r["sha256"], r["size"])
           for r in member_rows(fp)}
    assert got == {p: (_sha(c), len(c)) for p, c in RAR_MEMBERS.items()}

    # sha-verified restore through restore_archive.
    out = env / "rar_restore"
    restored = archives_mod.restore_archive(fp, str(out))
    assert restored["status"] == "ok"
    assert restored["members"] == 2
    assert restored["format"] == "rar"
    for path, content in RAR_MEMBERS.items():
        assert _sha((out / path).read_bytes()) == _sha(content)


@needs_rar_tool
def test_rar_corrupt_stored_whole(env, backends):
    # Truncated rar bytes: the extractor exits nonzero -> whole-blob fallback.
    good = build_real_rar(Path(env), {"a.txt": b"hello"}) \
        if RAR_CREATOR else None
    if good is not None:
        data = good.read_bytes()[: good.stat().st_size // 2]
    else:
        data = b"Rar!\x1a\x07\x01\x00" + b"\x00" * 200
    path = write_file(Path(env), "broken.rar", data)

    result = archives_mod.ingest_archive_file(str(path), backends, load_cfg())

    assert result["status"] == "stored_whole"
    assert result["stored_whole"] is True
    assert "rar" in result["reason"]
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0
    whole = execute(
        "SELECT id FROM files WHERE relative_path = 'broken.rar' "
        "AND deleted_at IS NULL").fetchone()
    assert whole is not None


@needs_rar_tool
@needs_rar_creator
def test_rar_encrypted_stored_whole(env, backends):
    rar_path = build_real_rar(Path(env), {"secret.txt": b"classified"},
                              name="locked.rar", password="pw12345")
    result = archives_mod.ingest_archive_file(str(rar_path), backends,
                                              load_cfg())
    assert result["status"] == "stored_whole"
    assert result["stored_whole"] is True
    assert "rar" in result["reason"]
    assert ("password" in result["reason"]
            or "Incorrect password" in result["reason"])
    assert execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0

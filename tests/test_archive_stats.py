"""archive stats: dedup/savings statistics over the archive manifest tables.

Covers the core SQL math (per-hash unique logical bytes, dedup vs
uncompressed, delta vs original archive bytes), fingerprint scoping, and
the CLI surface (JSON + human output, prefix scoping, unknown -> exit 1).
"""
import hashlib
import json

from click.testing import CliRunner

from backupme.cli import cli as cli_group
from backupme.core import archives as archives_mod
from backupme.core.database import execute
from tests.test_archives import build_zip_bytes, ingest_bytes


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_stats_cli(env, *args):
    runner = CliRunner()
    return runner.invoke(
        cli_group,
        ["--config", str(env / "config.yaml"), "--db", str(env / "test.db"),
         "archive", "stats", *args],
    )


def test_stats_math_and_scoping(env, backends):
    x = b"shared content " * 10
    y = b"unique Y content " * 4
    z = bytes(range(256))
    w = b"only in archive b " * 3
    y2 = b"changed Y member content"
    a_members = {"a.txt": x, "b.txt": y, "sub/c.bin": z}
    b_members = {"a.txt": x, "dup.txt": x, "d.txt": w, "b.txt": y2}

    ra = ingest_bytes(env, "a.zip", build_zip_bytes(a_members), backends)
    rb = ingest_bytes(env, "b.zip", build_zip_bytes(b_members), backends)
    assert ra["status"] == "archive_ingested" and rb["status"] == "archive_ingested"
    fa, fb = ra["fingerprint"], rb["fingerprint"]
    assert fa != fb

    # --- all-archive stats -------------------------------------------------
    stats = archives_mod.get_archive_stats()
    assert stats["status"] == "ok"
    assert stats["scope"] == "all"
    assert stats["archives"] == 2
    assert stats["member_entries"] == len(a_members) + len(b_members)
    assert stats["file_members"] == stats["member_entries"]
    assert stats["nested_members"] == 0

    expected_uncompressed = sum(len(v) for v in a_members.values()) + \
        sum(len(v) for v in b_members.values())
    assert stats["total_uncompressed"] == expected_uncompressed

    # x is stored under 3 member entries but is ONE unique hash.
    sizes_by_hash: dict[str, int] = {}
    for members in (a_members, b_members):
        for data in members.values():
            h = _sha(data)
            sizes_by_hash[h] = max(sizes_by_hash.get(h, 0), len(data))
    assert stats["unique_member_hashes"] == len(sizes_by_hash) == 5
    expected_unique = sum(sizes_by_hash.values())
    assert stats["unique_logical_bytes"] == expected_unique

    expected_saved = expected_uncompressed - expected_unique
    assert stats["dedup_savings_bytes"] == expected_saved
    assert stats["dedup_ratio"] == round(expected_saved / expected_uncompressed, 6)

    rows = execute(
        "SELECT coalesce(sum(original_size), 0) AS s FROM archives"
    ).fetchone()
    original_total = int(rows["s"])
    assert stats["original_size"] == original_total
    assert stats["vs_original_delta_bytes"] == original_total - expected_unique

    # --- scoped to one fingerprint ----------------------------------------
    scoped = archives_mod.get_archive_stats([fa])
    assert scoped["scope"] == "selected"
    assert scoped["archives"] == 1
    assert scoped["member_entries"] == len(a_members)
    assert scoped["total_uncompressed"] == sum(len(v) for v in a_members.values())
    assert scoped["unique_member_hashes"] == 3

    # Empty fingerprint list behaves like "all".
    assert archives_mod.get_archive_stats([])["archives"] == 2


def test_stats_cli_json_human_and_errors(env, backends):
    a_members = {"a.txt": b"alpha content", "b.txt": b"beta content"}
    b_members = {"a.txt": b"alpha content", "c.txt": b"gamma content"}
    ra = ingest_bytes(env, "a.zip", build_zip_bytes(a_members), backends)
    rb = ingest_bytes(env, "b.zip", build_zip_bytes(b_members), backends)
    assert ra["status"] == "archive_ingested" and rb["status"] == "archive_ingested"

    r_json = _run_stats_cli(env, "--json")
    assert r_json.exit_code == 0, r_json.output
    payload = json.loads(r_json.stdout)
    assert payload["status"] == "ok"
    assert payload["archives"] == 2
    assert payload["unique_member_hashes"] == 3
    assert payload["dedup_savings_bytes"] > 0

    # Prefix scoping through the CLI.
    r_scoped = _run_stats_cli(env, "-f", ra["fingerprint"][:12], "--json")
    assert r_scoped.exit_code == 0, r_scoped.output
    assert json.loads(r_scoped.stdout)["archives"] == 1

    # Human output carries the summary lines.
    r_human = _run_stats_cli(env)
    assert r_human.exit_code == 0, r_human.output
    assert "Archive stats" in r_human.output
    assert "Dedup saved" in r_human.output

    # Unknown fingerprint exits 1 with a clean JSON error.
    r_missing = _run_stats_cli(env, "-f", "f" * 64, "--json")
    assert r_missing.exit_code == 1
    assert json.loads(r_missing.stdout)["status"] == "error"
